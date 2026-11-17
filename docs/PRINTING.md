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
| 5 | Corner bumpers | `bumper.stl` | TPU 95A, **black** | **4** | upside down (top cap on the bed), as exported; all four are identical | none |
| 6 | Shock dampers | `damper.stl` | TPU 95A, **black** | **4** | flat, as exported | none |
| 7 | SDR whip clips | `sdr_clip.stl` | TPU 95A, **black** | **2** | on their backs, as exported | none |
| 8 | Lid gasket | `gasket.stl` | TPU 95A, **black** | 1 | flat, as exported | none |
| – | Lid inlay (optional) | export yourself | PETG, yellow or cream | 1 | with the lid, multi-colour | none |

Every part fits a 220 × 220 mm bed. Every recess in the case has a 45° roof or gusset, so **nothing needs supports**.
The files are already in their print orientation: load them as they are.

**Filament for one Badger:** about 465 g yellow PETG (base, sled, shelf), 160 g black PETG (lid),
150 g black TPU 95A (bumpers, dampers, clips, gasket).

## Print settings

| Setting | PETG (base, lid, sled, shelf) | TPU 95A (bumpers, dampers, clips, gasket) |
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
4. Print the TPU parts: 4 bumpers, 4 dampers, 2 clips, 1 gasket.
5. Check for stringing in the screw holes and the gasket groove; clear them before assembly.

## After printing: heat-set inserts

Melt in **22 × M3** and **4 × M2.5** inserts (soldering iron with an insert tip, about 220 °C for PETG):

| Where | Inserts |
|---|---|
| Base: top of the lid bosses | 8 × M3 |
| Base: bottom of the four corners (bumper bottom screws) | 4 × M3 |
| Base floor: power tray bosses | 4 × M3 |
| Base, rear: SDR clip bosses (from outside) | 4 × M3 |
| Base floor: SDR shelf bosses (right of SDR 1) | 2 × M3 |
| Sled: Pi / UPS holes | 4 × M2.5 |

Assembly steps continue in [HARDWARE.md](HARDWARE.md).

## Kit packing checklist

For a Honey Badger enclosure kit (printed parts plus fasteners):

**Printed parts:** base · lid · sled · SDR shelf · 4 bumpers · 4 dampers · 2 SDR clips · 1 gasket

**Fastener bag:**
- 22 × M3 heat-set inserts, 4 × M2.5 heat-set inserts (or pre-installed)
- 8 × M3 × 8 button head (4 lid edge, 4 bumper bottoms)
- 4 × M3 × 10 button head (lid corners, through the bumper caps)
- 8 × M3 washers (lid corners and bumper bottoms)
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

Parts: `base`, `lid`, `sled`, `sdr_shelf`, `bumper`, `damper`, `sdr_clip`, `gasket`, `panel_test`
(and `lid_inlay` with `-D 'markings="inlay"'`). Before printing a changed base, run the fit check and make sure it
prints no `WARNING`:

```bash
openscad --backend=manifold -o /tmp/fit.echo --export-format echo case/honey_badger_case.scad
```

---

The enclosure design is licensed under CERN-OHL-S-2.0; the badger logo and artwork are not
(see [LICENSING.md](../LICENSING.md) and [TRADEMARKS.md](../TRADEMARKS.md)).
