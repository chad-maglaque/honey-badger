#!/usr/bin/env python3
"""
forecastd — NOAA Weather Radio forecast → written text. Run by prepper-forecast.service (a oneshot):
hourly from prepper-forecast.timer, and at once when the launcher's admin taps "Update now"
(prepper-speech-forecast-now.path sees the request file).

One run:
  1. Due? ([forecast] interval_hours since the last attempt, default 3; "Update now" skips this.)
     Skipped in Endurance power mode unless asked by hand.
  2. Record ~6 min of the broadcast through alertd's audio tap: write /run/prepper/noaa-tap.request
     {"until", "pid"}; alertd saves what it hears to /run/prepper/noaa-tap/<start>.wav. The tap never
     takes the dongle from alert listening (see software/alertd/alertd.py).
  3. Silence or static (no carrier)? → "no clear signal", stop.
  4. ffmpeg → 16 kHz, whisper.cpp → text (2 threads, nice 15: llama-server and the portal come first).
  5. Clean: drop the repeated loop, split into sections, spot whether the forecast is new.
  6. Short version (≤ 200 bytes) for the mesh: the on-device AI if it answers in time and its
     numbers all come from the forecast, else rules (forecast_text.short_summary).
  7. Save history/<id>.json, latest.json, index.json, status.json in the portal's feature folder
     (/var/lib/prepper/portal/features/forecast). The portal feature reads them and posts.

  forecastd.py [--force] [--file capture.wav] [--config /etc/prepper/prepper.toml]
--file transcribes a saved capture instead of recording (to try it on real audio).
Exit 0 for every expected outcome (no radio, no signal, nothing due); 1 only on a crash.
"""
from __future__ import annotations

import argparse
import array
import contextlib
import fcntl
import json
import os
import re
import secrets
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
import forecast_text as FT  # noqa: E402
import speechlib  # noqa: E402

try:
    import sdrlease  # noqa: E402  (software/common: is the weather dongle lent out right now?)
except ImportError:                                   # pragma: no cover — always shipped together
    sdrlease = None

TAP_REQ = Path(os.environ.get("NOAA_TAP_REQUEST", "/run/prepper/noaa-tap.request"))
TAP_DIR = Path(os.environ.get("NOAA_TAP_DIR", "/run/prepper/noaa-tap"))
BEAT = Path(os.environ.get("ALERTD_BEAT", "/run/prepper/alertd.ok"))
POWER = Path(os.environ.get("POWER_FILE", "/run/prepper/power.json"))
REQUEST_NAME = "update-now.request"
PROMPT = ("NOAA Weather Radio. The National Weather Service forecast. Today, mostly sunny. Highs in the "
          "upper 70s. South winds 10 to 15 mph. Tonight, a 40 percent chance of thunderstorms.")
AI_SYSTEM = ("You write very short weather summaries for a text-message radio network. Use ONLY facts in "
             "the forecast given. One line, plain ASCII, at most 160 characters, no emoji, no quotes. "
             "Abbreviate like: Wx Tue: storms likely PM, hi 78, S wind 15-25. Tonight lo 61.")


def now() -> float:
    return time.time()


# ─────────────────────────────── config & files ──────────────────────────────

