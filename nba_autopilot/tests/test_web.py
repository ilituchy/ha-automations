"""Ingress status page: escaping, relative URLs, and who may approve.

FastAPI is not installed in the development environment, which is why the two
things worth asserting here -- what goes into the HTML and who is allowed to
decide -- live in ``render`` and ``apply_decision`` rather than inside the app
factory. These tests need neither a server nor a socket.

The page is a second path to executing a proposal, and the first one took a
bug that silently refused every approval. So the tests below check the
*negative* cases hardest: a non-owner must be refused before the state machine
is touched at all, and a stale decision must not reach the executor.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
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

import web  # noqa: E402
from models import MatchupScore  # noqa: E402
from state import Proposal, Store  # noqa: E402


def _proposal(action_id="R1:ADD_DROP:0", kind="ADD_DROP", payload=None,
              rationale="", state="PROPOSED", pid=1, run_id="R1"):
    return Proposal(
        id=pid,
        run_id=run_id,
        action_id=action_id,
        kind=kind,
        state=state,
        payload=payload if payload is not None else {},
        rationale=rationale,
        value_delta=0.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
    )


class FakeRef:
    def __init__(self, league_id, label):
        self.league_id = league_id
        self._label = label

    def display(self):
        return self._label


class FakeRead:
    """``matchup_score`` and nothing else, which is all the page calls.

    ``score`` may be a ``MatchupScore``, ``None`` (no row this period), or an
    exception to raise -- the three outcomes the live path has to survive.
    """

    def __init__(self, score=None, delay=0.0):
        self.score = score
        self.delay = delay
        self.calls = 0

    def matchup_score(self, team_id=None, matchup_period=None):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.score, BaseException):
            raise self.score
        return self.score


class FakeLeague:
    def __init__(self, league_id, label, score=None, delay=0.0):
        self.ref = FakeRef(league_id, label)
        self.read = FakeRead(score, delay)


class FakeCfg:
    def __init__(self, owner="", shadow_mode=False):
        self.ha_owner_user_id = owner
        self.shadow_mode = shadow_mode


class FakeStore:
    """Only the four things the page touches."""

    def __init__(self, pending=(), rows=None, stuck=(), run_leagues=None,
                 boards=()):
        self._pending = list(pending)
        self._rows = rows or {}
        self._stuck = list(stuck)
        #: Scoreboard rows, one per league. Separate from ``_rows`` because the
        #: page selects them with their own statement and every other fixture
        #: would otherwise have to carry an empty list for them.
        self._boards = list(boards)
        #: ``run_id -> league_id``, for the one query the page makes whose
        #: answer depends on its argument: which league a pending proposal's
        #: run belonged to. ``_rows`` matches on the SQL alone, so it cannot
        #: give two run ids two different leagues.
        self._run_leagues = dict(run_leagues or {})
        self.decisions = []
        self.decide_result = _proposal()

    def pending(self, run_id=None):
        return list(self._pending)

    def stuck_writes(self):
        return list(self._stuck)

    def query(self, sql, args=()):
        if "FROM scoreboards" in sql:
            return list(self._boards)
        if self._run_leagues and "SELECT league_id FROM runs" in sql:
            lid = self._run_leagues.get(args[0] if args else None)
            return [{"league_id": lid}] if lid is not None else []
        for fragment, rows in self._rows.items():
            if fragment in sql:
                return rows
        return []

    def decide(self, action_id, approve, user_id):
        self.decisions.append((action_id, approve, user_id))
        return self.decide_result


class FakeBot:
    def __init__(self, store=None, cfg=None, leagues=()):
        self.store = store or FakeStore()
        self.cfg = cfg or FakeCfg()
        self.leagues = list(leagues)
        self.executed = 0

    async def execute_approved(self):
        self.executed += 1


class TestRenderEscaping(unittest.TestCase):
    """Rationale text carries ESPN player names and, soon, LLM output.

    Both are untrusted by the project's own rules, and this page renders inside
    the authenticated Home Assistant frontend -- which is the worst possible
    place to put an injection, because the session it would run in is the one
    that can reconfigure the instance.
    """

    def test_rationale_is_escaped(self):
        nasty = "<script>alert('x')</script>"
        bot = FakeBot(FakeStore(pending=[_proposal(rationale=nasty)]))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_player_name_is_escaped(self):
        bot = FakeBot(FakeStore(pending=[
            _proposal(payload={"add": 1, "add_name": 'O"<b>Neal'}),
        ]))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("<b>Neal", out)

    def test_action_id_cannot_break_out_of_its_attribute(self):
        """The id lands inside value='...', so a quote in it must not escape."""
        bot = FakeBot(FakeStore(pending=[
            _proposal(action_id="R1:ADD:0' autofocus onfocus='alert(1)"),
        ]))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("onfocus='alert(1)", out)
        self.assertIn("&#x27;", out)

    def test_run_notes_are_escaped(self):
        bot = FakeBot(FakeStore(rows={
            "FROM runs": [{
                "run_id": "R1", "league_id": 1, "started_at": "2026-10-21",
                "scoring_period": 5, "shadow_mode": 0,
                "notes": '["<img src=x onerror=alert(1)>"]',
                "shadow_prices": "{}",
            }],
        }))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("<img src=x", out)


class TestRationaleBlock(unittest.TestCase):
    """The rationale is four times the height of the move it explains.

    Closed, *none* of it shows -- a clamped lead line was tried first and read
    as a card leading with its own footnotes. Open, nothing may be lost: the
    reviewer's caveat is the part most worth reading and it is always the
    *last* paragraph.
    """

    def test_even_a_short_single_paragraph_is_hidden(self):
        out = web._why("Add him. He is better.")
        self.assertIn("<details", out)
        self.assertNotIn("Add him. He is better.</summary>", out)

    def test_nothing_of_the_rationale_sits_in_the_summary(self):
        """The summary is empty; CSS puts "Show details" on it."""
        out = web._why("The lead.\n\nReviewer's caveat: think twice.")
        self.assertIn("<summary></summary>", out)
        self.assertIn("Reviewer&#x27;s caveat: think twice.", out)

    def test_an_empty_rationale_renders_no_control(self):
        self.assertEqual(web._why(""), "")
        self.assertEqual(web._why("   \n\n  "), "")

    def test_every_paragraph_survives(self):
        out = web._why("one\n\ntwo\n\nthree")
        for word in ("one", "two", "three"):
            self.assertIn(">%s<" % word, out)

    def test_paragraphs_are_escaped_on_both_sides_of_the_split(self):
        out = web._why("<b>lead</b>\n\n<b>rest</b>")
        self.assertNotIn("<b>", out)
        self.assertEqual(out.count("&lt;b&gt;"), 2)


class TestTimestamps(unittest.TestCase):
    """Runs are recorded in UTC with microseconds; nobody reads that.

    The live page showed ``2026-10-22T16:54:18.033797+00:00`` for a run the
    owner watched happen at breakfast -- right to the microsecond, and in the
    wrong zone by seven hours.
    """

    PT = timezone(timedelta(hours=-7))
    NOW = datetime(2026, 10, 22, 11, 0, tzinfo=PT)

    def test_a_run_today_is_shown_as_a_time(self):
        self.assertEqual(
            web._when("2026-10-22T16:54:18.033797+00:00", self.PT, self.NOW),
            "Today, 9:54 am",
        )

    def test_yesterday_is_named(self):
        self.assertEqual(
            web._when("2026-10-21T16:54:18+00:00", self.PT, self.NOW),
            "Yesterday, 9:54 am",
        )

    def test_older_runs_carry_their_date(self):
        self.assertEqual(
            web._when("2026-10-18T23:05:00+00:00", self.PT, self.NOW),
            "Sun 18 Oct, 4:05 pm",
        )

    def test_a_naive_timestamp_is_read_as_utc(self):
        """Older rows were written without an offset. They are UTC too."""
        self.assertEqual(
            web._when("2026-10-22 16:54:18", self.PT, self.NOW),
            "Today, 9:54 am",
        )

    def test_an_unparseable_timestamp_is_shown_rather_than_swallowed(self):
        """A row that does not fit the format is still evidence of something."""
        self.assertEqual(web._when("not a date", self.PT, self.NOW), "not a date")

    def test_noon_and_midnight_do_not_become_zero(self):
        self.assertEqual(web._clock(datetime(2026, 1, 1, 0, 5)), "12:05 am")
        self.assertEqual(web._clock(datetime(2026, 1, 1, 12, 5)), "12:05 pm")

    def test_a_bot_without_a_zone_still_renders(self):
        """`tz` is read off the bot; the test doubles here do not have one."""
        out = web.render(web.StatusPage(FakeBot()))
        self.assertIn("No runs recorded yet.", out)


class TestKindLabels(unittest.TestCase):
    def test_known_kinds_read_as_english(self):
        self.assertEqual(web._kind("ADD_DROP"), "Add / drop")

    def test_an_unknown_kind_is_shown_rather_than_dropped(self):
        """A kind added later must still render, just without a nice name."""
        self.assertEqual(web._kind("TRADE_OFFER"), "Trade offer")


def _run_rows(props, notes='[]'):
    """A store whose single run carries ``props``."""
    return {
        "FROM runs": [{
            "run_id": "R1", "league_id": 1, "started_at": "2026-10-22 09:54:00",
            "scoring_period": 3, "shadow_mode": 0,
            "notes": notes, "shadow_prices": "{}",
        }],
        "FROM proposals": props,
    }


LINEUP_PAYLOAD = json.dumps({
    "scoring_period": 3,
    "moves": [
        {"player_id": 3136195, "name": "Karl-Anthony Towns", "from": "UTIL",
         "to": "BE", "kind": "LINEUP",
         "why": "UTIL -> BE (no game today; p_play 0.93)"},
    ],
})


class TestAppliedMoves(unittest.TestCase):
    """What the run did, as moves rather than as a paragraph.

    The owner's question of the page is "what did it change for me today",
    and the answer used to be a semicolon-joined sentence buried under the
    working notes.
    """

    def test_a_lineup_write_is_listed_move_by_move(self):
        bot = FakeBot(FakeStore(rows=_run_rows([{
            "kind": "LINEUP", "state": "DONE",
            "rationale": "Karl-Anthony Towns UTIL->BE",
            "payload": LINEUP_PAYLOAD,
        }])))
        out = web.render(web.StatusPage(bot))
        self.assertIn("Set automatically", out)
        self.assertIn("Karl-Anthony Towns", out)
        self.assertIn(">UTIL<", out)
        self.assertIn(">Bench<", out)

    def test_the_slots_are_not_repeated_in_the_reason(self):
        moves = json.loads(LINEUP_PAYLOAD)["moves"]
        self.assertEqual(web._move_reason(moves[0]),
                         "no game today; p_play 0.93")

    def test_an_unrecognised_reason_is_shown_whole(self):
        self.assertEqual(web._move_reason({"why": "something else"}),
                         "something else")

    def test_a_headshot_is_requested_for_a_known_player(self):
        self.assertIn("/3136195.png", web._face(3136195))

    def test_an_unusable_player_id_still_renders_a_disc(self):
        """No id, no image -- but never a broken-image icon and never a URL."""
        out = web._face(None)
        self.assertIn("class='face", out)
        self.assertNotIn("background-image", out)

    def test_a_player_id_cannot_smuggle_css_into_the_style_attribute(self):
        out = web._face("1);background:url(//evil/x")
        self.assertNotIn("evil", out)

    def test_a_run_journaled_before_the_payload_carried_moves(self):
        """The one-line rationale is parsed back into rows, minus the faces."""
        bot = FakeBot(FakeStore(rows=_run_rows([{
            "kind": "LINEUP", "state": "DONE",
            "rationale": "Anthony Black BE->UTIL; Jalen Duren UTIL->BE",
            "payload": "{}",
        }])))
        out = web.render(web.StatusPage(bot))
        self.assertIn("Anthony Black", out)
        self.assertIn("Jalen Duren", out)
        self.assertIn("class=did", out)

    def test_a_rationale_that_is_not_a_move_list_falls_back(self):
        """Half a parse is worse than none: the row keeps its paragraph."""
        self.assertEqual(
            web._moves_of({"rationale": "lineup already optimal",
                           "payload": {}}),
            [],
        )

    def test_a_failed_lineup_write_says_so(self):
        bot = FakeBot(FakeStore(rows=_run_rows([{
            "kind": "LINEUP", "state": "FAILED",
            "rationale": "Karl-Anthony Towns UTIL->BE",
            "payload": LINEUP_PAYLOAD,
        }])))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("Set automatically", out)
        self.assertIn(">failed<", out)

    def test_a_move_name_is_escaped(self):
        bot = FakeBot(FakeStore(rows=_run_rows([{
            "kind": "LINEUP", "state": "DONE", "rationale": "",
            "payload": json.dumps({"moves": [
                {"player_id": 1, "name": "<img src=x>", "from": "BE",
                 "to": "UTIL", "why": ""},
            ]}),
        }])))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("<img src=x", out)


class TestPlayerLinks(unittest.TestCase):
    """Every name is a player the owner may want to go and read about.

    The id is already in hand wherever a name is rendered -- the headshot uses
    it -- so the name carries a link to ESPN's own page for him. The id is
    interpolated into a URL, so these tests care most about what cannot get
    into it.
    """

    def test_a_name_links_to_the_espn_player_page(self):
        out = web._who(3136195, "Karl-Anthony Towns")
        self.assertIn("href='https://www.espn.com/nba/player/_/id/3136195'",
                      out)
        self.assertIn("Karl-Anthony Towns", out)

    def test_the_link_opens_in_a_new_tab_without_a_handle_on_this_one(self):
        out = web._who(1, "A")
        self.assertIn("target=_blank", out)
        self.assertIn("noopener", out)
        self.assertIn("noreferrer", out)

    def test_an_unusable_id_renders_the_name_without_a_link(self):
        for bad in (None, "", "not-a-number", {}):
            out = web._who(bad, "Nameless Man")
            self.assertIn("Nameless Man", out)
            self.assertNotIn("<a", out)

    def test_an_id_cannot_smuggle_anything_into_the_url(self):
        out = web._who("1' onmouseover='x", "A")
        self.assertNotIn("onmouseover", out)
        self.assertNotIn("<a", out)

    def test_the_name_is_still_escaped_inside_the_link(self):
        self.assertNotIn("<img src=x", web._who(1, "<img src=x>"))

    def test_both_legs_of_a_swap_are_linked(self):
        out = web._swap({"add": 10, "add_name": "In Man",
                         "drop": 20, "drop_name": "Out Man", "waiver": False})
        self.assertIn("/id/10'", out)
        self.assertIn("/id/20'", out)

    def test_a_lineup_move_links_the_player_it_moved(self):
        bot = FakeBot(FakeStore(rows=_run_rows([{
            "kind": "LINEUP", "state": "DONE",
            "rationale": "Karl-Anthony Towns UTIL->BE",
            "payload": LINEUP_PAYLOAD,
        }])))
        out = web.render(web.StatusPage(bot))
        self.assertIn("/nba/player/_/id/3136195", out)

    def test_a_recovered_old_move_has_no_id_and_so_no_link(self):
        """Rows journaled before ``moves`` existed only parse back a name."""
        bot = FakeBot(FakeStore(rows=_run_rows([{
            "kind": "LINEUP", "state": "DONE",
            "rationale": "Anthony Black BE->UTIL",
            "payload": "{}",
        }])))
        out = web.render(web.StatusPage(bot))
        self.assertIn("Anthony Black", out)
        self.assertNotIn("/nba/player/_/id/", out)


def _panels(out: str) -> int:
    """How many scoreboard panels the page rendered.

    Counted by the one element every panel has and nothing else does. The
    panel's own tag is not stable -- it is an ``<a>`` to the matchup page when
    the row knows its league and a plain ``<section>`` when it does not -- and
    these tests are about how many leagues are shown, not about that.
    """
    return out.count("<div class=bhead>")


def _board(**over):
    """A scoreboard row as ``record_scoreboard`` writes one."""
    row = {
        "league_id": 1, "run_id": "R1",
        "updated_at": "2026-10-23T13:54:18+00:00",
        "matchup_period": 1, "scoring_period": 4,
        "my_team_id": 2, "my_name": "Kessler", "my_abbrev": "KES",
        "my_logo": "https://g.espncdn.com/x.svg", "my_points": 403.0,
        "opp_team_id": 4, "opp_name": "d'angelo", "opp_abbrev": "VODO",
        "opp_logo": "https://g.espncdn.com/y.svg", "opp_points": 513.5,
        "my_games_used": 12, "opp_games_used": 14,
        "my_games_today": 4, "games_cap": 30,
    }
    row.update(over)
    return row


class TestScoreboard(unittest.TestCase):
    """This week's head-to-head, at the head of its league's column.

    ``render`` is synchronous, so the numbers it is handed were either read
    live by the route that called it or recovered from the row the last run
    cached -- possibly hours ago. That is the whole reason the panel carries
    its own timestamp, and the reason a missing field has to render as an
    absence rather than as a zero: a score of 0-0 shown confidently is worse
    than no panel at all.
    """

    def test_one_panel_per_league_in_configured_order(self):
        bot = FakeBot(
            FakeStore(boards=[_board(league_id=9, my_name="Second"),
                              _board(league_id=4, my_name="First")]),
            leagues=[FakeLeague(4, "Alpha"), FakeLeague(9, "Beta")],
        )
        out = web.render(web.StatusPage(bot))
        self.assertLess(out.index("Alpha"), out.index("Beta"))
        self.assertEqual(_panels(out), 2)

    def test_a_league_with_no_reading_yet_is_simply_absent(self):
        """A fresh install shows nothing, not a 0-0 placeholder."""
        bot = FakeBot(
            FakeStore(boards=[_board(league_id=4)]),
            leagues=[FakeLeague(4, "Alpha"), FakeLeague(9, "Beta")],
        )
        out = web.render(web.StatusPage(bot))
        self.assertEqual(_panels(out), 1)

    def test_an_unreadable_league_still_holds_its_grid_cell(self):
        """*"matching each column at the top"*.

        The score band, the approvals and the runs are the same leagues in the
        same order, and the page is read *down* a column. A league whose score
        could not be read has to leave a hole: collapse it and the second
        league's panel slides into the first league's column, where it sits
        over the first league's approvals.
        """
        bot = FakeBot(
            FakeStore(boards=[_board(league_id=9, my_name="Beta's team")]),
            leagues=[FakeLeague(4, "Alpha"), FakeLeague(9, "Beta")],
        )
        out = web.render(web.StatusPage(bot))
        band = out.split("<div class=boards>")[1].split("</div><h2")[0]
        self.assertTrue(band.startswith("<div></div>"), band[:60])

    def test_the_score_band_uses_the_same_grid_as_the_columns_below(self):
        """One rule, not two that happen to agree today.

        Separate templates would look aligned until the first time one of them
        was edited, and the misalignment is silent.
        """
        self.assertIn(".leagues, .boards {", web.CSS)

    def test_no_panel_at_all_when_nothing_has_been_read(self):
        out = web.render(web.StatusPage(FakeBot(FakeStore())))
        self.assertNotIn("class=boards", out)

    def test_only_the_leader_gets_full_strength_ink(self):
        out = web._board(_board(), None)
        self.assertEqual(out.count("class='bteam up'"), 1)
        # The leader is the opponent here, so the emphasis must land on the
        # second row -- not on ours by default.
        self.assertLess(out.index("Kessler"), out.index("bteam up"))

    def test_the_gap_is_computed_rather_than_left_to_the_reader(self):
        self.assertIn("Behind by 110", web._board(_board(), None))
        self.assertIn("Ahead by 110",
                      web._board(_board(my_points=513.5, opp_points=403.0),
                                 None))
        self.assertIn("Level",
                      web._board(_board(opp_points=403.0), None))

    def test_games_after_today_is_the_sum_and_is_worded_as_a_projection(self):
        self.assertIn("<b>16</b> games after today", web._board(_board(), None))

    def test_a_quiet_night_says_so_rather_than_repeating_the_count(self):
        self.assertIn("no games left today",
                      web._board(_board(my_games_today=0), None))

    def test_an_uncapped_league_does_not_invent_a_denominator(self):
        self.assertEqual(web._games_line(12, 30), "12 of 30 games")
        self.assertEqual(web._games_line(12, None), "12 games")
        self.assertEqual(web._games_line(1, 0), "1 game")
        self.assertEqual(web._games_line(None, 30), "")

    def test_a_bye_is_said_plainly_rather_than_drawn_as_a_0_0(self):
        out = web._board(_board(opp_team_id=None, opp_name="", opp_points=0.0),
                         None)
        self.assertIn("No opponent (bye)", out)
        self.assertEqual(out.count("class=bpts"), 1)
        self.assertNotIn("Ahead by", out)

    def test_an_unreported_score_renders_as_an_absence(self):
        """A row ESPN has not scored yet is blank, not zero.

        ``my_games_used`` is ``None`` only when the matchup row carries no
        pooled entry at all, which is not the same fact as "nobody has played",
        so the subline is dropped rather than printed as ``0 of 30``.
        """
        out = web._board(_board(my_points=None, my_games_used=None,
                                opp_points=None, opp_games_used=None), None)
        self.assertEqual(out.count("<span class=bpts>—</span>"), 2)
        self.assertNotIn("class=bgames", out)
        # Neither side leads, so neither gets the emphasis and the footer has
        # no gap to report.
        self.assertNotIn("bteam up", out)

    def test_a_team_name_is_escaped_like_everything_else(self):
        out = web._board(_board(my_name="<img src=x onerror=alert(1)>"), None)
        self.assertNotIn("<img src=x", out)
        self.assertIn("&lt;img", out)

    def test_a_logo_that_is_not_https_falls_back_to_initials(self):
        """``CUSTOM_VALID`` is a URL a leaguemate typed, not an ESPN asset."""
        for bad in ("javascript:alert(1)", "data:image/svg+xml,<svg/>",
                    "http://meme.example/x.png", "/relative.png", "", None):
            out = web._crest(bad, "Kessler I Met")
            self.assertEqual(out, "<span class=crest>KI</span>", bad)

    def test_a_third_party_logo_is_never_told_where_it_was_loaded_from(self):
        """The document URL carries the ingress session token."""
        out = web._crest("https://imgflip.com/x.png", "Kessler")
        self.assertIn("referrerpolicy=no-referrer", out)
        self.assertIn("src='https://imgflip.com/x.png'", out)

    def test_a_logo_url_cannot_break_out_of_its_attribute(self):
        out = web._crest("https://x/y.png' onerror='alert(1)", "K")
        self.assertNotIn("onerror='alert(1)", out)
        self.assertIn("&#x27;", out)

    def test_the_whole_page_declines_to_send_a_referer(self):
        out = web.render(web.StatusPage(FakeBot(FakeStore())))
        self.assertIn("<meta name=referrer content=no-referrer>", out)

    def test_the_age_of_the_reading_is_shown(self):
        """A score cached this morning shown bare would read as live."""
        bot = FakeBot(FakeStore(boards=[_board(league_id=4)]),
                      leagues=[FakeLeague(4, "Alpha")])
        out = web.render(web.StatusPage(bot))
        self.assertIn("class=bwhen", out)

    def test_initials_cope_with_a_name_made_of_punctuation(self):
        self.assertEqual(web._initials("!!! ???"), "?")
        self.assertEqual(web._initials(""), "?")
        self.assertEqual(web._initials(None), "?")
        self.assertEqual(web._initials("d'angelo"), "D")


def _score(**over):
    """A ``MatchupScore`` as ``matchup_score()`` returns one."""
    kw = dict(
        matchup_period=1, my_team_id=2, my_points=444.0,
        opp_team_id=4, opp_name="d'angelo", opp_points=520.0,
        my_name="Kessler", my_abbrev="KES",
        my_logo="https://g.espncdn.com/live.svg", opp_abbrev="VODO",
        opp_logo="https://g.espncdn.com/opp.svg",
        my_games_used=13, opp_games_used=15,
    )
    kw.update(over)
    return MatchupScore(**kw)


class TestLiveScoreboard(unittest.TestCase):
    """The score is read from ESPN on every load, not recovered from SQLite.

    A lineup decision is still a fact an hour after it was made; a score an
    hour old is simply wrong, and this is the one number on the page worth a
    request while somebody is waiting. The credentials are right here -- each
    league carries its own authenticated reader.

    What the live read must never do is cost the page its ability to render:
    the Approve button is on it. So every failure below degrades to the run's
    cached row and the panel says which one the reader is looking at.
    """

    def page(self, leagues, boards=()):
        return web.StatusPage(
            FakeBot(FakeStore(boards=list(boards)), leagues=leagues))

    def test_the_live_score_is_what_renders(self):
        p = self.page([FakeLeague(4, "Alpha", score=_score())],
                      [_board(league_id=4, my_points=403.0)])
        boards = asyncio.run(p.live_scoreboards())
        self.assertEqual(boards[0]["my_points"], 444.0)
        self.assertEqual(boards[0]["my_logo"], "https://g.espncdn.com/live.svg")
        self.assertTrue(boards[0]["live"])

    def test_the_two_fields_espn_does_not_send_survive_from_the_run(self):
        """The cap is a season-long setting and tonight's count needs the
        lineup *and* the pro schedule -- neither is on the score response, and
        neither is worth more requests to rebuild."""
        p = self.page([FakeLeague(4, "Alpha", score=_score())],
                      [_board(league_id=4, games_cap=30, my_games_today=4)])
        board = asyncio.run(p.live_scoreboards())[0]
        self.assertEqual(board["games_cap"], 30)
        self.assertEqual(board["my_games_today"], 4)
        # And they reach the rendered panel, so the live score still carries
        # its denominator and its projection.
        out = web._board(board, None)
        self.assertIn("13 of 30 games", out)
        self.assertIn("<b>17</b> games after today", out)

    def test_a_dead_cookie_costs_a_stale_score_not_the_page(self):
        p = self.page(
            [FakeLeague(4, "Alpha", score=RuntimeError("espn_s2 expired"))],
            [_board(league_id=4, my_points=403.0)])
        boards = asyncio.run(p.live_scoreboards())
        self.assertEqual(boards[0]["my_points"], 403.0)
        self.assertFalse(boards[0].get("live"))
        # And the page still renders, buttons and all.
        out = web.render(p, boards=boards)
        self.assertEqual(_panels(out), 1)

    def test_a_slow_espn_is_given_up_on_rather_than_waited_for(self):
        lg = FakeLeague(4, "Alpha", score=_score(), delay=0.3)
        p = self.page([lg], [_board(league_id=4, my_points=403.0)])
        p.LIVE_TIMEOUT = 0.05
        board = asyncio.run(p.live_scoreboards())[0]
        self.assertEqual(board["my_points"], 403.0)
        self.assertFalse(board.get("live"))

    def test_no_matchup_row_this_period_is_not_an_error(self):
        """Preseason, or a week ESPN has not scheduled. Falls back quietly."""
        p = self.page([FakeLeague(4, "Alpha", score=None)],
                      [_board(league_id=4, my_points=403.0)])
        board = asyncio.run(p.live_scoreboards())[0]
        self.assertEqual(board["my_points"], 403.0)

    def test_a_league_with_neither_a_live_nor_a_cached_score_is_absent(self):
        p = self.page([FakeLeague(4, "Alpha", score=RuntimeError("down"))])
        self.assertEqual(asyncio.run(p.live_scoreboards()), [])

    def test_a_live_score_shows_even_with_no_cached_row_behind_it(self):
        """A fresh install should not have to wait for 09:00 tomorrow."""
        p = self.page([FakeLeague(4, "Alpha", score=_score())])
        board = asyncio.run(p.live_scoreboards())[0]
        self.assertEqual(board["my_points"], 444.0)
        self.assertIsNone(board.get("games_cap"))
        # No cap known, so no invented denominator.
        self.assertIn("13 games", web._board(board, None))

    def test_holding_down_refresh_does_not_become_a_request_each_time(self):
        lg = FakeLeague(4, "Alpha", score=_score())
        p = self.page([lg])
        for _ in range(5):
            asyncio.run(p.live_scoreboards())
        self.assertEqual(lg.read.calls, 1)

    def test_the_memo_expires_so_the_score_does_not_freeze(self):
        lg = FakeLeague(4, "Alpha", score=_score())
        p = self.page([lg])
        p.LIVE_TTL = -1.0
        asyncio.run(p.live_scoreboards())
        asyncio.run(p.live_scoreboards())
        self.assertEqual(lg.read.calls, 2)

    def test_a_failure_is_not_retried_on_every_single_load(self):
        """Otherwise a dead cookie makes every page load sit through the
        timeout before showing anything."""
        lg = FakeLeague(4, "Alpha", score=RuntimeError("down"))
        p = self.page([lg], [_board(league_id=4)])
        for _ in range(4):
            asyncio.run(p.live_scoreboards())
        self.assertEqual(lg.read.calls, 1)

    def test_one_league_failing_does_not_take_the_other_down(self):
        p = self.page(
            [FakeLeague(4, "Alpha", score=_score()),
             FakeLeague(9, "Beta", score=RuntimeError("down"))],
            [_board(league_id=9, my_points=111.0)])
        alpha, beta = asyncio.run(p.live_scoreboards())
        self.assertTrue(alpha["live"])
        self.assertEqual(beta["my_points"], 111.0)
        self.assertFalse(beta.get("live"))

    def test_the_panel_says_which_one_the_reader_is_looking_at(self):
        """A timestamp beside a live score invites the reader to date it; a
        live-looking score that is four hours old is the whole failure."""
        self.assertIn("<span class='bwhen fresh'>Live</span>",
                      web._board(_board(live=True), None))
        stale = web._board(_board(), None)
        self.assertIn("as of ", stale)
        self.assertNotIn("fresh", stale)

    def test_a_page_with_no_leagues_configured_asks_espn_nothing(self):
        p = self.page([], [_board(league_id=4)])
        self.assertEqual(len(asyncio.run(p.live_scoreboards())), 1)


class TestLogsDisclosure(unittest.TestCase):
    """The working notes are for when something looks wrong, not for always."""

    def test_notes_are_behind_a_dropdown(self):
        bot = FakeBot(FakeStore(rows=_run_rows([], '["cap 70, used 15"]')))
        out = web.render(web.StatusPage(bot))
        self.assertIn("<details class=logs>", out)
        self.assertIn("Logs (1)", out)
        self.assertIn("cap 70, used 15", out)

    def test_the_moves_are_not_behind_one(self):
        """*"this should be shown, just logs in the dropdown"*.

        The run card used to be the disclosure, so every move it made cost a
        tap to see. The only ``<details>`` in a run is now the logs.
        """
        bot = FakeBot(FakeStore(rows=_run_rows([{
            "kind": "LINEUP", "state": "DONE",
            "rationale": "Karl-Anthony Towns UTIL->BE",
            "payload": LINEUP_PAYLOAD,
        }], '["cap 70, used 15"]')))
        out = web.render(web.StatusPage(bot))
        # Counting the markup, not the stylesheet -- which mentions the tag.
        self.assertEqual(out.count("<details class"), 1)
        self.assertIn("<details class=logs>", out)

    def test_a_run_with_no_notes_has_no_dropdown(self):
        bot = FakeBot(FakeStore(rows=_run_rows([])))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("class=logs", out)
        self.assertIn("no moves proposed", out)


def _runs_band(out: str) -> str:
    """Everything below the Recent runs heading.

    The page now has three bands of league columns -- score, approvals, runs --
    so a count of ``<section class=lg>`` or of a league's name over the whole
    document is three times the thing these tests are about. Each of them is an
    assertion about one band, and this is the band.
    """
    return out.split("Recent runs")[-1]


def _two_leagues():
    """Four runs, the two leagues alternating as they really do."""
    rows = []
    for i, (lid, when) in enumerate([
        (7, "2026-10-22 09:54:18"), (9, "2026-10-22 09:54:00"),
        (7, "2026-10-21 16:02:00"), (9, "2026-10-21 16:01:40"),
    ]):
        rows.append({
            "run_id": "R%d" % i, "league_id": lid, "started_at": when,
            "scoring_period": 3, "shadow_mode": 0,
            "notes": "[]", "shadow_prices": "{}",
        })
    store = FakeStore(rows={"FROM runs ORDER BY": rows,
                            "FROM runs WHERE": rows[:1]})
    return FakeBot(store, leagues=[FakeLeague(9, "Dynassty"),
                                   FakeLeague(7, "The Chairman's Fantasy")])


class TestLeagueColumns(unittest.TestCase):
    """*"can we put dynasty and the other league side by side"*.

    The leagues run a second apart, so one chronological list alternated
    between them and every card had to be read to find out whose it was.
    """

    def test_each_league_gets_one_column(self):
        out = _runs_band(web.render(web.StatusPage(_two_leagues())))
        self.assertEqual(out.count("<section class=lg>"), 2)

    def test_the_league_is_named_once_not_once_per_run(self):
        """Once per band, as a column heading -- not once on every card in it.

        Four runs, two to a league, so a card that named its own league would
        make this two.
        """
        out = _runs_band(web.render(web.StatusPage(_two_leagues())))
        self.assertEqual(out.count("Dynassty"), 1)
        self.assertEqual(out.count("The Chairman&#x27;s Fantasy"), 1)

    def test_column_order_follows_the_configuration(self):
        """Not the run order: both run in the same second, so ordering by
        recency would swap the columns on a refresh."""
        out = web.render(web.StatusPage(_two_leagues()))
        self.assertLess(out.index("Dynassty"),
                        out.index("The Chairman&#x27;s Fantasy"))

    def test_every_run_still_appears(self):
        out = web.render(web.StatusPage(_two_leagues()))
        self.assertEqual(out.count("class='card run'"), 4)

    def test_a_league_no_longer_configured_keeps_its_column(self):
        """Its history does not vanish because it was removed from options."""
        bot = _two_leagues()
        bot.leagues = [FakeLeague(9, "Dynassty")]
        out = _runs_band(web.render(web.StatusPage(bot)))
        self.assertEqual(out.count("<section class=lg>"), 2)
        self.assertIn("league 7", out)


def _pending_bot(*leagues_with_swaps):
    """A bot whose waiting proposals belong to the leagues named.

    Each argument is a ``league_id``; one proposal is queued per argument, so
    passing the same id twice queues two for that league and passing one id
    leaves the other league's column empty.
    """
    pending, run_leagues = [], {}
    for i, lid in enumerate(leagues_with_swaps):
        run_id = "R%d" % i
        run_leagues[run_id] = lid
        pending.append(_proposal(
            action_id="%s:ADD_DROP:0" % run_id, pid=i + 1, run_id=run_id,
            payload={"add": 10 + i, "add_name": "Adds %d" % i,
                     "drop": 20 + i, "drop_name": "Drops %d" % i,
                     "waiver": False},
        ))
    store = FakeStore(pending=pending, run_leagues=run_leagues)
    return FakeBot(store, leagues=[FakeLeague(9, "Dynassty"),
                                   FakeLeague(7, "The Chairman's Fantasy")])


class TestPendingColumns(unittest.TestCase):
    """*"needs approval should be in the sepearate columns please"*.

    Same columns as the runs and in the same order, so a waiting swap sits
    above the runs of the league it came from.
    """

    def test_each_league_gets_a_column(self):
        out = web.render(web.StatusPage(_pending_bot(9, 7)))
        self.assertEqual(out.count("<section class=lg>"), 2)

    def test_a_league_with_nothing_waiting_keeps_its_column(self):
        """Otherwise one league's swap slides under the other's heading."""
        out = web.render(web.StatusPage(_pending_bot(7)))
        self.assertEqual(out.count("<section class=lg>"), 2)
        self.assertIn("Nothing waiting.", out)

    def test_the_column_order_matches_the_configuration(self):
        out = web.render(web.StatusPage(_pending_bot(7, 9)))
        self.assertLess(out.index("Dynassty"),
                        out.index("The Chairman&#x27;s Fantasy"))

    def test_the_card_no_longer_repeats_the_league(self):
        """The heading says it; the eyebrow spends that slot on the kind."""
        out = web.render(web.StatusPage(_pending_bot(9, 9)))
        self.assertEqual(out.count("Dynassty"), 1)
        self.assertEqual(out.count("<span class=league>Add / drop</span>"), 2)

    def test_both_swaps_keep_their_own_buttons(self):
        out = web.render(web.StatusPage(_pending_bot(9, 7)))
        self.assertIn("value='R0:ADD_DROP:0'", out)
        self.assertIn("value='R1:ADD_DROP:0'", out)

    def test_an_empty_queue_says_so_once_per_league(self):
        """*"this should show two cards on each row saying no updates for each
        league"*.

        It used to be one full-width card, which answered "is anything
        waiting" but not "is anything waiting *in this league*" -- and with two
        leagues that is the question being asked. The columns also have to hold
        their positions whether or not the queue is empty, because the score
        above and the runs below are the same columns.
        """
        out = web.render(web.StatusPage(_pending_bot()))
        self.assertEqual(out.count("<section class=lg>"), 2)
        self.assertEqual(out.count("Nothing waiting.</b></div>"), 2)

    def test_the_explanation_is_said_once_and_not_per_column(self):
        """Why the queue is empty belongs under the heading.

        Only add/drop needs a human, so an empty queue right after a
        notification that said "2 move(s)" reads as a broken page. That
        sentence has to survive the split into per-league cards -- but said
        twice in two cards it reads as boilerplate rather than as how the thing
        works.
        """
        out = web.render(web.StatusPage(_pending_bot()))
        self.assertEqual(out.count("only add/drop asks first"), 1)

    def test_a_column_still_appears_with_no_leagues_configured(self):
        """A fresh install, before the options are filled in.

        There is no column to put the card in, so the heading would otherwise
        be followed by nothing at all.
        """
        bot = FakeBot(FakeStore(pending=[], run_leagues={}), leagues=[])
        out = web.render(web.StatusPage(bot))
        self.assertIn("Nothing waiting.", out)


class TestStateVocabulary(unittest.TestCase):
    """The page invented ``EXECUTED``, which is ESPN's word and not ours.

    Nothing failed: an unknown state falls through to the neutral chip, so
    every completed run was rendered in grey and looked, correctly but
    uselessly, like every other state.
    """

    def test_the_page_only_names_states_the_store_can_write(self):
        import state
        known = {v for k, v in vars(state).items()
                 if k.isupper() and isinstance(v, str) and v == k}
        self.assertTrue(set(web.STATE_TAGS) <= known,
                        set(web.STATE_TAGS) - known)

    def test_every_state_the_store_can_write_has_a_colour(self):
        import state
        for name in (state.PROPOSED, state.APPROVED, state.EXECUTING,
                     state.DONE, state.FAILED, state.EXPIRED, state.REJECTED):
            self.assertIn(name, web.STATE_TAGS)

    def test_a_completed_write_is_the_one_the_page_calls_done(self):
        import state
        self.assertEqual(web.DONE, state.DONE)


class TestRenderUrls(unittest.TestCase):
    """Ingress serves the app under /api/hassio_ingress/<token>/.

    An absolute URL escapes that prefix and 404s against the frontend, and it
    does so only on the device -- never in a local test that hits the app
    directly. So the assertion is on the emitted markup.
    """

    def test_form_action_is_relative(self):
        bot = FakeBot(FakeStore(pending=[_proposal()]))
        out = web.render(web.StatusPage(bot))
        self.assertIn("action=decide", out)
        self.assertNotIn("action=/decide", out)
        self.assertNotIn('action="/', out)

    def test_no_absolute_paths_anywhere(self):
        bot = FakeBot(FakeStore(pending=[_proposal()]))
        out = web.render(web.StatusPage(bot))
        for attr in ("href=/", "src=/", "action=/"):
            self.assertNotIn(attr, out, "absolute URL would escape the prefix")


def _oops_page(page):
    """The failed-read page, which is markup no happy-path render reaches."""
    return web.render_team(page, {"league": "A", "error": "Nope."})


class TestCssScoping(unittest.TestCase):
    """No bare class selector may match an element nested inside a component.

    The page ships one stylesheet with no build step and no scoping, so a
    generic rule is global by construction. ``.top`` was the masthead's and
    also the header row of every run and every applied move; its ``margin:
    0 0 26px`` landed under each player's name in the log, which is how this
    was noticed. Renaming the masthead to ``.mast`` fixed it, and this test
    is what stops the next generic name doing the same.
    """

    #: Class names used in more than one place in the markup, so a rule on
    #: them alone is a rule on all of them.
    SHARED = ("top", "name", "body", "note", "head", "meta", "slots")

    #: Properties that place a box. A shared name may safely carry inherited
    #: typography -- that is what a utility class like ``.meta`` is for, and
    #: three dim 12px captions in three components is the intent, not a leak.
    #: These are the ones that move things, and so the ones that leak.
    LAYOUT = ("margin", "padding", "display", "position", "gap", "flex",
              "width", "height", "justify-content", "align-items", "float",
              "grid")

    def test_shared_class_names_carry_no_unscoped_layout(self):
        for cls in self.SHARED:
            # A bare ".cls {" at the start of a selector -- as opposed to
            # ".parent .cls", which is scoped and therefore fine.
            for m in re.finditer(r"(?m)^\.%s\s*\{([^}]*)\}" % cls, web.CSS):
                for prop in self.LAYOUT:
                    self.assertNotRegex(
                        m.group(1), r"(?m)(^|[;\s])%s\s*:" % prop,
                        "'.%s' sets '%s' unscoped; it appears in more than one "
                        "component, so that box will be placed in all of them"
                        % (cls, prop))

    def test_every_class_in_the_markup_is_one_the_stylesheet_knows(self):
        # Catches the other half of a rename: markup updated, CSS not, or the
        # reverse. Only flags classes the stylesheet never mentions at all.
        #
        # All three pages, because they share one stylesheet: a class renamed
        # for the index that the lineup editor still emits would show up as a
        # row with no styling at all, on the page nobody runs the tests for.
        bot = FakeBot(FakeStore(pending=[_proposal()], boards=[_board()]),
                      leagues=[FakeLeague(9, "A"), FakeLeague(7, "B")])
        page = web.StatusPage(bot)
        pages = [
            web.render(page),
            web.render_matchup(page, _matchup_view(
                [_pl(1, "A", 0, locked=True, status="out", status_word="Out")],
                [_pl(2, "B", 0, points=3.0)])),
            web.render_team(page, _team_view(
                [_pl(1, "A", 0, locked=True), _pl(2, "B", 12)]),
                note="Saved.", bad=True),
            _oops_page(page),
        ]
        used = set()
        for out in pages:
            for attr in re.findall(
                    r"class=(?:'([^']*)'|\"([^\"]*)\"|([^\s>]+))", out):
                used.update((attr[0] or attr[1] or attr[2]).split())
        for cls in sorted(used):
            if cls in self.HOOKS:
                continue
            self.assertIn(".%s" % cls, web.CSS,
                          "markup uses class '%s' that the stylesheet never "
                          "mentions -- a rename that missed a side" % cls)

    #: Classes the script finds elements by and the stylesheet has no opinion
    #: about. ``held`` is on a hidden input, which is invisible by nature, so
    #: a styling rule for it would be a lie told to pass a test.
    HOOKS = ("held",)

    def test_every_unstyled_class_is_one_the_script_actually_uses(self):
        """What stops the exemption above becoming a place to hide a typo.

        A class with no rule in the stylesheet is either a script hook or a
        mistake, and the only evidence for the first is the script.
        """
        for cls in self.HOOKS:
            self.assertIn(".%s" % cls, web.DRAG_JS,
                          "'%s' is exempt from the stylesheet check but the "
                          "script does not use it either" % cls)


class TestStatusPage(unittest.TestCase):
    def test_empty_page_renders(self):
        """The common case on a fresh install: no runs, no proposals."""
        out = web.render(web.StatusPage(FakeBot()))
        self.assertIn("Nothing waiting.", out)
        self.assertIn("No runs recorded yet.", out)

    def test_non_approvable_kinds_are_not_offered_a_button(self):
        """A LINEUP in PROPOSED means auto_approve was set wrongly.

        Rendering Approve for it would claim the move has not happened yet,
        when an autonomous kind is written and executed in the same breath.
        """
        store = FakeStore(pending=[_proposal(kind="LINEUP")])
        page = web.StatusPage(FakeBot(store))
        with self.assertLogs("autopilot.web", level="WARNING"):
            self.assertEqual(page.pending(), [])

    def test_league_label_comes_from_config(self):
        bot = FakeBot(
            FakeStore(
                pending=[_proposal()],
                rows={"league_id FROM runs": [{"league_id": 7}]},
            ),
            leagues=[FakeLeague(7, "Dynassty")],
        )
        self.assertEqual(web.StatusPage(bot).pending()[0]["league"], "Dynassty")

    def test_unknown_league_does_not_raise(self):
        """A run from a league since removed from config still has to render."""
        bot = FakeBot(FakeStore(pending=[_proposal()]), leagues=[])
        self.assertIn("league", web.StatusPage(bot).pending()[0]["league"])

    def test_shadow_banner_tracks_config(self):
        on = web.render(web.StatusPage(FakeBot(cfg=FakeCfg(shadow_mode=True))))
        off = web.render(web.StatusPage(FakeBot(cfg=FakeCfg(shadow_mode=False))))
        self.assertIn("Shadow mode", on)
        self.assertNotIn("Shadow mode", off)

    def test_stuck_writes_are_surfaced(self):
        bot = FakeBot(FakeStore(stuck=[{"id": 3}, {"id": 4}]))
        out = web.render(web.StatusPage(bot))
        self.assertIn("2 writes in an unknown state", out)

    def test_malformed_stored_json_does_not_break_the_page(self):
        """`notes` is written by us, but a half-written row must still render."""
        bot = FakeBot(FakeStore(rows={
            "FROM runs": [{
                "run_id": "R1", "league_id": 1, "started_at": "x",
                "scoring_period": 1, "shadow_mode": 1,
                "notes": "{not json", "shadow_prices": None,
            }],
        }))
        out = web.render(web.StatusPage(bot))
        self.assertIn("no moves proposed", out)


class TestApplyDecision(unittest.TestCase):
    def test_non_owner_is_refused_before_the_state_machine(self):
        """The refusal must happen first, not be undone afterwards."""
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        with self.assertLogs("autopilot.web", level="WARNING"):
            refusal = asyncio.run(web.apply_decision(
                bot, action_id="R1:ADD:0", verb="APPROVE",
                user_id="someone-else",
            ))
        self.assertEqual(refusal, web.NOT_OWNER)
        self.assertEqual(bot.store.decisions, [], "decide() must not be called")
        self.assertEqual(bot.executed, 0)

    def test_missing_user_id_fails_closed(self):
        """If the ingress header name is ever wrong, refuse rather than allow."""
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        with self.assertLogs("autopilot.web", level="WARNING"):
            refusal = asyncio.run(web.apply_decision(
                bot, action_id="R1:ADD:0", verb="APPROVE", user_id="",
            ))
        self.assertEqual(refusal, web.NOT_OWNER)
        self.assertEqual(bot.store.decisions, [])

    def test_owner_approval_executes(self):
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        refusal = asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="APPROVE", user_id="owner-abc",
        ))
        self.assertIsNone(refusal)
        self.assertEqual(bot.store.decisions, [("R1:ADD:0", True, "owner-abc")])
        self.assertEqual(bot.executed, 1)

    def test_rejection_does_not_execute(self):
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="REJECT", user_id="owner-abc",
        ))
        self.assertEqual(bot.store.decisions, [("R1:ADD:0", False, "owner-abc")])
        self.assertEqual(bot.executed, 0, "a rejection must write nothing to ESPN")

    def test_unknown_verb_is_not_an_approval(self):
        """Anything that is not APPROVE is a rejection; the button is the only
        source of the verb, so a mangled one must not fall through to approve."""
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="approve-ish", user_id="owner-abc",
        ))
        self.assertEqual(bot.store.decisions[0][1], False)
        self.assertEqual(bot.executed, 0)

    def test_verb_is_case_insensitive(self):
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="approve", user_id="owner-abc",
        ))
        self.assertEqual(bot.executed, 1)

    def test_stale_decision_does_not_reach_the_executor(self):
        """`decide` returning None means the row was not in PROPOSED.

        Calling `execute_approved` anyway would be harmless today, because the
        claim is still guarded -- but it would make this page's correctness
        depend on a guard two layers down instead of on its own logic.
        """
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        bot.store.decide_result = None
        refusal = asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="APPROVE", user_id="owner-abc",
        ))
        self.assertIsNone(refusal)
        self.assertEqual(bot.executed, 0)

    def test_unset_owner_allows_the_decision(self):
        """`ha_owner_user_id` is optional; ingress has already authenticated."""
        bot = FakeBot(cfg=FakeCfg(owner=""))
        refusal = asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="APPROVE", user_id="whoever",
        ))
        self.assertIsNone(refusal)
        self.assertEqual(bot.executed, 1)


class FakeRequest:
    """Only what `read_form` touches, which is the point of it being module-level."""

    def __init__(self, raw: bytes, content_length=None):
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        elif content_length is None and raw is not None:
            self.headers["content-length"] = str(len(raw))
        self._raw = raw

    async def body(self):
        return self._raw


class TestReadForm(unittest.TestCase):
    """`request.form()` cannot be used; see `web.read_form`.

    Starlette asserts on `python-multipart` for *every* content type, not just
    multipart, so a plain urlencoded body 500s. That was found on the device.
    These tests are the local version of that discovery, and they need neither
    FastAPI nor the package in question.
    """

    def test_parses_the_two_fields(self):
        form = asyncio.run(web.read_form(
            FakeRequest(b"action_id=R1%3AADD%3A0&verb=APPROVE")
        ))
        self.assertEqual(form, {"action_id": "R1:ADD:0", "verb": "APPROVE"})

    def test_missing_field_is_absent_not_empty(self):
        form = asyncio.run(web.read_form(FakeRequest(b"verb=APPROVE")))
        self.assertNotIn("action_id", form)

    def test_first_value_wins(self):
        """Matches Starlette's `FormData.get`, and is the safer verb to take."""
        form = asyncio.run(web.read_form(
            FakeRequest(b"verb=REJECT&verb=APPROVE")
        ))
        self.assertEqual(form["verb"], "REJECT")

    def test_empty_body(self):
        self.assertEqual(asyncio.run(web.read_form(FakeRequest(b""))), {})

    def test_declared_oversize_is_refused_without_reading(self):
        class Trap(FakeRequest):
            async def body(self):
                raise AssertionError("body must not be read past the cap")

        with self.assertLogs("autopilot.web", level="WARNING"):
            form = asyncio.run(web.read_form(
                Trap(b"x", content_length=web.MAX_FORM_BYTES + 1)
            ))
        self.assertEqual(form, {})

    def test_undeclared_oversize_is_refused(self):
        """A chunked body declares no length, so the cap is checked twice."""
        raw = b"verb=" + b"A" * (web.MAX_FORM_BYTES + 1)
        with self.assertLogs("autopilot.web", level="WARNING"):
            form = asyncio.run(web.read_form(FakeRequest(raw, content_length=0)))
        self.assertEqual(form, {})

    def test_garbage_content_length_does_not_raise(self):
        form = asyncio.run(web.read_form(
            FakeRequest(b"verb=APPROVE", content_length="not-a-number")
        ))
        self.assertEqual(form["verb"], "APPROVE")

    def test_undecodable_bytes_do_not_raise(self):
        form = asyncio.run(web.read_form(FakeRequest(b"verb=\xff\xfe")))
        self.assertIn("verb", form)

    def test_a_decision_from_a_parsed_form_is_a_rejection(self):
        """End to end on the two functions the route composes.

        A body with no recognizable verb must not approve anything -- this is
        the composition the route performs, with the HTTP layer removed.
        """
        form = asyncio.run(web.read_form(FakeRequest(b"action_id=R1%3AADD%3A0")))
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        asyncio.run(web.apply_decision(
            bot,
            action_id=str(form.get("action_id") or ""),
            verb=str(form.get("verb") or ""),
            user_id="owner-abc",
        ))
        self.assertEqual(bot.store.decisions, [("R1:ADD:0", False, "owner-abc")])
        self.assertEqual(bot.executed, 0)


