"""
decoy — the made-up household a DURESS session sees (docs/SECURITY.md → "Duress").

When someone is forced to hand over a PIN, the Badger must neither look empty (an empty prepper box looks
wrong and raises danger) nor show the real family. So every Badger keeps a small, boring, believable
household ready at all times, generated at install / first start from the templates below with a little
per-device randomness (seeded once), and editable by the owner on the real admin page.

  * It is harmless by design: a modest supplies list (a couple of cases of water, canned goods, rice and
    pasta, a first-aid kit, batteries, a flashlight, a few propane canisters for a camp stove — ordinary
    places like "Pantry", no hiding places), a short mundane family-board history over the last days, member
    first names (as many as the real family has), an empty vault. No locations, no schedules, no real names.
  * So it lives in its own plain file (decoy.json, next to the portal's other files, 0640): a duress session
    must be able to show it without the real private key.
  * materialize() writes a working copy (members, family board, community board copy, supplies, posture) into
    a folder the launcher then reads instead of the real files. Real members' device keys (hashes only) are
    carried over, so every family phone keeps working — it just sees the made-up names.

Trade-off (documented for the owner): an attacker who knows the family's real first names may notice the
names differ. The owner can type believable names (or the real ones) in the decoy editor.

Standard library only. CLI:  python3 decoy.py ensure [FILE]   (install: make one if missing)
                             python3 decoy.py show [FILE]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_FILE = "/var/lib/prepper/portal/decoy.json"
DEFAULT_SEALED_DIR = "/var/lib/prepper/portal/sealed"
VERSION = 1
CATEGORIES = ("water", "food", "medical", "fuel", "batteries", "hygiene", "other")
MAX_NAMES, MAX_NAME = 24, 24
MAX_SUPPLIES, MAX_POSTS, MAX_POST = 60, 40, 300
MAX_BYTES = 128 * 1024
POST_DAYS = 6                                       # board history spread over the last days

NAMES = ("Sarah", "Mike", "Emma", "Jake", "Laura", "Chris", "Anna", "Ben", "Katie", "Tom", "Megan", "Dan",
         "Lisa", "Sam", "Rachel", "Matt", "Jenny", "Nick", "Amy", "Josh", "Kelly", "Ryan", "Hannah", "Steve",
         "Olivia", "Luke", "Grace", "Adam", "Chloe", "Mark", "Julie", "Pete", "Nora", "Alex", "Beth", "Joe")

# (name, category, unit, qty lo, qty hi, {per-unit figures}, expiry days lo/hi or None, minimum, places)
SUPPLY_TEMPLATES = (
    ("Bottled water (24 × 0.5 L)", "water", "cases", 2, 3, {"litres": 12}, (240, 600), 1, ("Garage", "Pantry")),
    ("Water jug (1 gal)", "water", "jugs", 1, 3, {"litres": 3.785}, (150, 400), 0, ("Pantry", "Garage")),
    ("Canned beans", "food", "cans", 5, 10, {"kcal": 350}, (300, 700), 2, ("Pantry", "Kitchen cupboard")),
    ("Canned soup", "food", "cans", 4, 8, {"kcal": 250}, (250, 600), 0, ("Pantry", "Kitchen cupboard")),
    ("Canned tuna", "food", "cans", 3, 6, {"kcal": 190}, (300, 800), 0, ("Pantry",)),
    ("Rice", "food", "kg", 2, 4, {"kcal": 3600}, (300, 700), 1, ("Pantry", "Kitchen cupboard")),
    ("Pasta (500 g)", "food", "packs", 3, 6, {"kcal": 1750}, (200, 500), 1, ("Pantry", "Kitchen cupboard")),
    ("Peanut butter", "food", "jars", 1, 2, {"kcal": 2900}, (120, 300), 0, ("Pantry",)),
    ("Oatmeal", "food", "boxes", 1, 2, {"kcal": 1900}, (150, 400), 0, ("Kitchen cupboard",)),
    ("Crackers", "food", "boxes", 1, 3, {"kcal": 1200}, (45, 160), 0, ("Kitchen cupboard",)),
    ("First-aid kit", "medical", "kit", 1, 1, {}, None, 1, ("Bathroom cabinet", "Hall closet")),
    ("Pain relievers", "medical", "bottles", 1, 2, {}, (200, 600), 1, ("Bathroom cabinet",)),
    ("Adhesive bandages", "medical", "boxes", 1, 2, {}, None, 0, ("Bathroom cabinet",)),
    ("AA batteries", "batteries", "pcs", 8, 20, {"hours": 0}, (700, 1800), 4, ("Kitchen drawer",)),
    ("AAA batteries", "batteries", "pcs", 4, 12, {"hours": 0}, (700, 1800), 0, ("Kitchen drawer",)),
    ("LED flashlight", "other", "pcs", 1, 2, {}, None, 1, ("Kitchen drawer", "Hall closet")),
    ("Propane canister 1 lb (camp stove)", "fuel", "canisters", 2, 4, {"hours": 1.5}, None, 1, ("Garage",)),
    ("Matches", "other", "boxes", 1, 2, {}, None, 0, ("Kitchen drawer",)),
    ("Candles", "other", "pcs", 4, 8, {}, None, 0, ("Hall closet",)),
    ("Toilet paper", "hygiene", "rolls", 6, 16, {}, None, 4, ("Bathroom cabinet", "Hall closet")),
    ("Hand soap", "hygiene", "bottles", 1, 3, {}, None, 0, ("Bathroom cabinet",)),
)
ALWAYS = ("Bottled water (24 × 0.5 L)", "Canned beans", "Rice", "Pasta (500 g)", "First-aid kit", "AA batteries",
          "LED flashlight", "Propane canister 1 lb (camp stove)")

POSTS = ("Back around 6", "Charging the phones here", "Can someone check the rain gauge?", "Took the dog out",
         "Who has the car keys?", "Dinner's at 7", "Picked up milk and bread", "Laundry is in the dryer",
         "Bins go out tonight", "Running a bit late, start without me", "Wi-Fi was slow earlier, fine now",
         "Left the umbrella by the door", "Can someone water the plants?", "Library books are due Friday",
         "Power flickered for a second, all fine", "Can someone grab the mail?", "Leftovers in the fridge",
         "Garden needs weeding this weekend", "Phone's at 10%, plugging in here", "Movie night?",
         "Recycling day tomorrow", "Found the spare batteries", "Weather looks rainy tomorrow",
         "Spare charger is in the kitchen drawer", "Fixed the squeaky gate", "Thermostat's back to normal")


# ── small helpers ──

def _clean(s, limit: int) -> str:
    s = re.sub(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩﻿]", " ", str(s if s is not None else ""))
    return " ".join(s.split())[:limit]


def _num(v, lo: float, hi: float, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x or x in (float("inf"), float("-inf")):
        return default
    return round(max(lo, min(hi, x)), 3)


def _int(v, lo: int, hi: int, default: int) -> int:
    return int(_num(v, lo, hi, default))


def _rid(rng: random.Random, n: int = 12) -> str:
    abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(rng.choice(abc) for _ in range(n))


def write_file(path: Path, text: str, mode: int = 0o640) -> None:
    """Write-then-rename, created with `mode`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── the profile ──

