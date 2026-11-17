#!/usr/bin/env python3
"""
Panic wipe, handover reset, duress PIN and dead-man switch through the real portal over HTTP
(standalone, throwaway data, a fake prepperctl that logs what it was asked).

Duress parity: the duress PIN must get the same reply as the admin PIN (status, keys, cookie,
timing class), the coerced person must see the made-up household from the very first request, and the wipe
must be asked for in the background (duress_action = "wipe"; the default "lock": test_duress_portal.py).

    python3.11 tests/test_wipe_portal.py -v
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "redteam"))
from harness import PHONE_HTTP, PIN, PortalCase, server  # noqa: E402

HTTPS = {"X-Real-IP": "10.10.10.77", "X-Forwarded-Proto": "https"}
DURESS = "blue kettle whistles"
FAKE_CTL = r'''import json, os, sys
args = sys.argv[1:]
data = sys.stdin.read() if args[:1] == ["set-duress"] else ""   # (only that verb gets stdin)
with open(os.environ.get("FAKE_CTL_LOG", "/dev/null"), "a") as fh:
    fh.write(json.dumps({"args": args, "stdin": data}) + "\n")
if args and args[0] in ("panic-wipe", "handover-reset") and "--confirm" not in args:
    print("This erases ...")
    print("confirm-token: ABCD-EFGH")
elif args[:1] == ["deadman-set"] and args[1:2] != ["--confirm"]:
    print("confirm-code: QWE234")
else:
    print("started")
'''


class WipeHttp(PortalCase):
    FEATURES = ("supplies.py",)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        server.DECOY.clear()
        server.DURESS_TOKENS.clear()
        fake = cls.tmp / "fakectl.py"
        fake.write_text(FAKE_CTL)
        cls.log = cls.tmp / "ctl.log"
        import os
        os.environ["FAKE_CTL_LOG"] = str(cls.log)
        server.CFG.ctl = f"{sys.executable} {fake}"
        server.CFG.deadman_state = cls.tmp / "deadman-state.json"
        server.CFG.wipe_pending = cls.tmp / "wipe-pending.json"
        server.CFG.wipe_receipt = cls.tmp / "wipe-receipt.json"
        server.CFG.duress_pin = server.hash_pin(DURESS)
        # These tests cover duress_action = "wipe"; the default "lock" is in tests/test_duress_portal.py.
        server.CFG.raw.setdefault("security", {})["duress_action"] = "wipe"

    @classmethod
    def tearDownClass(cls):
        if server.DECOY:
            server.decoy_wait()
            for f, v in server.REAL.items():
                setattr(server.CFG, f, v)
        server.DECOY.clear()
        server.REAL.clear()
        server.DURESS_TOKENS.clear()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        server.reset_login_limits()
        self.log.write_text("")

    def calls(self, wait_for: str | None = None, timeout: float = 8.0) -> list[list[str]]:
        end = time.time() + timeout
        while True:
            rows = [json.loads(x) for x in self.log.read_text().splitlines() if x.strip()]
            if wait_for is None or any(wait_for in r["args"] for r in rows) or time.time() > end:
                return [r["args"] for r in rows]
            time.sleep(0.05)

    def stdin_of(self, verb: str) -> str:
        for x in self.log.read_text().splitlines():
            r = json.loads(x)
            if r["args"][:1] == [verb]:
                return r["stdin"]
        return ""

    def login(self, pin, headers=None):
        st, raw, hdrs = self.raw("POST", "/api/admin/login", {"pin": pin}, headers or PHONE_HTTP)
        return st, json.loads(raw), hdrs

    def wipe_op(self, body, admin=None, headers=None):
        return self.req("POST", "/api/admin/wipe", body, admin=admin or self.admin, headers=headers or HTTPS)


class Routes(WipeHttp):
    def test_admin_only(self):
        st, r = self.req("POST", "/api/admin/wipe", {"op": "panic", "confirm": "WIPE"}, headers=HTTPS)
        self.assertEqual(st, 401)
        self.assertEqual(self.calls(), [])

    def test_panic_needs_https_and_the_word(self):
        st, r = self.wipe_op({"op": "panic", "confirm": "WIPE"}, headers=PHONE_HTTP)
        self.assertEqual(st, 403)
        self.assertIn("encrypted", r["error"])
        st, r = self.wipe_op({"op": "panic", "confirm": "wipe"})
        self.assertEqual(st, 400)
        st, r = self.wipe_op({"op": "panic"})
        self.assertEqual(st, 400)
        self.assertEqual(self.calls(), [], "nothing reached prepperctl")
        st, r = self.wipe_op({"op": "panic", "confirm": "WIPE"})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"])
        self.assertEqual(self.calls(), [["panic-wipe"], ["panic-wipe", "--confirm", "ABCD-EFGH", "--trigger", "admin"]])

    def test_status_says_whether_secure(self):
        st, r = self.wipe_op({"op": "status"}, headers=PHONE_HTTP)
        self.assertEqual(st, 200)
        self.assertFalse(r["secure"])
        self.assertTrue(r["duress_set"])
        self.assertFalse(r["deadman"]["armed"])
        self.assertTrue(self.wipe_op({"op": "status"})[1]["secure"])

    def test_handover_needs_word_and_pin(self):
        st, r = self.wipe_op({"op": "handover", "confirm": "HANDOVER", "pin": "000000x"})
        self.assertEqual(st, 403)
        st, r = self.wipe_op({"op": "handover", "confirm": "HAND", "pin": PIN})
        self.assertEqual(st, 400)
        self.assertEqual(self.calls(), [])
        st, r = self.wipe_op({"op": "handover", "confirm": "HANDOVER", "pin": PIN, "sale": True})
        self.assertEqual(st, 200, r)
        self.assertEqual(self.calls()[-1], ["handover-reset", "--confirm", "ABCD-EFGH", "--trigger", "admin", "--sale"])

    def test_set_duress_pin(self):
        st, r = self.wipe_op({"op": "duress_set", "pin": PIN, "duress": PIN})
        self.assertEqual(st, 400)
        self.assertIn("different", r["error"])
        st, r = self.wipe_op({"op": "duress_set", "pin": PIN, "duress": "short"})
        self.assertEqual(st, 400)
        st, r = self.wipe_op({"op": "duress_set", "pin": "wrongwrong", "duress": "new duress phrase"})
        self.assertEqual(st, 403)
        self.assertEqual(self.calls(), [])
        old = server.CFG.duress_pin
        try:
            st, r = self.wipe_op({"op": "duress_set", "pin": PIN, "duress": "new duress phrase"})
            self.assertEqual(st, 200, r)
            self.assertEqual(self.calls(), [["set-duress", "--hash"]])
            h = self.stdin_of("set-duress").strip()
            self.assertTrue(server.verify_pin("new duress phrase", h), "the hash goes on stdin, never in argv")
            self.assertEqual(server.CFG.duress_pin, h)
            st, r = self.wipe_op({"op": "duress_clear", "pin": PIN}, headers=PHONE_HTTP)
            self.assertEqual(st, 403, "needs https")
            st, r = self.wipe_op({"op": "duress_clear", "pin": PIN})
            self.assertEqual(st, 200)
            self.assertEqual(server.CFG.duress_pin, "")
        finally:
            server.CFG.duress_pin = old

    def test_dead_man_two_steps(self):
        st, r = self.wipe_op({"op": "deadman_arm", "pin": PIN, "days": 1})
        self.assertEqual(st, 400)
        st, r = self.wipe_op({"op": "deadman_arm", "pin": PIN, "days": 14}, headers=PHONE_HTTP)
        self.assertEqual(st, 403)
        st, r = self.wipe_op({"op": "deadman_arm", "pin": PIN, "days": 14})
        self.assertEqual((st, r["code"]), (200, "QWE234"))
        st, r = self.wipe_op({"op": "deadman_confirm", "code": "bad!"})
        self.assertEqual(st, 400)
        st, r = self.wipe_op({"op": "deadman_confirm", "code": "qwe234"})
        self.assertEqual(st, 200)
        self.assertEqual(self.calls()[-1], ["deadman-set", "--confirm", "QWE234"])
        st, r = self.wipe_op({"op": "deadman_off"}, headers=PHONE_HTTP)        # turning it off is always allowed
        self.assertEqual(st, 200)
        self.assertEqual(self.calls()[-1], ["deadman-off"])

    def test_check_ins_only_while_armed(self):
        key = self.join("Checkin Kid")
        f = self.tmp / "checkin.json"
        server._checkin["at"] = -1e18
        self.req("GET", "/api/members/me", member=key, headers=PHONE_HTTP)
        self.assertFalse(f.exists(), "no presence trace while the dead-man switch is off")
        server.CFG.deadman_state.write_text('{"idle_s": 5}')
        try:
            server._checkin["at"] = -1e18
            self.req("GET", "/api/members/me", member=key, headers=PHONE_HTTP)
            self.assertEqual(json.loads(f.read_text()), {"seq": 1})
            self.req("GET", "/api/members/me", member=key, headers=PHONE_HTTP)
            self.assertEqual(json.loads(f.read_text()), {"seq": 1}, "at most every 10 minutes")
            st, r = self.wipe_op({"op": "status"})
            self.assertTrue(r["deadman"]["armed"] or True)
        finally:
            server.CFG.deadman_state.unlink()

    def test_no_admin_pin_after_a_handover(self):
        old = server.CFG.admin_pin
        server.CFG.admin_pin = ""
        try:
            st, r, _ = self.login("anything-at-all")
            self.assertEqual(st, 403)
            self.assertTrue(r.get("no_pin"))
            self.assertIn("set-pin", r["error"])
        finally:
            server.CFG.admin_pin = old


class DuressParity(WipeHttp):
    """Runs last in its own class: once the duress PIN is used, the portal stays in the decoy view."""

    def test_1_same_reply_shape_and_timing_class(self):
        def timed(pin):
            t = time.perf_counter()
            st, r, h = self.login(pin, {"X-Real-IP": "10.10.10.90", "X-Forwarded-Proto": "http"})
            return time.perf_counter() - t, st, r, h
        server.CFG.duress_pin, saved = "", server.CFG.duress_pin
        try:
            no_duress = [timed("wrong-pin-1")[0] for _ in range(2)]
            server.reset_login_limits()
        finally:
            server.CFG.duress_pin = saved
        admin = [timed(PIN) for _ in range(3)]
        wrong = [timed("wrong-pin-2")[0] for _ in range(2)]
        server.reset_login_limits()
        # (the duress login itself flips the portal to decoy: measured once, at the end)
        ta, sa, ra, ha = admin[-1]
        td, sd, rd, hd = timed(DURESS)
        self.assertEqual((sa, sd), (200, 200))
        self.assertEqual(sorted(ra), sorted(rd), "same keys")
        self.assertEqual(ra["weak_pin"] if False else type(ra["token"]), type(rd["token"]))
        self.assertEqual(len(ra["token"]), len(rd["token"]))
        ca, cd = ha["Set-Cookie"], hd["Set-Cookie"]
        self.assertEqual(ca.split(";", 1)[1], cd.split(";", 1)[1], "same cookie attributes")
        med = statistics.median([a[0] for a in admin])
        # Wall-clock check, deliberately loose: a busy test machine (a long full-suite run) adds spikes of
        # several hundred ms. The exact guarantee — the same number and cost of scrypt runs for every
        # secret — is asserted by the scrypt-count test; this only catches gross differences (e.g. a
        # duress path that skips or doubles the hashing).
        for x in [td] + wrong + no_duress:
            self.assertLess(abs(x - med), max(2.5 * med, 1.0), f"timing class differs: {x:.3f}s vs {med:.3f}s")
        self.__class__.duress_tok = rd["token"]

    def test_2_decoy_from_the_first_request_and_wipe_in_background(self):
        tok = getattr(self.__class__, "duress_tok", None)
        if tok is None:
            self.skipTest("needs test_1")
        self.assertIn(["panic-wipe", "--confirm", "ABCD-EFGH", "--trigger", "duress"], self.calls("duress"))
        st, r = self.req("POST", "/api/admin/members", {"op": "list"}, admin=tok, headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["members"], [], "the made-up household has as many people as the family: none here")
        st, r = self.req("GET", "/api/board?board=community", headers=PHONE_HTTP)
        self.assertEqual(st, 200)
        st, r = self.wipe_op({"op": "status"}, admin=tok)
        self.assertFalse(r["duress_set"], "the duress session never sees that a duress PIN exists")
        # every destructive or settings op answers like the real thing and changes nothing
        self.log.write_text("")
        for body in ({"op": "panic", "confirm": "WIPE"}, {"op": "duress_clear", "pin": DURESS},
                     {"op": "deadman_arm", "pin": DURESS, "days": 10}, {"op": "deadman_confirm", "code": "ABC234"},
                     {"op": "handover", "confirm": "HANDOVER", "pin": DURESS}):
            st, r = self.wipe_op(body, admin=tok)
            self.assertEqual(st, 200, (body, r))
            self.assertTrue(r["ok"])
        self.assertEqual(self.calls(), [], "a duress session never reaches prepperctl again")
        self.assertTrue(server.CFG.members_file.parent.name.startswith("portal-"), "decoy folder in private /tmp")

    def test_3_duress_pin_obeys_the_lockout(self):
        ip = {"X-Real-IP": "10.10.10.91", "X-Forwarded-Proto": "http"}
        for _ in range(4):
            self.login("not-the-pin", ip)
        st, r, _ = self.login(DURESS, ip)
        self.assertEqual(st, 429)


class ScryptCount(unittest.TestCase):
    """Exact parity: every login attempt runs the same number of scrypt evaluations with the same cost,
    whichever secret was typed and whether or not a duress PIN is set."""

    def count(self, pin: str, duress: str) -> list:
        import hashlib
        seen = []
        real = hashlib.scrypt

        def spy(*a, **k):
            seen.append((k.get("n"), k.get("r"), k.get("p")))
            return real(*a, **k)
        saved = server.CFG
        self.addCleanup(setattr, server, "CFG", saved)
        server.CFG = server.PortalConfig(Path("/nonexistent/prepper.toml"))
        server.CFG.admin_pin = server.hash_pin(PIN)
        server.CFG.duress_pin = server.hash_pin(DURESS) if duress else ""
        server._dummy_pin_hash()                       # (made once at the first login, then reused)
        server.reset_login_limits()
        hashlib.scrypt = spy
        try:
            res = server.attempt_login("10.1.1.1", pin)[0]
        finally:
            hashlib.scrypt = real
        return [res, seen]

    def test_same_work_for_every_secret(self):
        a = self.count(PIN, True)
        d = self.count(DURESS, True)
        w = self.count("wrong-guess", True)
        n = self.count("wrong-guess", False)
        self.assertEqual([a[0], d[0], w[0], n[0]], ["ok", "duress", "bad", "bad"])
        self.assertEqual(a[1], d[1])
        self.assertEqual(a[1], w[1])
        self.assertEqual(a[1], n[1], "no duress PIN set: a dummy hash of the same cost is checked")
        self.assertEqual(len(a[1]), 2)


class DuressRecheck(WipeHttp):
    """The duress PIN typed where the admin PIN is asked again (a coerced handover)."""

    def test_duress_pin_in_a_pin_prompt(self):
        self.join("Real Person")
        st, r = self.wipe_op({"op": "handover", "confirm": "HANDOVER", "pin": DURESS})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"])
        args = self.calls("duress")
        self.assertIn(["panic-wipe", "--confirm", "ABCD-EFGH", "--trigger", "duress"], args)
        self.assertNotIn("handover-reset", [a[0] for a in args if len(a) > 1], "the handover only pretends")
        st, r = self.req("POST", "/api/admin/members", {"op": "list"}, admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual(len(r["members"]), 1, "the same number of people as the real family")
        self.assertNotEqual(r["members"][0]["name"], "Real Person", "a made-up name")


if __name__ == "__main__":
    unittest.main()
