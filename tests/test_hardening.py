#!/usr/bin/env python3
"""
Install-time and supply-chain hardening (red-team findings 2, 8, 9, 12): the hotspot's defaults and
modes (render.py, prepper-build), the apps origin, logs at rest, and upd's peer-trust scope, git
commit pins and the encrypted device signing key.

Run: python3.11 tests/test_hardening.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "upd"))
import upd  # noqa: E402


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


render = load("hard_render", REPO / "os" / "render.py")
pb = load("hard_build", REPO / "builder" / "prepper-build")
EXAMPLE = tomllib.loads((REPO / "os" / "prepper.toml.example").read_text())


def cfg(**network) -> dict:
    c = json.loads(json.dumps(EXAMPLE))
    c["network"].update(network)
    return c


def kv(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out.setdefault(k, []).append(v)
    return out


class HotspotModes(unittest.TestCase):
    def test_example_is_protected_and_neutral(self):
        h = kv(render.hostapd(EXAMPLE, "wlan1"))
        self.assertEqual(h["ssid"], ["Home-Hotspot"])
        self.assertEqual(h["wpa"], ["2"])
        self.assertEqual(h["wpa_key_mgmt"], ["OWE"])          # no passphrase in the template: Enhanced Open
        self.assertEqual(h["ieee80211w"], ["2"])
        self.assertEqual(h["ap_isolate"], ["1"])

    def test_wpa2_with_passphrase(self):
        h = kv(render.hostapd(cfg(passphrase="correct horse battery"), "wlan1"))
        self.assertEqual((h["wpa"], h["wpa_passphrase"]), (["2"], ["correct horse battery"]))
        self.assertEqual(h["wpa_key_mgmt"], ["WPA-PSK SAE"])
        h = kv(render.hostapd(cfg(passphrase="correct horse battery"), "wlan0"))
        self.assertEqual(h["wpa_key_mgmt"], ["WPA-PSK"])

    def test_open_must_be_asked_for_by_name(self):
        h = kv(render.hostapd(cfg(passphrase="", security="open"), "wlan1"))
        self.assertNotIn("wpa", h)
        self.assertEqual(h["ap_isolate"], ["1"])
        with self.assertRaises(render.ConfigError):
            render.hostapd(cfg(passphrase="", security="wpa2"), "wlan1")
        with self.assertRaises(render.ConfigError):
            render.check(cfg(security="wep"), "wlan1")

    def test_owe_on_the_onboard_radio_falls_back_to_open_and_says_so(self):
        conf = render.hostapd(cfg(passphrase="", security="owe"), "wlan0")
        self.assertNotIn("wpa=", conf)
        self.assertIn("not supported by the onboard radio", conf)

    def test_security_overrides_a_leftover_passphrase(self):
        h = kv(render.hostapd(cfg(passphrase="correct horse battery", security="owe"), "wlan1"))
        self.assertEqual(h["wpa_key_mgmt"], ["OWE"])
        self.assertNotIn("wpa_passphrase", h)

    def test_product_ssid_warns(self):
        with tempfile.TemporaryDirectory() as d:
            import contextlib
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                render.render_all(cfg(ssid="HoneyBadger"), Path(d), "wlan1")
            self.assertIn("tells everyone in range", err.getvalue())
            self.assertIn("no password", err.getvalue())

    def test_leases_and_logs_not_kept(self):
        d = render.dnsmasq(EXAMPLE, "wlan1")
        self.assertIn("dhcp-leasefile=/run/prepper-dnsmasq/leases", d)
        self.assertIn("quiet-dhcp", d)
        n = render.nginx(EXAMPLE, tls_ready=True)
        self.assertEqual(n.count("access_log off;"), 3)  # launcher + apps origin + Kolibri HTML5-app origin
        self.assertNotIn("autoindex on; add_header X-Content-Type-Options nosniff always; location ~*", n)

    def test_apps_origin(self):
        n = render.nginx(EXAMPLE, tls_ready=True)
        self.assertIn("listen 8088;", n)
        self.assertIn("listen 8443 ssl;", n)
        self.assertIn("map $scheme $prepper_apps_port { https 8443; default 8088; }", n)
        n = render.nginx(EXAMPLE, tls_ready=False)
        self.assertNotIn("8443", n)
        self.assertIn("map $scheme $prepper_apps_port { https 8088; default 8088; }", n)


class Installer(unittest.TestCase):
    def test_installer_at_rest(self):
        sh = (REPO / "os" / "install.sh").read_text()
        self.assertIn("BOOT_ORDER=0xf16", sh)
        self.assertNotIn("0xf416", sh)
        self.assertIn("Storage=volatile", sh)
        self.assertIn("/run/prepper-dnsmasq", sh)
        self.assertIn("secrets.choice", sh)                       # a fresh hand-run install gets a Wi-Fi password


class Builder(unittest.TestCase):
    def args(self, **kw):
        a = dict(tier="base", langs="eng", hostname="badger", ssid=None, passphrase=None, country="US",
                 timezone="America/Denver", pin=None, uplink=None, relay=None, peer=None, trust_key=None,
                 wifi_security="wpa2", for_sale=False, ssh_key=None, ssh_password=None)
        a.update(kw)
        return types.SimpleNamespace(**a)

    def test_defaults_neutral_name_random_password(self):
        a = self.args()
        t = tomllib.loads(pb.render_config(a))
        self.assertRegex(t["network"]["ssid"], r"^Home-[A-Z2-9]{4}$")
        self.assertRegex(t["network"]["passphrase"], r"^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$")
        self.assertEqual(t["network"]["security"], "wpa2")
        b = self.args()
        self.assertNotEqual(tomllib.loads(pb.render_config(b))["network"]["passphrase"], t["network"]["passphrase"])

    def test_refuses_product_names(self):
        for name in ("HoneyBadger", "honey badger", "Honey-Badger 2", "PrepperNet", "Smiths HONEYBADGER"):
            with self.subTest(name=name), self.assertRaises(SystemExit):
                pb.render_config(self.args(ssid=name))
        self.assertEqual(tomllib.loads(pb.render_config(self.args(ssid="Garden-Shed")))["network"]["ssid"], "Garden-Shed")

    def test_no_password_modes(self):
        t = tomllib.loads(pb.render_config(self.args(wifi_security="owe")))
        self.assertEqual((t["network"]["passphrase"], t["network"]["security"]), ("", "owe"))
        t = tomllib.loads(pb.render_config(self.args(wifi_security="open")))
        self.assertEqual(t["network"]["security"], "open")
        with self.assertRaises(SystemExit):
            pb.render_config(self.args(passphrase=""))                  # "" no longer silently means open
        with self.assertRaises(SystemExit):
            pb.render_config(self.args(wifi_security="owe", passphrase="correct horse"))
        h = kv(render.hostapd(tomllib.loads(pb.render_config(self.args(passphrase="correct horse"))), "wlan1"))
        self.assertEqual(h["wpa_passphrase"], ["correct horse"])


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class PeerTrustScope(unittest.TestCase):
    CATALOG = """
