"""
Adversarial tests for software/meshbridge/meshbridge.py: channel routing and trust, command parsing,
cooldowns, the !ai worker lock, byte limits (property tests), hostile alert/node data, and the real
main() loop driven with a fake Meshtastic interface (outbox → channel mapping, on_text routing, replies).

Failing tests are intentional: each one marked `# BUG:` documents a defect (expected vs actual).
Run: python3.11 tests/test_adv_mesh.py
"""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "meshbridge"))
os.environ["PORTAL_QUIET"] = "1"
import meshbridge as mb  # noqa: E402

FAM, PUB = 1, 0
ANA = {"member": "m_ana", "name": "Ana", "removed": False}
GONE = {"member": "m_cal", "name": "Cal", "removed": True}


def rand_text(rnd: random.Random, n: int) -> str:
    pools = [(0x20, 0x7e), (0xa0, 0x2ff), (0x370, 0x3ff), (0x590, 0x6ff), (0x900, 0x97f), (0x3040, 0x30ff),
             (0x4e00, 0x4fff), (0x1f300, 0x1f6ff), (0x200b, 0x200f), (0x2028, 0x202e), (0x300, 0x36f),
             (0x1f1e6, 0x1f1ff), (0x10000, 0x1007f)]
    out = []
    for _ in range(n):
        if rnd.random() < 0.15:
            out.append(rnd.choice([" ", "\t", "\n", "　", " ", " ", "\r", "\x0b", "\x1c"]))
        else:
            a, b = rnd.choice(pools)
            out.append(chr(rnd.randint(a, b)))
    return "".join(out)


# ───────────────────────────── routing and trust ─────────────────────────────

class Routing(unittest.TestCase):
    def test_spoofed_from_id_on_the_public_channel_is_not_family(self):
        # BUG (design): route_text() trusts a packet for family-only commands whenever its fromId is a
        # registered member radio — on ANY channel, including the public one (and DMs). Meshtastic's
        # `from` field is not authenticated on channel traffic: anyone on the public channel (default,
        # well-known key) can transmit with from = Ana's node number. They then get !ai / !status (battery,
        # power mode, how many people are on the Wi-Fi) and — see OnText below — their community posts are
        # stored as verified family posts under Ana's name. Only the family channel's PSK actually proves
        # anything. Expected: ("community", False). Actual: ("community", True).
        # Source: software/meshbridge/meshbridge.py route_text (`trusted = ch == family_ch or bool(reg)`).
        self.assertEqual(mb.route_text("hi", PUB, FAM, PUB, ANA), ("community", False))
        self.assertEqual(mb.route_text("hi", 5, FAM, PUB, ANA), ("ignore", False))

    def test_disabled_family_channel_never_matches(self):
        # BUG (defensive): with the family channel disabled (mesh_family_channel = -1) route_text still
        # compares `ch == family_ch`, so a packet whose channel field is -1 is filed on the FAMILY board
        # and trusted. Expected: ("ignore", False). Actual: ("family", True).
        # Source: software/meshbridge/meshbridge.py route_text (no family_ch >= 0 guard).
        self.assertEqual(mb.route_text("hi", -1, -1, 0, None), ("ignore", False))

    def test_fuzz_route_text(self):
        rnd = random.Random(7)
        for _ in range(5000):
            fam, pub = rnd.randint(0, 7), rnd.randint(-1, 7)
            if pub == fam:
                pub = -1                                       # what main() does
            ch = rnd.choice([rnd.randint(-5, 300), fam, pub, 0])
            reg = rnd.choice([None, ANA, GONE, {}])
            board, trusted = mb.route_text("x", ch, fam, pub, reg)
            self.assertIn(board, ("family", "community", "ignore"))
            if reg and reg.get("removed"):
                self.assertEqual((board, trusted), ("ignore", False))
            if board == "family":
                self.assertEqual(ch, fam)
            if board == "community":
                self.assertEqual(ch, pub)


# ───────────────────────────── commands ─────────────────────────────

