# Security notes

What protects what on the Honey Badger, and what it doesn't protect. Families use it in an
emergency, so where we had to choose, **nothing may lock the family out**. Every safeguard below
has a way back.

## Who can do what

| Who | How they prove it | What they get |
|---|---|---|
| Anyone on the Wi-Fi | nothing | library, maps, tools, alerts, community board (radio only with a member's OK), the Badger's area (a ≥10 km grid cell) — no power, occupancy, USB drives or sensor readings |
| Family member | a device key from a one-time invite (HttpOnly cookie) | family board, family radios on the map, precise position, the family radio link (over https), approving new devices added to their own name |
| Admin | the admin PIN or passphrase → a 12-hour session (HttpOnly cookie) | settings, new members, removing members, Wi-Fi/radio key changes — **not** the family radio key, and not someone else's identity (see below) |
| The front panel | a root-only key (`/etc/prepper/panel.key`); optionally a panel code at the knob | invites, approving devices, the family radio link, precise position |
| Aid lead / aid volunteer | a one-time aid code → a device key | lead: the whole aid log; volunteer: their own entries + today's counts |
| Radios on the family channel | the channel's private key | family board, family commands (`!ai`, `!status`) |
| Any radio | nothing | community board, community commands |

## The front panel's key

The launcher (user `prepper`) gives the front panel a few things nobody on the Wi-Fi gets — above
all, family invites. It used to recognise the panel as "a request from loopback that didn't come
through nginx". Any local process, or anything that can make a service fetch a `localhost` URL,
looked the same.

Now the panel (which runs as root: it drives GPIO and calls `prepperctl` directly) makes a random
key at start, `/etc/prepper/panel.key` (root, 0600), and its SHA-256 in
`/etc/prepper/panel.key.sha256` (root:prepper, 0640). It sends the key in the `X-Badger-Panel`
header; the launcher compares the hash. The launcher can check the key but can't produce it, and
neither can any other service account (kiwix, llama, kolibri, the mesh bridge). nginx clears the
header, so phones can't send it, and a request carrying `Origin` or a cross-site `Sec-Fetch-Site`
(a browser page) is never "the panel". The feature framework's `"local"` access level uses the
same check (`Handler.local_console`).

Without the hash file, a portal behind nginx (a real Badger) refuses all panel requests. Only a
**standalone** portal (development, the go-bag laptop, which has no front panel) trusts loopback
alone.

Not covered: root, and anyone who can read root's files, can be the panel. That is fine: root can
do everything anyway.

## Secrets and plain HTTP

Most families use `http://badger.lan`: a self-signed certificate on phones is awkward (iOS needs a
profile install and a trust toggle). Plain HTTP on the hotspot can be read by **anyone who knows
the Wi-Fi password**: they can decrypt WPA2-PSK traffic they capture. That includes the person you
just removed, which is exactly when you change the Wi-Fi password and the family radio key.

So **new secrets are never sent over plain HTTP**:

- the new Wi-Fi password: read it on the Badger's screen (knob → *Join Wi-Fi*, the whole password
  and a QR code);
- the family channel link (contains the channel key) and the radio's Bluetooth PIN: knob →
  *Family radio*.

Over `https://` (`[network] https = true` and `sudo prepperctl gen-tls`; the admin page links to it)
the admin page shows the new Wi-Fi password too, and **family members** see the family radio link
on their own device (Settings → Family). The admin page never shows the family radio link (see
"The admin PIN is not a master key"). nginx tells the launcher which one a request used
(`X-Forwarded-Proto`). Requests made on the Badger itself count as safe.

The **document vault** goes further: on a hotspot without a password (`security = "owe"` or
`"open"`) it refuses plain HTTP altogether — no passphrase, no upload, no document — and says to use
`https://badger.lan` or set a Wi-Fi password. On a WPA2 hotspot it works over HTTP with a warning.

Still sent over plain HTTP (accepted trade-offs, so phones without the certificate keep working):

- the admin PIN at login and the session cookie;
- invite codes and the device key handed out at join (valid once / for that device).

