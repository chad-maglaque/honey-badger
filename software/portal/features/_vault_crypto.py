"""
Crypto for the family document vault (not a feature: files starting with "_" are not loaded).

Design (see docs/features/vault.md for the threat model):

  * Vault key VK: 32 random bytes, made once at setup. Never stored in the clear.
  * Passphrase wrap: m = scrypt(passphrase, salt, n, r=8, p=1, 64 bytes); VK is stored as
    VK XOR m[:32] with an HMAC-SHA256 tag keyed by m[32:]. A new random salt is drawn on every
    (re)wrap, so the pad is never reused. Changing the passphrase rewraps VK only.
  * Recovery wrap: the same, with m = HMAC-SHA512(recovery_key, salt). The recovery key is
    256 random bits, printed once at setup (base32, groups of 4).
  * Content: every blob (a file, a note, the index of names) is split into 1 MiB chunks.
    Chunk i of blob B is AES-256-CTR encrypted by the openssl command-line tool, with
    key||iv = PBKDF2-HMAC-SHA256(password_i, "", 1 iteration, 48 bytes) where
    password_i = hex(HMAC-SHA256(K_enc, B.id || i)). password_i is unique per chunk (B.id is
    128 random bits), so no key/IV pair is ever reused; it is a full-entropy key, so one
    PBKDF2 iteration is all that's needed (it is only openssl's way to take a key from a file
    descriptor instead of the command line, where `ps` would show it).
    Each chunk is then authenticated (encrypt-then-MAC) with HMAC-SHA256 under K_mac over
    the blob header, the chunk index, a "last chunk" flag and the ciphertext, so tampering,
    reordering, truncation, or swapping chunks/blobs is detected before anything is decrypted.
    K_enc and K_mac are HMAC-SHA256(VK, label).
  * Plaintext only ever lives in this process's memory and in the pipe to/from openssl.

Blob format:  "HBVAULT1" | kind (1 byte) | blob id (16)  then chunks:
              flags (1: bit0 = last) | length (4, big-endian) | ciphertext | tag (32)

Also a small offline tool for a backup bundle made by the vault's Export button:

    python3 _vault_crypto.py list    honey-badger-vault.tar
    python3 _vault_crypto.py extract honey-badger-vault.tar OUTDIR    (writes decrypted files!)
    python3 _vault_crypto.py restore honey-badger-vault.tar DATADIR   (copies the encrypted files back)

Standard library + the openssl program (OpenSSL 3 on the Badger; LibreSSL also works).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import unicodedata

MAGIC = b"HBVAULT1"
KIND_INDEX, KIND_FILE, KIND_NOTE = 0, 1, 2
CHUNK = 1 << 20
HEADER_LEN = len(MAGIC) + 1 + 16
TAG_LEN = 32
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 16, 8, 1        # ~64 MiB, ~0.5 s on a Pi 5
B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


class CryptoError(Exception):
    """Wrong key, or the data was changed or damaged."""


class OpenSSLError(CryptoError):
    """The openssl program is missing or failed."""


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64d(s) -> bytes:
    try:
        return base64.b64decode(str(s), validate=True)
    except (ValueError, TypeError):
        raise CryptoError("damaged vault settings") from None


# ── key wrapping ──

def norm_passphrase(pw: str) -> bytes:
    """Same passphrase typed on an iPhone or a laptop gives the same bytes."""
    return unicodedata.normalize("NFKC", str(pw)).strip().encode("utf-8")


def new_kdf(n: int = SCRYPT_N) -> dict:
    n = int(n)
    if n < 1024 or n > (1 << 20) or n & (n - 1):
        raise ValueError("scrypt n must be a power of two between 1024 and 1048576")
    return {"alg": "scrypt", "n": n, "r": SCRYPT_R, "p": SCRYPT_P, "salt": b64e(secrets.token_bytes(16))}


def passphrase_material(pw: str, kdf: dict) -> bytes:
    if not isinstance(kdf, dict) or kdf.get("alg") != "scrypt":
        raise CryptoError("unknown key derivation")
    try:
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
    except (KeyError, TypeError, ValueError):
        raise CryptoError("damaged vault settings") from None
    if not (1024 <= n <= (1 << 20) and 1 <= r <= 16 and 1 <= p <= 4):
        raise CryptoError("unsupported key derivation settings")
    return hashlib.scrypt(norm_passphrase(pw), salt=b64d(kdf["salt"]), n=n, r=r, p=p,
                          maxmem=256 * n * r + (16 << 20), dklen=64)


def recovery_material(rk: bytes, salt: bytes) -> bytes:
    return hmac.new(rk, b"hb-vault recovery v1|" + salt, hashlib.sha512).digest()


def wrap(vk: bytes, material: bytes, label: bytes = b"pass") -> dict:
    if len(vk) != 32 or len(material) != 64:
        raise CryptoError("bad key length")
    ct = bytes(a ^ b for a, b in zip(vk, material[:32], strict=True))
    tag = hmac.new(material[32:], b"hb-vault wrap v1|" + label + b"|" + ct, hashlib.sha256).digest()
    return {"ct": b64e(ct), "tag": b64e(tag)}


def unwrap(w: dict, material: bytes, label: bytes = b"pass") -> bytes:
    if not isinstance(w, dict):
        raise CryptoError("damaged vault settings")
    ct, tag = b64d(w.get("ct", "")), b64d(w.get("tag", ""))
    want = hmac.new(material[32:], b"hb-vault wrap v1|" + label + b"|" + ct, hashlib.sha256).digest()
    if len(ct) != 32 or not hmac.compare_digest(tag, want):
        raise CryptoError("wrong passphrase")
    return bytes(a ^ b for a, b in zip(ct, material[:32], strict=True))


def format_recovery(rk: bytes) -> str:
    s = base64.b32encode(rk).decode().rstrip("=")
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def parse_recovery(s: str) -> bytes | None:
    t = re.sub(r"[\s\-_.]", "", str(s or "")).upper()[:80].replace("0", "O").replace("1", "I").replace("8", "B")
    if len(t) != 52 or any(c not in B32 for c in t):
        return None
    try:
        raw = base64.b32decode(t + "====")
    except ValueError:
        return None
    return raw if len(raw) == 32 else None


# ── content encryption ──

class Cipher:
    def __init__(self, vk: bytes, openssl: str = "/usr/bin/openssl"):
        if len(vk) != 32:
            raise CryptoError("bad vault key")
        self.k_enc = hmac.new(bytes(vk), b"hb-vault enc v1", hashlib.sha256).digest()
        self.k_mac = hmac.new(bytes(vk), b"hb-vault mac v1", hashlib.sha256).digest()
        self.openssl = openssl

    def fingerprint(self) -> str:
        return hmac.new(self.k_mac, b"fingerprint", hashlib.sha256).hexdigest()[:16]

    # AES-256-CTR through the openssl program; the key goes through a pipe, never argv.
    def _ctr(self, blob_id: bytes, idx: int, data: bytes, decrypt: bool) -> bytes:
        pw = hmac.new(self.k_enc, blob_id + struct.pack(">Q", idx), hashlib.sha256).hexdigest()
        r, w = os.pipe()
        try:
            os.write(w, (pw + "\n").encode())
            os.close(w)
            w = -1
            args = [self.openssl, "enc"] + (["-d"] if decrypt else []) + [
                "-aes-256-ctr", "-pbkdf2", "-iter", "1", "-md", "sha256", "-nosalt", "-pass", f"fd:{r}"]
            try:
                p = subprocess.run(args, input=data, capture_output=True, pass_fds=(r,), timeout=60,
                                   env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            except (OSError, subprocess.SubprocessError) as e:
                raise OpenSSLError(f"openssl failed: {type(e).__name__}") from None
        finally:
            os.close(r)
            if w >= 0:
                os.close(w)
        if p.returncode != 0 or len(p.stdout) != len(data):
            raise OpenSSLError("openssl failed: " + p.stderr.decode(errors="replace")[:200].strip())
        return p.stdout

    def _tag(self, header: bytes, idx: int, flags: int, ct: bytes) -> bytes:
        m = hmac.new(self.k_mac, header, hashlib.sha256)
        m.update(struct.pack(">QBI", idx, flags, len(ct)))
        m.update(ct)
        return m.digest()

    def encrypt_stream(self, read, out, kind: int, blob_id: bytes | None = None) -> tuple[bytes, int]:
        """read(n) → bytes ('' at end); writes the blob to `out`. Returns (blob_id, plaintext size).
        Memory stays at about two chunks whatever the size."""
        blob_id = blob_id or secrets.token_bytes(16)
        header = MAGIC + bytes([kind]) + blob_id
        out.write(header)
        cur, idx, total = read(CHUNK), 0, 0
        while True:
            nxt = read(CHUNK) if len(cur) == CHUNK else b""
            last = not nxt
            ct = self._ctr(blob_id, idx, cur, decrypt=False)
            flags = 1 if last else 0
            out.write(struct.pack(">BI", flags, len(ct)) + ct + self._tag(header, idx, flags, ct))
            total += len(cur)
            if last:
                return blob_id, total
            cur, idx = nxt, idx + 1

    def encrypt_bytes(self, data: bytes, kind: int) -> bytes:
        buf, src = io.BytesIO(), io.BytesIO(data)
        self.encrypt_stream(src.read, buf, kind)
        return buf.getvalue()

    def _chunks(self, fh, kind: int, blob_id: bytes | None):
        header = fh.read(HEADER_LEN)
        if len(header) != HEADER_LEN or header[:len(MAGIC)] != MAGIC or header[len(MAGIC)] != kind:
            raise CryptoError("not a vault file, or damaged")
        if blob_id is not None and not hmac.compare_digest(header[len(MAGIC) + 1:], blob_id):
            raise CryptoError("vault file doesn't match its index entry")
        bid, idx = header[len(MAGIC) + 1:], 0
        while True:
            h = fh.read(5)
            if len(h) != 5:
                raise CryptoError("vault file is cut short or damaged")
            flags, n = struct.unpack(">BI", h)
            if flags not in (0, 1) or n > CHUNK or (n < CHUNK and flags == 0):
                raise CryptoError("vault file is damaged")
            ct, tag = fh.read(n), fh.read(TAG_LEN)
            if len(ct) != n or len(tag) != TAG_LEN:
                raise CryptoError("vault file is cut short or damaged")
            if not hmac.compare_digest(tag, self._tag(header, idx, flags, ct)):
                raise CryptoError("wrong key, or the file was changed or damaged")
            yield bid, idx, flags, ct
            if flags & 1:
                if fh.read(1):
                    raise CryptoError("vault file has extra data")
                return
            idx += 1

    def verify(self, fh, kind: int, blob_id: bytes | None = None) -> int:
        """Check every tag without decrypting (fast). Returns the plaintext size."""
        return sum(len(ct) for _, _, _, ct in self._chunks(fh, kind, blob_id))

    def decrypt_iter(self, fh, kind: int, blob_id: bytes | None = None):
        """Plaintext chunk by chunk; each chunk is authenticated before it is decrypted."""
        for bid, idx, _flags, ct in self._chunks(fh, kind, blob_id):
            yield self._ctr(bid, idx, ct, decrypt=True)

    def decrypt_bytes(self, data: bytes, kind: int, blob_id: bytes | None = None) -> bytes:
        return b"".join(self.decrypt_iter(io.BytesIO(data), kind, blob_id))


def openssl_ok(openssl: str) -> bool:
    try:
        c = Cipher(b"\0" * 32, openssl)
        return c.decrypt_bytes(c.encrypt_bytes(b"ok", KIND_NOTE), KIND_NOTE) == b"ok"
    except (CryptoError, OSError):
        return False


# ── duress hard lock (docs/SECURITY.md → "Duress") ──
# A flag in vault.json ("hard_lock": {"at"}), no secret in it: while it is there the family passphrase no longer
# opens the vault — only the printed recovery key does, after which the passphrase works again.

def is_hard_locked(meta) -> bool:
    return isinstance(meta, dict) and bool(meta.get("hard_lock"))


def hard_lock_file(path, now: float | None = None) -> bool:
    """Set the flag in a vault.json from another process (the root wipe engine). Keeps the file's owner and
    mode. False when there is no (readable) vault."""
    import tempfile
    import time
    try:
        st = os.stat(path)
        with open(path, "rb") as fh:
            d = json.loads(fh.read(1 << 20))
    except (OSError, ValueError, RecursionError):
        return False
    if not isinstance(d, dict) or not isinstance(d.get("wrap"), dict):
        return False
    if d.get("hard_lock"):
        return True
    d["hard_lock"] = {"at": int(now if now is not None else time.time())}
    fd, tmp = tempfile.mkstemp(prefix=".vault.json.", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(d, indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, st.st_mode & 0o777)
        if os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


# ── offline tool for an exported bundle ──

def _bundle_open(tar_path: str, pw: str, openssl: str):
    import tarfile
    tf = tarfile.open(tar_path, "r:")
    try:
        meta = json.loads(tf.extractfile("vault.json").read())
        vk = unwrap(meta["wrap"], passphrase_material(pw, meta["kdf"]))
        c = Cipher(vk, openssl)
        index = json.loads(c.decrypt_bytes(tf.extractfile("index.bin").read(), KIND_INDEX))
    except BaseException:
        tf.close()
        raise
    return tf, c, index


def _main(argv) -> int:
    import getpass
    import tarfile
    openssl = os.environ.get("OPENSSL", shutil.which("openssl") or "/usr/bin/openssl")
    if len(argv) < 2 or argv[0] not in ("list", "extract", "restore"):
        print(__doc__.split("Also a small offline tool")[1])
        return 2
    cmd, bundle = argv[0], argv[1]
    try:
        tf, c, index = _bundle_open(bundle, getpass.getpass("Vault passphrase: "), openssl)
    except (CryptoError, KeyError, ValueError, OSError, tarfile.TarError) as e:
        print(f"Can't open the vault: {e}")
        return 1
    try:
        return _run(cmd, argv, tf, c, index)
    finally:
        tf.close()


def _run(cmd, argv, tf, c, index) -> int:
    files = index.get("files", {})
    if cmd == "list":
        for e in sorted(files.values(), key=lambda e: (e.get("cat", ""), e.get("name", ""))):
            print(f"{e.get('cat', ''):10} {e.get('size', 0):>10}  {e.get('name', '')}")
        return 0
    if len(argv) < 3:
        print("give a folder to write to")
        return 2
    dest = os.path.abspath(argv[2])
    os.makedirs(dest, exist_ok=True)
    if cmd == "restore":
        for name in ["vault.json", "index.bin"] + [f"blobs/{e['blob']}.bin" for e in files.values() if e.get("blob")]:
            if not re.fullmatch(r"(vault\.json|index\.bin|blobs/[0-9a-f]{32}\.bin)", name):
                continue
            os.makedirs(os.path.join(dest, "blobs"), exist_ok=True)
            with tf.extractfile(name) as src, open(os.path.join(dest, name), "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(os.path.join(dest, name), 0o600)
        print(f"Restored {len(files)} items to {dest} (still encrypted).")
        return 0
    for e in files.values():
        if not e.get("blob"):
            continue
        kind = KIND_NOTE if e.get("kind") == "note" else KIND_FILE
        safe = re.sub(r"[^\w.\- ]", "_", e.get("name", "file"))[:100] or "file"
        if kind == KIND_NOTE:
            safe += ".txt"
        sub = os.path.join(dest, re.sub(r"[^\w-]", "_", e.get("cat", "other")))
        os.makedirs(sub, exist_ok=True)
        path, n = os.path.join(sub, safe), 1
        while os.path.exists(path):
            path, n = os.path.join(sub, f"{n}-{safe}"), n + 1
        try:
            src = tf.extractfile(f"blobs/{e['blob']}.bin")
        except KeyError:
            print(f"missing from the bundle, skipped: {e.get('name')}")
            continue
        try:
            with src, open(path, "wb") as out:
                for chunk in c.decrypt_iter(src, kind, bytes.fromhex(e["blob"])):
                    out.write(chunk)
        except CryptoError as err:
            os.unlink(path)
            print(f"damaged, skipped: {e.get('name')} ({err})")
            continue
        print(path)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
