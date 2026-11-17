"""
Family document vault: encrypted copies of IDs, passports, insurance, deeds, prescriptions,
medical notes, family photos and contact lists.

  * Everything on disk is encrypted: file contents, notes, and the index of names, categories
    and notes. A stolen Badger or a pulled NVMe shows only random-looking blobs (how many, and
    roughly how big). Crypto lives in _vault_crypto.py (read its docstring).
  * A family passphrase unlocks it. The unwrapped key is held only in this process's memory,
    per member device, and forgotten after `idle_minutes` without use, on "Lock now", or on
    a restart. Wrong passphrases are rate limited per network address, per device and overall.
  * Family members only. The admin PIN alone gives no access: an admin who isn't a member gets
    401 like anyone else, and even a member needs the passphrase. The printed recovery key is
    the only way back if the passphrase is forgotten.
  * Uploads go up as raw bodies (≤ 25 MiB) and are encrypted chunk by chunk as they arrive;
    downloads are checked, then decrypted on the fly. Plaintext never touches the disk.
  * Search words, names and notes travel in POST bodies, never in URLs (nginx logs URLs).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import secrets
import sys
import tarfile
import threading
import time
import urllib.parse
from pathlib import Path


def _load_crypto():
    """_vault_crypto.py sits next to this file; load it by path (features aren't a package)."""
    name = "badger_vault_crypto"
    path = Path(__file__).resolve().with_name("_vault_crypto.py")
    mod = sys.modules.get(name)
    if mod is not None and getattr(mod, "__file__", None) == str(path):
        return mod
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


vc = _load_crypto()

FEATURE = {"title": "Document vault", "icon": "🔐", "group": "family", "route": "/vault",
           "script": "/features/vault.js", "access": "member",
           "description": "Locked, encrypted copies of IDs, insurance, medical papers and family photos"}

MIB = 1024 * 1024
ROUTE_MAX = 25 * MIB                     # nginx allows 26 MiB on /api/
CATEGORIES = [("id", "IDs & passports"), ("insurance", "Insurance"), ("property", "Deeds & property"),
              ("medical", "Medical & prescriptions"), ("photos", "Family photos"), ("contacts", "Contacts"),
              ("money", "Bank & money"), ("other", "Other")]
CAT_IDS = {c for c, _ in CATEGORIES}
FAILS, FAIL_WINDOW, FAIL_LOCK = 5, 600.0, 600.0                  # per address / per device
G_FAILS, G_WINDOW, G_LOCK = 20, 3600.0, 900.0                     # whole vault
MAX_NAME, MAX_NOTE, MAX_TEXT = 120, 1000, 20000
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
INLINE_TYPES = ("image/jpeg", "image/png")


class Refused(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def _num(v, default, lo, hi):
    try:
        x = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def clean_text(s, limit: int) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(s or "")).strip()[:limit]


def clean_filename(s) -> str:
    s = re.sub(r"[\x00-\x1f\x7f]", "", str(s or "")).replace("\\", "/").rsplit("/", 1)[-1].strip()
    return s.lstrip(".")[:MAX_NAME] or "file"


def sniff(head: bytes) -> str:
    """Type by magic bytes (never by the name or the browser's claim)."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if head and b"\0" not in head:
        try:
            head[:-4].decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            pass
    return "application/octet-stream"


def _atomic_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class _BodyReader:
    """Reads exactly n bytes of a request body; remembers the first bytes for sniffing."""
    def __init__(self, rfile, n: int):
        self.rfile, self.left, self.head = rfile, n, b""

    def read(self, k: int) -> bytes:
        want, parts = min(k, self.left), []
        while want > 0:
            b = self.rfile.read(want)
            if not b:
                raise Refused(400, "The upload stopped part way. Try again.", close=True)
            parts.append(b)
            want -= len(b)
            self.left -= len(b)
        data = b"".join(parts)
        if len(self.head) < 64:
            self.head += data[:64 - len(self.head)]
        return data


class Vault:
    def __init__(self, folder: Path, cfg: dict | None = None, clock=time.time):
        cfg = cfg or {}
        self.dir, self.clock = Path(folder), clock
        self.meta_path, self.index_path, self.blobs = self.dir / "vault.json", self.dir / "index.bin", self.dir / "blobs"
        self.quota = _num(cfg.get("quota_mb"), 2048, 1, 1_000_000) * MIB
        self.max_files = _num(cfg.get("max_files"), 1000, 1, 20000)
        self.max_file = min(_num(cfg.get("max_file_mb"), 25, 1, 25) * MIB, ROUTE_MAX)
        self.idle = _num(cfg.get("idle_minutes"), 15, 1, 24 * 60) * 60
        self.max_session = _num(cfg.get("max_session_hours"), 12, 1, 72) * 3600
        self.scrypt_n = _num(cfg.get("scrypt_n"), vc.SCRYPT_N, 1024, 1 << 20)
        self.min_pass = _num(cfg.get("min_passphrase"), 10, 8, 64)
        self.openssl = str(cfg.get("openssl") or "/usr/bin/openssl")
        self.lock = threading.RLock()            # sessions, index, metadata
        self.kdf_lock = threading.Lock()         # one scrypt at a time (64 MiB each)
        self.sessions: dict[tuple, dict] = {}
        self.fails: dict[str, list] = {}
        self.gfail = [0, 0.0, 0.0]
        self.pending: dict[str, dict] = {}
        self.exports: dict[str, dict] = {}
        self._index = None                       # (fingerprint, dict) while unlocked somewhere
        self._cleaned = False
        self._openssl_ok = None

    # ── sessions ──
    @staticmethod
    def sid(member: dict) -> tuple:
        return (str(member.get("id", "")), str(member.get("device", "")))

    def expire(self) -> None:
        now = self.clock()
        with self.lock:
            for sid, s in list(self.sessions.items()):
                if now - s["last"] > self.idle or now - s["start"] > self.max_session:
                    self._drop(sid)
            for t in [t for t, p in self.pending.items() if p["exp"] < now or p["sid"] not in self.sessions]:
                del self.pending[t]
            for t in [t for t, p in self.exports.items() if p["exp"] < now]:
                del self.exports[t]
            if not self.sessions:
                self._index = None

    def _drop(self, sid) -> None:
        s = self.sessions.pop(sid, None)
        if s:
            k = s["key"]
            for i in range(len(k)):              # best effort: Python may hold other copies
                k[i] = 0

    def _start(self, member: dict, vk: bytes) -> None:
        now = self.clock()
        with self.lock:
            self._drop(self.sid(member))
            self.sessions[self.sid(member)] = {"key": bytearray(vk), "start": now, "last": now,
                                               "name": str(member.get("name", ""))}
            if len(self.sessions) > 200:
                self._drop(min(self.sessions, key=lambda k: self.sessions[k]["last"]))
        if not self._cleaned:
            self._cleaned = True
            try:
                self._cleanup(vc.Cipher(vk, self.openssl))
            except (vc.CryptoError, OSError):
                pass

    def lock_all(self, keep: tuple | None = None) -> int:
        with self.lock:
            n = 0
            for sid in [s for s in self.sessions if s != keep]:
                self._drop(sid)
                n += 1
            self.expire()
            return n

    def cipher(self, member: dict) -> "vc.Cipher":
        """The cipher for an unlocked device (touches its idle timer); Refused 423 when locked."""
        self.expire()
        with self.lock:
            s = self.sessions.get(self.sid(member))
            if not s:
                raise Refused(423, "The vault is locked.", locked=True)
            s["last"] = self.clock()
            vk = bytes(s["key"])
        return vc.Cipher(vk, self.openssl)

    # ── wrong-passphrase limits ──
    def wait(self, ip: str, member: dict) -> float:
        now = self.clock()
        recs = [self.fails.get("ip:" + ip), self.fails.get("dev:" + "/".join(self.sid(member))), self.gfail]
        return max([0.0] + [r[2] - now for r in recs if r])

    def _bump(self, rec: list, now: float, window: float, limit: int, lock: float) -> None:
        if now - rec[1] > window:
            rec[:] = [0, now, 0.0]
        rec[0] += 1
        if rec[0] >= limit:
            rec[2] = now + lock

    def _failed(self, ip: str, member: dict) -> None:
        now = self.clock()
        with self.lock:
            for k in ("ip:" + ip, "dev:" + "/".join(self.sid(member))):
                self._bump(self.fails.setdefault(k, [0, now, 0.0]), now, FAIL_WINDOW, FAILS, FAIL_LOCK)
            self._bump(self.gfail, now, G_WINDOW, G_FAILS, G_LOCK)
            if len(self.fails) > 2000:
                for k in sorted(self.fails, key=lambda k: self.fails[k][1])[:1000]:
                    self.fails.pop(k, None)

    def _check_wait(self, ip: str, member: dict) -> None:
        w = self.wait(ip, member)
        if w > 0:
            mins = max(1, int(w // 60 + 0.999))
            raise Refused(429, f"Too many wrong tries. Wait {mins} minute{'s' if mins != 1 else ''} and try again.",
                          wait=int(w + 0.999))

    def _succeeded(self, ip: str, member: dict) -> None:
        with self.lock:
            self.fails.pop("ip:" + ip, None)
            self.fails.pop("dev:" + "/".join(self.sid(member)), None)

    # ── metadata (the wrapped key) ──
    def meta(self) -> dict | None:
        try:
            d = json.loads(self.meta_path.read_text())
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) and isinstance(d.get("kdf"), dict) and isinstance(d.get("wrap"), dict) else None

    def _save_meta(self, d: dict) -> None:
        _atomic_bytes(self.meta_path, json.dumps(d, indent=1).encode())

    def _check_new_pass(self, pw) -> str:
        if not isinstance(pw, str):
            raise Refused(400, "Enter a passphrase.")
        n = len(vc.norm_passphrase(pw).decode())
        if n < self.min_pass:
            raise Refused(400, f"Use at least {self.min_pass} characters. A short sentence or four random words works well.")
        if n > 200:
            raise Refused(400, "That passphrase is too long (200 characters at most).")
        return pw

    def _try_pass(self, pw, ip: str, member: dict) -> bytes:
        if not isinstance(pw, str) or not pw or len(pw) > 400:
            raise Refused(400, "Enter the vault passphrase.")
        self._check_wait(ip, member)
        with self.kdf_lock:
            self._check_wait(ip, member)            # others may have failed while we queued
            m = self.meta()
            if not m:
                raise Refused(409, "The vault hasn't been set up yet.")
            try:
                vk = vc.unwrap(m["wrap"], vc.passphrase_material(pw, m["kdf"]))
            except vc.CryptoError:
                self._failed(ip, member)
                raise Refused(403, "That passphrase isn't right.") from None
        self._succeeded(ip, member)
        return vk

    def _need_openssl(self) -> None:
        if self._openssl_ok is None:
            self._openssl_ok = vc.openssl_ok(self.openssl)
        if not self._openssl_ok:
            raise Refused(503, "The vault needs the openssl program, which isn't working on this Badger.")

    def setup(self, member: dict, pw, ip: str) -> str:
        self._check_new_pass(pw)
        self._need_openssl()
        if self.meta():
            raise Refused(409, "The vault is already set up. Unlock it with the family passphrase.")
        vk, rk, rsalt = secrets.token_bytes(32), secrets.token_bytes(32), secrets.token_bytes(16)
        kdf = vc.new_kdf(self.scrypt_n)
        with self.kdf_lock:                     # never hold self.lock while waiting for this one
            material = vc.passphrase_material(pw, kdf)
        c = vc.Cipher(vk, self.openssl)
        with self.lock:
            if self.meta():
                raise Refused(409, "The vault is already set up. Unlock it with the family passphrase.")
            self.dir.mkdir(parents=True, exist_ok=True)
            self.blobs.mkdir(mode=0o700, exist_ok=True)
            idx = {"v": 1, "files": {}, "activity": []}
            self._activity(idx, member, "set up the vault")
            self._write_index(c, idx)
            self._save_meta({"v": 1, "created": int(self.clock()), "kdf": kdf, "wrap": vc.wrap(vk, material),
                             "recovery": {"salt": vc.b64e(rsalt), "wrap": vc.wrap(vk, vc.recovery_material(rk, rsalt), b"recovery")}})
            self._start(member, vk)
        return vc.format_recovery(rk)

    def unlock(self, member: dict, pw, ip: str) -> None:
        vk = self._try_pass(pw, ip, member)
        self._start(member, vk)

    def recover(self, member: dict, code, new_pw, ip: str) -> None:
        self._check_new_pass(new_pw)
        self._check_wait(ip, member)
        m = self.meta()
        if not m:
            raise Refused(409, "The vault hasn't been set up yet.")
        rk = vc.parse_recovery(code)
        try:
            if rk is None:
                raise vc.CryptoError("bad format")
            rec = m.get("recovery") or {}
            vk = vc.unwrap(rec.get("wrap"), vc.recovery_material(rk, vc.b64d(rec.get("salt", ""))), b"recovery")
        except vc.CryptoError:
            self._failed(ip, member)
            raise Refused(403, "That recovery key isn't right. It is 52 letters and digits, in groups of 4.") from None
        self._succeeded(ip, member)
        self._rewrap(vk, new_pw, member, "reset the passphrase with the recovery key")

    def _rewrap(self, vk: bytes, new_pw: str, member: dict, what: str) -> None:
        kdf = vc.new_kdf(self.scrypt_n)
        with self.kdf_lock:
            material = vc.passphrase_material(new_pw, kdf)
        with self.lock:
            m = self.meta()
            m.update(kdf=kdf, wrap=vc.wrap(vk, material), changed=int(self.clock()))
            self._save_meta(m)
            self.lock_all(keep=self.sid(member))    # a passphrase change locks every other device
            self._start(member, vk)
            self._log(member, what)

    def change_passphrase(self, member: dict, old, new, ip: str) -> None:
        self._check_new_pass(new)
        vk = self._try_pass(old, ip, member)
        self._rewrap(vk, new, member, "changed the passphrase")

    def new_recovery(self, member: dict, pw, ip: str) -> str:
        vk = self._try_pass(pw, ip, member)
        rk, rsalt = secrets.token_bytes(32), secrets.token_bytes(16)
        with self.lock:
            m = self.meta()
            m["recovery"] = {"salt": vc.b64e(rsalt), "wrap": vc.wrap(vk, vc.recovery_material(rk, rsalt), b"recovery")}
            self._save_meta(m)
            self._start(member, vk)
            self._log(member, "made a new recovery key")
        return vc.format_recovery(rk)

    # ── index ──
    def _read_index(self, c) -> dict:
        fp = c.fingerprint()
        with self.lock:
            if self._index and self._index[0] == fp:
                return self._index[1]
            try:
                data = self.index_path.read_bytes()
            except OSError:
                raise Refused(500, "The vault's list of documents is missing.") from None
            try:
                idx = json.loads(c.decrypt_bytes(data, vc.KIND_INDEX))
            except vc.CryptoError:
                raise Refused(500, "The vault's list of documents is damaged. Restore it from a backup.") from None
            if not isinstance(idx, dict) or not isinstance(idx.get("files"), dict):
                raise Refused(500, "The vault's list of documents is damaged. Restore it from a backup.")
            idx.setdefault("activity", [])
            self._index = (fp, idx)
            return idx

    def _write_index(self, c, idx: dict) -> None:
        _atomic_bytes(self.index_path, c.encrypt_bytes(json.dumps(idx).encode(), vc.KIND_INDEX))
        self._index = (c.fingerprint(), idx)

    def _activity(self, idx: dict, member: dict, what: str) -> None:
        idx.setdefault("activity", []).append({"at": int(self.clock()), "who": str(member.get("name", ""))[:32],
                                               "what": what[:200]})
        idx["activity"] = idx["activity"][-100:]

    def _log(self, member: dict, what: str) -> None:
        """Activity line for operations that don't otherwise touch the index."""
        try:
            c = self.cipher(member)
            with self.lock:
                idx = json.loads(json.dumps(self._read_index(c)))
                self._activity(idx, member, what)
                self._write_index(c, idx)
        except Refused:
            pass

    def _edit_index(self, member: dict, fn):
        c = self.cipher(member)
        with self.lock:
            idx = json.loads(json.dumps(self._read_index(c)))      # work on a copy; commit by writing
            out = fn(c, idx)
            self._write_index(c, idx)
            return out

    def _used(self, idx: dict) -> int:
        return sum(int(e.get("size", 0)) for e in idx["files"].values())

    @staticmethod
    def _public(e: dict) -> dict:
        return {k: e.get(k) for k in ("id", "kind", "name", "cat", "note", "size", "type", "added", "by", "edited")}

    def status(self, member: dict, ip: str) -> dict:
        self.expire()
        with self.lock:
            s = self.sessions.get(self.sid(member))
            out = {"setup": self.meta() is not None, "unlocked": bool(s),
                   "idle_left": int(self.idle - (self.clock() - s["last"])) if s else 0,
                   "idle_minutes": self.idle // 60, "wait": int(self.wait(ip, member) + 0.999),
                   "quota": self.quota, "max_files": self.max_files, "max_file": self.max_file,
                   "min_passphrase": self.min_pass,
                   "categories": [{"id": c, "label": label} for c, label in CATEGORIES]}
        if not out["setup"]:
            if self._openssl_ok is None:
                self._openssl_ok = vc.openssl_ok(self.openssl)
            out["ready"] = self._openssl_ok
        if s:
            idx = self._read_index(vc.Cipher(bytes(s["key"]), self.openssl))
            out.update(used=self._used(idx), count=len(idx["files"]),
                       activity=list(reversed(idx.get("activity", [])[-20:])))
        return out

    def list(self, member: dict, q: str = "", cat: str = "") -> dict:
        c = self.cipher(member)
        idx = self._read_index(c)
        words = [w for w in clean_text(q, 100).casefold().split() if w]
        rows = []
        for e in idx["files"].values():
            if cat and e.get("cat") != cat:
                continue
            hay = (e.get("name", "") + " " + e.get("note", "")).casefold()
            if all(w in hay for w in words):
                rows.append(self._public(e))
        rows.sort(key=lambda r: -(r.get("added") or 0))
        return {"files": rows, "used": self._used(idx), "count": len(idx["files"]), "quota": self.quota}

    def _check_room(self, idx: dict, size: int, extra_files: int = 0, reserved: int = 0) -> None:
        if len(idx["files"]) + extra_files >= self.max_files:
            raise Refused(413, f"The vault holds at most {self.max_files} items. Delete some first.")
        if self._used(idx) + reserved + size > self.quota:
            raise Refused(413, "The vault is full. Delete something, or ask the owner to raise the limit.")

    def _fields(self, data: dict, idx_entry: dict | None = None) -> dict:
        out = {}
        if "name" in data or idx_entry is None:
            out["name"] = clean_filename(data.get("name"))
        if "category" in data or idx_entry is None:
            cat = str(data.get("category") or "other")
            out["cat"] = cat if cat in CAT_IDS else "other"
        if "note" in data or idx_entry is None:
            out["note"] = clean_text(data.get("note"), MAX_NOTE)
        return out

    # ── uploads ──
    def start_upload(self, member: dict, data: dict) -> dict:
        size = data.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise Refused(400, "File size missing.")
        if size > self.max_file:
            raise Refused(413, f"Files can be at most {self.max_file // MIB} MB. Take a smaller photo or split the PDF.")
        c = self.cipher(member)
        fields = self._fields(data)
        with self.lock:
            idx = self._read_index(c)
            mine = [t for t, p in self.pending.items() if p["sid"] == self.sid(member)]
            for t in sorted(mine, key=lambda t: self.pending[t]["exp"])[:max(0, len(mine) - 2)]:
                del self.pending[t]
            if len(self.pending) >= 30:
                raise Refused(429, "Too many uploads at once. Try again in a minute.")
            self._check_room(idx, size, len(self.pending), sum(p["size"] for p in self.pending.values()))
            tok = secrets.token_urlsafe(18)
            self.pending[tok] = {"sid": self.sid(member), "size": size, "exp": self.clock() + 900, **fields}
        return {"upload": tok}

    def receive_upload(self, req, member: dict, tok: str) -> dict:
        n_raw = req.headers.get("Content-Length") or ""
        with self.lock:
            p = self.pending.get(tok)
            if not p or p["sid"] != self.sid(member):
                raise Refused(404, "That upload has expired. Choose the file again.", close=True)
            del self.pending[tok]
        if not n_raw.isdigit() or int(n_raw) != p["size"]:
            raise Refused(400, "The file size changed during upload. Try again.", close=True)
        c = self.cipher(member)
        self.blobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.blobs / f".up-{secrets.token_hex(8)}.tmp"
        body = _BodyReader(req.rfile, p["size"])
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                blob_id, size = c.encrypt_stream(body.read, fh, vc.KIND_FILE)
                fh.flush()
                os.fsync(fh.fileno())
            entry = {"id": secrets.token_urlsafe(12), "kind": "file", "name": p["name"], "cat": p["cat"],
                     "note": p["note"], "size": size, "type": sniff(body.head), "added": int(self.clock()),
                     "by": str(member.get("name", ""))[:32], "blob": blob_id.hex()}

            def commit(c2, idx):
                self._check_room(idx, size)
                os.replace(tmp, self.blobs / f"{entry['blob']}.bin")
                idx["files"][entry["id"]] = entry
                self._activity(idx, member, f"added “{entry['name']}”")
            self._edit_index(member, commit)
        except BaseException as e:
            if body.left:
                req.close_connection = True       # unread body bytes must not be parsed as a request
            try:
                tmp.unlink()
            except OSError:
                pass
            if isinstance(e, vc.CryptoError):
                raise Refused(500, "Couldn't encrypt the file: " + str(e)[:120]) from None
            raise
        return {"ok": True, "file": self._public(entry)}

    def add_note(self, member: dict, data: dict) -> dict:
        text = str(data.get("text") or "").replace("\r\n", "\n")
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)[:MAX_TEXT]
        if not text.strip():
            raise Refused(400, "The note is empty.")
        fields = self._fields(dict(data, name=data.get("name") or text.strip().split("\n")[0][:60]))
        c = self.cipher(member)
        raw = text.encode()
        blob = c.encrypt_bytes(raw, vc.KIND_NOTE)
        blob_hex = blob[len(vc.MAGIC) + 1:vc.HEADER_LEN].hex()
        entry = {"id": secrets.token_urlsafe(12), "kind": "note", "size": len(raw), "type": "text/plain",
                 "added": int(self.clock()), "by": str(member.get("name", ""))[:32], "blob": blob_hex, **fields}

        def commit(c2, idx):
            self._check_room(idx, len(raw))
            self.blobs.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_bytes(self.blobs / f"{blob_hex}.bin", blob)
            idx["files"][entry["id"]] = entry
            self._activity(idx, member, f"added the note “{entry['name']}”")
        self._edit_index(member, commit)
        return {"ok": True, "file": self._public(entry)}

    def _entry(self, idx: dict, fid) -> dict:
        if not isinstance(fid, str) or not ID_RE.match(fid) or fid not in idx["files"]:
            raise Refused(404, "That document isn't in the vault (maybe someone deleted it).")
        return idx["files"][fid]

    def read_note(self, member: dict, fid: str) -> dict:
        c = self.cipher(member)
        e = self._entry(self._read_index(c), fid)
        if e.get("kind") != "note":
            raise Refused(400, "That's a file, not a note.")
        try:
            data = (self.blobs / f"{e['blob']}.bin").read_bytes()
            text = c.decrypt_bytes(data, vc.KIND_NOTE, bytes.fromhex(e["blob"])).decode("utf-8", "replace")
        except (OSError, vc.CryptoError):
            raise Refused(500, "This note is damaged or missing. Restore it from a backup.") from None
        return {"text": text, "file": self._public(e)}

    def edit(self, member: dict, data: dict) -> dict:
        old_blob = []

        def fn(c, idx):
            e = self._entry(idx, data.get("id"))
            e.update(self._fields(data, e))
            if e.get("kind") == "note" and "text" in data:
                text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(data.get("text") or "").replace("\r\n", "\n"))[:MAX_TEXT]
                if not text.strip():
                    raise Refused(400, "The note is empty.")
                raw = text.encode()
                self._check_room(idx, len(raw) - int(e.get("size", 0)), extra_files=-1)
                blob = c.encrypt_bytes(raw, vc.KIND_NOTE)
                new_hex = blob[len(vc.MAGIC) + 1:vc.HEADER_LEN].hex()
                _atomic_bytes(self.blobs / f"{new_hex}.bin", blob)
                old_blob.append(e["blob"])
                e.update(blob=new_hex, size=len(raw))
            e["edited"] = int(self.clock())
            self._activity(idx, member, f"edited “{e['name']}”")
            return self._public(e)
        out = self._edit_index(member, fn)
        for b in old_blob:
            try:
                (self.blobs / f"{b}.bin").unlink()
            except OSError:
                pass
        return {"ok": True, "file": out}

    def delete(self, member: dict, fid) -> dict:
        def fn(c, idx):
            e = self._entry(idx, fid)
            del idx["files"][fid]
            self._activity(idx, member, f"deleted “{e['name']}”")
            return e
        e = self._edit_index(member, fn)
        try:
            (self.blobs / f"{e['blob']}.bin").unlink()
        except OSError:
            pass
        return {"ok": True}

    def send_file(self, req, member: dict, fid: str, inline: bool):
        c = self.cipher(member)
        e = self._entry(self._read_index(c), fid)
        if e.get("kind") != "file":
            raise Refused(400, "That's a note, not a file.")
        blob_id = bytes.fromhex(e["blob"])
        try:
            fh = open(self.blobs / f"{e['blob']}.bin", "rb")
        except OSError:
            raise Refused(500, "This file is missing from the vault. Restore it from a backup.") from None
        with fh:
            try:
                size = c.verify(fh, vc.KIND_FILE, blob_id)       # every chunk checked before a byte goes out
            except vc.CryptoError:
                raise Refused(500, "This file is damaged and can't be opened. Restore it from a backup.") from None
            if size != e.get("size"):
                raise Refused(500, "This file is damaged and can't be opened. Restore it from a backup.")
            fh.seek(0)
            ctype = e.get("type") if e.get("type") in INLINE_TYPES + ("application/pdf",) else "application/octet-stream"
            name = e.get("name") or "file"
            ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", name)[:120] or "file"
            disp = "inline" if inline and ctype in INLINE_TYPES else "attachment"
            req.send_response(200)
            req.send_header("Content-Type", ctype)
            req.send_header("Content-Length", str(size))
            req.send_header("Content-Disposition",
                            f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}")
            req.send_header("Cache-Control", "no-store")
            req.send_header("X-Content-Type-Options", "nosniff")
            req.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            req.send_header("Referrer-Policy", "no-referrer")
            req.end_headers()
            try:
                for chunk in c.decrypt_iter(fh, vc.KIND_FILE, blob_id):
                    req.wfile.write(chunk)
            except vc.CryptoError:
                req.close_connection = True       # headers are gone; a short body tells the browser it failed
        return None

    # ── backup bundle ──
    def start_export(self, member: dict) -> dict:
        self.cipher(member)
        tok = secrets.token_urlsafe(24)
        with self.lock:
            self.exports[tok] = {"sid": self.sid(member), "exp": self.clock() + 120}
        return {"url": f"/api/vault/export/{tok}"}

    def send_export(self, req, tok: str):
        self.expire()
        with self.lock:
            p = self.exports.pop(tok, None)
            if not p or p["sid"] not in self.sessions:
                raise Refused(404, "That backup link has expired. Unlock the vault and press Export again.")
            meta = self.meta_path.read_bytes()
            index = self.index_path.read_bytes()
            s = self.sessions[p["sid"]]
            idx = self._read_index(vc.Cipher(bytes(s["key"]), self.openssl))
            blobs = sorted({e["blob"] for e in idx["files"].values() if e.get("blob")})
        day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        req.send_response(200)
        req.send_header("Content-Type", "application/x-tar")
        req.send_header("Content-Disposition", f'attachment; filename="honey-badger-vault-{day}.tar"')
        req.send_header("Cache-Control", "no-store")
        req.send_header("X-Content-Type-Options", "nosniff")
        req.send_header("Connection", "close")
        req.end_headers()
        req.close_connection = True
        now = int(self.clock())

        def add(tf, name, data=None, fh=None, size=0):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = (len(data) if data is not None else size), 0o600, now
            tf.addfile(ti, io.BytesIO(data) if data is not None else fh)
        with tarfile.open(fileobj=req.wfile, mode="w|", format=tarfile.PAX_FORMAT) as tf:
            add(tf, "README.txt", BUNDLE_README.encode())
            add(tf, "vault.json", meta)
            add(tf, "index.bin", index)
            for b in blobs:
                try:
                    fh = open(self.blobs / f"{b}.bin", "rb")
                except OSError:
                    continue                      # deleted while we were sending
                with fh:
                    add(tf, f"blobs/{b}.bin", fh=fh, size=os.fstat(fh.fileno()).st_size)
        return None

    def _cleanup(self, c) -> None:
        """Once per start: drop upload leftovers and blobs no index entry points to (a crash between
        writing a blob and the index). Only ever ciphertext, but no reason to keep it."""
        idx = self._read_index(c)
        live = {e.get("blob") for e in idx["files"].values()}
        cutoff = self.clock() - 3600
        for f in self.blobs.glob("*") if self.blobs.is_dir() else []:
            try:
                old = f.stat().st_mtime < cutoff
                if old and (f.name.endswith(".tmp") or (f.suffix == ".bin" and f.stem not in live)):
                    f.unlink()
            except OSError:
                pass


