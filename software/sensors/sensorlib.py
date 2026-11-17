"""
sensorlib — the parts of the sensor station shared by sensord (the daemon) and the portal's
"sensors" feature: the metric table, the reading store (SQLite with downsampling), alert rules
with hysteresis/cooldown, admin settings validation, and the Meshtastic telemetry parser.

Stdlib only. Files (all in the feature's data dir, /var/lib/prepper/portal/features/sensors/):
  readings.db        SQLite (WAL): raw readings 48 h, 10-minute averages 30 days, hourly 1 year
  current.json       latest readings, device health, active alerts (written by sensord)
  events.jsonl       alert/clear/summary events, one JSON object per line (+ events.jsonl.1)
  settings.json      admin overrides from the portal (thresholds, remote nodes, mesh options)
  sensord-state.json sensord's own bookkeeping (alert states, mesh rate limit, telemetry offset)
  board-posted.json  how far the portal got posting events to the boards
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sqlite3
import threading
from pathlib import Path

# ─────────────────────────────── metrics ─────────────────────────────────────
# unit, friendly label, sane range (readings outside it are dropped as junk), display digits
METRICS: dict[str, dict] = {
    "temperature": {"unit": "°C", "label": "Temperature", "min": -60, "max": 150, "digits": 1},
    "humidity": {"unit": "%", "label": "Humidity", "min": 0, "max": 100, "digits": 0},
    "pressure": {"unit": "hPa", "label": "Air pressure", "min": 300, "max": 1100, "digits": 0},
    "water_level": {"unit": "cm", "label": "Water level", "min": -2000, "max": 10000, "digits": 0},
    "flood": {"unit": "", "label": "Flood switch", "min": 0, "max": 1, "digits": 0},
    "radiation": {"unit": "µSv/h", "label": "Radiation", "min": 0, "max": 100000, "digits": 2},
    "cpm": {"unit": "CPM", "label": "Geiger counts", "min": 0, "max": 10_000_000, "digits": 0},
    "voltage": {"unit": "V", "label": "Voltage", "min": -1, "max": 100, "digits": 2},
    "battery": {"unit": "%", "label": "Battery", "min": 0, "max": 101, "digits": 0},
    "lux": {"unit": "lx", "label": "Light", "min": 0, "max": 200000, "digits": 0},
    "iaq": {"unit": "IAQ", "label": "Air quality index", "min": 0, "max": 500, "digits": 0},
    "co2": {"unit": "ppm", "label": "CO₂", "min": 0, "max": 40000, "digits": 0},
    "pm25": {"unit": "µg/m³", "label": "Fine dust (PM2.5)", "min": 0, "max": 2000, "digits": 0},
    "pm10": {"unit": "µg/m³", "label": "Dust (PM10)", "min": 0, "max": 2000, "digits": 0},
    "wind_speed": {"unit": "m/s", "label": "Wind", "min": 0, "max": 100, "digits": 1},
    "rain_1h": {"unit": "mm", "label": "Rain (last hour)", "min": 0, "max": 500, "digits": 1},
    "soil_moisture": {"unit": "%", "label": "Soil moisture", "min": 0, "max": 100, "digits": 0},
    "soil_temperature": {"unit": "°C", "label": "Soil temperature", "min": -60, "max": 100, "digits": 1},
}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")             # sensor ids (config + auto-detected)
NODE_RE = re.compile(r"^![0-9a-f]{8}$")                       # Meshtastic node ids
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}:[a-z0-9_]{1,24}$")
BOARDS = ("community", "family", "none")
MESH_PREFIX = "Sensors: "          # over the air: never names the device (docs/MESH.md → Privacy)
LEVELS = ("warning", "danger")
CTRL = re.compile(r"[\x00-\x1f\x7f  ‪-‮⁦-⁩]")


def clean_text(s, limit: int) -> str:
    """Control and bidi-override characters out, whitespace collapsed, length capped."""
    return " ".join(CTRL.sub(" ", str(s)).split())[:limit]


def node_sensor_id(node: str) -> str:
    """'!abcd1234' → 'mesh-abcd1234' (the sensor id a remote node's readings are filed under)."""
    return "mesh-" + node[1:]


def finite(v) -> float | None:
    """A real number (not bool, not NaN/inf), else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def in_range(metric: str, v) -> float | None:
    v = finite(v)
    m = METRICS.get(metric)
    if v is None or m is None or not (m["min"] <= v <= m["max"]):
        return None
    return v


def fmt_value(metric: str, v: float | None) -> str:
    if v is None:
        return "—"
    if metric == "flood":
        return "WET" if v >= 0.5 else "dry"
    m = METRICS.get(metric, {"unit": "", "digits": 1})
    s = f"{v:.{m['digits']}f}"
    if s.startswith("-") and float(s) == 0:
        s = s[1:]
    unit = m["unit"]
    return s + ("" if not unit else unit if unit in ("°C", "%") else " " + unit)


def cut_bytes(text: str, limit: int = 200) -> str:
    """Trim to `limit` UTF-8 bytes, ending in … when shortened (LoRa packets are small)."""
    text = " ".join(str(text).split())
    if len(text.encode()) <= limit:
        return text
    while text and len((text + "…").encode()) > limit:
        text = text[:-1]
    return text + "…"


# ─────────────────────────────── small file helpers ─────────────────────────

def read_json(path: Path, default=None):
    try:
        with open(path, "rb") as fh:
            data = fh.read(4_000_000)
        return json.loads(data)
    except (OSError, ValueError, RecursionError):
        return default


def atomic_write(path: Path, text: str, mode: int = 0o640) -> None:
    """Write-then-rename (unique scratch name, O_EXCL, fsync): readers never see half a file."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def append_line(path: Path, obj: dict, mode: int = 0o640) -> None:
    """One JSON line in one O_APPEND write: atomic against other appenders (portal, sensord)."""
    line = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def read_events(data_dir: Path, limit_bytes: int = 600_000) -> list[dict]:
    """events.jsonl.1 then events.jsonl, oldest first; junk lines skipped."""
    out = []
    for name in ("events.jsonl.1", "events.jsonl"):
        try:
            with open(Path(data_dir) / name, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - limit_bytes))
                raw = fh.read(limit_bytes)
        except OSError:
            continue
        for line in raw.split(b"\n"):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and isinstance(ev.get("seq"), int):
                out.append(ev)
    return out


