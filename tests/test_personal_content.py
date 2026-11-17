#!/usr/bin/env python3
"""
Personal-use (non-commercial) content and Badgers built for sale.

Content licensed for personal / non-commercial use only (catalog `use = "personal"`)
must never be on a Badger when it is sold ([device] provisioning = "sale",
`prepper-build --for-sale`). The owner is offered it in the launcher and, once
they accept, their own Badger downloads it. Covers the catalog field, the loader's
fail-safe defaults, selection (tiers, pins, budgets can't bring it back), every
install path (internet plan, USB bundle, adopt), the owner's acceptance record,
removal, the portal API over HTTP and the builder's --for-sale flag.
    python3.11 tests/test_personal_content.py -v
"""
from __future__ import annotations

import http.client
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "upd"))
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"
import server  # noqa: E402
import upd  # noqa: E402

CATALOG = REPO / "content" / "catalog.toml"
# Named in the brief: every one of these must be personal-use only.
KNOWN_NC = (["hesperian_books", "wikihow_personal", "ifixit_en_all", "khanacademy_en_all", "crashcourse_en_all"]
            + [f"libretexts.org_en_{s}" for s in ("k12", "workforce", "bio", "chem", "eng", "geo", "math", "phys",
                                                   "biz", "human", "socialsci", "stats", "med")]
            + [f"ted_mul_{s}" for s in ("agriculture", "water", "energy", "food", "gardening", "health",
                                        "medicine", "sustainability", "all")])

SMALL = """
[[item]]
id = "free_a"
title = "Free A"
category = "reference"
kind = "pdfset"
size_gb = 1.0
tiers = ["base"]
priority = 1
license = "Public domain"
use = "any"
source = { type = "manual" }

[[item]]
id = "nc_b"
title = "NC B"
category = "learn"
kind = "pdfset"
size_gb = 1.0
tiers = ["base"]
priority = 1
license = "CC BY-NC-SA"
use = "personal"
source = { type = "manual" }

[[item]]
id = "nc_c"
title = "NC C (outside the tier)"
category = "learn"
kind = "pdfset"
size_gb = 1.0
tiers = ["ultimate"]
priority = 1
license = "CC BY-NC-ND"
use = "personal"
source = { type = "manual" }

[[item]]
id = "liar_d"
title = "Says any, licence says NC"
category = "learn"
kind = "pdfset"
size_gb = 1.0
tiers = ["base"]
priority = 1
license = "CC BY-NC 4.0"
use = "any"
source = { type = "manual" }

[[item]]
id = "nouse_e"
title = "No use field"
category = "learn"
kind = "pdfset"
size_gb = 1.0
tiers = ["base"]
priority = 1
license = "CC BY-SA"
source = { type = "manual" }

[[item]]
id = "typo_f"
title = "Misspelt use"
category = "learn"
kind = "pdfset"
size_gb = 1.0
tiers = ["base"]
priority = 1
license = "CC BY"
use = "Any"
source = { type = "manual" }
"""