class CommandParsing(unittest.TestCase):
    def make(self, **kw):
        self.said, self.t, self.jobs = [], [1000.0], []
        kw.setdefault("spawn", self.jobs.append)
        return mb.Commands(lambda text, dest: self.said.append((dest, text)), clock=lambda: self.t[0],
                           library=lambda q: f"LIB:{q}", ai=lambda q: f"AI:{q}", status=lambda: "STATUS", **kw)

    def test_spelling_edge_cases(self):
        c = self.make(cooldown=0)
        cases = {"!AI water": True, "!Ai water": True, "   !help   ": True, "!help me please": True,
                 "!ai​ water": False, "! ai water": False, "!aix water": False, "!a i": False,
                 "!!ai water": False, "!ai\twater": False, "¡ai water": False, "！ai water": False,
                 "!q": True, "!": False, "": False, "!" * 500: False, "!status‮": False}
        for text, is_cmd in cases.items():
            with self.subTest(text=repr(text)[:30]):
                self.assertEqual(c.handle(text, "!a1b2c3d4", trusted=True), is_cmd)
        c.handle("!q    many   spaces  ", "!s", trusted=True)
        self.assertEqual(self.said[-1][1], "LIB:many   spaces")

    def test_replies_only_to_the_asker(self):
        c = self.make(cooldown=0)
        for i, cmd in enumerate(["!help", "!q x", "!status", "!ai y", "!bogus"]):
            c.handle(cmd, f"!0000000{i}", trusted=True)
        for job in self.jobs:
            job()
        self.assertTrue(all(dest.startswith("!0000000") for dest, _ in self.said))
        self.assertTrue(all(dest == f"!0000000{i}" for i, (dest, _) in enumerate(self.said[:3])))

    def test_cooldown_cannot_be_dodged_by_respelling_the_sender(self):
        # BUG (low): the per-sender cooldown is keyed on the raw fromId string while the member registry
        # is keyed on fromId.lower(); "!A1B2C3D4" and "!a1b2c3d4" are the same radio but get separate
        # cooldowns (and separate !ai cooldowns). Expected: second command ignored. Actual: answered.
        # Note: since `from` itself is unauthenticated, a hostile radio can always rotate node numbers; the
        # cooldown is only a courtesy limit. Source: meshbridge.py Commands.handle (self.last[sender]).
        c = self.make()
        c.handle("!status", "!a1b2c3d4", trusted=True)
        c.handle("!status", "!A1B2C3D4", trusted=True)
        c.handle("!status", " !a1b2c3d4", trusted=True)
        self.assertEqual(len(self.said), 1, self.said)

    def test_ai_lock_released_when_the_worker_cannot_start(self):
        # BUG (low): Commands.handle acquires ai_busy, then calls say("Thinking…") and self.spawn(work)
        # outside any try/finally. If spawning fails (RuntimeError "can't start new thread" on a loaded
        # Pi) the lock is never released and every later !ai, from anyone, answers "busy" until restart.
        # Expected: lock released. Actual: held forever. Source: meshbridge.py Commands.handle (!ai branch).
        def boom(fn):
            raise RuntimeError("can't start new thread")
        c = self.make(spawn=boom)
        with contextlib.suppress(RuntimeError):
            c.handle("!ai first", "!a", trusted=True)
        self.assertTrue(c.ai_busy.acquire(blocking=False), "ai_busy leaked")

    def test_ai_lock_released_when_the_thinking_reply_fails(self):
        # BUG (low): same window — if say() raises (a reply callback that raises instead of returning False)
        # after ai_busy.acquire(), the lock leaks. Expected: released. Actual: held.
        # Source: meshbridge.py Commands.handle (!ai branch).
        def flaky(text, dest):
            raise OSError("serial write failed")
        c = mb.Commands(flaky, clock=lambda: 0.0, ai=lambda q: "x", spawn=lambda fn: fn())
        with contextlib.suppress(OSError):
            c.handle("!ai q", "!a", trusted=True)
        self.assertTrue(c.ai_busy.acquire(blocking=False), "ai_busy leaked")

    def test_ai_worker_exceptions_and_concurrency(self):
        calls = []

        def ai(q):
            calls.append(q)
            if q == "boom":
                raise KeyError("x")
            if q == "exit":
                raise SystemExit
            return "ok"
        said = []
        c = mb.Commands(lambda t, d: said.append(t), clock=lambda: time.time(), ai=ai, cooldown=0, ai_cooldown=0,
                        spawn=lambda fn: threading.Thread(target=fn).start())
        for q in ("boom", "fine", "exit", "fine2"):
            c.handle(f"!ai {q}", f"!{q}", trusted=True)
            deadline = time.time() + 2
            while c.ai_busy.locked() and time.time() < deadline:
                time.sleep(0.01)
            self.assertFalse(c.ai_busy.locked(), q)
        # 30 radios at once: exactly one gets the worker
        said.clear()
        gate = threading.Event()
        c2 = mb.Commands(lambda t, d: said.append((d, t)), clock=lambda: 0.0, ai=lambda q: gate.wait(2) and "A",
                         cooldown=0, ai_cooldown=0, spawn=lambda fn: threading.Thread(target=fn).start())
        ts = [threading.Thread(target=c2.handle, args=(f"!ai q{i}", f"!r{i}", True)) for i in range(30)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum("Thinking" in t for _, t in said), 1)
        self.assertEqual(sum("busy" in t for _, t in said), 29)
        gate.set()

    def test_level_changes_apply_to_the_next_command_only(self):
        levels = dict(mb.DEFAULT_ACCESS)
        said, jobs = [], []
        c = mb.Commands(lambda t, d: None, clock=lambda: 0.0, ai=lambda q: "ANSWER", cooldown=0, spawn=jobs.append,
                        access=lambda: levels)
        c.handle("!ai q", "!s", trusted=True, reply=said.append)
        levels["!ai"] = "off"                                   # admin turns it off while the worker runs
        jobs[0]()
        self.assertEqual(said[-1], "ANSWER")
        c.handle("!ai q2", "!t", trusted=True, reply=said.append)
        self.assertIn("turned off", said[-1])

    def test_access_function_garbage(self):
        # BUG (low, defence in depth): Commands.handle only refuses when the level is exactly "off" or
        # ("family" and untrusted); any other value — None, "everyone", a typo — falls through and the
        # command runs for anyone, i.e. unknown levels fail OPEN. main() always passes the access map
        # through command_access(), which sanitises it, so this is not reachable from the radio today;
        # a future caller that hands Commands a raw settings dict would open !ai/!status to the public.
        # Expected: unknown level treated as "off". Actual: treated as "community".
        # Source: software/meshbridge/meshbridge.py Commands.handle (level checks).
        for access in ({}, {"!ai": None}, {"!ai": "everyone"}, {"!help": "off"}):
            said = []
            c = mb.Commands(lambda t, d: None, clock=lambda: 0.0, cooldown=0, access=lambda a=access: a,
                            library=lambda q: "LIB", ai=lambda q: "AI", status=lambda: "S", spawn=lambda f: f())
            for cmd in ("!ai x", "!status", "!q x"):
                c.handle(cmd, "!x", trusted=False, reply=said.append)
            self.assertTrue(all(s in ("LIB",) or "turned off" in s or "family radios" in s for s in said), said)


