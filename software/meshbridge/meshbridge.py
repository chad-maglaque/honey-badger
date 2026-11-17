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
    [radio] mesh_commands: "community" (anyone), "family" (sent on the family channel),
    "off" (family radios are told it is off) or "ignore" (no answer to anyone).
    Defaults: !q community; !help, !ai and !status family.
      !help           what this radio may ask
      !q <words>      top library match: title + first ~150 characters
      !ai <question>  a short answer from the built-in AI solver, grounded in the library
      !status         battery, power source, mode, alerts, people on the Wi-Fi

Strangers (prime directive: the Badger must never become a beacon for people hunting it):
a radio outside the family never gets a refusal, a "usage" line or a "busy" line — only a real
answer to a command opened to the community, and only within small budgets (by default 6
answers an hour to all strangers together, 2 to any one radio, so forged sender IDs can't make
it transmit on demand). Their answers go through the mesh courtesy and are broadcast on the
public channel as a reply to the question (other Badgers hear it and stay quiet); a stranger's
direct message is answered (if at all) after a short wait, never at once. Only family radios
(the family channel's key) get immediate answers. No over-the-air text names the Badger.

Trust: Meshtastic's sender ID ("fromId") is NOT authenticated on channel traffic —
anyone can transmit with any ID. Only the family channel's private key proves
anything, so "family" means "arrived on the family channel", never "came from a
registered radio ID". Mesh posts are never marked verified; member names are put
only on posts that arrived on the family channel — and not even there while a removed
member may still hold the family key (removed after the last key change): the post is then
labelled "(unconfirmed)" and the family channel isn't trusted for commands until re-keyed.
Direct messages to the Badger are commands only, never board posts.
Radio pairing: "!pair <code>" heard on the family channel (not as a DM) is written to mesh-pair.json
([portal] mesh_pair_file) for the portal, which binds a radio to a member only after such a message
from that radio; it is never answered on air or posted. "!pair" anywhere else — or while a removed
member may still hold the family key (removed after the last re-key) — is dropped silently.
Replies are short on purpose: LoRa carries ~200 bytes per message.

Many Badgers, one mesh ("Badger-mesh courtesy", docs/MESH.md; [radio] mesh_courtesy = true):
  * Control packets (beacon, claim, done) go ONLY on the family channel or a configured private
    circle channel ([radio] mesh_circle_channel), never on the public one. They are sealed with a
    key derived from that channel's own secret (random-looking bytes, no fixed magic) and padded
    to random lengths typical of short text messages; beacons are sent at Poisson-random times.
  * Anything several Badgers would all send (a NOAA alert relay, the reply to "!q" on a shared
    channel, the forecast, a missing-person note) waits a back-off, then sends — unless meanwhile
    another Badger's copy was heard. On a private channel Badgers also "claim" first (lower node
    wins a tie) and, for low-stakes kinds only, a claim/"done" stands them down. On the public
    channel coordination is by hearing the text itself: Badgers take turns in an order derived
    from the message key and the Badgers they know, one turn every few seconds.
  * Safety rule: a NOAA alert (or sensor danger) is only ever cancelled by HEARING exactly the text
    this Badger would send (normalised full-text match: event, areas, expiry and tag). Anything else
    — "CANCELLED, all clear #tag" with the right tag — is relayed past. A claim can delay it (at
    most SAFETY_HOLD_MAX s), never cancel it.
  * Every transmission goes through one priority queue (alert > sensor danger > family > reply >
    community > automatic notes) with hourly budgets that shrink as more Badgers are heard, and
    backs off when the radio reports a busy channel. Stale items expire instead of going out late.
  * Replies and automatic notes use a small hop limit; alerts and family posts the radio default.
  * After a restart, alerts that were already live wait a random 30–120 s quiet period first.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import inspect
import json
import math
import os
import queue
import random
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
PEERS = Path("/var/lib/prepper/portal/mesh-peers.json")            # Badgers heard + courtesy status: 0640
PAIR_FILE = Path("/var/lib/prepper/portal/mesh-pair.json")         # radio pairing confirmations for the portal: 0600
API = os.environ.get("PREPPER_API", "http://127.0.0.1:8000")
MAX_BYTES = 200                                   # what one LoRa text message carries
ROTATE_TTL = 120                                  # a key-change request older than this is ignored
BOARD_MAX_BYTES = 2_000_000                       # (same limit the portal keeps its boards under)
TELEMETRY_MAX_BYTES = 1_000_000
TELEMETRY_EVERY = 30.0                            # at most one telemetry line per radio per 30 s
NODE_RE = re.compile(r"^![0-9a-f]{8}$")

COMMANDS = {"!help": "what you can ask", "!q": "<words> searches the library",
            "!ai": "<question> asks the AI solver", "!status": "battery and alerts"}
# "community": anyone (strangers within the stranger budgets, through the courtesy); "family": family
# radios only, everyone else silently ignored; "off": family radios are told it is off, everyone else
# ignored; "ignore": nobody gets any reply at all. A stranger never gets a refusal of any kind.
COMMAND_WORDS = tuple(COMMANDS)
DEFAULT_ACCESS = {"!help": "family", "!q": "community", "!ai": "family", "!status": "family"}
LEVELS = ("community", "family", "off", "ignore")
STRANGER_BUDGET = (6, 3600.0)            # answers to radios outside the family, all together, per window
STRANGER_PER_RADIO = (2, 3600.0)         # … and to any one of them (forged IDs: the global cap holds)
STRANGER_DM_DELAY = (3.0, 12.0)          # a stranger's direct message is never answered at once


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
                  f"levels: {', '.join(LEVELS)})", file=sys.stderr)
    if access["!help"] == "off":
        access["!help"] = "family"                   # family radios can always ask what's available
    return access


def help_text(access: dict[str, str], trusted: bool) -> str:
    """Only the commands this radio may use. Says nothing about what this node is."""
    usable = [w for w in ("!q", "!ai", "!status") if access.get(w) == "community" or (trusted and access.get(w) == "family")]
    if not usable:
        return "Sorry, no radio commands are available to you."
    return fit("Commands: " + " · ".join(f"{w} {COMMANDS[w]}" for w in usable) + ".")


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
    # 0640 (group prepper only) even when this creates the file; an older world-readable one is tightened
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
    with os.fdopen(fd, "a") as fh:
        try:
            if os.fstat(fd).st_mode & 0o007:
                os.fchmod(fd, 0o640)
        except OSError:
            pass
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
    return fit("Status: " + " · ".join(parts))


def status_answer() -> str:
    try:
        s = api_get("/api/status")
        return format_status(s if isinstance(s, dict) else {})
    except (OSError, ValueError):
        return "Status unavailable."


class Commands:
    """Routes !commands with per-sender rate limits. Family radios (trusted: the family channel) get
    answers and, where a command is off, a short "turned off". Radios outside the family get a real
    answer to a "community" command or nothing at all — never a refusal, usage or busy line — and
    at most `public_budget` answers per window all together, `per_radio_budget` each (so a stream
    of forged sender IDs can't turn the Badger into a beacon or an airtime amplifier). !ai runs one
    at a time in a worker thread; `io` runs the other slow answers (library, status) — by default
    inline; meshbridge's main() hands them to a worker so the radio's receive thread is never blocked."""

    def __init__(self, send, ai_enabled: bool = True, cooldown: float = 30, ai_cooldown: float = 120,
                 clock=time.time, library=library_answer, ai=ai_answer, status=status_answer, spawn=None,
                 access=None, io=None, public_budget: tuple[int, float] = STRANGER_BUDGET,
                 per_radio_budget: tuple[int, float] = STRANGER_PER_RADIO):
        self.send, self.ai_enabled = send, ai_enabled
        # a dict, or a function returning one (re-read on every command, so admin changes apply at once)
        self._access = access if callable(access) else (lambda a=dict(access or DEFAULT_ACCESS): a)
        self.cooldown, self.ai_cooldown, self.clock = cooldown, ai_cooldown, clock
        self.library, self.ai, self.status = library, ai, status
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.io = io or (lambda fn: fn())
        self.budget, self.budget_used = public_budget, collections.deque()
        self.per_radio = per_radio_budget
        self.stranger_used: dict[str, collections.deque] = {}
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

    def _stranger_room(self, key: str, now: float, charge: bool) -> bool:
        """Is there room in the stranger budgets for one more answer to `key`? (lock held)"""
        n, window = self.budget
        while self.budget_used and now - self.budget_used[0] > window:
            self.budget_used.popleft()
        if len(self.budget_used) >= n:
            return False
        pn, pwin = self.per_radio
        used = self.stranger_used.get(key)
        if used is not None:
            while used and now - used[0] > pwin:
                used.popleft()
            if len(used) >= pn:
                return False
        if charge:
            self.budget_used.append(now)
            self.stranger_used.setdefault(key, collections.deque()).append(now)
            if len(self.stranger_used) > 2000:
                for k in [k for k, d in self.stranger_used.items() if not d or now - d[-1] > pwin]:
                    del self.stranger_used[k]
        return True

    def level(self, word: str) -> tuple[str, dict]:
        """(level, access map) for one command word; unknown or garbled levels fail closed ("off")."""
        try:
            access = self._access()
        except Exception:  # noqa: BLE001 — a broken settings reader: nothing is answered
            access = {}
        access = access if isinstance(access, dict) else {}
        lv = access.get(word)
        if not isinstance(lv, str) or lv not in LEVELS:
            lv = "off"
        if word == "!help" and lv == "off":
            lv = "family"
        return lv, access

    def note_answered(self, sender) -> None:
        """Another Badger answered this radio: count it as if we had, so a radio that floods questions
        gets one answer per cooldown from the whole mesh, not one from each Badger."""
        with self._lock:
            self.last[self._key(sender)] = self.clock()
            self._prune(self.clock())

    def would_answer(self, text, trusted: bool, sender=None) -> bool:
        """Would this Badger really answer (not refuse, not stay silent)? For a stranger: only a
        "community" command with something to answer, while the stranger budgets have room — anything
        else is silently ignored (the bridge then doesn't even take part in the courtesy). For family:
        False when it would only say "turned off" (so a Badger that can answer goes first)."""
        word, _, arg = str(text).strip().partition(" ")
        word = word.lower()
        if word not in COMMAND_WORDS:
            return False
        lv, _ = self.level(word)
        if not (lv == "community" or (lv == "family" and trusted)):
            return False
        if trusted:
            return True
        if word in ("!q", "!ai") and not arg.strip():
            return False
        with self._lock:
            return self._stranger_room(self._key(sender), self.clock(), charge=False)

    def handle(self, text, sender, trusted: bool = True, reply=None, ignored=None) -> bool:
        """Returns True when the text was a command this bridge dealt with (answered, refused to a family
        radio, or ignored). trusted: it arrived on the family channel. ignored(): called when a flood is
        quietly ignored, so the mesh courtesy can tell other Badgers (on a private channel) it is dealt with."""
        if not isinstance(text, str):
            return False
        say = reply or (lambda t: self.send(t, sender))
        quiet = ignored or (lambda: None)
        text = text.strip()
        if not text.startswith("!"):
            return False
        word, _, arg = text.partition(" ")
        word, arg = word.lower(), arg.strip()
        if word not in COMMAND_WORDS:
            return False
        lv, access = self.level(word)
        answers = lv == "community" or (lv == "family" and trusted)
        if lv == "ignore" or (not trusted and (not answers or (word in ("!q", "!ai") and not arg))):
            return True                                   # strangers: no refusal, no usage line — nothing
        key = self._key(sender)
        with self._lock:
            now = self.clock()
            flood = now - self.last.get(key, -1e9) < self.cooldown       # quietly ignore floods
            if not flood and not trusted:
                flood = not self._stranger_room(key, now, charge=True)   # budgets spent: stay quiet
            if not flood:
                self.last[key] = now
                self._prune(now)
        if flood:
            quiet()
            return True
        if lv == "off":                                   # (only family radios get this far)
            say(f"{word} is turned off. Send !help for what's available.")
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
                if trusted:
                    say(fit(f"One AI question every {int(self.ai_cooldown)} s, please. !q is instant."))
            elif not self.ai_busy.acquire(blocking=False):
                if trusted:
                    say("AI solver is busy with another question. Try again in a minute.")
            else:
                started = False
                try:
                    self.last_ai[key] = now
                    if trusted:
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

ALERT_PREFIX = "NOAA ⚠"


def format_alert(a: dict, tagged: bool = True) -> str:
    """The relay text. Ends with " #a1b2", a short tag of the alert's id (the same on every Badger that
    decoded the same SAME message), so Badgers recognise each other's relays exactly (docs/MESH.md)."""
    body = (f"{ALERT_PREFIX} {a.get('event', 'Weather alert')} · {a.get('areas', '')} · until {a.get('expires', '?')}. "
            f"Tune a weather radio for details.")
    aid = a.get("id") if isinstance(a, dict) else None
    if tagged and isinstance(aid, str) and aid:
        return with_tag(body, tag_of(alert_key(aid)))
    return fit(body)


