#!/usr/bin/env python3
"""
meshbridge — joins the Badger's check-in boards to a Meshtastic LoRa mesh.

A Meshtastic node (e.g. RAK WisBlock / Heltec) on USB serial keeps running on
its own even when the Pi is off; this bridge, when up:
  * two boards, two channels: the family's private, encrypted channel ↔ the family
    board; the public channel ↔ the community board ([radio] mesh_family_channel /
    mesh_public_channel). Radios registered to a removed member are ignored.
  * sends board posts marked "Send over mesh" from the outbox, on that board's channel
    only (never a community post on the family channel), paced so a flood of posts
    can't hog the air or hold up family messages
  * broadcasts each new NOAA weather alert once on both channels (from alertd's alerts file)
  * keeps a snapshot of the positions radios share, for the offline map, and a log of
    the telemetry radios send (weather sensors, batteries) for the launcher's features
  * on request from the admin page, gives the family channel a fresh random key and
    reports the new channel link (to update the other family radios)
  * answers commands (rate-limited). Who may use each one is set per command in
    [radio] mesh_commands: "community" (anyone), "family" (sent on the family channel)
    or "off". Defaults: !help and !q community, !ai and !status family.
      !help           what this node can do
      !q <words>      top library match: title + first ~150 characters
      !ai <question>  a short answer from the built-in AI solver, grounded in the library
      !status         battery, power source, mode, alerts, people on the Wi-Fi

Trust: Meshtastic's sender ID ("fromId") is NOT authenticated on channel traffic —
anyone can transmit with any ID. Only the family channel's private key proves
anything, so "family" means "arrived on the family channel", never "came from a
registered radio ID". Mesh posts are never marked verified; member names are put
only on posts that arrived on the family channel. Direct messages to the Badger are
commands only, never board posts.
Replies are short on purpose: LoRa carries ~200 bytes per message.
"""
from __future__ import annotations

import collections
import json
import math
import os
import queue
import re
import secrets
import sys
import tempfile
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Defaults; [portal] board_file / mesh_outbox / mesh_nodes / mesh_telemetry … in prepper.toml
# (shared with the portal) or BOARD_FILE / MESH_OUTBOX / MESH_NODES / MESH_TELEMETRY /
# ALERTS_FILE in the environment override them.
BOARD = Path("/var/lib/prepper/portal/board.jsonl")                  # community board
FAMILY_BOARD = Path("/var/lib/prepper/portal/family-board.jsonl")
MEMBERS = Path("/var/lib/prepper/portal/members.json")
OUTBOX = Path("/var/lib/prepper/portal/mesh-outbox.jsonl")
NODES = Path("/var/lib/prepper/portal/mesh-nodes.json")
TELEMETRY = Path("/var/lib/prepper/portal/mesh-telemetry.jsonl")
ALERTS = Path("/run/prepper/alerts.json")
BEAT = Path("/run/prepper/mesh.ok")
ROTATE_REQ = Path("/var/lib/prepper/portal/mesh-rotate.json")
ROTATE_RES = Path("/var/lib/prepper/portal/mesh-rotate-result.json")
CHANNEL_LINK = Path("/var/lib/prepper/portal/mesh-channel.json")   # holds the key: 0600
BT_PIN = Path("/var/lib/prepper/portal/mesh-bt-pin.json")          # the radio's Bluetooth PIN: 0600
API = os.environ.get("PREPPER_API", "http://127.0.0.1:8000")
MAX_BYTES = 200                                   # what one LoRa text message carries
ROTATE_TTL = 120                                  # a key-change request older than this is ignored
BOARD_MAX_BYTES = 2_000_000                       # (same limit the portal keeps its boards under)
TELEMETRY_MAX_BYTES = 1_000_000
TELEMETRY_EVERY = 30.0                            # at most one telemetry line per radio per 30 s
NODE_RE = re.compile(r"^![0-9a-f]{8}$")

COMMANDS = {"!help": "what this Badger can do", "!q": "<words> searches the library",
            "!ai": "<question> asks the AI solver", "!status": "battery and alerts"}
DEFAULT_ACCESS = {"!help": "community", "!q": "community", "!ai": "family", "!status": "family"}
LEVELS = ("community", "family", "off")


def command_access(cfg: dict | None, override: dict | None = None) -> dict[str, str]:
    """Who may use each command. Starts from the defaults, then [radio] mesh_commands in
    prepper.toml (e.g. { q = "community", ai = "family", status = "off" }), then what the admin
    set in the launcher (portal settings → mesh_commands), which wins. Unknown commands or
    levels are ignored with a warning, so a typo can't open anything up."""
    access = dict(DEFAULT_ACCESS)
    pairs = []
    for src in ((cfg or {}).get("mesh_commands"), override):
        if isinstance(src, dict):
            pairs.extend(src.items())
    for k, v in pairs:
        word = "!" + str(k).lstrip("!").lower()
        if word in access and isinstance(v, str) and v in LEVELS:
            access[word] = v
        else:
            print(f"meshbridge: ignoring mesh_commands.{k} = {v!r} (commands: help, q, ai, status; "
                  f"levels: community, family, off)", file=sys.stderr)
    access["!help"] = access["!help"] if access["!help"] != "off" else "community"   # always say what's available
    return access


def help_text(access: dict[str, str], trusted: bool) -> str:
    """Only the commands this radio may use."""
    usable = [w for w in ("!q", "!ai", "!status") if access.get(w) == "community" or (trusted and access.get(w) == "family")]
    if not usable:
        return "Honey Badger: no radio commands are available to you. Board posts and NOAA alerts are relayed here."
    return fit("Honey Badger: " + " · ".join(f"{w} {COMMANDS[w]}" for w in usable) +
               ". Board posts and NOAA alerts are relayed here.")


# ─────────────────────────────── small helpers ───────────────────────────────