BUNDLE_README = """Honey Badger family document vault - encrypted backup

Everything in this bundle is encrypted. Without the family vault passphrase
(or the printed recovery key, on a Badger) nobody can read it.

To put it back on a Badger:   python3 /opt/prepper/software/portal/features/_vault_crypto.py restore THIS.tar /var/lib/prepper/portal/features/vault
   (then: sudo chown -R prepper:prepper /var/lib/prepper/portal/features/vault)
To read it on any computer with python3 and openssl, copy _vault_crypto.py next to it and run:
   python3 _vault_crypto.py list THIS.tar
   python3 _vault_crypto.py extract THIS.tar FOLDER     (writes UNENCRYPTED copies into FOLDER)
"""

# ── plumbing ──
_V: Vault | None = None
_V_lock = threading.Lock()


def _vault(ctx) -> Vault:
    global _V
    d = ctx.data_dir()
    with _V_lock:
        if _V is None or _V.dir != d:
            _V = Vault(d, ctx.cfg())
        return _V


def hotspot_has_password(ctx) -> bool:
    """WPA2 with a passphrase ([network] security auto/wpa2), as os/render.py configures hostapd.
    "owe" (Enhanced Open) and "open" have no password: owe stops passive listeners but anyone can
    set up a look-alike hotspot in front of it, so it counts as no password here."""
    net = (ctx.raw_config() or {}).get("network") or {}
    psk = net.get("passphrase", "")
    return isinstance(psk, str) and bool(psk) and net.get("security", "auto") in ("auto", "wpa2")


