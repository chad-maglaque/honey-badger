"""
Missing persons and lost & found.

Anyone on the Badger's Wi-Fi can post (rate-limited) a missing person or a lost/found item, with
an optional photo; whoever posts gets an edit token (kept in their browser) to update it, mark
the person FOUND SAFE, or close it. The admin can hide or remove any post. Search is by name,
partial and case/accent-insensitive ("jose" finds "José Núñez").

Photos: the browser re-encodes to JPEG on a canvas (≤1600 px, ≤1 MB), which also drops EXIF/GPS.
The server doesn't trust that: it checks the JPEG structure and dimensions, rebuilds the file
without any APP1–APP15/COM segments (EXIF, GPS, XMP, comments), stores it under a random id
(mode 0600) and serves it only as image/jpeg.

Mesh: a poster can ask for a short radio announcement ("MISSING: Ana Ruiz, 8, red jacket, last
seen Elm St 14:00 — see the community board"); nothing goes on air until the admin approves it, and
approvals are rate-limited (default 3 per hour) because LoRa airtime is shared and slow. When a
person who was announced is marked found safe, a FOUND SAFE announcement is queued for approval.
Where an approved announcement goes follows the security posture (software/common/posture.py):
  Neighbourly  the public (community) channel, as above;
  Guarded      the trusted circle only, through the circles feature's sharing path (a pointer — one short
               line, no places — answered only when a circle radio asks about missing persons), and only if
               the admin opted the "missing" category in; otherwise nothing is sent and the admin is told why;
  Lockdown     never.
A status change or removal withdraws the circle pointer (a FOUND SAFE approval shares a new one).

Sealed records (docs/SECURITY.md): the poster's contact details are sealed at rest. Names, descriptions,
where/when and photos stay readable, so searching for someone keeps working while the Badger is locked;
contacts show again once the owner unlocks it. New posts and updates are taken while locked.
"""
from __future__ import annotations

import importlib.util
import os
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

FEATURE = {"title": "Missing & found", "icon": "🔎", "group": "community", "route": "/missing",
           "script": "/features/missing.js", "access": "public",
           "description": "Missing people, found safe, lost & found items"}

KINDS = ("person", "item")
STATUSES = {"person": ("missing", "safe", "closed"), "item": ("lost", "found", "returned")}
RESOLVED = ("safe", "closed", "returned")
ANNOUNCE = {"missing": "MISSING", "safe": "FOUND SAFE", "lost": "LOST", "found": "FOUND"}
FIELDS = {"name": 120, "description": 1000, "where": 200, "when": 80, "contact": 200}
PRIVATE = ("contact",)                     # sealed at rest
RANK = {"missing": 0, "safe": 1, "lost": 2, "found": 2, "closed": 3, "returned": 3}
PHOTO_ID = re.compile(r"^([A-Za-z0-9_-]{12,32})\.jpg$")
MAX_UPLOAD = 1_250_000                       # route cap; [missing] max_photo_kb is the real limit (default 1200)
MAX_FLAGS = 20
MESH_TAIL = " — see the community board"


def _now() -> float:
    return time.time()


def _mono() -> float:
    """Rate-limit windows and lockouts (in memory): a wall-clock jump must not stretch or skip them."""
    return time.monotonic()


_add_limit = C.RateLimit(3, 600, clock=lambda: _mono())
_edit_limit = C.RateLimit(30, 600, clock=lambda: _mono())
_photo_limit = C.RateLimit(10, 600, clock=lambda: _mono())
_flag_limit = C.RateLimit(10, 600, clock=lambda: _mono())
_mesh_limit = C.RateLimit(3, 3600, clock=lambda: _mono())
_stores: dict[str, C.JsonStore] = {}
_stores_lock = threading.Lock()


def _reset_limits() -> None:
    for lim in (_add_limit, _edit_limit, _photo_limit, _flag_limit, _mesh_limit):
        lim.clear()


