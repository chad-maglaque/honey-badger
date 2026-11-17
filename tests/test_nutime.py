#!/usr/bin/env python3
"""Ke-Shi / Nu:Time™ acceptance tests (handoff §11).   python3 tests/test_nutime.py -v"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "software" / "nutime"))
import nutime as nt  # noqa: E402

UTC = dt.timezone.utc


def at(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


class Conversion(unittest.TestCase):
    def test_reference_table(self):
        for hms, beats, text in [("00:00:00", 0, "00:00:00"), ("06:00:00", 250, "02:50:00"),
                                 ("07:12:00", 300, "03:00:00"), ("12:00:00", 500, "05:00:00"),
                                 ("18:00:00", 750, "07:50:00")]:
            h, m, s = map(int, hms.split(":"))
            r = nt.from_seconds(h * 3600 + m * 60 + s)
            self.assertEqual(r["display"], text, hms)
            self.assertEqual(r["beatsOfDay"], beats, hms)

    def test_examples_from_handoff(self):
        self.assertEqual(nt.from_seconds(7 * 3600 + 13 * 60)["display"], "03:00:69")
        self.assertEqual(nt.from_seconds(86399)["display"], "09:99:98")              # 23:59:59
        self.assertEqual(nt.from_seconds(86399.135)["display"], "09:99:98")
        self.assertEqual(nt.from_seconds(86399.136)["display"], "09:99:99")          # last tick starts here

    def test_truncates_never_rounds_ahead(self):
        self.assertEqual(nt.from_seconds(0.863999)["tick"], 0)
        self.assertEqual(nt.from_seconds(0.864)["tick"], 1)
        self.assertEqual(nt.from_seconds(86.399999)["display"], "00:00:99")
        self.assertEqual(nt.from_seconds(86.4)["display"], "00:01:00")

    def test_full_day_has_exactly_100000_ticks_and_1000_beats(self):
        seen = {nt.ticks_from_us(i * 864_000) for i in range(100_000)}              # every tick start
        self.assertEqual(len(seen), 100_000)
        self.assertEqual(min(seen), 0); self.assertEqual(max(seen), 99_999)
        beats = {t // 100 for t in seen}
        self.assertEqual(len(beats), 1000)

    def test_wraparound(self):
        self.assertEqual(nt.from_seconds(86400)["display"], "00:00:00")
        self.assertEqual(nt.from_seconds(-0.5)["display"], nt.from_seconds(86399.5)["display"])

    def test_inverse(self):
        for d in ("00:00:00", "05:00:00", "09:99:99", "03:00:69"):
            r = nt.parse(d)
            self.assertEqual(nt.from_seconds(r["secondsOfDay"])["display"], d)
        with self.assertRaises(ValueError):
            nt.parse("10:00:00")

    def test_invariants(self):
        for us in range(0, nt.NOMINAL_DAY_US, 7_777_777_7):
            r = nt.fields_from_ticks(nt.ticks_from_us(us))
            self.assertTrue(0 <= r["gong"] <= 9 and 0 <= r["beat"] <= 99 and 0 <= r["tick"] <= 99)
            self.assertEqual(r["ticksOfDay"], r["gong"] * 10000 + r["beat"] * 100 + r["tick"])
            self.assertTrue(0 <= r["beatsOfDay"] < 1000)


class Localization(unittest.TestCase):
    def test_labels_change_names_not_numbers(self):
        i = at("2026-11-16T12:00:00Z")
        recs = {loc: nt.nu_time(i, locale=loc, with_sun=False) for loc in nt.LABELS}
        self.assertEqual({r["display"] for r in recs.values()}, {"05:00:00"})
        self.assertEqual((recs["zh"]["labels"]["gong"], recs["zh"]["labels"]["beat"], recs["zh"]["labels"]["tick"]), ("更", "分", "刻"))
        self.assertEqual(recs["fr"]["labels"]["tick"], "tic")
        self.assertEqual(recs["es"]["labels"]["tick"], "tic")
        self.assertEqual(recs["de"]["labels"]["tick"], "tick")


class Geography(unittest.TestCase):
    HEL = dict(lat=60.1699, lon=24.9384, tz="Europe/Helsinki")

    def test_schema_example(self):
        r = nt.nu_time(at("2026-11-16T12:00:00Z"), "utc", **self.HEL)
        self.assertEqual((r["schema"], r["reference"], r["display"], r["ticksOfDay"]), ("nu-time/v1", "UTC", "05:00:00", 50000))
        self.assertEqual(r["location"]["ianaTimeZone"], "Europe/Helsinki")   # location kept separate from reference

    def test_same_instant_three_references(self):
        i = at("2026-11-16T12:00:00Z")                                       # Helsinki is UTC+2 in November
        self.assertEqual(nt.nu_time(i, "utc", **self.HEL)["display"], "05:00:00")
        self.assertEqual(nt.nu_time(i, "civil", **self.HEL)["display"], "05:83:33")   # 14:00 local
        solar = nt.nu_time(i, "solar", **self.HEL)                           # +24.9384°/15 h = +1 h 39 m 45 s
        self.assertEqual(solar["display"], nt.from_seconds(12 * 3600 + 24.9384 / 15 * 3600)["display"])

    def test_legacy_longitude_quantization(self):
        i = at("2026-11-16T00:00:00Z")
        q = nt.nu_time(i, "solar", lon=24.9384, quantize_longitude=True, with_sun=False)
        self.assertAlmostEqual(q["notes"]["longitudeUsed"], 25.2)            # nearest 3.6° = 1/100 day
        self.assertEqual(q["display"], "00:70:00")                          # 7/100 day = 70 beats exactly

    def test_dst_spring_forward(self):
        # Helsinki 2026-03-29: 03:00 → 04:00 local (01:00 UTC). 23-hour civil day.
        before, after = at("2026-03-29T00:59:59Z"), at("2026-03-29T01:00:00Z")
        wall_b = nt.nu_time(before, "civil", **self.HEL, with_sun=False)
        wall_a = nt.nu_time(after, "civil", **self.HEL, with_sun=False)
        self.assertEqual(wall_b["display"], nt.from_seconds(2 * 3600 + 3599)["display"])   # 02:59:59 local
        self.assertEqual(wall_a["display"], nt.from_seconds(4 * 3600)["display"])          # jumps to 04:00 local
        e = nt.nu_time(after, "civil", **self.HEL, dst_policy="elapsed", with_sun=False)
        self.assertAlmostEqual(e["notes"]["civilDayHours"], 23.0)
        self.assertEqual(e["display"], nt.from_seconds(3 * 3600, 23 * 3600)["display"])    # 3 elapsed h of 23

    def test_dst_fall_back(self):
        # Helsinki 2026-10-25: 04:00 → 03:00 local (01:00 UTC). 25-hour civil day; wall display repeats.
        first = nt.nu_time(at("2026-10-25T00:30:00Z"), "civil", **self.HEL, with_sun=False)
        second = nt.nu_time(at("2026-10-25T01:30:00Z"), "civil", **self.HEL, with_sun=False)
        self.assertEqual(first["display"], second["display"])                               # 03:30 twice
        e2 = nt.nu_time(at("2026-10-25T01:30:00Z"), "civil", **self.HEL, dst_policy="elapsed", with_sun=False)
        self.assertAlmostEqual(e2["notes"]["civilDayHours"], 25.0)
        self.assertNotEqual(e2["display"], first["display"])


class Astronomy(unittest.TestCase):
    def test_noon_altitude_matches_declination(self):
        # Equinox at Greenwich: noon altitude ≈ 90 − latitude.
        p = nt.sun_position(at("2026-03-20T12:07:00Z"), 51.4779, 0.0)
        self.assertAlmostEqual(p["altitude"], 90 - 51.4779, delta=1.0)
        self.assertAlmostEqual(p["azimuth"], 180, delta=3)

    def test_equinox_day_is_about_12h(self):
        e = nt.sun_events(dt.date(2026, 3, 20), 0.0, 0.0)
        hours = (at(e["sunset"]) - at(e["sunrise"])).total_seconds() / 3600
        self.assertAlmostEqual(hours, 12.1, delta=0.15)                     # refraction adds a few minutes

    def test_polar_day_and_night(self):
        self.assertEqual(nt.sun_events(dt.date(2026, 6, 21), 78.22, 15.65)["polar"], "day")     # Longyearbyen
        self.assertEqual(nt.sun_events(dt.date(2026, 12, 21), 78.22, 15.65)["polar"], "night")

    def test_helsinki_sunrise_not_fixed_at_beat_250(self):
        # 16 Nov: Helsinki sunrise ≈ 08:30 EET (06:30 UTC) — civil beat ≈ 354, nowhere near 250.
        e = nt.sun_events(dt.date(2026, 11, 16), 60.1699, 24.9384)
        r = nt.nu_time(at(e["sunrise"]), "civil", lat=60.1699, lon=24.9384, tz="Europe/Helsinki", with_sun=False)
        self.assertTrue(340 < r["beatsOfDay"] < 370, r["beatsOfDay"])


    def test_sun_events_use_the_local_calendar_date(self):
        # 22:00 MST on 16 Nov in Denver is already 17 Nov in UTC. "Today's" sunrise
        # must still be the morning of 16 Nov local (≈ 06:45 MST = 13:45 UTC).
        den = dict(lat=39.7392, lon=-104.9903)
        evening = at("2026-11-17T05:00:00Z")
        self.assertEqual(nt.local_date(evening, "America/Denver"), dt.date(2026, 11, 16))
        e = nt.sun_events_at(evening, tz="America/Denver", **den)
        rise = at(e["sunrise"])
        self.assertEqual(rise.astimezone(nt.ZoneInfo("America/Denver")).date(), dt.date(2026, 11, 16))
        self.assertTrue(dt.datetime(2026, 11, 16, 13, 20, tzinfo=UTC) < rise < dt.datetime(2026, 11, 16, 14, 10, tzinfo=UTC), rise)
        self.assertNotEqual(e, nt.sun_events(evening.date(), **den), "the UTC date would give tomorrow's")
        # Sydney 07:00 local on 17 Nov is still 16 Nov in UTC: events for 17 Nov local.
        syd = nt.sun_events_at(at("2026-11-16T20:00:00Z"), -33.8688, 151.2093, "Australia/Sydney")
        self.assertEqual(at(syd["sunrise"]).astimezone(nt.ZoneInfo("Australia/Sydney")).date(), dt.date(2026, 11, 17))
        # Without a zone, the mean-solar date from longitude.
        self.assertEqual(nt.local_date(evening, None, -104.99), dt.date(2026, 11, 16))

    def test_civil_twilight_brackets_sunrise_and_sunset(self):
        d = dt.date(2026, 11, 16)
        s = nt.sun_events(d, 60.1699, 24.9384)
        c = nt.sun_events(d, 60.1699, 24.9384, zenith=96)
        self.assertLess(at(c["sunrise"]), at(s["sunrise"]))
        self.assertGreater(at(c["sunset"]), at(s["sunset"]))

if __name__ == "__main__":
    unittest.main()