def plain_http(req) -> bool:
    """Plain http:// from a phone. nginx always says which (X-Forwarded-Proto); a request on the Badger
    itself (loopback, no proxy headers) counts as secure, like Handler.secure_transport."""
    if req is None or req.secure_transport():
        return False
    proto = (req.headers.get("X-Forwarded-Proto") or "").strip().lower()
    return proto == "http" or req.client_address[0] not in ("127.0.0.1", "::1")


def transport_refusal(req, ctx) -> str | None:
    """The vault never takes a passphrase or hands out a document over plain http on a hotspot without a
    password: every frame there can be read by anyone in range, no need to even join (red-team finding 2)."""
    if not plain_http(req) or hotspot_has_password(ctx):
        return None
    net = (ctx.raw_config() or {}).get("network") or {}
    host = str(net.get("hostname") or "badger")
    https = net.get("https", True) is not False
    return ("This Wi-Fi has no password, so anyone nearby could read the vault over this connection. "
            + (f"Open https://{host}.lan/#/vault instead (install the Badger certificate first: "
               "Phones & laptops page), or ask the admin to set a Wi-Fi password."
               if https else "Ask the admin to set a Wi-Fi password (or turn on https)."))


TRANSPORT_WARNING = ("You're on plain http://. Anyone who knows the Wi-Fi password could read the vault over "
                     "this connection; https:// is safer.")


