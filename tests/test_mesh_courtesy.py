"""
Badger-mesh courtesy (software/meshbridge/meshbridge.py, docs/MESH.md): many Badgers on one Meshtastic
mesh cooperate instead of flooding it. Includes a simulation: N Badgers (2, 5, 20) on a fake shared
LoRa channel with a fake clock, built exactly as main() builds them (build_bridge + RadioLink), with
claims/beacons on PRIVATE_APP and texts delivered after a little "airtime" latency.

Covers: one NOAA alert → one relay per channel; one "!q" → one reply; forged claims/dones can't stop
an alert; a lost relay is picked up; busy-channel deferral; budgets shrinking with peers; startup
storm; hostile/garbled PRIVATE_APP packets; old outbox lines; loops/echo and board-once; replyId and
hopLimit handling; radio silence; privacy of control packets; public position precision; the status
file and the portal's status line.  Run: python3.11 tests/test_mesh_courtesy.py
"""
from __future__ import annotations

import base64
import contextlib
import heapq
import io
import itertools
import json
import os
import random
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "meshbridge"))
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"
import meshbridge as mb  # noqa: E402

T0 = 1_800_000_000.0
ASKER = "!0000a5c0"


def quiet():
    return contextlib.redirect_stderr(io.StringIO())


def alert(aid="TOR-1234567-KBOU", event="Tornado Warning", ttl=3600, now=T0):
    return {"id": aid, "event": event, "areas": "3 areas in CO", "expires": "Tue 18:30",
            "expires_ts": now + ttl, "test": False}


# ───────────────────────────── the fake mesh ─────────────────────────────

class SimRadio:
    """A Meshtastic interface as the bridge sees it: sendText/sendData go onto the shared fake channel."""

    def __init__(self, net, owner, hop=True, reply=True):
        self.net, self.owner = net, owner
        if not hop and not reply:
            self.sendText = self._text_plain

    def sendText(self, text, destinationId=None, channelIndex=0, hopLimit=None, replyId=None):
        self.net.tx(self.owner, "text", channelIndex, text, destinationId, hopLimit, replyId)

    def _text_plain(self, text, destinationId=None, channelIndex=0):
        self.net.tx(self.owner, "text", channelIndex, text, destinationId, None, None)

    def sendData(self, data, destinationId="^all", portNum=256, wantAck=False, channelIndex=0, hopLimit=None):
        assert portNum == 256 and wantAck is False
        self.net.tx(self.owner, "data", channelIndex, bytes(data), None, hopLimit, None)

    def getMyNodeInfo(self):
        return {"user": {"id": self.owner.id}}


class Net:
    """Everyone hears everyone (on the channels they share). DMs reach only their addressee, as with the
    real firmware. Control packets arrive after 0.5 s, texts after 1.5 s (airtime + rebroadcasts)."""

    def __init__(self, ctrl_latency=0.5, text_latency=1.5):
        self.t = T0
        self.q: list = []
        self.seq = itertools.count()
        self.pid = itertools.count(1000)
        self.nodes: list = []
        self.log: list = []           # every transmission: dict(t, src, kind, ch, payload, dest, hop, reply, lost)
        self.lost_text_from: set = set()
        self.lose_first_text = False  # the first text anyone sends never makes it on air (and nothing more from them)
        self.ctrl_latency, self.text_latency = ctrl_latency, text_latency

    def clock(self):
        return self.t

    def tx(self, src, kind, local_ch, payload, dest=None, hop=None, reply=None):
        name = src.chans.get(local_ch)
        if kind == "text" and self.lose_first_text:
            self.lose_first_text = False
            self.lost_text_from.add(src.id)
        lost = kind == "text" and src.id in self.lost_text_from
        self.log.append({"t": self.t, "src": src.id, "kind": kind, "ch": name, "payload": payload, "dest": dest,
                         "hop": hop, "reply": reply, "lost": lost})
        if name is None or lost:
            return
        pid = next(self.pid)
        for n in self.nodes:
            if n is src:
                continue
            j = n.index_of(name)
            if j is None or (dest and dest != n.id):
                continue
            pkt = {"from": src.num, "fromId": src.id, "toId": dest or "^all", "channel": j, "id": pid,
                   "hopStart": 3, "hopLimit": 3}
            if kind == "text":
                pkt["decoded"] = {"portnum": "TEXT_MESSAGE_APP", "text": payload}
                if reply:
                    pkt["decoded"]["replyId"] = reply
            else:
                pkt["decoded"] = {"portnum": "PRIVATE_APP", "payload": payload}
            lat = self.text_latency if kind == "text" else self.ctrl_latency
            heapq.heappush(self.q, (self.t + lat, next(self.seq), n, pkt))

    def run(self, seconds, step=0.25):
        end = self.t + seconds
        while self.t < end:
            self.t += step
            while self.q and self.q[0][0] <= self.t:
                _, _, n, pkt = heapq.heappop(self.q)
                n.receive(pkt)
            for n in self.nodes:
                n.tick()

    def replies(self, to=None):
        """Answers to commands: DMs to `to`, or public replies (replyId set) — alerts and posts have neither."""
        return [e for e in self.log if e["kind"] == "text" and not e["lost"] and
                ((to is not None and e["dest"] == to) or (e["dest"] is None and e["reply"] is not None))]

    def texts(self, needle, ch=None, delivered=True):
        return [e for e in self.log if e["kind"] == "text" and needle in e["payload"]
                and (ch is None or e["ch"] == ch) and not (delivered and e["lost"])]

    def introduce(self):
        """As if every Badger had heard every other Badger's beacon."""
        for a in self.nodes:
            for b in self.nodes:
                if a is not b and isinstance(a, SimBadger) and isinstance(b, SimBadger):
                    a.bridge.courtesy.peers.heard(b.id, b.num, "beacon")


class Plain:
    """An ordinary Meshtastic radio (a person with a phone) — or a hostile one."""

    def __init__(self, net, nid, chans=None):
        self.net, self.id, self.num = net, nid, int(nid[1:], 16)
        self.chans = chans or {0: "public"}
        self.heard: list = []
        net.nodes.append(self)

    def index_of(self, name):
        return next((i for i, n in self.chans.items() if n == name), None)

    def receive(self, pkt):
        self.heard.append(pkt)

    def tick(self):
        pass

    def say(self, text, ch=0, dest=None):
        self.net.tx(self, "text", ch, text, dest)

    def data(self, payload, ch=0):
        self.net.tx(self, "data", ch, payload)


class SimBadger(Plain):
    def __init__(self, net, i, chans=None, cfg=None, seed=0, load=None, tmp=None, silence=None):
        super().__init__(net, f"!{0x10000000 + i:08x}", chans or {0: "public"})
        self.posts: list = []
        self.radio = SimRadio(net, self)
        self.link = mb.RadioLink(self.radio, silence=silence)
        fam = next((k for k, v in self.chans.items() if v.startswith("fam")), -1)
        self.commands = mb.Commands(lambda t, d: None, clock=net.clock, library=lambda q: f"LIB:{q}",
                                    status=lambda: "STATUS", ai=lambda q: "AI", spawn=lambda fn: fn(),
                                    access=mb.DEFAULT_ACCESS)
        self.bridge = mb.build_bridge(self.link, cfg=cfg or {}, family_ch=fam, public_ch=0, my_id=self.id,
                                      my_num=self.num, commands=self.commands,
                                      members=(tmp or Path("/nonexistent")) / "members.json", clock=net.clock,
                                      rng=random.Random(seed * 7919 + i),
                                      board=lambda name, text, board, radio, member: self.posts.append((board, text)),
                                      load=load)
        self.sent_alerts: dict = {}
        self.bridge.relay_alerts([], self.sent_alerts, net.t)       # booted: later alerts are "new"

    def receive(self, pkt):
        with quiet():
            if pkt["decoded"].get("portnum") == "TEXT_MESSAGE_APP":
                self.bridge.on_text(pkt, None)
            self.bridge.on_data(pkt, None)                           # "meshtastic.receive" gets every packet

    def tick(self):
        with quiet():
            self.bridge.tick()

    def hear_alert(self, a):
        with quiet():
            self.bridge.relay_alerts([a], self.sent_alerts, self.net.t)


def town(n, seed=0, introduce=True, families=None, cfg=None, **kw):
    net = Net()
    badgers = []
    for i in range(n):
        chans = {0: "public"}
        if families:
            chans[1] = families[i]
        badgers.append(SimBadger(net, i, chans, cfg=cfg, seed=seed, **kw))
    if introduce:
        net.introduce()
    return net, badgers


# ───────────────────────────── the simulation ─────────────────────────────

