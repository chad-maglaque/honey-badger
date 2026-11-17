"""
Survival calculators: water and calorie needs, rationing, travel time, navigation, weather rules
of thumb. Rules of thumb are labelled; the published formulas are named.
"""
from __future__ import annotations

import math

from .engineering import EngError, _pos, sig

R_EARTH_KM = 6371.0088
ACTIVITY = {"rest": 1.2, "light": 1.375, "moderate": 1.55, "heavy": 1.725, "very_heavy": 1.9}
DRINK_L = {"rest": 2.5, "light": 3.5, "moderate": 5.0, "heavy": 7.0, "very_heavy": 8.0}


def _act(activity: str) -> str:
    a = str(activity or "light").lower().replace(" ", "_")
    if a not in ACTIVITY:
        raise EngError(f"activity must be one of {', '.join(ACTIVITY)}")
    return a


def water_needs(people=1, temp_c=22.0, activity="light", days=1.0) -> dict:
    n = _pos("people", people)
    d = _pos("days", days)
    a = _act(activity)
    if not -40 <= temp_c <= 55:
        raise EngError("temp_c must be between −40 and 55")
    drink = DRINK_L[a] * (1 + max(0.0, temp_c - 25) * 0.05)
    drink = min(drink, 12.0)
    total = (drink + 1.3) * n * d
    return {"drinking_L_per_person_day": sig(drink, 3), "with_cooking_hygiene_L_per_person_day": sig(drink + 1.3, 3),
            "total_L": sig(total, 4), "total_gal": sig(total / 3.785411784, 4),
            "fema_minimum_L": sig(3.785 * n * d, 4),
            "notes": "Rule of thumb. FEMA/Ready.gov: store at least 1 gallon (3.8 L) per person per day; more in heat, "
                     "for nursing mothers, children and the sick. Don't drink more than about 1–1.5 L an hour. "
                     "Ration sweat, not water: rest in shade in the heat of the day."}


def calorie_needs(weight_kg, activity="light", height_cm=None, age=None, sex=None, temp_c=None) -> dict:
    w = _pos("weight_kg", weight_kg)
    if w > 400:
        raise EngError("weight_kg looks wrong")
    a = _act(activity)
    if height_cm is not None and age is not None and sex in ("male", "female"):
        bmr = 10 * w + 6.25 * _pos("height_cm", height_cm) - 5 * _pos("age", age) + (5 if sex == "male" else -161)
        how = "Mifflin–St Jeor"
    else:
        bmr = 22.0 * w
        how = "about 22 kcal per kg of body weight (give height_cm, age and sex for Mifflin–St Jeor)"
    tdee = bmr * ACTIVITY[a]
    cold = 1.0
    if temp_c is not None and temp_c < 10:
        cold = min(1.5, 1 + 0.01 * (10 - temp_c))
    return {"bmr_kcal": sig(bmr, 4), "daily_kcal": sig(tdee * cold, 4), "method": how,
            "activity_factor": ACTIVITY[a], "cold_factor": sig(cold, 3),
            "notes": "Estimate. Hard work in the cold can need 4000–5000 kcal a day. Below about 1200 kcal a day "
                     "people weaken fast; children and pregnant women should not be rationed hard."}


def ration(total, per_person_per_day, people=1.0, extra_per_day=0.0, unit="") -> dict:
    t = _pos("total", total, allow_zero=True)
    pp, n = _pos("per_person_per_day", per_person_per_day), _pos("people", people)
    extra = _pos("extra_per_day", extra_per_day, allow_zero=True)
    out = {"days": sig(t / (pp * n + extra), 4), "unit": str(unit)[:20], "levels": []}
    for frac in (1.0, 0.75, 0.5):
        out["levels"].append({"ration": f"{int(frac * 100)}%", "per_person_per_day": sig(pp * frac, 4),
                              "days": sig(t / (pp * frac * n + extra), 4)})
    return out


TERRAIN = {"road": 1.0, "trail": 1.2, "rough": 1.5, "bush": 2.0, "snow": 2.0, "sand": 1.6}


def travel_time(distance_km, ascent_m=0.0, descent_m=0.0, load_kg=0.0, body_kg=75.0, terrain="trail",
                hours_per_day=8.0) -> dict:
    d = _pos("distance_km", distance_km)
    up = _pos("ascent_m", ascent_m, allow_zero=True)
    down = _pos("descent_m", descent_m, allow_zero=True)
    load, body = _pos("load_kg", load_kg, allow_zero=True), _pos("body_kg", body_kg)
    t = str(terrain).lower()
    if t not in TERRAIN:
        raise EngError(f"terrain must be one of {', '.join(TERRAIN)}")
    base = d / 5.0 + up / 600.0 + (down / 300.0) * (10 / 60)      # Naismith (+ Langmuir for steep descents)
    ratio = load / body
    load_f = 1 + max(0.0, ratio - 0.2)                              # +10% time per 10% body weight over 20%
    hours = base * TERRAIN[t] * load_f
    hpd = _pos("hours_per_day", hours_per_day)
    out = {"hours": sig(hours, 3), "days": sig(hours / hpd, 3), "naismith_hours": sig(base, 3),
           "terrain_factor": TERRAIN[t], "load_factor": sig(load_f, 3), "load_pct_body": sig(100 * ratio, 3),
           "formula": "Naismith: 5 km/h + 1 h per 600 m climbed (+10 min per 300 m of steep descent), × terrain × load",
           "notes": "Add rests (10 min an hour), and allow for the slowest person, darkness and weather."}
    if ratio > 0.33:
        out["warning"] = "A pack over a third of body weight risks injury; lighten it or split loads."
    return out