def generate(seed: int | None = None) -> dict:
    """A fresh, plausible profile. Same seed → same household (so a device's decoy stays stable)."""
    seed = int(seed) if isinstance(seed, int) and not isinstance(seed, bool) else secrets.randbits(48)
    rng = random.Random(seed)
    names = rng.sample(NAMES, 12)
    picks = [t for t in SUPPLY_TEMPLATES if t[0] in ALWAYS]
    rest = [t for t in SUPPLY_TEMPLATES if t[0] not in ALWAYS]
    picks += rng.sample(rest, rng.randint(4, 7))
    picks.sort(key=lambda t: (CATEGORIES.index(t[1]), t[0]))
    supplies = []
    for name, cat, unit, lo, hi, per, exp, mn, places in picks:
        supplies.append({"name": name, "category": cat, "qty": rng.randint(lo, hi), "unit": unit,
                         "location": rng.choice(places), "min": mn,
                         "expires_in_days": rng.randint(*exp) if exp else None,
                         "kcal": per.get("kcal", 0), "litres": per.get("litres", 0), "hours": per.get("hours", 0)})
    texts = rng.sample(POSTS, rng.randint(8, 12))
    board = [{"text": t} for t in texts]
    return normalize({"v": VERSION, "seed": seed, "created": int(time.time()), "names": names,
                      "supplies": supplies, "board": board})