class OneAlertOneRelay(unittest.TestCase):
    def test_one_relay_per_channel_for_2_5_20_badgers(self):
        for n in (2, 5, 20):
            for seed in range(4):
                with self.subTest(n=n, seed=seed):
                    fams = [("fam-A" if i % 3 == 0 else "fam-B" if i % 3 == 1 else f"fam-{i}") for i in range(n)]
                    net, bs = town(n, seed, families=fams)
                    a = alert()
                    for b in bs:
                        b.hear_alert(a)                                   # the same SAME burst, the same instant
                    net.run(120)
                    self.assertEqual(len(net.texts("Tornado Warning", "public")), 1)
                    for fam in set(fams):
                        self.assertEqual(len(net.texts("Tornado Warning", fam)), 1, fam)
                    first = net.texts("Tornado Warning", "public")[0]
                    self.assertLessEqual(first["t"] - T0, 45 + mb.SETTLE + 1)
                    # nobody lost it silently: every Badger knows it is dealt with, and who relayed it
                    for b in bs:
                        self.assertIn("TOR-1234567-KBOU", b.sent_alerts)
                    supp = sum(b.bridge.courtesy.counts["suppressed_alert"] for b in bs)
                    self.assertGreaterEqual(supp, n - 1)
                    rec = [e for b in bs for e in b.bridge.courtesy.events if e["what"] == "suppressed"]
                    self.assertTrue(all(e["by"] for e in rec))

    def test_relays_are_tagged_and_recognised_exactly(self):
        a = alert()
        text = mb.format_alert(a)
        self.assertTrue(text.endswith(" #" + mb.tag_of("alert:" + a["id"])))
        self.assertLessEqual(len(text.encode()), 200)
        m = mb.alert_matcher(a)
        self.assertTrue(m(text))
        self.assertTrue(m("  " + text.replace(" · ", "  ·  ").upper() + "\n"))   # normalised: spacing, case
        # CHANGED (red team finding 7): only the exact text this Badger would send counts. The older untagged
        # format of Badgers before the courtesy update no longer suppresses (a duplicate is safe, silence is not),
        # and neither does anything with the right tag and event but other words.
        tag = mb.tag_of("alert:" + a["id"])
        old = "NOAA ⚠ Tornado Warning · 3 areas in CO · until Tue 18:30. More on the Badger's Wi-Fi."
        for t in (old, f"#{tag}", f"lol #{tag}", f"NOAA ⚠ Tornado Warning #{mb.tag_of('alert:other')}",
                  f"NOAA ⚠ Flood Warning · x · until y #{tag}",
                  f"NOAA ⚠ Tornado Warning · CANCELLED by NWS, all clear #{tag}",
                  f"NOAA ⚠ Tornado Warning · 3 areas in CO · until Tue 18:30. Test only, disregard. #{tag}",
                  text.replace("Tue 18:30", "Tue 18:31"), text.replace("3 areas", "4 areas"), text[:-1],
                  "NOAA ⚠ Tornado Warning · 3 areas in CO · until Wed 01:00. More on the Badger's Wi-Fi."):
            self.assertFalse(m(t), t)

    def test_old_badger_relay_no_longer_suppresses_ours(self):
        # CHANGED (finding 7): an older Badger's untagged relay (different words) used to stand ours down. Only
        # an exact copy may do that now, so the neighbourhood gets our relay too: one copy from our Badgers.
        net, bs = town(3)
        old = Plain(net, "!01d0ba5e")                                     # a Badger without the update
        a = alert()
        for b in bs:
            b.hear_alert(a)
        old.say("NOAA ⚠ Tornado Warning · 3 areas in CO · until Tue 18:30. More on the Badger's Wi-Fi.")
        net.run(120)
        self.assertEqual(len([e for e in net.texts("Tornado Warning") if e["src"] != old.id]), 1)

    def test_exact_copy_from_another_radio_suppresses_ours(self):
        net, bs = town(3)
        other = Plain(net, "!01d0ba5e")
        a = alert()
        for b in bs:
            b.hear_alert(a)
        other.say(mb.format_alert(a))                                     # the very text we would send
        net.run(120)
        self.assertEqual([e for e in net.texts("Tornado Warning") if e["src"] != other.id], [])
        self.assertTrue(all("TOR-1234567-KBOU" in b.sent_alerts for b in bs))   # dealt with, not lost

    def test_heard_before_our_own_decoder_caught_it(self):
        net, bs = town(2)
        a = alert()
        bs[0].hear_alert(a)
        net.run(60)                                                       # bs[0] relays; bs[1]'s SDR is off
        bs[1].hear_alert(a)                                               # …decodes it a minute later
        net.run(120)
        self.assertEqual(len(net.texts("Tornado Warning")), 1)


class SafetyRule(unittest.TestCase):
    def test_forged_claims_cannot_stop_an_alert(self):
        for n in (1, 5, 20):
            with self.subTest(n=n):
                net, bs = town(n, seed=3)
                evil = Plain(net, "!00000001")                            # lowest node number: wins every tie
                a = alert()
                kh = mb.key_hash("alert:" + a["id"])
                for b in bs:
                    b.hear_alert(a)
                for _ in range(100):                                      # a claim every 2 s for 200 s
                    evil.data(mb.encode_claim(kh, 255))
                    net.run(2)
                relays = net.texts("Tornado Warning", "public")
                self.assertGreaterEqual(len(relays), 1)                   # delayed, never silenced
                self.assertLessEqual(relays[0]["t"] - T0, 45 + mb.SAFETY_HOLD_MAX + 15 + 2 * 11.25 + 2 * mb.SETTLE + 2)
                self.assertLessEqual(len(relays), 3)                      # and still no flood

    def test_forged_done_and_bare_tag_do_not_cancel_an_alert(self):
        net, bs = town(4, seed=1)
        evil = Plain(net, "!00000002")
        a = alert()
        for b in bs:
            b.hear_alert(a)
        evil.data(mb.encode_done(mb.key_hash("alert:" + a["id"])))
        evil.say("#" + mb.tag_of("alert:" + a["id"]))
        evil.say("NOAA ⚠ #" + mb.tag_of("alert:" + a["id"]))
        net.run(120)
        self.assertEqual(len([e for e in net.texts("Tornado Warning") if e["src"] != evil.id]), 1)

    def test_lost_relay_is_picked_up(self):
        for seed in range(3):
            with self.subTest(seed=seed):
                # CHANGED: no claims on the public channel any more; the Badger whose turn comes first loses
                # its relay on the air, and the next one in turn sends it
                net, bs = town(5, seed=seed)
                a = alert()
                net.lose_first_text = True
                for b in bs:
                    b.hear_alert(a)
                net.run(240)
                claimer = next(e for e in net.log if e["kind"] == "text")["src"]
                delivered = net.texts("Tornado Warning", "public")
                self.assertEqual(len(delivered), 1)
                self.assertNotEqual(delivered[0]["src"], claimer)

    def test_alert_never_deferred_by_a_busy_channel_or_budget(self):
        net, bs = town(1, load=lambda: (95.0, 50.0))
        bs[0].hear_alert(alert())
        net.run(10)
        self.assertEqual(len(net.texts("Tornado Warning")), 1)


class Replies(unittest.TestCase):
    def test_one_reply_to_a_shared_question(self):
        for n in (2, 5, 20):
            for seed in range(3):
                with self.subTest(n=n, seed=seed):
                    net, bs = town(n, seed)
                    asker = Plain(net, ASKER)
                    asker.say("!q water purification")
                    net.run(60)
                    # CHANGED (finding 6): a stranger's question on the public channel is answered in the open
                    # as a reply to it (no DM, no control packets): the other Badgers hear it and stay quiet
                    replies = net.replies(ASKER)
                    self.assertEqual(len(replies), 1)
                    r = replies[0]
                    self.assertEqual((r["payload"], r["dest"], r["ch"]), ("LIB:water purification", None, "public"))
                    self.assertEqual(r["hop"], 2)                         # local chatter: small hop limit
                    self.assertIsNotNone(r["reply"])                      # replyId = the request's packet id
                    self.assertLessEqual(r["t"] - T0, 10 + 2)
                    self.assertEqual([e for e in net.log if e["kind"] == "data"], [])

    def test_lone_badger_answers_at_once(self):
        net, bs = town(1)
        Plain(net, ASKER).say("!q knots")
        net.run(2)
        self.assertEqual(len(net.replies(ASKER)), 1)
        # CHANGED (finding 6): no "done" (or any control packet) on the public channel any more
        self.assertEqual([e for e in net.log if e["kind"] == "data"], [])

    def test_strangers_get_no_refusal_and_no_help(self):
        net, bs = town(3)
        asker = Plain(net, ASKER)
        for cmd in ("!help", "!status", "!ai water", "!q", "!ai"):        # family-only, or nothing to answer
            asker.say(cmd)
            asker.say(cmd, dest=bs[0].id)
            net.run(40)
        self.assertEqual([e for e in net.log if e["src"] != ASKER], [])

    def test_dm_to_one_badger_is_answered_by_that_badger_only(self):
        # CHANGED (finding 6): a stranger's DM is answered only for a community command, after a short wait
        # (never at once); "!status" (family) from a stranger gets nothing at all
        net, bs = town(5)
        asker = Plain(net, ASKER)
        asker.say("!status", dest=bs[3].id)
        net.run(20)
        self.assertEqual(net.replies(ASKER), [])
        asker.say("!q rope", dest=bs[3].id)
        net.run(2)
        self.assertEqual(net.replies(ASKER), [])                          # not at once
        net.run(20)
        self.assertEqual([(e["src"], e["dest"]) for e in net.replies(ASKER)], [(bs[3].id, ASKER)])

    def test_family_dm_is_answered_at_once(self):
        net, bs = town(2, families=["fam-A", "fam-A"])
        mom = Plain(net, "!0000a0a0", {0: "public", 1: "fam-A"})
        mom.say("!status", ch=1, dest=bs[1].id)
        net.run(2)
        self.assertEqual([(e["src"], e["payload"], e["ch"]) for e in net.replies("!0000a0a0")],
                         [(bs[1].id, "STATUS", "fam-A")])

    def test_dm_for_another_radio_is_not_answered(self):
        net, bs = town(1)
        with quiet():
            bs[0].bridge.on_text({"fromId": ASKER, "toId": "!0ther000", "channel": 0, "decoded": {"text": "!help"}})
        net.run(2)
        self.assertEqual([e for e in net.log if e["kind"] == "text"], [])

    def test_a_heard_reply_with_reply_id_suppresses_ours(self):
        net, bs = town(2)
        b = bs[0]
        with quiet():
            b.bridge.on_text({"fromId": ASKER, "channel": 0, "id": 4242, "decoded": {"text": "!q fire"}})
            b.bridge.on_text({"fromId": "!0b0b0b0b", "toId": "^all", "channel": 0,
                              "decoded": {"text": "Fire: …", "replyId": 4242}})
        net.run(30)
        self.assertEqual([e for e in net.log if e["src"] == b.id and e["kind"] == "text"], [])

    def test_badgers_that_would_refuse_leave_it_to_one_that_answers(self):
        net, bs = town(5, seed=2)
        for b in bs[1:]:
            b.commands._access = lambda: dict(mb.DEFAULT_ACCESS, **{"!q": "off"})
        Plain(net, ASKER).say("!q knots")
        net.run(60)
        self.assertEqual([(e["src"], e["payload"]) for e in net.replies(ASKER)], [(bs[0].id, "LIB:knots")])
        self.assertEqual([e for e in net.log if e["src"] not in (bs[0].id, ASKER)], [])   # the others: silent

    def test_flood_is_answered_once_per_cooldown_mesh_wide(self):
        net, bs = town(5, seed=4)
        asker = Plain(net, ASKER)
        for i in range(6):
            asker.say(f"!q thing {i}")
            net.run(5)
        net.run(60)
        self.assertLessEqual(len(net.replies(ASKER)), 3)                  # not 5 Badgers × 6 questions


