"""
Security posture — one switch: Neighbourly / Guarded (default) / Lockdown (docs/features/posture.md).

The posture is ONE source of truth for every "how much does this Badger show the world" knob: radio
commands, answers to strangers, the radio's position on the public channel, radio silence, NOAA relays on
the public channel, visitors' radio requests, what visitors on the Wi-Fi see, the Wi-Fi password, logs,
sharing with a trusted circle, and in Lockdown the radio itself (muted). The rules live in
software/common/posture.py; this plugin shows them and changes the level.

  GET  /api/posture            family members, the admin, the front panel: level + effective settings
  POST /api/posture            admin: {"level", "wifi": "password" | "off" | "keep" (Lockdown only, optional)}
  POST /api/posture/panel      the front panel (behind its code, if one is set): {"level", "wifi"}
  POST /api/posture/overrides  admin: {"clear": ["mesh_commands", …]} — back to the posture's own values

posture.json ({"level", "changed_at", "by"}) is the contract other programs read: the mesh bridge, the
front panel, os/render.py (logs) and the sealed-records feature (which locks itself on Lockdown).
Entering Lockdown on a hotspot without a password starts the usual failsafe Wi-Fi password change
(prepperctl wifi-secure: switches back by itself unless someone rejoins and confirms within 10 minutes;
the front panel always shows the current password), or turns the Wi-Fi off if asked.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))
import posture as P  # noqa: E402

FEATURE = {"title": "Security posture", "icon": "🛡️", "group": "tools", "script": "/features/posture.js",
           "access": "admin", "description": "How much this Badger shows the world"}

CLEARABLE = ("mesh_commands", "mesh_public_position", "visitor_radio_pages", "visitor_community")


def _family_or_admin_or_panel(req, ctx) -> bool:
    return bool(req.member() or req.is_admin() or req.local_console())


def hotspot_open(raw: dict) -> bool:
    """True when the hotspot has no password (OWE "enhanced open", or fully open)."""
    net = raw.get("network") if isinstance(raw.get("network"), dict) else {}
    sec = net.get("security", "auto")
    psk = net.get("passphrase", "")
    return sec in ("owe", "open", "owe-transition") or (sec in ("auto", None) and not psk)


def summary(ctx) -> dict:
    info, eff = ctx.posture()
    rows = []
    for knob, ent in eff.items():
        if knob == "mesh_commands":
            val = {c: e["value"] for c, e in ent.items()}
            pv = {c: e["posture"] for c, e in ent.items()}
            srcs = [e for e in ent.values() if e["source"] != "posture"]
            src = "override" if any(e["source"] == "override" for e in srcs) else ("ignored" if srcs else "posture")
            frm = sorted({e["from"] for e in srcs if e["from"]})
            rows.append({"knob": knob, "label": P.KNOB_LABELS.get(knob, knob), "value": P.describe(knob, val),
                         "posture": P.describe(knob, pv), "source": src, "from": ", ".join(frm) or None})
            continue
        rows.append({"knob": knob, "label": P.KNOB_LABELS.get(knob, "In Lockdown, the Wi-Fi" if knob == "lockdown_wifi"
                                                             else knob),
                     "value": P.describe(knob, ent["value"]), "posture": P.describe(knob, ent["posture"]),
                     "source": ent["source"], "from": ent["from"]})
    raw = ctx.raw_config()
    return {"level": info["level"], "label": info["label"], "changed_at": info.get("changed_at"), "by": info.get("by"),
            "damaged": bool(info.get("damaged")), "levels": [{"id": lv, "label": P.LABELS[lv], "blurb": P.BLURB[lv]}
                                                              for lv in P.LEVELS],
            "settings": rows, "wifi_open": hotspot_open(raw)}


def get_posture(req, ctx, args):
    out = summary(ctx)
    if not (args["admin"] or req.local_console()):
        out.pop("by", None)                          # family members see the level and what it does, not who
    return out


def change(req, ctx, level, by: str, wifi_choice=None, secure: bool = False) -> tuple[int, dict]:
    if level not in P.LEVELS:
        return 400, {"ok": False, "error": "choose Neighbourly, Guarded or Lockdown"}
    if wifi_choice not in (None, "password", "off", "keep"):
        return 400, {"ok": False, "error": "wifi must be password, off or keep"}
    before, _ = ctx.posture()
    try:
        P.write(ctx.config().posture_file, level, by)
    except OSError as e:
        return 500, {"ok": False, "error": f"Couldn't save the posture ({e.strerror or e}). Nothing changed."}
    ctx.log(f"posture {before['level']} → {level} by {by}")
    info, eff = ctx.posture(fresh=True)
    notes, wifi = [], None
    rc, out = ctx.ctl("posture-apply")               # logs in RAM unless Neighbourly + persistent_logs
    if rc != 0:
        notes.append("The log setting couldn't be applied right now: " + out[-200:])
    if level == "lockdown":
        choice = wifi_choice or eff["lockdown_wifi"]["value"]
        if choice == "off":
            rc, out = ctx.ctl("wifi-off")
            wifi = {"action": "off", "ok": rc == 0, "output": out[-300:]}
        elif choice == "password" and hotspot_open(ctx.raw_config()):
            rc, out = ctx.ctl("wifi-secure")
            pw = next((ln.split(": ", 1)[1].strip() for ln in out.splitlines() if ln.startswith("passphrase: ")), None)
            wifi = {"action": "password", "ok": rc == 0 and bool(pw), "applies_in_s": 10, "confirm_within_s": 600,
                    "passphrase": pw if (pw and secure) else None, "secret_hidden": bool(pw) and not secure,
                    "output": None if rc == 0 else out[-300:]}
            if wifi["ok"]:
                notes.append("The Wi-Fi gets a password in about 10 seconds. Rejoin with it (the Badger's screen shows "
                             "it: knob → Join Wi-Fi) and tap Keep on the admin page within 10 minutes, or it switches "
                             "back by itself.")
    if before["level"] != level:
        try:
            ctx.board_post("system:posture", "", f"The Badger's security posture is now {P.LABELS[level]} "
                           f"(set {'at the Badger' if by == 'front panel' else 'by the admin'}).", False, "family",
                           system="Badger")
        except Exception:  # noqa: BLE001 — the change stands even if the note can't be written
            pass
    return 200, {"ok": True, "posture": summary(ctx), "wifi": wifi, "notes": notes}


def post_posture(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    return change(req, ctx, d.get("level"), "admin", d.get("wifi"), req.secure_transport())


def post_panel(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    return change(req, ctx, d.get("level"), "front panel", d.get("wifi"), True)


def post_overrides(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    keys = d.get("clear")
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k in CLEARABLE for k in keys):
        return 400, {"ok": False, "error": f"clear: a list of {', '.join(CLEARABLE)}"}

    def drop(s):
        for k in keys:
            s.pop(k, None)
        return s
    ctx.update_settings(drop)
    return {"ok": True, "posture": summary(ctx)}


ROUTES = [
    {"method": "GET", "path": "/api/posture", "handler": get_posture, "access": _family_or_admin_or_panel},
    {"method": "POST", "path": "/api/posture", "handler": post_posture, "access": "admin"},
    {"method": "POST", "path": "/api/posture/panel", "handler": post_panel, "access": "local"},
    {"method": "POST", "path": "/api/posture/overrides", "handler": post_overrides, "access": "admin"},
]


def setup(ctx):
    info, _ = ctx.posture()
    if info.get("damaged"):
        ctx.log("posture.json is damaged: treated as Lockdown until the admin sets the posture again")
