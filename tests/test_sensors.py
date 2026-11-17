"""
Sensor station: drivers (fake I²C devices, fake serial ports on a pty, a fake 1-Wire sysfs tree,
a fake gpioget), BME280 compensation against the datasheet's worked example, the reading store
and its downsampling, alert rules (hysteresis, confirmation, cooldown), hostile Meshtastic
telemetry, sensord end to end (current.json, events, mesh outbox and its rate limit), the portal
feature over HTTP (access levels, validation, board posting) and the install fragment.
Run: python3.11 tests/test_sensors.py
"""
from __future__ import annotations

import http.client
import json
import os
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "sensors"))
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "tests"))
os.environ["PORTAL_QUIET"] = "1"

import drivers  # noqa: E402
import sensord  # noqa: E402
import sensorlib as sl  # noqa: E402
from os_helpers import BASH, Stubs  # noqa: E402

NOSLEEP = lambda s: None  # noqa: E731
sensord.log = lambda msg: None                          # quiet test output

# The BMP280 datasheet's worked example (§3.12, table "compensation formula example"); BME280's
# temperature and pressure formulas are identical.
DS_CALIB = dict(T1=27504, T2=26435, T3=-1000, P1=36477, P2=-10685, P3=3024, P4=2855, P5=140, P6=-7,
                P7=15500, P8=-14600, P9=6000)
DS_ADC_T, DS_ADC_P = 519888, 415148
HUM_CALIB = dict(H1=75, H2=370, H3=0, H4=309, H5=50, H6=30)    # typical values read from a real BME280


def encode_calib(c: dict) -> tuple[bytes, bytes]:
    b1 = bytearray(struct.pack("<Hhh", c["T1"], c["T2"], c["T3"]) +
                   struct.pack("<Hhhhhhhhh", *(c[f"P{i}"] for i in range(1, 10))))
    b1 += bytes([0, c.get("H1", 0)])
    h4, h5 = c.get("H4", 0) & 0xFFF, c.get("H5", 0) & 0xFFF
    b2 = struct.pack("<h", c.get("H2", 0)) + bytes([c.get("H3", 0), (h4 >> 4) & 0xFF, (h4 & 0xF) | ((h5 & 0xF) << 4),
                                                     (h5 >> 4) & 0xFF, c.get("H6", 0) & 0xFF])
    return bytes(b1), b2


def t_fine_int32(c, adc_t):
    """BME280 datasheet §4.2.3, bme280_compensate_T_int32 (t_fine part)."""
    var1 = (((adc_t >> 3) - (c["T1"] << 1)) * c["T2"]) >> 11
    var2 = (((((adc_t >> 4) - c["T1"]) * ((adc_t >> 4) - c["T1"])) >> 12) * c["T3"]) >> 14
    return var1 + var2


def hum_int32(c, adc_h, t_fine):
    """BME280 datasheet §4.2.3, bme280_compensate_H_int32 — an independent reference."""
    v = t_fine - 76800
    v = (((((adc_h << 14) - (c["H4"] << 20) - (c["H5"] * v)) + 16384) >> 15) *
         (((((((v * c["H6"]) >> 10) * (((v * c["H3"]) >> 11) + 32768)) >> 10) + 2097152) * c["H2"] + 8192) >> 14))
    v = v - (((((v >> 15) * (v >> 15)) >> 7) * c["H1"]) >> 4)
    v = max(0, min(v, 419430400))
    return (v >> 12) / 1024.0


# ─────────────────────────────── fake I²C devices ───────────────────────────

class FakeI2C:
    """Register-pointer device: write([reg, data…]) sets the pointer and writes; read(n) reads on."""

    def __init__(self):
        self.regs = bytearray(256)
        self.ptr = 0
        self.log: list[bytes] = []
        self.closed = False
        self.fail = False

    def write(self, data):
        if self.fail:
            raise OSError(121, "Remote I/O error")
        data = bytes(data)
        self.log.append(data)
        self.ptr = data[0]
        for i, b in enumerate(data[1:]):
            self.on_write((self.ptr + i) & 0xFF, b)

    def on_write(self, reg, val):
        self.regs[reg] = val

    def read(self, n):
        if self.fail:
            raise OSError(121, "Remote I/O error")
        out = bytes(self.regs[(self.ptr + i) & 0xFF] for i in range(n))
        self.ptr = (self.ptr + n) & 0xFF
        return out

    def close(self):
        self.closed = True


class FakeBME280(FakeI2C):
    def __init__(self, chip=0x60, calib=None, adc_t=DS_ADC_T, adc_p=DS_ADC_P, adc_h=30000):
        super().__init__()
        self.regs[0xD0] = chip
        b1, b2 = encode_calib(calib or {**DS_CALIB, **HUM_CALIB})
        self.regs[0x88:0x88 + 26] = b1
        self.regs[0xE1:0xE1 + 7] = b2
        self.set_adc(adc_t, adc_p, adc_h)

    def set_adc(self, t, p, h):
        self.regs[0xF7:0xFF] = bytes([p >> 12, (p >> 4) & 0xFF, (p & 0xF) << 4, t >> 12, (t >> 4) & 0xFF,
                                      (t & 0xF) << 4, h >> 8, h & 0xFF])

    def on_write(self, reg, val):
        if reg == 0xE0:
            return                                       # reset: nothing to do
        super().on_write(reg, val)


class FakeSHT(FakeI2C):
    """SHT3x (16-bit commands) or SHT4x (8-bit commands)."""

    def __init__(self, kind="sht3x", t_raw=0x6666, rh_raw=0x8000, bad_crc=False):
        super().__init__()
        self.kind, self.t_raw, self.rh_raw, self.bad_crc = kind, t_raw, rh_raw, bad_crc
        self.out = b""

    @staticmethod
    def word(v):
        b = bytes([v >> 8, v & 0xFF])
        return b + bytes([drivers.crc8_sensirion(b)])

    def write(self, data):
        data = bytes(data)
        self.log.append(data)
        if self.kind == "sht3x" and data == b"\xf3\x2d":
            self.out = self.word(0x8010)
        elif self.kind == "sht3x" and data == b"\x24\x00":
            self.out = self.word(self.t_raw) + self.word(self.rh_raw)
        elif self.kind == "sht4x" and data == b"\x89":
            self.out = self.word(0x1234) + self.word(0x5678)
        elif self.kind == "sht4x" and data == b"\xfd":
            self.out = self.word(self.t_raw) + self.word(self.rh_raw)
        else:
            self.out = b""
        if self.bad_crc and self.out:
            self.out = self.out[:-1] + bytes([self.out[-1] ^ 0xFF])

    def read(self, n):
        if len(self.out) < n:
            raise OSError(121, "Remote I/O error (NACK)")
        out, self.out = self.out[:n], self.out[n:]
        return out


class FakeADS1115(FakeI2C):
    def __init__(self, volts=1.8, fsr=4.096):
        super().__init__()
        self.volts, self.fsr = volts, fsr
        self.regs[0x01], self.regs[0x02] = 0x85, 0x83     # power-on config (OS bit set = idle)

    def write(self, data):
        data = bytes(data)
        self.log.append(data)
        self.ptr = data[0]
        if data[0] == 0x01 and len(data) == 3:
            raw = int(round(self.volts / self.fsr * 32768))
            raw = max(-32768, min(32767, raw)) & 0xFFFF
            self.regs[0x00], self.regs[0x01] = raw >> 8, raw & 0xFF
            self.cfg = (data[1] << 8) | data[2]
            self.conv = raw

    def read(self, n):
        if self.ptr == 0x00:
            return bytes([self.conv >> 8, self.conv & 0xFF])
        return bytes([0x85 | 0x80, 0x83])


