"""Solver tests. Pure stdlib so they run anywhere, including Python 3.9.

The assignment tests are brute-force cross-checked rather than golden-valued:
a hand-written expected answer only proves the solver agrees with whoever wrote
the test, while an exhaustive enumeration proves optimality.
"""

from __future__ import annotations

import datetime
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
        "rootfs",
        "app",
    ),
)

from solve.assign import (  # noqa: E402
    assignment_value,
    greedy_assignment,
    max_weight_assignment,
)
from espn import ids  # noqa: E402
from models import LeagueSettings, Projection, StatLimit  # noqa: E402
from solve.budget import (  # noqa: E402
    GamesBudget,
    PooledCapUnsupported,
    matchup_cap,
    reduced_cost_weights,
    remaining_budget,
    shadow_price,
    utilization_ratio,
)
from solve.value import (  # noqa: E402
    PointsValue,
    UnsupportedScoring,
    build_value_model,
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


def settings(**kw):
    """A ``LeagueSettings`` with everything the solver does not read defaulted.

    League ids here are deliberately fake. The repo is public and a real league
    id in a test is a standing invitation to poke at someone's league.
    """
    base = dict(
        league_id=111111111,
        season_id=2027,
        name="test league",
        scoring_type="H2H_POINTS",
        lineup_slot_counts={0: 1, 11: 3, 12: 3},
        lineup_slot_stat_limits=None,
        scoring_items={ids.PTS: 1.0, ids.STL: 2.0, ids.TO: -1.0},
        categories=frozenset(),
        waiver_mode="WAIVERS_TRADITIONAL",
        faab_budget=None,
        roster_lock_mode="INDIVIDUAL_GAME",
        current_scoring_period=1,
        current_matchup_period=1,
        matchup_period_boundaries={},
    )
    base.update(kw)
    return LeagueSettings(**base)


def gp_cap(per_matchup):
    """A pooled GP cap, expressed the way ESPN expresses it.

    Divided by seven on the way in, because that is how it arrives on the wire
    and a test that hands the code a round number would not catch the
    multiplication going missing.
    """
    return {
        ids.TEAM_TOTAL_LIMIT_SLOT: StatLimit(
            slot_id=ids.TEAM_TOTAL_LIMIT_SLOT,
            stat_id=ids.GP_STAT,
            limit_value=per_matchup / ids.SCORING_PERIODS_PER_MATCHUP,
        )
    }


class TestMatchupCap(unittest.TestCase):
    def test_no_cap_is_not_a_zero_cap(self):
        self.assertIsNone(matchup_cap(settings()))
        self.assertIsNone(matchup_cap(settings(lineup_slot_stat_limits={})))
        self.assertIsNone(remaining_budget(settings(), 0.0))
        self.assertEqual(shadow_price([(10.0, 1.0)], None), 0.0)

    def test_the_wire_fraction_becomes_a_round_cap(self):
        """40/7 per scoring period is 40 per matchup period, not 5.71."""
        both_observed = {10: 40.0, 11: 44.0}
        for starters, expected in both_observed.items():
            cap = matchup_cap(settings(lineup_slot_stat_limits=gp_cap(expected)))
            self.assertAlmostEqual(cap, expected, msg="%d starters" % starters)

    def test_a_per_slot_cap_is_refused_not_guessed(self):
        """An unobserved shape must fail loudly; see PooledCapUnsupported."""
        per_slot = {
            0: StatLimit(slot_id=0, stat_id=ids.GP_STAT, limit_value=4.0)
        }
        with self.assertRaises(PooledCapUnsupported):
            matchup_cap(settings(lineup_slot_stat_limits=per_slot))

    def test_a_cap_on_another_stat_is_refused(self):
        other = {
            ids.TEAM_TOTAL_LIMIT_SLOT: StatLimit(
                slot_id=ids.TEAM_TOTAL_LIMIT_SLOT,
                stat_id=ids.MINUTES,
                limit_value=100.0,
            )
        }
        with self.assertRaises(PooledCapUnsupported):
            matchup_cap(settings(lineup_slot_stat_limits=other))

    def test_overrun_is_reported_not_silently_clamped(self):
        """The self-falsifying check on the per-matchup reading."""
        st = settings(lineup_slot_stat_limits=gp_cap(40.0))
        with self.assertLogs("solve.budget", level="ERROR") as logs:
            budget = remaining_budget(st, 95.0)
        self.assertIn("95.0", "".join(logs.output))
        self.assertTrue(budget.exhausted)
        # Exhausted, but the real numbers survive into the journal rather than
        # being clamped to the cap -- the overrun is the evidence.
        self.assertAlmostEqual(budget.used, 95.0)


class TestBudget(unittest.TestCase):
    def test_lambda_is_the_marginal_opportunity(self):
        """Budget 2 out of 5 unit-cost candidates -> cutoff is the 2nd best."""
        cands = [(1.0, 1.0), (9.0, 1.0), (5.0, 1.0), (7.0, 1.0), (3.0, 1.0)]
        self.assertAlmostEqual(
            shadow_price(cands, GamesBudget(cap=2.0, used=0.0)), 7.0
        )

    def test_slack_budget_prices_at_zero(self):
        cands = [(9.0, 1.0), (7.0, 1.0)]
        self.assertAlmostEqual(
            shadow_price(cands, GamesBudget(cap=5.0, used=0.0)), 0.0
        )

    def test_exhausted_budget_is_infinite(self):
        self.assertEqual(
            shadow_price([(9.0, 1.0)], GamesBudget(cap=5.0, used=5.0)),
            float("inf"),
        )

    def test_fractional_costs_stretch_the_budget(self):
        """Half-likely players consume half a game, so more of them fit.

        Same five candidates as the unit-cost case with a budget of 2. At cost
        1.0 the budget reaches the 2nd best; at 0.5 it reaches the 4th.
        """
        cands = [(1.0, 0.5), (9.0, 0.5), (5.0, 0.5), (7.0, 0.5), (3.0, 0.5)]
        self.assertAlmostEqual(
            shadow_price(cands, GamesBudget(cap=2.0, used=0.0)), 3.0
        )

    def test_exhausted_budget_empties_the_lineup(self):
        w = reduced_cost_weights({1: 50.0}, {1: [4, 5]}, float("inf"))
        self.assertEqual(w, {})
        self.assertEqual(max_weight_assignment(w, {4: 1, 5: 1}), {})

    def test_reduced_cost_decision_rule(self):
        """start iff v > lambda -- below the cutoff, the slot sits empty."""
        w = reduced_cost_weights({1: 8.0}, {1: [5]}, 12.0)
        self.assertEqual(w, {})
        self.assertEqual(max_weight_assignment(w, {5: 1}), {})

    def test_availability_scales_the_value_but_not_the_threshold(self):
        """The cancellation argument in budget.py, as an executable claim.

        Two players with the same *expected* line: one certain to play with a
        conditional value of 10, one 50/50 with a conditional value of 20. A
        threshold of 15 must keep the coin-flip player and drop the certain
        one, because under a binding cap the risky player only spends a game
        in the branch where he produces.
        """
        values = {1: 10.0, 2: 20.0}
        costs = {1: 1.0, 2: 0.5}
        w = reduced_cost_weights(values, {1: [11], 2: [11]}, 15.0, costs)
        self.assertEqual(sorted(p for p, _ in w), [2])
        # ...and the surviving pair is scaled by p_play, not raw.
        self.assertAlmostEqual(w[(2, 11)], 0.5 * (20.0 - 15.0))

    def test_keep_nonpositive_cannot_change_optimum(self):
        """Pruning non-positive reduced costs is an optimization, not a policy."""
        vals = {1: 8.0, 2: 20.0}
        elig = {1: [5, 11], 2: [5, 11]}
        cap = {5: 1, 11: 1}
        lam = 12.0
        pruned = max_weight_assignment(reduced_cost_weights(vals, elig, lam), cap)
        full_w = reduced_cost_weights(vals, elig, lam, keep_nonpositive=True)
        full = max_weight_assignment(full_w, cap)
        self.assertAlmostEqual(
            assignment_value(pruned, full_w), assignment_value(full, full_w)
        )

    def test_utilization_ratio(self):
        cands = [(1.0, 1.0)] * 10
        rho = utilization_ratio(cands, GamesBudget(cap=4.0, used=0.0))
        self.assertAlmostEqual(rho, 0.4)
        self.assertEqual(utilization_ratio(cands, None), float("inf"))

    def test_observed_leagues_are_actually_constrained(self):
        """Both real leagues bind. If they did not, none of this would matter.

        A 24-player roster averaging 3.5 games a week has ~84 startable
        player-games against a cap of 44, so the optimizer has to bench
        playable players most nights.
        """
        deep = [(5.0, 1.0)] * 84
        rho = utilization_ratio(deep, GamesBudget(cap=44.0, used=0.0))
        self.assertLess(rho, 1.0)
        shallow = [(5.0, 1.0)] * 46
        self.assertLess(
            utilization_ratio(shallow, GamesBudget(cap=40.0, used=0.0)), 1.0
        )


class TestPointsValue(unittest.TestCase):
    """The leagues this app runs are H2H_POINTS. See docs/ADR.md ADR-010."""

    def _projection(self, mean, var=None, p_play=1.0, applied=False):
        return Projection(
            player_id=1,
            mean=mean,
            var=var or {},
            p_play=p_play,
            minutes=30.0,
            source="test",
            as_of=datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc),
            p_play_applied=applied,
        )

    def test_value_is_the_dot_product_with_scoring_items(self):
        model = build_value_model(settings())
        self.assertIsInstance(model, PointsValue)
        # 20 pts @1.0 + 3 stl @2.0 + 4 to @-1.0 = 20 + 6 - 4
        v = model.conditional_value(
            self._projection({ids.PTS: 20.0, ids.STL: 3.0, ids.TO: 4.0})
        )
        self.assertAlmostEqual(v, 22.0)

    def test_unscored_stats_contribute_nothing(self):
        """Rebounds are worth zero in a league that does not score them."""
        model = build_value_model(settings())
        v = model.conditional_value(
            self._projection({ids.PTS: 10.0, ids.REB: 99.0})
        )
        self.assertAlmostEqual(v, 10.0)

    def test_availability_is_divided_back_out(self):
        """A pre-discounted projection must not be discounted twice.

        The same player, expressed both ways, has to value identically --
        otherwise the threshold in budget.py means something different
        depending on which projection source answered.
        """
        model = build_value_model(settings())
        raw = model.conditional_value(
            self._projection({ids.PTS: 20.0}, p_play=0.5)
        )
        discounted = model.conditional_value(
            self._projection({ids.PTS: 10.0}, p_play=0.5, applied=True)
        )
        self.assertAlmostEqual(raw, discounted)
        self.assertAlmostEqual(raw, 20.0)

    def test_non_additive_scoring_is_refused(self):
        """A league scoring FG% cannot be valued by summing. Fail loudly."""
        with self.assertRaises(UnsupportedScoring) as cm:
            build_value_model(settings(scoring_items={ids.PTS: 1.0, ids.FG_PCT: 5.0}))
        self.assertIn("FG%", str(cm.exception))

    def test_unknown_scoring_type_is_refused(self):
        with self.assertRaises(UnsupportedScoring):
            build_value_model(settings(scoring_type="ROTO"))

    def test_categories_league_needs_states(self):
        """The category weights depend on the margin and cannot be defaulted."""
        cats = settings(scoring_type="H2H_CATEGORY", categories=frozenset({ids.PTS}))
        with self.assertRaises(UnsupportedScoring):
            build_value_model(cats)

    def test_risk_is_off_until_both_totals_are_known(self):
        """No margin, no variance term -- not a zero-margin guess."""
        model = build_value_model(settings(), my_points=400.0)
        self.assertIsNone(model.state)
        v = model.conditional_value(
            self._projection({ids.PTS: 10.0}, var={ids.PTS: 25.0})
        )
        self.assertAlmostEqual(v, 10.0)

    def test_trailing_seeks_variance_and_leading_avoids_it(self):
        """The one sign that matters in the risk term.

        Down 30 with the same projection, the high-variance player must be
        worth *more* than his mean; up 30, less. Any other sign convention
        benches your upside exactly when you need it.
        """
        proj = self._projection({ids.PTS: 10.0}, var={ids.PTS: 100.0})
        behind = build_value_model(
            settings(), my_points=370.0, opp_points=400.0,
            my_points_var=400.0, opp_points_var=400.0,
        )
        ahead = build_value_model(
            settings(), my_points=430.0, opp_points=400.0,
            my_points_var=400.0, opp_points_var=400.0,
        )
        mean_only = 10.0
        self.assertGreater(behind.conditional_value(proj), mean_only)
        self.assertLess(ahead.conditional_value(proj), mean_only)

    def test_negative_coefficients_cannot_subtract_variance(self):
        """Point variance squares the coefficient. A -1.0 turnover adds risk.

        Getting this wrong is invisible in the mean and inverts the risk term,
        so a turnover-prone player would read as a variance *reducer*.
        """
        behind = build_value_model(
            settings(), my_points=370.0, opp_points=400.0,
            my_points_var=400.0, opp_points_var=400.0,
        )
        no_to = behind.conditional_value(
            self._projection({ids.PTS: 10.0}, var={ids.PTS: 100.0})
        )
        with_to = behind.conditional_value(
            self._projection(
                {ids.PTS: 10.0, ids.TO: 0.0}, var={ids.PTS: 100.0, ids.TO: 9.0}
            )
        )
        # Trailing, so more variance is better; the turnover variance must
        # push the value up, not down.
        self.assertGreater(with_to, no_to)


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
