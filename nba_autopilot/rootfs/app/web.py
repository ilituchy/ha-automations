"""Ingress status page: the review surface for proposals needing approval.

Why this exists rather than notification taps alone: a notification is a
one-shot thing. Miss it, swipe it away, or read it on a locked screen and the
approval is gone until the proposal expires six hours later. This page is
always there, shows *why* a move was proposed next to the button that accepts
it, and survives being ignored.

Three properties are deliberate.

**It reuses the state machine; it does not reimplement it.** An approval from
this page calls the same ``Store.decide`` -> ``execute_approved`` pair a
notification tap calls, so the double-tap guard, the stale-proposal guard and
the audit trail are shared rather than duplicated. A second path to executing a
proposal is a second place for the ``PROPOSED`` check to be forgotten.

**Every URL it emits is relative.** Supervisor ingress serves the app under
``/api/hassio_ingress/<rotating-token>/``, so an absolute ``/decide`` would
escape the prefix and 404 against the HA frontend. There is an
``X-Ingress-Path`` header carrying the prefix, but not needing it at all is
better than using it correctly.

**Everything interpolated into the page is escaped.** Rationale strings contain
ESPN player names and, once the projection layer lands, LLM-generated text.
Both are untrusted input by the project's own rules, and this page renders
inside the authenticated HA frontend -- the single worst place to put an
injection.
"""

#
# **This module must not use ``from __future__ import annotations``.** That
# makes every annotation a string, and FastAPI resolves a route handler's
# annotations with ``get_type_hints``, which looks them up in *module* globals.
# ``Request`` is imported inside ``build_app`` to keep this file importable
# without FastAPI installed, so it is not a module global -- the name would not
# resolve, and FastAPI's fallback for a parameter whose type it cannot identify
# is to treat it as a required **query** parameter. The symptom is that every
# page load returns ``422 {"loc": ["query", "request"], "msg": "Field
# required"}``, which is how this was found: on the device, through the ingress
# proxy, because no test here imports FastAPI at all. See test_web.py.

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, quote

# Constants only -- no client, no httpx, nothing that opens a socket. The slot
# and pro-team names belong to ESPN's id space, and a second copy of them here
# would be a second thing to keep current.
from espn import ids

log = logging.getLogger("autopilot.web")

#: Supervisor ingress authenticates the request before it reaches us and passes
#: the resolved Home Assistant user in these headers. The name is capitalized
#: as the Supervisor sends it; the lookups below are case-insensitive anyway
#: because Starlette normalizes header names.
H_USER_ID = "x-remote-user-id"
H_USER_NAME = "x-remote-user-display-name"

#: Only these kinds ever reach this page as decisions. Lineup and IR moves are
#: autonomous by configuration and are shown as already-applied history, not as
#: questions -- surfacing an Approve button for something that already happened
#: would be a lie about what the button does.
APPROVABLE = ("ADD", "DROP", "ADD_DROP")


def _e(v: Any) -> str:
    """Escape for HTML text content. Everything user- or ESPN-derived goes through here."""
    return html.escape(str(v), quote=True)


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _news_for(player_id: int) -> List[Any]:
    """Recent notes about a player, or none.

    Imported here rather than at the top of the module for the reason stated
    there: everything this module imports at load time is constants, and
    ``espn.news`` pulls in ``httpx``. It is also the only call on the page
    that reaches a host outside the league API, so keeping it visible at its
    one call site is worth a line.
    """
    from espn import news

    return news.fetch(int(player_id))


#: ESPN's ``status`` when nobody rosters him. ``ONTEAM`` is deliberately absent:
#: it is answered by the team's name, and falling back to the word "On a team"
#: would hide a team lookup that failed.
ACQUISITION_WORDS = {
    "FREEAGENT": "Free agent",
    "WAIVERS": "On waivers",
}


def _split_row(split: Any) -> Dict[str, Any]:
    """One row of the Stats table."""
    return {
        "label": split.label, "games": split.games,
        "min": split.minutes, "pts": split.points, "reb": split.rebounds,
        "ast": split.assists, "stl": split.steals, "blk": split.blocks,
        "to": split.turnovers,
        "fpts": split.fantasy_total, "avg": split.fantasy_average,
    }


def _news_row(item: Any, tz: Any) -> Dict[str, Any]:
    """One Rotowire note, with its timestamp in the reader's own zone."""
    when = item.published
    if when is not None and tz is not None:
        try:
            when = when.astimezone(tz)
        except Exception:  # noqa: BLE001
            pass
    return {
        "when": "" if when is None else "%s %d, %s" % (
            when.strftime("%b"), when.day, _clock(when)),
        "source": item.source,
        "headline": item.headline,
        "spin": item.spin,
    }


