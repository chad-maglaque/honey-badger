"""
What this Badger reveals — a live green/amber checklist (docs/features/reveal.md).

Everything that tells an outsider something — the Wi-Fi, the radio, the posture, what is shared with a trusted
circle, the front panel, records left unlocked, logs, keys on disk, what visitors on the Wi-Fi may open — in
one list, computed from the config and the live state, each line with a one-tap fix where one exists.
Never shown to visitors: the admin page and the front panel only.

  GET  /api/reveal         admin / front panel: {"items": [{"id", "state": "ok"|"warn"|"info", "title",
                           "detail", "fix": {"action", "label"} | null, "how": "…" | null}], "summary"}
  POST /api/reveal/fix     admin / front panel: {"action"} — one of the fixes the list offered
  POST /api/reveal/ack     admin / front panel: {"id": "recovery_printed"} — the owner says it's done
"""
from __future__ import annotations

import importlib
import json
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))
import posture as P  # noqa: E402

FEATURE = {"title": "What this Badger reveals", "icon": "👁️", "group": "tools", "script": "/features/reveal.js",
           "access": "admin", "description": "Everything that tells outsiders something, with fixes"}

ATTENTION = re.compile(r"honey\s*-?_?badger|badger|prepper|prepared|survival|surviv|bunker|stockpile|stash|cache|"
                       r"armou?ry|ammo|doomsday|shtf|bug\s*-?out|homestead|preps\b|emergency|rescue|shelter", re.I)
JOURNALD_CONF = Path("/etc/systemd/journald.conf.d/prepper.conf")
JOURNAL_DIR = Path("/var/log/journal")
TLS_DIR = Path("/etc/prepper/tls")


def _admin_or_panel(req, ctx) -> bool:
    return bool(req.is_admin() or req.local_console())


def _raw(ctx) -> dict:
    return ctx.raw_config() or {}


def _sec(ctx) -> dict:
    s = _raw(ctx).get("security")
    return s if isinstance(s, dict) else {}


def _path(ctx, key: str, default: Path) -> Path:
    v = _sec(ctx).get(key) or (_raw(ctx).get("portal") or {}).get(key)
    return Path(v) if isinstance(v, str) and v else default


def _acks(ctx) -> dict:
    d = ctx.read_json(ctx.data_dir() / "acks.json", None)
    return d if isinstance(d, dict) else {}


def _sealed():
    """software/common/sealed.py (the sealed-records feature), if this Badger has it."""
    try:
        return importlib.import_module("sealed")
    except Exception:  # noqa: BLE001 — not installed (yet): the line is left out
        return None