class TestAnnotationsAreResolvable(unittest.TestCase):
    """`from __future__ import annotations` in web.py breaks every page load.

    FastAPI resolves a route handler's annotations with ``get_type_hints``,
    which consults *module* globals. ``Request`` is imported inside
    ``build_app`` so this file stays importable without FastAPI, so under that
    future import the name is a string that resolves nowhere -- and FastAPI's
    fallback for a parameter it cannot type is a required **query** parameter.
    Every GET then returns ``422 {"loc": ["query", "request"]}``.

    This happened. It was found on the device, through the ingress proxy,
    because nothing in this file imports FastAPI and so nothing here exercises
    the route layer. This test is the cheap proxy for the expensive discovery:
    the future import binds the name ``annotations`` in the module namespace,
    which is observable without FastAPI being installed at all.
    """

    def test_module_does_not_use_postponed_annotations(self):
        self.assertIsNone(
            getattr(web, "annotations", None),
            "web.py must not use `from __future__ import annotations`; "
            "see this test's docstring",
        )

    def test_route_annotations_are_not_string_literals(self):
        """The other half of the same mistake: quoting the annotation by hand.

        Removing the future import is not enough if a handler is written
        ``request: "Request"`` -- a string literal stays a string, and
        ``get_type_hints`` fails on it identically.
        """
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "rootfs", "app", "web.py",
        )
        with open(path) as fh:
            source = fh.read()
        self.assertNotIn('request: "Request"', source)
        self.assertIn("request: Request", source)


