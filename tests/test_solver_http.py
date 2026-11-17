"""
Problem solver over HTTP through the real portal, with a fake llama-server (which also plays the
model switch done by `prepperctl ai-model`): deep-solve jobs (progress, answer, tokens/s), the
queue (position, one job per phone, queue full, cancel), job privacy (per IP / per family member),
rate limits, the writing redirect, the family-only supplies tool, the calculators endpoint, the
admin AI-models panel (roles, self-test benchmark scoring, promotion only if at least as good,
force, roll back, refusing a model that doesn't fit in free RAM, idle swap back, battery mode),
admin settings, and the job store surviving a restart. Run: python3.11 tests/test_solver_http.py
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software"))
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"

import server  # noqa: E402
from solver import bench, jobs  # noqa: E402

BENCH = {  # keyword in the problem → (correct answer, tool call)
    "17/24": ("61/72", ("calc", {"expr": "17/24 + 5/36"})),
    "gallons": ("208.198", ("convert", {"value": "55 gal", "to": "L"})),
    "cliff": ("19.809", ("calc", {"expr": "sqrt(2 * 9.81 m/s^2 * 20 m)"})),
    "60 W": ("5", ("calc", {"expr": "60 W / 12 V"})),
    "Zero-sum": ("1", ("zero_sum", {"payoffs": [[3, -1], [-2, 4]]})),
    "prime": ("76127", None),
}


class Llama(BaseHTTPRequestHandler):
    current = "model_qwen3_4b"
    data_root = ""
    bad: set = set()                 # models that get some self-test answers wrong
    delay = 0.0
    down = False
    calls: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if Llama.down:
            return self._send(503, b"{}")
        if self.path == "/ai/health":
            return self._send(200, b'{"status":"ok"}')
        if self.path == "/ai/props":
            return self._send(200, json.dumps({"model_path": f"{Llama.data_root}/models/{Llama.current}.current",
                                               "default_generation_settings": {"n_ctx": 4096}}).encode())
        self._send(404, b"{}")

    def reply_for(self, msgs) -> str:
        problem = msgs[1]["content"]
        last = msgs[-1]["content"]
        Llama.calls.append((Llama.current, last[:40]))
        key = next((k for k in BENCH if k in problem), None)
        if "Step 1" in last:
            return "<think>let me think</think><plan>Knowns and unknowns listed; use a calculator.</plan>"
        if "Step 2" in last:
            if "supplies" in problem:
                return '<tool_call>{"tool": "supplies", "args": {}}</tool_call>'
            if key and BENCH[key][1]:
                name, args = BENCH[key][1]
                return "<tool_call>" + json.dumps({"tool": name, "args": args}) + "</tool_call>"
            if key:
                return "<answer>draft</answer>"
            return '<tool_call>{"tool": "calc", "args": {"expr": "6 * 7"}}</tool_call>'
        if last.startswith("<data"):
            return "<answer>draft</answer>"
        data = [m["content"] for m in msgs if m["role"] == "user" and m["content"].startswith("<data")]
        echo = data[-1][:300] if data else ""
        if key:
            ans = BENCH[key][0]
            if Llama.current in Llama.bad and key in ("17/24", "cliff", "prime"):
                ans = "12345"
            return f"<answer>Answer: {ans}\nWorking: {echo}\nConfidence: high\nFINAL: {ans}</answer>"
        return f"<answer>Answer: 42\nWorking: {echo}\nChecks: 6 × 7 = 42 by repeated addition.\nConfidence: high</answer>"

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n))
        if Llama.down:
            return self._send(503, b"{}")
        text = self.reply_for(req["messages"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for piece in re.findall(r".{1,6}", text, re.S):
                if Llama.delay:
                    time.sleep(Llama.delay)
                self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        except OSError:
            pass


SUPPLIES = {"items": {"water123": {"name": "Water jugs", "category": "water", "qty": 10, "unit": "gal"},
                      "rice1234": {"name": "Rice", "category": "food", "qty": 20, "unit": "kg", "kcal": 3600}},
            "household": {"people": 4, "set": True}}


class SolverHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="solver-")).resolve()
        data = cls.tmp / "data"
        (data / "models").mkdir(parents=True)
        for mid in ("model_qwen3_4b", "model_qwen3_8b", "model_qwen3_4b_thinking", "model_qwen25_1_5b"):
            (data / "models" / f"{mid}.current").write_bytes(b"GGUF" + b"\0" * 100)
        cls.meminfo = cls.tmp / "meminfo"
        cls.set_mem(7_400_000)
        cls.variant = cls.tmp / "llama-variant"
        Llama.data_root = str(data)
        cls.llama = ThreadingHTTPServer(("127.0.0.1", 0), Llama)
        cls.llama.daemon_threads = True
        threading.Thread(target=cls.llama.serve_forever, daemon=True).start()
        feats = cls.tmp / "features"
        feats.mkdir()
        shutil.copy(REPO / "software" / "portal" / "features" / "solver.py", feats / "solver.py")
        shutil.copy(REPO / "software" / "portal" / "features" / "supplies.py", feats / "supplies.py")
        (cls.tmp / "features-data" / "supplies").mkdir(parents=True)
        (cls.tmp / "features-data" / "supplies" / "supplies.json").write_text(json.dumps(SUPPLIES))
        cfg = cls.tmp / "prepper.toml"
        cfg.write_text(f"""
