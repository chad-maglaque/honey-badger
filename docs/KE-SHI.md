# Ke-Shi / Nu:Time™ on Honey Badger

Ke-Shi / Nu:Time™ divides the civil day into 10 parts, decimally. Its vocabulary is localized, and it shows the time alongside the Sun's position for your location. Honey Badger is a good home for it. The Badger already keeps GPS-disciplined UTC with an RTC battery for holdover, and serves time to everything on its Wi-Fi. So the Badger becomes the family's Nu:Time™ reference.

This document carries over the architecture handoff (`ke_shi_nu_time_architecture_handoff.md`, 2026-11-16) and records what Honey Badger implements. Items the handoff marked as **proposals** stay marked that way here.

## 1. Units (established)

| Unit | Subdivisions | Fraction of day | Duration |
|---|---:|---:|---:|
| Day | 10 gongs | 1 | 86,400 s |
| Gong / gēng 更 | 100 beats | 1/10 | 2 h 24 min = 8,640 s |
| Beat / fēn 分 | 100 ticks | 1/1,000 | 86.4 s |
| Tick / kè 刻 | — | 1/100,000 | 0.864 s |

- Display is `GG:BB:TT`, with gong 00–09, beat 00–99 and tick 00–99.
- 1,000 beats or 100,000 ticks make a day.
- Noon is `05:00:00` (beat 500). 06:00 is `02:50:00`; 07:12 is `03:00:00`; 18:00 is `07:50:00`. 23:59:59 is `09:99:98`, and the last tick `09:99:99` starts at 23:59:59.136.
- **A beat is not 1/100,000 of a day. That is a tick.**
- Values are truncated to the current tick, never rounded ahead.

## 2. How Honey Badger implements it

```
 GPS (+PPS) ──► gpsd ──► chrony ──► system clock (UTC)  ◄── Pi 5 RTC + ML-2020 cell (holdover)
                              │
                              ├──► NTP for the LAN (allow 10.10.10.0/24): phones, laptops, future desk clocks
                              ▼
               software/nutime/nutime.py  — conversion core (integers only) + reference layer + solar model
                 │                │                      │
            /api/time         front panel             tools/nutime.js — same core, in the browser
         (nu-time/v1 JSON)   "Nu:Time™" page        tools/nutime.html — dial · GG:BB:TT · 1,000-beat sky strip
                                                    tools/sun.html — sunrise/twilight table, same solar model
```

| Piece | File | What it does |
|---|---|---|
| Conversion core | `software/nutime/nutime.py` | `ticks = floor(µs × 100000 / day_µs)` in exact integers. Fields, formatting, inverse (`parse`). No floats at tick boundaries |
| Reference layer | same | `utc`, `civil` (IANA zone; DST policy `wall` or `elapsed`), `solar` (mean, from longitude), `apparent` (mean + equation of time). Optional legacy 3.6° longitude quantization |
| Locale layer | same | labels only (zh/en/fr/es/de). Numbers and field order never change |
| Solar model | same | NOAA general solar position: altitude, azimuth, sunrise/sunset, civil twilight, solar noon, polar day/night. Positions come from the **UTC instant**; "today's" sunrise and sunset are for the **local calendar date at the chosen place** (its IANA zone, or its mean-solar date when only coordinates are known), not the UTC date |
| Browser core | `software/portal/static/tools/nutime.js` | a line-for-line mirror in integer milliseconds, truncating (never rounding) to the tick. Checked against the Python core's vectors. Falls back to `format()` where old Safari lacks `formatToParts`, and to the device clock where there is no time-zone support |
| Sun tool | `software/portal/static/tools/sun.html` | sunrise, sunset, civil twilight and day length for any date, from the same NOAA model in `nutime.js`; the Badger has one solar model |
| Clock tool | `software/portal/static/tools/nutime.html` | canvas dial and readout. Hands are recomputed from the absolute instant every frame (`requestAnimationFrame`); astronomy is throttled to 1 Hz |
| API | `GET /api/time` | `nu-time/v1` record plus clock-sync quality from chrony |
| Front panel | `software/panel/panel.py` | home screen cycles to a "Nu:Time™ `GG:BB:TT`" page |
| Config | `[time]` in `prepper.toml` | default reference, locale, DST policy, time zone, home lat/lon until the GPS has a fix |
| Tests | `tests/test_nutime.py` | 19 tests covering every acceptance criterion in handoff §11, plus local-date sun events and civil twilight |

