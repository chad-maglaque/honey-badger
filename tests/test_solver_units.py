"""
Problem solver: exact arithmetic, the unit system, dimensional checks, constants and the hostile-input
limits of the expression parser. Run: python3.11 tests/test_solver_units.py
"""
from __future__ import annotations

import sys
import time
import unittest
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software"))

from solver import units as u  # noqa: E402


def val(expr):
    return u.evaluate(expr)


class Exact(unittest.TestCase):
    def test_fractions_and_decimals_are_exact(self):
        self.assertEqual(val("17/24 + 5/36").v, Fraction(61, 72))
        self.assertEqual(val("0.1 + 0.2").v, Fraction(3, 10))
        self.assertEqual(u.calc("0.1+0.2")["value"], "0.3")
        self.assertEqual(u.calc("17/24 + 5/36")["value"], "61/72 ≈ 0.8472222222")
        self.assertTrue(u.calc("1/3")["exact"])

    def test_precedence_and_juxtaposition(self):
        self.assertEqual(val("2 + 3 * 4").v, 14)
        self.assertEqual(val("2 ^ 3 ^ 2").v, 512)                # right-associative
        self.assertEqual(val("-2^2").v, -4)
        self.assertEqual(val("2 (3 + 4)").v, 14)
        self.assertEqual(val("10 %").v, Fraction(1, 10))
        q = val("60 W / 12 V")                                   # (60 W)/(12 V), not 60 W/12 · V
        self.assertEqual((q.v, q.d), (5, u.dim(A=1)))

    def test_powers_and_roots(self):
        self.assertEqual(val("sqrt(16)").v, 4)
        self.assertEqual(val("sqrt(9/4)").v, Fraction(3, 2))
        self.assertEqual(val("cbrt(27)").v, 3)
        self.assertAlmostEqual(float(val("sqrt(2)").v), 2 ** 0.5)
        self.assertEqual(val("2^-2").v, Fraction(1, 4))
        q = val("sqrt(4 m^2)")
        self.assertEqual((q.v, q.d), (2, u.dim(m=1)))
        self.assertEqual(len(str(val("2^1000").v.numerator)), 302)

    def test_functions(self):
        self.assertAlmostEqual(float(val("sin(30 deg)").v), 0.5)
        self.assertAlmostEqual(float(val("cos(pi)").v), -1)
        self.assertAlmostEqual(float(val("log(1000)").v), 3)
        self.assertAlmostEqual(float(val("ln(e)").v), 1)
        self.assertEqual(val("factorial(5)").v, 120)
        self.assertEqual(val("comb(5, 2)").v, 10)
        self.assertEqual(val("max(1 m, 3 ft)").v, 1)
        self.assertEqual(val("round(2.567, 2)").v, Fraction("2.57"))
        self.assertAlmostEqual(float(val("hypot(3 m, 4 m)").v), 5)


class Units(unittest.TestCase):
    def conv(self, expr, unit):
        return float(u.convert_q(val(expr), unit))

    def test_known_conversions(self):
        cases = [("55 gal", "L", 208.1976481), ("1 mi", "km", 1.609344), ("1 ft", "m", 0.3048), ("1 lb", "kg", 0.45359237),
                 ("100 psi", "kPa", 689.4757293), ("1 hp", "W", 745.6998716), ("60 mph", "m/s", 26.8224),
                 ("1 atm", "psi", 14.69594878), ("1 kWh", "MJ", 3.6), ("1 BTU", "J", 1055.05585262),
                 ("1 acre", "m^2", 4046.8564224), ("1 knot", "km/h", 1.852), ("12 V * 7 Ah", "Wh", 84),
                 ("1 ft3", "L", 28.31684659), ("3/4 in", "mm", 19.05), ("1 tsp", "mL", 4.928921594),
                 ("5 kg * 9.81 m/s^2", "N", 49.05), ("1 Cal", "kJ", 4.184), ("1 nmi", "m", 1852),
                 ("2 mSv", "Gy", 0.002), ("1 gpm", "L/min", 3.785411784), ("1 kgf", "lbf", 2.204622622)]
        for expr, unit, want in cases:
            with self.subTest(expr=expr, unit=unit):
                self.assertAlmostEqual(self.conv(expr, unit), want, delta=abs(want) * 1e-8)

    def test_exact_definitions(self):
        self.assertEqual(u.convert_q(val("1 in"), "cm"), Fraction(254, 100))
        self.assertEqual(u.convert_q(val("1 gal"), "in^3"), 231)
        self.assertEqual(u.convert_q(val("1 yd"), "ft"), 3)

    def test_temperatures(self):
        self.assertEqual(u.convert_q(val("20 degC"), "degF"), 68)
        self.assertEqual(u.convert_q(val("-40 degF"), "degC"), -40)
        self.assertEqual(u.convert_q(val("98.6 degF"), "K"), Fraction("310.15"))
        self.assertEqual(u.convert_q(val("0 K"), "degC"), Fraction("-273.15"))
        self.assertEqual(u.calc("5 °C to °F")["value"], "41")
        # inside a compound unit degC is a difference: 1 W/(m^2*degC) == 1 W/(m^2*K)
        self.assertEqual(u.convert_q(val("1 W/(m^2*degC)"), "W/(m^2*K)"), 1)
        self.assertAlmostEqual(float(u.convert_q(val("10 Cdeg"), "Fdeg")), 18)

    def test_prefixes_and_aliases(self):
        for a, b in (("1 km", "1000 m"), ("1 mA", "0.001 A"), ("1 µF", "1e-6 F"), ("1 uF", "1e-6 F"), ("1 kPa", "1000 Pa"),
                     ("1 mL", "1 cc"), ("2 feet", "24 inches"), ("1 GHz", "1000 MHz"), ("1 kcal", "1 Cal")):
            with self.subTest(a=a):
                self.assertEqual(val(a).v, val(b).v)
                self.assertEqual(val(a).d, val(b).d)

    def test_quantity_for_calculator_args(self):
        self.assertAlmostEqual(u.quantity("50 ft", "m"), 15.24)
        self.assertEqual(u.quantity(10, "m"), 10.0)
        self.assertEqual(u.quantity("10", "m"), 10.0)                # bare number: the default unit
        self.assertAlmostEqual(u.quantity("20 degC", "degF"), 68)
        self.assertEqual(u.quantity("0.5", "percent"), 0.5)
        with self.assertRaises(u.DimensionError):
            u.quantity("3 kg", "m")
        with self.assertRaises(u.CalcError):
            u.quantity(True, "m")
        with self.assertRaises(u.CalcError):
            u.quantity(float("nan"), "m")

    def test_calc_with_to(self):
        r = u.calc("55 gal to L")
        self.assertEqual(r["unit"], "L")
        self.assertTrue(r["value"].startswith("208.19"))
        self.assertEqual(u.calc("60 W / 12 V")["text"], "5 A")
        self.assertEqual(u.calc("sqrt(2 * 9.81 m/s^2 * 20 m)")["unit"], "m/s")


