"""
Family document vault: crypto (wrap/rewrap, chunked encrypt-then-MAC through openssl, tamper
detection), sessions and auto-lock, brute-force limits, quotas, no plaintext on disk, and the
HTTP API through the real portal (access by member / admin / visitor, uploads, downloads,
export bundle and the offline tool).
Run: python3.11 tests/test_vault.py
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import http.client
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
FEATS = REPO / "software" / "portal" / "features"
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "software" / "common"))
os.environ["PORTAL_QUIET"] = "1"

import server  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vault = load("test_vault_feature", FEATS / "vault.py")
vc = vault.vc
OPENSSL = "/usr/bin/openssl" if Path("/usr/bin/openssl").exists() else shutil.which("openssl")
PW = "purple tractor sings loudly"
MARK = b"SECRET-PASSPORT-CONTENT-7f3a9c"
NAME_MARK = "Grandma-passport-scan-QX81"
NOTE_MARK = "policy number 99887766"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Body:
    """A fake request for receive_upload."""
    def __init__(self, data: bytes, declared: int | None = None):
        self.rfile = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data) if declared is None else declared)}
        self.close_connection = False


def scan(folder: Path, needles) -> list[str]:
    hits = []
    for p in folder.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            for n in needles:
                if n in data:
                    hits.append(f"{p}: {n!r}")
    return hits


@unittest.skipUnless(OPENSSL, "needs the openssl program")
class Crypto(unittest.TestCase):
    def setUp(self):
        self.c = vc.Cipher(os.urandom(32), OPENSSL)

    def test_roundtrip_sizes(self):
        for n in (0, 1, 100, vc.CHUNK - 1, vc.CHUNK, vc.CHUNK + 1, 2 * vc.CHUNK + 12345):
            data = os.urandom(n)
            blob = self.c.encrypt_bytes(data, vc.KIND_FILE)
            self.assertEqual(self.c.decrypt_bytes(blob, vc.KIND_FILE), data, n)
            self.assertEqual(self.c.verify(io.BytesIO(blob), vc.KIND_FILE), n)
            if n > 16:
                self.assertNotIn(data[:16], blob)

    def test_same_plaintext_encrypts_differently(self):
        a, b = self.c.encrypt_bytes(MARK, vc.KIND_FILE), self.c.encrypt_bytes(MARK, vc.KIND_FILE)
        self.assertNotEqual(a[vc.HEADER_LEN:], b[vc.HEADER_LEN:])

    def test_tamper_anywhere_is_detected(self):
        data = os.urandom(vc.CHUNK + 5000)
        blob = bytearray(self.c.encrypt_bytes(data, vc.KIND_FILE))
        for pos in (0, 9, vc.HEADER_LEN + 2, vc.HEADER_LEN + 100, vc.HEADER_LEN + vc.CHUNK + 10, len(blob) - 40, len(blob) - 1):
            bad = bytearray(blob)
            bad[pos] ^= 0x01
            with self.assertRaises(vc.CryptoError, msg=f"flip at {pos}"):
                self.c.decrypt_bytes(bytes(bad), vc.KIND_FILE)

    def test_truncation_reorder_and_extra_data(self):
        data = os.urandom(2 * vc.CHUNK + 10)
        blob = self.c.encrypt_bytes(data, vc.KIND_FILE)
        rec = 5 + vc.CHUNK + 32
        h, c0, c1, c2 = blob[:vc.HEADER_LEN], blob[vc.HEADER_LEN:vc.HEADER_LEN + rec], \
            blob[vc.HEADER_LEN + rec:vc.HEADER_LEN + 2 * rec], blob[vc.HEADER_LEN + 2 * rec:]
        for bad in (h + c0 + c1, h + c1 + c0 + c2, h + c0 + c2, blob + b"x", blob[:-1], h):
            with self.assertRaises(vc.CryptoError):
                self.c.decrypt_bytes(bad, vc.KIND_FILE)
        # a non-final chunk marked final (truncation disguised): the flag is under the MAC
        forged = bytearray(h + c0)
        forged[vc.HEADER_LEN] = 1
        with self.assertRaises(vc.CryptoError):
            self.c.decrypt_bytes(bytes(forged), vc.KIND_FILE)

    def test_nothing_released_before_the_chunk_checks_out(self):
        data = os.urandom(vc.CHUNK + 10)
        blob = bytearray(self.c.encrypt_bytes(data, vc.KIND_FILE))
        blob[-40] ^= 1                                    # damage the second chunk
        it = self.c.decrypt_iter(io.BytesIO(bytes(blob)), vc.KIND_FILE)
        self.assertEqual(next(it), data[:vc.CHUNK])        # first chunk is authentic
        with self.assertRaises(vc.CryptoError):
            next(it)
        with self.assertRaises(vc.CryptoError):           # and verify() catches it before any byte goes out
            self.c.verify(io.BytesIO(bytes(blob)), vc.KIND_FILE)

    def test_wrong_key_kind_and_blob_id(self):
        blob = self.c.encrypt_bytes(MARK, vc.KIND_FILE)
        with self.assertRaises(vc.CryptoError):
            vc.Cipher(os.urandom(32), OPENSSL).decrypt_bytes(blob, vc.KIND_FILE)
        with self.assertRaises(vc.CryptoError):
            self.c.decrypt_bytes(blob, vc.KIND_INDEX)       # a file can't pose as the index
        with self.assertRaises(vc.CryptoError):
            self.c.decrypt_bytes(blob, vc.KIND_FILE, os.urandom(16))
        bid = blob[len(vc.MAGIC) + 1:vc.HEADER_LEN]
        self.assertEqual(self.c.decrypt_bytes(blob, vc.KIND_FILE, bid), MARK)

    def test_keys_never_in_argv(self):
        seen = []
        real = subprocess.run

        def spy(args, *a, **kw):
            seen.append(list(args))
            return real(args, *a, **kw)
        with mock.patch.object(vc.subprocess, "run", spy):
            blob = self.c.encrypt_bytes(MARK, vc.KIND_FILE)
            self.c.decrypt_bytes(blob, vc.KIND_FILE)
        bid = blob[len(vc.MAGIC) + 1:vc.HEADER_LEN]
        pw = hmac.new(self.c.k_enc, bid + struct.pack(">Q", 0), hashlib.sha256).hexdigest()
        key_iv = hashlib.pbkdf2_hmac("sha256", pw.encode(), b"", 1, 48).hex()
        self.assertEqual(len(seen), 2)
        for args in seen:
            line = " ".join(args).lower()
            for secret in (pw, key_iv[:64], key_iv[64:], self.c.k_enc.hex(), self.c.k_mac.hex()):
                self.assertNotIn(secret, line)
            self.assertNotIn("-k", args)
            self.assertNotIn("-K", args)
            self.assertTrue(any(a.startswith("fd:") for a in args))

    def test_openssl_key_derivation_is_what_we_document(self):
        """key||iv = PBKDF2-HMAC-SHA256(password, '', 1, 48): the format is plain AES-256-CTR, so any
        AES library could read it too. (-P prints a throwaway key; this test password is random.)"""
        pw = os.urandom(16).hex()
        r, w = os.pipe()
        os.write(w, (pw + "\n").encode()); os.close(w)
        p = subprocess.run([OPENSSL, "enc", "-aes-256-ctr", "-pbkdf2", "-iter", "1", "-md", "sha256", "-nosalt",
                            "-pass", f"fd:{r}", "-P"], capture_output=True, pass_fds=(r,), text=True)
        os.close(r)
        import re
        out = dict(re.findall(r"(key|iv)\s*=\s*([0-9A-Fa-f]+)", p.stdout))
        want = hashlib.pbkdf2_hmac("sha256", pw.encode(), b"", 1, 48)
        self.assertEqual(out["key"].lower(), want[:32].hex())
        self.assertEqual(out["iv"].lower(), want[32:].hex())

    def test_other_openssl_reads_the_same_format(self):
        others = [x for x in ("/opt/homebrew/bin/openssl", "/usr/local/bin/openssl", "/usr/bin/openssl")
                  if Path(x).exists() and os.path.realpath(x) != os.path.realpath(OPENSSL)]
        if not others:
            self.skipTest("only one openssl here")
        vk = os.urandom(32)
        blob = vc.Cipher(vk, OPENSSL).encrypt_bytes(MARK * 1000, vc.KIND_FILE)
        for o in others:
            self.assertEqual(vc.Cipher(vk, o).decrypt_bytes(blob, vc.KIND_FILE), MARK * 1000, o)

    def test_missing_openssl(self):
        c = vc.Cipher(os.urandom(32), "/nonexistent/openssl")
        with self.assertRaises(vc.OpenSSLError):
            c.encrypt_bytes(b"x", vc.KIND_FILE)
        self.assertFalse(vc.openssl_ok("/nonexistent/openssl"))
        self.assertTrue(vc.openssl_ok(OPENSSL))


class Wrap(unittest.TestCase):
    def test_wrap_unwrap_and_wrong_passphrase(self):
        vk, kdf = os.urandom(32), vc.new_kdf(1024)
        w = vc.wrap(vk, vc.passphrase_material(PW, kdf))
        self.assertEqual(vc.unwrap(w, vc.passphrase_material(PW, kdf)), vk)
        self.assertEqual(vc.unwrap(w, vc.passphrase_material("  " + PW + " ", kdf)), vk)    # outer spaces don't count
        with self.assertRaises(vc.CryptoError):
            vc.unwrap(w, vc.passphrase_material(PW + "!", kdf))
        with self.assertRaises(vc.CryptoError):                     # a pass-wrap can't be used as a recovery-wrap
            vc.unwrap(w, vc.passphrase_material(PW, kdf), b"recovery")

    def test_rewrap_uses_a_fresh_salt(self):
        vk = os.urandom(32)
        k1, k2 = vc.new_kdf(1024), vc.new_kdf(1024)
        self.assertNotEqual(k1["salt"], k2["salt"])
        w1, w2 = vc.wrap(vk, vc.passphrase_material(PW, k1)), vc.wrap(vk, vc.passphrase_material(PW, k2))
        self.assertNotEqual(w1["ct"], w2["ct"])

    def test_unicode_normalization(self):
        kdf = vc.new_kdf(1024)
        self.assertEqual(vc.passphrase_material("café au lait!", kdf), vc.passphrase_material("café au lait!", kdf))

    def test_kdf_limits(self):
        for n in (1000, 512, 1 << 21):
            with self.assertRaises(ValueError):
                vc.new_kdf(n)
        with self.assertRaises(vc.CryptoError):
            vc.passphrase_material(PW, {"alg": "scrypt", "n": 1 << 30, "r": 8, "p": 1, "salt": "AAAA"})
        with self.assertRaises(vc.CryptoError):
            vc.passphrase_material(PW, {"alg": "md5"})
        self.assertEqual(vc.SCRYPT_N, 1 << 16)            # ~0.5 s and 64 MiB on a Pi 5

    def test_recovery_key_format(self):
        rk = os.urandom(32)
        s = vc.format_recovery(rk)
        self.assertEqual(len(s.replace("-", "")), 52)
        self.assertEqual(vc.parse_recovery(s), rk)
        self.assertEqual(vc.parse_recovery(s.lower().replace("-", " ")), rk)
        self.assertEqual(vc.parse_recovery(s.replace("O", "0").replace("I", "1")), rk)
        for bad in ("", "abc", s[:-1], s + "A", "!" * 52, None, 12345):
            self.assertIsNone(vc.parse_recovery(bad))


@unittest.skipUnless(OPENSSL, "needs the openssl program")
class VaultUnit(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vault-")).resolve()
        self.clock = Clock()
        self.v = vault.Vault(self.dir, {"scrypt_n": 1024, "openssl": OPENSSL, "quota_mb": 5, "max_files": 6},
                             clock=self.clock)
        self.ana = {"id": "m_ana", "device": "d_1", "name": "Ana"}
        self.ana2 = {"id": "m_ana", "device": "d_2", "name": "Ana"}
        self.ben = {"id": "m_ben", "device": "d_9", "name": "Ben"}
        self.rk = self.v.setup(self.ana, PW, "10.0.0.2")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def refused(self, status, fn, *a):
        with self.assertRaises(vault.Refused) as cm:
            fn(*a)
        self.assertEqual(cm.exception.status, status, cm.exception.message)
        return cm.exception

    def upload(self, member, data, name="doc.pdf", cat="id", note=""):
        tok = self.v.start_upload(member, {"name": name, "category": cat, "note": note, "size": len(data)})["upload"]
        return self.v.receive_upload(Body(data), member, tok)["file"]

    def test_setup_rules(self):
        v2 = vault.Vault(self.dir / "x", {"scrypt_n": 1024, "openssl": OPENSSL})
        self.refused(400, v2.setup, self.ana, "short", "ip")
        self.refused(400, v2.setup, self.ana, None, "ip")
        self.refused(400, v2.setup, self.ana, "x" * 201, "ip")
        self.refused(409, self.v.setup, self.ben, PW + "2", "ip")
        bad = vault.Vault(self.dir / "y", {"scrypt_n": 1024, "openssl": "/nonexistent/openssl"})
        self.refused(503, bad.setup, self.ana, PW, "ip")
        self.assertFalse(bad.status(self.ana, "ip")["ready"])

    def test_files_are_private(self):
        self.upload(self.ana, b"%PDF-1.4 x")
        for p in [self.v.meta_path, self.v.index_path] + list(self.v.blobs.glob("*.bin")):
            self.assertEqual(p.stat().st_mode & 0o777, 0o600, p)
        self.assertEqual(self.v.blobs.stat().st_mode & 0o777, 0o700)

    def test_no_plaintext_on_disk(self):
        data = b"\xff\xd8\xff\xe0" + MARK * 50000                 # ~1.5 MB: several chunks
        f = self.upload(self.ana, data, name=NAME_MARK + ".jpg", cat="id", note=NOTE_MARK)
        self.v.add_note(self.ana, {"name": "Doctor " + NAME_MARK, "category": "medical", "text": "Allergy: " + MARK.decode()})
        self.v.edit(self.ana, {"id": f["id"], "note": NOTE_MARK + " renewed"})
        # Short words are searched for as they would leak (JSON strings): a bare 3-byte needle turns up by
        # chance in ~1.5 MB of ciphertext about 9% of the time; a 5-byte one, about once in 700,000 runs.
        needles = [MARK, MARK[:12], NAME_MARK.encode(), NOTE_MARK.encode(), b"Allergy", b"Doctor",
                   b'"Ana"', b'"IDs"', NAME_MARK.encode("utf-16-le")]
        self.assertEqual(scan(self.dir, needles), [])
        self.assertEqual(list(self.v.blobs.glob(".*")), [])         # no upload leftovers
        names = {p.name for p in self.v.blobs.iterdir()}
        self.assertTrue(all(len(n) == 36 and n.endswith(".bin") for n in names))   # random hex, no names

    def test_roundtrip_list_search_edit_delete(self):
        f = self.upload(self.ana, PNG, name="kids.png", cat="photos", note="summer")
        self.assertEqual(f["type"], "image/png")
        self.upload(self.ana, b"%PDF-1.4 " + MARK, name="../../etc/passwd", cat="nope")
        rows = self.v.list(self.ana)["files"]
        self.assertEqual({r["name"] for r in rows}, {"kids.png", "passwd"})       # path stripped
        self.assertEqual({r["cat"] for r in rows}, {"photos", "other"})           # unknown category → other
        self.assertEqual([r["name"] for r in self.v.list(self.ana, "SUMMER")["files"]], ["kids.png"])
        self.assertEqual([r["name"] for r in self.v.list(self.ana, "", "other")["files"]], ["passwd"])
        self.assertEqual(self.v.list(self.ana, "kids nothing")["files"], [])
        self.v.edit(self.ana, {"id": f["id"], "name": "Kids 2026.png", "category": "photos"})
        self.assertIn("Kids 2026.png", [r["name"] for r in self.v.list(self.ana)["files"]])
        blob = self.v._read_index(self.v.cipher(self.ana))["files"][f["id"]]["blob"]
        self.v.delete(self.ana, f["id"])
        self.assertFalse((self.v.blobs / f"{blob}.bin").exists())
        self.refused(404, self.v.delete, self.ana, f["id"])
        self.refused(404, self.v.edit, self.ana, {"id": "../../x"})
        self.refused(404, self.v.read_note, self.ana, 12)

    def test_notes(self):
        n = self.v.add_note(self.ana, {"category": "contacts", "text": "Grandpa: 555-0101\nNeighbor: 555-0199"})["file"]
        self.assertEqual(n["name"], "Grandpa: 555-0101")
        self.assertEqual(self.v.read_note(self.ana, n["id"])["text"], "Grandpa: 555-0101\nNeighbor: 555-0199")
        self.v.edit(self.ana, {"id": n["id"], "text": "Updated list"})
        self.assertEqual(self.v.read_note(self.ana, n["id"])["text"], "Updated list")
        self.assertEqual(len(list(self.v.blobs.glob("*.bin"))), 1)             # old version removed
        self.refused(400, self.v.add_note, self.ana, {"text": "   "})
        self.refused(400, self.v.edit, self.ana, {"id": n["id"], "text": ""})
        long = self.v.add_note(self.ana, {"name": "x", "text": "a" * 50000})["file"]
        self.assertEqual(long["size"], vault.MAX_TEXT)

    def test_quota_count_and_size_caps(self):
        self.refused(413, self.v.start_upload, self.ana, {"name": "big", "size": 26 * 1024 * 1024})
        self.refused(400, self.v.start_upload, self.ana, {"name": "x", "size": "12"})
        self.refused(400, self.v.start_upload, self.ana, {"name": "x", "size": -1})
        self.refused(400, self.v.start_upload, self.ana, {"name": "x", "size": True})
        self.upload(self.ana, b"a" * (3 * 1024 * 1024))
        self.refused(413, self.v.start_upload, self.ana, {"name": "x", "size": 3 * 1024 * 1024})   # 5 MB quota
        # reservations count: two starts that each fit but not together
        self.v.start_upload(self.ana, {"name": "y", "size": 1024 * 1024})
        self.refused(413, self.v.start_upload, self.ana, {"name": "z", "size": 1024 * 1024 + 600 * 1024})
        v = vault.Vault(self.dir / "n", {"scrypt_n": 1024, "openssl": OPENSSL, "max_files": 2})
        v.setup(self.ana, PW, "ip")
        for i in range(2):
            v.add_note(self.ana, {"name": str(i), "text": "x"})
        self.refused(413, v.add_note, self.ana, {"name": "3", "text": "x"})

    def test_upload_body_must_match(self):
        tok = self.v.start_upload(self.ana, {"name": "a", "size": 10})["upload"]
        self.refused(400, self.v.receive_upload, Body(b"x" * 10, declared=11), self.ana, tok)
        self.refused(404, self.v.receive_upload, Body(b"x" * 10), self.ana, tok)       # token is single use
        tok = self.v.start_upload(self.ana, {"name": "a", "size": 10})["upload"]
        self.refused(404, self.v.receive_upload, Body(b"x" * 10), self.ana2, tok)      # other device
        tok = self.v.start_upload(self.ana, {"name": "a", "size": 10})["upload"]
        r = Body(b"x" * 4, declared=10)                                                 # client hung up
        self.refused(400, self.v.receive_upload, r, self.ana, tok)
        self.assertEqual(list(self.v.blobs.glob(".*")), [])
        self.assertEqual(self.v.list(self.ana)["count"], 0)

    def test_sessions_are_per_device_and_expire(self):
        self.v.cipher(self.ana)
        self.refused(423, self.v.list, self.ana2)            # same member, other phone: unlock separately
        self.refused(423, self.v.list, self.ben)
        self.v.unlock(self.ben, PW, "10.0.0.3")
        self.clock.t += 14 * 60
        self.v.list(self.ana)                                # use keeps it open
        self.clock.t += 14 * 60
        self.v.list(self.ana)
        self.clock.t += 16 * 60
        self.refused(423, self.v.list, self.ana)             # idle 16 min → locked
        self.refused(423, self.v.list, self.ben)
        self.assertEqual(self.v.sessions, {})
        self.assertIsNone(self.v._index)                     # decrypted index dropped with the last session
        self.v.unlock(self.ana, PW, "10.0.0.2")
        for _ in range(60):                                  # active all day: still capped at 12 h
            self.clock.t += 14 * 60
            try:
                self.v.list(self.ana)
            except vault.Refused:
                break
        self.assertLessEqual(self.clock.t - 1_800_000_000.0, 13 * 3600 + 45 * 60)
        self.refused(423, self.v.list, self.ana)

    def test_status_does_not_keep_it_open(self):
        for _ in range(4):
            self.clock.t += 5 * 60
            self.v.status(self.ana, "ip")
        self.assertFalse(self.v.status(self.ana, "ip")["unlocked"])

    def test_lock_now_wipes_key(self):
        key = self.v.sessions[self.v.sid(self.ana)]["key"]
        self.v.unlock(self.ben, PW, "ip")
        with self.v.lock:
            self.v._drop(self.v.sid(self.ana))
        self.assertEqual(bytes(key), b"\0" * 32)
        self.refused(423, self.v.list, self.ana)
        self.v.list(self.ben)
        self.assertEqual(self.v.lock_all(), 1)
        self.refused(423, self.v.list, self.ben)

    def test_restart_means_locked(self):
        self.upload(self.ana, b"%PDF-1.4 abc")
        v2 = vault.Vault(self.dir, {"scrypt_n": 1024, "openssl": OPENSSL})
        self.assertTrue(v2.status(self.ana, "ip")["setup"])
        self.refused(423, v2.list, self.ana)
        v2.unlock(self.ana, PW, "ip")
        self.assertEqual(v2.list(self.ana)["count"], 1)

    def test_wrong_passphrase_lockout_per_ip_and_device(self):
        self.v.lock_all()
        for _ in range(4):
            self.refused(403, self.v.unlock, self.ben, "wrong passphrase!", "10.0.0.7")
        self.v.unlock(self.ben, PW, "10.0.0.7")              # success clears the count
        for _ in range(5):
            self.refused(403, self.v.unlock, self.ben, "wrong passphrase!", "10.0.0.7")
        e = self.refused(429, self.v.unlock, self.ben, PW, "10.0.0.7")    # even the right one now
        self.assertGreater(e.extra["wait"], 500)
        self.refused(429, self.v.unlock, self.ben, PW, "10.0.0.8")     # same device, new address: still locked
        self.v.unlock(self.ana, PW, "10.0.0.9")                         # others are not affected
        self.clock.t += 601
        self.v.unlock(self.ben, PW, "10.0.0.7")

    def test_global_lockout(self):
        for i in range(vault.G_FAILS):
            m = {"id": f"m_{i}", "device": "d", "name": "x"}
            self.refused(403, self.v.unlock, m, "wrong passphrase!", f"10.1.0.{i}")
        self.refused(429, self.v.unlock, self.ben, PW, "10.9.9.9")
        self.clock.t += vault.G_LOCK + 1
        self.v.unlock(self.ben, PW, "10.9.9.9")

    def test_recovery_key_and_passphrase_change(self):
        f = self.upload(self.ana, b"%PDF-1.4 " + MARK)
        self.v.unlock(self.ben, PW, "ip")
        self.refused(403, self.v.change_passphrase, self.ana, "not it at all", "a brand new phrase", "ip")
        self.refused(400, self.v.change_passphrase, self.ana, PW, "short", "ip")
        self.v.change_passphrase(self.ana, PW, "a brand new phrase", "ip")
        self.refused(423, self.v.list, self.ben)                         # others locked out
        self.v.list(self.ana)
        self.refused(403, self.v.unlock, self.ben, PW, "ip2")
        self.v.unlock(self.ben, "a brand new phrase", "ip2")
        # forgot it: recovery key sets a new one; the files are still there
        self.v.lock_all()
        self.refused(403, self.v.recover, self.ana, "AAAA-" * 13, "third passphrase here", "ip3")
        self.refused(403, self.v.recover, self.ana, "garbage", "third passphrase here", "ip3")
        self.v.recover(self.ana, self.rk.lower(), "third passphrase here", "ip3")
        self.v.unlock(self.ben, "third passphrase here", "ip4")
        self.assertEqual([r["id"] for r in self.v.list(self.ben)["files"]], [f["id"]])
        # a new recovery key replaces the old one
        rk2 = self.v.new_recovery(self.ana, "third passphrase here", "ip3")
        self.assertNotEqual(rk2, self.rk)
        self.refused(403, self.v.recover, self.ana, self.rk, "fourth passphrase here", "ip5")
        self.v.recover(self.ana, rk2, "fourth passphrase here", "ip6")
        acts = [a["what"] for a in self.v.status(self.ana, "ip")["activity"]]
        self.assertIn("reset the passphrase with the recovery key", acts)
        self.assertIn("changed the passphrase", acts)

    def test_tampered_storage(self):
        f = self.upload(self.ana, b"%PDF-1.4 " + MARK * 10)
        blob = next(self.v.blobs.glob("*.bin"))
        raw = bytearray(blob.read_bytes())
        raw[-50] ^= 1
        blob.write_bytes(bytes(raw))

        class Req:
            def __init__(self):
                self.out = []
                self.wfile = self
                self.close_connection = False

            def write(self, b):
                self.out.append(b)

            def send_response(self, code):
                self.out.append(("status", code))

            def send_header(self, k, v):
                pass

            def end_headers(self):
                pass
        r = Req()
        self.refused(500, self.v.send_file, r, self.ana, f["id"], False)
        self.assertEqual(r.out, [])                          # not one byte sent
        idx = bytearray(self.v.index_path.read_bytes())
        idx[40] ^= 1
        self.v.index_path.write_bytes(bytes(idx))
        self.v._index = None
        self.refused(500, self.v.list, self.ana)
        meta = json.loads(self.v.meta_path.read_text())
        meta["wrap"]["ct"] = vc.b64e(os.urandom(32))
        self.v.meta_path.write_text(json.dumps(meta))
        self.refused(403, self.v.unlock, self.ben, PW, "ip")

    def test_orphans_cleaned_on_first_unlock(self):
        self.upload(self.ana, b"%PDF-1.4 keep")
        orphan = self.v.blobs / ("ab" * 16 + ".bin")
        orphan.write_bytes(b"x")
        tmp = self.v.blobs / ".up-1234.tmp"
        tmp.write_bytes(b"x")
        fresh = self.v.blobs / ("cd" * 16 + ".bin")
        fresh.write_bytes(b"x")
        old = self.clock.t - 7200
        os.utime(orphan, (old, old)); os.utime(tmp, (old, old))
        os.utime(fresh, (self.clock.t, self.clock.t))
        v2 = vault.Vault(self.dir, {"scrypt_n": 1024, "openssl": OPENSSL}, clock=self.clock)
        v2.unlock(self.ana, PW, "ip")
        self.assertFalse(orphan.exists()); self.assertFalse(tmp.exists())
        self.assertTrue(fresh.exists())                      # might be an upload in flight
        self.assertEqual(v2.list(self.ana)["count"], 1)

    def test_concurrent_uploads(self):
        v = vault.Vault(self.dir / "c", {"scrypt_n": 1024, "openssl": OPENSSL})
        v.setup(self.ana, PW, "ip")
        members = [{"id": "m_ana", "device": f"d_{i}", "name": "Ana"} for i in range(6)]
        for m in members:
            v.unlock(m, PW, "ip")
        errors = []

        def go(m, i):
            try:
                tok = v.start_upload(m, {"name": f"f{i}", "size": 5000})["upload"]
                v.receive_upload(Body(bytes([i]) * 5000), m, tok)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=go, args=(m, i)) for i, m in enumerate(members)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        v._index = None                                      # read back from disk
        self.assertEqual(sorted(r["name"] for r in v.list(self.ana)["files"]), [f"f{i}" for i in range(6)])
        self.assertEqual(len(list(v.blobs.glob("*.bin"))), 6)


def http_req(port, method, path, body=None, member=None, admin=None, headers=None, raw=None, timeout=30):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    h = {"Content-Type": "application/json"}
    if member:
        h["X-Member-Token"] = member
    if admin:
        h["X-Admin-Token"] = admin
    h.update(headers or {})
    c.request(method, path, raw if raw is not None else (json.dumps(body) if body is not None else None), h)
    r = c.getresponse()
    data = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    try:
        return r.status, json.loads(data), hdrs
    except ValueError:
        return r.status, data, hdrs


@unittest.skipUnless(OPENSSL, "needs the openssl program")
class VaultHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vault-http-")).resolve()
        (cls.tmp / "data").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        for f in ("vault.py", "_vault_crypto.py"):
            shutil.copy(FEATS / f, feats / f)
        cfg = cls.tmp / "prepper.toml"
        cfg.write_text(f"""
