"""
Adversarial tests for key rotation: the mesh bridge's family-channel rotation (handle_rotation,
rotate_family_key, save_channel_link), prepperctl wifi-rotate/confirm/revert/status/apply, and the
front panel's invite / Wi-Fi screens.

Failing tests are intentional: each one marked `# BUG:` documents a defect (expected vs actual).
Run: python3.11 tests/test_adv_rotation.py
"""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import tomllib
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "software" / "meshbridge"))
sys.path.insert(0, str(REPO / "software" / "panel"))
os.environ["PORTAL_QUIET"] = "1"
import meshbridge as mb  # noqa: E402
import panel  # noqa: E402
from os_helpers import PY, toml_value  # noqa: E402
from test_prepperctl import Ctl  # noqa: E402


# ───────────────────────────── mesh family-key rotation ─────────────────────────────

class FakeChannel:
    def __init__(self, role):
        self.role = role
        self.settings = types.SimpleNamespace(psk=b"old-key")


class FakeNode:
    def __init__(self, roles=(1, 2, 0), url_fails=0, write_fails=False):
        self.channels = [FakeChannel(r) for r in roles]
        self.written, self.url_fails, self.write_fails = [], url_fails, write_fails

    def writeChannel(self, i):
        if self.write_fails:
            raise OSError("serial timeout")
        self.written.append(i)

    def getURL(self, includeAll=True):
        if self.url_fails:
            self.url_fails -= 1
            raise RuntimeError("radio busy")
        return "https://meshtastic.org/e/#" + self.channels[1].settings.psk.hex()


def iface(node):
    return types.SimpleNamespace(localNode=node)


