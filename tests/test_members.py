"""
Family members and the two boards: invites, device keys, removal, the family/community boards over
HTTP, family-only radio positions, and how the mesh bridge routes channels and gates commands.
Run: python3.11 tests/test_members.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "software" / "meshbridge"))
os.environ["PORTAL_QUIET"] = "1"
import members  # noqa: E402
import meshbridge as mb  # noqa: E402
import server  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class MembersUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="members-"))
        self.clock = Clock()
        self.db = members.Members(self.tmp / "members.json", clock=self.clock)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_invite_is_one_time_and_case_insensitive(self):
        code = self.db.create_invite()["code"]
        self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{2}$")
        st, m, key = self.db.join(code.lower().replace("-", " "), "Ana", "Ana's phone", "10.0.0.5")
        self.assertEqual((st, m["name"]), ("ok", "Ana"))
        self.assertEqual(self.db.auth(key)["name"], "Ana")
        self.assertEqual(self.db.join(code, "Eve", "x", "10.0.0.6")[0], "bad")      # used up

    def test_keys_and_codes_are_stored_hashed_and_private(self):
        code = self.db.create_invite()["code"]
        _, _, key = self.db.join(code, "Ana", "phone")
        raw = (self.tmp / "members.json").read_text()
        self.assertNotIn(key, raw)
        self.assertNotIn(code.replace("-", ""), raw)
        self.assertEqual((self.tmp / "members.json").stat().st_mode & 0o777, 0o600)

    def test_invite_codes_are_salted_scrypt_and_old_sha256_invites_still_work(self):
        code = self.db.create_invite()["code"]
        raw = json.loads((self.tmp / "members.json").read_text())
        self.assertTrue(all(k.startswith("s1$") for k in raw["invites"]))
        self.assertRegex(raw["code_salt"], r"^[0-9a-f]{32}$")
        self.assertNotIn(members._h(members.norm_code(code)), json.dumps(raw))
        # an invite written before this change (plain SHA-256 key) still redeems during its 24 h
        raw["invites"][members._h("AAAABBBBCC")] = {"id": "i_old", "expires": self.clock.t + 3600, "created": self.clock.t,
                                                     "member": None, "name": "Old", "source": "admin"}
        (self.tmp / "members.json").write_text(json.dumps(raw))
        self.assertEqual(self.db.join("aaaa-bbbb-cc", "", "phone")[0], "ok")
        self.assertEqual(self.db.join(code, "New", "phone")[0], "ok")

    def test_pending_device_expires_unapproved(self):
        ana = self.db.join(self.db.create_invite()["code"], "Ana", "phone")[1]
        st, m, key = self.db.join(self.db.create_invite(member_id=ana["id"], confirm=True)["code"], "", "tablet")
        self.assertEqual((st, m["pending"], self.db.auth_status(key)[0]), ("ok", True, "pending"))
        self.assertEqual(self.db.pending_info(key)["name"], "Ana")
        self.clock.t += members.PENDING_TTL + 1
        self.assertEqual(self.db.auth_status(key)[0], "unknown")
        self.assertEqual(self.db.pending_devices(), [])
        self.assertRaises(ValueError, self.db.approve_device, ana["id"], m["device"], True)

    def test_invite_expires(self):
        code = self.db.create_invite()["code"]
        self.clock.t += members.INVITE_TTL + 1
        self.assertEqual(self.db.join(code, "Ana", "phone")[0], "bad")

    def test_name_needed_unless_the_invite_has_one(self):
        code = self.db.create_invite()["code"]
        self.assertEqual(self.db.join(code, "  ", "phone")[0], "name")
        self.assertEqual(self.db.join(code, "Ana", "phone")[0], "ok")               # still usable after the slip
        code2 = self.db.create_invite("Ben")["code"]
        self.assertEqual(self.db.join(code2, "", "phone")[1]["name"], "Ben")

    def test_wrong_codes_lock_the_guesser_out(self):
        for _ in range(members.JOIN_FAILS):
            self.assertEqual(self.db.join("AAAA-BBBB-CC", "x", "y", "6.6.6.6")[0], "bad")
        good = self.db.create_invite()["code"]
        self.assertEqual(self.db.join(good, "x", "y", "6.6.6.6")[0], "locked")      # even a right code, while locked
        self.assertEqual(self.db.join(good, "Ana", "y", "10.0.0.5")[0], "ok")        # other devices unaffected
        self.clock.t += members.JOIN_LOCK + 1
        self.assertEqual(self.db.join_wait("6.6.6.6"), 0)

    def test_extra_device_and_removal(self):
        _, ana, k1 = self.db.join(self.db.create_invite()["code"], "Ana", "phone")
        _, same, k2 = self.db.join(self.db.create_invite(member_id=ana["id"])["code"], "ignored", "tablet")
        self.assertEqual(same["id"], ana["id"])
        self.assertEqual(self.db.auth(k2)["name"], "Ana")
        self.db.add_radio(ana["id"], "!A1B2C3D4")
        self.db.remove(ana["id"])
        self.assertIsNone(self.db.auth(k1))
        self.assertIsNone(self.db.auth(k2))
        reg = members.radio_registry(self.tmp / "members.json")
        self.assertTrue(reg["!a1b2c3d4"]["removed"])
        with self.assertRaises(ValueError):
            self.db.create_invite(member_id=ana["id"])                               # no new devices for them
        self.assertTrue(self.db.list()["members"][0]["removed"])

    def test_radios(self):
        _, ana, _ = self.db.join(self.db.create_invite()["code"], "Ana", "phone")
        _, ben, _ = self.db.join(self.db.create_invite()["code"], "Ben", "phone")
        for bad in ("a1b2c3d4", "!a1b2c3", "!zzzzzzzz", "!a1b2c3d4; rm -rf /"):
            with self.assertRaises(ValueError):
                self.db.add_radio(ana["id"], bad)
        self.db.add_radio(ana["id"], "!a1b2c3d4")
        with self.assertRaises(ValueError):
            self.db.add_radio(ben["id"], "!a1b2c3d4")                                 # already Ana's
        self.assertEqual(members.radio_registry(self.tmp / "members.json")["!a1b2c3d4"],
                         {"member": ana["id"], "name": "Ana", "removed": False})

    def test_forget_one_device(self):
        _, ana, k1 = self.db.join(self.db.create_invite()["code"], "Ana", "phone")
        dev = self.db.list()["members"][0]["devices"][0]["id"]
        self.db.revoke_device(ana["id"], dev)
        self.assertIsNone(self.db.auth(k1))
        self.assertIsNone(self.db.auth("x" * 500))
        self.assertIsNone(self.db.auth(""))

    def test_garbage_file(self):
        # A damaged file is no longer read as "no members" (the next write would have made that
        # permanent): reads that list members refuse, writes refuse, sign-ins simply fail, and the
        # file is left exactly as it was for the admin to restore (finding 7).
        (self.tmp / "members.json").write_text("[1, 2")
        with self.assertRaises(members.StoreError):
            self.db.list()
        with self.assertRaises(members.StoreError):
            self.db.create_invite("Ana")
        self.assertIsNone(self.db.auth("anything"))
        self.assertEqual(self.db.auth_status("anything"), ("unavailable", None))
        self.assertEqual((self.tmp / "members.json").read_text(), "[1, 2")
        self.assertFalse(self.db.status()["ok"])

    def test_backup_and_restore(self):
        _, ana, key = self.db.join(self.db.create_invite()["code"], "Ana", "phone")
        self.assertTrue(self.db.status()["backup"])                    # .bak: a copy of the last good list
        (self.tmp / "members.json").write_text("{broken")
        self.assertFalse(self.db.status()["ok"])
        self.db.restore_backup()
        self.assertEqual(self.db.auth(key)["name"], "Ana")
        self.assertTrue(any(p.name.startswith("members.json.damaged-") for p in self.tmp.iterdir()))

    def test_names_must_not_look_alike(self):
        self.db.join(self.db.create_invite()["code"], "Ana", "phone")
        for twin in ("ana", " A n a ", "ANA", "Аna", "A\u200bna", "Ána"):
            with self.subTest(twin=twin):
                st = self.db.join(self.db.create_invite()["code"], twin, "phone")[0]
                self.assertEqual(st, "taken")
                self.assertRaises(ValueError, self.db.create_invite, twin)
        self.assertEqual(self.db.join(self.db.create_invite()["code"], "Ana B.", "phone")[0], "ok")
        code = self.db.create_invite("Cal")["code"]                     # the invite's name wins
        self.assertEqual(self.db.join(code, "Admin", "phone")[1]["name"], "Cal")


class PortalBoards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="boards-")).resolve()
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
ctl = "/nonexistent/prepperctl"
services = {{}}
""")
        server.CFG = server.PortalConfig(cfg)
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

    @classmethod
    def req(cls, method, path, body=None, member=None, admin=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if member:
            h["X-Member-Token"] = member
        if admin:
            h["X-Admin-Token"] = admin
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, {}

    def post(self, body, member=None):
        server._last_post.clear()                     # the 5-second-per-client brake isn't under test here
        return self.req("POST", "/api/board", body, member=member)

    def invite_and_join(self, name):
        st, inv = self.req("POST", "/api/admin/members", {"op": "invite", "name": name}, admin=self.admin)
        self.assertEqual(st, 200, inv)
        st, j = self.req("POST", "/api/members/join", {"code": inv["code"], "name": "", "device": f"{name}'s phone"})
        self.assertEqual(st, 200, j)
        return j["key"]

    def test_family_board_end_to_end(self):
        self.assertEqual(self.req("GET", "/api/board?board=family")[0], 401)
        self.assertEqual(self.post({"board": "family", "name": "Mallory", "text": "hi"})[0], 401)
        ana = self.invite_and_join("Ana")
        self.assertEqual(self.req("GET", "/api/members/me", member=ana)[1]["member"]["name"], "Ana")
        st, r = self.post({"board": "family", "name": "Mallory", "text": "At the gym, all OK", "mesh": True}, member=ana)
        self.assertEqual(st, 200, r)
        st, b = self.req("GET", "/api/board?board=family", member=ana)
        last = b["messages"][-1]
        self.assertEqual((last["name"], last["text"], last["verified"]), ("Ana", "At the gym, all OK", True))   # name can't be spoofed
        self.assertNotIn("member", last)
        out = [json.loads(x) for x in (self.tmp / "mesh-outbox.jsonl").read_text().splitlines()]
        # CHANGED (mesh courtesy): outbox lines now also carry "kind" (priority), "at" (expiry) and "member"
        # (typed by a member: may go out on the family channel during radio silence) — see docs/MESH.md
        at = out[-1].pop("at")
        self.assertLessEqual(abs(at - time.time()), 60)
        self.assertEqual(out[-1], {"text": "Ana: At the gym, all OK", "board": "family", "kind": "family", "member": True})
        # the community board never sees family posts
        self.assertNotIn("At the gym, all OK", json.dumps(self.req("GET", "/api/board")[1]))

    def test_community_board_is_open_and_hides_membership_from_visitors(self):
        ben = self.invite_and_join("Ben")
        self.assertEqual(self.post({"name": "Neighbor", "text": "Water at the church"})[0], 200)
        self.assertEqual(self.post({"name": "whatever", "text": "Medic on Elm St"}, member=ben)[0], 200)
        visitor = self.req("GET", "/api/board")[1]["messages"]
        self.assertTrue(all("verified" not in m for m in visitor))
        mine = self.req("GET", "/api/board", member=ben)[1]["messages"]
        self.assertTrue(any(m.get("verified") and m["name"] == "Ben" for m in mine))

    def test_removal_cuts_access_and_marks_old_posts(self):
        cal = self.invite_and_join("Cal")
        dee = self.invite_and_join("Dee")
        self.post({"board": "family", "text": "Cal here"}, member=cal)
        lst = self.req("POST", "/api/admin/members", {"op": "list"}, admin=self.admin)[1]
        cal_id = [m["id"] for m in lst["members"] if m["name"] == "Cal"][0]
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "remove", "id": cal_id}, admin=self.admin)[0], 200)
        self.assertEqual(self.req("GET", "/api/board?board=family", member=cal)[0], 401)
        self.assertEqual(self.post({"board": "family", "text": "still here?"}, member=cal)[0], 401)
        self.assertEqual(self.req("GET", "/api/mesh/nodes", member=cal)[0], 401)
        seen = self.req("GET", "/api/board?board=family", member=dee)[1]["messages"]
        self.assertTrue([m for m in seen if m["text"] == "Cal here"][0]["removed_member"])

    def test_admin_only_and_family_only_endpoints(self):
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "list"})[0], 401)
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "invite"})[0], 401)
        self.assertEqual(self.req("GET", "/api/mesh/nodes")[0], 401)
        self.assertEqual(self.req("GET", "/api/mesh/nodes", admin=self.admin)[0], 200)
        eve = self.invite_and_join("Eve")
        self.assertEqual(self.req("GET", "/api/mesh/nodes", member=eve)[0], 200)
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "list"}, member=eve)[0], 401)   # members aren't admins
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "bogus"}, admin=self.admin)[0], 400)
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "add_radio", "id": "m_nope", "radio": "!a1b2c3d4"},
                                  admin=self.admin)[0], 400)
        self.assertEqual(self.req("GET", "/api/board?board=secret")[0], 404)

    def test_admin_sets_radio_command_levels(self):
        st, r = self.req("POST", "/api/admin/settings", {"mesh_commands": {"ai": "community", "status": "off",
                                                                            "help": "off", "evil": "community", "q": "root"}}, admin=self.admin)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["settings"]["mesh_commands"], {"ai": "community", "status": "off", "help": "family"})
        self.assertEqual(self.req("POST", "/api/admin/settings", {"mesh_commands": {"ai": "family"}})[0], 401)   # admins only
        st, r = self.req("POST", "/api/admin/settings", {"mesh_commands": {"q": "ignore", "help": "ignore"}}, admin=self.admin)
        self.assertEqual((r["settings"]["mesh_commands"]["q"], r["settings"]["mesh_commands"]["help"]), ("ignore", "ignore"))

    def test_join_brute_force_and_leave(self):
        server._members = None                        # fresh guess counters
        codes = [self.req("POST", "/api/members/join", {"code": f"ZZZZ-ZZZZ-Z{i}", "name": "x", "device": "y"})[0] for i in range(6)]
        self.assertEqual(codes[:5], [403] * 5)
        self.assertEqual(codes[5], 429)
        server._members = None
        fay = self.invite_and_join("Fay")
        self.assertEqual(self.req("POST", "/api/members/leave", {}, member=fay)[1]["ok"], True)
        self.assertEqual(self.req("GET", "/api/members/me", member=fay)[0], 401)


