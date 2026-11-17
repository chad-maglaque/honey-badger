# Licensing

Honey Badger is open: you may build, study, change and share it. Different parts carry
different licences, chosen so that improvements stay open while the name stays ours.

| What | Where | Licence |
|---|---|---|
| **Software**: services, launcher, front panel, installer, builder, tools, tests | `software/`, `os/`, `builder/`, `tools/`, `tests/`, `content/*.toml` | **AGPL-3.0-only**: [LICENSE](LICENSE) |
| **Hardware**: enclosure design (OpenSCAD source, STL exports, print and fit notes) | `case/` except the brand files below | **CERN-OHL-S-2.0**: [LICENSES/CERN-OHL-S-2.0.txt](LICENSES/CERN-OHL-S-2.0.txt) |
| **Documentation** | `docs/`, `README.md` and the other `*.md` files | **CC BY-SA 4.0**: [LICENSES/CC-BY-SA-4.0.txt](LICENSES/CC-BY-SA-4.0.txt) |
| **Brand**: the names "Honey Badger™" and its short form "Badger", the badger logo, artwork and icons, product renders, and the name "Nu:Time™" (trademarks; logos, artwork and renders also © Chad Maglaque) | `case/badger_*.svg`, `case/renders/`, `software/portal/static/icon*`, `software/portal/static/apple-touch-icon.png` | **All rights reserved.** See [TRADEMARKS.md](TRADEMARKS.md) |
| **Library content** (encyclopedias, books, courses, maps, models) | not in this repository; downloaded by the updater | each item's own licence, recorded in `content/catalog.toml` |

Copyright © 2026 Chad Maglaque and the Honey Badger contributors.

## What this means in practice

- **Build one for yourself, your family or your neighbourhood:** go ahead.
- **Change the software and hand Badgers to others, sell them, or let people use a changed
  Badger over its Wi-Fi or any network:** you must offer those people the complete source of
  your changed version under AGPL-3.0 (that is the "network" clause, §13). The launcher's
  Settings page has a **Source code** link; point it at your source if you change the code.
- **Change the case design and make or sell cases:** share your changed design files under
  CERN-OHL-S-2.0 and keep the licence notices.
- **Use the docs:** credit Honey Badger and share your changes under CC BY-SA 4.0.
- **The names and logos are not licensed** by any of the above. A changed or rebuilt version must
  use a name other than "Honey Badger" or "Badger", and no badger logo or artwork. Saying it is "compatible with Honey Badger" or
  "based on Honey Badger" is fine. See [TRADEMARKS.md](TRADEMARKS.md).

## Commercial licences

If AGPL-3.0 doesn't fit your product, a separate commercial licence for the software may be
available: contact the maintainer through the repository.

## Third-party programs

The installer fetches other projects that keep their own licences: for example kiwix-tools,
llama.cpp, whisper.cpp, piper, readsb, SatDump, multimon-ng, rtl-sdr, MapLibre, pdf.js and the
Meshtastic Python library. They run as separate programs or are used under licences
compatible with AGPL-3.0. Pinned versions are in `os/versions.env` and `os/install.d/`.

## Library content and selling Badgers

Each catalog item in `content/catalog.toml` records its licence and a `use` field:

- `use = "any"`: public domain, CC BY, CC BY-SA, GPL, ODbL and similar. May come preloaded on a
  Badger that is sold, as long as you keep the attribution the licence asks for.
- `use = "personal"`: personal / non-commercial use only. That is every CC BY-NC-* licence
  (iFixit, Khan Academy, Crash Course, LibreTexts, TED, the OpenWrt docs), Hesperian's terms,
  YouTube channels whose creators keep the copyright, MedlinePlus (it includes A.D.A.M.
  pages licensed to the NLM), and anything whose licence hasn't been checked yet. The
  updater treats a missing or unknown value, and any licence naming NC, as `"personal"`.

The rule for selling:

1. **Personal-use items must not be on a Badger when it is sold.** Build units for sale with
   `prepper-build flash … --for-sale` (it sets `[device] provisioning = "sale"`). On such a unit
   the updater never fetches personal-use items: not from the internet, another Badger, a USB
   bundle, `upd adopt`, a pin or a tier.
2. **The owner may download them, on their own Badger, for personal use.** When the buyer first
   unlocks Settings, the launcher lists those items with their sizes and licences and asks
   whether to download them. Accepting records who accepted, when, and which items and
   licences, and the buyer's Badger then downloads them. The buyer can also say "Not now"
   (the offer stays in Settings and on the Status page) or remove them later.
3. **Don't accept on the buyer's behalf.** That choice is theirs. `prepper-build watch` tells
   you whether a unit built for sale is clean to sell.

If you sell a Badger you first used yourself, re-flash it with `--for-sale` first.

This is a practical summary to help you follow the content licences, not legal advice. Check
each item's own licence, and correct the catalog if you find a licence is stricter or looser
than it says.