class Dimensions(unittest.TestCase):
    def test_dimension_errors(self):
        for expr in ("3 m + 2 s", "1 kg - 1 m", "sin(3 m)", "exp(2 s)", "2 ^ (3 m)", "max(1 m, 1 s)", "sqrt(m^3)"):
            with self.subTest(expr=expr):
                with self.assertRaises(u.DimensionError):
                    val(expr)
        with self.assertRaises(u.DimensionError) as cm:
            u.convert_q(val("3 kg"), "m/s")
        self.assertIn("mass", str(cm.exception))
        self.assertIn("speed", str(cm.exception))
        with self.assertRaises(u.DimensionError) as cm:
            u.convert_q(val("300 K"), "F")
        self.assertIn("degF", str(cm.exception))
        with self.assertRaises(u.DimensionError):
            u.calc("5 m to kg")

    def test_names(self):
        self.assertEqual(u.dim_name(val("1 N").d), "force")
        self.assertEqual(u.describe(val("2 kg*m^2/s^2"))["unit"], "J")
        self.assertEqual(u.describe(val("3 m/s"))["unit"], "m/s")


class Hostile(unittest.TestCase):
    def test_bad_syntax(self):
        for expr in ("", "2 +", "(2", "2)", "foo", "2 $ 3", "1e99999", "__import__('os')", "a.b", "2 ** ** 3",
                     "sqrt()", "comb(1)", "factorial(1000)", "factorial(2.5)", "1/0", "0^-1", "(-8)^(1/3.0001)",
                     "log(-1)", "1" * 500 + ".5e10"):
            with self.subTest(expr=expr[:30]):
                with self.assertRaises(u.CalcError):
                    val(expr)

    def test_limits_are_fast(self):
        for expr in ("9^9^9", "10^10^10", "(" * 41 + "1" + ")" * 41, "1+" * 300 + "1", "2^1024^2",
                     "factorial(300)^300", "-" * 50 + "1", "x" * 700):
            with self.subTest(expr=expr[:30]):
                t = time.monotonic()
                try:
                    val(expr)
                except u.CalcError:
                    pass
                self.assertLess(time.monotonic() - t, 1.0)

    def test_not_text(self):
        for bad in (None, 3, ["1"], {"a": 1}):
            with self.assertRaises(u.CalcError):
                u.calc(bad)

    def test_huge_results_become_float_or_error(self):
        q = val("10^400")
        self.assertIsInstance(q.v, Fraction)
        with self.assertRaises(u.CalcError):
            val("sin(1) * 10^400")


class Constants(unittest.TestCase):
    def test_lookup(self):
        self.assertEqual(u.search_constants("g0")[0]["value"], "9.80665")
        self.assertTrue(any(c["name"] == "c_water" for c in u.search_constants("water")))
        self.assertEqual(u.calc("c to km/s")["value"], "299792.458")
        self.assertAlmostEqual(float(u.convert_q(val("0.5 * 2 kg * g0 * 10 m"), "J")), 98.0665)
        self.assertEqual(u.search_constants("no such thing at all"), [])


if __name__ == "__main__":
    unittest.main()
