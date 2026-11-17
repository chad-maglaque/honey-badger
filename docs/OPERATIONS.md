# Operating manual

**Start here:** new to the Badger? [QUICKSTART](QUICKSTART.md) gets it running and secured on one page. Step-by-step setup with the trade-offs of each security setting: [SETUP](SETUP.md). Day-to-day use and emergencies: [USAGE](USAGE.md). This manual is the reference behind them.

## Building a Badger

```bash
# 1. See what fits (sizes, update headroom, first-download time)
./builder/prepper-build plan --tier base --langs eng,spa --drive-gb 1000 --mbps 200 -v

# 2. Put the NVMe in a USB enclosure (or use an SD card) and find it
diskutil list external          # macOS
lsblk                           # Linux

# 3. Stream the OS onto it and inject this unit's config (nothing is saved locally)
sudo ./builder/prepper-build flash /dev/disk4 --tier base --langs eng,spa \
     --hostname prepper --ssid Home-7Q4K --pin 246813

#    The Wi-Fi gets a random password (printed at the end) unless you give --passphrase.
#    For no password: --wifi-security owe (each phone still encrypted; phones from ~2019 on;
#    "owe-transition" also lets older phones join) or --wifi-security open (no encryption at all).
#    Pick a plain name: one that says "Badger" tells passers-by what is inside.

# 4. Install the drive, plug the Badger into your router by Ethernet, power on, then follow along
./builder/prepper-build watch badger.local
```

What happens next:

1. **Install.** The Badger installs its software (about 20 minutes, most of it compiling llama.cpp), then reboots.
2. **First content pull.** After the reboot it starts downloading content, smallest items first. The essentials (medical, survival, water, tools, maps overview) arrive within the first hour. The full English Wikipedia arrives last. The pull is retried until it's done, and a reboot in the middle resumes it. The launcher's Status page shows progress.
3. **Hotspot.** With the USB adapter, your Wi-Fi (the name you chose with `--ssid`) doesn't wait for the download. With no USB adapter and the onboard radio as the download link, the hotspot waits until the pull is done, or at most `[network] ap_defer_hours` (default 12).
4. **Clean-up.** At the end, the relay/proxy settings and the secrets the builder left on the boot partition are removed.

The PIN must be at least 6 characters.

**No Ethernet at the build site?** Pick one:

- `--uplink 'HomeWiFi:password'`: the onboard radio downloads. With no USB adapter, the access point waits until the download finishes, or `ap_defer_hours` at most.
- macOS Internet Sharing over a cable.
- `./builder/prepper-build relay` with `--relay 10.10.11.1:3128`: a pass-through proxy on a direct cable that stores nothing on the laptop. The Badger forgets the relay when provisioning finishes.

**Building several units?** Build one, then point the rest at it. They never touch the internet:

```bash
ssh prepper-admin@badger.local cat /etc/prepper/keys/disk_ed25519.pub   # copy this line
sudo ./builder/prepper-build flash /dev/disk4 --tier base --hostname badger2 \
     --peer http://badger.local/sync --trust-key "ssh-ed25519 AAAA… prepper"
```

### Building Badgers to sell

Some library items are licensed for **personal, non-commercial use only** (catalog `use = "personal"`: iFixit, Khan Academy, Crash Course, LibreTexts, TED, Hesperian, creator-owned video channels, MedlinePlus and a few others; `plan -v --for-sale` lists them). They must not be on a Badger when it is sold. Add `--for-sale` to every unit you will sell:

```bash
./builder/prepper-build plan --tier base --for-sale -v          # what a unit for sale holds, and what waits for its owner
sudo ./builder/prepper-build flash /dev/disk4 --tier base --for-sale --hostname badger \
     --ssid Home-7Q4K --pin 246813
./builder/prepper-build watch badger.local                      # at the end: "✓ For sale: no personal-use content …"
```

