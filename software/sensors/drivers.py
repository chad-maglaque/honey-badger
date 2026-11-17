"""
Sensor drivers for sensord — stdlib only.

  I²C      /dev/i2c-N, fcntl.ioctl(I2C_SLAVE) + os.read/os.write (the same way powerd reads the UPS)
  1-Wire   /sys/bus/w1/devices/28-*/w1_slave (kernel w1-gpio + w1-therm drivers)
  serial   termios raw mode + select() timeouts
  GPIO     the libgpiod command-line tool `gpioget` (v1 or v2, detected like powerd does). The
           stdlib has no binding for the GPIO character device; hand-packing the v2 ioctl
           structs is fragile, and lgpio/gpiod Python modules are not stdlib. One short
           process every few seconds costs nothing on a Pi 5, and powerd already relies on it.

Every driver has .read() → {metric: value} and raises OSError/ValueError on failure; all
hardware access goes through an injectable object (I²C device, serial opener, GPIO reader,
sysfs root) so the tests can run on a laptop.
"""
from __future__ import annotations

import fcntl
import os
import re
import select
import statistics
import subprocess
import termios
import time
import tty
from pathlib import Path

I2C_SLAVE = 0x0703


# ─────────────────────────────── buses ───────────────────────────────────────

class I2CDev:
    """One device on /dev/i2c-N. I2C_SLAVE (not _FORCE): if a kernel driver owns the address
    the ioctl fails with EBUSY instead of us fighting it. Each fd carries its own address, so
    powerd/panel talking to other addresses on the same bus can't confuse our register pointer."""

    def __init__(self, bus: int, addr: int):
        self.fd = os.open(f"/dev/i2c-{int(bus)}", os.O_RDWR)
        try:
            fcntl.ioctl(self.fd, I2C_SLAVE, int(addr))
        except OSError:
            os.close(self.fd)
            raise

    def write(self, data: bytes) -> None:
        if os.write(self.fd, bytes(data)) != len(data):
            raise OSError("short I2C write")

    def read(self, n: int) -> bytes:
        data = os.read(self.fd, n)
        if len(data) != n:
            raise OSError(f"short I2C read ({len(data)}/{n} bytes)")
        return data

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def reg_read(dev, reg: int, n: int) -> bytes:
    dev.write(bytes([reg]))
    return dev.read(n)


def reg_write(dev, reg: int, val: int) -> None:
    dev.write(bytes([reg, val & 0xFF]))


BAUDS = {1200: termios.B1200, 2400: termios.B2400, 4800: termios.B4800, 9600: termios.B9600,
         19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600, 115200: termios.B115200}


class Serial:
    """A serial port in raw 8N1 mode with timeouts. Opened per poll and closed again, so an
    unplugged/replugged USB device just works on the next poll."""

    def __init__(self, path: str, baud: int = 9600):
        if baud not in BAUDS:
            raise ValueError(f"unsupported baud rate {baud}")
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(self.fd)
            attrs = termios.tcgetattr(self.fd)
            attrs[2] |= termios.CLOCAL | termios.CREAD
            attrs[2] &= ~(termios.CSTOPB | termios.PARENB)
            attrs[4] = attrs[5] = BAUDS[baud]
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except (OSError, termios.error) as e:
            os.close(self.fd)
            raise OSError(f"cannot set up {path}: {e}") from None

    def write(self, data: bytes) -> None:
        end = time.monotonic() + 1.0
        while data:
            _, w, _ = select.select([], [self.fd], [], max(0.0, end - time.monotonic()))
            if not w:
                raise OSError("serial write timed out")
            n = os.write(self.fd, data)
            data = data[n:]

    def read(self, n: int, timeout: float, quiet: float | None = None) -> bytes:
        """Up to n bytes within `timeout` s; with `quiet`, also stop once nothing more has
        arrived for that long after the first byte."""
        buf = b""
        end = time.monotonic() + timeout
        while len(buf) < n:
            left = end - time.monotonic()
            if left <= 0:
                break
            wait = min(left, quiet) if (quiet and buf) else left
            r, _, _ = select.select([self.fd], [], [], wait)
            if not r:
                if quiet and buf:
                    break
                continue
            try:
                chunk = os.read(self.fd, n - len(buf))
            except BlockingIOError:
                continue
            if not chunk:
                raise OSError("serial device went away")
            buf += chunk
        return buf

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def _signed(v: int, bits: int) -> int:
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def crc8_sensirion(data: bytes) -> int:
    """CRC-8, polynomial 0x31, init 0xFF (SHT3x/SHT4x datasheets; 0xBEEF → 0x92)."""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