def _api(fn):
    def h(req, ctx, args):
        why = transport_refusal(req, ctx)
        if why and fn.__name__ != "h_lock":           # locking is always allowed
            return 403, {"ok": False, "error": why, "insecure": True}
        try:
            return fn(req, ctx, args, _vault(ctx))
        except Refused as e:
            if e.extra.get("close"):
                req.close_connection = True
            extra = {k: v for k, v in e.extra.items() if k != "close"}
            if getattr(req, "_responded", False):
                return None
            hdr = {"Retry-After": str(extra["wait"])} if "wait" in extra else None
            return e.status, {"ok": False, "error": e.message, **extra}, hdr
    h.__name__ = fn.__name__
    return h


def _data(args) -> dict:
    return args["data"] if isinstance(args.get("data"), dict) else {}


def _tail(args, prefix: str) -> str:
    t = args["path"][len(prefix):]
    if not ID_RE.match(t):
        raise Refused(404, "not found")
    return t


@_api
def h_status(req, ctx, args, v):
    st = v.status(args["member"], args["ip"])
    if plain_http(req):
        st["warning"] = TRANSPORT_WARNING
    return st


@_api
def h_setup(req, ctx, args, v):
    rk = v.setup(args["member"], _data(args).get("passphrase"), args["ip"])
    return {"ok": True, "recovery_key": rk}


