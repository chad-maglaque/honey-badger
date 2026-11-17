"""
Adversarial tests for the portal over HTTP: the two boards, /api/members/*, /api/mesh/nodes,
/api/local/invite, admin member ops (Wi-Fi/mesh rotation plumbing, mesh_commands), CSRF/CORS,
client_ip()/local_console(), plus a static scan of app.js / maps for unescaped server data.

Failing tests are intentional: each one marked `# BUG:` documents a defect (expected vs actual).
Run: python3.11 tests/test_adv_boards_http.py
"""
from __future__ import annotations

import email.message
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"
import members  # noqa: E402
import server  # noqa: E402

FAKE_CTL = r'''#!/bin/sh
mode=$(cat "{d}/ctl-mode" 2>/dev/null)
case "$1" in
  wifi-rotate)
    echo "passphrase: ABCD-EFGH-JKMN"; echo "confirm-within: 600"
    if [ "$mode" = noundo ]; then echo "warning: the automatic undo could not be scheduled — confirm or undo by hand"; fi ;;
  wifi-status)
    if [ "$mode" = statusfail ]; then echo "Traceback: json.decoder.JSONDecodeError" >&2; exit 1; fi
    if [ "$mode" = statuslist ]; then echo '[1, 2]'; exit 0; fi
    if [ "$mode" = notapplied ]; then echo '{{"pending": true, "applied": false, "seconds_left": 600}}'; exit 0; fi
    echo '{{"pending": true, "seconds_left": 412}}' ;;
  wifi-confirm) echo "kept the new Wi-Fi password" ;;
  wifi-revert) echo "switched back to the previous Wi-Fi password" ;;
  *) echo "unexpected $1"; exit 2 ;;
esac
'''