class Compensation(unittest.TestCase):
    def test_datasheet_example(self):
        t, p, h = drivers.bme280_compensate(DS_CALIB, DS_ADC_T, DS_ADC_P)
        self.assertAlmostEqual(t, 25.08, places=2)              # datasheet: 25.08 °C
        self.assertAlmostEqual(p, 100653.27, delta=0.1)         # datasheet (double): 100653.27 Pa
        self.assertIsNone(h)
        self.assertEqual(t_fine_int32(DS_CALIB, DS_ADC_T), 128422)   # datasheet t_fine

    def test_calibration_layout_roundtrip(self):
        c = {**DS_CALIB, **HUM_CALIB, "H4": -300, "H5": -7, "H6": -5}
        b1, b2 = encode_calib(c)
        self.assertEqual(drivers.bme280_parse_calib(b1, b2), c)
        self.assertNotIn("H1", drivers.bme280_parse_calib(b1, None))

    def test_humidity_matches_integer_reference(self):
        c = {**DS_CALIB, **HUM_CALIB}
        tf = t_fine_int32(c, DS_ADC_T)
        for adc_h in (20000, 26000, 30000, 34000, 38000):
            _, _, h = drivers.bme280_compensate(c, DS_ADC_T, DS_ADC_P, adc_h)
            self.assertAlmostEqual(h, hum_int32(c, adc_h, tf), delta=0.05, msg=f"adc_h={adc_h}")
        self.assertEqual(drivers.bme280_compensate(c, DS_ADC_T, DS_ADC_P, 0)[2], 0.0)       # clamped
        self.assertEqual(drivers.bme280_compensate(c, DS_ADC_T, DS_ADC_P, 65535)[2], 100.0)

    def test_humidity_monotonic(self):
        c = {**DS_CALIB, **HUM_CALIB}
        hs = [drivers.bme280_compensate(c, DS_ADC_T, DS_ADC_P, a)[2] for a in range(22000, 40000, 1000)]
        self.assertEqual(hs, sorted(hs))

    def test_zero_p1_no_division(self):
        self.assertEqual(drivers.bme280_compensate({**DS_CALIB, "P1": 0}, DS_ADC_T, DS_ADC_P)[1], 0.0)


class I2CDrivers(unittest.TestCase):
    def test_bme280(self):
        dev = FakeBME280()
        b = drivers.BME280(dev, sleep=NOSLEEP)
        r = b.read()
        self.assertEqual(b.kind, "bme280")
        self.assertAlmostEqual(r["temperature"], 25.08, places=2)
        self.assertAlmostEqual(r["pressure"], 1006.53, places=2)
        self.assertTrue(0 < r["humidity"] < 100)
        writes = [w for w in dev.log if len(w) == 2]
        self.assertIn(b"\xf2\x01", writes)                       # humidity oversampling before…
        self.assertLess(writes.index(b"\xf2\x01"), writes.index(b"\xf4\x25"))   # …the forced-mode write

    def test_bme280_sea_level(self):
        r = drivers.BME280(FakeBME280(), altitude_m=300, sleep=NOSLEEP).read()
        self.assertAlmostEqual(r["pressure"], 1042.6, delta=1.0)

    def test_bmp280_no_humidity(self):
        b = drivers.BME280(FakeBME280(chip=0x58), sleep=NOSLEEP)
        self.assertEqual(b.kind, "bmp280")
        self.assertNotIn("humidity", b.read())

    def test_wrong_chip_and_skipped(self):
        with self.assertRaises(ValueError):
            drivers.BME280(FakeBME280(chip=0x00), sleep=NOSLEEP)
        dev = FakeBME280(adc_t=0x80000)
        with self.assertRaises(OSError):
            drivers.BME280(dev, sleep=NOSLEEP).read()

    def test_blank_calibration(self):
        with self.assertRaises(ValueError):
            drivers.BME280(FakeBME280(calib={**DS_CALIB, **HUM_CALIB, "T1": 0}), sleep=NOSLEEP)

    def test_crc(self):
        self.assertEqual(drivers.crc8_sensirion(b"\xbe\xef"), 0x92)   # Sensirion datasheet example

    def test_sht3x(self):
        s = drivers.SHT3x(FakeSHT("sht3x", t_raw=0x6666, rh_raw=0x8000), sleep=NOSLEEP)
        r = s.read()
        self.assertAlmostEqual(r["temperature"], -45 + 175 * 0x6666 / 65535, places=2)
        self.assertAlmostEqual(r["humidity"], 50.0, places=0)

    def test_sht4x(self):
        r = drivers.SHT4x(FakeSHT("sht4x", t_raw=0x6666, rh_raw=0x0100), sleep=NOSLEEP).read()
        self.assertEqual(r["humidity"], 0.0)                     # −6 + … clamped at 0
        with self.assertRaises(OSError):
            drivers.SHT4x(FakeSHT("sht3x"), sleep=NOSLEEP)       # an SHT3x doesn't answer 0x89

    def test_sht_bad_crc(self):
        s = drivers.SHT3x(FakeSHT("sht3x"), sleep=NOSLEEP)
        s.dev.bad_crc = True
        with self.assertRaises(OSError):
            s.read()

    def test_ads1115_level(self):
        dev = FakeADS1115(volts=1.8)
        a = drivers.ADS1115Level(dev, channel=2, fsr=4.096, v_zero=0.6, v_full=3.0, full_scale_cm=500, offset_cm=10, sleep=NOSLEEP)
        r = a.read()
        self.assertAlmostEqual(r["water_level"], 260.0, delta=0.5)      # halfway → 250 cm + 10
        cfg = dev.cfg
        self.assertEqual((cfg >> 12) & 0x7, 4 + 2)               # AIN2 single-ended
        self.assertEqual((cfg >> 9) & 0x7, 1)                    # ±4.096 V
        self.assertTrue(cfg & 0x8000 and cfg & 0x100)            # start, single-shot
        dev.volts = 0.0
        with self.assertRaises(OSError):                          # open 4–20 mA loop
            a.read()
        with self.assertRaises(ValueError):
            drivers.ADS1115Level(dev, channel=5)
        with self.assertRaises(ValueError):
            drivers.ADS1115Level(dev, fsr=5.0)


class OneWire(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="w1-"))
        (self.root / "w1_bus_master1").mkdir()
        self.p = self.root / "28-0123456789ab"
        self.p.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def w(self, text):
        (self.p / "w1_slave").write_text(text)

    def test_reads(self):
        self.w("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n")
        self.assertEqual(drivers.DS18B20(self.p).read(), {"temperature": 23.125})
        self.w("90 fc 4b 46 7f ff 0c 10 3e : crc=3e YES\n90 fc 4b 46 7f ff 0c 10 3e t=-55000\n")
        self.assertEqual(drivers.DS18B20(self.p).read(), {"temperature": -55.0})

    def test_bad(self):
        for text in ("72 01 : crc=57 NO\n72 01 t=23125\n", "50 05 : crc=1 YES\n50 05 t=85000\n",
                     "x YES\nno temp\n", "", "x YES\nx t=999999\n"):
            self.w(text)
            with self.assertRaises(OSError, msg=text):
                drivers.DS18B20(self.p).read()
        with self.assertRaises(OSError):
            drivers.DS18B20(self.root / "28-ffffffffffff").read()

    def test_listing(self):
        (self.root / "3b-000000000001").mkdir()
        (self.root / "01-000000000002").mkdir()               # not a temperature family
        self.assertEqual([p.name for p in drivers.w1_probes(self.root)], ["28-0123456789ab", "3b-000000000001"])
        self.assertEqual(drivers.w1_probes(self.root / "missing"), [])


# ─────────────────────────────── GPIO ───────────────────────────────────────

class Gpio(unittest.TestCase):
    def runner(self, version, value, log):
        def run(argv):
            log.append(argv)
            if argv[:2] == ["gpioget", "--version"]:
                return version
            if argv[0] == "gpiofind":
                return "gpiochip4 24" if "v1." in version else None
            if version.startswith("v2") and "--bias=pull-up" in argv and "GPIO24" in argv:
                return f'"GPIO24"={"active" if value[0] else "inactive"}'
            if version.startswith("v1") and argv[-2:] == ["gpiochip4", "24"]:
                return str(value[0])
            return None
        return run

    def test_v2_and_v1(self):
        for ver in ("gpioget (libgpiod) v2.1", "gpioget (libgpiod) v1.6.3"):
            log, val = [], [1]
            g = drivers.GpioReader(24, runner=self.runner(ver.split()[-1], val, log))
            self.assertEqual(g.read(), 1)
            val[0] = 0
            self.assertEqual(g.read(), 0)
            self.assertIn("--bias=pull-up", g.argv)

    def test_unreadable(self):
        g = drivers.GpioReader(24, runner=lambda argv: None)
        with self.assertRaises(OSError):
            g.read()

    def test_fixed_cmd(self):
        g = drivers.GpioReader(24, cmd="mycmd 24", runner=lambda argv: "1" if argv == ["mycmd", "24"] else None)
        self.assertEqual(g.read(), 1)

    def test_float_polarity_and_majority(self):
        class R:
            def __init__(self, seq):
                self.seq = list(seq)

            def read(self):
                return self.seq.pop(0)
        self.assertEqual(drivers.FloatSwitch(R([1, 1, 1]), "high", sleep=NOSLEEP).read(), {"flood": 1.0})
        self.assertEqual(drivers.FloatSwitch(R([1, 0, 1]), "high", sleep=NOSLEEP).read(), {"flood": 1.0})
        self.assertEqual(drivers.FloatSwitch(R([0, 1, 0]), "high", sleep=NOSLEEP).read(), {"flood": 0.0})
        self.assertEqual(drivers.FloatSwitch(R([0, 0, 0]), "low", sleep=NOSLEEP).read(), {"flood": 1.0})
        with self.assertRaises(ValueError):
            drivers.FloatSwitch(R([]), "sideways")


