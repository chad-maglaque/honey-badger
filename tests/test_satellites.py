"""
Weather satellites: SGP4 against published reference vectors, pass prediction, TLE handling,
the recording policy (opt-in, high passes, daily cap, gap between loans), a full pass recorded
by sdrd with a fake SatDump while a pretend alertd lends its dongle, and the portal feature over
HTTP (public plan and pictures, admin-only orbit-data paste, traversal and bad input).
Run: python3.11 tests/test_satellites.py
"""
from __future__ import annotations

import http.client
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="sat-")).resolve()
os.environ.update({
    "PREPPER_SDR_DIR": str(TMP / "run-sdr"), "PREPPER_SDR_STATE": str(TMP / "state"),
    "ALERTS_FILE": str(TMP / "alerts.json"), "ALERTD_BEAT": str(TMP / "alertd.ok"),
    "PREPPER_USB_SYSFS": str(TMP / "sysfs"), "PREPPER_GPSD": "127.0.0.1:9", "PORTAL_QUIET": "1",
    "PREPPER_CONFIG": str(TMP / "prepper.toml"),
})
for sub in ("sdr", "common", "portal"):
    sys.path.insert(0, str(REPO / "software" / sub))

import orbit  # noqa: E402
import satplan  # noqa: E402
import sdrcommon as C  # noqa: E402
import sdrd  # noqa: E402
import sdrlease  # noqa: E402
import tlefile  # noqa: E402

L88_1 = "1 88888U          80275.98708465  .00073094  13844-3  66816-4 0    87"
L88_2 = "2 88888  72.8435 115.9689 0086731  52.6988 110.5714 16.05824518  1058"


def ck(line: str) -> str:
    return line + str(orbit.checksum(line))


def make_tle(norad=57166, epoch_unix=None, incl=98.7, raan=100.0, ecc=4000, argp=90.0, ma=270.0, mm=14.24):
    t = time.gmtime(epoch_unix if epoch_unix is not None else time.time())
    doy = t.tm_yday + (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec) / 86400.0
    ep = f"{t.tm_year % 100:02d}{doy:012.8f}"
    l1 = ck(f"1 {norad:05d}U 23091A   {ep}  .00000020  00000+0  28000-4 0  999")
    l2 = ck(f"2 {norad:05d} {incl:8.4f} {raan:8.4f} {ecc:07d} {argp:8.4f} {ma:8.4f} {mm:11.8f}12345")
    return l1, l2