class TestStoreQuery(unittest.TestCase):
    """`Store.query` exists so the page never touches `store.db` unlocked.

    A run executes on a worker thread, so an unlocked cursor on the one shared
    connection would interleave with its writes.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_returns_plain_dicts(self):
        self.store.start_run("R1", 5, True, league_id=7)
        rows = self.store.query("SELECT * FROM runs")
        self.assertEqual(len(rows), 1)
        self.assertIs(type(rows[0]), dict)
        self.assertEqual(rows[0]["league_id"], 7)

    def test_takes_the_lock(self):
        held = []

        class Watch:
            def __enter__(inner):
                held.append(True)
                return inner

            def __exit__(inner, *a):
                held.append(False)
                return False

        self.store._lock = Watch()
        self.store.query("SELECT 1")
        self.assertEqual(held, [True, False], "query() must hold the store lock")

    def test_page_does_not_touch_the_connection_directly(self):
        """Guards the whole point of the helper against a future edit."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "rootfs", "app", "web.py",
        )
        with open(path) as fh:
            self.assertNotIn("store.db", fh.read())


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The matchup sheet and the lineup editor.
# ---------------------------------------------------------------------------


def _body(out: str) -> str:
    """The markup, without the stylesheet.

    Every class name this page can use is also a selector in the ``<style>``
    block, so ``assertNotIn("lgpills", out)`` is always false and says nothing.
    """
    return out.split("</style>")[-1]


