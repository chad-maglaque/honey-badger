#!/usr/bin/env python3
"""
Tests for os/bin/firstboot.sh against a fake boot partition.

The script's test seam (PREPPER_TEST_ROOT; ignored as root) prefixes every
path. The software tarball on the fake boot partition carries a stub
prepperctl and install.sh that log their arguments.
   python3.11 tests/test_firstboot.py -v
"""
from __future__ import annotations

import io
import os
import sys
import tarfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from os_helpers import BASH, REPO, Stubs, TempDirCase, example_config, run  # noqa: E402

FIRSTBOOT = REPO / "os/bin/firstboot.sh"

# Stub prepperctl: logs its argv (and stdin for set-pin) to $FB_LOG; exits $FB_CTL_RC_<verb>.
STUB_CTL = r'''#!/bin/bash
verb=$1
data=""
[ "$verb" = set-pin ] && [ $# -eq 1 ] && data=$(cat)
printf '%s\t%s\t%s\n' "ctl" "$*" "$data" >> "$FB_LOG"
rc_var="FB_CTL_RC_${verb//-/_}"
exit "${!rc_var:-0}"
'''
STUB_INSTALL = r'''#!/bin/bash
printf 'install\t%s\t\n' "$*" >> "$FB_LOG"
exit "${FB_INSTALL_RC:-0}"
'''
USER_DATA = "#cloud-config\nhostname: badger\nusers:\n  - name: prepper-admin\n    passwd: \"$6$salt$hash\"\n"
NETWORK_CONFIG = "network:\n  version: 2\n  wifis:\n    wlan0:\n      access-points:\n        \"Home\":\n          password: \"uplinksecret\"\n"


def tarball() -> bytes:
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tar:
        for name, body in (("os/bin/prepperctl", STUB_CTL), ("os/install.sh", STUB_INSTALL),
                           ("os/nm_wifi.py", (REPO / "os/nm_wifi.py").read_text())):
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    return bio.getvalue()