class MeshRouting(unittest.TestCase):
    def test_route_text(self):
        fam, pub = 1, 0
        ana = {"member": "m_1", "name": "Ana", "removed": False}
        gone = {"member": "m_2", "name": "Cal", "removed": True}
        self.assertEqual(mb.route_text("hi", 1, fam, pub, None), ("family", True))       # key holders on the family channel
        self.assertEqual(mb.route_text("hi", 0, fam, pub, None), ("community", False))
        # CHANGED (finding 1): a registered radio ID on the public channel is NOT trusted — the sender ID
        # is not authenticated on the air (it used to give ("community", True)).
        self.assertEqual(mb.route_text("hi", 0, fam, pub, ana), ("community", False))
        self.assertEqual(mb.route_text("hi", 1, fam, pub, gone), ("ignore", False))      # removed member: ignored everywhere
        self.assertEqual(mb.route_text("hi", 5, fam, pub, None), ("ignore", False))
        # CHANGED (finding 4): older one-channel configs no longer make channel 0 the family's (it used to
        # route ("family", True)); channel_config turns them into a public-only setup.
        self.assertEqual(mb.channel_config({"mesh_channel": 0})[:2], (-1, 0))
        self.assertEqual(mb.route_text("hi", 0, -1, 0, None), ("community", False))

    def test_channel_config_fails_closed(self):
        cases = [((("mesh_family_channel", 1), ("mesh_public_channel", 0)), (1, 0)),
                 ((("mesh_family_channel", 2), ("mesh_public_channel", -1)), (2, -1)),
                 ((("mesh_family_channel", 0), ("mesh_public_channel", 1)), (-1, 1)),   # 0 is never the family's
                 ((("mesh_family_channel", 3), ("mesh_public_channel", 3)), (-1, 3)),   # one channel can't be both
                 ((("mesh_public_channel", 0),), (-1, 0)),                             # family must be explicit
                 ((("mesh_channel", 2),), (-1, 2)),
                 ((), (-1, 0)),
                 ((("mesh_family_channel", "1"), ("mesh_public_channel", 0)), (-1, 0)),   # not a number
                 ((("mesh_family_channel", True), ("mesh_public_channel", 0)), (-1, 0)),
                 ((("mesh_family_channel", 9), ("mesh_public_channel", 0)), (-1, 0))]
        for kv, want in cases:
            with self.subTest(cfg=kv):
                fam, pub, notes = mb.channel_config(dict(kv))
                self.assertEqual((fam, pub), want)
                if want[0] == -1 and kv:
                    self.assertTrue(notes)

    def test_family_only_commands(self):
        said = []
        c = mb.Commands(lambda t, d: said.append(t), clock=lambda: 0.0, library=lambda q: "LIB", ai=lambda q: "AI",
                        status=lambda: "STATUS", spawn=lambda fn: fn())
        c.handle("!status", "!stranger", trusted=False, reply=said.append)
        self.assertEqual(said, [])            # CHANGED (red team finding 6): strangers get no refusal at all
        c.handle("!q water", "!other", trusted=False, reply=said.append)
        self.assertEqual(said[-1], "LIB")
        c.handle("!status", "!family", trusted=True, reply=said.append)
        self.assertEqual(said[-1], "STATUS")

    def test_command_levels_are_configurable(self):
        self.assertEqual(mb.command_access({}), mb.DEFAULT_ACCESS)
        cfg = {"mesh_commands": {"ai": "community", "status": "off", "q": "family", "bogus": "community", "help": "weird"}}
        acc = mb.command_access(cfg)
        # CHANGED (red team finding 6): !help defaults to "family" (strangers are ignored)
        self.assertEqual((acc["!ai"], acc["!status"], acc["!q"], acc["!help"]), ("community", "off", "family", "family"))
        self.assertNotIn("!bogus", acc)
        # what the admin set in the launcher wins over prepper.toml; help can't be switched off for family radios
        acc = mb.command_access(cfg, {"ai": "family", "help": "off"})
        self.assertEqual((acc["!ai"], acc["!help"]), ("family", "family"))
        acc = mb.command_access(cfg, {"help": "community", "q": "ignore"})
        self.assertEqual((acc["!help"], acc["!q"]), ("community", "ignore"))

    def test_levels_apply_live(self):
        said, levels = [], {"!help": "community", "!q": "community", "!ai": "community", "!status": "off"}
        c = mb.Commands(lambda t, d: None, clock=lambda: 0.0, library=lambda q: "LIB", ai=lambda q: "AI",
                        status=lambda: "STATUS", spawn=lambda fn: fn(), cooldown=0, access=lambda: levels)
        c.handle("!ai splint", "!stranger", trusted=False, reply=said.append)     # opened to the community
        self.assertEqual(said[-1], "AI")
        c.handle("!status", "!family", trusted=True, reply=said.append)          # off for everyone
        self.assertIn("turned off", said[-1])
        c.handle("!help", "!stranger", trusted=False, reply=said.append)
        self.assertIn("!ai", said[-1]); self.assertNotIn("!status", said[-1])
        levels["!q"] = "family"                                                  # changed by the admin: applies at once
        n = len(said)
        c.handle("!q water", "!stranger2", trusted=False, reply=said.append)
        self.assertEqual(len(said), n)        # CHANGED (red team finding 6): silently ignored, no refusal
        self.assertIn("no radio commands", mb.help_text({"!q": "off", "!ai": "off", "!status": "off"}, False))

    def test_board_add_goes_to_the_right_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="mesh-boards-"))
        try:
            mb.BOARD, mb.FAMILY_BOARD = tmp / "community.jsonl", tmp / "family.jsonl"
            mb.board_add("Ana", "ok", "family", "!a1b2c3d4", "m_1")
            mb.board_add("Stranger", "hello", "community", "!ffffffff", "m_1")
            fam = json.loads((tmp / "family.jsonl").read_text())
            com = json.loads((tmp / "community.jsonl").read_text())
            # CHANGED (finding 1): mesh posts are never "verified" (it used to be True for a registered radio);
            # the member id is kept only on the family board, for the "removed member" mark.
            self.assertEqual((fam["name"], fam["verified"], fam["member"]), ("Ana", False, "m_1"))
            self.assertEqual((com["name"], com["verified"]), ("Stranger", False))
            self.assertNotIn("member", com)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FakeChannel:
    def __init__(self, role):
        self.role = role
        self.settings = type("S", (), {"psk": b"old-key"})()


