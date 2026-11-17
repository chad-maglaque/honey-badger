"""
Sharing RTL-SDR dongles: which dongle is which (serial numbers), alertd pinning itself to its own
dongle, the lending rules (gap between loans, daily cap, alerts), and a real alertd process loop
with fake rtl_fm / multimon-ng that lends its dongle and listens again promptly.
Run: python3.11 tests/test_sdr_sharing.py
"""
from __future__ import annotations

import importlib
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
TMP = Path(tempfile.mkdtemp(prefix="sdrshare-"))
os.environ["PREPPER_SDR_DIR"] = str(TMP / "run-sdr")
os.environ["PREPPER_SDR_STATE"] = str(TMP / "state")
os.environ["ALERTS_FILE"] = str(TMP / "alerts.json")
os.environ["NOAA_TAP_REQUEST"] = str(TMP / "tap.request")
os.environ["NOAA_TAP_DIR"] = str(TMP / "tap")
os.environ["PREPPER_USB_SYSFS"] = str(TMP / "no-sysfs")
for sub in ("common", "alertd", "sdr"):
    sys.path.insert(0, str(REPO / "software" / sub))

import alertd  # noqa: E402
import dongles  # noqa: E402
import sdrlease  # noqa: E402


def fake_sysfs(root: Path, devs: list[tuple[str, str, str]]) -> Path:
    """devs: (port, vid:pid, serial)."""
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    for port, ids, serial in devs:
        d = root / port
        d.mkdir()
        vid, pid = ids.split(":")
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        if serial is not None:
            (d / "serial").write_text(serial + "\n")
        (d / "product").write_text("RTL2838UHIDIR\n")
        iface = root / f"{port}:1.0"            # interface dirs must be ignored
        iface.mkdir()
        (iface / "idVendor").write_text(vid)
    return root


