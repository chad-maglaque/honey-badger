# Aid station

A simple log for a neighbourhood aid station: who came, what they needed, and what help they
got. It also has a public status line ("🟢 Aid station open · water · first aid") that everyone
on the Badger's Wi-Fi can see.

Find it on the launcher under **Neighbourhood → ⛑️ Aid station**.

## Who can see what

| | Status line | Visit log |
|---|---|---|
| Anyone on the Wi-Fi (neighbours, visitors) | ✅ read | ❌ |
| Family members | ✅ read | ❌ (unless also appointed as aid volunteers) |
| Aid volunteers (appointed by the admin) | ✅ read and change | ✅ read and write |
| Admin (admin PIN) | ✅ read and change | ✅ read and write, plus volunteers and privacy settings |

Volunteers are often not family, so they get their own sign-in, separate from family members.

## Setting up volunteers (admin)

1. Sign in as admin (Settings → Badger administration), then open **Aid station → Volunteers & privacy**.
2. Type the volunteer's name and tap **Create code**. You get a code like `K7QX-M2PD-9Z`.
3. On the volunteer's phone: join the Badger's Wi-Fi, open **Aid station → "Aid volunteer? Sign in here"**,
   and type the code.

Each code works **once, on one device, for 24 hours**. A volunteer with two phones needs two codes.
After five wrong codes, that phone has to wait 10 minutes.

- **Revoke** stops one device at once.
- **End of shift: sign out everyone** stops every volunteer device and cancels unused codes.
- A volunteer can sign their own device out from the page.

## Using the log (volunteers)

- **New visit** — built for one thumb: tap the help given (💧 water, 🥫 food, ⛑️ first aid,
  🏠 shelter, ℹ️ info, ➕ other), type a name *or a description* ("man ~40, blue coat"), and
  save. Needs, notes, follow-up and "handled by" are optional. The form clears for the next person.
- **Today** — counts for today (visits, people, each kind of help) and today's visits.
- **Follow-ups** — everyone marked "follow-up needed" that isn't done yet. Tap **✓ Follow-up done**.
- **Search** — by name, need or note; capitals and accents don't matter (`jose` finds `José`).
- **Station status** — open/closed, what's available now, and a short note. Shown to everyone,
  without any volunteer's name. If nobody updates it for 12 hours, it is marked as possibly out of date.
- **⬇ CSV** — downloads the log as a spreadsheet file (volunteers and admin).

## Privacy

- Records are **deleted automatically** after 30 days (the admin can choose 1–365 days on the page).
- Records stay on the Badger. They are **not** sent over the radio or shown to anyone else.
- They are **not encrypted** on the Badger's drive: anyone who takes the Badger apart could read
  them. Keep the Badger somewhere safe.
- Every CSV export, deletion and volunteer change is noted under **Recent activity** for the admin.
- A downloaded CSV lives on that phone or laptop. Delete it when you no longer need it.
- Write only what the station needs. A description is fine when someone doesn't want to give a name.
- The admin can **Delete all records now** (type DELETE to confirm).

## Limits

- Up to 10,000 records (configurable); after that new records are refused until old ones are
  exported and removed (lower the retention period).
- Each volunteer can save about 120 records per 10 minutes (stops a stolen key from flooding the log).
- Up to 30 volunteer devices plus unused codes.

## Hardware

None. It runs on the Badger itself.

## Settings (`/etc/prepper/prepper.toml`)

See the `[aidstation]` section of `os/prepper.toml.example`: `retention_days`, `code_hours`, `max_staff`,
`max_entries`, `status_hours`. Turn the feature off with `[features] aidstation = false`.
