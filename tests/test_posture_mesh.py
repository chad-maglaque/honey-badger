"""
The security posture in the mesh bridge: PostureWatch, radio silence from Lockdown, the radio muted in
Lockdown (CLIENT_MUTE, broadcasts off) and restored after, budgets/commands/alerts/position following the
posture without a restart, RAM-only mesh state, and the panic wipe's radio re-key — with a fake radio
through the real meshbridge.main().
Run: python3.11 tests/test_posture_mesh.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "meshbridge"))
sys.path.insert(0, str(REPO / "software" / "common"))
import meshbridge as mb  # noqa: E402
import posture as P  # noqa: E402


def ns(**kw):
    return types.SimpleNamespace(**kw)


class FakeChannel:
    def __init__(self, role, psk=b"", name=""):
        self.role = role
        self.settings = ns(psk=psk, name=name, uplink_enabled=True, downlink_enabled=True,
                           module_settings=ns(position_precision=32))


class FakeNode:
    def __init__(self, supported=True, write_fails=False):
        self.channels = [FakeChannel(1, b"\x01"), FakeChannel(2, b"F" * 32, "fam"), FakeChannel(0), FakeChannel(0),
                         FakeChannel(2, b"\x01", "LongSlow"), FakeChannel(0), FakeChannel(0), FakeChannel(0)]
        self.nodeNum = 0x0badfeed
        self.written, self.chan_writes, self.tx = [], [], []
        self.write_fails = write_fails
        self.owner = None
        if supported:
            self.localConfig = ns(device=ns(role=0, node_info_broadcast_secs=10800),
                                  position=ns(position_broadcast_secs=900, position_broadcast_smart_enabled=True),
                                  bluetooth=ns(enabled=False, mode=0, fixed_pin=0))
            self.moduleConfig = ns(telemetry=ns(device_update_interval=1800, environment_update_interval=0,
                                                power_update_interval=0, air_quality_interval=0),
                                   neighbor_info=ns(enabled=True))

    def writeConfig(self, name):
        if self.write_fails:
            raise OSError("radio busy")
        self.written.append(name)

    def beginSettingsTransaction(self):
        self.tx.append("begin")

    def commitSettingsTransaction(self):
        self.tx.append("commit")

    def writeChannel(self, i):
        self.chan_writes.append(i)

    def getURL(self, includeAll=True):
        return "https://meshtastic.org/e/#" + self.channels[1].settings.psk.hex()[:16]

    def setOwner(self, long_name=None, short_name=None):
        self.owner = (long_name, short_name)


class LockdownRadio(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pm-radio-"))
        self.saved = self.tmp / "mesh-radio-saved.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mute_save_restore_idempotent(self):
        node = FakeNode()
        iface = ns(localNode=node)
        r = mb.apply_lockdown_radio(iface, self.saved)
        self.assertIsNone(r["error"])
        self.assertEqual(node.localConfig.device.role, mb.ROLE_CLIENT_MUTE)
        self.assertEqual(node.localConfig.device.node_info_broadcast_secs, mb.NEVER)
        self.assertEqual(node.localConfig.position.position_broadcast_secs, mb.NEVER)
        self.assertFalse(node.localConfig.position.position_broadcast_smart_enabled)
        self.assertEqual(node.moduleConfig.telemetry.device_update_interval, mb.NEVER)
        self.assertFalse(node.moduleConfig.neighbor_info.enabled)
        self.assertEqual(set(node.written), {"device", "position", "telemetry", "neighbor_info"})
        self.assertEqual(node.tx, ["begin", "commit"])               # one transaction: one reboot
        old = json.loads(self.saved.read_text())["old"]
        self.assertEqual(old["device.role"], 0)
        self.assertEqual(old["telemetry.device_update_interval"], 1800)
        self.assertEqual(oct(self.saved.stat().st_mode & 0o777), "0o640")
        # again (after the radio rebooted and the bridge restarted): nothing written, saved values kept
        node.written.clear()
        r = mb.apply_lockdown_radio(iface, self.saved)
        self.assertEqual((node.written, r["applied"]), ([], []))
        self.assertEqual(json.loads(self.saved.read_text())["old"]["device.role"], 0)
        # leaving Lockdown
        r = mb.restore_radio(iface, self.saved)
        self.assertIsNone(r["error"])
        self.assertEqual((node.localConfig.device.role, node.localConfig.device.node_info_broadcast_secs), (0, 10800))
        self.assertTrue(node.moduleConfig.neighbor_info.enabled)
        self.assertFalse(self.saved.exists())
        self.assertEqual(mb.restore_radio(iface, self.saved)["note"], "nothing to restore")

    def test_unsupported_radio_never_crashes(self):
        iface = ns(localNode=FakeNode(supported=False))
        r = mb.apply_lockdown_radio(iface, self.saved)
        self.assertIsNone(r["error"])
        self.assertEqual(len(r["skipped"]), len(mb.LOCKDOWN_RADIO))
        self.assertIsNotNone(mb.apply_lockdown_radio(ns(), self.saved)["error"])      # no localNode at all

    def test_write_failure_is_reported_and_saved_first(self):
        node = FakeNode(write_fails=True)
        r = mb.apply_lockdown_radio(ns(localNode=node), self.saved)
        self.assertIn("radio busy", r["error"])
        self.assertTrue(self.saved.exists())                          # the old values survive for the restore
        node.write_fails = False
        node.localConfig.device.role = 0                              # the write never reached the radio
        self.assertEqual(mb.apply_lockdown_radio(ns(localNode=node), self.saved)["error"], None)
        self.assertEqual(json.loads(self.saved.read_text())["old"]["device.role"], 0)

    def test_damaged_saved_file_restores_only_known_fields(self):
        node = FakeNode()
        self.saved.write_text(json.dumps({"old": {"device.role": 0, "lora.region": 3, "bluetooth.fixed_pin": 1,
                                                  "position.position_broadcast_secs": "x" * 9, "device.x": 1}}))
        node.localConfig.device.role = 1
        mb.restore_radio(ns(localNode=node), self.saved)
        self.assertEqual(node.localConfig.device.role, 0)
        self.assertEqual(node.localConfig.bluetooth.fixed_pin, 0)
        self.assertEqual(node.localConfig.position.position_broadcast_secs, 900)
        self.saved.write_text("garbage")
        self.assertEqual(mb.restore_radio(ns(localNode=node), self.saved)["note"], "nothing to restore")


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pm-watch-"))
        self.now = [1000.0]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_follows_file_and_settings(self):
        pf, sf = self.tmp / "posture.json", self.tmp / "settings.json"
        w = mb.PostureWatch({"radio": {}}, pf, sf, clock=lambda: self.now[0], every=2)
        self.assertEqual((w.level, w.commands()["!q"], w.silence()), ("guarded", "community", (False, True)))
        P.write(pf, "lockdown", "t")
        self.assertFalse(w.refresh())                                 # too soon: cached
        self.now[0] += 3
        self.assertTrue(w.refresh())
        self.assertEqual((w.level, w.commands()["!q"], w.silence(), w.sharing()), ("lockdown", "family", (True, True), False))
        P.write(pf, "guarded", "t")
        sf.write_text(json.dumps({"mesh_commands": {"q": "ignore"}}))
        self.now[0] += 3
        self.assertTrue(w.refresh())
        self.assertEqual(w.commands()["!q"], "ignore")
        self.now[0] += 3
        self.assertFalse(w.refresh())                                 # nothing changed

    def test_silence_follows_posture(self):
        state = {"v": (False, True)}
        s = mb.Silence(None, posture=lambda: state["v"])
        self.assertFalse(s.active())
        state["v"] = (True, False)
        self.assertTrue(s.active())
        self.assertFalse(s.allows({"kind": "family", "member": True}))
        state["v"] = (True, True)
        self.assertTrue(s.allows({"kind": "family", "member": True}))
        self.assertFalse(s.allows({"kind": "alert"}))

        def boom():
            raise RuntimeError
        self.assertTrue(mb.Silence(None, posture=boom).active())      # a broken reader fails closed

    def test_budgets_follow(self):
        c = mb.Commands(lambda *a: True, public_budget=(6, 3600.0), per_radio_budget=(2, 3600.0))
        c.set_budgets(0, 0)
        self.assertFalse(c.would_answer("!q water", False, "!12345678"))
        c.set_budgets(12, 3)
        self.assertTrue(c.would_answer("!q water", False, "!12345678"))


class Sealing(unittest.TestCase):
    """Radio state on persistent storage and family-board lines are sealed when sealed records are set up."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pm-seal-")).resolve()
        self.old = (mb.SEALER, list(mb.RAM_DIRS), mb.FAMILY_BOARD)
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("needs openssl")
        try:
            import sealed
        except Exception:  # noqa: BLE001
            self.skipTest("no sealed records module")
        self.sealed = sealed
        s = sealed.use(self.tmp / "sealed", openssl=openssl)
        s.seal_text("x")                                           # the portal makes the device key
        (self.tmp / "members.json").write_text("{}")
        self.assertIsNotNone(mb.setup_sealer({"sealed": {"openssl": openssl}}, self.tmp / "members.json"))

    def tearDown(self):
        mb.SEALER, mb.RAM_DIRS[:], mb.FAMILY_BOARD = self.old[0], self.old[1], self.old[2]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_state_files(self):
        mb.RAM_DIRS[:] = [str(self.tmp / "run")]
        (self.tmp / "run").mkdir()
        doc = {"updated": 1, "nodes": [{"id": "!a1b2c3d4", "name": "Hendersons"}]}
        self.assertTrue(mb.write_state(self.tmp / "run" / "mesh-nodes.json", doc))
        self.assertEqual(json.loads((self.tmp / "run" / "mesh-nodes.json").read_text()), doc)
        self.assertTrue(mb.write_state(self.tmp / "mesh-nodes.json", doc))
        d = json.loads((self.tmp / "mesh-nodes.json").read_text())
        self.assertEqual(d["nodes"], [])
        self.assertNotIn("Hendersons", (self.tmp / "mesh-nodes.json").read_text())
        self.assertEqual(json.loads(mb.SEALER.open_text(d["sealed"], recent_ok=True)), doc)
        node = FakeNode()
        mb.save_channel_link(ns(localNode=node), self.tmp / "mesh-channel.json")
        self.assertNotIn("meshtastic.org", (self.tmp / "mesh-channel.json").read_text())

    def test_family_board_lines_sealed_and_sealed_names(self):
        mb.FAMILY_BOARD = self.tmp / "family-board.jsonl"
        mb.board_add("Ana", "home safe at 6", "family", "!a1b2c3d4", "m_ana")
        line = json.loads(mb.FAMILY_BOARD.read_text().splitlines()[0])
        self.assertEqual(list(line), ["s"])
        self.assertNotIn("home safe", mb.FAMILY_BOARD.read_text())
        self.assertEqual(mb.SEALER.open_json(line["s"], recent_ok=True)["text"], "home safe at 6")
        (self.tmp / "members.json").write_text(json.dumps({"members": {"m_ana": {"radios": ["!a1b2c3d4"]}},
                                                           "sealed": "hbs1:xyz"}))
        self.assertEqual(mb.radio_registry(self.tmp / "members.json")["!a1b2c3d4"]["name"], "a family member")


