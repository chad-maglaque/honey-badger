# Aircraft

See the planes around you — flight number, height, speed, and how far away and in which direction —
on a list and on the offline map. Planes broadcast this openly on 1090 MHz (ADS-B); the Badger's
second radio, **SDR 2**, picks it up. No internet is needed.

Open it from the launcher: **Home → Radio & sky → Aircraft**, or turn on **✈️ Aircraft** on the map.

## The two radios

Every Badger has two identical radio receivers (RTL-SDR Blog V4) inside:

| Radio | Jack on the back | Antenna | Job |
|---|---|---|---|
| SDR 1 | **SDR** (whip channel) | folding 162 MHz whip | NOAA Weather Radio alerts — always listening |
| SDR 2 | **SDR 2** | 1090 MHz whip *or* 137 MHz V-dipole | aircraft and weather satellites |

Only one antenna fits on SDR 2 at a time:

- **Aircraft whip (1090 MHz)** — as shipped. Aircraft are watched all day; the list is always fresh.
- **Satellite V-dipole (137 MHz)** — set up outdoors for weather-satellite pictures
  (see [Weather satellites](satellites.md)). Aircraft are then **on demand**: tap
  **Scan aircraft for 15 min**. The dipole hears fewer planes than the whip.
- **Wideband antenna (both)** — a discone or similar: aircraft all day, pausing during satellite passes.

When you change the antenna, tell the Badger: an admin taps the matching button under
**SDR 2 antenna** on the Aircraft or Weather satellites page (or sets `[radio] sdr2_antenna`).

## Using it

- The list is sorted by distance and refreshes every 5 seconds. Heights are in feet, speeds in mph,
  distances in miles (km in brackets) from the Badger's GPS position (or the home location in
  settings when there is no GPS fix).
- A red tag means the plane is squawking an emergency code (7500/7600/7700).
- **Show on the map** opens the offline map with the aircraft layer: each plane is an arrow
  pointing the way it flies; tap it for details.
- Range: typically 50–150 miles with the whip outdoors or at a window, much less indoors.
  Small planes, gliders, drones and many military flights don't broadcast ADS-B.

## If SDR 2 is missing or broken (fallback)

The Badger keeps working with one radio. The pages say *"SDR 2 isn't working"* (or *isn't plugged
in*), and:

- **Scan aircraft** borrows the weather radio (SDR 1) for up to 15 minutes. **Weather alerts are
  paused** meanwhile — the launcher, the front panel and the forecast feature all show it — and the
  radio goes back to listening right after.
- To keep alerts safe, a scan is **refused while any weather alert or watch is live**, at most
  4 scans a day (`[aircraft] scans_per_day`), never within 10 minutes of the last loan
  (`[radio] sdr_lend_gap_min`), and alertd caps all loans at 120 minutes a day.
- Only family members (and admins) can start a scan, because it pauses alerts. Anyone on the Wi-Fi
  can see the list.
- If SDR 2's program keeps crashing (3 times in 15 minutes), the Badger treats SDR 2 as failed for
  30 minutes, uses the fallback, then tries SDR 2 again by itself.

To forbid lending the weather radio at all: `[radio] sdr_lend = false`.

## Setting the radios' serial numbers (at build time)

The two dongles are identical, so the Badger tells them apart by the serial number stored in each
one. Set them once, with **only that dongle plugged in**, then unplug and replug it:

```
rtl_eeprom -d 0 -s BADGER-SDR1     # the weather-radio dongle (goes on the "SDR" jack)
rtl_eeprom -d 0 -s BADGER-SDR2     # the aircraft/satellite dongle (goes on the "SDR 2" jack)
rtl_test -t                        # with both plugged in: lists "SN: BADGER-SDR1" and "SN: BADGER-SDR2"
```

(Stop the radio services first: `sudo systemctl stop prepper-alertd prepper-sdr`.) Avoid all-digit
serials like the factory `00000001`: the radio tools read a number as a device position instead.

If the serials are not set, the Badger guesses: the first dongle on the internal USB hub is the
weather radio and the other is SDR 2, and the pages show a warning until the serials are set.
If only one dongle is found, it is always used for weather alerts. You can also name them
explicitly with `[radio] weather_sdr = "…"` and `sdr2 = "…"` (a serial pinned this way is strict:
alertd waits for that dongle rather than take another).

## Hardware

Included in every Badger: the second RTL-SDR Blog V4 (SDR 2) on its shelf above SDR 1, the
"SDR 2" SMA bulkhead jack (capped when unused), the RTL-SDR Blog dipole kit (used as the
satellite V-dipole) and a small 1090 MHz whip for the SDR 2 jack.

## Privacy and limits

- ADS-B is public broadcast data; the Badger only listens and never transmits. The aircraft list is
  readable by anyone on the Badger's Wi-Fi (rate-limited). The Badger's own position is shown rounded.
- Nothing is stored long-term: the live list lives in memory/`/run` only.
- Position decoding and distances are approximate; don't use this for navigation or air-traffic safety.
