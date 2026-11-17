"""
Aid-station log: who came to the neighbourhood aid station and what help they got.

The records are sensitive (names, health needs), so only these people can read or write them:
  * the Badger's admin (admin PIN) and aid leads (a volunteer the admin marks as lead):
    every record;
  * aid staff: volunteers the admin appoints with a one-time code (24 h, single use). A
    volunteer sees, edits and exports only the records THEY wrote, plus today's station-wide
    counts (the summary). A volunteer at the table can't read out everyone's visits and needs.
    Each device that redeems a code gets its own random key (kept in that browser, sent as the
    X-Aid-Token header). Only a SHA-256 of the key is stored; codes are stored as a salted
    scrypt hash. The admin can revoke one volunteer or everyone at once ("end of shift").
    Wrong codes lock an address out for 10 min.
Volunteers are often not family members, so this is separate from members.py (same design).

Records are deleted automatically after N days (default 30, admin can change it). CSV export
for staff and admin; every export is noted in a short audit list the admin can see.
An optional public status line ("Aid station open · water, food") is set by staff and shown
to everyone; it says nothing about who came.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import importlib.util
import io
import re
import secrets
import threading
import time
from pathlib import Path


def _load_common():
    path = Path(__file__).resolve().with_name("_community_common.py")
    spec = importlib.util.spec_from_file_location(f"{__name__}_common", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load_common()

FEATURE = {"title": "Aid station", "icon": "⛑️", "group": "community", "route": "/aid",
           "script": "/features/aidstation.js", "access": "public",
           "description": "Station status for everyone · visit log for aid volunteers"}

HELP = ("water", "food", "first_aid", "shelter", "info", "other")
STATUS_ITEMS = ("water", "food", "first_aid", "shelter", "charging", "info")
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"         # no 0/O, 1/I/L, U
MAX_CODES = 20
JOIN_FAILS, JOIN_WINDOW, JOIN_LOCK = 5, 600.0, 600.0
TOKEN_HEADER = "X-Aid-Token"
AUDIT_KEEP = 200


def _now() -> float:
    return time.time()


_write_limit = C.RateLimit(120, 600, clock=lambda: _now())      # per volunteer: a busy station, not a script
_status_limit = C.RateLimit(30, 600, clock=lambda: _now())
_stores: dict[str, dict] = {}
_stores_lock = threading.Lock()
_last_purge = [0.0]


def _reset_limits() -> None:
    _write_limit.clear(); _status_limit.clear(); _last_purge[0] = 0.0
    with _stores_lock:
        for s in _stores.values():
            s["staff"].fails.clear()


def norm_code(code) -> str:
    return re.sub(r"[\s\-_.]", "", str(code or "")).upper()[:32]


CODE_KDF = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32, "maxmem": 64 * 1024 * 1024}


def code_hash(code: str, salt: str) -> str:
    """A volunteer code as stored: scrypt with the file's salt (slow: a stolen file can't be brute-forced
    while the codes are still valid)."""
    return "s1$" + hashlib.scrypt(code.encode("utf-8", "surrogatepass"), salt=salt.encode(), **CODE_KDF).hex()


# ─────────────────────────────── staff codes ─────────────────────────────────

class Staff:
    """Aid volunteers: one record per device. Modelled on members.Members."""

    def __init__(self, path: Path):
        self.store = C.JsonStore(path, lambda: {"v": 1, "staff": {}, "codes": {}})
        self.fails: dict[str, list[float]] = {}
        self.flock = threading.Lock()
        self._seen: dict[str, float] = {}

    @staticmethod
    def _prune(d: dict, now: float) -> bool:
        dead = [k for k, v in d["codes"].items() if not isinstance(v, dict) or v.get("expires", 0) <= now]
        for k in dead:
            del d["codes"][k]
        return bool(dead)

    def salt(self) -> str:
        """The file's code salt (made once)."""
        s = self.store.view(lambda d: d.get("code_salt"))
        if isinstance(s, str) and re.fullmatch(r"[0-9a-f]{32}", s):
            return s

        def f(d):
            cur = d.get("code_salt")
            if isinstance(cur, str) and re.fullmatch(r"[0-9a-f]{32}", cur):
                return cur, False
            d["code_salt"] = secrets.token_hex(16)
            return d["code_salt"], True
        return self.store.update(f)

    def create_code(self, name: str, ttl: float, max_staff: int, lead: bool = False) -> dict:
        nm = C.clean_text(name, 40, label="Volunteer name")
        raw = "".join(secrets.choice(ALPHABET) for _ in range(10))
        now = _now()
        hashed = code_hash(norm_code(raw), self.salt())

        def f(d):
            self._prune(d, now)
            if len(d["staff"]) + len(d["codes"]) >= max_staff:
                raise C.FieldError(f"At most {max_staff} volunteer devices (including open codes). Revoke some first.")
            if len(d["codes"]) >= MAX_CODES:
                raise C.FieldError("Too many open codes: cancel them or wait for them to expire.")
            d["codes"][hashed] = {"expires": now + ttl, "created": now, "name": nm, "lead": bool(lead)}
            return None, True
        self.store.update(f)
        return {"code": f"{raw[:4]}-{raw[4:8]}-{raw[8:]}", "expires": now + ttl, "name": nm, "lead": bool(lead)}

    def join_wait(self, ip: str) -> float:
        with self.flock:
            rec = self.fails.get(ip)
            return max(0.0, rec[2] - _now()) if rec else 0.0

    def _failed(self, ip: str) -> None:
        with self.flock:
            now = _now()
            rec = self.fails.setdefault(ip, [0, now, 0.0])
            if now - rec[1] > JOIN_WINDOW:
                rec[:] = [0, now, 0.0]
            rec[0] += 1
            if rec[0] >= JOIN_FAILS:
                rec[2] = now + JOIN_LOCK
            if len(self.fails) > 1000:
                for k in sorted(self.fails, key=lambda k: self.fails[k][1])[:500]:
                    self.fails.pop(k, None)

    def join(self, code, name, device, ip: str):
        """("ok", {"id","name"}, key) | ("locked"|"bad"|"name", {}, message)."""
        if self.join_wait(ip) > 0:
            return "locked", {}, "Too many wrong codes. Wait 10 minutes and try again."
        try:
            nm_in = C.clean_text(name, 40, label="Your name")
            dev = C.clean_text(device, 40, label="This device") or "device"
        except C.FieldError as e:
            return "name", {}, str(e)
        code = norm_code(code)
        salt = self.store.view(lambda d: d.get("code_salt"))
        key = code_hash(code, salt) if isinstance(salt, str) and salt else None     # (slow: outside the lock)
        legacy = C.hash_token(code)                    # a code made before codes were salted (lives ≤ 24 h)
        now = _now()

        def f(d):
            changed = self._prune(d, now)
            k = key if key and key in d["codes"] else (legacy if legacy in d["codes"] else None)
            inv = d["codes"].get(k) if k else None
            if not inv:
                return ("bad", {}, "That code isn't valid or has expired. Ask the Badger's owner for a new one."), changed
            nm = inv.get("name") or nm_in
            if not nm:
                return ("name", {}, "Enter your name."), changed
            dev_key = secrets.token_urlsafe(32)
            sid = "s_" + secrets.token_hex(5)
            d["staff"][sid] = {"name": nm, "device": dev, "key": C.hash_token(dev_key), "created": now, "last_seen": now,
                               "lead": inv.get("lead") is True}
            del d["codes"][k]                          # one use only
            return ("ok", {"id": sid, "name": nm, "lead": inv.get("lead") is True}, dev_key), True
        res = self.store.update(f)
        if res[0] == "bad":
            self._failed(ip)
        elif res[0] == "ok":
            with self.flock:
                self.fails.pop(ip, None)
        return res

    def auth(self, raw) -> dict | None:
        if not isinstance(raw, str) or not raw or len(raw) > 128:
            return None
        want = C.hash_token(raw)

        def find(d):
            for sid, s in d["staff"].items():
                if isinstance(s, dict) and hmac.compare_digest(str(s.get("key", "")), want):
                    return sid, s.get("name", ""), s.get("lead") is True
            return None
        hit = self.store.view(find)
        if not hit:
            return None
        sid, name, lead = hit
        now = _now()
        if now - self._seen.get(sid, 0) > 300:          # don't rewrite the file on every request
            self._seen[sid] = now

            def seen(d):
                s = d["staff"].get(sid)
                if s:
                    s["last_seen"] = now
                return None, bool(s)
            self.store.update(seen)
        return {"id": sid, "name": name, "lead": lead}

    def leave(self, raw) -> bool:
        me = self.auth(raw)
        return bool(me) and self.revoke(me["id"])

    def revoke(self, sid: str) -> bool:
        def f(d):
            if sid in d["staff"]:
                del d["staff"][sid]
                return True, True
            return False, False
        return self.store.update(f)

    def set_lead(self, sid: str, lead: bool) -> bool:
        def f(d):
            s = d["staff"].get(sid)
            if not isinstance(s, dict):
                return False, False
            s["lead"] = bool(lead)
            return True, True
        return self.store.update(f)

    def revoke_all(self) -> int:
        def f(d):
            n = len(d["staff"]) + len(d["codes"])
            d["staff"], d["codes"] = {}, {}
            return n, n > 0
        return self.store.update(f)

    def list(self) -> dict:
        now = _now()

        def f(d):
            staff = [{"id": sid, "name": s.get("name", ""), "device": s.get("device", ""),
                      "created": s.get("created"), "last_seen": s.get("last_seen"), "lead": s.get("lead") is True}
                     for sid, s in d["staff"].items() if isinstance(s, dict)]
            codes = [{"name": v.get("name", ""), "expires": v.get("expires"), "lead": v.get("lead") is True}
                     for v in d["codes"].values() if isinstance(v, dict) and v.get("expires", 0) > now]
            return staff, codes
        staff, codes = self.store.view(f)
        staff.sort(key=lambda s: (s["name"].lower(), s["created"] or 0))
        return {"staff": staff, "codes": codes}