def normalize(p) -> dict:
    """A clean profile from anything (an owner's edit, an old or damaged file). Raises ValueError only when
    nothing usable is left. Board posts get their day/time/author spread from the seed."""
    if not isinstance(p, dict):
        raise ValueError("not a decoy profile")
    seed = p.get("seed")
    seed = seed if isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed < 2 ** 63 else secrets.randbits(48)
    names = []
    for n in (p.get("names") if isinstance(p.get("names"), list) else [])[:MAX_NAMES]:
        n = _clean(n, MAX_NAME)
        if n and n.lower() not in [x.lower() for x in names]:
            names.append(n)
    rng = random.Random(seed)
    for n in rng.sample(NAMES, len(NAMES)):          # top up to 12 so any family size gets a name
        if len(names) >= 12:
            break
        if n.lower() not in [x.lower() for x in names]:
            names.append(n)
    supplies = []
    for s in (p.get("supplies") if isinstance(p.get("supplies"), list) else [])[:MAX_SUPPLIES]:
        if not isinstance(s, dict):
            continue
        name = _clean(s.get("name"), 60)
        if not name:
            continue
        exp = s.get("expires_in_days")
        supplies.append({"name": name, "category": s.get("category") if s.get("category") in CATEGORIES else "other",
                         "qty": _num(s.get("qty"), 0, 100000), "unit": _clean(s.get("unit"), 16),
                         "location": _clean(s.get("location"), 60), "min": _num(s.get("min"), 0, 100000),
                         "expires_in_days": None if exp in (None, "") else _int(exp, -30, 3650, 365),
                         "kcal": _num(s.get("kcal"), 0, 1_000_000), "litres": _num(s.get("litres"), 0, 100_000),
                         "hours": _num(s.get("hours"), 0, 100_000)})
    board = []
    raw = p.get("board") if isinstance(p.get("board"), list) else []
    slots = random.Random(seed ^ 0x5EED)
    for i, b in enumerate(raw[:MAX_POSTS]):
        t = _clean(b.get("text") if isinstance(b, dict) else b, MAX_POST)
        if not t:
            continue
        day = slots.randint(0, POST_DAYS - 1)
        minute = slots.randint(7 * 60, 21 * 60 + 30)
        board.append({"text": t, "day": day, "minute": minute, "who": slots.randint(0, 11) if i else 0})
    out = {"v": VERSION, "seed": seed, "created": _int(p.get("created"), 0, 2 ** 40, int(time.time())),
           "names": names, "supplies": supplies, "board": board}
    if len(json.dumps(out)) > MAX_BYTES:
        raise ValueError("the decoy is too big")
    return out


def load(path) -> dict | None:
    try:
        raw = Path(path).read_bytes()
        if len(raw) > MAX_BYTES * 2:
            return None
        return normalize(json.loads(raw))
    except (OSError, ValueError, RecursionError, AttributeError, TypeError):
        return None


def save(path, profile) -> dict:
    p = normalize(profile)
    write_file(Path(path), json.dumps(p, indent=1, ensure_ascii=False), 0o640)
    return p


def ensure(path) -> dict:
    """The device's profile; made (and saved) if it is missing or damaged. Never raises on a read-only disk:
    the generated profile is still returned (a duress session must always have something to show)."""
    p = load(path)
    if p is not None:
        return p
    p = generate()
    try:
        save(path, p)
    except OSError:
        pass
    return p


def preview(profile: dict, members: int) -> dict:
    """What a duress session would see, in short (for the owner's admin page)."""
    n = max(0, int(members))
    return {"members": profile["names"][:n] if n else [], "supplies": len(profile["supplies"]),
            "posts": len(profile["board"]), "vault": "no documents yet", "posture": "Guarded"}


# ── the real family's shape (no names: hashes and ids only) ──