# ─────────────────────────────── reading store ──────────────────────────────

class Store:
    """Readings in SQLite (WAL: the portal reads while sensord writes; a power cut loses at most
    the last transaction, never the file). Every reading lands in `raw` and is folded into the
    10-minute and hourly buckets as it arrives, so downsampling needs no batch job; `prune()`
    only deletes what has aged out. Disk use is bounded by the retention windows, the series
    cap and a hard size cap (`max_mb`)."""

    RAW_KEEP = 48 * 3600
    A10_KEEP = 30 * 86400
    A60_KEEP = 366 * 86400

    def __init__(self, path: Path, max_series: int = 128, max_mb: float = 64, readonly: bool = False):
        self.path = Path(path)
        self.max_series = max_series
        self.max_mb = max_mb
        self.readonly = readonly
        if readonly:
            # The portal's side: an ordinary connection (a "mode=ro" one can't open a WAL file whose
            # -shm is gone after sensord stopped) that refuses writes, and never creates the file.
            if not self.path.exists():
                raise FileNotFoundError(str(self.path))
            self.db = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
            self.db.execute("PRAGMA query_only=ON")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not self.path.exists()
            self.db = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
            if fresh:
                with contextlib.suppress(OSError):
                    os.chmod(self.path, 0o640)
                self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS series(id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL,
                    sensor TEXT NOT NULL, metric TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
                    remote INTEGER NOT NULL DEFAULT 0, last_t INTEGER, last_v REAL);
                CREATE TABLE IF NOT EXISTS raw(series INTEGER, t INTEGER, v REAL,
                    PRIMARY KEY(series, t)) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS agg10(series INTEGER, t INTEGER, avg REAL, mn REAL, mx REAL, n INTEGER,
                    PRIMARY KEY(series, t)) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS agg60(series INTEGER, t INTEGER, avg REAL, mn REAL, mx REAL, n INTEGER,
                    PRIMARY KEY(series, t)) WITHOUT ROWID;
            """)
        self.db.execute("PRAGMA busy_timeout=5000")
        self._ids: dict[str, int] = {}

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.db.close()

    def _series_id(self, key: str, sensor: str, metric: str, label: str, remote: bool) -> int | None:
        sid = self._ids.get(key)
        if sid is not None:
            return sid
        row = self.db.execute("SELECT id FROM series WHERE key=?", (key,)).fetchone()
        if row:
            self._ids[key] = row[0]
            return row[0]
        if self.db.execute("SELECT count(*) FROM series").fetchone()[0] >= self.max_series:
            return None                                   # full: junk keys can't grow the file for ever
        cur = self.db.execute("INSERT INTO series(key, sensor, metric, label, remote) VALUES(?,?,?,?,?)",
                              (key, sensor, metric, label[:60], 1 if remote else 0))
        self._ids[key] = cur.lastrowid
        return cur.lastrowid

    def add_many(self, items: list[dict]) -> int:
        """items: {key, sensor, metric, t, v, label?, remote?}. Returns how many were stored."""
        n = 0
        with self.db:
            for it in items:
                key, metric = it["key"], it["metric"]
                v = in_range(metric, it.get("v"))
                if v is None or not KEY_RE.match(key):
                    continue
                sid = self._series_id(key, it["sensor"], metric, str(it.get("label") or ""), bool(it.get("remote")))
                if sid is None:
                    continue
                t = int(it["t"])
                cur = self.db.execute("INSERT OR IGNORE INTO raw(series, t, v) VALUES(?,?,?)", (sid, t, v))
                if cur.rowcount != 1:
                    continue                              # same second twice: keep the first, don't double-count
                for table, width in (("agg10", 600), ("agg60", 3600)):
                    self.db.execute(
                        f"INSERT INTO {table}(series, t, avg, mn, mx, n) VALUES(?,?,?,?,?,1) "
                        "ON CONFLICT(series, t) DO UPDATE SET avg=(avg*n+excluded.avg)/(n+1), "
                        "mn=min(mn, excluded.mn), mx=max(mx, excluded.mx), n=n+1",
                        (sid, t - t % width, v, v, v))
                self.db.execute("UPDATE series SET last_t=?, last_v=?, label=? WHERE id=? AND (last_t IS NULL OR last_t<=?)",
                                (t, v, str(it.get("label") or "")[:60], sid, t))
                n += 1
        return n

    def prune(self, now: float) -> None:
        now = int(now)
        with self.db:
            self.db.execute("DELETE FROM raw WHERE t<?", (now - self.RAW_KEEP,))
            self.db.execute("DELETE FROM agg10 WHERE t<?", (now - self.A10_KEEP,))
            self.db.execute("DELETE FROM agg60 WHERE t<?", (now - self.A60_KEEP,))
            # readings from a clock that was far in the future would never age out
            for table in ("raw", "agg10", "agg60"):
                self.db.execute(f"DELETE FROM {table} WHERE t>?", (now + 86400,))
            self.db.execute("DELETE FROM series WHERE id NOT IN (SELECT DISTINCT series FROM agg60)")
        self._ids.clear()
        if self.size_mb() > self.max_mb:                  # hard cap: drop the oldest half of everything
            with self.db:
                for table in ("raw", "agg10", "agg60"):
                    row = self.db.execute(f"SELECT min(t), max(t) FROM {table}").fetchone()
                    if row and row[0] is not None:
                        self.db.execute(f"DELETE FROM {table} WHERE t<?", ((row[0] + row[1]) // 2,))
        with contextlib.suppress(sqlite3.Error):
            self.db.execute("PRAGMA incremental_vacuum")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def size_mb(self) -> float:
        total = 0
        for suffix in ("", "-wal"):
            with contextlib.suppress(OSError):
                total += os.path.getsize(str(self.path) + suffix)
        return total / 1e6

    def series(self) -> list[dict]:
        rows = self.db.execute("SELECT key, sensor, metric, label, remote, last_t, last_v FROM series ORDER BY key")
        return [{"key": r[0], "sensor": r[1], "metric": r[2], "label": r[3], "remote": bool(r[4]),
                 "last_t": r[5], "last_v": r[6]} for r in rows]

    def history(self, key: str, span_s: int, now: float, points: int = 300) -> list[list]:
        """[[t, avg, min, max], …] for the last span_s seconds, at most ~`points` buckets."""
        row = self.db.execute("SELECT id FROM series WHERE key=?", (key,)).fetchone()
        if not row:
            return []
        sid, since = row[0], int(now) - int(span_s)
        if span_s <= self.RAW_KEEP:
            width = max(1, span_s // points)
            q = ("SELECT (t/?)*? AS b, avg(v), min(v), max(v) FROM raw WHERE series=? AND t>=? "
                 "GROUP BY b ORDER BY b")
        else:
            table, native = ("agg10", 600) if span_s <= self.A10_KEEP else ("agg60", 3600)
            width = max(native, span_s // points)
            q = (f"SELECT (t/?)*? AS b, sum(avg*n)/sum(n), min(mn), max(mx) FROM {table} WHERE series=? AND t>=? "
                 "GROUP BY b ORDER BY b")
        return [[r[0], r[1], r[2], r[3]] for r in self.db.execute(q, (width, width, sid, since))]

    def median(self, key: str, since: float, min_buckets: int = 36) -> float | None:
        """Median of the 10-minute averages since `since` (the "normal background" for a
        radiation alert); None until there are at least `min_buckets` of them."""
        row = self.db.execute("SELECT id FROM series WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        vals = sorted(r[0] for r in self.db.execute("SELECT avg FROM agg10 WHERE series=? AND t>=?", (row[0], int(since))))
        if len(vals) < min_buckets:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


# ─────────────────────────────── alert rules ────────────────────────────────

DEFAULT_RULES = [
    {"id": "flood", "sensor": "*", "metric": "flood", "above": 0.5, "hysteresis": 0, "confirm": 2,
     "cooldown_min": 30, "board": "community", "mesh": True, "level": "danger", "note": "water detected"},
    {"id": "freeze", "sensor": "*", "metric": "temperature", "below": 0.0, "hysteresis": 1.0, "confirm": 2,
     "cooldown_min": 360, "board": "community", "mesh": False, "level": "warning", "note": "freezing"},
    {"id": "heat", "sensor": "*", "metric": "temperature", "above": 40.0, "hysteresis": 2.0, "confirm": 2,
     "cooldown_min": 360, "board": "community", "mesh": False, "level": "warning", "note": "dangerous heat"},
    {"id": "radiation", "sensor": "*", "metric": "radiation", "above": 1.0, "above_background": 5.0,
     "hysteresis": 0.1, "confirm": 3, "cooldown_min": 60, "board": "community", "mesh": True,
     "level": "danger", "note": "radiation above normal"},
]
MAX_RULES = 40


def _num(d: dict, k: str, lo: float, hi: float, default=None):
    v = d.get(k, default)
    if v is None or v == "":
        return None
    v = finite(v) if not isinstance(v, str) else finite(_float_or_none(v))
    if v is None or not (lo <= v <= hi):
        raise ValueError(f"{k} must be a number between {lo:g} and {hi:g}")
    return v


def _float_or_none(s: str):
    try:
        return float(s)
    except ValueError:
        return None


def validate_rule(d, idx: int = 0) -> dict:
    """A rule from the config file or the admin page → normalized dict, or ValueError (friendly)."""
    if not isinstance(d, dict):
        raise ValueError("a rule must be a table/object")
    rid = str(d.get("id") or f"rule{idx + 1}").lower()
    if not ID_RE.match(rid):
        raise ValueError("rule id: letters, digits, - and _ only (max 24)")
    sensor = str(d.get("sensor", "*")).lower()
    if sensor != "*" and not ID_RE.match(sensor):
        raise ValueError(f"rule {rid}: unknown sensor name")
    metric = str(d.get("metric", ""))
    if metric not in METRICS:
        raise ValueError(f"rule {rid}: unknown reading type {metric[:30]!r}")
    m = METRICS[metric]
    span = m["max"] - m["min"]
    r = {"id": rid, "enabled": d.get("enabled", True) is not False, "sensor": sensor, "metric": metric,
         "above": _num(d, "above", m["min"], m["max"]), "below": _num(d, "below", m["min"], m["max"]),
         "above_background": _num(d, "above_background", 1.5, 1000),
         "hysteresis": _num(d, "hysteresis", 0, span, 0) or 0.0,
         "confirm": int(_num(d, "confirm", 1, 10, 1) or 1),
         "cooldown_min": int(_num(d, "cooldown_min", 0, 10080, 60) or 0),
         "board": str(d.get("board", "community")), "mesh": d.get("mesh", False) is True,
         "level": str(d.get("level", "warning")), "note": clean_text(d.get("note", ""), 60)}
    if r["board"] not in BOARDS:
        raise ValueError(f"rule {rid}: board must be community, family or none")
    if r["level"] not in LEVELS:
        raise ValueError(f"rule {rid}: level must be warning or danger")
    if r["above"] is None and r["below"] is None and r["above_background"] is None:
        raise ValueError(f"rule {rid}: give an 'above' or 'below' level")
    if r["above"] is not None and r["below"] is not None and r["below"] >= r["above"]:
        raise ValueError(f"rule {rid}: 'below' must be lower than 'above'")
    return r


def validate_rules(items) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("rules must be a list")
    if len(items) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} alert rules")
    out, seen = [], set()
    for i, d in enumerate(items):
        r = validate_rule(d, i)
        if r["id"] in seen:
            raise ValueError(f"two rules are called {r['id']}")
        seen.add(r["id"])
        out.append(r)
    return out


SETTINGS_KEYS = ("rules", "remote_nodes", "remote_untrusted", "mesh_alerts", "mesh_board",
                 "summary_every_h", "summary_board", "names")


def validate_settings(d) -> dict:
    """The admin page's settings → normalized dict (only the keys given), or ValueError."""
    if not isinstance(d, dict):
        raise ValueError("settings must be an object")
    out: dict = {}
    if "rules" in d:
        out["rules"] = None if d["rules"] is None else validate_rules(d["rules"])
    if "remote_nodes" in d:
        nodes = d["remote_nodes"]
        if not isinstance(nodes, list) or len(nodes) > 32:
            raise ValueError("remote_nodes: a list of at most 32 node ids")
        out["remote_nodes"] = []
        for n in nodes:
            n = str(n).lower()
            if not NODE_RE.match(n):
                raise ValueError("remote node ids look like !abcd1234")
            if n not in out["remote_nodes"]:
                out["remote_nodes"].append(n)
    for k in ("remote_untrusted", "mesh_alerts"):
        if k in d:
            if not isinstance(d[k], bool):
                raise ValueError(f"{k} must be true or false")
            out[k] = d[k]
    for k in ("mesh_board", "summary_board"):
        if k in d:
            if d[k] not in ("community", "family"):
                raise ValueError(f"{k} must be community or family")
            out[k] = d[k]
    if "summary_every_h" in d:
        v = d["summary_every_h"]
        if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= 24):
            raise ValueError("summary_every_h: 0 (off) to 24 hours")
        out["summary_every_h"] = v
    if "names" in d:
        names = d["names"]
        if not isinstance(names, dict) or len(names) > 64:
            raise ValueError("names: at most 64 sensors")
        out["names"] = {}
        for k, v in names.items():
            k = str(k).lower()
            if not ID_RE.match(k):
                raise ValueError("names: unknown sensor id")
            v = clean_text(v, 32)
            if v:
                out["names"][k] = v
    return out


