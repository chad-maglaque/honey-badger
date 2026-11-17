"""
Security posture (software/common/posture.py + features/posture.py + the portal's visitor gating):
the rules table, overrides, the posture.json contract, and the real portal over HTTP — who may see and
change it, what visitors may open in each posture, Lockdown's Wi-Fi step through prepperctl.
Run: python3.11 tests/test_posture.py
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FEATS = REPO / "software" / "portal" / "features"
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "software" / "common"))
os.environ["PORTAL_QUIET"] = "1"
import posture as P  # noqa: E402
import server  # noqa: E402

PIN = "246810"
PHONE = {"X-Real-IP": "10.10.10.77", "X-Forwarded-Proto": "http"}
PHONE_TLS = {"X-Real-IP": "10.10.10.77", "X-Forwarded-Proto": "https"}
FAKE_CTL = """#!/bin/sh
echo "$*" >> "$(dirname "$0")/ctl.log"
case "$1" in
  wifi-secure) echo "passphrase: ABCD-EFGH-JKMN"; echo "confirm-within: 600" ;;
  posture-apply) echo "nothing to change" ;;
  wifi-off) echo "Wi-Fi off" ;;
esac
exit 0
"""


class PostureCase(unittest.TestCase):
    """A real standalone portal with posture/circles/reveal (+ FEATURES), everything in a temp dir, a fake
    prepperctl that logs its verbs, and a fake front-panel key."""
    FEATURES: tuple[str, ...] = ("posture.py", "circles.py", "reveal.py")
    NETWORK = 'ssid = "Home-K7QX"\npassphrase = ""\nsecurity = "auto"'
    EXTRA = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="posture-")).resolve()
        t = cls.tmp
        for d in ("data", "run", "features", "media", "etc"):
            (t / d).mkdir()
        off = "\n".join(f"{f.stem} = false" for f in FEATS.glob("*.py")
                        if not f.name.startswith("_") and f.name not in cls.FEATURES)
        (t / "fakectl").write_text(FAKE_CTL)
        (t / "fakectl").chmod(0o755)
        cls.panel_key = "test-panel-key-123"
        import hashlib
        (t / "etc" / "panel.key.sha256").write_text(hashlib.sha256(cls.panel_key.encode()).hexdigest() + "\n")
        (t / "prepper.toml").write_text(f"""
[network]
{cls.NETWORK}

[upd]
data_root = "{t / 'data'}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{t}/upd-state.json"
report_dir = "{t}/reports"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
admin_pin = "{server.hash_pin(PIN)}"
board_file = "{t}/board.jsonl"
family_board_file = "{t}/family-board.jsonl"
members_file = "{t}/members.json"
settings_file = "{t}/portal-settings.json"
mesh_outbox = "{t}/mesh-outbox.jsonl"
features_data = "{t}/features-data"
usb_root = "{t}/media"
run_dir = "{t}/run"
panel_key_hash = "{t}/etc/panel.key.sha256"
panel_lock_file = "{t}/etc/panel-lock.json"
panel_lock_arm = "{t}/panel-lock-arm.json"
ctl = "{t}/fakectl"
services = {{}}
{cls.EXTRA}

