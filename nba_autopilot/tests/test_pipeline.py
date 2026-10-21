"""Calendar, games-used accounting, and the lineup pipeline.

The emphasis here is on the three facts that were *probed* rather than assumed,
because those are the ones that can quietly stop being true:

* scoring periods are daily and linear,
* matchup periods are Monday-Sunday weeks,
* neither of ESPN's games-played readings can be trusted on its own.

Every number used as a fixture below comes from the live 2026-27 season
(opener Tuesday 2026-10-20), so a test failing here is evidence about ESPN, not
just about this code.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

import pipeline  # noqa: E402
import usage as usage_mod  # noqa: E402
from espn import ids  # noqa: E402
from models import LeagueSettings, Projection, RosterEntry, StatLimit  # noqa: E402
from solve.calendar import (  # noqa: E402
    SeasonCalendar,
    game_days_for,
    team_games_in,
)

#: The real opener. A Tuesday, which is the whole reason the six-day first week
#: exists and why the cap must not be rescaled by days-in-week.
OPENER = date(2026, 10, 20)


def calendar_like_2027(days: int = 60, skip=()):  # -> SeasonCalendar
    """A daily calendar anchored on the real opener, with optional dark days."""
    dates = {
        1 + i: OPENER + timedelta(days=i)
        for i in range(days)
        if (1 + i) not in skip
    }
    return SeasonCalendar.from_period_dates(dates)


def settings(**kw) -> LeagueSettings:
    """League ids are fake on purpose; this repo is public."""
    base = dict(
        league_id=111111111,
        season_id=2027,
        name="test league",
        scoring_type="H2H_POINTS",
        # Shaped like the real 10-starter league: 10 starting slots, 3 bench,
        # 3 IR. The exact-capacity property that makes benching impossible on a
        # light slate only shows up at realistic proportions.
        lineup_slot_counts={11: 10, 12: 3, 13: 3},
        lineup_slot_stat_limits=None,
        scoring_items={ids.PTS: 1.0},
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


def gp_cap(per_matchup: float):
    return {
        ids.TEAM_TOTAL_LIMIT_SLOT: StatLimit(
            slot_id=ids.TEAM_TOTAL_LIMIT_SLOT,
            stat_id=ids.GP_STAT,
            limit_value=per_matchup / ids.SCORING_PERIODS_PER_MATCHUP,
        )
    }


def player(pid: int, *, slot=11, team=1, slots=(11, 12), locked=False,
           status="ACTIVE", ir=False) -> RosterEntry:
    return RosterEntry(
        player_id=pid,
        name="P%d" % pid,
        current_slot=slot,
        eligible_slots=frozenset(slots),
        pro_team_id=team,
        injury_status=status,
        lineup_locked=locked,
        ir_eligible=ir,
    )


def projection(pid: int, points: float, p_play: float = 1.0) -> Projection:
    return Projection(
        player_id=pid,
        mean={ids.PTS: points},
        var={ids.PTS: points},
        p_play=p_play,
        minutes=30.0,
        source="test",
        as_of=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )


class TestSeasonCalendar(unittest.TestCase):
    def test_matchup_periods_are_monday_sunday_weeks(self):
        """The probed week boundaries, reproduced exactly.

        Week 1 is six days because the season opens on a Tuesday. Every later
        week is seven. These numbers came off the live league alongside ESPN's
        own ``currentMatchupPeriod``, which the model matched.
        """
        cal = calendar_like_2027(days=45)
        self.assertEqual(cal.periods_in_matchup(1), list(range(1, 7)))
        self.assertEqual(cal.periods_in_matchup(2), list(range(7, 14)))
        self.assertEqual(cal.periods_in_matchup(3), list(range(14, 21)))
        self.assertEqual(cal.periods_in_matchup(4), list(range(21, 28)))
        for period in range(1, 7):
            self.assertEqual(cal.matchup_period_of(period), 1)
        self.assertEqual(cal.matchup_period_of(7), 2)
        self.assertEqual(cal.matchup_period_of(13), 2)
        self.assertEqual(cal.matchup_period_of(14), 3)

    def test_week_one_is_not_rescaled(self):
        """A six-day week must not shrink the budget.

        The cap is per matchup period, and ESPN's fractional ``limitValue``
        divides by a fixed 7 regardless. Recomputing from "days in this week"
        would hand week 1 a cap of 34 instead of 40.
        """
        cal = calendar_like_2027()
        self.assertEqual(len(cal.periods_in_matchup(1)), 6)
        self.assertEqual(
            40.0, ids.SCORING_PERIODS_PER_MATCHUP * (40 / ids.SCORING_PERIODS_PER_MATCHUP)
        )

    def test_gameless_days_stay_in_the_window(self):
        """A dark Monday is still a day of the week the cap is spent over."""
        cal = calendar_like_2027(days=45, skip=(9, 10))
        self.assertIn(9, cal.periods_in_matchup(2))
        self.assertNotIn(9, cal.game_periods_in_matchup(2))
        self.assertEqual(cal.date_of(9), OPENER + timedelta(days=8))

    def test_dates_are_eastern_derived_one_per_period(self):
        cal = calendar_like_2027()
        self.assertEqual(cal.date_of(1), OPENER)
        self.assertEqual(cal.period_of(OPENER), 1)
        self.assertEqual(cal.date_of(cal.period_of(date(2026, 11, 3))),
                         date(2026, 11, 3))

    def test_remaining_periods_includes_today(self):
        """Excluding today would price today's starts as free."""
        cal = calendar_like_2027()
        self.assertEqual(cal.remaining_periods(1, 4), [4, 5, 6])
        self.assertEqual(cal.remaining_periods(1, 4, inclusive=False), [5, 6])

    def test_nonlinear_input_is_reported_not_repaired(self):
        bad = {1: OPENER, 2: OPENER + timedelta(days=1),
               3: OPENER + timedelta(days=5)}
        cal = SeasonCalendar.from_period_dates(bad)
        self.assertFalse(cal.trustworthy)
        self.assertIsNotNone(cal.verify(1, 1))

    def test_verify_is_the_falsification_check(self):
        cal = calendar_like_2027()
        self.assertIsNone(cal.verify(7, 2))
        self.assertIsNotNone(cal.verify(7, 1))

    def test_verify_ignores_out_of_season_periods(self):
        """ESPN reports a scoring period in the offseason too; that is not
        evidence about week boundaries."""
        cal = calendar_like_2027(days=10)
        self.assertIsNone(cal.verify(900, 1))

    def test_empty_is_none_not_an_empty_calendar(self):
        self.assertIsNone(SeasonCalendar.from_period_dates({}))

    def test_team_games_in_window(self):
        sched = {1: {1: 1, 3: 1, 5: 1}, 2: {}}
        self.assertEqual(team_games_in([1, 2, 3], sched, 1), 2)
        self.assertEqual(team_games_in([1, 2, 3], sched, 2), 0)
        # proTeamId 0 is ESPN's free-agent sentinel and appears on real rosters.
        self.assertEqual(team_games_in([1], sched, 0), 0)
        self.assertEqual(game_days_for([1, 2, 3, 5], sched, 1), [1, 3, 5])


