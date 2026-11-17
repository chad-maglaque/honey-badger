"""
Missing persons and lost & found (software/portal/features/missing.py): posts, edit tokens,
found-safe, name search, photo checks (JPEG structure, metadata stripping, sizes, traversal),
admin moderation, admin-approved mesh announcements with rate limits, expiry, abuse limits.
Runs through the real portal.
Run: python3.11 tests/test_missing.py
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import random
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"

import server  # noqa: E402

FEATS = REPO / "software" / "portal" / "features"


def seg(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def make_jpeg(w=16, h=16, exif=True, comment=True, comps=3, scan=b"\x12\x34" * 60, sof=True, tail=b"\xff\xd9",
              pad=0) -> bytes:
    """A structurally valid JPEG (not decodable; the server only checks structure)."""
    out = b"\xff\xd8" + seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    if exif:
        out += seg(0xE1, b"Exif\x00\x00GPSLatitude=40.7128N;GPSLongitude=74.0060W" + b"\x00" * 16)
        out += seg(0xED, b"Photoshop 3.0\x00secret-iptc")
    if comment:
        out += seg(0xFE, b"secret comment")
    out += seg(0xDB, b"\x00" + bytes(range(64)))
    if sof:
        out += seg(0xC0, b"\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + bytes([comps]) +
                   b"".join(bytes([i + 1, 0x11, 0]) for i in range(comps)))
    out += seg(0xC4, b"\x00" + b"\x01" + b"\x00" * 15 + b"\x00")
    out += seg(0xDA, bytes([comps]) + b"".join(bytes([i + 1, 0]) for i in range(comps)) + b"\x00\x3f\x00")
    return out + scan + b"\x00" * pad + tail


def load_feature():
    spec = importlib.util.spec_from_file_location("missing_unit", FEATS / "missing.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="missing-")).resolve()
        (cls.tmp / "data").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        for f in ("_community_common.py", "missing.py"):
            shutil.copy(FEATS / f, feats / f)
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

[missing]
max_posts = 20
per_ip = 4
posts_per_10min = 3
max_photo_kb = 100
max_photo_px = 2048
resolved_keep_days = 14
mesh = true
mesh_per_hour = 2
board_copy = true
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        assert [f["id"] for f in server.FEATURES] == ["missing"], server.FEATURES
        cls.mod = server.FEATURES[0]["mod"]
        cls.ctx = server.FEATURES[0]["ctx"]
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]
        cls.data = cls.tmp / "features-data" / "missing"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.mod._now = time.time
        self.mod._reset_limits()
        server._last_post.clear()
        self.mod._store(self.ctx).update(lambda d: (d["posts"].clear(), True))
        for f in (self.data / "photos").glob("*"):
            f.unlink()
        for f in ("mesh-outbox.jsonl", "board.jsonl"):
            (self.tmp / f).write_text("")

    @classmethod
    def req(cls, method, path, body=None, admin=None, headers=None, raw=None, ip="10.10.10.50", conn=None):
        c = conn or http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = {"Content-Type": "application/json", "X-Real-IP": ip}
        if admin:
            h["X-Admin-Token"] = admin
        h.update(headers or {})
        c.request(method, path, raw if raw is not None else (json.dumps(body) if body is not None else None), h)
        r = c.getresponse()
        data = r.read()
        hdrs = dict(r.getheaders())
        if conn is None:
            c.close()
        try:
            return r.status, json.loads(data), hdrs
        except ValueError:
            return r.status, data, hdrs

    def add(self, ip="10.10.10.50", **kw):
        body = {"kind": "person", "name": "Ana Ruiz", "description": "8 years old, red jacket", "where": "Elm St",
                "when": "14:00", "contact": "Maria at the school gym"}
        body.update(kw)
        return self.req("POST", "/api/missing/add", body, ip=ip)

    def upload(self, pid, data, token=None, admin=None, ctype="image/jpeg", ip="10.10.10.50", conn=None, extra=None):
        h = {"Content-Type": ctype, "X-Badger-Upload": "1"}
        if token:
            h["X-Edit-Token"] = token
        h.update(extra or {})
        return self.req("POST", f"/api/missing/photo/{pid}", raw=data, headers=h, admin=admin, ip=ip, conn=conn)

    def posts(self, qs="", admin=None):
        return self.req("GET", "/api/missing" + qs, admin=admin)[1]["posts"]

    def outbox(self):
        return [json.loads(x) for x in (self.tmp / "mesh-outbox.jsonl").read_text().splitlines() if x.strip()]


class Posts(Base):
    def test_add_and_list_without_secrets(self):
        st, j, _ = self.add()
        self.assertEqual(st, 200, j)
        p = self.posts()[0]
        self.assertEqual((p["name"], p["status"], p["kind"], p["photo"], p["mesh"]), ("Ana Ruiz", "missing", "person", None, "none"))
        for k in ("tok", "ipt", "flags", "hidden", "mesh_text"):
            self.assertNotIn(k, p)
        raw = (self.data / "posts.json").read_text()
        self.assertNotIn(j["token"], raw); self.assertNotIn("10.10.10.50", raw)
        self.assertEqual(stat.S_IMODE((self.data / "posts.json").stat().st_mode), 0o600)

    def test_name_search_partial_case_accent_insensitive(self):
        self.add(name="José Núñez", description="tall")
        self.add(name="Mary Smith", description="last seen with José", ip="10.0.0.2")
        self.add(kind="item", name="Brown dog Rex", status="lost", description="", ip="10.0.0.3")
        for q, names in (("jose", ["José Núñez", "Mary Smith"]), ("NUÑEZ", ["José Núñez"]), ("nu", ["José Núñez"]),
                         ("smi", ["Mary Smith"]), ("zz", [])):
            got = [p["name"] for p in self.posts("?kind=person&q=" + urllib.parse.quote(q))]
            self.assertEqual(got, names, q)                       # name matches are listed first
        self.assertEqual([p["name"] for p in self.posts("?kind=item&q=rex")], ["Brown dog Rex"])
        self.assertEqual(len(self.posts("?status=lost")), 1)

    def test_found_safe_flow_and_ordering(self):
        a = self.add(name="Ana")[1]
        self.add(name="Ben", ip="10.0.0.2")
        self.assertEqual(self.req("POST", "/api/missing/update", {"id": a["id"], "status": "safe"})[0], 403)
        self.assertEqual(self.req("POST", "/api/missing/update", {"id": a["id"], "token": "x", "status": "safe"})[0], 403)
        st, j, _ = self.req("POST", "/api/missing/update", {"id": a["id"], "token": a["token"], "status": "safe"})
        self.assertEqual((st, j["post"]["status"]), (200, "safe"))
        self.assertEqual([p["name"] for p in self.posts("?kind=person")], ["Ben", "Ana"])       # still missing first
        self.assertEqual(self.req("POST", "/api/missing/update", {"id": a["id"], "token": a["token"], "status": "lost"})[0], 400)
        self.assertEqual(self.req("POST", "/api/missing/update", {"id": a["id"], "status": "closed"}, admin=self.admin)[0], 200)

    def test_cannot_start_resolved_or_with_wrong_status(self):
        self.assertEqual(self.add(status="safe")[0], 400)
        self.assertEqual(self.add(kind="item", status="missing")[0], 400)
        self.assertEqual(self.add(kind="thing")[0], 400)
        self.assertEqual(self.add(kind="item", status="found")[0], 200)

    def test_validation(self):
        bad = [{"name": ""}, {"name": None}, {"name": "x" * 121}, {"name": 1}, {"description": "d" * 1001},
               {"where": ["x"]}, {"when": "w" * 81}, {"contact": {"a": 1}}, {"mesh_request": "yes"}]
        for b in bad:
            self.assertEqual(self.add(**b)[0], 400, b)
        j = self.add(name="‮Ana\x00 Ruiz\x1f")[1]
        self.assertEqual(j["post"]["name"], "Ana Ruiz")
        self.assertEqual(self.req("POST", "/api/missing/update", {"id": "../x", "token": "t"})[0], 404)
        self.assertEqual(self.req("POST", "/api/missing/update", {"id": j["id"], "token": j["token"], "name": "y" * 500})[0], 400)
        self.assertEqual(self.posts()[0]["name"], "Ana Ruiz")

    def test_remove_by_owner_or_admin(self):
        a = self.add()[1]
        b = self.add(name="B", ip="10.0.0.2")[1]
        self.assertEqual(self.req("POST", "/api/missing/remove", {"id": a["id"], "token": b["token"]})[0], 403)
        self.assertEqual(self.req("POST", "/api/missing/remove", {"id": a["id"], "token": a["token"]})[0], 200)
        self.assertEqual(self.req("POST", "/api/missing/moderate", {"id": b["id"], "op": "remove"})[0], 401)
        self.assertEqual(self.req("POST", "/api/missing/moderate", {"id": b["id"], "op": "remove"}, admin=self.admin)[0], 200)
        self.assertEqual(self.posts(), [])

    def test_hide_and_reports(self):
        a = self.add()[1]
        for ip in ("10.9.0.1", "10.9.0.1", "10.9.0.2"):
            self.req("POST", "/api/missing/flag", {"id": a["id"]}, ip=ip)
        self.assertEqual(self.posts(admin=self.admin)[0]["flags"], 2)
        self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "hide"}, admin=self.admin)
        self.assertEqual(self.posts(), [])
        self.assertTrue(self.posts(admin=self.admin)[0]["hidden"])
        self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "unhide"}, admin=self.admin)
        self.assertEqual(len(self.posts()), 1)

    def test_resolved_posts_expire_with_their_photos(self):
        a = self.add()[1]
        b = self.add(name="Still missing", ip="10.0.0.2")[1]
        self.assertEqual(self.upload(a["id"], make_jpeg(), a["token"])[0], 200)
        self.req("POST", "/api/missing/update", {"id": a["id"], "token": a["token"], "status": "safe"})
        real = time.time
        self.mod._now = lambda: real() + 15 * 86400
        self.assertEqual([p["id"] for p in self.posts()], [b["id"]])
        self.assertEqual(list((self.data / "photos").glob("*.jpg")), [])

    def test_rate_limit_and_open_post_cap(self):
        for i in range(3):
            self.assertEqual(self.add(name=f"p{i}", ip="10.7.0.1")[0], 200)
        self.assertEqual(self.add(ip="10.7.0.1")[0], 429)
        self.mod._reset_limits()
        self.assertEqual(self.add(name="p3", ip="10.7.0.1")[0], 200)
        self.mod._reset_limits()
        st, j, _ = self.add(ip="10.7.0.1")
        self.assertEqual(st, 429); self.assertIn("At most 4", j["error"])

    def test_total_cap(self):
        for i in range(20):
            self.assertEqual(self.add(name=f"p{i}", ip=f"10.8.0.{i}")[0], 200)
        self.assertEqual(self.add(ip="10.8.1.1")[0], 507)

    def test_concurrent_status_updates_keep_valid_file(self):
        a = self.add()[1]

        def go(i):
            self.req("POST", "/api/missing/update", {"id": a["id"], "token": a["token"],
                                                     "status": ("missing", "safe")[i % 2]}, ip=f"10.6.0.{i}")
        ts = [threading.Thread(target=go, args=(i,)) for i in range(20)]
        [t.start() for t in ts]; [t.join() for t in ts]
        d = json.loads((self.data / "posts.json").read_text())
        self.assertEqual(len(d["posts"]), 1)
        self.assertIn(d["posts"][0]["status"], ("missing", "safe"))


class Photos(Base):
    def test_upload_strips_metadata_and_serves_jpeg(self):
        a = self.add()[1]
        st, j, _ = self.upload(a["id"], make_jpeg(640, 480), a["token"])
        self.assertEqual(st, 200, j)
        self.assertEqual((j["width"], j["height"]), (640, 480))
        url = self.posts()[0]["photo"]
        self.assertEqual(url, j["photo"])
        st, body, h = self.req("GET", url)
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "image/jpeg")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertTrue(body.startswith(b"\xff\xd8\xff\xe0") and body.endswith(b"\xff\xd9"))
        for secret in (b"Exif", b"GPS", b"secret", b"Photoshop"):
            self.assertNotIn(secret, body)
        files = list((self.data / "photos").glob("*.jpg"))
        self.assertEqual(len(files), 1)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)

    def test_upload_needs_owner_token_or_admin(self):
        a = self.add()[1]
        b = self.add(name="B", ip="10.0.0.2")[1]
        self.assertEqual(self.upload(a["id"], make_jpeg())[0], 403)
        self.assertEqual(self.upload(a["id"], make_jpeg(), b["token"])[0], 403)
        self.assertEqual(self.upload(a["id"], make_jpeg(), admin=self.admin)[0], 200)
        self.assertEqual(self.upload("nosuchpost1", make_jpeg(), a["token"])[0], 404)
        self.assertEqual(self.upload("../../etc", make_jpeg(), a["token"])[0], 404)

    def test_csrf_and_type_rules(self):
        a = self.add()[1]
        st = self.req("POST", f"/api/missing/photo/{a['id']}", raw=make_jpeg(),
                      headers={"Content-Type": "image/jpeg", "X-Edit-Token": a["token"]})[0]
        self.assertEqual(st, 415)                                    # no X-Badger-Upload header
        st = self.upload(a["id"], make_jpeg(), a["token"], extra={"Origin": "http://evil.example"})[0]
        self.assertEqual(st, 403)
        self.assertEqual(self.upload(a["id"], make_jpeg(), a["token"], ctype="image/svg+xml")[0], 415)
        self.assertEqual(self.upload(a["id"], make_jpeg(), a["token"], ctype="text/html")[0], 415)

    def test_bad_files_are_refused(self):
        a = self.add()[1]
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        html = b"<html><script>alert(1)</script></html>" + b" " * 200
        bad = [png, html, make_jpeg(tail=b""), make_jpeg(sof=False), make_jpeg(w=4000, h=10), make_jpeg(w=0),
               make_jpeg(comps=4), make_jpeg()[:60] + b"\xff\xd9", b"\xff\xd8\xff" + b"\x00" * 200 + b"\xff\xd9",
               make_jpeg(tail=b"\xff\xd9<script>")]
        for i, data in enumerate(bad):
            st, j, _ = self.upload(a["id"], data, a["token"])
            self.assertEqual(st, 400, (i, j))
        self.assertEqual(list((self.data / "photos").glob("*")), [])
        st, j, _ = self.upload(a["id"], make_jpeg(pad=150_000), a["token"])      # over max_photo_kb = 100
        self.assertEqual(st, 413)
        # over the route cap: refused from the headers alone, before any of the body is read
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sock.sendall((f"POST /api/missing/photo/{a['id']} HTTP/1.1\r\nHost: x\r\nContent-Type: image/jpeg\r\n"
                          f"X-Badger-Upload: 1\r\nX-Edit-Token: {a['token']}\r\nContent-Length: 1300000\r\n\r\n").encode())
            self.assertIn(b" 413 ", sock.recv(4096).split(b"\r\n")[0] + b" ")
        finally:
            sock.close()

    def test_rejected_upload_does_not_poison_keepalive(self):
        a = self.add()[1]
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            st, _, _ = self.upload(a["id"], make_jpeg(pad=5000), "wrong-token", conn=c)
            self.assertEqual(st, 403)
            st, j, _ = self.req("GET", "/api/missing", conn=c)
            self.assertEqual(st, 200); self.assertEqual(len(j["posts"]), 1)
        finally:
            c.close()

    def test_replace_and_remove_photo_cleans_up(self):
        a = self.add()[1]
        self.upload(a["id"], make_jpeg(), a["token"])
        first = list((self.data / "photos").glob("*.jpg"))
        self.upload(a["id"], make_jpeg(32, 32), a["token"])
        second = list((self.data / "photos").glob("*.jpg"))
        self.assertEqual(len(second), 1); self.assertNotEqual(first, second)
        self.assertEqual(self.req("POST", "/api/missing/photo-remove", {"id": a["id"], "token": "x"})[0], 403)
        self.assertEqual(self.req("POST", "/api/missing/photo-remove", {"id": a["id"], "token": a["token"]})[0], 200)
        self.assertEqual(list((self.data / "photos").glob("*.jpg")), [])
        self.upload(a["id"], make_jpeg(), a["token"])
        self.req("POST", "/api/missing/remove", {"id": a["id"], "token": a["token"]})
        self.assertEqual(list((self.data / "photos").glob("*.jpg")), [])

    def test_photo_paths_cannot_escape(self):
        a = self.add()[1]
        self.upload(a["id"], make_jpeg(), a["token"])
        (self.data / "posts.jpg").write_bytes(b"x")
        for path in ("/api/missing/photo/../posts.json", "/api/missing/photo/..%2fposts.json", "/api/missing/photo/posts.jpg",
                     "/api/missing/photo/" + "a" * 40 + ".jpg", "/api/missing/photo/abc", "/api/missing/photo/",
                     "/api/missing/photo/%2e%2e%2f%2e%2e%2fposts.jpg"):
            self.assertEqual(self.req("GET", path)[0], 404, path)

    def test_hidden_post_photo_only_for_admin(self):
        a = self.add()[1]
        url = self.upload(a["id"], make_jpeg(), a["token"])[1]["photo"]
        self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "hide"}, admin=self.admin)
        self.assertEqual(self.req("GET", url)[0], 404)
        self.assertEqual(self.req("GET", url, admin=self.admin)[0], 200)

    def test_concurrent_uploads_leave_one_file(self):
        a = self.add()[1]

        def go(i):
            self.upload(a["id"], make_jpeg(16 + i, 16), a["token"], ip=f"10.5.0.{i}")
        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        files = list((self.data / "photos").glob("*.jpg"))
        self.assertEqual(len(files), 1)
        self.assertEqual(self.req("GET", self.posts()[0]["photo"])[0], 200)

    def test_photo_rate_limit(self):
        a = self.add()[1]
        for _ in range(10):
            self.assertEqual(self.upload(a["id"], make_jpeg(), a["token"])[0], 200)
        self.assertEqual(self.upload(a["id"], make_jpeg(), a["token"])[0], 429)


class Mesh(Base):
    """Public-channel announcements: the Neighbourly posture (Guarded/Lockdown: tests/test_missing_posture.py)."""

    def setUp(self):
        super().setUp()
        (self.tmp / "posture.json").write_text(json.dumps({"level": "neighbourly"}))
        server._cache.pop("posture", None)
        self.addCleanup(lambda: ((self.tmp / "posture.json").unlink(missing_ok=True), server._cache.pop("posture", None)))

    def test_request_then_admin_approves(self):
        a = self.add(mesh_request=True)[1]
        self.assertEqual(a["post"]["mesh"], "requested")
        self.assertEqual(self.outbox(), [])                         # nothing on air without approval
        adm = self.req("GET", "/api/missing", admin=self.admin)[1]
        self.assertEqual(adm["mesh_pending"], 1)
        text = adm["posts"][0]["mesh_text"]
        self.assertEqual(text, "MISSING: Ana Ruiz, 8 years old, red jacket, last seen Elm St 14:00 — see the community board")
        # (CHANGED, red team finding 6: the tail used to be "see Badger board" — nothing on air names the device)
        self.assertEqual(self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "mesh_send"})[0], 401)
        st, j, _ = self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "mesh_send"}, admin=self.admin)
        self.assertEqual(st, 200, j)
        out = self.outbox()
        # CHANGED (mesh courtesy): kind, dedupe key (post id + status) and queue time ride along
        self.assertEqual(len(out), 1)
        self.assertEqual({k: out[0][k] for k in ("text", "board", "kind", "dedupe")},
                         {"text": text, "board": "community", "kind": "community", "dedupe": f"missing:{a['id']}:missing"})
        self.assertIsInstance(out[0]["at"], int)
        board = (self.tmp / "board.jsonl").read_text()
        self.assertIn("MISSING: Ana Ruiz", board)
        self.assertEqual(self.posts()[0]["mesh"], "sent")

    def test_found_safe_after_announcement_queues_followup(self):
        a = self.add()[1]
        self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "mesh_send"}, admin=self.admin)
        self.req("POST", "/api/missing/update", {"id": a["id"], "token": a["token"], "status": "safe"})
        p = self.posts(admin=self.admin)[0]
        self.assertEqual((p["mesh"], p["mesh_text"]), ("requested", "FOUND SAFE: Ana Ruiz — see the community board"))
        self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "mesh_send"}, admin=self.admin)
        self.assertEqual(self.outbox()[-1]["text"], "FOUND SAFE: Ana Ruiz — see the community board")

    def test_airtime_rate_limit_and_custom_text(self):
        ids = [self.add(name=f"P{i}", ip=f"10.4.0.{i}")[1]["id"] for i in range(3)]
        self.assertEqual(self.req("POST", "/api/missing/moderate", {"id": ids[0], "op": "mesh_send", "text": "MISSING: P0 short"},
                                  admin=self.admin)[0], 200)
        self.assertEqual(self.req("POST", "/api/missing/moderate", {"id": ids[1], "op": "mesh_send"}, admin=self.admin)[0], 200)
        st, j, _ = self.req("POST", "/api/missing/moderate", {"id": ids[2], "op": "mesh_send"}, admin=self.admin)
        self.assertEqual(st, 429); self.assertIn("airtime", j["error"])
        self.assertEqual(len(self.outbox()), 2)
        self.assertEqual(self.outbox()[0]["text"], "MISSING: P0 short")
        st, _, _ = self.req("POST", "/api/missing/moderate", {"id": ids[2], "op": "mesh_send", "text": "é" * 150}, admin=self.admin)
        self.assertEqual(st, 400)                                   # 300 bytes: too long for the radio

    def test_reject_closed_and_hidden(self):
        a = self.add(mesh_request=True)[1]
        self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "mesh_reject"}, admin=self.admin)
        self.assertEqual(self.posts()[0]["mesh"], "rejected")
        self.req("POST", "/api/missing/update", {"id": a["id"], "token": a["token"], "status": "closed"})
        self.assertEqual(self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "mesh_send"}, admin=self.admin)[0], 400)
        b = self.add(name="B", ip="10.0.0.2")[1]
        self.req("POST", "/api/missing/moderate", {"id": b["id"], "op": "hide"}, admin=self.admin)
        self.assertEqual(self.req("POST", "/api/missing/moderate", {"id": b["id"], "op": "mesh_send"}, admin=self.admin)[0], 400)
        self.assertEqual(self.outbox(), [])

    def test_owner_can_request_later_but_only_once_per_status(self):
        a = self.add()[1]
        self.req("POST", "/api/missing/update", {"id": a["id"], "token": a["token"], "mesh_request": True})
        self.assertEqual(self.posts()[0]["mesh"], "requested")
        self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "mesh_send"}, admin=self.admin)
        self.req("POST", "/api/missing/update", {"id": a["id"], "token": a["token"], "mesh_request": True})
        self.assertEqual(self.posts()[0]["mesh"], "sent")           # same news isn't re-requested


class Units(unittest.TestCase):
    M = load_feature()

    def test_mesh_text_fits_200_bytes(self):
        m = self.M
        p = {"kind": "person", "status": "missing", "name": "Ñ" * 100, "description": "ü" * 900,
             "where": "Øster " * 50, "when": "today"}
        t = m.mesh_text(p)
        self.assertLessEqual(len(t.encode()), 200)
        self.assertTrue(t.startswith("MISSING: Ñ")); self.assertTrue(t.endswith("see the community board"))
        self.assertIsNone(m.mesh_text({"status": "closed", "name": "x"}))
        self.assertEqual(m.mesh_text({"kind": "item", "status": "found", "name": "Keys", "where": "Park"}),
                         "FOUND: Keys, found at Park — see the community board")

    def test_clean_jpeg_fuzz_only_raises_field_errors(self):
        m = self.M
        rnd = random.Random(7)
        base = make_jpeg()
        for _ in range(3000):
            b = bytearray(base)
            for _ in range(rnd.randint(1, 6)):
                b[rnd.randrange(len(b))] = rnd.randrange(256)
            if rnd.random() < 0.3:
                b = b[:rnd.randrange(len(b))]
            try:
                out, w, h = m.clean_jpeg(bytes(b), 2048)
                self.assertTrue(out.startswith(b"\xff\xd8"))
            except m.C.FieldError:
                pass

    @unittest.skipUnless(shutil.which("sips"), "needs macOS sips to make a real JPEG")
    def test_real_encoder_output_is_accepted(self):
        d = Path(tempfile.mkdtemp(prefix="jpg-"))
        try:
            out = d / "x.jpg"
            subprocess.run(["sips", "-s", "format", "jpeg", str(REPO / "software/portal/static/icon-192.png"),
                            "--out", str(out)], check=True, capture_output=True)
            data, w, h = self.M.clean_jpeg(out.read_bytes(), 2048)
            self.assertEqual((w, h), (192, 192))
            self.assertTrue(data.endswith(b"\xff\xd9"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
