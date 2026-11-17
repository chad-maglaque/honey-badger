"""
orbit — pure-Python SGP4 orbit propagation and satellite pass prediction (stdlib only).

This is the near-Earth branch of SGP4 as published by Vallado, Crawford, Hujsak & Kelso,
"Revisiting Spacetrack Report #3" (AIAA 2006-6753), with WGS-72 constants and the
"improved" (opsmode "i") sidereal time. Weather satellites (Meteor-M, ~830 km, ~101 min
period) are near-Earth orbits; deep-space orbits (period >= 225 min) are refused, not
silently mispredicted.

    sat = parse_tle(line1, line2, name="METEOR-M2 3")
    r, v = sat.propagate_jd(jd)                 # TEME position (km) and velocity (km/s)
    for p in passes(sat, lat, lon, alt_m, start_epoch, hours=24, min_elev=10): ...

Accuracy: SGP4 with a fresh TLE is good to about a kilometre, i.e. pass times to a few
seconds; error grows by roughly 1-3 km per day of TLE age. Topocentric look angles ignore
polar motion and UT1-UTC (both negligible for pointing a fixed antenna).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# WGS-72 (the constants TLEs are fitted with)
MU = 398600.8
RE = 6378.135                                   # km
XKE = 60.0 / math.sqrt(RE * RE * RE / MU)
J2 = 0.001082616
J3 = -0.00000253881
J4 = -0.00000165597
J3OJ2 = J3 / J2
TWOPI = 2.0 * math.pi
X2O3 = 2.0 / 3.0
VKMPERSEC = RE * XKE / 60.0
DEG = math.pi / 180.0
MIN_PER_DAY = 1440.0
JD_UNIX = 2440587.5                             # Julian date of 1970-01-01 00:00 UTC

# WGS-84, for the observer
WGS84_A = 6378.137
WGS84_F = 1.0 / 298.257223563
OMEGA_EARTH = 7.292115e-5                       # rad/s


class TLEError(ValueError):
    """A TLE that cannot be used (bad format, checksum, deep-space orbit...)."""


class PropagationError(ValueError):
    """SGP4 could not propagate to this time (decayed, eccentricity out of range...)."""


def checksum(line: str) -> int:
    """TLE line checksum: sum of the digits, minus signs count 1, modulo 10 (column 69 holds it)."""
    s = 0
    for c in line[:68]:
        if c.isdigit():
            s += int(c)
        elif c == "-":
            s += 1
    return s % 10


def _implied(field: str) -> float:
    """TLE 'implied decimal point' numbers: ' 66816-4' -> 0.66816e-4, '-11606-4' -> -0.11606e-4."""
    f = field.strip()
    if not f:
        return 0.0
    sign = -1.0 if f[0] == "-" else 1.0
    f = f.lstrip("+-")
    if len(f) < 2 or f[-2] not in "+-":
        raise TLEError(f"bad exponent field {field!r}")
    mant, exp = f[:-2], int(f[-2:])
    if not mant.isdigit():
        raise TLEError(f"bad mantissa {field!r}")
    return sign * float("0." + mant) * 10.0 ** exp


def jday(year: int, mon: int, day: int, hr: int, minute: int, sec: float) -> float:
    """Julian date (Vallado's jday, valid 1900-2100)."""
    return (367.0 * year - int(7 * (year + int((mon + 9) / 12.0)) * 0.25) + int(275 * mon / 9.0)
            + day + 1721013.5 + ((sec / 60.0 + minute) / 60.0 + hr) / 24.0)


def gstime(jdut1: float) -> float:
    """Greenwich mean sidereal time (radians), IAU-82."""
    tut1 = (jdut1 - 2451545.0) / 36525.0
    temp = (-6.2e-6 * tut1 * tut1 * tut1 + 0.093104 * tut1 * tut1
            + (876600.0 * 3600 + 8640184.812866) * tut1 + 67310.54841)
    temp = (temp * DEG / 240.0) % TWOPI
    return temp + TWOPI if temp < 0.0 else temp


def jd_from_unix(t: float) -> float:
    return t / 86400.0 + JD_UNIX


def unix_from_jd(jd: float) -> float:
    return (jd - JD_UNIX) * 86400.0


@dataclass
class Satellite:
    name: str
    norad: int
    line1: str
    line2: str
    epoch_jd: float
    # everything below is filled by _init()
    bstar: float = 0.0
    inclo: float = 0.0
    nodeo: float = 0.0
    ecco: float = 0.0
    argpo: float = 0.0
    mo: float = 0.0
    no_kozai: float = 0.0

    @property
    def epoch_unix(self) -> float:
        return unix_from_jd(self.epoch_jd)

    @property
    def period_min(self) -> float:
        return TWOPI / self.no_unkozai

    def _init(self) -> None:
        """sgp4init, near-Earth branch."""
        ecco, inclo, no_kozai = self.ecco, self.inclo, self.no_kozai
        ss = 78.0 / RE + 1.0
        qzms2t = ((120.0 - 78.0) / RE) ** 4
        # initl
        eccsq = ecco * ecco
        omeosq = 1.0 - eccsq
        rteosq = math.sqrt(omeosq)
        cosio = math.cos(inclo)
        cosio2 = cosio * cosio
        ak = (XKE / no_kozai) ** X2O3
        d1 = 0.75 * J2 * (3.0 * cosio2 - 1.0) / (rteosq * omeosq)
        del_ = d1 / (ak * ak)
        adel = ak * (1.0 - del_ * del_ - del_ * (1.0 / 3.0 + 134.0 * del_ * del_ / 81.0))
        del_ = d1 / (adel * adel)
        self.no_unkozai = no_kozai / (1.0 + del_)
        ao = (XKE / self.no_unkozai) ** X2O3
        sinio = math.sin(inclo)
        po = ao * omeosq
        con42 = 1.0 - 5.0 * cosio2
        self.con41 = -con42 - cosio2 - cosio2
        posq = po * po
        rp = ao * (1.0 - ecco)
        if TWOPI / self.no_unkozai >= 225.0:
            raise TLEError("deep-space orbit (period of 225 minutes or more) — not supported")
        if omeosq < 0.0 or self.no_unkozai < 0.0:
            raise TLEError("orbit elements out of range")
        self.isimp = 1 if rp < (220.0 / RE + 1.0) else 0
        sfour, qzms24 = ss, qzms2t
        perige = (rp - 1.0) * RE
        if perige < 156.0:
            sfour = perige - 78.0
            if perige < 98.0:
                sfour = 20.0
            qzms24 = ((120.0 - sfour) / RE) ** 4
            sfour = sfour / RE + 1.0
        pinvsq = 1.0 / posq
        tsi = 1.0 / (ao - sfour)
        self.eta = eta = ao * ecco * tsi
        etasq = eta * eta
        eeta = ecco * eta
        psisq = abs(1.0 - etasq)
        coef = qzms24 * tsi ** 4
        coef1 = coef / psisq ** 3.5
        n = self.no_unkozai
        cc2 = coef1 * n * (ao * (1.0 + 1.5 * etasq + eeta * (4.0 + etasq))
                           + 0.375 * J2 * tsi / psisq * self.con41 * (8.0 + 3.0 * etasq * (8.0 + etasq)))
        self.cc1 = cc1 = self.bstar * cc2
        cc3 = 0.0
        if ecco > 1.0e-4:
            cc3 = -2.0 * coef * tsi * J3OJ2 * n * sinio / ecco
        self.x1mth2 = 1.0 - cosio2
        self.cc4 = 2.0 * n * coef1 * ao * omeosq * (
            eta * (2.0 + 0.5 * etasq) + ecco * (0.5 + 2.0 * etasq)
            - J2 * tsi / (ao * psisq) * (-3.0 * self.con41 * (1.0 - 2.0 * eeta + etasq * (1.5 - 0.5 * eeta))
                                        + 0.75 * self.x1mth2 * (2.0 * etasq - eeta * (1.0 + etasq))
                                        * math.cos(2.0 * self.argpo)))
        self.cc5 = 2.0 * coef1 * ao * omeosq * (1.0 + 2.75 * (etasq + eeta) + eeta * etasq)
        cosio4 = cosio2 * cosio2
        temp1 = 1.5 * J2 * pinvsq * n
        temp2 = 0.5 * temp1 * J2 * pinvsq
        temp3 = -0.46875 * J4 * pinvsq * pinvsq * n
        self.mdot = n + 0.5 * temp1 * rteosq * self.con41 + 0.0625 * temp2 * rteosq * (
            13.0 - 78.0 * cosio2 + 137.0 * cosio4)
        self.argpdot = (-0.5 * temp1 * con42 + 0.0625 * temp2 * (7.0 - 114.0 * cosio2 + 395.0 * cosio4)
                        + temp3 * (3.0 - 36.0 * cosio2 + 49.0 * cosio4))
        xhdot1 = -temp1 * cosio
        self.nodedot = xhdot1 + (0.5 * temp2 * (4.0 - 19.0 * cosio2) + 2.0 * temp3 * (3.0 - 7.0 * cosio2)) * cosio
        self.omgcof = self.bstar * cc3 * math.cos(self.argpo)
        self.xmcof = -X2O3 * coef * self.bstar / eeta if ecco > 1.0e-4 else 0.0
        self.nodecf = 3.5 * omeosq * xhdot1 * cc1
        self.t2cof = 1.5 * cc1
        den = (1.0 + cosio) if abs(cosio + 1.0) > 1.5e-12 else 1.5e-12
        self.xlcof = -0.25 * J3OJ2 * sinio * (3.0 + 5.0 * cosio) / den
        self.aycof = -0.5 * J3OJ2 * sinio
        self.delmo = (1.0 + eta * math.cos(self.mo)) ** 3
        self.sinmao = math.sin(self.mo)
        self.x7thm1 = 7.0 * cosio2 - 1.0
        self.d2 = self.d3 = self.d4 = self.t3cof = self.t4cof = self.t5cof = 0.0
        if self.isimp != 1:
            cc1sq = cc1 * cc1
            self.d2 = 4.0 * ao * tsi * cc1sq
            temp = self.d2 * tsi * cc1 / 3.0
            self.d3 = (17.0 * ao + sfour) * temp
            self.d4 = 0.5 * temp * ao * tsi * (221.0 * ao + 31.0 * sfour) * cc1
            self.t3cof = self.d2 + 2.0 * cc1sq
            self.t4cof = 0.25 * (3.0 * self.d3 + cc1 * (12.0 * self.d2 + 10.0 * cc1sq))
            self.t5cof = 0.2 * (3.0 * self.d4 + 12.0 * cc1 * self.d3 + 6.0 * self.d2 * self.d2
                                + 15.0 * cc1sq * (2.0 * self.d2 + cc1sq))
        self.propagate(0.0)                     # as sgp4init does: catches unusable elements now

    def propagate(self, tsince: float) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Position (km) and velocity (km/s) in the TEME frame, `tsince` minutes after the TLE epoch."""
        t = tsince
        xmdf = self.mo + self.mdot * t
        argpdf = self.argpo + self.argpdot * t
        nodedf = self.nodeo + self.nodedot * t
        argpm, mm = argpdf, xmdf
        t2 = t * t
        nodem = nodedf + self.nodecf * t2
        tempa = 1.0 - self.cc1 * t
        tempe = self.bstar * self.cc4 * t
        templ = self.t2cof * t2
        if self.isimp != 1:
            delomg = self.omgcof * t
            delmtemp = 1.0 + self.eta * math.cos(xmdf)
            delm = self.xmcof * (delmtemp * delmtemp * delmtemp - self.delmo)
            temp = delomg + delm
            mm = xmdf + temp
            argpm = argpdf - temp
            t3 = t2 * t
            t4 = t3 * t
            tempa = tempa - self.d2 * t2 - self.d3 * t3 - self.d4 * t4
            tempe = tempe + self.bstar * self.cc5 * (math.sin(mm) - self.sinmao)
            templ = templ + self.t3cof * t3 + t4 * (self.t4cof + t * self.t5cof)
        nm = self.no_unkozai
        em = self.ecco
        inclm = self.inclo
        if nm <= 0.0:
            raise PropagationError("mean motion went negative")
        am = (XKE / nm) ** X2O3 * tempa * tempa
        nm = XKE / am ** 1.5
        em = em - tempe
        if em >= 1.0 or em < -0.001:
            raise PropagationError("eccentricity out of range (the satellite has probably decayed)")
        if em < 1.0e-6:
            em = 1.0e-6
        mm = mm + self.no_unkozai * templ
        xlm = mm + argpm + nodem
        nodem = math.fmod(nodem, TWOPI)
        argpm = math.fmod(argpm, TWOPI)
        xlm = math.fmod(xlm, TWOPI)
        mm = math.fmod(xlm - argpm - nodem, TWOPI)
        sinip, cosip = math.sin(inclm), math.cos(inclm)
        ep, xincp, argpp, nodep, mp = em, inclm, argpm, nodem, mm
        axnl = ep * math.cos(argpp)
        temp = 1.0 / (am * (1.0 - ep * ep))
        aynl = ep * math.sin(argpp) + temp * self.aycof
        xl = mp + argpp + nodep + temp * self.xlcof * axnl
        # Kepler's equation
        u = math.fmod(xl - nodep, TWOPI)
        eo1 = u
        tem5 = 9999.9
        ktr = 1
        sineo1 = coseo1 = 0.0
        while abs(tem5) >= 1.0e-12 and ktr <= 10:
            sineo1, coseo1 = math.sin(eo1), math.cos(eo1)
            tem5 = 1.0 - coseo1 * axnl - sineo1 * aynl
            tem5 = (u - aynl * coseo1 + axnl * sineo1 - eo1) / tem5
            if abs(tem5) >= 0.95:
                tem5 = 0.95 if tem5 > 0.0 else -0.95
            eo1 = eo1 + tem5
            ktr += 1
        # short-period periodics
        ecose = axnl * coseo1 + aynl * sineo1
        esine = axnl * sineo1 - aynl * coseo1
        el2 = axnl * axnl + aynl * aynl
        pl = am * (1.0 - el2)
        if pl < 0.0:
            raise PropagationError("semi-latus rectum went negative")
        rl = am * (1.0 - ecose)
        rdotl = math.sqrt(am) * esine / rl
        rvdotl = math.sqrt(pl) / rl
        betal = math.sqrt(1.0 - el2)
        temp = esine / (1.0 + betal)
        sinu = am / rl * (sineo1 - aynl - axnl * temp)
        cosu = am / rl * (coseo1 - axnl + aynl * temp)
        su = math.atan2(sinu, cosu)
        sin2u = (cosu + cosu) * sinu
        cos2u = 1.0 - 2.0 * sinu * sinu
        temp = 1.0 / pl
        temp1 = 0.5 * J2 * temp
        temp2 = temp1 * temp
        mrt = rl * (1.0 - 1.5 * temp2 * betal * self.con41) + 0.5 * temp1 * self.x1mth2 * cos2u
        su = su - 0.25 * temp2 * self.x7thm1 * sin2u
        xnode = nodep + 1.5 * temp2 * cosip * sin2u
        xinc = xincp + 1.5 * temp2 * cosip * sinip * cos2u
        mvt = rdotl - nm * temp1 * self.x1mth2 * sin2u / XKE
        rvdot = rvdotl + nm * temp1 * (self.x1mth2 * cos2u + 1.5 * self.con41) / XKE
        sinsu, cossu = math.sin(su), math.cos(su)
        snod, cnod = math.sin(xnode), math.cos(xnode)
        sini, cosi = math.sin(xinc), math.cos(xinc)
        xmx = -snod * cosi
        xmy = cnod * cosi
        ux = xmx * sinsu + cnod * cossu
        uy = xmy * sinsu + snod * cossu
        uz = sini * sinsu
        vx = xmx * cossu - cnod * sinsu
        vy = xmy * cossu - snod * sinsu
        vz = sini * cossu
        if mrt < 1.0:
            raise PropagationError("the satellite has decayed (below the Earth's surface)")
        mr = mrt * RE
        r = (mr * ux, mr * uy, mr * uz)
        v = ((mvt * ux + rvdot * vx) * VKMPERSEC, (mvt * uy + rvdot * vy) * VKMPERSEC,
             (mvt * uz + rvdot * vz) * VKMPERSEC)
        return r, v

    def propagate_jd(self, jd: float):
        return self.propagate((jd - self.epoch_jd) * MIN_PER_DAY)

    def propagate_unix(self, t: float):
        return self.propagate_jd(jd_from_unix(t))


def parse_tle(line1: str, line2: str, name: str = "", check: bool = True) -> Satellite:
    """Parse a two-line element set (fixed columns). Raises TLEError on anything malformed."""
    l1, l2 = line1.rstrip("\r\n "), line2.rstrip("\r\n ")
    if len(l1) < 64 or len(l2) < 63 or l1[0] != "1" or l2[0] != "2":
        raise TLEError("not a two-line element set")
    if any(ord(c) < 32 or ord(c) > 126 for c in l1 + l2):
        raise TLEError("TLE contains unexpected characters")
    if check:
        for ln in (l1, l2):
            if len(ln) < 69 or not ln[68].isdigit() or int(ln[68]) != checksum(ln):
                raise TLEError("TLE checksum does not match (the download may be damaged)")
    try:
        norad = int(l1[2:7])
        if int(l2[2:7]) != norad:
            raise TLEError("the two lines are for different satellites")
        yy = int(l1[18:20])
        days = float(l1[20:32])
        ndot = float(l1[33:43])                         # unused by SGP4 (kept for completeness)
        bstar = _implied(l1[53:61])
        inclo = float(l2[8:16])
        nodeo = float(l2[17:25])
        ecco = float("0." + l2[26:33].strip())
        argpo = float(l2[34:42])
        mo = float(l2[43:51])
        no = float(l2[52:63])
    except ValueError as e:
        raise TLEError(f"unreadable TLE field: {e}") from None
    del ndot
    if not (0.0 <= inclo <= 180.0 and 0.0 <= nodeo < 360.0 and 0.0 <= argpo < 360.0
            and 0.0 <= mo < 360.0 and 0.0 <= ecco < 1.0 and 0.0 < no < 20.0 and 1.0 <= days < 367.0):
        raise TLEError("orbit elements out of range")
    if not all(math.isfinite(x) for x in (bstar, inclo, nodeo, ecco, argpo, mo, no, days)):
        raise TLEError("orbit elements out of range")
    year = 2000 + yy if yy < 57 else 1900 + yy
    epoch_jd = jday(year, 1, 1, 0, 0, 0.0) + days - 1.0
    sat = Satellite(name=(name or f"NORAD {norad}").strip()[:40], norad=norad, line1=l1, line2=l2, epoch_jd=epoch_jd)
    xpdotp = MIN_PER_DAY / TWOPI
    sat.bstar = bstar
    sat.inclo, sat.nodeo, sat.argpo, sat.mo = inclo * DEG, nodeo * DEG, argpo * DEG, mo * DEG
    sat.ecco = ecco
    sat.no_kozai = no / xpdotp
    try:
        sat._init()
    except PropagationError as e:
        raise TLEError(str(e)) from None
    return sat


def parse_tle_text(text: str, check: bool = True, limit: int = 200) -> tuple[list[Satellite], list[str]]:
    """Every TLE in a block of text (2-line or 3-line "name / 1 / 2" format).
    Returns (satellites, problems). Unreadable sets are reported, not fatal."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    sats, problems, name, i = [], [], "", 0
    while i < len(lines) and len(sats) < limit:
        ln = lines[i]
        if ln.startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            try:
                sats.append(parse_tle(ln, lines[i + 1], name, check=check))
            except TLEError as e:
                problems.append(f"{name or ln[2:7].strip()}: {e}")
            name, i = "", i + 2
            continue
        name = ln.lstrip("0 ").strip()[:40] if not ln.startswith(("1 ", "2 ")) else ""
        i += 1
    return sats, problems


# ───────────────────────────── observer geometry ─────────────────────────────

def observer_ecef(lat_deg: float, lon_deg: float, alt_m: float = 0.0) -> tuple[float, float, float]:
    lat, lon = lat_deg * DEG, lon_deg * DEG
    e2 = WGS84_F * (2.0 - WGS84_F)
    n = WGS84_A / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
    h = alt_m / 1000.0
    return ((n + h) * math.cos(lat) * math.cos(lon), (n + h) * math.cos(lat) * math.sin(lon),
            (n * (1.0 - e2) + h) * math.sin(lat))


def teme_to_ecef(r, jd: float):
    """Rotate a TEME vector into the Earth-fixed frame by Greenwich mean sidereal time."""
    g = gstime(jd)
    c, s = math.cos(g), math.sin(g)
    return (c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2])


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """(lat deg, lon deg, alt km) on WGS-84, iterative."""
    e2 = WGS84_F * (2.0 - WGS84_F)
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - e2))
    h = 0.0
    for _ in range(8):
        n = WGS84_A / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n if abs(lat) < 1.5 else z / math.sin(lat) - n * (1.0 - e2)
        lat = math.atan2(z, p * (1.0 - e2 * n / (n + h)))
    return lat / DEG, lon / DEG, h