def member_skeleton(members_file) -> list[dict]:
    """Active members of the real members.json, oldest first: ids, creation times, radios, and their devices
    (id, key HASH, created; a device still waiting for approval stays waiting). Names stay sealed and unread.
    [] when the file is missing or damaged."""
    try:
        d = json.loads(Path(members_file).read_bytes())
    except (OSError, ValueError, RecursionError):
        return []
    mem = d.get("members") if isinstance(d, dict) and isinstance(d.get("members"), dict) else {}
    out = []
    for mid, m in mem.items():
        if not isinstance(m, dict) or m.get("removed") or not isinstance(mid, str) or not re.fullmatch(r"m_[0-9a-f]{4,32}", mid):
            continue
        devs = []
        for x in m.get("devices") if isinstance(m.get("devices"), list) else []:
            if not isinstance(x, dict) or not isinstance(x.get("key"), str):
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", x["key"]) or not isinstance(x.get("id"), str):
                continue
            dev = {"id": x["id"][:16], "key": x["key"], "created": _num(x.get("created"), 0, 2 ** 40, 0)}
            if x.get("pending"):
                dev.update(pending=True, expires=_num(x.get("expires"), 0, 2 ** 40, 0))
            devs.append(dev)
        radios = [r for r in (m.get("radios") if isinstance(m.get("radios"), list) else [])
                  if isinstance(r, str) and re.fullmatch(r"![0-9a-f]{8}", r)]
        out.append({"id": mid, "created": _num(m.get("created"), 0, 2 ** 40, 0), "devices": devs, "radios": radios})
    out.sort(key=lambda m: (m["created"], m["id"]))
    return out


# ── the working copy ──

def _when(anchor: float, day: int, minute: int) -> float:
    """A local time `day` days before the anchor at `minute` past midnight, never after the anchor."""
    base = dt.datetime.fromtimestamp(anchor).replace(hour=0, minute=0, second=0, microsecond=0)
    t = (base - dt.timedelta(days=day) + dt.timedelta(minutes=minute)).timestamp()
    while t > anchor - 600:
        t -= 86400
    return t


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def materialize(profile: dict, paths: dict, skeleton: list[dict], anchor: float | None = None,
                community_src=None, mode: int = 0o600) -> dict:
    """Write the decoy's working copy. paths: {"members", "family_board", "board", "posture", "supplies"}
    (any missing key is skipped). Returns {"names": {member id: decoy name}}."""
    anchor = float(anchor if isinstance(anchor, (int, float)) and anchor > 0 else time.time())
    p = normalize(profile)
    rng = random.Random(p["seed"] ^ 0xD0C5)
    names = p["names"]
    name_of = {m["id"]: names[i % len(names)] for i, m in enumerate(skeleton)}
    if "members" in paths:
        members = {}
        for i, m in enumerate(skeleton):
            members[m["id"]] = {
                "name": name_of[m["id"]], "created": m["created"] or anchor - 86400 * (60 + i),
                "devices": [{"id": x["id"], "key": x["key"], "created": x["created"] or anchor - 86400 * 30,
                             "label": "phone" if j == 0 else "tablet", "last_seen": anchor - rng.randint(60, 7200),
                             **({"pending": True, "expires": x["expires"]} if x.get("pending") else {})}
                            for j, x in enumerate(m["devices"])],
                "radios": list(m["radios"])}
        write_file(paths["members"], json.dumps({"v": 1, "code_salt": secrets.token_hex(16), "members": members,
                                                 "invites": {}}, indent=1), mode)
    if "family_board" in paths:
        lines = []
        for b in p["board"]:
            t = _when(anchor, b["day"], b["minute"])
            rec = {"at": _iso(t), "text": b["text"], "via": "local"}
            if skeleton:
                m = skeleton[b["who"] % len(skeleton)]
                rec.update(name=name_of[m["id"]], member=m["id"], verified=True)
            else:
                rec["name"] = names[b["who"] % len(names)]
            lines.append((t, rec))
        lines.sort(key=lambda x: x[0])
        write_file(paths["family_board"], "".join(json.dumps(r) + "\n" for _, r in lines), mode)
    if "board" in paths:
        out = []
        try:
            src = Path(community_src).read_bytes()[-512_000:] if community_src else b""
        except OSError:
            src = b""
        for ln in src.split(b"\n")[-500:]:
            try:
                rec = json.loads(ln)
            except (ValueError, RecursionError):
                continue
            if not isinstance(rec, dict):
                continue
            mid = rec.get("member")
            if isinstance(mid, str) and not mid.startswith("feature-"):
                if mid not in name_of:
                    continue                          # a removed member's post: their real name stays out
                rec["name"] = name_of[mid]
            out.append(json.dumps(rec))
        write_file(paths["board"], "".join(x + "\n" for x in out), mode)
    if "posture" in paths:
        write_file(paths["posture"], json.dumps({"level": "guarded"}), mode)
    if "supplies" in paths:
        today = dt.date.fromtimestamp(anchor)
        items, log = {}, []
        n = max(1, len(skeleton))
        for k, s in enumerate(p["supplies"]):
            iid = _rid(rng)
            exp = s["expires_in_days"]
            items[iid] = {"name": s["name"], "category": s["category"], "qty": float(s["qty"]), "unit": s["unit"],
                          "location": s["location"],
                          "expiry": (today + dt.timedelta(days=exp)).isoformat() if exp is not None else "",
                          "min": float(s["min"]), "kcal": float(s["kcal"]), "litres": float(s["litres"]),
                          "hours": float(s["hours"]), "note": ""}
            who = name_of[skeleton[k % n]["id"]] if skeleton else names[0]
            log.append({"at": int(anchor - 86400 * rng.randint(9, 40) - rng.randint(0, 40000)), "who": who,
                        "what": "added", "name": s["name"], "unit": s["unit"], "qty": float(s["qty"])})
        log.sort(key=lambda r: r["at"])
        people = len(skeleton) if skeleton else 2
        d = {"items": items, "household": {"people": people, "pets": 0, "kcal": 2000, "water_l": 3.8, "set": True},
             "log": log, "notify": {}}
        write_file(paths["supplies"], json.dumps(d, separators=(",", ":"), ensure_ascii=False), mode)
    return {"names": name_of}


