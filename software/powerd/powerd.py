#!/usr/bin/env python3
"""
powerd — battery monitor, automatic power modes and clean shutdown.

Reads the UPS fuel gauge over raw I²C (no libraries), writes
/run/prepper/power.json for the launcher and front panel, and:
  * drops to Endurance mode when running on battery below auto_endurance_percent
  * returns to the default mode when external power comes back
  * shuts down cleanly when the battery is nearly empty (protects the SSD)
  * switches the 40 mm case fan with CPU temperature (hysteresis; hotter
    threshold in Endurance so it stays off longer on battery). The Pi's own
    Active Cooler is controlled separately by the Pi firmware.

It never acts on a guess: when it cannot tell whether external power is
present (power-loss pin unreadable, gauge missing), it reports "unknown" and
neither switches modes nor shuts down.

Backends
  x120x  Geekworm X1200/X1202/X1206 — MAX17043/MAX17048 at 0x36, power-loss on GPIO6
  ina219 Waveshare UPS HAT (B)-style — INA219 at 0x42, 2S pack, SoC from voltage
  mock   simulated battery for testing on a laptop
  none   no battery: reports mains power only

Input meter ([power] input_monitor) on the SOLAR / DC IN line (8–30 V):
  ina228 (85 V, recommended) · ina226 (36 V) · ina219 (26 V max)
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

I2C_SLAVE = 0x0703
RUN = Path("/run/prepper")
STATE = Path("/var/lib/prepper")
WATTS = {"endurance": 4.5, "balanced": 6.5, "full": 9.5}   # measured-ish Pi 5 + NVMe + Wi-Fi
MODES = tuple(WATTS)
RETRY_S = 60                    # a gauge or meter missing at startup is looked for again this often


class I2C:
    def __init__(self, bus: int, addr: int):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self.fd, I2C_SLAVE, addr)
        except OSError:
            os.close(self.fd)
            raise

    def read(self, reg: int, n: int) -> bytes:
        os.write(self.fd, bytes([reg]))
        data = os.read(self.fd, n)
        if len(data) != n:
            raise OSError(f"short I2C read from register {reg:#04x} ({len(data)}/{n} bytes)")
        return data

    def read16(self, reg: int) -> int:
        return int.from_bytes(self.read(reg, 2), "big")

    def close(self) -> None:
        os.close(self.fd)


def signed(v: int, bits: int) -> int:
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def read_cmd(argv: list[str]) -> str | None:
    """stdout of a command, or None if it could not run or exited non-zero
    (an empty string from a failed command must never read as "0")."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


LEGACY_PLD_CMD = "gpioget --numeric gpiochip0 6"     # old example default; wrong on libgpiod v1 and v2


class GpioPin:
    """Reads one input pin with whichever gpioget is installed. libgpiod v1
    (Bookworm) wants `gpioget CHIP OFFSET`; v2 (Trixie) wants
    `gpioget --numeric -c CHIP OFFSET` or a line name. The Pi 5's header pins
    are named GPIOn on whichever chip number the kernel gave the RP1, so the
    name is tried first. read() → 1, 0, or None (unknown)."""

    def __init__(self, pin: int, cmd: str = ""):
        self.pin = pin
        self.fixed = cmd.split() if cmd and cmd.strip() != LEGACY_PLD_CMD else None
        self.argv: list[str] | None = None          # the variant that worked, once found

    def candidates(self) -> list[list[str]]:
        if self.fixed:
            return [self.fixed]
        ver = read_cmd(["gpioget", "--version"]) or ""
        name, pin = f"GPIO{self.pin}", str(self.pin)
        if "v1." in ver:
            found = (read_cmd(["gpiofind", name]) or "").split()
            chips = [found] if len(found) == 2 else []
            return [["gpioget", *c] for c in chips] + [["gpioget", "gpiochip0", pin], ["gpioget", "gpiochip4", pin]]
        return [["gpioget", "--numeric", name], ["gpioget", "--numeric", "-c", "gpiochip0", pin],
                ["gpioget", "--numeric", "-c", "gpiochip4", pin]]

    @staticmethod
    def parse(out: str | None) -> int | None:
        tok = out.split()[-1] if out and out.split() else ""
        return int(tok) if tok in ("0", "1") else None

    def read(self) -> int | None:
        if self.argv:
            v = self.parse(read_cmd(self.argv))
            if v is not None:
                return v
            self.argv = None                         # stopped working: detect again
        for argv in self.candidates():
            v = self.parse(read_cmd(argv))
            if v is not None:
                self.argv = argv
                return v
        return None