def _pl(pid, name, slot, *, points=None, locked=False, eligible=None,
        team="CHA", opp="", tip="", status="", status_word=""):
    """One row of a rendered roster, in the shape ``_row`` produces."""
    return {
        "pid": pid, "name": name, "slot": slot, "team": team,
        "opp": opp, "tip": tip, "points": points, "locked": locked,
        "status": status, "status_word": status_word,
        "eligible": sorted(eligible if eligible is not None else [slot, 11, 12]),
    }


#: One starter per position, four UTIL, three bench, one IR -- the shape of the
#: live league this was built against, trimmed.
SLOTS = {0: 1, 1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 11: 4, 12: 3, 13: 1}


def _team_view(roster, **over):
    view = {
        "league": "Dynassty", "league_id": 9, "team_id": 7, "period": 5,
        "day": "Sat, Oct 24", "slot_counts": dict(SLOTS), "roster": roster,
    }
    view.update(over)
    return view


def _days(on=5, scores=((4, 110.0, 92.0), (5, None, None), (6, None, None))):
    return [{"sp": sp, "label": "D%d" % sp, "mine": a, "theirs": b,
             "on": sp == on, "today": sp == 5} for sp, a, b in scores]


def _matchup_view(mine, theirs, **over):
    view = {
        "league": "Dynassty", "league_id": 9, "period": 5,
        "day": "Sat, Oct 24", "today": True,
        "days": _days(), "week": 2, "week_span": "Oct 20\u201325",
        "this_week": True, "prev_week": 1, "next_week": None,
        "slot_counts": dict(SLOTS), "games_cap": 44,
        "score": MatchupScore(
            matchup_period=1, my_team_id=7, my_points=834.0,
            opp_team_id=6, opp_name="Theirs", opp_points=749.5,
            my_name="Mine", my_games_used=27, opp_games_used=22,
        ),
        "mine": mine, "theirs": theirs,
    }
    view.update(over)
    return view


