#!/usr/bin/env python3
"""
Adversarial tests for upd: hostile bundles and peers, replays, interrupted and
lying downloads, a full disk, concurrent runs, gc and symlinks, budget and
version edge cases. No internet; signatures need ssh-keygen.
    python3 tests/test_upd_adversarial.py -v
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "upd"))
import upd  # noqa: E402

HAVE_SSH = shutil.which("ssh-keygen") is not None


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FileServer(http.server.BaseHTTPRequestHandler):
    """Serves FileServer.files {path: bytes} with ETag/Range/If-Range, plus misbehaviours."""
    protocol_version = "HTTP/1.0"
    files: dict[str, bytes] = {}
    etags: dict[str, str] = {}
    mode = "honest"          # honest | ignore-if-range | wrong-range | no-validators | lie-length
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        body = FileServer.files.get(urllib.parse.urlparse(self.path).path)
        self.send_response(200 if body is not None else 404)
        if body is not None:
            self.send_header("ETag", f'"{sha(body)[:12]}"')
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        FileServer.seen.append({"path": path, "range": self.headers.get("Range"), "if-range": self.headers.get("If-Range")})
        body = FileServer.files.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        etag = FileServer.etags.get(path, f'"{sha(body)[:12]}"')
        rng, ifr = self.headers.get("Range"), self.headers.get("If-Range")
        start = None
        if rng and rng.startswith("bytes=") and rng.endswith("-"):
            start = int(rng[6:-1])
            if FileServer.mode != "ignore-if-range" and ifr and ifr != etag:
                start = None                                   # changed: whole file
        if FileServer.mode == "wrong-range" and start is not None:
            part, cr = body, f"bytes 0-{len(body) - 1}/{len(body)}"
        elif start is not None:
            part, cr = body[start:], f"bytes {start}-{len(body) - 1}/{len(body)}"
        else:
            part, cr = body, None
        self.send_response(206 if cr else 200)
        if FileServer.mode != "no-validators":
            self.send_header("ETag", etag)
        if cr:
            self.send_header("Content-Range", cr)
        self.send_header("Content-Length", str(len(part)))
        self.end_headers()
        self.wfile.write(part)


class Srv(http.server.ThreadingHTTPServer):
    daemon_threads = True


CATALOG = """
[[item]]
id = "z1"
title = "Zim one"
category = "reference"
kind = "zim"
size_gb = 0.0001
tiers = ["base"]
source = { type = "manual" }

[[item]]
id = "z2"
title = "Zim two"
category = "reference"
kind = "zim"
size_gb = 0.0001
tiers = ["base"]
source = { type = "manual" }

[[item]]
id = "kiwix-js"
title = "Kiwix JS"
category = "tools"
kind = "app"
size_gb = 0.0001
tiers = ["base"]
source = { type = "manual" }

[[item]]
id = "prepper_software"
title = "Software"
category = "tools"
kind = "app"
size_gb = 0.0001
tiers = ["base"]
source = { type = "manual" }
"""


class AdversarialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upd-adv-")).resolve()
        FileServer.files, FileServer.etags, FileServer.mode, FileServer.seen = {}, {}, "honest", []
        self.http = Srv(("127.0.0.1", 0), FileServer)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──
    def disk(self, name="d", extra="", catalog=CATALOG) -> upd.Config:
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        (d / "catalog.toml").write_text(catalog)
        (d / "prepper.toml").write_text(f"""
