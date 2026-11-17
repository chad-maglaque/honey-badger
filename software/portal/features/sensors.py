"""
Sensors — the portal side of the sensor station (software/sensors/sensord.py does the polling).

  GET  /api/sensors           current readings, remote mesh readings, active alerts   (readers)
  GET  /api/sensors/history   ?key=<sensor>:<metric>&range=24h|7d|30d|1y               (readers)
  GET  /api/sensors/alerts    just the active alerts, for a launcher banner           (everyone)
  GET  /api/sensors/admin     settings, rules, device diagnostics, mesh nodes heard   (admin)
  POST /api/sensors/admin     save settings (thresholds, remote nodes, mesh options)  (admin)
  POST /api/sensors/test      post a test message to a board (and the mesh)           (admin)

"Readers" are family members and the admin unless [sensors] public = true (then everyone on the
Wi-Fi): readings and a year of history (a tank level, a freezer probe, a well) say a lot about a
household. Active alerts (flood, freeze, radiation) are safety information for the whole
neighbourhood and go out over the mesh anyway, so /api/sensors/alerts stays open to everyone.
Error details, device paths and the list of radios heard are admin-only.

setup() starts a small thread that posts sensord's alert events (events.jsonl) to the community
or family board through ctx.board_post, catching up on anything written while the portal was
down. The mesh copy of an alert never waits for the portal: sensord writes the mesh outbox itself.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

_LIB = Path(__file__).resolve().parents[2] / "sensors" / "sensorlib.py"
if "badger_sensorlib" in sys.modules:
    sl = sys.modules["badger_sensorlib"]
else:
    _spec = importlib.util.spec_from_file_location("badger_sensorlib", _LIB)
    sl = importlib.util.module_from_spec(_spec)
    sys.modules["badger_sensorlib"] = sl
    _spec.loader.exec_module(sl)

FEATURE = {"title": "Sensors", "icon": "🌡️", "group": "community", "route": "/sensors",
           "script": "/features/sensors.js", "access": "public",
           "description": "Temperature, water level, flood and radiation alerts"}

RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "1y": 365 * 86400}
SYSTEM_NAME = "Badger sensors"
SYSTEM_MEMBER = {"id": "_sensors", "name": SYSTEM_NAME}     # the Badger itself, for the family board
_post_lock = threading.Lock()
_test_lock = threading.Lock()
_last_test = [0.0]


def _readers(req, ctx) -> bool:
    if (ctx.cfg() or {}).get("public", False) is True:
        return True
    return bool(req.member() or req.is_admin())


def _current(ctx) -> tuple[dict, bool]:
    cur = sl.read_json(ctx.data_dir() / "current.json", None)
    if not isinstance(cur, dict):
        return {}, False
    up = sl.finite(cur.get("updated")) or 0
    poll = sl.finite(cur.get("poll_s")) or 30
    running = time.time() - up < max(180, 4 * poll)
    return cur, running


def _settings(ctx) -> dict:
    return sl.effective_settings(ctx.cfg() or {}, sl.read_json(ctx.data_dir() / "settings.json", {}))


def _metric_list(values) -> list[dict]:
    out = []
    if not isinstance(values, dict):
        return out
    for metric, v in values.items():
        m = sl.METRICS.get(metric)
        v = sl.finite(v)
        if not m or v is None:
            continue
        out.append({"metric": metric, "label": m["label"], "unit": m["unit"], "value": v,
                    "text": sl.fmt_value(metric, v), "digits": m["digits"]})
    order = list(sl.METRICS)
    return sorted(out, key=lambda x: order.index(x["metric"]))


def _alerts(cur: dict, settings: dict) -> list[dict]:
    allowed = {sl.node_sensor_id(n) for n in settings["remote_nodes"]}
    out = []
    for a in cur.get("alerts") or []:
        if not isinstance(a, dict):
            continue
        if a.get("remote") and a.get("sensor") not in allowed:
            continue
        out.append({"key": str(a.get("key", ""))[:60], "sensor": str(a.get("sensor", ""))[:40],
                    "metric": str(a.get("metric", ""))[:24], "level": "danger" if a.get("level") == "danger" else "warning",
                    "text": sl.clean_text(a.get("text", ""), 300), "since": sl.finite(a.get("since")),
                    "value": sl.finite(a.get("value")), "stale": a.get("stale") is True, "remote": a.get("remote") is True})
    return out


def get_current(req, ctx, args):
    cur, running = _current(ctx)
    settings = _settings(ctx)
    admin = args["admin"]
    sensors = []
    for s in cur.get("sensors") or []:
        if not isinstance(s, dict):
            continue
        item = {"id": str(s.get("id", ""))[:40], "name": sl.clean_text(s.get("name", ""), 40),
                "type": str(s.get("type", ""))[:20], "ok": s.get("ok") is True, "at": sl.finite(s.get("at")),
                "metrics": _metric_list(s.get("values") if s.get("ok") else {}),
                "last": _metric_list(s.get("last_values"))}
        if admin:
            item["error"] = sl.clean_text(s.get("error", ""), 200)
        sensors.append(item)
    allowed = set(settings["remote_nodes"])
    remote = []
    for r in cur.get("remote") or []:
        if not isinstance(r, dict) or r.get("node") not in allowed:
            continue
        remote.append({"id": sl.node_sensor_id(r["node"]), "node": r["node"], "name": sl.clean_text(r.get("name", ""), 40),
                       "trusted": r.get("trusted") is True, "at": sl.finite(r.get("at")),
                       "metrics": _metric_list(r.get("values"))})
    units = (ctx.cfg() or {}).get("units", "metric")
    return {"running": running, "updated": sl.finite(cur.get("updated")), "sensors": sensors, "remote": remote,
            "alerts": _alerts(cur, settings), "units": "us" if units in ("us", "imperial") else "metric"}


def get_alerts(req, ctx, args):
    cur, running = _current(ctx)
    return {"running": running, "alerts": _alerts(cur, _settings(ctx))}


def get_history(req, ctx, args):
    qs = args["qs"]
    key = (qs.get("key") or [""])[0]
    rng = (qs.get("range") or ["24h"])[0]
    if not sl.KEY_RE.match(key) or rng not in RANGES:
        return 400, {"error": "unknown sensor or range"}
    sensor = key.split(":", 1)[0]
    if sensor.startswith("mesh-") and "!" + sensor[5:] not in _settings(ctx)["remote_nodes"]:
        return 404, {"error": "no such sensor"}
    path = ctx.data_dir() / "readings.db"
    now = time.time()
    try:
        store = sl.Store(path, readonly=True)
    except (FileNotFoundError, sqlite3.Error):
        return {"key": key, "range": rng, "points": []}
    try:
        pts = store.history(key, RANGES[rng], now, points=240)
    except sqlite3.Error as e:
        return 503, {"error": f"readings are busy, try again ({e})"[:120]}
    finally:
        store.close()
    metric = key.split(":", 1)[1]
    digits = sl.METRICS.get(metric, {}).get("digits", 1) + 1
    return {"key": key, "range": rng, "now": now, "unit": sl.METRICS.get(metric, {}).get("unit", ""),
            "points": [[p[0], round(p[1], digits), round(p[2], digits), round(p[3], digits)] for p in pts]}


def get_admin(req, ctx, args):
    cur, running = _current(ctx)
    saved = sl.read_json(ctx.data_dir() / "settings.json", {})
    cfg = ctx.cfg() or {}
    devices = []
    for s in cur.get("sensors") or []:
        if isinstance(s, dict):
            devices.append({k: s.get(k) for k in ("id", "name", "type", "ok", "error", "at", "auto", "poll_s", "port")})
    seen = [n for n in (cur.get("seen_nodes") or []) if isinstance(n, dict) and isinstance(n.get("node"), str)][:32]
    return {"running": running, "updated": sl.finite(cur.get("updated")),
            "settings": saved if isinstance(saved, dict) else {}, "effective": _settings(ctx),
            "rules": cur.get("rules") if isinstance(cur.get("rules"), list) else sl.config_rules(cfg),
            "default_rules": sl.config_rules(cfg), "devices": devices, "seen_nodes": seen,
            "config_errors": cur.get("config_errors") or [],
            "metrics": {k: {"label": m["label"], "unit": m["unit"]} for k, m in sl.METRICS.items()}}


def save_admin(req, ctx, args):
    data = args["data"]
    if not isinstance(data, dict):
        return 400, {"error": "send the settings as an object"}
    try:
        new = sl.validate_settings(data)
    except ValueError as e:
        return 400, {"error": str(e)[:200]}
    p = ctx.data_dir() / "settings.json"
    saved = sl.read_json(p, {})
    saved = saved if isinstance(saved, dict) else {}
    saved.update(new)
    if saved.get("rules", 0) is None:
        saved.pop("rules")                               # null = back to the config file / built-in rules
    saved = {k: saved[k] for k in sl.SETTINGS_KEYS if k in saved}
    ctx.atomic_write(p, json.dumps(saved, ensure_ascii=False, indent=1))
    try:
        p.chmod(0o640)
    except OSError:
        pass
    return {"ok": True, "settings": saved, "effective": _settings(ctx)}


def send_test(req, ctx, args):
    data = args["data"] if isinstance(args["data"], dict) else {}
    board = data.get("board", "community")
    if board not in ("community", "family"):
        return 400, {"error": "board must be community or family"}
    with _test_lock:
        now = time.monotonic()                      # a clock set back must not block tests for hours
        if _last_test[0] and now - _last_test[0] < 60:
            return 429, {"error": "one test a minute"}
        _last_test[0] = now
    text = "Test from the Badger's sensor station — no action needed."
    ok, msg = ctx.board_post("sensors", SYSTEM_NAME, text, False, board, SYSTEM_MEMBER if board == "family" else None)
    mesh = bool(data.get("mesh") is True and ctx.mesh_send(sl.MESH_PREFIX + text, board))
    return (200 if ok else 503), {"ok": ok, "message": msg, "mesh": mesh}


# ─────────────────────────────── events → boards ────────────────────────────

def _post_state_path(ctx) -> Path:
    return ctx.data_dir() / "board-posted.json"


def post_pending(ctx, now: float | None = None, max_posts: int = 3) -> int:
    """Post sensord's new alert/clear events to their boards; returns how many were posted.
    First run starts an hour back (an old install shouldn't replay its whole history); events
    older than a day are skipped; the board's own 5-second limit means "try again next round"."""
    now = time.time() if now is None else now
    with _post_lock:
        events = sorted(sl.read_events(ctx.data_dir()), key=lambda e: e["seq"])
        st = sl.read_json(_post_state_path(ctx), None)
        if not isinstance(st, dict) or not isinstance(st.get("seq"), int):
            st = {"seq": max([e["seq"] for e in events if (sl.finite(e.get("at")) or 0) < now - 3600] or [0])}
        start, posted = st["seq"], 0
        for ev in events:
            if ev["seq"] <= st["seq"]:
                continue
            at = sl.finite(ev.get("at")) or 0
            board = ev.get("board")
            if ev.get("kind") not in ("alert", "clear") or board not in ("community", "family") or at < now - 86400:
                st["seq"] = ev["seq"]
                continue
            text = ("⚠ " if ev["kind"] == "alert" else "✓ ") + sl.clean_text(ev.get("text", ""), 500)
            if now - at > 600:
                text += time.strftime(" (at %H:%M)", time.localtime(at))
            ok, msg = ctx.board_post("sensors", SYSTEM_NAME, text, False, board,
                                     SYSTEM_MEMBER if board == "family" else None)
            if not ok and "slow down" in str(msg):
                break                                    # the board's rate limit: next round
            if not ok:
                ctx.log(f"board post refused: {msg}")
            st["seq"] = ev["seq"]
            posted += ok
            if posted >= max_posts:
                break
        if st["seq"] != start or (events and not _post_state_path(ctx).exists()):
            ctx.atomic_write(_post_state_path(ctx), json.dumps(st))
        return posted


def _poster(ctx, interval: float) -> None:
    while True:
        try:
            post_pending(ctx)
        except Exception as e:  # noqa: BLE001 — keep posting after a bad file or a full disk
            ctx.log(f"posting sensor events failed: {type(e).__name__}: {e}")
        time.sleep(interval)


def setup(ctx):
    cfg = ctx.cfg() or {}
    if cfg.get("post_events", True) is False:
        return
    try:
        interval = max(1.0, float(cfg.get("post_interval_s", 6)))
    except (TypeError, ValueError):
        interval = 6.0
    threading.Thread(target=_poster, args=(ctx, interval), name="sensors-poster", daemon=True).start()


ROUTES = [
    {"method": "GET", "path": "/api/sensors", "handler": get_current, "access": _readers},
    {"method": "GET", "path": "/api/sensors/alerts", "handler": get_alerts, "access": "public"},
    {"method": "GET", "path": "/api/sensors/history", "handler": get_history, "access": _readers},
    {"method": "GET", "path": "/api/sensors/admin", "handler": get_admin, "access": "admin"},
    {"method": "POST", "path": "/api/sensors/admin", "handler": save_admin, "access": "admin"},
    {"method": "POST", "path": "/api/sensors/test", "handler": send_test, "access": "admin"},
]