@_api
def h_unlock(req, ctx, args, v):
    v.unlock(args["member"], _data(args).get("passphrase"), args["ip"])
    return {"ok": True}


@_api
def h_recover(req, ctx, args, v):
    d = _data(args)
    v.recover(args["member"], d.get("recovery"), d.get("passphrase"), args["ip"])
    return {"ok": True}


@_api
def h_lock(req, ctx, args, v):
    if _data(args).get("all") is True:
        v.cipher(args["member"])              # only an unlocked device may lock everyone out
        return {"ok": True, "locked": v.lock_all()}
    with v.lock:
        v._drop(v.sid(args["member"]))
    v.expire()
    return {"ok": True, "locked": 1}


@_api
def h_passphrase(req, ctx, args, v):
    d = _data(args)
    v.change_passphrase(args["member"], d.get("old"), d.get("new"), args["ip"])
    return {"ok": True}


@_api
def h_new_recovery(req, ctx, args, v):
    return {"ok": True, "recovery_key": v.new_recovery(args["member"], _data(args).get("passphrase"), args["ip"])}


@_api
def h_list(req, ctx, args, v):
    d = _data(args)
    return v.list(args["member"], str(d.get("q") or ""), str(d.get("category") or ""))


@_api
def h_upload_start(req, ctx, args, v):
    return v.start_upload(args["member"], _data(args))


