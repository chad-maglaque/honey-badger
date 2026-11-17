"""
Aircraft: cleaning readsb's aircraft.json, the public list over HTTP (validation, distance/bearing,
rate limit), who may start a scan, the scheduler's rules (SDR 2 always-on with the aircraft whip,
on demand with the satellite antenna, the weather-radio fallback with its limits), and real jobs
with a fake readsb: a fallback scan that pauses and then returns the weather radio, SDR 2
watching all the time and stepping aside for a satellite pass, and SDR 2 failing → fallback.
Run: python3.11 tests/test_aircraft.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="air-")).resolve()
os.environ.update({
    "PREPPER_SDR_DIR": str(TMP / "run-sdr"), "PREPPER_SDR_STATE": str(TMP / "state"),
    "ALERTS_FILE": str(TMP / "alerts.json"), "ALERTD_BEAT": str(TMP / "alertd.ok"),
    "PREPPER_USB_SYSFS": str(TMP / "sysfs"), "PREPPER_GPSD": "127.0.0.1:9", "PORTAL_QUIET": "1",
    "PREPPER_CONFIG": str(TMP / "prepper.toml"),
})
for sub in ("sdr", "common", "portal"):
    sys.path.insert(0, str(REPO / "software" / sub))

import sdrcommon as C  # noqa: E402
import sdrd  # noqa: E402
import sdrlease  # noqa: E402

HOME = (39.74, -104.99)                     # Denver
FAKE_READSB = '''#!{py}
import json, os, signal, sys, time
args = sys.argv[1:]
open({calls!r}, "a").write(json.dumps(args) + "\\n")
if os.path.exists({fail!r}):
    print("usb_claim_interface error -6", flush=True)
    sys.exit(1)
out = args[args.index("--write-json") + 1]
signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
while True:
    data = {{"now": time.time(), "aircraft": [
        {{"hex": "a1b2c3", "flight": "UAL123  ", "alt_baro": 35000, "gs": 450.5, "track": 270.2,
          "lat": 40.0, "lon": -105.3, "seen": 0.4, "seen_pos": 1.0, "squawk": "1200", "category": "A3"}},
        {{"hex": "abc123", "alt_baro": "ground", "seen": 2}}]}}
    tmp = os.path.join(out, ".a.tmp")
    open(tmp, "w").write(json.dumps(data))
    os.replace(tmp, os.path.join(out, "aircraft.json"))
    time.sleep(0.3)
'''
FAKE_SATDUMP = '''#!{py}
import json, sys, time
open({calls!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
time.sleep(2)
'''


def write_cfg(extra=""):
    """The test settings plus `extra` (TOML), merged table by table."""
    import tomllib
    cfg = {"time": {"latitude": HOME[0], "longitude": HOME[1]},
           "aircraft": {"readsb": str(TMP / "bin" / "readsb")}, "satellites": {"satdump": str(TMP / "bin" / "satdump")}}
    for k, v in tomllib.loads(extra).items():
        cfg.setdefault(k, {}).update(v)
    out = []
    for table, kv in cfg.items():
        out.append(f"[{table}]")
        out += [f"{k} = {json.dumps(v)}" for k, v in kv.items()]
    (TMP / "prepper.toml").write_text("\n".join(out) + "\n")


def setup_bins():
    (TMP / "bin").mkdir(exist_ok=True)
    for name, src in (("readsb", FAKE_READSB), ("satdump", FAKE_SATDUMP)):
        p = TMP / "bin" / name
        p.write_text(src.format(py=sys.executable, calls=str(TMP / f"{name}.calls"), fail=str(TMP / "readsb.fail")))
        p.chmod(0o755)


def calls(name):
    try:
        return [json.loads(x) for x in (TMP / f"{name}.calls").read_text().splitlines()]
    except OSError:
        return []


def sysfs(serials):
    root = TMP / "sysfs"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    for i, s in enumerate(serials):
        d = root / f"1-1.{i + 1}"
        d.mkdir()
        (d / "idVendor").write_text("0bda")
        (d / "idProduct").write_text("2838")
        (d / "serial").write_text(s)


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Owner:
    """A pretend alertd holding the weather dongle, yielding when asked."""

    def __init__(self):
        self.stop = threading.Event()
        self.log = []
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        while not self.stop.is_set():
            with sdrlease.hold("weather", poll=0.05):
                self.log.append("hold")
                while not self.stop.is_set():
                    if sdrlease.owner_should_yield("weather"):
                        self.log.append("yield")
                        break
                    time.sleep(0.05)

    def close(self):
        self.stop.set()
        self.t.join(3)


def load_feature():
    import importlib.util
    spec = importlib.util.spec_from_file_location("air_feat", REPO / "software" / "portal" / "features" / "aircraft.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Clean(unittest.TestCase):
    def setUp(self):
        self.m = load_feature()
        self.origin = {"lat": HOME[0], "lon": HOME[1]}

    def test_good_record(self):
        a = self.m.clean({"hex": "A1B2C3", "flight": "ual123 ", "alt_baro": 35012, "gs": 450.4, "track": 270.6,
                          "baro_rate": -1200, "lat": 40.0, "lon": -105.3, "seen": 0.44, "seen_pos": 1, "squawk": "7700",
                          "category": "A3", "emergency": "general"}, self.origin, 450)
        self.assertEqual((a["hex"], a["flight"], a["alt_ft"], a["speed_kt"], a["track"], a["vrate_fpm"]),
                         ("a1b2c3", "UAL123", 35012, 450, 271, -1200))
        self.assertEqual((a["squawk"], a["category"], a["emergency"]), ("7700", "A3", "general"))
        self.assertAlmostEqual(a["dist_km"], 39.5, delta=1.5)           # Denver → just west of Boulder
        self.assertTrue(300 < a["bearing"] < 330, a["bearing"])           # north-west

    def test_hostile_and_broken_fields(self):
        m, o = self.m, self.origin
        for bad_hex in ("", "zzzzzz", "a1b2c3d", "<b>", None, 123456):
            self.assertIsNone(m.clean({"hex": bad_hex}, o, 450))
        a = m.clean({"hex": "~0a0b0c", "flight": "<script>alert(1)</script>", "alt_baro": float("nan"),
                     "alt_geom": 1e9, "gs": -5, "track": 999, "lat": 91, "lon": 0, "squawk": "9999",
                     "category": "Z9", "emergency": "<img>", "seen": "x"}, o, 450)
        self.assertEqual(a["flight"], "SCRIPTAL")                          # only A-Z 0-9 space -, 8 chars
        self.assertIsNone(a["alt_ft"]); self.assertIsNone(a["speed_kt"]); self.assertIsNone(a["track"])
        self.assertIsNone(a["lat"]); self.assertIsNone(a["squawk"]); self.assertIsNone(a["category"])
        self.assertIsNone(a["emergency"])
        self.assertEqual(m.clean({"hex": "abcdef", "alt_baro": "ground"}, o, 450)["alt_ft"], "ground")
        self.assertIsNone(m.clean({"hex": "abcdef", "lat": True, "lon": 1}, o, 450)["lat"])
        old = m.clean({"hex": "abcdef", "lat": 40, "lon": -105, "seen_pos": 120}, o, 450)
        self.assertIsNone(old["lat"])                                      # old position: listed, not mapped
        self.assertIsNone(m.clean({"hex": "abcdef", "lat": -33.9, "lon": 151.2}, o, 450))   # garbled: far away
        self.assertIsNone(m.clean("junk", o, 450))
        nopos = m.clean({"hex": "abcdef", "lat": 40, "lon": -105}, None, 450)
        self.assertIsNone(nopos["dist_km"])


class Portal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        feats = TMP / "features"
        feats.mkdir(exist_ok=True)
        os.symlink(REPO / "software" / "portal" / "features" / "aircraft.py", feats / "aircraft.py")
        (TMP / "data").mkdir(exist_ok=True)
        cfg = TMP / "portal.toml"
        cfg.write_text(f"""