[upd]
data_root = "data"
state_file = "state/upd-state.json"
report_dir = "state/reports"
catalog = "catalog.toml"
tier = "base"
reserve_gb = 0
allowed_signers = "allowed_signers"
signing_key = "key"
disk_id = "{name}"
{"" if "require_signed_peers" in extra else "require_signed_peers = false"}
{extra}
""")
        return upd.Config.load(d / "prepper.toml")

    def bundle(self, name: str, entries: list[dict], disk_id="peer", generated=None) -> Path:
        """entries: {id, filename, data: bytes | {relpath: bytes}, version, ...overrides}."""
        b = self.tmp / "usb" / name
        (b / "files").mkdir(parents=True, exist_ok=True)
        items = []
        for e in entries:
            e = dict(e)
            data = e.pop("data")
            path = b / "files" / e["filename"]
            if isinstance(data, dict):
                for rel, content in data.items():
                    (path / rel).parent.mkdir(parents=True, exist_ok=True)
                    (path / rel).write_bytes(content)
                e.setdefault("tree_sha256", upd.tree_sha256(path))
            else:
                path.write_bytes(data)
                e.setdefault("sha256", sha(data))
                e.setdefault("size", len(data))
            e.setdefault("path", f"files/{e['filename']}")
            e.setdefault("comparable", True)
            items.append({k: v for k, v in e.items() if v is not ...})
        (b / "manifest.json").write_text(json.dumps({"disk_id": disk_id, "generated": generated or upd.now_iso(),
                                                     "format": 1, "items": items}))
        return b

    def install_from(self, cfg, bundle: Path) -> int:
        offers = upd.load_offer(str(bundle), cfg)
        return upd.apply_plan(cfg, upd.build_plan(cfg, offline=True, offers=offers), dry_run=False)

    def make_key(self, disk: str, trust: bool = True) -> str:
        key = self.tmp / disk / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        pub = (self.tmp / disk / "key.pub").read_text().split()
        line = f'prepper-update namespaces="prepper-update" {pub[0]} {pub[1]}\n'
        if trust:
            (self.tmp / disk / "allowed_signers").write_text(line)
        return line

    def tree_snapshot(self, root: Path) -> dict:
        return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() and not p.is_symlink() else
                                           os.readlink(p) if p.is_symlink() else None)
                for p in sorted(root.rglob("*"))}

    # ── signatures ──
    @unittest.skipUnless(HAVE_SSH, "ssh-keygen needed")
    def test_bad_and_mismatched_signatures(self):
        cfg = self.disk("a", extra="require_signed_peers = true")
        self.make_key("a")
        b = self.bundle("signed", [{"id": "z1", "filename": "z1_2026-01.zim", "data": b"one", "version": "2026-01"}])
        man = b / "manifest.json"
        self.assertTrue(upd.sign_file(man, cfg))
        self.assertIn("z1", upd.load_offer(str(b), cfg), "control: genuine signature accepted")
        good_sig = (b / "manifest.json.sig").read_bytes()
        # Signature over a different manifest: the bytes changed after signing.
        man.write_text(man.read_text().replace("2026-01", "2099-01"))
        self.assertEqual(upd.load_offer(str(b), cfg), {})
        # Signed by a key nobody trusts.
        other = self.disk("other")
        self.make_key("other", trust=False)
        self.assertTrue(upd.sign_file(man, other))
        self.assertEqual(upd.load_offer(str(b), cfg), {})
        # Garbage, empty, huge, or missing signature.
        for sig in (b"garbage", b"", b"-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n",
                    b"x" * (100 << 10)):
            (b / "manifest.json.sig").write_bytes(sig)
            self.assertEqual(upd.load_offer(str(b), cfg), {}, sig[:20])
        (b / "manifest.json.sig").unlink()
        self.assertEqual(upd.load_offer(str(b), cfg), {})
        # verify_bytes checks exactly the bytes it is given.
        man.write_text(man.read_text().replace("2099-01", "2026-01"))
        data = man.read_bytes()
        self.assertTrue(upd.verify_bytes(data, good_sig, cfg))
        self.assertFalse(upd.verify_bytes(data + b" ", good_sig, cfg))
        self.assertNotEqual(data.replace(b"2026-01", b"2026-02"), data)
        self.assertFalse(upd.verify_bytes(data.replace(b"2026-01", b"2026-02"), good_sig, cfg))

    @unittest.skipUnless(HAVE_SSH, "ssh-keygen needed")
    def test_manifest_is_never_reread_after_verification(self):
        """The old code verified a temp file, then read it again to parse it."""
        cfg = self.disk("a", extra="require_signed_peers = true")
        self.make_key("a")
        b = self.bundle("toctou", [{"id": "z1", "filename": "z1_2026-01.zim", "data": b"one", "version": "2026-01"}])
        self.assertTrue(upd.sign_file(b / "manifest.json", cfg))
        forged = (b / "manifest.json").read_text().replace("2026-01", "2099-12")
        real_verify = upd.verify_bytes

        def verify_then_swap(data, sig, c):
            ok = real_verify(data, sig, c)
            (b / "manifest.json").write_text(forged)          # swapped right after the check
            return ok
        with mock.patch.object(upd, "verify_bytes", verify_then_swap):
            offers = upd.load_offer(str(b), cfg, self.tmp / "unused")
        self.assertEqual(offers["z1"].version, "2026-01", "the verified bytes are the parsed bytes")
        self.assertFalse((self.tmp / "unused").exists(), "no temp copy of the manifest is written")

    @unittest.skipUnless(HAVE_SSH, "ssh-keygen needed")
    def test_replays_and_future_dated_manifests(self):
        cfg = self.disk("a", extra="require_signed_peers = true")
        self.make_key("a")

        def publish(generated, ver):
            b = self.bundle("r", [{"id": "z1", "filename": f"z1_{ver}.zim", "data": ver.encode(), "version": ver}],
                            disk_id="peer", generated=generated)
            self.assertTrue(upd.sign_file(b / "manifest.json", cfg))
            return upd.load_offer(str(b), cfg)
        self.assertIn("z1", publish("2026-06-01T00:00:00+00:00", "2026-06"))
        self.assertEqual(publish("2026-05-01T00:00:00+00:00", "2026-05"), {}, "older signed manifest replayed")
        self.assertEqual(publish("2026-05-31T23:59:59Z", "2026-05"), {}, "one second older is still older")
        self.assertIn("z1", publish("2026-06-01T00:00:00+00:00", "2026-06"), "the same manifest again is fine")
        # A peer whose clock says 2099 must not lock out every honest manifest after it.
        self.assertIn("z1", publish("2099-01-01T00:00:00+00:00", "2099-01"))
        self.assertIn("z1", publish(upd.now_iso(), "2026-11"), "honest manifest after a future-dated one")
        marks = json.loads(upd._peer_marks_path(cfg).read_text())
        self.assertLess(marks["peer"], "2099", "the stored mark is capped at the present")
        self.assertEqual(publish("garbage-date", "2026-01"), {}, "an unparseable date counts as ancient")

    # ── malformed manifests ──
    def test_hostile_manifest_entries(self):
        cfg = self.disk()
        b = self.tmp / "usb" / "evil"
        (b / "files" / "tree").mkdir(parents=True)
        (b / "files" / "ok.zim").write_bytes(b"ok")
        (b / "files" / "tree" / "a.txt").write_bytes(b"a")
        good = {"id": "z1", "version": "2026-01", "filename": "ok.zim", "path": "files/ok.zim", "sha256": sha(b"ok")}
        bad = [
            {**good, "id": "../../etc"}, {**good, "id": "a/b"}, {**good, "id": ".hidden"}, {**good, "id": ""},
            {**good, "id": "x" * 300}, {**good, "id": 5}, {**good, "sha256": None}, {**good, "sha256": "ABC"},
            {**good, "sha256": sha(b"ok").upper()}, {**good, "size": -1}, {**good, "size": True},
            {**good, "size": "12"}, {**good, "comparable": "false"}, {**good, "version": ""},
            {**good, "version": 2026}, {**good, "filename": "a/b.zim"}, {**good, "path": "files/../../x"},
            {**good, "id": "z2", "path": "files/tree", "filename": "tree"},                    # tree without tree hash
            {**good, "id": "z2", "path": "files/tree", "filename": "tree", "tree_sha256": "nope"},
            "not-an-object", None, ["list"],
        ]
        dup = {**good, "version": "2099-01"}                                                   # duplicate id, later
        (b / "manifest.json").write_text(json.dumps({"disk_id": "evil", "generated": upd.now_iso(),
                                                     "items": [good] + bad + [dup]}))
        offers = upd.load_offer(str(b), cfg)
        self.assertEqual(list(offers), ["z1"])
        self.assertEqual(offers["z1"].version, "2026-01", "duplicate id: the first entry wins")
        for doc in ('{"items": 5}', '{"items": {"a": 1}}', "[]", '"x"', "{", "[" * 100_000):
            (b / "manifest.json").write_text(doc)
            self.assertEqual(upd.load_offer(str(b), cfg), {}, doc[:20])

    def test_huge_manifest_refused_without_reading_it(self):
        cfg = self.disk()
        b = self.tmp / "usb" / "huge"
        b.mkdir(parents=True)
        with open(b / "manifest.json", "wb") as fh:
            fh.truncate(upd.MANIFEST_LIMIT + 1)                     # sparse: costs no disk
        with mock.patch.object(upd.json, "loads", side_effect=AssertionError("parsed a huge manifest")):
            self.assertEqual(upd.load_offer(str(b), cfg), {})
        FileServer.files["/peer/manifest.json"] = b"{" + b" " * (upd.MANIFEST_LIMIT + 10) + b"}"
        self.assertEqual(upd.load_offer(f"{self.base}/peer", cfg), {})

    # ── content integrity ──
    def test_truncated_tampered_and_size_lying_files_are_not_installed(self):
        cfg = self.disk()
        v1 = self.bundle("v1", [{"id": "z1", "filename": "z1_2026-01.zim", "data": b"version one", "version": "2026-01"}])
        self.assertEqual(self.install_from(cfg, v1), 0)
        cur = cfg.data_root / "zim" / "z1.current"
        self.assertEqual(cur.read_bytes(), b"version one")
        before = self.tree_snapshot(cfg.data_root)
        state_before = upd.State(cfg.state_file).get("z1")
        payload = b"version two, the real one"
        cases = {
            "truncated": dict(data=payload[:5], sha256=sha(payload), size=len(payload)),
            "tampered": dict(data=b"version two, the EVIL one", sha256=sha(payload), size=len(payload)),
            "size-smaller": dict(data=payload, size=3),
            "size-larger": dict(data=payload, size=10 ** 12),
        }
        for name, over in cases.items():
            with self.subTest(case=name):
                b = self.bundle(name, [{"id": "z1", "filename": "z1_2026-02.zim", "version": "2026-02", **over}])
                self.assertEqual(self.install_from(cfg, b), 1, name)
                self.assertEqual(cur.read_bytes(), b"version one", f"{name}: previous version still current")
                self.assertEqual(upd.State(cfg.state_file).get("z1"), state_before)
                after = self.tree_snapshot(cfg.data_root)
                self.assertEqual({k: v for k, v in after.items() if not k.startswith(("sync", ".staging"))},
                                 {k: v for k, v in before.items() if not k.startswith(("sync", ".staging"))})
                self.assertFalse([p for p in (cfg.data_root / ".staging").rglob("*") if p.is_file()],
                                 f"{name}: nothing left behind in staging")

    def test_lying_http_peer_cannot_fill_the_disk(self):
        dest = self.tmp / "dl" / "f.bin"
        dest.parent.mkdir()
        FileServer.files["/f.bin"] = b"A" * 100_000
        with self.assertRaises(RuntimeError):
            upd.http_download(f"{self.base}/f.bin", dest, expected_size=1000)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name("f.bin.part").exists(), "oversized partial discarded")

    def test_directory_items_need_a_matching_tree_hash(self):
        cfg = self.disk(extra='hooks = {}')
        tree = {"index.html": b"<h1>v1</h1>", "js/app.js": b"console.log(1)", ".git/HEAD": b"ref: main"}
        b = self.bundle("t1", [{"id": "kiwix-js", "filename": "kiwix-js", "data": tree, "version": "2026-01-01"}])
        self.assertEqual(self.install_from(cfg, b), 0)
        app = cfg.data_root / "apps" / "kiwix-js"
        self.assertEqual((app / ".git" / "HEAD").read_bytes(), b"ref: main")
        tampers = {
            "modified": lambda p: (p / "js/app.js").write_bytes(b"steal()"),
            "added": lambda p: (p / "evil.js").write_bytes(b"steal()"),
            "git-hook": lambda p: ((p / ".git/hooks").mkdir(), (p / ".git/hooks/post-merge").write_bytes(b"rm -rf /")),
            "removed": lambda p: (p / "index.html").unlink(),
            "inner-symlink": lambda p: (p / "copy.html").symlink_to("index.html"),
        }
        v2 = {"index.html": b"<h1>v2</h1>", "js/app.js": b"console.log(2)", ".git/HEAD": b"ref: main"}
        for n, (name, tamper) in enumerate(tampers.items()):
            with self.subTest(tamper=name):
                ver = f"2026-02-{n + 10:02d}"
                b = self.bundle(f"t-{name}", [{"id": "kiwix-js", "filename": "kiwix-js", "data": v2, "version": ver}])
                tamper(b / "files" / "kiwix-js")                # after the manifest was written
                self.assertEqual(self.install_from(cfg, b), 1, name)
                self.assertEqual((app / "index.html").read_bytes(), b"<h1>v1</h1>", f"{name}: old tree kept")
                self.assertFalse((app / "passwd").exists() or (app / "evil.js").exists())
        with self.assertRaises(ValueError):
            upd.tree_sha256(self.tmp / "usb" / "t-inner-symlink" / "files" / "kiwix-js")
        # A link out of the tree planted on the stick is never copied; the genuine tree installs without it.
        b = self.bundle("t-outlink", [{"id": "kiwix-js", "filename": "kiwix-js", "data": v2, "version": "2026-03-01"}])
        (b / "files" / "kiwix-js" / "passwd").symlink_to("/etc/passwd")
        self.assertEqual(self.install_from(cfg, b), 0)
        self.assertFalse((app / "passwd").exists() or (app / "passwd").is_symlink())
        self.assertEqual(upd.State(cfg.state_file).get("kiwix-js")["version"], "2026-03-01")
        self.assertEqual((app / "index.html").read_bytes(), b"<h1>v2</h1>")

    def test_export_import_round_trip_with_hashes(self):
        a = self.disk("a")
        b1 = self.bundle("src", [
            {"id": "z1", "filename": "z1_2026-01.zim", "data": b"zim", "version": "2026-01", "sha256": ...},
            {"id": "kiwix-js", "filename": "kiwix-js", "data": {"a.html": b"a", "sub/b.js": b"b"}, "version": "1"}])
        # (sha256=... drops it: upd must compute one before sharing)
        man = json.loads((b1 / "manifest.json").read_text())
        self.assertNotIn("sha256", man["items"][0])
        a.require_signed_peers = False
        offers = upd.load_offer(str(b1), a)
        self.assertNotIn("z1", offers, "no sha256: refused")
        man["items"][0]["sha256"] = sha(b"zim")
        (b1 / "manifest.json").write_text(json.dumps(man))
        self.assertEqual(self.install_from(a, b1), 0)
        st = upd.State(a.state_file)
        st.data["items"]["z1"]["sha256"] = None                     # e.g. a pmtiles item: upstream gives none
        st.save()
        args = type("A", (), {"dest": str(self.tmp / "usb" / "out"), "only": None, "diffs": None})
        self.assertEqual(upd.cmd_export(a, args), 0)
        out = json.loads((self.tmp / "usb" / "out" / "manifest.json").read_text())
        by = {i["id"]: i for i in out["items"]}
        self.assertEqual(by["z1"]["sha256"], sha(b"zim"), "export computes the missing sha256")
        self.assertEqual(upd.State(a.state_file).get("z1")["sha256"], sha(b"zim"), "and keeps it")
        self.assertEqual(by["kiwix-js"]["tree_sha256"], upd.tree_sha256(a.data_root / "apps" / "kiwix-js"))
        # Exporting again over the same folder with a stray file in the tree: cleaned, still verifies.
        (self.tmp / "usb" / "out" / "files" / "apps" / "kiwix-js" / "stray.txt").write_text("x")
        self.assertEqual(upd.cmd_export(a, args), 0)
        c = self.disk("c")
        self.assertEqual(self.install_from(c, self.tmp / "usb" / "out"), 0)
        self.assertEqual(self.tree_snapshot(c.data_root / "apps" / "kiwix-js"),
                         self.tree_snapshot(a.data_root / "apps" / "kiwix-js"))
        man = json.loads(upd.write_manifest(a).read_text())
        self.assertTrue(all(i.get("sha256") for i in man["items"]), "peer manifest: every file has a sha256")

    # ── resume ──
    def test_resume_with_changed_or_misbehaving_remote(self):
        url, dest = f"{self.base}/f.bin", self.tmp / "dl" / "f.bin"
        dest.parent.mkdir()
        part, meta = dest.with_name("f.bin.part"), dest.with_name("f.bin.part.json")
        old, new = b"OLD-" * 1000, b"NEW!" * 1000

        def interrupted(prefix: bytes, etag: str | None, last_mod=None):
            part.write_bytes(prefix)
            meta.write_text(json.dumps({"url": url, "etag": etag, "last_modified": last_mod}))
            FileServer.seen.clear()
        FileServer.files["/f.bin"] = new
        FileServer.etags["/f.bin"] = '"v2"'
        # 1. Remote changed since the partial download: If-Range → whole new file.
        interrupted(old[:1500], '"v1"')
        upd.http_download(url, dest)
        self.assertEqual(dest.read_bytes(), new)
        self.assertEqual(FileServer.seen[0]["if-range"], '"v1"')
        # 2. Unchanged: a real resume (206 from the right offset).
        interrupted(new[:1500], '"v2"')
        upd.http_download(url, dest)
        self.assertEqual(dest.read_bytes(), new)
        self.assertEqual(FileServer.seen[0]["range"], "bytes=1500-")
        # 3. Server answers the resume with the wrong range: start over, never splice.
        FileServer.mode = "wrong-range"
        interrupted(new[:1500], '"v2"')
        upd.http_download(url, dest)
        self.assertEqual(dest.read_bytes(), new)
        # 4. No validator saved and no sha256 to check: don't risk a splice, start over.
        FileServer.mode = "honest"
        interrupted(old[:1500], None)
        upd.http_download(url, dest)
        self.assertEqual(dest.read_bytes(), new)
        self.assertIsNone(FileServer.seen[0]["range"])
        # 5. Server ignores If-Range and splices: only a sha256 can catch it — and does.
        FileServer.mode = "ignore-if-range"
        cfg = self.disk()
        interrupted(old[:1500], '"v1"')
        item = upd.Item(id="z1", title="t", category="reference", kind="zim", source={"type": "http"},
                        size_gb=0, tiers=["base"], renderer="zim")
        r = upd.Remote(version="2", filename="f.bin", url=url, size=len(new), sha256=sha(new))
        staging = cfg.data_root / ".staging" / "z1"
        staging.mkdir(parents=True)
        shutil.move(str(part), staging / "f.bin.part")
        shutil.move(str(meta), staging / "f.bin.part.json")
        with self.assertRaisesRegex(RuntimeError, "sha256 mismatch"):
            upd.transfer(upd.PlanEntry(item, None, r, "install", method="http"), cfg, staging)
        self.assertFalse(list(staging.iterdir()), "the spliced file is discarded; the next run starts clean")
        FileServer.mode = "honest"
        staged = upd.transfer(upd.PlanEntry(item, None, r, "install", method="http"), cfg, staging)
        self.assertEqual(staged.read_bytes(), new)

    # ── disk full ──
    def test_disk_full_keeps_previous_version(self):
        cfg = self.disk()
        v1 = self.bundle("v1", [{"id": "z1", "filename": "z1_2026-01.zim", "data": b"one", "version": "2026-01"},
                                {"id": "z2", "filename": "z2_2026-01.zim", "data": b"two", "version": "2026-01"}])
        self.assertEqual(self.install_from(cfg, v1), 0)
        cur = cfg.data_root / "zim" / "z1.current"
        good_state = json.loads(cfg.state_file.read_text())
        v2 = self.bundle("v2", [{"id": "z1", "filename": "z1_2026-02.zim", "data": b"ONE", "version": "2026-02"}])
        enospc = OSError(errno.ENOSPC, "No space left on device")

        # (a) while copying the new file in
        with mock.patch.object(upd.shutil, "copyfile", side_effect=enospc):
            self.assertEqual(self.install_from(cfg, v2), 1)
        self.assertEqual((os.readlink(cur), cur.read_bytes()), ("z1_2026-01.zim", b"one"))
        # (b) while writing the ledger, after the swap
        real_aw = upd.atomic_write

        def full_ledger(path, text):
            if Path(path) == cfg.state_file:
                raise enospc
            return real_aw(path, text)
        with mock.patch.object(upd, "atomic_write", full_ledger):
            self.assertEqual(self.install_from(cfg, v2), 1)
        self.assertEqual((os.readlink(cur), cur.read_bytes()), ("z1_2026-01.zim", b"one"), "link rolled back")
        self.assertFalse((cfg.data_root / "zim" / "z1_2026-02.zim").exists(), "new copy removed again")
        self.assertEqual(json.loads(cfg.state_file.read_text())["items"], good_state["items"])
        self.assertFalse(list(cfg.state_file.parent.glob(".*.tmp")), "no half-written ledger left behind")
        # (c) while re-pointing the stable link
        with mock.patch.object(upd, "point_current", side_effect=enospc):
            self.assertEqual(self.install_from(cfg, v2), 1)
        self.assertEqual((os.readlink(cur), cur.read_bytes()), ("z1_2026-01.zim", b"one"))
        # With space again, it goes through.
        self.assertEqual(self.install_from(cfg, v2), 0)
        self.assertEqual((os.readlink(cur), cur.read_bytes()), ("z1_2026-02.zim", b"ONE"))
        self.assertFalse((cfg.data_root / "zim" / "z1_2026-01.zim").exists(), "old version retired")

    def test_failed_ledger_write_leaves_memory_clean(self):
        cfg = self.disk()
        st = upd.State(cfg.state_file)
        st.put("z1", {"version": "1"}, "installed")
        with mock.patch.object(upd, "atomic_write", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                st.put("z1", {"version": "2"}, "updated")
        self.assertEqual(st.get("z1"), {"version": "1"})
        st.put("z2", {"version": "1"}, "installed")             # a later save can't smuggle version 2 in
        self.assertEqual(json.loads(cfg.state_file.read_text())["items"]["z1"], {"version": "1"})

    # ── locks and exit codes ──
    def test_concurrent_runs_and_exit_codes(self):
        cfg = self.disk()
        args = type("A", (), {"now": True, "offline": True, "only": None, "dry_run": False, "json": False,
                              "src": str(self.tmp), "dest": str(self.tmp / "out"), "diffs": None})
        with upd.run_lock(cfg):
            self.assertEqual(upd.cmd_apply(cfg, args), upd.EX_TEMPFAIL)
            self.assertEqual(upd.cmd_check(cfg, args), upd.EX_TEMPFAIL)
            self.assertEqual(upd.cmd_import(cfg, args), upd.EX_TEMPFAIL)
            self.assertEqual(upd.cmd_export(cfg, args), upd.EX_TEMPFAIL)
            st = cfg.data_root / ".staging" / "z1"
            st.mkdir(parents=True)
            (st / "z1_2026-01.zim").write_bytes(b"downloaded, about to be installed")
            self.assertEqual(upd.cmd_gc(cfg, args), 0)
            self.assertTrue((st / "z1_2026-01.zim").exists(), "gc leaves a running apply alone")
            with self.assertRaises(upd.Busy):
                upd.cmd_adopt(cfg, type("A", (), {"id": "z1", "path": None}))
            # And from another process, as systemd would run it.
            res = subprocess.run([sys.executable, str(REPO / "software/upd/upd.py"), "-c", str(cfg.data_root.parent / "prepper.toml"),
                                  "apply", "--now"], capture_output=True, text=True)
            self.assertEqual(res.returncode, 75, res.stderr)
        # Lock free, all sources manual: the plan was evaluated, nothing to do → 0.
        self.assertEqual(upd.cmd_apply(cfg, args), 0)

    def test_nothing_reachable_exits_75(self):
        port = closed_port()
        cat = f"""
