# Using the Badger

Day-to-day use of a Honey Badger (v0.4), for everyone in the family and the neighbours who use it. Nothing needs
the internet, and nothing is installed on your phone. New to it? Start with [QUICKSTART](QUICKSTART.md). Setting
it up or looking after it? See [SETUP](SETUP.md).

**Getting on:** join the Badger's Wi-Fi (on the Badger: press the knob → **Join Wi-Fi (QR)**, which shows a QR
code and the password), then open **http://badger.lan**. Most phones open it by themselves.

## Who can use what

| Who | How | What they get |
|---|---|---|
| **Visitor** | anyone on the Badger's Wi-Fi | the library, Ask, maps, tools, weather alerts, the community board and the neighbourhood pages (in Lockdown: only the library, maps and tools) |
| **Family member** | a phone that joined with a one-time invite code (*Check-in board → Family*) | also the family board, Supplies, the Document vault, USB sticks, family radios on the map, the family radio link |
| **Aid volunteer** | a one-time aid code from the admin | the aid-station visit log |
| **Admin** | the admin passphrase (*Settings → Badger administration*) | settings, invites, removing members, the security pages. The admin passphrase alone does **not** open family-only pages or the vault |
| **The front panel** | standing at the Badger (behind the panel code, if one is set) | the knob menu, below |

## The home screen

At the top, four big buttons: **💬 Ask the library**, **⛑️ First aid & medical**, **🗺️ Open map** and
**📋 Check-in board** (plus **⛑️ Aid station open** when a neighbourhood aid station is open). Then a tile for
each part of the library (Medical, Survival, Maps, Tools, USB & Files…), then the feature pages in four groups:

| Group | Tiles |
|---|---|
| **Family** | 🥫 Supplies *(Family only)* · 🔐 Document vault *(Family only)* |
| **Neighbourhood** | 🔎 Missing & found · 🤝 Skills & resources · ⛑️ Aid station · 🌡️ Sensors |
| **Radio & sky** | 📻 NOAA forecast · ✈️ Aircraft · 🛰️ Weather satellites |
| **More** | 🎙️ Voice · 🧮 Problem solver |

