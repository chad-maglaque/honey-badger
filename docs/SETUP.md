# Setting up a Honey Badger

For the person who builds the Badger, or looks after it for the family. It walks through building, installing,
the first boot and **every security setting, with its trade-offs in plain words**. Day-to-day use for everyone
is in [USAGE](USAGE.md); the one-page version is [QUICKSTART](QUICKSTART.md). This guide is for Honey Badger
v0.4.

Where a reference already exists, this guide links to it instead of repeating it: [OPERATIONS](OPERATIONS.md)
(the operating manual), [SECURITY](SECURITY.md) (what protects what, and the limits), [MESH](MESH.md) (the
radio), [HARDWARE](HARDWARE.md) and the per-feature guides in [features/](FEATURES.md).

**The config file.** Everything on the Badger is set in one file, `/etc/prepper/prepper.toml`, documented line by
line in [`os/prepper.toml.example`](../os/prepper.toml.example). The builder writes it for you. To change it
later, edit it at the Badger's console (SSH as `prepper-admin` over Ethernet, or a keyboard and screen) and then
run `sudo prepperctl apply-config`. Many settings also have a button in the launcher; what you set there wins
over the file. Every section of the file is listed in [the config reference](#14-config-file-reference) at the
end.

## 1. Build the hardware

1. **Buy the parts:** [BOM](BOM.md) (with a four-order shopping plan and [bom.csv](bom.csv)). The Badger itself
   is roughly $900 plus the SSD; the [sensor kit](BOM.md#sensor-kit-optional) is optional.
2. **Print the case:** [PRINTING](PRINTING.md): PETG base, lid, sled and SDR shelf; TPU frames, bumpers, dampers,
   clips and gasket. No supports needed.
3. **Assemble and wire it:** [HARDWARE](HARDWARE.md): wiring and the GPIO map, the solar/DC input chain, the
   second SDR, the sensor socket, connector fit and the case fan.
4. **Before you close the lid:**
   - Set the **PD trigger to 15 V** before connecting it (20 V is above the UPS board's limit;
     [BOM → Before you order](BOM.md#before-you-order)).
   - Set the two SDR **serial numbers** (see [SDR 1 and SDR 2](#8-the-two-radio-receivers-sdr-1-and-sdr-2)).
   - Secure the LoRa antenna's tiny U.FL plug with a dab of hot glue, so the radio never runs without an antenna.
   - Fit the RTC cell if you have the rechargeable ML-2020, and set `rtc_battery = "ml2020"` in `[power]` (only
     with that cell: it turns on trickle charging).

## 2. Install the software

### With the builder (recommended)

`builder/prepper-build` runs on a Mac or Linux laptop (Python 3.11+). It streams Raspberry Pi OS straight onto
the Badger's NVMe (in a USB enclosure) or an SD card, verifies it on the fly, and adds this unit's settings.
Nothing large is ever stored on the laptop: the Badger downloads its own content afterwards.

```bash
./builder/prepper-build plan --tier base --langs eng,spa --drive-gb 1000 -v      # what fits, how long it takes
sudo ./builder/prepper-build flash /dev/disk4 --tier base --langs eng,spa --pin 246813
./builder/prepper-build watch badger.local                                         # follow the install
```

- **Tier** (`--tier`): how much of the library it holds — `field` (512 GB drive), `base` (1 TB) or `ultimate`
  (2 TB or more). Sizes per language: [CONTENTS](CONTENTS.md). You can change it later in `[upd]`.
- **Wi-Fi name** (`--ssid`): leave it out for a neutral random one like `Home-K7QX`. Names that say "Badger",
  "Honey Badger" or "prepper" are refused: a passer-by's phone would list your house as a prepared one.
- **Wi-Fi password:** random unless you give `--passphrase`; printed at the end and always shown on the front
  panel. For other choices see [Wi-Fi](#5-wi-fi).
- **Admin PIN** (`--pin`): 6–32 letters and digits, for the launcher's Settings page. Change it to a passphrase
  at first boot ([Admin passphrase](#admin-passphrase)).
- **No Ethernet where you build?** `--uplink 'HomeWiFi:password'`, Internet Sharing, or the pass-through relay;
  **several units?** build one, then point the others at it with `--peer`. Both: [OPERATIONS → Building a
  Badger](OPERATIONS.md#building-a-badger).

Install the drive, plug the Badger into your router by Ethernet and power it on. It installs its software
(about 20 minutes, most of it compiling the AI server), reboots, and starts downloading content, smallest and
most essential first (medical, survival, water, tools and a maps overview within the first hour; full Wikipedia
last). A reboot in the middle resumes it. The launcher's *Status* page shows progress.

### Building Badgers to sell: `--for-sale`

Some library items (iFixit, Khan Academy, Crash Course, LibreTexts, TED, Hesperian, MedlinePlus and a few more)
are licensed for **personal, non-commercial use only**. A Badger you build for your own household gets them like
everything else. A Badger you **sell** must not carry them: add `--for-sale` to `plan` and `flash`. That unit
never downloads them by itself (not from the internet, another Badger or a USB stick); when the buyer first
unlocks Settings, the launcher lists them with sizes and licences and offers to download them for the buyer's own
personal use. **Don't accept that offer yourself** — it is the buyer's decision. Details: [OPERATIONS → Building
Badgers to sell](OPERATIONS.md#building-badgers-to-sell) and [Handing a sold Badger to its
owner](OPERATIONS.md#handing-a-sold-badger-to-its-owner). (Not legal advice.)

### By hand, on a Pi that already runs Raspberry Pi OS

On Raspberry Pi OS Lite 64-bit (Trixie or Bookworm), copy the repository to the Pi and run
`sudo bash os/install.sh`. It is safe to run again; `--upgrade` is what software updates use. Without the
builder, the installer gives the hotspot a random Wi-Fi password (read it on the front panel), and there is no
admin PIN until you set one with `sudo prepperctl set-pin`.

**Versions.** Every outside program the installer downloads is pinned in
[`os/versions.env`](../os/versions.env) (never "latest"), so every Badger built from one checkout is identical.
Optional parts have their own installers in `os/install.d/` (speech, SDR programs, sensors, the solver's
sandbox, the wipe). The speech installer only downloads files whose checksums are filled in; see
[OPERATIONS → Troubleshooting](OPERATIONS.md#troubleshooting).

## 3. First boot

When the status light is solid and the content pull has started:

1. Press the knob → **Join Wi-Fi (QR)** and join with your phone. Open **http://badger.lan**.
2. Install the Badger's certificate on your own phone and switch to **https://badger.lan** for admin work
   ([Using https](#using-https)).
3. Work through the **first-boot security checklist** below — all of [Security settings](#4-security-settings),
   in that order. It is also in [QUICKSTART](QUICKSTART.md#3-first-boot-security-checklist) and
   [OPERATIONS](OPERATIONS.md#first-boot-security-checklist).
4. Set your weather-radio transmitter and county ([SDR 1](#sdr-1-weather-radio)), and the radio channels
   ([The mesh radio](#7-the-mesh-radio)).
5. Invite the family ([Family members](#6-family-members)).

The first-boot log is in `/var/log/prepper-firstboot.log`; other logs: [OPERATIONS →
Maintenance](OPERATIONS.md#maintenance).

## 4. Security settings

The Badger is built so that **nothing locks the family out** (every safeguard has a way back) and so that a
Badger that is **seized, stolen or forced open can't be turned against you**. Each setting below says what it
protects, what it costs and how to get back in. All of them are on **Settings → Badger administration** unless
noted. The full story, including the honest limits: [SECURITY](SECURITY.md).

### Using https

Most families use `http://badger.lan`. Anyone who knows the Wi-Fi password can read plain http traffic on the
hotspot, so the Badger **never sends new secrets over plain http**: sealed-records passphrases, the panic wipe,
duress and handover controls only work over `https://` (or at the Badger itself), and new Wi-Fi passwords and
radio keys are shown on the front panel instead. For admin work, install the certificate on your phone
(`http://badger.lan/ca.crt`; on an iPhone also turn it on under Settings → General → About → Certificate Trust
Settings) and use **https://badger.lan**. `[network] https = true` (the default) turns this on; `sudo prepperctl
gen-tls` makes a new certificate. Details: [SECURITY → Secrets and plain HTTP](SECURITY.md#secrets-and-plain-http).

### Admin passphrase

**What it does:** opens the admin page (settings, invites, removing members, Wi-Fi and radio key changes). It is
**not** a master key: it can't open the family-only pages, read the family radio key or the vault, or add a
device to someone else's name without their yes ([SECURITY → The admin PIN is not a master
key](SECURITY.md#the-admin-pin-is-not-a-master-key)).

**Set it** at the Badger: `sudo prepperctl set-pin`, then type it twice. It is read from the prompt only, never
from the command line (others could see it in `ps`).

**Trade-off:** a PIN of 6+ digits still works, but the launcher warns you: someone who copies the disk can guess
digits quickly. **Use a passphrase of 10+ characters — a few words.** After changing it, tap *Log out all admin
sessions*.

**Way back:** anyone with the console (SSH or keyboard and screen) can set a new one.

### Sealed records and the recovery card

**What it does:** the family's sensitive records — family board history, supplies and where they're kept,
member names, the aid-station log's names, contact details — are encrypted on the drive. After every restart the
Badger keeps working (alerts, library, maps, radio, new entries are saved sealed), but those records stay
unreadable until the owner unlocks them. A Badger that is taken shows nothing of them.

**Set it up** over https: *Sealed records* → owner passphrase (16+ characters or four or more words, **not** the
admin PIN) → the Badger shows a **recovery key** once (52 letters and digits) → *Print* it or write it down →
*I've written it down*. Until you do this the records are scrambled but "not protected yet": the key sits next to
them.

**Unlock** after a restart: *Sealed records* → owner passphrase → *Unlock* (over https, or at the Badger). Any
family member can tap *Lock now*. `[sealed] idle_minutes` locks it again after a time without use (off by
default: an aid station needs all-day access). Lockdown locks it at once.

**Trade-off:** while locked, family names show as "a family member", the family board shows only posts since
the lock (last 48 hours, at most 200; `[sealed] recent_hours` / `recent_max`), supplies show only new changes,
and nobody **new** can join the family. A running, **unlocked** Badger holds the keys in memory — lock it when
danger comes.

**The recovery card.** One piece of paper, kept away from the Badger (with your passports, in a safe, with a
relative), holding: the sealed-records **recovery key**, the **panel recovery code** (below) and the **Document
vault's recovery key** (made when a family member sets up the vault). They are the way back from a forgotten
passphrase **and the only way to end a duress lock**. *Forgot the passphrase?* on the sealed-records box takes
the recovery key and a new passphrase. If the passphrase and the recovery key are both lost, the sealed records
are gone for good. Details: [SECURITY → Sealed records](SECURITY.md#sealed-records-encryption-at-rest).

### Panel code

**What it does:** without it, anyone who can turn the knob can make a family invite, show the family radio key,
switch off weather alerts or shut the Badger down. With a 4–8 digit code, the knob asks for it before *Invite*,
*Approve device*, *Family radio*, *Posture*, *Services*, *Wi-Fi → Turn off*, *Import from USB*, *Restart* and
*Shut down*. *Join Wi-Fi* and *Open app* never ask (they are the way onto the network). `[panel] lock_protect`
changes the list.

**Set it:** *Set up panel code* on the admin page, then within 10 minutes at the Badger: knob → *System* →
*Panel lock* → *Set a code* (turn to pick a digit, press to add it, then *OK*; then again). Write the **12-digit
recovery code** it shows on the recovery card. (A visitor alone at the knob can't set a code: the admin page has
to allow it first.)

**Trade-off:** after the right code the panel stays open 5 minutes or until the screen sleeps; wrong codes make
it wait longer and longer (up to an hour), even across restarts.

**Way back:** on the code screen choose *Forgot?* and enter the recovery code; or hold the knob down while the
Badger starts; or at the console `sudo python3 /opt/prepper/software/panel/panel.py --reset-lock`. Details:
[TOUR → Knob menu](TOUR.md#knob-menu), [SECURITY → The front panel lock](SECURITY.md#the-front-panel-lock).

### Security posture

One switch for **how much this Badger shows the world**, set on the admin page (*Security* → *Security
posture*) or at the knob (*Posture*, behind the panel code). `[security] posture` is the starting level.

| | Neighbourly | **Guarded** (default) | Lockdown |
|---|---|---|---|
| Radio commands strangers may use | `!help`, `!q` | `!q` | none |
| Answers to strangers per hour (all / one radio) | 12 / 3 | 6 / 2 | 0 / 0 |
| Radio position on the public channel | rough (~6 km) | off | off |
| Radio silence | no | no | yes (family posts you type still go) |
| NOAA alerts on the public channel | yes | yes | family channel only |
| Visitors' "send over radio" requests | need a family member's OK | need a family member's OK | off |
| Neighbourhood, aircraft and satellite pages for visitors | everyone | everyone | family only |
| Sensors for visitors | alerts only | alerts only | hidden |
| Trusted circle | what you opt in | what you opt in | nothing |
| The radio itself | as set | as set | muted (no repeating, no position or telemetry) |
| Wi-Fi without a password | allowed | allowed | gets a password (or turns off) |

**Choosing:** Guarded suits most homes. Neighbourly when you want to help the neighbourhood more (it answers more
radio questions and shares a rough position). Lockdown when danger is close: the radio goes quiet, visitors see
only the library, maps and tools, and sealed records lock. A setting you made yourself (for example *Radio
commands*) still counts, except that in Lockdown nothing may be looser than Lockdown. `[security] lockdown_wifi`
and `lockdown_radio` change what Lockdown does to the Wi-Fi and the radio. Full table and step-by-step:
[features/posture.md](features/posture.md).

### Duress secrets: lock or wipe

For the moment someone forces you to unlock the Badger. All optional, all on the admin page over https under
*If someone forces you to unlock the Badger*:

- a **duress PIN** — a second admin PIN (or `sudo prepperctl set-duress`);
- a **duress passphrase** — typed where the sealed-records passphrase is asked (needs sealed records set up);
- a **duress code** at the knob: *System* → *Panel lock* → *Duress code* (needs a panel code first).

A duress secret behaves **exactly** like the real one — same screens, same timing — but from then on every
screen and every family phone shows the **made-up household** instead of the real one. Behind it, the owner
chooses (`[security] duress_action`, or *What happens behind the made-up household* — the page wins):

| | **Lock** (default, recommended) | **Wipe** |
|---|---|---|
| The family's data | kept, but hard-locked: the passphrases alone no longer open the sealed records or the vault | erased by the panic wipe (keys first) |
| Visible changes | none: no restart, no Wi-Fi change | none at first; after `[wipe] duress_delay_min` (10 min) a new Wi-Fi password, a new certificate, a launcher restart and Lockdown |
| Getting it back | the **recovery keys** from the recovery card | nothing to get back |
| Needs | sealed records set up (otherwise the lock lasts only until a restart) | — |

**People come first.** If the person forcing you can check what you say — they know the family, have seen this
Badger, or know what is on it — give the real secret. A made-up household they can see through is more dangerous
than losing data. Agree on this as a family beforehand, and agree that nobody is blamed afterwards.

**The made-up household.** Every Badger has one ready: as many people as your family (with made-up first names),
an ordinary supplies list in ordinary places, and a few dull board posts. Check it under *The made-up household →
Show and edit it*: if someone who might force you knows your family's first names, type believable ones; *Make a
new one* replaces it with a fresh random one. Ordinary things only — no places, no times when the house is empty.

**Recovery (lock mode),** when everyone is safe: over https, *Sealed records* → *Lock now* → *Unlock* with the
**recovery key** (or at the knob: *System* → *Recovery key*). The real records are back and the family board says
"Recovered after a duress lock." Then open the Document vault with **its** recovery key, typed where its
passphrase goes. What a careful attacker could still notice, honestly: [SECURITY →
Duress](SECURITY.md#duress-when-someone-forces-you-to-unlock-the-badger).

## 5. Wi-Fi

`[network] security` (the builder's `--wifi-security`):

| Mode | What it means | Trade-off |
|---|---|---|
| `wpa2` / `auto` (default) | a password, random by default (WPA2, plus WPA3 on the USB adapter) | everyone needs the password; whoever knows it can read plain http traffic, so change it when someone leaves |
| `owe` (Enhanced Open) | no password, but each phone's traffic is encrypted | phones older than about 2019 (before iOS 13 / Android 10) can't join; the onboard radio can't do it and runs open |
| `owe-transition` | `owe` plus an open twin network for older phones | **experimental, USB adapter only, not yet tested on real hardware**; phones on the open twin are not encrypted. If it misbehaves, use `owe` |
| `open` | no password, no encryption | anyone in range reads everything; the vault and other family secrets then only work over https |

- **The name:** pick a plain one. The builder refuses names with "Badger"/"prepper", and *What this Badger
  reveals* flags words like survival, bunker or emergency.
- **The password** is always on the front panel (knob → *Join Wi-Fi*) — the way back in if it was lost.
- **Changing it:** *Shared secrets* → *New Wi-Fi password* (a safe change: it switches back by itself unless
  someone rejoins and taps *Keep new password* within 10 minutes), or edit `[network] ssid` / `passphrase` and run
  `sudo prepperctl apply-config`.
- **Updates over your home Wi-Fi:** *Join a Wi-Fi network for updates* (the built-in radio joins while the USB
  adapter keeps the hotspot up), or `[network] uplink_wifi`.
- Other keys: `captive` (the join pop-up), `share_uplink` (off: phones on the hotspot don't get the internet),
  `country`, the channels, `max_clients`, `ap_defer_hours`. Reference: [OPERATIONS → Wi-Fi
  security](OPERATIONS.md#wi-fi-security), [SECURITY → The Wi-Fi](SECURITY.md#the-wi-fi).

## 6. Family members

Being a family member gives a device the family board, family radios on the map, Supplies, the Document vault,
USB sticks and the family radio link. Details: [OPERATIONS → Family members](OPERATIONS.md#family-members).

- **Invite:** *Family members* → type their name → *Invite a member*. A 10-character code and a link; it works
  **once, on one device, for 24 hours**. They join the Wi-Fi and open the link, or go to *Check-in board* →
  *Family* and type the code. Or at the knob: *Invite member (QR)*.
- **Another device for someone:** *Add a device* next to their name. The new device **waits for their yes**: their
  own phone asks "A new device wants to join as you". Lost every device? Approve the new one at the knob:
  *Approve device*. (So the admin PIN alone can't make a second "Mom".)
- **Radios:** each member can link their Meshtastic radio: *Settings* → *Family* → *Link my radio* (or the admin
  types the radio ID and taps *Add radio*). The Badger gives a 6-character code; from that radio, **on the family
  channel**, send `!pair CODE` within an hour. Their family-channel messages then carry their name.
- **Removing someone:** *Remove* cuts off all their devices and radios at once. It also offers a **new family
  radio key** (say *OK*): their radio still has the old one. Every family radio then needs the new link (members:
  *Settings* → *Family* → *Family radio link*, or knob → *Family radio* → *Channel link (QR)*). If they knew the
  Wi-Fi password or the admin PIN, change those too (*Shared secrets*). [SECURITY → A removed
  member](SECURITY.md#a-removed-member).
- **Aid volunteers** (often not family) get their own one-time codes on the Aid station page
  ([features/aidstation.md](features/aidstation.md)).

## 7. The mesh radio

Every Badger has a built-in Meshtastic LoRa radio that links its check-in boards to handheld Meshtastic radios
far beyond Wi-Fi range, with no phone network. The full story: [MESH](MESH.md).

**Channels.** In the Meshtastic app (connect to the Badger's radio over Bluetooth; its PIN is on the knob →
*Family radio* → *Bluetooth PIN* once the Badger has replaced a default one), keep the public channel (LongFast)
as **channel 0** and add a **private family channel with its own key** as **channel 1**. The defaults
`[radio] mesh_public_channel = 0` and `mesh_family_channel = 1` then match. Family features stay off until the
family channel is set, and it can never be channel 0. Use `-1` for the public channel to keep the community board
off the air. Share the family channel only with family radios: members scan the *Family radio link*.

**Radio commands** (*Radio commands* on the admin page, or `[radio] mesh_commands`): `!q` (library lookup), `!ai`
(a short AI answer), `!status` (battery, alerts, users) and `!help`, each set to **Community** (anyone in range),
**Family** (only messages on the family channel), **Off** (family radios are told it's off) or **Ignore** (no reply
to anyone). Defaults follow the posture (Guarded: `!q` community, the rest family). Radios outside the family never
get a refusal or help text, and only a few answers an hour. `!help` can never be opened to strangers.

**Privacy and position.** Nothing the Badger sends says "Badger". The radio's position on the public channel is off
(`mesh_public_position`; Neighbourly shares a rough ~6 km one); the family channel keeps what you set in the app.
Give the radio an unremarkable name (*What this Badger reveals* can rename it). A radio still on the default
Bluetooth PIN gets a random one (`mesh_bt_pin`). [MESH → Privacy on the mesh](MESH.md#privacy-on-the-mesh).

**Radio silence** (receive only): Lockdown turns it on. On its own: `[radio] mesh_silent = true`, or create the
file `/var/lib/prepper/portal/mesh-silent` (no restart). The Badger then sends nothing on its own except, with
`mesh_silent_allow_family = true`, family posts a member chose to send. The radio itself still repeats others and
announces itself now and then, except in Lockdown, which mutes it. To be sure it sends nothing, switch the radio off.

**Many Badgers nearby ("mesh courtesy")** is on by default (`mesh_courtesy`): Badgers take turns, so each NOAA
alert goes out once per channel, a question gets one answer, and after a town-wide power cut they come back
quietly. Their control messages go only on the family (and circle) channel, sealed. Budgets and busy-channel
limits: [MESH → Settings](MESH.md#settings-radio-in-preppertoml).

**Trusted circles.** A private channel shared **in person** between a few vetted households. Pull, not push: a
circle radio asks about one topic and gets a pointer ("power: can fix generators — ask Elm St"), never a list,
amount or location. Off until you opt in per category; nothing in Lockdown. Set up under *Security* → *Trusted
circle* (create or join, which needs https; give this Badger a name in the circle; opt in; add offers); the circle
link is on the knob → *Family radio* → *Circle link (QR)*. [features/circles.md](features/circles.md).

**Needs real hardware testing:** muting the radio in Lockdown (CLIENT_MUTE and broadcast intervals on every
firmware), sealed control packets between Badgers, per-channel position precision and circle direct messages are
tested in simulation; [MESH → Limits](MESH.md#limits--what-only-a-real-multi-radio-test-can-confirm) lists what
to check on real radios.

## 8. The two radio receivers (SDR 1 and SDR 2)

Every Badger has two identical RTL-SDR receivers. Background: [HARDWARE → Second SDR](HARDWARE.md#second-sdr-sdr-2).

**Serial numbers (once, at build time).** The Badger tells the dongles apart by a serial stored in each. With only
that dongle plugged in, stop the radio services and run `rtl_eeprom -d 0 -s BADGER-SDR1` (the weather radio), then
the same with `BADGER-SDR2` for the other; `rtl_test -t` with both plugged in should list both. Without serials the
Badger guesses by USB port and shows a warning. [features/aircraft.md](features/aircraft.md#setting-the-radios-serial-numbers-at-build-time).

### SDR 1 (weather radio)

Listens to NOAA Weather Radio all the time on the rear **SDR** jack (the folding whip: unfold it and tilt it back
about 20°). Set your nearest transmitter and county in `[radio]`:

```toml
weather_freq_mhz = 162.550     # your nearest NOAA transmitter (the launcher's Tools → Radio lists them)
same_fips = ["008031"]         # your county's SAME code(s); empty = show every alert heard
```

then `sudo prepperctl apply-config`. A warning for your county shows as a red banner on every phone and on the
front screen, the status light blinks fast, and new warnings go out once over the mesh (`mesh_alerts`). The
forecast-as-text feature records from the same radio ([features/forecast.md](features/forecast.md), `[forecast]`).

### SDR 2 (aircraft and satellites)

What SDR 2 does depends on the antenna on its jack; tell the Badger which (admin: *SDR 2 antenna* on the Aircraft
or Weather satellites page, or `[radio] sdr2_antenna`):

| Antenna on SDR 2 | Setting | Aircraft | Weather satellites |
|---|---|---|---|
| 1090 MHz whip (as shipped) | ADS-B whip / `"adsb"` | all day | not recorded |
| 137 MHz V-dipole, outdoors (arms 53 cm, 120° apart, flat, pointing north–south) | Satellite / `"satellite"` | on demand (*Scan aircraft for 15 min*) | every pass above 15° |
| a wideband antenna (discone) | Both / `"both"` | all day, paused during passes | every pass |

If SDR 2 is missing or fails, the Badger carries on with SDR 1; aircraft scans (and satellite passes only if you
allow `[satellites] fallback_capture`) then borrow the weather radio briefly, pausing alerts. `[radio] sdr_lend =
false` forbids that. Settings: `[aircraft]`, `[satellites]` ([aircraft.md](features/aircraft.md),
[satellites.md](features/satellites.md)). Satellite reception needs a clear sky and real-world testing at your site.

## 9. Sensors

Optional extras for flood, freezing, heat, water level and radiation warnings
([features/sensors.md](features/sensors.md); kit in [BOM](BOM.md#sensor-kit-optional)).

- **Rear SENSORS socket** (M8 4-pin, deliberately different from the power input): pin 1 = 3.3 V (through a
  100 mA polyfuse), pin 2 = GND, pin 3 = 1-Wire data for DS18B20 temperature probes (GPIO 4), pin 4 = float switch
  for the flood alarm (GPIO 24). [HARDWARE → Sensor socket](HARDWARE.md#sensor-socket).
- **Found by themselves:** BME280/BMP280 and SHT3x/SHT4x on the I²C bus, and DS18B20 probes (reboot once after
  adding 1-Wire probes). **Must be listed** in `[sensors]` as `[[sensors.device]]`: float switches, water-level
  sensors, Geiger counters.
- **Who sees readings:** family and admin (`[sensors] public = false`); active alerts are always shown to everyone.
  Mesh alerts go on the family channel by default (`mesh_board`).
- Start it: *Services* → `sensors`; each sensor's status is under *Sensor settings* on the Sensors page.
- Readings are **warnings, not calibrated measurements**, especially radiation.

## 10. Power and solar

- **Inputs:** USB-C PD (45 W+, the internal PD trigger asks for 15 V) and the **SOLAR / DC IN** GX16 socket,
  8–30 V: a portable panel directly (open-circuit voltage 30 V or less; never two panels in series), a 12/24 V
  battery or a car socket, each with its own adapter cable. GX16 pin 1 is +, pin 2 is −: check new cables with a
  multimeter. [HARDWARE → Solar and DC input](HARDWARE.md#solar-and-dc-input).
- **Modes** (knob → *Power mode*, or the admin page): Endurance (~4.5 W, ~10 h on the internal pack), Balanced
  (default, ~6.5 W, ~7 h), Full (9–15 W, 3–5 h). On battery below 30 % it switches to Endurance by itself, and it
  shuts down cleanly at 6 %. `[power]` sets `default_mode`, `auto_endurance_percent`, `shutdown_percent`, the
  optional input meter (`input_monitor = "ina228"`) and the case fan. [TOUR → Power and
  battery](TOUR.md#power-and-battery), [HARDWARE → Power modes](HARDWARE.md#power-modes).
- **Sizing:** a 40 W panel on a sunny day gives about 110 Wh, roughly Endurance around the clock. For Balanced or
  cloudy weeks, use about 100 W of panel or add a 256 Wh LiFePO4 battery.
- **Time:** the GPS sets the clock. Set a home position in `[time]` (`latitude`, `longitude`) for sun times,
  aircraft distances and satellite passes until the GPS has a fix; `[time] display` sets Local, UTC or Nu:Time™
  ([KE-SHI](KE-SHI.md)).

## 11. Updates and content

- **Automatic:** checks daily, downloads weekly whenever there is internet (Ethernet, `uplink_wifi` or a phone on
  USB). `[upd] auto_apply`, `window` (e.g. `"01:00-06:00"`), `reserve_gb` (free space it never fills),
  `max_rate_kbps`. New versions download in the background and swap in only when complete and verified.
- **Manual:** *Check for updates* / *Download updates* on the admin page, or knob → *Updates*.
- **Tier and languages:** `[upd] tier` and `languages`; the next update follows them.
- **From another Badger:** *Pull from another Badger on this network* (or `[upd] peers`). Peers and USB bundles are
  signed: trust the other Badger first with `sudo prepperctl trust "ssh-ed25519 AAAA… name"`.
- **USB bundles (no internet anywhere):** *Export* to a stick on one Badger (or knob → *Updates* → *Export to
  USB*), *Import* on the other (knob → *Import from USB*). An old stick can't roll a Badger back.
- **Software updates** install only when signed by a maintainer key you trust: `sudo prepperctl trust-maintainer
  "<key>"`. Until then, software is not updated.
- **Sold units:** the buyer's personal-use acceptance is recorded on the Badger
  ([OPERATIONS](OPERATIONS.md#handing-a-sold-badger-to-its-owner)); *Settings* → *Personal-use content* can remove
  it again.
- **Your own content:** [OPERATIONS → Adding your own content](OPERATIONS.md#adding-your-own-content).
- `[upd.hooks]` (what runs after an item updates) normally needs no changes.

Full reference: [OPERATIONS → Updates](OPERATIONS.md#updates).

## 12. AI models

The offline AI (`[ai]`) answers *Ask the library*, `!ai`, Voice and the Problem solver, always from the library's
own articles. Models have **roles**: *fast* (Ask, default Qwen3 4B), *reasoning* (deep solves, Qwen3 8B),
*coder* and *endurance* (the small Qwen2.5 1.5B used in Endurance mode). Only one is in memory at a time, and the
Badger never loads one that doesn't fit.

On the Problem solver page an admin sees **AI models**: for each role the downloaded models, their **self-test**
score (six problems with known answers) and speed, and *Run self-test*, *Make active*, *Load now* and *Roll back*.
A new model only becomes active if it scores at least as well as the current one (an admin can override).
Defaults per role: `[solver.models]`; `[ai] model` and `endurance_model` for the fast and endurance roles;
`[solver]` for queue and access. [features/solver.md](features/solver.md#ai-models-admin).

## 13. Panic wipe, handover and the dead-man switch

**Panic wipe** — for when you are about to lose control of the Badger. It destroys the keys first (well under a
second), then erases the family's data; the library, maps and AI stay, so it looks like a plain offline library.
It can't be undone. Start it from the admin page over https (type `WIPE`), the knob (*System* → *Panic wipe* →
*Yes* → hold the knob 3 s), or `sudo prepperctl panic-wipe`. Afterwards it is in Lockdown with a new Wi-Fi password
shown only on the front panel; family members must be invited again. `[wipe]` chooses what happens to the Wi-Fi
(`wifi`), the radio's keys (`radio`) and the mesh bridge (`mesh_after`), and which caches survive
(`keep_features`). If the panel says *Unplug the radio*, do it. [OPERATIONS → Panic
wipe](OPERATIONS.md#panic-wipe-duress-pin-dead-man-switch), [SECURITY → Panic wipe](SECURITY.md#panic-wipe).

**Handover / resale reset** — before you sell or give a Badger away: the panic wipe plus factory settings (a new
Wi-Fi name and password, no admin PIN, no duress PIN, no panel code, no trusted peers, posture back to Guarded).
Admin page over https (*Hand over or sell this Badger*: type `HANDOVER` + admin PIN; tick *I'm selling it* to also
remove personal-use content), the knob (*System* → *Hand over / sell*), or `sudo prepperctl handover-reset`. Also
factory-reset the radio in the Meshtastic app yourself. [OPERATIONS → Before you sell or give away a
Badger](OPERATIONS.md#before-you-sell-or-give-away-a-badger).

**Dead-man switch** (off unless you turn it on) — wipes by itself after 3–365 days of the Badger **running** with
no check-in (a family member opening the app, or an admin login). Two steps on the admin page over https (days +
admin PIN → a code → type it back) or `sudo prepperctl deadman-set DAYS` then `--confirm CODE`. The front panel
warns in the last 48 hours. **Think hard:** if everyone is away, ill or cut off that long, the family data is gone.
Don't set `[wipe] deadman_days` by hand.

## 14. Config file reference

Every section of `/etc/prepper/prepper.toml` ([`os/prepper.toml.example`](../os/prepper.toml.example) explains
every key). All feature sections are optional: each feature works with no settings, and any feature can be
switched off in `[features]`.

| Section | What it sets | Guide |
|---|---|---|
| `[device]` | edition label, unit name, time zone, `provisioning` (`owner` or `sale`) | [Install](#2-install-the-software) |
| `[network]` | host name, Wi-Fi name, password and security mode, channels, https, uplink Wi-Fi | [Wi-Fi](#5-wi-fi) |
| `[upd]` | tier, languages, free-space reserve, schedule, peers, signing (and `[upd.hooks]`) | [Updates](#11-updates-and-content) |
| `[portal]` | the launcher: admin PIN hash (set with `prepperctl set-pin`), duress PIN hash, visitors' radio requests (`visitor_mesh`), search languages, file paths | [Security](#4-security-settings) |
| `[ai]` | the AI model, its Endurance model, threads and context | [AI models](#12-ai-models) |
| `[time]` | Local / UTC / Nu:Time™ display, home position until the GPS has a fix | [Power](#10-power-and-solar) |
| `[power]` | UPS, battery size, auto-Endurance and shutdown levels, default mode, input meter, fan | [Power](#10-power-and-solar) |
| `[panel]` | the screen and knob pins, sleep time, what the panel code guards (`lock_protect`) | [Panel code](#panel-code) |
| `[radio]` | mesh channels, commands, privacy, radio silence, courtesy; weather frequency and county; SDR roles | [Mesh](#7-the-mesh-radio), [SDRs](#8-the-two-radio-receivers-sdr-1-and-sdr-2) |
| `[security]` | the posture and Lockdown's options, logs, visitor pages, the duress action | [Posture](#security-posture) |
| `[sealed]` | sealed records: idle auto-lock, what family members see while locked | [Sealed records](#sealed-records-and-the-recovery-card) |
| `[wipe]` | what the panic wipe does to the Wi-Fi and radio, kept caches, the duress delay, the dead-man switch | [Panic wipe](#13-panic-wipe-handover-and-the-dead-man-switch) |
| `[features]` | Courses, video and book libraries on or off; any built-in feature off (`skills = false`) | [FEATURES](FEATURES.md#turning-features-on-and-off) |
| `[forecast]` | how often to record the NOAA forecast, posting it on the board and the mesh | [forecast.md](features/forecast.md) |
| `[voice]` | who may use Voice, recording length, brief or full answers | [voice.md](features/voice.md) |
| `[aircraft]` | scan length, gain, range | [aircraft.md](features/aircraft.md) |
| `[satellites]` | when to record passes, picture count, fallback, orbit data (and `[[satellites.list]]`) | [satellites.md](features/satellites.md) |
| `[vault]` | auto-lock times, size limits, minimum passphrase length | [vault.md](features/vault.md) |
| `[supplies]` | first-run household size, daily needs, low/critical days, family-board notes | [supplies.md](features/supplies.md) |
| `[aidstation]` | how long records are kept, volunteer codes | [aidstation.md](features/aidstation.md) |
| `[skills]` | entry limits and expiry | [skills.md](features/skills.md) |
| `[missing]` | post and photo limits, radio announcements | [missing.md](features/missing.md) |
| `[sensors]` | who sees readings, units, alerts to boards and mesh, extra devices and alert rules | [sensors.md](features/sensors.md) |
| `[solver]` | who may use the Problem solver, queue and limits (and `[solver.models]`) | [solver.md](features/solver.md) |
