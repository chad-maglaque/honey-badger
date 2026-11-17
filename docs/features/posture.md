# 🛡️ Security posture

One switch for **how much this Badger shows the world**: Neighbourly, **Guarded** (the default) or
Lockdown. It sets the radio, the Wi-Fi, what visitors can open and what is shared with a trusted circle,
all at once, so nobody has to remember fifteen settings in an emergency.

**Who:** the admin (Settings → Badger administration → *Security posture*) or someone at the Badger's own
screen (knob → *Posture*, behind the panel code if one is set). Family members can see the current posture
(🛡 in the status bar); visitors can't.

## The three levels

| | Neighbourly | **Guarded** (default) | Lockdown |
|---|---|---|---|
| Radio commands from radios outside the family | `!help`, `!q` | `!q` only | none |
| Answers to strangers per hour (all / one radio) | 12 / 3 | 6 / 2 | 0 / 0 |
| Radio position on the public channel | rough (~6 km) | off | off |
| Radio silence (nothing sent but family posts you type) | no | no | **yes** |
| NOAA alerts relayed on the public channel | yes | yes | family channel only |
| Visitors' "send over radio" requests | need a family member's OK | need a family member's OK | off |
| Neighbourhood pages (board, skills, missing, aid station) for visitors | everyone | everyone | family only |
| Aircraft and satellite pages for visitors | everyone | everyone | family only |
| Sensors for visitors | alerts only | alerts only | hidden |
| Sharing with a trusted circle | what you opt in | what you opt in | **nothing** |
| The radio itself | as set | as set | **muted**: no repeating, no "here I am" announcements, no position or telemetry broadcasts |
| Wi-Fi | your choice | your choice | **password required** (or off) |
| Logs | in memory (on disk only if you ask) | in memory only | in memory only |

Never, in any posture: amounts of anything over the radio, the house's exact position to visitors, power or
occupancy to visitors, "Badger" in anything sent over the air.

## Your own settings still count — except in Lockdown

The posture is the starting point. A setting you made yourself (in `prepper.toml`, or on the Settings page —
for example *Radio commands*) wins, and the posture page shows it as "your setting". **In Lockdown, a setting
that would show more than Lockdown allows is ignored** (shown as such): Lockdown means lockdown. Two settings
are Lockdown's own options and always count:

- `[security] lockdown_wifi = "password"` (default) | `"off"` | `"keep"` — what Lockdown does to a Wi-Fi
  without a password;
- `[security] lockdown_radio = false` — don't touch the radio's own settings (for a radio you also use as a
  town repeater, say).

*Use the posture's own radio commands and visitor settings* on the posture page clears your Settings-page
choices.

## Lockdown, step by step

1. The posture is saved (`/var/lib/prepper/portal/posture.json`) and every part of the Badger follows within
   seconds — no restart.
2. **The radio goes quiet.** Radio silence; a post a family member types on the family board and chooses to
   send still goes out on the family channel (`[radio] mesh_silent_allow_family`). The mesh bridge also mutes
   the radio itself: device role **CLIENT_MUTE** (it stops repeating other people's packets), and its node-info,
   position, telemetry and neighbour-list broadcasts are switched off (set to the longest interval). The
   settings it had are saved first and **put back when you leave Lockdown**. A radio that doesn't have one of
   these settings is left as it is (the bridge logs what it applied and what it skipped).
3. **The Wi-Fi gets a password** if it had none (an "enhanced open" or open hotspot): the usual safe password
   change — the new password is on the Badger's screen (knob → *Join Wi-Fi*), it switches on about 10 seconds
   later, and **it switches back by itself unless someone rejoins with it and taps *Keep* within 10 minutes**.
   Or choose *Lockdown and turn the Wi-Fi off*.
4. **Visitors see only the library**, maps and tools. Family members (their own device key) and aid-station
   volunteers (their own aid code) carry on as before.
5. **Nothing goes to the trusted circle**: no answers, no questions.
6. Sealed records lock themselves (that feature reads `posture.json`).
7. A note on the family board says the posture changed.

Leaving Lockdown restores the radio's settings and the rest of the table; the Wi-Fi password stays.

## The front panel

The home screen shows **Posture: Guarded** (or the current one). *Posture* in the menu (behind the panel code)
shows the current level, lets you pick another (each behind a yes/no) — including *Lockdown, Wi-Fi off* — and
opens *What it reveals* ([reveal.md](reveal.md)).

## For other programs

`posture.json` is the contract: `{"level": "neighbourly" | "guarded" | "lockdown", "changed_at": <epoch>, "by":
"admin" | "front panel"}`, written atomically (0640) by the portal only. No file: `[security] posture` (default
`"guarded"`). A file that exists but can't be read or makes no sense counts as **Lockdown** (fail closed).
The rules table and the override logic are in `software/common/posture.py` (stdlib only).

## Settings

| Key | Default | Meaning |
|---|---|---|
| `[security] posture` | `"guarded"` | the level when none was chosen yet |
| `[security] lockdown_wifi` | `"password"` | Lockdown and a password-free hotspot: `password`, `off`, `keep` |
| `[security] lockdown_radio` | `true` | mute the radio itself in Lockdown |
| `[security] persistent_logs` | `false` | keep the system journal on disk — only while Neighbourly |
| `[security] visitor_community` / `visitor_radio_pages` | (posture) | `"public"` or `"members"` |
| `[portal] posture_file` | `/var/lib/prepper/portal/posture.json` | where the posture is kept |

## Limits

- Radio silence and the mute cover what the Badger and its radio send. Turning the radio off is the only way
  to be sure it sends nothing.
- The radio settings applied in Lockdown, and whether every firmware accepts CLIENT_MUTE and the longest
  intervals, need checking on real radios (the bridge never crashes over it, and says what it couldn't do).
- Changing the posture is logged on the family board; it isn't secret from the family.
