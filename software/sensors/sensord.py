#!/usr/bin/env python3
"""
sensord — the Honey Badger's sensor station.

Polls the sensors in [sensors] (plus any it can find safely by itself), stores the readings
(sensorlib.Store), raises threshold alerts and writes them where people will see them:

  * mesh radio: sensord appends ≤200-byte lines to the mesh outbox itself ({"text","board","kind","at"},
    one O_APPEND write each), so a flood alert still goes out over LoRa when the portal is
    down or busy — meshbridge reads the outbox on its own. Rate-limited (min gap + hourly cap).
    Tagged for the mesh courtesy (docs/MESH.md): a danger alert is kind "danger" (right after NOAA
    alerts), the periodic summary "auto" (lowest priority, local hop limit, deferred on a busy
    channel), other alerts/clears the board's kind. No dedupe key: local readings are unique.
    By default they go on the FAMILY channel ([sensors] mesh_board = "family"; "community" is the
    owner's explicit choice) and start "Sensors: " — over the air nothing names the device.
  * boards: every alert/clear goes into events.jsonl; the portal's sensors feature posts them
    to the community or family board (ctx.board_post) and catches up on anything written
    while it was down. The board file format stays the portal's business.
  * active alerts: current.json (the launcher banner and the sensors page read it).

Remote readings: Meshtastic environment telemetry that meshbridge appends to
mesh-telemetry.jsonl (in RAM: /run/prepper, or [portal] run_dir / mesh_telemetry). Untrusted radio data — validated, range-checked, rate-limited, and only
kept for nodes the admin picked.

Run: python3 sensord.py [--once] (config from $PREPPER_CONFIG or /etc/prepper/prepper.toml).
"""
from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import drivers  # noqa: E402
import sensorlib as sl  # noqa: E402

DATA = Path("/var/lib/prepper/portal/features/sensors")
OUTBOX = Path("/var/lib/prepper/portal/mesh-outbox.jsonl")
RUN = Path("/run/prepper")                     # the mesh bridge keeps radio state in RAM ([portal] run_dir)
TELEMETRY = RUN / "mesh-telemetry.jsonl"
W1_ROOT = Path("/sys/bus/w1/devices")

TYPES = {   # type → (primary metric, default poll seconds)
    "bme280": ("temperature", None), "bmp280": ("temperature", None), "sht3x": ("temperature", None),
    "sht4x": ("temperature", None), "ds18b20": ("temperature", None), "ads1115": ("water_level", None),
    "jsn_sr04t": ("water_level", None), "float": ("flood", 5), "gmc": ("radiation", 60),
    "geiger_text": ("radiation", 60),
}
TYPE_NAMES = {"bme280": "Weather sensor", "bmp280": "Pressure sensor", "sht3x": "Temperature & humidity",
              "sht4x": "Temperature & humidity", "ds18b20": "Temperature probe", "ads1115": "Water level",
              "jsn_sr04t": "Water level", "float": "Flood switch", "gmc": "Geiger counter",
              "geiger_text": "Geiger counter"}
I2C_TYPES = ("bme280", "bmp280", "sht3x", "sht4x", "ads1115")
SERIAL_TYPES = ("jsn_sr04t", "gmc", "geiger_text")
MAX_DEVICES = 24
MAX_REMOTE_NODES = 32
REMOTE_MIN_GAP = 60          # seconds between stored readings from one remote node
EVENTS_MAX = 512_000


def log(msg: str) -> None:
    print(f"sensord: {msg}", file=sys.stderr, flush=True)


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _float(v, default):
    f = sl.finite(v)
    return default if f is None else f


