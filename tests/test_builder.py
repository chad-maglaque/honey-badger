#!/usr/bin/env python3
"""
Tests for builder/prepper-build: plan, inject (cloud-init and firstrun),
password hashing, SSH-key checks, and hostile option values. Never flashes.
   python3.11 tests/test_builder.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import subprocess
import sys
import tarfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from os_helpers import BASH, PY, REPO, TempDirCase, run  # noqa: E402

BUILD = REPO / "builder/prepper-build"
loader = importlib.machinery.SourceFileLoader("prepper_build", str(BUILD))
spec = importlib.util.spec_from_loader("prepper_build", loader)
pb = importlib.util.module_from_spec(spec)
loader.exec_module(pb)

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq2nB3mT3Jv8Z0f0w6m9dYl0C5yqk1wqf2y3x9b8r6Z me@laptop"
PRIVATE = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\n-----END OPENSSH PRIVATE KEY-----\n"
HOSTILE = ["a b", "it's", 'say "hi"', "$(touch /tmp/pwned)", "`id`", "x;reboot", "back\\slash"]


def yaml_load(text: str):
    """PyYAML if this machine has it (not required: the project is stdlib-only)."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    return yaml.safe_load(text)


class Inject(TempDirCase):
    def setUp(self):
        super().setUp()
        self.boot = self.tmp / "bootfs"
        self.boot.mkdir()
        (self.boot / "config.txt").write_text("arm_64bit=1\n")
        (self.boot / "cmdline.txt").write_text("console=serial0,115200 root=PARTUUID=1234-02 rootwait quiet\n")
        self.home = self.tmp / "home"                      # never pick up the real ~/.ssh/id_ed25519.pub
        self.home.mkdir()

    def inject(self, *args, mode="cloud-init", expect_rc=0):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
        r = run([PY, str(BUILD), "inject", str(self.boot), "--mode", mode, *args], env=env)
        self.assertEqual(r.returncode, expect_rc, r.stdout + r.stderr)
        self.r = r
        return r

    def toml(self):
        return tomllib.loads((self.boot / "prepper/prepper.toml").read_text())

    # — cloud-init —
    def test_cloud_init_with_key(self):
        self.inject("--ssh-key", ED, "--hostname", "den", "--ssid", "Den", "--pin", "246813",
                    "--passphrase", "wifi-secret")
        ud = (self.boot / "user-data").read_text()
        self.assertTrue(ud.startswith("#cloud-config\n"))
        self.assertIn('hostname: "den"', ud)
        self.assertIn("lock_passwd: true", ud)
        self.assertIn(f'      - "{ED}"', ud)
        self.assertIn("ssh_pwauth: false", ud)
        self.assertFalse(any(x.strip().startswith("passwd:") for x in ud.splitlines()))
        self.assertTrue((self.boot / "meta-data").read_text().startswith("instance-id: den-"))
        self.assertIn("addresses: [10.10.11.2/24]", (self.boot / "network-config").read_text())
        c = self.toml()
        self.assertEqual((c["network"]["ssid"], c["network"]["passphrase"]), ("Den", "wifi-secret"))
        self.assertEqual(c["network"]["hostname"], "den")
        self.assertEqual(c["portal"]["admin_pin"], "", "the PIN travels on its own, hashed on the Badger")
        self.assertEqual((self.boot / "prepper/admin_pin").read_text(), "246813")
        for f in ("firstboot.sh", "prepper-firstboot.service", "software.tar.gz"):
            self.assertTrue((self.boot / "prepper" / f).exists(), f)
        self.assertFalse((self.boot / "firstrun.sh").exists())
        if (y := yaml_load(ud)) is not None:
            self.assertEqual(y["users"][0]["ssh_authorized_keys"], [ED])

    def test_cloud_init_password_hashed(self):
        self.inject("--ssh-password", "correct horse battery")
        ud = (self.boot / "user-data").read_text()
        self.assertIn("lock_passwd: false", ud)
        self.assertIn("ssh_pwauth: true", ud)
        self.assertNotIn("correct horse", ud)
        line = next(x for x in ud.splitlines() if x.strip().startswith("passwd:"))
        self.assertRegex(line, r'passwd: "\$6\$[./0-9A-Za-z]{16}\$[./0-9A-Za-z]{86}"$')

    def test_no_key_no_password_gets_random_password(self):
        self.inject()
        self.assertIn("gets the password", self.r.stdout)
        shown = self.r.stdout.split("password", 1)[1].split()[0]
        ud = (self.boot / "user-data").read_text()
        self.assertNotIn("change-me-now", ud)
        line = next(x for x in ud.splitlines() if x.strip().startswith("passwd:"))
        salt = line.split("$")[2]
        self.assertIn(pb.sha512_crypt(shown, salt), line, "the printed password is the one that was hashed")

    def test_uplink_and_hostile_values_cloud_init(self):
        for v in HOSTILE:
            with self.subTest(v=v):
                for p in self.boot.iterdir():
                    if p.name not in ("config.txt", "cmdline.txt"):
                        shutil.rmtree(p) if p.is_dir() else p.unlink()
                self.inject("--ssid", v, "--passphrase", f"pw {v} 1234", "--uplink", f"Up {v}:psk {v} 99",
                            "--ssh-key", ED)
                c = self.toml()
                self.assertEqual(c["network"]["ssid"], v)
                self.assertEqual(c["network"]["passphrase"], f"pw {v} 1234")
                self.assertEqual(c["network"]["uplink_wifi"], [{"ssid": f"Up {v}", "psk": f"psk {v} 99"}])
                nc = (self.boot / "network-config").read_text()
                if (y := yaml_load(nc)) is not None:
                    aps = y["network"]["wifis"]["wlan0"]["access-points"]
                    self.assertEqual(aps, {f"Up {v}": {"password": f"psk {v} 99"}})

    def test_unicode_ssid(self):
        self.inject("--ssid", "Café 🦡", "--uplink", "Maison 🏠:motdepasse", "--ssh-key", ED)
        self.assertEqual(self.toml()["network"]["ssid"], "Café 🦡")
        nc = (self.boot / "network-config").read_text()
        self.assertIn('"Maison 🏠":', nc)                       # raw UTF-8, not \ud83c surrogate escapes
        self.assertNotIn("\\ud83c", nc)

    def test_open_uplink(self):
        self.inject("--uplink", "Library", "--ssh-key", ED)
        nc = (self.boot / "network-config").read_text()
        self.assertIn('        "Library":\n          {}\n', nc)
        self.assertEqual(self.toml()["network"]["uplink_wifi"], [{"ssid": "Library", "psk": ""}])

    # — firstrun.sh (Bookworm without cloud-init) —
    def test_firstrun_mode(self):
        self.inject("--mode", "firstrun", "--ssh-key", ED, mode="firstrun")
        fr = (self.boot / "firstrun.sh").read_text()
        cmd = (self.boot / "cmdline.txt").read_text()
        self.assertIn("systemd.run=/boot/firmware/firstrun.sh", cmd)
        self.assertEqual(cmd.count("systemd.run="), 1)
        self.assertEqual(cmd.count("\n"), 1)
        self.assertIn("userconf prepper-admin '!'", fr)                        # key only: locked password
        self.assertIn("PasswordAuthentication no", fr)
        self.assertFalse((self.boot / "user-data").exists())
        # a second inject must not append systemd.run twice
        self.inject("--ssh-key", ED, mode="firstrun")
        self.assertEqual((self.boot / "cmdline.txt").read_text().count("systemd.run="), 1)

    def test_firstrun_hostile_key_comment_is_inert(self):
        for comment in HOSTILE:
            key = ED.rsplit(" ", 1)[0] + " " + comment
            with self.subTest(comment=comment):
                self.inject("--ssh-key", key, mode="firstrun")
                fr = self.boot / "firstrun.sh"
                if shutil.which("bash"):
                    # bash -n parses it; then run only the echo lines into a scratch home
                    self.assertEqual(run([BASH, "-n", str(fr)]).returncode, 0)
                    lines = [x for x in fr.read_text().splitlines()
                             if x.startswith("echo ") and x.endswith("authorized_keys")]
                    target = self.tmp / "auth"
                    script = "\n".join(x.replace("/home/prepper-admin/.ssh/authorized_keys", str(target))
                                       for x in lines)
                    r = run([BASH, "-c", script], cwd=self.tmp)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(target.read_text().splitlines()[-1], key)
                    target.unlink()
                self.assertFalse(Path("/tmp/pwned").exists())

    def test_firstrun_password_quoted(self):
        self.inject("--ssh-password", "p'w $(id) `x`", mode="firstrun")
        fr = (self.boot / "firstrun.sh").read_text()
        line = next(x for x in fr.splitlines() if "userconf" in x)
        self.assertRegex(line, r"userconf prepper-admin '\$6\$[^']+'$")
        self.assertNotIn("$(id)", fr)

    # — validation before anything is written —
    def assert_rejected(self, *args, msg=None):
        self.inject(*args, "--ssh-key", ED, expect_rc=1)
        if msg:
            self.assertIn(msg, self.r.stderr)
        self.assertFalse((self.boot / "prepper").exists(), "nothing written for a rejected option")
        self.assertFalse((self.boot / "user-data").exists())

    def test_passphrase_limits(self):
        self.assert_rejected("--passphrase", "1234567", msg="8–63")
        self.assert_rejected("--passphrase", "x" * 64, msg="8–63")
        self.assert_rejected("--passphrase", "pass\nword1", msg="8–63")
        self.inject("--passphrase", "x" * 8, "--ssh-key", ED)
        self.inject("--passphrase", "x" * 63, "--ssh-key", ED)

    def test_rejects_hostile_structural_values(self):
        cases = [
            ("--hostname", "den\nruncmd: [reboot]"), ("--hostname", "Den Box"), ("--hostname", "-x"),
            ("--timezone", "UTC\nruncmd: [reboot]"), ("--timezone", "../../etc/passwd"),
            ("--country", "us"), ("--country", "US\n"), ("--langs", "eng,../x"),
            ("--ssid", ""), ("--ssid", "x" * 33), ("--ssid", "a\nb"), ("--ssid", "a b"),
            ("--pin", "2468"), ("--pin", "24681357924681357924681357924681x"), ("--pin", "2468 13"),
            ("--uplink", "Home:short"), ("--uplink", ":password1"),
            ("--relay", "10.10.11.1:3128;reboot"), ("--relay", "$(id):1"), ("--relay", "host"),
            ("--relay", "host:99999"),
            ("--peer", "http://x/$(id)"), ("--peer", "file:///etc"),
            ("--trust-key", "ssh-rsa AAAA x"), ("--trust-key", ED + "\nssh-ed25519 AAAA evil"),
            ("--ssh-password", "short"),
        ]
        for opt, val in cases:
            with self.subTest(opt=opt, val=val):
                self.assert_rejected(f"{opt}={val}")

    def test_relay_proxy_env_is_plain(self):
        self.inject("--relay", "10.10.11.1:3128", "--ssh-key", ED)
        env = (self.boot / "prepper/proxy.env").read_text()
        self.assertIn("http_proxy=http://10.10.11.1:3128\n", env)
        self.assertIn("PREPPER_RELAY_ADDR=10.10.11.2/24\n", env)
        for line in env.splitlines():
            self.assertRegex(line, r"^[A-Za-z_]+=[A-Za-z0-9.:/,_-]+$")

    def test_not_a_boot_partition(self):
        (self.boot / "config.txt").unlink()
        self.inject("--ssh-key", ED, expect_rc=1)
        self.assertIn("not a Raspberry Pi boot partition", self.r.stderr)

    def test_software_tarball_is_root_owned(self):
        self.inject("--ssh-key", ED)
        with tarfile.open(self.boot / "prepper/software.tar.gz") as tar:
            members = tar.getmembers()
        names = {m.name for m in members}
        self.assertIn("os/bin/prepperctl", names)
        self.assertIn("os/systemd/prepper-hostapd.service", names)
        self.assertFalse(any(n.startswith("tests/") or "__pycache__" in n for n in names))
        for m in members:
            self.assertEqual((m.uid, m.gid, m.uname, m.gname), (0, 0, "root", "root"), m.name)
            self.assertFalse(m.mode & 0o022, m.name)


