# What the Badger does

Everything the Honey Badger offers a family, on one page. Join its Wi-Fi, open **http://badger.lan**, and it's all on the home screen. Nothing needs the internet, and nothing is installed on your phone.

**Who can use what.** Each feature below says who it is for:

- **Visitor**: anyone on the Badger's Wi-Fi: neighbours, guests, anyone nearby.
- **Family member**: a device that joined with a one-time invite code (Check-in board → Family). Features marked *Family only* on the home screen need this.
- **Admin**: whoever has the admin PIN (Settings → Badger administration). Admins change settings and tidy up; the PIN alone does **not** open family-only pages.
- **The Badger's own screen**: the small screen and knob on the front. They work with no phone at all.

How it fits together: [TOUR](TOUR.md) · how to run it: [OPERATIONS](OPERATIONS.md) · what protects what: [SECURITY](SECURITY.md).

## The library and everyday tools

These were the Badger's first job, and they're still at the top of the home screen.

**Offline library** (visitor). Reference, medical, survival, repair, maps, education, books, video and more, up to 1.35 TB. Search covers every article, document name, collection and tool. *Open with ▾* picks another reader. Courses (Kolibri), video (Jellyfin) and books (Kavita) run when enabled and the power mode allows. What's included: [CONTENTS](CONTENTS.md).

**Ask the Library: offline AI** (visitor). Ask a question in plain words. A small AI model on the Badger answers **only from the Badger's own articles**, and lists its sources. It can be wrong, so check the sources, especially for medical questions. It is slower on battery. Family radios can ask it too with `!ai` (see *Mesh radio* below). Details: [OPERATIONS](OPERATIONS.md#everyday-use).