def alert_key(aid: str) -> str:
    return f"alert:{aid}"


def canon(text) -> str:
    """Text as compared for the safety rule: Unicode NFKC, whitespace collapsed, case folded."""
    return " ".join(unicodedata.normalize("NFKC", str(text if text is not None else "")).split()).casefold()


def exact_matcher(text):
    """text → True only for a normalised full-text copy of `text` (the safety rule for alert/danger)."""
    want = canon(text)

    def match(heard) -> bool:
        return bool(want) and canon(heard) == want
    return match


def alert_matcher(a: dict):
    """text → True only when it is EXACTLY the relay this Badger would send for this alert (event, areas,
    expiry and tag), after normalisation (canon). Alert ids and the tag are computable by anyone who
    decodes the same NOAA broadcast, so a tag or an event name proves nothing: "NOAA ⚠ Tornado Warning ·
    CANCELLED, all clear #a1b2" never matches, and neither does the older untagged format of Badgers
    before the courtesy update (they may relay a second copy — duplicates are safe, silence is not)."""
    return exact_matcher(format_alert(a))


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


# ─────────────────────────────── Badger-mesh courtesy ────────────────────────
# How MANY Badgers share one mesh without flooding it (docs/MESH.md). Everything here is pure logic
# with an injected clock / random source / radio, so tests can run 20 Badgers on a fake channel.
# Nothing on the air is authenticated by the mesh itself: a hostile radio can transmit any text with any
# sender ID. Control packets (beacon/claim/done) exist only on private channels (family / circle) and are
# sealed with a key from that channel's secret; on the public channel Badgers coordinate only by hearing
# the actual text. A forger can make Badgers quieter for low-stakes traffic (replies, forecasts) but never
# cancel a NOAA alert: only hearing exactly the text this Badger would send does that (alert_matcher), and
# a claim (private channels only) delays an alert by SAFETY_HOLD_MAX at most.

PRIVATE_APP = 256                       # Meshtastic PortNum.PRIVATE_APP: not shown by chat apps
COURTESY_PROTO = (2, 0)                 # version of this courtesy protocol (in every beacon)
CTRL_VERSION = 2                        # v1 ("HB" magic, fixed sizes) is gone: it named the device
CTRL_BEACON, CTRL_CLAIM, CTRL_DONE = 1, 2, 3
CTRL_NONCE, CTRL_MAC = 4, 6             # bytes
CTRL_MIN, CTRL_MAX = 20, 96             # sealed packets are padded to a random length in this range
CTRL_LEN_MEDIAN, CTRL_LEN_SPREAD = 30.0, 0.45   # log-normal, like the lengths of short chat messages
KINDS = ("alert", "danger", "family", "reply", "community", "auto")      # priority order, highest first
SAFETY = frozenset({"alert", "danger"})  # only the real message (heard) cancels these, never a claim/done
TAG_RE = re.compile(r"(?:^|\s)#([0-9a-f]{4})$")
PEER_WINDOW = 7200                      # a Badger heard within 2 h counts as nearby
PRESUME_WINDOW = 7 * 86400              # after a restart, peers heard within a week are assumed still there…
PRESUME_FOR = 3600                      # …for the first hour (a town-wide power cut: everyone boots at once)
PEER_COUNT_CAP = 50                     # forged beacons can't make a Badger wait forever
# back-off before claiming, seconds: (lowest, extra per active peer, highest)
BACKOFF = {"alert": (5.0, 2.0, 45.0), "danger": (5.0, 2.0, 45.0), "reply": (1.0, 0.5, 10.0),
           "family": (5.0, 3.0, 60.0), "community": (10.0, 5.0, 120.0), "auto": (30.0, 30.0, 900.0)}
# channels without control packets (the public one): Badgers take turns, one every TURN seconds, in an
# order every Badger derives the same way from the message key and the Badgers it knows (capped by BACKOFF)
TURN = {"alert": 4.0, "danger": 4.0, "reply": 3.0, "family": 4.0, "community": 6.0, "auto": 20.0}
CLAIM_TTL = {"alert": 30, "danger": 30, "reply": 30, "family": 60, "community": 60, "auto": 120}
SETTLE = 6.0                            # after our claim: time for a simultaneous claim to reach us (multi-hop)
SAFETY_HOLD_MAX = 60.0                  # claims can hold an alert back this long in total, then it goes
LONE_ALERT_JITTER = 5.0                 # no Badger heard yet: alerts still wait 0–5 s (others may be booting)
BUDGET_DEFAULT = {"community": 30.0, "reply": 120.0, "auto": 4.0}         # per hour, for a Badger alone
BUDGET_MIN = {"community": 4.0, "reply": 10.0, "auto": 0.25}             # never squeezed below this
CTRL_PER_SENDER = (30, 600.0)           # control packets we listen to per radio per 10 min


def norm(text) -> str:
    return " ".join(str(text if text is not None else "").split())


def _th(text) -> str:
    """A short hash of a text's canonical form (canon)."""
    return hashlib.sha256(canon(text).encode("utf-8", "replace")).hexdigest()[:16]


KEY_SALT = b"honey-badger mesh courtesy v1"     # fixed: every Badger must compute the same opaque hash
KH_LEN = 6


def key_hash(key: str) -> bytes:
    """An opaque 6-byte HMAC of a dedupe key: claims and "done"s on the air never carry readable key text
    (no alert id, no sender, no post id) — only Badgers that hold the same item can match it."""
    return hmac.new(KEY_SALT, str(key).encode("utf-8", "replace"), hashlib.sha256).digest()[:KH_LEN]


def tag_of(key: str) -> str:
    """The 4-hex tag every Badger computes for the same key (alert id, forecast slot, post id)."""
    return key_hash(key)[:2].hex()


def with_tag(text, tag: str) -> str:
    text = norm(text)
    m = TAG_RE.search(text)
    if m and m.group(1) == tag:
        return fit(text)
    suffix = f" #{tag}"
    return fit(text, MAX_BYTES - len(suffix.encode())) + suffix


def node_num(v) -> int | None:
    """A radio's node number from an int or a "!a1b2c3d4" id; None if it isn't one."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v if 0 < v < 2 ** 32 else None
    if isinstance(v, str) and NODE_RE.match(v.strip().lower()):
        return int(v.strip()[1:], 16) or None
    return None


def hops_away(packet) -> int | None:
    """How many relays a packet went through (firmware 2.3+ sets hopStart); None if unknown."""
    if not isinstance(packet, dict):
        return None
    hs, hl = packet.get("hopStart"), packet.get("hopLimit")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (hs, hl)) or not 0 <= hl <= hs <= 7:
        return None
    return hs - hl


# Wire format v2 (docs/MESH.md). Only ever sent on a private channel (family / circle), where Meshtastic
# already encrypts it with the channel key. On top of that each packet is sealed with a key derived from
# that channel's own secret, so it carries no constant marker anyone could recognise, and padded with
# random bytes to a random length typical of a short chat message:
#   nonce(4, random) · mac(6) = HMAC(key, "m"·nonce·body) · body XOR HMAC(key, "s"·nonce) · random padding
#   body: beacon  ver=2 type=1 proto_major proto_minor        4 bytes
#         claim   ver=2 type=2 keyhash(6) ttl_seconds(1)        9 bytes
#         done    ver=2 type=3 keyhash(6)                       8 bytes
# Without the channel secret the bytes are indistinguishable from random; the length says nothing.
_BODY_LEN = {CTRL_BEACON: 4, CTRL_CLAIM: 2 + KH_LEN + 1, CTRL_DONE: 2 + KH_LEN}
_SYSRNG = random.SystemRandom()
_FALLBACK_SECRET = b"mesh courtesy v2 / no channel secret"   # only when the radio's key can't be read


def ctrl_key(secret: bytes | None = None) -> bytes:
    """The sealing key for one channel, from that channel's own secret (its PSK). Without one (the radio
    didn't say), a fixed key: still no plain marker, but recognisable to someone who knows this code."""
    raw = bytes(secret) if isinstance(secret, (bytes, bytearray)) and len(secret) >= 16 else _FALLBACK_SECRET
    return hmac.new(raw, b"mesh courtesy control v2", hashlib.sha256).digest()


DEFAULT_CTRL_KEY = ctrl_key(None)


def _ks(key: bytes, nonce: bytes) -> bytes:
    return hmac.new(key, b"s" + nonce, hashlib.sha256).digest()


def _mac(key: bytes, nonce: bytes, body: bytes) -> bytes:
    return hmac.new(key, b"m" + nonce + body, hashlib.sha256).digest()[:CTRL_MAC]


def ctrl_length(rng, need: int) -> int:
    """A random packet length like a short chat text (median ~30 bytes), never below what the body needs."""
    n = int(round(rng.lognormvariate(math.log(CTRL_LEN_MEDIAN), CTRL_LEN_SPREAD)))
    return max(need, CTRL_MIN, min(CTRL_MAX, n))


def seal(body: bytes, key: bytes | None = None, rng=None) -> bytes:
    rng = rng or _SYSRNG
    k = key or DEFAULT_CTRL_KEY
    while True:
        nonce = rng.randbytes(CTRL_NONCE)
        if nonce[:2] != b"HB":                       # never the old v1 magic: old Badgers mustn't misread it
            break
    head = nonce + _mac(k, nonce, body) + bytes(a ^ b for a, b in zip(body, _ks(k, nonce), strict=False))
    return head + rng.randbytes(ctrl_length(rng, len(head)) - len(head))


def encode_beacon(key: bytes | None = None, rng=None) -> bytes:
    return seal(bytes([CTRL_VERSION, CTRL_BEACON, COURTESY_PROTO[0], COURTESY_PROTO[1]]), key, rng)


def encode_claim(kh: bytes, ttl: int, key: bytes | None = None, rng=None) -> bytes:
    body = bytes([CTRL_VERSION, CTRL_CLAIM]) + bytes(kh[:KH_LEN]).ljust(KH_LEN, b"\0") + bytes([max(1, min(255, int(ttl)))])
    return seal(body, key, rng)


def encode_done(kh: bytes, key: bytes | None = None, rng=None) -> bytes:
    return seal(bytes([CTRL_VERSION, CTRL_DONE]) + bytes(kh[:KH_LEN]).ljust(KH_LEN, b"\0"), key, rng)


def decode_control(payload, key: bytes | None = None) -> dict | None:
    """A control packet from another Badger holding the same channel secret, or None for anything else
    (garbled, foreign, forged without the key, oversized, v1). Padding is ignored."""
    if isinstance(payload, str):
        if len(payload) > 2 * CTRL_MAX:
            return None
        try:
            payload = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return None
    b = bytes(payload)
    head = CTRL_NONCE + CTRL_MAC
    if not head + 2 <= len(b) <= CTRL_MAX:
        return None
    k = key or DEFAULT_CTRL_KEY
    nonce, mac = b[:CTRL_NONCE], b[CTRL_NONCE:head]
    ks = _ks(k, nonce)
    ver, t = b[head] ^ ks[0], b[head + 1] ^ ks[1]
    n = _BODY_LEN.get(t)
    if ver != CTRL_VERSION or n is None or len(b) < head + n:
        return None
    body = bytes(x ^ y for x, y in zip(b[head:head + n], ks, strict=False))
    if not hmac.compare_digest(mac, _mac(k, nonce, body)):
        return None
    if t == CTRL_BEACON:
        return {"type": "beacon", "proto": f"{body[2]}.{body[3]}"}
    if t == CTRL_CLAIM:
        return {"type": "claim", "kh": body[2:2 + KH_LEN], "ttl": body[2 + KH_LEN]}
    return {"type": "done", "kh": body[2:2 + KH_LEN]}


def _cnum(v, default, lo, hi, key=""):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        if v is not None:
            print(f"meshbridge: ignoring [radio] {key} = {v!r} (a number from {lo} to {hi})", file=sys.stderr)
        return default
    return max(lo, min(hi, v))


