# Hardware

## Why a Raspberry Pi 5 (8 GB) and not something bigger

| Option | For | Against | Verdict |
|---|---|---|---|
| **Pi 5 8 GB** | 3.2 W idle / 9.8 W peak, huge HAT ecosystem (UPS, NVMe, RTC battery), mainline Linux, PCIe NVMe | 2026 RAM-shortage price (~$175) | **Reference build** |
| Pi 5 16 GB | Bigger AI models | $385 (Nov 2026) and LLM speed is memory-bandwidth bound — barely faster | Not worth it |
| Radxa ROCK 5B+ 32 GB (RK3588) | NPU runs a 3B model at 7–8.5 tok/s, 2× M.2, $209 | Smaller HAT ecosystem, vendor kernel | Good "AI tier" alternative — software runs unchanged on Debian arm64 |
| Intel N100/N150 mini PC | x86, cheap with 16 GB | 6–14 W idle — doubles battery size | Only if you already have one |
| Hailo AI HAT+ 2 | Vision models | **Slower than the Pi 5 CPU for chat models** (Llama 3.2 3B: 2.6 vs 4.8 tok/s) | Skip |

The Prepper Disk uses a Pi 4B with 2 GB of RAM and a 512 GB SD card. The Pi 5 with 8 GB of RAM, NVMe storage and a clock battery fixes that design's three weak points:

- **SD card wear and corruption.** NVMe replaces the SD card for content.
- **Wrong clock after power loss.** The Pi 5's RTC battery keeps time, with GPS as a second source.
- **No room for courses, AI and maps at the same time.** 8 GB of RAM runs all three.

## Bill of materials — Ultimate build

Shopping list with where to buy each part and search terms: [BOM.md](BOM.md) (spreadsheet: [bom.csv](bom.csv)). How the finished Badger is used — controls, jacks, lights: [TOUR.md](TOUR.md).

Prices are rough US street prices in Nov 2026. RAM and SSD prices are moving fast, so check before you buy. **VERIFY** marks parts whose exact fit I could not confirm.

### Core

| Part | Example | ~USD | Notes |
|---|---|---:|---|
| Raspberry Pi 5 8 GB | — | 175 | |
| Active Cooler | official | 5 | fits under the NVMe HAT |
| UPS HAT, 4-cell, 5.1 V 5 A, **6–18 V DC input**, fuel gauge | Geekworm X1202 | 50 | 98 × 86 mm board, mounts under the Pi on pogo pins; MAX1704x at I²C 0x36, power-loss on GPIO6 (`[power] pld_gpio`). **VERIFY** which DC-in connector your unit has. Alternatives: X1206 V2 (4×21700, 72 Wh, check its input), Waveshare UPS HAT (E) (USB-C PD only) |
| 4× 18650 cells, 3,500 mAh, flat-top | Molicel M35A / Samsung 35E | 32 | ≈50 Wh pack. Buy from a reputable cell vendor; matched set |
| NVMe HAT, 2280, top-mount | Geekworm X1001 | 18 | **VERIFY** stacking with the X1202 (Geekworm sells them as compatible). PCIe Gen 2 — Gen 3 is not certified on the Pi 5 |
| NVMe SSD 2280, low-power DRAM-less | WD SN5000 / SN770 class | volatile | 512 GB Field · 1 TB Base · 2–4 TB Ultimate. Prefer APST idle in the mW range |
| RTC battery (rechargeable ML-2020) | Raspberry Pi SC1163 | 5 | set `[power] rtc_battery = "ml2020"` to enable charging — **never** with a CR coin cell |
| microSD 32 GB | any A1 | 8 | rescue image (same OS, no content) taped inside the lid |
| USB Wi-Fi 6 adapter, MT7921AU, AP mode | ALFA AWUS036AXML / EDUP EP-AX1672 | 50 | 85 × 63 × 20 mm, two RP-SMA jacks. In-kernel driver (5.19+); 5 GHz AP for 20–60 phones, WPA2/WPA3. Onboard Wi-Fi stays free as an uplink for updates. Without the adapter the onboard radio runs the hotspot, WPA2 only |
| 2× dual-band (2.4/5 GHz) adhesive FPC antennas, ≤ 48 × 12 mm, **pre-wired to RP-SMA male**, ~20 cm | — | 16 | **built in** under the lid; replace the adapter's stick antennas. FPC antennas usually come with U.FL: if so, add a U.FL→RP-SMA-male pigtail for each |
| M.2 NVMe USB enclosure | RTL9210 / JMS583 class | 20 | for loading the SSD from a laptop, and rescue access to the content |

### Radio bay

| Part | Example | ~USD | Notes |
|---|---|---:|---|
| **SDR 1** — weather radio | RTL-SDR Blog V4 (V4L as it goes EOL) | 40 | NOAA Weather Radio SAME alerts and the written NOAA forecast, AM/FM/shortwave listening. The V4 has had Linux driver quirks: if it won't tune, install the rtl-sdr-blog build of the driver. `alertd` starts by itself when the SDR is plugged in. Serial `BADGER-SDR1` (see [Second SDR](#second-sdr-sdr-2)) |
| **SDR 2** — aircraft and weather satellites (standard) | RTL-SDR Blog V4 **dipole kit** (dongle + telescopic dipole set, mount, 3 m and 60 cm coax) | 57 | ADS-B aircraft on 1090 MHz and **Meteor-M LRPT** weather-satellite pictures on 137 MHz. The kit's dipole becomes the outdoor satellite V-dipole. Rides on a printed shelf above SDR 1. Serial `BADGER-SDR2`. **NOAA APT satellites ended in 2025**; Meteor-M is what is left to receive |
| 1090 MHz ADS-B whip, SMA male, 7–10 cm | generic | 12 | screws onto the rear **SDR 2** jack for aircraft (as shipped) |
| SMA female bulkhead to **right-angle** SMA male, 20–25 cm | RG316 | 7 | SDR 2 jack to the second dongle; the right-angle plug turns toward the Wi-Fi side, clear of the Ethernet jack |
| SMA dust caps | — | 2 | for whichever jack has no antenna on it |
| Meshtastic node, 915 MHz (US) | RAK WisBlock RAK19007 + RAK4631 | 35 | standalone over USB serial — keeps meshing even when the Pi is off (~0.1 W). Runs from USB; a LiPo on its battery jack is optional. Ships with Bluetooth on and PIN 123456: the mesh bridge replaces that with a random PIN (shown on the front panel); see SECURITY.md |
| USB GPS with built-in patch antenna, ≤ 42 × 42 × 16 mm | u-blox M8/M10 USB board | 25 | **built in**, in a cradle under the lid facing the sky; position + GPS time via gpsd → chrony. Many USB pucks are ~50 mm across and won't fit the cradle: buy a board, or change `gps_box` |
| Compact 4-port USB 2.0 hub board, ≤ 44 × 25 mm | — | 8 | SDR 1, SDR 2, GPS and LoRa node share one Pi USB 2 port. **All four ports are used** |
| 915 MHz FPC antenna, ≤ 80 × 20 mm, U.FL | RAK or generic | 6 | **built in** under the lid — the LoRa radio can never run without an antenna |
| SMA female bulkhead to SMA male cable, 10–15 cm | RG316 | 6 | SDR 1's panel jack at the end of the whip channel. Or run the dongle's own SMA through the panel |
| Folding SDR whip, SMA, swivel knuckle, ≤ 135 mm folded, ≤ 9 mm grip diameter | 2 m/70 cm or 136–174 MHz handheld whip | 15 | SDR 1's antenna (162 MHz weather radio); folds into a recessed channel on the rear, held by two TPU clips |
| Short internal USB cables, right-angle where needed | — | 15 | Pi→Wi-Fi adapter, Pi→hub, hub→SDR 1, hub→SDR 2, hub→RAK (USB-C), hub→GPS |
| 90° USB-A and RJ45 adapters | — | 8 | there are 25 mm of plug room beyond the Pi's USB/Ethernet edge. Point them **up**: SDR 2 sits 3 mm below the plug room |