class FirstBoot(TempDirCase):
    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("the test seam is disabled for root")
        self.root = self.tmp / "root"
        self.boot = self.root / "boot/firmware"
        self.seed = self.boot / "prepper"
        self.seed.mkdir(parents=True)
        self.etc = self.root / "etc/prepper"
        self.state = self.root / "var/lib/prepper"
        self.log = self.tmp / "fb.log"
        self.log.touch()
        self.stubs = Stubs(self.tmp)
        self.config = example_config(network={"ssid": "Den", "passphrase": "wifisecret1"})
        (self.seed / "software.tar.gz").write_bytes(tarball())
        (self.seed / "prepper.toml").write_text(self.config)
        (self.seed / "admin_pin").write_text("246813")             # the builder writes no trailing newline
        (self.seed / "proxy.env").write_text("http_proxy=http://10.10.11.1:3128\nhttps_proxy=http://10.10.11.1:3128\n"
                                             "PREPPER_RELAY_ADDR=10.10.11.2/24\n")
        (self.seed / "trusted_keys").write_text("ssh-ed25519 AAAAC3Nza other\nnot a key\n")
        (self.seed / "firstboot.sh").write_text(FIRSTBOOT.read_text())
        (self.boot / "user-data").write_text(USER_DATA)
        (self.boot / "network-config").write_text(NETWORK_CONFIG)
        (self.boot / "meta-data").write_text("instance-id: badger-1\n")

    def firstboot(self, **env):
        e = self.stubs.env(PREPPER_TEST_ROOT=self.root, FB_LOG=self.log, **env)
        return run([BASH, str(FIRSTBOOT)], env=e)

    def ctl_calls(self):
        return [line.split("\t") for line in self.log.read_text().splitlines()]

    def assert_boot_clean(self):
        for f in ("prepper.toml", "admin_pin", "proxy.env"):
            self.assertFalse((self.seed / f).exists(), f"{f} left on the boot partition")
        self.assertEqual((self.boot / "user-data").read_text(), "#cloud-config\n")
        self.assertFalse((self.boot / "network-config").exists())
        self.assertTrue((self.boot / "meta-data").exists(), "meta-data keeps the instance-id")
        for p in self.boot.rglob("*"):
            if p.is_file():
                body = p.read_bytes()
                for secret in (b"wifisecret1", b"246813", b"uplinksecret", b"$6$salt"):
                    self.assertNotIn(secret, body, f"{secret!r} still in {p}")

    def test_success(self):
        r = self.firstboot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_boot_clean()
        self.assertEqual((self.etc / "prepper.toml").read_text(), self.config)
        self.assertEqual((self.etc / "prepper.toml").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.state / "ap-wlan0").exists(), "provisioning starts with wlan0 as the uplink")
        self.assertEqual((self.etc / "proxy.env").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.etc / "admin_pin.pending").exists(), "used PIN is scrubbed")
        self.assertFalse((self.seed / "software.tar.gz").exists())
        self.assertFalse((self.seed / "trusted_keys").exists())
        self.assertTrue((self.state / "firstboot.done").exists())
        self.assertTrue((self.state / "first-pull-pending").exists())
        calls = self.ctl_calls()
        pin_calls = [c for c in calls if c[1] == "set-pin"]
        self.assertEqual(pin_calls, [["ctl", "set-pin", "246813"]], "PIN on stdin, never as an argument")
        self.assertIn(["install", "", ""], calls)
        trusts = [c[1] for c in calls if c[1].startswith("trust")]
        self.assertEqual(trusts, ["trust ssh-ed25519 AAAAC3Nza other", "trust not a key"])
        apt = (self.root / "etc/apt/apt.conf.d/99prepper-proxy").read_text()
        self.assertIn('Acquire::http::Proxy "http://10.10.11.1:3128";', apt)
        self.assertIn(["--no-block", "reboot"], self.stubs.argvs("systemctl"))
        self.assertIn(["config", "--system", "http.proxy", "http://10.10.11.1:3128"], self.stubs.argvs("git"))
        self.assertTrue(any(a[:3] == ["con", "add", "type"] for a in self.stubs.argvs("nmcli")))
        self.assertTrue((self.root / "var/log/prepper-firstboot.log").exists())

    def test_stale_ap_marker_removed(self):
        self.state.mkdir(parents=True)
        (self.state / "ap-wlan0").touch()
        (self.state / "ap-deferred-since").touch()
        self.assertEqual(self.firstboot().returncode, 0)
        self.assertFalse((self.state / "ap-wlan0").exists())
        self.assertFalse((self.state / "ap-deferred-since").exists())

    def test_bad_trust_key_is_not_fatal(self):
        r = self.firstboot(FB_CTL_RC_trust="2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("could not trust key", r.stdout)
        self.assertTrue((self.state / "firstboot.done").exists())

    def test_install_failure_scrubs_and_retries(self):
        r = self.firstboot(FB_INSTALL_RC="7")
        self.assertEqual(r.returncode, 7)
        self.assert_boot_clean()                             # the EXIT trap scrubbed everything
        self.assertTrue((self.etc / "prepper.toml").exists(), "the retry needs the /etc copy")
        self.assertFalse((self.state / "firstboot.done").exists())
        self.assertIn("first boot FAILED", r.stdout)
        self.assertTrue(any(c[1].startswith("build-status error 0") for c in self.ctl_calls()))
        self.assertTrue((self.seed / "software.tar.gz").exists(), "kept for the retry")
        # next boot: no seed config any more, the /etc copy is used, and this time it works
        self.log.write_text("")
        r = self.firstboot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.state / "firstboot.done").exists())
        self.assertEqual((self.etc / "prepper.toml").read_text(), self.config)

    def test_failure_before_install_still_scrubs(self):
        # The PIN step fails and then tar breaks nothing; make the install itself
        # unreachable by removing bash's access to it: corrupt tarball → tar fails.
        (self.seed / "software.tar.gz").write_bytes(b"not a tarball")
        r = self.firstboot()
        self.assertNotEqual(r.returncode, 0)
        # nothing was copied yet (tar is the first step), so the seed config must still be there
        self.assertTrue((self.seed / "prepper.toml").exists(), "never scrub a secret before its copy exists")
        # but cloud-init's applied secrets are gone either way
        self.assertEqual((self.boot / "user-data").read_text(), "#cloud-config\n")
        self.assertFalse((self.boot / "network-config").exists())

    def test_pin_failure_keeps_pending_copy(self):
        r = self.firstboot(FB_CTL_RC_set_pin="2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.seed / "admin_pin").exists())
        self.assertTrue((self.etc / "admin_pin.pending").exists())
        self.assertEqual((self.etc / "admin_pin.pending").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len([c for c in self.ctl_calls() if c[1] == "set-pin"]), 2, "retried after install")

    def test_second_run_is_noop(self):
        self.assertEqual(self.firstboot().returncode, 0)
        self.log.write_text("")
        self.stubs.clear()
        (self.seed / "software.tar.gz").write_bytes(tarball())     # even with a seed present again
        r = self.firstboot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already done", r.stdout)
        self.assertEqual(self.ctl_calls(), [])
        self.assertEqual(self.stubs.calls("systemctl"), [])

    def test_missing_config_fails(self):
        (self.seed / "prepper.toml").unlink()
        r = self.firstboot()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no prepper.toml", r.stdout)

    def test_malformed_proxy_ignored(self):
        (self.seed / "proxy.env").write_text('http_proxy=\'http://x";\nAcquire::x "y\'\n')
        r = self.firstboot()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("malformed proxy", r.stdout)
        self.assertFalse((self.root / "etc/apt/apt.conf.d/99prepper-proxy").exists())

    def test_uplink_wifi_from_config(self):
        cfg = example_config(network={"uplink_wifi": [{"ssid": "Home $(id)", "psk": "pw 'quoted'"}]})
        (self.seed / "prepper.toml").write_text(cfg)
        r = self.firstboot(STUB_OUT_nmcli="wlan0:disconnected")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (prof,) = (self.root / "etc/NetworkManager/system-connections").glob("*.nmconnection")
        self.assertEqual(prof.stat().st_mode & 0o777, 0o600)
        self.assertIn("psk=pw 'quoted'\n", prof.read_text())
        self.assertIn(["--wait", "90", "connection", "up", "id", prof.stem], self.stubs.argvs("nmcli"))
        for _, args, _ in self.stubs.calls():
            self.assertFalse(any("quoted" in a for a in args), "Wi-Fi password in a command line")
        self.assertIn(["reg", "set", "US"], self.stubs.argvs("iw"))


if __name__ == "__main__":
    unittest.main()
