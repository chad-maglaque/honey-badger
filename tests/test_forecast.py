"""
NOAA forecast as text: transcript cleanup (forecast_text), the capture service (forecastd, with fake
ffmpeg / whisper-cli / llama-server and a pretend alertd tap), and the portal feature over HTTP
(public page, admin update/settings, posting once to the board and the mesh).
No real models or radios. Run: python3.11 tests/test_forecast.py
"""
from __future__ import annotations

import array
import http.client
import http.server
import json
import math
import os
import random
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "speech"))
sys.path.insert(0, str(REPO / "software" / "common"))
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"

import forecast_text as FT  # noqa: E402
import forecastd  # noqa: E402
import sdrlease  # noqa: E402
import speechlib  # noqa: E402

TRANSCRIPT = """winds 10 to 15 mph. Tonight, mostly clear. Lows around 61. South winds 5 to 10 mph.
This is NOAA Weather Radio station KEC81 broadcasting from Denver on 162.550 megahertz.
[BLANK_AUDIO]
The National Weather Service in Boulder has issued a hazardous weather outlook for eastern Colorado.
Thunderstorms may produce large hail Tuesday afternoon.
Here is the forecast for Denver and vicinity. Today, scattered thunderstorms likely in the afternoon.
Highs in the upper 70s. South winds 15 to 25 mph with gusts up to 35 mph. Chance of precipitation 60 percent.
Tonight, mostly clear. Lows around 61. South winds 5 to 10 mph. Wednesday, sunny. Highs around 82.
Wednesday night, partly cloudy. Lows in the mid 50s. Thursday, a chance of snow showers. Highs near 40.
Now the current conditions. At 2 PM, at Denver International Airport, it was partly cloudy.
The temperature was 74 degrees. The humidity was 30 percent.
This is NOAA Weather Radio station KEC81 broadcasting from Denver on 162.550 megahertz.
The National Weather Service in Boulder has issued a hazardous weather outlook for eastern Colorado.
Here is the forecast for Denver and vicinity. Today, scattered thunderstorms likely in the afternoon. Highs in the upper 70s. South winds 15 to 25"""
TRANSCRIPT2 = TRANSCRIPT.replace("upper 70s", "lower 60s").replace("thunderstorms likely", "rain likely") \
    .replace("Lows around 61", "Lows around 38").replace("sunny. Highs around 82", "snow. Highs around 30")
AT = time.mktime((2026, 11, 17, 9, 0, 0, 0, 0, -1))           # a Tuesday morning, local time


def speechy(seconds=3.0, rate=22050):
    """A tone that comes and goes in syllables: what the signal check calls speech."""
    out = array.array("h")
    for i in range(int(seconds * rate)):
        t = i / rate
        on = (int(t * 4) % 3) != 2                         # 250 ms on, on, off …
        env = abs(math.sin(math.pi * t * 4)) if on else 0.0
        out.append(int(9000 * env * math.sin(2 * math.pi * 220 * t)))
    return out


def static(seconds=3.0, rate=22050):
    r = random.Random(1)
    return array.array("h", (max(-32768, min(32767, int(r.gauss(0, 6000)))) for _ in range(int(seconds * rate))))


def write_wav(path, samples, rate=22050):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


FAKE_FFMPEG = textwrap.dedent('''\
    import os, shutil, sys, wave
    a = sys.argv[1:]
    log = os.environ.get("FAKE_LOG")
    if log:
        open(log, "a").write("ffmpeg " + " ".join(a) + "\\n")
    if os.environ.get("FAKE_FFMPEG_FAIL"):
        sys.exit(1)
    src, dst = a[a.index("-i") + 1], a[-1]
    head = open(src, "rb").read(12)
    if head[:4] == b"RIFF":
        shutil.copyfile(src, dst)
    else:
        with wave.open(dst, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            v = 0 if os.environ.get("FAKE_FFMPEG_SILENT") else 3000
            w.writeframes(b"".join(((v if (i // 20) % 2 else -v) & 0xFFFF).to_bytes(2, "little") for i in range(16000)))
''')
FAKE_WHISPER = textwrap.dedent('''\
    import os, sys, time
    a = sys.argv[1:]
    log = os.environ.get("FAKE_LOG")
    if log:
        open(log, "a").write("whisper " + " ".join(a) + "\\n")
    time.sleep(float(os.environ.get("FAKE_WHISPER_SLEEP", "0")))
    if os.environ.get("FAKE_WHISPER_FAIL"):
        sys.exit(3)
    text = open(os.environ["FAKE_TRANSCRIPT"]).read() if os.environ.get("FAKE_TRANSCRIPT") else ""
    open(a[a.index("-of") + 1] + ".txt", "w").write(text)
''')