@_api
def h_upload(req, ctx, args, v):
    req.close_connection = True               # whatever happens, never parse leftover body bytes as a request
    t = args["path"][len("/api/vault/upload/"):]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", t):
        raise Refused(404, "That upload has expired. Choose the file again.", close=True)
    return v.receive_upload(req, args["member"], t)


@_api
def h_note_add(req, ctx, args, v):
    return v.add_note(args["member"], _data(args))


@_api
def h_note_get(req, ctx, args, v):
    return v.read_note(args["member"], _tail(args, "/api/vault/note/"))


@_api
def h_edit(req, ctx, args, v):
    return v.edit(args["member"], _data(args))


@_api
def h_delete(req, ctx, args, v):
    return v.delete(args["member"], _data(args).get("id"))


@_api
def h_file(req, ctx, args, v):
    inline = (args["qs"].get("inline") or [""])[0] == "1"
    return v.send_file(req, args["member"], _tail(args, "/api/vault/file/"), inline)


@_api
def h_export_start(req, ctx, args, v):
    return v.start_export(args["member"])


@_api
def h_export(req, ctx, args, v):
    t = args["path"][len("/api/vault/export/"):]
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", t):
        raise Refused(404, "That backup link has expired.")
    return v.send_export(req, t)


def setup(ctx):
    v = _vault(ctx)

    def reaper():
        while True:
            time.sleep(20)
            try:
                _vault(ctx).expire()
            except Exception:  # noqa: BLE001 — never let the timer die
                pass
    threading.Thread(target=reaper, name="vault-autolock", daemon=True).start()
    if not Path(v.openssl).is_file():
        ctx.log(f"openssl not found at {v.openssl}: the vault can't be used until it is installed")


