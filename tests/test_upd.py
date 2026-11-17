#!/usr/bin/env python3
"""
End-to-end tests for upd against a fake Kiwix mirror on localhost.
No internet needed.   python3 tests/test_upd.py -v
"""
from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "upd"))
import upd  # noqa: E402


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # Like library.kiwix.org: /catalog/v2/entries?name=X returns only entries named X.
        u = urllib.parse.urlparse(self.path)
        name = urllib.parse.parse_qs(u.query).get("name")
        feed = Path(self.directory) / u.path.lstrip("/")
        if u.path == "/catalog/v2/entries" and name and feed.is_file():
            xml = feed.read_text()
            head = xml.split("<entry>", 1)[0].replace("</feed>", "")
            keep = [e for e in re.findall(r"<entry>.*?</entry>", xml, re.S) if f"<name>{name[0]}</name>" in e]
            body = (head + "".join(keep) + "</feed>").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    _stopped = False

    def stop(self):                     # idempotent: shutdown() twice would block forever
        if not self._stopped:
            self._stopped = True
            self.shutdown()
            self.server_close()


def serve(directory: Path) -> tuple[Server, str]:
    srv = Server(("127.0.0.1", 0), functools.partial(Quiet, directory=str(directory)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Mirror:
    """Mimics download.kiwix.org (listing + .meta4) and library.kiwix.org OPDS."""

    def __init__(self, root: Path):
        self.root = root
        (root / "zim/wikipedia").mkdir(parents=True)
        (root / "zim/other").mkdir(parents=True)
        (root / "catalog/v2").mkdir(parents=True)
        (root / "files").mkdir()
        (root / "fdroid").mkdir()
        self.opds: list[str] = []

    def publish_zim(self, sub: str, name: str, ver: str, payload: bytes, in_opds=True, bad_sha=False,
                    base: str = "", flavour: str = "") -> None:
        d = self.root / "zim" / sub
        fn = f"{name}_{ver}.zim"
        (d / fn).write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest() if not bad_sha else "0" * 64
        (d / (fn + ".meta4")).write_text(
            f'<?xml version="1.0"?><metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="{fn}">'
            f'<size>{len(payload)}</size><hash type="sha-256">{sha}</hash></file></metalink>')
        links = "".join(f'<a href="{p.name}">{p.name}</a>\n' for p in sorted(d.glob("*.zim")))
        (d / "index.html").write_text(f"<html><pre>{links}</pre></html>")
        if in_opds:
            self.opds = [e for e in self.opds if f"<name>{base or name}</name><flavour>{flavour}</flavour>" not in e]
            self.opds.append(
                f"<entry><title>{name}</title><name>{base or name}</name><flavour>{flavour}</flavour>"
                f"<updated>{ver}-01T00:00:00Z</updated><articleCount>{len(payload) * 10}</articleCount>"
                f'<link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim" '
                f'href="URLBASE/zim/{sub}/{fn}.meta4" length="{len(payload)}"/></entry>')
        self.write_opds()

    def write_opds(self) -> None:
        self.opds_xml = '<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(self.opds) + "</feed>"

    def finalize(self, base: str) -> None:
        (self.root / "catalog/v2/entries").write_text(self.opds_xml.replace("URLBASE", base))


class UpdTest(unittest.TestCase):
    def setUp(self):
        upd._OPDS_FULL.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="upd-test-")).resolve()
        self.mirror = Mirror(self.tmp / "mirror")
        self.srv, self.base = serve(self.tmp / "mirror")
        # A tiny git repo stands in for the prepper software / kiwix-js app sources.
        self.git = self.tmp / "gitsrc"
        self.git.mkdir()
        run = lambda *a: subprocess.run(a, cwd=self.git, check=True, capture_output=True)
        run("git", "init", "-q", "-b", "main"); (self.git / "README").write_text("v1")
        run("git", "add", "."); run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def disk(self, name: str, extra: str = "", catalog: str | None = None) -> upd.Config:
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        cat = d / "catalog.toml"
        cat.write_text(catalog or self.catalog())
        (d / "prepper.toml").write_text(f"""
[upd]
data_root = "data"
state_file = "state/upd-state.json"
report_dir = "state/reports"
catalog = "catalog.toml"
tier = "base"
languages = ["eng"]
reserve_gb = 0
kiwix_opds = "{self.base}/catalog/v2/entries"
kiwix_http = "{self.base}/zim/"
kiwix_rsync = ""
allowed_signers = "allowed_signers"
signing_key = "key"
disk_id = "{name}"
{extra}
""")
        return upd.Config.load(d / "prepper.toml")

    def catalog(self) -> str:
        return f"""
[[item]]
id = "wiki_test_maxi"
title = "Test wiki"
category = "reference"
kind = "zim"
size_gb = 0.0001
tiers = ["base"]
priority = 1
source = {{ type = "kiwix", name = "wiki_en_all_maxi", path = "wikipedia/" }}

[[item]]
id = "zimgit-water_en"
title = "Water (listing only)"
category = "food-water"
kind = "zim"
size_gb = 0.0001
tiers = ["base"]
priority = 2
source = {{ type = "kiwix", name = "zimgit-water_en", path = "other/" }}

[[item]]
id = "manual_pdf"
title = "Hand-copied"
category = "medical"
kind = "pdfset"
size_gb = 0.5
tiers = ["base"]
source = {{ type = "manual" }}

[[item]]
id = "fixed_file"
title = "Fixed URL"
category = "medical"
kind = "dataset"
size_gb = 0.0001
tiers = ["base"]
source = {{ type = "http", url = "{self.base}/files/drugs.json" }}

[[item]]
id = "apk_test"
title = "App"
category = "tools"
kind = "apk"
size_gb = 0.0001
tiers = ["base"]
source = {{ type = "http", index = "{self.base}/fdroid/index.html", pattern = "org\\\\.test_(?P<v>\\\\d+)\\\\.apk", download_base = "{self.base}/fdroid/" }}

[[item]]
id = "sw"
title = "Software"
category = "tools"
kind = "app"
size_gb = 0.0001
tiers = ["base"]
source = {{ type = "git", url = "{self.git}", ref = "HEAD" }}
"""

    def publish_base(self):
        m = self.mirror
        m.publish_zim("wikipedia", "wiki_en_all_maxi", "2026-01", b"W" * 3000, base="wiki_en_all", flavour="maxi")
        m.publish_zim("other", "zimgit-water_en", "2024-08", b"water" * 100, in_opds=False)
        (m.root / "files/drugs.json").write_text('{"v":1}')
        for v in (99, 133, 7):                       # natural sort must pick 133, not 99
            (m.root / f"fdroid/org.test_{v}.apk").write_bytes(b"apk%d" % v)
        (m.root / "fdroid/index.html").write_text(
            "".join(f'<a href="https://f-droid.org/repo/org.test_{v}.apk">x</a>' for v in (99, 133, 7)))
        m.finalize(self.base)

    def plan(self, cfg, **kw):
        return {p.item.id: p for p in upd.build_plan(cfg, **kw)}

    # ── tests ──
    def test_versions_sort_naturally(self):
        self.assertTrue(upd.vnewer("133", "99"))
        self.assertTrue(upd.vnewer("2026-08", "2026-07a"))
        self.assertTrue(upd.vnewer("2026-07a", "2026-07"))
        self.assertFalse(upd.vnewer("2025-12", "2026-01"))

    def test_install_update_and_integrity(self):
        self.publish_base()
        cfg = self.disk("a")
        p = self.plan(cfg)
        self.assertEqual(p["wiki_test_maxi"].status, "install")
        self.assertEqual(p["wiki_test_maxi"].remote.version, "2026-01")
        self.assertIsNotNone(p["wiki_test_maxi"].remote.sha256, "sha256 comes from the .meta4")
        self.assertEqual(p["zimgit-water_en"].status, "install", "falls back to the mirror listing")
        self.assertEqual(p["apk_test"].remote.version, "133")
        self.assertEqual(p["manual_pdf"].status, "manual")
        self.assertEqual(p["sw"].method, "git-delta")

        self.assertEqual(upd.apply_plan(cfg, list(p.values()), dry_run=False), 0)
        st = upd.State(cfg.state_file)
        self.assertEqual(st.get("wiki_test_maxi")["version"], "2026-01")
        z = cfg.data_root / "zim" / "wiki_en_all_maxi_2026-01.zim"
        self.assertTrue(z.exists())
        self.assertEqual(os.readlink(cfg.data_root / "zim" / "wiki_test_maxi.current"), z.name)
        self.assertTrue((cfg.data_root / "apk" / "org.test_133.apk").exists())
        self.assertTrue((cfg.data_root / "apps" / "sw" / "README").exists())
        manifest = json.loads((cfg.data_root / "sync/manifest.json").read_text())
        self.assertEqual({i["id"] for i in manifest["items"]} >= {"wiki_test_maxi", "fixed_file"}, True)

        # Nothing changed upstream → everything current.
        p = self.plan(cfg)
        self.assertTrue(all(x.status in ("up-to-date", "manual") for x in p.values()),
                        {k: v.status for k, v in p.items()})

        # New Wikipedia build + new commit → updates with a diff note; old file retired.
        self.mirror.publish_zim("wikipedia", "wiki_en_all_maxi", "2026-02", b"W" * 4000, base="wiki_en_all", flavour="maxi")
        self.mirror.finalize(self.base)
        (self.git / "README").write_text("v2")
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "v2"], cwd=self.git, check=True)
        p = self.plan(cfg)
        self.assertEqual(p["wiki_test_maxi"].status, "update")
        self.assertIn("+10,000 articles", p["wiki_test_maxi"].note)
        self.assertEqual(p["sw"].status, "update")
        self.assertEqual(upd.apply_plan(cfg, list(p.values()), dry_run=False), 0)
        self.assertFalse(z.exists(), "old version removed when keep_previous = false")
        self.assertTrue((cfg.data_root / "zim" / "wiki_en_all_maxi_2026-02.zim").exists())
        self.assertEqual((cfg.data_root / "apps/sw/README").read_text(), "v2")

        # Corrupted upstream file: rejected, installed version untouched.
        self.mirror.publish_zim("wikipedia", "wiki_en_all_maxi", "2026-03", b"X" * 4000, bad_sha=True,
                                base="wiki_en_all", flavour="maxi")
        self.mirror.finalize(self.base)
        p = self.plan(cfg, only={"wiki_test_maxi"})
        self.assertEqual(upd.apply_plan(cfg, list(p.values()), dry_run=False), 1)
        self.assertEqual(upd.State(cfg.state_file).get("wiki_test_maxi")["version"], "2026-02")

    def test_offline_reports_unreachable(self):
        cfg = self.disk("off")
        self.srv.stop()
        p = self.plan(cfg)
        self.assertEqual(p["wiki_test_maxi"].status, "offline")
        self.assertEqual(p["manual_pdf"].status, "manual")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen needed for signatures")
    def test_signed_bundle_and_peer(self):
        self.publish_base()
        a = self.disk("a")
        upd.apply_plan(a, upd.build_plan(a), dry_run=False)
        key = self.tmp / "a" / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        pub = (self.tmp / "a" / "key.pub").read_text().split()
        signers = f'prepper-update namespaces="prepper-update" {pub[0]} {pub[1]}\n'
        bundle = self.tmp / "usb" / "bundle"
        args = type("A", (), {"dest": str(bundle), "only": None, "diffs": None})
        self.assertEqual(upd.cmd_export(a, args), 0)
        self.assertTrue((bundle / "manifest.json.sig").exists())

        # Disk B is offline and trusts A: installs everything from the USB stick.
        b = self.disk("b")
        (self.tmp / "b" / "allowed_signers").write_text(signers)
        self.srv.stop()                                     # no internet from here on
        offers = upd.load_offer(str(bundle), b, self.tmp / "b")
        self.assertIn("wiki_test_maxi", offers)
        p = {x.item.id: x for x in upd.build_plan(b, offline=True, offers=offers)}
        self.assertEqual(p["wiki_test_maxi"].method, "copy")
        self.assertEqual(upd.apply_plan(b, list(p.values()), dry_run=False), 0)
        self.assertEqual(upd.State(b.state_file).get("wiki_test_maxi")["version"], "2026-01")

        # Disk C doesn't trust A: the bundle is refused.
        c = self.disk("c")
        self.assertEqual(upd.load_offer(str(bundle), c, self.tmp / "c"), {})

        # Disk D pulls from A over the LAN (A's /sync served over HTTP).
        # write_manifest signs it (re-signing over the .sig left by the export-time apply).
        # A bare `ssh-keygen -Y sign` here would stop at its "Overwrite?" prompt.
        upd.write_manifest(a)
        (self.tmp / "a" / "allowed_signers").write_text(signers)
        self.assertTrue(upd.verify_signature(a.data_root / "sync/manifest.json", a))
        psrv, pbase = serve(a.data_root)
        try:
            d = self.disk("d", extra=f'peers = ["{pbase}/sync"]')
            (self.tmp / "d" / "allowed_signers").write_text(signers)
            p = {x.item.id: x for x in upd.build_plan(d, offline=True)}
            self.assertEqual(p["wiki_test_maxi"].method, "peer")
            self.assertEqual(upd.apply_plan(d, list(p.values()), dry_run=False), 0)
            self.assertTrue((d.data_root / "zim/wiki_en_all_maxi_2026-01.zim").exists())
        finally:
            psrv.stop()

    # ── regression tests for review findings ──
    def make_key(self, disk: str) -> str:
        key = self.tmp / disk / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        pub = (self.tmp / disk / "key.pub").read_text().split()
        signers = f'prepper-update namespaces="prepper-update" {pub[0]} {pub[1]}\n'
        (self.tmp / disk / "allowed_signers").write_text(signers)
        return signers

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen needed for signatures")
    def test_resigning_replaces_stale_signature(self):
        """ssh-keygen -Y sign won't overwrite an existing .sig (prompts, reads EOF as no, exits 0)."""
        a = self.disk("a")
        self.make_key("a")
        m1 = upd.write_manifest(a)
        sig = m1.with_name("manifest.json.sig")
        self.assertTrue(sig.exists())
        self.assertTrue(upd.verify_signature(m1, a))
        first = sig.read_bytes()
        a.disk_id = "a-renamed"                       # different manifest content, same file name
        m2 = upd.write_manifest(a)
        self.assertIn("a-renamed", m2.read_text())
        self.assertNotEqual(sig.read_bytes(), first, "signature must be regenerated")
        self.assertTrue(upd.verify_signature(m2, a), "fresh signature must match the new manifest")
        self.assertFalse(list(m2.parent.glob(".manifest.json.signing*")), "scratch files cleaned up")
        # And a signing failure must not leave the old signature next to new content.
        a.signing_key.write_text("not a key")
        a.disk_id = "a-third"
        upd.write_manifest(a)
        self.assertFalse(sig.exists())

    def test_malicious_bundle_cannot_escape_data_root(self):
        self.publish_base()
        b = self.disk("b", extra="require_signed_peers = false")
        bundle = self.tmp / "usb" / "evil"
        (bundle / "files").mkdir(parents=True)
        (self.tmp / "usb" / "secret.txt").write_text("outside the bundle")
        (bundle / "files" / "x.zim").write_bytes(b"payload")
        (bundle / "files" / "link.zim").symlink_to(self.tmp / "usb" / "secret.txt")
        (bundle / "files" / "drugs.json").write_text('{"v":2}')
        items = [
            {"id": "wiki_test_maxi", "version": "2099-01", "filename": "../../../../pwned.zim", "path": "files/x.zim"},
            {"id": "zimgit-water_en", "version": "2099-01", "filename": "water.zim", "path": "../secret.txt"},
            {"id": "manual_pdf", "version": "2099-01", "filename": "manual.pdf", "path": "files/link.zim"},
            {"id": "apk_test", "version": "999", "filename": "/tmp/abs.apk", "path": "/etc/passwd"},
            {"id": "sw", "version": "x", "filename": ".bashrc", "path": "files/x.zim"},
            {"id": "fixed_file", "version": "v2", "filename": "drugs.json", "path": "files/drugs.json",
             "comparable": False, "obtained": "2099-01-01T00:00:00+00:00",
             "sha256": hashlib.sha256(b'{"v":2}').hexdigest()},
        ]
        (bundle / "manifest.json").write_text(json.dumps({"disk_id": "evil", "generated": upd.now_iso(), "items": items}))
        offers = upd.load_offer(str(bundle), b, self.tmp / "b")
        self.assertEqual(set(offers), {"fixed_file"}, "only the well-formed entry survives")
        before = {p for p in self.tmp.rglob("*") if not str(p).startswith(str(b.data_root))}
        args = type("A", (), {"src": str(bundle), "only": None, "dry_run": False})
        self.srv.stop()
        upd.cmd_import(b, args)
        after = {p for p in self.tmp.rglob("*") if not str(p).startswith(str(b.data_root))}
        new_outside = {p for p in after - before if not str(p).startswith(str(b.state_file.parent))}
        self.assertEqual(new_outside, set(), "nothing written outside data_root / state dir")
        self.assertFalse(any(p.name in ("pwned.zim", "abs.apk", ".bashrc") for p in self.tmp.rglob("*")))
        self.assertEqual((b.data_root / "data" / "drugs.json").read_text(), '{"v":2}')
        self.assertFalse(any("secret" in p.read_text(errors="ignore") for p in b.data_root.rglob("*") if p.is_file()))

    def test_opds_href_decides_subdir_and_file_stem_matches(self):
        m = self.mirror
        (m.root / "zim/books").mkdir()
        (m.root / "zim/gutenberg").mkdir()
        # Moved: the catalog still says gutenberg/, OPDS points at books/.
        m.publish_zim("books", "gutenberg_en_lcc-r", "2026-11a", b"G" * 500, base="gutenberg_en_lcc-r")
        # OPDS name differs from the file stem (gardenology.org_en_all is listed as gardenology_en_all).
        m.publish_zim("other", "gardenology.org_en_all", "2026-09", b"P" * 300, base="gardenology_en_all")
        m.finalize(self.base)
        cat = """
[[item]]
id = "gutenberg_en_lcc-r"
title = "G"
category = "books"
kind = "zim"
tiers = ["base"]
source = { type = "kiwix", name = "gutenberg_en_lcc-r", path = "gutenberg/" }

[[item]]
id = "gardenology.org_en_all"
title = "P"
category = "food-water"
kind = "zim"
tiers = ["base"]
source = { type = "kiwix", name = "gardenology.org_en_all", path = "other/" }
"""
        cfg = self.disk("g", catalog=cat)
        cfg.kiwix_rsync = "rsync://mirror.example/zim/"
        p = self.plan(cfg)
        g = p["gutenberg_en_lcc-r"].remote
        self.assertEqual(g.version, "2026-11a")
        self.assertTrue(g.url.endswith("/zim/books/gutenberg_en_lcc-r_2026-11a.zim"))
        self.assertEqual(g.rsync_url, "rsync://mirror.example/zim/books/gutenberg_en_lcc-r_2026-11a.zim")
        self.assertIsNotNone(g.sha256)
        self.assertIn("articleCount", g.meta, "found through OPDS, not the listing fallback")
        gd = p["gardenology.org_en_all"].remote
        self.assertEqual((gd.version, gd.filename), ("2026-09", "gardenology.org_en_all_2026-09.zim"))
        self.assertIn("articleCount", gd.meta, "matched by file stem in the full catalog")
        self.assertEqual(upd.kiwix_subdir_from_url("https://x/zim/books/a_2026-01.zim"), "books/")
        self.assertIsNone(upd.kiwix_subdir_from_url("https://x/other/a.zim"))

    def test_peer_cannot_downgrade_noncomparable_or_replay(self):
        old_t, new_t = "2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"
        local = {"version": "etag-new", "comparable": False, "obtained": new_t, "sha256": None}
        stale = upd.Remote(version="etag-old", filename="f", url="u", comparable=False, origin="peer:x",
                           meta={"obtained": old_t})
        fresh = upd.Remote(version="etag-newer", filename="f", url="u", comparable=False, origin="peer:x",
                           meta={"obtained": "2026-07-01T00:00:00+00:00"})
        self.assertFalse(upd.offer_is_newer(stale, local), "older copy on a peer is not an update")
        self.assertTrue(upd.offer_is_newer(fresh, local))
        self.assertTrue(upd.is_newer(stale, local), "(plain is_newer would have flip-flopped)")

        # Offline disk with the newer copy sees a peer bundle holding the old one: stays put.
        b = self.disk("b", extra="require_signed_peers = false")
        st = upd.State(b.state_file)
        st.put("fixed_file", {**local, "filename": "drugs.json", "path": "data/drugs.json"}, "installed")
        bundle = self.tmp / "usb" / "old"
        (bundle / "files").mkdir(parents=True)
        (bundle / "files" / "drugs.json").write_text("old")
        man = {"disk_id": "peer", "generated": "2026-02-01T00:00:00+00:00", "items": [
            {"id": "fixed_file", "version": "etag-old", "filename": "drugs.json", "path": "files/drugs.json",
             "comparable": False, "obtained": old_t, "sha256": hashlib.sha256(b"old").hexdigest()}]}
        (bundle / "manifest.json").write_text(json.dumps(man))
        offers = upd.load_offer(str(bundle), b, self.tmp / "b")
        p = {x.item.id: x for x in upd.build_plan(b, offline=True, offers=offers)}
        self.assertEqual(p["fixed_file"].status, "up-to-date")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen needed for signatures")
    def test_old_signed_manifest_is_not_replayed(self):
        a = self.disk("a")
        self.make_key("a")
        bundle = self.tmp / "usb" / "replay"
        bundle.mkdir(parents=True)
        def publish(generated):
            (bundle / "manifest.json").write_text(json.dumps({"disk_id": "a", "generated": generated, "items": []}))
            self.assertTrue(upd.sign_file(bundle / "manifest.json", a))
            return upd.load_offer(str(bundle), a, self.tmp / "a")
        publish("2026-05-01T00:00:00+00:00")
        marks = json.loads(upd._peer_marks_path(a).read_text())
        self.assertEqual(marks["a"], "2026-05-01T00:00:00+00:00")
        publish("2026-04-01T00:00:00+00:00")             # replay of an older signed manifest
        marks = json.loads(upd._peer_marks_path(a).read_text())
        self.assertEqual(marks["a"], "2026-05-01T00:00:00+00:00", "older manifest rejected, mark kept")

    def test_resume_discards_partial_when_server_sends_whole_file(self):
        (self.mirror.root / "files/big.bin").write_bytes(b"N" * 5000)
        dest = self.tmp / "dl" / "big.bin"
        dest.parent.mkdir()
        dest.with_name("big.bin.part").write_bytes(b"OLD-VERSION-BYTES")
        dest.with_name("big.bin.part.json").write_text(json.dumps(
            {"url": f"{self.base}/files/big.bin", "etag": '"old"', "last_modified": None}))
        upd.http_download(f"{self.base}/files/big.bin", dest)   # SimpleHTTP ignores Range → 200
        self.assertEqual(dest.read_bytes(), b"N" * 5000)
        self.assertFalse(dest.with_name("big.bin.part.json").exists())

    def test_gc_drops_parts_of_old_versions(self):
        cfg = self.disk("gc")
        st = cfg.data_root / ".staging" / "wiki_test_maxi"
        st.mkdir(parents=True)
        (st / "wiki_en_all_maxi_2025-12.zim.part").write_bytes(b"x")
        (st / "wiki_en_all_maxi_2026-02.zim.part").write_bytes(b"y")
        cfg.report_dir.mkdir(parents=True)
        (cfg.report_dir / "last-check.json").write_text(json.dumps({"items": [
            {"id": "wiki_test_maxi", "remote_filename": "wiki_en_all_maxi_2026-02.zim"}]}))
        (cfg.data_root / ".staging" / "not_in_catalog").mkdir()
        upd.cmd_gc(cfg, None)
        self.assertEqual(sorted(p.name for p in st.iterdir()), ["wiki_en_all_maxi_2026-02.zim.part"])
        self.assertFalse((cfg.data_root / ".staging" / "not_in_catalog").exists())

    def test_adopt_links_current_and_tree_reimport_replaces_dir(self):
        cfg = self.disk("t")
        tree = cfg.data_root / "docs" / "manual_pdf"
        tree.mkdir(parents=True)
        (tree / "old.pdf").write_text("old")
        args = type("A", (), {"id": "manual_pdf", "path": None})
        upd.cmd_adopt(cfg, args)
        self.assertEqual(os.readlink(cfg.data_root / "docs" / "manual_pdf.current"), "manual_pdf")
        # A bundle brings a new version of the same tree under the same name.
        bundle = self.tmp / "usb" / "tree"
        (bundle / "files/docs/manual_pdf").mkdir(parents=True)
        (bundle / "files/docs/manual_pdf/new.pdf").write_text("new")
        (bundle / "manifest.json").write_text(json.dumps({"disk_id": "x", "generated": upd.now_iso(), "items": [
            {"id": "manual_pdf", "version": "2999-01-01", "filename": "manual_pdf",
             "path": "files/docs/manual_pdf", "comparable": True,
             "tree_sha256": upd.tree_sha256(bundle / "files/docs/manual_pdf")}]}))
        cfg.require_signed_peers = False
        offers = upd.load_offer(str(bundle), cfg, self.tmp / "t")
        plan = upd.build_plan(cfg, offline=True, offers=offers, only={"manual_pdf"})
        self.assertEqual(upd.apply_plan(cfg, plan, dry_run=False), 0)
        self.assertEqual(sorted(p.name for p in tree.iterdir()), ["new.pdf"])
        self.assertFalse(list((cfg.data_root / "docs").glob(".manual_pdf.old-*")), "old tree removed")
        self.assertEqual(upd.State(cfg.state_file).get("manual_pdf")["version"], "2999-01-01")

    def test_budget_editions(self):
        cat = upd.load_catalog(REPO / "content/catalog.toml")
        cfg = upd.Config.load("/nonexistent")
        cfg.tier, cfg.budget_gb, cfg.languages = "pocket", 20, ("eng",)
        chosen = upd.wanted(cat, cfg)
        self.assertLessEqual(sum(i.size_gb for i in chosen), 20)
        ids = {i.id for i in chosen}
        self.assertIn("mdwiki_en_all_maxi", ids, "priority-1 medical always fits")
        self.assertIn("ready.gov_en_all", ids)
        cfg.pins = ("wikipedia_en_all_nopic",)
        cfg.budget_gb = 80
        self.assertIn("wikipedia_en_all_nopic", {i.id for i in upd.wanted(cat, cfg)})

    def test_sale_editions_never_select_personal_items(self):
        cat = upd.load_catalog(REPO / "content/catalog.toml")
        personal = {i.id for i in cat if i.use == "personal"}
        cfg = upd.Config.load("/nonexistent")
        cfg.provisioning, cfg.personal_file = "sale", self.tmp / "no-record.json"
        cfg.languages = ("eng", "spa", "fra")
        for tier in ("field", "base", "ultimate", "pocket"):
            cfg.tier, cfg.budget_gb, cfg.pins = tier, 0, ()
            self.assertFalse({i.id for i in upd.wanted(cat, cfg)} & personal, tier)
            cfg.budget_gb, cfg.pins = 5000, tuple(sorted(personal))
            self.assertFalse({i.id for i in upd.wanted(cat, cfg)} & personal, f"{tier} with pins")

    def test_internet_plan_skips_personal_items_on_a_sale_unit(self):
        self.publish_base()
        self.mirror.publish_zim("other", "nc_course_en", "2026-01", b"N" * 500, in_opds=False)
        self.mirror.finalize(self.base)
        # (the test catalog's items carry no `use`, which counts as personal: mark them open)
        cat = self.catalog().replace("source = {", 'license = "CC BY-SA"\nuse = "any"\nsource = {') + """
[[item]]
id = "nc_course_en"
title = "Non-commercial course"
category = "learn"
kind = "zim"
size_gb = 0.0001
tiers = ["base"]
license = "CC BY-NC-SA"
use = "personal"
source = { type = "kiwix", name = "nc_course_en", path = "other/" }
"""
        owner = self.disk("own", catalog=cat)
        self.assertEqual(self.plan(owner)["nc_course_en"].status, "install", "owner-built: as before")
        sale = self.disk("sale", catalog=cat, extra='personal_file = "personal.json"')
        (self.tmp / "sale" / "prepper.toml").write_text('[device]\nprovisioning = "sale"\n'
                                                       + (self.tmp / "sale" / "prepper.toml").read_text())
        sale = upd.Config.load(self.tmp / "sale" / "prepper.toml")
        p = self.plan(sale)
        self.assertNotIn("nc_course_en", p)
        self.assertEqual(p["wiki_test_maxi"].status, "install")
        self.assertEqual(upd.apply_plan(sale, list(p.values()), dry_run=False), 0)
        self.assertFalse(list((sale.data_root / "zim").glob("nc_course_en*")))
        upd.record_personal_decision(sale, "accept", by="new owner")
        self.assertEqual(self.plan(sale)["nc_course_en"].status, "install", "after the owner accepts")

    def test_catalog_is_consistent(self):
        cat = upd.load_catalog(REPO / "content/catalog.toml")
        import tomllib
        kinds = tomllib.loads((REPO / "content/renderers.toml").read_text())["kind"]
        cats = {"reference", "medical", "survival", "food-water", "energy-repair", "comms", "maps",
                "learn", "books", "video", "ai", "tools", "files"}
        for i in cat:
            self.assertIn(i.kind, kinds, f"{i.id}: no renderer for kind {i.kind}")
            self.assertIn(i.category, cats, i.id)
            self.assertIn(i.source.get("type"), upd.RESOLVERS, i.id)
            for t in i.tiers:
                self.assertIn(t, ("field", "base", "ultimate", "pocket"), i.id)


if __name__ == "__main__":
    unittest.main()
