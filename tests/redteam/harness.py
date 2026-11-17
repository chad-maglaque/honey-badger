"""
Red-team harness: a real standalone portal on a free localhost port with a throwaway data root,
selected feature plugins copied in, and helpers that make requests the way a phone on the hotspot
does (through "nginx": X-Real-IP + X-Forwarded-Proto). Used by tests/test_redteam_*.py.

Nothing here touches a real network or radio.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FEATS = REPO / "software" / "portal" / "features"
sys.path.insert(0, str(REPO / "software" / "portal"))
sys.path.insert(0, str(REPO / "software" / "common"))
os.environ["PORTAL_QUIET"] = "1"
import server  # noqa: E402

PIN = "246810"
OPENSSL = "/usr/bin/openssl" if Path("/usr/bin/openssl").exists() else shutil.which("openssl")
# A phone on the Badger's Wi-Fi, as nginx forwards it (plain http://badger.lan — the common case).
PHONE_HTTP = {"X-Real-IP": "10.10.10.77", "X-Forwarded-Proto": "http"}
ROGUE_HTTP = {"X-Real-IP": "10.10.10.66", "X-Forwarded-Proto": "http"}


class PortalCase(unittest.TestCase):
    """setUpClass starts the portal with FEATURES (file names under software/portal/features)."""
    FEATURES: tuple[str, ...] = ()
    EXTRA_TOML = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="redteam-")).resolve()
        (cls.tmp / "data").mkdir()
        (cls.tmp / "media").mkdir()
        feats = cls.tmp / "features"
        feats.mkdir()
        for f in cls.FEATURES:
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
admin_pin = "{server.hash_pin(PIN)}"
board_file = "{cls.tmp}/board.jsonl"
family_board_file = "{cls.tmp}/family-board.jsonl"
members_file = "{cls.tmp}/members.json"
settings_file = "{cls.tmp}/portal-settings.json"
mesh_outbox = "{cls.tmp}/mesh-outbox.jsonl"
mesh_nodes = "{cls.tmp}/mesh-nodes.json"
mesh_channel_link = "{cls.tmp}/mesh-channel.json"
features_data = "{cls.tmp}/features-data"
usb_root = "{cls.tmp}/media"
ctl = "/nonexistent/prepperctl"
services = {{}}

[vault]
scrypt_n = 1024
openssl = "{OPENSSL}"
quota_mb = 40
{cls.EXTRA_TOML}
""")
        server.CFG = server.PortalConfig(cfg)
        server._members = None
        server.reset_login_limits()
        server.load_features(feats)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        st, r = cls.req("POST", "/api/admin/login", {"pin": PIN})
        assert st == 200, r
        cls.admin = r["token"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        server._last_post.clear()

    @classmethod
    def raw(cls, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=30)
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = body if isinstance(body, (bytes, str)) or body is None else json.dumps(body)
        c.request(method, path, data, h)
        r = c.getresponse()
        out = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, out, hdrs

    @classmethod
    def req(cls, method, path, body=None, member=None, admin=None, headers=None):
        h = dict(headers or {})
        if member:
            h["X-Member-Token"] = member
        if admin:
            h["X-Admin-Token"] = admin
        st, out, _ = cls.raw(method, path, body, h)
        try:
            return st, json.loads(out)
        except ValueError:
            return st, {"_raw": out[:200]}

    def admin_op(self, body, headers=None):
        return self.req("POST", "/api/admin/members", body, admin=self.admin, headers=headers or PHONE_HTTP)

    def join(self, name, member_id=None, headers=None):
        st, inv = self.admin_op({"op": "invite", "name": "" if member_id else name, **({"id": member_id} if member_id else {})})
        self.assertEqual(st, 200, inv)
        st, j = self.req("POST", "/api/members/join", {"code": inv["code"], "name": name, "device": "phone"},
                         headers=headers or PHONE_HTTP)
        self.assertEqual(st, 200, j)
        return j["key"]

    def member_id(self, name):
        return [m["id"] for m in self.admin_op({"op": "list"})[1]["members"] if m["name"] == name and not m["removed"]][0]
