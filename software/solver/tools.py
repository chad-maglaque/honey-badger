"""
The tools the problem-solver model may call, with strict argument checking.

Every tool is a pure calculation, a read of the local library (kiwix-serve on loopback), the
sandboxed Python runner, or (family members only) a read-only summary of the household's supplies.
NO tool sends messages, writes files, changes settings or reaches the network: a model talked into
anything by a retrieved article ("prompt injection") has nothing dangerous to call. That invariant
is tested (tests/test_solver_loop.py) — keep it when adding tools.
"""
from __future__ import annotations

import html
import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from fractions import Fraction
from html.parser import HTMLParser
from typing import Any, Callable

from . import engineering as eng
from . import gametheory as gt
from . import simplex
from . import survival as sv
from . import units

MAX_RESULT_CHARS = 1600
MAX_ARTICLE_CHARS = 1800


class ToolError(ValueError):
    pass


@dataclass
class P:
    kind: str                       # str num int bool q choice list json awg any
    unit: str = ""                  # for q: the unit the calculator wants
    required: bool = False
    default: Any = None
    max_len: int = 200
    choices: tuple = ()
    doc: str = ""


@dataclass
class Tool:
    name: str
    fn: Callable
    params: dict
    desc: str
    group: str
    needs: str = ""                 # "" | "member" | "sandbox" | "library"


@dataclass
class ToolContext:
    """Per-job state handed to tools."""
    member: bool = False
    kiwix_search: Callable | None = None
    read_article: Callable | None = None
    sandbox: Any = None             # sandbox.SandboxClient (only when its isolation was verified)
    supplies: Callable | None = None
    hits: list = field(default_factory=list)
    sources: list = field(default_factory=list)     # articles actually read: citations [n]
    texts: list = field(default_factory=list)       # every tool result (for the dose check)


# ── sanitising untrusted text (library articles, snippets) ──
_MARKUP = re.compile(r"</?\s*(?:tool_call|tool_result|answer|data|think|plan|system|user|assistant|function|"
                     r"im_start|im_end|tool)\b[^>]{0,200}>", re.I)
_TEMPLATE = re.compile(r"<\|[^|<>]{0,40}\|>|\[/?INST\]|<<SYS>>|<</SYS>>|<s>|</s>", re.I)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")


def sanitize(text: str, limit: int) -> str:
    """Make retrieved text inert inside the prompt: no tag that our protocol or a chat template
    gives meaning to, no control or bidi characters, no JSON that looks like a tool call."""
    t = _CTRL.sub("", str(text))
    t = _TEMPLATE.sub("[removed]", t)
    for _ in range(3):                               # nested/overlapping attempts
        t2 = _MARKUP.sub("[removed]", t)
        if t2 == t:
            break
        t = t2
    t = re.sub(r'"\s*(tool|tool_name|function|arguments|args)\s*"\s*:', r'"\1 (quoted)":', t, flags=re.I)
    return t[:limit]


# ── output formatting ──