def fit(text, limit: int = MAX_BYTES) -> str:
    """Trim to `limit` UTF-8 bytes at a word boundary, ending with an ellipsis when cut."""
    text = " ".join(str(text).split())
    text = text.encode("utf-8", "replace").decode("utf-8")       # (a lone surrogate can't go on the air)
    if len(text.encode()) <= limit:
        return text
    budget = limit - len("…".encode())
    cut = text.encode()[:budget].decode("utf-8", "ignore")
    if " " in cut[len(cut) // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


HELP = help_text(DEFAULT_ACCESS, True)
_DROP = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def clean(s, limit: int) -> str:
    """Text from the air, made safe to show: no control, invisible or direction-changing characters."""
    s = "".join(ch for ch in str(s if s is not None else "") if unicodedata.category(ch) not in _DROP)
    return " ".join(s.split())[:limit]


def clean_body(s, limit: int) -> str:
    """A message body: control characters become spaces, lone surrogates go; everything else
    (emoji joiners, every script) is kept as sent."""
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", " ", str(s if s is not None else ""))
    return re.sub(r"[\ud800-\udfff]", "", s).strip()[:limit]


def _private_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomic write: mkstemp (O_EXCL, a fresh random name — never a planted file or symlink),
    the mode set on our own descriptor, then rename over the target (which replaces a symlink
    instead of following it)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_json(path: Path, data, mode: int = 0o640) -> bool:
    try:
        _private_file(path, json.dumps(data).encode(), mode)
        return True
    except (OSError, ValueError, TypeError) as e:
        print(f"meshbridge: cannot write {path.name}: {e}", file=sys.stderr)
        return False


def read_json(path: Path, default, limit: int = 4_000_000):
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit + 1)
        if len(raw) > limit:
            return default
        return json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return default


