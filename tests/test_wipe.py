#!/usr/bin/env python3
"""
Tests for the panic wipe, handover reset, duress deferral and dead-man switch (os/wipe.py through
os/bin/prepperctl), run unprivileged against a scratch root ($R test seam) with system commands
stubbed. A seized-disk check greps every byte left in the fake root for the family's secrets.

    python3.11 tests/test_wipe.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import threading
import time
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from os_helpers import BASH, PY, REPO, STUB_NAMES, Stubs, TempDirCase, example_config, run  # noqa: E402

CTL = REPO / "os/bin/prepperctl"
spec = importlib.util.spec_from_file_location("wipe_under_test", REPO / "os/wipe.py")
wipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wipe)

# Every one of these must be gone (or unreadable) after a wipe.
SECRETS = {
    "Lucia Garcia": "member names",
    "house empty till 18:00": "family board",
    "false wall in the cellar": "supplies / cache locations",
    "Insulin for Mrs Ortiz": "aid-station log",
    "ham radio licence": "skills",
    "MISSING-PERSON-TOM": "missing people",
    "FAMILYPSK": "family radio key (link)",
    "482913": "radio Bluetooth PIN",
    "SEALEDWRAP-KEY": "sealed-record key wrap",
    "SEALED-RECORD-BODY": "sealed records",
    "VAULTWRAP-KEY": "vault wrapped key",
    "PANELKEY-SECRET": "panel key",
    "PANELLOCK-HASH": "panel lock",
    "AAAAOWNDEVICEKEY": "device signing key (pub, in allowed_signers too)",
    "DEVICE-PRIVATE-KEY": "device signing key",
    "SIGNING-PASSPHRASE": "signing key passphrase",
    "TLS-SERVER-KEY": "TLS key",
    "OldPassw0rd!": "hotspot Wi-Fi password",
    "UplinkSecret9": "uplink Wi-Fi password",
    "Lucias-iPhone": "DHCP leases",
    "10.10.10.77 GET": "nginx log",
    "Hendersons": "mesh nodes",
    "!9f8e7d6c": "mesh peers",
    "39.7512": "house position",
    "OLD-WIFI-PENDING": "old Wi-Fi password of a pending change",
    "CIRCLEPSK": "trusted circle key (RAM copy)",
}


class WipeCase(TempDirCase):
    EXTRA_CONF: dict = {}

    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("the test seam is disabled for root; run the tests as a normal user")
        self.root = self.tmp / "root"
        for d in ("etc/prepper/keys", "etc/prepper/tls", "etc/default", "var/lib/prepper/portal/features", "media",
                  "run/prepper", "srv/prepper/zim", "srv/prepper/maps", "boot/firmware/prepper", "proc/sys/kernel/random"):
            (self.root / d).mkdir(parents=True)
        opt = self.root / "opt/prepper"
        (opt / "os").mkdir(parents=True)
        for f in ("render.py", "conf_write.py", "nm_wifi.py", "wipe.py", "prepper.toml.example", "bin", "systemd"):
            (opt / "os" / f).symlink_to(REPO / "os" / f)
        (opt / "software/upd").mkdir(parents=True)
        (opt / "software/common").mkdir(parents=True)
        (opt / "software/portal").symlink_to(REPO / "software/portal")
        self.fake_log = self.tmp / "fake.log"
        self.fake_log.touch()
        (opt / "software/upd/upd.py").write_text(f"#!{PY}\nimport sys\nsys.exit(0)\n")
        self.conf = self.root / "etc/prepper/prepper.toml"
        sections = {"network": {"passphrase": "OldPassw0rd!", "https": False, "ssid": "Smiths",
                                "uplink_wifi": [{"ssid": "Home", "psk": "UplinkSecret9"}]},
                    "upd": {"peers": ["http://10.10.10.11/sync"]},
                    "time": {"latitude": 39.7512, "longitude": -104.9871}}
        for k, v in self.EXTRA_CONF.items():
            sections.setdefault(k, {}).update(v)
        self.conf.write_text(example_config(**sections))
        self.conf.chmod(0o640)
        self.stubs = Stubs(self.tmp, STUB_NAMES + ("journalctl", "ssh-keygen", "openssl"))

    # ── a Badger after a normal family life ──
    def populate(self):
        r = self.root
        pdir = r / "var/lib/prepper/portal"
        feats = pdir / "features"

        def w(rel, text, base=r):
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            return p
        w("members.json", json.dumps({"members": {"m1": {"name": "Lucia Garcia (age 7)"}}}), pdir)
        w("members.json.bak", json.dumps({"members": {"m1": {"name": "Lucia Garcia (age 7)"}}}), pdir)
        w("family-board.jsonl", json.dumps({"text": "Leaving, house empty till 18:00"}) + "\n", pdir)
        w("board.jsonl", json.dumps({"text": "hello from Lucia Garcia"}) + "\n", pdir)
        w("mesh-channel.json", json.dumps({"url": "https://meshtastic.org/e/#CiISIFAMILYPSK"}), pdir)
        w("mesh-bt-pin.json", json.dumps({"pin": "482913"}), pdir)
        w("mesh-nodes.json", json.dumps({"nodes": [{"name": "Hendersons", "lat": 39.7512}]}), pdir)
        w("mesh-peers.json", json.dumps({"peers": [{"id": "!9f8e7d6c"}]}), pdir)
        w("mesh-telemetry.jsonl", '{"from": "!9f8e7d6c"}\n', pdir)
        w("mesh-outbox.jsonl", '{"text": "Lucia Garcia is fine"}\n', pdir)
        w("mesh-pending.json", '[{"text": "Lucia Garcia"}]', pdir)
        w("portal-settings.json", '{"display_time": "utc"}', pdir)
        w("posture.json", '{"level": "neighbourly", "changed_at": 1, "by": "admin"}', pdir)
        w("mesh-radio-saved.json", json.dumps({"name": "Hendersons relay"}), pdir)
        w("mesh-circle-slot.json", '{"slot": 2}', pdir)
        w("circles/records.json", json.dumps({"rows": ["ham radio licence"]}), feats)
        w("run/prepper/circle-status.json", json.dumps({"asked": "!9f8e7d6c"}))
        w("run/prepper/circle-ask.json", json.dumps({"q": "Hendersons"}))
        w("run/prepper/mesh-peers.json", json.dumps({"peers": [{"id": "!9f8e7d6c"}]}))
        w("run/prepper/mesh-bt-pin.json", json.dumps({"pin": "482913"}))
        w("run/prepper/mesh-telemetry.jsonl", '{"from": "!9f8e7d6c"}\n')
        w("mesh-silent", "", pdir)
        w("run/prepper/mesh-circle.json", json.dumps({"url": "https://meshtastic.org/e/#CIRCLEPSK"}))
        w("run/prepper/mesh-channel.json", json.dumps({"url": "https://meshtastic.org/e/#CiISIFAMILYPSK"}))
        w("run/prepper/mesh-nodes.json", json.dumps({"nodes": [{"name": "Hendersons"}]}))
        w("checkin.json", '{"seq": 4}', pdir)
        w("personal-content.json", '{"format": 1}', pdir)
        w("sealed/wrapped.key", "SEALEDWRAP-KEY", pdir)
        w("sealed-records/rec1.bin", "SEALED-RECORD-BODY", pdir)
        w("supplies/items.json", json.dumps({"items": [{"location": "false wall in the cellar"}]}), feats)
        w("aidstation/log.json", json.dumps({"rows": ["Insulin for Mrs Ortiz"]}), feats)
        w("skills/skills.json", json.dumps({"rows": ["ham radio licence"]}), feats)
        w("missing/posts.json", "MISSING-PERSON-TOM", feats)
        w("missing/photos/a.jpg", "\xff\xd8MISSING-PERSON-TOM", feats)
        w("vault/vault.json", json.dumps({"wrap": "VAULTWRAP-KEY"}), feats)
        w("vault/blobs/b1", "ciphertext", feats)
        w("forecast/latest.json", '{"text": "sunny"}', feats)
        w("satellites/posted.json", '{"n": 1}', feats)
        w("sensors/readings.db", "tank 40%", feats)
        w("etc/prepper/panel.key", "PANELKEY-SECRET")
        w("etc/prepper/panel.key.sha256", "0" * 64)
        w("etc/prepper/panel-lock.json", '{"hash": "PANELLOCK-HASH"}')
        w("etc/prepper/keys/disk_ed25519", "DEVICE-PRIVATE-KEY")
        w("etc/prepper/keys/disk_ed25519.pass", "SIGNING-PASSPHRASE")
        w("etc/prepper/keys/disk_ed25519.pub", "ssh-ed25519 AAAAOWNDEVICEKEY prepper-disk-01\n")
        w("etc/prepper/allowed_signers", 'prepper-update namespaces="prepper-update" ssh-ed25519 AAAAOWNDEVICEKEY\n'
                                         'prepper-update namespaces="prepper-update" ssh-ed25519 AAAAOTHERBADGER\n')
        w("etc/prepper/sealed/master.key", "SEALEDWRAP-KEY")
        w("etc/prepper/tls/server.key", "TLS-SERVER-KEY")
        w("etc/prepper/tls/server.crt", "cert")
        w("etc/prepper/tls/ca.crt", "ca")
        w("var/lib/prepper/wifi-rotate-pending.json", json.dumps({"old": "OLD-WIFI-PENDING"}))
        w("var/lib/prepper/sdr/position.json", json.dumps({"lat": 39.7512}))
        w("var/lib/prepper/sdr/images/index.json", "[]")
        w("var/lib/prepper/upd-state.json", '{"items": {"wikipedia": {}}}')
        w("srv/prepper/zim/wikipedia_en.zim", "ZIM-LIBRARY-CONTENT")
        w("srv/prepper/maps/world.pmtiles", "MAP-CONTENT")
        w("etc/NetworkManager/system-connections/prepper-uplink-abc.nmconnection",
          "[connection]\nid=prepper-uplink-abc\ntype=wifi\n[wifi-security]\npsk=UplinkSecret9\n")
        w("etc/NetworkManager/system-connections/Wired.nmconnection", "[connection]\nid=Wired\ntype=ethernet\n")
        w("etc/netplan/50-cloud-init.yaml", "network:\n  version: 2\n  ethernets:\n    eth0:\n      dhcp4: true\n"
                                           "  wifis:\n    wlan0:\n      access-points:\n        \"Home\":\n"
                                           "          password: \"UplinkSecret9\"\n")
        w("boot/firmware/network-config", "network:\n  version: 2\n  wifis:\n    wlan0:\n      access-points:\n"
                                          "        Home:\n          password: UplinkSecret9\n")
        w("var/log/nginx/error.log", "10.10.10.77 GET /api/vault\n")
        w("var/log/nginx/access.log.1", "10.10.10.77 GET /\n")
        w("var/log/prepper-firstboot.log", "Lucia Garcia\n")
        w("run/prepper-dnsmasq/leases", "1 aa:bb 10.10.10.77 Lucias-iPhone *\n")
        w("proc/uptime", "1000.0 900.0\n")
        w("proc/sys/kernel/random/boot_id", "boot-1\n")

    # ── running prepperctl ──
    def ctl(self, *args, sudo_user=None, input=None, **env):
        env.setdefault("STUB_OUT_iw", "Interface x\n\ttype AP")
        env.setdefault("FAKE_LOG", self.fake_log)
        e = self.stubs.env(PREPPER_TEST_ROOT=self.root, PREPPER_TEST_PY=PY, **env)
        if sudo_user:
            e["SUDO_USER"] = sudo_user
        return run([BASH, str(CTL), *args], env=e, input=input, timeout=120)

    def ok(self, *args, **kw):
        r = self.ctl(*args, **kw)
        self.assertEqual(r.returncode, 0, f"{args}: {r.stdout}{r.stderr}")
        return r

    def token(self, verb="panic-wipe", **kw):
        r = self.ok(verb, **kw)
        m = re.search(r"confirm-token: ([A-Z0-9]{4}-[A-Z0-9]{4})", r.stdout)
        self.assertTrue(m, r.stdout)
        return m.group(1)

    def wipe(self, trigger="cli", verb="panic-wipe", extra=(), **kw):
        return self.ok(verb, "--confirm", self.token(verb), "--trigger", trigger, *extra, **kw)

    def leftovers(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for p in self.root.rglob("*"):
            rel = p.relative_to(self.root)
            if rel.parts[:1] == ("opt",) or not p.is_file() or p.is_symlink():
                continue
            data = p.read_bytes()
            for needle in SECRETS:
                if needle.encode() in data:
                    found.setdefault(needle, []).append(str(rel))
        return found

    def receipt(self) -> dict:
        return json.loads((self.root / "run/prepper/wipe-receipt.json").read_text())

    def cfg(self) -> dict:
        return tomllib.loads(self.conf.read_text())


class PanicWipe(WipeCase):
    def test_every_listed_artifact_is_gone_and_the_library_survives(self):
        self.populate()
        before = self.leftovers()
        self.assertEqual(set(before), set(SECRETS), "the fixture plants every secret")
        r = self.wipe()
        self.assertIn("wipe:", r.stdout)
        self.assertEqual(self.leftovers(), {}, "secrets readable after the wipe")
        root = self.root
        # the library, maps, AI and software stay: a plain offline library
        self.assertEqual((root / "srv/prepper/zim/wikipedia_en.zim").read_text(), "ZIM-LIBRARY-CONTENT")
        self.assertEqual((root / "srv/prepper/maps/world.pmtiles").read_text(), "MAP-CONTENT")
        self.assertTrue((root / "var/lib/prepper/upd-state.json").exists())
        self.assertTrue((root / "var/lib/prepper/sdr/images/index.json").exists())
        feats = root / "var/lib/prepper/portal/features"
        self.assertEqual(sorted(p.name for p in feats.iterdir()), ["forecast", "satellites"])
        self.assertTrue((root / "var/lib/prepper/portal/personal-content.json").exists(), "licence decision kept")
        post = json.loads((root / "var/lib/prepper/portal/posture.json").read_text())
        self.assertEqual(post["level"], "lockdown", "a panic wipe leaves the Badger in Lockdown")
        self.assertEqual(post["by"], "panic wipe")
        self.assertFalse((root / "var/lib/prepper/portal/mesh-radio-saved.json").exists())
        self.assertFalse((root / "var/lib/prepper/portal/mesh-circle-slot.json").exists())
        for f in ("mesh-circle.json", "circle-status.json", "circle-ask.json", "mesh-peers.json", "mesh-nodes.json"):
            self.assertFalse((root / "run/prepper" / f).exists(), f)
        self.assertTrue((root / "var/lib/prepper/portal/mesh-silent").exists(), "…nor ends radio silence")
        self.assertTrue((root / "var/lib/prepper/portal").is_dir())
        self.assertTrue((root / "etc/NetworkManager/system-connections/Wired.nmconnection").exists())
        self.assertIn("AAAAOTHERBADGER", (root / "etc/prepper/allowed_signers").read_text())
        self.assertIn("ethernets:", (root / "etc/netplan/50-cloud-init.yaml").read_text())
        # new hotspot password (shown only on the panel), uplink gone, config still valid
        c = self.cfg()
        self.assertNotEqual(c["network"]["passphrase"], "OldPassw0rd!")
        self.assertRegex(c["network"]["passphrase"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual(c["network"]["uplink_wifi"], [])
        self.assertEqual(c["network"]["ssid"], "Smiths", "a wipe keeps the name (the handover changes it)")
        self.assertEqual(self.conf.stat().st_mode & 0o777, 0o640)
        # journal, leases, nginx, launcher restart, mesh bridge stopped
        sc = self.stubs.argvs("systemctl")
        jc = self.stubs.argvs("journalctl")
        self.assertIn(["--rotate"], jc)
        self.assertIn(["--vacuum-time=1s"], jc)
        self.assertIn(["stop", "dnsmasq"], sc)
        self.assertIn(["start", "dnsmasq"], sc)
        self.assertIn(["stop", "prepper-meshbridge"], sc)
        self.assertIn(["restart", "prepper-portal", "kiwix-serve"], sc)
        self.assertIn(["try-restart", "prepper-panel"], sc)
        # receipt in RAM only, without secrets; no marker left on disk
        rc = self.receipt()
        self.assertEqual(rc["mode"], "wipe")
        self.assertTrue(rc["finished"])
        self.assertEqual((root / "run/prepper/wipe-receipt.json").stat().st_mode & 0o777, 0o600)
        blob = json.dumps(rc)
        self.assertNotIn(c["network"]["passphrase"], blob, "the new Wi-Fi password is never in the receipt")
        self.assertFalse((root / "var/lib/prepper/wipe-pending.json").exists())
        self.assertIn("NOT re-keyed", rc["radio"], "no bridge running: said plainly")
        bad = {k: v for k, v in rc["steps"].items() if v.startswith("error")}
        self.assertEqual(bad, {}, bad)

    def test_idempotent_and_finishes_quickly(self):
        self.populate()
        t = time.monotonic()
        self.wipe()
        first = time.monotonic() - t
        self.wipe()                                          # nothing left to do: still fine
        self.assertEqual(self.leftovers(), {})
        bad = {k: v for k, v in self.receipt()["steps"].items() if v.startswith("error")}
        self.assertEqual(bad, {})
        self.assertLess(first, 60, "a wipe takes seconds (here: incl. stubs and apply-config)")

    def test_missing_tools_and_files_dont_stop_it(self):
        # a bare root: no journalctl (127), no portal folder, no keys
        (self.stubs.bin / "journalctl").unlink()
        self.wipe()
        rc = self.receipt()
        self.assertTrue(rc["finished"])
        self.assertIn("NOT vacuumed", rc["steps"]["logs"])

    def test_wifi_off_and_keep(self):
        self.conf.write_text(wipe.set_key(self.conf.read_text(), "wipe", "wifi", '"off"'))
        self.populate()
        self.wipe()
        self.assertEqual(self.cfg()["network"]["passphrase"], "OldPassw0rd!", "wifi = off: not changed, switched off")
        self.assertIn(["stop", "prepper-hostapd"], self.stubs.argvs("systemctl"))
        self.assertIn("Wi-Fi off", self.receipt()["steps"]["apply"])
        self.assertEqual(set(self.leftovers()), {"OldPassw0rd!"})

    def test_configured_paths_outside_the_badger_folders_are_ignored(self):
        t = self.conf.read_text().replace("[portal]\n", '[portal]\nmembers_file = "/etc/passwd"\nfamily_board_file = "/etc/prepper/prepper.toml"\nmesh_pair_file = "/var/lib/prepper/../../../etc/passwd"\n', 1)
        self.conf.write_text(t)
        (self.root / "etc/passwd").write_text("root:x:0:0\n")
        self.populate()
        self.wipe()
        self.assertTrue((self.root / "etc/passwd").exists())
        self.assertTrue(self.conf.exists())
        tomllib.loads(self.conf.read_text())


class RunDirOverride(WipeCase):
    def test_portal_overrides_are_honoured(self):
        t = self.conf.read_text().replace("[portal]\n", '[portal]\nrun_dir = "/run/prepper/alt"\n'
                                          'mesh_nodes = "/var/lib/prepper/radio/nodes.json"\n', 1)
        self.conf.write_text(t)
        self.populate()
        (self.root / "run/prepper/alt").mkdir(parents=True)
        (self.root / "run/prepper/alt/mesh-circle.json").write_text("CIRCLEPSK")
        (self.root / "var/lib/prepper/radio").mkdir(parents=True)
        (self.root / "var/lib/prepper/radio/nodes.json").write_text("Hendersons")
        self.wipe()
        self.assertEqual(self.leftovers(), {})


class Tokens(WipeCase):
    def test_no_wipe_without_the_right_token(self):
        self.populate()
        r = self.ok("panic-wipe")
        self.assertIn("can't be undone", r.stdout)
        self.assertEqual(set(self.leftovers()), set(SECRETS), "asking for a token changes nothing")
        r = self.ctl("panic-wipe", "--confirm", "AAAA-BBBB")
        self.assertEqual(r.returncode, 2)
        self.assertIn("nothing was erased", r.stderr)
        self.assertEqual(set(self.leftovers()), set(SECRETS))

    def test_a_token_works_once_and_expires(self):
        self.populate()
        tok = self.token()
        self.ctl("panic-wipe", "--confirm", "WRONG-ONE1")           # a wrong try uses it up
        self.assertNotEqual(self.ctl("panic-wipe", "--confirm", tok).returncode, 0)
        tok = self.token()
        f = self.root / "run/prepper/.confirm-panic-wipe"
        d = json.loads(f.read_text())
        self.assertNotIn(tok, f.read_text(), "stored hashed")
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        d["until"] = time.time() - 1
        f.write_text(json.dumps(d))
        self.assertNotEqual(self.ctl("panic-wipe", "--confirm", tok).returncode, 0)
        self.assertEqual(set(self.leftovers()), set(SECRETS))

    def test_a_wipe_token_cannot_fire_a_handover(self):
        tok = self.token("panic-wipe")
        self.assertNotEqual(self.ctl("handover-reset", "--confirm", tok).returncode, 0)

    def test_portal_triggers_are_limited_and_detached(self):
        self.populate()
        for trig in ("panel", "panel-duress", "deadman", "cli"):
            tok = self.token(sudo_user="prepper")
            r = self.ctl("panic-wipe", "--confirm", tok, "--trigger", trig, sudo_user="prepper")
            self.assertNotEqual(r.returncode, 0, trig)
        self.assertNotEqual(self.ctl("panic-wipe", "--confirm", "X", "--trigger", "bogus").returncode, 0)
        for verb in ("wipe-finish", "wipe-resume", "wipe-status", "deadman-check"):
            r = self.ctl(verb, sudo_user="prepper")
            self.assertIn("not available to the portal", r.stderr, verb)
        self.stubs.clear()
        (self.root / "var/lib/prepper/portal/portal-settings.json").write_text('{"duress_action": "wipe"}')
        tok = self.token(sudo_user="prepper")
        r = self.ok("panic-wipe", "--confirm", tok, "--trigger", "duress", sudo_user="prepper")
        self.assertIn("started", r.stdout)
        runs = self.stubs.argvs("systemd-run")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][-5:-2], ["panic-wipe", "--confirm", runs[0][-3]])
        self.assertEqual(runs[0][-2:], ["--trigger", "duress"])
        self.assertEqual(set(self.leftovers()), set(SECRETS), "the portal's own call wipes nothing itself")
        self.assertTrue(self.root.joinpath("var/lib/prepper/portal/portal-settings.json").exists())
        # the transient unit (root, no SUDO_USER) carries its own one-time token
        self.ok(*runs[0][runs[0].index("panic-wipe"):])
        self.assertNotIn("Lucia Garcia", self.leftovers())

    def test_inline_when_systemd_run_fails(self):
        self.populate()
        tok = self.token(sudo_user="prepper")
        self.ok("panic-wipe", "--confirm", tok, sudo_user="prepper", STUB_RC_systemd_run="1")
        self.assertNotIn("Lucia Garcia", self.leftovers())


class Mesh(WipeCase):
    def bridge(self, answer: dict | None, delay: float = 0.3):
        """A fake mesh bridge: heartbeat fresh; answers the wipe request like the specified bridge."""
        (self.root / "run/prepper/mesh.ok").touch()
        req = self.root / "var/lib/prepper/portal/mesh-rotate.json"
        res = self.root / "var/lib/prepper/portal/mesh-rotate-result.json"
        seen = {}

        def loop():
            end = time.time() + 60
            while time.time() < end:
                if req.exists():
                    try:
                        d = json.loads(req.read_text())
                    except ValueError:
                        time.sleep(0.05)
                        continue
                    seen.update(d)
                    time.sleep(delay)
                    if answer is not None:
                        req.unlink(missing_ok=True)
                        res.write_text(json.dumps({"id": d["id"], **answer, "url": "https://meshtastic.org/e/#NEWKEY"}))
                        (self.root / "var/lib/prepper/portal/mesh-channel.json").write_text('{"url": "#NEWKEY"}')
                    return
                time.sleep(0.05)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return seen, t

    def test_bridge_rekeys_every_private_channel(self):
        self.populate()
        seen, t = self.bridge({"ok": True, "wipe": True, "channels": [1, 2]})
        self.wipe()
        t.join(5)
        self.assertTrue(seen.get("wipe"))
        self.assertEqual(seen.get("channels"), "private")
        self.assertFalse(seen.get("factory_reset"))
        self.assertIn("new random keys", self.receipt()["radio"])
        self.assertEqual(self.leftovers(), {})
        portal = self.root / "var/lib/prepper/portal"
        self.assertFalse((portal / "mesh-channel.json").exists(), "the NEW link isn't kept either")
        self.assertFalse((portal / "mesh-rotate-result.json").exists())

    def test_older_bridge_and_silent_bridge_are_reported(self):
        self.populate()
        seen, t = self.bridge({"ok": True})                  # today's bridge: family channel only
        self.wipe()
        t.join(5)
        self.assertIn("circle channel keeps its key", self.receipt()["radio"])
        self.conf.write_text(wipe.set_key(self.conf.read_text(), "wipe", "mesh_wait_s", "1"))
        seen, t = self.bridge(None)
        start = time.monotonic()
        self.wipe()
        self.assertIn("didn't answer", self.receipt()["radio"])
        self.assertLess(time.monotonic() - start, 30)
        self.assertFalse((self.root / "var/lib/prepper/portal/mesh-rotate.json").exists())

    def test_partial_rekey_is_reported(self):
        self.populate()
        seen, t = self.bridge({"ok": False, "wipe": True, "channels": [1], "error": "not re-keyed: 2: OSError"})
        self.wipe()
        t.join(5)
        r = self.receipt()["radio"]
        self.assertIn("channels [1] only", r)
        self.assertIn("unplug the radio", r)

    def test_factory_reset_option(self):
        self.conf.write_text(wipe.set_key(self.conf.read_text(), "wipe", "radio", '"factory-reset"'))
        seen, t = self.bridge({"ok": True, "wipe": True, "factory_reset": True})
        self.wipe()
        t.join(5)
        self.assertTrue(seen.get("factory_reset"))
        self.assertIn("factory-reset", self.receipt()["radio"])


class Duress(WipeCase):
    """duress_action = "wipe" (the owner's choice; the default "lock" is in tests/test_duress.py)."""

    def populate(self):
        super().populate()
        (self.root / "var/lib/prepper/portal/portal-settings.json").write_text('{"duress_action": "wipe"}')

    def test_duress_defers_what_a_person_would_notice(self):
        self.populate()
        self.wipe("duress")
        c = self.cfg()
        self.assertEqual(c["network"]["passphrase"], "OldPassw0rd!", "Wi-Fi unchanged for now")
        self.assertEqual(self.stubs.argvs("hostnamectl"), [], "no apply-config yet")
        self.assertNotIn(["restart", "prepper-portal", "kiwix-serve"], self.stubs.argvs("systemctl"))
        self.assertTrue((self.root / "etc/prepper/tls/server.key").exists(), "https keeps working for now")
        posture = self.root / "var/lib/prepper/portal/posture.json"
        self.assertEqual(json.loads(posture.read_text())["level"], "neighbourly", "no visible posture change yet")
        left = self.leftovers()
        self.assertEqual(set(left), {"OldPassw0rd!", "TLS-SERVER-KEY"}, left)
        m = json.loads((self.root / "var/lib/prepper/wipe-pending.json").read_text())
        self.assertEqual(m["stage"], "b")
        self.assertGreater(m["b_due"], time.time() + 500)
        runs = self.stubs.argvs("systemd-run")
        self.assertTrue(any(a[-1] == "wipe-finish" and "--on-active=600s" in " ".join(a) or a[-1] == "wipe-finish"
                            for a in runs), runs)
        # the delayed timer fires
        self.ok("wipe-finish")
        self.assertEqual(json.loads(posture.read_text())["level"], "lockdown")
        self.assertEqual(self.leftovers(), {})
        self.assertNotEqual(self.cfg()["network"]["passphrase"], "OldPassw0rd!")
        self.assertFalse((self.root / "var/lib/prepper/wipe-pending.json").exists())

    def test_reboot_before_the_delayed_part_reschedules_it(self):
        self.populate()
        self.wipe("panel-duress")
        self.stubs.clear()
        r = self.ok("wipe-resume")
        self.assertIn("scheduled again", r.stdout)
        self.assertTrue(any(a[-1] == "wipe-finish" for a in self.stubs.argvs("systemd-run")))
        m = self.root / "var/lib/prepper/wipe-pending.json"
        d = json.loads(m.read_text())
        d["b_due"] = time.time() - 10
        m.write_text(json.dumps(d))
        self.ok("wipe-resume")
        self.assertEqual(self.leftovers(), {})
        self.assertNotIn(["try-restart", "prepper-panel"], self.stubs.argvs("systemctl"),
                         "a panel-started wipe never restarts the panel (it refreshes itself)")

    def test_power_cut_during_phase_a_is_finished_at_boot(self):
        self.populate()
        (self.root / "var/lib/prepper/wipe-pending.json").write_text(
            json.dumps({"v": 1, "mode": "wipe", "trigger": "admin", "stage": "a", "b_due": None, "started": 1}))
        self.ok("wipe-resume")
        self.assertEqual(self.leftovers(), {})
        self.assertIn("resumed", " ".join(self.receipt()["notes"]))
        self.assertIn("no wipe waiting", self.ok("wipe-resume").stdout)


class DuressLock(WipeCase):
    """The default duress action through prepperctl (the front panel's path): nothing erased, nothing restarted."""

    def test_lock_changes_nothing_visible(self):
        self.populate()
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()
                  and "opt" not in p.relative_to(self.root).parts[:1]}
        self.stubs.clear()
        self.wipe("panel-duress")
        self.assertEqual(self.stubs.argvs("systemctl"), [], "no restart, no service stopped")
        self.assertEqual(self.stubs.argvs("nmcli") + self.stubs.argvs("hostnamectl") + self.stubs.argvs("journalctl"), [])
        self.assertEqual(set(self.leftovers()), set(SECRETS), "nothing erased")
        self.assertEqual(self.cfg()["network"]["passphrase"], "OldPassw0rd!")
        self.assertFalse((self.root / "var/lib/prepper/wipe-pending.json").exists())
        self.assertFalse((self.root / "run/prepper/wipe-receipt.json").exists())
        changed = [k for k, v in before.items() if not Path(k).exists() or Path(k).read_bytes() != v]
        self.assertTrue(all("/run/prepper/.confirm" in k or k.endswith("vault.json") for k in changed), changed)


