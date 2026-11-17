"""
Family members for the Honey Badger: one-time invite codes, a key per device, removal.

  * The admin makes an invite (a 10-character code, valid 24 h, usable once). Whoever
    redeems it becomes a member, or adds a device to an existing member. The front panel
    can make invites too, from its own small pool (so it can never use up the admin's).
  * Each device gets its own random key (shown once, kept in that browser). Only a
    SHA-256 of it is stored, so a copy of this file can't be used to log in.
  * Removing a member kills all their devices at once and frees their mesh radios.
  * Members can register Meshtastic radios. A radio ID alone proves nothing on the mesh
    (anyone can transmit with any ID), so the bridge uses it only to put a name on posts
    that arrive on the family's private channel.
  * Names are cleaned (no control, invisible or direction-changing characters) and must
    not look like another member's name.

Standard library only; one JSON file, written atomically (with a .bak of the previous
version), guarded by a lock. A file that can't be read is never treated as "no members":
writes are refused until it is fixed (from members.json.bak, a copy of the last good list),
so one bad file can't wipe the family.
"""
from __future__ import annotations

import errno
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

ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"        # no 0/O, 1/I/L, U: easy to read aloud and type
INVITE_TTL = 24 * 3600
MAX_MEMBERS, MAX_DEVICES, MAX_INVITES, MAX_RADIOS = 50, 10, 20, 5
PANEL_MAX_INVITES = 5                    # open invites made at the front panel (a pool of its own)
PANEL_RATE = (5, 600.0)                  # …and at most 5 of them per 10 minutes
NODE_RE = re.compile(r"^![0-9a-f]{8}$")
JOIN_FAILS, JOIN_WINDOW, JOIN_LOCK = 5, 600.0, 600.0
FAILS_KEEP = 1000                        # remembered guessing addresses (locked ones are never dropped first)


class StoreError(RuntimeError):
    """members.json exists but can't be read or parsed: nothing is written until it is fixed."""


def _h(s: str) -> str:
    # surrogatepass: a JSON "\ud800" escape must hash (and simply not match), not crash
    return hashlib.sha256(str(s).encode("utf-8", "surrogatepass")).hexdigest()


def norm_code(code) -> str:
    """'k7qx-m2pd-9z' → 'K7QXM2PD9Z': case and separators don't matter. The alphabet has no
    look-alike characters (0/O, 1/I/L, U/V), so there is nothing else to fold."""
    return re.sub(r"[\s\-_.]", "", str(code if isinstance(code, str) else "")).upper()[:32]


# Characters that change how text around them is shown, or are invisible: bidi overrides and
# isolates, zero-width joiners/spaces, BOM, soft hyphen (all category Cf), C0/C1 controls (Cc),
# lone surrogates (Cs), private use (Co), unassigned (Cn), line/paragraph separators (Zl, Zp).
_DROP = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def clean_text(s, limit: int) -> str:
    s = "".join(ch for ch in str(s if s is not None else "") if unicodedata.category(ch) not in _DROP)
    return " ".join(s.split())[:limit]


def clean_name(s, limit: int = 32) -> str:
    return clean_text(s, limit)


# Letters from other scripts that look like Latin ones (the common impersonation set).
_CONFUSABLE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ӏ": "l", "к": "k", "м": "m", "т": "t", "в": "b", "н": "h",
    "α": "a", "ο": "o", "ρ": "p", "ν": "v", "τ": "t", "ι": "i", "κ": "k", "χ": "x", "υ": "u", "ε": "e",
    "0": "o", "1": "l", "i": "l", "|": "l", "!": "l",
})


def name_key(name: str) -> str:
    """What a name looks like, for "is this the same person?": case, spacing, punctuation,
    accents and look-alike letters from other scripts don't count ("Ana", "ANA", "A na", "Аna")."""
    s = unicodedata.normalize("NFKD", clean_name(name, 64)).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.translate(_CONFUSABLE)
    return "".join(ch for ch in s if ch.isalnum())


