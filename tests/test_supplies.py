"""
Supplies & rationing tracker: validation, days-of-supply math, expiry/low flags, rationing,
CSV export/import, family-board notices (once, batched, weekly digest, optional mesh), and the
HTTP API through the real portal (access, bad input, concurrency).
Run: python3.11 tests/test_supplies.py
"""
from __future__ import annotations

import datetime as dt
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FEATS = REPO / "software" / "portal" / "features"
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "software" / "common"))
sys.path.insert(0, str(REPO / "software" / "upd"))
os.environ["PORTAL_QUIET"] = "1"

import server  # noqa: E402
import upd  # noqa: E402

spec = importlib.util.spec_from_file_location("test_supplies_feature", FEATS / "supplies.py")
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

DAY = 86400
T0 = dt.datetime(2026, 11, 17, 12, 0).timestamp()     # local noon


def item(**kw):
    base = {"name": "x", "category": "other", "qty": 1.0, "unit": "", "location": "", "expiry": "", "min": 0.0,
            "kcal": 0.0, "litres": 0.0, "hours": 0.0, "note": ""}
    base.update(kw)
    return base


class Validation(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(sp.num("2,5"), 2.5)
        self.assertEqual(sp.num("1,500"), 1500.0)
        self.assertEqual(sp.num("12,000.5"), 12000.5)
        self.assertEqual(sp.num(" 3 "), 3.0)
        self.assertEqual(sp.num(""), 0.0)
        self.assertEqual(sp.num(1 / 3), 0.333)
        for bad in ("abc", "-1", "1e5", "nan", "inf", True, None, [1], float("nan"), float("inf"), -0.5, 1e12, "1.2.3", "."):
            with self.assertRaises(sp.Bad, msg=repr(bad)):
                sp.num(bad)

    def test_dates(self):
        self.assertEqual(sp.parse_date("2027-3-5"), "2027-03-05")
        self.assertEqual(sp.parse_date("2027/03/05"), "2027-03-05")
        self.assertEqual(sp.parse_date("2027-02"), "2027-02-28")          # month only: end of month
        self.assertEqual(sp.parse_date("2028-02"), "2028-02-29")
        self.assertEqual(sp.parse_date("2027-12"), "2027-12-31")
        self.assertEqual(sp.parse_date(""), "")
        for bad in ("2027-02-30", "1800-01-01", "tomorrow", "03/05/2027", "2027-13-01", "2027-03-05T00:00"):
            with self.assertRaises(sp.Bad, msg=bad):
                sp.parse_date(bad)

    def test_clean_item(self):
        it = sp.clean_item({"name": " Rice\n\x00 ", "category": "MEDS", "qty": "4", "unit": "kg" * 20, "kcal": 3600})
        self.assertEqual(it["name"], "Rice")
        self.assertEqual(it["category"], "medical")
        self.assertEqual(len(it["unit"]), 16)
        self.assertEqual(sp.clean_item({"name": "a", "category": "spaceships"})["category"], "other")
        with self.assertRaises(sp.Bad):
            sp.clean_item({"name": "  "})
        with self.assertRaises(sp.Bad):
            sp.clean_item("not a dict")
        edited = sp.clean_item({"qty": 9}, it)                 # edits keep the other fields
        self.assertEqual((edited["name"], edited["qty"], edited["kcal"]), ("Rice", 9.0, 3600.0))
        self.assertEqual(len(sp.clean_item({"name": "n" * 500, "note": "z" * 999})["note"]), 200)

    def test_litres_from_unit(self):
        self.assertAlmostEqual(sp.litres_per_unit(item(unit="Gallons")), 3.785)
        self.assertEqual(sp.litres_per_unit(item(unit="L")), 1.0)
        self.assertEqual(sp.litres_per_unit(item(unit="ml")), 0.001)
        self.assertEqual(sp.litres_per_unit(item(unit="bottles")), 0.0)
        self.assertEqual(sp.litres_per_unit(item(unit="bottles", litres=0.5)), 0.5)


class Summary(unittest.TestCase):
    def data(self, items, **hh):
        h = {k: v[0] for k, v in sp.HOUSEHOLD_KEYS.items()}
        h.update(hh)
        return {"items": {f"id{i:04d}": it for i, it in enumerate(items)}, "household": h}

    def test_days_of_water_and_food(self):
        d = self.data([item(name="Jugs", category="water", qty=10, unit="gal"),
                       item(name="Bottles", category="water", qty=24, unit="bottles", litres=0.5),
                       item(name="Mystery", category="water", qty=5, unit="crates"),
                       item(name="Rice", category="food", qty=5, unit="kg", kcal=3600),
                       item(name="Cans", category="food", qty=20, kcal=400),
                       item(name="Spice", category="food", qty=3)],
                      people=4, pets=1, pet_water_l=1.0)
        s = sp.summarize(d, dt.date(2026, 11, 17))
        self.assertAlmostEqual(s["water"]["litres"], 49.9, places=1)      # 37.85 + 12
        self.assertEqual(s["water"]["per_day"], 16.2)                     # 4 × 3.8 + 1
        self.assertEqual(s["water"]["days"], 3.1)
        self.assertEqual(s["water"]["uncounted"], 1)
        self.assertEqual(s["food"]["kcal"], 26000)
        self.assertEqual(s["food"]["days"], 3.2)                          # 26000 / 8000
        self.assertEqual(s["food"]["uncounted"], 1)
        self.assertEqual(s["water"]["level"], "low")                      # under 14 days

    def test_expired_not_counted_but_shown(self):
        d = self.data([item(name="Old cans", category="food", qty=10, kcal=400, expiry="2026-01-01"),
                       item(name="New cans", category="food", qty=10, kcal=400, expiry="2026-12-01"),
                       item(name="Far", category="food", qty=1, kcal=100, expiry="2030-01-01")], people=2)
        s = sp.summarize(d, dt.date(2026, 11, 17))
        self.assertEqual(s["food"]["kcal"], 4100)
        self.assertEqual(s["food"]["kcal_all"], 8100)
        self.assertEqual(s["food"]["days"], 1.0)
        self.assertEqual(s["food"]["level"], "critical")                  # under 3 days
        flags = {i["name"]: i["flags"] for i in s["items"]}
        self.assertEqual(flags, {"Old cans": ["expired"], "New cans": ["expiring"], "Far": []})
        self.assertEqual(s["counts"]["expired"], 1)
        self.assertEqual({i["name"]: i["days_left"] for i in s["items"]}["New cans"], 14)

    def test_low_and_out(self):
        d = self.data([item(name="AA", category="batteries", qty=2, min=8, hours=5),
                       item(name="Gauze", category="medical", qty=0),
                       item(name="Soap", category="hygiene", qty=8, min=8)])
        s = sp.summarize(d, dt.date(2026, 11, 17))
        flags = {i["name"]: i["flags"] for i in s["items"]}
        self.assertEqual(flags, {"AA": ["low"], "Gauze": ["out"], "Soap": []})
        self.assertEqual(s["batteries"]["hours"], 10)
        self.assertIsNone(s["batteries"]["days"])                         # no daily use set: hours only

    def test_fuel_days_and_no_people(self):
        d = self.data([item(name="Propane", category="fuel", qty=6, hours=2),
                       item(name="Water", category="water", qty=10, unit="L")], fuel_h_day=3, people=0)
        s = sp.summarize(d, dt.date(2026, 11, 17))
        self.assertEqual(s["fuel"]["days"], 4.0)
        self.assertIsNone(s["water"]["days"])                             # 0 people: no division by zero

    def test_ration_days(self):
        self.assertEqual(sp.ration_days(24000, 1200, 4), 5.0)
        self.assertEqual(sp.ration_days(24000, 1200, 4, 1200), 4.0)
        self.assertIsNone(sp.ration_days(100, 0, 4))


class FakeCtx:
    def __init__(self, folder: Path, cfg=None):
        self.folder, self.config = folder, cfg or {}
        self.posts, self.mesh, self.fail_post = [], [], False

    def data_dir(self):
        return self.folder

    def cfg(self):
        return self.config

    def atomic_write(self, p, text):
        upd.atomic_write(p, text)

    def board_post(self, ip, name, text, mesh, board, member):
        if self.fail_post:
            return False, "slow down"
        self.posts.append({"ip": ip, "text": text, "board": board, "member": member, "mesh": mesh})
        return True, "posted"

    def mesh_send(self, text, board="community"):
        self.mesh.append((text, board))
        return True

    def log(self, msg):
        pass


class Notices(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="supplies-")).resolve()
        self.t = [T0]
        self.ctx = FakeCtx(self.dir)
        self.st = sp.Store(self.dir / "supplies.json", self.ctx.config, clock=lambda: self.t[0])

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def put(self, **items):
        with self.st.lock:
            d = self.st.load()
            for iid, it in items.items():
                if it is None:
                    d["items"].pop(iid, None)
                else:
                    d["items"][iid] = item(**it)
            d["household"]["people"] = 2
            self.st.save(d, upd.atomic_write)

    def check(self):
        return sp.check_notify(self.st, self.ctx)

    def test_posts_once_per_crossing(self):
        self.put(water01=dict(name="Water", category="water", qty=100, unit="L"),
                 aa0001=dict(name="AA batteries", category="batteries", qty=10, min=8))
        self.assertIsNone(self.check())                         # nothing wrong: no post
        self.assertEqual(self.ctx.posts, [])
        self.put(aa0001=dict(name="AA batteries", category="batteries", qty=2, min=8, unit="pcs"))
        self.t[0] += 1800
        msg = self.check()
        self.assertIn("running low: AA batteries (2 pcs, want 8)", msg)
        p = self.ctx.posts[-1]
        self.assertEqual((p["board"], p["ip"], p["mesh"]), ("family", "feature:supplies", False))
        self.assertEqual(p["member"]["id"], "feature-supplies")
        for _ in range(10):                                     # still low: not again
            self.t[0] += 3600
            self.assertIsNone(self.check())
        self.assertEqual(len(self.ctx.posts), 1)

    def test_batching_and_hour_gap(self):
        self.put(a00001=dict(name="A", qty=0, min=1))
        self.check()
        self.put(b00001=dict(name="B", qty=0, min=1), c00001=dict(name="C", expiry="2026-11-20"))
        self.t[0] += 600
        self.assertIsNone(self.check())                         # within the hour: held back
        self.t[0] += 3000
        msg = self.check()
        self.assertIn("running low: B", msg)
        self.assertIn("expiring soon: C (2026-11-20)", msg)
        self.assertNotIn(" A ", msg)
        self.assertEqual(len(self.ctx.posts), 2)

    def test_flapping_item_reposted_only_after_three_days(self):
        self.put(a00001=dict(name="Soap", qty=1, min=2))
        self.check()
        self.put(a00001=dict(name="Soap", qty=3, min=2))
        self.t[0] += 3700; self.check()
        self.put(a00001=dict(name="Soap", qty=1, min=2))
        self.t[0] += 3700; self.assertIsNone(self.check())
        self.put(a00001=dict(name="Soap", qty=3, min=2))
        self.t[0] += 3 * DAY; self.check()
        self.put(a00001=dict(name="Soap", qty=1, min=2))
        self.t[0] += 3700
        self.assertIn("Soap", self.check())
        self.assertEqual(len(self.ctx.posts), 2)

    def test_expiring_then_expired(self):
        self.put(m00001=dict(name="Aspirin", category="medical", expiry="2026-11-25"))
        self.assertIn("expiring soon: Aspirin", self.check())
        self.t[0] += 9 * DAY
        msg = self.check()
        self.assertIn("expired: Aspirin (2026-11-25)", msg)

    def test_weekly_digest(self):
        self.put(a00001=dict(name="Rice", category="food", qty=1, min=5, kcal=3600),
                 w00001=dict(name="Water", category="water", qty=100, unit="L"))
        self.check()
        self.t[0] += 6 * DAY
        self.assertIsNone(self.check())
        self.t[0] += DAY + 60
        msg = self.check()
        self.assertTrue(msg.startswith("📦 Weekly supplies check"))
        self.assertIn("Rice", msg)
        self.assertIn("water lasts about 13.2 days for 2 people", msg)
        self.t[0] += 3 * DAY
        self.assertIsNone(self.check())
        self.assertEqual(len(self.ctx.posts), 2)
        self.ctx.config["digest_days"] = 0
        self.t[0] += 30 * DAY
        self.assertIsNone(self.check())

    def test_aggregate_water_and_mesh(self):
        self.put(w00001=dict(name="Water", category="water", qty=10, unit="L"))
        msg = self.check()                                       # 10 L for 2 people = 1.3 days
        self.assertIn("water lasts about 1.3 days", msg)
        self.assertEqual(self.ctx.mesh, [])                      # mesh is off by default
        self.ctx.config["mesh_critical"] = True
        self.put(f00001=dict(name="Cans", category="food", qty=2, kcal=400))
        self.t[0] += 3700
        self.check()
        self.assertEqual(len(self.ctx.mesh), 1)
        text, board = self.ctx.mesh[0]
        self.assertEqual(board, "family")
        self.assertIn("food", text)
        self.assertLessEqual(len(text.encode()), 200)
        self.t[0] += 3700
        self.check()
        self.assertEqual(len(self.ctx.mesh), 1)                  # not repeated

    def test_board_notes_off(self):
        self.ctx.config["board_notes"] = False
        self.put(a00001=dict(name="A", qty=0, min=1))
        self.check()
        self.assertEqual(self.ctx.posts, [])

    def test_failed_post_is_retried(self):
        self.put(a00001=dict(name="A", qty=0, min=1))
        self.ctx.fail_post = True
        self.assertIsNone(self.check())
        self.ctx.fail_post = False
        self.t[0] += 1800
        self.assertIn("A", self.check())

    def test_long_lists_are_cut(self):
        self.put(**{f"i{n:05d}": dict(name=f"Item number {n} " + "z" * 40, qty=0, min=1) for n in range(40)})
        msg = self.check()
        self.assertIn("and 34 more", msg)
        self.assertLessEqual(len(msg), 900)

    def test_deleted_items_forgotten(self):
        self.put(a00001=dict(name="A", qty=0, min=1))
        self.check()
        self.put(a00001=None)
        self.t[0] += 3700
        self.check()
        self.assertEqual(self.st.load()["notify"]["active"], [])


