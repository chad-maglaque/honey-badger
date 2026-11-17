#!/usr/bin/env python3
"""
Sealed records over HTTP, through the real portal (tests/redteam/harness.py): a family day, the owner's
setup, a restart (locked: write-only), unlocking, posture lockdown, and migrating an older Badger's
plaintext files. The forensic check is the red team's: grep every file under the data root.
Run: python3.11 tests/test_sealed_http.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "redteam"))
from harness import OPENSSL, PHONE_HTTP, PIN, PortalCase, server  # noqa: E402

import members as members_mod  # noqa: E402
import sealed  # noqa: E402

PHONE_HTTPS = {"X-Real-IP": "10.10.10.77", "X-Forwarded-Proto": "https"}
PW = "purple tractor sings loudly"
FEATS = ("supplies.py", "aidstation.py", "skills.py", "missing.py", "_community_common.py", "_vault_crypto.py")
SECRETS = ("house empty till 18:00", "Lucia Garcia", "false wall in the cellar", "Rosa Diaz", "needs insulin",
           "blue house at Oak", "call Maria at the gym")


def grep(folder: Path, needle: str) -> list[str]:
    n = needle.encode()
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and n in p.read_bytes())


def restart():
    """What a reboot does to the portal's memory: no keys, no caches."""
    sealed._instances.clear()
    sealed._default[0] = None
    server._members = None