Someone sniffing the hotspot with the Wi-Fi password could catch these. Use HTTPS for admin work if
you can. Change the Wi-Fi password when someone leaves the family: that ends their sniffing.

The USB adapter's hotspot also offers WPA3 (SAE), which can't be decrypted passively even with
the password. The onboard radio (wlan0) only does WPA2.

## The Wi-Fi

- **A password by default.** The builder (and a hand-run install) gives the hotspot a random WPA2
  password (12 characters, ~59 bits: offline cracking of a captured handshake is the attack it must
  stand). It is printed at the end of the build and always shown on the front panel (*Join Wi-Fi*).
- **A neutral name.** `prepper-build` refuses Wi-Fi names with "Badger"/"prepper" in them and picks
  one like `Home-K7QX`: a wardriver's list of SSIDs must not be a list of prepared households.
- **No password, still encrypted:** `[network] security = "owe"` (Enhanced Open). Each phone gets its
  own key, so nobody in range reads anyone's traffic. Phones from about 2019 on (iOS 13+, Android 10+)
  join; older ones can't (OWE-only). The onboard radio can't do OWE and runs open instead.
  **`security = "owe-transition"` (EXPERIMENTAL, USB adapter only, untested on hardware)** adds an
  open twin network under the real name for older phones plus a hidden OWE twin that newer phones
  are steered to; both are bridged (see OPERATIONS.md → Wi-Fi security). A phone on the open twin
  is not encrypted.
- **Open** (`security = "open"`): no password, no encryption — must be asked for by name. The vault
  then works only over https.