class StartupStorm(unittest.TestCase):
    def test_town_wide_restart(self):
        for presumed in (False, True):
            with self.subTest(presumed=presumed):
                net = Net()
                bs = [SimBadger(net, i, seed=11) for i in range(20)]
                if presumed:                                              # peers from the saved table
                    for b in bs:
                        b.bridge.courtesy.peers.load([{"id": o.id, "last": T0 - 5 * 3600} for o in bs if o is not b])
                        self.assertEqual(b.bridge.courtesy.peers.active(), 19)
                a = alert()
                for b in bs:
                    b.bridge.booted = False                               # first read after the restart
                    b.hear_alert(a)
                net.run(29)
                self.assertEqual(net.texts("Tornado Warning"), [])        # the quiet period
                net.run(200)
                self.assertEqual(len(net.texts("Tornado Warning")), 1)


class Airtime(unittest.TestCase):
    def make(self, load=(None, None), peers=0, **cfg):
        self.t = [T0]
        self.out = []
        a = mb.Airtime(lambda text, dest, ch, hop, rid, **kw: self.out.append((text, dest, ch, hop, rid)) or True,
                       lambda: self.t[0], mb.courtesy_config(cfg), peers=lambda: peers, load=lambda: load)
        return a

    def put_all(self, a):
        for k in mb.KINDS:
            a.put({"kind": k, "text": k, "ch": 0}, pump=False)
        a.pump()
        return [o[0] for o in self.out]

    def test_priority_order(self):
        self.assertEqual(self.put_all(self.make()), list(mb.KINDS))

    def test_busy_channel_deferral(self):
        a = self.make(load=(30.0, 2.0))
        self.assertEqual(self.put_all(a), ["alert", "danger", "family", "reply", "community"])
        self.assertEqual(a.status()["deferred"], {"auto": 1})
        a = self.make(load=(45.0, 2.0))
        self.assertEqual(self.put_all(a), ["alert", "danger", "family", "reply"])
        self.assertEqual(a.status()["deferred"], {"community": 1, "auto": 1})
        a = self.make(load=(5.0, 9.5))                                   # we hog the air ourselves
        self.assertNotIn("auto", self.put_all(a))

    def test_deferred_items_expire_instead_of_going_out_late(self):
        a = self.make(load=(45.0, None))
        with quiet():
            self.put_all(a)
            self.t[0] += 7201
            a.load = lambda: (None, None)
            a.pump()
        self.assertEqual([o[0] for o in self.out], ["alert", "danger", "family", "reply"])
        self.assertEqual(a.status()["queued"], {})

    def test_budget_scales_with_peers(self):
        a = self.make()
        b = [a.budget("auto", p) for p in (0, 4, 20, 30, 50)]
        self.assertEqual(b[0], 4)
        self.assertTrue(all(x > y for x, y in zip(b, b[1:], strict=False)), b)
        self.assertGreaterEqual(b[-1], mb.BUDGET_MIN["auto"])
        # town of 30: what all Badgers' automatic notes together may use is far below 30 × 4 an hour
        self.assertLess(30 * a.budget("auto", 29), 30)
        self.assertEqual(a.budget("alert", 50), float("inf"))
        a = self.make(peers=20)
        for i in range(5):
            a.put({"kind": "auto", "text": f"a{i}", "ch": 0}, pump=False)
        with quiet():
            for _ in range(12):
                a.pump()
                self.t[0] += 300                                          # one hour
        self.assertEqual(len(self.out), 1)
        self.assertEqual(self.make(**{"mesh_budget": {"auto": 0}}).budget("auto", 0), 0.0)

    def test_hop_limits(self):
        a = self.make()
        for k in mb.KINDS:
            a.put({"kind": k, "text": k, "ch": 0})
        hops = {o[0]: o[3] for o in self.out}
        self.assertEqual(hops, {"alert": None, "danger": None, "family": None, "reply": 2, "community": None, "auto": 2})

    def test_failed_send_is_retried(self):
        ok = [False]
        a = mb.Airtime(lambda *x, **k: ok[0], lambda: T0)
        a.put({"kind": "family", "text": "x", "ch": 1})
        self.assertEqual(a.status()["queued"], {"family": 1})
        ok[0] = True
        a.pump()
        self.assertEqual(a.status()["queued"], {})


class ForecastAndPosts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="courtesy-")).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def outbox(self, b, lines, name):
        p = self.tmp / f"{name}.jsonl"
        p.write_text("".join((json.dumps(x) if not isinstance(x, str) else x) + "\n" for x in lines))
        (self.tmp / f"{name}.jsonl.offset").write_text("0")
        ob = mb.Outbox(p, b.bridge.family_ch, b.bridge.public_ch, None, b.net.clock, airtime=b.bridge.airtime,
                       courtesy=b.bridge.courtesy, echo=b.bridge.heard.seen)
        with quiet():
            ob.read()
        return ob

    def test_same_forecast_from_20_badgers_goes_out_once(self):
        net, bs = town(20, seed=5)
        key = "forecast:162.550:2026-11-17:AM"
        for i, b in enumerate(bs):                                       # AI vs rules: different words
            self.outbox(b, [{"text": f"Today sunny, high 5{i % 3}. Tonight clear.", "board": "community",
                             "kind": "auto", "dedupe": key, "at": int(net.t)}], f"o{i}")
        net.run(1200, step=0.5)
        sent = [e for e in net.log if e["kind"] == "text" and "Today sunny" in e["payload"]]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["payload"].endswith(" #" + mb.tag_of(key)))
        self.assertEqual(sent[0]["hop"], 2)
        self.assertGreaterEqual(sent[0]["t"] - T0, 30)                   # automatic notes are in no hurry

    def test_old_and_new_outbox_lines(self):
        net, (b,) = town(1, families=["fam-A"])
        ob = self.outbox(b, [
            {"text": "plain old line", "board": "community"},
            {"text": "old family", "board": "family"},
            "garbage", {"no": "text"}, [1, 2], {"text": ""}, {"text": "   "},
            {"text": "bad fields", "board": "family", "kind": "ROOT", "dedupe": ["x"], "hop_limit": 99, "at": "soon"},
            {"text": "local note", "board": "community", "kind": "auto", "hop_limit": 1},
            {"text": "stale community", "board": "community", "at": int(net.t) - 3600},
            {"text": "danger!", "board": "community", "kind": "danger"},
        ], "compat")
        with quiet():
            ob.pump()
            net.run(1)
        got = {e["payload"]: (e["ch"], e["hop"]) for e in net.log if e["kind"] == "text"}
        self.assertEqual(got, {"danger!": ("public", None), "old family": ("fam-A", None),
                               "bad fields": ("fam-A", None), "plain old line": ("public", None),
                               "local note": ("public", 1)})
        texts = [e["payload"] for e in net.log if e["kind"] == "text"]
        self.assertEqual(texts[0], "danger!")                             # priority over earlier lines
        self.assertEqual(ob.inflight, {})                                 # everything settled
        ob.checkpoint()

    def test_mesh_in_posts_are_never_sent_back_out(self):
        net, (b,) = town(1)
        heard = "Water is available at the church on Elm St until 6 pm"
        with quiet():
            b.bridge.on_text({"fromId": "!0000cafe", "channel": 0, "decoded": {"text": heard}})
        self.assertEqual(b.posts, [("community", heard)])
        self.outbox(b, [{"text": f"Neighbor: {heard}", "board": "community"},
                        {"text": heard, "board": "community"}, {"text": "ok", "board": "community"}], "echo")
        net.run(2)
        self.assertEqual([e["payload"] for e in net.log if e["kind"] == "text"], ["ok"])

    def test_relays_from_several_badgers_land_on_the_board_once(self):
        net, (b,) = town(1)
        text = mb.format_alert(alert())
        old = "NOAA ⚠ Flood Warning · 1 area in CO · until Tue 20:00. More on the Badger's Wi-Fi."
        with quiet():
            for i in range(4):                                           # a collision: four copies
                b.bridge.on_text({"fromId": f"!0000000{i + 1}", "channel": 0, "decoded": {"text": text}})
                b.bridge.on_text({"fromId": f"!0000001{i + 1}", "channel": 0, "decoded": {"text": old}})
        self.assertEqual(b.posts, [("community", text), ("community", old)])

    def test_our_own_relay_heard_back_from_another_badger_is_not_posted(self):
        net, bs = town(1)
        bs[0].hear_alert(alert())
        net.run(10)
        text = net.texts("Tornado")[0]["payload"]
        with quiet():
            bs[0].bridge.on_text({"fromId": "!0000beef", "channel": 0, "decoded": {"text": text}})
        self.assertEqual(bs[0].posts, [])


