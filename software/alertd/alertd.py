#!/usr/bin/env python3
"""
alertd — NOAA Weather Radio SAME/EAS alerts → banner on every connected phone.

  rtl_fm (RTL-SDR tuned to your NOAA transmitter) → multimon-ng -a EAS → parse
  the SAME header → /run/prepper/alerts.json (the launcher shows a red banner
  and the front panel flashes it).

SAME header: ZCZC-ORG-EEE-PSSCCC[-PSSCCC…]+TTTT-JJJHHMM-LLLLLLLL-
Tests (RWT/RMT/NPT) are logged, not shown. Filter to your counties with
[radio].same_fips = ["008031", …] (SSCCC; county 000 = whole state).

Two ways other services use the weather radio:
  * Audio tap — write /run/prepper/noaa-tap.request as {"until": <epoch>, "pid": <pid>}; while it is live
    the received audio (16-bit mono, 22050 Hz) is saved, and when it ends (or after 15 min) it appears
    as /run/prepper/noaa-tap/<start-epoch>.wav (newest 6 kept). The NOAA forecast-to-text feature uses this.
  * Lending the dongle — software/common/sdrlease.py. A borrower (sdrd: an aircraft scan or a
    weather-satellite pass) asks for the "weather" dongle; alertd stops listening until it is handed
    back, then listens again within about a second. It refuses while an alert is live, always with
    [radio] sdr_lend = false, within [radio] sdr_lend_gap_min (10) minutes of the last loan ending
    (a SAME alert is sent once, so the radio must listen between loans), and once loans have added
    up to [radio] sdr_lend_max_min_per_day (120) minutes in the last 24 hours.

Several dongles: alertd pins itself to its own one — [radio] weather_sdr = "<serial>" (or a device
index, an integer); see software/sdr/dongles.py. If that dongle is not plugged in, alertd does
not start (it never takes the second dongle by mistake).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import sdrlease  # noqa: E402

try:                                    # which dongle is ours when several are plugged in
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdr"))
    import dongles  # noqa: E402
except ImportError:                     # an older install without software/sdr: single-dongle behaviour
    dongles = None

OUT = Path(os.environ.get("ALERTS_FILE", "/run/prepper/alerts.json"))
BEAT = Path("/run/prepper/alertd.ok")
TAP_REQ = Path(os.environ.get("NOAA_TAP_REQUEST", "/run/prepper/noaa-tap.request"))
TAP_DIR = Path(os.environ.get("NOAA_TAP_DIR", "/run/prepper/noaa-tap"))
RATE = 22050
TAP_MAX_S = 15 * 60
TAP_KEEP = 6
DONGLE = "weather"
EVENTS = {
    "TOR": "Tornado Warning", "TOA": "Tornado Watch", "SVR": "Severe Thunderstorm Warning",
    "SVA": "Severe Thunderstorm Watch", "SVS": "Severe Weather Statement", "SPS": "Special Weather Statement",
    "FFW": "Flash Flood Warning", "FFA": "Flash Flood Watch", "FFS": "Flash Flood Statement",
    "FLW": "Flood Warning", "FLA": "Flood Watch", "FLS": "Flood Statement",
    "WSW": "Winter Storm Warning", "WSA": "Winter Storm Watch", "BZW": "Blizzard Warning",
    "HWW": "High Wind Warning", "HWA": "High Wind Watch", "EWW": "Extreme Wind Warning",
    "HUW": "Hurricane Warning", "HUA": "Hurricane Watch", "HLS": "Hurricane Statement",
    "TRW": "Tropical Storm Warning", "TRA": "Tropical Storm Watch", "SSW": "Storm Surge Warning",
    "SSA": "Storm Surge Watch", "TSW": "Tsunami Warning", "TSA": "Tsunami Watch",
    "SMW": "Special Marine Warning", "DSW": "Dust Storm Warning", "FRW": "Fire Warning",
    "EQW": "Earthquake Warning", "VOW": "Volcano Warning", "AVW": "Avalanche Warning",
    "AVA": "Avalanche Watch", "DBW": "Dam Break Warning", "DBA": "Dam Watch",
    "EVI": "Evacuation Immediate", "CEM": "Civil Emergency Message", "CDW": "Civil Danger Warning",
    "SPW": "Shelter in Place Warning", "HMW": "Hazardous Materials Warning",
    "NUW": "Nuclear Power Plant Warning", "RHW": "Radiological Hazard Warning",
    "LEW": "Law Enforcement Warning", "LAE": "Local Area Emergency", "TOE": "911 Outage Emergency",
    "CAE": "Child Abduction Emergency", "BLU": "Blue Alert", "EAN": "National Emergency (EAN)",
    "ADR": "Administrative Message",
}
TESTS = {"RWT", "RMT", "NPT", "DMO"}
RTL_SDR_IDS = {("0bda", "2832"), ("0bda", "2838")}     # Realtek RTL2832U dongles (keep in sync with 99-prepper-sdr.rules)
STATES = {"01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
          "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
          "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
          "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
          "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
          "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
          "54": "WV", "55": "WI", "56": "WY", "72": "PR"}
SAME = re.compile(r"ZCZC-(?P<org>[A-Z]{3})-(?P<eee>[A-Z]{3})-(?P<locs>[\d-]+)\+(?P<dur>\d{4})-(?P<iss>\d{7})-(?P<sender>[^-]{1,8})-?")


def parse(line: str, now: dt.datetime | None = None) -> dict | None:
    m = SAME.search(line)
    if not m:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    locs = [l for l in m["locs"].split("-") if len(l) == 6]
    jjj, hh, mm = int(m["iss"][:3]), int(m["iss"][3:5]), int(m["iss"][5:7])
    year = now.year if jjj <= now.timetuple().tm_yday + 1 else now.year - 1
    issued = dt.datetime(year, 1, 1, hh, mm, tzinfo=dt.timezone.utc) + dt.timedelta(days=jjj - 1)
    expires = issued + dt.timedelta(hours=int(m["dur"][:2]), minutes=int(m["dur"][2:]))
    states = sorted({STATES.get(l[1:3], l[1:3]) for l in locs})
    return {
        "id": f"{m['eee']}-{m['iss']}-{m['sender']}", "code": m["eee"],
        "event": EVENTS.get(m["eee"], f"Alert {m['eee']}"), "org": m["org"],
        "locations": [l[1:] for l in locs], "areas": f"{len(locs)} areas in {', '.join(states)}",
        "issued": issued.isoformat(), "expires": expires.astimezone().strftime("%a %H:%M"),
        "expires_ts": expires.timestamp(), "sender": m["sender"].strip(), "test": m["eee"] in TESTS,
        "text": "Tune a weather radio for details." if m["eee"] not in TESTS else "",
    }


def relevant(a: dict, fips: list[str]) -> bool:
    if not fips:
        return True
    for loc in a["locations"]:
        for f in fips:
            if loc == f or (loc[2:] == "000" and loc[:2] == f[:2]) or loc == "00000":
                return True
    return False


def write(alerts: dict) -> None:
    now = time.time()
    live = [a for a in alerts.values() if a["expires_ts"] > now]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=OUT.parent, prefix=".alerts.")
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(sorted(live, key=lambda a: a["issued"], reverse=True)))
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)


def sdr_present() -> bool | None:
    """True/False from the USB device list; None where there is no sysfs (a laptop)."""
    root = Path("/sys/bus/usb/devices")
    if not root.is_dir():
        return None
    for dev in root.iterdir():
        try:
            ids = ((dev / "idVendor").read_text().strip(), (dev / "idProduct").read_text().strip())
        except OSError:
            continue
        if ids in RTL_SDR_IDS:
            return True
    return False


class Tap:
    """Saves received audio while someone has asked for it (see the module docstring)."""

    def __init__(self, req: Path = TAP_REQ, out: Path = TAP_DIR, clock=time.time):
        self.req, self.out, self.clock = req, out, clock
        self.w = None
        self.tmp = None
        self.start = 0.0
        self.checked = 0.0
        self.wanted = False

    def _live(self) -> bool:
        try:
            d = json.loads(self.req.read_text())
            return float(d.get("until", 0)) > self.clock()
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def feed(self, chunk: bytes) -> None:
        now = self.clock()
        if now - self.checked >= 1.0:
            self.checked = now
            self.wanted = self._live()
        if self.w and (not self.wanted or now - self.start > TAP_MAX_S):
            self.close()
            if self.wanted:                      # 15 min done: the request is over even if it says longer
                with contextlib.suppress(OSError):
                    self.req.unlink()
                self.wanted = False
            return
        if self.wanted and not self.w:
            try:
                self.out.mkdir(parents=True, exist_ok=True)
                self.start = now
                self.tmp = self.out / f".{int(now)}.wav.part"
                self.w = wave.open(str(self.tmp), "wb")
                self.w.setnchannels(1)
                self.w.setsampwidth(2)
                self.w.setframerate(RATE)
            except OSError as e:
                print(f"alertd: audio tap: {e}", file=sys.stderr)
                self.w, self.wanted = None, False
                return
        if self.w:
            self.w.writeframes(chunk[: len(chunk) & ~1])

    def close(self) -> None:
        if not self.w:
            return
        try:
            self.w.close()
            os.chmod(self.tmp, 0o644)
            os.replace(self.tmp, self.out / f"{int(self.start)}.wav")
            for old in sorted(self.out.glob("*.wav"))[:-TAP_KEEP]:
                old.unlink(missing_ok=True)
        except OSError as e:
            print(f"alertd: audio tap: {e}", file=sys.stderr)
        self.w = None


class LendLog:
    """When the dongle was lent and handed back, kept in /run/prepper/sdr/weather-lends.json so a
    restart of alertd doesn't forget it (a reboot does, which is fine)."""

    def __init__(self, path: Path | None = None, clock=time.time):
        self.path = path or (sdrlease.SDR_DIR / f"{DONGLE}-lends.json")
        self.clock = clock
        try:
            d = json.loads(self.path.read_text())
            self.loans = [[float(a), None if b is None else float(b)] for a, b in d.get("loans", [])][-50:]
        except (OSError, ValueError, TypeError, AttributeError):
            self.loans = []

    def _save(self) -> None:
        cut = self.clock() - 2 * 86400
        self.loans = [x for x in self.loans if (x[1] or self.clock()) > cut][-50:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}")
            tmp.write_text(json.dumps({"loans": self.loans}))
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"alertd: lend log: {e}", file=sys.stderr)

    def start(self) -> None:
        self.loans.append([self.clock(), None])
        self._save()

    def end(self) -> None:
        if self.loans and self.loans[-1][1] is None:
            self.loans[-1][1] = self.clock()
            self._save()

    def last_end(self) -> float | None:
        ends = [b for _, b in self.loans if b is not None]
        return max(ends) if ends else None

    def used(self, window: float = 86400) -> float:
        now = self.clock()
        return sum(max(0.0, min(b if b is not None else now, now) - max(a, now - window)) for a, b in self.loans)