def _store(ctx) -> C.JsonStore:
    key = str(ctx.data_dir())
    with _stores_lock:
        if key not in _stores:
            _stores[key] = C.JsonStore(ctx.data_dir() / "posts.json", lambda: {"v": 1, "posts": []})
        return _stores[key]


def _priv(ctx) -> C.Private:
    return C.Private(C.sealer_of(ctx), PRIVATE)


def _migrate(ctx) -> None:
    """Sealed-records setup (and older files): contacts into the sealed map, re-sealed with this run's key."""
    pv = _priv(ctx)

    def f(d):
        pv.migrate(d, d["posts"])
        pv.reseal(d, [x.get("id") for x in d["posts"] if isinstance(x, dict)])
        return None, True
    _store(ctx).update(f)


def _photos(ctx) -> Path:
    p = ctx.data_dir() / "photos"
    p.mkdir(mode=0o750, exist_ok=True)
    return p


def _cfg(ctx) -> dict:
    c = ctx.cfg()
    return {"max": C.cfg_int(c, "max_posts", 300, 10, 5000), "per_ip": C.cfg_int(c, "per_ip", 10, 1, 100),
            "adds": C.cfg_int(c, "posts_per_10min", 3, 1, 100),
            "photo_bytes": C.cfg_int(c, "max_photo_kb", 1200, 50, 1200) * 1000,
            "photo_px": C.cfg_int(c, "max_photo_px", 2048, 256, 8192),
            "photo_total": C.cfg_int(c, "photos_total_mb", 400, 10, 100000) * 1_000_000,
            "keep": C.cfg_int(c, "resolved_keep_days", 14, 1, 3650),
            "mesh": C.cfg_bool(c, "mesh", True), "mesh_hour": C.cfg_int(c, "mesh_per_hour", 3, 1, 30),
            "board_copy": C.cfg_bool(c, "board_copy", True)}


def _one(args, key, default=""):
    v = (args.get("qs") or {}).get(key) or [default]
    return str(v[0])[:100] if isinstance(v, list) else default


# ─────────────────────────────── JPEG check ──────────────────────────────────

def clean_jpeg(data: bytes, max_px: int) -> tuple[bytes, int, int]:
    """Check that `data` is a well-formed baseline/progressive JPEG no larger than max_px on a side,
    and return it rebuilt without metadata segments (APP1..APP15, COM, non-JFIF APP0)."""
    n = len(data)
    if n < 125 or data[:3] != b"\xff\xd8\xff":
        raise C.FieldError("That isn't a JPEG photo.")
    if data[-2:] != b"\xff\xd9":
        raise C.FieldError("The photo is damaged (it doesn't end properly).")
    out = bytearray(b"\xff\xd8")
    i, dims = 2, None
    while True:
        if i + 4 > n or data[i] != 0xFF:
            raise C.FieldError("The photo is damaged.")
        while i < n and data[i] == 0xFF:            # fill bytes
            i += 1
        if i + 3 > n:
            raise C.FieldError("The photo is damaged.")
        marker = data[i]
        i += 1
        if marker in (0x00, 0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            raise C.FieldError("The photo is damaged.")
        seglen = int.from_bytes(data[i:i + 2], "big")
        if seglen < 2 or i + seglen > n:
            raise C.FieldError("The photo is damaged.")
        seg = data[i + 2:i + seglen]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):     # SOFn: frame header
            if len(seg) < 6 or dims:
                raise C.FieldError("The photo is damaged.")
            h, w, comps = int.from_bytes(seg[1:3], "big"), int.from_bytes(seg[3:5], "big"), seg[5]
            if not (0 < w <= max_px and 0 < h <= max_px):
                raise C.FieldError(f"The photo is too big: at most {max_px} pixels on a side.")
            if comps not in (1, 3) or len(seg) != 6 + 3 * comps:
                raise C.FieldError("Unsupported photo format.")
            dims = (w, h)
        keep = not (0xE1 <= marker <= 0xEF or marker == 0xFE or (marker == 0xE0 and not seg.startswith(b"JFIF\x00")))
        if keep:
            out += bytes((0xFF, marker)) + data[i:i + seglen]
        i += seglen
        if marker == 0xDA:                          # start of scan: the rest is image data up to EOI
            if not dims:
                raise C.FieldError("The photo is damaged.")
            out += data[i:]
            return bytes(out), dims[0], dims[1]


