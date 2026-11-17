"""
dongles — which RTL-SDR is which.

Every Badger has two identical RTL-SDR Blog V4 dongles on its internal USB hub:
  SDR 1  the weather radio (alertd, NOAA Weather Radio, 162 MHz whip on the "SDR" jack)
  SDR 2  aircraft (1090 MHz whip) and weather satellites (137 MHz V-dipole), "SDR 2" jack

They are told apart by the serial number in their EEPROM, set once at build time (each dongle
alone on USB, then replug):

    rtl_eeprom -d 0 -s BADGER-SDR1      # the weather-radio dongle
    rtl_eeprom -d 0 -s BADGER-SDR2      # the aircraft/satellite dongle

(Avoid all-digit serials such as the factory "00000001": librtlsdr reads a number given to
`-d` as a device index first.)

prepper.toml [radio] overrides, normally left alone:
    weather_sdr = "auto"    # or a serial ("BADGER-SDR1") or a device index (an integer)
    sdr2 = "auto"           # or a serial, a device index, or "none" (don't use a second dongle)

Automatic assignment (assign()):
  * serials BADGER-SDR1 / BADGER-SDR2 present → used, whatever the USB order;
  * two dongles, serials not set → SDR 1 = the first on the hub, SDR 2 = the other, with a
    warning to set serials (by serial if the two differ, else by device index);
  * one dongle only → it is the weather radio, whichever it is (alerts come first); aircraft
    and satellites then borrow it now and then (the fallback in sdrd);
  * an explicitly configured weather serial that isn't plugged in → weather radio missing
    (alertd never takes another dongle by mistake when you pinned one).
Each role gets `arg`, what to pass to the program (`rtl_fm -d`, `readsb --device`,
`satdump --source_id`): the serial when it is unique, else the device index.
"""
from __future__ import annotations

import os
from pathlib import Path

RTL_SDR_IDS = {("0bda", "2832"), ("0bda", "2838")}     # Realtek RTL2832U (keep in sync with 99-prepper-sdr.rules)
SYSFS = Path(os.environ.get("PREPPER_USB_SYSFS", "/sys/bus/usb/devices"))
SDR1_SERIAL = "BADGER-SDR1"
SDR2_SERIAL = "BADGER-SDR2"
ANTENNAS = ("adsb", "satellite", "both")


def _rd(p: Path) -> str:
    try:
        return p.read_text(errors="replace").strip()[:64]
    except OSError:
        return ""


def _port_key(name: str):
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in name.replace("-", ".").replace(":", ".").split(".")]


def list_dongles(root: Path | None = None) -> list[dict] | None:
    """RTL-SDR dongles on USB as [{serial, product, manufacturer, port}] in hub-port order, or None
    where there is no USB sysfs to look at (a laptop, a container)."""
    root = root or SYSFS
    if not root.is_dir():
        return None
    out = []
    for dev in sorted(root.iterdir(), key=lambda p: _port_key(p.name)):
        if ":" in dev.name:                     # interfaces, not devices
            continue
        if (_rd(dev / "idVendor"), _rd(dev / "idProduct")) not in RTL_SDR_IDS:
            continue
        out.append({"serial": _rd(dev / "serial"), "product": _rd(dev / "product"),
                    "manufacturer": _rd(dev / "manufacturer"), "port": dev.name[:32]})
    return out


def _cfg(v):
    """('auto'|'none'|'serial'|'index', value) from a config value."""
    if isinstance(v, bool):
        return ("auto", None)
    if isinstance(v, int):
        return ("index", v) if v >= 0 else ("auto", None)
    if not isinstance(v, str):
        return ("auto", None)
    s = "".join(c for c in v.strip()[:32] if c.isprintable())
    if s.lower() in ("", "auto"):
        return ("auto", None)
    if s.lower() in ("none", "off", "no"):
        return ("none", None)
    return ("serial", s)


