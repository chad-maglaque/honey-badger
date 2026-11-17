#!/usr/bin/env python3
"""
portal — the launcher API for the Honey Badger.

nginx serves the static launcher, /content/ and the renderers; this process
answers /api/*. With `standalone = true` it also serves static files and
/content/ itself (for development, or a laptop running the go-bag edition).

Standard library only (Python 3.11+). Every client — phone, iPad, laptop — is
a browser; nothing is installed on them.

  GET  /api/status            battery, clients, storage, temps, services, mode
  GET  /api/library           catalog ⨝ installed state ⨝ renderers
  GET  /api/files?item=&path= browse a document/book/video collection
  GET  /api/search?q=         federated: every ZIM (full text) + files + tools
  POST /api/ask               {"q", "brief"?} → SSE stream: sources, then answer tokens
                              (brief: two sentences, sized for a LoRa message)
  GET  /api/mesh/nodes        radios on the mesh that shared a position (for the map)
  GET  /api/updates           last update check + build/provisioning progress
  GET  /api/content/personal  personal-use (non-commercial) content: items, the owner's decision, sale readiness
  GET  /api/settings          device defaults (renderer per kind, etc.)
  GET  /api/board             community check-in board (+ public mesh channel); ?board=family needs a member key
  POST /api/board             {"name","text","mesh","board"?} post a message (family: members only, under their name)
  GET  /api/members/me        who this device belongs to (X-Member-Token)
  POST /api/members/join      {"code","name","device"} redeem a one-time invite → {"key"} (shown once)
  POST /api/members/leave     forget this device
  GET  /api/features          feature plugins (software/portal/features/*.py) for the launcher
  POST /api/local/invite      a family invite for the front panel's QR code — only from the Badger
                              itself (loopback, not through nginx); everyone else gets 403
  GET  /api/alerts            NOAA Weather Radio SAME alerts decoded by the SDR
  GET  /api/time              Ke-Shi / Nu:Time™ record (nu-time/v1) + time-sync quality
                              ?mode=utc|civil|solar|apparent &locale= &lat= &lon= &tz=
  POST /api/admin/login       {"pin"} → {"token"}
  POST /api/admin/logout      revoke the token sent in X-Admin-Token
  POST /api/admin/<action>    updates | uplink | service | power | settings | usb | system | personal
                              (personal: {"action": "accept", "agree": true, "items"?, "name"?} | "later" | "remove")

Every POST must be `Content-Type: application/json` and same-origin (see
Handler.post_allowed): a page on another origin can neither send one without
a CORS preflight, which this server never grants for POST, nor read the reply.
"""
from __future__ import annotations

import calendar
import errno
import fcntl
import hashlib
import hmac
import importlib.util
import types
import html
import json
import math
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "upd"))
import upd  # noqa: E402  (catalog, state and selection logic are shared)
sys.path.insert(0, str(HERE.parent / "nutime"))
import nutime  # noqa: E402  (Ke-Shi / Nu:Time conversion core)
sys.path.insert(0, str(HERE))
import members as members_mod  # noqa: E402  (family members: invites, device keys, radios)
sys.path.insert(0, str(HERE.parent / "common"))
import visitor  # noqa: E402  (what visitors may learn: rounded position, no power/occupancy)
import posture as posture_mod  # noqa: E402  (the security posture: Neighbourly / Guarded / Lockdown)
import sealed  # noqa: E402  (sealed records: encryption at rest, docs/SECURITY.md)
import sealed_api  # noqa: E402  (its owner-facing API: setup, unlock, lock)
import decoy as decoy_mod  # noqa: E402  (the made-up household a duress session sees)
sys.path.insert(0, str(HERE.parent / "sdr"))
try:
    import sdrcommon  # noqa: E402  (the radio scheduler's status: weather radio paused, SDR 2)
except Exception:  # noqa: BLE001 — the launcher runs without the SDR software
    sdrcommon = None

CATEGORIES = [
    ("reference", "Reference", "Encyclopedias, dictionaries, travel guides"),
    ("medical", "Medical", "First aid, field medicine, drugs, diagnosis"),
    ("survival", "Survival", "Field manuals, shelter, fire, navigation, security"),
    ("food-water", "Food & Water", "Water treatment, gardening, foraging, preserving, livestock"),
    ("energy-repair", "Power & Repair", "Solar, electrical, engines, tools, iFixit"),
    ("comms", "Comms", "Ham radio, repeaters, mesh, Morse, frequencies"),
    ("maps", "Maps", "Street, topo, trails, satellite, routing"),
    ("learn", "Learn", "Courses and textbooks for every age"),
    ("books", "Books", "Literature, classics, how-to"),
    ("video", "Video", "How-to and education videos"),
    ("ai", "Ask AI", "Offline assistant grounded in the library"),
    ("tools", "Tools", "Morse, phrasebook, water dosing, radio reference"),
    ("files", "USB & Files", "Your own files from a USB stick"),
]

TOOLS = [
    {"id": "morse", "title": "Morse code", "url": "/tools/morse.html",
     "keywords": "morse code cw sos signal light sound decode encode"},
    {"id": "phrases", "title": "Emergency phrasebook", "url": "/tools/phrases.html",
     "keywords": "phrase translate language spanish french german portuguese help doctor water"},
    {"id": "water", "title": "Water disinfection calculator", "url": "/tools/water.html",
     "keywords": "water bleach chlorine purify disinfect boil drink"},
    {"id": "radio", "title": "Radio frequency reference", "url": "/tools/radio.html",
     "keywords": "radio frequency frs gmrs murs cb noaa weather ham 2m 70cm marine aviation"},
    {"id": "convert", "title": "Unit converter", "url": "/tools/convert.html",
     "keywords": "convert units temperature weight length volume fahrenheit celsius"},
    {"id": "nutime", "title": "Ke-Shi / Nu:Time™ clock", "url": "/tools/nutime.html",
     "keywords": "nu time ke-shi keshi decimal clock gong beat tick geng fen ke sky sunrise"},
    {"id": "sun", "title": "Sun, moon & time", "url": "/tools/sun.html",
     "keywords": "sunrise sunset moon phase daylight time navigation"},
]

FILE_KINDS = {
    ".pdf": "pdf", ".epub": "epub", ".mp4": "video", ".webm": "video", ".mkv": "video",
    ".m4v": "video", ".mp3": "audio", ".ogg": "audio", ".opus": "audio", ".m4a": "audio",
    ".flac": "audio", ".wav": "audio", ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".webp": "image", ".txt": "text", ".html": "html", ".htm": "html", ".zim": "zim",
    ".pmtiles": "pmtiles", ".gguf": "model",
}

ASK_SYSTEM = (
    "You are the offline assistant on a survival reference device. Answer ONLY from the "
    "numbered sources provided. Cite them like [1]. If the sources do not contain the "
    "answer, say so plainly and suggest what to look up. Be concise and practical. For "
    "medical emergencies, give the immediate first-aid steps from the sources and say to "
    "get trained help if at all possible. Never invent dosages."
)
ASK_BRIEF = (
    " This answer goes out over a low-bandwidth radio: reply in at most two short sentences, "
    "under 170 characters in total, plain text, no lists, no source numbers."
)


# ─────────────────────────────── config ──────────────────────────────────────

def valid_tz(name) -> bool:
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError, ValueError, OSError for odd keys
        return False


def system_tz() -> str:
    """The machine's IANA zone (from /etc/timezone or the /etc/localtime link), else UTC."""
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


class PortalConfig:
    def __init__(self, path: Path):
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
        self.raw = raw                                   # whole config, for feature plugins' own sections
        self.path = path                                 # (to re-read admin_pin after an upgrade-pin)
        p = raw.get("portal", {})
        self.upd = upd.Config.load(path)
        rel = lambda v: (path.parent / v).resolve() if v and not Path(v).is_absolute() else Path(v)
        self.listen = p.get("listen", "127.0.0.1:8000")
        self.static = rel(p.get("static", str(HERE / "static")))
        self.renderers_file = rel(p.get("renderers", str(HERE.parents[1] / "content/renderers.toml")))
        # The portal's own writable files live in /var/lib/prepper/portal/ (owned by its user).
        self.settings_file = rel(p.get("settings_file", "/var/lib/prepper/portal/portal-settings.json"))
        self.features_data = rel(p.get("features_data", "/var/lib/prepper/portal/features"))   # one folder per feature
        self.admin_pin = p.get("admin_pin", "")          # pbkdf2$salt$hash
        # The optional duress PIN (prepperctl set-duress): a normal-looking admin login that silently
        # starts a panic wipe. docs/SECURITY.md → "Duress PIN".
        self.duress_pin = p.get("duress_pin", "") if isinstance(p.get("duress_pin", ""), str) else ""
        # The dead-man switch's counter (root-written; it exists only while the switch is armed) and the
        # launcher's check-in counter next to the boards.
        self.deadman_state = rel(p.get("deadman_state", "/var/lib/prepper/deadman-state.json"))
        self.wipe_pending = rel(p.get("wipe_pending", "/var/lib/prepper/wipe-pending.json"))
        self.wipe_receipt = rel(p.get("wipe_receipt", "/run/prepper/wipe-receipt.json"))
        self.kiwix = p.get("kiwix", "http://127.0.0.1:8081/kiwix")
        self.kiwix_public = p.get("kiwix_public", "/kiwix")
        # llama-server runs with --api-prefix /ai, which prefixes every route
        # (/ai/health, /ai/v1/chat/completions), so the prefix belongs in the base URL.
        self.llama = p.get("llama", "http://127.0.0.1:8090/ai").rstrip("/")
        self.standalone = bool(p.get("standalone", False))
        self.edition = p.get("edition", "disk")
        self.ask_books = list(p.get("ask_books", []))
        self.ask_timeout = float(p.get("ask_timeout", 300))     # seconds without a token from llama-server
        # requests served at once (each holds a thread); more wait in the accept queue, then get a 503
        try:
            self.max_threads = min(1024, max(16, int(p.get("max_threads", 128))))
        except (TypeError, ValueError):
            self.max_threads = 128
        self.ask_concurrency = max(1, int(p.get("ask_concurrency", 2)))
        # Extra host[:port] names a browser may POST from (Origin), beyond the Host it asked for.
        self.allowed_origins = {str(h).lower() for h in p.get("allowed_origins", [])}
        self.search_langs = list(p.get("search_langs", ["eng"]))
        self.usb_root = Path(p.get("usb_root", "/media"))
        self.power_file = Path(p.get("power_file", "/run/prepper/power.json"))
        self.build_file = rel(p.get("build_file", "/var/lib/prepper/build.json"))
        self.board_file = rel(p.get("board_file", "/var/lib/prepper/portal/board.jsonl"))
        # Visitors' "send over mesh" requests wait here for a family member or the admin (red-team finding 10):
        # "approve" (default) or "off" (visitors' posts stay on the board).
        self.mesh_pending = rel(p.get("mesh_pending") or str(self.board_file.parent / "mesh-pending.json"))
        self.visitor_mesh = p.get("visitor_mesh", "approve") if p.get("visitor_mesh") in ("approve", "off") else "approve"
        self.mesh_outbox = rel(p.get("mesh_outbox", "/var/lib/prepper/portal/mesh-outbox.jsonl"))
        # What the mesh bridge learns from the radio lives in RAM only (/run/prepper, tmpfs): positions, Badgers
        # heard, telemetry, the channel links and the Bluetooth PIN — re-read from the radio after a restart.
        self.run_dir = rel(p.get("run_dir", "/run/prepper"))
        self.mesh_nodes = rel(p.get("mesh_nodes") or str(self.run_dir / "mesh-nodes.json"))
        # Badgers heard on the mesh + what the mesh courtesy is doing (written by meshbridge, docs/MESH.md)
        self.mesh_peers = rel(os.environ.get("MESH_PEERS") or p.get("mesh_peers") or str(self.run_dir / "mesh-peers.json"))
        # The security posture (software/common/posture.py): the contract other programs read
        self.posture_file = rel(p.get("posture_file") or str(self.mesh_outbox.with_name("posture.json")))
        # The trusted circle (features/circles.py ↔ meshbridge): requests to the radio, the circle link (RAM, 0600),
        # what the bridge answered/refused (RAM), our own questions to the circle (RAM)
        self.mesh_radio_request = rel(p.get("mesh_radio_request") or str(self.mesh_outbox.with_name("mesh-radio-req.json")))
        self.mesh_radio_result = rel(p.get("mesh_radio_result") or str(self.mesh_outbox.with_name("mesh-radio-res.json")))
        self.mesh_circle_link = rel(p.get("mesh_circle_link") or str(self.run_dir / "mesh-circle.json"))
        self.circle_status_file = rel(p.get("circle_status_file") or str(self.run_dir / "circle-status.json"))
        self.circle_ask_file = rel(p.get("circle_ask_file") or str(self.run_dir / "circle-ask.json"))
        self.members_file = rel(p.get("members_file", "/var/lib/prepper/portal/members.json"))
        self.mesh_rotate_request = rel(p.get("mesh_rotate_request", "/var/lib/prepper/portal/mesh-rotate.json"))
        self.mesh_rotate_result = rel(p.get("mesh_rotate_result", "/var/lib/prepper/portal/mesh-rotate-result.json"))
        self.mesh_channel_link = rel(p.get("mesh_channel_link") or str(self.run_dir / "mesh-channel.json"))
        # "!pair CODE" messages heard on the family channel (written by meshbridge): radio pairing proof
        self.mesh_pair_file = rel(p.get("mesh_pair_file", "/var/lib/prepper/portal/mesh-pair.json"))
        # the front panel's lock (root-only; the portal only checks whether it exists)
        self.panel_lock_file = rel(p.get("panel_lock_file", "/etc/prepper/panel-lock.json"))
        # the admin page "arms" the panel for 10 minutes: only then can a code be set at the knob
        self.panel_lock_arm = rel(p.get("panel_lock_arm", "/var/lib/prepper/portal/panel-lock-arm.json"))
        self.family_board_file = rel(p.get("family_board_file", "/var/lib/prepper/portal/family-board.jsonl"))
        # Family radio messages the mesh bridge heard, waiting in RAM (0600) for this launcher to seal them onto
        # the family board with its current key (so members can read them while the Badger is locked)
        self.family_inbox = rel(p.get("family_inbox") or str(self.run_dir / "family-inbox.jsonl"))
        # One JSON line per telemetry packet heard on the mesh (written by meshbridge, read by features).
        self.mesh_telemetry = rel(os.environ.get("MESH_TELEMETRY")
                                  or p.get("mesh_telemetry") or str(self.run_dir / "mesh-telemetry.jsonl"))
        # The front panel proves it is the panel with a key only root can read (/etc/prepper/panel.key,
        # made by the panel at start); the portal holds just its SHA-256 (root:prepper 0640). See
        # Handler.local_console and docs/SECURITY.md.
        self.mesh_bt_pin_file = rel(p.get("mesh_bt_pin_file") or str(self.run_dir / "mesh-bt-pin.json"))
        self.panel_key_hash = rel(p.get("panel_key_hash", "/etc/prepper/panel.key.sha256"))
        self.alerts_file = Path(p.get("alerts_file", "/run/prepper/alerts.json"))
        t = raw.get("time", {})
        self.time_mode = t.get("reference", "civil")
        self.time_display = t.get("display", "local")
        self.time_locale = t.get("locale", "en")
        self.time_dst = t.get("dst_policy", "wall")
        tz = t.get("timezone") or raw.get("device", {}).get("timezone") or system_tz()
        self.time_tz_note = None
        if not valid_tz(tz):
            # A typo here used to break /api/status for everyone; run on UTC and say why.
            self.time_tz_note = f"time zone {tz!r} is not a known IANA zone; using UTC"
            print(f"[portal] {self.time_tz_note}", file=sys.stderr)
            tz = "UTC"
        self.time_tz = tz
        self.time_lat = t.get("latitude")
        self.time_lon = t.get("longitude")
        # The systemd unit loads /etc/default/prepper, which sets AP_IFACE.
        self.ap_iface = p.get("ap_iface", os.environ.get("AP_IFACE") or "wlan0")
        self.ssid = raw.get("network", {}).get("ssid", "Home-Hotspot")
        self.hostname = raw.get("network", {}).get("hostname", "badger")
        self.ctl = p.get("ctl", "sudo -n /opt/prepper/bin/prepperctl")
        # name → probe URL; a renderer whose `requires` probe fails is hidden
        self.services = p.get("services", {
            "kiwix": self.kiwix + "/catalog/v2/root.xml",
            "llama": self.llama + "/health",
            "kolibri": "http://127.0.0.1:8080/",
            "kavita": "http://127.0.0.1:5000/",
            "jellyfin": "http://127.0.0.1:8096/health",
            "mesh": "file:///run/prepper/mesh.ok",
            "alerts": "file:///run/prepper/alertd.ok",
            "sdr": "file:///run/prepper/sdr/status.json",
            "sensors": "file:///var/lib/prepper/portal/features/sensors/current.json",
        })


CFG: PortalConfig
TOKENS: dict[str, float] = {}
MIN_PIN = 6
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


_inflight: dict[str, threading.Event] = {}


def cached(key: str, ttl: float, fn):
    """fn()'s value, kept ttl seconds. Single-flight: when it runs out, one thread recomputes it while
    the others keep using the old value (or, the very first time, wait for that one result) — before,
    every request arriving in that moment ran the same slow probe at once."""
    for _ in range(3):
        with _lock:
            hit = _cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            ev = _inflight.get(key)
            mine = ev is None
            if mine:
                ev = _inflight[key] = threading.Event()
            elif hit:
                return hit[1]                          # stale, but someone is already refreshing it
        if mine:
            try:
                val = fn()
                with _lock:
                    _cache[key] = (time.monotonic(), val)
                return val
            finally:
                with _lock:
                    _inflight.pop(key, None)
                ev.set()
        ev.wait(30)
        with _lock:
            hit = _cache.get(key)
        if hit:
            return hit[1]
    return fn()


def cached_background(key: str, ttl: float, fn, wait: float = 0.3, default=None):
    """Like cached(), but a request never waits for a slow refresh: a stale value is returned at once
    while one background thread refreshes it; with nothing cached yet, the request that started the
    refresh waits at most `wait` s for it, and the others get `default` straight away."""
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        ev = _inflight.get(key)
        if ev is not None and not hit:
            return default
        if ev is None:
            ev = _inflight[key] = threading.Event()

            def refresh():
                try:
                    val = fn()
                    with _lock:
                        _cache[key] = (time.monotonic(), val)
                except Exception as e:  # noqa: BLE001 — a probe must never take the portal down
                    print(f"[portal] {key} refresh failed: {type(e).__name__}", file=sys.stderr)
                finally:
                    with _lock:
                        _inflight.pop(key, None)
                    ev.set()
            threading.Thread(target=refresh, daemon=True, name=f"refresh-{key}").start()
    if hit:
        return hit[1]
    ev.wait(wait)
    with _lock:
        hit = _cache.get(key)
    return hit[1] if hit else default


# ─────────────────────────────── helpers ─────────────────────────────────────

# The admin secret: a PIN or (better) a passphrase, stored as scrypt (memory-hard: a copy of
# prepper.toml is slow to attack even on a GPU). Older installs have "pbkdf2$salt$hash": still
# accepted, and replaced by an scrypt hash of the same secret at the next successful login.
PIN_KDF = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSPHRASE = 10
MAX_PIN = 128