class _FakeReader:
    def __init__(self, by_slot, season_gp):
        self._by_slot = by_slot
        self._season_gp = season_gp

    def games_used(self):
        return self._by_slot

    def season_games_played(self):
        return self._season_gp


class _FakeSnapshots:
    def __init__(self):
        self.rows = {}

    def record_gp_snapshot(self, **kw):
        key = (kw["league_id"], kw["team_id"], kw["matchup_period"])
        self.rows.setdefault(key, dict(kw))

    def gp_snapshot(self, league_id, team_id, matchup_period):
        return self.rows.get((league_id, team_id, matchup_period))


class TestGamesUsed(unittest.TestCase):
    def measure(self, reader, store, period=1, matchup=1, cal=None):
        return usage_mod.measure(
            reader, store, league_id=1, team_id=2,
            calendar=cal or calendar_like_2027(),
            scoring_period=period, matchup_period=matchup,
        )

    def test_bench_and_ir_do_not_count_against_the_cap(self):
        by_slot = {11: 6, ids.BENCH_SLOT: 4, ids.IR_SLOT: 2}
        self.assertEqual(usage_mod.starts_from_stat_by_slot(by_slot), 6)

    def test_empty_stat_by_slot_is_unknown_not_zero(self):
        """The live failure mode: ESPN sent nothing, and nothing is not zero."""
        self.assertIsNone(usage_mod.starts_from_stat_by_slot({}))

    def test_season_delta_needs_a_first_day_baseline(self):
        store = _FakeSnapshots()
        # First run of the week, on the week's first day: baseline captured.
        got = self.measure(_FakeReader({}, 10), store, period=1, matchup=1)
        self.assertEqual(got.games, 0.0)
        self.assertEqual(got.source, usage_mod.SOURCE_SEASON_DELTA)
        # Later in the same week, four more games played.
        got = self.measure(_FakeReader({}, 14), store, period=4, matchup=1)
        self.assertEqual(got.games, 4.0)
        self.assertTrue(got.trustworthy)

    def test_midweek_baseline_is_refused_not_used(self):
        """Subtracting a Thursday reading from a Sunday one under-counts, and
        under-counting a hard cap is how you lose a lineup."""
        store = _FakeSnapshots()
        got = self.measure(_FakeReader({}, 30), store, period=4, matchup=1)
        self.assertFalse(got.trustworthy)
        self.assertEqual(got.source, usage_mod.SOURCE_UNKNOWN)
        self.assertIn("not the period's first day", got.note)

    def test_disagreement_takes_the_larger(self):
        store = _FakeSnapshots()
        self.measure(_FakeReader({}, 10), store, period=1, matchup=1)
        got = self.measure(
            _FakeReader({11: 9}, 14), store, period=4, matchup=1
        )
        self.assertEqual(got.games, 9.0)
        self.assertEqual(got.from_season_delta, 4)
        self.assertEqual(got.from_stat_by_slot, 9)

    def test_agreement_is_reported_as_such(self):
        store = _FakeSnapshots()
        self.measure(_FakeReader({}, 10), store, period=1, matchup=1)
        got = self.measure(_FakeReader({11: 4}, 14), store, period=4, matchup=1)
        self.assertEqual(got.source, usage_mod.SOURCE_BOTH)

    def test_no_reading_at_all_is_not_trustworthy(self):
        got = self.measure(_FakeReader({}, None), None)
        self.assertFalse(got.trustworthy)
        self.assertEqual(got.games, 0.0)