def device_specs(sc: dict) -> tuple[list[dict], list[str]]:
    """[[sensors.device]] → normalized specs; bad entries are reported, not fatal."""
    specs, errors, seen = [], [], set()
    table = sc.get("device", [])
    if not isinstance(table, list):
        return [], ["[[sensors.device]] must be a list of tables"]
    for i, d in enumerate(table[:MAX_DEVICES]):
        if not isinstance(d, dict):
            errors.append(f"device #{i + 1}: not a table")
            continue
        typ = str(d.get("type", "")).lower()
        did = str(d.get("id") or f"{typ}{i + 1}").lower()
        if typ not in TYPES:
            errors.append(f"device {did}: unknown type {typ[:20]!r}")
            continue
        if not sl.ID_RE.match(did) or did.startswith("mesh-") or did in seen:
            errors.append(f"device {did[:30]}: id must be unique, letters/digits/-/_ (max 24), not mesh-…")
            continue
        seen.add(did)
        metric, poll = TYPES[typ]
        spec = dict(d)
        spec.update(id=did, type=typ, metric=metric,
                    name=sl.clean_text(d.get("name") or TYPE_NAMES[typ], 32),
                    poll_s=_int(d.get("poll_s"), poll or _int(sc.get("poll_s"), 30, 5, 3600), 2, 3600),
                    alerts=d.get("alerts", True) is not False)
        if typ in SERIAL_TYPES and not str(d.get("port", "")).startswith("/dev/"):
            errors.append(f"device {did}: set port = \"/dev/serial/by-id/…\"")
            continue
        if typ == "float" and not isinstance(d.get("gpio"), int):
            errors.append(f"device {did}: set gpio = <BCM pin number>")
            continue
        specs.append(spec)
    return specs, errors


def friendly_error(e: BaseException, spec: dict) -> str:
    where = spec.get("port") or spec.get("serial") or ""
    if isinstance(e, FileNotFoundError):
        return f"not found — is it plugged in? ({where or e.filename or 'device'})"
    if isinstance(e, PermissionError):
        return "no permission to open it — is user prepper in groups i2c, dialout and gpio?"
    if isinstance(e, OSError) and e.errno == 121:
        return "no answer on the I²C bus (check the four wires and the address)"
    if isinstance(e, OSError) and e.errno == 16:
        return "busy — a kernel driver is using this address"
    return str(e) or type(e).__name__


class Device:
    """One sensor: builds its driver lazily, reopens it after errors, backs off when it keeps failing."""

    def __init__(self, spec: dict, factory, auto: bool = False):
        self.spec, self.factory, self.auto = spec, factory, auto
        self.id, self.type, self.metric = spec["id"], spec["type"], spec["metric"]
        self.drv = None
        self.ok, self.error, self.values, self.at = False, "", {}, 0.0
        self.fails, self.next_due = 0, 0.0

    def close(self) -> None:
        dev = getattr(self.drv, "dev", None)
        if dev is not None and hasattr(dev, "close"):
            dev.close()
        self.drv = None

    def poll(self, mono: float, now: float) -> dict | None:
        if mono < self.next_due:
            return None
        try:
            if self.drv is None:
                self.drv = self.factory(self.spec)
            vals = {k: v for k, v in self.drv.read().items() if sl.in_range(k, v) is not None}
            if not vals:
                raise ValueError("reading out of range")
        except Exception as e:  # noqa: BLE001 — a driver bug must not stop the other sensors
            self.close()
            self.fails += 1
            msg = sl.clean_text(friendly_error(e, self.spec), 160)
            if self.ok or msg != self.error:
                log(f"{self.id}: {msg}")
            self.ok, self.error = False, msg
            # back off: 1×, 2×, 4× … the poll interval, at most 5 minutes
            self.next_due = mono + min(300, self.spec["poll_s"] * (2 ** min(self.fails - 1, 6)))
            return None
        if not self.ok:
            log(f"{self.id}: reading ({', '.join(f'{k}={v}' for k, v in vals.items())})")
        self.ok, self.error, self.values, self.at, self.fails = True, "", vals, now, 0
        self.next_due = mono + self.spec["poll_s"]
        return vals


def mesh_kind(batch: list[dict], board: str) -> str:
    """The mesh-courtesy priority of one coalesced line: "danger" if it carries a danger alert,
    "auto" if it is only the periodic summary, else the board's own kind (family / community)."""
    if any(m.get("urgent") for m in batch):
        return "danger"
    if batch and all(m.get("summary") for m in batch):
        return "auto"
    return "family" if board == "family" else "community"