def png(w=200, h=200, seed=1) -> bytes:
    import random
    rnd = random.Random(seed)
    raw = b"".join(b"\x00" + bytes(rnd.randrange(256) for _ in range(w)) for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg(w=320, h=240) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + b"\x00" * 4000 + b"\xff\xd9"


def write_cfg(extra: str = "") -> None:
    (TMP / "prepper.toml").write_text(f"""
[time]
latitude = 39.74
longitude = -104.99
{extra}
""")


# ───────────────────────────── orbit maths ───────────────────────────────────

class Orbit(unittest.TestCase):
    def test_vallado_reference_epoch(self):
        # Vallado et al. 2006, SGP4 verification output for 88888 at t = 0 (km, km/s)
        s = orbit.parse_tle(L88_1, L88_2)
        r, v = s.propagate(0.0)
        for got, want in zip(r, (2328.96975262, -5995.22051338, 1719.97297192), strict=True):
            self.assertAlmostEqual(got, want, delta=1e-6)
        for got, want in zip(v, (2.912073281, -0.983417956, -7.090816210), strict=True):
            self.assertAlmostEqual(got, want, delta=1e-8)

    def test_spacetrack_report_3_reference(self):
        # Spacetrack Report #3 (1980) SGP4 test case; later code differs by under a metre
        want = {360: ((2456.10705566, -6071.93853760, 1222.89727783), (2.67938992, -0.44829041, -7.22879231)),
                720: ((2567.56195068, -6112.50384522, 713.96397400), (2.44024599, 0.09810869, -7.31995916)),
                1080: ((2663.09078980, -6115.48229980, 196.39640427), (2.19611958, 0.65241995, -7.36282432)),
                1440: ((2742.55133057, -6079.67144775, -326.38095856), (1.94850229, 1.21106251, -7.35619372))}
        s = orbit.parse_tle(L88_1, L88_2)
        for t, (wr, wv) in want.items():
            r, v = s.propagate(t)
            for got, w in zip(r, wr, strict=True):
                self.assertAlmostEqual(got, w, delta=0.015, msg=f"t={t}")
            for got, w in zip(v, wv, strict=True):
                self.assertAlmostEqual(got, w, delta=2e-5, msg=f"t={t}")

    def test_orbit_plane_matches_elements(self):
        s = orbit.parse_tle(L88_1, L88_2)
        r, v = s.propagate(100.0)
        hx, hy, hz = r[1] * v[2] - r[2] * v[1], r[2] * v[0] - r[0] * v[2], r[0] * v[1] - r[1] * v[0]
        inc = math.degrees(math.acos(hz / math.sqrt(hx * hx + hy * hy + hz * hz)))
        self.assertAlmostEqual(inc, 72.84, delta=0.1)

    def test_sidereal_time_and_julian_date(self):
        self.assertAlmostEqual(orbit.jday(2000, 1, 1, 12, 0, 0), 2451545.0, places=9)
        self.assertAlmostEqual(math.degrees(orbit.gstime(2451545.0)), 280.46061837, places=6)
        self.assertAlmostEqual(orbit.jd_from_unix(946728000.0), 2451545.0, places=9)
        self.assertAlmostEqual(orbit.unix_from_jd(2451545.0), 946728000.0, places=3)

    def test_epoch_parsing(self):
        s = orbit.parse_tle(*make_tle(epoch_unix=1763208000.0))         # 2025-11-15 12:00 UTC
        self.assertAlmostEqual(s.epoch_unix, 1763208000.0, delta=0.01)
        self.assertEqual(time.gmtime(orbit.parse_tle(L88_1, L88_2).epoch_unix).tm_year, 1980)

    def test_meteor_like_orbit(self):
        s = orbit.parse_tle(*make_tle())
        self.assertAlmostEqual(s.period_min, 101.1, delta=0.3)
        for t in (0, 500, 1440, 4320):
            r, _ = s.propagate(t)
            alt = math.sqrt(sum(x * x for x in r)) - orbit.RE
            self.assertTrue(790 < alt < 840, alt)

    def test_overhead_at_the_subpoint(self):
        s = orbit.parse_tle(*make_tle())
        t = s.epoch_unix + 3000
        lat, lon, alt = orbit.subpoint(s, t)
        az, el, rng = orbit.look_angles(s, orbit.jd_from_unix(t), lat, lon, 0.0)
        self.assertGreater(el, 89.5)
        self.assertAlmostEqual(rng, alt, delta=2.0)
        _, el2, _ = orbit.look_angles(s, orbit.jd_from_unix(t), -lat, lon + 180 if lon < 0 else lon - 180, 0.0)
        self.assertLess(el2, -60)

    def test_azimuth_convention(self):
        # a satellite due north of the observer has azimuth ~0, due east ~90
        s = orbit.parse_tle(*make_tle())
        t = s.epoch_unix
        while abs(orbit.subpoint(s, t)[0]) > 15:                           # somewhere near the equator
            t += 30
        lat, lon, _ = orbit.subpoint(s, t)
        az_n, el_n, _ = orbit.look_angles(s, orbit.jd_from_unix(t), lat - 5, lon)
        az_e, _, _ = orbit.look_angles(s, orbit.jd_from_unix(t), lat, lon - 5)
        self.assertTrue(az_n < 3 or az_n > 357, az_n)
        self.assertAlmostEqual(az_e, 90, delta=5)
        self.assertGreater(el_n, 20)

    def test_passes_are_consistent(self):
        s = orbit.parse_tle(*make_tle(epoch_unix=1763208000.0))
        ps = orbit.passes(s, 39.74, -104.99, 1600, 1763208000.0, 24, min_elev=0)
        self.assertTrue(4 <= len(ps) <= 9, len(ps))                     # a polar orbiter at 40°N
        for p in ps:
            self.assertLess(p["aos"], p["tca"])
            self.assertLess(p["tca"], p["los"])
            self.assertLess(p["los"] - p["aos"], 17 * 60)
            for edge in ("aos", "los"):
                _, el, _ = orbit.look_angles(s, orbit.jd_from_unix(p[edge]), 39.74, -104.99, 1600)
                self.assertAlmostEqual(el, 0.0, delta=0.1)
            for dt in (-60, 60):                                           # tca really is the top
                _, el, _ = orbit.look_angles(s, orbit.jd_from_unix(p["tca"] + dt), 39.74, -104.99, 1600)
                self.assertLess(el, p["max_elev"])
        high = orbit.passes(s, 39.74, -104.99, 1600, 1763208000.0, 24, min_elev=30)
        self.assertEqual(high, [p for p in ps if p["max_elev"] >= 30])
        p = max(ps, key=lambda p: p["max_elev"])
        a, b = orbit.window_above(s, p, 39.74, -104.99, 1600, 10)
        self.assertTrue(p["aos"] < a < p["tca"] < b < p["los"])
        for t in (a, b):
            _, el, _ = orbit.look_angles(s, orbit.jd_from_unix(t), 39.74, -104.99, 1600)
            self.assertAlmostEqual(el, 10.0, delta=0.1)
        self.assertIsNone(orbit.window_above(s, dict(p, max_elev=5), 39.74, -104.99, 1600, 10))

    def test_bad_tles(self):
        l1, l2 = make_tle()
        bad = l1[:30] + ("1" if l1[30] != "1" else "2") + l1[31:]
        with self.assertRaisesRegex(orbit.TLEError, "checksum"):
            orbit.parse_tle(bad, l2)
        with self.assertRaisesRegex(orbit.TLEError, "different satellites"):
            orbit.parse_tle(l1, ck(l2[:2] + "12345" + l2[7:68]))
        with self.assertRaisesRegex(orbit.TLEError, "deep-space"):
            orbit.parse_tle(*make_tle(mm=1.0027))
        with self.assertRaises(orbit.TLEError):
            orbit.parse_tle("1 garbage", "2 garbage")
        with self.assertRaises(orbit.TLEError):
            orbit.parse_tle(*make_tle(incl=190.0))
        with self.assertRaises(orbit.TLEError):
            orbit.parse_tle(l1.replace("U", "\x00", 1), l2, check=False)
        with self.assertRaises(orbit.TLEError):
            orbit.parse_tle(*make_tle(mm=0.0))

    def test_parse_text_formats(self):
        a, b = make_tle(57166), make_tle(59051, raan=160.0)
        text = f"METEOR-M2 3\n{a[0]}\n{a[1]}\n\n0 METEOR-M2 4\r\n{b[0]}\r\n{b[1]}\r\nrubbish\n1 nope\n2 nope\n"
        sats, problems = orbit.parse_tle_text(text)
        self.assertEqual([(s.name, s.norad) for s in sats], [("METEOR-M2 3", 57166), ("METEOR-M2 4", 59051)])
        self.assertEqual(len(problems), 1)
        many = "\n".join(f"{x}\n{y}" for x, y in [make_tle(10000 + i) for i in range(30)])
        self.assertEqual(len(orbit.parse_tle_text(many, limit=5)[0]), 5)


# ───────────────────────────── orbit data files ──────────────────────────────

class TLEFiles(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP / "state", ignore_errors=True)
        self.cfg = C.satellites_list(None)

    def test_newest_epoch_wins_across_files(self):
        now = time.time()
        old, new = make_tle(57166, now - 20 * 86400), make_tle(57166, now - 86400)
        st = TMP / "state"
        st.mkdir(parents=True)
        (st / "tle.txt").write_text("X\n" + "\n".join(new) + "\n")
        (st / "tle-upload.txt").write_text("Y\n" + "\n".join(old) + "\n")
        sats = tlefile.load(self.cfg)
        self.assertEqual(sats[57166].name, "METEOR-M2 3")                   # the configured name
        self.assertAlmostEqual(sats[57166].epoch_unix, now - 86400, delta=1)
        rows = tlefile.info(self.cfg, 14, 60)
        self.assertEqual(rows[0]["age_days"], 1.0)
        self.assertFalse(rows[0]["approximate"])
        self.assertFalse(rows[1]["loaded"])

    def test_age_flags(self):
        now = time.time()
        tlefile.import_text("\n".join(make_tle(57166, now - 20 * 86400)) + "\n" + "\n".join(make_tle(59051, now - 90 * 86400)))
        rows = {r["norad"]: r for r in tlefile.info(self.cfg, 14, 60)}
        self.assertTrue(rows[57166]["approximate"])
        self.assertFalse(rows[57166]["too_old"])
        self.assertTrue(rows[59051]["too_old"])

    def test_import_rules(self):
        names, problems = tlefile.import_text("\n".join(make_tle(25544)), allowed={57166, 59051})
        self.assertEqual(names, [])
        self.assertIn("not in the satellite list", problems[0])
        names, problems = tlefile.import_text("\n".join(make_tle(57166, time.time() + 30 * 86400)), allowed={57166})
        self.assertIn("future", problems[0])
        self.assertEqual(tlefile.import_text("x" * 70000)[0], [])
        names, _ = tlefile.import_text("M3\n" + "\n".join(make_tle(57166)), allowed={57166})
        self.assertEqual(names, ["M3"])
        st = (TMP / "state" / "tle-upload.txt").stat()
        self.assertEqual(st.st_mode & 0o777, 0o644)

    def test_merge_keeps_other_satellites(self):
        tlefile.import_text("\n".join(make_tle(57166)))
        tlefile.import_text("\n".join(make_tle(59051)))
        self.assertEqual(set(tlefile.load(None)), {57166, 59051})

    def test_fetch_with_fake_server(self):
        now = time.time()
        answers = {57166: ("METEOR-M2 3\n" + "\n".join(make_tle(57166, now - 3600)) + "\n").encode(),
                   59051: b"No GP data found"}

        class Resp:
            def __init__(self, data): self.data = data
            def read(self, n): return self.data[:n]
            def __enter__(self): return self
            def __exit__(self, *a): return False
        seen = []

        def opener(req, timeout):
            seen.append((req.full_url, req.get_header("User-agent")))
            norad = int(req.full_url.split("CATNR=")[1].split("&")[0])
            return Resp(answers[norad])
        names, problems = tlefile.fetch(self.cfg, "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=TLE",
                                        opener=opener)
        self.assertEqual(names, ["METEOR-M2 3"])
        self.assertIn("METEOR-M2 4", problems[0])
        self.assertEqual(seen[0][1], tlefile.USER_AGENT)                   # nothing personal in requests
        self.assertIn(57166, tlefile.load(self.cfg))
        names, problems = tlefile.fetch(self.cfg, "http://example.invalid/{norad}", opener=opener)
        self.assertEqual(names, [])
        self.assertIn("https", problems[0])

        def broken(req, timeout):
            raise OSError("network is unreachable")
        names, problems = tlefile.fetch(self.cfg, "https://x/{norad}", opener=broken)
        self.assertEqual((names, len(problems)), ([], 2))
        self.assertIn(57166, tlefile.load(self.cfg))                       # a failed refresh keeps the old data

    def test_cli_fetch_offline_exits_zero(self):
        write_cfg('[satellites]\ntle_url = "https://127.0.0.1:9/{norad}"\n')
        self.assertEqual(tlefile.main(["fetch"]), 0)


# ───────────────────────────── policy ────────────────────────────────────────

def settings(extra=""):
    write_cfg(extra)
    return C.settings(C.load_config())


def fake_pass(name, norad, start, max_elev, dur=600):
    return {"name": name, "norad": norad, "freq_mhz": 137.9, "pipeline": "meteor_m2-x_lrpt", "aos": start - 120,
            "los": start + dur + 120, "tca": start + dur / 2, "max_elev": max_elev, "aos_az": 0, "los_az": 180,
            "tca_az": 90, "rec_start": start, "rec_end": start + dur}


class Policy(unittest.TestCase):
    def test_capture_modes(self):
        sdr2 = {"weather": {"present": True}, "sdr2": {"present": True}}
        mode = lambda extra, asg=sdr2, ok=None: satplan.capture_mode(settings(extra), asg, ok)  # noqa: E731
        # as shipped: the aircraft whip on SDR 2 → passes not recorded, and the page says what to fit
        self.assertIsNone(mode("")["device"])
        self.assertIn("V-dipole", mode("")["why"])
        self.assertEqual(mode('[radio]\nsdr2_antenna = "satellite"')["device"], "sdr2")
        self.assertEqual(mode('[radio]\nsdr2_antenna = "both"')["device"], "sdr2")
        self.assertIn("aircraft pause", mode('[radio]\nsdr2_antenna = "both"')["why"])
        self.assertEqual(mode("[satellites]\ncapture = true")["device"], "sdr2")                 # opted in with the whip
        self.assertIn("poorly", mode("[satellites]\ncapture = true")["why"])
        self.assertIsNone(mode('[satellites]\ncapture = false\n[radio]\nsdr2_antenna = "satellite"')["device"])
        self.assertIsNone(mode('[features]\nsatellites = false\n[radio]\nsdr2_antenna = "satellite"')["device"])
        # fallback: SDR 2 missing or failing → only with fallback_capture, on the weather radio
        gone = {"weather": {"present": True}, "sdr2": {"present": False}}
        self.assertIsNone(mode('[radio]\nsdr2_antenna = "satellite"', gone)["device"])
        self.assertIn("fallback_capture", mode("", gone)["why"])
        self.assertEqual(mode("[satellites]\nfallback_capture = true", gone)["device"], "weather")
        self.assertEqual(mode("[satellites]\nfallback_capture = true", sdr2, False)["device"], "weather")   # failed
        self.assertIsNone(mode("[satellites]\nfallback_capture = true\n[radio]\nsdr_lend = false", gone)["device"])
        self.assertIsNone(mode("[satellites]\nfallback_capture = true", {"weather": {"present": False}})["device"])

    def test_antenna_choice(self):
        shutil.rmtree(TMP / "state", ignore_errors=True)
        self.assertEqual(settings()["radio"]["sdr2_antenna"], "adsb")
        self.assertEqual(settings('[radio]\nsdr2_antenna = "Satellite"')["radio"]["sdr2_antenna"], "satellite")
        self.assertEqual(settings('[radio]\nsdr2_antenna = "yagi"')["radio"]["sdr2_antenna"], "adsb")
        self.assertTrue(C.set_antenna("both", "admin"))
        self.assertFalse(C.set_antenna("<script>"))
        self.assertEqual(settings('[radio]\nsdr2_antenna = "satellite"')["radio"]["sdr2_antenna"], "both")   # the launcher wins
        (TMP / "state" / "antenna.json").write_text('{"antenna": "rm -rf"}')
        self.assertEqual(settings()["radio"]["sdr2_antenna"], "adsb")
        shutil.rmtree(TMP / "state", ignore_errors=True)

    def rows(self, **kw):
        return [{"norad": 57166, "loaded": True, **kw}, {"norad": 59051, "loaded": True, **kw}]

    def test_shared_radio_rules(self):
        s = settings("[satellites]\nfallback_capture = true")
        now = time.time()
        t = time.localtime(now)
        base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 1, 0, 0, 0, 0, -1))   # all on tomorrow's date
        passes = [fake_pass("A", 57166, base, 25, 900),               # too low for the shared radio
                  fake_pass("B", 57166, base + 7200, 70, 900),
                  fake_pass("C", 59051, base + 7200 + 1200, 50, 900),  # within 10 min of B's end → B is higher
                  fake_pass("D", 59051, base + 20000, 40, 900),
                  fake_pass("E", 57166, base + 30000, 35, 900),       # 3 × 16 min > 45 min: over the cap
                  fake_pass("F", 57166, base + 40000, 33, 900)]
        mode = {"device": "weather", "why": ""}
        out = {p["name"]: p for p in satplan.plan(passes, s, mode, {}, now, self.rows())}
        self.assertEqual(out["A"]["decision"], "skip"); self.assertIn("Too low", out["A"]["reason"])
        self.assertEqual(out["B"]["decision"], "record")
        self.assertEqual(out["C"]["decision"], "skip"); self.assertIn("Too close", out["C"]["reason"])
        rec = [p for p in out.values() if p["decision"] == "record"]
        by_day = {}
        for p in rec:
            by_day[satplan.day_of(p["start"])] = by_day.get(satplan.day_of(p["start"]), 0) + p["end"] - p["start"]
        self.assertTrue(all(v <= 45 * 60 for v in by_day.values()), by_day)
        capped = [p for p in out.values() if "daily limit" in p["reason"]]
        self.assertTrue(capped)                                        # 4 × 11 min > 45 min on one day
        self.assertTrue(all(p["max_elev"] <= min(r["max_elev"] for r in rec) for p in capped if
                            satplan.day_of(p["rec_start"]) == satplan.day_of(rec[0]["start"])))
        for a in rec:
            for b in rec:
                if a is not b:
                    self.assertTrue(b["start"] >= a["end"] + 600 or a["start"] >= b["end"] + 600)

    def test_used_today_counts(self):
        s = settings("[satellites]\nfallback_capture = true")
        now = time.time()
        p = fake_pass("B", 57166, now + 3600, 70)
        today = {satplan.day_of(p["rec_start"]): {"sat_s": 40 * 60}}
        out = satplan.plan([p], s, {"device": "weather", "why": ""}, today, now, self.rows())
        self.assertIn("daily limit", out[0]["reason"])

    def test_gap_after_last_loan_and_busy(self):
        s = settings("[satellites]\nfallback_capture = true")
        now = time.time()
        p = fake_pass("B", 57166, now + 300, 70)
        out = satplan.plan([p], s, {"device": "weather", "why": ""}, {}, now, self.rows(), last_lend_end=now - 60)
        self.assertEqual(out[0]["decision"], "skip")
        out = satplan.plan([p], s, {"device": "weather", "why": ""}, {}, now, self.rows(), busy=[(now, now + 900)])
        self.assertEqual(out[0]["decision"], "skip")
        out = satplan.plan([p], s, {"device": "weather", "why": ""}, {}, now, self.rows())
        self.assertEqual(out[0]["decision"], "record")

    def test_dedicated_radio_records_everything_reasonable(self):
        s = settings()
        now = time.time()
        passes = [fake_pass(str(i), 57166, now + 3600 + i * 6000, 20 + i) for i in range(8)]
        out = satplan.plan(passes, s, {"device": "sdr2", "why": ""}, {}, now, self.rows())
        self.assertTrue(all(p["decision"] == "record" for p in out))
        low = satplan.plan([fake_pass("L", 57166, now + 3600, 12)], s, {"device": "sdr2", "why": ""}, {}, now, self.rows())
        self.assertEqual(low[0]["decision"], "skip")

    def test_old_orbit_data_and_padding(self):
        s = settings("[satellites]\nfallback_capture = true")
        now = time.time()
        p = fake_pass("B", 57166, now + 3600, 70)
        out = satplan.plan([p], s, {"device": "weather", "why": ""}, {}, now, self.rows(too_old=True))
        self.assertIn("too old", out[0]["reason"])
        out = satplan.plan([p], s, {"device": "weather", "why": ""}, {}, now, self.rows(approximate=True))
        self.assertEqual(out[0]["decision"], "record")
        self.assertAlmostEqual(p["rec_start"] - out[0]["start"], satplan.PAD_S + satplan.STALE_PAD_S, delta=0.2)
        self.assertIn("approximate", out[0]["reason"])

    def test_done_and_past_passes(self):
        s = settings("[satellites]\nfallback_capture = true")
        now = time.time()
        p = fake_pass("B", 57166, now + 3600, 70)
        past = fake_pass("P", 57166, now - 7200, 70)
        out = satplan.plan([past, p], s, {"device": "weather", "why": ""}, {}, now, self.rows(),
                           done=[{"norad": 57166, "tca": p["tca"] + 30, "label": "Recorded."}])
        self.assertEqual([(e["name"], e["decision"], e["reason"]) for e in out], [("B", "done", "Recorded.")])

    def test_settings_validation(self):
        s = settings('[satellites]\nmin_elevation = "high"\ndaily_max_min = 1e9\ncapture = "yes"\nkeep_images = -5\n'
                     'list = [{name = "<b>X</b>", norad = 57166, pipeline = "rm -rf /"}, {norad = "nope"}, {norad = 59051}]')
        sat = s["satellites"]
        self.assertEqual(sat["min_elevation"], 30)
        self.assertEqual(sat["daily_max_min"], 600)
        self.assertEqual(sat["capture"], "auto")
        self.assertEqual(sat["keep_images"], 1)
        self.assertEqual([x["norad"] for x in sat["list"]], [59051])       # a pipeline with spaces/slashes is refused
        self.assertEqual(C.settings({})["satellites"]["list"][0]["norad"], 57166)