def trim_jsonl(path: Path, max_bytes: int, mode: int = 0o640) -> None:
    """Keep a JSON-lines file under max_bytes by dropping its oldest half."""
    try:
        if path.stat().st_size <= max_bytes:
            return
        with open(path, "rb") as fh:
            fh.seek(-(max_bytes // 2), os.SEEK_END)
            tail = fh.read()
        _private_file(path, tail.split(b"\n", 1)[1] if b"\n" in tail else b"", mode)
    except OSError as e:
        print(f"meshbridge: cannot trim {path.name}: {e}", file=sys.stderr)


def board_add(name, text, board: str = "community", radio="", member: str | None = None) -> None:
    """A post heard on the mesh. Never "verified" (a radio ID proves nothing on the air); a member
    id (for the "removed member" mark) only on the family board, where the channel key vouches."""
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "name": clean(name, 32) or "radio",
           "text": clean_body(text, 2000), "via": "mesh-in", "radio": clean(radio, 16), "verified": False}
    if member and board == "family":
        rec["member"] = str(member)[:16]
    path = FAMILY_BOARD if board == "family" else BOARD
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    trim_jsonl(path, BOARD_MAX_BYTES)


def radio_registry(path: Path) -> dict[str, dict]:
    """Radio ID → {"member", "name", "removed"} from the portal's members file (same format as members.py).
    Damaged or odd entries are skipped; an unreadable file gives {}."""
    d = read_json(path, {})
    out: dict[str, dict] = {}
    mems = d.get("members") if isinstance(d, dict) else None
    for mid, m in (mems.items() if isinstance(mems, dict) else []):
        if not isinstance(m, dict):
            continue
        name = clean(m.get("name", ""), 32)
        radios = m.get("radios") if isinstance(m.get("radios"), list) else []
        gone = m.get("removed_radios") if isinstance(m.get("removed_radios"), list) else []
        for r in radios:
            if isinstance(r, str):
                out[r.lower()] = {"member": str(mid), "name": name, "removed": bool(m.get("removed"))}
        for r in gone:
            if isinstance(r, str):
                out.setdefault(r.lower(), {"member": str(mid), "name": name, "removed": True})
    return out


def channel_config(cfg: dict) -> tuple[int, int, list[str]]:
    """(family channel, public channel, notes); -1 = not bridged. Fails closed:
      * the family channel must be set explicitly ([radio] mesh_family_channel); older configs
        with only mesh_channel bridge that channel as the PUBLIC one, family features off;
      * channel 0 (the radio's primary, usually the public LongFast) is never the family's;
      * family == public: the family side is switched off, not the public one."""
    notes: list[str] = []

    def chan(key: str, default: int) -> int:
        if key not in cfg:
            return default
        v = cfg[key]
        if isinstance(v, bool) or not isinstance(v, int) or not -1 <= v <= 7:
            notes.append(f"[radio] {key} = {v!r} is not a channel number (0–7, or -1 for none): not bridged")
            return -1
        return v

    if "mesh_family_channel" in cfg or "mesh_public_channel" in cfg:
        fam, pub = chan("mesh_family_channel", -1), chan("mesh_public_channel", 0)
        if "mesh_family_channel" not in cfg:
            notes.append("no [radio] mesh_family_channel: family board and family commands are off on the mesh")
    else:
        fam, pub = -1, chan("mesh_channel", 0)
        notes.append("older config (only mesh_channel): it is bridged as the public channel; set "
                     "[radio] mesh_family_channel to the family's private channel to turn family features on")
    if fam == 0:
        notes.append("mesh_family_channel = 0 is the radio's primary channel (normally the public one): "
                     "family features are off — give the family its own channel (1–7)")
        fam = -1
    if fam >= 0 and fam == pub:
        notes.append(f"mesh_family_channel and mesh_public_channel are both {fam}: one channel can't be private "
                     "and public, so family features are off")
        fam = -1
    return fam, pub, notes


def route_text(text: str, ch: int, family_ch: int, public_ch: int, reg: dict | None) -> tuple[str, bool]:
    """Where an incoming mesh text goes: ("family"|"community"|"ignore", trusted_for_commands).
    Trusted only on the family channel (its private key is the only proof the mesh offers)."""
    if reg and reg.get("removed"):
        return "ignore", False                       # a removed member's radio: nothing, not even commands
    if family_ch >= 0 and ch == family_ch:
        return "family", True
    if public_ch >= 0 and ch == public_ch:
        return "community", False
    return "ignore", False                           # another channel: commands only


def api_get(path: str, timeout: float = 10):
    with urllib.request.urlopen(f"{API}{path}", timeout=timeout) as r:
        return json.loads(r.read(1_000_000))


# ─────────────────────────────── commands ────────────────────────────────────

def library_answer(q: str) -> str:
    try:
        res = api_get(f"/api/search?q={urllib.parse.quote(q)}")
    except (OSError, ValueError):
        return "Library unavailable."
    arts = res.get("articles") if isinstance(res, dict) else None
    if not arts or not isinstance(arts, list) or not isinstance(arts[0], dict):
        return fit(f"No match for '{q[:40]}'.")
    a = arts[0]
    return fit(f"{a.get('title', '')}: {a.get('snippet', '')}", 190)


def ai_answer(q: str, timeout: float = 180) -> str:
    """A brief, library-grounded answer from the portal's AI solver; falls back to a library match."""
    body = json.dumps({"q": q[:300], "brief": True}).encode()
    req = urllib.request.Request(f"{API}/api/ask", data=body, headers={"Content-Type": "application/json"})
    answer, source, event = [], "", ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if event == "sources" and isinstance(data, list) and data and isinstance(data[0], dict):
                        source = str(data[0].get("title", ""))[:40]
                    elif event == "token" and isinstance(data, str):
                        answer.append(data)
                    elif event == "done":
                        break
    except urllib.error.HTTPError as e:
        if e.code == 503:
            return "AI solver is busy answering others. Try again in a minute."
        return library_answer(q)
    except (OSError, ValueError):
        return library_answer(q)
    text = " ".join("".join(answer).split())
    if not text or text.startswith("(The AI model isn't answering"):
        return library_answer(q)
    tail = f" [{source}]" if source else ""
    return fit(fit(text, MAX_BYTES - len(tail.encode())) + tail)


def format_status(s: dict) -> str:
    p = s.get("power") if isinstance(s.get("power"), dict) else {}
    parts = []
    if _num(p.get("percent")) is not None:
        bat = f"battery {round(p['percent'])}%"
        if _num(p.get("input_w")):
            bat += f", {p['input_w']:g} W in"
        elif p.get("charging") or p.get("external"):
            bat += ", on power"
        else:
            bat += ", on battery"
        if _num(p.get("runtime_min")) and not (p.get("charging") or p.get("external")):
            bat += f" (~{round(p['runtime_min'] / 60, 1):g} h left)"
        parts.append(bat)
    if p.get("mode"):
        parts.append(str(p["mode"]).capitalize())
    alerts = [a for a in (s.get("alerts") if isinstance(s.get("alerts"), list) else []) if isinstance(a, dict) and not a.get("test")]
    parts.append(f"{len(alerts)} alert{'s' if len(alerts) != 1 else ''}" if alerts else "no alerts")
    if alerts:
        parts.append(f"{alerts[0].get('event', 'Alert')} until {alerts[0].get('expires', '?')}")
    if _num(s.get("clients")) is not None:
        parts.append(f"{s['clients']} on Wi-Fi")
    return fit("Badger: " + " · ".join(parts))


def status_answer() -> str:
    try:
        s = api_get("/api/status")
        return format_status(s if isinstance(s, dict) else {})
    except (OSError, ValueError):
        return "Badger status unavailable."


class Commands:
    """Routes !commands with per-sender rate limits and an overall budget for replies to radios
    outside the family (so a stream of forged sender IDs can't turn the Badger into an airtime
    amplifier). !ai runs one at a time in a worker thread; `io` runs the other slow answers
    (library, status) — by default inline; meshbridge's main() hands them to a worker so the
    radio's receive thread is never blocked."""

    def __init__(self, send, ai_enabled: bool = True, cooldown: float = 30, ai_cooldown: float = 120,
                 clock=time.time, library=library_answer, ai=ai_answer, status=status_answer, spawn=None,
                 access=None, io=None, public_budget: tuple[int, float] = (20, 600.0)):
        self.send, self.ai_enabled = send, ai_enabled
        # a dict, or a function returning one (re-read on every command, so admin changes apply at once)
        self._access = access if callable(access) else (lambda a=dict(access or DEFAULT_ACCESS): a)
        self.cooldown, self.ai_cooldown, self.clock = cooldown, ai_cooldown, clock
        self.library, self.ai, self.status = library, ai, status
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.io = io or (lambda fn: fn())
        self.budget, self.budget_used = public_budget, collections.deque()
        self.last: dict[str, float] = {}
        self.last_ai: dict[str, float] = {}
        self.ai_busy = threading.Lock()
        self._lock = threading.Lock()

    @staticmethod
    def _key(sender) -> str:
        return clean(sender, 40).lower()            # "!A1B2C3D4", " !a1b2c3d4" … are one radio

    def _prune(self, now: float) -> None:
        if len(self.last) > 500:
            keep = max(self.cooldown, self.ai_cooldown)
            for d in (self.last, self.last_ai):
                for k in [k for k, t in d.items() if now - t > keep]:
                    del d[k]

    def handle(self, text, sender, trusted: bool = True, reply=None) -> bool:
        """Returns True when the text was a command this bridge answered (or refused, or ignored as a flood).
        trusted: it arrived on the family channel."""
        if not isinstance(text, str):
            return False
        say = reply or (lambda t: self.send(t, sender))
        text = text.strip()
        if not text.startswith("!"):
            return False
        word, _, arg = text.partition(" ")
        word, arg = word.lower(), arg.strip()
        if word not in ("!help", "!q", "!ai", "!status"):
            return False
        key = self._key(sender)
        with self._lock:
            now = self.clock()
            if now - self.last.get(key, -1e9) < self.cooldown:
                return True                               # quietly ignore floods
            if not trusted:
                n, window = self.budget
                while self.budget_used and now - self.budget_used[0] > window:
                    self.budget_used.popleft()
                if len(self.budget_used) >= n:
                    return True                           # too many answers to strangers lately: stay quiet
                self.budget_used.append(now)
            self.last[key] = now
            self._prune(now)
        access = self._access()
        level = access.get(word) if isinstance(access, dict) else None
        if not isinstance(level, str) or level not in ("community", "family"):
            level = "off"                                 # unknown or garbled levels fail closed
        if level == "off":
            say(f"{word} is turned off on this Badger. Send !help for what's available.")
        elif level == "family" and not trusted:
            say(f"{word} is for family radios. Send !help for what you can use.")
        elif word == "!help":
            say(help_text(access, trusted))
        elif word == "!status":
            self.io(lambda: say(self.status()))
        elif word == "!q":
            if arg:
                self.io(lambda: say(self.library(arg)))
            else:
                say("Usage: !q <words>")
        elif word == "!ai":
            if not arg:
                say("Usage: !ai <question>")
            elif not self.ai_enabled:
                self.io(lambda: say(self.library(arg)))
            elif now - self.last_ai.get(key, -1e9) < self.ai_cooldown:
                say(fit(f"One AI question every {int(self.ai_cooldown)} s, please. !q is instant."))
            elif not self.ai_busy.acquire(blocking=False):
                say("AI solver is busy with another question. Try again in a minute.")
            else:
                started = False
                try:
                    self.last_ai[key] = now
                    say("Thinking… the answer follows in a minute or so.")

                    def work():
                        try:
                            say(self.ai(arg))
                        except Exception as e:  # noqa: BLE001 — never let one question kill the bridge
                            print(f"meshbridge: !ai failed: {e}", file=sys.stderr)
                        finally:
                            self.ai_busy.release()
                    self.spawn(work)
                    started = True
                finally:
                    if not started:                       # the reply or the thread failed: free the solver
                        self.ai_busy.release()
        return True


# ─────────────────────────────── alerts ──────────────────────────────────────

def format_alert(a: dict) -> str:
    return fit(f"NOAA ⚠ {a.get('event', 'Weather alert')} · {a.get('areas', '')} · until {a.get('expires', '?')}. "
               f"More on the Badger's Wi-Fi.")


def alerts_to_send(alerts: list, sent: dict, now: float) -> list[dict]:
    """New, live, non-test alerts. `sent` maps alert id → expiry; expired (or garbled) ids are pruned in place.
    Entries with a bad id or expiry are skipped, never fatal."""
    for k in [k for k, exp in sent.items() if _num(exp) is None or exp < now]:
        del sent[k]
    out = []
    for a in alerts if isinstance(alerts, list) else []:
        if not isinstance(a, dict) or a.get("test"):
            continue
        aid, exp = a.get("id"), a.get("expires_ts", now + 1)
        if not isinstance(aid, str) or not aid or len(aid) > 200:
            continue
        if _num(exp) is None or exp <= now or aid in sent:
            continue
        out.append(a)
    return out


# ─────────────────────────────── positions ───────────────────────────────────

def _num(v):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        return v if math.isfinite(v) else None
    except OverflowError:                            # an int too big for a float
        return None


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def nodes_snapshot(nodes: dict, my_id: str | None, now: float, max_age: float = 7 * 86400) -> list[dict]:
    """Radios that shared a position, for the map: name, lat/lon, when, battery. Bad or ancient fixes dropped."""
    out = []
    for nid, n in (nodes.items() if isinstance(nodes, dict) else []):
        if not isinstance(n, dict):
            continue
        try:
            pos = _dict(n.get("position"))
            lat, lon = _num(pos.get("latitude")), _num(pos.get("longitude"))
            if lat is None and _num(pos.get("latitudeI")) is not None and _num(pos.get("longitudeI")) is not None:
                lat, lon = _num(pos["latitudeI"] / 1e7), _num(pos["longitudeI"] / 1e7)
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                continue
            heard = _num(pos.get("time")) or _num(n.get("lastHeard"))
            if heard is not None and now - heard > max_age:
                continue
            user = _dict(n.get("user"))
            uid = clean(user.get("id") or nid, 16)
            out.append({
                "id": uid, "name": clean(user.get("longName") or uid, 40), "short": clean(user.get("shortName") or "", 4),
                "lat": round(lat, 6), "lon": round(lon, 6), "heard": heard,
                "battery": _num(_dict(n.get("deviceMetrics")).get("batteryLevel")),
                "self": bool(my_id) and uid == my_id,
            })
        except (TypeError, ValueError, OverflowError):
            continue
    out.sort(key=lambda r: -(r["heard"] or 0))
    return out


# ─────────────────────────────── telemetry ───────────────────────────────────

TELEMETRY_KINDS = {"environmentMetrics": "environment", "deviceMetrics": "device", "powerMetrics": "power",
                   "airQualityMetrics": "air_quality"}
_FIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")


def telemetry_record(packet, family_ch: int, nodes, now: float) -> dict | None:
    """One line for mesh-telemetry.jsonl from a TELEMETRY_APP packet, or None if it isn't one we can trust
    the shape of. trusted: it came on the family channel. values: flat numbers only."""
    if not isinstance(packet, dict):
        return None
    sender = packet.get("fromId")
    if not isinstance(sender, str) or not NODE_RE.match(sender.strip().lower()):
        return None
    sender = sender.strip().lower()
    ch = packet.get("channel", 0)
    if ch is None:
        ch = 0
    if isinstance(ch, bool) or not isinstance(ch, int) or not 0 <= ch <= 7:
        return None
    dec = _dict(packet.get("decoded"))
    tel = dec.get("telemetry")
    if not isinstance(tel, dict):
        return None
    kind, metrics = "other", None
    for key, k in TELEMETRY_KINDS.items():
        if isinstance(tel.get(key), dict):
            kind, metrics = k, tel[key]
            break
    if metrics is None:
        metrics = next((v for k, v in tel.items() if isinstance(v, dict)), {})
    values = {}
    for k, v in list(metrics.items())[:64]:
        if isinstance(k, str) and _FIELD_RE.fullmatch(k) and _num(v) is not None:
            values[k] = v
        if len(values) >= 32:
            break
    if not values:
        return None
    node = _dict(nodes.get(sender) if isinstance(nodes, dict) else None) or \
        _dict(nodes.get(packet.get("fromId")) if isinstance(nodes, dict) else None)
    name = clean(_dict(node.get("user")).get("longName"), 40) or None
    return {"at": float(now), "from": sender, "name": name, "channel": ch,
            "trusted": family_ch >= 0 and ch == family_ch, "kind": kind, "values": values}


class TelemetryLog:
    """Appends telemetry lines (0640), at most one per radio per TELEMETRY_EVERY seconds, and keeps the
    file under TELEMETRY_MAX_BYTES by dropping its oldest half. Never raises into the radio thread."""

    def __init__(self, path: Path, family_ch: int, clock=None, every: float = TELEMETRY_EVERY,
                 max_bytes: int = TELEMETRY_MAX_BYTES):
        self.path, self.family_ch, self.every, self.max_bytes = Path(path), family_ch, every, max_bytes
        self.clock = clock or (lambda: time.time())
        self.last: dict[str, float] = {}
        self.lock = threading.Lock()

    def handle(self, packet, nodes=None) -> bool:
        try:
            now = self.clock()
            rec = telemetry_record(packet, self.family_ch, nodes or {}, now)
            if rec is None:
                return False
            with self.lock:
                if now - self.last.get(rec["from"], -1e18) < self.every:
                    return False
                self.last[rec["from"]] = now
                if len(self.last) > 2000:
                    for k in sorted(self.last, key=self.last.get)[:1000]:
                        del self.last[k]
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o640)
                try:
                    os.fchmod(fd, 0o640)
                    os.write(fd, (json.dumps(rec, allow_nan=False) + "\n").encode())
                finally:
                    os.close(fd)
                trim_jsonl(self.path, self.max_bytes, 0o640)
            return True
        except Exception as e:  # noqa: BLE001 — a hostile packet or a full disk must not stop the bridge
            print(f"meshbridge: telemetry not logged: {e}", file=sys.stderr)
            return False


