#!/usr/bin/env python3
"""
panel — the Badger's own screen and knob. Works with no phone at all.

Hardware: 1.3" SH1106 / 0.96" SSD1306 OLED (I²C 0x3C) + rotary encoder with
push button (GPIO 17/27/22 by default) + status LED (GPIO 23). Turn = move, press = select,
hold 3 s on the home screen = shut down (with confirmation).

  Home         SSID · clients · battery · address, cycles every 5 s
  Join Wi-Fi   QR code phones scan to join
  Open app     QR code for the launcher URL
  Invite       QR code that makes a family member (one use, 24 h)
  Family radio QR code of the family channel link (holds the channel key: shown here, on
               the Badger itself, rather than sent over plain HTTP) and the radio's Bluetooth PIN
  Status       battery, storage, temperature, updates, GPS, radio dongle problems (scrolls)
  Sensors      active alerts first, then readings           (only with the sensor station)
  Radio        weather radio · SDR 2 · forecast · SDR 2 antenna (adsb / satellite / both)
  Neighbourhood  aid station open/closed · missing people   (only with those features)
  Power mode   Endurance / Balanced / Full
  Services     AI, Kolibri, mesh, weather alerts — on/off
  Updates      check · download · import from USB · export to USB
  USB          safely eject
  Wi-Fi        on / off
  System       restart · shut down

`python3 panel.py --console` runs the same menus in a terminal
(a/d = turn, space = press, h = hold, q = quit) for testing on a laptop.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
import subprocess
import sys
import textwrap
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

try:                                    # the radio features' shared settings (software/sdr), if shipped
    sys.path.append(str(Path(__file__).resolve().parent.parent / "sdr"))
    import sdrcommon as _sdrc
except Exception:  # noqa: BLE001 — the panel must start without the radio code
    _sdrc = None

CTL = os.environ.get("PREPPER_CTL", "/opt/prepper/bin/prepperctl")
API = os.environ.get("PREPPER_API", "http://127.0.0.1:8000")
# The panel proves to the portal that it is the panel (for invites and the precise GPS position)
# with a key only root can read. The portal holds just its SHA-256, readable by group prepper.
PANEL_KEY = Path(os.environ.get("PANEL_KEY", "/etc/prepper/panel.key"))
PANEL_KEY_HASH = Path(os.environ.get("PANEL_KEY_HASH", "/etc/prepper/panel.key.sha256"))
_key_cache: dict = {}


# ───────────────────────────── data ──────────────────────────────────────────

def ensure_panel_key(key_path: Path = PANEL_KEY, hash_path: Path = PANEL_KEY_HASH, group: str = "prepper") -> str | None:
    """Make the panel key (root:root 0600) and its hash (root:prepper 0640) if missing; return the key.
    The panel runs as root (it drives GPIO and calls prepperctl directly), so only it — not the portal's
    user, not any other service account — can present the key."""
    try:
        key = key_path.read_text().strip()
    except OSError:
        key = ""
    try:
        if not key:
            key = secrets.token_urlsafe(32)
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(key + "\n")
        want = hashlib.sha256(key.encode()).hexdigest()
        try:
            have = hash_path.read_text().strip()
        except OSError:
            have = ""
        if have != want:
            tmp = hash_path.with_name(f".{hash_path.name}.{os.getpid()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o640)
            with os.fdopen(fd, "w") as fh:
                fh.write(want + "\n")
            if os.geteuid() == 0:
                import grp
                try:
                    os.chown(tmp, 0, grp.getgrnam(group).gr_gid)
                except KeyError:
                    pass
            os.chmod(tmp, 0o640)
            os.replace(tmp, hash_path)
        return key
    except OSError as e:
        print(f"panel: cannot set up the panel key ({e}); invites from this screen won't work", file=sys.stderr)
        return key or None


def panel_headers() -> dict:
    if "key" not in _key_cache:
        try:
            _key_cache["key"] = PANEL_KEY.read_text().strip()
        except OSError:
            _key_cache["key"] = ""
    return {"X-Badger-Panel": _key_cache["key"]} if _key_cache["key"] else {}


def api(path: str, limit: int | None = None) -> dict:
    try:
        req = urllib.request.Request(API + path, headers=panel_headers())
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.loads(r.read(limit) if limit else r.read())   # a cut-off reply fails to parse: {}
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def api_post(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(API + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json", **panel_headers()})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"error": f"HTTP {e.code}"}
    except (OSError, ValueError):
        return {"error": "launcher not answering"}


def read_secret_json(path: Path, key: str) -> str | None:
    """A value from one of the mesh bridge's owner-only files (the panel runs as root)."""
    try:
        d = json.loads(path.read_text())
        v = d.get(key) if isinstance(d, dict) else None
        return v if isinstance(v, str) and v else None
    except (OSError, ValueError):
        return None


def ctl(*args: str) -> str:
    if not Path(CTL).exists():
        return f"(test) {' '.join(args)}"
    try:
        r = subprocess.run([CTL, *args], capture_output=True, text=True, timeout=120)
        return (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else "OK"
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)


def human(n) -> str:
    if n is None:
        return "?"
    for u in ("B", "K", "M", "G", "T"):
        if n < 1000:
            return f"{n:.0f}{u}"
        n /= 1000
    return f"{n:.0f}P"


# ───────────────────────────── feature files (radio, sensors, forecast) ──────
# Other services write these, as other users; the panel runs as root. Every read is size-capped,
# never follows a symlink, never waits on a FIFO, and odd content of any kind just means "no data".

W = 21                                             # characters in one OLED row
SAT = "🛰"                                          # the OLED's font lacks it: OLED.lines swaps in "sat"
ANTENNAS = tuple(getattr(_sdrc, "ANTENNAS", ("adsb", "satellite", "both")))
ANTENNA_LABEL = {"adsb": "ADS-B whip", "satellite": "Satellite", "both": "Both (wideband)"}
ANTENNA_TEXT = {"adsb": "the 1090 MHz aircraft whip", "satellite": "the 137 MHz V-dipole (outdoors)",
                "both": "a wideband antenna (discone)"}
ANTENNA_TEXT.update({k: v for k, v in getattr(_sdrc, "ANTENNA_TEXT", {}).items() if k in ANTENNA_TEXT})
SDR_MAX_AGE = 30.0                                 # sdrd rewrites status.json far more often than this
SENSORS_STOPPED_S = 180.0                          # the portal calls sensord stopped after this, too
NB_POLL_S = 60.0                                   # aid station / missing people: portal reads, gently
NB_KEEP_S = 1800.0                                 # …and the last good answer is shown this long
AID_ITEMS = {"water": ("water", "Water"), "food": ("food", "Food"), "first_aid": ("1st aid", "First aid"),
             "shelter": ("shelter", "Shelter"), "charging": ("power", "Charging"), "info": ("info", "Information")}