class Hostile(unittest.TestCase):
    def test_garbled_private_app_packets(self):
        net, (b,) = town(1, families=["fam-A"])
        rnd = random.Random(9)
        payloads = [None, 5, 1.5, [], {}, "", "not base64!!", "A" * 5000, b"", b"HB", b"HB\x01", b"HB\x09\x01",
                    b"HB\x00\x02" + b"x" * 7, b"XX\x01\x02" + b"x" * 7, b"HB\x01\x02" + b"\x00" * 200,
                    base64.b64encode(mb.encode_beacon()).decode(), bytearray(mb.encode_done(b"\x01" * 6))]
        payloads += [bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 80))) for _ in range(300)]
        payloads += [b"HB" + bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 20))) for _ in range(300)]
        froms = [None, "", "!zz", 0, -5, 2 ** 40, "!00000abc", "!00000ABC", 7, True, "x" * 999]
        chans = [None, 0, 1, 7, 8, -1, "1", True, 1.5]
        with quiet():
            for p in payloads:
                for pkt in ({"decoded": {"portnum": "PRIVATE_APP", "payload": p}, "fromId": rnd.choice(froms),
                             "from": rnd.choice(froms), "channel": rnd.choice(chans)},
                            {"decoded": {"portnum": 256, "payload": p}, "fromId": "!0000beef"},
                            {"decoded": None}, {"decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": p}}, None, [], "x"):
                    b.bridge.on_data(pkt)
                    b.bridge.courtesy.heard_control(pkt)
            net.run(5)
        c = b.bridge.courtesy.counts
        self.assertGreater(c["garbled"], 50)                             # on the family channel: not ours
        self.assertGreater(c["ctrl_ignored"], 300)                       # anywhere else: not even looked at
        self.assertEqual(b.bridge.courtesy.peers.table, {})              # nothing forged got through
        json.dumps(b.bridge.status_doc(net.t))                           # the status file still serialises

    def test_forged_peers_are_capped(self):
        # (on the family channel — someone holding the family key; on the public channel beacons are ignored)
        net, (b,) = town(1, families=["fam-A"])
        with quiet():
            for i in range(1000):
                b.bridge.on_data({"fromId": f"!{0x20000000 + i:08x}", "channel": 1,
                                  "decoded": {"portnum": "PRIVATE_APP", "payload": mb.encode_beacon()}})
        self.assertLessEqual(len(b.bridge.courtesy.peers.table), 200)
        self.assertEqual(b.bridge.courtesy.peers.active(), mb.PEER_COUNT_CAP)
        # …and that only stretches back-offs within their caps: a reply still goes out within 10 s
        Plain(net, ASKER).say("!q x")
        net.run(10 + 2)
        self.assertEqual(len(net.replies(ASKER)), 1)

    def test_control_packets_on_the_public_channel_are_ignored(self):
        net, bs = town(3)
        evil = Plain(net, "!00000001")
        a = alert()
        kh = mb.key_hash("alert:" + a["id"])
        for i in range(50):
            evil.data(mb.encode_beacon())
            evil.data(mb.encode_claim(kh, 255))
            evil.data(mb.encode_done(mb.key_hash(f"reply:{ASKER}:{1000 + i}")))
        net.run(5)
        self.assertTrue(all(b.bridge.courtesy.peers.table.keys() <= {o.id for o in bs} for b in bs))
        self.assertTrue(all(b.bridge.courtesy.counts["ctrl_ignored"] >= 150 for b in bs))

    def test_control_flood_from_one_radio_is_ignored(self):
        net, (b,) = town(1, families=["fam-A"])
        n = 0
        with quiet():
            for i in range(100):
                n += b.bridge.courtesy.heard_control({"fromId": "!0000f00d", "channel": 1, "decoded": {
                    "portnum": "PRIVATE_APP", "payload": mb.encode_claim(bytes([i % 256]) * 6, 30)}})
        self.assertEqual(n, mb.CTRL_PER_SENDER[0])

    def test_our_own_packets_and_hostile_texts(self):
        net, (b,) = town(1)
        self.assertFalse(b.bridge.courtesy.heard_control({"fromId": b.id, "channel": 0, "decoded": {
            "portnum": "PRIVATE_APP", "payload": mb.encode_beacon()}}))
        rnd = random.Random(3)
        with quiet():
            for _ in range(300):
                t = "".join(chr(rnd.randint(32, 0x2FFF)) for _ in range(rnd.randint(0, 220)))
                b.bridge.on_text({"fromId": "!0000beef", "channel": rnd.choice([0, 1, 5]), "id": rnd.choice([None, 1, "x", -3]),
                                  "hopStart": rnd.choice([None, 3, 99]), "hopLimit": rnd.choice([None, 1, -1]),
                                  "decoded": {"text": t + rnd.choice(["", " #beef", " #" + mb.tag_of("x")]),
                                              "replyId": rnd.choice([None, 5, "x", True])}})
            net.run(20)


class Privacy(unittest.TestCase):
    def test_control_packets_say_nothing_readable(self):
        # CHANGED (finding 6): no "HB" magic, no fixed sizes. Sealed with a key from the channel's own secret,
        # padded to random lengths like short chat texts; without the key the bytes look random.
        aid = "TOR-1234567-KBOU"
        kh = mb.key_hash("alert:" + aid)
        key = mb.ctrl_key(bytes(range(32)))
        rnd = random.Random(1)
        claim, done, beacon = mb.encode_claim(kh, 30, key, rnd), mb.encode_done(kh, key, rnd), mb.encode_beacon(key, rnd)
        for p in (claim, done, beacon):
            self.assertFalse(p.startswith(b"HB"))
            self.assertTrue(mb.CTRL_MIN <= len(p) <= mb.CTRL_MAX)
            for leak in (b"TOR", b"KBOU", b"alert", b"1234567", kh):
                self.assertNotIn(leak, p)
        self.assertEqual(mb.decode_control(claim, key), {"type": "claim", "kh": kh, "ttl": 30})
        self.assertEqual(mb.decode_control(done, key), {"type": "done", "kh": kh})
        self.assertEqual(mb.decode_control(beacon, key), {"type": "beacon", "proto": "2.0"})
        for p in (claim, done, beacon):                                  # another channel's key / the fallback
            self.assertIsNone(mb.decode_control(p, mb.ctrl_key(b"x" * 32)))
            self.assertIsNone(mb.decode_control(p))
            self.assertIsNone(mb.decode_control(p[:12] + bytes([p[12] ^ 1]) + p[13:], key))   # tampered
        self.assertIsNone(mb.decode_control(b"HB\x01\x01\x01\x00"))     # v1 is gone
        # lengths: many different ones, spread like short texts; two packets of one kind never alike
        lens = [len(mb.encode_beacon(key, rnd)) for _ in range(500)]
        self.assertGreater(len(set(lens)), 20)
        self.assertTrue(20 <= sorted(lens)[250] <= 45, sorted(lens)[250])
        self.assertNotEqual(mb.encode_beacon(key, rnd), mb.encode_beacon(key, rnd))
        self.assertEqual(mb.ctrl_key(b"short"), mb.DEFAULT_CTRL_KEY)     # a weak secret is never used as a key

    def test_beacons_only_on_the_family_channel_by_default(self):
        net, bs = town(2, families=["fam-A", "fam-A"], introduce=False)
        net.run(200)
        beacons = [e for e in net.log if e["kind"] == "data" and mb.decode_control(e["payload"])["type"] == "beacon"]
        self.assertEqual({e["ch"] for e in beacons}, {"fam-A"})
        self.assertEqual(bs[0].bridge.courtesy.peers.active(), 1)        # found each other privately
        net, bs = town(2, introduce=False)                                # no family channel: no beacons at all
        net.run(200)
        self.assertEqual([e for e in net.log if e["kind"] == "data"], [])
        # CHANGED (finding 6): mesh_beacon_public is gone — control packets never go on the public channel
        with quiet():
            net, bs = town(2, families=["fam-A", "fam-B"], introduce=False, cfg={"mesh_beacon_public": True})
        net.run(200)
        chans = {e["ch"] for e in net.log if e["kind"] == "data"}
        self.assertEqual(chans, {"fam-A", "fam-B"})
        net, bs = town(2, families=["fam-A", "fam-A"], introduce=False, cfg={"mesh_beacons": False})
        net.run(3 * 3600, step=5)                                         # beacons off: none, ever
        self.assertEqual([e for e in net.log if e["kind"] == "data"], [])

    def test_beacon_times_are_irregular(self):
        net, bs = town(1, families=["fam-A"], introduce=False, cfg={"mesh_peer_beacon_min": 10})
        net.run(40 * 3600, step=10)
        ts = [e["t"] for e in net.log if e["kind"] == "data"]
        gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
        self.assertGreater(len(gaps), 60)
        self.assertGreaterEqual(min(gaps), 0.2 * 600 - 10)
        self.assertLessEqual(max(gaps), 3 * 600 + 10)
        self.assertGreater(max(gaps) / min(gaps), 5)                       # not "every 10 min ± 20 %"
        self.assertTrue(0.6 * 600 < sum(gaps) / len(gaps) < 1.4 * 600)

    def test_public_peers_are_learned_from_replies(self):
        net, bs = town(3, introduce=False)
        asker = Plain(net, ASKER)
        asker.say("!q one")
        net.run(40)
        # CHANGED: learned from hearing each other's public replies to the same question (no "done" any more)
        self.assertGreaterEqual(sum(b.bridge.courtesy.peers.active() for b in bs), 2)
        net.log.clear()
        asker.say("!q two")                                               # (another radio, no cooldown)
        net.run(40)
        self.assertLessEqual(len(net.replies(ASKER)), 2)

    def test_alert_text_does_not_name_the_badger(self):
        self.assertNotIn("Badger", mb.format_alert(alert()))


