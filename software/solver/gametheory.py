"""
Game theory and decisions, exact (fractions):

  nash(A, B)          all Nash equilibria of a 2-player game (pure, and mixed by support enumeration)
  zero_sum(A)         value and optimal mixed strategies of a zero-sum game (linear programming)
  expected_value(outcomes)   EV, variance, standard deviation of a lottery
  decision_tree(tree)  best choices and expected value of a decision tree

A[i][j] is the row player's payoff, B[i][j] the column player's (B omitted: zero-sum, B = −A).
"""
from __future__ import annotations

import itertools
import math
from fractions import Fraction

from . import simplex

MAX_STRATEGIES = 8          # support enumeration is 2^m · 2^n small linear systems
MAX_TREE_NODES = 300
MAX_TREE_DEPTH = 12


class GameError(ValueError):
    pass


def _f(v) -> Fraction:
    try:
        return simplex._frac(v)
    except simplex.LPError as e:
        raise GameError(str(e)) from None


def _matrix(M, name) -> list[list[Fraction]]:
    if not isinstance(M, (list, tuple)) or not M or not all(isinstance(r, (list, tuple)) for r in M):
        raise GameError(f"{name} must be a table of numbers (a list of rows)")
    n = len(M[0])
    if n == 0 or any(len(r) != n for r in M):
        raise GameError(f"every row of {name} needs the same number of entries")
    if len(M) > MAX_STRATEGIES or n > MAX_STRATEGIES:
        raise GameError(f"at most {MAX_STRATEGIES} strategies per player")
    return [[_f(v) for v in r] for r in M]


def _solve(Mx: list[list[Fraction]], rhs: list[Fraction]):
    """Gaussian elimination, exact. None if singular."""
    n = len(Mx)
    A = [row[:] + [b] for row, b in zip(Mx, rhs, strict=True)]
    for col in range(n):
        piv = next((r for r in range(col, n) if A[r][col] != 0), None)
        if piv is None:
            return None
        A[col], A[piv] = A[piv], A[col]
        p = A[col][col]
        A[col] = [v / p for v in A[col]]
        for r in range(n):
            if r != col and A[r][col] != 0:
                f = A[r][col]
                A[r] = [a - f * b for a, b in zip(A[r], A[col], strict=True)]
    return [A[r][n] for r in range(n)]


def _indifferent(P: list[list[Fraction]], rows: tuple, cols: tuple):
    """Mix over `cols` making the player whose payoffs are P[i][j] indifferent over `rows`.
    Returns (mix over cols, value) or None."""
    k = len(rows)
    M = [[P[i][j] for j in cols] + [Fraction(-1)] for i in rows]
    M.append([Fraction(1)] * k + [Fraction(0)])
    sol = _solve(M, [Fraction(0)] * k + [Fraction(1)])
    if sol is None:
        return None
    return sol[:k], sol[k]


def pure_equilibria(A, B) -> list[tuple[int, int]]:
    m, n = len(A), len(A[0])
    out = []
    for i in range(m):
        for j in range(n):
            if A[i][j] >= max(A[r][j] for r in range(m)) and B[i][j] >= max(B[i][c] for c in range(n)):
                out.append((i, j))
    return out


def nash(A, B=None) -> dict:
    A = _matrix(A, "the row player's payoffs")
    B = [[-v for v in r] for r in A] if B is None else _matrix(B, "the column player's payoffs")
    m, n = len(A), len(A[0])
    if len(B) != m or len(B[0]) != n:
        raise GameError("both payoff tables must be the same size")
    Bt = [[B[i][j] for i in range(m)] for j in range(n)]          # column player's payoffs, by column
    eqs, seen = [], set()
    for k in range(1, min(m, n) + 1):
        for I in itertools.combinations(range(m), k):
            for J in itertools.combinations(range(n), k):
                rq = _indifferent(A, I, J)                         # column mix q makes row indifferent on I
                cp = _indifferent(Bt, J, I)                        # row mix p makes column indifferent on J
                if rq is None or cp is None:
                    continue
                qJ, v = rq
                pI, u = cp
                if any(x < 0 for x in qJ) or any(x < 0 for x in pI):
                    continue
                p = [Fraction(0)] * m
                q = [Fraction(0)] * n
                for idx, x in zip(I, pI, strict=True):
                    p[idx] = x
                for idx, x in zip(J, qJ, strict=True):
                    q[idx] = x
                Aq = [sum(A[i][j] * q[j] for j in range(n)) for i in range(m)]
                pB = [sum(p[i] * B[i][j] for i in range(m)) for j in range(n)]
                if any(Aq[i] > v for i in range(m)) or any(pB[j] > u for j in range(n)):
                    continue
                key = (tuple(p), tuple(q))
                if key in seen:
                    continue
                seen.add(key)
                eqs.append({"row": p, "col": q, "row_payoff": v, "col_payoff": u,
                            "pure": all(x in (0, 1) for x in p + q)})
    return {"equilibria": eqs, "pure": pure_equilibria(A, B),
            "note": "" if eqs else "No equilibrium found by support enumeration (the game may be degenerate)."}