class Csv(unittest.TestCase):
    def test_roundtrip_and_injection_guard(self):
        d = {"items": {"abc123": item(name="=HYPERLINK(\"http://x\")", category="food", qty=3, unit="cans", kcal=400,
                                      expiry="2027-01-01", note="+1 555"),
                       "def456": item(name="Water", category="water", qty=10.5, unit="L")}}
        text = sp.to_csv(d)
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("'+1 555", text)
        self.assertTrue(text.startswith("id,name,category,quantity"))
        d2 = {"items": {}}
        res = sp.from_csv(text.encode(), d2, 100, replace=True)
        self.assertEqual((res["added"], res["updated"], res["errors"]), (2, 0, []))
        names = sorted(i["name"] for i in d2["items"].values())
        self.assertEqual(names, ["=HYPERLINK(\"http://x\")", "Water"])        # guard removed on the way back
        res = sp.from_csv(text.encode(), d, 100)                                # same ids: updates
        self.assertEqual((res["added"], res["updated"]), (0, 2))

    def test_aliases_merge_by_name_and_errors(self):
        d = {"items": {"abc123": item(name="Rice", location="Pantry", qty=1, category="food", kcal=3600)}}
        raw = ("﻿Item,Qty,Type,Where,Best By,Calories,Min\n"
               "Rice,5,food,pantry,2027-06,3600,2\n"
               "Beans,3,Food,Pantry,,3400,\n"
               ",4,food,,,,\n"
               "Bad,lots,food,,,,\n"
               "Bad date,1,water,,2027-02-31,,\n"
               "\n").encode("utf-8")
        res = sp.from_csv(raw, d, 100)
        self.assertEqual((res["added"], res["updated"]), (1, 1))
        self.assertEqual(len(res["errors"]), 3)
        self.assertTrue(res["errors"][0].startswith("row 4"))
        rice = d["items"]["abc123"]
        self.assertEqual((rice["qty"], rice["expiry"], rice["min"]), (5.0, "2027-06-30", 2.0))

    def test_limits_and_garbage(self):
        d = {"items": {}}
        raw = ("name,quantity\n" + "".join(f"item{i},1\n" for i in range(30))).encode()
        res = sp.from_csv(raw, d, 10)
        self.assertEqual(res["added"], 10)
        self.assertIn("at most 10", res["errors"][-1])
        with self.assertRaises(sp.Bad):
            sp.from_csv(b"", {"items": {}}, 10)
        with self.assertRaises(sp.Bad):
            sp.from_csv(b"qty,unit\n1,2\n", {"items": {}}, 10)
        res = sp.from_csv(b"name\n\xff\xfe caf\xe9\n", {"items": {}}, 10)     # not UTF-8: latin-1 fallback
        self.assertEqual(res["added"], 1)


