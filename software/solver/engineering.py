"""
Engineering calculators for the problem solver. Every function takes plain numbers in the units
named in its signature (the tool layer converts "50 ft", "3/4 in", "20 degC" first), and returns a
dict of results plus the formula used and caveats. Rules of thumb are labelled as such.

Electrical: ohms_law, wire_drop, solar_size, inverter_size, battery_runtime
Mechanical: beam, rope_load, lever, pulley
Fluids:     pipe_flow, pump_power, gravity_flow, hydrostatic
Heat:       heat_loss, fuel_energy, water_heating
Water:      bleach_dose, boiling_point
Radio:      antenna_length, link_budget, radio_horizon, fresnel
"""
from __future__ import annotations

import math

G0 = 9.80665


class EngError(ValueError):
    pass


def sig(x: float, n: int = 5) -> float:
    """Round to n significant figures (for display; calculations keep full precision)."""
    if x is None or not isinstance(x, (int, float)) or isinstance(x, bool):
        return x
    if x == 0 or not math.isfinite(x):
        return x
    return round(x, n - 1 - int(math.floor(math.log10(abs(x)))))


def _pos(name: str, v: float, allow_zero: bool = False) -> float:
    if v is None or not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        raise EngError(f"{name} must be a number")
    if v < 0 or (v == 0 and not allow_zero):
        raise EngError(f"{name} must be {'zero or ' if allow_zero else ''}more than zero")
    return float(v)


def _frac01(name, v):
    v = _pos(name, v)
    if v > 1:
        if v <= 100:
            return v / 100                     # 80 → 80 %
        raise EngError(f"{name} must be a fraction (0–1) or a percentage")
    return v


# ─────────────────────────────── electrical ───────────────────────────────

def ohms_law(voltage=None, current=None, resistance=None, power=None) -> dict:
    given = {k: v for k, v in (("voltage", voltage), ("current", current), ("resistance", resistance),
                               ("power", power)) if v is not None}
    if len(given) != 2:
        raise EngError("give exactly two of voltage, current, resistance, power")
    for k, v in given.items():
        _pos(k, v)
    V, I, R, P = voltage, current, resistance, power  # noqa: N806
    if V is not None and I is not None:
        R, P = V / I, V * I
    elif V is not None and R is not None:
        I, P = V / R, V * V / R
    elif V is not None and P is not None:
        I, R = P / V, V * V / P
    elif I is not None and R is not None:
        V, P = I * R, I * I * R
    elif I is not None and P is not None:
        V, R = P / I, P / (I * I)
    else:
        V, I = math.sqrt(P * R), math.sqrt(P / R)
    return {"voltage_V": sig(V), "current_A": sig(I), "resistance_ohm": sig(R), "power_W": sig(P),
            "formula": "V = I·R, P = V·I"}


RESISTIVITY = {"copper": 1.724e-8, "aluminium": 2.82e-8, "aluminum": 2.82e-8}     # Ω·m at 20 °C
# Rough ampacity, insulated copper, 60 °C column (NEC 310.16), for a sanity warning only.
AMPACITY = {14: 15, 12: 20, 10: 30, 8: 40, 6: 55, 4: 70, 3: 85, 2: 95, 1: 110, 0: 125, -1: 145, -2: 165, -3: 195}


def awg_area_mm2(awg: int) -> float:
    d = 0.127 * 92 ** ((36 - awg) / 39)
    return math.pi / 4 * d * d


def parse_awg(v) -> int:
    if isinstance(v, bool):
        raise EngError("wire gauge must be a number like 12 or 4/0")
    if isinstance(v, (int, float)) and float(v).is_integer():
        n = int(v)
    elif isinstance(v, str):
        s = v.strip().lower().replace("awg", "").replace("ga", "").strip()
        zeros = {"0": 0, "1/0": 0, "00": -1, "2/0": -1, "000": -2, "3/0": -2, "0000": -3, "4/0": -3}
        if s in zeros:
            n = zeros[s]
        elif s.lstrip("-").isdigit():
            n = int(s)
        else:
            raise EngError("wire gauge must be a number like 12 or 4/0")
    else:
        raise EngError("wire gauge must be a number like 12 or 4/0")
    if not -3 <= n <= 40:
        raise EngError("wire gauge must be between 4/0 and 40")
    return n