class Dongles(unittest.TestCase):
    """Every Badger has two identical dongles: SDR 1 (weather) and SDR 2 (aircraft/satellites)."""

    def setUp(self):
        self.root = TMP / "sysfs"

    def two(self, a="BADGER-SDR1", b="BADGER-SDR2"):
        return fake_sysfs(self.root, [("1-1.1", "0bda:2838", a), ("1-1.2", "0bda:2838", b), ("1-1.3", "1546:01a8", "GPS")])

    def roles(self, cfg=None):
        a = dongles.assign(cfg or {}, root=self.root)
        return a, a["weather"]["arg"], (a["sdr2"] or {}).get("arg"), (a["sdr2"] or {}).get("present")

    def test_list_ignores_interfaces_and_other_devices(self):
        self.two()
        ds = dongles.list_dongles(self.root)
        self.assertEqual([d["serial"] for d in ds], ["BADGER-SDR1", "BADGER-SDR2"])
        self.assertIsNone(dongles.list_dongles(TMP / "nope"))
        fake_sysfs(self.root, [("1-1", "0bda:2832", None)])
        self.assertEqual(dongles.list_dongles(self.root)[0]["serial"], "")       # no serial file

    def test_hub_port_order_is_numeric(self):
        fake_sysfs(self.root, [("1-1.10", "0bda:2838", "B"), ("1-1.2", "0bda:2838", "A")])
        self.assertEqual([d["serial"] for d in dongles.list_dongles(self.root)], ["A", "B"])

    def test_build_time_serials_win_whatever_the_usb_order(self):
        self.two("BADGER-SDR2", "BADGER-SDR1")
        a, w, s2, ok = self.roles()
        self.assertEqual((w, s2, ok, a["problem"], a["note"]), ("BADGER-SDR1", "BADGER-SDR2", True, None, None))

    def test_unset_distinct_serials_guess_by_port_with_a_warning(self):
        self.two("12345678", "ABCDEFGH")
        a, w, s2, ok = self.roles()
        self.assertEqual((w, s2, ok), ("12345678", "ABCDEFGH", True))
        self.assertIn("guessed", a["note"])

    def test_factory_duplicate_serials_fall_back_to_device_index(self):
        self.two("00000001", "00000001")
        a, w, s2, ok = self.roles()
        self.assertEqual((w, s2, ok), ("0", "1", True))                   # alertd takes the first, SDR 2 the other
        self.assertIn("same serial", a["note"])
        self.assertIsNone(a["problem"])

    def test_single_dongle_is_the_weather_radio_whichever_it_is(self):
        for serial in ("BADGER-SDR1", "BADGER-SDR2", "00000001"):
            fake_sysfs(self.root, [("1-1", "0bda:2838", serial)])
            a, w, s2, ok = self.roles()
            self.assertTrue(a["weather"]["present"], serial)
            self.assertFalse(ok, serial)                                    # → the borrowing fallback
        fake_sysfs(self.root, [("1-1", "0bda:2838", "BADGER-SDR2")])
        self.assertIn("Only one radio", dongles.assign({}, root=self.root)["note"])

    def test_no_dongle(self):
        fake_sysfs(self.root, [])
        a, w, s2, ok = self.roles()
        self.assertFalse(a["weather"]["present"])
        self.assertFalse(ok)

    def test_no_sysfs_means_unknown_not_missing(self):
        a = dongles.assign({}, root=TMP / "nope")
        self.assertIsNone(a["weather"]["present"])
        self.assertEqual((a["sdr2"]["arg"], a["sdr2"]["present"]), ("BADGER-SDR2", None))
        a = dongles.assign({"weather_sdr": "X1", "sdr2": "none"}, root=TMP / "nope")
        self.assertEqual((a["weather"]["arg"], a["sdr2"]), ("X1", None))

    def test_explicit_weather_serial_is_strict(self):
        fake_sysfs(self.root, [("1-1", "0bda:2838", "BADGER-SDR2")])
        a = dongles.assign({"weather_sdr": "BADGER-SDR1"}, root=self.root)
        self.assertFalse(a["weather"]["present"])                         # alertd must not take SDR 2 when pinned
        self.assertTrue(a["sdr2"]["present"])

    def test_explicit_serials_and_auto_partner(self):
        self.two("WX", "AIR")
        a, w, s2, ok = self.roles({"weather_sdr": "WX"})
        self.assertEqual((w, s2, ok), ("WX", "AIR", True))
        self.assertIn("SDR 2", a["note"])
        a, w, s2, ok = self.roles({"sdr2": "AIR"})
        self.assertEqual((w, s2), ("WX", "AIR"))

    def test_sdr2_none(self):
        self.two()
        a, w, s2, ok = self.roles({"sdr2": "none"})
        self.assertEqual(w, "BADGER-SDR1")
        self.assertIsNone(a["sdr2"])

    def test_same_dongle_for_both_roles(self):
        self.two()
        a = dongles.assign({"weather_sdr": "BADGER-SDR1", "sdr2": "BADGER-SDR1"}, root=self.root)
        self.assertIn("same dongle", a["problem"])
        self.assertFalse(a["sdr2"]["present"])

    def test_duplicate_pinned_serial(self):
        self.two("DUP", "DUP")
        a = dongles.assign({"weather_sdr": "DUP"}, root=self.root)
        self.assertIn("share the serial", a["problem"])
        self.assertFalse(a["weather"]["present"])

    def test_index_config(self):
        self.two("00000001", "00000001")
        a, w, s2, ok = self.roles({"weather_sdr": 1, "sdr2": 0})
        self.assertEqual((w, s2, ok), ("1", "0", True))
        a = dongles.assign({"weather_sdr": 5}, root=self.root)
        self.assertFalse(a["weather"]["present"])

    def test_three_unnamed_dongles_is_a_problem(self):
        fake_sysfs(self.root, [("1-1.1", "0bda:2838", "A"), ("1-1.2", "0bda:2838", "B"), ("1-1.3", "0bda:2838", "C")])
        a, w, s2, ok = self.roles()
        self.assertTrue(a["problem"])
        self.assertFalse(ok)

    def test_junk_config_values(self):
        self.two()
        for v in (True, -3, None, "", "auto", 3.5):
            a = dongles.assign({"weather_sdr": v, "sdr2": v}, root=self.root)
            self.assertEqual(a["weather"]["arg"], "BADGER-SDR1", v)