def _read_nofollow(path: Path) -> bytes:
    """A regular file's bytes. A symlink planted where our file should be is not ours: it reads as
    "no file yet" (FileNotFoundError), and the next write replaces the link, never its target."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        if e.errno == errno.ELOOP or (e.errno == errno.ENOENT):
            raise FileNotFoundError(str(path)) from None
        raise
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _devices(m: dict) -> list:
    d = m.get("devices")
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def _strs(v) -> list[str]:
    return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []


class Members:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}        # ip → [count, first_fail, locked_until]
        self._seen_write: dict[str, float] = {}
        self._panel_made: list[float] = []

    # ── storage ──
    @property
    def backup(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def _parse(self, raw: bytes) -> dict:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        d.setdefault("v", 1)
        d["members"] = {k: v for k, v in d["members"].items() if isinstance(v, dict)} \
            if isinstance(d.get("members"), dict) else {}
        d["invites"] = d.get("invites") if isinstance(d.get("invites"), dict) else {}
        return d

    def _load(self) -> dict:
        """The members file, or a fresh empty one if there is none yet. StoreError if it exists
        but can't be read or parsed — never "empty", which the next write would make permanent."""
        try:
            raw = _read_nofollow(self.path)
        except FileNotFoundError:
            return {"v": 1, "members": {}, "invites": {}}
        except OSError as e:
            raise StoreError(f"the family member list can't be read ({e.strerror or e})") from None
        try:
            return self._parse(raw)
        except (ValueError, RecursionError, AttributeError):
            raise StoreError("the family member list is damaged (members.json); an admin can restore the "
                             "last good copy from Settings → Family members") from None

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(d, indent=1).encode()
        self._write_file(self.path, data)
        try:                                       # a second copy of the latest good list, for restore_backup
            self._write_file(self.backup, data)
        except OSError:
            pass

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)   # O_EXCL, 0600
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)                   # replaces a planted symlink rather than following it
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def status(self) -> dict:
        """For the admin page: is the list readable, and is there a good backup to go back to?"""
        try:
            self._load()
            ok, err = True, None
        except StoreError as e:
            ok, err = False, str(e)
        try:
            self._parse(self.backup.read_bytes())
            bak = True
        except (OSError, ValueError, RecursionError, AttributeError):
            bak = False
        return {"ok": ok, "error": err, "backup": bak}

    def restore_backup(self) -> dict:
        """Put the last good copy back. The damaged file is kept next to it (members.json.damaged-<time>)."""
        with self.lock:
            try:
                raw = self.backup.read_bytes()
                self._parse(raw)
            except (OSError, ValueError, RecursionError, AttributeError):
                raise ValueError("there is no good backup copy of the member list") from None
            if self.path.exists():
                os.replace(self.path, self.path.with_name(f"{self.path.name}.damaged-{int(self.clock())}"))
            self._write_file(self.path, raw)
        return self.status()

    def _prune(self, d: dict) -> None:
        now = self.clock()
        for k in [k for k, v in d["invites"].items()
                  if not isinstance(v, dict) or (_num(v.get("expires")) or 0) <= now]:
            del d["invites"][k]

    @staticmethod
    def _active(d: dict) -> list[dict]:
        return [m for m in d["members"].values() if not m.get("removed")]

    def _name_taken(self, d: dict, name: str, except_mid: str | None = None) -> bool:
        key = name_key(name)
        if not key:
            return False
        for mid, m in d["members"].items():
            if mid != except_mid and not m.get("removed") and name_key(m.get("name", "")) == key:
                return True
        for v in d["invites"].values():                  # a name promised to an open invite
            if isinstance(v, dict) and not v.get("member") and v.get("name") and name_key(v["name"]) == key:
                return True
        return False

    # ── invites ──
    def create_invite(self, name: str = "", member_id: str | None = None, ttl: float = INVITE_TTL,
                      source: str = "admin") -> dict:
        """Returns {"code": "K7QX-M2PD-9Z", "expires": ts, "id": …}. member_id: an extra device for that
        member. source "panel": the front panel's own small pool, rate-limited."""
        nm = clean_name(name)
        with self.lock:
            d = self._load()
            self._prune(d)
            now = self.clock()
            if member_id is not None:
                m = d["members"].get(member_id)
                if not m or m.get("removed"):
                    raise ValueError("no such member")
                nm = ""
            elif len(self._active(d)) >= MAX_MEMBERS:
                raise ValueError(f"at most {MAX_MEMBERS} members")
            elif nm and self._name_taken(d, nm):
                raise ValueError("someone already has that name (or one that looks just like it)")
            pool = [v for v in d["invites"].values() if (v.get("source") or "admin") == source]
            if source == "panel":
                self._panel_made = [t for t in self._panel_made if now - t < PANEL_RATE[1]]
                if len(pool) >= PANEL_MAX_INVITES:
                    raise ValueError("too many open invites from this screen: use one or wait a day")
                if len(self._panel_made) >= PANEL_RATE[0]:
                    raise ValueError("too many invites in a short time: wait 10 minutes")
            elif len(pool) >= MAX_INVITES:
                raise ValueError("too many open invites: cancel some (Family members → Open invites)")
            raw = "".join(secrets.choice(ALPHABET) for _ in range(10))
            iid = "i_" + secrets.token_hex(4)
            d["invites"][_h(norm_code(raw))] = {"id": iid, "expires": now + ttl, "created": now,
                                                "member": member_id, "name": nm, "source": source}
            self._save(d)
            if source == "panel":
                self._panel_made.append(now)
        return {"code": f"{raw[:4]}-{raw[4:8]}-{raw[8:]}", "expires": now + ttl, "id": iid}

    def cancel_invite(self, invite_id: str | None = None, source: str | None = None) -> int:
        """Cancel one open invite (by its id from list()), or all of them (of one source). Returns how many."""
        with self.lock:
            d = self._load()
            self._prune(d)
            gone = [k for k, v in d["invites"].items()
                    if (invite_id is None or v.get("id") == invite_id)
                    and (source is None or (v.get("source") or "admin") == source)]
            for k in gone:
                del d["invites"][k]
            if gone:
                self._save(d)
        return len(gone)

    def join_wait(self, ip: str) -> float:
        rec = self._fails.get(ip)
        return max(0.0, rec[2] - self.clock()) if rec else 0.0

    def _join_failed(self, ip: str) -> None:
        now = self.clock()
        rec = self._fails.setdefault(ip, [0, now, 0.0])
        if now - rec[1] > JOIN_WINDOW and rec[2] <= now:
            rec[:] = [0, now, 0.0]
        rec[0] += 1
        if rec[0] >= JOIN_FAILS:
            rec[2] = max(rec[2], now + JOIN_LOCK)
        if len(self._fails) > FAILS_KEEP:
            # Forget the oldest addresses that are NOT locked: a spray of single failures from
            # throwaway addresses must not evict the lock on the one that kept guessing.
            free = sorted((k for k, r in self._fails.items() if r[2] <= now), key=lambda k: self._fails[k][1])
            for k in free[:max(len(free) // 2, len(self._fails) - FAILS_KEEP)]:
                self._fails.pop(k, None)
            if len(self._fails) > 4 * FAILS_KEEP:        # memory bound: then the locks ending soonest
                for k in sorted(self._fails, key=lambda k: self._fails[k][2])[:len(self._fails) - 4 * FAILS_KEEP]:
                    self._fails.pop(k, None)

    def join(self, code, name, device, ip: str = "?") -> tuple[str, dict, str]:
        """("ok", member, device_key) | ("locked"|"bad"|"name"|"full"|"taken", {}, message)."""
        with self.lock:
            if self.join_wait(ip) > 0:
                return "locked", {}, "Too many wrong codes. Wait 10 minutes."
            d = self._load()
            self._prune(d)
            key = _h(norm_code(code))
            inv = d["invites"].get(key)
            if not inv:
                self._join_failed(ip)
                return "bad", {}, "That code isn't valid or has expired. Ask for a new one."
            now = self.clock()
            mid = inv.get("member")
            if mid:
                m = d["members"].get(mid)
                if not m or m.get("removed"):
                    del d["invites"][key]; self._save(d)
                    return "bad", {}, "That invite is no longer valid."
                if len(_devices(m)) >= MAX_DEVICES:
                    return "bad", {}, f"At most {MAX_DEVICES} devices per member."
            else:
                if len(self._active(d)) >= MAX_MEMBERS:
                    return "full", {}, f"The family already has {MAX_MEMBERS} members. Ask the admin."
                # The name the admin put on the invite wins: the person joining can't pick another.
                nm = clean_name(inv.get("name")) or clean_name(name)
                if not nm:
                    return "name", {}, "Enter your name."
                others = dict(d, invites={k: v for k, v in d["invites"].items() if k != key})
                if self._name_taken(others, nm):
                    return "taken", {}, "Someone in the family already has that name. Add an initial or a nickname."
                mid = "m_" + secrets.token_hex(4)
                m = {"name": nm, "created": now, "devices": [], "radios": []}
                d["members"][mid] = m
            dev_key = secrets.token_urlsafe(32)
            m["devices"] = _devices(m) + [{"id": "d_" + secrets.token_hex(3), "label": clean_name(device, 40) or "device",
                                           "key": _h(dev_key), "created": now, "last_seen": now}]
            del d["invites"][key]                         # one use only
            self._save(d)
            self._fails.pop(ip, None)
            return "ok", self._public(mid, m), dev_key

    # ── authentication ──
    def auth_status(self, device_key) -> tuple[str, dict | None]:
        """("ok", member) | ("unknown", None): no such (or a revoked) key | ("unavailable", None):
        the list can't be read right now — the device must NOT forget its key over that."""
        if not isinstance(device_key, str) or not device_key or len(device_key) > 128:
            return "unknown", None
        want = _h(device_key)
        with self.lock:
            try:
                d = self._load()
            except StoreError:
                return "unavailable", None
            for mid, m in d["members"].items():
                if m.get("removed"):
                    continue
                for dev in _devices(m):
                    if isinstance(dev.get("key"), str) and hmac.compare_digest(dev["key"], want):
                        now = self.clock()
                        did = str(dev.get("id", ""))
                        if now - self._seen_write.get(did, 0) > 300:     # don't rewrite the file on every request
                            dev["last_seen"] = now; self._seen_write[did] = now
                            if len(self._seen_write) > 2000:
                                self._seen_write.clear()
                            try:
                                self._save(d)
                            except OSError:
                                pass                       # a full disk must not log everyone out
                        return "ok", {**self._public(mid, m), "device": did}
        return "unknown", None

    def auth(self, device_key) -> dict | None:
        return self.auth_status(device_key)[1]

    def leave(self, device_key) -> bool:
        if not isinstance(device_key, str) or not device_key:
            return False
        want = _h(device_key)
        with self.lock:
            d = self._load()
            for m in d["members"].values():
                devs = _devices(m)
                keep = [x for x in devs if not hmac.compare_digest(str(x.get("key", "")), want)]
                if len(keep) != len(devs):
                    m["devices"] = keep; self._save(d); return True
        return False

    # ── admin ──
    @staticmethod
    def _public(mid: str, m: dict) -> dict:
        return {"id": mid, "name": clean_name(m.get("name", "")), "removed": bool(m.get("removed")),
                "radios": _strs(m.get("radios"))}

    def list(self) -> dict:
        with self.lock:
            d = self._load()
            self._prune(d)
            members = [{**self._public(mid, m), "created": _num(m.get("created")),
                        "removed_at": _num(m.get("removed")) if m.get("removed") else None,
                        "devices": [{"id": str(x.get("id", "")), "label": clean_name(x.get("label", ""), 40),
                                     "last_seen": _num(x.get("last_seen"))} for x in _devices(m)]}
                       for mid, m in d["members"].items()]
            invites = [{"id": v.get("id"), "expires": _num(v.get("expires")), "name": clean_name(v.get("name", "")),
                        "member": v.get("member"), "source": v.get("source") or "admin"}
                       for v in d["invites"].values()]
        members.sort(key=lambda r: (r["removed"], r["name"].lower()))
        invites.sort(key=lambda r: r["expires"] or 0)
        return {"members": members, "invites": invites}

    def _edit(self, mid: str, fn) -> dict:
        with self.lock:
            d = self._load()
            m = d["members"].get(mid) if isinstance(mid, str) else None
            if not m:
                raise ValueError("no such member")
            fn(m, d)
            self._save(d)
            return self._public(mid, m)

    def remove(self, mid: str) -> dict:
        """Everything they had stops working at once; their posts stay, marked as from a removed member."""
        def f(m, d):
            m["removed"] = self.clock(); m["devices"] = []
            m["removed_radios"] = sorted(set(_strs(m.get("removed_radios"))) | set(_strs(m.get("radios"))))
            m["radios"] = []
            for k in [k for k, v in d["invites"].items() if isinstance(v, dict) and v.get("member") == mid]:
                del d["invites"][k]
        return self._edit(mid, f)

    def rename(self, mid: str, name: str) -> dict:
        nm = clean_name(name)
        if not nm:
            raise ValueError("name required")

        def f(m, d):
            if self._name_taken(d, nm, except_mid=mid):
                raise ValueError("someone already has that name (or one that looks just like it)")
            m["name"] = nm
        return self._edit(mid, f)

    def revoke_device(self, mid: str, dev_id: str) -> dict:
        return self._edit(mid, lambda m, d: m.__setitem__("devices", [x for x in _devices(m) if x.get("id") != dev_id]))

    def add_radio(self, mid: str, node) -> dict:
        node = str(node).strip().lower() if isinstance(node, str) else ""
        if not NODE_RE.match(node):
            raise ValueError("radio IDs look like !a1b2c3d4 (shown in the Meshtastic app)")

        def f(m, d):
            if m.get("removed"):
                raise ValueError("member was removed")
            for other in d["members"].values():
                if other is not m and node in _strs(other.get("radios")):
                    raise ValueError("that radio belongs to another member")
            radios = _strs(m.get("radios"))
            if node not in radios:
                if len(radios) >= MAX_RADIOS:
                    raise ValueError(f"at most {MAX_RADIOS} radios per member")
                radios.append(node)
            m["radios"] = radios
        return self._edit(mid, f)

    def remove_radio(self, mid: str, node) -> dict:
        node = str(node).strip().lower()
        return self._edit(mid, lambda m, d: m.__setitem__("radios", [r for r in _strs(m.get("radios")) if r != node]))


def radio_registry(path: Path) -> dict[str, dict]:
    """For meshbridge: radio ID → {"member", "name", "removed"} (read-only, no lock needed).
    An unreadable file gives {}: then no radio is known, which only means no names on posts."""
    try:
        d = json.loads(Path(path).read_bytes())
    except (OSError, ValueError, RecursionError):
        return {}
    out: dict[str, dict] = {}
    mems = d.get("members") if isinstance(d, dict) else None
    for mid, m in (mems.items() if isinstance(mems, dict) else []):
        if not isinstance(m, dict):
            continue
        name = clean_name(m.get("name", ""))
        for r in _strs(m.get("radios")):
            out[r.lower()] = {"member": mid, "name": name, "removed": False}
        for r in _strs(m.get("removed_radios")):
            out.setdefault(r.lower(), {"member": mid, "name": name, "removed": True})
    return out