def awg_name(n: int) -> str:
    return f"{-n + 1}/0" if n <= 0 else str(n)


def wire_drop(current, length, voltage, awg=None, area_mm2=None, material="copper", max_drop_pct=3.0) -> dict:
    """length is one way (the circuit runs out and back: 2 × length of wire)."""
    I, L, V = _pos("current", current), _pos("length", length), _pos("voltage", voltage)  # noqa: N806
    rho = RESISTIVITY.get(str(material).lower())
    if rho is None:
        raise EngError("material must be copper or aluminium")
    maxp = _pos("max_drop_pct", max_drop_pct)
    out = {"formula": "R = ρ·(2·length)/area, drop = I·R"}
    if awg is not None or area_mm2 is not None:
        if awg is not None:
            n = parse_awg(awg)
            area = awg_area_mm2(n)
            out["awg"] = awg_name(n)
            amp = AMPACITY.get(max(n, -3)) if n <= 14 else None
            if amp and I > amp and str(material).lower() == "copper":
                out["warning"] = (f"{I:g} A is more than {awg_name(n)} AWG copper should carry in a building "
                                  f"(about {amp} A): it can overheat. Use thicker wire.")
        else:
            area = _pos("area_mm2", area_mm2)
        R = rho * 2 * L / (area * 1e-6)  # noqa: N806
        drop = I * R
        out.update({"area_mm2": sig(area, 4), "resistance_ohm": sig(R, 4), "drop_V": sig(drop, 4),
                    "drop_pct": sig(100 * drop / V, 3), "power_lost_W": sig(I * drop, 4),
                    "voltage_at_load_V": sig(V - drop, 4), "ok": 100 * drop / V <= maxp})
    for n in range(40, -4, -1):                       # thinnest AWG that keeps the drop under the limit
        area = awg_area_mm2(n)
        if 100 * I * rho * 2 * L / (area * 1e-6) / V <= maxp:
            out["recommended_awg"] = awg_name(n)
            out["recommended_area_mm2"] = sig(area, 3)
            break
    else:
        need = rho * 2 * L * I / (maxp / 100 * V) * 1e6
        out["recommended_area_mm2"] = sig(need, 3)
        out["recommended_awg"] = "thicker than 4/0: use parallel runs or a higher voltage"
    return out


def solar_size(daily_wh, sun_hours, system_v=12.0, autonomy_days=1.0, dod=0.5, efficiency=0.75) -> dict:
    E, H = _pos("daily_wh", daily_wh), _pos("sun_hours", sun_hours)  # noqa: N806
    Vs, days = _pos("system_v", system_v), _pos("autonomy_days", autonomy_days)  # noqa: N806
    dod, eff = _frac01("dod", dod), _frac01("efficiency", efficiency)
    panel = E / (H * eff)
    batt_wh = E * days / dod
    return {"panel_W": sig(panel, 4), "battery_Wh": sig(batt_wh, 4), "battery_Ah": sig(batt_wh / Vs, 4),
            "charge_controller_A": sig(panel / Vs * 1.25, 3),
            "formula": "panels = daily Wh / (sun hours × system efficiency); battery = daily Wh × days / depth of discharge",
            "notes": "Sun hours are 'peak sun hours' for your worst month (winter). Lead-acid: dod 0.5; "
                     "lithium (LiFePO4): dod 0.8. Controller current includes 25% margin."}


def inverter_size(continuous_w, surge_w=None, battery_v=12.0, efficiency=0.9) -> dict:
    P, Vb, eff = _pos("continuous_w", continuous_w), _pos("battery_v", battery_v), _frac01("efficiency", efficiency)  # noqa: N806
    surge = _pos("surge_w", surge_w) if surge_w is not None else P * 2
    dc = P / (Vb * eff)
    return {"inverter_continuous_W": sig(P * 1.25, 3), "inverter_surge_W": sig(surge, 3),
            "dc_current_A": sig(dc, 3), "fuse_A": sig(dc * 1.25, 3),
            "formula": "DC current = W / (battery V × efficiency); size the inverter 25% above the load",
            "notes": "Motors (fridges, pumps, power tools) start at 2–3× their running watts."}