def _plain(v):
    if isinstance(v, Fraction):
        return units.fmt_num(v)
    if isinstance(v, float):
        return eng.sig(v, 6) if math.isfinite(v) else str(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def render(obj) -> str:
    s = json.dumps(_plain(obj), ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= MAX_RESULT_CHARS else s[:MAX_RESULT_CHARS] + "…(cut)"


# ── argument checking ──

def _check_json(v, depth=0):
    if depth > 8:
        raise ToolError("argument nested too deeply")
    if isinstance(v, dict):
        if len(v) > 60:
            raise ToolError("argument too big")
        for k, x in v.items():
            if not isinstance(k, str) or len(k) > 40:
                raise ToolError("bad key in argument")
            _check_json(x, depth + 1)
    elif isinstance(v, list):
        if len(v) > 120:
            raise ToolError("argument list too long")
        for x in v:
            _check_json(x, depth + 1)
    elif isinstance(v, str):
        if len(v) > 2000:
            raise ToolError("argument text too long")
    elif isinstance(v, float) and not math.isfinite(v):
        raise ToolError("numbers must be finite")


def coerce(name: str, spec: P, v):
    k = spec.kind
    if k == "str":
        if not isinstance(v, str):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                v = str(v)
            else:
                raise ToolError(f"{name} must be text")
        if len(v) > spec.max_len:
            raise ToolError(f"{name} is too long (limit {spec.max_len} characters)")
        return v
    if k == "num":
        if isinstance(v, str):
            try:
                return float(units.to_float(units.num(v)))
            except units.CalcError:
                pass
            try:
                q = units.evaluate(v)
            except units.CalcError as e:
                raise ToolError(f"{name}: {e}") from None
            if q.d != units.D0:
                raise ToolError(f"{name} must be a plain number")
            return units.to_float(q.v)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ToolError(f"{name} must be a number")
        return float(v)
    if k == "int":
        if isinstance(v, str) and v.strip().lstrip("-").isdigit() and len(v) < 10:
            v = int(v)
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if isinstance(v, bool) or not isinstance(v, int) or abs(v) > 10 ** 6:
            raise ToolError(f"{name} must be a whole number")
        return v
    if k == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.lower() in ("true", "yes", "false", "no"):
            return v.lower() in ("true", "yes")
        raise ToolError(f"{name} must be true or false")
    if k == "q":
        try:
            return units.quantity(v, spec.unit)
        except units.CalcError as e:
            raise ToolError(f"{name} (in {spec.unit}): {e}") from None
    if k == "choice":
        s = str(v).strip().lower().replace(" ", "_").replace("-", "_")
        if s not in spec.choices:
            raise ToolError(f"{name} must be one of {', '.join(spec.choices)}")
        return s
    if k in ("list", "json", "any"):
        _check_json(v)
        if k == "list" and not isinstance(v, list):
            raise ToolError(f"{name} must be a list")
        return v
    if k == "awg":
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ToolError(f"{name} must be a wire gauge like 12 or 4/0")
        return v
    raise ToolError(f"internal: unknown kind {k}")


def check_args(tool: Tool, args) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("args must be an object of named arguments")
    unknown = [k for k in args if k not in tool.params]
    if unknown:
        raise ToolError(f"unknown argument {str(unknown[0])[:30]!r}; {tool.name} takes: {', '.join(tool.params)}")
    out = {}
    for name, spec in tool.params.items():
        if name in args and args[name] is not None:
            out[name] = coerce(name, spec, args[name])
        elif spec.required:
            raise ToolError(f"{tool.name} needs {name}")
        elif spec.default is not None:
            out[name] = spec.default
    return out


# ── the tools ──

def t_calc(ctx, expr):
    return units.calc(expr)


def t_convert(ctx, value, to):
    return units.convert(value, to)


def t_constant(ctx, name):
    found = units.search_constants(name)
    if not found:
        raise ToolError(f"no constant matching {name[:30]!r}; try a word like 'gravity', 'water', 'light'")
    return {"constants": found}


def t_python(ctx, code):
    if ctx.sandbox is None:
        raise ToolError("the Python runner is not available on this Badger; use calc instead")
    res = ctx.sandbox.run(code)
    return {"stdout": res.get("stdout", "")[:1200], "stderr": res.get("stderr", "")[-600:],
            "exit_code": res.get("exit"), "timed_out": res.get("timed_out", False)}


def t_ohms_law(ctx, voltage=None, current=None, resistance=None, power=None):
    return eng.ohms_law(voltage, current, resistance, power)


def t_wire_drop(ctx, current, length, voltage, awg=None, area_mm2=None, material="copper", max_drop_pct=3.0):
    return eng.wire_drop(current, length, voltage, awg, area_mm2, material, max_drop_pct)


def t_solar_size(ctx, daily_wh, sun_hours, system_v=12.0, autonomy_days=1.0, dod=0.5, efficiency=0.75):
    return eng.solar_size(daily_wh, sun_hours, system_v, autonomy_days, dod, efficiency)


def t_inverter_size(ctx, continuous_w, surge_w=None, battery_v=12.0, efficiency=0.9):
    return eng.inverter_size(continuous_w, surge_w, battery_v, efficiency)


def t_battery_runtime(ctx, capacity_ah, voltage, load_w, dod=0.8, efficiency=0.9):
    return eng.battery_runtime(capacity_ah, voltage, load_w, dod, efficiency)


def t_beam(ctx, span, support="simply_supported", point_load=None, uniform_load=None, E=None, I=None,  # noqa: N803
           width=None, height=None, material=None):
    return eng.beam(span, support, point_load, uniform_load, E, I, width, height, material)


def t_rope_load(ctx, weight, angle_deg=90.0, legs=2, breaking_strength=None, safety_factor=5.0):
    return eng.rope_load(weight, angle_deg, legs, breaking_strength, safety_factor)


def t_lever(ctx, load, load_arm, effort_arm):
    return eng.lever(load, load_arm, effort_arm)


def t_pulley(ctx, load, supporting_lines, efficiency_per_sheave=0.9):
    return eng.pulley(load, supporting_lines, efficiency_per_sheave)


def t_pipe_flow(ctx, flow, diameter, length, roughness=1.5e-6, elevation_gain=0.0, minor_loss_k=0.0):
    return eng.pipe_flow(flow, diameter, length, roughness, elevation_gain, minor_loss_k)


def t_pump_power(ctx, flow, head, efficiency=0.6):
    return eng.pump_power(flow, head, efficiency)


def t_gravity_flow(ctx, elevation_drop, length, diameter, roughness=1.5e-6, minor_loss_k=1.5):
    return eng.gravity_flow(elevation_drop, length, diameter, roughness, minor_loss_k)


def t_hydrostatic(ctx, depth):
    return eng.hydrostatic(depth)


def t_heat_loss(ctx, area, delta_t, r_value=None, rsi=None, u_value=None, layers=None, hours=24.0):
    if layers is not None:
        fixed = []
        for ly in layers:
            if isinstance(ly, dict) and "thickness" in ly:
                ly = dict(ly)
                try:
                    ly["thickness"] = units.quantity(ly["thickness"], "m")
                except units.CalcError as e:
                    raise ToolError(f"layer thickness: {e}") from None
            fixed.append(ly)
        layers = fixed
    return eng.heat_loss(area, delta_t, r_value, rsi, u_value, layers, True, hours)


def t_fuel_energy(ctx, fuel, amount, efficiency=1.0):
    try:
        q = units.evaluate(amount) if isinstance(amount, str) else None
    except units.CalcError as e:
        raise ToolError(f"amount: {e}") from None
    if q is None:
        raise ToolError("amount needs a unit, like \"2 kg\" or \"5 L\"")
    if q.d == units.dim(kg=1):
        return eng.fuel_energy(fuel, mass_kg=units.to_float(q.v), efficiency=efficiency)
    if q.d == units.dim(m=3):
        return eng.fuel_energy(fuel, volume_l=units.to_float(q.v) * 1000, efficiency=efficiency)
    raise ToolError("amount must be a mass (kg, lb) or a volume (L, gal)")


def t_water_heating(ctx, volume, t_start, t_end=100.0, efficiency=0.5, power=None, fuel=None, boil_off=0.0):
    return eng.water_heating(volume, t_start, t_end, efficiency, power, fuel, boil_off)


def t_bleach_dose(ctx, volume, bleach_pct=8.25, cloudy=False, target_ppm=None):
    return eng.bleach_dose(volume, bleach_pct, cloudy, target_ppm)


def t_boiling_point(ctx, altitude):
    return eng.boiling_point(altitude)


def t_antenna_length(ctx, frequency, kind="dipole", velocity_factor=0.95):
    return eng.antenna_length(frequency, kind, velocity_factor)


def t_link_budget(ctx, frequency, distance, tx_power, tx_gain_dbi=2.15, rx_gain_dbi=2.15, losses_db=3.0,
                  rx_sensitivity_dbm=-110.0):
    if isinstance(tx_power, str) and re.search(r"[a-zA-Z]", tx_power.replace("dBm", "").replace("dbm", "")):
        try:
            w = units.quantity(tx_power, "W")
        except units.CalcError as e:
            raise ToolError(f"tx_power: {e}") from None
        p = eng.dbm(eng._pos("tx_power", w))
    else:
        try:
            p = float(str(tx_power).lower().replace("dbm", "").strip())
        except ValueError:
            raise ToolError("tx_power: give dBm (a number) or watts like \"5 W\"") from None
    return eng.link_budget(frequency, distance, p, tx_gain_dbi, rx_gain_dbi, losses_db, rx_sensitivity_dbm)


def t_radio_horizon(ctx, h1, h2=0.0):
    return eng.radio_horizon(h1, h2)


def t_fresnel(ctx, frequency, distance, d1=None):
    return eng.fresnel(frequency, distance, d1)


def t_nash(ctx, row_payoffs, col_payoffs=None):
    try:
        r = gt.nash(row_payoffs, col_payoffs)
    except (gt.GameError, simplex.LPError) as e:
        raise ToolError(str(e)) from None
    r["pure"] = [{"row": i + 1, "col": j + 1} for i, j in r["pure"]]
    return r


def t_zero_sum(ctx, payoffs):
    try:
        return gt.zero_sum(payoffs)
    except (gt.GameError, simplex.LPError) as e:
        raise ToolError(str(e)) from None


def t_expected_value(ctx, outcomes):
    try:
        return gt.expected_value(outcomes)
    except (gt.GameError, simplex.LPError) as e:
        raise ToolError(str(e)) from None


def t_decision_tree(ctx, tree):
    try:
        return gt.decision_tree(tree)
    except (gt.GameError, simplex.LPError) as e:
        raise ToolError(str(e)) from None


def t_linprog(ctx, objective, constraints, maximize=True):
    try:
        return simplex.linprog(objective, constraints, maximize)
    except simplex.LPError as e:
        raise ToolError(str(e)) from None


def t_water_needs(ctx, people=1, temp_c=22.0, activity="light", days=1.0):
    return sv.water_needs(people, temp_c, activity, days)


def t_calorie_needs(ctx, weight, activity="light", height=None, age=None, sex=None, temp_c=None):
    return sv.calorie_needs(weight, activity, height, age, sex, temp_c)


def t_ration(ctx, total, per_person_per_day, people=1.0, extra_per_day=0.0, unit=""):
    return sv.ration(total, per_person_per_day, people, extra_per_day, unit)


def t_travel_time(ctx, distance, ascent=0.0, descent=0.0, load=0.0, body_weight=75.0, terrain="trail",
                  hours_per_day=8.0):
    return sv.travel_time(distance, ascent, descent, load, body_weight, terrain, hours_per_day)


def t_nav_distance(ctx, lat1, lon1, lat2, lon2, declination=None):
    return sv.nav_distance(lat1, lon1, lat2, lon2, declination)


def t_dead_reckoning(ctx, lat, lon, bearing, distance=None, speed=None, hours=None):
    return sv.dead_reckoning(lat, lon, bearing, distance, speed, hours)


def t_weather(ctx, temp_c=None, humidity=None, dew_point_c=None, wind=None, pressure_change_hpa_3h=None):
    return sv.weather(temp_c, humidity, dew_point_c, wind, pressure_change_hpa_3h)


def t_lightning(ctx, seconds):
    return sv.lightning_distance(seconds)


def t_library_search(ctx, query):
    if ctx.kiwix_search is None:
        raise ToolError("the library isn't available")
    try:
        hits = ctx.kiwix_search(query, count=6) or []
    except Exception as e:  # noqa: BLE001 — a library failure is a tool error, not a crash
        raise ToolError(f"library search failed: {type(e).__name__}") from None
    ctx.hits = [h for h in hits if isinstance(h, dict) and h.get("fetch")][:5]
    if not ctx.hits:
        return {"results": [], "note": "Nothing found. Try other words (simpler, or the technical name)."}
    return {"results": [{"n": i + 1, "title": sanitize(h.get("title", ""), 120), "book": sanitize(h.get("source", ""), 80),
                         "snippet": sanitize(h.get("snippet", ""), 220)} for i, h in enumerate(ctx.hits)],
            "note": "Read one with library_read {\"n\": N}."}


def t_library_read(ctx, n):
    if not ctx.hits:
        raise ToolError("search first with library_search")
    if not 1 <= n <= len(ctx.hits):
        raise ToolError(f"n must be 1–{len(ctx.hits)} (from the last library_search)")
    h = ctx.hits[n - 1]
    text = ctx.read_article(h["fetch"]) if ctx.read_article else ""
    if not text:
        raise ToolError("that article couldn't be read")
    for k, s in enumerate(ctx.sources):
        if s["url"] == h.get("url"):
            num = k + 1
            break
    else:
        ctx.sources.append({"title": str(h.get("title", ""))[:200], "url": str(h.get("url", "")),
                            "source": str(h.get("source", ""))[:120], "text": text})
        num = len(ctx.sources)
    return {"source_number": num, "title": sanitize(h.get("title", ""), 120),
            "text": sanitize(text, MAX_ARTICLE_CHARS), "cite_as": f"[{num}]"}


def t_supplies(ctx):
    if not ctx.member or ctx.supplies is None:
        raise ToolError("the household supplies are only for family members")
    return ctx.supplies()


def _q(unit, required=False, default=None, doc=""):
    return P("q", unit=unit, required=required, default=default, doc=doc)


def _n(required=False, default=None):
    return P("num", required=required, default=default)


REGISTRY: dict[str, Tool] = {}


def _reg(name, fn, params, desc, group, needs=""):
    REGISTRY[name] = Tool(name, fn, params, desc, group, needs)


_reg("calc", t_calc, {"expr": P("str", required=True, max_len=600)},
     'exact arithmetic with units; "… to <unit>" converts. e.g. "sqrt(2*9.81 m/s^2*20 m)", "60 W / 12 V", "55 gal to L"', "core")
_reg("convert", t_convert, {"value": P("str", required=True, max_len=200), "to": P("str", required=True, max_len=60)},
     'unit conversion with dimension check, e.g. value "68 degF" to "degC"', "core")
_reg("constant", t_constant, {"name": P("str", required=True, max_len=60)}, "physical constants by name or word", "core")
_reg("python", t_python, {"code": P("str", required=True, max_len=6000)},
     "run short Python 3 (stdlib only, no files, no network, 10 s); print() the results", "core", needs="sandbox")
_reg("ohms_law", t_ohms_law, {"voltage": _q("V"), "current": _q("A"), "resistance": _q("ohm"), "power": _q("W")},
     "any two of voltage, current, resistance, power", "electrical")
_reg("wire_drop", t_wire_drop, {"current": _q("A", True), "length": _q("m", True, doc="one way"), "voltage": _q("V", True),
                                "awg": P("awg"), "area_mm2": _n(), "material": P("choice", choices=("copper", "aluminium", "aluminum")),
                                "max_drop_pct": _n()},
     "voltage drop over a cable run (length one way) and the wire gauge needed", "electrical")
_reg("solar_size", t_solar_size, {"daily_wh": _n(True), "sun_hours": _n(True), "system_v": _q("V"), "autonomy_days": _n(),
                                  "dod": _n(), "efficiency": _n()},
     "solar panel watts, battery Ah and controller amps for a daily load", "electrical")
_reg("inverter_size", t_inverter_size, {"continuous_w": _q("W", True), "surge_w": _q("W"), "battery_v": _q("V"), "efficiency": _n()},
     "inverter rating, DC current and fuse", "electrical")
_reg("battery_runtime", t_battery_runtime, {"capacity_ah": _n(True), "voltage": _q("V", True), "load_w": _q("W", True),
                                            "dod": _n(), "efficiency": _n()}, "hours a battery runs a load", "electrical")
_reg("beam", t_beam, {"span": _q("m", True), "support": P("choice", choices=("simply_supported", "cantilever")),
                      "point_load": _q("N"), "uniform_load": _q("N/m"), "E": _q("Pa"), "I": _q("m^4"), "width": _q("m"),
                      "height": _q("m"), "material": P("choice", choices=tuple(sorted(eng.MATERIALS)))},
     "beam bending moment, deflection, stress (point load at centre/free end, or uniform load)", "mechanical")
_reg("rope_load", t_rope_load, {"weight": _q("N", True), "angle_deg": _n(), "legs": P("int"), "breaking_strength": _q("N"),
                                "safety_factor": _n()}, "tension in rope/strap legs at an angle above horizontal", "mechanical")
_reg("lever", t_lever, {"load": _q("N", True), "load_arm": _q("m", True), "effort_arm": _q("m", True)}, "lever effort", "mechanical")
_reg("pulley", t_pulley, {"load": _q("N", True), "supporting_lines": P("int", required=True), "efficiency_per_sheave": _n()},
     "block and tackle effort with friction", "mechanical")
_reg("pipe_flow", t_pipe_flow, {"flow": _q("m^3/s", True), "diameter": _q("m", True), "length": _q("m", True),
                                "roughness": _q("m"), "elevation_gain": _q("m"), "minor_loss_k": _n()},
     "head loss and pressure drop for water in a pipe (Darcy–Weisbach)", "fluids")
_reg("pump_power", t_pump_power, {"flow": _q("m^3/s", True), "head": _q("m", True), "efficiency": _n()}, "pump power", "fluids")
_reg("gravity_flow", t_gravity_flow, {"elevation_drop": _q("m", True), "length": _q("m", True), "diameter": _q("m", True),
                                      "roughness": _q("m"), "minor_loss_k": _n()}, "flow from a tank/spring down a pipe", "fluids")
_reg("hydrostatic", t_hydrostatic, {"depth": _q("m", True)}, "water pressure at a depth", "fluids")
_reg("heat_loss", t_heat_loss, {"area": _q("m^2", True), "delta_t": _q("Cdeg", True), "r_value": _n(), "rsi": _n(), "u_value": _n(),
                                "layers": P("list"), "hours": _n()},
     'heat lost through a wall/roof; layers like [{"material":"fiberglass","thickness":"3.5 in"}]', "heat")
_reg("fuel_energy", t_fuel_energy, {"fuel": P("str", required=True, max_len=30), "amount": P("str", required=True, max_len=40),
                                    "efficiency": _n()}, "energy in fuel (wood, propane, diesel…), amount like \"2 kg\"", "heat")
_reg("water_heating", t_water_heating, {"volume": _q("L", True), "t_start": _q("degC", True), "t_end": _q("degC"),
                                        "efficiency": _n(), "power": _q("W"), "fuel": P("str", max_len=30), "boil_off": _q("L")},
     "energy, time or fuel to heat/boil water", "heat")
_reg("bleach_dose", t_bleach_dose, {"volume": _q("L", True), "bleach_pct": _n(), "cloudy": P("bool"), "target_ppm": _n()},
     "household bleach to disinfect drinking water (EPA)", "water")
_reg("boiling_point", t_boiling_point, {"altitude": _q("m", True)}, "boiling point of water at altitude, boil time", "water")
_reg("antenna_length", t_antenna_length, {"frequency": _q("MHz", True),
                                          "kind": P("choice", choices=("dipole", "half", "quarter", "five_eighths", "full", "loop")),
                                          "velocity_factor": _n()}, "antenna element lengths", "radio")
_reg("link_budget", t_link_budget, {"frequency": _q("MHz", True), "distance": _q("km", True), "tx_power": P("any", required=True),
                                    "tx_gain_dbi": _n(), "rx_gain_dbi": _n(), "losses_db": _n(), "rx_sensitivity_dbm": _n()},
     'radio link margin (tx_power in dBm or "5 W")', "radio")
_reg("radio_horizon", t_radio_horizon, {"h1": _q("m", True), "h2": _q("m")}, "line-of-sight distance between antenna heights", "radio")
_reg("fresnel", t_fresnel, {"frequency": _q("MHz", True), "distance": _q("km", True), "d1": _q("km")}, "Fresnel zone clearance", "radio")
_reg("nash", t_nash, {"row_payoffs": P("list", required=True), "col_payoffs": P("list")},
     "Nash equilibria (pure and mixed) of a 2-player game; omit col_payoffs for zero-sum", "games")
_reg("zero_sum", t_zero_sum, {"payoffs": P("list", required=True)}, "value and optimal mixed strategies of a zero-sum game", "games")
_reg("expected_value", t_expected_value, {"outcomes": P("list", required=True)}, "expected value of [[p, value], …]", "games")
_reg("decision_tree", t_decision_tree, {"tree": P("json", required=True)},
     'best choice in a tree: {"options":[{"label","cost"?,"node"}]} / {"branches":[{"p","node"}]} / number', "games")
_reg("linprog", t_linprog, {"objective": P("list", required=True), "constraints": P("list", required=True), "maximize": P("bool")},
     'linear programming, x ≥ 0: constraints [[coeffs, "<=", rhs], …]', "games")
_reg("water_needs", t_water_needs, {"people": _n(), "temp_c": _q("degC"), "activity": P("choice", choices=tuple(sv.ACTIVITY)),
                                    "days": _n()}, "drinking water needed", "survival")
_reg("calorie_needs", t_calorie_needs, {"weight": _q("kg", True), "activity": P("choice", choices=tuple(sv.ACTIVITY)),
                                        "height": _q("cm"), "age": _n(), "sex": P("choice", choices=("male", "female")),
                                        "temp_c": _q("degC")}, "daily calories", "survival")
_reg("ration", t_ration, {"total": _n(True), "per_person_per_day": _n(True), "people": _n(), "extra_per_day": _n(),
                          "unit": P("str", max_len=20)}, "how many days a supply lasts, at 100/75/50% rations", "survival")
_reg("travel_time", t_travel_time, {"distance": _q("km", True), "ascent": _q("m"), "descent": _q("m"), "load": _q("kg"),
                                    "body_weight": _q("kg"), "terrain": P("choice", choices=tuple(sv.TERRAIN)),
                                    "hours_per_day": _n()}, "walking time (Naismith) with terrain and load", "survival")
_reg("nav_distance", t_nav_distance, {"lat1": _n(True), "lon1": _n(True), "lat2": _n(True), "lon2": _n(True), "declination": _n()},
     "distance and bearing between two coordinates", "survival")
_reg("dead_reckoning", t_dead_reckoning, {"lat": _n(True), "lon": _n(True), "bearing": _n(True), "distance": _q("km"),
                                          "speed": _q("km/h"), "hours": _n()}, "position after travelling a bearing", "survival")
_reg("weather", t_weather, {"temp_c": _q("degC"), "humidity": _n(), "dew_point_c": _q("degC"), "wind": _q("km/h"),
                            "pressure_change_hpa_3h": _n()}, "dew point, cloud base, wind chill, heat index, pressure trend", "survival")
_reg("lightning", t_lightning, {"seconds": _n(True)}, "storm distance from flash-to-thunder seconds", "survival")
_reg("library_search", t_library_search, {"query": P("str", required=True, max_len=200)},
     "search the offline library (Wikipedia, manuals, Q&A); returns numbered results", "library", needs="library")
_reg("library_read", t_library_read, {"n": P("int", required=True)}, "read result n of the last search (cite as [source_number])",
     "library", needs="library")
_reg("supplies", t_supplies, {}, "the family's supplies summary (water, food, fuel days) — family members only", "survival",
     needs="member")

TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def available(ctx: ToolContext) -> list[str]:
    """The tools this job may call (visitors never get `supplies`; no `python` without a verified sandbox)."""
    out = []
    for name, t in REGISTRY.items():
        if t.needs == "member" and not (ctx.member and ctx.supplies is not None):
            continue
        if t.needs == "sandbox" and ctx.sandbox is None:
            continue
        if t.needs == "library" and ctx.kiwix_search is None:
            continue
        out.append(name)
    return out


def describe_tools(names: list[str]) -> str:
    lines = []
    for n in names:
        t = REGISTRY[n]
        args = ", ".join(a + ("*" if p.required else "") for a, p in t.params.items())
        lines.append(f"- {n}({args}): {t.desc}")
    return "\n".join(lines) + "\n(* = required)"


def run_tool(ctx: ToolContext, name, args) -> tuple[bool, str]:
    """(ok, text for the model). Never raises for bad input."""
    if not isinstance(name, str) or not TOOL_NAME.match(name):
        return False, "error: bad tool name"
    if name not in REGISTRY or name not in available(ctx):
        return False, f"error: no tool called {name!r} is available. Available: {', '.join(available(ctx))}"
    tool = REGISTRY[name]
    try:
        clean = check_args(tool, args)
        res = tool.fn(ctx, **clean)
        text = render(res)
        ctx.texts.append(text)
        return True, text
    except (ToolError, units.CalcError, eng.EngError, gt.GameError, simplex.LPError) as e:
        return False, f"error: {e}"
    except RecursionError:
        return False, "error: input nested too deeply"
    except (ValueError, TypeError, ZeroDivisionError, OverflowError, ArithmeticError) as e:
        return False, f"error: {type(e).__name__}: {str(e)[:200]}"


def run_direct(name: str, args, ctx: ToolContext | None = None) -> dict:
    """For the page's calculator panel (no AI): deterministic calculators only."""
    ctx = ctx or ToolContext()
    t = REGISTRY.get(name) if isinstance(name, str) else None
    if t is None or t.needs:
        raise ToolError("no such calculator")
    clean = check_args(t, args)
    return _plain(t.fn(ctx, **clean))


def catalogue() -> list[dict]:
    """Calculator descriptions for the page (no library/sandbox/supplies tools)."""
    out = []
    for name, t in REGISTRY.items():
        if t.needs:
            continue
        out.append({"name": name, "group": t.group, "desc": t.desc,
                    "params": [{"name": a, "kind": p.kind, "unit": p.unit, "required": p.required,
                                "choices": list(p.choices)} for a, p in t.params.items()]})
    return out


# ── reading an article from kiwix-serve (loopback only) ──
class _Text(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "table", "sup", "figure"}
    KEEP = {"p", "li", "h1", "h2", "h3", "h4", "dd", "dt", "td", "pre"}

    def __init__(self):
        super().__init__()
        self.skip = 0
        self.keep = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.KEEP:
            self.keep += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.KEEP and self.keep:
            self.keep -= 1
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip and self.keep:
            self.parts.append(data)


def article_reader(kiwix_base: str, limit: int = 4000) -> Callable[[str], str]:
    """Only URLs under the configured kiwix-serve on loopback are ever fetched (search results come from
    there; anything else is refused, so a crafted result can't make the Badger fetch another host)."""
    base = kiwix_base.rstrip("/")
    from urllib.parse import urlsplit
    b = urlsplit(base)
    loop = b.hostname in ("127.0.0.1", "localhost", "::1")

    def read(url: str) -> str:
        u = urlsplit(str(url))
        if not loop or u.scheme != b.scheme or u.netloc != b.netloc or not (u.path + "/").startswith(b.path.rstrip("/") + "/"):
            return ""
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                raw = r.read(600_000).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            return ""
        p = _Text()
        try:
            p.feed(raw)
        except Exception:  # noqa: BLE001 — malformed HTML: use what was parsed
            pass
        txt = re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", html.unescape("".join(p.parts)))).strip()
        return txt[:limit]
    return read