[upd]
data_root = "{data}"
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
llama = "http://127.0.0.1:{cls.llama.server_address[1]}/ai"
kiwix = "http://127.0.0.1:9/kiwix"
ctl = "/nonexistent/prepperctl"
services = {{}}

[ai]
model = "model_qwen3_4b"
endurance_model = "model_qwen25_1_5b"

[solver]
per_ip_per_hour = 4
max_queue = 3
calc_per_10min = 15
sandbox_socket = "{cls.tmp}/no-sandbox.sock"
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        cls.feat = next(f for f in server.FEATURES if f["id"] == "solver")
        cls.mod = cls.feat["mod"]
        S = cls.mod.S  # noqa: N806
        S.models.meminfo_path = str(cls.meminfo)
        S.models.variant_file = str(cls.variant)
        S.models.sleep = lambda s: None
        cls.ctl_calls = []
        S.models.ctl = cls.fake_ctl
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
        cls.llama.shutdown()
        cls.llama.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def fake_ctl(cls, *args):
        cls.ctl_calls.append(args)
        if args[:1] == ("ai-model",):
            Llama.current = args[1]
            return 0, f"AI model: {args[1]}"
        return 1, "unknown command"

    @classmethod
    def set_mem(cls, avail_kb, total_kb=8_000_000):
        cls.meminfo.write_text(f"MemTotal:       {total_kb} kB\nMemFree:          100000 kB\nMemAvailable:   {avail_kb} kB\n")

    @classmethod
    def req(cls, method, path, body=None, ip="10.0.0.5", member=None, admin=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=30)
        h = {"Content-Type": "application/json", "X-Real-IP": ip}
        if member:
            h["X-Member-Token"] = member
        if admin:
            h["X-Admin-Token"] = admin
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def setUp(self):
        Llama.delay = 0.0
        Llama.down = False
        Llama.bad = set()
        self.mod.S.hits.clear()
        self.mod.S.calc_hits.clear()
        self.set_mem(7_400_000)
        self.variant.write_text("default")
        self.wait_idle()

    def wait_idle(self, timeout=30):
        q = self.mod.S.queue
        t = time.time()
        while time.time() - t < timeout:
            st = q.stats()
            if not st["queued"] and not st["running"]:
                return
            time.sleep(0.05)
        self.fail("queue never went idle")

    def wait(self, jid, ip="10.0.0.5", member=None, admin=None, timeout=30):
        t = time.time()
        events = []
        while time.time() - t < timeout:
            st, j = self.req("GET", f"/api/solver/job?id={jid}&since={len(events)}", ip=ip, member=member, admin=admin)
            self.assertEqual(st, 200, j)
            events += j["events"]
            if j["state"] in jobs.FINAL:
                j["all_events"] = events
                return j
            time.sleep(0.05)
        self.fail("job never finished")

    def submit(self, problem, ip="10.0.0.5", member=None):
        return self.req("POST", "/api/solver/solve", {"problem": problem}, ip=ip, member=member)

    # ── deep solve ──
    def test_info(self):
        st, i = self.req("GET", "/api/solver/info")
        self.assertEqual(st, 200)
        self.assertTrue(i["enabled"] and i["ai"])
        self.assertFalse(i["python"])                         # no verified sandbox here → no Python tool
        self.assertFalse(i["admin"] or i["member"] or i["supplies"])
        self.assertNotIn("python_note", i)
        st, i = self.req("GET", "/api/solver/info", admin=self.admin)
        self.assertIn("isn't installed", i["python_note"])

    def test_solve_end_to_end(self):
        st, r = self.submit("A 60 W light runs from a 12 V battery: how many amps?")
        self.assertEqual(st, 200, r)
        j = self.wait(r["id"])
        self.assertEqual(j["state"], "done", j)
        res = j["result"]
        self.assertIn("FINAL: 5", res["answer"])
        self.assertEqual(res["tools_used"], ["calc"])
        self.assertEqual(res["confidence"], "high")
        kinds = [e["kind"] for e in j["all_events"]]
        for k in ("model", "plan", "tool", "result"):
            self.assertIn(k, kinds)
        self.assertGreater(j["tokens"], 10)
        self.assertGreaterEqual(j["elapsed"], 0)
        self.assertIn("Qwen3 8B", j["model"])                 # the reasoning role's model was loaded for it
        self.assertEqual(Llama.current, "model_qwen3_8b")
        self.assertIn(("ai-model", "model_qwen3_8b"), self.ctl_calls)

    def test_writing_redirect_and_validation(self):
        st, r = self.submit("Craft me a better introduction letter")
        self.assertEqual(st, 200)
        self.assertIn("problem solver", r["redirect"])
        self.assertNotIn("id", r)
        for body in ({"problem": ""}, {"problem": "x" * 2001}, {"problem": 5}, {}, {"problem": ["a"]}):
            with self.subTest(body=str(body)[:30]):
                st, r = self.req("POST", "/api/solver/solve", body, ip="10.0.0.40")
                self.assertEqual(st, 400)

    def test_queue_one_per_phone_cancel_and_privacy(self):
        Llama.delay = 0.05
        st, a = self.submit("What is 6 times 7, slowly?", ip="10.0.1.1")
        self.assertEqual(st, 200)
        st, r = self.submit("And 7 times 8?", ip="10.0.1.1")
        self.assertEqual(st, 409)                             # one problem per phone at a time
        st, b = self.submit("What is 3 times 4?", ip="10.0.1.2")
        self.assertEqual(st, 200)
        self.assertEqual(b["position"], 1)
        st, j = self.req("GET", f"/api/solver/job?id={b['id']}", ip="10.0.1.2")
        self.assertEqual((j["state"], j["position"]), ("queued", 1))
        # other phones can't see or cancel it
        self.assertEqual(self.req("GET", f"/api/solver/job?id={b['id']}", ip="10.0.1.3")[0], 404)
        self.assertEqual(self.req("POST", "/api/solver/cancel", {"id": b["id"]}, ip="10.0.1.3")[0], 404)
        self.assertEqual(self.req("GET", "/api/solver/job?id=../../etc/passwd")[0], 404)
        # cancel the queued one, then the running one
        self.assertEqual(self.req("POST", "/api/solver/cancel", {"id": b["id"]}, ip="10.0.1.2")[0], 200)
        self.assertEqual(self.wait(b["id"], ip="10.0.1.2")["state"], "cancelled")
        time.sleep(0.3)
        self.assertEqual(self.req("POST", "/api/solver/cancel", {"id": a["id"]}, ip="10.0.1.1")[0], 200)
        t = time.time()
        self.assertEqual(self.wait(a["id"], ip="10.0.1.1")["state"], "cancelled")
        self.assertLess(time.time() - t, 5)
        self.assertEqual(self.req("POST", "/api/solver/cancel", {"id": a["id"]}, ip="10.0.1.1")[0], 404)

    def test_queue_full(self):
        Llama.delay = 0.05
        ids = []
        for k in range(4):
            st, r = self.submit(f"What is {k} plus 1?", ip=f"10.0.2.{k}")
            self.assertEqual(st, 200, r)
            ids.append((r["id"], f"10.0.2.{k}"))
        st, r = self.submit("One more?", ip="10.0.2.9")
        self.assertEqual(st, 503)
        self.assertTrue(r["busy"])
        for jid, ip in ids:
            self.req("POST", "/api/solver/cancel", {"id": jid}, ip=ip)

    def test_rate_limit(self):
        for k in range(4):
            st, r = self.submit(f"What is {k} squared?", ip="10.0.3.1")
            self.assertEqual(st, 200, r)
            self.wait(r["id"], ip="10.0.3.1")
        st, r = self.submit("What is 5 squared?", ip="10.0.3.1")
        self.assertEqual(st, 429)

    def test_supplies_only_for_family_members(self):
        st, r = self.submit("How many days will our supplies of water last?", member=self.member, ip="10.0.4.1")
        self.assertEqual(st, 200, r)
        j = self.wait(r["id"], ip="10.0.4.1", member=self.member)
        self.assertIn('"days"', j["result"]["answer"])
        self.assertIn("supplies", j["result"]["tools_used"])
        # the member's job is invisible to anyone else, even from the same address
        self.assertEqual(self.req("GET", f"/api/solver/job?id={r['id']}", ip="10.0.4.1")[0], 404)
        self.assertEqual(self.req("GET", f"/api/solver/job?id={r['id']}", ip="10.0.4.1", admin=self.admin)[0], 404)
        # a visitor asking the same gets no supplies tool at all
        st, r = self.submit("How many days will our supplies of water last?", ip="10.0.4.2")
        j = self.wait(r["id"], ip="10.0.4.2")
        self.assertNotIn("Water jugs", json.dumps(j))
        self.assertNotIn('"days"', j["result"]["answer"])
        self.assertIn("no tool called", j["result"]["answer"])
        st, i = self.req("GET", "/api/solver/info", member=self.member)
        self.assertTrue(i["supplies"] and i["member"])

    def test_ai_down(self):
        Llama.down = True
        st, r = self.submit("What is 2 plus 2?", ip="10.0.5.1")
        j = self.wait(r["id"], ip="10.0.5.1")
        self.assertEqual(j["state"], "error")
        self.assertIn("calculators", j["error"])

    def test_model_that_does_not_fit_is_not_loaded(self):
        Llama.current = "model_qwen3_4b"
        self.set_mem(1_500_000)                                 # ~1.4 GB free: the 8B needs far more
        st, r = self.submit("What is 6 times 7?", ip="10.0.6.1")
        j = self.wait(r["id"], ip="10.0.6.1")
        self.assertEqual(j["state"], "done")
        self.assertEqual(Llama.current, "model_qwen3_4b")
        self.assertTrue(any("Not enough free memory" in w for w in j["result"]["warnings"]), j["result"]["warnings"])
        st, r = self.req("POST", "/api/solver/models", {"op": "load", "id": "model_qwen3_8b"}, admin=self.admin)
        self.assertEqual(st, 409)
        self.assertIn("Not enough free memory", r["error"])
        self.set_mem(6_000_000, total_kb=4_000_000)            # a 4 GB Badger can never take the 8B
        ok, why = self.mod.S.models.fits("model_qwen3_8b")
        self.assertFalse(ok)
        self.assertIn("8 GB", why)
        self.meminfo.write_text("garbage")
        self.assertFalse(self.mod.S.models.fits("model_qwen3_4b")[0])

    def test_battery_mode_keeps_small_model(self):
        Llama.current = "model_qwen25_1_5b"
        self.variant.write_text("endurance")
        calls = len(self.ctl_calls)
        st, r = self.submit("What is 6 times 7?", ip="10.0.7.1")
        j = self.wait(r["id"], ip="10.0.7.1")
        self.assertEqual(len(self.ctl_calls), calls)
        self.assertTrue(any("Battery-saving" in w for w in j["result"]["warnings"]))

    def test_switch_unsupported_falls_back(self):
        S = self.mod.S  # noqa: N806
        Llama.current = "model_qwen3_4b"
        S.models.ctl = lambda *a: (0, "(dev) would run: prepperctl ai-model x")
        try:
            st, r = self.submit("What is 6 times 7?", ip="10.0.8.1")
            j = self.wait(r["id"], ip="10.0.8.1")
            self.assertEqual(j["state"], "done")
            self.assertTrue(any("can't switch AI models" in w for w in j["result"]["warnings"]))
            self.assertFalse(S.models.switch_supported())
        finally:
            S.models.ctl = self.fake_ctl
            S.models._switch_ok_until = 0

    # ── calculators ──
    def test_calculators(self):
        st, r = self.req("GET", "/api/solver/calculators")
        self.assertEqual(st, 200)
        self.assertTrue(any(c["name"] == "bleach_dose" for c in r["calculators"]))
        st, r = self.req("POST", "/api/solver/calc", {"tool": "bleach_dose", "args": {"volume": "55 gal", "bleach_pct": 6}}, ip="10.0.9.1")
        self.assertEqual((st, r["result"]["drops"]), (200, 440))
        for body in ({"tool": "python", "args": {"code": "1"}}, {"tool": "supplies"}, {"tool": "nope"},
                     {"tool": "calc", "args": {"expr": "3 m + 2 s"}}, {"tool": "calc", "args": "x"}, {"tool": ["calc"]}):
            with self.subTest(body=str(body)[:40]):
                st, r = self.req("POST", "/api/solver/calc", body, ip="10.0.9.2")
                self.assertEqual(st, 400)
                self.assertFalse(r["ok"])
        codes = [self.req("POST", "/api/solver/calc", {"tool": "calc", "args": {"expr": "1+1"}}, ip="10.0.9.3")[0] for _ in range(16)]
        self.assertEqual(codes[-1], 429)

    # ── admin: models ──
    def test_models_panel_access(self):
        self.assertEqual(self.req("GET", "/api/solver/models")[0], 401)
        self.assertEqual(self.req("GET", "/api/solver/models", member=self.member)[0], 401)
        self.assertEqual(self.req("POST", "/api/solver/models", {"op": "select"}, member=self.member)[0], 401)
        st, m = self.req("GET", "/api/solver/models", admin=self.admin)
        self.assertEqual(st, 200)
        roles = {r["role"]: r for r in m["roles"]}
        self.assertEqual(set(roles), {"fast", "reasoning", "coder", "endurance"})
        self.assertEqual(roles["fast"]["active"], "model_qwen3_4b")
        reasoning = {c["id"]: c for c in roles["reasoning"]["candidates"]}
        self.assertIn("model_qwen3_8b", reasoning)
        self.assertFalse(reasoning["model_qwen3_8b"]["verified"])
        self.assertTrue(reasoning["model_qwen3_8b"]["installed"])
        coder = roles["coder"]["candidates"][0]
        self.assertIsNone(coder["installed"])                    # not downloaded in this fixture
        self.assertEqual(len(m["benchmark"]), len(bench.PROBLEMS))
        for body in ({"op": "explode"}, {"op": "select", "role": "boss", "id": "x"}, {"op": "bench", "id": "../x"},
                     {"op": "bench", "id": "model_nope"}, {"op": "bench", "id": "model_qwen25_coder_1_5b"}):
            with self.subTest(body=body):
                self.assertIn(self.req("POST", "/api/solver/models", body, admin=self.admin)[0], (400, 409))

    def bench_run(self, mid):
        st, r = self.req("POST", "/api/solver/models", {"op": "bench", "id": mid}, admin=self.admin, ip="10.0.10.1")
        self.assertEqual(st, 200, r)
        j = self.wait(r["id"], ip="10.0.10.1", admin=self.admin, timeout=60)
        self.assertEqual(j["state"], "done", j)
        return j["result"]["bench"]

    def test_benchmark_promotion_and_rollback(self):
        S = self.mod.S  # noqa: N806
        st, r = self.req("POST", "/api/solver/models", {"op": "select", "role": "reasoning", "id": "model_qwen3_4b_thinking"},
                         admin=self.admin)
        self.assertEqual(st, 409)
        self.assertIn("self-test", r["error"])                   # no score yet: not promoted
        Llama.delay = 0.001
        b8 = self.bench_run("model_qwen3_8b")
        self.assertEqual((b8["score"], b8["total"]), (6, 6))
        self.assertGreater(b8["tps"], 0)
        Llama.bad = {"model_qwen3_4b_thinking"}
        b4 = self.bench_run("model_qwen3_4b_thinking")
        self.assertEqual(b4["score"], 3)
        self.assertEqual([d["ok"] for d in b4["details"]], [False, True, False, True, True, False])
        self.assertEqual(S.models.bench("model_qwen3_4b_thinking")["score"], 3)
        st, r = self.req("POST", "/api/solver/models", {"op": "select", "role": "reasoning", "id": "model_qwen3_4b_thinking"},
                         admin=self.admin)
        self.assertEqual(st, 409)
        self.assertIn("below", r["error"])                       # worse than the current model: refused
        st, r = self.req("POST", "/api/solver/models", {"op": "select", "role": "reasoning", "id": "model_qwen3_4b_thinking",
                                                        "force": True}, admin=self.admin)
        self.assertEqual(st, 200, r)
        self.assertEqual(S.models.active("reasoning"), "model_qwen3_4b_thinking")
        st, r = self.req("POST", "/api/solver/models", {"op": "rollback", "role": "reasoning"}, admin=self.admin)
        self.assertEqual(st, 200, r)
        self.assertEqual(S.models.active("reasoning"), "model_qwen3_8b")
        st, m = self.req("GET", "/api/solver/models", admin=self.admin)
        reasoning = next(r for r in m["roles"] if r["role"] == "reasoning")
        self.assertEqual(reasoning["previous"], "model_qwen3_4b_thinking")
        self.assertEqual(next(c for c in reasoning["candidates"] if c["id"] == "model_qwen3_8b")["bench"]["score"], 6)
        self.assertEqual(self.req("POST", "/api/solver/models", {"op": "select", "role": "fast", "id": "model_qwen3_8b"},
                                  admin=self.admin)[0], 409)     # wrong role

    def test_idle_swap_back_to_fast(self):
        S = self.mod.S  # noqa: N806
        Llama.current = "model_qwen3_8b"
        S.models._loaded_cache = None
        S.models._last_use = time.time() - 3600
        self.assertEqual(S.models.idle_check(10), "model_qwen3_4b")
        self.assertEqual(Llama.current, "model_qwen3_4b")
        S.models._last_use = time.time()
        Llama.current = "model_qwen3_8b"
        self.assertIsNone(S.models.idle_check(10))               # recently used: stays
        Llama.current = "model_qwen3_4b"

    def test_admin_switch(self):
        self.assertEqual(self.req("POST", "/api/solver/settings", {"enabled": False})[0], 401)
        self.assertEqual(self.req("POST", "/api/solver/settings", {"enabled": "no"}, admin=self.admin)[0], 400)
        self.assertEqual(self.req("POST", "/api/solver/settings", {"enabled": False}, admin=self.admin)[0], 200)
        try:
            self.assertEqual(self.submit("What is 2 plus 2?", ip="10.0.11.1")[0], 403)
        finally:
            self.req("POST", "/api/solver/settings", {"enabled": True}, admin=self.admin)
        self.assertEqual(self.submit("What is 2 plus 2?", ip="10.0.11.1")[0], 200)

    def test_recent_jobs_listed_for_owner_only(self):
        st, r = self.submit("What is 9 times 9?", ip="10.0.12.1")
        self.wait(r["id"], ip="10.0.12.1")
        st, i = self.req("GET", "/api/solver/info", ip="10.0.12.1")
        self.assertIn(r["id"], [x["id"] for x in i["recent"]])
        st, i = self.req("GET", "/api/solver/info", ip="10.0.12.2")
        self.assertNotIn(r["id"], [x["id"] for x in i["recent"]])


