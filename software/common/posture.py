"""
posture — the Badger's security posture: ONE setting that drives every "how much does this Badger
show the world" knob (docs/features/posture.md). Standard library only; shared by the portal (and its
feature plugins), the mesh bridge, the front panel and os/render.py.

Three levels:
  neighbourly  more open: strangers may use !help and !q, the radio's position goes out coarsely (~6 km)
  guarded      the default: !q only for strangers (few answers an hour), no position, sharing beyond the
               Wi-Fi only by opt-in in a trusted circle
  lockdown     nothing beyond the family: radio silence (member-typed family posts may still go out),
               NOAA relays on the family channel only, no circle answers, the radio itself muted
               (CLIENT_MUTE, no node-info/position/telemetry broadcasts), visitors see only the library,
               the Wi-Fi needs a password

The contract other programs read — /var/lib/prepper/portal/posture.json ([portal] posture_file):
    {"level": "neighbourly" | "guarded" | "lockdown", "changed_at": <epoch seconds>, "by": "<who>"}
  * no file: the configured default ([security] posture, else "guarded");
  * a file that exists but can't be read or makes no sense: "lockdown" (fail closed: it was written
    atomically, so damage means tampering or a failing disk) — reported as {"damaged": true};
  * written only by the portal (admin or front panel), atomically, 0640.

Effective settings = the posture's defaults, then explicit owner overrides (prepper.toml keys, and the
launcher's own settings, which win), each shown with where it came from. In Lockdown an override may
only make things STRICTER: a looser one is ignored (and shown as such) — Lockdown means lockdown. Two
knobs are Lockdown's own options and always honoured: [security] lockdown_wifi and lockdown_radio.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path

LEVELS = ("neighbourly", "guarded", "lockdown")
DEFAULT = "guarded"
LABELS = {"neighbourly": "Neighbourly", "guarded": "Guarded", "lockdown": "Lockdown"}
BLURB = {
    "neighbourly": "More open: neighbours' radios may search the library and ask what's available; the radio "
                   "shares a rough position (within about 6 km).",
    "guarded": "The default: radios outside the family may only search the library, a few times an hour; no "
               "position on the air; nothing shared beyond the Wi-Fi unless you opt in.",
    "lockdown": "Nothing beyond the family: radio silence (family posts you type may still go out), weather "
                "alerts on the family channel only, the radio stops repeating and announcing itself, visitors "
                "on the Wi-Fi see only the library, and the Wi-Fi needs a password.",
}
DEFAULT_PATH = Path("/var/lib/prepper/portal/posture.json")
COMMANDS = ("help", "q", "ai", "status")
CMD_LEVELS = ("community", "family", "off", "ignore")          # loosest → strictest

# The posture's own values for every knob it drives.
TABLE: dict[str, dict] = {
    "neighbourly": {
        "mesh_commands": {"help": "community", "q": "community", "ai": "family", "status": "family"},
        "stranger_replies_per_hour": 12, "stranger_replies_per_radio_hour": 3,
        "public_position": "coarse", "radio_silence": False, "silent_allow_family": True,
        "alerts_public": True, "visitor_mesh": "approve", "visitor_sensors": "alerts",
        "visitor_radio_pages": "public", "visitor_community": "public",
        "wifi": "owner", "logs": "owner", "circle_sharing": "opt-in", "lockdown_radio": False,
    },
    "guarded": {
        "mesh_commands": {"help": "family", "q": "community", "ai": "family", "status": "family"},
        "stranger_replies_per_hour": 6, "stranger_replies_per_radio_hour": 2,
        "public_position": "off", "radio_silence": False, "silent_allow_family": True,
        "alerts_public": True, "visitor_mesh": "approve", "visitor_sensors": "alerts",
        "visitor_radio_pages": "public", "visitor_community": "public",
        "wifi": "owner", "logs": "ram", "circle_sharing": "opt-in", "lockdown_radio": False,
    },
    "lockdown": {
        "mesh_commands": {"help": "family", "q": "family", "ai": "family", "status": "family"},
        "stranger_replies_per_hour": 0, "stranger_replies_per_radio_hour": 0,
        "public_position": "off", "radio_silence": True, "silent_allow_family": True,
        "alerts_public": False, "visitor_mesh": "off", "visitor_sensors": "none",
        "visitor_radio_pages": "members", "visitor_community": "members",
        "wifi": "password", "logs": "ram", "circle_sharing": "none", "lockdown_radio": True,
    },
}
# What each knob means, for the posture page and the front panel (plain words).
KNOB_LABELS = {
    "mesh_commands": "Radio commands", "stranger_replies_per_hour": "Answers to radios outside the family, per hour",
    "stranger_replies_per_radio_hour": "…to any one of them", "public_position": "Radio position on the public channel",
    "radio_silence": "Radio silence", "silent_allow_family": "During silence, family posts you type still go out",
    "alerts_public": "NOAA alerts relayed on the public channel", "visitor_mesh": "Visitors' “send over radio” requests",
    "visitor_sensors": "Sensors for visitors", "visitor_radio_pages": "Aircraft and satellite pages for visitors",
    "visitor_community": "Neighbourhood pages for visitors", "wifi": "Wi-Fi", "logs": "Logs",
    "circle_sharing": "Sharing with a trusted circle", "lockdown_radio": "Radio muted (no repeating, no announcements)",
}
LOCKDOWN_OPTIONS = ("lockdown_wifi", "lockdown_radio")
_WIFI_CHOICES = ("password", "off", "keep")


# ───────────────────────────── the file ─────────────────────────────

def configured_default(raw: dict | None) -> str:
    sec = (raw or {}).get("security") if isinstance(raw, dict) else None
    v = sec.get("posture") if isinstance(sec, dict) else None
    return v if isinstance(v, str) and v in LEVELS else DEFAULT


def path_from(raw: dict | None) -> Path:
    portal = (raw or {}).get("portal") if isinstance(raw, dict) else None
    p = portal.get("posture_file") if isinstance(portal, dict) else None
    return Path(p) if isinstance(p, str) and p else DEFAULT_PATH


def read(path: Path | str | None = None, raw: dict | None = None) -> dict:
    """The current posture: {"level", "changed_at", "by", "label", "damaged", "source"}. Never raises."""
    p = Path(path) if path is not None else path_from(raw)
    try:
        st = os.stat(p)
    except OSError:
        lv = configured_default(raw)
        return {"level": lv, "changed_at": None, "by": None, "label": LABELS[lv], "damaged": False, "source": "default"}
    d = None
    try:
        if st.st_size <= 4096:
            with open(p, "rb") as fh:
                d = json.loads(fh.read(4097))
    except (OSError, ValueError, RecursionError):
        d = None
    lv = d.get("level") if isinstance(d, dict) else None
    if not isinstance(lv, str) or lv not in LEVELS:
        return {"level": "lockdown", "changed_at": None, "by": None, "label": LABELS["lockdown"], "damaged": True,
                "source": "file"}
    at = d.get("changed_at")
    at = at if isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) else None
    by = d.get("by")
    by = str(by)[:40] if isinstance(by, str) else None
    return {"level": lv, "changed_at": at, "by": by, "label": LABELS[lv], "damaged": False, "source": "file"}


def write(path: Path | str, level: str, by: str, now: float | None = None) -> dict:
    """Set the posture (atomic, 0640). Raises ValueError for an unknown level, OSError if it can't be saved."""
    if level not in LEVELS:
        raise ValueError(f"posture must be one of {', '.join(LEVELS)}")
    p = Path(path)
    rec = {"level": level, "changed_at": float(now if now is not None else time.time()),
           "by": " ".join(str(by or "?").split())[:40]}
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w") as fh:
            json.dump(rec, fh)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return rec