def _page():
    return web.StatusPage(FakeBot(leagues=[FakeLeague(9, "Dynassty"),
                                           FakeLeague(7, "Chairman's")]))


class TestRowHelpers(unittest.TestCase):
    """The small decisions, each of which is a thing the page would get wrong."""

    def test_a_missing_score_is_a_dash_and_a_zero_is_a_zero(self):
        """*He has not played* and *he played and did nothing* are not the same.

        ESPN publishes no row at all for the first and a 0.0 row for the
        second, and the whole reason ``DayEntry.points`` is Optional is to
        keep them apart all the way to the screen.
        """
        self.assertIn("&mdash;", web._points(None))
        self.assertIn("0.0", web._points(0.0))
        self.assertNotIn("&mdash;", web._points(0.0))

    def test_a_scored_number_is_marked_so_it_can_be_inked_differently(self):
        self.assertIn("scored", web._points(31.5))
        self.assertNotIn("scored", web._points(None))

    def test_an_initial_replaces_a_first_name_but_never_the_whole_name(self):
        self.assertEqual(web._short_name("LaMelo Ball"), "L. Ball")
        self.assertEqual(web._short_name("Jaren Jackson Jr."), "J. Jackson Jr.")
        # One word is all the name there is; an initial would erase him.
        self.assertEqual(web._short_name("Wembanyama"), "Wembanyama")
        self.assertEqual(web._short_name(""), "")

    def test_the_meta_line_drops_his_own_team_only_when_there_is_a_fixture(self):
        with_game = web._meta(_pl(1, "X", 0, team="MIL", opp="@PHI",
                                  tip="4:30 pm"))
        self.assertIn("@PHI", with_game)
        self.assertIn("4:30 pm", with_game)
        self.assertNotIn("MIL", with_game)
        # No fixture: the team is the only thing left worth saying.
        idle = web._meta(_pl(1, "X", 0, team="MIL"))
        self.assertIn("MIL", idle)
        self.assertIn("no game", idle)

    def test_an_injury_takes_the_line_from_the_fixture(self):
        out = web._meta(_pl(1, "X", 0, team="SAS", opp="@DAL", tip="5:00 pm",
                            status="dtd", status_word="Day-to-day"))
        self.assertIn("Day-to-day", out)
        self.assertNotIn("@DAL", out)
        self.assertIn("dtd", out)

    def test_the_meta_separator_survives_escaping_and_espn_text_does_not(self):
        """The entity is ours; the abbreviation is ESPN's and is not trusted."""
        out = web._meta(_pl(1, "X", 0, team="<b>CHA"))
        self.assertIn("&middot;", out)
        self.assertNotIn("&amp;middot;", out)
        self.assertNotIn("<b>CHA", out)

    def test_a_slot_with_capacity_four_gets_four_seats(self):
        seats = web._slot_seats(SLOTS)
        self.assertEqual(seats.count(11), 4)
        self.assertEqual(seats.count(0), 1)
        self.assertEqual(len(seats), sum(SLOTS.values()))

    def test_bench_and_ir_sort_after_every_starting_slot(self):
        order = web._slot_order(SLOTS)
        self.assertEqual(order[-2:], [12, 13])
        self.assertEqual(order[0], 0)

    def test_taking_a_seat_hands_out_a_different_player_each_time(self):
        """Four UTIL seats must not all be filled by the first UTIL player."""
        rows = [_pl(1, "A", 11), _pl(2, "B", 11), _pl(3, "C", 0)]
        self.assertEqual(web._seat(rows, 11)["pid"], 1)
        self.assertEqual(web._seat(rows, 11)["pid"], 2)
        self.assertIsNone(web._seat(rows, 11))
        self.assertEqual([r["pid"] for r in rows], [3])


