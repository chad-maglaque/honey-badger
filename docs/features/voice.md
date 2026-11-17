# Voice mode

Ask the Badger a question out loud on your phone and hear the answer. Use it when your hands are
busy, when typing is hard, or for someone who can't read small print.

Open it from the launcher: **More → 🎙️ Voice**.

## How to use it

1. Tap **🎙️ Tap to ask**, say your question (up to 30 seconds), and tap **Stop**.
2. The Badger shows what it heard ("You asked: …"), looks it up in its library, shows the answer
   with its sources, and reads it aloud. If the sound doesn't start by itself, tap ▶.
3. Tick **Longer answer** for a fuller (slower) reply. The default is two short sentences, which is
   quick to listen to.

The answers come from the same offline assistant as the **Ask** page, and only from articles on the
Badger. It can still be wrong, so check the sources, especially for anything medical.

If no AI model is installed (or it isn't answering), the Badger reads out the titles and first lines
of the best matching library articles instead.

## On plain `http://badger.lan`: use your phone's recorder

Browsers only let a web page use the microphone over a secure connection (`https://`, or on the
device itself). On the normal `http://` address the page shows **Record with your phone** instead:

- **Android** opens the sound recorder. Record, confirm, and it is sent.
- **iPhone** opens a file picker. Record your question in **Voice Memos** first, then pick it here.

For one-tap recording on any phone, install the Badger's certificate (**Add to your device** page),
then open **https://badger.lan**. Both ways work with Chrome (WebM/Opus), Safari (MP4/AAC), Firefox
(Ogg/Opus) and recorder files (m4a, mp3, 3gp/AMR, wav, flac).

## Limits

- One question is answered at a time, and one more can wait. If more people ask at once you see
  "The Badger is busy answering someone else. Try again in a minute."
- Up to 6 questions per device every 10 minutes. Recordings longer than 30 seconds are cut.
- Time per question on a Pi 5: about 2–4 s to understand a 10-second question, 10–60 s for the
  answer (longer on battery), and a few seconds to make the speech.
- English only (model `base.en`; voice `en_US-lessac-low`).

## Privacy

- Your recording is deleted as soon as it has been turned into text. The question, the answer and
  the spoken-answer file are kept in memory for 15 minutes, so your phone can fetch them, then
  deleted. They are linked to your device's network address, and other devices can't open them.
- Nothing is sent to the internet. Speech recognition (whisper.cpp), the AI (llama.cpp) and the
  voice (piper) all run on the Badger.
- Like the Ask page it is open to everyone on the Badger's Wi-Fi. An admin can restrict it to family
  members (`[voice] access = "member"`) or switch it off at any time (admin box at the bottom of the page).

## What it needs

- The speech software: ffmpeg, whisper.cpp with the `base.en` model, and piper with one small voice,
  about 300 MB. The installer adds it; on an older Badger run
  `sudo bash /opt/prepper/os/install.d/speech.sh` once while it has internet. Until then the page
  says so and points to the Ask page.
- Optional: an AI model (the normal Ask setup). Without one you get library matches read aloud.
- Uploads of up to 4 MB. The Badger's web server must accept bodies that large for `/api/voice/ask`
  (see the integration notes).

## For other features: speech helper

`software/speech/speechlib.py` is a small stdlib-only module any feature or service can use:

```python
import speechlib                                   # sys.path: software/speech
s = speechlib.Settings.from_config(ctx.cfg("voice"))
wav_bytes = speechlib.say("Boil water before drinking it.", s)       # piper → audio/wav bytes
text = speechlib.transcribe_file("/path/clip.m4a", s, max_seconds=30)  # ffmpeg + whisper.cpp
speechlib.available(s)                              # {"stt": bool, "tts": bool, "missing": [...], "hint": "..."}
```

Every program runs with a timeout and gets its text on stdin. Errors raise `speechlib.SpeechError`
with a readable message.

## Settings (`[voice]` in prepper.toml)

See the `[voice]` section of `os/prepper.toml.example`: `access`, `max_seconds`, `max_upload_mb`,
`per_ip_per_10min`, `queue`, `answer`, `allow_long`, `answer_timeout`, `speak`, `whisper_threads`,
`stt_timeout`, `tts_timeout`, `tts_max_chars`, and the program paths. Turn the feature off with
`[features] voice = false`.

## Hardware

Nothing extra: it uses the phone's microphone and speaker. A USB speaker and microphone on the Badger
itself is only needed for the planned push-to-talk on the front panel (not part of this feature yet).