class X120x:
    def __init__(self, cfg: dict, i2c=None):
        self.i2c = i2c or I2C(int(cfg.get("i2c_bus", 1)), 0x36)
        try:
            ver = self.i2c.read16(0x08)
        except OSError:
            self.i2c.close()                   # no HAT: Retrying tries again every minute — don't leak the fd
            raise
        self.is_17048 = (ver & 0xFFF0) == 0x0010
        self.pld = GpioPin(int(cfg.get("pld_gpio", 6)), str(cfg.get("pld_cmd", "") or ""))

    def read(self) -> dict:
        raw_v = self.i2c.read16(0x02)
        volts = raw_v * 78.125e-6 if self.is_17048 else (raw_v >> 4) * 1.25e-3
        soc = self.i2c.read16(0x04)
        pct = min(100.0, (soc >> 8) + (soc & 0xFF) / 256)
        pld = self.pld.read()
        return {"percent": round(pct, 1), "voltage": round(volts, 3),
                "external": None if pld is None else pld == 1}


class INA219:
    def __init__(self, cfg: dict, i2c=None):
        self.i2c = i2c or I2C(int(cfg.get("i2c_bus", 1)), int(cfg.get("ina219_address", 0x42)))
        self.cells = int(cfg.get("cells_series", 2))

    def read(self) -> dict:
        bus_v = (self.i2c.read16(0x02) >> 3) * 0.004
        shunt = signed(self.i2c.read16(0x01), 16)
        current_ma = shunt * 0.01 / 0.1           # 10 µV/LSB across a 0.1 Ω shunt
        per_cell = bus_v / self.cells
        pct = max(0.0, min(100.0, (per_cell - 3.2) / (4.15 - 3.2) * 100))
        return {"percent": round(pct, 1), "voltage": round(bus_v, 3), "external": current_ma > 20,
                "current_ma": round(current_ma)}


class Mock:
    def __init__(self, cfg: dict):
        self.pct, self.t = 87.0, time.monotonic()

    def read(self) -> dict:
        self.pct = max(0.0, self.pct - (time.monotonic() - self.t) / 600)
        self.t = time.monotonic()
        return {"percent": round(self.pct, 1), "voltage": 3.6 + self.pct / 200, "external": False}


class NoBattery:
    def __init__(self, cfg: dict):
        pass

    def read(self) -> dict:
        return {"percent": None, "voltage": None, "external": True}


BACKENDS = {"x120x": X120x, "ina219": INA219, "mock": Mock, "none": NoBattery}


class InputMonitor:
    """Optional meter on the SOLAR / DC IN line: volts, amps and watts coming in.

    Reads the shunt-voltage and bus-voltage registers directly and computes
    current = Vshunt / input_shunt_ohms, so no calibration register has to be
    programmed: every chip here powers up converting both continuously.
      ina228  85 V   VSHUNT 0x04, VBUS 0x05: 3-byte registers, value in bits 23..4
                     (shunt signed, 312.5 nV/LSB at ADCRANGE=0; bus 195.3125 µV/LSB)
      ina226  36 V   shunt 0x01 signed 2.5 µV/LSB, bus 0x02 1.25 mV/LSB
      ina219  26 V   shunt 0x01 signed 10 µV/LSB, bus 0x02 bits 15..3, 4 mV/LSB
    The INA219's 26 V limit is below a 30 V solar input — use the INA228."""

    CHIPS = ("ina228", "ina226", "ina219")

    def __init__(self, cfg: dict, i2c=None):
        self.chip = str(cfg.get("input_monitor", "none")).lower()
        if self.chip not in self.CHIPS:
            raise ValueError(f"input_monitor must be one of {', '.join(self.CHIPS)} or none")
        self.shunt = float(cfg.get("input_shunt_ohms", 0.1))
        if self.shunt <= 0:
            raise ValueError("input_shunt_ohms must be > 0")
        self.i2c = i2c or I2C(int(cfg.get("i2c_bus", 1)), int(cfg.get("input_i2c_address", 0x40)))

    def volts(self) -> tuple[float, float]:
        """(shunt volts, bus volts)."""
        if self.chip == "ina228":
            vshunt = signed(int.from_bytes(self.i2c.read(0x04, 3), "big") >> 4, 20) * 312.5e-9
            vbus = (int.from_bytes(self.i2c.read(0x05, 3), "big") >> 4) * 195.3125e-6
        elif self.chip == "ina226":
            vshunt = signed(self.i2c.read16(0x01), 16) * 2.5e-6
            vbus = self.i2c.read16(0x02) * 1.25e-3
        else:
            vshunt = signed(self.i2c.read16(0x01), 16) * 10e-6
            vbus = (self.i2c.read16(0x02) >> 3) * 4e-3
        return vshunt, vbus

    def read(self) -> dict:
        vshunt, volts = self.volts()
        amps = max(0.0, vshunt / self.shunt)
        return {"input_v": round(volts, 2), "input_a": round(amps, 3), "input_w": round(volts * amps, 1)}


