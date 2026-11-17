#!/usr/bin/env python3
"""
Tests for os/bin/prepperctl, run unprivileged against a scratch root.

The script's test seam (PREPPER_TEST_ROOT, PREPPER_TEST_PY; ignored as root)
prefixes every path; systemctl, nmcli, ip, nft … are stubs on PATH that log
their arguments.   python3.11 tests/test_prepperctl.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from os_helpers import BASH, PY, REPO, STUB_NAMES, Stubs, TempDirCase, example_config, run  # noqa: E402

CTL = REPO / "os/bin/prepperctl"
GOOD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq2nB3mT3Jv8Z0f0w6m9dYl0C5yqk1wqf2y3x9b8r6Z other-badger"
SENSITIVE = {   # verb → valid-looking arguments
    "trust": [GOOD_KEY],
    "trust-maintainer": [GOOD_KEY],
    "self-update-hook": [],
    "finish-provisioning": [],
    "first-pull": [],
    "build-status": ["done", "100", "Ready"],
    "set-pin": ["246813"],
    "gen-key": [],
    "gen-tls": [],
}


class Ctl(TempDirCase):
    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("the test seam is disabled for root; run the tests as a normal user")
        self.root = self.tmp / "root"
        for d in ("etc/prepper", "etc/default", "var/lib/prepper", "media", "run", "srv/prepper/zim",
                  "boot/firmware/prepper"):
            (self.root / d).mkdir(parents=True)
        self.make_opt()
        self.conf = self.root / "etc/prepper/prepper.toml"
        self.write_conf(example_config())
        self.conf.chmod(0o640)
        self.stubs = Stubs(self.tmp)

    def make_opt(self):
        """opt/prepper: the real os/ scripts and the portal (hash-pin), but a
        stub install.sh and upd.py that only log (FAKE_LOG) and exit FAKE_RC."""
        opt = self.root / "opt/prepper"
        (opt / "os").mkdir(parents=True)
        for f in ("render.py", "conf_write.py", "nm_wifi.py", "prepper.toml.example", "bin", "systemd"):
            (opt / "os" / f).symlink_to(REPO / "os" / f)
        (opt / "software/upd").mkdir(parents=True)
        (opt / "software/portal").symlink_to(REPO / "software/portal")
        self.fake_log = self.tmp / "fake.log"
        self.fake_log.touch()
        fake = ('#!/bin/bash\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$FAKE_LOG"\n'
                'exit "${FAKE_RC:-0}"\n')
        for f in (opt / "os/install.sh", opt / "software/upd/upd.py"):
            f.write_text(fake); f.chmod(0o755)
        (opt / "software/upd/upd.py").write_text(
            f"#!{PY}\nimport os, sys\nopen(os.environ['FAKE_LOG'], 'a').write('upd.py ' + ' '.join(sys.argv[1:]) + '\\n')\n"
            "sys.exit(int(os.environ.get('FAKE_UPD_RC', '0')))\n")

    def fake_calls(self):
        return self.fake_log.read_text().splitlines()

    def write_conf(self, text: str):
        self.conf.write_text(text)

    def ctl(self, *args, sudo_user: str | None = None, input: str | None = None, **env):
        env.setdefault("STUB_OUT_iw", "Interface x\n\ttype AP")        # hostapd "serves" an AP
        env.setdefault("FAKE_LOG", self.fake_log)
        e = self.stubs.env(PREPPER_TEST_ROOT=self.root, PREPPER_TEST_PY=PY, **env)
        if sudo_user:
            e["SUDO_USER"] = sudo_user
        return run([BASH, str(CTL), *args], env=e, input=input)

    def ok(self, *args, **kw):
        r = self.ctl(*args, **kw)
        self.assertEqual(r.returncode, 0, f"{args}: {r.stdout}{r.stderr}")
        return r

    def fails(self, *args, msg: str | None = None, rc: int | None = None, **kw):
        r = self.ctl(*args, **kw)
        self.assertNotEqual(r.returncode, 0, f"{args} should fail: {r.stdout}{r.stderr}")
        if rc is not None:
            self.assertEqual(r.returncode, rc, r.stderr)
        if msg:
            self.assertIn(msg, r.stdout + r.stderr)
        return r

    def snapshot(self) -> dict:
        out = {}
        for p in sorted(self.root.rglob("*")):
            if "opt" in p.relative_to(self.root).parts[:1]:
                continue
            if p.is_file():
                out[str(p.relative_to(self.root))] = p.read_bytes()
        return out


class Basics(Ctl):
    def test_unknown_verb_fails(self):
        for verb in ("frobnicate", "Set-Pin", "--help-me", "../net-up", " "):
            with self.subTest(verb=verb):
                self.fails(verb, msg="unknown verb")
        self.assertEqual(self.stubs.calls(), [])

    def test_help_succeeds(self):
        r = self.ok("help")
        self.assertIn("verbs:", r.stdout)
        self.ok()                                        # no verb = help

    def test_control_characters_and_overlong_refused(self):
        self.fails("service", "kiwix\n", "start", msg="control characters")
        self.fails("add-peer", "http://a/" + "b" * 600, msg="too long")
        self.assertEqual(self.stubs.calls(), [])


class Guard(Ctl):
    def test_portal_refused_for_every_sensitive_verb(self):
        before = self.snapshot()
        for verb, args in SENSITIVE.items():
            with self.subTest(verb=verb):
                r = self.fails(verb, *args, sudo_user="prepper", rc=2)
                self.assertIn("not available to the portal", r.stderr)
        self.assertEqual(self.stubs.calls(), [], "no system command may run")
        self.assertEqual(self.snapshot(), before, "no file may change")

    def test_admin_sudo_allowed(self):
        self.ok("build-status", "content", "40", "Downloading", sudo_user="alice")
        self.ok("trust", GOOD_KEY, sudo_user="alice")

    def test_portal_may_use_everyday_verbs(self):
        self.ok("power-mode", "balanced", sudo_user="prepper")
        self.ok("update-check", sudo_user="prepper")
        self.ok("service", "kiwix", "restart", sudo_user="prepper")


class Service(Ctl):
    def test_valid(self):
        self.ok("service", "llama", "stop")
        self.assertEqual(self.stubs.argvs("systemctl"), [["stop", "llama-server"], ["disable", "llama-server"]])

    def test_bad_names_never_reach_systemctl(self):
        for name in ("../etc/passwd", "--now", "ssh", "kiwix-serve", "kiwix;reboot", ""):
            with self.subTest(name=name):
                self.fails("service", name, "start")
        for act in ("--force", "enable", "start;reboot"):
            with self.subTest(act=act):
                self.fails("service", "kiwix", act, msg="bad action")
        self.assertEqual(self.stubs.calls("systemctl"), [])


class BuildStatus(Ctl):
    def read(self):
        return json.loads((self.root / "var/lib/prepper/build.json").read_text())

    def test_valid_and_sanitised(self):
        self.ok("build-status", "content", "42", 'Half "way" \\ there')
        b = self.read()
        self.assertEqual((b["phase"], b["pct"]), ("content", 42))
        self.assertNotIn('"', b["message"]); self.assertNotIn("\\", b["message"])
        self.assertEqual((self.root / "var/lib/prepper/build.json").stat().st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in (self.root / "var/lib/prepper").iterdir()], ["build.json"])

    def test_bad_values(self):
        for args in (("finished", "1"), ("done", "101"), ("done", "-1"), ("done", "1e2"), ("done", "abc"),
                     ("done", "1000"), ("--phase", "1")):
            with self.subTest(args=args):
                self.fails("build-status", *args)
        self.assertFalse((self.root / "var/lib/prepper/build.json").exists())

    def test_symlinked_state_dir_refused(self):
        state = self.root / "var/lib/prepper"
        shutil.rmtree(state)
        (self.tmp / "elsewhere").mkdir()
        state.symlink_to(self.tmp / "elsewhere")
        self.fails("build-status", "done", "100", msg="symlink")

    def test_done_brings_up_deferred_ap(self):
        self.ok("build-status", "done", "100", "Ready", STUB_RC_systemctl="0")   # hostapd "active": no net-up
        self.assertEqual(self.stubs.argvs("systemctl"), [["is-active", "-q", "prepper-hostapd"]])


class ConfWrite(Ctl):
    """os/conf_write.py (prepperctl conf_write) directly, then through add-peer."""

    def cw(self, text: str):
        return run([PY, str(REPO / "os/conf_write.py"), str(self.conf)], input=text)

    def test_atomic_keeps_mode_and_group(self):
        groups = [g for g in os.getgroups() if g != os.getegid()]
        if groups:
            os.chown(self.conf, -1, groups[0])
        st = self.conf.stat()
        r = self.cw(example_config(network={"ssid": "New"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        st2 = self.conf.stat()
        self.assertEqual(st2.st_mode & 0o7777, 0o640)
        self.assertEqual((st2.st_uid, st2.st_gid), (st.st_uid, st.st_gid))
        self.assertNotEqual(st2.st_ino, st.st_ino, "replaced by rename, not rewritten in place")
        self.assertEqual(tomllib.loads(self.conf.read_text())["network"]["ssid"], "New")
        self.assertEqual(sorted(p.name for p in self.conf.parent.iterdir()), ["prepper.toml"])

    def test_refuses_empty_and_unparseable(self):
        before = self.conf.read_text()
        for text in ("", "   \n", "[network\nssid=", "a = 1\na = 2\n"):
            with self.subTest(text=text):
                r = self.cw(text)
                self.assertEqual(r.returncode, 1)
                self.assertIn("refusing", r.stderr)
                self.assertEqual(self.conf.read_text(), before)
        self.assertFalse(Path(str(self.conf) + ".tmp").exists())

    def test_planted_tmp_symlink_not_followed(self):
        victim = self.tmp / "victim"
        victim.write_text("keep")
        Path(str(self.conf) + ".tmp").symlink_to(victim)
        self.assertEqual(self.cw(example_config()).returncode, 0)
        self.assertEqual(victim.read_text(), "keep")

    def test_add_peer(self):
        self.ok("add-peer", "http://10.10.10.11/sync")
        self.ok("add-peer", "http://10.10.10.11/sync")         # no duplicate
        self.ok("add-peer", "https://[fd00::2]:8443/sync")
        c = tomllib.loads(self.conf.read_text())
        self.assertEqual(c["upd"]["peers"], ["http://10.10.10.11/sync", "https://[fd00::2]:8443/sync"])
        self.assertEqual(self.conf.stat().st_mode & 0o777, 0o640)

    def test_add_peer_rejects(self):
        before = self.conf.read_text()
        for peer in ("file:///etc/shadow", "http://x/$(id)", 'http://x/"]\nadmin_pin = "', "--peer",
                     "http://a b/", "ftp://x/"):
            with self.subTest(peer=peer):
                self.fails("add-peer", peer)
        self.assertEqual(self.conf.read_text(), before)


class SetPin(Ctl):
    def pin_hash(self):
        return tomllib.loads(self.conf.read_text())["portal"]["admin_pin"]

    def verify(self, pin):
        sys.path.insert(0, str(REPO / "software" / "portal"))
        os.environ.setdefault("PORTAL_QUIET", "1")
        import server
        return server.verify_pin(pin, self.pin_hash())

    def test_policy(self):
        for bad in ("12345", "abc def1", "short1!", "a" * 129, " leading space ok?", "--help", "١٢٣٤٥٦", ""):
            with self.subTest(pin=bad):
                self.fails("set-pin", input=bad + "\n", msg="nothing was changed")
        self.assertEqual(self.pin_hash(), "")
        r = self.ok("set-pin", input="123456\n")           # digits only: accepted, with a warning
        self.assertIn("warning", r.stderr.lower())
        h6 = self.pin_hash()
        self.assertTrue(h6.startswith("scrypt$"), h6)
        self.assertTrue(self.verify("123456"))
        self.ok("set-pin", input="correct horse battery staple\n")
        self.assertNotEqual(self.pin_hash(), h6)
        self.assertTrue(self.verify("correct horse battery staple"))
        self.assertEqual(self.conf.stat().st_mode & 0o777, 0o640)
        self.assertIn(["restart", "prepper-portal"], self.stubs.argvs("systemctl"))

    def test_never_from_argv(self):
        # an argument shows in `ps`: refused, and nothing changes
        self.fails("set-pin", "correct horse battery", msg="never as an argument")
        self.assertEqual(self.pin_hash(), "")
        seg = CTL.read_text()[CTL.read_text().index("  set-pin)"):CTL.read_text().index("  upgrade-pin)")]
        self.assertNotIn('hash-pin "$pin"', seg)

    def test_from_stdin_without_newline(self):
        self.ok("set-pin", input="246813")              # firstboot: set-pin < admin_pin.pending
        self.assertTrue(self.pin_hash().startswith("scrypt$"))

    def test_inserts_missing_line(self):
        self.write_conf(example_config().replace('admin_pin = ""', "# (no pin line)"))
        self.ok("set-pin", input="246813\n")
        self.assertTrue(self.pin_hash().startswith("scrypt$"))

    def test_upgrade_pin_rehashes_the_same_secret_only(self):
        sys.path.insert(0, str(REPO / "software" / "portal"))
        import server
        legacy = server.legacy_hash_pin("246813", "abcd1234")
        self.write_conf(example_config().replace('admin_pin = ""', f'admin_pin = "{legacy}"'))
        before = self.conf.read_text()
        self.fails("upgrade-pin", sudo_user="prepper", input="999999\n")     # wrong PIN: no oracle, no change
        self.assertEqual(self.conf.read_text(), before)
        self.ok("upgrade-pin", sudo_user="prepper", input="246813\n")
        self.assertTrue(self.pin_hash().startswith("scrypt$"))
        self.assertTrue(self.verify("246813"))
        self.assertNotIn(["restart", "prepper-portal"], self.stubs.argvs("systemctl"))
        now = self.conf.read_text()
        self.fails("upgrade-pin", sudo_user="prepper", input="246813\n")     # already scrypt: nothing to do
        self.assertEqual(self.conf.read_text(), now)


class Paths(Ctl):
    def test_update_import(self):
        bundle = self.root / "media/usb-sda1/prepper-bundle"
        bundle.mkdir(parents=True)
        self.ok("update-import", str(bundle))
        (args,) = self.stubs.argvs("systemd-run")
        self.assertEqual(args[-2:], ["import", str(bundle.resolve())])
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.root / "media/usb-sda1/link").symlink_to(outside)
        for src in (str(outside), str(self.root / "media/usb-sda1/../../etc/prepper"),
                    str(self.root / "media/usb-sda1/link"), "--now", str(self.root / "media"),
                    str(self.root / "media/missing")):
            with self.subTest(src=src):
                self.fails("update-import", src)
        self.assertEqual(len(self.stubs.argvs("systemd-run")), 1)

    def test_update_export(self):
        (self.root / "media/usb-sda1").mkdir()
        mp = str(self.root / "media/usb-sda1")
        self.ok("update-export", mp + "/prepper-bundle", STUB_OUT_findmnt=mp)
        self.assertIn(["-n", "-o", "TARGET", "-T", mp], self.stubs.argvs("findmnt"))
        # nothing mounted there: the folder is on the system disk → refused
        self.fails("update-export", mp + "/prepper-bundle", STUB_OUT_findmnt="/", msg="no USB drive is mounted")
        self.fails("update-export", str(self.root / "media/nothere/b"), STUB_OUT_findmnt=mp, msg="does not exist")
        for dest in (str(self.root / "etc/x"), str(self.root / "media/../etc"), str(self.root / "media/"),
                     "-rf", "/media/usb-sda1"):
            with self.subTest(dest=dest):
                self.fails("update-export", dest)

    def test_usb_eject(self):
        mp = str(self.root / "media/usb-sda1")
        self.ok("usb-eject", "usb-sda1", STUB_OUT_findmnt=mp)
        self.assertIn(["--", mp], self.stubs.argvs("umount"))
        self.stubs.clear()
        for dev in ("../etc", "usb-sda1/../../", "-l", "/dev/../etc/x", ".hidden", "a b", "/dev/null0"):
            with self.subTest(dev=dev):
                self.fails("usb-eject", dev, STUB_OUT_findmnt=mp)
        self.fails("usb-eject", "usb-sda1", msg="not a USB mount", STUB_OUT_findmnt="/")
        self.assertEqual(self.stubs.calls("umount"), [])


class Uplink(Ctl):
    def setUp(self):
        super().setUp()
        self.write_conf(example_config(network={"ap_iface": "wlan1"}))
        self.ok("apply-config")
        self.stubs.clear()

    def profiles(self):
        d = self.root / "etc/NetworkManager/system-connections"
        return {p.name: p for p in d.glob("*.nmconnection")} if d.exists() else {}

    def assert_secret_not_in_argv(self, secret):
        for name, args, _ in self.stubs.calls():
            self.assertFalse(any(secret in a for a in args), f"{secret!r} in {name} argv")

    def test_join_password_on_stdin(self):
        self.ok("uplink-wifi", "Home Net", input="hunter2 hunter2\n")
        (prof,) = self.profiles().values()
        self.assertEqual(prof.stat().st_mode & 0o777, 0o600)
        text = prof.read_text()
        self.assertIn("psk=hunter2 hunter2\n", text)
        self.assertIn("ssid=" + "".join(f"{b};" for b in b"Home Net") + "\n", text)
        self.assertIn("interface-name=wlan0", text)
        self.assertIn(["connection", "reload"], self.stubs.argvs("nmcli"))
        up = [a for a in self.stubs.argvs("nmcli") if "up" in a]
        self.assertEqual(up, [["--wait", "60", "connection", "up", "id", prof.stem]])
        self.assert_secret_not_in_argv("hunter2")

    def test_legacy_argv_password_still_works(self):
        self.ok("uplink-wifi", "Home", "password1")
        self.assertIn("psk=password1", next(iter(self.profiles().values())).read_text())
        self.assert_secret_not_in_argv("password1")      # not passed on to nmcli at least

    def test_open_network(self):
        self.ok("uplink-wifi", "Library")
        text = next(iter(self.profiles().values())).read_text()
        self.assertNotIn("wifi-security", text)

    def test_hostile_ssid_and_psk(self):
        self.ok("uplink-wifi", "a]\\b;c=d [x]", input=" \\lead space\n")
        text = next(iter(self.profiles().values())).read_text()
        self.assertIn("psk=\\s\\\\lead space\n", text)     # GKeyFile escapes
        self.assertEqual(text.count("[wifi]"), 1)
        self.fails("uplink-wifi", "Home", input="pass\x01word1\n")

    def test_limits(self):
        self.fails("uplink-wifi", "€" * 11, input="password1\n", msg="too long")      # 11 chars, 33 bytes
        self.fails("uplink-wifi", "Home", input="short\n", msg="8–63")
        self.fails("uplink-wifi", "Home", input="x" * 64 + "\n", msg="8–63")
        self.ok("uplink-wifi", "€" * 10, input="password1\n")                          # 30 bytes is fine
        self.ok("uplink-wifi", "Hex", input="ab" * 32 + "\n")                          # a raw 64-hex PSK

    def test_join_failure_reported(self):
        self.fails("uplink-wifi", "Home", input="password1\n", STUB_RC_nmcli="4", msg="could not join")

    def test_refused_when_wlan0_is_ap(self):
        self.write_conf(example_config(network={"ap_iface": "wlan0"}))
        self.ok("apply-config")
        self.stubs.clear()
        self.fails("uplink-wifi", "Home", input="password1\n", msg="hotspot")
        self.assertEqual(self.stubs.calls("nmcli"), [])

    def test_forget(self):
        r = self.ok("uplink-forget", STUB_OUT_nmcli="GENERAL.CONNECTION:Home Net")
        self.assertIn("forgot Home Net", r.stdout)
        self.assertIn(["con", "delete", "id", "Home Net"], self.stubs.argvs("nmcli"))


class Keys(Ctl):
    def signers(self):
        return (self.root / "etc/prepper/allowed_signers").read_text().splitlines()

    def test_trust(self):
        self.ok("trust", GOOD_KEY)
        self.ok("trust", GOOD_KEY)
        self.assertEqual(self.signers(), ['prepper-update namespaces="prepper-update" ' + " ".join(GOOD_KEY.split()[:2])])

    def test_trust_code_scope(self):
        k12 = " ".join(GOOD_KEY.split()[:2])
        self.ok("trust", GOOD_KEY)                      # default: content only
        self.ok("trust", "--code", GOOD_KEY)            # upgraded in place, listed once
        self.ok("trust", "--code", GOOD_KEY)
        self.assertEqual(self.signers(), ['prepper-code namespaces="prepper-update" ' + k12])
        self.fails("trust", "--code")                   # key required
        self.fails("trust", "--code", "ssh-rsa AAAA x")

    def test_trust_rejects(self):
        k = GOOD_KEY.split()[1]
        for bad in ("ssh-rsa " + k, f"ssh-ed25519 {k}\n* namespaces=\"git\" ssh-ed25519 {k}",
                    f"ssh-ed25519 {k}!!", "ssh-ed25519", f" ssh-ed25519 {k}"):
            with self.subTest(key=bad):
                self.fails("trust", bad)
                self.fails("trust-maintainer", bad)
        self.assertFalse((self.root / "etc/prepper/allowed_signers").exists())
        self.assertFalse((self.root / "etc/prepper/git_allowed_signers").exists())

    def test_trust_maintainer(self):
        self.ok("trust-maintainer", GOOD_KEY)
        self.ok("trust-maintainer", GOOD_KEY)
        lines = (self.root / "etc/prepper/git_allowed_signers").read_text().splitlines()
        self.assertEqual(lines, ['maintainer namespaces="git" ' + " ".join(GOOD_KEY.split()[:2])])

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_gen_key(self):
        self.ok("gen-key")
        self.ok("gen-key")
        key = self.root / "etc/prepper/keys/disk_ed25519"
        self.assertTrue(key.exists())
        self.assertEqual(len(self.signers()), 1)
        self.assertTrue(self.signers()[0].startswith('prepper-update namespaces="prepper-update" '))   # content scope
        self.assertTrue((self.root / "etc/prepper/git_allowed_signers").exists())
        # encrypted: the key file alone signs nothing (red-team finding 8); passphrase in a 0600 file
        self.assertNotEqual(subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)], capture_output=True,
                                           stdin=subprocess.DEVNULL).returncode, 0)
        self.assertEqual((key.parent / "disk_ed25519.pass").stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_gen_key_encrypts_an_older_plain_key(self):
        key = self.root / "etc/prepper/keys/disk_ed25519"
        key.parent.mkdir(parents=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "old", "-f", str(key)], check=True,
                       stdin=subprocess.DEVNULL)
        pub = (key.parent / "disk_ed25519.pub").read_text()
        self.ok("gen-key")
        self.assertEqual((key.parent / "disk_ed25519.pub").read_text(), pub)          # same identity
        self.assertNotEqual(subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)], capture_output=True,
                                           stdin=subprocess.DEVNULL).returncode, 0)

    def test_gen_tls(self):
        if not shutil.which("openssl"):
            self.skipTest("needs openssl")
        probe = subprocess.run(["openssl", "req", "-help"], capture_output=True, text=True)
        if "-addext" not in probe.stdout + probe.stderr:
            self.skipTest("this openssl has no -addext")
        self.ok("gen-tls")
        tls = self.root / "etc/prepper/tls"
        self.assertEqual((tls / "server.key").stat().st_mode & 0o777, 0o600)
        # red-team finding 9: the CA key is gone once the server certificate is signed …
        self.assertFalse((tls / "ca.key").exists())
        self.assertEqual(sorted(p.name for p in tls.iterdir()), ["ca.crt", "server.crt", "server.key"])
        text = subprocess.run(["openssl", "x509", "-in", str(tls / "server.crt"), "-noout", "-text"],
                              capture_output=True, text=True).stdout
        self.assertIn("DNS:badger.lan", text)
        self.assertIn("IP Address:10.10.10.10", text)
        verify = subprocess.run(["openssl", "verify", "-CAfile", str(tls / "ca.crt"), str(tls / "server.crt")],
                                capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
        # … and the CA can only vouch for .lan/.local names and the hotspot's subnet, for about two years
        ca = subprocess.run(["openssl", "x509", "-in", str(tls / "ca.crt"), "-noout", "-text"],
                            capture_output=True, text=True).stdout
        self.assertIn("Name Constraints: critical", ca)
        self.assertIn("DNS:lan", ca)
        self.assertIn("DNS:local", ca)
        self.assertRegex(ca, r"IP:10\.10\.10\.0/255\.255\.255\.0")
        end = subprocess.run(["openssl", "x509", "-in", str(tls / "ca.crt"), "-noout", "-checkend", str(900 * 86400)],
                             capture_output=True, text=True)
        self.assertNotEqual(end.returncode, 0, "CA valid for more than ~2.5 years")
        # running it again makes a NEW CA (there is no key to reuse)
        first = (tls / "ca.crt").read_bytes()
        self.ok("gen-tls")
        self.assertNotEqual((tls / "ca.crt").read_bytes(), first)
        self.assertFalse((tls / "ca.key").exists())


def git_ssh_signing() -> bool:
    if not (shutil.which("git") and shutil.which("ssh-keygen")):
        return False
    v = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.split()[2]
    major, minor = (int(x) for x in v.split(".")[:2])
    return (major, minor) >= (2, 34)


@unittest.skipUnless(git_ssh_signing(), "needs git >= 2.34 and ssh-keygen")
class SelfUpdate(Ctl):
    """self-update-hook with real git: signatures, downgrades, and a hostile .git/config."""

    def setUp(self):
        super().setUp()
        # real git and tar here; systemd-run, rsync etc. stay stubbed
        self.stubs = Stubs(self.tmp / "s2", names=[n for n in STUB_NAMES if n != "git"])
        self.key = self.tmp / "maint"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "maint", "-f", str(self.key)],
                       check=True, stdin=subprocess.DEVNULL)
        self.src = self.root / "srv/prepper/apps/prepper_software"
        self.src.mkdir(parents=True)
        self.git("init", "-q")
        (self.src / "os").mkdir()
        (self.src / "os/install.sh").write_text(
            '#!/bin/bash\nprintf "install %s %s\\n" "$*" "$(cd "$(dirname "$0")/.." && ls -a | tr "\\n" " ")" >> "$FAKE_LOG"\n')
        self.evil = self.tmp / "PWNED"
        self.commit("v1", sign=True, date="2026-01-01T00:00:00")

    def git(self, *args, date: str | None = None):
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", HOME=str(self.tmp))
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        return subprocess.run(["git", "-C", str(self.src), "-c", "user.name=m", "-c", "user.email=m@x",
                               "-c", "gpg.format=ssh", "-c", f"user.signingkey={self.key}", *args],
                              check=True, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)

    def commit(self, msg: str, sign: bool, date: str):
        (self.src / "VERSION").write_text(msg)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg, *(["-S"] if sign else ["--no-gpg-sign"]), date=date)

    def plant_hostile_config(self):
        hook = self.tmp / "evil.sh"
        hook.write_text(f"#!/bin/sh\ntouch {self.evil}\nexit 0\n")
        hook.chmod(0o755)
        with open(self.src / ".git/config", "a") as fh:
            fh.write(f"[gpg \"ssh\"]\n\tprogram = {hook}\n[core]\n\tfsmonitor = {hook}\n\thooksPath = {self.tmp}\n")
        (self.src / "UNTRACKED_EXTRA").write_text("not in the commit")

    def trust(self):
        self.ok("trust-maintainer", self.key.with_suffix(".pub").read_text().strip())
        self.assertTrue(any(a[-1] == "self-update-hook" for a in self.stubs.argvs("systemd-run")),
                        "trusting a maintainer key kicks off a self-update")

    def installs(self):
        return [x for x in self.fake_calls() if x.startswith("install")]

    def test_signed_update_installs_from_clean_tree(self):
        self.trust()
        self.plant_hostile_config()
        r = self.ok("self-update-hook")
        self.assertIn("installed", r.stdout)
        self.assertFalse(self.evil.exists(), "a repo-local git config ran a program")
        (line,) = self.installs()
        self.assertIn("--upgrade", line)
        self.assertIn("VERSION", line)
        self.assertNotIn("UNTRACKED_EXTRA", line, "only the signed tree is installed")
        self.assertNotIn(".git", line)
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertTrue((self.root / "var/lib/prepper/software-commit").read_text().startswith(head))
        self.assertEqual([p.name for p in (self.root / "var/lib/prepper").iterdir() if p.name.startswith(".self")], [])
        r = self.ok("self-update-hook")                              # same commit again: nothing to do
        self.assertIn("already installed", r.stdout)
        self.assertEqual(len(self.installs()), 1)

    def test_unsigned_refused(self):
        self.trust()
        self.commit("v2-unsigned", sign=False, date="2026-02-01T00:00:00")
        self.fails("self-update-hook", msg="not signed by a trusted key")
        self.assertEqual(self.installs(), [])

    def test_untrusted_key_refused(self):
        other = self.tmp / "other"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True,
                       stdin=subprocess.DEVNULL)
        self.ok("trust-maintainer", other.with_suffix(".pub").read_text().strip())
        self.fails("self-update-hook", msg="not signed by a trusted key")

    def test_no_maintainer_key(self):
        self.fails("self-update-hook", msg="no maintainer key")

    def test_downgrade_refused(self):
        self.trust()
        self.commit("v2", sign=True, date="2026-03-01T00:00:00")
        self.ok("self-update-hook")
        # an older (but genuinely signed) release replayed by a peer or USB bundle
        self.git("reset", "-q", "--hard", "HEAD~1")
        self.fails("self-update-hook", msg="downgrade")
        self.assertEqual(len(self.installs()), 1)


class Power(Ctl):
    def setUp(self):
        super().setUp()
        for i in (0, 4):
            d = self.root / f"sys/devices/system/cpu/cpufreq/policy{i}"
            d.mkdir(parents=True)
            (d / "scaling_governor").write_text("ondemand\n")

    def test_modes(self):
        self.ok("power-mode", "endurance")
        self.assertEqual((self.root / "sys/devices/system/cpu/cpufreq/policy4/scaling_governor").read_text(),
                         "powersave\n")
        self.assertEqual((self.root / "run/prepper/llama-variant").read_text(), "endurance\n")
        self.assertEqual((self.root / "var/lib/prepper/power-mode").read_text(), "endurance\n")
        self.assertIn(["try-restart", "--no-block", "llama-server"], self.stubs.argvs("systemctl"))
        self.ok("power-mode", "full")
        self.assertEqual((self.root / "run/prepper/llama-variant").read_text(), "default\n")
        self.assertIn(["start", "--no-block", "kolibri"], self.stubs.argvs("systemctl"))   # features.kolibri = true
        self.assertNotIn(["start", "--no-block", "kavita"], self.stubs.argvs("systemctl"))

    def test_bad_mode(self):
        for m in ("turbo", "--full", "../x"):
            self.fails("power-mode", m)
        self.assertFalse((self.root / "var/lib/prepper/power-mode").exists())

    def test_no_cpufreq_is_not_fatal(self):
        shutil.rmtree(self.root / "sys")
        self.ok("power-mode", "balanced")

    def test_restore(self):
        self.ok("power-restore")
        self.assertEqual((self.root / "var/lib/prepper/power-mode").read_text(), "balanced\n")
        (self.root / "var/lib/prepper/power-mode-user").write_text("bogus\n")
        self.write_conf(example_config(power={"default_mode": "endurance"}))
        self.ok("power-restore")
        self.assertEqual((self.root / "var/lib/prepper/power-mode").read_text(), "endurance\n")

    def test_auto_switch_is_not_the_users_choice(self):
        st = self.root / "var/lib/prepper"
        self.ok("power-mode", "full")
        self.ok("power-mode", "endurance", "--auto")                 # powerd, low battery
        self.assertEqual((st / "power-mode").read_text(), "endurance\n")
        self.assertEqual((st / "power-mode-user").read_text(), "full\n")
        self.assertTrue((st / "power-mode-auto").exists())
        self.ok("power-restore")                                      # boot, or power is back
        self.assertEqual((st / "power-mode").read_text(), "full\n")
        self.assertFalse((st / "power-mode-auto").exists())

    def test_auto_flag_refused_from_portal(self):
        self.fails("power-mode", "endurance", "--auto", sudo_user="prepper", msg="powerd only")
        self.fails("power-mode", "endurance", "--force", msg="usage")

    def test_llama_variant_symlink_not_followed(self):
        run_dir = self.root / "run/prepper"
        run_dir.mkdir(parents=True, exist_ok=True)
        victim = self.tmp / "sudoers"
        victim.write_text("keep\n")
        (run_dir / "llama-variant").symlink_to(victim)
        self.ok("power-mode", "endurance")
        self.assertEqual(victim.read_text(), "keep\n")
        self.assertFalse((run_dir / "llama-variant").is_symlink())
        self.assertEqual((run_dir / "llama-variant").read_text(), "endurance\n")

    def test_auto_apply_enabled(self):
        self.ok("auto-apply-enabled")
        self.write_conf(example_config(upd={"auto_apply": False}))
        r = self.ctl("auto-apply-enabled")
        self.assertEqual(r.returncode, 1)                 # 1 = ExecCondition "skip", not failure


class NetUp(Ctl):
    def assert_hotspot_allow_list(self, rules: str, ap_if: str):
        """Only DNS, DHCP, NTP, ICMP and nginx's ports from the hotspot; everything else dropped."""
        lines = [x.strip() for x in rules.splitlines() if x.strip().startswith("iifname")]
        self.assertEqual(lines, [
            f"iifname {ap_if} ct state established,related accept",
            f"iifname {ap_if} tcp dport 22 drop",
            f"iifname {ap_if} meta l4proto {{ icmp, ipv6-icmp }} accept",
            f"iifname {ap_if} udp dport {{ 53, 67, 123 }} accept",
            f"iifname {ap_if} tcp dport {{ 53, 80, 443, 8088, 8443, 8089 }} accept",
            f"iifname {ap_if} drop",
        ])
        for port in ("22", "8080", "8000", "8081", "8090", "8091", "60631", "5000", "8096"):
            self.assertNotRegex(rules, rf"dport \{{[^}}]*\b{port}\b", f"port {port} open to the hotspot")

    def test_usb_adapter_and_shared_uplink(self):
        self.write_conf(example_config(network={
            "ap_iface": "wlan1", "share_uplink": True,
            "uplink_wifi": [{"ssid": "Bob's $(reboot) net", "psk": "p@ss word;"}, {"ssid": "Open"}]}))
        self.ok("net-up", STUB_STDIN="nft", STUB_RC_nmcli="1")      # neither uplink joins: still fine
        self.assertIn(["addr", "add", "10.10.10.10/24", "dev", "wlan1"], self.stubs.argvs("ip"))
        self.assertIn(["restart", "prepper-hostapd"], self.stubs.argvs("systemctl"))
        self.assertIn(["restart", "dnsmasq"], self.stubs.argvs("systemctl"))
        self.assertIn(["dev", "wlan1", "info"], self.stubs.argvs("iw"))
        ups = [a for a in self.stubs.argvs("nmcli") if "up" in a]
        self.assertEqual(len(ups), 2, "each uplink is tried in turn")
        profiles = sorted((self.root / "etc/NetworkManager/system-connections").glob("*.nmconnection"))
        self.assertEqual(len(profiles), 2)
        self.assertTrue(any("psk=p@ss word;" in p.read_text() for p in profiles))
        for _, args, _ in self.stubs.calls():
            self.assertFalse(any("p@ss" in a for a in args), "Wi-Fi password in a command line")
        (_, args, rules), = [c for c in self.stubs.calls("nft") if c[1] == ["-f", "-"]]
        self.assertIn("ip saddr 10.10.10.0/24 oifname != \"wlan1\" masquerade", rules)
        self.assertIn("delete table inet prepper", rules)
        self.assertIn('iifname "wlan1" tcp dport 22 drop', rules)
        self.assert_hotspot_allow_list(rules, '"wlan1"')
        self.assertIn(["-qw", "net.ipv4.ip_forward=1"], self.stubs.argvs("sysctl"))
        nm = (self.root / "etc/NetworkManager/conf.d/99-prepper.conf").read_text()
        self.assertIn("interface-name:wlan1", nm)

    def test_bridged_hotspot_firewall_covers_both_interfaces(self):
        self.write_conf(example_config(network={"ap_iface": "wlan1", "security": "owe-transition"}))
        self.ok("net-up", STUB_STDIN="nft")
        (_, _, rules), = [c for c in self.stubs.calls("nft") if c[1] == ["-f", "-"]]
        env = (self.root / "etc/default/prepper").read_text()
        bridge = env.split("AP_LINK_IFACE=", 1)[1].split()[0]
        self.assertNotEqual(bridge, "wlan1")
        self.assert_hotspot_allow_list(rules, f'{{ "wlan1", "{bridge}" }}')

    def test_wlan0_deferred_while_provisioning(self):
        (self.root / "var/lib/prepper/first-pull-pending").touch()
        r = self.ok("net-up", STUB_OUT_ip="default via 192.168.1.1 dev wlan0 proto dhcp")
        self.assertIn("AP deferred", r.stdout)
        self.assertFalse((self.root / "var/lib/prepper/ap-wlan0").exists())
        self.assertNotIn(["restart", "prepper-hostapd"], self.stubs.argvs("systemctl"))
        (run_args,) = self.stubs.argvs("systemd-run")
        self.assertTrue(any(a.startswith("--on-active=") for a in run_args))
        nm = (self.root / "etc/NetworkManager/conf.d/99-prepper.conf").read_text()
        self.assertNotIn("unmanaged-devices", nm)

    def test_wlan0_deferral_expires(self):
        (self.root / "var/lib/prepper/first-pull-pending").touch()
        since = self.root / "var/lib/prepper/ap-deferred-since"
        since.touch()
        old = time.time() - 13 * 3600
        os.utime(since, (old, old))
        r = self.ok("net-up", STUB_OUT_ip="default via 192.168.1.1 dev wlan0")
        self.assertIn("bringing the hotspot up anyway", r.stdout)
        self.assertTrue((self.root / "var/lib/prepper/ap-wlan0").exists())
        nm = (self.root / "etc/NetworkManager/conf.d/99-prepper.conf").read_text()
        self.assertIn("unmanaged-devices=interface-name:wlan0", nm)

    def test_wlan0_after_provisioning(self):
        self.ok("build-status", "done", "100", "Ready")
        self.stubs.clear()
        self.ok("net-up", STUB_OUT_ip="default via 192.168.1.1 dev wlan0", STUB_STDIN="nft")
        self.assertTrue((self.root / "var/lib/prepper/ap-wlan0").exists())
        self.assertIn(["restart", "prepper-hostapd"], self.stubs.argvs("systemctl"))
        rules = [d for _, a, d in self.stubs.calls("nft") if a == ["-f", "-"]]
        self.assertEqual(len(rules), 1)
        self.assertNotIn("masquerade", rules[0])                  # share_uplink = false
        self.assertIn('iifname "wlan0" tcp dport 22 drop', rules[0])
        self.assert_hotspot_allow_list(rules[0], '"wlan0"')

    def test_bad_config_without_previous_fails_before_touching_network(self):
        self.write_conf(example_config(network={"ssid": "a\nb"}))
        self.fails("net-up", msg="no earlier configuration")
        self.assertEqual(self.stubs.calls("ip"), [])

    def test_bad_config_keeps_last_good(self):
        self.write_conf(example_config(network={"ap_iface": "wlan1", "ssid": "Good"}))
        self.ok("net-up")
        self.write_conf(example_config(network={"ap_iface": "wlan1", "ssid": "a\nb"}))
        self.stubs.clear()
        r = self.ok("net-up")
        self.assertIn("last good configuration", r.stderr)
        self.assertIn("ssid=Good", (self.root / "etc/hostapd/prepper.conf").read_text())
        self.assertIn(["restart", "prepper-hostapd"], self.stubs.argvs("systemctl"))

    def test_defers_for_configured_uplink_without_route(self):
        # At boot NetworkManager may not have joined yet: no default route, but an uplink is configured.
        (self.root / "var/lib/prepper/first-pull-pending").touch()
        self.write_conf(example_config(network={"uplink_wifi": [{"ssid": "Home", "psk": "password1"}]}))
        r = self.ok("net-up")
        self.assertIn("AP deferred", r.stdout)
        self.assertFalse((self.root / "var/lib/prepper/ap-wlan0").exists())

    def test_defers_for_nm_autoconnect_profile(self):
        (self.root / "var/lib/prepper/first-pull-pending").touch()
        r = self.ok("net-up", STUB_OUT_nmcli="802-3-ethernet:yes\n802-11-wireless:yes")
        self.assertIn("AP deferred", r.stdout)

    def test_defers_during_firstboot_install(self):
        r = self.ok("net-up", PREPPER_FIRSTBOOT="1", STUB_OUT_ip="default via 1.2.3.4 dev wlan0")
        self.assertIn("AP deferred", r.stdout)

    def test_no_wifi_uplink_ap_comes_up(self):
        (self.root / "var/lib/prepper/first-pull-pending").touch()
        self.ok("net-up", STUB_OUT_ip="default via 192.168.1.1 dev eth0")
        self.assertTrue((self.root / "var/lib/prepper/ap-wlan0").exists())

    def test_ap_never_serves_fails_after_fallbacks(self):
        self.write_conf(example_config(network={"ap_iface": "wlan1"}))
        r = self.fails("net-up", STUB_OUT_iw="Interface x\n\ttype managed", msg="could not be started")
        self.assertIn("trying fallbacks", r.stderr)
        # it tried the adapter, the adapter at 2.4 GHz, then the onboard radio
        addrs = [a[-1] for a in self.stubs.argvs("ip") if a[:2] == ["addr", "add"]]
        self.assertEqual(addrs, ["wlan1", "wlan1", "wlan0"])
        self.assertIn("interface=wlan0", (self.root / "etc/hostapd/prepper.conf").read_text())
        self.assertNotIn(["restart", "dnsmasq"], self.stubs.argvs("systemctl"))


