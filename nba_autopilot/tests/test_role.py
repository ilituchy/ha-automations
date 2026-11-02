"""Role change and team-mate absence: the two minutes signals.

The requirement, in the user's own words:

    "is there anyway we can consider 'this man has scored 50 points two games
    in a row' that theres value. especially if we see playing time jump from 12
    mins to 30 mins, that could indicate another player is injured, and worth
    further investigation. often players average 20 but then someone is hurt
    and they get more minutes, which almost usually increases the score"

and the second half of it, which turned out to need a different mechanism:

    "afernee simmons had a huge increase in minutes because lebron james and
    joel embiid (his teammates) were both resting. is there anyway we can
    understand who is active for a given team so we can anticipate an increase
    in minutes?"

Two signals, deliberately kept apart. ``solve/role.py`` reads a change that has
already happened and *reweights the projection*. ``solve/absence.py`` reads one
that is about to and only *reports* it, because the share of a vacated rotation
that lands on any given man could not be measured -- see that module.

The test worth reading is
:meth:`TestTheGuards.test_an_injury_truncated_window_is_not_a_demotion`, which
is the one that cost the most accuracy when it was missing.
"""

from __future__ import annotations

import os
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

from espn import ids  # noqa: E402
from models import ProPlayer, RosterEntry  # noqa: E402
from solve import absence as absence_mod  # noqa: E402
from solve import projections as projections_mod  # noqa: E402
from solve import role as role_mod  # noqa: E402

M, GP = ids.MINUTES, ids.GP_STAT


def splits(*, projected=30.0, last15=None, last15_games=5.0,
           last7=None, last7_games=3.0, pts=20.0):
    """Only the fields the role read looks at, plus a stat to blend."""
    out = {ids.SPLIT_SEASON_PROJECTED: {M: projected, GP: 70.0, ids.PTS: pts}}
    if last15 is not None:
        out[ids.SPLIT_LAST_15] = {M: last15, GP: last15_games, ids.PTS: pts}
    if last7 is not None:
        out[ids.SPLIT_LAST_7] = {M: last7, GP: last7_games, ids.PTS: pts}
    return out


class TestTheOrdinaryCase(unittest.TestCase):
    """Nothing happens, loudly enough to be sure nothing happened."""

    def test_a_stable_role_changes_nothing_at_all(self):
        r = role_mod.read_role(splits(projected=30.0, last15=31.0))
        self.assertFalse(r.changed)
        self.assertEqual(r.trust, 0.0)
        self.assertEqual(r.adjust(0.62), 0.62)
        self.assertEqual(r.split, ids.SPLIT_LAST_15)
        self.assertEqual(r.describe(), "")

    def test_a_wobble_inside_the_deadband_is_a_wobble(self):
        """Measured: players inside it were already projected almost exactly
        right, so there is no error here and moving them only adds noise."""
        for recent in (27.5, 30.0, 32.9):
            r = role_mod.read_role(splits(projected=30.0, last15=recent))
            self.assertFalse(r.changed, recent)
            self.assertEqual(r.adjust(0.70), 0.70)

    def test_no_projected_minutes_is_silence_not_a_demotion(self):
        """A man with no projection has no role to have diverged from.

        The failure to avoid is reading a missing field as zero, which makes
        every unprojected player look like he has lost all his minutes.
        """
        s = splits(last15=28.0)
        s[ids.SPLIT_SEASON_PROJECTED].pop(M)
        r = role_mod.read_role(s)
        self.assertFalse(r.changed)
        self.assertEqual(r.divergence, 0.0)