# ─────────────────────────────── key rotation ────────────────────────────────

def rotate_family_key(iface, family_ch: int, rng=os.urandom) -> str | None:
    """A fresh 256-bit key on the family channel; returns the channel link (meshtastic.org/e/#…)
    that other family radios open or scan to follow (None if the radio wrote the key but the link
    can't be read yet: save_channel_link keeps trying). Their messages can't be read until they do."""
    if not isinstance(family_ch, int) or family_ch < 0:
        raise ValueError("no family channel is configured ([radio] mesh_family_channel)")
    if family_ch == 0:
        raise ValueError("channel 0 is the radio's primary (public) channel: it is never re-keyed here")
    node = iface.localNode
    chans = list(getattr(node, "channels", None) or [])
    if family_ch >= len(chans):
        raise ValueError(f"the radio has no channel {family_ch}")
    ch = chans[family_ch]
    try:
        role = int(getattr(ch, "role", 0) or 0)
    except (TypeError, ValueError):
        role = 0
    if role == 0:                                       # 0 = DISABLED in Meshtastic's channel protobuf
        raise ValueError(f"channel {family_ch} is disabled on the radio: set up the family channel first")
    ch.settings.psk = rng(32)
    node.writeChannel(family_ch)
    for attempt in range(3):                            # the key is on the radio now: get its link
        try:
            return node.getURL(includeAll=True)
        except Exception as e:  # noqa: BLE001 — radio busy / older library
            print(f"meshbridge: new key written, link not readable yet ({e})", file=sys.stderr)
            if attempt < 2:
                time.sleep(0.5)
    return None