class Provisioning(Ctl):
    def test_finish(self):
        st = self.root / "var/lib/prepper"
        for f in ("first-pull-pending", "ap-deferred-since", "firstboot.done"):
            (st / f).touch()
        (self.root / "etc/prepper/proxy.env").write_text("http_proxy=http://x:1\n")
        (self.root / "boot/firmware/prepper/secret").write_text("x")
        self.ok("finish-provisioning")
        self.assertFalse((st / "first-pull-pending").exists())
        self.assertFalse((st / "ap-deferred-since").exists())
        self.assertFalse((self.root / "etc/prepper/proxy.env").exists())
        self.assertFalse((self.root / "boot/firmware/prepper").exists())
        self.assertEqual(json.loads((st / "build.json").read_text())["phase"], "done")

    def first_pull(self, rc: int):
        st = self.root / "var/lib/prepper"
        (st / "first-pull-pending").touch()
        return self.ctl("first-pull", FAKE_UPD_RC=str(rc))

    def test_first_pull_offline_retries(self):
        r = self.first_pull(75)
        self.assertEqual(r.returncode, 75, r.stderr)                 # Restart=on-failure tries again
        self.assertTrue((self.root / "var/lib/prepper/first-pull-pending").exists())
        b = json.loads((self.root / "var/lib/prepper/build.json").read_text())
        self.assertEqual(b["phase"], "content")
        self.assertIn("apply --now", self.fake_calls()[0])

    def test_first_pull_done(self):
        for rc in (0, 1):                                            # a few failed items still finish
            with self.subTest(rc=rc):
                r = self.first_pull(rc)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse((self.root / "var/lib/prepper/first-pull-pending").exists())
                b = json.loads((self.root / "var/lib/prepper/build.json").read_text())
                self.assertEqual(b["phase"], "done")

    def test_apply_config(self):
        self.write_conf(example_config(network={"hostname": "den"}, device={"timezone": "Europe/Oslo"}))
        self.ok("apply-config")
        self.assertIn(["set-hostname", "den"], self.stubs.argvs("hostnamectl"))
        self.assertIn(["set-timezone", "Europe/Oslo"], self.stubs.argvs("timedatectl"))
        self.assertIn(["daemon-reload"], self.stubs.argvs("systemctl"))
        self.assertEqual((self.root / "etc/hostname").read_text(), "den\n")

    def test_rebuild_library_counts(self):
        for n in ("a.zim", "b c.zim"):
            (self.root / "srv/prepper/zim" / n).write_text("x")
        self.ok("apply-config")                       # writes etc/default/prepper (DATA_ROOT)
        self.stubs.clear()
        r = self.ok("rebuild-library", STUB_RC_kiwix_manage="1")   # a damaged ZIM is skipped, not fatal
        self.assertIn("library: 2 ZIMs", r.stdout)
        self.assertEqual(len(self.stubs.argvs("kiwix-manage")), 2)


