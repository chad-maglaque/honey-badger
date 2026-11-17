"""
NOAA forecast as text — the launcher side.

The heavy work (recording the broadcast through alertd's tap, ffmpeg, whisper.cpp) is done by
prepper-forecast.service (software/speech/forecastd.py), never in the portal. This feature only:
  * shows the latest written forecast, when it was captured, and older versions (public, like the
    weather radio itself);
  * lets an admin ask for a capture now (writes update-now.request; a systemd path unit starts the
    service) and switch the board / mesh posts on or off;
  * posts each NEW forecast once: to the community board (default on, at most every 6 h) and its
    short version (≤ 200 bytes) to the mesh community channel (default on, at most twice a day).
    A small watcher thread started in setup() does this. On the mesh it is an automatic note (kind
    "auto": lowest priority, local hop limit) with the dedupe key "forecast:<MHz>:<date>:<AM|PM>", so
    of all the Badgers listening to the same NOAA transmitter only one posts it (docs/MESH.md).

Files in ctx.data_dir() (written by the service unless noted): latest.json, index.json,
history/<id>.json, status.json, settings.json (admin, from here), posted.json (here),
update-now.request (here).
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "speech"))
import speechlib  # noqa: E402

FEATURE = {"title": "NOAA forecast", "icon": "📻", "group": "radio", "route": "/forecast",
           "script": "/features/forecast.js", "access": "public",
           "description": "The weather radio's forecast, written out"}

ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
NOTE = ("Automatically transcribed from NOAA Weather Radio — may contain errors. "
        "For warnings, listen to a weather radio or watch for the red alert banner.")
POST_NAME = "Weather Radio"
_lock = threading.Lock()
_last_update_req = [0.0]


def _num(v, d, lo, hi):
    return d if isinstance(v, bool) or not isinstance(v, (int, float)) else max(lo, min(hi, v))


def conf(ctx) -> dict:
    c = ctx.cfg() or {}
    s = ctx.read_json(ctx.data_dir() / "settings.json", {}) or {}
    s = s if isinstance(s, dict) else {}
    pick = lambda k, d: s[k] if isinstance(s.get(k), bool) else (c.get(k) if isinstance(c.get(k), bool) else d)  # noqa: E731
    return {
        "post_board": pick("post_board", True),
        "post_mesh": pick("post_mesh", True),
        "board_min_hours": _num(c.get("board_min_hours"), 6, 1, 168),
        "mesh_per_day": int(_num(c.get("mesh_per_day"), 2, 1, 6)),
        "interval_hours": int(_num(s.get("interval_hours", c.get("interval_hours")), 3, 1, 48)),
        "max_age_hours": _num(c.get("post_max_age_hours"), 12, 1, 72),
    }


def _public(rec: dict | None) -> dict | None:
    """What a browser gets of a stored forecast (no internal keys)."""
    if not isinstance(rec, dict) or not ID_RE.fullmatch(str(rec.get("id", ""))):
        return None
    secs = []
    for s in rec.get("sections") or []:
        if not isinstance(s, dict):
            continue
        out = {"kind": str(s.get("kind", ""))[:20], "title": str(s.get("title", ""))[:60],
               "text": str(s.get("text", ""))[:8000]}
        if isinstance(s.get("periods"), list):
            out["periods"] = [{"name": str(p.get("name", ""))[:30], "text": str(p.get("text", ""))[:1500]}
                              for p in s["periods"][:20] if isinstance(p, dict)]
            out["intro"] = str(s.get("intro", ""))[:1500]
        secs.append(out)
    return {"id": rec["id"], "at": rec.get("at"), "new": bool(rec.get("new")), "short": rec.get("short"),
            "short_by": rec.get("short_by"), "sections": secs, "raw": str(rec.get("raw", ""))[:30000],
            "seconds": rec.get("seconds"), "freq": rec.get("freq"), "model": rec.get("model"),
            "same_as": rec.get("same_as")}


def get_forecast(req, ctx, args):
    d = ctx.data_dir()
    st = ctx.read_json(d / "status.json", {}) or {}
    idx = ctx.read_json(d / "index.json", []) or []
    avail = speechlib.available(speechlib.Settings.from_config(ctx.cfg()))
    c = conf(ctx)
    hist = [{"id": i["id"], "at": i.get("at"), "new": bool(i.get("new")), "headline": str(i.get("headline", ""))[:200]}
            for i in idx[:100] if isinstance(i, dict) and ID_RE.fullmatch(str(i.get("id", "")))]
    out = {
        "latest": _public(ctx.read_json(d / "latest.json", None)),
        "history": hist, "note": NOTE,
        "status": {k: st.get(k) for k in ("state", "message", "at", "last_attempt", "started")} if isinstance(st, dict) else {},
        "available": avail["stt"], "install_hint": "" if avail["stt"] else avail["hint"],
        "update_pending": (d / "update-now.request").exists(),
        "interval_hours": c["interval_hours"],
    }
    if args["admin"]:
        out["settings"] = {k: c[k] for k in ("post_board", "post_mesh", "interval_hours")}
    return out


def get_item(req, ctx, args):
    fid = (args["qs"].get("id") or [""])[0]
    if not ID_RE.fullmatch(fid):
        return 400, {"error": "bad id"}
    rec = _public(ctx.read_json(ctx.data_dir() / "history" / f"{fid}.json", None))
    return rec if rec else (404, {"error": "That forecast is no longer kept."})


def update_now(req, ctx, args):
    with _lock:
        t, mono = time.time(), time.monotonic()     # the window on the monotonic clock: jumps can't stretch it
        if _last_update_req[0] and mono - _last_update_req[0] < 120:
            return 429, {"error": "A capture was asked for a moment ago. It takes about 10 minutes."}
        _last_update_req[0] = mono
    ctx.atomic_write(ctx.data_dir() / "update-now.request", json.dumps({"at": t}))
    return {"ok": True, "message": "Recording the weather broadcast now. The written forecast appears here in "
                                   "about 10 minutes."}


def save_settings(req, ctx, args):
    data = args["data"] or {}
    path = ctx.data_dir() / "settings.json"
    with _lock:
        s = ctx.read_json(path, {}) or {}
        s = s if isinstance(s, dict) else {}
        for k in ("post_board", "post_mesh"):
            if k in data:
                if not isinstance(data[k], bool):
                    return 400, {"error": f"{k} must be true or false"}
                s[k] = data[k]
        if "interval_hours" in data:
            v = data["interval_hours"]
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 48:
                return 400, {"error": "interval_hours must be a whole number from 1 to 48"}
            s["interval_hours"] = v
        ctx.atomic_write(path, json.dumps(s))
    return {"ok": True, "settings": {k: conf(ctx)[k] for k in ("post_board", "post_mesh", "interval_hours")}}


# ─────────────────────────────── posting ─────────────────────────────────────

def mesh_key(rec: dict) -> str:
    """The same on every Badger that hears the same transmitter in the same half day."""
    f = rec.get("freq")
    freq = f"{f:.3f}" if isinstance(f, (int, float)) and not isinstance(f, bool) else "?"
    at = rec.get("at") if isinstance(rec.get("at"), (int, float)) and not isinstance(rec.get("at"), bool) else time.time()
    lt = time.localtime(at)
    return f"forecast:{freq}:{time.strftime('%Y-%m-%d', lt)}:{'AM' if lt.tm_hour < 12 else 'PM'}"


def _mesh_send(ctx, text: str, key: str) -> bool:
    try:
        return bool(ctx.mesh_send(text, "community", kind="auto", dedupe=key))
    except TypeError:                                  # an older portal: plain call
        return bool(ctx.mesh_send(text, "community"))


def board_text(rec: dict) -> str:
    lines = ["New NOAA forecast (automatically transcribed from the weather radio — may contain errors):"]
    if rec.get("short"):
        lines.append(str(rec["short"]))
    f = next((s for s in rec.get("sections") or [] if isinstance(s, dict) and s.get("kind") == "forecast"), None)
    for p in ((f or {}).get("periods") or [])[:3]:
        lines.append(f"{str(p.get('name', ''))[:30]}: {str(p.get('text', ''))[:300]}")
    lines.append("Full text: Radio & sky → NOAA forecast.")
    return "\n".join(lines)[:1900]


def check_and_post(ctx, now: float | None = None) -> dict:
    """Post the latest forecast once if it is new. Returns what it did (for tests and the log)."""
    now = time.time() if now is None else now
    d = ctx.data_dir()
    did = {"board": False, "mesh": False}
    with _lock:
        rec = ctx.read_json(d / "latest.json", None)
        if not isinstance(rec, dict) or not ID_RE.fullmatch(str(rec.get("id", ""))):
            return did
        posted = ctx.read_json(d / "posted.json", {}) or {}
        posted = posted if isinstance(posted, dict) else {}
        if posted.get("last_id") == rec["id"]:
            return did
        posted["last_id"] = rec["id"]
        at = rec.get("at") if isinstance(rec.get("at"), (int, float)) else 0
        if rec.get("new") and now - at <= conf(ctx)["max_age_hours"] * 3600:
            c = conf(ctx)
            board_at = posted.get("board_at") if isinstance(posted.get("board_at"), (int, float)) else 0
            if c["post_board"] and now - board_at >= c["board_min_hours"] * 3600:
                ok, msg = ctx.board_post("feature:forecast", POST_NAME, board_text(rec), False, "community", None)
                if ok:
                    posted["board_at"] = now
                    did["board"] = True
                else:
                    ctx.log(f"board post not made: {msg}")
            sends = [t for t in posted.get("mesh_at", []) if isinstance(t, (int, float)) and now - t < 86400]
            short = rec.get("short")
            if c["post_mesh"] and isinstance(short, str) and short.strip() and len(sends) < c["mesh_per_day"]:
                if _mesh_send(ctx, short, mesh_key(rec)):
                    sends.append(now)
                    did["mesh"] = True
            posted["mesh_at"] = sends
        ctx.atomic_write(d / "posted.json", json.dumps(posted))
    return did


def _watch(ctx, every: float = 30.0):
    while True:
        try:
            check_and_post(ctx)
        except Exception as e:  # noqa: BLE001 — keep watching; a bad file must not kill the thread
            ctx.log(f"posting failed: {type(e).__name__}: {e}")
        time.sleep(every)


def setup(ctx):
    try:
        d = ctx.data_dir()
        posted = ctx.read_json(d / "posted.json", None)
        rec = ctx.read_json(d / "latest.json", None)
        if posted is None and isinstance(rec, dict) and rec.get("id"):
            # first start with forecasts already there (feature just enabled): don't post an old one
            ctx.atomic_write(d / "posted.json", json.dumps({"last_id": rec["id"]}))
    except OSError as e:
        ctx.log(f"setup: {e}")
    threading.Thread(target=_watch, args=(ctx,), daemon=True, name="forecast-poster").start()


ROUTES = [
    {"method": "GET", "path": "/api/forecast", "handler": get_forecast, "access": "public"},
    {"method": "GET", "path": "/api/forecast/item", "handler": get_item, "access": "public"},
    {"method": "POST", "path": "/api/forecast/update", "handler": update_now, "access": "admin"},
    {"method": "POST", "path": "/api/forecast/settings", "handler": save_settings, "access": "admin"},
]