**Offline maps** (visitor). Maps that work with no signal. Extra layers: *📡 Mesh radios* (radios that share their position; family members also see the family's own radios) and *✈️ Aircraft* (see *Radio & sky*).

**Tools** (visitor). Morse code, phrasebook, water-treatment dosing, radio frequency reference, unit converter, sun and moon times, and the Nu:Time™ clock. *USB & Files* shows what's on a stick in the front USB ports. The *Phones, iPads & laptops* page installs the launcher as an app and offers Android apps (Kiwix, Organic Maps, OsmAnd, Trail Sense, Meshtastic).

**Weather alerts** (everyone, and the Badger's own screen). The Badger listens to NOAA Weather Radio all the time on its first radio receiver (SDR 1). A warning for your county appears as a red banner on every phone and on the front screen, the status light blinks fast, and new warnings go out once over the mesh radio. Set your transmitter and county codes under `[radio]`; see [OPERATIONS](OPERATIONS.md).

**Updates** (admin, or automatic). New content and software arrive from the internet, from another Badger, or from a USB stick, in the background while everyone keeps using it. Everything is checked before it's used. Details: [OPERATIONS](OPERATIONS.md#updates).

**Front screen and knob** (the Badger's own screen). Wi-Fi QR code to join, status and battery, power modes, family invites, updates and safe shutdown, without a phone. Details: [TOUR](TOUR.md#front-panel).

## Family

**Check-in board: Family tab** (family member). Messages only the family can read, under each member's own name. *Send over mesh* uses the family's private, encrypted radio channel, so family radios hear it even when they're out of Wi-Fi range. Admins invite members and add devices; the front screen can show an invite QR code too. Details: [OPERATIONS](OPERATIONS.md#family-members).

**🥫 Supplies** (family member). The household's pantry list: water, food, medicine, fuel and batteries. It works out *how many days do we have?*, warns about things that are expiring or running low, and shows how long supplies last if you ration. It posts a note on the family board when something newly runs low. Spreadsheet import and export. Not visible to visitors or to the admin PIN alone. Details: [features/supplies.md](features/supplies.md).

**🔐 Document vault** (family member + the family passphrase). Encrypted copies of the papers you'd grab in a fire: IDs, insurance, deeds, prescriptions, photos of each family member, contact lists. Each device unlocks with the family passphrase and locks itself after 15 minutes. Keep the printed **recovery key** safe: if the passphrase and the recovery key are both lost, **nobody can open the vault**, not even the admin. Details: [features/vault.md](features/vault.md).

## Neighbourhood

**Check-in board: Community tab** (visitor). An open notice board for everyone nearby: "water at the church", "need a medic on Elm St". Names aren't checked. Posts can go out over the mesh radio's public channel. Details: [OPERATIONS](OPERATIONS.md#everyday-use).

**🔎 Missing & found** (visitor; admin moderates). Missing people and lost-and-found items, with photos, searchable by name. One tap marks someone **✅ FOUND SAFE** so nobody keeps searching. Photos lose their hidden location data. The poster can ask for a short radio announcement; nothing goes on air until an admin approves it. Everything here is visible to everyone on the Wi-Fi. Details: [features/missing.md](features/missing.md).

**🤝 Skills & resources** (visitor; admin moderates). Who nearby can help (nurse, electrician, generator, spare room…) and who needs help (a ride to the clinic, insulin kept cold). Entries expire after 90 days unless marked *Still current*. Visible to everyone on the Wi-Fi, so share only what you're comfortable with. Details: [features/skills.md](features/skills.md).

**⛑️ Aid station** (status line: visitor · visit log: aid volunteers and admin). A public status line ("🟢 Aid station open · water · first aid") and a quick one-thumb log of who came and what help they got. Volunteers sign in with a one-time code from the admin; they don't have to be family. Records are deleted automatically after 30 days by default, and are never sent over the radio. Details: [features/aidstation.md](features/aidstation.md).

**🌡️ Sensors** (visitor reads, unless the admin limits it to family; admin changes alerts). Temperature, humidity and air pressure, flood switch, water level in a tank, well or creek, a USB Geiger counter, and readings from Meshtastic radios that carry their own sensors. Flood, freezing, heat and radiation alerts show at the top of the page, go on the neighbourhood board and, for urgent ones, over the mesh radio. Sensors are optional extras that plug into the rear SENSORS socket, the I²C bus or a front USB port. Details: [features/sensors.md](features/sensors.md).

**Mesh radio** (visitor via the community channel, family via the family channel). The Badger's built-in Meshtastic LoRa radio links the check-in boards to handheld Meshtastic radios far beyond Wi-Fi range, with no phone network. Radios can send commands: `!q` (a library lookup), `!ai` (a short AI answer), `!status` (battery, alerts, users) and `!help`. The admin chooses who may use each one. Details: [OPERATIONS](OPERATIONS.md#everyday-use).

## Radio & sky

The Badger has **two radio receivers**. SDR 1 is the weather radio and always listens for alerts. SDR 2 watches aircraft and weather satellites, depending on which antenna is on its rear jack. See [HARDWARE](HARDWARE.md#second-sdr-sdr-2).

**📻 NOAA forecast** (visitor reads; admin settings and *Update now*). The Badger records a few minutes of NOAA Weather Radio every 3 hours, turns the spoken forecast into text, and keeps it for everyone to read, with a one-line summary. New forecasts can be posted on the community board and, briefly, over the mesh radio. It's transcribed by a computer and may contain mistakes: for warnings, rely on the alert banner. Details: [features/forecast.md](features/forecast.md).

**✈️ Aircraft** (visitor sees the list and map; family members or admin start a scan; admin sets the antenna). Planes around you, with flight number, height, speed, distance and direction, from their own ADS-B broadcasts on 1090 MHz. Emergency codes are flagged in red. With the 1090 MHz whip on SDR 2 (as shipped) it watches all day; otherwise tap *Scan aircraft for 15 min*. Receive only; not for navigation. Details: [features/aircraft.md](features/aircraft.md).

**🛰️ Weather satellites** (visitor; admin sets the antenna and orbit data). Cloud pictures of your region received straight from the Russian **Meteor-M** weather satellites as they pass over, plus the times of the next passes. Needs the 137 MHz V-dipole from the kit set up outdoors on SDR 2. (The American NOAA APT satellites were switched off in 2025.) Details: [features/satellites.md](features/satellites.md).

## More

**🎙️ Voice** (visitor; admin can limit it to family). Ask a question out loud on your phone and hear the answer. It uses the same offline AI as *Ask the Library*. Useful when your hands are busy or reading is hard. On plain `http://` your phone's recorder is used; over `https://` it's one tap. English only. Your recording is deleted once it's turned into text. Details: [features/voice.md](features/voice.md).

**🧮 Problem solver** (visitor; admin chooses AI models). Works out real problems — power and solar, water and pipes, structures and ropes, heat and fuel, radio, maths, games and decisions, simple code, survival planning — step by step with calculators and the library, and shows its working. Not for writing letters. Details: [features/solver.md](features/solver.md)

## Turning features on and off

Each feature has a `[<name>]` section in `/etc/prepper/prepper.toml` (documented in `os/prepper.toml.example`), and any of them can be switched off there with `[features] <name> = false`, for example `[features] skills = false`. Most also have an admin box at the bottom of their own page.

## Extra hardware

| Feature | Needs |
|---|---|
| Library, AI, maps, boards, Supplies, Vault, Missing & found, Skills, Aid station, Voice, Problem solver | nothing extra |
| Weather alerts, NOAA forecast | SDR 1 and its folding whip (every Badger) |
| Aircraft, Weather satellites | SDR 2, the 1090 MHz whip and the dipole kit (every Badger) |
| Mesh radio, radio announcements | the built-in Meshtastic node (every Badger) |
| Sensors | the optional [sensor kit](BOM.md#sensor-kit-optional) |
| NOAA forecast, Voice | the speech software, added by the installer |
