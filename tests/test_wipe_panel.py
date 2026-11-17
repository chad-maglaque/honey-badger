#!/usr/bin/env python3
"""
Front panel: Panic wipe and Hand over behind the panel lock + hold-to-confirm, and the panel duress
code (opens the panel like the real code, starts the wipe quietly, secret screens act empty).

    python3.11 tests/test_wipe_panel.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="wipe-panel-"))
os.environ.setdefault("PREPPER_SDR_DIR", str(TMP / "run-sdr"))
os.environ.setdefault("PREPPER_SDR_STATE", str(TMP / "state-sdr"))
os.environ.setdefault("PANEL_TIMEFMT", str(TMP / "timefmt"))
sys.path.insert(0, str(REPO / "software" / "panel"))
import panel  # noqa: E402

LOG = TMP / "ctl.log"
FAKE = TMP / "prepperctl"
FAKE.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
open({str(LOG)!r}, "a").write(json.dumps(args) + "\\n")
if args and args[0] in ("panic-wipe", "handover-reset") and "--confirm" not in args:
    print("This erases the family data")
    print("confirm-token: WXYZ-2345")
else:
    print("wipe: 10 steps done; radio: new random keys on the radio's private channels")
""")
FAKE.chmod(0o755)


class Disp:
    def __init__(self):
        self.qrs, self.texts = [], []

    def lines(self, rows, title=None, sel=None):
        self.texts.append((title, list(rows)))

    def qr(self, data, caption):
        self.qrs.append((data, caption))

    def sleep(self, on):
        pass


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TMP))
        LOG.write_text("")
        panel.CTL = str(FAKE)
        panel.API = "http://127.0.0.1:9"
        self.link = self.dir / "link.json"
        self.link.write_text(json.dumps({"url": "https://meshtastic.org/e/#FAMILY-PSK-SECRET", "at": 1}))
        self.pin = self.dir / "pin.json"
        self.pin.write_text(json.dumps({"pin": "482913", "at": 1}))
        self.lockfile = self.dir / "panel-lock.json"
        self.conf = self.dir / "prepper.toml"
        self.conf.write_text('[network]\nssid = "Smiths"\npassphrase = "OLDPASSWORD1"\n')

    def make(self):
        d = Disp()
        p = panel.Panel(d, {"network": {"ssid": "Smiths", "passphrase": "OLDPASSWORD1", "hostname": "badger"},
                            "portal": {"mesh_channel_link": str(self.link), "mesh_bt_pin_file": str(self.pin),
                                       "features_data": str(self.dir / "feat")},
                            "panel": {"lock_file": str(self.lockfile)}})
        p.spawn = lambda fn: fn()
        p.cfg_path = self.conf
        return p, d

    def calls(self):
        return [json.loads(x) for x in LOG.read_text().splitlines() if x.strip()]

    def enter(self, p, code):
        for digit in code:
            p.code_sel = int(digit)
            p.press()
        p.code_sel = p.code_choices().index("OK")
        p.press()

    def system(self, p):
        menu = dict(p.root())
        p.screen, p.stack = "menu", [("Menu", list(menu.items()))]
        return dict(menu["System"])