class CatalogUse(unittest.TestCase):
    """Every item says whether it may ship on a unit that is sold."""

    @classmethod
    def setUpClass(cls):
        import tomllib
        cls.raw = tomllib.loads(CATALOG.read_text())["item"]
        cls.items = {i.id: i for i in upd.load_catalog(CATALOG)}

    def test_every_item_has_a_valid_use(self):
        for i in self.raw:
            self.assertIn("use", i, f"{i['id']}: no `use` field")
            self.assertIn(i["use"], upd.USES, f"{i['id']}: use = {i['use']!r}")

    def test_noncommercial_licences_are_personal(self):
        nc = [i for i in self.raw if upd.licence_is_noncommercial(i["license"])]
        self.assertGreaterEqual(len(nc), 26, "the 26 CC BY-NC-* items (and more) must be recognised")
        for i in nc:
            self.assertEqual(i["use"], "personal", f"{i['id']}: {i['license']} is non-commercial")
            self.assertEqual(self.items[i["id"]].use, "personal")
        for iid in KNOWN_NC:
            self.assertEqual(self.items[iid].use, "personal", iid)

    def test_restrictive_or_unclear_licences_are_personal(self):
        for i in self.raw:
            lic = i["license"].lower()
            if lic.startswith(("per creator", "per channel", "per source")) or "(c)" in lic or "a.d.a.m." in lic:
                self.assertEqual(i["use"], "personal", f"{i['id']}: {i['license']}")

    def test_open_licences_stay_any(self):
        for iid in ("wikipedia_en_all_maxi", "gutenberg_en_all", "ready.gov_en_all", "maps_world_z8",
                    "phet_en_all", "model_qwen3_4b", "apk_kiwixmobile", "theworldfactbook_en_all"):
            self.assertEqual(self.items[iid].use, "any", iid)

    def test_noncommercial_detection(self):
        for s in ("CC BY-NC-SA", "CC BY-NC-ND", "CC BY-NC 4.0", "cc by-nc-sa / per book", "Free, non-commercial",
                  "noncommercial", "Personal use only", "Hesperian terms (free non-commercial)"):
            self.assertTrue(upd.licence_is_noncommercial(s), s)
        for s in ("CC BY-SA", "CC BY", "Public domain", "Apache-2.0", "GPL-3.0", "ODbL", "Inc. terms",
                  "Commercial OK", "", None, 3):
            self.assertFalse(upd.licence_is_noncommercial(s), s)

    def test_loader_fails_safe(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(SMALL)
            got = {i.id: i.use for i in upd.load_catalog(p)}
        self.assertEqual(got["free_a"], "any")
        self.assertEqual(got["nc_b"], "personal")
        self.assertEqual(got["liar_d"], "personal", "an NC licence wins over use = any")
        self.assertEqual(got["nouse_e"], "personal", "missing → personal")
        self.assertEqual(got["typo_f"], "personal", "unknown value → personal")
        self.assertEqual(upd.item_use(["any"], "CC BY"), "personal")
        self.assertEqual(upd.Item(id="x", title="x", category="c", kind="zim", source={}, size_gb=0,
                                  tiers=[], renderer="zim").use, "personal", "the dataclass default fails safe too")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="personal-")).resolve()
        (self.tmp / "catalog.toml").write_text(SMALL)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, provisioning: str | None = "sale", extra: str = "") -> upd.Config:
        dev = f'[device]\nprovisioning = "{provisioning}"\n' if provisioning is not None else ""
        (self.tmp / "prepper.toml").write_text(f"""{dev}
[upd]
data_root = "data"
state_file = "state/upd-state.json"
report_dir = "state/reports"
catalog = "catalog.toml"
personal_file = "portal/personal-content.json"
tier = "base"
languages = ["eng"]
reserve_gb = 0
require_signed_peers = false
{extra}
""")
        return upd.Config.load(self.tmp / "prepper.toml")

    def ids(self, cfg, record=None):
        return sorted(i.id for i in upd.wanted(upd.load_catalog(cfg.catalog), cfg, record))