def assign(radio: dict | None, dongles: list[dict] | None = None, root: Path | None = None) -> dict:
    """{"count", "dongles", "weather": {"arg", "serial", "present"}, "sdr2": None | {"arg", "serial",
    "present"}, "problem", "note"}. `arg` None = the program's default device (index 0)."""
    radio = radio or {}
    if dongles is None:
        dongles = list_dongles(root)
    wk, wv = _cfg(radio.get("weather_sdr", "auto"))
    sk, sv = _cfg(radio.get("sdr2", "auto"))
    problem = note = None

    if dongles is None:                         # can't look: trust the settings (or the build-time names)
        w = {"arg": str(wv) if wk != "auto" else None, "serial": wv if wk == "serial" else None, "present": None}
        s2 = None if sk == "none" else {"arg": str(sv) if sk != "auto" else SDR2_SERIAL,
                                        "serial": sv if sk == "serial" else (SDR2_SERIAL if sk == "auto" else None),
                                        "present": None}
        return {"count": None, "dongles": None, "weather": w, "sdr2": s2, "problem": None, "note": None}

    serials = [d["serial"] for d in dongles]
    unique = {s for s in serials if s and serials.count(s) == 1}

    def ref(i: int) -> dict:                    # how to address dongle number i
        s = serials[i]
        return {"arg": s if s in unique else str(i), "serial": s if s in unique else None, "present": True}

    def find(kind, val, default_serial=None):
        """index of the dongle a setting names, None if absent; 'auto' looks for the build-time serial."""
        if kind == "serial":
            return serials.index(val) if val in unique else None
        if kind == "index":
            return val if val < len(dongles) else None
        if kind == "auto" and default_serial in unique:
            return serials.index(default_serial)
        return None

    wi, si = find(wk, wv, SDR1_SERIAL), (None if sk == "none" else find(sk, sv, SDR2_SERIAL))
    if wk == "serial" and wv in serials and wv not in unique:
        problem = f"Two radios share the serial number {wv}. Give each its own (see the Aircraft help page)."
    if si is not None and si == wi:
        problem = "The weather radio and SDR 2 are set to the same dongle."
        si = None
    n = len(dongles)
    taken = {i for i in (wi, si) if i is not None}
    rest = [i for i in range(n) if i not in taken]

    if wi is None and wk == "auto":
        if si is None:                          # SDR 2 not identified either
            if sk == "auto" and n == 2:
                wi, si = 0, 1
                note = ("The two radios don't have their serial numbers set, so the Badger guessed which is which "
                        "from the USB port. Set them (BADGER-SDR1 / BADGER-SDR2) so the guess can never be wrong.")
            elif n >= 1:
                wi = 0
                if n > 2 and sk == "auto":
                    problem = ("More than two radios are plugged in and they aren't named. Set serial numbers "
                               "BADGER-SDR1 and BADGER-SDR2 (or [radio] weather_sdr / sdr2).")
        elif rest:
            wi = rest[0]                        # SDR 2 found: the weather radio is the other one
            if len(rest) > 1:
                note = "More than one radio could be the weather radio; set its serial number to BADGER-SDR1."
        else:
            wi, si = si, None                   # only SDR 2 is plugged in: weather alerts come first
            note = ("Only one radio found (SDR 2); it listens for weather alerts. Aircraft and satellites "
                    "borrow it now and then.")
    if si is None and sk == "auto" and wi is not None and n >= 2 and not problem:
        rest = [i for i in range(n) if i != wi]
        if len(rest) == 1:
            si = rest[0]
            if serials[si] != SDR2_SERIAL and not note:
                note = ("SDR 2's serial number isn't set; the Badger is using the radio that isn't the weather radio. "
                        "Set it to BADGER-SDR2.")
        else:
            problem = "More than two radios are plugged in; set SDR 2's serial number to BADGER-SDR2."

    weather = ref(wi) if wi is not None else {"arg": str(wv) if wk != "auto" else None,
                                              "serial": wv if wk == "serial" else None, "present": False}
    if weather["present"] and wi == 0 and weather["serial"] is None:
        weather["arg"] = "0"
    if sk == "none":
        sdr2 = None
    elif si is not None:
        sdr2 = ref(si)
    else:
        sdr2 = {"arg": str(sv) if sk in ("serial", "index") else None,
                "serial": sv if sk == "serial" else None, "present": False}
    if any(serials.count(s) > 1 for s in serials if s) and n >= 2 and not problem:
        note = ("Both radios have the same serial number, so the Badger tells them apart by USB port. "
                "Set them to BADGER-SDR1 and BADGER-SDR2 (see the Aircraft help page).")
    return {"count": n, "dongles": dongles, "weather": weather, "sdr2": sdr2, "problem": problem, "note": note}
