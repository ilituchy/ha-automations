"""Add/drop decision tests.

The requirement these encode, in the user's own words:

    "id rather say if theres a player that we think is doing 10-15% better
    than any of our lowest players across the board, we should make a move.
    The biggest thing we need to consider is games played. we may want to drop
    our weakest player to pick someone up to get another game in etc, and
    thats okay too. points matter in this league. even if we pick up a player
    who gives us 15 points that 15 points more than we would have had and that
    could decide a matchup."

plus the keeper asymmetry:

    "remember that in the dynasty league we keep players. being out for the
    season there does not automatically mean we'd want to drop them. different
    from the other league."

Every test below is one sentence of that, turned into arithmetic. The ones
worth reading are :meth:`TestGamesDriveIt.test_an_empty_slot_makes_any_body_worth_it`
and :meth:`TestKeeperAsymmetry.test_a_season_ending_injury_is_a_free_drop_in_redraft`,
which are the two halves the requirement says are different.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

from espn import ids  # noqa: E402
from models import (  # noqa: E402
    LeagueSettings,
    MatchupScore,
    Projection,
    RosterEntry,
    StatLimit,
)
from solve import addrop  # noqa: E402

#: Three starting slots and no bench, so a three-player roster is exactly
#: **full**. That matters more than it looks: ``espn/write.py`` allows an
#: unpaired add whenever the roster has room, and an unpaired add dominates
#: every swap -- nothing goes out. A fixture with spare slots therefore makes
#: every gate below untestable, because the answer is always "just add him".
#: Both live rosters are full (14/14 and 24/24), so full is the real case.
def slot_counts(*, starting=3, bench=0, ir=0):
    counts = {11: int(starting)}
    if bench:
        counts[12] = int(bench)
    if ir:
        counts[13] = int(ir)
    return counts


def settings(**kw) -> LeagueSettings:
    base = dict(
        league_id=111111111,
        season_id=2027,
        name="test league",
        scoring_type="H2H_POINTS",
        lineup_slot_counts=slot_counts(),
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


def player(pid, *, name=None, team=1, slot=11, locked=False, status="ACTIVE",
           acq="FREEAGENT", owned=None, adp=None, rank=None,
           outlook="") -> RosterEntry:
    return RosterEntry(
        player_id=pid,
        name=name or "P%d" % pid,
        current_slot=slot,
        eligible_slots=frozenset((11, 12)),
        pro_team_id=team,
        injury_status=status,
        lineup_locked=locked,
        acquisition_status=acq,
        percent_owned=owned,
        draft_adp=adp,
        draft_rank=rank,
        outlook=outlook,
    )


def projection(pid, p_play=1.0) -> Projection:
    return Projection(
        player_id=pid,
        mean={ids.PTS: 0.0},
        var={ids.PTS: 0.0},
        p_play=p_play,
        minutes=30.0,
        source="test",
        as_of=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )


class _Case:
    """One scenario, assembled so each test states only what it changes.

    ``schedule`` is ``pro_team -> {scoring_period: games}``. Team 1 plays every
    day of the window; team 2 plays only on day 1; team 3 never plays.
    """

    def __init__(self, **kw):
        self.horizon = kw.pop("horizon", [1, 2, 3])
        self.schedule = kw.pop("schedule", {
            1: {1: 1, 2: 1, 3: 1},
            2: {1: 1},
            3: {},
        })
        self.roster = kw.pop("roster", [])
        self.pool = kw.pop("pool", [])
        self.values = kw.pop("values", {})
        self.p_play = kw.pop("p_play", {})
        self.settings = kw.pop("settings", settings())
        self.kw = kw

    def run(self, **over):
        everyone = list(self.roster) + list(self.pool)
        projections = {
            e.player_id: projection(e.player_id, self.p_play.get(e.player_id, 1.0))
            for e in everyone
        }
        call = dict(self.kw)
        call.update(over)
        return addrop.plan_addrop(
            self.settings,
            self.roster,
            self.pool,
            self.values,
            projections,
            scoring_period=self.horizon[0] if self.horizon else 1,
            matchup_period=1,
            horizon=self.horizon,
            schedule=self.schedule,
            **call,
        )


# ---------------------------------------------------------------------------
# the acquisition cap
# ---------------------------------------------------------------------------


class TestAcquisitionCap(unittest.TestCase):
    def test_the_fraction_is_rounded_not_truncated(self):
        """A fifth of a seventh does not have to survive the round trip.

        The spelling ESPN actually sends, ``0.7142857142857143``, multiplies
        back to exactly ``5.0``: all 17 significant digits of the double are on
        the wire. That is luck, and it is one shortened field away from not
        holding -- ``0.7142857 * 7`` is ``4.999999900000001``, and ``int()`` of
        that is 4. A league with five weekly acquisitions would be silently
        reduced to four, which nobody could find from the settings page,
        because the settings page says five.
        """
        self.assertEqual(int(0.7142857 * ids.SCORING_PERIODS_PER_MATCHUP), 4)
        st = settings(
            acquisition_limit=0.7142857,
            acquisition_limit_per_scoring_period=True,
        )
        self.assertEqual(addrop.acquisitions_per_matchup(st), 5)
        # And the spelling on the live wire, which truncation happens to
        # survive. Both must read as five, for the same reason.
        st = settings(
            acquisition_limit=5.0 / 7.0,
            acquisition_limit_per_scoring_period=True,
        )
        self.assertEqual(addrop.acquisitions_per_matchup(st), 5)

    def test_the_other_live_league_reads_as_seven(self):
        st = settings(
            acquisition_limit=1.0, acquisition_limit_per_scoring_period=True
        )
        self.assertEqual(addrop.acquisitions_per_matchup(st), 7)

    def test_a_face_value_limit_is_not_multiplied(self):
        st = settings(
            acquisition_limit=3.0, acquisition_limit_per_scoring_period=False
        )
        self.assertEqual(addrop.acquisitions_per_matchup(st), 3)

    def test_absent_and_nonpositive_both_mean_unlimited(self):
        self.assertIsNone(addrop.acquisitions_per_matchup(settings()))
        self.assertIsNone(
            addrop.acquisitions_per_matchup(settings(acquisition_limit=-1.0))
        )

    def test_remaining_counts_down_from_the_matchup_period_key(self):
        st = settings(
            acquisition_limit=5.0 / 7.0,
            acquisition_limit_per_scoring_period=True,
        )
        self.assertEqual(addrop.remaining_acquisitions(st, {1: 1}, 1), 4)
        # Another week's spend is not this week's.
        self.assertEqual(addrop.remaining_acquisitions(st, {2: 5}, 1), 5)
        # An absent key is a real zero here: ESPN maintains this counter for
        # every team unconditionally.
        self.assertEqual(addrop.remaining_acquisitions(st, {}, 1), 5)
        self.assertEqual(addrop.remaining_acquisitions(st, None, 1), 5)
        # Never negative, even if ESPN's count exceeds our reading of the cap.
        self.assertEqual(addrop.remaining_acquisitions(st, {1: 9}, 1), 0)

    def test_an_exhausted_week_proposes_nothing(self):
        st = settings(
            acquisition_limit=1.0 / 7.0,
            acquisition_limit_per_scoring_period=True,
        )
        case = _Case(
            settings=st,
            roster=[player(1)],
            pool=[player(90, name="Great")],
            values={1: 5.0, 90: 40.0},
        )
        plan = case.run(acquisitions_used={1: 1})
        self.assertFalse(plan.proposed)
        self.assertEqual(plan.acquisitions_left, 0)
        self.assertIn("no acquisitions left", plan.notes[0])


# ---------------------------------------------------------------------------
# the two duals
# ---------------------------------------------------------------------------


class TestSlotPrices(unittest.TestCase):
    def test_a_slot_is_free_when_fewer_play_than_we_have_slots(self):
        """The case the whole design turns on: three starting slots, two
        players with a game. There is nobody to bench, so a newcomer's whole
        value is gain -- *"that 15 points more than we would have had."*"""
        roster = [player(1, team=1), player(2, team=1), player(3, team=3)]
        prices = addrop.slot_prices(
            roster, {1: 30.0, 2: 20.0, 3: 50.0},
            starting_slots=3, horizon=[1, 2],
            schedule={1: {1: 1, 2: 1}, 3: {}},
        )
        self.assertEqual(prices, {1: 0.0, 2: 0.0})

    def test_a_full_slate_prices_a_slot_at_the_worst_starter(self):
        roster = [player(i, team=1) for i in range(1, 6)]
        prices = addrop.slot_prices(
            roster, {1: 50.0, 2: 40.0, 3: 30.0, 4: 20.0, 5: 10.0},
            starting_slots=3, horizon=[1],
            schedule={1: {1: 1}},
        )
        # Five players have games for three slots; the third-best starts last,
        # so displacing him is what a newcomer costs.
        self.assertEqual(prices, {1: 30.0})

    def test_a_negative_valued_roster_does_not_price_a_slot_below_zero(self):
        """Benching is free, so a slot can never be worth less than nothing."""
        roster = [player(i, team=1) for i in range(1, 4)]
        prices = addrop.slot_prices(
            roster, {1: -5.0, 2: -9.0, 3: -1.0},
            starting_slots=3, horizon=[1], schedule={1: {1: 1}},
        )
        self.assertEqual(prices, {1: 0.0})


class TestWeekSurplus(unittest.TestCase):
    def test_surplus_is_summed_per_game_day_over_the_hurdle(self):
        got = addrop.week_surplus(
            per_game=20.0, p_play=1.0, game_days=[1, 2, 3],
            lam=0.0, prices={1: 15.0, 2: 0.0, 3: 25.0},
        )
        # 5 + 20 + 0 -- the third day he would sit, which costs nothing.
        self.assertEqual(got, 25.0)

    def test_lambda_and_the_slot_price_do_not_stack(self):
        """They are duals on two different constraints, and a start pays the
        binding one, not both. Adding them would double-charge."""
        got = addrop.week_surplus(
            per_game=30.0, p_play=1.0, game_days=[1],
            lam=10.0, prices={1: 20.0},
        )
        self.assertEqual(got, 10.0)

    def test_availability_scales_the_surplus_but_not_the_hurdle(self):
        """``p_play`` factors out of the threshold and still scales the
        quantity -- the cancellation argument in ``budget.py``."""
        got = addrop.week_surplus(
            per_game=20.0, p_play=0.5, game_days=[1, 2],
            lam=10.0, prices={},
        )
        self.assertEqual(got, 10.0)

    def test_an_exhausted_cap_makes_everyone_worthless(self):
        got = addrop.week_surplus(
            per_game=99.0, p_play=1.0, game_days=[1, 2, 3],
            lam=float("inf"), prices={},
        )
        self.assertEqual(got, 0.0)


# ---------------------------------------------------------------------------
# the gates
# ---------------------------------------------------------------------------


class TestQualityGate(unittest.TestCase):
    """Three players, all on team 1, all playing every day of the window.

    Same games on both sides of every swap on purpose, so the games reason
    cannot fire and the percentage bar is tested alone. With three starting
    slots and three players who play, a slot prices at the worst of them (20),
    which is also what makes the weakest player's own surplus zero.
    """

    def case(self, add_value):
        return _Case(
            roster=[player(1, team=1), player(2, name="Weak", team=1),
                    player(3, team=1)],
            pool=[player(90, name="Better", team=1)],
            values={1: 60.0, 2: 20.0, 3: 40.0, 90: add_value},
        )

    def test_twenty_percent_better_clears_the_twelve_percent_bar(self):
        plan = self.case(24.0).run()
        self.assertTrue(plan.proposed)
        self.assertEqual(plan.add.player_id, 90)
        self.assertEqual(plan.drop.player_id, 2)
        self.assertEqual(plan.reason, addrop.REASON_QUALITY)
        # (24 - 20) over three days in; the man going out was already at the
        # slot price, so he was contributing nothing.
        self.assertAlmostEqual(plan.gain, 12.0)

    def test_ten_percent_better_does_not(self):
        """The bar is a real bar, and this is the case it exists for: a swap
        that gains points but not enough of them to be worth permanence."""
        plan = self.case(22.0).run()
        self.assertFalse(plan.proposed)
        self.assertIn("without a quality or games reason",
                      " ".join(plan.notes))

    def test_the_bar_is_configurable_because_the_user_named_a_range(self):
        """"10-15%" is a range, so the number is an option, not a constant."""
        self.assertTrue(self.case(22.0).run(margin_pct=10.0).proposed)
        self.assertFalse(self.case(22.0).run(margin_pct=15.0).proposed)


class TestGamesDriveIt(unittest.TestCase):
    def test_more_games_beats_better_per_game(self):
        """*"we may want to drop our weakest player to pick someone up to get
        another game in etc, and thats okay too."*

        Player 2 is better per game than the pickup -- 40 against 36, so the
        quality gate refuses it -- but he is on a team that plays once more
        this week and the pickup's plays three times. The week's points say
        swap, and the week's points are what matter.
        """
        case = _Case(
            roster=[player(1, team=1), player(2, name="One Game", team=2),
                    player(3, team=1)],
            pool=[player(90, name="Three Games", team=1)],
            values={1: 60.0, 2: 40.0, 3: 50.0, 90: 36.0},
        )
        plan = case.run()
        self.assertTrue(plan.proposed)
        self.assertEqual(plan.reason, addrop.REASON_GAMES)
        self.assertEqual(plan.drop.player_id, 2)
        # Day 1 all three play, so a slot prices at 40 and the pickup would
        # sit; days 2 and 3 only two play, so a slot is free and he is worth
        # his whole 36. The man going out was exactly at the slot price.
        self.assertAlmostEqual(plan.gain, 72.0)

    def test_an_empty_slot_makes_any_body_worth_it(self):
        """*"even if we pick up a player who gives us 15 points that 15 points
        more than we would have had."*

        Two players for three starting slots: there is nobody to bench, so a
        slot is free and the pickup's whole value is gain. Nothing goes out
        either -- the roster has room, which ``espn/write.py`` permits.
        """
        case = _Case(
            roster=[player(1, team=1), player(2, team=1)],
            pool=[player(90, name="Warm Body", team=2)],
            values={1: 60.0, 2: 50.0, 90: 15.0},
        )
        plan = case.run()
        self.assertTrue(plan.proposed)
        self.assertIsNone(plan.drop)
        self.assertEqual(plan.reason, addrop.REASON_OPEN)
        self.assertAlmostEqual(plan.gain, 15.0)

    def test_a_pickup_who_would_only_sit_is_worth_nothing(self):
        """The other half of the same idea, and the reason the slot price has
        to exist. Five players with games for three slots: a sixth at 15 a game
        never cracks the lineup, so he is not 15 points more than we would have
        had, he is zero."""
        case = _Case(
            settings=settings(lineup_slot_counts=slot_counts(bench=2)),
            roster=[player(i, team=1) for i in range(1, 6)],
            pool=[player(90, name="Would Sit", team=1)],
            values={1: 50.0, 2: 45.0, 3: 40.0, 4: 35.0, 5: 30.0, 90: 15.0},
        )
        plan = case.run()
        self.assertFalse(plan.proposed)
        self.assertIn("clears the hurdle", " ".join(plan.notes))

    def test_a_high_lambda_makes_a_mediocre_pickup_worthless(self):
        """The league asymmetry, in one assertion. The same pickup, decided
        under each league's observed shadow price: free upside at
        ``lambda = 0.00``, and worthless at the dynasty league's ``20.20``,
        where playing him costs a game we would rather spend on someone
        better later in the week."""
        case = _Case(
            roster=[player(1, team=1), player(2, team=1), player(3, team=1)],
            pool=[player(90, name="Eighteen", team=1)],
            values={1: 60.0, 2: 5.0, 3: 30.0, 90: 18.0},
        )
        self.assertTrue(case.run(lam=0.0).proposed)
        self.assertFalse(case.run(lam=20.20).proposed)

    def test_an_exhausted_budget_proposes_nothing(self):
        case = _Case(
            roster=[player(1, team=1)],
            pool=[player(90, name="Great", team=1)],
            values={1: 1.0, 90: 60.0},
        )
        plan = case.run(lam=float("inf"))
        self.assertFalse(plan.proposed)
        self.assertIn("budget exhausted", " ".join(plan.notes))


class TestChurnFloor(unittest.TestCase):
    """A one-day window, so the gain is small enough for the floor to bite."""

    def case(self):
        return _Case(
            horizon=[1],
            roster=[player(1, team=1), player(2, name="Weak", team=1),
                    player(3, team=1)],
            pool=[player(90, name="Hair Better", team=1)],
            values={1: 60.0, 2: 20.0, 3: 40.0, 90: 22.5},
        )

    def test_a_gain_under_the_floor_is_not_worth_being_permanent(self):
        """2.5 points, over the 12% quality bar and under the 3-point floor.
        Every roster has a swap like this available on any given day, and
        acting on all of them spends a five-a-week budget on rounding error."""
        plan = self.case().run()
        self.assertFalse(plan.proposed)
        self.assertIn("churn floor", " ".join(plan.notes))
        self.assertTrue(self.case().run(churn_floor=0.0).proposed)

    def test_being_behind_by_less_than_the_gain_waives_the_floor(self):
        """*"that could decide a matchup."* 2.5 points is noise in general and
        is not noise when we are losing by one."""
        behind = MatchupScore(
            matchup_period=1, my_team_id=7, my_points=99.0,
            opp_team_id=6, opp_name="Them", opp_points=100.0,
        )
        plan = self.case().run(matchup=behind)
        self.assertTrue(plan.proposed)
        self.assertIn("churn floor is waived", " ".join(plan.notes))

    def test_being_behind_by_more_than_the_gain_does_not(self):
        """The waiver is for moves that could flip the scoreboard by
        themselves, not for every move made while behind."""
        far_behind = MatchupScore(
            matchup_period=1, my_team_id=7, my_points=40.0,
            opp_team_id=6, opp_name="Them", opp_points=100.0,
        )
        self.assertFalse(self.case().run(matchup=far_behind).proposed)

    def test_being_ahead_never_raises_the_floor(self):
        """We have our own projection and not the opponent's, so a lead is not
        evidence the week is safe. The score may only ever waive the floor."""
        ahead = MatchupScore(
            matchup_period=1, my_team_id=7, my_points=300.0,
            opp_team_id=6, opp_name="Them", opp_points=10.0,
        )
        plan = self.case().run(matchup=ahead, churn_floor=0.0)
        self.assertTrue(plan.proposed)
        self.assertEqual(plan.gain, self.case().run(churn_floor=0.0).gain)

    def test_a_bye_is_not_a_tie_we_are_losing(self):
        """A one-sided row reads ``opp_points = 0.0``; treating that as a live
        0-0 would make every marginal swap look decisive."""
        bye = MatchupScore(
            matchup_period=1, my_team_id=7, my_points=0.0,
            opp_team_id=None, opp_name="", opp_points=0.0,
        )
        self.assertFalse(self.case().run(matchup=bye).proposed)


# ---------------------------------------------------------------------------
# who may go out
# ---------------------------------------------------------------------------


class TestDropProtections(unittest.TestCase):
    """A full three-man roster where exactly one swap is worth making.

    "Weak" at 5 a game is the only player the 25-a-game pickup beats: a slot
    prices at 5.0 (three players for three slots, so the worst starter sets the
    price), which makes Weak's own surplus zero and the pickup's 60. Every
    protection below works by taking Weak off the table, and the proposal has
    to collapse rather than fall through to a swap that loses points.
    """

    def base(self, **kw):
        return _Case(
            roster=[player(1, team=1), player(2, name="Weak", team=1, **kw),
                    player(3, team=1)],
            pool=[player(90, name="Better", team=1)],
            values={1: 60.0, 2: 5.0, 3: 30.0, 90: 25.0},
        )

    def test_a_starter_is_never_dropped(self):
        """Their game may already be under way, and a drop that empties a
        started slot cannot be undone."""
        case = self.base()
        self.assertTrue(case.run().proposed)
        plan = case.run(starters=[1, 2])
        self.assertFalse(plan.proposed)
        self.assertIn("starting today", " ".join(plan.notes))

    def test_never_drop_is_matched_the_way_write_py_matches_it(self):
        """Case-insensitively and stripped, so a configured name that preflight
        would refuse is refused here instead of proposed and rejected at the
        wire."""
        plan = self.base().run(never_drop=["  weak  "])
        self.assertFalse(plan.proposed)
        self.assertIn("never_drop", " ".join(plan.notes))

    def test_a_locked_player_is_never_dropped(self):
        plan = self.base(locked=True).run()
        self.assertFalse(plan.proposed)
        self.assertIn("(locked)", " ".join(plan.notes))

    def test_a_full_roster_with_nobody_droppable_proposes_nothing(self):
        case = _Case(
            settings=settings(lineup_slot_counts={11: 1, 12: 1}),
            roster=[player(1, team=1), player(2, team=1)],
            pool=[player(90, name="Better", team=1)],
            values={1: 30.0, 2: 5.0, 90: 25.0},
        )
        plan = case.run(starters=[1, 2])
        self.assertFalse(plan.proposed)
        self.assertIn("nowhere to go", " ".join(plan.notes))


class TestMarketSignalsAreCarriedNotPriced(unittest.TestCase):
    """ESPN's ownership readings reach the decision and change none of it.

    Both halves matter. They have to arrive, because the layer that reasons in
    prose cannot see what it was not handed. And they have to change nothing
    here, because every gate in this module is denominated in points or in
    percent-per-game, and "but 88% of the league owns him" has no exchange rate
    into either -- inventing one is how a brake becomes a guess.
    """

    def case(self, **over):
        """``TestQualityGate``'s shape, with the ownership block filled in.

        Three starting slots, three players who all play every day, so a slot
        prices at the worst of them and the 24-against-20 swap is worth 12.
        """
        c = _Case(
            roster=[
                player(1, team=1, owned=99.9, adp=3.6, rank=3),
                player(2, name="Weak", team=1, owned=47.6, adp=131.2, rank=136),
                player(3, team=1, owned=90.6, adp=68.7, rank=58),
            ],
            pool=[player(90, name="Better", team=1, owned=63.6, adp=121.8,
                         rank=121, outlook="Likely starter.")],
            values={1: 60.0, 2: 20.0, 3: 40.0, 90: 24.0},
        )
        c.kw.update(over)
        return c

    def test_they_reach_both_sides_of_the_proposal(self):
        plan = self.case().run()
        self.assertTrue(plan.proposed)
        self.assertEqual(plan.add.percent_owned, 63.6)
        self.assertEqual(plan.add.draft_rank, 121)
        self.assertEqual(plan.add.outlook, "Likely starter.")
        self.assertEqual(plan.drop.name, "Weak")
        self.assertEqual(plan.drop.percent_owned, 47.6)
        self.assertEqual(plan.drop.draft_adp, 131.2)

    def test_the_most_owned_player_is_still_the_one_proposed(self):
        """Ownership is inverted against value, and value still decides.

        The pickup here is owned in fewer leagues than the man he replaces
        would be if the numbers were read as a preference. They are not read at
        all, so the swap goes through on its 25-against-5 per-game case -- and
        any future version that quietly starts pricing ownership breaks here
        rather than in the activity feed.
        """
        plan = self.case().run()
        self.assertEqual(plan.add.name, "Better")
        self.assertGreater(plan.drop.percent_owned, 0.0)
        self.assertEqual(plan.reason, addrop.REASON_QUALITY)

    def test_a_missing_reading_is_unknown_and_says_so(self):
        c = _Case(
            roster=[player(1, team=1), player(2, name="Weak", team=1),
                    player(3, team=1)],
            pool=[player(90, name="Better", team=1)],
            values={1: 60.0, 2: 20.0, 3: 40.0, 90: 24.0},
        )
        plan = c.run()
        self.assertIsNone(plan.add.percent_owned)
        self.assertEqual(plan.add.describe_market(), "no market readings reported")

    def test_the_market_description_names_every_reading_it_has(self):
        plan = self.case().run()
        said = plan.drop.describe_market()
        self.assertIn("48%", said)
        self.assertIn("131", said)
        self.assertIn("136", said)


class TestIrIsNotRoom(unittest.TestCase):
    """An empty IR slot is not somewhere a pickup may go.

    This is a regression, and it got all the way to the wire: both live leagues
    were proposed unpaired ``open-spot`` adds, and tapping **Approve** on the
    Chairman's one came back as an error from ESPN because the roster had no
    room. ``lineupSlotCounts`` summed to 16 there -- 13 active slots plus 3 IR
    -- against 14 players of whom one sat on IR, so the full sum read as "two
    spots free" when the true answer was none. ESPN will only accept a player
    into an IR slot if his own designation makes him IR-eligible, which a free
    agent worth acquiring never is.

    Both shapes below are the two live leagues as measured on 2026-10-21, which
    is the point: one of them has no room and one genuinely has two spots, and
    a fix that only hard-codes "the roster is full" would pass the first test
    and fail the second.
    """

    def chairmans(self, **over):
        """10 starting + 3 bench + 3 IR, holding 13 healthy and 1 on IR.

        Four of the thirteen are weak, so the tenth-best starter prices a slot
        at 5.0 and the 25-a-game pickup is worth +60 for the week. The man on
        IR plays for team 3, which never plays -- he is out, so he neither sets
        the slot price nor offers any surplus of his own.
        """
        roster = [player(i, team=1) for i in range(1, 14)]
        roster.append(player(99, name="Hurt", team=3, slot=13, status="OUT"))
        values = {i: 60.0 for i in range(1, 10)}
        values.update({i: 5.0 for i in range(10, 14)})
        values[99] = 1.0
        values[90] = 25.0
        case = _Case(
            settings=settings(lineup_slot_counts={11: 10, 12: 3, 13: 3}),
            roster=roster,
            pool=[player(90, name="Better", team=1)],
            values=values,
            p_play={99: 0.0},
        )
        case.kw.update(over)
        return case

    def test_the_pickup_has_to_be_paired_with_a_drop(self):
        """13 healthy players in 13 active slots: somebody has to go.

        Before the fix this came out as ``drop=None`` with reason
        ``open-spot``, which is the proposal ESPN refused.
        """
        plan = self.chairmans().run()
        self.assertTrue(plan.proposed)
        self.assertIsNotNone(plan.drop)
        self.assertNotEqual(plan.reason, addrop.REASON_OPEN)

    def test_the_paired_drop_is_not_the_man_already_on_ir(self):
        """The same bug one level down, and it survived the first fix.

        Dropping someone off IR frees an *IR* slot: the active count does not
        move, so ESPN refuses add+drop exactly as it refused the unpaired add.
        Hurt is also the drop this fixture would otherwise choose -- he is the
        cheapest way out on points and the lowest ``p_play`` on the tie-break
        -- so "a drop was paired" is not enough to assert.
        """
        plan = self.chairmans().run()
        self.assertIsNotNone(plan.drop)
        self.assertNotEqual(plan.drop.name, "Hurt")
        self.assertIn(
            "Hurt (on IR, which frees no active spot)", " ".join(plan.notes)
        )

    def test_a_man_on_ir_is_not_reported_as_blocked_when_there_is_real_room(self):
        """The exclusion is about making room, not about protecting him.

        The same roster with one more bench slot. Nothing has to go out now, so
        the plan is the unpaired add -- an open spot beats every swap on
        ``_pair_key`` by construction, because nobody leaves. What has to be
        true is that he is no longer *reported* as undroppable: that note
        explains an absence of room, and claiming it where there is room would
        be telling the user a reason that did not apply.
        """
        case = self.chairmans()
        case.settings = settings(lineup_slot_counts={11: 10, 12: 4, 13: 3})
        plan = case.run()
        self.assertTrue(plan.proposed)
        self.assertIsNone(plan.drop)
        self.assertEqual(plan.reason, addrop.REASON_OPEN)
        self.assertNotIn("on IR", " ".join(plan.notes))

    def test_no_active_room_and_nobody_droppable_says_so_in_active_spots(self):
        """And the count it reports is of active spots, not of roster slots.

        A note reading "full (14/16)" would be arithmetically true and would
        describe a roster with two spare slots, which is exactly the reading
        that caused the bug.
        """
        plan = self.chairmans(
            starters=list(range(1, 14)), never_drop=["Hurt"]
        ).run()
        self.assertFalse(plan.proposed)
        notes = " ".join(plan.notes)
        self.assertIn("roster is full (13/13 active spots)", notes)

    def test_two_players_on_ir_leave_two_active_spots_genuinely_free(self):
        """The Dynassty shape: 11 starting + 13 bench + 3 IR, 24 players, 2 on
        IR. 22 healthy in 24 active slots, so an unpaired add is legitimate and
        has to stay that way -- the fix excludes IR slots from the cap *and*
        IR occupants from the count, and dropping either half turns this
        league's correct proposal into a needless drop.
        """
        roster = [player(i, team=1) for i in range(1, 23)]
        roster += [player(98, name="Hurt A", team=3, slot=13, status="OUT"),
                   player(99, name="Hurt B", team=3, slot=13, status="OUT")]
        values = {i: 60.0 for i in range(1, 23)}
        values.update({98: 1.0, 99: 1.0, 90: 80.0})
        plan = _Case(
            settings=settings(lineup_slot_counts={11: 11, 12: 13, 13: 3}),
            roster=roster,
            pool=[player(90, name="Star", team=1)],
            values=values,
            p_play={98: 0.0, 99: 0.0},
        ).run()
        self.assertTrue(plan.proposed)
        self.assertIsNone(plan.drop)
        self.assertEqual(plan.reason, addrop.REASON_OPEN)


class TestKeeperAsymmetry(unittest.TestCase):
    """*"in the dynasty league we keep players ... different from the other
    league."*

    The same roster and the same pool, decided twice.
    """

    def case(self, keeper):
        """Six players for six roster spots, three of them starting.

        The injured star is out for the season, so his week surplus is zero
        while his per-game value is the highest on the team -- the exact
        divergence between the two metrics the module keeps apart. Players 4
        and 5 are the dead weight: a slot prices at 35 (the third-best of the
        six with games), so neither of them, nor the star, contributes a point
        this week. All three are therefore tied on gain, and which one goes out
        is decided entirely by who may go out.
        """
        roster = [player(i, team=1) for i in range(1, 6)]
        roster.append(player(6, name="Injured Star", team=1, status="OUT"))
        return _Case(
            settings=settings(
                keeper=keeper, lineup_slot_counts=slot_counts(bench=3),
            ),
            roster=roster,
            pool=[player(90, name="Replacement", team=1)],
            values={1: 40.0, 2: 35.0, 3: 30.0, 4: 25.0, 5: 20.0,
                    6: 60.0, 90: 45.0},
            p_play={6: 0.0},
        )

    def test_a_season_ending_injury_is_a_free_drop_in_redraft(self):
        """Three candidates all tied at +30, and the star loses the tie-break
        on ``p_play``: a man who will not suit up cannot help us on any day,
        while 4 and 5 can still matter if someone ahead of them is ruled out
        tomorrow. Nothing in the rules mentions injuries."""
        plan = self.case(keeper=False).run(starters=[1, 2, 3])
        self.assertTrue(plan.proposed)
        self.assertEqual(plan.drop.name, "Injured Star")
        self.assertEqual(plan.drop.surplus, 0.0)
        # He has games on the NBA schedule and none we can start, which is what
        # the games reason is allowed to notice and the schedule alone is not.
        self.assertEqual(plan.drop.games, 3)
        self.assertEqual(plan.drop.startable_games, 0.0)
        self.assertEqual(plan.reason, addrop.REASON_GAMES)

    def test_the_same_player_is_protected_in_a_keeper_league(self):
        """*"being out for the season there does not automatically mean we'd
        want to drop them."*

        He is the highest per-game value on the roster, so the rank gate keeps
        him however worthless he is this week -- stated as a rule rather than
        guessed at with a discount rate we would have had to invent.

        And the move still happens. The gate restricts who may go out; it does
        not switch add/drop off, so the same pickup arrives at the same +30 with
        the genuinely weakest player going out instead.
        """
        plan = self.case(keeper=True).run(starters=[1, 2, 3])
        self.assertTrue(plan.proposed)
        self.assertNotEqual(plan.drop.name, "Injured Star")
        self.assertEqual(plan.drop.player_id, 5)
        self.assertAlmostEqual(plan.gain, 30.0)
        self.assertIn("keeper league", " ".join(plan.notes))

    def test_the_gate_is_a_rank_and_the_rank_is_stated_in_the_notes(self):
        """A quarter of six is one and a half, which rounds to two: players 5
        and 4. The number reaches the approval card, because "we did not drop
        him, he is a keeper" is only auditable if the cutoff is visible."""
        plan = self.case(keeper=True).run(starters=[1, 2, 3])
        self.assertIn("only the 2 lowest-valued of 6 players may be dropped",
                      " ".join(plan.notes))

    def test_a_redraft_league_states_no_such_gate(self):
        plan = self.case(keeper=False).run(starters=[1, 2, 3])
        self.assertNotIn("keeper", " ".join(plan.notes))