class TestMatchupSheet(unittest.TestCase):
    """*"it should show the matchup, full list of whos playing me against
    other team"*."""

    def test_the_two_rosters_are_paired_by_lineup_slot(self):
        out = web.render_matchup(_page(), _matchup_view(
            [_pl(1, "My Guard", 0)], [_pl(2, "Their Guard", 0)]))
        row = out.split("<div class=mrow>")[1]
        # Abbreviated: the sheet is two rosters wide. See ``_short_name``.
        self.assertLess(row.index("M. Guard"), row.index("PG"))
        self.assertLess(row.index("PG"), row.index("T. Guard"))

    def test_a_slot_the_other_side_has_not_filled_is_a_dash_not_a_missing_cell(self):
        out = web.render_matchup(_page(), _matchup_view(
            [_pl(1, "Mine", 0)], []))
        self.assertEqual(out.count("<div class=mrow>"), len(web._slot_seats(SLOTS)) - 4)

    def test_the_day_total_adds_up_only_what_espn_has_scored(self):
        out = web.render_matchup(_page(), _matchup_view(
            [_pl(1, "Played", 0, points=31.5),
             _pl(2, "Also played", 1, points=10.0),
             _pl(3, "Not yet", 2)],
            [_pl(4, "Theirs", 0, points=5.0)]))
        tot = out.split("<div class='mrow tot'>")[1]
        self.assertIn("41.5", tot)
        self.assertIn("5.0", tot)

    def test_a_day_nobody_has_played_totals_to_a_dash_not_to_zero(self):
        """0.0 would claim both teams had turned up and scored nothing."""
        out = web.render_matchup(_page(), _matchup_view(
            [_pl(1, "Mine", 0)], [_pl(2, "Theirs", 0)]))
        tot = out.split("<div class='mrow tot'>")[1]
        self.assertNotIn("0.0", tot)
        self.assertEqual(tot.count("&mdash;"), 2)

    def test_the_lead_is_said_in_words_and_not_just_in_colour(self):
        out = web.render_matchup(_page(), _matchup_view([], []))
        self.assertIn("Ahead by 84.5", out)

    def test_a_bye_is_not_a_win(self):
        view = _matchup_view([], [], score=MatchupScore(
            matchup_period=1, my_team_id=7, my_points=100.0,
            opp_team_id=None, opp_name="", opp_points=0.0, my_name="Mine"))
        out = web.render_matchup(_page(), view)
        self.assertIn("Bye week", out)
        self.assertNotIn("Ahead by", out)

    def test_the_day_strip_links_to_every_other_day_of_the_week(self):
        out = web.render_matchup(_page(), _matchup_view([], []))
        self.assertIn("matchup?league=9&amp;sp=4", out)
        self.assertIn("matchup?league=9&amp;sp=6", out)

    def test_the_day_being_looked_at_is_not_a_link_to_itself(self):
        out = web.render_matchup(_page(), _matchup_view([], []))
        self.assertIn("<span class='day on' aria-current=date>", out)
        self.assertNotIn("matchup?league=9&amp;sp=5", out)

    def test_each_chip_carries_what_both_sides_scored_that_day(self):
        """The point of the strip: which day the week was decided on, visible
        before anything is tapped."""
        out = web.render_matchup(_page(), _matchup_view([], []))
        chip = out.split("<nav class=days>")[1].split("</nav>")[0]
        self.assertIn("110", chip)
        self.assertIn("92", chip)

    def test_a_day_espn_has_published_nothing_for_is_a_dash(self):
        out = web.render_matchup(_page(), _matchup_view([], []))
        chip = out.split("<nav class=days>")[1].split("</nav>")[0]
        self.assertEqual(chip.count("&mdash;"), 2)   # the two unplayed days

    def test_a_week_with_one_day_falls_back_to_naming_it(self):
        """No calendar means no week to lay out, and a strip of one chip is a
        label pretending to be a control."""
        out = web.render_matchup(_page(), _matchup_view(
            [], [], days=_days(scores=((5, None, None),))))
        self.assertNotIn("<nav class=days>", out)
        self.assertIn("Sat, Oct 24", out)

    def test_a_failed_read_says_so_and_offers_the_way_back(self):
        out = web.render_matchup(_page(), {"league": "Dynassty",
                                           "error": "Could not read it."})
        self.assertIn("Could not read it.", out)
        self.assertIn("class=back", out)
        self.assertIn("Nothing has been changed.", out)

    def test_a_player_name_is_escaped_here_too(self):
        out = web.render_matchup(_page(), _matchup_view(
            [_pl(1, "O'<script>alert(1)</script> Neal", 0)], []))
        self.assertNotIn("<script>alert", out)