class WipeRekey(unittest.TestCase):
    def test_every_private_channel_gets_a_new_key(self):
        node = FakeNode()
        node.channels[3] = FakeChannel(2, b"C" * 32, "circle")
        r = mb.wipe_radio_keys(ns(localNode=node), 1, rng=lambda n: b"N" * n)
        self.assertEqual((r["ok"], r["channels"]), (True, [1, 3]))      # not 0, not the default-key "LongSlow"
        self.assertEqual(node.channels[3].settings.psk, b"N" * 32)
        self.assertEqual(node.channels[4].settings.psk, b"\x01")

    def test_rotation_request_with_wipe_flag(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm-wipe-"))
        try:
            node = FakeNode()
            req, res, link = tmp / "req.json", tmp / "res.json", tmp / "link.json"
            link.write_text("{}")
            req.write_text(json.dumps({"id": "w1", "at": time.time(), "wipe": True, "channels": "private",
                                       "forget_link": True}))
            with contextlib.redirect_stderr(io.StringIO()):
                out = mb.handle_rotation(ns(localNode=node), 1, req, res, link)
            self.assertTrue(out["ok"] and out["wipe"])
            self.assertEqual(json.loads(res.read_text())["id"], "w1")
            self.assertFalse(link.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────────── main() with a fake radio ─────────────────────────────

class _Stop(Exception):
    pass


class FakeIface:
    def __init__(self):
        self.devPath = "/dev/ttyACM0"
        self.nodes = {}
        self.sent, self.data_sent = [], []
        self.localNode = FakeNode()

    def sendText(self, text, destinationId=None, channelIndex=0, hopLimit=None, replyId=None):
        self.sent.append((text, destinationId, channelIndex))

    def sendData(self, data, destinationId="^all", portNum=256, wantAck=False, channelIndex=0, hopLimit=None):
        self.data_sent.append((bytes(data), destinationId, channelIndex))

    def getMyNodeInfo(self):
        return {"user": {"id": "!0badfeed", "longName": "Honey Badger HQ", "shortName": "HB"}}


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pm-main-")).resolve()
        self.saved = {k: getattr(mb, k) for k in ("BOARD", "FAMILY_BOARD", "MEMBERS", "OUTBOX", "NODES", "ALERTS", "ROTATE_REQ",
                                                  "ROTATE_RES", "CHANNEL_LINK", "TELEMETRY", "BT_PIN", "PEERS", "BEAT",
                                                  "API", "time", "CIRCLE_LINK", "CIRCLE_STATUS", "CIRCLE_ASK",
                                                  "RADIO_REQ", "RADIO_RES")}
        self.mods = {k: sys.modules.get(k) for k in ("meshtastic", "meshtastic.serial_interface", "pubsub")}
        self.env = {k: os.environ.pop(k, None) for k in ("PREPPER_CONFIG", "MESH_PEERS", "MESH_TELEMETRY", "MESH_NODES",
                                                         "PORTAL_SETTINGS", "PREPPER_RUN_DIR")}
        mb.BEAT = self.tmp / "mesh.ok"
        mb.API = "http://127.0.0.1:9"
        (self.tmp / "members.json").write_text(json.dumps({"v": 1, "members": {}}))
        (self.tmp / "run").mkdir()

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(mb, k, v)
        for k, v in self.mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        for k, v in self.env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, outbox=(), loops=3, step=1.0, during=None, radio=None, extra=""):
        t = self.tmp
        radio = radio or {"mesh_family_channel": 1, "mesh_public_channel": 0, "mesh_boot_quiet_s": 0}
        (t / "prepper.toml").write_text("[radio]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in radio.items()) + f"""

[portal]
board_file = "{t}/board.jsonl"
family_board_file = "{t}/family-board.jsonl"
members_file = "{t}/members.json"
mesh_outbox = "{t}/mesh-outbox.jsonl"
alerts_file = "{t}/alerts.json"
settings_file = "{t}/portal-settings.json"
run_dir = "{t}/run"
{extra}
""")
        os.environ["PREPPER_CONFIG"] = str(t / "prepper.toml")
        (t / "mesh-outbox.jsonl").write_text("".join(json.dumps(p) + "\n" for p in outbox))
        (t / "mesh-outbox.jsonl.offset").write_text("0")
        self.iface = FakeIface()
        self.subs = {}
        mt = types.ModuleType("meshtastic")
        si = types.ModuleType("meshtastic.serial_interface")
        si.SerialInterface = lambda: self.iface
        mt.serial_interface = si
        ps = types.ModuleType("pubsub")
        ps.pub = types.SimpleNamespace(subscribe=lambda fn, topic: self.subs.__setitem__(topic, fn))
        sys.modules.update({"meshtastic": mt, "meshtastic.serial_interface": si, "pubsub": ps})
        n, now = [0], [time.time()]

        def sleep(_s):
            n[0] += 1
            now[0] += step
            if during:
                during(n[0], self)
            if n[0] >= loops:
                raise _Stop
        mb.time = types.SimpleNamespace(time=lambda: now[0], sleep=sleep, strftime=time.strftime, gmtime=time.gmtime)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                mb.main()
            except _Stop:
                pass
        self.log = err.getvalue()
        return self.iface.sent

    def test_ram_only_state_goes_to_run_dir(self):
        self.run_main(loops=2)
        run = self.tmp / "run"
        self.assertTrue((run / "mesh-peers.json").exists())
        self.assertTrue((run / "mesh-channel.json").exists())
        self.assertTrue((run / "mesh-nodes.json").exists())
        self.assertFalse((self.tmp / "mesh-peers.json").exists())
        doc = json.loads((run / "mesh-peers.json").read_text())
        self.assertEqual(doc["posture"]["level"], "guarded")
        self.assertEqual(doc["radio_names"], {"long": "Honey Badger HQ", "short": "HB"})

    def test_lockdown_silence_only_member_family_posts(self):
        P.write(self.tmp / "posture.json", "lockdown", "t")
        (self.tmp / "alerts.json").write_text(json.dumps([
            {"id": "TOR", "event": "Tornado Warning", "areas": "here", "expires": "18:30", "expires_ts": time.time() + 9000}]))
        sent = self.run_main([{"text": "C", "board": "community"}, {"text": "Ana: home", "board": "family", "member": True},
                              {"text": "Sensors: x", "board": "family"}], loops=60)
        self.assertEqual([(t, ch) for t, _, ch in sent], [("Ana: home", 1)])
        self.assertEqual(self.iface.data_sent, [])                    # no beacons, claims, circle packets
        self.assertEqual(self.iface.localNode.localConfig.device.role, mb.ROLE_CLIENT_MUTE)
        self.assertTrue((self.tmp / "mesh-radio-saved.json").exists())

    def test_lockdown_radio_option_off(self):
        P.write(self.tmp / "posture.json", "lockdown", "t")
        self.run_main(loops=2, extra="\n[security]\nlockdown_radio = false")
        self.assertEqual(self.iface.localNode.localConfig.device.role, 0)         # the owner said: leave the radio
        self.assertEqual(self.iface.sent, [])                                       # (still radio silence)

    def test_posture_change_mid_run_restores_radio_and_relays(self):
        P.write(self.tmp / "posture.json", "lockdown", "t")

        def during(i, case):
            if i == 5:
                P.write(case.tmp / "posture.json", "guarded", "t")
            if i == 8:                                    # (an alert during silence is never sent later: by design)
                (case.tmp / "alerts.json").write_text(json.dumps([{"id": "TOR", "event": "Tornado Warning", "areas": "here",
                                                                    "expires": "18:30", "expires_ts": time.time() + 9000}]))
        sent = self.run_main(loops=90, during=during)
        self.assertEqual(self.iface.localNode.localConfig.device.role, 0)      # restored
        self.assertFalse((self.tmp / "mesh-radio-saved.json").exists())
        self.assertEqual(sorted(ch for t, _, ch in sent if "Tornado" in t), [0, 1])
        self.assertIn("security posture now Guarded", self.log)

    def test_neighbourly_coarse_position_and_alerts_family_only_in_lockdown_with_silence_override(self):
        P.write(self.tmp / "posture.json", "neighbourly", "t")
        self.run_main(loops=2)
        self.assertEqual(self.iface.localNode.channels[0].settings.module_settings.position_precision, 12)
        P.write(self.tmp / "posture.json", "guarded", "t")
        self.run_main(loops=2)
        self.assertEqual(self.iface.localNode.channels[0].settings.module_settings.position_precision, 0)

    def test_stranger_budget_zero_in_lockdown_even_when_not_silent(self):
        P.write(self.tmp / "posture.json", "lockdown", "t")
        # an owner who explicitly turned silence off: Lockdown ignores that (looser) — and strangers get nothing
        self.run_main(loops=2, radio={"mesh_family_channel": 1, "mesh_public_channel": 0, "mesh_silent": False})
        self.iface.sent.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            self.subs["meshtastic.receive.text"]({"fromId": "!12345678", "channel": 0, "decoded": {"text": "!q water"},
                                                  "id": 7}, self.iface)
        self.assertEqual(self.iface.sent, [])

    def test_circle_created_through_main_answers_pointers(self):
        cd = self.tmp / "features" / "circles"
        cd.mkdir(parents=True)
        (cd / "bridge.json").write_text(json.dumps({"name": "Elm St", "share": {"skills": True}, "offers": [
            {"category": "skills", "topic": "power", "text": "can fix generators", "at": 1}], "blocked": []}))

        def during(i, case):
            if i == 1:
                (case.tmp / "mesh-radio-req.json").write_text(json.dumps({"id": "c1", "at": mb.time.time(), "op": "create",
                                                                         "name": "Oak"}))
            if i == 3:
                with contextlib.redirect_stderr(io.StringIO()):
                    case.subs["meshtastic.receive.text"]({"fromId": "!12345678", "channel": 2,
                                                          "decoded": {"text": "!help power"}}, case.iface)
        sent = self.run_main(loops=20, during=during)
        res = json.loads((self.tmp / "mesh-radio-res.json").read_text())
        self.assertEqual((res["ok"], res["channel"]), (True, 2))
        self.assertTrue((self.tmp / "run" / "mesh-circle.json").exists())
        self.assertIn(("power: can fix generators — ask Elm St", "!12345678", 2), sent)
        st = json.loads((self.tmp / "run" / "circle-status.json").read_text())
        self.assertEqual(st["channel"], 2)
        self.assertEqual(st["log"][-1]["result"], "answered")

    def test_bt_pin_rederived_after_restart(self):
        self.iface = None
        # the radio already has our random PIN (from before a reboot): the RAM file is written again from the radio
        node = FakeNode()
        node.localConfig.bluetooth = ns(enabled=True, mode=1, fixed_pin=482913)
        pin = self.tmp / "run" / "mesh-bt-pin.json"
        self.assertIsNone(mb.secure_bt_pin(ns(localNode=node), pin))
        self.assertEqual(json.loads(pin.read_text())["pin"], "482913")
        self.assertEqual(oct(pin.stat().st_mode & 0o777), "0o600")

    def test_nodes_older_than_a_day_are_dropped(self):
        now = 1_800_000_000
        nodes = {"!a": {"user": {"id": "!a"}, "position": {"latitude": 1.0, "longitude": 1.0, "time": now - 90000}},
                 "!b": {"user": {"id": "!b"}, "position": {"latitude": 1.0, "longitude": 1.0, "time": now - 3600}}}
        self.assertEqual([n["id"] for n in mb.nodes_snapshot(nodes, None, now)], ["!b"])


if __name__ == "__main__":
    unittest.main()