def effective_settings(cfg: dict, saved: dict | None) -> dict:
    """[sensors] config values, overridden by what the admin saved in the portal."""
    cfg = cfg or {}
    # Over the radio, sensor alerts and summaries go on the FAMILY channel unless the owner chooses the
    # public one: "water in the basement" on the public channel describes the house to anyone listening.
    mesh_board = cfg.get("mesh_board", "family")
    eff = {"rules": None, "remote_nodes": [], "remote_untrusted": cfg.get("remote_untrusted", False) is True,
           "mesh_alerts": cfg.get("mesh_alerts", True) is not False,
           "mesh_board": mesh_board if mesh_board in ("community", "family") else "family",
           "summary_every_h": 0, "summary_board": "family", "names": {}}
    try:
        eff["summary_every_h"] = max(0, min(24, int(cfg.get("summary_every_h", 0))))
    except (TypeError, ValueError):
        pass
    if cfg.get("summary_board") in ("community", "family"):
        eff["summary_board"] = cfg["summary_board"]
    nodes = cfg.get("remote_nodes", [])
    if isinstance(nodes, list):
        eff["remote_nodes"] = [str(n).lower() for n in nodes if NODE_RE.match(str(n).lower())][:32]
    if isinstance(saved, dict):
        try:
            eff.update(validate_settings({k: saved[k] for k in SETTINGS_KEYS if k in saved}))
        except ValueError:
            pass                                          # a hand-edited, broken file: fall back to the config
    return eff


