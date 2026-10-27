"""The player card: ESPN's own, rebuilt here because it has no URL.

*"second photo is what im hoping for... recent season avs fantasy points and
recent news as up to date as possible"*. ESPN's card is client state behind an
onClick, so there is nothing to link to; this builds the same thing from the
same two endpoints it uses.

The failure modes worth testing are all quiet ones. A filter sent wrong comes
back **200 with an empty game log**, a news feed read at the wrong nesting
depth comes back as "no news", and a window that caught no new games comes
back as a duplicate row rather than as an error. None of those raise, so each
one gets a test.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rootfs", "app"))

import web  # noqa: E402
from espn import news  # noqa: E402
from espn.read import EspnRead, _parse_card  # noqa: E402
from models import NewsItem, StatSplit  # noqa: E402

SEASON = 2027


def _block(split, *, source=0, season=SEASON, period=0, team=0,
           stats=None, avg=None, total=0.0, applied_avg=None, bid=None):
    """One ESPN stat block. ``id`` is how the aggregate splits are found."""
    out = {
        "id": bid if bid is not None else "%s%d%d" % (source, split, season),
        "statSourceId": source, "statSplitTypeId": split,
        "seasonId": season, "scoringPeriodId": period, "proTeamId": team,
        "appliedTotal": total,
    }
    if stats is not None:
        out["stats"] = stats
    if avg is not None:
        out["averageStats"] = avg
    if applied_avg is not None:
        out["appliedAverage"] = applied_avg
    return out


#: A full box-score line, keyed the way ESPN keys it: strings, by stat id.
LINE = {"40": 30.0, "0": 20.0, "6": 10.0, "3": 5.0, "2": 1.0, "1": 2.0,
        "11": 3.0, "42": 4.0}


def _player(blocks, **over):
    player = {
        "id": 7, "fullName": "Test Man", "proTeamId": 15,
        "eligibleSlots": [4, 11, 12, 13], "injuryStatus": "ACTIVE",
        "ownership": {"percentOwned": 51.32},
        "seasonOutlook": "He will play basketball.",
        "stats": blocks,
    }
    player.update(over)
    return player


def _entry(player, **over):
    out = {"player": player, "status": "FREEAGENT", "onTeamId": 0,
           "ratings": {"0": {"positionalRanking": 25}}}
    out.update(over)
    return out


def _card(blocks, player_over=None, entry_over=None):
    player = _player(blocks, **(player_over or {}))
    return _parse_card(_entry(player, **(entry_over or {})), player,
                       SEASON, EspnRead.CARD_SPLITS)


class TestParsingTheCard(unittest.TestCase):
    def test_the_header_facts_come_off_the_entry_not_the_player(self):
        """Position rank and roster status live on the *entry*; everything
        else lives on the player. Reading either from the wrong one yields a
        well-formed card with a missing fact."""
        card = _card([])
        self.assertEqual(card.position_rank, 25)
        self.assertEqual(card.acquisition_status, "FREEAGENT")
        self.assertAlmostEqual(card.percent_owned, 51.32)
        self.assertEqual(card.name, "Test Man")

    def test_positions_are_the_real_ones_not_the_slots_he_may_fill(self):
        """``eligibleSlots`` carries UTIL, bench and IR as well, which say
        where he may be *played* rather than what he *is*."""
        self.assertEqual(_card([]).positions, ("C",))

    def test_a_multi_position_player_keeps_espns_order(self):
        card = _card([], player_over={"eligibleSlots": [1, 0, 11, 12]})
        self.assertEqual(card.positions, ("PG", "SG"))

    def test_a_split_is_matched_by_its_id_which_carries_the_season(self):
        card = _card([_block(0, avg=LINE, stats=LINE, total=85.5,
                             applied_avg=21.375)])
        self.assertEqual([s.label for s in card.splits], ["Season"])
        self.assertAlmostEqual(card.splits[0].fantasy_total, 85.5)
        self.assertAlmostEqual(card.splits[0].fantasy_average, 21.375)
        self.assertEqual(card.splits[0].games, 4)

    def test_last_seasons_split_is_not_mistaken_for_this_ones(self):
        """The same payload carries both. Keying on the split alone would
        show last season's averages under the heading 'Season'."""
        card = _card([_block(0, season=SEASON - 1, avg=LINE, stats=LINE,
                             total=999.0)])
        self.assertEqual(card.splits, ())

    def test_a_split_espn_did_not_send_is_absent_not_a_row_of_zeroes(self):
        card = _card([_block(0, avg=LINE, stats=LINE)])
        self.assertEqual([s.label for s in card.splits], ["Season"])

    def test_the_splits_use_averages_and_the_game_log_uses_totals(self):
        """One block carries both; a game log built from ``averageStats``
        would show a player's season average on every single row."""
        day = _block(5, period=3, team=15, stats=LINE,
                     avg={"0": 1.0}, bid="05x")
        card = _card([day])
        self.assertAlmostEqual(card.game_log[0].points, 20.0)

    def test_a_scheduled_game_he_did_not_play_is_not_a_game(self):
        """ESPN returns 82 periods whatever happens; the ones not played
        carry no stats at all and would otherwise be rows of zeroes that drag
        every average down."""
        card = _card([
            _block(5, period=3, team=15, stats=LINE, bid="05a"),
            _block(5, period=4, team=15, bid="05b"),
            _block(5, period=5, team=15, stats={}, bid="05c"),
        ])
        self.assertEqual([r.scoring_period for r in card.game_log], [3])

    def test_the_game_log_is_newest_first(self):
        card = _card([
            _block(5, period=2, team=15, stats=LINE, bid="05a"),
            _block(5, period=9, team=15, stats=LINE, bid="05b"),
            _block(5, period=5, team=15, stats=LINE, bid="05c"),
        ])
        self.assertEqual([r.scoring_period for r in card.game_log], [9, 5, 2])

    def test_a_projection_row_is_not_a_game_he_played(self):
        """``statSourceId`` 1 is ESPN's projection. It is in the same payload
        and has the same shape."""
        card = _card([_block(5, source=1, period=3, team=15, stats=LINE,
                             bid="15a")])
        self.assertEqual(card.game_log, ())

    def test_last_seasons_games_are_not_in_this_seasons_log(self):
        """The payload carries them -- 78 of last season's against four of
        this one on a real response -- and they share every other field."""
        card = _card([
            _block(5, season=SEASON - 1, period=170, team=15, stats=LINE,
                   bid="05old"),
            _block(5, period=3, team=15, stats=LINE, bid="05new"),
        ])
        self.assertEqual([r.scoring_period for r in card.game_log], [3])

    def test_each_row_keeps_the_team_he_played_that_game_for(self):
        """A traded player's October fixtures belong to his October team, and
        the opponent is resolved against this rather than against his team
        today."""
        card = _card([
            _block(5, period=3, team=15, stats=LINE, bid="05a"),
            _block(5, period=9, team=2, stats=LINE, bid="05b"),
        ])
        self.assertEqual([r.pro_team for r in card.game_log], [2, 15])

    def test_the_fantasy_points_are_espns_own_not_ours(self):
        """The card exists to be held up against ESPN's, so it shows the
        number ESPN applied under this league's scoring."""
        card = _card([_block(5, period=3, team=15, stats=LINE, total=31.0,
                             bid="05a")])
        self.assertAlmostEqual(card.game_log[0].fantasy_points, 31.0)


