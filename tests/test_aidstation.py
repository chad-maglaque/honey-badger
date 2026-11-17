"""
Aid-station log (software/portal/features/aidstation.py): volunteer codes and device keys,
access control (staff + admin only), entries, summary, search, follow-ups, CSV export,
retention, the public status line, and adversarial input. Runs through the real portal.
Run: python3.11 tests/test_aidstation.py
"""
from __future__ import annotations

import csv
import http.client
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software" / "portal"))
os.environ["PORTAL_QUIET"] = "1"

import server  # noqa: E402

FEATS = REPO / "software" / "portal" / "features"


class Base(unittest.TestCase):
    EXTRA = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aid-")).resolve()
        (cls.tmp / "data").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        for f in ("_community_common.py", "aidstation.py"):
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

[aidstation]
retention_days = 30
{cls.EXTRA}
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        assert [f["id"] for f in server.FEATURES] == ["aidstation"], server.FEATURES
        cls.mod = server.FEATURES[0]["mod"]
        cls.ctx = server.FEATURES[0]["ctx"]
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]
        cls.data = cls.tmp / "features-data" / "aidstation"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.mod._now = time.time
        self.mod._reset_limits()
        s = self.mod._s(self.ctx)
        s["log"].update(lambda d: (d["entries"].clear(), True))
        s["staff"].revoke_all()
        s["settings"].update(lambda d: (d.pop("retention_days", None), True))
        s["status"].update(lambda d: (d.clear(), True))

    @classmethod
    def req(cls, method, path, body=None, aid=None, admin=None, member=None, headers=None, raw=None, ip=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if aid:
            h["X-Aid-Token"] = aid
        if admin:
            h["X-Admin-Token"] = admin
        if member:
            h["X-Member-Token"] = member
        if ip:
            h["X-Real-IP"] = ip
        h.update(headers or {})
        c.request(method, path, raw if raw is not None else (json.dumps(body) if body is not None else None), h)
        r = c.getresponse()
        data = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        try:
            return r.status, json.loads(data), hdrs
        except ValueError:
            return r.status, data, hdrs

    def code(self, name="Maria"):
        st, j, _ = self.req("POST", "/api/aidstation/admin", {"op": "invite", "name": name}, admin=self.admin)
        self.assertEqual(st, 200, j)
        return j["code"]

    def staff(self, name="Maria", ip="10.10.10.60"):
        st, j, _ = self.req("POST", "/api/aidstation/join", {"code": self.code(name), "device": "phone"}, ip=ip)
        self.assertEqual(st, 200, j)
        return j["key"]

    def add(self, key, **kw):
        body = {"person": "Ana Ruiz", "help": ["water"]}
        body.update(kw)
        return self.req("POST", "/api/aidstation/entry", body, aid=key)


class Access(Base):
    def test_public_sees_only_status(self):
        st, j, _ = self.req("GET", "/api/aidstation/me")
        self.assertEqual(st, 200)
        self.assertIsNone(j["staff"]); self.assertFalse(j["admin"]); self.assertEqual(j["status"], {"set": False})
        for path in ("/api/aidstation/entries", "/api/aidstation/summary", "/api/aidstation/export.csv"):
            self.assertEqual(self.req("GET", path)[0], 401, path)
            self.assertEqual(self.req("GET", path, aid="bogus")[0], 401, path)
        for path in ("/api/aidstation/entry", "/api/aidstation/followup", "/api/aidstation/delete", "/api/aidstation/status"):
            self.assertEqual(self.req("POST", path, {"person": "x"})[0], 401, path)

    def test_family_members_are_not_staff(self):
        inv = self.req("POST", "/api/admin/members", {"op": "invite", "name": "Ana"}, admin=self.admin)[1]
        server._last_post.clear()
        key = self.req("POST", "/api/members/join", {"code": inv["code"], "name": "Ana"})[1]["key"]
        self.assertEqual(self.req("GET", "/api/aidstation/entries", member=key)[0], 401)
        self.assertEqual(self.req("GET", "/api/aidstation/admin", member=key)[0], 401)

    def test_staff_and_admin_can_use_it(self):
        key = self.staff()
        st, j, _ = self.req("GET", "/api/aidstation/me", aid=key)
        self.assertEqual(j["staff"], {"name": "Maria"})
        self.assertEqual(self.req("GET", "/api/aidstation/entries", aid=key)[0], 200)
        self.assertEqual(self.req("GET", "/api/aidstation/entries", admin=self.admin)[0], 200)
        st, j, _ = self.add(None, person="By admin")
        self.assertEqual(st, 401)
        st, j, _ = self.req("POST", "/api/aidstation/entry", {"person": "By admin"}, admin=self.admin)
        self.assertEqual((st, j["entry"]["by"]), (200, "Admin"))

    def test_staff_cannot_use_admin_endpoints(self):
        key = self.staff()
        self.assertEqual(self.req("GET", "/api/aidstation/admin", aid=key)[0], 401)
        self.assertEqual(self.req("POST", "/api/aidstation/admin", {"op": "invite"}, aid=key)[0], 401)
        self.assertEqual(self.req("POST", "/api/aidstation/admin", {"op": "wipe", "confirm": "DELETE"}, aid=key)[0], 401)

    def test_code_single_use_and_expiry(self):
        code = self.code()
        self.assertEqual(self.req("POST", "/api/aidstation/join", {"code": code.lower().replace("-", " ")}, ip="10.0.0.2")[0], 200)
        self.assertEqual(self.req("POST", "/api/aidstation/join", {"code": code}, ip="10.0.0.3")[0], 403)
        code2 = self.code()
        real = time.time
        self.mod._now = lambda: real() + 25 * 3600
        self.assertEqual(self.req("POST", "/api/aidstation/join", {"code": code2}, ip="10.0.0.4")[0], 403)

    def test_wrong_codes_lock_out_that_address(self):
        good = self.code()
        for _ in range(5):
            self.assertEqual(self.req("POST", "/api/aidstation/join", {"code": "AAAA-BBBB-CC"}, ip="10.0.0.9")[0], 403)
        st, j, h = self.req("POST", "/api/aidstation/join", {"code": good}, ip="10.0.0.9")
        self.assertEqual(st, 429); self.assertEqual(h.get("Retry-After"), "600")
        self.assertEqual(self.req("POST", "/api/aidstation/join", {"code": good}, ip="10.0.0.10")[0], 200)

    def test_code_without_name_needs_one(self):
        code = self.code(name="")
        st, j, _ = self.req("POST", "/api/aidstation/join", {"code": code}, ip="10.0.0.20")
        self.assertEqual(st, 400)
        st, j, _ = self.req("POST", "/api/aidstation/join", {"code": code, "name": "Lee"}, ip="10.0.0.20")
        self.assertEqual((st, j["staff"]["name"]), (200, "Lee"))

    def test_join_rejects_weird_types(self):
        for body in ({"code": ["x"]}, {"code": 5}, {"code": None}, {"code": "x" * 5000}, {}):
            self.assertEqual(self.req("POST", "/api/aidstation/join", body, ip=f"10.0.0.{30 + len(str(body))}")[0], 403, body)
        st, j, _ = self.req("POST", "/api/aidstation/join", {"code": self.code(), "device": {"a": 1}, "name": 7}, ip="10.0.0.99")
        self.assertEqual(st, 200)                                 # wrong-typed extras are ignored, not stored
        devs = self.req("GET", "/api/aidstation/admin", admin=self.admin)[1]["staff"]
        self.assertEqual(devs[0]["device"], "device")

    def test_revoke_leave_and_end_of_shift(self):
        a, b, c = self.staff("A", ip="10.0.1.1"), self.staff("B", ip="10.0.1.2"), self.staff("C", ip="10.0.1.3")
        lst = self.req("GET", "/api/aidstation/admin", admin=self.admin)[1]
        self.assertEqual(sorted(s["name"] for s in lst["staff"]), ["A", "B", "C"])
        aid = next(s["id"] for s in lst["staff"] if s["name"] == "A")
        self.assertEqual(self.req("POST", "/api/aidstation/admin", {"op": "revoke", "id": aid}, admin=self.admin)[0], 200)
        self.assertEqual(self.req("GET", "/api/aidstation/entries", aid=a)[0], 401)
        self.assertTrue(self.req("POST", "/api/aidstation/leave", {}, aid=b)[1]["ok"])
        self.assertEqual(self.req("GET", "/api/aidstation/entries", aid=b)[0], 401)
        self.assertEqual(self.req("GET", "/api/aidstation/entries", aid=c)[0], 200)
        self.assertEqual(self.req("POST", "/api/aidstation/admin", {"op": "revoke_all"}, admin=self.admin)[1]["revoked"], 1)
        self.assertEqual(self.req("GET", "/api/aidstation/entries", aid=c)[0], 401)
        st, j, _ = self.req("GET", "/api/aidstation/me", aid=c)
        self.assertTrue(j["token_invalid"])

    def test_staff_cap(self):
        self.ctx.cfg = lambda section=None: {"max_staff": 2}
        try:
            self.code(); self.code()
            st, j, _ = self.req("POST", "/api/aidstation/admin", {"op": "invite", "name": "x"}, admin=self.admin)
            self.assertEqual(st, 400)
        finally:
            self.ctx.cfg = lambda section=None: server.CFG.raw.get(section or "aidstation", {}) or {}

    def test_keys_and_codes_are_stored_hashed_and_private(self):
        code = self.code()
        key = self.req("POST", "/api/aidstation/join", {"code": self.code()}, ip="10.0.2.1")[1]["key"]
        self.add(key)
        raw = (self.data / "staff.json").read_text()
        self.assertNotIn(key, raw)
        self.assertNotIn(code.replace("-", ""), raw)
        for f in ("staff.json", "log.json"):
            self.assertEqual(stat.S_IMODE((self.data / f).stat().st_mode), 0o600, f)

    def test_csrf_rules_still_apply(self):
        key = self.staff()
        st = self.req("POST", "/api/aidstation/entry", aid=key, headers={"Content-Type": "text/plain"},
                      raw=b'{"person":"x"}')[0]
        self.assertEqual(st, 415)
        st = self.req("POST", "/api/aidstation/entry", {"person": "x"}, aid=key, headers={"Origin": "http://evil.example"})[0]
        self.assertEqual(st, 403)
        st = self.req("POST", "/api/aidstation/join", {"code": "x"}, headers={"Sec-Fetch-Site": "cross-site"})[0]
        self.assertEqual(st, 403)

    def test_other_origins_cannot_send_the_aid_header(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("OPTIONS", "/api/aidstation/entries", headers={"Origin": "http://evil.example",
                                                                 "Access-Control-Request-Headers": "x-aid-token"})
        r = c.getresponse(); r.read()
        self.assertNotIn("x-aid-token", (r.getheader("Access-Control-Allow-Headers") or "").lower())
        c.close()


class Log(Base):
    def test_add_list_edit_delete(self):
        key = self.staff()
        st, j, _ = self.add(key, person="José Núñez", count=3, help=["food", "water", "food"], needs="insulin",
                            notes="line 1\nline 2", followup=True, followup_note="check tomorrow")
        self.assertEqual(st, 200, j)
        e = j["entry"]
        self.assertEqual((e["help"], e["count"], e["by"], e["handled_by"]), (["water", "food"], 3, "Maria", "Maria"))
        st, j, _ = self.req("POST", "/api/aidstation/entry", {"id": e["id"], "notes": "changed"}, aid=key)
        self.assertEqual((st, j["entry"]["notes"], j["entry"]["person"]), (200, "changed", "José Núñez"))
        lst = self.req("GET", "/api/aidstation/entries", aid=key)[1]
        self.assertEqual(lst["total"], 1)
        self.assertEqual(self.req("POST", "/api/aidstation/delete", {"id": e["id"]}, aid=key)[0], 200)
        self.assertEqual(self.req("POST", "/api/aidstation/delete", {"id": e["id"]}, aid=key)[0], 404)
        self.assertEqual(self.req("POST", "/api/aidstation/entry", {"id": e["id"], "notes": "x"}, aid=key)[0], 404)

    def test_search_is_accent_and_case_insensitive(self):
        key = self.staff()
        self.add(key, person="José Núñez", needs="Insulin")
        self.add(key, person="Mary Smith", help=["shelter"])
        for q, n in (("jose", 1), ("NUNEZ", 1), ("nún", 1), ("insul", 1), ("shelter", 1), ("smith mary", 1), ("zzz", 0), ("", 2)):
            r = self.req("GET", "/api/aidstation/entries?q=" + urllib.parse.quote(q), aid=key)[1]
            self.assertEqual(r["total"], n, q)

    def test_followups(self):
        key = self.staff()
        e1 = self.add(key, person="A", followup=True, followup_note="dressing")[1]["entry"]
        self.add(key, person="B")
        open_ = self.req("GET", "/api/aidstation/entries?followup=open", aid=key)[1]["entries"]
        self.assertEqual([e["person"] for e in open_], ["A"])
        st, j, _ = self.req("POST", "/api/aidstation/followup", {"id": e1["id"], "done": True}, aid=key)
        self.assertEqual(j["entry"]["followup_done"]["by"], "Maria")
        self.assertEqual(self.req("GET", "/api/aidstation/entries?followup=open", aid=key)[1]["entries"], [])
        b = self.req("GET", "/api/aidstation/entries?q=B", aid=key)[1]["entries"][0]
        self.assertEqual(self.req("POST", "/api/aidstation/followup", {"id": b["id"], "done": True}, aid=key)[0], 400)
        self.assertEqual(self.req("POST", "/api/aidstation/followup", {"id": e1["id"], "done": "yes"}, aid=key)[0], 400)

    def test_summary_counts_since(self):
        key = self.staff()
        now = time.time()
        self.add(key, person="old", help=["food"], at=now - 3 * 86400)
        self.add(key, person="a", help=["water", "first_aid"], count=2)
        self.add(key, person="b", help=["water"], followup=True)
        s = self.req("GET", f"/api/aidstation/summary?since={now - 3600}", aid=key)[1]
        self.assertEqual((s["visits"], s["people"], s["help"]["water"], s["help"]["first_aid"], s["help"]["food"]),
                         (2, 3, 2, 1, 0))
        self.assertEqual((s["followups_open"], s["total_records"]), (1, 3))
        s = self.req("GET", "/api/aidstation/summary?since=nonsense", aid=key)[1]      # falls back to local midnight
        self.assertGreaterEqual(s["visits"], 2)

    def test_validation(self):
        key = self.staff()
        bad = [{"person": ""}, {"person": None}, {"person": ["x"]}, {"person": "x" * 81}, {"person": "x", "count": 0},
               {"person": "x", "count": True}, {"person": "x", "count": "2"}, {"person": "x", "help": ["fire"]},
               {"person": "x", "help": "water"}, {"person": "x", "notes": "n" * 1001}, {"person": "x", "followup": "yes"},
               {"person": "x", "at": time.time() + 3600}, {"person": "x", "at": time.time() - 40 * 86400},
               {"person": "x", "at": "now"}, {"person": "x", "id": "../../etc"}]
        for b in bad:
            self.assertEqual(self.req("POST", "/api/aidstation/entry", b, aid=key)[0], 400 if "id" not in b else 400, b)
        st, j, _ = self.add(key, person="  A‮B\x00C  ", needs="x\r\ny\x07")
        self.assertEqual((j["entry"]["person"], j["entry"]["needs"]), ("ABC", "x\ny"))
        st = self.req("POST", "/api/aidstation/entry", aid=key, raw=b"[1,2]")[0]
        self.assertEqual(st, 400)

    def test_csv_export(self):
        key = self.staff()
        self.add(key, person="=HYPERLINK(\"http://x\")", notes="+cmd|' /C calc'!A0", help=["water", "food"])
        self.add(key, person="Zoë, \"quoted\"")
        st, body, h = self.req("GET", "/api/aidstation/export.csv", aid=key)
        self.assertEqual(st, 200)
        self.assertTrue(h["Content-Type"].startswith("text/csv"))
        self.assertTrue(h["Content-Disposition"].startswith("attachment;"))
        text = body.decode("utf-8")
        self.assertTrue(text.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual(rows[0][0], "time")
        self.assertEqual(len(rows), 3)
        cells = [c for r in rows[1:] for c in r]
        self.assertIn("'=HYPERLINK(\"http://x\")", cells)
        self.assertIn("'+cmd|' /C calc'!A0", cells)
        self.assertIn("Zoë, \"quoted\"", cells)
        self.assertFalse(any(c[:1] in "=+-@" for c in cells if c))
        audit = self.req("GET", "/api/aidstation/admin", admin=self.admin)[1]["audit"]
        self.assertEqual(audit[0]["who"], "Maria"); self.assertIn("exported 2", audit[0]["what"])
        self.assertEqual(self.req("GET", "/api/aidstation/export.csv", admin=self.admin)[0], 200)

    def test_retention_deletes_old_records(self):
        key = self.staff()
        self.add(key, person="old", at=time.time() - 20 * 86400)
        self.add(key, person="new")
        real = time.time
        self.mod._now = lambda: real() + 11 * 86400                # "old" is now 31 days old
        self.assertEqual(self.mod.purge(self.ctx, force=True), 1)
        self.mod._now = time.time
        names = [e["person"] for e in self.req("GET", "/api/aidstation/entries", aid=key)[1]["entries"]]
        self.assertEqual(names, ["new"])
        self.add(key, person="week", at=time.time() - 7 * 86400)
        st, j, _ = self.req("POST", "/api/aidstation/admin", {"op": "retention", "days": 3}, admin=self.admin)
        self.assertEqual((st, j["deleted"]), (200, 1))
        self.assertEqual(self.req("GET", "/api/aidstation/admin", admin=self.admin)[1]["retention_days"], 3)
        for bad in (0, 366, "7", True, None):
            self.assertEqual(self.req("POST", "/api/aidstation/admin", {"op": "retention", "days": bad}, admin=self.admin)[0], 400)

    def test_wipe_needs_confirmation(self):
        key = self.staff()
        self.add(key); self.add(key)
        self.assertEqual(self.req("POST", "/api/aidstation/admin", {"op": "wipe"}, admin=self.admin)[0], 400)
        st, j, _ = self.req("POST", "/api/aidstation/admin", {"op": "wipe", "confirm": "DELETE"}, admin=self.admin)
        self.assertEqual((st, j["deleted"]), (200, 2))
        self.assertEqual(self.req("GET", "/api/aidstation/entries", aid=key)[1]["total"], 0)

    def test_write_rate_limit_per_volunteer(self):
        key, other = self.staff("A", ip="10.0.3.1"), self.staff("B", ip="10.0.3.2")
        self.mod._write_limit.count = 3
        try:
            for _ in range(3):
                self.assertEqual(self.add(key)[0], 200)
            self.assertEqual(self.add(key)[0], 429)
            self.assertEqual(self.add(other)[0], 200)
        finally:
            self.mod._write_limit.count = 120

    def test_log_cap(self):
        key = self.staff()
        self.ctx.cfg = lambda section=None: {"max_entries": 100}
        try:
            s = self.mod._s(self.ctx)
            s["log"].update(lambda d: (d["entries"].extend({"id": f"x{i:09d}", "at": time.time(), "person": "p"} for i in range(100)), True))
            self.assertEqual(self.add(key)[0], 507)
        finally:
            self.ctx.cfg = lambda section=None: server.CFG.raw.get(section or "aidstation", {}) or {}

    def test_concurrent_adds_all_land(self):
        key = self.staff()
        errs = []

        def go(i):
            st = self.add(key, person=f"p{i}")[0]
            if st != 200:
                errs.append(st)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(25)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(self.req("GET", "/api/aidstation/entries", aid=key)[1]["total"], 25)
        json.loads((self.data / "log.json").read_text())           # still valid JSON

    def test_damaged_files_do_not_break_it(self):
        key = self.staff()
        (self.data / "log.json").write_text("{not json")
        self.assertEqual(self.req("GET", "/api/aidstation/entries", aid=key)[1]["total"], 0)
        (self.data / "log.json").write_text('{"entries": {"a": 1}}')
        self.assertEqual(self.add(key)[0], 200)
        (self.data / "log.json").write_text('{"entries": [1, "x", null, {"at": "bad"}]}')
        self.assertEqual(self.req("GET", "/api/aidstation/summary", aid=key)[0], 200)
        self.assertEqual(self.req("GET", "/api/aidstation/export.csv", aid=key)[0], 200)


class Status(Base):
    def test_status_line(self):
        key = self.staff()
        self.assertEqual(self.req("POST", "/api/aidstation/status", {"open": True}, member="x")[0], 401)
        st, j, _ = self.req("POST", "/api/aidstation/status", {"open": True, "items": ["water", "charging"],
                                                              "text": "Church hall"}, aid=key)
        self.assertEqual(st, 200, j)
        pub = self.req("GET", "/api/aidstation/status")[1]
        self.assertEqual((pub["open"], pub["items"], pub["text"], pub["stale"]), (True, ["water", "charging"], "Church hall", False))
        self.assertNotIn("by", pub)                               # anonymised: no volunteer names
        self.assertNotIn("Maria", json.dumps(self.req("GET", "/api/aidstation/me")[1]))
        real = time.time
        self.mod._now = lambda: real() + 13 * 3600
        self.assertTrue(self.req("GET", "/api/aidstation/status")[1]["stale"])

    def test_status_validation(self):
        key = self.staff()
        for b in ({"open": "yes"}, {"open": True, "items": ["guns"]}, {"open": True, "items": "water"},
                  {"open": True, "text": "x" * 101}):
            self.assertEqual(self.req("POST", "/api/aidstation/status", b, aid=key)[0], 400, b)


class Units(unittest.TestCase):
    def test_norm_code(self):
        sys.path.insert(0, str(FEATS))
        import importlib.util
        spec = importlib.util.spec_from_file_location("aid_unit", FEATS / "aidstation.py")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        self.assertEqual(m.norm_code(" k7qx-m2pd_9z "), "K7QXM2PD9Z")
        self.assertEqual(m.norm_code(None), "")
        self.assertEqual(len(m.norm_code("A" * 100)), 32)


if __name__ == "__main__":
    unittest.main(verbosity=1)