def config_rules(cfg: dict, devices: list[dict] | None = None) -> list[dict]:
    """Rules from the config: [[sensors.alert]] tables if present (else the built-in defaults),
    plus the per-device shortcuts alert_above / alert_below."""
    rules: list[dict] = []
    table = (cfg or {}).get("alert")
    if isinstance(table, list) and table:
        for i, d in enumerate(table[:MAX_RULES]):
            try:
                rules.append(validate_rule(d, i))
            except ValueError:
                continue
    else:
        rules = [validate_rule(d) for d in DEFAULT_RULES]
    for dev in devices or []:
        for side in ("above", "below"):
            v = dev.get(f"alert_{side}")
            if v is None or not dev.get("metric"):
                continue
            with contextlib.suppress(ValueError):
                rules.append(validate_rule({"id": f"{dev['id']}-{side}"[:24], "sensor": dev["id"],
                                            "metric": dev["metric"], side: v,
                                            "hysteresis": dev.get("alert_hysteresis", 0),
                                            "confirm": 2, "cooldown_min": 60, "board": dev.get("board", "community"),
                                            "mesh": dev.get("mesh", True), "level": "danger"}))
    return rules[:MAX_RULES]


def describe_rule(r: dict) -> str:
    parts = []
    if r.get("above") is not None:
        parts.append("above " + fmt_value(r["metric"], r["above"]) if r["metric"] != "flood" else "wet")
    if r.get("above_background") is not None:
        parts.append(f"{r['above_background']:g}× normal")
    if r.get("below") is not None:
        parts.append("below " + fmt_value(r["metric"], r["below"]))
    return " or ".join(parts)


