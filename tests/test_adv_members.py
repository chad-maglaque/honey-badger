"""
Adversarial / stress tests for software/portal/members.py (family invites, device keys, radios).

Failing tests are intentional: each one marked `# BUG:` documents a defect (expected vs actual).
Run: python3.11 tests/test_adv_members.py
"""
from __future__ import annotations

import json
import os
import random
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "software" / "meshbridge"))
os.environ["PORTAL_QUIET"] = "1"
import members  # noqa: E402
import meshbridge as mb  # noqa: E402


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.t

    def advance(self, dt: float):
        with self.lock:
            self.t += dt


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adv-members-")).resolve()
        self.path = self.tmp / "members.json"
        self.clock = Clock()
        self.db = members.Members(self.path, clock=self.clock)

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            try:
                p.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def join_new(self, name="Ana", ip="10.0.0.1"):
        code = self.db.create_invite()["code"]
        st, m, key = self.db.join(code, name, f"{name}'s phone", ip)
        self.assertEqual(st, "ok", key)
        return m, key

    def raw(self) -> dict:
        return json.loads(self.path.read_text())


# ───────────────────────────── one-time codes ─────────────────────────────

class OneTimeCodes(Base):
    def test_parallel_joins_racing_one_code_yield_exactly_one_member(self):
        code = self.db.create_invite()["code"]
        results, barrier = [], threading.Barrier(32)

        def go(i):
            barrier.wait()
            results.append(self.db.join(code, f"P{i}", "phone", f"10.1.0.{i}"))
        ts = [threading.Thread(target=go, args=(i,)) for i in range(32)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        oks = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(oks), 1, [r[0] for r in results])
        self.assertEqual(len(self.raw()["members"]), 1)
        self.assertEqual(self.raw()["invites"], {})

    def test_parallel_wrong_codes_from_one_ip_get_exactly_five_guesses(self):
        barrier, results = threading.Barrier(40), []

        def go(i):
            barrier.wait()
            results.append(self.db.join(f"AAAA-BBBB-{i:02d}", "x", "y", "6.6.6.6")[0])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(40)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("bad"), members.JOIN_FAILS)
        self.assertEqual(results.count("locked"), 40 - members.JOIN_FAILS)

    def test_lockout_survives_a_spray_of_failures_from_other_addresses(self):
        # BUG: _join_failed() evicts the 500 *oldest* entries once more than 1000 IPs have failed,
        # including entries that are currently LOCKED. An attacker with many addresses (IPv6 privacy
        # addresses, or MAC/IP cycling on the hotspot) fails once from 1000 throwaway addresses and
        # the lock on their main address is gone. Expected: a locked IP stays locked for JOIN_LOCK.
        # Actual: join_wait() drops to 0. Source: software/portal/members.py _join_failed (len > 1000 pruning).
        for _ in range(members.JOIN_FAILS):
            self.db.join("AAAA-BBBB-CC", "x", "y", "6.6.6.6")
        self.assertGreater(self.db.join_wait("6.6.6.6"), 0)
        for i in range(1001):
            self.clock.advance(0.01)
            self.db.join("AAAA-BBBB-CC", "x", "y", f"2001:db8::{i:x}")
        self.assertGreater(self.db.join_wait("6.6.6.6"), 0, "lock on 6.6.6.6 was evicted by the spray")

    def test_24h_boundary(self):
        inv = self.db.create_invite()
        self.clock.t = inv["expires"] - 0.001
        self.assertEqual(self.db.join(inv["code"], "Ana", "p", "1.1.1.1")[0], "ok")
        inv = self.db.create_invite()
        self.clock.t = inv["expires"]                       # exactly at expiry: gone
        self.assertEqual(self.db.join(inv["code"], "Ben", "p", "1.1.1.2")[0], "bad")
        inv = self.db.create_invite()
        self.clock.t = inv["expires"] + 1
        self.assertEqual(self.db.join(inv["code"], "Cal", "p", "1.1.1.3")[0], "bad")

    def test_code_spelling_variants_accepted(self):
        for i, fmt in enumerate((str.lower, lambda c: c.replace("-", " "), lambda c: c.replace("-", "."),
                                 lambda c: c.replace("-", "_"), lambda c: "  " + c + "\t\n",
                                 lambda c: c.replace("-", " "), lambda c: c.replace("-", ""))):
            code = self.db.create_invite()["code"]
            with self.subTest(i=i):
                self.assertEqual(self.db.join(fmt(code), f"N{i}", "p", f"9.9.9.{i}")[0], "ok")

    def test_hostile_code_inputs_never_crash_and_never_match(self):
        self.db.create_invite()
        hostile = ["", " ", "-" * 5000, "A" * 100000, "\x00" * 10, "ＫＱＸＭ２ＰＤ９Ｚ", "🦡" * 50,
                   "' OR 1=1 --", "../../etc/passwd", None, 12345, ["AAAA"], {"code": 1}]
        for i, c in enumerate(hostile):
            with self.subTest(code=repr(c)[:30]):
                st = self.db.join(c, "x", "y", f"7.7.{i}.1")[0]
                self.assertEqual(st, "bad")
        self.assertEqual(len(self.raw()["invites"]), 1)     # the real invite is untouched

    def test_lone_surrogate_code_is_a_wrong_code_not_a_crash(self):
        # BUG: a JSON body {"code": "\\ud800"} decodes to a str with a lone surrogate; norm_code() keeps it
        # and _h() calls .encode() → UnicodeEncodeError, so /api/members/join answers 500 instead of 403,
        # and the guess is not counted towards the lockout. Expected: ("bad", ...). Actual: exception.
        # Source: software/portal/members.py _h()/norm_code() (no errors= handling).
        self.db.create_invite()
        self.assertEqual(self.db.join("\ud800AAAA", "x", "y", "7.7.7.7")[0], "bad")