def courtesy_config(cfg: dict | None) -> dict:
    """[radio] keys for the mesh courtesy, validated; sane defaults with no config at all."""
    cfg = cfg if isinstance(cfg, dict) else {}
    budget = dict(BUDGET_DEFAULT)
    src = cfg.get("mesh_budget")
    for k, v in (src.items() if isinstance(src, dict) else []):
        if k in budget:
            budget[k] = float(_cnum(v, budget[k], 0, 3600, f"mesh_budget.{k}"))
    quiet = cfg.get("mesh_boot_quiet_s", [30, 120])
    if isinstance(quiet, list) and len(quiet) == 2:
        lo, hi = (_cnum(quiet[i], d, 0, 3600, "mesh_boot_quiet_s") for i, d in ((0, 30), (1, 120)))
        quiet = (min(lo, hi), max(lo, hi))
    else:
        n = _cnum(quiet, 120, 0, 3600, "mesh_boot_quiet_s")
        quiet = (n / 4, n)
    max_util = _cnum(cfg.get("mesh_max_util"), 25, 1, 100, "mesh_max_util")
    if cfg.get("mesh_beacon_public") is True:
        print("meshbridge: [radio] mesh_beacon_public is no longer supported — control packets never go on the "
              "public channel (docs/MESH.md)", file=sys.stderr)
    return {
        "enabled": cfg.get("mesh_courtesy", True) is not False,
        "beacons": cfg.get("mesh_beacons", True) is not False,
        "beacon_min": _cnum(cfg.get("mesh_peer_beacon_min"), 45, 10, 720, "mesh_peer_beacon_min"),
        "max_util": max_util,
        "busy_util": _cnum(cfg.get("mesh_max_util_busy"), 40, max_util, 100, "mesh_max_util_busy"),
        "max_air_tx": _cnum(cfg.get("mesh_max_air_tx"), 8, 1, 100, "mesh_max_air_tx"),
        "budget": budget,
        "budget_scale": _cnum(cfg.get("mesh_budget_peer_scale"), 4, 1, 100, "mesh_budget_peer_scale"),
        "boot_quiet": quiet,
        "hop_local": int(_cnum(cfg.get("mesh_hop_limit_local"), 2, 1, 7, "mesh_hop_limit_local")),
    }


class Silence:
    """Radio silence (receive-only). On when [radio] mesh_silent = true, or while the flag file exists
    ([portal] mesh_silent_file, default /var/lib/prepper/portal/mesh-silent) — the front panel / admin
    page create it to go silent at once and delete it to end silence, no restart needed. File contract:
    its existence means silent; it may hold JSON {"until": <epoch seconds>, "by": "<who>"}: once "until"
    has passed it no longer counts (the bridge never deletes it). Empty or unreadable content: silent.
    While silent the bridge transmits nothing: no relays, replies, beacons, claims, summaries, forecasts
    or community posts — except, with [radio] mesh_silent_allow_family = true (default), family-board
    posts a member typed and chose to send. It still receives, logs and fills the boards."""

    def __init__(self, path: Path | None, configured: bool = False, allow_family: bool = True, clock=None):
        self.path, self.configured, self.allow_family = path, configured, allow_family
        self.clock = clock or (lambda: time.time())
        self._cache = (-1e18, False)
        self.lock = threading.Lock()

    def flag(self) -> bool:
        if self.path is None:
            return False
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        if st.st_size == 0:
            return True
        d = read_json(self.path, None, limit=4096)
        until = _num(d.get("until")) if isinstance(d, dict) else None
        return until is None or until > self.clock()

    def active(self) -> bool:
        if self.configured:
            return True
        now = self.clock()
        with self.lock:
            t, v = self._cache
            if 0 <= now - t < 1.0:
                return v
        v = self.flag()
        with self.lock:
            self._cache = (now, v)
        return v

    def allows(self, item: dict) -> bool:
        """May this queued item go out now?"""
        return not self.active() or (self.allow_family and item.get("kind") == "family" and item.get("member") is True)