def save_channel_link(iface, path: Path) -> str | None:
    """Keep the radio's current channel link where the admin page (over HTTPS) and the front panel can
    show it again (it contains the keys, so 0600). The failsafe after a key change: the link is never lost."""
    try:
        url = iface.localNode.getURL(includeAll=True)
    except Exception as e:  # noqa: BLE001 — older library / radio busy: try again later
        print(f"meshbridge: cannot read the channel link: {e}", file=sys.stderr)
        return None
    if not isinstance(url, str) or not url:
        return None
    try:
        _private_file(path, json.dumps({"url": url, "at": time.time()}).encode(), 0o600)
    except OSError as e:
        print(f"meshbridge: cannot save the channel link: {e}", file=sys.stderr)
    return url


_handled_requests: collections.OrderedDict = collections.OrderedDict()


def handle_rotation(iface, family_ch: int, req_path: Path, res_path: Path, link_path: Path | None = None) -> dict | None:
    """Act on a pending request from the portal, once; the result goes where the portal looks.
    A request is acted on at most once even if its file can't be deleted (its id is remembered and
    compared with the last result), and never when it is older than ROTATE_TTL seconds — a request
    left behind while the radio was unplugged must not re-key the family channel days later."""
    try:
        st = req_path.stat()
    except OSError:
        return None
    req = read_json(req_path, None)
    if not isinstance(req, dict) or not req.get("id"):
        return None
    rid = str(req["id"])[:32]
    sig = f"{rid}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}"     # this very request file, not just its id
    prev = read_json(res_path, None)
    if sig in _handled_requests or (isinstance(prev, dict) and prev.get("req") == sig):
        try:
            req_path.unlink()
        except OSError:
            pass
        return None
    _handled_requests[sig] = True
    while len(_handled_requests) > 200:
        _handled_requests.popitem(last=False)
    try:
        req_path.unlink()
    except OSError as e:
        print(f"meshbridge: cannot remove the key-change request ({e}); acting on it once only", file=sys.stderr)
    now = time.time()
    at = _num(req.get("at"))
    if at is None:
        at = st.st_mtime
    if abs(now - at) > ROTATE_TTL:
        res = {"id": rid, "ok": False, "error": "the request was too old (the radio bridge was away); nothing changed",
               "at": now, "req": sig}
        print("meshbridge: ignored a stale family key request", file=sys.stderr)
        atomic_json(res_path, res, 0o600)
        return res
    written, url = False, None
    try:
        url = rotate_family_key(iface, family_ch)
        written = True
        res = {"id": rid, "ok": True, "url": url, "at": time.time()}
        print("meshbridge: family channel has a new key", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — report any radio/library error to the admin page
        res = {"id": rid, "ok": False, "error": str(e)[:300], "at": time.time()}
        print(f"meshbridge: key rotation failed: {e}", file=sys.stderr)
    finally:
        if written and link_path:                       # refresh the saved link whatever happened after the write
            saved = save_channel_link(iface, link_path)
            if url is None:
                url = saved
                res["url"] = saved
                if saved is None:
                    res["warning"] = ("the key changed but the radio didn't give the new link yet; "
                                      "use Show family radio link in a minute")
    res["req"] = sig
    atomic_json(res_path, res, 0o600)                   # holds the link (the key): owner only
    return res


# ─────────────────────────────── Bluetooth PIN ───────────────────────────────

def secure_bt_pin(iface, path: Path, rng=None) -> str | None:
    """Many radios ship with Bluetooth on and the PIN 123456 (or no PIN): anyone in range could pair
    and read the family channel key. If so, give the radio a random 6-digit PIN and keep it (0600)
    where the front panel and the admin page (over HTTPS) show it. Returns the new PIN, or None.
    (The radio restarts to apply it; the bridge reconnects by itself.)"""
    try:
        cfg = iface.localNode.localConfig
        bt = cfg.bluetooth
        if not getattr(bt, "enabled", False):
            return None
        mode, pin = int(getattr(bt, "mode", 0) or 0), int(getattr(bt, "fixed_pin", 0) or 0)
        # Meshtastic: mode 0 = RANDOM_PIN (shown on the radio's own screen), 1 = FIXED_PIN, 2 = NO_PIN
        if mode == 0 or (mode == 1 and pin not in (0, 123456)):
            return None
        new = 100000 + (rng or secrets.randbelow)(900000)
        bt.mode, bt.fixed_pin = 1, new
        iface.localNode.writeConfig("bluetooth")
        _private_file(path, json.dumps({"pin": f"{new:06d}", "at": time.time()}).encode(), 0o600)
        print("meshbridge: the radio had the default Bluetooth PIN; it now has a random one "
              "(front panel → Radio Bluetooth PIN)", file=sys.stderr)
        return f"{new:06d}"
    except Exception as e:  # noqa: BLE001 — older library / radio without Bluetooth: leave it
        print(f"meshbridge: Bluetooth PIN left as it is ({e})", file=sys.stderr)
        return None


# ─────────────────────────────── outbox ──────────────────────────────────────

def load_offset(path: Path, default: int) -> int:
    """First run (no offset file): start at the end — old posts were never meant for the mesh."""
    try:
        return max(0, int(path.read_text().strip()))
    except (OSError, ValueError):
        return default


def save_offset(path: Path, pos: int) -> None:
    try:
        _private_file(path, str(pos).encode(), 0o640)
    except OSError as e:
        print(f"meshbridge: cannot save outbox position: {e}", file=sys.stderr)


class Bucket:
    """Token bucket: `burst` messages at once, then one every `every` seconds."""

    def __init__(self, burst: int, every: float, clock):
        self.burst, self.every, self.clock = burst, every, clock
        self.tokens, self.t = float(burst), None

    def take(self) -> bool:
        now = self.clock()
        if self.t is not None:
            self.tokens = min(self.burst, self.tokens + (now - self.t) / self.every)
        self.t = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Outbox:
    """Board posts marked "Send over mesh", read from the portal's outbox file into two queues —
    family first, community second — each paced by its own bucket, so visitors flooding the
    community board can neither hog the air nor hold up family messages. A post for a channel
    that isn't bridged is dropped (a community post never goes out on the family channel)."""
    CAPS = {"family": 50, "community": 20}

    def __init__(self, path: Path, family_ch: int, public_ch: int, send, clock):
        self.path, self.send = path, send
        self.ch = {"family": family_ch, "community": public_ch}
        self.q = {"family": collections.deque(), "community": collections.deque()}
        self.bucket = {"family": Bucket(10, 15.0, clock), "community": Bucket(5, 120.0, clock)}
        self.offset_file = path.with_name(path.name + ".offset")
        path.touch(exist_ok=True)
        self.pos = load_offset(self.offset_file, path.stat().st_size)

    def read(self) -> None:
        size = self.path.stat().st_size
        if size < self.pos:
            self.pos = 0
        if size == self.pos:
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.pos)
            while True:
                start = fh.tell()
                line = fh.readline(4096)
                if not line.endswith(b"\n"):
                    if len(line) >= 4096:              # an absurdly long line: skip it whole
                        fh.readline(); self.pos = fh.tell(); continue
                    break                              # a post still being written: read it next round
                self.pos = fh.tell()
                try:
                    post = json.loads(line)
                    text = str(post["text"])[:200]
                    board = "family" if post.get("board") == "family" else "community"
                except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                    continue                           # not a post: skip it
                if self.ch[board] < 0:
                    print(f"meshbridge: no {board} channel is bridged — a {board} post was not sent", file=sys.stderr)
                    continue
                if len(self.q[board]) >= self.CAPS[board]:
                    print(f"meshbridge: {board} radio queue full — a post was dropped", file=sys.stderr)
                    continue
                self.q[board].append((start, text))

    def pump(self) -> None:
        for board in ("family", "community"):
            while self.q[board] and self.bucket[board].take():
                _, text = self.q[board][0]
                if not self.send(text, channel=self.ch[board]):
                    self.bucket[board].tokens += 1     # not spent: the radio didn't take it
                    break                              # keep the post, retry next round
                self.q[board].popleft()

    def checkpoint(self) -> None:
        """Remember where to start after a restart: the oldest post not yet sent."""
        queued = [s for q in self.q.values() for s, _ in q]
        save_offset(self.offset_file, min(queued) if queued else self.pos)
        if not queued and self.pos > 65536:
            self.compact()

    def compact(self) -> None:
        """Everything sent: empty the file (under the lock the portal appends with)."""
        try:
            import fcntl
            with open(self.path, "r+b") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                fh.seek(0, os.SEEK_END)
                if fh.tell() == self.pos:
                    fh.truncate(0)
                    self.pos = 0
                    save_offset(self.offset_file, 0)
        except (ImportError, OSError) as e:
            print(f"meshbridge: cannot empty the outbox: {e}", file=sys.stderr)


