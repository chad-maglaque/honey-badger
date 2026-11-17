<p align="center"><img src="docs/brand/honey-badger-logo.svg" width="440" alt="Honey Badger"></p>

# Honey Badger

An offline knowledge server you build yourself. It makes its own Wi-Fi network and serves an offline library to any phone, iPad or laptop through a browser. The library covers reference, medical, survival, repair, maps, education, books, video and an offline AI. It runs on its own battery, takes 12 V or solar input, receives NOAA weather alerts, bridges to a LoRa mesh, and updates itself from the internet, from other Badgers, or from a USB stick.

It also helps a family and its neighbours get through an emergency together:

- **Family:** a private family board, a supplies list that counts the days of water and food left, and an encrypted document vault for IDs, insurance and medical papers.
- **Neighbourhood:** missing people and lost & found with photos, a skills and resources directory, an aid-station log with a public status line, and optional sensors that warn of floods, freezing, heat or radiation.
- **Radio & sky:** the NOAA forecast written out as text, aircraft overhead, and cloud pictures from Meteor-M weather satellites, received by a second built-in radio.
- **Voice:** ask a question out loud and hear the answer.

Everything it does, and who can use each part: [docs/FEATURES.md](docs/FEATURES.md).

It is a superset of [Prepper Disk Premium](https://www.prepperdisk.com/pages/prepper-disk-premium-contents), redesigned after surveying Kiwix Hotspot, Project N.O.M.A.D., PrepperPi, Doom Box, RACHEL and others. [docs/DESIGN.md](docs/DESIGN.md) has the survey and lists what each gap closes.

| | Prepper Disk Premium | Honey Badger |
|---|---|---|
| Board / storage | Pi 4B 2 GB · 512 GB SD | Pi 5 8 GB · 512 GB – 4 TB **NVMe** · RTC battery |
| Content | 16 modules | 138 catalog items; 290 GB / 480 GB / 1.35 TB tiers ([CONTENTS](docs/CONTENTS.md)) |
| Power | external adapter | built-in 50 Wh UPS, USB-C PD + 8–30 V solar/DC in, auto Endurance mode, clean shutdown |
| Wi-Fi | 20 devices | Wi-Fi 6 USB adapter, 5 GHz, WPA2/WPA3 or open, captive portal; built-in antennas |
| Updates | web console, full downloads | rsync/git deltas, signed peer and USB-bundle sync, zimdiff patches, scheduled, resumable, verified |
| Works without a phone | — | OLED + knob: QR to join, status, power modes, updates, shutdown |
| Radio | RepeaterBook data | two SDRs: NOAA SAME alerts and the forecast as text; aircraft (ADS-B) and Meteor-M weather-satellite pictures. Meshtastic mesh bridge, GPS time and position |
| AI | — | "Ask the Library": local model answers only from the Badger's articles, with citations; also by voice |
| Problem solver | — | works out power, water, structure, heat, radio and survival problems step by step with calculators (and a sandboxed Python), showing its working ([solver](docs/features/solver.md)) |
| Family & neighbours | — | family board, supplies and rationing, encrypted document vault, missing & found, skills directory, aid-station log, flood/freeze/radiation sensors ([FEATURES](docs/FEATURES.md)) |
| Phones, iPads, laptops | browser | browser + installable app (PWA), offline tools, Android app depot |

## Quick start

```bash
./builder/prepper-build plan --tier base --langs eng,spa -v
sudo ./builder/prepper-build flash /dev/disk4 --tier base --ssid Home-7Q4K --pin 246813
./builder/prepper-build watch badger.local
```

The builder streams the OS straight onto the Badger's drive and verifies it on the fly. The Badger then downloads its own content, so **nothing large is ever stored on your laptop**. See [docs/OPERATIONS.md](docs/OPERATIONS.md) for Wi-Fi-only builds, the pass-through relay, and cloning units from each other.

## Repository

| Path | What |
|---|---|
| `content/catalog.toml` | every content item: source, size, tiers, priority, license (single source of truth) |
| `content/renderers.toml` | which app opens which kind of content (the renderer selector) |
| `software/upd/` | updater: change detection per source type, delta transfers, peers, signed bundles, budgets |
| `software/portal/` | launcher API (stdlib Python) + offline web app, tools, map viewers, PWA; feature plugins in `features/` (one `.py` + one `static/features/*.js` each) |
| `software/panel/` | front-panel firmware: OLED + rotary encoder menus |
| `software/powerd/` | UPS fuel gauge, auto power modes, safe shutdown |
| `software/nutime/` | Ke-Shi / Nu:Time™ decimal-time core (`GG:BB:TT`), references, solar model; `/api/time` |
| `software/alertd/` | NOAA Weather Radio SAME alert decoder (RTL-SDR) |
| `software/sdr/` | SDR 2 scheduler: aircraft (readsb) and Meteor-M satellite passes (SatDump), dongle roles, orbit data |
| `software/speech/` | speech-to-text and text-to-speech (whisper.cpp, piper) for the NOAA forecast and Voice |
| `software/sensors/` | sensor station: I²C, 1-Wire, float switch, water level, Geiger, mesh telemetry; alerts |
| `software/meshbridge/` | Meshtastic ↔ check-in board, `!q` library lookups over LoRa |
| `os/` | installer, config renderer, `prepperctl`, systemd units, first boot, pinned versions |
| `builder/prepper-build` | laptop tool: plan, stream-flash, inject, relay, watch, clone |
| `case/` | rugged parametric enclosure (`honey_badger_case.scad`, 204 × 168 × 82.5 mm): PETG base/lid/shock sled and SDR 2 shelf, TPU bumpers, dampers, gasket and SDR whip clips; ready-to-print `stl/` and preview `renders/` |
| `docs/brand/` | logo mark (SVG, 512 px PNG) and wordmark lockup; colors: honey #E8A317, coat #1B1A17, mantle #E9E4D6 |
| `docs/` | [FEATURES](docs/FEATURES.md) (what it does) · [TOUR](docs/TOUR.md) (how it works) · [BOM](docs/BOM.md) (shopping list) · [DESIGN](docs/DESIGN.md) · [HARDWARE](docs/HARDWARE.md) · [OPERATIONS](docs/OPERATIONS.md) · [CONTENTS](docs/CONTENTS.md) · [KE-SHI](docs/KE-SHI.md) |
| `docs/features/` | one user guide per feature: forecast, voice, aircraft, satellites, aid station, skills, missing, vault, supplies, sensors |
| `tests/` | updater end-to-end tests against a fake mirror; launcher and feature tests; dev fixture for the launcher |

## Development

```bash
python3 tests/test_upd.py -v                          # updater end-to-end (no internet needed)
python3 tests/test_nutime.py -v                       # Ke-Shi / Nu:Time acceptance tests
python3 tests/dev_fixture.py /tmp/prepper-dev          # fake installed Badger
PREPPER_CONFIG=/tmp/prepper-dev/prepper.toml python3 software/portal/server.py   # → http://127.0.0.1:8765, PIN 123456
python3 software/panel/panel.py --console              # front-panel menus in a terminal
python3 os/render.py --config os/prepper.toml.example --root /tmp/render   # see generated system configs
python3 tools/gen_contents.py                          # after editing the catalog
```

You need Python 3.11+. The Badger itself runs Raspberry Pi OS Lite 64-bit (Trixie or Bookworm).

## Content licensing

Everything in the catalog is public domain, openly licensed, or free to keep for personal use, and each item records its licence (`license`) and whether it may come preloaded on a Badger that is sold (`use = "any"` or `"personal"`). wikiHow, RepeaterBook and commercial survival titles are not included.

Some items are for **personal, non-commercial use only**: everything CC BY-NC-SA or CC BY-NC-ND (iFixit, Khan Academy, Crash Course, LibreTexts, TED, OpenWrt docs), Hesperian's books, creator-owned YouTube channels, MedlinePlus (it includes licensed A.D.A.M. pages) and a few whose licence hasn't been checked. A Badger you build for your own household gets them like everything else.

If you **sell** Badgers, build them with `prepper-build flash … --for-sale`. Those units never download personal-use items by themselves, whatever the tier or pins say. When the buyer first unlocks Settings, the launcher lists those items with their sizes and licences and offers to download them for the buyer's own personal use; the buyer's acceptance is recorded on the Badger and their Badger downloads them. See [LICENSING.md](LICENSING.md#library-content-and-selling-badgers) and [docs/OPERATIONS.md](docs/OPERATIONS.md#building-badgers-to-sell). This is a practical summary, not legal advice.

## Safety

Honey Badger helps in emergencies, but it is not a substitute for professional help or official warnings:

- Library and AI answers are **not medical, legal or professional advice**. In an emergency, call for help if you can.
- The written forecast is **transcribed automatically** from NOAA Weather Radio and may contain errors. Weather alerts depend on radio reception.
- Sensor readings, including radiation, are **warnings, not calibrated measurements**.
- The software, design and docs come **without any warranty**; see the licences.

## Licence

Software: **AGPL-3.0-only** ([LICENSE](LICENSE)). Case design: **CERN-OHL-S-2.0**. Docs: **CC BY-SA 4.0**.
"Honey Badger™" (short form "Badger"), the badger logo and artwork, and "Nu:Time™" are trademarks of Chad Maglaque; the logos and artwork are also © Chad Maglaque. None of them are covered by these licences.
Details: [LICENSING.md](LICENSING.md) · [TRADEMARKS.md](TRADEMARKS.md) · [CONTRIBUTING.md](CONTRIBUTING.md)