class TestTheSignal(unittest.TestCase):
    def test_a_shrunken_role_lifts_the_weight_toward_the_window(self):
        """Nikola Jovic: projected 24.6 minutes, playing 13.2.

        The projection is not a noisy estimate of his current role; it is an
        accurate estimate of a role he no longer has.
        """
        r = role_mod.read_role(splits(projected=24.6, last15=13.2))
        self.assertTrue(r.changed)
        self.assertAlmostEqual(r.divergence, -11.4, places=1)
        self.assertGreater(r.trust, 0.9)
        self.assertGreater(r.adjust(0.67), 0.85)
        self.assertIn("shrunk", r.describe())

    def test_an_expanded_role_does_the_same_the_other_way(self):
        """Anthony Black: projected 22.8, playing 28.0 -- and the season split
        agrees with the recent one, so only the *projection* is stale. Measured
        against the season average this case is invisible."""
        r = role_mod.read_role(splits(projected=22.8, last15=28.0))
        self.assertTrue(r.changed)
        self.assertAlmostEqual(r.divergence, 5.2, places=1)
        self.assertGreater(r.adjust(0.62), 0.62)
        self.assertIn("expanded", r.describe())

    def test_the_lift_is_capped_and_never_lowers_a_weight(self):
        r = role_mod.read_role(splits(projected=30.0, last15=5.0))
        self.assertEqual(r.trust, 1.0)
        self.assertAlmostEqual(r.adjust(0.10), role_mod.WEIGHT_CEILING)
        # A sample already big enough to earn more than the ceiling keeps it.
        self.assertEqual(r.adjust(0.95), 0.95)

    def test_the_ramp_is_linear_past_the_deadband(self):
        half = role_mod.DEADBAND_MINUTES + (
            role_mod.FULL_SHIFT_MINUTES - role_mod.DEADBAND_MINUTES) / 2.0
        r = role_mod.read_role(splits(projected=30.0, last15=30.0 + half))
        self.assertAlmostEqual(r.trust, 0.5, places=6)

    def test_the_description_states_both_numbers_not_the_difference(self):
        """A reader checking a projection needs what it assumed and what is
        happening, not a delta he has to solve backwards."""
        said = role_mod.read_role(splits(projected=22.8, last15=28.0)).describe()
        self.assertIn("22.8", said)
        self.assertIn("28.0", said)
        self.assertIn("+5.2", said)
        # And in points, because minutes are not the unit anybody decides in
        # -- named as a league-average scaling rather than as a projection of
        # this player, which it is not: see ``RoleRead.points_implied``.
        self.assertIn("+7 fantasy points", said)
        self.assertIn("league-average", said)


class TestTheShorterWindow(unittest.TestCase):
    """Last-7 when the role is still moving, last-15 otherwise."""

    def test_a_still_moving_role_reads_the_shorter_window(self):
        """Ace Bailey: 24, 22, 29, 34, 40, 37 minutes. His last-15 agrees with
        his projection and his last-7 does not, which is the whole signal."""
        r = role_mod.read_role(
            splits(projected=30.7, last15=31.0, last7=37.0))
        self.assertEqual(r.split, ids.SPLIT_LAST_7)
        self.assertAlmostEqual(r.divergence, 6.3, places=1)
        self.assertIn("last 7 days", r.describe())

    def test_the_longer_window_wins_when_it_says_more(self):
        r = role_mod.read_role(
            splits(projected=30.0, last15=18.0, last7=28.0))
        self.assertEqual(r.split, ids.SPLIT_LAST_15)
        self.assertAlmostEqual(r.divergence, -12.0, places=1)

    def test_the_switch_is_gated_by_the_deadband(self):
        """Ungated this cut mean error and *raised* RMSE -- a worse estimate
        dressed up as a fresher one. Last-7 is one to three games."""
        r = role_mod.read_role(
            splits(projected=30.0, last15=30.5, last7=32.0))
        self.assertEqual(r.split, ids.SPLIT_LAST_15)
        self.assertFalse(r.changed)


class TestTheGuards(unittest.TestCase):
    def test_an_injury_truncated_window_is_not_a_demotion(self):
        """Jaren Jackson Jr: a single ten-minute game he left injured.

        Read as a role change it moved his value from 34.3 to 16.0 against an
        actual 30.7 -- worse by itself than the rule was better everywhere
        else, and the reason :data:`role.MIN_WINDOW_GAMES` exists.
        """
        r = role_mod.read_role(
            splits(projected=31.2, last15=26.0, last15_games=4.0,
                   last7=10.0, last7_games=1.0))
        # The one-game window is ignored; the four-game one still counts.
        self.assertEqual(r.split, ids.SPLIT_LAST_15)
        self.assertAlmostEqual(r.divergence, -5.2, places=1)

    def test_a_thin_long_window_says_nothing_either(self):
        r = role_mod.read_role(
            splits(projected=31.0, last15=10.0, last15_games=2.0))
        self.assertFalse(r.changed)
        self.assertEqual(r.divergence, 0.0)

    def test_a_hurt_player_is_p_plays_business_not_the_projections(self):
        """An injured man's minutes collapse is an availability fact. Folding
        it in here would discount him twice and call the second one a demotion.
        """
        s = splits(projected=31.2, last15=10.0)
        self.assertTrue(role_mod.read_role(s, active=True).changed)
        self.assertFalse(role_mod.read_role(s, active=False).changed)


