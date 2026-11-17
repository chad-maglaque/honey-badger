"""
Weather satellites — when the Meteor-M satellites pass over, and the pictures the Badger made.

Passes are predicted from orbit data (TLEs) with software/sdr/orbit.py; recording and picture-
making (SatDump) are done by software/sdr/sdrd.py, which writes the plan to
/run/prepper/sdr/schedule.json and the pictures to /var/lib/prepper/sdr/images/.

  GET  /api/satellites              public  plan, orbit-data age, radio state, gallery list
  GET  /api/satellites/image/<id>   public  one picture (PNG/JPEG, checked by magic bytes)
  POST /api/satellites/tle          admin   paste orbit data (for a Badger that is never online)
  POST /api/satellites/tle/refresh  admin   download fresh orbit data now (needs internet)
  POST /api/satellites/antenna      admin   which antenna is on SDR 2: adsb | satellite | both

Optional: [satellites] board_post = true posts "New weather satellite image" on the community
board when a picture arrives (default off; never sent over the mesh).

Pass predictions depend on where the receiver is: times to the second and angles to a tenth of a
degree, collected over a few days, pin the Badger down to a few hundred metres (and rounding them
does not help: many rounded samples of a precise quantity average back to it). So visitors get
passes predicted here from the snapped visitor-grid point only (software/common/visitor.py), to the
minute and the whole degree, with just the Badger's decision (record / skip) borrowed from its own
plan; family members and the admin get the Badger's exact plan.
"""
from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdr"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))
import sdrcommon as C  # noqa: E402
import satplan  # noqa: E402
import tlefile  # noqa: E402
import visitor  # noqa: E402

FEATURE = {"title": "Weather satellites", "icon": "🛰️", "group": "radio", "route": "/satellites",
           "script": "/features/satellites.js", "access": "public",
           "description": "Pass times and pictures from Meteor-M weather satellites"}

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
CTYPES = {"png": "image/png", "jpg": "image/jpeg"}
_lock = threading.Lock()
_hits: dict[str, list[float]] = {}
_refresh = {"running": False, "result": None}
_vis_cache: dict = {}
VISITOR_REASON = {"record": "The Badger plans to record this pass.", "skip": "The Badger won't record this pass.",
                  "done": "Already handled.", "off": "Recording is off."}


def _rate_ok(ip: str, per_min: int = 60) -> bool:
    now = time.monotonic()
    with _lock:
        h = [t for t in _hits.get(ip, []) if now - t < 60]
        ok = len(h) < per_min
        if ok:
            h.append(now)
        _hits[ip] = h
        if len(_hits) > 2000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                del _hits[k]
    return ok


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def images_dir() -> Path:
    return C.state_dir() / "images"


def gallery() -> list[dict]:
    idx = C.read_json(images_dir() / "index.json", [])
    out = []
    for e in idx if isinstance(idx, list) else []:
        if not isinstance(e, dict) or not ID_RE.match(str(e.get("id", ""))) or e.get("ext") not in CTYPES:
            continue
        out.append({"id": e["id"], "name": str(e.get("name") or "")[:40], "aos": _num(e.get("aos")),
                    "los": _num(e.get("los")), "max_elev": _num(e.get("max_elev")), "width": _num(e.get("width")),
                    "height": _num(e.get("height")), "bytes": _num(e.get("bytes")), "created": _num(e.get("created"))})
    out.sort(key=lambda e: e["aos"] or e["created"] or 0, reverse=True)
    return out[:500]


def _pass(p: dict, precise: bool = True) -> dict | None:
    if not isinstance(p, dict):
        return None
    keys = ("aos", "los", "tca", "max_elev", "aos_az", "los_az", "start", "end")
    out = {k: _num(p.get(k)) for k in keys}
    if out["aos"] is None or out["los"] is None:
        return None
    if not precise:
        for k in ("aos", "los", "tca", "start", "end"):
            out[k] = visitor.round_time(out[k], 60)
        for k in ("max_elev", "aos_az", "los_az"):
            out[k] = visitor.round_angle(out[k])
    out.update(name=str(p.get("name") or "")[:40], decision=str(p.get("decision") or "")[:12],
               reason=str(p.get("reason") or "")[:300], approximate=bool(p.get("approximate")),
               freq_mhz=_num(p.get("freq_mhz")))
    return out


