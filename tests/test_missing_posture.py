#!/usr/bin/env python3
"""
Missing-person radio announcements follow the security posture (features/missing.py, software/common/posture.py):
Neighbourly → the public channel (admin-approved, as before); Guarded → the trusted circle only, as a pointer through
the circles feature's sharing path when "missing" is opted in, else nothing (and the admin is told why); Lockdown →
never. Through the real portal with the missing and circles features loaded (tests/redteam/harness.py).
Run: python3.11 tests/test_missing_posture.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "redteam"))
from harness import PHONE_HTTP, PortalCase, server  # noqa: E402


class MissingPosture(PortalCase):
    FEATURES = ("_community_common.py", "missing.py", "circles.py")
    EXTRA_TOML = "[missing]\nmesh = true\nmesh_per_hour = 30\nboard_copy = true\n"
    N = [0]

    def setUp(self):
        super().setUp()
        self.mod = next(f["mod"] for f in server.FEATURES if f["id"] == "missing")
        self.mod._reset_limits()
        (self.tmp / "mesh-outbox.jsonl").write_text("")
        (self.tmp / "mesh-circle-slot.json").unlink(missing_ok=True)
        st = self.tmp / "features-data" / "circles" / "state.json"
        st.unlink(missing_ok=True)
        self.posture("guarded")

    def tearDown(self):
        (self.tmp / "posture.json").unlink(missing_ok=True)
        server._cache.pop("posture", None)

    # helpers
    def posture(self, level):
        (self.tmp / "posture.json").write_text(json.dumps({"level": level, "changed_at": 1, "by": "test"}))
        server._cache.pop("posture", None)

    def in_circle(self, share_missing=True):
        (self.tmp / "mesh-circle-slot.json").write_text(json.dumps({"ch": 2}))
        st, r = self.req("POST", "/api/circles/settings", {"name": "Oak", "share": {"missing": share_missing}},
                         admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)

    def add(self, name="Ana Ruiz", **kw):
        self.N[0] += 1
        body = {"kind": "person", "name": name, "description": "8 years old, red jacket", "where": "Elm St",
                "when": "14:00", "mesh_request": True, **kw}
        st, r = self.req("POST", "/api/missing/add", body,
                         headers={"X-Real-IP": f"10.10.20.{self.N[0]}", "X-Forwarded-Proto": "http"})
        self.assertEqual(st, 200, r)
        return r

    def send(self, pid, text=None):
        body = {"id": pid, "op": "mesh_send", **({"text": text} if text is not None else {})}
        return self.req("POST", "/api/missing/moderate", body, admin=self.admin, headers=PHONE_HTTP)

    def outbox(self):
        return [json.loads(ln) for ln in (self.tmp / "mesh-outbox.jsonl").read_text().splitlines() if ln.strip()]

    def offers(self):
        d = json.loads((self.tmp / "features-data" / "circles" / "bridge.json").read_text())
        return [o for o in d["offers"] if o["category"] == "missing"]

    def listing(self):
        return self.req("GET", "/api/missing", admin=self.admin, headers=PHONE_HTTP)[1]

    # ── Neighbourly ──
    def test_neighbourly_public_channel(self):
        self.posture("neighbourly")
        a = self.add()
        self.assertEqual(self.listing()["mesh_route"], "public")
        st, r = self.send(a["id"])
        self.assertEqual((st, r["via"]), (200, "public"), r)
        self.assertEqual([(o["board"], o["text"][:17]) for o in self.outbox()], [("community", "MISSING: Ana Ruiz")])
        self.assertEqual(self.listing()["posts"][0]["mesh_via"], "public")

    # ── Guarded ──
    def test_guarded_without_a_circle_sends_nothing_and_says_why(self):
        a = self.add()
        lst = self.listing()
        self.assertEqual((lst["mesh_route"], lst["posture"]), ("none", "guarded"))
        self.assertIn("isn't in a trusted circle", lst["mesh_note"])
        st, r = self.send(a["id"])
        self.assertEqual(st, 409)
        self.assertIn("Neighbourly", r["error"])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.listing()["posts"][0]["mesh"], "requested")       # still waiting
        # visitors never see the routing note
        self.assertNotIn("mesh_note", self.req("GET", "/api/missing", headers=PHONE_HTTP)[1])

    def test_guarded_circle_not_opted_in(self):
        self.in_circle(share_missing=False)
        a = self.add()
        st, r = self.send(a["id"])
        self.assertEqual(st, 409)
        self.assertIn("isn't turned on", r["error"])
        self.assertEqual((self.outbox(), self.offers()), ([], []))

    def test_guarded_circle_feature_switched_off(self):
        saved = list(server.FEATURES)
        server.FEATURES[:] = [f for f in saved if f["id"] != "circles"]
        try:
            a = self.add()
            st, r = self.send(a["id"])
            self.assertEqual(st, 409)
            self.assertIn("switched off", r["error"])
        finally:
            server.FEATURES[:] = saved
        self.assertEqual(self.outbox(), [])

    def test_guarded_circle_pointer_lifecycle(self):
        self.in_circle()
        a = self.add()
        self.assertEqual(self.listing()["mesh_route"], "circle")
        st, r = self.send(a["id"])
        self.assertEqual((st, r["via"]), (200, "circle"), r)
        self.assertEqual(self.outbox(), [])                                      # nothing broadcast
        self.assertEqual([o["text"] for o in self.offers()], ["MISSING: Ana Ruiz, 8 years old, red jacket"])
        self.assertNotIn("Elm", json.dumps(self.offers()))                       # no places for the circle
        self.assertNotIn("Missing & found", (self.tmp / "board.jsonl").read_text() if (self.tmp / "board.jsonl").exists() else "")
        self.assertEqual(self.listing()["posts"][0]["mesh_via"], "circle")
        # found safe: the old pointer goes at once; the FOUND SAFE follow-up shares a new one when approved
        st, r = self.req("POST", "/api/missing/update", {"id": a["id"], "token": a["token"], "status": "safe"},
                         headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        self.assertEqual(self.offers(), [])
        self.assertEqual(self.listing()["posts"][0]["mesh"], "requested")
        st, r = self.send(a["id"])
        self.assertEqual((st, r["sent"]), (200, "FOUND SAFE: Ana Ruiz"), r)
        self.assertEqual([o["text"] for o in self.offers()], ["FOUND SAFE: Ana Ruiz"])
        # removed by the admin: pointer gone
        self.assertEqual(self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "remove"}, admin=self.admin,
                                  headers=PHONE_HTTP)[0], 200)
        self.assertEqual(self.offers(), [])

    def test_guarded_owner_removal_and_custom_text(self):
        self.in_circle()
        a = self.add(name="Tomas")
        st, r = self.send(a["id"], text="MISSING: Tomas, grey hoodie, ask for Maria")
        self.assertEqual((st, r["sent"]), (200, "MISSING: Tomas, grey hoodie, ask for Maria"), r)
        self.assertEqual(self.req("POST", "/api/missing/remove", {"id": a["id"], "token": a["token"]},
                                  headers=PHONE_HTTP)[0], 200)
        self.assertEqual(self.offers(), [])

    def test_guarded_refuses_places_and_amounts(self):
        self.in_circle()
        a = self.add(name="12 Elm St resident", description="")
        st, r = self.send(a["id"])
        self.assertEqual(st, 409)
        self.assertIn("address", r["error"])
        b = self.add(name="Bo")
        st, r = self.send(b["id"], text="MISSING: Bo, took 20 gallons of water")
        self.assertEqual((st, r["sent"]), (200, "MISSING: Bo, 8 years old, red jacket"))     # falls back to the safe line
        self.assertEqual(self.outbox(), [])

    # ── Lockdown ──
    def test_lockdown_never(self):
        self.in_circle()
        a = self.add()
        self.posture("lockdown")
        lst = self.listing()
        self.assertEqual((lst["mesh_route"], lst["posture"]), ("none", "lockdown"))
        st, r = self.send(a["id"])
        self.assertEqual(st, 409)
        self.assertIn("Lockdown", r["error"])
        st, r = self.send(a["id"], text="MISSING: Ana")
        self.assertEqual(st, 409)
        self.assertEqual((self.outbox(), self.offers()), ([], []))

    def test_damaged_posture_counts_as_lockdown(self):
        a = self.add()
        (self.tmp / "posture.json").write_text("{not json")
        server._cache.pop("posture", None)
        self.assertEqual(self.send(a["id"])[0], 409)
        self.assertEqual(self.outbox(), [])

    def test_only_the_admin_approves(self):
        self.posture("neighbourly")
        a = self.add()
        self.assertEqual(self.req("POST", "/api/missing/moderate", {"id": a["id"], "op": "mesh_send"},
                                  headers=PHONE_HTTP)[0], 401)
        self.assertEqual(self.outbox(), [])


if __name__ == "__main__":
    unittest.main()