class TestItReachesTheProjection(unittest.TestCase):
    """The blend is where this has to land or it is decoration."""

    def test_the_weight_rises_and_the_mean_moves_with_it(self):
        s = splits(projected=24.6, last15=13.2, pts=20.0)
        s[ids.SPLIT_LAST_15][ids.PTS] = 5.0
        plain = projections_mod.per_game_mean(s)
        role = role_mod.read_role(s)
        shifted = projections_mod.per_game_mean(s, role=role)
        self.assertLess(shifted[ids.PTS], plain[ids.PTS])
        # And toward the window, not past it.
        self.assertGreaterEqual(shifted[ids.PTS], 5.0)

    def test_an_unchanged_role_leaves_the_blend_exactly_where_it_was(self):
        """The regression guard. Every existing projection must be untouched."""
        s = splits(projected=30.0, last15=31.0, pts=20.0)
        s[ids.SPLIT_LAST_15][ids.PTS] = 25.0
        self.assertEqual(
            projections_mod.per_game_mean(s),
            projections_mod.per_game_mean(s, role=role_mod.read_role(s)),
        )

    def test_recent_form_reports_the_window_it_actually_read(self):
        s = splits(projected=30.7, last15=31.0, last7=37.0, pts=15.0)
        s[ids.SPLIT_LAST_7][ids.PTS] = 19.0
        s[ids.SPLIT_LAST_7][GP] = 3.0
        form, games, _w = projections_mod.recent_form(
            s, role=role_mod.read_role(s))
        self.assertEqual(form[ids.PTS], 19.0)
        self.assertEqual(games, 3.0)


# ---------------------------------------------------------------------------
# who else is missing tonight
# ---------------------------------------------------------------------------


def mate(pid, name, minutes, status="ACTIVE", team=20):
    return ProPlayer(player_id=pid, name=name, pro_team_id=team,
                     injury_status=status, minutes=minutes)


def ours(pid=1, name="Simons", team=20):
    return RosterEntry(
        player_id=pid, name=name, current_slot=11,
        eligible_slots=frozenset((11, 12)), pro_team_id=team,
        injury_status="ACTIVE",
    )


class TestTeammateAbsence(unittest.TestCase):
    """The signal the backward-looking rule structurally cannot see.

    Anfernee Simons' recent minutes are *below* his projection, so
    ``solve/role.py`` shrinks him -- while his actual production runs far above
    both, because the men ahead of him have been sitting. Reported rather than
    priced: the share of a vacated rotation that lands on one man came out at
    +0.014 over 246 team-days, which is a measurement failure rather than an
    absence of effect, and inventing the coefficient anyway is how a model
    acquires a term nobody can defend.
    """

    ROTATION = [
        mate(10, "Tyrese Maxey", 37.7),
        mate(11, "Jaylen Brown", 36.7),
        mate(12, "Joel Embiid", 35.0, "DAY_TO_DAY"),
        mate(13, "LeBron James", 33.4, "OUT"),
        mate(1, "Simons", 24.6),
        mate(14, "Adem Bona", 14.1),
        mate(15, "Deep Reserve", 4.0, "OUT"),
    ]

    def read(self, roster=None, rotation=None):
        return absence_mod.read_absences(
            roster or [ours()], {20: rotation if rotation is not None
                                 else self.ROTATION})

    def test_the_minutes_are_split_by_whether_he_is_ruled_out(self):
        """Out and doubtful are different facts, and summing them hides which
        one the reader is looking at -- a doubtful man may simply play."""
        r = self.read()[1]
        self.assertAlmostEqual(r.out_minutes, 33.4, places=1)
        self.assertAlmostEqual(r.doubtful_minutes, 35.0, places=1)
        self.assertAlmostEqual(r.own_minutes, 24.6, places=1)
        self.assertTrue(r.material)

    def test_a_fifteenth_man_is_not_a_vacated_rotation(self):
        """14 and 4 minutes a night are below MIN_ROTATION_MINUTES, so neither
        appears even though ESPN has one of them out."""
        names = [a[0] for a in self.read()[1].absent]
        self.assertIn("LeBron James", names)
        self.assertNotIn("Deep Reserve", names)

    def test_he_is_never_his_own_opportunity(self):
        """He is in the same list, and if *he* is the man listed out then his
        own minutes would read as a rotation opening up for him."""
        rot = [mate(1, "Simons", 24.6, "OUT"), mate(10, "Maxey", 37.7)]
        r = self.read(rotation=rot)[1]
        self.assertEqual(r.out_minutes, 0.0)
        self.assertEqual(r.absent, ())

    def test_nothing_missing_says_nothing(self):
        rot = [mate(10, "Maxey", 37.7), mate(1, "Simons", 24.6)]
        r = self.read(rotation=rot)[1]
        self.assertFalse(r.material)
        self.assertEqual(r.describe(), "")

    def test_the_description_names_the_biggest_absences_and_counts_the_rest(self):
        r = self.read()[1]
        said = r.describe()
        self.assertIn("Joel Embiid", said)
        self.assertIn("LeBron James", said)
        self.assertIn("doubtful", said)
        self.assertIn("out", said)
        # His own size, so the absence can be read against the man absorbing it.
        self.assertIn("24.6", said)

    def test_it_states_minutes_and_draws_no_conclusion(self):
        """The share could not be measured, so the sentence must not imply one.

        Specifically: no claim that he will play more or score more. That
        inference is the reviewer's, which can reason about position.
        """
        said = self.read()[1].describe()
        for forbidden in ("will play", "expect", "more points", "likely"):
            self.assertNotIn(forbidden, said.lower())

    def test_a_team_we_could_not_read_is_an_empty_read_not_a_missing_key(self):
        """Callers write this onto a card; a missing key makes each of them
        decide separately what absence of data means."""
        got = absence_mod.read_absences([ours(team=99)], {20: self.ROTATION})
        self.assertIn(1, got)
        self.assertFalse(got[1].material)


