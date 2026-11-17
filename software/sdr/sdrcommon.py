"""
sdrcommon — settings, paths and the small shared files of the radio features (stdlib only).

Used by sdrd (the radio scheduler), tlefile.py, and the portal's aircraft/satellites features,
so all of them read the same settings with the same defaults.

Files (run dir = /run/prepper/sdr, shared with sdrlease; state dir = /var/lib/prepper/sdr):
  run/status.json        sdrd → everyone: weather radio listening/paused, aircraft, satellites
  run/aircraft.cmd       portal → sdrd: {"action": "scan"|"stop", "id", "by", "minutes", "at"}
  run/readsb/aircraft.json   readsb's own output
  run/schedule.json      sdrd → portal: upcoming passes and what will happen to each
  state/tle.txt          fetched orbit data (tlefile.py); state/tle-upload.txt: pasted by an admin
  state/images/          satellite pictures + index.json
  state/ledger.json      sdrd's daily usage (satellite minutes, aircraft scans, last loan)
  state/position.json    last good GPS fix (predictions keep working without a fix)
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import socket
import time
import tomllib
from pathlib import Path

RUN = Path(os.environ.get("PREPPER_SDR_DIR", "/run/prepper/sdr"))
STATE = Path(os.environ.get("PREPPER_SDR_STATE", "/var/lib/prepper/sdr"))
CONFIG = Path(os.environ.get("PREPPER_CONFIG", "/etc/prepper/prepper.toml"))
ALERTS = Path(os.environ.get("ALERTS_FILE", "/run/prepper/alerts.json"))
ALERTD_BEAT = Path(os.environ.get("ALERTD_BEAT", "/run/prepper/alertd.ok"))
GPSD = os.environ.get("PREPPER_GPSD", "127.0.0.1:2947")

DEFAULT_SATS = [
    {"name": "METEOR-M2 3", "norad": 57166, "freq_mhz": 137.9, "pipeline": "meteor_m2-x_lrpt"},
    {"name": "METEOR-M2 4", "norad": 59051, "freq_mhz": 137.9, "pipeline": "meteor_m2-x_lrpt"},
]


def run_dir() -> Path:
    return Path(os.environ.get("PREPPER_SDR_DIR", str(RUN)))


def state_dir() -> Path:
    return Path(os.environ.get("PREPPER_SDR_STATE", str(STATE)))


def load_config(path: Path | None = None) -> dict:
    p = path or Path(os.environ.get("PREPPER_CONFIG", str(CONFIG)))
    try:
        return tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def read_json(path: Path, default=None, limit: int = 4_000_000):
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit + 1)
        if len(raw) > limit:
            return default
        return json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return default


def atomic_write(path: Path, text: str, mode: int = 0o640) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ───────────────────────────── settings ──────────────────────────────────────

def _num(d: dict, key: str, default: float, lo: float, hi: float) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return min(hi, max(lo, v))


def _bool(d: dict, key: str, default: bool) -> bool:
    v = d.get(key, default)
    return v if isinstance(v, bool) else default


def _str(d: dict, key: str, default: str, limit: int = 200) -> str:
    v = d.get(key, default)
    return str(v)[:limit] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else default


ANTENNAS = ("adsb", "satellite", "both")
ANTENNA_TEXT = {"adsb": "the 1090 MHz aircraft whip", "satellite": "the 137 MHz V-dipole (outdoors)",
                "both": "a wideband antenna (discone)"}


def _dev(v):
    """A dongle setting: a serial / "auto" / "none" (string) or a device index (int)."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v if 0 <= v < 64 else "auto"
    return "".join(c for c in str(v)[:32] if c.isprintable()) if isinstance(v, str) else "auto"


def antenna_file() -> Path:
    return state_dir() / "antenna.json"


def antenna(radio: dict | None = None) -> str:
    """Which antenna is on SDR 2: an admin's choice in the launcher (state/antenna.json) beats
    [radio] sdr2_antenna; default "adsb" (the Badger ships with the 1090 MHz whip fitted)."""
    o = read_json(antenna_file(), None, limit=4096)
    if isinstance(o, dict) and o.get("antenna") in ANTENNAS:
        return o["antenna"]
    v = str((radio or {}).get("sdr2_antenna", "adsb")).strip().lower()
    return v if v in ANTENNAS else "adsb"


def sdr2_info(st: dict) -> dict:
    """The SDR 2 part of a status (for the pages and the front panel), validated."""
    d = st.get("dongles") if isinstance(st.get("dongles"), dict) else {}
    ant = d.get("antenna") if d.get("antenna") in ANTENNAS else antenna()
    state = d.get("sdr2_state") if d.get("sdr2_state") in ("ok", "missing", "failed", "off") else None
    return {"sdr2_state": state, "antenna": ant, "antenna_text": ANTENNA_TEXT[ant],
            "fallback": bool(d.get("fallback")) if state else None,
            "sdr2_why": str(d.get("sdr2_why") or "")[:200] or None,
            "problem": str(d.get("problem") or "")[:300] or None, "note": str(d.get("note") or "")[:300] or None}