### The clock tool

The tool lives at Tools → Ke-Shi / Nu:Time™ clock.

- **Display:** an analog dial with three hands.
  - The **gong hand** goes round once a day.
  - The **beat hand** goes round once per gong (2 h 24 min).
  - The **tick hand** goes round once per beat (86.4 s).
  - An outer ring shows daylight from sunrise to sunset for the chosen place, on that place's own calendar date.
- **Readouts:** `GG:BB:TT`, the spoken form in the chosen language, and "beat 583.33 of 1,000".
- **1,000-beat day strip:** one column per beat, coloured by the Sun's altitude at that moment, with a marker for now. Sunrise and sunset visibly move with date and latitude; they are never pinned to beats 250 and 750.
- **Background:** the page background follows the current solar altitude through night, twilight, golden hour and day. It fades over 0.8 s.
- **Location:** this Badger (GPS fix, else the configured home), this device (needs HTTPS), 17 cities including Helsinki and Longyearbyen (polar), or typed coordinates. With no location it uses Greenwich, with Greenwich's own time zone.
- **Clock sync:** when the tool can reach the Badger, it measures the phone's clock offset against the GPS-disciplined Badger and corrects it. It shows the offset and the Badger's time source.
- **Offline:** cached by the iPad/phone app with the other tools, so it keeps working away from the Badger.

### API

`GET /api/time?mode=civil&locale=zh&lat=60.1699&lon=24.9384&tz=Europe/Helsinki`

```json
{
  "schema": "nu-time/v1",
  "instant": "2026-11-16T12:00:00Z",
  "reference": "civil-local",
  "location": { "latitude": 60.1699, "longitude": 24.9384, "ianaTimeZone": "Europe/Helsinki" },
  "ticksOfDay": 58333, "gong": 5, "beat": 83, "tick": 33, "beatsOfDay": 583.33, "display": "05:83:33",
  "locale": "zh", "labels": { "gong": "更", "beat": "分", "tick": "刻" },
  "notes": { "dstPolicy": "wall" },
  "sun": { "altitude": 7.909, "azimuth": 207.341 },
  "sunEvents": { "polar": null, "sunrise": "…", "sunset": "…", "solarNoon": "…" },
  "sync": { "source": "GPS + PPS", "stratum": 1, "offsetSeconds": 0.000002, "synced": true },
  "locationSource": "query"
}
```

`sunEvents` are for today's local date at the requested place: in `tz` if given (or the `[time]` zone), else the mean-solar date from `lon`. An unknown `tz` (query or `[time] timezone`) falls back to UTC with a note in `notes.timezone` / `clock.tz_note` instead of failing.

The handoff's schema fields are kept. `reference` is always present, and the location is a separate field from the reference, so `05:00:00` in UTC can't be mistaken for local noon. Without query parameters the endpoint uses the `[time]` defaults and the Badger's GPS position.

## 3. Policy decisions

These are open items in the handoff that a family Badger needs a default for. All of them are configurable.

| Question (handoff §12) | Honey Badger default | Why | Alternatives available |
|---|---|---|---|
| Default canonical display | **civil local** | matches the family's wall clocks and schedules | `utc`, `solar`, `apparent` per request or in `[time]` |
| DST days | **`wall`**: fraction of 24 h wall-clock time, so the display skips or repeats an hour like a wall clock | the handoff's recommendation; familiar | `elapsed`: fraction of the real 23 h / 25 h day (API and Python core) |
| Mean vs apparent solar | both offered | mean is the handoff's definition; apparent puts `05:00:00` at true solar noon, which is useful for sun-based navigation | — |
| Legacy 3.6° longitude rounding | off | it made the old prototype's dial disagree with its readout | `quantize_longitude=True` |
| Leap seconds | follow the system clock (POSIX time has no leap seconds; chrony slews through them) | standard on Linux | — |

The browser clock implements the `wall` DST policy only. The `elapsed` policy is available from the Python core and `/api/time`.

## 3a. Nu:Time™ as a display format everywhere

Nu:Time™ is also one of three display formats for **every** time the Badger shows: Local, UTC or Nu:Time. That covers the status-bar clock, check-in board messages, weather-alert expiry, update history, sunrise and sunset in the tools, and the front screen.

