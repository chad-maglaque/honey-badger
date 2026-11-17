"""
Voice mode over HTTP through the real portal, with fake ffmpeg / whisper-cli / piper executables
(config paths) and a fake AI (the feature's ctx.ask_stream). Covers the happy path, the no-AI library
fallback, bad uploads, access, admin switch, rate limit, the one-at-a-time queue under concurrency,
job isolation per IP, expiry, and the speechlib TTS helper. Run: python3.11 tests/test_voice.py
"""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "speech"))
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"

import server  # noqa: E402
import speechlib  # noqa: E402

FAKE_FFMPEG = textwrap.dedent('''\
    import os, shutil, sys, wave
    a = sys.argv[1:]
    open(os.environ["FAKE_LOG"], "a").write("ffmpeg " + " ".join(a) + "\\n")
    if os.environ.get("FAKE_FFMPEG_FAIL"):
        sys.exit(1)
    src, dst = a[a.index("-i") + 1], a[-1]
    with wave.open(dst, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        v = 0 if os.environ.get("FAKE_FFMPEG_SILENT") else 3000
        w.writeframes(b"".join(((v if (i // 20) % 2 else -v) & 0xFFFF).to_bytes(2, "little") for i in range(16000)))
''')
FAKE_WHISPER = textwrap.dedent('''\
    import os, sys, time
    a = sys.argv[1:]
    open(os.environ["FAKE_LOG"], "a").write("whisper " + " ".join(a) + "\\n")
    time.sleep(float(os.environ.get("FAKE_WHISPER_SLEEP", "0")))
    if os.environ.get("FAKE_WHISPER_FAIL"):
        sys.exit(3)
    open(a[a.index("-of") + 1] + ".txt", "w").write(os.environ.get("FAKE_SAID", "How do I purify water?"))
''')
FAKE_PIPER = textwrap.dedent('''\
    import os, sys, wave
    a = sys.argv[1:]
    text = sys.stdin.read()
    open(os.environ["FAKE_LOG"], "a").write("piper " + " ".join(a) + "\\n")
    open(os.environ["FAKE_PIPER_LOG"], "a").write(text)
    if os.environ.get("FAKE_PIPER_FAIL"):
        sys.exit(2)
    with wave.open(a[a.index("--output_file") + 1], "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\\x01\\x00" * 1600)
''')
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 2000             # looks like a Chrome recording
M4A = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 2000   # looks like Safari / Voice Memos


def fake_bin(folder: Path, name: str, body: str) -> str:
    p = folder / name
    p.write_text(f"#!{sys.executable}\n" + body)
    p.chmod(0o755)
    return str(p)


def fake_ask(sources=None, tokens=("Boil it for one minute [1]. ",), delay=0.0, calls=None):
    def ask_stream(q, write, brief=False):
        if calls is not None:
            calls.append((q, brief))
        write("sources", sources if sources is not None else
              [{"n": 1, "title": "Water purification", "url": "/kiwix/content/wikimed/A/Water", "source": "WikiMed"}])
        for t in tokens:
            time.sleep(delay)
            write("token", t)
        write("done", {})
    return ask_stream


class Voice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="voice-")).resolve()
        (cls.tmp / "data").mkdir()
        b = cls.tmp / "bin"
        b.mkdir()
        cls.bins = {"ffmpeg": fake_bin(b, "ffmpeg", FAKE_FFMPEG), "whisper": fake_bin(b, "whisper-cli", FAKE_WHISPER),
                    "piper": fake_bin(b, "piper", FAKE_PIPER)}
        (cls.tmp / "model.bin").write_bytes(b"m")
        (cls.tmp / "voice.onnx").write_bytes(b"v")
        (cls.tmp / "voice.onnx.json").write_text("{}")
        feats = cls.tmp / "features"
        feats.mkdir()
        shutil.copy(REPO / "software" / "portal" / "features" / "voice.py", feats / "voice.py")
        cls.log, cls.piper_log = cls.tmp / "calls.log", cls.tmp / "piper.log"
        os.environ["FAKE_LOG"], os.environ["FAKE_PIPER_LOG"] = str(cls.log), str(cls.piper_log)
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
llama = "http://127.0.0.1:9/ai"
ctl = "/nonexistent/prepperctl"
services = {{}}