# ─────────────────────────────── BME280 / BMP280 ────────────────────────────

def bme280_parse_calib(b1: bytes, b2: bytes | None) -> dict:
    """b1 = 26 bytes from 0x88 (T1..P9, H1 at 0xA1), b2 = 7 bytes from 0xE1 (H2..H6, BME280 only).
    Layout from the BME280 datasheet, table 16."""
    u16 = lambda i: b1[i] | (b1[i + 1] << 8)              # noqa: E731
    s16 = lambda i: _signed(u16(i), 16)                    # noqa: E731
    c = {"T1": u16(0), "T2": s16(2), "T3": s16(4), "P1": u16(6)}
    for k in range(2, 10):
        c[f"P{k}"] = s16(6 + 2 * (k - 1))
    if b2 is not None:
        c["H1"] = b1[25]
        c["H2"] = _signed(b2[0] | (b2[1] << 8), 16)
        c["H3"] = b2[2]
        c["H4"] = _signed((b2[3] << 4) | (b2[4] & 0x0F), 12)
        c["H5"] = _signed((b2[5] << 4) | (b2[4] >> 4), 12)
        c["H6"] = _signed(b2[6], 8)
    return c


def bme280_compensate(c: dict, adc_t: int, adc_p: int, adc_h: int | None = None):
    """The datasheet's double-precision compensation (BME280 datasheet §8.1; identical T/P to
    BMP280 §8.1). Returns (°C, Pa, %RH or None)."""
    var1 = (adc_t / 16384.0 - c["T1"] / 1024.0) * c["T2"]
    var2 = (adc_t / 131072.0 - c["T1"] / 8192.0) ** 2 * c["T3"]
    t_fine = int(var1 + var2)
    temp = (var1 + var2) / 5120.0

    var1 = t_fine / 2.0 - 64000.0
    var2 = var1 * var1 * c["P6"] / 32768.0
    var2 = var2 + var1 * c["P5"] * 2.0
    var2 = var2 / 4.0 + c["P4"] * 65536.0
    var1 = (c["P3"] * var1 * var1 / 524288.0 + c["P2"] * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * c["P1"]
    if var1 == 0:
        press = 0.0                                      # avoid division by zero (datasheet)
    else:
        p = 1048576.0 - adc_p
        p = (p - var2 / 4096.0) * 6250.0 / var1
        var1 = c["P9"] * p * p / 2147483648.0
        var2 = p * c["P8"] / 32768.0
        press = p + (var1 + var2 + c["P7"]) / 16.0

    hum = None
    if adc_h is not None and "H1" in c:
        h = t_fine - 76800.0
        h = (adc_h - (c["H4"] * 64.0 + c["H5"] / 16384.0 * h)) * (
            c["H2"] / 65536.0 * (1.0 + c["H6"] / 67108864.0 * h * (1.0 + c["H3"] / 67108864.0 * h)))
        h = h * (1.0 - c["H1"] * h / 524288.0)
        hum = max(0.0, min(100.0, h))
    return temp, press, hum


class BME280:
    """Bosch BME280 (T, RH, p) or BMP280 (T, p) in forced mode: one measurement per poll, then
    the chip sleeps — no self-heating, ~1 µA between readings."""
    CHIP_IDS = {0x60: "bme280", 0x58: "bmp280", 0x56: "bmp280", 0x57: "bmp280"}

    def __init__(self, dev, altitude_m: float = 0.0, sleep=time.sleep):
        self.dev, self.sleep, self.altitude = dev, sleep, float(altitude_m or 0)
        cid = reg_read(dev, 0xD0, 1)[0]
        if cid not in self.CHIP_IDS:
            raise ValueError(f"not a BME280/BMP280 (chip id {cid:#04x})")
        self.kind = self.CHIP_IDS[cid]
        self.humidity = self.kind == "bme280"
        reg_write(dev, 0xE0, 0xB6)                       # soft reset
        self.sleep(0.005)
        for _ in range(20):                              # wait for the NVM copy (status.im_update)
            if not reg_read(dev, 0xF3, 1)[0] & 0x01:
                break
            self.sleep(0.002)
        b1 = reg_read(dev, 0x88, 26)
        b2 = reg_read(dev, 0xE1, 7) if self.humidity else None
        self.calib = bme280_parse_calib(b1, b2)
        if self.calib["T1"] == 0 or self.calib["P1"] == 0:
            raise ValueError("calibration data is blank")
        reg_write(dev, 0xF5, 0x00)                       # config: no IIR filter, SPI off

    def read(self) -> dict:
        if self.humidity:
            reg_write(self.dev, 0xF2, 0x01)              # osrs_h ×1 (takes effect with the ctrl_meas write)
        reg_write(self.dev, 0xF4, 0x25)                  # osrs_t ×1, osrs_p ×1, forced mode
        self.sleep(0.01)
        for _ in range(25):
            if not reg_read(self.dev, 0xF3, 1)[0] & 0x08:
                break
            self.sleep(0.004)
        else:
            raise OSError("measurement did not finish")
        d = reg_read(self.dev, 0xF7, 8 if self.humidity else 6)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        adc_h = (d[6] << 8) | d[7] if self.humidity else None
        if adc_t == 0x80000 or adc_p == 0x80000:
            raise OSError("sensor returned the 'skipped' value")
        t, p, h = bme280_compensate(self.calib, adc_t, adc_p, adc_h)
        hpa = p / 100.0
        if self.altitude:                                 # report sea-level pressure, like forecasts do
            hpa = hpa / (1.0 - self.altitude / 44330.0) ** 5.255
        out = {"temperature": round(t, 2), "pressure": round(hpa, 2)}
        if h is not None:
            out["humidity"] = round(h, 1)
        return out


# ─────────────────────────────── Sensirion SHT3x / SHT4x ────────────────────

def _sht_words(data: bytes) -> tuple[int, int]:
    if len(data) != 6 or crc8_sensirion(data[0:2]) != data[2] or crc8_sensirion(data[3:5]) != data[5]:
        raise OSError("checksum error (loose wire or long cable?)")
    return (data[0] << 8) | data[1], (data[3] << 8) | data[4]


class SHT3x:
    def __init__(self, dev, sleep=time.sleep):
        self.dev, self.sleep = dev, sleep
        dev.write(b"\xf3\x2d")                           # read status register: 2 bytes + CRC
        st = dev.read(3)
        if crc8_sensirion(st[:2]) != st[2]:
            raise ValueError("not an SHT3x")

    def read(self) -> dict:
        self.dev.write(b"\x24\x00")                      # single shot, high repeatability, no clock stretching
        self.sleep(0.016)
        t, rh = _sht_words(self.dev.read(6))
        return {"temperature": round(-45 + 175 * t / 65535.0, 2), "humidity": round(100 * rh / 65535.0, 1)}


class SHT4x:
    def __init__(self, dev, sleep=time.sleep):
        self.dev, self.sleep = dev, sleep
        dev.write(b"\x89")                               # read serial number: 2 words + CRCs
        self.sleep(0.002)
        _sht_words(dev.read(6))

    def read(self) -> dict:
        self.dev.write(b"\xfd")                          # measure T & RH, high precision
        self.sleep(0.010)
        t, rh = _sht_words(self.dev.read(6))
        return {"temperature": round(-45 + 175 * t / 65535.0, 2),
                "humidity": round(max(0.0, min(100.0, -6 + 125 * rh / 65535.0)), 1)}


# ─────────────────────────────── ADS1115 + pressure transducer ───────────────

ADS_FSR = {6.144: 0, 4.096: 1, 2.048: 2, 1.024: 3, 0.512: 4, 0.256: 5}


class ADS1115Level:
    """Water level from a submersible pressure transducer read by an ADS1115 ADC (single-shot,
    single-ended input AINn). Linear: v_zero → 0 cm of water above the sensor, v_full →
    full_scale_cm; plus offset_cm (how high the transducer sits above your zero mark).
    `divider` multiplies the measured voltage back (e.g. 1.5 for a 10k/20k divider on a 0.5–4.5 V
    sensor). Default suits a 4–20 mA transducer across a 150 Ω resistor: 0.6–3.0 V."""

    def __init__(self, dev, channel: int = 0, fsr: float = 4.096, v_zero: float = 0.6, v_full: float = 3.0,
                 full_scale_cm: float = 500.0, offset_cm: float = 0.0, divider: float = 1.0, sleep=time.sleep):
        if channel not in (0, 1, 2, 3):
            raise ValueError("ADS1115 channel must be 0–3")
        if fsr not in ADS_FSR:
            raise ValueError("ADS1115 range must be one of 6.144, 4.096, 2.048, 1.024, 0.512, 0.256")
        if v_full <= v_zero:
            raise ValueError("v_full must be above v_zero")
        self.dev, self.sleep = dev, sleep
        self.channel, self.fsr = channel, fsr
        self.v_zero, self.v_full, self.full, self.offset, self.divider = v_zero, v_full, full_scale_cm, offset_cm, divider
        reg_read(dev, 0x01, 2)                           # is anything there?

    def volts(self) -> float:
        cfg = (1 << 15) | ((4 + self.channel) << 12) | (ADS_FSR[self.fsr] << 9) | (1 << 8) | (4 << 5) | 0x03
        self.dev.write(bytes([0x01, cfg >> 8, cfg & 0xFF]))
        for _ in range(20):
            self.sleep(0.009)                            # 128 samples/s → ~8 ms per conversion
            if reg_read(self.dev, 0x01, 2)[0] & 0x80:
                break
        else:
            raise OSError("conversion did not finish")
        raw = _signed(int.from_bytes(reg_read(self.dev, 0x00, 2), "big"), 16)
        return raw * self.fsr / 32768.0 * self.divider

    def read(self) -> dict:
        v = self.volts()
        span = self.v_full - self.v_zero
        if v < self.v_zero - 0.1 * span:
            raise OSError(f"no signal from the pressure sensor ({v:.2f} V) — check its wiring and power")
        level = (v - self.v_zero) / span * self.full + self.offset
        return {"water_level": round(level, 1)}


# ─────────────────────────────── DS18B20 (1-Wire) ───────────────────────────

W1_FAMILIES = ("10", "22", "28", "3b", "42")             # DS18S20, DS1822, DS18B20, MAX31850, DS28EA00


class DS18B20:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict:
        try:
            text = (self.path / "w1_slave").read_text(errors="replace")
        except OSError as e:
            raise OSError(f"probe not found ({e.strerror or e})") from None
        lines = text.strip().splitlines()
        if len(lines) < 2 or not lines[0].strip().endswith("YES"):
            raise OSError("checksum error (loose wire or missing 4.7 kΩ pull-up?)")
        m = re.search(r"t=(-?\d+)", lines[1])
        if not m:
            raise OSError("no temperature in the probe's answer")
        milli = int(m.group(1))
        if milli == 85000:                               # the power-on value: conversion didn't happen
            raise OSError("probe returned its power-on value (85 °C) — weak power or wiring")
        if not -55000 <= milli <= 125000:
            raise OSError("temperature out of the probe's range")
        return {"temperature": milli / 1000.0}


def w1_probes(root: Path) -> list[Path]:
    try:
        return sorted(p for p in Path(root).iterdir()
                      if re.fullmatch(r"[0-9a-f]{2}-[0-9a-f]{12}", p.name) and p.name[:2] in W1_FAMILIES)
    except OSError:
        return []


# ─────────────────────────────── GPIO float switch ──────────────────────────

def run_cmd(argv: list[str]) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


class GpioReader:
    """Reads one input with whichever gpioget is installed, asking for the internal pull-up when
    the tool supports it (keep the external 10 kΩ pull-up anyway: libgpiod v2 releases the line
    after each read). Detection order and the Pi 5 chip naming follow powerd's GpioPin."""

    def __init__(self, pin: int, cmd: str = "", runner=run_cmd):
        self.pin, self.run = int(pin), runner
        self.fixed = cmd.split() if cmd else None
        self.argv: list[str] | None = None

    def candidates(self) -> list[list[str]]:
        if self.fixed:
            return [self.fixed]
        ver = self.run(["gpioget", "--version"]) or ""
        name, pin = f"GPIO{self.pin}", str(self.pin)
        if "v1." in ver:
            found = (self.run(["gpiofind", name]) or "").split()
            chips = [found] if len(found) == 2 else [["gpiochip4", pin], ["gpiochip0", pin]]
            return [["gpioget", "--bias=pull-up", *c] for c in chips] + [["gpioget", *c] for c in chips]
        return [["gpioget", "--numeric", "--bias=pull-up", name], ["gpioget", "--numeric", name],
                ["gpioget", "--numeric", "-c", "gpiochip0", pin], ["gpioget", "--numeric", "-c", "gpiochip4", pin]]

    @staticmethod
    def parse(out: str | None) -> int | None:
        tok = out.split()[-1].strip('"').lower() if out and out.split() else ""
        tok = tok.split("=")[-1]
        return {"0": 0, "1": 1, "inactive": 0, "active": 1}.get(tok)

    def read(self) -> int:
        if self.argv:
            v = self.parse(self.run(self.argv))
            if v is not None:
                return v
            self.argv = None
        for argv in self.candidates():
            v = self.parse(self.run(argv))
            if v is not None:
                self.argv = argv
                return v
        raise OSError(f"cannot read GPIO {self.pin} (is gpioget installed and the user in group gpio?)")


class FloatSwitch:
    """A float switch between a GPIO and ground (10 kΩ pull-up to 3.3 V). wet_when = "high" means
    the switch OPENS when water lifts the float: a cut or unplugged cable then reads as wet — a
    false alarm instead of silence. Most float switches do this when mounted upside down."""

    def __init__(self, reader, wet_when: str = "high", samples: int = 3, sleep=time.sleep):
        if wet_when not in ("high", "low"):
            raise ValueError('wet_when must be "high" or "low"')
        self.reader, self.wet_level, self.samples, self.sleep = reader, 1 if wet_when == "high" else 0, samples, sleep

    def read(self) -> dict:
        vals = []
        for i in range(self.samples):                    # majority of 3 quick reads: ripples on the water
            if i:
                self.sleep(0.05)
            vals.append(self.reader.read())
        wet = sum(1 for v in vals if v == self.wet_level) * 2 > len(vals)
        return {"flood": 1.0 if wet else 0.0}


# ─────────────────────────────── JSN-SR04T ultrasonic (UART) ────────────────

def jsn_frames(buf: bytes) -> list[int]:
    """Distances (mm) from 0xFF H L SUM frames, SUM = (0xFF + H + L) & 0xFF."""
    out, i = [], 0
    while i + 3 < len(buf):
        if buf[i] == 0xFF and (0xFF + buf[i + 1] + buf[i + 2]) & 0xFF == buf[i + 3]:
            out.append((buf[i + 1] << 8) | buf[i + 2])
            i += 4
        else:
            i += 1
    return out


class JSNSR04T:
    """Waterproof ultrasonic distance sensor in UART mode (JSN-SR04T in a serial mode, AJ-SR04M,
    DFRobot A02YYUW). Sends the 0x55 trigger (the "on request" mode) and also accepts the
    auto-output stream. Level = mount_height_cm − distance; median of several pings."""

    def __init__(self, port: str, mount_height_cm: float, baud: int = 9600, opener=Serial,
                 min_mm: int = 200, max_mm: int = 6000, sleep=time.sleep):
        if not mount_height_cm or mount_height_cm <= 0:
            raise ValueError("set mount_height_cm: the distance from the sensor face down to your zero mark")
        self.port, self.baud, self.opener = port, baud, opener
        self.mount, self.min_mm, self.max_mm, self.sleep = float(mount_height_cm), min_mm, max_mm, sleep

    def read(self) -> dict:
        s = self.opener(self.port, self.baud)
        try:
            dists: list[int] = []
            for _ in range(5):
                s.write(b"\x55")
                dists += jsn_frames(s.read(64, timeout=0.3, quiet=0.06))
                if len(dists) >= 5:
                    break
                self.sleep(0.06)
        finally:
            s.close()
        good = [d for d in dists if self.min_mm <= d <= self.max_mm]
        if not good:
            raise OSError("no echo (too close, too far, or the sensor isn't answering)" if dists
                          else "no answer from the ultrasonic sensor (check TX/RX and the mode resistor)")
        dist_cm = statistics.median(good) / 10.0
        return {"water_level": round(self.mount - dist_cm, 1)}


# ─────────────────────────────── Geiger counters ────────────────────────────

class GQGMC:
    """GQ Electronics GMC-300/320/500/600 over USB serial (GQ-RFC1201 protocol). <GETCPM>> answers
    2 bytes (older 300/320 firmware) or 4 bytes (500/600, newer firmware), MSB first.
    factor: µSv/h per CPM — 0.0065 for the M4011 tube in the GMC-300/320."""

    def __init__(self, port: str, baud: int = 115200, factor: float = 0.0065, cpm_bytes: int = 0, opener=Serial):
        self.port, self.baud, self.factor, self.nbytes, self.opener = port, baud, float(factor), int(cpm_bytes), opener

    def read(self) -> dict:
        s = self.opener(self.port, self.baud)
        try:
            s.write(b"<HEARTBEAT0>>")                    # stop once-a-second streaming if it was on
            s.read(64, timeout=0.15, quiet=0.05)         # and drop whatever was in flight
            s.write(b"<GETCPM>>")
            want = self.nbytes or 4
            data = s.read(want, timeout=1.0, quiet=0.2)
        finally:
            s.close()
        if self.nbytes and len(data) != self.nbytes or len(data) not in (2, 4):
            raise OSError(f"unexpected answer from the Geiger counter ({len(data)} bytes)")
        cpm = int.from_bytes(data, "big")
        if cpm > 10_000_000:
            raise OSError("implausible count")
        return {"cpm": float(cpm), "radiation": round(cpm * self.factor, 4)}


CPM_RE = re.compile(r"\bCPM\b[\s,:=]+(\d+(?:\.\d+)?)", re.I)
USV_RE = re.compile(r"(?:uSv|µSv|μSv)(?:\s*/\s*h(?:r|our)?)?[\s,:=]+(\d+(?:\.\d+)?)", re.I)


def parse_geiger_line(line: str, factor: float | None, cpm_re=CPM_RE, usv_re=USV_RE) -> dict | None:
    """'CPS, 0, CPM, 13, uSv/hr, 0.07, SLOW' → {cpm: 13, radiation: 0.07}. With an explicit factor,
    µSv/h is computed from CPM (the device's own conversion may be for another tube)."""
    m = cpm_re.search(line)
    if not m:
        return None
    cpm = float(m.group(1))
    out = {"cpm": cpm}
    u = usv_re.search(line)
    if factor:
        out["radiation"] = round(cpm * factor, 4)
    elif u:
        out["radiation"] = float(u.group(1))
    return out


class TextGeiger:
    """Geiger counters that print a text line every second or so (MightyOhm kit, many DIY and
    Arduino-based boards). Reads lines for up to `listen_s` and keeps the newest that parses.
    factor (µSv/h per CPM): used when set, or when the line has no µSv/h value (default 0.0057,
    the SBM-20 tube)."""

    def __init__(self, port: str, baud: int = 9600, factor: float | None = None, listen_s: float = 2.5,
                 cpm_regex: str = "", opener=Serial):
        self.port, self.baud, self.factor, self.listen, self.opener = port, baud, factor, listen_s, opener
        self.cpm_re = re.compile(cpm_regex, re.I) if cpm_regex else CPM_RE

    def read(self) -> dict:
        s = self.opener(self.port, self.baud)
        try:
            buf = s.read(4096, timeout=self.listen)
        finally:
            s.close()
        best = None
        for line in buf.decode("ascii", "replace").splitlines()[-50:]:
            rec = parse_geiger_line(line[:300], self.factor, self.cpm_re)
            if rec:
                best = rec
        if not best:
            raise OSError("no readable line from the Geiger counter (check the port and baud rate)")
        if "radiation" not in best:
            best["radiation"] = round(best["cpm"] * 0.0057, 4)
        return best
