# Sensors

The Badger can watch a few things for you and warn everyone when they go wrong:

- **Temperature, humidity and air pressure.** A falling barometer means a storm is coming. Freezing temperatures can burst pipes.
- **Water.** A flood switch on the basement floor, or the water level in a tank, well or creek.
- **Radiation.** A USB Geiger counter.
- **Readings from other radios.** Meshtastic nodes nearby that carry their own sensors, for example a weather node on a hill or a level sensor at the creek.

Open **Sensors** on the launcher's home screen (under *Neighbourhood*) for the latest readings in large print. Tap a reading for its chart over 24 hours, 7 days, 30 days or a year. Alerts are shown at the top of the page in red or amber.

When something crosses an alert level, the Badger:

1. shows the alert at the top of the Sensors page;
2. posts it on the **neighbourhood board** (or the family board, if you choose), signed *Badger sensors*;
3. sends a short message over the **mesh radio** for urgent alerts (flood and radiation, unless you change this), on your **family channel** unless you choose the public one. It starts "Sensors:" (over the radio nothing names the Badger) and goes out even if the launcher itself isn't running.

When the reading is back to normal, it posts "back to normal" the same way.

## Standard alerts

Nothing needs setting up. These alerts apply to every sensor plugged into the Badger:

| Alert | When | Clears when | Goes to |
|---|---|---|---|
| Flood | the flood switch reads wet twice in a row (10 seconds) | dry twice in a row | neighbourhood board + mesh |
| Freezing | below 0 °C (32 °F), two readings in a row | above 1 °C | neighbourhood board |
| Heat | above 40 °C (104 °F) | below 38 °C | neighbourhood board |
| Radiation | above 1.0 µSv/h, **or** 5 times this Badger's normal level (the median of the last 24 hours), three readings in a row | back under the level | neighbourhood board + mesh |

Natural background radiation is usually 0.05–0.30 µSv/h, depending on where you live. 1.0 µSv/h is well above normal anywhere but still far below harmful for short periods. Treat an alert as "find out what's going on", and use the library (Medical → radiation emergencies) for what to do.

The same alert isn't announced again within its "quiet" time (30 minutes for flood, 6 hours for freezing and heat, 1 hour for radiation). It stays on the Sensors page the whole time.

## Changing alerts (admin)

Unlock admin mode under Settings, then open **Sensors** and scroll down to **Sensor settings**. There you can:

- **Rename sensors.** "Basement floor" means more than "Flood switch". Names appear in alerts, which everyone on the Wi-Fi sees.
- **Change, add or remove alerts.** Each alert has:
  - which sensor it watches: one sensor, or every sensor here;
  - which reading it watches;
  - its levels: above, below, and for radiation "× normal";
  - **Gap to clear**: how far back the reading must come before the alert clears. This stops an alert from switching on and off when a reading hovers near the line;
  - **Readings in a row** before it triggers;
  - **Quiet for**: how long before the same alert can be announced again;
  - which board it is posted on;
  - whether it also goes over the mesh.
- **Choose the mesh channel** (family, the default, or neighbourhood) and an optional **readings summary** every few hours, like "Sensors: 4.2°C, 81%RH, water 12cm, 0.11µSv/h". Off by default; it also goes on the family channel unless you choose otherwise.
- **Pick which mesh radios to show.** Readings from other people's radios appear only if you tick them.
- **Send a test message** to check that board posts (and the mesh) work.

**Back to the standard alerts** undoes your alert changes.

The same settings can be written in `/etc/prepper/prepper.toml` under `[sensors]`; see the `[sensors]` section of `os/prepper.toml.example`. Changes made on the page win.

### Mesh airtime

LoRa is slow, and everyone in range shares it. The Badger sends at most 4 sensor messages an hour: at least 10 minutes apart, or 2 minutes for urgent alerts. Messages waiting at the same time are combined into one, up to 200 bytes.

## Hardware

Each part is optional. Buy only what you need.