def hash_pin(pin: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    n, r, p = PIN_KDF["n"], PIN_KDF["r"], PIN_KDF["p"]
    digest = hashlib.scrypt(pin.encode("utf-8", "surrogatepass"), salt=salt.encode(), n=n, r=r, p=p,
                            dklen=32, maxmem=256 * 1024 * 1024).hex()
    return f"scrypt${n}${r}${p}${salt}${digest}"


def legacy_hash_pin(pin: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8", "surrogatepass"), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_pin(pin: str, stored) -> bool:
    """Does `pin` match a stored hash (scrypt, or the older PBKDF2)? Odd or absurd parameters: no."""
    if not isinstance(pin, str) or not isinstance(stored, str) or not pin:
        return False
    parts = stored.split("$")
    try:
        if parts[0] == "scrypt" and len(parts) == 6:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            if not (2 ** 10 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 4) \
                    or 128 * n * r > 256 * 1024 * 1024:
                return False
            got = hashlib.scrypt(pin.encode("utf-8", "surrogatepass"), salt=parts[4].encode(), n=n, r=r, p=p,
                                 dklen=32, maxmem=256 * 1024 * 1024 + 1024 * 1024).hex()
            return hmac.compare_digest(got, parts[5])
        if parts[0] == "pbkdf2" and len(parts) == 3:
            return hmac.compare_digest(legacy_hash_pin(pin, parts[1]), stored)
    except (ValueError, MemoryError):
        return False
    return False


def pin_is_legacy(stored) -> bool:
    return isinstance(stored, str) and stored.startswith("pbkdf2$")


def pin_policy(pin) -> tuple[str | None, str | None]:
    """(error, warning) for a NEW admin secret: a passphrase of 10+ characters, or 6+ digits (allowed,
    with a warning: digits alone fall quickly to someone who copies the Badger's disk)."""
    if not isinstance(pin, str) or not pin:
        return "Enter a passphrase.", None
    if len(pin) > MAX_PIN:
        return f"At most {MAX_PIN} characters.", None
    if any(ord(c) < 32 or ord(c) == 127 for c in pin) or pin != pin.strip():
        return "No control characters, and no spaces at the start or end.", None
    if pin.isdigit() and pin.isascii():
        if len(pin) < MIN_PIN:
            return f"A PIN of digits needs at least {MIN_PIN} of them (a passphrase of {MIN_PASSPHRASE}+ characters is better).", None
        return None, (f"A PIN of only digits is quick to guess for anyone who copies the Badger's disk. A passphrase "
                      f"of {MIN_PASSPHRASE} or more characters (a few words) is much stronger.")
    if len(pin) < MIN_PASSPHRASE:
        return (f"Use at least {MIN_PASSPHRASE} characters (a few words make a good passphrase), "
                f"or a PIN of {MIN_PIN}+ digits."), None
    return None, None


_pin_lock = threading.Lock()          # one PIN check at a time, whatever the thread count
_auth_lock = threading.Lock()
_fail_ip: dict[str, list[float]] = {}  # ip → [failures, locked_until, last_failure]
_fail_recent: list[float] = []         # times of recent misses from anywhere (sliding window)
_trusted: dict[str, float] = {}        # ip → last successful login: exempt from the global brake
GLOBAL_WINDOW, GLOBAL_FREE, GLOBAL_CAP = 900.0, 20, 60.0
TRUST_FOR = 30 * 86400
_now = time.monotonic                  # lockouts are in-memory: immune to wall-clock jumps (tests swap it)


def reset_login_limits() -> None:
    with _auth_lock:
        _fail_ip.clear()
        _fail_recent.clear()
        _trusted.clear()


def _pin_matches(pin: str) -> bool:
    return verify_pin(pin, CFG.admin_pin)


_upgrading = threading.Lock()


def upgrade_pin_hash(pin: str) -> bool:
    """After a good login with an older PBKDF2 hash: have prepperctl (root) put an scrypt hash of the same
    secret into prepper.toml (it checks the PIN against the current hash itself, so this can never set a
    different PIN), then use it here too. Best effort: on any failure the old hash simply stays."""
    if not pin_is_legacy(CFG.admin_pin) or not _upgrading.acquire(blocking=False):
        return False
    try:
        rc, out = ctl("upgrade-pin", input=pin + "\n")
        if rc != 0:
            return False
        try:
            new = tomllib.loads(Path(CFG.path).read_text()).get("portal", {}).get("admin_pin", "")
        except (OSError, ValueError, AttributeError):
            new = ""
        if isinstance(new, str) and new.startswith("scrypt$") and verify_pin(pin, new):
            CFG.admin_pin = new
            return True
        return False
    finally:
        _upgrading.release()


def check_pin(pin: str) -> bool:
    with _pin_lock:
        return _pin_matches(pin)


# ── duress, the made-up household, panic wipe (docs/SECURITY.md → "Duress", "Panic wipe") ──
# A duress secret (the duress PIN, the duress passphrase, the panel duress code) gets exactly the real secret's
# reply. Behind it the duress action runs ([security] duress_action / the admin page): "lock" (default: the
# sealed records and the vault get a hard lock — only the recovery keys reopen them; nothing destroyed, no
# restart, no Wi-Fi or posture change) or "wipe" (the panic wipe, noticeable steps delayed). Either way, from
# the next request on EVERY session — admin, family phones, the panel — sees the made-up household
# (software/common/decoy.py): the launcher reads a working copy in its private /tmp instead of the real files.
# It lasts until the owner types the real recovery key (lock), or until the wipe's restart (wipe: by then the
# wipe has put the same made-up household in place of the family's data). Why everyone and not only the coerced
# session: an attacker holding a family phone would otherwise see the real family on it.
DURESS_TOKENS: set[str] = set()       # admin sessions opened with a duress secret (memory only)
DECOY: dict = {}                      # set while the made-up household is shown (see enter_decoy)
REAL: dict = {}                       # the real paths and sealer, kept while the decoy is shown
DECOY_FIELDS = ("members_file", "board_file", "family_board_file", "family_inbox", "mesh_outbox", "mesh_nodes",
                "mesh_peers", "mesh_pending", "mesh_telemetry", "mesh_channel_link", "mesh_bt_pin_file", "mesh_pair_file",
                "mesh_rotate_request", "mesh_rotate_result", "settings_file", "mesh_circle_link", "circle_status_file",
                "circle_ask_file", "mesh_radio_request", "mesh_radio_result", "posture_file")
# Feature folders with nothing about the family in them: a duress session reads the real ones (weather, passes,
# sensor readings, voice settings) so the Badger looks as it always does.
DECOY_PASSTHROUGH = ("forecast", "satellites", "sensors", "voice", "aircraft")
_dummy_hash: list[str] = []
_decoy_lock = threading.Lock()
_decoy_profile_ok: list[bool] = []
_checkin = {"at": -1e18}


def _dummy_pin_hash() -> str:
    if not _dummy_hash:
        _dummy_hash.append(hash_pin(secrets.token_urlsafe(18)))
    return _dummy_hash[0]


def _duress_matches(pin: str) -> bool:
    """Always one full scrypt — against the duress hash, or a dummy of the same cost when none is set — so
    a login takes as long whichever secret was typed and doesn't tell whether a duress PIN exists."""
    stored = CFG.duress_pin if isinstance(getattr(CFG, "duress_pin", ""), str) else ""
    got = verify_pin(pin, stored or _dummy_pin_hash())
    return bool(stored) and got


def real_path(field: str) -> Path:
    """A [portal] path as it really is (not the decoy's working copy)."""
    v = REAL.get(field)
    return v if isinstance(v, Path) else getattr(CFG, field)


def decoy_profile_file() -> Path:
    p = (CFG.raw.get("portal") or {}).get("decoy_file") if isinstance(CFG.raw.get("portal"), dict) else None
    if isinstance(p, str) and p and Path(p).is_absolute():
        return Path(p)
    return real_path("members_file").parent / "decoy.json"


def decoy_profile() -> dict:
    """The device's made-up household (made on first use; harmless by design, so a plain file)."""
    return decoy_mod.ensure(decoy_profile_file())


def duress_action() -> str:
    return decoy_mod.duress_action(CFG.raw, real_path("settings_file"))


def real_sealer() -> sealed.Sealer:
    return DECOY.get("real_sealer") or sealed_store()


def decoy_active() -> bool:
    return bool(DECOY)


def _decoy_paths(d: Path) -> dict:
    return {"members": d / CFG.members_file.name, "family_board": d / CFG.family_board_file.name,
            "board": d / CFG.board_file.name, "posture": d / CFG.posture_file.name,
            "supplies": d / "features" / "supplies" / "supplies.json"}


def enter_decoy(held: bool = False, anchor: float | None = None, mode: str = "lock") -> sealed.Sealer:
    """Show the made-up household to everyone from now on. The decoy's sealer exists at once (its status goes
    into the reply that triggered this); the working copy is written in the background and requests wait for
    it (decoy_wait) — so the reply takes as long as a real one. held: the decoy shows as locked (as the real
    records were), until a passphrase is typed in it. Returns the decoy's sealer."""
    with _decoy_lock:
        if DECOY:
            return DECOY["sealer"]
        real_s = sealed_store()
        d = Path(tempfile.mkdtemp(prefix="portal-"))
        o = sealed_api.options(CFG)
        o.pop("folder", None)
        o["posture_file"] = d / CFG.posture_file.name
        o["idle_minutes"] = 0
        ds = sealed.DecoySealer(d / "sealed", real=real_s, held=held, **o)
        ds.register_migrator("members", _reseal_members)
        ds.register_migrator("family-board", lambda: seal_family_board(reseal=True))
        ready = threading.Event()
        DECOY.update(dir=str(d), since=time.time(), sealer=ds, real_sealer=real_s, ready=ready, mode=mode)
        for f in DECOY_FIELDS + ("features_data",):
            cur = getattr(CFG, f, None)
            if isinstance(cur, Path):
                REAL[f] = cur
        # (read now: a duress wipe running alongside removes the real file within seconds)
        skel = decoy_mod.member_skeleton(REAL.get("members_file", CFG.members_file))
    threading.Thread(target=_prepare_decoy, args=(d, ds, held, anchor, skel), daemon=True, name="decoy").start()
    return ds


def _prepare_decoy(d: Path, ds, held: bool, anchor, skel: list) -> None:
    try:
        prof = decoy_profile()
        decoy_mod.materialize(prof, _decoy_paths(d), skel, anchor, community_src=REAL.get("board_file"))
        st = read_json(REAL["settings_file"], {}) if "settings_file" in REAL else {}
        if isinstance(st, dict) and st:              # the Badger's ordinary settings look the same (not the choice)
            st.pop("duress_action", None)
            private_write(d / REAL["settings_file"].name, json.dumps(st, indent=2))
    except Exception as e:  # noqa: BLE001 — an empty-looking decoy beats none at all
        print(f"[portal] decoy not fully prepared: {type(e).__name__}", file=sys.stderr)
    try:
        for f in DECOY_FIELDS:
            cur = REAL.get(f)
            if isinstance(cur, Path):
                setattr(CFG, f, d / cur.name)
        CFG.features_data = d / "features"
        CFG.features_data.mkdir(exist_ok=True)
        CFG.decoy_active = True
        sealed.set_default(ds)
        global _members
        _members = None
        with _lock:
            _cache.clear()
        if held:                                     # sealed like the real ones, so "locked" looks the same
            with ds.unheld():
                for name in ("members", "family-board"):
                    try:
                        ds.migrators[name]()
                    except Exception:  # noqa: BLE001
                        pass
                f = _feature_handle("supplies")
                if f is not None and hasattr(f.mod, "_reseal"):
                    try:
                        f.mod._reseal(f.ctx)
                    except Exception:  # noqa: BLE001
                        pass
            ds.lock_all("locked")
            ds.locked_reason = None                  # "Locked since the Badger started", as after a restart
    finally:
        DECOY["ready"].set()


def decoy_wait(timeout: float = 30.0) -> None:
    ev = DECOY.get("ready")
    if ev is not None and not ev.is_set():
        ev.wait(timeout)


def decoy_tick() -> None:
    """Every request: the made-up household ready; a duress hard lock set by another program (the front panel's
    duress code goes through the root wipe engine) or still in place after a restart → the decoy, now."""
    if not _decoy_profile_ok:
        _decoy_profile_ok.append(True)
        try:
            decoy_profile()
        except Exception as e:  # noqa: BLE001
            print(f"[portal] decoy profile not made: {type(e).__name__}", file=sys.stderr)
    if DECOY:
        decoy_wait()
        return
    s = sealed_store()
    if s.hard_locked():
        was_open = s._priv is not None
        s.lock_all("locked")
        hard_lock_vault()
        enter_decoy(held=not was_open, anchor=s.hard_locked_at())
        decoy_wait()


def hard_lock_vault() -> None:
    f = _feature_handle("vault")
    if f is None or not hasattr(f.mod, "hard_lock_dir"):
        return
    try:
        f.mod.hard_lock_dir(real_path("features_data") / "vault")
    except Exception as e:  # noqa: BLE001
        print(f"[portal] vault not hard-locked: {type(e).__name__}", file=sys.stderr)


def duress_triggered(held: bool = False) -> sealed.Sealer:
    """A duress secret was used: the duress action, and the made-up household for everyone from now on."""
    if DECOY:
        return DECOY["sealer"]
    if duress_action() == "wipe":
        return duress_wipe(held)
    s = sealed_store()
    try:
        if not s.hard_lock():                        # not set up: nothing a recovery key could reopen
            s.lock_all("locked")
    except Exception as e:  # noqa: BLE001 — the decoy must show whatever happens
        print(f"[portal] sealed records not hard-locked: {type(e).__name__}", file=sys.stderr)
    hard_lock_vault()
    return enter_decoy(held=held, anchor=time.time(), mode="lock")


def exit_decoy(keep: str = "") -> None:
    """The real recovery key was typed: real paths back, the decoy's copy gone, duress sessions ended (but `keep`,
    the session that typed the recovery key), and a note on the (real) family board."""
    with _decoy_lock:
        if not DECOY:
            return
        decoy_wait()
        for f, v in REAL.items():
            if f != "real_sealer" and isinstance(v, Path):
                setattr(CFG, f, v)
        real_s = DECOY.get("real_sealer")
        d = DECOY.get("dir")
        DECOY.clear()
        REAL.clear()
        CFG.decoy_active = False
        if real_s is not None:
            sealed.set_default(real_s)
        global _members
        _members = None
        with _lock:
            _cache.clear()
        with _auth_lock:
            for t in DURESS_TOKENS:
                if t != keep:
                    TOKENS.pop(t, None)
            DURESS_TOKENS.clear()
    if d:
        shutil.rmtree(d, ignore_errors=True)
    family_notice("Recovered after a duress lock.", "recovered")


def wipe_already_running() -> bool:
    try:
        return CFG.wipe_pending.exists() or CFG.wipe_receipt.exists()
    except OSError:
        return False


def start_wipe(verb: str = "panic-wipe", trigger: str = "admin", extra: tuple = ()) -> tuple[int, str]:
    """prepperctl's two steps: a one-time token, then the confirmed run (it detaches into its own unit)."""
    rc, out = ctl(verb)
    m = re.search(r"confirm-token: ([A-Z0-9]{4}-[A-Z0-9]{4})", out or "")
    if rc != 0 or not m:
        return rc or 1, out
    return ctl(verb, "--confirm", m.group(1), "--trigger", trigger, *extra)


def erase_sealed_in_memory() -> None:
    """Crypto-erase in THIS process first: the launcher holds the sealed-records keys in memory (and the
    wipe's own sealed.destroy_keys(root) can only scrub the files). Zeroise + scrub-delete right away."""
    try:
        sealed.destroy_keys()
    except Exception as e:  # noqa: BLE001 — the wipe itself destroys the key files anyway
        print(f"[portal] sealed keys not erased in memory: {type(e).__name__}", file=sys.stderr)


def duress_wipe(held: bool = False) -> sealed.Sealer:
    """duress_action = "wipe": the made-up household now (before the reply), the wipe in the background."""
    first = not DECOY                    # once per launcher run: the first duress use starts it
    if first:
        erase_sealed_in_memory()
    ds = enter_decoy(held=held, mode="wipe")
    if not first or wipe_already_running():
        return ds

    def run():
        try:
            rc, out = start_wipe("panic-wipe", "duress")
            if rc != 0:
                print(f"[portal] wipe not started (exit {rc})", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[portal] wipe not started: {type(e).__name__}", file=sys.stderr)
    threading.Thread(target=run, daemon=True).start()
    return ds


def note_checkin() -> None:
    """The dead-man switch wipes after N days of running without a check-in. Only while it is armed (its
    state file exists), at most every 10 minutes, and only a counter — no times, no names."""
    if DECOY:
        return
    now = time.monotonic()
    if now - _checkin["at"] < 600:
        return
    try:
        if not CFG.deadman_state.exists():
            return
    except OSError:
        return
    _checkin["at"] = now
    path = CFG.board_file.parent / "checkin.json"
    try:
        cur = read_json(path, {}) or {}
        seq = cur.get("seq") if isinstance(cur, dict) and isinstance(cur.get("seq"), int) else 0
        private_write(path, json.dumps({"seq": (seq + 1) % 1_000_000_000}))
    except OSError as e:
        print(f"[portal] dead-man check-in not saved: {e.strerror or e}", file=sys.stderr)


def attempt_login(ip: str, pin: str, local: bool = False) -> tuple[str, float]:
    """("ok"|"duress"|"bad"|"locked", seconds to wait). The lockout check, the PIN check
    and the failure count happen as one step: checked separately, a burst of
    parallel requests all passed the lockout check before the first failure
    was counted, and got far more than 3 guesses in. Both secrets are always
    checked (same time for admin PIN, duress PIN and a wrong one)."""
    with _pin_lock:
        wait = login_wait(ip, local)
        if wait > 0:
            return "locked", wait
        admin = _pin_matches(pin)
        duress = _duress_matches(pin)
        if admin or duress:
            login_ok(ip)
            return ("ok" if admin else "duress"), 0.0
        login_failed(ip)
        return "bad", 0.0


def _global_wait(now: float) -> float:
    while _fail_recent and now - _fail_recent[0] > GLOBAL_WINDOW:
        _fail_recent.pop(0)
    n = len(_fail_recent)
    if n < GLOBAL_FREE:
        return 0.0
    return max(0.0, _fail_recent[-1] + min(GLOBAL_CAP, 2 ** (n - GLOBAL_FREE)) - now)


def login_wait(ip: str, local: bool = False) -> float:
    """Seconds this client must wait before another PIN attempt (0 = go ahead).

    Per client: exponential after 3 misses (1 s, 2 s … 15 min). For everyone:
    a brake once 20 misses landed in the last 15 minutes from anywhere (up to
    60 s), which decays as the window slides — a slow trickle of guesses can't
    keep it on for ever. A client that logged in successfully in the last 30
    days, and the local console (loopback without X-Real-IP), skip the global
    brake, so a flood of guesses from other addresses can't lock the owner out."""
    now = _now()
    with _auth_lock:
        rec = _fail_ip.get(ip)
        own = (rec[1] if rec else 0) - now
        exempt = local or now - _trusted.get(ip, -1e18) < TRUST_FOR
        return max(0.0, own, 0.0 if exempt else _global_wait(now))


def login_failed(ip: str) -> None:
    now = _now()
    with _auth_lock:
        rec = _fail_ip.setdefault(ip, [0.0, 0.0, 0.0])
        if now - rec[2] > 3600:                       # a client's count forgets after an hour
            rec[0] = 0
        rec[0] += 1
        rec[2] = now
        if rec[0] >= 3:
            rec[1] = now + min(900, 2 ** (rec[0] - 3))
        _fail_recent.append(now)
        del _fail_recent[:-1000]
        if len(_fail_ip) > 1000:
            # Forget the quietest clients that are not locked out: one-off misses sprayed from
            # many addresses must not wipe the lockout of the address that keeps guessing.
            free = sorted((k for k, r in _fail_ip.items() if r[1] <= now), key=lambda k: _fail_ip[k][2])
            for k in free[:max(len(free) // 2, len(_fail_ip) - 1000)]:
                _fail_ip.pop(k, None)
            if len(_fail_ip) > 4000:
                for k in sorted(_fail_ip, key=lambda k: _fail_ip[k][1])[:len(_fail_ip) - 4000]:
                    _fail_ip.pop(k, None)


def login_ok(ip: str) -> None:
    with _auth_lock:
        _fail_ip.pop(ip, None)
        _trusted[ip] = _now()
        if len(_trusted) > 200:
            for k in sorted(_trusted, key=_trusted.get)[:100]:
                _trusted.pop(k, None)


def revoke_token(tok: str) -> bool:
    with _auth_lock:
        return TOKENS.pop(tok, None) is not None


def revoke_all_tokens() -> int:
    """Every admin session ends (e.g. after removing someone who knew the PIN)."""
    with _auth_lock:
        n = len(TOKENS)
        TOKENS.clear()
        return n


PANEL_HEADER = "X-Badger-Panel"


def panel_key_ok(value) -> bool:
    """Is `value` the front panel's key? The portal only has the key's SHA-256
    ([portal] panel_key_hash, root:prepper 0640); the key itself is root-only, so neither the
    portal's user nor any other service account can present it. With no hash file at all, only a
    standalone portal (development, the go-bag laptop: no front panel) trusts loopback alone."""
    try:
        want = CFG.panel_key_hash.read_text().strip().lower()
    except FileNotFoundError:
        return bool(CFG.standalone)
    except OSError:
        return False                                   # there but unreadable: fail closed
    if not re.fullmatch(r"[0-9a-f]{64}", want) or not isinstance(value, str) or not value.strip():
        return False
    got = hashlib.sha256(value.strip().encode("utf-8", "surrogatepass")).hexdigest()
    return hmac.compare_digest(got, want)


def prune_tokens() -> None:
    now = time.time()
    with _auth_lock:
        for k in [k for k, exp in TOKENS.items() if exp <= now]:
            TOKENS.pop(k, None)
        while len(TOKENS) > 50:                          # oldest sessions go first
            TOKENS.pop(min(TOKENS, key=TOKENS.get), None)


def dumps_safe(obj) -> str:
    """JSON with <, > and & written as \\u escapes: the same data to any JSON
    parser, but a board post or search query echoed back can never read as
    markup, whatever a browser decides the response is."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def probe(url: str, timeout: float = 1.5) -> bool:
    if url.startswith("file://"):                      # daemons drop a heartbeat file
        p = Path(url.removeprefix("file://"))
        return p.exists() and time.time() - p.stat().st_mtime < 120
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def services_up() -> dict[str, bool]:
    def run():
        out = {}
        threads = []
        def one(name, url):
            out[name] = probe(url)
        for name, url in CFG.services.items():
            t = threading.Thread(target=one, args=(name, url)); t.start(); threads.append(t)
        for t in threads:
            t.join(3)
        return dict(out)
    # a hung service makes its probe wait 1.5 s: refresh in the background, never in a phone's request
    return cached_background("services", 15, run, wait=3.5, default={})


def load_renderers() -> dict:
    return cached("renderers", 60, lambda: tomllib.loads(CFG.renderers_file.read_text()).get("kind", {}))


def settings() -> dict:
    return read_json(CFG.settings_file, {}) or {}


_settings_lock = threading.Lock()


def update_settings(fn) -> dict:
    """Read-modify-write portal-settings.json (fn(dict) → dict), atomically. For feature plugins (posture
    overrides, "what this Badger reveals" fixes)."""
    with _settings_lock:
        cur = settings()
        new = fn(dict(cur) if isinstance(cur, dict) else {})
        if not isinstance(new, dict):
            raise ValueError("settings must stay a dict")
        CFG.settings_file.parent.mkdir(parents=True, exist_ok=True)
        upd.atomic_write(CFG.settings_file, json.dumps(new, indent=2))
    _cache.pop("posture", None)
    return new


def posture_state(fresh: bool = False) -> tuple[dict, dict]:
    """(posture info, effective settings) — software/common/posture.py, read at most once a second. A damaged
    posture file reads as Lockdown (fail closed)."""
    if fresh:
        _cache.pop("posture", None)

    def run():
        info = posture_mod.read(CFG.posture_file, CFG.raw)
        return info, posture_mod.effective(info["level"], CFG.raw, settings())
    return cached("posture", 1.0, run)


def posture_value(knob: str):
    return posture_state()[1][knob]["value"]


def visitor_mesh_mode() -> str:
    """"approve" (a visitor's radio request waits for a family member) or "off" — [portal] visitor_mesh, else
    the posture (Lockdown: off)."""
    return posture_value("visitor_mesh")


VISITOR_GATED = {"aircraft": "visitor_radio_pages", "satellites": "visitor_radio_pages"}
LOCKDOWN_MSG = "This page is for family members only right now."


def visitor_refusal(feat: dict, route: dict, headers) -> str | None:
    """Why a VISITOR (no family key, not the admin, not the panel) may not use this feature route under the
    current posture, or None. Neighbourhood pages (group "community") follow visitor_community, the aircraft
    and satellite pages visitor_radio_pages, sensors visitor_sensors ("none": not even alerts). Aid-station
    volunteers (their own aid key) are never refused here: the aid station decides for them."""
    if route.get("access", "admin") != "public":
        return None                                  # the feature's own access check applies
    fid = feat["id"]
    if fid == "sensors":
        return LOCKDOWN_MSG if posture_value("visitor_sensors") == "none" else None
    knob = VISITOR_GATED.get(fid) or ("visitor_community" if feat["info"].get("group") == "community" else None)
    if knob is None or posture_value(knob) != "members":
        return None
    if fid == "aidstation" and headers.get("X-Aid-Token"):
        return None
    return LOCKDOWN_MSG


def ctl(*args: str, input: str | None = None) -> tuple[int, str]:  # noqa: A002 — mirrors subprocess.run
    """Privileged actions go through one whitelisted helper (see os/bin/prepperctl).
    Secrets (the uplink Wi-Fi password) go on stdin via `input`, never in argv,
    where every local user could read them with ps."""
    cmd = CFG.ctl.split() + list(args)
    if CFG.standalone and not Path(cmd[-len(args) - 1]).exists():
        return 0, f"(dev) would run: {' '.join(cmd)}"
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, input=input, check=False)
        return res.returncode, (res.stdout + res.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


# ─────────────────────────────── status ──────────────────────────────────────

def ap_clients() -> int | None:
    if shutil.which("iw"):
        try:
            out = subprocess.run(["iw", "dev", CFG.ap_iface, "station", "dump"],
                                 capture_output=True, text=True, timeout=3).stdout
            return out.count("Station ")
        except (OSError, subprocess.SubprocessError):
            pass
    leases = Path("/run/prepper-dnsmasq/leases")        # in RAM (render.py); usually not readable by us
    try:
        return sum(1 for line in leases.read_text().splitlines() if line.strip())
    except OSError:
        pass
    return None


def cpu_temp() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def uptime_s() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        return None


def clock_now() -> dict:
    """The same instant three ways, for the panel and for pages that can't format time themselves."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    u = _dt.datetime.now(_dt.timezone.utc)
    try:
        loc = u.astimezone(ZoneInfo(CFG.time_tz))
    except Exception:  # noqa: BLE001 — bad zone name: fall back to UTC rather than fail the status call
        loc = u
    ref = CFG.time_mode if CFG.time_mode in ("utc", "civil") else "civil"
    nt = nutime.nu_time(u, ref, tz=CFG.time_tz, with_sun=False)
    display = settings().get("display_time", CFG.time_display)
    return {"utc": u.isoformat().replace("+00:00", "Z"), "local": loc.isoformat(), "tz": CFG.time_tz,
            "utc_hms": u.strftime("%H:%M:%S UTC"), "local_hms": loc.strftime("%H:%M:%S %Z"),
            "nutime": nt["display"] + " NT", "reference": ref, "display": display,
            **({"tz_note": CFG.time_tz_note} if CFG.time_tz_note else {})}


def uplink() -> dict | None:
    """Which internet link the Badger has for updates (Ethernet or the onboard Wi-Fi)."""
    def run():
        if not shutil.which("nmcli"):
            return None
        try:
            out = subprocess.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"],
                                 capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            dev, typ, state, conn = (line.split(":") + ["", "", "", ""])[:4]
            if dev != CFG.ap_iface and typ in ("ethernet", "wifi") and state.startswith("connected"):
                return {"device": dev, "type": typ, "connection": conn}
        return None
    return cached("uplink", 15, run)


GPSD_ADDR = ("127.0.0.1", 2947)


def gps_fix() -> dict | None:
    """One TPV report from gpsd (localhost:2947), cached 10 s. None if no fix/gpsd.
    Asked in the background: gpsd with no receiver (or no fix yet) answers nothing for seconds, and
    /api/status must not wait for that (it took 1.5 s whenever the cache ran out)."""
    def run():
        try:
            with socket.create_connection(GPSD_ADDR, timeout=1.0) as s:
                s.sendall(b'?WATCH={"enable":true,"json":true}\n')
                s.settimeout(1.5)
                buf, deadline = b"", time.monotonic() + 2.5
                while time.monotonic() < deadline:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    for line in buf.split(b"\n"):
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("class") == "TPV" and msg.get("mode", 0) >= 2 and "lat" in msg:
                            return {"lat": msg["lat"], "lon": msg["lon"], "alt": msg.get("altHAE"),
                                    "time": msg.get("time"), "mode": msg["mode"]}
        except OSError:
            return None
        return None
    return cached_background("gps", 10, run, wait=0.3)


def time_sync() -> dict:
    """How trustworthy the clock is: chrony's current source, stratum and offset."""
    def run():
        if not shutil.which("chronyc"):
            return {"source": "system clock", "synced": None}
        try:
            out = subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=3).stdout
            f = out.strip().split(",")
            name, stratum, offset, leap = f[1], int(f[2]), float(f[4]), f[13]
            src = {"GPS": "GPS", "PPS": "GPS + PPS"}.get(name, name or "RTC")
            return {"source": src, "stratum": stratum, "offsetSeconds": offset,
                    "synced": leap.strip().lower() != "not synchronised"}
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            return {"source": "system clock", "synced": None}
    return cached("timesync", 30, run)


def coord(text: str, name: str, limit: float) -> float:
    """A latitude/longitude from a query string: finite and in range, or ValueError (→ 400)."""
    try:
        v = float(text)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(v) or abs(v) > limit:
        raise ValueError(f"{name} must be between -{limit:g} and {limit:g}")
    return v


def nu_time_now(qs: dict | None = None, coarse: bool = False) -> dict:
    """coarse=True (visitors): the Badger's own position (GPS or [time] latitude/longitude) is rounded
    BEFORE anything is computed from it — solar time and sunrise/sunset to the second would otherwise
    give the exact place away. Coordinates the caller sent in the query are its own and used as given."""
    qs = qs or {}
    one = lambda k: (qs.get(k) or [None])[0]
    gps = gps_fix() or {}
    lat = coord(one("lat"), "lat", 90) if one("lat") else gps.get("lat", CFG.time_lat)
    lon = coord(one("lon"), "lon", 180) if one("lon") else gps.get("lon", CFG.time_lon)
    own_lon = bool(lon is not None and not one("lon"))
    if coarse and (own_lon or (lat is not None and not one("lat"))):
        # snap the Badger's own fix to the visitor grid (both coordinates together: the longitude
        # step depends on the latitude row); a coordinate the caller sent stays as sent
        c = visitor.coarse(gps.get("lat", CFG.time_lat), gps.get("lon", CFG.time_lon))
        if not one("lat"):
            lat = c[0] if c else None
        if own_lon:
            lon = c[1] if c else None
    mode = one("mode") or CFG.time_mode
    if mode in ("solar", "apparent") and lon is None:
        mode = "civil"                                # no location yet: fall back, and say so
    tz, tz_note = one("tz") or CFG.time_tz, None
    if not valid_tz(tz):
        tz_note = f"unknown time zone {tz!r}; using UTC"
        tz = "UTC"
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    rec = nutime.nu_time(now, mode, lat=lat, lon=lon, tz=tz,
                         locale=one("locale") or CFG.time_locale, dst_policy=CFG.time_dst)
    if tz_note:
        rec.setdefault("notes", {})["timezone"] = tz_note
    rec["sync"] = time_sync()
    rec["locationSource"] = "query" if one("lat") else ("gps" if gps.get("lat") is not None else
                                                        ("config" if CFG.time_lat is not None else None))
    if lat is not None and lon is not None:
        # Sunrise/sunset for *today at that place*: the local calendar date in its
        # time zone (a place given only by coordinates: its mean-solar date).
        place_tz = tz if (one("tz") or not one("lon")) else None
        rec["sunEvents"] = nutime.sun_events_at(now, lat, lon, place_tz)
        if coarse:
            rec["sunEvents"] = visitor.sun_view(rec["sunEvents"])    # to the minute
    if coarse and own_lon and isinstance(rec.get("notes"), dict):
        rec["notes"].pop("longitudeUsed", None)      # the rounded value is in "location" already
    return rec


def software_version() -> str:
    """The release in the repo's VERSION file (installed with the software), e.g. "0.4.0"."""
    try:
        v = (HERE.parent.parent / "VERSION").read_text().strip()
        return v if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", v) else "unknown"
    except OSError:
        return "unknown"


def status() -> dict:
    du = shutil.disk_usage(CFG.upd.data_root) if CFG.upd.data_root.exists() else None
    report = read_json(CFG.upd.report_dir / "last-check.json", {}) or {}
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    return {
        "edition": CFG.edition, "version": software_version(),
        "hostname": CFG.hostname, "ssid": CFG.ssid,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "uptime_s": uptime_s(), "load": [round(x, 2) for x in load], "cpu_temp_c": cpu_temp(),
        "clients": ap_clients(),
        "storage": {"total": du.total, "used": du.used, "free": du.free} if du else None,
        "power": read_json(CFG.power_file, None),
        "services": services_up(),
        "updates": {"checked": report.get("generated"), "counts": report.get("counts"),
                    "download_human": report.get("download_human")},
        "build": read_json(CFG.build_file, None),
        "alerts": active_alerts(),
        "mesh": mesh_courtesy_status(),
        "sdr": sdr_status(),
        "gps": gps_fix(),
        "clock": clock_now(),
        "uplink": uplink(),
        "nutime": (lambda r: {"display": r["display"], "reference": r["reference"]})(
            nutime.nu_time(None, CFG.time_mode if CFG.time_mode in ("utc", "civil") else "utc",
                           tz=CFG.time_tz, with_sun=False)),
        "posture": posture_summary(),               # (family, admin and the panel only: visitor.status_view)
    }


def posture_summary() -> dict | None:
    try:
        info = posture_state()[0]
    except Exception:  # noqa: BLE001 — never lose /api/status over it
        return None
    return {"level": info["level"], "label": info["label"], "changed_at": info.get("changed_at"),
            "damaged": bool(info.get("damaged"))}


def sdr_status() -> dict | None:
    """Weather radio listening/paused (and by what, until when) and SDR 2's state, for the status bar."""
    if sdrcommon is None:
        return None
    try:
        st = sdrcommon.read_status()
    except Exception:  # noqa: BLE001 — a bad status file must not break /api/status
        return None
    d = st.get("dongles") if isinstance(st.get("dongles"), dict) else {}
    until = st.get("until")
    return {"running": bool(st.get("running")), "weather_radio": str(st.get("weather_radio") or "")[:20],
            "by": str(st.get("by") or "")[:40] or None, "until": until if isinstance(until, (int, float)) else None,
            "sdr2": str(d.get("sdr2_state") or "")[:20] or None, "antenna": str(d.get("antenna") or "")[:20] or None}


def coarse_position(gps: dict | None) -> dict | None:
    """What visitors get: the Badger's position to ~10 km (enough for the map and sun times),
    never the precise spot the family is sheltering at. Members, admins and the panel get it all.
    (The one rule for every public page lives in software/common/visitor.py.)"""
    return visitor.coarse_position(gps)


MESH_STATE_NAMES = ("mesh-channel.json", "mesh-nodes.json", "mesh-peers.json", "mesh-bt-pin.json", "mesh-circle.json")
_mesh_guard: dict[str, tuple] = {}
_mesh_guard_lock = threading.Lock()


def _on_ram(path: Path) -> bool:
    s = str(path)
    return any(s == d or s.startswith(d.rstrip("/") + "/") for d in ("/run", "/dev/shm", str(CFG.run_dir)))


def mesh_state_paths() -> list[Path]:
    """Radio-state files that are NOT in RAM: a [portal] path pointing at persistent storage, and the old
    default locations in the portal's folder (left from before the bridge kept them in /run)."""
    out = []
    for p in (CFG.mesh_channel_link, CFG.mesh_nodes, CFG.mesh_peers, CFG.mesh_bt_pin_file, CFG.mesh_circle_link,
              *(CFG.mesh_outbox.parent / n for n in MESH_STATE_NAMES)):
        if not _on_ram(p) and p not in out:
            out.append(p)
    return out


def guard_mesh_state() -> int:
    """Never keep radio state in the clear on the disk: any of mesh_state_paths() holding plain JSON (written
    by an older bridge, a hand-made config, a copy) is sealed in place — {"sealed": token}, plus empty
    "nodes"/"peers" lists — with the device's public key (sealed records). Cheap when nothing changed (one
    stat per file); called on every request and by the readers. Returns how many files were sealed."""
    n = 0
    for path in mesh_state_paths():
        try:
            st = path.stat()
        except OSError:
            continue
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        key = str(path)
        if _mesh_guard.get(key) == sig:
            continue
        with _mesh_guard_lock:
            try:
                if st.st_size > 4_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                try:
                    d = json.loads(text) if text.strip() else None
                except (ValueError, RecursionError):
                    d = None
                if isinstance(d, dict) and sealed.is_token(d.get("sealed")):
                    _mesh_guard[key] = sig
                    continue
                if not text.strip():
                    _mesh_guard[key] = sig
                    continue
                out = {"sealed": sealed_store().seal_text(text)}
                for k in ("nodes", "peers"):
                    if isinstance(d, dict) and k in d:
                        out[k] = []
                mode = st.st_mode & 0o777
                private_write(path, json.dumps(out))
                os.chmod(path, mode & 0o660 or 0o600)
                st = path.stat()
                _mesh_guard[key] = (st.st_ino, st.st_mtime_ns, st.st_size)
                n += 1
                print(f"[portal] sealed {path.name} (radio state on persistent storage)", file=sys.stderr)
            except Exception as e:  # noqa: BLE001 — never break a request over it; tried again next time
                print(f"[portal] could not seal {path.name}: {type(e).__name__}", file=sys.stderr)
    return n


def mesh_state(path: Path, default):
    """A radio-state file the bridge writes (RAM), or its sealed form on persistent storage (opened if the
    Badger is unlocked, or it was sealed during this run); `default` when it can't be read."""
    guard_mesh_state()
    d = read_json(path, None)
    if isinstance(d, dict) and sealed.is_token(d.get("sealed")):
        try:
            d = json.loads(sealed_store().open_text(d["sealed"], recent_ok=True))
        except (sealed.Locked, sealed.SealError, ValueError, RecursionError):
            return default
    return d if d is not None else default


def mesh_nodes() -> dict:
    """Positions shared by radios on the mesh (written by meshbridge), validated for the map."""
    raw = mesh_state(CFG.mesh_nodes, {}) or {}
    now, out = time.time(), []
    for n in (raw.get("nodes") if isinstance(raw, dict) else None) or []:
        if not isinstance(n, dict):
            continue
        lat, lon = n.get("lat"), n.get("lon")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        heard = n.get("heard") if isinstance(n.get("heard"), (int, float)) else None
        if heard is not None and now - heard > 86400:
            continue                                     # positions older than a day: not shown (nor kept)
        bat = n.get("battery") if isinstance(n.get("battery"), (int, float)) else None
        out.append({"id": str(n.get("id", ""))[:16], "name": str(n.get("name", ""))[:40],
                    "short": str(n.get("short", ""))[:4], "lat": lat, "lon": lon,
                    "age_s": None if heard is None else max(0, int(now - heard)),
                    "battery": bat, "self": n.get("self") is True})
    return {"nodes": out[:500], "updated": raw.get("updated") if isinstance(raw, dict) else None}


def mesh_courtesy_status() -> dict | None:
    """"Badgers nearby: 3 · channel busy 18 %" for the status bar, from meshbridge's mesh-peers.json.
    Read-only and tolerant: None when the bridge isn't running (no file, or older than 15 minutes)."""
    raw = mesh_state(CFG.mesh_peers, None)
    if not isinstance(raw, dict):
        return None

    def num(v, lo, hi):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi else None
    upd = num(raw.get("updated"), 0, 1e11)
    if upd is None or not -60 <= time.time() - upd <= 900:
        return None
    peers = int(num(raw.get("peers_active"), 0, 10000) or 0)
    util = num(raw.get("channel_util"), 0, 100)
    air = raw.get("airtime") if isinstance(raw.get("airtime"), dict) else {}
    counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    queued = air.get("queued") if isinstance(air.get("queued"), dict) else {}
    deferred = air.get("deferred") if isinstance(air.get("deferred"), dict) else {}
    total = lambda d: int(sum(v for v in d.values() if num(v, 0, 1e9) is not None))   # noqa: E731
    silent = raw.get("silent") is True
    line = f"Badgers nearby: {peers}"
    if util is not None:
        line += f" · channel busy {round(util)} %"
    if silent:
        line += " · radio silence"
    return {"peers": peers, "channel_busy_pct": util, "air_tx_pct": num(raw.get("air_util_tx"), 0, 100),
            "courtesy": raw.get("enabled") is not False, "silent": silent, "queued": total(queued),
            "deferred": total(deferred), "suppressed": int(num(counts.get("suppressed"), 0, 1e9) or 0),
            "line": line}


def active_alerts() -> list[dict]:
    """SAME alerts written by sdr/alertd; expired ones are dropped here."""
    now = time.time()
    raw = read_json(CFG.alerts_file, []) or []
    out = []
    for a in raw if isinstance(raw, list) else []:
        exp = a.get("expires_ts", now + 1) if isinstance(a, dict) else None
        if isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > now:
            out.append(a)
    return out


# ─────────────────────────────── board ───────────────────────────────────────

_board_lock = threading.Lock()
_last_post: dict[str, float] = {}
_members: members_mod.Members | None = None
BOARDS = ("community", "family")
BOARD_MAX_BYTES = 2_000_000          # a board file past this keeps its newest half (flood protection)
OUTBOX_MAX_BYTES = 512_000           # the radio queue: past this, posts still land on the board, not the mesh
TAIL_BYTES = 512_000                 # boards are read from the end, never whole
_SURROGATES = re.compile(r"[\ud800-\udfff]")


def members_db() -> members_mod.Members:
    global _members
    if _members is None or _members.path != CFG.members_file:
        _members = members_mod.Members(CFG.members_file, sealer=sealed_store())
    return _members


def sealed_store() -> sealed.Sealer:
    """This portal's sealed-records instance ([sealed] in prepper.toml; state in <portal data>/sealed). During a
    duress: the made-up household's own sealer (see enter_decoy)."""
    if DECOY.get("sealer") is not None:
        return DECOY["sealer"]
    o = sealed_api.options(CFG)
    s = sealed.use(o.pop("folder"), **o)
    if "family-board" not in s.migrators:
        s.register_migrator("members", _reseal_members)
        s.register_migrator("family-board", lambda: seal_family_board(reseal=True))
    return s


def _reseal_members() -> None:
    db = members_db()
    if db.path.exists():
        with db.lock:
            db._save(db._load())


def family_notice(text: str, key: str) -> None:
    """A note from the Badger itself on the family board (a device or radio added to someone, a member
    removed): what an admin does to the family is always visible to the family. Never over the mesh."""
    try:
        board_post(f"system:{key}", "", text, False, "family", system="Badger")
    except Exception as e:  # noqa: BLE001 — the change itself stands even if the note can't be written
        print(f"[portal] family board note failed: {type(e).__name__}", file=sys.stderr)


def check_radio_pairings() -> list[dict]:
    """Bind radios whose "!pair CODE" the mesh bridge heard on the family channel (CFG.mesh_pair_file:
    {"confirms": [{"radio", "code", "at"}]}, written by meshbridge, read-only here). Cheap when nothing
    is waiting; called when members or the admin look at radios."""
    try:
        if CFG.mesh_pair_file.stat().st_size > 64 * 1024:
            return []
    except OSError:
        return []
    raw = read_json(CFG.mesh_pair_file, {}) or {}
    confirms = raw.get("confirms") if isinstance(raw, dict) else None
    if not isinstance(confirms, list) or not confirms:
        return []
    try:
        done = members_db().confirm_radios(confirms)
    except members_mod.StoreError:
        return []
    for r in done:
        family_notice(f"The radio {r['radio']} was linked to {r['name']}. Posts from it on the family channel "
                      f"now carry that name. If that's not right, tell the Badger's admin.", r["radio"])
    return done


def panel_lock_on() -> bool:
    """Has the owner set a front-panel code? (The lock file is root-only; existence is all we check.)"""
    try:
        return CFG.panel_lock_file.exists()
    except OSError:
        return False


def mesh_secret(secure: bool) -> dict:
    """The family radio link (= the channel key) and the radio's Bluetooth PIN, for a family member's own
    device. Over plain HTTP on the hotspot it is withheld (anyone who knows the Wi-Fi password could read
    it); the Badger's own screen always shows it."""
    link = mesh_state(CFG.mesh_channel_link, {}) or {}
    url = link.get("url") if isinstance(link, dict) and isinstance(link.get("url"), str) else None
    at = link.get("at") if isinstance(link, dict) and isinstance(link.get("at"), (int, float)) else None
    if url and not secure:
        return {"ok": True, "url": None, "at": at, "secret_hidden": True,
                "message": "The family radio link holds the channel key, so it isn't sent over this unencrypted "
                           "connection. Read it on the Badger's own screen (press the knob → Family radio), or "
                           "open this page with https:// to see it here."}
    pin = mesh_state(CFG.mesh_bt_pin_file, {}) or {}
    pin = pin.get("pin") if isinstance(pin, dict) and isinstance(pin.get("pin"), str) else None
    return {"ok": bool(url), "url": url, "at": at, "bt_pin": pin,
            "error": None if url else "The radio bridge hasn't reported the channel link yet."}


def board_file(board: str) -> Path:
    return CFG.family_board_file if board == "family" else CFG.board_file


def tail_lines(path: Path, limit: int, max_bytes: int = TAIL_BYTES) -> list[bytes]:
    """The last `limit` complete lines of a file, reading at most max_bytes from its end."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        data = fh.read(size - start)
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]                               # the first one is cut off
    return [ln for ln in lines if ln.strip()][-limit:]


STORAGE_FULL_MSG = "The Badger's storage is full, so this wasn't saved. Ask the Badger's owner to free some space."


def is_storage_full(e: BaseException) -> bool:
    return isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT)


PRIVATE_MODE = 0o640      # portal data (boards, mesh outbox, feature files): user + group prepper only


def private_write(path: Path, text: str) -> None:
    """atomic_write for portal data: created 0640, never world-readable whatever the umask."""
    upd.atomic_write(path, text, PRIVATE_MODE)


def open_private_append(path: Path):
    """Open a JSON-lines file for appending, creating it 0640 (a plain open() gives 0644 under the
    usual umask). An existing file that is still world-readable from an older release is tightened."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_MODE)
    try:
        if os.fstat(fd).st_mode & 0o007:
            os.fchmod(fd, PRIVATE_MODE)
    except OSError:
        pass
    return os.fdopen(fd, "a", encoding="utf-8")


def trim_jsonl(path: Path, max_bytes: int) -> None:
    """Keep a JSON-lines file under max_bytes by dropping its oldest half (atomic replace)."""
    try:
        if path.stat().st_size <= max_bytes:
            return
        keep = b"\n".join(tail_lines(path, 10 ** 9, max_bytes // 2)) + b"\n"
        private_write(path, keep.decode("utf-8", "replace"))
    except OSError as e:
        print(f"[portal] could not trim {path.name}: {e}", file=sys.stderr)


def _family_open(rec: dict, fam: dict) -> dict | None:
    """One family-board line: {"s": sealed token} → the post (None while locked, unless it was written since
    the Badger locked), or an older plain line as it is (counted, so it gets sealed)."""
    tok = rec.get("s") if len(rec) == 1 else None
    if not sealed.is_token(tok):
        fam["plain"] += 1
        return rec
    try:
        obj = sealed_store().open_json(tok, recent_ok=True)
    except sealed.Locked:
        fam["hidden"] += 1
        return None
    except sealed.SealError:
        fam["unreadable"] += 1
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("name") == sealed.LOCKED_NAME and isinstance(obj.get("member"), str) and not fam["locked"]:
        if fam.get("names") is None:                   # posted while locked: the name is known now
            try:
                fam["names"] = {m["id"]: m["name"] for m in members_db().list()["members"]}
            except members_mod.StoreError:
                fam["names"] = {}
        obj["name"] = fam["names"].get(obj["member"], obj["name"])
    return obj


def seal_family_board(reseal: bool = False) -> int:
    """Seal the family board's plain lines (older posts; lines the mesh bridge appends) — or, for the
    sealed-records setup, re-seal every line with this run's key. The file is only replaced if nothing was
    appended meanwhile (else we try again later). Returns how many lines were (re)sealed."""
    path, s = CFG.family_board_file, sealed_store()
    with _board_lock:
        try:
            before = path.stat().st_size
            raw = path.read_bytes()
        except OSError:
            return 0
        if raw and not raw.endswith(b"\n"):
            return 0                                    # a line is being written right now
        out, n = [], 0
        for ln in raw.split(b"\n"):
            if not ln.strip():
                continue
            try:
                rec = json.loads(ln)
            except (ValueError, RecursionError):
                continue                                # junk is dropped
            if not isinstance(rec, dict):
                continue
            tok = rec.get("s") if len(rec) == 1 else None
            if sealed.is_token(tok):
                obj = s.try_json(tok, None) if reseal else None
                if isinstance(obj, dict):
                    out.append(sealed.seal_jsonl_line(obj, s))
                    n += 1
                else:
                    out.append(ln.decode("utf-8", "replace"))
                continue
            out.append(sealed.seal_jsonl_line(rec, s))
            n += 1
        try:
            if not n or path.stat().st_size != before:
                return 0
            sealed.replace_scrubbing(path, "\n".join(out) + "\n", PRIVATE_MODE)
        except OSError as e:
            print(f"[portal] could not seal the family board: {type(e).__name__}", file=sys.stderr)
            return 0
    return n


# ── family radio messages while locked (docs/SECURITY.md → "Sealed records", docs/MESH.md) ──
# The mesh bridge can only seal with a key of its own, which a locked launcher can't open. So when the family
# board is on persistent storage, the bridge appends the family lines it hears to a RAM spool
# (CFG.family_inbox, /run/prepper/family-inbox.jsonl, 0600), and this launcher seals them with its CURRENT data
# key — they count as "written since it locked", so family members can read them while it is locked. Both
# sides hold flock(LOCK_EX) on the spool: the bridge for each append, we while copying and truncating, so no
# line is lost or doubled by a concurrent append. A reboot empties /run: lines not yet taken in are lost
# (taken in on each family-board read and every FAMILY_INBOX_EVERY seconds, so normally within seconds).
FAMILY_INBOX_EVERY = 5.0
FAMILY_INBOX_MAX_BYTES = 2_000_000          # read at most this much at once (the bridge stops at 1 MB)
_INBOX_FIELDS = {"at": 32, "name": 32, "text": 2000, "radio": 16, "member": 16}
_inbox_lock = threading.Lock()


def _inbox_record(ln: bytes) -> dict | None:
    """One spool line → a family-board post: only the bridge's fields, clipped; always an unverified radio post."""
    try:
        rec = json.loads(ln)
    except (ValueError, RecursionError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("text"), str) or not rec["text"].strip():
        return None
    out = {k: _SURROGATES.sub("", rec[k])[:n] for k, n in _INBOX_FIELDS.items() if isinstance(rec.get(k), str)}
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", out.get("at", "")):
        out["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out.setdefault("name", "radio")
    out.update(via="mesh-in", verified=False)          # a radio ID proves nothing on the air
    return out


def ingest_family_inbox() -> int:
    """Take the family radio lines the bridge spooled in RAM onto the family board, each sealed with this run's
    current key. The spool is truncated only after the board write succeeded (a crash in between can show a
    line twice, never lose one). Cheap when there is nothing (one lstat). Returns how many lines were taken."""
    path = CFG.family_inbox
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        return 0
    with _inbox_lock:
        try:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return 0
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            chunks, size = [], 0
            while size < FAMILY_INBOX_MAX_BYTES:
                b = os.pread(fd, 1 << 16, size)
                if not b:
                    break
                chunks.append(b)
                size += len(b)
            data = b"".join(chunks)
            cut = data.rfind(b"\n") + 1
            if cut <= 0:
                return 0                               # (the bridge writes whole lines under the lock)
            recs = [r for r in (_inbox_record(ln) for ln in data[:cut].split(b"\n") if ln.strip()) if r]
            if recs:
                s = sealed_store()
                lines = "".join(sealed.seal_jsonl_line(r, s) + "\n" for r in recs)
                with _board_lock:
                    path_b = CFG.family_board_file
                    path_b.parent.mkdir(parents=True, exist_ok=True)
                    with open_private_append(path_b) as fh:
                        fh.write(lines)
                        fh.flush()
                        os.fsync(fh.fileno())
                    trim_jsonl(path_b, BOARD_MAX_BYTES)
            rest = data[cut:] + os.pread(fd, FAMILY_INBOX_MAX_BYTES, size)   # (only past our read cap)
            os.ftruncate(fd, 0)
            if rest.strip():
                os.pwrite(fd, rest, 0)
            return len(recs)
        except (OSError, sealed.SealError) as e:       # the spool stays as it is: tried again later
            print(f"[portal] family radio messages not taken in yet: {type(e).__name__}", file=sys.stderr)
            return 0
        finally:
            os.close(fd)                               # (releases the lock)


def _family_inbox_loop() -> None:
    while True:
        time.sleep(FAMILY_INBOX_EVERY)
        try:
            with sealed_store().background():
                ingest_family_inbox()
        except Exception as e:  # noqa: BLE001 — never let the timer die
            print(f"[portal] family inbox: {type(e).__name__}", file=sys.stderr)


def start_family_inbox_timer() -> None:
    threading.Thread(target=_family_inbox_loop, daemon=True, name="family-inbox").start()


def family_recent_bounds() -> tuple[float, int]:
    """While locked, family posts sealed since the lock are readable only if they are from the last
    [sealed] recent_hours (48) and among the newest recent_max (200); older ones wait for the unlock."""
    c = (CFG.raw.get("sealed") if isinstance(getattr(CFG, "raw", None), dict) else None) or {}
    h, m = c.get("recent_hours", 48), c.get("recent_max", 200)
    if isinstance(h, bool) or not isinstance(h, (int, float)) or not 0 < h <= 24 * 366:
        h = 48
    if isinstance(m, bool) or not isinstance(m, int) or not 0 < m <= 10000:
        m = 200
    return float(h), m


def _post_epoch(at) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError, OverflowError):
        return None


def _recent_only(posts: list[dict], fam: dict) -> list[dict]:
    hours, most = family_recent_bounds()
    now = time.time()
    keep = [r for r in posts if (t := _post_epoch(r.get("at"))) is not None and now - t <= hours * 3600]
    keep = keep[-most:]
    fam["hidden"] += len(posts) - len(keep)
    return keep


def board_read(limit: int = 200, board: str = "community", for_member: bool = False,
               info: dict | None = None) -> list[dict]:
    """The last `limit` posts. Who-is-a-member marks are only shown to members, and posts that came
    in over the mesh are never "verified": a radio ID proves nothing on the air.
    The family board is sealed: while locked only posts written since the Badger locked are shown (radio
    messages the bridge spooled count, see ingest_family_inbox), and of those only the last [sealed]
    recent_hours / recent_max; `info` (a dict) gets {"locked", "hidden", …} for the page."""
    if board == "family":
        ingest_family_inbox()                          # radio messages waiting in RAM: sealed onto the board now
    fam = {"plain": 0, "hidden": 0, "unreadable": 0, "names": None,
           "locked": board == "family" and not sealed_store().is_unlocked()}
    try:
        lines = tail_lines(board_file(board), limit)
    except OSError:
        lines = []
    out, removed, decided = [], None, None
    for line in lines:
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if board == "family" and isinstance(rec, dict):
            rec = _family_open(rec, fam)
            if rec is None:
                continue
        if not isinstance(rec, dict):
            continue
        if rec.get("via") == "mesh-in":
            rec["verified"] = False
        if rec.get("system") is True or str(rec.get("member", "")).startswith("feature-"):
            rec["system"] = True                       # the Badger itself (older posts too): no ✓
            rec.pop("verified", None)
        if for_member and rec.get("member"):
            if removed is None:
                try:
                    removed = {m["id"] for m in members_db().list()["members"] if m["removed"]}
                except members_mod.StoreError:
                    removed = set()
            rec["removed_member"] = rec["member"] in removed
        if not for_member:
            rec.pop("verified", None)
        rec.pop("member", None)
        pid = rec.pop("pid", None)
        if isinstance(pid, str):                       # a visitor asked for the radio: what became of it
            if decided is None:
                decided = {i.get("id"): i.get("status") for i in mesh_pending_load()}
            rec["mesh"] = decided.get(pid) or "expired"
            if rec["mesh"] == "sent":
                rec["via"] = "mesh-out"
        out.append(rec)
    if board == "family":
        if fam["locked"]:
            out = _recent_only(out, fam)               # while locked: the last 48 h / 200 posts at most
        if fam["plain"]:
            seal_family_board()
        if info is not None:
            info.update(locked=fam["locked"], hidden=fam["hidden"], unreadable=fam["unreadable"])
            if fam["locked"]:
                info["recent_hours"], info["recent_max"] = family_recent_bounds()
    return out


# ── visitors' "send over mesh": a family member or the admin decides (red-team finding 10) ──
# The Badger's radio is the household's transmitter: an anonymous visitor must not make it send a lure
# under any name, or key it at will for a direction-finding team. Members' and the admin's posts go
# straight out; a visitor's request waits (at most MESH_PENDING_MAX at a time, MESH_PENDING_HOURS each).
MESH_PENDING_MAX = 30
MESH_PENDING_HOURS = 24


def mesh_pending_load() -> list[dict]:
    d = read_json(CFG.mesh_pending, {}) or {}
    items = d.get("items") if isinstance(d, dict) else None
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)] if isinstance(items, list) else []


def _mesh_pending_save(items: list[dict]) -> None:
    """(caller holds _board_lock) Keep every undecided request that hasn't expired and the last 200 decisions."""
    now = time.time()
    for i in items:
        if i.get("status") == "pending" and now - float(i.get("at") or 0) > MESH_PENDING_HOURS * 3600:
            i["status"] = "expired"
    waiting = [i for i in items if i.get("status") == "pending"]
    for i in waiting[:-MESH_PENDING_MAX]:
        i["status"] = "expired"                      # too many waiting: the oldest lapse
    CFG.mesh_pending.parent.mkdir(parents=True, exist_ok=True)
    private_write(CFG.mesh_pending, json.dumps({"items": items[-200:]}))


def mesh_pending_list() -> list[dict]:
    """What is waiting, for members and the admin (newest first)."""
    now = time.time()
    return [{"id": i["id"], "name": str(i.get("name", ""))[:40], "text": str(i.get("text", ""))[:300], "at": i.get("at")}
            for i in reversed(mesh_pending_load())
            if i.get("status") == "pending" and now - float(i.get("at") or 0) <= MESH_PENDING_HOURS * 3600]


def mesh_pending_decide(pid: str, send: bool, by: str) -> tuple[int, str]:
    """Send (or turn down) one visitor's request. by: who decided, kept with the record."""
    with _board_lock:
        items = mesh_pending_load()
        hit = next((i for i in items if i.get("id") == pid), None)
        if hit is None or hit.get("status") != "pending" or \
                time.time() - float(hit.get("at") or 0) > MESH_PENDING_HOURS * 3600:
            return 404, "That request isn't waiting any more."
        if send:
            text = f"{hit.get('name')}: {hit.get('text')}"
            if not outbox_append(outbox_record(text, "community", "community")):
                return 503, "The radio queue is full right now. Try again later."
        hit.update(status="sent" if send else "declined", by=str(by)[:40], decided=int(time.time()))
        _mesh_pending_save(items)
    return 200, "Sent over the radio." if send else "Not sent. It stays on the board."


MESH_KINDS = ("alert", "danger", "family", "reply", "community", "auto")


def outbox_record(text: str, board: str, kind: str | None = None, dedupe: str | None = None,
                  hop_limit: int | None = None, member: bool = False) -> dict:
    """One mesh-outbox line (docs/MESH.md): {"text","board"} plus "kind" (priority; default: the board),
    "at" (so the bridge can drop it if it waited too long), and optional "dedupe" (a key every Badger
    computes the same way: only one of them sends it), "hop_limit" (1–7) and "member" (typed by a family
    member: may still go out on the family channel during radio silence). Bad optional values are left out."""
    rec = {"text": text, "board": board, "kind": kind if kind in MESH_KINDS else board, "at": int(time.time())}
    if isinstance(dedupe, str) and 0 < len(dedupe) <= 120 and dedupe.isprintable():
        rec["dedupe"] = dedupe
    if isinstance(hop_limit, int) and not isinstance(hop_limit, bool) and 1 <= hop_limit <= 7:
        rec["hop_limit"] = hop_limit
    if member is True and board == "family":
        rec["member"] = True
    return rec


def outbox_append(rec: dict) -> bool:
    """Queue one message for the mesh bridge (caller holds _board_lock). False when the queue is
    full (the bridge has been away a long time): the radio is slow, there is no point piling up."""
    CFG.mesh_outbox.parent.mkdir(parents=True, exist_ok=True)
    try:
        if CFG.mesh_outbox.stat().st_size > OUTBOX_MAX_BYTES:
            return False
    except FileNotFoundError:
        pass
    with open_private_append(CFG.mesh_outbox) as fh:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)              # the bridge empties the file under the same lock
        except (ImportError, OSError):
            pass
        fh.write(json.dumps(rec) + "\n")
    return True


def is_system_poster(member) -> bool:
    """A post by the Badger itself (a feature's automatic note), not by a person. Only server code can
    pass such a member dict: request handlers pass members_db() records, whose ids start with "m_"."""
    return isinstance(member, dict) and (member.get("system") is True or str(member.get("id", "")).startswith("feature-"))


def board_post(ip: str, name: str, text: str, mesh: bool, board: str = "community",
               member: dict | None = None, system: str | None = None, approved: bool = False) -> tuple[bool, str]:
    """A board post. Feature plugins post automatic notes as the Badger with system="Supplies" (or the
    older stand-in member {"id": "feature-<id>", "name": …}): shown with a neutral "Badger" mark,
    never the ✓ that means "a family member wrote this".
    mesh=True goes straight to the radio for a family member, the Badger itself and approved=True (the
    admin); a visitor's request waits for a member or the admin (mesh_pending_decide)."""
    if not isinstance(board, str) or board not in BOARDS:
        return False, "no such board"
    if system is not None:
        member = {"id": "feature-system", "name": str(system), "system": True}
    sys_post = is_system_poster(member)
    if board == "family" and not member:
        return False, "the family board is for family members"
    mesh = mesh is True                                # "false", 1, [0] … never send over the radio
    if sys_post:
        name = members_mod.clean_name(member.get("name")) or "Badger"
    else:
        name = member["name"] if member else (members_mod.clean_name(name) or "anonymous")
    text = _SURROGATES.sub("", re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text))).strip()
    if not text:
        return False, "empty message"
    if mesh and len(f"{name}: {text}".encode()) > 200:      # what actually goes over the air
        return False, "mesh messages are limited to 200 bytes, including your name"
    text = text[:2000]
    with _board_lock:                                  # check-and-set, or two posts race through
        now = time.monotonic()                         # a wall-clock jump must not block (or free) posting
        if now - _last_post.get(ip, -1e18) < 5:
            return False, "slow down — one message every 5 seconds"
        _last_post[ip] = now
        if len(_last_post) > 1000:
            for k in [k for k, t in _last_post.items() if now - t >= 5]:
                del _last_post[k]
    ask = mesh and not (member or approved)            # a visitor: ask a family member to send it
    if ask:
        mesh = False
        if visitor_mesh_mode() == "off":
            ask = None
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "name": name, "text": text,
           "via": "mesh-out" if mesh else "local"}
    if sys_post:
        rec["system"] = True
    elif member:
        rec.update(member=member["id"], verified=True)
    path = board_file(board)
    queued = True
    try:
        with _board_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if ask:
                pid = secrets.token_urlsafe(9)
                items = mesh_pending_load()
                items.append({"id": pid, "at": int(time.time()), "name": name, "text": text, "status": "pending"})
                _mesh_pending_save(items)
                rec["pid"] = pid
            if mesh:
                queued = outbox_append(outbox_record(f"{name}: {text}", board, board,
                                                     member=bool(member) and not sys_post))
                if not queued:
                    rec["via"] = "local"
            line = sealed.seal_jsonl_line(rec, sealed_store()) if board == "family" else json.dumps(rec)
            with open_private_append(path) as fh:
                fh.write(line + "\n")
                fh.flush()                             # a full disk shows here, not as a silent "posted"
            trim_jsonl(path, BOARD_MAX_BYTES)
    except (OSError, sealed.SealError) as e:          # SealError: the family board couldn't be sealed
        with _board_lock:
            _last_post.pop(ip, None)                   # nothing was posted: don't make them wait
        if is_storage_full(e):
            return False, STORAGE_FULL_MSG
        print(f"[portal] board post not saved: {type(e).__name__}: {getattr(e, 'strerror', None) or e}",
              file=sys.stderr)
        return False, "The message could not be saved on the Badger. Try again in a moment."
    if not queued:
        return True, "posted here — but the radio queue is full, so it was not sent over the mesh"
    if ask:
        return True, "Posted. A family member will decide whether it also goes out over the radio."
    if ask is None:
        return True, "Posted here. Only family members can send messages over this Badger's radio."
    return True, "posted"


# ─────────────────────────────── library ─────────────────────────────────────

def content_url(rel: str) -> str:
    """/content/ link for a path under data_root, percent-encoded ("a #1.pdf" → "a%20%231.pdf")."""
    return "/content/" + urllib.parse.quote(rel)


def fill_url(template: str, *, item_id: str = "", book: str = "", rel: str = "") -> str:
    """Renderer URL placeholders: {file} is an encoded path, {url} the encoded
    /content/ link URL-encoded once more so it survives as a query value."""
    return (template.replace("{id}", urllib.parse.quote(item_id)).replace("{book}", urllib.parse.quote(book))
            .replace("{file}", urllib.parse.quote(rel))
            .replace("{url}", urllib.parse.quote(content_url(rel), safe="")))


def app_installed(item_id: str, state: "upd.State | None" = None) -> bool:
    """`requires = "app:<id>"`: the catalog app is installed and its files are present."""
    state = state or upd.State(CFG.upd.state_file)
    rec = state.get(item_id)
    root = CFG.upd.data_root.resolve()
    if rec and rec.get("path"):
        p = safe_join(root, rec["path"])
        return bool(p and p.exists())
    return (root / "apps" / item_id).is_dir()


def renderer_available(req: str | None, up: dict, state=None) -> bool:
    if not req:
        return True
    if req.startswith("app:"):
        return app_installed(req[4:], state)
    return bool(up.get(req, False))


def library() -> dict:
    def run():
        items = upd.load_catalog(CFG.upd.catalog)
        state = upd.State(CFG.upd.state_file)
        record = upd.load_personal_record(CFG.upd)
        wanted_ids = {i.id for i in upd.wanted(items, CFG.upd, record)}
        held_ids = {i.id for i in upd.held_personal(items, CFG.upd, record)}   # wait for the owner's OK
        rmap = load_renderers()
        up = services_up()
        defaults = settings().get("renderer_defaults", {})
        out = []
        for it in items:
            rec = state.get(it.id)
            if not rec and it.id not in wanted_ids and it.id not in held_ids:
                continue
            file_rel = f"{it.dir}/{rec['filename']}" if rec else ""
            # kiwix-serve runs with --nodatealiases, so the undated name keeps
            # working across updates — bookmarks and shared links don't break.
            book = re.sub(r"_\d{4}-\d{2}[a-z]?$", "", rec["filename"].removesuffix(".zim")) \
                if rec and it.kind == "zim" else ""
            kind = rmap.get(it.kind, {})
            rends = []
            for r in kind.get("renderer", []):
                if not renderer_available(r.get("requires"), up, state):
                    continue
                url = fill_url(r["url"], item_id=it.id, book=book, rel=file_rel)
                if r["url"].startswith("/kiwix"):
                    url = CFG.kiwix_public + url.removeprefix("/kiwix")
                rends.append({"id": r["id"], "name": r["name"], "url": url, "note": r.get("note", "")})
            default = defaults.get(it.kind, kind.get("default"))
            if rends and default not in {r["id"] for r in rends}:
                default = rends[0]["id"]
            out.append({
                "id": it.id, "title": it.title, "category": it.category, "kind": it.kind,
                "kind_label": kind.get("label", it.kind), "description": it.description,
                "lang": it.lang, "size_gb": it.size_gb, "priority": it.priority,
                "license": it.license, "use": it.use, "held": it.id in held_ids, "installed": bool(rec),
                "version": rec.get("version") if rec else None,
                "renderers": rends if rec else [], "default_renderer": default,
            })
        cats = [{"id": c, "title": t, "blurb": b,
                 "count": sum(1 for i in out if i["category"] == c and i["installed"])}
                for c, t, b in CATEGORIES]
        return {"categories": cats, "items": out, "tools": TOOLS}
    return cached("library", 20, run)


def safe_join(base: Path, rel: str) -> Path | None:
    """base/rel, resolved, or None if it escapes base. Returns a *resolved* path,
    so compare it only with resolved paths (data_root may sit behind a symlink)."""
    if "\x00" in rel:
        return None
    try:
        root = base.resolve()
        p = (root / rel.lstrip("/")).resolve()
        os.stat(p)
    except (FileNotFoundError, NotADirectoryError):
        pass                                          # may not exist (yet): callers check
    except (OSError, ValueError, RuntimeError):     # name too long, symlink loop, …: not a path we serve
        return None
    return p if p == root or root in p.parents else None


def list_files(item_id: str, rel: str) -> dict | None:
    items = {i.id: i for i in upd.load_catalog(CFG.upd.catalog)}
    state = upd.State(CFG.upd.state_file)
    it, rec = items.get(item_id), state.get(item_id)
    if not it or not rec:
        return None
    data_root = CFG.upd.data_root.resolve()
    root = safe_join(data_root, rec.get("path", ""))
    if root is None:
        return None
    target = safe_join(root, rel)
    if target is None or not target.is_dir():
        return None
    rmap = load_renderers()
    defaults = settings().get("renderer_defaults", {})
    entries = []
    for p in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if p.name.startswith("."):
            continue
        if p.is_symlink():
            # A link out of the library (or a dangling one) is not listed: nginx
            # would serve its target, and a dangling one made stat() fail the
            # whole listing.
            real = safe_join(data_root, str(p.relative_to(data_root)))
            if real is None or not real.exists():
                continue
        relp = str(p.relative_to(data_root))
        if p.is_dir():
            entries.append({"name": p.name, "dir": True, "path": str(p.relative_to(root))})
            continue
        fk = FILE_KINDS.get(p.suffix.lower(), "other")
        kind = rmap.get(fk, {})
        rends = [{"id": r["id"], "name": r["name"], "url": fill_url(r["url"], rel=relp)}
                 for r in kind.get("renderer", []) if not r.get("requires")]
        if not rends:
            rends = [{"id": "open", "name": "Open", "url": content_url(relp)}]
        try:
            size = p.stat().st_size
        except OSError:
            continue
        entries.append({"name": p.name, "dir": False, "kind": fk, "size": size,
                        "renderers": rends,
                        "default_renderer": defaults.get(fk, kind.get("default", rends[0]["id"]))})
    return {"item": item_id, "title": it.title, "path": rel, "entries": entries}


def file_index() -> list[tuple[str, str, str]]:
    """(lowercased name, display name, /content/ url) for every collection file."""
    def run():
        out = []
        data_root = CFG.upd.data_root.resolve()
        for sub in ("docs", "books", "video", "audio"):
            base = data_root / sub
            if base.exists():
                for p in base.rglob("*"):
                    if p.is_file() and not p.name.startswith("."):
                        rel = p.relative_to(data_root)
                        out.append((p.stem.lower().replace("_", " "), p.stem.replace("_", " "),
                                    content_url(str(rel))))
        return out
    return cached("file_index", 300, run)


# ─────────────────────────────── search & ask ────────────────────────────────

def kiwix_search(q: str, count: int = 15, books: list[str] | None = None) -> list[dict]:
    params = [("pattern", q), ("format", "xml"), ("pageLength", str(count))]
    if books:
        params += [("books.name", b) for b in books]
    else:
        params += [("books.filter.lang", lang) for lang in CFG.search_langs]
    url = f"{CFG.kiwix}/search?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            # kiwix-serve on loopback is trusted; ET never resolves external
            # entities and the expat it links has billion-laughs protection.
            root = ET.fromstring(r.read(8 << 20))  # noqa: S314
    except (urllib.error.URLError, OSError, ET.ParseError, TimeoutError):
        return []
    out = []
    for item in root.iter("item"):
        def get(tag, item=item):
            return (item.findtext(tag) or "").strip()
        book_el = item.find("book")
        link = get("link")
        # Only links into kiwix-serve itself: an absolute URL here would be
        # fetched by /api/ask (SSRF) and shown to browsers as a link
        # (javascript:…), so it is dropped rather than passed through.
        if not link.startswith("/") or link.startswith("//"):
            continue
        out.append({
            "title": get("title"),
            "url": CFG.kiwix_public + link.removeprefix(urllib.parse.urlparse(CFG.kiwix).path),
            "fetch": urllib.parse.urljoin(CFG.kiwix + "/", link),
            "snippet": re.sub(r"<[^>]+>", "", html.unescape(get("description")))[:300],
            "source": (book_el.findtext("title") if book_el is not None else "") or "",
        })
    return out


def search(q: str) -> dict:
    q = q.strip()
    if not q:
        return {"q": q, "articles": [], "files": [], "tools": []}
    terms = q.lower().split()
    files = [{"title": disp, "url": url} for low, disp, url in file_index()
             if all(t in low for t in terms)][:25]
    tools = [t for t in TOOLS if any(term in (t["keywords"] + " " + t["title"].lower()) for term in terms)]
    items = [{"id": i["id"], "title": i["title"], "category": i["category"]}
             for i in library()["items"]
             if i["installed"] and all(t in (i["title"] + " " + i["description"]).lower() for t in terms)]
    return {"q": q, "articles": kiwix_search(q), "files": files, "tools": tools, "collections": items}


class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "table", "sup", "figure"}
    KEEP = {"p", "li", "h1", "h2", "h3", "h4", "dd", "dt", "td", "pre"}

    def __init__(self):
        super().__init__()
        self.depth_skip = 0
        self.in_keep = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth_skip += 1
        elif tag in self.KEEP:
            self.in_keep += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth_skip:
            self.depth_skip -= 1
        elif tag in self.KEEP and self.in_keep:
            self.in_keep -= 1
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.depth_skip and self.in_keep:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", "".join(self.parts))).strip()