class TestPlanLineup(unittest.TestCase):
    """The pipeline, at the proportions that make the hard case appear."""

    #: Distinguishes "use the default calendar" from "pass None on purpose",
    #: which is one of the cases under test.
    DEFAULT = object()

    def plan(self, roster, projections, *, sett=None, sched=None,
             used=None, period=1, matchup=1, cal=DEFAULT):
        return pipeline.plan_lineup(
            sett or settings(),
            roster,
            projections,
            scoring_period=period,
            matchup_period=matchup,
            schedule=sched if sched is not None else {1: {period: 1}},
            calendar=calendar_like_2027() if cal is self.DEFAULT else cal,
            games_used=used,
        )

    def test_light_slate_puts_gameless_players_in_starting_slots(self):
        """13 players, 3 bench slots, 5 games today.

        You cannot bench eight players -- there is nowhere to put them -- and
        the cap counts games *played*, so a starter with no game costs nothing.
        A planner that treated this as "pick the best ten" would emit a map
        ESPN rejects.
        """
        roster = [player(i, team=(1 if i < 5 else 2)) for i in range(13)]
        projections = {i: projection(i, 30.0 - i) for i in range(13)}
        plan = self.plan(roster, projections, sched={1: {1: 1}, 2: {}})
        self.assertTrue(plan.feasible, plan.notes)
        self.assertEqual(len(plan.target), 13)
        self.assertEqual(
            sum(1 for s in plan.target.values() if s == ids.BENCH_SLOT), 3
        )
        # All five players with a game are starting.
        for pid in range(5):
            self.assertEqual(plan.target[pid], 11)

    def test_teamless_player_is_reported_not_silently_dropped(self):
        """A rostered player with proTeamId 0 is excluded for the whole week.

        That exclusion is correct -- an unsigned player cannot play -- but it
        is total and otherwise invisible, so it has to surface in the notes. A
        player already parked on IR is not news and must not be reported.
        """
        roster = [player(i) for i in range(11)]
        roster.append(player(11, team=0))
        roster.append(player(12, team=0, slot=ids.IR_SLOT, slots=(11, 12, 13),
                             ir=True))
        projections = {i: projection(i, 30.0) for i in range(13)}
        plan = self.plan(roster, projections)
        self.assertTrue(plan.feasible, plan.notes)
        teamless = [n for n in plan.notes if "proTeamId 0" in n]
        self.assertEqual(len(teamless), 1, plan.notes)
        self.assertIn("P11", teamless[0])
        # Already on IR: excluded from the report, not from the roster.
        self.assertNotIn("P12", teamless[0])
        self.assertEqual(len(plan.target), 13)

    def test_binding_cap_benches_the_marginal_player(self):
        """With one game of budget left, only the best start.

        The three benched players must be the three worst, and nobody may be
        left unassigned -- the whole reason the weights are shifted positive.
        """
        roster = [player(i) for i in range(13)]
        projections = {i: projection(i, 30.0 - i) for i in range(13)}
        cal = calendar_like_2027()
        used = usage_mod.GamesUsed(
            games=39.0, source=usage_mod.SOURCE_BOTH, trustworthy=True
        )
        plan = self.plan(
            roster, projections,
            sett=settings(lineup_slot_stat_limits=gp_cap(40)),
            sched={1: {1: 1}}, used=used, cal=cal,
        )
        self.assertTrue(plan.feasible, plan.notes)
        self.assertGreater(plan.lam, 0.0)
        benched = {p for p, s in plan.target.items() if s == ids.BENCH_SLOT}
        self.assertEqual(benched, {10, 11, 12})

    def test_uncapped_league_prices_nothing(self):
        roster = [player(i) for i in range(13)]
        projections = {i: projection(i, 10.0) for i in range(13)}
        plan = self.plan(roster, projections)
        self.assertEqual(plan.lam, 0.0)
        self.assertIn("league has no games-played cap", plan.notes)

    def test_untrustworthy_usage_disables_pricing(self):
        """A mis-priced cap is worse than an unpriced one, because only the
        first is hard to notice."""
        roster = [player(i) for i in range(13)]
        projections = {i: projection(i, 10.0) for i in range(13)}
        used = usage_mod.GamesUsed(
            games=0.0, source=usage_mod.SOURCE_UNKNOWN, trustworthy=False,
            note="both sources empty",
        )
        plan = self.plan(
            roster, projections,
            sett=settings(lineup_slot_stat_limits=gp_cap(40)), used=used,
        )
        self.assertEqual(plan.lam, 0.0)
        self.assertTrue(any("NOT priced" in n for n in plan.notes))

    def test_untrustworthy_calendar_disables_pricing(self):
        roster = [player(i) for i in range(13)]
        projections = {i: projection(i, 10.0) for i in range(13)}
        used = usage_mod.GamesUsed(
            games=5.0, source=usage_mod.SOURCE_BOTH, trustworthy=True
        )
        plan = self.plan(
            roster, projections,
            sett=settings(lineup_slot_stat_limits=gp_cap(40)),
            used=used, cal=None,
        )
        self.assertEqual(plan.lam, 0.0)
        self.assertTrue(any("NOT priced" in n for n in plan.notes))

    def test_locked_bench_player_consumes_bench_capacity(self):
        """The bug this test exists for: a locked player on the bench still
        occupies a bench slot. Fill all three underneath him and the target
        exceeds capacity, which costs the entire day's lineup."""
        roster = [player(i) for i in range(12)]
        roster.append(player(99, slot=ids.BENCH_SLOT, locked=True))
        projections = {e.player_id: projection(e.player_id, 20.0) for e in roster}
        plan = self.plan(roster, projections)
        self.assertTrue(plan.feasible, plan.notes)
        self.assertEqual(
            sum(1 for s in plan.target.values() if s == ids.BENCH_SLOT), 3
        )
        self.assertEqual(plan.target[99], ids.BENCH_SLOT)

    def test_locked_starter_keeps_his_slot(self):
        roster = [player(i) for i in range(13)]
        roster[12] = player(12, slot=11, locked=True)
        projections = {i: projection(i, 30.0 - i) for i in range(13)}
        used = usage_mod.GamesUsed(games=39.0, source="x", trustworthy=True)
        plan = self.plan(
            roster, projections,
            sett=settings(lineup_slot_stat_limits=gp_cap(40)), used=used,
        )
        self.assertTrue(plan.feasible, plan.notes)
        self.assertEqual(plan.target[12], 11)
        self.assertFalse(any(m.player_id == 12 for m in plan.moves))

    def test_ir_eligible_player_is_parked_to_free_the_bench(self):
        roster = [player(i) for i in range(12)]
        roster.append(
            player(50, slot=ids.BENCH_SLOT, status="OUT", ir=True,
                   slots=(11, 12, 13))
        )
        projections = {e.player_id: projection(e.player_id, 15.0) for e in roster}
        projections[50] = projection(50, 15.0, p_play=0.0)
        plan = self.plan(roster, projections)
        self.assertTrue(plan.feasible, plan.notes)
        self.assertEqual(plan.target[50], ids.IR_SLOT)
        self.assertTrue(any("to IR" in n for n in plan.notes))

    def test_ir_player_who_lost_eligibility_must_vacate(self):
        """ESPN's IR eligibility is its own rule. Leaving a now-ineligible
        player on IR gets the whole transaction rejected."""
        roster = [player(i) for i in range(12)]
        roster.append(
            player(60, slot=ids.IR_SLOT, status="ACTIVE", ir=False,
                   slots=(11, 12, 13))
        )
        projections = {e.player_id: projection(e.player_id, 15.0) for e in roster}
        plan = self.plan(roster, projections)
        self.assertTrue(plan.feasible, plan.notes)
        self.assertNotEqual(plan.target[60], ids.IR_SLOT)
        self.assertTrue(any("must come off IR" in n for n in plan.notes))

    def test_no_pointless_churn(self):
        """Indifferent players stay put. Every move lands in a public activity
        feed, so a reshuffle that gains nothing is a cost with no benefit."""
        roster = [player(i) for i in range(10)]
        roster += [player(i, slot=ids.BENCH_SLOT) for i in range(10, 13)]
        projections = {i: projection(i, 0.0) for i in range(13)}
        plan = self.plan(roster, projections, sched={1: {}})
        self.assertTrue(plan.feasible, plan.notes)
        self.assertEqual(plan.moves, ())

    def test_unprojectable_player_is_never_preferred(self):
        """He still has to stand somewhere, but he does not take a slot from a
        player we can actually value."""
        roster = [player(i) for i in range(13)]
        projections = {i: projection(i, 20.0) for i in range(13)}
        del projections[7]
        plan = self.plan(roster, projections)
        self.assertTrue(plan.feasible, plan.notes)
        self.assertEqual(plan.target[7], ids.BENCH_SLOT)

    def test_target_is_always_total(self):
        roster = [player(i) for i in range(13)]
        projections = {i: projection(i, 5.0) for i in range(13)}
        plan = self.plan(roster, projections)
        self.assertEqual(set(plan.target), {e.player_id for e in roster})