| Part | ~USD | Why |
|---|---:|---|
| BME280 breakout, 3.3 V, I²C (Adafruit 2652, Pimoroni, or a generic purple board, **not** a BMP280 if you want humidity) | 8–20 | temperature, humidity and air pressure. Found automatically |
| *or* SHT31 / SHT41 in a weatherproof mesh housing | 10–15 | temperature and humidity, accurate outdoors. Found automatically |
| DS18B20 waterproof probe (stainless tip, 1–3 m cable) + 4.7 kΩ resistor | 4 (2–5 pack: 10) | temperature in long cable runs: outside, freezer, well house, greenhouse. Found automatically |
| Float switch (vertical PP type, M10 stem) + 10 kΩ resistor | 4 | flood alarm on the floor or a high-water mark |
| JSN-SR04T **v3** (or AJ-SR04M, DFRobot A02YYUW) waterproof ultrasonic sensor | 12 (A02YYUW: 35) | water level in a tank, well or creek, without touching the water. UART mode |
| USB-to-TTL adapter, CP2102, 3.3 V | 6 | the easy way to connect the ultrasonic sensor (no level shifting) |
| ADS1115 16-bit ADC breakout + submersible pressure transducer (4–20 mA, 0–5 m, vented cable) + 150 Ω resistor + 5 V→24 V boost module | 10 + 40 + 3 | water depth in a well or deep tank, where ultrasonic can't reach |
| USB Geiger counter: **GQ GMC-320 Plus V5** (or GMC-500/600) | 110–200 | radiation, with its own display and battery |
| *or* a kit with serial text output (MightyOhm, a "RadiationD" board with a USB-serial adapter) | 40–100 | cheaper; set its port in the config |
| M8 4-pin A-coded field-wireable plug for the rear SENSORS socket (the socket itself is built into every Badger) | 6 | one sealed socket for the outdoor probe and float switch |
| 4-core shielded cable (alarm cable, 0.22 mm²), 10–20 m | 10 | from the socket to the probes |
| PG7 cable glands, heat-shrink with glue, small IP65 junction box | 8 | waterproofing joints outdoors |
| 100 mA resettable fuse (polyfuse) | 1 | protects the Pi's 3.3 V supply if an outdoor cable gets crushed |
| A Meshtastic node with a BME280 (RAK4631 + RAK1906, or Heltec + BME280) | 35–60 | readings from far away: a creek, a barn, a hilltop. Shows up as a mesh radio |

## Wiring guide

**Everything runs at 3.3 V.** The Pi's GPIO pins are 3.3 V only. 5 V on any GPIO pin can destroy the Pi. Power sensors from 3.3 V (header pin 1 or 17), unless a step below says otherwise.

Pins already used inside the Badger: GPIO 2/3 (I²C: UPS gauge at 0x36, display at 0x3C, input meter at 0x40), GPIO 6 (power-loss signal), GPIO 17/27/22 (knob), GPIO 23 (LED), GPIO 18 (fan). Don't connect anything else to those, except more I²C devices on GPIO 2/3.

| Sensor | Header pins (physical) | Notes |
|---|---|---|
| I²C sensors (BME280, SHT3x/4x, ADS1115) | 1 = 3.3 V · 3 = SDA (GPIO 2) · 5 = SCL (GPIO 3) · 9 = GND | shared bus. Addresses: BME280 0x76/0x77, SHT 0x44/0x45, ADS1115 0x48–0x4B. The Pi already has 1.8 kΩ pull-ups; most breakouts add 10 kΩ more, which is fine for 2–4 boards |
| DS18B20 probes (any number) | 1 = 3.3 V (red) · 7 = data, GPIO 4 (yellow) · 9 = GND (black) | **4.7 kΩ from data to 3.3 V**, once, at the Badger end. Probes can share the three wires |
| Float switch | 18 = GPIO 24 · 20 = GND | **10 kΩ from GPIO 24 to 3.3 V**. For long cables add a 1 kΩ resistor in series with the GPIO and a 100 nF capacitor to GND. A second switch can use GPIO 25 (pin 22) |
| Ultrasonic sensor, UART | 8 = TXD (GPIO 14) → sensor RX · 10 = RXD (GPIO 15) ← sensor TX · 2 = 5 V · 6 = GND | a JSN-SR04T on 5 V sends 5 V back: put a 1 kΩ / 2 kΩ divider on its TX line before pin 10. Easier: use a 3.3 V USB-TTL adapter and set `port = "/dev/serial/by-id/…"` |
| Geiger counter | a front USB port | its own cable. Set `port = "/dev/serial/by-id/…"` (run `ls /dev/serial/by-id/`). Never point it at the LoRa node or GPS port |

### Mode resistors and polarity

- **JSN-SR04T UART mode.** The resistor on the board's mode pads (usually "R27") selects the mode, and the values differ between board versions: check the seller's datasheet. The Badger works with both UART modes, continuous output and "answer when asked" (0x55). Plain trigger/echo mode doesn't work. The DFRobot A02YYUW is UART out of the box.
- **Float switch.** Mount it so the contacts **open when water lifts the float**: on most switches, take off the retaining clip, flip the float, and put the clip back. This is `wet_when = "high"`, the default. A cut or unplugged cable then raises a flood alarm instead of staying silent. If the switch closes when wet, set `wet_when = "low"` and accept that a broken wire can't be detected.

### Cable lengths