def article_text(url: str, limit: int = 2400) -> str:
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            raw = r.read(600_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return ""
    ex = TextExtractor()
    ex.feed(raw)
    return ex.text()[:limit]


def ask_stream(q: str, write, brief: bool = False) -> None:
    """Retrieve from the library, then stream a grounded answer from llama-server.
    brief: a two-sentence answer for the mesh radio (fewer tokens, no citations)."""
    hits = kiwix_search(q, count=6, books=CFG.ask_books or None)[:4]
    sources = []
    for h in hits:
        txt = article_text(h["fetch"])
        if txt:
            sources.append({**h, "text": txt})
    write("sources", [{"n": i + 1, "title": s["title"], "url": s["url"], "source": s["source"]}
                      for i, s in enumerate(sources)])
    if not sources:
        write("token", "I couldn't find anything on the disk about that. Try different words, "
                       "or browse the Medical / Survival sections.")
        write("done", {})
        return
    ctx = "\n\n".join(f"[{i + 1}] {s['title']} ({s['source']})\n{s['text']}" for i, s in enumerate(sources))
    body = json.dumps({
        "messages": [{"role": "system", "content": ASK_SYSTEM + (ASK_BRIEF if brief else "")},
                     {"role": "user", "content": f"Sources:\n{ctx}\n\nQuestion: {q}"}],
        "stream": True, "temperature": 0.2, "max_tokens": 90 if brief else 600,
    }).encode()
    req = urllib.request.Request(CFG.llama + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=CFG.ask_timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content")
                except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
                    continue
                if isinstance(delta, str) and delta:
                    write("token", delta)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        write("token", f"\n\n(The AI model isn't answering — {e}. The sources above still answer "
                       f"most questions; open them directly.)")
    write("done", {})


# ─────────────────────────────── feature plugins ─────────────────────────────
#
# Each file in software/portal/features/*.py is one feature. It may define:
#   FEATURE = {"title": "Supplies", "icon": "🥫", "group": "family" | "community" | "radio" | "tools",
#              "route": "/supplies", "script": "/features/supplies.js", "description": "…",
#              "default_enabled": True}
#   ROUTES  = [{"method": "GET" | "POST", "path": "/api/supplies/items", "handler": fn,
#               "access": "public" | "member" | "admin" | "member_or_admin" | "local" | callable(req, ctx) -> bool,
#               "prefix": False,        # True: path is a prefix ("/api/vault/file/")
#               "raw": False,           # True (POST): body not parsed; the client sends the header
#                                       #   X-Badger-Upload: 1 (CSRF-safe) and the handler reads req.rfile
#               "max_bytes": 25_000_000}]   # raw bodies only
#   setup(ctx)  called once at start
# A handler is fn(req, ctx, args) with args = {"qs", "data", "path", "member", "admin", "ip"} and returns
# a dict/list (200 JSON), (status, obj) or (status, obj, headers), or None when it wrote the response itself
# (ctx.send_bytes / ctx.send_file). A feature that fails to load is skipped; the rest of the portal runs.
# A feature is turned off with [features] <id> = false in prepper.toml.

FEATURES: list[dict] = []
FEATURE_LIMIT = 25_000_000


def human_mb(n: int) -> str:
    return f"{n / 1_000_000:.0f} MB" if n >= 1_000_000 else f"{max(1, n // 1000)} kB"


def _feature_send_bytes(req, data: bytes, ctype: str, headers: dict | None = None, status: int = 200) -> None:
    req.send_response(status)
    req.send_header("Content-Type", ctype)
    req.send_header("Content-Length", str(len(data)))
    req.send_header("X-Content-Type-Options", "nosniff")
    req.send_header("Cache-Control", "no-store")
    if ctype.split(";")[0].strip() in SANDBOX_TYPES:      # never run uploaded HTML/SVG on the launcher's origin
        req.send_header("Content-Security-Policy", "sandbox")
    for k, v in (headers or {}).items():
        req.send_header(k, v)
    req.end_headers()
    if req.command != "HEAD":
        req.wfile.write(data)


def _feature_send_file(req, path: Path, ctype: str, name: str | None = None, inline: bool = True) -> None:
    data = Path(path).read_bytes()
    disp = None
    if name:
        safe = re.sub(r'[^A-Za-z0-9._ -]', "_", name)[:120] or "file"
        disp = f'{"inline" if inline else "attachment"}; filename="{safe}"'
    _feature_send_bytes(req, data, ctype, {"Content-Disposition": disp} if disp else None)


def _feature_mesh_send(text: str, board: str = "community", kind: str | None = None, dedupe: str | None = None,
                       hop_limit: int | None = None) -> bool:
    """Queue a short message for the mesh radio (the bridge sends it on that board's channel).
    Optional (docs/MESH.md): kind = "alert" | "danger" | "family" | "reply" | "community" | "auto" (priority,
    default: the board), dedupe = a key other Badgers compute the same way (one Badger on the mesh sends
    it), hop_limit = 1–7. Old calls mesh_send(text, board) work as before."""
    text = " ".join(str(text).split())
    if not text or board not in ("community", "family"):
        return False
    if kind is not None and kind not in MESH_KINDS:
        return False
    while len(text.encode()) > 200:
        text = text[:-2] + "…"
    with _board_lock:
        return outbox_append(outbox_record(text, board, kind, dedupe, hop_limit))


def _feature_data_dir(fid: str) -> Path:
    base = REAL["features_data"] if DECOY and fid in DECOY_PASSTHROUGH and "features_data" in REAL else CFG.features_data
    d = base / re.sub(r"[^a-z0-9_-]", "", fid.lower())
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o750)
    except OSError:
        pass
    return d


def feature_ctx(fid: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=fid,
        cfg=lambda section=None: (CFG.raw.get(section or fid, {}) or {}),
        raw_config=lambda: CFG.raw,
        data_dir=lambda: _feature_data_dir(fid),
        read_json=read_json, atomic_write=private_write, members=members_db,
        board_post=board_post, mesh_send=_feature_mesh_send, alerts=active_alerts, status=status,
        send_bytes=_feature_send_bytes, send_file=_feature_send_file, ctl=ctl, kiwix_search=kiwix_search,
        ask_stream=ask_stream,   # ask_stream(q, write(event, data), brief=False): the library-grounded AI answer
        log=lambda msg: print(f"[{fid}] {msg}", file=sys.stderr),
        # the security posture and friends (features/posture.py, circles.py, reveal.py)
        posture=posture_state, settings=settings, update_settings=update_settings, config=lambda: CFG,
        panel_lock_on=lambda: panel_lock_on(), services_up=lambda: services_up(), features=lambda: features_list(),
        sealed=sealed_store,     # sealed records (software/common/sealed.py): seal while locked, open when unlocked
        feature=_feature_handle,  # another loaded feature: .mod, .ctx (None when it is switched off)
    )


def _feature_handle(fid: str) -> types.SimpleNamespace | None:
    """Another loaded feature plugin (e.g. missing → circles' sharing path): its module and its own ctx."""
    for f in FEATURES:
        if f["id"] == fid:
            return types.SimpleNamespace(mod=f["mod"], ctx=f["ctx"])
    return None


def load_features(folder: Path | None = None) -> None:
    FEATURES.clear()
    enabled = CFG.raw.get("features", {}) or {}
    for f in sorted((folder or HERE / "features").glob("*.py")):
        if f.name.startswith(("_", ".")):          # "_" helpers; "." dot-files such as macOS ._foo.py
            continue
        fid = f.stem
        try:
            spec = importlib.util.spec_from_file_location(f"badger_feature_{fid}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            info = dict(getattr(mod, "FEATURE", {}) or {})
            if enabled.get(fid, info.get("default_enabled", True)) is False:
                continue
            ctx = feature_ctx(fid)
            if hasattr(mod, "setup"):
                mod.setup(ctx)
            routes = [r for r in (getattr(mod, "ROUTES", []) or []) if r.get("path", "").startswith("/api/")]
            FEATURES.append({"id": fid, "mod": mod, "info": info, "routes": routes, "ctx": ctx})
        except Exception as e:  # noqa: BLE001 — one broken feature must not take the portal down
            print(f"[portal] feature {fid} not loaded: {type(e).__name__}: {e}", file=sys.stderr)


def match_feature(method: str, path: str):
    for feat in FEATURES:
        for r in feat["routes"]:
            if r.get("method", "GET") != method:
                continue
            if path == r["path"] or (r.get("prefix") and path.startswith(r["path"])):
                return feat, r
    return None


def features_list(visitor_view: bool = False) -> list[dict]:
    out = []
    for feat in FEATURES:
        i = feat["info"]
        if visitor_view and i.get("route") and visitor_refusal(feat, {"access": "public"}, {}):
            continue                                     # the posture hides it from visitors: no tile
        out.append({"id": feat["id"], "title": str(i.get("title", feat["id"])), "icon": str(i.get("icon", "•")),
                    "group": str(i.get("group", "tools")), "route": i.get("route"), "script": i.get("script"),
                    "access": i.get("access", "public"), "description": str(i.get("description", ""))})
    return out


# ─────────────────────────────── HTTP handler ────────────────────────────────

class BadRequest(Exception):
    def __init__(self, status: int, message: str, close: bool = False):
        super().__init__(message)
        self.status, self.message, self.close = status, message, close


_ask_slots: threading.BoundedSemaphore | None = None
_ask_slots_lock = threading.Lock()


def ask_slots() -> threading.BoundedSemaphore:
    """llama-server answers one question at a time; more than a couple waiting
    only ties up threads for minutes each. Created lazily from the config."""
    global _ask_slots
    with _ask_slots_lock:
        if _ask_slots is None:
            _ask_slots = threading.BoundedSemaphore(CFG.ask_concurrency)
        return _ask_slots


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
MEMBER_COOKIE, ADMIN_COOKIE = "bh_member", "bh_admin"
MEMBER_COOKIE_AGE, ADMIN_TTL = 10 * 365 * 86400, 12 * 3600
ROTATE_REQUEST_TTL = 120              # a mesh key request older than this is never acted on
SANDBOX_TYPES = ("text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml")


def host_of(hostport: str) -> str:
    h = hostport.strip().lower()
    return h.rsplit(":", 1)[0] if h.count(":") == 1 or (h.startswith("[") and "]:" in h) else h


def parse_range(header: str, size: int):
    """One `bytes=` range → (start, end) inclusive; None if unsatisfiable;
    "ignore" for anything we don't serve as a range (bad syntax, multi-range)."""
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d{0,18})\s*-\s*(\d{0,18})\s*", header or "")
    if not m or (not m.group(1) and not m.group(2)):
        return "ignore"
    a, b = m.group(1), m.group(2)
    if a:
        start = int(a)
        end = int(b) if b else size - 1
        if b and end < start:
            return "ignore"
        if start >= size:
            return None
        return start, min(end, size - 1)
    n = int(b)                                        # "-n": the last n bytes
    if n == 0 or size == 0:
        return None
    return max(0, size - n), size - 1


class BodyTimeout(TimeoutError):
    """The whole request body took longer than its deadline."""


BODY_DEADLINE = 120.0          # seconds for a whole request body, however slowly it trickles in …
BODY_RATE = 100_000            # … plus 1 s per 100 kB past the first MB (a 25 MB upload on weak Wi-Fi)
DRAIN_MAX = 32 * 1024 * 1024   # a refused body up to this size is read and thrown away …
DRAIN_SECONDS = 30.0           # … for at most this long, so nginx gets our answer instead of a reset (502)


class _BodyReader:
    """Stands in for the request's rfile while a POST is handled. It counts the bytes taken (a refusal
    then knows how much is left to drain) and holds the whole body to one deadline: the socket timeout
    alone starts again on every byte, so a phone sending 1 byte every 5 s kept a thread for ever.
    Handlers just call .read(n) as before; the next request on the connection reads the real rfile."""

    def __init__(self, raw, sock, length: int):
        self.raw, self.sock, self.taken = raw, sock, 0
        self.deadline = time.monotonic() + BODY_DEADLINE + max(0, length - 1_000_000) / BODY_RATE

    def _arm(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise BodyTimeout("request body took too long")
        try:
            if self.sock is not None:
                self.sock.settimeout(min(Handler.timeout, left))
        except OSError:
            pass

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            raise ValueError("read the body with an exact length")
        out = []
        while n > 0:
            self._arm()
            chunk = self.raw.read1(min(n, 1 << 16))
            if not chunk:
                break
            out.append(chunk)
            self.taken += len(chunk)
            n -= len(chunk)
        try:
            if self.sock is not None:
                self.sock.settimeout(Handler.timeout)   # replies (a streamed AI answer) get the usual timeout
        except OSError:
            pass
        return b"".join(out)

    def readline(self, limit: int = -1) -> bytes:       # (not used for bodies; kept file-like)
        self._arm()
        line = self.raw.readline(limit)
        self.taken += len(line)
        return line

    def __getattr__(self, name):
        return getattr(self.raw, name)


class PortalServer(ThreadingHTTPServer):
    """The portal's HTTP server: a deep accept queue and a bounded number of worker threads.

    The stock server listens with a backlog of 5 (a burst of phones got resets → nginx 502s) and starts
    one thread per connection without limit (a flood of slow clients grew to ~500 threads). Here at most
    max_threads connections are served at once; a connection that finds every slot busy for
    slot_wait seconds gets a short 503 instead of a thread."""
    request_queue_size = 256
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, max_threads: int = 128, slot_wait: float = 5.0):
        self.max_threads, self.slot_wait = max(4, int(max_threads)), slot_wait
        self._slots = threading.BoundedSemaphore(self.max_threads)
        self._quiet_until, self._quiet_count = 0.0, 0
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(timeout=self.slot_wait):
            self._busy(request)
            return
        try:
            threading.Thread(target=self._serve, args=(request, client_address), daemon=True).start()
        except RuntimeError:                           # can't start a thread (memory): refuse, don't die
            self._slots.release()
            self._busy(request)

    def _serve(self, request, client_address):
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _busy(self, request) -> None:
        body = b'{"error": "The Badger is very busy right now. Try again in a few seconds."}'
        try:
            request.settimeout(1.0)
            request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                            b"Retry-After: 5\r\nConnection: close\r\nContent-Length: " +
                            str(len(body)).encode() + b"\r\n\r\n" + body)
        except OSError:
            pass
        self.shutdown_request(request)
        self.note("all worker threads busy: refused a connection")

    def note(self, msg: str) -> None:
        """One log line for a burst of the same thing, not one per connection (a flood would fill the journal)."""
        now = time.monotonic()
        if now < self._quiet_until:
            self._quiet_count += 1
            return
        more = f" (+{self._quiet_count} more in the last minute)" if self._quiet_count else ""
        self._quiet_until, self._quiet_count = now + 60, 0
        sys.stderr.write(f"[portal] {msg}{more}\n")

    def handle_error(self, request, client_address):
        """A phone that hangs up or stalls mid-request is normal on Wi-Fi: one quiet line, no traceback."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)) or (
                isinstance(exc, OSError) and exc.errno in (errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED,
                                                           errno.ENOTCONN, errno.ETIMEDOUT)):
            self.note(f"client went away: {type(exc).__name__}")
            return
        super().handle_error(request, client_address)


class Handler(SimpleHTTPRequestHandler):
    server_version = "PrepperPortal/1.0"
    protocol_version = "HTTP/1.1"
    # Socket timeout: a client that opens a connection and then trickles (or
    # never sends) its request is dropped instead of holding a thread for ever.
    timeout = 60

    def log_message(self, fmt, *args):
        if os.environ.get("PORTAL_QUIET") != "1":
            sys.stderr.write("[portal] " + fmt % args + "\n")

    # — plumbing —
    def send_response(self, code, message=None):
        self._responded = True
        super().send_response(code, message)

    def send_json(self, obj, status=200, headers=None):
        body = dumps_safe(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.command in ("GET", "HEAD"):
            # Public read-only data may be read cross-origin; replies to POSTs
            # (a login token, admin output) never are.
            self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def guarded(self, fn, body: bool = False):
        """Every request ends with a response: an unexpected error becomes a JSON 500
        instead of a dropped connection. body=True (POST): the body is read through a
        _BodyReader (one deadline for the whole body), and a body we refuse or leave unread
        is drained before the connection closes — closing on unread bytes makes the kernel
        send a reset, and nginx then shows the phone a 502 instead of our answer."""
        self._responded = False
        try:
            guard_mesh_state()                         # radio state on persistent storage → sealed (cheap stats)
        except Exception:  # noqa: BLE001
            pass
        try:
            decoy_tick()                               # a duress: the made-up household for everyone (cheap stat)
        except Exception as e:  # noqa: BLE001
            print(f"[portal] decoy check failed: {type(e).__name__}", file=sys.stderr)
        raw_rfile, self._body = self.rfile, None
        if body:
            self._body = self.rfile = _BodyReader(raw_rfile, getattr(self, "connection", None), self._declared_length())
        try:
            try:
                fn()
            except BadRequest as e:
                if e.close:
                    self.close_connection = True
                if not self._responded:
                    self.drain_body()
                    self.send_json({"error": e.message}, e.status)
            except TimeoutError:
                # The body stalled or trickled past its deadline: say so if we can, and hang up.
                self.close_connection = True
                if not self._responded:
                    self.send_json({"error": "The upload was too slow and was stopped. Check the Wi-Fi "
                                             "signal and try again."}, 408)
            except (ConnectionError, BrokenPipeError):
                raise
            except OSError as e:
                if not is_storage_full(e):
                    raise
                sys.stderr.write(f"[portal] storage full handling {self.command} {self.path.split('?')[0]}\n")
                self.close_connection = True
                if not self._responded:
                    self.drain_body()
                    self.send_json({"error": STORAGE_FULL_MSG, "storage_full": True}, 507)
            if body and self._body_left() > 0:
                # The handler answered without reading the whole body (an early refusal): the rest
                # must never be parsed as the next request, and must not turn into a reset either.
                self.close_connection = True
                self.drain_body(seconds=5.0)
        except (ConnectionError, BrokenPipeError):
            self.close_connection = True                 # the phone went away: nothing to answer
        except Exception:  # noqa: BLE001
            import traceback
            sys.stderr.write("[portal] error handling %s %s\n%s" % (self.command, self.path, traceback.format_exc()))
            self.close_connection = True
            if not self._responded:
                try:
                    self.drain_body(seconds=5.0)
                    # No exception text: it can name paths, users and data (it is in the journal for the admin).
                    self.send_json({"error": "Something went wrong on the Badger. Try again; if it keeps "
                                             "happening, the admin can look at the Badger's log."}, 500)
                except OSError:
                    pass
        finally:
            if body:
                self.rfile = raw_rfile
                try:
                    if getattr(self, "connection", None) is not None:
                        self.connection.settimeout(self.timeout)
                except OSError:
                    pass

    def _declared_length(self) -> int:
        """Content-Length if it is one clean number (body_length() refuses everything else), else 0."""
        lens = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") or len(set(v.strip() for v in lens)) != 1:
            return 0
        v = lens[0].strip()
        return int(v) if re.fullmatch(r"[0-9]{1,12}", v) else 0

    def _body_left(self) -> int:
        b = getattr(self, "_body", None)
        return max(0, self._declared_length() - b.taken) if b is not None else 0

    def drain_body(self, seconds: float = DRAIN_SECONDS) -> None:
        """Read and throw away what is left of a body we are refusing (up to DRAIN_MAX, for at most
        `seconds`), so the answer reaches the phone: hanging up on unread bytes resets the connection
        and nginx turns that into a 502 (or a cut-off reply) instead of our friendly message."""
        b = getattr(self, "_body", None)
        left = self._body_left()
        if b is None or left <= 0 or left > DRAIN_MAX:
            return
        end, sock = time.monotonic() + seconds, getattr(self, "connection", None)
        try:
            while left > 0:
                t = end - time.monotonic()
                if t <= 0:
                    break
                if sock is not None:
                    sock.settimeout(min(5.0, t))
                chunk = b.raw.read1(min(left, 1 << 16))
                if not chunk:
                    break
                b.taken += len(chunk)
                left -= len(chunk)
        except OSError:
            self.close_connection = True

    def body_length(self) -> int:
        """Declared body size; anything we can't frame exactly closes the connection
        (the unread bytes would otherwise be parsed as the next request)."""
        if self.headers.get("Transfer-Encoding"):
            raise BadRequest(411, "chunked request bodies are not supported; send Content-Length", close=True)
        lens = self.headers.get_all("Content-Length") or []
        if len(set(v.strip() for v in lens)) > 1:
            raise BadRequest(400, "conflicting Content-Length headers", close=True)
        raw_len = (lens[0] if lens else "0").strip()
        if not re.fullmatch(r"[0-9]{1,12}", raw_len):        # int() would take "+5", "5_0", "٥"
            raise BadRequest(400, "bad Content-Length", close=True)
        return int(raw_len)

    def body_json(self) -> dict:
        n = self.body_length()
        if n > 1_000_000:
            # guarded() drains what was sent (bounded) before answering, so the phone sees this, not a 502
            raise BadRequest(413, "That's more than the Badger takes in one message (1 MB). Make it shorter.",
                             close=True)
        raw = self.rfile.read(n) if n else b""
        if len(raw) < n:
            raise BadRequest(400, "request body ended early", close=True)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):    # bad JSON, bad UTF-8, 5000-digit ints, [[[[…]]]]
            raise BadRequest(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise BadRequest(400, "body must be a JSON object")
        return data

    def post_allowed(self, raw: bool = False) -> None:
        """CSRF guard for every POST, checked before the body is read.

        1. JSON only. A page on another origin can send text/plain or form
           posts without asking; an application/json one needs a CORS
           preflight, and do_OPTIONS never grants it.
        2. A browser that says the request is cross-site (Sec-Fetch-Site) is refused.
        3. A browser Origin must name the host the request was sent to (or one
           in [portal] allowed_origins). Behind nginx the Host header is the
           upstream address (127.0.0.1:8000) unless nginx forwards $host; then
           the check can't be made here and 1–2 carry it."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if raw:
            # uploads: a custom header, which no other origin can send without a preflight we never grant
            if (self.headers.get("X-Badger-Upload") or "").strip() != "1":
                raise BadRequest(415, "uploads need the X-Badger-Upload: 1 header", close=True)
        elif ctype != "application/json":
            raise BadRequest(415, "send the request body as application/json", close=True)
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            raise BadRequest(403, "cross-site request refused", close=True)
        origin = self.headers.get("Origin")
        if origin is None:
            return                              # not sent by a browser page (curl, the builder, the panel)
        o = urllib.parse.urlsplit(origin.strip())
        if o.scheme not in ("http", "https") or not o.netloc:
            raise BadRequest(403, "requests from this origin are refused", close=True)   # "null", file:, …
        onet, host = o.netloc.lower(), (self.headers.get("Host") or "").strip().lower()
        if onet == host or onet in CFG.allowed_origins or host_of(onet) in CFG.allowed_origins:
            return
        if not CFG.standalone and host_of(host) in LOOPBACK_HOSTS:
            return                              # proxied without Host: can't compare (see above)
        raise BadRequest(403, "cross-origin request refused", close=True)

    def client_ip(self) -> str:
        # Only nginx (on loopback) gets to say who the client is.
        if self.client_address[0] in ("127.0.0.1", "::1") and self.headers.get("X-Real-IP"):
            return self.headers["X-Real-IP"].strip()
        return self.client_address[0]

    def run_feature(self, fr, path: str, qs: dict, data):
        feat, route = fr
        access, ctx = route.get("access", "admin"), feat["ctx"]
        me = self.member()
        admin = self.is_admin()
        ok = {"public": True, "member": bool(me), "admin": admin, "member_or_admin": bool(me) or admin,
              "local": self.local_console()}.get(access) if isinstance(access, str) else bool(access(self, ctx))
        why = visitor_refusal(feat, route, self.headers) if ok and not (me or admin or self.local_console()) else None
        if why:                                          # the security posture keeps visitors out of this page
            if route.get("raw"):
                self.close_connection = True
                self.drain_body()
            return self.send_json({"error": why, "posture": True}, 403)
        if not ok:
            if route.get("raw"):
                # The (unread) upload body would otherwise be parsed as the next request on this
                # keep-alive connection: drain it (bounded) and hang up after the refusal.
                self.close_connection = True
                self.drain_body()
            return self.send_json({"error": "family members only" if access in ("member", "member_or_admin")
                                   else "not allowed"}, 401 if access != "local" else 403)
        if route.get("raw"):
            n = self.headers.get("Content-Length")
            if not (n and n.isdigit()):
                raise BadRequest(411, "Content-Length required", close=True)
            limit = int(route.get("max_bytes", FEATURE_LIMIT))
            if int(n) > limit:
                raise BadRequest(413, f"That file is too big for the Badger (the limit is {human_mb(limit)}). "
                                      "Try a smaller one.", close=True)
        res = route["handler"](self, ctx, {"qs": qs or {}, "data": data, "path": path, "member": me,
                                           "admin": admin, "ip": self.client_ip()})
        if res is None:
            return None
        if isinstance(res, tuple):
            return self.send_json(res[1], res[0], res[2] if len(res) > 2 else None)
        return self.send_json(res)

    def local_console(self) -> bool:
        """The Badger's own front panel — the one check behind /api/local/* and the feature
        framework's "local" access level. All of these must hold:
          * straight to this port on loopback, not relayed by nginx (it always adds X-Real-IP),
          * not from a browser page (no Origin; Sec-Fetch-Site absent or "none"),
          * the panel's key in X-Badger-Panel (only root can read it: see panel_key_ok).
        Loopback alone proved nothing: any local process, or anything that can make a service
        fetch a localhost URL, looked like the panel."""
        h = self.headers
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        if h.get("X-Real-IP") or h.get("X-Forwarded-For") or h.get("Origin") is not None:
            return False
        if (h.get("Sec-Fetch-Site") or "none").strip().lower() != "none":
            return False
        return panel_key_ok(h.get(PANEL_HEADER))

    def precise_location_ok(self) -> bool:
        return bool(self.member() or self.is_admin() or self.local_console())

    def secure_transport(self) -> bool:
        """Is this request safe from eavesdroppers on the Wi-Fi? True over HTTPS (nginx passes
        X-Forwarded-Proto) and for requests made on the Badger itself (loopback, not via nginx).
        Plain HTTP on the hotspot is not: anyone who knows (or knew) the Wi-Fi password can read it,
        so new secrets (Wi-Fi password, family channel key) are never sent that way."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        if not self.headers.get("X-Real-IP") and not self.headers.get("X-Forwarded-For"):
            return True
        return (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https"

    def cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        if not raw or len(raw) > 8192:
            return ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                v = v.strip().strip('"')
                return v if re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", v) else ""
        return ""

    def member_key(self) -> str:
        """The device key: the X-Member-Token header (feature plugins, tools, tests) or the
        launcher's HttpOnly cookie (which no script on the page can read)."""
        return (self.headers.get("X-Member-Token") or "").strip() or self.cookie(MEMBER_COOKIE)

    def member_status(self) -> tuple[str, dict | None]:
        """("ok", member) | ("none", None): no key sent | ("unknown", None): revoked or never valid |
        ("unavailable", None): the member list can't be read right now (keep the key!)."""
        if not hasattr(self, "_member_cache"):
            tok = self.member_key()
            self._member_cache = members_db().auth_status(tok) if tok else ("none", None)
            if self._member_cache[0] == "ok":
                note_checkin()                   # (dead-man switch, only while armed)
        return self._member_cache

    def member(self) -> dict | None:
        """The family member this device's key belongs to, if any (removed members get None)."""
        return self.member_status()[1]

    def admin_token(self) -> str:
        return (self.headers.get("X-Admin-Token") or "").strip() or self.cookie(ADMIN_COOKIE)

    def is_admin(self) -> bool:
        prune_tokens()
        tok = self.admin_token()
        with _auth_lock:
            exp = TOKENS.get(tok) if tok else None
        return bool(exp and exp > time.time())

    def set_cookie(self, name: str, value: str, max_age: int) -> str:
        """HttpOnly (no script can read it — not even a same-origin app under /ai/ or /learn/),
        SameSite=Strict (never sent by another site's page), Path=/api/ (only the API sees it).
        Not "Secure": most families use plain http://badger.lan, and a cookie set over HTTPS would
        then silently vanish (see docs/SECURITY.md)."""
        return f"{name}={value}; Path=/api/; Max-Age={int(max_age)}; HttpOnly; SameSite=Strict"

    def member_refusal(self, what: str = "family members only"):
        """401 for no/unknown key ("key": "unknown" tells the launcher to forget it), 503 when the
        member list can't be read (then nobody should forget anything)."""
        st = self.member_status()[0]
        if st == "unavailable":
            return self.send_json({"error": "The family member list can't be read right now. "
                                            "Try again later, or ask the admin to check it.", "member": None,
                                   "key": "unavailable"}, 503)
        if st == "pending":                              # keep the key: it starts working once approved
            return self.send_json({"error": "This device is waiting to be approved on one of your other devices "
                                            "(or at the Badger's own screen).", "member": None, "key": "pending"}, 401)
        return self.send_json({"error": what, "member": None, "key": "unknown" if st == "unknown" else None},
                              401, {"Set-Cookie": self.set_cookie(MEMBER_COOKIE, "", 0)} if st == "unknown" else None)

    def translate_path(self, path):
        """Standalone mode only: /content/* → data_root, everything else → static."""
        path = urllib.parse.unquote(urllib.parse.urlparse(path).path)
        if path.startswith("/content/"):
            p = safe_join(CFG.upd.data_root, path.removeprefix("/content/"))
        else:
            p = safe_join(CFG.static, path if path != "/" else "index.html")
        return str(p) if p else str(CFG.static / "__nope__")

    def serve_content(self) -> None:
        """Standalone /content/ files with single-range support (PMTiles maps,
        video seeking and resumed downloads all need it; nginx does this on the Badger)."""
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            return super().do_GET() if self.command == "GET" else super().do_HEAD()
        st = path.stat()
        size = st.st_size
        etag = f'"{st.st_mtime_ns:x}-{size:x}"'
        last_mod = self.date_time_string(int(st.st_mtime))
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if rng and (not if_range or if_range.strip() in (etag, last_mod)):
            r = parse_range(rng, size)
            if r is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if r != "ignore":
                start, end = r
                status = 206
        length = max(0, end - start + 1)
        ctype = self.guess_type(str(path))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", last_mod)
        self.send_header("ETag", etag)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.split(";")[0].strip().lower() in SANDBOX_TYPES:
            # Library HTML/SVG (from a USB stick, a bundle, a doc set) runs as an
            # opaque origin: its scripts can't read the launcher's admin token
            # or call /api/ as the launcher. (Not applied to PDFs: Chrome
            # refuses to show a PDF in a sandboxed document.)
            self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-popups allow-forms allow-downloads")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # — routes —
    def do_OPTIONS(self):
        # CORS preflight: other origins may GET public data (with Range, for map
        # tiles); they are never allowed to POST JSON or send an admin token.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def no_body_expected(self) -> None:
        """GET/HEAD with a body: we never read it, so it must not be parsed as
        the next request on this connection (request smuggling)."""
        if self.headers.get("Transfer-Encoding") or (self.headers.get("Content-Length") or "0").strip() not in ("", "0"):
            self.close_connection = True

    def do_GET(self):
        self.no_body_expected()
        self.guarded(self.route_get)

    def route_get(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        one = lambda k, d="": (qs.get(k) or [d])[0]
        if u.path == "/api/status":
            st = status()
            if not self.precise_location_ok():
                st = visitor.status_view(st)          # no power, occupancy or uplink; position ~11 km
            return self.send_json(st)
        if u.path == "/api/library":
            return self.send_json(library())
        if u.path == "/api/files":
            res = list_files(one("item"), one("path"))
            return self.send_json(res) if res else self.send_json({"error": "not found"}, 404)
        if u.path == "/api/search":
            return self.send_json(search(one("q")))
        if u.path == "/api/content/personal":
            return self.send_json(personal_view(self.is_admin()))
        if u.path == "/api/updates":
            return self.send_json({
                "report": read_json(CFG.upd.report_dir / "last-check.json", None),
                "build": read_json(CFG.build_file, None),
                "history": (read_json(CFG.upd.state_file, {}) or {}).get("history", [])[-30:],
            })
        if u.path == "/api/settings":
            return self.send_json({"settings": settings(), "renderers": {
                k: {"label": v.get("label", k), "default": v.get("default"),
                    "options": [{"id": r["id"], "name": r["name"]} for r in v.get("renderer", [])]}
                for k, v in load_renderers().items()}})
        if u.path == "/api/board":
            board, me = one("board", "community"), self.member()
            if board not in BOARDS:
                return self.send_json({"error": "no such board"}, 404)
            if board == "family" and not me:
                return self.member_refusal()
            family = bool(me) or self.is_admin()
            if not family and posture_value("visitor_community") == "members" and not self.local_console():
                return self.send_json({"error": LOCKDOWN_MSG, "posture": True}, 403)
            sealed_info: dict = {}
            return self.send_json({"messages": board_read(board=board, for_member=bool(me), info=sealed_info),
                                   "sealed": sealed_info or None,      # family board: locked? posts hidden?
                                   "mesh": services_up().get("mesh", False), "board": board,
                                   "member": {"name": me["name"]} if me else None,
                                   # visitors' radio requests, for the people who may send them
                                   "mesh_pending": mesh_pending_list() if family and board == "community" else None,
                                   "mesh_needs_ok": not family and visitor_mesh_mode() == "approve",
                                   "mesh_visitors": family or visitor_mesh_mode() == "approve"})
        if u.path == "/api/members/me":
            me = self.member()
            if not me:
                st = self.member_status()[0]
                pend = members_db().pending_info(self.member_key()) if st == "pending" else None
                return self.send_json({"member": None, "admin": self.is_admin(), "pending": pend,
                                       "key": {"unknown": "unknown", "unavailable": "unavailable",
                                               "pending": "pending"}.get(st)},
                                      503 if st == "unavailable" else 401)
            check_radio_pairings()
            db = members_db()
            mine = [r for r in db.list()["members"] if r["id"] == me["id"]]
            return self.send_json({"member": {"name": me["name"], "radios": mine[0]["radios"] if mine else me["radios"],
                                              "pending_radios": db.pending_radios(me["id"])},
                                   "admin": self.is_admin(),
                                   # devices someone added to this member: this device is asked to approve
                                   "approvals": [{"device": x["device"], "label": x["label"], "created": x["created"]}
                                                 for x in db.pending_devices(me["id"])]})
        if u.path == "/api/members/radio-link":
            if not self.member():
                return self.member_refusal()
            return self.send_json(mesh_secret(self.secure_transport()))
        if u.path == "/api/local/devices":
            if not self.local_console():
                return self.send_json({"error": "only from the Badger's own screen"}, 403)
            return self.send_json({"pending": members_db().pending_devices()})
        if u.path == "/api/time":
            try:
                # visitors: everything (location, solar time, sun events) from the rounded position,
                # whether it came from the GPS or from [time] latitude/longitude
                return self.send_json(nu_time_now(qs, coarse=not self.precise_location_ok()))
            except (ValueError, KeyError, TypeError, OverflowError) as e:
                return self.send_json({"error": str(e)[:300]}, 400)
        if u.path == "/api/alerts":
            return self.send_json({"alerts": active_alerts()})
        if u.path == "/api/mesh/nodes":
            if not (self.member() or self.is_admin()):          # where the family's radios are: family only
                return self.send_json({"error": "family members only"}, 401)
            return self.send_json(mesh_nodes())
        if u.path == "/api/usb":
            # USB sticks are the family's own files (photos, a vault backup, a supplies list): members and
            # the admin only. A visitor gets an empty list, not an error, so the page simply shows nothing.
            if not (self.member() or self.is_admin()):
                return self.send_json([])
            return self.send_json(usb_list())
        if u.path in ("/api/usb/ls", "/api/usb/file"):
            if not (self.member() or self.is_admin()):
                return self.send_json({"error": "USB drives are for family members"}, 401)
            target = usb_resolve(one("drive"), one("path"))
            if target is None:
                return self.send_json({"error": "not found"}, 404)
            if u.path == "/api/usb/ls":
                listing = usb_listing(one("drive"), target)
                return self.send_json(listing) if listing else self.send_json({"error": "not found"}, 404)
            return self.send_usb_file(target)
        if u.path == "/api/sealed":
            return self.send_json(*sealed_api.get(self, sealed_store())[::-1])
        if u.path == "/api/features":
            return self.send_json({"features": features_list(
                visitor_view=not (self.member() or self.is_admin() or self.local_console()))})
        fr = match_feature("GET", u.path)
        if fr:
            return self.run_feature(fr, u.path, qs, None)
        if u.path.startswith("/api/"):
            return self.send_json({"error": "no such endpoint"}, 404)
        if CFG.standalone:
            if u.path.startswith("/content/"):
                return self.serve_content()
            return super().do_GET()
        return self.send_json({"error": "static files are served by nginx"}, 404)

    def do_HEAD(self):
        self.no_body_expected()

        def run():
            if CFG.standalone and not self.path.startswith("/api/"):
                if self.path.startswith("/content/"):
                    return self.serve_content()
                return super(Handler, self).do_HEAD()
            self.send_response(405); self.send_header("Content-Length", "0"); self.end_headers()
        self.guarded(run)

    def do_POST(self):
        self.guarded(self.route_post, body=True)

    def route_post(self):
        u = urllib.parse.urlparse(self.path)
        fr = match_feature("POST", u.path)
        if fr and fr[1].get("raw"):
            self.post_allowed(raw=True)
            return self.run_feature(fr, u.path, urllib.parse.parse_qs(u.query), None)
        self.post_allowed()
        data = self.body_json()
        if fr:
            return self.run_feature(fr, u.path, urllib.parse.parse_qs(u.query), data)
        if u.path == "/api/ask":
            return self.handle_ask(str(data.get("q", ""))[:500], data.get("brief") is True)
        if u.path == "/api/board":
            board, me = data.get("board") or "community", self.member()
            board = board if isinstance(board, str) else "?"
            if board == "family" and not me:
                return self.member_refusal("the family board is for family members")
            if board == "community" and not (me or self.is_admin()) and posture_value("visitor_community") == "members":
                return self.send_json({"ok": False, "message": LOCKDOWN_MSG, "posture": True}, 403)
            ok, msg = board_post(self.client_ip(), str(data.get("name", "")), str(data.get("text", "")),
                                 data.get("mesh") is True, board, me, approved=self.is_admin())
            return self.send_json({"ok": ok, "message": msg}, 200 if ok else (507 if msg == STORAGE_FULL_MSG else 400))
        if u.path == "/api/board/mesh":
            # a family member (or the admin) sends — or turns down — a visitor's "send over mesh"
            me = self.member()
            if not (me or self.is_admin()):
                return self.send_json({"error": "family members only"}, 401)
            pid = data.get("id")
            if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,20}", pid) or \
                    not isinstance(data.get("send"), bool):
                return self.send_json({"error": "bad request"}, 400)
            st, msg = mesh_pending_decide(pid, data["send"], me["name"] if me else "admin")
            return self.send_json({"ok": st == 200, "message": msg}, st)
        if u.path == "/api/members/join":
            try:
                st, member, key = members_db().join(data.get("code", ""), str(data.get("name", "")),
                                                     str(data.get("device", "")), self.client_ip())
            except members_mod.StoreError as e:
                return self.send_json({"ok": False, "error": str(e)}, 503)
            if st == "ok":
                # The key goes into an HttpOnly cookie (the launcher never stores it where scripts can
                # read it); it is also in the reply, once, for feature tools and older launchers.
                res = {"ok": True, "key": key, "member": {"name": member["name"]}}
                if member.get("pending"):
                    res.update(pending=True, message=f"Almost done: {member['name']} must approve this device on one "
                               "of their other devices (a question appears there when they open the Badger app), or "
                               "someone approves it at the Badger's own screen (knob → Approve device). Until then it "
                               "can't see family pages.")
                return self.send_json(res, headers={"Set-Cookie": self.set_cookie(MEMBER_COOKIE, key, MEMBER_COOKIE_AGE)})
            code = {"locked": 429, "name": 400, "full": 409, "taken": 409}.get(st, 403)
            return self.send_json({"ok": False, "error": key}, code, {"Retry-After": "600"} if st == "locked" else None)
        if u.path == "/api/members/adopt":
            # An older launcher kept the key in localStorage: it sends it once in the header, gets the
            # HttpOnly cookie, and deletes its copy only when told the key is "unknown".
            st, me = self.member_status()
            if st != "ok":
                return self.member_refusal()
            return self.send_json({"ok": True, "member": {"name": me["name"]}},
                                  headers={"Set-Cookie": self.set_cookie(MEMBER_COOKIE, self.member_key(), MEMBER_COOKIE_AGE)})
        if u.path == "/api/local/invite":
            if not self.local_console():
                return self.send_json({"error": "only from the Badger's own screen"}, 403)
            try:
                return self.send_json({"ok": True, **members_db().create_invite(source="panel")})
            except (ValueError, members_mod.StoreError) as e:
                return self.send_json({"ok": False, "error": str(e)}, 400)
        if u.path in ("/api/members/approve", "/api/local/approve"):
            return self.handle_device_approval(data, panel=u.path == "/api/local/approve")
        if u.path == "/api/members/radio":
            return self.handle_my_radio(data)
        if u.path == "/api/members/leave":
            tok = self.member_key()
            try:
                ok = bool(tok) and members_db().leave(tok)
            except members_mod.StoreError as e:
                return self.send_json({"ok": False, "error": str(e)}, 503)
            return self.send_json({"ok": ok}, headers={"Set-Cookie": self.set_cookie(MEMBER_COOKIE, "", 0)})
        if u.path == "/api/sealed":
            st, res = sealed_api.post(self, sealed_store(), data,
                                      pin_matches=lambda pw: bool(CFG.admin_pin) and verify_pin(pw, CFG.admin_pin),
                                      duress=types.SimpleNamespace(decoy=decoy_active(), real=real_sealer(),
                                                                   trigger=self.duress_passphrase,
                                                                   recovered=lambda: exit_decoy(self.admin_token())))
            return self.send_json(res, st)
        if u.path == "/api/admin/login":
            return self.handle_login(data)
        if u.path == "/api/admin/logout":
            clear = {"Set-Cookie": self.set_cookie(ADMIN_COOKIE, "", 0)}
            if data.get("all") is True:
                # "Log out every admin session" (e.g. after removing someone who knew the PIN)
                if not self.is_admin():
                    return self.send_json({"error": "admin login required"}, 401)
                return self.send_json({"ok": True, "ended": revoke_all_tokens()}, headers=clear)
            tok = self.admin_token()
            return self.send_json({"ok": bool(tok) and revoke_token(tok)}, headers=clear)
        if u.path.startswith("/api/admin/"):
            if not self.is_admin():
                return self.send_json({"error": "admin login required"}, 401)
            return self.handle_admin(u.path.removeprefix("/api/admin/"), data)
        return self.send_json({"error": "no such endpoint"}, 404)

    def send_usb_file(self, target: Path):
        """One file from a USB drive, after the member check. Behind nginx it hands the transfer back with
        X-Accel-Redirect to an internal-only location (ranges, big files); standalone it sends it itself.
        HTML/SVG/XML never runs on the launcher's origin (sandboxed), nothing is sniffed."""
        if not target.is_file():
            return self.send_json({"error": "not found"}, 404)
        import mimetypes
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if not CFG.standalone:
            rel = target.relative_to(CFG.usb_root.resolve())
            self.send_response(200)
            self.send_header("X-Accel-Redirect", "/_usb/" + urllib.parse.quote(str(rel)))
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.split(";")[0].strip().lower() in SANDBOX_TYPES:
            self.send_header("Content-Security-Policy", "sandbox")
        self.end_headers()
        if self.command == "HEAD":
            return None
        with open(target, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)
        return None

    def is_local_console(self) -> bool:
        """(older name) — the same single check as local_console()."""
        return self.local_console()

    def wifi_state(self) -> dict:
        """prepperctl wifi-status, checked: {"ok": True, "pending", "applied", "seconds_left"} or
        {"ok": False, "error"} — a failure is never reported as "nothing pending"."""
        rc, out = ctl("wifi-status")
        last = out.strip().splitlines()[-1] if out.strip() else ""
        try:
            st = json.loads(last) if rc == 0 else None
        except (ValueError, RecursionError):
            st = None
        if not isinstance(st, dict) or not isinstance(st.get("pending"), bool):
            return {"ok": False, "error": "Couldn't check whether a Wi-Fi password change is waiting"
                                          + (f": {out[-200:]}" if out.strip() else ".")}
        secs = st.get("seconds_left")
        return {"ok": True, "pending": st["pending"],
                "applied": st.get("applied") if isinstance(st.get("applied"), bool) else None,
                "damaged": bool(st.get("error")),
                "seconds_left": int(secs) if isinstance(secs, (int, float)) and not isinstance(secs, bool)
                and math.isfinite(secs) else 0}

    def handle_device_approval(self, d: dict, panel: bool):
        """Approve or refuse a device someone added to an existing member: from one of that member's own
        (approved) devices, or from the front panel. Then the family board says so."""
        if panel:
            if not self.local_console():
                return self.send_json({"error": "only from the Badger's own screen"}, 403)
            mid, by = d.get("member"), "front panel"
        else:
            me = self.member()
            if not me:
                return self.member_refusal()
            mid, by = me["id"], f"device {me.get('device', '')}"
        dev, approve = d.get("device"), d.get("approve")
        if not isinstance(mid, str) or not isinstance(dev, str) or not isinstance(approve, bool):
            return self.send_json({"ok": False, "error": "send device and approve (true/false)"}, 400)
        try:
            res = members_db().approve_device(mid, dev, approve, by=by)
        except members_mod.StoreError as e:
            return self.send_json({"ok": False, "error": str(e)}, 503)
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        name, label = res["member"]["name"], res.get("device", {}).get("label") or "device"
        if approve:
            family_notice(f"A new device was added to {name} ({label}), approved "
                          f"{'at the Badger' if panel else 'on one of their devices'}.", dev)
        return self.send_json({"ok": True, "approved": approve, "member": {"name": name}})

    def handle_my_radio(self, d: dict):
        """A member links their own radio: {"op": "pair", "radio"} gives a code to send as "!pair CODE"
        from that radio on the family channel; "status"; "cancel"; "remove" (one of their own radios)."""
        me = self.member()
        if not me:
            return self.member_refusal()
        db, op = members_db(), d.get("op", "status")
        try:
            if op == "pair":
                res = db.start_radio_pairing(me["id"], d.get("radio", ""))
                return self.send_json({"ok": True, "pending": True, **res}, 202)
            if op == "cancel":
                db.cancel_radio_pairing(me["id"], d.get("radio", ""))
            elif op == "remove":
                db.remove_radio(me["id"], str(d.get("radio", "")))
            elif op != "status":
                return self.send_json({"ok": False, "error": "unknown radio operation"}, 400)
            done = check_radio_pairings()
            mine = [r for r in db.list()["members"] if r["id"] == me["id"]]
            return self.send_json({"ok": True, "radios": mine[0]["radios"] if mine else [],
                                   "pending_radios": db.pending_radios(me["id"]),
                                   "paired": [r["radio"] for r in done if r["member"] == me["id"]]})
        except members_mod.StoreError as e:
            return self.send_json({"ok": False, "error": str(e)}, 503)
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)

    def request_mesh_rotation(self) -> dict:
        """Ask the mesh bridge (it owns the radio's serial link) for a new family channel key; poll with
        op=rotate_mesh_status. The bridge's own failsafes apply (it refuses on a stale request, etc.)."""
        rid = secrets.token_hex(6)
        try:
            CFG.mesh_rotate_request.parent.mkdir(parents=True, exist_ok=True)
            upd.atomic_write(CFG.mesh_rotate_request, json.dumps({"id": rid, "at": time.time()}))
        except OSError as e:
            return {"ok": False, "error": f"Couldn't ask the radio bridge for a new key ({e.strerror or e}). "
                                          "Nothing was changed on the radio."}
        return {"ok": True, "id": rid, "bridge_running": bool(services_up().get("mesh"))}

    def handle_members(self, d: dict):
        db, op = members_db(), d.get("op", "list")
        op = op if isinstance(op, str) else "?"
        mid = d.get("id") if isinstance(d.get("id"), str) else ""
        secure = self.secure_transport()
        hidden = ("For safety it isn't sent over this unencrypted connection. Read it on the Badger's own "
                  "screen (press the knob → {where}), or open this page with https:// to see it here.")
        try:
            if op == "list":
                check_radio_pairings()
                return self.send_json({"ok": True, **db.list(), "store": db.status(), "panel_lock": panel_lock_on()})
            if op == "store":
                return self.send_json({"ok": True, "store": db.status()})
            if op == "arm_panel_lock":
                # Setting a panel code needs both: this (an admin) and someone at the knob within 10 minutes.
                # So a visitor alone at the Badger can't lock the family out of their own panel.
                until = time.time() + 600
                try:
                    CFG.panel_lock_arm.parent.mkdir(parents=True, exist_ok=True)
                    upd.atomic_write(CFG.panel_lock_arm, json.dumps({"until": until}))
                except OSError as e:
                    return self.send_json({"ok": False, "error": f"couldn't save that ({e.strerror or e})"}, 500)
                return self.send_json({"ok": True, "until": until, "panel_lock": panel_lock_on()})
            if op == "restore_backup":
                return self.send_json({"ok": True, "store": db.restore_backup()})
            if op == "invite":
                # A device for an EXISTING member never works on the admin PIN alone: it waits until one of
                # that member's own devices (or the front panel) approves it, and the family board says so.
                inv = db.create_invite(str(d.get("name", "")), member_id=(mid or None), confirm=bool(mid))
                if mid:
                    inv.update(needs_approval=True, message="This device starts working once the member approves it "
                               "on one of their own devices, or someone approves it at the Badger's own screen "
                               "(knob → Approve device). The family board will say a device was added.")
                return self.send_json({"ok": True, **inv})
            if op == "cancel_invite":
                if d.get("all") is True:
                    return self.send_json({"ok": True, "cancelled": db.cancel_invite()})
                iid = d.get("invite")
                if not isinstance(iid, str) or not iid:
                    return self.send_json({"ok": False, "error": "which invite?"}, 400)
                return self.send_json({"ok": True, "cancelled": db.cancel_invite(iid)})
            if op == "remove":
                gone = db.remove(mid)
                family_notice(f"{gone['name']} was removed from the family on the Badger.", mid)
                res = {"ok": True, "member": gone}
                # Their radio (and anyone they showed it to) still has the family channel key: a new key is
                # part of removing someone (default on), through the same request + failsafes as "New family
                # radio key". Radios must then get the new link (members' phones, the Badger's screen).
                if d.get("rotate_key", True) is not False:
                    res["rotate"] = self.request_mesh_rotation()
                return self.send_json(res)
            if op == "rename":
                old = [m["name"] for m in db.list()["members"] if m["id"] == mid]
                res = db.rename(mid, str(d.get("name", "")))
                if old and old[0] != res["name"]:
                    family_notice(f"{old[0]} is now called {res['name']} on the Badger.", mid)
                return self.send_json({"ok": True, "member": res})
            if op == "revoke_device":
                return self.send_json({"ok": True, "member": db.revoke_device(mid, str(d.get("device", "")))})
            if op == "add_radio":
                # Pending until that radio proves itself: it sends "!pair CODE" on the family channel (the
                # mesh bridge notes it in mesh-pair.json). 202: nothing is bound yet.
                res = db.start_radio_pairing(mid, d.get("radio", ""))
                return self.send_json({"ok": True, "pending": True, **res,
                                       "message": f"On that radio, send  !pair {res['code']}  on the family channel "
                                                  "within an hour. The radio is linked when the Badger hears it."}, 202)
            if op == "cancel_radio":
                return self.send_json({"ok": True, "member": db.cancel_radio_pairing(mid, d.get("radio", ""))})
            if op == "remove_radio":
                return self.send_json({"ok": True, "member": db.remove_radio(mid, str(d.get("radio", "")))})
            if op == "rotate_wifi":
                # prepperctl makes the password (as root), saves it and switches ~10 s later; it refuses
                # (and changes nothing) if the automatic switch-back can't be scheduled.
                rc, out = ctl("wifi-rotate")
                lines = out.splitlines()
                pw = next((ln.split(": ", 1)[1].strip() for ln in lines if ln.startswith("passphrase: ")), None)
                if rc != 0 or not pw:
                    return self.send_json({"ok": False, "error": out[-300:] or "could not change the Wi-Fi password"}, 500)
                grace = next((int(ln.split(": ", 1)[1]) for ln in lines
                              if ln.startswith("confirm-within: ") and ln.split(": ", 1)[1].strip().isdigit()), 600)
                warnings = [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("warning:")]
                res = {"ok": True, "passphrase": pw if secure else None, "applies_in_s": 10, "confirm_within_s": grace,
                       "warnings": warnings}
                if not secure:
                    res.update(secret_hidden=True, message="A new Wi-Fi password is made. " + hidden.format(where="Join Wi-Fi"))
                return self.send_json(res)
            if op == "wifi_status":
                return self.send_json(self.wifi_state())
            if op in ("confirm_wifi", "revert_wifi"):
                if op == "confirm_wifi":
                    st = self.wifi_state()
                    if st.get("ok") and st.get("pending") and st.get("applied") is False and not st.get("damaged"):
                        # Confirming before the switch would keep a password nobody has rejoined with.
                        return self.send_json({"ok": False, "error": "The new password isn't switched on yet. Wait a few "
                                               "seconds, rejoin the Wi-Fi with it, then tap Keep."}, 409)
                rc, out = ctl("wifi-confirm" if op == "confirm_wifi" else "wifi-revert")
                return self.send_json({"ok": rc == 0, "message": out[-300:]}, 200 if rc == 0 else 500)
            if op == "mesh_link":
                # The family channel link IS the channel key: never to an admin session alone (the PIN would
                # then hand out family radio access). Members see it on their own device (Settings → Family,
                # over https), and the Badger's own screen always shows it.
                st = mesh_secret(False)
                return self.send_json({"ok": bool(st.get("url") or st.get("secret_hidden")), "url": None,
                                       "at": st.get("at"), "member_only": True,
                                       "error": None if st.get("secret_hidden") else st.get("error"),
                                       "message": "The family radio link holds the channel key, so it is shown only to "
                                                  "family members on their own phone (Settings → Family → Family radio "
                                                  "link, over https) and on the Badger's own screen (knob → Family radio)."})
            if op == "rotate_mesh":
                res = self.request_mesh_rotation()
                return self.send_json(res, 200 if res["ok"] else 500)
            if op in ("rotate_mesh_status", "rotate_mesh_cancel"):
                rid = d.get("rid") if isinstance(d.get("rid"), str) else ""
                res = read_json(CFG.mesh_rotate_result, {}) or {}
                if isinstance(res, dict) and rid and res.get("id") == rid:
                    url = res.get("url") if isinstance(res.get("url"), str) else None
                    out = {"ok": bool(res.get("ok")), "pending": False, "url": None, "member_only": True,
                           "error": res.get("error") if isinstance(res.get("error"), str) else None,
                           "warning": res.get("warning") if isinstance(res.get("warning"), str) else None}
                    if url:                          # the new key: to members and the Badger's screen, not the admin
                        out.update(secret_hidden=True, message="The family channel has a new key. Every family radio "
                                   "needs the new link: family members open it on their own phone (Settings → "
                                   "Family → Family radio link) or scan it on the Badger's screen (knob → Family "
                                   "radio), then share it with the other family radios in the Meshtastic app.")
                    return self.send_json(out)
                req = read_json(CFG.mesh_rotate_request, None)
                mine = isinstance(req, dict) and req.get("id") == rid and bool(rid)
                at = req.get("at") if mine and isinstance(req.get("at"), (int, float)) else None
                stale = mine and (at is None or abs(time.time() - at) > ROTATE_REQUEST_TTL)
                if mine and (op == "rotate_mesh_cancel" or stale):
                    # Given up: take the request back so a bridge that starts later never acts on it.
                    try:
                        CFG.mesh_rotate_request.unlink()
                    except OSError:
                        pass
                    return self.send_json({"ok": False, "pending": False, "cancelled": True,
                                           "error": "The radio bridge didn't answer. Nothing was changed."})
                if isinstance(req, dict) and req.get("id") and not mine:
                    return self.send_json({"ok": False, "pending": False, "error": "A newer request replaced this one."})
                return self.send_json({"ok": True, "pending": op != "rotate_mesh_cancel"})
        except members_mod.StoreError as e:
            return self.send_json({"ok": False, "error": str(e), "store": db.status()}, 503)
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        return self.send_json({"ok": False, "error": "unknown members operation"}, 400)

    def duress_passphrase(self) -> sealed.Sealer:
        """The duress passphrase was typed for the sealed records: this admin session is a duress one too."""
        with _auth_lock:
            if self.admin_token() in TOKENS:
                DURESS_TOKENS.add(self.admin_token())
        return duress_triggered(held=False)

    def recheck_pin(self, d: dict):
        """The admin PIN typed again for a destructive step: "ok", "duress" (the duress PIN typed under
        pressure: the wipe starts quietly and the step only pretends), or an error reply already sent."""
        pin = d.get("pin")
        pin = pin[:MAX_PIN] if isinstance(pin, str) else ""
        if len(pin) < MIN_PIN:
            self.send_json({"ok": False, "error": "Type the admin PIN to confirm."}, 400)
            return None
        result, wait = attempt_login(self.client_ip(), pin, self.local_console())
        if result == "locked":
            secs = int(wait) + 1
            self.send_json({"ok": False, "error": f"Too many wrong PINs. Try again in {secs} s."}, 429, {"Retry-After": str(secs)})
            return None
        if result == "bad":
            self.send_json({"ok": False, "error": "That isn't the admin PIN. Nothing was changed."}, 403)
            return None
        if result == "duress":
            with _auth_lock:
                DURESS_TOKENS.add(self.admin_token())
            duress_triggered(held=not real_sealer().is_unlocked())
        return result

    def handle_wipe(self, d: dict):
        """POST /api/admin/wipe {"op": …}: status | panic {"confirm": "WIPE"} | handover {"confirm":
        "HANDOVER", "pin", "sale"?} | duress_set {"pin", "duress"} | duress_clear {"pin"} |
        deadman_arm {"pin", "days"} | deadman_confirm {"code"} | deadman_off |
        action {"pin", "action": "lock"|"wipe"} | phrase_set {"passphrase", "duress"} | phrase_clear {"passphrase"} |
        decoy_get | decoy_save {"profile"} | decoy_regen.
        Everything but status, deadman_off and the decoy ops needs an encrypted connection (https, or the Badger
        itself). In a duress session every answer looks real, and nothing but the duress action happens. While
        the made-up household is shown (after any duress), every session sees the settings as "nothing set up"
        and setting changes only pretend; a panic wipe or handover from a session that did NOT come in with a
        duress secret is real (a real wipe can't hurt the people in the room)."""
        op = d.get("op", "status")
        op = op if isinstance(op, str) else "?"
        with _auth_lock:
            duress_session = self.admin_token() in DURESS_TOKENS
        duress = duress_session or decoy_active()
        secure = self.secure_transport()
        started = ("Wiping now. In a few seconds this Badger holds no family data; the library stays. The Wi-Fi "
                   "password changes: read the new one on the Badger's screen (knob → Join Wi-Fi).")
        if op == "status":
            dm = read_json(CFG.deadman_state, None)
            days = (CFG.raw.get("wipe") or {}).get("deadman_days", 0) if isinstance(CFG.raw.get("wipe"), dict) else 0
            try:
                days = tomllib.loads(Path(CFG.path).read_text()).get("wipe", {}).get("deadman_days", days)
            except (OSError, ValueError, AttributeError):
                pass
            armed = isinstance(dm, dict) and isinstance(days, int) and not isinstance(days, bool) and days > 0 and not duress
            idle = dm.get("idle_s") if armed and isinstance(dm.get("idle_s"), (int, float)) else 0
            left = max(0.0, days * 86400 - idle) if armed else 0
            rs = real_sealer()
            return self.send_json({"ok": True, "secure": secure, "duress_set": bool(CFG.duress_pin) and not duress,
                                   "deadman": {"armed": armed, "days": days if armed else 0,
                                               "left_h": round(left / 3600, 1), "warning": armed and left <= 48 * 3600},
                                   "duress_action": "lock" if duress else duress_action(),
                                   "duress_phrase": (not duress) and rs.has_duress(),
                                   "sealed_setup": True if duress else rs.is_setup(),
                                   "panel_code": panel_lock_on()})
        if op in ("decoy_get", "decoy_save", "decoy_regen"):
            return self.handle_decoy(op, d, duress)
        if op == "deadman_off":
            if duress:
                return self.send_json({"ok": True, "output": "dead-man switch off"})
            rc, out = ctl("deadman-off")
            return self.send_json({"ok": rc == 0, "output": out[-2000:]}, 200 if rc == 0 else 500)
        if not secure:
            return self.send_json({"ok": False, "secure": False, "error":
                                   "For safety this works only over an encrypted connection. Open this page with "
                                   "https://, or use the Badger's own screen (knob → System → Panic wipe / Hand over)."}, 403)
        if op == "panic":
            if d.get("confirm") != "WIPE":
                return self.send_json({"ok": False, "error": "Type WIPE (capital letters) to confirm."}, 400)
            if duress_session or wipe_already_running():
                return self.send_json({"ok": True, "message": started})
            erase_sealed_in_memory()
            rc, out = start_wipe("panic-wipe", "admin")
            if rc != 0:
                return self.send_json({"ok": False, "error": "The wipe could not start: " + out[-300:]}, 500)
            return self.send_json({"ok": True, "message": started})
        if op == "handover":
            if d.get("confirm") != "HANDOVER":
                return self.send_json({"ok": False, "error": "Type HANDOVER (capital letters) to confirm."}, 400)
            r = self.recheck_pin(d)
            if r is None:
                return None
            msg = ("Resetting for the next owner. The Badger's Wi-Fi gets a new name and password (on its screen: knob "
                   "→ Join Wi-Fi), and it has no admin PIN until the new owner sets one at the Badger.")
            if r == "duress" or duress_session:
                return self.send_json({"ok": True, "message": msg})
            rc, out = start_wipe("handover-reset", "admin", ("--sale",) if d.get("sale") is True else ())
            if rc != 0:
                return self.send_json({"ok": False, "error": "The reset could not start: " + out[-300:]}, 500)
            return self.send_json({"ok": True, "message": msg})
        if op in ("duress_set", "duress_clear"):
            r = self.recheck_pin(d)
            if r is None:
                return None
            if op == "duress_clear":
                if r == "duress" or duress:
                    return self.send_json({"ok": True, "duress_set": False})
                rc, out = ctl("set-duress", "--clear")
                if rc == 0:
                    CFG.duress_pin = ""
                return self.send_json({"ok": rc == 0, "duress_set": bool(CFG.duress_pin), "output": out[-500:]},
                                      200 if rc == 0 else 500)
            new = d.get("duress")
            err, _warn = pin_policy(new)
            if err:
                return self.send_json({"ok": False, "error": err}, 400)
            if r == "duress" or duress:
                return self.send_json({"ok": True, "duress_set": True})
            if verify_pin(new, CFG.admin_pin):
                return self.send_json({"ok": False, "error": "The duress PIN must be different from the admin PIN."}, 400)
            h = hash_pin(new)
            rc, out = ctl("set-duress", "--hash", input=h + "\n")
            if rc == 0:
                CFG.duress_pin = h
            return self.send_json({"ok": rc == 0, "duress_set": rc == 0, "output": out[-500:]}, 200 if rc == 0 else 500)
        if op == "deadman_arm":
            r = self.recheck_pin(d)
            if r is None:
                return None
            days = d.get("days")
            if not isinstance(days, int) or isinstance(days, bool) or not 3 <= days <= 365:
                return self.send_json({"ok": False, "error": "Choose 3 to 365 days."}, 400)
            if r == "duress" or duress:
                return self.send_json({"ok": True, "code": "".join(secrets.choice("ABCDEFGHJKMNPQRSTVWXYZ23456789")
                                                                   for _ in range(6))})
            rc, out = ctl("deadman-set", str(days))
            m = re.search(r"confirm-code: ([A-Z0-9]{6})", out or "")
            if rc != 0 or not m:
                return self.send_json({"ok": False, "error": out[-300:] or "failed"}, 500)
            return self.send_json({"ok": True, "code": m.group(1)})
        if op == "deadman_confirm":
            code = d.get("code")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{6}", code.strip()):
                return self.send_json({"ok": False, "error": "Type the 6-character code shown."}, 400)
            if duress:
                return self.send_json({"ok": True, "output": "dead-man switch ARMED"})
            rc, out = ctl("deadman-set", "--confirm", code.strip().upper())
            return self.send_json({"ok": rc == 0, "output": out[-1000:]}, 200 if rc == 0 else 400)
        if op == "action":
            r = self.recheck_pin(d)
            if r is None:
                return None
            act = d.get("action")
            if act not in ("lock", "wipe"):
                return self.send_json({"ok": False, "error": "Choose lock or wipe."}, 400)
            if r == "duress" or duress:
                return self.send_json({"ok": True, "duress_action": act})
            try:
                update_settings(lambda st: {**st, "duress_action": act})
            except (OSError, ValueError) as e:
                return self.send_json({"ok": False, "error": f"Not saved: {e}"}, 500)
            return self.send_json({"ok": True, "duress_action": duress_action()})
        if op in ("phrase_set", "phrase_clear"):
            rs = real_sealer()
            try:
                if duress:
                    if op == "phrase_set":
                        bad = sealed.passphrase_problem(d.get("duress"))
                        if bad:
                            raise sealed.Refused(400, bad)
                    if rs.is_setup():
                        rs.check_secret(d.get("passphrase"), self.client_ip(), allow_recovery=False)
                    return self.send_json({"ok": True, "duress_phrase": op == "phrase_set"})
                if op == "phrase_set":
                    if isinstance(d.get("duress"), str) and CFG.admin_pin and verify_pin(d["duress"], CFG.admin_pin):
                        raise sealed.Refused(400, "Don't use the admin PIN as the duress passphrase.")
                    rs.set_duress(d.get("passphrase"), d.get("duress"), self.client_ip())
                else:
                    rs.clear_duress(d.get("passphrase"), self.client_ip())
            except sealed.DuressSecret:
                self.duress_passphrase()                 # the duress passphrase typed as the owner's: it IS a duress
                return self.send_json({"ok": True, "duress_phrase": op == "phrase_set"})
            except sealed.Refused as e:
                return self.send_json({"ok": False, "error": e.message}, e.status)
            except sealed.SealError as e:
                return self.send_json({"ok": False, "error": f"Sealed records aren't working: {e}"}, 503)
            return self.send_json({"ok": True, "duress_phrase": rs.has_duress()})
        return self.send_json({"ok": False, "error": "unknown wipe operation"}, 400)

    def handle_decoy(self, op: str, d: dict, duress: bool):
        """The owner's decoy editor: preview, edit (names, supplies, board posts), regenerate. While the made-up
        household is shown, a change only pretends (the working copy stays as it is)."""
        try:
            prof = decoy_profile()
            n = len(decoy_mod.member_skeleton(real_path("members_file")))
            if op == "decoy_get":
                return self.send_json({"ok": True, "profile": prof, "preview": decoy_mod.preview(prof, n),
                                       "categories": list(decoy_mod.CATEGORIES)})
            if op == "decoy_regen":
                new = decoy_mod.generate()
            else:
                body = d.get("profile")
                if not isinstance(body, dict):
                    return self.send_json({"ok": False, "error": "Nothing to save."}, 400)
                if len(json.dumps(body)) > decoy_mod.MAX_BYTES:
                    return self.send_json({"ok": False, "error": "That is too much for the decoy."}, 413)
                new = decoy_mod.normalize({**body, "seed": prof["seed"], "created": prof.get("created")})
                if not new["supplies"] or not new["board"]:
                    return self.send_json({"ok": False, "error": "Keep a few supplies and a few board posts: an "
                                                                 "empty list looks wrong."}, 400)
            if not duress:
                new = decoy_mod.save(decoy_profile_file(), new)
            return self.send_json({"ok": True, "profile": new, "preview": decoy_mod.preview(new, n),
                                   "categories": list(decoy_mod.CATEGORIES)})
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        except OSError as e:
            return self.send_json({"ok": False, "error": f"Not saved ({e.strerror or e})."}, 500)

    def handle_login(self, data: dict):
        ip = self.client_ip()
        local = self.local_console()
        wait = login_wait(ip, local)
        if wait > 0:
            secs = int(wait) + 1
            return self.send_json({"error": f"Too many wrong PINs. Try again in {secs} s.", "retry_after": secs},
                                  429, {"Retry-After": str(secs)})
        pin = data.get("pin", "")
        pin = pin if isinstance(pin, str) else ""
        if len(pin) < MIN_PIN:
            return self.send_json({"error": f"PINs shorter than {MIN_PIN} characters are no longer accepted. "
                                            f"If yours is shorter, set a longer one on the Badger: "
                                            f"sudo prepperctl set-pin"}, 400)
        pin = pin[:MAX_PIN]                      # hashing a megabyte "PIN" is a free CPU burner
        time.sleep(0.25)
        if not CFG.admin_pin:
            # (a handover reset clears it: the next owner sets one at the Badger)
            return self.send_json({"error": "This Badger has no admin PIN yet. Set one at the Badger (keyboard and "
                                            "screen, or SSH): sudo prepperctl set-pin", "no_pin": True}, 403)
        result, wait = attempt_login(ip, pin, local)
        if result == "locked":
            secs = int(wait) + 1
            return self.send_json({"error": f"Too many wrong PINs. Try again in {secs} s.", "retry_after": secs},
                                  429, {"Retry-After": str(secs)})
        if result in ("ok", "duress"):
            # The duress PIN gets exactly the same reply (keys, cookie, timing): see duress_wipe().
            prune_tokens()
            tok = secrets.token_urlsafe(24)
            with _auth_lock:
                TOKENS[tok] = time.time() + ADMIN_TTL
                if result == "duress":
                    DURESS_TOKENS.add(tok)
            if result == "duress":
                duress_triggered(held=not real_sealer().is_unlocked())
            else:
                note_checkin()
            if result == "ok" and pin_is_legacy(CFG.admin_pin):     # an older PBKDF2 hash: scrypt it (same secret)
                threading.Thread(target=upgrade_pin_hash, args=(pin,), daemon=True).start()
            err, warn = pin_policy(pin)
            # The launcher uses the HttpOnly cookie; the token in the body is for tools and plugins.
            return self.send_json({"token": tok, "weak_pin": bool(err or warn)},
                                  headers={"Set-Cookie": self.set_cookie(ADMIN_COOKIE, tok, ADMIN_TTL)})
        return self.send_json({"error": "wrong PIN"}, 403)

    def handle_ask(self, q: str, brief: bool = False):
        slots = ask_slots()
        if not slots.acquire(blocking=False):
            return self.send_json({"error": "The assistant is answering other questions. Try again in a minute."},
                                  503, {"Retry-After": "30"})
        try:
            self._ask(q, brief)
        finally:
            slots.release()

    def _ask(self, q: str, brief: bool = False):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")       # tell nginx not to buffer
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def write(event, payload):
            try:
                self.wfile.write(f"event: {event}\ndata: {dumps_safe(payload)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                raise SystemExit from None             # the browser left: stop asking the model
        try:
            ask_stream(q, write, brief)
        except SystemExit:
            pass

    def handle_personal(self, d: dict):
        """POST /api/admin/personal — the owner's decision about personal-use content."""
        verb = d.get("action")
        name = d.get("name") if isinstance(d.get("name"), str) else ""
        items = d.get("items")
        if verb == "accept":
            if d.get("agree") is not True:
                return self.send_json({"ok": False, "error": "tick the box to agree to the personal-use terms first"}, 400)
            if items is not None and (not isinstance(items, list) or len(items) > 1000
                                      or not all(isinstance(x, str) and len(x) <= 128 for x in items)):
                return self.send_json({"ok": False, "error": "bad item list"}, 400)
        elif verb not in ("later", "remove"):
            return self.send_json({"ok": False, "error": "action must be accept, later or remove"}, 400)
        if len(name) > 64:
            return self.send_json({"ok": False, "error": "name is too long (64 characters at most)"}, 400)
        try:
            upd.record_personal_decision(CFG.upd, {"remove": "withdraw"}.get(verb, verb), items=items,
                                         by=name, ip=self.client_ip())
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        except OSError as e:
            return self.send_json({"ok": False, "error": f"couldn't save the decision: {e.strerror or e}"}, 500)
        _cache.pop("library", None)
        msg = {"later": "OK — the offer stays in Settings and on the Status page."}.get(verb, "")
        if verb == "accept":
            why = download_deferred()
            if why:
                msg = (f"Accepted. Not downloading now because {why}; they download with the next update "
                       "(weekly, or Settings → Download updates).")
            else:
                rc, out = ctl("update-apply")
                msg = ("Accepted. Downloading now — progress on the Status page. With no internet right now, they "
                       "download with the next update once the Badger is online (the weekly automatic update, "
                       "or Settings → Download updates).") if rc == 0 else \
                      "Accepted. The download couldn't start now (" + out.strip()[-200:] + "); it runs with the next update."
        elif verb == "remove":
            rc, out = ctl("update-apply")          # upd apply deletes them first, then carries on as usual
            msg = "Removed from this Badger's list. Their files are being deleted." if rc == 0 else \
                "Removed from this Badger's list. Their files are deleted with the next update."
        return self.send_json({"ok": True, "message": msg, "personal": personal_view(True)})

    def handle_admin(self, action: str, d: dict):
        if action == "wipe":
            return self.handle_wipe(d)
        if action == "members":
            return self.handle_members(d)
        if action == "personal":
            return self.handle_personal(d)
        if action == "updates":
            verb = d.get("action")
            if verb == "check":
                rc, out = ctl("update-check")
            elif verb == "apply":
                rc, out = ctl("update-apply")
            elif verb in ("import", "export"):
                p = usb_path(d.get("src" if verb == "import" else "dest"))
                if p is None:
                    return self.send_json({"error": "the bundle must be a folder on a USB drive under "
                                                    f"{CFG.usb_root}"}, 400)
                rc, out = ctl("update-" + verb, str(p))
                if verb == "import" and upd.held_personal(upd.load_catalog(CFG.upd.catalog), CFG.upd):
                    out += ("\nPersonal-use items in the bundle (non-commercial licences) are skipped until the "
                            "owner accepts them: Settings → Personal-use content.")
            elif verb == "add-peer":
                peer = str(d.get("peer", ""))
                if not re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]+(/[\w./-]*)?", peer):
                    return self.send_json({"error": "bad peer URL"}, 400)
                rc, out = ctl("add-peer", peer)
            else:
                return self.send_json({"error": "unknown update action"}, 400)
        elif action == "uplink":
            ssid, psk = str(d.get("ssid", "")).strip(), str(d.get("psk", ""))
            if d.get("forget") is True:
                rc, out = ctl("uplink-forget")
            elif not ssid or len(ssid.encode()) > 32 or ssid.startswith("-") or re.search(r"[\x00-\x1f\x7f]", ssid) \
                    or (psk and not re.fullmatch(r"[ -~]{8,63}|[0-9A-Fa-f]{64}", psk)):
                # (the same rule prepperctl applies: empty = open network, else 8–63 printable ASCII or 64 hex)
                return self.send_json({"error": "enter a network name (and password, 8–63 characters)"}, 400)
            else:
                rc, out = ctl("uplink-wifi", ssid, input=psk + "\n")      # password on stdin, never in argv
            _cache.pop("uplink", None)
        elif action == "service":
            name, verb = str(d.get("name", "")), str(d.get("action", ""))
            if name not in CFG.services or verb not in ("start", "stop", "restart"):
                return self.send_json({"error": "bad service request"}, 400)
            rc, out = ctl("service", name, verb)
            _cache.pop("services", None); _cache.pop("library", None)
        elif action == "power":
            mode = str(d.get("mode", ""))
            if mode not in ("endurance", "balanced", "full"):
                return self.send_json({"error": "mode must be endurance|balanced|full"}, 400)
            rc, out = ctl("power-mode", mode)
        elif action == "settings":
            cur = settings()
            rd = d.get("renderer_defaults")
            if d.get("display_time") in ("local", "utc", "nutime"):
                cur["display_time"] = d["display_time"]
            mc = d.get("mesh_commands")
            if isinstance(mc, dict):                    # who may use each mesh radio command
                levels = ("community", "family", "off", "ignore")
                clean = {k: v for k, v in mc.items() if k in ("help", "q", "ai", "status")
                         and isinstance(v, str) and v in levels}
                if clean.get("help") == "off":
                    clean["help"] = "family"             # never reopen !help to strangers
                prev = cur.get("mesh_commands") if isinstance(cur.get("mesh_commands"), dict) else {}
                cur["mesh_commands"] = {**prev, **clean}
            if isinstance(rd, dict):
                valid = load_renderers()
                cur["renderer_defaults"] = {k: v for k, v in rd.items()
                                            if isinstance(v, str) and k in valid
                                            and v in {r["id"] for r in valid[k].get("renderer", [])}}
            CFG.settings_file.parent.mkdir(parents=True, exist_ok=True)
            upd.atomic_write(CFG.settings_file, json.dumps(cur, indent=2))
            _cache.pop("library", None)
            return self.send_json({"ok": True, "settings": cur})
        elif action == "usb":
            dev = str(d.get("device", ""))
            if not EJECT_NAME.fullmatch(dev) or ".." in dev:
                return self.send_json({"error": "bad device"}, 400)
            rc, out = ctl("usb-eject", dev)
        elif action == "system":
            verb = d.get("action")
            if verb not in ("shutdown", "reboot", "wifi-off", "wifi-on"):
                return self.send_json({"error": "bad system action"}, 400)
            rc, out = ctl(verb)
        else:
            return self.send_json({"error": "unknown admin action"}, 404)
        return self.send_json({"ok": rc == 0, "output": out[-4000:]}, 200 if rc == 0 else 500)


