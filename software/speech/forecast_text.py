"""
forecast_text — turn a whisper transcript of NOAA Weather Radio into a readable written forecast.

Pure functions (stdlib only), used by forecastd.py and tested in tests/test_forecast.py:

  signal_check(samples, rate)   silence / static / speech, from the raw radio audio (before whisper)
  looks_like_weather(text)      did whisper hear a forecast, or hallucinate over noise?
  dedupe(sentences)             the broadcast loops every few minutes: keep each sentence once,
                                preferring the complete copy over a fragment cut off by the recording
  sectionize(sentences)         split into hazards / forecast (by period) / current conditions / …
  short_summary(sections, at)   ≤ 200 bytes for the mesh radio, rule-based (always works)
  ai_summary_ok(text, source)   checks an AI-written short version before it is used
"""
from __future__ import annotations

import difflib
import math
import re
import time

MESH_BYTES = 200

# ─────────────────────────────── signal ──────────────────────────────────────


def signal_check(samples, rate: int) -> dict:
    """samples: a sequence of 16-bit ints (mono). Returns {"verdict": "speech"|"silence"|"static", …}.

    No carrier on an FM receiver without squelch is loud, even hiss: high zero-crossing rate and
    a flat loudness envelope. Speech comes in syllables and pauses: the 20 ms loudness envelope
    varies a lot (coefficient of variation well above 0.4) and crosses zero far less often."""
    n = len(samples)
    frame = max(1, int(rate * 0.02))
    nframes = n // frame
    if nframes < 10:
        return {"verdict": "silence", "rms": 0.0, "env_cv": 0.0, "zcr": 0.0, "seconds": n / max(rate, 1)}
    stride = max(1, nframes // 3000)                       # ≤ 3000 frames spread across the capture
    env, zc, total = [], 0, 0
    for fi in range(0, nframes, stride):
        seg = samples[fi * frame:(fi + 1) * frame]
        env.append(math.sqrt(sum(x * x for x in seg) / len(seg)))
        prev = seg[0]
        for x in seg[1:]:
            if (x >= 0) != (prev >= 0):
                zc += 1
            prev = x
        total += len(seg) - 1
    mean = sum(env) / len(env)
    rms = math.sqrt(sum(e * e for e in env) / len(env))
    sd = math.sqrt(sum((e - mean) ** 2 for e in env) / len(env))
    cv = sd / mean if mean > 0 else 0.0
    zcr = zc / total if total else 0.0
    if rms < 150:
        verdict = "silence"
    elif cv < 0.3 or (zcr > 0.3 and cv < 0.5):
        verdict = "static"
    else:
        verdict = "speech"
    return {"verdict": verdict, "rms": round(rms, 1), "env_cv": round(cv, 3), "zcr": round(zcr, 3),
            "seconds": round(n / rate, 1)}


WEATHER_WORDS = {
    "forecast", "weather", "temperature", "temperatures", "degrees", "wind", "winds", "mph", "gusts",
    "rain", "showers", "thunderstorms", "storms", "snow", "sleet", "drizzle", "fog", "cloudy", "sunny",
    "clear", "highs", "lows", "high", "low", "humidity", "dewpoint", "pressure", "tonight", "today",
    "precipitation", "percent", "chance", "noaa", "warning", "watch", "advisory", "outlook", "visibility",
    "barometric", "partly", "mostly", "breezy", "frost", "freeze", "hail",
}


def looks_like_weather(text: str) -> bool:
    """Whisper invents short phrases ("Thank you.") over noise; a real forecast has many weather words."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    hits = sum(1 for w in words if w in WEATHER_WORDS)
    return len(words) >= 15 and hits >= 4


# ─────────────────────────────── sentences & dedupe ──────────────────────────

def sentences(text: str) -> list[str]:
    text = re.sub(r"\[[^\]]{0,40}\]|\([^)]{0,40}\)|♪+", " ", text or "")     # whisper's non-speech marks
    text = re.sub(r"\s+", " ", text).strip()
    # keep "a.m." / "p.m." / "St." together
    text = re.sub(r"\b([ap])\.\s?m\.", lambda m: m.group(1) + "m", text, flags=re.I)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    out = []
    for p in parts:
        p = p.strip()
        if p and len(p) > 1:
            out.append(p[:600])
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def _digits(n: str) -> list[str]:
    return re.findall(r"\d+", n)


def _sublist(a: list, b: list) -> bool:
    return not a or any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1))


def _same(a: str, b: str, threshold: float = 0.85) -> bool:
    """Two normalized sentences say the same thing: equal, one a cut-off piece of the other, or a
    close re-hearing with exactly the same numbers ("highs near 90" ≠ "highs near 91")."""
    if a == b:
        return True
    da, db = _digits(a), _digits(b)
    if len(a) >= 12 and a in b and _sublist(da, db):
        return True
    if len(b) >= 12 and b in a and _sublist(db, da):
        return True
    if da != db:
        return False
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sm.real_quick_ratio() >= threshold and sm.quick_ratio() >= threshold and sm.ratio() >= threshold


def dedupe(sents: list[str]) -> list[str]:
    """Remove the repeated loop. The broadcast repeats whole stretches, so a sentence is a repeat only
    when it AND a neighbour match an earlier sentence and its neighbour — a short line that rightly
    occurs twice ("South winds 5 to 10 mph." in two periods) is kept. When the earlier copy was cut
    off by the start of the recording, the complete later copy takes its place. Whisper's own repeat
    loops ("Thank you. Thank you. …") go the same way."""
    if sents and sents[0][:1].islower():                  # the recording started mid-sentence
        sents = sents[1:]
    items = [(s, _norm(s)) for s in sents]
    items = [(s, n) for s, n in items if len(n) >= 3]
    norms = [n for _, n in items]
    out: list[str | None] = [s for s, _ in items]
    for i in range(1, len(norms)):
        for j in range(i):
            if out[j] is None or not _same(norms[i], norms[j]):
                continue
            nxt = i + 1 < len(norms) and _same(norms[i + 1], norms[j + 1])
            prv = j > 0 and _same(norms[i - 1], norms[j - 1])
            if nxt or prv or (i == len(norms) - 1 and norms[i] in norms[j]):
                if len(norms[i]) > len(norms[j]) and norms[j] in norms[i]:
                    out[j] = items[i][0]                  # the earlier copy was a fragment
                out[i] = None
                break
    return [s for s in out if s is not None]


# ─────────────────────────────── sections ────────────────────────────────────

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PERIOD = re.compile(
    r"^(?:the\s+)?(today|tonight|this afternoon|this evening|this morning|rest of today|rest of tonight|"
    r"overnight|late tonight|(?:%s)(?:\s+night)?|(?:new year's|christmas|thanksgiving|independence)\s+day(?:\s+night)?)"
    r"\s*[,:.]\s*" % "|".join(DAYS), re.I)

SECTIONS = [  # (kind, title, header pattern) — checked in order; the first match starts a section
    ("station", "About this broadcast", re.compile(
        r"this is noaa weather radio|you are listening to noaa|noaa weather radio (?:station|all hazards)|"
        r"(?:broadcasting|transmitting) (?:from|on)|the national weather service office in \w+ (?:operates|broadcasts)", re.I)),
    ("hazards", "Hazardous weather outlook", re.compile(r"hazardous weather outlook", re.I)),
    ("statements", "Watches, warnings & statements", re.compile(
        r"(?:has|have) issued an? |\b(?:warning|watch|advisory|statement) (?:is|remains) in effect|"
        r"special weather statement|until \d{1,2}(?::\d\d)? ?[ap]m\b.*\b(?:warning|watch|advisory)", re.I)),
    ("marine", "Marine forecast", re.compile(r"marine (?:weather|forecast)|coastal waters|small craft|lake .* forecast", re.I)),
    ("current", "Current conditions", re.compile(
        r"current (?:weather )?(?:conditions|observations)|regional weather (?:roundup|summary)|"
        r"at \d{1,2}(?::\d\d)? ?(?:[ap]m|o'clock),|the temperature (?:was|is) \d|"
        r"(?:relative )?humidity (?:was|is) \d|barometric pressure|dew ?point (?:was|is)", re.I)),
    ("climate", "Climate summary", re.compile(r"climate (?:summary|report)|yesterday'?s high|record (?:high|low) for", re.I)),
    ("forecast", "Forecast", re.compile(r"\bforecast\b|\bzone forecast\b", re.I)),
]
ORDER = ["hazards", "statements", "forecast", "current", "marine", "climate", "other", "station"]
TITLES = {k: t for k, t, _ in SECTIONS} | {"other": "Other information"}


def _kind_of(sentence: str) -> str | None:
    if PERIOD.match(sentence):
        return "forecast"
    for kind, _t, rx in SECTIONS:
        if rx.search(sentence):
            if kind == "forecast" and re.search(r"marine|hazardous", sentence, re.I):
                continue
            return kind
    return None


def period_rank(name: str, at: float) -> int:
    """Chronological position of a forecast period, counted from the capture day (the capture
    usually starts in the middle of the loop, so "Tonight" can come before "Today")."""
    n = name.lower()
    if n in ("today", "this morning", "this afternoon", "rest of today"):
        return {"this morning": 0, "today": 1, "rest of today": 1, "this afternoon": 2}[n]
    if n in ("this evening", "tonight", "rest of tonight", "late tonight", "overnight"):
        return 3
    first = n.split()[0] if n else ""
    if first in DAYS:
        ahead = (DAYS.index(first) - time.localtime(at).tm_wday) % 7 or 7
        return 4 + ahead * 2 + (1 if n.endswith("night") else 0)
    return 100


def sectionize(sents: list[str], at: float | None = None) -> list[dict]:
    """[{kind, title, text, periods?: [{name, text}], intro?}] in a fixed reading order."""
    buckets: dict[str, list[str]] = {}
    current = "other"
    for s in sents:
        k = _kind_of(s)
        if k:
            current = k
        buckets.setdefault(current, []).append(s)
    out = []
    for kind in ORDER:
        if kind not in buckets:
            continue
        sec = {"kind": kind, "title": TITLES[kind], "text": " ".join(buckets[kind])}
        if kind == "forecast":
            intro, periods, cur = [], [], None
            for s in buckets[kind]:
                m = PERIOD.match(s)
                if m:
                    cur = {"name": m.group(1).strip().title(), "text": s[m.end():].strip()}
                    periods.append(cur)
                elif re.search(r"\bforecast\b", s, re.I):      # "Here is the forecast for …": a new block
                    cur = None
                    intro.append(s)
                elif cur is not None:
                    cur["text"] = (cur["text"] + " " + s).strip()
                else:
                    intro.append(s)
            # the loop may give the same period twice (start and end of the capture): keep the longer
            merged: dict[str, dict] = {}
            for p in periods:
                key = p["name"].lower()
                if key not in merged or len(p["text"]) > len(merged[key]["text"]):
                    merged[key] = p
            when = time.time() if at is None else at
            sec["periods"] = sorted(merged.values(), key=lambda p: period_rank(p["name"], when))
            sec["intro"] = " ".join(dict.fromkeys(intro))
            sec["text"] = " ".join(filter(None, [sec["intro"]] + [f"{p['name']}: {p['text']}" for p in sec["periods"]]))
        out.append(sec)
    return out


def forecast_key(sections: list[dict]) -> str:
    """What decides whether a new capture is a new forecast: the forecast (else everything but the
    station ID and current conditions, which change every hour)."""
    f = [s for s in sections if s["kind"] == "forecast"]
    if f:
        return _norm(f[0]["text"])
    return _norm(" ".join(s["text"] for s in sections if s["kind"] not in ("station", "current")))


def same_forecast(a: str, b: str) -> bool:
    """Same forecast as last time: the same numbers in the same order, and nearly the same words
    (a re-issued forecast usually changes a temperature or a chance of rain)."""
    if not a or not b or _digits(a) != _digits(b):
        return False
    return difflib.SequenceMatcher(None, a[:4000], b[:4000], autojunk=False).ratio() >= 0.9


def process(raw_text: str, at: float | None = None) -> dict:
    sents = dedupe(sentences(raw_text))
    secs = sectionize(sents, at)
    return {"sections": secs, "text": " ".join(sents), "key": forecast_key(secs)}


# ─────────────────────────────── short version ───────────────────────────────

ABBR = {d: d[:3].title() for d in DAYS}
DIRS = {"north": "N", "south": "S", "east": "E", "west": "W", "northeast": "NE", "northwest": "NW",
        "southeast": "SE", "southwest": "SW"}
NUMWORDS = {"zero": 0, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25, "thirty": 30}
CONDS = [  # (pattern, short) — precipitation first, then sky
    (r"thunderstorms?|t-storms?", "storms"), (r"freezing rain", "frz rain"), (r"sleet", "sleet"),
    (r"snow showers|snow", "snow"), (r"showers", "showers"), (r"drizzle", "drizzle"), (r"\brain\b", "rain"),
    (r"\bfog\b|foggy", "fog"), (r"mostly sunny", "mostly sunny"), (r"partly sunny", "partly sunny"),
    (r"mostly clear", "mostly clear"), (r"partly cloudy", "partly cloudy"), (r"mostly cloudy", "mostly cloudy"),
    (r"\bsunny\b", "sunny"), (r"\bclear\b", "clear"), (r"\bcloudy\b|overcast", "cloudy"), (r"\bwindy\b", "windy"),
]


def _label(name: str, at: float) -> str:
    n = name.lower()
    wd = time.strftime("%a", time.localtime(at))
    if n == "today" or n == "rest of today":
        return wd
    if n in ("this afternoon", "this evening"):
        return wd + (" PM" if n == "this afternoon" else " eve")
    if n == "this morning":
        return wd + " AM"
    if n in ("tonight", "overnight", "late tonight", "rest of tonight"):
        return "Tonight"
    parts = n.split()
    if parts and parts[0] in ABBR:
        return ABBR[parts[0]] + (" night" if n.endswith("night") else "")
    return name[:12]


def _num(tok: str) -> str:
    tok = tok.lower().replace("-", " ")
    return str(NUMWORDS[tok]) if tok in NUMWORDS else tok


PRECIP = ("storms", "frz rain", "sleet", "snow", "showers", "drizzle", "rain")


def period_bits(text: str) -> list[str]:
    t = text.lower()
    bits = []
    precip = next((short for rx, short in CONDS if short in PRECIP and re.search(rx, t)), None)
    sky = next((short for rx, short in CONDS if short not in PRECIP and short != "windy" and re.search(rx, t)), None)
    if precip:
        c = precip
        pct = re.search(r"(\d{1,3}) percent", t)
        if re.search(r"\blikely\b", t):
            c += " likely"
        elif pct:
            c += f" {pct.group(1)}%"
        elif re.search(r"slight chance", t):
            c = "slight chc " + c
        elif re.search(r"chance", t):
            c = "chc " + c
        if re.search(r"in the afternoon|this afternoon|in the evening", t):
            c += " PM"
        elif re.search(r"in the morning|before noon", t):
            c += " AM"
        elif re.search(r"after midnight", t):
            c += " late"
        bits.append(c)
    elif sky:
        bits.append(sky)
    if re.search(r"\bwindy\b", t):
        bits.append("windy")
    m = re.search(r"\b(highs?|lows?)\b(?:[^.]{0,30}?)\b(?:(lower|mid|upper)\s+)?(minus\s+)?(\d{1,3})s?\b", t)
    if m:
        kind = "hi" if m.group(1).startswith("high") else "lo"
        val = ("-" if m.group(3) else "") + m.group(4)
        if m.group(2):
            val = f"{m.group(2)} {val}s"
        bits.append(f"{kind} {val}")
        # a second temperature in the same period ("highs 78. lows 61")
        m2 = re.search(r"\b(highs?|lows?)\b(?:[^.]{0,30}?)\b(\d{1,3})", t[m.end():])
        if m2 and not m2.group(1).startswith(m.group(1)[:3]):
            bits.append(("hi " if m2.group(1).startswith("high") else "lo ") + m2.group(2))
    w = re.search(r"\b(north|south|east|west|northeast|northwest|southeast|southwest)(?:erly)?\s+winds?\s+"
                  r"(?:around\s+|near\s+|at\s+|up to\s+)?(\d{1,3}|five|ten|fifteen|twenty)"
                  r"(?:\s+to\s+(\d{1,3}|ten|fifteen|twenty|twenty five|thirty))?\s*(?:mph|miles)", t)
    if not w:
        w2 = re.search(r"\bwinds?\s+(north|south|east|west|northeast|northwest|southeast|southwest)\s+"
                       r"(?:around\s+|near\s+)?(\d{1,3})(?:\s+to\s+(\d{1,3}))?", t)
        w = w2
    if w:
        rng = _num(w.group(2)) + (f"-{_num(w.group(3))}" if w.group(3) else "")
        g = re.search(r"gusts?\s+(?:up\s+)?(?:to|around|near|as high as)\s+(\d{1,3})", t)
        bits.append(f"{DIRS[w.group(1)]} {rng} mph" + (f" g{g.group(1)}" if g else ""))
    elif re.search(r"light (?:and variable )?winds?|winds? light", t):
        bits.append("lt wind")
    return bits


def _fits(s: str) -> bool:
    return len(s.encode("utf-8")) <= MESH_BYTES


def short_summary(sections: list[dict], at: float | None = None) -> str | None:
    """'Wx Tue: storms likely, hi 78, S 15-25 mph. Tonight: lo 61.' — None when there is no forecast."""
    at = time.time() if at is None else at
    secs = {s["kind"]: s for s in sections}
    head = "Wx"
    alerts = []
    for k in ("statements", "hazards"):
        if k in secs:
            for m in re.finditer(r"\b((?:[a-z]+\s){0,2}(?:warning|watch|advisory))\b", secs[k]["text"], re.I):
                name = m.group(1).strip().title()
                if name.split()[0].lower() not in ("a", "an", "the", "no", "this", "that", "of", "for") \
                        and name not in alerts and len(name) < 32:
                    alerts.append(name)
    f = secs.get("forecast")
    periods = (f or {}).get("periods") or []
    if not periods:
        if f and f["text"]:
            txt = f"{head}: {f['text']}"
            while not _fits(txt):
                txt = txt[: -2].rstrip() + "…"
            return txt
        return None
    out = ""
    first = True
    for p in periods[:3]:
        bits = period_bits(p["text"])
        if not bits:
            continue
        piece = (f"{head} {_label(p['name'], at)}: " if first else f" {_label(p['name'], at)}: ") + ", ".join(bits) + "."
        if not _fits(out + piece):
            if first:
                while bits and not _fits(out + piece):
                    bits.pop()
                    piece = f"{head} {_label(p['name'], at)}: " + ", ".join(bits) + "."
                if not bits:
                    break
            else:
                break
        out += piece
        first = False
    if not out:
        return None
    if alerts:
        pre = f"{alerts[0]}! "
        if _fits(pre + out):
            out = pre + out
    return out


def ai_summary_ok(text: str, source: str) -> str | None:
    """An AI-written short version, cleaned; None if it must not be used: too long, more than one
    line of content, or containing a number that is not in the forecast (made up)."""
    if not isinstance(text, str):
        return None
    t = text.strip().strip('"').strip("'").strip()
    t = re.sub(r"\s+", " ", t)
    t = t.encode("ascii", "ignore").decode()
    if not t or not _fits(t) or len(t) < 15:
        return None
    src_nums = set(re.findall(r"\d+", source))
    for n in re.findall(r"\d+", t):
        if n not in src_nums:
            return None
    if not re.search(r"\d", t):
        return None
    if not t.lower().startswith("wx"):
        t = "Wx: " + t
        if not _fits(t):
            return None
    return t