def battery_runtime(capacity_ah, voltage, load_w, dod=0.8, efficiency=0.9) -> dict:
    C, V, P = _pos("capacity_ah", capacity_ah), _pos("voltage", voltage), _pos("load_w", load_w)  # noqa: N806
    dod, eff = _frac01("dod", dod), _frac01("efficiency", efficiency)
    usable = C * V * dod * eff
    return {"usable_Wh": sig(usable, 4), "hours": sig(usable / P, 4),
            "formula": "hours = Ah × V × depth of discharge × efficiency / load W",
            "notes": "Lead-acid: use dod 0.5 and expect less at high loads or in the cold."}


# ─────────────────────────────── mechanical ───────────────────────────────
MATERIALS = {   # E (Pa), allowable bending stress (Pa) — rough design values
    "softwood": (10e9, 8e6), "pine": (9e9, 7e6), "douglas_fir": (12.4e9, 9e6), "oak": (11e9, 10e6),
    "steel": (200e9, 150e6), "aluminium": (69e9, 100e6), "aluminum": (69e9, 100e6), "bamboo": (15e9, 20e6),
}


def beam(span, support="simply_supported", point_load=None, uniform_load=None, E=None, I=None,  # noqa: N803
         width=None, height=None, material=None) -> dict:
    """point_load in N (centre of a simply supported span, or the free end of a cantilever);
    uniform_load in N/m. E (Pa) and I (m^4), or a rectangular width × height (m) and a material."""
    L = _pos("span", span)  # noqa: N806
    if (point_load is None) == (uniform_load is None):
        raise EngError("give point_load (N) or uniform_load (N/m), not both")
    if support not in ("simply_supported", "cantilever"):
        raise EngError("support must be simply_supported or cantilever")
    allow = None
    if material is not None:
        if str(material).lower() not in MATERIALS:
            raise EngError(f"material must be one of {', '.join(sorted(MATERIALS))}")
        Em, allow = MATERIALS[str(material).lower()]
        E = E if E is not None else Em  # noqa: N806
    S = None  # noqa: N806
    if width is not None and height is not None:
        b, h = _pos("width", width), _pos("height", height)
        I = I if I is not None else b * h ** 3 / 12  # noqa: N806
        S = b * h * h / 6  # noqa: N806
    out = {}
    if point_load is not None:
        P = _pos("point_load", point_load)  # noqa: N806
        if support == "simply_supported":
            M, V, k, form = P * L / 4, P / 2, P * L ** 3 / 48, "M = PL/4, δ = PL³/(48EI)"  # noqa: N806
        else:
            M, V, k, form = P * L, P, P * L ** 3 / 3, "M = PL, δ = PL³/(3EI)"  # noqa: N806
    else:
        w = _pos("uniform_load", uniform_load)
        if support == "simply_supported":
            M, V, k, form = w * L * L / 8, w * L / 2, 5 * w * L ** 4 / 384, "M = wL²/8, δ = 5wL⁴/(384EI)"  # noqa: N806
        else:
            M, V, k, form = w * L * L / 2, w * L, w * L ** 4 / 8, "M = wL²/2, δ = wL⁴/(8EI)"  # noqa: N806
    out.update({"max_moment_Nm": sig(M), "max_shear_N": sig(V), "formula": form})
    if E is not None and I is not None:
        d = k / (_pos("E", E) * _pos("I", I))
        out["max_deflection_m"] = sig(d)
        out["span_over_deflection"] = sig(L / d, 3) if d else None
        out["deflection_note"] = "floors usually need span/deflection ≥ 360, shelves and roofs ≥ 240"
    if S is not None:
        stress = M / S
        out["max_stress_Pa"] = sig(stress)
        if allow:
            out["allowable_stress_Pa"] = allow
            out["stress_ok"] = stress <= allow
    if I is not None:
        out["I_m4"] = sig(I)
    return out