[[item]]
id = "z1"
title = "Z"
category = "reference"
kind = "zim"
size_gb = 0.0001
tiers = ["base"]
source = { type = "manual" }

[[item]]
id = "apk1"
title = "An app"
category = "tools"
kind = "apk"
size_gb = 0.0001
tiers = ["base"]
source = { type = "manual" }

[[item]]
id = "pinned"
title = "Pinned app"
category = "tools"
kind = "apk"
size_gb = 0.0001
tiers = ["base"]
source = { type = "manual", sha256 = "%s" }

[[item]]
id = "kjs"
title = "Kiwix JS"
category = "tools"
kind = "app"
size_gb = 0.0001
tiers = ["base"]
source = { type = "git", url = "https://example.invalid/k.git", ref = "refs/tags/1.0", commit = "%s" }
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hard-upd-")).resolve()
        self.good = b"good apk bytes"
        import hashlib
        self.pin = hashlib.sha256(self.good).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def disk(self, name: str, signer_line: str | None) -> upd.Config:
        d = self.tmp / name
        d.mkdir()
        (d / "catalog.toml").write_text(self.CATALOG % (self.pin, "a" * 40))
        (d / "prepper.toml").write_text("""
[upd]
data_root = "data"
state_file = "state/upd-state.json"
report_dir = "state/reports"
catalog = "catalog.toml"
tier = "base"
reserve_gb = 0
allowed_signers = "allowed_signers"
signing_key = "key"
require_signed_peers = true
""")
        if signer_line:
            (d / "allowed_signers").write_text(signer_line)
        return upd.Config.load(d / "prepper.toml")

    def signed_bundle(self, entries: dict[str, bytes], encrypted: bool) -> tuple[Path, list[str]]:
        import hashlib
        b = self.tmp / "usb"
        (b / "files").mkdir(parents=True)
        items = []
        for i, data in entries.items():
            (b / "files" / f"{i}.bin").write_bytes(data)
            items.append({"id": i, "filename": f"{i}.bin", "path": f"files/{i}.bin", "version": "2", "comparable": True,
                          "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        (b / "manifest.json").write_text(json.dumps({"disk_id": "peer", "generated": upd.now_iso(), "format": 1,
                                                     "items": items}))
        key = self.tmp / "signer"
        if encrypted:
            # like prepperctl gen-key: encrypted, passphrase in key.pass, given through SSH_ASKPASS
            (self.tmp / "signer.pass").write_text("s3cret-pass-phrase")
            ask = self.tmp / "ask"
            ask.write_text(f"#!/bin/sh\ncat {self.tmp / 'signer.pass'}\n")
            ask.chmod(0o700)
            import os
            env = dict(os.environ, SSH_ASKPASS=str(ask), SSH_ASKPASS_REQUIRE="force", DISPLAY="none")
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-C", "peer", "-f", str(key)], check=True,
                           stdin=subprocess.DEVNULL, env=env)
        else:
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "peer", "-f", str(key)], check=True,
                           stdin=subprocess.DEVNULL)
        signer = types.SimpleNamespace(signing_key=key)
        self.assertTrue(upd.sign_file(b / "manifest.json", signer), "signing works (encrypted key via askpass)")
        pub = (self.tmp / "signer.pub").read_text().split()
        return b, pub

    def plan(self, cfg, bundle):
        offers = upd.load_offer(str(bundle), cfg)
        return {p.item.id: p for p in upd.build_plan(cfg, offline=True, offers=offers)}

    def test_content_signer_brings_content_not_code(self):
        bundle, pub = self.signed_bundle({"z1": b"zim", "apk1": b"evil apk", "pinned": self.good, "kjs": b"x"}, encrypted=True)
        cfg = self.disk("content", f'prepper-update namespaces="prepper-update" {pub[0]} {pub[1]}\n')
        p = self.plan(cfg, bundle)
        self.assertEqual(p["z1"].status, "install")
        self.assertIn(p["apk1"].status, ("manual", "offline"), "an APK from a content-only signer is not taken")
        self.assertEqual(p["pinned"].status, "install", "matches the catalog's sha256 pin: fine from anyone trusted")
        self.assertNotEqual(p["kjs"].status, "install", "git-pinned code never comes from a peer")

    def test_code_signer_brings_code_but_pins_still_rule(self):
        bundle, pub = self.signed_bundle({"apk1": b"an apk", "pinned": b"not the pinned bytes", "kjs": b"x"}, encrypted=False)
        cfg = self.disk("code", f'prepper-code namespaces="prepper-update" {pub[0]} {pub[1]}\n')
        p = self.plan(cfg, bundle)
        self.assertEqual(p["apk1"].status, "install")
        self.assertNotEqual(p["pinned"].status, "install", "a file that doesn't match the catalog pin is refused")
        self.assertNotEqual(p["kjs"].status, "install")

    def test_untrusted_signer(self):
        bundle, _ = self.signed_bundle({"z1": b"zim"}, encrypted=False)
        cfg = self.disk("none", None)
        self.assertEqual(upd.load_offer(str(bundle), cfg), {})