# ───────────────────────────── names ─────────────────────────────

class Names(Base):
    def test_control_chars_html_and_length(self):
        m, _ = self.join_new("\x00A\x1bna\x7f\n<script>alert(1)</script>" + "x" * 500)
        self.assertNotRegex(m["name"], r"[\x00-\x1f\x7f]")
        self.assertLessEqual(len(m["name"]), 32)
        self.assertIn("<script>", m["name"])                # stored literally; escaped on output (see boards tests)

    def test_device_label_cleaned(self):
        code = self.db.create_invite()["code"]
        self.db.join(code, "Ana", "\x1b[31mred\x07" + "L" * 100, "1.2.3.4")
        lab = self.db.list()["members"][0]["devices"][0]["label"]
        self.assertNotRegex(lab, r"[\x00-\x1f\x7f]")
        self.assertLessEqual(len(lab), 40)

    def test_bidi_overrides_and_invisible_chars_are_stripped(self):
        # BUG: clean_name() only removes C0 controls and DEL. Bidi overrides (U+202E RLO, U+2066-2069),
        # zero-width chars (U+200B-U+200F, U+FEFF) and C1 controls (U+0080-U+009F) survive, so a member
        # (or an anonymous community poster, via the meshbridge longName path) can show up in the admin's
        # member list / remove-confirmation dialog / board as a visually different name ("Mom" vs
        # "M‮mo", or "Ana" vs "A​na" which looks identical to the real Ana).
        # Expected: such code points removed. Actual: kept. Source: software/portal/members.py clean_name.
        for evil in ("A‮na", "A​na", "⁦Ana⁩", "﻿Ana", "A\u0085na", "An‍a"):
            with self.subTest(name=repr(evil)):
                self.assertEqual(members.clean_name(evil), "Ana")

    def test_name_required_unless_invite_has_one_and_whitespace_only_is_empty(self):
        code = self.db.create_invite()["code"]
        for nm in ("", "   ", "\x00\x01", "\n\t"):
            self.assertEqual(self.db.join(code, nm, "p", "1.1.1.1")[0], "name")
        self.assertEqual(self.db.join(code, "Real", "p", "1.1.1.1")[0], "ok")