ROUTES = [
    {"method": "GET", "path": "/api/vault/status", "handler": h_status, "access": "member"},
    {"method": "POST", "path": "/api/vault/setup", "handler": h_setup, "access": "member"},
    {"method": "POST", "path": "/api/vault/unlock", "handler": h_unlock, "access": "member"},
    {"method": "POST", "path": "/api/vault/recover", "handler": h_recover, "access": "member"},
    {"method": "POST", "path": "/api/vault/lock", "handler": h_lock, "access": "member"},
    {"method": "POST", "path": "/api/vault/passphrase", "handler": h_passphrase, "access": "member"},
    {"method": "POST", "path": "/api/vault/recovery-key", "handler": h_new_recovery, "access": "member"},
    {"method": "POST", "path": "/api/vault/list", "handler": h_list, "access": "member"},
    {"method": "POST", "path": "/api/vault/upload-start", "handler": h_upload_start, "access": "member"},
    {"method": "POST", "path": "/api/vault/upload/", "handler": h_upload, "access": "member", "prefix": True,
     "raw": True, "max_bytes": ROUTE_MAX},
    {"method": "POST", "path": "/api/vault/note", "handler": h_note_add, "access": "member"},
    {"method": "GET", "path": "/api/vault/note/", "handler": h_note_get, "access": "member", "prefix": True},
    {"method": "POST", "path": "/api/vault/edit", "handler": h_edit, "access": "member"},
    {"method": "POST", "path": "/api/vault/delete", "handler": h_delete, "access": "member"},
    {"method": "GET", "path": "/api/vault/file/", "handler": h_file, "access": "member", "prefix": True},
    {"method": "POST", "path": "/api/vault/export", "handler": h_export_start, "access": "member"},
    # A plain link (no member header can ride along on a download), so the one-time token is the key:
    # 32 random bytes, 2 minutes, single use, and only while the device that asked is still unlocked.
    {"method": "GET", "path": "/api/vault/export/", "handler": h_export, "access": "public", "prefix": True},
]