def fake_bin(folder: Path, name: str, body: str) -> str:
    p = folder / name
    p.write_text(f"#!{sys.executable}\n" + body)
    p.chmod(0o755)
    return str(p)


class Fakes:
    """Fake speech programs in a temp folder + config section pointing at them."""

    def __init__(self, root: Path):
        self.root = root
        b = root / "bin"
        b.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = fake_bin(b, "ffmpeg", FAKE_FFMPEG)
        self.whisper = fake_bin(b, "whisper-cli", FAKE_WHISPER)
        self.model = root / "model.bin"
        self.model.write_bytes(b"fake model")
        self.transcript = root / "transcript.txt"
        self.log = root / "calls.log"
        os.environ["FAKE_TRANSCRIPT"] = str(self.transcript)
        os.environ["FAKE_LOG"] = str(self.log)

    def section(self) -> dict:
        return {"ffmpeg": self.ffmpeg, "whisper": self.whisper, "whisper_model": str(self.model)}


def clear_fake_env():
    for k in [k for k in os.environ if k.startswith("FAKE_") and k not in ("FAKE_TRANSCRIPT", "FAKE_LOG")]:
        del os.environ[k]


# ─────────────────────────────── pure text ───────────────────────────────────

class Text(unittest.TestCase):
    def test_signal_verdicts(self):
        self.assertEqual(FT.signal_check(array.array("h", [0] * 66150), 22050)["verdict"], "silence")
        self.assertEqual(FT.signal_check(static(), 22050)["verdict"], "static")
        self.assertEqual(FT.signal_check(speechy(), 22050)["verdict"], "speech")
        self.assertEqual(FT.signal_check(array.array("h", [5] * 10), 22050)["verdict"], "silence")   # too short

    def test_looks_like_weather(self):
        self.assertTrue(FT.looks_like_weather(TRANSCRIPT))
        self.assertFalse(FT.looks_like_weather("Thank you. Thank you. Thank you for watching."))
        self.assertFalse(FT.looks_like_weather(""))
        self.assertFalse(FT.looks_like_weather("weather " * 3))

    def test_dedupe_loop_and_fragments(self):
        s = FT.sentences(speechlib.clean_transcript(TRANSCRIPT))
        d = FT.dedupe(s)
        joined = " ".join(d)
        self.assertEqual(joined.count("This is NOAA Weather Radio"), 1)
        self.assertEqual(joined.count("hazardous weather outlook"), 1)
        self.assertEqual(joined.count("Highs in the upper 70s"), 1)
        self.assertNotIn("winds 10 to 15 mph. Tonight", joined)          # leading fragment dropped
        self.assertTrue(any(x.startswith("South winds 15 to 25 mph with gusts") for x in d))   # full copy kept
        self.assertEqual(FT.dedupe(["Thank you.", "Thank you.", "Thank you!"]), ["Thank you."])

    def test_fragment_first_then_full_copy_replaces_it(self):
        d = FT.dedupe(["Highs in the upper 70s.", "South winds 10 mph.", "Tonight, clear.",
                       "Today, sunny. Highs in the upper 70s.", "South winds 10 mph."])
        self.assertEqual(d, ["Today, sunny. Highs in the upper 70s.", "South winds 10 mph.", "Tonight, clear."])

    def test_lines_that_rightly_repeat_are_kept(self):
        s = ["Tonight, clear.", "Lows 61.", "South winds 5 to 10 mph.", "Wednesday night, cloudy.", "Lows 55.",
             "South winds 5 to 10 mph.", "Thursday, sunny.", "Highs near 90.", "Friday, sunny.", "Highs near 91."]
        self.assertEqual(FT.dedupe(s), s)
        self.assertEqual(FT.dedupe(s + s[:3] + ["South winds 5"]), s)          # the loop again, cut off

    def test_sections_and_period_order(self):
        r = FT.process(TRANSCRIPT, AT)
        kinds = [s["kind"] for s in r["sections"]]
        self.assertEqual(kinds[:2], ["hazards", "forecast"])
        self.assertIn("current", kinds)
        self.assertEqual(kinds[-1], "station")
        f = r["sections"][1]
        self.assertEqual([p["name"] for p in f["periods"]],
                         ["Today", "Tonight", "Wednesday", "Wednesday Night", "Thursday"])
        self.assertIn("forecast for Denver", f["intro"])
        self.assertNotIn("forecast for Denver", f["periods"][1]["text"])     # header didn't stick to Tonight
        self.assertNotIn("BLANK_AUDIO", r["text"])

    def test_short_summary(self):
        r = FT.process(TRANSCRIPT, AT)
        s = FT.short_summary(r["sections"], AT)
        self.assertLessEqual(len(s.encode()), 200)
        self.assertTrue(s.startswith("Wx Tue: storms likely PM, hi upper 70s, S 15-25 mph g35."), s)
        self.assertIn("Tonight: mostly clear, lo 61, S 5-10 mph.", s)

    def test_short_summary_with_warning_and_overflow(self):
        long_periods = " ".join(f"{d}, a chance of thunderstorms and showers with heavy rain at times. Highs near 9{i}. "
                                f"Northwest winds 20 to 30 mph with gusts up to 5{i} mph."
                                for i, d in enumerate(["Today", "Tonight", "Wednesday", "Thursday"]))
        secs = FT.process("The National Weather Service has issued a Winter Storm Warning for the mountains. "
                          + long_periods, AT)["sections"]
        s = FT.short_summary(secs, AT)
        self.assertLessEqual(len(s.encode()), 200)
        self.assertTrue(s.startswith("Winter Storm Warning! Wx Tue:"), s)

    def test_short_summary_without_periods(self):
        self.assertIsNone(FT.short_summary([{"kind": "current", "title": "x", "text": "It was 70."}], AT))
        s = FT.short_summary([{"kind": "forecast", "title": "Forecast", "text": "Forecast: " + "rain " * 100,
                               "periods": []}], AT)
        self.assertLessEqual(len(s.encode()), 200)

    def test_same_forecast(self):
        a, b = FT.process(TRANSCRIPT, AT)["key"], FT.process(TRANSCRIPT2, AT)["key"]
        self.assertTrue(FT.same_forecast(a, FT.process(TRANSCRIPT + " Thank you.", AT)["key"]))
        self.assertFalse(FT.same_forecast(a, b))
        self.assertFalse(FT.same_forecast(a, ""))

    def test_ai_summary_checks(self):
        src = "Today, storms likely. Highs 78. South winds 15 to 25 mph. Tonight lows 61."
        self.assertEqual(FT.ai_summary_ok("Wx Tue: storms, hi 78, S 15-25. Tonight lo 61.", src),
                         "Wx Tue: storms, hi 78, S 15-25. Tonight lo 61.")
        self.assertIsNone(FT.ai_summary_ok("Wx Tue: storms, hi 81.", src))           # 81 isn't in the forecast
        self.assertIsNone(FT.ai_summary_ok("Wx " + "storms 78 " * 40, src))          # too long
        self.assertIsNone(FT.ai_summary_ok("Storms are likely today.", src))         # no numbers at all
        self.assertEqual(FT.ai_summary_ok('"Storms, hi 78, lo 61"', src), "Wx: Storms, hi 78, lo 61")
        self.assertIsNone(FT.ai_summary_ok(None, src))