- **Client isolation** (`ap_isolate=1`): phones on the hotspot reach only the Badger, never each other
  (no ARP spoofing or port scans of the family's phones by a visitor).

## What visitors can learn

One rule (`software/common/visitor.py`) for every public answer — "miles is fine, it can't be
100 yards NNW":

- The Badger's position is **snapped to a fixed grid**: 0.1° of latitude (11 km) by enough 0.1°
  steps of longitude to make the cell at least 10 km wide. Deterministic, never random noise (noise
  averages out over repeated requests and converges on the house). Everything a visitor sees —
  `/api/status`, `/api/time` (location, solar time, sun times **to the minute**), the aircraft page
  (origin, distances, bearings, even which far-away planes are dropped), satellite pass predictions —
  is computed **only from the snapped point**. Two houses in the same cell give identical answers;
  `tests/test_visitor_privacy.py` checks that, and that trilaterating the aircraft list finds the cell
  centre.
- No battery/mains/solar state, runtime, number of connected devices, internet uplink or uptime.
- No USB drives (members and the admin browse them in the launcher; nginx serves the bytes only after
  the portal said yes), no sensor readings or history unless `[sensors] public = true` (alerts are
  always public), no name of who started an aircraft scan.
- A visitor's *Send over mesh* on the community board waits until a family member or the admin sends
  it (Check-in board): the Badger's radio is the household's transmitter. `[portal] visitor_mesh = "off"`
  stops the requests altogether.
- Family members, the admin and the front panel see everything precisely.

## Sign-ins and the other apps on badger.lan

The launcher shares its origin (`badger.lan`) with other web apps: the AI's own page (`/ai/`),
courses (`/learn/`), books (`/books/`), video (`/video/`) and the kiwix reader (`/kiwix/`). Any
script on an origin can read that origin's `localStorage` and `sessionStorage`, and that is where
the member key and admin token used to live.

Now:

1. **The member key and the admin session are HttpOnly cookies**
   (`SameSite=Strict; Path=/api/`). No script can read them, on any page. The launcher keeps only
   non-secret hints ("this device is a member") in web storage. An older launcher's
   `localStorage` key is handed to the Badger once (`POST /api/members/adopt`) and then deleted.
   It is deleted only if that worked, or if the Badger says the key is unknown. A Badger that is
   unreachable, or a member list that can't be read, never makes a device forget its key.
   `X-Member-Token` / `X-Admin-Token` headers still work (tools, tests, feature plugins).
   The cookies are not marked `Secure`: on plain HTTP a `Secure` cookie would silently disappear.
2. **Those apps get a Content-Security-Policy** (`os/render.py`, `app_csp`). Their scripts may
   only `fetch`/XHR/WebSocket, frame and run workers within their own path (`/ai/…`). They can't
   call `/api/` with the family's cookies, or frame the launcher to drive it.
   The web apps under `/apps/` (pdf.js, foliate-js, Kiwix JS — code fetched from upstream or a peer)
   run on **their own origin**: port 8088 (8443 over https). The launcher's `/apps/…` links redirect
   there; that origin serves only the apps and the library (`/content/`), with a policy that lets them
   fetch from it alone. Kiwix JS is pinned to a release tag **and** its commit (`content/catalog.toml`).
   Untrusted files (USB, content, peer sync, ZIM articles) were already sandboxed.
3. **CSRF:** every POST must be `application/json` (or carry `X-Badger-Upload` for uploads), and is
   refused when the browser says it is cross-site or the `Origin` doesn't match. The cookies are
   `SameSite=Strict`, and CORS never grants credentials.

**Left open:** a hostile script inside `/ai/`, `/learn/`, `/books/`, `/video/` or `/kiwix/` could
still open the launcher in a new window (`window.open`) and drive it while someone is signed in.
Same-origin windows can script each other, and only a separate origin stops that (as `/apps/` now
has). These services are installed packages, not code pulled from git or a peer; the CSP and
HttpOnly cookies stop stealing a token for later use and silent API calls from their pages.

## The local certificate

`prepperctl gen-tls` makes a local CA for `https://badger.lan`. Families install it on their phones,
so it must never be able to vouch for anything else:

- **nameConstraints** (critical): only names under `.lan` / `.local` and the hotspot's own /24 — a
  copy of the disk can't mint a certificate for a bank, mail or meshtastic.org that phones would trust;
- it expires with the server certificate (830 days, the server one 825 — the iOS maximum);
- **its private key is destroyed** right after signing the server certificate. Running `gen-tls` again
  (new address, expired certificate) therefore makes a **new CA**: install the new `/ca.crt` on each
  phone and remove the old profile.

## Peer and bundle trust

Other Badgers (`/sync/`) and USB bundles must be signed by a key in `/etc/prepper/allowed_signers`,
and trust is scoped by what an item can do (`software/upd/upd.py`):

- `prepper-update …` (what `prepperctl trust KEY` writes): **content** — ZIMs, maps, documents, datasets;
- `prepper-code …`: also **code** — web apps, Android APKs, AI models. `sudo prepperctl trust --code KEY`
  writes it. Use it only for a Badger you control.
- A catalog `sha256` pin is always checked; an item pinned to a git `commit` (Kiwix JS) never comes
  from a peer at all — only from upstream, checked against the pin (and `transfer.fsckObjects`).
- `require_signed_peers = false` means "take whatever is plugged in", unsigned code included.

This Badger's own signing key (`prepperctl gen-key`) is encrypted with a random passphrase kept in a
separate root-only file (`disk_ed25519.pass`, handed to ssh-keygen through `SSH_ASKPASS`): the key
file alone, copied anywhere, signs nothing. Someone with the whole disk has both — which is why it
is listed for content only.

## The mesh

- **A sender ID proves nothing.** Meshtastic's `fromId` isn't authenticated on channel traffic, so
  anyone can transmit as "your mother's radio". The only proof the mesh offers is the family
  channel's private key, so "family" means "arrived on the family channel":
  - family commands (`!ai`, `!status`) answer only on the family channel;
  - posts heard on the air are never marked ✓ (verified);
  - member names are attached only to posts that arrived on the family channel;
  - direct messages to the Badger are commands only, never board posts.

  A newer Meshtastic firmware's PKI direct messages could prove the sender. The library doesn't
  expose that reliably yet, so they get no extra trust.
- **Channels fail closed:** family features are off until `[radio] mesh_family_channel` is set.
  Channel 0 (the radio's primary, usually public channel) is never the family's. "Family = public"
  switches family off. A disabled public channel (`-1`) means community posts are dropped, never
  sent on the family channel.
- **Airtime:** community posts are paced (5 at once, then one per 2 minutes, at most 20 waiting),
  family posts go first. Posts heard from one radio land at most every 20 s (30 per 10 min per
  board). Commands have per-radio cool-downs, plus an overall budget of 20 replies per 10 minutes
  to radios outside the family, so forged IDs can't turn the Badger into an amplifier. Slow
  answers run off the radio's receive thread.
- **Key changes:** a request older than 2 minutes is ignored; one is never carried out twice,
  even if its file can't be deleted. The channel link (it holds the key) is written 0600 and kept
  current, even if a change was interrupted.
- **Bluetooth:** a radio with Bluetooth on and the default PIN `123456` (or no PIN) lets anyone
  nearby read every channel key. The bridge gives it a random PIN, shown on the front panel. Turn
  this off with `[radio] mesh_bt_pin = "leave"`.

## Wi-Fi password changes can't lock you out

`prepperctl wifi-rotate` (from the admin page):

- saves the old password (root-only), writes the new one, and only then records the change as
  pending;
- schedules the switch (10 s) and the automatic **switch-back** (10 min). If the switch-back can't
  be scheduled, it changes nothing;
- the switch-back timer is re-armed at every boot (`prepperctl wifi-rearm`, from
  `prepper-net.service`). Past its deadline, it switches back a minute after boot;
- *Keep new password* is refused until the hotspot has actually switched;
- a revert or apply started from the launcher runs as its own systemd unit, so the launcher's
  restart can't kill it half-way;
- Wi-Fi verbs take a lock, so two can't interleave;
- a damaged record of the old password never opens the network: the switch-back refuses, and the
  new password stays;
- the front panel always shows the current password; Ethernet and SSH are never affected.

## The admin PIN is not a master key

The admin PIN runs the Badger. It must not let one person — a rogue or coerced admin, or someone who
cracked the PIN from a copied disk — become a family member, speak as one, or take the family radio.

- **A device for an existing member needs that member's yes.** An admin invite with a member's id
  makes a *pending* device: it can sign in but sees nothing until one of that member's own devices
  approves it (a question appears in their launcher), or someone approves it at the front panel
  (knob → *Approve device*, behind the panel code if one is set). Then the family board gets a note
  from the Badger: "A new device was added to Ana (tablet)". A refused device is deleted. Unapproved
  devices disappear after 24 h. New members (new names) still join on the admin PIN alone, as before.
  *Way back:* a member who lost every device gets a new one approved at the front panel.
- **A radio is linked only after it proves itself.** Adding a radio (admin page, or a member for
  their own radio) gives a 6-character code that works for an hour. The radio must send
  `!pair CODE` on the family channel; the mesh bridge notes it (`mesh-pair.json`) and the launcher
  links the radio. The family board says "The radio !a1b2c3d4 was linked to Dad". Until then the
  bridge doesn't know the radio at all. (Anyone holding the family channel key can still claim
  any radio ID on the air — that is why removing a member changes the key, below.)
- **The family radio key goes to family members only:** on their own device over https, and on
  the front panel. The admin page never gets it, http or https. A new key from *New family radio
  key* is likewise only shown to members and on the panel.
- **The PIN itself:** a passphrase is allowed (10+ characters; 6+ digits still works, with a
  warning). It is stored as **scrypt** (N=2^15, r=8), which is slow even on a GPU, in
  `prepper.toml` (root:prepper 0640). Older `pbkdf2$` hashes keep working; at the next good login
  the launcher has `prepperctl upgrade-pin` re-hash the same secret with scrypt (it checks the PIN
  against the current hash itself, so it can never set a different PIN). `prepperctl set-pin`
  reads the secret from the prompt or stdin only — never from its arguments, which every user can
  see in `ps` — and hands it to the hasher on stdin too.
- **Transparency:** removing or renaming a member also leaves a note on the family board.

## The front panel lock

Anyone who can turn the knob used to get a family invite, the family radio key (QR), the radio's
Bluetooth PIN, *Services → Alerts off*, Wi-Fi off, USB import and shutdown behind a yes/no.

- **Optional panel code**, 4–8 digits, entered with the knob (turn to pick a digit, press to add it,
  then *OK*). **Off until the owner sets one**, so nothing changes by itself; the admin page shows a
  strong suggestion while it's off.
- Guards *Invite member*, *Approve device*, *Family radio*, *Services*, *Wi-Fi → Turn off*,
  *Import from USB*, *Restart* / *Shut down* (and hold-to-shut-down). *Join Wi-Fi* and *Open app*
  stay open — the way onto the network. `[panel] lock_protect` changes the list (also possible:
  `wifi_join`, `app`, `power`, `updates`, `usb_export`, `eject`, `time`, `status`).
- After the right code the panel stays open for 5 minutes or until the screen sleeps.
- Wrong codes: 3 free tries, then 30 s, doubling up to an hour; kept in the lock file, so a restart
  doesn't reset it.
- Stored as salted scrypt hashes in `/etc/prepper/panel-lock.json` (root, 0600). The launcher only
  sees whether the file exists. A damaged file locks (fail closed).
- **Setting a code needs the admin too:** the panel offers *Set a code* only within 10 minutes after
  the admin page's *Set up panel code*. A visitor alone at the knob can't lock the family out.
  Changing or turning off the code needs the code.
- **Ways back:** (1) the 12-digit **recovery code** shown when the code is set — write it on the
  recovery card; on the code screen choose *Forgot?*. (2) At the console (keyboard + screen, or
  SSH as an admin): `sudo python3 /opt/prepper/software/panel/panel.py --reset-lock`. Holding the
  knob down while the Badger starts opens the recovery-code screen directly.

## The member list

`members.json` is written atomically, with a copy of the last good version in
`members.json.bak`. A file that can't be read or parsed is **never** treated as "no members": the
launcher refuses to change it, and the admin page offers *Restore the last good copy*. Names are
cleaned of invisible and direction-changing characters, and two members can't have names that look
the same. Wrong invite codes lock the guessing address out for 10 minutes; a spray of failures
from many addresses can't clear that lock. Open invite codes are stored as a salted scrypt hash
(one hash per guess, ~40 ms), so a copy of the file can't be brute-forced in the 24 h a code lives;
aid-station codes the same way.

## The aid-station log

Names and health needs of neighbours. A volunteer sees, edits and exports only the records they
wrote, plus today's station-wide counts (no names). The admin and **aid leads** (a volunteer the
admin marks as lead, or a lead code) see everything. Every export and every role change is in the
audit list on the admin page.