class Conf:
    def __init__(self, raw: dict):
        f = raw.get("forecast", {}) if isinstance(raw.get("forecast"), dict) else {}
        p = raw.get("portal", {}) if isinstance(raw.get("portal"), dict) else {}
        num = lambda k, d, lo, hi: speechlib._int(f.get(k), d, lo, hi)  # noqa: E731
        self.enabled = (raw.get("features", {}) or {}).get("forecast") is not False and f.get("enabled") is not False
        self.interval_h = num("interval_hours", 3, 1, 48)
        self.record_s = num("record_minutes", 6, 1, 14) * 60
        self.keep = num("keep", 40, 2, 500)
        self.threads = num("whisper_threads", 2, 1, 8)
        self.stt_timeout = num("transcribe_timeout", 1800, 60, 7200)
        self.short_ai = f.get("short_ai", True) is not False
        self.ai_timeout = num("ai_timeout", 90, 5, 600)
        self.skip_endurance = f.get("skip_on_endurance", True) is not False
        radio = raw.get("radio", {}) if isinstance(raw.get("radio"), dict) else {}
        self.radio_off = radio.get("sdr_weather") == "off"
        self.freq = radio.get("weather_freq_mhz", 162.55)
        self.llama = str(p.get("llama", "http://127.0.0.1:8090/ai")).rstrip("/")
        base = Path(p.get("features_data", "/var/lib/prepper/portal/features"))
        self.data = Path(f.get("data_dir", str(base / "forecast")))
        self.speech = speechlib.Settings.from_config(f, whisper_threads=self.threads, stt_timeout=self.stt_timeout)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def set_status(c: Conf, state: str, message: str, **extra) -> None:
    st = read_json(c.data / "status.json", {}) or {}
    st.update(state=state, message=message, at=now(), **extra)
    if state not in ("running", "busy"):            # "busy": try again at the next hourly timer
        st["last_attempt"] = st["at"]
    write_json(c.data / "status.json", st)


def overrides(c: Conf) -> None:
    """Admin settings saved from the launcher (settings.json) beat prepper.toml."""
    s = read_json(c.data / "settings.json", {}) or {}
    if isinstance(s, dict) and isinstance(s.get("interval_hours"), int) and 1 <= s["interval_hours"] <= 48:
        c.interval_h = s["interval_hours"]


# ─────────────────────────────── recording ───────────────────────────────────

def weather_dongle_lent() -> bool:
    """Every Badger has two SDRs; the weather one (SDR 1) is only lent out — to a satellite pass — when
    SDR 2 is missing or has failed (the single-dongle fallback in sdrlease/alertd)."""
    if sdrlease is None:
        return False
    try:
        return sdrlease.borrow_request("weather") is not None
    except OSError:
        return False


def radio_listening() -> bool:
    """alertd touches its heartbeat every 30 s while it receives."""
    try:
        return now() - BEAT.stat().st_mtime < 120
    except OSError:
        return False


def record(seconds: int, poll: float = 2.0, grace: float = 60.0) -> Path | None:
    """Ask alertd's tap for `seconds` of audio; the finished WAV, or None if none arrived."""
    t0 = now()
    TAP_REQ.parent.mkdir(parents=True, exist_ok=True)
    write_json(TAP_REQ, {"until": t0 + seconds, "pid": os.getpid()})
    try:
        deadline = t0 + seconds + grace
        while now() < deadline:
            time.sleep(poll)
            if now() < t0 + seconds:
                continue
            best = None
            for f in TAP_DIR.glob("*.wav"):
                try:
                    start = int(f.stem)
                except ValueError:
                    continue
                # alertd starts the file within ~1 s of seeing the request (it checks once a second)
                if int(t0) - 1 <= start <= int(t0) + 60 and (best is None or start > int(best.stem)):
                    best = f
            if best is not None:
                return best
        return None
    finally:
        with contextlib.suppress(OSError):
            d = json.loads(TAP_REQ.read_text())
            if d.get("pid") == os.getpid():
                TAP_REQ.unlink()


def read_pcm(path: Path, max_s: int = 15 * 60) -> tuple[array.array, int]:
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if width != 2:
            raise ValueError("not 16-bit audio")
        raw = w.readframes(min(w.getnframes(), rate * max_s))
    a = array.array("h")
    a.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        a.byteswap()
    return (a[::ch] if ch > 1 else a), rate


# ─────────────────────────────── short version ───────────────────────────────