def rope_load(weight, angle_deg=90.0, legs=2, breaking_strength=None, safety_factor=5.0) -> dict:
    """Tension in each leg holding `weight` (N) with legs at angle_deg above horizontal
    (90 = hanging straight down)."""
    W = _pos("weight", weight)  # noqa: N806
    a = _pos("angle_deg", angle_deg)
    if a > 90:
        raise EngError("angle_deg is measured from horizontal: 0–90")
    n = int(legs) if isinstance(legs, (int, float)) and not isinstance(legs, bool) else 0
    if not 1 <= n <= 12:
        raise EngError("legs must be 1–12")
    T = W / (n * math.sin(math.radians(a)))  # noqa: N806
    out = {"tension_per_leg_N": sig(T), "tension_per_leg_kgf": sig(T / G0),
           "formula": "T = W / (legs × sin(angle from horizontal))",
           "notes": "Shallow angles multiply the load: at 30° each leg carries the whole weight, at 10° almost 3×. "
                    "A fall or jerk can load a rope many times the static weight."}
    if breaking_strength is not None:
        swl = _pos("breaking_strength", breaking_strength) / _pos("safety_factor", safety_factor)
        out.update({"safe_working_load_N": sig(swl), "ok": T <= swl})
    return out


def lever(load, load_arm, effort_arm) -> dict:
    F, a, b = _pos("load", load), _pos("load_arm", load_arm), _pos("effort_arm", effort_arm)  # noqa: N806
    return {"effort_N": sig(F * a / b), "mechanical_advantage": sig(b / a),
            "formula": "effort × effort_arm = load × load_arm"}


def pulley(load, supporting_lines, efficiency_per_sheave=0.9) -> dict:
    F = _pos("load", load)  # noqa: N806
    n = int(supporting_lines) if isinstance(supporting_lines, (int, float)) and not isinstance(supporting_lines, bool) else 0
    if not 1 <= n <= 20:
        raise EngError("supporting_lines must be 1–20")
    e = _frac01("efficiency_per_sheave", efficiency_per_sheave)
    ma = sum(e ** k for k in range(1, n + 1))          # friction at each sheave
    return {"ideal_effort_N": sig(F / n), "effort_N": sig(F / ma), "actual_mechanical_advantage": sig(ma, 3),
            "rope_pulled_per_metre_lifted_m": n,
            "formula": "ideal effort = load / lines; with friction, MA = e + e² + … + eⁿ"}


# ─────────────────────────────── fluids ───────────────────────────────
NU_WATER = 1.004e-6          # m²/s at 20 °C
RHO_WATER = 998.0


def friction_factor(re: float, rel_rough: float) -> float:
    if re < 2300:
        return 64 / re
    return 0.25 / math.log10(rel_rough / 3.7 + 5.74 / re ** 0.9) ** 2       # Swamee–Jain


def pipe_flow(flow, diameter, length, roughness=1.5e-6, elevation_gain=0.0, minor_loss_k=0.0) -> dict:
    """flow m³/s, diameter m (inside), length m, roughness m (PVC 0.0015 mm, steel 0.045 mm, old iron 0.26 mm)."""
    Q, D, L = _pos("flow", flow), _pos("diameter", diameter), _pos("length", length)  # noqa: N806
    eps = _pos("roughness", roughness, allow_zero=True)
    k = _pos("minor_loss_k", minor_loss_k, allow_zero=True)
    A = math.pi * D * D / 4  # noqa: N806
    v = Q / A
    re = v * D / NU_WATER
    f = friction_factor(re, eps / D)
    hf = f * L / D * v * v / (2 * G0) + k * v * v / (2 * G0)
    head = hf + float(elevation_gain)
    return {"velocity_m_s": sig(v), "reynolds": sig(re, 4), "regime": "laminar" if re < 2300 else "turbulent",
            "friction_factor": sig(f, 4), "head_loss_m": sig(hf), "total_head_m": sig(head),
            "pressure_drop_Pa": sig(RHO_WATER * G0 * head), "pressure_drop_psi": sig(RHO_WATER * G0 * head / 6894.757),
            "formula": "Darcy–Weisbach hf = f·(L/D)·v²/2g, f by Swamee–Jain (water at 20 °C)",
            "notes": "Keep household water velocity under about 2 m/s."}


