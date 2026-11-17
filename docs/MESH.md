# Many Badgers on one mesh

A Honey Badger is rarely alone. In a neighbourhood or a small town, several to dozens of Badgers
share one Meshtastic LoRa mesh: everyone's **public channel** (LongFast), plus each family's own
**private channel**. LoRa airtime is scarce — LongFast carries about 1 kbit/s, one 200-byte message
takes 1–2 seconds of air per hop, and every radio repeats it up to its hop limit. If every Badger
relayed every NOAA warning and answered every `!q`, ten Badgers would put ten copies of the same
tornado warning on the air at the worst possible moment.

So Badgers cooperate. This is called **mesh courtesy**; it is on by default
(`[radio] mesh_courtesy = true`) and needs no setup.

## What people see

- **One copy of each NOAA alert per channel**, whichever Badger's turn comes first (on the public
  channel an occasional second copy is possible — see *The safety rule*). The relay ends with a short
  tag such as `#a1b2` (the same on every Badger for the same alert).
- **One answer** when someone sends a command on a shared channel. Family radios (the family
  channel) can use `!help`, `!q`, `!ai` and `!status`, and a family radio's direct message is
  answered straight away. Radios outside the family can only use `!q` (by default), get **no reply at
  all** to anything else — no "turned off", no "family only", no help text — and at most a few
  answers an hour; their `!q` is answered in the open on the public channel, as a reply to the
  question, after the Badgers have taken turns. A stranger's direct message is answered (if at all)
  after a short wait, never at once.
- **One weather forecast** for all the Badgers listening to the same NOAA transmitter
  (twice a day at most).
- On the check-in board, an alert or forecast relayed by other Badgers appears **once**, even if
  several Badgers sent it.
- After a town-wide power cut, Badgers come back **quietly**: alerts that were already live wait a
  random 30–120 seconds, so the whole town doesn't transmit at once.
- The launcher's status shows a line like **"Badgers nearby: 3 · channel busy 18 %"** (or
  "· radio silence").
- Nothing the Badger sends over the air says "Badger" or "Honey Badger": not help text, replies,
  alert relays ("NOAA ⚠ Tornado Warning · … Tune a weather radio for details. #a1b2"), sensor alerts
  ("Sensors: ⚠ …", on the family channel by default), missing-person notes ("… — see the community
  board"; only in the Neighbourly posture — Guarded shares them with a trusted circle on request, Lockdown not at
  all) or forecasts ("Wx Tue: …").

## How it works

**Control packets — private channels only.** Badgers coordinate with three tiny packets on
Meshtastic's private-application port (PRIVATE_APP, 256), which chat apps don't show: a *beacon*
("a Badger is here"), a *claim* ("I'm about to send this") and a *done* ("that question is
answered"). They are sent **only on the family channel**, and on an optional **circle channel**
(`mesh_circle_channel`: a private channel with its own key that you share with neighbouring Badger
owners) — **never on the public channel**, and the bridge ignores any control packet heard there.
Each packet is sealed with a key derived from that channel's own secret (its key in the Meshtastic
app): there is no fixed marker, the bytes look random to anyone without that secret, and it is
padded to a random length typical of a short chat message (20–96 bytes, mostly 20–45). A family
channel with no key or one of Meshtastic's well-known default keys gets no control packets at all
(the bridge logs a warning — such a channel is not private).

**Finding each other.** A beacon goes out on those channels at random (Poisson) times averaging
`mesh_peer_beacon_min` minutes (45), never closer than a fifth of that or further apart than three
times — no rhythm a listener could lock on to. `mesh_beacons = false` turns beacons off. On the
public channel Badgers learn about each other only from what they hear anyway: tagged alert relays,
and public replies to a question they also saw. Badgers heard in the last 2 hours count as
"nearby". The list lives in RAM, `/run/prepper/mesh-peers.json` (0640): it survives a restart of the
bridge (Badgers heard in the last week are then assumed to still be there for the first hour), but not a
power-off — a seized Badger holds no list of other households.

**Taking turns.** For anything several Badgers would send:

- *On a private channel* (family / circle) each Badger waits a random back-off — longer the more
  Badgers it knows about (replies 1–10 s, alerts 5–45 s, community notes 10–120 s, automatic notes
  like the forecast 30 s–15 min) — sends a **claim**, waits ~6 s for a competing claim, then sends.
  If two claims cross, the radio with the lower node number goes ahead. It stands down if, while
  waiting, it **hears the message itself** from another Badger (or, for low-stakes things only, another
  Badger's claim or "done"). A claim that isn't followed by the message within 30 seconds or so is
  forgotten, so a Badger that goes quiet mid-relay can't swallow a message.
- *On the public channel* there are no claims. Every Badger puts itself and the Badgers it knows in
  an order derived from the message key and their radio IDs (the same order on every Badger, a
  different one for every message, so it is not always the same radio that transmits), and waits its
  turn: one turn every few seconds (alerts and replies 3–4 s, community notes 6 s, automatic notes
  20 s) after the kind's shortest wait, never longer than the kind's longest back-off. The first
  Badger's text is heard by the others, who stand down. If its relay is lost, the next one in turn
  sends it. Badgers that know different sets of peers may occasionally both send.

Replies to a family radio are direct messages, so after replying a Badger sends a "done" on the
family channel. Replies to a stranger on the public channel are broadcast as a reply to the question
(Meshtastic `replyId`), so the other Badgers hear them. When another Badger has answered a radio, the
others count that against the radio's 30-second cooldown. A Badger whose settings would only say
"turned off" to a family radio waits longer, so one that can answer does; a Badger that wouldn't
answer a stranger doesn't take part at all.

**Strangers and budgets.** Meshtastic sender IDs are not authenticated: anyone can transmit with
any ID, so a scanner could forge a new ID for every question to make a Badger transmit on demand (a
beacon to take bearings on, and a drain on airtime and battery). So radios outside the family get
at most `mesh_stranger_replies_per_hour` answers an hour **all together** (6 when Guarded, 12 Neighbourly,
0 Lockdown) and `mesh_stranger_replies_per_radio_hour` each (2 / 3 / 0); a "usage", "busy" or "turned off"
line is never sent to them. Set the total to 0 to answer only family radios.

**The safety rule.** A NOAA alert (or a sensor danger message) is cancelled **only by hearing exactly
the text this Badger would send** on that channel — the same event, areas, expiry and tag, compared
after normalising spaces, letter case and Unicode forms. Anything else never cancels it: not a
claim (on a private channel a claim can hold an alert back for at most a minute in total), not a
"done", not a bare `#tag`, not the older untagged format of Badgers that haven't been updated, and
not a text with the right tag but other words. A relay that wasn't sent because the identical text
was already heard is recorded in the log and the status file (who sent it), never silently dropped.

Why so strict: alert ids come from the public SAME header and the tag is computed with a published
constant, so anyone who decodes the same NOAA broadcast (a $25 SDR) can compute the tag. Before this
rule, a hostile radio could send "NOAA ⚠ Tornado Warning · CANCELLED, all clear #a1b2" — even before
the Badger decoded the alert — and every Badger in range would have stayed silent. Now that text is
relayed past (and shown on the board separately from the real alert). The cost: Badgers that format
the same alert slightly differently (another county list, an older version) each send their copy.
Two copies are safe; silence is not.

**What a forger can still do.** Everything on the air can be forged. A hostile radio can: make
Badgers quieter for low-stakes messages by sending the reply to a question first (one reply, by
`replyId`) or a forecast with its tag; send its own fake "alerts" (it can't stop the real one); send
the real alert's exact text early (neighbours then get the correct text from it, and Badgers stand
down); invent "Badgers" on the public channel by sending tagged alert copies or replies, which only
stretches turn waits within their caps; and make a Badger answer a stranger's `!q` — at most the
stranger budget per hour. On the public channel it can no longer hold, delay or cancel anything with
control packets. Someone holding the **family channel key** (a family phone, a former member before
the key is changed) can do more on that channel: claims (delay ≤ 1 min for alerts), "done"s
(low-stakes only), posts with any radio ID. After a member is removed, the Badger marks family-channel
posts "(unconfirmed)" and stops trusting the family channel for family-only commands until the family
channel has a new key (Settings → change the family radio key).

## Priorities, budgets and a busy channel

Everything a Badger transmits goes through one queue, in this order:

| Priority | Kind | Examples | Hop limit | Waits when the channel is busy? | Hourly budget (alone) | Expires after |
|---|---|---|---|---|---|---|
| 1 | `alert` | NOAA alert relays | radio default | never | none | when the alert ends |
| 2 | `danger` | sensor danger (flood, smoke) | radio default | never | (sensord's own limits) | 2 h |
| 3 | `family` | family board posts | radio default | never | 10 at once, then 1 per 15 s | 12 h |
| 4 | `reply` | answers to `!q`, `!help`… | 2 (or as far as the asker is) | never | 120 (strangers: 6, see above) | 5 min |
| 5 | `community` | community board posts, missing-person notes | radio default | above 40 % | 30 | 30 min |
| 6 | `auto` | forecast, sensor summaries | 2 | above 25 % | 4 | 2 h |

- **Budgets shrink as more Badgers are heard**: budget ÷ (1 + Badgers nearby ÷ 4)
  (`mesh_budget`, `mesh_budget_peer_scale`), never below 4 community / 10 replies / one automatic
  note per 4 hours. A town of 30 Badgers doesn't put 30 forecasts or 120 sensor summaries an hour
  on the air.
- **A busy channel**: the bridge reads the radio's own channel-utilization figure once a minute.
  Above `mesh_max_util` (25 %) automatic notes wait and community posts slow down; above
  `mesh_max_util_busy` (40 %) only alerts, sensor danger, family posts and replies go out. If our own
  share of airtime passes `mesh_max_air_tx` (8 %), automatic notes wait too.
- **Nothing goes out late**: something that waited past its expiry is dropped (and logged) instead.

## For feature authors (the outbox)

Features queue messages with `ctx.mesh_send(text, board)`; old calls keep working. Optional
keywords: `kind=` (one of the kinds above; default: the board's), `dedupe=` (a key every Badger
computes the same way — only one Badger on the mesh sends it; the bridge appends its `#tag`), and
`hop_limit=` (1–7). For `alert` and `danger` lines with a dedupe key, only another radio sending
exactly the same text (with its tag) stands this Badger down. Over-the-air text must not name the
device ("Badger"). Each line in `mesh-outbox.jsonl` is
`{"text", "board"}` plus optional `"kind"`, `"dedupe"`, `"hop_limit"`, `"at"` (when it was queued),
`"member"` (typed by a family member). Examples: the forecast uses
`kind="auto", dedupe="forecast:162.550:2026-11-17:AM"` (frequency, local date, half day), a
missing-person note `kind="community", dedupe="missing:<post id>:<status>"`, sensord marks danger
alerts `"danger"` (no dedupe: local readings are unique) and summaries `"auto"`.
The bridge never sends back out something it just heard on the mesh (no Badger-to-Badger loops).

## Privacy on the mesh

Trust no one on the air. **Anyone listening can learn:** that a radio exists and roughly where it
is (by taking radio bearings while it transmits), its radio ID and the names the radio gives itself,
everything sent on the public channel, and — for the family channel, whose content is encrypted —
when and how much it transmits (packet sizes and timing), and where from.

**What the Badger's own packets reveal:**

- *Public channel:* only texts a person could have sent — alert relays (with a `#tag`), forecasts,
  community posts, and answers to strangers' `!q` within the budget. No control packets, no
  "Badger" in any text. But a radio that relays every NOAA alert seconds after the broadcast, with a
  `#tag`, is recognisable as some kind of automatic relay by a patient listener; if that matters to
  you, set `mesh_alerts_public = false` (alerts then go to the family channel only) or use radio
  silence.
- *Family / circle channel:* encrypted by Meshtastic with the channel key; the Badger's control
  packets are additionally sealed with a key from that secret and padded to random lengths, and
  beacons come at random times, so to an outsider they look like occasional short chat messages.
  An outsider still sees that this radio transmits on a private channel now and then. Anyone holding
  the channel key (family phones) can read the family channel's texts, and — knowing this design — can
  recognise its control packets.
- Claims and "done"s carry an opaque 6-byte hash of what they are about (no alert id, sender or
  post id), inside the sealed packet.

**What the Badger hides by default:**

- No "Badger"/"Honey Badger" in anything it transmits; strangers get no help text and no refusals,
  and only a handful of answers an hour (forged IDs included).
- Sensor alerts and summaries go on the family channel (`[sensors] mesh_board = "family"`).
- The radio's own position on the public channel is **off** (`mesh_public_position = "off"`): at
  start the bridge sets the public channel's position precision to 0 (firmware 2.3+). Meshtastic
  keeps that many leading bits of latitude/longitude and reports the centre of the remaining box;
  the Meshtastic docs list 10 bits ≈ 23 km, 11 ≈ 12 km, **12 ≈ 5.8 km** (a box about 12 km across),
  13 ≈ 2.9 km, 14 ≈ 1.5 km, 16 ≈ 360 m, 32 = exact. `"coarse"` is 12 bits (within about 6 km);
  `"precise"` is exact; a number of bits may be given, but anything finer than 13 bits (≈ 3 km) is
  clamped to 13 — only the word `"precise"` ever shares the exact spot. The family channel keeps
  whatever you set in the Meshtastic app — the Badger never shares more than you chose. If you
  change the public setting in the app later, the Badger leaves it alone if it is coarser, but sets
  it back if it is finer than ~3 km without `"precise"` (it remembers what it set in
  `mesh-position-applied.json`).

**Radio silence (receive only).** Set `[radio] mesh_silent = true`, or create the file
`/var/lib/prepper/portal/mesh-silent` (`[portal] mesh_silent_file`; takes effect without a restart). The **Lockdown** posture turns radio silence on
for you (front panel → Posture, or the admin page). While it is on, the bridge transmits **nothing** on its own: no
alert relays, replies, beacons, claims, forecasts, sensor messages or community posts — it still
listens, logs and fills the boards. With `mesh_silent_allow_family = true` (default), a family post
a member typed and chose to send over the mesh still goes out on the family channel; set it to
`false` for total silence. Nothing held back during silence is sent afterwards.
The flag file: its existence means silence; it may contain JSON `{"until": <epoch seconds>,
"by": "<who>"}`, after which it stops counting (the bridge never deletes it). Empty or unreadable
content means silence.

Radio silence covers what the **Badger** sends. The Meshtastic radio itself still sends its own
"node info" and telemetry now and then, and repeats other people's messages — except in **Lockdown**,
where the bridge mutes the radio too (see *The security posture*). To be truly unfindable, switch the
radio off.

**What stays on the drive.** Nothing the radio tells the bridge: radio positions (`mesh-nodes.json`, none
older than 24 h), Badgers heard (`mesh-peers.json`), telemetry, the family and circle links and the Bluetooth
PIN all live in `/run/prepper` (RAM) and are read from the radio again after a restart (`[portal] run_dir`
and the per-file `[portal]` keys still override the paths). If one is pointed at persistent storage anyway — or
an old copy is left in `/var/lib/prepper/portal` from before — it is **sealed** (sealed records: the bridge writes
it sealed, and the launcher seals any plain copy it finds, on its next request), never kept in the clear. Family
posts heard on the radio go to a small spool in RAM (`/run/prepper/family-inbox.jsonl`), which the launcher seals
onto the family board within seconds with its current key — so family members can read them even while sealed
records are locked (the last 48 h / 200 posts). A reboot drops lines it hadn't taken in yet. Details:
[SECURITY.md](SECURITY.md) → "Family radio messages while locked".

**Advice:** give the radio an unremarkable long and short name (not "Honey Badger" or your family
name); give the family channel its own key (never a default one); keep position sharing off on the
public channel; keep `!help`, `!status` and `!ai` family-only (the default) — every reply on the
public channel is a transmission someone can take a bearing on; set `mesh_stranger_replies_per_hour
= 0` to answer only family radios; use radio silence when you don't want to be found.

## The security posture

One switch — Neighbourly / **Guarded** / Lockdown ([features/posture.md](features/posture.md)) — sets the
radio's behaviour: who may use each command, the stranger budgets, the public position, radio silence and
NOAA relays on the public channel. The bridge re-reads `posture.json` (and the launcher's settings) every couple
of seconds; no restart. A `[radio]` key below that you set yourself overrides the posture, except that in
Lockdown nothing may be looser than Lockdown (an explicit `mesh_silent = false` is ignored then).

**Lockdown mutes the radio itself.** Radio silence covers what the Badger sends; in Lockdown the bridge also sets
the radio's device role to **CLIENT_MUTE** (it stops repeating other people's packets) and its node-info,
position, telemetry and neighbour-info broadcasts off (the longest interval the field takes: `0` would mean
"the firmware default"). It saves the radio's previous values first
(`/var/lib/prepper/portal/mesh-radio-saved.json`, 0640) and puts them back when Lockdown ends. Idempotent
(a second run writes nothing — the radio reboots after a device-config write, the bridge reconnects and finds
it already muted); settings the radio/library doesn't have are skipped and logged; one settings transaction
where the library has one (one reboot). `[security] lockdown_radio = false` leaves the radio alone.

## The trusted circle

A circle is a private channel with its own key shared in person between vetted households
([features/circles.md](features/circles.md)). The admin page asks the bridge (request file
`mesh-radio-req.json`, result `mesh-radio-res.json` without any key, with the family key change's failsafes:
acted on once, never when older than 2 minutes) to **create** it (a random 256-bit key on a free channel slot,
or `mesh_circle_channel`; position precision 0, no uplink/downlink), **join** one from a link, **re-key** it, or
**leave**. The circle's link (that channel only, never the family channel or LoRa settings) is kept in RAM,
`/run/prepper/mesh-circle.json` (0600), for the front panel and family members.

On the circle channel only:

- a circle radio's text `!help <topic>` is a question; a Badger with an opted-in offer for that topic answers
  that radio alone, by direct message, after 2–10 s: `power: can fix generators — ask Elm St`;
- Badgers ask each other with a sealed `PRIVATE_APP` *query* (`nonce(6) · mac(8) · body XOR keystream · random
  padding`, 24–200 bytes; key = HMAC(circle secret, "trusted circle pointers v1"); body: version, type, a random
  4-byte question id, a topic code) broadcast on the circle channel; answers are sealed *answer* packets
  unicast to the asker, carrying the question id and the pointer text;
- refused, with nothing sent: blocked radios, Lockdown, unknown topics, nothing to offer, the same topic again
  within 30 min; refused **and flagged**: "all"/"inventory"-style words, several topics at once, more than 4
  topics from one radio in an hour; budgets 3 answers/radio/hour, 10 in total, our own questions 6 an hour;
- the log of questions and the answers we got are in RAM (`/run/prepper/circle-status.json`).

Courtesy beacons/claims also use the circle channel (sealed with a different derived key).

## Settings (`[radio]` in prepper.toml)

| Key | Default | Meaning |
|---|---|---|
| `mesh_courtesy` | `true` | `false`: act alone as before (every Badger relays and answers everything) |
| `mesh_commands` | posture (Guarded: `{ help = "family", q = "community", ai = "family", status = "family" }`) | who may use each command: `community`, `family`, `off` (family told it's off) or `ignore` (no reply to anyone) |
| `mesh_stranger_replies_per_hour` | posture (Guarded `6`) | answers to radios outside the family, all together (0–60) |
| `mesh_stranger_replies_per_radio_hour` | posture (Guarded `2`) | …and to any one of them (0–10) |
| `mesh_beacons` | `true` | `false`: no beacons |
| `mesh_peer_beacon_min` | `45` | average minutes between beacons (10–720), at random times |
| `mesh_circle_channel` | `-1` | the trusted circle's private channel (pointers on request + control packets); `-1` = none, or the slot the bridge picked when the circle was created/joined |
| `mesh_max_util` / `mesh_max_util_busy` | `25` / `40` | channel-busy thresholds (%) |
| `mesh_max_air_tx` | `8` | our own airtime share (%) above which automatic notes wait |
| `mesh_budget` | `{ community = 30, reply = 120, auto = 4 }` | messages per hour for a Badger alone |
| `mesh_budget_peer_scale` | `4` | budget ÷ (1 + Badgers nearby ÷ this) |
| `mesh_boot_quiet_s` | `[30, 120]` | random wait for alerts already live at start; `0` = off |
| `mesh_hop_limit_local` | `2` | hop limit for replies and automatic notes |
| `mesh_silent` | posture (Lockdown: on) | radio silence |
| `mesh_silent_allow_family` | `true` | during silence, still send member-typed family posts |
| `mesh_public_position` | posture (Neighbourly `"coarse"`, else `"off"`) | `"off"`, `"coarse"` (12 bits, ~6 km), `"precise"`, or bits 0–13 (finer is clamped to 13) |
| `mesh_alerts_public` | posture (Lockdown: family only) | NOAA alert relays on the public channel too |

`mesh_beacon_public` is gone: control packets never go on the public channel (a warning is logged if
it is still set).

## Limits — what only a real multi-radio test can confirm

The behaviour above is tested in a simulation of up to 20 Badgers on a fake channel. On real radios,
check: that PRIVATE_APP packets on the family channel are delivered to the other Badgers' bridges
(pubsub topic `meshtastic.receive`, `decoded.portnum = "PRIVATE_APP"`, `decoded.payload` as bytes)
and that the meshtastic library exposes the channel key (`localNode.channels[i].settings.psk`) the
seal is derived from (otherwise a fixed key is used and logged); whether `sendText` accepts `hopLimit`
and `replyId` (the bridge checks and falls back silently — without `replyId`, public answers to
strangers can't be recognised by other Badgers and an occasional second answer goes out); that
other Badgers receive a broadcast reply's `replyId`; how long a public-channel text takes to cross
three hops (turns are 3–4 s apart; slower paths mean occasional duplicates); that the encrypted
length of a padded PRIVATE_APP packet really overlaps that of short texts on the air; that
`deviceMetrics.channelUtilization` / `airUtilTx` are reported for the local node; and that the
firmware honours per-channel `position_precision` (and what the phone apps show for 12 and 13 bits).