# ───────────────────────────── recording a pass ──────────────────────────────

FAKE_SATDUMP = '''#!{py}
import json, os, sys, time
args = sys.argv[1:]
open({calls!r}, "a").write(json.dumps(args) + "\\n")
out = args[2]
timeout = int(args[args.index("--timeout") + 1])
end = time.time() + min(timeout, 3)
while time.time() < end:
    time.sleep(0.1)
os.makedirs(os.path.join(out, "MSU-MR"), exist_ok=True)
if os.environ.get("FAKE_NO_IMAGE") != "1":
    open(os.path.join(out, "MSU-MR", "msu_mr_rgb_221_corrected.png"), "wb").write(open({png!r}, "rb").read())
    open(os.path.join(out, "MSU-MR", "MSU-MR-1.png"), "wb").write(open({big!r}, "rb").read())
    open(os.path.join(out, "MSU-MR", "notes.txt"), "w").write("x")
'''


class Owner:
    """A pretend alertd: holds the weather dongle and yields when asked (see tests/test_framework.py)."""

    def __init__(self):
        self.stop = threading.Event()
        self.log = []
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        while not self.stop.is_set():
            with sdrlease.hold("weather", poll=0.05):
                self.log.append("hold")
                while not self.stop.is_set():
                    if sdrlease.owner_should_yield("weather"):
                        self.log.append("yield")
                        break
                    time.sleep(0.05)

    def close(self):
        self.stop.set()
        self.t.join(3)