class StatusPage:
    """Builds the view model. Separated from routing so it is testable without HTTP."""

    #: How long a live score is reused before ESPN is asked again. Short
    #: enough that the number on screen is the number now, long enough that
    #: holding down refresh -- or the redirect that follows every Approve --
    #: does not turn into a request per keystroke.
    LIVE_TTL = 25.0

    #: And how long a *failed* read is remembered. Shorter, because the usual
    #: cause is transient, but not zero: without it a dead cookie would make
    #: every single page load sit through the timeout below.
    FAIL_TTL = 10.0

    #: Long enough for a slow ESPN, short enough that a hung one does not hold
    #: the page hostage. The fallback is a cached score, not an error.
    LIVE_TIMEOUT = 4.0

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        #: ``league_id -> (monotonic deadline, board or None)``. Instance state
        #: because ``create_app`` builds one page and both handlers close over
        #: it, so this outlives a request the way a cache has to.
        self._live: Dict[Any, Tuple[float, Optional[Dict[str, Any]]]] = {}
        #: ``(league, player) -> (deadline, view)``. Same reasoning as
        #: ``_live``: one page object outlives every request, and a card
        #: costs two ESPN reads that nothing on the page can fall back on.
        self._card_cache: Dict[Tuple[Any, int],
                               Tuple[float, Dict[str, Any]]] = {}
        #: ``(deadline, {proTeamId: LiveGame})``. One slate serves every
        #: league and both sides of every matchup, so it is keyed by nothing.
        self._slate: Tuple[float, Mapping[int, Any]] = (0.0, {})

    def league_label(self, league_id: Optional[int]) -> str:
        for lg in getattr(self.bot, "leagues", []):
            if lg.ref.league_id == league_id:
                return lg.ref.display()
        return "league %s" % (league_id if league_id is not None else "?")

    def pending(self) -> List[Dict[str, Any]]:
        """Proposals awaiting a decision, newest run first."""
        out = []
        for p in self.bot.store.pending():
            if p.kind not in APPROVABLE:
                # An autonomous class should never sit in PROPOSED. If one does,
                # something set auto_approve wrongly and the right move is to
                # say so rather than to render a button for it.
                log.warning("pending proposal %s has non-approvable kind %r",
                            p.action_id, p.kind)
                continue
            league_id = self._league_of_run(p.run_id)
            out.append({
                "action_id": p.action_id,
                "kind": p.kind,
                # The id as well as the label, because the page groups this
                # list into the same per-league columns as the runs and the
                # column order has to come from the configured ids.
                "league_id": league_id,
                "league": self.league_label(league_id),
                "payload": dict(p.payload),
                "rationale": p.rationale,
                "value_delta": p.value_delta,
                "expires_at": p.expires_at,
            })
        out.reverse()
        return out

    #: How a swap the app refused is labelled, and in which colour.
    #:
    #: Only the app's own refusals are here. *"i dont care about what I did. i
    #: care about the ones you and the ai judge decided to not surface"* --
    #: a swap the owner rejected and one that expired unanswered are his own
    #: decisions, already known, and the run log has them.
    #:
    #: The two are kept apart because they are different events: one is the
    #: gate working, the other is a degraded run. Folding them together would
    #: make a week of outages look like a week of good judgment, which is the
    #: same distinction ``judge.Verdict.errored`` exists upstream to preserve.
    DECLINED = {
        "blocked": ("Reviewer said no", ""),
        "unreachable": ("Review unavailable", "hold"),
    }

    #: How many refusals one league shows. The cap is per league and not over
    #: the list, because a global one lets the busier league crowd the other
    #: out of its own column -- two leagues would mean one of them silently
    #: answering "nothing" while it had a fortnight of history.
    CONSIDERED_PER_LEAGUE = 10

    def considered(self, limit: int = 60) -> List[Dict[str, Any]]:
        """Add/drop swaps the app weighed and did not put to the owner.

        The answer to the question an empty approval queue invites. "Nothing
        waiting" is true and unsatisfying: a quiet week and a week where the
        reviewer refused six swaps a day read identically from it, and they
        call for opposite responses.

        Two sources, and which one answers for a run is decided per run.
        Blocked swaps have been recorded as rows since 2026-10-26; before
        that they existed only as a line in the run's notes, written for a
        person to read. Those lines are still the whole record of what the
        reviewer said on those days, so they are parsed back rather than
        thrown away -- and a run that has rows of its own never has its notes
        read, so nothing is listed twice and the fallback retires itself as
        the rows accumulate.
        """
        rows = self.bot.store.query(
            "SELECT p.*, r.league_id AS league_id, r.started_at AS started_at "
            "FROM proposals p LEFT JOIN runs r ON r.run_id = p.run_id "
            "WHERE p.kind='ADD_DROP' AND p.state='BLOCKED' "
            "ORDER BY p.id DESC LIMIT ?",
            (int(limit),),
        )
        out: List[Dict[str, Any]] = []
        recorded = set()
        for r in rows:
            row = dict(r)
            recorded.add(row.get("run_id"))
            payload = _loads(row.get("payload"), {})
            label, tag = self.DECLINED.get(
                str(payload.get("declined") or "blocked"),
                self.DECLINED["blocked"])
            out.append({
                "league_id": row.get("league_id"),
                "label": label,
                "tag": tag,
                "payload": payload,
                "rationale": row.get("rationale") or "",
                "note": "",
                "when": (row.get("finished_at") or row.get("created_at")
                         or row.get("started_at")),
            })
        out.extend(self._considered_from_notes(limit, recorded))
        # Newest first across both sources. Sorted on the stored timestamp,
        # which is ISO-8601 UTC everywhere, so a string sort is a time sort.
        out.sort(key=lambda e: str(e.get("when") or ""), reverse=True)
        return out[:int(limit)]

    def _considered_from_notes(self, limit: int,
                               skip: Any) -> List[Dict[str, Any]]:
        """Blocked swaps recovered from the notes of runs that recorded none.

        Text only: the note names both players but carries no ids, so these
        entries have no headshots and no links. That is the honest shape --
        less was kept at the time -- and it is why the rows above are the
        preferred source rather than a second rendering of the same thing.
        """
        out: List[Dict[str, Any]] = []
        runs = self.bot.store.query(
            "SELECT run_id, league_id, started_at, notes FROM runs "
            "ORDER BY started_at DESC LIMIT ?", (int(limit) * 4,))
        for run in runs:
            row = dict(run)
            if row.get("run_id") in skip:
                continue
            for note in _loads(row.get("notes"), []):
                found = _blocked_note(note)
                if found is None:
                    continue
                swap, why, unreachable = found
                label, tag = self.DECLINED[
                    "unreachable" if unreachable else "blocked"]
                out.append({
                    "league_id": row.get("league_id"),
                    "label": label,
                    "tag": tag,
                    "payload": {},
                    "rationale": why,
                    "note": swap,
                    "when": row.get("started_at"),
                })
        return out

    def _league_of_run(self, run_id: str) -> Optional[int]:
        rows = self.bot.store.query(
            "SELECT league_id FROM runs WHERE run_id=?", (run_id,)
        )
        return rows[0]["league_id"] if rows else None

    def recent_runs(self, limit: int = 8) -> List[Dict[str, Any]]:
        rows = self.bot.store.query(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
        )
        runs = []
        for r in rows:
            props = self.bot.store.query(
                "SELECT * FROM proposals WHERE run_id=? ORDER BY id", (r["run_id"],)
            )
            runs.append({
                "run_id": r["run_id"],
                "league_id": r["league_id"],
                "league": self.league_label(r["league_id"]),
                "started_at": r["started_at"],
                "scoring_period": r["scoring_period"],
                "shadow_mode": bool(r["shadow_mode"]),
                "notes": _loads(r["notes"], []),
                "shadow_prices": _loads(r["shadow_prices"], {}),
                "proposals": [{
                    "kind": p["kind"],
                    "state": p["state"],
                    "rationale": p["rationale"],
                    "payload": _loads(p["payload"], {}),
                } for p in props],
            })
        return runs

    def scoreboards(self) -> List[Dict[str, Any]]:
        """Each league's head-to-head as of its last run.

        The floor, not the normal path: :meth:`live_scoreboards` asks ESPN on
        every load and only lands here when that fails. Kept because the page's
        job is to render -- the Approve button is on it -- and an expired
        cookie must cost the reader a stale score, not the whole page.

        A league with no row yet -- a fresh install, or one whose only run
        aborted before the score was read -- is simply absent, and the header
        then shows nothing rather than a 0-0 placeholder.
        """
        rows = self.bot.store.query("SELECT * FROM scoreboards")
        boards = {}
        for r in rows:
            b = dict(r)
            b["league"] = self.league_label(b.get("league_id"))
            boards[b.get("league_id")] = b
        return self._ordered(boards)

    async def live_scoreboards(self) -> List[Dict[str, Any]]:
        """This week's head-to-head, read from ESPN now.

        The score is the one thing on this page that is worth a request while
        somebody is waiting: a lineup decision is hours old by the time it is
        read, but a score that is hours old is wrong. The credentials are right
        here -- every ``League`` holds its own authenticated reader -- and one
        ``matchup_score`` call is one HTTP request that carries both teams'
        points, names, abbrevs, logos and games-used.

        Two fields still come off the run's cached row, because they are not
        on that response and are not worth more requests to rebuild:

        * ``games_cap`` -- a league setting, fixed for the season;
        * ``my_games_today`` -- it depends on the lineup *and* the pro
          schedule, and the run that wrote it also wrote the lineup, so it
          stays true for the rest of the day anyway.

        Leagues are read concurrently and every failure degrades to that same
        cached row, labelled with its age. Nothing here may raise: a page that
        will not render is worse than a score that is an hour old.
        """
        cached = {b.get("league_id"): b for b in self.scoreboards()}
        leagues = list(getattr(self.bot, "leagues", []))
        if not leagues:
            return self._ordered(cached)
        got = await asyncio.gather(
            *(self._live_board(lg, cached) for lg in leagues),
            return_exceptions=True,
        )
        boards = dict(cached)
        for lg, b in zip(leagues, got):
            if isinstance(b, dict):
                boards[lg.ref.league_id] = b
            elif isinstance(b, BaseException):
                # Belt and braces: ``_live_board`` already swallows, so this
                # is only reachable if it is edited badly later.
                log.warning("live scoreboard for %s raised: %s",
                            lg.ref.league_id, b)
        return self._ordered(boards)

    async def _live_board(
        self, lg: Any, cached: Mapping[Any, Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """One league's live row, memoised, falling back to its cached one."""
        league_id = lg.ref.league_id
        now = time.monotonic()
        hit = self._live.get(league_id)
        if hit is not None and hit[0] > now:
            return hit[1] if hit[1] is not None else cached.get(league_id)

        fallback = cached.get(league_id)
        try:
            # ``EspnRead`` is a synchronous ``httpx.Client``; calling it on the
            # loop would stall every other request behind this one.
            score = await asyncio.wait_for(
                asyncio.to_thread(lg.read.matchup_score),
                timeout=self.LIVE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s's live score (%s); "
                        "showing the last cached one", league_id, exc)
            self._live[league_id] = (now + self.FAIL_TTL, None)
            return fallback
        if score is None:
            # No matchup row for us this period -- preseason, or a league that
            # has not scheduled the week. Not an error, and not a 0-0 either.
            self._live[league_id] = (now + self.LIVE_TTL, None)
            return fallback

        board = dict(fallback or {})
        board.update({
            "league_id": league_id,
            "league": self.league_label(league_id),
            "matchup_period": score.matchup_period,
            "my_team_id": score.my_team_id,
            "my_name": score.my_name,
            "my_abbrev": score.my_abbrev,
            "my_logo": score.my_logo,
            "my_points": score.my_points,
            "opp_team_id": score.opp_team_id,
            "opp_name": score.opp_name,
            "opp_abbrev": score.opp_abbrev,
            "opp_logo": score.opp_logo,
            "opp_points": score.opp_points,
            "my_games_used": score.my_games_used,
            "opp_games_used": score.opp_games_used,
            # Read just now, so the panel says so instead of showing a
            # timestamp the reader would have to date-arithmetic.
            "live": True,
        })
        self._live[league_id] = (now + self.LIVE_TTL, board)
        return board

    def _ordered(self, boards: Mapping[Any, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Configured order first, so the panels line up with the columns."""
        order = [k for k in self.league_order() if k in boards]
        order += [k for k in boards if k not in order]
        return [boards[k] for k in order]

    def league_order(self) -> List[Any]:
        """Configured league ids, in the order they were configured.

        Column order has to be stable across loads, and "whichever ran most
        recently" is not: both leagues run in the same second, so a refresh
        could swap the columns for no reason a reader could see.
        """
        return [lg.ref.league_id for lg in getattr(self.bot, "leagues", [])]

    def tz(self) -> Any:
        """The configured zone, or ``None`` to leave times as stored.

        ``Autopilot`` resolves ``cfg.timezone`` once at startup; reading its
        attribute rather than re-resolving the name keeps the page and the
        scheduler agreed about what "today" means, and keeps ``zoneinfo`` out
        of this module's import path.
        """
        return getattr(self.bot, "tz", None)

    # -- the two detail pages ---------------------------------------------
    #
    # Both read ESPN synchronously and both are therefore built inside one
    # ``to_thread`` hop rather than awaiting call by call: every read here goes
    # through the same ``httpx.Client``, so interleaving them buys nothing and
    # costs a thread each.
    #
    # Neither has the index page's floor. The index can fall back to the row
    # the last run cached, because a stale decision is still a fact; a matchup
    # has no cached form at all, so a failed read here renders as a stated
    # failure with a way back, not as an empty page.

    def league_by_id(self, league_id: Any) -> Optional[Any]:
        """The configured league with this id, or ``None``.

        Compared as strings because the id arrives off a query string. ``None``
        rather than defaulting to the first league: every caller of this goes
        on to read or write a roster, and silently picking the wrong team is
        the failure mode this whole app is most careful about.
        """
        for lg in getattr(self.bot, "leagues", []):
            if str(lg.ref.league_id) == str(league_id):
                return lg
        return None

    def _settings(self, lg: Any) -> Any:
        """League settings with the scoring period corrected to today's.

        ``Autopilot.settings_now`` is the one supported way to read these, and
        this page needs it for the same reason every write does: ESPN's
        ``latestScoringPeriod`` lags by most of a morning, and a lineup
        addressed to yesterday is rejected whole with ``409
        TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT``. The Team page writes
        lineups, so it must agree with the scheduler about what day it is.
        """
        now = getattr(self.bot, "settings_now", None)
        if now is None:
            return lg.read.league_settings()
        settings, _ = now(lg)
        return settings

    def _calendar(self) -> Any:
        cal = getattr(self.bot, "season_calendar", None)
        return None if cal is None else cal()

    def _games(self, period: int) -> Mapping[int, Any]:
        """``proTeamId -> ProGame`` for one scoring period, or ``{}``.

        Empty is a perfectly good answer -- it renders as no opponent line
        under any name -- so this never raises and never blocks the page.
        """
        games = getattr(self.bot, "pro_games", None)
        if games is None:
            return {}
        try:
            return dict(games().get(int(period), {}))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the pro schedule for period %s: %s",
                        period, exc)
            return {}

    #: How long the NBA scoreboard is reused. A game clock is the one thing
    #: on these pages that is wrong the moment it is stale, and the endpoint
    #: is public and small -- but a reader refreshing hard should not turn
    #: one page load into one request per roster either.
    SLATE_TTL = 25.0

    def _live_slate(self, period: int, today: int) -> Mapping[int, Any]:
        """``proTeamId -> LiveGame`` for games in progress, or ``{}``.

        Only for today. Yesterday's clock is not a clock, and ESPN's
        scoreboard answers for the current slate anyway -- asking it about a
        past scoring period would return today's games under the wrong day,
        which is worse than saying nothing.
        """
        if int(period) != int(today):
            return {}
        now = time.monotonic()
        until, slate = self._slate
        if now < until:
            return slate
        from espn import live  # late: this module imports no ESPN at load
        slate = live.fetch()
        self._slate = (now + self.SLATE_TTL, slate)
        return slate

    def _matchup_of(self, period: int) -> Optional[int]:
        """Which matchup week a scoring period falls in, or ``None``.

        ``None`` means the calendar could not say, and every caller treats
        that as "there is one week, the current one" rather than guessing a
        number -- a wrong week number here would address the score read to
        somebody else's fixture.
        """
        cal = self._calendar()
        if cal is None or not cal.trustworthy:
            return None
        try:
            return int(cal.matchup_period_of(int(period)))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not place period %s in a week: %s", period, exc)
            return None

    def _week(self, period: int) -> List[int]:
        """The scoring periods of ``period``'s matchup week, or just itself.

        What the day strip is drawn from, and what bounds it. A week rather
        than the season because the score above it is the *week's* score:
        letting the reader step into a day the header does not cover would put
        two unrelated numbers on one screen.
        """
        week = self._matchup_of(period)
        if week is None:
            return [int(period)]
        days = self._calendar().periods_in_matchup(week)
        return days or [int(period)]

    def _week_span(self, week: Optional[int]) -> str:
        """``Oct 20 - 25``, or ``""`` when there is no calendar to ask."""
        cal = self._calendar()
        if week is None or cal is None or not cal.trustworthy:
            return ""
        try:
            bounds = cal.matchup_period_bounds(int(week))
        except Exception:  # noqa: BLE001
            return ""
        if not bounds:
            return ""
        first, last = bounds
        # The month is repeated only when the week straddles one, which is
        # most of what makes this readable at a glance rather than a date
        # range to be parsed.
        if first.month == last.month:
            return "%s %d\u2013%d" % (first.strftime("%b"), first.day, last.day)
        return "%s %d \u2013 %s %d" % (first.strftime("%b"), first.day,
                                       last.strftime("%b"), last.day)

    def _weekday(self, period: int) -> str:
        """``Mon``, or ``d7`` without a calendar. Three letters, for a chip."""
        cal = self._calendar()
        if cal is None or not cal.trustworthy:
            return "d%d" % int(period)
        try:
            return cal.date_of(int(period)).strftime("%a")
        except Exception:  # noqa: BLE001
            return "d%d" % int(period)

    def _log_day(self, period: int) -> str:
        """``10/26``, which is how ESPN's own game log dates a row.

        Shorter than :meth:`_day_label` on purpose: the game log is eight
        numeric columns wide before the date column is counted, and "Mon, Oct
        26" spends a fifth of a phone saying which weekday it was.
        """
        cal = self._calendar()
        if cal is not None and getattr(cal, "trustworthy", False):
            try:
                day = cal.date_of(int(period))
            except Exception:  # noqa: BLE001
                pass
            else:
                return "%d/%d" % (day.month, day.day)
        return "day %d" % int(period)

    def _day_label(self, period: int) -> str:
        """``Sat, Oct 24`` for a scoring period, or ``day 5`` without a calendar.

        The day number is interpolated rather than formatted: ``%-d`` is a
        glibc extension and the add-on runs on musl, where it comes back
        through unexpanded. Same reason ``_clock`` builds the hour by hand.
        """
        cal = self._calendar()
        if cal is None or not cal.trustworthy:
            return "day %d" % int(period)
        try:
            day = cal.date_of(int(period))
        except Exception:  # noqa: BLE001
            return "day %d" % int(period)
        return "%s, %s %d" % (day.strftime("%a"), day.strftime("%b"), day.day)

    async def matchup_view(self, league_id: Any,
                           period: Any = None) -> Dict[str, Any]:
        lg = self.league_by_id(league_id)
        if lg is None:
            return {"error": "That league is not configured."}
        try:
            return await asyncio.to_thread(self._matchup_view, lg, period)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not build the matchup page for %s: %s",
                        league_id, exc)
            return {"league": self.league_label(lg.ref.league_id),
                    "league_id": lg.ref.league_id,
                    "error": "Could not read this matchup from ESPN."}

    def _matchup_view(self, lg: Any, period: Any) -> Dict[str, Any]:
        settings = self._settings(lg)
        today = int(settings.current_scoring_period)
        try:
            want = int(period)
        except (TypeError, ValueError):
            want = today

        # Which week is being looked at, and which weeks exist to look at.
        #
        # The forward bound matters more than it sounds: ESPN's schedule
        # carries every *future* week too, each one a real row with a real
        # opponent and 0.0-0.0 on it. Without a bound the arrows would walk
        # into seventeen fixtures that have not happened, all identical.
        this_week = self._matchup_of(today)
        last_week = this_week
        if last_week is not None:
            # ESPN's own number as a floor. The two agree on both live
            # leagues; taking the larger means a disagreement costs a week of
            # history rather than hiding the week being played.
            last_week = max(last_week,
                            int(getattr(settings, "current_matchup_period", 0) or 0))
        want_week = self._matchup_of(want)
        if last_week is not None and want_week is not None:
            want_week = max(1, min(want_week, last_week))
            if want_week != self._matchup_of(want):
                # A hand-edited or stale link. Land on that week rather than
                # erroring, which is what the day clamp below does too.
                want = self._calendar().periods_in_matchup(want_week)[0]

        week = self._week(want)
        # Clamped to its own week rather than refused: a stale bookmark should
        # land on a real day of a real week, not on an error.
        if want not in week:
            want = today if today in week else week[0]

        score = lg.read.matchup_score(matchup_period=want_week)
        if score is None:
            return {"league": self.league_label(lg.ref.league_id),
                    "league_id": lg.ref.league_id,
                    "error": "No matchup this week -- a bye, or the schedule "
                             "is not out yet."}
        sides = [score.my_team_id]
        if score.opp_team_id is not None:
            sides.append(score.opp_team_id)
        rosters = lg.read.day_rosters(want, team_ids=sides)
        games, tz = self._games(want), self.tz()
        # The same figure the scoreboard panel prints, from the same place, so
        # the two screens cannot disagree about how many games a week allows.
        try:
            from solve import budget as budget_mod
            cap = budget_mod.matchup_cap(settings)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the games cap: %s", exc)
            cap = None

        # One chip per day of the week, each carrying what both sides scored
        # on it. Off the matchup row that was already read, so the whole strip
        # is free -- and it turns "page blindly through six days" into "the
        # week was lost on Thursday, look at Thursday".
        days = [{
            "sp": sp,
            "label": self._weekday(sp),
            "mine": score.my_by_period.get(sp),
            "theirs": score.opp_by_period.get(sp),
            "on": sp == want,
            "today": sp == today,
        } for sp in week]

        slate = self._live_slate(want, today)

        def rows(team_id: Any) -> List[Dict[str, Any]]:
            return [_row(d, games, tz, slate)
                    for d in (rosters.get(team_id) or [])]

        return {
            "league": self.league_label(lg.ref.league_id),
            "league_id": lg.ref.league_id,
            "score": score,
            "period": want,
            "day": self._day_label(want),
            "today": want == today,
            "days": days,
            "week": want_week,
            "week_span": self._week_span(want_week),
            "this_week": want_week is not None and want_week == this_week,
            "prev_week": self._week_link(want_week, -1, last_week),
            "next_week": self._week_link(want_week, 1, last_week),
            "slot_counts": dict(settings.lineup_slot_counts),
            "games_cap": cap,
            "mine": rows(score.my_team_id),
            "theirs": rows(score.opp_team_id),
        }

    def _week_link(self, week: Optional[int], step: int,
                   last: Optional[int]) -> Optional[int]:
        """The scoring period to link to for the week ``step`` away, or ``None``.

        A *scoring period*, not a week number, because that is the only thing
        the URL carries: one parameter means the day and the week can never
        contradict each other. Its first day, so a week is entered at the top
        the way it is read.
        """
        if week is None or last is None:
            return None
        target = int(week) + step
        if target < 1 or target > last:
            return None
        try:
            days = self._calendar().periods_in_matchup(target)
        except Exception:  # noqa: BLE001
            return None
        return days[0] if days else None

    async def team_view(self, league_id: Any) -> Dict[str, Any]:
        lg = self.league_by_id(league_id)
        if lg is None:
            return {"error": "That league is not configured."}
        try:
            return await asyncio.to_thread(self._team_view, lg)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not build the team page for %s: %s",
                        league_id, exc)
            return {"league": self.league_label(lg.ref.league_id),
                    "league_id": lg.ref.league_id,
                    "error": "Could not read this roster from ESPN."}

    def _team_view(self, lg: Any) -> Dict[str, Any]:
        settings = self._settings(lg)
        period = int(settings.current_scoring_period)
        team_id = lg.espn.team_id
        rosters = lg.read.day_rosters(period, team_ids=[team_id])
        games, tz = self._games(period), self.tz()
        return {
            "league": self.league_label(lg.ref.league_id),
            "league_id": lg.ref.league_id,
            "team_id": team_id,
            "period": period,
            "day": self._day_label(period),
            "slot_counts": dict(settings.lineup_slot_counts),
            # The lineup editor is always today's, so the slate always
            # applies: ``period`` is its own "today".
            "roster": [_row(d, games, tz, self._live_slate(period, period))
                       for d in (rosters.get(team_id) or [])],
        }

    # -- the player card ---------------------------------------------------

    #: How long one player's card is reused. Longer than a live score, because
    #: none of it changes within a game: the game log gains a row when a game
    #: finishes and the news gains one when Rotowire files. Short enough that
    #: a note filed during the morning is on the card by the time the lineup
    #: is being set.
    CARD_TTL = 180.0

    #: How many cards are held at once. Comfortably more than two rosters, so
    #: the page a reader is actually working on never evicts itself.
    CARD_MAX = 120

    async def player_view(self, league_id: Any, player_id: Any) -> Dict[str, Any]:
        lg = self.league_by_id(league_id)
        if lg is None:
            return {"error": "That league is not configured."}
        try:
            pid = int(player_id)
        except (TypeError, ValueError):
            return {"error": "That is not a player id."}
        try:
            return await asyncio.to_thread(self._player_view, lg, pid)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not build the card for player %s in %s: %s",
                        pid, league_id, exc)
            return {"league_id": lg.ref.league_id,
                    "error": "Could not read this player from ESPN."}

    def _player_view(self, lg: Any, pid: int) -> Dict[str, Any]:
        """The card, flattened to strings. See ``_row`` on why it stops here.

        Two network reads, and the news one cannot fail the page: it is the
        half that is nice to have, while the stats and the game log are the
        half somebody opened this to see.
        """
        cached = self._card_cache.get((lg.ref.league_id, pid))
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]

        settings = self._settings(lg)
        period = int(settings.current_scoring_period)
        card = lg.read.player_card(pid, period)
        if card is None:
            return {"league_id": lg.ref.league_id,
                    "error": "ESPN has no player with that id."}

        manager = ""
        if card.on_team_id:
            try:
                manager = lg.read.team_names().get(card.on_team_id, "")
            except Exception as exc:  # noqa: BLE001
                log.debug("could not name team %s: %s", card.on_team_id, exc)
        if not manager:
            manager = ACQUISITION_WORDS.get(card.acquisition_status, "")

        status_cls, status_word = STATUS_WORDS.get(card.injury_status, ("", ""))
        view = {
            "league": self.league_label(lg.ref.league_id),
            "league_id": lg.ref.league_id,
            "pid": card.player_id,
            "name": card.name,
            "team": ids.pro_team(card.pro_team_id),
            "positions": ", ".join(card.positions),
            "status": status_cls,
            "status_word": status_word or "Healthy",
            "rank": card.position_rank,
            "owned": card.percent_owned,
            "manager": manager,
            "outlook": card.outlook,
            "splits": [_split_row(s) for s in card.splits],
            "log": self._game_log(card, period),
            "news": [_news_row(n, self.tz()) for n in _news_for(card.player_id)],
        }
        self._remember_card((lg.ref.league_id, pid), view)
        return view

    def _remember_card(self, key: Tuple[Any, int], view: Dict[str, Any]) -> None:
        """Cache one card, dropping the entries that have expired.

        Bounded because this process runs for months and the key space is
        every player in the league, not just the ones on a roster: browsing
        the free agent pool would otherwise leave a card in memory for each
        name looked at, for the rest of the season. The sweep is only paid
        for once the cache is bigger than a roster or two.
        """
        now = time.monotonic()
        if len(self._card_cache) >= self.CARD_MAX:
            for stale in [k for k, (until, _) in self._card_cache.items()
                          if until <= now]:
                del self._card_cache[stale]
            # Every entry still live and the cache still full: the oldest
            # deadline is the one closest to being useless anyway.
            if len(self._card_cache) >= self.CARD_MAX:
                del self._card_cache[min(
                    self._card_cache, key=lambda k: self._card_cache[k][0])]
        self._card_cache[key] = (now + self.CARD_TTL, view)

    def _game_log(self, card: Any, period: int) -> List[Dict[str, Any]]:
        """His games, newest first, each with the fixture it was played in.

        The opponent is looked up against the team on the row rather than
        against his team today, so a traded player's October fixtures stay
        his October team's. ``pro_games`` is keyed by scoring period, which is
        what the row carries.
        """
        try:
            schedule = (self.bot.pro_games() or {}) if hasattr(
                self.bot, "pro_games") else {}
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the pro schedule for a card: %s", exc)
            schedule = {}
        out = []
        for row in card.game_log:
            game = (schedule.get(row.scoring_period) or {}).get(row.pro_team)
            opp = ""
            if game is not None:
                opp = "%s%s" % ("" if game.home else "@",
                                ids.pro_team(game.opponent_id))
            out.append({
                "sp": row.scoring_period,
                "day": self._log_day(row.scoring_period),
                "opp": opp,
                "today": row.scoring_period == int(period),
                "min": row.minutes, "pts": row.points, "reb": row.rebounds,
                "ast": row.assists, "stl": row.steals, "blk": row.blocks,
                "to": row.turnovers, "fpts": row.fantasy_points,
            })
        return out

    def stuck(self) -> List[Mapping[str, Any]]:
        return self.bot.store.stuck_writes()

    def shadow_mode(self) -> bool:
        return bool(self.bot.cfg.shadow_mode)


# -- rendering -------------------------------------------------------------
#
# Hand-written HTML with inline CSS, deliberately. A template engine and a
# static-file mount are two more things to get wrong inside an ingress prefix,
# and this is one page.
#
# Home Assistant's own variables are named first in every declaration so a
# custom theme can still drive the page, but the *fallbacks* are what actually
# renders: ingress serves this document inside an iframe, and CSS custom
# properties do not cross a document boundary, so nothing the frontend defines
# on its own :root reaches us. Dark mode therefore cannot ride on
# ``--primary-background-color`` the way it does in the frontend -- it needs
# ``prefers-color-scheme``, which the iframe *does* inherit. The fallbacks are
# kept in two token sets below so the HA-theme path and the standalone path
# stay in agreement.

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f1f2f6;
  --card: #fff;
  --fg: #16181d;
  --dim: #626a78;
  --faint: #8d94a1;
  --line: rgba(16,18,27,.09);
  --lift: 0 1px 2px rgba(16,18,27,.05), 0 6px 20px -8px rgba(16,18,27,.14);
  --good: #0f8f5a;
  --on-good: #fff;
  --good-bg: rgba(15,143,90,.10);
  --bad: #cf3340;
  --bad-bg: rgba(207,51,64,.10);
  --warn: #a96508;
  --warn-bg: rgba(169,101,8,.10);
  --chip: rgba(16,18,27,.055);
  --shade: rgba(16,18,27,.26);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #131417;
    --card: #1d1f25;
    --fg: #e8eaef;
    --dim: #9ba2b0;
    --faint: #767d8b;
    --line: rgba(255,255,255,.08);
    --lift: 0 1px 2px rgba(0,0,0,.4);
    --good: #43cc8d;
    --on-good: #08281a;   /* the dark green is light enough that white fails */
    --good-bg: rgba(67,204,141,.13);
    --bad: #ff6f77;
    --bad-bg: rgba(255,111,119,.13);
    --warn: #e8a33d;
    --warn-bg: rgba(232,163,61,.13);
    --chip: rgba(255,255,255,.07);
    --shade: rgba(0,0,0,.62);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 22px 16px 56px;
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", sans-serif;
  background: var(--primary-background-color, var(--bg));
  color: var(--primary-text-color, var(--fg));
  -webkit-font-smoothing: antialiased;
  font-variant-numeric: tabular-nums;
}
.wrap { max-width: 660px; margin: 0 auto; }
/* Wide enough for two columns of runs, and only then. Everything above them
   is a single file of decisions and stays at a reading width in the middle,
   because a swap card stretched across a desktop is two names a foot apart. */
@media (min-width: 1000px) {
  .wrap { max-width: 1040px; }
  /* Capped but not centred. Both lists below are now full-width grids, and a
     header band floating 190px to the right of the first column reads as a
     different page sitting on top of this one. */
  .solo { max-width: 660px; }
}
/* One column per league. `auto-fit` rather than a fixed two: the columns
   collapse to one on a phone on their own, and a third league gets a column
   instead of being appended to somebody else's.

   The scoreboards share the rule rather than copying it. All three bands on
   this page -- score, approvals, runs -- are the same leagues in the same
   order, and a reader reads *down* a column. One template means the score
   card is directly above the swap it is the reason for; two templates that
   merely look alike would drift apart the first time one of them is edited. */
.leagues, .boards {
  display: grid; align-items: start; column-gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
}
/* The separation between stacked columns is a grid row-gap and not a margin on
   the second column, which is what it was. A margin needs a breakpoint to turn
   itself off again when the columns go side by side, and that breakpoint was
   1000px while `auto-fit` starts pairing them at about 690 -- so between those
   two widths the right-hand column sat 18px lower than the left. A row-gap
   applies between rows and only between rows, so it needs no breakpoint and
   cannot disagree with the template about how many rows there are. */
.leagues { row-gap: 18px; }
/* A grid item's min-content width is its widest unbreakable child, so without
   this a long player name widens the column past its 1fr share instead of
   letting the name ellipsis inside it. */
.lg { min-width: 0; }
.lghead {
  font-size: 13px; font-weight: 700; letter-spacing: -.01em;
  margin: 0 0 8px; padding: 0 2px;
  color: var(--primary-text-color, var(--fg));
}

h1 {
  font-size: 24px; font-weight: 700; letter-spacing: -.02em;
  margin: 0 0 10px;
}

/* Title and state, then the scoreboards as their own full-width band under
   them. They were beside the title for a while, which fit them into dead
   space but cost the alignment: squeezed into half the width they were
   narrower than the columns below and sat over the gap between them. A score
   belongs at the head of its own league's column. */
.masthead { margin: 0 0 20px; }
.boards { row-gap: 10px; margin: 0 0 26px; }
.board {
  display: block; padding: 11px 13px 10px; border-radius: 14px;
  background: var(--card-background-color, var(--card));
  border: 1px solid var(--line);
  color: inherit; text-decoration: none;
}
/* The panel is a link to the matchup behind it. It must not *read* as one --
   twelve points of blue underlined team name is not a scoreboard -- so it
   keeps its own colours and says so by lifting under the cursor instead. */
a.board:hover { border-color: var(--primary-color, #2b7de9); }
a.board:focus-visible {
  outline: 2px solid var(--primary-color, #2b7de9); outline-offset: 2px;
}
.bhead {
  display: flex; align-items: baseline; gap: 8px;
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .06em;
  color: var(--secondary-text-color, var(--dim));
}
/* The age of the reading, not an afterthought: the page renders a score the
   last run cached, so one from this morning shown bare would read as live. */
.bwhen {
  margin-left: auto; font-size: 10px; font-weight: 600;
  text-transform: none; letter-spacing: 0; white-space: nowrap;
}
/* A live reading gets a dot and the accent colour; a recovered one keeps the
   dim grey of the label it is. The distinction has to survive a glance,
   because it is the difference between a score and a memory of one. */
.bwhen.fresh { color: var(--good); text-transform: uppercase; letter-spacing: .06em; }
.bwhen.fresh::before {
  content: ""; display: inline-block; vertical-align: middle;
  width: 5px; height: 5px; margin: -1px 4px 0 0;
  border-radius: 50%; background: currentColor;
}
.bteam {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 0 6px;
}
.bteam + .bteam { border-top: 1px solid var(--line); }
/* Fixed box, `contain` rather than `cover`: a league member's logo is whatever
   he uploaded, and a wordmark cropped to a square is unreadable. */
.crest {
  flex: 0 0 26px; width: 26px; height: 26px; border-radius: 7px;
  object-fit: contain; background: var(--chip);
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700;
  color: var(--secondary-text-color, var(--dim));
}
/* `min-width: 0` on the flex child is what actually lets the name ellipsis.
   Without it the item's min-content width is the full team name and the
   points get pushed out of the panel instead. */
.bwho { flex: 1 1 auto; min-width: 0; }
.bname {
  display: block;
  font-size: 13.5px; font-weight: 600; letter-spacing: -.01em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.bgames {
  display: block; font-size: 10.5px; font-weight: 500;
  color: var(--secondary-text-color, var(--dim));
}
.bpts {
  font-size: 19px; font-weight: 700; letter-spacing: -.02em;
  color: var(--secondary-text-color, var(--dim));
}
/* Only the leader's total is in full-strength ink. Two identical numbers side
   by side make the reader do the subtraction the page could have done. */
.bteam.up .bpts { color: var(--primary-text-color, var(--fg)); }
.bfoot {
  margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line);
  font-size: 11.5px; line-height: 1.45;
  color: var(--secondary-text-color, var(--dim));
}
.bfoot b { color: var(--primary-text-color, var(--fg)); font-weight: 700; }
.bfoot .lead { color: var(--good); }
.bfoot .trail { color: var(--bad); }

.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 26px; }
.chip {
  font-size: 12px; font-weight: 600; line-height: 1;
  padding: 6px 10px; border-radius: 999px;
  background: var(--chip); color: var(--secondary-text-color, var(--dim));
}
.chip.on { background: var(--good-bg); color: var(--good); }
.chip.hold { background: var(--warn-bg); color: var(--warn); }

h2 {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--secondary-text-color, var(--faint));
  margin: 30px 0 10px;
}
h2:first-of-type { margin-top: 0; }
h2 .count {
  font-size: 11px; letter-spacing: 0; padding: 2px 7px; border-radius: 999px;
  background: var(--bad-bg); color: var(--bad);
}
/* One line of explanation under a heading, above the columns it applies to.
   This is where the "why is the queue empty" sentence went when the empty
   card became one card per league: said once under the heading it reads as
   how the thing works, said twice in two cards it reads as boilerplate. */
.lede {
  margin: -4px 0 12px; font-size: 13px;
  color: var(--secondary-text-color, var(--dim));
}

.card {
  background: var(--card-background-color, var(--card));
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 16px;
  margin-bottom: 10px;
  box-shadow: var(--lift);
}
.banner { display: flex; gap: 12px; align-items: flex-start; }
.banner .bar {
  flex: 0 0 3px; align-self: stretch; border-radius: 3px;
  background: var(--warn); min-height: 34px;
}
.banner.bad .bar { background: var(--bad); }
.banner b { font-size: 14px; }

.eyebrow {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; margin-bottom: 12px;
}
.eyebrow .league {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--secondary-text-color, var(--faint));
}
.delta {
  font-size: 12px; font-weight: 700; white-space: nowrap;
  padding: 4px 9px; border-radius: 999px;
  background: var(--good-bg); color: var(--good);
}
.delta.down { background: var(--bad-bg); color: var(--bad); }

.swap { display: grid; gap: 9px; }
.leg { display: flex; align-items: center; gap: 10px; min-width: 0; }
.pip {
  flex: 0 0 22px; height: 22px; border-radius: 999px;
  display: grid; place-items: center;
  font-size: 15px; font-weight: 700; line-height: 1;
}
/* A headshot as a background rather than an <img>: a player ESPN has no
   photo for then degrades to an empty disc instead of a broken-image icon,
   with no script to handle the error. */
.face {
  position: relative;
  flex: 0 0 36px; height: 36px; border-radius: 999px;
  background: var(--chip) center 18%/125% no-repeat;
  box-shadow: inset 0 0 0 1px var(--line);
}
.face.sm { flex-basis: 28px; height: 28px; }
/* The +/- rides on the corner of the headshot rather than beside it, so a
   swap is two faces in a column and not four things in a row. */
.face .pip {
  position: absolute; right: -3px; bottom: -3px;
  flex: 0 0 18px; height: 18px; font-size: 13px;
  box-shadow: 0 0 0 2px var(--card-background-color, var(--card));
}
.face.sm .pip { flex-basis: 16px; height: 16px; font-size: 12px; }
.pip.in { background: var(--good-bg); color: var(--good); }
.pip.out { background: var(--bad-bg); color: var(--bad); }
.who {
  font-size: 17px; font-weight: 600; letter-spacing: -.01em;
  overflow-wrap: anywhere;
}
.leg.out .who { color: var(--secondary-text-color, var(--dim)); }
/* A linked name keeps the colour of the name it replaced. Twenty blue names
   down a column reads as a navigation list, and the thing being looked at is
   the move, not the link. The underline only arrives on hover -- enough to
   answer "is this clickable" at the moment the question is asked. */
a.who {
  color: inherit; text-decoration: none;
  text-underline-offset: 2px; text-decoration-thickness: 1.5px;
}
a.who:hover, a.who:focus-visible { text-decoration: underline; }
a.who:focus-visible {
  outline: 2px solid var(--primary-color, #2b7de9);
  outline-offset: 3px; border-radius: 3px;
}
.tag {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .06em; padding: 3px 7px; border-radius: 6px;
  background: var(--chip); color: var(--secondary-text-color, var(--dim));
  white-space: nowrap;
}
.tag.hold { background: var(--warn-bg); color: var(--warn); }
.tag.on { background: var(--good-bg); color: var(--good); }
.tag.off { background: var(--bad-bg); color: var(--bad); }

/* The rationale carries the pricing arithmetic and the reviewer's caveat in
   full -- worth having, and four times the height of the move it explains, so
   none of it shows until it is asked for. A clamped lead line was tried first
   and read as a card leading with its own footnotes.
   `<details>` rather than script: this page ships no JavaScript. */
.why { margin: 13px 0 0; font-size: 13.5px; line-height: 1.55; }
.why > summary { list-style: none; cursor: pointer; }
.why > summary::-webkit-details-marker { display: none; }
.why > summary::after {
  content: "Show details";
  font-size: 12px; font-weight: 600;
  color: var(--primary-color, #2b7de9);
}
.why[open] > summary::after { content: "Hide details"; }
.why .detail {
  margin-top: 10px; padding-left: 12px;
  border-left: 2px solid var(--line);
  color: var(--secondary-text-color, var(--dim));
}
.why .detail p { margin: 0; }
.why .detail p + p { margin-top: 9px; }
.meta {
  font-size: 12px; line-height: 1.45;
  color: var(--secondary-text-color, var(--faint));
}
.eyebrow + .meta, .why + .meta { margin-top: 8px; }

.actions { display: flex; gap: 8px; margin-top: 16px; }
form { margin: 0; flex: 1; }
button {
  width: 100%; padding: 12px 0;
  font: inherit; font-size: 15px; font-weight: 600;
  border-radius: 11px; cursor: pointer;
  transition: filter .12s ease, background .12s ease;
}
button.ok {
  border: 0; color: var(--on-good); background: var(--good);
  box-shadow: 0 1px 2px rgba(16,18,27,.18);
}
button.no {
  border: 1px solid var(--line); background: transparent;
  color: var(--secondary-text-color, var(--dim));
}
button:hover { filter: brightness(1.06); }
button.no:hover { background: var(--chip); }
button:active { transform: translateY(1px); }

/* A run is not a disclosure. What it changed is the thing the page is open
   for, so it is on the surface of the card and nothing has to be tapped to
   reach it; only the working notes are behind a control. */
.run { padding: 0; }
.runline { padding: 14px 16px 0; min-width: 0; }
.runline .top {
  display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
  justify-content: space-between; margin-bottom: 3px;
}
.runline .name { font-size: 15px; font-weight: 600; }
.runbody { padding: 6px 16px 16px; }
.props { list-style: none; margin: 0; padding: 0; }
.props li { padding: 10px 0; border-top: 1px solid var(--line); }
.props .head { display: flex; align-items: center; gap: 7px; }
.props .kind { font-size: 13px; font-weight: 700; }
.props .swap { margin-top: 9px; gap: 7px; }
.props .swap .who { font-size: 15px; }
.sechead {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--secondary-text-color, var(--faint));
  margin: 14px 0 8px; display: flex; align-items: center; gap: 7px;
}
.props li:first-child .sechead, .runbody > .sechead:first-child {
  margin-top: 4px;
}

/* One applied lineup change: who, and the two slots. */
.did { list-style: none; margin: 0; padding: 0; }
.did li {
  display: flex; align-items: center; gap: 11px;
  padding: 7px 0; min-width: 0;
}
.did li + li { border-top: 1px solid var(--line); }
.did .who { font-size: 15px; font-weight: 600; }
.did .body { flex: 1; min-width: 0; }
/* Name left, slot change right. Both of these used to come from the unscoped
   ``.top`` above; spelling them out here is what lets that rule be scoped. */
.did .top {
  display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap;
  justify-content: space-between;
}
.did .slots {
  display: flex; align-items: center; gap: 6px; margin-top: 1px;
  font-size: 11px; font-weight: 700; letter-spacing: .04em;
  color: var(--secondary-text-color, var(--faint));
}
.did .slots .to { color: var(--good); }
.did .slots .arrow { opacity: .6; font-weight: 400; }
.did .note { font-size: 12px; color: var(--secondary-text-color, var(--dim)); }

details.logs { margin-top: 14px; border-top: 1px solid var(--line); }
details.logs > summary {
  list-style: none; cursor: pointer; padding: 11px 0 0;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--secondary-text-color, var(--faint));
}
details.logs > summary::-webkit-details-marker { display: none; }
details.logs > summary::after { content: " \\25B8"; }
details.logs[open] > summary::after { content: " \\25BE"; }
.notes { list-style: none; margin: 8px 0 0; padding: 0; }
.notes li {
  font-size: 12px; line-height: 1.5; padding: 3px 0 3px 11px;
  color: var(--secondary-text-color, var(--dim));
  border-left: 2px solid var(--line); overflow-wrap: anywhere;
}

/* -- chrome shared by the three pages ---------------------------------- */

/* Title and tabs on one line, the tabs dropping under the title on a phone.
   `space-between` and not a gap: the tab strip is a destination, and a reader
   looks for it at the edge of the page rather than next to the title. */
.mastrow {
  display: flex; flex-wrap: wrap; align-items: center;
  justify-content: space-between; gap: 10px 16px;
}
.tabs { display: flex; gap: 4px; padding: 3px; border-radius: 999px;
        background: var(--chip); }
.tab {
  padding: 6px 14px; border-radius: 999px; text-decoration: none;
  font-size: 13px; font-weight: 600;
  color: var(--secondary-text-color, var(--dim));
}
.tab.on {
  background: var(--card-background-color, var(--card));
  color: var(--primary-text-color, var(--fg));
  box-shadow: 0 1px 2px rgba(16,18,27,.12);
}
.tab:hover { color: var(--primary-text-color, var(--fg)); }
/* A detail page is somewhere you arrived from somewhere, so the way back is
   the first thing on it and not a browser gesture the reader has to know. */
.back {
  display: inline-block; margin: 0 0 12px; text-decoration: none;
  font-size: 13px; font-weight: 600;
  color: var(--secondary-text-color, var(--dim));
}
.back:hover { color: var(--primary-text-color, var(--fg)); }
.phead {
  font-size: 22px; font-weight: 700; letter-spacing: -.02em; margin: 0 0 14px;
}
/* A row of links that pick one of several things to look at. One league is
   the common case and then there is nothing to pick, so the strip is not
   rendered at all rather than rendered with a single inert pill. */
.lgpills { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 16px; }
.pill {
  padding: 7px 13px; border-radius: 999px; text-decoration: none;
  font-size: 13px; font-weight: 600; background: var(--chip);
  color: var(--secondary-text-color, var(--dim));
}
.pill.on {
  background: var(--good-bg); color: var(--good);
}

/* -- the matchup sheet -------------------------------------------------- */

.vs {
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center;
  gap: 10px; padding: 18px 16px; margin: 0 0 14px;
  border-radius: 18px; border: 1px solid var(--line);
  background: var(--card-background-color, var(--card));
  box-shadow: var(--lift);
}
.vsside { display: flex; flex-direction: column; align-items: center;
          gap: 5px; min-width: 0; text-align: center; }
.vsside .crest { flex: 0 0 38px; width: 38px; height: 38px; border-radius: 10px; }
.vspts { font-size: 28px; font-weight: 700; letter-spacing: -.03em;
         color: var(--secondary-text-color, var(--dim)); }
.vsside.up .vspts { color: var(--primary-text-color, var(--fg)); }
.vsname {
  font-size: 13px; font-weight: 600; max-width: 100%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.vsgames { font-size: 11px; color: var(--secondary-text-color, var(--faint)); }
.vsmid { font-size: 11px; font-weight: 700; text-transform: uppercase;
         letter-spacing: .06em; color: var(--secondary-text-color, var(--faint)); }
.vsgap { white-space: nowrap; }
.vsgap { display: block; margin-top: 3px; font-size: 12px; letter-spacing: 0;
         text-transform: none; }
.vsgap.lead { color: var(--good); }
.vsgap.trail { color: var(--bad); }

/* Which week, above the card whose totals are that week's. Named before the
   numbers are read, rather than after. */
.weeknav {
  display: flex; align-items: center; justify-content: center; gap: 4px;
  margin: 0 0 12px;
}
.weekis {
  flex: 1 1 auto; text-align: center;
  font-size: 13px; font-weight: 700; letter-spacing: -.01em;
}
.weekspan {
  display: block; font-size: 11px; font-weight: 500; letter-spacing: 0;
  color: var(--secondary-text-color, var(--faint));
}

/* The week as one line of chips. This replaced a pair of arrows and a date:
   the arrows made a reader page blindly through six days to find the one that
   decided the week, and the day's two scores are free -- they are on the same
   matchup row as the totals above. Scrolls rather than wraps, because a week
   is a sequence and a second row of it reads as a second week. */
.days {
  display: flex; gap: 6px; margin: 0 0 12px;
  overflow-x: auto; scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
}
.days::-webkit-scrollbar { display: none; }
.day {
  flex: 1 0 auto; min-width: 62px; text-align: center; text-decoration: none;
  padding: 7px 8px; border-radius: 11px;
  background: var(--chip); color: var(--secondary-text-color, var(--dim));
  border: 1px solid transparent;
}
.day.on {
  background: var(--card-background-color, var(--card));
  border-color: var(--primary-color, #2b7de9);
  color: var(--primary-text-color, var(--fg));
}
/* Today, when today is not the day being looked at. A ring rather than the
   fill, so "where I am" and "where now is" never look like the same thing. */
.day.now { border-color: var(--line); }
.day:hover { color: var(--primary-text-color, var(--fg)); }
.dayname {
  display: block; font-size: 10px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .06em;
}
.dayscore {
  display: block; margin-top: 2px; font-size: 11.5px; white-space: nowrap;
}
.dayscore b { font-weight: 700; }
.dayscore i { font-style: normal; opacity: .45; margin: 0 1px; }
/* Only the day's winner is in full-strength ink, the same rule the scoreboard
   panel uses on the two team totals. */
.dayscore .d1, .dayscore .d2 { color: inherit; }
.dayscore .d1.up { color: var(--good); }
.dayscore .d1.dn { color: var(--bad); }

.daynav {
  display: flex; align-items: center; justify-content: center; gap: 4px;
  margin: 0 0 12px;
}
.dayis { flex: 1 1 auto; text-align: center; font-size: 13px; font-weight: 600; }
.dayis b { color: var(--primary-color, #2b7de9); }
/* Fixed box whether or not there is a day that way, so the date underneath
   does not shuffle sideways at the ends of the week. */
.dayarrow {
  flex: 0 0 34px; height: 34px; border-radius: 10px; text-decoration: none;
  display: grid; place-items: center; font-size: 17px;
  color: var(--secondary-text-color, var(--dim)); background: var(--chip);
}
.dayarrow.off { opacity: .32; pointer-events: none; }

.sheet {
  border-radius: 16px; overflow: hidden;
  border: 1px solid var(--line);
  background: var(--card-background-color, var(--card));
  box-shadow: var(--lift);
}
.mrow { display: grid; grid-template-columns: 1fr 44px 1fr; align-items: center; }
.mrow + .mrow { border-top: 1px solid var(--line); }
.mslot {
  text-align: center; font-size: 10px; font-weight: 700; letter-spacing: .06em;
  text-transform: uppercase; color: var(--secondary-text-color, var(--faint));
  align-self: stretch; display: grid; place-items: center;
  background: var(--chip);
}
.mside { display: flex; align-items: center; gap: 8px; padding: 8px 10px;
         min-width: 0; }
.mside.them { flex-direction: row-reverse; text-align: right; }
/* No headshots on a phone. Two rosters side by side leave about seventy
   pixels of name each, and a 26px disc spends a third of that on decoration
   -- so the face is what turns "V. Wembanyama" into "V. Wemb...". ESPN's own
   matchup tab shows none either. They come back at the width that can afford
   them, and the Team page, which is one column wide, keeps them throughout. */
.mside .face { display: none; }
@media (min-width: 560px) {
  .mside { gap: 10px; padding: 9px 13px; }
  .mside .face { display: block; flex-basis: 32px; height: 32px; }
}
.mwho { flex: 1 1 auto; min-width: 0; }
.mside .who {
  display: block; font-size: 14px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mmeta {
  display: block; font-size: 10.5px; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
  color: var(--secondary-text-color, var(--faint));
}
.mmeta.out { color: var(--bad); }
.mmeta.dtd { color: var(--warn); }
/* A game in progress is the one line on the sheet that is changing, so it
   reads a shade stronger than the eleven rows around it that are not. */
.mmeta.live, .plmeta.live { color: var(--secondary-text-color, var(--dim)); }
/* Two rosters share a phone's width, and "@NYK &middot; Q2 9:19 &middot;
   38-30" does not fit beside a name in half of it. The score goes, because
   of the three facts on this line it is the one a fantasy reader needs
   least -- and because the alternative is ellipsising the clock. The Team
   page is one column wide and keeps it at every width. */
@media (max-width: 559px) { .mmeta .lvs { display: none; } }
.mpts { font-size: 15px; font-weight: 700; letter-spacing: -.02em;
        color: var(--secondary-text-color, var(--dim)); }
.mpts.scored { color: var(--primary-text-color, var(--fg)); }
/* Minutes, under the number they produced. A block so it takes its own line
   without the row growing: the points cell is one line of 15px type in a row
   padded to about forty, and this fits in what that padding already is. */
.plmin {
  display: block; margin-top: -1px;
  font-size: 11px; font-weight: 600; letter-spacing: 0;
  font-variant-numeric: tabular-nums;
  color: var(--secondary-text-color, var(--dim));
}
.mside.them .plmin, .mside.me .plmin { text-align: inherit; }
/* The day's subtotal, which is the question the sheet is open for. Reversed
   out so the eye does not have to find it among eleven rows that look alike. */
.mrow.tot { background: var(--chip); }
.mrow.tot .mpts { font-size: 17px; color: var(--primary-text-color, var(--fg)); }
.mrow.tot .mslot { background: none; }

/* -- the lineup editor -------------------------------------------------- */

.lineup { list-style: none; margin: 0 0 4px; padding: 0;
          border-radius: 16px; overflow: hidden;
          border: 1px solid var(--line);
          background: var(--card-background-color, var(--card)); }
.slotrow { display: block; }
.slotrow + .slotrow { border-top: 1px solid var(--line); }
.plr, .ph { display: flex; align-items: center; gap: 10px; padding: 9px 11px;
            min-width: 0; }
.slotrow.filled .ph { display: none; }
.ph { color: var(--secondary-text-color, var(--faint)); font-size: 12.5px; }
/* The only part of the row that starts a drag. Dragging from anywhere would
   be nicer with a mouse and unusable with a thumb: `touch-action: none` is
   what stops the browser scrolling instead, and applying it to the whole row
   would mean the page could not be scrolled past the roster at all. */
.grip {
  flex: 0 0 18px; height: 26px; cursor: grab; touch-action: none;
  display: grid; place-items: center;
  color: var(--secondary-text-color, var(--faint));
}
.grip::before {
  content: ""; width: 10px; height: 14px; opacity: .85;
  background: radial-gradient(currentColor 1.1px, transparent 1.3px) 0 0/5px 5px;
}
.plr.lock .grip { cursor: default; opacity: .3; }
.pick, .slotpill {
  flex: 0 0 auto; min-width: 56px; text-align: center;
  font: inherit; font-size: 11px; font-weight: 700; letter-spacing: .04em;
  padding: 6px 7px; border-radius: 9px;
  border: 1px solid var(--line); background-color: var(--chip);
  color: var(--secondary-text-color, var(--dim));
  -webkit-appearance: none; appearance: none;
}
/* The native control is suppressed so the pill matches the rest of the page,
   which also suppresses the one thing that said it *was* a control. The
   chevron puts that back. Drawn rather than drawn from a file for the reason
   the stylesheet is inline, and in a fixed grey rather than `currentColor`
   because a background image cannot take one: at 7px it reads on the grey
   pill and on the green one, in both themes. */
.pick {
  cursor: pointer; padding-right: 17px; min-width: 62px;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%23919aa8' stroke-width='1.7' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 6px center;
  background-size: 7px 5px;
}
.pick:focus-visible { outline: 2px solid var(--primary-color, #2b7de9);
                      outline-offset: 2px; }
.pick.start, .slotpill.start { background-color: var(--good-bg);
                               color: var(--good); border-color: transparent; }
.pick[disabled] { opacity: .55; cursor: default; background-image: none;
                  padding-right: 7px; min-width: 56px; }
.plr .face { flex-basis: 32px; height: 32px; }
.plwho { flex: 1 1 auto; min-width: 0; }
.plr .who {
  display: block; font-size: 14.5px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.plmeta {
  display: block; font-size: 10.5px; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
  color: var(--secondary-text-color, var(--faint));
}
.plmeta.out { color: var(--bad); }
.plmeta.dtd { color: var(--warn); }
.plpts { font-size: 14px; font-weight: 700; letter-spacing: -.02em;
         color: var(--secondary-text-color, var(--dim)); }
.plpts.scored { color: var(--primary-text-color, var(--fg)); }

/* Dragging. The row being carried is a fixed-position clone, so the list
   underneath keeps its geometry and nothing reflows while the thumb moves. */
.plr.lifting { opacity: .32; }
.ghost {
  position: fixed; z-index: 9; pointer-events: none; left: 0; top: 0;
  border-radius: 12px; box-shadow: 0 10px 30px -6px rgba(16,18,27,.45);
  background: var(--card-background-color, var(--card));
  border: 1px solid var(--line);
}
.slotrow.target { background: var(--good-bg); }
.slotrow.nope { background: var(--bad-bg); }

/* Sticky, because the roster is twenty-seven rows and a Save button that has
   to be scrolled to is a Save button that gets forgotten. The gradient has to
   reach full opacity quickly: rows pass *under* this bar, and a half-visible
   player name behind the button reads as a rendering fault. */
.savebar {
  position: sticky; bottom: 0; z-index: 4;
  display: flex; align-items: center; gap: 10px;
  margin-top: 4px; padding: 16px 0 10px;
  background: linear-gradient(to bottom, transparent 0,
              var(--primary-background-color, var(--bg)) 15px);
}
.savebar .count2 { flex: 1 1 auto; font-size: 12.5px;
                   color: var(--secondary-text-color, var(--dim)); }
.savebar button { width: auto; flex: 0 0 auto; padding: 11px 20px; }

/* What a league weighed and did not do, under its approval column. Closed by
   default: the reader who opens this is asking a follow-up question, and the
   one who is not should not have to scroll past the answer. */
.weighed { margin-top: 2px; border-top: 0; }
.weighed > summary { padding-top: 0; }
.cand { padding: 11px 0 2px; border-top: 1px solid var(--line); }
.cand:first-of-type { border-top: 0; }
.candhead {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; margin-bottom: 8px;
}
.candwhen {
  font-size: 11px; white-space: nowrap;
  color: var(--secondary-text-color, var(--faint));
}
/* Half-strength, because this is a list of things that did not happen and it
   sits directly under the one card that still might. */
.cand .who { font-size: 14px; }
.cand .swap { gap: 6px; }
/* A swap recovered from a run's notes: one line, because that line is the
   whole record. No faces, because the note kept names and not ids. */
.candswap { font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }

.empty {
  color: var(--secondary-text-color, var(--dim));
  font-size: 13.5px;
}
.empty b { display: block; color: var(--primary-text-color, var(--fg));
           font-size: 15px; margin-bottom: 3px; }
footer {
  margin-top: 28px; text-align: center;
  font-size: 12px; color: var(--secondary-text-color, var(--faint));
}

/* -- the player card -- */
/* Shared by the dialog and the standalone page, so one set of rules styles
   both and the two cannot drift apart. */
/* ``position``/``inset``/``margin`` are the UA's own defaults for an open
   modal in a current browser, and are spelled out because they are not in an
   older WebView: there a modal is ``position: absolute``, which anchors it to
   the top of the *document* rather than the viewport, so opening a card from
   halfway down the matchup page puts it off the top of the screen. */
#pcard {
  position: fixed; inset: 0; margin: auto;
  border: 0; padding: 0; width: min(560px, calc(100vw - 24px));
  max-height: min(86vh, 860px); border-radius: 16px;
  background: var(--card-background-color, var(--card));
  color: var(--primary-text-color, var(--fg));
  box-shadow: var(--lift); overflow: hidden;
}
#pcard::backdrop { background: rgba(8, 9, 12, .55); }
#pcard .pbody {
  max-height: inherit; overflow-y: auto; padding: 0 0 6px;
  /* Scroll shadows. The two ``local`` layers scroll with the content and so
     sit over the fixed shadow beneath them while an end is in view; once you
     scroll away from that end they travel off and uncover it. Which makes a
     shadow that appears only on the side there is more to see -- the dialog
     cuts a sentence in half at its bottom edge otherwise, and a hard cut
     reads as broken rather than as scrollable. */
  background:
    linear-gradient(var(--pbg) 42%, transparent) 50% 0 / 100% 26px no-repeat local,
    linear-gradient(to top, var(--pbg) 42%, transparent) 50% 100% / 100% 26px no-repeat local,
    linear-gradient(var(--shade), transparent) 50% 0 / 100% 14px no-repeat,
    linear-gradient(to top, var(--shade), transparent) 50% 100% / 100% 14px no-repeat;
}
.pclose { position: sticky; top: 0; z-index: 2; height: 0; text-align: right; }
.pclose button {
  position: relative; top: 10px; right: 10px;
  width: 32px; height: 32px; padding: 0; border: 0; border-radius: 999px;
  cursor: pointer; font-size: 19px; line-height: 1;
  background: var(--card-background-color, var(--card));
  color: var(--secondary-text-color, var(--dim));
  box-shadow: inset 0 0 0 1px var(--line);
}
.pclose button:hover { color: var(--primary-text-color, var(--fg)); }

/* What a scroll shadow fades out into. The card is transparent, so this is
   whatever is behind it: the page on the standalone route, the dialog's own
   surface inside the dialog. */
.pcard { --pbg: var(--primary-background-color, var(--bg)); padding: 4px 0 0; }
#pcard, #pcard .pcard { --pbg: var(--card-background-color, var(--card)); }
/* Headshot and name on the first row, the facts across the second. Side by
   side they fight: "Brandon Boston Jr." wraps to three lines to leave room
   for four right-aligned labels, and in the dialog the close button lands on
   top of the first of them. */
.phero { display: grid; grid-template-columns: auto 1fr; align-items: center;
         gap: 0 12px; padding: 16px; }
/* Only the name row keeps clear of the close button. Reserving that width
   down the whole hero costs the facts a column -- which is what puts a
   manager's surname on a line of its own. */
#pcard .pwho { padding-right: 44px; }
.face.big { flex: 0 0 56px; height: 56px; width: 56px; }
.pwho { min-width: 0; }
/* The page-wide ``h2`` is a small uppercase section heading. This one is a
   man's name, so every one of those declarations has to be undone. */
.pwho h2 {
  display: block; margin: 0; font-size: 19px; line-height: 1.2;
  font-weight: 650; text-transform: none; letter-spacing: -.01em;
  color: var(--primary-text-color, var(--fg));
}
.psub { font-size: 13px; color: var(--secondary-text-color, var(--dim)); }
.pfacts {
  /* A strip, not a table. An even grid sizes every column to the widest
     label and then wraps "Brendan Bridges" onto a line of its own inside a
     column with room to spare either side of it; letting each fact take the
     width it needs costs nothing and wraps the strip instead. */
  grid-column: 1 / -1; margin: 14px 0 0;
  display: flex; flex-wrap: wrap; gap: 10px 22px; align-items: start;
}
.pfacts div { display: block; flex: 0 1 auto; min-width: 72px; }
.pfacts dt {
  font-size: 10.5px; letter-spacing: .04em; text-transform: uppercase;
  white-space: nowrap; color: var(--secondary-text-color, var(--faint));
}
.pfacts dd { margin: 1px 0 0; font-size: 14.5px; font-weight: 650;
             line-height: 1.25; font-variant-numeric: tabular-nums; }
.pfacts dd.out { color: var(--bad); }
.pfacts dd.dtd { color: var(--warn); }

.pblock { border-top: 1px solid var(--line); padding: 12px 16px 14px; }
.pblock h3 {
  margin: 0 0 8px; font-size: 12px; font-weight: 600; letter-spacing: .04em;
  text-transform: uppercase; color: var(--secondary-text-color, var(--faint));
}
/* The eight stat columns do not fit a phone, and shrinking them to fit is
   how a table becomes unreadable. It scrolls sideways instead -- with the
   same scroll shadows as the dialog body, because a column sliced down the
   middle at the edge of the card is the other place this looks broken when
   it is only cut off. */
.pscroll {
  overflow-x: auto; margin: 0 -16px; padding: 0 16px;
  background:
    linear-gradient(to right, var(--pbg) 42%, transparent) 0 50% / 26px 100% no-repeat local,
    linear-gradient(to left, var(--pbg) 42%, transparent) 100% 50% / 26px 100% no-repeat local,
    linear-gradient(to right, var(--shade), transparent) 0 50% / 14px 100% no-repeat,
    linear-gradient(to left, var(--shade), transparent) 100% 50% / 14px 100% no-repeat;
}
.ptab { border-collapse: collapse; width: 100%; font-size: 13px;
        font-variant-numeric: tabular-nums; }
.ptab th, .ptab td { padding: 5px 0 5px 10px; text-align: right;
                     white-space: nowrap; }
.ptab thead th {
  font-size: 11px; font-weight: 600; color: var(--secondary-text-color, var(--faint));
  border-bottom: 1px solid var(--line);
}
.ptab tbody tr + tr th, .ptab tbody tr + tr td { border-top: 1px solid var(--line); }
.ptab .plab { text-align: left; padding-left: 0; font-weight: 500; }
.ptab .popp { text-align: left; color: var(--secondary-text-color, var(--dim)); }
.ptab .gp { color: var(--secondary-text-color, var(--faint)); }
/* The fantasy number leads, and is the only one with weight: the rest of the
   row is the evidence for it. */
.ptab .fpts { font-weight: 700; padding-right: 4px;
              box-shadow: inset -1px 0 0 var(--line); }
.ptab thead .fpts { box-shadow: none; }
.ptab tr.today { background: var(--good-bg); }

.pnews + .pnews { margin-top: 12px; padding-top: 12px;
                  border-top: 1px solid var(--line); }
.pwhen { font-size: 11px; letter-spacing: .03em; text-transform: uppercase;
         color: var(--secondary-text-color, var(--faint)); }
.phead2 { margin: 3px 0 0; font-size: 13.5px; font-weight: 600; }
.pspin { margin: 5px 0 0; font-size: 13px; line-height: 1.5;
         color: var(--secondary-text-color, var(--dim)); }
.pspin b { color: var(--primary-text-color, var(--fg)); margin-right: 3px; }
.pnone { margin: 0; font-size: 13px; color: var(--secondary-text-color, var(--dim)); }
"""

#: The one script this app ships, and the only page that loads it.
#:
#: It is an *enhancement* and nothing on the Team page depends on it. The
#: ``<select>`` in every row is the control; this turns the grip into a second
#: way to set the same value, because picking "UTIL" from a list eleven times
#: is not how anybody thinks about a lineup. With the script blocked -- a
#: strict frontend CSP, a reader who turns it off -- the page still renders,
#: still edits and still saves.
#:
#: Pointer events rather than HTML5 drag-and-drop: ``dragstart`` does not fire
#: for touch, and this page is read on a phone more than anywhere else. The
#: cost is ``touch-action: none``, which is why only the grip starts a drag --
#: putting it on the row would make the roster unscrollable.
#:
#: Inline rather than a file for the same reason the stylesheet is: serving a
#: second file needs the static mount this module deliberately does not have.
#: Opens a player's card over whatever page you were on.
#:
#: A progressive enhancement, deliberately: the name is a real link to a real
#: page, and everything here is an optimisation on top of that. If the script
#: does not run, if ``<dialog>`` is unsupported, or if the fetch fails, the
#: click is left alone and the browser navigates -- which lands on the same
#: markup, rendered as a page.
#:
#: Why it is worth having at all: the page a name sits on is usually the one
#: with the Approve button, and "read about him, then decide" should not cost
#: a navigation away from the decision and back.
#:
#: Modified clicks are left alone too, so middle-click and cmd-click still
#: open a tab -- the behaviour anybody who wants two players side by side
#: will reach for.
CARD_JS = """<script>
(function () {
  var dlg = document.getElementById('pcard');
  if (!dlg || typeof dlg.showModal !== 'function' || !window.fetch) return;
  var body = dlg.querySelector('.pbody');
  var token = 0;

  function plain(e) {
    return !e.defaultPrevented && e.button === 0 && !e.metaKey &&
           !e.ctrlKey && !e.shiftKey && !e.altKey;
  }

  document.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a[data-card]') : null;
    if (!a || !plain(e)) return;
    e.preventDefault();
    var mine = ++token;
    body.innerHTML = '<p class=pnone>Reading ESPN\u2026</p>';
    dlg.showModal();
    fetch(a.getAttribute('href') + '&frag=1', { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.text();
      })
      .then(function (html) {
        // A second click while the first is in flight must not have its card
        // overwritten by the slower one arriving late.
        if (mine === token) body.innerHTML = html;
      })
      .catch(function () {
        // The link still works; send them there rather than explaining.
        if (mine === token) window.location = a.getAttribute('href');
      });
  });

  // Clicking the backdrop closes it. The dialog fills its own box, so a click
  // landing on the element itself landed outside the card.
  dlg.addEventListener('click', function (e) {
    if (e.target === dlg) dlg.close();
  });
  dlg.addEventListener('close', function () { body.innerHTML = ''; });
})();
</script>"""

#: The empty shell the script fills. Rendered on every page that shows a name,
#: and inert until something opens it.
CARD_SHELL = (
    "<dialog id=pcard><form method=dialog class=pclose>"
    "<button aria-label=Close>&times;</button></form>"
    "<div class=pbody></div></dialog>"
)


DRAG_JS = """<script>
(function () {
  var form = document.getElementById('lineup');
  if (!form || !window.PointerEvent) return;
  var tally = form.querySelector('.count2');
  var drag = null;

  function held(li) { return li.querySelector('.plr'); }
  function field(plr) { return plr.querySelector('.pick, .held'); }
  function locked(plr) { return plr.getAttribute('data-locked') === '1'; }
  function fits(plr, li) {
    return (' ' + plr.getAttribute('data-eligible') + ' ')
      .indexOf(' ' + li.getAttribute('data-slot') + ' ') >= 0;
  }

  // A move is legal when both men can sit where the other is sitting, and
  // neither is locked. Capacity needs no check: every seat holds one player
  // and a swap is a permutation, so the counts cannot change.
  function legal(from, to) {
    if (!from || !to || to === from) return false;
    var a = held(from), b = held(to);
    if (!a || !fits(a, to)) return false;
    return !b || (!locked(b) && fits(b, from));
  }

  function place(li, plr) {
    li.insertBefore(plr, li.querySelector('.ph'));
    li.classList.add('filled');
    var f = field(plr);
    if (f) f.value = li.getAttribute('data-slot');
    var pick = plr.querySelector('.pick');
    if (pick) pick.classList.toggle('start', li.getAttribute('data-start') === '1');
  }

  function swap(from, to) {
    var a = held(from), b = held(to);
    if (b) place(from, b); else from.classList.remove('filled');
    place(to, a);
    count();
  }

  function count() {
    var fields = form.querySelectorAll('.pick, .held'), n = 0, i;
    for (i = 0; i < fields.length; i++) {
      if (fields[i].value !== fields[i].getAttribute('data-was')) n++;
    }
    if (tally) {
      tally.textContent = n
        ? n + (n === 1 ? ' player moved.' : ' players moved.') + ' Not saved yet.'
        : 'Nothing changed yet.';
    }
  }

  function paint() {
    if (!drag) return;
    drag.ghost.style.transform =
      'translate(' + (drag.x - drag.dx) + 'px,' + (drag.y - drag.dy) + 'px)';
    var el = document.elementFromPoint(drag.x, drag.y);
    var li = el && el.closest ? el.closest('.slotrow') : null;
    if (li !== drag.over) {
      if (drag.over) drag.over.classList.remove('target', 'nope');
      drag.over = li;
      if (li && li !== drag.from) {
        li.classList.add(legal(drag.from, li) ? 'target' : 'nope');
      }
    }
    // Edge autoscroll, on the timer rather than on movement: a thumb parked
    // at the bottom of a 27-row roster has to be able to reach the top of it
    // without letting go.
    var h = window.innerHeight;
    if (drag.y < 78) window.scrollBy(0, -11);
    else if (drag.y > h - 78) window.scrollBy(0, 11);
  }

  function stop() {
    if (!drag) return;
    var from = drag.from, over = drag.over;
    clearInterval(drag.timer);
    if (over) over.classList.remove('target', 'nope');
    drag.ghost.parentNode.removeChild(drag.ghost);
    drag.plr.classList.remove('lifting');
    drag = null;
    if (legal(from, over)) swap(from, over);
  }

  form.addEventListener('pointerdown', function (e) {
    if (e.button) return;
    var grip = e.target.closest ? e.target.closest('.grip') : null;
    if (!grip) return;
    var plr = grip.closest('.plr');
    if (!plr || locked(plr)) return;
    var box = plr.getBoundingClientRect();
    var ghost = plr.cloneNode(true);
    ghost.className = 'plr ghost';
    ghost.style.width = box.width + 'px';
    document.body.appendChild(ghost);
    plr.classList.add('lifting');
    drag = {
      plr: plr, from: plr.closest('.slotrow'), ghost: ghost, over: null,
      dx: e.clientX - box.left, dy: e.clientY - box.top,
      x: e.clientX, y: e.clientY, timer: setInterval(paint, 16)
    };
    grip.setPointerCapture(e.pointerId);
    paint();
    e.preventDefault();
  });

  document.addEventListener('pointermove', function (e) {
    if (!drag) return;
    drag.x = e.clientX;
    drag.y = e.clientY;
    paint();
    e.preventDefault();
  });
  document.addEventListener('pointerup', stop);
  document.addEventListener('pointercancel', stop);

  // The selects are the state, so a change made with one has to be counted
  // the same as one made by dragging. The row does not move under it -- it
  // will be in the right place on the next load, and shuffling the list while
  // somebody is reading it is worse than leaving it alone.
  form.addEventListener('change', count);
  count();
})();
</script>"""


#: Proposal states, mapped onto the three tag colours. Anything unlisted gets
#: the neutral chip, which is the right default for a state added later: a new
#: state painted green by accident reads as "this happened" when it may not
#: have.
STATE_TAGS = {
    "DONE": "on", "APPROVED": "on",
    "FAILED": "off", "REJECTED": "off", "EXPIRED": "off",
    "PROPOSED": "hold", "EXECUTING": "hold",
    # Neutral, not red. The app declining a swap is it working, and painting
    # a week of good judgment the same colour as a week of failed writes is
    # the thing ``Verdict.errored`` exists upstream to keep apart. The
    # dropdown that lists these picks its own colour per reason; this entry is
    # for a BLOCKED row met anywhere else, such as in a run's own card.
    "BLOCKED": "",
}

#: What ``state`` calls a write that landed. Spelled out rather than imported,
#: so the page has no reason to reach into the state machine; a test holds the
#: two in step. The first draft of this page guessed ``EXECUTED`` -- which is
#: ESPN's word on the wire, not ours -- and so painted every completed run
#: with the neutral chip and none of them with the green one.
DONE = "DONE"

#: ``ADD_DROP`` is the database's word for it, not a person's.
KIND_LABELS = {
    "ADD_DROP": "Add / drop", "ADD": "Add", "DROP": "Drop",
    "LINEUP": "Lineup", "IR": "IR move",
}


def _kind(kind: Any) -> str:
    key = str(kind).upper()
    return KIND_LABELS.get(key, key.replace("_", " ").capitalize())


def _clip(text: str, limit: int) -> str:
    """``text`` at most ``limit`` characters, with an ellipsis if it was cut."""
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


#: ESPN's public headshot CDN. No cookies and no ids of ours go with the
#: request, but it is a third party learning which players are on the page, so
#: it is one URL in one place rather than scattered through the markup.
HEADSHOT = "https://a.espncdn.com/i/headshots/nba/players/full/%d.png"


def _face(player_id: Any, extra: str = "", badge: str = "") -> str:
    """An avatar disc for a player id, or a plain one if the id is unusable.

    ``int()`` is the escaping here: the value lands inside a ``style``
    attribute, where ``_e`` would not be enough on its own, so nothing but a
    number can ever reach the URL.
    """
    try:
        style = " style='background-image:url(%s)'" % (HEADSHOT % int(player_id))
    except (TypeError, ValueError):
        style = ""
    return "<span class='face %s'%s>%s</span>" % (extra, style, badge)


#: ESPN's own NBA player page. The numeric id is the whole path -- ESPN
#: redirects to the slugged URL itself -- so no name of ours has to be made
#: URL-safe. The fallback, for a row that does not know its league.
PLAYER_URL = "https://www.espn.com/nba/player/_/id/%d"

#: The fantasy player page, scoped to a league and to one man.
#:
#: **The player card itself has no URL.** It is client state: the name in
#: ESPN's own tables is an ``<a>`` carrying no ``href``, opening the card
#: through an onClick, and the address bar does not move when it opens.
#: Re-checked on 2026-10-27 by opening it and watching ``location.href``.
#: ``playercard``, ``player/card`` and ``player`` all render ESPN's "Page not
#: found", and the legacy ``games.espn.com/fba/playercard?playerId=`` drops
#: the id on its way to a generic landing page.
#:
#: ``playernews`` does read ``playerId``, which the first pass at this missed
#: -- the route was listed as existing and never tried with an id on it.
#: Verified three ways on 2026-10-27: ``playerId=1966`` renders exactly one
#: headshot and it is LeBron's, ``playerId=3133628`` renders exactly Myles
#: Turner's, and dropping the parameter gives the whole league's news feed
#: instead. So this is a real per-player page, not a list he happens to be on.
#:
#: It carries his injury status and ESPN's "Spin" on each note, which is the
#: half of a start/sit this app does not already compute for itself. It does
#: not carry his stat line -- ESPN's own card links out to
#: ``espn.com/nba/player/stats`` for that, which is the page the owner already
#: rejected as "not as helpful", so the trade is the right way round.
FANTASY_URL = "https://fantasy.espn.com/basketball/playernews"


def _player_href(player_id: Any, league: Any = None) -> Optional[str]:
    """Where a player's name points, or ``None`` if nowhere useful.

    **This app's own card** when the league is known, which is everywhere a
    name is rendered beside a roster. It is a relative href like every other
    URL this module emits, so it resolves inside the ingress prefix; and it is
    a real page, not only what the dialog fetches, so a middle click and a
    reader without JavaScript both land somewhere that works.

    ESPN's own page otherwise -- a row journaled before the league column
    existed has no league to scope a card with, and a card is league-scoped
    because *"who has him"* is most of the question.

    Numbers only, built with ``%d`` rather than escaped with ``_e``: the only
    safe thing to interpolate into a URL is an integer, and both components
    here are ones.
    """
    # Both ids or neither: the card needs the league to say who rosters him
    # and the player to be about anybody, and a name we cannot resolve to a
    # player is a name we should not be linking at all.
    try:
        pid = int(player_id)
    except (TypeError, ValueError):
        return None
    try:
        lid = None if league is None else int(league)
    except (TypeError, ValueError):
        lid = None
    if lid is not None:
        # ``&amp;`` because the result goes straight into an attribute.
        return "player?league=%d&amp;id=%d" % (lid, pid)
    return PLAYER_URL % pid


def _who(player_id: Any, name: Any, cls: str = "who", *,
         league: Any = None) -> str:
    """A player's name, linked to where a reader would want to go next.

    Every name on this page names a real player somebody may want to read up
    on mid-decision, and what they want is his card -- what he has scored, who
    has him, whether he is hurt.

    ``data-card`` is what :data:`CARD_JS` hooks to open that card over this
    page instead of navigating away from it, which matters because the page it
    would be leaving is the one with the Approve button on it. It is set only
    on our own card links: an ESPN fallback still navigates, in a new tab, and
    carries the ``rel`` that keeps the opened tab from holding a handle on the
    authenticated frontend.

    A name we cannot build any link for renders as bare text rather than as a
    link to nowhere.
    """
    label = _e(name)
    href = _player_href(player_id, league)
    if href is None:
        return "<span class=%s>%s</span>" % (cls, label)
    if href.startswith("player?"):
        return "<a class='%s' href='%s' data-card>%s</a>" % (cls, href, label)
    return ("<a class='%s' href='%s' target=_blank "
            "rel='noopener noreferrer'>%s</a>" % (cls, href, label))


#: ESPN's slot abbreviations, as read by someone who did not write them. Only
#: the ones that are not already the word people use get an entry.
SLOT_WORDS = {"BE": "Bench"}


def _slot(name: Any) -> str:
    return SLOT_WORDS.get(str(name), str(name))


def _swap(payload: Mapping[str, Any], *, small: bool = False,
          league: Any = None) -> str:
    """The two legs of an add/drop. Shared by the queue and the history.

    Add before drop: the move is read as "who am I getting", and the man
    leaving is the cost of it, so he belongs second and dimmer.
    """
    out = ["<div class=swap>"]
    for verb, pid in (("ADD", payload.get("add")), ("DROP", payload.get("drop"))):
        if not pid:
            continue
        name = payload.get("%s_name" % verb.lower()) or "player %s" % pid
        side = "in" if verb == "ADD" else "out"
        tags = ""
        if verb == "ADD" and payload.get("waiver"):
            tags = " <span class='tag hold'>waiver claim</span>"
        badge = "<span class='pip %s'>%s</span>" % (
            side, "+" if verb == "ADD" else "−")
        out.append("<div class='leg %s'>%s%s%s</div>" % (
            side, _face(pid, "sm" if small else "", badge),
            _who(pid, name, league=league), tags,
        ))
    out.append("</div>")
    return "".join(out)


#: The two sentences ``Autopilot._review_addrop`` writes when a swap does not
#: get put to the owner. Matched here so that runs from before those swaps were
#: recorded as rows still have their history read back.
#:
#: Prose is a bad thing to parse and a worse thing to depend on, which is why
#: this is only ever consulted for a run that recorded no rows, and why
#: ``test_web`` asserts that the live code still writes sentences these match.
#: When that assertion fails the wording has moved and this has stopped finding
#: anything -- which is the point of it: the alternative to a failing test is a
#: dropdown that quietly goes empty.
_NOT_PROPOSED = re.compile(r"^(?P<swap>.+?) was not proposed -- (?P<why>.+)$",
                           re.S)
_WITHHELD = re.compile(
    r"^add/drop review unavailable, so (?P<swap>.+?) was withheld "
    r"rather than shown$", re.S)

#: What ``Verdict.describe`` says when it could not ask rather than was told.
_UNREACHABLE = "the reviewer could not be reached"


def _blocked_note(note: Any) -> Optional[Tuple[str, str, bool]]:
    """``(swap, why, unreachable)`` for a note about a withheld swap, else None.

    Deliberately narrow. A run's notes are mostly about other things -- the
    calendar, the budget, which players were undroppable -- and a loose match
    here would fill a list titled "swaps it did not put to you" with sentences
    that are not about swaps at all.
    """
    text = " ".join(str(note or "").split())
    hit = _NOT_PROPOSED.match(text)
    if hit:
        why = hit.group("why")
        return hit.group("swap"), why, why.startswith(_UNREACHABLE)
    hit = _WITHHELD.match(text)
    if hit:
        return (hit.group("swap"),
                "%s to review it, so it was withheld rather than shown"
                % _UNREACHABLE, True)
    return None


def _considered(rows: List[Mapping[str, Any]], league: Any, tz: Any) -> str:
    """What this league weighed and did not do. A disclosure, under the queue.

    *"when it says Nothing waiting for add drop it should have a drop down
    where i can see what it considered"*. "Nothing waiting" is true and
    unsatisfying on its own: it does not distinguish a quiet week from one
    where the reviewer turned down six swaps a day, and those call for
    opposite responses.

    Closed by default and absent entirely when there is nothing to show, so
    the common case costs a reader nothing. Each row names both players, says
    what became of it, and carries the reasoning behind the same "Show
    details" control the live cards use -- the verdict is a paragraph of
    somebody else's prose, and a column is not where a paragraph goes.
    """
    if not rows:
        return ""
    # Not ``_plural``, which pluralizes the last word: "2 swap not put to yous".
    out = ["<details class='logs weighed'><summary>%s</summary>"
           % _e("%d %s it did not put to you"
                % (len(rows), "swap" if len(rows) == 1 else "swaps"))]
    for row in rows:
        payload = row.get("payload") or {}
        out.append(
            "<div class=cand><div class=candhead>"
            "<span class='tag %s'>%s</span><span class=candwhen>%s</span>"
            "</div>%s%s</div>" % (
                _e(row.get("tag") or ""),
                _e(row.get("label") or ""),
                _e(_when(row.get("when"), tz)),
                # A recovered entry has names and no ids, so it is the one
                # line the note kept rather than two faces that cannot be
                # drawn. Not a degraded version of the card above it -- a
                # smaller record, shown as what it is.
                _swap(payload, small=True, league=league) if payload
                else "<div class=candswap>%s</div>" % _e(row.get("note") or ""),
                _why(row["rationale"]) if row.get("rationale") else "",
            ))
    out.append("</details>")
    return "".join(out)


def _applied(moves: List[Mapping[str, Any]], league: Any = None) -> str:
    """The lineup changes a run actually made, one row each."""
    out = ["<ul class=did>"]
    for m in moves:
        reason = _move_reason(m)
        out.append(
            "<li>%s<div class=body><div class=top>"
            "%s<span class=slots>%s"
            "<span class=arrow>&rarr;</span><span class=to>%s</span>"
            "</span></div>%s</div></li>" % (
                _face(m.get("player_id")),
                _who(m.get("player_id"),
                     m.get("name") or "player %s" % m.get("player_id"),
                     league=league),
                _e(_slot(m.get("from"))), _e(_slot(m.get("to"))),
                ("<div class=note>%s</div>" % _e(reason)) if reason else "",
            )
        )
    out.append("</ul>")
    return "".join(out)


def _moves_of(proposal: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The per-move detail of a lineup write, however old the row is.

    Runs journaled before the payload carried ``moves`` only have the one-line
    rationale ``"Name UTIL->BE; Name BE->UTIL"``, which is enough for the slots
    and the name but not for a headshot. Recovering it is all-or-nothing: a
    single bit that does not parse falls the whole proposal back to the plain
    rationale row, rather than showing a half-read list as if it were complete.
    """
    payload = proposal.get("payload")
    moves = payload.get("moves") if isinstance(payload, Mapping) else None
    if isinstance(moves, list) and moves:
        return [m for m in moves if isinstance(m, Mapping)]
    out: List[Mapping[str, Any]] = []
    for bit in str(proposal.get("rationale") or "").split("; "):
        name, sep, slots = bit.strip().rpartition(" ")
        src, arrow, dst = slots.partition("->")
        if not (name and sep and arrow and src and dst):
            return []
        out.append({"name": name, "from": src, "to": dst})
    return out


def _move_reason(move: Mapping[str, Any]) -> str:
    """The parenthetical from a move's rationale, without the slots again.

    ``pipeline`` writes ``"UTIL -> BE (no game today)"``; the slots are already
    shown as pills beside it, so only the reason is wanted. If the format ever
    changes the whole string is shown rather than nothing.
    """
    why = str(move.get("why") or "")
    head = "%s -> %s (" % (move.get("from", ""), move.get("to", ""))
    if why.startswith(head) and why.endswith(")"):
        return why[len(head):-1]
    return why


def _clock(dt: datetime) -> str:
    """``9:54 am``. Built by hand because ``%-I`` is a glibc extension.

    The add-on runs on musl, where the dash flag is not supported and the
    format string comes back through unexpanded.
    """
    return "%d:%02d %s" % (dt.hour % 12 or 12, dt.minute,
                           "am" if dt.hour < 12 else "pm")


def _when(raw: Any, tz: Any = None, now: Optional[datetime] = None) -> str:
    """A stored timestamp as a person reads it, in the configured zone.

    Runs are recorded in UTC, and ``datetime.isoformat`` keeps microseconds --
    ``2026-10-22T16:54:18.033797+00:00`` for a run the user watched happen at
    breakfast. Anything unparseable is handed back untouched rather than
    hidden: a timestamp that does not fit the format is still evidence.
    """
    text = str(raw)
    try:
        dt = datetime.fromisoformat(text[:-1] + "+00:00"
                                    if text.endswith("Z") else text)
    except (TypeError, ValueError):
        return text
    if dt.tzinfo is None:        # every writer uses UTC; see state.py
        dt = dt.replace(tzinfo=timezone.utc)
    if tz is not None:
        dt = dt.astimezone(tz)
    now = now or datetime.now(dt.tzinfo)
    days = (now.date() - dt.date()).days
    if days == 0:
        return "Today, %s" % _clock(dt)
    if days == 1:
        return "Yesterday, %s" % _clock(dt)
    return "%s %d %s, %s" % (dt.strftime("%a"), dt.day, dt.strftime("%b"),
                             _clock(dt))


def _chunks(text: Any) -> List[str]:
    """Rationale split into paragraphs on blank lines.

    The reviewer's caveat and the pricing detail arrive in one string separated
    by ``\\n\\n``; rendered as a single run of text they read as one
    undifferentiated wall, which is most of why this page looked unfinished.
    """
    return [c.strip() for c in str(text).split("\n\n") if c.strip()]


def _why(text: Any) -> str:
    """The rationale, every paragraph of it, behind one disclosure.

    Nothing of it shows closed -- not a lead line, not the first sentence. It
    is the shadow prices, the games-used arithmetic and the reviewer's caveat,
    and a card has one job above the fold: say what the move is and let it be
    answered. Asked for directly: "ALL the subtext should be hidden in show
    details which includes the calculations and reasoning."
    """
    chunks = _chunks(text)
    if not chunks:
        return ""
    return (
        "<details class=why><summary></summary><div class=detail>%s</div>"
        "</details>" % "".join("<p>%s</p>" % _e(c) for c in chunks)
    )


def _initials(name: Any) -> str:
    """Up to two letters for a team with no usable logo."""
    words = [w for w in str(name or "").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def _crest(url: Any, name: Any) -> str:
    """A team's logo, or its initials when there is nothing safe to show.

    ``teams[].logo`` is populated for every team on both live leagues, but a
    third of them are ``CUSTOM_VALID`` -- **an address a league member typed
    in**, observed pointing at imgflip, twitter and ifunny. So this is not
    ESPN's CDN the way the player headshots are, and the request is made from
    inside the authenticated frontend:

    * ``https://`` only. A relative or ``data:`` or ``javascript:`` value is
      refused outright rather than escaped and hoped about, and a plain
      ``http://`` one is refused because it would be a mixed-content block on
      a frontend served over TLS anyway.
    * ``referrerpolicy=no-referrer``, belt to the page-level ``<meta>``'s
      braces. The document URL contains the ingress session token, and that is
      not something to hand to a meme host because somebody picked a funny
      logo.
    * No ``onerror`` fallback, because that needs script and this page has
      none. A dead URL leaves an empty box, which is what the initials
      underneath it are for.
    """
    src = str(url or "").strip()
    if not src.lower().startswith("https://"):
        return "<span class=crest>%s</span>" % _e(_initials(name))
    return ("<img class=crest src='%s' alt='' loading=lazy "
            "referrerpolicy=no-referrer>" % _e(src))


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _board(b: Mapping[str, Any], tz: Any) -> str:
    """One league's head-to-head, as of that league's last run.

    Four questions in one panel, which are the four that were asked: what is
    the score, who is it against, how much of the week's games allowance has
    been spent, and what will it be once tonight is played.

    "After tonight" is the sum of a reading and a schedule lookup -- games
    already counted, plus the starters whose NBA teams play today. It is
    therefore a projection and is worded as one. It also assumes the lineup
    stands, which is fair: the run that wrote the number also wrote the lineup.
    """
    mine = _num(b.get("my_points"))
    theirs = _num(b.get("opp_points"))
    cap = _num(b.get("games_cap"))
    used = _num(b.get("my_games_used"))
    today = _num(b.get("my_games_today"))

    def row(name: Any, logo: Any, pts: Optional[float],
            games: Optional[float], leading: bool) -> str:
        return (
            "<div class='bteam%s'>%s<span class=bwho>"
            "<span class=bname>%s</span>%s</span>"
            "<span class=bpts>%s</span></div>" % (
                " up" if leading else "",
                _crest(logo, name),
                _e(name or "—"),
                ("<span class=bgames>%s</span>"
                 % _e(_games_line(games, cap))) if games is not None else "",
                "—" if pts is None else _e("%.0f" % pts),
            ))

    # Read just now, or recovered from the last run? The two deserve different
    # words: a timestamp beside a live score invites the reader to date it,
    # and a live-looking score that is four hours old is the failure this
    # whole label exists to prevent.
    if b.get("live"):
        when = "<span class='bwhen fresh'>Live</span>"
    else:
        when = "<span class=bwhen>as of %s</span>" % _e(
            _when(b.get("updated_at"), tz))
    # The whole panel is the link to the matchup behind it, rather than a
    # "details" affordance in a corner: the panel *is* the summary of that
    # page, so anywhere on it is the obvious place to press. A board with no
    # league id -- only reachable from a row written before the column existed
    # -- stays an inert section rather than linking nowhere.
    lid = b.get("league_id")
    open_tag, close_tag = "<section class=board>", "</section>"
    if lid is not None:
        open_tag = "<a class=board href='matchup?league=%s'>" % _e(lid)
        close_tag = "</a>"
    out = [open_tag,
           "<div class=bhead><span>%s</span>%s</div>"
           % (_e(b.get("league") or ""), when)]
    ahead = (mine is not None and theirs is not None and mine > theirs)
    behind = (mine is not None and theirs is not None and theirs > mine)
    out.append(row(b.get("my_name"), b.get("my_logo"), mine, used, ahead))
    if b.get("opp_team_id") is None:
        # A bye. One side, said plainly -- not a row of dashes implying an
        # opponent who has scored nothing.
        out.append("<div class=bteam><span class=crest>—</span>"
                   "<span class=bname>No opponent (bye)</span></div>")
    else:
        out.append(row(b.get("opp_name"), b.get("opp_logo"), theirs,
                       _num(b.get("opp_games_used")), behind))

    foot = []
    if mine is not None and theirs is not None and b.get("opp_team_id") is not None:
        gap = abs(mine - theirs)
        if not gap:
            foot.append("<b>Level</b>")
        else:
            foot.append("<b class='%s'>%s by %.0f</b>"
                        % ("lead" if ahead else "trail",
                           "Ahead" if ahead else "Behind", gap))
    if used is not None and today is not None:
        # "after today" rather than "once today is played": the longer phrase
        # wrapped the footer onto a second line in a panel this narrow, and said
        # nothing the short one does not.
        foot.append("<b>%.0f</b> games after today" % (used + today)
                    if today else "no games left today")
    if foot:
        out.append("<div class=bfoot>%s</div>" % " &middot; ".join(foot))
    out.append(close_tag)
    return "".join(out)


#: Injury tiers that earn a coloured meta line. Everything else -- ACTIVE, and
#: the tiers the outside sources add -- reads as "nothing to say", which is the
#: common case and should not be shouting.
STATUS_WORDS = {"OUT": ("out", "Out"), "DAY_TO_DAY": ("dtd", "Day-to-day")}


def _row(entry: Any, games: Mapping[int, Any], tz: Any,
         slate: Optional[Mapping[int, Any]] = None) -> Dict[str, Any]:
    """One roster entry flattened into the strings a template needs.

    The view model stops here. Everything below this line renders dicts of
    plain values, which is what makes the two new pages testable without an
    ESPN type in sight -- the same reason the queue and the run log are built
    from ``store`` rows rather than from ``Proposal`` objects.

    The opponent line is assembled rather than looked up: ESPN's pro schedule
    gives a side and an opponent id, and "@DAL" is the two of them together.
    A player whose team is not playing gets no line at all rather than a
    placeholder, because "no game" is already said by the dash in the points
    column.

    ``slate`` is the live scoreboard, and where it has his game it replaces
    the tipoff: a time that has passed is the one thing on the row that is
    certainly no longer true.
    """
    # Late, like ``news``: this module imports only ESPN's constants at
    # load time, so that importing it costs no network client.
    from espn import live
    p = entry.player
    game = games.get(p.pro_team_id)
    opp, tip = "", ""
    if game is not None:
        opp = "%s%s" % ("" if game.home else "@", ids.pro_team(game.opponent_id))
        if game.tipoff_utc is not None:
            when = game.tipoff_utc
            tip = _clock(when.astimezone(tz) if tz is not None else when)
        elif game.time_tbd:
            tip = "TBD"
    now = (slate or {}).get(p.pro_team_id)
    cls, word = STATUS_WORDS.get(p.injury_status, ("", ""))
    return {
        "pid": p.player_id,
        "name": p.name,
        "slot": int(p.current_slot),
        "team": ids.pro_team(p.pro_team_id),
        # The id as well as the abbreviation: the abbreviation is for a
        # reader, and the id is what narrows ESPN's fantasy player list to
        # the fifteen men this one is among. See ``_player_href``.
        "pro_team": int(p.pro_team_id or 0),
        "opp": opp,
        "tip": tip,
        # Where his game is, if it has started: "Q2 1:16" or "Final", and the
        # score from his team's side. Empty strings rather than None so the
        # template never has to ask which kind of nothing this is.
        "when": live.label(now),
        "score": "" if now is None or now.score is None or now.opp_score is None
                 else "%d-%d" % (now.score, now.opp_score),
        "points": entry.points,
        # Minutes played today, off the same ESPN row as the points. The two
        # are only meaningful together while a game is being played.
        "mins": getattr(entry, "minutes", None),
        "locked": bool(p.lineup_locked),
        "status": cls,
        "status_word": word,
        # Sorted so the markup is stable across loads, which is what makes a
        # rendered page diffable and a test assertable.
        "eligible": sorted(int(x) for x in p.eligible_slots),
    }


def _short_name(name: Any) -> str:
    """``LaMelo Ball`` -> ``L. Ball``. For the matchup sheet, and only there.

    Two rosters side by side on a phone is about seventy pixels of name per
    side, so the choice is an initial or an ellipsis, and an initial loses
    less: a surname identifies a player to someone who owns him, and
    ``Nickeil A...`` identifies nobody. The Team page is one column wide and
    keeps the full name.

    A one-word name is left alone -- turning it into an initial and nothing
    would erase it.
    """
    parts = str(name or "").split()
    if len(parts) < 2 or not parts[0]:
        return str(name or "")
    return "%s. %s" % (parts[0][0], " ".join(parts[1:]))


def _meta(row: Mapping[str, Any], cls: str = "plmeta") -> str:
    """``CHA @DAL 7:30 pm``, or ``@DAL &middot; Q2 1:16 &middot; 54-59`` once
    it has tipped off, or the injury if there is one to report.

    The injury wins the line. A day-to-day player's tipoff is of no use to
    somebody deciding whether to start him, and the two together would not fit
    in a column this narrow anyway.

    A live game beats the tipoff for the same reason, and more bluntly: the
    tipoff has already happened, so a row still showing it is a row stating
    something false. Where the game is replaces it, with the NBA score after
    -- last because it is the least of the three for a fantasy reader, and
    this line ellipsises from the right on a phone.
    """
    # A live game beats the injury tag, but only once he is on the floor:
    # minutes on the board settle for today what "Day-to-day" was guessing at,
    # and a man who is out should keep saying so while his team plays without
    # him.
    if row.get("when") and (not row.get("status_word") or row.get("mins")):
        line = "%s &middot; %s" % (
            _e(row.get("opp") or row.get("team") or ""), _e(row["when"]))
        if row.get("score"):
            # In a span of its own so half a phone can drop it rather than
            # slice the clock in half, which is the one part of this line
            # that is useless when it is cut.
            line += "<span class=lvs> &middot; %s</span>" % _e(row["score"])
        return "<span class='%s live'>%s</span>" % (cls, line)
    if row.get("status_word"):
        bits = [row.get("team") or "", row["status_word"]]
    elif row.get("opp"):
        # His own team is left out when he has a game: the opponent line
        # carries the fixture, and twelve characters of "MIL @PHI 7:30 pm" do
        # not fit in half a phone. It goes back in below, where there is no
        # fixture to spend the space on.
        bits = [row["opp"], row.get("tip") or ""]
    else:
        bits = [row.get("team") or "", "&middot;", "no game"]
    # Escaped piece by piece, so the one entity above survives and anything
    # arriving from ESPN -- a team abbreviation is still ESPN's string -- does
    # not. Joining first and escaping the result would print "&amp;middot;".
    return "<span class='%s %s'>%s</span>" % (
        cls, row.get("status") or "",
        " ".join(b if b == "&middot;" else _e(b) for b in bits if b))


def _points(value: Any, cls: str = "plpts", mins: Any = None) -> str:
    """The day's score, or a dash. ``None`` and ``0.0`` are not the same answer.

    A dash means ESPN has published nothing for this player today -- he has
    not tipped off, or is not playing. A zero means he played and scored
    nothing, which is the one number on the page worth being angry about.

    ``mins`` goes under it rather than on the fixture line, because minutes
    are the denominator of the number above them: 12.0 in six minutes and
    12.0 in thirty-six are the same cell and opposite facts. Zero minutes is
    left off -- a man who has not come on yet has a dash for points already,
    and "0m" under it says the same thing twice.
    """
    try:
        played = None if mins is None else float(mins)
    except (TypeError, ValueError):
        played = None
    under = ("" if not played
             else "<small class=plmin>%sm</small>" % _e("%d" % round(played)))
    if value is None:
        return "<span class=%s>&mdash;%s</span>" % (cls, under)
    return "<span class='%s scored'>%s%s</span>" % (
        cls, _e("%.1f" % float(value)), under)


def _slot_order(slot_counts: Mapping[int, int]) -> List[int]:
    """Every slot the league has, starters in ESPN's own id order, bench last.

    Id order is positional order -- PG, SG, SF, PF, C, then the combination
    slots, then UTIL -- so it needs no table of its own. Bench and IR are
    pulled to the end because they are not part of the lineup being compared.
    """
    starters = sorted(s for s in slot_counts if ids.is_starting_slot(s))
    tail = [s for s in (ids.BENCH_SLOT, ids.IR_SLOT) if s in slot_counts]
    return starters + tail


def _slot_seats(slot_counts: Mapping[int, int]) -> List[int]:
    """One entry per *seat*: UTIL with a capacity of four appears four times."""
    out: List[int] = []
    for slot in _slot_order(slot_counts):
        out += [slot] * max(0, int(slot_counts.get(slot, 0)))
    return out


def _seat(rows: List[Dict[str, Any]], slot: int) -> Optional[Dict[str, Any]]:
    """Take the next player sitting in ``slot``, or ``None`` if the seat is empty.

    Destructive on purpose: called once per seat down a list of seats, so a
    UTIL slot with a capacity of four hands out four different players rather
    than the same one four times.
    """
    for i, r in enumerate(rows):
        if r["slot"] == slot:
            return rows.pop(i)
    return None


def _games_line(used: Optional[float], cap: Optional[float]) -> str:
    """``12 of 30 games`` -- the denominator only when there is a cap.

    A league without ``lineupSlotStatLimits`` has no allowance to be spending,
    so "12 of — games" would be inventing a constraint. Most do have one; the
    uncapped case is why the denominator is conditional rather than assumed.
    """
    if used is None:
        return ""
    if cap:
        return "%.0f of %.0f games" % (used, cap)
    return "%.0f game%s" % (used, "" if used == 1 else "s")


def _league_columns(page: StatusPage,
                    rows: List[Dict[str, Any]],
                    *, all_leagues: bool = False
                    ) -> List[Tuple[Any, str, List[Dict[str, Any]]]]:
    """``(league_id, label, rows)`` per league, in the configured order.

    The id as well as the label because a column is not only a heading any
    more: it carries links into that league and a disclosure of what that
    league turned down, and both need the number rather than the name.

    Both lists on this page are grouped the same way, because a reader
    comparing two leagues wants the same league in the same place in both.
    Order comes from the config rather than from the data: both leagues run in
    the same second, so "whichever is newest" could swap the columns between
    two refreshes for no reason a reader could see.

    ``all_leagues`` keeps a column for a league with nothing in it, which is
    the difference between "this one has nothing to approve" and "this one is
    missing from the page".
    """
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    if all_leagues:
        for key in page.league_order():
            groups[key] = []
    for r in rows:
        # ``.get``: the scoreboard rows go through here too, and one written
        # before that column existed has no league at all. It gets its own
        # ``None`` group and a column at the end rather than taking the page
        # down with it.
        groups.setdefault(r.get("league_id"), []).append(r)
    order = [k for k in page.league_order() if k in groups]
    order += [k for k in groups if k not in order]  # a league since removed
    return [(k, page.league_label(k), groups[k]) for k in order]


def _open(title: str) -> str:
    """Everything above the first thing on the page. Shared by all three.

    One stylesheet inline for all of them: it is a few kilobytes, it is served
    from the same response, and a second file would need the static mount this
    module deliberately does not have.
    """
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        # This document's own URL contains the ingress session token, and the
        # page loads images from third parties: ESPN's headshot CDN, and
        # whatever host a league member chose for his team logo. None of them
        # gets a Referer header.
        "<meta name=referrer content=no-referrer>"
        "<title>%s</title><style>%s</style></head><body>"
        "<main class=wrap>" % (_e(title), CSS)
    )


def _close(user: str = "") -> str:
    """The foot of every page, plus the empty card dialog and its script.

    Here rather than on each page because every page this app serves renders
    player names, and a name whose card did not open on one of them would be
    a difference with no reason behind it. Both are inert until a name is
    clicked, and the markup is a few hundred bytes.
    """
    out = []
    if user:
        out.append("<footer>signed in as %s</footer>" % _e(user))
    out.append(CARD_SHELL)
    out.append(CARD_JS)
    out.append("</main></body></html>")
    return "".join(out)


#: The two places this app can be. Kept as data because the strip is rendered
#: on every page and the only thing that differs is which one is lit.
TABS = (("Overview", "."), ("Team", "team"))


def _tabs(here: str) -> str:
    """The top-level strip. ``here`` is the relative href of the current page.

    Relative hrefs, like every other URL this module emits: ingress serves the
    app under a rotating prefix, so ``/team`` would leave it. From ``.../team``
    a bare ``team`` resolves to itself and ``.`` to the directory above, which
    is the index -- the two cases that have to work.
    """
    out = ["<nav class=tabs>"]
    for label, href in TABS:
        out.append("<a class='tab%s' href='%s'>%s</a>"
                   % (" on" if href == here else "", _e(href), _e(label)))
    out.append("</nav>")
    return "".join(out)


def _league_links(page: StatusPage, here: str, current: Any) -> str:
    """One pill per configured league, or nothing when there is only one.

    A single inert pill is worse than no pill: it looks like a control and
    answers a question the reader did not ask. Two leagues is the configuration
    this was built for and the only one where the strip earns its line.
    """
    ids_ = page.league_order()
    if len(ids_) < 2:
        return ""
    out = ["<nav class=lgpills>"]
    for lid in ids_:
        out.append("<a class='pill%s' href='%s?league=%s'>%s</a>" % (
            " on" if str(lid) == str(current) else "",
            _e(here), _e(lid), _e(page.league_label(lid)),
        ))
    out.append("</nav>")
    return "".join(out)


def _oops(page: StatusPage, title: str, message: str) -> str:
    """A detail page that could not be built. Says so, and offers the way back.

    Worth a function: both pages reach ESPN on every load and neither has a
    cached form to fall back to, so this is the honest floor rather than an
    exceptional case. A blank page with a spinner would be the dishonest one.
    """
    return "".join([
        _open(title),
        "<a class=back href='.'>&larr; Overview</a>",
        "<h1 class=phead>%s</h1>" % _e(title),
        "<div class='card empty solo'><b>%s</b>"
        "The page needs a live read and that read did not come back. "
        "Nothing has been changed.</div>" % _e(message),
        _close(),
    ])


def render(page: StatusPage, *, user: str = "",
           boards: Optional[List[Dict[str, Any]]] = None) -> str:
    """The whole page, as a string.

    ``boards`` is passed in rather than fetched here because reading them
    touches the network and this function is deliberately synchronous and
    pure. The route awaits :meth:`StatusPage.live_scoreboards` and hands the
    result over; omitting it falls back to the run's cached rows, which is what
    every caller that is not a request wants.
    """
    pending = page.pending()
    shadow = page.shadow_mode()
    stuck = page.stuck()
    if boards is None:
        boards = page.scoreboards()
    tz = page.tz()
    parts: List[str] = [
        _open("NBA Autopilot"),
        "<div class=masthead>",
        "<div class=mastrow><h1>NBA Autopilot</h1>%s</div>" % _tabs("."),
    ]

    # The chips answer, above the fold, the two questions the page is opened
    # with: is it actually writing to ESPN, and does it want something from me.
    parts.append("<div class=chips>")
    parts.append("<span class='chip hold'>Shadow mode</span>" if shadow
                 else "<span class='chip on'>Live</span>")
    parts.append(
        "<span class=chip>%d waiting on you</span>" % len(pending) if pending
        else "<span class=chip>Nothing waiting</span>"
    )
    parts.append("</div></div>")

    # The head of each league's column: the standing the decisions under it are
    # in service of, not another thing to act on. Grouped through the same
    # helper as the two lists below, rather than emitted in whatever order the
    # read returned, because the only thing that makes this band worth its
    # height is sitting directly above the right league.
    if boards:
        parts.append("<div class=boards>")
        for _, _label, column in _league_columns(page, boards,
                                                 all_leagues=True):
            # A league whose score could not be read still holds its cell.
            # Collapsing it would shift every column to its left and silently
            # file the next league's score under this league's heading.
            parts.append("".join(_board(b, tz) for b in column)
                         if column else "<div></div>")
        parts.append("</div>")

    if shadow:
        parts.append(
            "<div class='card banner solo'><div class=bar></div><div>"
            "<b>Shadow mode</b><div class=meta>"
            "Decisions are recorded and reported, but nothing is sent to ESPN. "
            "Approving below marks the move approved and logs the request it "
            "would have made.</div></div></div>"
        )

    if stuck:
        parts.append(
            "<div class='card banner bad solo'><div class=bar></div><div>"
            "<b>%s in an unknown state</b><div class=meta>"
            "A POST timed out and ESPN may or may not have applied it. Runs are "
            "paused until this is reconciled.</div></div></div>"
            % _plural(len(stuck), "write")
        )

    parts.append("<h2 class=solo>Needs approval%s</h2>"
                 % ("<span class=count>%d</span>" % len(pending) if pending else ""))
    if not pending:
        # Say *why* it is empty. Only add/drop needs a human, and lineup moves
        # are applied without one, so an empty queue right after a notification
        # that said "2 move(s)" reads as a broken page rather than as a working
        # one. Reported from the phone: tapped the notification, found nothing
        # to approve, reasonably assumed something had gone wrong.
        parts.append(
            "<div class='lede solo'>Lineup and IR moves are applied "
            "automatically and appear under Recent runs; only add/drop asks "
            "first.</div>"
        )
    # A column per league, same as the runs below and the scores above, in the
    # same order, so a waiting swap sits between the score it affects and the
    # runs of the league it belongs to. The league name comes off the card's
    # eyebrow with it -- the column heading says it once instead of every card
    # saying it again -- and the kind takes that slot, which is what the eyebrow
    # was actually missing.
    #
    # Rendered whether or not anything is pending. An empty queue used to be
    # one full-width card, which answered "is anything waiting" but not "is
    # anything waiting *in this league*" -- and that second question is the
    # one somebody with two leagues is actually asking. Two cards saying
    # nothing is waiting is two answers; one card is half of one.
    columns = _league_columns(page, pending, all_leagues=True)
    # Grouped once for the whole band rather than queried per column: it is
    # one statement over a handful of indexed rows either way, and two leagues
    # should not mean two round trips to answer the same question.
    weighed: Dict[Any, List[Dict[str, Any]]] = {}
    for row in page.considered():
        column = weighed.setdefault(row.get("league_id"), [])
        if len(column) < page.CONSIDERED_PER_LEAGUE:
            column.append(row)
    if not columns:
        # No leagues configured and nothing queued, so there is no column to
        # put a card in. Reachable on a fresh install, before the options are
        # filled in -- and an empty grid there would render as a heading with
        # nothing under it at all.
        parts.append("<div class='card empty solo'><b>Nothing waiting.</b></div>")
    parts.append("<div class=leagues>")
    for lid, label, column in columns:
        parts.append("<section class=lg><h3 class=lghead>%s</h3>"
                     % _e(label))
        if not column:
            parts.append("<div class='card empty'>"
                         "<b>Nothing waiting.</b></div>")
        for p in column:
            parts.append("<div class=card>")
            parts.append(
                "<div class=eyebrow><span class=league>%s</span>%s</div>" % (
                    _e(_kind(p["kind"])),
                    ("<span class='delta%s'>%+.1f pts</span>"
                     % ("" if p["value_delta"] > 0 else " down",
                        p["value_delta"]))
                    if p["value_delta"] else "",
                ))
            parts.append(_swap(p["payload"], league=p.get("league_id")))
            if p["rationale"]:
                parts.append(_why(p["rationale"]))
            parts.append(
                "<div class=actions>"
                "<form method=post action=decide>"
                "<input type=hidden name=action_id value='%s'>"
                "<input type=hidden name=verb value=APPROVE>"
                "<button class=ok type=submit>Approve</button></form>"
                "<form method=post action=decide>"
                "<input type=hidden name=action_id value='%s'>"
                "<input type=hidden name=verb value=REJECT>"
                "<button class=no type=submit>Reject</button></form>"
                "</div>" % (_e(p["action_id"]), _e(p["action_id"]))
            )
            parts.append("</div>")
        parts.append(_considered(weighed.get(lid) or [], lid, tz))
        parts.append("</section>")
    parts.append("</div>")

    parts.append("<h2 class=solo>Recent runs</h2>")
    runs = page.recent_runs()
    if not runs:
        parts.append("<div class='card empty solo'><b>No runs recorded yet.</b>"
                     "The first scheduled run will appear here.</div>")
    else:
        # A column per league rather than one interleaved list. Two leagues run
        # a second apart, so the single column alternated between them and
        # every card had to be read to find out whose it was.
        #
        # ``all_leagues`` so the columns keep their positions when only one
        # league has run. Without it a league with no rows loses its column and
        # the one to its right slides over -- under the other league's score
        # and the other league's approvals, which is the one way this page can
        # mislead a reader who is reading down a column rather than across.
        parts.append("<div class=leagues>")
        for _lid, label, column in _league_columns(page, runs,
                                                   all_leagues=True):
            parts.append("<section class=lg><h3 class=lghead>%s</h3>"
                         % _e(label))
            if not column:
                parts.append("<div class='card empty'>"
                             "<b>No runs recorded yet.</b></div>")
            _runs(parts, column, tz)
            parts.append("</section>")
        parts.append("</div>")

    parts.append(_close(user))
    return "".join(parts)


# -- the matchup sheet ------------------------------------------------------


# -- the player card -------------------------------------------------------


def _n1(value: Any) -> str:
    """One decimal place, rounded **half up**, the way ESPN rounds.

    ``"%.1f" % 7.25`` is ``"7.2"`` in Python and ``7.3`` on ESPN's card: the
    C format uses the current rounding mode, which is round-half-to-even. The
    whole point of this page is that it can be held up against ESPN's own, so
    a column that disagrees in the last digit reads as a bug in the app even
    when the app is right.
    """
    try:
        return str(Decimal(str(float(value))).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP))
    except (TypeError, ValueError, ArithmeticError):
        return "&mdash;"


#: The box-score columns, in ESPN's own order. One tuple so the headings and
#: the cells cannot drift apart.
#:
#: The fantasy number is **not** in here. ESPN puts it last, which on a phone
#: is a column nine of ten and therefore the one off the right edge of a table
#: that has to scroll -- and it is the column the table is being read for. It
#: is rendered first instead, immediately after the row's label, where
#: scrolling cannot take it away. Pinning it there with ``position: sticky``
#: was tried first and is worse: the pinned cell sits over whichever column
#: happens to be under it, so the numbers it covers show through around its
#: edges.
CARD_COLUMNS = (
    ("MIN", "min"), ("PTS", "pts"), ("REB", "reb"), ("AST", "ast"),
    ("STL", "stl"), ("BLK", "blk"), ("TO", "to"),
)


def _stat_cells(row: Mapping[str, Any]) -> str:
    """The seven box-score numbers."""
    return "".join("<td>%s</td>" % _n1(row.get(key))
                   for _, key in CARD_COLUMNS)


def _card_head(view: Mapping[str, Any]) -> str:
    """Headshot, name, and the five facts ESPN puts beside it."""
    bits = [
        "<div class=phero>",
        _face(view.get("pid"), "big"),
        "<div class=pwho><h2>%s</h2><div class=psub>%s</div></div>" % (
            _e(view.get("name") or "Player"),
            _e(" &middot; ".join(  # noqa: FLY002
                b for b in (view.get("team"), view.get("positions")) if b)
            ).replace("&amp;middot;", "&middot;"),
        ),
        "<dl class=pfacts>",
    ]
    rank = view.get("rank")
    owned = view.get("owned")
    season = (view.get("splits") or [{}])[0]
    facts = [
        ("Avg points", "" if not season.get("games")
         else _n1(season.get("avg"))),
        ("Position rank", "" if not rank else "#%d" % int(rank)),
        ("Rostered", "" if owned is None else "%s%%" % _n1(owned)),
        ("Manager", view.get("manager") or ""),
    ]
    for label, value in facts:
        if not value:
            continue
        bits.append("<div><dt>%s</dt><dd>%s</dd></div>" % (
            _e(label), _e(value)))
    bits.append("<div><dt>Status</dt><dd class='%s'>%s</dd></div>" % (
        _e(view.get("status") or ""), _e(view.get("status_word") or "")))
    bits.append("</dl></div>")
    return "".join(bits)


def _distinct_windows(splits: Any) -> List[Mapping[str, Any]]:
    """Drop a trailing window that contains the same games as the one above it.

    The windows nest, so two of them covering the same number of games cover
    the same games -- and in the first fortnight of a season all four do. Four
    identical rows read as a bug rather than as "he has only played four
    times", which is what they actually mean, and the Season row already says
    it.
    """
    out: List[Mapping[str, Any]] = []
    for row in splits:
        if out and int(row.get("games") or 0) == int(out[-1].get("games") or 0):
            continue
        out.append(row)
    return out


def _card_stats(view: Mapping[str, Any]) -> str:
    """The Stats table: one row per window.

    ``avg`` as well as the total, because the total alone is unreadable across
    windows of different lengths -- 85.5 over a season and 85.5 over seven
    days are the same number about very different players.
    """
    rows = _distinct_windows(view.get("splits") or [])
    if not rows:
        return ""
    # Fantasy points **per game**, not the window total: 85.5 over a season
    # and 85.5 over seven days are the same number about very different
    # players, and comparing the windows is the only reason to show more than
    # one of them.
    out = ["<section class=pblock><h3>Stats</h3>",
           "<div class=pscroll><table class=ptab><thead><tr><th>Split</th>"
           "<th class=gp>GP</th><th class=fpts>FP/G</th>"]
    out.extend("<th>%s</th>" % _e(h) for h, _ in CARD_COLUMNS)
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append(
            "<tr><th class=plab>%s</th><td class=gp>%s</td>"
            "<td class=fpts>%s</td>%s</tr>" % (
                _e(row.get("label") or ""), _e(str(row.get("games") or 0)),
                _n1(row.get("avg")), _stat_cells(row)))
    out.append("</tbody></table></div></section>")
    return "".join(out)


def _card_log(view: Mapping[str, Any]) -> str:
    """The game log, newest first. Games he played, not games scheduled."""
    rows = view.get("log") or []
    if not rows:
        return ("<section class=pblock><h3>Game log</h3>"
                "<p class=pnone>No games played yet this season.</p></section>")
    out = ["<section class=pblock><h3>Game log</h3>",
           "<div class=pscroll><table class=ptab><thead><tr><th>Date</th>"
           "<th class=popp>Opp</th><th class=fpts>FPTS</th>"]
    out.extend("<th>%s</th>" % _e(h) for h, _ in CARD_COLUMNS)
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr%s><th class=plab>%s</th><td class=popp>%s</td>"
                   "<td class=fpts>%s</td>%s</tr>"
                   % (" class=today" if row.get("today") else "",
                      _e(row.get("day") or ""), _e(row.get("opp") or "&mdash;"),
                      _n1(row.get("fpts")), _stat_cells(row)))
    out.append("</tbody></table></div></section>")
    return "".join(out)


def _card_news(view: Mapping[str, Any]) -> str:
    """Recent notes. The Spin is the half worth reading; it is not truncated.

    Every string here is ESPN's, so every string here is escaped. This is the
    one block on the page whose content is prose written by somebody else.
    """
    rows = view.get("news") or []
    if not rows:
        return ("<section class=pblock><h3>Recent news</h3>"
                "<p class=pnone>Nothing filed in the last 30 days.</p>"
                "</section>")
    out = ["<section class=pblock><h3>Recent news</h3>"]
    for row in rows:
        out.append("<article class=pnews><div class=pwhen>%s</div>"
                   "<p class=phead2>%s</p>" % (
                       _e(row.get("when") or ""),
                       _e(row.get("headline") or "")))
        if row.get("spin"):
            out.append("<p class=pspin><b>Spin</b> %s</p>"
                       % _e(row["spin"]))
        out.append("</article>")
    out.append("</section>")
    return "".join(out)


def player_card_html(view: Mapping[str, Any]) -> str:
    """The card itself, with no page around it.

    Served on its own so the dialog can drop it straight into the page that
    opened it, and reused by :func:`render_player` so the standalone page and
    the dialog cannot show different things.
    """
    if view.get("error"):
        return "<div class='card empty solo'><b>%s</b>%s</div>" % (
            _e(view["error"]),
            "Nothing has been changed.")
    return "<div class=pcard>%s%s%s%s</div>" % (
        _card_head(view), _card_stats(view), _card_log(view), _card_news(view))


def render_player(page: StatusPage, view: Mapping[str, Any],
                  *, user: str = "") -> str:
    """One player, as a page of his own.

    This is what the link actually points at. The dialog is an enhancement
    layered over it, so with no JavaScript -- or in a new tab, which is where
    a middle click still sends it -- the same markup is a page that works.
    """
    name = view.get("name") or "Player"
    return "".join([
        _open("%s" % name),
        "<a class=back href='.'>&larr; Overview</a>",
        player_card_html(view),
        _close(user),
    ])


def render_matchup(page: StatusPage, view: Mapping[str, Any],
                   *, user: str = "") -> str:
    """One league's head-to-head, slot by slot, for one day of the week.

    The question this answers is the one the scoreboard panel raises and
    cannot settle: *the score is 834-749, who is still to play?* So the rows
    are paired by lineup slot -- my PG against theirs -- and each carries the
    opponent, the tipoff and the day's points, which together say whether a
    number is final or has not started.
    """
    if view.get("error"):
        return _oops(page, view.get("league") or "Matchup", view["error"])

    score = view["score"]
    parts = [
        _open("%s matchup" % (view.get("league") or "")),
        "<a class=back href='.'>&larr; Overview</a>",
        "<h1 class=phead>%s</h1>" % _e(view.get("league") or "Matchup"),
        _weeknav(view),
        _vs(score, view.get("games_cap")),
        _daystrip(view),
    ]

    mine, theirs = list(view["mine"]), list(view["theirs"])
    counts, lid = view["slot_counts"], view.get("league_id")
    starters = [s for s in _slot_seats(counts) if ids.is_starting_slot(s)]

    rows, mine_pts, their_pts = [], [], []
    for slot in starters:
        a, b = _seat(mine, slot), _seat(theirs, slot)
        rows.append("<div class=mrow>%s<div class=mslot>%s</div>%s</div>" % (
            _mside(a, "me", lid), _e(ids.slot_name(slot)),
            _mside(b, "them", lid)))
        for bucket, row in ((mine_pts, a), (their_pts, b)):
            if row is not None and row["points"] is not None:
                bucket.append(float(row["points"]))

    # The day's subtotal, which is the arithmetic a reader would otherwise do
    # down a column of eleven numbers. Summed over what ESPN has actually
    # scored, so a day not yet played totals to a dash rather than to 0.0.
    rows.append(
        "<div class='mrow tot'><div class='mside me'>%s</div>"
        "<div class=mslot>%s</div>"
        "<div class='mside them'>%s</div></div>" % (
            _points(sum(mine_pts) if mine_pts else None, "mpts"),
            _e("today" if view.get("today") else "day"),
            _points(sum(their_pts) if their_pts else None, "mpts"),
        ))
    parts.append("<div class=sheet>%s</div>" % "".join(rows))

    # Whatever is left in the two lists is everyone not starting. Behind a
    # disclosure because it is twice as long as the lineup above it and is not
    # what the page was opened for -- but present, because "who could he still
    # swap in" is the second question and it should not need another page.
    if mine or theirs:
        bench = []
        for slot in _slot_seats(counts):
            if ids.is_starting_slot(slot):
                continue
            a, b = _seat(mine, slot), _seat(theirs, slot)
            if a is None and b is None:
                continue
            bench.append("<div class=mrow>%s<div class=mslot>%s</div>%s</div>"
                         % (_mside(a, "me", lid), _e(ids.slot_name(slot)),
                            _mside(b, "them", lid)))
        if bench:
            parts.append(
                "<details class=logs><summary>Not starting</summary>"
                "<div class='sheet' style='margin-top:10px'>%s</div></details>"
                % "".join(bench))

    parts.append(_close(user))
    return "".join(parts)


def _vs(score: Any, cap: Optional[float] = None) -> str:
    """The header band: two crests, two totals, and who is up by how much."""
    mine, theirs = _num(score.my_points), _num(score.opp_points)
    bye = score.opp_team_id is None
    ahead = (not bye and mine is not None and theirs is not None
             and mine > theirs)
    behind = (not bye and mine is not None and theirs is not None
              and theirs > mine)

    def side(name: Any, logo: Any, pts: Optional[float], used: Any,
             leading: bool) -> str:
        return ("<div class='vsside%s'>%s<span class=vsname>%s</span>"
                "<span class=vspts>%s</span>%s</div>" % (
                    " up" if leading else "",
                    _crest(logo, name), _e(name or "—"),
                    "—" if pts is None else _e("%.1f" % pts),
                    ("<span class=vsgames>%s</span>"
                     % _e(_games_line(_num(used), cap)))
                    if used is not None else "",
                ))

    # "by 84.5" between two numbers is ambiguous about whose 84.5 it is, and
    # the reader of this page has exactly one side. So it is said in words --
    # and in the right tense, because a finished week that still says "Ahead
    # by" reads as a match still being played.
    done = bool(getattr(score, "final", False))
    if bye:
        gap = "<span class=vsgap>Bye week</span>"
    elif mine is None or theirs is None:
        gap = ""
    elif mine == theirs:
        gap = "<span class=vsgap>%s</span>" % ("Tied" if done else "Level")
    else:
        gap = "<span class='vsgap %s'>%s by %s</span>" % (
            "lead" if ahead else "trail",
            ("Won" if ahead else "Lost") if done else
            ("Ahead" if ahead else "Behind"),
            _e("%.1f" % abs(mine - theirs)))
    return ("<section class=vs>%s<div class=vsmid>%s%s</div>%s</section>" % (
        side(score.my_name, score.my_logo, mine, score.my_games_used, ahead),
        "final" if done else "vs",
        gap,
        side("No opponent" if bye else score.opp_name, score.opp_logo,
             None if bye else theirs, score.opp_games_used, behind),
    ))


def _dayhref(view: Mapping[str, Any], period: Any) -> str:
    """The page's only URL shape. One parameter, so the day and the week
    carried by a link can never contradict each other."""
    return "matchup?league=%s&amp;sp=%s" % (_e(view["league_id"]), _e(period))


def _weeknav(view: Mapping[str, Any]) -> str:
    """Which matchup week is on screen, and the way to the ones either side.

    Forward stops at the week being played. ESPN's schedule carries every
    future fixture as a real row with a real opponent and 0.0-0.0 on it, so
    without that bound the arrow walks into seventeen matchups that have not
    happened and all look alike.

    Rendered above the score card because it labels that card: the totals
    under it are the week's, and the week has to be named before they are
    read, not after.
    """
    week = view.get("week")
    if week is None:
        return ""

    def arrow(period: Any, glyph: str, label: str) -> str:
        if period is None:
            return "<span class='dayarrow off' aria-hidden=true>%s</span>" % glyph
        return ("<a class=dayarrow href='%s' aria-label='%s'>%s</a>"
                % (_dayhref(view, period), _e(label), glyph))

    span = view.get("week_span") or ""
    return ("<div class=weeknav>%s<span class=weekis>Week %s%s</span>%s</div>"
            % (
                arrow(view.get("prev_week"), "&lsaquo;", "Previous matchup"),
                _e(week),
                ("<span class=weekspan>%s</span>"
                 % _e("This week" if view.get("this_week") else span))
                if (span or view.get("this_week")) else "",
                arrow(view.get("next_week"), "&rsaquo;", "Next matchup"),
            ))


def _daystrip(view: Mapping[str, Any]) -> str:
    """Every day of the week as a chip, carrying what both sides scored on it.

    This replaced a pair of arrows and a date. The arrows made the reader page
    blindly through six days to find the one that decided the week; the chips
    put the whole week on one line, so "we lost it on Thursday" is visible
    before anything is tapped, and Thursday is one tap away.

    The numbers are free: they come off the same matchup row the totals above
    them do. A day ESPN has published nothing for shows a dash -- it has not
    been played -- which is the same distinction the per-player column makes.
    """
    days = view.get("days") or []
    if len(days) < 2:
        # One day is not a week and a strip of one chip is just a label.
        return ("<div class=daynav><span class=dayis>%s%s</span></div>" % (
            "<b>Today</b> &middot; " if view.get("today") else "",
            _e(view.get("day") or ""),
        ))
    out = ["<nav class=days>"]
    for day in days:
        inner = ("<span class=dayname>%s</span>"
                 "<span class=dayscore>%s</span>" % (
                     _e(day["label"]),
                     _daypair(day.get("mine"), day.get("theirs")),
                 ))
        if day.get("on"):
            # No link to where you already are, and marked for a screen
            # reader rather than only by colour.
            out.append("<span class='day on' aria-current=date>%s</span>" % inner)
        else:
            out.append("<a class='day%s' href='%s'>%s</a>" % (
                " now" if day.get("today") else "",
                _dayhref(view, day["sp"]), inner))
    out.append("</nav>")
    return "".join(out)


def _daypair(mine: Any, theirs: Any) -> str:
    """``118-96`` for one day, with the better side in full-strength ink."""
    if mine is None and theirs is None:
        return "&mdash;"
    a, b = _num(mine), _num(theirs)
    up = "" if a is None or b is None or a == b else (" up" if a > b else " dn")
    return "<b class='d1%s'>%s</b><i>%s</i><b class=d2>%s</b>" % (
        up,
        "&mdash;" if a is None else _e("%.0f" % a),
        "&ndash;",
        "&mdash;" if b is None else _e("%.0f" % b),
    )


def _mside(row: Optional[Mapping[str, Any]], side: str,
           league: Any = None) -> str:
    """One half of a matchup row. An empty seat is a dash, not a blank cell."""
    if row is None:
        return "<div class='mside %s'><span class=mmeta>—</span></div>" % side
    return ("<div class='mside %s'>%s<div class=mwho>%s%s</div>%s</div>" % (
        side, _face(row["pid"]),
        _who(row["pid"], _short_name(row["name"]), league=league),
        _meta(row, "mmeta"), _points(row["points"], "mpts", row.get("mins")),
    ))


# -- the lineup editor ------------------------------------------------------


def render_team(page: StatusPage, view: Mapping[str, Any], *, user: str = "",
                note: str = "", bad: bool = False) -> str:
    """One team's roster, as a form that posts a whole lineup.

    The control is a ``<select>`` per player, and dragging is a gesture that
    writes to those selects rather than a second way of saying the same thing.
    That ordering is deliberate: the selects work with no script at all, on a
    phone keyboard, and under a screen reader, and they are what the form
    actually submits -- so the page cannot be left in a state where what was
    dragged and what gets posted disagree.

    ``note`` carries the outcome of the last save. It is rendered above the
    roster rather than beside the button because a rejected lineup is the one
    thing on this page worth reading before anything else.
    """
    if view.get("error"):
        return _oops(page, view.get("league") or "Team", view["error"])

    counts = view["slot_counts"]
    rows = list(view["roster"])
    parts = [
        _open("%s roster" % (view.get("league") or "")),
        "<div class=masthead><div class=mastrow>"
        "<h1>Your team</h1>%s</div></div>" % _tabs("team"),
        _league_links(page, "team", view["league_id"]),
    ]
    if note:
        parts.append(
            "<div class='card banner%s solo'><div class=bar></div><div>"
            "<b>%s</b></div></div>" % (" bad" if bad else "", _e(note)))
    parts.append(
        "<div class='lede solo'>%s &middot; drag a player onto another to "
        "swap them, or pick a slot. Nothing is sent to ESPN until you "
        "save.</div>" % _e(view.get("day") or ""))

    parts.append("<form method=post action=lineup id=lineup>")
    parts.append("<input type=hidden name=league value='%s'>"
                 % _e(view["league_id"]))
    # The period is posted back rather than re-derived on arrival so that the
    # lineup that was shown and the lineup that is written are addressed to the
    # same day. A page left open across midnight should fail loudly on a stale
    # period, which ESPN does with a 409, and not quietly rewrite tomorrow.
    parts.append("<input type=hidden name=sp value='%s'>" % _e(view["period"]))

    for title, slots in (
        ("Starters", [s for s in _slot_order(counts) if ids.is_starting_slot(s)]),
        ("Bench", [ids.BENCH_SLOT]),
        ("Injured reserve", [ids.IR_SLOT]),
    ):
        seats = [s for s in _slot_seats(counts) if s in slots]
        if not seats:
            continue
        parts.append("<h2 class=solo>%s</h2><ul class=lineup>" % _e(title))
        for slot in seats:
            parts.append(_seat_row(_seat(rows, slot), slot, counts,
                                   view["league_id"]))
        parts.append("</ul>")

    # Anyone the seats did not account for: a player in a slot this league
    # does not have, or one more player in a slot than it has room for. Shown
    # rather than dropped, because ``set_lineup`` requires a *total* map and a
    # player with no field in this form would be refused by its preflight --
    # a 403 explaining nothing instead of a row explaining itself.
    if rows:
        parts.append("<h2 class=solo>Unplaced</h2><ul class=lineup>")
        for row in rows:
            parts.append(_seat_row(row, row["slot"], counts,
                                   view["league_id"]))
        parts.append("</ul>")

    parts.append(
        "<div class=savebar><span class=count2>Nothing changed yet.</span>"
        "<button class=ok type=submit>Save lineup</button></div>")
    parts.append("</form>")
    parts.append(DRAG_JS)
    parts.append(_close(user))
    return "".join(parts)


def _seat_row(row: Optional[Mapping[str, Any]], slot: int,
              counts: Mapping[int, int], league: Any = None) -> str:
    """One seat in the lineup: the player in it, or the fact that it is empty.

    Every seat renders its empty state as well as its player, hidden by the
    ``filled`` class. That is what lets a drag move a player into a vacancy
    without the script having to build markup -- it moves one element and
    toggles one class, and the two states were both rendered by the server.
    """
    empty = ("<span class=ph><span class=slotpill>%s</span>"
             "<span class=plmeta>Empty</span></span>"
             % _e(ids.slot_name(slot)))
    start = ids.is_starting_slot(slot)
    seat = "data-slot='%d' data-start='%d'" % (slot, 1 if start else 0)
    if row is None:
        return "<li class=slotrow %s>%s</li>" % (seat, empty)

    eligible = sorted({s for s in row["eligible"] if s in counts} | {slot})
    opts = "".join(
        "<option value='%d'%s>%s</option>"
        % (s, " selected" if s == slot else "", _e(ids.slot_name(s)))
        for s in eligible
    )
    field = (
        "<select class='pick%s' name='slot_%d' data-was='%d' "
        "aria-label='Lineup slot for %s'%s>%s</select>" % (
            " start" if start else "", int(row["pid"]), slot,
            _e(row["name"]), " disabled" if row["locked"] else "", opts)
    )
    if row["locked"]:
        # A disabled control submits nothing, and a lineup missing one player
        # is refused whole by the write preflight. The hidden twin is what
        # keeps the posted map total while the visible one stays unusable.
        field += ("<input type=hidden class=held name='slot_%d' value='%d' "
                  "data-was='%d'>" % (int(row["pid"]), slot, slot))
    return (
        "<li class='slotrow filled' %s>"
        "<div class='plr%s' data-pid='%d' data-eligible='%s' data-locked='%d'>"
        "<span class=grip aria-hidden=true></span>%s%s"
        "<div class=plwho>%s%s</div>%s</div>%s</li>" % (
            seat,
            " lock" if row["locked"] else "",
            int(row["pid"]),
            " ".join(str(s) for s in eligible),
            1 if row["locked"] else 0,
            field,
            _face(row["pid"]),
            _who(row["pid"], row["name"], league=league),
            _meta(row),
            _points(row["points"], "plpts", row.get("mins")),
            empty,
        ))


def _runs(parts: List[str], runs: List[Dict[str, Any]], tz: Any) -> None:
    """One league's runs, newest first.

    Every run shows what it changed without being opened first. Collapsing the
    whole run was the fix for a body that was a dozen notes long; now that the
    notes are behind their own control the body is a few moves, and hiding
    those made the page's one answer the thing hardest to reach.
    """
    for r in runs:
        # Lineup and IR writes are split out of the proposal list: they are the
        # only ones nobody approved, so they are the only ones the page has to
        # account for move by move. Everything else is a card above, or a row
        # somebody already answered.
        applied, rows = [], []
        for p in r["proposals"]:
            moves = (_moves_of(p)
                     if str(p["kind"]).upper() in ("LINEUP", "IR") else [])
            (applied if moves else rows).append((p, moves))
        nmoves = sum(len(m) for _, m in applied)

        # The note count is not repeated here: the dropdown below carries it,
        # and a run that changed nothing should say so in one line rather than
        # advertise seventeen notes about not doing anything.
        counts = [_plural(nmoves + len(rows), "move")] if nmoves or rows else []
        parts.append("<div class='card run'>")
        # The league is the column heading now, so the run leads with when it
        # ran -- which is what tells two of the same league's runs apart.
        parts.append(
            "<div class=runline><div class=top><span class=name>%s</span>"
            "<span class=tag>period %s</span>%s</div>%s</div>"
            % (_e(_when(r["started_at"], tz)), _e(r["scoring_period"]),
               "<span class='tag hold'>shadow</span>" if r["shadow_mode"] else "",
               ("<div class=meta>%s</div>" % " &middot; ".join(counts))
               if counts else "")
        )
        parts.append("<div class=runbody>")
        for p, moves in applied:
            state = str(p["state"]).upper()
            parts.append(
                "<div class=sechead>%s%s</div>" % (
                    "Set automatically" if state == DONE
                    else _e(_kind(p["kind"])),
                    "" if state == DONE else
                    "<span class='tag %s'>%s</span>" % (
                        STATE_TAGS.get(state, ""), _e(state.lower())),
                )
            )
            parts.append(_applied(moves, r.get("league_id")))
        if rows:
            parts.append("<ul class=props>")
            for p, _m in rows:
                payload = p["payload"] if isinstance(p["payload"], Mapping) else {}
                swap = (_swap(payload, small=True, league=r.get("league_id"))
                        if payload.get("add") or payload.get("drop") else "")
                parts.append(
                    "<li><div class=head><span class=kind>%s</span>"
                    "<span class='tag %s'>%s</span></div>%s%s</li>" % (
                        _e(_kind(p["kind"])),
                        STATE_TAGS.get(str(p["state"]).upper(), ""),
                        _e(str(p["state"]).lower()),
                        swap,
                        _why(p["rationale"]) if p["rationale"] else "",
                    )
                )
            parts.append("</ul>")
        if not applied and not rows:
            parts.append("<div class='meta empty'>no moves proposed</div>")
        if r["notes"]:
            # Working notes, not results: the games-used arithmetic, the
            # shadow prices, every player it refused to drop and why. Useful
            # when something looks wrong, and noise the rest of the time, so
            # the run opens on what it *did* and keeps the reasoning one tap
            # away.
            parts.append(
                "<details class=logs><summary>Logs (%d)</summary>"
                "<ul class=notes>" % len(r["notes"])
            )
            for n in r["notes"]:
                parts.append("<li>%s</li>" % _e(n))
            parts.append("</ul></details>")
        parts.append("</div></div>")


# -- deciding --------------------------------------------------------------

#: Two short fields of ``a=b&c=d``. The cap exists so an oversized body is
#: refused instead of buffered; the endpoint is reachable only through
#: authenticated ingress, so this is hygiene rather than a defence.
MAX_FORM_BYTES = 4096


async def read_form(request: Any) -> Dict[str, str]:
    """Parse the urlencoded POST body. First value wins; missing keys are absent.

    First rather than last because that is what Starlette's own ``FormData.get``
    returns, and because for a field whose two legal values are APPROVE and
    REJECT, "the earliest thing that was said" is the more conservative reading
    of a body that repeats itself.

    ``request.form()`` cannot be used. Starlette's form parser asserts on
    ``python-multipart`` for *every* content type, not just
    ``multipart/form-data``, because it borrows that package's Content-Type
    parser before it dispatches -- so the assertion fires on a plain
    ``application/x-www-form-urlencoded`` body too. That was found on the
    device as a 500: ``AssertionError: The `python-multipart` library must be
    installed to use form parsing``. Declaring the fields as ``Form(...)``
    would have hit the same wall by a different route.

    Adding the dependency would mean another wheel to resolve for musl and
    another pin to keep current, to parse two fields that ``parse_qs`` -- which
    is stdlib, and is what Starlette would ultimately have called -- handles
    directly.
    """
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_FORM_BYTES:
        log.warning("refusing a %d-byte form body", declared)
        return {}
    raw = await request.body()
    if len(raw) > MAX_FORM_BYTES:  # a chunked body declares no length
        log.warning("refusing a %d-byte form body", len(raw))
        return {}
    parsed = parse_qs(raw.decode("utf-8", "replace"))
    return {k: v[0] for k, v in parsed.items() if v}


#: Said by both write paths, so it has to be true of both: approving a swap
#: and hand-editing a lineup are the same authority over the same roster.
NOT_OWNER = (
    "This Home Assistant user is not the configured owner of this app, so it "
    "cannot change these rosters."
)


async def apply_decision(bot: Any, *, action_id: str, verb: str,
                         user_id: str) -> Optional[str]:
    """Authorize and apply one decision. ``None`` on success, else a refusal.

    Deliberately a module-level function rather than a closure inside
    ``build_app``: this is the security boundary of the page, and FastAPI is not
    installed in the development environment, so anything living inside the app
    factory is untestable there. A check that cannot be tested where it is
    written is a check that will eventually be wrong.

    Ingress has already authenticated the request against Home Assistant -- an
    unauthenticated one never reaches this process. What is decided here is
    *which* authenticated user: other household members must not be able to
    drop a player from this roster. It mirrors the notification-tap check in
    ``ha/bridge.py``, including failing closed when the id is absent.
    """
    owner = bot.cfg.ha_owner_user_id
    if owner and user_id != owner:
        log.warning("refusing decision on %s from user_id=%r (expected owner)",
                    action_id, user_id or None)
        return NOT_OWNER

    approve = str(verb).upper() == "APPROVE"
    decided = bot.store.decide(action_id, approve, user_id or "ingress")
    if decided is None:
        # Already decided, expired, or gone. Same no-op as a stale tap: the
        # redirect re-renders and the proposal simply is not there.
        log.info("ingress decision on %r changed nothing", action_id)
    elif approve:
        await bot.execute_approved()
    return None


#: How a saved lineup came back. ``bad`` drives the banner's colour and
#: nothing else; the page re-reads the roster either way, so a refused write
#: is shown against what ESPN actually has rather than against what was asked
#: for.
LineupResult = Tuple[str, bool]


async def apply_lineup(bot: Any, *, league_id: str, period: str,
                       slots: Mapping[str, str],
                       user_id: str) -> LineupResult:
    """Write a hand-edited lineup. ``(what to tell the reader, was it bad)``.

    A module-level function and not a closure in ``build_app``, for the same
    reason :func:`apply_decision` is: this is a security boundary, FastAPI is
    not installed in the development environment, and a check that cannot be
    tested where it is written is a check that will eventually be wrong.

    It validates almost nothing itself, on purpose. ``EspnWrite.set_lineup``
    already refuses a partial map, a phantom player, an ineligible slot, an
    over-full slot, a locked player being moved and a roster that would change
    size -- and it is the path every other lineup in this app goes through. A
    second, weaker copy of those rules here would be a second place for them
    to drift, and the one that drifted would be this one.

    What it does own is *who*: ingress has already authenticated the request
    against Home Assistant, and this decides whether that user is the
    configured owner. Other members of the household must not be able to bench
    somebody.
    """
    owner = bot.cfg.ha_owner_user_id
    if owner and user_id != owner:
        log.warning("refusing a lineup write from user_id=%r (expected owner)",
                    user_id or None)
        return NOT_OWNER, True

    lg = None
    for candidate in getattr(bot, "leagues", []):
        if str(candidate.ref.league_id) == str(league_id):
            lg = candidate
    if lg is None:
        return "That league is not configured.", True

    target: Dict[int, int] = {}
    for key, value in slots.items():
        if not key.startswith("slot_"):
            continue
        try:
            target[int(key[5:])] = int(value)
        except (TypeError, ValueError):
            # A field name or value that is not a number did not come from
            # this page. Refusing the whole write is right: a lineup is only
            # meaningful entire, and skipping the bad pair would submit a
            # partial map that the preflight would then reject anyway.
            log.warning("ignoring a lineup write with a malformed field %r", key)
            return "That form did not parse; nothing was sent.", True
    if not target:
        return "That form named no players; nothing was sent.", True

    try:
        scoring_period = int(period)
    except (TypeError, ValueError):
        scoring_period = None

    try:
        result = await asyncio.to_thread(
            lg.write.set_lineup, target, scoring_period=scoring_period)
    except Exception as exc:  # noqa: BLE001
        # Includes ``PreflightError`` -- two men in one slot, somebody moved
        # after his game started -- whose message is written for a person and
        # is the most useful thing that could be put on the screen.
        log.warning("manual lineup write for league %s failed: %s",
                    league_id, exc)
        # Truncated because this is carried to the next page in a query
        # string, and a preflight listing every eligible slot of a player with
        # ten of them runs to several hundred characters. The whole message is
        # in the log above, which is where a reader goes next anyway.
        return "ESPN refused that lineup: %s" % _clip(str(exc), 220), True

    if not result.items:
        return "That is already the lineup; nothing was sent.", False

    moved = _plural(len(result.items), "move")
    if result.shadow:
        return ("Shadow mode: %s recorded, nothing sent to ESPN." % moved), False
    return "Saved. %s written to ESPN." % moved, False


# -- routing ---------------------------------------------------------------


def build_app(bot: Any):
    """The FastAPI app. Imported late so the rest of the module stays testable."""
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

    page = StatusPage(bot)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    seen_headers = {"done": False}

    def identify(request: Request) -> tuple:
        """``(user_id, display_name)`` as ingress reports them.

        Logged once per process at info, because the exact header names are the
        one thing here that is taken from documentation rather than observed,
        and a silent mismatch would make every approval fail closed with no
        clue why.
        """
        uid = request.headers.get(H_USER_ID) or ""
        name = request.headers.get(H_USER_NAME) or ""
        if not seen_headers["done"]:
            seen_headers["done"] = True
            log.info(
                "ingress identity headers present: %s (user resolved: %s)",
                sorted(k for k in request.headers if k.startswith("x-remote-")),
                "yes" if uid else "NO -- approvals from the page will be refused",
            )
        return uid, name

    # Both handlers are `async def` on purpose. A sync handler would be run in
    # Starlette's threadpool, adding more threads contending for the store's
    # single connection; the page's queries are a handful of indexed reads over
    # tens of rows, so serving them on the loop is cheaper than the contention.
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        _, name = identify(request)
        # The one network call this page makes, and the reason the handler is
        # worth awaiting: a stale lineup decision is still a fact, a stale
        # score is not. It degrades to the run's cached row rather than
        # failing, so this cannot stop the page rendering.
        boards = await page.live_scoreboards()
        return HTMLResponse(render(page, user=name, boards=boards))

    @app.get("/matchup", response_class=HTMLResponse)
    async def matchup(request: Request):
        _, name = identify(request)
        view = await page.matchup_view(
            request.query_params.get("league"),
            request.query_params.get("sp"),
        )
        return HTMLResponse(render_matchup(page, view, user=name))

    @app.get("/team", response_class=HTMLResponse)
    async def team(request: Request):
        _, name = identify(request)
        # No league named: the first configured one, which is the only answer
        # that is not a guess. The pills above the roster are how you get to
        # the other, and with one league there is nothing to choose.
        order = page.league_order()
        wanted = request.query_params.get("league") or (
            order[0] if order else "")
        view = await page.team_view(wanted)
        return HTMLResponse(render_team(
            page, view, user=name,
            note=request.query_params.get("note") or "",
            bad=request.query_params.get("bad") == "1",
        ))

    @app.get("/player", response_class=HTMLResponse)
    async def player(request: Request):
        """One player's card. ``frag=1`` returns it without the page around it.

        The fragment is what the dialog fetches; the full page is what the
        link resolves to on its own. Same view model either way, so the two
        cannot disagree.
        """
        _, name = identify(request)
        order = page.league_order()
        wanted = request.query_params.get("league") or (
            order[0] if order else "")
        view = await page.player_view(wanted, request.query_params.get("id"))
        if request.query_params.get("frag") == "1":
            return HTMLResponse(player_card_html(view))
        return HTMLResponse(render_player(page, view, user=name))

    @app.post("/lineup")
    async def lineup(request: Request):
        form = await read_form(request)
        uid, _ = identify(request)
        note, bad = await apply_lineup(
            bot,
            league_id=str(form.get("league") or ""),
            period=str(form.get("sp") or ""),
            slots=form,
            user_id=uid,
        )
        # 303 back to the roster, so a refresh re-reads ESPN instead of
        # resubmitting the lineup, and the outcome is carried in the query
        # string rather than in server-side state. The page it lands on shows
        # what ESPN *has*, which after a refusal is not what was posted -- and
        # that is the point of re-reading rather than re-rendering the form.
        return RedirectResponse(
            "team?league=%s&note=%s&bad=%d" % (
                quote(str(form.get("league") or "")), quote(note), 1 if bad else 0),
            status_code=303,
        )

    @app.post("/decide")
    async def decide(request: Request):
        # Neither `Form(...)` nor `request.form()`; see `read_form`. The two
        # fields need no validation beyond "is it the owner" and "does it match
        # a PROPOSED row", both of which happen in `apply_decision`.
        form = await read_form(request)
        uid, _ = identify(request)
        refusal = await apply_decision(
            bot,
            action_id=str(form.get("action_id") or ""),
            verb=str(form.get("verb") or ""),
            user_id=uid,
        )
        if refusal:
            return PlainTextResponse(refusal, status_code=403)
        # 303 so a refresh re-reads the page instead of resubmitting the form,
        # and relative so it resolves inside the ingress prefix.
        return RedirectResponse(".", status_code=303)

    return app


async def serve(bot: Any, port: int) -> None:
    """Run the page until cancelled.

    Bound to ``0.0.0.0`` inside the container and never published on the host:
    ``config.yaml`` declares ``ingress_port`` with no ``ports:`` key, so the
    only route in is through the authenticated Supervisor ingress proxy.
    """
    import uvicorn

    config = uvicorn.Config(
        build_app(bot),
        host="0.0.0.0",
        port=int(port),
        log_level="warning",   # uvicorn's access log would duplicate every poll
        access_log=False,
    )
    server = uvicorn.Server(config)
    # uvicorn installs signal handlers by default, which would fight the ones
    # `Autopilot.serve` installed for graceful shutdown.
    server.install_signal_handlers = lambda: None
    log.info("status page listening on :%d (ingress only)", port)
    await server.serve()