class TestLineupEditor(unittest.TestCase):
    """*"this is where you can drag and drop players and manually update and
    save"*.

    The invariant that matters most here is totality: ``set_lineup`` refuses a
    partial map outright, so a player this page forgets to give a field is not
    a cosmetic bug -- it is a 403 on every save until somebody notices.
    """

    def _out(self, roster, **over):
        return web.render_team(_page(), _team_view(roster, **over))

    def test_every_rostered_player_gets_a_field(self):
        roster = [_pl(i, "P%d" % i, 11 if i < 4 else 12) for i in range(8)]
        out = self._out(roster)
        for i in range(8):
            self.assertIn("name='slot_%d'" % i, out,
                          "a player with no field makes the write partial, "
                          "which the preflight refuses whole")

    def test_a_player_in_a_slot_the_seats_did_not_cover_is_still_given_one(self):
        """More UTILs than the league has room for: shown, not dropped."""
        roster = [_pl(i, "P%d" % i, 11) for i in range(6)]
        out = self._out(roster)
        self.assertIn("Unplaced", out)
        for i in range(6):
            self.assertIn("name='slot_%d'" % i, out)

    def test_a_locked_player_cannot_be_edited_but_is_still_submitted(self):
        """A disabled control posts nothing, and a lineup short one player is
        refused whole. The hidden twin is what keeps the map total."""
        out = self._out([_pl(1, "Tipped off", 0, locked=True)])
        self.assertIn("disabled", out)
        self.assertIn("type=hidden class=held name='slot_1' value='0'", out)
        self.assertIn("data-locked='1'", out)

    def test_an_unlocked_player_gets_exactly_one_field(self):
        out = self._out([_pl(1, "Free", 0)])
        self.assertEqual(out.count("name='slot_1'"), 1)

    def test_the_slot_menu_offers_only_slots_he_is_eligible_for(self):
        out = self._out([_pl(1, "Centre", 4, eligible=[4, 11, 12])])
        row = out.split("name='slot_1'")[1].split("</select>")[0]
        self.assertIn(">C<", row)
        self.assertIn(">UTIL<", row)
        self.assertNotIn(">PG<", row)

    def test_a_slot_the_league_does_not_have_is_not_offered(self):
        out = self._out([_pl(1, "X", 0, eligible=[0, 8, 11, 12])],
                        slot_counts={0: 1, 11: 1, 12: 1})
        row = out.split("name='slot_1'")[1].split("</select>")[0]
        self.assertNotIn("G/F", row)

    def test_his_current_slot_is_always_offered_even_if_it_looks_ineligible(self):
        """Otherwise the select would silently propose moving him on save."""
        out = self._out([_pl(1, "X", 4, eligible=[11, 12])])
        row = out.split("name='slot_1'")[1].split("</select>")[0]
        self.assertIn("<option value='4' selected>C</option>", row)

    def test_the_drag_script_is_told_what_is_legal(self):
        out = self._out([_pl(1, "X", 0, eligible=[0, 5, 11, 12])])
        self.assertIn("data-eligible='0 5 11 12'", out)

    def test_an_empty_seat_is_rendered_so_a_drag_has_somewhere_to_land(self):
        out = self._out([_pl(1, "X", 0)])
        self.assertIn("Empty", out)
        self.assertEqual(out.count("class=slotrow "),
                         len(web._slot_seats(SLOTS)) - 1)

    def test_the_posted_period_is_the_one_that_was_shown(self):
        """A page left open across midnight must fail on a stale period rather
        than quietly write tomorrow's lineup."""
        self.assertIn("name=sp value='5'", self._out([]))

    def test_a_refused_save_is_reported_above_the_roster(self):
        out = self._out([_pl(1, "X", 0)], )
        self.assertNotIn("class='card banner", out)
        out = web.render_team(_page(), _team_view([_pl(1, "X", 0)]),
                              note="ESPN refused that lineup: no.", bad=True)
        self.assertIn("ESPN refused that lineup: no.", out)
        self.assertIn("banner bad", out)
        self.assertLess(out.index("ESPN refused"), out.index("name='slot_1'"))

    def test_a_player_name_cannot_break_out_of_the_aria_label(self):
        out = self._out([_pl(1, "' onfocus='alert(1)", 0)])
        self.assertNotIn("onfocus='alert(1)", out)

    def test_one_league_gets_no_league_strip(self):
        page = web.StatusPage(FakeBot(leagues=[FakeLeague(9, "Only one")]))
        self.assertNotIn("lgpills", _body(web.render_team(page, _team_view([]))))

    def test_two_leagues_get_a_strip_with_this_one_lit(self):
        out = self._out([])
        self.assertIn("<a class='pill on' href='team?league=9'>", out)
        self.assertIn("<a class='pill' href='team?league=7'>", out)


class TestDetailPageUrls(unittest.TestCase):
    """Ingress serves this app under a rotating prefix, so every URL it emits
    has to be relative. The two new pages emit several each."""

    def _pages(self):
        page = _page()
        return [
            web.render(page),
            web.render_matchup(page, _matchup_view([_pl(1, "A", 0)],
                                                   [_pl(2, "B", 0)])),
            web.render_team(page, _team_view([_pl(1, "A", 0)])),
        ]

    def test_no_absolute_paths_on_any_page(self):
        for out in self._pages():
            for attr in ("href=/", "src=/", "action=/", 'href="/', 'action="/'):
                self.assertNotIn(attr, out,
                                 "absolute URL would escape the ingress prefix")

    def test_the_lineup_form_posts_relatively(self):
        out = web.render_team(_page(), _team_view([]))
        self.assertIn("action=lineup", out)

    def test_the_scoreboard_panel_links_to_its_matchup(self):
        bot = FakeBot(FakeStore(boards=[_board(league_id=9)]),
                      leagues=[FakeLeague(9, "Dynassty")])
        out = web.render(web.StatusPage(bot))
        self.assertIn("<a class=board href='matchup?league=9'>", out)

    def test_a_panel_with_no_league_stays_an_inert_section(self):
        """Only reachable from a row written before the column existed, and a
        link to ``matchup?league=None`` would be a link to an error page."""
        board = _board()
        board.pop("league_id")
        bot = FakeBot(FakeStore(boards=[board]))
        out = web.render(web.StatusPage(bot))
        self.assertIn("<section class=board>", out)
        self.assertNotIn("matchup?league=None", out)


class FakeWrite:
    """``set_lineup`` and nothing else, which is all the save path calls."""

    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def set_lineup(self, target, scoring_period=None, **kw):
        self.calls.append((dict(target), scoring_period))
        if self.raises is not None:
            raise self.raises
        return self.result or _write_result()


class _WriteResult:
    def __init__(self, items=(), shadow=False):
        self.items = tuple(items)
        self.shadow = shadow
        self.ok = True


def _write_result(items=(({"playerId": 1},)), shadow=False):
    return _WriteResult(items, shadow)


def _writing_bot(owner="owner", **kw):
    lg = FakeLeague(9, "Dynassty")
    lg.write = FakeWrite(**kw)
    return FakeBot(cfg=FakeCfg(owner=owner), leagues=[lg]), lg


class TestLineupAuthorization(unittest.TestCase):
    """Who may rewrite the roster.

    Ingress has already authenticated the request against Home Assistant; what
    is decided here is *which* authenticated user. Other members of the
    household must not be able to bench somebody, and this must fail closed
    when ingress reports no id at all -- the same rule, and the same failure
    mode, as the approve buttons.
    """

    def test_a_stranger_is_refused_before_anything_is_written(self):
        bot, lg = _writing_bot()
        note, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period="5", slots={"slot_1": "0"},
            user_id="someone-else"))
        self.assertTrue(bad)
        self.assertEqual(note, web.NOT_OWNER)
        self.assertEqual(lg.write.calls, [])

    def test_a_missing_identity_is_refused_too(self):
        bot, lg = _writing_bot()
        note, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period="5", slots={"slot_1": "0"}, user_id=""))
        self.assertTrue(bad)
        self.assertEqual(lg.write.calls, [])

    def test_the_owner_gets_through(self):
        bot, lg = _writing_bot()
        note, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period="5", slots={"slot_1": "0"},
            user_id="owner"))
        self.assertFalse(bad)
        self.assertEqual(lg.write.calls, [({1: 0}, 5)])

    def test_an_unconfigured_owner_does_not_lock_the_page_out(self):
        """Matches ``apply_decision``: no owner set means no owner check."""
        bot, lg = _writing_bot(owner="")
        _, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period="5", slots={"slot_1": "0"},
            user_id="anybody"))
        self.assertFalse(bad)
        self.assertEqual(len(lg.write.calls), 1)

    def test_a_league_that_is_not_configured_is_refused(self):
        bot, lg = _writing_bot()
        _, bad = asyncio.run(web.apply_lineup(
            bot, league_id="404", period="5", slots={"slot_1": "0"},
            user_id="owner"))
        self.assertTrue(bad)
        self.assertEqual(lg.write.calls, [])


class TestLineupForm(unittest.TestCase):
    """Parsing the posted lineup. Almost nothing is validated here on purpose:
    ``set_lineup``'s preflight owns the roster rules and is the path every
    other lineup in this app goes through."""

    def _save(self, slots, period="5"):
        bot, lg = _writing_bot()
        note, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period=period, slots=slots, user_id="owner"))
        return note, bad, lg.write

    def test_only_slot_fields_become_the_target(self):
        _, _, w = self._save({"league": "9", "sp": "5",
                              "slot_11": "0", "slot_22": "12"})
        self.assertEqual(w.calls[0][0], {11: 0, 22: 12})

    def test_a_malformed_field_refuses_the_whole_write(self):
        """Skipping the bad pair would post a partial map, which the preflight
        rejects anyway -- with a message about the wrong thing."""
        note, bad, w = self._save({"slot_11": "0", "slot_oops": "12"})
        self.assertTrue(bad)
        self.assertEqual(w.calls, [])
        self.assertIn("did not parse", note)

    def test_a_non_numeric_slot_refuses_the_whole_write(self):
        note, bad, w = self._save({"slot_11": "bench please"})
        self.assertTrue(bad)
        self.assertEqual(w.calls, [])

    def test_a_form_naming_nobody_is_refused(self):
        note, bad, w = self._save({"league": "9", "sp": "5"})
        self.assertTrue(bad)
        self.assertEqual(w.calls, [])

    def test_the_period_is_passed_through_as_posted(self):
        _, _, w = self._save({"slot_1": "0"}, period="7")
        self.assertEqual(w.calls[0][1], 7)

    def test_an_unparseable_period_is_passed_as_none_not_as_zero(self):
        """``None`` lets ``set_lineup`` fall back to the league's own current
        period. Zero would address the write to a period that does not exist."""
        _, _, w = self._save({"slot_1": "0"}, period="")
        self.assertIsNone(w.calls[0][1])

    def test_a_preflight_refusal_is_reported_in_its_own_words(self):
        bot, lg = _writing_bot(raises=RuntimeError(
            "target puts 2 players in PG but capacity is 1"))
        note, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period="5", slots={"slot_1": "0"},
            user_id="owner"))
        self.assertTrue(bad)
        self.assertIn("2 players in PG", note)

    def test_an_unchanged_lineup_is_said_to_be_unchanged_and_is_not_an_error(self):
        bot, lg = _writing_bot(result=_write_result(items=()))
        note, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period="5", slots={"slot_1": "0"},
            user_id="owner"))
        self.assertFalse(bad)
        self.assertIn("already the lineup", note)

    def test_shadow_mode_says_nothing_was_sent(self):
        bot, lg = _writing_bot(
            result=_write_result(items=({"playerId": 1},), shadow=True))
        note, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period="5", slots={"slot_1": "0"},
            user_id="owner"))
        self.assertFalse(bad)
        self.assertIn("Shadow mode", note)
        self.assertIn("nothing sent to ESPN", note)

    def test_a_successful_save_counts_what_moved(self):
        bot, lg = _writing_bot(
            result=_write_result(items=({"playerId": 1}, {"playerId": 2})))
        note, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period="5", slots={"slot_1": "0"},
            user_id="owner"))
        self.assertFalse(bad)
        self.assertIn("2 moves", note)