def pump_power(flow, head, efficiency=0.6, density=RHO_WATER) -> dict:
    Q, H = _pos("flow", flow), _pos("head", head)  # noqa: N806
    eff, rho = _frac01("efficiency", efficiency), _pos("density", density)
    hyd = rho * G0 * Q * H
    return {"hydraulic_W": sig(hyd), "shaft_W": sig(hyd / eff), "shaft_hp": sig(hyd / eff / 745.7),
            "formula": "P = ρ·g·Q·H / efficiency"}


def gravity_flow(elevation_drop, length, diameter, roughness=1.5e-6, minor_loss_k=1.5) -> dict:
    """Flow from a tank or spring down a pipe open at the bottom."""
    H, L, D = _pos("elevation_drop", elevation_drop), _pos("length", length), _pos("diameter", diameter)  # noqa: N806
    eps = _pos("roughness", roughness, allow_zero=True)
    k = _pos("minor_loss_k", minor_loss_k, allow_zero=True)
    f = 0.02
    v = 0.0
    for _ in range(100):
        v = math.sqrt(2 * G0 * H / (1 + k + f * L / D))
        re = max(v * D / NU_WATER, 1.0)
        nf = friction_factor(re, eps / D)
        if abs(nf - f) < 1e-10:
            break
        f = nf
    Q = v * math.pi * D * D / 4  # noqa: N806
    return {"velocity_m_s": sig(v), "flow_m3_s": sig(Q), "flow_L_min": sig(Q * 60000), "flow_gpm": sig(Q * 15850.32),
            "friction_factor": sig(f, 4),
            "formula": "H = (1 + K + f·L/D)·v²/2g, solved for v (Darcy–Weisbach, Swamee–Jain)",
            "notes": "K covers entrance, bends and valves (1.5 is a short simple run). Air locks at high points stop flow."}


def hydrostatic(depth, density=RHO_WATER) -> dict:
    h, rho = _pos("depth", depth, allow_zero=True), _pos("density", density)
    p = rho * G0 * h
    return {"pressure_Pa": sig(p), "pressure_kPa": sig(p / 1000), "pressure_psi": sig(p / 6894.757),
            "formula": "p = ρ·g·h (gauge pressure)", "notes": "Every 10 m (33 ft) of water adds about 1 bar (14.5 psi)."}


# ─────────────────────────────── heat ───────────────────────────────
CONDUCTIVITY = {   # W/(m·K), typical
    "fiberglass": 0.04, "mineral_wool": 0.038, "eps": 0.036, "xps": 0.029, "polyiso": 0.023, "cellulose": 0.04,
    "straw_bale": 0.06, "softwood": 0.12, "hardwood": 0.16, "plywood": 0.13, "osb": 0.13, "drywall": 0.17,
    "brick": 0.7, "concrete": 1.4, "stone": 2.0, "soil": 1.0, "glass": 1.0, "snow": 0.2, "air_gap": 0.17,
    "wool_blanket": 0.04, "earth_dry": 0.5,
}
FUELS = {   # name: (MJ/kg lower heating value, kg/L or None), typical values
    "wood": (15.0, None), "wood_dry": (18.0, None), "charcoal": (29.0, None), "propane": (46.4, 0.493),
    "butane": (45.7, 0.573), "gasoline": (43.4, 0.745), "petrol": (43.4, 0.745), "diesel": (42.6, 0.832),
    "kerosene": (43.0, 0.80), "ethanol": (26.8, 0.789), "alcohol": (26.8, 0.789), "methanol": (19.9, 0.792),
    "white_gas": (44.0, 0.68), "coal": (24.0, None), "natural_gas": (47.1, None), "hexamine": (30.0, None),
}


