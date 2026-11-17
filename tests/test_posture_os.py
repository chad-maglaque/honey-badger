"""
The security posture on the OS side: prepperctl wifi-secure (Lockdown on a hotspot without a password →
a WPA2 password through the failsafe change; an explicit "owe"/"open" mode is put back by a switch-back),
prepperctl posture-apply, and os/render.py's journal setting (RAM unless Neighbourly + persistent_logs).
Run: python3.11 tests/test_posture_os.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "os"))
sys.path.insert(0, str(HERE.parent / "software" / "common"))
import posture as P  # noqa: E402
import render  # noqa: E402
from os_helpers import toml_value  # noqa: E402
from test_prepperctl import Ctl  # noqa: E402


class WifiSecure(Ctl):
    def net(self):
        return tomllib.loads(self.conf.read_text())["network"]

    def set_net(self, **kv):
        t = self.conf.read_text()
        for k, v in kv.items():
            t, n = re.subn(rf'(?m)^{k} = .*$', lambda _m, k=k, v=v: f"{k} = {toml_value(v)}", t, count=1)
            self.assertEqual(n, 1, k)
        self.write_conf(t)

    def pending(self):
        return self.root / "var/lib/prepper/wifi-rotate-pending.json"

    def test_owe_auto_gets_a_password_with_failsafe(self):
        self.set_net(passphrase="", security="auto")
        r = self.ok("wifi-secure", sudo_user="prepper")
        pw = [ln.split(": ", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("passphrase: ")][0]
        self.assertRegex(pw, r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual((self.net()["passphrase"], self.net()["security"]), (pw, "auto"))
        self.assertTrue(self.pending().exists())                    # the usual switch-back is armed
        runs = [" ".join(args) for _n, args, _i in self.stubs.calls("systemd-run")]
        self.assertTrue(any("--unit=prepper-wifi-revert" in a for a in runs), runs)

    def test_explicit_owe_becomes_auto_and_switch_back_restores_it(self):
        self.set_net(passphrase="", security="owe")
        self.ok("wifi-secure", sudo_user="prepper")
        self.assertEqual(self.net()["security"], "auto")
        self.assertTrue(self.net()["passphrase"])
        self.ok("wifi-revert")
        self.assertEqual((self.net()["security"], self.net()["passphrase"]), ("owe", ""))
        self.assertFalse((self.root / "var/lib/prepper/wifi-security-before").exists())

    def test_confirm_keeps_it(self):
        self.set_net(passphrase="", security="open")
        self.ok("wifi-secure", sudo_user="prepper")
        self.ok("wifi-confirm")
        self.assertEqual(self.net()["security"], "auto")
        self.assertFalse((self.root / "var/lib/prepper/wifi-security-before").exists())
        self.ok("wifi-revert")                                      # nothing pending: nothing changes
        self.assertEqual(self.net()["security"], "auto")

    def test_already_has_a_password(self):
        self.set_net(passphrase="OLDPASS-1234", security="auto")
        r = self.ok("wifi-secure", sudo_user="prepper")
        self.assertIn("already has a password", r.stdout)
        self.assertFalse(self.pending().exists())

    def test_pending_change_refused(self):
        self.set_net(passphrase="", security="auto")
        self.ok("wifi-secure", sudo_user="prepper")
        self.fails("wifi-secure", sudo_user="prepper", msg="waiting to be confirmed")

    def test_posture_apply_renders_the_journal(self):
        portal = self.root / "var/lib/prepper/portal"
        portal.mkdir(parents=True, exist_ok=True)
        self.ok("posture-apply")
        j = self.root / "etc/systemd/journald.conf.d/prepper.conf"
        self.assertIn("Storage=volatile", j.read_text())
        P.write(portal / "posture.json", "neighbourly", "t")
        self.write_conf(self.conf.read_text().replace('posture = "guarded"', 'posture = "guarded"\npersistent_logs = true'))
        (self.root / "var/log/journal").mkdir(parents=True)
        r = self.ok("posture-apply")
        self.assertIn("persistent", j.read_text())
        self.assertIn("logs: persistent", r.stdout)
        P.write(portal / "posture.json", "guarded", "t")
        self.ok("posture-apply")
        self.assertIn("Storage=volatile", j.read_text())
        self.assertFalse((self.root / "var/log/journal").exists())  # the old journal on disk is dropped
        self.assertIn("nothing to change", self.ok("posture-apply").stdout)


class Journald(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="posture-render-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_ram_unless_neighbourly_and_asked(self):
        c = {"security": {"persistent_logs": True}}
        self.assertIn("volatile", render.journald(c, self.root))           # guarded (no file)
        pf = self.root / "var/lib/prepper/portal/posture.json"
        P.write(pf, "neighbourly", "t")
        self.assertIn("persistent", render.journald(c, self.root))
        self.assertIn("volatile", render.journald({}, self.root))
        P.write(pf, "lockdown", "t")
        self.assertIn("volatile", render.journald(c, self.root))
        pf.write_text("junk")                                               # damaged: Lockdown → RAM
        self.assertIn("volatile", render.journald(c, self.root))

    def test_settings_file_read_under_root(self):
        P.write(self.root / "var/lib/prepper/portal/posture.json", "neighbourly", "t")
        (self.root / "var/lib/prepper/portal/portal-settings.json").write_text(json.dumps({"x": 1}))
        lv, vals = render.posture_now({}, self.root)
        self.assertEqual((lv, vals["public_position"]), ("neighbourly", "coarse"))


if __name__ == "__main__":
    unittest.main()