class TestTheNewsFeed(unittest.TestCase):
    ROTOWIRE = {"type": "Rotowire", "published": "2026-10-22T17:34:48Z",
                "description": "Turner notched 11 points.",
                "story": "He knocked down two triples."}

    def test_v3_nests_the_feed_one_level_deeper_than_v2(self):
        """Read at the wrong depth this returns a well-formed empty list,
        which renders as 'nothing filed' on a player with news."""
        self.assertEqual(len(news.parse({"news": {"feed": [self.ROTOWIRE]}}, 6)), 1)

    def test_the_v2_shape_still_parses(self):
        self.assertEqual(len(news.parse({"feed": [self.ROTOWIRE]}, 6)), 1)

    def test_only_notes_about_him_are_kept(self):
        """``Media`` is a highlight video whose description is its own title,
        and ``Story`` is a league-wide article that merely mentions him."""
        feed = [self.ROTOWIRE,
                {"type": "Media", "description": "Game Highlights"},
                {"type": "Story", "description": "Risers and fallers"}]
        got = news.parse({"news": {"feed": feed}}, 6)
        self.assertEqual([n.headline for n in got],
                         ["Turner notched 11 points."])

    def test_the_spin_is_carried_separately_from_what_happened(self):
        got = news.parse({"feed": [self.ROTOWIRE]}, 6)[0]
        self.assertEqual(got.headline, "Turner notched 11 points.")
        self.assertEqual(got.spin, "He knocked down two triples.")

    def test_the_timestamp_survives_espns_trailing_z(self):
        """``fromisoformat`` rejects ``Z`` before Python 3.11."""
        got = news.parse({"feed": [self.ROTOWIRE]}, 6)[0]
        self.assertEqual(got.published,
                         datetime(2026, 10, 22, 17, 34, 48, tzinfo=timezone.utc))

    def test_an_unreadable_timestamp_is_none_not_an_exception(self):
        item = dict(self.ROTOWIRE, published="whenever")
        self.assertIsNone(news.parse({"feed": [item]}, 6)[0].published)

    def test_the_limit_is_honoured(self):
        feed = [self.ROTOWIRE] * 10
        self.assertEqual(len(news.parse({"feed": feed}, 3)), 3)

    def test_an_empty_payload_is_no_news_rather_than_a_crash(self):
        for empty in (None, {}, {"news": {}}, {"feed": None}):
            self.assertEqual(news.parse(empty, 6), [])


