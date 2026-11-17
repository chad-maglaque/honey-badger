#!/usr/bin/env python3
"""
Red team — the radio side (Meshtastic LoRa). Adversaries: a roving band listening and taking
bearings, and a hostile radio on the public mesh (spoofing, forged control packets, forged relays).
Everything runs against meshbridge's pure logic with fake clocks/radios; nothing transmits.

Each failing test is a finding (`# FINDING:`). Run: python3.11 tests/test_redteam_rf.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "meshbridge"))
sys.path.insert(0, str(REPO / "software" / "sensors"))
os.environ["PORTAL_QUIET"] = "1"
import meshbridge as mb  # noqa: E402

T0 = 1_800_000_000.0
STRANGER = "!0bad0bad"


def quiet():
    return contextlib.redirect_stderr(io.StringIO())


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def alert(aid="TOR-3211845-KBOU/NWS", event="Tornado Warning"):
    return {"id": aid, "event": event, "areas": "Denver CO", "expires": "Tue 18:30", "expires_ts": T0 + 3600, "test": False}


class FindTheBadger(unittest.TestCase):
    """A roving band wants to know which radio on the mesh is a prepared household's server."""

    def commands(self, access, sent):
        return mb.Commands(lambda t, dest=None: sent.append((dest, t)), clock=Clock(), access=access,
                           library=lambda q: "Water: boil 1 min", status=lambda: "Badger: battery 80%")

    def test_help_reply_names_the_badger_to_any_stranger(self):
        # FINDING: "!help" is answered for anyone (DEFAULT_ACCESS["!help"] = "community", and DMs skip the
        # courtesy entirely: Bridge._command answers a DM "at once"). The reply starts "Honey Badger:".
        # A scanner DMs "!help" to every node in its NodeDB and gets a self-identifying answer from each
        # Badger, then takes a bearing on the reply.
        # (Adjusted with the fix: the owner's rule is that a stranger's "!help" is ignored by default, so the
        # original `assertTrue(sent)` no longer holds. Checked instead, and more strictly: by default nothing
        # at all goes out; with !help opened to the community the answer still doesn't name the device.)
        sent = []
        self.commands(dict(mb.DEFAULT_ACCESS), sent).handle("!help", STRANGER, trusted=False)
        self.assertEqual(sent, [], f"stranger got: {sent!r}")
        opened = dict(mb.DEFAULT_ACCESS, **{"!help": "community"})
        self.commands(opened, sent).handle("!help", STRANGER, trusted=False)
        self.assertTrue(sent)
        self.assertNotIn("Badger", sent[0][1], f"stranger got: {sent[0][1]!r}")

    def test_no_setting_makes_a_badger_ignore_strangers(self):
        # FINDING: even with every command "off", a stranger's "!q"/"!ai" gets "... is turned off on this
        # Badger" — a transmission that names the device. Only full radio silence stops it. There is no
        # "ignore strangers silently" level.
        sent = []
        off = {"!help": "off", "!q": "off", "!ai": "off", "!status": "off"}
        self.commands(off, sent).handle("!q water", STRANGER, trusted=False)
        self.assertEqual(sent, [], f"Badger answered a stranger although every command is off: {sent}")

    def test_family_only_refusal_still_identifies(self):
        # FINDING: "!status" (family-only by default) from a stranger → "!status is for family radios" —
        # tells the stranger there is a family behind this node.
        sent = []
        self.commands(dict(mb.DEFAULT_ACCESS), sent).handle("!status", STRANGER, trusted=False)
        self.assertEqual(sent, [], f"refusal transmitted: {sent}")

    def test_stranger_can_make_the_badger_transmit_on_demand(self):
        # FINDING: forged sender IDs get 20 replies / 10 min (public_budget) = up to ~2,900 transmissions a
        # day from the household's node on request: a beacon for direction finding, and airtime/battery drain.
        sent = []
        clock = Clock()
        c = mb.Commands(lambda t, dest=None: sent.append(t), clock=clock, access=dict(mb.DEFAULT_ACCESS))
        for i in range(60):                               # one hour, a forged ID every minute
            clock.t = T0 + i * 60
            c.handle("!help", f"!{i:08x}", trusted=False)
        self.assertLess(len(sent), 5, f"{len(sent)} transmissions in an hour, all on a stranger's request")


class ControlPacketFingerprint(unittest.TestCase):
    def test_control_packets_carry_a_plaintext_magic(self):
        # FINDING: every courtesy packet (claim/done on the PUBLIC channel, beacons on the family channel)
        # starts with the ASCII magic "HB" on PRIVATE_APP port 256. On the public channel (default key) any
        # listener decodes it: "this node is a Honey Badger". docs/MESH.md: "carry nothing identifying".
        for pkt in (mb.encode_beacon(), mb.encode_claim(mb.key_hash("x"), 30), mb.encode_done(mb.key_hash("x"))):
            self.assertFalse(pkt.startswith(b"HB"), f"control packet {pkt!r} announces a Badger")

    def test_control_packets_have_fixed_tell_tale_sizes(self):
        # FINDING: beacon 6 B, done 10 B, claim 11 B. Meshtastic encrypts with AES-CTR (length-preserving),
        # so even on the ENCRYPTED family channel a listener sees a 6-byte PRIVATE_APP-sized packet from the
        # same stationary node every ~45 min ± 20 % — a Badger beacon, its timing, and a DF target.
        sizes = {len(mb.encode_beacon()), len(mb.encode_claim(b"\0" * 6, 30)), len(mb.encode_done(b"\0" * 6))}
        self.assertNotEqual(sizes, {6, 10, 11}, "fixed, unpadded control-packet sizes")