[voice]
ffmpeg = "{cls.bins['ffmpeg']}"
whisper = "{cls.bins['whisper']}"
whisper_model = "{cls.tmp / 'model.bin'}"
piper = "{cls.bins['piper']}"
piper_voice = "{cls.tmp / 'voice.onnx'}"
max_upload_mb = 0.2
queue = 1
per_ip_per_10min = 50
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        cls.feat = next(f for f in server.FEATURES if f["id"] == "voice")
        cls.mod, cls.ctx = cls.feat["mod"], cls.feat["ctx"]
        cls.jobs = cls.tmp / "features-data" / "voice" / "jobs"
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]
        inv = cls.req("POST", "/api/admin/members", {"op": "invite", "name": "Ana"}, admin=cls.admin)[1]
        server._last_post.clear()
        cls.member = cls.req("POST", "/api/members/join", {"code": inv["code"], "name": "Ana"})[1]["key"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, method, path, body=None, admin=None, member=None, ip="10.0.0.5", raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=20)
        h = {"X-Real-IP": ip}
        if raw is not None:
            h.update({"Content-Type": "audio/webm", "X-Badger-Upload": "1"})
        else:
            h["Content-Type"] = "application/json"
        if admin:
            h["X-Admin-Token"] = admin
        if member:
            h["X-Member-Token"] = member
        h.update(headers or {})
        c.request(method, path, raw if raw is not None else (json.dumps(body) if body is not None else None), h)
        r = c.getresponse()
        data = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        try:
            return r.status, json.loads(data), hdrs
        except ValueError:
            return r.status, data, hdrs

    def setUp(self):
        for k in [k for k in os.environ if k.startswith("FAKE_") and k not in ("FAKE_LOG", "FAKE_PIPER_LOG")]:
            del os.environ[k]
        self.log.write_text("")
        self.piper_log.write_text("")
        self.calls = []
        self.ctx.ask_stream = fake_ask(calls=self.calls)
        self.mod.ai_up = lambda ctx: True
        self.ctx.kiwix_search = lambda q, count=15, books=None: []
        self.wait_idle()
        with self.mod.S.lock:                              # forget earlier tests' jobs and files
            self.mod._cleanup(self.ctx, time.time() + 10 ** 6)

    def wait_idle(self, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.mod.S.lock:
                if not self.mod.S.queue and not self.mod.S.running:
                    return
            time.sleep(0.05)
        self.fail("voice worker never went idle")

    def ask(self, data=WEBM, ip="10.0.0.5", qs="", **kw):
        return self.req("POST", "/api/voice/ask" + qs, raw=data, ip=ip, **kw)

    def wait_job(self, jid, ip="10.0.0.5", timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, j, _ = self.req("GET", f"/api/voice/job?id={jid}", ip=ip)
            self.assertEqual(st, 200, j)
            if j["stage"] in ("done", "error"):
                return j
            time.sleep(0.05)
        self.fail("job never finished")

    # — happy paths —

    def test_info(self):
        st, j, _ = self.req("GET", "/api/voice/info")
        self.assertEqual(st, 200)
        self.assertTrue(j["enabled"] and j["stt"] and j["tts"])
        self.assertEqual((j["max_seconds"], j["max_bytes"], j["access"]), (30, 200_000, "public"))

    def test_question_answered_and_spoken(self):
        st, j, _ = self.ask(ip="10.0.1.1")
        self.assertEqual(st, 200, j)
        done = self.wait_job(j["id"], ip="10.0.1.1")
        self.assertEqual(done["transcript"], "How do I purify water?")
        self.assertEqual(done["answer"], "Boil it for one minute [1].")
        self.assertTrue(done["ai"] and done["audio"])
        self.assertEqual(done["sources"][0]["title"], "Water purification")
        self.assertEqual(self.calls, [("How do I purify water?", True)])           # brief by default
        st, wav, h = self.req("GET", f"/api/voice/audio?id={j['id']}", ip="10.0.1.1")
        self.assertEqual((st, h["Content-Type"], wav[:4]), (200, "audio/wav", b"RIFF"))
        self.assertEqual(self.piper_log.read_text().strip(), "Boil it for one minute .")   # no [1] read aloud
        log = self.log.read_text()
        self.assertIn("-t 30.0", log)                       # recording capped at max_seconds
        self.assertIn("whisper", log)
        self.assertNotIn("Boil", log)                       # text went on stdin, not argv
        left = [p.name for p in self.jobs.iterdir()]
        self.assertEqual(left, [f"{j['id']}.answer.wav"])   # the recording itself is gone

    def test_safari_mp4_and_long_answer(self):
        st, j, _ = self.ask(M4A, ip="10.0.1.2", qs="?long=1")
        self.assertEqual(st, 200)
        self.assertEqual(self.wait_job(j["id"], ip="10.0.1.2")["stage"], "done")
        self.assertEqual(self.calls[-1][1], False)
        self.assertTrue(any(p.suffix == ".wav" for p in self.jobs.iterdir()))

    def test_no_ai_reads_library_matches(self):
        self.mod.ai_up = lambda ctx: False
        self.ctx.kiwix_search = lambda q, count=15, books=None: [
            {"title": "Water purification", "url": "/kiwix/a", "snippet": "Boiling kills germs.", "source": "WikiMed"},
            {"title": "Chlorine", "url": "/kiwix/b", "snippet": "", "source": "Wikipedia"}]
        st, j, _ = self.ask(ip="10.0.1.3")
        done = self.wait_job(j["id"], ip="10.0.1.3")
        self.assertFalse(done["ai"])
        self.assertTrue(done["answer"].startswith("The AI assistant isn't available"))
        self.assertIn("First: Water purification. Boiling kills germs.", done["answer"])
        self.assertEqual(len(done["sources"]), 2)
        self.assertEqual(self.calls, [])
        self.assertIn("Second: Chlorine", self.piper_log.read_text())

    def test_no_ai_and_nothing_found(self):
        self.mod.ai_up = lambda ctx: False
        st, j, _ = self.ask(ip="10.0.1.4")
        done = self.wait_job(j["id"], ip="10.0.1.4")
        self.assertIn("couldn't find anything", done["answer"])

    def test_ai_stops_answering_falls_back(self):
        self.ctx.ask_stream = fake_ask(tokens=("\n\n(The AI model isn't answering — timed out.)",))
        st, j, _ = self.ask(ip="10.0.1.5")
        done = self.wait_job(j["id"], ip="10.0.1.5")
        self.assertFalse(done["ai"])
        self.assertIn("isn't available", done["answer"])

    def test_slow_answer_is_cut_off(self):
        orig = self.mod.conf
        self.mod.conf = lambda ctx: dict(orig(ctx), answer_timeout=0.3)
        self.ctx.ask_stream = fake_ask(tokens=["word "] * 40, delay=0.05)
        try:
            t0 = time.time()
            st, j, _ = self.ask(ip="10.0.1.6")
            done = self.wait_job(j["id"], ip="10.0.1.6")
        finally:
            self.mod.conf = orig
        self.assertLess(time.time() - t0, 5)
        self.assertTrue(done["answer"].endswith("…"))
        self.assertLess(done["answer"].count("word"), 40)

    # — failures that must read well —

    def test_silence_and_no_words(self):
        os.environ["FAKE_FFMPEG_SILENT"] = "1"
        st, j, _ = self.ask(ip="10.0.2.1")
        done = self.wait_job(j["id"], ip="10.0.2.1")
        self.assertEqual(done["stage"], "error")
        self.assertIn("couldn't hear anything", done["error"])
        del os.environ["FAKE_FFMPEG_SILENT"]
        os.environ["FAKE_SAID"] = "  [BLANK_AUDIO]  "
        st, j, _ = self.ask(ip="10.0.2.1")
        self.assertIn("couldn't make out any words", self.wait_job(j["id"], ip="10.0.2.1")["error"])

    def test_converter_or_whisper_crash(self):
        os.environ["FAKE_FFMPEG_FAIL"] = "1"
        st, j, _ = self.ask(ip="10.0.2.2")
        done = self.wait_job(j["id"], ip="10.0.2.2")
        self.assertEqual(done["stage"], "error")
        self.assertIn("couldn't read that recording", done["error"])
        self.assertEqual(list(self.jobs.iterdir()), [])     # upload deleted even when conversion fails
        del os.environ["FAKE_FFMPEG_FAIL"]
        os.environ["FAKE_WHISPER_FAIL"] = "1"
        st, j, _ = self.ask(ip="10.0.2.2")
        self.assertIn("speech recognition failed", self.wait_job(j["id"], ip="10.0.2.2")["error"])

    def test_tts_failure_still_gives_text(self):
        os.environ["FAKE_PIPER_FAIL"] = "1"
        st, j, _ = self.ask(ip="10.0.2.3")
        done = self.wait_job(j["id"], ip="10.0.2.3")
        self.assertEqual(done["stage"], "done")
        self.assertFalse(done["audio"])
        self.assertIn("No spoken answer", done["note"])
        self.assertEqual(self.req("GET", f"/api/voice/audio?id={j['id']}", ip="10.0.2.3")[0], 404)

    def test_bad_uploads(self):
        self.assertEqual(self.ask(b"<html><script>alert(1)</script>" + b" " * 200, ip="10.0.3.1")[0], 415)
        self.assertEqual(self.ask(b"\x89PNG\r\n\x1a\n" + b"\0" * 500, ip="10.0.3.1")[0], 415)
        self.assertEqual(self.ask(WEBM[:50], ip="10.0.3.1")[0], 400)
        self.assertEqual(self.ask(WEBM + b"\0" * 300_000, ip="10.0.3.1")[0], 413)
        st = self.req("POST", "/api/voice/ask", raw=WEBM, ip="10.0.3.1", headers={"X-Badger-Upload": "0"})[0]
        self.assertEqual(st, 415)                                     # CSRF guard of the framework
        st = self.req("POST", "/api/voice/ask", raw=WEBM, ip="10.0.3.1", headers={"Origin": "http://evil.example"})[0]
        self.assertEqual(st, 403)
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_job_belongs_to_its_device(self):
        st, j, _ = self.ask(ip="10.0.4.1")
        self.wait_job(j["id"], ip="10.0.4.1")
        self.assertEqual(self.req("GET", f"/api/voice/job?id={j['id']}", ip="10.0.4.2")[0], 404)
        self.assertEqual(self.req("GET", f"/api/voice/audio?id={j['id']}", ip="10.0.4.2")[0], 404)
        for bad in ("", "x", "../../etc/passwd", "a" * 500, j["id"] + "x"):
            self.assertEqual(self.req("GET", f"/api/voice/job?id={bad}", ip="10.0.4.1")[0], 404, bad)

    def test_jobs_expire_and_files_go(self):
        st, j, _ = self.ask(ip="10.0.4.3")
        self.wait_job(j["id"], ip="10.0.4.3")
        self.assertTrue(any(self.jobs.iterdir()))
        with self.mod.S.lock:
            self.mod._cleanup(self.ctx, time.time() + self.mod.JOB_TTL + 1)
        self.assertEqual(list(self.jobs.iterdir()), [])
        self.assertEqual(self.req("GET", f"/api/voice/job?id={j['id']}", ip="10.0.4.3")[0], 404)

    # — access, switch, limits —

    def test_admin_switch(self):
        self.assertEqual(self.req("POST", "/api/voice/settings", {"enabled": False})[0], 401)
        self.assertEqual(self.req("POST", "/api/voice/settings", {"enabled": False}, member=self.member)[0], 401)
        self.assertEqual(self.req("POST", "/api/voice/settings", {"enabled": "no"}, admin=self.admin)[0], 400)
        self.assertEqual(self.req("POST", "/api/voice/settings", {"enabled": False}, admin=self.admin)[1]["enabled"], False)
        try:
            self.assertFalse(self.req("GET", "/api/voice/info")[1]["enabled"])
            st, j, _ = self.ask(ip="10.0.5.1")
            self.assertEqual(st, 403)
            self.assertIn("switched off", j["error"])
        finally:
            self.req("POST", "/api/voice/settings", {"enabled": True}, admin=self.admin)
        self.assertEqual(self.ask(ip="10.0.5.1")[0], 200)

    def test_member_only_access(self):
        sec = server.CFG.raw["voice"]
        sec["access"] = "member"
        try:
            self.assertEqual(self.req("GET", "/api/voice/info")[0], 401)
            self.assertEqual(self.ask(ip="10.0.5.2")[0], 401)
            self.assertEqual(self.req("GET", "/api/voice/info", member=self.member)[0], 200)
            self.assertEqual(self.req("GET", "/api/voice/info", admin=self.admin)[0], 200)
            st, j, _ = self.ask(ip="10.0.5.2", member=self.member)
            self.assertEqual(st, 200)
            self.assertEqual(self.req("GET", f"/api/voice/job?id={j['id']}", ip="10.0.5.2")[0], 401)   # no key
            self.assertEqual(self.req("GET", f"/api/voice/job?id={j['id']}", ip="10.0.5.2", member=self.member)[0], 200)
        finally:
            sec.pop("access")
        self.wait_idle()

    def test_not_installed(self):
        sec = server.CFG.raw["voice"]
        old = sec["whisper"]
        sec["whisper"] = "/nonexistent/whisper-cli"
        try:
            self.assertFalse(self.req("GET", "/api/voice/info")[1]["stt"])
            st, j, _ = self.ask(ip="10.0.5.3")
            self.assertEqual(st, 503)
            self.assertIn("speech.sh", j["install_hint"])
        finally:
            sec["whisper"] = old

    def test_rate_limit_per_device(self):
        import collections
        with self.mod.S.lock:
            self.mod.S.hits["10.0.6.1"] = collections.deque([time.time()] * 50)
        st, j, h = self.ask(ip="10.0.6.1")
        self.assertEqual(st, 429)
        self.assertEqual(h.get("Retry-After"), "120")
        self.assertEqual(self.ask(ip="10.0.6.2")[0], 200)          # other devices unaffected

    def test_one_at_a_time_under_load(self):
        os.environ["FAKE_WHISPER_SLEEP"] = "1.0"
        with concurrent.futures.ThreadPoolExecutor(6) as ex:
            res = list(ex.map(lambda i: self.ask(ip=f"10.0.7.{i}"), range(6)))
        codes = sorted(r[0] for r in res)
        self.assertEqual(codes, [200, 200, 503, 503, 503, 503])      # one answering + one waiting (queue = 1)
        busy = next(r for r in res if r[0] == 503)
        self.assertIn("busy", busy[1]["error"])
        self.assertEqual(busy[2].get("Retry-After"), "30")
        self.assertEqual(list(self.jobs.glob("*.webm")).__len__() <= 2, True)
        for i, r in enumerate(res):
            if r[0] == 200:
                self.assertEqual(self.wait_job(r[1]["id"], ip=f"10.0.7.{i}")["stage"], "done")
        whisper_runs = [ln for ln in self.log.read_text().splitlines() if ln.startswith("whisper")]
        self.assertEqual(len(whisper_runs), 2)

    def test_feature_listed(self):
        f = next(x for x in self.req("GET", "/api/features")[1]["features"] if x["id"] == "voice")
        self.assertEqual((f["group"], f["route"], f["script"]), ("tools", "/voice", "/features/voice.js"))


class Helper(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="say-"))
        self.old = {k: os.environ.get(k) for k in ("FAKE_LOG", "FAKE_PIPER_LOG")}
        os.environ["FAKE_LOG"] = str(self.tmp / "calls.log")
        os.environ["FAKE_PIPER_LOG"] = str(self.tmp / "p.log")

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_say_helper(self):
        (self.tmp / "v.onnx").write_bytes(b"v")
        s = speechlib.Settings(piper=fake_bin(self.tmp, "piper", FAKE_PIPER), piper_voice=str(self.tmp / "v.onnx"))
        wav = speechlib.say("Boil water [2] before drinking. https://example.org", s)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(Path(os.environ["FAKE_PIPER_LOG"]).read_text().strip(), "Boil water before drinking.")
        with self.assertRaises(speechlib.SpeechError):
            speechlib.say("[1] [2]", s)                                # nothing left to say
        with self.assertRaises(speechlib.SpeechError):
            speechlib.say("hello", speechlib.Settings(piper="/nonexistent/piper"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
