# NOAA forecast as text

NOAA Weather Radio reads the forecast aloud over and over. The Badger records a few minutes of that
broadcast now and then, writes it down, and keeps it for everyone to read on their phone. You don't
have to stand next to a radio waiting for your county to come round again.

Open it from the launcher: **Radio & sky → 📻 NOAA forecast**.

## What you see

- **The latest written forecast**, with the time it was recorded. It is split into parts where the
  Badger can tell them apart: hazardous weather outlook, watches and warnings, the forecast period by
  period (Today, Tonight, Wednesday…), current conditions, marine forecast, and so on.
- **In short**: a one-line summary, like `Wx Tue: storms likely PM, hi upper 70s, S 15-25 mph g35. Tonight: lo 61.`
- The **word-for-word transcript**, folded away, in case the tidy version missed something.
- **Older versions**, so you can see how the forecast has changed.

> **It is transcribed automatically and may contain errors.** A computer turns the radio voice into
> text, and it can mishear a number or a place name. For warnings, listen to a weather radio and
> watch for the red alert banner. Alerts are decoded separately and don't depend on this page.

## Sharing

When a **new** forecast arrives (not the same one heard again), the Badger:

- posts it on the **community check-in board** as "Weather Radio": on by default, at most once every 6 hours;
- sends the one-line summary to the **mesh community channel**: on by default, at most twice a day,
  because LoRa airtime is shared and slow. This only reaches the mesh if the community board is
  bridged to it (`[radio] mesh_public_channel` is not -1).

An administrator can turn either one off, change how often the Badger listens, or tap **Update now**,
all from the admin box at the bottom of the page.

The one-line summary is written by the Badger's offline AI when it is installed and answers within
90 seconds. Every number in it is checked against the forecast. If the AI is missing, slow, or adds a
number that isn't in the forecast, simple built-in rules write the summary instead.

## How it works

Every hour a background job (`prepper-forecast`) checks whether a capture is due (every 3 hours by
default). It asks the weather-alert receiver to save about 6 minutes of what it hears. This is a copy
of the audio, so alert listening never stops. It then:

1. checks the recording for silence or static, and stops with "no clear signal" if that's all it got;
2. turns the speech into text with **whisper.cpp** (model `base.en`, quantized) on 2 CPU cores at low
   priority, so the AI, the launcher and Voice mode stay responsive. On a Pi 5, 6 minutes of audio
   takes roughly 2 to 5 minutes;
3. removes the repeats (the broadcast loops, so a 6-minute recording usually holds parts of the
   loop twice), splits the text into parts and puts the forecast periods in order;
4. saves it, and keeps the last 40 captures.

The recording itself is deleted when the transcription is done. It is held in memory (`/run`) and
never written to the drive.

**Why `base.en`?** The NOAA voice is synthetic, clear and English-only, which a small English model
handles well. `small.en` is about 3× slower: on a Pi 5 that is shared with the AI model it would hold 2
cores for 10–15 minutes every 3 hours, for little gain on this kind of audio. `tiny.en` makes too many
mistakes with numbers. The 5-bit quantized `base.en` file is about 57 MB and uses about 200 MB of RAM
while it runs.

On battery in **Endurance** mode the scheduled captures are skipped. An admin's **Update now** still works.

## What it needs

- The weather-radio SDR (**SDR 1**, the "SDR" jack with the 162 MHz whip) and its alert receiver
  running. Every Badger has it. SDR 2, for aircraft and satellites, is not involved. Only if SDR 2 is
  missing or has failed does the Badger lend SDR 1 to satellite passes; a capture that falls in such a
  pass says so and tries again within the hour.
- The speech software (about 300 MB including the voice for Voice mode). The installer adds it; on an
  older Badger, run `sudo bash /opt/prepper/os/install.d/speech.sh` once while it has internet. Until
  then the page says speech recognition isn't installed.
- A NOAA transmitter in range, with `[radio] weather_freq_mhz` set to it (see Tools → Radio).

## Messages you may see

| Message | Meaning |
|---|---|
| No clear signal (static / silence) | The receiver heard noise or nothing. Check the whip antenna on the SDR jack and the frequency. |
| The weather radio isn't listening | No SDR plugged in, or the alert receiver is stopped. |
| Lent to a satellite pass | SDR 2 is missing or broken, so SDR 1 is shared. It tries again within the hour. |
| No clear forecast could be heard | There was sound, but not enough weather words: a weak or noisy signal. |
| Skipped to save battery | Endurance mode. |
| Speech recognition isn't installed | Run the speech installer (see above). |

## Settings (`[forecast]` in prepper.toml)

See the `[forecast]` section of `os/prepper.toml.example`: `interval_hours`, `record_minutes`, `keep`,
`skip_on_endurance`, `post_board`, `board_min_hours`, `post_mesh`, `mesh_per_day`, `post_max_age_hours`,
`short_ai`, `ai_timeout`, `whisper_threads`, `transcribe_timeout`, and the program paths. Turn the
feature off with `[features] forecast = false`.

To try it on a recording you already have: `sudo -u prepper python3 /opt/prepper/software/speech/forecastd.py --file capture.wav`.

## Privacy

Only the public NOAA broadcast is recorded. Nothing leaves the Badger except the posts described
above, and those go only to the board and the mesh.