class AccessAndHelp(unittest.TestCase):
    def test_help_text_fits_and_lists_only_usable_commands_for_every_combination(self):
        for lv in itertools.product(mb.LEVELS, repeat=4):
            access = dict(zip(("!help", "!q", "!ai", "!status"), lv, strict=True))
            for trusted in (False, True):
                s = mb.help_text(access, trusted)
                self.assertLessEqual(len(s.encode()), mb.MAX_BYTES, (access, trusted))
                for w in ("!q", "!ai", "!status"):
                    usable = access[w] == "community" or (trusted and access[w] == "family")
                    self.assertEqual(f"{w} " in s, usable, (access, trusted, s))

    def test_command_access_fuzz(self):
        rnd = random.Random(3)
        junk = ["community", "family", "off", "Community", "OFF", None, 1, [], {}, "on", "", "!ai", True]
        keys = ["help", "q", "ai", "status", "!ai", "AI", "!!q", "x", "", 5, None, "ai ", "аi"]
        with contextlib.redirect_stderr(io.StringIO()):
            for _ in range(2000):
                cfg = {"mesh_commands": {rnd.choice(keys): rnd.choice(junk) for _ in range(rnd.randint(0, 5))}}
                ov = {rnd.choice(keys): rnd.choice(junk) for _ in range(rnd.randint(0, 5))}
                acc = mb.command_access(cfg, rnd.choice([ov, None, [], "x"]))
                self.assertEqual(set(acc), set(mb.DEFAULT_ACCESS))
                self.assertTrue(set(acc.values()) <= set(mb.LEVELS))
                self.assertNotEqual(acc["!help"], "off")


# ───────────────────────────── byte limits ─────────────────────────────

class ByteLimits(unittest.TestCase):
    def test_fit_property_random_unicode(self):
        rnd = random.Random(42)
        for i in range(3000):
            text = rand_text(rnd, rnd.randint(0, 400))
            limit = rnd.choice([mb.MAX_BYTES, rnd.randint(4, 260)])
            out = mb.fit(text, limit)
            b = out.encode("utf-8")                          # never a split character / surrogate
            self.assertLessEqual(len(b), limit, (i, limit))
            self.assertNotIn("  ", out)
            if len(" ".join(text.split()).encode()) <= limit:
                self.assertEqual(out, " ".join(text.split()))

    def test_fit_non_strings(self):
        for v in (None, 123, 1.5, ["a"], {"a": 1}, b"bytes"):
            self.assertLessEqual(len(mb.fit(v).encode()), 200)

    def test_format_alert_hostile_fields(self):
        for a in ({}, {"event": None, "areas": None, "expires": None}, {"event": "🌪" * 500, "areas": "x" * 9999},
                  {"event": {"a": 1}, "areas": ["x"] * 100, "expires": 1e300}, {"event": "\n" * 50 + "Tornado"}):
            s = mb.format_alert(a)
            self.assertLessEqual(len(s.encode()), 200)
            self.assertNotIn("\n", s)

    def test_ai_answer_tail_with_long_multibyte_source(self):
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = (f"event: sources\ndata: {json.dumps([{'title': '🦡' * 60}])}\n\n"
                        f"event: token\ndata: {json.dumps('word ' * 200)}\n\nevent: done\ndata: {{}}\n\n").encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = mb.API
        try:
            mb.API = f"http://127.0.0.1:{srv.server_address[1]}"
            out = mb.ai_answer("q")
            self.assertLessEqual(len(out.encode()), 200)
            self.assertLessEqual(len(mb.fit(out).encode()), 200)
        finally:
            mb.API = old
            srv.shutdown(); srv.server_close()


# ───────────────────────────── hostile data files ─────────────────────────────

