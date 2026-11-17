"""
Supplies & rationing tracker: what the household has on hand, how long it lasts, what expires.

  * Items: category (water, food, medical, fuel, batteries, hygiene, other), quantity + unit,
    location, expiry date, a minimum to keep, and per-unit figures where they matter
    (kcal for food, litres for water, hours for fuel and batteries).
  * Household: people, pets and daily needs (default 2000 kcal and 3.8 L of water per person).
    From these: days of water and food, hours of fuel/battery, expiring (30 days) and expired
    items, low stock, and a rationing view ("at 1200 kcal a day you have N days").
  * Use / +/- buttons log each change. CSV export and import for spreadsheets.
  * A background check posts one family-board note when something newly runs low, expires or
    is about to (batched, at most one note an hour), and a weekly digest at most while things
    stay that way. Optional short family-mesh message when water or food drops under
    `critical_days` (off by default).

Family members only (an admin without membership gets nothing: what a household has stockpiled
is exactly what it shouldn't broadcast). One JSON file, written atomically, guarded by a lock.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
import secrets
import threading
import time
from pathlib import Path

FEATURE = {"title": "Supplies", "icon": "🥫", "group": "family", "route": "/supplies",
           "script": "/features/supplies.js", "access": "member",
           "description": "Water, food and medicine on hand: days left, expiry dates, low-stock warnings"}

CATEGORIES = [("water", "Water"), ("food", "Food"), ("medical", "Medical"), ("fuel", "Fuel"),
              ("batteries", "Batteries"), ("hygiene", "Hygiene"), ("other", "Other")]
CAT_IDS = [c for c, _ in CATEGORIES]
LITRES = {"l": 1.0, "liter": 1.0, "liters": 1.0, "litre": 1.0, "litres": 1.0, "ml": 0.001,
          "gal": 3.785, "gallon": 3.785, "gallons": 3.785, "qt": 0.946, "quart": 0.946, "quarts": 0.946,
          "floz": 0.0296, "fl oz": 0.0296, "oz": 0.0296, "cl": 0.01, "dl": 0.1}
MAX_QTY = 10_000_000.0
MAX_LOG = 500
ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
HOUSEHOLD_KEYS = {  # key: (default, lo, hi)
    "people": (1, 0, 500), "pets": (0, 0, 100), "kcal": (2000, 500, 6000), "water_l": (3.8, 0.5, 20),
    "pet_kcal": (0, 0, 5000), "pet_water_l": (1.0, 0, 20), "fuel_h_day": (0, 0, 24), "battery_h_day": (0, 0, 24)}
FIELDS = ["id", "name", "category", "quantity", "unit", "location", "expiry", "minimum",
          "kcal_per_unit", "litres_per_unit", "hours_per_unit", "note"]
ALIASES = {"qty": "quantity", "amount": "quantity", "count": "quantity", "min": "minimum", "min_qty": "minimum",
           "minimum_quantity": "minimum", "kcal": "kcal_per_unit", "calories": "kcal_per_unit",
           "calories_per_unit": "kcal_per_unit", "litres": "litres_per_unit", "liters": "litres_per_unit",
           "liters_per_unit": "litres_per_unit", "hours": "hours_per_unit", "expires": "expiry",
           "expiration": "expiry", "expiry_date": "expiry", "best_by": "expiry", "use_by": "expiry",
           "where": "location", "notes": "note", "cat": "category", "type": "category", "item": "name"}
CAT_ALIASES = {"med": "medical", "medicine": "medical", "meds": "medical", "first aid": "medical",
               "battery": "batteries", "power": "batteries", "drink": "water", "drinking water": "water",
               "gas": "fuel", "propane": "fuel", "petrol": "fuel", "diesel": "fuel", "toiletries": "hygiene",
               "sanitation": "hygiene"}


class Bad(ValueError):
    pass


# ── validation ──

def num(v, lo: float = 0.0, hi: float = MAX_QTY, what: str = "number") -> float:
    if isinstance(v, bool):
        raise Bad(f"{what}: not a number")
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):        # 1,500 or 12,000.5
            s = s.replace(",", "")
        elif s.count(",") == 1 and "." not in s:               # 1,5 (decimal comma)
            s = s.replace(",", ".")
        if s == "":
            return 0.0
        if not re.fullmatch(r"[+]?\d{0,12}(\.\d{0,6})?", s) or s in (".", "+", "+."):
            raise Bad(f"{what}: not a number")
        v = float(s)
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        raise Bad(f"{what}: not a number")
    if v < lo or v > hi:
        raise Bad(f"{what} must be between {lo:g} and {hi:g}")
    return round(float(v), 3)


def text(v, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v if v is not None else "")).strip()[:limit]


def parse_date(v) -> str:
    s = text(v, 20)
    if not s:
        return ""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", s) or re.fullmatch(r"(\d{4})/(\d{1,2})(?:/(\d{1,2}))?", s)
    if not m:
        raise Bad("expiry: use a date like 2027-03-31")
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if not 1990 <= y <= 2200:
        raise Bad("expiry: that year doesn't look right")
    try:
        if not d:                                     # "2027-03": the end of that month
            d = ((dt.date(y + (mo == 12), mo % 12 + 1, 1)) - dt.timedelta(days=1)).day
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        raise Bad("expiry: that date doesn't exist") from None


def norm_cat(v) -> str:
    c = text(v, 30).lower()
    c = CAT_ALIASES.get(c, c)
    return c if c in CAT_IDS else "other"


def clean_item(data: dict, old: dict | None = None) -> dict:
    """Validated item from a request (missing fields keep their old values when editing)."""
    if not isinstance(data, dict):
        raise Bad("bad item")
    o = dict(old or {})

    def has(k):
        return k in data and data[k] is not None
    item = {
        "name": text(data["name"], 60) if has("name") else o.get("name", ""),
        "category": norm_cat(data["category"]) if has("category") else o.get("category", "other"),
        "qty": num(data["qty"], what="quantity") if has("qty") else o.get("qty", 0.0),
        "unit": text(data["unit"], 16) if has("unit") else o.get("unit", ""),
        "location": text(data["location"], 60) if has("location") else o.get("location", ""),
        "expiry": parse_date(data["expiry"]) if has("expiry") else o.get("expiry", ""),
        "min": num(data["min"], what="minimum") if has("min") else o.get("min", 0.0),
        "kcal": num(data["kcal"], hi=1_000_000, what="kcal per unit") if has("kcal") else o.get("kcal", 0.0),
        "litres": num(data["litres"], hi=100_000, what="litres per unit") if has("litres") else o.get("litres", 0.0),
        "hours": num(data["hours"], hi=100_000, what="hours per unit") if has("hours") else o.get("hours", 0.0),
        "note": text(data["note"], 200) if has("note") else o.get("note", ""),
    }
    if not item["name"]:
        raise Bad("Give the item a name.")
    return item


def litres_per_unit(it: dict) -> float:
    if it.get("litres"):
        return float(it["litres"])
    u = re.sub(r"\s+", " ", str(it.get("unit", "")).strip().lower()).rstrip(".")
    return LITRES.get(u, 0.0)


# ── the numbers ──

def summarize(d: dict, today: dt.date, expiring_days: int = 30, critical_days: float = 3,
              low_days: float = 14) -> dict:
    h = d["household"]
    soon = today + dt.timedelta(days=expiring_days)
    water = {"litres": 0.0, "litres_all": 0.0, "uncounted": 0}
    food = {"kcal": 0.0, "kcal_all": 0.0, "uncounted": 0}
    fuel = {"hours": 0.0}
    batt = {"hours": 0.0}
    items, counts = [], {"low": 0, "out": 0, "expiring": 0, "expired": 0}
    for iid, it in d["items"].items():
        flags, days_left = [], None
        if it.get("expiry"):
            try:
                e = dt.date.fromisoformat(it["expiry"])
                days_left = (e - today).days
                if e < today:
                    flags.append("expired")
                elif e <= soon:
                    flags.append("expiring")
            except ValueError:
                pass
        if it.get("qty", 0) <= 0:
            flags.append("out")
        elif it.get("min", 0) and it["qty"] < it["min"]:
            flags.append("low")
        for f in flags:
            counts[f] += 1
        good = "expired" not in flags
        q, cat = float(it.get("qty", 0)), it.get("category")
        if cat == "water":
            lpu = litres_per_unit(it)
            if not lpu:
                water["uncounted"] += 1
            water["litres_all"] += q * lpu
            water["litres"] += q * lpu if good else 0
        elif cat == "food":
            if not it.get("kcal"):
                food["uncounted"] += 1
            food["kcal_all"] += q * it.get("kcal", 0)
            food["kcal"] += q * it.get("kcal", 0) if good else 0
        elif cat == "fuel":
            fuel["hours"] += q * it.get("hours", 0)
        elif cat == "batteries":
            batt["hours"] += q * it.get("hours", 0)
        items.append({**it, "id": iid, "flags": flags, "days_left": days_left})

    def days(total, per_day):
        return round(total / per_day, 1) if per_day > 0 else None

    def level(n):
        return None if n is None else "critical" if n < critical_days else "low" if n < low_days else "ok"
    water["per_day"] = round(h["people"] * h["water_l"] + h["pets"] * h["pet_water_l"], 2)
    food["per_day"] = round(h["people"] * h["kcal"] + h["pets"] * h["pet_kcal"], 1)
    water["days"], water["days_all"] = days(water["litres"], water["per_day"]), days(water["litres_all"], water["per_day"])
    food["days"], food["days_all"] = days(food["kcal"], food["per_day"]), days(food["kcal_all"], food["per_day"])
    fuel["days"], batt["days"] = days(fuel["hours"], h["fuel_h_day"]), days(batt["hours"], h["battery_h_day"])
    for x in (water, food, fuel, batt):
        x["level"] = level(x.get("days"))
        for k in ("litres", "litres_all", "kcal", "kcal_all", "hours"):
            if k in x:
                x[k] = round(x[k], 1)
    items.sort(key=lambda i: (CAT_IDS.index(i["category"]) if i["category"] in CAT_IDS else 99, i["name"].lower()))
    return {"items": items, "water": water, "food": food, "fuel": fuel, "batteries": batt, "counts": counts}


def ration_days(total: float, per_person: float, people: float, extra_per_day: float = 0.0) -> float | None:
    per_day = per_person * people + extra_per_day
    return round(total / per_day, 1) if per_day > 0 else None


# ── storage ──

class Store:
    def __init__(self, path: Path, cfg: dict | None = None, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.cfg = cfg if cfg is not None else {}
        self.lock = threading.RLock()

    def setting(self, key, default, lo, hi):
        v = self.cfg.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return default
        return max(lo, min(hi, v))

    def defaults(self) -> dict:
        cfgmap = {"people": "people", "pets": "pets", "kcal": "kcal_per_person", "water_l": "water_l_per_person"}
        out = {}
        for k, (dflt, lo, hi) in HOUSEHOLD_KEYS.items():
            out[k] = self.setting(cfgmap[k], dflt, lo, hi) if k in cfgmap else dflt
        out["set"] = False
        return out

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        d["items"] = {k: v for k, v in (d.get("items") or {}).items() if isinstance(v, dict) and ID_RE.match(str(k))} \
            if isinstance(d.get("items"), dict) else {}
        hh = d.get("household") if isinstance(d.get("household"), dict) else {}
        d["household"] = {**self.defaults(), **{k: v for k, v in hh.items() if k in HOUSEHOLD_KEYS or k == "set"}}
        d["log"] = d.get("log") if isinstance(d.get("log"), list) else []
        n = d.get("notify") if isinstance(d.get("notify"), dict) else {}
        d["notify"] = {"active": list(n.get("active") or []), "posted": dict(n.get("posted") or {}),
                       "last_post": float(n.get("last_post") or 0), "last_digest": float(n.get("last_digest") or 0)}
        return d

    def save(self, d: dict, atomic_write) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(d, separators=(",", ":")))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def today(self) -> dt.date:
        return dt.date.fromtimestamp(self.clock())

    def summary(self, d: dict) -> dict:
        return summarize(d, self.today(), int(self.setting("expiring_days", 30, 1, 365)),
                         float(self.setting("critical_days", 3, 0, 365)), float(self.setting("low_days", 14, 0, 365)))

    def log(self, d: dict, who: str, what: str, item: dict | None = None, delta: float | None = None) -> None:
        rec = {"at": int(self.clock()), "who": who[:32], "what": what}
        if item is not None:
            rec.update(name=item.get("name", ""), unit=item.get("unit", ""), qty=item.get("qty"))
        if delta is not None:
            rec["delta"] = delta
        d["log"].append(rec)
        d["log"] = d["log"][-MAX_LOG:]


# ── notices for the family board ──

def _fmt_qty(it: dict) -> str:
    q = it.get("qty", 0)
    s = f"{q:g}" + (f" {it['unit']}" if it.get("unit") else "")
    if it.get("min"):
        s += f", want {it['min']:g}"
    return s


def check_notify(store: Store, ctx, atomic_write=None) -> str | None:
    """Post one note for conditions that are new since the last check. Returns the text posted."""
    cfg = store.cfg
    atomic_write = atomic_write or ctx.atomic_write
    now = store.clock()
    with store.lock:
        d = store.load()
        s = store.summary(d)
        n = d["notify"]
        active, names = {}, {}
        for it in s["items"]:
            for f in it["flags"]:
                k = f"{it['id']}:{'low' if f == 'out' else f}"
                active[k] = it
                names[k] = f
        for what in ("water", "food"):                    # only categories the family actually tracks
            if s[what]["level"] == "critical" and any(i["category"] == what for i in s["items"]):
                active[f"_{what}:critical"] = None
        gap = 3 * 86400                                   # the same thing flapping in and out: once in 3 days
        fresh = [k for k in active if k not in n["active"] and now - n["posted"].get(k, 0) > gap]
        digest_s = float(store.setting("digest_days", 7, 0, 365)) * 86400
        msg, is_digest = None, False
        if fresh:
            if now - n["last_post"] < 3600:
                return None                               # batch: try again on the next check
            msg = _note(s, d, fresh, active, names, digest=False)
        elif active and digest_s and now - max(n["last_post"], n["last_digest"]) >= digest_s:
            msg, is_digest = _note(s, d, list(active), active, names, digest=True), True
        if msg and cfg.get("board_notes", True) is not False:
            ok, _ = ctx.board_post("feature:supplies", "Supplies", msg, False, "family",
                                   {"id": "feature-supplies", "name": "📦 Supplies"})
            if not ok:
                return None
        if cfg.get("mesh_critical") is True:
            for what in ("water", "food"):
                if f"_{what}:critical" in fresh:
                    ctx.mesh_send(f"Badger supplies: {what} about {s[what]['days']:g} days left "
                                  f"for {d['household']['people']:g} people", "family")
        for k in fresh:
            n["posted"][k] = now
        n["posted"] = {k: t for k, t in n["posted"].items() if now - t < 30 * 86400 or k in active}
        n["active"] = sorted(active)
        if msg:
            n["last_digest" if is_digest else "last_post"] = now
        store.save(d, atomic_write)
        return msg


def _note(s, d, keys, active, names, digest: bool) -> str:
    groups = {"low": [], "expiring": [], "expired": []}
    for k in keys:
        it = active.get(k)
        if it is None:
            continue
        f = "low" if names[k] in ("low", "out") else names[k]
        label = it["name"] + (f" ({_fmt_qty(it)})" if f == "low" else f" ({it.get('expiry')})")
        groups[f].append(label)
    parts = []
    for f, title in (("low", "running low"), ("expired", "expired"), ("expiring", "expiring soon")):
        if groups[f]:
            shown = groups[f][:6]
            more = len(groups[f]) - len(shown)
            parts.append(f"{title}: " + ", ".join(shown) + (f" and {more} more" if more else ""))
    h = d["household"]
    tail = []
    for what, unit in (("water", "water"), ("food", "food")):
        days = s[what]["days"]
        if days is not None and (digest or any(k.startswith(f"_{what}:") for k in keys)):
            tail.append(f"{unit} lasts about {days:g} days for {h['people']:g} people")
    head = "📦 Weekly supplies check — " if digest else "📦 Supplies — "
    msg = head + "; ".join(parts + tail) + ". Details under Supplies."
    return msg if len(msg) <= 900 else msg[:880] + "…"


# ── CSV ──

def _csv_cell(v) -> str:
    s = "" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(d: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(FIELDS)
    for iid, it in sorted(d["items"].items(), key=lambda kv: (kv[1].get("category", ""), kv[1].get("name", "").lower())):
        w.writerow([_csv_cell(x) for x in (iid, it.get("name"), it.get("category"), float(it.get("qty", 0)), it.get("unit"),
                                           it.get("location"), it.get("expiry"), float(it.get("min", 0)),
                                           float(it.get("kcal", 0)), float(it.get("litres", 0)),
                                           float(it.get("hours", 0)), it.get("note"))])
    return buf.getvalue()


def from_csv(raw: bytes, d: dict, max_items: int, replace: bool = False) -> dict:
    try:
        s = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        s = raw.decode("latin-1")
    try:
        rows = list(csv.reader(io.StringIO(s, newline="")))
    except csv.Error as e:
        raise Bad(f"That file isn't a readable CSV ({e}).") from None
    if not rows:
        raise Bad("The file is empty.")
    cols = [ALIASES.get(re.sub(r"[\s\-]+", "_", c.strip().lower()), re.sub(r"[\s\-]+", "_", c.strip().lower()))
            for c in rows[0]]
    if "name" not in cols:
        raise Bad("The first row must name the columns, with at least a 'name' column.")
    keymap = {"quantity": "qty", "minimum": "min", "kcal_per_unit": "kcal", "litres_per_unit": "litres",
              "hours_per_unit": "hours"}
    items = {} if replace else dict(d["items"])
    by_name = {(v.get("name", "").casefold(), v.get("location", "").casefold()): k for k, v in items.items()}
    added = updated = 0
    errors = []
    for n, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        rec = {}
        for c, v in zip(cols, row, strict=False):
            if c in FIELDS:
                v = v[1:] if v[:2] in ("'=", "'+", "'-", "'@") else v
                rec[keymap.get(c, c)] = v
        try:
            iid = rec.pop("id", "").strip()
            old_id = iid if iid in items else by_name.get((text(rec.get("name"), 60).casefold(),
                                                           text(rec.get("location"), 60).casefold()))
            item = clean_item(rec, items.get(old_id) if old_id else None)
        except Bad as e:
            if len(errors) < 20:
                errors.append(f"row {n}: {e}")
            continue
        if old_id:
            items[old_id] = {**items[old_id], **item}
            updated += 1
        else:
            if len(items) >= max_items:
                errors.append(f"row {n}: at most {max_items} items; the rest were skipped")
                break
            new_id = secrets.token_urlsafe(9)
            items[new_id] = item
            by_name[(item["name"].casefold(), item["location"].casefold())] = new_id
            added += 1
    d["items"] = items
    return {"added": added, "updated": updated, "errors": errors}


# ── HTTP ──
_S: dict[str, Store] = {}
_S_lock = threading.Lock()


def _store(ctx) -> Store:
    p = ctx.data_dir() / "supplies.json"
    with _S_lock:
        st = _S.get(str(p))
        if st is None:
            st = _S[str(p)] = Store(p, ctx.cfg())
        return st


def _who(args) -> str:
    return str(args["member"]["name"])[:32]


def _state(st: Store, d: dict) -> dict:
    s = st.summary(d)
    return {**s, "household": d["household"], "log": list(reversed(d["log"][-30:])),
            "categories": [{"id": c, "label": label} for c, label in CATEGORIES],
            "settings": {"expiring_days": st.setting("expiring_days", 30, 1, 365),
                         "critical_days": st.setting("critical_days", 3, 0, 365),
                         "low_days": st.setting("low_days", 14, 0, 365),
                         "max_items": int(st.setting("max_items", 2000, 1, 20000))},
            "today": st.today().isoformat()}


def get_all(req, ctx, args):
    st = _store(ctx)
    with st.lock:
        return _state(st, st.load())


def _write(ctx, args, fn):
    st = _store(ctx)
    with st.lock:
        d = st.load()
        try:
            res = fn(st, d)
        except Bad as e:
            return 400, {"ok": False, "error": str(e)}
        if isinstance(res, tuple):
            return res
        st.save(d, ctx.atomic_write)
        return {"ok": True, **(res or {}), "state": _state(st, d)}


def _iid(v):
    if not isinstance(v, str) or not ID_RE.match(v):
        raise Bad("no such item")
    return v


def post_item(req, ctx, args):
    data = args["data"] or {}

    def fn(st, d):
        iid = data.get("id")
        if iid:
            iid = _iid(iid)
            if iid not in d["items"]:
                return 404, {"ok": False, "error": "That item was deleted."}
            item = clean_item(data, d["items"][iid])
            d["items"][iid] = item
            st.log(d, _who(args), "edited", item)
        else:
            if len(d["items"]) >= int(st.setting("max_items", 2000, 1, 20000)):
                raise Bad("The list is full. Delete some items first.")
            item = clean_item(data)
            iid = secrets.token_urlsafe(9)
            d["items"][iid] = item
            st.log(d, _who(args), "added", item)
        return {"id": iid}
    return _write(ctx, args, fn)


def post_delete(req, ctx, args):
    data = args["data"] or {}

    def fn(st, d):
        iid = _iid(data.get("id"))
        item = d["items"].pop(iid, None)
        if item is None:
            return 404, {"ok": False, "error": "That item was already deleted."}
        st.log(d, _who(args), "deleted", item)
    return _write(ctx, args, fn)


def post_adjust(req, ctx, args):
    data = args["data"] or {}

    def fn(st, d):
        iid = _iid(data.get("id"))
        if iid not in d["items"]:
            return 404, {"ok": False, "error": "That item was deleted."}
        it = d["items"][iid]
        if "set" in data:
            new = num(data["set"], what="quantity")
            delta = round(new - it.get("qty", 0), 3)
            what = "counted"
        else:
            v = data.get("delta")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > MAX_QTY:
                raise Bad("amount: not a number")
            if data.get("use") is True:
                v = -abs(v)
            delta = round(float(v), 3)
            new = round(min(MAX_QTY, max(0.0, it.get("qty", 0) + delta)), 3)
            delta = round(new - it.get("qty", 0), 3)
            what = "used" if data.get("use") is True else ("added" if delta > 0 else "took")
        it["qty"] = new
        st.log(d, _who(args), what, it, delta)
        return {"qty": new}
    return _write(ctx, args, fn)


def post_household(req, ctx, args):
    data = args["data"] or {}

    def fn(st, d):
        hh = dict(d["household"])
        for k, (_dflt, lo, hi) in HOUSEHOLD_KEYS.items():
            if k in data and data[k] is not None and data[k] != "":
                hh[k] = num(data[k], lo, hi, what=k.replace("_", " "))
        hh["set"] = True
        d["household"] = hh
        st.log(d, _who(args), "updated household needs")
    return _write(ctx, args, fn)


def get_csv(req, ctx, args):
    st = _store(ctx)
    with st.lock:
        body = to_csv(st.load()).encode("utf-8")
    day = st.today().isoformat()
    ctx.send_bytes(req, b"\xef\xbb\xbf" + body, "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="supplies-{day}.csv"'})
    return None


def post_import(req, ctx, args):
    n = int(req.headers.get("Content-Length") or 0)
    raw = req.rfile.read(n)
    if len(raw) < n:
        req.close_connection = True
        return 400, {"ok": False, "error": "The upload stopped part way."}
    mode = (args["qs"].get("mode") or ["merge"])[0]
    if mode not in ("merge", "replace"):
        return 400, {"ok": False, "error": "mode must be merge or replace"}

    def fn(st, d):
        res = from_csv(raw, d, int(st.setting("max_items", 2000, 1, 20000)), replace=mode == "replace")
        st.log(d, _who(args), f"imported a spreadsheet ({res['added']} added, {res['updated']} updated)")
        return res
    return _write(ctx, args, fn)


def get_log(req, ctx, args):
    st = _store(ctx)
    with st.lock:
        return {"log": list(reversed(st.load()["log"]))}


def setup(ctx):
    st = _store(ctx)
    every = float(st.setting("check_minutes", 30, 1, 1440)) * 60

    def loop():
        time.sleep(min(120.0, every))
        while True:
            try:
                check_notify(_store(ctx), ctx)
            except Exception as e:  # noqa: BLE001 — the timer must survive a bad file
                ctx.log(f"supplies check failed: {type(e).__name__}: {e}")
            time.sleep(every)
    threading.Thread(target=loop, name="supplies-check", daemon=True).start()


ROUTES = [
    {"method": "GET", "path": "/api/supplies", "handler": get_all, "access": "member"},
    {"method": "POST", "path": "/api/supplies/item", "handler": post_item, "access": "member"},
    {"method": "POST", "path": "/api/supplies/delete", "handler": post_delete, "access": "member"},
    {"method": "POST", "path": "/api/supplies/adjust", "handler": post_adjust, "access": "member"},
    {"method": "POST", "path": "/api/supplies/household", "handler": post_household, "access": "member"},
    {"method": "GET", "path": "/api/supplies/export.csv", "handler": get_csv, "access": "member"},
    {"method": "POST", "path": "/api/supplies/import", "handler": post_import, "access": "member",
     "raw": True, "max_bytes": 1_000_000},
    {"method": "GET", "path": "/api/supplies/log", "handler": get_log, "access": "member"},
]
