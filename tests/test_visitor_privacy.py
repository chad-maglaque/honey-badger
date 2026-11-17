#!/usr/bin/env python3
"""
What a visitor (anyone on the Wi-Fi, no family key, no admin login) can learn — red-team findings
1, 2 and 10, through the real portal with every feature loaded.

  * a generic scan of EVERY public JSON answer (core API + every feature's public GET routes) for
    coordinates within 2 km of the home, for decimal fragments of the precise position, and for
    power / occupancy / uplink data;
  * the visitor grid ("miles is fine, it just can't be 100 yards NNW"): many different homes inside
    one grid cell give byte-identical visitor answers (status, time + sun, aircraft, satellites), so
    nothing — however often it is asked, with however many planes — narrows below the cell;
  * tests/redteam/trilaterate.py against /api/aircraft finds the snapped cell centre, never the house;
  * family members and the admin still get the precise data;
  * community posts reach the radio only with a member's/admin's approval; USB drives, sensors
    readings and the vault over plain http on a password-less hotspot are family-only/refused;
  * a 500 never carries exception text.

Run: python3.11 tests/test_visitor_privacy.py -v
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SDR_TMP = Path(tempfile.mkdtemp(prefix="vis-sdr-")).resolve()
os.environ.update({"PREPPER_SDR_DIR": str(SDR_TMP / "run"), "PREPPER_SDR_STATE": str(SDR_TMP / "state"),
                   "PREPPER_GPSD": "127.0.0.1:9", "PORTAL_QUIET": "1"})
(SDR_TMP / "run" / "readsb").mkdir(parents=True)
(SDR_TMP / "state").mkdir()
sys.path.insert(0, str(REPO / "tests" / "redteam"))
sys.path.insert(0, str(REPO / "software" / "sdr"))
sys.path.insert(0, str(REPO / "software" / "common"))
import harness  # noqa: E402
import orbit  # noqa: E402
import trilaterate  # noqa: E402
import visitor  # noqa: E402
from harness import PHONE_HTTP, PortalCase, server  # noqa: E402

HOME = (39.739212, -104.990251)            # the family's house (precise)
CELL = visitor.coarse(*HOME)               # (39.7, -105.0): what visitors may learn
FEATS = REPO / "software" / "portal" / "features"
PRIVATE_KEYS = {"power", "clients", "uplink", "uptime_s", "runtime_min", "input_w", "input_v", "percent", "charging"}
LAT_KEYS, LON_KEYS = ("lat", "latitude"), ("lon", "lng", "longitude")


def km(a, b):
    return trilaterate._haversine(a[0], a[1], b[0], b[1])


def offset(lat, lon, dn_km, de_km):
    return lat + dn_km / 111.195, lon + de_km / (111.195 * math.cos(math.radians(lat)))


def make_tle(norad, mm=14.24, raan=100.0, ma=270.0):
    t = time.gmtime(time.time() - 3600)
    doy = t.tm_yday + (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec) / 86400.0
    ep = f"{t.tm_year % 100:02d}{doy:012.8f}"
    l1 = f"1 {norad:05d}U 23091A   {ep}  .00000020  00000+0  28000-4 0  999"
    l2 = f"2 {norad:05d}  98.7000 {raan:8.4f} 0004000  90.0000 {ma:8.4f} {mm:11.8f}12345"
    return l1 + str(orbit.checksum(l1)), l2 + str(orbit.checksum(l2))


def walk(obj, path=""):
    """Every dict in a JSON value, with its path."""
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


def leaks(body: bytes, data) -> list[str]:
    """Everything in one visitor answer that gives away the house or the household."""
    found = []
    for path, d in walk(data):
        nums = {k: v for k, v in d.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        for la in LAT_KEYS:
            for lo in LON_KEYS:
                if la in nums and lo in nums and km((nums[la], nums[lo]), HOME) < 2.0:
                    found.append(f"{path}: {la}/{lo} = {nums[la]}, {nums[lo]} is within 2 km of the house")
        # any pair of numbers in one object that sits within 2 km of the house, whatever the keys
        vals = list(nums.items())
        for ka, a in vals:
            for kb, b in vals:
                if ka != kb and abs(a - HOME[0]) < 0.018 and abs(b - HOME[1]) < 0.024 \
                        and km((a, b), HOME) < 2.0:
                    found.append(f"{path}: {ka}={a}, {kb}={b} within 2 km of the house")
        for k in PRIVATE_KEYS & set(d):
            if d[k] not in (None, [], {}):
                found.append(f"{path}.{k} = {d[k]!r} (power / occupancy / uplink)")
    text = body.decode("utf-8", "replace")
    for frag in (r"39\.7[34]\d*", r"-104\.99\d*", r"-104\.98\d*"):
        for m in re.finditer(r"(?<![\d.])" + frag + r"(?!\d)", text):
            found.append(f"precise coordinate fragment {m.group(0)!r} in the text")
    return found


class VisitorPortal(PortalCase):
    FEATURES = ()                  # every feature is loaded from its real folder below (they import neighbours)
    EXTRA_TOML = f"""