class TestValidateTarget(unittest.TestCase):
    """The duplicate of ``_preflight_lineup``. It is duplicated so a bad plan
    becomes a reported non-event instead of an exception that aborts the run."""

    def test_ineligible_slot(self):
        roster = [player(1, slots=(12,))]
        problems = pipeline.validate_target(settings(), roster, {1: 11})
        self.assertTrue(any("not eligible" in p for p in problems))

    def test_over_capacity(self):
        roster = [player(i) for i in range(4)]
        target = {i: ids.BENCH_SLOT for i in range(4)}
        problems = pipeline.validate_target(settings(), roster, target)
        self.assertTrue(any("capacity is 3" in p for p in problems))

    def test_partial_map(self):
        roster = [player(1), player(2)]
        problems = pipeline.validate_target(settings(), roster, {1: 11})
        self.assertTrue(any("unassigned" in p for p in problems))

    def test_locked_player_moved(self):
        roster = [player(1, slot=11, locked=True)]
        problems = pipeline.validate_target(
            settings(), roster, {1: ids.BENCH_SLOT}
        )
        self.assertTrue(any("lineup-locked" in p for p in problems))

    def test_ir_without_eligibility(self):
        roster = [player(1, slots=(11, 12, 13), ir=False)]
        problems = pipeline.validate_target(settings(), roster, {1: ids.IR_SLOT})
        self.assertTrue(any("IR-eligible" in p for p in problems))

    def test_entire_roster_benched(self):
        roster = [player(i, slot=ids.BENCH_SLOT) for i in range(2)]
        problems = pipeline.validate_target(
            settings(lineup_slot_counts={11: 10, 12: 3}), roster,
            {0: ids.BENCH_SLOT, 1: ids.BENCH_SLOT},
        )
        self.assertTrue(any("benches the entire roster" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