# ─────────────────────────────── storage ─────────────────────────────────────

def _s(ctx) -> dict:
    d = ctx.data_dir()
    key = str(d)
    with _stores_lock:
        if key not in _stores:
            _stores[key] = {
                "staff": Staff(d / "staff.json"),
                "log": C.JsonStore(d / "log.json", lambda: {"v": 1, "entries": []}),
                "settings": C.JsonStore(d / "settings.json", lambda: {"v": 1, "audit": []}),
                "status": C.JsonStore(d / "status.json", lambda: {"v": 1}),
            }
        return _stores[key]


def retention_days(ctx) -> int:
    over = _s(ctx)["settings"].view(lambda d: d.get("retention_days"))
    if isinstance(over, int) and not isinstance(over, bool) and 1 <= over <= 365:
        return over
    return C.cfg_int(ctx.cfg(), "retention_days", 30, 1, 365)


def purge(ctx, force: bool = False) -> int:
    """Delete records older than the retention period. Cheap; runs at most once a minute
    from requests, and hourly from a background thread."""
    now = _now()
    if not force and now - _last_purge[0] < 60:
        return 0
    _last_purge[0] = now
    cutoff = now - retention_days(ctx) * 86400

    def f(d):
        keep = [e for e in d["entries"] if isinstance(e, dict) and isinstance(e.get("at"), (int, float)) and e["at"] >= cutoff]
        n = len(d["entries"]) - len(keep)
        if n:
            d["entries"] = keep
        return n, n > 0
    n = _s(ctx)["log"].update(f)
    if n:
        ctx.log(f"retention: deleted {n} record(s) older than {retention_days(ctx)} days")
    return n