class SilenceTheAlerts(unittest.TestCase):
    """A hostile radio wants the neighbourhood not to hear a real NOAA warning from the Badgers."""

    def courtesy(self, clock, peers=1):
        c = mb.Courtesy(lambda payload, ch: None, clock, random.Random(1), my_id="!00000001", my_num=1)
        for i in range(peers):
            c.peers.heard(f"!0000aa{i:02x}", 0xAA00 + i, "beacon")
        return c

    def test_forged_relay_with_altered_text_cancels_the_real_relay(self):
        # FINDING: alert ids are deterministic from the public SAME header (alertd: EEE-issue-sender) and the
        # tag salt is a published constant, so a hostile radio that decodes the same NOAA broadcast (a $25
        # SDR) can send "NOAA ⚠ Tornado Warning · CANCELLED, all clear #<tag>" first. alert_matcher only checks
        # prefix + event name + tag, so every Badger in range marks the alert "already relayed" and never sends
        # the real one on that channel. docs/MESH.md: "the worst a forger can do is … delay an alert by a minute".
        clock = Clock()
        c = self.courtesy(clock)
        a = alert()
        fired = []
        with quiet():
            res = c.submit(mb.alert_key(a["id"]), "alert", 0, lambda: fired.append(1), match=mb.alert_matcher(a))
        self.assertEqual(res, "pending")
        tag = mb.tag_of(mb.alert_key(a["id"]))           # what the attacker computes
        forged = f"NOAA ⚠ Tornado Warning · CANCELLED by NWS, all clear, stay home #{tag}"
        with quiet():
            c.heard_text(forged, "!0bad0bad", 0, {"from": 0x0BAD0BAD})
            for _ in range(200):
                clock.t += 1
                c.tick()
        self.assertTrue(fired, "a forged 'all clear' with the right tag silenced the real alert relay")

    def test_forgery_sent_in_advance_suppresses_even_a_lone_badger(self):
        # FINDING: submit() checks the 6-hour relay history before anything else (_heard_already), so a
        # forged relay heard BEFORE the Badger decodes the alert suppresses it outright — even with no other
        # Badger around (no claim, no wait).
        clock = Clock()
        c = self.courtesy(clock, peers=0)
        a = alert()
        tag = mb.tag_of(mb.alert_key(a["id"]))
        with quiet():
            c.heard_text(f"NOAA ⚠ Tornado Warning · test only, disregard #{tag}", "!0bad0bad", 0, {"from": 1})
            fired = []
            res = c.submit(mb.alert_key(a["id"]), "alert", 0, lambda: fired.append(1), match=mb.alert_matcher(a))
        self.assertNotEqual(res, "suppressed", "a pre-seeded forged relay suppressed the alert")


class FamilyChannelImpersonation(unittest.TestCase):
    """Anyone holding the family PSK (an ex-member before rotation, a pulled radio, a phone with the app)."""

    def test_psk_holder_posts_as_any_member_by_forging_fromId(self):
        # FINDING (known, documented): on the family channel the post is labelled with the member that owns
        # the claimed fromId. A removed member's own radio is ignored — but they simply claim Mom's radio ID.
        tmp = Path(tempfile.mkdtemp(prefix="rt-mesh-"))
        try:
            members = tmp / "members.json"
            members.write_text(json.dumps({"members": {
                "m_mom": {"name": "Mom", "radios": ["!11111111"]},
                "m_ex": {"name": "Ex", "removed": True, "radios": ["!22222222"]}}, "invites": {}}))
            posts = []
            b = mb.Bridge(1, 0, lambda *a, **k: True, mb.Commands(lambda *a, **k: None), members, None,
                          "!00000001", Clock(), board=lambda name, text, board, radio, member: posts.append((name, member)))
            with quiet():
                b.on_text({"fromId": "!22222222", "channel": 1, "decoded": {"text": "it's me, the ex"}})
                b.on_text({"fromId": "!11111111", "channel": 1, "decoded": {"text": "Kids: go to the old mill now"}})
            self.assertNotIn(("Mom", "m_mom"), posts, "the removed member posted as Mom with a spoofed radio ID")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BadgerSaysBadger(unittest.TestCase):
    """Texts the Badger puts on the PUBLIC channel by default that name it."""

    def test_sensor_alerts_go_public_with_a_badger_prefix(self):
        # FINDING: sensord sends flood/radiation alerts (DEFAULT_RULES mesh=True) to mesh_board = "community"
        # (default) as "Badger sensors: ⚠ Basement floor: water detected" — names the device and describes
        # the house, in clear, on LongFast.
        import sensord
        import sensorlib as sl
        tmp = Path(tempfile.mkdtemp(prefix="rt-sens-"))
        try:
            eff = sl.effective_settings({}, None)
            ns = types.SimpleNamespace(mesh_sent=[], mesh_queue=[{"text": "⚠ Basement floor: water detected",
                                                                  "board": eff["mesh_board"], "urgent": True}],
                                       mesh_per_hour=4, mesh_gap=600, mesh_gap_danger=120, outbox=tmp / "out.jsonl")
            sensord.Sensord.flush_mesh(ns, T0)
            line = json.loads((tmp / "out.jsonl").read_text().splitlines()[0])
            self.assertFalse(line["board"] == "community" and "Badger" in line["text"],
                             f"public-channel text by default: {line}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_person_announcement_names_the_badger(self):
        # FINDING (low): approved missing-person announcements end " — see Badger board" (missing.py
        # MESH_TAIL) on the public channel.
        src = (REPO / "software" / "portal" / "features" / "missing.py").read_text()
        self.assertNotIn('MESH_TAIL = " — see Badger board"', src)


if __name__ == "__main__":
    unittest.main()