### Front panel & I/O

| Part | ~USD | Notes |
|---|---:|---|
| 1.3" SH1106 I²C OLED 128×64 | 8 | address 0x3C (no clash with the fuel gauge at 0x36) |
| EC11 rotary encoder with push, 15 mm shaft | 3 | GPIO 17 / 27 / 22. Cut the shaft so the knob sits inside the control pocket |
| Low-profile knob, 14 × 8 mm | 2 | |
| 12 mm momentary button | 3 | Pi 5 power-button pads (J2) — clean power-on/off with the Pi off |
| 5 mm LED, red or amber, + 330 Ω | 1 | GPIO 23. Red or amber, not green: easy on night vision |
| 5 mm clip-in LED holder | 1 | fits the 6.3 mm hole |
| 2× panel-mount USB-A 3.0 extensions, 30 cm | 12 | user files and update bundles. M3 ears |
| Panel-mount USB-C extension | 8 | USB-C PD input. M2.5 ears |
| USB-C PD trigger board, **fixed 15 V** | 5 | turns any PD charger/power bank into DC for the UPS input. A ZY12PDN can output 20 V, above the X1202's 18 V limit: use a fixed 15 V board, or set the ZY12PDN to 15 V and lock it |
| DC5521 barrel plug pigtail | 2 | from the ideal diodes into the X1202's DC input |
| GX16-2 aviation connector pair | 6 | sealed **8–30 V DC input**: portable solar panel direct, 12 V battery, vehicle |
| M8 4-pin A-coded female panel socket, front mount, IP67, with leads | 6 | rear **SENSORS** socket for outdoor sensors. A different connector from the GX16 on purpose, so the power plug can never go in. See [Sensor socket](#sensor-socket) |
| 100 mA resettable polyfuse + 4.7 kΩ and 10 kΩ resistors | 2 | 3.3 V to the sensor socket, and the 1-Wire and float-switch pull-ups |
| 12 V 5 A step-down regulator, input up to 50 V (Pololu D36V50F12) | 35 | regulates panel voltage (20–26 V) down to 12 V; drops out and passes lower inputs through. 12 V suits the X1202's 6–18 V input |
| 5 A inline fuse + SMBJ30A surge-clamp (TVS) diode | 3 | on the DC input, before the regulator |
| P-channel MOSFET reverse-polarity stage (≥ 40 V, ≥ 10 A P-FET + 15 V zener + 100 kΩ) | 3 | right after the fuse |
| 2× ideal-diode modules, LM74700-based | 12 | LM74700 modules are single-channel: one per source, so USB-C and the DC input can both feed the UPS |
| INA228 current/voltage sensor breakout (optional) | 15 | 85 V capable, I²C 0x40 — shows input volts and watts ("☀ 14 W") on the screen and launcher. Set `[power] input_monitor = "ina228"`. Not an INA219: it tops out at 26 V, below a panel's 25–30 V open-circuit voltage |
| Panel-mount RJ45 extension | 8 | Ethernet for building/updating. M3 ears |
| 40 mm 5 V fan + dust filter | 15 | Noctua NF-A4x10 5V |
| AO3400-class logic-level MOSFET + flyback diode | 3 | GPIO 18 switches the case fan on and off with temperature. See [Case fan](#case-fan) |

### Case

| Part | ~USD | Notes |
|---|---:|---|
| PETG, about 625 g: **yellow** for the base, sled and SDR shelf, **black** for the lid | 35 | Honey Badger colours. See [Material](#material-petg-not-pla-tough) |
| TPU 95A, **black**, about 150 g | 25 | 4 corner bumpers, 4 shock dampers, lid gasket, 2 SDR clips |
| 22 M3 heat-set inserts, 4 M2.5 inserts | 5 | see [Hardware](#hardware-counts) |
| M3 button-head screws and washers, M2.5 and M2 screws, M2 nylon spacers | 16 | see [Hardware](#hardware-counts) |
| 3 mm closed-cell EVA or neoprene foam, 85 × 56 mm | 3 | between the lid hold-down posts and the NVMe HAT |
| Hook-and-loop strap, 20 mm | 2 | through the sled slots, around the UPS cells |
| Wiring and consumables | 45 | wire, zip ties ≤ 2.2 mm wide, Kapton tape, hot glue for the U.FL plugs, thermal pad, perfboard, J2 header, sticker paper |
| Transport hard case with foam | 115 | Pelican 1400 class (interior ≈ 300 × 225 × 132 mm) — see [Drops and transport](#drops-and-transport) |

### Field power kit (external)

| Part | ~USD | Notes |
|---|---:|---|
| 12.8 V LiFePO4 20 Ah (256 Wh) | 80–150 | safest chemistry, 3,000+ cycles; do not charge below 0 °C (or buy a self-heating pack) |
| Portable 12 V-class folding panel (yours: **Jackery SolarSaga 40W Air**) | — | plugs **straight into the Badger** through an adapter cable — see [Solar and DC input](#solar-and-dc-input) |
| Adapter cables to the GX16 plug | 10–20 each | DC8020 (Jackery), MC4, XT60, Anderson PowerPole, car socket, DC5521 — make the ones you need |
| Victron SmartSolar MPPT 75/10 (optional) | 50–75 | only with a separate battery: best harvest, and 24 V-class panels |
| Faraday bag | 30 | for the spare SD, spare Pi and USB bundle. A plastic case is **not** EMP protection |

**Approximate total:** about $900 for the Badger without its SSD (both SDRs included; see [BOM.md](BOM.md#totals) for the exact sum), plus the SSD, the field power kit and the transport case. The optional sensor kit is about $60, plus about $110 for a Geiger counter. Tools, if you need them, are about $130 more.

## Wiring

```
 USB-C PD charger
   └► PD trigger (fixed 15 V) ──────────────────────────────────────► ideal diode #1 ─┐
 Panel / battery / vehicle, 8–30 V (open-circuit ≤ 30 V)                               │
   └► adapter ► GX16 ► 5 A fuse ► P-FET (reverse polarity) ► TVS ► [INA228]            │
        └► 12 V step-down (D36V50F12) ──────────────────────────────► ideal diode #2 ─┤
                                                                                       ▼
                            DC5521 ► UPS HAT DC-in (6–18 V) · 4×18650 · gauge @0x36 · PLD → GPIO6
                                                                                       ▼  pogo pins (5.1 V 5 A)
                            Raspberry Pi 5 ── PCIe FFC ──► NVMe HAT (2280) · RTC battery (ML-2020)

     front: OLED (I²C 0x3C) · encoder GPIO17/27/22 · button → J2 · LED GPIO23
     USB3 #1 ──► Wi-Fi 6 adapter (AP, 5 GHz) ── RP-SMA ──► 2 flex antennas under the lid
     USB3 #2 ──► front USB 1 (panel extension)
     USB2 #1 ──► internal hub ─┬► SDR 1 (weather) ── SMA bulkhead ──► rear "SDR" jack, folding whip
                   (4 of 4     ├► SDR 2 (aircraft/satellites, on the shelf) ── right-angle SMA pigtail
                    ports)     │                                             ──► rear "SDR 2" jack
                               ├► USB GPS (patch antenna, under the lid)
                               └► Meshtastic node ── U.FL ──► flex antenna under the lid
     USB2 #2 ──► front USB 2 (panel extension)
     GPIO 18 ──► MOSFET ──► 40 mm case fan          Ethernet ──► rear RJ45 extension
     3.3 V ─► 100 mA polyfuse ─┐
     GPIO 4 (1-Wire, 4.7 kΩ) ──┼──► rear "SENSORS" M8 socket (1 = 3.3 V · 2 = GND · 3 = 1-Wire · 4 = float)
     GPIO 24 (float, 10 kΩ) ───┘
```

### GPIO map

Every Pi GPIO the Badger uses. Physical header pin numbers are in brackets; 3.3 V is on pins 1 and 17, GND on 9 and 20 among others. The power button uses the Pi 5's own J2 pads, not a GPIO.

| GPIO | Used for | Notes |
|---|---|---|
| 2, 3 (3, 5) | I²C bus | UPS fuel gauge 0x36, OLED 0x3C, INA228 0x40. Sensor boards share it: BME280 0x76/0x77, SHT3x/4x 0x44/0x45, ADS1115 0x48–0x4B (see [sensors.md](features/sensors.md)) |
| 4 (7) | 1-Wire, DS18B20 temperature probes | SENSORS socket pin 3; 4.7 kΩ pull-up to 3.3 V inside the case; `dtoverlay=w1-gpio` |
| 6 (31) | UPS power-loss signal | `[power] pld_gpio` |
| 17, 27, 22 (11, 13, 15) | rotary encoder A, B, push | |
| 18 (12) | case fan MOSFET | |
| 23 (16) | status LED | through 330 Ω |
| 24 (18) | float switch (flood alarm) | SENSORS socket pin 4; 10 kΩ pull-up to 3.3 V inside the case |
| 25 (22) | spare | a second float switch, if you want one |
| 14, 15 (8, 10) | UART0, optional | only for a UART ultrasonic water-level sensor wired to the header; the installer turns it on only when `[sensors]` uses `/dev/ttyAMA0`. The easier way is a 3.3 V USB-TTL adapter |

### USB port budget

The Pi 5 has two USB 3 ports (blue) and two USB 2 ports (black). Use them like this:

| Pi port | Device |
|---|---|
| USB 3 #1 | Wi-Fi 6 adapter (hotspot) |
| USB 3 #2 | front **USB 1**, through a panel extension |
| USB 2 #1 | internal 4-port hub (**full**): SDR 1, SDR 2, GPS, LoRa node |
| USB 2 #2 | front **USB 2**, through a panel extension |

| Hub port | Device |
|---|---|
| 1 | SDR 1, weather radio (`BADGER-SDR1`) |
| 2, 3 | GPS and Meshtastic node (USB-C), either way round |
| 4 | SDR 2, aircraft and satellites (`BADGER-SDR2`) |

Which dongle goes in which hub port doesn't matter once their serials are set (below). Without serials, the Badger guesses that the first dongle on the hub is the weather radio, so keep SDR 1 on the lower-numbered port.

The plugs need room: there are 25 mm beyond the Pi's USB/Ethernet edge before the Wi-Fi adapter's slot, so use 90° adapters or right-angle cables. Point the 90° adapters **up**: SDR 2's shelf puts its dongle 3 mm below the plug room.

**USB current.** The hub hangs off one USB 2 port and now feeds two RTL-SDR V4s (roughly 0.3 A each while tuned), the GPS and the LoRa node. `usb_max_current_enable=1` (below) already lets the Pi supply 1.6 A across all its USB ports, shared with the Wi-Fi adapter and anything in the front ports. When you build, check the hub's draw with both SDRs running (aircraft on SDR 2, alerts on SDR 1) plus GPS and LoRa: a USB power meter between the Pi and the hub works. If a dongle drops off the bus under load, try a different hub board or shorter, thicker cables.

`os/install.sh` sets the matching firmware options in `/boot/firmware/config.txt`:

- `dtparam=pciex1`
- `dtparam=i2c_arm=on`
- `usb_max_current_enable=1`, so USB devices get 1.6 A from the UPS
- Bluetooth off (the Pi's own; the Meshtastic radio's Bluetooth is separate — see the BOM note above)
- `dtoverlay=w1-gpio,gpiopin=4` for the DS18B20 probes, and `dtparam=uart0=on` only when a configured sensor uses UART0 (from the sensors installer, `os/install.d/sensors.sh`; reboot once after adding them)

It also writes the EEPROM: `POWER_OFF_ON_HALT=1`, `PSU_MAX_CURRENT=5000`, and boot from NVMe → SD → USB.

## Power modes

Switch modes from the knob (Power mode) or the launcher (Settings → Badger administration). On battery below 30 %, the Badger drops to Endurance by itself and goes back to your default when power returns.

| | **Endurance** | **Balanced** (default) | **Full** |
|---|---|---|---|
| Use it for | long outages on battery | everyday | on mains or strong sun |
| Library, maps, search, tools, check-in board | ✓ | ✓ | ✓ |
| Wi-Fi hotspot, all users | ✓ | ✓ | ✓ |
| Weather alerts (SDR 1), LoRa mesh, GPS, sensors | ✓ | ✓ | ✓ |
| Aircraft and satellite passes (SDR 2) | ✓ | ✓ | ✓ |
| NOAA forecast captures (every 3 h) | skipped (an admin's *Update now* still works) | ✓ | ✓ |
| CPU | slowest clock: busy pages load a little slower with many users | adjusts to load | full speed all the time |
| Ask the Library (AI) | **smaller model**: answers sooner (~10 words/s) but less careful | 4B model, ~3–5 words/s | 4B model, quickest under load |
| Courses (Kolibri) | **off** | on, if enabled | on |
| Video and book libraries (Jellyfin, Kavita) | **off** | off unless enabled | on, if enabled |
| Case fan | starts at 62 °C | starts at 55 °C | always on |
| Average draw | ~4.5 W | ~6.5 W | ~9 W idle · 12–15 W with AI answering |

Updates still download in any mode when there's internet, at the lowest disk and CPU priority. To keep them off battery, set an update window.

The chosen mode survives a reboot (`prepperctl power-restore` reapplies it at boot).

## Power consumption and runtime

These figures are estimates from published measurements of the parts. With the INA228 input meter fitted you can measure your own.

| Part | Typical | Peak |
|---|---:|---:|
| Raspberry Pi 5 (8 GB) | 3.2 W | 9.8 W |
| NVMe SSD (low-power, with idle power-saving) | 0.1–0.5 W | 4–7 W (writing) |
| Wi-Fi 6 adapter (hotspot) | 1.0 W | 3 W |
| SDR 1 (weather alerts) | 1.3 W | 1.5 W |
| SDR 2 (aircraft, with the 1090 MHz whip: always on) | 1.3 W | 1.5 W |
| LoRa node · GPS · screen | 0.4 W | 0.9 W |
| Fans (case + Pi cooler, when running) | 0–0.8 W | 0.8 W |
| Conversion losses (UPS, step-down) | ~10 % | ~12 % |
| **Whole Badger** | **4.5–6.5 W** without SDR 2 · **add 1.3–1.5 W** with aircraft on | **≈ 20 W** (AI answering + SSD writing + busy Wi-Fi), ≈ 21.5 W with SDR 2 |

While charging, the input also feeds the battery, up to about 10 W more. Size power sources for **30 W or more**; a 45 W USB-C charger is comfortable.

**SDR 2 and battery life.** With the aircraft whip fitted (as shipped), SDR 2 listens all day in every power mode, which adds about 1.3–1.5 W. The mode averages and runtimes in this section leave it out. With it, the internal battery lasts roughly **~7.5 h in Endurance** and **~5.5 h in Balanced**. With the satellite V-dipole fitted, SDR 2 runs only during passes and on-demand aircraft scans. On a long outage you can stop it under Settings → Services → *sdr*. That stops aircraft and satellites only; weather alerts run on SDR 1 and keep listening.

| Runtime | Endurance (4.5 W) | Balanced (6.5 W) | Full (9–14 W) | Flat out (20 W) |
|---|---:|---:|---:|---:|
| Internal battery (50 Wh, ~45 Wh usable) | **~10 h** | **~7 h** | ~3–5 h | ~2 h |
| + 256 Wh LiFePO4 | ~2.5 days | ~1.75 days | ~1 day | ~12 h |
| Your 40 W panel, sunny day (~110 Wh) | covers it | ~70 % | ~35–45 % | — |

`powerd` shuts down cleanly at 6 % so the SSD never browns out. Press the power button to start again once there's power.

`powerd` knows whether external power is present from the UPS's power-loss pin (`[power] pld_gpio = 6`). It reads it with `gpioget` and detects libgpiod v1 or v2 by itself (`pld_cmd = ""`). If your board signals it some other way, set `pld_cmd` to a command that prints 1 on external power and 0 on battery.

## Case fan

The Pi's own Active Cooler (under the SSD board) is run by the Pi firmware. It speeds up as the processor warms.

The 40 mm case fan on the left wall pulls outside air across the whole box and out through the louvers on the right. `powerd` switches it through a MOSFET on GPIO 18, with a gap between the on and off temperatures so it doesn't flicker:

| Mode | On at | Off at |
|---|---|---|
| Balanced | 55 °C | 47 °C |
| Endurance | 62 °C | 54 °C (stays quiet and saves power) |
| Full | always on | — |

If the temperature can't be read, the fan runs; that's the safe failure. The Status page shows whether it's on. To run it all the time without the MOSFET, wire it to 5 V and set `case_fan = "none"`.

**The gate resistor matters.** With the usual pull-down on the MOSFET gate, the fan is **off** whenever GPIO 18 isn't driven: while the Pi boots, after it halts, or if it hangs. A 10 kΩ pull-up to 3.3 V instead makes it fail on; `powerd` still switches it.

## Inside the case

The layout, as modelled in `case/honey_badger_case.scad`:

| Part | Where and how it mounts |
|---|---|
| Pi 5 + UPS board + battery cells + NVMe SSD | one stack: the Geekworm X1202 (98 × 86 mm) screws to a PETG **sled** (102 × 90 mm, 4 × M2.5 inserts in the Pi/UPS holes), the Pi sits on the UPS standoffs, and the SSD board stacks on top. The sled floats on 4 TPU dampers in floor cups. Lid posts press it down through 3 mm foam, with 1 mm of preload |
| Battery cells (4 × 18650) | in the UPS board's holder, plus a hook-and-loop strap through the sled slots |
| SSD | screwed to the NVMe HAT (M2 screw), with a thermal pad |
| Pi plugs (USB, Ethernet) | 25 mm of free space beyond the port edge: use 90° adapters |
| Wi-Fi 6 adapter (85 × 63 × 20 mm) | stands on edge in a slot against the right wall, rear right. Two strap slots for zip ties |
| Radio bay: SDR 1, compact USB hub (≤ 44 × 25 mm board), RAK node | zip-tied to anchors on the floor, right side; the RAK node sits in the front-right bay |
| SDR 2 | lying flat on the printed **SDR shelf** (part `sdr_shelf`), zip-tied through its slots. The shelf's deck sits 1.5 mm above SDR 1 and overhangs the hub. It stands on a leg in the gap between the sled and SDR 1, and on two posts fixed with 2 × M3 × 12 into heat-set inserts in floor bosses (heads sunk 6 mm into the posts). Vents in the deck let SDR 1's heat rise past it. USB to the hub's 4th port; a right-angle SMA pigtail runs back along the gap beside the Wi-Fi adapter to the rear SDR 2 jack |
| Power electronics (step-down, ideal diodes, PD trigger, INA228, fuse, P-FET, TVS) | an 80 × 26 mm carrier (perfboard) on 4 bosses in the front floor bay, under the screen, with M3 × 6 screws into heat-set inserts. Tape the underside with Kapton |
| Wi-Fi ×2, LoRa antennas | adhesive flex antennas in raised locating frames under the lid, with a notch for each cable. The two Wi-Fi antennas sit 132 mm apart at right angles |
| GPS | cradle with a zip-tie slot under the lid, front right |
| Antenna cables | zip-tie anchors under the lid and on the floor, rear right; leave a service loop so the lid lifts off. Hot-glue the U.FL plugs |
| Wiring to the front | zip-tie anchors along the left floor channel |
| 40 mm fan | left wall, 16 mm from the cells; 4 × M3 × 20 screws and nuts (or the fan's own screws) |
| Screen | 4 × M2 screws from the front, nylon spacers and nuts inside, in a pocket that puts the glass about 3 mm behind the face |
| Knob, button, light | in the front control pocket, on their own panel nuts (see below) |
| USB-A and Ethernet extensions | M3 screws through their mounting ears |
| USB-C extension | M2.5 screws through its ears |
| GX16, SDR and SDR 2 SMA jacks, SENSORS socket | their own panel nuts, at the back of their recesses |
| SDR clips | 2 × M3 × 6 each, into heat-set inserts in bosses behind the whip channel |

### Front and rear panels

- **Front:** three zones on one centre line. The OLED sits behind a recessed window with a rounded rim. Next to it is a recessed **control pocket**, 10 mm deep with a 45° roof so it prints without supports. The low-profile 14 × 8 mm knob on the EC11 encoder (15 mm shaft, cut to fit), the 12 mm power button and the status LED (5 mm, in a clip-in holder in a 6.3 mm hole) sit in one row across it. On the right, a second pocket, 8 mm deep, holds the two USB-A panel extensions stacked one above the other (USB 1 on top), on M3 ears. Every pocket and well rim is rounded.
- **Rear:** USB-C PD in (M2.5 ears) and RJ45 sit flush. The GX16 solar/DC socket sits in a 28 mm well, with room for your fingers on the plug's lock ring. The SDR SMA jack is at the end of a recessed **whip channel**, 14 mm deep. The folding whip folds into the channel and snaps into two TPU clips. To use it, unfold it and tilt it back about 20° so it clears the channel roof.
- **SDR 2** jack (SMA): low on the rear, midway between the Ethernet and USB-C ear screws, under the whip clip with room for your fingers. It takes the 1090 MHz whip for aircraft, or the 3 m coax from the outdoor satellite V-dipole.
- **SENSORS** socket (M8, 4-pin, IP67): between the USB-C ear screw and the GX16, with its cap on when unused. See [Sensor socket](#sensor-socket).
- With the jacks capped, nothing on the front or rear stands out more than about 3.5 mm, inside the 6 mm bumpers. The 1090 MHz whip on SDR 2 does stand out: unscrew it and cap the jack for transport.
- The SDR 1 whip folds away, so the only antenna that stays outside is whatever is on SDR 2. If you want a LoRa mast antenna, set `lora_ext = true` in the SCAD: it adds a capped SMA jack in its own well on the rear (add a U.FL-to-SMA bulkhead pigtail and a dust cap). The model warns that `lora_ext` and SDR 2 share the rear band.

## Connector fit (wall thickness)

The walls are 4 mm, which is thicker than some panel-mount parts can grip. The controls mount on the back of the control pocket (2.5 mm), the rear jacks on the back of their wells (3 mm), and the case thins the wall locally from the inside where needed:

| Part | Needs | Case provides |
|---|---|---|
| Rotary encoder (EC11, ~5 mm thread) | ≤ 2 mm panel | 2.5 mm pocket back, 16 mm counterbore inside, 2 mm left; blind hole for the anti-rotation tab |
| 12 mm push button (~6 mm thread) | ≤ 3–4 mm | 2.5 mm pocket back |
| 5 mm LED in a clip-in holder | ≤ 3 mm | 6.3 mm hole in the 2.5 mm pocket back |
| Screen | recessed, no lens | 31 × 15 mm window with a 1.5 mm rounded rim; module in an inside pocket ≈ 3 mm behind the face |
| USB-A, USB-C, Ethernet panel extensions | ear screws long enough for the panel | USB-A: 2.5 mm pocket back, M3 × 8; Ethernet: 4 mm wall, M3 × 10; USB-C: M2.5 × 10 |
| GX16 socket | ≈ 6–8 mm of thread | 3 mm at the back of its well |
| SDR and SDR 2 SMA bulkheads | ≈ 6 mm of thread | 3 mm at the back of the rear band, 12 mm counterbore inside, 2 mm left |
| SENSORS M8 socket (front mount) | M8 thread, ≈ 11.6 mm nut | 8.2 mm hole in the 3 mm at the back of the rear band |
| Rear connector bodies inside the case | ~25 mm deep | 7 mm clearance to the Pi stack (checked by the model) |

Parts vary by vendor. Print `panel_test` first and adjust the `*_relief`, `*_cut` and `*_at` parameters if yours differ.

## Solar and DC input

The rear **SOLAR / DC IN** jack takes **8–30 V DC** from a portable panel, a battery or a vehicle. There's no controller box: the panel plugs straight in.

**The input chain.** GX16 → 5 A fuse → P-channel MOSFET (reverse-polarity protection) → SMBJ30A TVS → INA228 meter (optional) → 12 V step-down → ideal diode → UPS DC-in. USB-C has its own ideal diode, so both inputs can be connected at once.

**How it handles each source.**

| Source | What the Badger sees | Behaviour |
|---|---|---|
| Panel in good sun (e.g. SolarSaga 40W Air: 20.5–22.5 V working, 25–26 V open-circuit) | 20–26 V | the step-down holds 12 V; the Badger runs and charges its battery |
| Panel in weak sun or clouds | voltage sags | the regulator drops out and passes whatever the panel gives (the UPS accepts 6–18 V). Less harvest, but no oscillation, and the battery fills the gap |
| 12 V LiFePO4 or lead-acid battery | 12–14.6 V | stepped down to 12 V, or passed straight through when it's lower; the battery's own BMS protects it from over-discharge |
| Vehicle socket | 12–14.4 V (spikes clamped by the TVS) | same as a 12 V battery |
| 24 V battery or vehicle | 24–29 V | stepped down to 12 V |

**Will a given panel or battery work?** Check two numbers on its label:

1. **Open-circuit voltage (Voc) ≤ 30 V.** Most 12 V-class portable panels qualify, including the Jackery SolarSaga range, typically 21–26 V. **Never** connect two panels in series, or a 24 V-class panel (Voc 40 V+), without a charge controller.
2. **The plug.** The Badger end is always the sealed GX16. Make an adapter cable for each source you own:

| Source plug | Common on | Adapter |
|---|---|---|
| **DC8020** (8 mm) | Jackery SolarSaga panels | DC8020 → GX16 |
| **MC4** | most rigid and folding panels | MC4 pair → GX16 |
| **XT60** | EcoFlow, Anker and many power stations' solar cables | XT60 → GX16 |
| **Anderson PowerPole** | ham radio, Bluetti, Goal Zero | Anderson → GX16 |
| **Car socket plug** | vehicles, power-station 12 V outputs | cigarette plug → GX16 |
| **DC5521** (5.5 × 2.1 mm) | small 12 V supplies | DC5521 → GX16 |

**Before the first connection of any new cable,** check the polarity with a multimeter. Barrel and 8020 connectors differ between brands. GX16 pin 1 is +, pin 2 is −. The P-MOSFET stage blocks reverse polarity, but don't rely on it. Label each cable with its source and voltage.

**Sizing with your 40 W panel.**

- **A sunny day:** about 40 W × 4 peak sun hours × 0.7 ≈ **110 Wh/day**. That roughly matches the Badger in Endurance (≈ 4.5 W × 24 h ≈ 108 Wh).
- **What that means:** on a good day the 40 W panel keeps it going in Endurance and tops up the battery, and the internal pack carries it overnight.
- **For more:** to run Balanced around the clock, or through cloudy or winter days, use about a 100 W panel, add the 256 Wh LiFePO4, or both.

**Seeing the input.** With the optional INA228 fitted and `[power] input_monitor = "ina228"`, `powerd` reports input volts, amps and watts. Set `input_shunt_ohms` to your board's shunt (Adafruit's INA228 uses 0.015 Ω). You'll see them on the launcher's status bar ("☀ 14 W"), on its Status page, and on the front screen. The firmware also reads an INA226 (36 V) or INA219 (26 V), but only use those if your input never goes above their limit; a panel's open-circuit voltage does.

## Second SDR (SDR 2)

Every Badger has two identical RTL-SDR Blog V4 receivers. **SDR 1** listens to NOAA Weather Radio all the time and is never taken away for anything else while SDR 2 works. **SDR 2** watches aircraft and records weather-satellite passes. What it does depends on the antenna on its rear jack:

| Antenna on the SDR 2 jack | `[radio] sdr2_antenna` | Aircraft | Weather satellites |
|---|---|---|---|
| **1090 MHz whip** (as shipped) | `"adsb"` | all day | not recorded (unless `[satellites] capture = true`; the whip hears 137 MHz poorly) |
| **137 MHz V-dipole**, outdoors | `"satellite"` | on demand: *Scan aircraft for 15 min* | every pass that rises 15° or more |
| a wideband antenna (discone) | `"both"` | all day, paused during passes | every pass |

Only one antenna fits at a time. When you change it, tell the Badger: an admin taps the matching button under *SDR 2 antenna* on the Aircraft or Weather satellites page (that choice wins), or sets `sdr2_antenna`.

**The satellite V-dipole.** Use the dipole from the SDR 2 kit. Extend each arm to **53 cm**, set the arms about **120°** apart, and lay it **flat** (horizontal) with the arms pointing roughly **north and south**, outdoors and clear of metal and buildings. The kit's **3 m coax** reaches the SDR 2 jack. Details: [satellites.md](features/satellites.md).

**Build steps.**

1. Print `sdr_shelf` (PETG, deck on the bed). Melt 2 M3 inserts into the shelf bosses on the floor, right of SDR 1.
2. Fit SDR 1 and the hub first. Then stand the shelf's leg in the gap between the sled and SDR 1 and screw the two posts down with 2 × M3 × 12.
3. Zip-tie SDR 2 flat on the deck. Plug its USB into the hub's 4th port.
4. Fit the SDR 2 bulkhead jack in the rear (its own nut) and run the right-angle SMA pigtail to the dongle, with the plug turned toward the Wi-Fi side, clear of the Ethernet jack.
5. Point the Pi's 90° USB adapters **up**. SDR 2's top is 3 mm below the plug room (`part="assembly"` prints the exact gap).
6. **Set the serial numbers, one dongle at a time.** The dongles are identical, so the Badger tells them apart by a serial stored in each. Stop the radio services, then plug in **only** the weather-radio dongle:

   ```bash
   sudo systemctl stop prepper-alertd prepper-sdr
   rtl_eeprom -d 0 -s BADGER-SDR1     # only SDR 1 plugged in; unplug and replug it afterwards
   rtl_eeprom -d 0 -s BADGER-SDR2     # then only SDR 2 plugged in; unplug and replug it
   rtl_test -t                        # both plugged in: lists SN: BADGER-SDR1 and SN: BADGER-SDR2
   ```

   Don't use all-digit serials such as the factory `00000001`: the radio tools read a number as a device position. Without serials the Badger guesses by hub port and shows a warning until they are set. `[radio] weather_sdr` and `sdr2` can name the dongles explicitly (see [aircraft.md](features/aircraft.md)).

**If SDR 2 is missing or fails**, the Badger carries on with SDR 1 alone. Aircraft scans and (only if an admin allows it) high satellite passes then borrow the weather radio for short, capped periods, never while an alert or watch is live, and weather alerts pause meanwhile. `[radio] sdr_lend = false` forbids lending it at all.

## Sensor socket

The rear **SENSORS** socket is an **M8 4-pin A-coded IP67** panel socket, between the USB-C ear screw and the GX16. It is deliberately a different connector from the GX16 8–30 V power input, so a power plug can never go into it. Keep its cap on when nothing is plugged in.

| Pin | Signal | Inside the case |
|---|---|---|
| 1 | 3.3 V | from the Pi's 3.3 V through a **100 mA polyfuse** (protects the Pi if an outdoor cable is crushed) |
| 2 | GND | |
| 3 | 1-Wire data (DS18B20 probes) | GPIO 4, **4.7 kΩ** pull-up to 3.3 V |
| 4 | float switch (flood alarm) | GPIO 24, **10 kΩ** pull-up to 3.3 V |

GPIO 25 is spare for a second float switch. UART0 (GPIO 14/15) is optional, for a header-wired ultrasonic level sensor. I²C sensors (BME280, SHT3x/4x, ADS1115) don't use the socket: they go on the shared I²C bus with short wires, at the addresses in the [GPIO map](#gpio-map). Everything is 3.3 V: never put 5 V on a GPIO pin. The sensor kit is in [BOM.md](BOM.md#sensor-kit-optional). Wiring, cable lengths, waterproofing and setup are in [sensors.md](features/sensors.md).

## Printing the case

The model is `case/honey_badger_case.scad`; every cutout is a parameter.

- **Outside:** 204 × 168 × 82.5 mm with the lid on (79 mm base + 3.5 mm lid), and 216 × 180 × ~91 mm with the bumpers. Interior 196 × 160 × 76 mm, walls 4 mm. Each part fits a 220 × 220 mm bed.
- **Parts:**

  | Part | Material | Qty | Command |
  |---|---|:-:|---|
  | base | PETG, yellow | 1 | `openscad -D 'part="base"' -o base.stl case/honey_badger_case.scad` |
  | lid | PETG, black | 1 | `openscad -D 'part="lid"' -o lid.stl case/honey_badger_case.scad` (exports face down) |
  | sled | PETG, yellow | 1 | `openscad -D 'part="sled"' -o sled.stl case/honey_badger_case.scad` |
  | bumper | TPU 95A | 4 | `openscad -D 'part="bumper"' -o bumper.stl case/honey_badger_case.scad` (exports upside down, top cap on the bed; all four are the same) |
  | damper | TPU 95A | 4 | `openscad -D 'part="damper"' -o damper.stl case/honey_badger_case.scad` |
  | gasket | TPU 95A | 1 | `openscad -D 'part="gasket"' -o gasket.stl case/honey_badger_case.scad` |
  | sdr_clip | TPU 95A | 2 | `openscad -D 'part="sdr_clip"' -o sdr_clip.stl case/honey_badger_case.scad` |
  | sdr_shelf | PETG, yellow | 1 | `openscad -D 'part="sdr_shelf"' -o sdr_shelf.stl case/honey_badger_case.scad` (exports deck down) — carries SDR 2 |
  | panel_test | PETG | 1 (optional) | `openscad -D 'part="panel_test"' -o panel_test.stl case/honey_badger_case.scad` — the front and rear walls only, to check connector fit before the full base |
  | lid_inlay | PETG, yellow or cream | 1 (optional) | only with `markings = "inlay"`; see [Lid markings](#lid-markings) |

  Add `--backend=manifold` if your OpenSCAD build has it; it renders much faster.

- **Gasket:** printed as a 1.5 × 2.1 mm TPU cord that sits in a 2.2 × 1.6 mm groove in the lid. The lid's locating lip is notched around every screw boss.
- **Showcase renders:** `part="showcase"` draws the finished Badger with the bought parts in place (screen, knob, button, ports, whip, fan) — see `case/renders/showcase_*.png`. Visual only; those stand-ins are not printed.
- **Fit check:** `openscad -D 'part="assembly"' case/honey_badger_case.scad` shows everything together. It prints the outer size, the headroom over the stack (6 mm), the plug room, the fan-to-cell gap and how far panel parts stand out, and warns if any part or cutout collides.
- **Measure first:** panel-mount connectors vary by vendor. Print `panel_test` and adjust the `*_cut` / `*_at` parameters before printing the base.

### Hardware counts

| Item | Qty | Where |
|---|:-:|---|
| M3 heat-set inserts | 22 | 8 lid bosses, 4 bumper bottoms (base corners), 4 power tray, 4 SDR clips, 2 SDR shelf |
| M3 × 12 | 2 | SDR shelf posts |
| M2.5 heat-set inserts | 4 | sled, in the Pi/UPS holes |
| M3 × 8 button head | 4 | lid edge screws, in counterbores |
| M3 × 10 button head + washer | 4 | lid corners, through the bumper top caps |
| M3 × 8 button head + washer | 4 | bumper bottom caps, into the base |
| M3 × 20 + nuts | 4 | fan (or the fan's own screws) |
| M3 × 6 | 4 + 4 | SDR clips, power tray |
| M3 × 10 + nuts | 6 | USB-A and RJ45 ears |
| M2.5 × 10 + nuts | 2 | USB-C ears |
| M2.5 | 4 | UPS board to the sled inserts (length to suit your standoffs) |
| M2 + nylon spacers + nuts | 4 | OLED |

### Assembly notes

- **Bumpers:** rounded TPU 95A corner bumpers that follow the case corners. Each wraps 6 mm around the corner and 16 mm along each face, with a top cap and a bottom cap; the bottom caps are the feet (there are no separate feet). A tongue on each bumper sits in a groove in the case corner. Each bumper is held by two screws with their heads sunk in counterbores below the cap surface: the corner lid screw through the top cap (M3 × 10 + washer), and an M3 × 8 + washer through the bottom cap into an insert in the base.
- **Edges:** every outside edge is rounded, so nothing cuts a hand. The vertical corners are 7 mm and the base bottom and lid top are 2 mm. The seam between base and lid is 0.8 mm on each side, which leaves a soft V. Every outside bumper edge, cap rim and the rims of the pockets, wells, louvers and screen window are 1–1.5 mm, and the SDR clips are rounded too. Cutouts that a connector's flange covers stay square.
- **Lid:** 8 M3 screws: the 4 corner screws through the bumpers, and 4 edge screws in counterbores.
- **Opening the lid:** remove the 4 corner screws at the top of the bumpers and the 4 edge screws. Flex the top caps out of the way and lift the lid. The bumpers stay on; their bottom screws hold them to the base.

### Lid markings

The case is plain by default (`markings = "none"`): no engraving. Add printed labels:

- a vinyl or laminated sticker on the lid with the badger mark and "Join Wi-Fi · open badger.lan"
- small labels for the panel connectors
- the Wi-Fi QR sticker, in its shallow 30 × 30 mm recess on the lid

Two other options in the SCAD's `markings` parameter:

| Option | How | Notes |
|---|---|---|
| `"inlay"` | multi-colour print (AMS/MMU). The lid prints face down; the badger mark and text are printed flush in its first layers in yellow or cream. Export `lid` and `lid_inlay` with `-D 'markings="inlay"'` and load them as one object | most durable, flush with the surface |
| `"engrave"` | 0.8 mm deep engraving in the lid | paint-fill it if you want contrast |

**Recommendation:** use the inlay if you have a multi-colour printer. Otherwise use labels.

### Material: PETG, not PLA Tough

| | PETG | PLA Tough / PLA+ | ASA | PC / PC-CF |
|---|---|---|---|---|
| Softens at | ~75–80 °C | ~55–60 °C | ~95–100 °C | ~110–140 °C |
| Hot car (60–75 °C inside) | survives | **warps, screws loosen** | survives | survives |
| Impact | tough, bends before it cracks | good at room temperature, brittle when cold | good | best |
| Sunlight (UV) | fair | fair | excellent | good |
| Printer needs | open frame is fine | anything | enclosure | enclosure, hot nozzle (≥ 270 °C) |

- **Use PETG for the base, lid and sled.** A prepper box lives in cars, sheds and go-bags. PLA Tough is strong on the bench, but it creeps under screw pressure and softens in a parked car. Keep it for fit tests.
- **ASA** if the Badger will sit in sun for long periods and your printer is enclosed.
- **PC** if you want the toughest shell and your printer can handle it.
- **TPU 95A** for the bumpers, dampers, gasket and SDR clips in every case.

### Rugged print settings

| Setting | PETG shell and sled | TPU bumpers, dampers and clips |
|---|---|---|
| Layer height | 0.2 mm | 0.2 mm |
| Walls | **6** (≈ 2.4 mm, so the 4 mm walls are nearly solid) | 3 |
| Top / bottom | 6 / 6 | 4 / 4 |
| Infill | **40 % gyroid** | **20 % gyroid** (the soft infill is the cushion) |
| Nozzle / bed | 240–250 °C / 80 °C | 220–230 °C / 50 °C, slow (≤ 25 mm/s) |
| Cooling | 30–50 % (hotter layers bond better) | 50 % |
| Orientation | base open side up; lid face down; sled flat | bumpers upside down (top cap on the bed); dampers flat; clips on their backs |

Every recess in the case has a 45° roof or gusset, so nothing needs supports. Layer bonds are the weak direction on any FDM print. The bumpers take the corner hits, and corner hits are exactly what would split layers on a bare box.

### What makes it rugged

| Threat | Built-in answer |
|---|---|
| Corner and edge drops | rounded TPU corner bumpers, 6 mm thick, follow the case corners. Their caps wrap the lid and base, and each is screwed top and bottom |
| Shock to the electronics | the whole Pi/UPS/NVMe stack screws to a PETG **sled** that floats on four TPU **dampers**. Lid **hold-down posts** press it down through a 3 mm foam pad, so it can't hammer up into the lid |
| Cells shaking loose | strap slots in the sled for a hook-and-loop strap around the UPS cells |
| Screen | no lens: the OLED sits about 3 mm behind the front face behind a bevelled bezel, so a flat hit lands on the case, not the glass |
| Knob and jacks | the knob, button and LED sit in a recessed pocket; the GX16 and SDR jack sit in recesses. Nothing stands out more than about 3.5 mm, inside the bumpers |
| Lid popping open | 8 M3 screws into brass heat-set inserts, 4 of them through the bumper caps |
| Water and dust | TPU gasket on the lid. The exhaust louvers slope down toward the outside, so rain runs off. The fan intake has a grille and needs a filter. Splash-resistant, **not waterproof** |
| Loose parts inside | radio bay on zip-tie anchors, SDR 2 zip-tied to a screwed shelf, Wi-Fi adapter in a slot, SMA cables on bulkheads |

Wi-Fi, LoRa and GPS antennas are built in. The SDR 1 whip folds into its channel; the short 1090 MHz whip on SDR 2 unscrews, and a dust cap goes on the jack.

### Drops and transport

**What to expect from 20 ft.** A 20 ft (6 m) drop onto concrete is far beyond anything a 3D-printed enclosure can promise with a Pi and an SSD inside. Rated hard cases like Pelicans are drop-tested to about 1 m, and the MIL-STD-810H transit drop is 4 ft (1.2 m). So there are two configurations:

| Configuration | Goal | Status |
|---|---|---|
| **In use**: Badger on its own, bumpers on | survive 4 ft (1.2 m) drops onto a hard floor, all faces, edges and corners | design target; **not yet tested** |
| **Transport**: Badger, whip folded, in a foam-lined hard case | give the best chance at big drops (vehicle, stairs, 20 ft) | plausible; **not tested** — the hard case does most of the work |

**Transport case.** A Pelican 1400-class case (interior ≈ 300 × 225 × 132 mm) leaves 2–4 cm of foam on every side of the bumpered Badger (216 × 180 × 91 mm). Use firm pick-and-pluck or cut closed-cell foam that holds the Badger snugly on all six sides. Fold the SDR whip into its channel, unscrew the 1090 MHz whip from SDR 2 and cap both the SDR 2 jack and the SENSORS socket. Everything else is already inside. The USB bundle stick and rescue SD fit in the leftover space. Add a Faraday bag inside if you want EMP protection for spares.

**Drop-test procedure.** Do this before you trust it. A spare dummy unit with the same weight is ideal.

1. Fully assemble, with the batteries in, and power it on.
2. Drop it from 1 m onto plywood laid over concrete. Do it once on each of the 6 faces, the 12 edges and the 8 corners: 26 drops in all.
3. After each set, check that it is still running, the Wi-Fi is up, the screen works, the cells are seated, no bumper has torn and no layer has split.
4. Repeat at 1.2 m. For the transport configuration, keep going in steps of 0.5 m inside the hard case, as high as you're willing to risk.
5. Write down the height where something first fails, and fix that part: more walls, thicker bumpers, or firmer foam.