# ── state other programs check (the panel, the wipe) ──

def sealed_dir(raw: dict | None) -> Path:
    """Where sealed records keep device.key: [sealed] dir, else next to [portal] members_file."""
    raw = raw if isinstance(raw, dict) else {}
    s = raw.get("sealed") if isinstance(raw.get("sealed"), dict) else {}
    if isinstance(s.get("dir"), str) and os.path.isabs(s["dir"]):
        return Path(s["dir"])
    p = raw.get("portal") if isinstance(raw.get("portal"), dict) else {}
    mf = p.get("members_file")
    if isinstance(mf, str) and os.path.isabs(mf):
        return Path(mf).parent / "sealed"
    return Path(DEFAULT_SEALED_DIR)


def hard_locked(folder) -> float | None:
    """When the sealed records were hard-locked by a duress (the time), else None. A damaged key file: None."""
    try:
        raw = (Path(folder) / "device.key").read_bytes()
        if len(raw) > 1 << 20:
            return None
        d = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    hl = d.get("hard_lock") if isinstance(d, dict) else None
    if isinstance(hl, dict):
        at = hl.get("at")
        return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 1.0
    return None


def duress_action(raw: dict | None, settings_file=None) -> str:
    """"lock" (default) or "wipe": the admin page's choice (portal-settings.json "duress_action") wins over
    [security] duress_action in prepper.toml."""
    if settings_file:
        try:
            s = json.loads(Path(settings_file).read_text())
            v = s.get("duress_action") if isinstance(s, dict) else None
            if v in ("lock", "wipe"):
                return v
        except (OSError, ValueError, RecursionError):
            pass
    raw = raw if isinstance(raw, dict) else {}
    sec = raw.get("security") if isinstance(raw.get("security"), dict) else {}
    return sec.get("duress_action") if sec.get("duress_action") in ("lock", "wipe") else "lock"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("ensure", "show"):
        print("usage: decoy.py ensure|show [FILE]", file=sys.stderr)
        return 2
    path = Path(argv[1] if len(argv) > 1 else DEFAULT_FILE)
    if argv[0] == "ensure":
        had = load(path) is not None
        ensure(path)
        print("decoy profile " + ("kept" if had else "made") + f": {path}")
        return 0
    p = load(path)
    if p is None:
        print("no decoy profile yet", file=sys.stderr)
        return 1
    print(json.dumps(p, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