# ─────────────────────────────── helpers ─────────────────────────────────────

def _fit(s: str, nbytes: int) -> str:
    s = " ".join(str(s).split())
    if len(s.encode()) <= nbytes:
        return s
    while s and len((s + "…").encode()) > nbytes:
        s = s[:-1]
    return (s.rstrip(" ,") + "…") if s else ""


def mesh_text(p: dict) -> str | None:
    """'MISSING: Ana Ruiz, 8, red jacket, last seen Elm St 14:00 — see the community board' (≤200 bytes).
    Over the air: never names the device."""
    prefix = ANNOUNCE.get(p.get("status"))
    if not prefix:
        return None
    name = _fit(p.get("name", ""), 70)
    head = f"{prefix}: {name}"
    if p.get("status") == "safe":
        return _fit(head + MESH_TAIL, 200)
    where = " ".join(x for x in (p.get("where", ""), p.get("when", "")) if x)
    seen = ""
    if where:
        lead = "last seen " if p.get("kind") == "person" else ("found at " if p.get("status") == "found" else "lost at ")
        seen = ", " + lead + _fit(where, 70)
    budget = 200 - len((head + seen + MESH_TAIL).encode()) - 2
    desc = _fit(p.get("description", ""), budget) if budget >= 10 else ""
    return _fit(head + (", " + desc if desc else "") + seen + MESH_TAIL, 200)


# ── where an approved announcement goes: the posture decides ──
CIRCLE_DAYS = {"missing": 7, "lost": 7, "found": 7, "safe": 2}


def _circles(ctx):
    try:
        h = ctx.feature("circles")
    except (AttributeError, TypeError):            # an older portal: no cross-feature access
        return None
    return h if h is not None and hasattr(h.mod, "share_pointer") else None


def mesh_route(ctx) -> tuple[str, str, str]:
    """("public" | "circle" | "none", posture level, a note for the admin)."""
    try:
        level = (ctx.posture()[0] or {}).get("level")
    except Exception:  # noqa: BLE001 — an older portal without the posture: as before
        level = None
    if level == "lockdown":
        return "none", level, ("Lockdown: nothing is announced on the radio, not even to your trusted circle. "
                               "Change the security posture first if this must go out.")
    if level != "guarded":
        return "public", level or "neighbourly", ""
    h = _circles(ctx)
    if h is None:
        why = "the trusted-circle feature is switched off"
    else:
        ok, why = h.mod.can_share(h.ctx, "missing")
        if ok:
            return "circle", level, ("Guarded: shared with your trusted circle only. Circle radios that ask about "
                                     "missing persons get one short line (no places); nothing is broadcast.")
    return "none", level, (f"Guarded: announcements go only to your trusted circle, and {why}, so nothing goes out "
                           "on the radio. To announce it to every radio in range, switch the posture to Neighbourly.")


def circle_text(p: dict, custom: str | None, checks) -> str | None:
    """The pointer for the circle (≤80 characters): the admin's own wording if they changed it, else the
    announcement without where/when — circles get pointers, never places. None if nothing passes `checks`."""
    prefix = ANNOUNCE.get(p.get("status"))
    if not prefix:
        return None
    name, desc = p.get("name", ""), p.get("description", "")
    cands = []
    if custom and custom != mesh_text(p):
        cands.append(custom[:-len(MESH_TAIL)] if custom.endswith(MESH_TAIL) else custom)
    if desc and p.get("status") != "safe":
        cands.append(f"{prefix}: {name}, {desc}")
    cands.append(f"{prefix}: {name}")
    for c in cands:
        t = _fit(c, 80)
        if t and checks(t):
            return t
    return None