A group only shows when one of its features is switched on. The **search bar** at the top searches everything.
Red banners at the top show weather and sensor alerts. For family and the admin, the **status bar** shows the battery,
people connected and the security posture 🛡 (visitors don't see these). Tap the 🕒 clock in the bottom bar to switch between Local time,
UTC and Nu:Time™.

## Everyday features

### Library search

Type in the search bar: it covers every article, document name, collection and tool. Results open in the
Badger's reader; *Open with ▾* picks another one (tick *Remember* to keep it). The library holds reference,
medical, survival, repair, maps, education, books and video, depending on the size the Badger was built with
([CONTENTS](CONTENTS.md)). Courses, video and books run when they are switched on and the power mode allows.
**Who:** everyone.

### Ask the library

Tap **💬 Ask the library** and ask in plain words. A small AI on the Badger answers **only from the Badger's
own articles** and lists its sources. It can be wrong, so check the sources, especially for anything medical. It
is slower on battery (about a minute). **Who:** everyone. Family radios can ask it too, with `!ai`.

### Problem solver

**More → 🧮 Problem solver** works out real problems step by step and shows its working: power and solar, water
and pipes, building and ropes, heat and fuel, radio range, maths and decisions, short programs and survival
planning. Give it the numbers and units you know and tap **Solve**. Hard problems take a few minutes; you can
leave the page and come back. The **Calculators** box at the bottom gives the same calculators without the AI.
Medical answers only repeat what the library says. It is not for writing letters. **Who:** everyone (the admin can
limit it to family); a family member's problems can use the family's Supplies list. [solver.md](features/solver.md)

### Maps and radio positions

**🗺️ Open map** shows offline maps that work with no signal. Two extra layers:

- **📡 Mesh radios** (family members and the admin): Meshtastic radios that share their position, including the
  family's own. Green = heard within the hour, amber = within a day, grey = older. Also from *Radios on the map*
  on the check-in board.
- **✈️ Aircraft** (everyone): planes the Badger hears, as arrows pointing the way they fly.

### Check-in boards (community and family)

**📋 Check-in board** has two tabs:

- **Community** (everyone): an open notice board for everyone nearby — "water at the church", "need a medic on
  Elm St". Names aren't checked. *Send over mesh* sends it on the radio's public channel; a visitor's request
  waits until a family member or the admin sends it.
- **Family** (family members): private messages under each member's own name. *Send over mesh* uses the family's
  private, encrypted radio channel, so family radios hear it out of Wi-Fi range. To join, type your invite code
  here.

Radio messages are 200 bytes at most. Posts heard over the radio never get the ✓ family mark (anyone can fake a
radio's ID); automatic notes from the Badger itself carry a grey **Badger** mark. Once sealed records are set up,
after a restart and until the owner unlocks them, the family tab shows only posts since then ("Family history is
locked").

### Mesh commands from a radio

From a Meshtastic radio, send a message to the Badger's radio (or on a channel it listens to):

| Command | Answer | Who may use it (Guarded, the default) |
|---|---|---|
| `!q <words>` | the best-matching library article: title and a line | anyone in radio range |
| `!ai <question>` | a two-sentence AI answer with its source (about a minute; one per radio every 2 minutes) | family channel only |
| `!status` | battery, power source, mode, active alerts, people on the Wi-Fi | family channel only |
| `!help` | the list of commands | family channel only |
| `!pair CODE` | links your radio to your name (code from *Settings → Family → Link my radio*) | family channel |

Radios outside the family get **no reply at all** to anything they may not use, and only a few answers an hour.
The admin changes who may use each command under *Radio commands*; the posture changes the defaults. In a
**trusted circle**, a circle radio can send `!help <topic>` (e.g. `!help power`) on the circle channel and gets a
pointer back ([circles.md](features/circles.md)). More: [MESH](MESH.md).

### Weather alerts (NOAA)

The Badger listens to NOAA Weather Radio all the time on its first radio receiver. A warning for your county shows
as a **red banner** on every phone and on the front screen, the status light blinks fast, and each new warning
goes out once over the mesh radio. Keep the whip on the rear **SDR** jack unfolded. **Who:** everyone, and the
front panel. If the banner says *Weather alerts paused*, the weather radio is briefly lent to another job (only
when the second receiver is out of action).

### NOAA forecast

**Radio & sky → 📻 NOAA forecast**: the weather radio's spoken forecast, recorded every few hours and written out
as text, with a one-line summary and older versions. It is transcribed by a computer and can mishear a number or
a place: for warnings, rely on the alert banner. **Who:** everyone reads; the admin can tap *Update now* and change
settings. [forecast.md](features/forecast.md)

### Aircraft

**Radio & sky → ✈️ Aircraft**: planes around you from their own ADS-B broadcasts — flight, height, speed, distance
and direction; emergency codes in red. *Show on the map* opens the aircraft layer. With the aircraft whip on the
SDR 2 jack it watches all day; otherwise a family member or the admin taps *Scan aircraft*. Receive only — not
for navigation. **Who:** everyone sees it (family only in Lockdown); family and admin start scans; the admin sets
the antenna. [aircraft.md](features/aircraft.md)

### Weather satellites

**Radio & sky → 🛰️ Weather satellites**: cloud pictures received straight from the Meteor-M weather satellites as
they pass, and the times of the next passes. Needs the 137 MHz V-dipole set up outdoors on SDR 2. **Who:**
everyone (family only in Lockdown); the admin sets the antenna. [satellites.md](features/satellites.md)

### Document vault

**Family → 🔐 Document vault**: encrypted copies of the papers you'd grab in a fire — IDs, insurance, deeds,
prescriptions, photos of each family member, contact lists. The first family member to open it chooses the
**family passphrase** and gets a **recovery key** to print: put it on the recovery card. Each device unlocks with
the passphrase and locks itself after 15 minutes. *Download encrypted backup* saves a copy for a USB stick. If the
passphrase **and** the recovery key are both lost, nobody can open it — keep the paper originals. **Who:** family
members with the passphrase (not the admin passphrase alone). [vault.md](features/vault.md)

### Supplies

**Family → 🥫 Supplies**: the household's pantry list — water, food, medicine, fuel, batteries. It answers *how many
days do we have?*, warns about things that are expiring or running low, shows how long supplies last if you ration,
and posts a note on the family board when something newly runs low. Spreadsheet import and export. **Who:** family
members. [supplies.md](features/supplies.md)

### Aid station

**Neighbourhood → ⛑️ Aid station**: a public status line ("🟢 Aid station open · water · first aid") and a quick
one-thumb log of who came and what help they got, with today's counts and follow-ups. Volunteers sign in with a
one-time code from the admin (*Aid volunteer? Sign in here*). Records are deleted after 30 days by default and are
never sent over the radio. **Who:** everyone sees the status; volunteers and the admin keep the log.
[aidstation.md](features/aidstation.md)

### Skills & resources

**Neighbourhood → 🤝 Skills & resources**: who nearby can help (nurse, electrician, generator, spare room…) and who
needs help (a ride to the clinic, insulin kept cold). Entries expire after 90 days unless marked *Still current*.
Everything here is visible to everyone on the Wi-Fi, so share only what you're comfortable with. **Who:** everyone;
the admin moderates. [skills.md](features/skills.md)

### Missing & found

**Neighbourhood → 🔎 Missing & found**: missing people and lost & found things, with photos, searchable by name.
One tap marks someone **✅ FOUND SAFE**. Photos lose their hidden location data. The poster can ask for a short
radio announcement; nothing goes on air until the admin approves it, and where it goes depends on the posture.
Everything here is visible to everyone on the Wi-Fi. **Who:** everyone; the admin moderates.
[missing.md](features/missing.md)

### Sensors

**Neighbourhood → 🌡️ Sensors** (when sensors are fitted): temperature, humidity, air pressure, a flood switch, the
water level in a tank, well or creek, a Geiger counter, and readings from Meshtastic radios with sensors. Flood,
freezing, heat and radiation alerts show at the top of every page, go on the board and, for urgent ones, over the
mesh. Readings are warnings, not calibrated measurements. **Who:** alerts for everyone; readings and charts for
family and the admin (unless the admin opens them to everyone). [sensors.md](features/sensors.md)

### Voice

**More → 🎙️ Voice**: tap, ask out loud (up to 30 seconds), and hear the answer from the same offline AI as Ask.
On plain `http://` your phone's own recorder is used (on an iPhone, record in Voice Memos first, then pick the
file); over `https://` it is one tap. English only. Your recording is deleted once it is turned into text. **Who:**
everyone (the admin can limit it to family). [voice.md](features/voice.md)

### Tools, USB & files

- **Tools** (everyone): Morse code, phrasebook, water-treatment dosing, radio frequencies, unit converter, sun and
  moon times, the Nu:Time™ clock, and Android apps to install (Kiwix, Organic Maps, OsmAnd, Trail Sense,
  Meshtastic).
- **USB & Files** (family members and the admin): plug a USB stick into a front port and browse its files. Before
  pulling it out, eject it at the knob: **USB: eject**.

### Phones, iPads and laptops

Nothing is installed and nothing is stored on your device unless you choose. On an iPhone or iPad: Safari →
Share → *Add to Home Screen*. On Android: Chrome → *Install app*. On a laptop: any browser. The launcher's
*Phones, iPads & laptops* page (link at the bottom of the home screen) has the steps.

### Using https

`http://badger.lan` works for everything day to day. A few things need **https://badger.lan**: unlocking the
sealed records, the emergency controls, joining a trusted circle, seeing the family radio link, the vault on a
Wi-Fi without a password, and one-tap Voice. To use it, install the Badger's certificate once from
`http://badger.lan/ca.crt` (on an iPhone, then turn it on under Settings → General → About → Certificate Trust
Settings) and open **https://badger.lan**.

### Status and Settings

- **Status** (*status* in the status bar): battery, power mode and people connected (family and admin only), the Wi-Fi name, storage, the
  software version, services, and update progress ("Building this Badger" while the first download runs).
- **Settings → This device**: appearance (including *Night vision (red)*), how times are shown, text size, which
  reader opens what, and **Family** (your name, *Family radio link*, *Link my radio*, *Forget this device*).
- **Settings → Badger administration** (admin passphrase): power mode, updates, family members, shared secrets,
  radio commands, services, restart and shut down, sealed records, the panel code and the emergency controls.
- **Security pages**, from *Badger administration → Security* (family members can open the first and third
  from *Settings → Family*):
  - **🛡️ Security posture** — Neighbourly / Guarded / Lockdown ([posture.md](features/posture.md));
  - **👁️ What this Badger reveals** — a green/amber checklist with one-tap fixes, admin only ([reveal.md](features/reveal.md));
  - **🤝 Trusted circle** — a few vetted households sharing "who can help" pointers by radio ([circles.md](features/circles.md)).

## The front panel

The small screen cycles through the Wi-Fi name, battery, address, time, update progress and alerts; it goes dark
after 45 seconds (touch the knob to wake it). **Turn** the knob to move, **press** to choose, **hold 3 s** on the
home screen to shut down (it asks first). Menu entries marked 🔒 in [TOUR → Knob menu](TOUR.md#knob-menu) ask for
the panel code, if one is set.

| Menu entry | What it does |
|---|---|
| **Join Wi-Fi (QR)** | QR code and the whole Wi-Fi password — never locked |
| **Open app (QR)** | QR code that opens badger.lan — never locked |
| **Invite member (QR)** | a one-time family invite to scan (once, 24 h) |
| **Approve device** | a device added to someone's name, waiting for a yes (only shown when there is one) |
| **Family radio** | *Channel link (QR)* for family radios, *Circle link (QR)*, the radio's *Bluetooth PIN* |
| **Posture** | shows *Posture: Guarded* (or the current level); choose another, *Lockdown, Wi-Fi off*, and *What it reveals* |
| **Status** | battery, time left, storage, temperature, users, updates, GPS |
| **Sensors** | alerts first, then readings (when sensors are fitted) |
| **Radio** | weather radio listening or paused, SDR 2, the latest forecast, *SDR 2 antenna* |
| **Neighbourhood** | aid station open or closed, missing people |
| **Power mode** | Endurance · Balanced · Full |
| **Services** | AI, Courses, Mesh, Alerts on or off |
| **Updates** | Check now · Download now · Import from USB · Export to USB |
| **Time format** | Local · UTC · Nu:Time™ |
| **USB: eject** | eject a stick before pulling it out |
| **Wi-Fi** | Turn on · Turn off |
| **System** | Restart · Shut down · Panel lock · Recovery key · Panic wipe · Hand over / sell |

## Feature reference

Every built-in feature, by its id (the name used in `/etc/prepper/prepper.toml`, where `[features] <id> = false`
turns it off):

| Id | Page | Who |
|---|---|---|
| `supplies` | Family → 🥫 Supplies | family members |
| `vault` | Family → 🔐 Document vault | family members + the family passphrase |
| `missing` | Neighbourhood → 🔎 Missing & found | everyone (family only in Lockdown); admin moderates and approves announcements |
| `skills` | Neighbourhood → 🤝 Skills & resources | everyone (family only in Lockdown); admin moderates |
| `aidstation` | Neighbourhood → ⛑️ Aid station | status: everyone (family only in Lockdown) · log: aid volunteers and the admin |
| `sensors` | Neighbourhood → 🌡️ Sensors | alerts: everyone · readings: family and admin (unless made public) · settings: admin |
| `forecast` | Radio & sky → 📻 NOAA forecast | everyone; admin settings |
| `aircraft` | Radio & sky → ✈️ Aircraft | everyone (family only in Lockdown); family and admin start scans; admin sets the antenna |
| `satellites` | Radio & sky → 🛰️ Weather satellites | everyone (family only in Lockdown); admin sets the antenna |
| `voice` | More → 🎙️ Voice | everyone (admin can limit it to family) |
| `solver` | More → 🧮 Problem solver | everyone (admin can limit it to family); admin manages AI models |
| `posture` | Settings → Security posture; knob → Posture | admin and the front panel change it; family members see it |
| `reveal` | Settings → What this Badger reveals; knob → Posture → What it reveals | admin and the front panel |
| `circles` | Settings → Trusted circle | admin sets it up; family members ask the circle and see its link |

The library, Ask, maps, tools, boards, weather alerts and USB & Files are part of the launcher itself, not
switchable features.

## Emergencies

### Power cut

The Badger runs on its internal battery (about 7 hours in Balanced, about 10 in Endurance) and charges from solar
or a 12 V battery on the **SOLAR / DC IN** socket. Below 30 % it switches to Endurance by itself (Courses, video
and books stop; a smaller AI); at 6 % it shuts down cleanly. Press the power button to start it again.

**After any restart the sealed records are locked** (once they are set up) until the owner unlocks them (over https, or at the Badger).
While locked, everything keeps working: weather alerts, the library, Ask, maps, the community board, the radio,
and new entries (supplies, family posts, the aid log) are saved sealed. Family members see family posts written
since the lock; older history, full supplies, names and contact details come back once the owner unlocks.

### Lockdown

When danger is close: **Settings → Badger administration → Security → Security posture → Lockdown**, or at the
knob: **Posture → Lockdown** (or *Lockdown, Wi-Fi off*). Within seconds the radio goes quiet and is muted, visitors
see only the library, maps and tools, nothing is shared with a trusted circle, sealed records lock, and a Wi-Fi
without a password gets one (shown on the front panel; it switches back unless someone rejoins and taps *Keep*
within 10 minutes). Family members carry on as normal. Choose Guarded again when it's over; the radio's settings
come back. [posture.md](features/posture.md)

### Radio silence

Lockdown includes radio silence: the Badger sends nothing on the radio except family posts a member types and
chooses to send. Whoever set up the Badger can also turn on radio silence alone ([SETUP → The mesh
radio](SETUP.md#7-the-mesh-radio)). The radio itself can still be found while it is switched on; to be sure,
unplug or switch off the radio.

### Someone forces you to unlock it

**People come first. The Badger's data is never worth a life.** If the person can check what you say — they know
the family, have seen this Badger, know what is on it — give the **real** PIN or passphrase. Only if they can't
tell, give a **duress** secret (if your family set one up): the duress PIN at the admin login, the duress
passphrase where the owner passphrase is asked, or the duress code at the knob. Everything looks normal, but every
screen and every family phone now shows a made-up household. Whoever is forced gives whatever keeps people safest,
and nobody blames them afterwards.

**Afterwards, when everyone is safe:** if the family chose *Lock* (the default), nothing was erased. Over https,
open *Settings → Badger administration → Sealed records → Lock now → Unlock* and type the **recovery key** from the
recovery card (or at the knob: *System → Recovery key*). The real records come back and the family board says
"Recovered after a duress lock." Then open the Document vault with **its** recovery key, typed where its
passphrase goes. If the family chose *Wipe*, the data is gone. [SETUP → Duress
secrets](SETUP.md#duress-secrets-lock-or-wipe)

### Panic wipe

If the Badger is about to be taken (raid, looting, a group you don't trust is taking over), the panic wipe erases
the family's data in seconds — who lives here, the supplies and where they are, the boards, the vault — and keeps
the library, maps and AI, so it looks like a plain offline library. **It can't be undone.**

- At the knob: **System → Panic wipe → Yes**, then **hold the knob for 3 seconds** (a short press cancels).
- On the admin page over https: *Emergency: panic wipe* → type `WIPE` → *Wipe family data now*.

Afterwards it is in Lockdown with a new Wi-Fi password (read it at the knob: *Join Wi-Fi*), and family members must
be invited again. If the screen says *Unplug the radio*, do that. [OPERATIONS → Panic
wipe](OPERATIONS.md#panic-wipe-duress-pin-dead-man-switch)

### A lost or stolen phone, or removing a member

- **Lost phone:** the admin opens *Family members* and taps *forget* next to that device (or *Remove* for the whole
  person). In the vault, *Lock the vault on every device*, then change the vault passphrase.
- **Removing someone** cuts off all their devices and radios at once and offers a **new family radio key** (say
  *OK*). Every family radio then needs the new link: on your phone, *Settings → Family → Family radio link*, or at
  the knob, *Family radio → Channel link (QR)*. If they knew the Wi-Fi password or the admin passphrase, change
  those too (*Shared secrets*). [SETUP → Family members](SETUP.md#6-family-members)

### A forgotten passphrase or code

| Forgotten | Way back |
|---|---|
| Wi-Fi password | knob → **Join Wi-Fi (QR)** always shows it |
| Admin passphrase | at the Badger's console: `sudo prepperctl set-pin` |
| Sealed-records passphrase | *Sealed records → Forgot the passphrase?* with the **recovery key** from the recovery card |
| Document vault passphrase | *Forgot the passphrase?* in the vault with **its** recovery key. Both lost: nobody can open it |
| Panel code | on the code screen choose *Forgot?* and enter the 12-digit recovery code; or hold the knob while the Badger starts |
| My phone isn't "family" any more | ask the admin for a new invite |

## Maintenance

- **Monthly:** run on battery for 10 minutes to exercise the cells; check *Status* for errors; open **What this
  Badger reveals** and fix anything amber; check the recovery card is still where you put it.
- **Updates** come by themselves weekly whenever the Badger has internet; or *Download updates* on the admin page,
  or a USB bundle from another Badger. The status bar shows "↑ N updates" when some are waiting.
- **Battery in storage:** every 6–12 months charge to 50–60 %, power on, let updates run.
- **Dust and seals:** keep the fan grille on the left side clear and clean its dust filter; check the lid gasket;
  keep caps on the SDR 2 and SENSORS sockets when nothing is plugged in; fold the weather whip away for transport.
- **Spares in a Faraday bag:** the rescue microSD (taped inside the lid), a USB update bundle, a spare Pi if you
  can. [OPERATIONS → Maintenance](OPERATIONS.md#maintenance)

## Troubleshooting

The full table is in [OPERATIONS → Troubleshooting](OPERATIONS.md#troubleshooting). The common ones:

| Problem | Try |
|---|---|
| Can't see the Badger's Wi-Fi | knob → *Wi-Fi* → *Turn on*. During the first download, a Badger without the USB Wi-Fi adapter waits up to 12 hours before starting its hotspot |
| Joined the Wi-Fi but nothing opens | type **http://badger.lan** in the browser (not a search); turn off mobile data for a moment |
| Forgot the Wi-Fi password | knob → *Join Wi-Fi (QR)* |
| A page says "Family only" | join the family on this phone: *Check-in board → Family*, with an invite code |
| The admin page says there is no admin PIN | set one at the Badger: `sudo prepperctl set-pin` |
| No box to type the sealed-records passphrase | you're on `http://`: open **https://badger.lan** (see [Using https](#using-https)) |
| "Family history is locked" | the Badger restarted; the owner unlocks the sealed records |
| Ask is slow or says the model isn't running | it is slow on battery; *Settings → Badger administration → Services → llama* — start |
| Wikipedia says "not downloaded" | it arrives last: *Status* shows progress |
| No weather alerts | unfold the whip on the **SDR** jack; check the transmitter and county are set ([SETUP](SETUP.md#sdr-1-weather-radio)) |
| No aircraft | check the antenna on **SDR 2** and the box at the top of the Aircraft page; indoors, try a window |
| Voice won't record | on `http://`, use your phone's recorder (or install the certificate and use https) |
| Vault won't unlock | the family passphrase, or *Forgot the passphrase?* with its recovery key; after 5 wrong tries, wait 10–15 minutes |
| Knob says "Too many wrong codes" | wait the minutes it shows, or use *Forgot?* with the recovery code |
| USB stick doesn't show | only family members and the admin see USB sticks |
