#!/usr/bin/env python3
"""
Family radio messages while the Badger is locked (docs/SECURITY.md → "Sealed records", docs/MESH.md).

The mesh bridge spools the family lines it hears to RAM (/run/prepper/family-inbox.jsonl, 0600) when the family
board is on persistent storage; the launcher seals each with its CURRENT key onto the board, so family members can
read them while locked — only the last [sealed] recent_hours (48) / recent_max (200), never visitors or an admin
session alone. Through the real portal (tests/redteam/harness.py) and the real meshbridge.board_add.
Run: python3.11 tests/test_family_inbox.py -v
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "redteam"))
from harness import OPENSSL, PHONE_HTTP, REPO, PortalCase, server  # noqa: E402

sys.path.insert(0, str(REPO / "software" / "meshbridge"))
import meshbridge as mb  # noqa: E402
import sealed  # noqa: E402

PW = "purple tractor sings loudly"
VISITOR = {"X-Real-IP": "10.10.10.99", "X-Forwarded-Proto": "http"}


def iso(ago_s: float = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago_s))


def restart():
    """A launcher restart (not a reboot): no keys, no caches; /run (the spool) stays."""
    sealed._instances.clear()
    sealed._default[0] = None
    server._members = None


@unittest.skipUnless(OPENSSL, "needs openssl")
class FamilyInbox(PortalCase):
    EXTRA_TOML = "[sealed]\nscrypt_n = 1024\n"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.saved = {k: getattr(mb, k) for k in ("SEALER", "FAMILY_BOARD", "FAMILY_INBOX")}
        cls.saved_ram = list(mb.RAM_DIRS)
        cls.rundir = cls.tmp / "run"
        cls.rundir.mkdir()
        mb.RAM_DIRS[:] = [str(cls.rundir)]
        st, inv = cls.req("POST", "/api/admin/members", {"op": "invite", "name": "Mom Garcia"}, admin=cls.admin,
                          headers=PHONE_HTTP)
        assert st == 200, inv
        st, j = cls.req("POST", "/api/members/join", {"code": inv["code"], "name": "Mom Garcia", "device": "phone"},
                        headers=PHONE_HTTP)
        assert st == 200, j
        cls.mom = j["key"]
        cls.mom_id = [m["id"] for m in cls.req("POST", "/api/admin/members", {"op": "list"}, admin=cls.admin,
                                               headers=PHONE_HTTP)[1]["members"] if m["name"] == "Mom Garcia"][0]
        st, r = cls.req("POST", "/api/sealed", {"op": "setup", "passphrase": PW}, admin=cls.admin)   # at the Badger
        assert st == 200, r
        # the bridge is another process: its own Sealer instance on the same folder (public key only)
        mb.SEALER = sealed.Sealer(cls.tmp / "sealed", openssl=OPENSSL)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.saved.items():
            setattr(mb, k, v)
        mb.RAM_DIRS[:] = cls.saved_ram
        super().tearDownClass()
        restart()

    def setUp(self):
        super().setUp()
        n = self.id().rsplit(".", 1)[-1]
        server.CFG.family_board_file = mb.FAMILY_BOARD = self.tmp / f"fb-{n}.jsonl"
        server.CFG.family_inbox = mb.FAMILY_INBOX = self.rundir / f"inbox-{n}.jsonl"
        server.CFG.raw.setdefault("sealed", {}).pop("recent_max", None)
        restart()                                                   # locked (set up, after a restart)
        self.assertFalse(server.sealed_store().is_unlocked())

    # helpers
    def family(self, member=None, admin=None, headers=PHONE_HTTP):
        return self.req("GET", "/api/board?board=family", member=member, admin=admin, headers=headers)

    def texts(self):
        st, b = self.family(member=self.mom)
        self.assertEqual(st, 200, b)
        return [m["text"] for m in b["messages"]], b

    def spool(self, *recs):
        with open(server.CFG.family_inbox, "a") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def unlock(self):
        st, r = self.req("POST", "/api/sealed", {"op": "unlock", "secret": PW}, admin=self.admin)
        self.assertEqual(st, 200, r)

    # ── the main case ──
    def test_radio_post_while_locked_reaches_members_sealed(self):
        mb.board_add("Ana", "home safe at 6, radio", "family", "!a1b2c3d4", self.mom_id)
        inbox = server.CFG.family_inbox
        self.assertEqual(inbox.stat().st_mode & 0o777, 0o600)
        self.assertIn("home safe at 6", inbox.read_text())                       # RAM only
        self.assertFalse(mb.FAMILY_BOARD.exists())                               # nothing on disk yet
        texts, b = self.texts()
        self.assertEqual(texts, ["home safe at 6, radio"])
        self.assertTrue(b["sealed"]["locked"])
        self.assertEqual((b["sealed"]["recent_hours"], b["sealed"]["recent_max"]), (48.0, 200))
        msg = b["messages"][0]
        self.assertEqual((msg["via"], msg["verified"], msg.get("system")), ("mesh-in", False, None))
        # on disk: sealed, one {"s": token} line; the spool is empty
        raw = mb.FAMILY_BOARD.read_text()
        self.assertNotIn("home safe", raw)
        self.assertEqual([list(json.loads(ln)) for ln in raw.splitlines()], [["s"]])
        self.assertEqual(inbox.read_bytes(), b"")
        # never for visitors or an admin session alone; never on the community board
        self.assertIn(self.family(headers=VISITOR)[0], (401, 403))
        self.assertIn(self.family(admin=self.admin)[0], (401, 403))
        self.assertNotIn("home safe", json.dumps(self.req("GET", "/api/board?board=community", headers=VISITOR)[1]))
        # still there after the unlock
        self.unlock()
        self.assertEqual(self.texts()[0], ["home safe at 6, radio"])

    def test_old_way_when_the_spool_cant_be_written(self):
        """Bridge-sealed lines (its own key) stay hidden while locked — why the spool exists."""
        mb.FAMILY_INBOX = self.tmp / "no-such-dir" / "inbox.jsonl"
        mb.board_add("Ana", "via the old path", "family", "!a1b2c3d4", self.mom_id)
        self.assertNotIn("via the old path", mb.FAMILY_BOARD.read_text())
        texts, b = self.texts()
        self.assertEqual(texts, [])
        self.assertEqual(b["sealed"]["hidden"], 1)
        self.unlock()
        self.assertEqual(self.texts()[0], ["via the old path"])

    def test_no_sealed_records_keeps_the_plain_way(self):
        old = mb.SEALER
        mb.SEALER = None
        try:
            mb.board_add("Ana", "plain as before", "family", "!a1b2c3d4", self.mom_id)
        finally:
            mb.SEALER = old
        self.assertFalse(server.CFG.family_inbox.exists())
        self.assertIn("plain as before", mb.FAMILY_BOARD.read_text())            # the launcher seals it on read
        self.texts()
        self.assertNotIn("plain as before", mb.FAMILY_BOARD.read_text())

    def test_board_in_ram_is_written_directly(self):
        server.CFG.family_board_file = mb.FAMILY_BOARD = self.rundir / "fb-ram.jsonl"
        mb.board_add("Ana", "ram board", "family", "!a1b2c3d4", self.mom_id)
        self.assertFalse(server.CFG.family_inbox.exists())
        self.assertTrue(mb.FAMILY_BOARD.exists())

    # ── the bound while locked ──
    def test_only_last_48h_and_200_while_locked(self):
        recent = [{"at": iso(60 * i), "name": "radio", "text": f"recent {i}"} for i in range(205, 0, -1)]
        stale = [{"at": iso(72 * 3600), "name": "radio", "text": f"three days old {i}"} for i in range(3)]
        self.spool(*recent, *stale)
        self.assertEqual(server.ingest_family_inbox(), 208)
        texts, b = self.texts()
        self.assertEqual(len(texts), 197)                       # the board's last 200 lines, minus the 3 stale
        self.assertFalse(any(t.startswith("three days") for t in texts))
        self.assertEqual(texts[-1], "recent 1")
        self.assertGreaterEqual(b["sealed"]["hidden"], 3)
        server.CFG.raw["sealed"]["recent_max"] = 50
        texts, b = self.texts()
        self.assertEqual(texts, [f"recent {i}" for i in range(50, 0, -1)])        # the newest 50
        self.assertGreaterEqual(b["sealed"]["hidden"], 150)
        server.CFG.raw["sealed"].pop("recent_max")
        self.unlock()                                           # unlocked: no bound
        texts = self.texts()[0]
        self.assertEqual(len(texts), 200)
        self.assertEqual(texts[-3:], [f"three days old {i}" for i in range(3)])

    def test_bound_applies_to_posts_made_here_too(self):
        server._last_post.clear()
        self.assertEqual(self.req("POST", "/api/board", {"board": "family", "text": "made here"}, member=self.mom,
                                  headers=PHONE_HTTP)[0], 200)
        self.spool({"at": iso(49 * 3600), "name": "radio", "text": "49 h"}, {"at": iso(47 * 3600), "name": "radio",
                                                                          "text": "47 h"})
        self.assertEqual(self.texts()[0], ["made here", "47 h"])
        server.CFG.raw["sealed"]["recent_hours"] = 1
        try:
            self.assertEqual(self.texts()[0], ["made here"])
        finally:
            server.CFG.raw["sealed"].pop("recent_hours")

    # ── what the spool may say ──
    def test_hostile_spool_lines(self):
        inbox = server.CFG.family_inbox
        self.spool({"text": "fake verified", "verified": True, "via": "local", "system": True, "member": self.mom_id,
                    "pid": "x", "at": "yesterday"},
                   {"text": ""}, [1, 2], {"text": 5}, {"name": "n" * 500, "text": "t" * 5000})
        with open(inbox, "a") as fh:
            fh.write("not json\n{\"text\": \"half a line")
        self.assertEqual(server.ingest_family_inbox(), 2)
        self.assertEqual(inbox.read_text(), "{\"text\": \"half a line")            # kept for the next pass
        st, b = self.family(member=self.mom)
        a, c = b["messages"]
        self.assertEqual((a["text"], a["via"], a["verified"], a.get("system"), a.get("mesh")),
                         ("fake verified", "mesh-in", False, None, None))
        self.assertRegex(a["at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual((len(c["name"]), len(c["text"])), (32, 2000))

    def test_symlinked_spool_is_refused(self):
        target = self.tmp / "elsewhere.jsonl"
        target.write_text(json.dumps({"text": "via a link"}) + "\n")
        os.symlink(target, server.CFG.family_inbox)
        self.assertEqual(server.ingest_family_inbox(), 0)
        mb.board_add("Ana", "not through the link", "family", "!a1b2c3d4", self.mom_id)
        self.assertEqual(target.read_text().count("\n"), 1)                      # the bridge didn't follow it
        self.assertNotIn("not through the link", mb.FAMILY_BOARD.read_text())    # (sealed the old way instead)

    def test_full_spool_falls_back(self):
        with open(server.CFG.family_inbox, "w") as fh:
            fh.write(" " * (mb.INBOX_MAX_BYTES + 1))
        mb.board_add("Ana", "spool full", "family", "!a1b2c3d4", self.mom_id)
        self.assertNotIn("spool full", server.CFG.family_inbox.read_text())
        self.assertTrue(mb.FAMILY_BOARD.exists())

    # ── concurrency ──
    def test_concurrent_appends_and_ingest_lose_nothing(self):
        threads, per, stop = 4, 40, threading.Event()
        taken = []

        def bridge(t):
            for i in range(per):
                mb.board_add("radio", f"t{t} m{i}", "family", "!a1b2c3d4", None)

        def portal():
            while not stop.is_set():
                taken.append(server.ingest_family_inbox())

        ing = [threading.Thread(target=portal) for _ in range(2)]
        for t in ing:
            t.start()
        ws = [threading.Thread(target=bridge, args=(t,)) for t in range(threads)]
        for t in ws:
            t.start()
        for t in ws:
            t.join()
        stop.set()
        for t in ing:
            t.join()
        taken.append(server.ingest_family_inbox())
        self.assertEqual(sum(taken), threads * per)
        self.assertEqual(server.CFG.family_inbox.read_bytes(), b"")
        lines = mb.FAMILY_BOARD.read_text().splitlines()
        s = server.sealed_store()
        got = sorted(s.open_json(json.loads(ln)["s"], recent_ok=True)["text"] for ln in lines)
        self.assertEqual(got, sorted(f"t{t} m{i}" for t in range(threads) for i in range(per)))

    def test_timer_takes_the_spool_in(self):
        old = server.FAMILY_INBOX_EVERY
        server.FAMILY_INBOX_EVERY = 0.05
        try:
            server.start_family_inbox_timer()
            mb.board_add("Ana", "by the timer", "family", "!a1b2c3d4", self.mom_id)
            deadline = time.time() + 5
            while time.time() < deadline and not (mb.FAMILY_BOARD.exists() and mb.FAMILY_BOARD.stat().st_size):
                time.sleep(0.02)
            self.assertTrue(mb.FAMILY_BOARD.exists())
            self.assertEqual(server.CFG.family_inbox.read_bytes(), b"")
        finally:
            server.FAMILY_INBOX_EVERY = old

    # ── restarts ──
    def test_launcher_restart_keeps_the_spool_reboot_drops_it(self):
        mb.board_add("Ana", "before the restart", "family", "!a1b2c3d4", self.mom_id)
        restart()                                       # the launcher restarts; /run/prepper stays
        self.assertEqual(self.texts()[0], ["before the restart"])
        mb.board_add("Ana", "lost in a reboot", "family", "!a1b2c3d4", self.mom_id)
        server.CFG.family_inbox.unlink()                # a reboot empties /run before the launcher took it in
        restart()
        self.assertEqual(self.texts()[0], [])           # (the first one was sealed with the old run's key)
        self.assertNotIn("lost in a reboot", mb.FAMILY_BOARD.read_text())

    def test_decoy_never_takes_real_radio_lines(self):
        self.assertIn("family_inbox", server.DECOY_FIELDS)

    def test_wipe_knows_the_spool(self):
        sys.path.insert(0, str(REPO / "os"))
        import wipe
        self.assertIn("family-inbox.jsonl", wipe.RUN_MESH_FILES)


if __name__ == "__main__":
    unittest.main()