class Lib(unittest.TestCase):
    def test_sniff(self):
        self.assertEqual(speechlib.sniff_audio(b"\x1aE\xdf\xa3" + b"\0" * 20), "webm")
        self.assertEqual(speechlib.sniff_audio(b"\0\0\0\x18ftypM4A " + b"\0" * 8), "mp4")
        self.assertEqual(speechlib.sniff_audio(b"RIFF\0\0\0\0WAVEfmt "), "wav")
        self.assertEqual(speechlib.sniff_audio(b"OggS" + b"\0" * 20), "ogg")
        self.assertEqual(speechlib.sniff_audio(b"#!AMR\n" + b"\0" * 20), "amr")
        self.assertIsNone(speechlib.sniff_audio(b"<html><script>alert(1)</script>"))
        self.assertIsNone(speechlib.sniff_audio(b"\x89PNG\r\n\x1a\n" + b"\0" * 8))
        self.assertIsNone(speechlib.sniff_audio(b"RIFF"))

    def test_settings_validation(self):
        s = speechlib.Settings.from_config({"whisper": 5, "whisper_threads": "lots", "piper": "x\x00y",
                                            "stt_timeout": 10 ** 9, "tts_timeout": True})
        d = speechlib.Settings()
        self.assertEqual((s.whisper, s.whisper_threads, s.piper), (d.whisper, d.whisper_threads, d.piper))
        self.assertEqual(s.stt_timeout, 3600)
        self.assertEqual(s.tts_timeout, d.tts_timeout)
        self.assertEqual(speechlib.Settings.from_config(None).whisper, d.whisper)

    def test_speech_text(self):
        t = speechlib.speech_text("**Boil** water [1] for 1 minute [2, 3]. See https://x.example/a.\n- Then cool it 🙂")
        self.assertNotIn("[", t)
        self.assertNotIn("http", t)
        self.assertNotIn("*", t)
        self.assertIn("Boil water for 1 minute", t)
        long = speechlib.speech_text("This is a sentence. " * 200, 300)
        self.assertLessEqual(len(long), 300)
        self.assertTrue(long.endswith("."))

    def test_run_timeout_kills_the_group(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            slow = fake_bin(tmp, "slow", "import subprocess, sys, time\n"
                                         "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                                         "time.sleep(30)\n")
            t0 = time.monotonic()
            with self.assertRaises(speechlib.SpeechError) as e:
                speechlib.run([slow], 0.5)
            self.assertLess(time.monotonic() - t0, 10)
            self.assertIn("too long", str(e.exception))
            with self.assertRaises(speechlib.SpeechError):
                speechlib.run([str(tmp / "missing")], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_available(self):
        av = speechlib.available(speechlib.Settings(ffmpeg="/nonexistent", whisper="/nonexistent"))
        self.assertFalse(av["stt"])
        self.assertIn("install.d/speech.sh", av["hint"])


# ─────────────────────────────── the service ─────────────────────────────────

class Service(unittest.TestCase):
    def setUp(self):
        clear_fake_env()
        self.tmp = Path(tempfile.mkdtemp(prefix="fcd-")).resolve()
        self.f = Fakes(self.tmp)
        self.f.transcript.write_text(TRANSCRIPT)
        self.data = self.tmp / "data"
        forecastd.TAP_REQ = self.tmp / "run" / "noaa-tap.request"
        forecastd.TAP_DIR = self.tmp / "run" / "noaa-tap"
        forecastd.BEAT = self.tmp / "run" / "alertd.ok"
        forecastd.POWER = self.tmp / "run" / "power.json"
        sdrlease.SDR_DIR = self.tmp / "run" / "sdr"
        (self.tmp / "run").mkdir()
        self.capture = self.tmp / "cap.wav"
        write_wav(self.capture, speechy(4))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        clear_fake_env()

    def conf(self, **kw):
        sec = {**self.f.section(), "data_dir": str(self.data), "short_ai": False, **kw}
        return forecastd.Conf({"forecast": sec, "portal": {"llama": "http://127.0.0.1:9/ai"}})

    def status(self):
        return json.loads((self.data / "status.json").read_text())

    def test_file_capture_new_then_unchanged_then_new(self):
        c = self.conf()
        self.assertEqual(forecastd.run(c, False, self.capture), 0)
        st, latest = self.status(), json.loads((self.data / "latest.json").read_text())
        self.assertEqual(st["state"], "ok")
        self.assertTrue(latest["new"])
        self.assertEqual(latest["short_by"], "rules")
        self.assertLessEqual(len(latest["short"].encode()), 200)
        self.assertTrue((self.data / "history" / f"{latest['id']}.json").exists())
        self.assertEqual(oct((self.data / "latest.json").stat().st_mode & 0o777), "0o640")
        log = self.f.log.read_text()
        self.assertIn("-ar 16000", log)
        self.assertIn("-t 2", log)                                   # 2 whisper threads by default
        forecastd.run(self.conf(), False, self.capture)
        second = json.loads((self.data / "latest.json").read_text())
        self.assertFalse(second["new"])
        self.assertEqual(second["same_as"], latest["id"])
        self.f.transcript.write_text(TRANSCRIPT2)
        forecastd.run(self.conf(), False, self.capture)
        third = json.loads((self.data / "latest.json").read_text())
        self.assertTrue(third["new"])
        self.assertEqual(len(json.loads((self.data / "index.json").read_text())), 3)

    def test_history_is_pruned(self):
        c = self.conf(keep=2)
        for _ in range(4):
            forecastd.run(c, False, self.capture)
        idx = json.loads((self.data / "index.json").read_text())
        self.assertEqual(len(idx), 2)
        self.assertEqual(sorted(p.stem for p in (self.data / "history").glob("*.json")), sorted(i["id"] for i in idx))

    def test_static_and_silence_are_no_signal(self):
        write_wav(self.capture, static(4))
        forecastd.run(self.conf(), False, self.capture)
        self.assertEqual(self.status()["state"], "no_signal")
        self.assertIn("static", self.status()["message"])
        self.assertFalse((self.data / "latest.json").exists())
        write_wav(self.capture, array.array("h", [0] * 88200))
        forecastd.run(self.conf(), False, self.capture)
        self.assertIn("silence", self.status()["message"])
        self.assertNotIn("whisper", self.f.log.read_text() if self.f.log.exists() else "")   # never transcribed

    def test_whisper_hallucination_is_no_signal(self):
        self.f.transcript.write_text("Thank you. Thank you. [Music]")
        forecastd.run(self.conf(), False, self.capture)
        self.assertEqual(self.status()["state"], "no_signal")

    def test_whisper_missing_failing_or_slow(self):
        forecastd.run(self.conf(whisper=str(self.tmp / "nope")), False, self.capture)
        self.assertEqual(self.status()["state"], "no_whisper")
        self.assertIn("speech.sh", self.status()["message"])
        os.environ["FAKE_WHISPER_FAIL"] = "1"
        forecastd.run(self.conf(), False, self.capture)
        self.assertEqual(self.status()["state"], "error")
        del os.environ["FAKE_WHISPER_FAIL"]
        os.environ["FAKE_WHISPER_SLEEP"] = "5"
        c = self.conf()
        c.stt_timeout = 1
        t0 = time.monotonic()
        forecastd.run(c, False, self.capture)
        self.assertLess(time.monotonic() - t0, 4.5)
        self.assertEqual(self.status()["state"], "error")
        self.assertIn("too long", self.status()["message"])

    def test_bad_capture_file(self):
        self.capture.write_bytes(b"RIFF garbage")
        forecastd.run(self.conf(), False, self.capture)
        self.assertEqual(self.status()["state"], "error")

    def test_due_logic_request_file_and_endurance(self):
        c = self.conf()
        self.data.mkdir()
        (self.data / "status.json").write_text(json.dumps({"last_attempt": time.time()}))
        forecastd.run(c, False, None)                     # not due: nothing written
        self.assertNotIn("state", self.status())
        (self.data / "update-now.request").write_text("{}")
        forecastd.run(self.conf(), False, None)           # forced by the request → radio check
        self.assertFalse((self.data / "update-now.request").exists())
        self.assertEqual(self.status()["state"], "no_radio")
        (self.data / "status.json").write_text(json.dumps({"last_attempt": 0}))
        forecastd.POWER.write_text(json.dumps({"mode": "endurance"}))
        forecastd.run(self.conf(), False, None)
        self.assertEqual(self.status()["state"], "skipped")
        forecastd.run(self.conf(skip_on_endurance=False), True, None)
        self.assertEqual(self.status()["state"], "no_radio")

    def test_disabled_and_radio_off(self):
        c = forecastd.Conf({"features": {"forecast": False}, "forecast": {"data_dir": str(self.data)}})
        self.assertEqual(forecastd.run(c, True, None), 0)
        self.assertFalse(self.data.exists())
        c = forecastd.Conf({"radio": {"sdr_weather": "off"}, "forecast": {**self.f.section(), "data_dir": str(self.data)}})
        forecastd.run(c, True, None)
        self.assertIn("turned off", self.status()["message"])

    def test_lent_dongle_is_busy_and_retried(self):
        forecastd.BEAT.touch()
        sdrlease.SDR_DIR.mkdir(parents=True)
        (sdrlease.SDR_DIR / "weather.borrow").write_text(json.dumps({"owner": "sat", "until": time.time() + 600,
                                                                     "pid": os.getpid()}))
        forecastd.run(self.conf(), True, None)
        st = self.status()
        self.assertEqual(st["state"], "busy")
        self.assertIn("SDR 2", st["message"])
        self.assertNotIn("last_attempt", st)              # not counted: the next hourly run tries again

    def test_record_through_tap(self):
        """A pretend alertd: while the request is live, write a capture named by its start time."""
        stop = threading.Event()

        def alertd():
            started = None
            while not stop.is_set():
                try:
                    live = json.loads(forecastd.TAP_REQ.read_text())["until"] > time.time()
                except (OSError, ValueError, KeyError):
                    live = False
                if live and started is None:
                    started = int(time.time())
                if not live and started is not None:
                    forecastd.TAP_DIR.mkdir(parents=True, exist_ok=True)
                    write_wav(forecastd.TAP_DIR / f"{started}.wav", speechy(2))
                    started = None
                time.sleep(0.05)
        forecastd.TAP_DIR.mkdir(parents=True)
        write_wav(forecastd.TAP_DIR / "1000.wav", speechy(1))        # an old capture: must not be picked
        t = threading.Thread(target=alertd, daemon=True)
        t.start()
        try:
            got = forecastd.record(1, poll=0.1, grace=3)
            self.assertIsNotNone(got)
            self.assertNotEqual(got.name, "1000.wav")
            self.assertFalse(forecastd.TAP_REQ.exists())                 # request cleaned up
            got.unlink()
            # whole run through the tap
            forecastd.BEAT.touch()
            c = self.conf()
            c.record_s = 1
            orig = forecastd.record
            forecastd.record = lambda s: orig(s, poll=0.1, grace=3)
            try:
                forecastd.run(c, True, None)
            finally:
                forecastd.record = orig
            self.assertEqual(self.status()["state"], "ok")
            self.assertEqual(json.loads((self.data / "latest.json").read_text())["source"], "radio")
            self.assertEqual([p.name for p in forecastd.TAP_DIR.glob("*.wav")], ["1000.wav"])   # ours deleted
        finally:
            stop.set()
            t.join(2)

    def test_record_without_alertd_gives_none(self):
        self.assertIsNone(forecastd.record(1, poll=0.1, grace=0.5))
        self.assertFalse(forecastd.TAP_REQ.exists())

    def test_concurrent_runs_one_wins(self):
        self.data.mkdir()
        import fcntl
        with open(self.data / ".run.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            forecastd.run(self.conf(), False, self.capture)
        self.assertFalse((self.data / "latest.json").exists())

    def test_main_with_bad_config(self):
        cfg = self.tmp / "bad.toml"
        cfg.write_text("[forecast\nbroken")
        seen = {}
        orig = forecastd.run
        forecastd.run = lambda c, force, file: seen.update(c=c, force=force, file=file) or 0
        try:
            self.assertEqual(forecastd.main(["--config", str(cfg), "--force"]), 0)
        finally:
            forecastd.run = orig
        self.assertEqual((seen["c"].interval_h, seen["force"], seen["file"]), (3, True, None))   # defaults


class FakeLlama(http.server.BaseHTTPRequestHandler):
    reply = "Wx Tue: storms likely PM, hi 70s, S 15-25. Tonight lo 61."
    health = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(self.health)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        assert body["stream"] is False and body["max_tokens"] <= 100
        out = json.dumps({"choices": [{"message": {"content": FakeLlama.reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class AiShort(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLlama)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/ai"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def conf(self, url):
        return forecastd.Conf({"portal": {"llama": url}, "forecast": {"ai_timeout": 5}})

    def test_ai_used_when_numbers_check_out(self):
        secs = FT.process(TRANSCRIPT, AT)["sections"]
        FakeLlama.reply = "Wx Tue: storms likely PM, hi 70s, S 15-25. Tonight lo 61."
        self.assertEqual(forecastd.ai_short(self.conf(self.url), secs), FakeLlama.reply)
        FakeLlama.reply = "Wx Tue: storms, hi 99. Tonight lo 61."        # 99 invented
        self.assertIsNone(forecastd.ai_short(self.conf(self.url), secs))
        self.assertIsNone(forecastd.ai_short(self.conf("http://127.0.0.1:9/ai"), secs))   # nobody there
        self.assertIsNone(forecastd.ai_short(self.conf(self.url), []))


# ─────────────────────────────── portal feature ──────────────────────────────

class Portal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.tmp = Path(tempfile.mkdtemp(prefix="fcp-")).resolve()
        (cls.tmp / "data").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        shutil.copy(REPO / "software" / "portal" / "features" / "forecast.py", feats / "forecast.py")
        cls.f = Fakes(cls.tmp)
        cfg = cls.tmp / "prepper.toml"
        cfg.write_text(f"""
[upd]
data_root = "{cls.tmp / 'data'}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{cls.tmp}/upd-state.json"
report_dir = "{cls.tmp}/reports"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
admin_pin = "{server.hash_pin('246810')}"
board_file = "{cls.tmp}/board.jsonl"
family_board_file = "{cls.tmp}/family-board.jsonl"
members_file = "{cls.tmp}/members.json"
settings_file = "{cls.tmp}/portal-settings.json"
mesh_outbox = "{cls.tmp}/mesh-outbox.jsonl"
features_data = "{cls.tmp}/features-data"
ctl = "/nonexistent/prepperctl"
services = {{}}

[forecast]
ffmpeg = "{cls.f.ffmpeg}"
whisper = "{cls.f.whisper}"
whisper_model = "{cls.f.model}"
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        cls.feat = next(x for x in server.FEATURES if x["id"] == "forecast")
        cls.mod, cls.ctx = cls.feat["mod"], cls.feat["ctx"]
        cls.data = cls.tmp / "features-data" / "forecast"
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, method, path, body=None, admin=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if admin:
            h["X-Admin-Token"] = admin
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def setUp(self):
        for p in ("latest.json", "index.json", "status.json", "posted.json", "settings.json", "update-now.request"):
            (self.data / p).unlink(missing_ok=True)
        shutil.rmtree(self.data / "history", ignore_errors=True)
        for p in ("board.jsonl", "mesh-outbox.jsonl"):
            (self.tmp / p).write_text("")
        self.mod._last_update_req[0] = 0.0
        self.server._last_post.clear()

    def capture(self, transcript=TRANSCRIPT, at=None):
        self.f.transcript.write_text(transcript)
        wav = self.tmp / "cap.wav"
        write_wav(wav, speechy(3))
        c = forecastd.Conf({"forecast": {**self.f.section(), "data_dir": str(self.data), "short_ai": False}})
        forecastd.transcribe_capture(c, wav, at or time.time(), "radio")
        return json.loads((self.data / "latest.json").read_text())

    def test_page_empty_then_latest(self):
        st, j = self.req("GET", "/api/forecast")
        self.assertEqual(st, 200)
        self.assertIsNone(j["latest"])
        self.assertTrue(j["available"])
        self.assertNotIn("settings", j)                    # admin only
        rec = self.capture()
        st, j = self.req("GET", "/api/forecast")
        self.assertEqual(j["latest"]["id"], rec["id"])
        self.assertIn("may contain errors", j["note"])
        self.assertNotIn("key", j["latest"])
        self.assertEqual(j["history"][0]["id"], rec["id"])
        self.assertEqual(self.req("GET", "/api/forecast", admin=self.admin)[1]["settings"]["post_board"], True)

    def test_item_lookup_is_safe(self):
        rec = self.capture()
        self.assertEqual(self.req("GET", f"/api/forecast/item?id={rec['id']}")[1]["id"], rec["id"])
        for bad in ("../latest", "..%2F..%2Fstatus", "", "x" * 300, "20261117-090000-zzzz"):
            self.assertEqual(self.req("GET", f"/api/forecast/item?id={bad}")[0], 400, bad)
        self.assertEqual(self.req("GET", "/api/forecast/item?id=20201117-090000-abcd")[0], 404)
        (self.data / "history" / "20201117-090000-abcd.json").write_text("[1, 2]")      # corrupt file
        self.assertEqual(self.req("GET", "/api/forecast/item?id=20201117-090000-abcd")[0], 404)

    def test_not_installed_shows_hint(self):
        sec = self.server.CFG.raw["forecast"]
        old = sec["whisper"]
        sec["whisper"] = "/nonexistent/whisper-cli"
        try:
            j = self.req("GET", "/api/forecast")[1]
            self.assertFalse(j["available"])
            self.assertIn("speech.sh", j["install_hint"])
        finally:
            sec["whisper"] = old

    def test_update_now_is_admin_only_and_rate_limited(self):
        self.assertEqual(self.req("POST", "/api/forecast/update", {})[0], 401)
        st, j = self.req("POST", "/api/forecast/update", {}, admin=self.admin)
        self.assertEqual((st, j["ok"]), (200, True))
        self.assertTrue((self.data / "update-now.request").exists())
        self.assertTrue(self.req("GET", "/api/forecast")[1]["update_pending"])
        self.assertEqual(self.req("POST", "/api/forecast/update", {}, admin=self.admin)[0], 429)
        st = self.req("POST", "/api/forecast/update", {}, admin=self.admin, headers={"Origin": "http://evil.example"})[0]
        self.assertEqual(st, 403)

    def test_settings_validation(self):
        self.assertEqual(self.req("POST", "/api/forecast/settings", {"post_mesh": False})[0], 401)
        for bad in ({"post_mesh": "no"}, {"interval_hours": 0}, {"interval_hours": 2.5}, {"interval_hours": True},
                    {"interval_hours": 10 ** 6}):
            self.assertEqual(self.req("POST", "/api/forecast/settings", bad, admin=self.admin)[0], 400, bad)
        st, j = self.req("POST", "/api/forecast/settings", {"post_mesh": False, "interval_hours": 6}, admin=self.admin)
        self.assertEqual((st, j["settings"]["post_mesh"], j["settings"]["interval_hours"]), (200, False, 6))
        # the service picks up the interval
        c = forecastd.Conf({"forecast": {"data_dir": str(self.data)}})
        forecastd.overrides(c)
        self.assertEqual(c.interval_h, 6)

    def board(self):
        return [json.loads(x) for x in (self.tmp / "board.jsonl").read_text().splitlines()]

    def mesh(self):
        return [json.loads(x) for x in (self.tmp / "mesh-outbox.jsonl").read_text().splitlines()]

    def test_posts_new_forecast_once(self):
        rec = self.capture()
        self.assertEqual(self.mod.check_and_post(self.ctx), {"board": True, "mesh": True})
        self.assertEqual(self.mod.check_and_post(self.ctx), {"board": False, "mesh": False})     # once only
        b = self.board()
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["name"], "Weather Radio")
        self.assertIn("may contain errors", b[0]["text"])
        self.assertIn(rec["short"], b[0]["text"])
        m = self.mesh()
        self.assertEqual(m, [{"text": rec["short"], "board": "community"}])
        self.assertLessEqual(len(m[0]["text"].encode()), 200)

    def test_unchanged_forecast_is_not_posted(self):
        self.capture()
        self.mod.check_and_post(self.ctx)
        self.capture()                                     # same forecast again
        self.assertEqual(self.mod.check_and_post(self.ctx), {"board": False, "mesh": False})

    def test_limits_board_6h_mesh_twice_a_day(self):
        t = time.time()
        self.capture(TRANSCRIPT, t)
        self.mod.check_and_post(self.ctx, t)
        self.capture(TRANSCRIPT2, t + 3600)                # new, but only 1 h later
        self.assertEqual(self.mod.check_and_post(self.ctx, t + 3600), {"board": False, "mesh": True})
        self.capture(TRANSCRIPT, t + 7200)
        self.assertEqual(self.mod.check_and_post(self.ctx, t + 7200), {"board": False, "mesh": False})   # 2/day used
        self.capture(TRANSCRIPT2, t + 7 * 3600)
        self.server._last_post.clear()                      # the board's own 5-second limit is real time
        self.assertEqual(self.mod.check_and_post(self.ctx, t + 7 * 3600), {"board": True, "mesh": False})
        self.capture(TRANSCRIPT, t + 25 * 3600)
        self.server._last_post.clear()
        self.assertEqual(self.mod.check_and_post(self.ctx, t + 25 * 3600), {"board": True, "mesh": True})

    def test_switches_and_stale_captures(self):
        self.req("POST", "/api/forecast/settings", {"post_mesh": False, "post_board": False}, admin=self.admin)
        self.capture()
        self.assertEqual(self.mod.check_and_post(self.ctx), {"board": False, "mesh": False})
        (self.data / "settings.json").unlink()
        self.capture(TRANSCRIPT2, time.time() - 13 * 3600)   # older than 12 h (Badger was off)
        self.assertEqual(self.mod.check_and_post(self.ctx), {"board": False, "mesh": False})

    def test_garbage_files_dont_break_anything(self):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "latest.json").write_text('{"id": "../../etc/passwd", "new": true}')
        (self.data / "index.json").write_text('[{"id": "<script>"}, 5, null]')
        (self.data / "posted.json").write_text("[]")
        st, j = self.req("GET", "/api/forecast")
        self.assertEqual(st, 200)
        self.assertIsNone(j["latest"])
        self.assertEqual(j["history"], [])
        self.assertEqual(self.mod.check_and_post(self.ctx), {"board": False, "mesh": False})

    def test_setup_does_not_post_old_forecasts(self):
        self.capture()
        (self.data / "posted.json").unlink(missing_ok=True)
        orig = self.mod.threading.Thread
        self.mod.threading.Thread = lambda *a, **k: type("T", (), {"start": lambda s: None})()
        try:
            self.mod.setup(self.ctx)
        finally:
            self.mod.threading.Thread = orig
        self.assertEqual(self.mod.check_and_post(self.ctx), {"board": False, "mesh": False})

    def test_feature_listed_in_radio_group(self):
        f = next(x for x in self.req("GET", "/api/features")[1]["features"] if x["id"] == "forecast")
        self.assertEqual((f["group"], f["route"], f["access"]), ("radio", "/forecast", "public"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
