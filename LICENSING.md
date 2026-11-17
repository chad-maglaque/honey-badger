# Licensing

Honey Badger is open: you may build, study, change and share it. Different parts carry
different licences, chosen so that improvements stay open while the name stays ours.

| What | Where | Licence |
|---|---|---|
| **Software**: services, launcher, front panel, installer, builder, tools, tests | `software/`, `os/`, `builder/`, `tools/`, `tests/`, `content/*.toml` | **AGPL-3.0-only**: [LICENSE](LICENSE) |
| **Hardware**: enclosure design (OpenSCAD source, STL exports, print and fit notes) | `case/` except the brand files below | **CERN-OHL-S-2.0**: [LICENSES/CERN-OHL-S-2.0.txt](LICENSES/CERN-OHL-S-2.0.txt) |
| **Documentation** | `docs/`, `README.md` and the other `*.md` files | **CC BY-SA 4.0**: [LICENSES/CC-BY-SA-4.0.txt](LICENSES/CC-BY-SA-4.0.txt) |
| **Brand**: the name "Honey Badger", the badger logo and artwork, product renders, and the name "Nu:Time™" | `case/badger_*.svg`, `case/renders/`, `software/portal/static/icon*`, `software/portal/static/apple-touch-icon.png` | **All rights reserved.** See [TRADEMARKS.md](TRADEMARKS.md) |
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
- **The name and logo are not licensed** by any of the above. A changed or rebuilt version must
  use a different name and no badger logo. Saying it is "compatible with Honey Badger" or
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

Some catalog items are licensed for non-commercial use only (CC BY-NC-SA: iFixit, Khan
Academy, LibreTexts; CC BY-NC-ND: TED). They must not come preloaded on Badgers that are sold.
Let the owner's Badger download them for personal use, or leave them out.