class Retrying:
    """A gauge or meter that may not be there yet (HAT seated badly, i2c-dev
    not loaded until the next boot, sensor plugged in later): tries to open it
    every RETRY_S instead of giving up at startup."""

    def __init__(self, name: str, factory):
        self.name, self.factory = name, factory
        self.dev, self.next_try, self.warned = None, 0.0, False

    def get(self):
        if self.dev is None and time.monotonic() >= self.next_try:
            try:
                self.dev = self.factory()
                print(f"powerd: {self.name} found", file=sys.stderr)
            except (OSError, ValueError) as e:
                self.next_try = time.monotonic() + RETRY_S
                if not self.warned:
                    print(f"powerd: {self.name} unavailable ({e}); retrying every {RETRY_S} s", file=sys.stderr)
                    self.warned = True
        return self.dev


class CaseFan:
    """On/off case fan through a logic-level MOSFET on a GPIO, driven with the Pi 5 `pinctrl` tool."""

    def __init__(self, cfg: dict, runner=subprocess.run):
        self.pin = int(cfg.get("case_fan_pin", 18))
        self.on_c = float(cfg.get("fan_on_c", 55))
        self.off_c = float(cfg.get("fan_off_c", 47))
        if self.off_c > self.on_c:                     # a swapped pair would make the fan flap
            self.on_c, self.off_c = self.off_c, self.on_c
        self.endurance_extra = float(cfg.get("fan_endurance_extra_c", 7))
        self.state: bool | None = None
        self.runner = runner                            # test seam

    def set(self, on: bool) -> None:
        if on != self.state:
            try:
                r = self.runner(["pinctrl", "set", str(self.pin), "op", "dh" if on else "dl"],
                                check=False, timeout=5, capture_output=True)
                ok = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            # Only remember a state the pin actually took; otherwise try again next round.
            self.state = on if ok else None

    def update(self, temp_c: float | None, mode: str) -> bool | None:
        if temp_c is None:
            self.set(True)                     # can't read the temperature: fail safe, run the fan
            return True
        # (This cannot cover a hung Pi: the MOSFET gate's pull-down turns the
        # fan off whenever the GPIO stops being driven high.)
        extra = self.endurance_extra if mode == "endurance" else 0
        if mode == "full" or temp_c >= self.on_c + extra:
            self.set(True)
        elif temp_c <= self.off_c + extra or self.state is None:
            # Below the off point — or in the band with no known state yet
            # (startup): off, until it climbs past the on point.
            self.set(False)
        return self.state


def cpu_temp() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def mode_now() -> str:
    try:
        return (STATE / "power-mode").read_text().strip()
    except OSError:
        return "balanced"


CTL = "/opt/prepper/bin/prepperctl"


def prepperctl(*args: str) -> None:
    try:
        subprocess.run([CTL, *args], check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"powerd: prepperctl {' '.join(args)} failed: {e}", file=sys.stderr)


def set_mode_auto(mode: str) -> None:
    """An automatic switch: applied, but not saved as the user's choice."""
    prepperctl("power-mode", mode, "--auto")


def restore_user_mode() -> None:
    prepperctl("power-restore")


def poweroff() -> None:
    subprocess.run(["systemctl", "poweroff"], check=False)