def _call(mod, *names):
    for n in names:
        fn = getattr(mod, n, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return None
    return None


def _peers(ctx) -> dict:
    d = ctx.read_json(ctx.config().mesh_peers, None)
    if not isinstance(d, dict) or not isinstance(d.get("updated"), (int, float)) or time.time() - d["updated"] > 900:
        return {}
    return d


def _item(id_, state, title, detail="", fix=None, how=None) -> dict:
    return {"id": id_, "state": state, "title": title, "detail": detail,
            "fix": {"action": fix[0], "label": fix[1]} if fix else None, "how": how}


def checks(ctx) -> list[dict]:
    raw = _raw(ctx)
    info, eff = ctx.posture()
    vals = P.values(eff)
    items: list[dict] = []
    net = raw.get("network") if isinstance(raw.get("network"), dict) else {}

    # — the Wi-Fi —
    sec, psk = net.get("security", "auto"), net.get("passphrase", "")
    if sec == "open" or (sec in ("owe", "owe-transition")) or (sec in ("auto", None) and not psk):
        kind = "open (no password, no encryption)" if sec == "open" else "without a password (encrypted, anyone can join)"
        items.append(_item("wifi_password", "warn", "Wi-Fi has no password",
                           f"The hotspot runs {kind}: anyone in range can join and open what visitors see.",
                           ("wifi_password", "Give the Wi-Fi a password")))
    else:
        items.append(_item("wifi_password", "ok", "Wi-Fi needs a password"))
    ssid = str(net.get("ssid", "Home-Hotspot"))
    if ATTENTION.search(ssid):
        items.append(_item("wifi_name", "warn", "Wi-Fi name draws attention",
                           f"“{ssid[:32]}” tells everyone in range what is in the house.", None,
                           "Pick a plain name (like Home-K7QX): [network] ssid in prepper.toml, then "
                           "sudo prepperctl apply-config. Every phone has to rejoin."))
    else:
        items.append(_item("wifi_name", "ok", "Wi-Fi name is plain"))

    # — the radio —
    pos = vals.get("public_position", "off")
    if pos != "off":
        items.append(_item("radio_position", "warn", "Radio shares its position on the public channel",
                           f"Position precision: {P.describe('public_position', pos)}.",
                           ("position_off", "Stop sharing the position")))
    else:
        items.append(_item("radio_position", "ok", "Radio position is off on the public channel"))
    peers = _peers(ctx)
    names = peers.get("radio_names") if isinstance(peers.get("radio_names"), dict) else None
    member_names = []
    try:
        member_names = [m["name"] for m in ctx.members().list()["members"] if not m.get("removed")]
    except Exception:  # noqa: BLE001
        member_names = []
    if names is None:
        items.append(_item("radio_name", "info", "Radio name: radio not connected", "Checked when the radio is plugged in."))
    else:
        ln, sn = str(names.get("long") or ""), str(names.get("short") or "")
        fam = [n for n in member_names if n and len(n) >= 3 and (n.lower() in ln.lower())]
        surname = any(w.lower() in ln.lower() for n in member_names for w in n.split()[1:] if len(w) >= 3)
        if ATTENTION.search(ln) or ATTENTION.search(sn) or fam or surname:
            items.append(_item("radio_name", "warn", "Radio name draws attention",
                               f"The radio calls itself “{ln[:40]}” ({sn[:8]}) to everyone in range.",
                               ("radio_rename", "Give it a plain name")))
        else:
            items.append(_item("radio_name", "ok", f"Radio name is plain ({ln[:24] or 'no name'})"))

    # — the posture —
    if info["level"] == "neighbourly":
        items.append(_item("posture", "warn", "Posture: Neighbourly", P.BLURB["neighbourly"],
                           ("posture_guarded", "Switch to Guarded")))
    else:
        items.append(_item("posture", "ok", f"Posture: {info['label']}" + (" (file damaged)" if info.get("damaged") else ""),
                           P.BLURB[info["level"]]))

    # — sharing beyond the Wi-Fi —
    circ = ctx.read_json(ctx.data_dir().parent / "circles" / "state.json", None)
    share = circ.get("share") if isinstance(circ, dict) and isinstance(circ.get("share"), dict) else {}
    in_circle = _in_circle(ctx)
    shared = [c for c in ("skills", "resources", "aid", "missing") if share.get(c) is True]
    if vals.get("circle_sharing") == "none":
        items.append(_item("sharing", "ok", "Nothing shared beyond the Wi-Fi (Lockdown)"))
    elif not in_circle or not shared:
        items.append(_item("sharing", "ok", "Nothing shared beyond the Wi-Fi"))
    else:
        for c in shared:
            items.append(_item(f"sharing_{c}", "warn", f"Shared with your trusted circle: {c}",
                               "Pointers only (“someone can help — ask …”), only to a circle radio that asks, a few "
                               "times an hour.", (f"stop_sharing:{c}", "Stop sharing this")))

    # — what the radio says to strangers —
    cmds = vals.get("mesh_commands", {})
    open_cmds = [f"!{c}" for c, lv in cmds.items() if lv == "community"]
    per_hour = vals.get("stranger_replies_per_hour", 0)
    bits = []
    if open_cmds and per_hour:
        bits.append(f"radios outside the family can make it answer {', '.join(open_cmds)} (up to {per_hour} an hour)")
    if vals.get("alerts_public"):
        bits.append("it relays NOAA alerts on the public channel (recognisable as an automatic relay)")
    if bits:
        items.append(_item("public_traffic", "warn", "The radio answers or relays in public", "; ".join(bits).capitalize() + ". "
                           "Control packets go only on the family/circle channels, sealed.",
                           ("public_quiet", "Answer only family radios")))
    else:
        items.append(_item("public_traffic", "ok", "No public replies; control packets only on private channels"))

    # — the front panel —
    if ctx.panel_lock_on():
        items.append(_item("panel_lock", "ok", "Front panel has a code"))
    else:
        items.append(_item("panel_lock", "warn", "Front panel has no code",
                           "Anyone at the knob can show the family radio key and the Wi-Fi password, or change the posture.",
                           ("panel_lock", "Set up a panel code")))

    # — sealed records (another feature; left out if this Badger doesn't have it) —
    sealed = _sealed()
    if sealed is not None:
        unlocked = _call(sealed, "is_unlocked")
        setup = _call(sealed, "is_set_up", "is_setup", "configured", "is_configured")
        if setup is None and callable(getattr(sealed, "current", None)):
            try:
                setup = bool(sealed.current().is_setup())
            except Exception:  # noqa: BLE001
                setup = None
        can_lock = callable(getattr(sealed, "lock_all", None)) or callable(getattr(sealed, "lock", None))
        if setup is False:
            items.append(_item("sealed", "warn", "Sealed records aren't set up",
                               "Community records are stored readable on the disk.", None,
                               "Set them up on the admin page (sealed records)."))
        elif unlocked:
            items.append(_item("sealed", "warn", "Sealed records are unlocked right now",
                               "Anyone who takes the Badger now could read them.",
                               ("sealed_lock", "Lock them now") if can_lock else None))
        elif unlocked is False:
            items.append(_item("sealed", "ok", "Sealed records are locked"))

    acks = _acks(ctx)
    if isinstance(acks.get("recovery_printed"), (int, float)):
        items.append(_item("recovery_printed", "ok", "Recovery card printed and put away"))
    else:
        items.append(_item("recovery_printed", "warn", "Recovery card not confirmed",
                           "Print the recovery codes (panel code, sealed records), keep them away from the Badger.",
                           ("ack_recovery", "I printed it and put it away")))

    if (_path(ctx, "tls_dir", TLS_DIR) / "ca.key").exists():
        items.append(_item("ca_key", "warn", "The local certificate authority's key is on the disk",
                           "Whoever copies the disk could make certificates phones trust (only for .lan names).", None,
                           "At the Badger: sudo prepperctl gen-tls (makes a new one and destroys the key; reinstall "
                           "the certificate on phones)."))
    else:
        items.append(_item("ca_key", "ok", "No certificate-authority key on the disk"))

    persistent = _logs_persistent(ctx) or vals.get("logs") == "persistent"
    items.append(_item("logs", "warn", "Logs are kept on the disk", "Who used the Badger, when, can be read later.",
                       ("logs_ram", "Keep logs in memory only")) if persistent
                 else _item("logs", "ok", "Logs are in memory only (gone at power-off)"))

    # — what visitors on the Wi-Fi can open —
    vis = ["library, maps, tools", "the community board"]
    feats = {f["id"]: f for f in ctx.features()} if hasattr(ctx, "features") else {}
    if vals.get("visitor_community") == "public":
        vis += [feats[f]["title"] for f in ("skills", "missing", "aidstation") if f in feats]
    if vals.get("visitor_radio_pages") == "public":
        vis += [feats[f]["title"] + " (area only, ~10 km)" for f in ("aircraft", "satellites") if f in feats]
    vs = vals.get("visitor_sensors")
    if "sensors" in feats and vs != "none":
        vis.append("sensor readings" if vs == "readings" else "sensor alerts")
    loose = vs == "readings" or vals.get("visitor_community") == "public"
    items.append(_item("visitor_pages", "warn" if loose else "info", "What visitors on the Wi-Fi can open",
                       "; ".join(vis) + ". Never: power, people at home, family pages, positions.",
                       ("visitors_members", "Neighbourhood pages for family only") if loose else None))

    # — a duress secret (docs/SECURITY.md → "Duress"): the made-up household is always ready; the secrets are
    # the owner's to set. While a duress is showing that household, this says what it would on a fresh Badger.
    cfg = ctx.config()
    decoy = bool(getattr(cfg, "decoy_active", False))
    sealer = None
    try:
        sealer = ctx.sealed() if callable(getattr(ctx, "sealed", None)) else None
    except Exception:  # noqa: BLE001
        sealer = None
    has_pin = bool(getattr(cfg, "duress_pin", "")) and not decoy
    has_phrase = bool(sealer is not None and not decoy and callable(getattr(sealer, "has_duress", None))
                      and sealer.has_duress())
    if has_pin or has_phrase:
        items.append(_item("duress", "ok", "A duress secret is set",
                           "Under threat it opens a made-up household and quietly locks (or wipes) the real one."))
    else:
        items.append(_item("duress", "warn", "No duress PIN or passphrase",
                           "If someone forces you to unlock the Badger, there is nothing to give them but the real one.",
                           None, "Admin page → Emergency → “If someone forces you to unlock the Badger”: set a duress "
                           "PIN, a duress passphrase and (at the knob) a duress code. A made-up household is ready."))

    # — panic wipe (another feature; only if this Badger has it) —
    wipe = _wipe(ctx)
    if wipe is not None:
        items.append(_item("wipe", "ok", "Panic wipe is set up") if wipe else
                     _item("wipe", "warn", "Panic wipe isn't set up", "If the Badger is taken, its records stay.", None,
                           "Set it up on the admin page."))
    return items


def _in_circle(ctx) -> bool:
    st = ctx.read_json(ctx.config().circle_status_file, None)
    ch = st.get("channel") if isinstance(st, dict) else None
    if isinstance(ch, int) and not isinstance(ch, bool) and ch > 0:
        return True
    r = _raw(ctx).get("radio") or {}
    if isinstance(r.get("mesh_circle_channel"), int) and not isinstance(r.get("mesh_circle_channel"), bool) \
            and r["mesh_circle_channel"] > 0:
        return True
    return ctx.config().mesh_outbox.with_name("mesh-circle-slot.json").exists()


def _logs_persistent(ctx) -> bool:
    conf = _path(ctx, "journald_conf", JOURNALD_CONF)
    try:
        text = conf.read_text()
        m = re.search(r"(?m)^\s*Storage\s*=\s*(\w+)", text)
        if not m or m.group(1).lower() != "volatile":
            return True
    except OSError:
        return False                                 # not a Badger install (tests, a laptop): nothing to say
    jd = _path(ctx, "journal_dir", JOURNAL_DIR)
    try:
        return any(jd.iterdir())
    except OSError:
        return False


def _wipe(ctx):
    """True/False when a panic-wipe / dead-man feature is present, None when this Badger has none."""
    for name in ("wipe", "panic"):
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001
            continue
        v = _call(mod, "configured", "is_configured", "is_set_up")
        if isinstance(v, bool):
            return v
    raw = _raw(ctx)
    for key in ("wipe", "panic", "deadman"):
        sec = raw.get(key)
        if isinstance(sec, dict):
            return sec.get("enabled", True) is not False
    s = _sec(ctx)
    if any(k in s for k in ("panic_wipe", "deadman", "dead_man")):
        return bool(s.get("panic_wipe") or s.get("deadman") or s.get("dead_man"))
    return None


def get_reveal(req, ctx, args):
    items = checks(ctx)
    warn = sum(1 for i in items if i["state"] == "warn")
    return {"items": items, "summary": {"warn": warn, "ok": sum(1 for i in items if i["state"] == "ok")},
            "checked_at": time.time()}


def _offered(ctx, action: str) -> bool:
    return any(i["fix"] and i["fix"]["action"] == action for i in checks(ctx))


def post_fix(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    action = d.get("action")
    if not isinstance(action, str) or len(action) > 40 or not _offered(ctx, action):
        return 400, {"ok": False, "error": "That fix isn't offered right now (reload the list)."}
    by = "front panel" if req.local_console() and not args["admin"] else "admin"
    msg = "Done."
    if action == "wifi_password":
        rc, out = ctx.ctl("wifi-secure")
        if rc != 0:
            return 500, {"ok": False, "error": out[-300:] or "the Wi-Fi password could not be set"}
        msg = ("The Wi-Fi gets a password in about 10 seconds. The Badger's screen shows it (knob → Join Wi-Fi). "
               "Rejoin with it and tap Keep on the admin page within 10 minutes, or it switches back by itself.")
    elif action == "position_off":
        ctx.update_settings(lambda s: dict(s, mesh_public_position="off"))
    elif action == "radio_rename":
        rec = {"id": secrets.token_hex(6), "at": time.time(), "op": "rename"}
        try:
            ctx.atomic_write(ctx.config().mesh_radio_request, json.dumps(rec))
        except OSError as e:
            return 500, {"ok": False, "error": f"couldn't ask the radio bridge ({e.strerror or e})"}
        msg = "The radio gets a plain name in a few seconds (it may restart)."
    elif action == "posture_guarded":
        P.write(ctx.config().posture_file, "guarded", by)
    elif action.startswith("stop_sharing:"):
        cat = action.split(":", 1)[1]
        p = ctx.data_dir().parent / "circles" / "state.json"
        st = ctx.read_json(p, None)
        if isinstance(st, dict) and isinstance(st.get("share"), dict):
            st["share"][cat] = False
            ctx.atomic_write(p, json.dumps(st))
            b = ctx.read_json(p.with_name("bridge.json"), None)
            if isinstance(b, dict) and isinstance(b.get("share"), dict):
                b["share"][cat] = False
                ctx.atomic_write(p.with_name("bridge.json"), json.dumps(b))
    elif action == "public_quiet":
        ctx.update_settings(lambda s: dict(s, mesh_commands={**(s.get("mesh_commands") if isinstance(
            s.get("mesh_commands"), dict) else {}), "q": "family", "help": "family"}))
    elif action == "panel_lock":
        until = time.time() + 600
        try:
            ctx.atomic_write(ctx.config().panel_lock_arm, json.dumps({"until": until}))
        except OSError as e:
            return 500, {"ok": False, "error": f"couldn't save that ({e.strerror or e})"}
        msg = "Now, within 10 minutes, at the Badger: knob → System → Panel lock → Set a code."
    elif action == "sealed_lock":
        mod = _sealed()
        try:
            (getattr(mod, "lock_all", None) or mod.lock)()
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "error": f"couldn't lock them ({type(e).__name__})"}
    elif action == "ack_recovery":
        return post_ack(req, ctx, {**args, "data": {"id": "recovery_printed"}})
    elif action == "logs_ram":
        rc, out = ctx.ctl("posture-apply")
        if rc != 0:
            return 500, {"ok": False, "error": out[-300:] or "couldn't change the log setting"}
    elif action == "visitors_members":
        ctx.update_settings(lambda s: dict(s, visitor_community="members", visitor_radio_pages="members"))
    else:
        return 400, {"ok": False, "error": "unknown fix"}
    ctx.log(f"reveal fix {action} by {by}")
    return {"ok": True, "message": msg, **get_reveal(req, ctx, args)}


def post_ack(req, ctx, args):
    d = args["data"] if isinstance(args["data"], dict) else {}
    if d.get("id") != "recovery_printed":
        return 400, {"ok": False, "error": "id: recovery_printed"}
    acks = _acks(ctx)
    acks["recovery_printed"] = time.time()
    ctx.atomic_write(ctx.data_dir() / "acks.json", json.dumps(acks))
    return {"ok": True, **get_reveal(req, ctx, args)}


ROUTES = [
    {"method": "GET", "path": "/api/reveal", "handler": get_reveal, "access": _admin_or_panel},
    {"method": "POST", "path": "/api/reveal/fix", "handler": post_fix, "access": _admin_or_panel},
    {"method": "POST", "path": "/api/reveal/ack", "handler": post_ack, "access": _admin_or_panel},
]