## A removed member

Removing someone ends their device keys at once, and their radio IDs stop naming posts. Things
they may still know:

- **The family radio key:** their radio still has it, and with it they can post on the family
  channel claiming any family radio's ID. So *Remove* also gives the family channel a **new key**
  (on by default; the admin can say no). It uses the same request and failsafes as *New family
  radio key*. Every family radio then needs the new link: family members open it on their phones
  (Settings → Family → *Family radio link*) or scan it on the front panel (knob → *Family radio*).
- **The Wi-Fi password:** change it (above).
- **The admin PIN:** change it at the Badger (`sudo prepperctl set-pin`), then *Log out all admin
  sessions* (Settings → Badger administration).

## Disk

- **Logs:** nginx keeps no access log (visitor addresses, phone models, one-time download links) and
  only critical errors; the systemd journal is in RAM only (`Storage=volatile`, gone at power-off);
  DHCP leases with phone names live in `/run`, and dnsmasq logs no per-phone lines. A 500 error never
  returns exception text (it is in the journal for the admin).
- **Boot order:** NVMe, then SD — no USB boot (`BOOT_ORDER=0xf16`): nobody with the box in hand boots
  their own OS from a stick. (Without disk encryption an SD card or a pulled NVMe still reads.)
- The installer turns off swap on the NVMe (`dphys-swapfile`, if present). The document vault holds
  decrypted pieces in the launcher's memory, and those must never be paged to disk. Swap in RAM
  (zram) is fine. A swap file set up some other way (e.g. a `/swapfile` in `/etc/fstab`) should be
  removed by hand.
- `X-Real-IP` is trusted only from loopback (nginx). A local process could forge it to dodge
  per-address rate limits. That doesn't open anything: panel-only actions need the panel key.