class RadioSilence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="silence-")).resolve()
        self.flag = self.tmp / "mesh-silent"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_contract(self):
        now = [T0]
        s = mb.Silence(self.flag, clock=lambda: now[0])
        self.assertFalse(s.active())
        self.flag.write_text("")
        now[0] += 2
        self.assertTrue(s.active())
        self.flag.write_text(json.dumps({"until": T0 + 60, "by": "panel"}))
        now[0] += 2
        self.assertTrue(s.active())
        now[0] += 100
        self.assertFalse(s.active())                                     # "until" passed
        self.flag.write_text("{garbage")
        now[0] += 2
        self.assertTrue(s.active())                                      # unreadable: fail silent
        self.flag.unlink()
        now[0] += 2
        self.assertFalse(s.active())
        self.assertTrue(mb.Silence(None, configured=True).active())

    def test_nothing_automatic_goes_out(self):
        net = Net()
        silence = mb.Silence(self.flag, clock=net.clock)
        self.flag.write_text("")
        b = SimBadger(net, 0, {0: "public", 1: "fam-A"}, silence=silence,
                      cfg={"mesh_beacon_public": True}, tmp=self.tmp)
        o = SimBadger(net, 1, {0: "public", 1: "fam-A"})
        net.introduce()
        asker = Plain(net, ASKER)
        b.hear_alert(alert())
        o.hear_alert(alert())
        asker.say("!help")
        asker.say("!status", dest=b.id)
        ob = ForecastAndPosts.outbox(types.SimpleNamespace(tmp=self.tmp), b, [
            {"text": "forecast", "board": "community", "kind": "auto", "dedupe": "forecast:x"},
            {"text": "community post", "board": "community"},
            {"text": "Ana: home safe", "board": "family", "kind": "family", "member": True},
            {"text": "Supplies: 2 days of water left", "board": "family", "kind": "family"},
            {"text": "sensor danger", "board": "family", "kind": "danger"}], "silent")
        net.run(400)
        mine = [e for e in net.log if e["src"] == b.id]
        self.assertEqual([(e["kind"], e["payload"]) for e in mine], [("text", "Ana: home safe")])
        self.assertTrue(b.bridge.status_doc(net.t)["silent"])
        self.assertTrue(len(b.posts) == 0 or all(bd in ("community", "family") for bd, _ in b.posts))
        self.assertEqual(ob.inflight, {})                                # nothing waits to go out later
        self.assertEqual(len(net.texts("Tornado", "public")), 1)         # the other Badger relays it
        self.assertEqual(net.texts("Tornado", "public")[0]["src"], o.id)
        # silence lifted: nothing from the silent period is sent afterwards
        self.flag.unlink()
        net.log.clear()
        net.run(60)
        self.assertEqual([e for e in net.log if e["src"] == b.id and e["kind"] == "text"], [])

    def test_allow_family_off_means_total_silence(self):
        a = mb.Airtime(lambda *x, **k: True, lambda: T0, silence=mb.Silence(None, configured=True, allow_family=False))
        a.put({"kind": "family", "text": "x", "ch": 1, "member": True})
        self.assertEqual(a.status()["counts"].get("sent_family"), None)
        self.assertEqual(a.status()["counts"]["silenced_family"], 1)