class Engine:
    """Threshold alerts with hysteresis, confirmation and cooldown.

    For each (rule, series): a reading past the level `confirm` times in a row raises the alert;
    it clears only once the reading is back past the level by `hysteresis` (also `confirm` times),
    so a value hovering at the line doesn't flap. A raised alert is announced (an event: board +
    mesh) unless the same alert was announced less than `cooldown_min` ago; it still shows in
    the active list. A clear is announced only if the raise was. State is a plain dict, saved by
    sensord across restarts, so a restart doesn't re-announce an alert that is still on."""

    def __init__(self, state: dict | None = None):
        st = state if isinstance(state, dict) else {}
        self.active: dict[str, dict] = st.get("active") if isinstance(st.get("active"), dict) else {}
        self.last_fired: dict[str, float] = st.get("last_fired") if isinstance(st.get("last_fired"), dict) else {}
        self.pending: dict[str, int] = st.get("pending") if isinstance(st.get("pending"), dict) else {}

    def state(self) -> dict:
        return {"active": self.active, "last_fired": self.last_fired, "pending": self.pending}

    @staticmethod
    def matches(rule: dict, rd: dict) -> bool:
        if rule["metric"] != rd["metric"] or not rule["enabled"]:
            return False
        if rule["sensor"] == "*":
            return not rd.get("remote") and rd.get("alerts", True)
        return rule["sensor"] == rd["sensor"]

    @staticmethod
    def levels(rule: dict, background: float | None) -> tuple[float | None, float | None]:
        hi = rule.get("above")
        if rule.get("above_background") is not None and background is not None and background > 0:
            rel = rule["above_background"] * background
            hi = rel if hi is None else min(hi, rel)
        return hi, rule.get("below")

    def evaluate(self, rules: list[dict], readings: list[dict], now: float, background=None) -> list[dict]:
        """readings: {key, sensor, metric, value, label, remote?, alerts?}; background(key) → float|None."""
        events = []
        live = {r["id"] for r in rules if r["enabled"]}
        for sk in [k for k, a in self.active.items() if a.get("rule") not in live]:
            del self.active[sk]                           # rule removed or switched off: drop silently
        for sk in [k for k in self.pending if k.split("|", 1)[0] not in live]:
            del self.pending[sk]
        for rule in rules:
            for rd in readings:
                if not self.matches(rule, rd):
                    continue
                v = finite(rd.get("value"))
                if v is None:
                    continue
                sk = f"{rule['id']}|{rd['key']}"
                bg = background(rd["key"]) if (background and rule.get("above_background") is not None) else None
                hi, lo = self.levels(rule, bg)
                h = rule["hysteresis"]
                st = self.active.get(sk)
                if st is None:
                    side = "above" if hi is not None and v > hi else "below" if lo is not None and v < lo else None
                    if side is None:
                        self.pending.pop(sk, None)
                        continue
                    n = self.pending.get(sk, 0) + 1
                    if n < rule["confirm"]:
                        self.pending[sk] = n
                        continue
                    self.pending.pop(sk, None)
                    announce = sk not in self.last_fired or now - float(self.last_fired[sk]) >= rule["cooldown_min"] * 60
                    level_v = hi if side == "above" else lo
                    st = {"rule": rule["id"], "key": rd["key"], "sensor": rd["sensor"], "metric": rd["metric"],
                          "side": side, "threshold": level_v, "background": bg, "since": now, "at": now,
                          "value": v, "level": rule["level"], "label": rd.get("label") or rd["sensor"],
                          "remote": bool(rd.get("remote")), "announced": announce}
                    st["text"] = alert_text(st, rule)
                    self.active[sk] = st
                    if announce:
                        self.last_fired[sk] = now
                        events.append(self._event("alert", st, rule, now))
                else:
                    st["value"], st["at"] = v, now
                    st["text"] = alert_text(st, rule)
                    thr = st["threshold"]
                    if st["side"] == "above":
                        thr = hi if hi is not None else thr
                        back = v < thr - h if rule["metric"] != "flood" else v < 0.5
                    else:
                        thr = lo if lo is not None else thr
                        back = v > thr + h
                    if not back:
                        self.pending.pop(sk, None)
                        continue
                    n = self.pending.get(sk, 0) + 1
                    if n < rule["confirm"]:
                        self.pending[sk] = n
                        continue
                    self.pending.pop(sk, None)
                    del self.active[sk]
                    if st.get("announced"):
                        events.append(self._event("clear", st, rule, now))
        # bounded: a flood of junk series can't grow the state file for ever
        if len(self.last_fired) > 500:
            for k in sorted(self.last_fired, key=self.last_fired.get)[:len(self.last_fired) - 500]:
                del self.last_fired[k]
        return events

    @staticmethod
    def _event(kind: str, st: dict, rule: dict, now: float) -> dict:
        text = st["text"] if kind == "alert" else clear_text(st)
        return {"kind": kind, "at": now, "rule": rule["id"], "key": st["key"], "sensor": st["sensor"],
                "metric": st["metric"], "value": st["value"], "level": st["level"], "text": text,
                "board": rule["board"], "mesh": rule["mesh"], "remote": st.get("remote", False)}

    def active_list(self, now: float, stale_s: float = 900) -> list[dict]:
        out = []
        for a in sorted(self.active.values(), key=lambda a: (a.get("level") != "danger", a.get("since", 0))):
            out.append({k: a.get(k) for k in ("rule", "key", "sensor", "metric", "value", "level", "text",
                                              "since", "at", "remote")} | {"stale": now - a.get("at", now) > stale_s})
        return out


