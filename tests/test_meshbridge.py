"""
Mesh features: alert broadcast, !ai / !status / !q / !help routing, position snapshots for the map,
plus the portal side (brief AI answers, /api/mesh/nodes). No radio needed: the Meshtastic node and
the portal/AI are faked.  Run: python3.11 tests/test_meshbridge.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "meshbridge"))
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"
import meshbridge as mb  # noqa: E402
import server  # noqa: E402


class Fake(BaseHTTPRequestHandler):
    """Stands in for the portal (search, status, ask) and for llama-server."""
    routes: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def _reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        Fake.seen.append((self.command, self.path, body))
        key = self.path.split("?")[0]
        status, ctype, payload = Fake.routes.get(key, (404, "application/json", b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _reply


def sse(*events) -> bytes:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


class FakeServer:
    def __enter__(self):
        Fake.routes, Fake.seen = {}, []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        return self

    def __exit__(self, *a):
        self.srv.shutdown(); self.srv.server_close()


class Fit(unittest.TestCase):
    def test_fits_in_200_bytes_at_a_word_boundary(self):
        s = mb.fit("word " * 100)
        self.assertLessEqual(len(s.encode()), 200)
        self.assertTrue(s.endswith("…"))
        self.assertNotIn("wor…", s)

    def test_multibyte_text_never_splits_a_character(self):
        s = mb.fit("⚠é" * 200)
        s.encode()                                    # would raise on a split character
        self.assertLessEqual(len(s.encode()), 200)

    def test_short_text_untouched_and_whitespace_collapsed(self):
        self.assertEqual(mb.fit("  hello \n world "), "hello world")

    def test_help_fits_one_message(self):
        self.assertLessEqual(len(mb.fit(mb.HELP).encode()), 200)
        self.assertIn("!ai", mb.HELP)


class Alerts(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.alerts = [
            {"id": "TOR-1", "event": "Tornado Warning", "areas": "3 areas in CO", "expires": "Tue 18:30", "expires_ts": self.now + 3600, "test": False},
            {"id": "RWT-1", "event": "Required Weekly Test", "areas": "1 area", "expires": "x", "expires_ts": self.now + 3600, "test": True},
            {"id": "SVR-0", "event": "Severe Thunderstorm Warning", "areas": "a", "expires": "y", "expires_ts": self.now - 1, "test": False},
            {"event": "no id"}, "not a dict",
        ]

    def test_only_new_live_real_alerts(self):
        sent = {}
        out = mb.alerts_to_send(self.alerts, sent, self.now)
        self.assertEqual([a["id"] for a in out], ["TOR-1"])

    def test_sent_once_and_expired_ids_pruned(self):
        sent = {"TOR-1": self.now + 3600, "OLD": self.now - 10}
        self.assertEqual(mb.alerts_to_send(self.alerts, sent, self.now), [])
        self.assertNotIn("OLD", sent)

    def test_garbage_file_is_harmless(self):
        self.assertEqual(mb.alerts_to_send({"oops": 1}, {}, self.now), [])

    def test_format(self):
        s = mb.format_alert(self.alerts[0])
        self.assertIn("Tornado Warning", s)
        self.assertIn("until Tue 18:30", s)
        self.assertLessEqual(len(s.encode()), 200)


class Positions(unittest.TestCase):
    def test_snapshot(self):
        now = 1_800_000_000
        nodes = {
            "!a": {"user": {"id": "!a", "longName": "Ana", "shortName": "ANA"}, "position": {"latitude": 39.74, "longitude": -104.99, "time": now - 60},
                   "deviceMetrics": {"batteryLevel": 81}},
            "!b": {"user": {"id": "!b", "longName": "Ben"}, "position": {"latitudeI": 397400000, "longitudeI": -1049900000}, "lastHeard": now - 7200},
            "!c": {"user": {"id": "!c"}, "position": {"latitude": 0, "longitude": 0}},           # no fix
            "!d": {"user": {"id": "!d"}, "position": {"latitude": 95, "longitude": 10}},          # impossible
            "!e": {"user": {"id": "!e"}, "position": {"latitude": 1, "longitude": 1, "time": now - 30 * 86400}},  # ancient
            "!f": {"user": {"id": "!f"}},                                                         # never shared
            "!me": {"user": {"id": "!me", "longName": "Badger"}, "position": {"latitude": 39.7, "longitude": -105.0, "time": now}},
            "!g": {"user": {"id": "!g"}, "position": {"latitude": float("nan"), "longitude": 1}},
        }
        snap = mb.nodes_snapshot(nodes, "!me", now)
        self.assertEqual([n["id"] for n in snap], ["!me", "!a", "!b"])
        ana = snap[1]
        self.assertEqual((ana["name"], ana["short"], ana["battery"]), ("Ana", "ANA", 81))
        self.assertAlmostEqual(snap[2]["lat"], 39.74)
        self.assertTrue(snap[0]["self"])
        self.assertFalse(ana["self"])


class Status(unittest.TestCase):
    def test_on_battery_with_alert(self):
        s = mb.format_status({"power": {"percent": 63.4, "external": False, "charging": False, "runtime_min": 380, "mode": "endurance"},
                              "alerts": [{"event": "Tornado Warning", "expires": "Tue 18:30", "test": False}], "clients": 4})
        self.assertIn("battery 63%", s)
        self.assertIn("on battery", s)
        self.assertIn("~6.3 h left", s)
        self.assertIn("Endurance", s)
        self.assertIn("1 alert", s)
        self.assertIn("Tornado Warning until Tue 18:30", s)
        self.assertIn("4 on Wi-Fi", s)
        self.assertLessEqual(len(s.encode()), 200)

    def test_solar_and_no_alerts(self):
        s = mb.format_status({"power": {"percent": 90, "external": True, "charging": True, "input_w": 14.2, "mode": "balanced"},
                              "alerts": [{"event": "Test", "test": True}], "clients": 0})
        self.assertIn("14.2 W in", s)
        self.assertIn("no alerts", s)

    def test_no_power_data(self):
        # CHANGED (red team finding 6): nothing sent over the air names the device
        self.assertTrue(mb.format_status({}).startswith("Status:"))
        self.assertNotIn("Badger", mb.format_status({}) + mb.HELP + mb.help_text({}, False))


class Routing(unittest.TestCase):
    def make(self, **kw):
        self.sent, self.t = [], [1000.0]
        jobs = []
        c = mb.Commands(lambda text, dest: self.sent.append((dest, text)), clock=lambda: self.t[0],
                        library=lambda q: f"LIB:{q}", ai=lambda q: f"AI:{q}", status=lambda: "STATUS",
                        spawn=jobs.append, **kw)
        return c, jobs

    def test_commands(self):
        c, jobs = self.make()
        self.assertFalse(c.handle("hello all", "!a"))                # ordinary chat is not a command
        self.assertFalse(c.handle("!unknown thing", "!a"))
        self.assertTrue(c.handle("!help", "!a"))
        self.t[0] += 31; c.handle("!status", "!a")
        self.t[0] += 31; c.handle("!q water purification", "!a")
        self.assertEqual([t for _, t in self.sent][1:], ["STATUS", "LIB:water purification"])
        self.assertTrue(all(d == "!a" for d, _ in self.sent))       # replies go to the asker only

    def test_flood_is_ignored(self):
        c, _ = self.make()
        c.handle("!status", "!a"); c.handle("!status", "!a"); c.handle("!status", "!a")
        self.assertEqual(len(self.sent), 1)
        c.handle("!status", "!b")                                   # other radios aren't affected
        self.assertEqual(len(self.sent), 2)

    def test_ai_runs_in_background_one_at_a_time(self):
        c, jobs = self.make()
        c.handle("!ai how to purify water", "!a")
        self.assertIn("Thinking", self.sent[-1][1])
        self.assertEqual(len(jobs), 1)
        c.handle("!ai another", "!b")                               # worker still busy
        self.assertIn("busy", self.sent[-1][1])
        jobs[0]()                                                   # the answer arrives
        self.assertEqual(self.sent[-1], ("!a", "AI:how to purify water"))
        self.t[0] += 31
        c.handle("!ai again", "!a")                                 # inside the 120 s AI cooldown
        self.assertIn("One AI question every 120 s", self.sent[-1][1])
        self.t[0] += 120
        c.handle("!ai again", "!a")
        self.assertIn("Thinking", self.sent[-1][1])

    def test_ai_disabled_falls_back_to_library(self):
        c, jobs = self.make(ai_enabled=False)
        c.handle("!ai splint a leg", "!a")
        self.assertEqual(self.sent[-1][1], "LIB:splint a leg")
        self.assertEqual(jobs, [])

    def test_usage_messages(self):
        c, _ = self.make()
        c.handle("!q", "!a"); self.t[0] += 31; c.handle("!ai", "!a")
        self.assertEqual([t for _, t in self.sent], ["Usage: !q <words>", "Usage: !ai <question>"])

    def test_ai_crash_releases_the_worker(self):
        sent, jobs = [], []
        c = mb.Commands(lambda t, d: sent.append(t), clock=lambda: 0.0, ai=lambda q: 1 / 0, spawn=jobs.append)
        c.handle("!ai x", "!a"); jobs[0]()
        self.assertTrue(c.ai_busy.acquire(blocking=False))


class AiAnswer(unittest.TestCase):
    def test_brief_answer_with_source(self):
        with FakeServer() as f:
            mb.API = f.url
            Fake.routes["/api/ask"] = (200, "text/event-stream", sse(
                ("sources", [{"n": 1, "title": "Water purification"}]),
                ("token", "Boil water for one minute, "), ("token", "or three above 2,000 m."), ("done", {})))
            out = mb.ai_answer("how long to boil water")
            self.assertEqual(out, "Boil water for one minute, or three above 2,000 m. [Water purification]")
            method, path, body = Fake.seen[-1]
            self.assertEqual((method, path), ("POST", "/api/ask"))
            self.assertTrue(json.loads(body)["brief"])

    def test_long_answer_trimmed_with_source_kept(self):
        with FakeServer() as f:
            mb.API = f.url
            Fake.routes["/api/ask"] = (200, "text/event-stream", sse(
                ("sources", [{"title": "Hypothermia"}]), ("token", "warm the core first " * 40), ("done", {})))
            out = mb.ai_answer("hypothermia")
            self.assertLessEqual(len(out.encode()), 200)
            self.assertTrue(out.endswith("[Hypothermia]"))

    def test_busy_and_offline(self):
        with FakeServer() as f:
            mb.API = f.url
            Fake.routes["/api/ask"] = (503, "application/json", b'{"error":"busy"}')
            self.assertIn("busy", mb.ai_answer("x"))
            Fake.routes["/api/ask"] = (200, "text/event-stream", sse(
                ("sources", []), ("token", "(The AI model isn't answering — down.)"), ("done", {})))
            Fake.routes["/api/search"] = (200, "application/json", json.dumps(
                {"articles": [{"title": "Splints", "snippet": "Pad the splint well."}]}).encode())
            self.assertEqual(mb.ai_answer("splint"), "Splints: Pad the splint well.")   # library fallback
        mb.API = "http://127.0.0.1:9"                                                 # nothing listening
        self.assertEqual(mb.ai_answer("x"), "Library unavailable.")


class Portal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="mesh-portal-"))
        cfg = cls.tmp / "prepper.toml"
        cfg.write_text(f'[portal]\nstandalone = true\nmesh_nodes = "{cls.tmp}/mesh-nodes.json"\nctl = "/nonexistent"\nservices = {{}}\n')
        server.CFG = server.PortalConfig(cfg)

    def test_mesh_nodes_validated(self):
        now = time.time()
        (self.tmp / "mesh-nodes.json").write_text(json.dumps({"updated": now, "nodes": [
            {"id": "!a", "name": "Ana", "short": "ANA", "lat": 39.7, "lon": -105.0, "heard": now - 120, "battery": 80, "self": False},
            {"id": "!x", "name": "bad", "lat": "39", "lon": 1},
            {"id": "!y", "lat": 200, "lon": 1},
            {"id": "<script>", "name": "x" * 99, "lat": 1.5, "lon": 2.5, "self": "yes"}, 7]}))
        res = server.mesh_nodes()
        self.assertEqual([n["id"] for n in res["nodes"]], ["!a", "<script>"])
        a = res["nodes"][0]
        self.assertTrue(110 <= a["age_s"] <= 130)
        self.assertEqual(len(res["nodes"][1]["name"]), 40)
        self.assertFalse(res["nodes"][1]["self"])                 # only a real true counts

    def test_missing_or_garbage_file(self):
        (self.tmp / "mesh-nodes.json").write_text("not json")
        self.assertEqual(server.mesh_nodes()["nodes"], [])
        (self.tmp / "mesh-nodes.json").unlink()
        self.assertEqual(server.mesh_nodes()["nodes"], [])

    def test_brief_prompt_and_token_budget(self):
        with FakeServer() as f:
            server.CFG.llama = f.url
            Fake.routes["/v1/chat/completions"] = (200, "text/event-stream",
                b'data: {"choices":[{"delta":{"content":"Boil it."}}]}\n\ndata: [DONE]\n\n')
            orig_search, orig_text = server.kiwix_search, server.article_text
            server.kiwix_search = lambda q, count=6, books=None: [{"title": "Water", "url": "/k/w", "source": "wiki", "fetch": "x"}]
            server.article_text = lambda url, limit=2400: "Boil water one minute."
            try:
                for brief, budget in ((True, 90), (False, 600)):
                    out = []
                    server.ask_stream("boil?", lambda e, d, out=out: out.append((e, d)), brief=brief)
                    body = json.loads(Fake.seen[-1][2])
                    self.assertEqual(body["max_tokens"], budget)
                    self.assertEqual(server.ASK_BRIEF in body["messages"][0]["content"], brief)
                    self.assertIn(("token", "Boil it."), out)
            finally:
                server.kiwix_search, server.article_text = orig_search, orig_text


if __name__ == "__main__":
    unittest.main(verbosity=1)
