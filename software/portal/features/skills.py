"""
Skills & resources directory: neighbours list what they can offer (a nurse, an electrician, a
generator, well water…) and what they need. Anyone on the Badger's Wi-Fi can browse, search
and add an entry; whoever adds one gets an edit token (kept in their browser) to change, renew
or remove it; the admin can hide or remove anything.

Everything here is visible to anyone on the Wi-Fi, so the page asks people to share only what
they're comfortable with. Abuse limits: per-address rate limits and entry caps, length limits
on every field, a total cap, and entries expire unless renewed (default 90 days).
"""
from __future__ import annotations

import importlib.util
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

FEATURE = {"title": "Skills & resources", "icon": "🤝", "group": "community", "route": "/skills",
           "script": "/features/skills.js", "access": "public",
           "description": "Who nearby can help — and who needs help"}

KINDS = ("offer", "need")
CATEGORIES = ("medical", "trades", "radio", "care", "language", "food", "water", "power", "tools",
              "transport", "shelter", "other")
FIELDS = {"title": 80, "name": 40, "contact": 200, "availability": 120, "details": 500}
MAX_FLAGS = 20


def _now() -> float:
    return time.time()


def _mono() -> float:
    """Rate-limit windows and lockouts (in memory): a wall-clock jump must not stretch or skip them."""
    return time.monotonic()


_add_limit = C.RateLimit(5, 600, clock=lambda: _mono())
_edit_limit = C.RateLimit(30, 600, clock=lambda: _mono())
_flag_limit = C.RateLimit(10, 600, clock=lambda: _mono())
_stores: dict[str, C.JsonStore] = {}
_stores_lock = threading.Lock()


def _reset_limits() -> None:
    for lim in (_add_limit, _edit_limit, _flag_limit):
        lim.clear()


def _store(ctx) -> C.JsonStore:
    key = str(ctx.data_dir())
    with _stores_lock:
        if key not in _stores:
            _stores[key] = C.JsonStore(ctx.data_dir() / "skills.json", lambda: {"v": 1, "entries": []})
        return _stores[key]


def _cfg(ctx):
    c = ctx.cfg()
    return {"max": C.cfg_int(c, "max_entries", 500, 10, 5000), "per_ip": C.cfg_int(c, "per_ip", 10, 1, 100),
            "expire": C.cfg_int(c, "expire_days", 90, 0, 3650), "adds": C.cfg_int(c, "adds_per_10min", 5, 1, 100)}


def _expired(e: dict, days: int, now: float) -> bool:
    return days > 0 and now - float(e.get("updated") or 0) > days * 86400


def _purge(d: dict, days: int, now: float) -> bool:
    keep = [e for e in d["entries"] if isinstance(e, dict) and not _expired(e, days, now)]
    if len(keep) != len(d["entries"]):
        d["entries"] = keep
        return True
    return False


def _clean(data: dict, partial: bool = False) -> dict:
    out = {}
    if not partial or "kind" in data:
        out["kind"] = C.clean_choice(data.get("kind"), KINDS, "Offer or need", default="offer")
    if not partial or "category" in data:
        out["category"] = C.clean_choice(data.get("category"), CATEGORIES, "Category", default="other")
    for k, n in FIELDS.items():
        if not partial or k in data:
            out[k] = C.clean_text(data.get(k), n, multiline=(k == "details"),
                                  label={"title": "What", "name": "Name", "contact": "Where / how to find you",
                                         "availability": "When", "details": "Details"}[k],
                                  required=(k == "title"))
    return out


def _public(e: dict, admin: bool) -> dict:
    out = {k: e.get(k) for k in ("id", "kind", "category", *FIELDS, "created", "updated")}
    if admin:
        out["hidden"] = bool(e.get("hidden"))
        out["flags"] = len(e.get("flags") or [])
    return out


def _one(args, key, default=""):
    v = (args.get("qs") or {}).get(key) or [default]
    return str(v[0])[:100] if isinstance(v, list) else default


# ─────────────────────────────── handlers ────────────────────────────────────

def h_list(req, ctx, args):
    admin = bool(args["admin"])
    q, kind, cat = _one(args, "q"), _one(args, "kind"), _one(args, "category")
    cfg = _cfg(ctx)
    now = _now()
    st = _store(ctx)
    if st.view(lambda d: any(isinstance(e, dict) and _expired(e, cfg["expire"], now) for e in d["entries"])):
        st.update(lambda d: (None, _purge(d, cfg["expire"], now)))

    def f(d):
        out = []
        for e in reversed(d["entries"]):
            if not isinstance(e, dict) or (e.get("hidden") and not admin):
                continue
            if kind in KINDS and e.get("kind") != kind:
                continue
            if cat in CATEGORIES and e.get("category") != cat:
                continue
            if q and not C.match(q, e.get("title"), e.get("name"), e.get("details"), e.get("availability"),
                                 e.get("contact"), e.get("category")):
                continue
            out.append(_public(e, admin))
        return out
    return {"entries": st.view(f), "categories": list(CATEGORIES), "expire_days": cfg["expire"]}