def http_req(port, method, path, body=None, member=None, admin=None, headers=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
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


class SuppliesHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="supplies-http-")).resolve()
        (cls.tmp / "data").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        shutil.copy(FEATS / "supplies.py", feats / "supplies.py")
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

[supplies]
max_items = 50
check_minutes = 1440
people = 3
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = http_req(cls.port, "POST", "/api/admin/login", {"pin": "246810"})[1]["token"]
        db = server.members_db()
        cls.ana = db.join(db.create_invite("Ana")["code"], "Ana", "phone", "t")[2]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, *a, **kw):
        return http_req(self.port, *a, **kw)

    def add(self, **kw):
        st, j, _ = self.req("POST", "/api/supplies/item", kw, member=self.ana)
        self.assertEqual(st, 200, j)
        return j["id"], j["state"]

    def test_access(self):
        for method, path, body in (("GET", "/api/supplies", None), ("POST", "/api/supplies/item", {"name": "x"}),
                                   ("GET", "/api/supplies/export.csv", None), ("POST", "/api/supplies/adjust", {}),
                                   ("GET", "/api/supplies/log", None), ("POST", "/api/supplies/household", {"people": 2})):
            self.assertEqual(self.req(method, path, body)[0], 401, path)
            self.assertEqual(self.req(method, path, body, admin=self.admin)[0], 401, path)
        self.assertEqual(self.req("POST", "/api/supplies/import", raw=b"name\nx\n", admin=self.admin,
                                  headers={"X-Badger-Upload": "1", "Content-Type": "text/csv"})[0], 401)
        st, j, _ = self.req("GET", "/api/supplies", member=self.ana)
        self.assertEqual(st, 200)
        self.assertEqual(j["household"]["people"], 3)          # default from [supplies] people
        self.assertFalse(j["household"]["set"])

    def test_add_use_adjust_edit_delete(self):
        iid, s = self.add(name="Rice", category="food", qty="4", unit="kg", kcal=3600, min=2, location="Pantry")
        st, j, _ = self.req("POST", "/api/supplies/adjust", {"id": iid, "delta": 1.5, "use": True}, member=self.ana)
        self.assertEqual(j["qty"], 2.5)
        st, j, _ = self.req("POST", "/api/supplies/adjust", {"id": iid, "delta": -100}, member=self.ana)
        self.assertEqual(j["qty"], 0.0)                         # never below zero
        st, j, _ = self.req("POST", "/api/supplies/adjust", {"id": iid, "set": "7"}, member=self.ana)
        self.assertEqual(j["qty"], 7.0)
        st, j, _ = self.req("POST", "/api/supplies/item", {"id": iid, "expiry": "2026-01-01"}, member=self.ana)
        it = [i for i in j["state"]["items"] if i["id"] == iid][0]
        self.assertEqual((it["name"], it["qty"], it["flags"]), ("Rice", 7.0, ["expired"]))
        log = self.req("GET", "/api/supplies/log", member=self.ana)[1]["log"]
        self.assertEqual([x["what"] for x in log[:4]], ["edited", "counted", "took", "used"])
        self.assertEqual(log[3]["who"], "Ana")
        self.assertEqual(log[3]["delta"], -1.5)
        self.assertEqual(self.req("POST", "/api/supplies/delete", {"id": iid}, member=self.ana)[0], 200)
        self.assertEqual(self.req("POST", "/api/supplies/delete", {"id": iid}, member=self.ana)[0], 404)
        self.assertEqual(self.req("POST", "/api/supplies/adjust", {"id": iid, "delta": 1}, member=self.ana)[0], 404)

    def test_bad_input(self):
        iid, _ = self.add(name="Soap", category="hygiene", qty=1)
        for body in ({"name": ""}, {"name": "x", "qty": "lots"}, {"name": "x", "qty": -1}, {"name": "x", "qty": 1e300},
                     {"name": "x", "expiry": "soon"}, {"name": "x", "qty": True}, {"name": "x", "kcal": [1]}):
            self.assertEqual(self.req("POST", "/api/supplies/item", body, member=self.ana)[0], 400, body)
        for body in ({"id": iid, "delta": "1"}, {"id": iid, "delta": True}, {"id": iid}, {"id": "../x", "delta": 1},
                     {"id": iid, "delta": 1e12}, {"id": iid, "set": "-3"}, {"id": None, "delta": 1}):
            self.assertEqual(self.req("POST", "/api/supplies/adjust", body, member=self.ana)[0], 400, body)
        st, _, _ = self.req("POST", "/api/supplies/adjust", member=self.ana, raw=b'{"id": "%s", "delta": NaN}' % iid.encode())
        self.assertEqual(st, 400)
        self.assertEqual(self.req("POST", "/api/supplies/item", {"id": "zzzzzzzzzz", "name": "x"}, member=self.ana)[0], 404)
        self.assertEqual(self.req("POST", "/api/supplies/household", {"people": 9999}, member=self.ana)[0], 400)
        self.assertEqual(self.req("POST", "/api/supplies/household", {"kcal": "abc"}, member=self.ana)[0], 400)

    def test_household_changes_days(self):
        self.add(name="Water jugs", category="water", qty=10, unit="gal")
        st, j, _ = self.req("POST", "/api/supplies/household", {"people": 2, "pets": 1, "water_l": 3.8, "pet_water_l": 1}, member=self.ana)
        self.assertEqual(st, 200)
        self.assertTrue(j["state"]["household"]["set"])
        w = j["state"]["water"]
        self.assertEqual(w["per_day"], 8.6)
        self.assertAlmostEqual(w["days"], round(w["litres"] / 8.6, 1))

    def test_csv_export_import(self):
        self.add(name="=cmd|' /C calc'!A0", category="other", qty=1)
        st, body, h = self.req("GET", "/api/supplies/export.csv", member=self.ana)
        self.assertEqual(st, 200)
        self.assertTrue(h["content-type"].startswith("text/csv"))
        self.assertIn("attachment", h["content-disposition"])
        text = body.decode("utf-8-sig")
        self.assertIn("'=cmd", text)
        up = {"X-Badger-Upload": "1", "Content-Type": "text/csv"}
        st, j, _ = self.req("POST", "/api/supplies/import", member=self.ana, headers=up,
                            raw=b"name,category,quantity,unit,kcal_per_unit\nOats,food,2,kg,3800\n")
        self.assertEqual((st, j["added"]), (200, 1))
        self.assertIn("Oats", [i["name"] for i in j["state"]["items"]])
        st, j, _ = self.req("POST", "/api/supplies/import?mode=bogus", member=self.ana, headers=up, raw=b"name\nx\n")
        self.assertEqual(st, 400)
        st, j, _ = self.req("POST", "/api/supplies/import", member=self.ana, headers=up, raw=b"\x00\x01garbage")
        self.assertEqual(st, 400)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/api/supplies/import")
        for k, v in dict(up, **{"X-Member-Token": self.ana, "Content-Length": "1000001"}).items():
            c.putheader(k, v)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 413)          # refused before the body is read
        c.close()
        before = len(self.req("GET", "/api/supplies", member=self.ana)[1]["items"])
        st, j, _ = self.req("POST", "/api/supplies/import?mode=replace", member=self.ana, headers=up, raw=b"name\nOnly one\n")
        self.assertEqual([i["name"] for i in j["state"]["items"]], ["Only one"])
        self.assertGreater(before, 1)
        self.assertEqual(self.req("POST", "/api/supplies/import", member=self.ana, raw=b"name\nx\n",
                                  headers={"Content-Type": "text/csv"})[0], 415)

    def test_item_cap(self):
        st, j, _ = self.req("POST", "/api/supplies/import?mode=replace", member=self.ana,
                            headers={"X-Badger-Upload": "1", "Content-Type": "text/csv"},
                            raw=("name\n" + "".join(f"i{n}\n" for n in range(50))).encode())
        self.assertEqual(j["added"], 50)
        st, j, _ = self.req("POST", "/api/supplies/item", {"name": "one too many"}, member=self.ana)
        self.assertEqual(st, 400)
        self.req("POST", "/api/supplies/import?mode=replace", member=self.ana,
                 headers={"X-Badger-Upload": "1", "Content-Type": "text/csv"}, raw=b"name\nreset\n")

    def test_concurrent_adjust(self):
        iid, _ = self.add(name="Candles", category="other", qty=0)

        def go():
            self.req("POST", "/api/supplies/adjust", {"id": iid, "delta": 1}, member=self.ana)
        ts = [threading.Thread(target=go) for _ in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        items = self.req("GET", "/api/supplies", member=self.ana)[1]["items"]
        self.assertEqual([i["qty"] for i in items if i["id"] == iid], [20.0])

    def test_board_note_lands_on_family_board(self):
        self.add(name="Batteries D", category="batteries", qty=1, min=6, unit="pcs")
        server._last_post.clear()
        feat = [f for f in server.FEATURES if f["id"] == "supplies"][0]
        msg = feat["mod"].check_notify(feat["mod"]._store(feat["ctx"]), feat["ctx"])
        self.assertIn("Batteries D", msg)
        board = [json.loads(x) for x in (self.tmp / "family-board.jsonl").read_text().splitlines()]
        self.assertEqual(board[-1]["text"], msg)
        self.assertEqual(board[-1]["name"], "📦 Supplies")
        self.assertEqual(self.req("GET", "/api/board?board=community")[1]["messages"], [])   # never on the public board
        st, j, _ = self.req("GET", "/api/board?board=family", member=self.ana)
        self.assertEqual(j["messages"][-1]["text"], msg)

    def test_file_is_private(self):
        self.add(name="x")
        p = self.tmp / "features-data" / "supplies" / "supplies.json"
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=1)