def _lat_lon(lat, lon):
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise EngError("latitude must be −90…90 and longitude −180…180 (decimal degrees)")


def nav_distance(lat1, lon1, lat2, lon2, declination_deg=None) -> dict:
    _lat_lon(lat1, lon1)
    _lat_lon(lat2, lon2)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    dist = 2 * R_EARTH_KM * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = (math.degrees(math.atan2(y, x)) + 360) % 360
    out = {"distance_km": sig(dist, 5), "distance_mi": sig(dist / 1.609344, 5), "distance_nmi": sig(dist / 1.852, 5),
           "initial_bearing_true_deg": sig(brg, 4),
           "formula": "haversine great-circle distance; initial bearing (true north)"}
    if declination_deg is not None:
        out["initial_bearing_magnetic_deg"] = sig((brg - declination_deg) % 360, 4)
        out["declination_note"] = "magnetic = true − declination (east declination positive)"
    return out


def dead_reckoning(lat, lon, bearing_deg, distance_km=None, speed_kmh=None, hours=None) -> dict:
    _lat_lon(lat, lon)
    if distance_km is None:
        if speed_kmh is None or hours is None:
            raise EngError("give distance_km, or speed_kmh and hours")
        distance_km = _pos("speed_kmh", speed_kmh, allow_zero=True) * _pos("hours", hours, allow_zero=True)
    d = _pos("distance_km", distance_km, allow_zero=True) / R_EARTH_KM
    th = math.radians(bearing_deg % 360)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(th))
    l2 = l1 + math.atan2(math.sin(th) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return {"lat": sig(math.degrees(p2), 7), "lon": sig((math.degrees(l2) + 540) % 360 - 180, 7),
            "distance_km": sig(distance_km, 5), "formula": "great-circle destination from start, true bearing and distance",
            "notes": "Dead reckoning drifts about 10% of distance travelled; fix your position whenever you can."}


def dew_point(temp_c: float, rh: float) -> float:
    b, c = 17.62, 243.12
    g = math.log(rh / 100) + b * temp_c / (c + temp_c)
    return c * g / (b - g)


def weather(temp_c=None, humidity=None, dew_point_c=None, wind_kmh=None, pressure_change_hpa_3h=None) -> dict:
    out, rules = {}, []
    if humidity is not None and not 1 <= humidity <= 100:
        raise EngError("humidity is relative humidity in % (1–100)")
    if temp_c is not None and humidity is not None and dew_point_c is None:
        dew_point_c = dew_point(temp_c, humidity)
        out["dew_point_C"] = sig(dew_point_c, 3)
    if temp_c is not None and dew_point_c is not None:
        spread = temp_c - dew_point_c
        out["cloud_base_m"] = sig(max(0.0, 125 * spread), 3)
        out["cloud_base_ft"] = sig(max(0.0, 125 * spread) / 0.3048, 3)
        if spread < 2.5:
            rules.append("Air is near saturation (spread under 2.5 °C): fog, mist or low cloud likely, especially at night.")
        if dew_point_c <= 0 and temp_c < 5:
            rules.append("Frost likely on a clear, calm night.")
    if temp_c is not None and wind_kmh is not None and temp_c <= 10 and wind_kmh > 4.8:
        v = wind_kmh ** 0.16
        wc = 13.12 + 0.6215 * temp_c - 11.37 * v + 0.3965 * temp_c * v
        out["wind_chill_C"] = sig(wc, 3)
        if wc <= -27:
            rules.append("Frostbite possible on exposed skin within 30 minutes; at −40 within 10 minutes.")
    if temp_c is not None and humidity is not None and temp_c >= 26.7:
        t = temp_c * 9 / 5 + 32
        r = humidity
        hi = (-42.379 + 2.04901523 * t + 10.14333127 * r - 0.22475541 * t * r - 6.83783e-3 * t * t
              - 5.481717e-2 * r * r + 1.22874e-3 * t * t * r + 8.5282e-4 * t * r * r - 1.99e-6 * t * t * r * r)
        out["heat_index_C"] = sig((hi - 32) * 5 / 9, 3)
        out["heat_index_F"] = sig(hi, 3)
        if hi >= 103:
            rules.append("Heat index in the danger range: heat stroke likely with exertion. Rest in shade, drink, cool the body.")
        elif hi >= 90:
            rules.append("Extreme caution: heat cramps and heat exhaustion possible with exertion.")
    if pressure_change_hpa_3h is not None:
        p = float(pressure_change_hpa_3h)
        if p <= -6:
            trend = "falling very fast: a storm or strong wind is likely within hours"
        elif p <= -3.6:
            trend = "falling fast: unsettled weather, wind and rain likely within 12 hours"
        elif p <= -1.6:
            trend = "falling: weather may worsen"
        elif p < 1.6:
            trend = "steady: little change expected"
        elif p < 3.6:
            trend = "rising: improving weather"
        else:
            trend = "rising fast: clearing but often windy and colder"
        out["pressure_trend"] = trend
    out["rules"] = rules
    out["formulas"] = ("dew point: Magnus; cloud base ≈ 125 m per °C of spread; wind chill: NWS 2001; "
                       "heat index: NWS Rothfusz regression; pressure tendency: rules of thumb")
    if not out.get("rules") and len(out) == 2:
        raise EngError("give temp_c with humidity/dew_point_c/wind_kmh, or pressure_change_hpa_3h")
    return out


def lightning_distance(seconds) -> dict:
    s = _pos("seconds", seconds, allow_zero=True)
    km = s * 0.343
    return {"distance_km": sig(km, 3), "distance_mi": sig(km / 1.609344, 3), "take_shelter": s <= 30,
            "notes": "30-30 rule: if thunder follows the flash within 30 s, get inside; wait 30 minutes after the last thunder."}