class Peers:
    """Badgers heard on the air (beacon, claim or done) — at most `cap`, the oldest forgotten first."""

    def __init__(self, clock, cap: int = 200):
        self.clock, self.cap = clock, cap
        self.boot = clock()
        self.table: dict[str, dict] = {}
        self.presumed: set[str] = set()

    def load(self, recs) -> None:
        now = self.clock()
        for r in (recs if isinstance(recs, list) else [])[: self.cap]:
            if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not NODE_RE.match(r["id"]):
                continue
            last = _num(r.get("last"))
            if last is None or not 0 <= now - last <= PRESUME_WINDOW:
                continue
            self.table[r["id"]] = {"id": r["id"], "num": node_num(r["id"]), "first": _num(r.get("first")) or last,
                                   "last": last, "proto": clean(r.get("proto"), 8), "via": "saved"}
            if now - last > PEER_WINDOW:
                self.presumed.add(r["id"])

    def heard(self, nid: str, num: int | None, via: str, msg: dict | None = None) -> bool:
        """Returns True for a Badger not in the table before."""
        now = self.clock()
        rec = self.table.get(nid)
        new = rec is None
        if new:
            if len(self.table) >= self.cap:
                del self.table[min(self.table, key=lambda k: self.table[k]["last"])]
            rec = self.table[nid] = {"id": nid, "num": num, "first": now, "last": now, "proto": "", "via": via}
        rec.update(last=now, via=via, num=num if num is not None else rec.get("num"))
        if msg and msg.get("type") == "beacon":
            rec.update(proto=msg["proto"])
        self.presumed.discard(nid)
        return new

    def active_ids(self, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        presume = now - self.boot < PRESUME_FOR
        return [k for k, r in self.table.items() if now - r["last"] <= PEER_WINDOW or (presume and k in self.presumed)]

    def active(self, now: float | None = None) -> int:
        return min(len(self.active_ids(now)), PEER_COUNT_CAP)

    def snapshot(self) -> list[dict]:
        return sorted((dict(r) for r in self.table.values()), key=lambda r: -r["last"])


class _Pending:
    __slots__ = ("key", "kh", "tag", "ch", "kind", "created", "t_go", "t_send", "state", "cap_until", "on_go",
                 "match", "label", "req_id", "at_boot", "on_final", "held_by", "late_yield", "text")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Courtesy:
    """Duplicate suppression between Badgers, keyed by a dedupe key per channel (docs/MESH.md):
      submit()        something several Badgers might send: wait a back-off (claim first on a private
                      channel), send — or don't, if another Badger's copy (or, low-stakes only and on a
                      private channel, a claim/done) was heard meanwhile
      heard_text()    a text heard on a channel: recognises other Badgers' copies (alerts: exact text only)
      heard_control() a PRIVATE_APP packet on a private channel: beacon, claim or done
      done()          tells the others (private channels only) that a reply went out
      tick()          timers (claims, sends, beacons); call every second or so
    ctrl_chs: the private channels (family / circle) where control packets may be sent and are listened
    to; ctrl_keys: channel → sealing key (ctrl_key(channel secret)). Nowhere else — the public channel
    never carries a control packet; there Badgers take turns and listen for the text itself.
    Thread-safe; callbacks (sending) always run outside the lock."""

    def __init__(self, send_ctrl, clock, rng=None, *, my_id=None, my_num=None, cfg=None,
                 beacon_chs=(), load=None, silence=None, ctrl_chs=None, ctrl_keys=None):
        self.cfg = cfg or courtesy_config({})
        self.enabled = bool(self.cfg["enabled"])
        self.send_ctrl, self.clock = send_ctrl, clock
        self.rng = rng or random.SystemRandom()
        self.my_id = my_id.strip().lower() if isinstance(my_id, str) and my_id.strip() else None
        self.my_num = my_num if node_num(my_num) else node_num(self.my_id)
        ok = lambda c: isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 7  # noqa: E731
        self.ctrl_chs = {c for c in (beacon_chs if ctrl_chs is None else ctrl_chs) if ok(c)}
        self.ctrl_keys = {c: k for c, k in (ctrl_keys or {}).items() if ok(c) and isinstance(k, bytes) and k}
        self.beacon_chs = [c for c in beacon_chs if ok(c) and c in self.ctrl_chs]
        self.load = load or (lambda: (None, None))
        self.silence = silence or Silence(None)
        self.peers = Peers(clock)
        self.boot = clock()
        self.pending: dict[tuple, _Pending] = {}
        self.final: collections.OrderedDict = collections.OrderedDict()     # (kh, ch) → (state, by, t)
        self.sent_tags: collections.OrderedDict = collections.OrderedDict()  # (tag, ch) → t, what WE sent
        self.claims: collections.OrderedDict = collections.OrderedDict()     # (kh, ch) → (num, id, until)
        self.dones: collections.OrderedDict = collections.OrderedDict()      # (kh, ch) → (id, t)
        self.reply_ids: collections.OrderedDict = collections.OrderedDict()  # (packet id, ch) → (id, t)
        self.done_sent: collections.OrderedDict = collections.OrderedDict()
        self.req_seen: collections.OrderedDict = collections.OrderedDict()   # (request id, ch) → t: we took part
        self.sent_texts: collections.OrderedDict = collections.OrderedDict() # (canon hash, ch) → t, what WE sent
        self.relays: collections.deque = collections.deque(maxlen=300)        # (t, ch, text, sender)
        self.ctrl_seen: dict[str, collections.deque] = {}
        self.events: collections.deque = collections.deque(maxlen=40)
        self.counts: collections.Counter = collections.Counter()
        self.next_beacon = self.boot + self.rng.uniform(15, 90)
        self.lock = threading.RLock()

    # — helpers (lock held) —
    def key_for(self, ch: int) -> bytes:
        return self.ctrl_keys.get(ch, DEFAULT_CTRL_KEY)

    def set_ctrl_key(self, ch: int, key: bytes | None) -> None:
        """A private channel's sealing key changed (the family channel was re-keyed); None: no control
        packets on it any more (its secret is weak or unknown)."""
        with self.lock:
            if isinstance(key, bytes) and key:
                self.ctrl_keys[ch] = key
                self.ctrl_chs.add(ch)
            else:
                self.ctrl_keys.pop(ch, None)
                self.ctrl_chs.discard(ch)
                self.beacon_chs = [c for c in self.beacon_chs if c != ch]

    def _beacon_gap(self) -> float:
        """Seconds to the next beacon: Poisson-random (exponential) around the configured mean, clamped to
        a fifth .. three times of it — no rhythm a listener could lock on to."""
        mean = self.cfg["beacon_min"] * 60.0
        return max(mean * 0.2, min(mean * 3.0, self.rng.expovariate(1.0 / mean)))

    def _turn_delay(self, key: str, kind: str, now: float) -> float:
        """No control packets on this channel: every Badger orders itself among the Badgers it knows by a
        hash of (key, radio id) — the same order everywhere — and waits its turn; the first one's text is
        heard by the rest, who stand down. A different order for every message (no fixed transmitter)."""
        lo, _, hi = BACKOFF.get(kind, BACKOFF["community"])
        ids = set(self.peers.active_ids(now))
        if self.my_id:
            order = sorted(ids | {self.my_id}, key=lambda i: hmac.new(KEY_SALT, f"{key}|{i}".encode("utf-8", "replace"),
                                                                      hashlib.sha256).digest())
            slot = order.index(self.my_id)
        else:
            slot = self.rng.randrange(len(ids) + 1)
        return lo + min(slot * TURN.get(kind, 4.0), hi - lo) + self.rng.uniform(0.0, 0.5)

    def _shared_quiet(self, key: str, now: float, lo: float, hi: float) -> float:
        """After a restart, on a channel without claims: the end of the quiet period falls on the same wall-clock
        moment for every Badger that holds the same item (a key-derived phase), so their turns still line up."""
        period = hi - lo
        if period <= 0:
            return lo
        frac = int.from_bytes(hmac.new(KEY_SALT, b"quiet|" + key.encode("utf-8", "replace"), hashlib.sha256).digest()[:4],
                              "big") / 2 ** 32
        phase = frac * period
        t0 = now + lo
        anchor = phase + math.ceil((t0 - phase) / period) * period
        return anchor - now

    def _event(self, what: str, p: _Pending | None = None, by: str | None = None, now: float | None = None) -> None:
        self.events.append({"at": round(self.clock() if now is None else now), "what": what,
                            "kind": p.kind if p else None, "label": (p.label if p else "")[:60],
                            "ch": p.ch if p else None, "by": by})

    def _backoff(self, kind: str, peers: int, hops: int = 0) -> float:
        lo, per, hi = BACKOFF.get(kind, BACKOFF["community"])
        d = self.rng.uniform(lo, max(lo, min(hi, lo + per * peers)))
        if kind == "reply":
            d += min(max(hops, 0), 7) * 1.0          # the Badger nearest the asker answers first
        return d

    def _rebackoff(self, kind: str) -> float:
        hi = BACKOFF.get(kind, BACKOFF["community"])[2]
        return self.rng.uniform(1.0, max(2.0, hi * 0.25))

    def _matches(self, p: _Pending, text: str) -> bool:
        if p.match is not None:
            try:
                return bool(p.match(text))
            except Exception:  # noqa: BLE001 — a matcher must never break the radio thread
                return False
        if p.kind in SAFETY:                         # safety rule: only an exact copy cancels it
            return p.text is not None and canon(text) == canon(p.text)
        m = TAG_RE.search(text)
        return bool(m) and m.group(1) == p.tag

    def _remember(self, d: collections.OrderedDict, k, v, cap: int = 2000) -> None:
        d[k] = v
        d.move_to_end(k)
        while len(d) > cap:
            d.popitem(last=False)

    def _finish(self, p: _Pending, state: str, by: str | None, run: list, now: float) -> None:
        self.pending.pop((p.kh, p.ch), None)
        self._remember(self.final, (p.kh, p.ch), (state, by, now))
        self.counts[state] += 1
        self.counts[f"{state}_{p.kind}"] += 1
        self._event(state, p, by, now)
        if state == "suppressed" and p.kind in SAFETY:
            print(f"meshbridge: {p.label or p.kind} on channel {p.ch} was already relayed by {by} — "
                  "not sent again", file=sys.stderr)
        if p.on_final:
            run.append(lambda f=p.on_final, s=state, b=by: f(s, b))

    def _go(self, p: _Pending, run: list, now: float) -> None:
        self._remember(self.sent_tags, (p.tag, p.ch), now, 500)
        if p.text:
            self._remember(self.sent_texts, (_th(p.text), p.ch), now, 500)
        on_go = p.on_go
        self._finish(p, "sent", self.my_id, run, now)
        run.insert(len(run) - (1 if p.on_final else 0), on_go)     # send first, then report it final

    def _heard_already(self, p: _Pending, now: float) -> str | None:
        k = (p.kh, p.ch)
        if p.kind not in SAFETY and k in self.dones and now - self.dones[k][1] < 900:
            return self.dones[k][0]
        if p.kind == "reply" and p.req_id is not None and (p.req_id, p.ch) in self.reply_ids:
            return self.reply_ids[(p.req_id, p.ch)][0]
        for t, ch, text, sender in reversed(self.relays):
            if ch == p.ch and now - t < 6 * 3600 and self._matches(p, text):
                return sender
        return None

    def _hold(self, p: _Pending, num: int | None, sid: str, ttl: float, now: float) -> None:
        """Another Badger claimed what we're about to send."""
        if p.state == "claimed" and num is not None and self.my_num is not None and num > self.my_num:
            return                                   # simultaneous claims: the lower node number sends — us
        hold = now + max(1.0, min(ttl, 300.0))
        if p.kind in SAFETY and now >= p.cap_until:
            # held long enough: from now on an alert ignores claims — except that it may still give way ONCE
            # in a simultaneous-claim tie (so Badgers past the cap don't all send), for 15 s at most
            if p.state != "claimed" or p.late_yield:
                return
            p.late_yield = True
            hold = min(hold, now + 15.0)
        elif p.kind in SAFETY:
            hold = min(hold, p.cap_until)
        p.state, p.held_by = "wait", sid
        p.t_go = max(p.t_go, hold + self._rebackoff(p.kind))
        self.counts["held"] += 1
        self._event("held", p, sid, now)

    # — API —
    def submit(self, key: str, kind: str, ch: int, on_go, *, match=None, label: str = "", hops: int = 0,
               req_id=None, at_boot: bool = False, extra: float = 0.0, on_final=None, text: str | None = None) -> str:
        """Returns "sent" (on_go already called), "pending", "suppressed" or "duplicate". `text`: what will
        go out (alert/danger without a matcher are only ever cancelled by hearing exactly this). `extra`:
        wait at least this long, even with no other Badger around."""
        kind = kind if kind in KINDS else "community"
        kh = key_hash(key)
        run: list = []
        with self.lock:
            now = self.clock()
            k = (kh, ch)
            if req_id is not None:
                self._remember(self.req_seen, (req_id, ch), now, 500)
            if k in self.pending or k in self.final:
                return "duplicate"
            p = _Pending(key=key, kh=kh, tag=kh[:2].hex(), ch=ch, kind=kind, created=now, state="wait",
                         on_go=on_go, match=match, label=clean(label, 60), req_id=req_id, at_boot=at_boot,
                         on_final=on_final, text=norm(text) if isinstance(text, str) and text.strip() else None)
            peers = self.peers.active(now)
            text_only = ch not in self.ctrl_chs
            by = self._heard_already(p, now) if self.enabled else None
            if self.silence.active():
                self._finish(p, "silenced", None, run, now)     # radio silence: nothing automatic goes out
                res = "silenced"
            elif by is not None:
                self._finish(p, "suppressed", by, run, now)
                res = "suppressed"
            elif extra <= 0 and (not self.enabled or (peers == 0 and not at_boot and kind not in SAFETY)):
                self._go(p, run, now)
                res = "sent"
            else:
                lo, hi = self.cfg["boot_quiet"]
                if not self.enabled:
                    delay = 0.0
                elif at_boot and hi > 0:
                    if text_only and peers > 0:             # restart: the same quiet period, then turns
                        delay = self._shared_quiet(key, now, lo, hi) + self._turn_delay(key, kind, now)
                    else:
                        delay = self.rng.uniform(lo, hi)    # everyone boots together; spread out
                elif peers == 0:
                    delay = self.rng.uniform(0, LONE_ALERT_JITTER) if kind in SAFETY else 0.0
                elif text_only:
                    delay = self._turn_delay(key, kind, now)
                else:
                    delay = self._backoff(kind, peers, hops)
                p.t_go = now + delay + max(0.0, extra)
                p.cap_until = p.t_go + SAFETY_HOLD_MAX
                self.pending[k] = p
                c = self.claims.get(k)
                if c and c[2] > now:
                    self._hold(p, c[0], c[1], c[2] - now, now)
                self.counts["pending"] += 1
                res = "pending"
        self._run(run)
        return res

    def _run(self, run: list) -> None:
        for fn in run:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — a failed send must not stop the others
                print(f"meshbridge: courtesy callback failed: {type(e).__name__}: {e}", file=sys.stderr)

    def heard_text(self, text, sender, ch: int, packet=None) -> dict:
        """A text another radio sent on channel `ch`. Returns {"relay": a Badger's relay (alert/tagged),
        "board_key": show it on the board once per this key, "ours": a copy of what this Badger sent}."""
        t = norm(text)
        sid = clean(sender, 16).lower()
        info = {"relay": False, "board_key": None, "ours": False}
        if not t or (self.my_id and sid == self.my_id):
            return info
        m = TAG_RE.search(t)
        is_alert = t.startswith(ALERT_PREFIX)
        run: list = []
        with self.lock:
            now = self.clock()
            if m or is_alert:
                self.relays.append((now, ch, t, sid))
                if m and is_alert and NODE_RE.match(sid):
                    self.peers.heard(sid, node_num(packet.get("from")) if isinstance(packet, dict) else None, "relay")
                if self.enabled:
                    for p in [p for p in self.pending.values() if p.ch == ch and self._matches(p, t)]:
                        self._finish(p, "suppressed", sid, run, now)
                if is_alert:                         # by its exact text: a forged variant never hides the real one
                    info.update(relay=True, board_key="text:" + _th(t))
                    info["ours"] = (_th(t), ch) in self.sent_texts
                elif m and sid in self.peers.table:
                    info.update(relay=True, board_key="tag:" + m.group(1))
                    info["ours"] = (m.group(1), ch) in self.sent_tags
            dec = packet.get("decoded") if isinstance(packet, dict) else None
            rid = dec.get("replyId") if isinstance(dec, dict) else None
            if isinstance(rid, int) and not isinstance(rid, bool) and rid:
                self._remember(self.reply_ids, (rid, ch), (sid, now), 500)
                if (rid, ch) in self.req_seen and NODE_RE.match(sid):
                    self.peers.heard(sid, node_num(packet.get("from")) if isinstance(packet, dict) else None, "reply")
                if self.enabled:
                    for p in [p for p in self.pending.values() if p.kind == "reply" and p.req_id == rid and p.ch == ch]:
                        self._finish(p, "suppressed", sid, run, now)
        self._run(run)
        return info

    def heard_control(self, packet) -> bool:
        """A PRIVATE_APP packet. True if it was a valid courtesy message from another Badger."""
        if not isinstance(packet, dict):
            return False
        dec = packet.get("decoded")
        if not isinstance(dec, dict) or dec.get("portnum") not in ("PRIVATE_APP", PRIVATE_APP):
            return False
        ch = packet.get("channel", 0)
        ch = 0 if ch is None else ch
        if isinstance(ch, bool) or not isinstance(ch, int) or not 0 <= ch <= 7:
            return False
        with self.lock:
            private = ch in self.ctrl_chs
            key = self.key_for(ch)
        if not private:
            self.counts["ctrl_ignored"] += 1         # control packets on the public channel: never ours
            return False
        msg = decode_control(dec.get("payload"), key)
        if msg is None:
            self.counts["garbled"] += 1
            return False
        sid = packet.get("fromId")
        sid = sid.strip().lower() if isinstance(sid, str) else ""
        num = node_num(packet.get("from"))
        if not NODE_RE.match(sid):
            if num is None:
                return False
            sid = f"!{num:08x}"
        num = num if num is not None else node_num(sid)
        if (self.my_id and sid == self.my_id) or (self.my_num is not None and num == self.my_num):
            return False                             # our own packet coming back
        run: list = []
        with self.lock:
            now = self.clock()
            seen = self.ctrl_seen.setdefault(sid, collections.deque())
            while seen and now - seen[0] > CTRL_PER_SENDER[1]:
                seen.popleft()
            if len(seen) >= CTRL_PER_SENDER[0]:
                self.counts["ctrl_flood"] += 1
                return False                         # a radio spamming control packets: not listening
            seen.append(now)
            if len(self.ctrl_seen) > 1000:
                for k in [k for k, d in self.ctrl_seen.items() if not d or now - d[-1] > CTRL_PER_SENDER[1]]:
                    del self.ctrl_seen[k]
            if self.peers.heard(sid, num, msg["type"], msg):
                self._event("new Badger", None, sid, now)
            if msg["type"] == "claim":
                k = (msg["kh"], ch)
                ttl = max(1, min(300, msg["ttl"]))
                self._remember(self.claims, k, (num, sid, now + ttl), 500)
                p = self.pending.get(k)
                if p is not None and self.enabled:
                    self._hold(p, num, sid, ttl, now)
            elif msg["type"] == "done":
                k = (msg["kh"], ch)
                self._remember(self.dones, k, (sid, now), 500)
                p = self.pending.get(k)
                if p is not None and self.enabled and p.kind not in SAFETY:
                    self._finish(p, "suppressed", sid, run, now)
        self._run(run)
        return True

    def done(self, key: str, ch: int, kind: str = "reply") -> None:
        if not self.enabled or self.silence.active():
            return
        kh = key_hash(key)
        with self.lock:
            if ch not in self.ctrl_chs or (kh, ch) in self.done_sent:
                return                               # never a control packet on the public channel
            self._remember(self.done_sent, (kh, ch), self.clock(), 500)
            payload = encode_done(kh, self.key_for(ch), self.rng)
        self.send_ctrl(payload, ch)

    def tick(self) -> None:
        run: list = []
        silent = self.silence.active()
        with self.lock:
            now = self.clock()
            peers = self.peers.active(now)
            for p in sorted(self.pending.values(), key=lambda p: p.t_go):
                if silent:
                    self._finish(p, "silenced", None, run, now)
                    continue
                if p.state == "wait" and now >= p.t_go:
                    if (peers == 0 and not p.at_boot) or p.ch not in self.ctrl_chs or not self.enabled:
                        self._go(p, run, now)        # nobody to coordinate with / no claims on this channel
                        continue
                    payload = encode_claim(p.kh, CLAIM_TTL.get(p.kind, 60), self.key_for(p.ch), self.rng)
                    run.append(lambda b=payload, c=p.ch: self.send_ctrl(b, c))
                    p.state, p.t_send = "claimed", now + SETTLE
                    self.counts["claims"] += 1
                elif p.state == "claimed" and now >= p.t_send:
                    self._go(p, run, now)
            if self.enabled and self.cfg.get("beacons", True) and self.beacon_chs and now >= self.next_beacon:
                util = self.load()[0]
                if self.silence.active() or (util is not None and util >= self.cfg["busy_util"]):
                    self.next_beacon = now + self.rng.uniform(300, 1200)   # silence / busy: the beacon can wait
                else:
                    for c in self.beacon_chs:            # (sealed separately: no two packets alike)
                        b = encode_beacon(self.key_for(c), self.rng)
                        run.append(lambda b=b, c=c: self.send_ctrl(b, c))
                    self.counts["beacons"] += 1
                    self.next_beacon = now + self._beacon_gap()
            # forget old state (each dict is roughly in time order: entries move to the end when updated)
            for d, age, when in ((self.claims, 0, lambda v: v[2]), (self.dones, 900, lambda v: v[1]),
                                 (self.reply_ids, 900, lambda v: v[1]), (self.final, 86400, lambda v: v[2]),
                                 (self.sent_tags, 86400, lambda v: v), (self.done_sent, 3600, lambda v: v),
                                 (self.sent_texts, 86400, lambda v: v), (self.req_seen, 900, lambda v: v)):
                while d and now - when(next(iter(d.values()))) > age:
                    d.popitem(last=False)
        self._run(run)

    def status(self) -> dict:
        with self.lock:
            now = self.clock()
            pend = collections.Counter(p.kind for p in self.pending.values())
            return {"enabled": self.enabled, "proto": "%d.%d" % COURTESY_PROTO, "me": self.my_id,
                    "silent": self.silence.active(), "beacon_channels": list(self.beacon_chs),
                    "control_channels": sorted(self.ctrl_chs), "beacons": bool(self.cfg.get("beacons", True)),
                    "peers_active": self.peers.active(now), "peers": self.peers.snapshot()[:200],
                    "pending": dict(pend), "counts": dict(self.counts),
                    "quiet_until": None if not self.cfg["boot_quiet"][1] else round(self.boot + self.cfg["boot_quiet"][1]),
                    "recent": list(self.events)[-20:]}


def _kwargs_supported(fn, names) -> set:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return set()
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return set(names)
    return {n for n in names if n in params}


def channel_load(iface) -> tuple[float | None, float | None]:
    """(channel utilization %, our own airtime %) as the radio reports them in its device metrics
    (meshtastic python: getMyNodeInfo()["deviceMetrics"], or nodesByNum[myInfo.my_node_num]); None if unknown."""
    dm = None
    try:
        info = iface.getMyNodeInfo()
        dm = info.get("deviceMetrics") if isinstance(info, dict) else None
    except Exception:  # noqa: BLE001 — older library / radio busy
        dm = None
    if not isinstance(dm, dict):
        try:
            dm = (iface.nodesByNum or {}).get(iface.myInfo.my_node_num, {}).get("deviceMetrics")
        except Exception:  # noqa: BLE001
            dm = None
    if not isinstance(dm, dict):
        return None, None

    def pct(v):
        v = _num(v)
        return None if v is None or not 0 <= v <= 100 else float(v)
    return pct(dm.get("channelUtilization")), pct(dm.get("airUtilTx"))


class RadioLink:
    """The one way to the radio. Hop limits and reply ids are passed only when the installed meshtastic
    library's sendText/sendData accept them (checked once; a TypeError retries without them)."""

    def __init__(self, iface, lock: threading.Lock | None = None, silence: "Silence | None" = None):
        self.iface, self.lock = iface, lock or threading.Lock()
        self.silence = silence or Silence(None)
        self.text_kw = _kwargs_supported(getattr(iface, "sendText", None), ("hopLimit", "replyId"))
        self.data_kw = _kwargs_supported(getattr(iface, "sendData", None), ("hopLimit",))

    def text(self, text, dest=None, channel=None, hop_limit=None, reply_id=None, silent_ok: bool = False) -> bool:
        if not isinstance(channel, int) or isinstance(channel, bool) or channel < 0:
            return False
        if not silent_ok and self.silence.active():
            return False                             # radio silence: the last line of defence
        kw = {"channelIndex": channel}
        if dest:
            kw["destinationId"] = dest
        extra = {}
        if hop_limit is not None and "hopLimit" in self.text_kw:
            extra["hopLimit"] = int(hop_limit)
        if reply_id is not None and "replyId" in self.text_kw:
            extra["replyId"] = int(reply_id)
        try:
            with self.lock:                          # the serial link isn't safe to share between threads
                try:
                    self.iface.sendText(fit(text), **kw, **extra)
                except TypeError:
                    if not extra:
                        raise
                    self.text_kw = set()             # this library doesn't take them after all
                    self.iface.sendText(fit(text), **kw)
            return True
        except Exception as e:  # noqa: BLE001 — serial/radio errors: the caller decides whether to retry
            print(f"meshbridge: send failed ({e})", file=sys.stderr)
            return False

    def data(self, payload: bytes, channel: int, hop_limit=None) -> bool:
        if not isinstance(channel, int) or channel < 0 or not hasattr(self.iface, "sendData") or self.silence.active():
            return False
        kw = {"destinationId": "^all", "portNum": PRIVATE_APP, "wantAck": False, "channelIndex": channel}
        if hop_limit is not None and "hopLimit" in self.data_kw:
            kw["hopLimit"] = int(hop_limit)
        try:
            with self.lock:
                self.iface.sendData(bytes(payload), **kw)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"meshbridge: control packet not sent ({e})", file=sys.stderr)
            return False