def look_angles(sat: Satellite, jd: float, lat_deg: float, lon_deg: float, alt_m: float = 0.0,
                obs=None) -> tuple[float, float, float]:
    """(azimuth deg from north, elevation deg, range km) of the satellite seen from the observer."""
    r, _ = sat.propagate_jd(jd)
    x, y, z = teme_to_ecef(r, jd)
    ox, oy, oz = obs or observer_ecef(lat_deg, lon_deg, alt_m)
    dx, dy, dz = x - ox, y - oy, z - oz
    lat, lon = lat_deg * DEG, lon_deg * DEG
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    south = sl * co * dx + sl * so * dy - cl * dz
    east = -so * dx + co * dy
    up = cl * co * dx + cl * so * dy + sl * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    el = math.asin(max(-1.0, min(1.0, up / rng))) / DEG
    az = math.atan2(east, -south) / DEG % 360.0
    return az, el, rng


def subpoint(sat: Satellite, t_unix: float) -> tuple[float, float, float]:
    """(lat, lon, alt km) of the point under the satellite."""
    jd = jd_from_unix(t_unix)
    r, _ = sat.propagate_jd(jd)
    return ecef_to_geodetic(*teme_to_ecef(r, jd))


# ───────────────────────────── pass prediction ───────────────────────────────