def ai_short(c: Conf, sections: list[dict]) -> str | None:
    """Ask llama-server for the short version; None if it is not there, slow, or makes things up."""
    src = " ".join(s["text"] for s in sections if s["kind"] in ("hazards", "statements", "forecast"))[:2500]
    if not src:
        return None
    try:
        with urllib.request.urlopen(c.llama + "/health", timeout=2) as r:
            if r.status != 200:
                return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    body = json.dumps({"messages": [{"role": "system", "content": AI_SYSTEM},
                                    {"role": "user", "content": f"Forecast:\n{src}\n\nShort summary:"}],
                       "stream": False, "temperature": 0.2, "max_tokens": 80}).encode()
    req = urllib.request.Request(c.llama + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=c.ai_timeout) as r:
            out = json.loads(r.read(200_000))["choices"][0]["message"]["content"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    return FT.ai_summary_ok(out.splitlines()[0] if isinstance(out, str) and out.strip() else "", src)


# ─────────────────────────────── one run ─────────────────────────────────────

def store(c: Conf, rec: dict) -> None:
    hist = c.data / "history"
    hist.mkdir(parents=True, exist_ok=True)
    write_json(hist / f"{rec['id']}.json", rec)
    write_json(c.data / "latest.json", rec)
    idx = read_json(c.data / "index.json", []) or []
    idx = [i for i in idx if isinstance(i, dict) and i.get("id") != rec["id"]]
    idx.insert(0, {"id": rec["id"], "at": rec["at"], "new": rec["new"], "headline": rec["headline"]})
    for old in idx[c.keep:]:
        with contextlib.suppress(OSError, KeyError, TypeError):
            if re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", old["id"]):
                (hist / f"{old['id']}.json").unlink()
    write_json(c.data / "index.json", idx[: c.keep])


def transcribe_capture(c: Conf, wav: Path, at: float, source: str) -> int:
    try:
        pcm, rate = read_pcm(wav)
    except (OSError, ValueError, EOFError, wave.Error) as e:
        set_status(c, "error", f"The recording could not be read ({e}).")
        return 0
    sig = FT.signal_check(pcm, rate)
    del pcm
    if sig["verdict"] != "speech":
        what = "silence" if sig["verdict"] == "silence" else "static"
        set_status(c, "no_signal", f"No clear signal from the weather radio ({what} on {c.freq} MHz). "
                                   "Check the antenna and the frequency in Settings.", signal=sig)
        return 0
    set_status(c, "running", "Turning the broadcast into text (a few minutes)…")
    with tempfile.TemporaryDirectory(prefix="forecast-") as tmp:
        w16 = Path(tmp) / "in16k.wav"
        try:
            speechlib.to_wav16k(wav, w16, c.speech)
            raw = speechlib.transcribe(w16, c.speech, threads=c.threads, prompt=PROMPT, timeout=c.stt_timeout)
        except speechlib.SpeechError as e:
            set_status(c, "error", f"Transcription failed: {e}.")
            return 0
    if not FT.looks_like_weather(raw):
        set_status(c, "no_signal", "No clear forecast could be heard — the signal may be weak or noisy.",
                   signal=sig)
        return 0
    res = FT.process(raw, at)
    prev = read_json(c.data / "latest.json", {}) or {}
    new = not FT.same_forecast(res["key"], prev.get("key", ""))
    short, how = (ai_short(c, res["sections"]) if c.short_ai else None), "ai"
    if not short:
        short, how = FT.short_summary(res["sections"], at), "rules"
    first = next((s for s in res["sections"] if s["kind"] == "forecast"), None) or (res["sections"] or [{}])[0]
    rec = {
        "id": time.strftime("%Y%m%d-%H%M%S", time.gmtime(at)) + "-" + secrets.token_hex(2),
        "at": at, "seconds": sig["seconds"], "source": source, "freq": c.freq,
        "model": Path(c.speech.whisper_model).name, "sections": res["sections"], "text": res["text"][:20000],
        "raw": raw[:30000], "key": res["key"][:6000], "new": new, "short": short, "short_by": how if short else None,
        "headline": (short or first.get("text", ""))[:200], "signal": sig,
        "same_as": None if new else prev.get("id"),
    }
    store(c, rec)
    set_status(c, "ok", "New forecast captured." if new else "Captured; the forecast hasn't changed since last time.",
               latest=rec["id"])
    return 0


def run(c: Conf, force: bool, file: Path | None) -> int:
    if not c.enabled:
        return 0
    c.data.mkdir(parents=True, exist_ok=True)
    req = c.data / REQUEST_NAME
    if req.exists():
        force = True
        with contextlib.suppress(OSError):
            req.unlink()
    overrides(c)
    with open(c.data / ".run.lock", "a") as lock:  # held for the whole run
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("forecastd: another run is in progress", file=sys.stderr)
            return 0
        return _run_locked(c, force, file)


def _run_locked(c: Conf, force: bool, file: Path | None) -> int:
    st = read_json(c.data / "status.json", {}) or {}
    if not force and file is None:
        last = st.get("last_attempt") if isinstance(st.get("last_attempt"), (int, float)) else 0
        if now() - last < c.interval_h * 3600 - 600:
            return 0
        power = read_json(POWER, {}) or {}
        if c.skip_endurance and isinstance(power, dict) and power.get("mode") == "endurance":
            set_status(c, "skipped", "Skipped to save battery (Endurance mode). An admin can still tap Update now.")
            return 0
    if not speechlib.available(c.speech)["stt"]:
        set_status(c, "no_whisper", speechlib.INSTALL_HINT)
        return 0
    if file is not None:
        return transcribe_capture(c, file, now(), "file")
    if c.radio_off:
        set_status(c, "no_radio", "The weather radio is turned off in the settings ([radio] sdr_weather = \"off\").")
        return 0
    if weather_dongle_lent():
        set_status(c, "busy", "The weather radio is lent to a satellite pass right now (SDR 2 is missing or "
                              "not working, so both share one dongle). It will try again within the hour.")
        return 0
    if not radio_listening():
        set_status(c, "no_radio", "The weather radio isn't listening right now — no SDR plugged in, or the "
                                  "alert receiver is stopped. Plug in the SDR and try again.")
        return 0
    at = now()
    set_status(c, "running", f"Recording the weather broadcast ({c.record_s // 60} min)…", started=at)
    wav = record(c.record_s)
    if wav is None and weather_dongle_lent():
        set_status(c, "busy", "The weather radio was lent to a satellite pass during the recording (SDR 2 is "
                              "missing or not working). It will try again within the hour.")
        return 0
    if wav is None:
        set_status(c, "no_radio", "No audio came from the weather radio. It may have been busy, or the SDR "
                                  "was unplugged. It will try again later.")
        return 0
    try:
        return transcribe_capture(c, wav, at, "radio")
    finally:
        with contextlib.suppress(OSError):
            wav.unlink()                           # /run is RAM: don't leave 16 MB captures behind


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NOAA Weather Radio forecast to text")
    ap.add_argument("--force", action="store_true", help="run now even if not due")
    ap.add_argument("--file", type=Path, help="transcribe this WAV instead of recording")
    ap.add_argument("--config", type=Path, default=Path(os.environ.get("PREPPER_CONFIG", "/etc/prepper/prepper.toml")))
    a = ap.parse_args(argv)
    try:
        raw = tomllib.loads(a.config.read_text()) if a.config.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"forecastd: bad config {a.config}: {e}", file=sys.stderr)
        raw = {}
    c = Conf(raw)
    try:
        return run(c, a.force, a.file)
    except Exception as e:  # noqa: BLE001 — leave a readable status behind, then fail the unit
        with contextlib.suppress(Exception):
            set_status(c, "error", f"Something went wrong ({type(e).__name__}). It will try again later.")
        raise


if __name__ == "__main__":
    sys.exit(main())