class Portal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="adv-boards-")).resolve()
        (cls.tmp / "data").mkdir()
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
mesh_nodes = "{cls.tmp}/mesh-nodes.json"
mesh_rotate_request = "{cls.tmp}/mesh-rotate.json"
mesh_rotate_result = "{cls.tmp}/mesh-rotate-result.json"
mesh_channel_link = "{cls.tmp}/mesh-channel.json"
ctl = "{cls.tmp}/fake-prepperctl"
services = {{}}
""")
        fake = cls.tmp / "fake-prepperctl"
        fake.write_text(FAKE_CTL.format(d=cls.tmp))
        fake.chmod(0o755)
        server.CFG = server.PortalConfig(cfg)
        server._members = None
        server.reset_login_limits()
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        server._last_post.clear()
        (self.tmp / "ctl-mode").write_text("")

    @classmethod
    def raw_req(cls, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=15)
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = body if isinstance(body, (bytes, str)) or body is None else json.dumps(body)
        c.request(method, path, data, h)
        r = c.getresponse()
        raw = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, raw, hdrs

    @classmethod
    def req(cls, method, path, body=None, member=None, admin=None, headers=None):
        h = dict(headers or {})
        if member:
            h["X-Member-Token"] = member
        if admin:
            h["X-Admin-Token"] = admin
        st, raw, _ = cls.raw_req(method, path, body, h)
        try:
            return st, json.loads(raw)
        except ValueError:
            return st, {}

    def admin_op(self, body):
        return self.req("POST", "/api/admin/members", body, admin=self.admin)

    def join(self, name):
        st, inv = self.admin_op({"op": "invite", "name": name})
        self.assertEqual(st, 200, inv)
        st, j = self.req("POST", "/api/members/join", {"code": inv["code"], "name": "", "device": "phone"})
        self.assertEqual(st, 200, j)
        return j["key"]

    def member_id(self, name):
        return [m["id"] for m in self.admin_op({"op": "list"})[1]["members"] if m["name"] == name and not m["removed"]][0]

    def post(self, body, member=None, ip=None):
        server._last_post.clear()
        return self.req("POST", "/api/board", body, member=member, headers={"X-Real-IP": ip} if ip else None)

    def outbox(self):
        p = self.tmp / "mesh-outbox.jsonl"
        return p.read_text().splitlines() if p.exists() else []


# ───────────────────────────── family board access ─────────────────────────────

class FamilyAccess(Portal):
    def test_family_board_needs_a_live_member_key(self):
        ana = self.join("AnaF")
        self.assertEqual(self.post({"board": "family", "text": "family secret 1"}, member=ana)[0], 200)
        gone = self.join("GoneF")
        self.admin_op({"op": "remove", "id": self.member_id("GoneF")})
        for tok in (None, "", " ", "x" * 50, "x" * 200, ana + "x", ana[:-1], gone, "null", "undefined"):
            with self.subTest(tok=repr(tok)[:20]):
                st, b = self.req("GET", "/api/board?board=family", member=tok)
                self.assertEqual(st, 401)
                self.assertNotIn("family secret", json.dumps(b))
                self.assertEqual(self.post({"board": "family", "text": "x"}, member=tok)[0], 401)

    def test_query_string_variants_never_leak_the_family_board(self):
        ana = self.join("AnaQ")
        self.post({"board": "family", "text": "family secret Q"}, member=ana)
        for qs in ("board=family", "board=Family", "board=FAMILY", "board=family%20", "board=%66amily",
                   "board=family&board=community", "board=community&board=family", "board[]=family",
                   "board=family%00", "board=..%2Ffamily", "board=family/../community", "BOARD=family",
                   "board=fam%C4%B1ly", "board=%EF%BD%86amily"):
            with self.subTest(qs=qs):
                st, b = self.req("GET", "/api/board?" + qs)
                self.assertNotIn("family secret", json.dumps(b))
                self.assertNotEqual(st, 500)

    def test_post_body_board_types(self):
        ana = self.join("AnaB")
        for board in (["family"], {"family": 1}, 1, 0, None, True, "family\x00", " family", "FAMILY"):
            with self.subTest(board=repr(board)):
                st, r = self.post({"board": board, "text": f"anon {board!r}"})
                self.assertNotEqual(st, 500, r)
        fam = (self.tmp / "family-board.jsonl")
        self.assertNotIn("anon", fam.read_text() if fam.exists() else "")
        st, r = self.post({"board": "family", "text": "ok", "name": "Mallory"}, member=ana)
        self.assertEqual(st, 200)

    def test_name_cannot_be_spoofed_by_a_member_on_either_board(self):
        ana = self.join("AnaN")
        for board in ("family", "community"):
            self.post({"board": board, "text": f"spoof on {board}", "name": "Admin"}, member=ana)
            msgs = self.req("GET", f"/api/board?board={board}", member=ana)[1]["messages"]
            rec = [m for m in msgs if m["text"] == f"spoof on {board}"][0]
            self.assertEqual(rec["name"], "AnaN")

    def test_visitors_never_see_membership_marks(self):
        ana = self.join("AnaV")
        self.post({"text": "member on community"}, member=ana)
        with open(self.tmp / "board.jsonl", "a") as fh:        # as meshbridge writes it
            fh.write(json.dumps({"at": "x", "name": "AnaV", "text": "via radio", "via": "mesh-in",
                                 "radio": "!a1b2c3d4", "verified": True, "member": "m_123"}) + "\n")
        st, b = self.req("GET", "/api/board")
        for m in b["messages"]:
            self.assertNotIn("verified", m)
            self.assertNotIn("member", m)
            self.assertNotIn("removed_member", m)
        mine = self.req("GET", "/api/board", member=ana)[1]["messages"]
        self.assertTrue(all("member" not in m for m in mine))  # member ids never leave the server

    def test_members_me_and_mesh_nodes(self):
        ana = self.join("AnaM")
        self.admin_op({"op": "add_radio", "id": self.member_id("AnaM"), "radio": "!0badf00d"})
        st, r = self.req("GET", "/api/members/me", member=ana)
        self.assertEqual(set(r["member"]), {"name", "radios"})
        self.assertEqual(r["member"]["radios"], ["!0badf00d"])
        for tok in (None, "bogus"):
            self.assertEqual(self.req("GET", "/api/mesh/nodes", member=tok)[0], 401)
        self.assertEqual(self.req("GET", "/api/mesh/nodes", admin="bogus")[0], 401)
        self.assertEqual(self.req("GET", "/api/mesh/nodes", member=ana)[0], 200)


# ───────────────────────────── content / injection ─────────────────────────────

class Injection(Portal):
    XSS = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "</script><svg onload=alert(1)>",
           "javascript:alert(1)", "&lt;b&gt;", "  ", "{{constructor.constructor('alert(1)')()}}"]

    def test_stored_xss_payloads_come_back_json_escaped(self):
        ana = self.join("<b>Ana</b>")                         # a member name with markup
        for i, p in enumerate(self.XSS):
            self.post({"name": p, "text": p + f" #{i}"}, ip=f"10.5.0.{i}")
        self.post({"board": "family", "text": self.XSS[1]}, member=ana)
        for path, tok in (("/api/board", None), ("/api/board?board=family", ana), ("/api/members/me", ana)):
            st, raw, hdrs = self.raw_req("GET", path, headers={"X-Member-Token": tok} if tok else None)
            self.assertEqual(st, 200)
            self.assertEqual(hdrs.get("Content-Type"), "application/json")
            self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")
            self.assertNotRegex(raw, rb"[<>&]")
        msgs = self.req("GET", "/api/board")[1]["messages"]
        for p in self.XSS:
            self.assertTrue(any(m["text"].startswith(p.strip()) for m in msgs), p)   # stored verbatim, not mangled
        st, raw, _ = self.raw_req("POST", "/api/admin/members", {"op": "list"}, {"X-Admin-Token": self.admin})
        self.assertNotRegex(raw, rb"[<>&]")

    def test_outbox_is_one_json_line_per_post(self):
        before = len(self.outbox())
        nasty = ["line1\nline2", "a\r\nb", "x y z", "q\u0085r", '{"text":"inj","board":"family"}\n{"x":1}',
                 "tab\there"]
        for i, t in enumerate(nasty):
            st, r = self.post({"name": "N\n{\"board\":\"family\"}", "text": t, "mesh": True}, ip=f"10.6.0.{i}")
            self.assertEqual(st, 200, r)
        lines = self.outbox()[before:]
        self.assertEqual(len(lines), len(nasty))
        for ln in lines:
            rec = json.loads(ln)
            self.assertEqual(rec["board"], "community")         # a visitor can't steer a post onto the family channel

    def test_mesh_byte_limit_counts_multibyte_member_names(self):
        key = self.join("😀" * 32)                              # 128 bytes of name
        st, r = self.post({"board": "family", "text": "a" * 70, "mesh": True}, member=key)   # 128 + 2 + 70 = 200
        self.assertEqual(st, 200, r)
        st, r = self.post({"board": "family", "text": "a" * 71, "mesh": True}, member=key)
        self.assertEqual(st, 400)
        st, r = self.post({"text": "é" * 99, "name": "ab", "mesh": True})                   # 2+2+198 = 202
        self.assertEqual(st, 400)
        self.assertLessEqual(max(len(json.loads(x)["text"].encode()) for x in self.outbox()), 200)

    def test_mesh_flag_must_be_a_real_boolean(self):
        # BUG: POST /api/board uses bool(data.get("mesh")), so {"mesh": "false"}, {"mesh": "no"}, {"mesh": 0.1},
        # {"mesh": [0]} all send the post over LoRa (scarce airtime, broadcast to strangers on the public
        # channel). /api/ask uses `is True` for its flag; the board should too.
        # Expected: only JSON true queues a mesh message. Actual: any truthy value does.
        # Source: software/portal/server.py route_post /api/board (bool(data.get("mesh"))).
        before = len(self.outbox())
        for i, v in enumerate(("false", "no", "0", [0], {"no": 1})):
            self.post({"name": "x", "text": f"not for the radio {i}", "mesh": v}, ip=f"10.7.0.{i}")
        self.assertEqual(self.outbox()[before:], [])

    def test_lone_surrogates_are_a_client_error_not_a_500(self):
        # BUG: JSON allows "\ud800" escapes; the portal passes the resulting lone-surrogate str to
        # .encode() (members._h for invite codes, the mesh byte check in board_post) and answers
        # 500 "internal error" with a traceback in the log. Expected: 400/403. Actual: 500.
        # Source: software/portal/members.py _h(); software/portal/server.py board_post (.encode()).
        body = b'{"code": "\\ud800ABCD", "name": "x", "device": "y"}'
        st, raw, _ = self.raw_req("POST", "/api/members/join", body)
        self.assertNotEqual(st, 500, raw)
        body = b'{"name": "x", "text": "hi \\udfff", "mesh": true}'
        server._last_post.clear()
        st, raw, _ = self.raw_req("POST", "/api/board", body, {"X-Real-IP": "10.7.9.9"})
        self.assertNotEqual(st, 500, raw)


# ───────────────────────────── rate limits / client identity ─────────────────────────────

class FakeHandler:
    """Just enough of a request for Handler.client_ip / local_console."""
    client_ip = server.Handler.client_ip
    local_console = server.Handler.local_console
    is_local_console = server.Handler.is_local_console

    def __init__(self, addr, headers):
        self.client_address = (addr, 12345)
        self.headers = email.message.Message()
        for k, v in headers.items():
            self.headers[k] = v


class Identity(Portal):
    def test_x_real_ip_only_trusted_from_loopback(self):
        for addr in ("10.42.0.17", "192.168.4.2", "fe80::1", "::ffff:127.0.0.1", "127.0.0.2"):
            with self.subTest(addr=addr):
                h = FakeHandler(addr, {"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"})
                self.assertEqual(h.client_ip(), addr)
                self.assertFalse(h.local_console())
        self.assertEqual(FakeHandler("127.0.0.1", {"x-real-ip": " 9.9.9.9 "}).client_ip(), "9.9.9.9")
        self.assertEqual(FakeHandler("::1", {"X-Real-IP": "9.9.9.9"}).client_ip(), "9.9.9.9")

    def test_local_console_header_variants(self):
        self.assertTrue(FakeHandler("127.0.0.1", {}).local_console())
        self.assertTrue(FakeHandler("::1", {}).local_console())
        for hdrs in ({"x-real-ip": "10.0.0.2"}, {"X-REAL-IP": "10.0.0.2"}, {"x-forwarded-for": "10.0.0.2"},
                     {"X-Forwarded-For": "10.0.0.2", "X-Real-IP": "10.0.0.2"}):
            with self.subTest(h=hdrs):
                self.assertFalse(FakeHandler("127.0.0.1", hdrs).local_console())

    def test_board_flood_limit_per_client(self):
        self.assertEqual(self.req("POST", "/api/board", {"text": "one"}, headers={"X-Real-IP": "10.9.9.1"})[0], 200)
        st, r = self.req("POST", "/api/board", {"text": "two"}, headers={"X-Real-IP": "10.9.9.1"})
        self.assertEqual(st, 400)
        self.assertIn("slow down", r["message"])
        ana = self.join("AnaR")                                  # the family board shares the per-client brake
        st, r = self.req("POST", "/api/board", {"board": "family", "text": "three"}, member=ana,
                         headers={"X-Real-IP": "10.9.9.1"})
        self.assertEqual(st, 400)

    def test_parallel_posts_from_one_client_only_one_lands(self):
        server._last_post.clear()
        res, barrier = [], threading.Barrier(20)

        def go(i):
            barrier.wait()
            res.append(self.req("POST", "/api/board", {"text": f"race {i}"}, headers={"X-Real-IP": "10.9.8.1"})[0])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(res.count(200), 1, res)

    def test_parallel_http_joins_on_one_code(self):
        st, inv = self.admin_op({"op": "invite", "name": "Racer"})
        res, barrier = [], threading.Barrier(16)

        def go(i):
            barrier.wait()
            res.append(self.req("POST", "/api/members/join", {"code": inv["code"], "name": f"R{i}", "device": "d"},
                                headers={"X-Real-IP": f"10.11.0.{i}"})[0])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(res.count(200), 1, res)


# ───────────────────────────── CSRF / CORS ─────────────────────────────

class CrossOrigin(Portal):
    ENDPOINTS = [("/api/members/join", {"code": "AAAA-BBBB-CC", "name": "x"}), ("/api/members/leave", {}),
                 ("/api/board", {"text": "csrf"}), ("/api/admin/members", {"op": "invite"}),
                 ("/api/local/invite", {}), ("/api/admin/settings", {"mesh_commands": {"ai": "community"}}),
                 ("/api/admin/login", {"pin": "246810"})]

    def test_cross_origin_posts_refused(self):
        variants = [{"Origin": "http://evil.example"}, {"Origin": "null"}, {"Origin": "file://"},
                    {"Origin": f"http://127.0.0.1:{self.port}.evil.example"},
                    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "CROSS-SITE "},
                    {"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"},
                    {"Content-Type": "multipart/form-data; boundary=x"}]
        before = (self.tmp / "board.jsonl").read_text() if (self.tmp / "board.jsonl").exists() else ""
        for path, body in self.ENDPOINTS:
            for v in variants:
                with self.subTest(path=path, v=v):
                    h = {"X-Admin-Token": self.admin}
                    h.update(v)
                    st, raw, hdrs = self.raw_req("POST", path, body, h)
                    self.assertIn(st, (403, 415), raw)
                    self.assertNotIn("Access-Control-Allow-Origin", hdrs)
        after = (self.tmp / "board.jsonl").read_text() if (self.tmp / "board.jsonl").exists() else ""
        self.assertEqual(before, after)

    def test_preflight_never_grants_tokens_or_post(self):
        st, raw, hdrs = self.raw_req("OPTIONS", "/api/board", None, {
            "Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-member-token, x-admin-token, content-type"})
        allow_h = hdrs.get("Access-Control-Allow-Headers", "").lower()
        allow_m = hdrs.get("Access-Control-Allow-Methods", "").upper()
        for bad in ("x-member-token", "x-admin-token", "content-type", "*"):
            self.assertNotIn(bad, allow_h)
        self.assertNotIn("POST", allow_m)
        self.assertNotIn("Access-Control-Allow-Credentials", hdrs)

    def test_post_replies_never_carry_cors(self):
        for path, body in self.ENDPOINTS:
            st, raw, hdrs = self.raw_req("POST", path, body, {"X-Admin-Token": self.admin})
            self.assertNotIn("Access-Control-Allow-Origin", hdrs, path)


# ───────────────────────────── /api/local/invite ─────────────────────────────

class LocalInvite(Portal):
    def tearDown(self):
        p = self.tmp / "members.json"
        if p.exists():                                        # drop open invites so other tests can invite
            d = json.loads(p.read_text()); d["invites"] = {}; p.write_text(json.dumps(d))
        server._members = None                                # …and the panel's invite rate counter

    def test_only_direct_loopback_without_proxy_headers(self):
        self.assertEqual(self.req("POST", "/api/local/invite", {})[0], 200)
        for h in ({"X-Real-IP": "10.0.0.9"}, {"x-real-ip": "10.0.0.9"}, {"X-Forwarded-For": "10.0.0.9"},
                  {"x-forwarded-for": "127.0.0.1"}, {"X-Real-IP": "127.0.0.1"}):
            with self.subTest(h=h):
                self.assertEqual(self.req("POST", "/api/local/invite", {}, headers=h)[0], 403)
        st, raw, _ = self.raw_req("GET", "/api/local/invite")
        self.assertEqual(st, 404)

    def test_front_panel_cannot_exhaust_invites_and_lock_out_the_admin(self):
        # BUG: /api/local/invite has no rate limit and its invites share the MAX_INVITES=20 pool with the
        # admin's. Anyone who can press the knob (or any local process/service account on the Pi —
        # loopback is the only check) can mint 20 codes in a second; the admin page then gets "too many
        # open invites" for 24 h, and there is no admin operation to list or cancel open invites.
        # Expected: the admin can still invite (separate pool / cancel op / panel rate limit).
        # Actual: 400 "too many open invites". Source: server.py route_post /api/local/invite;
        # members.py create_invite (shared MAX_INVITES, no cancel).
        # FIXED: the panel has its own small pool (PANEL_MAX_INVITES, also rate-limited); the admin can
        # list and cancel open invites. (This test used to expect exactly MAX_INVITES panel invites,
        # i.e. the shared pool; it now checks the panel's own cap.)
        codes = [self.req("POST", "/api/local/invite", {})[0] for _ in range(members.MAX_INVITES + 5)]
        self.assertEqual(codes.count(200), members.PANEL_MAX_INVITES)
        st, r = self.admin_op({"op": "invite", "name": "Grandma"})
        self.assertEqual(st, 200, r)
        st, lst = self.admin_op({"op": "list"})
        panel = [i for i in lst["invites"] if i["source"] == "panel"]
        self.assertEqual(len(panel), members.PANEL_MAX_INVITES)
        self.assertEqual(self.admin_op({"op": "cancel_invite", "invite": panel[0]["id"]})[1]["cancelled"], 1)
        left = len(panel) - 1 + 1                             # the other panel invites + Grandma's
        self.assertEqual(self.admin_op({"op": "cancel_invite", "all": True})[1]["cancelled"], left)
        self.assertEqual(self.admin_op({"op": "list"})[1]["invites"], [])


# ───────────────────────────── admin ops ─────────────────────────────

class AdminOps(Portal):
    def test_member_ops_with_hostile_ids_and_values(self):
        for body in ({"op": "remove", "id": "../../etc"}, {"op": "rename", "id": "m_nope", "name": "x"},
                     {"op": "revoke_device", "id": None}, {"op": "add_radio", "id": ["x"], "radio": "!a1b2c3d4"},
                     {"op": ["list"]}, {"op": None}, {"op": "invite", "id": "m_nope"},
                     {"op": "rotate_mesh_status", "rid": {"x": 1}}, {"op": "rename", "id": "", "name": ""}):
            with self.subTest(body=body):
                st, r = self.admin_op(body)
                self.assertIn(st, (200, 400), r)

    def test_settings_mesh_commands_with_unhashable_values(self):
        # BUG: handle_admin("settings") filters with `v in levels` where levels is a set, so a JSON list or
        # object as a level value ({"ai": ["community"]}) raises TypeError: unhashable type → 500.
        # Expected: the bad entry ignored (200) like other bad values. Actual: 500 internal error.
        # Source: software/portal/server.py handle_admin settings (`v in levels`).
        st, r = self.req("POST", "/api/admin/settings", {"mesh_commands": {"ai": ["community"], "q": {"a": 1}}},
                         admin=self.admin)
        self.assertEqual(st, 200, r)
        st, r = self.req("POST", "/api/admin/settings", {"mesh_commands": {"ai": "community", "help": "off",
                                                                           "AI": "family", "!q": "off"}},
                         admin=self.admin)
        self.assertEqual(st, 200)
        self.assertEqual(r["settings"]["mesh_commands"].get("help"), "community")

    def test_mesh_commands_only_known_levels_reach_the_file(self):
        self.req("POST", "/api/admin/settings", {"mesh_commands": {"ai": "Community", "status": "everyone",
                                                                   "q": None, "__proto__": "community"}},
                 admin=self.admin)
        saved = json.loads((self.tmp / "portal-settings.json").read_text()).get("mesh_commands", {})
        self.assertTrue(set(saved) <= {"help", "q", "ai", "status"})
        self.assertTrue(set(saved.values()) <= {"community", "family", "off"})

    def test_wifi_status_failure_is_not_reported_as_nothing_pending(self):
        # BUG: when `prepperctl wifi-status` fails (corrupt pending file → Python traceback, rc 1) the
        # portal answers {"ok": true, "pending": false}: the admin page hides the "keep/undo new password"
        # box although a change IS waiting. Expected: ok false / an error. Actual: ok true, pending false.
        # Source: software/portal/server.py handle_members wifi_status (rc != 0 → st = {}).
        (self.tmp / "ctl-mode").write_text("statusfail")
        st, r = self.admin_op({"op": "wifi_status"})
        self.assertFalse(r.get("ok") and r.get("pending") is False, r)

    def test_wifi_status_odd_json_is_not_a_500(self):
        # BUG (low): a JSON list on the last line of wifi-status output → st.get() AttributeError → 500.
        # Source: software/portal/server.py handle_members wifi_status.
        (self.tmp / "ctl-mode").write_text("statuslist")
        st, r = self.admin_op({"op": "wifi_status"})
        self.assertNotEqual(st, 500, r)

    def test_rotate_wifi_tells_the_admin_when_there_is_no_automatic_undo(self):
        # BUG: prepperctl wifi-rotate prints "warning: the automatic undo could not be scheduled — confirm or
        # undo by hand" (and still exits 0) when the revert timer can't be created. The portal keeps only
        # the passphrase and returns {"ok": true, ..., "confirm_within_s": 600}; app.js then promises "or it
        # switches back" — a promise that is no longer true. If the new password is then lost, everyone is
        # locked out. Expected: the warning reaches the admin (a field / message in the reply).
        # Actual: dropped. Source: software/portal/server.py handle_members rotate_wifi (and see
        # test_adv_rotation for the prepperctl side).
        (self.tmp / "ctl-mode").write_text("noundo")
        st, r = self.admin_op({"op": "rotate_wifi"})
        self.assertEqual(st, 200)
        self.assertIn("automatic undo", json.dumps(r))

    def test_rotate_mesh_status_only_matches_its_own_request(self):
        st, r = self.admin_op({"op": "rotate_mesh"})
        (self.tmp / "mesh-rotate-result.json").write_text(json.dumps({"id": "other", "ok": True, "url": "U"}))
        self.assertTrue(self.admin_op({"op": "rotate_mesh_status", "rid": r["id"]})[1]["pending"])
        for junk in ("[1,2]", "null", "\"str\"", "{bad"):
            (self.tmp / "mesh-rotate-result.json").write_text(junk)
            st2, r2 = self.admin_op({"op": "rotate_mesh_status", "rid": r["id"]})
            self.assertEqual((st2, r2.get("pending")), (200, True))
        for junk in ("[1]", "\"x\"", "{\"url\": 5}"):
            (self.tmp / "mesh-channel.json").write_text(junk)
            self.assertEqual(self.admin_op({"op": "mesh_link"})[0], 200)

    def test_rotate_mesh_request_file_is_atomic_and_fresh(self):
        ids = {self.admin_op({"op": "rotate_mesh"})[1]["id"] for _ in range(5)}
        self.assertEqual(len(ids), 5)
        self.assertIn(json.loads((self.tmp / "mesh-rotate.json").read_text())["id"], ids)

    def test_member_ops_need_admin(self):
        ana = self.join("AnaA")
        for op in ("list", "invite", "remove", "rotate_wifi", "confirm_wifi", "revert_wifi", "wifi_status",
                   "rotate_mesh", "rotate_mesh_status", "mesh_link", "add_radio"):
            with self.subTest(op=op):
                self.assertEqual(self.req("POST", "/api/admin/members", {"op": op}, member=ana)[0], 401)
                self.assertEqual(self.req("POST", "/api/admin/members", {"op": op}, admin=self.admin + "x")[0], 401)


# ───────────────────────────── fixes: the panel key, secrets, cookies, privacy ─────────────────────────────

class PanelKey(Portal):
    """Finding 2: the front panel proves itself with a root-only key (the portal has its SHA-256)."""

    KEY = "panel-secret-key-for-tests_0123456789abcdefghij"

    def setUp(self):
        super().setUp()
        import hashlib
        self.hash_file = self.tmp / "panel.key.sha256"
        self.hash_file.write_text(hashlib.sha256(self.KEY.encode()).hexdigest() + "\n")
        self.old = server.CFG.panel_key_hash
        server.CFG.panel_key_hash = self.hash_file
        server._members = None

    def tearDown(self):
        server.CFG.panel_key_hash = self.old
        p = self.tmp / "members.json"
        if p.exists():
            d = json.loads(p.read_text()); d["invites"] = {}; p.write_text(json.dumps(d))
        server._members = None

    def test_loopback_alone_is_not_the_panel(self):
        self.assertFalse(FakeHandler("127.0.0.1", {}).local_console())
        self.assertFalse(FakeHandler("::1", {}).local_console())
        self.assertEqual(self.req("POST", "/api/local/invite", {})[0], 403)

    def test_the_key_is_the_panel(self):
        ok = {"X-Badger-Panel": self.KEY}
        self.assertTrue(FakeHandler("127.0.0.1", ok).local_console())
        st, r = self.req("POST", "/api/local/invite", {}, headers=ok)
        self.assertEqual(st, 200, r)
        for bad in ({"X-Badger-Panel": self.KEY + "x"}, {"X-Badger-Panel": ""}, {"X-Badger-Panel": self.KEY.upper()},
                    {**ok, "X-Real-IP": "10.0.0.2"}, {**ok, "X-Forwarded-For": "10.0.0.2"},
                    {**ok, "Origin": "http://127.0.0.1"}, {**ok, "Sec-Fetch-Site": "same-origin"}):
            with self.subTest(h=bad):
                self.assertFalse(FakeHandler("127.0.0.1", bad).local_console())
        self.assertFalse(FakeHandler("10.0.0.5", ok).local_console())      # not on the Badger itself

    def test_missing_or_unreadable_hash_fails_closed_on_the_badger(self):
        server.CFG.panel_key_hash = self.tmp / "nope.sha256"
        old = server.CFG.standalone
        try:
            server.CFG.standalone = False                                 # behind nginx: a real Badger
            self.assertFalse(FakeHandler("127.0.0.1", {"X-Badger-Panel": self.KEY}).local_console())
            server.CFG.standalone = True                                  # a dev portal / go-bag laptop
            self.assertTrue(FakeHandler("127.0.0.1", {}).local_console())
        finally:
            server.CFG.standalone = old
        self.hash_file.write_text("not a hash")
        server.CFG.panel_key_hash = self.hash_file
        self.assertFalse(FakeHandler("127.0.0.1", {"X-Badger-Panel": self.KEY}).local_console())

    def test_feature_local_access_uses_the_same_check(self):
        self.assertIs(server.Handler.is_local_console.__doc__ is not None, True)
        src = (REPO / "software/portal/server.py").read_text()
        run_feature = src[src.index("    def run_feature"):src.index("    def local_console")]
        self.assertIn('"local": self.local_console()', run_feature)


NGINX_HTTP = {"X-Real-IP": "10.42.0.9", "X-Forwarded-Proto": "http"}
NGINX_HTTPS = {"X-Real-IP": "10.42.0.9", "X-Forwarded-Proto": "https"}


class Secrets(Portal):
    """Finding 5: new secrets are not sent over plain HTTP on the hotspot."""

    def test_new_wifi_password_hidden_over_plain_http(self):
        st, r = self.req("POST", "/api/admin/members", {"op": "rotate_wifi"}, admin=self.admin, headers=NGINX_HTTP)
        self.assertEqual(st, 200, r)
        self.assertIsNone(r["passphrase"])
        self.assertTrue(r["secret_hidden"])
        self.assertIn("screen", r["message"])
        st, raw, _ = self.raw_req("POST", "/api/admin/members", {"op": "rotate_wifi"},
                                  {"X-Admin-Token": self.admin, **NGINX_HTTP})
        self.assertNotIn(b"ABCD-EFGH-JKMN", raw)
        st, r = self.req("POST", "/api/admin/members", {"op": "rotate_wifi"}, admin=self.admin, headers=NGINX_HTTPS)
        self.assertEqual(r["passphrase"], "ABCD-EFGH-JKMN")

    def test_channel_link_hidden_over_plain_http(self):
        (self.tmp / "mesh-channel.json").write_text(json.dumps({"url": "https://meshtastic.org/e/#SECRET", "at": 1}))
        st, raw, _ = self.raw_req("POST", "/api/admin/members", {"op": "mesh_link"}, {"X-Admin-Token": self.admin, **NGINX_HTTP})
        self.assertEqual(st, 200)
        self.assertNotIn(b"SECRET", raw)
        self.assertTrue(json.loads(raw)["secret_hidden"])
        st, r = self.req("POST", "/api/admin/members", {"op": "mesh_link"}, admin=self.admin, headers=NGINX_HTTPS)
        self.assertEqual(r["url"], "https://meshtastic.org/e/#SECRET")
        st, r = self.req("POST", "/api/admin/members", {"op": "rotate_mesh"}, admin=self.admin)
        (self.tmp / "mesh-rotate-result.json").write_text(json.dumps({"id": r["id"], "ok": True, "url": "https://x/#NEWKEY"}))
        st, raw, _ = self.raw_req("POST", "/api/admin/members", {"op": "rotate_mesh_status", "rid": r["id"]},
                                  {"X-Admin-Token": self.admin, **NGINX_HTTP})
        self.assertNotIn(b"NEWKEY", raw)
        self.assertFalse(json.loads(raw)["pending"])

    def test_forged_proto_from_the_network_is_not_https(self):
        h = FakeHandler("10.42.0.9", {"X-Forwarded-Proto": "https"})
        h.secure_transport = server.Handler.secure_transport.__get__(h)
        self.assertFalse(h.secure_transport())


class Rotation(Portal):
    def test_stale_mesh_request_is_withdrawn(self):
        import time as _t
        st, r = self.admin_op({"op": "rotate_mesh"})
        req = self.tmp / "mesh-rotate.json"
        req.write_text(json.dumps({"id": r["id"], "at": _t.time() - 600}))
        st, x = self.admin_op({"op": "rotate_mesh_status", "rid": r["id"]})
        self.assertEqual((x["pending"], x["ok"]), (False, False))
        self.assertFalse(req.exists())                          # a bridge starting later will never act on it

    def test_cancel_withdraws_the_request(self):
        st, r = self.admin_op({"op": "rotate_mesh"})
        st, x = self.admin_op({"op": "rotate_mesh_cancel", "rid": r["id"]})
        self.assertTrue(x["cancelled"])
        self.assertFalse((self.tmp / "mesh-rotate.json").exists())
        self.admin_op({"op": "rotate_mesh"})
        self.admin_op({"op": "rotate_mesh_cancel", "rid": "someone-else"})   # only its own request
        self.assertTrue((self.tmp / "mesh-rotate.json").exists())

    def test_confirm_refused_before_the_new_password_is_on(self):
        (self.tmp / "ctl-mode").write_text("notapplied")
        st, r = self.admin_op({"op": "confirm_wifi"})
        self.assertEqual(st, 409, r)
        self.assertIn("isn't switched on yet", r["error"])
        self.assertFalse(self.admin_op({"op": "wifi_status"})[1]["applied"])


class Cookies(Portal):
    """Finding 9: sign-ins as HttpOnly, SameSite=Strict cookies (headers still work for plugins)."""

    @staticmethod
    def cookie_of(hdrs, name):
        sc = hdrs.get("Set-Cookie", "")
        return sc.split(";", 1)[0].split("=", 1)[1] if sc.startswith(name + "=") else None

    def test_join_sets_an_httponly_cookie_that_signs_in(self):
        st, inv = self.admin_op({"op": "invite", "name": "CookieAna"})
        st, raw, hdrs = self.raw_req("POST", "/api/members/join", {"code": inv["code"], "name": "", "device": "p"})
        self.assertEqual(st, 200)
        sc = hdrs["Set-Cookie"]
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/api/"):
            self.assertIn(attr, sc)
        key = self.cookie_of(hdrs, "bh_member")
        self.assertEqual(key, json.loads(raw)["key"])
        c = {"Cookie": f"bh_member={key}"}
        self.assertEqual(self.req("GET", "/api/board?board=family", headers=c)[0], 200)
        self.assertEqual(self.req("GET", "/api/members/me", headers=c)[1]["member"]["name"], "CookieAna")
        st, raw, hdrs = self.raw_req("POST", "/api/members/leave", {}, c)
        self.assertTrue(json.loads(raw)["ok"])
        self.assertIn("Max-Age=0", hdrs["Set-Cookie"])
        st, r = self.req("GET", "/api/board?board=family", headers=c)
        self.assertEqual((st, r["key"]), (401, "unknown"))            # the launcher may forget it now

    def test_admin_cookie_and_log_out_everywhere(self):
        st, raw, hdrs = self.raw_req("POST", "/api/admin/login", {"pin": "246810"})
        tok = self.cookie_of(hdrs, "bh_admin")
        self.assertIn("HttpOnly", hdrs["Set-Cookie"])
        c = {"Cookie": f"other=1; bh_admin={tok}"}
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "list"}, headers=c)[0], 200)
        self.assertTrue(self.req("GET", "/api/members/me", headers=c)[1]["admin"])
        st, r = self.req("POST", "/api/admin/logout", {"all": True}, headers=c)
        self.assertTrue(r["ok"])
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "list"}, headers=c)[0], 401)
        type(self).admin = self.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]   # for the other tests
        self.assertEqual(self.req("POST", "/api/admin/logout", {"all": True})[0], 401)  # needs an admin

    def test_odd_cookies_are_ignored(self):
        for c in ("bh_member=", "bh_member=\"x\"", "bh_member=" + "a" * 500, "bh_member=a b", "=;;", "bh_admin=null"):
            with self.subTest(c=c):
                self.assertEqual(self.req("GET", "/api/board?board=family", headers={"Cookie": c})[0], 401)

    def test_old_key_adoption(self):
        key = self.join("AdoptMe")
        st, raw, hdrs = self.raw_req("POST", "/api/members/adopt", {}, {"X-Member-Token": key})
        self.assertEqual(st, 200)
        self.assertEqual(self.cookie_of(hdrs, "bh_member"), key)
        st, r = self.req("POST", "/api/members/adopt", {}, member="not-a-key")
        self.assertEqual((st, r["key"]), (401, "unknown"))

    def test_damaged_member_list_is_503_not_unknown(self):
        key = self.join("Steady")
        p = self.tmp / "members.json"
        good = p.read_text()
        try:
            p.write_text(good[: len(good) // 2])
            st, r = self.req("GET", "/api/board?board=family", member=key)
            self.assertEqual((st, r["key"]), (503, "unavailable"))     # the device must keep its key
            st, r = self.admin_op({"op": "list"})
            self.assertEqual(st, 503)
            self.assertFalse(r["store"]["ok"])
            self.assertTrue(r["store"]["backup"])
            st, r = self.admin_op({"op": "restore_backup"})
            self.assertEqual(st, 200, r)
            self.assertEqual(self.req("GET", "/api/board?board=family", member=key)[0], 200)
        finally:
            if not p.exists() or not p.read_text().strip().endswith("}"):
                p.write_text(good)


class Privacy(Portal):
    def test_visitors_get_a_coarse_position(self):
        old = server.gps_fix
        server.gps_fix = lambda: {"lat": 39.739212, "lon": -104.990251, "alt": 1609.3, "time": "x", "mode": 3}
        try:
            gps = self.req("GET", "/api/status", headers=NGINX_HTTP)[1]["gps"]      # a phone on the Wi-Fi
            self.assertEqual((gps["lat"], gps["lon"], gps["coarse"]), (39.7, -105.0, True))
            self.assertNotIn("alt", gps)
            key = self.join("GpsAna")
            self.assertEqual(self.req("GET", "/api/status", member=key, headers=NGINX_HTTP)[1]["gps"]["lat"], 39.739212)
            self.assertEqual(self.req("GET", "/api/status", admin=self.admin, headers=NGINX_HTTP)[1]["gps"]["lon"], -104.990251)
        finally:
            server.gps_fix = old

    def test_badger_notes_never_wear_the_family_checkmark(self):
        ok, _ = server.board_post("feature:supplies", "", "Low on water", False, "family",
                                  {"id": "feature-supplies", "name": "Supplies"})
        self.assertTrue(ok)
        server._last_post.clear()
        ok, _ = server.board_post("feature:forecast", "", "Storm tonight", False, "community", system="Forecast")
        self.assertTrue(ok)
        key = self.join("NoteReader")
        for board, text in (("family", "Low on water"), ("community", "Storm tonight")):
            msgs = self.req("GET", f"/api/board?board={board}", member=key)[1]["messages"]
            rec = [m for m in msgs if m["text"] == text][0]
            self.assertTrue(rec["system"])
            self.assertNotIn("verified", rec)
        # a client can't claim to be the Badger
        self.post({"text": "fake note", "system": True, "name": "Badger"}, ip="10.77.0.1")
        rec = [m for m in self.req("GET", "/api/board")[1]["messages"] if m["text"] == "fake note"][0]
        self.assertNotIn("system", rec)

    def test_mesh_in_posts_never_verified_even_if_the_file_says_so(self):
        key = self.join("MeshReader")
        with open(self.tmp / "family-board.jsonl", "a") as fh:
            fh.write(json.dumps({"at": "x", "name": "Ana", "text": "radio says hi", "via": "mesh-in", "verified": True}) + "\n")
        rec = [m for m in self.req("GET", "/api/board?board=family", member=key)[1]["messages"] if m["text"] == "radio says hi"][0]
        self.assertIs(rec["verified"], False)


class BoardLimits(Portal):
    def test_boards_are_read_from_the_tail_and_trimmed(self):
        p = self.tmp / "board.jsonl"
        before = p.read_text() if p.exists() else ""
        old = server.BOARD_MAX_BYTES
        try:
            server.BOARD_MAX_BYTES = 20_000
            with open(p, "a") as fh:
                for i in range(400):
                    fh.write(json.dumps({"at": "x", "name": "n", "text": f"filler {i} " + "x" * 80}) + "\n")
            self.assertEqual(len(server.board_read(limit=50)), 50)
            self.post({"text": "newest"}, ip="10.88.0.1")
            self.assertLessEqual(p.stat().st_size, 20_000)
            msgs = self.req("GET", "/api/board")[1]["messages"]
            self.assertEqual(msgs[-1]["text"], "newest")
        finally:
            server.BOARD_MAX_BYTES = old
            p.write_text(before)

    def test_full_radio_queue_still_posts_to_the_board(self):
        out = self.tmp / "mesh-outbox.jsonl"
        before = out.read_text() if out.exists() else ""
        old = server.OUTBOX_MAX_BYTES
        try:
            server.OUTBOX_MAX_BYTES = 10
            out.write_text("x" * 50)
            st, r = self.post({"text": "queue full test", "mesh": True}, ip="10.88.0.2")
            self.assertEqual(st, 200)
            self.assertIn("queue is full", r["message"])
            self.assertEqual(out.read_text(), "x" * 50)
        finally:
            server.OUTBOX_MAX_BYTES = old
            out.write_text(before)


# ───────────────────────────── front end: static scan ─────────────────────────────

def enclosing_calls(line: str, pos: int) -> list[str]:
    """Names of the function calls whose parentheses enclose `pos` in `line`, innermost first."""
    out, depth = [], 0
    for i in range(pos - 1, -1, -1):
        c = line[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                m = re.search(r"([A-Za-z_$][\w$.]*)\s*$", line[:i])
                out.append(m.group(1) if m else "")
            else:
                depth -= 1
    return out


class FrontEnd(unittest.TestCase):
    FIELDS = re.compile(r"(?<![\w$.])(?:m|d|x|r|me|n|u)\.(?:name|text|label|code|passphrase|error|message|url|id|at|"
                        r"short|via)\b|(?<![\w$.])(?:url|who|link)\b(?![\w$]*\s*[(=:])")
    SAFE_CALLS = {"esc", "href", "num", "tfmt", "fmtTs", "encodeURIComponent", "lsSet", "lsGet", "getAttribute",
                  "confirm", "act", "api", "showInvite", "showLink", "Math.round", "String"}

    def scan(self, src: str, start_marker: str, end_marker: str):
        a = src.index(start_marker)
        b = src.index(end_marker, a)
        problems = []
        for ln_no, line in enumerate(src[a:b].splitlines(), src[:a].count("\n") + 1):
            if "+" not in line or ("<" not in line and "innerHTML" not in line):
                continue                                       # no HTML being built on this line
            for m in self.FIELDS.finditer(line):
                pre = line[:m.start()].rstrip()
                if pre.endswith(("===", "!==", "?", "&&", "||", "!", "if (", ".value =")) or \
                        line[m.end():].lstrip().startswith(("?", "===", "!==", "&&", "||", ")", ".length", ".map")):
                    if not pre.endswith(("+",)):
                        continue                               # used as a condition, not printed
                calls = enclosing_calls(line, m.start())
                if any(c in self.SAFE_CALLS for c in calls):
                    continue
                if re.search(r"(?:var|function)\s*\w*\s*\(?[^)]*$", pre) and "+" not in pre[-3:]:
                    continue
                if not pre.endswith("+") and not line[m.end():].lstrip().startswith("+"):
                    continue                                   # not concatenated into a string
                problems.append(f"app.js:{ln_no}: {m.group(0)} :: {line.strip()[:120]}")
        return problems

    def test_board_and_admin_rendering_escape_server_data(self):
        src = (REPO / "software/portal/static/app.js").read_text()
        problems = self.scan(src, 'api("/api/board?board="', "function members") if "function members" in src else []
        problems += self.scan(src, 'api("/api/board?board="', 'document.getElementById("rotMesh")')
        self.assertEqual(sorted(set(problems)), [])

    def test_esc_and_safeurl_definitions(self):
        src = (REPO / "software/portal/static/app.js").read_text()
        esc = re.search(r"function esc\(s\)\s*\{(.*?)\}\);\s*\}", src, re.S).group(1)
        for ch in ("&", "<", ">", '"', "'"):
            self.assertIn(ch, esc)
        self.assertIn("https?", src[src.index("function safeUrl"):src.index("function href")])

    def test_maps_render_nodes_as_text(self):
        src = (REPO / "software/portal/static/maps/index.html").read_text()
        a = src.index("/api/mesh/nodes") - 1500
        region = src[max(0, a):src.index("/api/mesh/nodes") + 1500]
        self.assertNotRegex(region, r"innerHTML\s*=|setHTML\(|insertAdjacentHTML|bindPopup\(")
        self.assertIn("textContent", region)


if __name__ == "__main__":
    unittest.main(verbosity=1)
