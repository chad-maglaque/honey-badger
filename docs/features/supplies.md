# Supplies & rationing

**Family → 🥫 Supplies** is the household's pantry list: water, food, medicine, fuel,
batteries, hygiene. It answers *how many days do we have?*, *what's about to expire?* and
*what are we running out of?*

No extra hardware is needed.

## Using it

1. **Join the family** on your phone first (Check-in board → Family, with an invite code).
   Only family members see the list.
2. **Set the household:** how many people and pets, and daily needs. Defaults are 2000 kcal
   and 3.8 L (about 1 US gallon) of water per person per day; pets count for water (1 L a day
   each, editable) and, if you set it, food.
3. **Quick add:** tap a preset (water jug, canned food, rice, batteries…) or type a name, the
   quantity and the unit, and press **Add**. *More details* adds where it's kept, the expiry date,
   a **minimum** to keep on hand, and:
   - **kcal per unit** for food (it's on the label: kcal per serving × servings). Food without it
     isn't counted in the days.
   - **litres per unit** for water. Units `L`, `ml`, `gal`, `qt` are understood without it.
   - **hours per unit** for fuel and batteries (optional).
4. **Use it:** **−** and **+** change the count by one; **Use…** asks how many; **Edit** changes
   anything or deletes the item. Every change is logged with who made it (*Recent changes*).

## What it shows

- **Days of water and food** for your household, counting only items that haven't expired.
  Red under 3 days, "low" under 14 (both settings).
- **Fuel and batteries**: total hours; days too if you set how many hours a day you use.
- **Needs attention:** expired items, items expiring within 30 days, items below their minimum
  or used up.
- **Rationing:** "at 1200 kcal per person per day you have N days", with a table for 2000–800
  kcal and 3.8 / 2 / 1 L of water. Below ~1200 kcal for long, people weaken; under 2 L of water
  a day is only for a short emergency, and heat, nursing or illness need more.

## Family board notes

The Badger checks the list every 30 minutes. When something **newly** runs low, runs out,
expires or is about to, it posts **one** note on the **family** board (never the public one),
from "📦 Supplies", listing everything new since the last check. It won't repeat itself for
things that stay that way; an item that bounces back and forth is mentioned again only after
3 days. While anything is still low or expired it posts a short **weekly reminder** (at most).
Optionally (`mesh_critical = true`, off by default) it also sends one short message on the
family mesh channel when water or food drops under 3 days.

## Spreadsheets

*Spreadsheet (CSV)* downloads the list, or imports one: the first row names the columns
(`name, category, quantity, unit, location, expiry, minimum, kcal_per_unit, litres_per_unit,
hours_per_unit, note`; common variations like `qty`, `calories`, `best by` work too). Items with
the same name and location are updated, others added; tick *Replace* to start over from the
file. Bad rows are skipped and listed. Dates are `2027-03-31` (or `2027-03` for the end of that
month).

## Limits and privacy

- Up to 2000 items (setting). The list lives in one file on the Badger and is **sealed**
  (encrypted; see "Sealed records" in docs/SECURITY.md): a Badger that is taken shows nothing of
  what you have or where it's kept, once the owner has set an owner passphrase. Only family members
  can see it; the admin PIN alone doesn't open it.
- After a restart the Badger is **locked** until the owner unlocks it (Settings → Sealed records).
  Meanwhile you can still add items and record changes (+/−, use, edit, household): they are saved
  sealed and applied in order at the unlock. The page shows only what was added since it locked; the
  totals, rationing, the change log and the CSV export wait for the unlock.
- Numbers are estimates: calorie labels, how much people really eat and drink in heat or
  stress, and spoilage all vary. Keep a margin.
- Settings: `[supplies]` in `prepper.toml` (see the `[supplies]` section of `os/prepper.toml.example`).