def alert_text(st: dict, rule: dict) -> str:
    label, metric = st["label"], st["metric"]
    if st.get("remote"):
        label += " (mesh radio)"
    if metric == "flood":
        return f"{label}: water detected — the flood switch is wet"
    m = METRICS[metric]
    val, thr = fmt_value(metric, st["value"]), fmt_value(metric, st["threshold"])
    if st["side"] == "above":
        extra = ""
        if st.get("background") and rule.get("above_background") is not None:
            extra = f", normal here is {fmt_value(metric, st['background'])}"
        msg = f"{label}: {m['label'].lower()} {val}, above the alert level of {thr}{extra}"
    else:
        msg = f"{label}: {m['label'].lower()} {val}, below the alert level of {thr}"
    return msg + (f" ({rule['note']})" if rule.get("note") else "")


def clear_text(st: dict) -> str:
    label = st["label"] + (" (mesh radio)" if st.get("remote") else "")
    if st["metric"] == "flood":
        return f"{label}: dry again — the flood switch is back to normal"
    return f"{label}: back to normal ({METRICS[st['metric']]['label'].lower()} {fmt_value(st['metric'], st['value'])})"


# ─────────────────────────────── Meshtastic telemetry ───────────────────────

# Meshtastic field (camelCase or snake_case, compared without "_" and case) → (metric, scale)
TELEMETRY_KEYS = {
    "temperature": ("temperature", 1), "relativehumidity": ("humidity", 1),
    "barometricpressure": ("pressure", 1), "voltage": ("voltage", 1), "batterylevel": ("battery", 1),
    "lux": ("lux", 1), "iaq": ("iaq", 1), "co2": ("co2", 1), "pm25standard": ("pm25", 1),
    "pm10standard": ("pm10", 1), "windspeed": ("wind_speed", 1), "rainfall1h": ("rain_1h", 1),
    "soilmoisture": ("soil_moisture", 1), "soiltemperature": ("soil_temperature", 1),
    "radiation": ("radiation", 0.01),                  # Meshtastic reports µR/h; 100 µR/h ≈ 1 µSv/h
}
TELEMETRY_KINDS = ("environment", "device", "power", "air_quality", "other")
MAX_LINE = 4096