class WipeFromThePanel(PanelCase):
    def test_wipe_needs_code_confirm_and_hold(self):
        panel.PanelLock(self.lockfile).set("2468")
        p, d = self.make()
        sysm = self.system(p)
        self.assertIn("Panic wipe", sysm)
        self.assertIn("Hand over / sell", sysm)
        sysm["Panic wipe"]()
        self.assertEqual(p.screen, "code", "behind the panel lock")
        self.enter(p, "2468")
        self.assertEqual(p.screen, "confirm")
        self.assertEqual(p.sel, 0, "defaults to No")
        p.press()                                          # No
        self.assertEqual(self.calls(), [])
        sysm["Panic wipe"]()                               # unlocked for 5 minutes now
        p.sel = 1
        p.press()                                          # Yes → hold screen
        self.assertEqual(p.screen, "hold")
        p.press()                                          # a short press cancels
        self.assertNotEqual(p.screen, "hold")
        self.assertEqual(self.calls(), [])
        sysm["Panic wipe"]()
        p.sel = 1
        p.press()
        p.hold()                                           # held 3 s
        self.assertEqual(self.calls(), [["panic-wipe"], ["panic-wipe", "--confirm", "WXYZ-2345", "--trigger", "panel"]])
        self.assertTrue(any("Wiped" in " ".join(rows) for _, rows in d.texts))

    def test_without_a_lock_still_confirm_and_hold(self):
        p, d = self.make()
        sysm = self.system(p)
        sysm["Panic wipe"]()
        self.assertEqual(p.screen, "confirm")
        p.sel = 1
        p.press()
        self.assertEqual(p.screen, "hold")
        self.assertEqual(self.calls(), [])

    def test_sleep_cancels_hold(self):
        p, d = self.make()
        self.system(p)["Panic wipe"]()
        p.sel = 1
        p.press()
        p.last_input = time.monotonic() - 1000
        p.tick()
        self.assertEqual(p.screen, "home")
        p.hold()                                           # a hold on the home screen is shutdown, not a wipe
        self.assertNotIn("panic-wipe", [c[0] for c in self.calls()])

    def test_handover_asks_twice_then_hold(self):
        p, d = self.make()
        self.system(p)["Hand over / sell"]()
        p.sel = 1
        p.press()
        self.assertEqual(p.screen, "confirm", "a second question")
        p.sel = 1
        p.press()
        p.hold()
        self.assertEqual(self.calls()[-1], ["handover-reset", "--confirm", "WXYZ-2345", "--trigger", "panel"])

    def test_after_a_wipe_the_new_wifi_password_is_shown(self):
        p, d = self.make()
        self.conf.write_text('[network]\nssid = "Smiths"\npassphrase = "NEWW-PASS-WORD"\n')
        p.wifi_screen()
        self.assertIn("NEWW-PASS-WORD", d.qrs[-1][0])


