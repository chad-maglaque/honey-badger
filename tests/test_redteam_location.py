#!/usr/bin/env python3
"""
Red team — finding the household from the public aircraft page (adversary: a visitor on the
Wi-Fi, or anyone who once was). The Badger rounds its own position for visitors, but publishes
exact plane positions together with each plane's distance and bearing from the Badger.

Run: python3.11 tests/test_redteam_location.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="rt-air-")).resolve()
os.environ.update({
    "PREPPER_SDR_DIR": str(TMP / "run-sdr"), "PREPPER_SDR_STATE": str(TMP / "state"),
    "PREPPER_GPSD": "127.0.0.1:9", "PREPPER_CONFIG": str(TMP / "prepper.toml"), "PORTAL_QUIET": "1",
})
sys.path.insert(0, str(REPO / "software" / "sdr"))
sys.path.insert(0, str(REPO / "tests" / "redteam"))
import trilaterate  # noqa: E402

HOME = (39.739212, -104.990251)          # the family's house (precise)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


aircraft = load("rt_aircraft", REPO / "software" / "portal" / "features" / "aircraft.py")


class Ctx:
    def raw_config(self):
        return {"time": {"latitude": HOME[0], "longitude": HOME[1]}}


def offset(lat, lon, dn_km, de_km):
    import math
    return lat + dn_km / 111.195, lon + de_km / (111.195 * math.cos(math.radians(lat)))


class AircraftTrilateration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (TMP / "run-sdr" / "readsb").mkdir(parents=True)
        (TMP / "state").mkdir()
        planes = []
        for i, (dn, de) in enumerate(((40, 5), (-25, 30), (8, -45), (60, 60))):
            lat, lon = offset(HOME[0], HOME[1], dn, de)
            planes.append({"hex": f"a0000{i}", "flight": f"TST{i}", "alt_baro": 30000, "gs": 400, "track": 90,
                           "lat": lat, "lon": lon, "seen": 0.5, "seen_pos": 1.0})
        (TMP / "run-sdr" / "readsb" / "aircraft.json").write_text(json.dumps({"now": time.time(), "aircraft": planes}))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_visitor_recovers_exact_position_from_public_aircraft_list(self):
        # FINDING: GET /api/aircraft is "public" and returns per plane lat/lon (5 decimals) + dist_km (0.1 km)
        # + bearing (aircraft.py clean()). Three planes give the Badger's position to ~100 m — the house —
        # while /api/status deliberately rounds it to ~11 km for visitors. ("origin" itself is rounded to
        # 0.01°, ~1 km, also finer than /api/status.) tests/redteam/trilaterate.py is the attack.
        reply = aircraft.get_aircraft(None, Ctx(), {"ip": "10.10.10.66", "qs": {}, "data": None,
                                                    "member": None, "admin": False})
        self.assertGreaterEqual(reply["count"], 3, reply)
        lat, lon = trilaterate.from_reply(reply)
        err_km = trilaterate._haversine(lat, lon, *HOME)
        # (CORRECTED with the owner's approval: visitors get positions snapped to a fixed ~10 km grid, so the
        # best an attacker can recover is the cell centre. The house can be anywhere in its cell, so the bar is
        # "no closer than the cell centre", not a fixed 5 km, which no grid can guarantee for every house.)
        floor = trilaterate._haversine(round(HOME[0], 1), round(HOME[1], 1), *HOME) - 0.5
        self.assertGreater(err_km, floor, f"visitor located the house to {err_km * 1000:.0f} m "
                                        f"({lat:.5f}, {lon:.5f}) from the public aircraft page")

    def test_aircraft_origin_is_finer_than_status(self):
        # FINDING (low): origin is rounded to 0.01° (~1 km) here, 0.1° in /api/status, 0.01° on the
        # satellites page (sdrd.py schedule.json) — inconsistent coarsening; the finest one wins.
        reply = aircraft.snapshot(Ctx())
        o = reply["origin"]
        self.assertEqual((o["lat"], o["lon"]), (round(HOME[0], 1), round(HOME[1], 1)),
                         f"aircraft origin {o} is finer than the visitor position in /api/status")


if __name__ == "__main__":
    unittest.main()
