"""
Feature-plugin framework (portal loader, access levels, raw uploads, file responses), the SDR
lease that lets services share one dongle, and alertd's audio tap.
Run: python3.11 tests/test_framework.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "software" / "common"))
sys.path.insert(0, str(REPO / "software" / "alertd"))
os.environ["PORTAL_QUIET"] = "1"

import server  # noqa: E402

DEMO = textwrap.dedent('''
    import json
    FEATURE = {"title": "Demo", "icon": "D", "group": "family", "route": "/demo",
               "script": "/features/demo.js", "access": "member", "description": "test"}
    def pub(req, ctx, args): return {"hello": "world", "q": args["qs"].get("x", [""])[0]}
    def mem(req, ctx, args): return {"name": args["member"]["name"]}
    def adm(req, ctx, args): return 201, {"admin": True}
    def echo(req, ctx, args): return {"got": args["data"]}
    def up(req, ctx, args):
        n = int(req.headers["Content-Length"])
        data = req.rfile.read(n)
        (ctx.data_dir() / "up.bin").write_bytes(data)
        return {"bytes": len(data)}
    def down(req, ctx, args):
        ctx.send_bytes(req, b"<svg onload=alert(1)>", "image/svg+xml")
    def file_(req, ctx, args):
        return None if ctx.send_file(req, ctx.data_dir() / "up.bin", "application/octet-stream",
                                     name='evil"\\r\\nX: y.bin', inline=False) else None
    def mesh(req, ctx, args): return {"ok": ctx.mesh_send("x" * 400, "family")}
    def boom(req, ctx, args): raise RuntimeError("oops")
    ROUTES = [
        {"method": "GET", "path": "/api/demo/pub", "handler": pub, "access": "public"},
        {"method": "GET", "path": "/api/demo/mem", "handler": mem, "access": "member"},
        {"method": "GET", "path": "/api/demo/adm", "handler": adm, "access": "admin"},
        {"method": "GET", "path": "/api/demo/default", "handler": adm},
        {"method": "GET", "path": "/api/demo/local", "handler": pub, "access": "local"},
        {"method": "POST", "path": "/api/demo/echo", "handler": echo, "access": "public"},
        {"method": "POST", "path": "/api/demo/up", "handler": up, "access": "member", "raw": True, "max_bytes": 1000},
        {"method": "GET", "path": "/api/demo/down", "handler": down, "access": "public"},
        {"method": "GET", "path": "/api/demo/file/", "handler": file_, "access": "member", "prefix": True},
        {"method": "POST", "path": "/api/demo/mesh", "handler": mesh, "access": "admin"},
        {"method": "GET", "path": "/api/demo/boom", "handler": boom, "access": "public"},
        {"method": "GET", "path": "/not-api", "handler": pub, "access": "public"},
    ]
''')


class Features(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="feat-")).resolve()
        (cls.tmp / "data").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        (feats / "demo.py").write_text(DEMO)
        (feats / "broken.py").write_text("raise ImportError('nope')\n")
        (feats / "off.py").write_text("FEATURE = {'title': 'Off'}\nROUTES = []\n")
        (feats / "_skip.py").write_text("raise SystemExit(1)\n")
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

[features]
off = false

[demo]
greeting = "hi"
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
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
        cls.srv.shutdown(); cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, method, path, body=None, member=None, admin=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if member:
            h["X-Member-Token"] = member
        if admin:
            h["X-Admin-Token"] = admin
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

    def test_list_only_loaded_features(self):
        st, j, _ = self.req("GET", "/api/features")
        self.assertEqual(st, 200)
        ids = [f["id"] for f in j["features"]]
        self.assertEqual(ids, ["demo"])             # broken skipped, off disabled, _skip ignored
        self.assertEqual(j["features"][0]["route"], "/demo")

    def test_access_levels(self):
        self.assertEqual(self.req("GET", "/api/demo/pub?x=1")[1], {"hello": "world", "q": "1"})
        self.assertEqual(self.req("GET", "/api/demo/mem")[0], 401)
        self.assertEqual(self.req("GET", "/api/demo/mem", member="bogus")[0], 401)
        self.assertEqual(self.req("GET", "/api/demo/mem", member=self.member)[1], {"name": "Ana"})
        self.assertEqual(self.req("GET", "/api/demo/adm", member=self.member)[0], 401)
        st, j, _ = self.req("GET", "/api/demo/adm", admin=self.admin)
        self.assertEqual((st, j), (201, {"admin": True}))
        self.assertEqual(self.req("GET", "/api/demo/default")[0], 401)       # no access given → admin only
        self.assertEqual(self.req("GET", "/api/demo/default", admin=self.admin)[0], 201)

    def test_local_means_the_badger_itself(self):
        # standalone test server: requests arrive without X-Real-IP on loopback = the Badger's own panel
        self.assertEqual(self.req("GET", "/api/demo/local")[0], 200)
        self.assertEqual(self.req("GET", "/api/demo/local", headers={"X-Real-IP": "192.168.4.20"})[0], 403)

    def test_json_post_keeps_csrf_rules(self):
        server._last_post.clear()
        self.assertEqual(self.req("POST", "/api/demo/echo", {"a": 1})[1], {"got": {"a": 1}})
        st = self.req("POST", "/api/demo/echo", headers={"Content-Type": "text/plain"}, raw=b'{"a":1}')[0]
        self.assertEqual(st, 415)
        st = self.req("POST", "/api/demo/echo", {"a": 1}, headers={"Origin": "http://evil.example"})[0]
        self.assertEqual(st, 403)

    def test_refused_raw_upload_does_not_poison_the_connection(self):
        # A refused upload's body is never read: the server must hang up rather than parse those
        # bytes as the next request on the same keep-alive connection.
        import socket
        evil = b"GET /api/demo/pub?x=smuggled HTTP/1.1\r\nHost: x\r\n\r\n"
        req = (b"POST /api/demo/up HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Badger-Upload: 1\r\n"
               b"Content-Type: application/octet-stream\r\nContent-Length: " + str(len(evil)).encode() +
               b"\r\n\r\n" + evil)
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(req)
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        self.assertIn(b" 401 ", data.split(b"\r\n", 1)[0])
        self.assertNotIn(b"smuggled", data)
        self.assertEqual(data.count(b"HTTP/1.1 "), 1)

    def test_raw_upload(self):
        up = {"Content-Type": "image/jpeg", "X-Badger-Upload": "1"}
        self.assertEqual(self.req("POST", "/api/demo/up", member=self.member, raw=b"\xff" * 10,
                                  headers={"Content-Type": "image/jpeg"})[0], 415)       # no upload header
        self.assertEqual(self.req("POST", "/api/demo/up", raw=b"\xff" * 10, headers=up)[0], 401)
        self.assertEqual(self.req("POST", "/api/demo/up", member=self.member, raw=b"\xff" * 2000, headers=up)[0], 413)
        st = self.req("POST", "/api/demo/up", member=self.member, raw=b"\xff" * 10,
                      headers=dict(up, Origin="http://evil.example"))[0]
        self.assertEqual(st, 403)
        st, j, _ = self.req("POST", "/api/demo/up", member=self.member, raw=b"\xff" * 10, headers=up)
        self.assertEqual((st, j), (200, {"bytes": 10}))
        self.assertEqual((self.tmp / "features-data" / "demo" / "up.bin").read_bytes(), b"\xff" * 10)

    def test_file_responses_are_safe(self):
        st, body, h = self.req("GET", "/api/demo/down")
        self.assertEqual(st, 200)
        self.assertEqual(h.get("Content-Security-Policy"), "sandbox")
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
        (self.tmp / "features-data" / "demo").mkdir(parents=True, exist_ok=True)
        (self.tmp / "features-data" / "demo" / "up.bin").write_bytes(b"abc")
        st, body, h = self.req("GET", "/api/demo/file/x", member=self.member)
        self.assertEqual((st, body), (200, b"abc"))
        disp = h["Content-Disposition"]
        self.assertTrue(disp.startswith("attachment;"))
        self.assertNotIn("\r", disp); self.assertNotIn('evil"', disp)

    def test_mesh_send_is_shortened_and_routed(self):
        server._last_post.clear()
        out = self.tmp / "mesh-outbox.jsonl"
        out.write_text("")
        self.assertEqual(self.req("POST", "/api/demo/mesh", {}, admin=self.admin)[1], {"ok": True})
        line = json.loads(out.read_text().splitlines()[-1])
        self.assertEqual(line["board"], "family")
        self.assertLessEqual(len(line["text"].encode()), 200)

    def test_feature_crash_is_a_500_not_a_dead_portal(self):
        self.assertEqual(self.req("GET", "/api/demo/boom")[0], 500)
        self.assertEqual(self.req("GET", "/api/demo/pub")[0], 200)

    def test_routes_outside_api_are_ignored(self):
        self.assertNotEqual(self.req("GET", "/not-api")[1], {"hello": "world", "q": ""})

    def test_feature_config_section(self):
        ctx = server.FEATURES[0]["ctx"]
        self.assertEqual(ctx.cfg(), {"greeting": "hi"})


class Lease(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sdr-"))
        os.environ["PREPPER_SDR_DIR"] = str(self.dir)
        import importlib

        import sdrlease
        self.L = importlib.reload(sdrlease)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def owner(self, refuse_reason=None, stop=None):
        """A pretend alertd in a thread: holds the dongle, yields (or refuses) when asked."""
        L, log = self.L, []

        def run():
            while not stop.is_set():
                with L.hold("weather", poll=0.05):
                    log.append("hold")
                    while not stop.is_set():
                        if L.owner_should_yield("weather"):
                            if refuse_reason:
                                L.refuse("weather", refuse_reason)
                                time.sleep(0.05)
                                continue
                            log.append("yield")
                            break
                        time.sleep(0.05)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return log, t

    def test_borrow_and_hand_back(self):
        stop = threading.Event()
        log, t = self.owner(stop=stop)
        time.sleep(0.2)
        with self.L.borrow("weather", owner="sat", seconds=60, wait=5, poll=0.05):
            self.assertIn("yield", log)
            self.assertFalse(self.L.holder_free("weather"))
        time.sleep(0.4)
        self.assertEqual(log.count("hold"), 2)      # the owner took it back
        stop.set(); t.join(2)

    def test_refused_while_alert(self):
        stop = threading.Event()
        log, t = self.owner(refuse_reason="alert live", stop=stop)
        time.sleep(0.2)
        with self.assertRaises(self.L.Busy) as e, self.L.borrow("weather", seconds=60, wait=5, poll=0.05):
            pass
        self.assertIn("alert live", str(e.exception))
        self.assertIsNone(self.L.borrow_request("weather"))      # request cleaned up
        stop.set(); t.join(2)

    def test_owner_that_never_yields_times_out(self):
        import fcntl
        fd = os.open(self.dir / "weather.lock", os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(self.L.Busy), self.L.borrow("weather", wait=0.3, poll=0.05):
                pass
        finally:
            os.close(fd)

    def test_stale_request_from_dead_process_is_ignored(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
        (self.dir / "weather.borrow").write_text(json.dumps({"pid": p.pid, "until": time.time() + 999}))
        self.assertIsNone(self.L.borrow_request("weather"))
        (self.dir / "weather.borrow").write_text(json.dumps({"pid": os.getpid(), "until": time.time() - 1}))
        self.assertIsNone(self.L.borrow_request("weather"))
        (self.dir / "weather.borrow").write_text("garbage")
        self.assertIsNone(self.L.borrow_request("weather"))


class AudioTap(unittest.TestCase):
    def test_tap_writes_wav_only_while_requested(self):
        import alertd
        d = Path(tempfile.mkdtemp(prefix="tap-"))
        try:
            now = [1000.0]
            req = d / "req.json"
            tap = alertd.Tap(req=req, out=d / "out", clock=lambda: now[0])
            tap.feed(b"\x01\x00" * 100)                       # nobody asked: nothing saved
            self.assertFalse((d / "out").exists())
            req.write_text(json.dumps({"until": 1005}))
            now[0] = 1001
            tap.feed(b"\x01\x00" * 100)
            tap.feed(b"\x02\x00" * 101 + b"\x03")             # odd byte is dropped, frames stay aligned
            now[0] = 1006
            tap.feed(b"\x00\x00" * 10)                        # request over → file finished
            files = list((d / "out").glob("*.wav"))
            self.assertEqual(len(files), 1)
            with wave.open(str(files[0])) as w:
                self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (22050, 1, 201))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_lending_policy(self):
        import alertd
        self.assertIsNone(alertd.lending_allowed({}, {}))
        self.assertIn("sdr_lend", alertd.lending_allowed({}, {"sdr_lend": False}))
        live = {"a": {"expires_ts": time.time() + 600, "event": "Tornado Warning"}}
        self.assertIn("Tornado", alertd.lending_allowed(live, {}))
        old = {"a": {"expires_ts": time.time() - 1, "event": "Tornado Warning"}}
        self.assertIsNone(alertd.lending_allowed(old, {}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
