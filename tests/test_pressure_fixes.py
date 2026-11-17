"""
Fixes from the VM pressure test (tests/vm/vm_pressure.sh): request bodies refused or trickled over a
real socket, bounded worker threads and a deep accept queue, quiet client disconnects, single-flight
status caches and a non-blocking GPS probe, monotonic lockouts and rate limits under wall-clock jumps,
a friendly 507 on a full disk, private file modes, and plain aircraft wording.
Run: python3.11 tests/test_pressure_fixes.py
"""
from __future__ import annotations

import errno
import http.client
import io
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
for sub in ("software/portal", "software/portal/features", "software/common", "software/upd",
            "software/panel", "software/meshbridge", "software/sdr"):
    sys.path.insert(0, str(REPO / sub))
os.environ["PORTAL_QUIET"] = "1"

import server  # noqa: E402
import members  # noqa: E402
import upd  # noqa: E402

OPENSSL = "/usr/bin/openssl" if Path("/usr/bin/openssl").exists() else shutil.which("openssl")
HOUR, DAYS_400 = 3600.0, 400 * 86400.0

FEAT = textwrap.dedent('''
    import errno
    FEATURE = {"title": "P", "icon": "P", "group": "tools", "route": "/p", "script": "/features/p.js",
               "access": "public", "description": "pressure test"}
    def up(req, ctx, args):
        n = int(req.headers["Content-Length"])
        return {"bytes": len(req.rfile.read(n))}
    def early(req, ctx, args):
        return 409, {"error": "not now"}            # answers without reading the body
    def full(req, ctx, args):
        raise OSError(errno.ENOSPC, "No space left on device", "/var/lib/prepper/x")
    def write(req, ctx, args):
        ctx.atomic_write(ctx.data_dir() / "x.json", "{}")
        return {"ok": True}
    ROUTES = [
        {"method": "POST", "path": "/api/p/up", "handler": up, "access": "public", "raw": True, "max_bytes": 100000},
        {"method": "POST", "path": "/api/p/member-up", "handler": up, "access": "member", "raw": True, "max_bytes": 5000000},
        {"method": "POST", "path": "/api/p/early", "handler": early, "access": "public", "raw": True, "max_bytes": 5000000},
        {"method": "POST", "path": "/api/p/full", "handler": full, "access": "public"},
        {"method": "POST", "path": "/api/p/write", "handler": write, "access": "public"},
    ]
''')


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def portal_config(tmp: Path) -> Path:
    cfg = tmp / "prepper.toml"
    cfg.write_text(f"""
[upd]
data_root = "{tmp / 'data'}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{tmp}/upd-state.json"
report_dir = "{tmp}/reports"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
admin_pin = "{server.hash_pin('246810')}"
board_file = "{tmp}/board.jsonl"
family_board_file = "{tmp}/family-board.jsonl"
members_file = "{tmp}/members.json"
settings_file = "{tmp}/portal-settings.json"
mesh_outbox = "{tmp}/mesh-outbox.jsonl"
mesh_pending = "{tmp}/mesh-pending.json"
features_data = "{tmp}/features-data"
ctl = "/nonexistent/prepperctl"
services = {{}}
""")
    return cfg


