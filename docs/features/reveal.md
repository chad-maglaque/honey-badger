# 👁️ What this Badger reveals

A live checklist of everything that tells someone **outside the family** something about this household —
green ✅ when it's fine, amber ⚠️ when it gives something away — each with a **one-tap fix** where one exists.

**Who:** the admin (Settings → *What this Badger reveals*) and the Badger's own screen (knob → *Posture* →
*What it reveals*, behind the panel code). Never visitors, never members' devices.

## The checks

| Line | Amber when | One-tap fix |
|---|---|---|
| Wi-Fi password | the hotspot is open or "enhanced open" (no password) | give it a WPA2 password (the safe change: switches back by itself unless someone rejoins and confirms within 10 minutes) |
| Wi-Fi name | it says badger / prepper / survival / bunker / emergency … | — (how to rename it) |
| Radio position | the radio shares its position on the public channel | turn it off |
| Radio name | its long or short name draws attention, or holds a family name | rename it to what thousands of untouched radios are called ("Meshtastic 4f2a") |
| Posture | Neighbourly | switch to Guarded |
| Shared beyond the Wi-Fi | a category is shared with a trusted circle (one line each) | stop sharing that category |
| Public replies / control traffic | radios outside the family can make the Badger answer, or it relays NOAA alerts on the public channel | answer only family radios |
| Front panel | no panel code | arm the panel for setting a code (then set it at the knob within 10 minutes) |
| Sealed records | unlocked right now, or not set up (only if this Badger has that feature) | lock them now |
| Recovery card | not confirmed as printed and put away | "I printed it" |
| Local certificate key | the local CA's private key is on the drive | — (`sudo prepperctl gen-tls` makes a new one and destroys the key) |
| Logs | the system journal is kept on the drive | keep logs in memory only |
| Visitor pages | what visitors on the Wi-Fi can open (amber if neighbourhood pages or sensor readings are open) | neighbourhood pages for family only |
| Panic wipe | not set up (only if this Badger has that feature) | — |
| Duress secret | no duress PIN or passphrase set (a made-up household is always ready; after a duress this always reads "not set") | — (admin page → Emergency) |

The list is computed fresh every time from the config and the live state (the radio's names come from the mesh
bridge). A fix is only accepted when the list offers it at that moment.

## Limits

- The radio-name check needs the radio plugged in.
- "Recovery card printed" is your word for it: the Badger can't see paper.
- Checks for other features (sealed records, panic wipe) appear only when those features are on this Badger.