- **I²C is for short distances only:** about 50 cm of plain wire, 1–2 m of good twisted cable at best. Mount a BME280 just outside the case: the inside is warm, and readings would be several degrees too high. A small vented, shaded box on the back of the shelf works well. Don't run I²C to the garden.
- **DS18B20 probes reach 20–30 m** on 4-core alarm or Cat5 cable. Use one pair for data and GND, another wire for 3.3 V, and run the cable as one line from probe to probe (no star wiring). This is the right choice for outdoor, freezer or well-house temperature.
- **Float switch:** any length. It's just a contact; the 1 kΩ + 100 nF filter keeps long runs quiet.
- **UART ultrasonic:** 9600 baud is fine to about 10 m.
- **Further than that** (a creek 300 m away, the barn, the far end of the property): use a **Meshtastic node with its own sensor**, running on a small solar panel. It sends environment telemetry over LoRa, and the Badger shows it under *From mesh radios* once you tick it. Only nodes on your family channel are accepted unless you allow the public channel. RS-485 sensors are not supported yet.

### Outdoor connector

The rear **GX16** socket is the 8–30 V **power input**. Never use it for sensors. Outdoor sensors plug into the rear **SENSORS** socket instead: an M8 4-pin, A-coded, IP67 socket (capped when unused), a different connector from the GX16, so the power plug can't go into it. Pins:

1. 3.3 V, through a 100 mA polyfuse
2. GND
3. 1-Wire data (GPIO 4, 4.7 kΩ pull-up inside the case)
4. float switch (GPIO 24, 10 kΩ pull-up inside the case)

### Waterproofing

- Only the probe tip of a DS18B20 and the transducer head of a JSN-SR04T are waterproof. Their circuit boards are not. Put those boards and every cable joint in an IP65 box, with cables through PG7 glands. Seal splices with glue-lined heat-shrink.
- Leave a drip loop in every cable before it enters a box or the case.
- Mount the ultrasonic sensor pointing straight down, **at least 25 cm above the highest water** (it is blind closer than that). In a creek, mount it over a stilling well: a vertical PVC pipe, 10 cm or wider, open at the bottom. It calms waves and keeps debris out of the beam. Measure `mount_height_cm` from the sensor face to your zero mark (the tank floor, or a marker on the creek bed).
- A pressure transducer needs a **vented** cable, with the vent tube kept dry, or its readings drift with the weather.

### Geiger counter placement

- Keep it in **one place**: about 1 m above the floor, away from granite worktops, old orange-glazed ceramics, gas-lamp mantles and concrete walls. The "× normal" alert compares against this spot's own last 24 hours.
- Indoors, it measures what reaches people indoors. To watch for fallout arriving, put it by a window or in a dry, shaded outdoor box on a USB extension (up to 5 m).
- Keep it plugged in. GQ counters charge from USB, and the Badger asks for counts per minute every minute.
- `factor` converts counts to µSv/h. The GMC-300/320 tube uses 0.0065. Other tubes differ; check your counter's manual. The Badger is not a calibrated dosimeter. Readings are a warning, not a measurement for medical decisions.

## After wiring

1. Re-run the installer, or reboot once if you added 1-Wire probes. The installer adds `dtoverlay=w1-gpio` to `/boot/firmware/config.txt` if it is missing, and a reboot is needed for it to take effect.
2. Open **Sensors**. Automatically found sensors appear within 10 minutes; restarting `prepper-sensord` finds them at once. Float switches, ultrasonic sensors, ADCs and Geiger counters must be listed in `[sensors]` first (see the example file).
3. As admin, check **Sensor settings → Sensors**. Each sensor says *working* or explains what's wrong: "not found — is it plugged in?", "no answer on the I²C bus", "checksum error (loose wire or missing 4.7 kΩ pull-up?)".
4. Press **Post a test message**.

## Limits

- Readings are taken every 30 seconds (float switches every 5 seconds). A flood alert reaches the board and the mesh within about 15 seconds.
- History: every reading for 48 hours, 10-minute averages for 30 days, hourly averages for a year. The database is capped at 64 MB.
- The mesh copy of an alert is limited to 200 bytes and a few messages an hour, so it may be shortened or combined with others.
- Readings from mesh radios are reported by other people's equipment. The Badger checks that the numbers are plausible, but it can't check that they're true. Don't base a decision on one remote reading alone.
- The Badger reads GPIO with the `gpioget` tool, which is reliable but not fast. It isn't suited to rain gauges or wind sensors that count pulses.

## Privacy

- **Active alerts** (flood, freezing, radiation) are shown to **everyone on the Badger's Wi-Fi**: they are safety information for the whole neighbourhood. **Readings and history** (a tank level, a freezer, a year of graphs) are for **family members and the admin** by default — they say a lot about a household. To show them to everyone, set `public = true` under `[sensors]`. Sensor names appear in alerts, so don't name sensors after people or rooms you'd rather keep private.
- Error details, device paths and the list of radios heard are shown only to the admin.
- Mesh alerts go out on the **family channel** by default (`[sensors] mesh_board = "family"`, `summary_board = "family"`). "Water in the basement" sent on the public channel tells anyone listening — and anyone taking radio bearings — about your house. Choose the neighbourhood (public) channel in the settings only if you want neighbours with Meshtastic radios to get them; without a family channel set up in `[radio]`, family-channel messages simply aren't sent.
- Nothing leaves the Badger except those board posts and mesh messages.