def _withdraw_circle(ctx, p: dict) -> None:
    mesh = p.get("mesh") or {}
    if mesh.get("via") != "circle" or not mesh.get("circle"):
        return
    h = _circles(ctx)
    if h is not None:
        try:
            h.mod.withdraw_pointer(h.ctx, f"missing:{p.get('id')}")
        except Exception as e:  # noqa: BLE001
            ctx.log(f"circle pointer not withdrawn: {type(e).__name__}")
    mesh["circle"] = False


def _public(p: dict, admin: bool, pv: C.Private | None = None, priv: dict | None = None) -> dict:
    out = {k: p.get(k) for k in ("id", "kind", "status", *FIELDS, "created", "updated", "status_at")}
    if pv is not None:
        sv = pv.view(p, priv or {})
        out.update(contact=sv["contact"], contact_sealed=bool(sv.get("sealed")))
    out["photo"] = f"/api/missing/photo/{p['photo']}.jpg" if p.get("photo") else None
    out["mesh"] = (p.get("mesh") or {}).get("state", "none")
    out["mesh_via"] = (p.get("mesh") or {}).get("via", "public") if out["mesh"] == "sent" else None
    if admin:
        out["hidden"] = bool(p.get("hidden"))
        out["flags"] = len(p.get("flags") or [])
        out["mesh_text"] = mesh_text(p)
        out["mesh_sent_at"] = (p.get("mesh") or {}).get("sent_at")
    return out


def _find(d, pid):
    for p in d["posts"]:
        if isinstance(p, dict) and p.get("id") == pid:
            return p
    return None


def _delete_photo(ctx, photo) -> None:
    if isinstance(photo, str) and C.ID_RE.match(photo):
        try:
            (_photos(ctx) / f"{photo}.jpg").unlink()
        except OSError:
            pass


def _purge(ctx, d: dict, keep_days: int, now: float) -> bool:
    dead = [p for p in d["posts"] if not isinstance(p, dict) or
            (p.get("status") in RESOLVED and now - float(p.get("status_at") or p.get("updated") or 0) > keep_days * 86400)]
    for p in dead:
        if isinstance(p, dict):
            _delete_photo(ctx, p.get("photo"))
    if dead:
        gone = {id(p) for p in dead}
        d["posts"] = [p for p in d["posts"] if id(p) not in gone]
        _priv(ctx).tidy(d, [p.get("id") for p in d["posts"]])
    moved = _priv(ctx).migrate(d, d["posts"]) if any(isinstance(p, dict) and "contact" in p for p in d["posts"]) else False
    return bool(dead) or moved


def _clean(data: dict, kind: str, partial: bool = False) -> dict:
    out = {}
    labels = {"name": "Name" if kind == "person" else "What", "description": "Description",
              "where": "Where", "when": "When", "contact": "Contact"}
    for k, n in FIELDS.items():
        if not partial or k in data:
            out[k] = C.clean_text(data.get(k), n, multiline=(k == "description"), label=labels[k], required=(k == "name"))
    if not partial or "status" in data:
        out["status"] = C.clean_choice(data.get("status"), STATUSES[kind], "Status", default=STATUSES[kind][0])
    return out


def _owner_or_admin(p, raw_token, args) -> bool:
    return bool(args["admin"]) or C.token_ok(raw_token, p.get("tok"))


# ─────────────────────────────── handlers ────────────────────────────────────