[time]
latitude = {HOME[0]}
longitude = {HOME[1]}

[network]
hostname = "badger"
ssid = "Garden-Shed"
passphrase = ""
"""
    ip_n = 0

    @classmethod
    def setUpClass(cls):
        (SDR_TMP / "state" / "tle.txt").write_text("METEOR-M2 3\n" + "\n".join(make_tle(57166)) + "\n" +
                                                   "METEOR-M2 4\n" + "\n".join(make_tle(59051, raan=160.0, ma=40.0)) + "\n")
        super().setUpClass()
        server.load_features(FEATS)
        cls.loaded = {f["id"] for f in server.FEATURES}

    def setUp(self):
        super().setUp()
        self._gps = server.gps_fix
        self.set_home(*HOME)
        self.planes(random.Random(1), 40)
        (self.tmp / "power.json").write_text('{"percent": 41, "external": false, "runtime_min": 190, "input_w": 0, '
                                             '"input_v": 0, "mode": "endurance", "charging": false}')
        self._power = server.CFG.power_file
        server.CFG.power_file = self.tmp / "power.json"

    def tearDown(self):
        server.gps_fix = self._gps
        server.CFG.power_file = self._power
        server.CFG.time_lat, server.CFG.time_lon = HOME
        server.CFG.raw["time"] = {"latitude": HOME[0], "longitude": HOME[1]}

    # — helpers —
    def set_home(self, lat, lon):
        fix = {"lat": lat, "lon": lon, "alt": 1609.3, "time": "x", "mode": 3}
        server.gps_fix = lambda: dict(fix)
        server.CFG.time_lat, server.CFG.time_lon = lat, lon
        server.CFG.raw["time"] = {"latitude": lat, "longitude": lon}
        server._cache.clear()
        import sdrcommon
        sdrcommon._pos_cache.clear()
        (SDR_TMP / "state" / "position.json").unlink(missing_ok=True)
        for f in server.FEATURES:
            if f["id"] == "aircraft":
                f["mod"]._cache.clear()
            if f["id"] == "satellites":
                f["mod"]._vis_cache.clear()

    def planes(self, rnd, n, around=HOME):
        out = []
        for i in range(n):
            lat, lon = offset(around[0], around[1], rnd.uniform(-300, 300), rnd.uniform(-300, 300))
            out.append({"hex": f"{0xa00000 + i:06x}", "flight": f"TST{i}", "alt_baro": 30000, "gs": 400, "track": 90,
                        "lat": lat, "lon": lon, "seen": 0.5, "seen_pos": 1.0})
        (SDR_TMP / "run" / "readsb" / "aircraft.json").write_text(json.dumps({"now": time.time(), "aircraft": out}))
        for f in server.FEATURES:
            if f["id"] == "aircraft":
                f["mod"]._cache.clear()

    def visitor_get(self, path):
        VisitorPortal.ip_n += 1
        h = {"X-Real-IP": f"10.10.{10 + VisitorPortal.ip_n // 250}.{VisitorPortal.ip_n % 250 + 2}",
             "X-Forwarded-Proto": "http"}
        st, body, _ = self.raw("GET", path, None, h)
        try:
            return st, body, json.loads(body)
        except ValueError:
            return st, body, None

    def public_paths(self) -> list[str]:
        paths = ["/api/status", "/api/time", "/api/time?mode=solar", "/api/time?mode=apparent", "/api/library",
                 "/api/board", "/api/alerts", "/api/features", "/api/usb", "/api/settings", "/api/updates",
                 "/api/content/personal", "/api/search?q=water", "/api/members/me", "/api/mesh/nodes",
                 "/api/usb/ls?drive=x"]
        for f in server.FEATURES:
            for r in f["routes"]:
                if r.get("method", "GET") == "GET":
                    paths.append(r["path"] + ("x" if r.get("prefix") else ""))
        return paths

    # — the generic scan —
    def test_no_public_answer_locates_the_house_or_tells_power_and_occupancy(self):
        member = self.join("Ana")
        scanned = 0
        for path in self.public_paths():
            with self.subTest(path=path):
                st, body, data = self.visitor_get(path)
                self.assertLess(st, 500, f"{path}: {body[:200]!r}")
                if data is None:
                    continue
                scanned += 1
                self.assertEqual(leaks(body, data), [], path)
        self.assertGreater(scanned, 20)
        self.assertLessEqual({"aircraft", "satellites", "sensors", "missing", "skills", "aidstation"}, self.loaded)
        # the scan itself works: a member's view of the same data is precise and gets flagged
        st, body, _ = self.raw("GET", "/api/status", None, {**PHONE_HTTP, "X-Member-Token": member})
        self.assertTrue(leaks(body, json.loads(body)), "self-test: the scanner sees the member's precise data")

    def test_status_view_for_visitors(self):
        st, _, s = self.visitor_get("/api/status")
        self.assertEqual(st, 200)
        self.assertEqual((s["gps"]["lat"], s["gps"]["lon"]), CELL)
        for k in visitor.PRIVATE_STATUS_KEYS:
            self.assertIsNone(s[k], k)
        self.assertIsNotNone(s["storage"] if "storage" in s else True)      # library info still there
        self.assertIn("alerts", s)                                            # alerts stay public

    # — the grid —
    def visitor_snapshot(self) -> dict:
        out = {}
        st, _, s = self.visitor_get("/api/status")
        out["status.gps"] = s["gps"]
        for mode in ("civil", "solar", "apparent"):
            st, _, t = self.visitor_get(f"/api/time?mode={mode}")
            self.assertEqual(st, 200, t)
            out[f"time.{mode}"] = {k: t.get(k) for k in ("location", "sunEvents", "locationSource")}
            out[f"time.{mode}.notes"] = {k: v for k, v in (t.get("notes") or {}).items()}
        st, _, a = self.visitor_get("/api/aircraft")
        out["aircraft"] = {"origin": a["origin"], "aircraft": a["aircraft"]}
        st, _, s = self.visitor_get("/api/satellites")
        out["satellites"] = {"position": s["position"], "passes": s["passes"]}
        return out

    def test_every_home_in_one_cell_gives_identical_visitor_answers(self):
        rnd = random.Random(7)
        step = visitor.lon_step(CELL[0])
        homes = [HOME] + [(rnd.uniform(CELL[0] - 0.0499, CELL[0] + 0.0499),
                           rnd.uniform(CELL[1] - step / 2 + 0.001, CELL[1] + step / 2 - 0.001)) for _ in range(12)]
        for _attempt in range(2):                       # the visitor pass list is rebuilt every 10 minutes
            bucket = int(time.time() // 600)
            snaps = []
            for h in homes:
                self.assertEqual(visitor.coarse(*h), CELL)
                self.set_home(*h)
                snaps.append(self.visitor_snapshot())
            if int(time.time() // 600) == bucket:
                break
        self.assertTrue(snaps[0]["satellites"]["passes"], "test premise: some predicted passes")
        self.assertTrue(snaps[0]["aircraft"]["aircraft"], "test premise: planes")
        for h, s in zip(homes[1:], snaps[1:], strict=True):
            for k in s:
                self.assertEqual(s[k], snaps[0][k], f"{k} differs between homes {homes[0]} and {h} in the same cell")
        # and each home's real position is never what the visitor sees
        for h in homes:
            self.assertNotEqual(snaps[0]["status.gps"]["lat"], h[0])

    def test_sun_and_pass_times_are_to_the_minute(self):
        st, _, t = self.visitor_get("/api/time")
        for k in ("sunrise", "sunset", "solarNoon"):
            v = t["sunEvents"].get(k)
            if v:
                self.assertRegex(v, r"T\d\d:\d\d:00(\+00:00)?", k)
        self.assertNotIn("longitudeUsed", t.get("notes") or {})
        st, _, s = self.visitor_get("/api/satellites")
        for p in s["passes"]:
            for k in ("aos", "los", "tca"):
                self.assertEqual(p[k] % 60, 0, k)
            self.assertIsNone(p["start"])
            self.assertEqual(p["max_elev"], round(p["max_elev"]))

    def test_cells_are_at_least_10_km_and_snapping_is_deterministic(self):
        rnd = random.Random(3)
        for _ in range(400):
            lat, lon = rnd.uniform(-80, 80), rnd.uniform(-179.9, 179.9)
            c = visitor.coarse(lat, lon)
            self.assertEqual(c, visitor.coarse(lat, lon))
            step = visitor.lon_step(c[0])
            width = step * 111.195 * math.cos(math.radians(c[0]))
            self.assertGreaterEqual(width, 9.99, (lat, step))
            self.assertLessEqual(abs(c[0] - lat), 0.05 + 1e-9)
            d = abs((c[1] - lon + 180) % 360 - 180)
            self.assertLessEqual(d, step / 2 + 1e-9)
        self.assertIsNone(visitor.coarse(float("nan"), 0))
        self.assertIsNone(visitor.coarse(True, 0))

    # — aircraft: trilateration finds the cell centre —
    def test_trilateration_of_the_public_aircraft_list_finds_the_cell_centre(self):
        rnd = random.Random(11)
        for run in range(15):
            self.planes(rnd, rnd.choice([5, 40, 300]))
            st, _, reply = self.visitor_get("/api/aircraft")
            self.assertEqual(st, 200)
            self.assertEqual((reply["origin"]["lat"], reply["origin"]["lon"]), CELL)
            lat, lon = trilaterate.from_reply(reply)
            self.assertLess(km((lat, lon), CELL), 0.1, f"run {run}: recovered {lat:.5f}, {lon:.5f}, not the cell centre")
            self.assertGreater(km((lat, lon), HOME), km(CELL, HOME) - 0.1, f"run {run}: closer to the house than the cell")
        # nobody who asked was told who started a scan
        self.assertIsNone(reply["radio"]["by"])

    def test_planes_at_the_range_edge_are_kept_or_dropped_by_the_snapped_point(self):
        # a plane exactly at max_range_km from the REAL house would otherwise appear/disappear as a measurement
        edge = []
        for i, brg in enumerate(range(0, 360, 10)):
            d = 450.0
            lat, lon = offset(CELL[0], CELL[1], d * math.cos(math.radians(brg)) - 0.3, d * math.sin(math.radians(brg)) - 0.3)
            edge.append({"hex": f"{0xb00000 + i:06x}", "lat": lat, "lon": lon, "seen": 0.5, "seen_pos": 1.0})
        (SDR_TMP / "run" / "readsb" / "aircraft.json").write_text(json.dumps({"now": time.time(), "aircraft": edge}))
        kept = []
        for h in (HOME, (CELL[0] + 0.04, CELL[1] - 0.09), (CELL[0] - 0.04, CELL[1] + 0.09)):
            self.set_home(*h)
            kept.append(sorted(a["hex"] for a in self.visitor_get("/api/aircraft")[2]["aircraft"]))
        self.assertEqual(kept[0], kept[1])
        self.assertEqual(kept[0], kept[2])

    def test_family_and_admin_get_the_precise_view(self):
        member = self.join("Ben")
        st, a = self.req("GET", "/api/aircraft", member=member, headers=PHONE_HTTP)
        self.assertEqual((a["origin"]["lat"], a["origin"]["lon"]), HOME)
        lat, lon = trilaterate.from_reply(a)
        self.assertLess(km((lat, lon), HOME), 0.2)
        st, t = self.req("GET", "/api/time", member=member, headers=PHONE_HTTP)
        self.assertEqual((t["location"]["latitude"], t["location"]["longitude"]), HOME)
        st, s = self.req("GET", "/api/status", admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual(s["power"]["percent"], 41)
        self.assertEqual(s["gps"]["lat"], HOME[0])

    # — community board → radio needs a family member —
    def outbox(self) -> str:
        p = self.tmp / "mesh-outbox.jsonl"
        return p.read_text() if p.exists() else ""

    def test_visitor_mesh_post_waits_for_a_family_member(self):
        before = self.outbox()
        st, r = self.req("POST", "/api/board", {"text": "Water at the quarry 21:00", "name": "Badger", "mesh": True},
                         headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        self.assertIn("family member will decide", r["message"])
        self.assertEqual(self.outbox(), before, "nothing queued for the radio")
        msgs = self.req("GET", "/api/board", headers=PHONE_HTTP)[1]
        post = [m for m in msgs["messages"] if "quarry" in m["text"]][-1]
        self.assertEqual(post["mesh"], "pending")
        self.assertNotIn("pid", post)
        self.assertIsNone(msgs["mesh_pending"], "visitors don't see the approval list")
        self.assertTrue(msgs["mesh_needs_ok"])
        # a visitor can't approve (not even their own)
        member = self.join("Cleo")
        pend = self.req("GET", "/api/board", member=member, headers=PHONE_HTTP)[1]["mesh_pending"]
        pid = [p["id"] for p in pend if "quarry" in p["text"]][0]
        server._last_post.clear()
        st, r = self.req("POST", "/api/board/mesh", {"id": pid, "send": True}, headers=PHONE_HTTP)
        self.assertEqual(st, 401)
        self.assertEqual(self.outbox(), before)
        # a member sends it: now (and only now) it is queued, once
        st, r = self.req("POST", "/api/board/mesh", {"id": pid, "send": True}, member=member, headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        self.assertIn("Badger: Water at the quarry 21:00", self.outbox())
        st, r = self.req("POST", "/api/board/mesh", {"id": pid, "send": True}, member=member, headers=PHONE_HTTP)
        self.assertEqual(st, 404, "a request is sent at most once")
        post = [m for m in self.req("GET", "/api/board", headers=PHONE_HTTP)[1]["messages"] if "quarry" in m["text"]][-1]
        self.assertEqual((post["mesh"], post["via"]), ("sent", "mesh-out"))

    def test_declined_bad_and_admin_posts(self):
        server._last_post.clear()
        self.req("POST", "/api/board", {"text": "Come alone", "name": "x", "mesh": True}, headers=ROGUE)
        st, pend = self.req("GET", "/api/board", admin=self.admin, headers=PHONE_HTTP)
        pid = [p["id"] for p in pend["mesh_pending"] if p["text"] == "Come alone"][0]
        before = self.outbox()
        st, r = self.req("POST", "/api/board/mesh", {"id": pid, "send": False}, admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual(st, 200)
        self.assertEqual(self.outbox(), before)
        for bad in ({"id": pid}, {"id": "../../x", "send": True}, {"id": 5, "send": True}, {"id": pid, "send": "yes"}):
            self.assertEqual(self.req("POST", "/api/board/mesh", bad, admin=self.admin, headers=PHONE_HTTP)[0], 400, bad)
        server._last_post.clear()
        st, r = self.req("POST", "/api/board", {"text": "Admin note", "name": "Owner", "mesh": True},
                         admin=self.admin, headers=PHONE_HTTP)
        self.assertEqual(st, 200)
        self.assertIn("Owner: Admin note", self.outbox())

    def test_pending_requests_are_capped(self):
        for i in range(server.MESH_PENDING_MAX + 10):
            server._last_post.clear()
            self.req("POST", "/api/board", {"text": f"spam {i}", "name": "x", "mesh": True}, headers=ROGUE)
        pend = self.req("GET", "/api/board", admin=self.admin, headers=PHONE_HTTP)[1]["mesh_pending"]
        self.assertLessEqual(len(pend), server.MESH_PENDING_MAX)

    # — USB drives —
    def test_usb_is_family_only_and_stays_on_the_drive(self):
        mnt = self.tmp / "media" / "usb-sdz1"
        (mnt / "photos").mkdir(parents=True)
        (mnt / "photos" / "kids.jpg").write_bytes(b"\xff\xd8\xff photo")
        (mnt / "page.html").write_text("<script>steal()</script>")
        (mnt / "out").symlink_to("/etc")
        member = self.join("Dee")
        with mock.patch.object(server.os.path, "ismount", lambda p: str(p) == str(mnt)), \
                mock.patch.object(server, "on_usb_mount", lambda p, r: True):
            self.assertEqual(self.req("GET", "/api/usb", headers=PHONE_HTTP), (200, []))
            self.assertEqual(self.req("GET", "/api/usb/ls?drive=usb-sdz1", headers=PHONE_HTTP)[0], 401)
            self.assertEqual(self.req("GET", "/api/usb/file?drive=usb-sdz1&path=photos/kids.jpg", headers=PHONE_HTTP)[0], 401)
            st, lst = self.req("GET", "/api/usb", member=member, headers=PHONE_HTTP)
            self.assertEqual([d["drive"] for d in lst], ["usb-sdz1"])
            self.assertNotIn("url", lst[0])
            st, ls = self.req("GET", "/api/usb/ls?drive=usb-sdz1", member=member, headers=PHONE_HTTP)
            self.assertEqual(st, 200)
            self.assertEqual(sorted(e["name"] for e in ls["entries"]), ["page.html", "photos"], "link off the drive hidden")
            st, body, hdr = self.raw("GET", "/api/usb/file?drive=usb-sdz1&path=photos/kids.jpg", None,
                                     {**PHONE_HTTP, "X-Member-Token": member})
            self.assertEqual((st, body), (200, b"\xff\xd8\xff photo"))
            st, body, hdr = self.raw("GET", "/api/usb/file?drive=usb-sdz1&path=page.html", None,
                                     {**PHONE_HTTP, "X-Member-Token": member})
            self.assertEqual(hdr.get("Content-Security-Policy"), "sandbox")
            self.assertEqual(hdr.get("X-Content-Type-Options"), "nosniff")
            for bad in ("drive=usb-sdz1&path=../../prepper.toml", "drive=usb-sdz1&path=out/passwd", "drive=../&path=x",
                        "drive=usb-sdz1&path=/etc/passwd", "drive=nope&path=x"):
                self.assertEqual(self.req("GET", "/api/usb/file?" + bad, member=member, headers=PHONE_HTTP)[0], 404, bad)
            # behind nginx: an internal redirect, nginx sends the bytes
            server.CFG.standalone = False
            try:
                st, body, hdr = self.raw("GET", "/api/usb/file?drive=usb-sdz1&path=photos/kids.jpg", None,
                                         {"X-Member-Token": member, "X-Real-IP": "10.10.10.77", "X-Forwarded-Proto": "http"})
            finally:
                server.CFG.standalone = True
            self.assertEqual(hdr.get("X-Accel-Redirect"), "/_usb/usb-sdz1/photos/kids.jpg")
            self.assertEqual(body, b"")

    # — sensors: family-only by default, alerts for everyone —
    def test_sensor_readings_family_only_alerts_public(self):
        self.assertEqual(self.req("GET", "/api/sensors", headers=PHONE_HTTP)[0], 401)
        self.assertEqual(self.req("GET", "/api/sensors/history?key=a:temperature&range=24h", headers=PHONE_HTTP)[0], 401)
        self.assertEqual(self.req("GET", "/api/sensors/alerts", headers=PHONE_HTTP)[0], 200)
        member = self.join("Eve")
        self.assertEqual(self.req("GET", "/api/sensors", member=member, headers=PHONE_HTTP)[0], 200)
        server.CFG.raw["sensors"] = {"public": True}
        try:
            self.assertEqual(self.req("GET", "/api/sensors", headers=PHONE_HTTP)[0], 200)
        finally:
            server.CFG.raw.pop("sensors", None)

    # — errors —
    def test_500_carries_no_exception_text(self):
        with mock.patch.object(server, "library", side_effect=RuntimeError("/etc/prepper/secret path leaked")):
            st, body, _ = self.raw("GET", "/api/library", None, PHONE_HTTP)
        self.assertEqual(st, 500)
        self.assertNotIn(b"secret", body)
        self.assertNotIn(b"RuntimeError", body)
        self.assertNotIn("detail", json.loads(body))


ROGUE = {"X-Real-IP": "10.10.10.66", "X-Forwarded-Proto": "http"}


@unittest.skipUnless(harness.OPENSSL, "needs the openssl program")
class VaultTransport(PortalCase):
    """The vault over plain http on a hotspot without a password: refused, with a way forward."""
    FEATURES = ("vault.py", "_vault_crypto.py")
    PW = "purple tractor sings loudly"
    HTTPS = {"X-Real-IP": "10.10.10.77", "X-Forwarded-Proto": "https"}

    def setUp(self):
        super().setUp()
        server.CFG.raw["network"] = {"hostname": "badger", "passphrase": ""}

    def tearDown(self):
        server.CFG.raw.pop("network", None)

    def test_open_hotspot_http_refused_https_works(self):
        ana = self.join("Ana")
        for path, body in (("/api/vault/setup", {"passphrase": self.PW}), ("/api/vault/unlock", {"passphrase": self.PW}),
                           ("/api/vault/list", {}), ("/api/vault/note", {"text": "x"})):
            st, j = self.req("POST", path, body, member=ana, headers=PHONE_HTTP)
            self.assertEqual(st, 403, path)
            self.assertIn("https://badger.lan/#/vault", j["error"])
        st, j = self.req("GET", "/api/vault/status", member=ana, headers=PHONE_HTTP)
        self.assertEqual(st, 403)
        self.assertEqual(self.req("GET", "/api/vault/export/" + "a" * 30, headers=PHONE_HTTP)[0], 403)
        self.assertEqual(self.req("POST", "/api/vault/lock", {}, member=ana, headers=PHONE_HTTP)[0], 200, "locking always works")
        st, j = self.req("POST", "/api/vault/setup", {"passphrase": self.PW}, member=ana, headers=self.HTTPS)
        self.assertEqual(st, 200, j)
        st, j = self.req("GET", "/api/vault/status", member=ana, headers=self.HTTPS)
        self.assertNotIn("warning", j)
        # Enhanced Open counts as no password (anyone can stand up a look-alike hotspot)
        server.CFG.raw["network"] = {"passphrase": "", "security": "owe"}
        self.assertEqual(self.req("GET", "/api/vault/status", member=ana, headers=PHONE_HTTP)[0], 403)
        # WPA2: allowed over http, with a warning
        server.CFG.raw["network"] = {"passphrase": "correct horse battery"}
        st, j = self.req("GET", "/api/vault/status", member=ana, headers=PHONE_HTTP)
        self.assertEqual(st, 200)
        self.assertIn("https://", j["warning"])
        # open by name, https turned off: the message says what to do instead
        server.CFG.raw["network"] = {"passphrase": "", "security": "open", "https": False}
        st, j = self.req("GET", "/api/vault/status", member=ana, headers=PHONE_HTTP)
        self.assertEqual(st, 403)
        self.assertIn("Wi-Fi password", j["error"])


def tearDownModule():
    shutil.rmtree(SDR_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
