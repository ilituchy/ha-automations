"""The fast lineup value must equal the exact matcher, and both must equal
brute force. Cross-checked, not golden-valued."""

from __future__ import annotations

import itertools
import random
import unittest

import _fixtures  # noqa: F401  (sets sys.path)
from advisor.league import SLOT_ELIGIBILITY
from advisor.lineup import best_lineup, fast_lineup_value

SLOTS = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "SUPER_FLEX", "K", "DEF")


def brute(players, pos, val, slots):
    best = 0.0
    for perm in itertools.permutations(players + [None] * len(slots), len(slots)):
        if any(p is not None and not (set(pos[p]) & SLOT_ELIGIBILITY[s]) for p, s in zip(perm, slots)):
            continue
        used = [p for p in perm if p is not None]
        if len(used) != len(set(used)):
            continue
        best = max(best, sum(val[p] for p in used))
    return best


class TestLineup(unittest.TestCase):
    def test_fast_equals_exact_on_random_rosters(self):
        rng = random.Random(7)
        for trial in range(300):
            n = rng.randint(5, 26)
            pos = {str(i): (rng.choice(["QB", "RB", "RB", "WR", "WR", "WR", "TE", "K", "DEF"]),) for i in range(n)}
            val = {p: rng.choice([0.0, rng.uniform(0, 9000)]) for p in pos}
            exact, assign = best_lineup(pos, lambda p: pos[p], lambda p: val[p], SLOTS)
            fast = fast_lineup_value(pos, lambda p: pos[p], lambda p: val[p], SLOTS)
            self.assertAlmostEqual(exact, fast, places=6, msg="trial %d" % trial)
            for p, slot in assign.items():
                self.assertTrue(set(pos[p]) & SLOT_ELIGIBILITY[slot])

    def test_matches_brute_force_small(self):
        rng = random.Random(3)
        slots = ("QB", "RB", "WR", "FLEX", "SUPER_FLEX")
        for _ in range(60):
            n = rng.randint(2, 6)
            pos = {str(i): (rng.choice(["QB", "RB", "WR", "TE"]),) for i in range(n)}
            val = {p: round(rng.uniform(0, 100), 2) for p in pos}
            want = brute(list(pos), pos, val, slots)
            self.assertAlmostEqual(fast_lineup_value(pos, lambda p: pos[p], lambda p: val[p], slots), want, places=6)
            self.assertAlmostEqual(best_lineup(pos, lambda p: pos[p], lambda p: val[p], slots)[0], want, places=6)

    def test_greedy_counterexample(self):
        # Greedy-by-value into "best open slot" fails here; matroid greedy must not.
        pos = {"a": ("RB",), "b": ("QB",), "c": ("RB",)}
        val = {"a": 50.0, "b": 40.0, "c": 30.0}
        slots = ("SUPER_FLEX", "RB")
        # a must take RB so b can take SUPER_FLEX: 90, not a@SF + c@RB = 80.
        self.assertEqual(fast_lineup_value(pos, lambda p: pos[p], lambda p: val[p], slots), 90.0)

    def test_kicker_never_at_qb(self):
        pos = {"k": ("K",), "q": ("QB",)}
        val = {"k": 500.0, "q": 0.0}
        total, assign = best_lineup(pos, lambda p: pos[p], lambda p: val[p], ("QB",))
        self.assertNotIn("k", assign)


if __name__ == "__main__":
    unittest.main()