- **One shared formatter:** `NuTime.fmt()` in `tools/nutime.js` does the work in the browser. In Nu:Time mode it uses the `[time]` reference (civil by default) and adds `NT`, e.g. `2026-11-17 05:83:33 NT`.
- **Front screen:** gets all three strings from `/api/status` → `clock`.
- **Each phone's choice:** stored in its browser; tap the clock to cycle.
- **Badger-wide default:** `[time] display`.

## 4. Localization (established)

| Locale | Gong | Beat | Tick | Spoken |
|---|---|---|---|---|
| Chinese | gēng 更 | fēn 分 | kè 刻 | gēng : fēn : kè |
| English | gong | beat | tick | gong : beat : tick |
| French | gong | beat | tic | gong : beat : tic |
| Spanish | gong | beat | tic | gong : beat : tic |
| German | gong | beat | tick | gong : beat : tick |

Translate the labels, never the durations or the field order.

## 5. Solar model and sky colour

- **Algorithm.** The Badger uses the NOAA general solar position equations instead of SunCalc, so there is no dependency and identical code runs in Python and JavaScript, offline. It is accurate to about a minute for sunrise/sunset and a few tenths of a degree in altitude. That's right for a clock face and sky colour; it is not navigation-grade.
- **Sky colour is artistic.** The palette goes from astronomical night through nautical and civil twilight, the warm horizon band and golden hour, to day blue. Real sky colour also depends on weather, aerosols and elevation.
- **Matrix idea not used.** The handoff's effective-latitude lookup matrix (about 1,000 × 227) is an approximation idea. The Badger computes the actual altitude instead; 1,000 evaluations per strip repaint are trivial.

## 6. Hardware desk clock (proposal, not built)

The handoff's §8 hardware design becomes a natural Badger accessory: a small battery-powered Nu:Time™ clock on the family's desk or wall, syncing from the Badger.

| Part | Suggestion | Notes |
|---|---|---|
| MCU | ESP32-C3 or Raspberry Pi Pico W | Wi-Fi to reach the Badger's NTP; deep sleep between ticks |
| RTC | DS3231 (TCXO, ±2 ppm) with a coin cell | about 0.2 ticks/day of holdover drift at 2 ppm |
| Display | 2.9″ e-paper updated once per beat (86.4 s), or a small LCD/OLED for live ticks | `GG:BB` large, `TT` small |
| Sync | SNTP to the Badger (`10.10.10.10`) | the Badger's chrony already serves the LAN, GPS-disciplined |

**Firmware rules (from the handoff).**

- Keep UTC in the RTC.
- Derive `GG:BB:TT` from the absolute timestamp every time: `floor(ms_since_midnight × 100000 / 86400000)`.
- Never sleep 864 ms in a loop; schedule on the next absolute tick boundary.
- Compute the Sun separately and slowly.

**Oscillator maths (reference).**

- **Watch crystal:** at 32.768 kHz a tick is 28,311.552 cycles. Alternate 28,311 and 28,312 cycles with an accumulator: 69 of every 125 ticks are long.
- **Fast clock:** any clock frequency divisible by 125 divides exactly (1 MHz gives 864,000 cycles per tick).
- **Accuracy:** depends on ppm, temperature and aging, not on the divider. 20 ppm is about 2 ticks/day; 2 ppm is about 0.2 ticks/day.

## 7. Historical note

Ke-Shi borrows a linguistic and conceptual lineage from East Asian timekeeping: named watches (更), divisions (分, 刻), and attention to the Sun. Its `10 × 100 × 100` arithmetic is modern. In history, gēng were usually night watches, and kè varied by era; neither matches these durations. Its decimal structure matches French Revolutionary time: a tick equals a decimal second, and a beat equals a decimal minute. The names, emphasis on geolocation, and sky presentation are its own. Treat "Ke-Shi" and its character styling as product nomenclature.

## 8. Open items carried forward

- A finalized desk-clock PCB, part numbers and enclosure.
- A sync transport for non-Wi-Fi clocks. Options: LoRa broadcast from the Badger's Meshtastic node, or GPS on the clock itself.
- Whether other Nu:Time implementations should share the Badger's `civil` default.
- Historically validated Chinese branding beyond the agreed UI labels.
