#!/usr/bin/env python3
"""
Duress — unit tests: the made-up household (software/common/decoy.py), the sealed records' hard lock and duress
passphrase (software/common/sealed.py), the vault's hard lock (features/vault.py), the wipe engine's duress
action (os/wipe.py: "lock" = flags only; "wipe" = the panic wipe + the made-up household in place) and the
front panel in a duress (software/panel/panel.py). Over HTTP: tests/test_duress_portal.py.

    python3.11 tests/test_duress.py -v
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "common"))
sys.path.insert(0, str(REPO / "software" / "portal"))
import decoy  # noqa: E402
import sealed  # noqa: E402

OPENSSL = "/usr/bin/openssl" if Path("/usr/bin/openssl").exists() else shutil.which("openssl")
PW = "purple tractor sings loudly"
DURESS = "seven blue kettles whistle"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vault = load("duress_test_vault", REPO / "software/portal/features/vault.py")
wipe = load("duress_test_wipe", REPO / "os/wipe.py")
TELLS = re.compile(r"duress|decoy|wipe|dead|made.?up|fake|test", re.I)
ORDINARY_PLACES = {"Garage", "Pantry", "Kitchen cupboard", "Bathroom cabinet", "Hall closet", "Kitchen drawer"}


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="duress-")).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ───────────────────────────── the made-up household ─────────────────────────────

class Profile(TmpCase):
    def test_plausible_ordinary_household(self):
        for seed in range(30):
            p = decoy.generate(seed)
            names = [s["name"] for s in p["supplies"]]
            text = json.dumps(p)
            self.assertFalse(TELLS.search(text), (seed, TELLS.search(text)))
            for must in ("Bottled water", "Canned beans", "Rice", "Pasta", "First-aid kit", "AA batteries",
                         "LED flashlight", "Propane canister"):
                self.assertTrue(any(n.startswith(must) for n in names), (seed, must))
            self.assertTrue(10 <= len(p["supplies"]) <= 16, len(p["supplies"]))
            for s in p["supplies"]:
                self.assertIn(s["location"], ORDINARY_PLACES, "ordinary places only, no hiding places")
                self.assertLessEqual(s["qty"], 24, "modest amounts")
                if s["expires_in_days"] is not None:
                    self.assertTrue(30 <= s["expires_in_days"] <= 1800, s)
            water = [s for s in p["supplies"] if s["category"] == "water"]
            self.assertLessEqual(sum(s["qty"] * s["litres"] for s in water), 60, "a couple of cases, not a cistern")
            self.assertTrue(8 <= len(p["board"]) <= 12)
            for b in p["board"]:
                self.assertTrue(0 <= b["day"] < decoy.POST_DAYS and 7 * 60 <= b["minute"] <= 21 * 60 + 30)
                self.assertFalse(re.search(r"\b(away|gone|empty|leaving|vacation|holiday|street|road|ave)\b", b["text"], re.I))
            self.assertEqual(len(p["names"]), 12)
            self.assertEqual(len({n.lower() for n in p["names"]}), 12)

    def test_seeded_per_device_and_stable(self):
        a, b, c = decoy.generate(7), decoy.generate(7), decoy.generate(8)
        self.assertEqual({k: v for k, v in a.items() if k != "created"}, {k: v for k, v in b.items() if k != "created"})
        self.assertNotEqual(a["names"], c["names"])
        self.assertNotEqual(decoy.generate()["seed"], decoy.generate()["seed"])
        self.assertEqual(decoy.normalize(a), decoy.normalize(decoy.normalize(a)), "idempotent: a save keeps it")

    def test_owner_edits_are_cleaned_and_capped(self):
        p = decoy.generate(1)
        p["names"] = ["  Ana‮ ", "ana", "B" * 100, 5, None] + [f"N{i}" for i in range(40)]
        p["supplies"] = [{"name": "x" * 500, "qty": "1e9", "category": "weapons", "expires_in_days": "99999"},
                         {"name": "", "qty": 1}, "junk", {"name": "Rice", "qty": -5, "unit": "kg\x00"}] * 30
        p["board"] = ["\x07Hello", {"text": "y" * 5000}, {"text": ""}] * 30
        n = decoy.normalize(p)
        self.assertEqual(n["names"][0], "Ana")
        self.assertNotIn("ana", n["names"][1:2], "no duplicate names")
        self.assertTrue(all(len(x) <= decoy.MAX_NAME for x in n["names"]))
        self.assertLessEqual(len(n["names"]), decoy.MAX_NAMES)
        self.assertLessEqual(len(n["supplies"]), decoy.MAX_SUPPLIES)
        s = n["supplies"][0]
        self.assertEqual(len(s["name"]), 60)
        self.assertEqual(s["category"], "other")
        self.assertEqual(s["qty"], 100000)
        self.assertEqual(s["expires_in_days"], 3650)
        self.assertEqual(n["supplies"][1]["qty"], 0)
        self.assertEqual(n["supplies"][1]["unit"], "kg")
        self.assertLessEqual(len(n["board"]), decoy.MAX_POSTS)
        self.assertTrue(all(len(b["text"]) <= decoy.MAX_POST and "\x07" not in b["text"] for b in n["board"]))
        with self.assertRaises(ValueError):
            decoy.normalize(["not", "a", "dict"])

    def test_ensure_makes_once_and_survives_damage(self):
        f = self.tmp / "portal" / "decoy.json"
        p = decoy.ensure(f)
        self.assertEqual(f.stat().st_mode & 0o777, 0o640)
        self.assertEqual(decoy.ensure(f)["seed"], p["seed"], "kept, not remade")
        f.write_text("{damaged")
        q = decoy.ensure(f)
        self.assertTrue(q["supplies"] and q["board"])
        self.assertEqual(decoy.load(f)["seed"], q["seed"])
        ro = self.tmp / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            self.assertTrue(decoy.ensure(ro / "decoy.json")["supplies"], "a read-only disk still gets a decoy")
        finally:
            ro.chmod(0o700)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(decoy.main(["ensure", str(f)]), 0)
            self.assertEqual(decoy.main(["bogus"]), 2)


class Materialize(TmpCase):
    def members_file(self):
        f = self.tmp / "members.json"
        f.write_text(json.dumps({"members": {
            "m_aaaa1111": {"created": 100, "devices": [{"id": "d_1", "key": "a" * 64, "created": 101},
                                                       {"id": "d_2", "key": "b" * 64, "created": 102, "pending": True,
                                                        "expires": 9e9}],
                           "radios": ["!0a0b0c0d"], "sealed": "hbs1:xx"},
            "m_bbbb2222": {"created": 200, "devices": [{"id": "d_3", "key": "c" * 64}], "radios": ["bad"]},
            "m_cccc3333": {"created": 50, "removed": 300, "devices": [{"id": "d_9", "key": "d" * 64}]},
            "../evil": {"created": 1, "devices": []},
            "m_dddd4444": "junk"}}))
        return f

    def test_skeleton_keeps_hashes_never_names(self):
        sk = decoy.member_skeleton(self.members_file())
        self.assertEqual([m["id"] for m in sk], ["m_aaaa1111", "m_bbbb2222"], "active members, oldest first")
        self.assertEqual([d["id"] for d in sk[0]["devices"]], ["d_1", "d_2"])
        self.assertTrue(sk[0]["devices"][1]["pending"], "a device waiting for approval keeps waiting")
        self.assertEqual(sk[0]["radios"], ["!0a0b0c0d"])
        self.assertEqual(sk[1]["radios"], [])
        self.assertEqual(decoy.member_skeleton(self.tmp / "missing.json"), [])
        (self.tmp / "bad.json").write_text("[")
        self.assertEqual(decoy.member_skeleton(self.tmp / "bad.json"), [])

    def test_working_copy(self):
        prof = decoy.generate(3)
        sk = decoy.member_skeleton(self.members_file())
        board_src = self.tmp / "board-src.jsonl"
        board_src.write_text("\n".join(json.dumps(x) for x in (
            {"at": "x", "name": "Real Name A", "text": "hi", "member": "m_aaaa1111"},
            {"at": "x", "name": "Removed Person", "text": "bye", "member": "m_cccc3333"},
            {"at": "x", "name": "Visitor", "text": "anyone have water?"},
            {"at": "x", "name": "Supplies", "text": "note", "member": "feature-supplies"})) + "\nnot json\n")
        d = self.tmp / "work"
        anchor = time.time()
        paths = {"members": d / "members.json", "family_board": d / "family-board.jsonl", "board": d / "board.jsonl",
                 "posture": d / "posture.json", "supplies": d / "features/supplies/supplies.json"}
        out = decoy.materialize(prof, paths, sk, anchor, community_src=board_src)
        m = json.loads(paths["members"].read_text())["members"]
        self.assertEqual(sorted(m), ["m_aaaa1111", "m_bbbb2222"], "the same number of members, same ids")
        self.assertEqual(m["m_aaaa1111"]["devices"][0]["key"], "a" * 64, "family phones keep working")
        self.assertEqual([m[k]["name"] for k in ("m_aaaa1111", "m_bbbb2222")], prof["names"][:2])
        self.assertEqual(out["names"]["m_aaaa1111"], prof["names"][0])
        posts = [json.loads(x) for x in paths["family_board"].read_text().splitlines()]
        self.assertEqual(len(posts), len(prof["board"]))
        times = [time.mktime(time.strptime(p["at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone for p in posts]
        self.assertEqual(times, sorted(times))
        for p in posts:
            t = dt.datetime.strptime(p["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
            self.assertTrue(anchor - (decoy.POST_DAYS + 1) * 86400 < t < anchor, p)
            self.assertIn(p["member"], m)
            self.assertEqual(p["name"], m[p["member"]]["name"])
        com = [json.loads(x) for x in paths["board"].read_text().splitlines()]
        self.assertEqual([c["name"] for c in com], [prof["names"][0], "Visitor", "Supplies"],
                         "member names mapped, a removed member's real name dropped")
        self.assertEqual(json.loads(paths["posture"].read_text()), {"level": "guarded"})
        sup = json.loads(paths["supplies"].read_text())
        today = dt.date.fromtimestamp(anchor).isoformat()
        self.assertEqual(len(sup["items"]), len(prof["supplies"]))
        for it in sup["items"].values():
            self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{6,32}", next(k for k, v in sup["items"].items() if v is it)))
            if it["expiry"]:
                self.assertGreater(it["expiry"], today, "dates in the near future")
        self.assertEqual(sup["household"]["people"], 2)
        self.assertTrue(all(r["at"] < anchor for r in sup["log"]))
        for p in paths.values():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_no_members_still_plausible(self):
        d = self.tmp / "w"
        decoy.materialize(decoy.generate(4), {"members": d / "m.json", "family_board": d / "f.jsonl",
                                              "supplies": d / "s.json"}, [])
        self.assertEqual(json.loads((d / "m.json").read_text())["members"], {})
        self.assertTrue((d / "f.jsonl").read_text().strip())
        self.assertTrue(json.loads((d / "s.json").read_text())["items"])

    def test_state_helpers(self):
        self.assertEqual(decoy.sealed_dir({"sealed": {"dir": "/x/sealed"}}), Path("/x/sealed"))
        self.assertEqual(decoy.sealed_dir({"portal": {"members_file": "/y/members.json"}}), Path("/y/sealed"))
        self.assertEqual(decoy.sealed_dir({"sealed": {"dir": "relative"}}), Path(decoy.DEFAULT_SEALED_DIR))
        self.assertEqual(decoy.duress_action({}), "lock", "lock is the default")
        self.assertEqual(decoy.duress_action({"security": {"duress_action": "wipe"}}), "wipe")
        self.assertEqual(decoy.duress_action({"security": {"duress_action": "boom"}}), "lock")
        st = self.tmp / "settings.json"
        st.write_text(json.dumps({"duress_action": "wipe"}))
        self.assertEqual(decoy.duress_action({"security": {"duress_action": "lock"}}, st), "wipe", "the admin page wins")
        (self.tmp / "device.key").write_text(json.dumps({"hard_lock": {"at": 123}}))
        self.assertEqual(decoy.hard_locked(self.tmp), 123.0)
        (self.tmp / "device.key").write_text("{")
        self.assertIsNone(decoy.hard_locked(self.tmp))
        self.assertIsNone(decoy.hard_locked(self.tmp / "nope"))


# ───────────────────────────── sealed records ─────────────────────────────

@unittest.skipUnless(OPENSSL, "needs openssl")
class SealedDuress(TmpCase):
    def setUp(self):
        super().setUp()
        self.s = sealed.Sealer(self.tmp / "sealed", openssl=OPENSSL, scrypt_n=1024)
        self.rec = self.s.seal(b"insulin in the cellar")
        self.rk = self.s.setup(PW)["recovery_key"]
        self.rec2 = self.s.seal(b"second record")
        self.s.lock_all()

    def test_hard_lock_needs_the_recovery_key_then_the_passphrase_works_again(self):
        self.assertEqual(self.s.unlock(PW), "passphrase")
        files = sorted(p.name for p in (self.tmp / "sealed").rglob("*") if p.is_file())
        self.assertTrue(self.s.hard_lock())
        self.assertFalse(self.s.is_unlocked())
        self.assertTrue(self.s.hard_locked())
        with self.assertRaises(sealed.Locked):
            self.s.open_(self.rec)
        self.assertEqual(self.s.unlock(PW), "hard-locked", "the right passphrase opens nothing")
        self.assertFalse(self.s.is_unlocked())
        with self.assertRaises(sealed.Refused) as cm:
            self.s.change_passphrase(PW, "another long passphrase here")
        self.assertEqual(cm.exception.status, 423)
        with self.assertRaises(sealed.Refused):
            self.s.new_recovery_key(PW)
        self.assertEqual(sorted(p.name for p in (self.tmp / "sealed").rglob("*") if p.is_file()), files,
                         "nothing destroyed")
        self.assertNotIn(b"cellar", (self.tmp / "sealed/device.key").read_bytes())
        self.assertEqual(self.s.unlock(self.rk), "recovery")
        self.assertFalse(self.s.hard_locked())
        self.assertEqual(self.s.open_(self.rec), b"insulin in the cellar")
        self.s.lock_all()
        self.assertEqual(self.s.unlock(PW), "passphrase", "the passphrase works again")
        self.assertEqual(self.s.open_(self.rec2), b"second record")

    def test_recover_with_a_new_passphrase_ends_the_hard_lock(self):
        self.s.hard_lock()
        self.s.recover(self.rk, "a brand new owner passphrase")
        self.assertTrue(self.s.is_unlocked())
        self.assertFalse(self.s.hard_locked())

    def test_hard_lock_from_another_process(self):
        self.s.unlock(PW)
        st = (self.tmp / "sealed/device.key").stat()
        self.assertTrue(sealed.hard_lock_in(self.tmp / "sealed"))
        self.assertTrue(sealed.hard_lock_in(self.tmp / "sealed"), "idempotent")
        self.assertEqual((self.tmp / "sealed/device.key").stat().st_mode & 0o777, st.st_mode & 0o777)
        self.assertFalse(self.s.is_unlocked(), "a running launcher forgets its keys at its next look")
        self.assertEqual(decoy.hard_locked(self.tmp / "sealed") is not None, True)
        self.assertFalse(sealed.hard_lock_in(self.tmp / "nothing"))

    def test_hard_lock_needs_setup(self):
        s2 = sealed.Sealer(self.tmp / "s2", openssl=OPENSSL, scrypt_n=1024)
        s2.seal(b"x")
        self.assertFalse(s2.hard_lock(), "no recovery key to come back with")
        self.assertFalse(sealed.hard_lock_in(self.tmp / "s2"))
        self.assertFalse(s2.hard_locked())

    def test_duress_passphrase(self):
        with self.assertRaises(sealed.Refused):
            self.s.set_duress("wrong passphrase entirely", DURESS)
        with self.assertRaises(sealed.Refused) as cm:
            self.s.set_duress(PW, "short")
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(sealed.Refused) as cm:
            self.s.set_duress(PW, PW)
        self.assertIn("different", cm.exception.message)
        self.assertFalse(self.s.has_duress())
        self.s.set_duress(PW, DURESS)
        self.assertTrue(self.s.has_duress())
        self.assertNotIn(DURESS.encode(), (self.tmp / "sealed/device.key").read_bytes())
        self.assertEqual(self.s.unlock(DURESS), "duress")
        self.assertFalse(self.s.is_unlocked(), "the duress passphrase unlocks nothing")
        self.assertEqual(self.s.check_secret(DURESS), "duress")
        self.assertEqual(self.s.check_secret(PW), "passphrase")
        self.assertEqual(self.s.check_secret(self.rk), "recovery")
        for _ in range(8):
            self.assertEqual(self.s.unlock(DURESS), "duress")
        self.assertEqual(self.s.wait(""), 0, "never counted as a wrong try")
        with self.assertRaises(sealed.DuressSecret):
            self.s.change_passphrase(DURESS, "something else entirely long")
        with self.assertRaises(sealed.Refused):
            self.s.change_passphrase(PW, DURESS)
        self.s.clear_duress(PW)
        self.assertFalse(self.s.has_duress())
        with self.assertRaises(sealed.Refused):
            self.s.unlock(DURESS)

    def test_same_work_for_every_secret(self):
        """Every passphrase try runs the same scrypts (count and cost), whichever secret, duress set or not."""
        def count(secret, with_duress):
            if with_duress and not self.s.has_duress():
                self.s.set_duress(PW, DURESS)
            if not with_duress and self.s.has_duress():
                self.s.clear_duress(PW)
            self.s.lock_all()
            seen = []
            real = hashlib.scrypt

            def spy(*a, **k):
                seen.append((k.get("n"), k.get("r"), k.get("p")))
                return real(*a, **k)
            with mock.patch.object(hashlib, "scrypt", spy):
                try:
                    res = self.s.unlock(secret)
                except sealed.Refused:
                    res = "bad"
            self.s._fails.clear()
            return res, seen
        a = count(PW, True)
        d = count(DURESS, True)
        w = count("not the passphrase at all", True)
        n = count("not the passphrase at all", False)
        p = count(PW, False)
        self.assertEqual([a[0], d[0], w[0], n[0], p[0]], ["passphrase", "duress", "bad", "bad", "passphrase"])
        self.assertEqual(len(a[1]), 2)
        for x in (d, w, n, p):
            self.assertEqual(x[1], a[1])

    def test_decoy_sealer_reports_like_the_real_one(self):
        self.s.unlock(PW)
        real_st = self.s.status("1.2.3.4")
        ds = sealed.DecoySealer(self.tmp / "decoy-sealed", real=self.s, held=True, openssl=OPENSSL, scrypt_n=1024)
        self.assertFalse(ds.is_unlocked())
        st = ds.status("1.2.3.4")
        self.assertEqual(sorted(st), sorted(real_st), "the same fields")
        self.assertEqual((st["setup"], st["protected"], st["provisional"], st["fingerprint"], st["setup_at"]),
                         (True, True, False, real_st["fingerprint"], real_st["setup_at"]))
        self.assertFalse(st["unlocked"])
        with ds.unheld():
            tok = ds.seal_json({"x": 1})
            self.assertEqual(ds.open_json(tok), {"x": 1})
        with self.assertRaises(sealed.Locked):
            ds.open_json(tok)
        ds.open_up()
        self.assertTrue(ds.status()["unlocked"])
        self.assertEqual(ds.open_json(tok), {"x": 1})
        ds.lock_all()
        self.assertFalse(ds.is_unlocked())
        self.assertFalse(ds.hard_locked())
        self.assertIsNone(sealed._instances.get(str((self.tmp / "decoy-sealed").resolve())), "never a module instance")


# ───────────────────────────── the vault ─────────────────────────────

class Body:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        out, self.data = self.data[:n], self.data[n:]
        return out


@unittest.skipUnless(OPENSSL, "needs openssl")
class VaultHardLock(TmpCase):
    def setUp(self):
        super().setUp()
        self.v = vault.Vault(self.tmp / "vault", {"scrypt_n": 1024, "openssl": OPENSSL})
        self.ana = {"id": "m_ana", "device": "d_1", "name": "Ana"}
        self.ben = {"id": "m_ben", "device": "d_2", "name": "Ben"}
        self.rk = self.v.setup(self.ana, PW, "ip")
        self.v.add_note(self.ana, {"name": "Passports", "category": "id", "text": "in the safe"})
        self.v.unlock(self.ben, PW, "ip")

    def refused(self, status, fn, *a):
        with self.assertRaises(vault.Refused) as cm:
            fn(*a)
        self.assertEqual(cm.exception.status, status, cm.exception.message)
        return cm.exception

    def test_only_the_recovery_key_then_the_passphrase_again(self):
        files = sorted(p.name for p in (self.tmp / "vault").rglob("*") if p.is_file())
        self.assertTrue(vault.hard_lock_dir(self.tmp / "vault"))
        self.assertTrue(self.v.hard_locked())
        self.refused(423, self.v.cipher, self.ana)
        self.refused(423, self.v.cipher, self.ben)
        e = self.refused(423, self.v.unlock, self.ana, PW, "ip")
        self.assertTrue(e.extra.get("hard_locked"))
        self.assertIn("recovery key", e.message)
        self.refused(403, self.v.unlock, self.ana, "not the passphrase", "ip")
        self.assertTrue(self.v.status(self.ana, "ip")["hard_locked"])
        self.assertEqual(sorted(p.name for p in (self.tmp / "vault").rglob("*") if p.is_file()), files, "nothing destroyed")
        self.v.unlock(self.ana, self.rk, "ip")
        self.assertFalse(self.v.hard_locked())
        self.assertEqual(len(self.v.list(self.ana)["files"]), 1)
        self.v.unlock(self.ben, PW, "ip")
        self.v.cipher(self.ben)

    def test_forgot_passphrase_flow_ends_it_too(self):
        self.v.hard_lock()
        self.v.recover(self.ana, self.rk, "another family passphrase", "ip")
        self.assertFalse(self.v.hard_locked())
        self.v.cipher(self.ana)

    def test_flag_from_another_process(self):
        path = self.tmp / "vault" / "vault.json"
        mode = path.stat().st_mode & 0o777
        self.assertTrue(vault.vc.hard_lock_file(path))
        self.assertTrue(vault.vc.hard_lock_file(path))
        self.assertEqual(path.stat().st_mode & 0o777, mode)
        self.refused(423, self.v.cipher, self.ben)
        self.assertFalse(vault.vc.hard_lock_file(self.tmp / "none.json"))


# ───────────────────────────── the wipe engine ─────────────────────────────

class FakeWipe(wipe.Wipe):
    """No system commands: everything the engine would run is recorded."""

    def __init__(self, *a, **k):
        self.ran = []
        super().__init__(*a, **k)

    def run(self, *argv, timeout=20, input=None):  # noqa: A002
        self.ran.append(list(argv))
        return 0


@unittest.skipUnless(OPENSSL, "needs openssl")
class WipeEngine(TmpCase):
    def setUp(self):
        super().setUp()
        r = self.root = self.tmp / "root"
        self.pdir = r / "var/lib/prepper/portal"
        (self.pdir / "features/vault").mkdir(parents=True)
        (r / "run/prepper").mkdir(parents=True)
        (r / "etc/prepper").mkdir(parents=True)
        self.conf = r / "etc/prepper/prepper.toml"
        self.conf.write_text('[network]\npassphrase = "OldPassw0rd!"\n')
        s = sealed.Sealer(self.pdir / "sealed", openssl=OPENSSL, scrypt_n=1024)
        self.tok = s.seal_text("Lucia Garcia, insulin in the cellar")
        self.rk = s.setup(PW)["recovery_key"]
        self.tok = s.seal_text("Lucia Garcia, insulin in the cellar")
        v = vault.Vault(self.pdir / "features/vault", {"scrypt_n": 1024, "openssl": OPENSSL})
        self.vrk = v.setup({"id": "m_aaaa1111", "device": "d_1"}, PW, "ip")
        (self.pdir / "members.json").write_text(json.dumps({"members": {"m_aaaa1111": {
            "created": 1, "devices": [{"id": "d_1", "key": "e" * 64, "created": 1}], "radios": [], "sealed": self.tok}}}))
        (self.pdir / "family-board.jsonl").write_text(json.dumps({"s": self.tok}) + "\n")
        (self.pdir / "posture.json").write_text('{"level": "neighbourly"}')
        self.before = {p: p.read_bytes() for p in self.pdir.rglob("*") if p.is_file() and "sealed" not in p.parts
                       and p.name != "vault.json"}

    def engine(self, trigger="duress", action=None):
        if action:
            (self.pdir / "portal-settings.json").write_text(json.dumps({"duress_action": action}))
        return FakeWipe(str(self.root), str(self.conf), "/nonexistent/prepperctl", "wipe", trigger)

    def test_lock_is_the_default_and_touches_nothing_but_two_flags(self):
        for trig in ("duress", "panel-duress"):
            w = self.engine(trig)
            self.assertEqual(w.duress_action, "lock")
            self.assertEqual(w.run_all(), 0)
            self.assertEqual(w.ran, [], "no systemctl, no nmcli, no journalctl, no restart")
            self.assertEqual(w.summary(), "done")
        self.assertIsNotNone(decoy.hard_locked(self.pdir / "sealed"))
        self.assertTrue(vault.vc.is_hard_locked(json.loads((self.pdir / "features/vault/vault.json").read_text())))
        for p, b in self.before.items():
            self.assertEqual(p.read_bytes(), b, f"{p.name} untouched")
        self.assertEqual(self.conf.read_text(), '[network]\npassphrase = "OldPassw0rd!"\n', "Wi-Fi untouched")
        self.assertFalse((self.root / "var/lib/prepper/wipe-pending.json").exists(), "no resume marker")
        self.assertFalse((self.root / "run/prepper/wipe-receipt.json").exists(), "no receipt for wipe-status")
        # and it is all recoverable
        s = sealed.Sealer(self.pdir / "sealed", openssl=OPENSSL, scrypt_n=1024)
        self.assertEqual(s.unlock(PW), "hard-locked")
        self.assertEqual(s.unlock(self.rk), "recovery")
        self.assertEqual(s.open_text(self.tok), "Lucia Garcia, insulin in the cellar")
        v = vault.Vault(self.pdir / "features/vault", {"scrypt_n": 1024, "openssl": OPENSSL})
        v.unlock({"id": "m_x", "device": "d"}, self.vrk, "ip")

    def test_admin_and_panel_wipes_are_not_affected(self):
        w = self.engine("admin")
        self.assertFalse(w.duress)
        w2 = self.engine("panel-duress", "wipe")
        self.assertEqual(w2.duress_action, "wipe")

    def test_wipe_mode_puts_the_made_up_household_in_place(self):
        w = self.engine("duress", "wipe")
        with mock.patch.object(FakeWipe, "schedule_b", return_value=True):
            w.run_all()
        self.assertEqual(json.loads((self.root / "var/lib/prepper/wipe-pending.json").read_text())["stage"], "b")
        self.assertTrue(w.steps["decoy"].startswith("made-up household in place (1"), w.steps)
        m = json.loads((self.pdir / "members.json").read_text())["members"]
        self.assertEqual(list(m), ["m_aaaa1111"], "same member id: the family phone keeps working")
        self.assertEqual(m["m_aaaa1111"]["devices"][0]["key"], "e" * 64)
        prof = decoy.load(self.pdir / "decoy.json")
        self.assertEqual(m["m_aaaa1111"]["name"], prof["names"][0])
        self.assertTrue((self.pdir / "features/supplies/supplies.json").exists())
        self.assertTrue((self.pdir / "family-board.jsonl").read_text().strip())
        left = [p for p in self.pdir.rglob("*") if p.is_file() and b"Lucia" in p.read_bytes()]
        self.assertEqual(left, [])
        self.assertEqual(json.loads((self.pdir / "posture.json").read_text())["level"], "neighbourly",
                         "no visible change before the delay")
        self.assertFalse((self.pdir / "sealed" / "device.key").exists(), "the keys went first")

    def test_resume_of_an_interrupted_wipe_never_becomes_a_lock(self):
        (self.root / "var/lib/prepper/wipe-pending.json").write_text(json.dumps(
            {"v": 1, "mode": "wipe", "trigger": "duress", "stage": "a", "b_due": None, "started": 1}))
        args = type("A", (), {"root": str(self.root), "conf": str(self.conf), "ctl": "/nonexistent"})()
        with mock.patch.object(wipe, "Wipe", FakeWipe), mock.patch.object(FakeWipe, "schedule_b", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            wipe.resume(args, boot=True)
        self.assertFalse((self.pdir / "sealed" / "device.key").exists(), "the wipe went on")


# ───────────────────────────── the front panel ─────────────────────────────

class PanelDecoy(TmpCase):
    def setUp(self):
        super().setUp()
        os.environ["PANEL_LOCK"] = str(self.tmp / "panel-lock.json")
        self.panel = load("duress_test_panel", REPO / "software/panel/panel.py")
        self.sdir = self.tmp / "sealed"
        self.sdir.mkdir()
        (self.sdir / "device.key").write_text(json.dumps({"pub": "x", "priv": "y", "wraps": {"pass": {"kdf": {}}}}))
        link = self.tmp / "mesh-channel.json"
        link.write_text(json.dumps({"url": "https://meshtastic.org/e/#FAMILYPSK"}))
        pin = self.tmp / "mesh-bt-pin.json"
        pin.write_text(json.dumps({"pin": "482913"}))
        cfg = {"portal": {"mesh_channel_link": str(link), "mesh_bt_pin_file": str(pin)},
               "sealed": {"dir": str(self.sdir)}}

        class Disp:
            def __init__(s):
                s.out, s.qrs = [], []

            def lines(s, rows, title=None, sel=None):
                s.out.append((title, list(rows)))

            def qr(s, data, caption):
                s.qrs.append((data, caption))

            def sleep(s, on):
                pass
        self.d = Disp()
        with mock.patch.object(self.panel, "api", return_value={}):
            self.p = self.panel.Panel(self.d, cfg)
        self.p.spawn = lambda fn: fn()

    def tearDown(self):
        os.environ.pop("PANEL_LOCK", None)
        super().tearDown()

    def test_secrets_vanish_while_hard_locked(self):
        self.p.radio_link_qr()
        self.assertIn("FAMILYPSK", self.d.qrs[-1][0])
        self.p.bt_pin()
        self.assertIn("482913", self.d.out[-1][1][0] + " ".join(self.d.out[-1][1]))
        sealed.hard_lock_in(self.sdir)          # (needs wraps.pass: present)
        self.assertTrue(self.p.in_decoy())
        n = len(self.d.qrs)
        self.p.radio_link_qr()
        self.assertEqual(len(self.d.qrs), n, "no radio key QR in a duress")
        self.p.bt_pin()
        self.assertNotIn("482913", " ".join(self.d.out[-1][1]))
        self.assertEqual(self.p.approve_entries(), [])
        self.assertIsNone(self.p.deadman_page())

    def test_wipe_only_pretends_after_the_duress_code(self):
        self.p.decoy = True
        with mock.patch.object(self.panel, "ctl_output") as ctl, mock.patch.object(self.panel.time, "sleep"):
            self.p.do_wipe("panic-wipe", "panel")
        ctl.assert_not_called()
        self.assertIn("Wiped", " ".join(self.d.out[-1][1]))

    def test_recovery_key_at_the_knob(self):
        rk = "ABCD" * 13
        self.p.ask_code("rkey")
        self.assertEqual(self.p.code_choices()[:3], ["A", "B", "C"])
        self.p.code_buf = rk
        self.p.code_sel = self.p.code_choices().index("OK")
        self.p.decoy = True
        with mock.patch.object(self.panel, "api_post", return_value={"ok": True, "via": "recovery"}) as post:
            self.p.code_press()
        post.assert_called_once_with("/api/sealed", {"op": "unlock", "secret": rk})
        self.assertFalse(self.p.decoy)
        self.assertIn("recovery", " ".join(self.d.out[-1][1]))
        self.p.ask_code("rkey")
        self.p.code_buf = "ABC"
        self.p.code_sel = self.p.code_choices().index("OK")
        with mock.patch.object(self.panel, "api_post") as post:
            self.p.code_press()
        post.assert_not_called()
        self.p.ask_code("rkey")
        for _ in range(60):
            self.p.code_sel = 0
            self.p.code_press()
        self.assertEqual(len(self.p.code_buf), 52, "at most 52 characters")


if __name__ == "__main__":
    unittest.main()