def heat_loss(area, delta_t, r_value=None, rsi=None, u_value=None, layers=None, films=True, hours=24.0) -> dict:
    """area m², delta_t K (inside − outside). Insulation: r_value (US, ft²·°F·h/BTU), rsi (m²·K/W),
    u_value (W/m²·K) or layers [{"material": "fiberglass", "thickness": 0.09}] (m)."""
    A, dT = _pos("area", area), _pos("delta_t", delta_t, allow_zero=True)  # noqa: N806
    hrs = _pos("hours", hours)
    given = [x is not None for x in (r_value, rsi, u_value, layers)]
    if sum(given) != 1:
        raise EngError("give one of r_value (US), rsi, u_value or layers")
    detail = None
    if r_value is not None:
        R = _pos("r_value", r_value) * 0.1761  # noqa: N806
    elif rsi is not None:
        R = _pos("rsi", rsi)  # noqa: N806
    elif u_value is not None:
        R = 1 / _pos("u_value", u_value)  # noqa: N806
    else:
        if not isinstance(layers, list) or not 1 <= len(layers) <= 12:
            raise EngError("layers must be a list of 1–12 {material, thickness}")
        R, detail = 0.0, []  # noqa: N806
        for k, ly in enumerate(layers):
            if not isinstance(ly, dict):
                raise EngError(f"layer {k + 1} must be {{material, thickness}}")
            mat = str(ly.get("material", "")).lower()
            if mat not in CONDUCTIVITY:
                raise EngError(f"layer {k + 1}: material must be one of {', '.join(sorted(CONDUCTIVITY))}")
            t = ly.get("thickness")
            t = _pos(f"layer {k + 1} thickness", t)
            if t > 5:
                raise EngError(f"layer {k + 1}: thickness is in metres")
            r = t / CONDUCTIVITY[mat]
            detail.append({"material": mat, "rsi": sig(r, 3)})
            R += r  # noqa: N806
        if films:
            R += 0.17  # noqa: N806 — inside + outside surface films
    W = A * dT / R  # noqa: N806
    kwh = W * hrs / 1000
    out = {"rsi_total": sig(R, 3), "r_value_us": sig(R / 0.1761, 3), "heat_loss_W": sig(W), "heat_loss_BTU_h": sig(W * 3.41214),
           f"energy_kWh_per_{hrs:g}h": sig(kwh),
           "firewood_kg": sig(kwh * 3.6 / (15.0 * 0.6)),
           "formula": "Q = A·ΔT / R (conduction only)",
           "notes": "Draughts often lose as much heat again; windows lose 5–10× more than an insulated wall. "
                    "Firewood assumes seasoned wood (15 MJ/kg) in a stove at 60% efficiency."}
    if detail:
        out["layers"] = detail
    return out


def fuel_energy(fuel, mass_kg=None, volume_l=None, efficiency=1.0) -> dict:
    f = FUELS.get(str(fuel).lower().replace(" ", "_"))
    if f is None:
        raise EngError(f"fuel must be one of {', '.join(sorted(FUELS))}")
    mj_kg, dens = f
    if (mass_kg is None) == (volume_l is None):
        raise EngError("give mass_kg or volume_l")
    if volume_l is not None:
        if dens is None:
            raise EngError(f"{fuel} is measured by mass: give mass_kg")
        m = _pos("volume_l", volume_l) * dens
    else:
        m = _pos("mass_kg", mass_kg)
    eff = _frac01("efficiency", efficiency)
    mj = m * mj_kg * eff
    return {"mass_kg": sig(m), "energy_MJ": sig(mj), "energy_kWh": sig(mj / 3.6), "energy_BTU": sig(mj * 947.817),
            "MJ_per_kg": mj_kg, "formula": "E = mass × heating value × efficiency",
            "notes": "Typical lower heating values; wet wood gives much less (and smokes)."}


