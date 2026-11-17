# 🤝 Trusted circle

A few households you have **met and trust**, whose Badgers share a private radio channel. When someone in the
circle needs help — power, water, a medic — their radio asks, and Badgers that can help say **who to ask**.
Nothing else: no lists, no amounts, no locations, no copies of anyone's records.

Why so strict: in a long emergency a town must never broadcast its strategic resources. A rogue neighbour,
a stolen radio or a leaked key must not turn a circle into a shopping list for raiders.

**Who:** the admin sets it up (Settings → *Trusted circle*). Family members can ask the circle and see the
circle link on their own device. Visitors see nothing.

## Setting it up

1. **Create** a circle (a plain channel name, like "Circle" — phones show it). The Badger gives its radio a new
   private channel with a strong random key.
2. **Share it in person.** The circle link (it holds the key) is shown on the Badger's screen (knob → *Family
   radio* → *Circle link (QR)*) and to family members on their own device over https — never on the admin page,
   never sent by message.
3. Other households **join** with that link on their own Badger (Settings → *Trusted circle* → *Join*; this needs
   `https://`, because the link holds the key). A phone with the Meshtastic app can scan it too.
4. Give this Badger a **name in the circle** — a nickname or a street ("Elm St"), never an address.
5. **Opt in**, per category: *Skills*, *Resources*, *Aid-station status*, *Missing persons*. Until you do, nothing
   is shared. Then add **offers**: a topic and a short line ("power — can fix generators"). With *Missing persons* on, a
   missing-person announcement the admin approves in the Guarded posture becomes such a line (no places) instead
   of a public broadcast ([missing.md](missing.md)); it is withdrawn when the person is found or the post removed.

## What circle members can learn — the rules

These are enforced in the Badger's code, not just recommended:

- **Pointers only.** An answer is one line: *"power: can fix generators — ask Elm St"*. Your records stay on
  your Badger; nothing is copied to the others.
- **Pull, not push.** Nothing is ever announced. A circle radio has to ask about **one topic**
  (`!help power` on the circle channel, or *Ask the circle* on another Badger), and your Badger answers **only
  that radio** (a direct message), after a random wait of a few seconds.
- **No sweeps.** Asking for everything at once ("all", "inventory", several topics) or about many different
  things in an hour is refused and **flagged** on your circle page.
- **Budgets.** At most 3 answers an hour to one radio, 10 to everyone, the same topic to the same radio once per
  30 minutes. Forged radio IDs can't get past the total. Every question is logged (in memory) with what was done.
- **No amounts.** Offers have no quantity field, and a line that looks like an amount ("20 gallons", "plenty of
  food", "5 generators") is refused. Aid-station status has no numbers ("open · water, first aid").
- **No addresses.** A line or a name that looks like an address, coordinates or a map hint asks you to confirm
  ("share anyway") before it can be shared.
- **Silence means no.** A Badger with nothing to offer says nothing — a "no" would tell someone something too.
- **Nothing in Lockdown** ([posture.md](posture.md)): no answers, no questions.
- **Blocked radios** get nothing, and their answers are ignored.
- Over-the-air text never says "Badger".

## Asking the circle

*Ask the circle → Who can help?* sends one sealed question for one topic. Answers (pointers) appear on the page
within a minute and are forgotten after a day. One question a minute, ten an hour.

Topics: power, water, food, medical, shelter, tools, comms, transport, childcare, aid, missing, other.

## When something goes wrong

A Badger or a radio from the circle is lost or stolen, or a household is no longer trusted:

1. **Block** its radio ID (Settings → *Trusted circle* → *Blocked radios*).
2. **New circle key.** Same safety as the family key change: a request is acted on once, never when it is old,
   and the new link is kept where the Badger's screen can always show it.
3. Show the **new link in person** to the households you keep. The others are out.

*Leave the circle* makes the radio forget the circle's key.

## How it works (radio)

The circle is a Meshtastic channel with its own 256-bit key (position precision 0, no internet uplink). The
bridge uses a free channel slot (or `[radio] mesh_circle_channel`). Questions and answers between Badgers are
`PRIVATE_APP` packets **sealed again** with a key derived from the circle's own secret (no fixed marker, random
padding to 24–200 bytes), on the circle channel only. Circle radios' text questions (`!help power`) get a direct
text reply. Details and limits: [MESH.md](../MESH.md).

## Privacy and limits

- Anyone holding the circle key can read circle traffic and ask questions (within the budgets) — that is what
  membership means. Share the link only in person; re-key after a loss.
- Radio IDs on the air are not proof of anything: a member can forge another member's ID. Budgets and blocks
  limit the damage; the key change ends it.
- The circle's link, the question log and the answers live in RAM only (`/run/prepper`). Your settings and
  offers are in `features/circles/` on the drive (sealed-records cover is up to that feature).
- Delivery of `PRIVATE_APP` unicast packets and of direct texts on a secondary channel needs checking on real
  radios.