class Airtime:
    """Every text this bridge transmits goes through here: strict priority (alert > danger > family >
    reply > community > auto), per-kind pacing, hourly budgets that shrink as more Badgers are heard,
    channel-busy deferral (never for alerts), and expiry of stale items instead of sending them late.
    Items: {"kind","text","ch", optional "dest","hop_limit","reply_id","at","expires","on_sent","on_drop"}."""
    TTL = {"alert": 6 * 3600, "danger": 2 * 3600, "family": 12 * 3600, "reply": 300, "community": 1800, "auto": 7200}
    PACE = {"family": (10, 15.0), "community": (5, None), "reply": (6, None), "auto": (1, None)}   # None: budget
    CAP = {"alert": 50, "danger": 50, "family": 100, "reply": 20, "community": 40, "auto": 10}

    def __init__(self, send, clock, cfg: dict | None = None, peers=None, load=None, silence=None):
        self.send, self.clock = send, clock
        self.silence = silence or Silence(None)
        self.cfg = cfg or courtesy_config({})
        self.peers = peers or (lambda: 0)
        self.load = load or (lambda: (None, None))
        self.q = {k: collections.deque() for k in KINDS}
        self.buckets = {k: Bucket(burst, every or 3600.0 / max(self.budget(k, 0), 1e-6), clock)
                        for k, (burst, every) in self.PACE.items()}
        self.counts: collections.Counter = collections.Counter()
        self.deferred: dict[str, int] = {}
        self.level = 0
        self.lock = threading.Lock()

    def budget(self, kind: str, peers: int) -> float:
        """Messages per hour this Badger may send of `kind` with `peers` other Badgers nearby."""
        base = self.cfg["budget"].get(kind)
        if base is None:
            return float("inf")
        if base <= 0:
            return 0.0
        return max(min(base, BUDGET_MIN[kind]), base / (1 + peers / self.cfg["budget_scale"]))

    def busy_level(self) -> int:
        util, air = self.load()
        if util is not None and util >= self.cfg["busy_util"]:
            return 2
        if (util is not None and util >= self.cfg["max_util"]) or (air is not None and air >= self.cfg["max_air_tx"]):
            return 1
        return 0

    def put(self, item: dict, pump: bool = True) -> bool:
        kind = item.get("kind") if item.get("kind") in KINDS else "community"
        now = self.clock()
        it = dict(item, kind=kind)
        at = _num(it.get("at"))
        it["at"] = now if at is None or at > now + 300 else at
        exp = _num(it.get("expires"))
        it["expires"] = exp if exp is not None else it["at"] + self.TTL[kind]
        if it.get("hop_limit") is None and kind in ("reply", "auto"):
            it["hop_limit"] = self.cfg["hop_local"]   # keep chatter local
        drop = None
        with self.lock:
            if len(self.q[kind]) >= self.CAP[kind]:
                self.counts[f"dropped_{kind}"] += 1
                drop = it.get("on_drop")
            else:
                self.q[kind].append(it)
        if drop is not None:
            print(f"meshbridge: radio queue for {kind} is full — a message was dropped", file=sys.stderr)
            self._call(drop, "full")
            return False
        if pump:
            self.pump()
        return True

    @staticmethod
    def _call(fn, *a) -> None:
        try:
            fn(*a)
        except Exception as e:  # noqa: BLE001
            print(f"meshbridge: radio callback failed: {type(e).__name__}: {e}", file=sys.stderr)

    def pump(self) -> int:
        run, n = [], 0
        with self.lock:
            now = self.clock()
            peers = self.peers()
            self.level = level = self.busy_level()
            for kind, q in self.q.items():
                if any(now > it["expires"] for it in q):
                    keep = collections.deque()
                    for it in q:
                        if now > it["expires"]:
                            self.counts[f"expired_{kind}"] += 1
                            print(f"meshbridge: a {kind} message waited too long for the radio — not sent late",
                                  file=sys.stderr)
                            if it.get("on_drop"):
                                run.append((it["on_drop"], "expired"))
                        else:
                            keep.append(it)
                    self.q[kind] = keep
            if self.silence.active():                # radio silence: drop what may not go (never sent later)
                for kind, q in self.q.items():
                    keep = collections.deque(it for it in q if self.silence.allows(it))
                    for it in q:
                        if not self.silence.allows(it):
                            self.counts[f"silenced_{kind}"] += 1
                            if it.get("on_drop"):
                                run.append((it["on_drop"], "silenced"))
                    self.q[kind] = keep
            self.deferred = {}
            for kind in KINDS:
                q = self.q[kind]
                if not q:
                    continue
                if (level >= 1 and kind == "auto") or (level >= 2 and kind == "community"):
                    self.deferred[kind] = len(q)     # a busy channel: these wait (until they expire)
                    continue
                b = self.buckets.get(kind)
                if b is not None and self.PACE[kind][1] is None:
                    per_hour = self.budget(kind, peers)
                    b.every = 3600.0 / per_hour if per_hour > 0 else float("inf")
                    if level >= 1 and kind == "community":
                        b.every *= 2                 # getting busy: community posts slow down
                while q and (b is None or b.take()):
                    it = q[0]
                    extra = {"silent_ok": True} if self.silence.active() else {}    # (allowed: checked above)
                    ok = self.send(it["text"], it.get("dest"), it["ch"], it.get("hop_limit"), it.get("reply_id"), **extra)
                    if not ok:
                        if b is not None:
                            b.tokens += 1            # not spent: the radio didn't take it
                        break                        # keep it, retry next round
                    q.popleft()
                    n += 1
                    self.counts[f"sent_{kind}"] += 1
                    if it.get("on_sent"):
                        run.append((it["on_sent"],))
        for fn, *a in run:
            self._call(fn, *a)
        return n

    def status(self) -> dict:
        with self.lock:
            peers = self.peers()
            return {"queued": {k: len(q) for k, q in self.q.items() if q}, "deferred": dict(self.deferred),
                    "silent": self.silence.active(),
                    "busy_level": self.level, "counts": dict(self.counts),
                    "budget_per_hour": {k: round(self.budget(k, peers), 2) for k in BUDGET_DEFAULT}}


class Heard:
    """Texts heard on the mesh lately: an outbox line repeating one is an echo, never sent back out."""

    def __init__(self, clock, ttl: float = 6 * 3600, cap: int = 500):
        self.clock, self.ttl, self.cap = clock, ttl, cap
        self.d: collections.OrderedDict = collections.OrderedDict()
        self.lock = threading.Lock()

    @staticmethod
    def _k(text) -> str:
        return hashlib.sha256(norm(text).casefold().encode("utf-8", "replace")).hexdigest()[:20]

    def add(self, text) -> None:
        if len(norm(text)) < 20:
            return
        with self.lock:
            self.d[self._k(text)] = self.clock()
            self.d.move_to_end(self._k(text))
            while len(self.d) > self.cap:
                self.d.popitem(last=False)

    def seen(self, text) -> bool:
        t = norm(text)
        cands = [t] + ([t.split(": ", 1)[1]] if ": " in t else [])     # "Name: text" as the portal writes it
        now = self.clock()
        with self.lock:
            return any(len(c) >= 20 and now - self.d.get(self._k(c), -1e18) < self.ttl for c in cands)


# ─────────────────────────────── position privacy ────────────────────────────

# Meshtastic channel module_settings.position_precision = how many leading bits of the 32-bit latitude_i /
# longitude_i (units of 1e-7°) are kept; the firmware reports the centre of the remaining box (firmware
# PositionModule / PositionPrecision: mask UINT32_MAX << (32 - bits), plus 1 << (31 - bits)). The box is
# 2^(32-bits) · 1e-7° tall; the Meshtastic docs quote half of it (the furthest the true spot can be from the
# reported one along an axis): 10 ≈ 23.3 km, 11 ≈ 11.7 km, 12 ≈ 5.8 km, 13 ≈ 2.9 km, 14 ≈ 1.5 km,
# 16 ≈ 364 m, 19 ≈ 45 m; 0 = never sent, 32 = exact.
POSITION_BITS = {"off": 0, "coarse": 12, "precise": 32}   # coarse: within ~6 km (a box ~12 km across)
POSITION_FINEST = 13                    # never finer than ~3 km on the public channel unless "precise"