def write_json(path: Path, data: dict) -> None:
    """Atomic, and safe in /run/prepper, which user prepper can also write:
    a fixed temp name there could be a planted symlink that root writes through."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class Policy:
    """What to do with one reading. Acts only on a known battery level AND a
    known power source: "unknown" never switches modes or shuts down.
    step() → "endurance" | "restore" | "poweroff" | None."""

    def __init__(self, cfg: dict, state: Path | None = None):
        self.shutdown_pct = float(cfg.get("shutdown_percent", 6))
        self.auto_end = float(cfg.get("auto_endurance_percent", 30))
        self.low_reads = 0
        # prepperctl power-mode --auto leaves this marker (and power-restore /
        # a user's choice removes it), so an automatic switch survives a
        # powerd restart and is undone when power returns.
        self.auto_switched = ((state or STATE) / "power-mode-auto").exists()

    def step(self, pct: float | None, ext: bool | None, mode: str) -> str | None:
        if pct is not None and ext is False:
            # Three low readings in a row: one glitchy gauge read must not power the Badger off.
            self.low_reads = self.low_reads + 1 if pct <= self.shutdown_pct else 0
            if self.low_reads >= 3:
                return "poweroff"
            if pct <= self.auto_end and mode != "endurance" and not self.auto_switched:
                self.auto_switched = True
                return "endurance"
            return None
        self.low_reads = 0
        if ext is True and self.auto_switched:
            self.auto_switched = False
            return "restore"
        return None


def read_all(gauge: Retrying, monitor: Retrying | None) -> dict:
    g = gauge.get()
    try:
        if g is None:
            raise OSError("gauge not found")
        r = g.read()
    except OSError as e:
        r = {"percent": None, "voltage": None, "external": None, "error": str(e)}
    m = monitor.get() if monitor else None
    if m:
        try:
            r.update(m.read())
            # Power on DC IN proves external power; its absence proves
            # nothing (USB-C PD input is not metered), so never set False.
            if r.get("external") is None and r["input_v"] > 6:
                r["external"] = True
        except OSError:
            pass
    return r


def main() -> int:
    cfg_path = Path(os.environ.get("PREPPER_CONFIG", "/etc/prepper/prepper.toml"))
    cfg = tomllib.loads(cfg_path.read_text()).get("power", {}) if cfg_path.exists() else {}
    kind = cfg.get("ups", "x120x")
    if kind not in BACKENDS:
        print(f"powerd: unknown [power] ups = {kind!r}; reporting mains only", file=sys.stderr)
        kind = "none"
    gauge = Retrying(f"{kind} gauge", lambda: BACKENDS[kind](cfg))
    monitor = None
    chip = str(cfg.get("input_monitor", "none")).lower()
    if chip in InputMonitor.CHIPS:
        monitor = Retrying(f"{chip} input meter", lambda: InputMonitor(cfg))
    elif chip != "none":
        print(f"powerd: input_monitor must be one of {', '.join(InputMonitor.CHIPS)} or none — ignoring {chip!r}",
              file=sys.stderr)
    fan = CaseFan(cfg) if cfg.get("case_fan", "auto") != "none" and shutil.which("pinctrl") else None
    pack_wh = float(cfg.get("pack_wh", 50))
    policy = Policy(cfg)
    out_path = Path(os.environ.get("POWER_FILE", RUN / "power.json"))
    interval = float(os.environ.get("POWERD_INTERVAL", 10))

    while True:
        r = read_all(gauge, monitor)
        mode = mode_now()
        temp = cpu_temp()
        r["cpu_temp_c"] = temp
        r["case_fan"] = fan.update(temp, mode) if fan else None
        pct, ext = r.get("percent"), r.get("external")
        runtime = None
        if pct is not None and ext is False:
            runtime = round(pack_wh * pct / 100 * 0.9 / WATTS.get(mode, 6.5) * 60)
        try:
            write_json(out_path, {**r, "charging": bool(ext), "runtime_min": runtime, "mode": mode,
                                  "pack_wh": pack_wh, "at": int(time.time())})
        except OSError as e:                       # a full /run must not stop the shutdown logic
            print(f"powerd: cannot write {out_path}: {e}", file=sys.stderr)

        action = policy.step(pct, ext, mode)
        if action == "endurance":
            print(f"powerd: on battery at {pct}% — switching to endurance", file=sys.stderr)
            set_mode_auto("endurance")
        elif action == "restore":
            print("powerd: external power back — restoring the chosen power mode", file=sys.stderr)
            restore_user_mode()
        elif action == "poweroff":
            print(f"powerd: battery {pct}% — clean shutdown", file=sys.stderr)
            poweroff()
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
