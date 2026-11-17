"""
Shared helpers for the neighbourhood features (aidstation, skills, missing).

Not a feature itself (the leading "_" keeps the portal from loading it). Each feature loads
its own copy by file path (see _common() in each feature), so nothing depends on sys.path
and no state (rate limits, caches) is shared between features.

  JsonStore   one JSON file, guarded by a lock, cached by mtime, written atomically 0600
  RateLimit   per-key sliding window ("5 posts per 10 minutes per address"), bounded memory
  Field       validation of client fields (type, length, control/bidi characters)
  fold/match  case- and accent-insensitive partial search ("jose" finds "José")
  tokens      random edit tokens; only a SHA-256 is stored
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
# C0 controls except \n (multi-line fields keep it), DEL, C1 controls, and the Unicode
# direction overrides that can make a post display as something it isn't.
_CTRL_ML = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")
_CTRL_1L = re.compile(r"[\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")
_FOLD_EXTRA = {"ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "æ": "ae", "œ": "oe", "ı": "i", "ß": "ss"}


# ── storage ──

def write_private(path: Path, data: bytes | str) -> None:
    """Write-then-rename, file mode 0600 from the start (mkstemp), fsync'd, never half-written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode("utf-8") if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class JsonStore:
    """One JSON object in one file. All reads and changes go through view()/update(), which
    hold the lock; the parsed copy is reused while the file's mtime/size are unchanged."""

    def __init__(self, path: Path, default):
        self.path = Path(path)
        self.default = default                 # callable → fresh default dict
        self.lock = threading.RLock()
        self._cache = None
        self._stamp = None

    def _stat(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def _get(self) -> dict:
        stamp = self._stat()
        if self._cache is not None and stamp == self._stamp:
            return self._cache
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            d = None
        base = self.default()
        if not isinstance(d, dict):
            d = base
        for k, v in base.items():               # wrong shapes (hand-edited, damaged) fall back to the default
            if not isinstance(d.get(k), type(v)):
                d[k] = v
        self._cache, self._stamp = d, stamp
        return d

    def view(self, fn):
        """fn(data) → result, under the lock. fn must not change data."""
        with self.lock:
            return fn(self._get())

    def update(self, fn):
        """fn(data) → (result, changed). Saved when changed; on an exception the cache is dropped,
        so a half-made change is never kept in memory."""
        with self.lock:
            d = self._get()
            try:
                res, changed = fn(d)
                if changed:
                    write_private(self.path, json.dumps(d, separators=(",", ":"), ensure_ascii=False))
                    self._stamp = self._stat()
            except BaseException:
                self._cache = None
                raise
            return res


# ── rate limits ──

class RateLimit:
    """At most `count` events per `window` seconds per key. check() records the event when it is
    allowed and returns 0, else returns the seconds to wait (nothing recorded)."""

    def __init__(self, count: int, window: float, clock=time.monotonic, max_keys: int = 2000):
        self.count, self.window, self.clock, self.max_keys = count, window, clock, max_keys
        self.lock = threading.Lock()
        self.hits: dict[str, list[float]] = {}

    def check(self, key: str, count: int | None = None) -> float:
        limit = self.count if count is None else count
        with self.lock:
            now = self.clock()
            lst = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(lst) >= limit:
                self.hits[key] = lst
                return max(1.0, self.window - (now - lst[0]))
            lst.append(now)
            self.hits[key] = lst
            if len(self.hits) > self.max_keys:
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] >= self.window]:
                    del self.hits[k]
                if len(self.hits) > self.max_keys:          # still full: forget the quietest half
                    for k in sorted(self.hits, key=lambda k: self.hits[k][-1])[: len(self.hits) // 2]:
                        del self.hits[k]
            return 0.0

    def clear(self) -> None:
        with self.lock:
            self.hits.clear()


def wait_msg(seconds: float) -> str:
    s = int(seconds + 0.999)
    return f"Too many in a short time. Try again in {s} seconds." if s < 120 else \
        f"Too many in a short time. Try again in {(s + 59) // 60} minutes."


# ── fields ──

class FieldError(ValueError):
    pass


def clean_text(value, limit: int, *, multiline: bool = False, label: str = "text", required: bool = False) -> str:
    """A client string: must be a string (or absent), control/bidi characters removed, trimmed,
    at most `limit` characters (refused, not cut, so nobody's words are silently lost)."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise FieldError(f"{label}: must be text")
    if multiline:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = _CTRL_ML.sub("", value.replace("\t", " "))
        value = re.sub(r"\n{3,}", "\n\n", value)
    else:
        value = _CTRL_1L.sub("", value.replace("\t", " ").replace("\n", " "))
    value = value.strip()
    if len(value) > limit:
        raise FieldError(f"{label}: at most {limit} characters")
    if required and not value:
        raise FieldError(f"{label}: required")
    return value


def clean_choice(value, choices, label: str, default=None):
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or value not in choices:
        raise FieldError(f"{label}: choose one of {', '.join(choices)}")
    return value


def clean_bool(value, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise FieldError(f"{label}: must be true or false")
    return value


def clean_int(value, lo: int, hi: int, label: str, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise FieldError(f"{label}: a whole number from {lo} to {hi}")
    return value


def cfg_int(cfg: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = (cfg or {}).get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return int(min(hi, max(lo, v)))


def cfg_bool(cfg: dict, key: str, default: bool) -> bool:
    v = (cfg or {}).get(key, default)
    return v if isinstance(v, bool) else default


def valid_id(s) -> bool:
    return isinstance(s, str) and bool(ID_RE.match(s))


def new_id() -> str:
    return secrets.token_urlsafe(9)


# ── search ──

def fold(s) -> str:
    """'José ÑÚÑEZ-Øvergård' → 'jose nunez-overgard': accents, case and spacing don't matter."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = "".join(_FOLD_EXTRA.get(c, c) for c in s)
    return " ".join(s.split())


def match(query: str, *texts) -> bool:
    """Every word of the query appears somewhere (partial words count)."""
    words = fold(query).split()
    if not words:
        return True
    hay = " \n ".join(fold(t) for t in texts)
    return all(w in hay for w in words)


# ── tokens and addresses ──

def new_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(24)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(str(raw).encode()).hexdigest()


def token_ok(raw, stored) -> bool:
    if not isinstance(raw, str) or not raw or len(raw) > 128 or not isinstance(stored, str) or not stored:
        return False
    return hmac.compare_digest(hash_token(raw), stored)


def ip_tag(data_dir: Path, ip: str) -> str:
    """A salted hash of the client address, so per-address caps survive a restart without the
    files keeping a list of who was on the Wi-Fi."""
    salt_file = Path(data_dir) / ".salt"
    try:
        salt = salt_file.read_text().strip()
    except OSError:
        salt = ""
    if len(salt) < 16:
        salt = secrets.token_hex(16)
        try:
            write_private(salt_file, salt)
        except OSError:
            pass
    return hashlib.sha256(f"{salt}|{ip}".encode()).hexdigest()[:20]


def csv_cell(v) -> str:
    """Spreadsheet-safe cell: a leading = + - @ (or tab/CR) would run as a formula in Excel/LibreOffice."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s