class LendRules(unittest.TestCase):
    def setUp(self):
        self.now = [100000.0]
        self.path = TMP / f"lends-{self._testMethodName}.json"
        self.log = alertd.LendLog(self.path, clock=lambda: self.now[0])

    def test_two_argument_form_unchanged(self):
        self.assertIsNone(alertd.lending_allowed({}, {}))

    def test_gap_between_loans(self):
        self.log.start(); self.now[0] += 900; self.log.end()
        self.now[0] += 60
        why = alertd.lending_allowed({}, {}, self.log)
        self.assertIn("between loans", why)
        self.assertIn("try again in 9 min", why)
        self.now[0] += 540
        self.assertIsNone(alertd.lending_allowed({}, {}, self.log))
        self.now[0] -= 300
        self.assertIsNone(alertd.lending_allowed({}, {"sdr_lend_gap_min": 0}, self.log))
        self.assertIn("between loans", alertd.lending_allowed({}, {"sdr_lend_gap_min": 30}, self.log))

    def test_daily_cap(self):
        for _ in range(9):                               # 9 × 15 min = 135 min
            self.log.start(); self.now[0] += 900; self.log.end(); self.now[0] += 1200
        self.assertIn("limit", alertd.lending_allowed({}, {}, self.log))
        self.assertIsNone(alertd.lending_allowed({}, {"sdr_lend_max_min_per_day": 300}, self.log))
        self.now[0] += 86400
        self.assertIsNone(alertd.lending_allowed({}, {}, self.log))

    def test_open_loan_counts_and_survives_restart(self):
        self.log.start(); self.now[0] += 600
        self.assertAlmostEqual(self.log.used(), 600)
        again = alertd.LendLog(self.path, clock=lambda: self.now[0])
        self.assertAlmostEqual(again.used(), 600)
        again.end()
        self.assertEqual(again.last_end(), self.now[0])

    def test_corrupt_log_is_ignored(self):
        self.path.write_text('{"loans": [["x", 1]]}')
        self.assertEqual(alertd.LendLog(self.path).loans, [])
        self.path.write_text("[[[")
        self.assertEqual(alertd.LendLog(self.path).loans, [])

    def test_alert_and_switch_still_win(self):
        self.assertIn("sdr_lend", alertd.lending_allowed({}, {"sdr_lend": False}, self.log))
        live = {"a": {"expires_ts": time.time() + 600, "event": "Flash Flood Watch"}}
        self.assertIn("Flash Flood Watch", alertd.lending_allowed(live, {}, self.log))

    def test_rtl_fm_argv(self):
        self.assertNotIn("-d", alertd.rtl_fm_argv(162.55))
        argv = alertd.rtl_fm_argv(162.4, "00000162")
        self.assertEqual(argv[argv.index("-d") + 1], "00000162")
        self.assertEqual(argv[-1], "-")


FAKE_RTL = '''#!{py}
import os, sys, time, json
d = {d!r}
with open(os.path.join(d, "rtl_fm.calls"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
out = sys.stdout.buffer
while not os.path.exists(os.path.join(d, "unplug")):
    try:
        out.write(b"\\x00\\x00" * 2048); out.flush()
    except BrokenPipeError:
        sys.exit(0)
    time.sleep(0.05)
sys.exit(1)
'''
FAKE_MM = '''#!{py}
import os, sys, time
d = {d!r}
said = False
for chunk in iter(lambda: sys.stdin.buffer.read(4096), b""):
    # an alert "decoded" from the audio — only once audio bytes have really arrived
    if not said and os.path.exists(os.path.join(d, "say")):
        print(open(os.path.join(d, "say")).read().strip(), flush=True)
        said = True
'''


