# Missing & found

A shared board for **missing people** and **lost & found things**, with photos, that everyone on
the Badger's Wi-Fi can search. When someone is found, one tap marks them **✅ FOUND SAFE** in big
green letters so nobody keeps searching.

Find it on the launcher under **Neighbourhood → 🔎 Missing & found**.

## Using it

- **👤 People / 🎒 Lost & found things** switch between the two lists.
- **Search by name** — part of a name is enough, and capitals and accents don't matter
  (`jose nun` finds `José Núñez`). Matches on the name come first, then matches in descriptions.
- **Report a missing person** — name(s), description (age, height, clothes, medical needs), where
  and when last seen, how to reach you, and an optional photo.
- **Lost or found something** — what it is, description, where and when, how to reach you, photo.
- On the phone you posted from, your post shows:
  - **✅ Found safe** (people) or **Returned to owner** (things)
  - **Close**, **Edit**, **Add / Replace / Remove photo**, **Delete post**
  - **📡 Ask for a radio announcement**
  That phone keeps a private key for each post. If you lose it, ask the Badger's owner.
- **Report a problem** tells the Badger's owner that a post looks wrong or abusive.

Posts marked found safe, closed or returned are deleted automatically after 14 days, with their photos.
Open posts stay until the poster or the admin removes them.

## Photos

The photo is shrunk **on your phone** before it is sent (at most 1600 pixels, about 1 MB). This
also removes the hidden data cameras add, such as **GPS location**. The Badger checks the photo
again and strips any of that data that is still there, then keeps it under a random name.

Taking a photo of someone else's child? Make sure the family agrees.

## Radio announcements (mesh)

The Badger can send a short message to every Meshtastic radio in range on the community channel, e.g.

> MISSING: Ana Ruiz, 8 years old, red jacket, last seen Elm St 14:00 — see Badger board

- Nothing goes on air unless the **admin approves it**. The poster asks; the admin sees the
  request (with the text, which they can shorten) and taps **Send on radio** or **Decline**.
- At most **3 announcements per hour** in total, because radio airtime is shared and slow.
- If a person who was announced as missing is later marked found safe, a
  "FOUND SAFE: Ana Ruiz — see Badger board" announcement is queued for the admin automatically.
- Each announcement is also posted on the community check-in board.

## Privacy

**Everything here, including photos and contact details, can be seen by anyone on the Badger's
Wi-Fi.** Share a way to reach you that you're comfortable with (e.g. "Maria at the school gym" or
"the check-in board"). Radio announcements can be heard by anyone with a Meshtastic radio in range.

## For the admin

Signed in as admin, every post shows **Hide / Unhide**, **Remove**, **Clear reports** and the
radio announcement box. Hidden posts and their photos are only visible to the admin. The admin
can also change any post's status (e.g. mark someone found safe on the family's behalf).

## Limits (to stop abuse)

- 3 new posts per device per 10 minutes, at most 10 open posts per device, 300 posts in total.
- 10 photo uploads per device per 10 minutes; photos up to 1.2 MB and 2048 pixels; 400 MB for all photos.
- Name: 120 characters · Description: 1000 · Where: 200 · When: 80 · Contact: 200.

## Hardware

None required. Radio announcements use the Badger's Meshtastic radio if it has one
(see `docs/HARDWARE.md`); without it, everything else works.

## Settings (`/etc/prepper/prepper.toml`)

See the `[missing]` section of `os/prepper.toml.example`: `max_posts`, `per_ip`, `posts_per_10min`,
`max_photo_kb`, `max_photo_px`, `photos_total_mb`, `resolved_keep_days`, `mesh`, `mesh_per_hour`,
`board_copy`. Turn the feature off with `[features] missing = false`.