class Selection(Base):
    def test_provisioning_setting(self):
        self.assertEqual(self.cfg(None).provisioning, "owner", "default: built by its owner, as before")
        self.assertEqual(self.cfg("owner").provisioning, "owner")
        self.assertEqual(self.cfg("sale").provisioning, "sale")
        self.assertEqual(self.cfg("forsale").provisioning, "sale", "a typo fails safe")
        (self.tmp / "prepper.toml").write_text('[device]\nprovisioning = 1\n[upd]\ndata_root = "d"\n')
        self.assertEqual(upd.Config.load(self.tmp / "prepper.toml").provisioning, "sale")
        self.assertEqual(upd.Config.load("/nonexistent").provisioning, "owner")
        self.assertEqual(self.cfg().personal_file, self.tmp / "portal/personal-content.json")

    def test_owner_mode_keeps_everything(self):
        self.assertEqual(self.ids(self.cfg("owner")), ["free_a", "liar_d", "nc_b", "nouse_e", "typo_f"])

    def test_sale_mode_excludes_personal(self):
        cfg = self.cfg("sale")
        self.assertEqual(self.ids(cfg), ["free_a"])
        held = [i.id for i in upd.held_personal(upd.load_catalog(cfg.catalog), cfg)]
        self.assertEqual(sorted(held), ["liar_d", "nc_b", "nouse_e", "typo_f"])

    def test_pins_and_budget_cannot_bring_it_back(self):
        cfg = self.cfg("sale", extra='budget_gb = 100\npins = ["nc_b", "nc_c", "free_a"]')
        self.assertEqual(self.ids(cfg), ["free_a"])
        cfg.tier = "ultimate"
        self.assertEqual(self.ids(cfg), ["free_a"])
        cfg.budget_gb = 0
        self.assertEqual(self.ids(cfg), [])
        cfg.languages = ("eng", "spa", "mul")
        self.assertEqual(self.ids(cfg), [])
        # A forged record naming ids that aren't offered does nothing beyond those ids.
        self.assertEqual(self.ids(cfg, {"accepted": {"items": [{"id": "nc_c"}]}}), ["nc_c"])

    def test_budget_not_spent_on_excluded_items(self):
        cfg = self.cfg("sale", extra='budget_gb = 1.5\npins = ["nc_b"]')
        self.assertEqual(self.ids(cfg), ["free_a"], "the pinned NC item doesn't eat the budget")

    def test_acceptance_admits_only_accepted_ids(self):
        cfg = self.cfg("sale")
        upd.record_personal_decision(cfg, "accept", items=["nc_b"], by="Pat", ip="10.10.10.60")
        self.assertEqual(self.ids(cfg), ["free_a", "nc_b"])
        rec = upd.load_personal_record(cfg)
        self.assertEqual(rec["accepted"]["by"], "Pat")
        self.assertEqual(rec["accepted"]["ip"], "10.10.10.60")
        self.assertEqual(rec["accepted"]["licences"], ["CC BY-NC-SA"])
        self.assertEqual(rec["accepted"]["notice"], upd.PERSONAL_NOTICE)
        self.assertTrue(rec["accepted"]["at"])
        # Accepting the rest adds to what was accepted.
        upd.record_personal_decision(cfg, "accept")
        self.assertEqual(self.ids(cfg), ["free_a", "liar_d", "nc_b", "nouse_e", "typo_f"])
        self.assertEqual(len([h for h in upd.load_personal_record(cfg)["history"] if h["event"] == "accepted"]), 2)
        with self.assertRaises(ValueError):
            upd.record_personal_decision(cfg, "accept")              # nothing left to accept

    def test_bad_decisions_refused(self):
        cfg = self.cfg("sale")
        for items in (["nc_c"], ["free_a"], ["nope"], ["../x"]):
            with self.assertRaises(ValueError, msg=items):
                upd.record_personal_decision(cfg, "accept", items=items)
        for items in ("nc_b", [1], [None], {"nc_b": 1}, ["x"] * 1001):
            with self.assertRaises(ValueError, msg=repr(items)[:40]):
                upd.record_personal_decision(cfg, "accept", items=items)
        with self.assertRaises(ValueError):
            upd.record_personal_decision(cfg, "delete-everything")
        with self.assertRaises(ValueError):
            upd.record_personal_decision(cfg, "accept", items=[])
        self.assertFalse(cfg.personal_file.exists(), "nothing written for a refused request")
        self.assertEqual(self.ids(self.cfg("owner")), ["free_a", "liar_d", "nc_b", "nouse_e", "typo_f"])

    def test_name_is_cleaned(self):
        cfg = self.cfg("sale")
        upd.record_personal_decision(cfg, "accept", by="Ann\x1b[31m\n" + "x" * 200)
        self.assertEqual(upd.load_personal_record(cfg)["accepted"]["by"], "Ann[31m" + "x" * 57)

    def test_later_then_offer_is_a_notice(self):
        cfg = self.cfg("sale")
        self.assertEqual(upd.personal_offer(cfg)["offer"], "first-run")
        upd.record_personal_decision(cfg, "later")
        o = upd.personal_offer(cfg)
        self.assertEqual(o["offer"], "notice")
        self.assertEqual(self.ids(cfg), ["free_a"], "not now = still nothing downloaded")
        self.assertTrue(o["sale_ready"])

    def test_offer_contents(self):
        cfg = self.cfg("sale")
        o = upd.personal_offer(cfg)
        self.assertEqual(o["provisioning"], "sale")
        self.assertEqual(sorted(o["pending"]), ["liar_d", "nc_b", "nouse_e", "typo_f"])
        self.assertNotIn("nc_c", [r["id"] for r in o["items"]], "outside the tier: not offered")
        row = [r for r in o["items"] if r["id"] == "nc_b"][0]
        self.assertEqual((row["license"], row["size_gb"], row["manual"], row["allowed"]), ("CC BY-NC-SA", 1.0, True, False))
        self.assertIsNone(upd.personal_offer(self.cfg("owner"))["offer"], "owners get these as today")
        self.assertFalse(upd.personal_offer(self.cfg("owner"))["sale_ready"])

    def test_withdraw_excludes_even_in_owner_mode(self):
        cfg = self.cfg("owner")
        upd.record_personal_decision(cfg, "withdraw")
        self.assertEqual(self.ids(cfg), ["free_a"])
        self.assertEqual(upd.personal_offer(cfg)["offer"], "notice")
        upd.record_personal_decision(cfg, "accept")
        self.assertEqual(self.ids(cfg), ["free_a", "liar_d", "nc_b", "nouse_e", "typo_f"])
        self.assertNotIn("withdrawn_at", upd.load_personal_record(cfg))

    def test_damaged_records_accept_nothing(self):
        cfg = self.cfg("sale")
        cfg.personal_file.parent.mkdir(parents=True)
        bad = ["{", "[]", '"x"', '{"accepted": {"items": "nc_b"}}', '{"accepted": {"items": [{"id": 5}]}}',
               '{"accepted": {"items": [{"id": "../../etc"}]}}', '{"accepted": [1]}', "[" * 100000,
               json.dumps({"accepted": {"items": [{"id": "nc_b"}]}, "pad": "x" * (300 << 10)})]
        for text in bad:
            cfg.personal_file.write_text(text)
            self.assertEqual(self.ids(cfg), ["free_a"], text[:50])
        # A symlink is never followed (someone pointing it at another file).
        real = self.tmp / "elsewhere.json"
        real.write_text(json.dumps({"accepted": {"items": [{"id": "nc_b"}]}}))
        cfg.personal_file.unlink()
        cfg.personal_file.symlink_to(real)
        self.assertEqual(self.ids(cfg), ["free_a"])
        cfg.personal_file.unlink()
        shutil.copy(real, cfg.personal_file)
        self.assertEqual(self.ids(cfg), ["free_a", "nc_b"])

    def test_concurrent_accepts(self):
        cfg = self.cfg("sale")
        errs = []

        def go(iid):
            try:
                upd.record_personal_decision(cfg, "accept", items=[iid])
            except ValueError as e:
                errs.append(e)
        ts = [threading.Thread(target=go, args=(i,)) for i in ("nc_b", "liar_d", "nouse_e", "typo_f")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(self.ids(cfg), ["free_a", "liar_d", "nc_b", "nouse_e", "typo_f"])
        json.loads(cfg.personal_file.read_text())


class InstallPaths(Base):
    """Every way content arrives obeys the rule: USB bundles/peers, adopt, apply."""

    def bundle(self) -> Path:
        b = self.tmp / "usb" / "bundle"
        items = []
        for iid in ("free_a", "nc_b"):
            d = b / "files" / "docs" / iid
            d.mkdir(parents=True)
            (d / "x.pdf").write_text(iid)
            items.append({"id": iid, "version": "2999-01-01", "filename": iid, "path": f"files/docs/{iid}",
                          "comparable": True, "tree_sha256": upd.tree_sha256(d)})
        (b / "manifest.json").write_text(json.dumps({"disk_id": "owner-badger", "generated": upd.now_iso(),
                                                     "items": items}))
        return b

    def import_bundle(self, cfg):
        offers = upd.load_offer(str(self.bundle()), cfg, self.tmp)
        self.assertIn("nc_b", offers, "the bundle does carry it")
        plan = upd.build_plan(cfg, offline=True, offers=offers)
        return {p.item.id for p in plan}, upd.apply_plan(cfg, plan, dry_run=False)

    def test_usb_bundle_needs_acceptance_on_a_sale_unit(self):
        cfg = self.cfg("sale")
        planned, rc = self.import_bundle(cfg)
        self.assertNotIn("nc_b", planned)
        self.assertEqual(rc, 0)
        st = upd.State(cfg.state_file)
        self.assertIsNotNone(st.get("free_a"))
        self.assertIsNone(st.get("nc_b"))
        self.assertFalse((cfg.data_root / "docs" / "nc_b").exists())
        # --only can't ask for it either
        offers = upd.load_offer(str(self.tmp / "usb" / "bundle"), cfg, self.tmp)
        self.assertEqual(upd.build_plan(cfg, offline=True, offers=offers, only={"nc_b"}), [])
        # After the owner accepts, the same bundle brings it in.
        upd.record_personal_decision(cfg, "accept", items=["nc_b"])
        shutil.rmtree(self.tmp / "usb")
        planned, rc = self.import_bundle(cfg)
        self.assertIn("nc_b", planned)
        self.assertIsNotNone(upd.State(cfg.state_file).get("nc_b"))

    def test_owner_unit_imports_everything(self):
        cfg = self.cfg("owner")
        planned, rc = self.import_bundle(cfg)
        self.assertIn("nc_b", planned)

    def test_adopt_refused_on_sale_unit(self):
        cfg = self.cfg("sale")
        d = cfg.data_root / "docs" / "nc_b"
        d.mkdir(parents=True)
        (d / "a.pdf").write_text("x")
        with self.assertRaises(SystemExit) as e:
            upd.cmd_adopt(cfg, types.SimpleNamespace(id="nc_b", path=None))
        self.assertIn("personal use", str(e.exception))
        self.assertIsNone(upd.State(cfg.state_file).get("nc_b"))
        upd.record_personal_decision(cfg, "accept", items=["nc_b"])
        upd.cmd_adopt(cfg, types.SimpleNamespace(id="nc_b", path=None))
        self.assertIsNotNone(upd.State(cfg.state_file).get("nc_b"))

    def test_report_lists_held_items(self):
        cfg = self.cfg("sale")
        path = upd.write_report(cfg, upd.build_plan(cfg, offline=True))
        rep = json.loads(path.read_text())
        self.assertEqual(rep["provisioning"], "sale")
        self.assertEqual(sorted(rep["personal_held"]), ["liar_d", "nc_b", "nouse_e", "typo_f"])

    def test_withdraw_removes_files_once(self):
        cfg = self.cfg("owner")
        self.import_bundle(cfg)
        nc = cfg.data_root / "docs" / "nc_b"
        self.assertTrue(nc.is_dir())
        outside = self.tmp / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        st = upd.State(cfg.state_file)
        # A ledger entry pointing out of data_root, or a symlink to outside, removes nothing outside.
        st.data["items"]["liar_d"] = {"version": "1", "path": "../precious", "filename": "precious"}
        (cfg.data_root / "docs" / "typo_f").symlink_to(outside)
        st.data["items"]["typo_f"] = {"version": "1", "path": "docs/typo_f", "filename": "typo_f"}
        st.save()
        upd.record_personal_decision(cfg, "withdraw")
        rec = upd.load_personal_record(cfg)
        args = types.SimpleNamespace(now=True, offline=True, only=None, dry_run=False)
        upd.cmd_apply(cfg, args)
        st = upd.State(cfg.state_file)
        self.assertFalse(nc.exists(), "personal item deleted")
        self.assertIsNone(st.get("nc_b"))
        self.assertIsNotNone(st.get("free_a"), "open-licence items untouched")
        self.assertTrue((cfg.data_root / "docs" / "free_a").is_dir())
        self.assertEqual((outside / "keep.txt").read_text(), "keep", "nothing outside data_root is touched")
        self.assertFalse((cfg.data_root / "docs" / "typo_f").is_symlink(), "only the link went")
        self.assertEqual(st.data["personal_pruned"], rec["withdrawn_at"])
        self.assertTrue(any("withdrawn" in h["event"] for h in st.data["history"]))
        # Once per removal: a later re-accept + reinstall is not deleted again by the old request.
        self.assertEqual(upd.prune_withdrawn(cfg, st, upd.load_catalog(cfg.catalog), rec), [])

    def test_sale_mode_alone_never_deletes(self):
        cfg = self.cfg("owner")
        self.import_bundle(cfg)
        cfg = self.cfg("sale")                                # e.g. a typo in [device] provisioning
        upd.cmd_apply(cfg, types.SimpleNamespace(now=True, offline=True, only=None, dry_run=False))
        self.assertTrue((cfg.data_root / "docs" / "nc_b").is_dir(), "only the owner's removal deletes content")
        self.assertEqual(upd.personal_offer(cfg)["installed"], ["nc_b"])
        self.assertFalse(upd.personal_offer(cfg)["sale_ready"])


class PortalAPI(unittest.TestCase):
    """GET /api/content/personal (everyone, read-only) and POST /api/admin/personal (admin)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="personal-portal-")).resolve()
        (cls.tmp / "data").mkdir()
        st = cls.tmp / "state"
        (st / "portal").mkdir(parents=True)
        cls.cfgfile = cls.tmp / "prepper.toml"
        cls.cfgfile.write_text(f"""
[device]
provisioning = "sale"

[upd]
data_root = "{cls.tmp / 'data'}"
catalog = "{CATALOG}"
state_file = "{st}/upd-state.json"
report_dir = "{st}/reports"
personal_file = "{st}/portal/personal-content.json"
tier = "field"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
settings_file = "{st}/portal/portal-settings.json"
admin_pin = "{server.hash_pin('246810')}"
board_file = "{st}/portal/board.jsonl"
mesh_outbox = "{st}/portal/mesh-outbox.jsonl"
power_file = "{st}/power.json"
ctl = "/nonexistent/prepperctl"
services = {{}}
""")
        server.CFG = server.PortalConfig(cls.cfgfile)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        server.reset_login_limits()
        cls.token = cls.post("/api/admin/login", {"pin": "246810"})[1]["token"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        server.CFG.upd.personal_file.unlink(missing_ok=True)
        (self.tmp / "state" / "power.json").unlink(missing_ok=True)
        server._cache.clear()
        self.calls = []
        self._ctl = server.ctl
        server.ctl = lambda *a, **k: (self.calls.append(a), (0, "started"))[1]

    def tearDown(self):
        server.ctl = self._ctl

    @classmethod
    def req(cls, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data)

    @classmethod
    def post(cls, path, obj, admin=False):
        h = {"Content-Type": "application/json"}
        if admin:
            h["X-Admin-Token"] = cls.token
        return cls.req("POST", path, json.dumps(obj), h)

    def get(self, admin=False):
        return self.req("GET", "/api/content/personal", headers={"X-Admin-Token": self.token} if admin else {})

    def test_read_only_view_for_everyone(self):
        st, d = self.get()
        self.assertEqual(st, 200)
        self.assertEqual(d["provisioning"], "sale")
        self.assertEqual(d["offer"], "first-run")
        self.assertFalse(d["admin"])
        self.assertIn("ifixit_en_all", d["pending"])
        self.assertIn("ted_mul_water", d["pending"])
        self.assertNotIn("khanacademy_en_all", d["pending"], "ultimate-only: not offered on a field Badger")
        self.assertTrue(d["sale_ready"])
        row = [r for r in d["items"] if r["id"] == "ifixit_en_all"][0]
        self.assertEqual(row["license"], "CC BY-NC-SA")
        self.assertGreater(row["size_gb"], 1)
        self.assertNotIn("history", d)

    def test_library_badges_and_holds(self):
        st, lib = self.req("GET", "/api/library")
        items = {i["id"]: i for i in lib["items"]}
        self.assertEqual(items["ifixit_en_all"]["use"], "personal")
        self.assertTrue(items["ifixit_en_all"]["held"])
        self.assertFalse(items["ifixit_en_all"]["installed"])
        self.assertEqual(items["wikipedia_en_all_maxi"]["use"], "any")
        self.assertFalse(items["wikipedia_en_all_maxi"]["held"])

    def test_accepting_needs_admin_and_agreement(self):
        self.assertEqual(self.post("/api/admin/personal", {"action": "accept", "agree": True})[0], 401)
        self.assertEqual(self.post("/api/admin/personal", {"action": "later"})[0], 401)
        st, d = self.post("/api/admin/personal", {"action": "accept"}, admin=True)
        self.assertEqual(st, 400)
        self.assertIn("agree", d["error"])
        for body in ({"action": "accept", "agree": "yes"}, {"action": "accept", "agree": True, "items": "ifixit_en_all"},
                     {"action": "accept", "agree": True, "items": [{"id": 1}]},
                     {"action": "accept", "agree": True, "items": ["khanacademy_en_all"]},
                     {"action": "accept", "agree": True, "items": ["wikipedia_en_all_maxi"]},
                     {"action": "accept", "agree": True, "name": "x" * 65},
                     {"action": "accept", "agree": True, "items": ["a" * 200]},
                     {"action": "nuke"}, {"action": ["accept"]}, {}):
            st, d = self.post("/api/admin/personal", body, admin=True)
            self.assertEqual(st, 400, body)
        self.assertFalse(server.CFG.upd.personal_file.exists())
        self.assertEqual(self.calls, [])

    def test_accept_records_and_queues_download(self):
        st, d = self.post("/api/admin/personal", {"action": "accept", "agree": True, "name": "Robin",
                                                  "items": ["ifixit_en_all", "ted_mul_water"]}, admin=True)
        self.assertEqual(st, 200, d)
        self.assertIn("Downloading now", d["message"])
        self.assertEqual(self.calls, [("update-apply",)])
        rec = json.loads(server.CFG.upd.personal_file.read_text())
        self.assertEqual(rec["accepted"]["by"], "Robin")
        self.assertEqual(rec["accepted"]["ip"], "127.0.0.1")
        self.assertEqual(sorted(rec["accepted"]["licences"]), ["CC BY-NC-ND", "CC BY-NC-SA"])
        self.assertEqual({x["id"] for x in rec["accepted"]["items"]}, {"ifixit_en_all", "ted_mul_water"})
        # Everyone sees that it was accepted, but not who or from where.
        st, pub = self.get()
        self.assertNotIn("by", pub["accepted"])
        self.assertNotIn("ip", pub["accepted"])
        self.assertEqual(sorted(pub["accepted"]["items"]), ["ifixit_en_all", "ted_mul_water"])
        self.assertFalse(pub["sale_ready"])
        self.assertEqual(pub["offer"], "notice", "more items are still on offer")
        st, adm = self.get(admin=True)
        self.assertEqual(adm["accepted"]["by"], "Robin")
        self.assertEqual(adm["history"][-1]["event"], "accepted")
        st, lib = self.req("GET", "/api/library")
        items = {i["id"]: i for i in lib["items"]}
        self.assertFalse(items["ifixit_en_all"]["held"])
        self.assertTrue(items["ted_mul_food"]["held"])
        # The updater now wants it.
        self.assertIn("ifixit_en_all", {i.id for i in upd.wanted(upd.load_catalog(CATALOG), server.CFG.upd)})

    def test_endurance_or_battery_defers_the_download(self):
        for power in ({"mode": "endurance", "external": True}, {"mode": "balanced", "external": False}):
            server.CFG.upd.personal_file.unlink(missing_ok=True)
            (self.tmp / "state" / "power.json").write_text(json.dumps(power))
            st, d = self.post("/api/admin/personal", {"action": "accept", "agree": True}, admin=True)
            self.assertEqual(st, 200)
            self.assertIn("Not downloading now", d["message"])
        self.assertEqual(self.calls, [])

    def test_not_now_and_remove(self):
        st, d = self.post("/api/admin/personal", {"action": "later"}, admin=True)
        self.assertEqual(st, 200)
        self.assertEqual(d["personal"]["offer"], "notice")
        self.assertEqual(self.calls, [])
        self.post("/api/admin/personal", {"action": "accept", "agree": True}, admin=True)
        st, d = self.post("/api/admin/personal", {"action": "remove"}, admin=True)
        self.assertEqual(st, 200)
        self.assertTrue(d["personal"]["withdrawn_at"])
        self.assertIsNone(d["personal"]["accepted"])
        self.assertEqual(self.calls[-1], ("update-apply",), "the updater deletes them")
        self.assertNotIn("ifixit_en_all", {i.id for i in upd.wanted(upd.load_catalog(CATALOG), server.CFG.upd)})

    def test_import_mentions_held_items(self):
        usb = self.tmp / "media" / "usb0" / "bundle"
        usb.mkdir(parents=True)
        old = server.usb_path
        server.usb_path = lambda v: usb if v == str(usb) else None      # (no real USB mount in a test)
        try:
            st, d = self.post("/api/admin/updates", {"action": "import", "src": str(usb)}, admin=True)
        finally:
            server.usb_path = old
        self.assertEqual(st, 200, d)
        self.assertIn("skipped until the owner accepts", d["output"])


BUILD = REPO / "builder/prepper-build"
_loader = importlib.machinery.SourceFileLoader("prepper_build_pc", str(BUILD))
_spec = importlib.util.spec_from_loader("prepper_build_pc", _loader)
pb = importlib.util.module_from_spec(_spec)
_loader.exec_module(pb)


class Builder(unittest.TestCase):
    def args(self, **kw):
        base = dict(tier="base", langs="eng", hostname="badger", ssid="Garden-Shed", passphrase="correct horse", country="US",
                    timezone="America/Denver", pin=None, uplink=None, relay=None, peer=None, trust_key=None,
                    ssh_key=None, ssh_password=None, mode="auto", for_sale=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_for_sale_sets_provisioning(self):
        import tomllib
        self.assertEqual(tomllib.loads(pb.render_config(self.args()))["device"]["provisioning"], "owner")
        conf = pb.render_config(self.args(for_sale=True))
        self.assertEqual(tomllib.loads(conf)["device"]["provisioning"], "sale")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "prepper.toml"
            p.write_text(conf)
            cfg = upd.Config.load(p)
        self.assertEqual(cfg.provisioning, "sale")
        cfg.personal_file = Path(d) / "none.json"
        ids = {i.id for i in upd.wanted(upd.load_catalog(CATALOG), cfg)}
        self.assertTrue(ids, "a sale unit still gets its open content")
        self.assertFalse({"ifixit_en_all", "ted_mul_water", "hesperian_books"} & ids)
        for i in upd.wanted(upd.load_catalog(CATALOG), cfg):
            self.assertEqual(i.use, "any", i.id)

    def test_for_sale_plan_refuses_personal_pins(self):
        items = upd.load_catalog(CATALOG)
        with self.assertRaises(SystemExit):
            pb.check_sale_pins(items, ["wikipedia_en_all_nopic", "ifixit_en_all"], True)
        pb.check_sale_pins(items, ["ifixit_en_all"], False)
        pb.check_sale_pins(items, ["wikipedia_en_all_nopic"], True)

    def test_plan_cli(self):
        import subprocess
        r = subprocess.run([sys.executable, str(BUILD), "plan", "--tier", "field", "--for-sale"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NOT preloaded", r.stdout)
        self.assertIn("iFixit", r.stdout)
        r = subprocess.run([sys.executable, str(BUILD), "plan", "--tier", "pocket", "--budget-gb", "50",
                            "--for-sale", "--pin-item", "ted_mul_water"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("personal / non-commercial", r.stderr)

    def test_cfg_for_never_reads_a_local_record(self):
        c = pb._cfg_for(self.args(for_sale=True))
        self.assertEqual(c.provisioning, "sale")
        self.assertFalse(c.personal_file.exists())


if __name__ == "__main__":
    unittest.main()
