#!/usr/bin/env python3
"""
Red team — defaults, install-time choices and the front panel (adversaries: a wardriver, a
visitor in the room, a raider or thief holding the box, a compromised "helpful" content peer).
Static checks against the shipped config/renderer/installer plus the real panel menu code.

Each failing test is a finding (`# FINDING:`). Run: python3.11 tests/test_redteam_config.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="rt-cfg-"))
os.environ.setdefault("PREPPER_SDR_DIR", str(TMP / "run-sdr"))
os.environ.setdefault("PREPPER_SDR_STATE", str(TMP / "state-sdr"))
os.environ.setdefault("PANEL_TIMEFMT", str(TMP / "timefmt"))
sys.path.insert(0, str(REPO / "software" / "panel"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


render = load("rt_render", REPO / "os" / "render.py")
EXAMPLE = tomllib.loads((REPO / "os" / "prepper.toml.example").read_text())
CTL = (REPO / "os" / "bin" / "prepperctl").read_text()
BUILDER = (REPO / "builder" / "prepper-build").read_text()


class WardriverDefaults(unittest.TestCase):
    def test_default_hotspot_is_open_and_named_honeybadger(self):
        # FINDING: prepper.toml.example and `prepper-build --ssid` default to SSID "HoneyBadger" with an empty
        # passphrase → an OPEN AP (render.hostapd adds no wpa= line). A wardriver's list of SSIDs is a list of
        # prepared households; and on an open AP every frame (admin PIN, member cookies, vault passphrase,
        # decrypted documents — all plain HTTP by default) is readable by anyone in range without joining.
        conf = render.hostapd(EXAMPLE, "wlan1")
        self.assertIn("wpa=2", conf, "default hotspot is open")
        self.assertNotIn("ssid=HoneyBadger", conf, "default SSID names the product")

    def test_builder_defaults_match(self):
        self.assertNotIn('p.add_argument("--ssid", default="HoneyBadger")', BUILDER)
        self.assertNotIn('p.add_argument("--passphrase", default=""', BUILDER)

    def test_clients_on_the_hotspot_are_not_isolated(self):
        # FINDING (low): ap_isolate=0 — a visitor's device can reach (ARP-spoof, port-scan) family phones on
        # the same AP. With the portal on plain HTTP this enables active MITM even on a WPA2 network.
        self.assertIn("ap_isolate=1", render.hostapd(EXAMPLE, "wlan1"))


class SameOriginCode(unittest.TestCase):
    def nginx(self):
        return render.nginx(EXAMPLE, tls_ready=True)

    def test_apps_are_served_same_origin_without_csp(self):
        # FINDING: /apps/ (data_root/apps) gets neither the sandbox nor app_csp, and the kiwix-js item there is
        # a git checkout of github.com/kiwix/kiwix-js at HEAD (catalog: unpinned, unsigned; resolve_git only
        # records the commit). Its JS runs on http://badger.lan with the family's SameSite=Strict cookies
        # attached to /api/ calls: whoever controls that repo's HEAD — or any trusted peer/USB-bundle signer,
        # since offers carry their own sha256 — owns every family session that opens a ZIM in Kiwix JS.
        block = [ln for ln in self.nginx().splitlines() if "location /apps/" in ln][0]
        self.assertTrue("sandbox" in block or "Content-Security-Policy" in block, block)
        cat = (REPO / "content" / "catalog.toml").read_text()
        self.assertNotIn('source = { type = "git", url = "https://github.com/kiwix/kiwix-js.git", ref = "HEAD" }', cat)

    def test_usb_drives_are_browsable_by_every_visitor(self):
        # FINDING: location /files/ { alias /media/; autoindex on; } — no auth. Any stick a family member
        # plugs in (photos, a vault export, a supplies CSV) is a public directory listing on the Wi-Fi.
        block = [ln for ln in self.nginx().splitlines() if "location /files/" in ln][0]
        self.assertNotIn("autoindex on", block)

    def test_nginx_keeps_an_access_log(self):
        # FINDING (low, at rest): the server block sets no access_log, so Debian's default
        # /var/log/nginx/access.log records every visitor IP, time, URL (incl. /api/vault/export/<token>,
        # /api/missing/photo/<id>) and User-Agent (phone model). A raider gets a visit diary.
        self.assertIn("access_log off", self.nginx())


class LocalCAAndSecrets(unittest.TestCase):
    def test_local_ca_has_no_name_constraints(self):
        # FINDING: `prepperctl gen-tls` makes a 20-year root CA with only basicConstraints/keyUsage. Families
        # install it on phones (iOS: profile + full trust) to use https://badger.lan. ca.key stays on the
        # Badger (/etc/prepper/tls/ca.key, unencrypted). A raider/thief who images the disk can mint a valid
        # certificate for ANY site (bank, mail, meshtastic.org) that every family phone trusts.
        gen = CTL[CTL.index("  gen-tls)"):CTL.index("  gen-key)")]
        self.assertIn("nameConstraints", gen, "unconstrained root CA, key kept on the device")

    def test_set_pin_puts_the_pin_in_argv(self):
        # FINDING (low): set-pin reads the PIN from stdin to keep it out of `ps` … then runs
        # `server.py hash-pin "$pin"`, where it is visible in `ps`/procfs to every local user for that moment.
        seg = CTL[CTL.index("  set-pin)"):CTL.index("  set-pin)") + 1500]
        self.assertNotRegex(seg, r'hash-pin "\$pin"')

    def test_device_signing_key_has_no_passphrase(self):
        # FINDING (raider): gen-key makes /etc/prepper/keys/disk_ed25519 with -N "" and lists it in
        # allowed_signers. Whoever images the disk can sign peer manifests / USB bundles that this Badger,
        # and every Badger that `prepperctl trust`-ed it, will install (APKs, ZIMs, models, /apps/ code).
        seg = CTL[CTL.index("  gen-key)"):CTL.index("  trust)")]
        self.assertNotIn('-N ""', seg)

    def test_boot_order_allows_usb_boot(self):
        # FINDING (thief/raider, low): BOOT_ORDER=0xf416 (NVMe, SD, USB). With no disk encryption, anyone with
        # the box boots their own USB OS and reads/modifies everything, then returns it.
        install = (REPO / "os" / "install.sh").read_text()
        self.assertNotIn("BOOT_ORDER=0xf416", install)

    def test_no_disk_encryption_or_panic_wipe_exists(self):
        # FINDING (design): nothing in os/ sets up LUKS / an encrypted data partition, and there is no wipe verb.
        blob = CTL + (REPO / "os" / "install.sh").read_text()
        self.assertTrue(re.search(r"cryptsetup|luks|panic|wipe", blob, re.I), "no at-rest encryption, no panic wipe")


class FrontPanelHasNoLock(unittest.TestCase):
    """A visitor in the room, an aid volunteer at the table, a child, a raider holding the box."""

    class Disp:
        def __init__(self):
            self.qrs, self.texts = [], []

        def lines(self, rows, title=None, sel=None):
            self.texts.append(list(rows))

        def qr(self, data, caption):
            self.qrs.append((data, caption))

        def sleep(self, on):
            pass

    def make(self, link: Path, pin: Path, panel_cfg: dict | None = None):
        import panel
        panel.API = "http://127.0.0.1:9"
        d = self.Disp()
        p = panel.Panel(d, {"network": {"ssid": "Smiths", "passphrase": "correct horse battery", "hostname": "badger"},
                            "portal": {"mesh_channel_link": str(link), "mesh_bt_pin_file": str(pin),
                                       "features_data": str(TMP / "feat")},
                            "panel": {"lock_file": str(TMP / "panel-lock.json"), **(panel_cfg or {})}})
        p.spawn = lambda fn: fn()
        return p, d

    def secrets_files(self):
        link, pin = TMP / "link.json", TMP / "pin.json"
        link.write_text(json.dumps({"url": "https://meshtastic.org/e/#FAMILY-PSK-SECRET", "at": 1}))
        pin.write_text(json.dumps({"pin": "482913", "at": 1}))
        return link, pin

    def tearDown(self):
        (TMP / "panel-lock.json").unlink(missing_ok=True)

    @staticmethod
    def leaks(d, wifi=True):
        return [q for q in d.qrs if "FAMILY-PSK-SECRET" in q[0] or (wifi and "correct horse" in q[0])] + \
               [t for t in d.texts if any("482913" in x for x in t)]

    def test_knob_shows_family_radio_key_wifi_password_and_bt_pin_without_any_pin(self):
        # FINDING: Panel.root() offers "Family radio → Channel link (QR)" (the family PSK), "Bluetooth PIN",
        # "Join Wi-Fi" (the full password), "Invite member (QR)" (→ a family device key), "Services → Alerts
        # off / Mesh off", "Import from USB" and "Shut down" to whoever turns the knob. The only gate is a
        # yes/no confirm. In an aid-station or shared-room setup, or for a raider who returns the unit,
        # physical access = family membership + radio key + silenced alerts.
        # FIXED with the optional panel code. CHANGED test: the owner's rules are that the lock is OFF until
        # they set a code (so nothing changes on its own) and that Join Wi-Fi stays open by default (the way
        # back onto the network); both are configurable. So the panel here has a code set, and the Wi-Fi
        # password is checked separately with "wifi_join" added to [panel] lock_protect.
        import panel
        link, pin = self.secrets_files()
        panel.PanelLock(TMP / "panel-lock.json").set("2468")
        p, d = self.make(link, pin)
        labels = [r[0] for r in p.root()]
        p.radio_link_qr()
        p.bt_pin()
        self.assertEqual(p.screen, "code")
        p.wifi_screen()                                  # open by default (owner's choice)
        self.assertEqual(p.screen, "qr")
        self.assertIn("Invite member (QR)", labels)
        self.assertEqual(self.leaks(d, wifi=False), [], "secrets shown on the panel with no unlock step")
        p, d = self.make(link, pin, {"lock_protect": ["invite", "radio", "services", "wifi_off", "usb_import",
                                                      "shutdown", "wifi_join"]})
        p.radio_link_qr()
        p.bt_pin()
        p.wifi_screen()
        p.invite_qr()
        self.assertEqual(self.leaks(d), [], "secrets shown on the panel with no unlock step")

    def test_every_guarded_entry_asks_for_the_code_and_the_right_code_opens_it(self):
        import panel
        link, pin = self.secrets_files()
        panel.PanelLock(TMP / "panel-lock.json").set("2468")
        p, d = self.make(link, pin)
        menu = dict(p.root())
        sysm = dict(menu["System"])
        upd = dict(menu["Updates"])
        wifi = dict(menu["Wi-Fi"])
        for name, target in (("Invite", menu["Invite member (QR)"]), ("Family radio", menu["Family radio"]),
                             ("Services", menu["Services"]), ("Wi-Fi off", wifi["Turn off"]),
                             ("USB import", upd["Import from USB"]), ("Shut down", sysm["Shut down"]),
                             ("Restart", sysm["Restart"])):
            p.screen, p.stack = "menu", [("Menu", list(menu.items()))]
            target()
            self.assertEqual(p.screen, "code", name)
        p.screen, p.stack = "home", []
        p.hold()                                         # hold-to-shut-down on the home screen too
        self.assertEqual(p.screen, "code")
        p.screen, p.stack = "menu", [("Menu", list(menu.items()))]
        menu["Open app (QR)"]()                          # open by default
        self.assertEqual(p.screen, "qr")
        # enter 2468 with the knob, then OK: the family radio link is shown
        p.stack = [("Menu", list(menu.items()))]
        p.radio_link_qr()
        for digit in "2468":
            p.code_sel = int(digit)
            p.press()
        p.code_sel = p.code_choices().index("OK")
        p.press()
        self.assertTrue(any("FAMILY-PSK-SECRET" in q[0] for q in d.qrs))

    def test_wrong_codes_slow_down_and_survive_a_restart(self):
        import panel
        clock = [1_800_000_000.0]
        lock = panel.PanelLock(TMP / "panel-lock.json", clock=lambda: clock[0])
        rec = lock.set("2468")
        for _ in range(panel.LOCK_FREE_TRIES - 1):
            self.assertEqual(lock.check("0000")[0], "bad")
        st, wait = lock.check("0000")
        self.assertEqual(st, "bad")
        self.assertGreaterEqual(wait, panel.LOCK_BASE_WAIT - 1)
        self.assertEqual(lock.check("2468")[0], "wait")              # even the right code waits
        again = panel.PanelLock(TMP / "panel-lock.json", clock=lambda: clock[0])   # (a restart)
        self.assertGreater(again.wait(), 0)
        clock[0] += 10_000
        self.assertEqual(again.check("2468")[0], "ok")
        raw = (TMP / "panel-lock.json").read_text()
        self.assertNotIn("2468", raw)
        self.assertNotIn(rec, raw)
        self.assertEqual((TMP / "panel-lock.json").stat().st_mode & 0o777, 0o600)
        # the recovery code removes the lock (the way back for a forgotten code)
        self.assertEqual(again.recover("1" * 12)[0], "bad")
        self.assertEqual(again.recover(rec)[0], "ok")
        self.assertFalse(again.is_set())

    def test_a_visitor_cannot_set_a_code_unless_the_admin_armed_it(self):
        import panel
        link, pin = self.secrets_files()
        p, d = self.make(link, pin)
        p.arm_file = TMP / "arm.json"
        p.lock_menu()
        self.assertNotIn("Set a code", [r for r, _ in p.stack[-1][1]])
        (TMP / "arm.json").write_text(json.dumps({"until": __import__("time").time() + 600}))
        panel._file_cache.clear()
        p.lock_menu()
        self.assertIn("Set a code", [r for r, _ in p.stack[-1][1]])
        (TMP / "arm.json").unlink()


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
