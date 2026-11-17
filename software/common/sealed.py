"""
sealed — "sealed records": the household's sensitive records are encrypted at rest, so a seized or
stolen Badger (disk pulled, box carried away) reveals nothing, while the Badger still restarts on its
own and keeps working (owner's decision; docs/SECURITY.md → "Sealed records").

How it works
  * A device sealing keypair (ECDH on NIST P-256). The PUBLIC key sits on disk in the clear, so the Badger can
    SEAL new records at any time, even while locked (write-only). The PRIVATE key is stored only
    encrypted, under a random key-encryption key (KEK) that is itself wrapped twice: by the owner
    passphrase (scrypt) and by a recovery key (256 random bits, shown once at setup).
  * Records are not public-key encrypted one by one. Each run of the portal (and each lock) makes a
    fresh random 256-bit data key (DEK) and wraps it for the public key, ECIES-style: an ephemeral P-256
    key, `openssl pkeyutl -derive` with the device public key, HMAC-SHA512 over the shared secret and
    both public keys → the vault's XOR+HMAC wrap. Stored in keys/<kid>.<fp>.dek. Records are sealed with
    the DEK using the vault's encrypt-then-MAC scheme
    (AES-256-CTR through the openssl program + HMAC-SHA256: features/_vault_crypto.py Cipher).
    Record = b"HBS1" | kid (8 bytes) | vault blob. Text form: "hbs1:" + base64url.
  * Locked after every restart. Unlocking (owner passphrase or recovery key, admin page over https or
    on the Badger itself) decrypts the private key into this process's memory only; DEKs are unwrapped
    on demand and cached in memory. "Lock now", the optional idle timer and posture "lockdown" forget
    them (best effort: Python can't promise to wipe every copy; bytearrays are overwritten).
  * While locked, the Badger still holds the current run's DEK (it made it): records sealed since the
    Badger last started or locked can be shown where a caller explicitly asks (open_(…, recent_ok=True)
    — the family board's "posts since it locked"). open_() without it raises Locked.
  * Before the owner sets a passphrase the keypair exists already ("provisional"): its KEK sits on disk
    next to it, so data is sealed from day one (nothing readable to a casual forensic grep) but NOT
    protected until setup. Setup makes a NEW keypair, rewraps every DEK under it, re-seals all data
    (migrators) and scrubs the provisional key. Flash wear-levelling may keep old blocks: set the
    passphrase at first boot.
  * Keys and passphrases go to openssl through pipes / inherited fds (/dev/fd/N), never argv.

Internal API (other code calls these on the portal's default instance, see use()):
  is_unlocked() · seal(bytes)->bytes · open_(bytes)->bytes (raises Locked) · lock_all() ·
  destroy_keys() — crypto-erase: scrub-deletes device.key and keys/*.dek, zeroises memory.
  hard_lock() — DURESS "lock": locked, and the passphrase alone no longer unlocks (only the recovery key,
    after which the passphrase works again). A flag in device.key ("hard_lock": {"at"}), no secret in it.
  set_duress()/clear_duress() — an optional DURESS passphrase: typed where the owner passphrase is asked,
    unlock() returns "duress" (the launcher shows the decoy and runs the duress action). Every passphrase try
    runs one scrypt for the passphrase and one for the duress passphrase (a dummy of the same cost when none
    is set), so the time doesn't tell which was typed or whether one exists.
  DecoySealer — the sealer a duress session uses: a throw-away key in the decoy's folder that reports itself
    like the real one (set up, its fingerprint), so the admin page looks as it always does.
  Text/JSON helpers: seal_text / open_text / seal_json / open_json / is_token.
  SealedMap: per-key sealed fields inside a JSON document, writable while locked.
  SealedJournal: sealed JSON-lines of operations written while locked, applied after unlock.

Files (state dir, default /var/lib/prepper/portal/sealed/, 0700):
  device.key          JSON 0600: public key PEM, fingerprint, encrypted private key, the wraps
  keys/<kid>.<fp>.dek wrapped data keys, 0600
  lockout.json        wrong-passphrase lockout that survives a restart
Standard library + the openssl program (OpenSSL 3 on the Badger; macOS LibreSSL works too).
"""
from __future__ import annotations

import base64
import collections
import contextlib
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MAGIC = b"HBS1"
KID_LEN = 8
TOKEN = "hbs1:"
KIND_RECORD, KIND_PRIV = 9, 10                 # vault-blob kind bytes (never shared with the vault's 0/1/2)
DEK_MAGIC = b"HBDEK1"
CURVE = "P-256"
ALG = "ecdh-p256-hmacsha512+aes256ctr-hmacsha256"
FAILS, FAIL_WINDOW, FAIL_LOCK = 5, 600.0, 600.0           # per address
G_FAILS, G_WINDOW, G_LOCK = 20, 3600.0, 900.0              # whole Badger (survives a restart)
MIN_PASS_CHARS, MIN_PASS_WORDS = 16, 4
CACHE_ITEMS, CACHE_BYTES = 20000, 32 << 20
LOCKED_NAME = "a family member"


class Locked(Exception):
    """Sealed data can't be read: the owner hasn't unlocked the Badger since it started (or it was locked)."""


class SealError(Exception):
    """Damaged, unknown key (crypto-erased), or the openssl program failed."""


class DuressSecret(Exception):
    """The duress passphrase was typed where the owner passphrase was asked (callers that act on it catch it)."""


HARD_MSG = ("Sealed records are locked with the recovery key only. Type the recovery key (13 groups of 4 letters "
            "and digits) to unlock; after that the owner passphrase works again.")