class TestRoundingMatchesEspn(unittest.TestCase):
    """The card is meant to be held up against ESPN's. A column that
    disagrees in the last digit reads as a bug in the app."""

    def test_a_half_rounds_up_not_to_even(self):
        """``"%.1f" % 7.25`` is ``7.2`` in Python and ``7.3`` on ESPN."""
        self.assertEqual(web._n1(7.25), "7.3")

    def test_the_observed_case_from_a_live_card(self):
        """29 points over 4 games, which ESPN's own card shows as 7.3."""
        self.assertEqual(web._n1(29.0 / 4.0), "7.3")

    def test_a_number_that_is_not_one_renders_as_a_dash(self):
        for bad in (None, "", "x"):
            self.assertEqual(web._n1(bad), "&mdash;")


class TestWindowsThatAreTheSameWindow(unittest.TestCase):
    def test_a_window_that_caught_no_new_games_is_dropped(self):
        """In the first fortnight of a season all four windows hold the same
        four games, and four identical rows read as a bug."""
        rows = [{"label": "Season", "games": 4}, {"label": "Last 7", "games": 4},
                {"label": "Last 15", "games": 4}, {"label": "Last 30", "games": 4}]
        self.assertEqual([r["label"] for r in web._distinct_windows(rows)],
                         ["Season"])

    def test_windows_that_differ_are_all_kept(self):
        rows = [{"label": "Season", "games": 70}, {"label": "Last 7", "games": 3},
                {"label": "Last 15", "games": 7}, {"label": "Last 30", "games": 14}]
        self.assertEqual(len(web._distinct_windows(rows)), 4)

    def test_the_season_row_always_survives(self):
        self.assertEqual(len(web._distinct_windows([{"label": "S", "games": 0}])), 1)


def _view(**over):
    view = {
        "league_id": 9, "pid": 7, "name": "Test Man", "team": "MIL",
        "positions": "C", "status": "", "status_word": "Healthy",
        "rank": 25, "owned": 51.32, "manager": "Free agent", "outlook": "",
        "splits": [web._split_row(StatSplit(
            key="00", label="Season", games=4, minutes=27.5, points=7.25,
            rebounds=5.5, assists=2.25, steals=0.25, blocks=2.75,
            turnovers=3.0, fantasy_total=85.5, fantasy_average=21.375))],
        "log": [{"sp": 7, "day": "10/26", "opp": "IND", "today": True,
                 "min": 28.0, "pts": 6.0, "reb": 9.0, "ast": 4.0, "stl": 1.0,
                 "blk": 4.0, "to": 3.0, "fpts": 31.0}],
        "news": [{"when": "Oct 22, 5:34 pm", "source": "Rotowire",
                  "headline": "Turner notched 11 points.",
                  "spin": "He knocked down two triples."}],
    }
    view.update(over)
    return view


