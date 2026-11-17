# Security notes

What protects what on the Honey Badger, and what it doesn't protect. Families use it in an
emergency, so where we had to choose, **nothing may lock the family out**. Every safeguard below
has a way back.

## Who can do what

| Who | How they prove it | What they get |
|---|---|---|
| Anyone on the Wi-Fi | nothing | library, maps, tools, community board, coarse position (~10 km) |
| Family member | a device key from a one-time invite (HttpOnly cookie) | family board, family radios on the map, precise position |
| Admin | the admin PIN → a 12-hour session (HttpOnly cookie) | settings, members, Wi-Fi/radio key changes |
| The front panel | a root-only key (`/etc/prepper/panel.key`) | invites, precise position |
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
the admin page shows them too. nginx tells the launcher which one a request used
(`X-Forwarded-Proto`). Requests made on the Badger itself count as safe.

Still sent over plain HTTP (accepted trade-offs, so phones without the certificate keep working):

- the admin PIN at login and the session cookie;
- invite codes and the device key handed out at join (valid once / for that device).

Someone sniffing the hotspot with the Wi-Fi password could catch these. Use HTTPS for admin work if
you can. Change the Wi-Fi password when someone leaves the family: that ends their sniffing.

The USB adapter's hotspot also offers WPA3 (SAE), which can't be decrypted passively even with
the password. The onboard radio (wlan0) only does WPA2.

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
   The vendored viewers under `/apps/` (pdf.js, foliate-js) are ours and are left alone.
   Untrusted files (USB, content, peer sync, ZIM articles) were already sandboxed.
3. **CSRF:** every POST must be `application/json` (or carry `X-Badger-Upload` for uploads), and is
   refused when the browser says it is cross-site or the `Origin` doesn't match. The cookies are
   `SameSite=Strict`, and CORS never grants credentials.

**Left open:** a hostile script inside one of those apps could still open the launcher in a new
window (`window.open`) and drive it while someone is signed in. Same-origin windows can script
each other, and only a separate origin stops that. The full fix is to serve the third-party apps
on their own port or host name. That needs changes to the catalog and renderer URLs, so it is left
for later. Until then, the CSP and HttpOnly cookies stop the realistic attacks: stealing a token
for later use, and silent API calls from an app page.

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

## The member list

`members.json` is written atomically, with a copy of the last good version in
`members.json.bak`. A file that can't be read or parsed is **never** treated as "no members": the
launcher refuses to change it, and the admin page offers *Restore the last good copy*. Names are
cleaned of invisible and direction-changing characters, and two members can't have names that look
the same. Wrong invite codes lock the guessing address out for 10 minutes; a spray of failures
from many addresses can't clear that lock.

## A removed member

Removing someone ends their device keys at once, and their radio IDs stop naming posts. Things
they may still know:

- **The Wi-Fi password and the family radio key:** change them (above).
- **The admin PIN:** change it at the Badger (`sudo prepperctl set-pin`), then *Log out all admin
  sessions* (Settings → Badger administration).

## Disk

- The installer turns off swap on the NVMe (`dphys-swapfile`, if present). The document vault holds
  decrypted pieces in the launcher's memory, and those must never be paged to disk. Swap in RAM
  (zram) is fine. A swap file set up some other way (e.g. a `/swapfile` in `/etc/fstab`) should be
  removed by hand.
- `X-Real-IP` is trusted only from loopback (nginx). A local process could forge it to dodge
  per-address rate limits. That doesn't open anything: panel-only actions need the panel key.
