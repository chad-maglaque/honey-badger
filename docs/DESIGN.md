# Design

## 1. What we're improving on

### Prepper Disk Premium (the baseline)

| | Detail |
|---|---|
| Hardware | Pi 4B 2 GB, 512 GB SD card, aluminium case 3.5 × 2.75 × 1 in, USB-C power, one USB port |
| Network | Own Wi-Fi "HoneyBadger", up to 20 devices, http://10.10.10.10 |
| Content | 16 modules: Wikipedia (EN, 6–7 M articles), Gutenberg (60k), TED (farming/weather), wikiHow (90k+), Stack Exchange (outdoors/sustainability), Ready.gov/FEMA, medical wikis (50k+), survival bookshelf, Ky Furneaux guides, exclusive eBooks, RepeaterBook, Morse decoder, phrase translator (6 languages), street maps + satellite (NA/EU/Oceania), 63 National Park maps, iFixit; plus USB file hosting |
| Software | Internet-in-a-Box + Kiwix, web console, unified search |
| Updates | Free via the web console; yearly paid content packs on SD |
| Power | External adapter or USB-C battery (accessory: "10 h") |

### The field (surveyed Nov 2026)

| Product | Strength worth copying | Weakness |
|---|---|---|
| **Kiwix Hotspot** (official, CHF 280–330) | NVMe, RTC battery, 4–9 W, 24 clients, bundles Android APKs (Trail Sense) | no maps, AI, radio or education |
| **Project N.O.M.A.D.** (free, x86) | AI with retrieval and citations over ZIMs, openFDA drug reference, Kolibri, maps with waypoints, Meshtastic web, one-click apps, auto-updates with rollback | x86 only (15–90 W), no auth, needs a separate router |
| **PrepperPi** (free) | captive portal, 200-country PMTiles downloader, USB auto-import, GPIO power button, signed images | no password on admin, no AI/routing/mesh |
| **Doom Box** ($699) | built-in screen, RTL-SDR, Pelican case | SD storage, small library, slow AI |
| **RACHEL AI** ($750) | built-in battery, 20–50 users, semantic search | expensive, education-only |
| **Reddit Pi 5 field computer** | 256 Wh LiFePO4 for 24 h+, live GPS on the map, GPS-disciplined time, browser SDR | DIY one-off |
| **Trove / Survival LLM** (USB) | first-aid decision tree, 1,000+ radio frequencies, citation-checked answers | needs a working PC |
| **CrankGPT** | hand-crank power, voice interface | toy-scale model |
| **IIAB** | 30+ apps incl. offline email, PBX, Bluetooth PAN | Ansible complexity |

### Gaps closed by the Honey Badger, ranked by value when the grid is down

