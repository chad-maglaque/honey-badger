#!/usr/bin/env python3
"""
Catalog consistency: content/catalog.toml, content/renderers.toml and the
generated docs/CONTENTS.md must agree with each other and with the code.
    python3 tests/test_catalog.py -v
"""
from __future__ import annotations

import math
import os
import re
import sys
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "upd"))
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "tools"))
os.environ["PORTAL_QUIET"] = "1"
import gen_contents  # noqa: E402
import server  # noqa: E402
import upd  # noqa: E402

CATALOG = REPO / "content" / "catalog.toml"
RENDERERS = REPO / "content" / "renderers.toml"
REQUIRED = ("id", "title", "category", "kind", "lang", "size_gb", "tiers", "priority", "license",
            "description", "source")
TIERS = {"pocket", "field", "base", "ultimate"}
SOURCE_KEYS = {                     # what each resolver in upd.py reads from `source`
    "kiwix": ({"name"}, {"name", "path"}),
    "http": (set(), {"url", "filename", "index", "pattern", "download_base", "sha256_url"}),
    "hf": ({"repo", "file"}, {"repo", "file", "revision"}),
    "git": ({"url"}, {"url", "ref", "commit"}),
    "rsync": ({"url"}, {"url"}),
    "pmtiles": (set(), {"builds", "base", "bbox", "maxzoom"}),
    "manual": (set(), set()),
}


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = tomllib.loads(CATALOG.read_text())["item"]
        cls.kinds = tomllib.loads(RENDERERS.read_text())["kind"]

    def each(self):
        return self.raw

    def test_required_fields_and_types(self):
        for i in self.each():
            missing = [k for k in REQUIRED if k not in i]
            self.assertEqual(missing, [], f"{i.get('id')}: missing {missing}")
            for k in ("id", "title", "category", "kind", "lang", "license", "description"):
                self.assertIsInstance(i[k], str, f"{i['id']}.{k}")
                self.assertEqual(i[k], i[k].strip(), f"{i['id']}.{k} has stray whitespace")
            self.assertTrue(i["title"] and i["description"], i["id"])
            self.assertIsInstance(i["size_gb"], (int, float), i["id"])
            self.assertTrue(math.isfinite(i["size_gb"]) and i["size_gb"] >= 0, f"{i['id']}: size_gb {i['size_gb']}")
            self.assertIsInstance(i["priority"], int, i["id"])
            self.assertNotIsInstance(i["priority"], bool, i["id"])
            self.assertIn(i["priority"], range(1, 10), f"{i['id']}: priority 1–9")
            self.assertIsInstance(i["tiers"], list, i["id"])
            self.assertTrue(re.fullmatch(r"[a-z]{3}", i["lang"]), f"{i['id']}: lang {i['lang']!r} (ISO 639-3)")

    def test_tiers_categories_kinds_sources_are_known(self):
        cats = {c for c, _, _ in server.CATEGORIES}
        for i in self.each():
            self.assertIn(i["category"], cats, i["id"])
            self.assertLessEqual(set(i["tiers"]), TIERS, f"{i['id']}: tiers {i['tiers']}")
            self.assertEqual(len(i["tiers"]), len(set(i["tiers"])), f"{i['id']}: duplicate tier")
            self.assertIn(i["kind"], upd.KIND_DIRS, f"{i['id']}: kind {i['kind']} has no data_root folder")
            src = i["source"]
            self.assertIsInstance(src, dict, i["id"])
            self.assertIn(src.get("type"), upd.RESOLVERS, i["id"])
            self.assertIn(src["type"], SOURCE_KEYS, i["id"])
            need, allowed = SOURCE_KEYS[src["type"]]
            keys = set(src) - {"type"}
            self.assertLessEqual(need, keys, f"{i['id']}: source needs {need - keys}")
            self.assertLessEqual(keys, allowed, f"{i['id']}: unknown source keys {keys - allowed}")

    def test_git_code_is_pinned_to_a_commit(self):
        # red-team finding 8: web apps run on the families' phones, so a git checkout of code is pinned to a
        # full commit id (upd.resolve_git installs that commit or nothing). prepper_software is exempt: its
        # commits must carry a maintainer signature (prepperctl self-update-hook).
        import re
        for i in self.raw:
            src = i["source"]
            if src["type"] == "git" and i["id"] != "prepper_software":
                self.assertRegex(src.get("commit", ""), re.compile(r"^[0-9a-f]{40}$"), i["id"])
                self.assertNotEqual(src.get("ref", "HEAD"), "HEAD", i["id"])

    def test_source_details(self):
        for i in self.each():
            src = i["source"]
            if src["type"] == "kiwix":
                self.assertTrue(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", src["name"]), i["id"])
                self.assertTrue(src.get("path", "x/").endswith("/"), f"{i['id']}: path must end in /")
                self.assertNotIn("..", src.get("path", ""), i["id"])
                self.assertEqual(i["kind"], "zim", i["id"])
            elif src["type"] == "http":
                self.assertTrue(("url" in src) != ("index" in src), f"{i['id']}: exactly one of url / index")
                if "index" in src:
                    self.assertIn("pattern", src, i["id"])
                    self.assertIn("v", re.compile(src["pattern"]).groupindex, f"{i['id']}: pattern needs (?P<v>…)")
                for k in ("url", "index", "download_base"):
                    if k in src:
                        self.assertTrue(src[k].startswith("https://"), f"{i['id']}: {k} must be https")
            elif src["type"] == "hf":
                self.assertTrue(re.fullmatch(r"[\w.-]+/[\w.-]+", src["repo"]), i["id"])
                self.assertNotIn("..", src["file"], i["id"])
            elif src["type"] == "git":
                self.assertTrue(src["url"].startswith("https://"), i["id"])
            elif src["type"] == "pmtiles":
                if "bbox" in src:
                    b = src["bbox"]
                    self.assertEqual(len(b), 4, i["id"])
                    self.assertTrue(-180 <= b[0] < b[2] <= 180 and -90 <= b[1] < b[3] <= 90, f"{i['id']}: bbox {b}")

    def test_ids_unique_and_safe(self):
        ids = [i["id"] for i in self.raw]
        dupes = sorted({x for x in ids if ids.count(x) > 1})
        self.assertEqual(dupes, [], "duplicate ids")
        for x in ids:
            with self.subTest(id=x):
                # Ids become staging folder names and file names: never path syntax.
                self.assertTrue(upd.SAFE_ID.fullmatch(x), x)
                self.assertTrue(upd.safe_name(x), x)
        self.assertEqual(len(upd.load_catalog(CATALOG)), len(ids), "upd loads every item")

    def test_tierless_items_are_pin_only(self):
        # An empty tiers list is allowed: such an item is only installed when pinned.
        cfg = upd.Config.load("/nonexistent")
        items = upd.load_catalog(CATALOG)
        for tier in TIERS:
            cfg.tier = tier
            chosen = {i.id for i in upd.wanted(items, cfg)}
            for i in self.raw:
                if not i["tiers"]:
                    self.assertNotIn(i["id"], chosen)

    def test_every_kind_has_a_renderer(self):
        for i in self.each():
            self.assertIn(i["kind"], self.kinds, f"{i['id']}: no renderer for kind {i['kind']}")
            self.assertTrue(self.kinds[i["kind"]].get("renderer"), f"kind {i['kind']} lists no renderers")

    def test_renderers_are_well_formed(self):
        ids = {i["id"]: i for i in self.raw}
        services = set(server.PortalConfig(Path("/nonexistent/prepper.toml")).services)
        for name, kind in self.kinds.items():
            with self.subTest(kind=name):
                rends = kind.get("renderer", [])
                rids = [r["id"] for r in rends]
                self.assertEqual(len(rids), len(set(rids)), "duplicate renderer ids")
                if "default" in kind:
                    self.assertIn(kind["default"], rids, "default must be one of the renderers")
                for r in rends:
                    self.assertTrue(r.get("name") and r.get("url"), r)
                    self.assertTrue(r["url"].startswith("/"), f"{r['id']}: renderer URLs stay on the Badger")
                    self.assertFalse(re.search(r"\{(?!id\}|book\}|file\}|url\})[^}]*\}", r["url"]),
                                     f"{r['id']}: unknown placeholder in {r['url']}")
                    req = r.get("requires")
                    if req and req.startswith("app:"):
                        self.assertIn(req[4:], ids, f"{r['id']}: requires unknown app {req}")
                    elif req:
                        self.assertIn(req, services, f"{r['id']}: requires unknown service {req}")

    def test_use_field_matches_licence(self):
        """`use` says whether an item may ship on a Badger that is sold (see the catalog header)."""
        loaded = {i.id: i for i in upd.load_catalog(CATALOG)}
        for i in self.each():
            self.assertIn(i.get("use"), upd.USES, f"{i['id']}: use must be one of {upd.USES}")
            if upd.licence_is_noncommercial(i["license"]):
                self.assertEqual(i["use"], "personal", f"{i['id']}: {i['license']} is non-commercial")
            self.assertEqual(loaded[i["id"]].use, i["use"], i["id"])
        nc = [i["id"] for i in self.each() if upd.licence_is_noncommercial(i["license"])]
        self.assertGreaterEqual(len(nc), 26)

    def test_contents_md_matches_catalog(self):
        doc = (REPO / "docs" / "CONTENTS.md").read_text()
        self.assertEqual(doc, gen_contents.render(gen_contents.load_items()),
                         "docs/CONTENTS.md is stale: run python3 tools/gen_contents.py")
        # And spelled out: the edition totals in the table are the catalog's tier sums.
        rows = {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(
            r"^\| (Pocket|Field|Base|Ultimate)\b[^|]*\|[^|]*\| ([\d,]+) GB \| ([\d,]+) GB \|$", doc, re.M)}
        self.assertEqual(set(rows), {"Pocket", "Field", "Base", "Ultimate"})
        for tier, (eng, spa) in rows.items():
            t = tier.lower()
            self.assertEqual(int(eng.replace(",", "")), round(gen_contents.total(self.raw, t)), tier)
            self.assertEqual(int(spa.replace(",", "")), round(gen_contents.total(self.raw, t, ("eng", "spa"))), tier)

    def test_category_lists_agree(self):
        self.assertEqual([c for c, _ in gen_contents.CATS], [c for c, _, _ in server.CATEGORIES])


if __name__ == "__main__":
    unittest.main()