def parse_telemetry(line: bytes | str, now: float) -> dict | None:
    """One line of mesh-telemetry.jsonl → {at, node, name, channel, trusted, kind, values{metric: v}}
    or None. Everything in it came over the air from anyone with the channel key (or anyone at
    all on a public channel): types, ranges, sizes and the timestamp are all checked."""
    if isinstance(line, str):
        line = line.encode("utf-8", "replace")
    if not line or len(line) > MAX_LINE:
        return None
    try:
        d = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(d, dict):
        return None
    node = d.get("from")
    if not isinstance(node, str) or not NODE_RE.match(node.lower()):
        return None
    at = finite(d.get("at"))
    if at is None or at > now + 300 or at < now - 7 * 86400:
        return None
    kind = d.get("kind") if d.get("kind") in TELEMETRY_KINDS else "other"
    vals = d.get("values")
    if not isinstance(vals, dict) or len(vals) > 64:
        return None
    out = {}
    for k, v in vals.items():
        if not isinstance(k, str) or len(k) > 40:
            continue
        spec = TELEMETRY_KEYS.get(k.replace("_", "").lower())
        if not spec:
            continue
        v = finite(v)
        if v is None:
            continue
        v = in_range(spec[0], v * spec[1])
        if v is not None:
            out[spec[0]] = round(v, 4)
    if not out:
        return None
    name = d.get("name")
    name = clean_text(name, 32) if isinstance(name, str) else ""
    ch = d.get("channel")
    ch = ch if isinstance(ch, int) and not isinstance(ch, bool) and 0 <= ch <= 7 else None
    return {"at": at, "node": node.lower(), "name": name, "channel": ch, "trusted": d.get("trusted") is True,
            "kind": kind, "values": out}


class TelemetryTail:
    """Reads only the lines appended to mesh-telemetry.jsonl since last time (offset kept by the
    caller). A truncated/rotated file starts over; a partial last line waits for the next round;
    each round reads at most `budget` bytes so a huge file can't stall the daemon."""

    def __init__(self, path: Path, offset: int | None = None, budget: int = 256_000):
        self.path = Path(path)
        self.offset = offset
        self.budget = budget

    def read(self, now: float) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if self.offset is None:
            self.offset = max(0, size - 64_000)          # first run: just the recent tail
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return []
        out = []
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(self.budget)
        pos = 0
        while True:
            nl = chunk.find(b"\n", pos)
            if nl < 0:
                if len(chunk) - pos >= MAX_LINE:          # an endless line: skip what we have
                    pos = len(chunk)
                break
            rec = parse_telemetry(chunk[pos:nl], now)
            if rec:
                out.append(rec)
            pos = nl + 1
        self.offset += pos
        return out