def personal_view(admin: bool) -> dict:
    """GET /api/content/personal: what everyone may see; who accepted from where, and
    the history, only for the admin."""
    view = upd.personal_offer(CFG.upd)
    if view.get("accepted") and not admin:
        view["accepted"] = {k: v for k, v in view["accepted"].items() if k not in ("ip", "by")}
    if admin:
        view["history"] = upd.load_personal_record(CFG.upd).get("history", [])[-20:]
    view["admin"] = admin
    return view


def download_deferred() -> str | None:
    """Endurance mode or running on battery: don't start a big download now."""
    p = read_json(CFG.power_file, None)
    if not isinstance(p, dict):
        return None
    if p.get("mode") == "endurance":
        return "the Badger is in Endurance mode"
    if p.get("external") is False:
        return "the Badger is running on battery"
    return None


def usb_path(value) -> Path | None:
    """A path on a USB drive (under usb_root, symlinks resolved), or None. The
    resolved path is what gets passed on, so "/media/../etc", "usb0/../../etc",
    an absolute path elsewhere or a leading "-" can't reach prepperctl."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    root = CFG.usb_root.resolve()
    p = Path(value.strip())
    try:
        p = (p if p.is_absolute() else root / p).resolve()
    except (OSError, RuntimeError):
        return None
    return p if root in p.parents and on_usb_mount(p, root) else None


def on_usb_mount(p: Path, root: Path) -> bool:
    """p lies on a filesystem mounted below usb_root that is not the root
    filesystem: /media/usb0/x with nothing plugged in is just a folder on the
    SD card, and an export there would fill it."""
    try:
        root_dev = os.stat("/").st_dev
        q = p
        while q != root and root in q.parents:
            if q.exists() and os.path.ismount(q):
                return os.stat(q).st_dev != root_dev
            q = q.parent
    except OSError:
        pass
    return False


# What `prepperctl usb-eject` accepts, exactly: a mount name directly under
# /media (udev's usb-sda1) or a block device node — never an option or a path.
EJECT_NAME = re.compile(r"/dev/[A-Za-z0-9]+|[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")


def mount_sources() -> dict[str, str]:
    """mountpoint → source device, from /proc/self/mounts (empty where there is none)."""
    out = {}
    try:
        for line in Path("/proc/self/mounts").read_text().splitlines():
            f = line.split()
            if len(f) >= 2:
                # /proc/mounts escapes spaces etc. as \040 octal
                out[re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f[1])] = f[0]
    except OSError:
        pass
    return out


def usb_list() -> list[dict]:
    """Mounted drives under usb_root: udev's /media/usb-* and desktop-style
    /media/<user>/<label>. `eject` is the argument usb-eject accepts for it —
    the mount name for a drive directly under /media, else its /dev node (a
    nested mount can't be named under /media) — or None if neither works."""
    out = []
    root = CFG.usb_root
    if not root.exists():
        return out
    sources = None
    for p in sorted(root.glob("*")) + sorted(root.glob("*/*")):
        if p.is_symlink() or not p.is_dir() or not os.path.ismount(p):
            continue
        if p.parent == root and EJECT_NAME.fullmatch(p.name):
            eject = p.name
        else:
            sources = mount_sources() if sources is None else sources
            src = sources.get(str(p), "")
            eject = src if re.fullmatch(r"/dev/[A-Za-z0-9]+", src) else None
        du = shutil.disk_usage(p)
        out.append({"name": p.name, "path": str(p), "free": du.free, "total": du.total,
                    "bundle": (p / "manifest.json").exists(), "eject": eject,
                    "drive": str(p.relative_to(root))})
    return out


def usb_resolve(drive: str, rel: str) -> Path | None:
    """A file or folder on a mounted USB drive (drive = its name under usb_root, as usb_list gives it),
    symlinks resolved and kept inside that drive; None for anything else."""
    if not isinstance(drive, str) or not drive or "\x00" in drive or "\x00" in (rel or ""):
        return None
    root = CFG.usb_root
    if drive not in {d["drive"] for d in usb_list()}:
        return None
    base = (root / drive).resolve()
    target = safe_join(base, rel or "")
    return target if target is not None and target.exists() else None


def usb_listing(drive: str, target: Path) -> dict | None:
    if not target.is_dir():
        return None
    base = (CFG.usb_root / drive).resolve()
    entries = []
    try:
        names = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:2000]
    except OSError:
        return None
    for p in names:
        if p.name.startswith("."):
            continue
        real = safe_join(base, str(p.relative_to(base)))
        if real is None or not real.exists():
            continue                                   # a link off the drive, or a dangling one
        rel = str(p.relative_to(base))
        q = urllib.parse.urlencode({"drive": drive, "path": rel})
        if real.is_dir():
            entries.append({"name": p.name, "dir": True, "path": rel})
            continue
        try:
            size = real.stat().st_size
        except OSError:
            continue
        entries.append({"name": p.name, "dir": False, "path": rel, "size": size, "url": "/api/usb/file?" + q})
    return {"drive": drive, "path": str(target.relative_to(base)) if target != base else "", "entries": entries}