def _elev(sat, t, lat, lon, alt_m, obs):
    return look_angles(sat, jd_from_unix(t), lat, lon, alt_m, obs)[1]


def _cross(sat, a, b, lat, lon, alt_m, obs, level, rising):
    """Bisect the time in [a, b] where elevation crosses `level` (to ~0.5 s)."""
    for _ in range(40):
        if b - a < 0.5:
            break
        m = (a + b) / 2.0
        above = _elev(sat, m, lat, lon, alt_m, obs) >= level
        if above == rising:
            b = m
        else:
            a = m
    return (a + b) / 2.0


def _peak(sat, a, b, lat, lon, alt_m, obs):
    """Golden-section search for the maximum elevation in [a, b]."""
    g = (math.sqrt(5.0) - 1.0) / 2.0
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = _elev(sat, c, lat, lon, alt_m, obs), _elev(sat, d, lat, lon, alt_m, obs)
    while b - a > 1.0:
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = _elev(sat, c, lat, lon, alt_m, obs)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = _elev(sat, d, lat, lon, alt_m, obs)
    t = (a + b) / 2.0
    return t, _elev(sat, t, lat, lon, alt_m, obs)


def passes(sat: Satellite, lat: float, lon: float, alt_m: float, start: float, hours: float = 24.0,
           min_elev: float = 0.0, horizon: float = 0.0, step: float = 60.0, limit: int = 100) -> list[dict]:
    """Passes over the observer between `start` (unix time) and start + hours, whose highest
    elevation is at least `min_elev`. Each: {aos, los, tca, max_elev, aos_az, los_az, tca_az}
    (aos/los: when it rises above / sets below `horizon` degrees)."""
    obs = observer_ecef(lat, lon, alt_m)
    end = start + hours * 3600.0
    out = []
    t = start
    try:
        prev = _elev(sat, t, lat, lon, alt_m, obs)
    except PropagationError:
        return []
    rise = start if prev >= horizon else None     # already up at the start: a partial pass
    while t < end and len(out) < limit:
        t2 = min(t + step, end)
        try:
            e2 = _elev(sat, t2, lat, lon, alt_m, obs)
        except PropagationError:
            break
        if prev < horizon <= e2:
            rise = _cross(sat, t, t2, lat, lon, alt_m, obs, horizon, True)
        elif prev >= horizon > e2 and rise is not None:
            set_ = _cross(sat, t, t2, lat, lon, alt_m, obs, horizon, False)
            tca, mx = _peak(sat, rise, set_, lat, lon, alt_m, obs)
            if mx >= min_elev:
                jd = jd_from_unix
                out.append({
                    "aos": round(rise, 1), "los": round(set_, 1), "tca": round(tca, 1), "max_elev": round(mx, 1),
                    "aos_az": round(look_angles(sat, jd(rise), lat, lon, alt_m, obs)[0]),
                    "los_az": round(look_angles(sat, jd(set_), lat, lon, alt_m, obs)[0]),
                    "tca_az": round(look_angles(sat, jd(tca), lat, lon, alt_m, obs)[0]),
                })
            rise = None
        t, prev = t2, e2
    return out


def window_above(sat: Satellite, p: dict, lat: float, lon: float, alt_m: float, level: float) -> tuple[float, float] | None:
    """The part of a pass during which the satellite is at or above `level` degrees, or None."""
    if p["max_elev"] < level:
        return None
    obs = observer_ecef(lat, lon, alt_m)
    a = p["aos"] if _elev(sat, p["aos"], lat, lon, alt_m, obs) >= level else \
        _cross(sat, p["aos"], p["tca"], lat, lon, alt_m, obs, level, True)
    b = p["los"] if _elev(sat, p["los"], lat, lon, alt_m, obs) >= level else \
        _cross(sat, p["tca"], p["los"], lat, lon, alt_m, obs, level, False)
    return a, b