class TestRenderingTheCard(unittest.TestCase):
    def test_it_shows_the_three_things_it_was_asked_for(self):
        """*"recent season avs fantasy points and recent news"*."""
        out = web.player_card_html(_view())
        self.assertIn("Season", out)
        self.assertIn("21.4", out)          # fantasy points per game
        self.assertIn("31.0", out)          # the game's fantasy points
        self.assertIn("He knocked down two triples.", out)

    def test_the_fantasy_column_comes_before_the_box_score(self):
        """Eight numeric columns do not fit a phone, so the table scrolls --
        and the column it is read for must not be the one off the edge."""
        out = web.player_card_html(_view())
        stats, log = out.index("<h3>Stats"), out.index("<h3>Game log")
        self.assertLess(out.index("FP/G", stats), out.index(">MIN<", stats))
        self.assertLess(out.index(">FPTS<", log), out.index(">PTS<", log))

    def test_every_string_espn_sent_is_escaped(self):
        """The Spin is prose written by somebody else and rendered inside the
        authenticated Home Assistant frontend."""
        nasty = "<img src=x onerror=alert(1)>"
        out = web.player_card_html(_view(
            name=nasty,
            news=[{"when": nasty, "source": "", "headline": nasty,
                   "spin": nasty}]))
        self.assertNotIn("<img src=x", out)
        self.assertIn("&lt;img src=x", out)

    def test_the_headshot_url_can_only_be_a_number(self):
        out = web.player_card_html(_view(pid="7' onload='x"))
        self.assertNotIn("onload", out)

    def test_a_player_with_no_games_says_so(self):
        out = web.player_card_html(_view(log=[], splits=[]))
        self.assertIn("No games played yet", out)

    def test_a_player_with_no_news_says_so_rather_than_showing_nothing(self):
        out = web.player_card_html(_view(news=[]))
        self.assertIn("Nothing filed", out)

    def test_an_error_renders_as_an_error_not_as_an_empty_card(self):
        out = web.player_card_html({"error": "Could not read this player."})
        self.assertIn("Could not read this player.", out)
        self.assertNotIn("Game log", out)

    def test_todays_game_is_marked(self):
        self.assertIn("class=today", web.player_card_html(_view()))

    def test_an_absent_fact_is_left_out_rather_than_shown_empty(self):
        out = web.player_card_html(_view(rank=None, owned=None, manager=""))
        self.assertNotIn("Position rank", out)
        self.assertNotIn("Rostered", out)
        self.assertIn("Status", out)

    def test_the_fragment_carries_no_page_around_it(self):
        """It is dropped straight into the page that opened it."""
        out = web.player_card_html(_view())
        self.assertNotIn("<!doctype", out)
        self.assertNotIn("<body", out)


class TestTheDialogIsOnlyAnEnhancement(unittest.TestCase):
    """The name is a real link to a real page. Everything the script does is
    an optimisation on top of that, so the page has to work without it."""

    def test_the_link_resolves_to_a_page_of_its_own(self):
        self.assertIn("href='player?league=9&amp;id=7'",
                      web._who(7, "A", league=9))

    def test_the_shell_and_the_script_are_on_every_page(self):
        out = web._close("someone")
        self.assertIn("id=pcard", out)
        self.assertIn("data-card", out)

    def test_a_modified_click_is_left_to_the_browser(self):
        """Middle-click and cmd-click still open a tab, which is what anybody
        comparing two players will reach for."""
        self.assertIn("e.metaKey", web.CARD_JS)
        self.assertIn("e.button === 0", web.CARD_JS)

    def test_the_script_falls_back_to_navigating_when_the_fetch_fails(self):
        self.assertIn("window.location = a.getAttribute('href')", web.CARD_JS)

    def test_it_does_nothing_at_all_where_dialog_is_unsupported(self):
        self.assertIn("typeof dlg.showModal !== 'function'", web.CARD_JS)


if __name__ == "__main__":
    unittest.main()