[upd]
data_root = "{cls.tmp / 'data'}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{cls.tmp}/upd-state.json"
report_dir = "{cls.tmp}/reports"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
admin_pin = "{server.hash_pin('246810')}"
board_file = "{cls.tmp}/board.jsonl"
family_board_file = "{cls.tmp}/family-board.jsonl"
members_file = "{cls.tmp}/members.json"
settings_file = "{cls.tmp}/portal-settings.json"
mesh_outbox = "{cls.tmp}/mesh-outbox.jsonl"
features_data = "{cls.tmp}/features-data"
ctl = "/nonexistent/prepperctl"
services = {{}}

[vault]
scrypt_n = 1024
openssl = "{OPENSSL}"
quota_mb = 40
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        cls.mod = [f for f in server.FEATURES if f["id"] == "vault"][0]["mod"]
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]
        db = server.members_db()
        cls.ana = db.join(db.create_invite("Ana")["code"], "Ana", "phone", "t")[2]
        st, m, _ = db.join(db.create_invite("Ben")["code"], "Ben", "phone", "t")
        cls.ben = _
        cls.ana_tablet = db.join(db.create_invite(member_id=db.auth(cls.ana)["id"])["code"], "", "tablet", "t")[2]
        cls.data = cls.tmp / "features-data" / "vault"
        st, j, _ = cls.req("POST", "/api/vault/setup", {"passphrase": PW}, member=cls.ana)
        assert st == 200, j
        cls.rk = j["recovery_key"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def req(cls, *a, **kw):
        return http_req(cls.port, *a, **kw)

    def setUp(self):
        v = self.mod._V
        v.fails.clear(); v.gfail[:] = [0, 0.0, 0.0]
        if self.req("GET", "/api/vault/status", member=self.ana)[1].get("unlocked") is not True:
            self.assertEqual(self.req("POST", "/api/vault/unlock", {"passphrase": PW}, member=self.ana)[0], 200)

    def upload(self, data, name="doc.pdf", cat="id", note="", member=None, ctype="application/octet-stream"):
        member = member or self.ana
        st, j, _ = self.req("POST", "/api/vault/upload-start", {"name": name, "category": cat, "note": note, "size": len(data)}, member=member)
        if st != 200:
            return st, j
        st, j, _ = self.req("POST", "/api/vault/upload/" + j["upload"], member=member, raw=data,
                            headers={"Content-Type": ctype, "X-Badger-Upload": "1"})
        return st, j

    def test_feature_listed_for_family(self):
        f = [x for x in self.req("GET", "/api/features")[1]["features"] if x["id"] == "vault"][0]
        self.assertEqual((f["group"], f["access"], f["route"]), ("family", "member", "/vault"))

    def test_access_visitor_admin_member(self):
        paths = [("GET", "/api/vault/status", None), ("POST", "/api/vault/unlock", {"passphrase": PW}),
                 ("POST", "/api/vault/list", {}), ("POST", "/api/vault/setup", {"passphrase": PW}),
                 ("POST", "/api/vault/export", {}), ("GET", "/api/vault/file/abcdefghijkl", None),
                 ("POST", "/api/vault/lock", {"all": True}), ("POST", "/api/vault/recover", {"recovery": self.rk, "passphrase": "x" * 12})]
        for method, path, body in paths:
            self.assertEqual(self.req(method, path, body)[0], 401, path)                       # visitor on the Wi-Fi
            self.assertEqual(self.req(method, path, body, admin=self.admin)[0], 401, path)      # admin PIN, not a member
            self.assertEqual(self.req(method, path, body, member="forged-token")[0], 401, path)
        self.assertEqual(self.req("POST", "/api/vault/upload/abcdefghijklmnopqrstuv", raw=b"x",
                                  headers={"X-Badger-Upload": "1"}, admin=self.admin)[0], 401)
        self.assertEqual(self.req("GET", "/api/vault/status", member=self.ana)[0], 200)

    def test_upload_download_roundtrip(self):
        data = b"%PDF-1.4 " + MARK * 40000                    # ~1.2 MB, two chunks
        st, j = self.upload(data, name="Ana passport ✈.pdf", note=NOTE_MARK + " zebra-41")
        self.assertEqual(st, 200, j)
        fid = j["file"]["id"]
        st, body, h = self.req("GET", f"/api/vault/file/{fid}", member=self.ana)
        self.assertEqual((st, body), (200, data))
        self.assertEqual(h["content-type"], "application/pdf")
        self.assertTrue(h["content-disposition"].startswith('attachment; filename="Ana passport _.pdf"'))
        self.assertIn("filename*=UTF-8''Ana%20passport%20%E2%9C%88.pdf", h["content-disposition"])
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", h["content-security-policy"])
        st, body, h = self.req("GET", f"/api/vault/file/{fid}?inline=1", member=self.ana)
        self.assertTrue(h["content-disposition"].startswith("attachment"))      # PDFs never inline
        self.assertEqual(scan(self.data, [MARK, NOTE_MARK.encode(), b"passport"]), [])
        found = self.req("POST", "/api/vault/list", {"q": "99887766 ZEBRA-41"}, member=self.ana)[1]["files"]
        self.assertEqual([f["id"] for f in found], [fid])

    def test_images_inline_only_jpeg_png_and_html_never_served_as_html(self):
        st, j = self.upload(PNG, name="kid.png", cat="photos", ctype="image/png")
        st, body, h = self.req("GET", f"/api/vault/file/{j['file']['id']}?inline=1", member=self.ana)
        self.assertEqual((st, h["content-type"]), (200, "image/png"))
        self.assertTrue(h["content-disposition"].startswith("inline"))
        st, j = self.upload(b"<html><script>alert(1)</script></html>", name="x.html", ctype="text/html")
        st, body, h = self.req("GET", f"/api/vault/file/{j['file']['id']}?inline=1", member=self.ana)
        self.assertEqual(h["content-type"], "application/octet-stream")
        self.assertTrue(h["content-disposition"].startswith("attachment"))
        st, j = self.upload(b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>", name="x.svg", ctype="image/svg+xml")
        self.assertNotIn("svg", self.req("GET", f"/api/vault/file/{j['file']['id']}", member=self.ana)[2]["content-type"])

    def test_other_member_and_device_must_unlock(self):
        st, j = self.upload(b"%PDF-1.4 shared")
        self.assertEqual(st, 200)
        for who in (self.ben, self.ana_tablet):
            st, j2, _ = self.req("POST", "/api/vault/list", {}, member=who)
            self.assertEqual((st, j2.get("locked")), (423, True))
            self.assertEqual(self.req("GET", f"/api/vault/file/{j['file']['id']}", member=who)[0], 423)
            self.assertEqual(self.req("POST", "/api/vault/unlock", {"passphrase": PW}, member=who)[0], 200)
            self.assertEqual(self.req("GET", f"/api/vault/file/{j['file']['id']}", member=who)[1], b"%PDF-1.4 shared")
            self.assertEqual(self.req("POST", "/api/vault/lock", {}, member=who)[0], 200)

    def test_lock_now_and_auto_lock(self):
        self.assertEqual(self.req("POST", "/api/vault/lock", {}, member=self.ana)[1], {"ok": True, "locked": 1})
        self.assertEqual(self.req("POST", "/api/vault/list", {}, member=self.ana)[0], 423)
        self.assertEqual(self.req("POST", "/api/vault/unlock", {"passphrase": PW}, member=self.ana)[0], 200)
        v, real = self.mod._V, self.mod._V.clock
        try:
            now = real()
            v.clock = lambda: now + 16 * 60
            st, j, _ = self.req("GET", "/api/vault/status", member=self.ana)
            self.assertFalse(j["unlocked"])
            self.assertEqual(self.req("POST", "/api/vault/list", {}, member=self.ana)[0], 423)
        finally:
            v.clock = real

    def test_wrong_passphrase_limited(self):
        self.req("POST", "/api/vault/lock", {}, member=self.ben)
        for _ in range(5):
            st, j, _ = self.req("POST", "/api/vault/unlock", {"passphrase": "nope nope nope"}, member=self.ben,
                                headers={"X-Real-IP": "192.168.4.50"})
            self.assertEqual(st, 403)
        st, j, h = self.req("POST", "/api/vault/unlock", {"passphrase": PW}, member=self.ben, headers={"X-Real-IP": "192.168.4.50"})
        self.assertEqual(st, 429)
        self.assertIn("Wait", j["error"])
        self.assertTrue(int(h["retry-after"]) > 0)
        self.assertGreater(self.req("GET", "/api/vault/status", member=self.ben, headers={"X-Real-IP": "192.168.4.50"})[1]["wait"], 0)

    def test_upload_errors(self):
        up = {"Content-Type": "application/octet-stream", "X-Badger-Upload": "1"}
        st, j, _ = self.req("POST", "/api/vault/upload-start", {"name": "a", "size": 5}, member=self.ana)
        tok = j["upload"]
        self.assertEqual(self.req("POST", "/api/vault/upload/" + tok, member=self.ana, raw=b"123456", headers=up)[0], 400)
        self.assertEqual(self.req("POST", "/api/vault/upload/" + tok, member=self.ana, raw=b"12345", headers=up)[0], 404)
        st, j, _ = self.req("POST", "/api/vault/upload-start", {"name": "a", "size": 5}, member=self.ana)
        self.assertEqual(self.req("POST", "/api/vault/upload/" + j["upload"], member=self.ben, raw=b"12345", headers=up)[0], 404)
        self.assertEqual(self.req("POST", "/api/vault/upload/" + j["upload"], member=self.ana, raw=b"12345")[0], 415)   # no upload header
        self.assertEqual(self.req("POST", "/api/vault/upload/../../x", member=self.ana, raw=b"12345", headers=up)[0], 404)
        self.assertEqual(self.req("POST", "/api/vault/upload-start", {"name": "a", "size": 25 * 1024 * 1024 + 1}, member=self.ana)[0], 413)
        st, j, _ = self.req("POST", "/api/vault/upload-start", {"name": "a", "size": 5}, member=self.ana)
        self.req("POST", "/api/vault/lock", {}, member=self.ana)            # locking drops pending uploads
        self.assertEqual(self.req("POST", "/api/vault/upload/" + j["upload"], member=self.ana, raw=b"12345", headers=up)[0], 404)
        self.assertEqual(self.req("POST", "/api/vault/upload-start", {"name": "a", "size": 5}, member=self.ana)[0], 423)
        self.req("POST", "/api/vault/unlock", {"passphrase": PW}, member=self.ana)
        big = {"Content-Type": "application/octet-stream", "X-Badger-Upload": "1", "Content-Length": str(26 * 1024 * 1024)}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/api/vault/upload/abcdefghijklmnopqrstuv")
        for k, val in dict(big, **{"X-Member-Token": self.ana}).items():
            c.putheader(k, val)
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 413)                     # refused before a byte is read
        c.close()

    def test_traversal_and_junk_ids(self):
        for p in ("/api/vault/file/../../../etc/passwd", "/api/vault/file/", "/api/vault/file/%2e%2e%2fvault.json",
                  "/api/vault/note/zz", "/api/vault/file/" + "a" * 200):
            self.assertEqual(self.req("GET", p, member=self.ana)[0], 404, p)
        for body in ({"id": None}, {"id": ["x"]}, {"id": "../vault.json"}, {}):
            self.assertEqual(self.req("POST", "/api/vault/delete", body, member=self.ana)[0], 404)
        self.assertEqual(self.req("POST", "/api/vault/list", {"q": ["x"], "category": {"a": 1}}, member=self.ana)[0], 200)

    def test_export_bundle_and_offline_tool(self):
        st, j = self.upload(b"\xff\xd8\xff\xe0" + MARK * 3, name=NAME_MARK + ".jpg", cat="photos")
        self.req("POST", "/api/vault/note", {"name": "Meds", "category": "medical", "text": "Insulin " + NOTE_MARK}, member=self.ana)
        self.assertEqual(self.req("GET", "/api/vault/export/" + "a" * 32)[0], 404)
        self.assertEqual(self.req("POST", "/api/vault/export", {}, member=self.ben)[0], 423)
        st, j, _ = self.req("POST", "/api/vault/export", {}, member=self.ana)
        url = j["url"]
        st, tar, h = self.req("GET", url)                  # a plain link: no member header needed
        self.assertEqual(st, 200)
        self.assertEqual(h["content-type"], "application/x-tar")
        self.assertIn("attachment", h["content-disposition"])
        self.assertEqual(self.req("GET", url)[0], 404)       # single use
        for needle in (MARK, NAME_MARK.encode(), NOTE_MARK.encode(), b"Insulin"):
            self.assertNotIn(needle, tar)
        bundle = self.tmp / "bundle.tar"
        bundle.write_bytes(tar)
        with tarfile.open(bundle) as tf:
            names = tf.getnames()
        self.assertIn("vault.json", names); self.assertIn("index.bin", names); self.assertIn("README.txt", names)
        out = io.StringIO()
        with mock.patch("getpass.getpass", return_value="wrong passphrase"), contextlib.redirect_stdout(out):
            self.assertEqual(vc._main(["list", str(bundle)]), 1)
        out = io.StringIO()
        with mock.patch("getpass.getpass", return_value=PW), contextlib.redirect_stdout(out):
            self.assertEqual(vc._main(["list", str(bundle)]), 0)
        self.assertIn(NAME_MARK, out.getvalue())
        dest = self.tmp / "extracted"
        with mock.patch("getpass.getpass", return_value=PW), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vc._main(["extract", str(bundle), str(dest)]), 0)
        self.assertEqual((dest / "photos" / (NAME_MARK + ".jpg")).read_bytes(), b"\xff\xd8\xff\xe0" + MARK * 3)
        self.assertIn(NOTE_MARK, (dest / "medical" / "Meds.txt").read_text())
        restored = self.tmp / "restored"
        with mock.patch("getpass.getpass", return_value=PW), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vc._main(["restore", str(bundle), str(restored)]), 0)
        v2 = vault.Vault(restored, {"openssl": OPENSSL})
        v2.unlock({"id": "m", "device": "d", "name": "x"}, PW, "ip")
        self.assertIn(NAME_MARK + ".jpg", [f["name"] for f in v2.list({"id": "m", "device": "d"})["files"]])

    def test_export_link_dies_with_the_session(self):
        st, j, _ = self.req("POST", "/api/vault/export", {}, member=self.ana)
        self.req("POST", "/api/vault/lock", {}, member=self.ana)
        self.assertEqual(self.req("GET", j["url"])[0], 404)

    def test_lock_all(self):
        self.req("POST", "/api/vault/unlock", {"passphrase": PW}, member=self.ben)
        self.req("POST", "/api/vault/lock", {}, member=self.ana)
        self.assertEqual(self.req("POST", "/api/vault/lock", {"all": True}, member=self.ana)[0], 423)   # locked devices can't
        self.req("POST", "/api/vault/unlock", {"passphrase": PW}, member=self.ana)
        self.assertEqual(self.req("POST", "/api/vault/lock", {"all": True}, member=self.ana)[1]["locked"], 2)
        self.assertEqual(self.req("POST", "/api/vault/list", {}, member=self.ben)[0], 423)

    def test_removed_member_loses_access(self):
        db = server.members_db()
        st, m, key = db.join(db.create_invite("Cal")["code"], "Cal", "phone", "t")
        self.assertEqual(self.req("POST", "/api/vault/unlock", {"passphrase": PW}, member=key)[0], 200)
        self.assertEqual(self.req("POST", "/api/vault/list", {}, member=key)[0], 200)
        db.remove(m["id"])
        self.assertEqual(self.req("POST", "/api/vault/list", {}, member=key)[0], 401)

    def test_bad_json_fields(self):
        for body in ({"passphrase": 123}, {"passphrase": ["x"]}, {"passphrase": "x" * 5000}, {}):
            self.assertEqual(self.req("POST", "/api/vault/unlock", body, member=self.ana)[0], 400, body)
        self.assertEqual(self.req("POST", "/api/vault/upload-start", {"name": {"x": 1}, "size": 3}, member=self.ana)[0], 200)
        self.assertEqual(self.req("POST", "/api/vault/note", {"text": 5}, member=self.ana)[0], 200)
        self.assertEqual(self.req("POST", "/api/vault/note", {"text": None}, member=self.ana)[0], 400)

    def test_plaintext_never_on_disk_after_everything(self):
        self.upload(b"%PDF-1.4 " + MARK, name=NAME_MARK, note=NOTE_MARK)
        self.assertEqual(scan(self.tmp / "features-data", [MARK, NAME_MARK.encode(), NOTE_MARK.encode()]), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