class Position(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pos-")).resolve()
        self.state = self.tmp / "pos.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def iface(self, prec=32, module=True, fail=False):
        writes = []

        def write(i):
            if fail:
                raise RuntimeError("radio busy")
            writes.append(i)
        chans = []
        for p in (prec, 32):
            st = types.SimpleNamespace(module_settings=types.SimpleNamespace(position_precision=p)) if module \
                else types.SimpleNamespace()
            chans.append(types.SimpleNamespace(role=1, settings=st))
        return types.SimpleNamespace(localNode=types.SimpleNamespace(channels=chans, writeChannel=write)), writes

    def test_off_coarse_precise_idempotent(self):
        i, w = self.iface(32)
        r = mb.apply_position_privacy(i, 0, "off", self.state)
        self.assertTrue(r["applied"])
        self.assertEqual((i.localNode.channels[0].settings.module_settings.position_precision, w), (0, [0]))
        self.assertEqual(i.localNode.channels[1].settings.module_settings.position_precision, 32)   # family untouched
        r = mb.apply_position_privacy(i, 0, "off", self.state)
        self.assertEqual((r["applied"], w), (False, [0]))                 # already so: no write
        self.assertEqual(oct(self.state.stat().st_mode & 0o777), "0o640")
        i2, _ = self.iface(0)
        mb.apply_position_privacy(i2, 0, "coarse", self.tmp / "c.json")
        self.assertEqual(i2.localNode.channels[0].settings.module_settings.position_precision, 12)
        i3, _ = self.iface(0)
        mb.apply_position_privacy(i3, 0, "precise", self.tmp / "p.json")
        self.assertEqual(i3.localNode.channels[0].settings.module_settings.position_precision, 32)

    def test_owner_changes_win(self):
        i, w = self.iface(32)
        mb.apply_position_privacy(i, 0, "off", self.state)
        i.localNode.channels[0].settings.module_settings.position_precision = 11   # changed in the app
        r = mb.apply_position_privacy(i, 0, "off", self.state)
        self.assertFalse(r["applied"])
        self.assertIn("owner", r["note"])
        self.assertEqual(i.localNode.channels[0].settings.module_settings.position_precision, 11)
        # CHANGED (owner: "miles is fine, it just can't be 100 yards NNW"): set finer than ~3 km in the app
        # while the Badger says off/coarse → set back; only mesh_public_position = "precise" allows it
        for app in (14, 16, 32):
            i.localNode.channels[0].settings.module_settings.position_precision = app
            r = mb.apply_position_privacy(i, 0, "off", self.state)
            self.assertTrue(r["applied"], r)
            self.assertEqual(i.localNode.channels[0].settings.module_settings.position_precision, 0)
        i.localNode.channels[0].settings.module_settings.position_precision = 13   # ~3 km: the owner's call
        self.assertFalse(mb.apply_position_privacy(i, 0, "off", self.state)["applied"])

    def test_precision_mapping_and_clamp(self):
        # Meshtastic: bits of lat/lon kept; the docs quote 10 ≈ 23.3 km, 11 ≈ 11.7 km, 12 ≈ 5.8 km, 13 ≈ 2.9 km,
        # 14 ≈ 1.5 km, 16 ≈ 364 m, 19 ≈ 45 m (half the box the firmware reports the centre of)
        for bits, km in ((10, 23.3), (11, 11.7), (12, 5.8), (13, 2.9), (14, 1.5), (16, 0.364), (19, 0.045)):
            self.assertAlmostEqual(mb.position_distance_m(bits) / 1000, km, delta=km * 0.03, msg=bits)
        self.assertIsNone(mb.position_distance_m(0))
        self.assertEqual(mb.position_distance_m(32), 0.0)
        coarse = mb.position_distance_m(mb.POSITION_BITS["coarse"])
        self.assertTrue(5000 <= coarse <= 12000, coarse)                   # "miles is fine"
        self.assertGreaterEqual(mb.position_distance_m(mb.POSITION_FINEST), 2800)
        self.assertEqual(mb.position_bits("precise"), (32, ""))
        self.assertEqual(mb.position_bits("off"), (0, ""))
        for v in (14, 16, 20, 31, 32):                                     # numbers never finer than ~3 km
            bits, note = mb.position_bits(v)
            self.assertEqual(bits, mb.POSITION_FINEST)
            self.assertIn("precise", note)
        self.assertEqual(mb.position_bits(11), (11, ""))
        for bad in ("PRECISE", "exact", True, -1, 33, 12.5, None, [32]):
            self.assertIsNone(mb.position_bits(bad)[0], bad)
        i, _ = self.iface(32)
        r = mb.apply_position_privacy(i, 0, 18, self.tmp / "n.json")
        self.assertEqual(i.localNode.channels[0].settings.module_settings.position_precision, 13)
        self.assertIn("precise", r["note"])

    def test_unsupported_or_failing_radio(self):
        for kw in ({"module": False}, {"fail": True}):
            i, _ = self.iface(32, **kw)
            r = mb.apply_position_privacy(i, 0, "off", self.tmp / f"{kw}.json")
            self.assertFalse(r["applied"])
            self.assertTrue(r["note"])
        r = mb.apply_position_privacy(types.SimpleNamespace(), 0, "off", self.state)
        self.assertFalse(r["applied"])
        r = mb.apply_position_privacy(self.iface()[0], 0, "bogus", self.state)
        self.assertIn("unknown", r["note"])
        r = mb.apply_position_privacy(self.iface()[0], 5, "off", self.state)
        self.assertIn("no channel", r["note"])


class Library(unittest.TestCase):
    def test_hop_limit_and_reply_id_only_when_supported(self):
        sent = []

        class Old:
            def sendText(self, text, destinationId=None, channelIndex=0):
                sent.append(("old", text, destinationId, channelIndex))

        class Kw:
            def sendText(self, text, **kw):
                if "hopLimit" in kw:
                    raise TypeError("unexpected keyword argument 'hopLimit'")
                sent.append(("kw", text, kw))

        self.assertTrue(mb.RadioLink(Old()).text("a", "!x", 0, hop_limit=2, reply_id=5))
        link = mb.RadioLink(Kw())
        with quiet():
            self.assertTrue(link.text("b", None, 1, hop_limit=2, reply_id=5))
        self.assertTrue(link.text("c", None, 1, hop_limit=2))
        self.assertEqual(sent, [("old", "a", "!x", 0), ("kw", "b", {"channelIndex": 1}), ("kw", "c", {"channelIndex": 1})])
        self.assertFalse(mb.RadioLink(Old()).data(b"x", 0))               # no sendData: quietly no control traffic
        self.assertFalse(mb.RadioLink(Old()).text("x", None, -1))

    def test_channel_load(self):
        mk = lambda **kw: types.SimpleNamespace(**kw)  # noqa: E731
        self.assertEqual(mb.channel_load(mk(getMyNodeInfo=lambda: {"deviceMetrics": {"channelUtilization": 18.4, "airUtilTx": 2.1}})),
                         (18.4, 2.1))
        i = mk(getMyNodeInfo=lambda: {}, myInfo=mk(my_node_num=7),
               nodesByNum={7: {"deviceMetrics": {"channelUtilization": 33, "airUtilTx": "x"}}})
        self.assertEqual(mb.channel_load(i), (33.0, None))
        for bad in (mk(), mk(getMyNodeInfo=lambda: 1 / 0), mk(getMyNodeInfo=lambda: {"deviceMetrics": {"channelUtilization": 900}})):
            self.assertEqual(mb.channel_load(bad), (None, None))

    def test_config_defaults_and_garbage(self):
        c = mb.courtesy_config({})
        self.assertEqual((c["enabled"], c["beacons"], c["beacon_min"], c["max_util"], c["busy_util"], c["boot_quiet"],
                          c["hop_local"]), (True, True, 45, 25, 40, (30, 120), 2))
        with quiet():
            c = mb.courtesy_config({"mesh_courtesy": "no", "mesh_peer_beacon_min": "x", "mesh_max_util": 90,
                                    "mesh_max_util_busy": 10, "mesh_budget": {"auto": -5, "bogus": 1, "reply": True},
                                    "mesh_boot_quiet_s": 0, "mesh_hop_limit_local": 99})
        self.assertEqual((c["enabled"], c["beacon_min"], c["busy_util"], c["budget"]["auto"], c["budget"]["reply"],
                          c["boot_quiet"], c["hop_local"]), (True, 45, 90, 0, 120, (0, 0), 7))
        self.assertFalse(mb.courtesy_config({"mesh_courtesy": False})["enabled"])

    def test_courtesy_off_is_the_old_behaviour(self):
        net, bs = town(3, cfg={"mesh_courtesy": False})
        a = alert()
        for b in bs:
            b.hear_alert(a)
        Plain(net, ASKER).say("!q x")                                     # (!help: strangers get nothing now)
        net.run(5)
        self.assertEqual(len(net.texts("Tornado")), 3)
        self.assertEqual(len(net.replies(ASKER)), 3)
        self.assertEqual([e for e in net.log if e["kind"] == "data"], [])


class StatusFile(unittest.TestCase):
    def test_status_doc_and_portal_line(self):
        import server
        tmp = Path(tempfile.mkdtemp(prefix="courtesy-status-")).resolve()
        try:
            net, bs = town(4, load=lambda: (18.4, 1.0))
            bs[0].hear_alert(alert())
            net.run(60)
            doc = bs[0].bridge.status_doc(time.time())
            self.assertEqual(doc["peers_active"], 3)
            self.assertEqual(doc["channel_util"], 18.4)
            self.assertIn("airtime", doc)
            self.assertFalse(doc["silent"])
            (tmp / "prepper.toml").write_text(f'[portal]\nstandalone = true\nmesh_peers = "{tmp}/mesh-peers.json"\n'
                                              'ctl = "/nonexistent"\nservices = {}\n')
            server.CFG = server.PortalConfig(tmp / "prepper.toml")
            self.assertIsNone(server.mesh_courtesy_status())                 # no file: no line
            self.assertTrue(mb.atomic_json(tmp / "mesh-peers.json", doc, 0o640))
            self.assertEqual(oct((tmp / "mesh-peers.json").stat().st_mode & 0o777), "0o640")
            st = server.mesh_courtesy_status()
            self.assertEqual(st["line"], "Badgers nearby: 3 · channel busy 18 %")
            self.assertEqual((st["peers"], st["silent"], st["courtesy"]), (3, False, True))
            self.assertIn("mesh", server.status())
            doc.update(silent=True, channel_util=None, updated=time.time() - 3600)
            mb.atomic_json(tmp / "mesh-peers.json", doc)
            self.assertIsNone(server.mesh_courtesy_status())                 # stale: the bridge isn't running
            doc["updated"] = time.time()
            mb.atomic_json(tmp / "mesh-peers.json", doc)
            self.assertEqual(server.mesh_courtesy_status()["line"], "Badgers nearby: 3 · radio silence")
            for junk in ("[]", "{", json.dumps({"updated": "x"}), json.dumps({"updated": time.time(), "peers_active": "9",
                                                                              "channel_util": 1e9, "airtime": [1]})):
                (tmp / "mesh-peers.json").write_text(junk)
                r = server.mesh_courtesy_status()
                self.assertTrue(r is None or r["line"] == "Badgers nearby: 0")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Strangers(unittest.TestCase):
    """Red team finding 6: the Badger must never become a beacon that transmits on a stranger's demand."""

    def cmds(self, clock, **kw):
        sent = []
        c = mb.Commands(lambda t, d=None: sent.append(t), clock=clock, library=lambda q: "LIB", ai=lambda q: "AI",
                        status=lambda: "STATUS", spawn=lambda fn: fn(), access=dict(mb.DEFAULT_ACCESS), **kw)
        return c, sent

    def test_forged_ids_get_at_most_the_hourly_budget(self):
        t = [T0]
        c, sent = self.cmds(lambda: t[0])
        for i in range(600):                                              # a forged ID every 6 s for an hour
            t[0] = T0 + i * 6
            c.handle(f"!q water {i}", f"!{i:08x}", trusted=False)
        self.assertEqual(len(sent), mb.STRANGER_BUDGET[0])
        t[0] = T0 + 3600 + 7                                              # the next hour: room again
        c.handle("!q water", "!feedf00d", trusted=False)
        self.assertEqual(len(sent), mb.STRANGER_BUDGET[0] + 1)

    def test_one_radio_gets_two_answers_an_hour(self):
        t = [T0]
        c, sent = self.cmds(lambda: t[0])
        for i in range(20):
            t[0] = T0 + i * 60
            c.handle(f"!q x{i}", "!0000abcd", trusted=False)
        self.assertEqual(len(sent), mb.STRANGER_PER_RADIO[0])
        for i in range(20):                                               # family radios: only the cooldown
            t[0] = T0 + i * 60
            c.handle(f"!q x{i}", "!0000f00d", trusted=True)
        self.assertEqual(len(sent), mb.STRANGER_PER_RADIO[0] + 20)

    def test_budgets_are_configurable(self):
        self.assertEqual(mb.stranger_budgets({}), ((6, 3600.0), (2, 3600.0)))
        with quiet():
            self.assertEqual(mb.stranger_budgets({"mesh_stranger_replies_per_hour": 0,
                                                  "mesh_stranger_replies_per_radio_hour": "x"}), ((0, 3600.0), (2, 3600.0)))
            self.assertEqual(mb.stranger_budgets({"mesh_stranger_replies_per_hour": 10 ** 9})[0][0], 60)
        t = [T0]
        c, sent = self.cmds(lambda: t[0], public_budget=(0, 3600.0))      # 0: strangers never get an answer
        c.handle("!q water", "!0000abcd", trusted=False)
        self.assertEqual(sent, [])
        self.assertFalse(c.would_answer("!q water", False, "!0000abcd"))

    def test_strangers_never_get_refusals_usage_or_busy_lines(self):
        t = [T0]
        levels = {"!help": "community", "!q": "community", "!ai": "community", "!status": "off"}
        sent = []
        jobs = []
        c = mb.Commands(lambda t_, d=None: sent.append(t_), clock=lambda: t[0], ai=lambda q: "AI", spawn=jobs.append,
                        access=lambda: levels, cooldown=0, public_budget=(100, 3600.0), per_radio_budget=(100, 3600.0))
        for i, cmd in enumerate(("!status", "!q", "!ai", "!ai one", "!ai two")):
            c.handle(cmd, f"!0000000{i}", trusted=False)
        self.assertEqual(sent, [])                                        # "one" took the worker silently
        jobs[0]()
        self.assertEqual(sent, ["AI"])
        for lv in ("family", "off", "ignore"):
            levels["!q"] = lv
            c.handle("!q x", "!00000009", trusted=False)
        levels["!q"] = "ignore"
        c.handle("!q x", "!00000010", trusted=True)                      # ignore: not even family hears back
        self.assertEqual(sent, ["AI"])

    def test_no_over_the_air_text_names_the_device(self):
        for access in itertools.product(mb.LEVELS, repeat=4):
            acc = dict(zip(mb.COMMAND_WORDS, access, strict=True))
            for trusted in (False, True):
                self.assertNotIn("Badger", mb.help_text(acc, trusted))
        self.assertNotIn("Badger", mb.format_status({"power": {"percent": 50}}) + mb.format_alert(alert()))
        said = []
        c = mb.Commands(lambda t, d=None: None, clock=lambda: T0, access={"!q": "off", "!ai": "family"},
                        cooldown=0, spawn=lambda f: f(), ai=lambda q: "AI")
        for cmd in ("!q x", "!ai", "!help"):
            c.handle(cmd, "!00000001", trusted=True, reply=said.append)
        self.assertTrue(said)
        self.assertFalse([x for x in said if "Badger" in x], said)


class ControlChannels(unittest.TestCase):
    def test_build_bridge_control_channels(self):
        link = mb.RadioLink(types.SimpleNamespace())
        cmds = mb.Commands(lambda *a: None)
        b = mb.build_bridge(link, cfg={}, family_ch=1, public_ch=0, my_id="!00000001", commands=cmds,
                            members=Path("/nonexistent"))
        self.assertEqual((b.courtesy.ctrl_chs, b.courtesy.beacon_chs), ({1}, [1]))
        b = mb.build_bridge(link, cfg={"mesh_circle_channel": 3, "mesh_beacons": False}, family_ch=1, public_ch=0,
                            my_id="!00000001", commands=cmds, members=Path("/nonexistent"))
        self.assertEqual((b.courtesy.ctrl_chs, b.courtesy.beacon_chs), ({1, 3}, []))
        with quiet():
            for bad in (0, "3", True, 9):                                 # never the public channel / garbage
                b = mb.build_bridge(link, cfg={"mesh_circle_channel": bad}, family_ch=-1, public_ch=0,
                                    my_id="!00000001", commands=cmds, members=Path("/nonexistent"))
                self.assertEqual(b.courtesy.ctrl_chs, set(), bad)
        b = mb.build_bridge(link, cfg={}, family_ch=2, public_ch=2, my_id="!00000001", commands=cmds,
                            members=Path("/nonexistent"), ctrl_chs=[2, 0])
        self.assertEqual(b.courtesy.ctrl_chs, set())

    def test_channel_secret(self):
        def iface(psk):
            ch = types.SimpleNamespace(settings=types.SimpleNamespace(psk=psk))
            return types.SimpleNamespace(localNode=types.SimpleNamespace(channels=[ch, ch]))
        self.assertEqual(mb.channel_secret(iface(b"\x07" * 32), 1), ("ok", b"\x07" * 32))
        for weak in (b"", b"\x01", b"\x02" * 15):                         # none / Meshtastic's default keys
            self.assertEqual(mb.channel_secret(iface(weak), 1), ("weak", None))
        self.assertEqual(mb.channel_secret(types.SimpleNamespace(), 1), ("unknown", None))
        self.assertEqual(mb.channel_secret(iface(b"x" * 32), 5), ("unknown", None))

    def test_badgers_with_different_family_keys_do_not_understand_each_other(self):
        net, bs = town(2, families=["fam-A", "fam-A"], introduce=False)
        bs[1].bridge.courtesy.set_ctrl_key(1, mb.ctrl_key(b"k" * 32))     # bs[1] got a new family key
        net.run(200)
        self.assertEqual(bs[1].bridge.courtesy.peers.active(), 0)
        self.assertEqual(bs[0].bridge.courtesy.peers.active(), 0)
        self.assertGreater(bs[0].bridge.courtesy.counts["garbled"] + bs[1].bridge.courtesy.counts["garbled"], 0)
        bs[0].bridge.courtesy.set_ctrl_key(1, mb.ctrl_key(b"k" * 32))     # …and now the other one too
        bs[0].bridge.courtesy.next_beacon = bs[1].bridge.courtesy.next_beacon = net.t
        net.run(5)
        self.assertEqual(bs[0].bridge.courtesy.peers.active(), 1)
        bs[0].bridge.courtesy.set_ctrl_key(1, None)                       # weak key now: no control traffic
        self.assertEqual((bs[0].bridge.courtesy.ctrl_chs, bs[0].bridge.courtesy.beacon_chs), (set(), []))

    def test_danger_from_the_outbox_is_cancelled_only_by_an_exact_copy(self):
        tmp = Path(tempfile.mkdtemp(prefix="danger-")).resolve()
        try:
            net, bs = town(3)
            evil = Plain(net, "!00000001")
            line = {"text": "Flood at the river bridge, avoid Elm St", "board": "community", "kind": "danger",
                    "dedupe": "danger:river:1"}
            for i, b in enumerate(bs):
                ForecastAndPosts.outbox(types.SimpleNamespace(tmp=tmp), b, [line], f"d{i}")
            evil.say("All clear at the river bridge #" + mb.tag_of("danger:river:1"))
            net.run(120)
            self.assertEqual(len([e for e in net.texts("Flood at the river") if e["src"] != evil.id]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FamilyKeyExposure(unittest.TestCase):
    def test_removed_member_before_or_after_the_last_rekey(self):
        tmp = Path(tempfile.mkdtemp(prefix="exposed-")).resolve()
        try:
            mem, rot = tmp / "members.json", tmp / "rot.json"
            mem.write_text(json.dumps({"members": {"m_mom": {"name": "Mom", "radios": ["!11111111"]},
                                                   "m_ex": {"name": "Ex", "removed": T0, "radios": []}}}))
            self.assertTrue(mb.family_key_exposed(mem, rot))              # no key change on record
            rot.write_text(json.dumps({"ok": True, "at": T0 - 5}))
            self.assertTrue(mb.family_key_exposed(mem, rot))              # re-keyed BEFORE the removal
            rot.write_text(json.dumps({"ok": False, "at": T0 + 5}))
            self.assertTrue(mb.family_key_exposed(mem, rot))              # a failed attempt doesn't count
            rot.write_text(json.dumps({"ok": True, "at": T0 + 5}))
            self.assertFalse(mb.family_key_exposed(mem, rot))
            for junk in ("[]", "{", json.dumps({"members": []})):
                mem.write_text(junk)
                self.assertFalse(mb.family_key_exposed(mem, rot))
            mem.write_text(json.dumps({"members": {"m_ex": {"name": "Ex", "removed": True}}}))
            self.assertFalse(mb.family_key_exposed(mem, rot))             # unknown time, but a re-key on record
            self.assertTrue(mb.family_key_exposed(mem, None))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_family_commands_are_off_while_exposed(self):
        tmp = Path(tempfile.mkdtemp(prefix="exposed-")).resolve()
        try:
            (tmp / "members.json").write_text(json.dumps({"members": {
                "m_mom": {"name": "Mom", "radios": ["!11111111"]}, "m_ex": {"name": "Ex", "removed": T0}}}))
            net = Net()
            b = SimBadger(net, 0, {0: "public", 1: "fam-A"}, tmp=tmp)
            b.bridge.rotation = tmp / "rot.json"
            mom = Plain(net, "!11111111", {0: "public", 1: "fam-A"})
            mom.say("!status", ch=1, dest=b.id)
            mom.say("home safe", ch=1)
            net.run(5)
            self.assertEqual(net.replies("!11111111"), [])
            self.assertEqual(b.posts, [("family", "home safe")])
            (tmp / "rot.json").write_text(json.dumps({"ok": True, "at": T0 + 1}))   # the family was re-keyed
            net.run(6)
            mom.say("!status", ch=1, dest=b.id)
            net.run(5)
            self.assertEqual([e["payload"] for e in net.replies("!11111111")], ["STATUS"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Pairing(unittest.TestCase):
    """Radio pairing confirmation: an admin can't bind a radio to a member without a message from that radio."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pair-")).resolve()
        self.pair = self.tmp / "mesh-pair.json"
        self.t = [T0]
        self.sent, self.posts = [], []
        self.bridge = mb.Bridge(1, 0, lambda *a, **k: self.sent.append(a) or True,
                                mb.Commands(lambda *a, **k: self.sent.append(a), clock=lambda: self.t[0]),
                                self.tmp / "members.json", None, "!00000001", lambda: self.t[0],
                                board=lambda *a: self.posts.append(a), pair_file=self.pair)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rx(self, text, sender="!A1B2C3D4", ch=1, to=None):
        pkt = {"fromId": sender, "channel": ch, "decoded": {"text": text}}
        if to:
            pkt["toId"] = to
        with quiet():
            self.bridge.on_text(pkt)

    def confirms(self):
        return json.loads(self.pair.read_text())["confirms"] if self.pair.exists() else []

    def test_valid_confirm_is_written_owner_only_and_nothing_goes_out(self):
        self.rx("!pair k7qx-2m")
        self.assertEqual(self.confirms(), [{"radio": "!a1b2c3d4", "code": "K7QX2M", "at": T0}])
        self.assertEqual(json.loads(self.pair.read_text())["v"], 1)
        self.assertEqual(oct(self.pair.stat().st_mode & 0o777), "0o600")
        self.assertEqual((self.sent, self.posts), ([], []))
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["mesh-pair.json"])   # no temp file left behind

    def test_wrong_channel_dm_and_malformed_are_dropped_silently(self):
        self.rx("!pair ABCD12", ch=0)                                     # public channel
        self.rx("!pair ABCD12", ch=3)                                     # some other channel
        self.rx("!pair ABCD12", to="!00000001")                           # a DM, even on the family channel
        for bad in ("!pair", "!pair abc", "!pair " + "A" * 17, "!pair AB CD", "!pair ABCD;rm", "!pairABCD",
                    "!pair ABCD\n!q x", "!pair ÄBCDE", "!pair ----", "!pair --AB--", "!PAIR\tabcd x"):
            self.rx(bad)
        self.rx("!pair ABCD12", sender="?")                               # no usable radio id
        self.rx("!pair ABCD12", sender="!zzzzzzzz")
        self.assertEqual(self.confirms(), [])
        self.assertEqual((self.sent, self.posts), ([], []))
        self.rx("  !PAIR\tABCDE12345XY  ")                                # case and spacing are fine
        self.assertEqual([c["code"] for c in self.confirms()], ["ABCDE12345XY"])

    def test_rate_limit_keep_and_max_age(self):
        for i in range(8):
            self.t[0] = T0 + i
            self.rx(f"!pair CODE{i:02d}")
        self.assertEqual(len(self.confirms()), mb.PAIR_PER_RADIO[0])      # 5 per radio per 10 min
        self.t[0] = T0 + 601
        self.rx("!pair LATER1")
        self.assertEqual(self.confirms()[-1]["code"], "LATER1")
        for i in range(40):                                               # many radios: the total cap
            self.rx(f"!pair MANY{i:02d}", sender=f"!{0x30000000 + i:08x}")
        self.assertLessEqual(len(self.confirms()), 20)
        self.t[0] = T0 + 601 + 7201                                       # everything earlier is too old now
        self.rx("!pair FRESH1", sender="!0000beef")
        self.assertEqual([c["code"] for c in self.confirms()], ["FRESH1"])

    def test_record_pair_confirm_hostile_inputs_and_files(self):
        f = self.pair
        for radio, code in ((None, "ABCD"), ("!a1b2c3d4", None), (5, "ABCD"), ("!a1b2c3d4", "AB"),
                            ("a1b2c3d4", "ABCD"), ("!a1b2c3d4", "AB CD"), ("!a1b2c3d4", "A" * 13)):
            self.assertFalse(mb.record_pair_confirm(f, radio, code, T0))
        self.assertFalse(mb.record_pair_confirm(f, "!a1b2c3d4", "ABCD", float("nan")))
        self.assertFalse(f.exists())
        for junk in ("{", "[]", json.dumps({"confirms": [1, None, {"radio": "x", "code": "ABCD", "at": T0},
                                                         {"radio": "!a1b2c3d4", "code": "ab cd", "at": T0},
                                                         {"radio": "!a1b2c3d4", "code": "OLD1", "at": T0 - 9999}]}),
                     "x" * 70_000):
            f.write_text(junk)
            self.assertTrue(mb.record_pair_confirm(f, "!A1B2C3D4", "ok12", T0))
            self.assertEqual(json.loads(f.read_text()), {"v": 1, "confirms": [{"radio": "!a1b2c3d4", "code": "OK12",
                                                                              "at": T0}]})
        link = self.tmp / "elsewhere.json"                               # a planted symlink is replaced, not followed
        link.write_text("keep")
        f.unlink()
        f.symlink_to(link)
        self.assertTrue(mb.record_pair_confirm(f, "!a1b2c3d4", "ABCD", T0))
        self.assertEqual(link.read_text(), "keep")
        self.assertFalse(f.is_symlink())
        self.assertFalse(mb.record_pair_confirm(self.tmp / "nodir" / "x" / "\0bad", "!a1b2c3d4", "ABCD", T0))
        self.assertFalse(mb.record_pair_confirm(None, "!a1b2c3d4", "ABCD", T0))

    def test_refused_while_a_removed_member_may_hold_the_family_key(self):
        (self.tmp / "members.json").write_text(json.dumps({"members": {
            "m_mom": {"name": "Mom", "radios": ["!11111111"]}, "m_ex": {"name": "Ex", "removed": T0 - 60}}}))
        rot = self.tmp / "rot.json"
        self.bridge.rotation = rot
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.bridge.on_text({"fromId": "!a1b2c3d4", "channel": 1, "decoded": {"text": "!pair ABCD12"}})
        self.assertIn("pairing refused until the family channel is re-keyed", err.getvalue())
        self.assertFalse(self.pair.exists())
        self.assertEqual((self.sent, self.posts), ([], []))
        rot.write_text(json.dumps({"ok": True, "at": T0}))                # re-keyed after the removal
        self.t[0] += 6                                                    # (the exposure check is cached 5 s)
        self.rx("!pair ABCD12")
        self.assertEqual([c["code"] for c in self.confirms()], ["ABCD12"])

    def test_off_without_a_pair_file_and_for_removed_radios(self):
        self.bridge.pair_file = None
        self.rx("!pair ABCD12")
        self.assertFalse(self.pair.exists())
        self.bridge.pair_file = self.pair
        (self.tmp / "members.json").write_text(json.dumps({"members": {"m_ex": {"name": "Ex", "removed": T0,
                                                                                "removed_radios": ["!a1b2c3d4"]}}}))
        self.rx("!pair ABCD12")
        self.assertFalse(self.pair.exists())
        self.assertEqual((self.sent, self.posts), ([], []))


class FeatureKeys(unittest.TestCase):
    def test_forecast_key_is_shared_per_transmitter_and_half_day(self):
        sys.path.insert(0, str(REPO / "software" / "portal" / "features"))
        sys.path.insert(0, str(REPO / "software" / "speech"))
        import forecast
        t = time.mktime((2026, 11, 17, 9, 0, 0, 0, 0, -1))
        k = forecast.mesh_key({"freq": 162.55, "at": t})
        self.assertEqual(k, "forecast:162.550:2026-11-17:AM")
        self.assertEqual(forecast.mesh_key({"freq": 162.550, "at": t + 3600}), k)
        self.assertNotEqual(forecast.mesh_key({"freq": 162.55, "at": t + 4 * 3600}), k)       # PM
        self.assertNotEqual(forecast.mesh_key({"freq": 162.40, "at": t}), k)
        self.assertTrue(forecast.mesh_key({}).startswith("forecast:?:"))
        calls = []
        old_ctx = types.SimpleNamespace(mesh_send=lambda text, board: calls.append((text, board)) or True)
        self.assertTrue(forecast._mesh_send(old_ctx, "x", k))                 # an older portal still works
        self.assertEqual(calls, [("x", "community")])


if __name__ == "__main__":
    unittest.main(verbosity=1)