# ───────────────────────────── keys ─────────────────────────────

class Keys(Base):
    def test_key_dead_after_leave_revoke_and_remove(self):
        m, k1 = self.join_new("Ana")
        _, same, k2 = self.db.join(self.db.create_invite(member_id=m["id"])["code"], "", "tablet", "1.1.1.1")
        _, same, k3 = self.db.join(self.db.create_invite(member_id=m["id"])["code"], "", "laptop", "1.1.1.1")
        self.assertTrue(self.db.leave(k1))
        self.assertIsNone(self.db.auth(k1))
        self.assertFalse(self.db.leave(k1))                  # second leave: nothing
        dev = [d for d in self.db.list()["members"][0]["devices"] if d["label"] == "tablet"][0]["id"]
        self.db.revoke_device(m["id"], dev)
        self.assertIsNone(self.db.auth(k2))
        self.assertEqual(self.db.auth(k3)["id"], m["id"])
        self.db.remove(m["id"])
        self.assertIsNone(self.db.auth(k3))
        # a removed member's key can't be re-added by an old extra-device invite either
        self.assertRaises(ValueError, self.db.create_invite, member_id=m["id"])

    def test_odd_keys(self):
        _, key = self.join_new()
        for bad in ("", " ", key[:-1], key + "x", key.upper() if key.upper() != key else key.lower(),
                    " " + key, "x" * 128, "x" * 129, "x" * 100000, "\x00", "ÿ" * 40, "🦡" * 30, key + "\x00"):
            with self.subTest(k=repr(bad)[:30]):
                self.assertIsNone(self.db.auth(bad))
        self.assertIsNotNone(self.db.auth(key))

    def test_keys_unique_and_not_stored(self):
        keys = [self.join_new(f"N{i}", f"1.0.0.{i}")[1] for i in range(15)]
        self.assertEqual(len(set(keys)), 15)
        raw = self.path.read_text()
        self.assertFalse(any(k in raw for k in keys))

    def test_removed_member_rejoining_is_a_new_member(self):
        m, k = self.join_new("Ana")
        self.db.add_radio(m["id"], "!a1b2c3d4")
        self.db.remove(m["id"])
        m2, k2 = self.join_new("Ana")
        self.assertNotEqual(m2["id"], m["id"])
        self.assertEqual(m2["radios"], [])                   # radios are not inherited
        self.assertIsNone(self.db.auth(k))
        self.assertEqual(self.db.auth(k2)["id"], m2["id"])
        reg = members.radio_registry(self.path)
        self.assertTrue(reg["!a1b2c3d4"]["removed"])
        self.db.add_radio(m2["id"], "!a1b2c3d4")              # re-registering their radio to the new record works
        self.assertFalse(members.radio_registry(self.path)["!a1b2c3d4"]["removed"])


# ───────────────────────────── limits ─────────────────────────────