class PortalCase(unittest.TestCase):
    max_threads = 128

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="press-")).resolve()
        (cls.tmp / "data").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        (feats / "p.py").write_text(FEAT)
        server.CFG = server.PortalConfig(portal_config(cls.tmp))
        server.reset_login_limits()
        server.load_features(feats)
        cls.srv = server.PortalServer(("127.0.0.1", 0), server.Handler, max_threads=cls.max_threads, slot_wait=0.3)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        server._last_post.clear()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if data.startswith(b"{") else data)

    def stream(self, path, length, send, ctype="application/json", extra=b"", chunk=65536):
        """A phone uploading: headers, then `send` bytes of the declared `length` in chunks (like nginx
        with proxy_request_buffering off), then read the reply. Returns (status line, JSON body)."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        try:
            s.sendall(f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: {ctype}\r\n"
                      f"Content-Length: {length}\r\n".encode() + extra + b"\r\n")
            left = send
            while left > 0:
                n = min(chunk, left)
                s.sendall(b"x" * n)                   # a reset here (the old behaviour) fails the test
                left -= n
            s.shutdown(socket.SHUT_WR) if send >= length else None
            data = b""
            while True:
                b = s.recv(65536)
                if not b:
                    break
                data += b
                if b"\r\n\r\n" in data:
                    head, _, rest = data.partition(b"\r\n\r\n")
                    cl = [ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length:")]
                    if cl and len(rest) >= int(cl[0].split(b":")[1]):
                        break
            head, _, rest = data.partition(b"\r\n\r\n")
            return head.split(b"\r\n")[0].decode(), json.loads(rest or b"{}")
        finally:
            s.close()


class RefusedBodies(PortalCase):
    """Finding 6: a refused body is drained (bounded) so the friendly answer arrives, not a reset/502."""

    def test_oversized_json_body_gets_a_clean_413(self):
        line, body = self.stream("/api/board", 5_000_000, 5_000_000)
        self.assertIn("413", line)
        self.assertIn("1 MB", body["error"])

    def test_oversized_raw_upload_gets_a_clean_413(self):
        line, body = self.stream("/api/p/up", 3_000_000, 3_000_000, ctype="application/octet-stream",
                                 extra=b"X-Badger-Upload: 1\r\n")
        self.assertIn("413", line)
        self.assertIn("too big", body["error"])
        self.assertIn("100 kB", body["error"])

    def test_refused_raw_upload_for_a_visitor_is_drained(self):
        line, body = self.stream("/api/p/member-up", 2_000_000, 2_000_000, ctype="application/octet-stream",
                                 extra=b"X-Badger-Upload: 1\r\n")
        self.assertIn("401", line)
        self.assertIn("family members", body["error"])

    def test_upload_without_the_csrf_header_is_drained(self):
        line, body = self.stream("/api/p/up", 2_000_000, 2_000_000, ctype="application/octet-stream")
        self.assertIn("415", line)

    def test_handler_that_answers_early_still_reaches_the_phone(self):
        line, body = self.stream("/api/p/early", 2_000_000, 2_000_000, ctype="application/octet-stream",
                                 extra=b"X-Badger-Upload: 1\r\n")
        self.assertIn("409", line)
        self.assertEqual(body["error"], "not now")

    def test_body_bigger_than_the_drain_cap_is_not_read(self):
        # declared 40 MB (nginx caps bodies at 26 MB anyway): answered at once, nothing waited for
        t0 = time.monotonic()
        line, body = self.stream("/api/board", 40_000_000, 0)
        self.assertIn("413", line)
        self.assertLess(time.monotonic() - t0, 5)

    def test_good_upload_and_keepalive_still_work(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        for _ in range(3):
            c.request("POST", "/api/p/up", b"y" * 50_000, {"Content-Type": "application/octet-stream",
                                                          "X-Badger-Upload": "1"})
            r = c.getresponse()
            self.assertEqual((r.status, json.loads(r.read())["bytes"]), (200, 50_000))
        c.close()


class SlowBodies(PortalCase):
    """Finding 7: one deadline for the whole body, however slowly it trickles."""

    def test_trickled_body_is_cut_at_the_deadline(self):
        with mock.patch.object(server, "BODY_DEADLINE", 1.5):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            s.sendall(b"POST /api/board HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Content-Length: 100\r\n\r\n")
            t0, data = time.monotonic(), b""
            try:
                for _ in range(10):                   # 1 byte every 0.4 s: never idle long enough for the socket timeout
                    s.sendall(b"{")
                    time.sleep(0.4)
            except OSError:
                pass
            try:
                while True:
                    b = s.recv(4096)
                    if not b:
                        break
                    data += b
            except OSError:
                pass
            s.close()
        self.assertTrue(data.startswith(b"HTTP/1.1 408"), data[:60])
        self.assertLess(time.monotonic() - t0, 8)
        self.assertEqual(self.req("GET", "/api/status")[0], 200)

    def test_deadline_grows_with_the_declared_size(self):
        r = server._BodyReader(io.BufferedReader(io.BytesIO(b"")), None, 25_000_000)
        self.assertGreater(r.deadline - time.monotonic(), server.BODY_DEADLINE + 200)


class Threads(PortalCase):
    """Findings 5 and 7: deep accept queue, bounded worker threads, a 503 when all are busy."""
    max_threads = 4

    def test_queue_and_cap(self):
        self.assertEqual(server.PortalServer.request_queue_size, 256)
        self.assertEqual(self.srv.max_threads, 4)
        hogs = []
        try:
            for _ in range(4):                        # slow clients hold every slot
                s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                s.sendall(b"GET /api/status HTTP/1.1\r\n")
                hogs.append(s)
            time.sleep(0.3)
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            s.sendall(b"GET /api/status HTTP/1.1\r\nHost: x\r\n\r\n")
            data = s.recv(4096)
            s.close()
            self.assertTrue(data.startswith(b"HTTP/1.1 503"), data[:60])
            self.assertIn(b"busy", data)
        finally:
            for h in hogs:
                h.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:            # slots come back once the slow clients leave
            try:
                if self.req("GET", "/api/status")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        self.assertEqual(self.req("GET", "/api/status")[0], 200)

    def test_client_disconnects_log_one_quiet_line(self):
        err = io.StringIO()
        self.srv._quiet_until = 0.0
        with redirect_stderr(err):
            for exc in (ConnectionResetError(errno.ECONNRESET, "reset"), BrokenPipeError(errno.EPIPE, "pipe"),
                        TimeoutError("timed out")):
                try:
                    raise exc
                except OSError:
                    self.srv.handle_error(None, ("10.0.0.9", 1))
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(err.getvalue().count("\n"), 1, err.getvalue())


class StorageFull(PortalCase):
    """Finding 13: ENOSPC → 507 with plain words, no OSError detail."""

    def test_feature_on_a_full_disk(self):
        err = io.StringIO()
        with redirect_stderr(err):
            st, j = self.req("POST", "/api/p/full", {})
        self.assertEqual(st, 507)
        self.assertIn("storage is full", j["error"])
        self.assertNotIn("Errno", json.dumps(j))
        self.assertNotIn("Traceback", err.getvalue())

    def test_board_post_on_a_full_disk(self):
        def boom(path):
            raise OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(server, "open_private_append", boom):
            st, j = self.req("POST", "/api/board", {"name": "Al", "text": "hello"})
        self.assertEqual(st, 507)
        self.assertFalse(j["ok"])
        self.assertIn("storage is full", j["message"])
        st, j = self.req("POST", "/api/board", {"name": "Al", "text": "hello again"})   # no 5 s wait imposed
        self.assertEqual((st, j["ok"]), (200, True))


class FileModes(PortalCase):
    """Finding 14: boards, the mesh outbox and feature files are 0640, never world-readable."""

    def mode(self, p: Path) -> int:
        return stat.S_IMODE(p.stat().st_mode)

    def test_modes(self):
        old = os.umask(0o022)
        try:
            board = self.tmp / "board.jsonl"
            board.write_text("")
            os.chmod(board, 0o644)                    # an older install's file is tightened
            ok, _ = server.board_post("10.1.1.1", "Al", "hi", False)
            self.assertTrue(ok)
            self.assertEqual(self.mode(board), 0o640)
            ok, _ = server.board_post("10.1.1.2", "", "fam", False, "family",
                                      {"id": "feature-x", "name": "Badger", "system": True})
            self.assertTrue(ok)
            self.assertEqual(self.mode(self.tmp / "family-board.jsonl"), 0o640)
            with server._board_lock:
                server.outbox_append({"text": "x", "board": "community"})
            self.assertEqual(self.mode(self.tmp / "mesh-outbox.jsonl"), 0o640)
            st, _ = self.req("POST", "/api/p/write", {})
            self.assertEqual(st, 200)
            self.assertEqual(self.mode(self.tmp / "features-data" / "p" / "x.json"), 0o640)
            p = self.tmp / "trim.jsonl"
            p.write_text("x" * 100 + "\n" + "y" * 100 + "\n")
            server.trim_jsonl(p, 50)
            self.assertEqual(self.mode(p), 0o640)
        finally:
            os.umask(old)

    def test_meshbridge_board_add_is_private(self):
        import meshbridge
        old = os.umask(0o022)
        try:
            with mock.patch.object(meshbridge, "BOARD", self.tmp / "mb-board.jsonl"):
                meshbridge.board_add("radio", "hello")
            self.assertEqual(self.mode(self.tmp / "mb-board.jsonl"), 0o640)
        finally:
            os.umask(old)


class Caches(unittest.TestCase):
    """Finding 12: single-flight refreshes, and a GPS probe that never holds up /api/status."""

    def setUp(self):
        server._cache.clear()

    def test_single_flight(self):
        calls = []

        def slow():
            calls.append(1)
            time.sleep(0.3)
            return len(calls)
        out = []
        ts = [threading.Thread(target=lambda: out.append(server.cached("sf", 10, slow))) for _ in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(out, [1] * 12)

    def test_stale_value_served_while_refreshing(self):
        server._cache["st"] = (time.monotonic() - 100, "old")
        gate = threading.Event()

        def slow():
            gate.wait(2)
            return "new"
        t0 = time.monotonic()
        self.assertEqual(server.cached_background("st", 10, slow), "old")
        self.assertLess(time.monotonic() - t0, 0.2)
        gate.set()
        deadline = time.monotonic() + 3
        while server._cache["st"][1] != "new" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(server.cached_background("st", 10, slow), "new")

    def test_gps_probe_does_not_block(self):
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(4)
        held = []
        threading.Thread(target=lambda: held.append(lsock.accept()), daemon=True).start()
        try:
            with mock.patch.object(server, "GPSD_ADDR", lsock.getsockname()):   # gpsd that never answers
                t0 = time.monotonic()
                self.assertIsNone(server.gps_fix())
                self.assertLess(time.monotonic() - t0, 0.8)
                t0 = time.monotonic()
                for _ in range(5):
                    server.gps_fix()
                self.assertLess(time.monotonic() - t0, 0.8)
        finally:
            lsock.close()
            for conn, _ in held:
                conn.close()


class ClockJumps(unittest.TestCase):
    """Finding 9: lockouts, rate limits and idle timers ignore the wall clock jumping back 1 h or
    forward 400 days; stored dates still use it."""

    def test_portal_pin_lockout(self):
        server.reset_login_limits()
        mono = Clock(1000.0)
        with mock.patch.object(server, "_now", mono):
            for _ in range(3):
                server.login_failed("10.2.2.2")
            w = server.login_wait("10.2.2.2")
            self.assertTrue(0 < w <= 1.0)
            for jump in (-HOUR, DAYS_400):
                with mock.patch("time.time", return_value=time.time() + jump):
                    self.assertAlmostEqual(server.login_wait("10.2.2.2"), w, delta=0.01)
            mono.t += 2
            self.assertEqual(server.login_wait("10.2.2.2"), 0.0)
        server.reset_login_limits()

    def test_member_join_lockout(self):
        tmp = Path(tempfile.mkdtemp(prefix="mj-"))
        try:
            wall, mono = Clock(1_800_000_000.0), Clock(500.0)
            db = members.Members(tmp / "m.json", clock=wall, mono=mono)
            for _ in range(members.JOIN_FAILS):
                db.join("AAAA-BBBB-CC", "x", "d", "10.3.3.3")
            self.assertAlmostEqual(db.join_wait("10.3.3.3"), members.JOIN_LOCK, delta=1)
            wall.t -= HOUR
            self.assertAlmostEqual(db.join_wait("10.3.3.3"), members.JOIN_LOCK, delta=1)
            wall.t += HOUR + DAYS_400
            self.assertAlmostEqual(db.join_wait("10.3.3.3"), members.JOIN_LOCK, delta=1)
            mono.t += members.JOIN_LOCK + 1
            self.assertEqual(db.join_wait("10.3.3.3"), 0.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless(OPENSSL, "needs the openssl program")
    def test_vault_idle_timer(self):
        import vault
        tmp = Path(tempfile.mkdtemp(prefix="vj-"))
        try:
            wall, mono = Clock(1_800_000_000.0), Clock(100.0)
            v = vault.Vault(tmp, {"scrypt_n": 1024, "openssl": OPENSSL, "idle_minutes": 15}, clock=wall, mono=mono)
            ana = {"id": "m_ana", "device": "d1", "name": "Ana"}
            v.setup(ana, "correct horse battery", "10.0.0.2")
            v.unlock(ana, "correct horse battery", "10.0.0.2")
            self.assertTrue(v.status(ana, "10.0.0.2")["unlocked"])
            wall.t -= HOUR
            self.assertLessEqual(v.status(ana, "10.0.0.2")["idle_left"], 900)
            wall.t += HOUR + DAYS_400
            self.assertTrue(v.status(ana, "10.0.0.2")["unlocked"])     # a forward jump doesn't lock it either
            mono.t += 16 * 60
            self.assertFalse(v.status(ana, "10.0.0.2")["unlocked"])
            meta = json.loads((tmp / "vault.json").read_text())
            self.assertEqual(meta["created"], 1_800_000_000)           # stored dates: wall clock
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_feature_rate_limits(self):
        import _community_common as C
        rl = C.RateLimit(2, 600)
        self.assertEqual(rl.check("ip"), 0.0)
        self.assertEqual(rl.check("ip"), 0.0)
        for jump in (-HOUR, DAYS_400):
            with mock.patch("time.time", return_value=time.time() + jump):
                self.assertGreater(rl.check("ip"), 0)
        import skills
        import missing
        import aidstation
        for mod in (skills, missing, aidstation):                   # the features' limiters: monotonic too
            lim = getattr(mod, "_add_limit", None) or mod._write_limit
            with mock.patch("time.time", return_value=time.time() + DAYS_400):
                self.assertAlmostEqual(lim.clock(), time.monotonic(), delta=1)

    def test_board_rate_limit(self):
        server._last_post.clear()
        self.assertEqual(server._last_post.get("x"), None)
        tmp = Path(tempfile.mkdtemp(prefix="br-"))
        try:
            server.CFG = server.PortalConfig(portal_config(tmp))
            self.assertTrue(server.board_post("10.4.4.4", "A", "one", False)[0])
            with mock.patch("time.time", return_value=time.time() - HOUR):
                self.assertFalse(server.board_post("10.4.4.4", "A", "two", False)[0])   # still within 5 s
                server._last_post["10.4.4.4"] -= 6
                self.assertTrue(server.board_post("10.4.4.4", "A", "three", False)[0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_panel_lock_backoff(self):
        import panel
        tmp = Path(tempfile.mkdtemp(prefix="pl-"))
        try:
            boot = tmp / "boot_id"
            boot.write_text("boot-1\n")
            wall, mono = Clock(1_800_000_000.0), Clock(50.0)
            with mock.patch.object(panel, "BOOT_ID", boot):
                lk = panel.PanelLock(tmp / "lock.json", clock=wall, mono=mono)
                lk.set("2468")
                for _ in range(panel.LOCK_FREE_TRIES):
                    lk.check("1111")
                w = lk.wait()
                self.assertAlmostEqual(w, panel.LOCK_BASE_WAIT, delta=0.01)
                wall.t -= HOUR
                self.assertAlmostEqual(lk.wait(), w, delta=0.01)        # not 3630 s
                wall.t += HOUR + DAYS_400
                self.assertAlmostEqual(lk.wait(), w, delta=0.01)        # not ended either
                mono.t += 10
                self.assertAlmostEqual(lk.wait(), w - 10, delta=0.01)
                # after a reboot (new boot id) the wall-clock deadline counts, capped at the delay
                boot.write_text("boot-2\n")
                wall.t = 1_800_000_000.0 - HOUR
                self.assertAlmostEqual(lk.wait(), panel.LOCK_BASE_WAIT, delta=0.01)
                wall.t = 1_800_000_000.0 + panel.LOCK_BASE_WAIT + 1
                self.assertEqual(lk.wait(), 0.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Updater(unittest.TestCase):
    """Finding 13: a full disk makes upd.py print one clear line and exit non-zero, no traceback."""

    def test_full_disk(self):
        def boom(cfg, args):
            raise OSError(errno.ENOSPC, "No space left on device", "/var/lib/prepper/reports/last-check.json")
        err = io.StringIO()
        with mock.patch.object(upd.Config, "load", staticmethod(lambda p: object())), \
                mock.patch.object(upd, "cmd_status", boom), redirect_stderr(err):
            rc = upd.main(["-c", "/nonexistent.toml", "status"])
        self.assertEqual(rc, 3)
        self.assertIn("disk is full", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_other_errors_still_raise(self):
        def boom(cfg, args):
            raise OSError(errno.EACCES, "Permission denied")
        with mock.patch.object(upd.Config, "load", staticmethod(lambda p: object())), \
                mock.patch.object(upd, "cmd_status", boom):
            with self.assertRaises(PermissionError):
                upd.main(["-c", "/nonexistent.toml", "status"])

    def test_atomic_write_mode(self):
        tmp = Path(tempfile.mkdtemp(prefix="aw-"))
        try:
            upd.atomic_write(tmp / "a", "x", 0o600)
            self.assertEqual(stat.S_IMODE((tmp / "a").stat().st_mode), 0o600)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class AircraftWording(unittest.TestCase):
    """Finding 15: no raw decoder log lines on the aircraft page."""

    def test_plain(self):
        import aircraft
        raw = "The aircraft decoder stopped early: [2026-11-17 13:54:28.200 UTC] Abnormal exit. uptime:  0d  0h  0m 0.005s"
        m = aircraft.plain_message(raw)
        self.assertIn("plugged in", m)
        self.assertNotIn("uptime", m)
        self.assertNotIn("2026", m)
        m = aircraft.plain_message("The aircraft decoder stopped (weird thing 42); trying again shortly.")
        self.assertIn("try again shortly", m)
        self.assertNotIn("42", m)
        self.assertEqual(aircraft.plain_message("Asking the radio…"), "Asking the radio…")
        self.assertEqual(aircraft.plain_message(None), "")
        self.assertLessEqual(len(aircraft.plain_message("x" * 1000)), 300)


if __name__ == "__main__":
    unittest.main(verbosity=1)
