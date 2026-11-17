# Weather satellites

Pictures of the clouds over your region, straight from Russian **Meteor-M** weather satellites
(METEOR-M2 3 and METEOR-M2 4), received by the Badger itself as they fly over — no internet needed.
Useful for seeing a storm system coming when every other source is down.

Open it from the launcher: **Home → Radio & sky → Weather satellites**.
(The American NOAA APT satellites were switched off in 2025; Meteor-M is what is left to receive.)

## What you see

- **Next passes** — when each satellite rises and sets over the Badger, how high it gets, the
  direction it comes from, and whether the Badger will record it (and if not, why).
- **Pictures** — the newest first, with the satellite, the time and the highest elevation.
  Tap one to open it full size. The newest 30 are kept (`[satellites] keep_images`).
- **Orbit data** — how old the satellites' orbit data is. Pass times come from it.

## The antenna matters most

Pictures need the **137 MHz V-dipole** on the **SDR 2** jack, set up **outdoors** with a clear
view of the sky:

1. Take the RTL-SDR Blog dipole kit: extend each arm to **53 cm** and set the arms in a **V, about
   120° apart**.
2. Lay it **flat (horizontal)**, arms pointing roughly north and south, on a table, fence post or
   roof — away from metal and buildings. Use the **3 m coax** to reach the Badger.
3. Screw the coax onto **SDR 2** (in place of the aircraft whip) and, as an admin, tap
   **Satellite V-dipole** under *SDR 2 antenna* on this page.

A QFH antenna works even better. The aircraft whip, and the weather radio's folding 162 MHz whip,
hear 137 MHz poorly: expect noisy or empty pictures with them. The Badger therefore records passes
automatically only when the satellite (or a wideband) antenna is set; with the aircraft whip it
records only if an admin sets `[satellites] capture = true`.

## When it records

- **On SDR 2 (normal):** every pass whose highest point is at least 15° (`min_elevation_dedicated`),
  only the part above 10° (`record_above`), about 10–15 minutes per pass, 3–5 passes a day.
  Weather alerts are not affected. With a wideband antenna, the aircraft list pauses during a pass.
- **Fallback — SDR 2 missing or broken:** nothing is recorded unless an admin opts in with
  `[satellites] fallback_capture = true`. Then only **high passes** (≥ 30°) are recorded on the
  **weather radio**, at most **45 minutes a day**, never while a weather alert or watch is live,
  never within 10 minutes of the radio's last loan — because **weather alerts pause while it
  records** (a NOAA alert is broadcast once, so a missed one is gone). The page and the front panel
  say *"Weather alerts paused for a satellite pass until …"*.

After each pass the picture takes a minute or two to appear (SatDump decodes it on the Badger).

## Orbit data (TLEs)

Pass times are worked out on the Badger from each satellite's orbit data. It is downloaded from
Celestrak when the Badger is installed and refreshed every day whenever the Badger has internet.
Without internet it slowly goes stale:

- under 2 weeks old: times good to a few seconds;
- **over 14 days:** times shown as *approximate*, and recordings start and stop 2 minutes wider;
- **over 60 days:** not used for recording.

To refresh it without internet, an admin can paste fresh orbit data (from a phone that has signal:
celestrak.org → search "METEOR") into **Paste orbit data** on the page, or put a TLE file on the
Badger as `/etc/prepper/tle-initial.txt` and run `tlefile.py import`. If the Badger's clock is
wrong (no GPS and no internet time), the page warns that pass times can't be trusted.

## Settings

See `[satellites]` in `prepper.toml`: the satellite list and frequencies (Meteor-M moves between
137.100 and 137.900 MHz now and then — check a current status page), the elevations, the daily
fallback limit, how many pictures to keep, and `board_post = true` to post *"New weather satellite
image"* on the community board (off by default; never sent over the mesh radio).

## Privacy and limits

- Receive-only; the Badger never transmits. Pictures are public on the Badger's Wi-Fi (they are
  public broadcasts). Orbit-data downloads go only to the address in `[satellites] tle_url`
  (Celestrak) with a generic name, nothing about you.
- Pictures depend on the weather, the antenna, local interference and the satellites' own status;
  some passes give no picture. Night passes show only the infrared channels.