class Units(unittest.TestCase):
    def test_sha512_crypt_known_vectors(self):
        # From the glibc / Drepper SHA-crypt specification.
        self.assertEqual(pb.sha512_crypt("Hello world!", "saltstring"),
                         "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEdFCoEOfaS35inz1")
        self.assertEqual(pb.sha512_crypt("Hello world!", "saltstringsaltstring", rounds=10000),
                         "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNSnCM/UrjmM0Dp8vO"
                         "uZeHBy/YTBmSK6H9qs/y3RnOaw5v.")

    def test_sha512_crypt_matches_openssl(self):
        if not shutil.which("openssl"):
            self.skipTest("no openssl")
        for pw, salt in (("change me", "abcdefgh12345678"), ("p'w $(id)", "./AZaz09"), ("ü" * 70, "x")):
            r = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input=pw,
                               capture_output=True, text=True)
            if r.returncode or not r.stdout.startswith("$6$"):
                self.skipTest("this openssl has no SHA-512 crypt (passwd -6)")
            self.assertEqual(pb.sha512_crypt(pw, salt), r.stdout.strip())

    def test_ssh_keys(self):
        self.assertEqual(pb.ssh_keys([ED]), [ED])
        with self.assertRaises(SystemExit):
            pb.ssh_keys(["~/no/such/file.pub"])
        with self.assertRaises(SystemExit):
            pb.ssh_keys(["not a key"])

    def test_private_key_file_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "id_ed25519"
            p.write_text(PRIVATE)
            with self.assertRaises(SystemExit):
                pb.ssh_keys([str(p)])
            pub = Path(d) / "id_ed25519.pub"
            pub.write_text("# comment\n\n" + ED + "\n")
            self.assertEqual(pb.ssh_keys([str(pub)]), [ED])
            pub.write_text(ED + "\x07bell\n")
            with self.assertRaises(SystemExit):
                pb.ssh_keys([str(pub)])

    def test_yaml_quote(self):
        self.assertEqual(pb.yq('a"b\n'), '"a\\"b\\n"')
        self.assertEqual(pb.yq("🦡"), '"🦡"')


class Plan(unittest.TestCase):
    def test_plan_runs(self):
        for args in (["--tier", "field"], ["--tier", "ultimate", "-v", "--langs", "eng,spa"],
                     ["--tier", "base", "--budget-gb", "64"]):
            with self.subTest(args=args):
                r = run([PY, str(BUILD), "plan", *args])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("items", r.stdout)
                self.assertIn("Drive", r.stdout)

    def test_plan_rejects_unknown_tier(self):
        r = run([PY, str(BUILD), "plan", "--tier", "mega"])
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