class RateLimit:
    """Mesh-in posts: one per radio per `per_node` seconds, and at most `total` per `window` overall."""

    def __init__(self, clock, per_node: float = 20.0, total: int = 30, window: float = 600.0):
        self.clock, self.per_node, self.total, self.window = clock, per_node, total, window
        self.last: dict[str, float] = {}
        self.recent: collections.deque = collections.deque()

    def allow(self, sender: str) -> bool:
        now = self.clock()
        if now - self.last.get(sender, -1e18) < self.per_node:
            return False
        while self.recent and now - self.recent[0] > self.window:
            self.recent.popleft()
        if len(self.recent) >= self.total:
            return False
        self.last[sender] = now
        self.recent.append(now)
        if len(self.last) > 2000:
            for k in [k for k, t in self.last.items() if now - t > self.per_node]:
                del self.last[k]
        return True


class Bridge:
    """What happens to packets heard on the mesh (the pubsub callbacks)."""

    def __init__(self, family_ch: int, public_ch: int, send, commands: Commands, members: Path,
                 telemetry: TelemetryLog | None, my_id: str | None, clock):
        self.family_ch, self.public_ch, self.send, self.commands = family_ch, public_ch, send, commands
        self.members, self.telemetry, self.my_id = members, telemetry, my_id
        self.limits = {"family": RateLimit(clock), "community": RateLimit(clock)}   # one per board

    def on_text(self, packet, interface=None):
        try:
            self._on_text(packet, interface)
        except Exception as e:  # noqa: BLE001 — one odd packet must never take the bridge down
            print(f"meshbridge: ignored a packet ({type(e).__name__}: {e})", file=sys.stderr)

    def _on_text(self, packet, interface):
        if not isinstance(packet, dict):
            return
        dec = packet.get("decoded")
        text = dec.get("text") if isinstance(dec, dict) else None
        if not isinstance(text, str) or not text.strip():
            return
        text = text.strip()
        sender = packet.get("fromId")
        sender = clean(sender, 16) if isinstance(sender, str) and sender.strip() else "?"
        ch = packet.get("channel", 0)
        if ch is None:
            ch = 0
        if isinstance(ch, bool) or not isinstance(ch, int):
            return                                   # not a channel index we understand
        to = packet.get("toId")
        direct = isinstance(to, str) and to not in ("", "^all") or (bool(self.my_id) and to == self.my_id)
        reg = radio_registry(self.members).get(sender.lower())
        if reg and reg.get("removed"):
            print(f"meshbridge: ignored a message from a removed member's radio {sender}", file=sys.stderr)
            return
        board, trusted = route_text(text, ch, self.family_ch, self.public_ch, reg)
        if text.startswith("!"):
            # replies go to the asker only, on the channel the request came in on
            self.commands.handle(text, sender, trusted, reply=lambda t, ch=ch: self.send(t, sender, ch))
            return
        if direct or board == "ignore":
            return                                   # direct messages and other channels: commands only
        if not self.limits[board].allow(sender.lower()):
            return
        nodes = getattr(interface, "nodes", None) if interface is not None else None
        node = nodes.get(sender, {}) if isinstance(nodes, dict) else {}
        long_name = clean(_dict(_dict(node).get("user")).get("longName"), 32)
        if board == "family" and reg:
            board_add(reg["name"] or long_name or sender, text, board, sender, reg["member"])
        else:                                        # community: never a member's name (IDs can be forged)
            board_add(long_name or sender, text, board, sender, None)

    def on_telemetry(self, packet, interface=None):
        if self.telemetry is not None:
            self.telemetry.handle(packet, getattr(interface, "nodes", None) if interface is not None else None)


