"""Solver tests. Pure stdlib so they run anywhere, including Python 3.9.

The assignment tests are brute-force cross-checked rather than golden-valued:
a hand-written expected answer only proves the solver agrees with whoever wrote
the test, while an exhaustive enumeration proves optimality.
"""

from __future__ import annotations

import itertools
import math
import os
import random
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "nba_autopilot",
        "rootfs",
        "app",
    ),
)

from solve.assign import (  # noqa: E402
    assignment_value,
    greedy_assignment,
    max_weight_assignment,
)
from solve.budget import (  # noqa: E402
    reduced_cost_weights,
    remaining_budget,
    shadow_prices,
    utilization_ratio,
)
from solve.weights import (  # noqa: E402
    CategoryState,
    RatioSpec,
    category_weights,
    normal_pdf,
    player_value,
    ratio_contribution,
    risk_coefficients,
    win_probability,
)


def brute_force(weights, slot_capacity):
    """Exhaustive optimum. Exponential, so tests keep instances tiny."""
    players = sorted({p for (p, _s) in weights})
    slots = sorted({s for (_p, s) in weights})
    best_val = 0.0
    best = {}
    # Each player independently picks a slot he is eligible for, or sits.
    options = []
    for p in players:
        choices = [None] + [s for s in slots if (p, s) in weights]
        options.append(choices)
    for combo in itertools.product(*options):
        used = {}
        val = 0.0
        ok = True
        for p, s in zip(players, combo):
            if s is None:
                continue
            used[s] = used.get(s, 0) + 1
            if used[s] > slot_capacity.get(s, 0):
                ok = False
                break
            val += weights[(p, s)]
        if ok and val > best_val + 1e-12:
            best_val = val
            best = {p: s for p, s in zip(players, combo) if s is not None}
    return best_val, best


class TestAssignment(unittest.TestCase):
    def test_greedy_counterexample(self):
        """The bug that ships in public fantasy tools.

        Slots {G, C}. Alice is G/C-eligible worth 30, Bob is C-only worth 25.
        Greedy seats Alice first and strands either Bob or a slot.
        """
        G, C = 5, 4
        weights = {
            ("alice", G): 30.0,
            ("alice", C): 30.0,
            ("bob", C): 25.0,
        }
        cap = {G: 1, C: 1}

        exact = max_weight_assignment(weights, cap)
        self.assertAlmostEqual(assignment_value(exact, weights), 55.0)
        self.assertEqual(exact, {"alice": G, "bob": C})

        naive = greedy_assignment(weights, cap)
        self.assertAlmostEqual(assignment_value(naive, weights), 30.0)
        # The headline failure: greedy starts strictly fewer players.
        self.assertLess(len(naive), len(exact))

    def test_benching_is_free(self):
        """A negative-value player must sit, and his slot must stay empty."""
        weights = {("a", 1): -5.0, ("b", 1): -2.0}
        got = max_weight_assignment(weights, {1: 1})
        self.assertEqual(got, {})

    def test_multi_capacity_slot(self):
        """UTIL slots have capacity > 1; the expansion must honour it."""
        weights = {(p, 11): float(v) for p, v in [("a", 10), ("b", 9), ("c", 8)]}
        got = max_weight_assignment(weights, {11: 2})
        self.assertEqual(len(got), 2)
        self.assertEqual(set(got), {"a", "b"})

    def test_locked_players_pinned(self):
        """A player whose game started keeps his slot and consumes capacity."""
        weights = {("a", 5): 10.0, ("b", 5): 99.0}
        got = max_weight_assignment(weights, {5: 1}, locked={"a": 5})
        self.assertEqual(got, {"a": 5})

    def test_locked_infeasible_raises(self):
        """Disagreeing with ESPN about capacity must fail loud, not guess."""
        with self.assertRaises(ValueError):
            max_weight_assignment({("b", 5): 1.0}, {5: 1}, locked={"a": 5, "c": 5})

    def test_ineligible_absent_not_zero(self):
        """Absence means forbidden. A 0.0 weight is a legal, chosen pair."""
        forbidden = max_weight_assignment({("a", 1): 5.0}, {1: 1, 2: 1})
        self.assertEqual(forbidden, {"a": 1})
        explicit_zero = max_weight_assignment(
            {("a", 1): -1.0, ("a", 2): 0.0}, {1: 1, 2: 1}
        )
        # 0.0 ties with benching; either is optimal, but slot 1 must not be used.
        self.assertNotEqual(explicit_zero.get("a"), 1)

    def test_fuzz_against_brute_force(self):
        """200 random instances; exact solver must match exhaustive search."""
        rng = random.Random(20261020)
        for trial in range(200):
            n_players = rng.randint(1, 6)
            n_slots = rng.randint(1, 4)
            cap = {s: rng.randint(1, 2) for s in range(n_slots)}
            weights = {}
            for p in range(n_players):
                for s in range(n_slots):
                    if rng.random() < 0.6:
                        weights[(p, s)] = round(rng.uniform(-5, 20), 3)
            if not weights:
                continue
            want, _ = brute_force(weights, cap)
            got = max_weight_assignment(weights, cap)
            got_val = assignment_value(got, weights)
            self.assertAlmostEqual(
                got_val,
                want,
                places=6,
                msg="trial %d: got %.6f want %.6f\nweights=%r cap=%r"
                % (trial, got_val, want, weights, cap),
            )

    def test_fuzz_capacity_respected(self):
        rng = random.Random(7)
        for _ in range(100):
            cap = {s: rng.randint(1, 3) for s in range(4)}
            weights = {
                (p, s): rng.uniform(1, 10) for p in range(12) for s in range(4)
            }
            got = max_weight_assignment(weights, cap)
            counts = {}
            for s in got.values():
                counts[s] = counts.get(s, 0) + 1
            for s, c in counts.items():
                self.assertLessEqual(c, cap[s])

    def test_realistic_size_is_fast(self):
        """15 players x 13 slot-units, the actual production shape."""
        import time

        cap = {0: 1, 1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 11: 3, 12: 3}
        weights = {
            (p, s): float((p * 7 + s * 3) % 19)
            for p in range(15)
            for s in cap
            if (p + s) % 4 != 0
        }
        t0 = time.time()
        for _ in range(20):
            max_weight_assignment(weights, cap)
        elapsed = (time.time() - t0) / 20
        self.assertLess(elapsed, 0.05, "assignment too slow: %.4fs" % elapsed)