class HostileData(unittest.TestCase):
    def test_alerts_to_send_survives_hostile_alert_files(self):
        # BUG (low): alerts_to_send() assumes `expires_ts` is a number and `id` is hashable. An alerts file
        # with {"expires_ts": "soon"} or {"id": ["x"]} raises TypeError, which is not caught anywhere in
        # main()'s loop: the bridge crash-loops (systemd restarts it every 15 s) and while the file stays
        # like that no board post, rotation request or heartbeat is processed. Expected: bad entries
        # skipped. Actual: TypeError. Source: meshbridge.py alerts_to_send (and main loop, no try).
        now = 1_800_000_000
        hostile = [{"id": "A", "expires_ts": "soon"}, {"id": ["x"]}, {"id": {"k": 1}}, {"id": "B", "expires_ts": None},
                   {"id": "C", "expires_ts": float("nan")}, {"id": "D", "test": "no"}, {"id": 5},
                   {"id": "E", "expires_ts": now + 60}]
        problems = []
        for a in hostile:
            try:
                mb.alerts_to_send([a], {}, now)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{a!r}: {type(e).__name__}")
        try:
            mb.alerts_to_send([], {"x": "later", "y": None}, now)
        except Exception as e:  # noqa: BLE001
            problems.append(f"sent-file garbage: {type(e).__name__}")
        self.assertEqual(problems, [])
        self.assertEqual([a["id"] for a in mb.alerts_to_send([hostile[-1]], {}, now)], ["E"])

    def test_nodes_snapshot_drops_bad_numbers(self):
        now = 1_800_000_000
        nodes = {f"!n{i}": {"user": {"id": f"!n{i}"}, "position": {"latitude": lat, "longitude": lon, "time": now}}
                 for i, (lat, lon) in enumerate([(float("nan"), 1), (1, float("inf")), ("45", "7"), (True, 1),
                                                 (91, 0), (0, 181), (-90.0000001, 0), (None, None), ([1], [2])])}
        nodes["!ok"] = {"user": {"id": "!ok", "longName": "<b>x</b>" * 20}, "position": {"latitude": 45.5, "longitude": 7.1}}
        snap = mb.nodes_snapshot(nodes, None, now)
        self.assertEqual([n["id"] for n in snap], ["!ok"])
        self.assertLessEqual(len(snap[0]["name"]), 40)
        json.dumps(snap, allow_nan=False)                     # strictly valid JSON for the portal

    def test_nodes_snapshot_survives_hostile_structures(self):
        # BUG (low): nodes_snapshot() calls .get() on n["position"], n["user"] and n["deviceMetrics"]
        # without checking they are dicts, and math.isfinite() on ints, which raises OverflowError for
        # huge ones. Any of these crashes main()'s loop (no try) → restart loop, no outbox/heartbeat.
        # Expected: the bad node skipped. Actual: AttributeError / OverflowError.
        # Source: meshbridge.py nodes_snapshot / _num.
        now = 1_800_000_000
        hostile = {"!a": {"position": "here"}, "!b": {"position": [1, 2]},
                   "!c": {"position": {"latitude": 1, "longitude": 1}, "user": "Ana"},
                   "!d": {"position": {"latitude": 1, "longitude": 1}, "deviceMetrics": [80]},
                   "!e": {"position": {"latitudeI": 10 ** 400, "longitudeI": 1}},
                   "!f": {"position": {"latitude": 1, "longitude": 1}, "lastHeard": 10 ** 400}}
        problems = []
        for k, v in hostile.items():
            try:
                mb.nodes_snapshot({k: v}, None, now)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{k}: {type(e).__name__}")
        self.assertEqual(problems, [])

    def test_board_add_hostile_text_is_one_json_line(self):
        tmp = Path(tempfile.mkdtemp(prefix="adv-mesh-board-"))
        old = mb.BOARD, mb.FAMILY_BOARD
        try:
            mb.BOARD, mb.FAMILY_BOARD = tmp / "c.jsonl", tmp / "f.jsonl"
            mb.board_add("N\n{\"verified\":true}", "t\r\n{\"x\":1} ", "community", "!a\n", None)
            mb.board_add("x" * 99, "🦡" * 5000, "weird-board", "r" * 99, None)
            lines = (tmp / "c.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            recs = [json.loads(x) for x in lines]
            self.assertFalse(recs[0]["verified"])
            self.assertFalse((tmp / "f.jsonl").exists())
        finally:
            mb.BOARD, mb.FAMILY_BOARD = old
            shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────────── main() with a fake radio ─────────────────────────────

class _Stop(Exception):
    pass


class FakeChannel:
    def __init__(self, role):
        self.role = role
        self.settings = types.SimpleNamespace(psk=b"old")


class FakeLocalNode:
    def __init__(self):
        self.channels = [FakeChannel(1), FakeChannel(2), FakeChannel(2), FakeChannel(0)]
        self.nodeNum = 0x0badfeed                      # a configured node (the library sets it from myInfo)

    def writeChannel(self, i):
        pass

    def getURL(self, includeAll=True):
        return "https://meshtastic.org/e/#" + "".join(c.settings.psk.hex()[:8] for c in self.channels)


class FakeIface:
    def __init__(self, nodes=None):
        self.devPath = "/dev/ttyACM0"                  # a real radio on a serial port (meshbridge.radio_ready)
        self.nodes = nodes or {}
        self.sent = []
        self.data_sent = []
        self.localNode = FakeLocalNode()

    def sendText(self, text, destinationId=None, channelIndex=0):
        self.sent.append((text, destinationId, channelIndex))

    def sendData(self, data, destinationId="^all", portNum=256, wantAck=False, channelIndex=0):
        self.data_sent.append((bytes(data), portNum, channelIndex))     # mesh-courtesy control packets

    def getMyNodeInfo(self):
        return {"user": {"id": "!badger00"}}


class Bridge(unittest.TestCase):
    """Runs the real meshbridge.main() against FakeIface; time.sleep ends the loop."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adv-mesh-main-")).resolve()
        self.saved_globals = {k: getattr(mb, k) for k in ("BOARD", "FAMILY_BOARD", "MEMBERS", "OUTBOX", "NODES", "ALERTS",
                                                          "ROTATE_REQ", "ROTATE_RES", "CHANNEL_LINK", "BEAT", "API", "time")}
        self.saved_env = {k: os.environ.pop(k, None) for k in ("BOARD_FILE", "FAMILY_BOARD_FILE", "MEMBERS_FILE",
                                                               "MESH_OUTBOX", "MESH_NODES", "ALERTS_FILE",
                                                               "PORTAL_SETTINGS", "PREPPER_CONFIG", "MESH_TELEMETRY")}
        self.saved_mods = {k: sys.modules.get(k) for k in ("meshtastic", "meshtastic.serial_interface", "pubsub")}
        mb.BEAT = self.tmp / "mesh.ok"
        mb.API = "http://127.0.0.1:9"
        (self.tmp / "members.json").write_text(json.dumps({"v": 1, "invites": {}, "members": {
            "m_ana": {"name": "Ana", "devices": [], "radios": ["!a1b2c3d4"]},
            "m_cal": {"name": "Cal", "devices": [], "radios": [], "removed": 1, "removed_radios": ["!cccccccc"]}}}))
        # the family channel was re-keyed after Cal was removed (otherwise family-channel posts are only
        # "unconfirmed" and family commands are off: red team finding 11, tests/test_redteam_rf.py)
        (self.tmp / "mesh-rotate-result.json").write_text(json.dumps({"id": "r1", "ok": True, "at": 2}))

    def tearDown(self):
        for k, v in self.saved_globals.items():
            setattr(mb, k, v)
        for k, v in self.saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        for k, v in self.saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, radio: dict, outbox=(), loops=1, clock_step=None):
        t = self.tmp
        lines = [f"{k} = {json.dumps(v)}" for k, v in radio.items()]
        (t / "prepper.toml").write_text("[radio]\n" + "\n".join(lines) + f"""

[portal]
board_file = "{t}/board.jsonl"
family_board_file = "{t}/family-board.jsonl"
members_file = "{t}/members.json"
mesh_outbox = "{t}/mesh-outbox.jsonl"
mesh_nodes = "{t}/mesh-nodes.json"
alerts_file = "{t}/alerts.json"
settings_file = "{t}/portal-settings.json"
mesh_rotate_request = "{t}/mesh-rotate.json"
mesh_rotate_result = "{t}/mesh-rotate-result.json"
mesh_channel_link = "{t}/mesh-channel.json"
mesh_telemetry = "{t}/mesh-telemetry.jsonl"
mesh_bt_pin_file = "{t}/mesh-bt-pin.json"
""")
        os.environ["PREPPER_CONFIG"] = str(t / "prepper.toml")
        (t / "mesh-outbox.jsonl").write_text("".join(json.dumps(p) + "\n" for p in outbox))
        (t / "mesh-outbox.jsonl.offset").write_text("0")
        self.iface = FakeIface({"!stranger": {"user": {"longName": "Ana"}}})
        self.subs = {}
        mt = types.ModuleType("meshtastic")
        si = types.ModuleType("meshtastic.serial_interface")
        si.SerialInterface = lambda: self.iface
        mt.serial_interface = si
        ps = types.ModuleType("pubsub")
        ps.pub = types.SimpleNamespace(subscribe=lambda fn, topic: self.subs.__setitem__(topic, fn))
        sys.modules.update({"meshtastic": mt, "meshtastic.serial_interface": si, "pubsub": ps})
        n = [0]
        fake_now = [time.time()]

        def sleep(_s):
            n[0] += 1
            if clock_step is not None:
                fake_now[0] += clock_step                 # a fake clock: each loop is clock_step seconds
            if n[0] >= loops:
                raise _Stop
        now = time.time if clock_step is None else (lambda: fake_now[0])
        mb.time = types.SimpleNamespace(time=now, sleep=sleep, strftime=time.strftime, gmtime=time.gmtime)
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                mb.main()
            except _Stop:
                pass
        return self.iface.sent

    def rx(self, text, sender, ch, to=None):
        pkt = {"fromId": sender, "channel": ch, "decoded": {"text": text}}
        if to:
            pkt["toId"] = to
        with contextlib.redirect_stderr(io.StringIO()):
            self.subs["meshtastic.receive.text"](pkt, self.iface)

    def board(self, name):
        p = self.tmp / name
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    # — outbox → channel —
    def test_outbox_two_channels(self):
        sent = self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0},
                             [{"text": "F", "board": "family"}, {"text": "C", "board": "community"},
                              {"text": "D"}, {"text": "X", "board": "FAMILY"}, "garbage", {"no": "text"}])
        self.assertEqual([(t, ch) for t, _, ch in sent], [("F", 1), ("C", 0), ("D", 0), ("X", 0)])
        self.assertTrue(all(dest is None for _, dest, _ in sent))

    def test_outbox_legacy_single_channel(self):
        # CHANGED (review finding 4): an old config with only mesh_channel no longer makes that channel the
        # family's — nobody said it is private, and it was often the public channel 0. It is bridged as the
        # public channel; family features stay off until mesh_family_channel is set. So family-board posts
        # are NOT sent on it (they used to be: ("F", 2)), community posts are.
        sent = self.run_main({"mesh_channel": 2}, [{"text": "F", "board": "family"}, {"text": "C"}])
        self.assertEqual([(t, ch) for t, _, ch in sent], [("C", 2)])
        self.rx("hello", "!12345678", 2)
        self.assertEqual(self.board("family-board.jsonl"), [])
        self.assertEqual(self.board("board.jsonl")[-1]["text"], "hello")

    def test_public_channel_disabled_keeps_visitor_posts_off_the_family_channel(self):
        # BUG: with the two-board config and the public channel switched off (mesh_public_channel = -1),
        # main()'s outbox loop falls into the "older single-channel setup" branch and transmits COMMUNITY
        # board posts — written by anonymous Wi-Fi visitors — on the family's private, encrypted channel.
        # Expected: community posts dropped (no public channel to carry them). Actual: sent on channel 1.
        # Source: software/meshbridge/meshbridge.py main() outbox loop
        #         (`if public_ch < 0 and board != "family": ch = family_ch`).
        sent = self.run_main({"mesh_family_channel": 1, "mesh_public_channel": -1},
                             [{"text": "visitor: buy my stuff", "board": "community"}, {"text": "F", "board": "family"}])
        self.assertNotIn(("visitor: buy my stuff", 1), [(t, ch) for t, _, ch in sent])
        self.assertIn(("F", 1), [(t, ch) for t, _, ch in sent])

    def test_family_channel_disabled_does_not_wedge_the_outbox(self):
        # BUG: a family-board post marked "Send over mesh" while the family channel is disabled
        # (mesh_family_channel = -1) makes send() return False, which the outbox loop treats as "radio busy,
        # retry next round" — forever. Every later post (community ones included) is stuck behind it and
        # the offset never advances. Expected: the undeliverable post skipped, the community post sent on
        # channel 0. Actual: nothing sent, ever. Source: meshbridge.py main() outbox loop (break on
        # send() False) + send() (`if channel < 0: return False`).
        sent = self.run_main({"mesh_family_channel": -1, "mesh_public_channel": 0},
                             [{"text": "F", "board": "family"}, {"text": "C", "board": "community"}], loops=3)
        self.assertIn(("C", 0), [(t, ch) for t, _, ch in sent])

    def test_equal_channels_fail_closed(self):
        # BUG: when mesh_family_channel == mesh_public_channel main() keeps the channel as FAMILY and drops
        # the public one ("one channel can't be both private and public"). If the owner points both at the
        # default public channel 0 (a likely mistake), everyone on the public mesh lands on the family
        # board and is trusted for family-only commands. Expected: fail closed (treat it as public / refuse
        # to start). Actual: stranger's message → family board, !status answered.
        # Source: meshbridge.py main() (`if public_ch == family_ch: public_ch = -1`).
        self.run_main({"mesh_family_channel": 0, "mesh_public_channel": 0})
        self.rx("hello from a stranger", "!5eeeeee5", 0)
        self.assertEqual(self.board("family-board.jsonl"), [])
        self.iface.sent.clear()
        self.rx("!status", "!5eeeeee5", 0)
        # CHANGED (red team finding 6): not answered — and no "for family radios" refusal either
        self.assertEqual(self.iface.sent, [])

    # — on_text —
    def test_replies_go_to_the_sender_on_the_request_channel(self):
        # CHANGED (red team finding 6): a family radio (family channel) gets a DM on that channel; a stranger's
        # "!q" on the public channel is answered there in the open (as a reply to the question, so other Badgers
        # hear it); "!help" from strangers and anything on other channels gets nothing at all.
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        self.rx("!help", "!00000001", 1)
        self.assertEqual(len(self.iface.sent), 1)
        text, dest, chan = self.iface.sent[0]
        self.assertEqual((dest, chan), ("!00000001", 1))
        self.assertLessEqual(len(text.encode()), 200)
        self.assertNotIn("Badger", text)
        for ch in (0, 3, 7):
            self.iface.sent.clear()
            self.rx("!help", f"!0000000{ch}", ch)
            self.rx("!status", f"!0000001{ch}", ch)
            self.assertEqual(self.iface.sent, [], ch)
        self.rx("!q water", "!00000020", 0)                              # (library offline in the test)
        deadline = time.time() + 5                                       # answered on main()'s worker thread
        while not self.iface.sent and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual([(d, c) for _, d, c in self.iface.sent], [(None, 0)])
        self.iface.sent.clear()
        self.rx("!q water", "!00000021", 3)
        self.assertEqual(self.iface.sent, [])

    def test_spoofed_member_radio_on_public_channel_is_not_a_verified_family_post(self):
        # BUG: a packet on the PUBLIC channel with fromId = Ana's registered radio (trivial to forge: `from`
        # is not authenticated on channel traffic) is written to the community board as
        # {"name": "Ana", "verified": true, "member": "m_ana"} — members then see it with the ✓ family badge
        # — and it unlocks family-only commands. Expected: unverified, not trusted. Actual: verified/trusted.
        # Source: meshbridge.py main() on_text (board_add(..., reg["member"]) for any channel) and route_text.
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        self.rx("Ana here: the family should meet at the old mill", "!a1b2c3d4", 0)
        rec = self.board("board.jsonl")[-1]
        self.assertFalse(rec.get("verified"), rec)
        self.assertNotIn("member", rec)
        self.iface.sent.clear()
        self.rx("!status", "!a1b2c3d4", 0)
        # CHANGED (red team finding 6): not trusted, and strangers get no refusal at all (it identified a family)
        self.assertEqual(self.iface.sent, [])

    def test_direct_messages_are_commands_only(self):
        # BUG: route_text documents "another channel (or a DM): commands only", but on_text never looks at
        # toId. A DM to the Badger arrives with the channel index it was sent on (0 for PKI DMs / the
        # primary channel), so it is published on the community board for every Wi-Fi visitor to read —
        # or, with mesh_family_channel = 0, on the family board. Expected: a DM that isn't a command is not
        # posted to any board. Actual: posted. Source: meshbridge.py main() on_text (no toId check).
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        self.rx("private: my gate code is 4711", "!0d0d0d0d", 0, to="!badger00")
        self.assertEqual([r for r in self.board("board.jsonl") if "gate code" in r["text"]], [])

    def test_removed_members_radio_is_ignored_everywhere(self):
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        for ch in (0, 1, 4):
            self.rx("still here", "!CCCCCCCC", ch)
            self.rx("!help", "!cccccccc", ch)
        self.assertEqual(self.board("board.jsonl") + self.board("family-board.jsonl"), [])
        self.assertEqual(self.iface.sent, [])

    def test_hostile_packets_do_not_crash_or_leak_into_the_family_board(self):
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        cb = self.subs["meshtastic.receive.text"]
        rnd = random.Random(5)
        sent_on = {}
        chans = [None, "", "1", "x", -1, 2 ** 70, "999999999999999999999", 1.9, [], {}, True, "0x1", 0, 1, 2]
        senders = [None, "", "!a1b2c3d4", "!A1B2C3D4", 12345, "!" * 40, "?", "\n", "‮!a1b2c3d4"]
        with contextlib.redirect_stderr(io.StringIO()):
            for _i in range(400):
                pkt = {"fromId": rnd.choice(senders), "channel": rnd.choice(chans),
                       "decoded": {"text": rand_text(rnd, rnd.randint(0, 300))}}
                if rnd.random() < 0.2:
                    pkt.pop("channel")
                if rnd.random() < 0.1:
                    pkt.pop("fromId")
                try:
                    eff = int(pkt.get("channel") or 0)
                except (TypeError, ValueError):
                    eff = 0
                sent_on[" ".join(pkt["decoded"]["text"].split())] = eff
                cb(pkt, self.iface)
                cb({"decoded": {}}, self.iface)
                cb({}, self.iface)
        for rec in self.board("family-board.jsonl"):          # only what arrived on the family channel
            self.assertEqual(sent_on.get(" ".join(rec["text"].split())), 1, rec)
        self.assertTrue(all(len(t.encode()) <= 200 for t, _, _ in self.iface.sent))

    def test_non_dict_decoded_payload(self):
        # BUG (low): on_text does packet.get("decoded", {}).get("text", "").strip(): a packet whose
        # "decoded" or "text" is None (or not a str) raises inside the pubsub listener.
        # Expected: ignored. Actual: AttributeError. Source: meshbridge.py main() on_text.
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        cb = self.subs["meshtastic.receive.text"]
        problems = []
        for pkt in ({"decoded": None, "fromId": "!x"}, {"decoded": {"text": None}, "fromId": "!x"},
                    {"decoded": {"text": 5}, "fromId": "!x"}):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    cb(pkt, self.iface)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{pkt}: {type(e).__name__}")
        self.assertEqual(problems, [])

    def test_alerts_relayed_once_on_both_channels(self):
        # CHANGED (mesh courtesy): an alert that is already live when the bridge starts waits a random
        # 30–120 s quiet period (a town-wide restart must not storm the mesh), and even a Badger that has
        # heard no other Badger waits 0–5 s. Here the quiet period is switched off and a fake clock runs
        # 1 s per loop; it is still relayed exactly once per channel, now with its "#tag".
        (self.tmp / "alerts.json").write_text(json.dumps([
            {"id": "TOR", "event": "Tornado Warning", "areas": "here", "expires": "18:30", "expires_ts": time.time() + 600},
            {"id": "RWT", "event": "Test", "test": True, "expires_ts": time.time() + 600}]))
        sent = self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0, "mesh_boot_quiet_s": 0},
                             loops=12, clock_step=1.0)
        self.assertEqual(sorted(ch for t, _, ch in sent if "Tornado" in t), [0, 1])
        self.assertTrue(all(t.endswith(" #" + mb.tag_of("alert:TOR")) for t, _, _ in sent if "Tornado" in t))
        self.assertFalse(any("Test" in t for t, _, _ in sent))

    def test_alerts_live_at_boot_wait_for_the_quiet_period(self):
        (self.tmp / "alerts.json").write_text(json.dumps([
            {"id": "TOR", "event": "Tornado Warning", "areas": "here", "expires": "18:30", "expires_ts": time.time() + 900}]))
        sent = self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0}, loops=29, clock_step=1.0)
        self.assertEqual([t for t, _, _ in sent if "Tornado" in t], [])              # < 30 s after start: quiet
        sent = self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0}, loops=130, clock_step=1.0)
        self.assertEqual(sorted(ch for t, _, ch in sent if "Tornado" in t), [0, 1])   # by 120 s: relayed once


    # — mesh courtesy in main() (more in tests/test_mesh_courtesy.py) —
    def test_main_listens_for_badger_control_packets_and_writes_the_status_file(self):
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        self.assertIn("meshtastic.receive", self.subs)                   # PRIVATE_APP beacons / claims
        peers = self.tmp / "mesh-peers.json"
        self.assertEqual(oct(peers.stat().st_mode & 0o777), "0o640")
        doc = json.loads(peers.read_text())
        self.assertEqual((doc["enabled"], doc["silent"], doc["peers_active"]), (True, False, 0))
        with contextlib.redirect_stderr(io.StringIO()):
            self.subs["meshtastic.receive"]({"fromId": "!0000beef", "channel": 1, "decoded": {
                "portnum": "PRIVATE_APP", "payload": mb.encode_beacon()}}, self.iface)
            self.subs["meshtastic.receive"]({"fromId": "!0000beef", "decoded": {"portnum": "PRIVATE_APP",
                                                                                 "payload": b"\xff" * 70}}, self.iface)
            self.subs["meshtastic.receive"](None, self.iface)

    def test_main_radio_silence_flag_file(self):
        (self.tmp / "mesh-silent").write_text(json.dumps({"until": time.time() + 600, "by": "panel"}))
        (self.tmp / "alerts.json").write_text(json.dumps([
            {"id": "TOR", "event": "Tornado Warning", "areas": "here", "expires": "18:30", "expires_ts": time.time() + 900}]))
        sent = self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0, "mesh_boot_quiet_s": 0},
                             [{"text": "C", "board": "community"}, {"text": "Ana: F", "board": "family", "member": True},
                              {"text": "Supplies: F2", "board": "family"}], loops=12, clock_step=1.0)
        self.assertEqual([(t, ch) for t, _, ch in sent], [("Ana: F", 1)])   # only what a member typed for family
        self.rx("!help", "!12345678", 0)
        self.assertEqual(len(self.iface.sent), 1)                        # heard, not answered
        self.assertEqual(self.iface.data_sent, [])
        self.assertTrue(json.loads((self.tmp / "mesh-peers.json").read_text())["silent"])

    # — fixes: pacing, mesh-in limits, telemetry —
    def test_community_flood_is_paced_and_never_delays_family_posts(self):
        posts = [{"text": f"C{i}", "board": "community"} for i in range(40)] + [{"text": "F", "board": "family"}]
        sent = self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0}, posts)
        texts = [t for t, _, _ in sent]
        self.assertEqual(texts[0], "F")                                  # family first, despite 40 posts ahead of it
        self.assertLessEqual(sum(1 for t in texts if t.startswith("C")), 5)   # a burst of at most 5 on the public channel

    def test_mesh_in_posts_are_rate_limited_per_radio(self):
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        for i in range(10):
            self.rx(f"spam {i}", "!0000beef", 0)
        self.assertEqual(len(self.board("board.jsonl")), 1)
        self.rx("someone else", "!0000cafe", 0)
        self.assertEqual(len(self.board("board.jsonl")), 2)

    def test_community_posts_never_carry_a_member_name(self):
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        self.rx("hi all", "!a1b2c3d4", 0)                                # Ana's ID, but anyone can send it
        rec = self.board("board.jsonl")[-1]
        self.assertNotEqual(rec["name"], "Ana")                          # the node's own (claimed) name, or its ID
        self.rx("home safe", "!a1b2c3d4", 1)                             # the family channel's key vouches
        rec = self.board("family-board.jsonl")[-1]
        self.assertEqual((rec["name"], rec["member"], rec["verified"]), ("Ana", "m_ana", False))

    def telemetry(self, pkt):
        with contextlib.redirect_stderr(io.StringIO()):
            self.subs["meshtastic.receive.telemetry"](pkt, self.iface)
        p = self.tmp / "mesh-telemetry.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_telemetry_lines(self):
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        self.iface.nodes["!00c0ffee"] = {"user": {"longName": "Garden‮ sensor"}}
        recs = self.telemetry({"fromId": "!00C0FFEE", "channel": 1, "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {
            "time": 5, "environmentMetrics": {"temperature": 21.5, "relativeHumidity": 40, "barometricPressure": 1013.2,
                                              "bogus": "x", "nested": {"a": 1}, "flag": True, "nan": float("nan")}}}})
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["from"], r["name"], r["channel"], r["trusted"], r["kind"]),
                         ("!00c0ffee", "Garden sensor", 1, True, "environment"))
        self.assertEqual(r["values"], {"temperature": 21.5, "relativeHumidity": 40, "barometricPressure": 1013.2})
        self.assertIsInstance(r["at"], float)
        self.assertEqual(oct((self.tmp / "mesh-telemetry.jsonl").stat().st_mode & 0o777), "0o640")
        # the same radio again within 30 s: not logged; another radio on the public channel: untrusted
        self.assertEqual(len(self.telemetry({"fromId": "!00c0ffee", "channel": 1, "decoded": {"telemetry": {
            "deviceMetrics": {"batteryLevel": 80}}}})), 1)
        recs = self.telemetry({"fromId": "!0000aaaa", "decoded": {"telemetry": {"deviceMetrics": {"batteryLevel": 80, "voltage": 4.1}}}})
        self.assertEqual((recs[-1]["kind"], recs[-1]["trusted"], recs[-1]["channel"], recs[-1]["name"]), ("device", False, 0, None))
        recs = self.telemetry({"fromId": "!0000bbbb", "channel": 0, "decoded": {"telemetry": {"powerMetrics": {"ch1Voltage": 12.6}}}})
        self.assertEqual(recs[-1]["kind"], "power")
        recs = self.telemetry({"fromId": "!0000cccc", "channel": 0, "decoded": {"telemetry": {"airQualityMetrics": {"pm25Standard": 7}}}})
        self.assertEqual(recs[-1]["kind"], "air_quality")
        recs = self.telemetry({"fromId": "!0000dddd", "channel": 0, "decoded": {"telemetry": {"localStats": {"numPacketsTx": 7}}}})
        self.assertEqual(recs[-1]["kind"], "other")

    def test_hostile_telemetry_never_crashes_or_logs(self):
        self.run_main({"mesh_family_channel": 1, "mesh_public_channel": 0})
        before = len(self.telemetry({}))
        for pkt in (None, [], "x", {"fromId": None}, {"fromId": "!zz"}, {"fromId": "!0000eeee", "channel": "1",
                    "decoded": {"telemetry": {"deviceMetrics": {"batteryLevel": 1}}}},
                    {"fromId": "!0000eeee", "channel": 99, "decoded": {"telemetry": {"deviceMetrics": {"batteryLevel": 1}}}},
                    {"fromId": "!0000eeee", "decoded": None}, {"fromId": "!0000eeee", "decoded": {"telemetry": []}},
                    {"fromId": "!0000eeee", "decoded": {"telemetry": {"deviceMetrics": {"batteryLevel": 10 ** 400}}}},
                    {"fromId": "!0000eeee", "decoded": {"telemetry": {"deviceMetrics": {"x" * 99: 1}}}}):
            with self.subTest(pkt=repr(pkt)[:40]):
                self.assertEqual(len(self.telemetry(pkt)), before)

    def test_telemetry_file_stays_small(self):
        log = mb.TelemetryLog(self.tmp / "t.jsonl", 1, clock=iter(range(0, 10 ** 9, 100)).__next__, max_bytes=5000)
        for i in range(400):
            log.handle({"fromId": f"!{i:08x}", "channel": 0, "decoded": {"telemetry": {"deviceMetrics": {"voltage": 3.9}}}})
        size = (self.tmp / "t.jsonl").stat().st_size
        self.assertLessEqual(size, 5000)
        lines = (self.tmp / "t.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[-1])["from"], f"!{399:08x}")     # the newest kept
        for ln in lines:
            json.loads(ln)                                               # whole lines only


class BluetoothPin(unittest.TestCase):
    def iface(self, enabled=True, mode=1, pin=123456, fail=False):
        bt = types.SimpleNamespace(enabled=enabled, mode=mode, fixed_pin=pin)
        written = []

        def write(what):
            if fail:
                raise OSError("radio busy")
            written.append(what)
        node = types.SimpleNamespace(localConfig=types.SimpleNamespace(bluetooth=bt), writeConfig=write)
        return types.SimpleNamespace(localNode=node), bt, written

    def test_default_pin_replaced_and_saved_owner_only(self):
        tmp = Path(tempfile.mkdtemp(prefix="adv-bt-"))
        try:
            for mode, pin in ((1, 123456), (2, 0)):                      # the default PIN, or no PIN at all
                iface, bt, written = self.iface(mode=mode, pin=pin)
                with contextlib.redirect_stderr(io.StringIO()):
                    new = mb.secure_bt_pin(iface, tmp / "pin.json", rng=lambda n: 4242)
                self.assertEqual(new, "104242")
                self.assertEqual((bt.mode, bt.fixed_pin, written), (1, 104242, ["bluetooth"]))
                self.assertEqual(json.loads((tmp / "pin.json").read_text())["pin"], "104242")
                self.assertEqual((tmp / "pin.json").stat().st_mode & 0o777, 0o600)
            for kw in ({"enabled": False}, {"mode": 0}, {"mode": 1, "pin": 987654}, {"fail": True}):
                iface, bt, written = self.iface(**kw)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertIsNone(mb.secure_bt_pin(iface, tmp / "pin2.json"))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(mb.secure_bt_pin(types.SimpleNamespace(), tmp / "pin3.json"))   # no localNode
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StaleRequests(unittest.TestCase):
    def test_old_request_is_never_acted_on(self):
        tmp = Path(tempfile.mkdtemp(prefix="adv-stale-"))
        try:
            node = types.SimpleNamespace(channels=[FakeChannel(1), FakeChannel(2)], written=[])
            node.writeChannel = node.written.append
            node.getURL = lambda includeAll=True: "u"
            iface = types.SimpleNamespace(localNode=node)
            req, res = tmp / "req.json", tmp / "res.json"
            req.write_text(json.dumps({"id": "old1", "at": time.time() - 3 * 86400}))
            with contextlib.redirect_stderr(io.StringIO()):
                out = mb.handle_rotation(iface, 1, req, res)
            self.assertFalse(out["ok"])
            self.assertEqual(node.written, [])
            self.assertFalse(req.exists())
            req.write_text(json.dumps({"id": "old2"}))                  # no "at": the file's age decides
            os.utime(req, (time.time() - 600, time.time() - 600))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(mb.handle_rotation(iface, 1, req, res)["ok"])
            self.assertEqual(node.written, [])
            req.write_text(json.dumps({"id": "fresh", "at": time.time()}))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(mb.handle_rotation(iface, 1, req, res)["ok"])
            self.assertEqual(node.written, [1])
            req.write_text(json.dumps({"id": "zero", "at": time.time()}))   # channel 0 is never re-keyed
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(mb.handle_rotation(iface, 0, req, res)["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