class RecordPass(unittest.TestCase):
    def setUp(self):
        for d in ("run-sdr", "state", "sysfs"):
            shutil.rmtree(TMP / d, ignore_errors=True)
        (TMP / "bin").mkdir(exist_ok=True)
        (TMP / "a.png").write_bytes(png(200, 300, 1))
        (TMP / "b.png").write_bytes(png(300, 300, 2))
        self.calls = TMP / "satdump.calls"
        self.calls.unlink(missing_ok=True)
        fake = TMP / "bin" / "satdump"
        fake.write_text(FAKE_SATDUMP.format(py=sys.executable, calls=str(self.calls), png=str(TMP / "a.png"), big=str(TMP / "b.png")))
        fake.chmod(0o755)
        write_cfg(f'[satellites]\nfallback_capture = true\nsatdump = "{fake}"\n[radio]\nweather_sdr = "00000162"\nsdr2 = "none"\n')
        importlib_reload()
        (TMP / "alertd.ok").touch()
        self.owner = Owner()

    def tearDown(self):
        self.owner.close()
        os.environ.pop("FAKE_NO_IMAGE", None)

    def run_pass(self, d, dur=3):
        now = time.time()
        p = fake_pass("METEOR-M2 3", 57166, now, 60, dur)
        p.update(decision="record", device="weather", start=now, end=now + 100, reason="")   # fake SatDump stops after 3 s
        d.plan = [p]
        d.start_due()
        job = d.jobs["weather"]
        self.assertIsNotNone(job)
        return p, job

    def test_records_a_pass_on_the_borrowed_weather_radio(self):
        d = sdrd.Daemon(borrow_wait=5)
        d.refresh(force=True)
        p, job = self.run_pass(d)
        seen_paused = False
        end = time.time() + 15
        while job.is_alive() and time.time() < end:
            st = C.read_status()
            if st.get("weather_radio") == "paused":
                seen_paused = True
                self.assertEqual(st["by"], "satellite pass")
                self.assertGreater(st["until"], time.time())
            time.sleep(0.1)
        self.assertFalse(job.is_alive())
        self.assertTrue(seen_paused)
        self.assertIn("yield", self.owner.log)
        time.sleep(0.3)
        self.assertEqual(self.owner.log.count("hold"), 2)                  # alertd got its dongle back
        self.assertEqual(C.read_status().get("weather_radio"), "listening")
        argv = json.loads(self.calls.read_text().splitlines()[0])
        self.assertEqual(argv[:3], ["live", "meteor_m2-x_lrpt", argv[2]])
        self.assertEqual(argv[argv.index("--frequency") + 1], "137900000")
        self.assertEqual(argv[argv.index("--source_id") + 1], "00000162")
        idx = d.gallery.index()
        self.assertEqual(len(idx), 1)
        self.assertEqual((idx[0]["ext"], idx[0]["width"], idx[0]["height"]), ("png", 200, 300))   # the "221" picture won
        self.assertEqual((TMP / "state" / "images" / idx[0]["file"]).stat().st_mode & 0o777, 0o640)
        self.assertFalse((TMP / "state" / "work").exists() and any((TMP / "state" / "work").iterdir()))
        self.assertGreater(d.ledger.day().get("sat_s", 0), 0)
        self.assertIsNotNone(d.ledger.last_lend_end)
        self.assertEqual(d.ledger.history()[-1]["result"], "image")
        d.replan()
        sched = json.loads((TMP / "run-sdr" / "schedule.json").read_text())
        self.assertEqual(sched["mode"]["device"], "weather")

    def test_no_picture_is_reported(self):
        os.environ["FAKE_NO_IMAGE"] = "1"
        d = sdrd.Daemon(borrow_wait=5)
        d.refresh(force=True)
        _, job = self.run_pass(d, 2)
        job.join(15)
        self.assertEqual(d.gallery.index(), [])
        self.assertEqual(d.ledger.history()[-1]["result"], "no image")

    def test_skipped_while_an_alert_is_live(self):
        (TMP / "alerts.json").write_text(json.dumps([{"event": "Tornado Warning", "expires_ts": time.time() + 600}]))
        try:
            d = sdrd.Daemon(borrow_wait=5)
            d.refresh(force=True)
            now = time.time()
            p = fake_pass("METEOR-M2 3", 57166, now, 60, 200)
            p.update(decision="record", device="weather", start=now, end=now + 200, reason="")
            d.plan = [p]
            d.start_due()
            self.assertIsNone(d.jobs["weather"])
            self.assertEqual(p["decision"], "skip")
            self.assertIn("alert", p["reason"])
            self.assertEqual(self.owner.log.count("yield"), 0)
        finally:
            (TMP / "alerts.json").unlink()

    def test_refused_by_alertd_is_a_skip(self):
        self.owner.close()
        refuser = threading.Event()

        def run():
            with sdrlease.hold("weather", poll=0.05):
                while not refuser.is_set():
                    if sdrlease.owner_should_yield("weather"):
                        sdrlease.refuse("weather", "the weather radio was lent a moment ago")
                    time.sleep(0.05)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        try:
            d = sdrd.Daemon(borrow_wait=5)
            d.refresh(force=True)
            _, job = self.run_pass(d)
            job.join(10)
            self.assertEqual(d.ledger.history()[-1]["result"], "skipped")
            self.assertIn("moment ago", d.ledger.history()[-1]["detail"])
            self.assertIsNone(d.ledger.last_lend_end)
        finally:
            refuser.set()
            t.join(3)
        self.owner = Owner()

    def plan_with(self, cfg, serials):
        root = TMP / "sysfs"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir()
        for i, sn in enumerate(serials):
            d = root / f"1-1.{i + 1}"
            d.mkdir()
            (d / "idVendor").write_text("0bda"); (d / "idProduct").write_text("2838"); (d / "serial").write_text(sn)
        write_cfg(cfg)
        tlefile.import_text("\n".join(make_tle(57166, time.time() - 3600)) + "\n"
                            + "\n".join(make_tle(59051, time.time() - 7200, raan=150.0, ma=10.0)))
        C._pos_cache.clear()
        d = sdrd.Daemon(borrow_wait=5)
        d.refresh(force=True)
        return d, json.loads((TMP / "run-sdr" / "schedule.json").read_text())

    def test_real_plan_on_sdr2(self):
        d, sched = self.plan_with('[radio]\nsdr2_antenna = "satellite"\n', ["BADGER-SDR1", "BADGER-SDR2"])
        self.assertEqual(sched["mode"]["device"], "sdr2")
        self.assertEqual(sched["position"], {"lat": 39.74, "lon": -104.99, "source": "settings"})
        self.assertGreaterEqual(len(sched["passes"]), 6)                  # two polar orbiters, 24 h
        rec = [p for p in sched["passes"] if p["decision"] == "record"]
        self.assertTrue(rec)
        for p in sched["passes"]:
            self.assertEqual(p["decision"] == "record", p["max_elev"] >= 15 and not any(
                q is not p and q["decision"] == "record" and q["max_elev"] > p["max_elev"]
                and q["start"] < p["end"] + 60 and p["start"] < q["end"] + 60 for q in rec), p)

    def test_real_plan_as_shipped_and_in_fallback(self):
        d, sched = self.plan_with("", ["BADGER-SDR1", "BADGER-SDR2"])     # aircraft whip: times only
        self.assertIsNone(sched["mode"]["device"])
        self.assertTrue(all(p["decision"] == "off" for p in sched["passes"]))
        d, sched = self.plan_with("[satellites]\nfallback_capture = true\n", ["BADGER-SDR1"])   # SDR 2 gone
        self.assertEqual(sched["mode"]["device"], "weather")
        rec = [p for p in sched["passes"] if p["decision"] == "record"]
        self.assertTrue(all(p["max_elev"] >= 30 for p in rec))
        by_day = {}
        for p in rec:
            by_day[satplan.day_of(p["start"])] = by_day.get(satplan.day_of(p["start"]), 0) + p["end"] - p["start"]
        self.assertTrue(all(v <= 45 * 60 for v in by_day.values()))

    def test_gallery_keeps_newest_n(self):
        g = sdrd.Gallery(TMP / "state" / "images")
        f = TMP / "a.png"
        for i in range(5):
            g.add(f, ("png", 200, 300), {"name": "M", "norad": 57166, "aos": time.time() + i}, keep=3)
        idx = g.index()
        self.assertEqual(len(idx), 3)
        self.assertEqual(len(list((TMP / "state" / "images").glob("*.png"))), 3)

    def test_image_detection(self):
        d = TMP / "imgs"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
        (d / "fake.png").write_bytes(b"<svg onload=alert(1)>" + b" " * 5000)       # wrong magic
        (d / "tiny.png").write_bytes(png(10, 10))                                    # too small
        (d / "photo.jpg").write_bytes(jpeg())
        (d / "rgb_map.png").write_bytes(png(200, 400, 3))
        os.symlink("/etc/passwd", d / "link.png")
        self.assertEqual(sdrd.image_info(d / "photo.jpg"), ("jpg", 320, 240))
        self.assertIsNone(sdrd.image_info(d / "fake.png"))
        got = sdrd.pick_image(d, ["221", "rgb", "corrected"])
        self.assertEqual(got[0].name, "rgb_map.png")                       # "rgb" beats the plain jpg despite "map"
        (d / "rgb_map.png").unlink()
        self.assertEqual(sdrd.pick_image(d, ["221"])[0].name, "photo.jpg")


