"""A game in progress, on a roster row.

*"if they are in game, then we def need to be able to see the live minutes
quarter and score since we have room"*.

Three facts from two places. The minutes come off the fantasy roster row that
was already being read for the day's points, so they cost nothing. The quarter
and the score come off ESPN's public NBA scoreboard, which is a different host
with a different idea of what a team is called -- it says ``NY`` and ``SA``
where the fantasy API says ``NYK`` and ``SAS`` -- so the join is on the team
*id*, which the two do agree on, and never on the abbreviation.

The quiet failure here is a clock that is merely old. A scoreboard that cannot
be read, or a game on a day that is not today, has to come back as "no live
game" and leave the tipoff showing, rather than as a stopped clock.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rootfs", "app"))

import web  # noqa: E402
from espn import live  # noqa: E402
from espn.read import _day_row  # noqa: E402
from models import LiveGame  # noqa: E402


def _event(state="in", period=2, clock="1:16", detail="1:16 - 2nd",
           home=(18, "NY", "20"), away=(8, "DET", "25")):
    return {
        "competitions": [{
            "status": {"period": period, "displayClock": clock,
                       "type": {"state": state, "shortDetail": detail}},
            "competitors": [
                {"team": {"id": h, "abbreviation": a}, "score": s}
                for h, a, s in (home, away)
            ],
        }],
    }


class TestReadingTheScoreboard(unittest.TestCase):

    def test_both_sides_are_indexed_under_their_own_team_id(self):
        games = live.parse({"events": [_event()]})
        self.assertEqual({8, 18}, set(games))

    def test_each_side_sees_its_own_score_first(self):
        games = live.parse({"events": [_event()]})
        self.assertEqual((25, 20), (games[8].score, games[8].opp_score))
        self.assertEqual((20, 25), (games[18].score, games[18].opp_score))

    def test_the_join_is_the_id_and_never_the_abbreviation(self):
        """The scoreboard says ``NY``; the fantasy API says ``NYK``. Matching
        on the string would silently find nothing for six teams."""
        games = live.parse({"events": [_event()]})
        self.assertIn(18, games)
        self.assertNotIn("NY", games)

    def test_a_game_that_has_not_tipped_off_is_left_out(self):
        """Not returned with an empty clock: the schedule already knows how to
        say "7:30 pm", and that is the better answer."""
        self.assertEqual({}, live.parse({"events": [_event(state="pre")]}))

    def test_a_finished_game_is_kept_and_flagged(self):
        games = live.parse({"events": [_event(state="post", detail="Final")]})
        self.assertTrue(games[8].final)

    def test_a_half_scored_event_is_skipped_rather_than_half_read(self):
        bad = _event()
        bad["competitions"][0]["competitors"] = [{"team": {"id": 8}}]
        self.assertEqual({}, live.parse({"events": [bad]}))

    def test_rubbish_is_no_games_rather_than_an_exception(self):
        for payload in (None, {}, {"events": None}, {"events": [None]}):
            self.assertEqual({}, live.parse(payload))


class TestSayingWhereTheGameIs(unittest.TestCase):

    def test_a_quarter_and_a_clock(self):
        self.assertEqual("Q2 1:16", live.label(
            LiveGame(8, period=2, clock="1:16", detail="1:16 - 2nd")))

    def test_overtime_counts_from_one_again(self):
        self.assertEqual("OT 2:11", live.label(
            LiveGame(8, period=5, clock="2:11")))
        self.assertEqual("2OT 2:11", live.label(
            LiveGame(8, period=6, clock="2:11")))

    def test_halftime_defers_to_espns_own_word_for_it(self):
        """A period number cannot say "halftime": period 2 with no clock left
        would render as "Q2 0.0", which is a quarter that is not being
        played."""
        self.assertEqual("Half", live.label(
            LiveGame(8, period=2, clock="0.0", detail="Halftime")))

    def test_the_end_of_a_period_says_so(self):
        self.assertEqual("End Q3", live.label(
            LiveGame(8, period=3, clock="0.0", detail="End of 3rd")))

    def test_a_final_is_final_and_says_when_it_took_longer(self):
        self.assertEqual("Final", live.label(LiveGame(8, final=True, period=4)))
        self.assertEqual("Final/OT",
                         live.label(LiveGame(8, final=True, period=5)))

    def test_no_game_is_an_empty_string_not_a_dash(self):
        self.assertEqual("", live.label(None))


class TestMinutesComeOffTheRowThatWasAlreadyRead(unittest.TestCase):
    """No second request: the day's points and the day's minutes are two
    fields of one ESPN stat row."""

    ROW = {"playerPoolEntry": {"player": {"stats": [
        {"scoringPeriodId": 8, "statSourceId": 0, "statSplitTypeId": 5,
         "appliedTotal": 12.0, "stats": {"0": 6.0, "40": 17.0}},
        {"scoringPeriodId": 7, "statSourceId": 0, "statSplitTypeId": 5,
         "appliedTotal": 31.0, "stats": {"40": 34.0}},
    ]}}}

    def test_the_right_day(self):
        self.assertEqual(17.0, _day_row(self.ROW, 8)["stats"]["40"])
        self.assertEqual(34.0, _day_row(self.ROW, 7)["stats"]["40"])

    def test_a_day_espn_has_no_row_for_is_none(self):
        """Which is what lets the page print a dash rather than a zero."""
        self.assertIsNone(_day_row(self.ROW, 9))


class TestWhatTheRowSaysWhileHeIsPlaying(unittest.TestCase):

    LIVE = {"team": "DET", "opp": "@NYK", "tip": "5:00 pm",
            "when": "Q2 1:16", "score": "25-20", "mins": 17.0}

    def test_the_clock_replaces_the_tipoff(self):
        """A tipoff that has passed is the one thing on the row that is
        certainly no longer true."""
        out = web._meta(self.LIVE, "mmeta")
        self.assertIn("Q2 1:16", out)
        self.assertNotIn("5:00 pm", out)

    def test_the_score_is_separable_so_half_a_phone_can_drop_it(self):
        self.assertIn("<span class=lvs> &middot; 25-20</span>",
                      web._meta(self.LIVE, "mmeta"))
        self.assertIn("@media (max-width: 559px) { .mmeta .lvs", web.CSS)

    def test_an_injury_wins_while_he_is_not_on_the_floor(self):
        row = dict(self.LIVE, status_word="Out", mins=None)
        self.assertIn("Out", web._meta(row, "mmeta"))
        self.assertNotIn("Q2", web._meta(row, "mmeta"))

    def test_but_minutes_settle_it_once_he_is(self):
        """A day-to-day tag is a guess about today that minutes have
        answered."""
        row = dict(self.LIVE, status_word="Day-to-day")
        self.assertIn("Q2 1:16", web._meta(row, "mmeta"))

    def test_the_tipoff_still_shows_before_the_game_starts(self):
        row = dict(self.LIVE, when="", score="", mins=None)
        self.assertIn("5:00 pm", web._meta(row, "mmeta"))

    def test_minutes_sit_under_the_number_they_produced(self):
        self.assertIn("<small class=plmin>17m</small>",
                      web._points(12.0, "mpts", 17.0))

    def test_a_man_who_has_not_come_on_has_no_minutes_under_his_dash(self):
        """"0m" under a dash says the same thing twice."""
        self.assertNotIn("plmin", web._points(None, "mpts", 0.0))
        self.assertNotIn("plmin", web._points(None, "mpts", None))

    def test_minutes_survive_a_player_espn_sent_nothing_for(self):
        self.assertIn("plmin", web._points(None, "mpts", 4.0))


class TestTheSlateIsOnlyEverTodays(unittest.TestCase):

    class Bot:
        leagues: list = []

    def setUp(self):
        self.page = web.StatusPage(self.Bot())

    def test_a_past_day_gets_no_clock_at_all(self):
        """ESPN's scoreboard answers for the current slate. Asking it about
        Thursday would put tonight's clock under Thursday's games."""
        self.page._slate = (float("inf"), {8: LiveGame(8, period=2)})
        self.assertEqual({}, self.page._live_slate(7, 8))

    def test_today_is_served_from_the_cache_while_it_is_warm(self):
        warm = {8: LiveGame(8, period=3)}
        self.page._slate = (float("inf"), warm)
        self.assertIs(warm, self.page._live_slate(8, 8))

    def test_a_scoreboard_that_cannot_be_read_is_no_clock_not_a_broken_page(self):
        """Decoration on a page that is useful without it. ESPN being down
        must cost the clock and nothing else."""
        class Dead:
            def get(self, *a, **k):
                raise OSError("down")
        self.assertEqual({}, live.fetch(Dead()))


if __name__ == "__main__":
    unittest.main()