def h_list(req, ctx, args):
    admin = bool(args["admin"])
    q, kind, status = _one(args, "q"), _one(args, "kind"), _one(args, "status")
    cfg = _cfg(ctx)
    now = _now()
    st = _store(ctx)
    st.update(lambda d: (None, _purge(ctx, d, cfg["keep"], now)))
    pv = _priv(ctx)

    def f(d):
        priv, _ = pv.reveal(d)
        out = []
        for p in d["posts"]:
            if not isinstance(p, dict) or (p.get("hidden") and not admin):
                continue
            if kind in KINDS and p.get("kind") != kind:
                continue
            if status and p.get("status") != status:
                continue
            name_hit = C.match(q, p.get("name"))
            if q and not (name_hit or C.match(q, p.get("description"), p.get("where"))):
                continue
            out.append((0 if name_hit else 1, RANK.get(p.get("status"), 9), -float(p.get("status_at") or p.get("updated") or 0),
                        _public(p, admin, pv, priv)))
        out.sort(key=lambda t: t[:3])
        return [t[3] for t in out]
    posts = st.view(f)
    res = {"posts": posts, "keep_days": cfg["keep"], "mesh": cfg["mesh"]}
    if admin:
        res["mesh_pending"] = sum(1 for p in posts if p["mesh"] == "requested")
        res["mesh_route"], res["posture"], res["mesh_note"] = mesh_route(ctx)
    return res


