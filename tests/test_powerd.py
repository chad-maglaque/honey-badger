#!/usr/bin/env python3
"""
Tests for software/powerd/powerd.py with fake I²C devices and a fake pinctrl.
   python3.11 tests/test_powerd.py -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "powerd"))
import powerd  # noqa: E402


class FakeI2C:
    """Registers → raw bytes, big-endian like the chips."""

    def __init__(self, regs: dict[int, bytes], fail: bool = False):
        self.regs, self.fail, self.closed = regs, fail, False

    def read(self, reg: int, n: int) -> bytes:
        if self.fail:
            raise OSError("no ACK")
        data = self.regs[reg]
        assert len(data) == n, (reg, n)
        return data

    def read16(self, reg: int) -> int:
        return int.from_bytes(self.read(reg, 2), "big")

    def close(self):
        self.closed = True


def u16(v: int) -> bytes:
    return (v & 0xFFFF).to_bytes(2, "big")


def u24(v: int) -> bytes:
    return (v & 0xFFFFFF).to_bytes(3, "big")


class Runner:
    """Stands in for subprocess.run in CaseFan: records pinctrl calls."""

    def __init__(self, rc: int = 0, exc: Exception | None = None):
        self.calls, self.rc, self.exc = [], rc, exc

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.rc, "", "")


class Fan(unittest.TestCase):
    def fan(self, **cfg):
        self.run = Runner()
        return powerd.CaseFan(cfg, runner=self.run)

    def test_hysteresis_balanced(self):
        f = self.fan()
        seq = [(40, False), (50, False), (54.9, False), (55, True), (50, True), (47.1, True), (47, False),
               (52, False), (56, True)]
        for t, want in seq:
            with self.subTest(t=t):
                self.assertEqual(f.update(t, "balanced"), want)
        self.assertEqual(self.run.calls, [["pinctrl", "set", "18", "op", "dl"], ["pinctrl", "set", "18", "op", "dh"],
                                          ["pinctrl", "set", "18", "op", "dl"], ["pinctrl", "set", "18", "op", "dh"]],
                         "the pin is driven only on a change")

    def test_endurance_plus_7(self):
        f = self.fan()
        self.assertFalse(f.update(60, "endurance"))      # below 62
        self.assertTrue(f.update(62, "endurance"))
        self.assertTrue(f.update(55, "endurance"))       # still above 54
        self.assertFalse(f.update(54, "endurance"))

    def test_full_always_on(self):
        f = self.fan()
        for t in (20, 40, 60):
            self.assertTrue(f.update(t, "full"))

    def test_startup_in_band_is_off(self):
        f = self.fan()
        self.assertFalse(f.update(50, "balanced"))       # known state from the first reading

    def test_unreadable_temperature_runs_fan(self):
        f = self.fan()
        self.assertTrue(f.update(None, "balanced"))

    def test_pinctrl_failure_retried(self):
        f = powerd.CaseFan({}, runner=Runner(rc=1))
        self.assertIsNone(f.update(60, "balanced"))       # the pin did not take: state unknown
        f.runner = Runner(exc=subprocess.TimeoutExpired("pinctrl", 5))
        self.assertIsNone(f.update(60, "balanced"))       # no crash
        f.runner = Runner()
        self.assertTrue(f.update(60, "balanced"))
        self.assertEqual(len(f.runner.calls), 1)

    def test_custom_and_swapped_thresholds(self):
        f = powerd.CaseFan({"fan_on_c": 40, "fan_off_c": 60, "case_fan_pin": 12}, runner=Runner())
        self.assertEqual((f.on_c, f.off_c), (60, 40))
        self.assertTrue(f.update(61, "balanced"))
        self.assertEqual(f.runner.calls[0][2], "12")


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, True))

    def policy(self, **cfg):
        return powerd.Policy(cfg, state=self.state)

    def test_auto_endurance_at_30(self):
        p = self.policy()
        self.assertIsNone(p.step(31, False, "balanced"))
        self.assertEqual(p.step(30, False, "balanced"), "endurance")
        self.assertIsNone(p.step(29, False, "endurance"))
        self.assertIsNone(p.step(28, False, "balanced"), "a user who switches back is not overruled")
        self.assertEqual(p.step(28, True, "endurance"), "restore")
        self.assertIsNone(p.step(90, True, "balanced"))

    def test_already_endurance_is_not_restored(self):
        p = self.policy()
        self.assertIsNone(p.step(20, False, "endurance"))
        self.assertIsNone(p.step(20, True, "endurance"))

    def test_shutdown_at_6_after_three_reads(self):
        p = self.policy()
        p.step(10, False, "balanced")
        self.assertIsNone(p.step(6, False, "endurance"))
        self.assertIsNone(p.step(5.5, False, "endurance"))
        self.assertEqual(p.step(5, False, "endurance"), "poweroff")

    def test_glitch_resets_low_count(self):
        p = self.policy()
        p.step(5, False, "endurance"); p.step(5, False, "endurance")
        p.step(7, False, "endurance")                       # one good reading
        self.assertIsNone(p.step(5, False, "endurance"))
        p.step(5, False, "endurance")
        p.step(5, None, "endurance")                        # unknown source resets too
        self.assertIsNone(p.step(5, False, "endurance"))

    def test_unknown_power_never_acts(self):
        p = self.policy()
        for _ in range(10):
            self.assertIsNone(p.step(1, None, "balanced"))
            self.assertIsNone(p.step(None, False, "balanced"))
            self.assertIsNone(p.step(None, None, "balanced"))

    def test_external_power_never_shuts_down(self):
        p = self.policy()
        for _ in range(5):
            self.assertIsNone(p.step(1, True, "balanced"))

    def test_custom_thresholds(self):
        p = self.policy(auto_endurance_percent=50, shutdown_percent=10)
        self.assertEqual(p.step(50, False, "full"), "endurance")
        for _ in range(2):
            p.step(10, False, "endurance")
        self.assertEqual(p.step(10, False, "endurance"), "poweroff")

    def test_auto_flag_survives_restart(self):
        (self.state / "power-mode-auto").write_text("endurance\n")      # left by prepperctl power-mode --auto
        p = self.policy()
        self.assertTrue(p.auto_switched)
        self.assertEqual(p.step(40, True, "endurance"), "restore")


class Meters(unittest.TestCase):
    def mon(self, chip, regs, shunt=0.1):
        return powerd.InputMonitor({"input_monitor": chip, "input_shunt_ohms": shunt}, i2c=FakeI2C(regs))

    def test_ina228(self):
        # VBUS 24.0 V: 24 / 195.3125e-6 = 122880 → bits 23..4; VSHUNT 1.5 mV at 0.015 Ω = 0.1 A.
        vbus = 122880 << 4
        vshunt = round(1.5e-3 / 312.5e-9) << 4
        r = self.mon("ina228", {0x05: u24(vbus), 0x04: u24(vshunt)}, shunt=0.015).read()
        self.assertEqual(r["input_v"], 24.0)
        self.assertAlmostEqual(r["input_a"], 0.1, places=3)
        self.assertAlmostEqual(r["input_w"], 2.4, places=1)

    def test_ina228_negative_shunt(self):
        neg = (-round(1e-3 / 312.5e-9)) & 0xFFFFF                       # 20-bit two's complement
        m = self.mon("ina228", {0x05: u24(61440 << 4), 0x04: u24(neg << 4)})
        vshunt, vbus = m.volts()
        self.assertAlmostEqual(vshunt, -1e-3, places=9)
        self.assertEqual(m.read()["input_a"], 0.0, "reverse current is not input power")
        self.assertEqual(round(vbus, 2), 12.0)

    def test_ina226(self):
        r = self.mon("ina226", {0x02: u16(round(18.0 / 1.25e-3)), 0x01: u16(round(5e-3 / 2.5e-6))}).read()
        self.assertEqual(r["input_v"], 18.0)
        self.assertAlmostEqual(r["input_a"], 0.05, places=3)
        m = self.mon("ina226", {0x02: u16(1000), 0x01: u16(-400)})
        self.assertAlmostEqual(m.volts()[0], -1e-3)
        self.assertEqual(m.read()["input_a"], 0.0)

    def test_ina219(self):
        bus = (round(12.0 / 4e-3) << 3) | 0b010                         # low bits are flags
        r = self.mon("ina219", {0x02: u16(bus), 0x01: u16(round(10e-3 / 10e-6))}).read()
        self.assertEqual(r["input_v"], 12.0)
        self.assertAlmostEqual(r["input_a"], 0.1, places=3)
        m = self.mon("ina219", {0x02: u16(bus), 0x01: u16(-1000)})
        self.assertAlmostEqual(m.volts()[0], -10e-3)

    def test_bad_config(self):
        with self.assertRaises(ValueError):
            powerd.InputMonitor({"input_monitor": "ina999"}, i2c=FakeI2C({}))
        with self.assertRaises(ValueError):
            powerd.InputMonitor({"input_monitor": "ina228", "input_shunt_ohms": 0}, i2c=FakeI2C({}))

    def test_signed(self):
        self.assertEqual(powerd.signed(0xFFFF, 16), -1)
        self.assertEqual(powerd.signed(0x7FFF, 16), 32767)
        self.assertEqual(powerd.signed(0x80000, 20), -(1 << 19))


class Gauges(unittest.TestCase):
    def test_x120x_max17048(self):
        pin = powerd.GpioPin(6, "echo 1")
        g = powerd.X120x({}, i2c=FakeI2C({0x08: u16(0x0012), 0x02: u16(round(3.9 / 78.125e-6)), 0x04: u16(87 << 8 | 128)}))
        g.pld = pin
        r = g.read()
        self.assertAlmostEqual(r["voltage"], 3.9, places=3)
        self.assertEqual(r["percent"], 87.5)
        self.assertTrue(r["external"])

    def test_x120x_max17043_and_unknown_pld(self):
        g = powerd.X120x({}, i2c=FakeI2C({0x08: u16(0x0003), 0x02: u16(round(3.7 / 1.25e-3) << 4), 0x04: u16(50 << 8)}))
        g.pld = powerd.GpioPin(6, "false")                 # gpioget fails → unknown, never "battery"
        r = g.read()
        self.assertAlmostEqual(r["voltage"], 3.7, places=3)
        self.assertIsNone(r["external"])

    def test_x120x_missing_hat_closes_fd(self):
        dev = FakeI2C({}, fail=True)
        with self.assertRaises(OSError):
            powerd.X120x({}, i2c=dev)
        self.assertTrue(dev.closed)

    def test_ina219_ups(self):
        g = powerd.INA219({}, i2c=FakeI2C({0x02: u16(round(7.6 / 0.004) << 3), 0x01: u16(-500)}))
        r = g.read()
        self.assertEqual(r["voltage"], 7.6)
        self.assertEqual(r["current_ma"], -50)
        self.assertFalse(r["external"])

    def test_gpio_parse(self):
        self.assertEqual(powerd.GpioPin.parse('"GPIO6"=active\n1'), 1)
        self.assertEqual(powerd.GpioPin.parse("0"), 0)
        self.assertIsNone(powerd.GpioPin.parse(""))
        self.assertIsNone(powerd.GpioPin.parse(None))
        self.assertIsNone(powerd.GpioPin.parse("inactive"))


class ReadAll(unittest.TestCase):
    def test_meter_proves_external_power(self):
        gauge = powerd.Retrying("g", lambda: type("G", (), {"read": lambda self: {"percent": 50.0, "external": None}})())
        meter = powerd.Retrying("m", lambda: powerd.InputMonitor(
            {"input_monitor": "ina226"}, i2c=FakeI2C({0x02: u16(round(19 / 1.25e-3)), 0x01: u16(0)})))
        self.assertTrue(powerd.read_all(gauge, meter)["external"])

    def test_meter_absence_proves_nothing(self):
        gauge = powerd.Retrying("g", lambda: type("G", (), {"read": lambda self: {"percent": 50.0, "external": None}})())
        meter = powerd.Retrying("m", lambda: powerd.InputMonitor(
            {"input_monitor": "ina226"}, i2c=FakeI2C({0x02: u16(0), 0x01: u16(0)})))
        self.assertIsNone(powerd.read_all(gauge, meter)["external"])

    def test_missing_gauge_reports_unknown(self):
        def boom():
            raise OSError("no /dev/i2c-1")
        g = powerd.Retrying("g", boom)
        r = powerd.read_all(g, None)
        self.assertIsNone(r["external"])
        self.assertIsNone(r["percent"])


class WriteJson(unittest.TestCase):
    def test_atomic_and_symlink_safe(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            victim = d / "victim"
            victim.write_text("keep")
            (d / ".power.json.tmp").symlink_to(victim)     # the old fixed temp name, planted
            out = d / "power.json"
            powerd.write_json(out, {"percent": 50})
            self.assertEqual(json.loads(out.read_text()), {"percent": 50})
            self.assertEqual(victim.read_text(), "keep")
            self.assertEqual(out.stat().st_mode & 0o777, 0o644)
            self.assertEqual(sorted(p.name for p in d.iterdir()), [".power.json.tmp", "power.json", "victim"])


if __name__ == "__main__":
    unittest.main()