def lending_allowed(alerts: dict, cfg: dict, lends: LendLog | None = None) -> str | None:
    """None if the dongle may be lent now, else the reason it may not."""
    if cfg.get("sdr_lend") is False:
        return "the weather radio is set not to lend its dongle ([radio] sdr_lend = false)"
    now = time.time()
    live = [a for a in alerts.values() if a["expires_ts"] > now]
    if live:
        return f"a weather alert is live ({live[0]['event']}); the radio keeps listening"
    if lends is not None:
        gap = _num(cfg.get("sdr_lend_gap_min"), 10) * 60
        last = lends.last_end()
        if last is not None and lends.clock() - last < gap:
            back = max(1, math.ceil((last + gap - lends.clock()) / 60))
            return (f"the weather radio was lent a moment ago and listens at least {gap / 60:g} minutes between "
                    f"loans; try again in {back} min")
        cap = _num(cfg.get("sdr_lend_max_min_per_day"), 120) * 60
        if lends.used() >= cap:
            return f"the weather radio has been lent {cap / 60:g} minutes in the last day, its limit; it keeps listening"
    return None


def _num(v, default: float) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else default


def pick_device(cfg: dict) -> tuple[str | None, bool | None, str | None]:
    """(rtl_fm -d argument or None, is our dongle plugged in (None = can't tell), a problem to log)."""
    if dongles is None:
        return None, sdr_present(), None
    a = dongles.assign(cfg)
    return a["weather"]["arg"], a["weather"]["present"], a["problem"]