@unittest.skipUnless(OPENSSL, "needs openssl")
class SealedCase(PortalCase):
    FEATURES = FEATS
    EXTRA_TOML = "[sealed]\nscrypt_n = 1024\n"

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        restart()

    def family_day(self):
        self.mom = self.join("Mom Garcia")
        self.lucia = self.join("Lucia Garcia (age 7)")
        server._last_post.clear()
        self.assertEqual(self.req("POST", "/api/board", {"board": "family", "text": "Leaving 14:00, house empty till 18:00"},
                                  member=self.mom, headers=PHONE_HTTP)[0], 200)
        st, r = self.req("POST", "/api/supplies/item", {"name": "Insulin pens", "category": "medical", "qty": 12,
                                                        "location": "behind the false wall in the cellar"},
                         member=self.mom, headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        self.pens = r["id"]
        st, r = self.req("POST", "/api/aidstation/entry", {"person": "Rosa Diaz", "needs": "needs insulin", "count": 2,
                                                           "help": ["water"]}, admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        self.aid_id = r["entry"]["id"]
        st, r = self.req("POST", "/api/skills/add", {"kind": "offer", "category": "medical", "title": "Nurse, 20 years",
                                                     "name": "Ana", "contact": "blue house at Oak & 3rd"}, headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        self.skill = (r["id"], r["token"])
        st, r = self.req("POST", "/api/missing/add", {"kind": "person", "name": "Tomas Ruiz", "description": "red jacket",
                                                      "contact": "call Maria at the gym"},
                         headers={"X-Real-IP": "10.10.10.78", "X-Forwarded-Proto": "http"})
        self.assertEqual(st, 200, r)
        self.missing = (r["id"], r["token"])

    def sealed_post(self, body, headers=PHONE_HTTPS, admin=True, member=None):
        return self.req("POST", "/api/sealed", body, admin=self.admin if admin else None, member=member, headers=headers)

    def assert_nothing_at_rest(self):
        for s in SECRETS:
            self.assertEqual(grep(self.tmp, s), [], f"{s!r} readable at rest")
        mem = json.loads((self.tmp / "members.json").read_text())
        self.assertFalse(any(d.get("last_seen") for m in mem["members"].values() for d in m.get("devices", [])))


class BeforeSetup(SealedCase):
    """Out of the box (no owner passphrase yet): sealed from day one, everything works as before."""

    def test_family_day_leaves_nothing_readable_and_everything_works(self):
        self.family_day()
        self.assert_nothing_at_rest()
        st, b = self.req("GET", "/api/board?board=family", member=self.mom, headers=PHONE_HTTP)
        self.assertEqual(b["messages"][-1]["text"], "Leaving 14:00, house empty till 18:00")
        self.assertEqual(b["messages"][-1]["name"], "Mom Garcia")
        self.assertFalse(b["sealed"]["locked"])
        sup = self.req("GET", "/api/supplies", member=self.mom, headers=PHONE_HTTP)[1]
        self.assertEqual(sup["items"][0]["location"], "behind the false wall in the cellar")
        self.assertNotIn("locked", sup)
        sk = self.req("GET", "/api/skills?q=oak", headers=PHONE_HTTP)[1]["entries"]
        self.assertEqual([e["contact"] for e in sk], ["blue house at Oak & 3rd"])        # search sees the contact
        mi = self.req("GET", "/api/missing?q=tomas", headers=PHONE_HTTP)[1]["posts"]
        self.assertEqual(mi[0]["contact"], "call Maria at the gym")
        st, s = self.req("GET", "/api/sealed", member=self.mom, headers=PHONE_HTTP)
        self.assertEqual((st, s["setup"], s["provisional"], s["unlocked"]), (200, False, True, True))
        self.assertEqual(self.req("GET", "/api/sealed", headers=PHONE_HTTP)[0], 401)        # visitors: nothing

    def test_who_may_set_it_up(self):
        st, r = self.sealed_post({"op": "setup", "passphrase": PW}, headers=PHONE_HTTP)
        self.assertEqual(st, 403)
        self.assertTrue(r.get("insecure"))
        mom = self.join("Mother Two")
        self.assertEqual(self.sealed_post({"op": "setup", "passphrase": PW}, admin=False, member=mom)[0], 401)
        self.assertEqual(self.sealed_post({"op": "setup", "passphrase": "short"})[0], 400)
        server.CFG.admin_pin = server.hash_pin("the admin pin as a passphrase")
        try:
            st, r = self.sealed_post({"op": "setup", "passphrase": "the admin pin as a passphrase"})
            self.assertEqual(st, 400)
            self.assertIn("PIN", r["error"])
        finally:
            server.CFG.admin_pin = server.hash_pin(PIN)
        self.assertEqual(self.sealed_post({"op": "bogus"})[0], 400)
        self.assertEqual(self.sealed_post({"op": "unlock", "secret": PW})[0], 409)          # not set up yet


class OwnerCycle(SealedCase):
    def test_setup_restart_locked_write_only_then_unlock(self):
        self.family_day()
        # ── setup at the Badger itself (loopback, not via nginx: secure) ──
        st, r = self.sealed_post({"op": "setup", "passphrase": PW}, headers={})
        self.assertEqual(st, 200, r)
        self.assertRegex(r["recovery_key"], r"^([A-Z2-7]{4}-){12}[A-Z2-7]{4}$")
        self.assertTrue(all(v == "ok" for v in r["migrated"].values()), r["migrated"])
        self.assertEqual(set(r["migrated"]), {"members", "family-board", "supplies", "aidstation", "skills", "missing"})
        self.assertFalse(r["status"]["recovery_ack"])
        self.assertEqual(self.sealed_post({"op": "setup", "passphrase": PW})[0], 409)
        self.assert_nothing_at_rest()

        # ── a restart: locked ──
        restart()
        st, s = self.req("GET", "/api/sealed", admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual((s["setup"], s["unlocked"], s["secure"]), (True, False, False))
        # auth still works (hashed keys in the clear); names don't show
        me = self.req("GET", "/api/members/me", member=self.mom, headers=PHONE_HTTP)
        self.assertEqual(me[0], 200)
        self.assertEqual(me[1]["member"]["name"], sealed.LOCKED_NAME)
        names = [m["name"] for m in self.admin_op({"op": "list"})[1]["members"]]
        self.assertNotIn("Mom Garcia", names)
        st, r = self.admin_op({"op": "invite", "name": "Uncle Ed"})
        self.assertEqual(st, 400)
        self.assertIn("locked", r["error"])
        # the family board: history hidden, new posts saved (sealed) and shown
        b = self.req("GET", "/api/board?board=family", member=self.mom, headers=PHONE_HTTP)[1]
        self.assertTrue(b["sealed"]["locked"])
        self.assertGreaterEqual(b["sealed"]["hidden"], 1)
        self.assertEqual(b["messages"], [])
        server._last_post.clear()
        self.assertEqual(self.req("POST", "/api/board", {"board": "family", "text": "Back home, all fine"},
                                  member=self.lucia, headers=PHONE_HTTP)[0], 200)
        b = self.req("GET", "/api/board?board=family", member=self.mom, headers=PHONE_HTTP)[1]
        self.assertEqual([m["text"] for m in b["messages"]], ["Back home, all fine"])
        self.assertEqual(self.req("GET", "/api/board?board=community", headers=PHONE_HTTP)[1].get("sealed"), None)
        # supplies: write-only
        sup = self.req("GET", "/api/supplies", member=self.mom, headers=PHONE_HTTP)[1]
        self.assertTrue(sup["locked"])
        self.assertEqual(sup["items"], [])
        st, r = self.req("POST", "/api/supplies/item", {"name": "Water jugs", "category": "water", "qty": 6, "unit": "gal",
                                                        "location": "under the stairs"}, member=self.mom, headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        self.assertTrue(r["locked"])
        jugs = r["id"]
        self.assertEqual([i["name"] for i in r["state"]["items"]], ["Water jugs"])          # what was added since
        self.assertEqual(self.req("POST", "/api/supplies/adjust", {"id": self.pens, "delta": 2, "use": True},
                                  member=self.mom, headers=PHONE_HTTP)[0], 200)            # an item it can't see
        self.assertEqual(self.req("GET", "/api/supplies/export.csv", member=self.mom, headers=PHONE_HTTP)[0], 423)
        self.assertEqual(self.req("GET", "/api/supplies/log", member=self.mom, headers=PHONE_HTTP)[0], 423)
        self.assertEqual(self.req("POST", "/api/supplies/item", {"name": ""}, member=self.mom, headers=PHONE_HTTP)[0], 400)
        # aid station: counts work, old names sealed, new records shown
        ent = self.req("GET", "/api/aidstation/entries", admin=self.admin, headers=PHONE_HTTP)[1]
        self.assertTrue(ent["sealed_locked"])
        self.assertEqual(ent["entries"][0]["person"], "🔒 sealed")
        self.assertEqual(self.req("GET", "/api/aidstation/summary", admin=self.admin, headers=PHONE_HTTP)[1]["people"], 2)
        st, r = self.req("POST", "/api/aidstation/entry", {"person": "Walk-in Joe", "needs": "a blanket"},
                         admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual((st, r["entry"]["person"]), (200, "Walk-in Joe"))
        self.assertEqual(self.req("POST", "/api/aidstation/entry", {"id": self.aid_id, "person": "🔒 sealed", "count": 3},
                                  admin=self.admin, headers=PHONE_HTTP)[0], 200)            # placeholder not saved
        self.assertEqual(self.req("GET", "/api/aidstation/export.csv", admin=self.admin, headers=PHONE_HTTP)[0], 423)
        # skills & missing: the public part searchable, the contact sealed
        sk = self.req("GET", "/api/skills?q=nurse", headers=PHONE_HTTP)[1]["entries"]
        self.assertEqual((sk[0]["title"], sk[0]["contact"], sk[0]["contact_sealed"]), ("Nurse, 20 years", "", True))
        mi = self.req("GET", "/api/missing?q=tomas", headers=PHONE_HTTP)[1]["posts"]
        self.assertTrue(mi[0]["contact_sealed"])
        st, r = self.req("POST", "/api/skills/edit", {"id": self.skill[0], "token": self.skill[1], "availability": "evenings"},
                         headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        st, r = self.req("POST", "/api/skills/add", {"title": "Generator", "contact": "ask at the church"},
                         headers={"X-Real-IP": "10.10.10.90", "X-Forwarded-Proto": "http"})
        self.assertEqual((st, r["entry"]["contact"]), (200, "ask at the church"))
        self.assertEqual(grep(self.tmp, "ask at the church"), [])
        self.assertEqual(grep(self.tmp, "under the stairs"), [])
        self.assertEqual(grep(self.tmp, "Back home, all fine"), [])
        self.assertEqual(grep(self.tmp, "Walk-in Joe"), [])

        # ── unlock: wrong passphrase, insecure transport, then right ──
        self.assertEqual(self.sealed_post({"op": "unlock", "secret": PW}, headers=PHONE_HTTP)[0], 403)
        st, r = self.sealed_post({"op": "unlock", "secret": "not it at all, sorry"})
        self.assertEqual(st, 403)
        st, r = self.sealed_post({"op": "unlock", "secret": PW})
        self.assertEqual((st, r["via"]), (200, "passphrase"), r)
        b = self.req("GET", "/api/board?board=family", member=self.mom, headers=PHONE_HTTP)[1]
        self.assertEqual([(m["name"], m["text"]) for m in b["messages"]],
                         [("Mom Garcia", "Leaving 14:00, house empty till 18:00"), ("Lucia Garcia (age 7)", "Back home, all fine")])
        sup = self.req("GET", "/api/supplies", member=self.mom, headers=PHONE_HTTP)[1]
        self.assertNotIn("locked", sup)
        by = {i["id"]: i for i in sup["items"]}
        self.assertEqual(by[self.pens]["qty"], 10.0)                    # the change made while locked, applied
        self.assertEqual(by[jugs]["location"], "under the stairs")
        self.assertIn(("Mom Garcia", "added", "Water jugs"), [(x["who"], x["what"], x.get("name")) for x in sup["log"]])
        self.assertFalse((self.tmp / "features-data" / "supplies" / "supplies.pending.jsonl").exists())
        ent = {e["id"]: e for e in self.req("GET", "/api/aidstation/entries", admin=self.admin, headers=PHONE_HTTP)[1]["entries"]}
        self.assertEqual((ent[self.aid_id]["person"], ent[self.aid_id]["needs"], ent[self.aid_id]["count"]),
                         ("Rosa Diaz", "needs insulin", 3))
        sk = {e["id"]: e for e in self.req("GET", "/api/skills", headers=PHONE_HTTP)[1]["entries"]}
        self.assertEqual((sk[self.skill[0]]["contact"], sk[self.skill[0]]["availability"]), ("blue house at Oak & 3rd", "evenings"))
        self.assertEqual(self.req("GET", "/api/missing?q=tomas", headers=PHONE_HTTP)[1]["posts"][0]["contact"],
                         "call Maria at the gym")
        self.assertEqual(self.req("GET", "/api/members/me", member=self.mom, headers=PHONE_HTTP)[1]["member"]["name"], "Mom Garcia")
        self.assertEqual(self.admin_op({"op": "invite", "name": "Uncle Ed"})[0], 200)
        self.assert_nothing_at_rest()
        self.assertEqual(grep(self.tmp, "Uncle Ed"), [])

        # ── "Lock now": any family member, even over plain http (no secret involved) ──
        st, r = self.sealed_post({"op": "lock"}, headers=PHONE_HTTP, admin=False, member=self.lucia)
        self.assertEqual((st, r["status"]["unlocked"]), (200, False))
        self.assertEqual(self.sealed_post({"op": "lock"}, headers=PHONE_HTTP, admin=False)[0], 401)
        self.assertEqual(self.req("GET", "/api/supplies", member=self.mom, headers=PHONE_HTTP)[1]["items"], [])

        # ── posture lockdown: forced lock, unlock only when the admin confirms ──
        self.sealed_post({"op": "unlock", "secret": PW})
        (self.tmp / "posture.json").write_text(json.dumps({"level": "lockdown"}))
        server.sealed_store()._posture = (0.0, None, "guarded")          # (don't wait for the 1 s re-check)
        st, s = self.req("GET", "/api/sealed", admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual((s["unlocked"], s["lockdown"], s["locked_reason"]), (False, True, "lockdown"))
        st, r = self.sealed_post({"op": "unlock", "secret": PW})
        self.assertEqual(st, 409)
        self.assertTrue(r["lockdown"])
        st, r = self.sealed_post({"op": "unlock", "secret": PW, "confirm_lockdown": True})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["status"]["lockdown_override"])
        (self.tmp / "posture.json").unlink()

        # ── recovery key over HTTP, and the acknowledgement ──
        self.assertEqual(self.sealed_post({"op": "ack_recovery"}, headers=PHONE_HTTP)[1]["status"]["recovery_ack"], True)
        st, r = self.sealed_post({"op": "new_recovery", "passphrase": PW})
        self.assertEqual(st, 200, r)
        rk = r["recovery_key"]
        restart()
        st, r = self.sealed_post({"op": "recover", "recovery_key": rk, "new": "a completely new passphrase"})
        self.assertEqual((st, r["status"]["unlocked"]), (200, True), r)
        st, r = self.sealed_post({"op": "change", "old": "a completely new passphrase", "new": PW})
        self.assertEqual(st, 200, r)


class WrongPassphraseLockout(SealedCase):
    def test_lockout(self):
        self.assertEqual(self.sealed_post({"op": "setup", "passphrase": PW}, headers={})[0], 200)
        restart()
        codes = [self.sealed_post({"op": "unlock", "secret": "wrong wrong wrong wrong"})[0] for _ in range(sealed.FAILS)]
        self.assertEqual(codes, [403] * sealed.FAILS)
        st, r = self.sealed_post({"op": "unlock", "secret": PW})
        self.assertEqual(st, 429)
        self.assertGreater(r["wait"], 0)
        self.assertEqual(self.sealed_post({"op": "unlock", "secret": PW}, headers={})[0], 200)   # the Badger itself
        self.assertEqual(self.sealed_post({"op": "unlock", "secret": "x" * 500})[0], 400)
        self.assertEqual(self.sealed_post({"op": "unlock", "secret": 12345})[0], 400)


class MigrateOlderBadger(SealedCase):
    """An older Badger's plaintext files: still readable, and all sealed (old copies zeroed) at setup."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t = cls.tmp
        plain = members_mod.Members(t / "members.json")                 # no sealer: the old format
        code = plain.create_invite(name="Lucia Garcia (age 7)")["code"]
        _, _, cls.key = plain.join(code, "", "Lucia's tablet")
        (t / "family-board.jsonl").write_text(json.dumps({"at": "2026-11-01T10:00:00Z", "name": "Dad",
                                                          "text": "house empty till 18:00", "via": "local"}) + "\n")
        sup = t / "features-data" / "supplies"
        sup.mkdir(parents=True, exist_ok=True)
        (sup / "supplies.json").write_text(json.dumps({"items": {"abcdefgh1": {
            "name": "Insulin pens", "category": "medical", "qty": 3, "location": "behind the false wall in the cellar"}}}))
        sk = t / "features-data" / "skills"
        sk.mkdir(parents=True, exist_ok=True)
        (sk / "skills.json").write_text(json.dumps({"v": 1, "entries": [{"id": "skill00001", "kind": "offer",
            "category": "medical", "title": "Nurse", "contact": "blue house at Oak & 3rd", "updated": 4e9}]}))
        aid = t / "features-data" / "aidstation"
        aid.mkdir(parents=True, exist_ok=True)
        import time
        (aid / "log.json").write_text(json.dumps({"v": 1, "entries": [{"id": "aid0000001", "at": time.time() - 60,
            "person": "Rosa Diaz", "needs": "needs insulin", "count": 1, "help": [], "followup": True,
            "followup_done": {"at": time.time(), "by": "Volunteer Vic"}, "by": "Admin", "by_id": "admin"}]}))

    def test_setup_migrates_and_scrubs(self):
        self.assertTrue(grep(self.tmp, "false wall in the cellar"))      # (the old Badger's state)
        self.assertEqual(self.req("GET", "/api/members/me", member=self.key, headers=PHONE_HTTP)[1]["member"]["name"],
                         "Lucia Garcia (age 7)")
        st, r = self.sealed_post({"op": "setup", "passphrase": PW}, headers=PHONE_HTTPS)
        self.assertEqual(st, 200, r)
        self.assertTrue(all(v == "ok" for v in r["migrated"].values()), r["migrated"])
        for s in SECRETS + ("Volunteer Vic",):
            self.assertEqual(grep(self.tmp, s), [], f"{s!r} still readable after setup")
        self.assertEqual(list(self.tmp.rglob(".*.scrub")), [])
        # and everything still reads the same (unlocked by the setup)
        self.assertEqual(self.req("GET", "/api/members/me", member=self.key, headers=PHONE_HTTP)[1]["member"]["name"],
                         "Lucia Garcia (age 7)")
        b = self.req("GET", "/api/board?board=family", member=self.key, headers=PHONE_HTTP)[1]
        self.assertEqual(b["messages"][0]["text"], "house empty till 18:00")
        sup = self.req("GET", "/api/supplies", member=self.key, headers=PHONE_HTTP)[1]
        self.assertEqual(sup["items"][0]["location"], "behind the false wall in the cellar")
        self.assertEqual(self.req("GET", "/api/skills", headers=PHONE_HTTP)[1]["entries"][0]["contact"], "blue house at Oak & 3rd")
        e = self.req("GET", "/api/aidstation/entries", admin=self.admin, headers=PHONE_HTTP)[1]["entries"][0]
        self.assertEqual((e["person"], e["followup_done"]["by"]), ("Rosa Diaz", "Volunteer Vic"))
        # a plain line the mesh bridge appends later is sealed the next time the board is read
        with open(self.tmp / "family-board.jsonl", "a") as fh:
            fh.write(json.dumps({"at": "2026-11-02T10:00:00Z", "name": "radio", "text": "radio says hello there",
                                 "via": "mesh-in"}) + "\n")
        b = self.req("GET", "/api/board?board=family", member=self.key, headers=PHONE_HTTP)[1]
        self.assertEqual(b["messages"][-1]["text"], "radio says hello there")
        self.assertEqual(grep(self.tmp, "radio says hello there"), [])


if __name__ == "__main__":
    unittest.main()
