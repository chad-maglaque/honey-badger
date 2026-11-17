#!/usr/bin/env python3
"""
Sealed records — unit tests for software/common/sealed.py (the crypto, lock state, posture lockdown,
idle lock, lockout, crypto-erase, SealedMap/SealedJournal). Over-HTTP tests: tests/test_sealed_http.py.
Run: python3.11 tests/test_sealed.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "common"))
import sealed  # noqa: E402

OPENSSL = "/usr/bin/openssl" if Path("/usr/bin/openssl").exists() else shutil.which("openssl")
OPENSSL3 = next((p for p in ("/opt/homebrew/bin/openssl", "/usr/local/bin/openssl")
                 if Path(p).exists() and os.path.realpath(p) != os.path.realpath(OPENSSL or "")), None)
PW = "purple tractor sings loudly"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def grep(folder: Path, needle: bytes) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and needle in p.read_bytes())


@unittest.skipUnless(OPENSSL, "needs openssl")
class SealerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sealed-")).resolve()
        self.wall, self.mono = Clock(), Clock(1000.0)
        self.s = self.make()

    def make(self, **kw):
        opts = {"openssl": OPENSSL, "scrypt_n": 1024, "clock": self.wall, "mono": self.mono}
        opts.update(kw)
        return sealed.Sealer(self.tmp / "sealed", **opts)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Basics(SealerBase):
    def test_round_trip_and_forms(self):
        rec = self.s.seal(b"Insulin pens behind the false wall")
        self.assertTrue(rec.startswith(sealed.MAGIC))
        self.assertEqual(self.s.open_(rec), b"Insulin pens behind the false wall")
        tok = self.s.seal_text("Lucia Garcia (age 7)")
        self.assertTrue(sealed.is_token(tok))
        self.assertEqual(self.s.open_text(tok), "Lucia Garcia (age 7)")
        self.assertEqual(self.s.open_json(self.s.seal_json({"a": [1, "é"]})), {"a": [1, "é"]})
        self.assertNotEqual(self.s.seal(b"x"), self.s.seal(b"x"))            # random per record
        self.assertEqual(grep(self.tmp, b"false wall"), [])
        self.assertEqual(grep(self.tmp, b"Lucia"), [])

    def test_before_setup_it_is_sealed_but_provisional(self):
        self.s.seal(b"x")
        st = self.s.status()
        self.assertTrue(st["unlocked"])
        self.assertFalse(st["setup"])
        self.assertTrue(st["provisional"])
        m = json.loads((self.tmp / "sealed" / "device.key").read_text())
        self.assertIn("provisional", m["wraps"])
        self.assertEqual((self.tmp / "sealed" / "device.key").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.tmp / "sealed").stat().st_mode & 0o777, 0o700)

    def test_tampering_is_detected(self):
        rec = bytearray(self.s.seal(b"hello world, this is a record"))
        for i in (len(rec) - 1, len(rec) - 40, 20):
            bad = bytearray(rec)
            bad[i] ^= 1
            self.s._cache.clear()
            with self.assertRaises(sealed.SealError):
                self.s.open_(bytes(bad))
        with self.assertRaises(sealed.SealError):
            self.s.open_(bytes(rec[:-5]))
        with self.assertRaises(sealed.SealError):
            self.s.open_(b"HBS1" + b"\0" * 8 + bytes(rec[12:]))               # unknown key id
        with self.assertRaises(sealed.SealError):
            self.s.open_text("hbs1:!!!notbase64")
        with self.assertRaises(sealed.SealError):
            self.s.open_(b"plain text, not sealed at all")

    def test_concurrent_sealing_and_opening(self):
        errors = []

        def work(i):
            try:
                for j in range(5):
                    data = f"{i}-{j}".encode()
                    if self.s.open_(self.s.seal(data)) != data:
                        errors.append("mismatch")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        ts = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(list((self.tmp / "sealed" / "keys").glob("*.dek"))), 1)     # one key per run

    def test_passphrase_rules(self):
        self.assertIsNotNone(sealed.passphrase_problem("short"))
        self.assertIsNotNone(sealed.passphrase_problem("aaaaaaaaaaaaaaaaaaaa"))         # long but trivial
        self.assertIsNone(sealed.passphrase_problem("correct horse battery staple"))
        self.assertIsNone(sealed.passphrase_problem("Xk9#mQ2vLp7!Rz4w"))                # 16 characters
        self.assertIsNone(sealed.passphrase_problem("red cat sat mat"))                 # 4 words, 15 chars
        self.assertIsNotNone(sealed.passphrase_problem("a b c d"))
        self.assertIsNotNone(sealed.passphrase_problem(None))
        self.assertIsNotNone(sealed.passphrase_problem("x" * 300))
        with self.assertRaises(sealed.Refused) as cm:
            self.s.setup("too short")
        self.assertEqual(cm.exception.status, 400)


class SetupLockUnlock(SealerBase):
    def setUp(self):
        super().setUp()
        self.before = self.s.seal(b"written before setup")
        self.before_tok = self.s.seal_text("also before")
        old_fp = json.loads((self.tmp / "sealed" / "device.key").read_text())["fp"]
        self.migrated = []
        self.s.register_migrator("probe", lambda: self.migrated.append(True))
        self.res = self.s.setup(PW)
        self.old_fp = old_fp

    def restart(self):
        self.s = self.make()
        return self.s

    def test_setup_rewraps_keys_and_scrubs_the_provisional_one(self):
        self.assertRegex(self.res["recovery_key"], r"^([A-Z2-7]{4}-){12}[A-Z2-7]{4}$")
        self.assertEqual(self.res["migrated"], {"probe": "ok"})
        self.assertEqual(self.migrated, [True])
        m = json.loads((self.tmp / "sealed" / "device.key").read_text())
        self.assertNotIn("provisional", m["wraps"])
        self.assertNotEqual(m["fp"], self.old_fp)
        self.assertEqual(list((self.tmp / "sealed" / "keys").glob(f"*.{self.old_fp}.dek")), [])
        self.assertEqual(list((self.tmp / "sealed").glob(".*.scrub")), [])
        self.assertEqual(self.s.open_(self.before), b"written before setup")    # still readable, new key
        self.assertTrue(self.s.status()["setup"])
        with self.assertRaises(sealed.Refused) as cm:
            self.s.setup(PW)
        self.assertEqual(cm.exception.status, 409)

    def test_locked_after_restart_and_write_only(self):
        s = self.restart()
        self.assertFalse(s.is_unlocked())
        with self.assertRaises(sealed.Locked):
            s.open_(self.before)
        rec = s.seal(b"posted while locked")                 # public key only
        self.assertEqual(s.open_(rec, recent_ok=True), b"posted while locked")
        with self.assertRaises(sealed.Locked):
            s.open_(rec)                                      # not without asking for "recent" explicitly
        self.assertEqual(s.unlock(PW, "10.0.0.1"), "passphrase")
        self.assertEqual(s.open_(self.before), b"written before setup")
        self.assertEqual(s.open_text(self.before_tok), "also before")
        self.assertEqual(s.open_(rec), b"posted while locked")

    def test_lock_now_forgets_everything_including_recent(self):
        recent = self.s.seal(b"recent")
        self.s.lock_all()
        self.assertIsNone(self.s._priv)
        self.assertEqual(self.s._deks, {})
        self.assertEqual(len(self.s._cache), 0)
        with self.assertRaises(sealed.Locked):
            self.s.open_(recent, recent_ok=True)              # the run key was rotated by the lock
        self.assertEqual(self.s.status()["locked_reason"], "locked")

    def test_recovery_key_unlocks_and_resets_the_passphrase(self):
        rk = self.res["recovery_key"]
        s = self.restart()
        self.assertEqual(s.unlock(rk.lower().replace("-", " "), "10.0.0.1"), "recovery")
        s = self.restart()
        s.recover(rk, "a brand new owner passphrase", "10.0.0.1")
        self.assertTrue(s.is_unlocked())
        s = self.restart()
        with self.assertRaises(sealed.Refused):
            s.unlock(PW, "10.0.0.2")
        s.unlock("a brand new owner passphrase", "10.0.0.2")
        with self.assertRaises(sealed.Refused):
            s.recover("AAAA-BBBB", "another new passphrase ok", "10.0.0.3")

    def test_change_passphrase_and_new_recovery_key(self):
        with self.assertRaises(sealed.Refused) as cm:
            self.s.change_passphrase("wrong wrong wrong wrong", "a brand new owner passphrase", "ip")
        self.assertEqual(cm.exception.status, 403)
        self.s.change_passphrase(PW, "a brand new owner passphrase", "ip")
        rk2 = self.s.new_recovery_key("a brand new owner passphrase", "ip")
        self.assertFalse(self.s.status()["recovery_ack"])
        self.s.ack_recovery()
        self.assertTrue(self.s.status()["recovery_ack"])
        s = self.restart()
        with self.assertRaises(sealed.Refused):
            s.unlock(self.res["recovery_key"], "ip2")                      # the old recovery key stopped working
        self.assertEqual(s.unlock(rk2, "ip2"), "recovery")
        s = self.restart()
        s.unlock("a brand new owner passphrase", "ip3")
        self.assertEqual(s.open_(self.before), b"written before setup")

    def test_wrong_passphrases_lock_out_even_across_a_restart(self):
        s = self.restart()
        for _ in range(sealed.FAILS):
            with self.assertRaises(sealed.Refused) as cm:
                s.unlock("not the passphrase at all", "10.6.6.6")
            self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(sealed.Refused) as cm:
            s.unlock(PW, "10.6.6.6")                              # even the right one waits
        self.assertEqual(cm.exception.status, 429)
        s.unlock(PW, "10.0.0.9")                                  # another address is fine
        # Badger-wide: G_FAILS from many addresses → everyone waits, and a restart doesn't reset it
        s.lock_all()
        for i in range(sealed.G_FAILS):
            with self.assertRaises(sealed.Refused):
                s.unlock("not the passphrase at all", f"10.1.{i}.1")
        s = self.restart()
        with self.assertRaises(sealed.Refused) as cm:
            s.unlock(PW, "10.9.9.9")
        self.assertEqual(cm.exception.status, 429)
        self.wall.t += sealed.G_LOCK + 1
        s.unlock(PW, "10.9.9.9")

    def test_idle_auto_lock(self):
        s = self.make(idle_minutes=10)
        s.unlock(PW, "ip")
        self.mono.t += 9 * 60
        self.assertEqual(s.open_(self.before), b"written before setup")   # use resets the timer
        self.mono.t += 9 * 60
        self.assertTrue(s.is_unlocked())
        with s.background():                                            # background jobs don't count as use
            s.open_(self.before)
        self.mono.t += 2 * 60
        self.assertFalse(s.is_unlocked())
        self.assertEqual(s.status()["locked_reason"], "idle")

    def test_posture_lockdown_forces_a_lock_and_needs_confirmation(self):
        posture = self.tmp / "posture.json"
        self.assertEqual(self.s.posture_file, posture)
        posture.write_text(json.dumps({"level": "lockdown", "at": 1}))
        self.mono.t += 5
        self.assertFalse(self.s.is_unlocked())
        self.assertEqual(self.s.status()["locked_reason"], "lockdown")
        with self.assertRaises(sealed.Refused) as cm:
            self.s.unlock(PW, "ip")
        self.assertEqual(cm.exception.status, 409)
        self.assertTrue(cm.exception.extra.get("lockdown"))
        self.s.unlock(PW, "ip", confirm_lockdown=True)
        self.mono.t += 5
        self.assertTrue(self.s.is_unlocked())
        self.assertTrue(self.s.status()["lockdown_override"])
        posture.write_text(json.dumps({"level": "lockdown", "at": 2, "again": True}))   # set again: locks again
        self.mono.t += 5
        self.assertFalse(self.s.is_unlocked())
        posture.write_text(json.dumps({"level": "guarded"}))
        self.mono.t += 5
        self.s.unlock(PW, "ip")
        self.assertTrue(self.s.is_unlocked())

    def test_destroy_keys_is_a_crypto_erase(self):
        gone = self.s.destroy_keys()
        self.assertIn(str(self.tmp / "sealed" / "device.key"), gone)
        self.assertTrue(any(g.endswith(".dek") for g in gone))
        self.assertFalse((self.tmp / "sealed" / "device.key").exists())
        self.assertEqual(list((self.tmp / "sealed" / "keys").glob("*")), [])
        self.assertIsNone(self.s._priv)
        with self.assertRaises(sealed.SealError):
            self.s.open_(self.before)
        rec = self.s.seal(b"after the wipe")                     # keeps working with a fresh provisional key
        self.assertEqual(self.s.open_(rec), b"after the wipe")
        self.assertFalse(self.s.status()["setup"])
        with self.assertRaises(sealed.SealError):
            self.s.open_(self.before)

    def test_destroy_keys_with_a_root_like_the_panic_wipe(self):
        folder = self.tmp / "root" / "var/lib/prepper/portal/sealed"
        s = sealed.Sealer(folder, openssl=OPENSSL, scrypt_n=1024)
        s.seal(b"x")
        gone = sealed.destroy_keys(root=str(self.tmp / "root"))
        self.assertIn(str(folder / "device.key"), gone)
        self.assertFalse((folder / "device.key").exists())
        self.assertIn("/var/lib/prepper/portal/sealed", sealed.KEY_PATHS)

    def test_destroy_keys_from_another_process(self):
        gone = sealed.destroy_keys_in(self.tmp / "sealed")
        self.assertTrue(gone)
        self.assertFalse((self.tmp / "sealed" / "device.key").exists())
        self.assertEqual(sealed.destroy_keys_in(self.tmp / "sealed"), [])


class Plumbing(SealerBase):
    def test_no_secret_ever_in_argv(self):
        seen = []
        real = subprocess.run

        def spy(args, *a, **kw):
            seen.append((list(args), kw.get("pass_fds", ())))
            return real(args, *a, **kw)
        subprocess.run = spy
        try:
            s = self.s
            s.seal(b"x")
            res = s.setup(PW)
            s.lock_all()
            s.unlock(PW, "ip")
            s.open_(s.seal(b"y"))
            s2 = self.make()
            s2.unlock(res["recovery_key"], "ip")
            s2.open_(s.seal(b"z"))
        finally:
            subprocess.run = real
        priv = bytes(s._priv)
        self.assertTrue(seen)
        for args, fds in seen:
            blob = " ".join(args)
            self.assertNotIn(PW, blob)
            self.assertNotIn("PRIVATE KEY", blob)
            self.assertNotIn(res["recovery_key"], blob)
            self.assertNotIn(priv.decode(errors="ignore")[40:80], blob)
            for a in args:
                if a.startswith("/dev/fd/"):
                    self.assertIn(int(a.rsplit("/", 1)[1]), fds)

    def test_replace_scrubbing_zeroes_the_old_inode(self):
        p = self.tmp / "data.json"
        p.write_text("very secret plaintext")
        spy = self.tmp / "spy-link"
        os.link(p, spy)                                         # a second name for the OLD inode
        sealed.replace_scrubbing(p, "sealed now")
        self.assertEqual(p.read_text(), "sealed now")
        self.assertEqual(spy.read_bytes(), b"\0" * len("very secret plaintext"))
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual([x.name for x in self.tmp.glob(".*.scrub")], [])

    @unittest.skipUnless(OPENSSL3, "needs a second openssl (OpenSSL 3) to cross-check")
    def test_records_open_with_the_other_openssl(self):
        # macOS LibreSSL here, OpenSSL 3 on the Badger (Debian 13): the same files must work with both.
        rec = self.s.seal(b"portable")
        self.s.setup(PW)
        rec2 = self.s.seal(b"after setup")
        other = sealed.Sealer(self.tmp / "sealed", openssl=OPENSSL3, scrypt_n=1024)
        other.unlock(PW, "ip")
        self.assertEqual(other.open_(rec), b"portable")
        self.assertEqual(other.open_(rec2), b"after setup")
        rec3 = other.seal(b"from openssl 3")
        self.s.lock_all()
        self.s.unlock(PW, "ip")
        self.assertEqual(self.s.open_(rec3), b"from openssl 3")


class Maps(SealerBase):
    def test_sealed_map_write_only_while_locked(self):
        d = {}
        m = sealed.SealedMap(d, self.s)
        m.update("m_1", {"name": "Ana", "dev": {"d1": {"label": "phone"}}})
        self.assertTrue(sealed.is_token(d["sealed"]))
        self.s.setup(PW)
        self.s.lock_all()
        vals, complete = m.read()
        self.assertEqual((vals, complete), ({}, False))
        m.update("m_1", {"dev": {"d2": {"label": "tablet"}}})
        m.update("m_2", {"name": "Ben"})
        self.assertEqual(set(d["sealed_pending"]), {"m_1", "m_2"})
        self.assertEqual(m.read()[0], {"m_1": {"dev": {"d2": {"label": "tablet"}}}, "m_2": {"name": "Ben"}})
        self.assertFalse(m.fold())                              # nothing to fold while locked
        self.s.unlock(PW, "ip")
        self.assertEqual(m.read(), ({"m_1": {"name": "Ana", "dev": {"d1": {"label": "phone"}, "d2": {"label": "tablet"}}},
                                     "m_2": {"name": "Ben"}}, True))
        self.assertTrue(m.fold(live={"m_1"}))
        self.assertNotIn("sealed_pending", d)
        self.assertEqual(set(m.read()[0]), {"m_1"})
        m.update("m_1", {"dev": {"d1": None}, "name": None})
        self.assertEqual(m.read()[0], {"m_1": {"dev": {"d2": {"label": "tablet"}}}})
        m.remove("m_1")
        self.assertEqual(m.read()[0], {})

    def test_plain_map_and_legacy_plain_map_sealed_while_locked(self):
        d = {}
        plain = sealed.SealedMap(d, None)
        plain.update("a", {"x": 1})
        self.assertEqual(d["sealed"], {"a": {"x": 1}})
        self.s.setup(PW)
        self.s.lock_all()
        legacy = {"sealed": {"a": {"contact": "blue house, Oak St"}}}
        lm = sealed.SealedMap(legacy, self.s)
        self.assertTrue(lm.fold())
        self.assertNotIn(b"blue house", json.dumps(legacy).encode())
        self.s.unlock(PW, "ip")
        self.assertEqual(lm.read()[0], {"a": {"contact": "blue house, Oak St"}})

    def test_journal(self):
        self.s.setup(PW)
        self.s.lock_all()
        j = sealed.SealedJournal(self.tmp / "ops.jsonl", self.s)
        j.append({"op": "add", "n": 1})
        j.append({"op": "add", "n": 2})
        self.assertEqual(j.pending(), 2)
        self.assertEqual([o["n"] for o in j.recent()], [1, 2])
        self.assertEqual(j.take(), [])                          # locked: nothing to apply yet
        self.assertEqual(grep(self.tmp, b'"op"'), [])
        self.assertEqual((self.tmp / "ops.jsonl").stat().st_mode & 0o777, 0o600)
        self.s.lock_all()                                       # a later lock: still kept, not readable
        self.assertEqual(j.recent(), [])
        self.s.unlock(PW, "ip")
        self.assertEqual([o["n"] for o in j.take()], [1, 2])
        j.clear()
        self.assertEqual(j.pending(), 0)


class ModuleApi(SealerBase):
    def test_module_level_functions_for_other_code(self):
        s = sealed.use(self.tmp / "sealed2", openssl=OPENSSL, scrypt_n=1024)
        self.assertIs(sealed.current(), s)
        self.assertIs(sealed.use(self.tmp / "sealed2"), s)
        rec = sealed.seal(b"api")
        self.assertTrue(sealed.is_unlocked())
        self.assertEqual(sealed.open_(rec), b"api")
        s.setup(PW)
        sealed.lock_all()
        with self.assertRaises(sealed.Locked):
            sealed.open_(rec)
        gone = sealed.destroy_keys()
        self.assertTrue(any("sealed2/device.key" in g for g in gone))
        self.assertTrue(json.loads(sealed.seal_jsonl_line({"a": 1}))["s"].startswith("hbs1:"))


if __name__ == "__main__":
    unittest.main()