class MeshRotation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adv-rot-")).resolve()
        self.req, self.res, self.link = self.tmp / "req.json", self.tmp / "res.json", self.tmp / "link.json"
        self.old_umask = os.umask(0o022)                       # what a systemd service gets by default

    def tearDown(self):
        os.umask(self.old_umask)
        for p in self.tmp.rglob("*"):
            with contextlib.suppress(OSError):
                p.chmod(0o700)
        with contextlib.suppress(OSError):
            self.tmp.chmod(0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rotate(self, node, ch=1):
        with contextlib.redirect_stderr(io.StringIO()):
            return mb.handle_rotation(iface(node), ch, self.req, self.res, self.link)

    def test_result_file_holding_the_key_is_owner_only(self):
        # BUG (low): the rotation result {"url": "https://meshtastic.org/e/#<all channel keys>"} is written
        # by atomic_json() with the process umask (0644), while the very same link in mesh-channel.json is
        # carefully created 0600 ("holds the key"). Mitigated today by the 0750 portal directory, but a copy
        # / backup of the folder or a looser directory mode exposes the family channel key.
        # Expected: 0600. Actual: 0644. Source: meshbridge.py handle_rotation → atomic_json(res_path, …).
        self.req.write_text(json.dumps({"id": "r1"}))
        out = self.rotate(FakeNode())
        self.assertTrue(out["ok"])
        self.assertIn("#", json.loads(self.res.read_text())["url"])
        self.assertEqual(stat.S_IMODE(self.res.stat().st_mode) & 0o077, 0, oct(self.res.stat().st_mode))

    def test_new_key_link_is_not_lost_when_getURL_fails_after_the_write(self):
        # BUG: rotate_family_key() writes the new key to the radio (writeChannel) and only then asks for
        # the link (getURL). If getURL raises (radio busy / older library), handle_rotation reports failure
        # and never calls save_channel_link: the radio now has a key nobody knows, the admin is told it
        # failed, and "Show family radio link" still returns the OLD link. The module promises "the link is
        # never lost". Expected: once writeChannel succeeded, the link file ends up with the new key (retry
        # getURL / save it later) or the result says the key DID change. Actual: link file never written,
        # result {"ok": false}. Source: meshbridge.py rotate_family_key/handle_rotation.
        node = FakeNode(url_fails=1)
        self.req.write_text(json.dumps({"id": "r2"}))
        out = self.rotate(node)
        self.assertEqual(node.written, [1])                     # the key really changed on the radio
        new_url = node.getURL()
        saved = json.loads(self.link.read_text())["url"] if self.link.exists() else None
        self.assertTrue(saved == new_url or (out.get("url") == new_url), (out, saved))

    def test_request_is_acted_on_once_even_if_it_cannot_be_deleted(self):
        # BUG: handle_rotation() ignores a failed unlink of the request file and rotates anyway; main() calls
        # it every 5 s, so a request that can't be removed (read-only dir/file system, wrong owner after a
        # restore) re-keys the family channel every 5 seconds, cutting every family radio off for good.
        # Expected: a request that can't be consumed is not acted on again (remember the id / refuse).
        # Actual: rotates on every call. Source: meshbridge.py handle_rotation (unlink OSError swallowed).
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        rd = self.tmp / "ro"
        rd.mkdir()
        self.req = rd / "req.json"
        self.req.write_text(json.dumps({"id": "stuck"}))
        rd.chmod(0o500)
        node = FakeNode()
        keys = []
        for _ in range(3):
            self.rotate(node)
            keys.append(node.channels[1].settings.psk)
        self.assertEqual(len(node.written), 1, f"rotated {len(node.written)} times")

    def test_corrupt_huge_and_odd_requests_do_nothing(self):
        node = FakeNode()
        for blob in (b"", b"{", b"[]", b"null", b'"id"', b'{"id": ""}', b'{"id": 0}', b'{"id": null}',
                     b'{"id": false}', b'{"id": []}', b"\xff\xfe\x00", b"{" * 100000, b" " * 5_000_000):
            with self.subTest(blob=blob[:12]):
                self.req.write_bytes(blob)
                self.assertIsNone(self.rotate(node))
        self.assertEqual(node.written, [])
        self.assertFalse(self.res.exists())

    def test_odd_but_truthy_ids_are_echoed_safely(self):
        node = FakeNode()
        for rid in (123, ["x"], {"a": 1}, "x" * 500, "<script>"):
            self.req.write_text(json.dumps({"id": rid}))
            out = self.rotate(node)
            self.assertLessEqual(len(out["id"]), 32)
            self.assertEqual(json.loads(self.res.read_text())["id"], out["id"])

    def test_refuses_missing_disabled_and_out_of_range_channels(self):
        for roles, ch in (((1, 2, 0), -1), ((1, 2, 0), 2), ((1, 2, 0), 3), ((1, 2, 0), 99), ((), 0), ((1, None), 1),
                          ((1, "0"), 1)):
            node = FakeNode(roles)
            with self.subTest(roles=roles, ch=ch):
                self.req.write_text(json.dumps({"id": "x"}))
                out = self.rotate(node, ch)
                self.assertFalse(out["ok"])
                self.assertEqual(node.written, [])
        node = FakeNode()
        node.channels = None
        self.req.write_text(json.dumps({"id": "y"}))
        self.assertFalse(self.rotate(node)["ok"])

    def test_write_failure_is_reported_and_link_untouched(self):
        self.link.write_text(json.dumps({"url": "OLD", "at": 1}))
        self.req.write_text(json.dumps({"id": "w"}))
        out = self.rotate(FakeNode(write_fails=True))
        self.assertFalse(out["ok"])
        self.assertIn("serial timeout", out["error"])
        self.assertEqual(json.loads(self.link.read_text())["url"], "OLD")

    def test_each_request_gets_a_fresh_key(self):
        node, urls = FakeNode(), []
        for i in range(5):
            self.req.write_text(json.dumps({"id": f"r{i}"}))
            urls.append(self.rotate(node)["url"])
        self.assertEqual(len(set(urls)), 5)
        self.assertEqual(json.loads(self.res.read_text())["id"], "r4")
        self.assertEqual(json.loads(self.link.read_text())["url"], urls[-1])

    def test_racing_requests_never_crash_and_rotate_at_most_once_each(self):
        node, stop, errors = FakeNode(), threading.Event(), []

        def writer():
            for i in range(60):
                tmp = self.tmp / f".req.{i}"
                tmp.write_text(json.dumps({"id": f"w{i}"}))
                os.replace(tmp, self.req)
                time.sleep(0.001)
            stop.set()
        t = threading.Thread(target=writer)
        t.start()
        while not stop.is_set():
            try:
                self.rotate(node)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        t.join()
        self.rotate(node)
        self.assertEqual(errors, [])
        self.assertLessEqual(len(node.written), 60)

    def test_link_file_ends_up_0600_whatever_was_there(self):
        self.link.write_text("{}")
        self.link.chmod(0o644)
        mb.save_channel_link(iface(FakeNode()), self.link)
        self.assertEqual(stat.S_IMODE(self.link.stat().st_mode), 0o600)
        victim = self.tmp / "victim"
        victim.write_text("keep")
        self.link.unlink()
        self.link.symlink_to(victim)
        mb.save_channel_link(iface(FakeNode()), self.link)
        self.assertEqual(victim.read_text(), "keep")             # the symlink is replaced, not followed
        self.assertFalse(self.link.is_symlink())
        self.assertEqual(stat.S_IMODE(self.link.stat().st_mode), 0o600)

    def test_link_temp_file_already_present_with_0644(self):
        # BUG (low): save_channel_link() opens its temp file ".<name>.<pid>" with O_CREAT|O_TRUNC but not
        # O_EXCL; the 0600 mode only applies when the file is created. A leftover/pre-created temp file with
        # 0644 keeps 0644 and is renamed over mesh-channel.json → the channel key is group/world-readable.
        # Expected: 0600. Actual: 0644. Source: meshbridge.py save_channel_link (os.open flags).
        tmp = self.link.with_name(f".{self.link.name}.{os.getpid()}")
        tmp.write_text("stale")
        tmp.chmod(0o644)
        mb.save_channel_link(iface(FakeNode()), self.link)
        self.assertEqual(stat.S_IMODE(self.link.stat().st_mode), 0o600)

    def test_link_temp_file_symlink_is_not_followed(self):
        # BUG (low): same open() follows a symlink planted at ".<name>.<pid>": the channel key is written
        # into the symlink's target (any file the portal user can write) before the rename.
        # Expected: target untouched (O_EXCL|O_NOFOLLOW / mkstemp). Actual: target overwritten with the key.
        # Source: meshbridge.py save_channel_link; atomic_json/save_offset have the same pattern.
        victim = self.tmp / "victim"
        victim.write_text("keep")
        self.link.with_name(f".{self.link.name}.{os.getpid()}").symlink_to(victim)
        mb.save_channel_link(iface(FakeNode()), self.link)
        self.assertEqual(victim.read_text(), "keep")


# ───────────────────────────── prepperctl wifi-* ─────────────────────────────

SYSTEMD_RUN_STUB = r'''#!{py}
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps(["systemd-run", sys.argv[1:], None]) + "\n")
m = os.environ.get("FAIL_UNIT_MATCH", "")
sys.exit(1 if m and any(m in a for a in sys.argv[1:]) else 0)
'''

OLD = "oldfamilypass"


class WifiRotation(Ctl):
    def setUp(self):
        super().setUp()
        self.write_conf(self.conf.read_text().replace('passphrase = ""', f'passphrase = "{OLD}"'))
        self.pending = self.root / "var/lib/prepper/wifi-rotate-pending.json"

    def pw(self):
        return tomllib.loads(self.conf.read_text())["network"]["passphrase"]

    def set_pw(self, pw):
        t = re.sub(r'(?m)^passphrase = .*$', lambda _: f"passphrase = {toml_value(pw)}", self.conf.read_text(), count=1)
        self.write_conf(t)
        self.assertEqual(self.pw(), pw)

    def new_pw(self, r):
        return [ln.split(": ", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("passphrase: ")][0]

    def test_no_automatic_undo_means_no_rotation(self):
        # BUG: if the revert timer can't be scheduled, wifi-rotate only prints a warning and exits 0 with
        # the new password saved and switching in 10 s. The failsafe that makes rotation safe ("rejoin and
        # confirm within 10 minutes, or it switches back by itself") is gone, and the portal drops the
        # warning (see test_adv_boards_http). If the admin then loses the new password, every device is
        # locked out of the hotspot. Expected: abort like the first timer does — old password kept, no
        # pending state, non-zero exit. Actual: rc 0, new password, pending file, no undo scheduled.
        # Source: os/bin/prepperctl wifi-rotate (`|| echo "warning: the automatic undo could not be scheduled…"`).
        stub = self.stubs.bin / "systemd-run"
        stub.write_text(SYSTEMD_RUN_STUB.format(py=PY))
        r = self.ctl("wifi-rotate", sudo_user="prepper", FAIL_UNIT_MATCH="--unit=prepper-wifi-revert")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.pw(), OLD)
        self.assertFalse(self.pending.exists())

    def test_switch_cannot_be_scheduled_so_nothing_changes(self):
        r = self.ctl("wifi-rotate", sudo_user="prepper", STUB_RC_systemd_run="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing changed", r.stderr)
        self.assertEqual(self.pw(), OLD)
        self.assertFalse(self.pending.exists())

    def test_failed_config_write_leaves_no_pending_state(self):
        # BUG: wifi-rotate writes wifi-rotate-pending.json BEFORE set_passphrase. set_passphrase only finds a
        # "[network]" header that is alone on its line; with a perfectly valid "[network]   # hotspot" it
        # appends a second [network] table, conf_write refuses the unparseable result and the script dies —
        # leaving the pending file behind with no timers. From then on wifi-rotate refuses ("a password
        # change is waiting"), wifi-status reports a pending change, and wifi-revert fails the same way.
        # Expected: on any failure, no pending file (or the rotation works). Actual: stuck pending state.
        # Source: os/bin/prepperctl wifi-rotate (order of pending write vs set_passphrase) and
        # set_passphrase (header regex `^\[network\][ \t]*\n`).
        self.write_conf(self.conf.read_text().replace("[network]\n", "[network]   # the hotspot\n", 1))
        tomllib.loads(self.conf.read_text())                     # still valid TOML
        r = self.ctl("wifi-rotate", sudo_user="prepper")
        if r.returncode == 0:
            self.assertNotEqual(self.pw(), OLD)
        else:
            self.assertFalse(self.pending.exists(), "pending file left behind after a failed rotation: " + r.stderr)
            self.assertEqual(self.pw(), OLD)

    def test_old_password_with_special_characters_round_trips(self):
        for old in ['pa"ss\\word$HOME`id`', "  lead and trail  ", "semi;colon#hash='q'", "x" * 63, "$(reboot)!!",
                    "-starts-with-dash", "ends-with-backslash\\"]:
            with self.subTest(old=old):
                self.set_pw(old)
                self.ok("wifi-rotate", sudo_user="prepper")
                self.assertNotEqual(self.pw(), old)
                self.ok("wifi-revert", sudo_user="prepper")
                self.assertEqual(self.pw(), old)
                self.assertFalse(self.pending.exists())
        self.assertEqual([c for c in self.stubs.calls() if "reboot" in c[1]], [])

    def test_saved_password_cannot_inject_config_lines(self):
        self.ok("wifi-rotate")
        self.pending.write_text(json.dumps({"old": "goodpass1\nssid=evil\nwpa=0\n[portal]\nadmin_pin = \"x\"",
                                            "deadline": time.time() + 60}))
        self.ctl("wifi-revert")
        conf = tomllib.loads(self.conf.read_text())             # still one valid config
        self.assertNotEqual(conf.get("portal", {}).get("admin_pin"), "x")
        self.assertNotIn("\n", conf["network"].get("ssid", ""))
        for p in self.root.rglob("*"):
            if p.is_file() and "opt" not in p.relative_to(self.root).parts[:1]:
                txt = p.read_bytes()
                self.assertNotRegex(txt, rb"(?m)^ssid=evil$", str(p))
                self.assertNotRegex(txt, rb"(?m)^wpa=0$", str(p))

    def test_pending_without_old_password_does_not_open_the_network(self):
        # BUG (low): wifi-revert reads the saved password with .get("old", ""); a damaged/foreign pending
        # file without "old" (e.g. {"deadline": …}) turns the hotspot into an OPEN network.
        # Expected: refuse ("cannot read the saved password"). Actual: passphrase = "" (open Wi-Fi).
        # Source: os/bin/prepperctl wifi-revert.
        self.ok("wifi-rotate")
        self.pending.write_text(json.dumps({"deadline": time.time() + 60}))
        self.ctl("wifi-revert")
        self.assertNotEqual(self.pw(), "")

    def test_corrupt_pending_file(self):
        # BUG (low): wifi-status decides "pending" from the *content* (`bool(json.load(...))`) while every other
        # verb uses the file's existence. A pending file that parses to an empty/falsy value ("[]", "{}",
        # "0") makes wifi-status print {"pending": false} while wifi-rotate refuses because a change "is
        # waiting" — the admin page shows no keep/undo box and the admin can't rotate again.
        # Expected: pending true (file exists) or an error. Actual: {"pending": false, "seconds_left": 0}.
        # Source: os/bin/prepperctl wifi-status.
        self.ok("wifi-rotate")
        new = self.pw()
        for blob in ("{not json", "[]", '{"old": 5, "deadline": "soon"}', ""):
            with self.subTest(blob=blob):
                self.pending.write_text(blob)
                r = self.ctl("wifi-status", sudo_user="prepper")
                if r.returncode == 0:                            # if it answers, it must not say "nothing pending"
                    self.assertTrue(json.loads(r.stdout.strip().splitlines()[-1])["pending"], r.stdout)
                self.fails("wifi-rotate", msg="waiting to be confirmed")
        self.pending.write_text("{not json")
        self.fails("wifi-revert", msg="cannot read")
        self.assertEqual(self.pw(), new)
        self.assertTrue(self.pending.exists())

    def test_pending_dangling_symlink_is_replaced_not_followed(self):
        target = self.tmp / "elsewhere" / "planted.json"
        target.parent.mkdir()
        self.pending.symlink_to(target)
        self.ok("wifi-rotate")
        self.assertFalse(target.exists())
        self.assertFalse(self.pending.is_symlink())
        self.assertEqual(stat.S_IMODE(self.pending.stat().st_mode), 0o600)

    def test_portal_user_may_run_every_wifi_verb(self):
        for verb in ("wifi-status", "wifi-rotate", "wifi-status", "wifi-confirm", "wifi-rotate", "wifi-revert",
                     "wifi-status", "wifi-apply"):
            with self.subTest(verb=verb):
                self.ok(verb, sudo_user="prepper")
        self.assertEqual(self.pw(), self.pw())

    def test_confirm_and_revert_are_idempotent(self):
        self.ok("wifi-rotate")
        self.ok("wifi-revert")
        self.assertEqual(self.pw(), OLD)
        self.assertIn("no Wi-Fi password change", self.ok("wifi-revert").stdout)
        self.assertIn("no Wi-Fi password change", self.ok("wifi-confirm").stdout)
        r = self.ok("wifi-rotate")
        self.ok("wifi-confirm")
        self.ok("wifi-confirm")
        self.ok("wifi-revert")
        self.assertEqual(self.pw(), self.new_pw(r))

    def test_revert_keeps_hand_edits_made_meanwhile(self):
        self.ok("wifi-rotate")
        t = self.conf.read_text().replace('ssid = "HoneyBadger"', 'ssid = "Edited"', 1)
        self.write_conf(t + '\n[extra]\nnote = "hand edit"\n')
        self.ok("wifi-revert")
        conf = tomllib.loads(self.conf.read_text())
        self.assertEqual(conf["network"]["passphrase"], OLD)
        self.assertEqual(conf["extra"]["note"], "hand edit")
        if 'ssid = "Edited"' in t:
            self.assertEqual(conf["network"]["ssid"], "Edited")
        self.assertEqual(stat.S_IMODE(self.conf.stat().st_mode), 0o640)

    def test_rotate_without_a_network_section(self):
        t = re.sub(r"(?ms)^\[network\]\n.*?(?=^\[)", "", self.conf.read_text(), count=1)
        self.write_conf(t)
        before = tomllib.loads(t)
        self.assertNotIn("network", before)
        r = self.ok("wifi-rotate")
        after = tomllib.loads(self.conf.read_text())
        self.assertEqual(after["network"]["passphrase"], self.new_pw(r))
        for k in before:
            self.assertEqual(before[k], after[k], k)
        self.assertEqual(json.loads(self.pending.read_text())["old"], "")


# ───────────────────────────── front panel ─────────────────────────────

class FakeDisplay:
    def __init__(self):
        self.qrs, self.texts = [], []

    def lines(self, rows, title=None, sel=None):
        self.texts.append((list(rows), title))

    def qr(self, data, caption):
        self.qrs.append((data, caption))

    def sleep(self, on):
        pass


class Panel(unittest.TestCase):
    def setUp(self):
        self.old_api, self.old_post = panel.API, panel.api_post
        panel.API = "http://127.0.0.1:9"
        os.environ["PANEL_TIMEFMT"] = str(Path(tempfile.mkdtemp(prefix="adv-panel-")) / "fmt")

    def tearDown(self):
        panel.API, panel.api_post = self.old_api, self.old_post

    def make(self, psk="", ssid="HoneyBadger"):
        self.d = FakeDisplay()
        return panel.Panel(self.d, {"network": {"ssid": ssid, "passphrase": psk, "hostname": "badger"}})

    def test_wifi_caption_shows_the_whole_password(self):
        # BUG (low): the "Join Wi-Fi" caption prints at most 36 characters of the passphrase (4 lines of 9),
        # yet WPA allows 63, and the code comment says this screen "is the way back in if a new password was
        # lost". A 40-character admin-set password is shown truncated. Expected: every character visible
        # (more lines / smaller font / scrolling). Actual: last characters missing.
        # Source: software/panel/panel.py Panel.root ("Join Wi-Fi (QR)" caption, min(len(psk), 36)).
        psk = "correct-horse-battery-staple-and-more-40"
        p = self.make(psk)
        dict(p.root())["Join Wi-Fi (QR)"]()
        data, caption = self.d.qrs[-1]
        self.assertIn(psk, caption.replace("\n", ""))

    def test_rotated_password_fits_the_caption_and_qr(self):
        p = self.make("ABCD-EFGH-JKMN")
        dict(p.root())["Join Wi-Fi (QR)"]()
        data, caption = self.d.qrs[-1]
        self.assertIn("ABCD-EFGH-JKMN", caption.replace("\n", ""))
        self.assertIn("P:ABCD-EFGH-JKMN;", data)

    def test_wifi_qr_escapes_special_characters(self):
        p = self.make('a;b,c"d:e\\f gh', ssid='Net;"x":\\')
        q = p.wifi_qr()
        self.assertTrue(q.startswith("WIFI:T:WPA;S:"))
        self.assertTrue(q.endswith(";;"))
        fields, cur, esc = [], "", False                       # parse like a phone would
        for c in q[len("WIFI:"):]:
            if esc:
                cur += c; esc = False
            elif c == "\\":
                esc = True
            elif c == ";":
                fields.append(cur); cur = ""
            else:
                cur += c
        kv = dict(f.split(":", 1) for f in fields if f)
        self.assertEqual((kv["S"], kv["P"]), ('Net;"x":\\', 'a;b,c"d:e\\f gh'))
        self.assertEqual(self.make("").wifi_qr(), "WIFI:T:nopass;S:HoneyBadger;;")

    def test_invite_qr_happy_path_and_errors(self):
        p = self.make()
        panel.api_post = lambda path, body=None: {"ok": True, "code": "K7QX-M2PD-9Z", "expires": 1}
        p.invite_qr()
        data, caption = self.d.qrs[-1]
        self.assertEqual(data, "http://badger.lan/#/join/K7QX-M2PD-9Z")
        self.assertIn("K7QX", caption)
        for r in ({"ok": False, "error": "too many open invites: wait for some to expire"}, {"error": "HTTP 403"}, {}):
            panel.api_post = lambda path, body=None, r=r: r
            p.invite_qr()
            self.assertEqual(p.screen, "msg")
            self.assertTrue(all(len(x) <= 60 for x in self.d.texts[-1][0]))

    def test_invite_qr_survives_odd_replies(self):
        # BUG (low): invite_qr() does r.get("code") and code[:5] without type checks: a JSON reply that is
        # a list/str/number (proxy error page, future API change) or a non-string code crashes the panel's
        # input handler. Expected: an error message on screen. Actual: AttributeError/TypeError.
        # Source: software/panel/panel.py Panel.invite_qr / api_post (returns whatever JSON it got).
        p = self.make()
        problems = []
        for r in ([], "error", 5, {"code": 12345}, {"code": ["K7QX"]}):
            panel.api_post = lambda path, body=None, r=r: r
            try:
                p.invite_qr()
            except Exception as e:  # noqa: BLE001
                problems.append(f"{r!r}: {type(e).__name__}")
        self.assertEqual(problems, [])

    def test_api_post_against_a_real_http_server(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body, code = {"/a": (b'{"error": "only from the Badger\'s own screen"}', 403),
                              "/b": (b"<html>bad gateway</html>", 502),
                              "/c": (b'{"ok": true, "code": "AAAA-BBBB-CC"}', 200)}[self.path]
                self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)
                H.seen.append(dict(self.headers))
        H.seen = []
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            panel.API = f"http://127.0.0.1:{srv.server_address[1]}"
            self.assertIn("own screen", self.old_post("/a")["error"])
            self.assertEqual(self.old_post("/b"), {"error": "HTTP 502"})
            self.assertEqual(self.old_post("/c")["code"], "AAAA-BBBB-CC")
            self.assertEqual(H.seen[-1].get("Content-Type"), "application/json")
            self.assertNotIn("X-Real-IP", H.seen[-1])           # the panel must look like the local console
            panel.API = "http://127.0.0.1:9"
            self.assertEqual(self.old_post("/x"), {"error": "launcher not answering"})
        finally:
            srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