[upd]
data_root = "{TMP / 'data'}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{TMP}/upd-state.json"
report_dir = "{TMP}/reports"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
admin_pin = "{server.hash_pin('246810')}"
board_file = "{TMP}/board.jsonl"
family_board_file = "{TMP}/family-board.jsonl"
members_file = "{TMP}/members.json"
settings_file = "{TMP}/portal-settings.json"
mesh_outbox = "{TMP}/mesh-outbox.jsonl"
features_data = "{TMP}/features-data"
ctl = "/nonexistent/prepperctl"
services = {{}}

[time]
latitude = {HOME[0]}
longitude = {HOME[1]}

[aircraft]
requests_per_min = 20
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        assert [f["id"] for f in server.FEATURES] == ["aircraft"]
        cls.mod = server.FEATURES[0]["mod"]
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]
        inv = cls.req("POST", "/api/admin/members", {"op": "invite", "name": "Ana"}, admin=cls.admin)[1]
        server._last_post.clear()
        cls.member = cls.req("POST", "/api/members/join", {"code": inv["code"], "name": "Ana"})[1]["key"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    n = 0

    @classmethod
    def req(cls, method, path, body=None, member=None, admin=None, ip=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if ip is None:
            cls.n += 1
            ip = f"10.2.{cls.n // 250}.{cls.n % 250}"
        h["X-Real-IP"] = ip
        if member:
            h["X-Member-Token"] = member
        if admin:
            h["X-Admin-Token"] = admin
        c.request(method, path, raw if raw is not None else (json.dumps(body) if body is not None else None), h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def setUp(self):
        shutil.rmtree(TMP / "run-sdr", ignore_errors=True)
        (TMP / "run-sdr" / "readsb").mkdir(parents=True)
        self.mod._cache.clear()
        self.mod._cmd_last.clear()
        C._pos_cache.clear()
        write_cfg()

    def aircraft_json(self, planes, now=None):
        (TMP / "run-sdr" / "readsb" / "aircraft.json").write_text(json.dumps({"now": now or time.time(), "aircraft": planes}))

    def running(self, **air):
        C.write_status({"weather_radio": "listening", "aircraft": dict({"state": "idle", "scan_minutes": 15}, **air),
                        "dongles": {"sdr2_state": "ok", "antenna": "adsb", "fallback": False}})

    def test_public_list_sorted_by_distance(self):
        self.aircraft_json([{"hex": "000001", "lat": 41.0, "lon": -105, "seen": 1},
                            {"hex": "000002", "lat": 39.8, "lon": -105, "seen": 1},
                            {"hex": "000003", "seen": 1},
                            {"hex": "000004", "lat": 39.9, "lon": -105, "seen": 200},          # not heard for 3 min
                            {"hex": "bad!"}])
        st, j = self.req("GET", "/api/aircraft")
        self.assertEqual(st, 200)
        self.assertEqual([a["hex"] for a in j["aircraft"]], ["000002", "000001", "000003"])
        self.assertFalse(j["stale"])
        self.assertEqual(j["origin"], {"lat": 39.74, "lon": -104.99, "source": "settings"})
        self.assertFalse(j["radio"]["running"])

    def test_stale_garbage_and_huge_files(self):
        self.aircraft_json([{"hex": "000001"}], now=time.time() - 120)
        st, j = self.req("GET", "/api/aircraft")
        self.assertEqual((j["stale"], j["aircraft"]), (True, []))
        f = TMP / "run-sdr" / "readsb" / "aircraft.json"
        for junk in ("{", "[]", '{"now": "x", "aircraft": 5}', '{"now": 1e300}', "null"):
            f.write_text(junk)
            self.mod._cache.clear()
            st, j = self.req("GET", "/api/aircraft")
            self.assertEqual((st, j["aircraft"]), (200, []), junk)
        f.write_text('{"now": %f, "aircraft": [' % time.time() + ",".join(['{"hex":"abcdef","flight":"X"}'] * 200000) + "]}")
        self.mod._cache.clear()
        st, j = self.req("GET", "/api/aircraft")
        self.assertEqual((st, j["aircraft"]), (200, []))                   # over 3 MB: ignored, not parsed
        self.aircraft_json([{"hex": f"{i:06x}"} for i in range(3000)])
        self.mod._cache.clear()
        self.assertEqual(len(self.req("GET", "/api/aircraft")[1]["aircraft"]), self.mod.MAX_AIRCRAFT)

    def test_rate_limited_per_address(self):
        codes = [self.req("GET", "/api/aircraft", ip="10.7.7.7")[0] for _ in range(25)]
        self.assertIn(429, codes)
        self.assertEqual(self.req("GET", "/api/aircraft", ip="10.7.7.8")[0], 200)

    def test_scan_needs_a_member_or_admin(self):
        self.running()
        self.assertEqual(self.req("POST", "/api/aircraft/scan", {})[0], 401)
        self.assertEqual(self.req("POST", "/api/aircraft/stop", {})[0], 401)
        st, j = self.req("POST", "/api/aircraft/scan", {}, member=self.member)
        self.assertEqual(st, 202)
        cmd = json.loads((TMP / "run-sdr" / "aircraft.cmd").read_text())
        self.assertEqual((cmd["action"], cmd["by"], cmd["id"]), ("scan", "Ana", j["id"]))
        self.assertEqual((TMP / "run-sdr" / "aircraft.cmd").stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.req("POST", "/api/aircraft/scan", {}, member=self.member)[0], 429)   # one moment…
        self.assertEqual(self.req("POST", "/api/aircraft/stop", {}, admin=self.admin)[0], 202)
        self.assertEqual(json.loads((TMP / "run-sdr" / "aircraft.cmd").read_text())["by"], "admin")

    def test_scan_input_validation(self):
        self.running()
        for bad in ({"minutes": 0}, {"minutes": 31}, {"minutes": "5"}, {"minutes": True}, {"minutes": 2.5}):
            self.mod._cmd_last.clear()
            self.assertEqual(self.req("POST", "/api/aircraft/scan", bad, member=self.member)[0], 400, bad)
        self.mod._cmd_last.clear()
        self.assertEqual(self.req("POST", "/api/aircraft/scan", {"minutes": 5, "by": "Mallory"}, member=self.member)[0], 202)
        cmd = json.loads((TMP / "run-sdr" / "aircraft.cmd").read_text())
        self.assertEqual((cmd["minutes"], cmd["by"]), (5, "Ana"))          # the name comes from the member key

    def test_scan_when_scheduler_is_down(self):
        st, j = self.req("POST", "/api/aircraft/scan", {}, member=self.member)
        self.assertEqual(st, 503)
        self.assertFalse((TMP / "run-sdr" / "aircraft.cmd").exists())

    def test_radio_state_and_pause_are_shown(self):
        C.write_status({"weather_radio": "paused", "by": "aircraft scan", "until": time.time() + 600,
                        "aircraft": {"state": "scanning", "device": "weather", "message": "<b>hi</b>" * 100},
                        "dongles": {"sdr2_state": "failed", "sdr2_why": "usb error", "antenna": "adsb", "fallback": True,
                                    "problem": None, "note": "x"}})
        st, j = self.req("GET", "/api/aircraft")
        r = j["radio"]
        self.assertEqual((r["weather_radio"], r["paused_by"], r["state"], r["sdr2_state"], r["fallback"]),
                         ("paused", "aircraft scan", "scanning", "failed", True))
        self.assertLessEqual(len(r["message"]), 300)
        self.assertIn("1090", r["antenna_text"])
        C.write_status({"weather_radio": "paused", "by": "x", "until": time.time() - 3600})
        self.mod._cache.clear()
        self.assertNotEqual(self.req("GET", "/api/aircraft")[1]["radio"]["weather_radio"], "paused")   # stale pause


class Scheduler(unittest.TestCase):
    """sdrd's rules and real jobs with a fake readsb."""

    def setUp(self):
        for d in ("run-sdr", "state", "sysfs"):
            shutil.rmtree(TMP / d, ignore_errors=True)
        for f in ("readsb.calls", "satdump.calls", "readsb.fail", "alerts.json"):
            (TMP / f).unlink(missing_ok=True)
        os.environ["PREPPER_SDR_DIR"] = str(TMP / "run-sdr")
        import importlib
        importlib.reload(sdrlease)
        sdrd.sdrlease = sdrlease
        setup_bins()
        (TMP / "alertd.ok").touch()
        C._pos_cache.clear()
        self.owner = Owner()
        self.d = None

    def tearDown(self):
        if self.d:
            for j in self.d.jobs.values():
                if j:
                    j.stop()
            for j in self.d.jobs.values():
                if j:
                    j.join(10)
        self.owner.close()

    def daemon(self, cfg="", serials=None):
        write_cfg(cfg)
        if serials is not None:
            sysfs(serials)
        self.d = sdrd.Daemon(borrow_wait=5)
        self.d.restart_backoff = 0.1
        self.d.refresh(force=True)
        return self.d

    def scan(self, minutes=None):
        cmd = {"action": "scan", "id": "req1", "by": "Ana"}
        if minutes:
            cmd["minutes"] = minutes
        C.atomic_write(C.run_dir() / "aircraft.cmd", json.dumps(cmd))
        self.d.handle_cmd()
        return self.d.air["message"]

    # rules
    def test_fallback_rules(self):
        d = self.daemon('[radio]\nsdr2 = "none"\n[aircraft]\nscans_per_day = 2\n')
        ok, msg, dev, secs = d.scan_decision(900)
        self.assertEqual((ok, dev, secs), (True, "weather", 900))
        self.assertIn("borrows the weather radio", msg)
        (TMP / "alerts.json").write_text(json.dumps([{"event": "Tornado Watch", "expires_ts": time.time() + 600}]))
        ok, msg, *_ = d.scan_decision(900)
        self.assertFalse(ok); self.assertIn("Tornado Watch", msg)
        (TMP / "alerts.json").unlink()
        d.ledger.lend_end(time.time() - 120)
        ok, msg, *_ = d.scan_decision(900)
        self.assertFalse(ok); self.assertIn("between loans", msg)
        d.ledger.lend_end(time.time() - 3600)
        d.ledger.add_scan(time.time()); d.ledger.add_scan(time.time())
        ok, msg, *_ = d.scan_decision(900)
        self.assertFalse(ok); self.assertIn("today's 2 scans", msg)

    def test_fallback_scan_is_shortened_before_a_planned_pass(self):
        d = self.daemon('[radio]\nsdr2 = "none"\n')
        now = time.time()
        d.plan = [{"decision": "record", "device": "weather", "start": now + 1500, "end": now + 2100, "name": "M"}]
        ok, msg, dev, secs = d.scan_decision(900)
        self.assertTrue(ok)
        self.assertAlmostEqual(secs, 900, delta=2)
        d.plan[0]["start"] = now + 1000                                   # 1000 - 600 gap = 400 s of room
        ok, msg, dev, secs = d.scan_decision(900)
        self.assertTrue(ok); self.assertAlmostEqual(secs, 400, delta=2)
        d.plan[0]["start"] = now + 800
        ok, msg, *_ = d.scan_decision(900)
        self.assertFalse(ok); self.assertIn("pass is planned", msg)

    def test_switches_and_missing_programs(self):
        d = self.daemon('[radio]\nsdr2 = "none"\nsdr_lend = false\n')
        self.assertIn("not to lend", d.scan_decision(900)[1])
        d = self.daemon('[radio]\nsdr2 = "none"\n[features]\naircraft = false\n')
        self.assertIn("turned off", d.scan_decision(900)[1])
        d = self.daemon('[radio]\nsdr2 = "none"\n[aircraft]\nreadsb = "/nonexistent/readsb"\n')
        self.assertIn("isn't installed", d.scan_decision(900)[1])

    def test_sdr2_with_satellite_antenna_scans_on_demand_without_touching_weather(self):
        d = self.daemon('[radio]\nsdr2_antenna = "satellite"\n', ["BADGER-SDR1", "BADGER-SDR2"])
        ok, msg, dev, secs = d.scan_decision(900)
        self.assertEqual((ok, dev), (True, "sdr2"))
        d.ledger.add_scan(time.time()); d.ledger.add_scan(time.time()); d.ledger.add_scan(time.time()); d.ledger.add_scan(time.time())
        (TMP / "alerts.json").write_text(json.dumps([{"event": "Tornado Watch", "expires_ts": time.time() + 600}]))
        self.assertTrue(d.scan_decision(900)[0])                            # no fallback limits on SDR 2

    def test_sdr2_with_aircraft_whip_is_always_on(self):
        d = self.daemon("", ["BADGER-SDR2", "BADGER-SDR1"])
        self.assertIn("all the time", d.scan_decision(900)[1])
        d.manage_always()
        job = d.jobs["sdr2"]
        self.assertTrue(job and job.always)
        self.assertTrue(wait_for(lambda: (C.run_dir() / "readsb" / "aircraft.json").exists()))
        argv = calls("readsb")[0]
        self.assertEqual(argv[argv.index("--device") + 1], "BADGER-SDR2")
        self.assertEqual(argv[argv.index("--lat") + 1], "39.7400")
        self.assertEqual(self.owner.log.count("yield"), 0)                 # the weather radio is untouched
        d.write_status()
        st = C.read_status()
        self.assertEqual((st["aircraft"]["state"], st["weather_radio"], st["dongles"]["sdr2_state"]),
                         ("always_on", "listening", "ok"))
        self.assertIn("Already watching", self.scan())

    def test_always_on_steps_aside_for_a_pass(self):
        d = self.daemon('[radio]\nsdr2_antenna = "both"\n', ["BADGER-SDR1", "BADGER-SDR2"])
        self.assertEqual(d.mode["device"], "sdr2")
        d.manage_always()
        self.assertTrue(wait_for(lambda: len(calls("readsb")) == 1))
        now = time.time()
        p = {"decision": "record", "device": "sdr2", "start": now + 5, "end": now + 100, "name": "METEOR-M2 3",
             "norad": 57166, "tca": now + 50, "aos": now, "los": now + 110, "max_elev": 50, "freq_mhz": 137.9,
             "pipeline": "meteor_m2-x_lrpt", "reason": ""}
        d.plan = [p]
        d.manage_always()                                                  # < 90 s before the pass: stop
        self.assertTrue(wait_for(lambda: not d.jobs["sdr2"].is_alive()))
        d.jobs["sdr2"] = None
        d.manage_always()                                                  # not restarted with a pass this close
        self.assertIsNone(d.jobs["sdr2"])
        d.start_due()                                                      # the pass starts on SDR 2
        self.assertEqual(d.jobs["sdr2"].kind, "satellite")
        self.assertTrue(wait_for(lambda: len(calls("satdump")) == 1))
        argv = calls("satdump")[0]
        self.assertEqual(argv[argv.index("--source_id") + 1], "BADGER-SDR2")
        self.assertEqual(self.owner.log.count("yield"), 0)

    def test_fallback_scan_lends_and_returns_the_weather_radio(self):
        d = self.daemon('[radio]\nsdr2 = "none"\n')
        msg = self.scan(minutes=1)
        self.assertIn("Weather alerts are paused", msg)
        job = d.jobs["weather"]
        self.assertTrue(wait_for(lambda: (C.run_dir() / "readsb" / "aircraft.json").exists()))
        d.write_status()
        st = C.read_status()
        self.assertEqual((st["weather_radio"], st["by"]), ("paused", "aircraft scan"))
        self.assertEqual(st["aircraft"]["state"], "scanning")
        self.assertIn("yield", self.owner.log)
        C.atomic_write(C.run_dir() / "aircraft.cmd", json.dumps({"action": "stop", "id": "req2"}))
        d.handle_cmd()
        job.join(10)
        self.assertFalse(job.is_alive())
        d.tick()
        st = C.read_status()
        self.assertEqual(st["weather_radio"], "listening")
        self.assertFalse((C.run_dir() / "readsb" / "aircraft.json").exists())
        self.assertTrue(wait_for(lambda: self.owner.log.count("hold") == 2, 3))
        self.assertEqual(d.ledger.day()["scans"], 1)
        self.assertIsNotNone(d.ledger.last_lend_end)
        self.assertIn("between loans", self.scan())                       # no second loan straight away

    def test_bad_commands_are_ignored(self):
        d = self.daemon('[radio]\nsdr2 = "none"\n')
        for junk in ("[1]", "{", '{"action": "rm"}', '{"action": "scan", "minutes": 999999, "id": "../x", "by": "\\u0000x"}'):
            (C.run_dir()).mkdir(parents=True, exist_ok=True)
            (C.run_dir() / "aircraft.cmd").write_text(junk)
            d.handle_cmd()
            self.assertFalse((C.run_dir() / "aircraft.cmd").exists())
        job = d.jobs["weather"]
        self.assertTrue(job)
        self.assertLessEqual(job.seconds, 15 * 60)                         # minutes capped
        self.assertIsNone(d.air["request"])                               # a bad id is not echoed

    def test_failing_sdr2_falls_back_to_the_weather_radio(self):
        (TMP / "readsb.fail").touch()
        d = self.daemon("", ["BADGER-SDR1", "BADGER-SDR2"])
        d.manage_always()
        self.assertTrue(wait_for(lambda: d.jobs["sdr2"] and not d.jobs["sdr2"].is_alive(), 10))
        self.assertGreaterEqual(len(calls("readsb")), 3)
        self.assertIsNone(d.sdr2_ok())
        d.tick()
        st = C.read_status()
        self.assertEqual((st["dongles"]["sdr2_state"], st["dongles"]["fallback"]), ("failed", True))
        self.assertIn("usb_claim_interface", st["dongles"]["sdr2_why"])
        (TMP / "readsb.fail").unlink()
        ok, msg, dev, _ = d.scan_decision(900)
        self.assertEqual((ok, dev), (True, "weather"))                     # degraded, but aircraft still possible
        self.assertTrue(any(h["kind"] == "sdr2" and h["result"] == "failed" for h in d.ledger.history()))
        d.sdr2_failed_until = d.next_always = 0                            # 30 min later: SDR 2 is tried again
        d.manage_always()
        self.assertTrue(d.jobs["sdr2"].always)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