def importlib_reload():
    import importlib
    importlib.reload(sdrlease)
    sdrd.sdrlease = sdrlease


# ───────────────────────────── the portal feature ────────────────────────────

class Portal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        feats = TMP / "features"
        feats.mkdir(exist_ok=True)
        for f in ("satellites.py", "aircraft.py"):
            (feats / f).unlink(missing_ok=True)
            os.symlink(REPO / "software" / "portal" / "features" / f, feats / f)
        (TMP / "data").mkdir(exist_ok=True)
        cfg = TMP / "portal.toml"
        cfg.write_text(f"""
[upd]
data_root = "{TMP / 'data'}"
catalog = "{REPO}/content/catalog.toml"
state_file = "{TMP}/upd-state.json"
report_dir = "{TMP}/reports"

[portal]
standalone = true
static = "{REPO}/software/portal/static"
renderers = "{REPO}/content/renderers.toml"
admin_pin = "{server.hash_pin('246810')}"
board_file = "{TMP}/board.jsonl"
family_board_file = "{TMP}/family-board.jsonl"
members_file = "{TMP}/members.json"
settings_file = "{TMP}/portal-settings.json"
mesh_outbox = "{TMP}/mesh-outbox.jsonl"
features_data = "{TMP}/features-data"
ctl = "/nonexistent/prepperctl"
services = {{}}

[satellites]
tle_url = "https://127.0.0.1:9/{{norad}}"

[time]
latitude = 39.74
longitude = -104.99
""")
        server.CFG = server.PortalConfig(cfg)
        server.reset_login_limits()
        server.load_features(feats)
        assert {f["id"] for f in server.FEATURES} == {"aircraft", "satellites"}, server.FEATURES
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = cls.req("POST", "/api/admin/login", {"pin": "246810"})[1]["token"]
        inv = cls.req("POST", "/api/admin/members", {"op": "invite", "name": "Ana"}, admin=cls.admin)[1]
        server._last_post.clear()
        cls.member = cls.req("POST", "/api/members/join", {"code": inv["code"], "name": "Ana"})[1]["key"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    @classmethod
    def req(cls, method, path, body=None, member=None, admin=None, headers=None, raw=None, ip=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if ip:
            h["X-Real-IP"] = ip
        if member:
            h["X-Member-Token"] = member
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

    def setUp(self):
        self.server._last_post.clear()
        shutil.rmtree(TMP / "state", ignore_errors=True)
        shutil.rmtree(TMP / "run-sdr", ignore_errors=True)
        write_cfg()

    def test_overview_is_public_and_validated(self):
        (TMP / "run-sdr").mkdir(parents=True)
        p = fake_pass("<img src=x onerror=alert(1)>", 57166, time.time() + 3600, 60)
        p.update(decision="record", reason="Will record", start=p["rec_start"], end=p["rec_end"])
        (TMP / "run-sdr" / "schedule.json").write_text(json.dumps({
            "generated": time.time(), "mode": {"device": "weather", "why": "High passes…"},
            "position": {"lat": 39.74, "lon": -104.99, "source": "settings"},
            "passes": [p, {"aos": "x"}, "junk", {"aos": 1, "los": 2, "max_elev": float("nan")}]}))
        st, j, _ = self.req("GET", "/api/satellites")
        self.assertEqual(st, 200)
        self.assertEqual(len(j["passes"]), 2)
        self.assertEqual(j["passes"][0]["decision"], "record")
        self.assertIsNone(j["passes"][1]["max_elev"])
        self.assertFalse(j["running"])
        self.assertEqual([r["norad"] for r in j["tle"]], [57166, 59051])
        self.assertEqual(j["mode"], "High passes…")

    def test_garbage_schedule_does_not_break_it(self):
        (TMP / "run-sdr").mkdir(parents=True)
        for junk in ("[1,2]", "{", '{"passes": "abc", "mode": 5, "position": []}', "null"):
            (TMP / "run-sdr" / "schedule.json").write_text(junk)
            st, j, _ = self.req("GET", "/api/satellites", ip=f"10.1.0.{len(junk)}")
            self.assertEqual(st, 200, junk)
            self.assertEqual(j["passes"], [])

    def add_image(self, data=None, ext="png"):
        g = sdrd.Gallery(TMP / "state" / "images")
        src = TMP / f"src.{ext}"
        src.write_bytes(data or png())
        return g.add(src, (ext, 200, 200), {"name": "METEOR-M2 3", "norad": 57166, "aos": time.time(), "max_elev": 61.2}, keep=10)

    def test_image_serving(self):
        e = self.add_image()
        st, j, _ = self.req("GET", "/api/satellites")
        self.assertEqual(j["images"][0]["id"], e["id"])
        st, body, h = self.req("GET", "/api/satellites/image/" + e["id"])
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        for bad in ("../index.json", "..%2Findex.json", "x" * 80, "", "index", e["id"] + ".png", "%00"):
            self.assertEqual(self.req("GET", "/api/satellites/image/" + bad)[0], 404, bad)

    def test_tampered_image_is_not_served(self):
        e = self.add_image()
        (TMP / "state" / "images" / e["file"]).write_bytes(b"<html><script>alert(1)</script>" + b" " * 3000)
        self.assertEqual(self.req("GET", "/api/satellites/image/" + e["id"])[0], 404)
        idx = json.loads((TMP / "state" / "images" / "index.json").read_text())
        idx.append({"id": "evil", "file": "../../etc/passwd", "ext": "svg"})
        (TMP / "state" / "images" / "index.json").write_text(json.dumps(idx))
        self.assertEqual(self.req("GET", "/api/satellites/image/evil")[0], 404)

    def test_tle_paste_is_admin_only_and_validated(self):
        text = "METEOR-M2 3\n" + "\n".join(make_tle(57166)) + "\n"
        self.assertEqual(self.req("POST", "/api/satellites/tle", {"text": text})[0], 401)
        self.assertEqual(self.req("POST", "/api/satellites/tle", {"text": text}, member=self.member)[0], 401)
        st, j, _ = self.req("POST", "/api/satellites/tle", {"text": text}, admin=self.admin)
        self.assertEqual((st, j["loaded"]), (200, ["METEOR-M2 3"]))
        self.assertTrue((TMP / "state" / "tle-upload.txt").exists())
        for bad, code in (({"text": 5}, 400), ({"text": ""}, 400), ({}, 400), ({"text": "x" * 20001}, 413),
                          ({"text": "\n".join(make_tle(25544))}, 400), ({"text": "hello\nworld"}, 400)):
            self.assertEqual(self.req("POST", "/api/satellites/tle", bad, admin=self.admin)[0], code, bad)
        st, j, _ = self.req("GET", "/api/satellites")
        self.assertTrue(j["tle"][0]["loaded"])

    def test_refresh_is_admin_only(self):
        self.assertEqual(self.req("POST", "/api/satellites/tle/refresh", {}, member=self.member)[0], 401)
        self.assertEqual(self.req("POST", "/api/satellites/tle/refresh", {}, admin=self.admin)[0], 202)
        end = time.time() + 10
        while time.time() < end:
            st, j, _ = self.req("GET", "/api/satellites/tle/refresh", admin=self.admin)
            if not j["running"] and j["result"]:
                break
            time.sleep(0.1)
        self.assertEqual(j["result"]["loaded"], [])                         # offline in the test: no crash

    def test_rate_limit(self):
        codes = [self.req("GET", "/api/satellites", ip="10.9.9.9")[0] for _ in range(65)]
        self.assertIn(429, codes)
        self.assertEqual(self.req("GET", "/api/satellites", ip="10.9.9.8")[0], 200)

    def test_antenna_route_is_admin_only(self):
        self.assertEqual(self.req("POST", "/api/satellites/antenna", {"antenna": "satellite"}, member=self.member)[0], 401)
        self.assertEqual(self.req("POST", "/api/aircraft/antenna", {"antenna": "satellite"})[0], 401)
        for bad in ({"antenna": "yagi"}, {"antenna": ["adsb"]}, {}):
            self.assertEqual(self.req("POST", "/api/satellites/antenna", bad, admin=self.admin)[0], 400)
        st, j, _ = self.req("POST", "/api/satellites/antenna", {"antenna": "satellite"}, admin=self.admin)
        self.assertEqual((st, j["antenna"]), (200, "satellite"))
        self.assertEqual(C.antenna(), "satellite")
        st, j, _ = self.req("GET", "/api/satellites", ip="10.4.4.4")
        self.assertEqual((j["antenna"], j["sdr2_state"]), ("satellite", None))       # scheduler not running
        self.assertEqual(self.req("POST", "/api/aircraft/antenna", {"antenna": "adsb"}, admin=self.admin)[1]["antenna"], "adsb")

    def test_board_post_on_new_picture(self):
        mod = next(f["mod"] for f in self.server.FEATURES if f["id"] == "satellites")
        posts = []
        ctx = types.SimpleNamespace(raw_config=lambda: C.load_config(), atomic_write=C.atomic_write, log=lambda m: None,
                                    board_post=lambda *a: posts.append(a) or (True, "posted"))
        posted = TMP / "posted.json"
        posted.unlink(missing_ok=True)
        self.add_image()
        write_cfg("[satellites]\nboard_post = true")
        mod._post_new(ctx, posted)                                         # first run: remember only
        self.assertEqual(posts, [])
        time.sleep(1.1)
        self.add_image()
        mod._post_new(ctx, posted)
        self.assertEqual(len(posts), 1)
        self.assertIn("New weather satellite image", posts[0][2])
        self.assertEqual((posts[0][3], posts[0][4]), (False, "community"))    # never over the mesh
        mod._post_new(ctx, posted)
        self.assertEqual(len(posts), 1)
        write_cfg()                                                        # default: off
        time.sleep(1.1)
        self.add_image()
        mod._post_new(ctx, posted)
        self.assertEqual(len(posts), 1)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
