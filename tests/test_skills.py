"""
Skills & resources directory (software/portal/features/skills.py) and the shared community
helpers (_community_common.py): browse/search, add with edit token, edit/renew/remove, admin
moderation, reports, expiry, rate limits, caps, validation, concurrency. Runs through the real portal.
Run: python3.11 tests/test_skills.py
"""
from __future__ import annotations

import http.client
import importlib.util
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


def load_common():
    spec = importlib.util.spec_from_file_location("community_common_test", FEATS / "_community_common.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="skills-")).resolve()
        (cls.tmp / "data").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        for f in ("_community_common.py", "skills.py"):
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

[skills]
max_entries = 12
per_ip = 4
expire_days = 90
adds_per_10min = 5
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        assert [f["id"] for f in server.FEATURES] == ["skills"], server.FEATURES
        cls.mod = server.FEATURES[0]["mod"]
        cls.ctx = server.FEATURES[0]["ctx"]
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]
        cls.data = cls.tmp / "features-data" / "skills"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.mod._now = time.time
        self.mod._reset_limits()
        self.mod._store(self.ctx).update(lambda d: (d["entries"].clear(), True))

    @classmethod
    def req(cls, method, path, body=None, admin=None, headers=None, raw=None, ip="10.10.10.50"):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = {"Content-Type": "application/json", "X-Real-IP": ip}
        if admin:
            h["X-Admin-Token"] = admin
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

    def add(self, ip="10.10.10.50", **kw):
        body = {"kind": "offer", "category": "medical", "title": "Registered nurse", "name": "Ana",
                "contact": "Blue house, Oak St", "availability": "evenings"}
        body.update(kw)
        return self.req("POST", "/api/skills/add", body, ip=ip)

    def listing(self, qs="", admin=None):
        return self.req("GET", "/api/skills" + qs, admin=admin)[1]["entries"]