def position_distance_m(bits: int) -> float | None:
    """How far off (metres, along a north–south axis) a position sent with `bits` may be — the figure the
    Meshtastic docs list; None for 0 (not sent), 0.0 for 32 (exact)."""
    if not isinstance(bits, int) or isinstance(bits, bool) or not 0 <= bits <= 32:
        raise ValueError(bits)
    if bits == 0:
        return None
    if bits == 32:
        return 0.0
    return (2 ** (32 - bits)) * 1e-7 / 2 * 111_195.0      # half the box; 1° of latitude ≈ 111.195 km


def position_bits(mode) -> tuple[int | None, str]:
    """[radio] mesh_public_position → (bits, note). "off" | "coarse" | "precise", or a number of bits
    (0–32), which is clamped to POSITION_FINEST: only the word "precise" ever shares an exact position."""
    if isinstance(mode, str) and mode in POSITION_BITS:
        return POSITION_BITS[mode], ""
    if isinstance(mode, int) and not isinstance(mode, bool) and 0 <= mode <= 32:
        if mode > POSITION_FINEST:
            return POSITION_FINEST, (f"mesh_public_position = {mode} is finer than ~3 km: using "
                                     f"{POSITION_FINEST} bits (say \"precise\" to share the exact position)")
        return mode, ""
    return None, f"unknown mesh_public_position {mode!r}: left as it is"


def apply_position_privacy(iface, public_ch: int, mode, state_path: Path) -> dict:
    """How precisely the radio's own position goes out on the PUBLIC channel ([radio]
    mesh_public_position = "off" | "coarse" | "precise" | bits, default "off"): channel
    module_settings.position_precision (firmware 2.3+), written with writeChannel. Idempotent; never raises.
    The family channel is left exactly as set in the Meshtastic app (we never share MORE than you chose).
    Doesn't fight the owner in the safe direction: if the value on the radio differs from what the bridge
    last applied (state file), someone changed it in the app since — it is left alone and that is logged —
    unless it is now finer than ~3 km (POSITION_FINEST) without "precise": then it is set back (a radio on
    the public channel must never say where the house is to within a few hundred metres by accident)."""
    res = {"mode": mode, "channel": None, "applied": False, "note": ""}
    try:
        want, why = position_bits(mode)
        if want is None:
            res["note"] = why
            return res
        ch = public_ch if isinstance(public_ch, int) and public_ch >= 0 else 0
        res["channel"] = ch
        node = iface.localNode
        chans = list(getattr(node, "channels", None) or [])
        if ch >= len(chans):
            res["note"] = f"the radio has no channel {ch}"
            return res
        ms = getattr(getattr(chans[ch], "settings", None), "module_settings", None)
        if ms is None or not hasattr(ms, "position_precision"):
            res["note"] = "this radio/library has no per-channel position precision (firmware 2.3+ needed): left as it is"
            return res
        cur = int(getattr(ms, "position_precision", 0) or 0)
        state = read_json(state_path, {})
        state = state if isinstance(state, dict) else {}
        last = state.get(str(ch))
        too_fine = want != 32 and cur > POSITION_FINEST
        if cur == want:
            res["note"] = "already set"
        elif isinstance(last, int) and not isinstance(last, bool) and last != cur and not too_fine:
            res["note"] = (f"left at {cur} bits: it was changed on the radio after the Badger set {last} "
                           "(the owner's choice wins)")
            return res
        else:
            ms.position_precision = want
            node.writeChannel(ch)
            res["applied"] = True
            res["note"] = f"public channel position precision {cur} → {want} bits"
            if too_fine and isinstance(last, int) and last != cur:
                res["note"] += " (it had been set finer than ~3 km on the radio; only \"precise\" allows that)"
        if why:
            res["note"] = why + "; " + res["note"]
        state[str(ch)] = want
        atomic_json(state_path, state, 0o640)
        return res
    except Exception as e:  # noqa: BLE001 — older library / radio busy: leave it, say so
        res["note"] = f"position privacy not applied ({type(e).__name__}: {e})"
        return res


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
    """Messages queued by the portal, features and sensord ("Send over mesh"), read from the outbox
    file. Each line: {"text", "board"} plus, optionally (docs/MESH.md), "kind" (alert | danger | family |
    reply | community | auto — the priority; default: the board), "dedupe" (a key other Badgers compute
    the same way: only one Badger on the mesh sends it), "hop_limit" (1–7) and "at" (when it was queued:
    stale items expire instead of going out late). Old lines with just text and board work as before.
    Lines go to the Airtime queue (priorities, pacing, budgets), dedupe lines through the courtesy first.
    A post for a channel that isn't bridged is dropped (a community post never goes out on the family
    channel), and so is an echo of something just heard on the mesh (no Badger-to-Badger loops)."""
    CAPS = {"family": 50, "community": 20}

    def __init__(self, path: Path, family_ch: int, public_ch: int, send, clock, airtime: "Airtime | None" = None,
                 courtesy: "Courtesy | None" = None, echo=None):
        self.path, self.clock = path, clock
        self.ch = {"family": family_ch, "community": public_ch}
        if airtime is None:                            # standalone use: send(text, channel=…)
            airtime = Airtime(lambda text, dest=None, channel=0, hop_limit=None, reply_id=None, **_:
                              send(text, channel=channel), clock)
        self.airtime, self.courtesy, self.echo = airtime, courtesy, echo
        self.inflight: dict[int, str] = {}            # file offset → board, until sent / expired / suppressed
        self.lock = threading.Lock()
        self.offset_file = path.with_name(path.name + ".offset")
        path.touch(exist_ok=True)
        self.pos = load_offset(self.offset_file, path.stat().st_size)
        self.saved = None

    @staticmethod
    def parse(line: bytes, now: float) -> dict | None:
        """One outbox line → {"text","board","kind","dedupe","hop_limit","at"}; None if it isn't a post.
        Fields that don't validate fall back to the defaults (an old or odd writer still gets its post out)."""
        try:
            post = json.loads(line)
            text = str(post["text"])[:200]
            board = "family" if post.get("board") == "family" else "community"
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return None
        if not norm(text):
            return None
        kind = post.get("kind")
        kind = kind if isinstance(kind, str) and kind in KINDS else board
        dd = post.get("dedupe")
        dd = dd if isinstance(dd, str) and 0 < len(dd) <= 120 and dd.isprintable() else None
        hop = post.get("hop_limit")
        hop = hop if isinstance(hop, int) and not isinstance(hop, bool) and 1 <= hop <= 7 else None
        at = _num(post.get("at"))
        at = at if at is not None and now - 30 * 86400 < at <= now + 300 else now
        member = post.get("member") is True and board == "family"     # typed by a family member (radio silence)
        return {"text": text, "board": board, "kind": kind, "dedupe": dd, "hop_limit": hop, "at": at, "member": member}

    def _release(self, start: int) -> None:
        with self.lock:
            self.inflight.pop(start, None)

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
                post = self.parse(line, self.clock())
                if post is None:
                    continue                           # not a post: skip it
                board = post["board"]
                if self.ch[board] < 0:
                    print(f"meshbridge: no {board} channel is bridged — a {board} post was not sent", file=sys.stderr)
                    continue
                if self.echo is not None and self.echo(post["text"]):
                    print("meshbridge: an outbox line repeats a message just heard on the mesh — not sent back out",
                          file=sys.stderr)
                    continue
                with self.lock:
                    if sum(1 for b in self.inflight.values() if b == board) >= self.CAPS[board]:
                        print(f"meshbridge: {board} radio queue full — a post was dropped", file=sys.stderr)
                        continue
                    self.inflight[start] = board
                self._queue(post, start)

    def _queue(self, post: dict, start: int) -> None:
        ch, release = self.ch[post["board"]], (lambda *_a, s=start: self._release(s))
        item = {"kind": post["kind"], "text": post["text"], "ch": ch, "hop_limit": post["hop_limit"],
                "at": post["at"], "member": post["member"], "on_sent": release, "on_drop": release}
        key = post["dedupe"]
        if key and self.courtesy is not None:
            item["text"] = with_tag(post["text"], tag_of(key))
            self.courtesy.submit(key, post["kind"], ch, lambda it=item: self.airtime.put(it, pump=False),
                                 label=post["text"][:40], text=item["text"],
                                 match=exact_matcher(item["text"]) if post["kind"] in SAFETY else None,
                                 on_final=lambda state, by, r=release: None if state == "sent" else r())
        else:
            self.airtime.put(item, pump=False)

    def pump(self) -> None:
        self.airtime.pump()

    def checkpoint(self) -> None:
        """Remember where to start after a restart: the oldest post not yet sent."""
        with self.lock:
            queued = list(self.inflight)
        at = min(queued) if queued else self.pos
        if at != self.saved:
            save_offset(self.offset_file, at)
            self.saved = at
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
                    self.saved = 0
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


PAIR_RE = re.compile(r"^!pair\s+([A-Za-z0-9-]{4,16})\s*$", re.I)
PAIR_CODE_RE = re.compile(r"^[A-Z0-9]{4,12}$")
PAIR_PER_RADIO = (5, 600.0)             # "!pair" confirmations recorded per radio per 10 min
PAIR_TOTAL = (30, 600.0)                # … and from all radios together


def record_pair_confirm(path: Path, radio, code, now: float, keep: int = 20, max_age: float = 7200) -> bool:
    """A family radio sent "!pair <code>" on the family channel: note it where the portal looks, so binding a
    radio to a member needs a message from that very radio (it holds the family key and knows the code the
    portal showed). Rewrites the file atomically, 0600: {"v": 1, "confirms": [{"radio", "code", "at"}, …]},
    newest `keep`, none older than `max_age` s. Codes: letters/digits (dashes dropped), upper-cased, 4–12.
    Returns True when recorded; never raises."""
    try:
        if path is None or not isinstance(radio, str) or not isinstance(code, str):
            return False
        radio = radio.strip().lower()
        code = code.replace("-", "").upper()
        if not NODE_RE.match(radio) or not PAIR_CODE_RE.match(code) or _num(now) is None:
            return False
        path = Path(path)
        old = read_json(path, {}, limit=64_000)
        recs = old.get("confirms") if isinstance(old, dict) else None
        out = []
        for r in recs if isinstance(recs, list) else []:
            if not isinstance(r, dict):
                continue
            at, rr, cc = _num(r.get("at")), r.get("radio"), r.get("code")
            if at is None or not 0 <= now - at <= max_age or not isinstance(rr, str) or not NODE_RE.match(rr) \
                    or not isinstance(cc, str) or not PAIR_CODE_RE.match(cc):
                continue
            out.append({"radio": rr, "code": cc, "at": at})
        out.append({"radio": radio, "code": code, "at": float(now)})
        out = sorted(out, key=lambda r: r["at"])[-max(1, int(keep)):]
        path.parent.mkdir(parents=True, exist_ok=True)
        _private_file(path, json.dumps({"v": 1, "confirms": out}).encode(), 0o600)
        return True
    except Exception as e:  # noqa: BLE001 — a full disk or an odd file must not stop the bridge
        print(f"meshbridge: radio pairing confirmation not recorded ({type(e).__name__})", file=sys.stderr)
        return False


def family_key_exposed(members: Path, rotation: Path | None) -> bool:
    """True while a removed member may still hold the family channel key: someone was removed after the last
    successful key change the bridge made (mesh-rotate-result.json), or removed at an unknown time with no key
    change on record. Anyone with the key can transmit on the family channel with any radio ID, so until the
    family is re-keyed a family-channel post can't be confirmed as a member's (finding: removed member posts as
    Mom) and the channel isn't trusted for family-only commands."""
    d = read_json(members, {})
    mems = d.get("members") if isinstance(d, dict) else None
    if not isinstance(mems, dict):
        return False
    res = read_json(rotation, None) if rotation is not None else None
    rot = _num(res.get("at")) if isinstance(res, dict) and res.get("ok") is True else None
    for m in mems.values():
        if not isinstance(m, dict) or not m.get("removed"):
            continue
        when = _num(m["removed"])
        if when is None and rot is None:
            return True
        if when is not None and (rot is None or when > rot):
            return True
    return False


