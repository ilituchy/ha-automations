"""Entrypoint. Scheduler, action listener, ingress status page.

Modes:
    python3 /app/main.py --discover   one-shot league discovery, exits
    python3 /app/main.py --once       one pipeline run, exits
    python3 /app/main.py              daemon: scheduled runs + approval loop

Startup order is deliberate. Config, then ESPN auth, then HA reachability, then
the scheduler. Each of those is a thing that can be wrong in a way the user must
be told about, and finding out at 09:00 is worse than finding out at boot.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from zoneinfo import ZoneInfo

import config as appconfig
import discover as discovery
import judge as judge_mod
import pipeline
import usage as usage_mod
from espn import ids
from espn.read import EspnAuthError, EspnError, EspnRead, make_client
from espn.write import EspnWrite
import ha.bridge as bridge_mod
from ha.bridge import Action, ActionEvent, HaBridge
from sources.espn_injuries import EspnInjuries
from solve import addrop as addrop_mod
from solve import budget as budget_mod
from solve import projections as projections_mod
from solve import value as value_mod
from solve.calendar import SeasonCalendar
from state import Store

log = logging.getLogger("autopilot")

#: How long one fetch of ESPN's injuries page is reused. Short because the
#: page is the late-breaking input -- a player ruled out at 6:40pm for a 7pm
#: tip is exactly the case this source exists to catch -- and long enough that
#: every league in a single run cycle shares one request.
INJURY_NOTE_TTL = timedelta(minutes=15)

#: Candidates one run will put in front of the add/drop reviewer, per league.
#: Separate from ``addrop_max_moves``, which caps the swaps that reach the user:
#: a blocked swap is not a move, so it must not spend the move budget, or a run
#: whose three best candidates are all vetoed stops with nothing to show while a
#: fourth the reviewer would have approved sits unexamined. This is the bound on
#: the other side -- the reviewer costs a request and several seconds each time,
#: so a league where everything is rightly refused must not loop the pool.
#:
#: Blocking all five is a legitimate outcome and not an error. It is the reason
#: the reviewer exists.
MAX_ADDROP_REVIEWS = 5

#: Distinguishes "not built yet" from "built, and the answer was None". Without
#: it a failed calendar build would be retried on every league of every run,
#: which means re-downloading a multi-megabyte season document to get the same
#: failure.
_UNSET = object()


def _ulid() -> str:
    """Lexicographically sortable run id.

    Sortable matters: run ids are embedded in notification action ids, so
    ordering them tells you which of two stale notifications is newer without
    a database lookup.
    """
    try:
        from ulid import ULID

        return str(ULID())
    except Exception:  # noqa: BLE001 - ulid is optional at dev time
        return datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")


def _redact_host(url: str) -> str:
    """``https://abc123.ui.nabu.casa`` -> ``https://***.ui.nabu.casa``.

    Keeps everything diagnostically useful -- the scheme, and whether this is
    Nabu Casa or a self-hosted domain -- and drops the one label that is
    effectively a secret: the first, which is where the random part lives in
    both ``abc123.ui.nabu.casa`` and ``ha.example.com``.

    Three labels is the threshold. An apex domain has no subdomain to hide, so
    redacting ``example.com`` into ``***.com`` would destroy the useful half of
    a string that was not sensitive to begin with.

    A bare IPv4 address is replaced whole. Dropping only its first octet would
    satisfy the label rule while still publishing most of a network address,
    which is the opposite of the point.
    """
    try:
        if "://" in url:
            scheme, _, rest = url.partition("://")
            prefix = scheme + "://"
        else:
            prefix, rest = "", url
        host, slash, path = rest.partition("/")
        hostname, colon, port = host.partition(":")
        labels = hostname.split(".")
        if len(labels) == 4 and all(l.isdigit() for l in labels):
            labels = ["***"]
        elif len(labels) >= 3:
            labels[0] = "***"
        return "%s%s%s%s%s%s" % (
            prefix, ".".join(labels), colon, port, slash, path,
        )
    except Exception:  # noqa: BLE001 - a log line must never raise
        return "<unparseable>"


def join_injuries(
    players: Sequence[Any], injuries: Optional[Any]
) -> Dict[int, Any]:
    """``player_id -> InjuryNote`` for whoever is on the page.

    Factored out of :meth:`Autopilot._availability_overrides` because there are
    now two consumers wanting different things from the same join: that method
    wants the normalized bucket, and the add/drop review wants the reporter's
    actual words. Doing the id-then-name lookup twice would be two chances to
    drift apart on which players matched, which is the sort of disagreement
    that only shows up as a swap being judged on a report about someone else.

    A module-level function rather than a method because it needs nothing from
    the instance -- and because ``tests/test_espn_injuries.py`` calls
    ``_availability_overrides`` unbound, with ``None`` for ``self``.

    Takes any sequence of players rather than the roster specifically, since
    the review has to ask about a free agent too.
    """
    if injuries is None:
        return {}
    payload = injuries.payload or {}
    by_id = payload.get("by_athlete_id") or {}
    by_name = payload.get("by_name") or {}
    from sources.crosswalk import normalize_name

    out: Dict[int, Any] = {}
    for entry in players:
        note = by_id.get(str(entry.player_id))
        if note is None:
            note = by_name.get(normalize_name(entry.name))
        if note is not None:
            out[int(entry.player_id)] = note
    return out


def _ordinal(n: int) -> str:
    """``2 -> "second"``. For prose on an approval card, nothing else."""
    return {1: "first", 2: "second", 3: "third", 4: "fourth",
            5: "fifth"}.get(n, "%dth" % n)


def _starts_today(
    work: "LineupWork", roster: Sequence[Any], scoring_period: int
) -> Optional[int]:
    """Games today's lineup will spend against the weekly cap.

    A game counts when a player is in a *starting* slot and his NBA team plays
    on this scoring period -- the same two conditions ESPN counts under, which
    is what makes "used + today" the number the owner is actually asking for
    when he wants to know where the week stands after tonight.

    Measured against ``plan.target`` -- the lineup about to be written -- not
    against the one ESPN currently has, because by the time anyone reads the
    page the write has happened. Falls back to the current lineup when there is
    no plan, which is the aborted-run case: wrong to show nothing there, since
    whatever ESPN has set is exactly what will play.

    ``None`` when there is no pro schedule to check against; the page then says
    nothing rather than implying a quiet night.
    """
    if not work.schedule:
        return None
    slots = (dict(work.plan.target) if work.plan is not None
             else {int(e.player_id): int(e.current_slot) for e in roster})
    teams = {int(e.player_id): int(e.pro_team_id or 0) for e in roster}
    return sum(
        1 for pid, slot in slots.items()
        if ids.is_starting_slot(int(slot))
        and (work.schedule.get(teams.get(int(pid), 0)) or {}).get(
            int(scoring_period))
    )


def _apply_swap(
    roster: Sequence[Any], pool: Sequence[Any], decision: Any
) -> Tuple[List[Any], List[Any]]:
    """The roster and pool as they would be if ``decision`` went through.

    So that a run offering several swaps prices the second against the first.
    Without it every attempt sees the same weakest player and the same best
    free agent, and the run proposes three different pickups all paired with
    the same man going out -- three cards, one of which can be approved.

    The acquisition lands on the **bench**, which is where ESPN puts one and is
    also what keeps the slot arithmetic honest: ``ids.active_roster_use``
    counts bench occupancy, so a drop-plus-add leaves the count where it was
    and an add into an open spot raises it by one, exactly as on ESPN. The
    lineup that eventually starts him is not this layer's business -- that is
    the rebalance after the write (ADR-020).

    A pure function returning new lists. Mutating the caller's roster would
    make the real one, read from ESPN at the top of the run, diverge from what
    the rest of the run thinks it is holding.
    """
    drop_id = (
        int(decision.drop.player_id) if decision.drop is not None else None
    )
    add_id = int(decision.add.player_id)
    next_roster = [
        e for e in roster if drop_id is None or int(e.player_id) != drop_id
    ]
    for entry in pool:
        if int(entry.player_id) == add_id:
            next_roster.append(
                dataclasses.replace(entry, current_slot=ids.BENCH_SLOT)
            )
            break
    return next_roster, [e for e in pool if int(e.player_id) != add_id]


@dataclasses.dataclass(frozen=True)
class LineupWork:
    """Everything between a roster read and a validated lineup plan.

    One object rather than a long tuple because there are now two callers that
    need the whole chain -- the scheduled run, and the rebalance that happens
    the moment an acquisition lands -- and the chain is the part that must not
    drift. ``plan_lineup`` takes eight arguments that each change the answer; two
    call sites assembling them by hand is two sets of eight chances to compute a
    different lineup for the same roster and never notice, because both would
    look like valid lineups.

    ``plan`` is ``None`` when ESPN projected nobody at all. That is not a
    failure to plan, it is a refusal: every value would be zero, so the
    "optimal" lineup would be whatever the tie-breaks happened to emit, and
    writing that is worse than leaving yesterday's lineup alone. The caller
    decides what to say about it, because the scheduled run and the rebalance
    owe the user different sentences.
    """

    plan: Optional["pipeline.LineupPlan"]
    projections: Mapping[int, Any]
    availability: Mapping[int, float]
    #: Rostered players ESPN gave no usable projection for. Excluded from the
    #: lineup rather than valued at zero -- see ``solve/projections.py``.
    skipped: Sequence[Any]
    #: ``player_id -> (bucket, label)`` from the injuries page.
    overrides: Mapping[int, Tuple[str, str]]
    injuries: Optional[Any]
    cal: Optional[SeasonCalendar]
    matchup_period: int
    schedule: Mapping[int, Mapping[int, int]]
    games_used: Optional[Any]
    notes: Tuple[str, ...]
    #: Only the sources *this* chain consulted. The run-level `"espn": "ok"`
    #: belongs to the caller, which is the thing that read the league at all.
    sources_used: Mapping[str, str]
    sources_missing: Mapping[str, str]


class League:
    """One league's read/write pair, plus the label used in notifications.

    Leagues share the HTTP client (same cookie, same hosts, and httpx pools
    connections), the store and the HA bridge. They share nothing else: the
    optimizer, the games-played budget and the roster are all league-local,
    and every proposal names its league because a notification reading only
    "drop Smith" is unactionable when two teams are in play.
    """

    def __init__(self, ref: appconfig.LeagueRef, cfg: appconfig.Config,
                 http: Any, store: Store) -> None:
        self.ref = ref
        self.name = ref.display()
        self.espn = cfg.espn(ref)
        self.read = EspnRead(self.espn, http)
        self.write = EspnWrite(
            self.espn,
            http,
            self.read,
            journal=store,
            shadow_mode=cfg.shadow_mode,
            never_drop=cfg.never_drop,
            max_faab_bid=cfg.max_faab_bid,
        )

    def __repr__(self) -> str:
        return "<League %d team=%d>" % (self.ref.league_id, self.ref.team_id)


class Autopilot:
    def __init__(self, cfg: appconfig.Config) -> None:
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.store = Store(cfg.db_path)
        # One client across leagues: identical credentials and hosts, so a
        # second client would buy nothing but a second connection pool.
        self.http = make_client(cfg.espn(cfg.leagues[0]))
        self.leagues = [League(ref, cfg, self.http, self.store)
                        for ref in cfg.leagues]
        self.ha = HaBridge(
            notify_device=cfg.ha_notify_device,
            owner_user_id=cfg.ha_owner_user_id,
            todo_entity=cfg.ha_todo_entity,
        )
        # Season-scoped, league-independent, and expensive to fetch. See
        # :meth:`season_calendar`.
        self._calendar: Any = _UNSET
        self._schedule: Optional[Dict[int, Dict[int, int]]] = None
        #: The same payload kept whole, for the status page. See
        #: :meth:`pro_games`.
        self._pro_games: Optional[Mapping[int, Mapping[int, Any]]] = None
        # League-independent but *not* season-scoped: injuries move hourly, so
        # this one carries a TTL rather than living for the process. See
        # :meth:`injury_notes`.
        self._injuries: Any = None
        self._injuries_at: Optional[datetime] = None
        # ``None`` means no review gate. Built from the presence of a key
        # rather than from a separate on/off switch, so there is one fact to
        # get right instead of two that can contradict each other -- a key
        # with the switch off, or a switch on with no key, would both need a
        # behaviour nobody could predict from the Configuration tab.
        self.judge: Optional[judge_mod.AddDropJudge] = None
        if cfg.anthropic_api_key:
            self.judge = judge_mod.AddDropJudge(
                cfg.anthropic_api_key, model=cfg.ai_judge_model
            )
            log.info(
                "add/drop review is on (%s): every proposed swap is checked "
                "before you see it, and one that cannot be checked is not "
                "shown", cfg.ai_judge_model,
            )
        else:
            log.info(
                "add/drop review is off: no anthropic_api_key is set, so swaps "
                "are proposed on their points case alone"
            )

    def league_for_run(self, run_id: str) -> Optional[League]:
        """Map a stored run back to its league, to execute an approved tap.

        A tap carries only a run id, so this is the step that decides whose
        roster a move lands on. It returns ``None`` rather than defaulting to
        the first league: writing to the wrong league is precisely what the
        ownership check exists to prevent, and a silent fallback would route
        around that check instead of tripping it.
        """
        row = self.store.db.execute(
            "SELECT league_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or row["league_id"] is None:
            log.error("run %s has no league recorded; refusing to execute", run_id)
            return None
        for lg in self.leagues:
            if lg.ref.league_id == int(row["league_id"]):
                return lg
        log.error(
            "run %s belongs to league %s, which is no longer configured; "
            "refusing to execute", run_id, row["league_id"],
        )
        return None

    # -- preflight at boot -------------------------------------------------

    def startup_checks(self) -> bool:
        ok = True
        log.info("config: %s", json.dumps(self.cfg.redacted(), default=str))

        for lg in self.leagues:
            try:
                settings = lg.read.league_settings()
                log.info(
                    "ESPN OK: [%d] %s, %s, scoring period %d",
                    lg.ref.league_id,
                    settings.name,
                    settings.scoring_type,
                    settings.current_scoring_period,
                )
            except EspnAuthError as exc:
                # One cookie serves every league, so an auth failure is global.
                # Say it once and stop rather than repeating it per league.
                log.error("ESPN auth failed: %s", exc)
                self._notify_problem(
                    "ESPN cookie expired",
                    "Autopilot cannot reach your leagues. Re-copy espn_s2 from "
                    "your browser and update the app configuration.",
                )
                return False
            except EspnError as exc:
                log.error("league %d unreachable: %s", lg.ref.league_id, exc)
                ok = False
                continue

            mine = lg.read.my_team_ids()
            if lg.ref.team_id not in mine:
                log.error(
                    "league %d: team_id %d is not owned by this SWID (owned: "
                    "%s). Refusing to run -- writing to a team you do not own "
                    "is permitted by ESPN and would be publicly attributed to "
                    "you.",
                    lg.ref.league_id,
                    lg.ref.team_id,
                    sorted(mine),
                )
                ok = False

        ext = self.ha.check_external_url()
        if ext is None:
            # Not fatal: lineup/IR autonomy needs no approval, so the app is
            # still useful. But add/drop approval cannot work, so say so.
            log.warning(
                "HA is not externally reachable. Autonomous lineup and IR moves "
                "will still run; add/drop approvals will not be actionable."
            )
        else:
            # Only the fact matters here, not the address. A Nabu Casa hostname
            # is a long random label that fronts this instance's login page,
            # and app logs get pasted into bug reports; printing the whole
            # thing on every start buys nothing the scheme and suffix don't.
            log.info("HA external access: OK (%s)", _redact_host(ext))

        stuck = self.store.stuck_writes()
        if stuck:
            log.error(
                "%d indeterminate write(s) from a previous run need "
                "reconciliation before new writes are safe: %s",
                len(stuck),
                [s["id"] for s in stuck],
            )
            ok = False

        if self.cfg.shadow_mode:
            log.warning(
                "SHADOW MODE: the full pipeline runs and notifies, but no "
                "request reaches ESPN. Turn this off once a few runs look right."
            )
        return ok

    def _notify_problem(self, title: str, message: str) -> None:
        try:
            self.ha.notify(title=title, message=message, tag="autopilot-problem",
                           actions=())
            self.ha.persistent_notification(title, message)
        except Exception as exc:  # noqa: BLE001
            log.error("could not deliver problem notification: %s", exc)

    # -- the daily run -----------------------------------------------------

    def run_once(self) -> List[str]:
        """Run every configured league. Returns the run ids.

        A league that throws does not stop the others. They are independent
        problems that happen to share a cookie, and skipping today's lineup in
        league B because league A's roster fetch timed out would be a cost with
        no corresponding benefit.
        """
        run_ids: List[str] = []
        for lg in self.leagues:
            try:
                run_ids.append(self.run_league(lg))
            except EspnError as exc:
                log.error("league %s failed: %s", lg.name, exc)
            except Exception:  # noqa: BLE001 - one league must not kill the rest
                log.exception("league %s raised", lg.name)
        return run_ids

    def season_calendar(self) -> Optional[SeasonCalendar]:
        """The NBA calendar, built once per process.

        Derived from the pro schedule, not from any league's settings, so it is
        the same object for every configured league -- and the payload it comes
        from is multi-megabyte, which is the practical reason it is cached
        rather than the conceptual one.
        """
        if self._calendar is _UNSET:
            try:
                dates = self.leagues[0].read.scoring_period_dates()
                self._calendar = SeasonCalendar.from_period_dates(dates)
            except EspnError as exc:
                log.error("could not build the season calendar: %s", exc)
                self._calendar = None
        return self._calendar

    def pro_schedule(self) -> Mapping[int, Mapping[int, int]]:
        """``proTeamId -> {scoringPeriod: games}``, built once per process."""
        if self._schedule is None:
            try:
                self._schedule = self.leagues[0].read.pro_team_scoring_periods()
            except EspnError as exc:
                log.error("could not read the pro schedule: %s", exc)
                self._schedule = {}
        return self._schedule

    def pro_games(self) -> Mapping[int, Mapping[int, Any]]:
        """``scoringPeriod -> {proTeamId: ProGame}``, built once per process.

        Same payload and the same reason as :meth:`pro_schedule` -- which
        counts the games this keeps whole -- so the two are deliberately two
        parses of one multi-megabyte response rather than two requests. Only
        the status page reads this one: the optimizer wants counts and nothing
        else, and a game's tipoff time is a thing to *show* a reader rather
        than a thing to decide with.
        """
        if self._pro_games is None:
            try:
                self._pro_games = self.leagues[0].read.pro_games_by_period()
            except EspnError as exc:
                log.error("could not read the pro game schedule: %s", exc)
                self._pro_games = {}
        return self._pro_games

    def injury_notes(self) -> Optional[Any]:
        """ESPN's injuries page, shared across leagues, refreshed on a TTL.

        Unlike the calendar and the pro schedule this is not season-static --
        a note can flip two hours before tip -- so caching it for the life of
        the process would mean a lineup written at 7pm acted on the morning's
        news. The TTL is short enough that every run inside one cycle shares
        one fetch and the next cycle gets fresh data.

        Returns the ``SourceResult`` or ``None``; ``None`` is a degraded run,
        not a failed one, and the caller records it in ``sources_missing``.
        """
        now = datetime.now(timezone.utc)
        if (
            self._injuries is not None
            and self._injuries_at is not None
            and now - self._injuries_at < INJURY_NOTE_TTL
        ):
            return self._injuries
        self._injuries = EspnInjuries().fetch()
        self._injuries_at = now
        return self._injuries

    def _availability_overrides(
        self, roster: Sequence[Any], injuries: Optional[Any]
    ) -> Tuple[Dict[int, Tuple[str, str]], List[str]]:
        """Build ``player_id -> (status, label)`` from the injuries page.

        The join is a plain id match: ESPN's fantasy ``playerId`` and its site
        ``athlete`` id are the same number (verified 2026-10-21 -- Mouhamed
        Gueye is 4712863 in both the fantasy player list and the injuries
        page), so this source needs no crosswalk. Players the page could not
        give an id for fall back to a normalised-name lookup, which is why
        both indexes are carried in the payload.

        This **reports** disagreement rather than resolving it. ESPN's fantasy
        ``injuryStatus`` stays the floor -- ``p_play_for`` only honours an
        override that is *worse* -- so what comes back from here is a note
        about what the beat reporters said, and the run record keeps both
        readings.
        """
        if injuries is None:
            return {}, []

        overrides: Dict[int, Tuple[str, str]] = {}
        notes: List[str] = []
        joined = join_injuries(roster, injuries)
        by_id = (injuries.payload or {}).get("by_athlete_id") or {}
        for entry in roster:
            note = joined.get(int(entry.player_id))
            if note is None:
                continue
            matched_on = "id" if str(entry.player_id) in by_id else "name"

            label = "espn-injuries" if not note.comment_matched else "espn-beat-report"
            overrides[entry.player_id] = (note.bucket, label)

            espn_status = entry.availability
            if note.bucket != espn_status:
                notes.append(
                    "%s: ESPN fantasy says %s, injuries page says %s%s"
                    % (
                        entry.name,
                        espn_status,
                        note.bucket,
                        " (%r)" % note.comment_phrase if note.comment_phrase else "",
                    )
                )
            if matched_on == "name":
                log.info(
                    "espn_injuries: matched %s by name, not id", entry.name
                )
        return overrides, notes

    def settings_now(self, lg: "League") -> Tuple[Any, Optional[str]]:
        """This league's settings, with the scoring period set to *today's*.

        The one way the rest of the app should read settings. ESPN's own
        ``latestScoringPeriod`` is right for most of the day and wrong for the
        rest of it (see :meth:`today_period`), and the field it lands in is
        read by the optimizer, the budget horizon, the games-used baseline and
        every write -- so correcting it in one place is the only way they can
        be made to agree.
        """
        settings = lg.read.league_settings()
        period, note = self.today_period(settings)
        if note:
            log.warning("league %d: %s", lg.ref.league_id, note)
            settings = dataclasses.replace(
                settings, current_scoring_period=period
            )
        return settings, note

    def today_period(self, settings: Any) -> Tuple[int, Optional[str]]:
        """The scoring period a write sent right now is addressed to.

        ESPN's ``status.latestScoringPeriod`` is the latest period it has
        *results* for, and it lags the period the league is actually in by
        most of a morning. Observed live: on 2026-10-22 the 09:00 run read
        period 2 and the 09:54 run read 3, with no NBA game having finished in
        between; the number simply rolled over around midday Eastern.

        The write service does not lag. It accepts the current period and
        rejects every other one with ``409
        TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT``. So on 2026-10-23 the 09:00
        run addressed its lineup to period 3, ESPN was already in 4, and both
        leagues lost the entire day's lineup to a rejection -- the only reason
        it had not happened sooner is that the mornings before it had nothing
        to move, and a write with no items is never sent.

        The season calendar already knows which period a date is, in the same
        US Eastern the periods are keyed in, and it is checked against ESPN
        every run. So the date is the authority here and
        ``latestScoringPeriod`` is only the fallback -- used when there is no
        trustworthy calendar, or when today falls outside the season, which is
        the offseason and not a disagreement about anything.

        Returns the period and, when it is not the one ESPN reported, a note
        saying so. The note is worth keeping: a persistent disagreement in the
        other direction would mean the calendar had drifted, and this is the
        only place the two numbers are ever compared.
        """
        espn = int(settings.espn_latest_scoring_period
                   or settings.current_scoring_period)
        cal = self.season_calendar()
        if cal is None or not cal.trustworthy:
            return espn, None
        today = datetime.now(ids.GAME_DATE_TZ).date()
        period = cal.period_of(today)
        if not cal.in_season(period) or period == espn:
            return espn, None
        return period, (
            "ESPN reports scoring period %d, but %s is period %d; writing to "
            "%d, which is the one ESPN accepts" % (espn, today, period, period)
        )

    def _calendar_for(
        self, settings: Any
    ) -> Tuple[Optional[SeasonCalendar], int, List[str]]:
        """The calendar, this league's matchup period, and any complaints.

        The cross-check runs here rather than in a test because it is checking
        ESPN's behaviour, not ours. If the week convention ever changes, the
        model keeps returning plausible numbers and only this comparison
        notices; a calendar that fails it is downgraded to ``None``, which
        switches cap pricing off rather than letting it be wrong.
        """
        notes: List[str] = []
        cal = self.season_calendar()
        if cal is None:
            return None, int(settings.current_matchup_period), [
                "no season calendar: the games-played cap cannot be priced"
            ]
        # ESPN's own pair, not the corrected period -- see the note on
        # LeagueSettings.espn_latest_scoring_period.
        problem = cal.verify(
            settings.espn_latest_scoring_period or settings.current_scoring_period,
            settings.current_matchup_period,
        )
        if problem:
            log.error(
                "league %d: season calendar disagrees with ESPN -- %s. "
                "Not pricing the games-played cap this run.",
                settings.league_id, problem,
            )
            return None, int(settings.current_matchup_period), [
                "calendar cross-check failed: %s" % problem
            ]
        matchup = cal.matchup_period_of(settings.current_scoring_period)
        notes.append(cal.describe(matchup))
        return cal, matchup, notes

    def _consider_addrops(
        self,
        lg: "League",
        settings: Any,
        roster: Any,
        projections: Mapping[Any, Any],
        plan: "pipeline.LineupPlan",
        *,
        cal: Optional[SeasonCalendar],
        matchup_period: int,
        schedule: Mapping[int, Mapping[int, int]],
        injuries: Optional[Any] = None,
        matchup: Optional[Any] = None,
    ) -> Tuple[List["addrop_mod.AddDropPlan"], List[str],
               List[Tuple["addrop_mod.AddDropPlan", str]]]:
        """Price the pool and decide up to ``addrop_max_moves`` swaps. Reads only.

        Returns the swaps it settled on, the notes, and the ones it turned
        down with the reason each time. The third list is *returned* rather
        than written here because this method reads and nothing else -- the
        caller holds the ``run_id`` and owns every write in a run, and that
        separation is what makes a pool fetch that fails cost nothing.

        Every read here is wrapped, because none of them is worth a run. The
        lineup has already been computed and written by the time this is called
        -- the valuable, time-critical half of the run is done -- so a pool
        fetch that fails must degrade to "no add/drop this run" and nothing
        else. That ordering is the reason this is a separate method rather than
        part of ``plan_lineup``.

        ``injuries`` is the same page the availability overrides came from, and
        it is passed in rather than re-fetched so that the review reads the
        reporting this run actually acted on. A second fetch could legitimately
        return something newer, and a reviewer shown different news from the
        model it is reviewing would be answering a different question.

        ``matchup`` is passed in for the same reason and one more: the caller
        already read it to cache the scoreboard, and reading it again here
        would be a second request for a number that cannot have changed in the
        intervening second. ``None`` means a bye, the preseason, or a failed
        read -- all of which this method already has to handle.

        Returns the swaps to offer, best first, and the notes for all of them
        including the ones that were declined or blocked. The list is empty when
        there is nothing worth doing, which is the ordinary outcome; the reason
        is always in the notes.
        """
        notes: List[str] = []
        if not settings.is_points:
            # The gain is measured in points, and in a category league there is
            # no such number. Declining is honest; converting category value
            # into a points-looking float would not be.
            notes.append(
                "add/drop not considered: this is a %s league and the swap "
                "arithmetic is in points" % settings.scoring_type
            )
            return [], notes, []

        horizon = (
            cal.remaining_periods(matchup_period, settings.current_scoring_period)
            if cal is not None and cal.trustworthy
            else []
        )
        if not horizon:
            notes.append(
                "add/drop not considered: no trustworthy horizon for matchup "
                "period %d, so a pickup's games cannot be counted"
                % matchup_period
            )
            return [], notes, []

        try:
            pool = lg.read.free_agents()
        except Exception as exc:  # noqa: BLE001 - the lineup is already applied
            log.error("could not read the free-agent pool: %s", exc)
            notes.append("add/drop not considered: pool read failed (%s)" % exc)
            return [], notes, []

        model = value_mod.build_value_model(settings)
        values = dict(plan.values)
        pool_projections: Dict[Any, Any] = {}
        if pool:
            try:
                splits = lg.read.player_projections([e.player_id for e in pool])
            except Exception as exc:  # noqa: BLE001
                log.error("could not project the free-agent pool: %s", exc)
                notes.append(
                    "add/drop not considered: pool projections failed (%s)" % exc
                )
                return [], notes, []
            pool_projections, _pool_avail, unprojected = (
                projections_mod.build_projections(
                    pool, splits, as_of=datetime.now(timezone.utc)
                )
            )
            for pid, proj in pool_projections.items():
                values[pid] = float(model.conditional_value(proj))
            if unprojected:
                # Left at zero value rather than dropped, so they are
                # unproposable instead of invisible -- the same convention the
                # roster side uses.
                notes.append(
                    "%d of %d pool players have no usable projection and are "
                    "valued at zero" % (len(unprojected), len(pool))
                )

        # Our own players need to be in ``projections`` too: the drop side is
        # priced with exactly the same arithmetic as the add side.
        everyone = dict(pool_projections)
        everyone.update(projections)

        if matchup is None:
            # A bye, or the preseason. Not an error, and specifically not a
            # 0-0 tie -- see ``read.matchup_score``.
            notes.append("no matchup row this period, so the score cannot "
                         "waive the churn floor")

        used: Optional[Mapping[int, int]] = None
        try:
            used = lg.read.acquisitions_used()
        except Exception as exc:  # noqa: BLE001
            log.error("could not read the acquisition counter: %s", exc)
            notes.append("acquisition counter unavailable (%s)" % exc)

        # The lineup we are about to have in place, not the one ESPN had when
        # the run started. A player the optimizer just started may already be
        # tipping off, and a drop that empties a started slot cannot be undone.
        starters = [
            pid for pid, slot in plan.target.items()
            if ids.is_starting_slot(int(slot))
        ]

        accepted: List["addrop_mod.AddDropPlan"] = []
        # Advanced after each accepted swap, so swap two is priced against the
        # roster swap one leaves behind rather than against today's. Without
        # this the loop would propose the same best pickup repeatedly, or three
        # different pickups all paired with the same single weakest player.
        sim_roster = list(roster)
        sim_pool = list(pool)
        sim_used = dict(used) if used is not None else None
        # Players a blocked swap has retired from consideration for the rest of
        # the run. The verdict is prose, so there is no telling which half of
        # the pair it objected to -- retiring both is the conservative read, and
        # re-offering a pair that shares a half with a rejected one is asking
        # the reviewer the same question twice.
        #
        # Kept apart from ``never_drop`` rather than appended to it: these are
        # the app's own doing and are reported as such. Folding them in told a
        # user whose ``never_drop`` is empty that three of his players were in
        # it, which reads as a misconfiguration he then goes looking for.
        vetoed_names: List[str] = []
        vetoed_adds: set = set()
        # Every swap this run looked at and would not put in front of the
        # owner, with why. Kept so the question the empty approval queue
        # invites -- "so what did it consider?" -- has an answer that names
        # the two players rather than a line of prose in a run log.
        declined: List[Tuple["addrop_mod.AddDropPlan", str]] = []
        # Protected for the rest of the run, and reported as such rather than
        # as a ``never_drop`` entry they are not.
        just_added: List[int] = []

        wanted = max(1, int(self.cfg.addrop_max_moves))
        # Never below ``wanted``, so raising the option can never be silently
        # undone by this cap: a user who asks for five swaps and whose reviewer
        # approves all five gets five.
        budget = max(MAX_ADDROP_REVIEWS, wanted)
        attempts = 0
        # Set by every ``break`` below, so the note after the loop can tell
        # "the reviewer used up its look" from "the pool ran dry" -- the two
        # read identically from the outside and mean opposite things.
        ended_early = False

        while len(accepted) < wanted and attempts < budget:
            attempts += 1
            decision = addrop_mod.plan_addrop(
                settings,
                sim_roster,
                [e for e in sim_pool if int(e.player_id) not in vetoed_adds],
                values,
                everyone,
                scoring_period=settings.current_scoring_period,
                matchup_period=matchup_period,
                horizon=horizon,
                schedule=schedule,
                lam=plan.lam,
                starters=starters,
                matchup=matchup,
                acquisitions_used=sim_used,
                never_drop=self.cfg.never_drop,
                review_blocked=vetoed_names,
                just_added=just_added,
                margin_pct=self.cfg.addrop_margin_pct,
                churn_floor=self.cfg.addrop_churn_floor,
            )
            if attempts == 1 and decision.acquisitions_left is not None:
                # Once, from the real counter. Re-reporting it per attempt would
                # print a countdown of a budget nothing has spent yet.
                notes.append(
                    "%d acquisition(s) left in matchup period %d"
                    % (decision.acquisitions_left, matchup_period)
                )
            log.info("add/drop: %s", decision.describe())
            if not decision.proposed:
                # The notes of the attempt that found nothing are the ones that
                # explain where the list stopped, so they are always kept. The
                # notes of the attempts that *did* propose are kept below, with
                # the swap they belong to.
                notes.extend(decision.notes)
                ended_early = True
                break
            notes.extend(decision.notes)

            # How many swaps this one is priced on top of -- the accepted ones,
            # not the attempts. A blocked candidate is never applied to
            # ``sim_roster``, so a swap found after one rejection is still
            # priced against the real roster and must not claim otherwise. Using
            # the attempt number here told the user a card assumed a swap "above
            # it" that the reviewer had thrown away.
            rank = len(accepted)
            if rank:
                # On the card, not only in the record. The gain was computed
                # against a roster that only exists if the swaps above this one
                # are approved, and a user tapping this one in isolation is
                # entitled to know the number assumes company.
                decision = dataclasses.replace(
                    decision,
                    # "above it" in both cases: the pronoun is this card's own
                    # swap, which is singular however many are stacked on top
                    # of it. Only the verb agrees with the count.
                    rationale="%s\n\nPriced as the %s move of this run, "
                    "assuming the %s above it %s approved."
                    % (
                        decision.rationale,
                        _ordinal(rank + 1),
                        "swap" if rank == 1 else "%d swaps" % rank,
                        "is" if rank == 1 else "are",
                    ),
                )

            reviewed, review_notes, unreachable = self._review_addrop(
                lg, decision,
                roster=sim_roster, pool=sim_pool, matchup=matchup,
                injuries=injuries,
            )
            notes.extend(review_notes)
            if reviewed is None:
                # The verdict is the last note the review added, which is the
                # sentence written for a person. Recorded whether the reviewer
                # judged it or could not be reached: both ended the same way
                # for this swap, and only the words say which.
                declined.append(
                    (decision, review_notes[-1] if review_notes else
                     "the reviewer did not approve it"))
                if unreachable:
                    # Not a judgment about this swap, so the next one would be
                    # withheld for the same non-reason. Stop rather than spend
                    # the remaining attempts failing the same way.
                    notes.append(
                        "stopped looking for further swaps: the reviewer is "
                        "unreachable, so none of them could be shown either"
                    )
                    ended_early = True
                    break
                vetoed_adds.add(int(decision.add.player_id))
                if decision.drop is not None:
                    vetoed_names.append(decision.drop.name)
                continue

            accepted.append(reviewed)
            sim_roster, sim_pool = _apply_swap(sim_roster, sim_pool, reviewed)
            # A man this run just picked up is not a candidate to give away in
            # the same run. The arithmetic would normally never choose him -- he
            # was taken for being better than everyone -- but a thin pool and an
            # unlucky schedule can make the newest bench player the cheapest
            # drop, and "add him, then drop him" is two cards that cancel out
            # and one acquisition spent on nothing.
            just_added.append(int(reviewed.add.player_id))
            if sim_used is not None:
                sim_used[matchup_period] = sim_used.get(matchup_period, 0) + 1

        if not ended_early and len(accepted) < wanted:
            # Fell out of the loop having neither filled the order nor run out
            # of candidates, which leaves only one explanation: the reviewer
            # spent the run's looks refusing things. Said plainly, because the
            # alternative -- an empty list and silence -- is indistinguishable
            # from "the pool had nothing in it", and the two call for opposite
            # responses from the user.
            notes.append(
                "stopped after %d candidate(s): the reviewer blocked %d of "
                "them, and %d is the most one run will ask it about"
                % (attempts, attempts - len(accepted), budget)
            )

        return accepted, notes, declined

    @staticmethod
    def _addrop_payload(decision: Any, matchup_period: int) -> Dict[str, Any]:
        """Both ids *and* both names.

        The ids are what the write path needs; the names are what the approval
        card and the notification show, and re-reading them at approval time
        would mean a second ESPN round trip to answer a question we already
        knew the answer to when we decided. A swap that was declined is
        recorded in the same shape as one that was offered, so the page can
        render either with the same code.

        ``bid`` is always ``None``. A FAAB league never reaches here --
        ``plan_addrop`` declines rather than bidding zero -- so there is no bid
        to carry, and writing ``0`` would look like a deliberate minimum bid to
        anyone reading the row later.
        """
        return {
            "add": int(decision.add.player_id),
            "add_name": decision.add.name,
            "drop": int(decision.drop.player_id) if decision.drop else None,
            "drop_name": decision.drop.name if decision.drop else None,
            "bid": None,
            "waiver": bool(decision.add.waiver),
            "matchup_period": int(matchup_period),
            "reason": decision.reason,
        }

    def _record_declined(self, run_id: str, decision: Any, why: str,
                         matchup_period: int) -> None:
        """Journal a swap the app turned down. Never raises.

        Wrapped whole: this is a record of something that did *not* happen, and
        failing to write it must not cost the run anything that did. The
        decision has already been made and acted on by the time this is called.
        """
        try:
            self.store.propose(
                run_id, "ADD_DROP",
                self._addrop_payload(decision, matchup_period),
                rationale="%s\n\n%s" % (why, decision.rationale),
                value_delta=decision.gain,
                declined=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record the declined swap: %s", exc)

    def _review_addrop(
        self,
        lg: "League",
        decision: "addrop_mod.AddDropPlan",
        *,
        roster: Sequence[Any],
        pool: Sequence[Any],
        matchup: Optional[Any],
        injuries: Optional[Any],
    ) -> Tuple[Optional["addrop_mod.AddDropPlan"], List[str], bool]:
        """Show the swap to the reviewer before the user sees it.

        Returns the plan to go ahead with or ``None`` when it was blocked, the
        notes, and whether the block was *"could not be reached"* rather than a
        judgment. All three outcomes carry notes, because all three are things
        the user is entitled to read afterwards -- *"it should be able to show
        why it rejected a request to add drop"*. A block with no stated reason
        would be the same silence the points model was already criticised for.

        The third value exists because the caller offers several swaps a run and
        the two kinds of block mean opposite things for the next one: a judgment
        is about this pair and says nothing about the next, while an unreachable
        reviewer would withhold every remaining swap for the same non-reason.

        A plan that proposes nothing is returned untouched without a call. The
        reviewer exists to veto moves, and there is nothing to veto; asking it
        to confirm an absence would spend a request to be told what we knew.
        """
        if self.judge is None or not decision.proposed:
            return decision, [], False

        notes: List[str] = []
        reports = join_injuries(list(roster) + list(pool), injuries)
        verdict = self.judge.review(
            league_name=lg.name,
            keeper=bool(lg.ref.keeper),
            plan=decision,
            matchup=matchup,
            reports=reports,
            acquisitions_left=decision.acquisitions_left,
        )
        swap = decision.add.name
        if decision.drop is not None:
            swap = "%s for %s" % (decision.add.name, decision.drop.name)

        if not verdict.approve:
            notes.append("%s was not proposed -- %s" % (swap, verdict.describe()))
            if verdict.errored:
                # Distinguished in the log as well as in the note. A week of
                # outages and a week of sound judgment produce the same absence
                # of proposals, and only this line tells them apart.
                log.warning(
                    "add/drop review unavailable, so %s was withheld rather "
                    "than shown unexamined", swap,
                )
            return None, notes, bool(verdict.errored)

        notes.append("%s cleared review -- %s" % (swap, verdict.reason))
        if not verdict.concern:
            return decision, notes, False
        # A caveat raised on an approval goes on the card, not only into the
        # record: the card is where the decision is actually made, and a
        # reservation filed somewhere the user is not looking is not a
        # reservation. Appended to the computed rationale rather than replacing
        # it -- see the invariant on ``models.Move.rationale``.
        return (
            dataclasses.replace(
                decision,
                rationale="%s\n\nReviewer's caveat: %s"
                % (decision.rationale, verdict.concern),
            ),
            notes,
            False,
        )

    def plan_from_roster(
        self, lg: "League", settings: Any, roster: Sequence[Any]
    ) -> LineupWork:
        """Roster in, lineup out, with every input that produced it.

        Extracted from ``run_league`` when the post-acquisition rebalance
        needed the same chain. It deliberately does no writing, no notifying
        and no aborting -- it reads, computes, and hands back what it found,
        including the notes. What to *do* about an infeasible plan differs
        between the caller that is starting a run and the caller that has just
        made an irreversible move, and only they can know.

        The three slow reads behind this are all cached on the instance
        (``season_calendar``, ``pro_schedule``, ``injury_notes``), so calling it
        twice in one run costs one extra projections fetch rather than a second
        multi-megabyte season document.
        """
        notes: List[str] = []
        sources_used: Dict[str, str] = {}
        sources_missing: Dict[str, str] = {}

        cal, matchup_period, cal_notes = self._calendar_for(settings)
        notes.extend(cal_notes)
        schedule = self.pro_schedule()
        if not schedule:
            sources_missing["pro-schedule"] = "unavailable"

        injuries = self.injury_notes()
        overrides, injury_notes = self._availability_overrides(roster, injuries)
        notes.extend(injury_notes)
        if injuries is None:
            sources_missing["espn-injuries"] = "unavailable or stale"
            notes.append(
                "no injuries page this run: availability falls back to ESPN's "
                "fantasy status alone"
            )
        else:
            parse = injuries.payload.get("parse") or {}
            # The join count is here, not just the page counts, because it is
            # the only number that can tell a healthy quiet run from a broken
            # one. Observed on-device 2026-10-21: 80 notes parsed, 61 refined,
            # and exactly 2 of 24 rostered players on the page -- both already
            # OUT in ESPN's fantasy feed, so the run produced no notes at all.
            # Without "2 of 24" in the record, that silence looks identical to
            # the join matching nobody because the id spaces drifted apart.
            sources_used["espn-injuries"] = (
                "%d notes, %d refined by beat report, %d of %d rostered "
                "players on the page"
                % (
                    parse.get("notes", 0),
                    parse.get("refined_by_comment", 0),
                    len(overrides),
                    len(roster),
                )
            )

        splits = lg.read.player_projections([e.player_id for e in roster])
        projections, availability, skipped = projections_mod.build_projections(
            roster, splits, as_of=datetime.now(timezone.utc), statuses=overrides
        )
        if skipped:
            notes.append(
                "no projection for: %s" % ", ".join(e.name for e in skipped)
            )

        def work(plan: Optional["pipeline.LineupPlan"],
                 games_used: Optional[Any]) -> LineupWork:
            return LineupWork(
                plan=plan,
                projections=projections,
                availability=availability,
                skipped=skipped,
                overrides=overrides,
                injuries=injuries,
                cal=cal,
                matchup_period=matchup_period,
                schedule=schedule,
                games_used=games_used,
                notes=tuple(notes),
                sources_used=sources_used,
                sources_missing=sources_missing,
            )

        if not projections:
            sources_missing["espn-projection"] = "no players projected"
            return work(None, None)

        games_used = usage_mod.measure(
            lg.read,
            self.store,
            league_id=lg.ref.league_id,
            team_id=lg.ref.team_id,
            calendar=cal,
            scoring_period=settings.current_scoring_period,
            matchup_period=matchup_period,
        ) if settings.has_gp_cap else None
        if games_used is not None:
            log.info("games used: %s", games_used.describe())
            notes.append(games_used.describe())

        plan = pipeline.plan_lineup(
            settings,
            roster,
            projections,
            scoring_period=settings.current_scoring_period,
            matchup_period=matchup_period,
            schedule=schedule,
            calendar=cal,
            games_used=games_used,
            availability=availability,
        )
        notes.extend(plan.notes)
        return work(plan, games_used)

    def run_league(self, lg: "League") -> str:
        run_id = _ulid()
        # Before anything is journalled: the run is *about* a day, and ESPN's
        # own number for which day that is lags.
        settings, period_note = self.settings_now(lg)
        self.store.start_run(
            run_id,
            settings.current_scoring_period,
            self.cfg.shadow_mode,
            league_id=lg.ref.league_id,
        )
        log.info("=== run %s | %s | scoring period %d ===", run_id,
                 settings.name, settings.current_scoring_period)

        notes: List[str] = []
        if period_note:
            notes.append(period_note)
        sources_used: Dict[str, str] = {"espn": "ok"}
        sources_missing: Dict[str, str] = {}

        roster = lg.read.roster()
        log.info("roster: %d players, %d locked", len(roster),
                 sum(1 for p in roster if p.lineup_locked))

        work = self.plan_from_roster(lg, settings, roster)
        notes.extend(work.notes)
        sources_used.update(work.sources_used)
        sources_missing.update(work.sources_missing)
        cal, matchup_period = work.cal, work.matchup_period
        schedule, injuries = work.schedule, work.injuries
        projections = work.projections

        # Before the plan is examined, because the scoreboard is a fact about
        # the week rather than about this run's decisions, and the two paths
        # that abort below are exactly the ones where the owner most wants to
        # open the page and see where he stands.
        matchup = self._read_matchup(lg, matchup_period, notes)
        self._record_scoreboard(run_id, lg, settings, work, roster, matchup)

        if work.plan is None:
            notes.append("aborting: no usable projections for any player")
            self._finish(run_id, sources_used, sources_missing, {}, {}, notes)
            self._notify_problem(
                "Autopilot: no projections",
                "%s: ESPN returned no usable projections, so no lineup was "
                "computed. Yesterday's lineup is untouched." % lg.name,
            )
            return run_id
        plan = work.plan
        for line in pipeline.summarize(plan, roster).splitlines():
            log.info("  %s", line)

        coefficients = getattr(
            value_mod.build_value_model(settings), "coefficients", {}
        )
        # Keyed by the pseudo-slot ESPN hangs the pooled cap on, so the stored
        # dual is self-describing when it is read back out of the journal.
        shadow_prices = (
            {ids.TEAM_TOTAL_LIMIT_SLOT: plan.lam} if plan.cap else {}
        )

        if not plan.feasible:
            self._finish(run_id, sources_used, sources_missing, shadow_prices,
                         coefficients, notes)
            self._notify_problem(
                "Autopilot: lineup not applied",
                "%s: the computed lineup failed its own validation, so nothing "
                "was sent to ESPN. Reasons:\n%s"
                % (lg.name, "\n".join(n for n in plan.notes
                                      if n.startswith("invalid target"))),
            )
            return run_id

        if plan.changed:
            self._apply_lineup(run_id, lg, plan, roster, settings)
        else:
            log.info("lineup already optimal; nothing to do")
            notes.append("no change needed")

        # Deliberately after the lineup write. The lineup is the reversible,
        # time-critical half; add/drop is the permanent half and needs several
        # more ESPN reads. Doing it second means a pool fetch that hangs or
        # fails costs the run nothing it had already earned.
        decisions, addrop_notes, declined = self._consider_addrops(
            lg, settings, roster, projections, plan,
            cal=cal, matchup_period=matchup_period, schedule=schedule,
            injuries=injuries, matchup=matchup,
        )
        notes.extend(addrop_notes)
        for turned_down, why in declined:
            self._record_declined(run_id, turned_down, why, matchup_period)
        # Best first, each its own approval. Offered in the order they were
        # priced, because that is the order their estimates assume: the second
        # was computed as if the first went through. Nothing here waits for an
        # answer -- under ``autonomy_add_drop`` they all execute, and otherwise
        # they all sit in the queue and the user takes the ones he wants.
        for decision in decisions:
            self._offer_addrop(run_id, lg, decision, matchup_period,
                               scoring_period=settings.current_scoring_period)

        self._finish(run_id, sources_used, sources_missing, shadow_prices,
                     coefficients, notes)
        return run_id

    def _read_matchup(
        self, lg: "League", matchup_period: int, notes: List[str]
    ) -> Optional[Any]:
        """This week's score, or ``None`` with the reason in the notes.

        Wrapped because nothing downstream of it is worth a run: the score
        waives the churn floor in add/drop and fills a panel on the status
        page, and both degrade to "not shown" rather than to a failure.
        """
        try:
            return lg.read.matchup_score(matchup_period=matchup_period)
        except Exception as exc:  # noqa: BLE001
            log.error("could not read the matchup score: %s", exc)
            notes.append("matchup score unavailable (%s)" % exc)
            return None

    def _record_scoreboard(
        self,
        run_id: str,
        lg: "League",
        settings: Any,
        work: LineupWork,
        roster: Any,
        matchup: Optional[Any],
    ) -> None:
        """Cache the head-to-head for the status page. Never raises.

        The page reads the score from ESPN itself on every load, so this row is
        not where the number on screen normally comes from. It earns its keep
        two other ways:

        * it is the page's floor when that read fails -- an expired cookie costs
          the reader a stale score, labelled with its age, not the whole page;
        * it carries the two fields that are *not* on the score response and so
          could not be live even in principle -- ``games_cap``, a league setting
          fixed for the season, and ``my_games_today``, which depends on the
          lineup this run just set and therefore holds for the rest of the day.

        Wrapped whole and swallowing everything. A scoreboard is the least
        important thing this run does, and it is recorded *before* the lineup
        write -- so an exception escaping here would cost the run the one part
        of it that matters.
        """
        if matchup is None:
            return
        try:
            try:
                cap = budget_mod.matchup_cap(settings)
            except Exception:  # noqa: BLE001
                # A per-slot cap, which budget.py refuses to model. The score
                # is still worth showing; it just loses its denominator.
                cap = None
            self.store.record_scoreboard(
                lg.ref.league_id,
                run_id=run_id,
                matchup_period=int(matchup.matchup_period),
                scoring_period=int(settings.current_scoring_period),
                my_team_id=int(matchup.my_team_id),
                my_name=matchup.my_name,
                my_abbrev=matchup.my_abbrev,
                my_logo=matchup.my_logo,
                my_points=float(matchup.my_points),
                opp_team_id=matchup.opp_team_id,
                opp_name=matchup.opp_name,
                opp_abbrev=matchup.opp_abbrev,
                opp_logo=matchup.opp_logo,
                opp_points=float(matchup.opp_points),
                my_games_used=matchup.my_games_used,
                opp_games_used=matchup.opp_games_used,
                my_games_today=_starts_today(work, roster,
                                             settings.current_scoring_period),
                games_cap=None if cap is None else int(cap),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not cache the scoreboard: %s", exc)

    def _finish(
        self,
        run_id: str,
        sources_used: Mapping[str, str],
        sources_missing: Mapping[str, str],
        shadow_prices: Mapping[int, float],
        category_weights: Mapping[int, float],
        notes: List[str],
    ) -> None:
        self.store.finish_run(
            run_id,
            sources_used=sources_used,
            sources_missing=sources_missing,
            shadow_prices=shadow_prices,
            category_weights=category_weights,
            notes=notes,
        )

    def _apply_lineup(
        self,
        run_id: str,
        lg: "League",
        plan: "pipeline.LineupPlan",
        roster: Any,
        settings: Any,
    ) -> None:
        """Journal, execute, notify. Lineup and IR only -- never an acquisition.

        ``auto_approve=True`` is the autonomy decision, and it still writes a
        proposal row: autonomy changes who approves, never whether there is an
        audit trail. The claim is still taken, so a scheduled run and a tap can
        never both execute the same plan.

        ``shadow_mode`` is enforced inside ``EspnWrite``, not here. There is one
        place that decides whether a packet reaches ESPN, and it is the same
        place that builds the packet.
        """
        # ``target`` is what gets written; ``moves`` is what happened. The
        # status page needs the second and cannot derive it from the first,
        # because the difference is against the roster as it was, which is not
        # journaled. Slot names rather than ids so a row stays readable if the
        # slot numbering is ever remapped under it.
        payload = {
            "target": {str(k): int(v) for k, v in plan.target.items()},
            "scoring_period": int(settings.current_scoring_period),
            "moves": [
                {
                    "player_id": int(m.player_id),
                    "name": m.player_name,
                    "from": ids.slot_name(m.from_slot or -1),
                    "to": ids.slot_name(m.to_slot or -1),
                    "kind": m.kind,
                    "why": m.rationale,
                }
                for m in plan.moves
            ],
        }
        rationale = "; ".join(
            "%s %s->%s" % (m.player_name, ids.slot_name(m.from_slot or -1),
                           ids.slot_name(m.to_slot or -1))
            for m in plan.moves
        )
        proposal = self.store.propose(
            run_id, "LINEUP", payload, rationale=rationale,
            value_delta=sum(m.value_delta for m in plan.moves),
            auto_approve=True,
        )
        if not self.store.claim_for_execution(proposal.id):
            log.warning("lineup proposal %s already claimed", proposal.action_id)
            return
        try:
            result = lg.write.set_lineup(
                {int(k): int(v) for k, v in plan.target.items()},
                roster=roster,
                scoring_period=settings.current_scoring_period,
            )
            self.store.complete(proposal.id, bool(result and result.ok))
        except Exception as exc:  # noqa: BLE001 - one league must not kill the rest
            log.exception("lineup write failed for %s", lg.name)
            self.store.complete(proposal.id, False, str(exc))
            self._notify_problem("Autopilot: lineup write failed",
                                 "%s: %s" % (lg.name, str(exc)[:400]))
            return

        title = "%s: %d move(s)%s" % (
            lg.name, len(plan.moves), " [shadow]" if self.cfg.shadow_mode else "",
        )
        try:
            self.ha.notify(
                title=title,
                message=pipeline.summarize(plan, roster),
                tag="autopilot-lineup-%d" % lg.ref.league_id,
                actions=(),
                time_sensitive=False,
            )
        except Exception as exc:  # noqa: BLE001 - a failed push is not a failed run
            log.error("could not deliver lineup notification: %s", exc)

    def rebalance_after_swap(
        self, run_id: str, lg: "League", payload: Mapping[str, Any], result: Any
    ) -> None:
        """Re-read, re-optimize and apply the lineup now that the roster moved.

        A pickup that sits on the bench is worth nothing. The lineup was written
        earlier in the run against a roster that did not contain him, so without
        this the points the swap was approved *for* arrive a day late -- and in a
        week with three days left, a day late is a third of the gain. The
        requirement was plain: *"remember when i make an add, we have to
        potentially rebalance the lineup"*.

        Immediately after the transaction rather than on the next scheduled run,
        because the next run is up to 24 hours away and tonight's games are not.

        **Only when the roster actually changed.** Three things can make the
        write a no-op for this purpose, and all three would otherwise produce a
        confident log line about a rebalance that rebalanced nothing:

        * a waiver claim is ``PENDING`` -- the player is not ours yet, and the
          drop has not happened either, so the roster on ESPN is the one the
          lineup was already optimized against;
        * ``shadow_mode`` means the swap never left the process;
        * a write that failed.

        **It honours ``autonomy_lineup``.** That flag is the standing answer to
        "may this app move players without asking", and an add/drop approval is
        not a second answer to it: approving a swap says who should be on the
        roster, not who should be in the starting five. With it off the run says
        what it would have done and leaves the lineup alone.

        Never raises. It runs after an irreversible, already-journalled
        transaction, and a failure here must not make a completed swap look
        failed -- the swap happened, and the worst case is a bench spot for a
        night.
        """
        if not (result is not None and getattr(result, "ok", False)):
            return
        if getattr(result, "shadow", False):
            log.info("shadow mode: roster unchanged, so no rebalance")
            return
        if payload.get("waiver"):
            log.info(
                "waiver claim is pending, so the roster is unchanged and the "
                "lineup is already optimal for it; it will be rebalanced on the "
                "run after the claim processes"
            )
            return

        swap = payload.get("add_name") or "the acquisition"
        if not self.cfg.autonomy_lineup:
            log.info(
                "autonomy_lineup is off, so the lineup is left alone after %s; "
                "he will not be started until you move him or it is turned on",
                swap,
            )
            return

        try:
            settings, _ = self.settings_now(lg)
            roster = lg.read.roster()
            work = self.plan_from_roster(lg, settings, roster)
        except Exception as exc:  # noqa: BLE001 - the swap already succeeded
            log.exception("could not re-read %s after %s", lg.name, swap)
            self._notify_problem(
                "Autopilot: lineup not rebalanced",
                "%s: %s went through, but the roster could not be re-read, so "
                "the lineup still reflects the old one. %s"
                % (lg.name, swap, str(exc)[:200]),
            )
            return

        plan = work.plan
        if plan is None or not plan.feasible:
            # Deliberately loud. Everywhere else an unusable plan means
            # "yesterday's lineup stands", which is a safe default. Here the
            # roster underneath it just changed, so the lineup standing is one
            # that may start a player who is no longer on the team.
            log.warning("no usable lineup for %s after %s", lg.name, swap)
            self._notify_problem(
                "Autopilot: lineup not rebalanced",
                "%s: %s went through, but no valid lineup could be computed "
                "afterwards. Worth a look -- the lineup may still be set for "
                "the old roster." % (lg.name, swap),
            )
            return

        if not plan.changed:
            log.info("lineup still optimal after %s; nothing to do", swap)
            return

        log.info("rebalancing %s after %s: %d move(s)",
                 lg.name, swap, len(plan.moves))
        try:
            self._apply_lineup(run_id, lg, plan, roster, settings)
        except Exception as exc:  # noqa: BLE001 - see "never raises" above
            # ``_apply_lineup`` handles a refused ESPN write itself, so reaching
            # here means the journal or the store broke. Caught anyway because
            # one of this method's two callers is the approval loop, and an
            # exception escaping it would stop every *other* pending approval
            # from executing. A bench spot must not cost the queue.
            log.exception("could not rebalance %s after %s", lg.name, swap)
            self._notify_problem(
                "Autopilot: lineup not rebalanced",
                "%s: %s went through, but the new lineup could not be applied. "
                "%s" % (lg.name, swap, str(exc)[:200]),
            )

    def _offer_addrop(
        self,
        run_id: str,
        lg: "League",
        decision: "addrop_mod.AddDropPlan",
        matchup_period: int,
        *,
        scoring_period: Optional[int] = None,
    ) -> None:
        """Journal the swap and ask, or -- under autonomy -- journal and do it.

        The payload carries both ids *and* both names. The ids are what the
        write path needs; the names are what the approval card and the
        notification show, and re-reading them at approval time would mean a
        second ESPN round trip to answer a question we already knew the answer
        to when we decided.

        ``bid`` is always ``None``. A FAAB league never reaches here --
        ``plan_addrop`` declines rather than bidding zero -- so there is no bid
        to carry, and writing ``0`` would look like a deliberate minimum bid to
        anyone reading the row later.
        """
        payload = self._addrop_payload(decision, matchup_period)
        auto = bool(self.cfg.autonomy_add_drop)
        proposal = self.store.propose(
            run_id, "ADD_DROP", payload,
            rationale=decision.rationale,
            value_delta=decision.gain,
            auto_approve=auto,
        )

        # Both names, in that order, because a swap has two halves and a card
        # headed with only one of them is a prompt the user cannot answer. The
        # first version read "Dropping <the man coming in>", which named the
        # right player under the wrong verb.
        swap = decision.add.name
        if decision.drop is not None:
            swap = "%s for %s" % (decision.add.name, decision.drop.name)
        title = "%s: %s %s%s" % (
            lg.name,
            "add/drop" if auto else "approve add/drop?",
            swap,
            " [shadow]" if self.cfg.shadow_mode else "",
        )
        log.info("add/drop %s: %s", swap, decision.rationale)

        tag = bridge_mod.addrop_tag(lg.ref.league_id, proposal.action_id)

        if not auto:
            # The one class of move that asks. The buttons carry the verb; the
            # proposal id does not -- see ``ha.bridge.item_action_id``.
            try:
                self.ha.notify(
                    title=title,
                    message=decision.rationale,
                    tag=tag,
                    actions=(
                        Action(
                            action_id=bridge_mod.item_action_id(
                                proposal.action_id, bridge_mod.APPROVE
                            ),
                            title="Approve",
                        ),
                        Action(
                            action_id=bridge_mod.item_action_id(
                                proposal.action_id, bridge_mod.REJECT
                            ),
                            title="Reject",
                            destructive=True,
                        ),
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - the row is the record
                log.error("could not deliver add/drop notification: %s", exc)
                # Not fatal and not retried. The proposal is in the database and
                # the status page lists it, so the move is still approvable by
                # the surface that does not depend on push delivery.
            return

        # Autonomy. The claim is still taken, so a later tap on a stale
        # notification cannot execute the same swap twice.
        if not self.store.claim_for_execution(proposal.id):
            log.warning("add/drop proposal %s already claimed",
                        proposal.action_id)
            return
        try:
            result = lg.write.add_drop(
                add=payload["add"],
                drop=payload["drop"],
                bid=None,
                waiver=payload["waiver"],
                scoring_period=scoring_period,
            )
            self.store.complete(proposal.id, bool(result and result.ok))
        except Exception as exc:  # noqa: BLE001 - one league must not kill the rest
            log.exception("add/drop write failed for %s", lg.name)
            self.store.complete(proposal.id, False, str(exc))
            self._notify_problem("Autopilot: add/drop failed",
                                 "%s: %s" % (lg.name, str(exc)[:400]))
            return
        try:
            self.ha.notify(
                title=title,
                message=decision.rationale,
                tag=tag,
                actions=(),
                time_sensitive=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("could not deliver add/drop notification: %s", exc)

        # After the notification, not before. The swap is the news; the lineup
        # change that follows from it is a consequence, and the user should read
        # them in that order.
        self.rebalance_after_swap(run_id, lg, payload, result)

    # -- approval loop -----------------------------------------------------

    async def handle_action(self, ev: ActionEvent) -> None:
        """Apply a tap. Already authorized and shape-checked by the bridge.

        The verb is carried by the button, not by the proposal, so it must be
        stripped before any database lookup -- ``parsed.proposal_action_id`` is
        the id as ``state.py`` stores it. Passing the raw tapped id here matches
        no row and fails silently, which is the failure this whole module is
        built to prevent.
        """
        parsed = ev.parsed or bridge_mod.parse_action_id(ev.action_id)
        if parsed is None:
            log.warning("unrecognized action id %r", ev.action_id)
            return

        if parsed.is_bulk:
            for p in self.store.pending(parsed.run_id):
                self.store.decide(p.action_id, parsed.approve, ev.user_id or "?")
            log.info("%s for run %s", parsed.verb, parsed.run_id)
        else:
            decided = self.store.decide(
                parsed.proposal_action_id, parsed.approve, ev.user_id or "?"
            )
            if decided is None:
                # Already decided, expired, or never existed. A stale tap on an
                # old notification lands here and correctly does nothing.
                log.info("tap on %r changed nothing", parsed.proposal_action_id)
                return
        await self.execute_approved()

    async def execute_approved(self) -> None:
        for p in self.store.approved():
            if not self.store.claim_for_execution(p.id):
                # Lost the race to another tap or the scheduler. Correct
                # behaviour is to do nothing at all.
                log.info("proposal %s already claimed", p.action_id)
                continue
            try:
                # Inside the try: this reads the store, and an exception
                # escaping here would abandon every *other* approved proposal
                # in the queue. The `continue` below means it is always bound
                # by the time anything downstream reads it.
                lg = self.league_for_run(p.run_id)
                result = self._execute(p)
                self.store.complete(p.id, bool(result and result.ok))
            except Exception as exc:  # noqa: BLE001 - must not kill the loop
                log.exception("executing %s failed", p.action_id)
                self.store.complete(p.id, False, str(exc))
                self._notify_problem("Autopilot: move failed", str(exc)[:400])
                continue

            # Take the card down; send nothing in its place. The user decided
            # this himself a second ago and does not need telling -- but the
            # card must stop offering Approve and Reject for a swap that has
            # already happened, or a later tap on it reads as a no-op bug. What
            # he does still hear about is the lineup change below, if there is
            # one, because that part he did not do.
            if lg is not None and p.kind in ("ADD", "DROP", "ADD_DROP"):
                self.ha.dismiss(
                    bridge_mod.addrop_tag(lg.ref.league_id, p.action_id)
                )

            # The approved swap is on the roster; the lineup still is not. This
            # is the tap path, so it may be hours after the run that proposed
            # it -- which is exactly why the rebalance cannot wait for the next
            # scheduled one. Journalled under the original run, because that is
            # the decision this follows from.
            if p.kind in ("ADD", "DROP", "ADD_DROP") and lg is not None:
                self.rebalance_after_swap(
                    p.run_id, lg, p.payload or {}, result
                )

    def _execute(self, proposal: Any) -> Any:
        lg = self.league_for_run(proposal.run_id)
        if lg is None:
            raise ValueError(
                "cannot route %s to a league; not executing" % proposal.action_id
            )
        kind = proposal.kind
        payload = proposal.payload
        # Recomputed rather than read off the payload. This is the tap path, so
        # it can be hours -- or a night's sleep -- after the run that journalled
        # the proposal, and the period that was current then is exactly the one
        # ESPN now refuses.
        period = self.settings_now(lg)[0].current_scoring_period
        if kind == "LINEUP":
            return lg.write.set_lineup(
                {int(k): int(v) for k, v in payload["target"].items()},
                scoring_period=period,
            )
        if kind in ("ADD", "DROP", "ADD_DROP"):
            return lg.write.add_drop(
                add=payload.get("add"),
                drop=payload.get("drop"),
                bid=payload.get("bid"),
                waiver=bool(payload.get("waiver", True)),
                scoring_period=period,
            )
        raise ValueError("unknown proposal kind %r" % kind)

    # -- daemon ------------------------------------------------------------

    async def scheduled_run(self) -> None:
        """The daily run, as the scheduler invokes it.

        **This must stay a coroutine function.** ``AsyncIOScheduler`` only
        awaits the job on the event loop when it *is* one; a plain function is
        handed to a worker thread instead, where there is no running loop. The
        first version of this was a ``lambda`` calling ``asyncio.create_task``,
        which meant every scheduled run died with ``RuntimeError: no running
        event loop`` -- the job was dispatched to a thread, so the one thing it
        did needed the loop it no longer had. Nothing caught it in testing
        because the failure needs a real fire to happen at all.

        ``run_once`` is blocking (ESPN reads, SQLite, notifications), so it goes
        to a thread deliberately -- just via ``await``, from inside the loop,
        rather than from a thread that is pretending to be in one.
        """
        await asyncio.to_thread(self.run_once)

    async def _serve_web(self) -> None:
        """The ingress status page, isolated so its failure is survivable.

        A crash here must not take the daemon down with it. The page is how you
        *see* proposals; the notification is how you get told about them, and
        losing the former while keeping the latter is a degraded app rather than
        a dead one. So the exception is logged and the task ends quietly.
        """
        try:
            import web

            await web.serve(self, self.cfg.ingress_port)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception(
                "the ingress status page stopped; scheduled runs and "
                "notification approvals are unaffected"
            )

    async def serve(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        queue: "asyncio.Queue[ActionEvent]" = asyncio.Queue()
        sched = AsyncIOScheduler(timezone=self.tz)

        for t in self.cfg.run_times:
            hour, minute = t.split(":")
            sched.add_job(
                self.scheduled_run,
                CronTrigger(hour=int(hour), minute=int(minute), timezone=self.tz),
                name="run@%s" % t,
                # The default grace is one second. On a job that fires once a
                # day, a loop that happens to be busy for a second at 09:00
                # would discard the entire day's run as a misfire. An hour is
                # still far short of the next fire, so this cannot double up.
                misfire_grace_time=3600,
            )
            log.info("scheduled daily run at %s %s", t, self.cfg.timezone)

        # Expiry sweep. Approval must not carry over, and a proposal whose
        # notification was never tapped has to die on its own.
        sched.add_job(
            lambda: self.store.expire_stale(),
            CronTrigger(minute="*/15", timezone=self.tz),
            name="expire-sweep",
        )
        sched.start()

        listener = asyncio.create_task(self.ha.listen_actions(queue, stop=stop))
        # The review surface. Deliberately a sibling task rather than a
        # precondition: if uvicorn cannot bind, the scheduler and the
        # notification path must keep working, because they are what make the
        # app useful and the page is only the nicer way to answer it.
        web_task = asyncio.create_task(self._serve_web())
        try:
            while not stop.is_set():
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                await self.handle_action(ev)
        finally:
            listener.cancel()
            web_task.cancel()
            sched.shutdown(wait=False)
            self.store.close()
            self.http.close()
            log.info("stopped")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="nba-autopilot")
    ap.add_argument("--discover", action="store_true",
                    help="print league facts and exit")
    ap.add_argument("--once", action="store_true", help="one run, then exit")
    args = ap.parse_args(argv)

    try:
        cfg = appconfig.load()
    except appconfig.ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2
    appconfig.setup_logging(cfg.log_level)

    bot = Autopilot(cfg)

    if args.discover:
        failures = 0
        for lg in bot.leagues:
            log.info("")
            log.info("################ league %d (team %d) ################",
                     lg.ref.league_id, lg.ref.team_id)
            try:
                report = discovery.report(lg.read)
            except EspnError as exc:
                log.error("discovery failed for league %d: %s",
                          lg.ref.league_id, exc)
                failures += 1
                continue
            log.info("discovery complete; %d facts collected", len(report))
        return 1 if failures else 0

    if not bot.startup_checks():
        log.error("startup checks failed; not scheduling runs")
        return 1

    if args.once:
        bot.run_once()
        return 0

    asyncio.run(bot.serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
