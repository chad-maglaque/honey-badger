"""
Problem solver: the exact simplex (linear programming) and the game-theory / decision solvers on
textbook cases, plus bad and oversized input. Run: python3.11 tests/test_solver_math.py
"""
from __future__ import annotations

import sys
import time
import unittest
from fractions import Fraction as F
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software"))

from solver import gametheory as gt  # noqa: E402
from solver import simplex as sx  # noqa: E402


class Simplex(unittest.TestCase):
    def test_wyndor_glass(self):
        # Hillier & Lieberman: max 3x + 5y, x ≤ 4, 2y ≤ 12, 3x + 2y ≤ 18 → (2, 6), z = 36
        r = sx.linprog([3, 5], [([1, 0], "<=", 4), ([0, 2], "<=", 12), ([3, 2], "<=", 18)])
        self.assertEqual(r["status"], "optimal")
        self.assertEqual(r["x"], [2, 6])
        self.assertEqual(r["value"], 36)

    def test_minimise_with_ge(self):
        # min 2x + 3y, x + y ≥ 10, x ≤ 4 → (4, 6), 26
        r = sx.linprog([2, 3], [([1, 1], ">=", 10), ([1, 0], "<=", 4)], maximize=False)
        self.assertEqual((r["x"], r["value"]), ([4, 6], 26))

    def test_diet_problem(self):
        # min 0.6a + 0.35b; protein 5a + 7b ≥ 8; carbs 4a + 2b ≥ 15; fat 2a + b ≥ 3 → a = 3.75 (15/4), b = 0, cost 2.25
        r = sx.linprog(["0.6", "0.35"], [([5, 7], ">=", 8), ([4, 2], ">=", 15), ([2, 1], ">=", 3)], maximize=False)
        self.assertEqual(r["status"], "optimal")
        self.assertEqual(r["value"], F(9, 4))
        self.assertEqual(r["x"], [F(15, 4), 0])

    def test_equalities_and_redundant_rows(self):
        r = sx.linprog([1, 1, 0], [([1, 1, 1], "=", 5), ([1, 0, 0], "=", 2), ([2, 1, 1], "=", 7)])
        self.assertEqual(r["status"], "optimal")
        self.assertEqual(r["value"], 5)

    def test_negative_rhs_is_flipped(self):
        r = sx.linprog([-1, -1], [([-1, -1], "<=", -2)], maximize=True)      # x + y ≥ 2, max −(x+y) → −2
        self.assertEqual(r["value"], -2)

    def test_infeasible_and_unbounded(self):
        self.assertEqual(sx.linprog([1, 1], [([1, 1], "<=", -1)])["status"], "infeasible")
        self.assertEqual(sx.linprog([1, 1], [([1, 1], ">=", 5), ([1, 1], "<=", 4)])["status"], "infeasible")
        self.assertEqual(sx.linprog([1, 1], [([1, -1], "<=", 1)])["status"], "unbounded")

    def test_beale_cycling_example_terminates(self):
        # Beale (1955): cycles with the textbook rule; Bland's rule must terminate. max 3/4x1 −20x2 +1/2x3 −6x4
        r = sx.linprog([F(3, 4), -20, F(1, 2), -6],
                       [([F(1, 4), -8, -1, 9], "<=", 0), ([F(1, 2), -12, F(-1, 2), 3], "<=", 0), ([0, 0, 1, 0], "<=", 1)])
        self.assertEqual(r["status"], "optimal")
        self.assertEqual(r["value"], F(5, 4))

    def test_bad_input(self):
        for obj, cons in (([], []), ([1], [([1, 2], "<=", 1)]), ([1], [([1], "<", 1)]), ([True], []),
                          ([1], [([float("nan")], "<=", 1)]), ([1], [("x", "<=", 1)]), ([1] * 41, []),
                          ([1], [([1], "<=", 1)] * 41), ([10 ** 40], []), (["1/0"], []), ("abc", [])):
            with self.subTest(obj=str(obj)[:20]):
                with self.assertRaises(sx.LPError):
                    sx.linprog(obj, cons)

    def test_dict_constraints_and_speed(self):
        r = sx.linprog([1, 1], [{"coeffs": [1, 2], "op": "<=", "rhs": 4}, {"coeffs": [3, 1], "op": "≤", "rhs": 6}])
        self.assertEqual(r["value"], F(14, 5))
        t = time.monotonic()
        n = 30
        sx.linprog([1] * n, [([((i * 7 + j * 3) % 11) + 1 for j in range(n)], "<=", 100 + i) for i in range(n)])
        self.assertLess(time.monotonic() - t, 10)


