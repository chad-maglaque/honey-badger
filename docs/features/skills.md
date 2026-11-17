# Skills & resources

A neighbourhood directory: who nearby can help, and who needs help.

- **Offers** — skills (nurse, electrician, ham radio operator, childcare, translation…) and
  resources (generator, chainsaw, well water, 4×4, spare room…).
- **Needs** — "need insulin kept cold", "need a ride to the clinic", "need someone who speaks Tagalog".

Find it on the launcher under **Neighbourhood → 🤝 Skills & resources**.

## Using it

- **Browse** with the Offers / Needs / All buttons, or pick a category.
- **Search** for words like `nurse`, `generator`, `spanish`. Capitals and accents don't matter.
- **Add an offer or need** — say what it is, and optionally a name, where/how to find you, and when.
- **Your own entries** show **Edit**, **Still current** and **Remove** buttons on the phone you
  added them from. That phone keeps a private key for each entry. If you clear the browser's data
  or use another phone, ask the Badger's owner to change or remove it.
- **Report** tells the Badger's owner that an entry looks wrong or abusive.

Entries disappear 90 days after they were added or last marked **Still current**, so the
directory doesn't fill with offers that are no longer true.

## Privacy

**Everything here can be seen by anyone on the Badger's Wi-Fi**, including people you don't know.
Share only what you're comfortable with. A first name and "ask at the blue house on Oak St" is
usually enough. Don't post phone numbers or exact addresses unless you're sure.

"Where / how to find you" is **sealed** on the Badger's drive (docs/SECURITY.md → "Sealed
records"): while the Badger is locked after a restart it shows as "sealed" until the owner unlocks
it; what is offered or needed stays searchable. Edits and new entries work while locked.

The Badger does not store who posted what by name or address; it keeps a scrambled code of the
posting device's network address only to enforce the limits below.

## For the admin

Signed in as admin, every entry shows **Hide / Unhide**, **Remove**, and **Clear reports**, with a
count of reports. Hidden entries are kept but only the admin sees them. The admin can also edit any entry.

## Limits (to stop abuse)

- 5 new entries per device per 10 minutes, at most 10 entries per device at once.
- 500 entries in total.
- What: 80 characters · Name: 40 · Where/how: 200 · When: 120 · Details: 500.

## Hardware

None. It runs on the Badger itself.

## Settings (`/etc/prepper/prepper.toml`)

See the `[skills]` section of `os/prepper.toml.example`: `max_entries`, `per_ip`, `adds_per_10min`,
`expire_days`. Turn the feature off with `[features] skills = false`.