def h_add(req, ctx, args):
    data = args["data"] or {}
    try:
        kind = C.clean_choice(data.get("kind"), KINDS, "Kind", default="person")
        fields = _clean(data, kind)
        want_mesh = C.clean_bool(data.get("mesh_request"), "Radio announcement")
    except C.FieldError as e:
        return 400, {"error": str(e)}
    if fields["status"] in RESOLVED:
        return 400, {"error": "A new post can't start out resolved."}
    cfg = _cfg(ctx)
    wait = _add_limit.check(args["ip"], cfg["adds"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}
    tag = C.ip_tag(ctx.data_dir(), args["ip"])
    raw, hashed = C.new_token()
    now = _now()
    pv = _priv(ctx)
    private = {k: fields.pop(k) for k in PRIVATE if k in fields}

    def f(d):
        changed = _purge(ctx, d, cfg["keep"], now)
        if sum(1 for p in d["posts"] if isinstance(p, dict) and p.get("ipt") == tag and p.get("status") not in RESOLVED) >= cfg["per_ip"]:
            return (429, {"error": f"At most {cfg['per_ip']} open posts from one device. Close one of yours first."}), changed
        if len(d["posts"]) >= cfg["max"]:
            return (507, {"error": "The board is full. Ask the Badger's owner to tidy it up."}), changed
        p = {"id": C.new_id(), "kind": kind, **fields, "photo": None, "created": now, "updated": now, "status_at": now,
             "tok": hashed, "ipt": tag, "flags": [], "hidden": False,
             "mesh": {"state": "requested" if want_mesh and cfg["mesh"] else "none", "requested_at": now if want_mesh else None}}
        pv.put(d, p, private)
        d["posts"].append(p)
        return (200, {"ok": True, "id": p["id"], "token": raw, "post": _public(p, False, pv, {p["id"]: private})}), True
    return _store(ctx).update(f)


def h_update(req, ctx, args):
    data = args["data"] or {}
    if not C.valid_id(data.get("id")):
        return 404, {"error": "no such post"}
    wait = _edit_limit.check(args["ip"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}
    cfg = _cfg(ctx)
    now = _now()

    def f(d):
        p = _find(d, data["id"])
        if not p:
            return (404, {"error": "no such post (it may have been removed)"}), False
        if not _owner_or_admin(p, data.get("token"), args):
            return (403, {"error": "only the person who posted this (or the Badger's owner) can change it"}), False
        try:
            fields = _clean({k: v for k, v in data.items() if k in (*FIELDS, "status")}, p["kind"], partial=True)
            want_mesh = C.clean_bool(data.get("mesh_request"), "Radio announcement")
        except C.FieldError as e:
            return (400, {"error": str(e)}), False
        old = p.get("status")
        pv = _priv(ctx)
        pv.migrate(d, [p])
        pv.put(d, p, {k: fields.pop(k) for k in PRIVATE if k in fields})
        p.update(fields)
        p["updated"] = now
        mesh = p.setdefault("mesh", {"state": "none"})
        if p["status"] != old:
            p["status_at"] = now
            _withdraw_circle(ctx, p)                   # the circle's pointer said the old status
            # news of someone who was announced as missing goes out too, once the admin approves it
            if cfg["mesh"] and p["status"] == "safe" and mesh.get("sent_status") == "missing":
                mesh.update(state="requested", requested_at=now)
            elif mesh.get("state") == "requested" and not ANNOUNCE.get(p["status"]):
                mesh["state"] = "none"
        if want_mesh and cfg["mesh"] and ANNOUNCE.get(p["status"]) and mesh.get("sent_status") != p["status"]:
            mesh.update(state="requested", requested_at=now)
        return (200, {"ok": True, "post": _public(p, bool(args["admin"]), pv, pv.reveal(d)[0])}), True
    return _store(ctx).update(f)


def h_remove(req, ctx, args):
    data = args["data"] or {}
    if not C.valid_id(data.get("id")):
        return 404, {"error": "no such post"}
    wait = _edit_limit.check(args["ip"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}

    def f(d):
        p = _find(d, data["id"])
        if not p:
            return (404, {"error": "no such post"}), False
        if not _owner_or_admin(p, data.get("token"), args):
            return (403, {"error": "only the person who posted this (or the Badger's owner) can remove it"}), False
        _withdraw_circle(ctx, p)
        _delete_photo(ctx, p.get("photo"))
        d["posts"] = [x for x in d["posts"] if x is not p]
        _priv(ctx).drop(d, p.get("id"))
        return (200, {"ok": True}), True
    return _store(ctx).update(f)


def h_photo_upload(req, ctx, args):
    # Read the body first, whatever happens: unread bytes would be parsed as the next request.
    n = int(req.headers.get("Content-Length") or 0)
    body = req.rfile.read(n) if n else b""
    if len(body) < n:
        req.close_connection = True
        return 400, {"error": "upload ended early"}
    pid = args["path"].removeprefix("/api/missing/photo/")
    if not C.valid_id(pid):
        return 404, {"error": "no such post"}
    cfg = _cfg(ctx)
    if n > cfg["photo_bytes"]:
        return 413, {"error": f"The photo is too large: at most {cfg['photo_bytes'] // 1000} KB."}
    ctype = (req.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype not in ("image/jpeg", "image/jpg"):
        return 415, {"error": "Send the photo as image/jpeg."}
    wait = _photo_limit.check(args["ip"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}
    raw_token = (req.headers.get("X-Edit-Token") or "").strip()
    owner = _store(ctx).view(lambda d: (lambda p: None if not p else (p.get("tok"), p.get("photo")))(_find(d, pid)))
    if owner is None:
        return 404, {"error": "no such post"}
    if not (args["admin"] or C.token_ok(raw_token, owner[0])):
        return 403, {"error": "only the person who posted this (or the Badger's owner) can add a photo"}
    try:
        jpeg, w, h = clean_jpeg(body, cfg["photo_px"])
    except C.FieldError as e:
        return 400, {"error": str(e)}
    folder = _photos(ctx)
    used = 0
    for f in folder.glob("*.jpg"):
        try:
            used += f.stat().st_size
        except OSError:
            pass
    if used + len(jpeg) > cfg["photo_total"]:
        return 507, {"error": "There's no room left for photos. Ask the Badger's owner to tidy up old posts."}
    photo_id = secrets.token_urlsafe(12)
    C.write_private(folder / f"{photo_id}.jpg", jpeg)

    def f(d):
        p = _find(d, pid)
        if not p:
            return None, False
        old = p.get("photo")
        p.update(photo=photo_id, photo_w=w, photo_h=h, updated=_now())
        return old, True
    try:
        old = _store(ctx).update(f)
    except BaseException:
        _delete_photo(ctx, photo_id)
        raise
    if old is None and _store(ctx).view(lambda d: _find(d, pid)) is None:       # post vanished meanwhile
        _delete_photo(ctx, photo_id)
        return 404, {"error": "no such post"}
    if old:
        _delete_photo(ctx, old)
    return {"ok": True, "photo": f"/api/missing/photo/{photo_id}.jpg", "width": w, "height": h}


def h_photo_remove(req, ctx, args):
    data = args["data"] or {}
    if not C.valid_id(data.get("id")):
        return 404, {"error": "no such post"}

    def f(d):
        p = _find(d, data["id"])
        if not p:
            return (404, {"error": "no such post"}), False
        if not _owner_or_admin(p, data.get("token"), args):
            return (403, {"error": "not allowed"}), False
        _delete_photo(ctx, p.get("photo"))
        p["photo"] = None
        return (200, {"ok": True}), True
    return _store(ctx).update(f)


def h_photo_get(req, ctx, args):
    m = PHOTO_ID.match(args["path"].removeprefix("/api/missing/photo/"))
    if not m:
        return 404, {"error": "not found"}
    photo = m.group(1)
    vis = _store(ctx).view(lambda d: next((not p.get("hidden") for p in d["posts"]
                                           if isinstance(p, dict) and p.get("photo") == photo), None))
    if vis is None or (not vis and not args["admin"]):
        return 404, {"error": "not found"}
    path = _photos(ctx) / f"{photo}.jpg"
    if not path.is_file() or path.is_symlink():
        return 404, {"error": "not found"}
    ctx.send_file(req, path, "image/jpeg")
    return None


def h_flag(req, ctx, args):
    data = args["data"] or {}
    if not C.valid_id(data.get("id")):
        return 404, {"error": "no such post"}
    wait = _flag_limit.check(args["ip"])
    if wait:
        return 429, {"error": C.wait_msg(wait)}
    tag = C.ip_tag(ctx.data_dir(), args["ip"])

    def f(d):
        p = _find(d, data["id"])
        if not p:
            return (404, {"error": "no such post"}), False
        fl = p.get("flags") if isinstance(p.get("flags"), list) else []
        if tag in fl or len(fl) >= MAX_FLAGS:
            return (200, {"ok": True}), False
        p["flags"] = fl + [tag]
        return (200, {"ok": True}), True
    return _store(ctx).update(f)


def h_moderate(req, ctx, args):
    data = args["data"] or {}
    op = data.get("op")
    ops = ("hide", "unhide", "remove", "clear_flags", "mesh_send", "mesh_reject")
    if not C.valid_id(data.get("id")) or op not in ops:
        return 400, {"error": "send id and op (" + ", ".join(ops) + ")"}
    cfg = _cfg(ctx)
    text = None
    if op == "mesh_send":
        if not cfg["mesh"]:
            return 400, {"error": "Radio announcements are turned off ([missing] mesh = false)."}
        try:
            text = C.clean_text(data.get("text"), 200, label="Announcement")
        except C.FieldError as e:
            return 400, {"error": str(e)}
        if len(text.encode()) > 200:
            return 400, {"error": "Announcement: at most 200 bytes (accents and emoji count double or more)."}

    def f(d):
        p = _find(d, data["id"])
        if not p:
            return (404, {"error": "no such post"}), False
        mesh = p.setdefault("mesh", {"state": "none"})
        if op == "remove":
            _withdraw_circle(ctx, p)
            _delete_photo(ctx, p.get("photo"))
            d["posts"] = [x for x in d["posts"] if x is not p]
            _priv(ctx).drop(d, p.get("id"))
        elif op == "clear_flags":
            p["flags"] = []
        elif op in ("hide", "unhide"):
            p["hidden"] = op == "hide"
        elif op == "mesh_reject":
            mesh["state"] = "rejected"
        else:
            msg = text or mesh_text(p)
            if not msg:
                return (400, {"error": "Nothing to announce for a closed post."}), False
            if p.get("hidden"):
                return (400, {"error": "Unhide the post before announcing it."}), False
            route, level, note = mesh_route(ctx)
            if route == "none":
                return (409, {"error": note, "posture": level, "sent": None}), False
            if route == "circle":
                h = _circles(ctx)
                line = circle_text(p, text, lambda t: not (h.mod.quantity_like(t) or h.mod.address_like(t)))
                if not line:
                    return (409, {"error": "This can't go to the circle: it reads like an address or an amount, and "
                                           "the circle only gets pointers. Shorten the text and try again.",
                                  "posture": level}), False
                ok, got = h.mod.share_pointer(h.ctx, "missing", f"missing:{p.get('id')}", line,
                                              days=CIRCLE_DAYS.get(p.get("status"), 7))
                if not ok:
                    return (409, {"error": f"Not shared with the circle: {got}.", "posture": level}), False
                mesh.update(state="sent", sent_at=_now(), sent_status=p.get("status"), text=got, via="circle",
                            circle=True)
                return (200, {"ok": True, "sent": got, "via": "circle", "note": note, "posture": level}), True
            wait = _mesh_limit.check("mesh", cfg["mesh_hour"])
            if wait:
                return (429, {"error": "Radio airtime limit reached (" + str(cfg["mesh_hour"]) + " announcements an hour). " + C.wait_msg(wait)}), False
            if not _mesh_send(ctx, msg, f"missing:{p.get('id')}:{p.get('status')}"):
                return (500, {"error": "Couldn't queue the announcement for the radio."}), False
            mesh.update(state="sent", sent_at=_now(), sent_status=p.get("status"), text=msg, via="public")
            if cfg["board_copy"]:
                try:
                    ctx.board_post("feature:missing", "Missing & found", msg, False, "community", None)
                except Exception as e:  # noqa: BLE001 — the radio message already went out
                    ctx.log(f"board copy failed: {type(e).__name__}")
            return (200, {"ok": True, "sent": msg, "via": "public"}), True
        return (200, {"ok": True}), True
    return _store(ctx).update(f)


def _mesh_send(ctx, msg: str, key: str) -> bool:
    """A community note on the mesh (docs/MESH.md), with a dedupe key from the post (and its status, so a
    FOUND SAFE follow-up is not taken for a repeat of the MISSING note)."""
    try:
        return bool(ctx.mesh_send(msg, "community", kind="community", dedupe=key))
    except TypeError:                                  # an older portal: plain call
        return bool(ctx.mesh_send(msg, "community"))


def setup(ctx):
    s = C.sealer_of(ctx)
    if s is not None:
        s.register_migrator("missing", lambda: _migrate(ctx))
    _photos(ctx)
    try:
        os.chmod(_photos(ctx), 0o750)
    except OSError:
        pass


ROUTES = [
    {"method": "GET", "path": "/api/missing", "handler": h_list, "access": "public"},
    {"method": "POST", "path": "/api/missing/add", "handler": h_add, "access": "public"},
    {"method": "POST", "path": "/api/missing/update", "handler": h_update, "access": "public"},
    {"method": "POST", "path": "/api/missing/remove", "handler": h_remove, "access": "public"},
    {"method": "POST", "path": "/api/missing/photo-remove", "handler": h_photo_remove, "access": "public"},
    {"method": "POST", "path": "/api/missing/flag", "handler": h_flag, "access": "public"},
    {"method": "POST", "path": "/api/missing/moderate", "handler": h_moderate, "access": "admin"},
    {"method": "POST", "path": "/api/missing/photo/", "handler": h_photo_upload, "access": "public", "prefix": True,
     "raw": True, "max_bytes": MAX_UPLOAD},
    {"method": "GET", "path": "/api/missing/photo/", "handler": h_photo_get, "access": "public", "prefix": True},
]