class Handover(WipeCase):
    EXTRA_CONF = {"device": {"provisioning": "sale"}}

    def test_handover_restores_factory_defaults(self):
        self.populate()
        self.conf.write_text(self.conf.read_text().replace("[portal]\n", '[portal]\nduress_pin = "scrypt$x"\n', 1)
                             .replace('admin_pin = ""', 'admin_pin = "scrypt$16384$8$1$aa$' + "0" * 64 + '"'))
        self.wipe(verb="handover-reset", trigger="cli")
        self.assertEqual(self.leftovers(), {})
        c = self.cfg()
        self.assertRegex(c["network"]["ssid"], r"^Home-[A-Z2-9]{4}$")
        self.assertRegex(c["network"]["passphrase"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual(c["network"]["security"], "auto")
        self.assertEqual(c["portal"]["admin_pin"], "", "first-run: the new owner sets a PIN")
        self.assertNotIn("duress_pin", c["portal"])
        self.assertEqual(c["device"]["provisioning"], "sale")
        self.assertEqual(c["upd"]["peers"], [])
        self.assertNotIn("latitude", c.get("time", {}))
        rec = json.loads((self.root / "var/lib/prepper/portal/personal-content.json").read_text())
        self.assertIn("withdrawn_at", rec)
        self.assertNotIn("accepted", rec)
        self.assertNotIn("AAAAOTHERBADGER", (self.root / "etc/prepper/allowed_signers").read_text(),
                         "the old owner's Badgers are no longer trusted")
        self.assertTrue((self.root / "srv/prepper/zim/wikipedia_en.zim").exists())
        self.assertEqual(self.receipt()["mode"], "handover")
        self.assertEqual(json.loads((self.root / "var/lib/prepper/portal/posture.json").read_text())["level"], "guarded")
        self.assertFalse((self.root / "var/lib/prepper/portal/mesh-silent").exists())
        self.assertIn("identity", self.receipt()["steps"])
        self.assertTrue(self.stubs.argvs("ssh-keygen"), "a new signing identity is made")

    def test_handover_never_defers_and_asks_for_a_radio_reset(self):
        seen, t = Mesh.bridge(self, {"ok": True, "wipe": True, "factory_reset": True})
        self.wipe(verb="handover-reset", trigger="cli")
        t.join(5)
        self.assertTrue(seen.get("factory_reset"))
        self.assertFalse((self.root / "var/lib/prepper/wipe-pending.json").exists())
        self.assertTrue(self.receipt()["finished"])

    def test_owner_unit_with_sale_flag(self):
        self.conf.write_text(self.conf.read_text().replace('provisioning = "sale"', 'provisioning = "owner"'))
        self.populate()
        self.wipe(verb="handover-reset", extra=("--sale",))
        self.assertEqual(self.cfg()["device"]["provisioning"], "sale")
        self.assertNotEqual(self.ctl("panic-wipe", "--sale").returncode, 0)


class DuressPin(WipeCase):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(REPO / "software/portal"))
        import server
        self.server = server
        self.admin_hash = server.hash_pin("correct horse battery")
        self.conf.write_text(self.conf.read_text().replace('admin_pin = ""', f'admin_pin = "{self.admin_hash}"'))

    def test_set_and_clear_from_the_portal(self):
        h = self.server.hash_pin("purple tractor sings")
        self.ok("set-duress", "--hash", sudo_user="prepper", input=h + "\n")
        self.assertEqual(self.cfg()["portal"]["duress_pin"], h)
        self.assertEqual(self.cfg()["portal"]["admin_pin"], self.admin_hash)
        self.ok("set-duress", "--clear", sudo_user="prepper")
        self.assertNotIn("duress_pin", self.cfg()["portal"])
        for bad in ("nonsense", self.admin_hash, "scrypt$1$2$3$zz$" + "0" * 64 + "\n$x"):
            self.assertNotEqual(self.ctl("set-duress", "--hash", sudo_user="prepper", input=bad + "\n").returncode, 0, bad)
        self.assertNotEqual(self.ctl("set-duress", sudo_user="prepper", input="purple tractor sings\n").returncode, 0)

    def test_console_refuses_the_admin_pin(self):
        r = self.ctl("set-duress", input="correct horse battery\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must be different", r.stderr)
        self.ok("set-duress", input="purple tractor sings\n")
        self.assertTrue(self.server.verify_pin("purple tractor sings", self.cfg()["portal"]["duress_pin"]))

    def test_verify_pin_mirrors_the_portal(self):
        for pin, stored in (("abc123456", self.server.hash_pin("abc123456")),
                            ("abc123456", self.server.legacy_hash_pin("abc123456", "salt"))):
            self.assertTrue(wipe.verify_pin(pin, stored))
            self.assertFalse(wipe.verify_pin(pin + "x", stored))
        self.assertFalse(wipe.verify_pin("x", ""))
        self.assertFalse(wipe.verify_pin("x", "scrypt$99999999$8$1$a$b"))


class DeadMan(WipeCase):
    def uptime(self, s, boot="boot-1"):
        (self.root / "proc/uptime").write_text(f"{s} 0\n")
        (self.root / "proc/sys/kernel/random/boot_id").write_text(boot + "\n")

    def status(self):
        return json.loads(self.ok("deadman-status").stdout)

    def test_two_step_arming(self):
        self.populate()
        self.assertNotEqual(self.ctl("deadman-set", "1").returncode, 0, "at least 3 days")
        self.assertNotEqual(self.ctl("deadman-set", "--confirm", "ABCDEF").returncode, 0, "nothing waiting")
        r = self.ok("deadman-set", "3", sudo_user="prepper")
        code = re.search(r"confirm-code: ([A-Z0-9]{6})", r.stdout).group(1)
        self.assertFalse(self.status()["armed"], "not armed until confirmed")
        self.assertNotEqual(self.ctl("deadman-set", "--confirm", "ZZZZZZ").returncode, 0)
        self.assertNotEqual(self.ctl("deadman-set", "--confirm", code).returncode, 0, "a wrong try used it up")
        code = re.search(r"confirm-code: (\w+)", self.ok("deadman-set", "3").stdout).group(1)
        r = self.ok("deadman-set", "--confirm", code, sudo_user="prepper")
        self.assertIn("ARMED", r.stdout)
        self.assertEqual(self.cfg()["wipe"]["deadman_days"], 3)
        self.assertIn(["enable", "--now", "prepper-deadman.timer"], self.stubs.argvs("systemctl"))
        self.assertTrue(self.status()["armed"])
        self.ok("deadman-off", sudo_user="prepper")
        self.assertFalse(self.status()["armed"])
        self.assertEqual(self.cfg()["wipe"]["deadman_days"], 0)

    def arm(self, days=3):
        code = re.search(r"confirm-code: (\w+)", self.ok("deadman-set", str(days)).stdout).group(1)
        self.ok("deadman-set", "--confirm", code)

    def test_counts_only_running_time_and_check_ins_reset_it(self):
        self.populate()
        self.uptime(1000)
        self.arm(3)
        up = 1000.0
        for _ in range(30):                                  # 30 hourly runs → 30 h
            up += 3600
            self.uptime(up)
            self.ok("deadman-check")
        self.assertAlmostEqual(self.status()["idle_h"], 30, delta=0.5)
        (self.root / "proc/uptime").write_text(f"{up + 10 * 86400} 0\n")   # a giant jump counts 2 h at most
        self.ok("deadman-check")
        self.assertLess(self.status()["idle_h"], 33)
        self.uptime(50, boot="boot-2")                        # switched off for a week: nothing counted
        self.ok("deadman-check")
        self.assertLess(self.status()["idle_h"], 33)
        (self.root / "var/lib/prepper/portal/checkin.json").write_text('{"seq": 5}')   # someone checked in
        self.ok("deadman-check")
        self.assertEqual(self.status()["idle_h"], 0)
        self.assertIn("Lucia Garcia", self.leftovers(), "no wipe yet")

    def test_overdue_wipes(self):
        self.populate()
        self.uptime(1000)
        self.arm(3)
        up = 1000.0
        fired = False
        for _ in range(45):
            up += 7200
            self.uptime(up)
            r = self.ok("deadman-check")
            if "wipe:" in r.stdout:
                fired = True
                break
        self.assertTrue(fired)
        self.assertEqual(self.leftovers(), {})
        self.assertEqual(self.receipt()["trigger"], "deadman")
        self.assertIn('"armed": false', self.ok("deadman-status").stdout)

    def test_off_by_default(self):
        self.populate()
        self.assertIn('"result": "off"', self.ok("deadman-check").stdout)
        self.assertIn("Lucia Garcia", self.leftovers())


class Sealed(WipeCase):
    def test_sealed_destroy_keys_is_called_first(self):
        self.populate()
        mark = self.tmp / "sealed-called"
        (self.root / "opt/prepper/software/common/sealed.py").write_text(
            "import os\nKEY_PATHS = ['/var/lib/prepper/portal/custom-sealed.key']\n"
            f"def destroy_keys(root='/'):\n    open({str(mark)!r}, 'w').write(root)\n")
        (self.root / "var/lib/prepper/portal/custom-sealed.key").write_text("SEALEDWRAP-KEY")
        self.wipe()
        self.assertEqual(mark.read_text(), str(self.root))
        self.assertIn("destroy_keys() done", self.receipt()["steps"]["sealed-keys"])
        self.assertEqual(self.leftovers(), {})
        order = list(self.receipt()["steps"])
        self.assertEqual(order[:2], ["sealed-keys", "vault-keys"], "crypto-erase comes first")

    def test_a_broken_sealed_module_does_not_stop_the_wipe(self):
        self.populate()
        (self.root / "opt/prepper/software/common/sealed.py").write_text("raise RuntimeError('boom')\n")
        self.wipe()
        self.assertEqual(self.leftovers(), {})
        self.assertIn("failed to load", self.receipt()["steps"]["sealed-keys"])


class TextEdits(unittest.TestCase):
    def test_set_key(self):
        t = '[network]  # hi\nssid = "a"\nuplink_wifi = [\n  { ssid = "H]", psk = "p#q" },\n]\nhttps = true\n[upd]\npeers = ["x"]\n'
        t2 = wipe.set_key(t, "network", "uplink_wifi", "[]")
        self.assertEqual(tomllib.loads(t2)["network"], {"ssid": "a", "uplink_wifi": [], "https": True})
        t3 = wipe.set_key(t2, "network", "ssid", None)
        self.assertNotIn("ssid", tomllib.loads(t3)["network"])
        t4 = wipe.set_key(t3, "wipe", "deadman_days", "5")
        self.assertEqual(tomllib.loads(t4)["wipe"]["deadman_days"], 5)
        self.assertEqual(tomllib.loads(t4)["upd"]["peers"], ["x"])

    def test_strip_wifis(self):
        y = "network:\n  version: 2\n  wifis:\n    wlan0:\n      password: x\n\n  ethernets:\n    eth0: {}\n"
        out = wipe.strip_wifis(y)
        self.assertNotIn("password", out)
        self.assertIn("ethernets:", out)
        self.assertIn("eth0", out)


if __name__ == "__main__":
    unittest.main()