class Directory(Base):
    def test_add_list_and_no_secrets_leak(self):
        st, j, _ = self.add(details="ER, 10 years")
        self.assertEqual(st, 200, j)
        self.assertTrue(j["token"])
        e = self.listing()[0]
        self.assertEqual((e["title"], e["name"], e["kind"], e["category"]), ("Registered nurse", "Ana", "offer", "medical"))
        for secret in ("tok", "ipt", "flags", "hidden", "token"):
            self.assertNotIn(secret, e)
        raw = (self.data / "skills.json").read_text()
        self.assertNotIn(j["token"], raw)                        # only a hash is stored
        self.assertNotIn("10.10.10.50", raw)                     # addresses are salted hashes
        self.assertEqual(stat.S_IMODE((self.data / "skills.json").stat().st_mode), 0o600)

    def test_filters_and_accent_insensitive_search(self):
        self.add(title="Enfermera — José Núñez", category="medical")
        self.add(title="Generator 3 kW", category="power", ip="10.0.0.2")
        self.add(kind="need", title="Need insulin fridge", category="medical", ip="10.0.0.3")
        self.assertEqual(len(self.listing()), 3)
        self.assertEqual(len(self.listing("?kind=need")), 1)
        self.assertEqual(len(self.listing("?kind=offer&category=medical")), 1)
        self.assertEqual(len(self.listing("?category=bogus")), 3)
        for q, n in (("jose", 1), ("NÚÑEZ", 1), ("nune", 1), ("generator kw", 1), ("power", 1), ("xyz", 0)):
            self.assertEqual(len(self.listing("?q=" + urllib.parse.quote(q))), n, q)

    def test_edit_renew_remove_with_token_only(self):
        j = self.add()[1]
        eid, tok = j["id"], j["token"]
        self.assertEqual(self.req("POST", "/api/skills/edit", {"id": eid, "title": "x"})[0], 403)
        self.assertEqual(self.req("POST", "/api/skills/edit", {"id": eid, "token": "wrong", "title": "x"})[0], 403)
        self.assertEqual(self.req("POST", "/api/skills/edit", {"id": eid, "token": ["x"], "title": "x"})[0], 403)
        st, j2, _ = self.req("POST", "/api/skills/edit", {"id": eid, "token": tok, "title": "Nurse (ER)", "kind": "need"})
        self.assertEqual((st, j2["entry"]["title"], j2["entry"]["kind"], j2["entry"]["name"]), (200, "Nurse (ER)", "need", "Ana"))
        self.assertEqual(self.req("POST", "/api/skills/edit", {"id": eid, "token": tok, "title": ""})[0], 400)
        self.assertEqual(self.req("POST", "/api/skills/remove", {"id": eid, "token": "nope"})[0], 403)
        self.assertEqual(self.req("POST", "/api/skills/remove", {"id": eid, "token": tok})[0], 200)
        self.assertEqual(self.req("POST", "/api/skills/remove", {"id": eid, "token": tok})[0], 404)
        self.assertEqual(self.listing(), [])

    def test_admin_moderation(self):
        eid = self.add()[1]["id"]
        self.assertEqual(self.req("POST", "/api/skills/moderate", {"id": eid, "op": "hide"})[0], 401)
        self.assertEqual(self.req("POST", "/api/skills/moderate", {"id": eid, "op": "hide"}, admin=self.admin)[0], 200)
        self.assertEqual(self.listing(), [])
        adm = self.listing(admin=self.admin)
        self.assertTrue(adm[0]["hidden"])
        self.assertEqual(self.req("POST", "/api/skills/edit", {"id": eid, "title": "fixed"}, admin=self.admin)[0], 200)
        self.assertEqual(self.req("POST", "/api/skills/moderate", {"id": eid, "op": "unhide"}, admin=self.admin)[0], 200)
        self.assertEqual(self.listing()[0]["title"], "fixed")
        self.assertEqual(self.req("POST", "/api/skills/moderate", {"id": eid, "op": "nuke"}, admin=self.admin)[0], 400)
        self.assertEqual(self.req("POST", "/api/skills/moderate", {"id": eid, "op": "remove"}, admin=self.admin)[0], 200)
        self.assertEqual(self.listing(admin=self.admin), [])

    def test_reports_count_once_per_address(self):
        eid = self.add()[1]["id"]
        for ip in ("10.1.0.1", "10.1.0.1", "10.1.0.2"):
            self.assertEqual(self.req("POST", "/api/skills/flag", {"id": eid}, ip=ip)[0], 200)
        self.assertEqual(self.listing(admin=self.admin)[0]["flags"], 2)
        self.req("POST", "/api/skills/moderate", {"id": eid, "op": "clear_flags"}, admin=self.admin)
        self.assertEqual(self.listing(admin=self.admin)[0]["flags"], 0)
        self.assertEqual(self.req("POST", "/api/skills/flag", {"id": "nosuchid00"})[0], 404)

    def test_expiry_and_renew(self):
        j = self.add()[1]
        self.add(title="Generator", ip="10.0.0.9")
        real = time.time
        self.mod._now = lambda: real() + 60 * 86400
        self.assertEqual(self.req("POST", "/api/skills/edit", {"id": j["id"], "token": j["token"]})[0], 200)   # renew
        self.mod._now = lambda: real() + 100 * 86400
        self.assertEqual([e["title"] for e in self.listing()], ["Registered nurse"])

    def test_validation(self):
        bad = [{"title": ""}, {"title": None}, {"title": "x" * 81}, {"title": 5}, {"kind": "sell"}, {"category": "weapons"},
               {"kind": ["offer"]}, {"contact": "c" * 201}, {"details": "d" * 501}, {"name": {"a": 1}}]
        for b in bad:
            self.assertEqual(self.add(**b)[0], 400, b)
        st, j, _ = self.add(title="  Nurse‮\u0000\x1b[31m  ", details="a\r\nb\n\n\n\nc")
        self.assertEqual(st, 200)
        e = self.listing()[0]
        self.assertEqual((e["title"], e["details"]), ("Nurse[31m", "a\nb\n\nc"))
        self.assertEqual(self.req("POST", "/api/skills/edit", {"id": "../../x", "token": "t"})[0], 404)
        self.assertEqual(self.req("POST", "/api/skills/add", raw=b"not json")[0], 400)

    def test_markup_is_never_returned_raw(self):
        self.add(title="<script>alert(1)</script>", name="<img src=x onerror=alert(1)>")
        st, body, h = self.req("GET", "/api/skills", headers={"Accept": "*/*"})
        raw = json.dumps(body)
        self.assertEqual(st, 200)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/skills"); r = c.getresponse(); data = r.read(); c.close()
        self.assertNotIn(b"<script>", data)                     # JSON escapes < > & (dumps_safe)
        self.assertIn("<script>", raw)                          # …but the text itself is intact

    def test_csrf(self):
        self.assertEqual(self.req("POST", "/api/skills/add", headers={"Content-Type": "text/plain"},
                                  raw=b'{"title":"x"}')[0], 415)
        self.assertEqual(self.req("POST", "/api/skills/add", {"title": "x"}, headers={"Origin": "http://evil.example"})[0], 403)


