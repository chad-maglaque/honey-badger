#!/usr/bin/env python3
"""
sdrd — the Badger's radio scheduler: aircraft (ADS-B, readsb) and weather-satellite passes
(Meteor-M LRPT, SatDump) on RTL-SDR dongles, without losing NOAA weather alerts.

Every Badger has two dongles (see dongles.py): SDR 1 belongs to alertd (NOAA Weather Radio) and
is never touched in normal operation; SDR 2 does aircraft and satellites, depending on which
antenna is fitted to its jack ([radio] sdr2_antenna, or the admin's choice in the launcher):
  "adsb"       (1090 MHz whip, as shipped) — aircraft all the time; satellite passes only if
               [satellites] capture = true (the whip hears 137 MHz poorly), aircraft pausing per pass;
  "satellite"  (137 MHz V-dipole outdoors) — every pass >= 15°; aircraft scans on demand;
  "both"       (a wideband antenna) — aircraft all the time, paused for each pass.

Fallback — SDR 2 missing, or failing (its program keeps dying): sdrd borrows SDR 1 from
alertd through sdrlease, by the cautious single-dongle rules:
  * aircraft — only when someone taps "Scan aircraft" (15 min, a few a day, then handed back);
  * satellites — only if an admin opted in ([satellites] fallback_capture = true), only high
    passes, at most 45 min a day;
  * never while an alert is live, never twice without >= 10 min of listening in between
    (alertd enforces both as well), and the status file says the weather radio is paused.

Talks to the portal through files in /run/prepper/sdr (see sdrcommon). Runs as `prepper`
(prepper-sdr.service). Child programs get argv only (no secrets) and are killed with their
process group.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "common"))
import dongles  # noqa: E402
import satplan  # noqa: E402
import sdrcommon as C  # noqa: E402
import sdrlease  # noqa: E402
import tlefile as tle  # noqa: E402

LABEL = {"aircraft": "aircraft scan", "satellite": "satellite pass"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
IMG_MAX = 60_000_000
IMG_MIN = 2_000


def log(msg: str) -> None:
    print(f"sdrd: {msg}", file=sys.stderr, flush=True)


# ───────────────────────────── ledger ────────────────────────────────────────

class Ledger:
    """Daily usage that must survive a restart: shared-dongle satellite seconds, aircraft scans,
    when the weather dongle was last handed back, and a short history for the pages."""

    def __init__(self, path: Path | None = None, clock=time.time):
        self.path = path or (C.state_dir() / "ledger.json")
        self.clock = clock
        self.lock = threading.Lock()
        d = C.read_json(self.path, {}) or {}
        self.d = d if isinstance(d, dict) else {}
        if not isinstance(self.d.get("days"), dict):
            self.d["days"] = {}
        if not isinstance(self.d.get("history"), list):
            self.d["history"] = []

    def _save(self) -> None:
        days = self.d["days"]
        for k in sorted(days)[:-8]:
            del days[k]
        self.d["history"] = self.d["history"][-40:]
        with contextlib.suppress(OSError):
            C.atomic_write(self.path, json.dumps(self.d), 0o640)

    def day(self, t: float | None = None) -> dict:
        k = satplan.day_of(t if t is not None else self.clock())
        v = self.d["days"].get(k)
        return v if isinstance(v, dict) else {}

    def days(self) -> dict:
        return {k: v for k, v in self.d["days"].items() if isinstance(v, dict)}

    def _bump(self, key: str, amount: float, t: float) -> None:
        k = satplan.day_of(t)
        v = self.d["days"].setdefault(k, {})
        v[key] = (v.get(key) or 0) + amount

    def add_sat(self, seconds: float, t: float) -> None:
        with self.lock:
            self._bump("sat_s", round(max(0.0, seconds), 1), t)
            self._save()

    def add_scan(self, t: float) -> None:
        with self.lock:
            self._bump("scans", 1, t)
            self._save()

    def lend_end(self, t: float) -> None:
        with self.lock:
            self.d["last_lend_end"] = t
            self._save()

    @property
    def last_lend_end(self) -> float | None:
        v = self.d.get("last_lend_end")
        return float(v) if isinstance(v, (int, float)) else None

    def note(self, kind: str, result: str, detail: str = "", **extra) -> None:
        with self.lock:
            self.d["history"].append(dict({"at": round(self.clock()), "kind": kind, "result": result,
                                           "detail": str(detail)[:300]}, **extra))
            self._save()

    def history(self) -> list[dict]:
        return list(self.d["history"])


# ───────────────────────────── processes ─────────────────────────────────────

def start_proc(argv: list[str], logfile: Path) -> subprocess.Popen:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    fh = open(logfile, "wb")
    try:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    finally:
        fh.close()


def stop_proc(p: subprocess.Popen, first=signal.SIGINT, grace: float = 10.0) -> None:
    """Ask nicely (SIGINT lets SatDump/readsb close the dongle), then SIGTERM, then SIGKILL, whole group."""
    for sig, wait in ((first, grace), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        if p.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(timeout=wait)


def _children(pid: int) -> list[int]:
    out = []
    for task in Path(f"/proc/{pid}/task").glob("*"):
        with contextlib.suppress(OSError, ValueError):
            out += [int(x) for x in (task / "children").read_text().split()]
    return out


def holds_usb(pid: int, depth: int = 0) -> bool:
    """True while the process (or a child) has a USB device node open. False without /proc."""
    if depth > 3 or not Path("/proc").is_dir():
        return False
    try:
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            with contextlib.suppress(OSError):
                if os.readlink(fd).startswith("/dev/bus/usb/"):
                    return True
    except OSError:
        return False
    return any(holds_usb(c, depth + 1) for c in _children(pid))


def log_tail(p: Path, n: int = 300) -> str:
    try:
        with open(p, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4000))
            lines = [ln for ln in fh.read().decode("utf-8", "replace").splitlines() if ln.strip()]
        return re.sub(r"\x1b\[[0-9;]*m", "", lines[-1])[:n] if lines else ""
    except OSError:
        return ""


# ───────────────────────────── images ────────────────────────────────────────

def image_info(path: Path) -> tuple[str, int, int] | None:
    """(ext, width, height) for a real PNG/JPEG (magic bytes + header), else None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        return "png", int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = int.from_bytes(head[i + 2:i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return "jpg", int.from_bytes(head[i + 7:i + 9], "big"), int.from_bytes(head[i + 5:i + 7], "big")
            i += 2 + seg
        return "jpg", 0, 0
    return None


def pick_image(workdir: Path, prefer: list[str]) -> tuple[Path, tuple[str, int, int]] | None:
    """The best picture SatDump made: name matches `prefer` (in order), not a map overlay, then the largest."""
    best, best_key = None, None
    for f in workdir.rglob("*"):
        if f.is_symlink() or not f.is_file() or f.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        size = f.stat().st_size
        if not IMG_MIN <= size <= IMG_MAX:
            continue
        inf = image_info(f)
        if not inf or (inf[2] and inf[2] < 100):
            continue
        name = f.name.lower()
        score = sum((len(prefer) - i) * 10 for i, pat in enumerate(prefer) if pat and pat in name)
        if any(w in name for w in ("map", "proj", "overlay")):
            score -= 5
        key = (score, size)
        if best_key is None or key > best_key:
            best, best_key = (f, inf), key
    return best


class Gallery:
    def __init__(self, folder: Path | None = None):
        self.dir = folder or (C.state_dir() / "images")
        self.lock = threading.Lock()

    def index(self) -> list[dict]:
        d = C.read_json(self.dir / "index.json", [])
        return [e for e in d if isinstance(e, dict) and ID_RE.match(str(e.get("id", "")))] if isinstance(d, list) else []

    def add(self, src: Path, inf: tuple[str, int, int], meta: dict, keep: int) -> dict:
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            ext, w, h = inf
            iid = time.strftime("%Y%m%d-%H%M", time.gmtime(meta.get("aos") or time.time())) + \
                f"-{int(meta.get('norad', 0))}-{secrets.token_hex(3)}"
            dest = self.dir / f"{iid}.{ext}"
            shutil.copyfile(src, dest)
            os.chmod(dest, 0o640)
            entry = {"id": iid, "file": dest.name, "ext": ext, "width": w, "height": h, "bytes": dest.stat().st_size,
                     "name": meta.get("name"), "norad": meta.get("norad"), "aos": meta.get("aos"), "los": meta.get("los"),
                     "tca": meta.get("tca"), "max_elev": meta.get("max_elev"), "device": meta.get("device"),
                     "created": round(time.time())}
            idx = [entry] + self.index()
            for old in idx[keep:]:
                with contextlib.suppress(OSError):
                    (self.dir / Path(str(old.get("file", ""))).name).unlink()
            C.atomic_write(self.dir / "index.json", json.dumps(idx[:keep]), 0o640)
            return entry


# ───────────────────────────── jobs ──────────────────────────────────────────

class Job(threading.Thread):
    """One use of one dongle: an aircraft session or a satellite pass."""

    def __init__(self, d: "Daemon", kind: str, device: str, seconds: float, always: bool = False,
                 meta: dict | None = None, by: str = "", request: str | None = None):
        super().__init__(daemon=True)
        self.d, self.kind, self.device, self.always = d, kind, device, always
        self.seconds = seconds
        self.meta = meta or {}
        self.by, self.request = by, request
        self.stop_evt = threading.Event()
        self.state = "starting"
        self.started = time.time()
        self.until = None if always else self.started + seconds
        self.message = ""
        self.proc: subprocess.Popen | None = None

    def stop(self) -> None:
        self.stop_evt.set()

    def _borrow(self):
        if self.device != "weather":
            return contextlib.nullcontext()
        return sdrlease.borrow("weather", owner=LABEL[self.kind], seconds=int(self.seconds) + 120, wait=self.d.borrow_wait)

    def run(self) -> None:
        try:
            if self.kind == "aircraft":
                self.run_aircraft()
            else:
                self.run_pass()
        except Exception as e:  # noqa: BLE001 — a job failing must never take the scheduler down
            log(f"{self.kind} job failed: {type(e).__name__}: {e}")
            self.message = "Something went wrong with the radio program; see the system log."
            self.d.ledger.note(self.kind, "failed", f"{type(e).__name__}: {e}", name=self.meta.get("name"))
        finally:
            if self.proc and self.proc.poll() is None:
                stop_proc(self.proc, signal.SIGTERM, 3)
            self.state = "done"
            self.d.wake()

    # aircraft
    def run_aircraft(self) -> None:
        jdir = C.run_dir() / "readsb"
        logf = C.run_dir() / "readsb.log"
        borrowed = False
        try:
            with self._borrow():
                borrowed = self.device == "weather"
                if borrowed:
                    self.d.ledger.add_scan(time.time())
                    self.d.set_paused(LABEL["aircraft"], self.until)
                backoff = self.d.restart_backoff
                while not self.stop_evt.is_set():
                    jdir.mkdir(parents=True, exist_ok=True)
                    self.proc = start_proc(self.d.readsb_argv(self.device, jdir), logf)
                    self.state = "always_on" if self.always else "scanning"
                    t0 = time.monotonic()
                    while self.proc.poll() is None and not self.stop_evt.is_set():
                        if self.until and time.time() >= self.until:
                            break
                        self.stop_evt.wait(1.0)
                    exited = self.proc.poll() is not None
                    stop_proc(self.proc, signal.SIGINT, 5)
                    if self.stop_evt.is_set() or (self.until and time.time() >= self.until) or not exited:
                        break                     # the normal end: time up, or stopped
                    why = log_tail(logf) or "no reason given"
                    if self.device == "sdr2":
                        if time.monotonic() - t0 > 300:
                            self.d.sdr2_good()
                        else:
                            self.d.sdr2_trouble(f"the aircraft decoder stopped: {why}")
                    if not self.always:
                        self.message = f"The aircraft decoder stopped early: {why}"
                        self.d.ledger.note("aircraft", "failed", why)
                        break
                    if self.device == "sdr2" and not self.d.sdr2_ok():
                        self.message = f"SDR 2 isn't working ({why})."
                        break                     # give up for now: the fallback takes over
                    # the always-on decoder died (dongle unplugged?): say why, wait, try again
                    self.message = f"The aircraft decoder stopped ({why}); trying again shortly."
                    log(self.message)
                    if time.monotonic() - t0 > 300:
                        backoff = self.d.restart_backoff
                    self.state = "restarting"
                    self.stop_evt.wait(backoff)
                    backoff = min(backoff * 2, 300.0)
        except sdrlease.Busy as e:
            self.message = f"The weather radio can't be lent right now: {e}."
            self.d.ledger.note("aircraft", "refused", str(e))
        finally:
            with contextlib.suppress(OSError):
                (jdir / "aircraft.json").unlink()
            if borrowed:
                self.d.ledger.lend_end(time.time())
                self.d.set_listening()

    # satellite pass
    def run_pass(self) -> None:
        m, s = self.meta, self.d.s["satellites"]
        work = C.state_dir() / "work" / f"{m['norad']}-{int(m['tca'])}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        logf = C.run_dir() / "satdump.log"
        borrowed, t_lent = False, None
        try:
            with self._borrow():
                borrowed = self.device == "weather"
                t_lent = time.time()
                self.until = max(self.until or 0, t_lent)
                if borrowed:
                    self.d.set_paused(LABEL["satellite"], self.until + 30)
                dur = max(60, int((self.until or t_lent) - t_lent))
                self.proc = start_proc(self.d.satdump_argv(self.device, m, work, dur), logf)
                self.state = "recording"
                t_start = time.monotonic()
                while self.proc.poll() is None and not self.stop_evt.is_set() and time.time() < (self.until or 0) + 20:
                    self.stop_evt.wait(1.0)
                if (self.device == "sdr2" and self.proc.poll() not in (None, 0) and not self.stop_evt.is_set()
                        and time.monotonic() - t_start < 30):
                    self.d.sdr2_trouble("SatDump stopped at once: " + (log_tail(logf) or "no reason given"))
                if self.proc.poll() is None:
                    if self.stop_evt.is_set():
                        stop_proc(self.proc, signal.SIGINT, 15)
                    else:
                        # live part over: SatDump closes the dongle, then makes pictures. Hand the
                        # dongle back as soon as it is closed; let the processing run on.
                        t = time.monotonic()
                        while self.proc.poll() is None and holds_usb(self.proc.pid) and time.monotonic() - t < 30:
                            time.sleep(1.0)
                        if holds_usb(self.proc.pid):
                            stop_proc(self.proc, signal.SIGINT, 20)
        except sdrlease.Busy as e:
            self.message = f"Skipped: {e}."
            self.d.ledger.note("satellite", "skipped", str(e), name=m.get("name"), norad=m.get("norad"), tca=m.get("tca"))
            shutil.rmtree(work, ignore_errors=True)
            return
        finally:
            if borrowed:
                now = time.time()
                self.d.ledger.add_sat(now - (t_lent or now), t_lent or now)
                self.d.ledger.lend_end(now)
                self.d.set_listening()
        # processing (the dongle is already back)
        self.state = "processing"
        if self.proc and self.proc.poll() is None:
            t = time.monotonic()
            while self.proc.poll() is None and time.monotonic() - t < 900 and not self.stop_evt.is_set():
                time.sleep(1.0)
            stop_proc(self.proc, signal.SIGINT, 10)
        got = pick_image(work, s["prefer_image"])
        if got:
            entry = self.d.gallery.add(got[0], got[1], dict(m, device=self.device), s["keep_images"])
            self.message = "New picture."
            if self.device == "sdr2":
                self.d.sdr2_good()
            self.d.ledger.note("satellite", "image", "", name=m.get("name"), norad=m.get("norad"), tca=m.get("tca"),
                               image=entry["id"])
            log(f"pass {m.get('name')} → {entry['file']}")
        else:
            tail = log_tail(logf)
            self.message = "No picture this time (weak signal, or the antenna doesn't suit 137 MHz)."
            self.d.ledger.note("satellite", "no image", tail, name=m.get("name"), norad=m.get("norad"), tca=m.get("tca"))
        shutil.rmtree(work, ignore_errors=True)


# ───────────────────────────── daemon ────────────────────────────────────────

class Daemon:
    def __init__(self, clock=time.time, borrow_wait: float = 45.0, replan_s: float = 600.0):
        self.clock = clock
        self.borrow_wait = borrow_wait
        self.replan_s = replan_s
        self.lock = threading.RLock()
        self._wake = threading.Event()
        self.ledger = Ledger()
        self.gallery = Gallery()
        self.jobs: dict[str, Job | None] = {"weather": None, "sdr2": None}
        self.paused: dict | None = None
        self.air = {"message": "", "request": None}
        self.s = C.settings()
        self.cfg_mtime = None
        self.assign = dongles.assign(self.s["radio"])
        self.assign_at = 0.0
        self.plan: list[dict] = []
        self.plan_key = None
        self.plan_at = 0.0
        self.pos = None
        self.tle_rows: list[dict] = []
        self.mode = {"device": None, "why": ""}
        self.next_always = 0.0
        self.restart_backoff = 15.0
        self.sdr2_trouble_log: list[float] = []
        self.sdr2_failed_until = 0.0
        self.sdr2_why = ""

    def wake(self) -> None:
        self._wake.set()

    # status
    def set_paused(self, by: str, until: float | None) -> None:
        with self.lock:
            self.paused = {"by": by, "until": until}
        self.write_status()

    def set_listening(self) -> None:
        with self.lock:
            self.paused = None
        self.write_status()

    # argv
    def readsb_argv(self, device: str, jdir: Path) -> list[str]:
        a = self.s["aircraft"]
        argv = [a["readsb"], "--device-type", "rtlsdr", "--gain", str(a["gain"]), "--write-json", str(jdir),
                "--write-json-every", "1", "--quiet"]
        dev = self.assign["weather"]["arg"] if device == "weather" else (self.assign["sdr2"] or {}).get("arg")
        if dev:
            argv += ["--device", str(dev)]
        if self.pos:
            argv += ["--lat", f"{self.pos['lat']:.4f}", "--lon", f"{self.pos['lon']:.4f}"]
        return argv

    def satdump_argv(self, device: str, m: dict, work: Path, dur: int) -> list[str]:
        s = self.s["satellites"]
        argv = [s["satdump"], "live", m["pipeline"], str(work), "--source", "rtlsdr",
                "--samplerate", str(int(s["samplerate"])), "--frequency", str(int(round(m["freq_mhz"] * 1e6))),
                "--gain", f"{s['gain']:g}", "--timeout", str(int(dur))]
        # the serial when it is unique, else the device index (identical dongles with factory serials)
        dev = self.assign["weather"]["arg"] if device == "weather" else (self.assign["sdr2"] or {}).get("arg")
        if dev:
            argv += ["--source_id", str(dev)]
        return argv

    # helpers
    def busy(self, device: str) -> Job | None:
        j = self.jobs.get(device)
        return j if j and j.is_alive() else None

    def sdr2_ok(self) -> dict | None:
        """SDR 2, if it is plugged in and hasn't been failing."""
        x = self.assign.get("sdr2")
        return x if x and x.get("present") and self.clock() >= self.sdr2_failed_until else None

    def sdr2_trouble(self, why: str) -> None:
        """SDR 2's program died early. Three times within 15 minutes → treat SDR 2 as failed for
        30 minutes and fall back to borrowing the weather radio (by the cautious rules)."""
        now = self.clock()
        self.sdr2_why = why[:200]
        self.sdr2_trouble_log = [t for t in self.sdr2_trouble_log if now - t < 900] + [now]
        if len(self.sdr2_trouble_log) >= 3:
            self.sdr2_failed_until = now + 1800
            self.sdr2_trouble_log = []
            self.plan_key = None
            log(f"SDR 2 keeps failing ({why}); using the weather radio as a fallback for 30 min")
            self.ledger.note("sdr2", "failed", why)

    def sdr2_good(self) -> None:
        self.sdr2_trouble_log = []

    def antenna(self) -> str:
        return self.s["radio"].get("sdr2_antenna", "adsb")

    def installed(self, key: str) -> bool:
        p = self.s["aircraft"]["readsb"] if key == "readsb" else self.s["satellites"]["satdump"]
        return os.path.isfile(p) and os.access(p, os.X_OK)

    def next_planned(self, device: str, after: float) -> dict | None:
        c = [p for p in self.plan if p["decision"] == "record" and p["device"] == device and p["end"] > after]
        return min(c, key=lambda p: p["start"]) if c else None

    # settings / dongles / plan
    def refresh(self, force: bool = False) -> None:
        now = self.clock()
        try:
            mt = Path(os.environ.get("PREPPER_CONFIG", str(C.CONFIG))).stat().st_mtime
        except OSError:
            mt = None
        if force or mt != self.cfg_mtime:
            self.cfg_mtime = mt
            self.s = C.settings()
            self.plan_key = None
        if force or now - self.assign_at > 15:
            self.assign_at = now
            self.s = C.settings()                # picks up an antenna change made in the launcher
            new = dongles.assign(self.s["radio"])
            if new != self.assign:
                self.plan_key = None
            self.assign = new
        tle_mt = tuple(f.stat().st_mtime if f.exists() else 0 for f in tle.files())
        key = (tle_mt, json.dumps(self.s["satellites"], sort_keys=True), json.dumps(self.s["radio"], sort_keys=True),
               json.dumps(self.assign, sort_keys=True), bool(self.sdr2_ok()))
        if self.plan_key != key or now - self.plan_at > self.replan_s:
            self.replan(key)

    def replan(self, key=None) -> None:
        now = self.clock()
        s = self.s
        self.plan_key, self.plan_at = key, now
        self.pos = C.position(s["home"])
        sats_cfg = s["satellites"]["list"]
        self.tle_rows = tle.info(sats_cfg, s["satellites"]["tle_stale_days"], s["satellites"]["tle_max_days"], now)
        self.mode = satplan.capture_mode(s, self.assign, bool(self.sdr2_ok()))
        passes = []
        if self.pos and s["satellites"]["enabled"]:
            try:
                passes = satplan.predict(tle.load(sats_cfg), sats_cfg, self.pos, now - 1200, 24.0,
                                         record_above=s["satellites"]["record_above"])
            except Exception as e:  # noqa: BLE001 — bad orbit data must not stop the scheduler
                log(f"pass prediction failed: {type(e).__name__}: {e}")
        busy = []
        j = self.busy(self.mode["device"] or "")
        if j and j.kind == "aircraft" and not j.always:
            busy.append((j.started, j.until or now))
        labels = {"image": "Recorded — there is a new picture.", "no image": "Recorded, but no picture came out.",
                  "failed": "Recording failed."}
        done = [dict(h, label=labels.get(h.get("result")) or f"Skipped: {h.get('detail') or 'the radio was not free'}.")
                for h in self.ledger.history() if h.get("kind") == "satellite" and isinstance(h.get("tca"), (int, float))]
        for jj in self.jobs.values():
            if jj and jj.is_alive() and jj.kind == "satellite":
                done.append({"norad": jj.meta.get("norad"), "tca": jj.meta.get("tca"),
                             "label": "Recording now." if jj.state != "processing" else "Recorded; making the picture…"})
        self.plan = satplan.plan(passes, s, self.mode, self.ledger.days(), now, self.tle_rows, busy=busy,
                                 last_lend_end=self.ledger.last_lend_end if self.mode["device"] == "weather" else None,
                                 done=done)
        clock_bad = any(r.get("loaded") and r.get("age_days", 0) < -3 for r in self.tle_rows)
        sched = {"generated": round(now), "mode": self.mode, "clock_suspect": clock_bad,
                 "position": ({"lat": round(self.pos["lat"], 2), "lon": round(self.pos["lon"], 2),
                               "source": self.pos["source"]} if self.pos else None),
                 "tle": self.tle_rows, "passes": self.plan[:60]}
        with contextlib.suppress(OSError):
            C.atomic_write(C.run_dir() / "schedule.json", json.dumps(sched), 0o644)

    # aircraft requests from the portal
    def handle_cmd(self) -> None:
        p = C.run_dir() / "aircraft.cmd"
        if not p.exists():
            return
        d = C.read_json(p, None, limit=4096)
        with contextlib.suppress(OSError):
            p.unlink()
        if not isinstance(d, dict) or d.get("action") not in ("scan", "stop"):
            return
        rid = str(d.get("id", ""))[:40] if ID_RE.match(str(d.get("id", ""))) else None
        by = "".join(c for c in str(d.get("by", ""))[:40] if c.isprintable())
        self.air["request"] = rid
        if d["action"] == "stop":
            for j in self.jobs.values():
                if j and j.is_alive() and j.kind == "aircraft" and not j.always:
                    j.stop()
                    self.air["message"] = "Stopped. The weather radio is listening again." if j.device == "weather" else "Stopped."
                    return
            self.air["message"] = "No scan was running."
            return
        mins = d.get("minutes")
        mins = mins if isinstance(mins, int) and not isinstance(mins, bool) else self.s["aircraft"]["scan_minutes"]
        mins = max(1, min(mins, self.s["aircraft"]["scan_minutes"]))
        ok, msg, dev, secs = self.scan_decision(mins * 60)
        self.air["message"] = msg
        if ok:
            j = Job(self, "aircraft", dev, secs, by=by, request=rid)
            self.jobs[dev] = j
            j.start()
            self.plan_key = None              # a scan reshapes the satellite plan

    def scan_decision(self, secs: float) -> tuple[bool, str, str | None, float]:
        s, now = self.s, self.clock()
        a, radio = s["aircraft"], s["radio"]
        if not a["enabled"]:
            return False, "Aircraft scanning is turned off.", None, 0
        if not self.installed("readsb"):
            return False, "The aircraft decoder (readsb) isn't installed on this Badger.", None, 0
        for j in self.jobs.values():
            if j and j.is_alive() and j.kind == "aircraft":
                if j.always:
                    return False, "Already watching for aircraft all the time on SDR 2.", None, 0
                return False, f"A scan is already running until {satplan.hhmm(j.until or now)}.", None, 0
        sdr2 = self.sdr2_ok()
        if sdr2:
            if self.antenna() in ("adsb", "both"):
                j = self.busy("sdr2")
                if j and j.kind == "satellite":
                    return False, (f"SDR 2 is recording a satellite pass until {satplan.hhmm(j.until or now)}; aircraft "
                                   "come back by themselves after it."), None, 0
                return False, "SDR 2 watches for aircraft all the time; it is starting up.", None, 0
            j = self.busy("sdr2")
            if j:
                return False, f"SDR 2 is busy with a {LABEL[j.kind]} until {satplan.hhmm(j.until or now)}.", None, 0
            nxt = self.next_planned("sdr2", now)
            dur = min(secs, (nxt["start"] - 60 - now) if nxt else secs)
            if dur >= 120:
                return True, (f"Scanning on SDR 2 until {satplan.hhmm(now + dur)}. It has the satellite antenna, so it "
                              "hears fewer planes than the aircraft whip would."), "sdr2", dur
            return False, f"A satellite pass starts on SDR 2 at {satplan.hhmm(nxt['start'])}; try after it.", None, 0
        # fallback: SDR 2 missing or failing — borrow the weather radio, cautiously
        if not radio["sdr_lend"]:
            return False, "The weather radio is set not to lend its dongle ([radio] sdr_lend = false).", None, 0
        if self.assign["weather"].get("present") is False:
            return False, "No radio is plugged in.", None, 0
        live = C.alerts_live()
        if live:
            return False, (f"A weather alert is live ({str(live[0].get('event', 'alert'))[:60]}); the radio keeps "
                           "listening for updates. Try again when it has ended."), None, 0
        j = self.busy("weather")
        if j:
            return False, f"The radio is busy with a {LABEL[j.kind]} until {satplan.hhmm(j.until or now)}.", None, 0
        used = int(self.ledger.day(now).get("scans") or 0)
        if used >= a["scans_per_day"]:
            return False, (f"That's today's {a['scans_per_day']} scans. Each scan pauses weather alerts, so they are "
                           "limited while SDR 2 isn't working. Try again tomorrow."), None, 0
        gap = radio["lend_gap_min"] * 60
        last = self.ledger.last_lend_end
        if last and now < last + gap:
            return False, (f"The weather radio just got back; it listens at least {radio['lend_gap_min']:g} minutes "
                           f"between loans. Try again at {satplan.hhmm(last + gap)}."), None, 0
        nxt = self.next_planned("weather", now)
        dur = secs
        if nxt:
            room = nxt["start"] - gap - now
            if room < 300:
                return False, (f"A weather-satellite pass is planned at {satplan.hhmm(nxt['start'])}; try again after "
                               f"{satplan.hhmm(nxt['end'] + gap)}."), None, 0
            dur = min(secs, room)
        return True, (f"SDR 2 isn't working, so this scan borrows the weather radio until {satplan.hhmm(now + dur)}. "
                      "Weather alerts are paused until then."), "weather", dur

    # second dongle, always-on aircraft
    def manage_always(self) -> None:
        now = self.clock()
        sdr2 = self.sdr2_ok()
        want = bool(sdr2 and self.antenna() in ("adsb", "both") and self.s["aircraft"]["enabled"]
                    and self.installed("readsb"))
        j = self.jobs.get("sdr2")
        alive = j and j.is_alive()
        if alive and j.kind == "aircraft" and j.always and not want:
            j.stop()
            return
        nxt = self.next_planned("sdr2", now) if self.mode["device"] == "sdr2" else None
        if alive and j.kind == "aircraft" and j.always and nxt and nxt["start"] - now < 90:
            j.stop()                              # make way for the pass
            return
        if want and not alive and now >= self.next_always and not (nxt and nxt["start"] - now < 300):
            self.next_always = now + 30
            self.jobs["sdr2"] = Job(self, "aircraft", "sdr2", 0, always=True, by="always on")
            self.jobs["sdr2"].start()

    # satellite passes
    def start_due(self) -> None:
        now = self.clock()
        for p in self.plan:
            if p["decision"] != "record" or not p["start"] or p["start"] - now > 20 or p["end"] - now < 90:
                continue
            dev = p["device"]
            j = self.jobs.get(dev)
            if j and j.is_alive():
                if j.kind == "satellite" and j.meta.get("norad") == p["norad"] and abs(j.meta.get("tca", 0) - p["tca"]) < 300:
                    continue
                if j.kind == "aircraft" and j.always:
                    j.stop()                      # handled next tick, once it has let go
                    continue
                if j.kind == "satellite":
                    continue                      # another pass still running (shouldn't happen: plan avoids overlaps)
                self.skip(p, f"the radio was busy with a {LABEL[j.kind]}")
                continue
            if dev == "weather":
                why = None
                if not self.s["radio"]["sdr_lend"]:
                    why = "the weather radio is set not to lend its dongle"
                elif C.alerts_live():
                    why = "a weather alert is live, so the radio kept listening"
                if why:
                    self.skip(p, why)
                    continue
            if not self.installed("satdump"):
                self.skip(p, "SatDump isn't installed")
                continue
            where = "SDR 2" if dev == "sdr2" else "the weather radio (fallback)"
            log(f"recording {p['name']} (max {p['max_elev']}°) on {where} until {satplan.hhmm(p['end'])}")
            job = Job(self, "satellite", dev, p["end"] - now, meta=p)
            job.until = p["end"]
            self.jobs[dev] = job
            job.start()
            p["decision"] = "recording"

    def skip(self, p: dict, why: str) -> None:
        p["decision"], p["reason"] = "skip", f"Skipped: {why}."
        self.ledger.note("satellite", "skipped", why, name=p["name"], norad=p["norad"], tca=p["tca"])
        log(f"skipped {p['name']}: {why}")

    def status(self) -> dict:
        now = self.clock()
        s, a = self.s, self.assign
        with self.lock:
            paused = dict(self.paused) if self.paused else None
        air_job = next((j for j in self.jobs.values() if j and j.is_alive() and j.kind == "aircraft"), None)
        sat_job = next((j for j in self.jobs.values() if j and j.is_alive() and j.kind == "satellite"), None)
        sdr2 = a.get("sdr2") or {}
        ok2 = bool(self.sdr2_ok())
        if not sdr2:
            s2state = "off"
        elif not sdr2.get("present"):
            s2state = "missing"
        elif not ok2:
            s2state = "failed"
        else:
            s2state = "ok"
        if not s["aircraft"]["enabled"]:
            ast = "off"
        elif air_job:
            ast = air_job.state
        elif not self.installed("readsb"):
            ast = "unavailable"
        else:
            ast = "idle"
        scans_used = int(self.ledger.day(now).get("scans") or 0)
        nxt = next((p for p in self.plan if p["decision"] in ("record", "recording") and (p["end"] or 0) > now), None)
        return {
            "weather_radio": "paused" if paused else C.weather_listening_state(),
            "by": paused["by"] if paused else None, "until": paused["until"] if paused else None,
            "dongles": {"count": a.get("count"), "weather": a["weather"].get("serial"),
                        "weather_present": a["weather"].get("present"), "sdr2": sdr2.get("serial"),
                        "sdr2_present": sdr2.get("present"), "sdr2_state": s2state,
                        "sdr2_failed_until": self.sdr2_failed_until if s2state == "failed" else None,
                        "sdr2_why": self.sdr2_why if s2state == "failed" else None,
                        "antenna": self.antenna(), "fallback": not ok2,
                        "problem": a.get("problem"), "note": a.get("note")},
            "aircraft": {"enabled": s["aircraft"]["enabled"], "state": ast,
                         "device": air_job.device if air_job else None,
                         "until": air_job.until if air_job else None, "by": air_job.by if air_job else None,
                         "message": (air_job.message if air_job and air_job.message else self.air["message"]),
                         "request": self.air["request"], "installed": self.installed("readsb"),
                         "always_on": bool(air_job and air_job.always),
                         "scans_left": max(0, s["aircraft"]["scans_per_day"] - scans_used),
                         "scan_minutes": s["aircraft"]["scan_minutes"],
                         "dedicated": ok2},
            "satellites": {"enabled": s["satellites"]["enabled"],
                           "state": sat_job.state if sat_job else ("idle" if s["satellites"]["enabled"] else "off"),
                           "device": self.mode.get("device"), "mode": self.mode.get("why"),
                           "installed": self.installed("satdump"),
                           "current": ({"name": sat_job.meta.get("name"), "until": sat_job.until} if sat_job else None),
                           "next": ({"name": nxt["name"], "start": nxt["start"], "end": nxt["end"],
                                     "max_elev": nxt["max_elev"]} if nxt else None),
                           "used_today_min": round(float(self.ledger.day(now).get("sat_s") or 0) / 60.0, 1)},
            "history": self.ledger.history()[-10:],
        }

    def write_status(self) -> None:
        with contextlib.suppress(OSError):
            C.write_status(self.status())

    def tick(self) -> None:
        self.refresh()
        self.handle_cmd()
        for dev, j in list(self.jobs.items()):
            if j and not j.is_alive():
                if j.kind == "satellite" or (j.kind == "aircraft" and not j.always):
                    self.plan_key = None
                if j.kind == "aircraft" and not j.always:
                    self.air["message"] = j.message or ("Scan finished. The weather radio is listening again."
                                                        if j.device == "weather" else "Scan finished.")
                self.jobs[dev] = None
        self.manage_always()
        self.start_due()
        self.write_status()

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log("started")
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — keep scheduling whatever happens
                log(f"tick failed: {type(e).__name__}: {e}")
            self._wake.wait(1.0)
            self._wake.clear()
        for j in self.jobs.values():
            if j and j.is_alive():
                j.stop()
        for j in self.jobs.values():
            if j:
                j.join(30)
        self.write_status()


def main() -> int:
    os.umask(0o027)
    C.run_dir().mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    Daemon().run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