def visitor_passes(s_all: dict, plan: list[dict]) -> list[dict]:
    """Passes for visitors: predicted from the snapped grid point (never from the real position),
    times to the minute, angles to the degree; decision from the Badger's plan, matched by satellite
    and time (±10 min), with a fixed sentence instead of its reason (which quotes exact figures)."""
    real = C.position(s_all["home"])
    snap = visitor.coarse_position(real)
    if not snap:
        return []
    now = time.time()
    key = (snap["lat"], snap["lon"], int(now // 600))
    with _lock:
        if _vis_cache.get("key") == key:
            base = _vis_cache["passes"]
        else:
            base = None
    if base is None:
        sats_cfg = s_all["satellites"]["list"]
        try:
            base = satplan.predict(tlefile.load(sats_cfg), sats_cfg, {"lat": snap["lat"], "lon": snap["lon"], "alt_m": 0.0},
                                   key[2] * 600 - 1200, 24.0)
        except Exception:  # noqa: BLE001 — bad orbit data: no visitor pass list, the page still works
            base = []
        with _lock:
            _vis_cache.update(key=key, passes=base)
    out = []
    for p in base[:60]:
        if not isinstance(p, dict) or _num(p.get("los")) is None or p["los"] < now - 1200:
            continue
        match = next((q for q in plan if isinstance(q, dict) and q.get("norad") == p.get("norad")
                      and isinstance(q.get("tca"), (int, float)) and abs(q["tca"] - (p.get("tca") or 0)) < 600), None)
        decision = str((match or {}).get("decision") or "skip")[:12]
        row = _pass(dict(p, decision=decision, reason=VISITOR_REASON.get(decision, "")), precise=False)
        if row:
            row["start"] = row["end"] = None
            out.append(row)
    return out


def get_overview(req, ctx, args):
    if not _rate_ok(args["ip"]):
        return 429, {"error": "too many requests"}, {"Retry-After": "10"}
    s = C.settings(ctx.raw_config())["satellites"]
    sched = C.read_json(C.run_dir() / "schedule.json", {})
    sched = sched if isinstance(sched, dict) else {}
    st = C.read_status()
    sat = st.get("satellites") if isinstance(st.get("satellites"), dict) else {}
    paused = st.get("weather_radio") == "paused"
    raw_passes = sched.get("passes") if isinstance(sched.get("passes"), list) else []
    if args.get("member") or args.get("admin"):
        passes = [x for x in (_pass(p) for p in raw_passes[:60]) if x]
    else:
        passes = visitor_passes(C.settings(ctx.raw_config()), raw_passes)
    tle_rows = tlefile.info(s["list"], s["tle_stale_days"], s["tle_max_days"])
    mode = sched.get("mode") if isinstance(sched.get("mode"), dict) else {}
    hist = [h for h in (st.get("history") if isinstance(st.get("history"), list) else []) if isinstance(h, dict) and h.get("kind") == "satellite"][-5:]
    return {
        "running": bool(st.get("running")),
        "weather_radio": st.get("weather_radio"), "paused_by": st.get("by") if paused else None,
        "paused_until": st.get("until") if paused else None,
        "state": sat.get("state", "off"), "current": ({"name": str(sat["current"].get("name") or "")[:40], "until": _num(sat["current"].get("until"))}
                    if isinstance(sat.get("current"), dict) else None),
        "installed": sat.get("installed"),
        "mode": str(mode.get("why") or sat.get("mode") or "")[:400], "device": mode.get("device"),
        "used_today_min": sat.get("used_today_min"), "daily_max_min": s["daily_max_min"],
        "generated": _num(sched.get("generated")),
        "clock_suspect": bool(sched.get("clock_suspect")),
        "position": visitor.coarse_position(sched.get("position")),      # sdrd writes it rounded already
        "passes": passes, "tle": tle_rows, "images": gallery()[:s["keep_images"]],
        "history": [{"at": _num(h.get("at")), "result": str(h.get("result", ""))[:20], "name": str(h.get("name") or "")[:40],
                     "detail": str(h.get("detail") or "")[:200]} for h in hist],
        "board_post": s["board_post"], **C.sdr2_info(st),
    }


def get_image(req, ctx, args):
    if not _rate_ok(args["ip"], 120):
        return 429, {"error": "too many requests"}, {"Retry-After": "10"}
    iid = args["path"][len("/api/satellites/image/"):]
    if not ID_RE.match(iid):
        return 404, {"error": "no such picture"}
    entry = next((e for e in (C.read_json(images_dir() / "index.json", []) or [])
                  if isinstance(e, dict) and e.get("id") == iid), None)
    if not entry or entry.get("ext") not in CTYPES:
        return 404, {"error": "no such picture"}
    base = images_dir().resolve()
    f = (base / f"{iid}.{entry['ext']}").resolve()
    if f.parent != base or not f.is_file() or f.is_symlink():
        return 404, {"error": "no such picture"}
    with open(f, "rb") as fh:
        head = fh.read(8)
    if (entry["ext"] == "png" and head != b"\x89PNG\r\n\x1a\n") or (entry["ext"] == "jpg" and head[:3] != b"\xff\xd8\xff"):
        return 404, {"error": "no such picture"}
    if f.stat().st_size > 80_000_000:
        return 413, {"error": "picture too large"}
    ctx.send_file(req, f, CTYPES[entry["ext"]], name=f"{iid}.{entry['ext']}", inline=True)
    return None


def post_tle(req, ctx, args):
    data = args["data"] if isinstance(args["data"], dict) else {}
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return 400, {"error": "paste the orbit data (two-line elements) as text"}
    if len(text) > 20_000:
        return 413, {"error": "too much text — paste just the satellites you need"}
    s = C.settings(ctx.raw_config())["satellites"]
    names, problems = tlefile.import_text(text, allowed={x["norad"] for x in s["list"]})
    if not names:
        return 400, {"error": "No usable orbit data found.", "problems": problems[:10]}
    return {"ok": True, "loaded": names, "problems": problems[:10]}


def post_refresh(req, ctx, args):
    with _lock:
        if _refresh["running"]:
            return 202, {"ok": True, "message": "Already downloading…"}
        _refresh["running"] = True
    s = C.settings(ctx.raw_config())["satellites"]

    def run():
        try:
            names, problems = tlefile.fetch(s["list"], s["tle_url"])
            _refresh["result"] = {"at": time.time(), "loaded": names, "problems": problems[:10]}
        except Exception as e:  # noqa: BLE001
            _refresh["result"] = {"at": time.time(), "loaded": [], "problems": [f"{type(e).__name__}"]}
        finally:
            _refresh["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return 202, {"ok": True, "message": "Downloading fresh orbit data (needs an internet connection)…"}


def post_antenna(req, ctx, args):
    data = args["data"] if isinstance(args["data"], dict) else {}
    value = data.get("antenna")
    if value not in C.ANTENNAS:
        return 400, {"error": "antenna must be one of: " + ", ".join(C.ANTENNAS)}
    C.set_antenna(value, "admin")
    return {"ok": True, "antenna": value, "antenna_text": C.ANTENNA_TEXT[value]}


def get_refresh(req, ctx, args):
    return {"running": _refresh["running"], "result": _refresh["result"]}


# ── optional board post when a new picture arrives ──

def _post_new(ctx, posted_file: Path) -> None:
    s = C.settings(ctx.raw_config())["satellites"]
    imgs = gallery()
    if not imgs:
        return
    newest = max(imgs, key=lambda e: e["created"] or 0)
    last = (C.read_json(posted_file, {}) or {}).get("id")
    if last is None:                                       # first run: remember, don't post old pictures
        ctx.atomic_write(posted_file, json.dumps({"id": newest["id"]}))
        return
    if newest["id"] == last:
        return
    ctx.atomic_write(posted_file, json.dumps({"id": newest["id"]}))
    if not s["board_post"] or time.time() - (newest["created"] or 0) > 3 * 3600:
        return
    when = time.strftime("%H:%M", time.localtime(newest["aos"] or newest["created"] or time.time()))
    elev = f", highest {newest['max_elev']:.0f}°" if newest["max_elev"] is not None else ""
    text = (f"New weather satellite image: {newest['name'] or 'Meteor-M'} at {when}{elev}. "
            "Open Weather satellites on the Badger to see it.")
    ok, msg = ctx.board_post("weather-satellites", "Weather satellites", text, False, "community")
    if not ok:
        ctx.log(f"board post skipped: {msg}")


def setup(ctx):
    posted = ctx.data_dir() / "posted.json"

    def loop():
        while True:
            try:
                _post_new(ctx, posted)
            except Exception as e:  # noqa: BLE001 — never let this thread die
                ctx.log(f"board-post watcher: {type(e).__name__}: {e}")
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name="satellites-board").start()


ROUTES = [
    {"method": "GET", "path": "/api/satellites", "handler": get_overview, "access": "public"},
    {"method": "GET", "path": "/api/satellites/image/", "handler": get_image, "access": "public", "prefix": True},
    {"method": "POST", "path": "/api/satellites/tle", "handler": post_tle, "access": "admin"},
    {"method": "POST", "path": "/api/satellites/tle/refresh", "handler": post_refresh, "access": "admin"},
    {"method": "GET", "path": "/api/satellites/tle/refresh", "handler": get_refresh, "access": "admin"},
    {"method": "POST", "path": "/api/satellites/antenna", "handler": post_antenna, "access": "admin"},
]