class Limits(Base):
    def test_rate_limit_per_address(self):
        cnt = 0
        for i in range(5):
            st, j, _ = self.add(title=f"t{i}", ip="10.2.0.1")
            if st == 200:
                cnt += 1
                self.req("POST", "/api/skills/remove", {"id": j["id"], "token": j["token"]}, ip="10.2.0.1")
        self.assertEqual(cnt, 5)
        st, j, _ = self.add(ip="10.2.0.1")
        self.assertEqual(st, 429); self.assertIn("Try again", j["error"])
        self.assertEqual(self.add(ip="10.2.0.2")[0], 200)

    def test_entries_per_address(self):
        for i in range(4):
            self.assertEqual(self.add(title=f"t{i}", ip="10.3.0.1")[0], 200)
        st, j, _ = self.add(ip="10.3.0.1")
        self.assertEqual(st, 429); self.assertIn("At most 4", j["error"])

    def test_total_cap(self):
        for i in range(12):
            self.assertEqual(self.add(title=f"t{i}", ip=f"10.4.0.{i}")[0], 200)
        self.assertEqual(self.add(ip="10.4.1.1")[0], 507)

    def test_concurrent_adds_respect_caps(self):
        res = []

        def go(i):
            res.append(self.add(title=f"t{i}", ip="10.5.0.1" if i % 2 else f"10.5.1.{i}")[0])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(30)]
        [t.start() for t in ts]; [t.join() for t in ts]
        entries = self.listing()
        self.assertLessEqual(len(entries), 12)
        d = json.loads((self.data / "skills.json").read_text())
        per = {}
        for e in d["entries"]:
            per[e["ipt"]] = per.get(e["ipt"], 0) + 1
        self.assertLessEqual(max(per.values()), 4)
        self.assertEqual(res.count(200), len(entries))


class Common(unittest.TestCase):
    C = load_common()

    def test_fold_and_match(self):
        C = self.C
        self.assertEqual(C.fold("José ÑÚÑEZ-Øvergård  Straße"), "jose nunez-overgard strasse")
        self.assertTrue(C.match("jose nun", "José Núñez"))
        self.assertTrue(C.match("", "anything"))
        self.assertFalse(C.match("ana maria", "Ana Ruiz"))
        self.assertTrue(C.match("maria", "Ana", "María"))

    def test_clean_text(self):
        C = self.C
        self.assertEqual(C.clean_text(None, 5), "")
        with self.assertRaises(C.FieldError):
            C.clean_text(5, 5)
        with self.assertRaises(C.FieldError):
            C.clean_text("toolong", 3)
        with self.assertRaises(C.FieldError):
            C.clean_text("  ", 3, required=True)
        self.assertEqual(C.clean_text("a\nb⁦c\x85", 10), "a bc")
        self.assertEqual(C.clean_text("a\r\nb\tc", 10, multiline=True), "a\nb c")

    def test_choices_ints_bools(self):
        C = self.C
        with self.assertRaises(C.FieldError):
            C.clean_int(True, 1, 5, "n")
        with self.assertRaises(C.FieldError):
            C.clean_int(9, 1, 5, "n")
        self.assertEqual(C.clean_int(None, 1, 5, "n", default=1), 1)
        with self.assertRaises(C.FieldError):
            C.clean_bool(1, "b")
        self.assertEqual(C.cfg_int({"x": True}, "x", 3, 1, 9), 3)
        self.assertEqual(C.cfg_int({"x": 100}, "x", 3, 1, 9), 9)
        self.assertEqual(C.cfg_int({"x": "5"}, "x", 3, 1, 9), 3)
        self.assertFalse(C.cfg_bool({"x": "yes"}, "x", False))

    def test_tokens(self):
        C = self.C
        raw, h = C.new_token()
        self.assertTrue(C.token_ok(raw, h))
        self.assertFalse(C.token_ok(raw + "x", h))
        self.assertFalse(C.token_ok(None, h)); self.assertFalse(C.token_ok(raw, None)); self.assertFalse(C.token_ok("x" * 200, h))

    def test_rate_limit_window_and_memory(self):
        C = self.C
        now = [1000.0]
        r = C.RateLimit(2, 60, clock=lambda: now[0], max_keys=10)
        self.assertEqual(r.check("a"), 0); self.assertEqual(r.check("a"), 0)
        self.assertGreater(r.check("a"), 0)
        now[0] += 61
        self.assertEqual(r.check("a"), 0)
        for i in range(50):
            r.check(f"k{i}")
        self.assertLessEqual(len(r.hits), 11)

    def test_store_survives_damage_and_failed_updates(self):
        C = self.C
        d = Path(tempfile.mkdtemp(prefix="store-"))
        try:
            s = C.JsonStore(d / "x.json", lambda: {"items": []})
            s.update(lambda x: (x["items"].append(1), True))
            with self.assertRaises(RuntimeError):
                def boom(x):
                    x["items"].append(2)
                    raise RuntimeError("fail")
                s.update(boom)
            self.assertEqual(s.view(lambda x: list(x["items"])), [1])      # half-made change not kept
            (d / "x.json").write_text('{"items": "nope"}')
            self.assertEqual(s.view(lambda x: x["items"]), [])
            self.assertEqual([p.name for p in d.iterdir() if p.name.startswith(".")], [])   # no temp files left
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_csv_cell(self):
        C = self.C
        self.assertEqual(C.csv_cell("=1+1"), "'=1+1")
        self.assertEqual(C.csv_cell("-2"), "'-2")
        self.assertEqual(C.csv_cell("ok"), "ok")
        self.assertEqual(C.csv_cell(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=1)