- `--for-sale` writes `[device] provisioning = "sale"` into the unit's `prepper.toml`. Without it the unit is `"owner"`, which is today's behaviour for people building their own.
- A unit for sale never downloads personal-use items, from anywhere: the internet, another Badger (`--peer`, `clone`), a USB bundle or `upd adopt`. Pins and tiers can't override it, and `plan --for-sale --pin-item <personal item>` refuses.
- The first content pull, and every update after it, fetch only the rest.
- **Don't accept the personal-use offer in the launcher yourself.** That is the buyer's decision (see [Handing a sold Badger to its owner](#handing-a-sold-badger-to-its-owner)). `watch` reports whether the unit is still clean to sell, and so does `GET /api/content/personal` (`"sale_ready": true`).
- Selling a Badger you used yourself? Re-flash it with `--for-sale` first — or, if you can't re-flash, run a **handover reset** with "I'm selling it" ticked (see [Before you sell or give away a Badger](#before-you-sell-or-give-away-a-badger)).

## Handing a sold Badger to its owner

On a Badger built with `--for-sale`:

1. **First setup.** The home page shows "Finish setting up this Badger". When the owner unlocks *Settings* with the admin PIN, the first card is the setup step: *"Some free courses and repair guides (iFixit, Khan Academy, LibreTexts, TED, …) are licensed for personal, non-commercial use only. They aren't included on the Badger as sold. Download them now for your own personal use?"* It lists every item this Badger's tier would hold, with its size and licence. Each item has a tick box, so the owner can leave out big ones (Khan Academy alone is 180 GB on Ultimate).
2. **Accept & download.** The owner ticks "I'll use these for personal, non-commercial purposes only" and, optionally, types their name. The Badger records who accepted, when, from which device address, and which items and licences, in `/var/lib/prepper/portal/personal-content.json`. Then it starts a normal update download. The usual rules apply: smallest first, resumable, items that don't fit in the free space past `[upd] reserve_gb` are skipped (Status shows them). In Endurance mode or on battery it doesn't start right away; the items come with the next update (the weekly one, or *Download updates*).
3. **No internet at hand-off?** Accepting still works. The items download the next time the Badger is online.
4. **Not now.** The offer stays in *Settings → Personal-use content* and as a notice on the home and Status pages. Anyone can hide that notice on their own device. It comes back if new items appear.
5. **Everyone can see the list and whether it was accepted.** Only the admin can accept or remove, and only the admin sees who accepted.
6. **Manual items** (Hesperian, Kolibri channels, a repeater list) are never downloaded automatically. Accepting lets the owner add them with `upd adopt`, which a unit for sale otherwise refuses.
7. **USB bundles and other Badgers.** A unit for sale skips personal-use items in a bundle or from a peer until the owner has accepted them. *Import* says so.
8. **Removing them.** *Settings → Personal-use content → Remove personal-use content* deletes them from the drive with the next update run (started right away) and stops future downloads, on any Badger. Accepting again brings them back.

From the shell: `sudo python3 /opt/prepper/software/upd/upd.py status` shows the provisioning mode and what is waiting. The API is `GET /api/content/personal` (everyone; read-only) and `POST /api/admin/personal` with `{"action": "accept", "agree": true, "items": [...], "name": "..."}`, `{"action": "later"}` or `{"action": "remove"}` (admin).

This follows each item's licence as recorded in the catalog. It is not legal advice.

## First-boot security checklist

Do these once, on the first day, before the family's data goes on the Badger (each takes a minute; the admin page
is at Settings → Badger administration — open it with `https://badger.lan` so secrets don't cross the Wi-Fi in the
clear):

1. **Admin passphrase.** Set an admin PIN/passphrase if the builder didn't (`--pin`), or change the one you were
   given: `sudo prepperctl set-pin` at the Badger (a passphrase of 10+ characters — a few words — is best).
2. **Sealed-records passphrase + recovery key.** Settings → Badger administration → *Sealed records* → set the owner
   passphrase (16+ characters, or four or more words, not the admin PIN). **Print or write down the recovery key**
   it shows once, keep it away from the Badger, then tick *Yes, it's written down*. Until this is done the records
   are sealed but "not protected yet" ([SECURITY.md → Sealed records](SECURITY.md#sealed-records-encryption-at-rest)).
3. **Panel code.** *Set up panel code* on the admin page, then at the knob within 10 minutes (see *Family members →
   Panel code* below). Write down its recovery code too.
4. **Posture.** Settings → Badger administration → *Security posture*: Guarded is the default; choose Neighbourly
   to help neighbours more, Lockdown when danger is close ([posture.md](features/posture.md)).
5. **Check "What this Badger reveals".** Settings → *What this Badger reveals*: fix anything amber with its one-tap
   button ([reveal.md](features/reveal.md)).

6. **Duress secrets (recommended).** A made-up household is already ready on every Badger; give it a way in: a
   duress PIN, a duress passphrase and (at the knob) a duress code — admin page → *If someone forces you to unlock the
   Badger*. Agree as a family when to use them (see [Duress](#duress-when-someone-forces-you-to-unlock-the-badger)).

Optional, once you've thought about it: the dead-man switch (see [Panic wipe, duress PIN, dead-man
switch](#panic-wipe-duress-pin-dead-man-switch)).

## Before you sell or give away a Badger

Run a **handover reset**. It does everything the panic wipe does (below) and puts the Badger back to factory settings. The library, maps, AI and software stay, so the next owner doesn't wait days for downloads.

- **Admin page** (Settings → Badger administration → *Hand over or sell this Badger*, over `https://`): type `HANDOVER`, your admin PIN, tick *I'm selling it* if you are, then *Reset for the next owner*.
- **Front panel:** knob → System → *Hand over / sell* (behind the panel code if one is set) → *Yes* twice → hold the knob for 3 seconds.
- **Console:** `sudo prepperctl handover-reset` prints a one-time code, then `sudo prepperctl handover-reset --confirm CODE [--sale]` within 2 minutes.

Afterwards the Badger has a new Wi-Fi name like `Home-K7QX` and a new password, both on the front panel (knob → *Join Wi-Fi*). It has **no admin PIN**: the new owner sets one at the Badger (`sudo prepperctl set-pin`, keyboard and screen or SSH). The posture is back to Guarded (the default). Also gone: the duress PIN, the dead-man switch, the panel code, the list of trusted Badgers and peers (`[upd] peers`, `allowed_signers`), the home position in `[time]`, and the signing identity (a new one is made). On a unit built for sale (or with *I'm selling it*), personal-use content is removed and `[device] provisioning` is `"sale"` again, so the next owner gets the setup offer.

The mesh radio gets a **factory reset** request. The bridge has to support it (see SECURITY.md → Panic wipe → *The radio*): to be sure, factory-reset the radio yourself in the Meshtastic app before it leaves your hands.

## Panic wipe, duress PIN, dead-man switch

For the moment the Badger might be taken, or someone forces you to open it. Read [SECURITY.md → Panic wipe](SECURITY.md#panic-wipe) for what it erases, what survives and the limits.

**When to use it:** you are about to lose control of the Badger (raid, looting, a group you don't trust is taking it over), and the family's data — who lives here, when the house is empty, what supplies there are and where they are hidden, the neighbours you can reach on the radio — must not go with it. It takes a few seconds and can't be undone. The library, maps and AI stay: the box looks like a plain offline library.

**Three ways to start it:**

1. **Admin page** (Settings → Badger administration → *Emergency: panic wipe*): type `WIPE`, then *Wipe family data now*. Only over `https://` (or on the Badger itself), so someone who sniffed an admin session on plain Wi-Fi can't fire it.
2. **Front panel:** knob → System → *Panic wipe* → *Yes* → **hold the knob for 3 seconds**. Behind the panel code if one is set. A short press cancels; so does the screen going to sleep.
3. **Console:** `sudo prepperctl panic-wipe` (prints a one-time code), then `sudo prepperctl panic-wipe --confirm CODE` within 2 minutes.

**Afterwards:** the Badger is in **Lockdown** (radio muted, nothing beyond the family; change it on the admin page or the panel when the danger is over). The hotspot has a new password, shown only on the front panel (knob → *Join Wi-Fi*); change this with `[wipe] wifi = "off"` or `"keep"`. The radio gets new random keys on its private channels if the mesh bridge is running — the panel tells you; if it says *Unplug the radio*, do that (the radio itself still holds the old keys and the list of radios it heard). Family members must be invited again. `sudo prepperctl wipe-status` shows what was done (kept in memory only, until the next restart).

**Duress secrets:** see *Duress* below — by default a duress secret **locks** instead of wiping.

**Dead-man switch** (off unless you turn it on): wipes by itself after N days (3–365) of the Badger *running* with no check-in. A check-in is any family member opening the Badger app on their device, or an admin login. Turning it on takes two steps (admin page over `https://`: days + admin PIN → a code → type the code back; or `sudo prepperctl deadman-set DAYS`, then `sudo prepperctl deadman-set --confirm CODE`). The admin page shows the hours left; in the last 48 hours the front panel warns on its home screen. Off: admin page or `sudo prepperctl deadman-off`. **Think hard before using it:** if everyone is away, ill or cut off for that long, the family data is gone.

**Why the order matters:** on flash storage (the NVMe and SD cards) deleting a file doesn't reliably erase it. The wipe first destroys the small key files that unlock the sealed records and the vault ("crypto-erase"), which takes well under a second, then cleans up the rest. A power cut half-way is finished at the next start (`prepper-wipe-resume.service`).

### Duress: when someone forces you to unlock the Badger

**People first.** If the person forcing you can check what you say (they know the family, have seen this Badger,
know what is on it), give the real PIN or passphrase. Use a duress secret only when they can't tell. Agree as a
family: whoever is forced gives whatever keeps people safest, and nobody blames them afterwards.
[SECURITY.md → Duress](SECURITY.md#duress-when-someone-forces-you-to-unlock-the-badger) has the details and the limits.

**Set up** (admin page over `https://` → Emergency → *If someone forces you to unlock the Badger*):

1. *What happens behind the made-up household*: **Lock** (default — nothing is erased; only the recovery keys
   reopen the family's records and the vault) or **Wipe** (the panic wipe; nothing to get back). Admin PIN to save.
   Lock needs sealed records set up (First-boot checklist, step 2).
2. *Duress PIN* (admin PIN + the new one), *Duress passphrase* (owner passphrase + the new one), and at the knob
   *System → Panel lock → Duress code* (after the panel code).
3. *The made-up household → Show and edit it*: check the names (the family's size is copied; type believable first
   names), the supplies and the board posts; *Make a new one* for a fresh random one. Ordinary things only.

**Under threat:** type the duress PIN at the admin login, the duress passphrase where the owner passphrase is
asked, or the duress code at the knob. Everything looks normal; every screen and family phone now shows the
made-up household. Lock mode changes nothing else — no restart, no Wi-Fi change.

**Afterwards (Lock), when everyone is safe:** admin page over `https://` → *Sealed records* → *Lock now* →
*Unlock* with the **recovery key** — or at the knob: *System → Recovery key*. The real records are back, and the
family board says "Recovered after a duress lock." Then open the *Document vault* with **its** recovery key
(typed where its passphrase goes). Keep both recovery keys away from the Badger: they are the one thing that ends
the lock.

## Everyday use

- **Join** the Wi-Fi (scan the QR on the front panel → *Join Wi-Fi*). Phones usually pop the launcher open by themselves. If not, go to **http://badger.lan**.
- **Search** covers every ZIM (full text), document file names, collections and tools.
- **Ask the Library** answers only from the Badger's articles and lists its sources. It's slow on battery (~1 min) and can be wrong, so check the sources.
- **Open with ▾** on any item picks another renderer. Tick *Remember* to make it your default on that device.
- The **Check-in board** has two tabs:
  - **Community:** open to everyone on the Badger's Wi-Fi, for neighbors and anyone nearby ("water at the church", "need a medic on Elm St"). Names aren't verified. *Send over mesh* uses the public channel, so any Meshtastic radio in range sees it.
  - **Family:** members only (see *Family members* below). Posts go out under the member's own name, and *Send over mesh* uses the family's private, encrypted channel.
  - Mesh messages are 200 bytes at most, including the name. Messages heard on each channel land on the matching board; direct messages to the Badger's radio are only read as commands, never posted.
  - Posts heard over the mesh never get the ✓ family mark: on the air, anyone can send with any radio's ID. On the family board they carry the member's name when their radio is registered (the family channel's key vouches for them); on the community board they show the name the radio gives itself.
  - The radio is slow and shared, so the Badger paces what it sends: family posts first, community posts at most 5 at once and then one every 2 minutes (extra ones wait, up to 20). Posts heard from one radio land on a board at most every 20 seconds.
  - Automatic notes from the Badger's own features (supplies running low, forecasts…) carry a grey **Badger** mark, never ✓.
- **Commands from Meshtastic radios** (one reply per radio every 30 s). You choose who may use each one: Settings → Badger administration → *Radio commands*, with each set to **Community** (anyone in radio range), **Family** (only messages sent on the family's private channel), **Off** (family radios are told it's off) or **Ignore** (no reply to anyone). Changes apply to the next message. The defaults are `!q` community and `!help`, `!ai` and `!status` family. Radios outside the family get **no reply at all** to anything they may not use (no help text, no "turned off"), and at most a few answers an hour ([MESH.md](MESH.md)). `!help` can never be opened to strangers: setting it to Off stores Family. The same settings can live in prepper.toml as `[radio] mesh_commands = { help = "family", q = "community", ai = "family", status = "family" }`; what you set in the launcher wins.
  - `!q <words>`: the best-matching library article, title plus a line.
  - `!ai <question>`: a two-sentence answer from the AI solver, grounded in the library, with its source. It takes about a minute; one question at a time, one per radio every 2 minutes (`[radio] mesh_ai_cooldown_s`). With `mesh_ai = false` it answers like `!q`.
  - `!status`: battery, power source, mode, active alerts and how many people are on the Wi-Fi.
  - `!help`: the list.
- **NOAA alerts over the mesh:** each new warning the SDR decodes is broadcast once to every radio on the channel (`[radio] mesh_alerts`). Tests aren't relayed. With other Badgers nearby they take turns, so usually one copy goes out per channel (see below and [MESH.md](MESH.md)).
- **Many Badgers on one mesh** ([MESH.md](MESH.md)): Badgers nearby take turns instead of all transmitting the same thing. Each NOAA alert is relayed once per channel for the whole mesh, a `!q` on a shared channel gets one answer, Badgers on the same NOAA transmitter post one forecast between them, and after a town-wide power cut alerts wait a random 30–120 s. Messages go out by priority (alerts, sensor danger, family posts, replies, community posts, automatic notes), with hourly budgets that shrink as more Badgers are heard, and automatic notes wait when the radio reports a busy channel. An alert is skipped only when this Badger *heard* exactly the text it would have sent; anything else (a claim, a bare tag, different words) never cancels it, because two copies are safe and silence is not. The launcher's status shows "Badgers nearby: N · channel busy x %". Settings: `[radio] mesh_courtesy` (on), `mesh_max_util`, `mesh_budget` and others in MESH.md.
- **Privacy and radio silence** ([MESH.md → Privacy on the mesh](MESH.md#privacy-on-the-mesh)): Badger-to-Badger control packets (beacons, claims, "done") go only on the family channel (and an optional private circle channel), never on the public one; they are sealed with that channel's own secret and padded to random lengths, and beacons come at random times. Nothing the Badger sends says "Badger". Anyone holding the family channel key can still recognise them, and strangers on the public channel can see that an automatic relay exists (set `mesh_alerts_public = false` to avoid that). The radio's own position is not shared on the public channel (`[radio] mesh_public_position = "off"`, or `"coarse"` / `"precise"`). **Radio silence** (receive only): `[radio] mesh_silent = true`, or create `/var/lib/prepper/portal/mesh-silent` (no restart; optional JSON `{"until": <epoch>}`) — the Badger then sends nothing on its own except, if `mesh_silent_allow_family = true`, family posts a member chose to send. Give the radio an unremarkable name, not "Honey Badger".
- **Radios on the map:** radios that share their position (Meshtastic app → Settings › Position) appear on the offline map: open *Maps* and tap *📡 Mesh radios*, or *Radios on the map* on the check-in board. Green means heard within the hour, amber within a day, grey older. Turn it off with `[radio] mesh_map = false`.
- **Two mesh channels.** In the Meshtastic app, keep the public channel (LongFast) as channel 0 and add a private family channel with its own key as channel 1. Share the family channel only with family radios, by QR code. Then set `[radio] mesh_public_channel = 0` and `mesh_family_channel = 1`; use `-1` for `mesh_public_channel` to keep the community board off the air. Family features stay off until `mesh_family_channel` is set; it can't be `0` (the radio's primary, public channel) or the same as the public one. An older config with only `mesh_channel` bridges that channel as the public one. Messages hop through any nearby Meshtastic radio with the same radio settings. On the family channel the content stays encrypted while it does. Turn off position sharing on the public channel so only family can see where your radios are.

### Family members

- **Invite:** Settings → Badger administration → *Family members* → *Invite a member*. The Badger shows a 10-character code (e.g. `K7QX-M2PD-9Z`) and a link. The code works **once**, on one device, for 24 hours. On their phone they join the Wi-Fi and open the link, or go to Check-in board → Family and type it.
- **Invite from the Badger itself:** knob → *Invite member (QR)* → *Yes*. The screen shows a QR code they scan on the Badger's Wi-Fi, with the code beside it. No phone or PIN needed: whoever holds the Badger can invite — unless you set a **panel code** (below). The screen has its own small allowance (5 open invites, 5 per 10 minutes), so it can never use up yours.
- **Panel code (strongly suggested):** the admin page shows *Front panel lock is off* until you set one. Tap *Set up panel code*, then within 10 minutes at the Badger: knob → *System* → *Panel lock* → *Set a code* (4–8 digits: turn to pick a digit, press to add it, then *OK*; then the same again). **Write down the 12-digit recovery code it shows** and keep it with your recovery card. From then on the knob asks for the code before *Invite*, *Approve device*, *Family radio*, *Posture*, *Services*, *Wi-Fi → Turn off*, *Import from USB*, *Restart* and *Shut down*. *Join Wi-Fi* and *Open app* stay open. Forgot the code? On the code screen choose *Forgot?* and enter the recovery code; or at the console run `sudo python3 /opt/prepper/software/panel/panel.py --reset-lock`.
- **Open invites** are listed under the members; *Cancel* (or *Cancel all open invites*) makes unused codes stop working.
- **Names:** the name you type when inviting is the one they get. Two members can't have names that look the same ("Ana", "ANA", "Аna" with a Cyrillic А…); add an initial.
- **What membership gives:** the family board and family radios on the offline map. The library, maps, tools and the community board are open to everyone anyway. `!ai` / `!status` are for radios on the family channel (whoever has its key).
- **More devices:** *Add a device* next to their name makes an invite for that person. The new device **waits for their approval**: the next time they open the Badger app on one of their own devices, it asks "A new device wants to join as you" — *Yes, it's mine* or *No, remove it*. Lost every device? Approve the new one at the Badger: knob → *Approve device*. When it is approved, the family board says "A new device was added to <name>". (So the admin PIN alone can't make someone a second "Mom".) Each device has its own key, kept by the browser in a cookie no web page can read, and the Badger stores just a hash of it.
- **Radios:** type their radio's ID (`!a1b2c3d4`, from the Meshtastic app) and tap *Add radio* — or they do it themselves: Settings → Family → *Link my radio*. The Badger shows a 6-character code; from that radio, **on the family channel**, send `!pair CODE` within an hour. When the Badger hears it, the radio is linked, its messages on the family channel carry their name, and the family board says so. An ID proves nothing by itself (any radio can claim it), so it never unlocks family commands or the ✓ mark.
- **The family radio link** (the channel key, for setting up a radio): family members find it on their own phone under Settings → Family → *Family radio link* (over `https://`), and it is always on the Badger's screen (knob → *Family radio*). The admin page doesn't show it.
- **If the member list is damaged** (a bad SD card, a restore gone wrong) the Badger refuses to change it instead of starting from empty, and the admin page offers *Restore the last good copy* (`members.json.bak`).
- **Remove someone:** *Remove* cuts off all their devices and radios immediately. Their old family posts stay, marked *removed member*, and messages from their radios are ignored. They can still use the library like any visitor. The family board notes the removal.
- **Removing also changes the family radio key** (you're asked; *OK* is the recommended answer): their radio still has the old key. **Every family radio then needs the new link** — family members open it on their phones (Settings → Family → *Family radio link*) or scan it on the Badger's screen, then share the channel QR in the Meshtastic app with the other family radios.
- **If a removed member knew the shared secrets,** change them under *Shared secrets*:
  - **New Wi-Fi password.** The Badger makes one and switches about 10 seconds later. Everyone, you included, rejoins with it. Over plain `http://` the admin page doesn't show it (anyone who knew the old password could read it off the air): read it on the Badger's own screen (knob → *Join Wi-Fi*, which shows the whole password and a QR code), or use `https://`. **Failsafe:** once the hotspot has switched, reopen the admin page and tap *Keep new password* within 10 minutes. If nobody does (the password was lost, or no device can get on), the Badger switches back to the old password by itself — even if it was restarted meanwhile (then a minute after it is back up). If that automatic switch-back can't be set up, the password isn't changed at all. Ethernet and SSH are never affected.
  - **New family radio key.** The Badger's radio gets a fresh random key on the family channel (removing a member does this too). The channel link (it contains the key) is shown to family members on their own phones over `https://` (Settings → Family → *Family radio link*), and always on the Badger's screen: knob → *Family radio* → *Channel link (QR)*. Scan it with the Meshtastic app to update your radio, then share the channel QR from the app with the other family radios. Radios that don't get it stop hearing family messages, and so does the removed member. A request the radio bridge didn't pick up within 2 minutes is dropped, never carried out later. **Failsafe:** the current link can be shown again any time, and the Meshtastic app can connect to the Badger's own radio over Bluetooth and show its channel QR, even if the Badger's software is down.
  - **The radio's Bluetooth PIN.** Many radios come with Bluetooth on and the PIN `123456` (or none): anyone nearby could pair with yours and read the family key. The bridge gives such a radio a random PIN (the radio restarts once), shown on the Badger's screen: knob → *Family radio* → *Bluetooth PIN*. Turn this off with `[radio] mesh_bt_pin = "leave"`; then change the PIN yourself in the Meshtastic app (Settings → Bluetooth → Fixed PIN).
  - **The admin PIN.** A removed member who knows it is still an admin. Change it at the Badger (`sudo prepperctl set-pin`), then tap *Log out all admin sessions*. Even so, the PIN alone can't read the family radio key, add a device to someone else's name without their yes, or link a radio without a message from it.
  - Until you change them, a removed member who knew them could still join the Wi-Fi as a visitor and read family-channel radio traffic.
- **Guessing codes doesn't work:** codes are about 49 bits of randomness, and 5 wrong tries from one device lock it out for 10 minutes.
- **Weather alerts** from NOAA Weather Radio appear as a red banner on every screen and on the front panel. The decoder (`alertd`) starts by itself when an SDR is plugged in. Set `[radio] weather_freq_mhz` to your nearest transmitter and `same_fips` to your county codes.
- **USB:** plug a stick into the front; its files appear under *USB & Files* for family members and the admin (visitors see nothing: a stick holds the family's own files). Eject from the panel (*USB: eject*) before pulling it.
- **Night vision:** Settings → Appearance → *Night vision (red)*.

## New features

Each has its own page on the launcher's home screen and its own guide. The overview, with who can use what, is in [FEATURES.md](FEATURES.md).

| Home screen group | Feature | Guide | Who |
|---|---|---|---|
| Family | 🥫 Supplies: pantry list, days of water and food, expiry and low-stock notes | [supplies.md](features/supplies.md) | family members |
| Family | 🔐 Document vault: encrypted IDs, insurance, medical papers, photos | [vault.md](features/vault.md) | family members + the family passphrase |
| Neighbourhood | 🔎 Missing & found: missing people, lost & found, photos, radio announcements | [missing.md](features/missing.md) | everyone; admin moderates and approves announcements |
| Neighbourhood | 🤝 Skills & resources: who can help, who needs help | [skills.md](features/skills.md) | everyone; admin moderates |
| Neighbourhood | ⛑️ Aid station: public status line, visit log | [aidstation.md](features/aidstation.md) | status: everyone · log: aid volunteers (codes from the admin) |
| Neighbourhood | 🌡️ Sensors: temperature, flood, water level, radiation alerts | [sensors.md](features/sensors.md) | everyone reads; admin changes alerts |
| Radio & sky | 📻 NOAA forecast as text | [forecast.md](features/forecast.md) | everyone; admin settings |
| Radio & sky | ✈️ Aircraft (SDR 2) | [aircraft.md](features/aircraft.md) | everyone; family members start scans; admin sets the antenna |
| Radio & sky | 🛰️ Weather satellites, Meteor-M (SDR 2) | [satellites.md](features/satellites.md) | everyone; admin sets the antenna and orbit data |
| More | 🎙️ Voice: ask out loud, hear the answer | [voice.md](features/voice.md) | everyone (admin can limit it to family) |

- **Settings:** each feature reads its own `[<name>]` section of `/etc/prepper/prepper.toml` (documented in `os/prepper.toml.example`). Turn one off with `[features] <name> = false`. Most have an admin box at the bottom of their page, and what you set there wins.
- **Data:** each feature keeps its files in `/var/lib/prepper/portal/features/<name>/`, owned by `prepper`.
- **Installers:** the speech software (forecast, Voice), the radio programs (aircraft, satellites) and the sensor firmware switches are installed by `os/install.d/speech.sh`, `sdr.sh` and `sensors.sh`. On an older Badger, run one again with `sudo bash /opt/prepper/os/install.d/<name>.sh` while it has internet.
- **Second radio:** SDR 2 needs its serial number set once at build time, and the Badger needs to know which antenna is on its jack. See [HARDWARE](HARDWARE.md#second-sdr-sdr-2).
- **Services:** Settings → Badger administration → *Services* lists `sdr` (aircraft and satellites) and `sensors` next to the others, with start/stop buttons.

## Times

Every time on the Badger can show as **Local**, **UTC** or **Nu:Time™**.

- **On a phone:** tap the 🕒 clock in the launcher's bottom bar to switch, or pick in Settings → *Show times as*. Each phone remembers its own choice.
- **Badger-wide default:** Settings → Badger administration, or `[time] display` in the config.
- **Front screen:** knob → Time format.

## Power

| Mode | Use when | What changes |
|---|---|---|
| **Endurance** | on battery, long outage | CPU powersave, small AI model, Kolibri/Jellyfin/Kavita stopped |
| **Balanced** | default | ondemand CPU, default model, Kolibri on if enabled |
| **Full** | on mains or solar surplus | performance CPU, everything on |

To switch, use Settings → Badger administration, or the panel → *Power mode*.

`powerd` changes modes automatically:

- On battery below 30 %, it switches to Endurance.
- When power returns, it switches back to your default mode.
- At 6 %, it shuts down cleanly. To start the Badger again, press the power button.
- After a reboot, `prepperctl power-restore` puts back the last mode you chose.

**Inputs:**

- USB-C PD (any 45 W+ charger or power bank)
- 8–30 V through the GX16 connector: a portable solar panel directly, a 12 V or 24 V battery, a vehicle socket, or a charge controller's load port

Both can be connected at once.

**Power settings** (`[power]` in the config):

| Key | Default | What it does |
|---|---|---|
| `pld_gpio` | `6` | the UPS's power-loss pin: 1 while external power is present |
| `pld_cmd` | `""` | empty = read `pld_gpio` with `gpioget` (libgpiod v1 or v2, detected). Or a command that prints 1 (external power) or 0 (battery) |
| `input_monitor` | `"none"` | the optional meter on the solar/DC input: `"ina228"` (85 V, recommended), `"ina226"` (36 V), `"ina219"` (26 V: only if your input never goes above it), or `"none"` |

## Updates

**Over your home Wi-Fi.**

- Settings → Badger administration → *Join a Wi-Fi network for updates*, or set `[network] uplink_wifi` in the config.
- The Badger's built-in radio joins your network while the USB adapter keeps the hotspot running, so **everyone can keep using it while it updates**.
- Downloads run in the background at low priority and swap in only when complete and verified. The one item being replaced pauses for about a second.
- With no USB adapter, the single radio can't do both at once, so use Ethernet.

**Automatic.**

- Checks run daily and downloads run weekly, whenever there's an internet link (Ethernet, `uplink_wifi`, or phone USB tethering).
- The weekly download honours `[upd] auto_apply` (`false` skips it) and `window` (for example `"01:00-06:00"`; empty = any time).
- Other Badgers listed in `[upd] peers` are tried first.
- Status → Updates shows each item's old → new version, the transfer method, its size and notes like "+1,204 articles".

**Manual.**

- From Settings → *Check for updates* / *Download updates*, or the panel → *Updates*.
- *Download now* ignores the update window.
- From the shell: `sudo python3 /opt/prepper/software/upd/upd.py check` (or `apply --now`).

**Sneakernet (no internet anywhere).**

1. On a Badger that has the new content: Settings → *Export* to `/media/usb-sda1/prepper-bundle`, or panel → *Export to USB*.
2. On the other Badger, plug in the stick and choose *Import* (or panel → *Import from USB*).
3. Bundles are signed. The receiving Badger must trust the sender: `sudo prepperctl trust "ssh-ed25519 AAAA… sender"`.
4. Once a Badger has seen a bundle from a disk, it refuses older bundles from that same disk, so an old stick can't roll it back.

**ZIM diffs between Badgers.**

1. On the hub Badger, set `keep_previous = true`.
2. After an update, run `upd export /media/usb… --diffs 2026-08`. This writes OLD→NEW `.zimdiff` patches.
3. Badgers that still have the old version apply them with `zimpatch` instead of copying the whole file.

**Software ("firmware") updates.**

- The `prepper_software` catalog item tracks your fork's `stable` branch.
- It installs only if the commit is signed by a maintainer key in `/etc/prepper/git_allowed_signers`.
- Add the key with `sudo prepperctl trust-maintainer "ssh-ed25519 AAAA… name"`. Until you do, software updates are not installed.
- This file is separate from `/etc/prepper/allowed_signers`, which only trusts bundles and peers.

## Wi-Fi security

`[network] security` (the builder's `--wifi-security`):

| Value | What it is |
|---|---|
| `auto` / `wpa2` | A password (random by default; WPA2, plus WPA3 on the USB adapter). |
| `owe` | No password, but each phone's traffic is encrypted (Enhanced Open). **OWE only: phones older than about 2019 (before iOS 13 / Android 10) cannot join.** The onboard radio can't do OWE and runs open. |
| `owe-transition` | **Experimental, USB adapter only, not yet tested on hardware.** Two networks on the one adapter: an open one under your Wi-Fi name (older phones join it, unencrypted) and a hidden OWE twin that newer phones use automatically. `prepperctl net-up` bridges them (`br-prepper`), so both share the Badger's address, DHCP and the launcher popup, and phones can't reach each other. If it misbehaves, set `owe`. The launcher's Status counts only phones on the open twin. |
| `open` | No password, no encryption: anyone in range reads everything. |

With no password the vault and other family secrets only work over `https://`.

## Phones, iPads, laptops

Nothing is installed on them, and nothing is stored on them unless the user chooses. See the launcher's *Phones, iPads & laptops* page.

- **iPad / iPhone:**
  1. In Safari, Share → *Add to Home Screen*.
  2. Optional: for offline tools, install `http://badger.lan/ca.crt`, trust it under Settings → General → About → Certificate Trust Settings, then use `https://badger.lan`.
- **Android:** Chrome → *Install app*. Kiwix, Organic Maps, OsmAnd, Trail Sense and Meshtastic APKs are under Tools.
- **Laptop:** any browser. For a laptop that must work away from the Badger, run the go-bag edition:
  1. Copy the repo.
  2. Set `[portal] standalone = true` in its config, with `[upd] tier = "pocket"`, `budget_gb = 64` and `peers = ["http://badger.lan/sync"]`.
  3. Run `upd apply`, then `server.py`.
  4. Optionally pin extra items with `pins = ["wikipedia_en_all_nopic"]`.

## Adding your own content

- **Manual catalog items** (Hesperian books, NPS and USGS maps, local repeaters, openFDA labels, satellite imagery): copy the files to `/srv/prepper/<dir>/<id>/`. The item's description names the folder. Then register them:
  `sudo python3 /opt/prepper/software/upd/upd.py adopt hesperian_books`
  The launcher lists them, and peers and bundles share them like any other item.
- **Any other ZIM:** add an `[[item]]` with `source = { type = "kiwix", name = "<file stem without date>", path = "<mirror dir>/" }`, its `license`, and `use = "any"` (open licence) or `use = "personal"` (non-commercial or unclear), then run `tools/gen_contents.py`. An item without `use` counts as personal-use only: a Badger built for sale won't fetch it until the owner accepts.
- **Family papers:** use the **Document vault** on the launcher (Family → 🔐 Document vault). It encrypts everything on the Badger's disk and makes encrypted backups; see [vault.md](features/vault.md). Keep the passphrase in your head and the recovery key on paper, not on the Badger.

## Maintenance

- **Monthly:** run on battery for 10 minutes to exercise the cells. Check Status for errors.
- **Every 6–12 months, if stored:** charge to 50–60 %, power on, let updates run, check the gasket.
- **Spares to keep in a Faraday bag:**
  - the rescue microSD (boots the same OS; content stays on the NVMe)
  - a USB bundle of the priority-1 and priority-2 items
  - a spare Pi 5 if you can afford one
- **Logs:**

  ```bash
  journalctl -u upd-apply -u prepper-firstpull -u prepper-portal -u kiwix-serve -u prepper-powerd -b
  journalctl -u prepper-alertd -u prepper-sdr -u prepper-forecast -u prepper-sensord -b   # radios, forecast, sensors
  cat /var/log/prepper-firstboot.log
  ```

- **Admin PIN or passphrase:** `sudo prepperctl set-pin`, then type it twice at the prompt (or `sudo prepperctl set-pin < file`; never as an argument — that would show in `ps`). A passphrase of 10+ characters (a few words) is best; a PIN of 6+ digits still works but you're warned: someone who copies the disk can guess digits quickly. It is stored as an scrypt hash; an older PIN hash is upgraded by itself at your next login. Repeated wrong PINs are rate-limited. Afterwards, *Log out all admin sessions* (Settings → Badger administration) ends sessions opened with the old one.
- **Security notes** (what is protected how, and what isn't): [SECURITY.md](SECURITY.md).
- **Wi-Fi name or password:** edit `/etc/prepper/prepper.toml`, then run `sudo prepperctl apply-config`. The USB adapter's hotspot uses WPA2/WPA3; the built-in radio's (wlan0) is WPA2 only, because some phones fail to join it otherwise.

### Files and users

| Path | Owner | What |
|---|---|---|
| `/etc/prepper/prepper.toml` | root | the one config file |
| `/etc/prepper/allowed_signers` | root | keys trusted for bundles and peers (`prepperctl trust`) |
| `/etc/prepper/git_allowed_signers` | root | keys trusted for software updates (`prepperctl trust-maintainer`) |
| `/var/lib/prepper` | root (only root writes here) | build status, update state, power mode |
| `/var/lib/prepper/portal` | prepper (the launcher) | `portal-settings.json`, `board.jsonl`, `family-board.jsonl`, `members.json` (+ `.bak`), `mesh-outbox.jsonl`, `mesh-peers.json` (Badgers nearby + mesh courtesy status), `mesh-silent` (radio-silence flag), `mesh-telemetry.jsonl`, `mesh-channel.json` (the radio key, 0600) |
| `/var/lib/prepper/portal/features/<name>` | prepper | each feature's own data (the vault's is encrypted; supplies and the aid-station log are not) |
| `/etc/prepper/panel.key` | root, 0600 | the front panel's key (proves to the launcher that a request comes from the Badger's own screen) |
| `/etc/prepper/panel.key.sha256` | root:prepper, 0640 | its hash, which the launcher checks against |
| `/etc/prepper/panel-lock.json` | root, 0600 | the optional front-panel code (salted hashes + wrong-try counter); absent = no code |
| `/var/lib/prepper/portal/panel-lock-arm.json` | prepper | written by *Set up panel code*: a code may be set at the knob for 10 minutes |
| `/var/lib/prepper/portal/mesh-pair.json` | prepper (written by the mesh bridge) | `!pair CODE` messages heard on the family channel, for linking radios |
| `/var/lib/prepper/wipe-pending.json` | root | only while a wipe runs (or waits for its delayed part): lets `wipe-resume` finish it after a power cut; no secrets |
| `/var/lib/prepper/deadman-state.json` | root, 0644 | only while the dead-man switch is armed: running time without a check-in |
| `/var/lib/prepper/portal/checkin.json` | prepper | only while the dead-man switch is armed: a check-in counter (no times, no names) |
| `/run/prepper/wipe-receipt.json` | root, 0600, RAM | what the last wipe did; gone at power-off |
| `/run/prepper/family-inbox.jsonl` | prepper, 0600, RAM | family radio messages the mesh bridge heard, for a few seconds until the launcher seals them onto the family board; a reboot drops lines not yet taken in |
| `/var/lib/kolibri` | kolibri | Courses data |
| `/srv/prepper` | — | the content |

`kiwix-serve`, `llama-server` and Kolibri run as their own users (kiwix, llama, kolibri), so a bug in one can't reach the others' files. All of them, and the launcher API, listen on 127.0.0.1 only; phones reach them through nginx.

**Hotspot firewall** (`prepperctl net-up`): from the Wi-Fi hotspot only DNS (53), DHCP (67), time (123/udp), nginx (80, 443, the apps origin 8088/8443, Kolibri's HTML5-app origin 8089) and ping get through; everything else, SSH included, is dropped. Ethernet and the uplink are not filtered. nginx also caps each phone at 20 open `/api/` requests and 10 POSTs a second (burst 20); over that it answers 429.

**Crash restarts:** nginx, dnsmasq, chrony and gpsd get a drop-in (`/etc/systemd/system/<unit>.d/prepper-restart.conf`) that restarts them 2 s after a crash; nginx and the launcher API have no start limit, so repeated restarts never leave them failed.

`/etc/hosts`: `render.py` manages only the block between `# BEGIN prepper` and `# END prepper`; lines you add elsewhere are kept.

Launcher settings (`[portal]`): `settings_file`, `board_file` and `mesh_outbox` point into `/var/lib/prepper/portal/`. `llama = "http://127.0.0.1:8090/ai"` is the AI server's address.

### prepperctl

`sudo prepperctl <verb>`. The ones you'll use most:

| Verb | What it does |
|---|---|
| `apply-config` | apply edits to `prepper.toml` |
| `set-pin` | set the admin PIN or passphrase (typed at the prompt, or on stdin) |
| `power-mode endurance\|balanced\|full` | change the power mode |
| `update-check`, `update-apply` | check for or download updates now |
| `trust "<key>"` | trust another Badger's bundles and peer data |
| `trust-maintainer "<key>"` | trust a maintainer's key for software updates |
| `wifi-on`, `wifi-off` | turn the hotspot on or off |
| `panic-wipe` / `panic-wipe --confirm CODE` | erase the family's data (keys first), keep the library — see [Panic wipe](#panic-wipe-duress-pin-dead-man-switch) |
| `handover-reset` / `handover-reset --confirm CODE [--sale]` | panic wipe + factory settings, before selling or giving it away |
| `set-duress` (`--clear`) | set (or remove) the duress PIN, typed at the prompt |
| `deadman-set DAYS`, `deadman-set --confirm CODE`, `deadman-off`, `deadman-status` | the dead-man switch |
| `wipe-status` | what the last wipe did (since the last restart) |

The system uses these itself: `wipe-finish` (the delayed part of a duress wipe), `wipe-resume` (at boot: finishes a wipe a power cut interrupted), `deadman-check` (hourly while the dead-man switch is armed), `power-restore` (reapplies the power mode at boot), `finish-provisioning` (last step of the first content pull), `auto-apply-enabled` (lets the weekly timer skip when `auto_apply = false`).

## Troubleshooting

| Symptom | Check |
|---|---|
| No Wi-Fi network (the name you chose) | Panel → *Wi-Fi → Turn on*. If you have no USB adapter and the first download is still running on a Wi-Fi uplink, the access point is deferred on purpose, for at most `ap_defer_hours` (default 12) |
| Launcher loads but Wikipedia says "not downloaded" | Status → Building; the largest items arrive last |
| "Open with" shows only *Download* | `kiwix-serve` is down: Settings → Services → kiwix — start |
| Ask the Library says the model isn't running | Settings → Services → llama — start, or check that the model item is installed |
| No weather alerts | Check SDR 1 is plugged in (`alertd` starts when it is) and the whip on the **SDR** jack is unfolded. An RTL-SDR Blog V4 may need the rtl-sdr-blog driver build. If the radio pages warn that the serials aren't set, set them (HARDWARE → Second SDR) |
| Software update not installed | Add the maintainer key: `sudo prepperctl trust-maintainer "<key>"` |
| Clock wrong after storage | Fit the ML-2020 RTC cell, set `rtc_battery = "ml2020"`, or plug in the GPS |
| Update says "needs X GB incl. reserve" | Free space, lower `reserve_gb`, or move to a bigger drive |
| No aircraft | Check the antenna on the **SDR 2** jack (the 1090 MHz whip, for aircraft all day) and the warning box at the top of the Aircraft page: it says if SDR 2 is missing or failing, if the satellite dipole is set (then aircraft are on demand: tap *Scan aircraft*), or if the decoder isn't installed. An admin sets the antenna under *SDR 2 antenna*. Indoors, range drops a lot: try a window |
| Forecast says "no clear signal" | The weather radio heard static or silence. Unfold the whip on the **SDR** jack (SDR 1, not SDR 2) and check `[radio] weather_freq_mhz` is your nearest NOAA transmitter (Tools → Radio) |
| Forecast or Voice says speech recognition isn't installed | The speech installer only downloads files whose pinned SHA-256 checksums are filled in `os/install.d/speech.sh`. Fill them (the script says how), then run `sudo bash /opt/prepper/os/install.d/speech.sh` while the Badger has internet |
| Vault won't unlock | Use the family passphrase, or *Forgot the passphrase?* with the recovery key. After 5 wrong tries the device waits 10–15 minutes. If both are lost, **nobody can recover the vault**, not the admin and not the builder: restore the paper originals |
| Sensors show nothing | Check the wiring against [sensors.md](features/sensors.md) (3.3 V, pull-ups, I²C addresses), reboot once after adding 1-Wire probes, and list float switches, water-level sensors and Geiger counters in `[sensors]`. Then Settings → Badger administration → *Services* → `sensors` (start it), and see *Sensor settings → Sensors* on the Sensors page for each sensor's status |
