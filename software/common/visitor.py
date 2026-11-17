"""
visitor — what someone on the Badger's Wi-Fi without a family key or the admin login may learn.

One rule for every public page (status, time and sun, aircraft, satellites, …): "miles is fine, it
just can't be 100 yards NNW".

  * The Badger's position is SNAPPED to a fixed grid — deterministic, never random noise (jitter
    averages out over repeated requests and converges on the house; a grid gives the same answer
    every time, so asking a thousand times teaches nothing new).
  * Cells are at least ~10 km on a side: 0.1° of latitude (11.1 km) by a whole number of 0.1° steps
    of longitude, as many as it takes for the cell to be ≥ 10 km wide at that latitude (0.1° at the
    equator, 0.2° at 40°, 0.3° at 70°). The snapped point is the cell centre: lat rounded to 0.1°,
    lon rounded to the row's step.
  * Everything visitor-facing (sun times, map centre, aircraft and satellite distances, bearings,
    pass times, solar time, "origin") is computed ONLY from the snapped point, so no combination of
    fields can be inverted to anything finer than the cell. Sun times are given to the minute.
  * The household's power, occupancy and uplink stay private ("grid down, 3 h of battery left,
    2 phones home" is what a raider wants to know).

Family members, the admin and the front panel get the precise data; callers decide that and call
these helpers only for visitors. Standard library only.
"""
from __future__ import annotations

import math

DECIMALS = 1                     # latitude rows: 0.1° ≈ 11.1 km
CELL_KM = 10.0                   # cells are at least this wide east-west too
_KM_PER_DEG = 111.195


def lon_step(lat_snapped: float) -> float:
    """Longitude width of the cells in the row at this (snapped) latitude: n × 0.1°, the smallest n
    that makes the cell ≥ CELL_KM wide (rows near the poles: one cell per 30°, i.e. still a grid)."""
    width_01 = 0.1 * _KM_PER_DEG * math.cos(math.radians(min(abs(lat_snapped), 89.9)))
    n = max(1, math.ceil(CELL_KM / width_01 - 1e-9)) if width_01 > 0 else 300
    return min(n, 300) / 10.0


# /api/status keys that describe the household rather than the library: battery, mains or solar,
# runtime left (power), how many devices are on the Wi-Fi (clients), how the Badger reaches the
# internet (uplink), how long since it last lost power (uptime), and the household's security posture
# (whether it is locked down is the family's business, not a passer-by's).
PRIVATE_STATUS_KEYS = ("power", "clients", "uplink", "uptime_s", "posture", "version")   # version: don't help attackers pick exploits


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def coarse(lat, lon) -> tuple[float, float] | None:
    """(lat, lon) snapped to the visitor grid (the cell centre), or None if not usable numbers."""
    a, o = _num(lat), _num(lon)
    if a is None or o is None or abs(a) > 90 or abs(o) > 180:
        return None
    la = round(a, DECIMALS)
    step = lon_step(la)
    lo = round(round(o / step) * step, 6)
    if lo > 180:
        lo -= 360
    elif lo < -180:
        lo += 360
    return la, round(lo, 6) + 0.0


def coarse_position(pos: dict | None, lat_key: str = "lat", lon_key: str = "lon") -> dict | None:
    """A position dict for visitors: only the rounded coordinates (+ the GPS mode / source label,
    which say nothing about where), marked "coarse"."""
    if not isinstance(pos, dict):
        return None
    c = coarse(pos.get(lat_key), pos.get(lon_key))
    if c is None:
        return None
    out = {lat_key: c[0], lon_key: c[1], "coarse": True}
    for k in ("mode", "source"):
        if k in pos and isinstance(pos[k], (int, str)) and not isinstance(pos[k], bool):
            out[k] = pos[k] if isinstance(pos[k], int) else str(pos[k])[:20]
    return out


def round_time(ts, step: int = 60) -> float | None:
    """A timestamp (epoch seconds) to the nearest `step` seconds."""
    t = _num(ts)
    return None if t is None else float(round(t / step) * step)


def round_angle(v) -> float | None:
    a = _num(v)
    return None if a is None else float(round(a))


def status_view(st: dict) -> dict:
    """/api/status as a visitor sees it: no power/occupancy/uplink, the position rounded."""
    out = dict(st)
    for k in PRIVATE_STATUS_KEYS:
        out[k] = None
    out["gps"] = coarse_position(st.get("gps"))
    return out


def round_iso_minute(v):
    """An ISO-8601 time to the minute (sun times); anything else → unchanged/None."""
    import datetime as _dt
    if not isinstance(v, str):
        return v if v is None else None
    try:
        t = _dt.datetime.fromisoformat(v)
    except ValueError:
        return None
    t = (t + _dt.timedelta(seconds=30)).replace(second=0, microsecond=0)
    return t.isoformat()


def sun_view(ev: dict | None) -> dict | None:
    """Sun events (already computed from the snapped point) with every time to the minute."""
    if not isinstance(ev, dict):
        return None
    return {k: (round_iso_minute(v) if k in ("solarNoon", "sunrise", "sunset") else v) for k, v in ev.items()}
