#!/usr/bin/env python3
"""
Red team — data at rest. Adversary: a raider who seizes the Badger, pulls the NVMe/microSD and
reads every file (or a thief who resells the box). We drive the real portal through a normal
family day, then grep the data folder the way a forensic reader would.

The vault passes (it is encrypted). Everything else is plaintext today: each failing test is a
finding (`# FINDING:`). Run: python3.11 tests/test_redteam_rest.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "redteam"))
from harness import OPENSSL, PHONE_HTTP, PortalCase, server  # noqa: E402


def grep(folder: Path, needle: str) -> list[str]:
    n = needle.encode()
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and n in p.read_bytes())


class SeizedBadger(PortalCase):
    FEATURES = ("supplies.py", "vault.py", "_vault_crypto.py")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        self = cls("run")
        # A normal family day.
        cls.mom = self.join("Mom Garcia")
        self.join("Lucia Garcia (age 7)")
        self.req("POST", "/api/board", {"board": "family", "text": "Leaving for the clinic 14:00, house empty till 18:00"},
                 member=cls.mom, headers=PHONE_HTTP)
        server._last_post.clear()
        self.req("POST", "/api/supplies/household", {"people": 4, "pets": 1}, member=cls.mom, headers=PHONE_HTTP)
        self.req("POST", "/api/supplies/item", {"name": "Insulin pens", "category": "medical", "qty": 12, "unit": "pens",
                                                "location": "behind the false wall in the cellar"},
                 member=cls.mom, headers=PHONE_HTTP)
        self.req("POST", "/api/supplies/item", {"name": "Drinking water", "category": "water", "qty": 400, "unit": "L",
                                                "location": "barn loft"}, member=cls.mom, headers=PHONE_HTTP)
        # The radio side writes these (meshbridge): the family channel link, Badgers heard, radio positions.
        (cls.tmp / "mesh-channel.json").write_text(json.dumps({"url": "https://meshtastic.org/e/#CiISIFAMILYPSK", "at": 1}))
        (cls.tmp / "mesh-peers.json").write_text(json.dumps({"updated": 1, "peers": [
            {"id": "!a1b2c3d4", "last": 1, "via": "relay"}, {"id": "!0e0e0e0e", "last": 1, "via": "beacon"}]}))
        (cls.tmp / "mesh-nodes.json").write_text(json.dumps({"updated": 1, "nodes": [
            {"id": "!a1b2c3d4", "name": "Hendersons", "lat": 39.7512, "lon": -104.9871, "heard": 1},
            {"id": "!11111111", "name": "Mom's radio", "lat": 39.7392, "lon": -104.9903, "heard": 1, "self": False}]}))
        if OPENSSL:
            self.req("POST", "/api/vault/setup", {"passphrase": "purple tractor sings loudly"}, member=cls.mom,
                     headers=PHONE_HTTP)
            self.req("POST", "/api/vault/note", {"text": "Passport X1234567", "name": "Mom passport"},
                     member=cls.mom, headers=PHONE_HTTP)

    def test_vault_contents_are_encrypted(self):
        # Holds: nothing of the vault note is readable on disk.
        if not OPENSSL:
            self.skipTest("needs openssl")
        self.assertEqual(grep(self.tmp, "X1234567"), [])
        self.assertEqual(grep(self.tmp, "Mom passport"), [])

    def test_supplies_and_cache_locations_are_plaintext(self):
        # FINDING: features-data/supplies/*.json holds every item, quantity and its hiding place, the
        # household size, and a usage log — readable by anyone holding the disk. "The cache map."
        self.assertEqual(grep(self.tmp, "false wall in the cellar"), [], "hiding place readable at rest")

    def test_family_board_and_schedules_are_plaintext(self):
        # FINDING: family-board.jsonl (2 MB of history) is plaintext: when the house is empty, who goes where.
        self.assertEqual(grep(self.tmp, "house empty till 18:00"), [])

    def test_member_names_children_and_presence_times_are_plaintext(self):
        # FINDING: members.json: every family member's name (here a child's age in it), device labels, radio
        # IDs, and per-device last_seen — when each person's phone was last home.
        hits = grep(self.tmp, "Lucia Garcia")
        self.assertEqual(hits, [], f"family roster readable at rest: {hits}")
        mem = json.loads((self.tmp / "members.json").read_text())
        seen = [d.get("last_seen") for m in mem["members"].values() for d in m.get("devices", [])]
        self.assertFalse(any(seen), "per-device presence timestamps kept")

    def test_family_radio_key_is_plaintext(self):
        # FINDING: mesh-channel.json (0600 prepper) holds the family channel link = the PSK. Also on the radio
        # itself (all channel keys, NodeDB, PKI key) — a pulled radio needs no Pi at all.
        self.assertEqual(grep(self.tmp, "FAMILYPSK"), [])

    def test_seized_badger_maps_other_badger_households(self):
        # FINDING: mesh-peers.json lists the node IDs of every Badger heard in the last week; mesh-nodes.json
        # has the last position of every radio in the NodeDB. Joined, the raider gets coordinates of other
        # prepared households to visit next.
        peers = {p["id"] for p in json.loads((self.tmp / "mesh-peers.json").read_text())["peers"]}
        nodes = json.loads((self.tmp / "mesh-nodes.json").read_text())["nodes"]
        targets = [(n["name"], n["lat"], n["lon"]) for n in nodes if n["id"] in peers]
        self.assertEqual(targets, [], f"other Badger households located from a seized unit: {targets}")


if __name__ == "__main__":
    unittest.main()