def main() -> int:
    global BOARD, FAMILY_BOARD, MEMBERS, OUTBOX, NODES, ALERTS, ROTATE_REQ, ROTATE_RES, CHANNEL_LINK, TELEMETRY, BT_PIN
    cfg_path = Path(os.environ.get("PREPPER_CONFIG", "/etc/prepper/prepper.toml"))
    raw = tomllib.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg, portal = raw.get("radio", {}), raw.get("portal", {})
    BOARD = Path(os.environ.get("BOARD_FILE") or portal.get("board_file") or BOARD)
    FAMILY_BOARD = Path(os.environ.get("FAMILY_BOARD_FILE") or portal.get("family_board_file") or FAMILY_BOARD)
    MEMBERS = Path(os.environ.get("MEMBERS_FILE") or portal.get("members_file") or MEMBERS)
    OUTBOX = Path(os.environ.get("MESH_OUTBOX") or portal.get("mesh_outbox") or OUTBOX)
    NODES = Path(os.environ.get("MESH_NODES") or portal.get("mesh_nodes") or NODES)
    TELEMETRY = Path(os.environ.get("MESH_TELEMETRY") or portal.get("mesh_telemetry") or TELEMETRY)
    ALERTS = Path(os.environ.get("ALERTS_FILE") or portal.get("alerts_file") or ALERTS)
    ROTATE_REQ = Path(portal.get("mesh_rotate_request") or ROTATE_REQ)
    ROTATE_RES = Path(portal.get("mesh_rotate_result") or ROTATE_RES)
    CHANNEL_LINK = Path(portal.get("mesh_channel_link") or CHANNEL_LINK)
    BT_PIN = Path(portal.get("mesh_bt_pin_file") or BT_PIN)
    if cfg.get("mesh") == "off":
        return 0
    family_ch, public_ch, notes = channel_config(cfg)
    for n in notes:
        print(f"meshbridge: {n}", file=sys.stderr)
    alerts_public = cfg.get("mesh_alerts_public", True) is not False
    relay_alerts = cfg.get("mesh_alerts", True) is not False
    share_map = cfg.get("mesh_map", True) is not False
    try:
        import meshtastic.serial_interface
        from pubsub import pub
    except ImportError:
        print("meshbridge: meshtastic python package missing", file=sys.stderr)
        time.sleep(3600); return 0

    while True:                                      # wait for a node to be plugged in
        try:
            iface = meshtastic.serial_interface.SerialInterface()
            break
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001 — no device yet (the library calls sys.exit for that)
            print(f"meshbridge: no Meshtastic node ({e}); retrying in 60 s", file=sys.stderr)
            time.sleep(60)

    send_lock = threading.Lock()

    def send(text: str, dest: str | None = None, channel: int | None = None) -> bool:
        if channel is None:
            channel = family_ch if family_ch >= 0 else public_ch
        if not isinstance(channel, int) or channel < 0:
            return False
        try:
            with send_lock:                          # the serial link isn't safe to share between threads
                if dest:
                    iface.sendText(fit(text), destinationId=dest, channelIndex=channel)
                else:
                    iface.sendText(fit(text), channelIndex=channel)
            return True
        except Exception as e:  # noqa: BLE001 — serial/radio errors: caller decides whether to retry
            print(f"meshbridge: send failed ({e})", file=sys.stderr)
            return False

    # Slow answers (library search, status) run here, one at a time, never on the radio's receive thread.
    io_q: queue.Queue = queue.Queue(maxsize=8)

    def io_worker():
        while True:
            fn = io_q.get()
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"meshbridge: command failed: {e}", file=sys.stderr)
    threading.Thread(target=io_worker, daemon=True).start()

    def io(fn):
        try:
            io_q.put_nowait(fn)
        except queue.Full:
            print("meshbridge: command queue full — a request was dropped", file=sys.stderr)

    settings_file = Path(os.environ.get("PORTAL_SETTINGS") or portal.get("settings_file")
                         or "/var/lib/prepper/portal/portal-settings.json")
    try:
        ai_cooldown = float(cfg.get("mesh_ai_cooldown_s", 120))
    except (TypeError, ValueError):
        ai_cooldown = 120.0
    def access():                                    # re-read on every command: admin changes apply at once
        s = read_json(settings_file, {})
        return command_access(cfg, s.get("mesh_commands") if isinstance(s, dict) else None)
    commands = Commands(send, ai_enabled=cfg.get("mesh_ai", True) is not False, ai_cooldown=ai_cooldown, io=io,
                        access=access)
    try:
        my_id = (iface.getMyNodeInfo() or {}).get("user", {}).get("id")
    except Exception:  # noqa: BLE001 — older library versions
        my_id = None
    clock = lambda: time.time()  # noqa: E731 — (tests swap the module's time)
    bridge = Bridge(family_ch, public_ch, send, commands, MEMBERS,
                    TelemetryLog(TELEMETRY, family_ch, clock), my_id, clock)
    lost = []

    def on_lost(interface=None, **_):
        lost.append(True)

    pub.subscribe(bridge.on_text, "meshtastic.receive.text")
    pub.subscribe(bridge.on_telemetry, "meshtastic.receive.telemetry")
    pub.subscribe(on_lost, "meshtastic.connection.lost")
    print("meshbridge: connected", file=sys.stderr)
    save_channel_link(iface, CHANNEL_LINK)
    if cfg.get("mesh_bt_pin", "secure") != "leave":
        secure_bt_pin(iface, BT_PIN)
    # Where we got to, kept next to the outbox: posts written while the bridge
    # was down (node unplugged, Pi rebooting) are still sent.
    outbox = Outbox(OUTBOX, family_ch, public_ch, send, clock)
    sent_file = OUTBOX.with_name("mesh-alerts-sent.json")
    sent_alerts = read_json(sent_file, {})
    if not isinstance(sent_alerts, dict):
        sent_alerts = {}
    last_snapshot, last_link = 0.0, time.time()

    def step(what, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a bad file or a radio hiccup: log, carry on next round
            print(f"meshbridge: {what} failed: {type(e).__name__}: {e}", file=sys.stderr)

    while True:
        if lost:
            BEAT.unlink(missing_ok=True)
            print("meshbridge: node disconnected — restarting", file=sys.stderr)
            return 1                                 # systemd restarts us; we wait for the node again
        step("heartbeat", lambda: (BEAT.parent.mkdir(parents=True, exist_ok=True), BEAT.touch()))
        now = time.time()

        def rotation():
            with send_lock:                          # don't interleave with a send on the serial link
                handle_rotation(iface, family_ch, ROTATE_REQ, ROTATE_RES, CHANNEL_LINK)
        step("key change", rotation)

        # NOAA alerts → one broadcast each, remembered across restarts until they expire
        def alerts(now=now):
            for a in alerts_to_send(read_json(ALERTS, []), sent_alerts, now):
                ok = send(format_alert(a), channel=family_ch) if family_ch >= 0 else False
                if alerts_public and public_ch >= 0:
                    ok = send(format_alert(a), channel=public_ch) or ok
                if ok:
                    sent_alerts[a["id"]] = a.get("expires_ts", now + 6 * 3600)
                    print(f"meshbridge: relayed {clean(a.get('event'), 60)} to the mesh", file=sys.stderr)
                    atomic_json(sent_file, sent_alerts)
        if relay_alerts:
            step("alerts", alerts)
        # positions → the offline map
        if share_map and now - last_snapshot >= 30:
            last_snapshot = now
            step("map snapshot", lambda now=now: atomic_json(NODES, {"updated": now, "nodes": nodes_snapshot(
                dict(iface.nodes or {}), my_id, now)}))
        # the channel link, kept fresh (it changes when someone edits channels in the Meshtastic app)
        if now - last_link >= 600:
            last_link = now
            step("channel link", lambda: save_channel_link(iface, CHANNEL_LINK))
        # board posts marked "Send over mesh"
        step("outbox", lambda: (outbox.read(), outbox.pump(), outbox.checkpoint()))
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