def water_heating(volume_l, t_start, t_end=100.0, efficiency=0.5, power_w=None, fuel=None, boil_off_l=0.0) -> dict:
    """Energy (and time or fuel) to heat water from t_start to t_end °C, optionally boiling some off."""
    V = _pos("volume_l", volume_l)  # noqa: N806
    if not -50 <= t_start <= 100 or not -50 <= t_end <= 100 or t_end < t_start:
        raise EngError("temperatures must be in °C, with t_end ≥ t_start, at most 100")
    eff = _frac01("efficiency", efficiency)
    boil = _pos("boil_off_l", boil_off_l, allow_zero=True)
    q = V * 4186 * (t_end - t_start) + boil * 2.257e6
    out = {"energy_needed_kJ": sig(q / 1000), "energy_input_kJ": sig(q / eff / 1000), "energy_input_Wh": sig(q / eff / 3600),
           "formula": "Q = m·c·ΔT (+ m_boiled·L) ÷ efficiency",
           "notes": "Open fires: efficiency ~0.1–0.2; camp stoves ~0.3–0.5; a lid helps a lot."}
    if power_w is not None:
        out["minutes"] = sig(q / eff / _pos("power_w", power_w) / 60, 3)
    if fuel is not None:
        fe = FUELS.get(str(fuel).lower().replace(" ", "_"))
        if fe is None:
            raise EngError(f"fuel must be one of {', '.join(sorted(FUELS))}")
        out["fuel_kg"] = sig(q / eff / (fe[0] * 1e6), 3)
        out["fuel_g"] = sig(q / eff / (fe[0] * 1e3), 3)
    return out


# ─────────────────────────────── water treatment ───────────────────────────────

def bleach_dose(volume_l, bleach_pct=8.25, cloudy=False, target_ppm=None) -> dict:
    """EPA emergency disinfection: unscented household bleach, 6 drops per gallon at 8.25%
    (8 at 6%), twice that for cloudy or very cold water; wait 30 minutes."""
    V = _pos("volume_l", volume_l)  # noqa: N806
    pct = _pos("bleach_pct", bleach_pct)
    if pct > 15:
        raise EngError("bleach_pct is the bottle's sodium hypochlorite percentage, usually 5–8.25")
    gal = V / 3.785411784
    per_gal = 6.0 if abs(pct - 8.25) < 1e-9 else 8.0 * 6.0 / pct
    drops = gal * per_gal * (2 if cloudy else 1)
    out = {"drops": max(1, math.ceil(drops - 0.05)), "ml": sig(drops * 0.05, 3), "tsp": sig(drops * 0.05 / 4.929, 3),
           "wait_minutes": 60 if cloudy else 30,
           "source": "US EPA, Emergency Disinfection of Drinking Water",
           "notes": "Plain unscented bleach only (no 'splashless' or scented). Filter cloudy water through cloth first. "
                    "After the wait it should smell slightly of chlorine; if not, repeat the dose and wait 15 more minutes. "
                    "Bleach does not kill Cryptosporidium: boiling does."}
    if target_ppm is not None:
        ppm = _pos("target_ppm", target_ppm)
        if ppm > 10:
            raise EngError("drinking-water chlorine doses are 2–5 ppm; more than 10 is not safe to drink")
        ml = ppm * V / (pct * 10)          # bleach ≈ pct × 10 mg/mL of sodium hypochlorite
        out["ml_for_target"] = sig(ml, 3)
        out["drops_for_target"] = max(1, math.ceil(ml * 20 - 0.05))
    return out


def boiling_point(altitude_m) -> dict:
    h = float(altitude_m)
    if not -500 <= h <= 9000:
        raise EngError("altitude must be between −500 and 9000 m")
    p = 101325 * (1 - 2.25577e-5 * h) ** 5.25588                            # standard atmosphere
    mmhg = p / 133.322387415
    t = 1730.63 / (8.07131 - math.log10(mmhg)) - 233.426                    # Antoine, water 1–100 °C
    return {"pressure_kPa": sig(p / 1000, 4), "boiling_point_C": sig(t, 4), "boiling_point_F": sig(t * 9 / 5 + 32, 4),
            "boil_minutes": 3 if h > 2000 else 1,
            "formula": "standard-atmosphere pressure, then the Antoine equation for water",
            "notes": "CDC: bring to a rolling boil for 1 minute; above 2,000 m (6,500 ft) boil for 3 minutes."}


