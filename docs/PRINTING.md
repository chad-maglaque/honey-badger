# Printing a Honey Badger

Everything you need to print one Badger enclosure, on one page. The design source is
[`case/honey_badger_case.scad`](../case/honey_badger_case.scad); ready-to-print files are in
[`case/stl/`](../case/stl). Background on the design and materials: [HARDWARE.md](HARDWARE.md#printing-the-case).
Parts to buy: [BOM.md](BOM.md).

## Print list

| # | Part | File | Material | Qty | Orientation on the bed | Supports |
|:-:|---|---|---|:-:|---|---|
| 0 | Panel test (optional, print first) | `panel_test.stl` | PETG or PLA, any colour | 1 | flat, as exported | none |
| 1 | Base | `base.stl` | PETG, **yellow** | 1 | open side up (floor on the bed), as exported | none |
| 2 | Lid | `lid.stl` | PETG, **black** | 1 | face down, as exported | none |
| 3 | Sled (holds the Pi and UPS) | `sled.stl` | PETG, **yellow** | 1 | flat, as exported | none |
| 4 | SDR shelf (carries SDR 2) | `sdr_shelf.stl` | PETG, **yellow** | 1 | deck down, as exported | none |
| 5 | Front and rear frames | `frame.stl` | TPU 95A, **black** | **2** | face down (the wraps point up), as exported; the front and rear frames are the same part | none |
| 6 | Edge bumpers | `edge_bumper.stl` | TPU 95A, **black** | **4** | lying on the side leg's outer face, tongue up, as exported; all four are identical | none |
| 7 | Shock dampers | `damper.stl` | TPU 95A, **black** | **4** | flat, as exported | none |
| 8 | SDR whip clips | `sdr_clip.stl` | TPU 95A, **black** | **2** | on their backs, as exported | none |
| 9 | Lid gasket | `gasket.stl` | TPU 95A, **black** | 1 | flat, as exported | none |
| – | Lid inlay (optional) | export yourself | PETG, yellow or cream | 1 | with the lid, multi-colour | none |

Every part fits a 220 × 220 mm bed; the largest is the frame, 216 × 95 mm (22 mm tall), so centre it and skip the brim.
Every recess in the case has a 45° roof or gusset, and the bumpers have 45° bevels where they meet the bed, so
**nothing needs supports**. The files are already in their print orientation: load them as they are.

**Filament for one Badger:** about 465 g yellow PETG (base, sled, shelf), 160 g black PETG (lid),
240 g black TPU 95A (2 frames ≈ 70 g each, 4 edge bumpers ≈ 20 g each, plus dampers, clips and gasket; at the settings below).

## Print settings

| Setting | PETG (base, lid, sled, shelf) | TPU 95A (frames, edge bumpers, dampers, clips, gasket) |
|---|---|---|
| Layer height | 0.2 mm | 0.2 mm |
| Walls | **6** (≈ 2.4 mm, so the 4 mm walls are nearly solid) | 3 |
| Top / bottom layers | 6 / 6 | 4 / 4 |
| Infill | **40 % gyroid** | **20 % gyroid** (the soft infill is the cushion) |
| Nozzle / bed | 240–250 °C / 80 °C | 220–230 °C / 50 °C |
| Speed | normal | slow, ≤ 25 mm/s |
| Cooling | 30–50 % (hotter layers bond better) | 50 % |

Use PETG, not PLA: PLA softens in a parked car and creeps under screw pressure. ASA or PC are fine if your
printer is enclosed (see [HARDWARE.md › Material](HARDWARE.md#material-petg-not-pla-tough)).

## Order and checks

1. **Print the panel test first.** Panel-mount connectors vary by vendor. Fit your USB-A, USB-C, Ethernet,
   GX16 power, M8 sensor and SMA parts, the knob, button and LED. If anything is tight or loose, adjust the
   matching `*_cut`, `*_relief` or `*_at` parameter in the SCAD and re-export (commands below).
2. Print the base, then the lid. Check the lid seats on the base before printing the bumpers.
3. Print the sled and the SDR shelf.
4. Print the TPU parts: 2 frames, 4 edge bumpers, 4 dampers, 2 clips, 1 gasket.
5. Check for stringing in the screw holes, the gasket groove and the four edge-bumper grooves along the side walls;
   clear them before assembly. Slide each edge bumper along its groove once before fitting the electronics: it should
   run end to end with a firm push (a drop of soapy water helps the first time).
6. Lightly deburr the two SDR whip clips: their ring crowns are cut flat so they sit below the bumpers, which leaves small
   sharp edges on the arm tops. A few strokes with a file or fine sandpaper.

## After printing: heat-set inserts

Melt in **24 × M3** and **4 × M2.5** inserts (soldering iron with an insert tip, about 220 °C for PETG):

| Where | Inserts |
|---|---|
| Base: top of the lid bosses | 8 × M3 |
| Base, underneath: the four corners and the front and rear middle bosses (frame bottom screws) | 6 × M3 |
| Base floor: power tray bosses | 4 × M3 |
| Base, rear: SDR clip bosses (from outside) | 4 × M3 |
| Base floor: SDR shelf bosses (right of SDR 1) | 2 × M3 |
| Sled: Pi / UPS holes | 4 × M2.5 |

Assembly steps continue in [HARDWARE.md](HARDWARE.md); the bumper fitting order and how to open the case are in
[HARDWARE.md › Assembly notes](HARDWARE.md#assembly-notes).

## Kit packing checklist

For a Honey Badger enclosure kit (printed parts plus fasteners):

**Printed parts:** base · lid · sled · SDR shelf · 2 frames · 4 edge bumpers · 4 dampers · 2 SDR clips · 1 gasket

**Fastener bag:**
- 24 × M3 heat-set inserts, 4 × M2.5 heat-set inserts (or pre-installed)
- 6 × M3 × 12 button head + 6 washers (frame tops: they are also the 4 corner and the front/rear middle lid screws)
- 6 × M3 × 8 button head + 6 washers (frame bottoms, into the base)
- 2 × M3 × 6 button head (the two side lid screws, hidden under the top edge bumpers)
- 8 × M3 × 6 (4 SDR clips, 4 power tray)
- 6 × M3 × 10 + 6 nuts (USB-A and Ethernet panel ears)
- 4 × M3 × 20 + 4 nuts (fan, unless it comes with its own)
- 2 × M3 × 12 (SDR shelf)
- 2 × M2.5 × 10 + 2 nuts (USB-C panel ears)
- 4 × M2.5 (UPS board to the sled; length to suit your standoffs)
- 4 × M2 + 4 nylon spacers + 4 nuts (OLED)
- 20 × zip ties, ≤ 2.2 mm wide (the anchor slots are 2.4 mm)
- 1 × 20 mm hook-and-loop strap (Wi-Fi adapter)
- 3 mm closed-cell foam, cut to the stack (see HARDWARE.md)

The electronics, connectors and antennas are listed in [BOM.md](BOM.md).

## Re-exporting the files

After changing a parameter, re-export the part you changed (add `--backend=manifold` for much faster renders):

```bash
openscad --backend=manifold -D 'part="base"' -o case/stl/base.stl case/honey_badger_case.scad
```

Parts: `base`, `lid`, `sled`, `sdr_shelf`, `frame`, `edge_bumper`, `damper`, `sdr_clip`, `gasket`, `panel_test`
(and `lid_inlay` with `-D 'markings="inlay"'`). Before printing a changed base, run the fit check and make sure it
prints no `WARNING`:

```bash
openscad --backend=manifold -o /tmp/fit.echo --export-format echo case/honey_badger_case.scad
```

---

The enclosure design is licensed under CERN-OHL-S-2.0; the badger logo and artwork are not
(see [LICENSING.md](../LICENSING.md) and [TRADEMARKS.md](../TRADEMARKS.md)).
