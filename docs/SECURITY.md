# Security notes

What protects what on the Honey Badger, and what it doesn't protect. Families use it in an
emergency, so where we had to choose, **nothing may lock the family out**. Every safeguard below
has a way back.

## Who can do what

| Who | How they prove it | What they get |
|---|---|---|
| Anyone on the Wi-Fi | nothing | library, maps, tools, alerts, community board (radio only with a member's OK), the Badger's area (a ≥10 km grid cell) — no power, occupancy, USB drives, sensor readings or security posture; in Lockdown only the library, maps and tools |
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
- Guards *Invite member*, *Approve device*, *Family radio*, *Posture*, *Services*, *Wi-Fi → Turn off*,
  *Import from USB*, *Restart* / *Shut down* (and hold-to-shut-down). *Join Wi-Fi* and *Open app*
  stay open — the way onto the network. `[panel] lock_protect` changes the list (also possible:
  `wifi_join`, `app`, `power`, `updates`, `usb_export`, `eject`, `time`, `status`).
- *System → Panic wipe* and *Hand over / sell* always need the code when one is set (whatever
  `lock_protect` says), then a yes/no, then holding the knob down for 3 seconds (a short press or the
  screen sleeping cancels). See "Panic wipe" below. An optional **duress code** (*Panel lock → Duress
  code*) opens the panel exactly like the code and quietly runs the duress action (see "Duress" below).
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

## The problem solver's Python sandbox

The problem solver can run short Python programs the AI wrote. Each run is its own systemd unit, started on demand through a socket (`prepper-solver-sandbox`), as a throw-away user (`DynamicUser`) with no network, no access to the family data, keys or settings, and tight time and memory limits. The portal checks that these properties really hold (a probe) before every use; if it can't prove them, the Python tool stays off and there is **no unsandboxed fallback**. Invariant: the solver's tools (calculators, library search, the read-only supplies lookup, the sandbox) have no side effects outside the sandbox. They can't post, send over the radio, change settings or write files, so a confused or manipulated model can only produce a wrong answer, not an action.

## Sealed records (encryption at rest)

The owner's decision: the Badger must restart on its own and keep working, a Badger that is seized or
stolen must reveal nothing of the household, and it must never be turned against its owners. So the
household's sensitive records are **sealed**: encrypted on the drive, readable only after the owner
unlocks the Badger with the **owner passphrase** (or the recovery key), and only in the launcher's memory.

**What is sealed**

| Data | Sealed | Stays readable while locked (why) |
|---|---|---|
| Supplies (`features/supplies/supplies.json`): items, hiding places, household, log, notices | everything | — (changes made while locked go into `supplies.pending.jsonl`, sealed, applied at unlock) |
| Aid-station log (`features/aidstation/log.json`) | person, needs, notes, follow-up note, volunteer names | time, number of people, help types, follow-up flag (today's counts and the retention purge keep working) |
| Family board history (`family-board.jsonl`) | every line, whole | — (to family members only: posts written since the Badger locked, at most the last 48 h / 200 — radio messages included; older ones after unlock) |
| Family members (`members.json`, `.bak`) | names, device labels, last-seen times, names on open invites | hashed device keys, ids, radio IDs (sign-in keeps working; the mesh bridge needs radio IDs) |
| Skills & resources, Missing & found | contact details | what is offered or needed, names, descriptions, photos (the community search keeps working) |
| Document vault | already encrypted with its own family passphrase | — |

Not sealed: public boards, alerts, the library, maps, sensors, aid-station public status, aid-volunteer
names in `staff.json` and the audit list, visitors' radio requests (`mesh-pending.json`), and files other
services write (see "Not covered" below).

**How it works** (`software/common/sealed.py`)

- A device keypair (ECDH on NIST P-256). The **public** key is on the drive in the clear, so the Badger
  seals new records at any time, even locked: everything keeps working write-only. The **private** key
  is stored only encrypted, under a random key that is wrapped twice: by the owner passphrase (scrypt,
  N=2¹⁶, r=8 — about 0.5 s on a Pi 5) and by a 256-bit **recovery key** shown once at setup.
- Each run of the launcher (and each lock) makes a fresh random data key, wrapped for the public key
  ECIES-style (`openssl pkeyutl -derive` with a throw-away key, HMAC-SHA512 over the shared secret and
  both public keys). Records are encrypted with it by the vault's scheme: AES-256-CTR through the
  `openssl` program plus HMAC-SHA256 (encrypt-then-MAC), a unique key per record. Tampering is detected.
- Keys and passphrases reach `openssl` only through pipes (`/dev/fd/N`), never on its command line.
- Why P-256 and not RSA or X25519: macOS LibreSSL (where the tests run) has no X25519, and its RSA-3072
  key generation takes 5–15 s; P-256 ECDH works identically with LibreSSL and OpenSSL 3 (Debian 13), and
  the tests check the files cross-open between the two.

**Locked and unlocked**

- **Locked after every restart.** Unlock on the admin page (Settings → Badger administration →
  Sealed records) over **https**, or at the Badger itself; a passphrase is never accepted over plain
  http on the hotspot. Any family member can press **Lock now** (no secret involved).
- While locked: names show as "a family member"; the family board says *Family history is locked — the
  owner can unlock it* and shows family members only posts written since it locked, and of those only the
  last 48 hours and at most 200 (`[sealed] recent_hours`, `recent_max`) — never to visitors or an admin
  session alone; family radio messages heard while locked count (see "Family radio messages" below); supplies take new items and
  changes but show only those; the aid log shows older records as "🔒 sealed"; contact details read
  "sealed". Nobody **new** can join the family (names can't be checked for look-alikes); an extra device
  for an existing member can. CSV exports wait for the unlock.
- Optional idle auto-lock (`[sealed] idle_minutes`, default off — an aid station needs all-day access).
- **Posture "lockdown"** (`posture.json`, `level = "lockdown"`) locks at once and refuses to unlock
  unless the admin ticks *Unlock anyway*; setting lockdown again locks again.
- Wrong passphrases: 5 per address per 10 min, then a 10-minute wait; 20 Badger-wide per hour, then
  15 minutes for everyone — that one survives a restart (`sealed/lockout.json`).
- The passphrase: 16+ characters or four or more words, and not the admin PIN. *Change passphrase*
  needs the current one; *Forgot the passphrase?* takes the recovery key and a new passphrase; *Make a
  new recovery key* retires the old one. The admin page nags until the owner confirms the recovery key
  is written down (`recovery_ack`, for the "What this Badger reveals" check).

**Before setup ("not protected yet").** The keypair is made on first use, with its key next to it on the
drive, so data is sealed from the first day (a forensic grep finds nothing) but anyone who knows this
design can open it. Setting the owner passphrase makes a **new** keypair, rewraps every data key,
re-seals every store with it and zero-fills the old key and the old plaintext files (an older Badger's
files are migrated the same way). The admin page shows this as a warning until it's done.

**Crypto-erase (for the panic wipe):** `sealed.destroy_keys()` in the launcher (zeroises the keys in
memory, then zero-fills and deletes `sealed/device.key`, `sealed/keys/*.dek`, `sealed/lockout.json`),
or `sealed.destroy_keys_in("/var/lib/prepper/portal/sealed")` / `python3 software/common/sealed.py
destroy-keys` from another process (a running launcher still holds keys in memory until it is stopped
or locked). Everything sealed so far is then unreadable for good; the Badger keeps running with a fresh,
unprotected keypair.

**Limits — honestly**

- A running, **unlocked** Badger holds the keys in memory: someone who takes it running and keeps
  power on can read what an unlocked Badger shows. Lock it (or let lockdown do it) when danger comes.
  While locked, the current run's data key is in memory too (that is how posts since the lock show).
- Python can't promise every copy in memory is wiped; key buffers are overwritten, swap is off.
- Flash wear-levelling: zero-filling a file doesn't guarantee the old blocks are gone from an SSD or SD
  card. Data written before the owner passphrase was set may be recoverable in a lab — set it at first boot.
- It protects against a raider reading the drive, not against one who **changes** the Badger and gives it
  back (they could swap the public key or the code). A Badger that was out of your hands is suspect.
- Radio state the mesh bridge writes (its radio key `mesh-channel.json`, Badgers heard `mesh-peers.json`,
  radio positions `mesh-nodes.json`, telemetry) lives in RAM (`/run/prepper`), see below.

**Family radio messages while locked.** The mesh bridge can seal only with a key of its own, which a locked
launcher can't open — a family message heard on the radio would wait for the unlock. So when the family board
is on persistent storage, the bridge appends the family lines it hears to a spool in RAM
(`/run/prepper/family-inbox.jsonl`, `[portal] family_inbox`, 0600) instead, and the launcher takes them in —
on every family-board read and every 5 seconds — sealing each with its **current** data key, so it counts as
written since the lock: family members can read it while locked (within the 48 h / 200 bound above). Both sides
hold an exclusive `flock` on the spool (the bridge per append; the launcher while copying, then truncating), so a
concurrent append is never lost; the spool is emptied only after the board write succeeded (a crash in between
can show a line twice, never lose one). Spool lines always become unverified radio posts, whatever they say. Limits: the
words sit in RAM in the clear for those few seconds (like everything the bridge handles); a **reboot** (or
power cut) drops lines not yet taken in — a launcher restart doesn't (the spool stays in `/run`); if the
spool can't be written or passes 1 MB (launcher not running), the bridge falls back to sealing the line with
its own key straight onto the board (readable after the unlock). Without sealed records set up on the bridge's
side (no device key yet), the old way stays: plain lines, sealed by the launcher on the next read. The panic
wipe erases the spool with the other RAM radio state.

## Security posture, trusted circles, "what this Badger reveals"

- **One switch** ([features/posture.md](features/posture.md)): Neighbourly / Guarded (default) / Lockdown drives
  the radio commands, stranger budgets, public position, radio silence, public NOAA relays, visitors' radio
  requests, visitor pages (sensors, aircraft, satellites, neighbourhood pages), Wi-Fi, logs and circle sharing —
  one source of truth (`software/common/posture.py`), owner overrides shown as such. In Lockdown an override can
  only tighten. Changing it needs the admin, or the front panel behind its code. A damaged `posture.json`
  counts as Lockdown (fail closed). Lockdown also mutes the radio (CLIENT_MUTE, broadcasts off; restored after)
  and gives a password-free hotspot a WPA2 password through the failsafe change (`prepperctl wifi-secure`).
- **Trusted circles** ([features/circles.md](features/circles.md)): records stay on their own Badger; peers get
  pointers only, by pull, to the asker only, budgeted and logged; sweeps refused and flagged; no amounts, no
  locations (address-like text needs confirming), no aggregates; opt-in per category; nothing in Lockdown. The
  circle link (the key) is shown like the family link: family members over https and the front panel, never the
  admin page; joining by link needs https.
- **What this Badger reveals** ([features/reveal.md](features/reveal.md)): a green/amber checklist with one-tap
  fixes, admin and front panel only.
- **RAM-only mesh state**: radio positions, Badgers heard, telemetry, the family/circle links and the Bluetooth
  PIN live in `/run/prepper`, re-read from the radio after a restart. A copy on persistent storage (a
  `[portal]` path outside `/run`, or an old file left in the portal folder) is sealed in place by the launcher
  on its next request, and written sealed by the bridge; family-board lines go through the RAM spool above.

## Panic wipe

For the moment the Badger is about to be taken. Prime directive: what the family keeps on it must not
be turned against them — who lives there, when the house is empty, what supplies there are and where
they are hidden, which neighbours they reach on the radio. Engine: `os/wipe.py`; how to use it:
OPERATIONS.md → "Panic wipe, duress PIN, dead-man switch".

**Crypto-erase first.** On flash (the NVMe, SD cards) overwriting or deleting a file does not reliably
erase it: wear levelling keeps old copies in blocks the operating system can't reach. So the wipe
destroys the small **key** files first — that takes well under a second — and whatever they encrypted
is unreadable from then on, even from an image of the raw flash made later. Then it cleans up. Order:

1. **Keys:** the sealed-records keys (`sealed.destroy_keys()`: device key + wrapped data keys; the
   launcher also zeroises its copies in memory at once), the vault's passphrase- and recovery-wrapped
   key (`features/vault/vault.json`), and `[wipe] key_paths`.
2. **The radio:** a key-change request with `"wipe": true` asks the mesh bridge for new random keys on
   every enabled private channel (family, circle, any other channel 1–7 with its own key) and, with
   `"forget_link"`, not to keep the new links — `[wipe] radio = "factory-reset"` asks for the radio's own
   factory reset (keys and node list). The wipe waits up to `mesh_wait_s` (8 s) for the bridge's answer
   (`{"ok", "wipe", "channels": [...]}`), carrying on with the next steps meanwhile, and the receipt and
   the front panel say what the radio did — including "only some channels" or "no answer".
3. **Device secrets:** the family and circle radio links (= keys) and the radio's Bluetooth PIN (on disk
   and in `/run`), the panel key and panel code, the device signing key, its passphrase file and its own
   line in `allowed_signers`, the old Wi-Fi password kept for a pending change, first-boot leftovers,
   the TLS keys.
4. **Family data**, deny-by-default: everything in `/var/lib/prepper/portal` goes — members (and the
   `.bak`), both boards, the aid log, supplies, skills, missing people, sealed records, the vault's
   blobs, visitors' radio requests, mesh pairing/outbox, trusted-circle records and slot, the radio's
   saved pre-Lockdown settings, settings, check-ins — except a short keep-list: the personal-use licence
   decision, `mesh-silent` (a wipe never ends radio silence), the made-up household (`decoy.json`, harmless by
   design; a handover reset removes it) and the `forecast` / `satellites` caches
   (`[wipe] keep_features`). A feature added later is erased by default. Also the radio feature's saved
   station position. **The posture is set to Lockdown** (`posture.json`): whoever holds the Badger next
   gets the quietest box — radio muted, nothing beyond the family, visitors see only the library, the
   Wi-Fi needs a password. (After a duress wipe this waits with the other noticeable steps.)
5. **Radio state** again — RAM (`/run/prepper` or `[portal] run_dir`: positions, Badgers heard,
   telemetry, family/circle links, Bluetooth PIN, circle traffic) and any older copy in the portal folder —
   then the bridge is stopped and disabled (`[wipe] mesh_after = "off"`).
6. **Uplink Wi-Fi:** `[network] uplink_wifi`, NetworkManager Wi-Fi client profiles (files and live), the
   `wifis:` block of netplan and of the first-boot `network-config`.
7. **Logs:** `journalctl --rotate` + `--vacuum-time=1s`, nginx logs, the first-boot log, DHCP leases
   (dnsmasq stopped, leases removed, started again — it keeps them in memory too).
8. **Last:** a fresh TLS certificate (a new local CA; phones that trusted the old one see a warning), a
   new random hotspot password (shown only on the front panel; `[wipe] wifi = "off"` switches the Wi-Fi
   off, `"keep"` leaves it), `apply-config`, and a restart of the launcher, which drops sessions and keys
   held in its memory.

Every step keeps going when another fails and is idempotent. A marker
(`/var/lib/prepper/wipe-pending.json`, no secrets) lets `prepper-wipe-resume.service` finish a wipe a
power cut interrupted. A receipt of what was done is kept in RAM only (`/run/prepper/wipe-receipt.json`,
`prepperctl wipe-status`). Starting it: the admin page over https (type `WIPE`), the front panel (code,
yes, hold 3 s), or `prepperctl panic-wipe` — always with a **one-time confirmation token** (2 minutes,
stored hashed in RAM, used up by any attempt), so a stray call can't fire it. The launcher may only ask
for the admin or duress kind; the wipe then runs as its own systemd unit, so the launcher's restart at
the end can't cut it short.

**What survives:** the library, maps, AI models, software and update state; `prepper.toml` (hostname,
radio channel numbers, the admin PIN **hash**, the duress PIN hash, `[time]` home position, `[upd]`
peers); other Badgers' keys in `allowed_signers`; satellite pictures; the personal-use licence decision;
the radio-silence flag. A handover reset (below) clears the config-side items too.

**Limits — honestly:**

- **The radio holds its own copy** of every channel key and its node list (positions of radios heard).
  Only the bridge can change that, and only while it is running and the radio answers (the receipt and
  the panel say what happened; an older bridge re-keys the family channel only). If it says the radio
  was not re-keyed, **unplug the radio** (or factory-reset it in the Meshtastic app). New keys don't erase
  the radio's node list — only a factory reset does. A radio taken away separately needs no Pi at all.
- **Keys in memory:** the launcher, the panel and the bridge hold keys in RAM until they restart; the
  launcher zeroises its sealed-records keys at once, the rest goes at the restart (step 8; after a
  duress wipe that is up to `duress_delay_min` later). Python can't promise every copy is gone.
- **A disk imaged BEFORE the wipe** keeps everything that was readable then. The wipe can't reach a copy.
  Plaintext written before sealed records were set up may survive in flash blocks the OS can't erase;
  the sealed records and the vault don't (their keys are gone).
- **Admin PIN hash, duress PIN hash** stay in `prepper.toml` (scrypt, slow to attack) — so the owner can
  still run the library afterwards. A handover reset removes them.
- A raider who **changes** the Badger and gives it back is not covered (see Sealed records). Something
  already copied off (USB exports, vault backups on phones) is not on the Badger.

### Duress: when someone forces you to unlock the Badger

**People come first.** The Badger's data is never worth a life. A family may be made to hand over a PIN
at gunpoint; whatever happens must keep the people in the room safe, must never look suspicious, and should be
undoable if everyone gets through it. This repo is public: assume the person forcing you knows that duress
secrets exist and knows this page.

**When to give the real secret, when the duress one.** If the person can *check* what you say — an insider, a
neighbour who knows the family, someone who has seen this Badger or knows what is on it — give the **real** PIN
or passphrase. A made-up household they can see through is more dangerous than losing the data. Use a duress
secret only when they can't tell the difference. Agree on this as a family beforehand: **whoever is forced
gives whatever keeps people safest, and nobody blames them afterwards.**

**The duress secrets** (all optional, all set by the owner; the admin page's Emergency box nags until one is
set, and so does "What this Badger reveals"):

- the **duress PIN** — a second admin PIN (`[portal] duress_pin`, scrypt; admin page over https with the admin
  PIN, or `sudo prepperctl set-duress`; never the admin PIN);
- the **duress passphrase** — typed where the sealed-records owner passphrase is asked (admin page over https
  with the owner passphrase; stored as an scrypt tag in `sealed/device.key`, as strong as a passphrase, never
  the owner passphrase or the admin PIN);
- the **panel duress code** — at the knob (*System → Panel lock → Duress code*; salted scrypt in
  `panel-lock.json`).

Each gets **exactly** the real secret's reply: same status, same fields, same cookie, the same work and so the
same timing. Every admin login runs both PIN checks, every passphrase try runs both scrypts, every panel code
try both hashes — against a dummy of the same cost when no duress secret is set, so the timing doesn't even
tell whether one exists. Wrong-try lockouts apply as usual; a duress secret is never counted as a wrong try.
Typed where the admin PIN is asked again (handover, duress or dead-man setup), or where the owner passphrase
is asked (change passphrase, new recovery key), the duress secret does the same.

**What everyone then sees: the made-up household.** Not an empty box — an empty prepper box looks wrong and
raises danger. Every Badger keeps a small, boring household ready at all times
(`/var/lib/prepper/portal/decoy.json`, 0640): made at install and, if missing, at the launcher's first start,
from built-in templates with a little randomness seeded once per device (`software/common/decoy.py`), so no two
Badgers show the same one. It is:

- a modest supplies list an ordinary household might have — a couple of cases of bottled water, a few water
  jugs, canned beans/soup/tuna, rice, pasta, peanut butter, a first-aid kit, pain relievers, AA/AAA batteries,
  an LED flashlight, a few 1 lb propane canisters for a camp stove, matches, toilet paper — believable
  quantities, expiry dates a few months to a few years ahead, ordinary places (Pantry, Garage, Kitchen drawer),
  no hiding places;
- a short, mundane family-board history over the last six days ("Back around 6", "Charging the phones here",
  "Can someone check the rain gauge?") — no places, no times when the house is empty, no real names;
- **as many members as the real family**, with common first names chosen once per device. The real members'
  device keys (hashes only) are carried over, so every family phone keeps working and simply shows the made-up
  names. *Trade-off:* someone who knows the family's real first names may notice — the owner can type
  believable names (or the real ones) in the decoy editor;
- an empty vault (not set up: "no documents"), an empty aid log, skills and missing lists, the public community
  board (members' posts under the made-up names; posts of removed members left out), posture **Guarded**, no
  family radio link, no Bluetooth PIN, no devices waiting for approval, no circle, and the Emergency box,
  "What this Badger reveals" and the panel's Duress menu all saying that **nothing is set up**. Weather,
  satellite passes, sensor readings and the library are the real ones.

The decoy holds only made-up content (harmless by design), so it is a plain file the launcher can read without
the real keys. The owner previews, edits (names, supplies, board posts) and regenerates it on the real admin
page (*Emergency → The made-up household*). During a duress the launcher writes a working copy into its private
`/tmp` and reads that instead of the real files — sealed with a throw-away key of its own, so "locked" and
"Lock now" look exactly as they do for real records, and its sealed-records status shows the real one's
fingerprint and setup date. Board times are anchored to the moment of the duress (a restart rebuilds the same
copy). Anything typed into it is dropped at the recovery or a restart.

**Who sees it, and for how long:** after any duress, **every** session — the coerced admin page, a real admin
login, every family phone, the front panel — sees the made-up household, until the owner types the **recovery
key**. Why everyone: an attacker who holds a family phone would otherwise see the real family on it, and a
"real" view that differs from the coerced one is exactly the tell this must avoid.

**What happens behind it** — the owner's choice, *Emergency → What happens behind the made-up household*
(`portal-settings.json`, else `[security] duress_action`):

- **`lock` (default — recoverable).** Nothing is erased. The sealed records get a **hard lock**: a flag in
  `sealed/device.key` (`"hard_lock": {"at"}` — no secret in it), the keys leave memory, and the owner passphrase
  alone no longer opens them — only the **recovery key** does, after which the passphrase works again. The
  vault likewise (`"hard_lock"` in `vault.json`; every device locked; only the vault's recovery key, then the
  family passphrase again). **No restart, no Wi-Fi change, no posture change, no radio change, no visible
  message**, no wipe receipt, no marker for `prepperctl wipe-status`. The front panel's duress code reaches the
  same flags through the root wipe engine (`os/wipe.py`); the launcher checks the flag on every request (one
  stat), and after a restart it is still there, so the made-up household comes back by itself. Typed into the
  made-up sealed page, the owner passphrase "unlocks" too — a family member is never shown "wrong passphrase"
  under threat. Needs sealed records set up (otherwise there is no recovery key: the made-up household then
  lasts only until the launcher restarts; the setup box says so).
- **`wipe`** — the panic wipe above, started in the background: the keys go at once, the steps a person in the
  room could notice (new Wi-Fi password, https certificate, launcher restart, posture Lockdown) wait
  `[wipe] duress_delay_min` (10 minutes; a timer, re-armed at boot), and the wipe puts the same made-up
  household **in place of the family's data** (members — same phones —, family board, supplies), so the box
  never looks emptied afterwards. **There is nothing to recover**; the setup box says so plainly.

**Recovery (lock mode).** When everyone is safe: on the admin page over https, *Sealed records → Lock now →
Unlock* with the **recovery key** (or *Forgot the passphrase?* with the recovery key and a new passphrase), or at
the knob: *System → Recovery key* (letters and digits, picked with the knob). The hard lock ends, the made-up
household goes, the real records are back exactly as they were, duress sessions are logged out (not the one that
typed the key), and the family board gets a note from the Badger: "Recovered after a duress lock." Then unlock
the vault with its own recovery key (typed in its passphrase box). Anyone who forces the **recovery key** out of
you gets the real data — keep it away from the Badger.

**What a careful attacker could still notice** (honestly):

- the names differ from the family's real names (see above), and the made-up household is small;
- posts, supplies and members added during the duress vanish after the recovery (and after a restart, the copy
  is rebuilt from the profile);
- a device that joins during the duress exists only in the made-up household (it has to join again later);
- family radio messages that arrive meanwhile don't show (they wait in RAM and appear after the recovery; a
  reboot in between loses them), and the family radio link and Bluetooth PIN are "not there";
- the Badger's disk, read by someone who knows this design, shows the hard-lock flag in `sealed/device.key`
  and `vault.json`; `decoy.json` is on every Badger, so it alone tells nothing;
- an armed dead-man switch keeps counting during a lock (check-ins in the made-up household don't count), so a
  long duress lock can still end in the dead-man wipe;
- in wipe mode: the radio going quiet and, after the delay, the Wi-Fi password changing, the posture going to
  Lockdown and the sealed-records box saying "not protected yet" (its keys are gone);
- a posture or Wi-Fi change made from inside the made-up household is real (the Wi-Fi switch, Lockdown's
  Wi-Fi password), even though the real posture file is not touched.

### Dead-man switch

Off by default. Armed in two steps (admin page over https: days + admin PIN → a 6-character code → type
it back within 10 minutes; or `prepperctl deadman-set DAYS` then `--confirm CODE`). When armed, an hourly
timer counts how long the Badger has been **running** with no check-in (a family member's device using
the launcher, or an admin login — the launcher bumps a counter, no times or names, and only while armed)
and runs the panic wipe after `deadman_days` (3–365). Only uptime within one boot counts, at most ~2 h per
run: a clock jump (GPS fix after a flat RTC battery) can't fire it, and a Badger that is switched off
never counts down. Warnings: the admin page shows the hours left; in the last 48 hours the front panel
shows "CHECK IN" on its home screen. Turning it off is one step (also over plain http).

### Handover / resale reset

`prepperctl handover-reset` (admin page over https: type `HANDOVER` + the admin PIN; front panel: code,
two questions, hold 3 s): everything the panic wipe does, never delayed, plus factory settings — a new
neutral hotspot name and random password (the builder's rules), `security = "auto"`, no admin PIN (the
launcher says to set one at the Badger), no duress PIN, dead-man off, `[upd] peers` and `allowed_signers`
emptied, `[time]` home position removed, a new signing identity, the posture back to **Guarded** (the shipped
default) and radio silence off, and a radio factory-reset request. On a
unit built for sale (or with `--sale`), `[device] provisioning` is `"sale"` again and personal-use content
is withdrawn, removed and the acceptance record deleted, so the next owner is asked again.

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
