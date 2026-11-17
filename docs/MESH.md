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

- **One copy of each NOAA alert per channel**, whichever Badger hears it first. The relay ends with a
  short tag such as `#a1b2` (the same on every Badger for the same alert) so Badgers recognise each
  other's relays.
- **One answer** when someone sends `!q`, `!help`, `!status` or `!ai` on a shared channel. The Badger
  nearest to the asker usually answers first. A question sent as a direct message to one Badger is
  answered by that Badger only, straight away.
- **One weather forecast** for all the Badgers listening to the same NOAA transmitter
  (twice a day at most).
- On the check-in board, an alert or forecast relayed by other Badgers appears **once**, even if
  several Badgers sent it.
- After a town-wide power cut, Badgers come back **quietly**: alerts that were already live wait a
  random 30–120 seconds, so the whole town doesn't transmit at once.
- The launcher's status shows a line like **"Badgers nearby: 3 · channel busy 18 %"** (or
  "· radio silence").

## How it works

**Finding each other.** Every ~45 minutes (`mesh_peer_beacon_min`, with random jitter) a Badger
sends a 6-byte "a Badger is here" beacon on Meshtastic's private-application port (PRIVATE_APP, 256),
which chat apps don't show. By default the beacon goes on the **family channel only**
(`mesh_beacon_public = false`). On the public channel Badgers learn about each other from the
messages they already exchange: the tiny "done" after a reply, and tagged alert relays. Badgers heard
in the last 2 hours count as "nearby". The list lives in `/var/lib/prepper/portal/mesh-peers.json`
(0640) and survives restarts: after a restart, Badgers heard in the last week are assumed to still
be there for the first hour.

**Taking turns.** For anything several Badgers would send, each Badger:

1. waits a random back-off — longer the more Badgers it knows about (replies 1–10 s, alerts 5–45 s,
   community notes 10–120 s, automatic notes like the forecast 30 s–15 min). The first Badger to
   speak is the one with the shortest wait, so with many Badgers the first copy still goes out fast;
2. sends a **claim** ("I'm about to send this"), waits ~6 s for a competing claim, then sends.
   If two claims cross, the radio with the lower node number goes ahead and the other stands back;