# ───────────────────────────── effective settings ─────────────────────────────

def _bits(v) -> int | None:
    """public_position → bits of precision (larger = looser), None for a value we can't read."""
    if isinstance(v, str):
        return {"off": 0, "coarse": 12, "precise": 32}.get(v)
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 32:
        return min(v, 13)
    return None


def _looser(knob: str, a, b) -> bool:
    """Is value `a` looser (shows more) than `b` for this knob?"""
    if knob in ("stranger_replies_per_hour", "stranger_replies_per_radio_hour"):
        return a > b
    if knob == "public_position":
        return (_bits(a) or 0) > (_bits(b) or 0)
    if knob in ("radio_silence",):
        return (not a) and bool(b)
    if knob in ("alerts_public", "silent_allow_family"):
        return bool(a) and not b
    if knob == "visitor_mesh":
        return a == "approve" and b == "off"
    if knob == "visitor_sensors":
        order = ("none", "alerts", "readings")
        return order.index(a) > order.index(b)
    if knob in ("visitor_radio_pages", "visitor_community"):
        return a == "public" and b == "members"
    if knob == "logs":
        return a == "persistent" and b != "persistent"
    return False


def _int(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return int(max(lo, min(hi, v)))


def overrides(raw: dict | None, settings: dict | None) -> dict:
    """Explicit owner choices, knob → (value, where). prepper.toml first, then the launcher's settings
    (portal-settings.json), which win. Values that don't validate are not overrides at all."""
    raw = raw if isinstance(raw, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    radio = raw.get("radio") if isinstance(raw.get("radio"), dict) else {}
    portal = raw.get("portal") if isinstance(raw.get("portal"), dict) else {}
    sec = raw.get("security") if isinstance(raw.get("security"), dict) else {}
    sensors = raw.get("sensors") if isinstance(raw.get("sensors"), dict) else {}
    out: dict = {}
    cmds: dict = {}
    for src, where in ((radio.get("mesh_commands"), "prepper.toml [radio] mesh_commands"),
                       (settings.get("mesh_commands"), "Settings → Radio commands")):
        if isinstance(src, dict):
            for k, v in src.items():
                k = str(k).lstrip("!").lower()
                if k in COMMANDS and isinstance(v, str) and v in CMD_LEVELS:
                    cmds[k] = (v, where)
    if cmds:
        out["mesh_commands"] = cmds
    for knob, key, lo, hi in (("stranger_replies_per_hour", "mesh_stranger_replies_per_hour", 0, 60),
                              ("stranger_replies_per_radio_hour", "mesh_stranger_replies_per_radio_hour", 0, 10)):
        v = _int(radio.get(key), lo, hi) if key in radio else None
        if v is not None:
            out[knob] = (v, f"prepper.toml [radio] {key}")
    for src, where in ((radio.get("mesh_public_position"), "prepper.toml [radio] mesh_public_position"),
                       (settings.get("mesh_public_position"), "Settings (what this Badger reveals)")):
        if _bits(src) is not None:
            out["public_position"] = (src, where)
    for knob, key in (("radio_silence", "mesh_silent"), ("silent_allow_family", "mesh_silent_allow_family"),
                      ("alerts_public", "mesh_alerts_public")):
        if isinstance(radio.get(key), bool):
            out[knob] = (radio[key], f"prepper.toml [radio] {key}")
    if portal.get("visitor_mesh") in ("approve", "off"):
        out["visitor_mesh"] = (portal["visitor_mesh"], "prepper.toml [portal] visitor_mesh")
    if sensors.get("public") is True:
        out["visitor_sensors"] = ("readings", "prepper.toml [sensors] public")
    for knob in ("visitor_radio_pages", "visitor_community"):
        for src, where in ((sec.get(knob), f"prepper.toml [security] {knob}"), (settings.get(knob), "Settings")):
            if src in ("public", "members"):
                out[knob] = (src, where)
    if sec.get("persistent_logs") is True:
        out["logs"] = ("persistent", "prepper.toml [security] persistent_logs")
    elif sec.get("persistent_logs") is False:
        out["logs"] = ("ram", "prepper.toml [security] persistent_logs")
    if sec.get("lockdown_radio") is False:
        out["lockdown_radio"] = (False, "prepper.toml [security] lockdown_radio")
    if sec.get("lockdown_wifi") in _WIFI_CHOICES:
        out["lockdown_wifi"] = (sec["lockdown_wifi"], "prepper.toml [security] lockdown_wifi")
    return out


def effective(level: str, raw: dict | None = None, settings: dict | None = None) -> dict:
    """knob → {"value", "posture", "source": "posture" | "override" | "ignored", "from"}. mesh_commands is
    a dict of per-command entries of the same shape. Every knob is always present."""
    level = level if level in LEVELS else "lockdown"
    base = TABLE[level]
    ov = overrides(raw, settings)
    lock = level == "lockdown"
    out: dict = {}
    for knob, pv in base.items():
        if knob == "mesh_commands":
            cmds = {}
            for c in COMMANDS:
                cv = pv[c]
                o = ov.get("mesh_commands", {}).get(c)
                ent = {"value": cv, "posture": cv, "source": "posture", "from": None}
                if o is not None:
                    v, where = o
                    if c == "help" and v == "off":
                        v = "family"                # !help is never "off": family radios can always ask
                    if lock and CMD_LEVELS.index(v) < CMD_LEVELS.index(cv):
                        ent.update(source="ignored", **{"from": where})
                    else:
                        ent.update(value=v, source="override", **{"from": where})
                cmds[c] = ent
            out[knob] = cmds
            continue
        ent = {"value": pv, "posture": pv, "source": "posture", "from": None}
        o = ov.get(knob)
        if o is not None:
            v, where = o
            if knob == "logs" and v == "persistent" and level != "neighbourly":
                ent.update(source="ignored", **{"from": where})        # persistent logs only when Neighbourly
            elif knob == "lockdown_radio":
                ent.update(value=bool(v) and pv, source="override" if pv else "posture", **{"from": where})
            elif lock and _looser(knob, v, pv):
                ent.update(source="ignored", **{"from": where})
            else:
                ent.update(value=v, source="override", **{"from": where})
        out[knob] = ent
    wifi_choice = ov.get("lockdown_wifi", ("password", None))
    out["lockdown_wifi"] = {"value": wifi_choice[0], "posture": "password",
                            "source": "override" if wifi_choice[1] else "posture", "from": wifi_choice[1]}
    return out


def values(eff: dict) -> dict:
    """{knob: value} (mesh_commands → {cmd: level}) from effective()."""
    out = {}
    for k, e in eff.items():
        if k == "mesh_commands":
            out[k] = {c: x["value"] for c, x in e.items()}
        else:
            out[k] = e["value"]
    return out


def bridge_commands(eff: dict) -> dict[str, str]:
    """mesh_commands in the mesh bridge's form ({"!q": "community", …})."""
    return {"!" + c: x["value"] for c, x in eff["mesh_commands"].items()}


def describe(knob: str, value) -> str:
    """A knob's value in plain words (posture page, front panel)."""
    if knob == "mesh_commands" and isinstance(value, dict):
        names = {"community": "anyone", "family": "family", "off": "off", "ignore": "nobody"}
        return ", ".join(f"!{c} {names.get(value.get(c), '?')}" for c in COMMANDS)
    if knob == "public_position":
        return {"off": "off", "coarse": "rough (~6 km)", "precise": "exact"}.get(value, f"{value} bits")
    if isinstance(value, bool):
        return "on" if value else "off"
    return {"approve": "need a family member's OK", "alerts": "alerts only", "readings": "alerts and readings",
            "none": "hidden", "public": "everyone", "members": "family only", "owner": "your choice",
            "password": "password required", "ram": "in memory only (gone at power-off)",
            "persistent": "kept on disk", "opt-in": "only what you opt in", "keep": "left as it is"}.get(str(value), str(value))