class Queue(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sq-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_runs_in_order_and_records_progress(self):
        order = []

        def runner(job, progress, cancel, stats):
            order.append(job["payload"]["n"])
            for k in range(200):
                progress("tool", f"step {k}", ok=True, junk=object())
            stats(tokens=42, gen_seconds=2.0, model="m")
            return {"answer": str(job["payload"]["n"])}
        q = jobs.JobQueue(self.dir, runner)
        ids = [q.submit("solve", {"n": n}, {"ip": "1.1.1.1"})[0]["id"] for n in range(3)]
        t = time.time()
        while q.stats()["queued"] or q.stats()["running"]:
            time.sleep(0.02)
            self.assertLess(time.time() - t, 10)
        self.assertEqual(order, [0, 1, 2])
        j = q.get(ids[0], {"ip": "1.1.1.1"})
        self.assertEqual((j["state"], j["result"]["answer"], j["tokens"]), ("done", "0", 42))
        self.assertLessEqual(len(j["events"]), jobs.MAX_EVENTS)
        self.assertNotIn("junk", j["events"][-1])
        self.assertIsNone(q.get(ids[0], {"ip": "2.2.2.2"}))
        self.assertEqual(oct((self.dir / f"{ids[0]}.json").stat().st_mode & 0o777), "0o600")

    def test_survives_restart_and_expires(self):
        gate = threading.Event()

        def runner(job, progress, cancel, stats):
            gate.wait(5)
            return {}
        q = jobs.JobQueue(self.dir, runner)
        running = q.submit("solve", {}, {"ip": "1.1.1.1"})[0]["id"]
        time.sleep(0.2)
        queued = q.submit("solve", {}, {"ip": "1.1.1.2"})[0]["id"]
        (self.dir / "junk.json").write_text("{not json")
        (self.dir / ("A" * 20 + ".json")).write_text(json.dumps({"id": "B" * 20}))
        q2 = jobs.JobQueue(self.dir, runner)                        # "the portal restarted"
        for jid, ip in ((running, "1.1.1.1"), (queued, "1.1.1.2")):
            j = q2.get(jid, {"ip": ip})
            self.assertEqual(j["state"], "interrupted")
            self.assertIn("restarted", j["error"])
        self.assertFalse((self.dir / "junk.json").exists())
        gate.set()
        now = [time.time() + 13 * 3600]
        q3 = jobs.JobQueue(self.dir, runner, clock=lambda: now[0])
        self.assertIsNone(q3.get(running, {"ip": "1.1.1.1"}))      # past the ttl: gone

    def test_cancel_running_and_errors(self):
        def runner(job, progress, cancel, stats):
            if job["payload"].get("fail"):
                raise RuntimeError("boom")
            cancel.wait(5)
            return {}
        q = jobs.JobQueue(None, runner)
        jid = q.submit("solve", {}, {"ip": "a"})[0]["id"]
        time.sleep(0.1)
        self.assertFalse(q.cancel(jid, {"ip": "b"}))
        self.assertTrue(q.cancel(jid, {"ip": "a"}))
        time.sleep(0.2)
        self.assertEqual(q.get(jid, {"ip": "a"})["state"], "cancelled")
        jid = q.submit("solve", {"fail": True}, {"ip": "a"})[0]["id"]
        time.sleep(0.2)
        j = q.get(jid, {"ip": "a"})
        self.assertEqual((j["state"], j["error"]), ("error", "boom"))

    def test_member_jobs_are_private(self):
        q = jobs.JobQueue(None, lambda *a: {})
        jid = q.submit("solve", {}, {"ip": "a", "member": "m1"})[0]["id"]
        self.assertIsNone(q.get(jid, {"ip": "a"}))
        self.assertIsNone(q.get(jid, {"ip": "a", "member": "m2"}))
        self.assertIsNone(q.get(jid, {"ip": "a", "admin": True}))
        self.assertIsNotNone(q.get(jid, {"ip": "zzz", "member": "m1"}))

    def test_queue_full(self):
        gate = threading.Event()
        q = jobs.JobQueue(None, lambda *a: gate.wait(5) and {}, max_queue=2)
        q.submit("solve", {}, {"ip": "a"})
        time.sleep(0.1)
        q.submit("solve", {}, {"ip": "b"})
        q.submit("solve", {}, {"ip": "c"})
        with self.assertRaises(jobs.QueueFull):
            q.submit("solve", {}, {"ip": "d"})
        gate.set()


class BenchScoring(unittest.TestCase):
    def test_extract(self):
        for text, want in (("FINAL: 5", 5.0), ("FINAL: 61/72", 61 / 72), ("final = -3.5e2", -350.0), ("FINAL: 76,127", 76127.0),
                           ("FINAL: 1\nFINAL: 2", 2.0), ("FINAL: **19.81**", 19.81)):
            with self.subTest(text=text):
                self.assertAlmostEqual(bench.extract(text), want)
        for text in ("no final line", "", None, "FINAL: x", "FINAL: 1/0"):
            self.assertIsNone(bench.extract(text))

    def test_scoring_with_fake_solver(self):
        answers = {"17/24": "61/72", "gallons": "208.2", "cliff": "19.8", "60 W": "5.0", "Zero-sum": "1", "prime": "76127"}

        def solve(q):
            k = next(k for k in answers if k in q)
            return {"answer": f"FINAL: {answers[k]}", "tokens": 100, "gen_seconds": 20.0}
        r = bench.run(solve)
        self.assertEqual((r["score"], r["total"], r["tps"]), (6, 6, 5.0))
        answers["prime"] = "76128"
        answers["gallons"] = "210"
        self.assertEqual(bench.run(solve)["score"], 4)

        def broken(q):
            raise RuntimeError("model crashed")
        r = bench.run(broken)
        self.assertEqual((r["score"], r["tps"]), (0, 0.0))
        ev = threading.Event()
        ev.set()
        self.assertEqual(len(bench.run(solve, cancel=ev)["details"]), 0)


if __name__ == "__main__":
    unittest.main()