[[item]]
id = "net1"
title = "Net"
category = "medical"
kind = "dataset"
size_gb = 0.0001
tiers = ["base"]
source = {{ type = "http", url = "http://127.0.0.1:{port}/a.json" }}

[[item]]
id = "z1"
title = "Manual"
category = "reference"
kind = "zim"
size_gb = 0.0001
tiers = ["base"]
source = {{ type = "manual" }}
"""
        cfg = self.disk(catalog=cat, extra="timeout = 2")
        args = type("A", (), {"now": True, "offline": False, "only": None, "dry_run": False})
        self.assertEqual(upd.cmd_apply(cfg, args), upd.EX_TEMPFAIL, "nothing could be checked: retry later")
        FileServer.files["/a.json"] = b"{}"
        cfg.timeout = 5
        (cfg.catalog).write_text(cat.replace(f"127.0.0.1:{port}", self.base.removeprefix("http://")))
        self.assertEqual(upd.cmd_apply(cfg, args), 0)
        self.assertEqual(upd.cmd_apply(cfg, args), 0, "evaluated, already current → 0")

    # ── gc ──
    def test_gc_never_follows_symlinks_out_of_data_root(self):
        cfg = self.disk()
        outside = self.tmp / "outside"
        (outside / "kind").mkdir(parents=True)
        for n in ("precious.part", "precious.part.json", "z1_2026-01.zim.part"):
            (outside / n).write_bytes(b"keep me")
        (outside / "kind" / ".x.old-1").mkdir()
        (outside / "kind" / ".x.old-1" / "f").write_bytes(b"keep me")
        staging = cfg.data_root / ".staging"
        staging.mkdir(parents=True)
        (staging / "z1").symlink_to(outside)                    # a wanted id, but a link
        (staging / "z2").mkdir()
        (staging / "z2" / "z2_2026-01.zim.part").symlink_to(outside / "precious.part")
        (staging / "unwanted").symlink_to(outside)
        (cfg.data_root / "maps").symlink_to(outside / "kind")   # a kind dir on another disk
        before = self.tree_snapshot(outside)
        self.assertEqual(upd.cmd_gc(cfg, None), 0)
        self.assertEqual(self.tree_snapshot(outside), before, "nothing outside data_root touched")
        self.assertFalse((staging / "z1").is_symlink() or (staging / "unwanted").is_symlink())
        # staging itself a symlink
        shutil.rmtree(staging)
        staging.symlink_to(outside)
        self.assertEqual(upd.cmd_gc(cfg, None), 0)
        self.assertEqual(self.tree_snapshot(outside), before)
        self.assertFalse(staging.exists() or staging.is_symlink())

    # ── hooks ──
    def test_hooks_are_per_item_recorded_and_retried(self):
        log = self.tmp / "hooks.log"
        flag = self.tmp / "fail"
        flag.write_text("1")
        extra = f"""
