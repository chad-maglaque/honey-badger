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
validated fields only. Where the Badger is IS secret: exact plane positions together with each
plane's distance and bearing from the Badger give its position to ~100 m (three planes are enough).
So for visitors the origin is snapped to the visitor grid like everywhere else
(software/common/visitor.py, cells >= 10 km) and every distance and bearing is measured from that
snapped point: trilaterating the list finds the cell centre, never the house. Family members and
the admin get distances and bearings from the real position. Who started a scan is shown to family members and the admin only.
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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))
import sdrcommon as C  # noqa: E402
import visitor  # noqa: E402

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


def _origin_view(origin: dict | None, precise: bool) -> dict | None:
    if not origin:
        return None
    if precise:
        return {"lat": origin["lat"], "lon": origin["lon"], "source": origin["source"]}
    c = visitor.coarse(origin["lat"], origin["lon"])
    return {"lat": c[0], "lon": c[1], "source": origin["source"], "coarse": True} if c else None


def snapshot(ctx, precise: bool = False) -> dict:
    """aircraft.json → the cleaned list, shared by every caller for 2 seconds. precise=False (visitors):
    origin, distances and bearings from the snapped grid point; True (family, admin): from the real one."""
    now = time.monotonic()
    key = "precise" if precise else "visitor"
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < 2.0:
            return hit[1]
    s = C.settings(ctx.raw_config())
    real = C.position(s["home"])
    shown = _origin_view(real, precise)
    raw = C.read_json(C.run_dir() / "readsb" / "aircraft.json", None, limit=3_000_000)
    planes, updated, stale = [], None, True
    if isinstance(raw, dict):
        updated = _f(raw.get("now"), 0, 1e11)
        stale = not updated or time.time() - updated > 30
        if not stale:
            for a in (raw.get("aircraft") if isinstance(raw.get("aircraft"), list) else [])[:5000]:
                # Visitors: distance, bearing — and even the range check that drops far-away planes — come
                # from the snapped point only (a plane kept or dropped at exactly 450 km from the real spot
                # would be a measurement of it, repeated with every plane).
                c = clean(a, real if precise else shown, s["aircraft"]["max_range_km"])
                if c and (c["seen_s"] is None or c["seen_s"] <= 60):
                    planes.append(c)
                if len(planes) >= MAX_AIRCRAFT:
                    break
    planes.sort(key=lambda p: (p["dist_km"] is None, p["dist_km"] or 0, p["flight"] or p["hex"]))
    data = {"aircraft": planes, "count": len(planes), "updated": updated, "stale": stale, "origin": shown}
    with _lock:
        _cache[key] = (now, data)
    return data


# The radio service reports why the decoder stopped with the last line of readsb's log ("[2026-…]
# Abnormal exit. uptime: 0d 0h 0m 0.005s", "usb_open error -3", …). Phones get plain words instead;
# the raw line stays in the system log for the admin.
_DECODER_PREFIXES = ("The aircraft decoder stopped early:", "The aircraft decoder stopped (", "SDR 2 isn't working (")
_NO_DONGLE = re.compile(r"no (supported )?devices? found|usb_open|usb_claim|no such device|abnormal exit|"
                        r"rtlsdr|rtl-sdr|failed to open|uptime:|libusb|device not found", re.I)


def plain_message(msg: str) -> str:
    msg = str(msg or "")
    if not msg.startswith(_DECODER_PREFIXES):
        return msg[:300]
    retrying = "trying again" in msg
    if _NO_DONGLE.search(msg):
        text = ("The aircraft receiver couldn't find its radio dongle. Check that the SDR stick is plugged in "
                "(and its antenna attached)")
    else:
        text = "The aircraft receiver stopped unexpectedly"
    return text + ("; it will try again shortly." if retrying else ", then try again.")


def radio(ctx, family: bool = False) -> dict:
    st = C.read_status()
    air = st.get("aircraft") if isinstance(st.get("aircraft"), dict) else {}
    paused = st.get("weather_radio") == "paused"
    return {"running": bool(st.get("running")), "weather_radio": st.get("weather_radio"),
            "paused_by": st.get("by") if paused else None, "paused_until": st.get("until") if paused else None,
            "state": air.get("state", "off"), "device": air.get("device"), "until": air.get("until"),
            "by": air.get("by") if family else None, "message": plain_message(air.get("message")), "request": air.get("request"),
            "installed": air.get("installed"), "always_on": bool(air.get("always_on")),
            "scans_left": air.get("scans_left"), "scan_minutes": air.get("scan_minutes", 15),
            "dedicated": bool(air.get("dedicated")), **C.sdr2_info(st)}


def get_aircraft(req, ctx, args):
    s = C.settings(ctx.raw_config())
    if not _rate_ok(args["ip"], s["aircraft"]["requests_per_min"]):
        return 429, {"error": "too many requests — the list refreshes by itself every few seconds"}, {"Retry-After": "10"}
    family = bool(args.get("member") or args.get("admin"))
    return dict(snapshot(ctx, precise=family), radio=radio(ctx, family))


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