1. **Integrated power with measured runtime.** Includes a UPS, a 12 V/solar input, automatic Endurance mode and clean shutdown → [HARDWARE](HARDWARE.md#power-consumption-and-runtime), `software/powerd`.
2. **Emergency information intake.** An RTL-SDR decodes NOAA Weather Radio SAME alerts and shows them as a banner on every phone and on the front panel → `software/alertd`.
3. **Communication beyond Wi-Fi.** A check-in board is bridged to a Meshtastic LoRa node, with `!q` library lookups over LoRa → `software/meshbridge`.
4. **Reliability.**
   - NVMe instead of SD.
   - RTC battery, plus GPS time through chrony.
   - Hardware watchdog.
   - Rescue SD card.
   - Atomic, resumable, checksum-verified updates.
5. **Medical depth.** WikiMed, MedlinePlus (EN/ES), NHS medicines, FAS military medicine, the zimgit medicine library and WikiVet. Hesperian and openFDA labels are manual items for now.
6. **Navigation.** Vector maps straight from PMTiles, phone or Badger GPS position, saved rally points, and a canvas fallback for old devices.
7. **App depot.** Kiwix, Organic Maps, OsmAnd, Trail Sense and Meshtastic APKs (from F-Droid) let phones leave with content.
8. **Rebuilding corpus.** Survivor Library (205 GB), US Army field manuals, Appropedia, CD3WD, Energypedia, Low-tech Magazine, WikiCiv, and Gutenberg's Science, Medicine, Agriculture, Technology and Military classes.
9. **Grounded AI.** "Ask the Library" retrieves articles from the Badger and answers only from them, with numbered sources and a "no source" refusal. The default model is Qwen3 4B; Endurance mode uses Qwen2.5 1.5B.
10. **Security the rivals skipped.**
    - PIN-protected admin (PBKDF2).
    - One whitelisted privileged helper (`prepperctl`) behind sudo.
    - Signed peer and bundle manifests (ssh-keygen ed25519).
    - Software self-updates only install from signed commits.
    - WPA2/WPA3 or open network, your choice.
11. **Works with no phone.** The OLED and knob show Wi-Fi and launcher QR codes, status, power modes, services, updates, USB eject and shutdown.
12. **Badger-to-Badger.** Units sync from each other over the LAN or by USB bundle. ZIM diffs can be made on one Badger with `zimdiff` and shipped to others.

**Not copied, on purpose:**

- **wikiHow.** No ZIM is published, and the licence is CC BY-NC-SA, so it's a manual item.
- **RepeaterBook.** Proprietary data. Import your own region as CHIRP CSV instead.
- **Ky Furneaux guides and Prepper Disk exclusives.** Commercial titles.
- **NOAA APT satellite images.** Those satellites were retired in 2025.

## 2. Architecture

```
 phones · iPads · laptops (browser only)            front panel (OLED + knob)
              │ Wi-Fi 6 AP (hostapd) · DHCP/DNS (dnsmasq) · captive-portal probes    │
              ▼                                                                      ▼
 nginx :80/:443 ── /            launcher (static, PWA)                         panel.py
            ├── /api/       ─► portal (Python stdlib) ── prepperctl (sudo, whitelisted verbs)
            ├── /kiwix/     ─► kiwix-serve  (all ZIMs, full-text search, undated aliases)
            ├── /content/   ─► raw files: ZIM, PMTiles (range requests), PDF, EPUB, video, APK
            ├── /maps/      ─► MapLibre + pmtiles.js (vector) · Leaflet + protomaps-leaflet (lite)
            ├── /ai/        ─► llama-server (llama.cpp)       /learn/ ─► Kolibri
            ├── /books/     ─► Kavita (optional)              /video/ ─► Jellyfin (optional)
            ├── /sync/      ─► this Badger's signed manifest for peer Badgers
            └── /files/     ─► USB sticks (read-only share)
 background: upd (timers) · powerd (UPS) · alertd (SDR) · meshbridge (LoRa) · gpsd+chrony
```

**Rules:**

- The Python services use **only the standard library**, so nothing breaks when PyPI isn't reachable. The panel and mesh bridge use a venv built at install time.
- **Every web asset is vendored** under `/opt/prepper/vendor`, so nothing loads from a CDN.
- **One config file**, `/etc/prepper/prepper.toml`. `os/render.py` turns it into the hostapd, dnsmasq, nginx, NetworkManager and chrony configs.
- **One privileged entry point**, `prepperctl`. The portal runs as an unprivileged user.

### Launcher and renderer selector

`content/renderers.toml` maps each content kind to the apps that can open it.

| Kind | Renderers |
|---|---|
| zim | Kiwix server · Kiwix (no frame) · Kiwix JS in the browser · Download |
| pmtiles | MapLibre · Leaflet lite · phone-app packs |
| pdf | PDF.js · native browser viewer |
| epub | foliate-js · Kavita · download |
| video | built-in player · Jellyfin |
| model | Ask the Library · open chat |

Which renderer opens an item, highest priority first:

1. The viewer's own choice, stored in their browser.
2. The admin's device default.
3. The catalog default.

A renderer whose service is down (`requires = "kiwix"` …) is hidden rather than left as a broken link. Links to Kiwix use kiwix-serve's `--nodatealiases` names, so bookmarks survive content updates.

### Firmware: navigation without a phone

`panel.py` runs the front panel. Turn the knob to move, press to select, and hold for 3 s on the home screen to shut down.

The home screen cycles through:

- SSID and number of connected clients
- battery level
- launcher address
- build progress
- weather alerts

The menus are listed in the module docstring. `--console` runs the same menu tree in a terminal for testing.

## 3. Updates and how differences are found

`upd` uses a single catalog (`content/catalog.toml`, 138 items) and a state ledger. Each source type has its own way to detect change:

| Source | Change detection | Transfer | True delta? |
|---|---|---|---|
| `kiwix` | OPDS catalog (name + flavour → newest dated build, article counts), fallback to the mirror listing; sha256 from `.meta4` | rsync `--fuzzy` against the installed ZIM, else resumable HTTP | partial: unchanged compressed clusters (mostly images) match |
| `git` | `git ls-remote` commit id | shallow fetch + reset | yes |
| `rsync` | hash of the remote tree listing | `rsync --link-dest` (unchanged files hard-linked) | yes, per file |
| `http` | ETag / Last-Modified / Content-Length, or the newest version in a listing (natural sort) | resumable HTTP | no |
| `hf` | `X-Linked-Etag` (= sha256 of the GGUF) | resumable HTTP | no |
| `pmtiles` | newest Protomaps daily build | `pmtiles extract` by bbox and zoom (fetches only the needed bytes) | no |
| peer / bundle | signed `manifest.json` from another Badger or a USB stick | LAN copy / local copy, or **zimpatch** if the offer carries an OLD→NEW `.zimdiff` | zimdiff: yes |

**Kiwix publishes no diffs.** zim-tools 3.8 still ships `zimdiff` and `zimpatch`. So a hub Badger with internet keeps one previous version (`keep_previous = true`) and runs `upd export --diffs <old-version>`. That writes small patch files that offline Badgers apply with zimpatch. `zimdiff` speed and RAM use on a 120 GB ZIM have not been tested.

**Safety:**

- **Staging.** Downloads go to `.staging/` on the same filesystem.
- **Verification.** Each download is checked against the publisher's sha256 or the peer manifest's.
- **Atomic swap.** A rename and a `<id>.current` symlink swap make the new version live.
- **Hooks.** After the swap a hook runs, e.g. rebuilding the Kiwix library atomically.
- **Old versions.** The previous version is removed only after the new one is live.
- **Space check.** Each item needs its size plus `reserve_gb` free before it starts.
- **Ordering.** Smallest items first, so essentials land first if power or the link drops.
- **Resumable.** Partial downloads resume.
- **Locking.** A lock file stops two runs overlapping.
- **Report.** Every check writes a report the launcher shows, with per-item versions, transfer method, bytes, and "+N articles" notes.

**Schedule.**

- A daily metadata check (cheap).
- A weekly download, optionally limited to a time window and a bandwidth cap.
- Both run automatically whenever an uplink exists.
- Peers are always tried before the internet.

## 4. The builder: no copies on the laptop

`builder/prepper-build flash` handles the OS image:

1. It downloads Raspberry Pi OS and xz-decompresses it **while downloading**.
2. It writes the result straight to the target drive, hashing the compressed stream as it passes and checking it against the published sha256.
3. It mounts the boot partition and adds about 100 KB: the software tarball, `prepper.toml`, the first-boot unit, and either cloud-init `user-data`/`network-config` (Trixie) or `firstrun.sh` (Bookworm).

The laptop never holds the image or any content. At first boot the Badger installs itself, brings up its Wi-Fi, and downloads its own content straight to its NVMe.

The Badger needs internet for that first download. Ways to give it access, in order of preference:

| | How the Badger gets internet | Laptop stores |
|---|---|---|
| A | Ethernet to your router | nothing |
| B | macOS Internet Sharing over a cable | nothing |
| C | `prepper-build relay`: a pass-through proxy on a direct cable (Badger 10.10.11.2 ↔ laptop 10.10.11.1) | nothing (streams through memory) |
| D | Peer sync from an already-built Badger (`--peer`, `clone`) | nothing; no internet at all |

## 5. Editions

| Edition | What it is |
|---|---|
| **Badger** (Field / Base / Ultimate) | the server — content follows `[upd].tier` |
| **iPad / iPhone** | thin client. The launcher installs to the Home Screen. With the Badger's local CA installed and HTTPS on, a service worker caches the launcher and tools, so Morse, phrasebook, water, radio, converter and sun keep working out of range. Bulk content stays on the Badger. To take content away, download ZIMs into the free Kiwix app and a map region into Organic Maps |
| **Laptop** | thin client in any browser; Chrome/Edge can install it as an app |
| Laptop go-bag (optional) | the same software in `standalone` mode with `tier = "pocket"` and `budget_gb = N`. `upd` fills the budget by priority, pins first, and syncs **from the Badger** as a peer |

## 6. Ke-Shi / Nu:Time

The Badger keeps GPS-disciplined UTC with RTC holdover, so it acts as the family's reference for **Ke-Shi / Nu:Time**. That is a decimal day of 10 gongs × 100 beats × 100 ticks, displayed as `GG:BB:TT` (noon = `05:00:00`).

It is built in at every level:

- an integer-exact conversion core in Python with a JavaScript mirror
- utc / civil / mean-solar / true-solar references
- zh/en/fr/es/de labels
- `GET /api/time` returning `nu-time/v1` records
- an offline clock tool with a dial and a 1,000-beat sky strip
- a front-panel page

17 acceptance tests cover the handoff's criteria. [KE-SHI.md](KE-SHI.md) has the details, the policy defaults, and the desk-clock accessory proposal.

## 7. Known limits and open checks

- **Unconfirmed upstream versions** are marked `VERIFY` in `os/versions.env`: the go-pmtiles 1.31.2 asset name, the llama.cpp tag, @protomaps/basemaps and protomaps-leaflet. The installer stops loudly on a 404.
- **`llama-server --api-prefix`** needs a 2025+ build. On an older build, point `/ai/` at port 8090.
- **Pi 5 onboard AP limits** aren't published; field reports say 5–12 clients. Use the USB adapter for crowds, or wire a dedicated access point to the Ethernet port.
- **Wi-Fi uplink during the build.** With no USB adapter and a Wi-Fi uplink, the access point is deferred until the first content pull finishes, so the Badger doesn't cut its own connection.
- **Hesperian books, openFDA labels, NPS and USGS maps, satellite imagery and local repeaters** are manual items with instructions. They are candidates for proper source types later.
- **Routing and place search** (Valhalla, Photon) are not built yet. Routing data has to be built on a PC, not on the Pi.
- **The case has been rendered and clash-checked but not printed or drop-tested.** Print `panel_test` first, then follow the drop-test procedure in [HARDWARE](HARDWARE.md#drops-and-transport).
