# Bill of materials and sourcing

Every part, how many, what to search for, and where to buy it. To print the enclosure, see [PRINTING.md](PRINTING.md). The same list is in [bom.csv](bom.csv) for a spreadsheet. [HARDWARE.md](HARDWARE.md) explains why each part was chosen.

Prices are rough US street prices in November 2026. RAM and SSD prices are moving quickly this year, so check before you order.

## Totals

| Group | Approx. cost | What's in it |
|---|---:|---|
| **The Badger** (core, radio, panel, case, wiring, solar input) | **≈ $901 + SSD** | Pi 5, battery UPS, NVMe HAT, Wi-Fi 6, two SDRs (weather radio + aircraft/satellites), LoRa, GPS, screen and knob, connectors, filament, hardware and consumables |
| SSD | volatile | 512 GB (Field) · 1 TB (Base) · 2–4 TB (Ultimate) |
| Field power kit | ≈ $15–200 | you already have the Jackery panel: a DC8020 adapter cable is all that's required. Add a 256 Wh LiFePO4 (≈ $115) for overnight and cloudy days; the Victron MPPT only if you pair it with that battery |
| Sensor kit (optional) | ≈ $60 (+ ≈ $110 Geiger counter) | outdoor temperature/humidity/pressure, waterproof temperature probe, flood float switch, water-level sensor, cable and plug for the rear SENSORS socket. See [Sensor kit](#sensor-kit-optional) and [features/sensors.md](features/sensors.md) |
| Transport | ≈ $145 | foam-lined hard case, Faraday bag for spares |
| Test fixtures (for testing a built Badger, mostly unattended) | ≈ $135 (+ ≈ $95 optional) | webcam on the front panel, smart plug for power-cut tests, a second Meshtastic radio, USB-C power meter, spare stick, card and reader. See [Test fixtures](#test-fixtures) |
| Tools, if you don't have them | ≈ $130 | soldering iron with heat-set insert tips, multimeter, crimpers and strippers, hex keys, glue gun. Plus a 3D printer that can do TPU |

## Where to buy: a four-order plan

Most of the Badger comes from four places.

1. **A Raspberry Pi approved reseller** (Adafruit, PiShop.us, CanaKit, Micro Center, Digi-Key or SparkFun in the US).
   - Pi 5 8 GB, Active Cooler, RTC battery.
   - Adafruit also stocks the panel-mount USB-A/USB-C/Ethernet extensions, a USB-C PD trigger board, the INA228 meter, the encoder, the button, the LED and an OLED, so one Adafruit order covers most of the front and rear panels.
   - The list of approved resellers is on each product's page at raspberrypi.com ("Buy now"). Buying there avoids grey-market boards.
2. **Amazon**, from the brand's own storefront where there is one.
   - Geekworm X1202 UPS and X1001 NVMe HAT.
   - ALFA AWUS036AXML Wi-Fi adapter.
   - Two RTL-SDR Blog V4 or V4L: one bare dongle, one as the kit with the dipole antenna set.
   - USB GPS, GX16 connectors, ideal-diode modules, Noctua fan, NVMe USB enclosure.
   - Heat-set inserts, filament, cables, consumables, Pelican case.
3. **RAKwireless store** (store.rakwireless.com) or **Rokland** (US).
   - Meshtastic node and its built-in 915 MHz flex antenna.
   - Rokland is also ALFA's US distributor.
4. **A battery specialist** (18650batterystore.com, IMRbatteries.com or Liion Wholesale).
   - Four matched genuine 18650 cells.
   - **Don't buy cells on general marketplaces**, where fakes ("9900 mAh") are common and dangerous.

**Also from:**

- **SSD:** a major retailer (B&H, Newegg, Micro Center, or "sold by Amazon").
- **Step-down regulator:** pololu.com.
- **Foam:** a craft store.
- **Solar kit:** a solar retailer.

## The list

### Core (≈ $379 + SSD)

| Part | Qty | Buy | Search for | ~$ |
|---|:-:|---|---|---:|
| Raspberry Pi 5, 8 GB | 1 | approved reseller | "Raspberry Pi 5 8GB" | 175 |
| Active Cooler | 1 | approved reseller | "Raspberry Pi 5 Active Cooler" | 5 |
| RTC battery, rechargeable ML-2020 (SC1163) | 1 | approved reseller | "Raspberry Pi 5 RTC battery" | 5 |
| UPS HAT, 4×18650, 6–18 V input — Geekworm X1202 (98 × 86 mm board) | 1 | geekworm.com, Amazon | "Geekworm X1202" | 50 |
| 18650 cells, 3,500 mAh flat-top (Molicel M35A / Samsung 35E) | 4 | battery specialist | "Molicel M35A flat top" | 32 |
| NVMe HAT, 2280 — Geekworm X1001 | 1 | geekworm.com, Amazon | "Geekworm X1001" | 18 |
| NVMe SSD 2280, low-power (WD SN5000 / SN770 class) | 1 | major retailer | "WD Blue SN5000 2TB" | volatile |
| microSD 32 GB (rescue copy) | 1 | anywhere | "microSD 32GB A1" | 8 |
| USB Wi-Fi 6 adapter, MT7921AU — ALFA AWUS036AXML (85 × 63 × 20 mm, RP-SMA jacks) | 1 | Rokland, Amazon | "ALFA AWUS036AXML" | 50 |
| Dual-band 2.4/5 GHz adhesive FPC antenna, ≤ 48 × 12 mm, **pre-wired to RP-SMA male**, ~20 cm (built in). Or a U.FL FPC antenna plus a U.FL→RP-SMA-male pigtail | 2 | Amazon, Digi-Key | "2.4G 5G FPC antenna RP-SMA male" | 16 |
| M.2 NVMe USB enclosure, USB-C 10 Gbps (load the SSD from a laptop; rescue access) | 1 | Amazon | "M.2 NVMe USB enclosure" | 20 |

### Radio (≈ $236)

| Part | Qty | Buy | Search for | ~$ |
|---|:-:|---|---|---:|
| RTL-SDR Blog V4 (or V4L). It has had Linux driver quirks: if it won't tune, use the rtl-sdr-blog build of the driver | 1 | RTL-SDR Blog store / Amazon | "RTL-SDR Blog V4" | 40 |
| RAK WisBlock Meshtastic kit, **915 MHz**. Runs from USB; a LiPo is optional | 1 | RAK store, Rokland | "RAK WisBlock Meshtastic starter kit 915" | 35 |
| USB GPS with built-in patch antenna, ≤ 42 × 42 × 16 mm (built in, under the lid). Many pucks are ~50 mm across and won't fit the cradle | 1 | Amazon, SparkFun | "u-blox M8 USB GPS module" | 25 |
| **SDR 2** (aircraft and weather satellites): RTL-SDR Blog V4 **kit** — dongle plus the dipole antenna set (telescopic elements, mount, 3 m and 60 cm coax). Set up as a 137 MHz V-dipole outdoors for satellites. Rides on a printed shelf above SDR 1 | 1 | RTL-SDR Blog store / Amazon | "RTL-SDR Blog V4 dipole kit" | 57 |
| 1090 MHz ADS-B whip, SMA male, 7–10 cm (screws onto the SDR 2 jack for aircraft) | 1 | Amazon | "1090MHz ADS-B antenna SMA male" | 12 |
| SMA female bulkhead to **right-angle** SMA male, 20–25 cm RG316 (SDR 2 jack to the second dongle; the plug turns toward the Wi-Fi side, clear of the Ethernet jack) | 1 | Amazon | "SMA female bulkhead right angle SMA male RG316" | 7 |
| SMA dust caps (whichever jack has no antenna on it) | 1 pack | Amazon | "SMA female dust cap" | 2 |
| Compact 4-port USB 2.0 hub board, ≤ 44 × 25 mm (all four ports used: SDR 1, SDR 2, GPS, LoRa) | 1 | Amazon, Adafruit | "USB 2.0 hub board 4 port mini" | 8 |
| 915 MHz FPC antenna, U.FL, ≤ 80 × 20 mm (built in) | 1 | RAK store, Rokland | "915MHz FPC antenna IPEX" | 6 |
| SMA female bulkhead to SMA male cable, 10–15 cm RG316 (SDR panel jack to the dongle; or run the dongle's own SMA through the panel) | 1 | Amazon | "SMA female bulkhead to SMA male RG316" | 6 |
| Folding SDR whip: SMA male, swivel knuckle, ≤ 135 mm folded, ≤ 9 mm grip (2 m/70 cm or 136–174 MHz) | 1 | Amazon, ham radio stores | "SMA male folding antenna 2m 70cm" | 15 |
| Short internal USB cables, 10–20 cm, right-angle plugs where needed (Pi→Wi-Fi adapter, Pi→hub, hub→SDR 1, hub→SDR 2, hub→RAK USB-C, hub→GPS) | 1 set | Amazon | "short right angle USB cable" | 15 |
| 90° USB-A and RJ45 adapters (only 25 mm of plug room beyond the Pi's ports; point them **up** — SDR 2 sits 3 mm below) | 1 set | Amazon | "90 degree USB A adapter" | 8 |

### Front and rear panels (≈ $153)

| Part | Qty | Buy | Search for | ~$ |
|---|:-:|---|---|---:|
| M8 4-pin A-coded **female panel socket**, front mount, IP67, with leads (rear SENSORS socket: 1 = 3.3 V, 2 = GND, 3 = 1-Wire, 4 = float). A different connector from the GX16 power input on purpose | 1 | Amazon, Digi-Key | "M8 4 pin female panel mount connector front" | 6 |
| 100 mA resettable polyfuse (3.3 V to the sensor socket; protects the Pi if an outdoor cable is crushed) + 4.7 kΩ and 10 kΩ pull-ups | 1 set | Amazon, Digi-Key | "PTC resettable fuse 100mA" | 2 |
| 1.3″ OLED 128×64, I²C, SH1106 | 1 | Amazon, AliExpress | "1.3 inch OLED SH1106 I2C" | 8 |
| EC11 rotary encoder with push, 15 mm D-shaft (cut to fit) | 1 | Adafruit, Digi-Key | "EC11 rotary encoder switch 15mm shaft" | 3 |
| Low-profile knob, 14 × 8 mm, 6 mm D-shaft | 1 | Adafruit, Amazon | "14mm low profile knob 6mm D shaft" | 2 |
| 12 mm momentary button | 1 | Adafruit, Digi-Key | "12mm momentary push button panel mount" | 3 |
| 5 mm LED, **red or amber** (not green) + 330 Ω resistor | 1 | Adafruit, Digi-Key | "5mm amber LED" | 1 |
| 5 mm clip-in LED holder (6.3 mm hole) | 1 | Digi-Key, Amazon | "5mm LED holder clip" | 1 |
| Panel-mount USB-A 3.0 extensions, 30 cm, M3 ears | 2 | Adafruit, Amazon | "panel mount USB 3.0 extension cable" | 12 |
| Panel-mount USB-C extension, M2.5 ears | 1 | Adafruit, Amazon | "panel mount USB-C extension" | 8 |
| USB-C PD trigger, **fixed 15 V** (Adafruit HUSB238 set to 15 V). A ZY12PDN can output 20 V: set it to 15 V and lock it | 1 | Adafruit, Amazon | "USB-C PD trigger 15V" | 5 |
| DC5521 (5.5 × 2.1 mm) barrel plug pigtail, for the X1202 input | 1 | Amazon | "DC5521 male pigtail" | 2 |
| GX16-2 aviation connector pair (plus one spare plug per adapter cable) | 1 | Amazon | "GX16 2 pin aviation connector" | 6 |
| 12 V 5 A step-down regulator, up to 50 V input — Pololu D36V50F12 | 1 | pololu.com, Amazon | "Pololu D36V50F12" | 35 |
| 5 A inline blade fuse holder + SMBJ30A TVS diode | 1 | Digi-Key, Amazon | "SMBJ30A" / "inline blade fuse holder" | 3 |
| Reverse-polarity stage: P-channel MOSFET ≥ 40 V, ≥ 10 A (AOD4185, IRF4905) + 15 V zener + 100 kΩ | 1 | Digi-Key, Amazon | "AOD4185 P-channel MOSFET" | 3 |
| INA228 current/voltage sensor, 85 V (optional input meter) | 1 | Adafruit, Digi-Key | "INA228 breakout" | 15 |
| Ideal-diode module, LM74700-based (single channel: one per source) | 2 | Amazon, AliExpress | "LM74700 ideal diode module" | 12 |
| Panel-mount RJ45 extension, M3 ears | 1 | Adafruit, Amazon | "panel mount ethernet extension" | 8 |
| Noctua NF-A4x10 5V fan + 40 mm dust filter | 1 | Amazon | "Noctua NF-A4x10 5V" | 15 |
| AO3400-class logic-level MOSFET (module or on perfboard) + flyback diode, for the case fan | 1 | Amazon, Digi-Key | "AO3400 MOSFET module" | 3 |

### Case (≈ $86, unchanged: the SDR shelf uses filament and fasteners already listed)

| Part | Qty | Buy | Search for | ~$ |
|---|:-:|---|---|---:|
| PETG filament, **yellow**, 1 kg (base, sled and SDR shelf, ~465 g; also the lid inlay colour) | 1 | Amazon, maker stores | "yellow PETG filament 1.75" | 20 |
| PETG filament, **black** (lid, ~160 g; a 0.5–1 kg spool) | 1 | Amazon, maker stores | "black PETG filament 1.75" | 15 |
| TPU 95A filament, **black**, 1 kg (bumpers, dampers, gasket, SDR clips, ~150 g) | 1 | Amazon | "black TPU 95A filament" | 25 |
| M3 heat-set inserts (8 lid, 4 bumper bottom, 4 power tray, 4 SDR clips, 2 SDR shelf) | 22 | Amazon | "M3 heat set insert" | 4 |
| M2.5 heat-set inserts (sled, Pi/UPS holes) | 4 | Amazon | "M2.5 heat set insert" | 1 |
| M3 button-head screw kit: 8 × M3×8 (lid edge, bumper bottom), 4 × M3×10 (lid corners), 8 × M3×6 (SDR clips, tray), 6 × M3×10 + nuts (USB-A, RJ45 ears), 4 × M3×20 + nuts (fan), 2 × M3×12 (SDR shelf), 8 washers | 1 | Amazon, hardware store | "M3 button head screw kit" | 9 |
| M2.5 and M2 kit: M2.5 for the USB-C ears and the UPS; 4 × M2 + nylon spacers + nuts for the OLED | 1 | Amazon, hardware store | "M2 M2.5 screw nylon spacer kit" | 7 |
| 3 mm closed-cell EVA/neoprene foam | 1 | craft store, McMaster | "EVA foam sheet 3mm" | 3 |
| 20 mm hook-and-loop strap | 1 | anywhere | "velcro strap 20mm" | 2 |

### Wiring and consumables (≈ $45)

| Part | Qty | Buy | Search for | ~$ |
|---|:-:|---|---|---:|
| Silicone hook-up wire (18 and 24 AWG), heat-shrink, Dupont jumpers, I²C splices | 1 | Amazon, Adafruit | "silicone wire kit" | 15 |
| Zip ties, ≤ 2.2 mm wide (the anchor slots are 2.4 mm) | 100-pack | anywhere | "2.2mm zip ties" | 3 |
| Kapton tape | 1 | Amazon | "kapton tape" | 5 |
| Hot glue sticks (secure the U.FL plugs) | 1 | anywhere | "mini hot glue sticks" | 3 |
| Thermal pad, 1 mm (SSD) | 1 | Amazon | "thermal pad 1mm" | 5 |
| Perfboard, cut to 80 × 26 mm (power tray carrier) | 1 | Amazon, Adafruit | "perfboard" | 3 |
| 2-pin 2.54 mm header for the Pi 5 J2 power-button pads | 1 | Adafruit, Digi-Key | "2 pin header 2.54mm" | 1 |
| Printable vinyl or laminated sticker paper (lid mark, panel labels, QR sticker) | 1 | Amazon, office stores | "printable vinyl sticker paper" | 10 |

### Sensor kit (optional)

Plugs into the rear **SENSORS** socket (or the front USB ports for the Geiger counter). Wiring, pins and placement are in [features/sensors.md](features/sensors.md). Any mix works; the Badger finds most of them by itself.

| Part | Qty | Buy | Search for | ~$ |
|---|:-:|---|---|---:|
| BME280 breakout, 3.3 V, I²C (temperature, humidity, pressure), in a vented weatherproof box — or an SHT31/SHT41 in a weather shield | 1 | Adafruit, Amazon | "BME280 breakout 3.3V" | 10 |
| DS18B20 waterproof temperature probe, 1–3 m | 1 | Amazon, Adafruit | "DS18B20 waterproof probe" | 4 |
| Float switch, normally open (flood alarm) | 1 | Amazon | "float switch normally open" | 4 |
| JSN-SR04T waterproof ultrasonic sensor (UART mode) + CP2102 3.3 V USB-TTL adapter (water level without touching the water) | 1 | Amazon | "JSN-SR04T" | 18 |
| M8 4-pin A-coded **male field-wireable plug** (mates with the SENSORS socket) | 1 | Amazon, Digi-Key | "M8 4 pin male field wireable connector" | 6 |
| 4-core shielded cable, 10–20 m (outdoor runs; keep I²C runs short, use the DS18B20 or a Meshtastic sensor node for distant spots) | 1 | Amazon, hardware store | "4 core shielded cable 22AWG" | 10 |
| PG7 cable glands, glue-lined heat-shrink, small IP65 junction box | 1 set | Amazon | "PG7 cable gland IP65 junction box" | 8 |
| *Optional:* USB Geiger counter, GQ GMC-320 Plus V5 (front USB port; a warning, not a calibrated dose) | 1 | gqelectronicsllc.com, Amazon | "GQ GMC-320 Plus" | 110 |
| *Optional:* ADS1115 + 4–20 mA vented pressure transducer + 150 Ω + 5→24 V boost (depth in wells or deep tanks) | 1 set | Amazon | "4-20mA water level transducer" | 53 |

### Field power kit (≈ $15–200; you already have the panel)

| Part | Buy | Search for | ~$ |
|---|---|---|---:|
| 12.8 V 20 Ah LiFePO4 (LiTime, Renogy, Dakota Lithium) | solar retailer, Amazon | "12V 20Ah LiFePO4" | 115 |
| Victron SmartSolar MPPT 75/10 (optional; only with the LiFePO4, for best harvest or 24 V-class panels) | Victron dealer, Amazon | "Victron SmartSolar 75/10" | 60 |
| Portable folding panel — **you have a Jackery SolarSaga 40W Air**; a 100 W panel for Balanced around the clock | solar retailer, Amazon | "100W folding solar panel" | (have) |
| Adapter cables: DC8020 (Jackery) → GX16, plus MC4 / XT60 / Anderson / car plug as needed | Amazon ("DC8020 to Anderson", "MC4 to Anderson" adapters + a GX16 plug) | "DC8020 adapter cable" | 15 each |
| Fused 12 V cable to the GX16 plug | DIY | — | 10 |

### Transport (≈ $145)

| Part | Buy | Search for | ~$ |
|---|---|---|---:|
| Pelican 1400-class case with foam (interior ≈ 300 × 225 × 132 mm) | pelican.com, B&H, Amazon | "Pelican 1400 with foam" | 115 |
| Faraday bag for spares (Mission Darkness) | MOS Equipment, Amazon | "Mission Darkness Faraday bag" | 30 |

### Tools (≈ $130, if you don't have them)

| Part | Buy | Search for | ~$ |
|---|---|---|---:|
| Soldering iron with heat-set insert tips (Pinecil + tip set) | Amazon, Pine64 | "Pinecil heat set insert tip" | 55 |
| Multimeter, auto-ranging | anywhere | "auto ranging multimeter" | 25 |
| Dupont/JST crimper + wire strippers | Amazon | "dupont crimper kit" | 30 |
| Hex keys (1.5, 2, 2.5 mm) + small Phillips drivers | anywhere | "hex key set" | 10 |
| Mini hot glue gun | anywhere | "mini hot glue gun" | 10 |

### Test fixtures

These let the Badger be tested end to end over SSH with almost nobody touching it: the scripts in `tests/hw/` read the front panel through the webcam, cut and restore power with the smart plug, and talk to the second radio over USB. None of them go inside the Badger.

| Part | Qty | Buy | Search for | ~$ | What it tests |
|---|:-:|---|---|---:|---|
| USB webcam, 1080p (Logitech C270/C920 class) | 1 | anywhere | "Logitech C920" | 30 | reads the screen, status light and knob in snapshots |
| Gooseneck or clamp webcam mount | 1 | Amazon | "webcam clamp mount gooseneck" | 12 | holds the camera square to the front panel |
| Smart plug with a **local** HTTP API and power metering (Shelly Plug US Gen3; no cloud account needed) | 1 | Amazon, shelly.com | "Shelly Plug US Gen3" | 20 | cuts and restores the charger on command: battery takeover, auto-Endurance at 30 %, clean shutdown at 6 %, restart when power returns, brownouts |
| Second Meshtastic radio, **915 MHz** (Heltec WiFi LoRa 32 V3 with antenna, or a second RAK kit) | 1 | Heltec store, Rokland, Amazon | "Heltec LoRa 32 V3 915" | 25 | two-way mesh: check-ins, `!q` library lookups over the air, range, unplug/replug recovery |
| Inline USB-C power meter, 240 W (shows V, A, W and the PD contract) | 1 | Amazon | "USB C power meter 240W" | 20 | checks the PD trigger really asks for 15 V, and the Badger's own watt readings |
| USB flash drive, 32 GB | 1 | anywhere | "32GB USB 3 flash drive" | 8 | file sharing, eject, update-bundle export and import |
| Spare microSD 32 GB + USB card reader | 1 | anywhere | "microSD card reader USB-C" | 16 | builder flash and first-boot runs without touching the real card |
| Ethernet cable, 2 m | 1 | anywhere | "cat6 ethernet cable 2m" | 5 | keeps the Badger reachable over SSH while its Wi-Fi is under test |
| *Optional:* programmable bench supply, 0–60 V, USB-controllable (Riden RD6006 class) | 1 | Amazon, AliExpress | "Riden RD6006" | 90 | sweeps the solar/DC input 8–30 V, open-circuit 25–26 V like your SolarSaga, over-voltage and reverse-polarity checks, slow sags like a cloudy panel |
| *Optional:* DS18B20 waterproof temperature probe | 1 | Amazon, Adafruit | "DS18B20 waterproof probe" | 5 | air temperature inside the closed case during the heat test |

Already covered elsewhere: the multimeter (Tools) and your SolarSaga 40W (Field power kit). Weather-alert decoding is tested on the real weekly NOAA test broadcast and with recorded alert audio, so it needs no transmitter (transmitting on NOAA frequencies isn't legal anyway).

## Before you order

1. **Check the UPS and NVMe HATs stack together.** Geekworm sells the X1202 (under the Pi) and X1001 (on top) as compatible; confirm on their wiki. Also check which DC-in connector your X1202 revision has; the DC5521 pigtail assumes a 5.5 × 2.1 mm barrel.
2. **Print the `panel_test` first.** It is just the front and rear walls. Panel-mount connectors vary by vendor. Measure yours and adjust the case parameters before printing the full base.
3. **Buy the 915 MHz LoRa version** in the US (868 MHz in the EU).
4. **The SDR:** the RTL-SDR Blog V4 is being end-of-lifed; the V4L works the same for weather radio. Either may need the rtl-sdr-blog driver build.
5. **Check the GPS size.** The cradle takes ≤ 42 × 42 × 16 mm. A bare u-blox board fits; many round pucks (~50 mm) don't.
6. **Set the PD trigger to 15 V before you connect it.** 20 V is above the X1202's 18 V limit.
7. **Substitutions are fine** for generic parts (OLED, encoder, button, extensions, hub) as long as the cutout sizes match. For the Pi, UPS, cells, SSD class and Wi-Fi chipset, stick to the listed parts or check compatibility first.