class Refused(Exception):
    """A request the owner-facing API turns down: .status (HTTP), message, extra fields."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def _load_vault_crypto():
    """Reuse the vault's crypto (features/_vault_crypto.py), loaded by path (features aren't a package)."""
    name = "badger_vault_crypto"
    if name in sys.modules:
        return sys.modules[name]
    here = Path(__file__).resolve()
    for cand in (here.parents[1] / "portal" / "features" / "_vault_crypto.py", here.with_name("_vault_crypto.py")):
        if cand.exists():
            spec = importlib.util.spec_from_file_location(name, cand)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("_vault_crypto.py not found")


vc = _load_vault_crypto()


# ── small helpers ──

def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64u(s: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError):
        raise SealError("damaged sealed record") from None


def _zero(b) -> None:
    if isinstance(b, bytearray):
        for i in range(len(b)):
            b[i] = 0


def is_token(v) -> bool:
    return isinstance(v, str) and v.startswith(TOKEN)


def passphrase_problem(pw) -> str | None:
    """None when good enough: at least 16 characters, or 4+ words (a short sentence)."""
    if not isinstance(pw, str) or not pw.strip():
        return "Enter a passphrase."
    norm = vc.norm_passphrase(pw).decode()
    if len(norm) > 200:
        return "That passphrase is too long (200 characters at most)."
    words = [w for w in re.split(r"[\s\-_.,;:]+", norm) if len(w) >= 2]
    if len(norm) >= MIN_PASS_CHARS or (len(words) >= MIN_PASS_WORDS and len(norm) >= 12):
        if len(set(norm.replace(" ", ""))) < 5:
            return "That passphrase is too easy to guess. Use a short sentence or four random words."
        return None
    return (f"Use at least {MIN_PASS_CHARS} characters, or four or more words "
            "(a short sentence you'll remember works well).")


def _fsync_dir(d: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write-then-rename, created with `mode` (0600 by default), fsync'd."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _zero_fill(path: Path) -> None:
    """Overwrite a file's bytes in place (best effort: flash wear-levelling may keep old blocks)."""
    try:
        fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return
    try:
        size = os.fstat(fd).st_size
        left = size
        while left > 0:
            n = os.write(fd, b"\0" * min(left, 1 << 16))
            if n <= 0:
                break
            left -= n
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def scrub_delete(path: Path) -> bool:
    """Zero a file, then unlink it. True when something was deleted."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        with contextlib.suppress(OSError):
            if path.is_symlink():
                path.unlink()
        return False
    _zero_fill(path)
    try:
        path.unlink()
    except OSError:
        return False
    _fsync_dir(path.parent)
    return True


def replace_scrubbing(path: Path, data: bytes | str, mode: int = 0o600) -> None:
    """Replace a file that held secrets or plaintext: the new content is written and renamed into place
    first (never a moment without a good file), then the OLD inode is zeroed and unlinked through a hard
    link kept for that purpose. A crash leaves at most a .scrub file, removed by sweep_scrubs()."""
    path = Path(path)
    data = data.encode("utf-8") if isinstance(data, str) else data
    keep = None
    if path.is_file() and not path.is_symlink():
        keep = path.with_name(f".{path.name}.{secrets.token_hex(4)}.scrub")
        try:
            os.link(path, keep)
        except OSError:
            keep = None
    write_private(path, data, mode)
    if keep is not None:
        scrub_delete(keep)


def sweep_scrubs(folder: Path) -> int:
    n = 0
    with contextlib.suppress(OSError):
        for p in Path(folder).glob(".*.scrub"):
            n += scrub_delete(p)
    return n


# ── the openssl program ──

def _run_openssl(openssl: str, args: list[str], data: bytes = b"", fds: dict | None = None,
                 timeout: float = 120) -> bytes:
    """Run openssl. `fds` {"name": bytes} (private keys, peer keys) are handed over on inherited pipes as
    /dev/fd/N — the caller writes "{name}" in args. Nothing secret ever appears in argv."""
    opened: list[int] = []
    try:
        for name, blob in (fds or {}).items():
            if len(blob) > 60000:
                raise SealError("key too large for a pipe")
            r, w = os.pipe()
            opened.append(r)
            try:
                os.write(w, bytes(blob))
            finally:
                os.close(w)
            args = [a.replace("{" + name + "}", f"/dev/fd/{r}") for a in args]
        try:
            p = subprocess.run([openssl] + args, input=data, capture_output=True, timeout=timeout,
                               pass_fds=tuple(opened), env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        except (OSError, subprocess.SubprocessError) as e:
            raise SealError(f"openssl failed: {type(e).__name__}") from None
    finally:
        for fd in opened:
            with contextlib.suppress(OSError):
                os.close(fd)
    if p.returncode != 0:
        raise SealError("openssl failed: " + p.stderr.decode(errors="replace")[:160].strip())
    return p.stdout


def _pub_fp(pub_pem: bytes) -> bytes:
    body = b"".join(ln.strip() for ln in pub_pem.splitlines() if ln and not ln.startswith(b"-----"))
    try:
        der = base64.b64decode(body, validate=True)
    except ValueError:
        raise SealError("damaged public key") from None
    return hashlib.sha256(der).digest()[:8]


# ── the sealer ──

class Sealer:
    def __init__(self, folder: Path, openssl: str = "/usr/bin/openssl", scrypt_n: int = vc.SCRYPT_N,
                 idle_minutes: float = 0, posture_file: Path | None = None, clock=time.time, mono=time.monotonic):
        self.dir = Path(folder)
        self.openssl = openssl
        self.scrypt_n = scrypt_n
        self.idle = max(0.0, float(idle_minutes or 0)) * 60
        self.posture_file = Path(posture_file) if posture_file else self.dir.parent / "posture.json"
        self.clock, self.mono = clock, mono
        self.lock = threading.RLock()
        self.kdf_lock = threading.Lock()
        self._priv: bytearray | None = None          # unlocked: the private key PEM
        self._deks: dict[bytes, bytearray] = {}      # kid → DEK (unlocked)
        self._session: tuple[bytes, bytearray] | None = None
        self._cache: collections.OrderedDict = collections.OrderedDict()
        self._cache_bytes = 0
        self._meta_cache = None                      # (stamp, dict)
        self._last_use = 0.0
        self._override = None                        # posture stamp the admin confirmed unlocking under
        self._posture = (0.0, None, "guarded")       # (checked at, stamp, level)
        self._fails: dict[str, list] = {}
        self._quiet = threading.local()
        self._openssl_ok = None
        self.locked_reason = None
        self.migrators: dict[str, object] = {}

    # ── files ──
    @property
    def meta_path(self) -> Path:
        return self.dir / "device.key"

    @property
    def keys_dir(self) -> Path:
        return self.dir / "keys"

    def paths(self) -> dict:
        """What destroy_keys() erases (for the panic wipe and the docs)."""
        return {"dir": str(self.dir), "device_key": str(self.meta_path), "data_keys": str(self.keys_dir / "*.dek"),
                "lockout": str(self.dir / "lockout.json")}

    def _ensure_dir(self) -> None:
        for d in (self.dir, self.keys_dir):
            d.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(d, 0o700)

    def meta(self) -> dict | None:
        try:
            st = self.meta_path.stat()
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            self._meta_cache = None
            return None
        if self._meta_cache and self._meta_cache[0] == stamp:
            return self._meta_cache[1]
        try:
            d = json.loads(self.meta_path.read_bytes())
        except (OSError, ValueError, RecursionError):
            raise SealError("the sealed-records key file is damaged (sealed/device.key)") from None
        if not isinstance(d, dict) or not isinstance(d.get("pub"), str) or not isinstance(d.get("priv"), str):
            raise SealError("the sealed-records key file is damaged (sealed/device.key)")
        self._meta_cache = (stamp, d)
        return d

    def _write_meta(self, d: dict) -> None:
        self._ensure_dir()
        replace_scrubbing(self.meta_path, json.dumps(d, indent=1).encode())
        self._meta_cache = None

    def is_setup(self) -> bool:
        m = self.meta()
        return bool(m and isinstance(m.get("wraps"), dict) and m["wraps"].get("pass"))

    def available(self) -> bool:
        if self._openssl_ok is None:
            try:
                self._openssl_ok = vc.openssl_ok(self.openssl) and bool(
                    _run_openssl(self.openssl, ["version"], timeout=10))
            except SealError:
                self._openssl_ok = False
        return self._openssl_ok

    # ── keys ──
    def _genkey(self) -> tuple[bytearray, bytes]:
        priv = bytearray(_run_openssl(self.openssl, ["genpkey", "-algorithm", "EC", "-pkeyopt",
                                                     f"ec_paramgen_curve:{CURVE}"], timeout=60))
        if b"PRIVATE KEY" not in priv:
            raise SealError("openssl made no key")
        pub = _run_openssl(self.openssl, ["pkey", "-in", "{k}", "-pubout"], fds={"k": priv})
        if b"PUBLIC KEY" not in pub:
            raise SealError("openssl made no public key")
        return priv, pub

    @staticmethod
    def _enc_priv(priv: bytearray, kek: bytes, openssl: str) -> str:
        return b64u(vc.Cipher(kek, openssl).encrypt_bytes(bytes(priv), KIND_PRIV))

    def _dec_priv(self, m: dict, kek: bytes) -> bytearray:
        try:
            return bytearray(vc.Cipher(kek, self.openssl).decrypt_bytes(unb64u(m["priv"]), KIND_PRIV))
        except vc.CryptoError as e:
            raise SealError(f"the private key can't be decrypted ({e})") from None

    def _ensure_keys(self) -> dict:
        """The keypair, made on first use (provisional until the owner sets a passphrase)."""
        with self.lock:
            m = self.meta()
            if m:
                return m
            if not self.available():
                raise SealError("sealing needs the openssl program, which isn't working on this Badger")
            self._ensure_dir()
            priv, pub = self._genkey()
            kek = secrets.token_bytes(32)
            m = {"v": 1, "alg": ALG, "created": int(self.clock()),
                 "pub": pub.decode(), "fp": _pub_fp(pub).hex(), "priv": self._enc_priv(priv, kek, self.openssl),
                 "wraps": {"provisional": vc.b64e(kek)}}
            self._write_meta(m)
            _zero(priv)
            return m

    @staticmethod
    def _dek_kdf(shared: bytes, eph_pub: bytes, fp: str) -> bytes:
        if len(shared) < 32:
            raise SealError("key agreement failed")
        return hmac.new(shared, b"hb-sealed dek v1|" + _pub_fp(eph_pub) + bytes.fromhex(fp), hashlib.sha512).digest()

    def _wrap_dek(self, m: dict, dek: bytes | bytearray) -> bytes:
        """ECIES: an ephemeral key agrees a secret with the device public key; the DEK is wrapped under it."""
        eph, eph_pub = self._genkey()
        try:
            shared = _run_openssl(self.openssl, ["pkeyutl", "-derive", "-inkey", "{e}", "-peerkey", "{p}"],
                                  fds={"e": eph, "p": m["pub"].encode()})
        finally:
            _zero(eph)
        w = vc.wrap(bytes(dek), self._dek_kdf(shared, eph_pub, m["fp"]), b"sealed-dek")
        body = json.dumps({"eph": eph_pub.decode(), "w": w}).encode()
        return DEK_MAGIC + bytes.fromhex(m["fp"]) + body

    def _unwrap_dek(self, priv: bytearray, blob: bytes, fp: str) -> bytearray:
        if not blob.startswith(DEK_MAGIC) or blob[len(DEK_MAGIC):len(DEK_MAGIC) + 8].hex() != fp:
            raise SealError("damaged data key")
        try:
            body = json.loads(blob[len(DEK_MAGIC) + 8:])
            eph_pub = body["eph"].encode()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SealError("damaged data key") from None
        if b"PUBLIC KEY" not in eph_pub or len(eph_pub) > 4000:
            raise SealError("damaged data key")
        shared = _run_openssl(self.openssl, ["pkeyutl", "-derive", "-inkey", "{k}", "-peerkey", "{p}"],
                              fds={"k": priv, "p": eph_pub})
        try:
            return bytearray(vc.unwrap(body.get("w"), self._dek_kdf(shared, eph_pub, fp), b"sealed-dek"))
        except vc.CryptoError:
            raise SealError("damaged data key") from None

    def _dek_path(self, kid: bytes, fp: str) -> Path:
        return self.keys_dir / f"{kid.hex()}.{fp}.dek"

    def _session_key(self) -> tuple[bytes, bytearray]:
        with self.lock:
            if self._session is not None:
                return self._session
            m = self._ensure_keys()
            kid, dek = secrets.token_bytes(KID_LEN), bytearray(secrets.token_bytes(32))
            self._ensure_dir()
            write_private(self._dek_path(kid, m["fp"]), self._wrap_dek(m, dek))
            self._session = (kid, dek)
            return self._session

    # ── locked / unlocked ──
    def _load_provisional(self) -> None:
        """Before setup the private key's KEK is on disk: load it whenever it is needed."""
        m = self.meta()
        if not m or self.is_setup() or self._priv is not None or m.get("hard_lock"):
            return
        kek = vc.b64d((m.get("wraps") or {}).get("provisional", ""))
        if len(kek) != 32:
            raise SealError("the sealed-records key file is damaged (sealed/device.key)")
        self._priv = self._dec_priv(m, kek)

    def _posture_level(self) -> tuple[str, object]:
        now = self.mono()
        at, stamp, level = self._posture
        if now - at < 1.0:
            return level, stamp
        try:
            st = self.posture_file.stat()
            new_stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._posture = (now, None, "guarded")
            return "guarded", None
        if new_stamp != stamp:
            # same contract as software/common/posture.py: a damaged posture file counts as lockdown
            try:
                d = json.loads(self.posture_file.read_text()) if st.st_size < 65536 else {}
                level = d.get("level") if isinstance(d, dict) and d.get("level") in (
                    "neighbourly", "guarded", "lockdown") else "lockdown"
            except (OSError, ValueError, RecursionError):
                level = "lockdown"
        self._posture = (now, new_stamp, level)
        return level, new_stamp

    def lockdown(self) -> bool:
        return self._posture_level()[0] == "lockdown"

    def _checks(self) -> None:
        """(caller holds self.lock) Idle timer, posture lockdown and a duress hard lock force a lock."""
        if self._priv is not None and self.hard_locked():
            self._lock(reason="locked")
            return
        if self._priv is None or not self.is_setup():
            return
        level, stamp = self._posture_level()
        if level == "lockdown" and self._override != stamp:
            self._lock(reason="lockdown")
        elif self.idle and self.mono() - self._last_use > self.idle:
            self._lock(reason="idle")

    def is_unlocked(self) -> bool:
        with self.lock:
            try:
                self._load_provisional()
            except SealError:
                return False
            self._checks()
            return self._priv is not None or (self.meta() is None)

    def _touch(self) -> None:
        if not getattr(self._quiet, "on", False):
            self._last_use = self.mono()

    @contextlib.contextmanager
    def background(self):
        """Background jobs read sealed data without keeping the idle timer alive."""
        old = getattr(self._quiet, "on", False)
        self._quiet.on = True
        try:
            yield
        finally:
            self._quiet.on = old

    def _lock(self, reason: str = "locked") -> None:
        _zero(self._priv)
        self._priv = None
        for d in self._deks.values():
            _zero(d)
        self._deks.clear()
        if self._session is not None:
            _zero(self._session[1])
        self._session = None
        self._cache.clear()
        self._cache_bytes = 0
        self._override = None
        self.locked_reason = reason

    def lock_all(self, reason: str = "locked") -> None:
        """Forget the private key, every data key and every decrypted copy (best effort)."""
        with self.lock:
            self._lock(reason)

    # ── seal / open ──
    def _cache_put(self, rec: bytes, kid: bytes, pt: bytes) -> None:
        h = hashlib.sha256(rec).digest()[:16]
        if h in self._cache:
            return
        self._cache[h] = (kid, pt)
        self._cache_bytes += len(pt)
        while len(self._cache) > CACHE_ITEMS or self._cache_bytes > CACHE_BYTES:
            _, (_, old) = self._cache.popitem(last=False)
            self._cache_bytes -= len(old)

    def seal(self, data: bytes) -> bytes:
        """Encrypt for the device key. Works while locked (public key only)."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("seal() takes bytes")
        kid, dek = self._session_key()
        rec = MAGIC + kid + vc.Cipher(bytes(dek), self.openssl).encrypt_bytes(bytes(data), KIND_RECORD)
        with self.lock:
            if self._session is not None and self._session[0] == kid:
                self._cache_put(rec, kid, bytes(data))
        return rec

    def _dek_for(self, kid: bytes, recent_ok: bool) -> bytearray:
        with self.lock:
            self._load_provisional()
            self._checks()
            if self._session is not None and self._session[0] == kid and (recent_ok or self._priv is not None):
                return self._session[1]
            if self._priv is None:
                if self.meta() is None:
                    raise SealError("this record's key is gone (erased)")
                raise Locked("Sealed records are locked. The owner can unlock them in Settings.")
            if kid in self._deks:
                return self._deks[kid]
            m = self.meta()
            if not m:
                raise SealError("no device key (it was erased)")
            path = self._dek_path(kid, m["fp"])
            try:
                blob = path.read_bytes()
            except OSError:
                raise SealError("this record's key is gone (erased)") from None
            dek = self._unwrap_dek(self._priv, blob, m["fp"])
            self._deks[kid] = dek
            return dek

    def open_(self, rec: bytes, recent_ok: bool = False) -> bytes:
        """Decrypt a sealed record. Raises Locked while locked (unless recent_ok and the record was sealed
        since the Badger last started or locked), SealError when damaged or its key was erased."""
        if not isinstance(rec, (bytes, bytearray)) or len(rec) < len(MAGIC) + KID_LEN + 30 or rec[:4] != MAGIC:
            raise SealError("not a sealed record")
        rec = bytes(rec)
        kid = rec[4:4 + KID_LEN]
        h = hashlib.sha256(rec).digest()[:16]
        with self.lock:
            self._load_provisional()
            self._checks()
            hit = self._cache.get(h)
            if hit is not None:
                is_recent = self._session is not None and self._session[0] == hit[0]
                if self._priv is not None or (recent_ok and is_recent):
                    self._cache.move_to_end(h)
                    if self._priv is not None:
                        self._touch()
                    return hit[1]
        dek = self._dek_for(kid, recent_ok)
        try:
            pt = vc.Cipher(bytes(dek), self.openssl).decrypt_bytes(rec[4 + KID_LEN:], KIND_RECORD)
        except vc.CryptoError as e:
            raise SealError(f"damaged sealed record ({e})") from None
        with self.lock:
            self._cache_put(rec, kid, pt)
            if self._priv is not None:
                self._touch()
        return pt

    def readable(self, rec: bytes, recent_ok: bool = True) -> bool:
        try:
            self.open_(rec, recent_ok=recent_ok)
            return True
        except (Locked, SealError):
            return False

    # text / JSON forms
    def seal_text(self, s: str) -> str:
        return TOKEN + b64u(self.seal(str(s).encode("utf-8", "surrogatepass")))

    def open_text(self, tok: str, recent_ok: bool = False) -> str:
        if not is_token(tok):
            raise SealError("not a sealed value")
        return self.open_(unb64u(tok[len(TOKEN):]), recent_ok).decode("utf-8", "replace")

    def seal_json(self, obj) -> str:
        return self.seal_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))

    def open_json(self, tok: str, recent_ok: bool = False):
        try:
            return json.loads(self.open_text(tok, recent_ok))
        except (ValueError, RecursionError):
            raise SealError("damaged sealed record") from None

    def try_json(self, tok: str, default=None, recent_ok: bool = False):
        try:
            return self.open_json(tok, recent_ok)
        except (Locked, SealError):
            return default

    # ── owner actions ──
    def _check_wait(self, ip: str) -> None:
        w = self.wait(ip)
        if w > 0:
            mins = max(1, int(w // 60 + 0.999))
            raise Refused(429, f"Too many wrong tries. Wait {mins} minute{'s' if mins != 1 else ''} and try again.",
                          wait=int(w + 0.999))

    def _gfail(self) -> dict:
        try:
            d = json.loads((self.dir / "lockout.json").read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def wait(self, ip: str = "") -> float:
        rec = self._fails.get(ip)
        local = (rec[2] - self.mono()) if rec else 0.0
        g = self._gfail()
        glob = float(g.get("until") or 0) - self.clock() if isinstance(g.get("until"), (int, float)) else 0.0
        return max(0.0, local, min(glob, G_LOCK))

    def _failed(self, ip: str) -> None:
        now = self.mono()
        with self.lock:
            rec = self._fails.setdefault(ip, [0, now, 0.0])
            if now - rec[1] > FAIL_WINDOW:
                rec[:] = [0, now, 0.0]
            rec[0] += 1
            if rec[0] >= FAILS:
                rec[2] = now + FAIL_LOCK
            if len(self._fails) > 2000:
                for k in sorted(self._fails, key=lambda k: self._fails[k][1])[:1000]:
                    self._fails.pop(k, None)
            g, wall = self._gfail(), self.clock()
            if not isinstance(g.get("first"), (int, float)) or wall - g["first"] > G_WINDOW or wall < g["first"]:
                g = {"count": 0, "first": wall, "until": 0}
            g["count"] = int(g.get("count") or 0) + 1
            if g["count"] >= G_FAILS:
                g["until"] = wall + G_LOCK
            with contextlib.suppress(OSError):
                self._ensure_dir()
                write_private(self.dir / "lockout.json", json.dumps(g).encode())

    # ── duress passphrase ──
    def _dummy_kdf(self, like: dict) -> dict:
        n = like.get("n") if isinstance(like, dict) and isinstance(like.get("n"), int) else self.scrypt_n
        if getattr(self, "_dummy", None) is None or self._dummy.get("n") != n:
            try:
                self._dummy = vc.new_kdf(n)
            except ValueError:
                self._dummy = vc.new_kdf(self.scrypt_n)
        return self._dummy

    @staticmethod
    def _duress_tag(material: bytes) -> str:
        return vc.b64e(hmac.new(material, b"hb-sealed duress v1", hashlib.sha256).digest())

    def _is_duress(self, secret: str, m: dict) -> bool:
        """One scrypt, always: against the duress passphrase, or a dummy of the passphrase's cost."""
        d = m.get("duress") if isinstance(m.get("duress"), dict) else None
        like = ((m.get("wraps") or {}).get("pass") or {}).get("kdf") or {}
        try:
            if d and isinstance(d.get("kdf"), dict) and isinstance(d.get("h"), str):
                return hmac.compare_digest(self._duress_tag(vc.passphrase_material(secret, d["kdf"])), d["h"])
            vc.passphrase_material(secret, self._dummy_kdf(like))
        except (vc.CryptoError, ValueError, TypeError):
            pass
        return False

    def has_duress(self) -> bool:
        try:
            m = self.meta()
        except SealError:
            return False
        return bool(m and isinstance(m.get("duress"), dict))

    def set_duress(self, passphrase: str, duress: str, ip: str = "") -> None:
        """The owner passphrase, then a duress passphrase (as strong as a passphrase, and a different one)."""
        bad = passphrase_problem(duress)
        if bad:
            raise Refused(400, bad)
        kek, _ = self._kek_from(passphrase, ip, allow_recovery=False)
        with self.kdf_lock:
            m = dict(self.meta())
            try:
                vc.unwrap(m["wraps"]["pass"]["w"], vc.passphrase_material(duress, m["wraps"]["pass"]["kdf"]), b"sealed-pass")
                same = True
            except (vc.CryptoError, KeyError, TypeError):
                same = False
            if same:
                raise Refused(400, "The duress passphrase must be different from the owner passphrase.")
            kdf = vc.new_kdf(int((m["wraps"]["pass"].get("kdf") or {}).get("n") or self.scrypt_n))
            m["duress"] = {"kdf": kdf, "h": self._duress_tag(vc.passphrase_material(duress, kdf))}
            with self.lock:
                self._write_meta(m)

    def clear_duress(self, passphrase: str, ip: str = "") -> None:
        self._kek_from(passphrase, ip, allow_recovery=False)
        with self.kdf_lock, self.lock:
            m = dict(self.meta())
            m.pop("duress", None)
            self._write_meta(m)

    # ── hard lock (duress "lock") ──
    def hard_locked(self) -> bool:
        try:
            m = self.meta()
        except SealError:
            return False
        return bool(m and m.get("hard_lock"))

    def hard_locked_at(self) -> float | None:
        try:
            m = self.meta()
        except SealError:
            return None
        hl = (m or {}).get("hard_lock")
        if not hl:
            return None
        at = hl.get("at") if isinstance(hl, dict) else None
        return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 1.0

    def hard_lock(self) -> bool:
        """Lock now, and from now on only the recovery key unlocks (then the passphrase works again). False when
        sealed records aren't set up (there is no recovery key to come back with)."""
        with self.kdf_lock, self.lock:
            if not self.is_setup():
                return False
            m = dict(self.meta())
            if not m.get("hard_lock"):
                m["hard_lock"] = {"at": int(self.clock())}
                self._write_meta(m)
            self._lock(reason="locked")
        return True

    def _clear_hard_lock(self) -> None:
        with self.lock:
            m = self.meta()
            if m and m.get("hard_lock"):
                m = dict(m)
                m.pop("hard_lock", None)
                self._write_meta(m)

    def _kek_from(self, secret, ip: str, allow_recovery: bool = True, hard_ok: bool = False) -> tuple[bytes, str]:
        """(KEK, "passphrase"|"recovery") or Refused (wrong secret: counted for the lockout). The duress passphrase
        raises DuressSecret (never counted as wrong). While hard-locked the passphrase raises Refused(423) unless
        hard_ok (then the caller gets it and decides)."""
        if not isinstance(secret, str) or not secret.strip() or len(secret) > 400:
            raise Refused(400, "Enter the owner passphrase (or the recovery key).")
        self._check_wait(ip)
        with self.kdf_lock:
            self._check_wait(ip)
            m = self.meta()
            if not m or not self.is_setup():
                raise Refused(409, "Sealed records aren't set up yet.")
            wraps = m["wraps"]
            rk = vc.parse_recovery(secret) if allow_recovery else None
            duress = False
            try:
                if rk is not None and isinstance(wraps.get("recovery"), dict):
                    r = wraps["recovery"]
                    kek = vc.unwrap(r["w"], vc.recovery_material(rk, vc.b64d(r["salt"])), b"sealed-recovery")
                    how = "recovery"
                else:
                    p = wraps["pass"]
                    try:
                        kek = vc.unwrap(p["w"], vc.passphrase_material(secret, p["kdf"]), b"sealed-pass")
                        how = "passphrase"
                    finally:
                        duress = self._is_duress(secret, m)     # (always: the same work for every secret)
            except (vc.CryptoError, KeyError, TypeError):
                if duress:
                    raise DuressSecret() from None
                self._failed(ip)
                raise Refused(403, "That passphrase isn't right." if rk is None else
                              "That recovery key isn't right.") from None
            if how == "passphrase" and m.get("hard_lock") and not hard_ok:
                raise Refused(423, HARD_MSG, hard_locked=True)
        with self.lock:
            self._fails.pop(ip, None)
        return kek, how

    def check_secret(self, secret, ip: str = "", allow_recovery: bool = True) -> str:
        """Which secret this is — "passphrase" | "recovery" | "duress" — without unlocking anything (the decoy's
        sealed page). Refused when wrong (counted for the lockout like any try)."""
        try:
            return self._kek_from(secret, ip, allow_recovery=allow_recovery, hard_ok=True)[1]
        except DuressSecret:
            return "duress"

    def _wraps_for(self, kek: bytes, passphrase: str | None, rk: bytes | None, old: dict | None) -> dict:
        wraps = {k: v for k, v in (old or {}).items() if k in ("pass", "recovery")}
        if passphrase is not None:
            kdf = vc.new_kdf(self.scrypt_n)
            wraps["pass"] = {"kdf": kdf, "w": vc.wrap(kek, vc.passphrase_material(passphrase, kdf), b"sealed-pass")}
        if rk is not None:
            salt = secrets.token_bytes(16)
            wraps["recovery"] = {"salt": vc.b64e(salt),
                                 "w": vc.wrap(kek, vc.recovery_material(rk, salt), b"sealed-recovery")}
        return wraps

    def setup(self, passphrase: str) -> dict:
        """First-time setup: a new keypair under the owner passphrase + a recovery key (returned ONCE),
        every data key rewrapped, all data re-sealed (migrators), the provisional key scrubbed."""
        bad = passphrase_problem(passphrase)
        if bad:
            raise Refused(400, bad)
        if not self.available():
            raise Refused(503, "Sealed records need the openssl program, which isn't working on this Badger.")
        with self.kdf_lock:
            with self.lock:
                if self.is_setup():
                    raise Refused(409, "Sealed records are already set up. Unlock them with the owner passphrase.")
                old = self._ensure_keys()
                self._load_provisional()
                old_priv, old_fp = self._priv, old["fp"]
                priv, pub = self._genkey()
                kek, rk = secrets.token_bytes(32), secrets.token_bytes(32)
                new = {"v": 1, "alg": old.get("alg"), "created": old.get("created") or int(self.clock()),
                       "setup_at": int(self.clock()), "pub": pub.decode(), "fp": _pub_fp(pub).hex(),
                       "priv": self._enc_priv(priv, kek, self.openssl),
                       "wraps": self._wraps_for(kek, passphrase, rk, None), "recovery_ack": False}
                # every data key, rewrapped under the new public key (new files; the old ones go after the commit)
                olds = sorted(self.keys_dir.glob(f"*.{old_fp}.dek")) if self.keys_dir.exists() else []
                for p in olds:
                    kid = bytes.fromhex(p.name.split(".")[0])
                    try:
                        dek = self._deks.get(kid) or self._unwrap_dek(old_priv, p.read_bytes(), old_fp)
                    except (OSError, SealError, ValueError):
                        continue                                  # damaged: nothing it sealed was readable anyway
                    self._deks[kid] = dek
                    write_private(self._dek_path(kid, new["fp"]), self._wrap_dek(new, dek))
                self._write_meta(new)                             # the commit point
                for p in olds:
                    scrub_delete(p)
                _zero(old_priv)
                self._priv = priv
                self._override = None
                self._last_use = self.mono()
                if self._session is not None:                     # this run's key: drop, a new one under the new key
                    _zero(self._session[1])
                    self._session = None
                self.locked_reason = None
        report = self.run_migrators()
        return {"recovery_key": vc.format_recovery(rk), "migrated": report}

    def unlock(self, secret: str, ip: str = "", confirm_lockdown: bool = False) -> str:
        """Owner passphrase or recovery key → the private key into memory. Returns "passphrase"|"recovery".
        Two answers unlock NOTHING: "duress" (the duress passphrase) and "hard-locked" (the right passphrase while
        a duress hard lock holds) — the launcher answers both like a success and shows the decoy."""
        level, stamp = self._posture_level()
        if level == "lockdown" and not confirm_lockdown:
            raise Refused(409, "The Badger is in lockdown, so sealed records stay locked. Unlock anyway only if "
                               "you are sure nobody unwanted can reach the Badger.", lockdown=True)
        try:
            kek, how = self._kek_from(secret, ip, hard_ok=True)
        except DuressSecret:
            return "duress"                               # nothing unlocked: the launcher shows the decoy
        if how == "passphrase" and self.hard_locked():
            return "hard-locked"                          # right passphrase, but only the recovery key opens
        m = self.meta()
        priv = self._dec_priv(m, kek)
        with self.lock:
            _zero(self._priv)
            self._priv = priv
            self._override = stamp if level == "lockdown" else None
            self._last_use = self.mono()
            self.locked_reason = None
        if how == "recovery":
            self._clear_hard_lock()
        return how

    def change_passphrase(self, old: str, new: str, ip: str = "") -> None:
        bad = passphrase_problem(new)
        if bad:
            raise Refused(400, bad)
        kek, _ = self._kek_from(old, ip)
        with self.kdf_lock, self.lock:
            m = dict(self.meta())
            if isinstance(m.get("duress"), dict) and self._is_duress(new, m):
                raise Refused(400, "Pick a passphrase different from the duress passphrase.")
            m["wraps"] = self._wraps_for(kek, new, None, m.get("wraps"))
            m["changed"] = int(self.clock())
            self._write_meta(m)

    def recover(self, recovery_key: str, new: str, ip: str = "", confirm_lockdown: bool = False) -> None:
        """Forgotten passphrase: the recovery key sets a new one (and unlocks)."""
        bad = passphrase_problem(new)
        if bad:
            raise Refused(400, bad)
        if vc.parse_recovery(recovery_key) is None:
            raise Refused(400, "That doesn't look like a recovery key (13 groups of 4 letters and digits).")
        kek, how = self._kek_from(recovery_key, ip)
        if how != "recovery":
            raise Refused(403, "That recovery key isn't right.")
        with self.kdf_lock, self.lock:
            m = dict(self.meta())
            m["wraps"] = self._wraps_for(kek, new, None, m.get("wraps"))
            m["changed"] = int(self.clock())
            m.pop("hard_lock", None)                      # the recovery key ends a duress hard lock
            self._write_meta(m)
        self.unlock(new, ip, confirm_lockdown=confirm_lockdown)

    def new_recovery_key(self, passphrase: str, ip: str = "") -> str:
        kek, _ = self._kek_from(passphrase, ip, allow_recovery=False)
        rk = secrets.token_bytes(32)
        with self.kdf_lock, self.lock:
            m = dict(self.meta())
            m["wraps"] = self._wraps_for(kek, None, rk, m.get("wraps"))
            m["recovery_ack"] = False
            self._write_meta(m)
        return vc.format_recovery(rk)

    def ack_recovery(self) -> None:
        with self.lock:
            m = self.meta()
            if m and self.is_setup() and not m.get("recovery_ack"):
                m = dict(m)
                m["recovery_ack"] = True
                self._write_meta(m)

    def destroy_keys(self) -> list[str]:
        """CRYPTO-ERASE: zeroise memory, then zero-fill and delete device.key, every keys/*.dek and the public
        copies. Everything sealed so far becomes unreadable for good. The Badger keeps running: the next
        seal makes a fresh provisional keypair (the owner sets a passphrase again)."""
        gone = []
        with self.kdf_lock, self.lock:
            self._lock(reason="erased")
            for p in [self.meta_path, *sorted(self.keys_dir.glob("*.dek")),
                      self.dir / "lockout.json"]:
                if scrub_delete(p):
                    gone.append(str(p))
            sweep_scrubs(self.dir)
            self._meta_cache = None
            self.locked_reason = "erased"
        return gone

    # ── migration (setup and later) ──
    def register_migrator(self, name: str, fn) -> None:
        """fn() re-reads one store and writes it back sealed (plaintext → sealed, old keys → this run's key)."""
        self.migrators[str(name)] = fn

    def run_migrators(self) -> dict:
        out = {}
        for name, fn in list(self.migrators.items()):
            try:
                fn()
                out[name] = "ok"
            except Exception as e:  # noqa: BLE001 — one store must not stop the others
                out[name] = f"{type(e).__name__}: {str(e)[:120]}"
        return out

    def status(self, ip: str = "") -> dict:
        try:
            m = self.meta()
        except SealError as e:
            return {"available": self.available(), "setup": False, "unlocked": False, "error": str(e)}
        setup = self.is_setup()
        unlocked = self.is_unlocked()
        level, _ = self._posture_level()
        idle_left = None
        if setup and unlocked and self.idle:
            idle_left = max(0, int(self.idle - (self.mono() - self._last_use)))
        return {"available": self.available(), "setup": setup, "unlocked": bool(unlocked),
                "protected": setup, "provisional": bool(m) and not setup,
                "recovery_ack": bool(m and m.get("recovery_ack")) if setup else None,
                "idle_minutes": int(self.idle // 60), "idle_left": idle_left,
                "posture": level, "lockdown": level == "lockdown",
                "lockdown_override": bool(unlocked and setup and level == "lockdown"),
                "locked_reason": None if unlocked else self.locked_reason,
                "wait": int(self.wait(ip) + 0.999), "fingerprint": (m or {}).get("fp"),
                "setup_at": (m or {}).get("setup_at")}


class DecoySealer(Sealer):
    """The sealer of a duress session (docs/SECURITY.md → "Duress"): a provisional key in the decoy's own folder,
    so the made-up records can be sealed, locked and unlocked like real ones — while status() reports what the
    REAL sealer would (set up, its fingerprint, its setup date, its wrong-try wait). `held` = locked: nothing
    opens until the launcher says the passphrase (or the duress passphrase) was typed."""

    def __init__(self, folder: Path, real: Sealer | None = None, held: bool = False, **opts):
        super().__init__(folder, **opts)
        self.real = real
        self.held = bool(held)
        self.locked_reason = None
        self._bypass = threading.local()

    def _held(self) -> bool:
        return self.held and not getattr(self._bypass, "on", False)

    @contextlib.contextmanager
    def unheld(self):
        """For the launcher's own setup work (sealing the made-up records) while the decoy shows as locked."""
        old = getattr(self._bypass, "on", False)
        self._bypass.on = True
        try:
            yield
        finally:
            self._bypass.on = old
            if self.held and not old:
                with self.lock:
                    reason = self.locked_reason
                    self._lock(reason or "locked")
                    self.locked_reason = reason

    def _load_provisional(self) -> None:
        if not self._held():
            super()._load_provisional()

    def is_unlocked(self) -> bool:
        if self._held():
            return False
        return super().is_unlocked()

    def hard_locked(self) -> bool:
        return False

    def lock_all(self, reason: str = "locked") -> None:
        with self.lock:
            self._lock(reason)
            self.held = True

    def open_up(self) -> None:
        """The (duress) passphrase was typed in the decoy: show the made-up records."""
        with self.lock:
            self.held = False
            self.locked_reason = None
            self._last_use = self.mono()

    def status(self, ip: str = "") -> dict:
        st = super().status(ip)
        try:
            rm = (self.real.meta() if self.real is not None else None) or {}
        except SealError:
            rm = {}
        unlocked = self.is_unlocked()
        idle = int(self.real.idle // 60) if self.real is not None else 0
        st.update(available=True, setup=True, protected=True, provisional=False, unlocked=unlocked,
                  recovery_ack=bool(rm.get("recovery_ack", True)), idle_minutes=idle, idle_left=None,
                  lockdown_override=False, locked_reason=None if unlocked else self.locked_reason,
                  wait=int((self.real.wait(ip) if self.real is not None else 0) + 0.999),
                  fingerprint=rm.get("fp") or st.get("fingerprint"), setup_at=rm.get("setup_at"))
        if idle and unlocked:
            st["idle_left"] = idle * 60
        return st


# ── sealed fields inside a JSON document ──

def _merge(base: dict, part: dict) -> dict:
    if part.get("__del__") is True:
        return {}
    out = dict(base)
    for k, v in part.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class SealedMap:
    """Per-key sensitive fields of a JSON document `d`, kept as ONE sealed token d[key] ({key: {field: …}})
    plus, for changes made while locked, per-key lists of sealed partial updates d[key + "_pending"]
    (write-only: appending needs no reading). Opened with one decryption, cached; folded back into the one
    token whenever the Badger is unlocked. With sealer=None everything stays plain (d[key] is a dict)."""

    def __init__(self, d: dict, sealer: Sealer | None, key: str = "sealed"):
        self.d, self.s, self.key, self.pkey = d, sealer, key, key + "_pending"

    def _pending(self) -> dict:
        p = self.d.get(self.pkey)
        if not isinstance(p, dict):
            p = {}
        return {k: [t for t in v if is_token(t)] for k, v in p.items() if isinstance(v, list)}

    def _base(self) -> tuple[dict, bool]:
        """(the folded map, readable?)"""
        raw = self.d.get(self.key)
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if isinstance(v, dict)}, True
        if not is_token(raw):
            return {}, True
        if self.s is None:
            return {}, False
        try:
            v = self.s.open_json(raw)
        except (Locked, SealError):
            return {}, False
        return ({k: x for k, x in v.items() if isinstance(x, dict)} if isinstance(v, dict) else {}), True

    def read(self) -> tuple[dict, bool]:
        """({key: fields}, complete). Locked: only what was written since the Badger locked (complete=False)."""
        base, ok = self._base()
        out = {k: dict(v) for k, v in base.items()}
        complete = ok
        for k, toks in self._pending().items():
            for t in toks:
                part = self.s.try_json(t, None, recent_ok=True) if self.s is not None else None
                if not isinstance(part, dict):
                    complete = False
                    continue
                out[k] = _merge(out.get(k, {}), part)
            if not out.get(k):
                out.pop(k, None)
        return out, complete

    def get(self, k: str) -> dict | None:
        return self.read()[0].get(k)

    def update(self, k: str, part: dict) -> None:
        """Set some fields of key k (None removes a field; {"__del__": True} clears k)."""
        if self.s is None:
            base = self.d.get(self.key) if isinstance(self.d.get(self.key), dict) else {}
            base[k] = _merge(base.get(k, {}), part)
            if not base[k]:
                base.pop(k)
            self.d[self.key] = base
            return
        if self.s.is_unlocked() and self._base()[1]:
            vals, complete = self.read()
            if complete:
                vals[k] = _merge(vals.get(k, {}), part)
                if not vals[k]:
                    vals.pop(k)
                self._store(vals)
                return
        p = self._pending()
        p.setdefault(k, []).append(self.s.seal_json(part))
        self.d[self.pkey] = p

    def update_many(self, parts: dict, live=None) -> None:
        """Several keys at once: one sealing when unlocked (keys not in `live` are dropped then)."""
        if not parts and live is None:
            return
        if self.s is not None and self.s.is_unlocked() and self._base()[1]:
            vals, complete = self.read()
            if complete:
                for k, part in parts.items():
                    vals[k] = _merge(vals.get(k, {}), part)
                    if not vals[k]:
                        vals.pop(k)
                if live is not None:
                    vals = {k: v for k, v in vals.items() if k in live}
                self._store(vals)
                if isinstance(self.d.get(self.key), dict):
                    self.d[self.key] = None
                return
        for k, part in parts.items():
            self.update(k, part)
        if live is not None:
            self.fold(live)

    def remove(self, k: str) -> None:
        """Forget key k: its pending updates go at once; its part of the folded token now when unlocked
        (else at the next fold that is given the live keys)."""
        p = self._pending()
        if k in p:
            p.pop(k)
            self.d[self.pkey] = p
        raw = self.d.get(self.key)
        if isinstance(raw, dict):
            raw.pop(k, None)
            if self.s is None or not self.s.is_unlocked():
                return
        if self.s is not None and self.s.is_unlocked():
            vals, complete = self.read()
            if complete:
                vals.pop(k, None)
                self._store(vals)

    def _store(self, vals: dict) -> None:
        self.d[self.key] = self.s.seal_json(vals) if vals else None
        self.d.pop(self.pkey, None)

    def fold(self, live=None) -> bool:
        """Unlocked: merge pending updates into the one token, dropping keys not in `live`. Locked with a plain
        (legacy) map: seal it as pending updates, so plaintext never stays. True when d changed."""
        if self.s is None:
            if live is None or not isinstance(self.d.get(self.key), dict):
                return False
            base = self.d[self.key]
            dead = [k for k in base if k not in live]
            for k in dead:
                base.pop(k)
            return bool(dead)
        raw = self.d.get(self.key)
        if not self.s.is_unlocked():
            if isinstance(raw, dict):
                p = self._pending()
                for k, v in raw.items():
                    if isinstance(v, dict) and (live is None or k in live):
                        p[k] = [self.s.seal_json(v)] + p.get(k, [])
                self.d[self.pkey] = p
                self.d[self.key] = None
                return True
            return False
        vals, complete = self.read()
        if not complete:
            return False                                       # an unreadable part (erased key): leave it
        if live is not None:
            vals = {k: v for k, v in vals.items() if k in live}
        if not self._pending() and not isinstance(raw, dict) and (live is None or set(vals) == set(self._base()[0])):
            return False
        self._store(vals)
        return True

    def reseal(self, live=None) -> None:
        """Migration: always write a fresh token (this run's key)."""
        if self.s is None or not self.s.is_unlocked():
            self.fold(live)
            return
        vals, complete = self.read()
        if not complete:
            return
        if live is not None:
            vals = {k: v for k, v in vals.items() if k in live}
        self._store(vals)


class SealedJournal:
    """Operations written while locked: one sealed JSON object per line (append-only, 0600). Applied in order
    by the owner of the store once the Badger is unlocked (drain), then the file is scrubbed."""

    def __init__(self, path: Path, sealer: Sealer):
        self.path, self.s = Path(path), sealer
        self.lock = threading.RLock()

    def append(self, op: dict) -> None:
        line = self.s.seal_json(op) + "\n"
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

    def _lines(self) -> list[str]:
        try:
            return [ln.strip() for ln in self.path.read_text(encoding="utf-8").splitlines() if is_token(ln.strip())]
        except OSError:
            return []

    def pending(self) -> int:
        return len(self._lines())

    def recent(self) -> list[dict]:
        """The operations readable now (all when unlocked; while locked, those since it locked)."""
        out = []
        for ln in self._lines():
            op = self.s.try_json(ln, None, recent_ok=True)
            if isinstance(op, dict):
                out.append(op)
        return out

    def take(self) -> list[dict]:
        """Unlocked: every readable op, in order (unreadable lines — an erased key — are skipped). The caller
        applies them, saves its store, then calls clear() — all under its own lock, which append() callers
        hold too, so nothing is lost in between."""
        if not self.s.is_unlocked():
            return []
        out = []
        for ln in self._lines():
            op = self.s.try_json(ln, None)
            if isinstance(op, dict):
                out.append(op)
        return out

    def clear(self) -> None:
        with self.lock:
            scrub_delete(self.path)


# ── the portal's instance ──
_instances: dict[str, Sealer] = {}
_default: list = [None]
_inst_lock = threading.Lock()


def use(folder, make_default: bool = True, **opts) -> Sealer:
    """The Sealer for this state folder (made once per folder), also made the default for the module-level
    functions below (unless make_default=False). The portal calls this with its config; other code just calls
    the functions."""
    key = str(Path(folder).resolve())
    with _inst_lock:
        s = _instances.get(key)
        if s is None:
            s = _instances[key] = Sealer(Path(folder), **opts)
        else:
            for k in ("openssl", "scrypt_n"):
                if k in opts:
                    setattr(s, k, opts[k])
            if "idle_minutes" in opts:
                s.idle = max(0.0, float(opts["idle_minutes"] or 0)) * 60
            if opts.get("posture_file"):
                s.posture_file = Path(opts["posture_file"])
        if make_default:
            _default[0] = s
        return s


def set_default(s: Sealer) -> None:
    """Make `s` the default for the module-level functions (the decoy's sealer during a duress, and back)."""
    with _inst_lock:
        _default[0] = s


def current() -> Sealer:
    s = _default[0]
    if s is None:
        raise SealError("sealed records aren't configured in this process")
    return s


def is_unlocked() -> bool:
    return current().is_unlocked()


def is_setup() -> bool:
    """Has the owner set a passphrase (records protected, locked after a restart)?"""
    return current().is_setup()


def status() -> dict:
    return current().status()


def seal(data: bytes) -> bytes:
    return current().seal(data)


def open_(data: bytes, recent_ok: bool = False) -> bytes:
    return current().open_(data, recent_ok)


def lock_all() -> None:
    """Lock every instance in this process (cheap; safe to call any time)."""
    for s in list(_instances.values()):
        s.lock_all()


DEFAULT_DIR = "/var/lib/prepper/portal/sealed"
KEY_PATHS = (DEFAULT_DIR,)                    # where destroy_keys() erases when this process has no instance


def destroy_keys(root: str | None = None) -> list[str]:
    """Crypto-erase for the panic wipe: zeroise and scrub-delete every instance's keys (normally one, in the
    portal). In another process (no instance) — or with `root` (a test root, or "/") — the key files in
    <root>/var/lib/prepper/portal/sealed are scrubbed instead. Returns the deleted paths."""
    gone = []
    for s in list(_instances.values()):
        if root is None:
            gone += s.destroy_keys()
        else:
            s.lock_all("erased")
    if root is not None or not _instances:
        base = Path(root or "/")
        gone += destroy_keys_in(base / DEFAULT_DIR.lstrip("/"))
    return gone


def seal_text(s: str) -> str:
    return current().seal_text(s)


def open_text(tok: str, recent_ok: bool = False) -> str:
    return current().open_text(tok, recent_ok)


def seal_json(obj) -> str:
    return current().seal_json(obj)


def open_json(tok: str, recent_ok: bool = False):
    return current().open_json(tok, recent_ok)


def seal_jsonl_line(obj: dict, sealer: Sealer | None = None) -> str:
    """One JSON-lines record as written to a sealed board file: {"s": token} (no newline)."""
    return json.dumps({"s": (sealer or current()).seal_json(obj)})


def hard_lock_in(folder) -> bool:
    """DURESS "lock" from another process (the root wipe engine, for the front panel's duress code): set the
    hard-lock flag in <folder>/device.key. The file keeps its owner (the launcher's user) and 0600. A running
    launcher notices on its next request (it checks the flag) and forgets its keys. False when sealed records
    aren't set up (nothing a recovery key could reopen) or the file can't be read."""
    path = Path(folder) / "device.key"
    try:
        st = path.stat()
        d = json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return False
    if not isinstance(d, dict) or not ((d.get("wraps") or {}).get("pass") if isinstance(d.get("wraps"), dict) else None):
        return False
    if d.get("hard_lock"):
        return True
    d["hard_lock"] = {"at": int(time.time())}
    replace_scrubbing(path, json.dumps(d, indent=1).encode())
    if os.geteuid() == 0:
        with contextlib.suppress(OSError):
            os.chown(path, st.st_uid, st.st_gid)
    return True


def destroy_keys_in(folder) -> list[str]:
    """Crypto-erase from another process (e.g. a root helper) that has no Sealer: scrub-delete the key files
    in a state folder (default /var/lib/prepper/portal/sealed). A running portal still holds keys in memory
    until it is stopped or calls lock_all()."""
    folder = Path(folder)
    gone = []
    for p in [folder / "device.key", *sorted((folder / "keys").glob("*.dek")),
              folder / "lockout.json"]:
        if scrub_delete(p):
            gone.append(str(p))
    sweep_scrubs(folder)
    return gone


if __name__ == "__main__":                              # pragma: no cover — a manual check
    import argparse
    ap = argparse.ArgumentParser(description="Sealed records: status, or crypto-erase the keys in a folder.")
    ap.add_argument("cmd", choices=("status", "destroy-keys"))
    ap.add_argument("folder", nargs="?", default="/var/lib/prepper/portal/sealed")
    a = ap.parse_args()
    if a.cmd == "status":
        print(json.dumps(Sealer(Path(a.folder)).status(), indent=1))
    else:
        print("\n".join(destroy_keys_in(a.folder)) or "nothing to erase")