class TestBudget(unittest.TestCase):
    def test_no_cap_is_not_a_zero_cap(self):
        self.assertIsNone(remaining_budget(None, {}))
        self.assertIsNone(remaining_budget({}, {}))
        lam = shadow_prices({5: [10.0, 9.0, 8.0]}, None)
        self.assertEqual(lam, {5: 0.0})

    def test_lambda_is_the_marginal_opportunity(self):
        """Budget 2 out of 5 candidates -> cutoff is the 2nd best."""
        lam = shadow_prices({5: [1.0, 9.0, 5.0, 7.0, 3.0]}, {5: 2})
        self.assertAlmostEqual(lam[5], 7.0)

    def test_slack_budget_prices_at_zero(self):
        lam = shadow_prices({5: [9.0, 7.0]}, {5: 5})
        self.assertAlmostEqual(lam[5], 0.0)

    def test_exhausted_budget_is_infinite(self):
        lam = shadow_prices({5: [9.0]}, {5: 0})
        self.assertEqual(lam[5], float("inf"))

    def test_exhausted_slot_drops_out_of_weights(self):
        w = reduced_cost_weights(
            {1: 50.0}, {1: [4, 5]}, {4: float("inf"), 5: 10.0}
        )
        self.assertEqual(w, {(1, 5): 40.0})

    def test_reduced_cost_decision_rule(self):
        """start iff v_hat > lambda_s -- below the cutoff, the slot sits empty."""
        lam = {5: 12.0}
        w = reduced_cost_weights({1: 8.0}, {1: [5]}, lam)
        self.assertEqual(w, {})
        self.assertEqual(max_weight_assignment(w, {5: 1}), {})

    def test_keep_nonpositive_cannot_change_optimum(self):
        """Pruning non-positive reduced costs is an optimization, not a policy."""
        vals = {1: 8.0, 2: 20.0}
        elig = {1: [5, 11], 2: [5, 11]}
        lam = {5: 12.0, 11: 3.0}
        cap = {5: 1, 11: 1}
        pruned = max_weight_assignment(reduced_cost_weights(vals, elig, lam), cap)
        full_w = reduced_cost_weights(vals, elig, lam, keep_nonpositive=True)
        full = max_weight_assignment(full_w, cap)
        self.assertAlmostEqual(
            assignment_value(pruned, full_w), assignment_value(full, full_w)
        )

    def test_utilization_ratio(self):
        rho = utilization_ratio({5: [1.0] * 10}, {5: 4})
        self.assertAlmostEqual(rho[5], 0.4)
        self.assertEqual(utilization_ratio({5: [1.0]}, None)[5], float("inf"))