class Sensord:
    def __init__(self, raw: dict, *, data_dir: Path | None = None, outbox: Path | None = None,
                 telemetry: Path | None = None, i2c_open=drivers.I2CDev, serial_open=drivers.Serial,
                 gpio_runner=drivers.run_cmd, w1_root: Path = W1_ROOT, sleep=time.sleep, mono=time.monotonic):
        self.raw = raw or {}
        sc = self.raw.get("sensors", {}) or {}
        portal = self.raw.get("portal", {}) or {}
        self.sc = sc
        self.data = Path(data_dir or Path(portal.get("features_data") or DATA.parent) / "sensors")
        self.data.mkdir(parents=True, exist_ok=True)
        self.outbox = Path(outbox or sc.get("mesh_outbox") or portal.get("mesh_outbox") or OUTBOX)
        # the same file the mesh bridge writes: [sensors] telemetry_file, else [portal] mesh_telemetry (or
        # $MESH_TELEMETRY), else <[portal] run_dir>/mesh-telemetry.jsonl (/run/prepper: RAM)
        self.telemetry_path = Path(telemetry or sc.get("telemetry_file") or os.environ.get("MESH_TELEMETRY")
                                   or portal.get("mesh_telemetry")
                                   or Path(portal.get("run_dir") or RUN) / TELEMETRY.name)
        self.i2c_open, self.serial_open, self.gpio_runner = i2c_open, serial_open, gpio_runner
        self.w1_root, self.sleep, self.mono = Path(w1_root), sleep, mono
        self.bus = _int(sc.get("i2c_bus", (self.raw.get("power") or {}).get("i2c_bus", 1)), 1, 0, 20)
        self.poll_s = _int(sc.get("poll_s"), 30, 5, 3600)
        self.auto = sc.get("auto_detect", True) is not False
        self.mesh_gap = _int(sc.get("mesh_min_interval_s"), 600, 60, 86400)
        self.mesh_gap_danger = _int(sc.get("mesh_min_interval_danger_s"), 120, 30, 86400)
        self.mesh_per_hour = _int(sc.get("mesh_max_per_hour"), 4, 1, 12)
        self.store = sl.Store(self.data / "readings.db", max_mb=_float(sc.get("max_db_mb"), 64))
        st = sl.read_json(self.data / "sensord-state.json", {})
        st = st if isinstance(st, dict) else {}
        self.engine = sl.Engine(st.get("engine"))
        self.seq = st.get("seq", 0) if isinstance(st.get("seq"), int) else 0
        self.mesh_sent = [t for t in st.get("mesh_sent", []) if sl.finite(t) is not None][-20:]
        q = st.get("mesh_queue", [])
        self.mesh_queue = [m for m in q if isinstance(m, dict) and isinstance(m.get("text"), str)][:10] if isinstance(q, list) else []
        self.last_summary = _float(st.get("last_summary"), 0.0)
        off = st.get("telemetry_offset")
        self.tail = sl.TelemetryTail(self.telemetry_path, off if isinstance(off, int) and off >= 0 else None)
        rem = st.get("remote", {})
        self.remote: dict[str, dict] = rem if isinstance(rem, dict) else {}
        seen = st.get("seen", {})
        self.seen: dict[str, dict] = seen if isinstance(seen, dict) else {}
        self.specs, self.config_errors = device_specs(sc)
        for e in self.config_errors:
            log(f"config: {e}")
        self.devices: list[Device] = [Device(s, self.make_driver) for s in self.specs]
        self.settings_mtime = None
        self.saved_settings: dict = {}
        self.settings = sl.effective_settings(sc, None)
        self.rules: list[dict] = []
        self.load_settings(force=True)
        self.next_scan = 0.0
        self.next_prune = 0.0
        self.next_tele = 0.0
        self.next_write = 0.0
        self.last_state_write = 0.0
        self.last_current = -1e9
        self.dirty = True
        self._bg_cache: dict[str, tuple[float, float | None]] = {}

    # — config & settings —
    def load_settings(self, force: bool = False) -> None:
        p = self.data / "settings.json"
        try:
            mt = p.stat().st_mtime_ns
        except OSError:
            mt = None
        if not force and mt == self.settings_mtime:
            return
        self.settings_mtime = mt
        saved = sl.read_json(p, {}) if mt is not None else {}
        self.saved_settings = saved if isinstance(saved, dict) else {}
        self.settings = sl.effective_settings(self.sc, self.saved_settings)
        self.rules = self.settings["rules"] if self.settings["rules"] is not None else sl.config_rules(self.sc, self.specs)
        if not force:
            log("settings changed — reloaded")
        self.dirty = True

    def reserved_addresses(self) -> set[int]:
        power, panel = self.raw.get("power", {}) or {}, self.raw.get("panel", {}) or {}
        res = {0x36, 0x3C, _int(panel.get("i2c_address"), 0x3C, 0, 127)}
        if str(power.get("input_monitor", "none")) != "none":
            res.add(_int(power.get("input_i2c_address"), 0x40, 0, 127))
        if str(power.get("ups", "")) == "ina219":
            res.add(_int(power.get("ina219_address"), 0x42, 0, 127))
        for s in self.specs:
            if s["type"] in I2C_TYPES:
                res.add(_int(s.get("address"), -1, -1, 127))
        return res

    # — drivers —
    def make_driver(self, spec: dict):
        t = spec["type"]
        if t in I2C_TYPES:
            default = {"bme280": 0x76, "bmp280": 0x76, "sht3x": 0x44, "sht4x": 0x44, "ads1115": 0x48}[t]
            dev = self.i2c_open(_int(spec.get("bus"), self.bus, 0, 20), _int(spec.get("address"), default, 3, 0x77))
            try:
                if t in ("bme280", "bmp280"):
                    return drivers.BME280(dev, altitude_m=_float(spec.get("altitude_m"), 0.0), sleep=self.sleep)
                if t == "sht3x":
                    return drivers.SHT3x(dev, sleep=self.sleep)
                if t == "sht4x":
                    return drivers.SHT4x(dev, sleep=self.sleep)
                return drivers.ADS1115Level(dev, channel=_int(spec.get("channel"), 0, 0, 3),
                                            fsr=_float(spec.get("range_v"), 4.096), v_zero=_float(spec.get("v_zero"), 0.6),
                                            v_full=_float(spec.get("v_full"), 3.0),
                                            full_scale_cm=_float(spec.get("full_scale_cm"), 500.0),
                                            offset_cm=_float(spec.get("offset_cm"), 0.0),
                                            divider=_float(spec.get("divider"), 1.0), sleep=self.sleep)
            except BaseException:
                dev.close()
                raise
        if t == "ds18b20":
            serial = str(spec.get("serial", "")).lower()
            if serial:
                if not re.fullmatch(r"[0-9a-f]{2}-[0-9a-f]{12}", serial):
                    raise ValueError("serial looks like 28-0123456789ab")
                return drivers.DS18B20(self.w1_root / serial)
            probes = drivers.w1_probes(self.w1_root)
            if not probes:
                raise OSError("no 1-Wire probe found (is dtoverlay=w1-gpio on and the 4.7 kΩ pull-up fitted?)")
            return drivers.DS18B20(probes[0])
        if t == "float":
            reader = drivers.GpioReader(spec["gpio"], str(spec.get("gpio_cmd", "") or ""), runner=self.gpio_runner)
            return drivers.FloatSwitch(reader, str(spec.get("wet_when", "high")), sleep=self.sleep)
        port, baud = str(spec["port"]), _int(spec.get("baud"), 0, 0, 1_000_000)
        if t == "jsn_sr04t":
            return drivers.JSNSR04T(port, _float(spec.get("mount_height_cm"), 0.0), baud=baud or 9600,
                                    opener=self.serial_open, sleep=self.sleep)
        if t == "gmc":
            return drivers.GQGMC(port, baud=baud or 115200, factor=_float(spec.get("factor"), 0.0065),
                                 cpm_bytes=_int(spec.get("cpm_bytes"), 0, 0, 4), opener=self.serial_open)
        factor = sl.finite(spec.get("factor"))
        return drivers.TextGeiger(port, baud=baud or 9600, factor=factor if factor and factor > 0 else None,
                                  cpm_regex=str(spec.get("cpm_regex", "") or "")[:200], opener=self.serial_open)

    def autodetect(self) -> None:
        """Only probes that can't upset anything: a register read at the BME280 addresses, the
        SHT4x serial-number/SHT3x status commands at 0x44/0x45 (skipping addresses the power
        meter or display use), and the 1-Wire sysfs folder. Serial ports are never probed —
        the Meshtastic node and GPS live on them."""
        have = {d.id for d in self.devices}
        reserved = self.reserved_addresses()
        found = []
        bus_ok = os.path.exists(f"/dev/i2c-{self.bus}") if self.i2c_open is drivers.I2CDev else True
        for addr in (0x76, 0x77):
            if not bus_ok or addr in reserved or f"bme280-{addr:02x}" in have:
                continue
            try:
                dev = self.i2c_open(self.bus, addr)
            except OSError:
                continue
            try:
                drv = drivers.BME280(dev, sleep=self.sleep)
                found.append(({"id": f"bme280-{addr:02x}", "type": drv.kind, "address": addr}, drv))
            except (OSError, ValueError):
                dev.close()
        for addr in (0x44, 0x45):
            if not bus_ok or addr in reserved or f"sht-{addr:02x}" in have:
                continue
            for cls, typ in ((drivers.SHT4x, "sht4x"), (drivers.SHT3x, "sht3x")):
                try:
                    dev = self.i2c_open(self.bus, addr)
                except OSError:
                    break
                try:
                    found.append(({"id": f"sht-{addr:02x}", "type": typ, "address": addr}, cls(dev, sleep=self.sleep)))
                    break
                except (OSError, ValueError):
                    dev.close()
        configured_probes = {str(s.get("serial", "")).lower() for s in self.specs if s["type"] == "ds18b20"}
        anon = any(s["type"] == "ds18b20" and not s.get("serial") for s in self.specs)
        for i, p in enumerate(drivers.w1_probes(self.w1_root)):
            pid = f"probe-{p.name[-6:]}"
            if p.name in configured_probes or pid in have or (anon and i == 0):
                continue
            found.append(({"id": pid, "type": "ds18b20", "serial": p.name}, drivers.DS18B20(p)))
        for base, drv in found:
            if len(self.devices) >= MAX_DEVICES:
                break
            metric, poll = TYPES[base["type"]]
            spec = {**base, "metric": metric, "poll_s": poll or self.poll_s, "alerts": True,
                    "name": TYPE_NAMES[base["type"]] + (f" {base['serial'][-4:]}" if base["type"] == "ds18b20" else "")}
            d = Device(spec, self.make_driver, auto=True)
            d.drv = drv
            self.devices.append(d)
            log(f"found {base['type']} ({base['id']})")
            self.dirty = True

    # — one round —
    def name_of(self, sensor_id: str, fallback: str) -> str:
        return self.settings["names"].get(sensor_id) or fallback

    def step(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        mono = self.mono()
        self.load_settings()
        if self.auto and mono >= self.next_scan:
            self.next_scan = mono + 600
            self.autodetect()
        items, readings = [], []
        for d in self.devices:
            vals = d.poll(mono, now)
            if not vals:
                continue
            label = self.name_of(d.id, d.spec["name"])
            for metric, v in vals.items():
                key = f"{d.id}:{metric}"
                items.append({"key": key, "sensor": d.id, "metric": metric, "t": now, "v": v, "label": label})
                readings.append({"key": key, "sensor": d.id, "metric": metric, "value": v, "label": label,
                                 "alerts": d.spec.get("alerts", True)})
            self.dirty = True
        if mono >= self.next_tele:
            self.next_tele = mono + 10
            r_items, r_readings = self.ingest_telemetry(now)
            items += r_items
            readings += r_readings
        if items:
            try:
                self.store.add_many(items)
            except sqlite3.Error as e:
                log(f"cannot store readings: {e}")
        events = self.engine.evaluate(self.rules, readings, now, lambda k: self.background(k, now)) if readings else []
        for ev in events:
            self.emit(ev, now)
        self.maybe_summary(now)
        self.flush_mesh(now)
        if mono >= self.next_prune:
            self.next_prune = mono + 3600
            try:
                self.store.prune(now)
            except sqlite3.Error as e:
                log(f"cannot prune readings: {e}")
        # current.json doubles as the heartbeat (the portal calls the service stopped when it is
        # over 3 minutes old), so it is rewritten at least once a minute even when nothing changed
        if events or (self.dirty and mono >= self.next_write) or mono - self.last_current >= 60:
            self.next_write = mono + max(5, min(self.poll_s, 30))
            self.last_current = mono
            self.write_current(now)
            self.dirty = False
        if events or mono - self.last_state_write >= 30:
            self.save_state()
            self.last_state_write = mono
        return events

    def background(self, key: str, now: float) -> float | None:
        hit = self._bg_cache.get(key)
        mono = self.mono()
        if hit and mono - hit[0] < 600:
            return hit[1]
        try:
            v = self.store.median(key, now - 86400)
        except sqlite3.Error:
            v = None
        self._bg_cache[key] = (mono, v)
        return v

    def ingest_telemetry(self, now: float) -> tuple[list[dict], list[dict]]:
        allowed = set(self.settings["remote_nodes"])
        untrusted_ok = self.settings["remote_untrusted"]
        items, readings = [], []
        try:
            recs = self.tail.read(now)
        except OSError as e:
            log(f"cannot read mesh telemetry: {e}")
            return [], []
        for rec in recs:
            node = rec["node"]
            s = self.seen.get(node)
            if s is None:
                if len(self.seen) >= MAX_REMOTE_NODES:       # forget the node heard from longest ago
                    del self.seen[min(self.seen, key=lambda k: self.seen[k].get("at", 0))]
                s = self.seen[node] = {"node": node, "name": "", "trusted": False, "at": 0, "metrics": []}
            s["at"] = max(s.get("at", 0), rec["at"])
            if rec["name"]:
                s["name"] = rec["name"]
            s["trusted"] = bool(rec["trusted"])
            s["metrics"] = sorted(set(s.get("metrics", [])) | set(rec["values"]))[:20]
            self.dirty = True
            if node not in allowed or (not rec["trusted"] and not untrusted_ok):
                continue
            sid = sl.node_sensor_id(node)
            r = self.remote.setdefault(node, {"values": {}, "at": 0, "stored": 0})
            if rec["at"] < r.get("at", 0):
                continue                                      # older than what we have: replayed or delayed
            r["values"] = {**r.get("values", {}), **rec["values"]}
            r["values"] = dict(list(r["values"].items())[:20])
            r["at"], r["name"], r["trusted"] = rec["at"], s["name"], rec["trusted"]
            label = self.name_of(sid, s["name"] or node)
            if rec["at"] - r.get("stored", 0) < REMOTE_MIN_GAP:
                continue
            r["stored"] = rec["at"]
            for metric, v in rec["values"].items():
                key = f"{sid}:{metric}"
                items.append({"key": key, "sensor": sid, "metric": metric, "t": rec["at"], "v": v,
                              "label": label, "remote": True})
                readings.append({"key": key, "sensor": sid, "metric": metric, "value": v, "label": label,
                                 "remote": True})
        for node in [n for n in self.remote if n not in allowed]:
            del self.remote[node]
        return items, readings

    # — outputs —
    def emit(self, ev: dict, now: float) -> None:
        self.seq = max(self.seq + 1, int(now * 1000))
        ev = {**ev, "seq": self.seq}
        p = self.data / "events.jsonl"
        try:
            if p.exists() and p.stat().st_size > EVENTS_MAX:
                os.replace(p, self.data / "events.jsonl.1")
            sl.append_line(p, ev)
        except OSError as e:
            log(f"cannot write event: {e}")
        log(f"{ev['kind']}: {ev['text']}")
        if ev["kind"] in ("alert", "clear") and ev.get("mesh") and self.settings["mesh_alerts"]:
            board = "family" if ev.get("board") == "family" else self.settings["mesh_board"]
            mark = "⚠" if ev["kind"] == "alert" else "✓"
            self.queue_mesh(f"{mark} {ev['text']}", board, ev.get("level") == "danger" and ev["kind"] == "alert")

    def queue_mesh(self, text: str, board: str, urgent: bool = False, summary: bool = False) -> None:
        self.mesh_queue.append({"text": sl.clean_text(text, 400), "board": board, "urgent": urgent, "summary": summary})
        while len(self.mesh_queue) > 10:
            drop = next((i for i, m in enumerate(self.mesh_queue) if not m.get("urgent")), 0)
            del self.mesh_queue[drop]

    def flush_mesh(self, now: float) -> bool:
        self.mesh_sent = [t for t in self.mesh_sent if 0 <= now - t < 3600]
        if not self.mesh_queue or len(self.mesh_sent) >= self.mesh_per_hour:
            return False
        board = self.mesh_queue[0]["board"]
        batch = [m for m in self.mesh_queue if m["board"] == board]
        gap = self.mesh_gap_danger if any(m.get("urgent") for m in batch) else self.mesh_gap
        if self.mesh_sent and now - max(self.mesh_sent) < gap:
            return False
        text = sl.cut_bytes(sl.MESH_PREFIX + " · ".join(m["text"] for m in batch), 200)
        kind = mesh_kind(batch, board)
        try:
            self.outbox.parent.mkdir(parents=True, exist_ok=True)
            sl.append_line(self.outbox, {"text": text, "board": board, "kind": kind, "at": int(now)})
        except OSError as e:
            log(f"cannot queue mesh message: {e}")
            return False
        self.mesh_queue = [m for m in self.mesh_queue if m["board"] != board]
        self.mesh_sent.append(now)
        return True

    def summary_text(self) -> str:
        parts, have = [], set()
        for d in self.devices:
            if not d.ok:
                continue
            for metric, short in (("temperature", None), ("humidity", None), ("water_level", "water"),
                                  ("flood", "flood"), ("radiation", None), ("pressure", None)):
                if metric in d.values and metric not in have:
                    have.add(metric)
                    v = sl.fmt_value(metric, d.values[metric]).replace(" ", "")
                    if metric == "humidity":
                        v += "RH"
                    parts.append(f"{short} {v}" if short else v)
        return ", ".join(parts)

    def maybe_summary(self, now: float) -> None:
        h = self.settings["summary_every_h"]
        if not h or now - self.last_summary < h * 3600:
            return
        text = self.summary_text()
        self.last_summary = now
        if not text:
            return
        self.queue_mesh(text, self.settings["summary_board"], summary=True)
        self.emit({"kind": "summary", "at": now, "text": text, "board": "none", "mesh": False}, now)

    def write_current(self, now: float) -> None:
        sensors = []
        for d in self.devices:
            sensors.append({"id": d.id, "name": self.name_of(d.id, d.spec["name"]), "type": d.type, "ok": d.ok,
                            "error": d.error, "at": d.at or None, "values": d.values if d.ok else {},
                            "last_values": d.values, "auto": d.auto, "poll_s": d.spec["poll_s"],
                            "port": str(d.spec.get("port") or d.spec.get("serial") or
                                        (f"i2c-{self.bus} {d.spec['address']:#04x}" if isinstance(d.spec.get("address"), int) else
                                         f"GPIO{d.spec['gpio']}" if d.type == "float" else ""))[:80]})
        remote = []
        for node, r in sorted(self.remote.items()):
            sid = sl.node_sensor_id(node)
            remote.append({"id": sid, "node": node, "name": self.name_of(sid, r.get("name") or node),
                           "trusted": bool(r.get("trusted")), "at": r.get("at"), "values": r.get("values", {})})
        doc = {"updated": now, "poll_s": self.poll_s, "sensors": sensors, "remote": remote,
               "seen_nodes": sorted(self.seen.values(), key=lambda s: -s.get("at", 0)),
               "alerts": self.engine.active_list(now), "rules": self.rules,
               "config_errors": self.config_errors, "mesh_queue": len(self.mesh_queue)}
        try:
            sl.atomic_write(self.data / "current.json", json.dumps(doc, ensure_ascii=False, default=str))
        except OSError as e:
            log(f"cannot write current.json: {e}")

    def save_state(self) -> None:
        st = {"engine": self.engine.state(), "seq": self.seq, "mesh_sent": self.mesh_sent,
              "mesh_queue": self.mesh_queue, "last_summary": self.last_summary,
              "telemetry_offset": self.tail.offset, "remote": self.remote, "seen": self.seen}
        try:
            sl.atomic_write(self.data / "sensord-state.json", json.dumps(st, ensure_ascii=False, default=str))
        except OSError as e:
            log(f"cannot save state: {e}")

    def close(self) -> None:
        for d in self.devices:
            d.close()
        self.save_state()
        self.store.close()


def load_config() -> dict:
    p = Path(os.environ.get("PREPPER_CONFIG", "/etc/prepper/prepper.toml"))
    try:
        return tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        log(f"cannot read {p}: {e} — running with defaults")
        return {}


def main(argv: list[str]) -> int:
    raw = load_config()
    sc = raw.get("sensors", {}) or {}
    if sc.get("enabled", True) is False or (raw.get("features", {}) or {}).get("sensors") is False:
        log("sensors are switched off in prepper.toml — exiting")
        return 0
    d = Sensord(raw)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    log(f"started: {len(d.devices)} configured sensor(s), auto-detect {'on' if d.auto else 'off'}, data in {d.data}")
    try:
        while not stop:
            d.step()
            if "--once" in argv:
                break
            time.sleep(1)
    finally:
        d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
