#!/usr/bin/env python3
"""
Duress through the real portal over HTTP (standalone, throwaway data, a fake prepperctl that logs what it was
asked): the duress PIN and the duress passphrase answer exactly like the real ones; behind them the default
"lock" hard-locks the sealed records and the vault (nothing destroyed, no restart, no Wi-Fi or posture change)
and every session sees the made-up household — plausible, as many people as the family, nothing that says
"duress" — until the owner types the recovery key. Also: the owner's setup ops and decoy editor, a restart and
the front panel's path (the flag set by another process), and duress_action = "wipe".

    python3.11 tests/test_duress_portal.py -v
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "redteam"))
from harness import PHONE_HTTP, PIN, PortalCase, server  # noqa: E402

import sealed  # noqa: E402  (harness put software/common on the path)

HTTPS = {"X-Real-IP": "10.10.10.77", "X-Forwarded-Proto": "https"}
OWNER = "correct horse battery staple"
PHRASE = "purple monkey dishwasher tonight"
DPIN = "blue kettle whistles"
REAL_NAMES = ("Lucia", "Tomasz")
REAL_SECRETS = ("Lucia", "Tomasz", "Ammo cache", "under the floor", "house empty till six", "FAMILYPSK", "482913")
TELLS = re.compile(r"duress|wipe|decoy|dead", re.I)
FAKE_CTL = r'''import json, os, sys
args = sys.argv[1:]
data = sys.stdin.read() if args[:1] == ["set-duress"] else ""
with open(os.environ.get("FAKE_CTL_LOG", "/dev/null"), "a") as fh:
    fh.write(json.dumps({"args": args, "stdin": data}) + "\n")
if args and args[0] in ("panic-wipe", "handover-reset") and "--confirm" not in args:
    print("confirm-token: ABCD-EFGH")
else:
    print("started")
'''


class DuressCase(PortalCase):
    FEATURES = ("supplies.py", "vault.py", "_vault_crypto.py", "posture.py")
    EXTRA_TOML = "[sealed]\nscrypt_n = 1024\n"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        server.DECOY.clear()
        server.REAL.clear()
        server.DURESS_TOKENS.clear()
        fake = cls.tmp / "fakectl.py"
        fake.write_text(FAKE_CTL)
        cls.log = cls.tmp / "ctl.log"
        cls.log.write_text("")
        os.environ["FAKE_CTL_LOG"] = str(cls.log)
        server.CFG.ctl = f"{sys.executable} {fake}"
        server.CFG.wipe_pending = cls.tmp / "wipe-pending.json"
        server.CFG.wipe_receipt = cls.tmp / "wipe-receipt.json"
        server.CFG.deadman_state = cls.tmp / "deadman-state.json"
        server.CFG.duress_pin = server.hash_pin(DPIN)
        for f in ("mesh_bt_pin_file", "family_inbox", "mesh_peers", "mesh_circle_link", "circle_status_file",
                  "circle_ask_file", "mesh_telemetry"):
            setattr(server.CFG, f, cls.tmp / Path(getattr(server.CFG, f)).name)

    @classmethod
    def tearDownClass(cls):
        if server.DECOY:
            server.decoy_wait()
            for f, v in server.REAL.items():
                setattr(server.CFG, f, v)
        server.DECOY.clear()
        server.REAL.clear()
        server.DURESS_TOKENS.clear()
        sealed._instances.clear()
        sealed._default[0] = None
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        server.reset_login_limits()

    def calls(self) -> list[list[str]]:
        return [json.loads(x)["args"] for x in self.log.read_text().splitlines() if x.strip()]

    def login(self, pin, headers=None):
        st, raw, hdrs = self.raw("POST", "/api/admin/login", {"pin": pin}, headers or PHONE_HTTP)
        return st, json.loads(raw), hdrs

    def sealed_op(self, body, admin=None):
        return self.req("POST", "/api/sealed", body, admin=admin or self.admin, headers=HTTPS)

    def wipe_op(self, body, admin=None, headers=None):
        return self.req("POST", "/api/admin/wipe", body, admin=admin or self.admin, headers=headers or HTTPS)

    @classmethod
    def family(cls):
        """A real household: two members (one with a radio link and a BT PIN on file), supplies with a hiding
        place, a family-board post, sealed records set up, a duress passphrase, a vault with a document."""
        c = cls
        k1, k2 = c.join_cls("Lucia"), c.join_cls("Tomasz")
        c.req("POST", "/api/supplies/item", {"name": "Ammo cache", "category": "other", "qty": 3,
                                             "location": "under the floor"}, member=k1, headers=HTTPS)
        c.req("POST", "/api/board", {"board": "family", "text": "house empty till six", "name": "x"}, member=k1,
              headers=HTTPS)
        server._last_post.clear()
        st, r = c.req("POST", "/api/sealed", {"op": "setup", "passphrase": OWNER}, admin=c.admin, headers=HTTPS)
        assert st == 200, r
        rk = r["recovery_key"]
        st, r = c.req("POST", "/api/admin/wipe", {"op": "phrase_set", "passphrase": OWNER, "duress": PHRASE},
                      admin=c.admin, headers=HTTPS)
        assert st == 200 and r["duress_phrase"], r
        st, r = c.req("POST", "/api/vault/setup", {"passphrase": "family vault phrase"}, member=k1, headers=HTTPS)
        assert st == 200, r
        vrk = r["recovery_key"]
        c.req("POST", "/api/vault/note", {"name": "Passports", "category": "id", "text": "in the safe"}, member=k1,
              headers=HTTPS)
        Path(server.CFG.mesh_channel_link).write_text(json.dumps({"url": "https://meshtastic.org/e/#FAMILYPSK"}))
        Path(server.CFG.mesh_bt_pin_file).write_text(json.dumps({"pin": "482913"}))
        Path(server.CFG.posture_file).write_text(json.dumps({"level": "neighbourly", "changed_at": 1, "by": "admin"}))
        return k1, k2, rk, vrk

    @classmethod
    def join_cls(cls, name):
        st, inv = cls.req("POST", "/api/admin/members", {"op": "invite", "name": name}, admin=cls.admin, headers=PHONE_HTTP)
        assert st == 200, inv
        st, j = cls.req("POST", "/api/members/join", {"code": inv["code"], "name": name, "device": "phone"},
                        headers=PHONE_HTTP)
        assert st == 200, j
        return j["key"]

    def scan(self, member, admin, label):
        """Every page a duress session looks at: nothing real, nothing that says duress / wipe / decoy / dead."""
        pages = [("GET", "/api/status", None, None, None), ("GET", "/api/features", None, member, None),
                 ("GET", "/api/board?board=family", None, member, None), ("GET", "/api/board", None, member, None),
                 ("GET", "/api/members/me", None, member, None), ("GET", "/api/supplies", None, member, None),
                 ("GET", "/api/vault/status", None, member, None), ("GET", "/api/posture", None, member, None),
                 ("GET", "/api/sealed", None, None, admin), ("GET", "/api/status", None, None, admin),
                 ("POST", "/api/admin/members", {"op": "list"}, None, admin),
                 ("GET", "/api/members/radio-link", None, member, None)]
        seen = {}
        for method, path, body, m, a in pages:
            st, r = self.req(method, path, body, member=m, admin=a, headers=HTTPS)
            text = json.dumps(r)
            self.assertNotEqual(st, 500, (label, path, r))
            hit = TELLS.search(text)
            self.assertIsNone(hit, f"{label}: {path} says {hit and hit.group(0)!r}: {text[:300]}")
            for s in REAL_SECRETS:
                self.assertNotIn(s, text, f"{label}: {path} shows the real {s!r}")
            seen[path + (body or {}).get("op", "")] = (st, r)
        return seen


class LockMode(DuressCase):
    """The default action, start to finish (numbered: one duress per portal)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.k1, cls.k2, cls.rk, cls.vrk = cls.family()
        cls.toml = Path(server.CFG.path).read_text()

    def test_1_status_before(self):
        st, r = self.wipe_op({"op": "status"})
        self.assertEqual((r["duress_set"], r["duress_phrase"], r["duress_action"], r["sealed_setup"]),
                         (True, True, "lock", True))

    def test_2_duress_passphrase_answers_like_the_owner_passphrase(self):
        def timed(secret):
            self.sealed_op({"op": "lock"})
            t = time.perf_counter()
            st, r = self.sealed_op({"op": "unlock", "secret": secret})
            return time.perf_counter() - t, st, r
        reals = [timed(OWNER) for _ in range(3)]
        wrong = timed("not the owner passphrase at all")
        self.assertEqual(wrong[1], 403)
        self.sealed_op({"op": "lock"})
        self.log.write_text("")
        td, sd, rd = timed(PHRASE)
        ta, sa, ra = reals[-1]
        self.assertEqual((sa, sd), (200, 200))
        self.assertEqual(sorted(ra), sorted(rd), "the same reply")
        self.assertEqual(sorted(ra["status"]), sorted(rd["status"]), "the same status fields")
        self.assertEqual(rd["via"], "passphrase")
        for k in ("setup", "unlocked", "protected", "provisional", "fingerprint", "setup_at", "locked_reason", "posture"):
            self.assertEqual(ra["status"][k] if k != "posture" else "guarded", rd["status"][k], k)
        med = statistics.median(x[0] for x in reals)
        self.assertLess(abs(td - med), max(1.5 * med, 0.6), f"timing class: {td:.3f}s vs {med:.3f}s")
        self.assertTrue(server.DECOY, "the made-up household from now on")

    def test_3_everyone_sees_the_made_up_household(self):
        seen = self.scan(self.k1, self.admin, "after the duress passphrase")
        sup = seen["/api/supplies"][1]
        names = [i["name"] for i in sup["items"]]
        self.assertTrue(any(n.startswith("Bottled water") for n in names), names)
        self.assertTrue(any("Propane" in n for n in names))
        self.assertGreaterEqual(len(names), 10)
        board = seen["/api/board?board=family"][1]["messages"]
        self.assertGreaterEqual(len(board), 8)
        mem = seen["/api/admin/memberslist"][1]["members"]
        self.assertEqual(len(mem), 2, "the same number of people as the real family")
        self.assertFalse({m["name"] for m in mem} & set(REAL_NAMES))
        self.assertEqual(seen["/api/members/me"][0], 200, "a real family phone keeps working")
        self.assertIn(seen["/api/members/me"][1]["member"]["name"], {m["name"] for m in mem})
        st, me2 = self.req("GET", "/api/members/me", member=self.k2, headers=HTTPS)
        self.assertEqual(st, 200)
        v = seen["/api/vault/status"][1]
        self.assertFalse(v["setup"], "an empty vault: no documents")
        self.assertEqual(seen["/api/posture"][1]["level"], "guarded")
        self.assertEqual(seen["/api/status"][1].get("posture", {"level": "guarded"})["level"] if
                         isinstance(seen["/api/status"][1].get("posture"), dict) else "guarded", "guarded")
        link = seen["/api/members/radio-link"]
        self.assertFalse(link[1].get("url"), "no family radio link")
        self.assertIsNone(link[1].get("bt_pin"))
        st, r = self.wipe_op({"op": "status"})
        self.assertEqual((r["duress_set"], r["duress_phrase"], r["duress_action"]), (False, False, "lock"),
                         "after a duress nobody sees that one is set up")
        st, r = self.req("POST", "/api/admin/wipe", {"op": "decoy_get"}, admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual(st, 200)

    def test_4_nothing_visible_happened(self):
        calls = self.calls()
        self.assertEqual(calls, [], "no prepperctl: no restart, no Wi-Fi change, no posture change, no wipe")
        self.assertEqual(Path(server.CFG.path).read_text(), self.toml, "prepper.toml untouched (Wi-Fi password)")
        real_posture = json.loads(server.real_path("posture_file").read_text())
        self.assertEqual(real_posture["level"], "neighbourly", "the real posture is unchanged")
        self.assertTrue(server.real_path("members_file").exists(), "nothing destroyed")
        rs = server.real_sealer()
        self.assertTrue(rs.hard_locked())
        self.assertEqual(rs.unlock(OWNER), "hard-locked", "the passphrase alone opens nothing now")
        self.assertFalse(rs.is_unlocked())

    def test_5_the_decoy_behaves_like_sealed_records(self):
        st, r = self.sealed_op({"op": "lock"})
        self.assertEqual(st, 200)
        self.assertFalse(r["status"]["unlocked"])
        st, r = self.req("GET", "/api/board?board=family", member=self.k1, headers=HTTPS)
        self.assertTrue(r["sealed"]["locked"], "locked looks locked")
        st, r = self.sealed_op({"op": "unlock", "secret": "a wrong passphrase here"})
        self.assertEqual(st, 403)
        st, r = self.sealed_op({"op": "unlock", "secret": OWNER})
        self.assertEqual((st, r["via"]), (200, "passphrase"), "the real passphrase too: never 'wrong' under threat")
        st, r = self.sealed_op({"op": "setup", "passphrase": "another long passphrase"})
        self.assertEqual(st, 409)
        st, r = self.sealed_op({"op": "new_recovery", "passphrase": PHRASE})
        self.assertEqual(st, 200)
        self.assertNotEqual(sealed.vc.parse_recovery(r["recovery_key"]), sealed.vc.parse_recovery(self.rk))
        st, r = self.sealed_op({"op": "change", "old": PHRASE, "new": "brand new owner phrase"})
        self.assertEqual(st, 200)
        self.assertEqual(server.real_sealer().check_secret(OWNER), "passphrase", "a change in the decoy changes nothing")
        # a duress session's settings ops only pretend
        st, r = self.wipe_op({"op": "phrase_clear", "passphrase": OWNER})
        self.assertEqual(st, 200)
        self.assertTrue(server.real_sealer().has_duress())
        st, r = self.wipe_op({"op": "decoy_save", "profile": {"names": ["X"], "supplies": [{"name": "Gold"}],
                                                                "board": ["hi"]}})
        self.assertEqual(st, 200)
        self.assertNotEqual(server.decoy_profile()["names"][0], "X", "pretend only")
        self.assertEqual(self.calls(), [])

    def test_6_many_phones_at_once(self):
        errs = []

        def hit(i):
            st, r = self.req("GET", "/api/supplies" if i % 2 else "/api/board?board=family",
                             member=self.k1 if i % 3 else self.k2, headers=HTTPS)
            if st != 200 or any(s in json.dumps(r) for s in REAL_SECRETS):
                errs.append((st, str(r)[:200]))
        ts = [threading.Thread(target=hit, args=(i,)) for i in range(24)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])

    def test_7_recovery_key_brings_everything_back(self):
        st, raw, _ = self.raw("POST", "/api/admin/login", {"pin": DPIN}, PHONE_HTTP)
        dtok = json.loads(raw)["token"]
        self.assertIn(self.admin, server.DURESS_TOKENS, "it typed the duress passphrase")
        st, r = self.sealed_op({"op": "unlock", "secret": self.rk})
        self.assertEqual((st, r["via"]), (200, "recovery"))
        self.assertTrue(r["status"]["unlocked"])
        self.assertFalse(server.DECOY)
        self.assertFalse(server.real_sealer().hard_locked())
        st, r = self.req("GET", "/api/supplies", member=self.k1, headers=HTTPS)
        self.assertIn("Ammo cache", json.dumps(r), "the real records, nothing lost")
        st, r = self.req("GET", "/api/board?board=family", member=self.k2, headers=HTTPS)
        texts = [m["text"] for m in r["messages"]]
        self.assertIn("house empty till six", texts)
        self.assertIn("Recovered after a duress lock.", texts)
        st, r = self.req("POST", "/api/admin/members", {"op": "list"}, admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual(sorted(m["name"] for m in r["members"]), sorted(REAL_NAMES))
        st, r = self.req("POST", "/api/admin/members", {"op": "list"}, admin=dtok, headers=PHONE_HTTP)
        self.assertEqual(st, 401, "a duress session ends with the recovery")
        st, r = self.req("POST", "/api/admin/members", {"op": "list"}, admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual(st, 200, "…but not the one that typed the recovery key")
        # the vault: still hard-locked until its own recovery key
        st, r = self.req("POST", "/api/vault/unlock", {"passphrase": "family vault phrase"}, member=self.k1, headers=HTTPS)
        self.assertEqual(st, 423, r)
        self.assertIn("recovery key", r["error"])
        st, r = self.req("POST", "/api/vault/unlock", {"passphrase": self.vrk}, member=self.k1, headers=HTTPS)
        self.assertEqual(st, 200, r)
        st, r = self.req("POST", "/api/vault/list", {}, member=self.k1, headers=HTTPS)
        self.assertEqual(len(r["files"]), 1)
        self.sealed_op({"op": "lock"})
        st, r = self.sealed_op({"op": "unlock", "secret": OWNER})
        self.assertEqual((st, r["via"]), (200, "passphrase"), "the owner passphrase works again")
        self.assertEqual(self.calls(), [])


class DuressPin(DuressCase):
    """The duress PIN: same login reply; a restart keeps the made-up household; the panel's path (a flag set by
    another process) does the same."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.k1, cls.k2, cls.rk, cls.vrk = cls.family()

    def test_1_login_parity_then_decoy(self):
        def timed(pin):
            t = time.perf_counter()
            st, r, h = self.login(pin, {"X-Real-IP": "10.10.10.90", "X-Forwarded-Proto": "http"})
            return time.perf_counter() - t, st, r, h
        admin = [timed(PIN) for _ in range(3)]
        server.reset_login_limits()
        self.req("POST", "/api/sealed", {"op": "lock"}, admin=self.admin, headers=HTTPS)
        td, sd, rd, hd = timed(DPIN)
        ta, sa, ra, ha = admin[-1]
        self.assertEqual((sa, sd), (200, 200))
        self.assertEqual(sorted(ra), sorted(rd))
        self.assertEqual(len(ra["token"]), len(rd["token"]))
        self.assertEqual(ha["Set-Cookie"].split(";", 1)[1], hd["Set-Cookie"].split(";", 1)[1])
        med = statistics.median(a[0] for a in admin)
        self.assertLess(abs(td - med), max(1.5 * med, 0.6))
        seen = self.scan(self.k1, rd["token"], "after the duress PIN")
        self.assertFalse(seen["/api/sealed"][1]["unlocked"], "locked, as the real records were")
        self.assertEqual(self.calls(), [])

    def test_2_restart_keeps_it(self):
        server.decoy_wait()
        for f, v in server.REAL.items():
            setattr(server.CFG, f, v)
        server.DECOY.clear()
        server.REAL.clear()
        sealed._instances.clear()
        server._members = None
        seen = self.scan(self.k1, self.admin, "after a restart")
        self.assertTrue(server.DECOY)
        self.assertFalse(seen["/api/sealed"][1]["unlocked"])
        self.assertEqual(len(seen["/api/admin/memberslist"][1]["members"]), 2)

    def test_3_panel_flag_and_recovery_at_the_panel(self):
        st, r = self.req("POST", "/api/sealed", {"op": "unlock", "secret": self.rk}, admin=self.admin, headers=HTTPS)
        self.assertEqual(r.get("via"), "recovery")
        self.assertFalse(server.DECOY)
        st, r = self.req("GET", "/api/supplies", member=self.k1, headers=HTTPS)
        self.assertIn("Ammo cache", json.dumps(r))
        # the front panel's duress code: the root wipe engine sets the flag; the portal notices at once
        self.assertTrue(sealed.hard_lock_in(server.real_sealer().dir))
        self.scan(self.k1, self.admin, "after the panel's duress code")
        self.assertTrue(server.DECOY)


class Setup(DuressCase):
    """The owner's setup ops and decoy editor (no duress happens here)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        st, r = cls.req("POST", "/api/sealed", {"op": "setup", "passphrase": OWNER}, admin=cls.admin, headers=HTTPS)
        assert st == 200, r

    def test_action_needs_pin_and_https(self):
        st, r = self.wipe_op({"op": "action", "pin": PIN, "action": "wipe"}, headers=PHONE_HTTP)
        self.assertEqual(st, 403)
        st, r = self.wipe_op({"op": "action", "pin": "wrongwrong", "action": "wipe"})
        self.assertEqual(st, 403)
        st, r = self.wipe_op({"op": "action", "pin": PIN, "action": "explode"})
        self.assertEqual(st, 400)
        st, r = self.wipe_op({"op": "action", "pin": PIN, "action": "wipe"})
        self.assertEqual((st, r["duress_action"]), (200, "wipe"))
        self.assertEqual(json.loads(Path(server.CFG.settings_file).read_text())["duress_action"], "wipe")
        st, r = self.wipe_op({"op": "action", "pin": PIN, "action": "lock"})
        self.assertEqual(r["duress_action"], "lock")
        st, r = self.req("POST", "/api/admin/wipe", {"op": "action", "pin": PIN, "action": "wipe"}, headers=HTTPS)
        self.assertEqual(st, 401)

    def test_phrase_rules(self):
        st, r = self.wipe_op({"op": "phrase_set", "passphrase": OWNER, "duress": PHRASE}, headers=PHONE_HTTP)
        self.assertEqual(st, 403)
        st, r = self.wipe_op({"op": "phrase_set", "passphrase": "wrong owner passphrase", "duress": PHRASE})
        self.assertEqual(st, 403)
        st, r = self.wipe_op({"op": "phrase_set", "passphrase": OWNER, "duress": "short"})
        self.assertEqual(st, 400)
        st, r = self.wipe_op({"op": "phrase_set", "passphrase": OWNER, "duress": OWNER})
        self.assertEqual(st, 400)
        server.CFG.admin_pin, saved = server.hash_pin("an admin passphrase long"), server.CFG.admin_pin
        try:
            st, r = self.wipe_op({"op": "phrase_set", "passphrase": OWNER, "duress": "an admin passphrase long"})
            self.assertEqual(st, 400)
        finally:
            server.CFG.admin_pin = saved
        st, r = self.wipe_op({"op": "phrase_set", "passphrase": OWNER, "duress": PHRASE})
        self.assertEqual((st, r["duress_phrase"]), (200, True))
        st, r = self.wipe_op({"op": "phrase_clear", "passphrase": OWNER})
        self.assertEqual((st, r["duress_phrase"]), (200, False))
        self.assertFalse(server.DECOY)

    def test_decoy_editor(self):
        st, r = self.req("POST", "/api/admin/wipe", {"op": "decoy_get"}, headers=PHONE_HTTP)
        self.assertEqual(st, 401)
        st, r = self.wipe_op({"op": "decoy_get"}, headers=PHONE_HTTP)
        self.assertEqual(st, 200)
        p = r["profile"]
        self.assertTrue(p["supplies"] and p["board"])
        self.assertEqual(r["preview"]["posture"], "Guarded")
        f = server.decoy_profile_file()
        self.assertTrue(f.exists(), "made by default, before any duress is set up")
        p["names"] = ["Ana", "Ben"] + p["names"]
        p["board"] = [{"text": "Back around 6"}, {"text": "<script>alert(1)</script>"}]
        st, r = self.wipe_op({"op": "decoy_save", "profile": p})
        self.assertEqual(st, 200, r)
        self.assertEqual(server.decoy_profile()["names"][:2], ["Ana", "Ben"])
        self.assertEqual(r["profile"]["seed"], p["seed"], "the device's seed stays")
        st, r = self.wipe_op({"op": "decoy_save", "profile": {**p, "supplies": []}})
        self.assertEqual(st, 400, "an empty list looks wrong")
        st, r = self.wipe_op({"op": "decoy_save", "profile": {**p, "board": [{"text": "x" * 300}] * 1000}})
        self.assertEqual(st, 413)
        st, r = self.wipe_op({"op": "decoy_save", "profile": "nope"})
        self.assertEqual(st, 400)
        old = server.decoy_profile()["seed"]
        st, r = self.wipe_op({"op": "decoy_regen"})
        self.assertEqual(st, 200)
        self.assertNotEqual(server.decoy_profile()["seed"], old)
        self.assertEqual(f.stat().st_mode & 0o777, 0o640)


class WipeMode(DuressCase):
    """duress_action = "wipe" (chosen on the admin page): the wipe is asked for, the made-up household shows."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.k1, cls.k2, cls.rk, cls.vrk = cls.family()
        st, r = cls.req("POST", "/api/admin/wipe", {"op": "action", "pin": PIN, "action": "wipe"}, admin=cls.admin,
                        headers=HTTPS)
        assert r["duress_action"] == "wipe", r

    def test_wipe_mode(self):
        self.log.write_text("")
        st, r, _ = self.login(DPIN)
        self.assertEqual(st, 200)
        end = time.time() + 8
        while ["panic-wipe", "--confirm", "ABCD-EFGH", "--trigger", "duress"] not in self.calls() and time.time() < end:
            time.sleep(0.05)
        self.assertIn(["panic-wipe", "--confirm", "ABCD-EFGH", "--trigger", "duress"], self.calls())
        seen = self.scan(self.k1, r["token"], "wipe mode")
        self.assertGreaterEqual(len(seen["/api/supplies"][1]["items"]), 10, "not an empty box")
        self.assertEqual(len(seen["/api/admin/memberslist"][1]["members"]), 2)
        # in wipe mode the keys are gone: any passphrase "unlocks" the made-up records (never "wrong" under threat)
        self.req("POST", "/api/sealed", {"op": "lock"}, admin=r["token"], headers=HTTPS)
        st, x = self.req("POST", "/api/sealed", {"op": "unlock", "secret": OWNER}, admin=r["token"], headers=HTTPS)
        self.assertEqual((st, x["via"]), (200, "passphrase"))


if __name__ == "__main__":
    unittest.main()