def main() -> None:
    global CFG
    if len(sys.argv) >= 2 and sys.argv[1] == "hash-pin":
        # The secret comes on stdin (prepperctl set-pin): an argument would show in `ps` to every local
        # user. (An argument still works for development fixtures.) Policy problems → exit 2.
        pin = sys.argv[2] if len(sys.argv) >= 3 else sys.stdin.readline().rstrip("\r\n")
        err, warn = pin_policy(pin)
        if err:
            print(f"error: {err}", file=sys.stderr); sys.exit(2)
        if warn:
            print(f"warning: {warn}", file=sys.stderr)
        print(hash_pin(pin)); return
    if len(sys.argv) == 2 and sys.argv[1] == "rehash-pin":
        # prepperctl upgrade-pin: stdin = the stored hash, then the PIN. Prints an scrypt hash of the SAME
        # PIN only if the stored one is the older PBKDF2 kind and the PIN matches it; else exit 1.
        stored = sys.stdin.readline().strip()
        pin = sys.stdin.readline().rstrip("\r\n")[:MAX_PIN]
        if not pin_is_legacy(stored) or not verify_pin(pin, stored):
            sys.exit(1)
        print(hash_pin(pin)); return
    cfg_path = Path(os.environ.get("PREPPER_CONFIG", upd.DEFAULT_CONFIG))
    CFG = PortalConfig(cfg_path)
    load_features()
    start_family_inbox_timer()
    try:
        decoy_profile()                          # the made-up household, always ready (docs/SECURITY.md → "Duress")
    except Exception as e:  # noqa: BLE001
        print(f"[portal] decoy profile not made: {type(e).__name__}", file=sys.stderr)
    host, port = CFG.listen.rsplit(":", 1)
    srv = PortalServer((host, int(port)), Handler, max_threads=CFG.max_threads)
    print(f"[portal] {CFG.edition} edition on http://{CFG.listen} "
          f"({'standalone' if CFG.standalone else 'behind nginx'})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