class TestWeights(unittest.TestCase):
    def _state(self, z, stat_id=0, **kw):
        """A category whose margin SD is 1, so margin == z."""
        return CategoryState(
            stat_id=stat_id,
            my_mean=z,
            opp_mean=0.0,
            my_var=0.5,
            opp_var=0.5,
            **kw
        )

    def test_gradient_table(self):
        """The numbers that justify the whole design. s_k = 1."""
        self.assertAlmostEqual(normal_pdf(0.0), 0.3989422804, places=9)
        w0 = category_weights([self._state(0.0)])[0]
        w3 = category_weights([self._state(3.0)])[0]
        wm3 = category_weights([self._state(-3.0)])[0]
        self.assertAlmostEqual(w0, 0.3989422804, places=9)
        self.assertAlmostEqual(w3, 0.0044318484, places=9)
        self.assertAlmostEqual(wm3, 0.0044318484, places=9)
        # A tossup is worth ~90x a decided category, in either direction.
        self.assertAlmostEqual(w0 / w3, 90.0171313, places=4)
        self.assertAlmostEqual(w3, wm3, places=12)

    def test_punting_is_emergent(self):
        """A hopeless category gets ~zero weight with no punt flag anywhere."""
        contested = self._state(0.0, stat_id=1)
        hopeless = CategoryState(
            stat_id=2, my_mean=0.0, opp_mean=40.0, my_var=0.5, opp_var=0.5
        )
        w = category_weights([contested, hopeless])
        self.assertGreater(w[1], 1e6 * w[2])

    def test_clinched_also_gets_zero_weight(self):
        """Symmetry: a won category is as worthless at the margin as a lost one."""
        won = CategoryState(
            stat_id=2, my_mean=40.0, opp_mean=0.0, my_var=0.5, opp_var=0.5
        )
        lost = CategoryState(
            stat_id=3, my_mean=0.0, opp_mean=40.0, my_var=0.5, opp_var=0.5
        )
        w = category_weights([won, lost])
        self.assertAlmostEqual(w[2], w[3], places=15)

    def test_lower_is_better_flips_sign(self):
        """Turnovers: more is worse, so the weight is negative."""
        to = CategoryState(
            stat_id=11,
            my_mean=10.0,
            opp_mean=10.0,
            my_var=0.5,
            opp_var=0.5,
            lower_is_better=True,
        )
        self.assertLess(category_weights([to])[11], 0.0)
        # And a turnover-prone player is penalised with no caller special-case.
        v = player_value({11: 4.0}, {11: 1.0}, [to], include_risk=False)
        self.assertLess(v, 0.0)

    def test_risk_sign_follows_deficit(self):
        """Trailing -> seek variance. Leading -> avoid it."""
        behind = CategoryState(
            stat_id=1, my_mean=0.0, opp_mean=5.0, my_var=1.0, opp_var=1.0
        )
        ahead = CategoryState(
            stat_id=1, my_mean=5.0, opp_mean=0.0, my_var=1.0, opp_var=1.0
        )
        self.assertGreater(risk_coefficients([behind])[1], 0.0)
        self.assertLess(risk_coefficients([ahead])[1], 0.0)

    def test_risk_changes_the_pick(self):
        """Down in blocks, the high-variance big beats the steadier mean."""
        behind = CategoryState(
            stat_id=1, my_mean=0.0, opp_mean=4.0, my_var=1.0, opp_var=1.0
        )
        steady = player_value({1: 1.1}, {1: 0.2}, [behind])
        swingy = player_value({1: 1.0}, {1: 6.0}, [behind])
        self.assertGreater(swingy, steady)
        # Without the risk term the ordering reverses -- i.e. the term is
        # actually doing the work, not decorating it.
        self.assertGreater(
            player_value({1: 1.1}, {1: 0.2}, [behind], include_risk=False),
            player_value({1: 1.0}, {1: 6.0}, [behind], include_risk=False),
        )

    def test_ratio_beats_team_rate(self):
        """FG%: helps iff his rate beats the team's, scaled by volume."""
        spec = RatioSpec(made_stat=13, att_stat=14, team_made=450.0, team_att=1000.0)
        hot = ratio_contribution(spec, 1.0, player_made=6.0, player_att=10.0)
        cold = ratio_contribution(spec, 1.0, player_made=3.0, player_att=10.0)
        at_rate = ratio_contribution(spec, 1.0, player_made=4.5, player_att=10.0)
        self.assertGreater(hot, 0.0)
        self.assertLess(cold, 0.0)
        self.assertAlmostEqual(at_rate, 0.0, places=12)
        # Volume scaling: same rate, twice the attempts, twice the impact.
        self.assertAlmostEqual(
            ratio_contribution(spec, 1.0, 12.0, 20.0), 2.0 * hot, places=12
        )

    def test_zero_attempts_is_zero_not_nan(self):
        spec = RatioSpec(made_stat=13, att_stat=14, team_made=450.0, team_att=1000.0)
        v = ratio_contribution(spec, 1.0, player_made=0.0, player_att=0.0)
        self.assertEqual(v, 0.0)
        self.assertFalse(math.isnan(v))

    def test_win_probability_is_the_true_objective(self):
        """J must be bounded by the category count and monotone in the margin."""
        cats = [self._state(0.0, stat_id=i) for i in range(9)]
        self.assertAlmostEqual(win_probability(cats), 4.5, places=9)
        better = [self._state(2.0, stat_id=i) for i in range(9)]
        self.assertGreater(win_probability(better), win_probability(cats))
        self.assertLess(win_probability(better), 9.0)

    def test_degenerate_variance_does_not_explode(self):
        locked = CategoryState(
            stat_id=0, my_mean=5.0, opp_mean=0.0, my_var=0.0, opp_var=0.0
        )
        w = category_weights([locked])[0]
        self.assertTrue(math.isfinite(w))
        self.assertAlmostEqual(w, 0.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