class Nash(unittest.TestCase):
    def test_prisoners_dilemma(self):
        r = gt.nash([[-1, -3], [0, -2]], [[-1, 0], [-3, -2]])
        self.assertEqual(r["pure"], [(1, 1)])
        self.assertEqual(len(r["equilibria"]), 1)
        self.assertEqual(r["equilibria"][0]["row"], [0, 1])

    def test_matching_pennies(self):
        r = gt.nash([[1, -1], [-1, 1]])
        self.assertEqual(r["pure"], [])
        e = r["equilibria"][0]
        self.assertEqual((e["row"], e["col"], e["row_payoff"]), ([F(1, 2)] * 2, [F(1, 2)] * 2, 0))

    def test_battle_of_the_sexes(self):
        r = gt.nash([[2, 0], [0, 1]], [[1, 0], [0, 2]])
        self.assertEqual(sorted(r["pure"]), [(0, 0), (1, 1)])
        mixed = [e for e in r["equilibria"] if not e["pure"]]
        self.assertEqual(len(mixed), 1)
        self.assertEqual(mixed[0]["row"], [F(2, 3), F(1, 3)])
        self.assertEqual(mixed[0]["col"], [F(1, 3), F(2, 3)])
        self.assertEqual(mixed[0]["row_payoff"], F(2, 3))

    def test_rock_paper_scissors(self):
        r = gt.nash([[0, -1, 1], [1, 0, -1], [-1, 1, 0]])
        self.assertEqual(len(r["equilibria"]), 1)
        self.assertEqual(r["equilibria"][0]["row"], [F(1, 3)] * 3)

    def test_stag_hunt_three_equilibria(self):
        r = gt.nash([[4, 0], [3, 3]], [[4, 3], [0, 3]])
        self.assertEqual(len(r["equilibria"]), 3)

    def test_bad_games(self):
        for a, b in (([], None), ([[1, 2], [3]], None), ([[1] * 9], None), ([[1, 2]], [[1, 2], [3, 4]]), ("x", None),
                     ([[True]], None), ([[1, None]], None)):
            with self.subTest(a=str(a)[:20]):
                with self.assertRaises(gt.GameError):
                    gt.nash(a, b)

    def test_size_cap_is_fast(self):
        t = time.monotonic()
        gt.nash([[(i * j) % 5 for j in range(8)] for i in range(8)], [[(i + 2 * j) % 7 for j in range(8)] for i in range(8)])
        self.assertLess(time.monotonic() - t, 30)


class ZeroSum(unittest.TestCase):
    def test_value_and_strategies(self):
        r = gt.zero_sum([[3, -1], [-2, 4]])
        self.assertEqual(r["value"], 1)
        self.assertEqual(r["row"], [F(3, 5), F(2, 5)])
        self.assertEqual(r["col"], [F(1, 2), F(1, 2)])

    def test_saddle_point(self):
        r = gt.zero_sum([[4, 2, 3], [1, 0, -1], [5, 1, 2]])          # saddle at (row 0, col 1): value 2
        self.assertEqual(r["value"], 2)
        self.assertEqual(r["row"][0], 1)

    def test_rps(self):
        r = gt.zero_sum([[0, -1, 1], [1, 0, -1], [-1, 1, 0]])
        self.assertEqual(r["value"], 0)


class Decisions(unittest.TestCase):
    def test_expected_value(self):
        r = gt.expected_value([[0.5, 100], ["rest", -20]])
        self.assertEqual(r["expected_value"], 40)
        self.assertEqual(r["variance"], 3600)
        r = gt.expected_value([{"p": "1/3", "value": 3}, {"p": "2/3", "value": 0}])
        self.assertEqual(r["expected_value"], 1)
        for bad in ([[0.5, 1]], [[1.5, 1], [-0.5, 2]], [], [[0.5, 1], ["rest", 2], ["rest", 3]], [["x", 1]], "no"):
            with self.subTest(bad=str(bad)[:30]):
                with self.assertRaises(gt.GameError):
                    gt.expected_value(bad)

    def test_decision_tree(self):
        tree = {"decision": "Fix the roof?", "options": [
            {"label": "fix now", "cost": 300, "node": 0},
            {"label": "wait", "node": {"chance": "storm", "branches": [
                {"label": "storm", "p": 0.2, "node": -2000}, {"label": "no storm", "p": 0.8, "node": 0}]}}]}
        r = gt.decision_tree(tree)
        self.assertEqual(r["expected_value"], -300)
        self.assertEqual(r["policy"][0]["choose"], "fix now")

    def test_tree_limits(self):
        node = 1
        for _ in range(15):
            node = {"options": [{"label": "x", "node": node}]}
        with self.assertRaises(gt.GameError):
            gt.decision_tree(node)
        wide = {"options": [{"label": str(i), "node": {"options": [{"label": "a", "node": j} for j in range(20)]}} for i in range(20)]}
        with self.assertRaises(gt.GameError):
            gt.decision_tree(wide)
        with self.assertRaises(gt.GameError):
            gt.decision_tree({"branches": [{"p": 0.3, "node": 1}]})
        with self.assertRaises(gt.GameError):
            gt.decision_tree({"what": 1})


if __name__ == "__main__":
    unittest.main()
