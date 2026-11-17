#!/usr/bin/env python3
"""
nutime — Ke-Shi / Nu:Time™ conversion core for Honey Badger.

  1 day = 10 gongs · 1 gong = 100 beats · 1 beat = 100 ticks
  1 day = 1,000 beats = 100,000 ticks · beat = 86.4 s · tick = 0.864 s
  display GG:BB:TT  (gong 00–09, beat 00–99, tick 00–99)

Layers (kept separate, per the architecture handoff):
  instant      absolute UTC; the only source of truth
  reference    which day the fraction is taken of: utc | civil | solar | apparent
  conversion   integer arithmetic only — no float drift at tick boundaries
  locale       labels only; durations and field order never change
  solar        Sun altitude/azimuth/sunrise/sunset from the UTC instant + lat/lon

Standard library only (Python 3.9+ for zoneinfo).

  python3 nutime.py                          # now, UTC reference
  python3 nutime.py --mode civil --tz Europe/Helsinki --lat 60.17 --lon 24.94
  python3 nutime.py --at 2026-11-16T12:00:00Z --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from zoneinfo import ZoneInfo

SCHEMA = "nu-time/v1"
TICKS_PER_DAY = 100_000
NOMINAL_DAY_US = 86_400_000_000          # microseconds in a nominal 86,400 s day
MODES = ("utc", "civil", "solar", "apparent")
DST_POLICIES = ("wall", "elapsed")

# Labels only — §4 of the handoff. Durations and GG:BB:TT order are identical everywhere.
LABELS = {
    "zh": {"gong": "更", "beat": "分", "tick": "刻", "gong_latin": "gēng", "beat_latin": "fēn", "tick_latin": "kè"},
    "en": {"gong": "gong", "beat": "beat", "tick": "tick"},
    "fr": {"gong": "gong", "beat": "beat", "tick": "tic"},
    "es": {"gong": "gong", "beat": "beat", "tick": "tic"},
    "de": {"gong": "gong", "beat": "beat", "tick": "tick"},
}


# ───────────────────────────── conversion core ──────────────────────────────

def fields_from_ticks(ticks: int) -> dict:
    ticks %= TICKS_PER_DAY
    gong, rem = divmod(ticks, 10_000)
    beat, tick = divmod(rem, 100)
    return {"ticksOfDay": ticks, "gong": gong, "beat": beat, "tick": tick,
            "beatsOfDay": ticks / 100, "display": f"{gong:02d}:{beat:02d}:{tick:02d}"}


def ticks_from_us(us_into_day: int, day_us: int = NOMINAL_DAY_US) -> int:
    """Whole ticks elapsed: floor(us * 100000 / day_us), exact in integers, truncating."""
    us_into_day %= day_us
    return us_into_day * TICKS_PER_DAY // day_us


def from_seconds(seconds_since_midnight: float, day_seconds: float = 86_400) -> dict:
    """Convenience for tests and callers holding plain seconds (rounded to the microsecond)."""
    return fields_from_ticks(ticks_from_us(round(seconds_since_midnight * 1_000_000), round(day_seconds * 1_000_000)))


def parse(display: str) -> dict:
    """GG:BB:TT → fields + SI seconds since the reference midnight (inverse conversion)."""
    g, b, t = (int(x) for x in display.split(":"))
    if not (0 <= g <= 9 and 0 <= b <= 99 and 0 <= t <= 99):
        raise ValueError(f"out of range: {display}")
    ticks = g * 10_000 + b * 100 + t
    return {**fields_from_ticks(ticks), "secondsOfDay": ticks * 0.864}


# ───────────────────────────── solar model ──────────────────────────────────
# NOAA "General Solar Position Calculations" (fractional-year form). Good to about
# a minute for sunrise/sunset and a few tenths of a degree in altitude — fine for
# a clock face and sky colour; use a full ephemeris for navigation-grade work.

def _solar_terms(instant: dt.datetime) -> tuple[float, float]:
    u = instant.astimezone(dt.timezone.utc)
    days_in_year = 366 if (u.year % 4 == 0 and (u.year % 100 != 0 or u.year % 400 == 0)) else 365
    hour = u.hour + u.minute / 60 + (u.second + u.microsecond / 1e6) / 3600
    g = 2 * math.pi / days_in_year * (u.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))          # minutes
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))  # radians
    return eqtime, decl


def equation_of_time_minutes(instant: dt.datetime) -> float:
    return _solar_terms(instant)[0]


def check_coords(lat: float | None, lon: float | None) -> None:
    """ValueError for a latitude/longitude that isn't a finite number in range.
    (NaN and ±inf used to escape as OverflowError from deep in the maths, and
    lat=200 silently produced a plausible-looking but meaningless record.)"""
    for name, v, limit in (("latitude", lat, 90), ("longitude", lon, 180)):
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > limit:
            raise ValueError(f"{name} must be a number between -{limit} and {limit}, not {v!r}")


def sun_position(instant: dt.datetime, lat: float, lon: float) -> dict:
    """Altitude/azimuth in degrees (azimuth clockwise from north), no refraction."""
    check_coords(lat, lon)
    eqtime, decl = _solar_terms(instant)
    u = instant.astimezone(dt.timezone.utc)
    tst = u.hour * 60 + u.minute + (u.second + u.microsecond / 1e6) / 60 + eqtime + 4 * lon
    ha = math.radians(tst / 4 - 180)
    phi = math.radians(lat)
    cos_z = max(-1.0, min(1.0, math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)))
    zen = math.acos(cos_z)
    alt = 90 - math.degrees(zen)
    az = math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi))) + 180
    return {"altitude": round(alt, 3), "azimuth": round(az % 360, 3)}


def sun_events(date: dt.date, lat: float, lon: float, zenith: float = 90.833) -> dict:
    """Sunrise/sunset (as UTC instants) on a calendar date *at the place*, or polar day/night.

    `date` is the local calendar date there: the formula puts solar noon at
    12:00 − lon/15 h after that date's 00:00 UTC, which lands on the same local
    day for every longitude. Use sun_events_at() to go from an instant + zone.
    zenith 90.833° = sunrise/sunset (refraction + solar disc); 96° = civil twilight."""
    check_coords(lat, lon)
    noon = dt.datetime(date.year, date.month, date.day, 12, tzinfo=dt.timezone.utc)
    noon -= dt.timedelta(minutes=4 * lon)            # solar terms at (roughly) local noon
    eqtime, decl = _solar_terms(noon)
    noon = dt.datetime(date.year, date.month, date.day, 12, tzinfo=dt.timezone.utc)
    phi = math.radians(lat)
    x = math.cos(math.radians(zenith)) / (math.cos(phi) * math.cos(decl)) - math.tan(phi) * math.tan(decl)
    solar_noon = noon.replace(hour=0) + dt.timedelta(minutes=720 - 4 * lon - eqtime)
    if x < -1:
        return {"polar": "day", "solarNoon": solar_noon.isoformat(), "sunrise": None, "sunset": None}
    if x > 1:
        return {"polar": "night", "solarNoon": solar_noon.isoformat(), "sunrise": None, "sunset": None}
    ha = math.degrees(math.acos(x))
    rise = noon.replace(hour=0) + dt.timedelta(minutes=720 - 4 * (lon + ha) - eqtime)
    sset = noon.replace(hour=0) + dt.timedelta(minutes=720 - 4 * (lon - ha) - eqtime)
    return {"polar": None, "solarNoon": solar_noon.isoformat(), "sunrise": rise.isoformat(), "sunset": sset.isoformat()}


def local_date(instant: dt.datetime, tz: str | None = None, lon: float | None = None) -> dt.date:
    """Calendar date at the place: in its IANA zone, else its mean-solar date from longitude, else UTC."""
    u = instant.astimezone(dt.timezone.utc)
    if tz:
        return u.astimezone(ZoneInfo(tz)).date()
    if lon is not None:
        check_coords(None, lon)
        return (u + dt.timedelta(hours=lon / 15)).date()
    return u.date()


def sun_events_at(instant: dt.datetime, lat: float, lon: float, tz: str | None = None,
                  zenith: float = 90.833) -> dict:
    """Today's sun events at the place — "today" meaning its local date, not the UTC date
    (at 22:00 in Denver the UTC date is already tomorrow)."""
    return sun_events(local_date(instant, tz, lon), lat, lon, zenith)


# ───────────────────────────── reference layer ──────────────────────────────

def _us_since_utc_midnight(u: dt.datetime) -> int:
    return ((u.hour * 60 + u.minute) * 60 + u.second) * 1_000_000 + u.microsecond


def reference_position(instant: dt.datetime, mode: str = "utc", *, tz: str | None = None,
                       lon: float | None = None, dst_policy: str = "wall",
                       quantize_longitude: bool = False) -> tuple[int, int, dict]:
    """→ (microseconds into the reference day, length of that day in µs, notes)."""
    u = instant.astimezone(dt.timezone.utc)
    if mode == "utc":
        return _us_since_utc_midnight(u), NOMINAL_DAY_US, {}
    if mode == "civil":
        if not tz:
            raise ValueError("civil mode needs an IANA time zone")
        zone = ZoneInfo(tz)
        local = u.astimezone(zone)
        if dst_policy == "wall":
            # Fraction of a nominal 24 h of *wall-clock* time (handoff's recommended policy).
            # On DST days the display skips or repeats, exactly as wall clocks do.
            us = ((local.hour * 60 + local.minute) * 60 + local.second) * 1_000_000 + local.microsecond
            return us, NOMINAL_DAY_US, {"dstPolicy": "wall"}
        # "elapsed": fraction of the actual civil day (23 h or 25 h on transition days).
        mid = dt.datetime.combine(local.date(), dt.time(0), tzinfo=zone)
        nxt = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(0), tzinfo=zone)
        start, end = mid.astimezone(dt.timezone.utc), nxt.astimezone(dt.timezone.utc)
        day_us = round((end - start).total_seconds() * 1_000_000)
        us = round((u - start).total_seconds() * 1_000_000)
        return us, day_us, {"dstPolicy": "elapsed", "civilDayHours": day_us / 3.6e9}
    if mode in ("solar", "apparent"):
        if lon is None:
            raise ValueError(f"{mode} mode needs a longitude")
        eff_lon = round(lon / 3.6) * 3.6 if quantize_longitude else lon
        offset_us = round(eff_lon / 15 * 3600 * 1_000_000)            # mean solar time
        notes = {"longitudeUsed": eff_lon}
        if mode == "apparent":
            eot = equation_of_time_minutes(u)
            offset_us += round(eot * 60 * 1_000_000)                   # true (apparent) solar time
            notes["equationOfTimeMinutes"] = round(eot, 3)
        return (_us_since_utc_midnight(u) + offset_us) % NOMINAL_DAY_US, NOMINAL_DAY_US, notes
    raise ValueError(f"mode must be one of {MODES}")


def nu_time(instant: dt.datetime | None = None, mode: str = "utc", *, lat: float | None = None,
            lon: float | None = None, tz: str | None = None, locale: str = "en",
            dst_policy: str = "wall", quantize_longitude: bool = False, with_sun: bool = True) -> dict:
    """A nu-time/v1 record for one instant. The instant, reference and location are always explicit."""
    instant = instant or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware (UTC recommended)")
    check_coords(lat, lon)
    us, day_us, notes = reference_position(instant, mode, tz=tz, lon=lon, dst_policy=dst_policy,
                                           quantize_longitude=quantize_longitude)
    rec = {
        "schema": SCHEMA,
        "instant": instant.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "reference": {"utc": "UTC", "civil": "civil-local", "solar": "solar-mean", "apparent": "solar-apparent"}[mode],
        "location": {"latitude": lat, "longitude": lon, "ianaTimeZone": tz},
        **fields_from_ticks(ticks_from_us(us, day_us)),
        "locale": locale if locale in LABELS else "en",
        "labels": LABELS.get(locale, LABELS["en"]),
    }
    if notes:
        rec["notes"] = notes
    if with_sun and lat is not None and lon is not None:
        rec["sun"] = sun_position(instant, lat, lon)
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ke-Shi / Nu:Time")
    ap.add_argument("--at", help="ISO instant, e.g. 2026-11-16T12:00:00Z (default: now)")
    ap.add_argument("--mode", choices=MODES, default="utc")
    ap.add_argument("--tz"); ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--locale", default="en", choices=sorted(LABELS))
    ap.add_argument("--dst-policy", choices=DST_POLICIES, default="wall")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    at = dt.datetime.fromisoformat(a.at.replace("Z", "+00:00")) if a.at else None
    rec = nu_time(at, a.mode, lat=a.lat, lon=a.lon, tz=a.tz, locale=a.locale, dst_policy=a.dst_policy)
    if a.json:
        print(json.dumps(rec, indent=2, ensure_ascii=False))
    else:
        L = rec["labels"]
        print(f"{rec['display']}  ({rec['reference']})  {rec['gong']} {L['gong']} · {rec['beat']} {L['beat']} · "
              f"{rec['tick']} {L['tick']}  — beat {rec['beatsOfDay']:.2f} of 1000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
