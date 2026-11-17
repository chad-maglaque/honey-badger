#!/usr/bin/env python3
"""
Portal API tests against a throwaway standalone instance (no nginx/kiwix/llama).
    python3 tests/test_portal.py -v
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"
import server  # noqa: E402


class PortalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="portal-test-")).resolve()
        real = cls.tmp / "real-data"
        (real / "docs" / "papers").mkdir(parents=True)
        (real / "maps").mkdir()
        (real / "docs" / "papers" / "a #1.pdf").write_bytes(b"%PDF-1.4 hash")
        cls.blob = bytes(range(256)) * 40                       # 10,240 bytes
        (real / "maps" / "world.pmtiles").write_bytes(cls.blob)
        (cls.tmp / "data").symlink_to(real)                     # data_root behind a symlink
        state = cls.tmp / "state"
        state.mkdir()
        (state / "upd-state.json").write_text(json.dumps({"items": {"hesperian_books": {
            "version": "1", "filename": "papers", "path": "docs/papers", "size": 1}}, "history": []}))
        cls.cfg = cls.tmp / "prepper.toml"
        cls.cfg.write_text(f"""
[upd]
data_root = "{cls.tmp / 'data'}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{state}/upd-state.json"
report_dir = "{state}/reports"
tier = "base"

[time]
timezone = "Not/AZone"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
settings_file = "{state}/portal/portal-settings.json"
admin_pin = "{server.hash_pin('246810')}"
board_file = "{state}/portal/board.jsonl"
mesh_outbox = "{state}/portal/mesh-outbox.jsonl"
ctl = "/nonexistent/prepperctl"
services = {{}}
""")
        server.CFG = server.PortalConfig(cls.cfg)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def jget(self, path):
        r, data = self.req("GET", path)
        return r.status, json.loads(data)

    # 7 — Range requests in standalone mode
    def test_range_requests(self):
        url = "/content/maps/world.pmtiles"
        r, data = self.req("GET", url)
        self.assertEqual((r.status, data), (200, self.blob))
        self.assertEqual(r.getheader("Accept-Ranges"), "bytes")
        r, data = self.req("GET", url, headers={"Range": "bytes=10-19"})
        self.assertEqual((r.status, data), (206, self.blob[10:20]))
        self.assertEqual(r.getheader("Content-Range"), f"bytes 10-19/{len(self.blob)}")
        self.assertEqual(r.getheader("Content-Length"), "10")
        r, data = self.req("GET", url, headers={"Range": "bytes=10000-"})
        self.assertEqual((r.status, data), (206, self.blob[10000:]))
        r, data = self.req("GET", url, headers={"Range": "bytes=-16"})
        self.assertEqual((r.status, data), (206, self.blob[-16:]))
        r, data = self.req("GET", url, headers={"Range": "bytes=10200-99999"})       # end clamped
        self.assertEqual((r.status, data), (206, self.blob[10200:]))
        r, data = self.req("GET", url, headers={"Range": "bytes=20000-"})
        self.assertEqual(r.status, 416)
        self.assertEqual(r.getheader("Content-Range"), f"bytes */{len(self.blob)}")
        r, data = self.req("GET", url, headers={"Range": "bytes=5-2"})               # invalid → whole file
        self.assertEqual((r.status, len(data)), (200, len(self.blob)))
        r, data = self.req("GET", url, headers={"Range": "bytes=0-1", "If-Range": '"stale"'})
        self.assertEqual(r.status, 200, "If-Range mismatch → whole file")
        r, data = self.req("HEAD", url, headers={"Range": "bytes=0-99"})
        self.assertEqual((r.status, data, r.getheader("Content-Length")), (206, b"", "100"))

    # 8 + 10 — symlinked data_root, percent-encoded file URLs
    def test_files_through_symlink_and_encoded_urls(self):
        st, d = self.jget("/api/files?item=hesperian_books&path=")
        self.assertEqual(st, 200, d)
        e = [x for x in d["entries"] if x["name"] == "a #1.pdf"][0]
        urls = {r["id"]: r["url"] for r in e["renderers"]}
        self.assertEqual(urls["native"], "/content/docs/papers/a%20%231.pdf")
        self.assertIn("file=%2Fcontent%2Fdocs%2Fpapers%2Fa%2520%25231.pdf", urls["pdfjs"])
        r, data = self.req("GET", urls["native"])
        self.assertEqual((r.status, data), (200, b"%PDF-1.4 hash"))
        st, d = self.jget("/api/files?item=hesperian_books&path=../../..")
        self.assertEqual(st, 404)

    # 9 — every error is a JSON reply, not a dropped connection
    def test_errors_are_json(self):
        st, d = self.jget("/api/time?tz=Mars/Olympus")
        self.assertEqual(st, 200)
        self.assertIn("Mars/Olympus", d["notes"]["timezone"])
        st, d = self.jget("/api/time?tz=../../etc/passwd&lat=1&lon=2")
        self.assertEqual(st, 200)
        st, d = self.jget("/api/time?lat=abc")
        self.assertEqual(st, 400)
        st, d = self.jget("/api/status")                       # bad [time].timezone in config
        self.assertEqual(st, 200)
        self.assertEqual(d["clock"]["tz"], "UTC")
        self.assertIn("Not/AZone", d["clock"]["tz_note"])
        for body in (b"[1,2]", b'"x"', b"null", b"{not json"):
            r, data = self.req("POST", "/api/board", body=body, headers={"Content-Type": "application/json"})
            self.assertEqual(r.status, 400, body)
            self.assertIn("error", json.loads(data))

    def test_oversized_body_closes_connection(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(b"POST /api/board HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: 5000000\r\n\r\n{\"a\":")
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        self.assertTrue(buf.startswith(b"HTTP/1.1 413"), buf[:80])
        self.assertIn(b"Connection: close", buf)

    # 16 — mesh limit counts "name: text"
    def test_mesh_limit_includes_name(self):
        server._last_post.clear()
        ok, msg = server.board_post("10.0.0.9", "N" * 30, "x" * 180, True)
        self.assertFalse(ok)
        self.assertIn("200 bytes", msg)

    # 3 — short PINs refused, lockout after repeated misses
    def test_admin_pin_rules(self):
        st, d = self.post_login("1234")
        self.assertEqual(st, 400)
        self.assertIn("prepperctl set-pin", d["error"])
        server.reset_login_limits()
        codes = [self.post_login("999999")[0] for _ in range(4)]
        self.assertEqual(codes[:3], [403, 403, 403])
        self.assertEqual(codes[3], 429, "locked out after 3 misses")
        st, d = self.post_login("246810")
        self.assertEqual(st, 429, "even the right PIN waits out the lockout")
        server.reset_login_limits()
        st, d = self.post_login("246810")
        self.assertEqual(st, 200)
        self.assertIn(d["token"], server.TOKENS)
        server.TOKENS["expired"] = 1.0
        server.prune_tokens()
        self.assertNotIn("expired", server.TOKENS)

    def post_login(self, pin):
        r, data = self.req("POST", "/api/admin/login", body=json.dumps({"pin": pin}),
                           headers={"Content-Type": "application/json"})
        return r.status, json.loads(data)

    # 11 — kiwix-js only offered when installed; installed-but-no-viewer is visible
    def test_library_renderers(self):
        st, lib = self.jget("/api/library")
        self.assertEqual(st, 200)
        self.assertFalse(server.renderer_available("app:kiwix-js", {}))
        (self.tmp / "data" / "apps" / "kiwix-js").mkdir(parents=True)
        self.assertTrue(server.renderer_available("app:kiwix-js", {}))

    # 17/18 — defaults
    def test_defaults(self):
        c = server.PortalConfig(Path("/nonexistent/prepper.toml"))
        self.assertEqual(c.llama, "http://127.0.0.1:8090/ai")
        self.assertEqual(c.services["llama"], "http://127.0.0.1:8090/ai/health")
        self.assertEqual(str(c.board_file), "/var/lib/prepper/portal/board.jsonl")
        self.assertEqual(str(c.mesh_outbox), "/var/lib/prepper/portal/mesh-outbox.jsonl")
        self.assertEqual(str(c.settings_file), "/var/lib/prepper/portal/portal-settings.json")
        self.assertEqual(str(c.build_file), "/var/lib/prepper/build.json")
        os.environ["AP_IFACE"] = "wlan1"
        try:
            self.assertEqual(server.PortalConfig(Path("/nonexistent/prepper.toml")).ap_iface, "wlan1")
        finally:
            del os.environ["AP_IFACE"]


if __name__ == "__main__":
    unittest.main()