3. stands down if, while waiting, it **hears the message itself** from another Badger (or, for
   low-stakes things only, another Badger's claim or "done"). A claim that isn't followed by the
   message within 30 seconds or so is forgotten, and someone else sends it — a Badger that goes
   quiet mid-relay can't swallow a message.

Replies are direct messages that other Badgers can't hear, so after replying a Badger sends a tiny
"done" that says the question is answered. When another Badger has answered a radio, the others
count that against the radio's 30-second cooldown, so a radio that floods questions gets one answer
per cooldown from the whole mesh, not one from each Badger. A Badger whose settings would only
refuse a command ("for family radios", "turned off") waits longer, so one that can answer does.

**The safety rule.** A NOAA alert is cancelled **only by hearing the relayed alert text itself**
on that channel (our tagged format, or the older untagged format of Badgers that haven't been
updated). A claim can hold an alert back for at most a minute in total, a "done" or a bare `#tag`
never stops it, and a relay that wasn't sent because another Badger already sent it is recorded in
the log and the status file (who sent it), never silently dropped. Everything on the air can be
forged — anyone can transmit with any radio ID — so the design assumes hostile radios: the worst a
forger can do is make Badgers quieter for low-stakes messages (a reply, a forecast), or delay an alert
by a minute or two.

## Priorities, budgets and a busy channel

Everything a Badger transmits goes through one queue, in this order:

| Priority | Kind | Examples | Hop limit | Waits when the channel is busy? | Hourly budget (alone) | Expires after |
|---|---|---|---|---|---|---|
| 1 | `alert` | NOAA alert relays | radio default | never | none | when the alert ends |
| 2 | `danger` | sensor danger (flood, smoke) | radio default | never | (sensord's own limits) | 2 h |
| 3 | `family` | family board posts | radio default | never | 10 at once, then 1 per 15 s | 12 h |
| 4 | `reply` | answers to `!q`, `!help`… | 2 (or as far as the asker is) | never | 120 | 5 min |
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
`hop_limit=` (1–7). Each line in `mesh-outbox.jsonl` is
`{"text", "board"}` plus optional `"kind"`, `"dedupe"`, `"hop_limit"`, `"at"` (when it was queued),
`"member"` (typed by a family member). Examples: the forecast uses
`kind="auto", dedupe="forecast:162.550:2026-11-17:AM"` (frequency, local date, half day), a
missing-person note `kind="community", dedupe="missing:<post id>:<status>"`, sensord marks danger
alerts `"danger"` (no dedupe: local readings are unique) and summaries `"auto"`.
The bridge never sends back out something it just heard on the mesh (no Badger-to-Badger loops).

## Privacy on the mesh

Trust no one on the air. **Anyone listening can learn:** that a radio exists and roughly where it
is (by taking radio bearings while it transmits), its radio ID and the names the radio gives itself,
and everything sent on the public channel. The family channel's content is encrypted, but its
traffic can still be seen and located.

**What the Badger hides by default:**

- Badger-to-Badger packets carry nothing identifying: no names, positions, kinds, capabilities,
  counts or readable keys — a claim is 11 bytes holding an opaque 6-byte hash (HMAC-SHA256 with a
  fixed salt) of what it is about; a beacon is 6 bytes holding only the protocol version.
- Beacons go on the family channel only (`mesh_beacon_public = false`). Alert dedupe on the public
  channel works without them: Badgers recognise the relayed alert text itself.
- Alert relays no longer say "Badger": they end "Tune a weather radio for details."
- The radio's own position on the public channel is **off** (`mesh_public_position = "off"`): at
  start the bridge sets the public channel's position precision to 0 (firmware 2.3+). `"coarse"`
  shares it to about 12 km, `"precise"` exactly. The family channel keeps whatever you set in the
  Meshtastic app — the Badger never shares more than you chose. If you change the public setting in
  the app later, the Badger leaves it alone (it remembers what it set in `mesh-position-applied.json`).

**Radio silence (receive only).** Set `[radio] mesh_silent = true`, or create the file
`/var/lib/prepper/portal/mesh-silent` (`[portal] mesh_silent_file`; the front panel or admin page
can do this without a restart). While it is on, the bridge transmits **nothing** on its own: no
alert relays, replies, beacons, claims, forecasts, sensor messages or community posts — it still
listens, logs and fills the boards. With `mesh_silent_allow_family = true` (default), a family post
a member typed and chose to send over the mesh still goes out on the family channel; set it to
`false` for total silence. Nothing held back during silence is sent afterwards.
The flag file: its existence means silence; it may contain JSON `{"until": <epoch seconds>,
"by": "<who>"}`, after which it stops counting (the bridge never deletes it). Empty or unreadable
content means silence.

Radio silence covers what the **Badger** sends. The Meshtastic radio itself still sends its own
"node info" and telemetry now and then, and repeats other people's messages, unless you change that
in the Meshtastic app (for example the device role, and turning off position and telemetry
broadcasts). To be truly unfindable, switch the radio off.

**Advice:** give the radio an unremarkable long and short name (not "Honey Badger" or your family
name); keep position sharing off on the public channel; set `!status` and `!ai` to family-only (the
default) — any reply on the public channel tells listeners a Badger is there; use radio silence when
you don't want to be found.

## Settings (`[radio]` in prepper.toml)

| Key | Default | Meaning |
|---|---|---|
| `mesh_courtesy` | `true` | `false`: act alone as before (every Badger relays and answers everything) |
| `mesh_peer_beacon_min` | `45` | minutes between beacons (10–720) |
| `mesh_beacon_public` | `false` | also beacon on the public channel |
| `mesh_max_util` / `mesh_max_util_busy` | `25` / `40` | channel-busy thresholds (%) |
| `mesh_max_air_tx` | `8` | our own airtime share (%) above which automatic notes wait |
| `mesh_budget` | `{ community = 30, reply = 120, auto = 4 }` | messages per hour for a Badger alone |
| `mesh_budget_peer_scale` | `4` | budget ÷ (1 + Badgers nearby ÷ this) |
| `mesh_boot_quiet_s` | `[30, 120]` | random wait for alerts already live at start; `0` = off |
| `mesh_hop_limit_local` | `2` | hop limit for replies and automatic notes |
| `mesh_silent` | `false` | radio silence |
| `mesh_silent_allow_family` | `true` | during silence, still send member-typed family posts |
| `mesh_public_position` | `"off"` | `"off"`, `"coarse"` or `"precise"` |

## Limits — what only a real multi-radio test can confirm

The behaviour above is tested in a simulation of up to 40 Badgers on a fake channel. On real radios,
check: that PRIVATE_APP packets are delivered to the other Badgers' bridges (pubsub topic
`meshtastic.receive`, `decoded.portnum = "PRIVATE_APP"`, `decoded.payload` as bytes); whether the
installed meshtastic library's `sendText` accepts `hopLimit` and `replyId` (the bridge checks and
falls back silently); that `deviceMetrics.channelUtilization` / `airUtilTx` are reported for the
local node; that the firmware honours per-channel `position_precision`; and how long a claim takes to
cross three hops (the bridge waits 6 s; with slower multi-hop paths an occasional duplicate is possible).