def setup(ctx):
    def loop():
        while True:
            try:
                purge(ctx, force=True)
            except Exception as e:  # noqa: BLE001 — keep the thread alive; it retries next hour
                ctx.log(f"retention purge failed: {type(e).__name__}")
            time.sleep(3600)
    threading.Thread(target=loop, name="aidstation-purge", daemon=True).start()


# ─────────────────────────────── access ──────────────────────────────────────

def _staff_of(req, ctx) -> dict | None:
    tok = (req.headers.get(TOKEN_HEADER) or "").strip()
    return _s(ctx)["staff"].auth(tok) if tok else None


def can_use(req, ctx) -> bool:
    """Route access: the admin, or a device with a valid aid-staff key."""
    return bool(req.is_admin()) or _staff_of(req, ctx) is not None


def _who(req, ctx, args) -> dict | None:
    """{"kind": "staff"|"lead"|"admin", "id", "name", "all": sees every record?}"""
    st = _staff_of(req, ctx)
    if st:
        lead = st.get("lead") is True
        return {"kind": "lead" if lead else "staff", "id": st["id"], "name": st["name"], "all": lead}
    if args.get("admin"):
        return {"kind": "admin", "id": "admin", "name": "Admin", "all": True}
    return None


def _mine(e: dict, who: dict) -> bool:
    """May `who` see/change this record? Admin and leads: all. A volunteer: only the ones they wrote
    (by their device id; older records without one belong to no volunteer)."""
    return bool(who.get("all")) or (isinstance(e, dict) and e.get("by_id") == who["id"])