class AlertdLoop(unittest.TestCase):
    """alertd.main() for real, with fake radio programs: lend, resume within a second or two, refuse a
    second loan straight away, and pick its own dongle with -d."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="alertd-"))
        bind = self.d / "bin"
        bind.mkdir()
        for name, src in (("rtl_fm", FAKE_RTL), ("multimon-ng", FAKE_MM)):
            p = bind / name
            p.write_text(src.format(py=sys.executable, d=str(self.d)))
            p.chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bind}:{self.old_path}"
        self.sdr = self.d / "sdr"
        os.environ["PREPPER_SDR_DIR"] = str(self.sdr)
        importlib.reload(sdrlease)
        alertd.sdrlease = sdrlease
        self.cfg = self.d / "prepper.toml"
        os.environ["PREPPER_CONFIG"] = str(self.cfg)

    def tearDown(self):
        (self.d / "unplug").touch()
        os.environ["PATH"] = self.old_path
        os.environ.pop("PREPPER_CONFIG", None)
        os.environ["PREPPER_SDR_DIR"] = str(TMP / "run-sdr")
        if hasattr(self, "t"):
            self.t.join(10)
        shutil.rmtree(self.d, ignore_errors=True)

    def calls(self):
        try:
            return [json.loads(x) for x in (self.d / "rtl_fm.calls").read_text().splitlines()]
        except OSError:
            return []

    def wait_for(self, cond, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def start(self):
        self.rc = []
        self.t = threading.Thread(target=lambda: self.rc.append(alertd.main()), daemon=True)
        self.t.start()

    def test_lend_resume_and_gap(self):
        self.cfg.write_text('[radio]\nweather_freq_mhz = 162.475\nsdr_lend_gap_min = 10\n')
        self.start()
        self.assertTrue(self.wait_for(lambda: len(self.calls()) == 1))
        self.assertIn("162.475M", self.calls()[0])
        time.sleep(0.5)
        with sdrlease.borrow("weather", owner="satellite pass", seconds=60, wait=10, poll=0.05):
            self.assertFalse(sdrlease.holder_free("weather"))
            time.sleep(0.3)
            self.assertEqual(len(self.calls()), 1)       # alertd is not listening while lent
        t_back = time.monotonic()
        self.assertTrue(self.wait_for(lambda: len(self.calls()) == 2, 5))
        self.assertLess(time.monotonic() - t_back, 3.0, "alertd should listen again promptly")
        lends = json.loads((self.sdr / "weather-lends.json").read_text())["loans"]
        self.assertEqual(len(lends), 1)
        self.assertIsNotNone(lends[0][1])
        time.sleep(0.5)
        with self.assertRaises(sdrlease.Busy) as e, sdrlease.borrow("weather", owner="aircraft scan", seconds=60, wait=8, poll=0.05):
            pass
        self.assertIn("between loans", str(e.exception))

    def test_decodes_an_alert_from_the_audio_and_then_refuses_to_lend(self):
        now = time.gmtime()
        iss = f"{now.tm_yday:03d}{now.tm_hour:02d}{now.tm_min:02d}"
        (self.d / "say").write_text(f"EAS: ZCZC-WXR-TOR-008031+0100-{iss}-KBOU/NWS-")
        self.cfg.write_text('[radio]\nsame_fips = ["008031"]\n')
        self.start()
        out = Path(os.environ["ALERTS_FILE"])
        self.assertTrue(self.wait_for(lambda: out.exists() and "Tornado Warning" in out.read_text()))
        with self.assertRaises(sdrlease.Busy) as e, sdrlease.borrow("weather", owner="satellite pass", seconds=60, wait=8, poll=0.05):
            pass
        self.assertIn("Tornado Warning", str(e.exception))
        out.unlink()

    def test_pins_its_own_dongle(self):
        root = fake_sysfs(self.d / "sysfs", [("1-1.1", "0bda:2838", "BADGER-SDR2"), ("1-1.2", "0bda:2838", "BADGER-SDR1")])
        dongles.SYSFS = root
        try:
            self.cfg.write_text("[radio]\n")                              # automatic: by the build-time serials
            self.start()
            self.assertTrue(self.wait_for(lambda: len(self.calls()) == 1))
            argv = self.calls()[0]
            self.assertEqual(argv[argv.index("-d") + 1], "BADGER-SDR1")
        finally:
            dongles.SYSFS = TMP / "no-sysfs"

    def test_does_not_take_the_second_dongle(self):
        root = fake_sysfs(self.d / "sysfs", [("1-1.1", "0bda:2838", "BADGER-SDR2")])
        dongles.SYSFS = root
        try:
            self.cfg.write_text('[radio]\nweather_sdr = "BADGER-SDR1"\n')      # pinned: strict
            self.start()
            self.t.join(5)
            self.assertEqual(self.rc, [0])
            self.assertEqual(self.calls(), [])
        finally:
            dongles.SYSFS = TMP / "no-sysfs"


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