[features]
{off}
""")
        server.CFG = server.PortalConfig(t / "prepper.toml")
        server._members = None
        server._cache.clear()
        server.reset_login_limits()
        server.load_features(FEATS)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        st, r = cls.req("POST", "/api/admin/login", {"pin": PIN})
        assert st == 200, r
        cls.admin = r["token"]
        st, inv = cls.req("POST", "/api/admin/members", {"op": "invite", "name": "Ana"}, admin=cls.admin)
        st, j = cls.req("POST", "/api/members/join", {"code": inv["code"], "name": "Ana", "device": "phone"}, headers=PHONE)
        assert st == 200, j
        cls.member = j["key"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        server._last_post.clear()
        server._cache.pop("posture", None)
        self.set_level(None)
        for f in ("portal-settings.json", "ctl.log"):
            (self.tmp / f).unlink(missing_ok=True)

    @classmethod
    def set_level(cls, level):
        p = cls.tmp / "posture.json"
        if level is None:
            p.unlink(missing_ok=True)
        else:
            P.write(p, level, "test")
        server._cache.pop("posture", None)

    def ctl_log(self):
        p = self.tmp / "ctl.log"
        return p.read_text().split("\n") if p.exists() else []

    @classmethod
    def req(cls, method, path, body=None, member=None, admin=None, headers=None, panel=False):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=30)
        h = {"Content-Type": "application/json"}
        if panel:
            h["X-Badger-Panel"] = cls.panel_key
        h.update(headers or {})
        if member:
            h["X-Member-Token"] = member
        if admin:
            h["X-Admin-Token"] = admin
        c.request(method, path, None if body is None else (body if isinstance(body, (str, bytes)) else json.dumps(body)), h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, {"_raw": raw[:200]}


# ───────────────────────────── the rules ─────────────────────────────

class Rules(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="posture-rules-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_is_guarded_and_config_default_counts(self):
        self.assertEqual(P.read(self.tmp / "none.json")["level"], "guarded")
        self.assertEqual(P.read(self.tmp / "none.json", {"security": {"posture": "neighbourly"}})["level"], "neighbourly")
        self.assertEqual(P.read(self.tmp / "none.json", {"security": {"posture": "chaos"}})["level"], "guarded")

    def test_write_contract_and_mode(self):
        rec = P.write(self.tmp / "p.json", "lockdown", "front panel", now=123.0)
        d = json.loads((self.tmp / "p.json").read_text())
        self.assertEqual(d, {"level": "lockdown", "changed_at": 123.0, "by": "front panel"})
        self.assertEqual(rec, d)
        self.assertEqual(stat.S_IMODE((self.tmp / "p.json").stat().st_mode), 0o640)
        with self.assertRaises(ValueError):
            P.write(self.tmp / "p.json", "panic", "x")
        info = P.read(self.tmp / "p.json")
        self.assertEqual((info["level"], info["by"], info["damaged"]), ("lockdown", "front panel", False))

    def test_damaged_file_fails_closed(self):
        for junk in ("", "not json", '{"level": "open"}', "[1,2]", '{"level": 5}', "x" * 5000):
            (self.tmp / "p.json").write_text(junk)
            info = P.read(self.tmp / "p.json")
            self.assertEqual((info["level"], info["damaged"]), ("lockdown", True), junk[:20])

    def test_table_per_level(self):
        n, g, lk = (P.values(P.effective(lv)) for lv in P.LEVELS)
        self.assertEqual(n["mesh_commands"]["q"], "community")
        self.assertEqual(n["mesh_commands"]["help"], "community")
        self.assertEqual(g["mesh_commands"], {"help": "family", "q": "community", "ai": "family", "status": "family"})
        self.assertTrue(all(v == "family" for v in lk["mesh_commands"].values()))
        self.assertEqual((n["public_position"], g["public_position"], lk["public_position"]), ("coarse", "off", "off"))
        self.assertEqual((n["radio_silence"], g["radio_silence"], lk["radio_silence"]), (False, False, True))
        self.assertTrue(lk["silent_allow_family"])
        self.assertEqual((g["alerts_public"], lk["alerts_public"]), (True, False))
        self.assertEqual((g["stranger_replies_per_hour"], lk["stranger_replies_per_hour"]), (6, 0))
        self.assertEqual((g["visitor_community"], lk["visitor_community"]), ("public", "members"))
        self.assertEqual((g["visitor_sensors"], lk["visitor_sensors"]), ("alerts", "none"))
        self.assertEqual((g["circle_sharing"], lk["circle_sharing"]), ("opt-in", "none"))
        self.assertEqual((g["lockdown_radio"], lk["lockdown_radio"]), (False, True))
        self.assertEqual((g["logs"], lk["logs"]), ("ram", "ram"))
        self.assertEqual(lk["visitor_mesh"], "off")

    def test_overrides_shown_as_such(self):
        raw = {"radio": {"mesh_commands": {"q": "ignore", "bogus": "community", "ai": "loud"},
                         "mesh_stranger_replies_per_hour": 2, "mesh_public_position": 11},
               "portal": {"visitor_mesh": "off"}, "sensors": {"public": True}}
        eff = P.effective("guarded", raw, {"mesh_commands": {"status": "off"}})
        self.assertEqual(eff["mesh_commands"]["q"], {"value": "ignore", "posture": "community", "source": "override",
                                                     "from": "prepper.toml [radio] mesh_commands"})
        self.assertEqual(eff["mesh_commands"]["status"]["from"], "Settings → Radio commands")
        self.assertEqual(eff["mesh_commands"]["ai"]["source"], "posture")          # a bad level is no override
        self.assertEqual(eff["stranger_replies_per_hour"]["value"], 2)
        self.assertEqual(eff["public_position"]["value"], 11)
        self.assertEqual(eff["visitor_mesh"]["value"], "off")
        self.assertEqual(eff["visitor_sensors"]["value"], "readings")

    def test_launcher_settings_win_over_toml(self):
        eff = P.effective("guarded", {"radio": {"mesh_public_position": "coarse"}}, {"mesh_public_position": "off"})
        self.assertEqual((eff["public_position"]["value"], eff["public_position"]["from"]),
                         ("off", "Settings (what this Badger reveals)"))

    def test_lockdown_ignores_looser_overrides_keeps_stricter(self):
        raw = {"radio": {"mesh_silent": False, "mesh_alerts_public": True, "mesh_commands": {"q": "community", "ai": "ignore"},
                         "mesh_stranger_replies_per_hour": 60, "mesh_public_position": "precise"},
               "portal": {"visitor_mesh": "approve"}, "sensors": {"public": True},
               "security": {"visitor_community": "public", "persistent_logs": True}}
        eff = P.effective("lockdown", raw)
        v = P.values(eff)
        self.assertTrue(v["radio_silence"])
        self.assertFalse(v["alerts_public"])
        self.assertEqual(v["mesh_commands"]["q"], "family")
        self.assertEqual(eff["mesh_commands"]["q"]["source"], "ignored")
        self.assertEqual(v["mesh_commands"]["ai"], "ignore")                    # stricter: honoured
        self.assertEqual((v["stranger_replies_per_hour"], v["public_position"]), (0, "off"))
        self.assertEqual((v["visitor_mesh"], v["visitor_sensors"], v["visitor_community"], v["logs"]),
                         ("off", "none", "members", "ram"))
        self.assertEqual(eff["radio_silence"]["source"], "ignored")

    def test_help_is_never_off_and_logs_persist_only_when_neighbourly(self):
        eff = P.effective("guarded", {"radio": {"mesh_commands": {"help": "off"}}})
        self.assertEqual(eff["mesh_commands"]["help"]["value"], "family")
        sec = {"security": {"persistent_logs": True}}
        self.assertEqual(P.values(P.effective("neighbourly", sec))["logs"], "persistent")
        self.assertEqual(P.effective("guarded", sec)["logs"]["source"], "ignored")

    def test_lockdown_options(self):
        v = P.values(P.effective("lockdown", {"security": {"lockdown_radio": False, "lockdown_wifi": "off"}}))
        self.assertEqual((v["lockdown_radio"], v["lockdown_wifi"]), (False, "off"))
        v = P.values(P.effective("guarded", {"security": {"lockdown_wifi": "nonsense"}}))
        self.assertEqual(v["lockdown_wifi"], "password")
        self.assertFalse(v["lockdown_radio"])

    def test_garbage_config_never_raises(self):
        for raw in (None, [], {"radio": "x"}, {"radio": {"mesh_commands": [1]}}, {"security": 5},
                    {"radio": {"mesh_stranger_replies_per_hour": float("nan")}}, {"radio": {"mesh_public_position": True}}):
            eff = P.effective("guarded", raw, {"mesh_commands": "x"})
            self.assertEqual(eff["stranger_replies_per_hour"]["value"], 6)
        self.assertEqual(P.effective("bogus")["radio_silence"]["value"], True)      # unknown level: Lockdown

    def test_describe(self):
        self.assertIn("!q anyone", P.describe("mesh_commands", {"help": "family", "q": "community", "ai": "family",
                                                                "status": "off"}))
        self.assertEqual(P.describe("public_position", "coarse"), "rough (~6 km)")
        self.assertEqual(P.describe("radio_silence", True), "on")


# ───────────────────────────── over HTTP ─────────────────────────────

class PostureHTTP(PostureCase):
    FEATURES = ("posture.py", "circles.py", "reveal.py", "skills.py", "aircraft.py", "sensors.py", "aidstation.py")

    def test_who_may_see_it(self):
        self.assertEqual(self.req("GET", "/api/posture", headers=PHONE)[0], 401)
        st, d = self.req("GET", "/api/posture", member=self.member, headers=PHONE)
        self.assertEqual((st, d["level"]), (200, "guarded"))
        self.assertNotIn("by", d)
        st, d = self.req("GET", "/api/posture", admin=self.admin, headers=PHONE)
        self.assertEqual(st, 200)
        self.assertIn("by", d)
        self.assertEqual({r["knob"] for r in d["settings"]} >= {"mesh_commands", "radio_silence", "wifi"}, True)
        st, d = self.req("GET", "/api/posture", panel=True)
        self.assertEqual(st, 200)

    def test_status_shows_posture_to_family_not_visitors(self):
        self.set_level("lockdown")
        self.assertEqual(self.req("GET", "/api/status", member=self.member, headers=PHONE)[1]["posture"]["level"], "lockdown")
        self.assertIsNone(self.req("GET", "/api/status", headers=PHONE)[1]["posture"])

    def test_only_admin_or_panel_change_it(self):
        self.assertEqual(self.req("POST", "/api/posture", {"level": "lockdown"}, headers=PHONE)[0], 401)
        self.assertEqual(self.req("POST", "/api/posture", {"level": "lockdown"}, member=self.member, headers=PHONE)[0], 401)
        self.assertEqual(self.req("POST", "/api/posture/panel", {"level": "lockdown"}, admin=self.admin, headers=PHONE)[0], 403)
        self.assertFalse((self.tmp / "posture.json").exists())
        st, r = self.req("POST", "/api/posture", {"level": "neighbourly"}, admin=self.admin, headers=PHONE)
        self.assertEqual((st, r["posture"]["level"]), (200, "neighbourly"))
        d = json.loads((self.tmp / "posture.json").read_text())
        self.assertEqual((d["level"], d["by"]), ("neighbourly", "admin"))
        st, r = self.req("POST", "/api/posture/panel", {"level": "guarded"}, panel=True)
        self.assertEqual((st, json.loads((self.tmp / "posture.json").read_text())["by"]), (200, "front panel"))

    def test_bad_input(self):
        for body in ({"level": "chaos"}, {"level": None}, {"level": ["lockdown"]}, {}, {"level": "lockdown", "wifi": "boom"}):
            self.assertEqual(self.req("POST", "/api/posture", body, admin=self.admin, headers=PHONE)[0], 400, body)
        self.assertFalse((self.tmp / "posture.json").exists())
        self.assertIn(self.req("POST", "/api/posture", "[1]", admin=self.admin, headers=PHONE)[0], (400,))

    def test_lockdown_wifi_password_hidden_over_http_shown_over_https(self):
        st, r = self.req("POST", "/api/posture", {"level": "lockdown"}, admin=self.admin, headers=PHONE)
        self.assertEqual(st, 200)
        self.assertTrue(r["wifi"]["ok"])
        self.assertIsNone(r["wifi"]["passphrase"])
        self.assertTrue(r["wifi"]["secret_hidden"])
        self.assertIn("wifi-secure", self.ctl_log())
        self.assertIn("posture-apply", self.ctl_log())
        self.set_level("guarded")
        st, r = self.req("POST", "/api/posture", {"level": "lockdown"}, admin=self.admin, headers=PHONE_TLS)
        self.assertEqual(r["wifi"]["passphrase"], "ABCD-EFGH-JKMN")

    def test_lockdown_wifi_off_and_keep(self):
        st, r = self.req("POST", "/api/posture", {"level": "lockdown", "wifi": "off"}, admin=self.admin, headers=PHONE)
        self.assertEqual(r["wifi"]["action"], "off")
        self.assertIn("wifi-off", self.ctl_log())
        self.set_level("guarded")
        (self.tmp / "ctl.log").unlink()
        st, r = self.req("POST", "/api/posture", {"level": "lockdown", "wifi": "keep"}, admin=self.admin, headers=PHONE)
        self.assertIsNone(r["wifi"])
        self.assertNotIn("wifi-secure", self.ctl_log())

    def test_family_board_note(self):
        self.req("POST", "/api/posture", {"level": "neighbourly"}, admin=self.admin, headers=PHONE)
        st, b = self.req("GET", "/api/board?board=family", member=self.member, headers=PHONE)
        self.assertTrue(any("Neighbourly" in m["text"] for m in b["messages"]))

    def test_visitors_in_lockdown(self):
        for path in ("/api/skills", "/api/aircraft", "/api/sensors/alerts", "/api/board?board=community"):
            self.assertNotEqual(self.req("GET", path, headers=PHONE)[0], 403, path)
        self.set_level("lockdown")
        for path in ("/api/skills", "/api/aircraft", "/api/sensors/alerts", "/api/board?board=community"):
            st, r = self.req("GET", path, headers=PHONE)
            self.assertEqual(st, 403, path)
            self.assertTrue(r.get("posture"))
        st, r = self.req("POST", "/api/board", {"board": "community", "text": "hi", "name": "x"}, headers=PHONE)
        self.assertEqual(st, 403)
        st, r = self.req("POST", "/api/skills/add", {"kind": "skill", "text": "x"}, headers=PHONE)
        self.assertEqual(st, 403)
        # family members and the admin are not visitors
        self.assertEqual(self.req("GET", "/api/skills", member=self.member, headers=PHONE)[0], 200)
        self.assertEqual(self.req("GET", "/api/board?board=community", admin=self.admin, headers=PHONE)[0], 200)
        # tiles: hidden from visitors, still there for the family
        ids = {f["id"] for f in self.req("GET", "/api/features", headers=PHONE)[1]["features"]}
        self.assertFalse({"skills", "aircraft", "sensors"} & ids)
        ids = {f["id"] for f in self.req("GET", "/api/features", member=self.member, headers=PHONE)[1]["features"]}
        self.assertTrue({"skills", "aircraft", "sensors"} <= ids)
        # the library and the Ask page are untouched
        self.assertEqual(self.req("GET", "/api/library", headers=PHONE)[0], 200)

    def test_aid_volunteers_still_reach_the_aid_station(self):
        self.set_level("lockdown")
        self.assertEqual(self.req("GET", "/api/aidstation/status", headers=PHONE)[0], 403)
        st, _ = self.req("GET", "/api/aidstation/me", headers=dict(PHONE, **{"X-Aid-Token": "x" * 20}))
        self.assertNotEqual(st, 403)

    def test_settings_override_relaxes_guarded_but_not_lockdown(self):
        (self.tmp / "portal-settings.json").write_text(json.dumps({"visitor_community": "members"}))
        server._cache.pop("posture", None)
        self.assertEqual(self.req("GET", "/api/skills", headers=PHONE)[0], 403)
        (self.tmp / "portal-settings.json").write_text(json.dumps({"visitor_community": "public"}))
        self.set_level("lockdown")
        self.assertEqual(self.req("GET", "/api/skills", headers=PHONE)[0], 403)

    def test_visitor_mesh_off_in_lockdown(self):
        st, r = self.req("POST", "/api/board", {"board": "community", "text": "hello", "name": "v", "mesh": True}, headers=PHONE)
        self.assertIn("family member will decide", r["message"])
        self.set_level("lockdown")
        (self.tmp / "portal-settings.json").write_text(json.dumps({"visitor_community": "public"}))
        server._cache.pop("posture", None)
        st, r = self.req("POST", "/api/board", {"board": "community", "text": "hello", "name": "v", "mesh": True}, headers=PHONE)
        self.assertEqual(st, 403)                      # (Lockdown ignores the looser visitor setting)

    def test_damaged_file_is_lockdown_for_visitors(self):
        (self.tmp / "posture.json").write_text("garbage")
        server._cache.pop("posture", None)
        self.assertEqual(self.req("GET", "/api/skills", headers=PHONE)[0], 403)
        st, d = self.req("GET", "/api/posture", admin=self.admin, headers=PHONE)
        self.assertTrue(d["damaged"])

    def test_clear_overrides(self):
        (self.tmp / "portal-settings.json").write_text(json.dumps({"mesh_commands": {"q": "family"}, "display_time": "utc"}))
        server._cache.pop("posture", None)
        self.assertEqual(self.req("POST", "/api/posture/overrides", {"clear": ["../etc"]}, admin=self.admin, headers=PHONE)[0], 400)
        self.assertEqual(self.req("POST", "/api/posture/overrides", {"clear": ["mesh_commands"]}, headers=PHONE)[0], 401)
        st, r = self.req("POST", "/api/posture/overrides", {"clear": ["mesh_commands"]}, admin=self.admin, headers=PHONE)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads((self.tmp / "portal-settings.json").read_text()), {"display_time": "utc"})

    def test_radio_state_on_persistent_storage_is_sealed(self):
        nodes = {"updated": time.time(), "nodes": [{"id": "!a1b2c3d4", "name": "Hendersons", "lat": 39.75, "lon": -104.98,
                                                    "heard": time.time()}]}
        legacy = self.tmp / "mesh-peers.json"                     # an old default location (portal folder)
        legacy.write_text(json.dumps({"updated": 1, "peers": [{"id": "!a1b2c3d4"}]}))
        ram = self.tmp / "run" / "mesh-nodes.json"
        ram.write_text(json.dumps(nodes))
        self.req("GET", "/api/library", headers=PHONE)            # any request runs the guard
        d = json.loads(legacy.read_text())
        self.assertTrue(d["sealed"].startswith("hbs1:"))
        self.assertEqual(d["peers"], [])
        self.assertNotIn("a1b2c3d4", legacy.read_text())
        self.assertEqual(json.loads(ram.read_text()), nodes)       # RAM: left alone
        old = server.CFG.mesh_nodes
        try:
            server.CFG.mesh_nodes = self.tmp / "nodes-on-disk.json"
            server.CFG.mesh_nodes.write_text(json.dumps(nodes))
            st, r = self.req("GET", "/api/mesh/nodes", member=self.member, headers=PHONE)
            self.assertEqual([n["name"] for n in r["nodes"]], ["Hendersons"])   # opened (sealed during this run)
            self.assertNotIn("Hendersons", server.CFG.mesh_nodes.read_text())
            self.assertEqual(server.guard_mesh_state(), 0)        # already sealed: nothing to do
        finally:
            server.CFG.mesh_nodes = old
        legacy.unlink()

    def test_concurrent_changes_leave_a_valid_file(self):
        def flip(lv):
            for _ in range(5):
                self.req("POST", "/api/posture", {"level": lv, "wifi": "keep"}, admin=self.admin, headers=PHONE)
        ts = [threading.Thread(target=flip, args=(lv,)) for lv in ("guarded", "neighbourly", "lockdown")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        info = P.read(self.tmp / "posture.json")
        self.assertFalse(info["damaged"])
        self.assertEqual([p.name for p in self.tmp.glob(".posture.json.*")], [])


if __name__ == "__main__":
    unittest.main()