class FakeNode:
    def __init__(self, roles):
        self.channels = [FakeChannel(r) for r in roles]
        self.written = []

    def writeChannel(self, i):
        self.written.append(i)

    def getURL(self, includeAll=True):
        return "https://meshtastic.org/e/#" + self.channels[1].settings.psk.hex()[:16]


class KeyRotation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rotate-"))
        self.iface = type("I", (), {})()
        self.iface.localNode = FakeNode([1, 2, 0])            # primary, family (secondary), disabled

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_key_written_and_link_returned(self):
        url = mb.rotate_family_key(self.iface, 1, rng=lambda n: b"\x01" * n)
        self.assertEqual(self.iface.localNode.channels[1].settings.psk, b"\x01" * 32)
        self.assertEqual(self.iface.localNode.written, [1])
        self.assertTrue(url.startswith("https://meshtastic.org/e/#"))
        self.assertEqual(self.iface.localNode.channels[0].settings.psk, b"old-key")   # public channel untouched

    def test_refuses_what_would_break_things(self):
        for ch in (-1, 2, 9):                                  # none, disabled, missing
            with self.subTest(ch=ch), self.assertRaises(ValueError):
                mb.rotate_family_key(self.iface, ch)
        self.assertEqual(self.iface.localNode.written, [])

    def test_request_result_and_recoverable_link(self):
        req, res, link = self.tmp / "req.json", self.tmp / "res.json", self.tmp / "link.json"
        req.write_text(json.dumps({"id": "abc123"}))
        out = mb.handle_rotation(self.iface, 1, req, res, link)
        self.assertTrue(out["ok"])
        self.assertFalse(req.exists())                         # acted on once
        self.assertEqual(json.loads(res.read_text())["id"], "abc123")
        saved = json.loads(link.read_text())
        self.assertEqual(saved["url"], out["url"])             # the admin page can show it again any time
        self.assertEqual(link.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(mb.handle_rotation(self.iface, 1, req, res, link))   # nothing pending

    def test_failure_is_reported_and_nothing_changes(self):
        req, res = self.tmp / "req.json", self.tmp / "res.json"
        req.write_text(json.dumps({"id": "x1"}))
        out = mb.handle_rotation(self.iface, 2, req, res)     # disabled channel
        self.assertFalse(out["ok"])
        self.assertIn("disabled", json.loads(res.read_text())["error"])
        self.assertEqual(self.iface.localNode.channels[2].settings.psk, b"old-key")


class Failsafes(PortalBoards):
    """The portal side of rotation, the front-panel invite, with a fake prepperctl."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fake = cls.tmp / "fake-prepperctl"
        fake.write_text('#!/bin/sh\ncase "$1" in\n'
                        '  wifi-rotate) echo "passphrase: ABCD-EFGH-JKMN"; echo "confirm-within: 600" ;;\n'
                        '  wifi-status) echo \'{"pending": true, "seconds_left": 412}\' ;;\n'
                        '  wifi-confirm) echo "kept the new Wi-Fi password" ;;\n'
                        '  wifi-revert) echo "switched back to the previous Wi-Fi password" ;;\n'
                        '  *) echo "unexpected $1"; exit 2 ;;\nesac\n')
        fake.chmod(0o755)
        server.CFG.ctl = str(fake)
        server.CFG.mesh_rotate_request = cls.tmp / "mesh-rotate.json"
        server.CFG.mesh_rotate_result = cls.tmp / "mesh-rotate-result.json"
        server.CFG.mesh_channel_link = cls.tmp / "mesh-channel.json"

    def admin_op(self, body):
        return self.req("POST", "/api/admin/members", body, admin=self.admin)

    def test_wifi_rotation_confirm_and_undo(self):
        st, r = self.admin_op({"op": "rotate_wifi"})
        self.assertEqual((st, r["passphrase"], r["confirm_within_s"]), (200, "ABCD-EFGH-JKMN", 600))
        st, r = self.admin_op({"op": "wifi_status"})
        self.assertEqual((r["pending"], r["seconds_left"]), (True, 412))
        self.assertIn("kept", self.admin_op({"op": "confirm_wifi"})[1]["message"])
        self.assertIn("switched back", self.admin_op({"op": "revert_wifi"})[1]["message"])
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "rotate_wifi"})[0], 401)   # admins only

    def test_mesh_rotation_request_and_link(self):
        st, r = self.admin_op({"op": "rotate_mesh"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads((self.tmp / "mesh-rotate.json").read_text())["id"], r["id"])
        self.assertTrue(self.admin_op({"op": "rotate_mesh_status", "rid": r["id"]})[1]["pending"])
        (self.tmp / "mesh-rotate-result.json").write_text(json.dumps({"id": r["id"], "ok": True, "url": "https://meshtastic.org/e/#X"}))
        done = self.admin_op({"op": "rotate_mesh_status", "rid": r["id"]})[1]
        # CHANGED (red team 4): the new key goes to members and the Badger's screen, never to an admin session
        self.assertEqual((done["pending"], done["ok"], done["url"]), (False, True, None))
        self.assertIn("needs the new link", done["message"])
        self.assertFalse(self.admin_op({"op": "mesh_link"})[1]["ok"])        # not reported yet
        (self.tmp / "mesh-channel.json").write_text(json.dumps({"url": "https://meshtastic.org/e/#Y", "at": 1}))
        self.assertIsNone(self.admin_op({"op": "mesh_link"})[1]["url"])
        gil = self.invite_and_join("Gil")
        self.assertEqual(self.req("GET", "/api/members/radio-link", member=gil)[1]["url"], "https://meshtastic.org/e/#Y")

    def test_front_panel_invite_only_from_the_badger_itself(self):
        st, r = self.req("POST", "/api/local/invite", {})                    # straight to the port, as the panel does
        self.assertEqual(st, 200, r)
        self.assertRegex(r["code"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{2}$")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)   # relayed by nginx for a LAN client
        c.request("POST", "/api/local/invite", "{}", {"Content-Type": "application/json", "X-Real-IP": "10.10.10.77"})
        resp = c.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        c.close()


class InsiderProtections(Failsafes):
    """Red team 4 and 11: the admin PIN is not a master key over the family."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        server.CFG.mesh_pair_file = cls.tmp / "mesh-pair.json"

    def setUp(self):
        server._last_post.clear()
        for f in ("mesh-pair.json", "mesh-rotate.json"):
            try:
                (self.tmp / f).unlink()
            except FileNotFoundError:
                pass

    def mid(self, name):
        return [m["id"] for m in self.admin_op({"op": "list"})[1]["members"] if m["name"] == name and not m["removed"]][0]

    def family(self, member):
        return self.req("GET", "/api/board?board=family", member=member)[1]["messages"]

    def extra_device(self, name, label="new phone"):
        st, inv = self.admin_op({"op": "invite", "id": self.mid(name)})
        self.assertEqual(st, 200, inv)
        self.assertTrue(inv["needs_approval"])
        st, j = self.req("POST", "/api/members/join", {"code": inv["code"], "name": "", "device": label})
        self.assertEqual(st, 200, j)
        self.assertTrue(j["pending"])
        return j["key"]

    def test_device_for_an_existing_member_waits_for_their_approval(self):
        mom = self.invite_and_join("Mom")
        kid = self.invite_and_join("Kid")
        rogue = self.extra_device("Mom", "rogue phone")
        st, me = self.req("GET", "/api/members/me", member=rogue)
        self.assertEqual((st, me["member"], me["key"], me["pending"]["name"]), (401, None, "pending", "Mom"))
        server._last_post.clear()
        st, r = self.post({"board": "family", "text": "Meet at the old mill"}, member=rogue)
        self.assertEqual((st, r["key"]), (401, "pending"))                  # nothing posted, key kept
        self.assertEqual(self.req("GET", "/api/board?board=family", member=rogue)[0], 401)
        self.assertEqual(self.req("GET", "/api/mesh/nodes", member=rogue)[0], 401)
        self.assertEqual(self.req("GET", "/api/members/radio-link", member=rogue)[0], 401)
        self.assertFalse(any("old mill" in m["text"] for m in self.family(mom)))
        # Mom's own phone is asked; Kid's is not, and Kid can't approve Mom's device
        appr = self.req("GET", "/api/members/me", member=mom)[1]["approvals"]
        self.assertEqual([a["label"] for a in appr], ["rogue phone"])
        self.assertEqual(self.req("GET", "/api/members/me", member=kid)[1]["approvals"], [])
        st, r = self.req("POST", "/api/members/approve", {"device": appr[0]["device"], "approve": True}, member=kid)
        self.assertEqual(st, 400)
        st, r = self.req("POST", "/api/members/approve", {"device": appr[0]["device"], "approve": True}, member=rogue)
        self.assertEqual(st, 401)                                            # it can't approve itself
        st, r = self.req("POST", "/api/members/approve", {"device": appr[0]["device"], "approve": "yes"}, member=mom)
        self.assertEqual(st, 400)
        # Mom says no: the device is gone for good
        st, r = self.req("POST", "/api/members/approve", {"device": appr[0]["device"], "approve": False}, member=mom)
        self.assertEqual((st, r["approved"]), (200, False))
        st, me = self.req("GET", "/api/members/me", member=rogue)
        self.assertEqual((st, me["key"]), (401, "unknown"))
        self.assertFalse(any("added to Mom (" in m["text"] for m in self.family(mom)))

    def test_approved_device_works_and_the_family_board_says_so(self):
        mom = self.invite_and_join("Mona")
        tablet = self.extra_device("Mona", "tablet")
        dev = self.req("GET", "/api/members/me", member=mom)[1]["approvals"][0]["device"]
        st, r = self.req("POST", "/api/members/approve", {"device": dev, "approve": True}, member=mom)
        self.assertEqual((st, r["approved"]), (200, True))
        self.assertEqual(self.req("GET", "/api/members/me", member=tablet)[1]["member"]["name"], "Mona")
        note = [m for m in self.family(tablet) if "A new device was added to Mona" in m["text"]]
        self.assertEqual(len(note), 1)
        self.assertTrue(note[0].get("system"))
        self.assertNotIn("verified", note[0])
        self.assertEqual(self.req("POST", "/api/members/approve", {"device": dev, "approve": True}, member=mom)[0], 400)

    def test_front_panel_can_approve_when_no_device_is_left(self):
        self.invite_and_join("Opa")
        lost = self.mid("Opa")
        devs = [d["id"] for m in self.admin_op({"op": "list"})[1]["members"] if m["id"] == lost for d in m["devices"]]
        self.admin_op({"op": "revoke_device", "id": lost, "device": devs[0]})        # the phone was lost
        new = self.extra_device("Opa", "replacement")
        st, r = self.req("GET", "/api/local/devices")                       # loopback, no nginx: the panel
        self.assertEqual(st, 200, r)
        pend = [x for x in r["pending"] if x["member"] == lost]
        self.assertEqual(pend[0]["label"], "replacement")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)  # relayed for a LAN client: refused
        c.request("POST", "/api/local/approve", json.dumps({"member": lost, "device": pend[0]["device"], "approve": True}),
                  {"Content-Type": "application/json", "X-Real-IP": "10.10.10.77"})
        resp = c.getresponse(); resp.read(); c.close()
        self.assertEqual(resp.status, 403)
        st, r = self.req("POST", "/api/local/approve", {"member": lost, "device": pend[0]["device"], "approve": True})
        self.assertEqual(st, 200, r)
        self.assertEqual(self.req("GET", "/api/members/me", member=new)[1]["member"]["name"], "Opa")
        self.assertTrue(any("added to Opa" in m["text"] and "at the Badger" in m["text"] for m in self.family(new)))

    def test_new_members_still_join_on_the_admin_pin_alone(self):
        st, inv = self.admin_op({"op": "invite", "name": "Newcomer"})
        self.assertNotIn("needs_approval", inv)
        st, j = self.req("POST", "/api/members/join", {"code": inv["code"], "name": "", "device": "phone"})
        self.assertEqual((st, j.get("pending")), (200, None))
        self.assertEqual(self.req("GET", "/api/members/me", member=j["key"])[0], 200)

    def test_radio_is_bound_only_after_it_sends_the_code_on_the_family_channel(self):
        dad = self.invite_and_join("Dad")
        dad_id = self.mid("Dad")
        st, r = self.admin_op({"op": "add_radio", "id": dad_id, "radio": "!DEADBEEF"})
        self.assertEqual(st, 202, r)
        self.assertRegex(r["code"], r"^[A-Z2-9]{6}$")
        self.assertIn(f"!pair {r['code']}", r["message"])
        row = [m for m in self.admin_op({"op": "list"})[1]["members"] if m["id"] == dad_id][0]
        self.assertEqual((row["radios"], [x["radio"] for x in row["pending_radios"]]), ([], ["!deadbeef"]))
        self.assertNotIn("!deadbeef", members.radio_registry(self.tmp / "members.json"))   # the bridge sees nothing yet
        pair = self.tmp / "mesh-pair.json"
        now = time.time()
        for wrong in ([{"radio": "!deadbeef", "code": "ZZZZZZ", "at": now}],            # wrong code
                      [{"radio": "!0badf00d", "code": r["code"], "at": now}],           # right code, other radio
                      [{"radio": "!deadbeef", "code": r["code"], "at": now - 7200}],    # heard before it was made
                      [{"radio": ["!deadbeef"], "code": r["code"], "at": now}], "junk", [1, None]):
            pair.write_text(json.dumps({"confirms": wrong}))
            row = [m for m in self.admin_op({"op": "list"})[1]["members"] if m["id"] == dad_id][0]
            self.assertEqual(row["radios"], [], wrong)
        pair.write_text(json.dumps({"confirms": [{"radio": "!deadbeef", "code": r["code"].lower(), "at": now}]}))
        st, me = self.req("GET", "/api/members/me", member=dad)
        self.assertEqual((me["member"]["radios"], me["member"]["pending_radios"]), (["!deadbeef"], []))
        self.assertEqual(members.radio_registry(self.tmp / "members.json")["!deadbeef"]["name"], "Dad")
        notes = [m for m in self.family(dad) if "!deadbeef was linked to Dad" in m["text"]]
        self.assertEqual(len(notes), 1)
        self.admin_op({"op": "list"})                                        # the same confirm again: no second note
        self.assertEqual(len([m for m in self.family(dad) if "!deadbeef was linked" in m["text"]]), 1)

    def test_radio_code_expires_and_members_pair_their_own(self):
        eli = self.invite_and_join("Eli")
        st, r = self.req("POST", "/api/members/radio", {"op": "pair", "radio": "!12345678"}, member=eli)
        self.assertEqual(st, 202, r)
        st, s = self.req("POST", "/api/members/radio", {"op": "status"}, member=eli)
        self.assertEqual([x["radio"] for x in s["pending_radios"]], ["!12345678"])
        db = server.members_db()
        real = db.clock
        try:
            db.clock = lambda: real() + members.RADIO_PAIR_TTL + 5
            (self.tmp / "mesh-pair.json").write_text(json.dumps({"confirms": [
                {"radio": "!12345678", "code": r["code"], "at": real() + members.RADIO_PAIR_TTL + 1}]}))
            st, s = self.req("POST", "/api/members/radio", {"op": "status"}, member=eli)
            self.assertEqual((s["radios"], s["pending_radios"]), ([], []))
        finally:
            db.clock = real
        st, r = self.req("POST", "/api/members/radio", {"op": "pair", "radio": "!12345678"}, member=eli)
        (self.tmp / "mesh-pair.json").write_text(json.dumps({"confirms": [{"radio": "!12345678", "code": r["code"], "at": time.time()}]}))
        st, s = self.req("POST", "/api/members/radio", {"op": "status"}, member=eli)
        self.assertEqual((s["radios"], s["paired"]), (["!12345678"], ["!12345678"]))
        st, s = self.req("POST", "/api/members/radio", {"op": "remove", "radio": "!12345678"}, member=eli)
        self.assertEqual(s["radios"], [])
        for bad in ({"op": "pair", "radio": "!xyz"}, {"op": "pair"}, {"op": "explode"}):
            self.assertEqual(self.req("POST", "/api/members/radio", bad, member=eli)[0], 400)
        self.assertEqual(self.req("POST", "/api/members/radio", {"op": "status"})[0], 401)

    def test_removing_a_member_rotates_the_family_key_by_default(self):
        self.invite_and_join("Ex")
        st, r = self.admin_op({"op": "remove", "id": self.mid("Ex")})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["rotate"]["ok"])
        self.assertEqual(json.loads((self.tmp / "mesh-rotate.json").read_text())["id"], r["rotate"]["id"])
        mon = self.invite_and_join("Monitor")
        self.assertTrue(any("Ex was removed" in m["text"] for m in self.family(mon)))
        (self.tmp / "mesh-rotate.json").unlink()
        self.invite_and_join("Ex2")
        st, r = self.admin_op({"op": "remove", "id": self.mid("Ex2"), "rotate_key": False})
        self.assertEqual((st, "rotate" in r), (200, False))
        self.assertFalse((self.tmp / "mesh-rotate.json").exists())

    def test_admin_page_learns_whether_the_panel_is_locked(self):
        self.assertFalse(self.admin_op({"op": "list"})[1]["panel_lock"])
        lock = self.tmp / "panel-lock.json"
        server.CFG.panel_lock_file = lock
        lock.write_text("{}")
        try:
            self.assertTrue(self.admin_op({"op": "list"})[1]["panel_lock"])
        finally:
            lock.unlink()
        server.CFG.panel_lock_arm = self.tmp / "panel-lock-arm.json"
        st, r = self.admin_op({"op": "arm_panel_lock"})
        self.assertEqual(st, 200, r)
        rec = json.loads((self.tmp / "panel-lock-arm.json").read_text())
        self.assertAlmostEqual(rec["until"], time.time() + 600, delta=30)
        self.assertEqual(self.req("POST", "/api/admin/members", {"op": "arm_panel_lock"})[0], 401)