class TestTheLineupRationaleNamesTheRightWindow(unittest.TestCase):
    """It said "last-15" unconditionally, which stopped being true.

    A rationale that reports the wrong window is worse than one that reports
    none: a reader checking the number against ESPN's own card finds a
    different one and concludes the model is broken.
    """

    def bit(self, *, split, changed_from=None):
        import dataclasses
        from datetime import datetime, timezone

        from models import Projection
        from solve import value as value_mod
        from models import LeagueSettings

        st = LeagueSettings(
            league_id=1, season_id=2027, name="t", scoring_type="H2H_POINTS",
            lineup_slot_counts={11: 1}, lineup_slot_stat_limits=None,
            scoring_items={ids.PTS: 1.0}, categories=frozenset(),
            waiver_mode="WAIVERS_TRADITIONAL", faab_budget=None,
            roster_lock_mode="INDIVIDUAL_GAME", current_scoring_period=1,
            current_matchup_period=1, matchup_period_boundaries={},
        )
        role = None
        if changed_from is not None:
            role = role_mod.RoleRead(
                split=split, divergence=changed_from,
                projected_minutes=30.0, recent_minutes=30.0 + changed_from,
                games=3.0,
            )
        proj = Projection(
            player_id=1, mean={ids.PTS: 20.0}, var={ids.PTS: 20.0},
            p_play=1.0, minutes=30.0, source="t",
            as_of=datetime(2026, 11, 2, tzinfo=timezone.utc),
            form_mean={ids.PTS: 25.0}, form_games=3.0, form_weight=0.55,
            role=role,
        )
        import pipeline
        return pipeline._form_bit(proj, value_mod.build_value_model(st))

    def test_the_default_window_is_still_named_last_15(self):
        self.assertIn("last-15", self.bit(split=ids.SPLIT_LAST_15))

    def test_a_shorter_window_is_named_last_7(self):
        said = self.bit(split=ids.SPLIT_LAST_7, changed_from=7.0)
        self.assertIn("last-7", said)
        self.assertNotIn("last-15", said)

    def test_the_role_change_is_appended_as_the_reason_for_the_weight(self):
        said = self.bit(split=ids.SPLIT_LAST_7, changed_from=7.0)
        self.assertIn("role expanded", said)
        self.assertIn("37.0", said)

    def test_an_unchanged_role_appends_nothing(self):
        said = self.bit(split=ids.SPLIT_LAST_15)
        self.assertNotIn("role", said.lower())
