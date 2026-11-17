"""
satplan — which weather-satellite passes get recorded, and why the others don't (pure functions).

predict() lists the passes over the Badger; plan() applies the sharing policy:

  SDR 2 (standard) — with the satellite (or a wideband) antenna fitted, every pass
      >= min_elevation_dedicated (15°); with the aircraft whip only if [satellites] capture = true.
      Overlapping passes: the higher one wins.
  fallback, the weather dongle (SDR 2 missing/failed) — opt-in ([satellites] fallback_capture = true):
      only passes whose highest point is >= min_elevation (30°), recording only the part above
      record_above (10°), at most daily_max_min (45) minutes a day, at least [radio]
      sdr_lend_gap_min (10) minutes of listening between loans, never while an alert is live
      (alertd refuses too), highest passes first when they compete.

Each planned pass carries a `decision` ("record", "skip", "off") and a plain-language `reason`.
"""
from __future__ import annotations

import time

import orbit

PAD_S = 30                  # start a little early, stop a little late
STALE_PAD_S = 120           # more when the orbit data is old (predictions drift)
MAX_RECORD_S = 20 * 60


def hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def day_of(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def predict(sats: dict, sats_cfg: list[dict], pos: dict, start: float, hours: float = 24.0,
            min_elev: float = 5.0, record_above: float = 10.0) -> list[dict]:
    """Passes of every configured satellite with orbit data, sorted by rise time."""
    out = []
    for cfg in sats_cfg:
        sat = sats.get(cfg["norad"])
        if not sat:
            continue
        for p in orbit.passes(sat, pos["lat"], pos["lon"], pos.get("alt_m") or 0.0, start, hours, min_elev=min_elev):
            win = orbit.window_above(sat, p, pos["lat"], pos["lon"], pos.get("alt_m") or 0.0, record_above)
            out.append(dict(p, name=cfg["name"], norad=cfg["norad"], freq_mhz=cfg["freq_mhz"], pipeline=cfg["pipeline"],
                            rec_start=round(win[0], 1) if win else None, rec_end=round(win[1], 1) if win else None))
    out.sort(key=lambda p: p["aos"])
    return out


def capture_mode(s: dict, assignment: dict, sdr2_ok: bool | None = None) -> dict:
    """{"device": "sdr2" | "weather" | None, "why": plain text} from settings, the antenna on SDR 2 and
    which dongles work. sdr2_ok: SDR 2 present and not failing (default: present)."""
    sat, radio = s["satellites"], s["radio"]
    ant = radio.get("sdr2_antenna", "adsb")
    if sdr2_ok is None:
        sdr2_ok = bool((assignment.get("sdr2") or {}).get("present"))
    if not sat["enabled"]:
        return {"device": None, "why": "The weather-satellite feature is turned off."}
    if sat["capture"] is False:
        return {"device": None, "why": "Recording is turned off ([satellites] capture = false). Pass times are still shown."}
    if sdr2_ok:
        lo = f"{sat['min_elevation_dedicated']:g}°"
        if ant in ("satellite", "both"):
            return {"device": "sdr2", "why": f"Every pass higher than {lo} is recorded on SDR 2"
                    + (" (aircraft pause during each pass)." if ant == "both" else ".") + " Weather alerts are not affected."}
        if sat["capture"] is True:
            return {"device": "sdr2", "why": f"Passes higher than {lo} are recorded on SDR 2 with the aircraft whip "
                    "(aircraft pause during each pass). The whip hears 137 MHz poorly, so expect noisy pictures; "
                    "the V-dipole does much better."}
        return {"device": None, "why": "SDR 2 has the aircraft whip fitted, so passes are not recorded. To record them, "
                "set up the V-dipole outdoors on SDR 2 and choose “Satellite antenna” below."}
    # fallback: SDR 2 missing or not working — only the weather radio is left
    if sat["fallback_capture"]:
        if not radio["sdr_lend"]:
            return {"device": None, "why": "SDR 2 isn't working, and the weather radio is set not to lend its dongle "
                                           "([radio] sdr_lend = false)."}
        if assignment.get("weather", {}).get("present") is False:
            return {"device": None, "why": "No radio is plugged in."}
        return {"device": "weather", "why": f"SDR 2 isn't working, so high passes (above {sat['min_elevation']:g}°) are "
                                            f"recorded on the weather radio, up to {sat['daily_max_min']:g} minutes a day. "
                                            "Weather alerts pause while a pass is recorded."}
    return {"device": None, "why": "SDR 2 is missing or not working, so passes are not recorded. Recording on the weather "
                                   "radio instead is opt-in, because weather alerts pause meanwhile "
                                   "([satellites] fallback_capture = true)."}


def plan(passes: list[dict], s: dict, mode: dict, ledger_days: dict, now: float, tle_rows: list[dict],
         busy: list[tuple[float, float]] | None = None, last_lend_end: float | None = None,
         done: list[dict] | None = None) -> list[dict]:
    """Annotate each upcoming pass with decision/reason and its recording window (start/end).
    ledger_days: {"YYYY-MM-DD": {"sat_s": seconds already recorded on the shared dongle}}.
    busy: windows the device is already taken (a running aircraft scan). done: passes already handled."""
    sat = s["satellites"]
    gap = s["radio"]["lend_gap_min"] * 60.0
    rows = {r["norad"]: r for r in tle_rows}
    dev = mode["device"]
    out, cands = [], []
    for p in passes:
        e = dict(p, decision="skip", reason="", start=None, end=None, device=dev)
        row = rows.get(p["norad"], {})
        e["approximate"] = bool(row.get("approximate"))
        end_ref = p["rec_end"] or p["los"]
        if end_ref < now:
            continue
        if done and any(d.get("norad") == p["norad"] and abs(d.get("tca", 0) - p["tca"]) < 300 for d in done):
            hit = next(d for d in done if d.get("norad") == p["norad"] and abs(d.get("tca", 0) - p["tca"]) < 300)
            e["decision"], e["reason"] = "done", str(hit.get("label") or "Already handled.")
            out.append(e)
            continue
        if dev is None:
            e["decision"], e["reason"] = "off", mode["why"]
        elif row.get("too_old"):
            e["reason"] = "The orbit data is too old to plan a recording. Connect the Badger to the internet once, or paste fresh orbit data."
        elif p["rec_start"] is None:
            e["reason"] = f"Too low to record (highest {p['max_elev']:.0f}°)."
        else:
            need = sat["min_elevation"] if dev == "weather" else sat["min_elevation_dedicated"]
            if p["max_elev"] < need:
                e["reason"] = (f"Too low (highest {p['max_elev']:.0f}°; the shared radio only records passes above {need:g}°)."
                               if dev == "weather" else f"Too low (highest {p['max_elev']:.0f}°; set to record above {need:g}°).")
            else:
                pad = PAD_S + (STALE_PAD_S if e["approximate"] else 0)
                start = max(p["rec_start"] - pad, now)
                end = min(p["rec_end"] + pad, start + MAX_RECORD_S)
                if end - start < 120:
                    e["reason"] = "Too little of the pass is left to record."
                else:
                    e["start"], e["end"] = round(start, 1), round(end, 1)
                    cands.append(e)
        out.append(e)

    accepted: list[dict] = []
    planned_s: dict[str, float] = {}
    blocks = list(busy or [])
    if dev == "weather" and last_lend_end:
        blocks.append((last_lend_end - 1.0, last_lend_end))
    for e in sorted(cands, key=lambda c: -c["max_elev"]):          # highest passes first
        dur = e["end"] - e["start"]
        margin = gap if dev == "weather" else 60.0
        clash = [a for a in accepted if e["start"] < a["end"] + margin and a["start"] < e["end"] + margin]
        blocked = [b for b in blocks if e["start"] < b[1] + margin and b[0] < e["end"] + margin]
        if dev == "weather":
            d = day_of(e["start"])
            used = float((ledger_days.get(d) or {}).get("sat_s", 0)) + planned_s.get(d, 0.0)
            if used + dur > sat["daily_max_min"] * 60.0:
                e["reason"] = (f"Over the daily limit ({sat['daily_max_min']:g} minutes of recording a day on the "
                               "shared radio, so weather alerts are rarely paused).")
                continue
        if clash:
            e["reason"] = (f"Too close to the {clash[0]['name']} pass at {hhmm(clash[0]['start'])}, which is higher"
                           + (f" (the weather radio listens at least {gap / 60:g} minutes between loans)." if dev == "weather" else "."))
            continue
        if blocked:
            e["reason"] = "The radio is busy then (an aircraft scan, or it was just lent)."
            continue
        e["decision"] = "record"
        e["reason"] = (f"Will record {hhmm(e['start'])}–{hhmm(e['end'])}"
                       + (" on the weather radio (alerts pause meanwhile)." if dev == "weather" else " on the second radio.")
                       + (" Orbit data is old, so the times are approximate." if e["approximate"] else ""))
        accepted.append(e)
        if dev == "weather":
            planned_s[day_of(e["start"])] = planned_s.get(day_of(e["start"]), 0.0) + dur
    out.sort(key=lambda e: e["aos"])
    return out