class AdminSecret(unittest.TestCase):
    """Passphrases, scrypt, and the move from the older PBKDF2 hashes."""

    def test_policy(self):
        for bad in ("", "12345", "abc123", "short pa", "x" * 129, " leading", "tab\there12345"):
            self.assertIsNotNone(server.pin_policy(bad)[0], bad)
        self.assertEqual(server.pin_policy("correct horse battery"), (None, None))
        err, warn = server.pin_policy("246810")
        self.assertIsNone(err)
        self.assertIn("passphrase", warn)

    def test_scrypt_and_legacy_verify(self):
        h = server.hash_pin("correct horse battery")
        self.assertTrue(h.startswith("scrypt$32768$8$1$"))
        self.assertTrue(server.verify_pin("correct horse battery", h))
        self.assertFalse(server.verify_pin("correct horse batterY", h))
        old = server.legacy_hash_pin("246810", "abcd")
        self.assertTrue(server.verify_pin("246810", old) and server.pin_is_legacy(old))
        self.assertFalse(server.verify_pin("246811", old))
        for junk in ("", "scrypt$", "scrypt$1048577$8$1$s$00", "scrypt$1073741824$8$1$s$00", "scrypt$3$8$1$s$00",
                     "scrypt$16384$64$1$s$00", "pbkdf2$x", "md5$x$y", None, 5):
            self.assertFalse(server.verify_pin("246810", junk), junk)

    def test_hash_pin_cli_reads_stdin_and_rehash_needs_the_old_pin(self):
        import subprocess
        py, srv = sys.executable, str(REPO / "software/portal/server.py")
        r = subprocess.run([py, srv, "hash-pin"], input="correct horse battery\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(server.verify_pin("correct horse battery", r.stdout.strip()))
        r = subprocess.run([py, srv, "hash-pin"], input="short\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        old = server.legacy_hash_pin("246810", "abcd")
        r = subprocess.run([py, srv, "rehash-pin"], input=f"{old}\n246810\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(server.verify_pin("246810", r.stdout.strip()))
        for stdin in (f"{old}\n999999\n", f"{r.stdout.strip()}\n246810\n", "\n246810\n"):
            self.assertEqual(subprocess.run([py, srv, "rehash-pin"], input=stdin, capture_output=True, text=True).returncode, 1)


class PinUpgradeOverHTTP(PortalBoards):
    def test_old_pbkdf2_hash_is_upgraded_at_the_next_login(self):
        legacy = server.legacy_hash_pin("135791", "s4lt")
        cfg_text = server.CFG.path.read_text()
        saved = server.CFG.admin_pin
        calls = []

        def fake_ctl(*args, input=None):  # noqa: A002 — what prepperctl upgrade-pin does, in-process
            calls.append((args, input))
            stored = __import__("tomllib").loads(server.CFG.path.read_text())["portal"]["admin_pin"]
            pin = (input or "").rstrip("\n")
            if args != ("upgrade-pin",) or not server.pin_is_legacy(stored) or not server.verify_pin(pin, stored):
                return 2, "not upgraded"
            server.CFG.path.write_text(server.CFG.path.read_text().replace(stored, server.hash_pin(pin)))
            return 0, "admin PIN hash upgraded"
        real_ctl = server.ctl
        try:
            server.CFG.path.write_text(cfg_text.replace(saved, legacy))
            server.CFG.admin_pin = legacy
            server.ctl = fake_ctl
            server.reset_login_limits()
            self.assertEqual(self.req("POST", "/api/admin/login", {"pin": "135799"})[0], 403)
            self.assertEqual(calls, [])                                     # a wrong PIN never reaches prepperctl
            st, r = self.req("POST", "/api/admin/login", {"pin": "135791"})
            self.assertEqual(st, 200, r)
            self.assertTrue(r["weak_pin"])
            for _ in range(100):
                if server.CFG.admin_pin.startswith("scrypt$"):
                    break
                time.sleep(0.05)
            self.assertTrue(server.CFG.admin_pin.startswith("scrypt$"))
            self.assertEqual(calls[0][1], "135791\n")                       # on stdin, never in argv
            self.assertNotIn("135791", " ".join(calls[0][0]))
            self.assertIn("scrypt$", server.CFG.path.read_text())
            server.reset_login_limits()
            self.assertEqual(self.req("POST", "/api/admin/login", {"pin": "135791"})[0], 200)
            self.assertEqual(len(calls), 1)                                 # nothing left to upgrade
        finally:
            server.ctl = real_ctl
            server.CFG.path.write_text(cfg_text)
            server.CFG.admin_pin = saved
            server.reset_login_limits()


if __name__ == "__main__":
    unittest.main(verbosity=1)