# ─────────────────────────────── serial (pty) ───────────────────────────────

class PtyDevice:
    """The device end of a pseudo-terminal: answers commands (bytes → reply) and/or streams lines."""

    def __init__(self, replies=None, stream=None, every=0.1):
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self.replies, self.stream, self.every = replies or {}, stream, every
        self.got = b""
        self.stop = False
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        buf, last = b"", 0.0
        while not self.stop:
            r, _, _ = select.select([self.master], [], [], 0.02)
            if r:
                try:
                    data = os.read(self.master, 256)
                except OSError:
                    return
                buf += data
                self.got += data
                for cmd, reply in self.replies.items():
                    while cmd in buf:
                        buf = buf.replace(cmd, b"", 1)
                        if reply:
                            os.write(self.master, reply)
            if self.stream and time.monotonic() - last > self.every:
                last = time.monotonic()
                try:
                    os.write(self.master, self.stream)
                except OSError:
                    return

    def close(self):
        self.stop = True
        self.t.join(1)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


class SerialDrivers(unittest.TestCase):
    def test_gmc_2_and_4_bytes(self):
        for reply, cpm in ((b"\x00\x1c", 28), (b"\x00\x00\x01\x2c", 300)):
            dev = PtyDevice({b"<GETCPM>>": reply, b"<HEARTBEAT0>>": b""})
            try:
                r = drivers.GQGMC(dev.path, baud=115200, factor=0.0065).read()
            finally:
                dev.close()
            self.assertEqual(r["cpm"], cpm)
            self.assertAlmostEqual(r["radiation"], cpm * 0.0065, places=4)
            self.assertIn(b"<HEARTBEAT0>>", dev.got)

    def test_gmc_silent_or_garbage(self):
        dev = PtyDevice({b"<GETCPM>>": b"\x01"})
        try:
            with self.assertRaises(OSError):
                drivers.GQGMC(dev.path).read()
        finally:
            dev.close()
        dev = PtyDevice({b"<GETCPM>>": b"\x00\x1c"})
        try:
            with self.assertRaises(OSError):                     # forced 4-byte mode, 2 came back
                drivers.GQGMC(dev.path, cpm_bytes=4).read()
        finally:
            dev.close()

    def test_text_geiger(self):
        dev = PtyDevice(stream=b"CPS, 1, CPM, 22, uSv/hr, 0.12, SLOW\r\n", every=0.1)
        try:
            g = drivers.TextGeiger(dev.path, listen_s=0.6)
            self.assertEqual(g.read(), {"cpm": 22.0, "radiation": 0.12})
            g.factor = 0.0057
            r = g.read()
            self.assertAlmostEqual(r["radiation"], 22 * 0.0057, places=4)
        finally:
            dev.close()

    def test_text_geiger_garbage(self):
        dev = PtyDevice(stream=b"\x00\xff hello world\r\n" * 3, every=0.1)
        try:
            with self.assertRaises(OSError):
                drivers.TextGeiger(dev.path, listen_s=0.4).read()
        finally:
            dev.close()

    def test_line_parser(self):
        self.assertEqual(drivers.parse_geiger_line("CPM: 15 uSv/h: 0.08", None), {"cpm": 15.0, "radiation": 0.08})
        self.assertEqual(drivers.parse_geiger_line("cpm=40", None), {"cpm": 40.0})
        self.assertIsNone(drivers.parse_geiger_line("CPS, 3", None))

    def test_jsn(self):
        frame = bytes([0xFF, 0x04, 0xD2, (0xFF + 0x04 + 0xD2) & 0xFF])     # 1234 mm
        dev = PtyDevice({b"\x55": frame})
        try:
            r = drivers.JSNSR04T(dev.path, mount_height_cm=200, sleep=NOSLEEP).read()
        finally:
            dev.close()
        self.assertAlmostEqual(r["water_level"], 200 - 123.4, places=1)
        self.assertEqual(drivers.jsn_frames(b"\x00\xff\x04\xd2\xd5\xff\x00\x00\x00junk"), [1234])
        with self.assertRaises(ValueError):
            drivers.JSNSR04T(dev.path, mount_height_cm=0)

    def test_jsn_no_echo(self):
        frame = bytes([0xFF, 0x00, 0x00, 0xFF])                  # 0 mm: no echo
        dev = PtyDevice({b"\x55": frame})
        try:
            with self.assertRaises(OSError) as cm:
                drivers.JSNSR04T(dev.path, mount_height_cm=200, sleep=NOSLEEP).read()
            self.assertIn("no echo", str(cm.exception))
        finally:
            dev.close()

    def test_missing_port(self):
        with self.assertRaises(OSError):
            drivers.GQGMC("/dev/does-not-exist").read()
        with self.assertRaises(ValueError):
            drivers.Serial("/dev/null", baud=12345)


# ─────────────────────────────── store ──────────────────────────────────────

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="store-"))
        self.st = sl.Store(self.tmp / "r.db")

    def tearDown(self):
        self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def item(self, t, v, key="a:temperature", metric="temperature"):
        return {"key": key, "sensor": key.split(":")[0], "metric": metric, "t": t, "v": v, "label": "A"}

    def test_downsampling(self):
        base = 1_700_000_000 - 1_700_000_000 % 3600
        self.st.add_many([self.item(base + i * 30, float(i % 20)) for i in range(240)])   # 2 h, every 30 s
        rows = self.st.db.execute("SELECT t, avg, mn, mx, n FROM agg10 ORDER BY t").fetchall()
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0][4], 20)
        self.assertAlmostEqual(rows[0][1], 9.5)
        self.assertEqual((rows[0][2], rows[0][3]), (0.0, 19.0))
        hours = self.st.db.execute("SELECT n, avg FROM agg60").fetchall()
        self.assertEqual([h[0] for h in hours], [120, 120])
        # history picks the right table and bucket width
        now = base + 7200
        h24 = self.st.history("a:temperature", 86400, now, points=240)
        self.assertTrue(all(p[0] % 360 == 0 for p in h24))
        h7 = self.st.history("a:temperature", 7 * 86400, now, points=240)
        self.assertIn(len(h7), (3, 4))                           # 2520 s buckets over 2 h of data
        self.assertTrue(all(p[0] % 2520 == 0 for p in h7))
        h1y = self.st.history("a:temperature", 365 * 86400, now)
        self.assertAlmostEqual(sum(p[1] for p in h1y) / len(h1y), 9.5)
        self.assertEqual(self.st.history("nope:temperature", 86400, now), [])

    def test_retention(self):
        now = 1_700_000_000
        self.st.add_many([self.item(now - 3 * 86400, 1.0), self.item(now - 40 * 86400, 2.0),
                          self.item(now - 400 * 86400, 3.0), self.item(now - 10, 4.0),
                          self.item(now + 10 * 86400, 5.0)])     # a clock that was in the future
        self.st.prune(now)
        raw = [r[0] for r in self.st.db.execute("SELECT v FROM raw")]
        self.assertEqual(raw, [4.0])
        a10 = sorted(r[0] for r in self.st.db.execute("SELECT avg FROM agg10"))
        self.assertEqual(a10, [1.0, 4.0])
        a60 = sorted(r[0] for r in self.st.db.execute("SELECT avg FROM agg60"))
        self.assertEqual(a60, [1.0, 2.0, 4.0])

    def test_junk_rejected(self):
        n = self.st.add_many([self.item(1, float("nan")), self.item(2, 9999.0), self.item(3, True),
                              self.item(4, "5"), self.item(5, 1.0, key="../x:temperature"),
                              self.item(6, 1.0, metric="nonsense", key="a:nonsense"), self.item(7, 20.0)])
        self.assertEqual(n, 1)
        self.assertEqual(self.st.add_many([self.item(7, 30.0)]), 0)          # same second: first wins
        self.assertEqual(self.st.db.execute("SELECT n FROM agg10").fetchone()[0], 1)

    def test_series_cap(self):
        st = sl.Store(self.tmp / "cap.db", max_series=3)
        n = st.add_many([self.item(1, 1.0, key=f"s{i}:temperature") for i in range(10)])
        self.assertEqual(n, 3)
        st.close()

    def test_size_cap(self):
        st = sl.Store(self.tmp / "big.db", max_mb=0.0001)
        now = 1_700_000_000
        st.add_many([self.item(now - 3600 + i, float(i % 50)) for i in range(3000)])
        st.prune(now)
        self.assertLess(st.db.execute("SELECT count(*) FROM raw").fetchone()[0], 3000)
        st.close()

    def test_median_and_readonly(self):
        now = 1_700_000_000
        self.st.add_many([self.item(now - i * 600, 0.1 + (i % 3) * 0.01, key="g:radiation", metric="radiation")
                          for i in range(50)])
        self.assertAlmostEqual(self.st.median("g:radiation", now - 86400), 0.11)
        self.assertIsNone(self.st.median("g:radiation", now - 3600))        # too few buckets yet
        ro = sl.Store(self.tmp / "r.db", readonly=True)
        self.assertTrue(ro.history("g:radiation", 86400, now))
        with self.assertRaises(sl.sqlite3.Error):
            ro.db.execute("DELETE FROM raw")
        ro.close()
        with self.assertRaises(FileNotFoundError):
            sl.Store(self.tmp / "missing.db", readonly=True)

    def test_concurrent_reader(self):
        now = time.time()
        stop, errors = [], []

        def reader():
            ro = sl.Store(self.tmp / "r.db", readonly=True)
            while not stop:
                try:
                    ro.history("a:temperature", 86400, time.time())
                except sl.sqlite3.Error as e:
                    errors.append(e)
            ro.close()
        self.st.add_many([self.item(now - 100, 1.0)])
        ts = [threading.Thread(target=reader) for _ in range(3)]
        for t in ts:
            t.start()
        for i in range(200):
            self.st.add_many([self.item(now + i, float(i % 30))])
        stop.append(1)
        for t in ts:
            t.join()
        self.assertEqual(errors, [])