class TestTheCardCacheIsBounded(unittest.TestCase):
    """This process runs for months, and the key space is every player in the
    league rather than the ones on a roster: browsing the free agent pool
    would otherwise leave a card in memory for every name looked at."""

    def _page(self):
        page = web.StatusPage.__new__(web.StatusPage)
        page._card_cache = {}
        return page

    def test_it_stops_growing(self):
        page = self._page()
        for i in range(web.StatusPage.CARD_MAX * 3):
            page._remember_card((9, i), {"pid": i})
        self.assertLessEqual(len(page._card_cache), web.StatusPage.CARD_MAX)

    def test_an_expired_card_is_swept_before_a_live_one_is_dropped(self):
        page = self._page()
        page._card_cache = {(9, i): (0.0, {"pid": i})
                            for i in range(web.StatusPage.CARD_MAX)}
        page._remember_card((9, 999), {"pid": 999})
        self.assertEqual(list(page._card_cache), [(9, 999)])

    def test_the_card_just_asked_for_is_the_one_kept(self):
        page = self._page()
        for i in range(web.StatusPage.CARD_MAX * 2):
            page._remember_card((9, i), {"pid": i})
        self.assertIn((9, web.StatusPage.CARD_MAX * 2 - 1), page._card_cache)


class TestTheFactsEspnShowsBesideTheName(unittest.TestCase):
    def test_his_primary_position_comes_first(self):
        """ESPN shows "SG, PG" for a shooting guard who can run the point,
        and the card is meant to read the same way beside it."""
        card = _card([], player_over={"eligibleSlots": [0, 1, 11],
                                      "defaultPositionId": 2})
        self.assertEqual(card.positions, ("SG", "PG"))

    def test_without_a_default_the_order_is_espns_own(self):
        card = _card([], player_over={"eligibleSlots": [0, 1, 11]})
        self.assertEqual(card.positions, ("PG", "SG"))

    def test_an_unranked_player_has_no_rank_rather_than_rank_zero(self):
        """ESPN reports 0 for a player it has not ranked, and "#0" on a card
        reads as a rank of nought."""
        card = _card([], entry_over={"ratings": {"0": {"positionalRanking": 0}}})
        self.assertIsNone(card.position_rank)
        self.assertNotIn("Position rank", web.player_card_html(_view(rank=None)))

    def test_a_window_with_no_games_is_not_a_row_of_zeroes(self):
        """It reads as "he scores nothing" rather than "he has not played",
        which the game log says in words just below it."""
        card = _card([_block(0, avg={}, stats={"42": 0.0}, total=0.0)])
        self.assertEqual(card.splits, ())


class TestTheCardLooksLikeACardOnASmallScreen(unittest.TestCase):
    """The modal's layout, in the three places it went wrong on a phone.

    None of this is testable by rendering, because it is all stylesheet -- but
    each one is a line somebody could delete as redundant without the page
    looking any different on the machine they deleted it on.
    """

    def test_the_dialog_is_pinned_to_the_viewport_not_the_document(self):
        """A current browser's UA sheet already says this; an older WebView
        says ``absolute``, which anchors the card to the top of the document.
        Open one from halfway down the matchup page there and it is above the
        screen."""
        self.assertIn("position: fixed; inset: 0; margin: auto;", web.CSS)

    def test_the_shadow_colour_exists_in_both_themes(self):
        """A scroll shadow at 26% black is invisible on a dark surface, so
        the cue that a table has more to the right would be there in daylight
        and gone at night."""
        light, dark = web.CSS.split("@media (prefers-color-scheme: dark)", 1)
        self.assertIn("--shade:", light)
        self.assertIn("--shade:", dark)
        self.assertIn("--pbg: var(--card-background-color", web.CSS)

    def test_the_shadow_hides_itself_at_the_end_of_the_scroll(self):
        """The trick is two pairs of layers: the ones that cover sit on the
        content (``local``) and travel away as you scroll, uncovering the
        fixed shadow beneath. Both cover layers must come first in the
        shorthand, or they are painted under what they are there to hide."""
        for block in ("#pcard .pbody {", ".pscroll {"):
            rule = web.CSS.split(block, 1)[1].split("}", 1)[0]
            bg = rule.split("background:", 1)[1]
            self.assertEqual(2, bg.count("no-repeat local"), block)
            self.assertLess(bg.rfind("no-repeat local"),
                            bg.find("var(--shade)"), block)

    def test_only_the_name_keeps_clear_of_the_close_button(self):
        """Reserving that width down the whole hero costs the facts strip a
        column, which is what put a manager's surname on a line by itself."""
        self.assertIn("#pcard .pwho { padding-right:", web.CSS)
        self.assertNotIn("#pcard .phero { padding-right:", web.CSS)