def _one(args, key, default=""):
    v = (args.get("qs") or {}).get(key) or [default]
    return v[0] if isinstance(v, list) else default


def _at(e: dict) -> float:
    v = e.get("at")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _num(s, default=None):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return default
    return v if v == v and abs(v) < 1e11 else default      # NaN / absurd values: ignore


# ─────────────────────────────── handlers ────────────────────────────────────

def public_status(ctx) -> dict:
    st = _s(ctx)["status"].view(dict)
    if not st.get("updated"):
        return {"set": False}
    hours = C.cfg_int(ctx.cfg(), "status_hours", 12, 1, 168)
    return {"set": True, "open": bool(st.get("open")), "items": [i for i in st.get("items", []) if i in STATUS_ITEMS],
            "text": str(st.get("text", ""))[:100], "updated": st.get("updated"),
            "stale": _now() - float(st.get("updated") or 0) > hours * 3600}


def h_me(req, ctx, args):
    tok = (req.headers.get(TOKEN_HEADER) or "").strip()
    st = _staff_of(req, ctx)
    return {"staff": ({"name": st["name"], "lead": True} if st.get("lead") else {"name": st["name"]}) if st else None,
            "admin": bool(args["admin"]),
            "token_invalid": bool(tok) and not st, "status": public_status(ctx)}


def h_status(req, ctx, args):
    return public_status(ctx)


