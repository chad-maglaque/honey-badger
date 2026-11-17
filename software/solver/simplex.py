"""
A small dense two-phase simplex with exact fractions and Bland's rule (never cycles).

    linprog(objective=[3, 5], constraints=[([1, 0], "<=", 4), ([0, 2], "<=", 12), ([3, 2], "<=", 18)])
    → {"status": "optimal", "x": [2, 6], "value": 36}

All variables are ≥ 0. Constraints are (coefficients, "<=" | ">=" | "=", right-hand side).
Sizes are capped (MAX_VARS × MAX_ROWS) so a hostile problem can't tie up the Badger.
"""
from __future__ import annotations

import math
from fractions import Fraction

MAX_VARS = 40
MAX_ROWS = 40
MAX_ITER = 5000


class LPError(ValueError):
    pass


def _frac(v) -> Fraction:
    if isinstance(v, bool):
        raise LPError("expected a number")
    if isinstance(v, Fraction):
        return v
    if isinstance(v, int):
        if abs(v) > 10 ** 30:
            raise LPError("number too large")
        return Fraction(v)
    if isinstance(v, float):
        if not math.isfinite(v) or abs(v) > 1e30:
            raise LPError("expected a finite number")
        return Fraction(str(v))
    if isinstance(v, str):
        s = v.strip()
        if len(s) > 40:
            raise LPError("number too long")
        try:
            f = Fraction(s)
        except (ValueError, ZeroDivisionError):
            raise LPError(f"not a number: {s!r}") from None
        if abs(f) > 10 ** 30:
            raise LPError("number too large")
        return f
    raise LPError("expected a number")


def _pivot(T: list[list[Fraction]], r: int, c: int) -> None:
    piv = T[r][c]
    T[r] = [v / piv for v in T[r]]
    row = T[r]
    for i, other in enumerate(T):
        if i != r and other[c] != 0:
            f = other[c]
            T[i] = [a - f * b for a, b in zip(other, row, strict=True)]


def _run(T, basis, allowed: int) -> str:
    """Maximise; the objective row is T[-1] (reduced costs; entering column has a negative entry)."""
    m = len(T) - 1
    for _ in range(MAX_ITER):
        z = T[-1]
        enter = next((j for j in range(allowed) if z[j] < 0), None)
        if enter is None:
            return "optimal"
        best = None
        for i in range(m):
            a = T[i][enter]
            if a > 0:
                ratio = T[i][-1] / a
                if best is None or ratio < best[0] or (ratio == best[0] and basis[i] < basis[best[1]]):
                    best = (ratio, i)
        if best is None:
            return "unbounded"
        _pivot(T, best[1], enter)
        basis[best[1]] = enter
    raise LPError("the problem did not converge")


def linprog(objective, constraints, maximize: bool = True) -> dict:
    if not isinstance(objective, (list, tuple)) or not objective:
        raise LPError("objective must be a list of numbers")
    n = len(objective)
    if n > MAX_VARS:
        raise LPError(f"at most {MAX_VARS} variables")
    if not isinstance(constraints, (list, tuple)) or len(constraints) > MAX_ROWS:
        raise LPError(f"constraints must be a list of at most {MAX_ROWS}")
    c = [_frac(v) for v in objective]
    if not maximize:
        c = [-v for v in c]
    rows = []
    for k, con in enumerate(constraints):
        if isinstance(con, dict):
            con = (con.get("coeffs"), con.get("op"), con.get("rhs"))
        if not isinstance(con, (list, tuple)) or len(con) != 3:
            raise LPError(f"constraint {k + 1}: expected [coefficients, op, rhs]")
        a, op, b = con
        if not isinstance(a, (list, tuple)) or len(a) != n:
            raise LPError(f"constraint {k + 1}: needs {n} coefficients")
        if op not in ("<=", ">=", "=", "==", "≤", "≥"):
            raise LPError(f"constraint {k + 1}: op must be <=, >= or =")
        op = {"≤": "<=", "≥": ">=", "==": "="}.get(op, op)
        a = [_frac(v) for v in a]
        b = _frac(b)
        if b < 0:
            a, b = [-v for v in a], -b
            op = {"<=": ">=", ">=": "<=", "=": "="}[op]
        rows.append((a, op, b))
    m = len(rows)
    n_slack = sum(1 for _, op, _ in rows if op in ("<=", ">="))
    n_art = sum(1 for _, op, _ in rows if op in (">=", "="))
    width = n + n_slack + n_art + 1
    T, basis = [], []
    s_i, a_i = n, n + n_slack
    art_cols = []
    for a, op, b in rows:
        row = a + [Fraction(0)] * (n_slack + n_art) + [b]
        if op == "<=":
            row[s_i] = Fraction(1)
            basis.append(s_i)
            s_i += 1
        elif op == ">=":
            row[s_i] = Fraction(-1)
            s_i += 1
            row[a_i] = Fraction(1)
            basis.append(a_i)
            art_cols.append(a_i)
            a_i += 1
        else:
            row[a_i] = Fraction(1)
            basis.append(a_i)
            art_cols.append(a_i)
            a_i += 1
        T.append(row)
    # phase 1: maximise −Σ artificials
    if art_cols:
        z = [Fraction(0)] * width
        for j in art_cols:
            z[j] = Fraction(1)
        for i, bj in enumerate(basis):
            if bj in art_cols:
                z = [x - y for x, y in zip(z, T[i], strict=True)]
        T.append(z)
        _run(T, basis, width - 1)
        if T[-1][-1] < 0:
            return {"status": "infeasible", "x": None, "value": None,
                    "message": "No solution satisfies all the constraints."}
        T.pop()
        # drive artificials out of the basis (or drop redundant rows)
        i = 0
        while i < len(T):
            if basis[i] in art_cols:
                j = next((j for j in range(n + n_slack) if T[i][j] != 0), None)
                if j is None:
                    T.pop(i)
                    basis.pop(i)
                    continue
                _pivot(T, i, j)
                basis[i] = j
            i += 1
        art_set = set(art_cols)
        keep = [j for j in range(width) if j not in art_set]
        T = [[row[j] for j in keep] for row in T]
        width = len(keep)
    # phase 2
    z = [Fraction(0)] * width
    for j in range(n):
        z[j] = -c[j]
    for i, bj in enumerate(basis):
        if z[bj] != 0:
            f = z[bj]
            z = [x - f * y for x, y in zip(z, T[i], strict=True)]
    T.append(z)
    status = _run(T, basis, width - 1)
    if status == "unbounded":
        return {"status": "unbounded", "x": None, "value": None,
                "message": "The objective can grow without limit: a constraint is missing."}
    x = [Fraction(0)] * n
    for i, bj in enumerate(basis):
        if bj < n:
            x[bj] = T[i][-1]
    value = T[-1][-1]
    if not maximize:
        value = -value
    del m
    return {"status": "optimal", "x": x, "value": value}