def h_add(req, ctx, args):
    try:
        fields = _clean(args["data"] or {})
    except C.FieldError as e:
        return 400, {"error": str(e)}
    cfg = _cfg(ctx)
    ip = args["ip"]
    wait = _add_limit.check(ip, cfg["adds"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}
    tag = C.ip_tag(ctx.data_dir(), ip)
    raw, hashed = C.new_token()
    now = _now()

    def f(d):
        changed = _purge(d, cfg["expire"], now)
        if sum(1 for e in d["entries"] if isinstance(e, dict) and e.get("ipt") == tag) >= cfg["per_ip"]:
            return (429, {"error": f"At most {cfg['per_ip']} entries from one device. Edit or remove one of yours first."}), changed
        if len(d["entries"]) >= cfg["max"]:
            return (507, {"error": "The directory is full. Ask the Badger's owner to tidy it up."}), changed
        e = {"id": C.new_id(), **fields, "created": now, "updated": now, "tok": hashed, "ipt": tag, "flags": []}
        d["entries"].append(e)
        return (200, {"ok": True, "id": e["id"], "token": raw, "entry": _public(e, False)}), True
    return _store(ctx).update(f)


def _find(d, eid):
    for e in d["entries"]:
        if isinstance(e, dict) and e.get("id") == eid:
            return e
    return None


def _owner_or_admin(e, data, args) -> bool:
    return bool(args["admin"]) or C.token_ok(data.get("token"), e.get("tok"))


def h_edit(req, ctx, args):
    d = args["data"] or {}
    if not C.valid_id(d.get("id")):
        return 404, {"error": "no such entry"}
    try:
        fields = _clean({k: v for k, v in d.items() if k in ("kind", "category", *FIELDS)}, partial=True)
    except C.FieldError as e:
        return 400, {"error": str(e)}
    wait = _edit_limit.check(args["ip"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}

    def f(db):
        e = _find(db, d["id"])
        if not e:
            return (404, {"error": "no such entry (it may have expired or been removed)"}), False
        if not _owner_or_admin(e, d, args):
            return (403, {"error": "only the person who added this entry can change it"}), False
        e.update(fields)
        e["updated"] = _now()                       # also "renews" an entry with no changes
        return (200, {"ok": True, "entry": _public(e, bool(args["admin"]))}), True
    return _store(ctx).update(f)


def h_remove(req, ctx, args):
    d = args["data"] or {}
    if not C.valid_id(d.get("id")):
        return 404, {"error": "no such entry"}
    wait = _edit_limit.check(args["ip"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}

    def f(db):
        e = _find(db, d["id"])
        if not e:
            return (404, {"error": "no such entry"}), False
        if not _owner_or_admin(e, d, args):
            return (403, {"error": "only the person who added this entry can remove it"}), False
        db["entries"] = [x for x in db["entries"] if x is not e]
        return (200, {"ok": True}), True
    return _store(ctx).update(f)


def h_flag(req, ctx, args):
    d = args["data"] or {}
    if not C.valid_id(d.get("id")):
        return 404, {"error": "no such entry"}
    wait = _flag_limit.check(args["ip"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}
    tag = C.ip_tag(ctx.data_dir(), args["ip"])

    def f(db):
        e = _find(db, d["id"])
        if not e:
            return (404, {"error": "no such entry"}), False
        fl = e.get("flags") if isinstance(e.get("flags"), list) else []
        if tag in fl or len(fl) >= MAX_FLAGS:
            return (200, {"ok": True}), False
        e["flags"] = fl + [tag]
        return (200, {"ok": True}), True
    return _store(ctx).update(f)


def h_moderate(req, ctx, args):
    d = args["data"] or {}
    op = d.get("op")
    if not C.valid_id(d.get("id")) or op not in ("hide", "unhide", "remove", "clear_flags"):
        return 400, {"error": "send id and op (hide, unhide, remove, clear_flags)"}

    def f(db):
        e = _find(db, d["id"])
        if not e:
            return (404, {"error": "no such entry"}), False
        if op == "remove":
            db["entries"] = [x for x in db["entries"] if x is not e]
        elif op == "clear_flags":
            e["flags"] = []
        else:
            e["hidden"] = op == "hide"
        return (200, {"ok": True}), True
    return _store(ctx).update(f)


ROUTES = [
    {"method": "GET", "path": "/api/skills", "handler": h_list, "access": "public"},
    {"method": "POST", "path": "/api/skills/add", "handler": h_add, "access": "public"},
    {"method": "POST", "path": "/api/skills/edit", "handler": h_edit, "access": "public"},
    {"method": "POST", "path": "/api/skills/remove", "handler": h_remove, "access": "public"},
    {"method": "POST", "path": "/api/skills/flag", "handler": h_flag, "access": "public"},
    {"method": "POST", "path": "/api/skills/moderate", "handler": h_moderate, "access": "admin"},
]