def h_status_set(req, ctx, args):
    who = _who(req, ctx, args)
    if not who:
        return 401, {"error": "aid staff only"}
    d = args["data"] or {}
    try:
        is_open = C.clean_bool(d.get("open"), "open")
        items = d.get("items") or []
        if not isinstance(items, list) or any(i not in STATUS_ITEMS for i in items):
            raise C.FieldError("items: unknown item")
        text = C.clean_text(d.get("text"), 100, label="Note")
    except C.FieldError as e:
        return 400, {"error": str(e)}
    wait = _status_limit.check(who["id"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}
    rec = {"v": 1, "open": is_open, "items": [i for i in STATUS_ITEMS if i in items], "text": text,
           "updated": _now(), "by": who["name"]}

    def f(st):
        st.clear(); st.update(rec)
        return None, True
    _s(ctx)["status"].update(f)
    return {"ok": True, "status": public_status(ctx)}


def h_join(req, ctx, args):
    d = args["data"] or {}
    staff = _s(ctx)["staff"]
    st, me, key = staff.join(d.get("code") if isinstance(d.get("code"), str) else "",
                             d.get("name") if isinstance(d.get("name"), str) else "",
                             d.get("device") if isinstance(d.get("device"), str) else "", args["ip"])
    if st == "ok":
        ctx.log("an aid volunteer device joined")
        return {"ok": True, "key": key, "staff": {"name": me["name"]}}
    code = {"locked": 429, "name": 400}.get(st, 403)
    return code, {"ok": False, "error": key}, ({"Retry-After": "600"} if st == "locked" else {})


def h_leave(req, ctx, args):
    tok = (req.headers.get(TOKEN_HEADER) or "").strip()
    return {"ok": bool(tok) and _s(ctx)["staff"].leave(tok)}


def _clean_entry(d: dict, who: dict, ctx, partial: bool = False) -> dict:
    out = {}

    def has(k):
        return not partial or k in d
    if has("person"):
        out["person"] = C.clean_text(d.get("person"), 80, label="Person", required=True)
    if has("count"):
        out["count"] = C.clean_int(d.get("count"), 1, 99, "People", default=1)
    if has("help"):
        hl = d.get("help") if d.get("help") is not None else []
        if not isinstance(hl, list) or len(hl) > len(HELP) or any(h not in HELP for h in hl):
            raise C.FieldError("Help given: unknown type")
        out["help"] = [h for h in HELP if h in hl]
    if has("needs"):
        out["needs"] = C.clean_text(d.get("needs"), 300, multiline=True, label="Needs")
    if has("notes"):
        out["notes"] = C.clean_text(d.get("notes"), 1000, multiline=True, label="Notes")
    if has("followup"):
        out["followup"] = C.clean_bool(d.get("followup"), "Follow-up")
    if has("followup_note"):
        out["followup_note"] = C.clean_text(d.get("followup_note"), 200, label="Follow-up note")
    if has("handled_by"):
        out["handled_by"] = C.clean_text(d.get("handled_by"), 40, label="Handled by") or who["name"]
    if "at" in d and d.get("at") is not None:
        at = d.get("at")
        now = _now()
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not (now - retention_days(ctx) * 86400 <= at <= now + 300):
            raise C.FieldError("Time: must be within the retention period and not in the future")
        out["at"] = float(at)
    return out


def _public_entry(e: dict) -> dict:
    return {k: e.get(k) for k in ("id", "at", "person", "count", "help", "needs", "notes", "followup",
                                  "followup_note", "followup_done", "handled_by", "by", "updated")}


def h_entries(req, ctx, args):
    who = _who(req, ctx, args)
    if not who:
        return 401, {"error": "aid staff only"}
    purge(ctx)
    q = _one(args, "q")[:100]
    since = _num(_one(args, "since"), None)
    until = _num(_one(args, "until"), None)
    fu = _one(args, "followup")
    limit = int(_num(_one(args, "limit"), 300) or 300)
    limit = max(1, min(1000, limit))

    def f(d):
        out = []
        for e in reversed(d["entries"]):
            if not isinstance(e, dict) or not _mine(e, who):
                continue
            if since is not None and _at(e) < since:
                continue
            if until is not None and _at(e) >= until:
                continue
            if fu == "open" and not (e.get("followup") and not e.get("followup_done")):
                continue
            if q and not C.match(q, e.get("person"), e.get("needs"), e.get("notes"), e.get("handled_by"),
                                 e.get("followup_note"), " ".join(str(h) for h in (e.get("help") if isinstance(e.get("help"), list) else []))):
                continue
            out.append(_public_entry(e))
        return out
    found = _s(ctx)["log"].view(f)
    return {"entries": found[:limit], "total": len(found), "retention_days": retention_days(ctx),
            "scope": "all" if who["all"] else "mine"}


def h_save(req, ctx, args):
    who = _who(req, ctx, args)
    if not who:
        return 401, {"error": "aid staff only"}
    d = args["data"] or {}
    eid = d.get("id")
    try:
        if eid is not None and not C.valid_id(eid):
            raise C.FieldError("no such record")
        fields = _clean_entry(d, who, ctx, partial=eid is not None)
    except C.FieldError as e:
        return 400, {"error": str(e)}
    wait = _write_limit.check(who["id"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}
    purge(ctx)
    now = _now()
    cap = C.cfg_int(ctx.cfg(), "max_entries", 10000, 100, 100000)

    def f(log):
        if eid is None:
            if len(log["entries"]) >= cap:
                return (507, {"error": f"The log is full ({cap} records). Export it, then lower the retention "
                                       "period or ask the admin to raise max_entries."}), False
            e = {"id": C.new_id(), "at": now, "count": 1, "help": [], "needs": "", "notes": "", "followup": False,
                 "followup_note": "", "followup_done": None, "handled_by": who["name"], "by": who["name"],
                 "by_id": who["id"], "created": now, "updated": now}
            e.update(fields)
            log["entries"].append(e)
            log["entries"].sort(key=lambda x: _at(x) if isinstance(x, dict) else 0.0)
            return (200, {"ok": True, "entry": _public_entry(e)}), True
        for e in log["entries"]:
            if isinstance(e, dict) and e.get("id") == eid and _mine(e, who):
                e.update(fields)
                e["updated"], e["edited_by"] = now, who["name"]
                if "followup" in fields and not fields["followup"]:
                    e["followup_done"] = None
                if "at" in fields:
                    log["entries"].sort(key=lambda x: _at(x) if isinstance(x, dict) else 0.0)
                return (200, {"ok": True, "entry": _public_entry(e)}), True
        return (404, {"error": "no such record (it may have been deleted)"}), False
    return _s(ctx)["log"].update(f)


def h_followup(req, ctx, args):
    who = _who(req, ctx, args)
    if not who:
        return 401, {"error": "aid staff only"}
    d = args["data"] or {}
    if not C.valid_id(d.get("id")) or not isinstance(d.get("done"), bool):
        return 400, {"error": "send id and done (true/false)"}
    wait = _write_limit.check(who["id"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}

    def f(log):
        for e in log["entries"]:
            if isinstance(e, dict) and e.get("id") == d["id"] and _mine(e, who):
                if not e.get("followup"):
                    return (400, {"error": "that record has no follow-up"}), False
                e["followup_done"] = {"at": _now(), "by": who["name"]} if d["done"] else None
                e["updated"] = _now()
                return (200, {"ok": True, "entry": _public_entry(e)}), True
        return (404, {"error": "no such record"}), False
    return _s(ctx)["log"].update(f)


def h_delete(req, ctx, args):
    who = _who(req, ctx, args)
    if not who:
        return 401, {"error": "aid staff only"}
    d = args["data"] or {}
    if not C.valid_id(d.get("id")):
        return 400, {"error": "no such record"}

    def f(log):
        keep = [e for e in log["entries"] if not (isinstance(e, dict) and e.get("id") == d["id"] and _mine(e, who))]
        if len(keep) == len(log["entries"]):
            return (404, {"error": "no such record"}), False
        log["entries"] = keep
        return (200, {"ok": True}), True
    res = _s(ctx)["log"].update(f)
    if res[0] == 200:
        _audit(ctx, who, "deleted a record")
    return res


def h_summary(req, ctx, args):
    """Counts only (no names). A volunteer gets today's (since local midnight); admin and leads any period."""
    who = _who(req, ctx, args)
    if not who:
        return 401, {"error": "aid staff only"}
    purge(ctx)
    since = _num(_one(args, "since"), None) if who["all"] else None
    if since is None:
        lt = time.localtime(_now())
        since = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

    def f(log):
        counts = {h: 0 for h in HELP}
        visits = people = fu_open = 0
        for e in log["entries"]:
            if not isinstance(e, dict):
                continue
            if e.get("followup") and not e.get("followup_done"):
                fu_open += 1
            if _at(e) < since:
                continue
            visits += 1
            c = e.get("count")
            people += c if isinstance(c, int) and not isinstance(c, bool) and 0 < c < 100 else 1
            for h in e.get("help") if isinstance(e.get("help"), list) else []:
                if isinstance(h, str) and h in counts:
                    counts[h] += 1
        return {"since": since, "visits": visits, "people": people, "help": counts,
                "followups_open": fu_open, "total_records": len(log["entries"])}
    out = {**_s(ctx)["log"].view(f), "retention_days": retention_days(ctx)}
    if not who["all"]:
        out.pop("total_records", None)
        out["scope"] = "today"
    return out


def _audit(ctx, who: dict, what: str) -> None:
    def f(st):
        st["audit"] = (st.get("audit") or [])[-(AUDIT_KEEP - 1):] + [{"at": _now(), "who": who["name"], "what": what}]
        return None, True
    _s(ctx)["settings"].update(f)


def h_export(req, ctx, args):
    who = _who(req, ctx, args)
    if not who:
        return 401, {"error": "aid staff only"}
    purge(ctx)
    since = _num(_one(args, "since"), None)
    rows = _s(ctx)["log"].view(lambda log: [_public_entry(e) for e in log["entries"]
                                            if isinstance(e, dict) and _mine(e, who)
                                            and (since is None or _at(e) >= since)])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "person", "people", "help given", "needs", "notes", "follow-up needed",
                "follow-up note", "follow-up done", "handled by", "recorded by"])

    def ts(t):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if isinstance(t, (int, float)) else ""
    for e in rows:
        done = e.get("followup_done") or {}
        w.writerow([C.csv_cell(x) for x in (
            ts(e.get("at")), e.get("person"), e.get("count"), " ".join(str(h) for h in (e.get("help") if isinstance(e.get("help"), list) else [])), e.get("needs"),
            e.get("notes"), "yes" if e.get("followup") else "", e.get("followup_note"),
            (ts(done.get("at")) + " " + str(done.get("by", ""))).strip() if isinstance(done, dict) and done else "",
            e.get("handled_by"), e.get("by"))])
    _audit(ctx, who, f"exported {len(rows)} record(s)" + ("" if who["all"] else " (their own)"))
    name = time.strftime("aid-station-%Y%m%d-%H%M.csv", time.localtime(_now()))
    ctx.send_bytes(req, ("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{name}"'})
    return None


# ── admin ──

def h_admin_get(req, ctx, args):
    return {**_s(ctx)["staff"].list(), "retention_days": retention_days(ctx),
            "retention_default": C.cfg_int(ctx.cfg(), "retention_days", 30, 1, 365),
            "audit": list(reversed(_s(ctx)["settings"].view(lambda d: list(d.get("audit") or []))))[:50],
            "code_hours": C.cfg_int(ctx.cfg(), "code_hours", 24, 1, 168)}


def h_admin_post(req, ctx, args):
    d = args["data"] or {}
    op = d.get("op")
    staff = _s(ctx)["staff"]
    admin = {"name": "Admin"}
    try:
        if op == "invite":
            ttl = C.cfg_int(ctx.cfg(), "code_hours", 24, 1, 168) * 3600
            lead = d.get("lead") is True
            res = staff.create_code(d.get("name") if isinstance(d.get("name"), str) else "", ttl,
                                    C.cfg_int(ctx.cfg(), "max_staff", 30, 1, 200), lead=lead)
            if lead:
                _audit(ctx, admin, "made an aid-lead code (sees every record)")
            return {"ok": True, **res}
        if op == "set_lead":
            sid, lead = d.get("id"), d.get("lead")
            if not isinstance(sid, str) or not isinstance(lead, bool):
                return 400, {"error": "send id and lead (true/false)"}
            if not staff.set_lead(sid, lead):
                return 404, {"error": "no such volunteer"}
            _audit(ctx, admin, "made a volunteer aid lead" if lead else "ended a volunteer's aid-lead role")
            return {"ok": True}
        if op == "revoke":
            sid = d.get("id")
            if not isinstance(sid, str) or not staff.revoke(sid):
                return 404, {"error": "no such volunteer"}
            _audit(ctx, admin, "revoked a volunteer device")
            return {"ok": True}
        if op == "revoke_all":
            n = staff.revoke_all()
            _audit(ctx, admin, f"revoked all volunteers ({n})")
            return {"ok": True, "revoked": n}
        if op == "retention":
            days = C.clean_int(d.get("days"), 1, 365, "Days")

            def f(st):
                st["retention_days"] = days
                return None, True
            _s(ctx)["settings"].update(f)
            _audit(ctx, admin, f"set retention to {days} days")
            return {"ok": True, "retention_days": days, "deleted": purge(ctx, force=True)}
        if op == "wipe":
            if d.get("confirm") != "DELETE":
                return 400, {"error": 'type DELETE to confirm'}

            def w(log):
                n = len(log["entries"])
                log["entries"] = []
                return n, True
            n = _s(ctx)["log"].update(w)
            _audit(ctx, admin, f"deleted all {n} record(s)")
            return {"ok": True, "deleted": n}
    except C.FieldError as e:
        return 400, {"error": str(e)}
    return 400, {"error": "unknown op"}


ROUTES = [
    {"method": "GET", "path": "/api/aidstation/me", "handler": h_me, "access": "public"},
    {"method": "GET", "path": "/api/aidstation/status", "handler": h_status, "access": "public"},
    {"method": "POST", "path": "/api/aidstation/status", "handler": h_status_set, "access": can_use},
    {"method": "POST", "path": "/api/aidstation/join", "handler": h_join, "access": "public"},
    {"method": "POST", "path": "/api/aidstation/leave", "handler": h_leave, "access": "public"},
    {"method": "GET", "path": "/api/aidstation/entries", "handler": h_entries, "access": can_use},
    {"method": "POST", "path": "/api/aidstation/entry", "handler": h_save, "access": can_use},
    {"method": "POST", "path": "/api/aidstation/followup", "handler": h_followup, "access": can_use},
    {"method": "POST", "path": "/api/aidstation/delete", "handler": h_delete, "access": can_use},
    {"method": "GET", "path": "/api/aidstation/summary", "handler": h_summary, "access": can_use},
    {"method": "GET", "path": "/api/aidstation/export.csv", "handler": h_export, "access": can_use},
    {"method": "GET", "path": "/api/aidstation/admin", "handler": h_admin_get, "access": "admin"},
    {"method": "POST", "path": "/api/aidstation/admin", "handler": h_admin_post, "access": "admin"},
]
