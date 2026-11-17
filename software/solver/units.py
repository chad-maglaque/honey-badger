"""
Exact arithmetic with units, for the problem solver's `calc` / `convert` / `constant` tools.

  calc("17/24 + 5/36")              → 61/72 (exact, plus a decimal)
  calc("60 W / 12 V")               → 5 A
  calc("55 gal to L")               → 208.1976... L
  calc("sqrt(2 * 9.81 m/s^2 * 20 m)")  → 19.809... m/s
  convert("68 degF", "degC")        → 20
  calc("3 m + 2 s")                 → DimensionError ("can't add length and time")

Numbers are kept as exact fractions (decimal literals are exact: 0.1 is 1/10) until a function
or a fractional power needs floating point. Units: SI base units and prefixes, derived SI units,
and common US customary units. Temperatures: "20 degC" alone is an absolute temperature
(293.15 K); inside a compound unit (W/(m^2*degC)) degC/degF are temperature *differences*.
Hard limits on expression length, nesting, number size and powers keep a hostile expression
from tying up the Badger.
"""
from __future__ import annotations

import math
import re
from fractions import Fraction

MAX_EXPR = 600
MAX_TOKENS = 400
MAX_DEPTH = 40
MAX_BITS = 4096                 # a Fraction bigger than this becomes a float
MAX_LITERAL_EXP = 400


class CalcError(ValueError):
    """Bad expression (syntax, unknown name, a limit)."""


class DimensionError(CalcError):
    """Units that don't fit together (adding metres to seconds, converting kg to m/s)."""


# ── dimensions: exponents of (m, kg, s, A, K, mol, cd) ──
BASE = ("m", "kg", "s", "A", "K", "mol", "cd")
D0 = (0, 0, 0, 0, 0, 0, 0)


def dim(m=0, kg=0, s=0, A=0, K=0, mol=0, cd=0) -> tuple:  # noqa: N803 — SI symbols
    return (m, kg, s, A, K, mol, cd)


DIM_NAMES = {
    D0: "a plain number", dim(m=1): "length", dim(kg=1): "mass", dim(s=1): "time", dim(A=1): "electric current",
    dim(K=1): "temperature", dim(mol=1): "amount of substance", dim(cd=1): "luminous intensity",
    dim(m=2): "area", dim(m=3): "volume", dim(m=1, s=-1): "speed", dim(m=1, s=-2): "acceleration",
    dim(kg=1, m=1, s=-2): "force", dim(kg=1, m=2, s=-2): "energy", dim(kg=1, m=2, s=-3): "power",
    dim(kg=1, m=-1, s=-2): "pressure", dim(kg=1, m=2, s=-3, A=-1): "voltage",
    dim(kg=1, m=2, s=-3, A=-2): "resistance", dim(s=1, A=1): "electric charge", dim(s=-1): "frequency",
    dim(kg=1, m=-3): "density", dim(m=3, s=-1): "flow rate", dim(m=2, s=-2): "dose (energy per mass)",
    dim(kg=-1, m=-2, s=4, A=2): "capacitance", dim(kg=1, m=2, s=-2, A=-2): "inductance",
    dim(kg=1, s=-3, K=-1): "heat transfer coefficient", dim(kg=1, m=1, s=-3, K=-1): "thermal conductivity",
    dim(kg=1, m=2, s=-1): "angular momentum", dim(kg=1, m=1, s=-1): "momentum",
    dim(kg=1, m=2, s=-2, K=-1): "heat capacity", dim(m=2, s=-2, K=-1): "specific heat",
    dim(kg=1, s=-1): "mass flow", dim(kg=1, s=-2): "surface tension / spring rate per length",
}


def dim_name(d: tuple) -> str:
    if d in DIM_NAMES:
        return DIM_NAMES[d]
    return "units of " + dim_str(d)


def dim_str(d: tuple) -> str:
    num = [f"{b}^{e}" if e != 1 else b for b, e in zip(BASE, d, strict=True) if e > 0]
    den = [f"{b}^{-e}" if e != -1 else b for b, e in zip(BASE, d, strict=True) if e < 0]
    if not num and not den:
        return "1"
    s = "·".join(num) or "1"
    if den:
        s += "/" + ("·".join(den) if len(den) == 1 else "(" + "·".join(den) + ")")
    return s


def _dmul(a, b):
    return tuple(x + y for x, y in zip(a, b, strict=True))