def rtl_fm_argv(freq: float, device: str | None = None) -> list[str]:
    argv = ["rtl_fm", "-f", f"{freq}M", "-M", "fm", "-s", str(RATE), "-E", "dc", "-F", "0"]
    if device:
        argv += ["-d", str(device)]          # librtlsdr takes an index or a serial number here
    return argv + ["-"]


def listen(freq: float, fips: list[str], alerts: dict, cfg: dict, device: str | None = None,
           lends: LendLog | None = None) -> tuple[str, int]:
    """One receiving session. Returns ("lent", 0) when it stopped to lend the dongle,
    else ("ended", rc) when the receiver exited."""
    radio = subprocess.Popen(rtl_fm_argv(freq, device), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    # stdin carries raw audio bytes, stdout text lines: binary pipes, stdout decoded below
    # (text=True here would make stdin a text stream, and writing audio to it would fail).
    proc = subprocess.Popen(["multimon-ng", "-q", "-t", "raw", "-a", "EAS", "-"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    tap = Tap()
    lent = []

    def pump():
        # rtl_fm → multimon-ng, through us so the audio tap can see it
        try:
            while True:
                chunk = radio.stdout.read1(8192)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                proc.stdin.flush()
                tap.feed(chunk)
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            tap.close()
            with contextlib.suppress(OSError, ValueError):
                proc.stdin.close()

    def watch():
        # heartbeat on a timer (multimon-ng prints only when an alert arrives) + lending requests
        beat = 0.0
        while radio.poll() is None and proc.poll() is None:
            now = time.monotonic()
            if now - beat >= 30:
                beat = now
                try:
                    BEAT.parent.mkdir(parents=True, exist_ok=True); BEAT.touch()
                except OSError:
                    pass
            req = sdrlease.borrow_request(DONGLE)
            if req:
                why = lending_allowed(alerts, cfg, lends)
                if why:
                    sdrlease.refuse(DONGLE, why)
                else:
                    print(f"alertd: lending the dongle to {req.get('owner', '?')}", file=sys.stderr)
                    if lends is not None:
                        lends.start()
                    lent.append(True)
                    radio.terminate()
                    return
            time.sleep(2)

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    with io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace") as out:
        for line in out:
            a = parse(line)
            if not a:
                continue
            if a["test"]:
                print(f"alertd: test {a['code']} from {a['sender']}", file=sys.stderr); continue
            if not relevant(a, fips) or a["id"] in alerts:
                continue
            print(f"alertd: {a['event']} — {a['areas']} until {a['expires']}", file=sys.stderr)
            alerts[a["id"]] = a
            write(alerts)
    radio.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        radio.wait(timeout=5)
    with contextlib.suppress(OSError):
        radio.stdout.close()
    rc = proc.wait()
    return ("lent", 0) if lent else ("ended", rc)


def main() -> int:
    cfg_path = Path(os.environ.get("PREPPER_CONFIG", "/etc/prepper/prepper.toml"))
    cfg = tomllib.loads(cfg_path.read_text()).get("radio", {}) if cfg_path.exists() else {}
    if cfg.get("sdr_weather") == "off":
        return 0
    # Exit 0 when there is nothing to listen with: the unit restarts only on
    # failure, and udev starts it again when an RTL-SDR is plugged in.
    if not (shutil.which("rtl_fm") and shutil.which("multimon-ng")):
        print("alertd: rtl_fm/multimon-ng not installed", file=sys.stderr); return 0
    if sdr_present() is False:
        BEAT.unlink(missing_ok=True)
        print("alertd: no RTL-SDR plugged in; will start when one is connected", file=sys.stderr); return 0
    device, present, problem = pick_device(cfg)
    if problem:
        print(f"alertd: {problem}", file=sys.stderr)
    if present is False:
        BEAT.unlink(missing_ok=True)
        print(f"alertd: the weather radio's dongle ({device or 'see [radio] weather_sdr'}) is not plugged in; "
              "will start when it is", file=sys.stderr)
        return 0
    freq = float(cfg.get("weather_freq_mhz", 162.55))
    fips = [str(f).zfill(6)[1:] if len(str(f)) == 6 else str(f).zfill(5) for f in cfg.get("same_fips", [])]
    alerts: dict[str, dict] = {}
    lends = LendLog()
    while True:
        # hold the dongle (waits while it is lent out), listen until the receiver ends or we lend it
        with sdrlease.hold(DONGLE, poll=0.5):
            lends.end()                          # back from a loan (no-op otherwise)
            print(f"alertd: listening on {freq} MHz" + (f" (dongle {device})" if device else ""), file=sys.stderr)
            started = time.monotonic()
            why, rc = listen(freq, fips, alerts, cfg, device, lends)
        if why != "lent":
            break
        BEAT.unlink(missing_ok=True)
    BEAT.unlink(missing_ok=True)
    # rtl_fm died at once and no dongle can be confirmed (no USB sysfs to look at):
    # treat it as "no SDR" rather than crash-looping every 30 s; udev starts us on plug-in.
    if time.monotonic() - started < 10 and sdr_present() is not True:
        print("alertd: receiver failed to start and no RTL-SDR is visible; exiting", file=sys.stderr)
        return 0
    return rc or 1                 # rtl_fm exited (dongle unplugged) → systemd restarts us


if __name__ == "__main__":
    sys.exit(main())