[upd.hooks]
app = "echo self-update-hook >> {log}"
prepper_software = "echo sw >> {log}; test ! -e {flag}"
zim = "echo zim >> {log}"
"""
        cfg = self.disk(extra=extra)
        tree = {"a": b"1"}
        b = self.bundle("h", [{"id": "kiwix-js", "filename": "kiwix-js", "data": tree, "version": "1"},
                              {"id": "prepper_software", "filename": "prepper_software", "data": tree, "version": "1"},
                              {"id": "z1", "filename": "z1_1.zim", "data": b"z", "version": "1"}])
        self.assertEqual(self.install_from(cfg, b), 0, "a failing hook doesn't fail the install")
        self.assertEqual(sorted(log.read_text().split()), ["sw", "zim"],
                         "the legacy kind-wide app hook does not self-update for kiwix-js")
        st = upd.State(cfg.state_file)
        self.assertEqual(st.get("prepper_software")["hook"]["status"], "failed")
        self.assertEqual(st.get("z1")["hook"]["status"], "ok")
        self.assertNotIn("hook", st.get("kiwix-js"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            upd.cmd_status(cfg, None)
        self.assertIn("post-install hook failed", out.getvalue())
        log.write_text("")
        flag.unlink()
        self.assertEqual(self.install_from(cfg, b), 0)          # nothing new to install …
        self.assertEqual(log.read_text().split(), ["sw"], "… but the failed hook is run again")
        self.assertEqual(upd.State(cfg.state_file).get("prepper_software")["hook"]["status"], "ok")
        log.write_text("")
        self.assertEqual(self.install_from(cfg, b), 0)
        self.assertEqual(log.read_text(), "", "a hook that succeeded is not repeated")

    # ── config, budgets, versions ──
    def test_config_types_are_checked(self):
        for line in ('budget_gb = "fifty"', 'budget_gb = nan', 'peers = "http://x"', 'require_signed_peers = "no"',
                     'max_rate_kbps = "fast"', 'hooks = "x"'):
            with self.subTest(line=line), self.assertRaises(SystemExit):
                self.disk("cfg", extra=line)
        self.assertEqual(self.disk("cfg2", extra='budget_gb = "50"').budget_gb, 50.0)
        self.assertEqual(self.disk("cfg3", extra="timeout = 7.9").timeout, 7)

    def test_budget_selection_edges(self):
        def it(i, size, pri=5, tiers=("pocket",), lang="eng"):
            return upd.Item(id=i, title=i, category="reference", kind="zim", source={}, size_gb=size,
                            tiers=list(tiers), renderer="zim", lang=lang, priority=pri)
        items = [it("a", 10, 1), it("b", 5, 2), it("c", 5, 2), it("d", 50, 3), it("spa", 1, 1, lang="spa"),
                 it("pin_only", 30, 4, tiers=()), it("zero", 0, 9)]
        cfg = upd.Config.load("/nonexistent")
        cfg.tier, cfg.languages = "pocket", ("eng",)
        ids = lambda: [i.id for i in upd.wanted(items, cfg)]         # noqa: E731
        pool = ["a", "b", "c", "d", "zero"]
        for budget in (0, -5, -1e9):
            cfg.budget_gb = budget
            self.assertEqual(ids(), pool, f"budget {budget}: no budget mode, the whole tier")
        cfg.budget_gb = 1e12
        self.assertEqual(sorted(ids()), sorted(pool))
        cfg.budget_gb = float("inf")
        self.assertEqual(sorted(ids()), sorted(pool))
        cfg.budget_gb = 0.0001
        self.assertEqual(ids(), ["zero"], "only zero-size items fit a tiny budget")
        cfg.budget_gb = 20
        self.assertEqual(ids(), ["a", "b", "c", "zero"], "priority order, smallest first within a priority")
        cfg.pins = ("pin_only", "pin_only", "nope", "d")
        chosen = ids()
        self.assertEqual(chosen[:2], ["pin_only", "d"], "pins first, once each, unknown pins ignored")
        self.assertEqual(chosen, ["pin_only", "d"], "pins over budget: all kept, nothing else is added")
        cfg.pins = ("spa",)
        cfg.budget_gb = 11
        self.assertEqual(ids(), ["spa", "a", "zero"], "a pin may be outside the device's languages")

    def test_version_comparison_edges(self):
        newer = [("133", "99"), ("2026-08", "2026-07a"), ("2026-07a", "2026-07"), ("2026-11-17", "2026-11"),
                 ("1.10", "1.9"), ("v2", "v1"), ("2026-07B", "2026-07a"), ("1", ""), ("0", None), ("a", "9"),
                 (10, 9), ("1.0.1", "1.0")]
        for a, b in newer:
            with self.subTest(a=a, b=b):
                self.assertTrue(upd.vnewer(a, b), f"{a!r} > {b!r}")
                self.assertFalse(upd.vnewer(b, a), f"not {b!r} > {a!r}")
        for a, b in (("", ""), (None, None), ("", None), ("2026-07A", "2026-07a"), ("1.0", "1-0"), ("v1", "V1"),
                     ("x", "x"), ("2026_07", "2026-07")):
            with self.subTest(equal=(a, b)):
                self.assertFalse(upd.vnewer(a, b))
                self.assertFalse(upd.vnewer(b, a))
        self.assertEqual(upd.vkey("!!!"), (), "punctuation only: like no version")

    def test_atomic_write_failure_keeps_the_old_file(self):
        p = self.tmp / "f.json"
        upd.atomic_write(p, "old")
        with mock.patch.object(upd.os, "fsync", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                upd.atomic_write(p, "new")
        self.assertEqual(p.read_text(), "old")
        self.assertEqual([x.name for x in self.tmp.iterdir()], ["f.json"], "scratch file removed")
        (self.tmp / "target").write_text("victim")
        tmpname = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmpname.symlink_to(self.tmp / "target")                 # a planted link at the scratch name
        upd.atomic_write(p, "new")
        self.assertEqual((self.tmp / "target").read_text(), "victim")
        self.assertEqual(p.read_text(), "new")

    def test_unsafe_upstream_file_names_refused(self):
        cfg = self.disk()
        item = upd.Item(id="z1", title="t", category="reference", kind="zim", source={"type": "http"},
                        size_gb=0, tiers=["base"], renderer="zim")
        for fn in ("", ".", "..", "../x", "a/b", ".hidden", "x\x00y"):
            r = upd.Remote(version="1", filename=fn, url=f"{self.base}/x")
            with self.subTest(fn=fn), self.assertRaises(RuntimeError):
                upd.transfer(upd.PlanEntry(item, None, r, "install", method="http"), cfg,
                             cfg.data_root / ".staging" / "z1")


if __name__ == "__main__":
    unittest.main()