class GitPin(unittest.TestCase):
    def item(self, **src):
        return upd.Item(id="kjs", title="K", category="tools", kind="app",
                        source={"type": "git", "url": "https://example.invalid/k.git", **src},
                        size_gb=0.0, tiers=["base"], renderer="app")

    def ls_remote(self, out):
        return lambda *a, **k: types.SimpleNamespace(stdout=out, returncode=0)

    def test_pin_must_match_upstream(self):
        import unittest.mock as mock
        c = types.SimpleNamespace(timeout=5)
        pin = "e2c89eb692cf9c58b36986b6f42eecf7a59c7357"
        tag_obj = "1" * 40
        with mock.patch.object(upd, "have", lambda x: True), \
                mock.patch.object(upd.subprocess, "run", self.ls_remote(
                    f"{tag_obj}\trefs/tags/4.4.0\n{pin}\trefs/tags/4.4.0^{{}}\n")):
            r = upd.resolve_git(self.item(ref="refs/tags/4.4.0", commit=pin), c)
        self.assertEqual((r.version, r.meta["commit"], r.meta.get("pinned")), (pin[:12], pin, True))
        with mock.patch.object(upd, "have", lambda x: True), \
                mock.patch.object(upd.subprocess, "run", self.ls_remote(f"{'2' * 40}\trefs/tags/4.4.0\n")):
            with self.assertRaises(ValueError):
                upd.resolve_git(self.item(ref="refs/tags/4.4.0", commit=pin), c)
        with self.assertRaises(ValueError):
            with mock.patch.object(upd, "have", lambda x: True):
                upd.resolve_git(self.item(ref="refs/tags/4.4.0", commit="abc"), c)

    def test_shipped_catalog_pins_kiwix_js(self):
        items = {i.id: i for i in upd.load_catalog(REPO / "content" / "catalog.toml")}
        src = items["kiwix-js"].source
        self.assertRegex(src["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(src["ref"].startswith("refs/tags/"))

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_checkout_must_be_exactly_the_pin(self):
        with tempfile.TemporaryDirectory() as d:
            g = ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@x"]
            subprocess.run(["git", "init", "-q", d], check=True)
            (Path(d) / "a.js").write_text("1")
            subprocess.run(g + ["add", "."], check=True)
            subprocess.run(g + ["commit", "-qm", "v1"], check=True)
            head = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            upd.git_checkout_ok(Path(d), head)
            with self.assertRaises(RuntimeError):
                upd.git_checkout_ok(Path(d), "0" * 40)
            (Path(d) / "a.js").write_text("steal()")
            with self.assertRaises(RuntimeError):
                upd.git_checkout_ok(Path(d), head)


if __name__ == "__main__":
    unittest.main()