class WifiRotate(Ctl):
    def test_new_password_saved_and_switch_scheduled(self):
        before = tomllib.loads(self.conf.read_text())
        r = self.ok("wifi-rotate", sudo_user="prepper")              # the admin page may do this
        pw = [ln.split(": ", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("passphrase: ")][0]
        self.assertRegex(pw, r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        after = tomllib.loads(self.conf.read_text())
        self.assertEqual(after["network"]["passphrase"], pw)
        for section in before:                                       # nothing else changed
            if section != "network":
                self.assertEqual(before[section], after[section], section)
        self.assertEqual({k: v for k, v in before["network"].items() if k != "passphrase"},
                         {k: v for k, v in after["network"].items() if k != "passphrase"})
        self.assertEqual(self.conf.stat().st_mode & 0o777, 0o640)
        apply_args, revert_args = self.stubs.argvs("systemd-run")
        self.assertIn("--on-active=10s", apply_args)
        self.assertEqual(apply_args[-1], "wifi-apply")
        self.assertIn("--unit=prepper-wifi-revert", revert_args)     # the automatic undo
        self.assertIn("--on-active=610s", revert_args)
        self.assertEqual(revert_args[-1], "wifi-revert")
        pend = self.root / "var/lib/prepper/wifi-rotate-pending.json"
        self.assertEqual(json.loads(pend.read_text())["old"], before["network"]["passphrase"])
        self.assertEqual(pend.stat().st_mode & 0o777, 0o600)          # the old password: root only

    def pw(self):
        return tomllib.loads(self.conf.read_text())["network"]["passphrase"]

    def test_no_one_confirms_so_it_switches_back(self):
        self.write_conf(self.conf.read_text().replace('passphrase = ""', 'passphrase = "oldfamilypass"'))
        self.ok("wifi-rotate")
        self.assertNotEqual(self.pw(), "oldfamilypass")
        status = json.loads(self.ok("wifi-status").stdout)
        self.assertTrue(status["pending"]); self.assertGreater(status["seconds_left"], 500)
        self.fails("wifi-rotate", msg="waiting to be confirmed")     # a second change can't lose the original
        r = self.ok("wifi-revert")                                     # what the timer runs after 10 minutes
        self.assertIn("switched back", r.stdout)
        self.assertEqual(self.pw(), "oldfamilypass")
        self.assertFalse(json.loads(self.ok("wifi-status").stdout)["pending"])
        self.assertIn("nothing", self.ok("wifi-revert").stdout.replace("no Wi-Fi password change to undo", "nothing"))

    def test_confirm_keeps_the_new_password(self):
        r = self.ok("wifi-rotate")
        new = [ln.split(": ", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("passphrase: ")][0]
        self.ok("wifi-confirm", sudo_user="prepper")
        self.assertIn(["stop", "prepper-wifi-revert.timer"], self.stubs.argvs("systemctl"))
        self.ok("wifi-revert")                                         # a late timer firing does nothing
        self.assertEqual(self.pw(), new)

    def test_each_rotation_is_different(self):
        a = self.ok("wifi-rotate").stdout
        self.ok("wifi-confirm")
        b = self.ok("wifi-rotate").stdout
        self.assertNotEqual(a, b)

    # — failsafe fixes (review finding 10) —
    def status(self):
        return json.loads(self.ok("wifi-status").stdout.strip().splitlines()[-1])

    def test_status_says_when_the_new_password_is_switched_on(self):
        self.ok("wifi-rotate")
        self.assertEqual((self.status()["pending"], self.status()["applied"]), (True, False))
        self.ok("wifi-apply")                                          # what the 10-second timer runs
        self.assertTrue(self.status()["applied"])

    def test_rearm_at_boot(self):
        self.assertIn("no Wi-Fi password change", self.ok("wifi-rearm").stdout)
        self.ok("wifi-rotate")
        self.stubs.clear()
        r = self.ok("wifi-rearm")                                       # prepper-net.service, after a reboot
        self.assertIn("armed again", r.stdout)
        args = self.stubs.argvs("systemd-run")[-1]
        self.assertIn("--unit=prepper-wifi-revert", args)
        self.assertEqual(args[-1], "wifi-revert")
        secs = int([a for a in args if a.startswith("--on-active=")][0].split("=")[1].rstrip("s"))
        self.assertTrue(500 <= secs <= 610, secs)
        pend = self.root / "var/lib/prepper/wifi-rotate-pending.json"
        d = json.loads(pend.read_text())
        d["deadline"] = time.time() - 3600                              # the Badger was off past the deadline
        pend.write_text(json.dumps(d))
        self.ok("wifi-rearm")
        self.assertIn("--on-active=60s", self.stubs.argvs("systemd-run")[-1])
        self.assertIn("wifi-rearm", (REPO / "os/systemd/prepper-net.service").read_text())

    def test_rearm_failure_is_reported(self):
        self.ok("wifi-rotate")
        self.fails("wifi-rearm", STUB_RC_systemd_run="1", msg="could not re-arm")

    def test_revert_from_the_portal_runs_the_switch_on_its_own(self):
        self.write_conf(self.conf.read_text().replace('passphrase = ""', 'passphrase = "oldfamilypass"'))
        self.ok("wifi-rotate", sudo_user="prepper")
        self.stubs.clear()
        self.ok("wifi-revert", sudo_user="prepper")
        self.assertEqual(self.pw(), "oldfamilypass")                    # the config is back at once
        runs = self.stubs.argvs("systemd-run")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][-1], "wifi-apply")                    # the restart runs outside the portal
        self.assertNotIn(["restart", "prepper-portal", "kiwix-serve"], self.stubs.argvs("systemctl"))

    def test_lock_serialises_wifi_verbs(self):
        import fcntl
        lock = open(self.root / "var/lib/prepper/.wifi.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            import subprocess as sp
            from os_helpers import BASH as B
            e = self.stubs.env(PREPPER_TEST_ROOT=self.root, PREPPER_TEST_PY=sys.executable, FAKE_LOG=self.fake_log)
            proc = sp.Popen([B, str(CTL), "wifi-rotate"], env=e, stdout=sp.PIPE, stderr=sp.PIPE, stdin=sp.DEVNULL, text=True)
            time.sleep(1.0)
            self.assertIsNone(proc.poll(), "wifi-rotate ran while another Wi-Fi change held the lock")
            self.assertFalse((self.root / "var/lib/prepper/wifi-rotate-pending.json").exists())
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN); lock.close()
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue((self.root / "var/lib/prepper/wifi-rotate-pending.json").exists())

    def test_section_headers_with_comments(self):
        t = self.conf.read_text().replace("[network]\n", "[network]   # the hotspot\n", 1).replace("[portal]\n", "[portal]  # launcher\n", 1)
        self.write_conf(t)
        r = self.ok("wifi-rotate")
        conf = tomllib.loads(self.conf.read_text())
        self.assertEqual(conf["network"]["passphrase"], [ln.split(": ", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("passphrase: ")][0])
        self.assertEqual(self.conf.read_text().count("[network]"), 1)
        self.ok("set-pin", input="246813\n")
        self.assertEqual(self.conf.read_text().count("[portal]"), 1)
        self.assertTrue(tomllib.loads(self.conf.read_text())["portal"]["admin_pin"].startswith("scrypt$"))


class AiModel(Ctl):
    def setUp(self):
        super().setUp()
        (self.root / "srv/prepper/models").mkdir(parents=True)
        (self.root / "srv/prepper/models/m1.current").write_text("gguf")
        cat = self.root / "opt/prepper/content"
        cat.mkdir(parents=True)
        (cat / "catalog.toml").write_text('[[item]]\nid = "m1"\nkind = "model"\nctx = 6144\nflash_attn = true\n')

    def test_selects_and_restarts(self):
        r = self.ok("ai-model", "m1", sudo_user="prepper")
        self.assertIn("AI model: m1", r.stdout)
        self.assertEqual((self.root / "run/prepper/llama-model").read_text(), "m1\nctx=6144\nflash=1\n")
        self.assertIn(["try-restart", "--no-block", "llama-server"], self.stubs.argvs("systemctl"))

    def test_without_catalog_entry_just_the_id(self):
        (self.root / "srv/prepper/models/m2.current").write_text("gguf")
        self.ok("ai-model", "m2")
        self.assertEqual((self.root / "run/prepper/llama-model").read_text(), "m2\n")

    def test_refusals(self):
        for bad in ("../m1", ".hidden", "m1/x", "a b", "-x", "nope"):
            with self.subTest(id=bad):
                self.fails("ai-model", bad)
        self.fails("ai-model")
        self.assertFalse((self.root / "run/prepper/llama-model").exists())
        self.assertNotIn("try-restart", [a[0] for a in self.stubs.argvs("systemctl")])

    def test_refused_in_endurance(self):
        (self.root / "run/prepper").mkdir(parents=True, exist_ok=True)
        (self.root / "run/prepper/llama-variant").write_text("endurance\n")
        self.fails("ai-model", "m1", msg="battery-saving")
        self.assertFalse((self.root / "run/prepper/llama-model").exists())


if __name__ == "__main__":
    unittest.main()