def set_antenna(value: str, by: str = "") -> bool:
    if value not in ANTENNAS:
        return False
    atomic_write(antenna_file(), json.dumps({"antenna": value, "by": str(by)[:40], "at": time.time()}), 0o640)
    return True


def satellites_list(raw_list) -> list[dict]:
    """[[satellites.list]] entries, validated; the Meteor-M defaults when none are given."""
    out = []
    if isinstance(raw_list, list):
        for s in raw_list[:12]:
            if not isinstance(s, dict):
                continue
            norad = s.get("norad")
            if isinstance(norad, bool) or not isinstance(norad, int) or not 0 < norad < 1_000_000:
                continue
            freq = _num(s, "freq_mhz", 137.9, 130.0, 2000.0)
            pipe = _str(s, "pipeline", "meteor_m2-x_lrpt", 60)
            if not all(c.isalnum() or c in "_-" for c in pipe):
                continue
            name = "".join(c for c in _str(s, "name", f"NORAD {norad}", 40) if c.isprintable())
            out.append({"name": name or f"NORAD {norad}", "norad": norad, "freq_mhz": freq, "pipeline": pipe})
    return out or [dict(s) for s in DEFAULT_SATS]


def settings(raw: dict | None = None) -> dict:
    """Every radio-feature setting with its default (works with no config at all)."""
    raw = raw if isinstance(raw, dict) else load_config()
    radio = raw.get("radio") if isinstance(raw.get("radio"), dict) else {}
    air = raw.get("aircraft") if isinstance(raw.get("aircraft"), dict) else {}
    sat = raw.get("satellites") if isinstance(raw.get("satellites"), dict) else {}
    feats = raw.get("features") if isinstance(raw.get("features"), dict) else {}
    tm = raw.get("time") if isinstance(raw.get("time"), dict) else {}
    cap = sat.get("capture", "auto")
    if cap not in (True, False, "auto"):
        cap = "auto"
    return {
        "radio": {
            "weather_sdr": _dev(radio.get("weather_sdr", "auto")),
            "sdr2": _dev(radio.get("sdr2", "auto")),
            "sdr2_antenna": antenna(radio),
            "sdr_lend": _bool(radio, "sdr_lend", True),
            "sdr_weather": _str(radio, "sdr_weather", "auto", 10),
            "lend_gap_min": _num(radio, "sdr_lend_gap_min", 10, 0, 240),
        },
        "aircraft": {
            "enabled": feats.get("aircraft", True) is not False,
            "scan_minutes": int(_num(air, "scan_minutes", 15, 1, 30)),
            "scans_per_day": int(_num(air, "scans_per_day", 4, 0, 48)),
            "readsb": _str(air, "readsb", "/usr/local/bin/readsb"),
            "gain": _str(air, "gain", "auto", 10),
            "max_range_km": _num(air, "max_range_km", 450, 10, 1000),
            "requests_per_min": int(_num(air, "requests_per_min", 60, 5, 600)),
        },
        "satellites": {
            "enabled": feats.get("satellites", True) is not False,
            "capture": cap,
            "fallback_capture": _bool(sat, "fallback_capture", False),
            "min_elevation": _num(sat, "min_elevation", 30, 0, 90),
            "min_elevation_dedicated": _num(sat, "min_elevation_dedicated", 15, 0, 90),
            "record_above": _num(sat, "record_above", 10, 0, 60),
            "daily_max_min": _num(sat, "daily_max_min", 45, 0, 600),
            "keep_images": int(_num(sat, "keep_images", 30, 1, 500)),
            "board_post": _bool(sat, "board_post", False),
            "satdump": _str(sat, "satdump", "/usr/local/bin/satdump"),
            "gain": _num(sat, "gain", 40, 0, 50),
            "samplerate": _num(sat, "samplerate", 1.024e6, 2.5e5, 3.2e6),
            "tle_url": _str(sat, "tle_url", "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=TLE", 300),
            "tle_stale_days": _num(sat, "tle_stale_days", 14, 1, 365),
            "tle_max_days": _num(sat, "tle_max_days", 60, 1, 3650),
            "prefer_image": ([x[:40].lower() for x in sat["prefer_image"] if isinstance(x, str) and x][:10]
                             if isinstance(sat.get("prefer_image"), list) else ["221", "rgb", "corrected"]),
            "list": satellites_list(sat.get("list")),
        },
        "home": {"lat": tm.get("latitude"), "lon": tm.get("longitude"),
                 "alt_m": _num(sat, "altitude_m", 0, -500, 9000)},
    }


