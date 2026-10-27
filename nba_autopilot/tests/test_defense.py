"""Opponent-defence tests: the parse, the fit, and the multiplier.

The fit is checked against *properties* rather than golden numbers -- that a
bigger sample moves further from the prior, that shrinkage is monotone, that a
cell nobody has played resolves to exactly neutral. A golden number here would
only prove the fit agrees with whoever ran it first, and every constant in
``sources/defense.py`` was measured rather than chosen, so the numbers are
expected to move when the archive grows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from datetime import timedelta

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

from espn import ids  # noqa: E402
from sources import defense  # noqa: E402
from state import Store  # noqa: E402


def _sched_payload(teams_games):
    """``{team: [(period, home, away)]}`` -> a ``proTeamSchedules_wl`` body."""
    return {"settings": {"proTeams": [
        {"id": tid, "proGamesByScoringPeriod": {
            str(p): [{"homeProTeamId": h, "awayProTeamId": a}]
            for p, h, a in games}}
        for tid, games in teams_games.items()]}}


def _player(pid, *, centre=False, rows=()):
    """rows: ``(season, period, pro_team, {statId: value})``."""
    return {
        "id": pid,
        "eligibleSlots": [ids.CENTER_SLOT] if centre else [0, 11],
        "stats": [
            {"statSourceId": 0, "statSplitTypeId": 5, "seasonId": s,
             "scoringPeriodId": p, "proTeamId": t,
             "stats": {str(k): v for k, v in st.items()}}
            for s, p, t, st in rows
        ],
    }


COEF = {ids.PTS: 1.0, ids.OREB: 1.5, ids.DREB: 1.0}


class TestParsingTheSchedule(unittest.TestCase):
    def test_the_season_comes_from_the_caller_not_the_payload(self):
        """ESPN does not put ``seasonId`` anywhere in this response.

        Verified 2026-10-26 against ``seasons/2027?view=proTeamSchedules_wl``,
        whose only top-level keys are ``display`` and ``settings``. An earlier
        version read it out of the payload, got ``None`` for every game, and
        produced an empty schedule -- which then silently produced an empty
        fit, which looks exactly like "not enough data yet".
        """
        out = defense.parse_schedule(_sched_payload({1: [(5, 1, 2)]}), 2027)
        self.assertEqual(out, {(2027, 5, 1): 2})

    def test_both_sides_of_a_game_are_indexed(self):
        payload = _sched_payload({1: [(5, 1, 2)], 2: [(5, 1, 2)]})
        out = defense.parse_schedule(payload, 2027)
        self.assertEqual(out[(2027, 5, 1)], 2)
        self.assertEqual(out[(2027, 5, 2)], 1)

    def test_two_seasons_do_not_collide(self):
        """The whole reason the key carries a season.

        ``seasons/2027/players`` ships last season's game rows alongside this
        season's. Resolving those against this season's calendar is how 13,128
        games got the wrong opponent the first time this was written.
        """
        a = defense.parse_schedule(_sched_payload({1: [(5, 1, 2)]}), 2026)
        b = defense.parse_schedule(_sched_payload({1: [(5, 1, 9)]}), 2027)
        both = dict(a); both.update(b)
        self.assertEqual(both[(2026, 5, 1)], 2)
        self.assertEqual(both[(2027, 5, 1)], 9)

    def test_a_malformed_game_is_skipped_not_fatal(self):
        payload = {"settings": {"proTeams": [
            {"id": 1, "proGamesByScoringPeriod": {
                "5": [None, {}, {"homeProTeamId": 1}, {"awayProTeamId": 2},
                      {"homeProTeamId": 1, "awayProTeamId": 2}]}}]}}
        self.assertEqual(defense.parse_schedule(payload, 2027),
                         {(2027, 5, 1): 2})

    def test_no_teams_is_an_empty_schedule(self):
        for payload in ({}, None, {"settings": {}}, {"settings": {"proTeams": []}}):
            self.assertEqual(defense.parse_schedule(payload, 2027), {})


class TestParsingPlayerGames(unittest.TestCase):
    def setUp(self):
        self.sched = defense.parse_schedule(
            _sched_payload({1: [(5, 1, 2)], 2: [(5, 1, 2)]}), 2027)

    def test_a_game_is_scored_with_the_league_coefficients(self):
        payload = [_player(7, rows=[(2027, 5, 1, {ids.PTS: 20, ids.OREB: 2,
                                                  ids.DREB: 6})])]
        games = defense.parse_player_games(payload, self.sched, COEF)
        self.assertEqual(len(games), 1)
        self.assertAlmostEqual(games[0].points, 20 + 1.5 * 2 + 1.0 * 6)
        self.assertEqual(games[0].opponent, 2)

    def test_projection_rows_are_not_actuals(self):
        """``statSourceId`` 1 is ESPN's projection. Fitting a defence model on
        projections would measure ESPN's opinion of the opponent, not the
        opponent."""
        payload = [{"id": 7, "eligibleSlots": [], "stats": [
            {"statSourceId": 1, "statSplitTypeId": 5, "seasonId": 2027,
             "scoringPeriodId": 5, "proTeamId": 1, "stats": {"0": 99}}]}]
        self.assertEqual(defense.parse_player_games(payload, self.sched, COEF), [])

    def test_season_total_rows_are_not_per_game_rows(self):
        payload = [{"id": 7, "eligibleSlots": [], "stats": [
            {"statSourceId": 0, "statSplitTypeId": 0, "seasonId": 2027,
             "scoringPeriodId": 5, "proTeamId": 1, "stats": {"0": 99}}]}]
        self.assertEqual(defense.parse_player_games(payload, self.sched, COEF), [])

    def test_the_team_on_the_row_wins_not_the_player_record(self):
        """A traded player's games belong to the team he played them for.

        The day row carries its own ``proTeamId``; the player record carries
        only where he ended up. Using the latter would attribute a whole
        season of games to the wrong side of a February trade.
        """
        payload = [_player(7, rows=[(2027, 5, 2, {ids.PTS: 10})])]
        games = defense.parse_player_games(payload, self.sched, COEF)
        self.assertEqual(games[0].pro_team, 2)
        self.assertEqual(games[0].opponent, 1)

    def test_an_unresolvable_opponent_is_dropped(self):
        """Better absent than attributed to a guess."""
        payload = [_player(7, rows=[(2027, 99, 1, {ids.PTS: 10})])]
        self.assertEqual(defense.parse_player_games(payload, self.sched, COEF), [])

    def test_centre_eligibility_comes_from_the_c_slot(self):
        payload = [_player(1, centre=True, rows=[(2027, 5, 1, {ids.PTS: 10})]),
                   _player(2, centre=False, rows=[(2027, 5, 1, {ids.PTS: 10})])]
        games = defense.parse_player_games(payload, self.sched, COEF)
        self.assertEqual({g.player_id: g.centre_eligible for g in games},
                         {1: True, 2: False})

    def test_an_empty_stat_dict_is_a_game_not_played(self):
        """ESPN emits a per-period row with no stats for a scheduled game the
        player did not appear in. Counting it as a zero would read as the
        opponent having shut him out."""
        payload = [{"id": 7, "eligibleSlots": [], "stats": [
            {"statSourceId": 0, "statSplitTypeId": 5, "seasonId": 2027,
             "scoringPeriodId": 5, "proTeamId": 1, "stats": {}}]}]
        self.assertEqual(defense.parse_player_games(payload, self.sched, COEF), [])

    def test_minutes_are_carried_through(self):
        payload = [_player(7, rows=[(2027, 5, 1, {ids.PTS: 10,
                                                  ids.MINUTES: 31.5})])]
        games = defense.parse_player_games(payload, self.sched, COEF)
        self.assertAlmostEqual(games[0].minutes, 31.5)


def _games(opponent, n, points, *, centre=False, season=2027, base=None,
           player_offset=0):
    """``n`` player-games against ``opponent``, each player also playing enough
    other games to clear ``MIN_PLAYER_GAMES`` against a neutral opponent."""
    out = []
    per = defense.MIN_PLAYER_GAMES + 1
    for i in range(n):
        pid = 1000 + player_offset + i
        out.append(defense.PlayerGame(
            season=season, player_id=pid, scoring_period=1, pro_team=1,
            opponent=opponent, centre_eligible=centre, minutes=30.0,
            points=points))
        for j in range(per - 1):
            out.append(defense.PlayerGame(
                season=season, player_id=pid, scoring_period=2 + j,
                pro_team=1, opponent=99, centre_eligible=centre,
                minutes=30.0, points=base if base is not None else 30.0))
    return out


class TestFittingTheTable(unittest.TestCase):
    def test_too_little_data_publishes_nothing(self):
        """An empty table is a correct answer. A table fitted on forty games
        is a wrong one that is indistinguishable from a right one."""
        table = defense.fit_defense(_games(2, 5, 40.0))
        self.assertTrue(table.empty)
        self.assertEqual(table.multiplier(2, False), 1.0)

    def test_a_player_below_the_game_threshold_contributes_nothing(self):
        few = [defense.PlayerGame(season=2027, player_id=1, scoring_period=p,
                                  pro_team=1, opponent=2, centre_eligible=False,
                                  minutes=30.0, points=50.0)
               for p in range(defense.MIN_PLAYER_GAMES - 1)]
        self.assertTrue(defense.fit_defense(few).empty)

    def test_a_tiny_baseline_is_not_a_denominator(self):
        """Ratios against 2 points a game are noise amplified a thousandfold."""
        rows = _games(2, 200, 4.0, base=2.0)
        table = defense.fit_defense(rows)
        self.assertEqual(table.multiplier(2, False), 1.0)

    def test_a_suppressing_opponent_gets_a_multiplier_below_one(self):
        rows = _games(2, 400, 21.0, base=30.0) + _games(
            3, 400, 30.0, base=30.0, player_offset=5000)
        table = defense.fit_defense(rows)
        self.assertLess(table.team[2], 1.0)
        self.assertGreater(table.team[2], 0.5)

    def test_shrinkage_is_monotone_in_sample_size(self):
        """More games against the same opponent must move further from neutral,
        never less far. This is the property the two prior constants exist to
        provide and the one a careless edit would break."""
        far = None
        for n in (200, 400, 800, 1600):
            rows = _games(2, n, 20.0, base=30.0) + _games(
                3, 400, 30.0, base=30.0, player_offset=90000)
            m = defense.fit_defense(rows).team[2]
            if far is not None:
                self.assertLessEqual(m, far + 1e-9)
            far = m

    def test_an_unplayed_opponent_is_exactly_neutral(self):
        table = defense.fit_defense(_games(2, 400, 20.0, base=30.0))
        self.assertEqual(table.multiplier(77, False), 1.0)
        self.assertEqual(table.multiplier(77, True), 1.0)

    def test_the_centre_adjustment_is_separate_from_the_team_effect(self):
        """A team that suppresses everyone equally must get a team multiplier
        and a centre adjustment near zero -- otherwise the centre term is
        double-counting the overall effect."""
        rows = (_games(2, 400, 21.0, base=30.0, centre=False)
                + _games(2, 400, 21.0, base=30.0, centre=True,
                         player_offset=20000)
                + _games(3, 400, 30.0, base=30.0, player_offset=40000))
        table = defense.fit_defense(rows)
        self.assertLess(table.team[2], 1.0)
        self.assertAlmostEqual(table.centre.get(2, 0.0), 0.0, places=1)

    def test_a_rim_protector_shows_up_only_on_centres(self):
        rows = (_games(2, 600, 30.0, base=30.0, centre=False)
                + _games(2, 600, 21.0, base=30.0, centre=True,
                         player_offset=20000)
                + _games(3, 600, 30.0, base=30.0, player_offset=40000))
        table = defense.fit_defense(rows)
        self.assertLess(table.centre[2], 0.0)
        self.assertLess(table.multiplier(2, True), table.multiplier(2, False))

    def test_seasons_are_folded_oldest_first(self):
        table = defense.fit_defense(
            _games(2, 400, 20.0, base=30.0, season=2026)
            + _games(2, 400, 20.0, base=30.0, season=2027, player_offset=7000))
        self.assertEqual(table.seasons, (2026, 2027))


class TestTheMultiplier(unittest.TestCase):
    def setUp(self):
        self.table = defense.TeamDefense(
            team={2: 0.90, 3: 1.10}, centre={2: -0.05, 3: 0.04},
            seasons=(2027,), games=50000)

    def test_no_opponent_means_no_adjustment(self):
        """A player whose game could not be resolved must not be moved in
        either direction."""
        self.assertEqual(self.table.multiplier(None, True), 1.0)
        self.assertEqual(self.table.multiplier(None, False), 1.0)

    def test_an_unknown_team_means_no_adjustment(self):
        self.assertEqual(self.table.multiplier(404, True), 1.0)

    def test_the_centre_term_compounds_on_the_team_term(self):
        self.assertAlmostEqual(self.table.multiplier(2, False), 0.90)
        self.assertAlmostEqual(self.table.multiplier(2, True), 0.90 * 0.95)
        self.assertAlmostEqual(self.table.multiplier(3, True), 1.10 * 1.04)

    def test_a_non_centre_never_sees_the_centre_term(self):
        self.assertAlmostEqual(self.table.multiplier(3, False), 1.10)

    def test_the_clamp_holds_against_a_payload_that_changed_shape(self):
        wild = defense.TeamDefense(team={2: 4.0, 3: 0.0}, centre={2: 3.0})
        self.assertEqual(wild.multiplier(2, True), 1.20)
        self.assertEqual(wild.multiplier(3, False), 0.80)

    def test_an_empty_table_is_neutral_for_everyone(self):
        empty = defense.TeamDefense()
        self.assertTrue(empty.empty)
        for team in (1, 2, 30, None):
            self.assertEqual(empty.multiplier(team, True), 1.0)


class TestTheCachedTable(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "_defense_test.db")
        if os.path.exists(self.path):
            os.remove(self.path)
        self.store = Store(self.path)

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_a_round_trip_keeps_integer_team_ids(self):
        """JSON object keys are strings. A table whose keys come back as
        ``"2"`` instead of ``2`` misses every lookup and returns 1.0 for
        everyone -- which is indistinguishable from having no table at all."""
        self.store.save_defense("abc", team={2: 0.9}, centre={2: -0.05},
                                seasons=(2026, 2027), games=1234)
        got = self.store.load_defense("abc", timedelta(days=7))
        self.assertEqual(got["team"], {2: 0.9})
        self.assertEqual(got["centre"], {2: -0.05})
        self.assertEqual(got["seasons"], (2026, 2027))
        self.assertEqual(got["games"], 1234)
        self.assertIsInstance(next(iter(got["team"])), int)

    def test_a_missing_scope_is_none(self):
        self.assertIsNone(self.store.load_defense("nope", timedelta(days=7)))

    def test_an_expired_table_is_none(self):
        self.store.save_defense("abc", team={2: 0.9}, centre={}, seasons=(2027,),
                                games=9)
        self.assertIsNone(self.store.load_defense("abc", timedelta(seconds=-1)))

    def test_a_second_fit_replaces_the_first(self):
        self.store.save_defense("abc", team={2: 0.9}, centre={}, seasons=(2027,),
                                games=1)
        self.store.save_defense("abc", team={2: 1.1}, centre={}, seasons=(2027,),
                                games=2)
        got = self.store.load_defense("abc", timedelta(days=7))
        self.assertEqual(got["team"], {2: 1.1})
        self.assertEqual(got["games"], 2)

    def test_two_scopes_do_not_share(self):
        self.store.save_defense("a", team={2: 0.9}, centre={}, seasons=(), games=1)
        self.store.save_defense("b", team={2: 1.1}, centre={}, seasons=(), games=1)
        self.assertEqual(
            self.store.load_defense("a", timedelta(days=7))["team"], {2: 0.9})

    def test_an_unreadable_row_is_none_not_an_exception(self):
        """A corrupt cache must cost the adjustment, not the run."""
        self.store.save_defense("abc", team={2: 0.9}, centre={}, seasons=(),
                                games=1)
        self.store.db.execute(
            "UPDATE defense SET table_json='{not json' WHERE scope='abc'")
        self.store.db.commit()
        self.assertIsNone(self.store.load_defense("abc", timedelta(days=7)))


if __name__ == "__main__":
    unittest.main()


class TestTheRunJoinsItToTheRoster(unittest.TestCase):
    """``Autopilot._matchup_multipliers``: schedule x roster x fitted table.

    The join is where this feature fails *silently* if it fails at all -- a
    wrong scoring-period key or a renamed attribute returns an empty mapping,
    every multiplier becomes 1.0, and the projections look exactly as they
    did before. Nothing downstream can tell that apart from "no fitted table
    yet", so it is checked here rather than trusted.
    """

    def setUp(self):
        import main as main_mod
        from models import ProGame, RosterEntry

        self.app = object.__new__(main_mod.Autopilot)
        self.app._defense = {}
        self.table = defense.TeamDefense(
            team={20: 0.90, 30: 1.10}, centre={20: -0.05, 30: 0.04},
            seasons=(2027,), games=50000)
        self.settings = type("S", (), {
            "current_scoring_period": 7, "season_id": 2027,
            "scoring_items": {ids.PTS: 1.0}, "league_id": 1})()
        # Team 1 plays team 20 today; team 2 plays team 30; team 3 is idle.
        self.app._pro_games = {7: {
            1: ProGame(scoring_period=7, pro_team_id=1, opponent_id=20,
                       home=True),
            2: ProGame(scoring_period=7, pro_team_id=2, opponent_id=30,
                       home=False),
        }}
        self.app._defense[self.app._defense_scope(self.settings)] = self.table

        def entry(pid, team, centre):
            return RosterEntry(
                player_id=pid, name="p%d" % pid, current_slot=ids.BENCH_SLOT,
                eligible_slots=frozenset(
                    {ids.CENTER_SLOT, 11} if centre else {0, 11}),
                pro_team_id=team, injury_status="ACTIVE")

        self.roster = [entry(1, 1, False), entry(2, 1, True),
                       entry(3, 2, False), entry(4, 3, False)]

    def test_each_player_gets_his_own_opponent(self):
        out = self.app._matchup_multipliers(None, self.settings, self.roster)
        self.assertAlmostEqual(out[1], 0.90)
        self.assertAlmostEqual(out[3], 1.10)

    def test_a_centre_gets_the_centre_term_and_a_guard_does_not(self):
        out = self.app._matchup_multipliers(None, self.settings, self.roster)
        self.assertAlmostEqual(out[1], 0.90)
        self.assertAlmostEqual(out[2], 0.90 * 0.95)

    def test_a_player_with_no_game_today_is_absent(self):
        """Absent, not 1.0-valued. He is benched by the eligibility pass
        regardless, and inventing a multiplier for a game nobody is playing
        would be a number with nothing behind it."""
        out = self.app._matchup_multipliers(None, self.settings, self.roster)
        self.assertNotIn(4, out)

    def test_the_scoring_period_is_the_one_being_planned(self):
        """Keyed on ``current_scoring_period``. Reading yesterday's slate would
        price every player against the wrong team, which is worse than no
        adjustment and just as quiet."""
        self.settings.current_scoring_period = 8
        self.assertEqual(
            self.app._matchup_multipliers(None, self.settings, self.roster), {})

    def test_an_empty_table_adjusts_nobody(self):
        self.app._defense = {
            self.app._defense_scope(self.settings): defense.TeamDefense()}
        self.assertEqual(
            self.app._matchup_multipliers(None, self.settings, self.roster), {})

    def test_an_instance_without_a_cache_never_fetches(self):
        """Every test in tests/ builds ``Autopilot`` without ``__init__``. The
        correct reading of "no cache configured" is *do not fetch 25 MB from a
        unit test*, so such an instance gets no adjustment by design."""
        bare = object.__new__(type(self.app))
        table = bare.team_defense(None, self.settings)
        self.assertTrue(table.empty)
        self.assertEqual(
            bare._matchup_multipliers(None, self.settings, self.roster), {})

    def test_leagues_scoring_alike_share_one_scope(self):
        """The fit is a function of the coefficients and nothing else, so two
        leagues that score the same way must not fetch twice."""
        a = type("S", (), {"scoring_items": {ids.PTS: 1.0, ids.DD: 4.0}})()
        b = type("S", (), {"scoring_items": {ids.DD: 4.0, ids.PTS: 1.0}})()
        c = type("S", (), {"scoring_items": {ids.PTS: 1.0, ids.DD: 5.0}})()
        self.assertEqual(self.app._defense_scope(a), self.app._defense_scope(b))
        self.assertNotEqual(self.app._defense_scope(a), self.app._defense_scope(c))
