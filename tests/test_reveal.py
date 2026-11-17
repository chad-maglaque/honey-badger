"""
"What this Badger reveals" (features/reveal.py): the live checklist over HTTP, who may see it (never
visitors), each one-tap fix, and the optional lines for other features (sealed records, panic wipe).
Run: python3.11 tests/test_reveal.py
"""
from __future__ import annotations

import json
import sys
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_posture import PHONE, P, PostureCase, server  # noqa: E402


class Reveal(PostureCase):
    NETWORK = 'ssid = "HoneyBadger"\npassphrase = ""\nsecurity = "auto"'
    EXTRA = 'journald_conf = "{}/journald.conf"'

    @classmethod
    def setUpClass(cls):
        cls.EXTRA = ""
        super().setUpClass()
        server.CFG.raw.setdefault("security", {})["journald_conf"] = str(cls.tmp / "journald.conf")
        server.CFG.raw["security"]["journal_dir"] = str(cls.tmp / "journal")
        server.CFG.raw["security"]["tls_dir"] = str(cls.tmp / "tls")

    def setUp(self):
        super().setUp()
        self.real_sealed = sys.modules.get("sealed")
        (self.tmp / "journald.conf").write_text("[Journal]\nStorage=volatile\n")
        for f in ("panel-lock-arm.json", "mesh-radio-req.json"):
            (self.tmp / f).unlink(missing_ok=True)
        (self.tmp / "features-data" / "reveal" / "acks.json").unlink(missing_ok=True)
        sys.modules.pop("wipe", None)

    def tearDown(self):
        if self.real_sealed is not None:
            sys.modules["sealed"] = self.real_sealed
        sys.modules.pop("wipe", None)

    def items(self, **kw):
        st, d = self.req("GET", "/api/reveal", admin=self.admin, headers=PHONE, **kw)
        self.assertEqual(st, 200, d)
        return {i["id"]: i for i in d["items"]}

    def fix(self, action):
        return self.req("POST", "/api/reveal/fix", {"action": action}, admin=self.admin, headers=PHONE)

    def test_never_for_visitors_or_members(self):
        self.assertEqual(self.req("GET", "/api/reveal", headers=PHONE)[0], 401)
        self.assertEqual(self.req("GET", "/api/reveal", member=self.member, headers=PHONE)[0], 401)
        self.assertEqual(self.req("POST", "/api/reveal/fix", {"action": "posture_guarded"}, headers=PHONE)[0], 401)
        self.assertEqual(self.req("GET", "/api/reveal", panel=True)[0], 200)

    def test_checklist_lines(self):
        it = self.items()
        for k in ("wifi_password", "wifi_name", "radio_position", "radio_name", "posture", "sharing", "public_traffic",
                  "panel_lock", "recovery_printed", "ca_key", "logs", "visitor_pages"):
            self.assertIn(k, it)
        self.assertEqual(it["wifi_password"]["state"], "warn")
        self.assertEqual(it["wifi_name"]["state"], "warn")            # "HoneyBadger"
        self.assertIsNone(it["wifi_name"]["fix"])
        self.assertTrue(it["wifi_name"]["how"])
        self.assertEqual(it["radio_position"]["state"], "ok")
        self.assertEqual(it["radio_name"]["state"], "info")           # radio not connected
        self.assertEqual(it["posture"]["state"], "ok")
        self.assertEqual(it["public_traffic"]["state"], "warn")       # !q for strangers by default
        self.assertEqual(it["panel_lock"]["state"], "warn")
        self.assertEqual(it["logs"]["state"], "ok")
        self.assertNotIn("wipe", it)                                  # no panic-wipe feature here

    def test_radio_name_from_bridge(self):
        (self.tmp / "run" / "mesh-peers.json").write_text(json.dumps({"updated": time.time(), "radio_names": {
            "long": "Ana's Survival Radio", "short": "ASR"}}))
        it = self.items()
        self.assertEqual(it["radio_name"]["state"], "warn")
        st, r = self.fix("radio_rename")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads((self.tmp / "mesh-radio-req.json").read_text())["op"], "rename")
        (self.tmp / "run" / "mesh-peers.json").write_text(json.dumps({"updated": time.time(), "radio_names": {
            "long": "Meshtastic 4f2a", "short": "4f2a"}}))
        self.assertEqual(self.items()["radio_name"]["state"], "ok")

    def test_fix_position_and_public(self):
        self.set_level("neighbourly")
        it = self.items()
        self.assertEqual((it["radio_position"]["state"], it["posture"]["state"]), ("warn", "warn"))
        st, r = self.fix("position_off")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads((self.tmp / "portal-settings.json").read_text())["mesh_public_position"], "off")
        self.assertEqual({i["id"]: i for i in r["items"]}["radio_position"]["state"], "ok")
        self.fix("public_quiet")
        s = json.loads((self.tmp / "portal-settings.json").read_text())
        self.assertEqual((s["mesh_commands"]["q"], s["mesh_commands"]["help"]), ("family", "family"))
        st, r = self.fix("posture_guarded")
        self.assertEqual(P.read(self.tmp / "posture.json")["level"], "guarded")

    def test_fix_not_offered_is_refused(self):
        for a in ("position_off", "rm -rf", "", None, "x" * 100, "stop_sharing:skills"):
            self.assertEqual(self.req("POST", "/api/reveal/fix", {"action": a}, admin=self.admin, headers=PHONE)[0], 400, a)

    def test_fix_wifi_and_panel_and_ack(self):
        st, r = self.fix("wifi_password")
        self.assertEqual(st, 200)
        self.assertIn("wifi-secure", self.ctl_log())
        st, r = self.fix("panel_lock")
        self.assertGreater(json.loads((self.tmp / "panel-lock-arm.json").read_text())["until"], time.time())
        st, r = self.fix("ack_recovery")
        self.assertEqual({i["id"]: i for i in r["items"]}["recovery_printed"]["state"], "ok")
        self.assertEqual(self.req("POST", "/api/reveal/ack", {"id": "other"}, admin=self.admin, headers=PHONE)[0], 400)

    def test_logs_persistent_detected_and_fixed(self):
        (self.tmp / "journald.conf").write_text("[Journal]\nStorage=persistent\n")
        self.assertEqual(self.items()["logs"]["state"], "warn")
        st, r = self.fix("logs_ram")
        self.assertIn("posture-apply", self.ctl_log())
        (self.tmp / "journald.conf").write_text("[Journal]\nStorage=volatile\n")
        (self.tmp / "journal").mkdir(exist_ok=True)
        (self.tmp / "journal" / "x").write_text("old")
        self.assertEqual(self.items()["logs"]["state"], "warn")       # journal files still on disk
        (self.tmp / "journal" / "x").unlink()

    def test_ca_key(self):
        (self.tmp / "tls").mkdir(exist_ok=True)
        (self.tmp / "tls" / "ca.key").write_text("k")
        self.assertEqual(self.items()["ca_key"]["state"], "warn")
        (self.tmp / "tls" / "ca.key").unlink()
        self.assertEqual(self.items()["ca_key"]["state"], "ok")

    def test_sharing_lines_and_stop(self):
        d = self.tmp / "features-data" / "circles"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({"share": {"skills": True, "aid": True}}))
        (d / "bridge.json").write_text(json.dumps({"share": {"skills": True, "aid": True}}))
        self.assertEqual(self.items()["sharing"]["state"], "ok")       # not in a circle: nothing goes out
        (self.tmp / "mesh-circle-slot.json").write_text(json.dumps({"ch": 2}))
        it = self.items()
        self.assertEqual((it["sharing_skills"]["state"], it["sharing_aid"]["state"]), ("warn", "warn"))
        self.fix("stop_sharing:skills")
        self.assertFalse(json.loads((d / "bridge.json").read_text())["share"]["skills"])
        self.assertNotIn("sharing_skills", self.items())
        self.set_level("lockdown")
        self.assertEqual(self.items()["sharing"]["title"], "Nothing shared beyond the Wi-Fi (Lockdown)")
        (self.tmp / "mesh-circle-slot.json").unlink()

    def test_visitors_line_and_fix(self):
        it = self.items()
        self.assertIn("visitor_pages", it)
        if it["visitor_pages"]["fix"]:
            self.fix("visitors_members")
            s = json.loads((self.tmp / "portal-settings.json").read_text())
            self.assertEqual(s["visitor_community"], "members")

    def test_sealed_and_wipe_lines_when_present(self):
        calls = []
        sealed = types.ModuleType("sealed")
        sealed.is_unlocked = lambda: True
        sealed.lock_all = lambda: calls.append("lock")
        sys.modules["sealed"] = sealed
        wipe = types.ModuleType("wipe")
        wipe.configured = lambda: False
        sys.modules["wipe"] = wipe
        it = self.items()
        self.assertEqual(it["sealed"]["state"], "warn")
        self.assertEqual(it["wipe"]["state"], "warn")
        self.fix("sealed_lock")
        self.assertEqual(calls, ["lock"])
        sealed.is_unlocked = lambda: False
        self.assertEqual(self.items()["sealed"]["state"], "ok")

        def broken():
            raise RuntimeError("x")
        sealed.is_unlocked = broken
        self.assertNotIn("sealed", self.items())                      # a broken module never breaks the list


if __name__ == "__main__":
    unittest.main()