class DuressCode(PanelCase):
    def test_set_rules(self):
        lock = panel.PanelLock(self.lockfile)
        with self.assertRaises(ValueError):
            lock.set_duress("1357")                        # no panel code yet
        lock.set("2468")
        with self.assertRaises(ValueError):
            lock.set_duress("2468")                        # must differ
        with self.assertRaises(ValueError):
            lock.set_duress("12")
        lock.set_duress("1357")
        self.assertTrue(lock.has_duress())
        raw = self.lockfile.read_text()
        self.assertNotIn("1357", raw)
        self.assertEqual(self.lockfile.stat().st_mode & 0o777, 0o600)
        lock.set("9999")                                   # changing the code keeps the duress code
        self.assertTrue(lock.has_duress())
        self.assertEqual(lock.check("1357")[0], "duress")
        lock.set("1357")                                   # … unless the new code IS the duress code
        self.assertFalse(lock.has_duress())
        self.assertEqual(lock.check("1357")[0], "ok")
        lock.set_duress("4444")
        lock.clear_duress()
        self.assertFalse(lock.has_duress())

    def test_duress_opens_like_the_code_and_wipes_quietly(self):
        lock = panel.PanelLock(self.lockfile)
        lock.set("2468")
        lock.set_duress("1357")
        p, d = self.make()
        menu = dict(p.root())
        p.screen, p.stack = "menu", [("Menu", list(menu.items()))]
        p.radio_link_qr()
        self.assertEqual(p.screen, "code")
        self.enter(p, "1357")
        self.assertIn(["panic-wipe", "--confirm", "WXYZ-2345", "--trigger", "panel-duress"], self.calls())
        self.assertNotEqual(p.screen, "code", "unlocked, exactly like the real code")
        self.assertFalse(any("FAMILY-PSK-SECRET" in q[0] for q in d.qrs), "no family key on a duress unlock")
        self.assertTrue(any("No radio link" in " ".join(rows) for _, rows in d.texts), "looks like nothing was set up")
        p.bt_pin()
        self.assertFalse(any("482913" in " ".join(rows) for _, rows in d.texts))
        self.assertEqual(p.approve_entries(), [])
        self.assertFalse(any("Wiped" in " ".join(rows) for _, rows in d.texts), "nothing says a wipe happened")
        n = len(self.calls())
        p.start_duress()                                   # (a second duress unlock: the wipe runs once)
        self.assertEqual(len(self.calls()), n, "the wipe is started once per panel run")

    def test_same_work_for_code_duress_and_wrong(self):
        import hashlib
        lock = panel.PanelLock(self.lockfile)
        lock.set("2468")

        def count(code, with_duress):
            if with_duress and not lock.has_duress():
                lock.set_duress("1357")
            if not with_duress:
                lock.clear_duress()
            seen = []
            real = hashlib.scrypt

            def spy(*a, **k):
                seen.append(k.get("n"))
                return real(*a, **k)
            hashlib.scrypt = spy
            try:
                st = lock.check(code)[0]
            finally:
                hashlib.scrypt = real
                d = json.loads(self.lockfile.read_text())
                d.update(fails=0, until=0, delay=0, mono_until=0)
                self.lockfile.write_text(json.dumps(d))
            return st, seen
        res = [count("2468", True), count("1357", True), count("0000", True), count("0000", False), count("2468", False)]
        self.assertEqual([r[0] for r in res], ["ok", "duress", "bad", "bad", "ok"])
        self.assertEqual(len({len(r[1]) for r in res}), 1, res)

    def test_duress_menu_needs_the_code(self):
        lock = panel.PanelLock(self.lockfile)
        lock.set("2468")
        p, d = self.make()
        p.lock_menu()
        rows = dict(p.stack[-1][1])
        rows["Duress code"]()
        self.assertEqual(p.screen, "code")
        self.enter(p, "2468")
        rows = dict(p.stack[-1][1])
        rows["Set duress code"]()
        self.enter(p, "1357")
        self.enter(p, "1357")
        self.assertTrue(panel.PanelLock(self.lockfile).has_duress())
        p.lock_now()
        p.lock_menu()
        dict(p.stack[-1][1])["Duress code"]()
        self.enter(p, "2468")
        dict(p.stack[-1][1])["Change duress code"]()
        self.enter(p, "2468")                              # same as the panel code: refused
        self.enter(p, "2468")
        self.assertEqual(panel.PanelLock(self.lockfile).check("2468")[0], "ok")


class KeyRefresh(PanelCase):
    def test_a_destroyed_panel_key_is_made_again_by_root(self):
        key, h = self.dir / "panel.key", self.dir / "panel.key.sha256"
        old = (panel.PANEL_KEY, panel.PANEL_KEY_HASH)
        panel.PANEL_KEY, panel.PANEL_KEY_HASH = key, h
        try:
            panel._key_cache.clear()
            panel.ensure_panel_key(key, h)
            first = panel.panel_headers()
            key.unlink()
            h.unlink()                                     # the wipe destroyed both
            if os.geteuid() == 0:
                second = panel.panel_headers()
                self.assertNotEqual(first, second)
            else:
                panel.ensure_panel_key(key, h)             # (what after_wipe does as root)
                panel._key_cache.clear()
                self.assertNotEqual(first, panel.panel_headers())
        finally:
            panel.PANEL_KEY, panel.PANEL_KEY_HASH = old
            panel._key_cache.clear()


class DeadManWarning(PanelCase):
    def test_home_screen_warns_within_two_days(self):
        st = self.dir / "dm.json"
        old = panel.DEADMAN_STATE
        panel.DEADMAN_STATE = st
        try:
            p, d = self.make()
            self.assertIsNone(p.deadman_page())
            self.conf.write_text(self.conf.read_text() + "\n[wipe]\ndeadman_days = 3\n")
            st.write_text(json.dumps({"idle_s": 3600}))
            panel._file_cache.clear()
            self.assertIsNone(p.deadman_page())
            st.write_text(json.dumps({"idle_s": 2 * 86400}))
            panel._file_cache.clear()
            page = p.deadman_page()
            self.assertIn("CHECK IN", page[0])
            self.assertIn("24 h", " ".join(page))
        finally:
            panel.DEADMAN_STATE = old


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