# ───────────────────────────── status ────────────────────────────────────────

def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError, OverflowError):
        return False
    except PermissionError:
        return True
    return int(pid) > 0


def write_status(st: dict) -> None:
    st = dict(st, updated=time.time(), pid=os.getpid())
    atomic_write(run_dir() / "status.json", json.dumps(st), 0o644)


def read_status(max_age: float = 30.0) -> dict:
    """sdrd's status, or a 'not running' one if it is missing, stale, or its writer is gone.
    A paused weather radio whose `until` has passed is reported as listening again."""
    st = read_json(run_dir() / "status.json", None)
    now = time.time()
    if (not isinstance(st, dict) or not isinstance(st.get("updated"), (int, float))
            or now - st["updated"] > max_age or not pid_alive(st.get("pid"))):
        return {"running": False, "weather_radio": weather_listening_state(),
                "aircraft": {"state": "off"}, "satellites": {"state": "off"}}
    st["running"] = True
    if st.get("weather_radio") == "paused":
        until = st.get("until")
        if not isinstance(until, (int, float)) or until < now - 60:
            st["weather_radio"], st["by"], st["until"] = weather_listening_state(), None, None
    return st


def weather_listening_state() -> str:
    """listening (alertd's heartbeat is fresh) or off."""
    try:
        return "listening" if time.time() - ALERTD_BEAT.stat().st_mtime < 90 else "off"
    except OSError:
        return "off"


def alerts_live(path: Path | None = None) -> list[dict]:
    now = time.time()
    a = read_json(path or Path(os.environ.get("ALERTS_FILE", str(ALERTS))), []) or []
    return [x for x in a if isinstance(x, dict) and isinstance(x.get("expires_ts"), (int, float))
            and x["expires_ts"] > now] if isinstance(a, list) else []


# ───────────────────────────── position ──────────────────────────────────────

def _valid_pos(lat, lon) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon)) \
        and -90 <= lat <= 90 and -180 <= lon <= 180


def gpsd_fix(addr: str | None = None, timeout: float = 2.5) -> dict | None:
    """One TPV with a 2D/3D fix from gpsd, or None."""
    host, _, port = (addr or GPSD).rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=1.0) as s:
            s.sendall(b'?WATCH={"enable":true,"json":true}\n')
            s.settimeout(1.0)
            buf, deadline = b"", time.monotonic() + timeout
            while time.monotonic() < deadline and len(buf) < 65536:
                try:
                    chunk = s.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                buf += chunk
                for line in buf.split(b"\n"):
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(m, dict) and m.get("class") == "TPV" and (m.get("mode") or 0) >= 2 \
                            and _valid_pos(m.get("lat"), m.get("lon")):
                        alt = m.get("altHAE", m.get("alt"))
                        return {"lat": m["lat"], "lon": m["lon"],
                                "alt_m": alt if isinstance(alt, (int, float)) and math.isfinite(alt) else 0.0}
    except (OSError, ValueError):
        return None
    return None


_pos_cache: dict = {}


def position(home: dict | None = None, ttl: float = 30.0, use_gps: bool = True) -> dict | None:
    """Where the Badger is: GPS → last saved GPS fix → [time] latitude/longitude. {lat, lon, alt_m, source}."""
    now = time.monotonic()
    if _pos_cache.get("at") and now - _pos_cache["at"] < ttl:
        return _pos_cache["pos"]
    pos = None
    fix = gpsd_fix() if use_gps else None
    saved_path = state_dir() / "position.json"
    if fix:
        pos = dict(fix, source="gps")
        old = read_json(saved_path, None)
        fresh = (isinstance(old, dict) and _valid_pos(old.get("lat"), old.get("lon"))
                 and isinstance(old.get("at"), (int, float)) and time.time() - old["at"] < 86400
                 and abs(old["lat"] - fix["lat"]) < 0.01 and abs(old["lon"] - fix["lon"]) < 0.01)
        if not fresh:
            with contextlib.suppress(OSError):
                atomic_write(saved_path, json.dumps(dict(fix, at=time.time())), 0o640)
    if not pos:
        old = read_json(saved_path, None)
        if isinstance(old, dict) and _valid_pos(old.get("lat"), old.get("lon")):
            alt = old.get("alt_m", 0.0)
            pos = {"lat": old["lat"], "lon": old["lon"], "alt_m": alt if isinstance(alt, (int, float)) else 0.0,
                   "source": "last gps fix"}
    if not pos and home and _valid_pos(home.get("lat"), home.get("lon")):
        pos = {"lat": float(home["lat"]), "lon": float(home["lon"]), "alt_m": float(home.get("alt_m") or 0.0),
               "source": "settings"}
    _pos_cache.update(at=now, pos=pos)
    return pos


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def compass(deg: float) -> str:
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg % 360) / 45.0 + 0.5) % 8]