class Limits(Base):
    def test_member_cap_enforced_at_join_time(self):
        # BUG: MAX_MEMBERS is only checked in create_invite(), never in join(). With 49 members the admin
        # (or anyone at the front panel via /api/local/invite) can mint up to 20 invites while the count is
        # still below the cap, and every one of them still redeems: the family ends up with 69 members.
        # Expected: join() refuses once 50 active members exist. Actual: 54+ members.
        # Source: software/portal/members.py join() (no MAX_MEMBERS check).
        for i in range(members.MAX_MEMBERS - 1):
            self.join_new(f"M{i:02d}", f"10.2.{i}.1")
        codes = [self.db.create_invite()["code"] for _ in range(5)]
        for i, c in enumerate(codes):
            self.db.join(c, f"Late{i}", "p", f"10.3.{i}.1")
        active = sum(1 for m in self.raw()["members"].values() if not m.get("removed"))
        self.assertLessEqual(active, members.MAX_MEMBERS)

    def test_invite_cap(self):
        for _ in range(members.MAX_INVITES):
            self.db.create_invite()
        self.assertRaises(ValueError, self.db.create_invite)
        self.clock.advance(members.INVITE_TTL + 1)
        self.db.create_invite()                              # expired ones free their slots

    def test_device_cap_under_concurrency(self):
        m, _ = self.join_new("Ana")
        for _ in range(members.MAX_DEVICES - 2):
            self.db.join(self.db.create_invite(member_id=m["id"])["code"], "", "d", "1.1.1.1")
        codes = [self.db.create_invite(member_id=m["id"])["code"] for _ in range(8)]
        barrier, res = threading.Barrier(len(codes)), []

        def go(c, i):
            barrier.wait()
            res.append(self.db.join(c, "", f"d{i}", f"2.2.2.{i}")[0])
        ts = [threading.Thread(target=go, args=(c, i)) for i, c in enumerate(codes)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(res.count("ok"), 1)
        self.assertEqual(len(self.raw()["members"][m["id"]]["devices"]), members.MAX_DEVICES)

    def test_radio_cap_and_uniqueness_under_races(self):
        a, _ = self.join_new("Ana", "1.1.1.1")
        b, _ = self.join_new("Ben", "1.1.1.2")
        barrier, errs = threading.Barrier(20), []

        def go(i):
            barrier.wait()
            try:
                self.db.add_radio((a, b)[i % 2]["id"], "!deadbeef")
            except ValueError as e:
                errs.append(str(e))
        ts = [threading.Thread(target=go, args=(i,)) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        owners = [mid for mid, m in self.raw()["members"].items() if "!deadbeef" in m["radios"]]
        self.assertEqual(len(owners), 1)
        radios = [f"!{i:08x}" for i in range(12)]
        barrier = threading.Barrier(len(radios))

        def add(r):
            barrier.wait()
            try:
                self.db.add_radio(b["id"], r)
            except ValueError:
                pass
        ts = [threading.Thread(target=add, args=(r,)) for r in radios]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertLessEqual(len(self.raw()["members"][b["id"]]["radios"]), members.MAX_RADIOS)

    def test_radio_id_spellings(self):
        a, _ = self.join_new("Ana")
        for good in ("!A1B2C3D4", " !a1b2c3d4 ", "!a1b2c3d4"):
            self.db.add_radio(a["id"], good)
        self.assertEqual(self.raw()["members"][a["id"]]["radios"], ["!a1b2c3d4"])
        for bad in ("!a1b2c3d4\n", "!a1b2c3d", "!a1b2c3d45", "!ａ1b2c3d4", "!a1b2c3d٤", "", "!", None, 5):
            with self.subTest(r=repr(bad)):
                if bad == "!a1b2c3d4\n":
                    continue                                 # strip() makes this one fine
                self.assertRaises(ValueError, self.db.add_radio, a["id"], bad)


# ───────────────────────────── storage ─────────────────────────────

class Storage(Base):
    def test_concurrent_join_auth_remove_no_lost_updates(self):
        pre = [self.join_new(f"Pre{i}", f"10.9.0.{i}") for i in range(10)]
        codes = [self.db.create_invite()["code"] for _ in range(members.MAX_INVITES)]
        errors, joined, barrier = [], [], threading.Barrier(len(codes) + 10 + 5)

        def joiner(c, i):
            barrier.wait()
            try:
                st, m, k = self.db.join(c, f"New{i}", "p", f"10.8.0.{i}")
                if st == "ok":
                    joined.append(m["id"])
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        def authr(k):
            barrier.wait()
            try:
                for _ in range(20):
                    self.clock.advance(400)                  # force the last_seen rewrite path
                    self.db.auth(k)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        def remover(mid):
            barrier.wait()
            try:
                self.db.remove(mid)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        ts = [threading.Thread(target=joiner, args=(c, i)) for i, c in enumerate(codes)]
        ts += [threading.Thread(target=authr, args=(k,)) for _, k in pre]
        ts += [threading.Thread(target=remover, args=(m["id"],)) for m, _ in pre[:5]]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        d = self.raw()                                        # still valid JSON
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(len(d["members"]), 10 + len(joined))
        self.assertTrue(all(mid in d["members"] for mid in joined))
        removed = [mid for mid, m in d["members"].items() if m.get("removed")]
        self.assertEqual(sorted(removed), sorted(m["id"] for m, _ in pre[:5]))
        self.assertTrue(all(d["members"][m["id"]]["devices"] == [] for m, _ in pre[:5]))
        self.assertEqual([p for p in self.tmp.iterdir() if p.name.startswith(".members.json.")], [])

    def test_corrupt_file_is_not_silently_wiped_by_the_next_write(self):
        # BUG: _load() treats an unparseable members.json as empty, and the next write (an invite from the
        # admin page or the front panel, or a join) replaces it, destroying every member, device key and
        # radio registration without a trace. Expected: refuse to write (raise) or keep the unreadable file
        # (e.g. members.json.corrupt) before starting over. Actual: file silently overwritten with only the
        # new invite. Source: software/portal/members.py _load()/_save().
        for i in range(3):
            self.join_new(f"Fam{i}", f"10.0.1.{i}")
        good = self.path.read_bytes()
        corrupt = good[: len(good) // 2]
        self.path.write_bytes(corrupt)
        try:
            self.db.create_invite("Zed")
            refused = False
        except Exception:  # noqa: BLE001
            refused = True
        kept = any(p.read_bytes() == corrupt for p in self.tmp.iterdir() if p.is_file())
        self.assertTrue(refused or kept, "the corrupt members.json was overwritten and its contents lost")

    def test_unreadable_file_is_not_silently_wiped(self):
        # BUG: same root cause as above, for a members.json the portal can't read (wrong owner/mode after
        # a restore from backup): it is treated as empty and replaced on the next write.
        # Expected: an error / the file left alone. Actual: replaced with a fresh, member-less file.
        # Source: software/portal/members.py _load() (catches OSError and returns {}).
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        self.join_new("Ana")
        before = self.path.read_bytes()
        self.path.chmod(0)
        try:
            self.db.create_invite()
        except Exception:  # noqa: BLE001
            pass
        self.path.chmod(0o600)
        self.assertEqual(self.path.read_bytes(), before)

    def test_symlinked_file_is_replaced_not_followed(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("do not touch")
        self.path.symlink_to(victim)
        self.db.create_invite()
        self.assertEqual(victim.read_text(), "do not touch")
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_hostile_structures_do_not_crash(self):
        # BUG (robustness): a members.json whose member entries / radio lists have the wrong JSON type makes
        # auth() (i.e. every request that carries X-Member-Token), list() and radio_registry() raise instead
        # of skipping the bad entry, so one bad record takes down the family board for everyone and crashes
        # the mesh bridge's on_text. Expected: bad entries skipped. Actual: AttributeError/TypeError.
        # Source: software/portal/members.py auth()/list()/radio_registry(); meshbridge.radio_registry().
        hostile = {"v": 1, "invites": {"x": 5, "y": {"expires": "soon"}},
                   "members": {"m_bad1": "string", "m_bad2": {"name": 7, "radios": 5, "removed_radios": 3,
                                                              "devices": "nope"},
                               "m_ok": {"name": "Ana", "devices": [], "radios": ["!a1b2c3d4"]}}}
        self.path.write_text(json.dumps(hostile))
        problems = []
        for label, fn in (("auth", lambda: self.db.auth("k")), ("list", self.db.list),
                          ("members.radio_registry", lambda: members.radio_registry(self.path)),
                          ("mb.radio_registry", lambda: mb.radio_registry(self.path))):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                problems.append(f"{label}: {type(e).__name__}: {e}")
        self.assertEqual(problems, [])

    def test_random_garbage_files(self):
        rnd = random.Random(1234)
        for _i in range(200):
            blob = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 200)))
            self.path.write_bytes(blob)
            self.assertIsNone(self.db.auth("k"))
            self.assertIsInstance(members.radio_registry(self.path), dict)
            self.assertIsInstance(mb.radio_registry(self.path), dict)


if __name__ == "__main__":
    unittest.main(verbosity=1)