class Bridge:
    """What happens to packets heard on the mesh (the pubsub callbacks), and — with the mesh courtesy —
    what this Badger sends that other Badgers might send too (alert relays, replies on shared channels)."""

    def __init__(self, family_ch: int, public_ch: int, send, commands: Commands, members: Path,
                 telemetry: TelemetryLog | None, my_id: str | None, clock, *, courtesy: Courtesy | None = None,
                 airtime: Airtime | None = None, board=None, load=None, silence: Silence | None = None,
                 rotation: Path | None = None, pair_file: Path | None = None):
        self.family_ch, self.public_ch, self.send, self.commands = family_ch, public_ch, send, commands
        self.pair_file = pair_file                     # None: radio pairing confirmations off
        self.pair_seen: dict[str, collections.deque] = {}
        self.pair_all: collections.deque = collections.deque()
        self.rotation = rotation
        self._exposed = (-1e18, False)
        self._exposed_said = -1e18
        self.members, self.telemetry, self.my_id = members, telemetry, my_id
        self.clock = clock
        self.limits = {"family": RateLimit(clock), "community": RateLimit(clock)}   # one per board
        self.courtesy, self.airtime = courtesy, airtime
        self.hop_local = courtesy.cfg["hop_local"] if courtesy is not None else 2
        self.board = board or board_add
        self.load = load or (lambda: (None, None))
        self.silence = silence or (courtesy.silence if courtesy is not None else Silence(None))
        self.heard = Heard(clock)                      # loop guard: never send back out what we heard
        self.echo_keys: collections.OrderedDict = collections.OrderedDict()   # (board, key) → t: relays shown once
        self.alert_inflight: dict[str, dict] = {}
        self.done_alerts: dict = {}
        self.alerts_dirty = False
        self.booted = False
        self.lock = threading.Lock()

    def _pair_allowed(self, sender: str) -> bool:
        now = self.clock()
        with self.lock:
            for d, (n, win) in ((self.pair_all, PAIR_TOTAL), (self.pair_seen.setdefault(sender, collections.deque()),
                                                                PAIR_PER_RADIO)):
                while d and now - d[0] > win:
                    d.popleft()
                if len(d) >= n:
                    return False
            self.pair_all.append(now)
            self.pair_seen[sender].append(now)
            if len(self.pair_seen) > 500:
                for k in [k for k, d in self.pair_seen.items() if not d or now - d[-1] > PAIR_PER_RADIO[1]]:
                    del self.pair_seen[k]
        return True

    def family_exposed(self) -> bool:
        """family_key_exposed(), re-read at most every 5 s (members file + last key-change result)."""
        now = self.clock()
        t, v = self._exposed
        if 0 <= now - t < 5:
            return v
        v = family_key_exposed(self.members, self.rotation if self.rotation is not None else ROTATE_RES)
        self._exposed = (now, v)
        if v and now - self._exposed_said > 3600:
            self._exposed_said = now
            print("meshbridge: a removed member may still have the family channel key — family-channel posts are "
                  "marked unconfirmed and family-only commands are off until the family channel gets a new key",
                  file=sys.stderr)
        return v

    # — packets heard —
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
        if self.my_id and sender.lower() == self.my_id.lower():
            return                                   # our own message coming back
        to = packet.get("toId")
        direct = isinstance(to, str) and to not in ("", "^all") or (bool(self.my_id) and to == self.my_id)
        if direct and self.my_id and isinstance(to, str) and to.lower() != self.my_id.lower():
            return                                   # a DM for another radio (another Badger): not ours to answer
        info = self.courtesy.heard_text(text, sender, ch, packet) if self.courtesy is not None and not direct else {}
        reg = radio_registry(self.members).get(sender.lower())
        if reg and reg.get("removed"):
            print(f"meshbridge: ignored a message from a removed member's radio {sender}", file=sys.stderr)
            return
        if text[:5].lower() == "!pair":
            # radio pairing confirmation: only on the family channel, never a DM, never answered on air and
            # never on a board; anywhere else dropped without a word
            m = PAIR_RE.match(text)
            if m and self.family_ch >= 0 and ch == self.family_ch and not direct and self.pair_file is not None:
                if self.family_exposed():           # a removed member may hold the family key
                    print("meshbridge: pairing refused until the family channel is re-keyed", file=sys.stderr)
                elif self._pair_allowed(sender.lower()) and \
                        record_pair_confirm(self.pair_file, sender, m.group(1), self.clock()):
                    print(f"meshbridge: pairing confirmation from radio {sender.lower()}", file=sys.stderr)
            return
        board, trusted = route_text(text, ch, self.family_ch, self.public_ch, reg)
        exposed = board == "family" and self.family_exposed()
        if exposed:
            trusted = False                          # the key may be in a removed member's hands
        if text.startswith("!"):
            self._command(text, sender, ch, trusted, packet, direct)
            return
        if direct or board == "ignore":
            return                                   # direct messages and other channels: commands only
        self.heard.add(text)
        if info.get("ours"):
            return                                   # another Badger's copy of what we sent: shown here already
        bk = (board, info["board_key"]) if info.get("board_key") else None
        with self.lock:
            if bk and bk in self.echo_keys:
                return                               # the same relay from several Badgers: on the board once
        if not self.limits[board].allow(sender.lower()):
            return
        if bk:
            with self.lock:
                self.echo_keys[bk] = self.clock()
                while len(self.echo_keys) > 500:
                    self.echo_keys.popitem(last=False)
        nodes = getattr(interface, "nodes", None) if interface is not None else None
        node = nodes.get(sender, {}) if isinstance(nodes, dict) else {}
        long_name = clean(_dict(_dict(node).get("user")).get("longName"), 32)
        if board == "family" and reg and exposed:   # can't tell a member from a removed one holding the key
            self.board(f"{clean(reg['name'] or long_name or sender, 18)} (unconfirmed)", text, board, sender, None)
        elif board == "family" and reg:
            self.board(reg["name"] or long_name or sender, text, board, sender, reg["member"])
        else:                                        # community: never a member's name (IDs can be forged)
            self.board(long_name or sender, text, board, sender, None)

    def _command(self, text, sender, ch, trusted, packet, direct) -> None:
        req_id = packet.get("id")
        req_id = req_id if isinstance(req_id, int) and not isinstance(req_id, bool) and 0 < req_id < 2 ** 32 else None
        hops = hops_away(packet)
        word = text.partition(" ")[0].lower()
        if self.silence.active() or word not in COMMAND_WORDS:
            return                                   # radio silence: heard, never answered
        cour = self.courtesy if self.courtesy is not None and self.courtesy.enabled else None
        rid = req_id if req_id is not None else hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
        key = f"reply:{sender.lower()}:{rid}"
        if trusted:
            if direct or cour is None:
                # a family radio's DM to this Badger (only we got it), or courtesy off: answer at once
                self.commands.handle(text, sender, True,
                                     reply=lambda t: self._reply(t, sender, ch, req_id, hops, None, dm=True))
                return

            # heard on the family channel: the family's other Badgers heard it too — one of us answers
            def go():
                self.commands.handle(text, sender, True,
                                     reply=lambda t: self._reply(t, sender, ch, req_id, hops, key, dm=True),
                                     ignored=lambda: cour.done(key, ch))
            able = self.commands.would_answer(text, True)
            cour.submit(key, "reply", ch, go, hops=hops or 0, req_id=req_id, label=word,
                        extra=0.0 if able else BACKOFF["reply"][2],
                        on_final=lambda st, by: self.commands.note_answered(sender) if st == "suppressed" else None)
            return
        # a radio outside the family: only a real answer to a "community" command, on the public channel,
        # within the stranger budgets — otherwise nothing at all (no refusal, no control packet)
        if ch != self.public_ch or not self.commands.would_answer(text, False, sender):
            return
        if cour is None:
            self.commands.handle(text, sender, False,
                                 reply=lambda t: self._reply(t, sender, ch, req_id, hops, None, dm=direct))
            return

        def go_public():
            self.commands.handle(text, sender, False,
                                 reply=lambda t: self._reply(t, sender, ch, req_id, hops, key, dm=direct))
        cour.submit(key, "reply", ch, go_public, hops=hops or 0, req_id=req_id, label=word,
                    extra=cour.rng.uniform(*STRANGER_DM_DELAY) if direct else 0.0,
                    on_final=lambda st, by: self.commands.note_answered(sender) if st == "suppressed" else None)

    def _reply(self, t, sender, ch, req_id, hops, key, dm: bool = True) -> None:
        """A family radio gets a direct message; a stranger's question on the public channel is answered in
        the open as a reply to it (replyId), so every other Badger hears it and stays quiet."""
        dest = sender if dm else None
        if self.airtime is None:
            self.send(t, dest, ch)
            return
        hop = None if hops is None else min(7, max(self.hop_local, hops))    # far enough to reach the asker
        on_sent = (lambda: self.courtesy.done(key, ch)) if key and self.courtesy is not None else None
        self.airtime.put({"kind": "reply", "text": t, "dest": dest, "ch": ch, "hop_limit": hop,
                          "reply_id": req_id, "on_sent": on_sent})

    def on_data(self, packet, interface=None):
        """Everything the radio hears ("meshtastic.receive"): only PRIVATE_APP courtesy packets are ours."""
        try:
            if self.courtesy is not None:
                self.courtesy.heard_control(packet)
        except Exception as e:  # noqa: BLE001 — a hostile packet must not take the bridge down
            print(f"meshbridge: ignored a control packet ({type(e).__name__}: {e})", file=sys.stderr)

    def on_telemetry(self, packet, interface=None):
        if self.telemetry is not None:
            self.telemetry.handle(packet, getattr(interface, "nodes", None) if interface is not None else None)

    # — what we send —
    def relay_alerts(self, alerts, sent: dict, now: float, public: bool = True) -> None:
        """Each new NOAA alert once per channel (family, and public when `public`), through the courtesy:
        with other Badgers around, only one relays it on each channel. `sent` (alert id → expiry) is
        updated when every channel is done (relayed by us or heard from another Badger)."""
        at_boot = not self.booted
        self.booted = True
        self.done_alerts = sent
        chans = [c for c in (self.family_ch, self.public_ch if public else -1) if c >= 0]
        for a in alerts_to_send(alerts, sent, now):
            aid = a["id"]
            if aid in self.alert_inflight or not chans:
                continue
            exp = a.get("expires_ts", now + 6 * 3600)
            self.alert_inflight[aid] = {"left": set(chans), "exp": exp}
            text, match, label = format_alert(a), alert_matcher(a), clean(a.get("event"), 60) or "NOAA alert"
            for ch in chans:
                item = {"kind": "alert", "text": text, "ch": ch, "expires": exp,
                        "on_sent": lambda aid=aid, ch=ch, label=label: self._alert_final(aid, ch, "sent", label),
                        "on_drop": lambda why, aid=aid, ch=ch, label=label: self._alert_final(aid, ch, why, label)}

                def go(it=item):
                    if self.airtime is not None:
                        self.airtime.put(it)
                    elif self.send(it["text"], None, it["ch"]):
                        it["on_sent"]()
                if self.courtesy is None:
                    go()
                    continue
                self.courtesy.submit(alert_key(aid), "alert", ch, go, match=match, label=label, at_boot=at_boot, text=text,
                                     on_final=lambda st, by, aid=aid, ch=ch, label=label:
                                     None if st == "sent" else self._alert_final(aid, ch, st, label, by))

    def _alert_final(self, aid: str, ch: int, state: str, label: str, by: str | None = None) -> None:
        with self.lock:
            rec = self.alert_inflight.get(aid)
            if rec is None:
                return
            rec["left"].discard(ch)
            if state == "sent":
                print(f"meshbridge: relayed {label} to the mesh (channel {ch})", file=sys.stderr)
            if not rec["left"]:
                del self.alert_inflight[aid]
                self.done_alerts[aid] = rec["exp"]
                self.alerts_dirty = True

    def tick(self) -> None:
        if self.courtesy is not None:
            self.courtesy.tick()
        if self.airtime is not None:
            self.airtime.pump()

    def status_doc(self, now: float) -> dict:
        util, air = self.load()
        doc = {"updated": now, "channel_util": util, "air_util_tx": air, "silent": self.silence.active()}
        if self.courtesy is not None:
            doc.update(self.courtesy.status())
        if self.airtime is not None:
            doc["airtime"] = self.airtime.status()
        return doc


def circle_channel(cfg: dict, family_ch: int, public_ch: int) -> int:
    """[radio] mesh_circle_channel: a private channel shared with neighbouring Badgers' owners (its own key),
    used only for courtesy control packets. -1 = none (default); never 0, the public or the family channel."""
    v = cfg.get("mesh_circle_channel", -1)
    if isinstance(v, bool) or not isinstance(v, int) or not -1 <= v <= 7:
        print(f"meshbridge: [radio] mesh_circle_channel = {v!r} is not a channel (1–7, -1 for none): not used",
              file=sys.stderr)
        return -1
    if v in (0, public_ch) or (v >= 0 and v == family_ch):
        if v >= 0 and v != family_ch:
            print(f"meshbridge: mesh_circle_channel = {v} is the public channel: control packets never go there",
                  file=sys.stderr)
        return -1
    return v


