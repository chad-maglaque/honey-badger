"""
Built-in self-test: a fixed set of problems with known answers, run through the full solve loop
(model + tools) on the model under test. Score = problems answered correctly; speed = generated
tokens per second. A new model is only promoted to a role if it scores at least as well as the
current one (models.ModelManager.select).
"""
from __future__ import annotations

import re
import time
from fractions import Fraction

PROBLEMS = [
    {"id": "arith", "topic": "arithmetic", "answer": 61 / 72, "tol": 1e-3,
     "q": "What is 17/24 + 5/36? Give the exact value."},
    {"id": "units", "topic": "unit conversion", "answer": 208.198, "tol": 2e-3,
     "q": "A rain barrel holds 55 US gallons. How many litres is that?"},
    {"id": "physics", "topic": "physics", "answer": 19.809, "tol": 5e-3,
     "q": "A rock is dropped from a 20 m cliff. Ignoring air resistance (g = 9.81 m/s²), how fast is it going "
          "when it hits the ground, in m/s?"},
    {"id": "electric", "topic": "electrical", "answer": 5.0, "tol": 1e-3,
     "q": "A 60 W light runs from a 12 V battery. How many amps does it draw?"},
    {"id": "game", "topic": "payoff matrix", "answer": 1.0, "tol": 1e-3,
     "q": "Zero-sum game. The row player's payoffs are: row 1 = [3, -1], row 2 = [-2, 4] (columns are the column "
          "player's two choices). What is the value of the game to the row player when both play optimally?"},
    {"id": "code", "topic": "short code task", "answer": 76127, "tol": 0.0,
     "q": "What is the sum of all prime numbers below 1000? (A short program helps.)"},
]

SUFFIX = "\n\n(Self-test: end your final answer with one line exactly like  FINAL: <number>)"

_FINAL = re.compile(r"FINAL\s*[:=]\s*\**\s*([-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?)(?:\s*/\s*(\d+))?", re.I)


def extract(answer: str):
    """The number after the last FINAL:, as a float (a/b fractions allowed); None if there isn't one."""
    if not isinstance(answer, str):
        return None
    ms = list(_FINAL.finditer(answer[-4000:]))
    if not ms:
        return None
    m = ms[-1]
    try:
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v = float(Fraction(int(float(m.group(1).replace(",", ""))), int(m.group(2))))
        return v
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def correct(value, expected: float, tol: float) -> bool:
    if value is None:
        return False
    if tol == 0:
        return abs(value - expected) < 1e-9
    return abs(value - expected) <= tol * max(1.0, abs(expected))


def run(solve, progress=None, cancel=None, clock=time.monotonic) -> dict:
    """solve(problem_text) -> loop.Outcome-like dict with "answer", "tokens", "tps"/"gen_seconds".
    Returns {"score", "correct", "total", "tps", "seconds", "details"}."""
    progress = progress or (lambda *a, **k: None)
    t0 = clock()
    details, toks, gsec, ok = [], 0, 0.0, 0
    for i, p in enumerate(PROBLEMS):
        if cancel is not None and cancel.is_set():
            break
        progress("bench", f"Test {i + 1}/{len(PROBLEMS)}: {p['topic']}")
        try:
            out = solve(p["q"] + SUFFIX)
        except Exception as e:  # noqa: BLE001 — a failing problem scores zero, the test goes on
            out = {"answer": "", "tokens": 0, "gen_seconds": 0.0, "error": type(e).__name__}
        v = extract(out.get("answer", ""))
        good = correct(v, p["answer"], p["tol"])
        ok += good
        toks += int(out.get("tokens") or 0)
        gsec += float(out.get("gen_seconds") or 0.0)
        details.append({"id": p["id"], "topic": p["topic"], "got": v, "expected": p["answer"], "ok": good})
        progress("bench", f"{p['topic']}: {'right' if good else 'wrong'} ({v if v is not None else 'no FINAL line'})", ok=good)
    total = len(PROBLEMS)
    return {"score": ok, "correct": ok, "total": total, "tps": round(toks / gsec, 2) if gsec > 0 else 0.0,
            "seconds": round(clock() - t0, 1), "at": time.time(), "details": details}