# ─────────────────────────────── rules ──────────────────────────────────────

def rd(v, key="out:temperature", sensor="out", metric="temperature", **kw):
    return {"key": key, "sensor": sensor, "metric": metric, "value": v, "label": "Outside", **kw}


class Rules(unittest.TestCase):
    def rules(self, **over):
        base = {"id": "freeze", "sensor": "*", "metric": "temperature", "below": 0.0, "hysteresis": 1.0,
                "confirm": 2, "cooldown_min": 60, "board": "community", "mesh": True, "level": "warning"}
        return [sl.validate_rule({**base, **over})]

    def test_confirm_hysteresis_cooldown(self):
        e, rules, t = sl.Engine(), self.rules(), 1000.0
        self.assertEqual(e.evaluate(rules, [rd(-1)], t), [])           # 1st reading: not yet
        ev = e.evaluate(rules, [rd(-1.5)], t + 30)                      # 2nd in a row: alert
        self.assertEqual([x["kind"] for x in ev], ["alert"])
        self.assertIn("below the alert level of 0.0°C", ev[0]["text"])
        # hovering around the line doesn't flap: needs > +1.0 twice to clear
        for v in (0.2, -0.1, 0.5, 0.9, 1.2, -0.3):
            self.assertEqual(e.evaluate(rules, [rd(v)], t + 60), [])
        self.assertEqual(len(e.active), 1)
        ev = e.evaluate(rules, [rd(1.5)], t + 90) + e.evaluate(rules, [rd(1.6)], t + 120)
        self.assertEqual([x["kind"] for x in ev], ["clear"])
        self.assertIn("back to normal", ev[0]["text"])
        # within the cooldown: active again, but not announced; and its clear is silent too
        e.evaluate(rules, [rd(-2)], t + 200)
        self.assertEqual(e.evaluate(rules, [rd(-2)], t + 230), [])
        self.assertEqual(len(e.active_list(t + 230)), 1)
        e.evaluate(rules, [rd(3)], t + 260)
        self.assertEqual(e.evaluate(rules, [rd(3)], t + 290), [])
        # after the cooldown it is announced again
        e.evaluate(rules, [rd(-2)], t + 4000)
        self.assertEqual(len(e.evaluate(rules, [rd(-2)], t + 4030)), 1)

    def test_flood_and_scope(self):
        rules = [sl.validate_rule(d) for d in sl.DEFAULT_RULES]
        e = sl.Engine()
        wet = rd(1.0, key="b:flood", sensor="b", metric="flood")
        e.evaluate(rules, [wet], 0)
        ev = e.evaluate(rules, [wet], 5)
        self.assertEqual(ev[0]["level"], "danger")
        self.assertTrue(ev[0]["mesh"])
        self.assertIn("water detected", ev[0]["text"])
        # "*" rules ignore remote nodes and sensors with alerts = false
        e2 = sl.Engine()
        for _ in range(3):
            self.assertEqual(e2.evaluate(rules, [rd(-5, remote=True), rd(-5, key="fz:temperature", sensor="fz", alerts=False)], 0), [])

    def test_background_rule(self):
        rules = [sl.validate_rule(d) for d in sl.DEFAULT_RULES if d["id"] == "radiation"]
        e = sl.Engine()
        g = lambda v: rd(v, key="g:radiation", sensor="g", metric="radiation")   # noqa: E731
        bg = lambda key: 0.1                                                     # noqa: E731
        for _ in range(3):
            ev = e.evaluate(rules, [g(0.45)], 0, bg)                  # < 5× normal (0.5) and < 1.0
        self.assertEqual(ev, [])
        for i in range(3):
            ev = e.evaluate(rules, [g(0.6)], i, bg)
        self.assertEqual(len(ev), 1)
        self.assertIn("normal here is 0.10 µSv/h", ev[0]["text"])
        # without a background yet only the absolute level counts
        e2 = sl.Engine()
        for i in range(3):
            self.assertEqual(e2.evaluate(rules, [g(0.6)], i, lambda k: None), [])

    def test_state_roundtrip_and_removed_rule(self):
        e, rules = sl.Engine(), self.rules(confirm=1)
        e.evaluate(rules, [rd(-3)], 0)
        e2 = sl.Engine(json.loads(json.dumps(e.state())))           # sensord restart
        self.assertEqual(e2.evaluate(rules, [rd(-3)], 10), [])        # still on: not re-announced
        self.assertEqual(len(e2.active), 1)
        e2.evaluate(self.rules(id="other", confirm=1, below=-50), [rd(-3)], 20)
        self.assertEqual(e2.active, {})
        self.assertIsInstance(sl.Engine({"active": "junk"}).active, dict)

    def test_validation(self):
        bad = [{"metric": "temperature"}, {"metric": "nope", "above": 1}, {"metric": "temperature", "above": "hot"},
               {"metric": "temperature", "above": 1e9}, {"metric": "temperature", "above": 5, "below": 10},
               {"metric": "temperature", "above": 5, "board": "town"}, {"metric": "temperature", "above": 5, "level": "x"},
               {"id": "../x", "metric": "temperature", "above": 5}, {"metric": "temperature", "above": 5, "sensor": "a b"},
               {"metric": "temperature", "above": True}, {"metric": "temperature", "above": float("nan")}, "rule"]
        for d in bad:
            with self.assertRaises(ValueError, msg=repr(d)):
                sl.validate_rule(d)
        r = sl.validate_rule({"metric": "temperature", "above": "30", "note": "hot‮\x00 day" + "x" * 100})
        self.assertEqual(r["above"], 30.0)
        self.assertNotIn("‮", r["note"])
        self.assertLessEqual(len(r["note"]), 60)
        with self.assertRaises(ValueError):
            sl.validate_rules([{"id": "a", "metric": "temperature", "above": 1}] * 2)
        with self.assertRaises(ValueError):
            sl.validate_rules([{"metric": "temperature", "above": 1}] * 41)

    def test_settings(self):
        ok = sl.validate_settings({"remote_nodes": ["!ABCD1234", "!abcd1234"], "summary_every_h": 2,
                                   "names": {"out": " Back\x00yard "}, "mesh_alerts": False})
        self.assertEqual(ok["remote_nodes"], ["!abcd1234"])
        self.assertEqual(ok["names"], {"out": "Back yard"})
        for bad in ({"remote_nodes": ["abcd"]}, {"remote_nodes": "!abcd1234"}, {"summary_every_h": 25},
                    {"summary_every_h": True}, {"mesh_board": "none"}, {"mesh_alerts": "yes"},
                    {"names": {"../x": "a"}}, {"names": {str(i): "a" for i in range(65)}}, [],
                    {"remote_nodes": [f"!{i:08x}" for i in range(33)]}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                sl.validate_settings(bad)
        eff = sl.effective_settings({"mesh_board": "family", "remote_nodes": ["!00000001", "bad"]}, {"rules": "junk"})
        self.assertEqual(eff["mesh_board"], "family")
        self.assertEqual(eff["remote_nodes"], ["!00000001"])
        self.assertIsNone(eff["rules"])

    def test_config_rules(self):
        self.assertEqual([r["id"] for r in sl.config_rules({})], ["flood", "freeze", "heat", "radiation"])
        rules = sl.config_rules({"alert": [{"id": "x", "metric": "humidity", "above": 90}, {"metric": "bad"}]},
                                [{"id": "tank", "metric": "water_level", "alert_above": 150}])
        self.assertEqual([r["id"] for r in rules], ["x", "tank-above"])
        self.assertEqual(rules[1]["above"], 150.0)

    def test_formatting(self):
        self.assertEqual(sl.fmt_value("temperature", -0.04), "0.0°C")
        self.assertEqual(sl.fmt_value("flood", 1), "WET")
        self.assertEqual(sl.fmt_value("radiation", 0.114), "0.11 µSv/h")
        s = sl.cut_bytes("é" * 300, 200)
        self.assertLessEqual(len(s.encode()), 200)
        self.assertTrue(s.endswith("…"))


# ─────────────────────────────── telemetry ──────────────────────────────────

class Telemetry(unittest.TestCase):
    NOW = 1_700_000_000.0

    def line(self, **over):
        d = {"at": self.NOW - 5, "from": "!abcd1234", "name": "Barn", "channel": 1, "trusted": True,
             "kind": "environment", "values": {"temperature": 4.2, "relativeHumidity": 81, "barometricPressure": 1012.5}}
        d.update(over)
        return json.dumps(d)

    def test_good(self):
        r = sl.parse_telemetry(self.line(), self.NOW)
        self.assertEqual(r["node"], "!abcd1234")
        self.assertEqual(r["values"], {"temperature": 4.2, "humidity": 81.0, "pressure": 1012.5})
        r = sl.parse_telemetry(self.line(values={"relative_humidity": 50, "radiation": 15}), self.NOW)
        self.assertEqual(r["values"], {"humidity": 50.0, "radiation": 0.15})

    def test_hostile(self):
        hostile = [
            "", "not json", "[]", "null", '"str"', "{" * 5000 + "}" * 5000, "x" * 5000,
            self.line(**{"from": "abcd1234"}), self.line(**{"from": "!ABCD12345"}), self.line(**{"from": 123}),
            self.line(at=self.NOW + 3600), self.line(at=self.NOW - 30 * 86400), self.line(at="soon"), self.line(at=True),
            self.line(values="hot"), self.line(values={"temperature": "4.2"}), self.line(values={"temperature": True}),
            self.line(values={"temperature": 1e300}), self.line(values={"relativeHumidity": 250}),
            self.line(values={f"k{i}": 1 for i in range(70)}), self.line(values={}), self.line(values={"secret": 1}),
            self.line(values={"x" * 50: 1}),
            '{"at": %f, "from": "!abcd1234", "values": {"temperature": NaN}}' % self.NOW,
            '{"at": %f, "from": "!abcd1234", "values": {"temperature": Infinity}}' % self.NOW,
        ]
        for h in hostile:
            self.assertIsNone(sl.parse_telemetry(h, self.NOW), msg=h[:80])

    def test_name_and_fields_sanitized(self):
        r = sl.parse_telemetry(self.line(name="<script>‮evil\x07" + "A" * 100, channel=99, trusted="yes",
                                         kind="weird"), self.NOW)
        self.assertNotIn("‮", r["name"])
        self.assertNotIn("\x07", r["name"])
        self.assertLessEqual(len(r["name"]), 32)
        self.assertIsNone(r["channel"])
        self.assertFalse(r["trusted"])
        self.assertEqual(r["kind"], "other")
        self.assertEqual(sl.parse_telemetry(self.line(name=["x"]), self.NOW)["name"], "")

    def test_tail(self):
        tmp = Path(tempfile.mkdtemp(prefix="tele-"))
        try:
            p = tmp / "t.jsonl"
            p.write_text(self.line() + "\n" + "garbage\n")
            tail = sl.TelemetryTail(p, offset=0)
            self.assertEqual(len(tail.read(self.NOW)), 1)
            with open(p, "a") as fh:
                fh.write(self.line(name="Half")[:40])         # a line still being written
            self.assertEqual(tail.read(self.NOW), [])
            with open(p, "a") as fh:
                fh.write(self.line(name="Half")[40:] + "\n")
            self.assertEqual([r["name"] for r in tail.read(self.NOW)], ["Half"])
            p.write_text(self.line(name="New") + "\n")        # rotated / truncated
            self.assertEqual([r["name"] for r in tail.read(self.NOW)], ["New"])
            with open(p, "a") as fh:                          # an endless line can't wedge the reader
                fh.write("y" * 300_000 + "\n" + self.line(name="After") + "\n")
            names = []
            for _ in range(4):
                names += [r["name"] for r in tail.read(self.NOW)]
            self.assertEqual(names, ["After"])
            self.assertEqual(sl.TelemetryTail(tmp / "missing").read(self.NOW), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────────── sensord end to end ─────────────────────────

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeSerialGMC:
    cpm = 20

    def __init__(self, path, baud):
        if "missing" in path:
            raise OSError(2, "No such file or directory")
        self.out = b""

    def write(self, data):
        if data == b"<GETCPM>>":
            self.out = FakeSerialGMC.cpm.to_bytes(2, "big")

    def read(self, n, timeout, quiet=None):
        out, self.out = self.out[:n], self.out[n:]
        return out

    def close(self):
        pass


class SensordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sensord-")).resolve()
        self.w1 = self.tmp / "w1"
        (self.w1 / "28-00000000beef").mkdir(parents=True)
        (self.w1 / "28-00000000beef" / "w1_slave").write_text("aa YES\naa t=12500\n")
        self.devs = {0x76: FakeBME280()}
        self.opened: list[int] = []
        self.wet = [0]
        self.clock = FakeClock()
        self.addCleanup(shutil.rmtree, self.tmp, True)     # cleanups run last-in first-out: after d.close()

    def i2c_open(self, bus, addr):
        self.opened.append(addr)
        if addr not in self.devs:
            raise OSError(121, "Remote I/O error")
        return self.devs[addr]

    def gpio(self, argv):
        if argv[:2] == ["gpioget", "--version"]:
            return "v2.1"
        if "--bias=pull-up" in argv:
            return str(self.wet[0])
        return None

    def make(self, extra: str = "", **kw) -> sensord.Sensord:
        import tomllib
        cfg = tomllib.loads(f"""
[portal]
features_data = "{self.tmp}/features"
mesh_outbox = "{self.tmp}/outbox.jsonl"
[power]
input_monitor = "ina228"
input_i2c_address = 0x45
[sensors]
poll_s = 30
mesh_min_interval_s = 600
mesh_min_interval_danger_s = 120
mesh_max_per_hour = 4
telemetry_file = "{self.tmp}/tele.jsonl"
remote_nodes = ["!abcd1234"]
[[sensors.device]]
type = "float"
id = "basement"
name = "Basement"
gpio = 24
[[sensors.device]]
type = "gmc"
id = "geiger"
port = "/dev/serial/by-id/gmc"
[[sensors.device]]
type = "jsn_sr04t"
id = "bad-port"
port = "COM3"
[[sensors.device]]
type = "laser"
id = "x"
{extra}
""")
        d = sensord.Sensord(cfg, i2c_open=self.i2c_open, serial_open=FakeSerialGMC, gpio_runner=self.gpio,
                            w1_root=self.w1, sleep=NOSLEEP, mono=self.clock, **kw)
        self.addCleanup(d.close)
        return d

    def tick(self, d, now, secs=30):
        self.clock.t += secs
        return d.step(now)

    def test_end_to_end(self):
        d = self.make()
        data = self.tmp / "features" / "sensors"
        self.assertEqual(len(d.config_errors), 2)                # bad port + unknown type
        t = 1_700_000_000.0
        d.step(t)
        cur = json.loads((data / "current.json").read_text())
        ids = {s["id"]: s for s in cur["sensors"]}
        self.assertEqual(set(ids), {"basement", "geiger", "bme280-76", "probe-00beef"})
        self.assertAlmostEqual(ids["bme280-76"]["values"]["temperature"], 25.08, places=1)
        self.assertEqual(ids["probe-00beef"]["values"], {"temperature": 12.5})
        self.assertEqual(ids["basement"]["values"], {"flood": 0.0})
        self.assertAlmostEqual(ids["geiger"]["values"]["radiation"], 0.13)
        # reserved addresses are never probed: display 0x3C, gauge 0x36, the power meter at 0x45
        self.assertNotIn(0x45, self.opened)
        self.assertIn(0x44, self.opened)
        # flood: wet twice in a row (float switch polls every 5 s) → alert, event, mesh line
        self.wet[0] = 1
        self.tick(d, t + 5, 5)
        ev = self.tick(d, t + 10, 5)
        self.assertEqual([e["kind"] for e in ev], ["alert"])
        events = sl.read_events(data)
        self.assertEqual(events[-1]["board"], "community")
        out = [json.loads(x) for x in (self.tmp / "outbox.jsonl").read_text().splitlines()]
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["text"].startswith("Badger sensors: ⚠ Basement: water detected"))
        self.assertLessEqual(len(out[0]["text"].encode()), 200)
        cur = json.loads((data / "current.json").read_text())
        self.assertEqual(cur["alerts"][0]["level"], "danger")
        # dry again: clear goes out too, but not before the danger gap (120 s)
        self.wet[0] = 0
        self.tick(d, t + 15, 5)
        ev = self.tick(d, t + 20, 5)
        self.assertEqual([e["kind"] for e in ev], ["clear"])
        self.assertEqual(len((self.tmp / "outbox.jsonl").read_text().splitlines()), 1)
        self.tick(d, t + 200, 5)                                  # non-urgent gap is 600 s
        self.assertEqual(len((self.tmp / "outbox.jsonl").read_text().splitlines()), 1)
        self.tick(d, t + 700, 5)
        lines = (self.tmp / "outbox.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("dry again", json.loads(lines[1])["text"])
        # readings stored
        st = sl.Store(data / "readings.db", readonly=True)
        self.assertTrue(st.history("basement:flood", 86400, t + 700))
        st.close()
        # restart: state survives, nothing re-announced
        d.close()
        d2 = self.make()
        self.assertTrue(d2.seq >= events[-1]["seq"])

    def test_mesh_hourly_cap_and_coalescing(self):
        d = self.make()
        t = 1_700_000_000.0
        for i in range(8):
            d.queue_mesh(f"danger {i}", "community", urgent=True)
        sent = 0
        for k in range(60):
            sent += d.flush_mesh(t + k * 60)
        self.assertEqual(sent, 1)                                 # all queued ones coalesced into one line
        self.assertIn("danger 0 · danger 1", (self.tmp / "outbox.jsonl").read_text())
        for k in range(10):
            d.queue_mesh(f"later {k}", "family", urgent=True)
            d.flush_mesh(t + 4000 + k * 130)
        msgs = [json.loads(x) for x in (self.tmp / "outbox.jsonl").read_text().splitlines()]
        self.assertLessEqual(len([m for m in msgs if m["board"] == "family"]), 4)
        self.assertLessEqual(len(d.mesh_queue), 10)

    def test_remote_nodes_and_settings(self):
        d = self.make()
        t = 1_700_000_000.0
        tele = self.tmp / "tele.jsonl"
        lines = [{"at": t - 2, "from": "!abcd1234", "name": "Barn", "trusted": True, "kind": "environment",
                  "values": {"temperature": -3.0}},
                 {"at": t - 2, "from": "!00000bad", "name": "Stranger", "trusted": False, "kind": "environment",
                  "values": {"temperature": -9.0}},
                 {"at": t - 1, "from": "!abcd1234", "name": "Barn", "trusted": True, "values": {"temperature": 99999}}]
        tele.write_text("".join(json.dumps(x) + "\n" for x in lines))
        d.tail.offset = 0
        d.step(t)
        cur = json.loads((self.tmp / "features/sensors/current.json").read_text())
        self.assertEqual([r["node"] for r in cur["remote"]], ["!abcd1234"])
        self.assertEqual(cur["remote"][0]["values"], {"temperature": -3.0})
        self.assertEqual({s["node"] for s in cur["seen_nodes"]}, {"!abcd1234", "!00000bad"})
        # a rule for the remote node, saved from the portal → reloaded without restart
        (self.tmp / "features/sensors/settings.json").write_text(json.dumps(
            {"remote_nodes": ["!abcd1234"], "rules": [{"id": "barn", "sensor": "mesh-abcd1234", "metric": "temperature",
                                                       "below": 0, "confirm": 1, "board": "family", "mesh": True}]}))
        with open(tele, "a") as fh:
            fh.write(json.dumps({**lines[0], "at": t + 100}) + "\n")
        self.clock.t += 30
        ev = d.step(t + 120)
        self.assertEqual([e["rule"] for e in ev], ["barn"])
        self.assertIn("(mesh radio)", ev[0]["text"])
        out = [json.loads(x) for x in (self.tmp / "outbox.jsonl").read_text().splitlines()]
        self.assertEqual(out[-1]["board"], "family")
        # the untrusted stranger is ignored unless the admin allows both the node and public-channel readings
        self.assertNotIn("!00000bad", d.remote)

    def test_device_failure_and_backoff(self):
        d = self.make()
        t = 1_700_000_000.0
        d.step(t)
        self.devs[0x76].fail = True
        self.tick(d, t + 30)
        bme = next(x for x in d.devices if x.id == "bme280-76")
        self.assertFalse(bme.ok)
        self.assertIn("no answer on the I²C bus", bme.error)
        self.assertTrue(self.devs[0x76].closed)
        due1 = bme.next_due
        self.tick(d, t + 60, 60)
        self.assertGreater(bme.next_due - self.clock.t, due1 - (self.clock.t - 60) - 1)   # interval grew
        self.devs[0x76].fail = False
        self.clock.t += 400
        d.step(t + 500)
        self.assertTrue(bme.ok)

    def test_summary(self):
        d = self.make()
        d.settings["summary_every_h"] = 1
        t = 1_700_000_000.0
        d.step(t)
        text = d.summary_text()
        self.assertIn("µSv/h", text)
        self.assertIn("RH", text)
        msgs = (self.tmp / "outbox.jsonl").read_text().splitlines()
        self.assertTrue(any("Badger sensors: " in m for m in msgs))
        n = len(msgs)
        self.tick(d, t + 600)
        self.assertEqual(len((self.tmp / "outbox.jsonl").read_text().splitlines()), n)   # ≤ 1 per hour

    def test_heartbeat_without_sensors(self):
        import tomllib
        raw = tomllib.loads(f'[portal]\nfeatures_data = "{self.tmp}/f2"\n[sensors]\nauto_detect = false\n'
                            f'telemetry_file = "{self.tmp}/none.jsonl"\n')
        d = sensord.Sensord(raw, i2c_open=self.i2c_open, w1_root=self.w1, sleep=NOSLEEP, mono=self.clock)
        self.addCleanup(d.close)
        p = self.tmp / "f2/sensors/current.json"
        d.step(100.0)
        self.assertEqual(json.loads(p.read_text())["updated"], 100.0)
        self.clock.t += 30
        d.step(130.0)
        self.assertEqual(json.loads(p.read_text())["updated"], 100.0)     # nothing new: not rewritten…
        self.clock.t += 31
        d.step(161.0)
        self.assertEqual(json.loads(p.read_text())["updated"], 161.0)     # …but at least once a minute

    def test_disabled_exits(self):
        cfgp = self.tmp / "p.toml"
        cfgp.write_text("[sensors]\nenabled = false\n")
        r = subprocess.run([sys.executable, str(REPO / "software/sensors/sensord.py"), "--once"],
                           env={**os.environ, "PREPPER_CONFIG": str(cfgp)}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("switched off", r.stderr)


# ─────────────────────────────── portal feature over HTTP ───────────────────

import server  # noqa: E402


class PortalHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sens-http-")).resolve()
        (cls.tmp / "data").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        (feats / "sensors.py").symlink_to(REPO / "software/portal/features/sensors.py")
        cfg = cls.tmp / "prepper.toml"
        cfg.write_text(f"""
[upd]
data_root = "{cls.tmp / 'data'}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{cls.tmp}/upd-state.json"
report_dir = "{cls.tmp}/reports"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
admin_pin = "{server.hash_pin('246810')}"
board_file = "{cls.tmp}/board.jsonl"
family_board_file = "{cls.tmp}/family-board.jsonl"
members_file = "{cls.tmp}/members.json"
settings_file = "{cls.tmp}/portal-settings.json"
mesh_outbox = "{cls.tmp}/mesh-outbox.jsonl"
features_data = "{cls.tmp}/features-data"
ctl = "/nonexistent/prepperctl"
services = {{}}

[sensors]
post_events = false
remote_nodes = ["!abcd1234"]
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        assert [f["id"] for f in server.FEATURES] == ["sensors"], server.FEATURES
        cls.ctx = server.FEATURES[0]["ctx"]
        cls.mod = server.FEATURES[0]["mod"]
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]
        inv = cls.req("POST", "/api/admin/members", {"op": "invite", "name": "Ana"}, admin=cls.admin)[1]
        server._last_post.clear()
        cls.member = cls.req("POST", "/api/members/join", {"code": inv["code"], "name": "Ana"})[1]["key"]
        # a sensord run writing into the feature's data dir
        cls.clock = FakeClock()
        raw = {"portal": {"features_data": str(cls.tmp / "features-data"), "mesh_outbox": str(cls.tmp / "mesh-outbox.jsonl")},
               "sensors": {"auto_detect": True, "telemetry_file": str(cls.tmp / "tele.jsonl"), "remote_nodes": ["!abcd1234"],
                           "device": [{"type": "gmc", "id": "geiger", "port": "/dev/serial/by-id/missing"}]}}
        now = time.time()
        (cls.tmp / "tele.jsonl").write_text(json.dumps({"at": now - 1, "from": "!abcd1234", "name": "Barn<b>", "trusted": True,
                                                         "values": {"temperature": 3.5}}) + "\n" +
                                            json.dumps({"at": now - 1, "from": "!0000beef", "name": "Hidden", "trusted": True,
                                                        "values": {"temperature": 1.0}}) + "\n")
        cls.d = sensord.Sensord(raw, i2c_open=lambda b, a: FakeBME280() if a == 0x76 else (_ for _ in ()).throw(OSError("nack")),
                                serial_open=FakeSerialGMC, w1_root=cls.tmp / "now1", sleep=NOSLEEP, mono=cls.clock)
        cls.d.tail.offset = 0
        cls.d.step(now)

    @classmethod
    def tearDownClass(cls):
        cls.d.close()
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, method, path, body=None, member=None, admin=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if member:
            h["X-Member-Token"] = member
        if admin:
            h["X-Admin-Token"] = admin
        h.update(headers or {})
        try:
            c.request(method, path, raw if raw is not None else (json.dumps(body) if body is not None else None), h)
            r = c.getresponse()
            data = r.read()
        finally:
            c.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def setUp(self):
        server.CFG.raw["sensors"]["public"] = True
        server._last_post.clear()

    def test_public_readings(self):
        st, r = self.req("GET", "/api/sensors")
        self.assertEqual(st, 200)
        self.assertTrue(r["running"])
        ids = {s["id"]: s for s in r["sensors"]}
        self.assertEqual(ids["bme280-76"]["metrics"][0]["metric"], "temperature")
        self.assertFalse(ids["geiger"]["ok"])
        self.assertNotIn("error", ids["geiger"])                  # error details: admin only
        self.assertEqual([x["node"] for x in r["remote"]], ["!abcd1234"])
        self.assertEqual(r["remote"][0]["name"], "Barn<b>")       # data; the page escapes it
        st, r = self.req("GET", "/api/sensors", admin=self.admin)
        self.assertIn("error", {s["id"]: s for s in r["sensors"]}["geiger"])
        self.assertEqual(self.req("GET", "/api/sensors/alerts")[0], 200)
        feats = self.req("GET", "/api/features")[1]["features"]
        self.assertEqual(feats[0]["route"], "/sensors")

    def test_private_mode(self):
        server.CFG.raw["sensors"]["public"] = False
        self.assertEqual(self.req("GET", "/api/sensors")[0], 401)
        self.assertEqual(self.req("GET", "/api/sensors/history?key=bme280-76:temperature")[0], 401)
        self.assertEqual(self.req("GET", "/api/sensors", member=self.member)[0], 200)
        self.assertEqual(self.req("GET", "/api/sensors", admin=self.admin)[0], 200)
        self.assertEqual(self.req("GET", "/api/sensors", member="forged-token")[0], 401)

    def test_history(self):
        st, r = self.req("GET", "/api/sensors/history?key=bme280-76:temperature&range=24h")
        self.assertEqual(st, 200)
        self.assertEqual(len(r["points"]), 1)
        self.assertAlmostEqual(r["points"][0][1], 25.08, places=1)
        for rng in ("7d", "30d", "1y"):
            self.assertEqual(self.req("GET", f"/api/sensors/history?key=bme280-76:temperature&range={rng}")[0], 200)
        for bad in ("key=../../etc:passwd", "key=bme280-76:temperature&range=5y", "key=", "key=A:B",
                    "key=bme280-76:temperature%00", "key=" + "a" * 300 + ":temperature"):
            self.assertEqual(self.req("GET", "/api/sensors/history?" + bad)[0], 400, bad)
        self.assertEqual(self.req("GET", "/api/sensors/history?key=mesh-0000beef:temperature")[0], 404)
        self.assertEqual(self.req("GET", "/api/sensors/history?key=mesh-abcd1234:temperature")[0], 200)
        self.assertEqual(self.req("GET", "/api/sensors/history?key=nosuch:temperature")[1]["points"], [])

    def test_admin_access(self):
        for who in ({}, {"member": self.member}, {"admin": "forged"}):
            self.assertEqual(self.req("GET", "/api/sensors/admin", **who)[0], 401, who)
            self.assertEqual(self.req("POST", "/api/sensors/admin", {"mesh_alerts": False}, **who)[0], 401, who)
            self.assertEqual(self.req("POST", "/api/sensors/test", {}, **who)[0], 401, who)
        st, r = self.req("GET", "/api/sensors/admin", admin=self.admin)
        self.assertEqual(st, 200)
        self.assertIn("!0000beef", {n["node"] for n in r["seen_nodes"]})     # admin sees every radio heard
        self.assertEqual([x["id"] for x in r["default_rules"]], ["flood", "freeze", "heat", "radiation"])

    def test_admin_save_and_validate(self):
        p = self.tmp / "features-data/sensors/settings.json"
        bad = [{"rules": [{"metric": "temperature"}]}, {"remote_nodes": ["evil"]}, {"summary_every_h": 99},
               {"names": {"x": 5, "../y": "z"}}, {"rules": "all"}]
        for b in bad:
            st, r = self.req("POST", "/api/sensors/admin", b, admin=self.admin)
            self.assertEqual(st, 400, b)
            self.assertIn("error", r)
        st, r = self.req("POST", "/api/sensors/admin", raw=b"[1,2]", admin=self.admin)
        self.assertEqual(st, 400)
        try:                                                  # refused before the body is read
            st, r = self.req("POST", "/api/sensors/admin", raw=b"x" * 1_100_000, admin=self.admin)
            self.assertEqual(st, 413)
        except (BrokenPipeError, ConnectionResetError):
            pass
        st, r = self.req("POST", "/api/sensors/admin", {"rules": [{"id": "hot", "metric": "temperature", "above": 30}],
                                                        "names": {"bme280-76": "Porch"}, "remote_nodes": ["!abcd1234", "!0000beef"],
                                                        "evil": "ignored"}, admin=self.admin)
        self.assertEqual(st, 200, r)
        saved = json.loads(p.read_text())
        self.assertEqual(saved["rules"][0]["id"], "hot")
        self.assertNotIn("evil", saved)
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o640")
        self.assertEqual(self.req("GET", "/api/sensors/history?key=mesh-0000beef:temperature")[0], 200)
        st, r = self.req("POST", "/api/sensors/admin", {"rules": None, "remote_nodes": ["!abcd1234"]}, admin=self.admin)
        self.assertNotIn("rules", json.loads(p.read_text()))
        # sensord picks the names up
        self.clock.t += 60
        self.d.step(time.time())
        r = self.req("GET", "/api/sensors")[1]
        self.assertEqual({s["id"]: s["name"] for s in r["sensors"]}["bme280-76"], "Porch")

    def test_posting_to_boards(self):
        data = self.tmp / "features-data/sensors"
        now = time.time()
        (data / "events.jsonl").write_text("")
        (data / "board-posted.json").unlink(missing_ok=True)
        old = {"seq": 1, "kind": "alert", "at": now - 7200, "text": "old one", "board": "community"}
        sl.append_line(data / "events.jsonl", old)
        self.assertEqual(self.mod.post_pending(self.ctx, now), 0)    # first run: history is not replayed
        evs = [{"seq": 10, "kind": "alert", "at": now - 1, "text": "Basement: water <b>detected</b>", "board": "community"},
               {"seq": 11, "kind": "summary", "at": now, "text": "x", "board": "none"},
               {"seq": 12, "kind": "clear", "at": now - 900, "text": "Freezer back to normal", "board": "family"},
               {"seq": 13, "kind": "alert", "at": now - 3 * 86400, "text": "stale", "board": "community"},
               {"seq": 14, "kind": "alert", "at": now, "text": "x", "board": "../../etc"}]
        for e in evs:
            sl.append_line(data / "events.jsonl", e)
        with open(data / "events.jsonl", "a") as fh:
            fh.write("garbage\n{\"seq\": \"x\"}\n")
        self.assertEqual(self.mod.post_pending(self.ctx, now), 1)    # board allows one post per 5 s per sender
        server._last_post.clear()
        self.assertEqual(self.mod.post_pending(self.ctx, now), 1)
        server._last_post.clear()
        self.assertEqual(self.mod.post_pending(self.ctx, now), 0)
        board = [json.loads(x) for x in (self.tmp / "board.jsonl").read_text().splitlines()]
        self.assertEqual(board[-1]["name"], "Badger sensors")
        self.assertTrue(board[-1]["text"].startswith("⚠ Basement: water"))
        fam = [json.loads(x) for x in (self.tmp / "family-board.jsonl").read_text().splitlines()]
        self.assertTrue(fam[-1]["text"].startswith("✓ Freezer back to normal (at "))
        self.assertNotIn("stale", (self.tmp / "board.jsonl").read_text())
        self.assertEqual(json.loads((data / "board-posted.json").read_text())["seq"], 14)
        # no mesh copies from the portal: sensord does that itself
        self.assertNotIn("water", (self.tmp / "mesh-outbox.jsonl").read_text() if (self.tmp / "mesh-outbox.jsonl").exists() else "")

    def test_test_message_rate_limited(self):
        self.mod._last_test[0] = 0
        st, r = self.req("POST", "/api/sensors/test", {"board": "community", "mesh": True}, admin=self.admin)
        self.assertEqual(st, 200, r)
        self.assertTrue(r["mesh"])
        self.assertEqual(self.req("POST", "/api/sensors/test", {}, admin=self.admin)[0], 429)
        self.mod._last_test[0] = 0
        self.assertEqual(self.req("POST", "/api/sensors/test", {"board": "town"}, admin=self.admin)[0], 400)

    def test_stale_daemon(self):
        p = self.tmp / "features-data/sensors/current.json"
        doc = json.loads(p.read_text())
        doc["updated"] = time.time() - 3600
        doc["alerts"] = [{"key": "a:flood", "sensor": "a", "metric": "flood", "level": "danger", "text": "x" * 1000,
                          "since": 1, "remote": False}, "junk",
                         {"key": "mesh-0000beef:temperature", "sensor": "mesh-0000beef", "remote": True, "text": "hidden"}]
        p.write_text(json.dumps(doc))
        try:
            r = self.req("GET", "/api/sensors/alerts")[1]
            self.assertFalse(r["running"])
            self.assertEqual(len(r["alerts"]), 1)
            self.assertLessEqual(len(r["alerts"][0]["text"]), 300)
            p.write_text("{broken")
            r = self.req("GET", "/api/sensors")[1]
            self.assertFalse(r["running"])
            self.assertEqual(r["sensors"], [])
        finally:
            self.clock.t += 60
            self.d.dirty = True
            self.d.next_write = 0
            self.d.step(time.time())

    def test_concurrent_reads(self):
        errs = []

        def hit():
            for _ in range(10):
                st, _ = self.req("GET", "/api/sensors/history?key=bme280-76:temperature&range=24h")
                if st != 200:
                    errs.append(st)
        ts = [threading.Thread(target=hit) for _ in range(6)]
        for t in ts:
            t.start()
        for _ in range(5):
            self.clock.t += 31
            self.d.step(time.time())
        for t in ts:
            t.join()
        self.assertEqual(errs, [])


# ─────────────────────────────── install fragment ───────────────────────────

class InstallFragment(unittest.TestCase):
    def run_frag(self, conf: str, cfgtxt: str | None):
        tmp = Path(tempfile.mkdtemp(prefix="sens-inst-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stubs = Stubs(tmp)
        (tmp / "prepper.toml").write_text(conf)
        if cfgtxt is not None:
            (tmp / "config.txt").write_text(cfgtxt)
        script = f"""
set -euo pipefail
OPT="{REPO}"; CONF="{tmp}/prepper.toml"
step() {{ echo "-- $*"; }}
toml() {{ python3 -c "import tomllib,sys;d=tomllib.load(open('$CONF','rb'))
for k in sys.argv[1].split('.'): d=d.get(k,{{}}) if isinstance(d,dict) else {{}}
print(d if not isinstance(d,(dict,list)) else '')" "$1"; }}
( . "{REPO}/os/install.d/sensors.sh" )
"""
        r = subprocess.run([BASH, "-c", script], env=stubs.env(PREPPER_CFGTXT=str(tmp / "config.txt")),
                           capture_output=True, text=True, timeout=60)
        txt = (tmp / "config.txt").read_text() if (tmp / "config.txt").exists() else None
        return r, txt, stubs

    def test_idempotent_append(self):
        conf = '[sensors]\n[[sensors.device]]\ntype = "jsn_sr04t"\nport = "/dev/ttyAMA0"\nmount_height_cm = 100\n'
        r, txt, stubs = self.run_frag(conf, "# prepper-disk\ndtparam=i2c_arm=on\n[pi4]\narm_boost=1\n")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("[all]\ndtoverlay=w1-gpio,gpiopin=4\n", txt)
        self.assertIn("dtparam=uart0=on", txt)
        self.assertTrue(any(a[:2] == ["enable", "prepper-sensord"] for a in stubs.argvs("systemctl")))
        # second run: nothing added
        tmp = Path(stubs.bin).parent
        r2 = subprocess.run([BASH, "-c", f"""
set -euo pipefail
OPT="{REPO}"; CONF="{tmp}/prepper.toml"
step() {{ :; }}
toml() {{ echo ""; }}
( . "{REPO}/os/install.d/sensors.sh" )
"""], env=stubs.env(PREPPER_CFGTXT=str(tmp / "config.txt")), capture_output=True, text=True, timeout=60)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual((tmp / "config.txt").read_text().count("w1-gpio"), 1)
        self.assertEqual((tmp / "config.txt").read_text().count("[all]"), 1)

    def test_existing_overlay_and_disabled(self):
        r, txt, _ = self.run_frag("[sensors]\nonewire_gpio = 17\n", "dtoverlay=w1-gpio,gpiopin=26\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(txt, "dtoverlay=w1-gpio,gpiopin=26\n")            # left alone, no uart0 either
        r, txt, stubs = self.run_frag("[sensors]\nenabled = false\n", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(txt, "")
        self.assertTrue(any(a[:2] == ["disable", "--now"] for a in stubs.argvs("systemctl")))

    def test_no_config_txt(self):
        r, txt, stubs = self.run_frag("", None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(txt)
        self.assertIn("not a Raspberry Pi", r.stdout)
        self.assertTrue(any(a[:2] == ["enable", "prepper-sensord"] for a in stubs.argvs("systemctl")))


class ConfigExample(unittest.TestCase):
    def test_example_parses_and_uncommented_devices_work(self):
        import tomllib
        main = (REPO / "os/prepper.toml.example").read_text()
        text = main[main.index("# ── Sensor station"):]        # the [sensors] part (the last section of the example)
        cfg = tomllib.loads(text)
        self.assertEqual(sensord.device_specs(cfg["sensors"]), ([], []))
        # uncomment every example table and key: they must all be valid
        import re
        lines = [line[2:] if re.match(r"# (\[\[sensors\.|[a-z_0-9]+ = )", line) else line for line in text.splitlines()]
        full = tomllib.loads("\n".join(lines))
        specs, errors = sensord.device_specs(full["sensors"])
        self.assertEqual(errors, [])
        self.assertEqual({s["type"] for s in specs}, {"float", "jsn_sr04t", "ads1115", "gmc", "geiger_text", "ds18b20", "bme280"})
        self.assertEqual([r["id"] for r in sl.config_rules(full["sensors"], specs)][:1], ["creek"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
