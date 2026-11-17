"""
Trusted circle — a few vetted households whose Badgers share a private radio channel (docs/features/circles.md).

A circle is a private Meshtastic channel with its own key, shared IN PERSON by QR code. The admin creates
one (a strong random key, written to the radio by the mesh bridge), or joins one with the link another
household showed them; can re-key it (lost or stolen Badger, a household you no longer trust: re-key, then
re-share the new link in person with the households you keep), block a radio, or leave.

What a circle may learn from this Badger — enforced here and in the mesh bridge, never optional:
  * nothing at all until the admin opts in, per category: skills · resources · aid-station status · missing
    persons; and nothing in Lockdown;
  * only POINTERS: "power: can fix generators — ask Elm St" (this Badger's chosen circle name, never where it
    is). Records stay here; no copy is sent anywhere; no amounts (there is no quantity field, and text that
    looks like an amount is refused), no counts, no lists;
  * PULL, not push: only as a direct answer to a circle radio that asked about one topic, rate-limited and
    logged; a sweep ("all", several topics at once, many topics in an hour) is refused and flagged here;
  * text that looks like an address or a map position is held back until the admin confirms it.
The circle's link (it holds the key) is shown to family members on their own device over https and on the
Badger's screen — never on the admin page (like the family radio link).

Routes (all JSON):
  GET  /api/circles            family members / admin: the circle, settings, offers, answers; admin: log + flags
  POST /api/circles/op         admin: {"op": "create", "name"} | {"op": "join", "url"} (https only) | rotate | leave
  GET  /api/circles/op?id=     admin: the bridge's result for that request (never a key)
  POST /api/circles/settings   admin: {"name", "share": {"skills": bool, …}, "confirm": bool}
  POST /api/circles/offer      admin: {"op": "add", "category", "topic", "text", "days", "confirm"} | {"op": "remove", "id"}
  POST /api/circles/block      admin: {"radio": "!a1b2c3d4", "block": bool, "label"}
  POST /api/circles/ask        family members / admin: {"topic"} — ask the circle who can help (one topic)
  GET  /api/circles/link       family members (https) and the front panel: the circle link

Other features share through can_share / share_pointer / withdraw_pointer (ctx.feature("circles")): a pointer
under the same rules, answered only on request (features/missing.py in Guarded).
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time

FEATURE = {"title": "Trusted circle", "icon": "🤝", "group": "tools", "script": "/features/circles.js",
           "access": "member", "description": "A few vetted households sharing pointers by radio"}

CATEGORIES = ("skills", "resources", "aid", "missing")
CATEGORY_LABELS = {"skills": "Skills", "resources": "Resources", "aid": "Aid-station status", "missing": "Missing persons"}
TOPICS = ("power", "water", "food", "medical", "shelter", "tools", "comms", "transport", "childcare", "aid", "missing",
          "other")                       # the same list (and order) as meshbridge.CIRCLE_TOPICS
CATEGORY_TOPICS = {"skills": ("power", "water", "food", "medical", "shelter", "tools", "comms", "transport",
                              "childcare", "other"),
                   "resources": ("power", "water", "food", "medical", "shelter", "tools", "comms", "transport", "other"),
                   "aid": ("aid",), "missing": ("missing",)}
MAX_OFFERS = 50
MAX_BLOCKED = 200
TEXT_MAX = 80
NAME_MAX = 24
ASK_PER_MIN, ASK_PER_HOUR = 1, 10
RADIO_RE = re.compile(r"^![0-9a-f]{8}$")
ATTENTION = re.compile(r"badger|prepper|prepared|survival|surviv|bunker|stockpile|stash|cache|armory|armoury|"
                       r"ammo|guns?\b|vault|supplies|stock\b", re.I)
_STREET = (r"st|street|rd|road|ave|avenue|blvd|boulevard|ln|lane|dr|drive|way|ct|court|pl|place|terrace|ter|"
           r"hwy|highway|route|rte|pike|parkway|pkwy|circle|cir|crescent|close|row|trail|trl")
ADDRESS_PATTERNS = [
    re.compile(r"\b\d{1,6}[a-z]?\s+(?:[A-Za-z][\w'.-]*\s+){0,3}(?:" + _STREET + r")\b", re.I),   # 12 Elm St
    re.compile(r"-?\d{1,3}\.\d{3,}\s*[,; ]\s*-?\d{1,3}\.\d{3,}"),                               # 39.7392, -104.99
    re.compile(r"\b\d{1,3}°\s*\d{1,2}['′]"),                                                  # 39°44'
    re.compile(r"\b(?:apt|apartment|unit|suite|flat|lot|po box|p\.o\. box|box)\s*#?\s*\d+", re.I),
    re.compile(r"#\s*\d{1,5}\b"),
    re.compile(r"\b\d{5}(?:-\d{4})?\b"),                                                      # ZIP code
    re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"),                                    # UK postcode
    re.compile(r"///\w+\.\w+\.\w+"),                                                          # what3words
    re.compile(r"\b(?:corner of|next to|across from|behind the|opposite the|blue house|red house|white house|"
               r"farmhouse|the house on|at the end of)\b", re.I),
]
QUANTITY = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:x\b|×|gal\b|gallons?|l\b|lt\b|liters?|litres?|lbs?\b|pounds?|kg\b|kilos?|oz\b|cans?\b|"
    r"cases?\b|bags?\b|boxes?\b|bottles?\b|jugs?\b|drums?\b|barrels?\b|rounds?\b|rds\b|tanks?\b|batter(?:y|ies)\b|"
    r"units?\b|pcs\b|pieces?\b|packs?\b|generators?\b|guns?\b|rifles?\b|days?\s+(?:of|worth)|weeks?\s+(?:of|worth)|"
    r"months?\s+(?:of|worth)|years?\s+(?:of|worth)|kwh\b|watts?\b|w\b|kw\b|amps?\b|ah\b|meals?\b|people\b)|"
    r"\b(?:plenty of|lots of|loads of|tons of|a stockpile|stockpiled?|surplus|enough for|spare)\b", re.I)

_lock = threading.Lock()
_ask_times: dict[str, list] = {}


# ───────────────────────────── helpers ─────────────────────────────

def _clean(s, limit: int) -> str:
    s = "".join(ch for ch in str(s if s is not None else "") if ch.isprintable())
    return " ".join(s.split())[:limit]


def address_like(text: str) -> bool:
    return any(p.search(text or "") for p in ADDRESS_PATTERNS)


def quantity_like(text: str) -> bool:
    return bool(QUANTITY.search(text or ""))


def _paths(ctx) -> dict:
    cfg = ctx.config()
    d = ctx.data_dir()
    return {"state": d / "state.json", "bridge": d / "bridge.json", "req": cfg.mesh_radio_request,
            "res": cfg.mesh_radio_result, "link": cfg.mesh_circle_link, "status": cfg.circle_status_file,
            "ask": cfg.circle_ask_file, "slot": cfg.mesh_outbox.with_name("mesh-circle-slot.json")}


def _load(ctx) -> dict:
    s = ctx.read_json(_paths(ctx)["state"], None)
    s = s if isinstance(s, dict) else {}
    share = s.get("share") if isinstance(s.get("share"), dict) else {}
    return {"v": 1, "name": _clean(s.get("name"), NAME_MAX),
            "share": {c: share.get(c) is True for c in CATEGORIES},
            "offers": [o for o in (s.get("offers") if isinstance(s.get("offers"), list) else [])
                       if isinstance(o, dict) and o.get("category") in CATEGORIES and o.get("topic") in TOPICS
                       and isinstance(o.get("id"), str)][:MAX_OFFERS],
            "blocked": [b for b in (s.get("blocked") if isinstance(s.get("blocked"), list) else [])
                        if isinstance(b, dict) and isinstance(b.get("radio"), str) and RADIO_RE.match(b["radio"])][:MAX_BLOCKED]}


def _save(ctx, st: dict) -> None:
    p = _paths(ctx)
    ctx.atomic_write(p["state"], json.dumps(st))
    now = time.time()
    # what the mesh bridge needs to answer — and only that (no ids, no who-added-it)
    ctx.atomic_write(p["bridge"], json.dumps({
        "v": 1, "name": st["name"], "share": st["share"],
        "offers": [{"category": o["category"], "topic": o["topic"], "text": o.get("text", ""),
                    "expires": o.get("expires"), "at": o.get("at")} for o in st["offers"]
                   if not isinstance(o.get("expires"), (int, float)) or o["expires"] > now],
        "blocked": [b["radio"] for b in st["blocked"]]}))


def _status(ctx) -> dict:
    d = ctx.read_json(_paths(ctx)["status"], None)
    if not isinstance(d, dict) or not isinstance(d.get("updated"), (int, float)) or time.time() - d["updated"] > 300:
        return {}
    return d


def _channel(ctx) -> int:
    st = _status(ctx)
    ch = st.get("channel")
    if isinstance(ch, int) and not isinstance(ch, bool) and 1 <= ch <= 7:
        return ch
    raw = ctx.raw_config().get("radio") or {}
    v = raw.get("mesh_circle_channel") if isinstance(raw, dict) else None
    if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 7:
        return v
    slot = ctx.read_json(_paths(ctx)["slot"], None)
    v = slot.get("ch") if isinstance(slot, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 7 else -1


def _sharing_allowed(ctx) -> bool:
    _, eff = ctx.posture()
    return eff["circle_sharing"]["value"] != "none"


def _items(xs, keys, limit=100):
    out = []
    for x in (xs if isinstance(xs, list) else [])[-limit:]:
        if isinstance(x, dict):
            out.append({k: (x.get(k) if isinstance(x.get(k), (int, float)) and not isinstance(x.get(k), bool)
                            else _clean(x.get(k), 160)) for k in keys if k in x})
    return out


# ─────────────── the sharing path for other features (missing persons, aid status) ───────────────
# A feature never pushes to the circle: it leaves a POINTER here (an offer tagged with its own key), which the
# bridge answers only when a circle radio asks about that topic — the same rules as an offer the admin types.

def can_share(ctx, category: str) -> tuple[bool, str]:
    """(True, "") when `category` may go to the circle now; else (False, why — plain words for the admin)."""
    if category not in CATEGORIES:
        return False, "that isn't something a circle can be told about"
    if not _sharing_allowed(ctx):
        return False, "in Lockdown nothing goes out to the circle"
    if _channel(ctx) <= 0:
        return False, "this Badger isn't in a trusted circle"
    if not _load(ctx)["share"][category]:
        return False, (f"sharing “{CATEGORY_LABELS[category]}” with your circle isn't turned on "
                       "(Settings → Trusted circle)")
    return True, ""


def share_pointer(ctx, category: str, key: str, text: str, days: int = 7, topic: str | None = None) -> tuple[bool, str]:
    """Put (or replace) the pointer `key` (e.g. "missing:<post id>") for `category`. (True, the text shared) or
    (False, why). Text that looks like an amount or a place is refused — there is no "share anyway" here."""
    ok, why = can_share(ctx, category)
    if not ok:
        return False, why
    topic = category if category in ("aid", "missing") else topic
    if topic not in CATEGORY_TOPICS[category]:
        return False, "no such topic"
    text = _clean(text, TEXT_MAX)
    if not text or not isinstance(key, str) or not key or len(key) > 80:
        return False, "nothing to share"
    if quantity_like(text) or address_like(text):
        return False, "that line looks like an amount or a place, and the circle only gets pointers"
    days = days if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 90 else 7
    with _lock:
        st = _load(ctx)
        st["offers"] = [o for o in st["offers"] if o.get("source") != key]
        if len(st["offers"]) >= MAX_OFFERS:
            return False, f"the circle offers are full ({MAX_OFFERS}); remove an old one first"
        now = time.time()
        st["offers"].append({"id": secrets.token_urlsafe(8), "category": category, "topic": topic, "text": text,
                             "expires": now + days * 86400, "at": now, "source": key})
        _save(ctx, st)
    return True, text


def withdraw_pointer(ctx, key: str) -> bool:
    """Remove the pointer `key` (the post was closed or removed). True when there was one."""
    with _lock:
        st = _load(ctx)
        keep = [o for o in st["offers"] if o.get("source") != key]
        if len(keep) == len(st["offers"]):
            return False
        st["offers"] = keep
        _save(ctx, st)
    return True


# ───────────────────────────── routes ─────────────────────────────

def get_circles(req, ctx, args):
    st = _load(ctx)
    status = _status(ctx)
    info, _ = ctx.posture()
    admin = bool(args["admin"])
    out = {"in_circle": _channel(ctx) > 0, "channel": _channel(ctx), "bridge_running": bool(status),
           "posture": info["level"], "sharing_allowed": _sharing_allowed(ctx),
           "name": st["name"], "share": st["share"], "categories": [{"id": c, "label": CATEGORY_LABELS[c],
                                                                     "topics": list(CATEGORY_TOPICS[c])} for c in CATEGORIES],
           "topics": list(TOPICS),
           "offers": [{k: o.get(k) for k in ("id", "category", "topic", "text", "expires", "at")} for o in st["offers"]],
           "answers": _items(status.get("answers"), ("at", "topic", "from", "text"), 50),
           "asks": _items(status.get("asks"), ("at", "topic", "answers"), 20)}
    if admin:
        out["blocked"] = st["blocked"]
        out["log"] = _items(status.get("log"), ("at", "from", "topic", "result", "via"), 100)
        out["flags"] = _items(status.get("flags"), ("at", "from", "what", "via"), 50)
    return out


def post_op(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    op = d.get("op")
    if op not in ("create", "join", "rotate", "leave"):
        return 400, {"ok": False, "error": "op: create, join, rotate or leave"}
    rec = {"id": secrets.token_hex(6), "at": time.time(), "op": op}
    if op == "create":
        name = _clean(d.get("name") or "Circle", 12)
        if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,11}", name):
            return 400, {"ok": False, "error": "A circle name is 1–11 letters, digits, spaces, dots or dashes."}
        if ATTENTION.search(name):
            return 400, {"ok": False, "error": "Pick a plain name: phones show it, and a name like that tells a reader "
                                               "what the channel is for."}
        rec["name"] = name
    if op == "join":
        url = d.get("url")
        if not isinstance(url, str) or not re.fullmatch(r"https://meshtastic\.org/e/[?A-Za-z0-9=&_-]{0,41}#[A-Za-z0-9_=-]{4,900}",
                                                       url.strip()):
            return 400, {"ok": False, "error": "Paste the circle link exactly as shown (https://meshtastic.org/e/#…)."}
        if not req.secure_transport():
            return 403, {"ok": False, "error": "The circle link holds the circle's key, so it isn't sent over this "
                                               "unencrypted connection. Open this page with https:// to join."}
        rec["url"] = url.strip()
    if op in ("rotate", "leave") and _channel(ctx) <= 0:
        return 409, {"ok": False, "error": "This Badger isn't in a circle."}
    if op in ("create", "join") and _channel(ctx) > 0:
        return 409, {"ok": False, "error": "This Badger is already in a circle. Leave it first."}
    p = _paths(ctx)
    try:
        p["req"].parent.mkdir(parents=True, exist_ok=True)
        ctx.atomic_write(p["req"], json.dumps(rec))
        try:
            p["req"].chmod(0o600)                    # (a join request holds the key until the bridge reads it)
        except OSError:
            pass
    except OSError as e:
        return 500, {"ok": False, "error": f"Couldn't ask the radio bridge ({e.strerror or e}). Nothing changed."}
    ctx.log(f"circle {op} requested")
    return {"ok": True, "id": rec["id"], "bridge_running": bool(ctx.services_up().get("mesh"))}


def get_op(req, ctx, args):
    rid = (args["qs"].get("id") or [""])[0]
    res = ctx.read_json(_paths(ctx)["res"], None)
    if not isinstance(res, dict) or res.get("id") != rid or not rid:
        return {"ok": True, "done": False}
    return {"ok": True, "done": True, "result": {k: res.get(k) for k in ("ok", "op", "error", "channel", "name", "at")
                                                 if k in res}}


def post_settings(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    with _lock:
        st = _load(ctx)
        if "name" in d:
            name = _clean(d.get("name"), NAME_MAX + 1)
            if len(name) > NAME_MAX:
                return 400, {"ok": False, "error": f"Keep the circle name to {NAME_MAX} characters."}
            if ATTENTION.search(name):
                return 400, {"ok": False, "error": "Pick a plain name (a street or a nickname): a name like that draws "
                                                   "attention if the circle key ever leaks."}
            if address_like(name) and d.get("confirm") is not True:
                return 409, {"ok": False, "needs_confirm": True,
                             "warning": "That looks like an address. Circle members only need to know who to ask — "
                                        "not where you live. Send it again with “share anyway” if you're sure."}
            st["name"] = name
        share = d.get("share")
        if share is not None:
            if not isinstance(share, dict) or not all(k in CATEGORIES and isinstance(v, bool) for k, v in share.items()):
                return 400, {"ok": False, "error": "share: {skills|resources|aid|missing: true/false}"}
            st["share"].update(share)
        _save(ctx, st)
    return {"ok": True, "name": st["name"], "share": st["share"]}


def post_offer(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    op = d.get("op", "add")
    with _lock:
        st = _load(ctx)
        if op == "remove":
            oid = d.get("id")
            n = len(st["offers"])
            st["offers"] = [o for o in st["offers"] if o.get("id") != oid]
            if len(st["offers"]) == n:
                return 404, {"ok": False, "error": "no such offer"}
            _save(ctx, st)
            return {"ok": True}
        if op != "add":
            return 400, {"ok": False, "error": "op: add or remove"}
        cat, topic = d.get("category"), d.get("topic")
        if cat not in CATEGORIES:
            return 400, {"ok": False, "error": "category: skills, resources, aid or missing"}
        if cat in ("aid", "missing"):
            topic = cat
        if topic not in CATEGORY_TOPICS[cat]:
            return 400, {"ok": False, "error": f"topic for {cat}: {', '.join(CATEGORY_TOPICS[cat])}"}
        if not isinstance(d.get("text", ""), str):
            return 400, {"ok": False, "error": "text must be text"}
        text = _clean(d.get("text", ""), TEXT_MAX + 1)
        if len(text) > TEXT_MAX:
            return 400, {"ok": False, "error": f"Keep it short: {TEXT_MAX} characters at most."}
        if quantity_like(text) or (cat == "aid" and re.search(r"\d", text)):
            return 400, {"ok": False, "error": "No amounts, please: circle members learn only that someone can help "
                                               "and who to ask — never how much anyone has."}
        if address_like(text) and d.get("confirm") is not True:
            return 409, {"ok": False, "needs_confirm": True,
                         "warning": "That looks like an address or a place. Answers say who to ask, never where. "
                                    "Remove it, or send again with “share anyway”."}
        days = d.get("days", 30)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            return 400, {"ok": False, "error": "days: 1 to 90"}
        if len(st["offers"]) >= MAX_OFFERS:
            return 409, {"ok": False, "error": f"At most {MAX_OFFERS} offers. Remove an old one first."}
        now = time.time()
        offer = {"id": secrets.token_urlsafe(8), "category": cat, "topic": topic, "text": text,
                 "expires": now + days * 86400, "at": now}
        st["offers"].append(offer)
        _save(ctx, st)
    return {"ok": True, "offer": offer, "shared": st["share"][cat] and _sharing_allowed(ctx)}


def post_block(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    radio = d.get("radio")
    radio = radio.strip().lower() if isinstance(radio, str) else ""
    if not RADIO_RE.match(radio) or not isinstance(d.get("block"), bool):
        return 400, {"ok": False, "error": "radio: !a1b2c3d4 and block: true/false"}
    with _lock:
        st = _load(ctx)
        st["blocked"] = [b for b in st["blocked"] if b["radio"] != radio]
        if d["block"]:
            if len(st["blocked"]) >= MAX_BLOCKED:
                return 409, {"ok": False, "error": "the block list is full"}
            st["blocked"].append({"radio": radio, "label": _clean(d.get("label"), 32), "at": time.time()})
        _save(ctx, st)
    return {"ok": True, "blocked": st["blocked"]}


def post_ask(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    topic = d.get("topic")
    if topic not in TOPICS:
        return 400, {"ok": False, "error": "Ask about one topic: " + ", ".join(TOPICS)}
    if _channel(ctx) <= 0:
        return 409, {"ok": False, "error": "This Badger isn't in a circle."}
    if not _sharing_allowed(ctx):
        return 409, {"ok": False, "error": "In Lockdown nothing goes out to the circle."}
    who = args["member"]["id"] if args["member"] else "admin:" + args["ip"]
    now = time.time()
    with _lock:
        times = [t for t in _ask_times.get(who, []) if now - t < 3600]
        if sum(1 for t in times if now - t < 60) >= ASK_PER_MIN or len(times) >= ASK_PER_HOUR:
            return 429, {"ok": False, "error": "One question a minute (ten an hour), please: radio time is shared."}
        times.append(now)
        _ask_times[who] = times
        if len(_ask_times) > 500:
            for k in [k for k, v in _ask_times.items() if not v or now - v[-1] > 3600]:
                del _ask_times[k]
        p = _paths(ctx)["ask"]
        cur = ctx.read_json(p, None)
        asks = [a for a in (cur.get("asks") if isinstance(cur, dict) and isinstance(cur.get("asks"), list) else [])
                if isinstance(a, dict) and isinstance(a.get("at"), (int, float)) and now - a["at"] < 120][-19:]
        asks.append({"id": secrets.token_hex(6), "topic": topic, "at": now})
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            ctx.atomic_write(p, json.dumps({"asks": asks}))
        except OSError as e:
            return 500, {"ok": False, "error": f"Couldn't hand the question to the radio ({e.strerror or e})."}
    return {"ok": True, "topic": topic, "message": "Asked. Answers (if anyone can help) appear here within a minute."}


def get_link(req, ctx, args):
    if not (args["member"] or req.local_console()):
        return 401, {"error": "family members only"}
    d = ctx.read_json(_paths(ctx)["link"], None)
    url = d.get("url") if isinstance(d, dict) and isinstance(d.get("url"), str) else None
    if url and not req.secure_transport():
        return {"ok": True, "url": None, "secret_hidden": True,
                "message": "The circle link holds the circle's key, so it isn't sent over this unencrypted connection. "
                           "Show it on the Badger's own screen (knob → Family radio → Circle link), or open this page "
                           "with https://."}
    return {"ok": bool(url), "url": url, "error": None if url else "No circle link yet (is the radio plugged in?)."}


def _member_or_admin(req, ctx) -> bool:
    return bool(req.member() or req.is_admin())


def _member_or_panel(req, ctx) -> bool:
    return bool(req.member() or req.local_console())


ROUTES = [
    {"method": "GET", "path": "/api/circles", "handler": get_circles, "access": "member_or_admin"},
    {"method": "POST", "path": "/api/circles/op", "handler": post_op, "access": "admin"},
    {"method": "GET", "path": "/api/circles/op", "handler": get_op, "access": "admin"},
    {"method": "POST", "path": "/api/circles/settings", "handler": post_settings, "access": "admin"},
    {"method": "POST", "path": "/api/circles/offer", "handler": post_offer, "access": "admin"},
    {"method": "POST", "path": "/api/circles/block", "handler": post_block, "access": "admin"},
    {"method": "POST", "path": "/api/circles/ask", "handler": post_ask, "access": _member_or_admin},
    {"method": "GET", "path": "/api/circles/link", "handler": get_link, "access": _member_or_panel},
]
