"""
Front panel: the radio (weather radio / SDR 2 / antenna), sensors, forecast and neighbourhood
screens, the sensor-alert LED, and how the panel copes with missing, stale, odd or hostile files.
No display, GPIO or network needed: a fake display, temp folders, a fake clock and a tiny local
HTTP server stand in for the portal.

Run: python3.11 tests/test_panel_features.py
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "software" / "panel"))
TMP = Path(tempfile.mkdtemp(prefix="panel-feat-"))
os.environ["PREPPER_SDR_DIR"] = str(TMP / "run-sdr")
os.environ["PREPPER_SDR_STATE"] = str(TMP / "state-sdr")
os.environ["PANEL_TIMEFMT"] = str(TMP / "timefmt")
import panel  # noqa: E402

NOW = 1_800_000_000.0


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class FakeDisplay:
    def __init__(self):
        self.qrs, self.texts = [], []

    def lines(self, rows, title=None, sel=None):
        self.texts.append((list(rows), title, sel))

    def qr(self, data, caption):
        self.qrs.append((data, caption))

    def sleep(self, on):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.old_api = panel.API
        panel.API = "http://127.0.0.1:9"                     # nothing listens: api() → {}
        self.run_dir = Path(os.environ["PREPPER_SDR_DIR"])
        self.state_dir = Path(os.environ["PREPPER_SDR_STATE"])
        self.feat = TMP / "features"
        for d in (self.run_dir, self.state_dir, self.feat):
            shutil.rmtree(d, ignore_errors=True)
        self.run_dir.mkdir(parents=True)
        panel._file_cache.clear()
        self.now = NOW

    def tearDown(self):
        panel.API = self.old_api

    def make(self, cfg=None):
        self.d = FakeDisplay()
        c = {"network": {"ssid": "HoneyBadger", "passphrase": "", "hostname": "badger"},
             "portal": {"features_data": str(self.feat)}}
        c.update(cfg or {})
        p = panel.Panel(self.d, c)
        p.clock = lambda: self.now
        p.spawn = lambda fn: fn()                           # background reads run inline in tests
        return p

    def test_wifi_qr_nopass_for_owe_and_open(self):
        for sec in ("owe", "open", "owe-transition"):
            p = self.make({"network": {"ssid": "Home-7Q4K", "passphrase": "leftover-secret", "security": sec}})
            self.assertEqual(p.wifi_qr(), "WIFI:T:nopass;S:Home-7Q4K;;", sec)
        p = self.make({"network": {"ssid": "Home-7Q4K", "passphrase": "pass1234", "security": "wpa2"}})
        self.assertEqual(p.wifi_qr(), "WIFI:T:WPA;S:Home-7Q4K;P:pass1234;;")

    # files
    def write(self, path: Path, obj, raw: bytes | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".w")
        tmp.write_bytes(raw if raw is not None else json.dumps(obj).encode())
        os.replace(tmp, path)                               # a new inode, as the real writers do

    def sdr(self, **over):
        st = {"updated": self.now - 2, "pid": os.getpid(), "weather_radio": "listening", "by": None, "until": None,
              "dongles": {"count": 2, "sdr2_state": "ok", "antenna": "adsb", "fallback": False,
                          "problem": None, "note": None},
              "aircraft": {"state": "always_on"}, "satellites": {"state": "idle", "current": None}}
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(st.get(k), dict):
                st[k] = {**st[k], **v}
            else:
                st[k] = v
        self.write(self.run_dir / "status.json", st)
        return st

    def sensors_file(self, **over):
        cur = {"updated": self.now - 10, "poll_s": 30,
               "sensors": [{"id": "sht", "name": "Porch", "ok": True,
                            "values": {"humidity": 55.0, "temperature": 21.46}},
                           {"id": "geiger", "name": "Geiger", "ok": True, "values": {"radiation": 0.12}},
                           {"id": "tank", "name": "Tank", "ok": True, "values": {"water_level": 120.0}},
                           {"id": "dead", "name": "Broken", "ok": False, "values": {"temperature": 99}}],
               "remote": [], "alerts": []}
        cur.update(over)
        self.write(self.feat / "sensors" / "current.json", cur)
        return cur

    # menu helpers
    def root(self, p):
        return dict(p.root())

    def entry(self, p, prefix):
        for label, target in p.root():
            if label.startswith(prefix):
                return label, target
        return None, None

    def open_screen(self, p, prefix):
        label, target = self.entry(p, prefix)
        self.assertIsNotNone(target, f"no {prefix!r} entry in {[lbl for lbl, _ in p.root()]}")
        p.stack = [("Menu", p.root())]
        target()
        self.assertEqual(p.screen, "menu")
        return p.stack[-1]

    def rows(self, p, prefix):
        return [r for r, _ in self.open_screen(p, prefix)[1]]

    def detail(self, p, prefix, row_prefix):
        title, items = self.open_screen(p, prefix)
        for r, t in items:
            if r.startswith(row_prefix):
                t()
                return [x for x, _ in p.stack[-1][1]]
        self.fail(f"no row {row_prefix!r} in {[r for r, _ in items]}")


# ───────────────────────────── radio / SDR ─────────────────────────────

class Radio(Base):
    def test_no_radio_no_entry_and_old_labels_intact(self):
        p = self.make()
        r = self.root(p)
        self.assertNotIn("Radio", " ".join(r))
        for k in ("Join Wi-Fi (QR)", "Status", "Power mode", "System"):
            self.assertIn(k, r)

    def test_installed_but_scheduler_down(self):
        self.state_dir.mkdir(parents=True)
        p = self.make()
        rows = self.rows(p, "Radio")
        self.assertEqual(rows, ["Radio service off", "SDR 2 antenna"])

    def test_listening_and_aircraft(self):
        self.sdr()
        p = self.make()
        self.assertIn("Radio", self.root(p))
        rows = self.rows(p, "Radio")
        self.assertEqual(rows[:2], ["WX radio: listening", "SDR2 ✈ aircraft"])
        self.assertIn("SDR 2 antenna", rows)
        d = self.detail(p, "Radio", "SDR2")
        self.assertIn("Antenna: adsb", d)
        self.assertTrue(all(len(x) <= panel.W for x in d))

    def test_paused_for_a_satellite_pass(self):
        until = self.now + 600
        self.sdr(weather_radio="paused", by="satellite pass", until=until,
                 satellites={"state": "recording", "current": {"name": "METEOR-M2 3", "until": until}})
        p = self.make()
        self.assertIn("Radio ! WX paused", self.root(p))
        rows = self.rows(p, "Radio")
        self.assertEqual(rows[0], f"!WX PAUSED: sat→{panel.hm(until)}")
        self.assertLessEqual(len(rows[0]), panel.W)
        self.assertEqual(rows[1], f"SDR2 {panel.SAT} REC → {panel.hm(until)}")
        d = " ".join(self.detail(p, "Radio", "!WX"))
        self.assertIn(f"WX PAUSED: sat pass → {panel.hm(until)}", d)
        # the home screen gets an attention page
        p.screen, p.home_page = "home", 0
        p.draw()
        self.assertEqual(self.d.texts[-1][0][:2], ["! WX RADIO PAUSED", f"sat pass → {panel.hm(until)}"])

    def test_paused_for_aircraft_scan_and_odd_by(self):
        self.sdr(weather_radio="paused", by="aircraft scan", until=self.now + 60)
        p = self.make()
        self.assertIn("aircraft scan →", " ".join(self.detail(p, "Radio", "!WX")))
        self.sdr(weather_radio="paused", by="evil\nline‮flip" * 20, until=self.now + 60)
        rows = self.rows(p, "Radio")
        self.assertTrue(rows[0].startswith("!WX PAUSED: evi→"))
        self.assertNotIn("\n", "".join(rows))
        self.assertNotIn("‮", "".join(rows))

    def test_pause_over_or_bogus_until_is_not_a_pause(self):
        p = self.make()
        for until in (self.now - 1, None, "soon", True, float("inf"), [1]):
            raw = json.dumps({"updated": self.now, "pid": os.getpid(), "weather_radio": "paused",
                              "by": "satellite pass", "until": until}, allow_nan=True).encode()
            self.write(self.run_dir / "status.json", None, raw)
            self.assertNotIn("Radio ! WX paused", self.root(p), until)
            self.assertIsNone(p.wx_pause(p.sdr_status()))

    def test_stale_or_dead_writer_is_ignored(self):
        p = self.make()
        cases = [dict(updated=self.now - 31), dict(updated=self.now + 120), dict(updated="now"),
                 dict(updated=None), dict(pid=dead_pid()), dict(pid=None), dict(pid=True), dict(pid=-1),
                 dict(pid=0), dict(pid="1"), dict(pid=2 ** 70)]
        for over in cases:
            self.sdr(**over)
            self.assertIsNone(p.sdr_status(), over)
            self.assertNotIn("Radio", " ".join(self.root(p)), over)
        self.sdr(updated=self.now - 29)
        self.assertIsNotNone(p.sdr_status())

    def test_sdr2_missing_failed_off(self):
        p = self.make()
        self.sdr(dongles={"sdr2_state": "missing", "fallback": True})
        self.assertIn("SDR2 MISSING–fallback", self.rows(p, "Radio"))
        self.sdr(dongles={"sdr2_state": "failed", "sdr2_why": "readsb keeps exiting (usb error -9)"},
                 aircraft={"state": "idle"})
        rows = self.rows(p, "Radio")
        self.assertIn("SDR2 FAILED–fallback", rows)
        d = " ".join(self.detail(p, "Radio", "SDR2"))
        self.assertIn("usb error -9", d)
        self.assertIn("borrow", d)
        self.sdr(dongles={"sdr2_state": "off"}, aircraft={"state": "off"})
        self.assertIn("SDR2 off", self.rows(p, "Radio"))
        self.sdr(dongles={"sdr2_state": "ok"}, aircraft={"state": "idle"})
        self.assertIn("SDR2 idle", self.rows(p, "Radio"))
        self.sdr(dongles={"sdr2_state": "weird"}, aircraft={"state": "idle"})
        self.assertFalse(any(r.startswith("SDR2") for r in self.rows(p, "Radio")))

    def test_problem_and_note_in_status_and_radio(self):
        self.sdr(dongles={"problem": "Two dongles with the same serial: set serials (rtl_eeprom)",
                          "note": "Dongle serials not set; using device order"})
        p = self.make()
        p.stack = [("Menu", [])]
        p.status_rows()
        title, items = p.stack[-1]
        self.assertEqual(title, "Status")
        labels = [r for r, _ in items]
        self.assertTrue(any(r.startswith("! Radio: Two dongles") for r in labels), labels)
        self.assertTrue(any(r.startswith("Radio: Dongle serials not set") for r in labels), labels)
        self.assertTrue(any(r.startswith("Batt") for r in labels))
        self.assertIn("Free", " ".join(labels))               # all status rows reachable now (it scrolls)
        rows = self.rows(p, "Radio")
        self.assertTrue(any(r.startswith("! Radio: Two dongles") for r in rows))

    def test_status_screen_survives_a_hostile_radio_file(self):
        self.write(self.run_dir / "status.json", None, b'{"updated": 1e400, "pid": [], "dongles": "x"}')
        p = self.make()
        p.stack = [("Menu", [])]
        p.status_rows()
        self.assertEqual(p.stack[-1][0], "Status")

    def test_hostile_status_files(self):
        p = self.make()
        f = self.run_dir / "status.json"
        payloads = [b"", b"null", b"[]", b"42", b'"x"', b"{", b"\xff\xfe", b"[" * 100000 + b"]" * 100000,
                    json.dumps({"updated": self.now, "pid": os.getpid(), "dongles": [], "aircraft": "x",
                                "satellites": {"state": "recording", "current": "x"}}).encode(),
                    json.dumps({"updated": self.now, "pid": os.getpid(), "weather_radio": {"a": 1},
                                "dongles": {"sdr2_state": ["failed"], "sdr2_why": {"x": 1}, "antenna": ["adsb"],
                                            "problem": ["x"], "note": 5}}).encode()]
        for raw in payloads:
            self.write(f, None, raw)
            self.root(p)
            if p.sdr_status() is not None:
                p.stack = [("Menu", [])]
                p.radio_screen()
                for r, _t in p.stack[-1][1]:
                    self.assertIsInstance(r, str)
        self.write(f, None, b" " * (panel.read_json_file.__defaults__[0] + 10))   # too big: ignored
        self.assertIsNone(p.sdr_status())

    def test_symlink_and_fifo_are_not_read(self):
        p = self.make()
        real = TMP / "elsewhere.json"
        real.write_text(json.dumps({"updated": self.now, "pid": os.getpid(), "weather_radio": "listening"}))
        f = self.run_dir / "status.json"
        os.symlink(real, f)
        self.assertIsNone(p.sdr_status())
        f.unlink()
        os.mkfifo(f)
        out = {}
        t = threading.Thread(target=lambda: out.update(v=p.sdr_status()), daemon=True)
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive(), "a FIFO blocked the panel")
        self.assertIsNone(out["v"])

    def test_file_cache_sees_changes(self):
        p = self.make()
        self.sdr(weather_radio="listening")
        self.assertEqual(p.wx_row(p.sdr_status())[0], "WX radio: listening")
        self.sdr(weather_radio="off")
        self.assertEqual(p.wx_row(p.sdr_status())[0], "WX radio: off")


class Antenna(Base):
    def menu(self, p):
        return dict(self.open_screen(p, "Radio")[1])["SDR 2 antenna"]

    def test_menu_writes_the_launchers_format(self):
        self.state_dir.mkdir(parents=True)
        self.sdr()
        p = self.make()
        items = self.menu(p)
        self.assertEqual([lbl for lbl, _ in items], ["● ADS-B whip", "  Satellite", "  Both (wideband)"])
        items[1][1]()
        f = self.state_dir / "antenna.json"
        d = json.loads(f.read_text())
        self.assertEqual(d["antenna"], "satellite")
        self.assertEqual(d["at"], self.now)
        self.assertIsInstance(d["by"], str)
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o640)
        self.assertEqual(sorted(x.name for x in self.state_dir.iterdir()), ["antenna.json"])   # no scratch left
        self.assertIn("Satellite", self.d.texts[-1][0])
        self.assertEqual(p.screen, "msg")
        self.assertEqual([lbl for lbl, _ in self.menu(p)][1], "● Satellite")
        if panel._sdrc is not None:                         # the radio scheduler reads it back
            self.assertEqual(panel._sdrc.antenna({}), "satellite")
        for a in ("both", "adsb"):
            self.assertIsNone(panel.write_antenna(a, self.now))
            self.assertEqual(p.chosen_antenna(), a)

    def test_refuses_bad_values_and_missing_or_symlinked_folder(self):
        p = self.make()
        self.assertEqual(panel.write_antenna("laser", self.now), "unknown antenna")
        self.assertEqual(panel.write_antenna("adsb", self.now), "radio not set up")
        p.set_antenna("adsb")
        self.assertIn("not saved", self.d.texts[-1][0][0])
        real = TMP / "real-state"
        shutil.rmtree(real, ignore_errors=True)
        real.mkdir()
        os.symlink(real, self.state_dir)
        try:
            self.assertEqual(panel.write_antenna("adsb", self.now), "radio not set up")
            self.assertEqual(list(real.iterdir()), [])
        finally:
            self.state_dir.unlink()

    def test_symlinked_target_is_replaced_not_followed(self):
        self.state_dir.mkdir(parents=True)
        victim = TMP / "victim.txt"
        victim.write_text("precious")
        os.symlink(victim, self.state_dir / "antenna.json")
        self.assertIsNone(panel.write_antenna("both", self.now))
        self.assertEqual(victim.read_text(), "precious")
        self.assertFalse((self.state_dir / "antenna.json").is_symlink())
        self.assertEqual(json.loads((self.state_dir / "antenna.json").read_text())["antenna"], "both")

    def test_unwritable_target(self):
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "antenna.json").mkdir()            # a directory in the way
        self.assertEqual(panel.write_antenna("adsb", self.now), "could not save")
        self.assertEqual(sorted(x.name for x in self.state_dir.iterdir()), ["antenna.json"])

    def test_chosen_antenna_falls_back_to_config(self):
        self.state_dir.mkdir(parents=True)
        p = self.make({"radio": {"sdr2_antenna": " Satellite "}})
        self.assertEqual(p.chosen_antenna(), "satellite")
        self.write(self.state_dir / "antenna.json", {"antenna": "../../etc"})
        self.assertEqual(p.chosen_antenna(), "satellite")
        p = self.make({"radio": {"sdr2_antenna": 7}})
        self.assertEqual(p.chosen_antenna(), "adsb")


# ───────────────────────────── sensors ─────────────────────────────

class Sensors(Base):
    def test_no_file_or_disabled_no_entry(self):
        p = self.make()
        self.assertFalse(any(lbl.startswith("Sensors") for lbl in self.root(p)))
        self.sensors_file()
        self.assertIn("Sensors", self.root(p))
        for cfg in ({"features": {"sensors": False}}, {"sensors": {"enabled": False}}):
            self.assertFalse(any(lbl.startswith("Sensors") for lbl in self.root(self.make(cfg))), cfg)

    def test_readings_in_order_metric_and_us(self):
        self.sensors_file()
        p = self.make()
        rows = self.rows(p, "Sensors")
        self.assertEqual(rows, ["No alerts", "Temp 21.5°C", "Humidity 55%", "Water 120 cm", "Radiation 0.12 µSv/h"])
        p = self.make({"sensors": {"units": "us"}})
        rows = self.rows(p, "Sensors")
        self.assertIn("Temp 70.6°F", rows)
        self.assertIn("Water 47.2 in", rows)
        self.assertNotIn("Broken", " ".join(rows))          # a sensor that isn't ok shows no reading

    def test_same_metric_twice_names_the_sensor(self):
        self.sensors_file(sensors=[{"id": "a", "name": "Shed", "ok": True, "values": {"temperature": 5}},
                                   {"id": "b", "name": "Attic", "ok": True, "values": {"temperature": -0.04}}])
        rows = self.rows(self.make(), "Sensors")
        self.assertEqual(rows[1:], ["Temp 0.0°C Attic", "Temp 5.0°C Shed"])

    def test_alerts_first_danger_first_and_led(self):
        self.sensors_file(alerts=[
            {"key": "sht:humidity", "sensor": "sht", "metric": "humidity", "value": 96, "level": "warning",
             "text": "Porch: humidity 96%, above the alert level of 90%", "stale": False},
            {"key": "tank:water_level", "sensor": "tank", "metric": "water_level", "value": 310, "level": "danger",
             "text": "Tank: water level 310 cm, above the alert level of 300 cm", "stale": False}])
        p = self.make()
        self.assertIn("Sensors !! DANGER", self.root(p))
        rows = self.rows(p, "Sensors")
        self.assertEqual(rows[:2], ["!! Water 310 cm", "! Humidity 96%"])
        self.assertTrue(rows[2].startswith("Temp"))
        d = " ".join(self.detail(p, "Sensors", "!! Water"))
        self.assertIn("above the alert level of 300 cm", d)
        self.assertTrue(p.sensor_danger())
        self.assertEqual(panel.led_state({}, sensor_danger=p.sensor_danger()), "fast")
        p.screen, p.home_page = "home", 0
        p.draw()
        self.assertEqual(self.d.texts[-1][0][0], "!! SENSOR DANGER")

    def test_warning_only_and_stale_alerts_dont_blink(self):
        self.sensors_file(alerts=[{"sensor": "sht", "metric": "humidity", "value": 96, "level": "warning",
                                   "text": "humid", "stale": False},
                                  {"sensor": "tank", "metric": "water_level", "value": 310, "level": "danger",
                                   "text": "old flood", "stale": True}])
        p = self.make()
        self.assertIn("Sensors ! alert", self.root(p))
        self.assertFalse(p.sensor_danger())
        self.assertEqual(panel.led_state({}, sensor_danger=p.sensor_danger()), "solid")
        rows = self.rows(p, "Sensors")
        self.assertIn("!! Water 310 cm?", rows)

    def test_stopped_service_shows_no_alerts(self):
        self.sensors_file(updated=self.now - 600, alerts=[{"sensor": "tank", "metric": "water_level", "value": 310,
                                                            "level": "danger", "text": "flood", "stale": False}])
        p = self.make()
        self.assertIn("Sensors", self.root(p))
        rows = self.rows(p, "Sensors")
        self.assertEqual(rows[0], "Sensors not running")
        self.assertFalse(any(r.startswith("!") for r in rows))
        self.assertFalse(p.sensor_danger())
        # slow polling stretches the window, as in the portal (4 × poll_s)
        self.sensors_file(updated=self.now - 600, poll_s=300)
        self.assertNotEqual(self.rows(p, "Sensors")[0], "Sensors not running")

    def test_remote_nodes_only_when_allowed(self):
        remote = [{"id": "mesh-0000beef", "node": "!0000beef", "name": "Neighbour", "values": {"temperature": 30}}]
        alerts = [{"sensor": "mesh-0000beef", "metric": "temperature", "value": 60, "level": "danger",
                   "text": "Neighbour (mesh radio): hot", "remote": True, "stale": False}]
        self.sensors_file(remote=remote, alerts=alerts)
        p = self.make()
        self.assertFalse(p.sensor_danger())
        self.assertNotIn("30.0", " ".join(self.rows(p, "Sensors")))
        p = self.make({"sensors": {"remote_nodes": ["!0000BEEF"]}})
        self.assertTrue(p.sensor_danger())
        self.assertIn("Temp 30.0°C Neighbour", self.rows(p, "Sensors"))
        # the launcher's saved settings beat the config
        self.write(self.feat / "sensors" / "settings.json", {"remote_nodes": []})
        self.assertFalse(p.sensor_danger())

    def test_hostile_sensor_file(self):
        p = self.make()
        f = self.feat / "sensors" / "current.json"
        for raw in (b"[]", b"{", b'{"sensors": 5, "alerts": "x", "remote": {}}',
                    b'{"updated": NaN, "sensors": [1, null, {"ok": true, "values": [1]}]}',
                    json.dumps({"updated": self.now, "sensors": [{"id": {"x": 1}, "name": "\x1b[2J\nHi", "ok": True,
                                                                "values": {"temperature": True, "humidity": "5",
                                                                           "pressure": 1e300, "nope": 5,
                                                                           "water_level": 12}}],
                                "remote": [{"node": ["!0000beef"], "values": {"temperature": 1}}],
                                "alerts": [5, {"level": "danger", "metric": "x", "value": "y", "text": ["z"]},
                                           {"level": "danger", "metric": ["temperature"], "sensor": ["s"],
                                            "remote": True, "text": "list keys"},
                                           {"level": "danger", "metric": "flood", "value": 1, "sensor": "s",
                                            "text": "‮flood\n\n" * 100}]}).encode()):
            self.write(f, None, raw)
            self.root(p)
            p.sensor_danger()
            s = p.sensors()
            if s is not None:
                p.stack = [("Menu", [])]
                p.sensors_screen()
                for r, _t in p.stack[-1][1]:
                    self.assertNotIn("\n", r)
                    self.assertNotIn("\x1b", r)
                    self.assertNotIn("‮", r)
                p.screen, p.home_page = "home", 0
                p.draw()
        rows = [r for r, _ in p.stack[-1][1]]
        self.assertIn("!! Flood WET", rows)
        self.assertIn("!! Sensor", rows)                 # a danger alert on an unknown metric
        self.assertIn("Water 12 cm", rows)
        self.assertFalse(any("Pressure" in r for r in rows))


# ───────────────────────────── forecast ─────────────────────────────

class Forecast(Base):
    def files(self, status=None, latest=None):
        if status is not None:
            self.write(self.feat / "forecast" / "status.json", status)
        if latest is not None:
            self.write(self.feat / "forecast" / "latest.json", latest)

    def test_forecast_alone_shows_the_radio_entry(self):
        p = self.make()
        self.assertNotIn("Radio", self.root(p))
        self.files({"state": "ok", "message": "New forecast captured.", "at": self.now - 7200},
                   {"at": self.now - 7200, "short": "Today sunny, high 78. Tonight 40% storms, low 60.",
                    "headline": "x", "raw": "y" * 30000})
        self.assertIn("Radio", self.root(p))
        rows = self.rows(p, "Radio")
        self.assertEqual(rows, ["Forecast 2 h ago"])
        d = self.detail(p, "Radio", "Forecast")
        self.assertIn("Today sunny, high 78.", " ".join(d))
        self.assertTrue(any(x.startswith("Heard ") for x in d))
        self.assertTrue(all(len(x) <= panel.W for x in d))

    def test_states(self):
        p = self.make()
        want = {"running": "Forecast: updating…", "no_signal": "Forecast: no signal", "no_radio": "Forecast: no radio",
                "busy": "Forecast: radio busy", "no_whisper": "Forecast: not set up", "skipped": "Forecast: skipped",
                "error": "Forecast: failed", "bogus": "Forecast: none yet", 5: "Forecast: none yet"}
        for state, row in want.items():
            self.files({"state": state, "message": "No clear signal from the weather radio (static on 162.55 MHz).",
                        "at": self.now - 60})
            fc = p.forecast()
            self.assertEqual(p.forecast_row(fc), row, state)
        self.files({"state": "no_signal", "message": "No clear signal (static).", "at": self.now})
        self.assertIn("No clear signal (static).", " ".join(self.detail(p, "Radio", "Forecast")))
        self.assertIn("No forecast yet.", self.detail(p, "Radio", "Forecast"))

    def test_a_run_that_died_half_way(self):
        p = self.make()
        self.files({"state": "running", "message": "Turning…", "at": self.now - 4000})
        self.assertEqual(p.forecast_row(p.forecast()), "Forecast: stuck?")
        self.files({"state": "running", "message": "Turning…", "at": self.now - 100})
        self.assertEqual(p.forecast_row(p.forecast()), "Forecast: updating…")

    def test_disabled_missing_and_hostile(self):
        self.files({"state": "ok"}, {"short": "x", "at": self.now})
        self.assertIsNone(self.make({"features": {"forecast": False}}).forecast())
        self.assertIsNone(self.make({"forecast": {"enabled": False}}).forecast())
        p = self.make()
        for st, lt in ((b"[]", b"[]"), (b"{", b"\x00"), (b'{"state": ["ok"], "at": "x"}', b'{"short": 5, "at": "y"}'),
                       (b'{"state": "ok", "message": {"a": 1}}', b'{"short": "line1\\nline2\\u202e", "at": 1e999}')):
            self.write(self.feat / "forecast" / "status.json", None, st)
            self.write(self.feat / "forecast" / "latest.json", None, lt)
            fc = p.forecast()
            if fc is not None:
                p.forecast_row(fc)
                for line in p.forecast_lines(fc):
                    self.assertNotIn("\n", line)
                    self.assertNotIn("‮", line)
        shutil.rmtree(self.feat / "forecast")
        self.assertIsNone(p.forecast())


# ───────────────────────────── neighbourhood ─────────────────────────────

class FakePortal:
    def __init__(self):
        self.aid = (200, {"set": True, "open": True, "items": ["water", "first_aid", "laser"],
                          "text": "Behind the school\n‮side door", "updated": NOW - 1800, "stale": False})
        self.missing = (200, {"posts": [
            {"kind": "person", "status": "missing", "name": "Ann Example"},
            {"kind": "person", "status": "missing", "name": "Bob\nExample"},
            {"kind": "person", "status": "safe", "name": "Safe Person"},
            {"kind": "item", "status": "missing", "name": "A dog"}, 5, None]})
        self.seen, self.delay = [], 0.0
        portal = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith(("/api/aidstation", "/api/missing")):
                    portal.seen.append((self.path, dict(self.headers)))
                    time.sleep(portal.delay)
                if self.path.startswith("/api/aidstation/status"):
                    code, body = portal.aid
                elif self.path.startswith("/api/missing"):
                    code, body = portal.missing
                else:
                    code, body = 404, {"error": "not found"}
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Neighbourhood(Base):
    def setUp(self):
        super().setUp()
        self.portal = FakePortal()
        panel.API = self.portal.url
        self.mono = 1000.0

    def tearDown(self):
        self.portal.close()
        super().tearDown()

    def make(self, cfg=None):
        p = super().make(cfg)
        for poll in p.polls.values():
            poll.clock = lambda: self.mono
        return p

    def test_rows_and_details(self):
        p = self.make()
        self.assertIn("Neighbourhood", self.root(p))
        rows = self.rows(p, "Neighbourhood")
        self.assertEqual(rows, ["Aid: OPEN water 1st aid", "Missing: 2 people"])
        d = self.detail(p, "Neighbourhood", "Aid")
        self.assertEqual(d[0], "Aid station OPEN")
        self.assertIn("Has: Water, First aid", d)
        self.assertIn("Behind the school side door", " ".join(d))
        self.assertIn("Updated 30 min ago", d)
        d = self.detail(p, "Neighbourhood", "Missing")
        self.assertEqual(d[:2], ["• Ann Example", "• Bob Example"])
        paths = [s[0] for s in self.portal.seen]
        self.assertIn("/api/missing?kind=person&status=missing", paths)
        self.assertIn("/api/aidstation/status", paths)

    def test_closed_stale_unset_and_counts(self):
        p = self.make()
        self.portal.aid = (200, {"set": True, "open": False, "items": [], "text": "", "updated": NOW, "stale": True})
        self.portal.missing = (200, {"posts": [{"kind": "person", "status": "missing", "name": ""}]})
        self.root(p)
        self.assertEqual(self.rows(p, "Neighbourhood"), ["Aid: closed? (old)", "Missing: 1 person"])
        self.assertIn("• (no name given)", self.detail(p, "Neighbourhood", "Missing"))
        self.mono += 61
        self.portal.aid = (200, {"set": False})
        self.portal.missing = (200, {"posts": []})
        self.root(p)
        self.assertEqual(self.rows(p, "Neighbourhood"), ["Aid station: no news", "Missing: none"])
        self.mono += 61
        self.portal.missing = (200, {"posts": [{"kind": "person", "status": "missing", "name": f"P{i}"}
                                               for i in range(25)]})
        self.root(p)
        d = self.detail(p, "Neighbourhood", "Missing")
        self.assertIn("…and 5 more", d)

    def test_polls_gently_and_keeps_the_last_good_answer(self):
        p = self.make()
        self.root(p)
        n = len(self.portal.seen)
        self.assertEqual(n, 2)
        for _ in range(5):                                  # menu opens and ticks within a minute: no new reads
            self.root(p)
            p.tick()
            self.mono += 5
        self.assertEqual(len(self.portal.seen), n)
        self.mono += 60
        self.portal.aid = (500, {"error": "boom"})
        self.portal.missing = (200, b"<html>bad gateway</html>")
        self.root(p)
        self.assertEqual(len(self.portal.seen), n + 2)
        self.assertEqual(self.rows(p, "Neighbourhood")[0], "Aid: OPEN water 1st aid")   # last good answer
        self.mono += panel.NB_KEEP_S + 1
        self.assertNotIn("Neighbourhood", " ".join(lbl for lbl, _ in p.root()))   # too old now: hidden
        self.assertGreaterEqual(panel.Poll(lambda: 1, every=1).every, 30)

    def test_not_installed_or_disabled(self):
        self.portal.aid = (404, {"error": "not found"})
        self.portal.missing = (404, {"error": "not found"})
        p = self.make()
        self.assertNotIn("Neighbourhood", self.root(p))
        self.portal.aid = (200, {"set": False})
        p = self.make({"features": {"aidstation": False, "missing": False}})
        before = len(self.portal.seen)
        self.assertNotIn("Neighbourhood", self.root(p))
        p.root()
        p.tick()
        self.assertEqual(len(self.portal.seen), before)    # disabled features are never asked

    def test_odd_replies(self):
        p = self.make()
        for aid, miss in (([], []), ("x", 5), ({"set": "yes"}, {"posts": "many"}),
                          ({"set": True, "open": "yes", "items": "water", "text": ["x"], "updated": "now"},
                           {"posts": [{"kind": ["person"], "status": "missing"}, {"status": "missing", "name": 7}]}),
                          (b"[" * 50000, b"{" * 10), (b'{"set": true, "items": [' + b'"water",' * 100000 + b'"food"]}',
                                                     b'{"posts": [' + b'{},' * 400000 + b'{}]}')):
            self.portal.aid, self.portal.missing = (200, aid), (200, miss)
            self.mono += 61
            self.root(p)
            if any(x.get() is not None for x in p.polls.values()):
                p.stack = [("Menu", [])]
                p.neighbourhood_screen()
                for r, _ in p.stack[-1][1]:
                    self.assertNotIn("\n", r)
        a = panel.fetch_aid()
        self.assertIsNone(a)                                # > 64 KB reply: cut off, not parsed

    def test_the_knob_never_waits_for_the_portal(self):
        self.portal.delay = 2.0
        p = super().make()
        p.spawn = lambda fn: threading.Thread(target=fn, daemon=True).start()   # real background threads
        t0 = time.monotonic()
        p.root()
        p.tick()
        self.assertLess(time.monotonic() - t0, 1.0)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not all(x.get() is not None for x in p.polls.values()):
            time.sleep(0.05)
        self.assertIn("Neighbourhood", self.root(p))

    def test_sends_the_panel_key(self):
        keyf = TMP / "panel.key"
        keyf.write_text("k" * 43)
        old_key, old_cache = panel.PANEL_KEY, dict(panel._key_cache)
        panel.PANEL_KEY = keyf
        panel._key_cache.clear()
        try:
            self.make().root()
            self.assertEqual(self.portal.seen[-1][1].get("X-Badger-Panel"), "k" * 43)
        finally:
            panel.PANEL_KEY = old_key
            panel._key_cache.clear()
            panel._key_cache.update(old_cache)


# ───────────────────────────── knob walk, LED, helpers ─────────────────────────────

class Knob(Base):
    def test_walk_into_radio_and_back(self):
        self.sdr()
        p = self.make()
        p.press()                                           # home → menu
        labels = [lbl for lbl, _ in p.stack[-1][1]]
        p.turn(labels.index("Radio"))
        p.press()
        self.assertEqual(p.stack[-1][0], "Radio")
        self.assertEqual(self.d.texts[-1][1], "Radio")
        p.turn(1)
        p.press()                                           # SDR2 detail page
        self.assertEqual(p.stack[-1][0], "SDR 2")
        self.assertEqual(p.stack[-1][1][0][0], "SDR2 ✈ aircraft")
        p.press()                                           # any row: back
        self.assertEqual(p.stack[-1][0], "Radio")
        n = len(p.stack[-1][1])
        p.turn(n - p.sel)                                   # ‹ Back
        p.press()
        self.assertEqual(p.stack[-1][0], "Menu")
        p.sel = len(p.stack[-1][1])
        p.press()
        self.assertEqual(p.screen, "home")

    def test_sleep_from_a_feature_page(self):
        self.sensors_file()
        p = self.make()
        self.open_screen(p, "Sensors")
        p.last_input -= p.sleep_after + 1
        p.tick()
        self.assertEqual((p.screen, p.stack, p.asleep), ("home", [], True))

    def test_led_states(self):
        self.assertEqual(panel.led_state({}), "solid")
        self.assertEqual(panel.led_state({}, sensor_danger=True), "fast")
        self.assertEqual(panel.led_state({"alerts": [{"event": "Tornado Warning"}]}), "fast")
        self.assertEqual(panel.led_state({"power": {"percent": 10, "charging": False}}, sensor_danger=True), "fast")
        self.assertEqual(panel.led_state({"power": {"percent": 10, "charging": False}}), "medium")

    def test_everything_broken_menu_still_works(self):
        for d in (self.run_dir, self.feat / "sensors", self.feat / "forecast"):
            d.mkdir(parents=True, exist_ok=True)
        for f in (self.run_dir / "status.json", self.feat / "sensors" / "current.json",
                  self.feat / "forecast" / "status.json"):
            os.mkfifo(f)
        p = self.make()
        out = {}
        t = threading.Thread(target=lambda: out.update(r=p.root(), d=p.sensor_danger(), a=p.attention_pages()),
                             daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertIn("Status", dict(out["r"]))
        self.assertFalse(out["d"])
        self.assertEqual(out["a"], [])

    def test_helpers(self):
        self.assertEqual(panel.clean("a\n\tb‮c\x00d  e"), "a b c d e")
        self.assertEqual(panel.clean(5), "")
        self.assertEqual(panel.clean("x" * 500, 10), "x" * 10)
        for v in (True, "1", None, float("nan"), float("inf")):
            self.assertIsNone(panel.finite(v))
        self.assertEqual(panel.hm(1e300), "--:--")
        self.assertEqual(panel.hm("x"), "--:--")
        self.assertEqual(panel.ago(30), "just now")
        self.assertEqual(panel.ago(3 * 86400), "3 days ago")
        self.assertEqual(panel.fmt_reading("flood", 0.2), "dry")
        self.assertEqual(panel.fmt_reading("pressure", 1013.25, us=True), "29.92 inHg")
        self.assertTrue(all(len(x) <= panel.W for x in panel.wrap("y" * 100)))


# ───────────────────────────── panel lock + device approval ─────────────────────────────

class PanelLockKnob(Base):
    """Red team 5: the optional panel code (default off), entered with the knob."""

    def setUp(self):
        super().setUp()
        self.lockf, self.arm = TMP / "panel-lock.json", TMP / "panel-lock-arm.json"
        for f in (self.lockf, self.arm):
            f.unlink(missing_ok=True)
        panel._file_cache.clear()

    def make(self, cfg=None):
        c = {"panel": {"lock_file": str(self.lockf)},
             "portal": {"features_data": str(self.feat), "panel_lock_arm": str(self.arm)}}
        c.update(cfg or {})
        return super().make(c)

    def enter(self, p, code):
        for digit in code:
            p.code_sel = int(digit)
            p.press()
        p.code_sel = p.code_choices().index("OK")
        p.press()

    def test_default_off_changes_nothing(self):
        p = self.make()
        menu = dict(p.root())
        menu["Services"]()
        self.assertEqual((p.screen, p.stack[-1][0]), ("menu", "Services"))
        p.stack = [("Menu", list(menu.items()))]
        menu["Invite member (QR)"]()
        self.assertEqual(p.screen, "confirm")

    def test_set_with_the_knob_only_when_armed_then_unlock_and_relock_on_sleep(self):
        p = self.make()
        p.stack = [("Menu", [])]
        p.lock_menu()
        self.assertEqual([r for r, _ in p.stack[-1][1]][0], "Off. To set a code,")
        self.arm.write_text(json.dumps({"until": time.time() + 600}))
        panel._file_cache.clear()
        p.lock_menu()
        dict(p.stack[-1][1])["Set a code"]()
        self.assertEqual((p.screen, p.code_mode), ("code", "set1"))
        self.enter(p, "13579")
        self.assertEqual(p.code_mode, "set2")
        self.enter(p, "13579")
        self.assertIn("Recovery code", self.d.texts[-1][0][0])
        rec = "".join(self.d.texts[-1][0][1].split())
        self.assertRegex(rec, r"^\d{12}$")
        self.assertTrue(p.plock.is_set())
        # now guarded: wrong code refused, Cancel goes back, right code opens
        menu = dict(p.root())
        p.stack = [("Menu", list(menu.items()))]
        menu["Services"]()
        self.assertEqual(p.screen, "code")
        self.enter(p, "1111")
        self.assertEqual((p.screen, self.d.texts[-1][0][0]), ("msg", "Wrong code."))
        menu["Services"]()
        p.code_sel = p.code_choices().index("Cancel")
        p.press()
        self.assertEqual(p.screen, "menu")
        menu["Services"]()
        p.code_sel = 1; p.press()
        p.code_sel = p.code_choices().index("Del"); p.press()           # Del removes the slip
        self.assertEqual(p.code_buf, "")
        self.enter(p, "13579")
        self.assertEqual((p.screen, p.stack[-1][0]), ("menu", "Services"))
        menu["Invite member (QR)"]()                                     # unlocked for a while: no second prompt
        self.assertEqual(p.screen, "confirm")
        p.last_input -= p.sleep_after + 1
        p.tick()                                                         # asleep = locked again
        p.wake()
        p.stack = [("Menu", list(menu.items()))]
        menu["Services"]()
        self.assertEqual(p.screen, "code")
        # Forgot? → recovery code → the lock is gone
        p.code_sel = p.code_choices().index("Forgot?"); p.press()
        self.assertEqual(p.code_mode, "recover")
        self.enter(p, rec)
        self.assertFalse(p.plock.is_set())

    def test_change_and_off_need_the_code_and_mismatch_changes_nothing(self):
        panel.PanelLock(self.lockf).set("2468")
        p = self.make()
        p.stack = [("Menu", [])]
        p.lock_menu()
        items = dict(p.stack[-1][1])
        items["Turn lock off"]()
        self.assertEqual(p.screen, "code")
        self.enter(p, "2468")
        self.assertEqual(p.screen, "confirm")
        p.sel = 1
        p.press()
        self.assertFalse(p.plock.is_set())
        panel.PanelLock(self.lockf).set("2468")
        p = self.make()
        p.stack = [("Menu", [])]
        p.lock_menu()
        dict(p.stack[-1][1])["Change code"]()
        self.enter(p, "2468")
        self.assertEqual(p.code_mode, "set1")
        self.enter(p, "9999")
        self.enter(p, "9998")
        self.assertIn("differ", self.d.texts[-1][0][1])
        self.assertEqual(panel.PanelLock(self.lockf).check("2468")[0], "ok")
        self.assertEqual(p.code_choices()[:10], [str(i) for i in range(10)])

    def test_damaged_lock_file_stays_locked_and_the_console_reset_clears_it(self):
        self.lockf.write_text("{not json")
        p = self.make()
        menu = dict(p.root())
        p.stack = [("Menu", list(menu.items()))]
        menu["Family radio"]()
        self.assertEqual(p.screen, "msg")
        self.assertIn("damaged", " ".join(self.d.texts[-1][0]))
        cfg = TMP / "panel-reset.toml"
        cfg.write_text(f'[panel]\nlock_file = "{self.lockf}"\ndisplay = "none"\n')
        r = subprocess.run([sys.executable, str(REPO / "software/panel/panel.py"), "--reset-lock"],
                           env={**os.environ, "PREPPER_CONFIG": str(cfg)}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.lockf.exists())

    def test_protect_list_is_configurable_and_bogus_keys_ignored(self):
        panel.PanelLock(self.lockf).set("2468")
        p = self.make({"panel": {"lock_file": str(self.lockf), "lock_protect": ["wifi_join", "nonsense"]}})
        self.assertEqual(p.protect, {"wifi_join"})
        menu = dict(p.root())
        menu["Join Wi-Fi (QR)"]()
        self.assertEqual(p.screen, "code")
        p.stack = [("Menu", list(menu.items()))]
        menu["Services"]()
        self.assertEqual(p.screen, "menu")

    def test_arm_window_is_bounded(self):
        now = time.time()
        for rec, want in (({"until": now + 600}, True), ({"until": now - 1}, False), ({"until": now + 86400}, False),
                          ({"until": "soon"}, False), ([1], False)):
            self.arm.write_text(json.dumps(rec))
            panel._file_cache.clear()
            self.assertEqual(panel.lock_armed(self.arm, now), want, rec)


class ApproveDevice(Base):
    def setUp(self):
        super().setUp()
        self.pending = [{"member": "m_1", "name": "Mom", "device": "d_1", "label": "new phone"}]
        self.posted = []
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj):
                raw = json.dumps(obj).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.reply({"pending": me.pending} if self.path == "/api/local/devices" else {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                me.posted.append((self.path, body))
                me.pending = []
                self.reply({"ok": True})
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        panel.API = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        super().tearDown()

    def test_panel_lists_and_approves(self):
        lockf = TMP / "approve-lock.json"
        lockf.unlink(missing_ok=True)
        p = self.make({"panel": {"lock_file": str(lockf)}})
        menu = dict(p.root())
        self.assertIn("Approve device (1)", menu)
        p.stack = [("Menu", list(menu.items()))]
        menu["Approve device (1)"]()
        dict(p.stack[-1][1])["Mom: new phone"]()
        dict(p.stack[-1][1])["Approve"]()
        p.sel = 1
        p.press()
        self.assertEqual(self.posted, [("/api/local/approve", {"member": "m_1", "device": "d_1", "approve": True})])
        self.assertNotIn("Approve device (1)", dict(p.root()))

    def test_guarded_by_the_panel_code(self):
        lockf = TMP / "approve-lock.json"
        panel.PanelLock(lockf).set("2468")
        try:
            p = self.make({"panel": {"lock_file": str(lockf)}})
            menu = dict(p.root())
            p.stack = [("Menu", list(menu.items()))]
            menu["Approve device (1)"]()
            self.assertEqual(p.screen, "code")
        finally:
            lockf.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
