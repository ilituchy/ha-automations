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
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

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
    """This week's head-to-head, cached by the last run and rendered cold.

    The page has no ESPN credentials and no event loop, so every number here
    came out of SQLite and may be hours old. That is the whole reason the panel
    carries its own timestamp, and the reason a missing field has to render as
    an absence rather than as a zero -- a score of 0-0 shown confidently is
    worse than no panel at all.
    """

    def test_one_panel_per_league_in_configured_order(self):
        bot = FakeBot(
            FakeStore(boards=[_board(league_id=9, my_name="Second"),
                              _board(league_id=4, my_name="First")]),
            leagues=[FakeLeague(4, "Alpha"), FakeLeague(9, "Beta")],
        )
        out = web.render(web.StatusPage(bot))
        self.assertLess(out.index("Alpha"), out.index("Beta"))
        self.assertEqual(out.count("<section class=board>"), 2)

    def test_a_league_with_no_reading_yet_is_simply_absent(self):
        """A fresh install shows nothing, not a 0-0 placeholder."""
        bot = FakeBot(
            FakeStore(boards=[_board(league_id=4)]),
            leagues=[FakeLeague(4, "Alpha"), FakeLeague(9, "Beta")],
        )
        out = web.render(web.StatusPage(bot))
        self.assertEqual(out.count("<section class=board>"), 1)

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
        self.assertIn("<section class=board>", out)

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
        out = web.render(web.StatusPage(_two_leagues()))
        self.assertEqual(out.count("<section class=lg>"), 2)

    def test_the_league_is_named_once_not_once_per_run(self):
        out = web.render(web.StatusPage(_two_leagues()))
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
        out = web.render(web.StatusPage(bot))
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

    def test_an_empty_queue_says_so_once_rather_than_per_column(self):
        out = web.render(web.StatusPage(_pending_bot()))
        self.assertEqual(out.count("Nothing waiting."), 1)
        self.assertIn("only add/drop asks first", out)


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