def zero_sum(A) -> dict:
    """Value v and optimal strategies: the row player maximises min over columns."""
    A = _matrix(A, "the payoff table")
    m, n = len(A), len(A[0])
    shift = 1 - min(min(r) for r in A)                  # make every entry ≥ 1, so the value is > 0
    S = [[v + shift for v in r] for r in A]
    # row player: minimise Σx  s.t.  Sᵀx ≥ 1, x ≥ 0;   value = 1/Σx
    res = simplex.linprog([1] * m, [([S[i][j] for i in range(m)], ">=", 1) for j in range(n)], maximize=False)
    if res["status"] != "optimal":
        raise GameError("could not solve the game")
    tot = res["value"]
    p = [x / tot for x in res["x"]]
    # column player: maximise Σy  s.t.  S y ≤ 1
    res2 = simplex.linprog([1] * n, [(S[i], "<=", 1) for i in range(m)], maximize=True)
    q = [y / res2["value"] for y in res2["x"]]
    return {"value": 1 / tot - shift, "row": p, "col": q}


def expected_value(outcomes) -> dict:
    """outcomes: [{"p": 0.3, "value": 100}, …] or [[p, value], …]. Probabilities must sum to 1
    (within 0.001; "rest" for one probability takes whatever is left)."""
    if not isinstance(outcomes, (list, tuple)) or not outcomes or len(outcomes) > 100:
        raise GameError("outcomes must be a list of 1–100 [probability, value] pairs")
    ps, vs, rest = [], [], None
    for k, o in enumerate(outcomes):
        if isinstance(o, dict):
            p, v = o.get("p", o.get("prob", o.get("probability"))), o.get("value", o.get("v"))
        elif isinstance(o, (list, tuple)) and len(o) == 2:
            p, v = o
        else:
            raise GameError(f"outcome {k + 1}: expected [probability, value]")
        if p == "rest":
            if rest is not None:
                raise GameError("only one probability can be \"rest\"")
            rest = k
            ps.append(Fraction(0))
        else:
            p = _f(p)
            if p < 0 or p > 1:
                raise GameError(f"outcome {k + 1}: probability must be between 0 and 1")
            ps.append(p)
        vs.append(_f(v))
    total = sum(ps)
    if rest is not None:
        if total > 1:
            raise GameError("the probabilities add up to more than 1")
        ps[rest] = 1 - total
        total = Fraction(1)
    if abs(total - 1) > Fraction(1, 1000):
        raise GameError(f"the probabilities add up to {float(total):g}, not 1")
    ev = sum(p * v for p, v in zip(ps, vs, strict=True)) / total
    var = sum(p * (v - ev) ** 2 for p, v in zip(ps, vs, strict=True)) / total
    return {"expected_value": ev, "variance": var, "std_dev": math.sqrt(float(var)),
            "best": max(vs), "worst": min(vs)}


def decision_tree(tree) -> dict:
    """Nodes: {"decision": "name", "options": [{"label", "cost"?, "node"}]}
              {"chance": "name", "branches": [{"label", "p", "node"}]}
              {"value": number}  (an outcome; a bare number works too)
    Returns the expected value at the root and the best option at every decision node."""
    count = [0]
    policy = []

    def walk(node, depth, path):
        count[0] += 1
        if count[0] > MAX_TREE_NODES:
            raise GameError(f"the tree is too big (limit {MAX_TREE_NODES} nodes)")
        if depth > MAX_TREE_DEPTH:
            raise GameError(f"the tree is too deep (limit {MAX_TREE_DEPTH})")
        if isinstance(node, (int, float, str)) and not isinstance(node, bool):
            return _f(node)
        if not isinstance(node, dict):
            raise GameError(f"{path or 'root'}: a node must be a decision, chance or value")
        if "value" in node:
            return _f(node["value"])
        if "options" in node:
            opts = node["options"]
            if not isinstance(opts, list) or not opts or len(opts) > 20:
                raise GameError(f"{path or 'root'}: options must be a list of 1–20")
            best = None
            vals = []
            for k, o in enumerate(opts):
                if not isinstance(o, dict):
                    raise GameError(f"{path}: option {k + 1} must be an object")
                label = str(o.get("label", f"option {k + 1}"))[:60]
                v = walk(o.get("node", o.get("value")), depth + 1, f"{path}/{label}") - _f(o.get("cost", 0))
                vals.append({"label": label, "value": v})
                if best is None or v > best[1]:
                    best = (label, v)
            policy.append({"at": str(node.get("decision", path or "start"))[:60], "choose": best[0],
                           "value": best[1], "options": vals})
            return best[1]
        if "branches" in node:
            br = node["branches"]
            if not isinstance(br, list) or not br or len(br) > 20:
                raise GameError(f"{path or 'root'}: branches must be a list of 1–20")
            tot, ev = Fraction(0), Fraction(0)
            for k, b in enumerate(br):
                if not isinstance(b, dict):
                    raise GameError(f"{path}: branch {k + 1} must be an object")
                p = _f(b.get("p", b.get("prob")))
                if p < 0 or p > 1:
                    raise GameError(f"{path}: probabilities must be between 0 and 1")
                label = str(b.get("label", f"branch {k + 1}"))[:60]
                tot += p
                ev += p * walk(b.get("node", b.get("value")), depth + 1, f"{path}/{label}")
            if abs(tot - 1) > Fraction(1, 1000):
                raise GameError(f"{path or 'root'}: branch probabilities add up to {float(tot):g}, not 1")
            return ev / tot
        raise GameError(f"{path or 'root'}: a node needs options, branches or a value")

    ev = walk(tree, 0, "")
    return {"expected_value": ev, "policy": list(reversed(policy))}