def _ddiv(a, b):
    return tuple(x - y for x, y in zip(a, b, strict=True))


# ── numbers: Fraction while exact, float otherwise ──

def _fix(v):
    if isinstance(v, Fraction):
        if v.denominator == 1 and v.numerator.bit_length() <= MAX_BITS:
            return v
        if v.numerator.bit_length() > MAX_BITS or v.denominator.bit_length() > MAX_BITS:
            return _flt(v)
        return v
    if isinstance(v, bool):
        raise CalcError("not a number")
    if isinstance(v, int):
        if v.bit_length() > MAX_BITS:
            return _flt(v)
        return Fraction(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise CalcError("the result is too large or not a number")
        return v
    raise CalcError("not a number")


def _flt(v) -> float:
    try:
        f = float(v)
    except OverflowError:
        raise CalcError("the result is too large") from None
    if not math.isfinite(f):
        raise CalcError("the result is too large or not a number")
    return f


def num(v):
    """A number from a Python value or decimal string, exactly where possible."""
    if isinstance(v, str):
        s = v.strip().replace("_", "")
        if not re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", s) or len(s) > 400:
            raise CalcError(f"not a number: {v[:40]!r}")
        m = re.search(r"[eE]([+-]?\d+)$", s)
        if m and abs(int(m.group(1))) > MAX_LITERAL_EXP:
            raise CalcError("number out of range")
        return _fix(Fraction(s))
    return _fix(v)


class Q:
    """A quantity: value in SI base units (Fraction or float) and its dimension."""
    __slots__ = ("v", "d")

    def __init__(self, v, d=D0):
        self.v = _fix(v)
        self.d = tuple(d)

    def __repr__(self):
        return f"Q({self.v!r}, {self.d!r})"

    def _same(self, o, what):
        if self.d != o.d:
            raise DimensionError(f"can't {what} {dim_name(self.d)} and {dim_name(o.d)}")

    def __add__(self, o):
        self._same(o, "add")
        return Q(_add(self.v, o.v), self.d)

    def __sub__(self, o):
        self._same(o, "subtract")
        return Q(_add(self.v, _neg(o.v)), self.d)

    def __mul__(self, o):
        return Q(_mul(self.v, o.v), _dmul(self.d, o.d))

    def __truediv__(self, o):
        if o.v == 0:
            raise CalcError("division by zero")
        return Q(_div(self.v, o.v), _ddiv(self.d, o.d))

    def __neg__(self):
        return Q(_neg(self.v), self.d)

    def pow(self, e: "Q") -> "Q":
        if e.d != D0:
            raise DimensionError("an exponent must be a plain number")
        ev = e.v
        if isinstance(ev, float) and ev.is_integer() and abs(ev) <= 1024:
            ev = Fraction(int(ev))
        nd = self.d
        if self.d != D0:
            fe = ev if isinstance(ev, Fraction) else Fraction(ev).limit_denominator(12)
            if isinstance(ev, float) and abs(float(fe) - ev) > 1e-12:
                raise DimensionError("a quantity with units can only be raised to a simple fraction")
            nd = tuple(x * fe for x in self.d)
            if any(x.denominator != 1 for x in nd):
                raise DimensionError(f"{dim_name(self.d)} to the power {fe} has no meaning")
            nd = tuple(int(x) for x in nd)
            ev = fe if isinstance(ev, float) else ev
        return Q(_pow(self.v, ev), nd)


def _add(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return _flt(_flt(a) + _flt(b))
    return _fix(a + b)


def _neg(a):
    return -a


def _mul(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return _flt(_flt(a) * _flt(b))
    if a.numerator.bit_length() + b.numerator.bit_length() > 2 * MAX_BITS:
        return _flt(_flt(a) * _flt(b))
    return _fix(a * b)


def _div(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return _flt(_flt(a) / _flt(b))
    return _fix(a / b)


def _iroot(n: int, k: int):
    if n < 0 or n.bit_length() > MAX_BITS:
        return None
    r = round(n ** (1.0 / k)) if n.bit_length() < 1000 else None
    if r is None:
        return None
    for c in (r - 1, r, r + 1):
        if c >= 0 and c ** k == n:
            return c
    return None


def _pow(b, e):
    if isinstance(e, Fraction) and e.denominator in (2, 3) and isinstance(b, Fraction) and b >= 0:
        rn, rd = _iroot(b.numerator, e.denominator), _iroot(b.denominator, e.denominator)
        if rn is not None and rd is not None:
            return _pow(Fraction(rn, rd), Fraction(e.numerator))
    if isinstance(e, Fraction) and e.denominator == 1 and isinstance(b, Fraction):
        n = e.numerator
        if b == 0 and n < 0:
            raise CalcError("division by zero")
        bits = max(b.numerator.bit_length(), b.denominator.bit_length())
        if abs(n) <= 1024 and bits * abs(n) <= 2 * MAX_BITS:
            return _fix(b ** n)
    fb, fe = _flt(b), _flt(e)
    if fb < 0 and not fe.is_integer():
        raise CalcError("a negative number to a fractional power is not a real number")
    if fb == 0 and fe < 0:
        raise CalcError("division by zero")
    try:
        return _flt(math.pow(fb, fe))
    except (OverflowError, ValueError):
        raise CalcError("the result is too large") from None


def to_float(v) -> float:
    return _flt(v)


# ── units ──
class Unit:
    __slots__ = ("factor", "d", "offset")

    def __init__(self, factor, d, offset=0):
        self.factor, self.d, self.offset = _fix(factor), d, Fraction(offset)


Fr = Fraction
PI = math.pi
_LB = Fr("0.45359237")
_G0 = Fr("9.80665")
_IN = Fr("0.0254")
_FT = Fr("0.3048")
_GAL = Fr("231") * _IN ** 3              # US liquid gallon, exactly 3.785411784 L

UNITS: dict[str, Unit] = {}
PREFIXABLE: set[str] = set()


def _u(names, factor, d, offset=0, prefix=False):
    for n in names.split():
        UNITS[n] = Unit(factor, d, offset)
        if prefix:
            PREFIXABLE.add(n)


_N = dim(kg=1, m=1, s=-2)
_J = dim(kg=1, m=2, s=-2)
_W = dim(kg=1, m=2, s=-3)
_PA = dim(kg=1, m=-1, s=-2)
_V = dim(kg=1, m=2, s=-3, A=-1)
_OHM = dim(kg=1, m=2, s=-3, A=-2)

_u("m", 1, dim(m=1), prefix=True)
_u("meter meters metre metres", 1, dim(m=1))
_u("g", Fr(1, 1000), dim(kg=1), prefix=True)
_u("gram grams", Fr(1, 1000), dim(kg=1))
_u("kilogram kilograms", 1, dim(kg=1))
_u("s", 1, dim(s=1), prefix=True)
_u("sec secs second seconds", 1, dim(s=1))
_u("A", 1, dim(A=1), prefix=True)
_u("amp amps ampere amperes", 1, dim(A=1))
_u("K", 1, dim(K=1), prefix=True)
_u("kelvin", 1, dim(K=1))
_u("mol", 1, dim(mol=1), prefix=True)
_u("cd", 1, dim(cd=1))
_u("N", 1, _N, prefix=True)
_u("newton newtons", 1, _N)
_u("J", 1, _J, prefix=True)
_u("joule joules", 1, _J)
_u("W", 1, _W, prefix=True)
_u("watt watts", 1, _W)
_u("Pa", 1, _PA, prefix=True)
_u("Hz", 1, dim(s=-1), prefix=True)
_u("Bq", 1, dim(s=-1), prefix=True)
_u("C", 1, dim(A=1, s=1), prefix=True)
_u("coulomb coulombs", 1, dim(A=1, s=1))
_u("V", 1, _V, prefix=True)
_u("volt volts", 1, _V)
_u("ohm Ω", 1, _OHM, prefix=True)
_u("ohms", 1, _OHM)
_u("S", 1, dim(kg=-1, m=-2, s=3, A=2), prefix=True)
_u("F", 1, dim(kg=-1, m=-2, s=4, A=2), prefix=True)
_u("farad farads", 1, dim(kg=-1, m=-2, s=4, A=2))
_u("H", 1, dim(kg=1, m=2, s=-2, A=-2), prefix=True)
_u("T", 1, dim(kg=1, s=-2, A=-1), prefix=True)
_u("Wb", 1, dim(kg=1, m=2, s=-2, A=-1), prefix=True)
_u("Gy Sv", 1, dim(m=2, s=-2), prefix=True)
_u("L l", Fr(1, 1000), dim(m=3), prefix=True)
_u("liter liters litre litres", Fr(1, 1000), dim(m=3))
_u("cc", Fr(1, 1000000), dim(m=3))
_u("Wh", 3600, _J, prefix=True)
_u("Ah", 3600, dim(A=1, s=1), prefix=True)
_u("eV", Fr("1.602176634e-19"), _J, prefix=True)
_u("bar", 100000, _PA, prefix=True)
_u("cal", Fr("4.184"), _J, prefix=True)
_u("Cal kcal", Fr("4184"), _J)
_u("t tonne tonnes", 1000, dim(kg=1))
# time
_u("min mins minute minutes", 60, dim(s=1))
_u("h hr hrs hour hours", 3600, dim(s=1))
_u("d day days", 86400, dim(s=1))
_u("wk week weeks", 604800, dim(s=1))
_u("yr year years", Fr(3155760, 100) * 1000, dim(s=1))          # Julian year, 365.25 d
# US customary length/area/volume
_u("in inch inches", _IN, dim(m=1))
_u("ft foot feet", _FT, dim(m=1))
_u("yd yard yards", 3 * _FT, dim(m=1))
_u("mi mile miles", 5280 * _FT, dim(m=1))
_u("nmi", 1852, dim(m=1))
_u("mil thou", _IN / 1000, dim(m=1))
_u("ac acre acres", 43560 * _FT ** 2, dim(m=2))
_u("ha hectare hectares", 10000, dim(m=2))
_u("gal gallon gallons", _GAL, dim(m=3))
_u("qt quart quarts", _GAL / 4, dim(m=3))
_u("pt pint pints", _GAL / 8, dim(m=3))
_u("cup cups", _GAL / 16, dim(m=3))
_u("floz", _GAL / 128, dim(m=3))
_u("tbsp", _GAL / 256, dim(m=3))
_u("tsp", _GAL / 768, dim(m=3))
_u("gpm", _GAL / 60, dim(m=3, s=-1))
_u("cfm", _FT ** 3 / 60, dim(m=3, s=-1))
# mass / force / pressure / energy / power
_u("lb lbs pound pounds", _LB, dim(kg=1))
_u("oz ounce ounces", _LB / 16, dim(kg=1))
_u("ton tons", 2000 * _LB, dim(kg=1))
_u("lbf", _LB * _G0, _N)
_u("kgf", _G0, _N)
_u("psi", _LB * _G0 / _IN ** 2, _PA)
_u("atm", 101325, _PA)
_u("torr", Fr(101325, 760), _PA)
_u("mmHg", Fr("133.322387415"), _PA)
_u("inHg", Fr("3386.389"), _PA)
_u("BTU btu", Fr("1055.05585262"), _J)
_u("therm", Fr("105505585.262"), _J)
_u("hp", 550 * _FT * _LB * _G0, _W)
# speed, rotation, angles, ratios
_u("mph", 5280 * _FT / 3600, dim(m=1, s=-1))
_u("kph kmh", Fr(1000, 3600), dim(m=1, s=-1))
_u("kn knot knots kt", Fr(1852, 3600), dim(m=1, s=-1))
_u("rpm", Fr(1, 60), dim(s=-1))
_u("rad radian radians", 1, D0)
_u("deg degree degrees °", PI / 180, D0)
_u("rev turn turns", 2 * PI, D0)
_u("percent", Fr(1, 100), D0)
_u("ppm", Fr(1, 1000000), D0)
# temperature: absolute when alone after a number, a difference inside a compound unit
_u("degC °C celsius", 1, dim(K=1), offset=Fr("273.15"))
_u("degF °F fahrenheit", Fr(5, 9), dim(K=1), offset=Fr(45967, 180))
_u("degR rankine", Fr(5, 9), dim(K=1))
_u("Cdeg dC", 1, dim(K=1))                          # explicit temperature differences
_u("Fdeg dF", Fr(5, 9), dim(K=1))

PREFIXES = {"Y": Fr(10) ** 24, "Z": Fr(10) ** 21, "E": Fr(10) ** 18, "P": Fr(10) ** 15, "T": Fr(10) ** 12,
            "G": Fr(10) ** 9, "M": Fr(10) ** 6, "k": Fr(10) ** 3, "h": Fr(100), "da": Fr(10), "d": Fr(1, 10),
            "c": Fr(1, 100), "m": Fr(1, 1000), "u": Fr(1, 10 ** 6), "µ": Fr(1, 10 ** 6), "μ": Fr(1, 10 ** 6),
            "n": Fr(1, 10 ** 9), "p": Fr(1, 10 ** 12), "f": Fr(1, 10 ** 15), "a": Fr(1, 10 ** 18)}


def find_unit(name: str) -> Unit | None:
    u = UNITS.get(name)
    if u:
        return u
    for plen in (2, 1):
        p, rest = name[:plen], name[plen:]
        if p in PREFIXES and rest in PREFIXABLE:
            b = UNITS[rest]
            return Unit(_mul(b.factor, PREFIXES[p]), b.d)
    return None


# ── constants ──
CONSTANTS: dict[str, tuple] = {
    # name: (value, unit expression, description)
    "pi": (PI, "", "π"),
    "π": (PI, "", "π"),
    "e": (math.e, "", "Euler's number"),
    "c": (299792458, "m/s", "speed of light in vacuum (exact)"),
    "g0": (_G0, "m/s^2", "standard gravity (exact)"),
    "gravity": (_G0, "m/s^2", "standard gravity (exact)"),
    "G": (Fr("6.67430e-11"), "m^3/(kg*s^2)", "Newtonian constant of gravitation"),
    "h_planck": (Fr("6.62607015e-34"), "J*s", "Planck constant (exact)"),
    "k_B": (Fr("1.380649e-23"), "J/K", "Boltzmann constant (exact)"),
    "N_A": (Fr("6.02214076e23"), "1/mol", "Avogadro constant (exact)"),
    "R_gas": (Fr("8.314462618"), "J/(mol*K)", "molar gas constant"),
    "sigma_SB": (Fr("5.670374419e-8"), "W/(m^2*K^4)", "Stefan–Boltzmann constant"),
    "q_e": (Fr("1.602176634e-19"), "C", "elementary charge (exact)"),
    "m_e": (Fr("9.1093837015e-31"), "kg", "electron mass"),
    "m_p": (Fr("1.67262192369e-27"), "kg", "proton mass"),
    "eps0": (Fr("8.8541878128e-12"), "F/m", "vacuum permittivity"),
    "mu0": (Fr("1.25663706212e-6"), "N/A^2", "vacuum permeability"),
    "atm0": (101325, "Pa", "standard atmosphere (exact)"),
    "rho_water": (1000, "kg/m^3", "density of fresh water (about 4 °C; 998 at 20 °C)"),
    "rho_seawater": (1025, "kg/m^3", "density of sea water (typical)"),
    "rho_air": (Fr("1.225"), "kg/m^3", "density of air at sea level, 15 °C"),
    "c_water": (4186, "J/(kg*K)", "specific heat of liquid water"),
    "c_ice": (2108, "J/(kg*K)", "specific heat of ice"),
    "L_vap_water": (2257000, "J/kg", "latent heat of vaporisation of water at 100 °C"),
    "L_fus_water": (334000, "J/kg", "latent heat of fusion of ice"),
    "R_earth": (6371000, "m", "mean radius of the Earth"),
    "M_earth": (Fr("5.9722e24"), "kg", "mass of the Earth"),
    "solar_constant": (1361, "W/m^2", "sunlight above the atmosphere; about 1000 W/m² reaches the ground at noon"),
    "rho_copper": (Fr("1.68e-8"), "ohm*m", "resistivity of copper at 20 °C"),
    "rho_aluminium": (Fr("2.65e-8"), "ohm*m", "resistivity of aluminium at 20 °C"),
    "sound_speed_air": (343, "m/s", "speed of sound in air at 20 °C"),
}


def constant_q(name: str) -> Q:
    v, unit, _ = CONSTANTS[name]
    if not unit:
        return Q(v)
    return Q(v) * unit_q(unit)


def search_constants(text: str, limit: int = 8) -> list[dict]:
    t = text.strip().lower()
    out = []
    for name, (v, unit, desc) in CONSTANTS.items():
        if name == "π":
            continue
        if not t or t == name.lower() or t in desc.lower() or t in name.lower():
            out.append({"name": name, "value": fmt_num(Q(v).v), "unit": unit, "description": desc})
    exact = [o for o in out if o["name"].lower() == t]
    return (exact or out)[:limit]


# ── parser ──
_SUPER = str.maketrans({"²": "^2", "³": "^3", "¹": "^1", "⁴": "^4", "⁰": "^0", "×": "*", "·": "*", "÷": "/",
                        "−": "-", "–": "-"})
_TOKEN = re.compile(r"\s*(?:(?P<num>(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?)"
                    r"|(?P<id>[A-Za-z_°µμΩπ][A-Za-z0-9_°µμΩ]*)"
                    r"|(?P<op>\*\*|[-+*/^(),%]))")
FUNCS = {"sqrt", "cbrt", "abs", "exp", "ln", "log", "log10", "log2", "sin", "cos", "tan", "asin", "acos",
         "atan", "atan2", "sinh", "cosh", "tanh", "min", "max", "round", "floor", "ceil", "hypot",
         "factorial", "comb", "perm", "gcd", "deg2rad", "rad2deg"}


def _tokens(s: str) -> list[tuple[str, str]]:
    s = s.translate(_SUPER).replace("⁻^", "^-").replace("⁻", "^-")
    out, pos = [], 0
    while pos < len(s):
        if s[pos:].strip() == "":
            break
        m = _TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise CalcError(f"can't read the expression at {s[pos:pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        if len(out) > MAX_TOKENS:
            raise CalcError("the expression is too long")
    return out


class _Parser:
    def __init__(self, text: str):
        if not isinstance(text, str):
            raise CalcError("the expression must be text")
        if len(text) > MAX_EXPR:
            raise CalcError(f"the expression is too long (limit {MAX_EXPR} characters)")
        self.t = _tokens(text)
        self.i = 0
        self.depth = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, op):
        k, v = self.take()
        if v != op or k != "op":
            raise CalcError(f"expected {op!r}")

    def parse(self) -> Q:
        if not self.t:
            raise CalcError("empty expression")
        q = self.sum()
        if self.i != len(self.t):
            raise CalcError(f"unexpected {self.peek()[1]!r}")
        return q

    def _enter(self):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CalcError("the expression is nested too deeply")

    def sum(self) -> Q:
        self._enter()
        left = self.product()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            right = self.product()
            left = left + right if op == "+" else left - right
        self.depth -= 1
        return left

    def product(self) -> Q:
        start = self.i
        left = self.juxt()
        plain = self.i == start + 1 and self.t[start][0] == "num"
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else (None, None)
            if op == "/" and plain and self.peek()[0] == "num" and nxt[0] == "id" and nxt[1] not in FUNCS:
                # "3/4 in", "1/2 cup": a fraction of a unit, as people write it
                left = self.juxt_tail(left / self.atom())
            else:
                right = self.juxt()
                left = left * right if op == "*" else left / right
            plain = False
        return left

    def _starts_atom(self):
        k, v = self.peek()
        return k in ("num", "id") or (k == "op" and v == "(")

    def juxt(self) -> Q:
        """Implicit multiplication ("60 W", "2 (3+4)") binds tighter than * and /,
        so "60 W / 12 V" is (60 W)/(12 V)."""
        left, plain = self.unary()
        return self.juxt_tail(left, plain)

    def juxt_tail(self, left: Q, plain=None) -> Q:
        if plain is None and left.d == D0 and self.peek()[0] == "id":
            plain = left.v                        # "(3/4) degC" after a fraction: still a plain number
        while self._starts_atom():
            k, v = self.peek()
            nxt = self.t[self.i + 1] if self.i + 1 < len(self.t) else (None, None)
            u = UNITS.get(v) if k == "id" else None
            if plain is not None and u is not None and u.offset and nxt not in (("op", "^"), ("op", "**")):
                self.take()                       # "20 degC": an absolute temperature
                left, plain = Q(_add(_mul(plain, u.factor), u.offset), u.d), None
                continue
            right = self.power()
            left, plain = left * right, None
        return left

    def unary(self):
        k, v = self.peek()
        if k == "op" and v in ("+", "-"):
            self.take()
            self._enter()
            q, plain = self.unary()
            self.depth -= 1
            if v == "-":
                return -q, (None if plain is None else _neg(plain))
            return q, plain
        k2 = self.peek()[0]
        q = self.power()
        plain = q.v if (k2 == "num" and q.d == D0 and self.t[self.i - 1][0] == "num") else None
        return q, plain

    def power(self) -> Q:
        base = self.atom()
        if self.peek() == ("op", "%"):
            self.take()
            base = base * Q(Fr(1, 100))
        if self.peek() in (("op", "^"), ("op", "**")):
            self.take()
            self._enter()
            e, _ = self.unary()
            self.depth -= 1
            return base.pow(e)
        return base

    def atom(self) -> Q:
        k, v = self.take()
        if k == "num":
            return Q(num(v))
        if k == "op" and v == "(":
            q = self.sum()
            self.expect(")")
            return q
        if k == "id":
            if v in FUNCS and self.peek() == ("op", "("):
                self.take()
                args = []
                if self.peek() != ("op", ")"):
                    args.append(self.sum())
                    while self.peek() == ("op", ","):
                        self.take()
                        args.append(self.sum())
                        if len(args) > 20:
                            raise CalcError("too many arguments")
                self.expect(")")
                return _call(v, args)
            if v in CONSTANTS:
                return constant_q(v)
            u = find_unit(v)
            if u is not None:
                return Q(u.factor, u.d)
            m = re.fullmatch(r"(.*?[A-Za-zΩ])(\d)", v)                     # "m2", "ft3"
            if m and m.group(1) not in CONSTANTS:
                u = find_unit(m.group(1))
                if u is not None:
                    return Q(u.factor, u.d).pow(Q(int(m.group(2))))
            raise CalcError(f"unknown name {v[:30]!r} (not a unit, constant or function)")
        raise CalcError(f"unexpected {v!r}" if v else "the expression ends too early")


def _plain(q: Q, fn: str) -> float:
    if q.d != D0:
        raise DimensionError(f"{fn}() needs a plain number, not {dim_name(q.d)}")
    return to_float(q.v)


def _call(fn: str, args: list[Q]) -> Q:
    n = len(args)

    def need(k):
        if n != k:
            raise CalcError(f"{fn}() takes {k} argument{'s' if k > 1 else ''}")
    try:
        if fn in ("min", "max"):
            if not args:
                raise CalcError(f"{fn}() needs arguments")
            for a in args[1:]:
                args[0]._same(a, "compare")
            vals = [to_float(a.v) for a in args]
            idx = vals.index(min(vals) if fn == "min" else max(vals))
            return args[idx]
        if fn == "hypot":
            if not args:
                raise CalcError("hypot() needs arguments")
            for a in args[1:]:
                args[0]._same(a, "combine")
            return Q(math.hypot(*[to_float(a.v) for a in args]), args[0].d)
        if fn == "atan2":
            need(2)
            args[0]._same(args[1], "compare")
            return Q(math.atan2(to_float(args[0].v), to_float(args[1].v)))
        if fn in ("comb", "perm", "gcd"):
            need(2)
            a, b = (_plain(x, fn) for x in args)
            if not (a.is_integer() and b.is_integer()) or a < 0 or b < 0 or a > 10000 or b > 10000:
                raise CalcError(f"{fn}() needs whole numbers from 0 to 10000")
            return Q({"comb": math.comb, "perm": math.perm, "gcd": math.gcd}[fn](int(a), int(b)))
        if fn == "round":
            if n not in (1, 2):
                raise CalcError("round() takes 1 or 2 arguments")
            nd = int(_plain(args[1], fn)) if n == 2 else 0
            if not -20 <= nd <= 20:
                raise CalcError("round() to at most 20 places")
            v = args[0].v
            return Q(round(v, nd) if isinstance(v, Fraction) else round(v, nd), args[0].d)
        need(1)
        a = args[0]
        if fn == "abs":
            return Q(abs(a.v), a.d)
        if fn == "sqrt":
            return a.pow(Q(Fr(1, 2)))
        if fn == "cbrt":
            if a.d == D0 and to_float(a.v) < 0:
                return Q(-(-to_float(a.v)) ** (1 / 3))
            return a.pow(Q(Fr(1, 3)))
        if fn in ("floor", "ceil"):
            f = math.floor if fn == "floor" else math.ceil
            return Q(f(a.v), a.d)
        x = _plain(a, fn)
        if fn == "factorial":
            if not x.is_integer() or not 0 <= x <= 300:
                raise CalcError("factorial() needs a whole number from 0 to 300")
            return Q(math.factorial(int(x)))
        f = {"exp": math.exp, "ln": math.log, "log": math.log10, "log10": math.log10, "log2": math.log2,
             "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
             "atan": math.atan, "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
             "deg2rad": math.radians, "rad2deg": math.degrees}[fn]
        return Q(f(x))
    except (ValueError, OverflowError) as e:
        if isinstance(e, CalcError):
            raise
        raise CalcError(f"{fn}(): {e}") from None


def evaluate(expr: str) -> Q:
    return _Parser(expr).parse()


def unit_q(unit: str) -> Q:
    """The size of a unit expression ("m/s", "kW*h", "1/min") as a quantity."""
    return _Parser(unit).parse()


def _lone_offset(unit: str) -> Unit | None:
    u = UNITS.get(unit.strip())
    return u if u is not None and u.offset else None


def convert_q(q: Q, unit: str):
    """The number q is in `unit` (Fraction or float); DimensionError if it doesn't fit."""
    if not isinstance(unit, str) or not unit.strip():
        raise CalcError("say which unit to convert to")
    lone = _lone_offset(unit)
    if lone is not None:
        if q.d != lone.d:
            raise DimensionError(f"can't convert {dim_name(q.d)} to a temperature")
        return _div(_add(q.v, _neg(lone.offset)), lone.factor)
    target = unit_q(unit)
    if target.d != q.d:
        hint = ""
        if unit.strip() in ("F", "C") and q.d == dim(K=1):
            hint = " (for temperatures write degF or degC)"
        raise DimensionError(f"can't convert {dim_name(q.d)} to {dim_name(target.d)} ({unit.strip()}){hint}")
    return _div(q.v, target.v)


def quantity(value, unit: str):
    """For calculator arguments: a number (taken to be in `unit`) or text with its own unit
    ("50 ft", "3/4 in", "20 degC"), returned as a float in `unit`."""
    if isinstance(value, bool):
        raise CalcError("expected a number")
    if isinstance(value, (int, float)):
        v = float(value)
        if not math.isfinite(v):
            raise CalcError("expected a finite number")
        return v
    if not isinstance(value, str):
        raise CalcError("expected a number or a number with a unit")
    q = evaluate(value)
    if q.d == D0 and not re.search(r"[A-Za-z_°µμΩ%]", value.replace("e", "")):
        return to_float(q.v)                         # a bare number in text: take the default unit
    return to_float(convert_q(q, unit))


# ── output ──

def fmt_num(v, sig: int = 10) -> str:
    if isinstance(v, Fraction):
        if v.denominator == 1:
            s = str(v.numerator)
            return s if len(s) <= 40 else f"{float(v):.{sig}g}"
        f = float(v)
        dec = f"{f:.{sig}g}"
        if v.denominator <= 10 ** 6 and len(str(v.numerator)) <= 9:
            if Fraction(dec) == v:
                return dec
            return f"{v.numerator}/{v.denominator} ≈ {dec}"
        return dec
    return f"{v:.{sig}g}"


NAMED = [("N", _N), ("J", _J), ("W", _W), ("Pa", _PA), ("V", _V), ("ohm", _OHM), ("C", dim(A=1, s=1)),
         ("F", dim(kg=-1, m=-2, s=4, A=2)), ("H", dim(kg=1, m=2, s=-2, A=-2)), ("T", dim(kg=1, s=-2, A=-1)),
         ("Wb", dim(kg=1, m=2, s=-2, A=-1)), ("S", dim(kg=-1, m=-2, s=3, A=2))]


def describe(q: Q) -> dict:
    """{"value": "...", "unit": "...", "dimension": "...", "text": "5 A"} in SI."""
    unit = next((n for n, d in NAMED if d == q.d), None)
    if unit is None:
        unit = "" if q.d == D0 else dim_str(q.d)
    val = fmt_num(q.v)
    return {"value": val, "unit": unit, "dimension": dim_name(q.d),
            "exact": isinstance(q.v, Fraction), "text": (val + (" " + unit if unit else "")).strip()}


_TO = re.compile(r"\s+(?:to|into|as|in units of)\s+|\s*->\s*|\s*=>\s*")


def calc(expr: str) -> dict:
    """The `calc` tool: evaluate, optionally "… to <unit>"."""
    if not isinstance(expr, str):
        raise CalcError("the expression must be text")
    last = None
    for m in _TO.finditer(expr):
        last = m
    if last is not None and last.start() > 0 and expr[last.end():].strip():
        src, unit = expr[:last.start()], expr[last.end():].strip()
        q = evaluate(src)
        v = convert_q(q, unit)
        return {"value": fmt_num(v), "unit": unit, "dimension": dim_name(q.d),
                "exact": isinstance(v, Fraction), "text": f"{fmt_num(v)} {unit}", "si": describe(q)["text"]}
    return describe(evaluate(expr))


def convert(value: str, to: str) -> dict:
    q = evaluate(value) if isinstance(value, str) else Q(num(value))
    v = convert_q(q, to)
    return {"value": fmt_num(v), "unit": to.strip(), "dimension": dim_name(q.d), "text": f"{fmt_num(v)} {to.strip()}",
            "exact": isinstance(v, Fraction)}