FORECAST_ROW = {"ok": "ready", "running": "updating…", "no_signal": "no signal", "no_radio": "no radio",
                "busy": "radio busy", "no_whisper": "not set up", "skipped": "skipped", "error": "failed",
                "stalled": "stuck?"}
# sensor metrics: unit, digits, short label (in the panel's order: the main readings first)
SENSOR_METRICS = {
    "temperature": ("°C", 1, "Temp"), "humidity": ("%", 0, "Humidity"), "water_level": ("cm", 0, "Water"),
    "flood": ("", 0, "Flood"), "radiation": ("µSv/h", 2, "Radiation"), "cpm": ("CPM", 0, "Geiger"),
    "pressure": ("hPa", 0, "Pressure"), "wind_speed": ("m/s", 1, "Wind"), "rain_1h": ("mm", 1, "Rain 1h"),
    "co2": ("ppm", 0, "CO2"), "pm25": ("µg/m³", 0, "PM2.5"), "pm10": ("µg/m³", 0, "PM10"),
    "iaq": ("IAQ", 0, "Air qual"), "lux": ("lx", 0, "Light"), "soil_moisture": ("%", 0, "Soil"),
    "soil_temperature": ("°C", 1, "Soil temp"), "voltage": ("V", 2, "Volts"), "battery": ("%", 0, "Battery"),
}
_file_cache: dict = {}
_file_lock = threading.Lock()


def sdr_run_dir() -> Path:
    return _sdrc.run_dir() if _sdrc is not None else Path(os.environ.get("PREPPER_SDR_DIR", "/run/prepper/sdr"))


def sdr_state_dir() -> Path:
    return _sdrc.state_dir() if _sdrc is not None else Path(os.environ.get("PREPPER_SDR_STATE", "/var/lib/prepper/sdr"))


def clean(s, limit: int = 84) -> str:
    """Someone else's text made fit for the screen: strings only, no control/format characters
    (newlines, bidi overrides…), whitespace collapsed, length capped."""
    if not isinstance(s, str):
        return ""
    s = "".join(" " if c.isspace() or unicodedata.category(c)[0] == "C" else c for c in s[:limit * 2])
    return " ".join(s.split())[:limit]


def skey(v) -> str | None:
    """A JSON value usable as a lookup key (a list or dict from a hostile file would raise)."""
    return v if isinstance(v, str) else None


def finite(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def wrap(text: str, width: int = W, max_lines: int = 14) -> list[str]:
    return textwrap.wrap(text, width, break_long_words=True, break_on_hyphens=False)[:max_lines]


def hm(t) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(t))
    except (OverflowError, ValueError, OSError, TypeError):
        return "--:--"


def ago(secs) -> str:
    secs = finite(secs)
    if secs is None:
        return "?"
    secs = max(0.0, secs)
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs / 60:.0f} min ago"
    if secs < 172800:
        return f"{secs / 3600:.0f} h ago"
    return f"{secs / 86400:.0f} days ago"


def pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid < 2 ** 31:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def read_json_file(path, limit: int = 1_000_000):
    """Parsed JSON from a regular file of at most `limit` bytes, else None. Re-parsed only when the
    file changes (the home screen asks every 5 s). The value is shared: callers must not change it."""
    path = str(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        with _file_lock:
            hit = _file_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        chunks, total = [], 0
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            total += len(b)
            if total > limit:                      # grew while being read
                return None
            chunks.append(b)
        try:
            val = json.loads(b"".join(chunks))
        except (ValueError, RecursionError):
            val = None
        with _file_lock:
            if len(_file_cache) > 64:
                _file_cache.clear()
            _file_cache[path] = (key, val)
        return val
    except OSError:
        return None
    finally:
        os.close(fd)


def write_antenna(value: str, now: float, by: str = "front panel") -> str | None:
    """Save the SDR 2 antenna choice the way the launcher does (sdrcommon.set_antenna: state dir
    antenna.json = {"antenna", "by", "at"}), but safely as root in a folder `prepper` owns: no
    symlink is followed, the scratch file is new (O_EXCL), and the result belongs to the folder's
    owner so sdrd can read it. Returns None when saved, else a short reason."""
    if value not in ANTENNAS:
        return "unknown antenna"
    body = json.dumps({"antenna": value, "by": by, "at": now}).encode()
    try:
        dfd = os.open(sdr_state_dir(), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return "radio not set up"
    tmp = f".antenna.json.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        dst = os.fstat(dfd)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o640, dir_fd=dfd)
        try:
            if os.geteuid() == 0:
                os.fchown(fd, dst.st_uid, dst.st_gid)
            os.fchmod(fd, 0o640)
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, "antenna.json", src_dir_fd=dfd, dst_dir_fd=dfd)
        return None
    except OSError:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        return "could not save"
    finally:
        os.close(dfd)


def fmt_reading(metric: str, v: float, us: bool = False) -> str:
    unit, digits, _ = SENSOR_METRICS[metric]
    if metric == "flood":
        return "WET" if v >= 0.5 else "dry"
    if us and unit == "°C":                        # the same conversions as the launcher's page
        v, unit, digits = v * 9 / 5 + 32, "°F", 1
    elif us and unit == "cm":
        v, unit, digits = v / 2.54, "in", 1
    elif us and unit == "hPa":
        v, unit, digits = v * 0.02953, "inHg", 2
    s = f"{v:.{digits}f}"
    if s.startswith("-") and float(s) == 0:
        s = s[1:]
    return s + ("" if not unit else unit if unit == "%" or unit.startswith("°") else " " + unit)


class Poll:
    """A portal read refreshed in the background, at most every `every` seconds (≥ 30): the knob
    never waits for the network. get() → the last good answer (≤ `keep` s old) or None."""

    def __init__(self, fetch, every: float = NB_POLL_S, keep: float = NB_KEEP_S, clock=time.monotonic):
        self.fetch, self.every, self.keep, self.clock = fetch, max(30.0, every), keep, clock
        self.value, self.got, self.tried, self.busy = None, None, None, False
        self.lock = threading.Lock()

    def kick(self, spawn) -> None:
        with self.lock:
            if self.busy or (self.tried is not None and self.clock() - self.tried < self.every):
                return
            self.busy, self.tried = True, self.clock()
        try:
            spawn(self._run)
        except RuntimeError:                       # can't start a thread right now: try again later
            with self.lock:
                self.busy = False

    def _run(self) -> None:
        try:
            v = self.fetch()
        except Exception:  # noqa: BLE001 — a bad reply must never take the panel down
            v = None
        with self.lock:
            if v is not None:
                self.value, self.got = v, self.clock()
            self.busy = False

    def get(self):
        with self.lock:
            if self.got is None or self.clock() - self.got > self.keep:
                return None
            return self.value


def fetch_aid() -> dict | None:
    d = api("/api/aidstation/status", limit=65536)
    if not isinstance(d.get("set"), bool):
        return None                                # not installed / not answering
    if not d["set"]:
        return {"set": False}
    items = d.get("items") if isinstance(d.get("items"), list) else []
    return {"set": True, "open": d.get("open") is True,
            "items": [i for i in AID_ITEMS if i in items], "text": clean(d.get("text"), 100),
            "updated": finite(d.get("updated")), "stale": d.get("stale") is True}


def fetch_missing() -> dict | None:
    d = api("/api/missing?kind=person&status=missing", limit=2_000_000)
    posts = d.get("posts")
    if not isinstance(posts, list):
        return None
    n, names = 0, []
    for p in posts[:5000]:
        if isinstance(p, dict) and p.get("kind", "person") == "person" and p.get("status") == "missing":
            n += 1
            if len(names) < 20:
                names.append(clean(p.get("name"), 40) or "(no name given)")
    return {"count": n, "names": names}


# ───────────────────────────── displays ──────────────────────────────────────

class OLED:
    W, H = 128, 64

    def __init__(self, cfg: dict):
        from luma.core.interface.serial import i2c
        from luma.oled.device import sh1106, ssd1306
        from PIL import ImageFont
        dev = {"sh1106": sh1106, "ssd1306": ssd1306}[cfg.get("display", "sh1106")]
        self.dev = dev(i2c(port=1, address=int(cfg.get("i2c_address", 0x3C))))
        self.font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
        self.big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
        self.small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 8)

    def lines(self, rows: list[str], title: str | None = None, sel: int | None = None):
        from luma.core.render import canvas
        rows = [r.replace(SAT, "sat") for r in rows]      # DejaVu has ✈ → – but no emoji
        with canvas(self.dev) as d:
            y = 0
            if title:
                d.text((0, 0), title[:18], font=self.big, fill="white")
                d.line((0, 15, 127, 15), fill="white")
                y = 17
            for i, row in enumerate(rows):
                if sel is not None and i == sel:
                    d.rectangle((0, y, 127, y + 11), fill="white")
                    d.text((2, y - 1), row[:21], font=self.font, fill="black")
                else:
                    d.text((2, y - 1), row[:21], font=self.font, fill="white")
                y += 12

    def qr(self, data: str, caption: str):
        import qrcode
        from luma.core.render import canvas
        q = qrcode.QRCode(border=1, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
        q.add_data(data); q.make(fit=True)
        m = q.get_matrix()
        scale = max(1, min(64 // len(m), 3))
        with canvas(self.dev) as d:
            for y, row in enumerate(m):
                for x, on in enumerate(row):
                    if not on:
                        d.rectangle((x * scale, y * scale, x * scale + scale - 1, y * scale + scale - 1), fill="white")
            rows = caption.split("\n")
            if len(rows) <= 5:
                for i, part in enumerate(rows):
                    d.text((len(m) * scale + 3, i * 12), part[:12], font=self.font, fill="white")
            else:                                       # a long Wi-Fi password: every character, smaller
                for i, part in enumerate(rows[:8]):
                    d.text((len(m) * scale + 2, i * 8), part[:14], font=self.small, fill="white")

    def sleep(self, on: bool):
        (self.dev.hide if on else self.dev.show)()


class Console:
    """Terminal stand-in for the OLED, for development."""

    def lines(self, rows, title=None, sel=None):
        out = ["┌" + "─" * 22 + "┐"]
        if title:
            out.append(f"│ {title[:20]:<20} │")
            out.append("├" + "─" * 22 + "┤")
        for i, r in enumerate(rows):
            mark = "▶" if sel is not None and i == sel else " "
            out.append(f"│{mark}{r[:21]:<21}│")
        out.append("└" + "─" * 22 + "┘")
        print("\033[2J\033[H" + "\n".join(out), flush=True)

    def qr(self, data, caption):
        self.lines(caption.split("\n") + [f"[QR] {data[:16]}"], title="Scan me")

    def sleep(self, on):
        if on:
            print("\033[2J\033[H(screen asleep)", flush=True)


# ───────────────────────────── menus ─────────────────────────────────────────

class Panel:
    def __init__(self, disp, cfg: dict):
        self.d, self.cfg = disp, cfg
        net = cfg.get("network", {})
        self.ssid, self.psk = net.get("ssid", "HoneyBadger"), net.get("passphrase", "")
        self.url = f"http://{net.get('hostname', 'badger')}.lan"
        portal = cfg.get("portal", {})
        self.link_file = Path(portal.get("mesh_channel_link", "/var/lib/prepper/portal/mesh-channel.json"))
        self.bt_pin_file = Path(portal.get("mesh_bt_pin_file", "/var/lib/prepper/portal/mesh-bt-pin.json"))
        self.stack: list[tuple[str, list]] = []
        self.sel = 0
        self.screen = "home"              # home | menu | qr | msg | confirm
        self.home_page = 0
        self.last_input = time.monotonic()
        self.asleep = False
        self.msg = ""
        self.confirm_action = None
        self.lock = threading.RLock()
        self.sleep_after = float(cfg.get("panel", {}).get("sleep_after_s", 45))
        self.timefmt_file = Path(os.environ.get("PANEL_TIMEFMT", "/var/lib/prepper/panel-timefmt"))
        try:
            self.timefmt = self.timefmt_file.read_text().strip()
        except OSError:
            self.timefmt = ""                 # empty = follow the Badger-wide default
        fd = portal.get("features_data") if isinstance(portal, dict) else None
        self.features_dir = Path(fd) if isinstance(fd, str) and os.path.isabs(fd) else Path("/var/lib/prepper/portal/features")
        self.clock = time.time                # wall clock, for the other services' timestamps (tests swap it)
        self.spawn = lambda fn: threading.Thread(target=fn, daemon=True).start()
        self.polls = {"aidstation": Poll(fetch_aid), "missing": Poll(fetch_missing)}

    # — menu tree (built fresh so labels show current state) —
    def root(self) -> list:
        s = api("/api/status")
        svc = s.get("services", {})
        onoff = lambda k: "on " if svc.get(k) else "off"
        return [
            # the password is printed too: this screen is the way back in if a new password was lost
            ("Join Wi-Fi (QR)", self.wifi_screen),
            ("Open app (QR)", lambda: self.show_qr(self.url, "Open\n" + self.url.replace("http://", ""))),
            ("Invite member (QR)", lambda: self.confirm("New family\ninvite?", self.invite_qr)),
            ("Family radio", [("Channel link (QR)", lambda: self.confirm("Show radio\nkey QR?", self.radio_link_qr)),
                              ("Bluetooth PIN", self.bt_pin)]),
            ("Status", self.status_rows),
            *self.feature_entries(),
            ("Power mode", [("Endurance", lambda: self.run("power-mode", "endurance")),
                            ("Balanced", lambda: self.run("power-mode", "balanced")),
                            ("Full", lambda: self.run("power-mode", "full"))]),
            ("Services", [(f"AI  [{onoff('llama')}]", lambda: self.toggle("llama", svc)),
                          (f"Courses [{onoff('kolibri')}]", lambda: self.toggle("kolibri", svc)),
                          (f"Mesh [{onoff('mesh')}]", lambda: self.toggle("mesh", svc)),
                          (f"Alerts [{onoff('alerts')}]", lambda: self.toggle("alerts", svc))]),
            ("Updates", [("Check now", lambda: self.run("update-check")),
                         ("Download now", lambda: self.run("update-apply")),
                         ("Import from USB", self.import_usb),
                         ("Export to USB", self.export_usb)]),
            ("Time format", [(f"{'● ' if self.time_mode(s) == m else '  '}{l}", (lambda m=m: self.set_timefmt(m)))
                             for m, l in (("local", "Local"), ("utc", "UTC"), ("nutime", "Nu:Time™"))]),
            ("USB: eject", self.eject_usb),
            ("Wi-Fi", [("Turn on", lambda: self.run("wifi-on")),
                       ("Turn off", lambda: self.confirm("Wi-Fi off?", lambda: self.run("wifi-off")))]),
            ("System", [("Restart", lambda: self.confirm("Restart?", lambda: self.run("reboot"))),
                        ("Shut down", lambda: self.confirm("Shut down?", lambda: self.run("shutdown")))]),
        ]

    def time_mode(self, s: dict) -> str:
        return self.timefmt or (s.get("clock") or {}).get("display", "local")

    def set_timefmt(self, m: str):
        self.timefmt = m
        try:
            self.timefmt_file.write_text(m)
        except OSError:
            pass
        self.show_msg(f"Times: {dict(local='Local', utc='UTC', nutime='Nu:Time™')[m]}")

    def clock_text(self, s: dict) -> tuple[str, list[str]]:
        c = s.get("clock") or {}
        texts = {"local": c.get("local_hms", "--:--:--"), "utc": c.get("utc_hms", "--:--:-- UTC"), "nutime": c.get("nutime", "--:--:-- NT")}
        m = self.time_mode(s)
        return texts[m], [texts[k] for k in ("local", "utc", "nutime") if k != m]

    def wifi_screen(self):
        """QR + the whole password beside it, 9 characters a line (WPA allows 63: up to 7 lines,
        which the display draws in a smaller font)."""
        if not self.psk:
            return self.show_qr(self.wifi_qr(), f"{self.ssid[:9]}\nopen")
        self.show_qr(self.wifi_qr(), f"{self.ssid[:9]}\n" + "\n".join(self.psk[i:i + 9] for i in range(0, len(self.psk), 9)))

    def radio_link_qr(self):
        url = read_secret_json(self.link_file, "url")
        if not url:
            return self.show_msg("No radio link\nyet: is the mesh\nradio plugged in?")
        self.show_qr(url, "Family\nradio\nchannel\nScan in\nMeshtastic")

    def bt_pin(self):
        pin = read_secret_json(self.bt_pin_file, "pin")
        self.show_msg(f"Radio Bluetooth\nPIN: {pin}" if pin else "Radio Bluetooth\nPIN: unchanged\n(see the radio's\nown screen/manual)")

    def wifi_qr(self) -> str:
        esc = lambda s: "".join("\\" + c if c in '\\;,":' else c for c in s)
        return f"WIFI:T:WPA;S:{esc(self.ssid)};P:{esc(self.psk)};;" if self.psk else f"WIFI:T:nopass;S:{esc(self.ssid)};;"

    def invite_qr(self):
        """A one-time family invite, as a QR code the new member scans on the Badger's Wi-Fi."""
        r = api_post("/api/local/invite")
        r = r if isinstance(r, dict) else {"error": "odd reply from the launcher"}
        code = r.get("code")
        if not isinstance(code, str) or not code or len(code) > 20:
            err = r.get("error", "?")
            return self.show_msg("No invite:\n" + (err if isinstance(err, str) else "?")[:40])
        self.show_qr(f"{self.url}/#/join/{code}", f"Family\ninvite\n{code[:5]}\n{code[5:]}\n24h, once")

    def status_rows(self):
        s = api("/api/status")
        p, st, u = s.get("power") or {}, s.get("storage") or {}, (s.get("updates") or {}).get("counts") or {}
        g = s.get("gps") or {}
        rows = [
            f"Batt {p.get('percent', '?')}% {'chg' if p.get('charging') else ''}".strip(),
            f"~{(p.get('runtime_min') or 0) / 60:.1f}h left" if p.get("runtime_min") else f"Mode {p.get('mode', '?')}",
            f"In {p['input_v']:.1f}V {p['input_w']:.0f}W" if p.get("input_v") is not None else "Input: no meter",
            f"Free {human(st.get('free'))} of {human(st.get('total'))}",
            f"CPU {s.get('cpu_temp_c') or '?'}C  {s['clients'] if s.get('clients') is not None else '?'} users",
            f"Updates: {(u.get('update', 0) + u.get('install', 0))} pending",
            f"GPS {g['lat']:.3f},{g['lon']:.3f}" if g.get("lat") is not None else "GPS: no fix",
        ]
        items = [(r, None) for r in rows]
        try:
            items += self.radio_problem_rows(self.sdr_status())
        except Exception:  # noqa: BLE001 — never lose the status screen over the radio's file
            pass
        self.show_list("Status", items)                # scrolls with the knob; press = back

    # — features: radio, sensors, forecast, neighbourhood —
    def section(self, name: str) -> dict:
        v = self.cfg.get(name)
        return v if isinstance(v, dict) else {}

    def feature_on(self, fid: str) -> bool:
        return self.section("features").get(fid) is not False and self.section(fid).get("enabled", True) is not False

    def feature_entries(self) -> list:
        """Root menu entries for the features that are installed and have something to show."""
        out = []
        try:
            s = self.sensors()
            if s is not None:
                lv = {a["level"] for a in s["alerts"] if not a["stale"]}
                out.append(("Sensors" + (" !! DANGER" if "danger" in lv else " ! alert" if lv else ""), self.sensors_screen))
        except Exception:  # noqa: BLE001 — a feature's odd file must never cost the whole menu
            pass
        try:
            st = self.sdr_status()
            if st is not None or self.forecast() is not None or self.sdr_installed():
                out.append(("Radio" + (" ! WX paused" if self.wx_pause(st) else ""), self.radio_screen))
        except Exception:  # noqa: BLE001
            pass
        try:
            self.poll_neighbourhood()
            if any(p.get() is not None for p in self.polls.values()):
                out.append(("Neighbourhood", self.neighbourhood_screen))
        except Exception:  # noqa: BLE001
            pass
        return out

    # radio (sdrd's status.json; see software/sdr/sdrcommon.py and sdrd.py)
    def sdr_installed(self) -> bool:
        return sdr_state_dir().is_dir()

    def sdr_status(self) -> dict | None:
        """sdrd's status, or None when it is missing, odd, over 30 s old, or its writer is gone."""
        st = read_json_file(sdr_run_dir() / "status.json")
        if not isinstance(st, dict):
            return None
        up, now = finite(st.get("updated")), self.clock()
        if up is None or not -SDR_MAX_AGE <= now - up <= SDR_MAX_AGE or not pid_alive(st.get("pid")):
            return None
        return st

    def wx_pause(self, st: dict | None) -> tuple[str, float] | None:
        """(what, until) while the weather radio is lent out — NOAA alerts are not heard then."""
        if not st or st.get("weather_radio") != "paused":
            return None
        until = finite(st.get("until"))
        if until is None or until <= self.clock():
            return None
        by = st.get("by")
        what = {"satellite pass": "sat pass", "aircraft scan": "aircraft scan"}.get(by) if isinstance(by, str) else None
        return (what or clean(by, 14) or "radio job"), until

    def wx_row(self, st: dict) -> tuple[str, list[str]]:
        p = self.wx_pause(st)
        if p:
            what, until = p
            return (f"!WX PAUSED: {what[:3]}→{hm(until)}",
                    ["WX PAUSED:", f"{what} → {hm(until)}", *wrap("NOAA weather alerts are not heard until then; the radio goes back "
                                        "to listening by itself.")])
        if st.get("weather_radio") == "listening":
            return "WX radio: listening", ["Weather radio is", "listening for NOAA", "alerts."]
        return "WX radio: off", wrap("The weather radio is not listening (alerts service stopped or no "
                                     "dongle). See Services.")

    def dongles(self, st: dict | None) -> dict:
        d = st.get("dongles") if isinstance(st, dict) else None
        return d if isinstance(d, dict) else {}

    def chosen_antenna(self) -> str:
        o = read_json_file(sdr_state_dir() / "antenna.json", 4096)
        if isinstance(o, dict) and o.get("antenna") in ANTENNAS:
            return o["antenna"]
        v = self.section("radio").get("sdr2_antenna")
        v = v.strip().lower() if isinstance(v, str) else "adsb"
        return v if v in ANTENNAS else "adsb"

    def sdr2_row(self, st: dict) -> tuple[str, list[str]] | None:
        d = self.dongles(st)
        sub = lambda k: st.get(k) if isinstance(st.get(k), dict) else {}
        sat, air = sub("satellites"), sub("aircraft")
        cur = sat.get("current") if isinstance(sat.get("current"), dict) else {}
        state = skey(d.get("sdr2_state"))
        if state == "missing":
            row = "SDR2 MISSING–fallback"
        elif state == "failed":
            row = "SDR2 FAILED–fallback"
        elif sat.get("state") == "recording":
            until = finite(cur.get("until"))
            row = f"SDR2 {SAT} REC" + (f" → {hm(until)}" if until else "")
        elif air.get("state") in ("always_on", "scanning", "starting", "restarting"):
            row = "SDR2 ✈ aircraft"
        elif sat.get("state") == "processing":
            row = f"SDR2 {SAT} processing"
        elif state == "off":
            row = "SDR2 off"
        elif state == "ok":
            row = "SDR2 idle"
        else:
            return None
        lines = [row]
        words = {"ok": "working", "missing": "not plugged in", "failed": "keeps failing", "off": "not used"}
        if state in words:
            lines.append(f"SDR 2: {words[state]}")
        if state == "failed" and clean(d.get("sdr2_why")):
            lines += wrap("Why: " + clean(d.get("sdr2_why"), 200))
        if state in ("missing", "failed"):
            lines += wrap("Aircraft and satellites borrow the weather radio's dongle now and then, by the "
                          "careful rules.")
        if sat.get("state") == "recording" and clean(cur.get("name")):
            lines += wrap("Recording " + clean(cur.get("name"), 40))
        ant = d.get("antenna") if d.get("antenna") in ANTENNAS else self.chosen_antenna()
        lines += [f"Antenna: {ant}", *wrap(ANTENNA_TEXT[ant])]
        return row, lines

    def radio_problem_rows(self, st: dict | None) -> list:
        """dongles.problem / dongles.note (e.g. the dongle serials are not set) as rows + details."""
        d, out = self.dongles(st), []
        for key, mark in (("problem", "! Radio: "), ("note", "Radio: ")):
            text = clean(d.get(key), 300)
            if text:
                out.append((mark + text, self.page("Radio", wrap(text))))
        return out

    def antenna_menu(self) -> list:
        cur = self.chosen_antenna()
        return [(f"{'● ' if a == cur else '  '}{ANTENNA_LABEL.get(a, a)}", (lambda a=a: self.set_antenna(a)))
                for a in ANTENNAS]

    def set_antenna(self, value: str):
        why = write_antenna(value, self.clock())
        if why:
            return self.show_msg(f"Antenna not saved:\n{why}")
        self.show_msg("\n".join(["SDR 2 antenna:", ANTENNA_LABEL.get(value, value),
                                 *wrap(ANTENNA_TEXT.get(value, ""))[:2], "Radio follows ~15 s"]))

    def radio_screen(self):
        st, rows = self.sdr_status(), []
        if st is None:
            if self.sdr_installed():
                rows.append(("Radio service off", self.page("Radio", wrap(
                    "The radio scheduler isn't running (or hasn't said anything for 30 s)."))))
        else:
            r, lines = self.wx_row(st)
            rows.append((r, self.page("Weather radio", lines)))
            s2 = self.sdr2_row(st)
            if s2:
                rows.append((s2[0], self.page("SDR 2", s2[1])))
            rows += self.radio_problem_rows(st)
        fc = self.forecast()
        if fc is not None:
            rows.append((self.forecast_row(fc), self.page("Forecast", self.forecast_lines(fc))))
        if st is not None or self.sdr_installed():
            rows.append(("SDR 2 antenna", self.antenna_menu()))
        self.show_list("Radio", rows)

    # forecast (software/speech/forecastd.py → the forecast feature's folder)
    def forecast(self) -> dict | None:
        if not self.feature_on("forecast"):
            return None
        base = self.features_dir / "forecast"
        st, latest = read_json_file(base / "status.json", 200_000), read_json_file(base / "latest.json", 4_000_000)
        st = st if isinstance(st, dict) else None
        latest = latest if isinstance(latest, dict) else None
        if st is None and latest is None:
            return None
        state = skey(st.get("state")) if st else None
        state = state if state in FORECAST_ROW else None
        at = finite(st.get("at")) if st else None
        if state in ("running", "busy") and (at is None or self.clock() - at > 3600):
            state = "stalled" if state == "running" else state     # a run killed half-way
        return {"state": state, "message": clean(st.get("message"), 300) if st else "", "at": at,
                "short": clean(latest.get("short") or latest.get("headline"), 300) if latest else "",
                "heard": finite(latest.get("at")) if latest else None}

    def forecast_row(self, fc: dict) -> str:
        if fc["state"] in (None, "ok") and fc["heard"]:
            return f"Forecast {ago(self.clock() - fc['heard'])}"
        return f"Forecast: {FORECAST_ROW.get(fc['state'], 'none yet')}"

    def forecast_lines(self, fc: dict) -> list[str]:
        lines = wrap(fc["short"], max_lines=10) if fc["short"] else ["No forecast yet."]
        if fc["heard"]:
            lines.append(f"Heard {hm(fc['heard'])}, {ago(self.clock() - fc['heard'])}")
        if fc["state"] not in (None, "ok") and fc["message"]:
            lines += wrap(fc["message"], max_lines=6)
        return lines

    # sensors (software/sensors/sensord.py → current.json)
    def sensor_nodes(self, base: Path) -> set[str]:
        """Remote mesh sensors the admin allowed: settings saved in the launcher, else [sensors]."""
        saved = read_json_file(base / "settings.json", 200_000)
        nodes = saved.get("remote_nodes") if isinstance(saved, dict) and isinstance(saved.get("remote_nodes"), list) \
            else self.section("sensors").get("remote_nodes")
        if not isinstance(nodes, list):
            return set()
        hexd = set("0123456789abcdef")
        return {n.lower() for n in nodes[:32] if isinstance(n, str) and len(n) == 9 and n[0] == "!"
                and set(n[1:].lower()) <= hexd}

    def sensors(self) -> dict | None:
        if not self.feature_on("sensors"):
            return None
        base = self.features_dir / "sensors"
        cur = read_json_file(base / "current.json", 4_000_000)
        if not isinstance(cur, dict):
            return None
        now, up = self.clock(), finite(cur.get("updated"))
        poll = min(finite(cur.get("poll_s")) or 30.0, 3600.0)
        running = up is not None and -60 <= now - up < max(SENSORS_STOPPED_S, 4 * poll)
        allowed = self.sensor_nodes(base)
        remote_ids = {"mesh-" + n[1:] for n in allowed}
        lst = lambda k: cur.get(k)[:200] if isinstance(cur.get(k), list) else []
        names, readings = {}, []
        sources = [(s, s.get("values") if s.get("ok") is True else {}) for s in lst("sensors") if isinstance(s, dict)]
        sources += [(r, r.get("values")) for r in lst("remote") if isinstance(r, dict) and skey(r.get("node")) in allowed]
        for s, vals in sources:
            sid = clean(s.get("id"), 40)
            name = clean(s.get("name"), 20) or sid or "sensor"
            names[sid] = name
            if not isinstance(vals, dict):
                continue
            for metric, v in list(vals.items())[:40]:
                v = finite(v)
                if metric in SENSOR_METRICS and v is not None and abs(v) <= 1e8:
                    readings.append((metric, name, v))
        order = list(SENSOR_METRICS)
        readings = sorted(readings, key=lambda r: (order.index(r[0]), r[1]))[:60]
        alerts = []
        for a in lst("alerts") if running else []:     # a stopped service's alerts are not news
            if not isinstance(a, dict) or (a.get("remote") and skey(a.get("sensor")) not in remote_ids):
                continue
            metric, v, sid = skey(a.get("metric")), finite(a.get("value")), clean(a.get("sensor"), 40)
            alerts.append({"level": "danger" if a.get("level") == "danger" else "warning",
                           "metric": metric if metric in SENSOR_METRICS else None,
                           "value": v if v is not None and abs(v) <= 1e8 else None,
                           "name": names.get(sid, "") if sid else "",
                           "text": clean(a.get("text"), 300), "stale": a.get("stale") is True})
        alerts.sort(key=lambda a: (a["level"] != "danger", a["stale"]))
        return {"running": running, "updated": up, "alerts": alerts[:20], "readings": readings,
                "us": self.section("sensors").get("units") in ("us", "imperial")}

    def sensor_danger(self) -> bool:
        """A live danger-level sensor alert (the LED blinks fast for it, as for a weather alert)."""
        try:
            s = self.sensors()
            return bool(s and any(a["level"] == "danger" and not a["stale"] for a in s["alerts"]))
        except Exception:  # noqa: BLE001
            return False

    def alert_row(self, a: dict, us: bool) -> str:
        what = SENSOR_METRICS[a["metric"]][2] if a["metric"] else (a["name"] or "Sensor")
        val = fmt_reading(a["metric"], a["value"], us) if a["metric"] and a["value"] is not None else ""
        return f"{'!!' if a['level'] == 'danger' else '!'} {what} {val}".rstrip() + ("?" if a["stale"] else "")

    def sensors_screen(self):
        s = self.sensors()
        if s is None:
            return self.show_msg("No sensor data")
        rows = []
        if not s["running"]:
            last = f"Last update {hm(s['updated'])}." if s["updated"] else "No update yet."
            rows.append(("Sensors not running", self.page("Sensors", wrap("The sensor service has stopped "
                                                                          "writing. " + last))))
        elif not s["alerts"]:
            rows.append(("No alerts", None))
        for a in s["alerts"]:
            detail = wrap(a["text"] or "Sensor alert") + (wrap("(no new reading for 15+ minutes)") if a["stale"] else [])
            rows.append((self.alert_row(a, s["us"]), self.page("DANGER" if a["level"] == "danger" else "Alert", detail)))
        counts: dict = {}
        for metric, _, _ in s["readings"]:
            counts[metric] = counts.get(metric, 0) + 1
        for metric, name, v in s["readings"]:
            label, val = SENSOR_METRICS[metric][2], fmt_reading(metric, v, s["us"])
            row = f"{label} {val}" + (f" {name}" if counts[metric] > 1 else "")
            rows.append((row, self.page(name, [name, f"{label}: {val}"])))
        if not s["readings"]:
            rows.append(("No readings yet", None))
        self.show_list("Sensors", rows)

    # neighbourhood (the portal's public aid-station and missing-people reads)
    def poll_neighbourhood(self):
        for fid, p in self.polls.items():
            if self.feature_on(fid):
                p.kick(self.spawn)

    def aid_row(self, aid: dict) -> tuple[str, list[str]]:
        if not aid.get("set"):
            return "Aid station: no news", wrap("The aid station hasn't posted whether it is open yet.")
        items = [AID_ITEMS[i] for i in aid["items"]]
        if aid["open"]:
            row = "Aid: OPEN " + " ".join(short for short, _ in items)
        else:
            row = "Aid station: closed"
        if aid["stale"]:
            row = ("Aid: open?" if aid["open"] else "Aid: closed?") + " (old)"
        lines = [f"Aid station {'OPEN' if aid['open'] else 'closed'}"]
        if items:
            lines += wrap("Has: " + ", ".join(full for _, full in items))
        if aid["text"]:
            lines += wrap(aid["text"])
        if aid["updated"]:
            lines.append(f"Updated {ago(self.clock() - aid['updated'])}")
        if aid["stale"]:
            lines += wrap("This may be out of date.")
        return row[:60], lines

    def missing_row(self, m: dict) -> tuple[str, list[str]]:
        n = m["count"]
        row = "Missing: none" if n == 0 else "Missing: 1 person" if n == 1 else f"Missing: {n} people"
        lines = [f"• {name}"[:W] for name in m["names"]] or ["No one is listed", "as missing."]
        if n > len(m["names"]):
            lines.append(f"…and {n - len(m['names'])} more")
        return row, lines + wrap("Photos and details: Missing people in the app.")

    def neighbourhood_screen(self):
        self.poll_neighbourhood()
        rows = []
        aid, miss = self.polls["aidstation"].get(), self.polls["missing"].get()
        if aid is not None:
            r, lines = self.aid_row(aid)
            rows.append((r, self.page("Aid station", lines)))
        if miss is not None:
            r, lines = self.missing_row(miss)
            rows.append((r, self.page("Missing people", lines)))
        if not rows:
            return self.show_msg("No news from the\nneighbourhood yet")
        self.show_list("Neighbourhood", rows)

    def attention_pages(self) -> list[list[str]]:
        """Home-screen pages for things that need eyes: a paused weather radio, sensor alerts."""
        pages = []
        try:
            p = self.wx_pause(self.sdr_status())
            if p:
                pages.append(["! WX RADIO PAUSED", f"{p[0]} → {hm(p[1])}", "Weather alerts", "not heard till then"])
        except Exception:  # noqa: BLE001
            pass
        try:
            s = self.sensors()
            for a in [a for a in (s["alerts"] if s else []) if not a["stale"]][:3]:
                head = "!! SENSOR DANGER" if a["level"] == "danger" else "! SENSOR ALERT"
                pages.append([head, *(wrap(a["text"])[:3] or [self.alert_row(a, s["us"])])])
        except Exception:  # noqa: BLE001
            pass
        return pages

    def toggle(self, name, svc):
        self.run("service", name, "stop" if svc.get(name) else "start")

    def usb_mounts(self) -> list[Path]:
        return [p for p in sorted(Path("/media").glob("usb-*")) if os.path.ismount(p)]

    def import_usb(self):
        for m in self.usb_mounts():
            for cand in [m, *m.glob("*")]:
                if (cand / "manifest.json").exists():
                    return self.run("update-import", str(cand))
        self.show_msg("No update bundle\nfound on USB")

    def export_usb(self):
        m = self.usb_mounts()
        if not m:
            return self.show_msg("Plug in a USB\ndrive first")
        self.confirm(f"Export to\n{m[0].name}?", lambda: self.run("update-export", str(m[0] / "prepper-bundle")))

    def eject_usb(self):
        m = self.usb_mounts()
        if not m:
            return self.show_msg("No USB drive")
        self.run("usb-eject", m[0].name)

    def run(self, *args):
        self.show_msg("Working…")
        self.show_msg(ctl(*args)[:80])

    # — screens —
    def show_qr(self, data, caption):
        self.screen = "qr"; self.d.qr(data, caption)

    def show_msg(self, text, title=None):
        self.screen = "msg"; self.d.lines(text.split("\n")[:5 if title is None else 4], title=title)

    def show_list(self, title: str, rows: list):
        """A page of rows the knob scrolls through (the menu screen). A row's target is an action, a
        submenu (list), or None: pressing it goes back."""
        self.stack.append((title, [(clean(r, 60), t if t is not None else self.back) for r, t in rows][:120]))
        self.sel, self.screen = 0, "menu"
        self.draw()

    def page(self, title: str, lines: list[str]):
        """An action that opens a read-only page of text lines."""
        return lambda: self.show_list(title, [(ln, None) for ln in lines])

    def back(self):
        if self.stack:
            self.stack.pop()
        self.sel = 0
        if not self.stack:
            self.screen = "home"
        self.draw()

    def confirm(self, q, action):
        # Default to "No": a stray press must never shut the Badger down.
        self.screen, self.confirm_action, self.sel, self.confirm_q = "confirm", action, 0, q
        self.draw()

    def draw(self):
        with self.lock:
            if self.screen == "home":
                s = api("/api/status")
                p = s.get("power") or {}
                batt = f"{'⚡' if p.get('charging') else ''}{p.get('percent', '--')}%" if p else "mains"
                if p.get("input_w") is not None and p.get("input_v", 0) > 1:
                    batt += f"  sun {p['input_w']:.0f}W"
                pages = [
                    [self.ssid, f"{s.get('clients') or 0} connected", batt, self.url.replace("http://", "")],
                    ["Time", "  " + self.clock_text(s)[0], *self.clock_text(s)[1]],
                    ["Join Wi-Fi, open", self.url.replace("http://", ""), "", "Press knob: menu"],
                ]
                b = s.get("build") or {}
                if b.get("phase") and b.get("phase") != "done":
                    pages.insert(0, ["Building…", f"{b.get('phase')} {b.get('pct', 0)}%", (b.get("message") or "")[:21], ""])
                pages[0:0] = self.attention_pages()          # sensor alerts, paused weather radio
                for a in s.get("alerts") or []:
                    pages.insert(0, ["! WEATHER ALERT", (a.get("event") or "")[:21], (a.get("areas") or "")[:21], ""])
                self.d.lines(pages[self.home_page % len(pages)])
            elif self.screen == "menu":
                title, items = self.stack[-1]
                start = max(0, self.sel - 3)
                self.d.lines([i[0] for i in items][start:start + 4] + (["‹ Back"] if start + 4 > len(items) else []),
                             title=title, sel=self.sel - start)
            elif self.screen == "confirm":
                self.d.lines(["No", "Yes"], title=getattr(self, "confirm_q", "Sure?").replace("\n", " "), sel=self.sel)

    # — input —
    def wake(self) -> bool:
        self.last_input = time.monotonic()
        if self.asleep:
            self.asleep = False; self.d.sleep(False); self.draw()
            return True
        return False

    def turn(self, step: int):
        if self.wake():
            return
        with self.lock:
            if self.screen == "menu":
                n = len(self.stack[-1][1]) + 1          # + Back
                self.sel = (self.sel + step) % n
            elif self.screen == "confirm":
                self.sel = 1 - self.sel
            elif self.screen == "home":
                self.home_page += step
            self.draw()

    def press(self):
        if self.wake():
            return
        with self.lock:
            if self.screen == "home":
                self.stack, self.sel, self.screen = [("Menu", self.root())], 0, "menu"
            elif self.screen in ("qr", "msg"):
                self.screen = "menu" if self.stack else "home"
            elif self.screen == "confirm":
                act, self.confirm_action = self.confirm_action, None
                self.screen = "menu"
                if self.sel == 1 and act:
                    act(); return
            elif self.screen == "menu":
                title, items = self.stack[-1]
                if self.sel >= len(items):              # Back
                    self.stack.pop(); self.sel = 0
                    if not self.stack:
                        self.screen = "home"
                else:
                    label, target = items[self.sel]
                    if isinstance(target, list):
                        self.stack.append((label, target)); self.sel = 0
                    else:
                        target(); return
            self.draw()

    def hold(self):
        self.wake()
        if self.screen == "home":
            self.stack = [("Menu", [])]
            self.confirm("Shut down?", lambda: self.run("shutdown"))

    def tick(self):
        try:
            self.poll_neighbourhood()                # background reads, ≥ 60 s apart
        except Exception:  # noqa: BLE001
            pass
        if not self.asleep and time.monotonic() - self.last_input > self.sleep_after:
            if self.screen in ("msg", "qr", "confirm", "menu"):
                self.screen, self.stack = "home", []
            self.asleep = True; self.d.sleep(True)
            return
        if not self.asleep and self.screen == "home":
            self.home_page += 1
            self.draw()


def led_state(status: dict, sensor_danger: bool = False) -> str:
    """Front LED: solid = ready · slow blink = building/updating · medium = low battery ·
    fast = weather alert, or a danger-level sensor alert (flood, radiation…)."""
    if status.get("alerts") or sensor_danger:
        return "fast"
    p = status.get("power") or {}
    if p.get("percent") is not None and p.get("charging") is False and p["percent"] < 30:
        return "medium"
    b = status.get("build") or {}
    if b.get("phase") and b.get("phase") != "done":
        return "slow"
    return "solid"


def run_hardware(p: Panel, cfg: dict):
    from gpiozero import LED, Button, RotaryEncoder
    pc = cfg.get("panel", {})
    led = LED(int(pc.get("led_pin", 23)))
    led_now = {"v": None}

    def update_led():
        want = led_state(api("/api/status"), sensor_danger=p.sensor_danger())
        if want == led_now["v"]:
            return
        led_now["v"] = want
        if want == "solid":
            led.on()
        else:
            on, off = {"slow": (1.0, 1.0), "medium": (0.4, 0.4), "fast": (0.12, 0.12)}[want]
            led.blink(on_time=on, off_time=off)
    enc = RotaryEncoder(int(pc.get("encoder_a", 17)), int(pc.get("encoder_b", 27)), max_steps=0)
    btn = Button(int(pc.get("encoder_button", 22)), hold_time=3, bounce_time=0.05)
    held = {"v": False}
    enc.when_rotated_clockwise = lambda: p.turn(1)
    enc.when_rotated_counter_clockwise = lambda: p.turn(-1)
    btn.when_held = lambda: (held.update(v=True), p.hold())

    def released():
        if held["v"]:
            held["v"] = False
        else:
            p.press()
    btn.when_released = released
    while True:
        update_led()
        time.sleep(5)
        p.tick()


def run_console(p: Panel):
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            c = sys.stdin.read(1)
            if c == "q":
                break
            {"a": lambda: p.turn(-1), "d": lambda: p.turn(1), " ": p.press, "h": p.hold,
             "t": p.tick}.get(c, lambda: None)()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main() -> int:
    cfg_path = Path(os.environ.get("PREPPER_CONFIG", "/etc/prepper/prepper.toml"))
    cfg = tomllib.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    if "--make-key" in sys.argv:                     # install.sh: set up the panel key once, as root
        return 0 if ensure_panel_key() else 1
    if os.geteuid() == 0:
        ensure_panel_key()
    if "--console" in sys.argv:
        p = Panel(Console(), cfg); p.draw(); run_console(p); return 0
    if cfg.get("panel", {}).get("display", "sh1106") == "none":
        # Under systemd there is no terminal for the console UI; exit 0 and the
        # unit (Restart=on-failure) stays stopped.
        print("panel: display = \"none\"; nothing to drive", file=sys.stderr)
        return 0
    try:
        disp = OLED(cfg.get("panel", {}))
    except Exception as e:  # noqa: BLE001 — no display attached: exit quietly, no restart loop
        print(f"panel: no display ({e}); exiting", file=sys.stderr)
        return 0
    p = Panel(disp, cfg); p.draw(); run_hardware(p, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