class TestSaveOutcomeIsNotAnInjection(unittest.TestCase):
    """The outcome of a save travels in the query string of a redirect, so by
    the time it is rendered it is a value off the URL bar."""

    def test_a_note_from_the_url_is_escaped(self):
        out = web.render_team(_page(), _team_view([]),
                              note="<img src=x onerror=alert(1)>")
        self.assertNotIn("<img src=x", out)
        self.assertIn("&lt;img", out)

    def test_an_espn_refusal_is_clipped_before_it_reaches_a_url(self):
        bot, lg = _writing_bot(raises=RuntimeError("x" * 500))
        note, bad = asyncio.run(web.apply_lineup(
            bot, league_id="9", period="5", slots={"slot_1": "0"},
            user_id="owner"))
        self.assertTrue(bad)
        self.assertLess(len(note), 280)
        self.assertTrue(note.endswith("…"))


class TestMatchupWeekNavigation(unittest.TestCase):
    """*"id love to be able to toggle back to previous matchups"*."""

    def test_the_week_is_named_above_the_card_whose_totals_it_is(self):
        out = web.render_matchup(_page(), _matchup_view([], []))
        self.assertIn("Week 2", out)
        self.assertLess(out.index("Week 2"), out.index("<section class=vs>"))

    def test_the_arrows_link_to_the_first_day_of_the_week_either_side(self):
        out = web.render_matchup(_page(), _matchup_view(
            [], [], prev_week=1, next_week=14))
        self.assertIn("href='matchup?league=9&amp;sp=1' "
                      "aria-label='Previous matchup'", out)
        self.assertIn("href='matchup?league=9&amp;sp=14' "
                      "aria-label='Next matchup'", out)

    def test_there_is_no_way_forward_from_the_week_being_played(self):
        """ESPN's schedule carries every future fixture as a real row with a
        real opponent and 0.0-0.0 on it, so an unbounded arrow walks into
        seventeen matchups that have not happened."""
        out = web.render_matchup(_page(), _matchup_view([], [], next_week=None))
        self.assertIn("dayarrow off", out)
        self.assertNotIn("Next matchup", out)

    def test_the_current_week_says_so_instead_of_its_dates(self):
        out = web.render_matchup(_page(), _matchup_view([], [], this_week=True))
        self.assertIn("This week", out)

    def test_a_past_week_is_labelled_with_its_dates(self):
        out = web.render_matchup(_page(), _matchup_view(
            [], [], this_week=False, week=1, week_span="Oct 20–25"))
        self.assertIn("Oct 20", out)
        self.assertNotIn("This week", out)

    def test_no_calendar_means_no_week_strip_rather_than_a_guessed_one(self):
        out = web.render_matchup(_page(), _matchup_view([], [], week=None))
        self.assertNotIn("class=weeknav", _body(out))


class TestFinishedMatchupReadsInThePastTense(unittest.TestCase):
    """A decided week that still says "Ahead by" reads as one still being
    played, which is the whole reason the page knows the difference."""

    def _score(self, **over):
        kw = dict(matchup_period=1, my_team_id=7, my_points=1392.0,
                  opp_team_id=6, opp_name="Theirs", opp_points=1287.0,
                  my_name="Mine")
        kw.update(over)
        return MatchupScore(**kw)

    def test_a_running_week_is_ahead_and_a_finished_one_is_won(self):
        running = web.render_matchup(_page(), _matchup_view(
            [], [], score=self._score()))
        self.assertIn("Ahead by 105.0", running)
        self.assertNotIn("Won by", running)

        done = web.render_matchup(_page(), _matchup_view(
            [], [], score=self._score(outcome="WIN")))
        self.assertIn("Won by 105.0", done)
        self.assertNotIn("Ahead by", done)

    def test_a_lost_week_says_lost(self):
        out = web.render_matchup(_page(), _matchup_view(
            [], [], score=self._score(my_points=1190.0, opp_points=1221.5,
                                      outcome="LOSS")))
        self.assertIn("Lost by 31.5", out)

    def test_the_middle_of_the_card_says_which_it_is(self):
        self.assertIn(">vs", web.render_matchup(
            _page(), _matchup_view([], [], score=self._score())))
        self.assertIn(">final", web.render_matchup(
            _page(), _matchup_view([], [], score=self._score(outcome="WIN"))))

    def test_level_becomes_tied_once_it_is_over(self):
        out = web.render_matchup(_page(), _matchup_view(
            [], [], score=self._score(my_points=100.0, opp_points=100.0,
                                      outcome="TIE")))
        self.assertIn("Tied", out)
        self.assertNotIn("Level", out)


class TestDayPair(unittest.TestCase):
    def test_both_sides_missing_is_one_dash_not_two(self):
        self.assertEqual(web._daypair(None, None), "&mdash;")

    def test_the_winner_of_the_day_is_marked(self):
        self.assertIn("d1 up", web._daypair(110.0, 92.0))
        self.assertIn("d1 dn", web._daypair(92.0, 110.0))

    def test_a_level_day_marks_neither(self):
        out = web._daypair(50.0, 50.0)
        self.assertNotIn("up", out)
        self.assertNotIn("dn", out)

    def test_one_side_reported_and_the_other_not_still_shows_the_one(self):
        out = web._daypair(110.0, None)
        self.assertIn("110", out)
        self.assertIn("&mdash;", out)


class FakeDayRead:
    """``matchup_score`` and ``day_rosters``, recording what they were asked."""

    def __init__(self, by_period=None, rosters=None):
        self.asked = []
        self.periods = []
        self.by_period = by_period or {1: 110.0, 2: 92.0}
        self.rosters = rosters or {}

    def matchup_score(self, team_id=None, matchup_period=None):
        self.asked.append(matchup_period)
        return MatchupScore(
            matchup_period=matchup_period or 0, my_team_id=7, my_points=1.0,
            opp_team_id=6, opp_name="Theirs", opp_points=2.0, my_name="Mine",
            my_by_period=dict(self.by_period), opp_by_period={},
        )

    def day_rosters(self, scoring_period, team_ids=None):
        self.periods.append(scoring_period)
        return dict(self.rosters)

    def league_settings(self):
        raise AssertionError("the page must go through settings_now")


class FakeCalendar:
    """Weeks of ``size`` days starting at period 1, like the real one."""

    trustworthy = True

    def __init__(self, size=7, weeks=6, first=date(2026, 10, 20)):
        self.size = size
        self.weeks = weeks
        self.first = first

    def matchup_period_of(self, period):
        return (int(period) - 1) // self.size + 1

    def periods_in_matchup(self, week):
        if week < 1 or week > self.weeks:
            return []
        lo = (int(week) - 1) * self.size + 1
        return list(range(lo, lo + self.size))

    def matchup_period_bounds(self, week):
        days = self.periods_in_matchup(week)
        return (self.date_of(days[0]), self.date_of(days[-1])) if days else None

    def date_of(self, period):
        return self.first + timedelta(days=int(period) - 1)


class FakeSettings:
    def __init__(self, sp, mp):
        self.current_scoring_period = sp
        self.current_matchup_period = mp
        self.lineup_slot_counts = dict(SLOTS)
        self.lineup_slot_stat_limits = None


def _week_page(sp=15, mp=3, **cal_kw):
    """A page whose "today" is scoring period ``sp``, in matchup week ``mp``."""
    lg = FakeLeague(9, "Dynassty")
    lg.read = FakeDayRead()
    lg.espn = type("E", (), {"team_id": 7})()
    bot = FakeBot(leagues=[lg])
    bot.season_calendar = lambda: FakeCalendar(**cal_kw)
    bot.settings_now = lambda league: (FakeSettings(sp, mp), None)
    bot.pro_games = lambda: {}
    return web.StatusPage(bot), lg


class TestWhichWeekThePageOpens(unittest.TestCase):
    """The clamping. Every one of these is a URL somebody can type or keep."""

    def test_no_day_asked_for_opens_on_today(self):
        page, lg = _week_page()
        view = page._matchup_view(lg, None)
        self.assertEqual(view["period"], 15)
        self.assertEqual(view["week"], 3)
        self.assertTrue(view["this_week"])

    def test_the_score_is_read_for_the_week_being_shown_not_the_current_one(self):
        """Otherwise a past week's rows would sit under this week's total --
        and under this week's opponent, who is a different person."""
        page, lg = _week_page()
        page._matchup_view(lg, 3)
        self.assertEqual(lg.read.asked, [1])

    def test_a_past_day_opens_its_own_week_and_can_come_forward(self):
        page, lg = _week_page()
        view = page._matchup_view(lg, 3)
        self.assertEqual(view["week"], 1)
        self.assertFalse(view["this_week"])
        self.assertIsNone(view["prev_week"])      # no week zero
        self.assertEqual(view["next_week"], 8)    # first day of week 2

    def test_the_week_being_played_has_no_way_forward(self):
        page, lg = _week_page()
        view = page._matchup_view(lg, 15)
        self.assertEqual(view["prev_week"], 8)
        self.assertIsNone(view["next_week"])

    def test_a_day_past_the_current_week_is_clamped_back_to_it(self):
        """A hand-edited link, or a bookmark kept past the end of the season."""
        page, lg = _week_page()
        view = page._matchup_view(lg, 400)
        self.assertEqual(view["week"], 3)
        self.assertEqual(view["period"], 15)

    def test_a_day_below_the_season_is_clamped_to_the_first_week(self):
        page, lg = _week_page()
        view = page._matchup_view(lg, -5)
        self.assertEqual(view["week"], 1)
        self.assertEqual(view["period"], 1)

    def test_rubbish_in_the_query_string_opens_today(self):
        page, lg = _week_page()
        self.assertEqual(page._matchup_view(lg, "tuesday")["period"], 15)

    def test_espn_being_ahead_of_the_calendar_does_not_hide_the_live_week(self):
        """The calendar is the authority on dates -- that is the fix for the
        409s -- but a disagreement must cost history, not the week in play."""
        page, lg = _week_page(sp=15, mp=4)
        self.assertEqual(page._matchup_view(lg, 25)["week"], 4)

    def test_the_roster_is_read_for_the_day_being_shown(self):
        page, lg = _week_page()
        page._matchup_view(lg, 3)
        self.assertEqual(lg.read.periods, [3])


class TestTheDayStripIsBuiltFromTheWeek(unittest.TestCase):
    def test_one_entry_per_day_of_the_week(self):
        page, lg = _week_page(size=6, weeks=6)
        days = page._matchup_view(lg, 3)["days"]
        self.assertEqual([d["sp"] for d in days], [1, 2, 3, 4, 5, 6])

    def test_exactly_one_day_is_the_one_being_looked_at(self):
        page, lg = _week_page()
        days = page._matchup_view(lg, 3)["days"]
        self.assertEqual([d["sp"] for d in days if d["on"]], [3])

    def test_the_scores_come_off_the_matchup_row_already_read(self):
        page, lg = _week_page()
        days = {d["sp"]: d["mine"] for d in page._matchup_view(lg, 3)["days"]}
        self.assertEqual(days[1], 110.0)
        self.assertEqual(days[2], 92.0)

    def test_a_day_the_row_says_nothing_about_stays_none(self):
        """``None`` is what renders as a dash; 0.0 would claim it was played."""
        page, lg = _week_page()
        days = {d["sp"]: d["mine"] for d in page._matchup_view(lg, 3)["days"]}
        self.assertIsNone(days[5])

    def test_today_is_marked_even_when_another_day_is_open(self):
        page, lg = _week_page()
        days = page._matchup_view(lg, 15)["days"]
        self.assertEqual([d["sp"] for d in days if d["today"]], [15])
