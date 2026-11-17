"""
Aircraft — planes overhead, decoded from their own ADS-B broadcasts (1090 MHz) by readsb.

The radio side lives in software/sdr/sdrd.py (prepper-sdr.service). SDR 2 (standard in every
Badger) watches for aircraft all the time while the 1090 MHz whip is on its jack. With the
satellite antenna fitted, scans are on demand. If SDR 2 is missing or failing, a scan borrows
the weather radio for up to 15 minutes (weather alerts pause meanwhile; refused while an alert
is live; a few a day) — the fallback.

  GET  /api/aircraft          public   planes + the radio's state (rate-limited per address)
  POST /api/aircraft/scan     member_or_admin   ask for a scan (in the fallback it pauses weather
                                       alerts, so not for anonymous guests on the Wi-Fi)
  POST /api/aircraft/stop     member_or_admin   end a scan early
  POST /api/aircraft/antenna  admin    which antenna is on SDR 2: adsb | satellite | both

ADS-B is unencrypted public broadcast; nothing here is secret, but the list is rebuilt from
validated fields only, and the Badger's position is rounded.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdr"))
import sdrcommon as C  # noqa: E402

FEATURE = {"title": "Aircraft", "icon": "✈️", "group": "radio", "route": "/aircraft",
           "script": "/features/aircraft.js", "access": "public",
           "description": "Planes overhead, from their own radio broadcasts"}

HEX_RE = re.compile(r"^~?[0-9a-f]{6}$")
FLIGHT_RE = re.compile(r"[^A-Z0-9 -]")
CAT_RE = re.compile(r"^[A-D][0-7]$")
SQUAWK_RE = re.compile(r"^[0-7]{4}$")
MAX_AIRCRAFT = 400
_lock = threading.Lock()
_hits: dict[str, list[float]] = {}
_cache: dict = {}
_cmd_last: dict[str, float] = {}


def _rate_ok(ip: str, per_min: int) -> bool:
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


def _f(v, lo=-math.inf, hi=math.inf):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        return None
    return v


def clean(a: dict, origin: dict | None, max_km: float) -> dict | None:
    """One readsb aircraft record → only the fields the page shows, validated."""
    if not isinstance(a, dict):
        return None
    hx = a.get("hex")
    hx = hx.lower() if isinstance(hx, str) else ""
    if not HEX_RE.match(hx):
        return None
    flight = FLIGHT_RE.sub("", str(a.get("flight", "") or "").upper()).strip()[:8]
    alt = a.get("alt_baro")
    alt = "ground" if alt == "ground" else _f(alt, -2000, 80000)
    if alt is None:
        alt = _f(a.get("alt_geom"), -2000, 80000)
    lat, lon = _f(a.get("lat"), -90, 90), _f(a.get("lon"), -180, 180)
    seen_pos = _f(a.get("seen_pos"), 0, 1e6)
    if lat is None or lon is None or (seen_pos is not None and seen_pos > 60):
        lat = lon = None                       # no position, or an old one: list it, don't map it
    out = {"hex": hx, "flight": flight, "alt_ft": alt if alt == "ground" else (round(alt) if alt is not None else None),
           "speed_kt": (lambda v: round(v) if v is not None else None)(_f(a.get("gs"), 0, 2000)),
           "track": (lambda v: round(v) if v is not None else None)(_f(a.get("track"), 0, 360)),
           "vrate_fpm": (lambda v: round(v) if v is not None else None)(_f(a.get("baro_rate", a.get("geom_rate")), -20000, 20000)),
           "lat": round(lat, 5) if lat is not None else None, "lon": round(lon, 5) if lon is not None else None,
           "seen_s": (lambda v: round(v, 1) if v is not None else None)(_f(a.get("seen"), 0, 1e6)),
           "squawk": str(a.get("squawk")) if SQUAWK_RE.match(str(a.get("squawk", ""))) else None,
           "category": str(a.get("category")) if CAT_RE.match(str(a.get("category", ""))) else None,
           "emergency": str(a.get("emergency")) if str(a.get("emergency", "none")) in
           ("general", "lifeguard", "minfuel", "nordo", "unlawful", "downed") else None,
           "dist_km": None, "bearing": None}
    if origin and lat is not None:
        d = C.haversine_km(origin["lat"], origin["lon"], lat, lon)
        if d > max_km:
            return None                        # a garbled position halfway round the world
        out["dist_km"] = round(d, 1)
        out["bearing"] = round(C.bearing_deg(origin["lat"], origin["lon"], lat, lon))
    return out


def snapshot(ctx) -> dict:
    """aircraft.json → the cleaned list, shared by every caller for 2 seconds."""
    now = time.monotonic()
    with _lock:
        if _cache.get("at") and now - _cache["at"] < 2.0:
            return _cache["data"]
    s = C.settings(ctx.raw_config())
    origin = C.position(s["home"])
    raw = C.read_json(C.run_dir() / "readsb" / "aircraft.json", None, limit=3_000_000)
    planes, updated, stale = [], None, True
    if isinstance(raw, dict):
        updated = _f(raw.get("now"), 0, 1e11)
        stale = not updated or time.time() - updated > 30
        if not stale:
            for a in (raw.get("aircraft") if isinstance(raw.get("aircraft"), list) else [])[:5000]:
                c = clean(a, origin, s["aircraft"]["max_range_km"])
                if c and (c["seen_s"] is None or c["seen_s"] <= 60):
                    planes.append(c)
                if len(planes) >= MAX_AIRCRAFT:
                    break
    planes.sort(key=lambda p: (p["dist_km"] is None, p["dist_km"] or 0, p["flight"] or p["hex"]))
    data = {"aircraft": planes, "count": len(planes), "updated": updated, "stale": stale,
            "origin": ({"lat": round(origin["lat"], 2), "lon": round(origin["lon"], 2), "source": origin["source"]}
                       if origin else None)}
    with _lock:
        _cache.update(at=now, data=data)
    return data


def radio(ctx) -> dict:
    st = C.read_status()
    air = st.get("aircraft") if isinstance(st.get("aircraft"), dict) else {}
    paused = st.get("weather_radio") == "paused"
    return {"running": bool(st.get("running")), "weather_radio": st.get("weather_radio"),
            "paused_by": st.get("by") if paused else None, "paused_until": st.get("until") if paused else None,
            "state": air.get("state", "off"), "device": air.get("device"), "until": air.get("until"),
            "by": air.get("by"), "message": str(air.get("message") or "")[:300], "request": air.get("request"),
            "installed": air.get("installed"), "always_on": bool(air.get("always_on")),
            "scans_left": air.get("scans_left"), "scan_minutes": air.get("scan_minutes", 15),
            "dedicated": bool(air.get("dedicated")), **C.sdr2_info(st)}


def get_aircraft(req, ctx, args):
    s = C.settings(ctx.raw_config())
    if not _rate_ok(args["ip"], s["aircraft"]["requests_per_min"]):
        return 429, {"error": "too many requests — the list refreshes by itself every few seconds"}, {"Retry-After": "10"}
    return dict(snapshot(ctx), radio=radio(ctx))


def _command(ctx, args, action: str):
    data = args["data"] if isinstance(args["data"], dict) else {}
    who = (args["member"] or {}).get("name") if args["member"] else ("admin" if args["admin"] else "")
    key = f"who:{who}" if who else f"ip:{args['ip']}"
    now = time.monotonic()
    with _lock:
        if now - _cmd_last.get(key, 0) < 5:
            return 429, {"error": "one moment — try again in a few seconds"}
        _cmd_last[key] = now
        if len(_cmd_last) > 1000:
            for k in [k for k, t in _cmd_last.items() if now - t > 60]:
                del _cmd_last[k]
    st = C.read_status()
    if not st.get("running"):
        return 503, {"error": "The radio service isn't running. An admin can check Settings → Services."}
    cmd = {"action": action, "id": secrets.token_urlsafe(9), "by": str(who)[:40], "at": time.time()}
    if action == "scan":
        m = data.get("minutes")
        if m is not None:
            if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 30:
                return 400, {"error": "minutes must be a whole number from 1 to 30"}
            cmd["minutes"] = m
    C.atomic_write(C.run_dir() / "aircraft.cmd", json.dumps(cmd), 0o640)
    return 202, {"ok": True, "id": cmd["id"], "message": "Asking the radio…"}


def post_antenna(req, ctx, args):
    """Admin: "I put the V-dipole on SDR 2" — the scheduler follows within ~15 s."""
    data = args["data"] if isinstance(args["data"], dict) else {}
    value = data.get("antenna")
    if value not in C.ANTENNAS:
        return 400, {"error": "antenna must be one of: " + ", ".join(C.ANTENNAS)}
    C.set_antenna(value, "admin")
    return {"ok": True, "antenna": value, "antenna_text": C.ANTENNA_TEXT[value]}


def post_scan(req, ctx, args):
    return _command(ctx, args, "scan")


def post_stop(req, ctx, args):
    return _command(ctx, args, "stop")


ROUTES = [
    {"method": "GET", "path": "/api/aircraft", "handler": get_aircraft, "access": "public"},
    {"method": "POST", "path": "/api/aircraft/scan", "handler": post_scan, "access": "member_or_admin"},
    {"method": "POST", "path": "/api/aircraft/stop", "handler": post_stop, "access": "member_or_admin"},
    {"method": "POST", "path": "/api/aircraft/antenna", "handler": post_antenna, "access": "admin"},
]