def channel_secret(iface, ch: int) -> tuple[str, bytes | None]:
    """("ok", psk) for a channel with its own strong key; ("weak", None) for no key or one of Meshtastic's
    well-known default keys (a 0/1-byte psk); ("unknown", None) if the radio/library doesn't say."""
    try:
        chans = list(getattr(iface.localNode, "channels", None) or [])
        psk = bytes(chans[ch].settings.psk)
    except Exception:  # noqa: BLE001 — older library / fake radio
        return "unknown", None
    if len(psk) < 16:
        return "weak", None
    return "ok", psk


def build_bridge(link: RadioLink, *, cfg: dict, family_ch: int, public_ch: int, my_id: str | None, commands: Commands,
                 members: Path, telemetry: TelemetryLog | None = None, clock=None, rng=None, my_num: int | None = None,
                 board=None, peers_file: Path | None = None, load=None, silence: Silence | None = None,
                 ctrl_chs=None, ctrl_keys: dict | None = None, rotation: Path | None = None,
                 pair_file: Path | None = None) -> Bridge:
    """The bridge with its mesh courtesy and airtime queue wired to one radio link (main() and the
    many-Badger simulation in the tests build it the same way). Control packets only on the family channel and
    the circle channel (ctrl_chs; default: both, as configured), sealed with ctrl_keys (default: the fixed
    fallback key — main() passes keys derived from the radio's channel secrets). Beacons on the same channels
    unless [radio] mesh_beacons = false."""
    clock = clock or (lambda: time.time())
    cc = courtesy_config(cfg)
    load = load or (lambda: (None, None))
    silence = silence or link.silence
    if ctrl_chs is None:
        ctrl_chs = [c for c in (family_ch, circle_channel(cfg, family_ch, public_ch)) if c >= 0]
    ctrl_chs = [c for c in ctrl_chs if isinstance(c, int) and 0 < c <= 7 and c != public_ch]
    beacon_chs = list(ctrl_chs) if cc["beacons"] else []
    cour = Courtesy(lambda payload, ch: link.data(payload, ch), clock, rng, my_id=my_id, my_num=my_num, cfg=cc,
                    beacon_chs=beacon_chs, load=load, silence=silence, ctrl_chs=ctrl_chs,
                    ctrl_keys={c: k for c, k in (ctrl_keys or {}).items() if c in ctrl_chs})
    if peers_file is not None:
        saved = read_json(peers_file, {})
        cour.peers.load(saved.get("peers") if isinstance(saved, dict) else None)
    air = Airtime(link.text, clock, cc, peers=cour.peers.active, load=load, silence=silence)
    bridge = Bridge(family_ch, public_ch, lambda t, dest=None, ch=None: link.text(t, dest, ch), commands, members,
                    telemetry, my_id, clock, courtesy=cour, airtime=air, board=board, load=load, silence=silence,
                    rotation=rotation, pair_file=pair_file)
    return bridge


def stranger_budgets(cfg: dict) -> tuple[tuple[int, float], tuple[int, float]]:
    """[radio] mesh_stranger_replies_per_hour (default 6, 0–60) and mesh_stranger_replies_per_radio_hour
    (default 2, 0–10): how many answers radios outside the family get, all together / each."""
    total = int(_cnum(cfg.get("mesh_stranger_replies_per_hour"), STRANGER_BUDGET[0], 0, 60,
                      "mesh_stranger_replies_per_hour"))
    each = int(_cnum(cfg.get("mesh_stranger_replies_per_radio_hour"), STRANGER_PER_RADIO[0], 0, 10,
                     "mesh_stranger_replies_per_radio_hour"))
    return (total, 3600.0), (each, 3600.0)


def radio_ready(iface) -> bool:
    """True only when the interface really talks to a Meshtastic node. With no radio plugged in, meshtastic
    2.7.x hands back a half-built SerialInterface instead of raising: devPath None, no .nodes, no myInfo, and a
    localNode (if any) still at node number -1. Treating that as "connected" made the launcher show the mesh as
    up with no radio attached, and the map snapshot failed every 30 s."""
    try:
        if not getattr(iface, "devPath", None):
            return False
        num = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
        if isinstance(num, int) and not isinstance(num, bool) and num > 0:
            return True
        num = getattr(getattr(iface, "localNode", None), "nodeNum", None)
        return isinstance(num, int) and not isinstance(num, bool) and num > 0
    except Exception:  # noqa: BLE001 — a broken interface object is "no radio"
        return False


def close_quietly(iface) -> None:
    try:
        iface.close()
    except BaseException as e:  # noqa: BLE001 — a half-built interface may not even have a stream to close
        if isinstance(e, KeyboardInterrupt):
            raise


def open_radio(factory, sleep=None, wait: float = 60.0, log=None, tries: int | None = None):
    """Wait for a Meshtastic node: call factory() (the library's SerialInterface) until it returns an interface
    that radio_ready() accepts; anything else is closed and retried every `wait` seconds. The "no radio" line
    is logged when the reason changes and then once every 10 tries, not every minute. Returns the interface,
    or None after `tries` attempts (tests)."""
    log = log or (lambda m: print(m, file=sys.stderr))
    sleep = sleep or (lambda s: time.sleep(s))
    last, n = None, 0
    while tries is None or n < tries:
        n += 1
        try:
            iface = factory()
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001 — no device yet (the library calls sys.exit for that)
            iface, why = None, f"no Meshtastic node ({type(e).__name__}: {e})"
        else:
            if radio_ready(iface):
                return iface
            close_quietly(iface)
            iface, why = None, "no Meshtastic radio found (no device answered on the serial ports)"
        if why != last or n % 10 == 0:
            log(f"meshbridge: {why}; retrying every {int(wait)} s")
            last = why
        sleep(wait)
    return None


def main() -> int:
    global BOARD, FAMILY_BOARD, MEMBERS, OUTBOX, NODES, ALERTS, ROTATE_REQ, ROTATE_RES, CHANNEL_LINK, TELEMETRY, BT_PIN, PEERS
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
    PEERS = Path(os.environ.get("MESH_PEERS") or portal.get("mesh_peers") or OUTBOX.with_name("mesh-peers.json"))
    pair_file = Path(portal.get("mesh_pair_file") or PAIR_FILE)
    silent_file = Path(os.environ.get("MESH_SILENT_FILE") or portal.get("mesh_silent_file")
                       or OUTBOX.with_name("mesh-silent"))
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

    BEAT.unlink(missing_ok=True)                     # "not connected" until a real node answers
    iface = open_radio(meshtastic.serial_interface.SerialInterface)

    send_lock = threading.Lock()
    silence = Silence(silent_file, configured=cfg.get("mesh_silent") is True,
                      allow_family=cfg.get("mesh_silent_allow_family", True) is not False,
                      clock=lambda: time.time())
    link = RadioLink(iface, send_lock, silence)

    def send(text: str, dest: str | None = None, channel: int | None = None) -> bool:
        if channel is None:
            channel = family_ch if family_ch >= 0 else public_ch
        return link.text(text, dest, channel)

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
    budget_all, budget_each = stranger_budgets(cfg)
    commands = Commands(send, ai_enabled=cfg.get("mesh_ai", True) is not False, ai_cooldown=ai_cooldown, io=io,
                        access=access, public_budget=budget_all, per_radio_budget=budget_each)
    try:
        my_id = (iface.getMyNodeInfo() or {}).get("user", {}).get("id")
    except Exception:  # noqa: BLE001 — older library versions
        my_id = None
    try:
        my_num = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
    except Exception:  # noqa: BLE001
        my_num = None
    clock = lambda: time.time()  # noqa: E731 — (tests swap the module's time)
    load_cache = {"v": (None, None), "at": -1e18}

    def load():                                      # the radio's channel utilization, read once a minute
        return load_cache["v"]

    def refresh_load(now):
        if now - load_cache["at"] >= 60:
            load_cache.update(v=channel_load(iface), at=now)
    refresh_load(clock())
    # control packets: only on private channels whose own key the radio tells us (sealed with it)
    ctrl_chs = [c for c in (family_ch, circle_channel(cfg, family_ch, public_ch)) if c > 0 and c != public_ch]
    ctrl_keys: dict[int, bytes] = {}

    def channel_key(c: int) -> bytes | None:
        with send_lock:
            state, secret = channel_secret(iface, c)
        if state == "weak":
            print(f"meshbridge: channel {c} has no private key (or a well-known default one) — anyone can read it; "
                  "no Badger control packets there. Give it its own key in the Meshtastic app", file=sys.stderr)
            return None
        if state == "unknown":
            print(f"meshbridge: can't read channel {c}'s key from the radio: control packets there use a fixed key",
                  file=sys.stderr)
        return ctrl_key(secret)
    for c in list(ctrl_chs):
        k = channel_key(c)
        if k is None:
            ctrl_chs.remove(c)
        else:
            ctrl_keys[c] = k
    bridge = build_bridge(link, cfg=cfg, family_ch=family_ch, public_ch=public_ch, my_id=my_id, my_num=my_num,
                          commands=commands, members=MEMBERS, telemetry=TelemetryLog(TELEMETRY, family_ch, clock),
                          clock=clock, peers_file=PEERS, load=load, silence=silence, ctrl_chs=ctrl_chs,
                          ctrl_keys=ctrl_keys, rotation=ROTATE_RES, pair_file=pair_file)
    if silence.active():
        print("meshbridge: radio silence is on — receiving only", file=sys.stderr)
    if not bridge.courtesy.enabled:
        print("meshbridge: mesh courtesy is off ([radio] mesh_courtesy = false): acting alone", file=sys.stderr)
    lost = []

    def on_lost(interface=None, **_):
        lost.append(True)

    pub.subscribe(bridge.on_text, "meshtastic.receive.text")
    pub.subscribe(bridge.on_telemetry, "meshtastic.receive.telemetry")
    pub.subscribe(bridge.on_data, "meshtastic.receive")            # PRIVATE_APP: other Badgers' beacons/claims
    pub.subscribe(on_lost, "meshtastic.connection.lost")
    print("meshbridge: connected", file=sys.stderr)
    save_channel_link(iface, CHANNEL_LINK)
    if cfg.get("mesh_bt_pin", "secure") != "leave":
        secure_bt_pin(iface, BT_PIN)
    with send_lock:                                  # the radio's own position on the public channel
        pp = apply_position_privacy(iface, public_ch, cfg.get("mesh_public_position", "off"),
                                    OUTBOX.with_name("mesh-position-applied.json"))
    print(f"meshbridge: public position {pp['mode']}: {pp['note']}", file=sys.stderr)
    # Where we got to, kept next to the outbox: posts written while the bridge
    # was down (node unplugged, Pi rebooting) are still sent.
    outbox = Outbox(OUTBOX, family_ch, public_ch, send, clock, airtime=bridge.airtime, courtesy=bridge.courtesy,
                    echo=bridge.heard.seen)
    sent_file = OUTBOX.with_name("mesh-alerts-sent.json")
    sent_alerts = read_json(sent_file, {})
    if not isinstance(sent_alerts, dict):
        sent_alerts = {}
    last_snapshot, last_link, last_status = 0.0, time.time(), 0.0

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
                res = handle_rotation(iface, family_ch, ROTATE_REQ, ROTATE_RES, CHANNEL_LINK)
            if isinstance(res, dict) and res.get("ok") and family_ch > 0:
                bridge.courtesy.set_ctrl_key(family_ch, channel_key(family_ch))   # sealed with the new secret
        step("key change", rotation)

        step("channel load", lambda now=now: refresh_load(now))

        # NOAA alerts → one relay per channel per alert on the whole mesh, remembered across restarts
        def alerts(now=now):
            bridge.relay_alerts(read_json(ALERTS, []), sent_alerts, now, alerts_public)
            if bridge.alerts_dirty:
                bridge.alerts_dirty = False
                atomic_json(sent_file, sent_alerts)
        if relay_alerts:
            step("alerts", alerts)
        step("courtesy", bridge.tick)
        # positions → the offline map
        if share_map and now - last_snapshot >= 30:
            last_snapshot = now
            step("map snapshot", lambda now=now: atomic_json(NODES, {"updated": now, "nodes": nodes_snapshot(
                dict(getattr(iface, "nodes", None) or {}), my_id, now)}))
        # the channel link, kept fresh (it changes when someone edits channels in the Meshtastic app)
        if now - last_link >= 600:
            last_link = now
            step("channel link", lambda: save_channel_link(iface, CHANNEL_LINK))
            if family_ch in bridge.courtesy.ctrl_chs:     # re-keyed in the Meshtastic app? follow it
                step("channel key", lambda: bridge.courtesy.set_ctrl_key(family_ch, channel_key(family_ch)))
        # board posts marked "Send over mesh"
        step("outbox", lambda: (outbox.read(), outbox.pump(), outbox.checkpoint()))
        # Badgers nearby + what the courtesy did, for the launcher's status line and the front panel
        if now - last_status >= 30:
            last_status = now
            step("peers file", lambda now=now: atomic_json(PEERS, bridge.status_doc(now), 0o640))
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