# ---------------------------------------------------------------------------
# what reaches the write path
# ---------------------------------------------------------------------------


class TestProposalShape(unittest.TestCase):
    def test_waiver_is_read_off_the_player_not_the_league(self):
        """ADR-017. One live league had its whole pool on waivers while the
        other had 299 free agents and exactly one waiver player, so there is no
        league-level answer to read."""
        case = _Case(
            roster=[player(1, team=1), player(2, team=1)],
            pool=[player(90, name="Claimed", team=1, acq="WAIVERS")],
            values={1: 30.0, 2: 5.0, 90: 25.0},
        )
        plan = case.run(starters=[1])
        self.assertTrue(plan.add.waiver)
        self.assertIn("on waivers", plan.rationale)

    def test_a_faab_league_declines_rather_than_bidding_zero(self):
        """Neither live league uses FAAB, so there is no bid policy and none
        can be tested. Bidding zero would submit, lose, spend an acquisition,
        and appear in the league feed."""
        case = _Case(
            settings=settings(faab_budget=100),
            roster=[player(1, team=1), player(2, team=1)],
            pool=[player(90, name="Better", team=1)],
            values={1: 30.0, 2: 5.0, 90: 25.0},
        )
        plan = case.run()
        self.assertFalse(plan.proposed)
        self.assertIn("no bidding", " ".join(plan.notes))

    def test_a_player_already_on_our_roster_is_never_proposed(self):
        """ESPN's pool read can echo a player we hold; preflight refuses the
        add outright, so it must not be proposed."""
        case = _Case(
            roster=[player(1, team=1), player(2, team=1)],
            pool=[player(1, team=1)],
            values={1: 30.0, 2: 5.0},
        )
        self.assertFalse(case.run().proposed)

    def test_the_rationale_names_every_number_that_moved_the_decision(self):
        """An approval the user cannot audit is not really an approval, so both
        names, both per-game values and the cap price all have to appear."""
        case = _Case(
            roster=[player(1, team=1), player(2, name="Weak", team=1),
                    player(3, team=1)],
            pool=[player(90, name="Better", team=1)],
            values={1: 60.0, 2: 5.0, 3: 30.0, 90: 25.0},
        )
        plan = case.run(starters=[1], lam=2.5)
        self.assertEqual(plan.drop.name, "Weak")
        for expected in ("Better", "Weak", "25.0", "5.0", "2.50"):
            self.assertIn(expected, plan.rationale)

    def test_a_week_with_no_days_left_proposes_nothing(self):
        case = _Case(
            horizon=[],
            roster=[player(1, team=1)],
            pool=[player(90, name="Great", team=1)],
            values={1: 1.0, 90: 60.0},
        )
        plan = case.run()
        self.assertFalse(plan.proposed)
        self.assertIn("no scoring periods left", " ".join(plan.notes))

    def test_an_empty_pool_proposes_nothing(self):
        plan = _Case(roster=[player(1)], values={1: 1.0}).run()
        self.assertFalse(plan.proposed)
        self.assertIn("no available players", " ".join(plan.notes))

    def test_only_one_swap_is_ever_returned(self):
        """The acquisition budget is five to seven for a whole week, so a layer
        that proposed its best five ideas at once would spend the week on one
        run's view of the world."""
        case = _Case(
            roster=[player(i, team=1) for i in range(1, 4)],
            pool=[player(90 + i, name="Pool%d" % i, team=1) for i in range(3)],
            values={1: 30.0, 2: 4.0, 3: 5.0,
                    90: 29.0, 91: 28.0, 92: 27.0},
        )
        plan = case.run(starters=[1])
        self.assertTrue(plan.proposed)
        # Best add against cheapest drop, and exactly one of each.
        self.assertEqual(plan.add.player_id, 90)
        self.assertEqual(plan.drop.player_id, 2)


class TestCapInteraction(unittest.TestCase):
    def test_the_gp_cap_shape_is_the_one_budget_py_prices(self):
        """Sanity-check that the fixture used by the lineup tests still builds
        a cap this module's ``lam`` could have come from."""
        st = settings(lineup_slot_stat_limits={
            ids.TEAM_TOTAL_LIMIT_SLOT: StatLimit(
                slot_id=ids.TEAM_TOTAL_LIMIT_SLOT,
                stat_id=ids.GP_STAT,
                limit_value=40.0 / ids.SCORING_PERIODS_PER_MATCHUP,
            ),
        })
        self.assertTrue(st.has_gp_cap)


if __name__ == "__main__":
    unittest.main()
