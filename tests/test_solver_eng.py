"""
Problem solver: every engineering and survival calculator against known values (textbook formulas,
published tables, hand calculations), plus bad input. Run: python3.11 tests/test_solver_eng.py
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software"))

from solver import engineering as e  # noqa: E402
from solver import survival as s  # noqa: E402
from solver import tools  # noqa: E402


class Electrical(unittest.TestCase):
    def test_ohms_law_all_pairs(self):
        V, I, R, P = 12.0, 5.0, 2.4, 60.0  # noqa: N806
        for kw in ({"voltage": V, "current": I}, {"voltage": V, "resistance": R}, {"voltage": V, "power": P},
                   {"current": I, "resistance": R}, {"current": I, "power": P}, {"resistance": R, "power": P}):
            with self.subTest(kw=kw):
                r = e.ohms_law(**kw)
                for k, want in (("voltage_V", V), ("current_A", I), ("resistance_ohm", R), ("power_W", P)):
                    self.assertAlmostEqual(r[k], want, places=3)
        with self.assertRaises(e.EngError):
            e.ohms_law(voltage=12)
        with self.assertRaises(e.EngError):
            e.ohms_law(voltage=12, current=1, power=12)
        with self.assertRaises(e.EngError):
            e.ohms_law(voltage=-12, current=1)

    def test_awg_table(self):
        # NEC Chapter 9 Table 8 / standard AWG: 10 AWG = 5.261 mm², 12 AWG = 3.309 mm², 4/0 = 107.2 mm²
        self.assertAlmostEqual(e.awg_area_mm2(10), 5.261, places=2)
        self.assertAlmostEqual(e.awg_area_mm2(12), 3.309, places=2)
        self.assertAlmostEqual(e.awg_area_mm2(-3), 107.2, delta=0.2)
        self.assertEqual(e.parse_awg("4/0"), -3)
        self.assertEqual(e.awg_name(-1), "2/0")

    def test_wire_drop(self):
        # 10 AWG copper ≈ 3.277 Ω/km; 10 A over 10 m one way (20 m of wire) → 0.655 V
        r = e.wire_drop(current=10, length=10, voltage=12, awg=10)
        self.assertAlmostEqual(r["drop_V"], 0.6554, places=3)
        self.assertAlmostEqual(r["drop_pct"], 5.46, places=1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["recommended_awg"], "7")
        r = e.wire_drop(current=40, length=1, voltage=12, awg=14)
        self.assertIn("warning", r)
        r = e.wire_drop(current=500, length=100, voltage=12)
        self.assertIn("thicker than 4/0", r["recommended_awg"])
        with self.assertRaises(e.EngError):
            e.wire_drop(current=10, length=10, voltage=12, awg="banana")
        with self.assertRaises(e.EngError):
            e.wire_drop(current=10, length=10, voltage=12, material="gold")

    def test_solar_inverter_battery(self):
        r = e.solar_size(daily_wh=1200, sun_hours=5)
        self.assertEqual((r["panel_W"], r["battery_Wh"], r["battery_Ah"]), (320.0, 2400.0, 200.0))
        r = e.solar_size(1200, 5, system_v=24, dod=80)          # percent accepted
        self.assertEqual(r["battery_Ah"], 62.5)
        r = e.inverter_size(1000, battery_v=12, efficiency=0.9)
        self.assertAlmostEqual(r["dc_current_A"], 92.6, places=1)
        self.assertEqual(r["inverter_surge_W"], 2000)
        r = e.battery_runtime(100, 12, 30, dod=0.5, efficiency=0.9)
        self.assertAlmostEqual(r["hours"], 18.0, places=2)


class Mechanical(unittest.TestCase):
    def test_beam_formulas(self):
        E, I = 200e9, 8.0e-6  # noqa: N806
        r = e.beam(4, "simply_supported", point_load=10000, E=E, I=I)
        self.assertAlmostEqual(r["max_moment_Nm"], 10000.0)
        self.assertAlmostEqual(r["max_deflection_m"], 10000 * 64 / (48 * E * I), delta=1e-4 * r["max_deflection_m"])
        r = e.beam(4, "simply_supported", uniform_load=2000, E=E, I=I)
        self.assertAlmostEqual(r["max_moment_Nm"], 4000.0)
        self.assertAlmostEqual(r["max_deflection_m"], 5 * 2000 * 256 / (384 * E * I), delta=1e-4 * r["max_deflection_m"])
        r = e.beam(2, "cantilever", point_load=1000, E=E, I=I)
        self.assertAlmostEqual(r["max_moment_Nm"], 2000.0)
        self.assertAlmostEqual(r["max_deflection_m"], 1000 * 8 / (3 * E * I), delta=1e-4 * r["max_deflection_m"])
        r = e.beam(2, "cantilever", uniform_load=500, E=E, I=I)
        self.assertAlmostEqual(r["max_deflection_m"], 500 * 16 / (8 * E * I), delta=1e-4 * r["max_deflection_m"])

    def test_beam_rectangle_and_material(self):
        # a 2×8 (38 × 184 mm) softwood joist, 3 m span, 1 kN at mid-span
        r = e.beam(3, point_load=1000, width=0.038, height=0.184, material="softwood")
        self.assertAlmostEqual(r["I_m4"], 0.038 * 0.184 ** 3 / 12, places=9)
        self.assertAlmostEqual(r["max_stress_Pa"], 750 / (0.038 * 0.184 ** 2 / 6), delta=50)
        self.assertTrue(r["stress_ok"])
        with self.assertRaises(e.EngError):
            e.beam(3, point_load=1, uniform_load=1)
        with self.assertRaises(e.EngError):
            e.beam(3, point_load=1, material="cheese")

    def test_rope_lever_pulley(self):
        self.assertAlmostEqual(e.rope_load(1000, 30)["tension_per_leg_N"], 1000.0)
        self.assertAlmostEqual(e.rope_load(1000, 90)["tension_per_leg_N"], 500.0)
        self.assertAlmostEqual(e.rope_load(1000, 10)["tension_per_leg_N"], 1000 / (2 * math.sin(math.radians(10))), places=0)
        r = e.rope_load(1000, 30, breaking_strength=4000, safety_factor=5)
        self.assertFalse(r["ok"])
        self.assertAlmostEqual(e.lever(1000, 0.5, 2)["effort_N"], 250.0)
        r = e.pulley(1000, 4, 1.0)
        self.assertAlmostEqual(r["effort_N"], 250.0)
        self.assertGreater(e.pulley(1000, 4, 0.9)["effort_N"], 250)
        with self.assertRaises(e.EngError):
            e.rope_load(1000, 120)


class Fluids(unittest.TestCase):
    def test_pipe_flow(self):
        r = e.pipe_flow(0.001, 0.025, 100)
        self.assertAlmostEqual(r["velocity_m_s"], 0.001 / (math.pi * 0.025 ** 2 / 4), places=3)
        re_ = r["velocity_m_s"] * 0.025 / 1.004e-6
        f = 0.25 / math.log10(1.5e-6 / 0.025 / 3.7 + 5.74 / re_ ** 0.9) ** 2
        self.assertAlmostEqual(r["friction_factor"], f, places=4)
        self.assertAlmostEqual(r["head_loss_m"], f * 100 / 0.025 * r["velocity_m_s"] ** 2 / (2 * 9.80665), delta=0.01)
        self.assertEqual(r["regime"], "turbulent")
        self.assertEqual(e.pipe_flow(1e-6, 0.025, 10)["regime"], "laminar")

    def test_laminar_is_64_over_re(self):
        self.assertAlmostEqual(e.friction_factor(1000, 0.0), 0.064)

    def test_pump_hydrostatic_gravity(self):
        self.assertAlmostEqual(e.pump_power(0.001, 10, 1.0)["hydraulic_W"], 998 * 9.80665 * 0.001 * 10, places=2)
        self.assertAlmostEqual(e.hydrostatic(10)["pressure_kPa"], 97.87, places=1)
        g = e.gravity_flow(10, 100, 0.025)
        v = g["velocity_m_s"]
        f = g["friction_factor"]
        self.assertAlmostEqual((1 + 1.5 + f * 100 / 0.025) * v * v / (2 * 9.80665), 10, places=2)   # energy balance closes
        self.assertGreater(e.gravity_flow(10, 100, 0.05)["flow_L_min"], g["flow_L_min"] * 4)


class Heat(unittest.TestCase):
    def test_heat_loss(self):
        r = e.heat_loss(10, 20, rsi=2.0)
        self.assertAlmostEqual(r["heat_loss_W"], 100.0)
        r = e.heat_loss(10, 20, r_value=11.36)                    # US R-11.36 ≈ RSI 2.0
        self.assertAlmostEqual(r["heat_loss_W"], 100.0, delta=0.5)
        r = e.heat_loss(10, 20, u_value=0.5)
        self.assertAlmostEqual(r["heat_loss_W"], 100.0)
        r = e.heat_loss(10, 20, layers=[{"material": "fiberglass", "thickness": 0.09}])
        self.assertAlmostEqual(r["rsi_total"], 0.09 / 0.04 + 0.17, places=2)
        with self.assertRaises(e.EngError):
            e.heat_loss(10, 20, rsi=2, u_value=1)
        with self.assertRaises(e.EngError):
            e.heat_loss(10, 20, layers=[{"material": "fiberglass", "thickness": 90}])

    def test_fuel_and_water_heating(self):
        r = e.fuel_energy("propane", mass_kg=1)
        self.assertAlmostEqual(r["energy_MJ"], 46.4)
        r = e.fuel_energy("diesel", volume_l=1)
        self.assertAlmostEqual(r["energy_MJ"], 0.832 * 42.6, places=2)
        with self.assertRaises(e.EngError):
            e.fuel_energy("wood", volume_l=1)
        r = e.water_heating(1, 20, 100, efficiency=1.0)
        self.assertAlmostEqual(r["energy_needed_kJ"], 334.88)
        r = e.water_heating(1, 20, 100, efficiency=0.5, power_w=1000)
        self.assertAlmostEqual(r["minutes"], 334880 / 0.5 / 1000 / 60, places=1)


class Water(unittest.TestCase):
    def test_bleach_epa_table(self):
        # EPA: 8 drops/gal at 6%, 6 drops/gal at 8.25%, 2 drops/L, double for cloudy water
        self.assertEqual(e.bleach_dose(3.785411784, 6)["drops"], 8)
        self.assertEqual(e.bleach_dose(3.785411784, 8.25)["drops"], 6)
        self.assertEqual(e.bleach_dose(1, 6)["drops"], 3)               # 2.1 → round up
        self.assertEqual(e.bleach_dose(3.785411784, 8.25, cloudy=True)["drops"], 12)
        self.assertEqual(e.bleach_dose(3.785411784 * 55, 6)["drops"], 440)
        r = e.bleach_dose(100, 6, target_ppm=2)
        self.assertAlmostEqual(r["ml_for_target"], 2 * 100 / 60, places=2)
        with self.assertRaises(e.EngError):
            e.bleach_dose(10, 6, target_ppm=50)
        with self.assertRaises(e.EngError):
            e.bleach_dose(10, 40)

    def test_boiling_point(self):
        self.assertAlmostEqual(e.boiling_point(0)["boiling_point_C"], 100.0, delta=0.05)
        self.assertAlmostEqual(e.boiling_point(3000)["boiling_point_C"], 90.0, delta=0.3)
        self.assertAlmostEqual(e.boiling_point(1609)["boiling_point_C"], 94.6, delta=0.5)    # Denver ≈ 95 °C
        self.assertEqual(e.boiling_point(2500)["boil_minutes"], 3)
        self.assertEqual(e.boiling_point(100)["boil_minutes"], 1)


class Radio(unittest.TestCase):
    def test_antenna(self):
        r = e.antenna_length(146)
        self.assertAlmostEqual(r["length_m"], 299.792458 / 146 / 2 * 0.95, places=3)
        self.assertAlmostEqual(r["classic_468_over_f_ft"], 3.205, places=2)
        self.assertAlmostEqual(e.antenna_length(146, "quarter", 1.0)["length_m"], 299.792458 / 146 / 4, places=3)

    def test_link_budget(self):
        r = e.link_budget(2400, 1, 20, 0, 0, 0, -80)
        self.assertAlmostEqual(r["free_space_loss_dB"], 100.04, places=1)
        self.assertAlmostEqual(r["received_dbm"], -80.04, places=1)
        self.assertAlmostEqual(r["free_space_range_km"], 10 ** (-0.04 / 20), delta=0.005)
        self.assertAlmostEqual(e.dbm(1), 30.0)
        ok, text = tools.run_tool(tools.ToolContext(), "link_budget", {"frequency": "915 MHz", "distance": "10 km", "tx_power": "1 W"})
        self.assertTrue(ok, text)

    def test_horizon_fresnel(self):
        self.assertAlmostEqual(e.radio_horizon(10)["radio_horizon_km"], 13.03, places=1)
        self.assertAlmostEqual(e.radio_horizon(10, 10)["radio_horizon_km"], 26.06, places=1)
        self.assertAlmostEqual(e.fresnel(2400, 1)["fresnel_radius_m"], 5.59, places=2)
        with self.assertRaises(e.EngError):
            e.fresnel(2400, 1, 2)


class Survival(unittest.TestCase):
    def test_water_and_calories(self):
        r = s.water_needs(1, 22, "rest")
        self.assertEqual(r["drinking_L_per_person_day"], 2.5)
        hot = s.water_needs(1, 35, "rest")
        self.assertGreater(hot["drinking_L_per_person_day"], 2.5)
        self.assertAlmostEqual(s.water_needs(4, 20, "light", 3)["fema_minimum_L"], 45.42, places=1)
        r = s.calorie_needs(70, "moderate", 175, 30, "male")
        self.assertAlmostEqual(r["bmr_kcal"], 1648.75, delta=1)            # Mifflin–St Jeor
        self.assertAlmostEqual(r["daily_kcal"], 1648.75 * 1.55, delta=2)
        r = s.calorie_needs(60, "light", 165, 40, "female")
        self.assertAlmostEqual(r["bmr_kcal"], 600 + 1031.25 - 200 - 161, delta=1)
        self.assertGreater(s.calorie_needs(70, "light", temp_c=-20)["daily_kcal"], s.calorie_needs(70, "light")["daily_kcal"])
        with self.assertRaises(e.EngError):
            s.water_needs(1, 22, "partying")

    def test_ration(self):
        r = s.ration(100, 4, 5)
        self.assertEqual(r["days"], 5.0)
        self.assertEqual(r["levels"][2]["days"], 10.0)

    def test_travel_time_naismith(self):
        r = s.travel_time(10, 600, terrain="road")
        self.assertEqual(r["hours"], 3.0)
        r = s.travel_time(10, 0, load_kg=30, body_kg=75, terrain="road")
        self.assertAlmostEqual(r["hours"], 2 * 1.2, places=2)
        self.assertIn("warning", s.travel_time(10, 0, load_kg=40, body_kg=75))

    def test_navigation(self):
        r = s.nav_distance(51.5074, -0.1278, 48.8566, 2.3522)            # London → Paris
        self.assertAlmostEqual(r["distance_km"], 343.5, delta=1)
        self.assertAlmostEqual(r["initial_bearing_true_deg"], 148.1, delta=0.3)
        r = s.nav_distance(0, 0, 0, 1, declination_deg=10)
        self.assertAlmostEqual(r["distance_km"], 111.19, delta=0.05)
        self.assertAlmostEqual(r["initial_bearing_magnetic_deg"], 80, delta=0.01)
        d = s.dead_reckoning(0, 0, 90, 111.195)
        self.assertAlmostEqual(d["lat"], 0, places=5)
        self.assertAlmostEqual(d["lon"], 1.0, places=3)
        d = s.dead_reckoning(45, 10, 0, speed_kmh=5, hours=2)
        self.assertAlmostEqual(d["lat"], 45 + 10 / 111.195, places=3)
        with self.assertRaises(e.EngError):
            s.nav_distance(91, 0, 0, 0)

    def test_weather(self):
        self.assertAlmostEqual(s.weather(temp_c=-10, wind_kmh=30)["wind_chill_C"], -19.5, delta=0.1)   # NWS table ≈ −20
        hi = s.weather(temp_c=(90 - 32) * 5 / 9, humidity=70)
        self.assertAlmostEqual(hi["heat_index_F"], 106, delta=1)                                       # NWS table: 106 °F
        self.assertAlmostEqual(s.dew_point(20, 50), 9.3, delta=0.1)
        self.assertIn("storm", s.weather(pressure_change_hpa_3h=-7)["pressure_trend"])
        r = s.weather(temp_c=10, dew_point_c=9)
        self.assertTrue(any("fog" in x for x in r["rules"]))
        with self.assertRaises(e.EngError):
            s.weather()
        self.assertAlmostEqual(s.lightning_distance(10)["distance_km"], 3.43, places=2)


class ToolLayer(unittest.TestCase):
    """The same calculators through the tool layer: units in text, strict argument checking."""
    def run_tool(self, name, args):
        ok, text = tools.run_tool(tools.ToolContext(), name, args)
        return ok, text

    def test_units_in_arguments(self):
        ok, text = self.run_tool("wire_drop", {"current": "10 A", "length": "32.8 ft", "voltage": 12, "awg": "10"})
        self.assertTrue(ok, text)
        self.assertIn('"drop_V":0.65', text)
        ok, text = self.run_tool("gravity_flow", {"elevation_drop": "82 ft", "length": "300 m", "diameter": "3/4 in"})
        self.assertTrue(ok, text)
        ok, text = self.run_tool("bleach_dose", {"volume": "55 gal", "bleach_pct": 6})
        self.assertTrue(ok and '"drops":440' in text, text)
        ok, text = self.run_tool("boiling_point", {"altitude": "10000 ft"})
        self.assertTrue(ok, text)
        ok, text = self.run_tool("heat_loss", {"area": "100 ft^2", "delta_t": 20,
                                               "layers": [{"material": "fiberglass", "thickness": "3.5 in"}]})
        self.assertTrue(ok, text)
        ok, text = self.run_tool("fuel_energy", {"fuel": "propane", "amount": "20 lb"})
        self.assertTrue(ok, text)

    def test_strict_arguments(self):
        for name, args, frag in (("wire_drop", {"current": 10}, "needs"), ("calc", {"expr": "1", "evil": 1}, "unknown argument"),
                                 ("wire_drop", {"current": "10 kg", "length": 1, "voltage": 12}, "current"),
                                 ("ohms_law", {"voltage": True, "current": 1}, "error"), ("calc", "2+2", "object"),
                                 ("beam", {"span": 3, "support": "floating", "point_load": 1}, "support"),
                                 ("pulley", {"load": 10, "supporting_lines": 2.5}, "whole number"),
                                 ("nash", {"row_payoffs": [[1]] * 200}, "too long"),
                                 ("calc", {"expr": "x" * 5000}, "too long"), ("Calc!", {}, "bad tool name"),
                                 ("nosuch", {}, "no tool"), ("python", {"code": "print(1)"}, "no tool"),
                                 ("supplies", {}, "no tool"), ("library_read", {"n": 1}, "no tool")):
            with self.subTest(name=name, args=str(args)[:30]):
                ok, text = self.run_tool(name, args)
                self.assertFalse(ok)
                self.assertIn(frag, text)

    def test_results_are_compact_json(self):
        ok, text = self.run_tool("zero_sum", {"payoffs": [[3, -1], [-2, 4]]})
        self.assertTrue(ok)
        self.assertIn('"value":"1"', text)
        self.assertIn('"row":["0.6","0.4"]', text)
        ok, text = self.run_tool("linprog", {"objective": [3, 5], "constraints": [[[1, 0], "<=", 4], [[0, 2], "<=", 12], [[3, 2], "<=", 18]]})
        self.assertIn('"value":"36"', text)
        ok, text = self.run_tool("nash", {"row_payoffs": [[2, 0], [0, 1]], "col_payoffs": [[1, 0], [0, 2]]})
        self.assertIn('"pure":[{"row":1,"col":1},{"row":2,"col":2}]', text)
        self.assertLessEqual(len(text), tools.MAX_RESULT_CHARS + 10)

    def test_direct_calculators(self):
        self.assertEqual(tools.run_direct("ohms_law", {"voltage": 12, "current": 2})["power_W"], 24)
        with self.assertRaises(tools.ToolError):
            tools.run_direct("python", {"code": "1"})
        with self.assertRaises(tools.ToolError):
            tools.run_direct("library_search", {"query": "x"})
        names = {c["name"] for c in tools.catalogue()}
        self.assertIn("beam", names)
        self.assertFalse(names & {"python", "supplies", "library_search", "library_read"})


if __name__ == "__main__":
    unittest.main()