# ─────────────────────────────── radio ───────────────────────────────
C_LIGHT = 299792458.0


def antenna_length(frequency_mhz, kind="dipole", velocity_factor=0.95) -> dict:
    f = _pos("frequency_mhz", frequency_mhz)
    k = _frac01("velocity_factor", velocity_factor)
    lam = C_LIGHT / (f * 1e6)
    frac = {"dipole": 0.5, "half": 0.5, "quarter": 0.25, "full": 1.0, "five_eighths": 0.625, "loop": 1.0}.get(kind)
    if frac is None:
        raise EngError("kind must be dipole, quarter, five_eighths, full or loop")
    L = lam * frac * k  # noqa: N806
    out = {"wavelength_m": sig(lam), "length_m": sig(L, 4), "length_ft": sig(L / 0.3048, 4),
           "formula": f"length = {frac:g} × wavelength × {k:g} (end effect / velocity factor)",
           "notes": "Cut a little long and trim for lowest SWR."}
    if kind in ("dipole", "half"):
        out["each_leg_m"] = sig(L / 2, 4)
        out["classic_468_over_f_ft"] = sig(468 / f, 4)
    return out


def dbm(watts: float) -> float:
    return 10 * math.log10(watts * 1000)


def link_budget(frequency_mhz, distance_km, tx_power_dbm, tx_gain_dbi=2.15, rx_gain_dbi=2.15, losses_db=3.0,
                rx_sensitivity_dbm=-110.0) -> dict:
    f, d = _pos("frequency_mhz", frequency_mhz), _pos("distance_km", distance_km)
    fspl = 20 * math.log10(d) + 20 * math.log10(f) + 32.44
    rx = tx_power_dbm + tx_gain_dbi + rx_gain_dbi - losses_db - fspl
    margin = rx - rx_sensitivity_dbm
    max_fspl = tx_power_dbm + tx_gain_dbi + rx_gain_dbi - losses_db - rx_sensitivity_dbm
    max_d = 10 ** ((max_fspl - 20 * math.log10(f) - 32.44) / 20)
    return {"free_space_loss_dB": sig(fspl, 4), "received_dbm": sig(rx, 4), "margin_dB": sig(margin, 3),
            "works_in_free_space": margin > 0, "free_space_range_km": sig(max_d, 3),
            "formula": "FSPL = 20·log10(d km) + 20·log10(f MHz) + 32.44",
            "notes": "Free space is the best case: hills, trees and buildings add 10–40 dB. Aim for 10–20 dB margin; "
                     "line of sight (radio_horizon) matters more than power on VHF/UHF."}


def radio_horizon(h1_m, h2_m=0.0) -> dict:
    a, b = _pos("h1_m", h1_m, allow_zero=True), _pos("h2_m", h2_m, allow_zero=True)
    return {"radio_horizon_km": sig(4.12 * (math.sqrt(a) + math.sqrt(b)), 4),
            "optical_horizon_km": sig(3.57 * (math.sqrt(a) + math.sqrt(b)), 4),
            "formula": "d ≈ 4.12·(√h1 + √h2) km (h in m, 4/3 earth radius for radio)"}


def fresnel(frequency_mhz, distance_km, d1_km=None) -> dict:
    f, D = _pos("frequency_mhz", frequency_mhz), _pos("distance_km", distance_km)  # noqa: N806
    d1 = D / 2 if d1_km is None else _pos("d1_km", d1_km)
    if d1 >= D:
        raise EngError("d1_km must be less than distance_km")
    r = 17.32 * math.sqrt(d1 * (D - d1) / ((f / 1000) * D))
    return {"fresnel_radius_m": sig(r, 4), "clearance_60pct_m": sig(0.6 * r, 4),
            "formula": "r = 17.32·√(d1·d2 / (f GHz · D)) m"}
