#!/usr/bin/env python3
"""
Adversarial tests for the portal API, against a real standalone instance on a
free localhost port with a throwaway data root, a fake kiwix-serve and a fake
llama-server (no nginx, no network).
    python3 tests/test_portal_security.py -v
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"
import server  # noqa: E402

PIN = "246810"
SECRET = "TOP-SECRET-OUTSIDE-DATA-ROOT"

SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss><channel>
<item><title>Water purification</title><link>/kiwix/content/book/A/Water</link>
  <description>&lt;img src=x onerror=alert(1)&gt;Boil water for one minute.</description>
  <book><title>Field Manual</title></book></item>
<item><title>Evil js</title><link>javascript:alert(1)</link><description>x</description></item>
<item><title>Evil ssrf</title><link>http://169.254.169.254/latest/meta-data</link><description>x</description></item>
<item><title>Evil proto-relative</title><link>//evil.example/x</link><description>x</description></item>
</channel></rss>"""


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Backend(http.server.BaseHTTPRequestHandler):
    """kiwix-serve (/kiwix/search, /kiwix/content/…) and llama-server (/ai/v1/chat/completions)."""
    protocol_version = "HTTP/1.0"
    llama_mode = "ok"            # ok | slow-headers | stall | error
    requests: list[str] = []

    def log_message(self, *a):
        pass

    def reply(self, code, body: bytes, ctype="text/html"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Backend.requests.append(self.path)
        u = urllib.parse.urlparse(self.path)
        if u.path == "/kiwix/search":
            return self.reply(200, SEARCH_XML.encode(), "text/xml")
        if u.path.startswith("/kiwix/content/"):
            return self.reply(200, b"<html><body><p>Boil water for one minute.</p></body></html>")
        return self.reply(404, b"no")

    def do_POST(self):
        Backend.requests.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        mode = Backend.llama_mode
        try:
            if mode == "slow-headers":
                time.sleep(6)
            if mode == "error":
                return self.reply(500, b"boom")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for tok in ("Boil ", "it ", "<b>hard</b>"):
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': tok}}]})}\n\n".encode())
                self.wfile.flush()
                if mode == "stall":
                    time.sleep(6)
            self.wfile.write(b"data: not-json\n\ndata: {\"choices\": []}\n\ndata: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class Srv(http.server.ThreadingHTTPServer):
    daemon_threads = True


class PortalSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="portal-sec-")).resolve()
        (cls.tmp / "secret.txt").write_text(SECRET)
        (cls.tmp / "outside").mkdir()
        (cls.tmp / "outside" / "inner.txt").write_text(SECRET)
        real = cls.tmp / "real-data"
        papers = real / "docs" / "papers"
        papers.mkdir(parents=True)
        (real / "maps").mkdir()
        (real / "zim").mkdir()
        (papers / "a #1.pdf").write_bytes(b"%PDF-1.4 hash")
        (papers / "page.html").write_text("<script>alert(document.domain)</script>")
        (papers / "pic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
        (papers / "empty.txt").write_bytes(b"")
        (papers / "escape.txt").symlink_to(cls.tmp / "secret.txt")          # file link out of data_root
        (papers / "escdir").symlink_to(cls.tmp / "outside")                 # dir link out of data_root
        (papers / "dangling.pdf").symlink_to(cls.tmp / "nope.pdf")          # broken link
        (papers / "inside.pmtiles").symlink_to(real / "maps" / "world.pmtiles")   # link that stays inside
        (real / "zim" / "evil").symlink_to(cls.tmp / "outside")             # a whole item that escapes
        cls.blob = bytes(range(256)) * 40
        (real / "maps" / "world.pmtiles").write_bytes(cls.blob)
        (cls.tmp / "data").symlink_to(real)
        (cls.tmp / "usb" / "usb0").mkdir(parents=True)
        state = cls.tmp / "state"
        state.mkdir()
        (state / "upd-state.json").write_text(json.dumps({"items": {
            "hesperian_books": {"version": "1", "filename": "papers", "path": "docs/papers", "size": 1},
            "mdwiki_en_all_maxi": {"version": "1", "filename": "evil", "path": "zim/evil", "size": 1},
        }, "history": []}))
        cls.backend = Srv(("127.0.0.1", 0), Backend)
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        cls.bbase = f"http://127.0.0.1:{cls.backend.server_address[1]}"
        cls.cfg = cls.tmp / "prepper.toml"
        cls.cfg.write_text(f"""
[upd]
data_root = "{cls.tmp / 'data'}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{state}/upd-state.json"
report_dir = "{state}/reports"
tier = "base"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
settings_file = "{state}/portal/portal-settings.json"
admin_pin = "{server.hash_pin(PIN)}"
board_file = "{state}/portal/board.jsonl"
mesh_outbox = "{state}/portal/mesh-outbox.jsonl"
usb_root = "{cls.tmp / 'usb'}"
ctl = "/nonexistent/prepperctl"
kiwix = "{cls.bbase}/kiwix"
llama = "{cls.bbase}/ai"
ask_timeout = 1.5
ask_concurrency = 1
allowed_origins = ["badger.lan"]
services = {{}}
""")
        server.CFG = server.PortalConfig(cls.cfg)
        server._ask_slots = None
        cls.old_timeout = server.Handler.timeout
        server.Handler.timeout = 3                  # slowloris test: idle sockets dropped after 3 s
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        cls.host = f"127.0.0.1:{cls.port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.backend.shutdown()
        cls.backend.server_close()
        server.Handler.timeout = cls.old_timeout
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        server.reset_login_limits()
        server._last_post.clear()
        server.TOKENS.clear()
        server._cache.clear()
        server.CFG.llama = f"{self.bbase}/ai"
        server.CFG.kiwix = f"{self.bbase}/kiwix"
        Backend.llama_mode = "ok"

    # ── helpers ──
    def req(self, method, path, body=None, headers=None, timeout=15):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def post(self, path, obj, headers=None, **kw):
        r, data = self.req("POST", path, body=json.dumps(obj),
                           headers={"Content-Type": "application/json", **(headers or {})}, **kw)
        return r, data

    def jpost(self, path, obj, headers=None):
        r, data = self.post(path, obj, headers)
        return r.status, json.loads(data)

    def jget(self, path, headers=None):
        r, data = self.req("GET", path, headers=headers)
        return r.status, json.loads(data)

    def raw(self, payload: bytes, timeout=10, half_close=False) -> bytes:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        s.sendall(payload)
        if half_close:
            s.shutdown(socket.SHUT_WR)
        buf = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except (socket.timeout, ConnectionResetError):
            pass
        s.close()
        return buf

    def login(self, ip="10.9.9.9") -> str:
        st, d = self.jpost("/api/admin/login", {"pin": PIN}, {"X-Real-IP": ip})
        self.assertEqual(st, 200, d)
        return d["token"]

    def assert_no_secret(self, data: bytes, what=""):
        self.assertNotIn(SECRET.encode(), data, what)

    # ── path traversal ──
    TRAVERSALS = ["..", "../", "../..", "../../..", "../../../../../../etc", "%2e%2e", "%2e%2e/%2e%2e",
                  "%252e%252e/%252e%252e", "..%2f..%2f..", "..\\..\\..", "/etc", "/etc/passwd", "//etc",
                  "docs/../../..", "escdir", "escdir/inner.txt", "escape.txt", "x%00y", "%00", "a" * 5000,
                  ("../" * 400) + "etc", "....//....//", "%c0%ae%c0%ae/", "‥/‥"]

    def test_traversal_in_files_query(self):
        for p in self.TRAVERSALS:
            for param in ("path", "item"):
                with self.subTest(param=param, p=p[:40]):
                    q = {"item": "hesperian_books", "path": ""}
                    q[param] = p
                    # Send the payload exactly as written (already-encoded parts stay encoded once).
                    keep = "%/\\."           # the payload's own %-escapes, slashes, backslashes and dots go as written
                    qs = "&".join(k + "=" + (urllib.parse.quote(v, safe=keep) if v is p else urllib.parse.quote(v))
                                  for k, v in q.items())
                    r, data = self.req("GET", "/api/files?" + qs)
                    self.assertIn(r.status, (404,), data[:200])
                    self.assert_no_secret(data)
                    self.assertEqual(r.getheader("Content-Type"), "application/json")

    def test_whole_item_symlinked_out_is_refused(self):
        st, d = self.jget("/api/files?item=mdwiki_en_all_maxi&path=")
        self.assertEqual(st, 404, d)

    def test_traversal_in_content_and_static_paths(self):
        paths = ["/content/../secret.txt", "/content/../../secret.txt", "/content/%2e%2e/secret.txt",
                 "/content/%2e%2e/%2e%2e/secret.txt", "/content/%252e%252e/secret.txt",
                 "/content/..%2fsecret.txt", "/content/..%5c..%5csecret.txt", "/content/docs/papers/escape.txt",
                 "/content/docs/papers/escdir/inner.txt", "/content/%00", "/content/docs%00/x",
                 "/content//" + str(self.tmp / "secret.txt"), "/content/" + "a/" * 3000,
                 "/../secret.txt", "/%2e%2e/%2e%2e/secret.txt", "/tools/../../../secret.txt",
                 "/..%2f..%2f..%2fsecret.txt", "//" + str(self.tmp / "secret.txt"),
                 "/content/docs/papers/dangling.pdf"]
        for p in paths:
            with self.subTest(p=p[:60]):
                r, data = self.req("GET", p)
                self.assertNotEqual(r.status, 200, (p, data[:100]))
                self.assertLess(r.status, 500, p)
                self.assert_no_secret(data, p)
                r, data = self.req("HEAD", p)
                self.assertNotEqual(r.status, 200, p)

    def test_symlinks_out_of_data_root_not_listed_or_served(self):
        st, d = self.jget("/api/files?item=hesperian_books&path=")
        self.assertEqual(st, 200, d)              # (a dangling link used to make this a 500)
        names = {e["name"] for e in d["entries"]}
        self.assertNotIn("escape.txt", names)
        self.assertNotIn("escdir", names)
        self.assertNotIn("dangling.pdf", names)
        self.assertIn("inside.pmtiles", names, "a link that stays inside data_root is fine")
        self.assertIn("a #1.pdf", names)
        st, d = self.jget("/api/files?item=hesperian_books&path=escdir")
        self.assertEqual(st, 404)
        r, data = self.req("GET", "/content/docs/papers/inside.pmtiles")
        self.assertEqual((r.status, data), (200, self.blob))

    # ── Range ──
    def test_range_edge_cases(self):
        url, n = "/content/maps/world.pmtiles", len(self.blob)
        cases = {
            "bytes=-0": (416, b""), "bytes=999999999-": (416, b""), f"bytes={n}-": (416, b""),
            "bytes=0-1,5-6": (200, self.blob), "bytes=abc": (200, self.blob), "bytes=": (200, self.blob),
            "bytes=--1": (200, self.blob), "items=0-1": (200, self.blob), "bytes=1-0": (200, self.blob),
            "bytes=" + "9" * 5000 + "-": (200, self.blob), "bytes=0-" + "9" * 30: (200, self.blob),
            "bytes=0-0": (206, self.blob[:1]), f"bytes={n - 1}-": (206, self.blob[-1:]),
            f"bytes=-{n * 10}": (206, self.blob), "bytes = 2 - 3": (206, self.blob[2:4]),
        }
        for rng, (status, body) in cases.items():
            with self.subTest(range=rng[:40]):
                r, data = self.req("GET", url, headers={"Range": rng})
                self.assertEqual(r.status, status, rng[:40])
                self.assertEqual(data, body)
                self.assertEqual(int(r.getheader("Content-Length")), len(body))
                if status == 416:
                    self.assertEqual(r.getheader("Content-Range"), f"bytes */{n}")
        r, _ = self.req("HEAD", url)
        etag, lm = r.getheader("ETag"), r.getheader("Last-Modified")
        r, data = self.req("GET", url, headers={"Range": "bytes=0-9", "If-Range": etag})
        self.assertEqual((r.status, data), (206, self.blob[:10]))
        r, data = self.req("GET", url, headers={"Range": "bytes=0-9", "If-Range": lm})
        self.assertEqual((r.status, data), (206, self.blob[:10]))
        for stale in ('"stale"', 'W/' + etag, "Mon, 01 Jan 1990 00:00:00 GMT", "garbage"):
            r, data = self.req("GET", url, headers={"Range": "bytes=0-9", "If-Range": stale})
            self.assertEqual((r.status, len(data)), (200, n), stale)
        r, data = self.req("GET", "/content/docs/papers/empty.txt", headers={"Range": "bytes=0-"})
        self.assertEqual(r.status, 416)
        r, data = self.req("GET", "/content/docs/papers/empty.txt")
        self.assertEqual((r.status, data), (200, b""))

    # ── bodies ──
    def test_oversized_chunked_and_misframed_bodies(self):
        hdr = b"POST /api/board HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        out = self.raw(hdr + b"Content-Length: 2000000\r\n\r\n{")
        self.assertTrue(out.startswith(b"HTTP/1.1 413"), out[:80])
        # Chunked: refused, connection closed, and the "chunk" is never run as a second request.
        smuggled = b"GET /api/status HTTP/1.1\r\nHost: x\r\n\r\n"
        body = b"%x\r\n%s\r\n0\r\n\r\n" % (len(smuggled), smuggled)
        out = self.raw(hdr + b"Transfer-Encoding: chunked\r\n\r\n" + body)
        self.assertTrue(out.startswith(b"HTTP/1.1 411"), out[:80])
        self.assertEqual(out.count(b"HTTP/1.1 "), 1, "smuggled request must not be answered")
        for cl in (b"-1", b"+5", b"5_0", b"abc", b"1e3", b"\xd9\xa5"):
            with self.subTest(cl=cl):
                out = self.raw(hdr + b"Content-Length: " + cl + b"\r\n\r\n{}")
                self.assertTrue(out.startswith(b"HTTP/1.1 400"), out[:80])
                self.assertIn(b"Connection: close", out)
        out = self.raw(hdr + b"Content-Length: 2\r\nContent-Length: 40\r\n\r\n{}")
        self.assertTrue(out.startswith(b"HTTP/1.1 400"), out[:80])
        # Promises 100 bytes, sends 2 and half-closes: a clean 400, no wedged thread.
        t0 = time.monotonic()
        out = self.raw(hdr + b"Content-Length: 100\r\n\r\n{}", half_close=True)
        self.assertTrue(out.startswith(b"HTTP/1.1 400"), out[:80])
        self.assertLess(time.monotonic() - t0, 5)
        # GET with a body: the body is never parsed as another request.
        out = self.raw(b"GET /api/alerts HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n%s" % (len(smuggled), smuggled))
        self.assertEqual(out.count(b"HTTP/1.1 200"), 1, out[:300])

    def test_json_shapes(self):
        bad = [b"[1,2]", b'"x"', b"null", b"123", b"true", b"{not json", b"[" * 200_000 + b"]" * 200_000,
               b'{"a":' * 50_000 + b"1" + b"}" * 50_000, b'{"n": ' + b"1" * 5000 + b"}", b"\xff\xfe{}",
               b'{"a": NaN', b"\x00"]
        for body in bad:
            with self.subTest(body=body[:30]):
                r, data = self.req("POST", "/api/board", body=body, headers={"Content-Type": "application/json",
                                                                             "X-Real-IP": "10.1.1.1"})
                self.assertEqual(r.status, 400, data[:200])
                self.assertIn("error", json.loads(data))
        # Wrong types inside a valid object: refused cleanly, never a 500.
        for obj in ({"pin": ["246810"]}, {"pin": {"x": 1}}, {"pin": 246810}, {"pin": None}, {"pin": "9" * 100_000}):
            with self.subTest(obj=str(obj)[:40]):
                st, d = self.jpost("/api/admin/login", obj, {"X-Real-IP": "10.1.1.2"})
                self.assertIn(st, (400, 403), d)
        for obj in ({"name": ["x"], "text": {"y": 1}}, {"text": 5, "mesh": "yes"}):
            st, d = self.jpost("/api/board", obj, {"X-Real-IP": "10.1.1.3"})
            self.assertLess(st, 500, d)
            server._last_post.clear()

    def test_wrong_content_types_refused(self):
        for ctype in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
                      "application/jsonx", "text/json", "application/json-patch+json"):
            for path in ("/api/board", "/api/admin/login", "/api/ask", "/api/admin/system"):
                with self.subTest(ctype=ctype, path=path):
                    h = {"Content-Type": ctype} if ctype else {}
                    r, data = self.req("POST", path, body=b'{"pin":"246810","text":"hi","q":"water"}', headers=h)
                    self.assertEqual(r.status, 415, data[:200])
        st, d = self.jpost("/api/board", {"text": "hello"}, {"X-Real-IP": "10.1.1.4"})
        self.assertEqual(st, 200)
        r, data = self.req("POST", "/api/board", body=json.dumps({"text": "hi again"}),
                           headers={"Content-Type": "Application/JSON; charset=utf-8", "X-Real-IP": "10.1.1.5"})
        self.assertEqual(r.status, 200, data)

    # ── slowloris ──
    def test_slow_clients_do_not_wedge_the_server(self):
        socks = []
        for _ in range(40):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            s.sendall(b"GET /api/status HTTP/1.1\r\nHost: x\r\n")      # headers never finished
            socks.append(s)
        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=10) for _ in range(10)]   # say nothing
        slow_body = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        slow_body.sendall(b"POST /api/board HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                          b"Content-Length: 500\r\n\r\n{\"te")
        t0 = time.monotonic()
        st, d = self.jget("/api/alerts")
        self.assertEqual(st, 200)
        self.assertLess(time.monotonic() - t0, 2, "a normal request is served while 50 sockets dawdle")
        # Then the server hangs up on them (Handler.timeout = 3 s here; 60 s in production).
        time.sleep(4)
        closed = 0
        for s in socks + idle + [slow_body]:
            try:
                s.settimeout(2)
                closed += s.recv(1024) == b"" or True       # any answer (EOF or a 4xx) means it let go
            except (ConnectionResetError, BrokenPipeError):
                closed += 1
            except socket.timeout:
                pass
            finally:
                s.close()
        self.assertEqual(closed, 51, "every dawdling connection was dropped")

    # ── PIN brute force ──
    def test_pin_lockout_and_correct_pin_refused_while_locked(self):
        ip = {"X-Real-IP": "10.2.0.1"}
        codes = [self.jpost("/api/admin/login", {"pin": "000000"}, ip)[0] for _ in range(3)]
        self.assertEqual(codes, [403, 403, 403])
        r, data = self.post("/api/admin/login", {"pin": "000000"}, ip)
        self.assertEqual(r.status, 429)
        self.assertGreaterEqual(int(r.getheader("Retry-After")), 1)
        st, d = self.jpost("/api/admin/login", {"pin": PIN}, ip)
        self.assertEqual(st, 429, "the right PIN still waits out the lockout")
        self.assertNotIn("token", d)
        # Another client is not locked out by this one.
        st, d = self.jpost("/api/admin/login", {"pin": PIN}, {"X-Real-IP": "10.2.0.2"})
        self.assertEqual(st, 200)

    def test_parallel_guesses_get_no_extra_tries(self):
        ip = {"X-Real-IP": "10.3.0.1"}
        codes, lock = [], threading.Lock()

        def guess(i):
            st, _ = self.jpost("/api/admin/login", {"pin": f"{100000 + i}"}, ip)
            with lock:
                codes.append(st)
        ts = [threading.Thread(target=guess, args=(i,)) for i in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(sorted(codes), [403] * 3 + [429] * 9,
                         "a burst from one client gets exactly 3 guesses, not one per thread")

    def test_global_backoff_across_spoofed_ips(self):
        def guess(i):
            self.jpost("/api/admin/login", {"pin": "111111"}, {"X-Real-IP": f"10.4.{i // 250}.{i % 250}"})
        ts = [threading.Thread(target=guess, args=(i,)) for i in range(22)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        st, d = self.jpost("/api/admin/login", {"pin": PIN}, {"X-Real-IP": "10.4.9.9"})
        self.assertEqual(st, 429, "22 misses from 22 addresses slow everyone down for a while")

    def test_trickle_of_guesses_cannot_lock_the_owner_out(self):
        clock = [1_000_000.0]
        real = server._now
        server._now = lambda: clock[0]
        try:
            # One wrong PIN every 2 minutes from ever-new addresses, for 10 hours.
            for i in range(300):
                server.login_failed(f"10.8.{i // 250}.{i % 250}")
                clock[0] += 120
                self.assertEqual(server.login_wait("10.8.99.99"), 0.0, f"locked after {i + 1} trickled guesses")
            # A real flood does brake strangers for a while …
            for i in range(30):
                server.login_failed(f"10.9.0.{i}")
                clock[0] += 1
            self.assertGreater(server.login_wait("10.8.99.99"), 0)
            # … but not the console, nor a device that has logged in before …
            self.assertEqual(server.login_wait("127.0.0.1", local=True), 0.0)
            server.login_ok("10.8.50.50")
            self.assertEqual(server.login_wait("10.8.50.50"), 0.0)
            # … and it wears off once the flood stops.
            clock[0] += server.GLOBAL_WINDOW + 1
            self.assertEqual(server.login_wait("10.8.99.99"), 0.0)
        finally:
            server._now = real
        # Over HTTP: a device that logged in before gets in during a flood from others.
        self.login("10.8.7.7")
        def guess(i):
            self.jpost("/api/admin/login", {"pin": "111111"}, {"X-Real-IP": f"10.10.0.{i}"})
        ts = [threading.Thread(target=guess, args=(i,)) for i in range(24)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(self.jpost("/api/admin/login", {"pin": PIN}, {"X-Real-IP": "10.10.9.9"})[0], 429)
        self.assertEqual(self.jpost("/api/admin/login", {"pin": PIN}, {"X-Real-IP": "10.8.7.7"})[0], 200)

    def test_x_real_ip_only_trusted_from_loopback(self):
        h = server.Handler.__new__(server.Handler)
        h.headers = {"X-Real-IP": "1.2.3.4"}
        h.client_address = ("10.0.0.5", 1234)
        self.assertEqual(h.client_ip(), "10.0.0.5")
        h.client_address = ("127.0.0.1", 1234)
        self.assertEqual(h.client_ip(), "1.2.3.4")

    # ── tokens ──
    ADMIN = {"updates": {"action": "check"}, "uplink": {"forget": True}, "service": {"name": "kiwix", "action": "stop"},
             "power": {"mode": "full"}, "settings": {"display_time": "utc"}, "usb": {"device": "usb0"},
             "system": {"action": "reboot"}, "logoutx": {}, "nonexistent": {}, "../login": {"pin": PIN},
             "login/../system": {"action": "reboot"}}

    def test_every_admin_endpoint_needs_a_valid_token(self):
        good = self.login()
        forged = [None, "", " ", "x", good[:-1], good + "x", good.upper(), "null", "undefined", "0" * 32,
                  "expired-token", good + "\r\nX-Admin-Token: " + good]
        server.TOKENS["expired-token"] = time.time() - 1
        for action, body in self.ADMIN.items():
            for tok in forged:
                with self.subTest(action=action, tok=str(tok)[:12]):
                    h = {} if tok is None else {"X-Admin-Token": tok}
                    try:
                        r, data = self.post("/api/admin/" + action, body, h)
                    except ValueError:        # http.client itself refuses a header with CR/LF
                        continue
                    self.assertIn(r.status, (401, 404) if "/" in action else (401,), data[:200])
        r, data = self.req("GET", "/api/admin/settings", headers={"X-Admin-Token": good})
        self.assertEqual(r.status, 404, "admin actions are never reachable by GET")
        st, d = self.jpost("/api/admin/settings", {"display_time": "utc"}, {"X-Admin-Token": good})
        self.assertEqual((st, d.get("ok")), (200, True))

    def test_logout_revokes_and_expiry_is_enforced(self):
        tok = self.login()
        h = {"X-Admin-Token": tok}
        self.assertEqual(self.jpost("/api/admin/power", {"mode": "full"}, h)[0], 200)
        st, d = self.jpost("/api/admin/logout", {}, h)
        self.assertEqual((st, d["ok"]), (200, True))
        self.assertEqual(self.jpost("/api/admin/power", {"mode": "full"}, h)[0], 401, "token dead after logout")
        st, d = self.jpost("/api/admin/logout", {}, h)
        self.assertEqual(d["ok"], False, "a second logout finds nothing")
        st, d = self.jpost("/api/admin/logout", {}, {"X-Admin-Token": "bogus"})
        self.assertEqual(d["ok"], False)
        tok2 = self.login()
        server.TOKENS[tok2] = time.time() - 0.01                    # 12 h passed
        self.assertEqual(self.jpost("/api/admin/power", {"mode": "full"}, {"X-Admin-Token": tok2})[0], 401)
        self.assertNotIn(tok2, server.TOKENS, "expired tokens are pruned")

    def test_admin_inputs_are_confined(self):
        h = {"X-Admin-Token": self.login()}
        usb = (self.tmp / "usb").resolve()
        for src in ("/etc/passwd", "../../etc", "/media/../etc", str(usb) + "/../secret.txt", "", "\x00",
                    str(self.tmp / "secret.txt"), "usb0/../../secret.txt", ["usb0"]):
            for verb, key in (("import", "src"), ("export", "dest")):
                with self.subTest(verb=verb, src=str(src)[:30]):
                    st, d = self.jpost("/api/admin/updates", {"action": verb, key: src}, h)
                    self.assertEqual(st, 400, d)
        # Nothing mounted at usb0: it's a plain folder on the system disk, so an
        # export there would fill the SD card. Refused.
        for verb, key in (("import", "src"), ("export", "dest")):
            st, d = self.jpost("/api/admin/updates", {"action": verb, key: "usb0/bundle"}, h)
            self.assertEqual(st, 400, d)
        real = server.on_usb_mount
        server.on_usb_mount = lambda p, root: p == usb / "usb0" or (usb / "usb0") in p.parents   # "plugged in"
        try:
            st, d = self.jpost("/api/admin/updates", {"action": "import", "src": "usb0/bundle"}, h)
            self.assertEqual(st, 200, d)
            self.assertIn(f"update-import {usb}/usb0/bundle", d["output"], "the resolved absolute path is passed on")
            st, d = self.jpost("/api/admin/updates", {"action": "export", "dest": "usb0/-rf"}, h)
            self.assertIn(f"update-export {usb}/usb0/-rf", d["output"], "never a bare option")
            st, d = self.jpost("/api/admin/updates", {"action": "export", "dest": "other/x"}, h)
            self.assertEqual(st, 400, "a folder beside the mounted stick is still the system disk")
        finally:
            server.on_usb_mount = real
        self.assertFalse(server.on_usb_mount(usb / "usb0" / "x", usb), "a temp folder is not a mount")
        for dev in ("--all", "-f", "../sda", "usb0; reboot", "", "a/../../b"):
            with self.subTest(dev=dev):
                self.assertEqual(self.jpost("/api/admin/usb", {"device": dev}, h)[0], 400)
        self.assertEqual(self.jpost("/api/admin/usb", {"device": "usb0"}, h)[0], 200)
        for body in ({"ssid": "-x"}, {"ssid": "home", "psk": "short"}, {"ssid": "home", "psk": "1234567"},
                     {"ssid": "home", "psk": "p"}, {"ssid": "a\nb"}, {"ssid": "x" * 33}, {"ssid": ""},
                     {"ssid": "home", "psk": "p" * 64}, {"ssid": "home", "psk": "g" * 64},
                     {"ssid": "home", "psk": "pässwörd123"}, {"ssid": "home", "psk": "line\nbreak!"}):
            with self.subTest(uplink=str(body)[:30]):
                self.assertEqual(self.jpost("/api/admin/uplink", body, h)[0], 400)
        for psk in ("12345678", "", "a" * 63, "0123456789abcdefABCDEF" + "0" * 42):
            self.assertEqual(self.jpost("/api/admin/uplink", {"ssid": "home", "psk": psk}, h)[0], 200, psk)
        for body in ({"name": "kiwix;reboot", "action": "stop"}, {"name": "kiwix", "action": "rm"}):
            self.assertEqual(self.jpost("/api/admin/service", body, h)[0], 400)
        st, d = self.jpost("/api/admin/settings", {"renderer_defaults": {"pdf": {"x": 1}, "zim": ["a"], "nope": "x",
                                                                         "epub": "foliate"}}, h)
        self.assertEqual(st, 200, d)
        self.assertEqual(d["settings"]["renderer_defaults"], {"epub": "foliate"})
        for peer in ("http://x/;reboot", "file:///etc", "http://a b", "javascript:x", "http://x/$(id)"):
            self.assertEqual(self.jpost("/api/admin/updates", {"action": "add-peer", "peer": peer}, h)[0], 400)

    def test_wifi_password_goes_on_stdin_never_argv(self):
        """prepperctl uplink-wifi SSID < password: anything in argv shows up in `ps`."""
        fake = self.tmp / "fake-prepperctl"
        log = self.tmp / "ctl.log"
        fake.write_text(f"#!/bin/sh\nprintf 'ARGV:%s\\n' \"$*\" >> {log}\nprintf 'STDIN:' >> {log}\ncat >> {log}\n")
        fake.chmod(0o755)
        h = {"X-Admin-Token": self.login()}
        old = server.CFG.ctl
        server.CFG.ctl = str(fake)
        try:
            secret = "correct horse battery"
            st, d = self.jpost("/api/admin/uplink", {"ssid": "Neighbour Net", "psk": secret}, h)
            self.assertEqual(st, 200, d)
            st, d = self.jpost("/api/admin/uplink", {"ssid": "Open Cafe", "psk": ""}, h)
            self.assertEqual(st, 200, d)
        finally:
            server.CFG.ctl = old
        text = log.read_text()
        argv = [line for line in text.splitlines() if line.startswith("ARGV:")]
        self.assertEqual(argv, ["ARGV:uplink-wifi Neighbour Net", "ARGV:uplink-wifi Open Cafe"])
        self.assertNotIn(secret, "\n".join(argv), "the password is never an argument")
        self.assertIn(f"STDIN:{secret}\n", text, "it arrives as one line on stdin")
        self.assertIn("STDIN:\n", text, "an open network sends an empty line")

    def test_usb_list_names_what_usb_eject_accepts(self):
        usb = self.tmp / "usb"
        for d in ("usb-sda1", "alice/KINGSTON", "alice/WEIRD", "has space"):
            (usb / d).mkdir(parents=True, exist_ok=True)
        mounts = {str(usb / "usb-sda1"), str(usb / "alice" / "KINGSTON"), str(usb / "alice" / "WEIRD"),
                  str(usb / "has space")}
        sources = {str(usb / "alice" / "KINGSTON"): "/dev/sdb1", str(usb / "alice" / "WEIRD"): "/dev/../sda",
                   str(usb / "has space"): "/dev/sdc1"}
        real_ismount = os.path.ismount
        with unittest.mock.patch.object(server.os.path, "ismount", lambda p: str(p) in mounts or real_ismount(p)), \
                unittest.mock.patch.object(server, "mount_sources", lambda: sources):
            self.assertEqual(self.jget("/api/usb"), (200, []))      # visitors: the family's drives aren't listed
            st, lst = self.jget("/api/usb", headers={"X-Admin-Token": self.login()})
        self.assertEqual(st, 200)
        eject = {u["path"].removeprefix(str(usb) + "/"): u["eject"] for u in lst}
        self.assertEqual(eject, {"usb-sda1": "usb-sda1",            # udev mount: its name under /media
                                 "alice/KINGSTON": "/dev/sdb1",     # nested: only its device can name it
                                 "alice/WEIRD": None,               # odd source: not offered
                                 "has space": "/dev/sdc1"})         # a name usb-eject would refuse
        for e in filter(None, eject.values()):
            self.assertTrue(server.EJECT_NAME.fullmatch(e), e)
        h = {"X-Admin-Token": self.login()}
        for dev in ("usb-sda1", "/dev/sdb1"):
            self.assertEqual(self.jpost("/api/admin/usb", {"device": dev}, h)[0], 200, dev)
        for dev in ("alice/KINGSTON", "has space", "KINGSTON LABEL", "/dev/../sda", "/media/usb-sda1"):
            self.assertEqual(self.jpost("/api/admin/usb", {"device": dev}, h)[0], 400, dev)

    # ── CSRF / cross-origin ──
    def test_cross_origin_posts_refused(self):
        evil = [{"Origin": "http://evil.example"}, {"Origin": "null"}, {"Origin": "file://"},
                {"Origin": f"http://{self.host}.evil.example"}, {"Origin": "http://127.0.0.1"},
                {"Sec-Fetch-Site": "cross-site"},
                {"Origin": f"http://{self.host}", "Sec-Fetch-Site": "cross-site"}]
        for h in evil:
            for path, body in (("/api/admin/login", {"pin": PIN}), ("/api/board", {"text": "spam"}),
                               ("/api/ask", {"q": "x"})):
                with self.subTest(h=h, path=path):
                    st, d = self.jpost(path, body, {**h, "X-Real-IP": "10.5.0.1"})
                    self.assertEqual(st, 403, d)
                    self.assertNotIn("token", d)
        self.assertEqual(server._fail_ip, {}, "refused before the PIN was even checked")
        st, d = self.jpost("/api/board", {"text": "same origin"},
                           {"Origin": f"http://{self.host}", "Sec-Fetch-Site": "same-origin", "X-Real-IP": "10.5.0.2"})
        self.assertEqual(st, 200, d)
        st, d = self.jpost("/api/board", {"text": "listed origin"}, {"Origin": "http://badger.lan", "X-Real-IP": "10.5.0.3"})
        self.assertEqual(st, 200, d)
        st, d = self.jpost("/api/board", {"text": "no origin: curl / builder"}, {"X-Real-IP": "10.5.0.4"})
        self.assertEqual(st, 200, d)
        # Behind nginx (Host = the upstream address) the Origin can't be compared; Sec-Fetch-Site still counts.
        server.CFG.standalone = False
        try:
            st, d = self.jpost("/api/board", {"text": "proxied"},
                               {"Host": "127.0.0.1:8000", "Origin": "http://badger.local", "X-Real-IP": "10.5.0.5"})
            self.assertEqual(st, 200, d)
            st, d = self.jpost("/api/board", {"text": "proxied"}, {"Host": "127.0.0.1:8000", "Origin": "http://evil.example",
                                                                   "Sec-Fetch-Site": "cross-site", "X-Real-IP": "10.5.0.6"})
            self.assertEqual(st, 403, d)
            st, d = self.jpost("/api/board", {"text": "proxied"}, {"Host": "badger.lan", "Origin": "http://evil.example",
                                                                   "X-Real-IP": "10.5.0.7"})
            self.assertEqual(st, 403, "when nginx forwards Host, the Origin is checked against it")
        finally:
            server.CFG.standalone = True

    def test_preflight_never_grants_post_or_admin_header(self):
        r, _ = self.req("OPTIONS", "/api/admin/system", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-admin-token"})
        allow_h = (r.getheader("Access-Control-Allow-Headers") or "").lower()
        allow_m = (r.getheader("Access-Control-Allow-Methods") or "").upper()
        self.assertNotIn("content-type", allow_h)
        self.assertNotIn("x-admin-token", allow_h)
        self.assertNotIn("POST", allow_m)
        r, data = self.post("/api/admin/login", {"pin": "000000"}, {"X-Real-IP": "10.5.1.1"})
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"), "POST replies are never readable cross-origin")
        r, data = self.req("GET", "/api/alerts")
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "*")

    # ── header injection & XSS ──
    def test_hostile_file_names_stay_data(self):
        papers = self.tmp / "real-data" / "docs" / "papers"
        names = ['quote".pdf', "<img src=x onerror=alert(1)>.pdf", "semi;colon.pdf", "pct%0d%0a.pdf"]
        try:
            (papers / "evil\r\nX-Injected: 1.pdf").write_bytes(b"%PDF crlf")
            names.append("evil\r\nX-Injected: 1.pdf")
        except OSError:
            pass
        for n in names[:4]:
            (papers / n).write_bytes(b"%PDF " + n.encode())
        try:
            r, data = self.req("GET", "/api/files?item=hesperian_books&path=")
            self.assertEqual(r.status, 200)
            self.assertNotIn(b"<", data, "markup characters are \\u-escaped in JSON")
            self.assertIsNone(r.getheader("X-Injected"))
            listed = {e["name"]: e for e in json.loads(data)["entries"]}
            for n in names:
                with self.subTest(name=n):
                    self.assertIn(n, listed)
                    for rend in listed[n]["renderers"]:
                        self.assertNotRegex(rend["url"], r"[\r\n\"<> ]", "URLs are percent-encoded")
                    url = [x["url"] for x in listed[n]["renderers"] if x["url"].startswith("/content/")][0]
                    r, body = self.req("GET", url)
                    self.assertEqual(r.status, 200, url)
                    self.assertIsNone(r.getheader("X-Injected"))
                    self.assertIsNone(r.getheader("Content-Disposition"))
            for p in ("/content/docs/papers/a%0d%0aX-Injected:%201", "/content/docs%0d%0aX-Injected:%201",
                      "/content/docs/papers%0d%0aLocation:%20http://evil"):
                r, _ = self.req("GET", p)
                self.assertIsNone(r.getheader("X-Injected"), p)
                self.assertNotIn("evil", r.getheader("Location") or "", p)
        finally:
            for n in names:
                (papers / n).unlink(missing_ok=True)

    def test_xss_payloads_come_back_as_inert_json(self):
        payload = '<script>alert("x")</script><img src=x onerror=alert(1)>'
        st, d = self.jpost("/api/board", {"name": payload, "text": payload + " & more"}, {"X-Real-IP": "10.6.0.1"})
        self.assertEqual(st, 200, d)
        r, data = self.req("GET", "/api/board")
        self.assertEqual(r.getheader("Content-Type"), "application/json")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertNotIn(b"<", data)
        self.assertNotIn(b">", data)
        msgs = json.loads(data)["messages"]
        self.assertEqual(msgs[-1]["name"], payload[:32], "stored as typed (the browser escapes on display)")
        self.assertEqual(msgs[-1]["text"], payload + " & more")
        r, data = self.req("GET", "/api/search?q=" + urllib.parse.quote(payload))
        self.assertEqual(r.status, 200)
        self.assertNotIn(b"<", data)
        res = json.loads(data)
        self.assertEqual(res["q"], payload)
        urls = [a["url"] for a in res["articles"]]
        self.assertEqual(urls, ["/kiwix/content/book/A/Water"],
                         "only kiwix-relative links survive: no javascript:, no absolute or //host URLs")
        self.assertEqual(res["articles"][0]["snippet"], "Boil water for one minute.")
        self.assertFalse(any("169.254" in p or "evil" in p for p in Backend.requests))

    def test_library_html_and_svg_are_sandboxed(self):
        for name, sandboxed in (("page.html", True), ("pic.svg", True), ("a%20%231.pdf", False)):
            r, _ = self.req("GET", "/content/docs/papers/" + name)
            self.assertEqual(r.status, 200, name)
            csp = r.getheader("Content-Security-Policy") or ""
            self.assertEqual("sandbox" in csp and "allow-same-origin" not in csp, sandboxed, name)
            self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    # ── /api/time ──
    def test_time_parameters_validated(self):
        for q in ("lat=nan&lon=1", "lat=1&lon=nan", "lat=inf&lon=1", "lat=1&lon=-inf", "lat=1e999&lon=1",
                  "lat=91&lon=0", "lat=0&lon=181", "lat=abc", "lon=1e308&mode=solar", "lat=--1&lon=2",
                  "lat=0x10&lon=1"):
            with self.subTest(q=q):
                st, d = self.jget("/api/time?" + q)
                self.assertEqual(st, 400, d)
                self.assertIn("error", d)
        st, d = self.jget("/api/time?lat=89.9&lon=-179.9&mode=apparent&locale=%3Cscript%3E&tz=Europe/Oslo")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["locale"], "en")
        st, d = self.jget("/api/time?mode=bogus")
        self.assertEqual(st, 400)

    # ── /api/ask ──
    def ask(self, q="how to purify water", timeout=20):
        t0 = time.monotonic()
        r, data = self.post("/api/ask", {"q": q}, timeout=timeout)
        events = []
        for block in data.decode().split("\n\n"):
            ev = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            if "event" in ev:
                events.append((ev["event"], json.loads(ev["data"])))
        return r, events, time.monotonic() - t0

    def test_ask_streams_sources_then_tokens(self):
        r, ev, _ = self.ask()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
        self.assertEqual([e for e, _ in ev][0], "sources")
        self.assertEqual(ev[0][1][0]["url"], "/kiwix/content/book/A/Water")
        self.assertEqual("".join(d for e, d in ev if e == "token"), "Boil it <b>hard</b>")
        self.assertEqual(ev[-1][0], "done")

    def test_ask_with_llama_down_fails_cleanly(self):
        server.CFG.llama = f"http://127.0.0.1:{closed_port()}/ai"
        r, ev, dt = self.ask()
        self.assertEqual(r.status, 200)
        self.assertIn("isn't answering", "".join(d for e, d in ev if e == "token"))
        self.assertEqual(ev[-1][0], "done")
        self.assertLess(dt, 5)

    def test_ask_with_llama_erroring_or_slow_fails_within_timeout(self):
        for mode in ("error", "slow-headers", "stall"):
            with self.subTest(mode=mode):
                Backend.llama_mode = mode
                r, ev, dt = self.ask()
                self.assertEqual(ev[-1][0], "done", mode)
                self.assertIn("isn't answering", "".join(d for e, d in ev if e == "token"))
                self.assertLess(dt, 5, f"{mode}: gave up after ask_timeout (1.5 s), took {dt:.1f}")

    def test_ask_with_kiwix_down_says_so(self):
        server.CFG.kiwix = f"http://127.0.0.1:{closed_port()}/kiwix"
        r, ev, dt = self.ask()
        self.assertEqual(ev[0], ("sources", []))
        self.assertIn("couldn't find anything", "".join(d for e, d in ev if e == "token"))
        self.assertEqual(ev[-1][0], "done")

    def test_ask_concurrency_is_bounded_and_disconnects_are_harmless(self):
        Backend.llama_mode = "stall"
        first = {}
        t = threading.Thread(target=lambda: first.update(zip(("r", "ev", "dt"), self.ask(), strict=True)))
        t.start()
        time.sleep(0.6)
        r, data = self.post("/api/ask", {"q": "second"})
        self.assertEqual(r.status, 503, data[:200])
        self.assertTrue(r.getheader("Retry-After"))
        t.join(20)
        self.assertEqual(first["ev"][-1][0], "done")
        # A browser that leaves mid-answer: the slot is released and the server carries on.
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        body = b'{"q": "water"}'
        s.sendall(b"POST /api/ask HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                  % (len(body), body))
        self.assertTrue(s.recv(100).startswith(b"HTTP/1.1 200"))
        s.close()
        time.sleep(2.5)
        Backend.llama_mode = "ok"
        r, ev, _ = self.ask()
        self.assertEqual(r.status, 200, "slot freed after the disconnect")
        self.assertEqual(ev[-1][0], "done")

    # ── concurrency ──
    def test_concurrent_requests(self):
        tok = self.login()
        results, lock = [], threading.Lock()

        def work(i):
            k = i % 6
            try:
                if k == 0:
                    r, _ = self.req("GET", "/api/status")
                elif k == 1:
                    r, _ = self.req("GET", "/api/library")
                elif k == 2:
                    r, _ = self.req("GET", "/api/files?item=hesperian_books&path=")
                elif k == 3:
                    r, d = self.req("GET", "/content/maps/world.pmtiles", headers={"Range": f"bytes={i}-{i + 99}"})
                    assert d == self.blob[i:i + 100]
                elif k == 4:
                    r, _ = self.post("/api/board", {"name": f"n{i}", "text": f"msg {i}"}, {"X-Real-IP": f"10.7.0.{i}"})
                else:
                    r, _ = self.post("/api/admin/settings", {"display_time": ("utc", "local", "nutime")[i % 3]},
                                     {"X-Admin-Token": tok})
                code = r.status
            except Exception as e:  # noqa: BLE001
                code = repr(e)
            with lock:
                results.append(code)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(60)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual([c for c in results if c not in (200, 206)], [])
        settings = json.loads(server.CFG.settings_file.read_text())
        self.assertIn(settings["display_time"], ("utc", "local", "nutime"))
        self.assertFalse(list(server.CFG.settings_file.parent.glob(".*.tmp")), "no scratch files left behind")
        for line in server.CFG.board_file.read_text().splitlines():
            json.loads(line)

    def test_odd_methods_and_head(self):
        for m in ("PUT", "DELETE", "PATCH", "TRACE", "FOO"):
            r, _ = self.req(m, "/api/admin/system", body=b"{}", headers={"Content-Type": "application/json"})
            self.assertGreaterEqual(r.status, 400, m)
            self.assertLess(r.status, 600, m)
        r, _ = self.req("HEAD", "/api/status")
        self.assertEqual(r.status, 405)
        st, d = self.jget("/api/alerts")          # still alive
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main()
