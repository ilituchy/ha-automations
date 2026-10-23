"""ESPN Fantasy v3 read client. Never mutates anything.

The read and write surfaces live on different hosts, and this module physically
cannot write: it only knows ``READ_HOST`` and only issues ``GET``. That
separation is the cheapest possible guarantee that a bug in parsing cannot
become a bug in your roster.

Everything is returned as the frozen types from ``app/types.py``. Parsing is
deliberately strict about one thing: it never invents a default for a value that
changes a decision. A missing GP cap is ``None``, not ``{}``. A missing
projection is an absent key, not ``0.0``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

import httpx

from espn import ids
from models import (
    GameRef,
    LeagueSettings,
    MatchupScore,
    MatchupWindow,
    RosterEntry,
    StatLimit,
)

log = logging.getLogger(__name__)

PlayerId = int
SlotId = int
StatId = int


class EspnError(RuntimeError):
    pass


class EspnAuthError(EspnError):
    """401/403 -- almost always an expired ``espn_s2``.

    Distinct from a transport error on purpose: cookie expiry is the single most
    likely ongoing failure of this app, and it needs a *different* notification
    ("re-paste your cookie") than a network blip ("will retry").
    """


@dataclass(frozen=True)
class EspnConfig:
    league_id: int
    team_id: int
    espn_s2: str
    swid: str
    season: int
    #: Ours, not ESPN's. Carried on the client config so that
    #: :meth:`EspnRead.league_settings` can stamp it onto the settings object
    #: the solver reads, which keeps every consumer asking one question --
    #: "is this a keeper league?" -- of one place. Defaulted to False because
    #: redraft is the assumption that makes the *reversible* mistake: treating
    #: a keeper league as redraft proposes a drop the user can reject, while
    #: the converse silently never proposes one at all.
    keeper: bool = False

    def cookies(self) -> Dict[str, str]:
        """Read requests send both cookies; writes send only ``espn_s2``.

        ``SWID`` behaves as a label rather than a credential on the write path,
        so ``espn/write.py`` omits it -- one fewer secret on the wire, and it
        costs nothing because writes are authorized by ``espn_s2`` alone.
        """
        return {"espn_s2": self.espn_s2, "SWID": self.swid}


def make_client(cfg: EspnConfig, *, timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        cookies=cfg.cookies(),
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        },
        follow_redirects=False,
    )


class EspnRead:
    def __init__(self, cfg: EspnConfig, http: httpx.Client) -> None:
        self.cfg = cfg
        self.http = http
        self._cache: Dict[str, Any] = {}

    # -- transport ---------------------------------------------------------

    @property
    def _league_url(self) -> str:
        return "%s/apis/v3/games/%s/seasons/%d/segments/0/leagues/%d" % (
            ids.READ_HOST,
            ids.GAME,
            self.cfg.season,
            self.cfg.league_id,
        )

    def _get(
        self,
        url: str,
        views: Iterable[str] = (),
        *,
        fantasy_filter: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        headers: Dict[str, str] = {}
        if fantasy_filter is not None:
            import json

            headers["X-Fantasy-Filter"] = json.dumps(fantasy_filter)
        query: List[Tuple[str, str]] = [("view", v) for v in views]
        for k, v in (params or {}).items():
            query.append((k, str(v)))
        r = self.http.get(url, params=query, headers=headers)
        if r.status_code in (401, 403):
            raise EspnAuthError(
                "ESPN returned %d -- espn_s2 is probably expired. "
                "Re-copy the cookie and update the app options."
                % r.status_code
            )
        if r.status_code == 404:
            raise EspnError(
                "ESPN returned 404 for league %d season %d. Check league_id "
                "and that the league exists this season."
                % (self.cfg.league_id, self.cfg.season)
            )
        if r.status_code >= 400:
            raise EspnError("ESPN %d: %s" % (r.status_code, r.text[:300]))
        # 302 to the login page is how ESPN reports a dead cookie on some paths.
        if r.status_code in (301, 302, 307, 308):
            raise EspnAuthError("ESPN redirected to %s -- cookie likely dead"
                                % r.headers.get("location", "?"))
        return r.json()

    def league(self, *views: str, refresh: bool = False) -> Mapping[str, Any]:
        key = "|".join(sorted(views))
        if refresh or key not in self._cache:
            self._cache[key] = self._get(self._league_url, views)
        return self._cache[key]

    # -- settings ----------------------------------------------------------

    def league_settings(self) -> LeagueSettings:
        raw = self.league("mSettings", "mStatus", "mNav")
        s = raw.get("settings") or {}
        roster = s.get("rosterSettings") or {}
        scoring = s.get("scoringSettings") or {}
        acq = s.get("acquisitionSettings") or {}
        status = raw.get("status") or {}

        slot_counts = {
            int(k): int(v)
            for k, v in (roster.get("lineupSlotCounts") or {}).items()
            if int(v) > 0
        }

        # The GP cap. ESPN omits the key entirely in uncapped leagues, and may
        # also send an all-zero map, which means the same thing. Both collapse
        # to None -- see the note in solve/budget.remaining_budget.
        #
        # The value is a nested object, not an int:
        #     {"15": {"limitValue": 5.714285714285714, "statId": 42}}
        # Reading it as an int raised TypeError on the very first live league.
        # Unparseable entries are skipped rather than guessed at: a wrong cap
        # silently mis-plans every lineup, whereas a dropped one at least makes
        # `has_gp_cap` false and takes the budget logic out of the loop.
        raw_limits = roster.get("lineupSlotStatLimits")
        limits: Optional[Dict[SlotId, StatLimit]] = None
        if raw_limits:
            parsed: Dict[SlotId, StatLimit] = {}
            for k, v in raw_limits.items():
                if not isinstance(v, Mapping):
                    log.warning(
                        "lineupSlotStatLimits[%s] is %s, not an object; "
                        "ignoring this cap", k, type(v).__name__
                    )
                    continue
                try:
                    slot, limit, stat = int(k), float(v["limitValue"]), int(v["statId"])
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning(
                        "lineupSlotStatLimits[%s] unparseable (%s): %r", k, exc, v
                    )
                    continue
                if limit > 0:
                    parsed[slot] = StatLimit(slot_id=slot, stat_id=stat,
                                             limit_value=limit)
            limits = parsed or None

        scoring_items: Dict[StatId, float] = {}
        categories: Set[StatId] = set()
        for item in scoring.get("scoringItems") or []:
            stat = int(item.get("statId"))
            if item.get("isReverseItem"):
                pass  # sign handled via ids.LOWER_IS_BETTER, kept consistent
            points = item.get("points")
            if points is not None:
                scoring_items[stat] = float(points)
            categories.add(stat)

        # The acquisition cap, in the same fractional per-scoring-period units
        # ESPN uses for the games-played cap. Anything non-positive collapses to
        # None, i.e. *no cap*: ESPN spells "unlimited" as `-1` on the
        # season-long `acquisitionLimit` sibling (both live leagues), and a
        # limit of zero would describe a league where nobody may ever add
        # anyone. Neither may be allowed to read as "a cap of zero" -- see the
        # note on lineup_slot_stat_limits for why that distinction is load-
        # bearing rather than pedantic.
        acq_limit = _opt_float(acq.get("matchupAcquisitionLimit"))
        if acq_limit is not None and acq_limit <= 0:
            acq_limit = None

        scoring_type = str(s.get("scoringSettings", {}).get("scoringType")
                           or s.get("scoringType") or "UNKNOWN")
        # In a categories league the scoringItems *are* the category list; in a
        # points league they carry point values. Only treat them as categories
        # when ESPN says the league is category-scored.
        if not scoring_type.upper().startswith("H2H_CATEGORY"):
            categories = set()

        return LeagueSettings(
            league_id=self.cfg.league_id,
            season_id=self.cfg.season,
            name=str(s.get("name") or "league %d" % self.cfg.league_id),
            scoring_type=scoring_type,
            lineup_slot_counts=slot_counts,
            lineup_slot_stat_limits=limits,
            scoring_items=scoring_items,
            categories=frozenset(categories),
            # `acquisitionType` is the mode. `waiverProcessDays` is a *list* of
            # weekday names, so reading it first produced the string
            # "['SUNDAY']" as the waiver mode on both live leagues.
            waiver_mode=str(acq.get("acquisitionType") or "UNKNOWN"),
            # Honour `isUsingAcquisitionBudget` over `acquisitionBudget`. Both
            # live leagues run traditional waiver priority and still report a
            # budget of 100; taking that at face value would let the app bid
            # FAAB in a league where FAAB does not exist.
            faab_budget=(
                _opt_int(acq.get("acquisitionBudget"))
                if acq.get("isUsingAcquisitionBudget")
                else None
            ),
            # ESPN spells it `lineupLocktimeType` -- lowercase `t` in `time`.
            # The old `lineupLockTimeType` matched nothing, so every league
            # reported its lock mode as UNKNOWN. Both live leagues are
            # INDIVIDUAL_GAME, the strictest mode there is: each player locks
            # at their own tip-off rather than at the day's first game.
            roster_lock_mode=str(roster.get("lineupLocktimeType") or "UNKNOWN"),
            # Verbatim, in both fields. This is the latest period ESPN has
            # *results* for, which is not the same as the period it is
            # currently in -- `Autopilot.today_period` corrects the first of
            # these two and leaves the second alone.
            current_scoring_period=int(status.get("latestScoringPeriod") or 0),
            espn_latest_scoring_period=int(status.get("latestScoringPeriod") or 0),
            current_matchup_period=int(status.get("currentMatchupPeriod") or 0),
            matchup_period_boundaries=_matchup_windows(s),
            acquisition_limit=acq_limit,
            acquisition_limit_per_scoring_period=bool(
                acq.get("matchupLimitPerScoringPeriod")
            ),
            # Not from ESPN. See the note on LeagueSettings.keeper.
            keeper=bool(self.cfg.keeper),
        )

    # -- roster ------------------------------------------------------------

    def roster(
        self, team_id: Optional[int] = None, scoring_period: Optional[int] = None
    ) -> List[RosterEntry]:
        """Players actually on the team right now.

        **A dropped player lingers in ``entries``.** Observed live: after a
        successful ``DROP``, the entry stays in the array still carrying its
        old ``lineupSlotId``, and only ``playerPoolEntry.status`` (``WAIVERS``)
        and ``onTeamId`` (``0``) say otherwise. So ``len(entries)`` is not a
        roster count, and taking it as one is not a cosmetic error: the
        optimizer would assign a lineup slot to someone who is not on the
        team -- which ESPN rejects, losing the whole day's lineup -- and the
        exact-capacity matching in ``pipeline.py`` would fit 13 players into
        13 slots when one of them is a ghost.
        """
        team_id = self.cfg.team_id if team_id is None else team_id
        params = {"scoringPeriodId": scoring_period} if scoring_period else None
        raw = self._get(self._league_url, ("mRoster", "mTeam"), params=params)
        for team in raw.get("teams") or []:
            if int(team.get("id")) != int(team_id):
                continue
            entries = (team.get("roster") or {}).get("entries") or []
            kept, dropped = [], []
            for e in entries:
                if _is_on_team(e, int(team_id)):
                    kept.append(_parse_entry(e))
                else:
                    dropped.append(e)
            if dropped:
                # Worth a line each: this is rare, and when it happens it
                # explains a roster that is one player short of its slots.
                for e in dropped:
                    ppe = e.get("playerPoolEntry") or {}
                    log.info(
                        "excluding player %s from team %d's roster: status=%r "
                        "onTeamId=%r (dropped, awaiting waiver processing)",
                        e.get("playerId"), team_id, ppe.get("status"),
                        ppe.get("onTeamId"),
                    )
            return kept
        raise EspnError("team %d not found in league %d" % (team_id, self.cfg.league_id))

    def my_team_ids(self) -> Set[int]:
        """Teams whose ``owners`` include our SWID.

        Every write checks membership here. The reason is specific: ESPN honours
        a commissioner's edit to *another* team's lineup with ``200 EXECUTED``,
        so a ``team_id`` typo would not error -- it would silently rewrite a
        leaguemate's roster and post it to the public activity feed under the
        user's name.
        """
        raw = self.league("mTeam")
        swid = self.cfg.swid.strip()
        swid_bare = swid.strip("{}").upper()
        out: Set[int] = set()
        for team in raw.get("teams") or []:
            owners = team.get("owners") or []
            for owner in owners:
                if str(owner).strip("{}").upper() == swid_bare:
                    out.add(int(team["id"]))
        return out

    def free_agents(self, limit: int = 200) -> List[RosterEntry]:
        """Available players, best-first by ESPN's own ranking.

        ``X-Fantasy-Filter`` is mandatory here: without it ESPN returns the
        first N players in internal order, which is not useful, and with a large
        N it returns a multi-megabyte payload.
        """
        flt = {
            "players": {
                "filterStatus": {
                    "value": ["FREEAGENT", "WAIVERS"]
                },
                "limit": int(limit),
                "offset": 0,
                "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
                "filterStatsForTopScoringPeriodIds": {
                    "value": 5,
                    "additionalValue": [
                        "00%d" % self.cfg.season,
                        "10%d" % self.cfg.season,
                    ],
                },
            }
        }
        raw = self._get(self._league_url, ("kona_player_info",), fantasy_filter=flt)
        return [_parse_entry(p) for p in (raw.get("players") or [])]

    def player_in_pool(self, player_id: PlayerId) -> Optional[RosterEntry]:
        """One player from the league pool, for his ``acquisition_status``.

        ``free_agents()`` is sorted and truncated, so a specific target may not
        be in it; this asks about him directly. Returns ``None`` when ESPN has
        no row for the id at all, which is the honest answer to give a caller
        about to try acquiring him.
        """
        flt = {"players": {"filterIds": {"value": [int(player_id)]}}}
        raw = self._get(self._league_url, ("kona_player_info",), fantasy_filter=flt)
        for p in raw.get("players") or []:
            return _parse_entry(p)
        return None

    # -- cap accounting ----------------------------------------------------

    def games_used(
        self, team_id: Optional[int] = None, matchup_period: Optional[int] = None
    ) -> Dict[SlotId, int]:
        """Games consumed against the cap this matchup period, by slot key.

        Two different numbers live in two different places and they are not
        interchangeable:

        * ``teams[].valuesByStat["42"]`` is **season-to-date** games played.
        * ``cumulativeScore.statBySlot`` under ``mMatchupScore`` is **this
          matchup period** only.

        A season-long cap needs the former; a weekly cap needs the latter. We
        read the matchup-scoped figure here and fall back to the season figure,
        because getting this backwards means either hoarding a budget you have
        already spent or spending one you have not.

        **The per-stat field is ``value``, not ``score``.** This method read
        ``score`` from its first line until 2026-10-21, so every lookup missed
        and the method returned ``{}`` on every call -- which ``usage.measure``
        correctly reported as "statBySlot was empty" and then quietly worked
        around via the season delta. The real shape, live:

            {"15": {"statId": 42, "value": 2.0,
                    "limitExceeded": false, "exceededOnScoringPeriod": 0}}

        That bug was invisible because we are in matchup period 1, where
        season-to-date and matchup-to-date are the same number -- both readings
        said 1 and 4 for the two leagues. It stops being invisible on the first
        day of matchup period 2, when the fallback would start counting last
        week's games against this week's cap.

        **Filtered to the current matchup period**, because ``schedule``
        carries the whole season and summing it would add up every week the team
        has played. ``matchup_period`` defaults to ESPN's own
        ``currentMatchupPeriod``; pass it explicitly to ask about another week.
        Rows ESPN has not scored yet contribute nothing rather than zero, which
        is the same distinction ``usage.starts_from_stat_by_slot`` preserves.
        """
        team_id = self.cfg.team_id if team_id is None else team_id
        raw = self._get(self._league_url, ("mMatchupScore", "mTeam", "mStatus"))
        if matchup_period is None:
            matchup_period = _opt_int(
                (raw.get("status") or {}).get("currentMatchupPeriod")
            )
        out: Dict[SlotId, int] = {}
        for match in raw.get("schedule") or []:
            if matchup_period is not None and _opt_int(
                match.get("matchupPeriodId")
            ) != int(matchup_period):
                continue
            for side in ("home", "away"):
                entry = match.get(side) or {}
                if int(entry.get("teamId", -1)) != int(team_id):
                    continue
                by_slot = (entry.get("cumulativeScore") or {}).get("statBySlot") or {}
                for slot, stats in by_slot.items():
                    val = (stats or {}).get("value")
                    if val is None:
                        # Pre-2026-10-21 this method read `score`, so accept it
                        # as a fallback rather than assume the probe caught
                        # every shape ESPN serves.
                        val = (stats or {}).get("score")
                    if val is None:
                        continue
                    out[int(slot)] = out.get(int(slot), 0) + int(val)
        return out

    def matchup_score(
        self, team_id: Optional[int] = None, matchup_period: Optional[int] = None
    ) -> Optional[MatchupScore]:
        """This week's score so far: ours, theirs, and who they are.

        ESPN sends **no projected total** for basketball -- there is no
        ``totalProjectedPointsLive`` on either side of a live row, verified on
        both leagues -- so a projected *final* has to be built by projecting the
        opponent's roster the same way ours is. What this returns is the actual
        accumulated score, which is the honest half of that.

        ``totalPoints`` and ``sum(pointsByScoringPeriod)`` agreed exactly on
        every live row, so the former is used and the latter is kept as the
        fallback for a row ESPN has not totalled yet.

        Returns ``None`` when no row for this team exists in the period, which
        happens on a bye and during the preseason. A missing matchup is not an
        error and must not read as a 0-0 tie.
        """
        team_id = self.cfg.team_id if team_id is None else int(team_id)
        raw = self._get(self._league_url, ("mMatchupScore", "mTeam", "mStatus"))
        if matchup_period is None:
            matchup_period = _opt_int(
                (raw.get("status") or {}).get("currentMatchupPeriod")
            )
        names = {
            int(t["id"]): str(t.get("name") or t.get("abbrev") or t["id"]).strip()
            for t in raw.get("teams") or []
            if t.get("id") is not None
        }

        def total(side: Mapping[str, Any]) -> float:
            pts = side.get("totalPoints")
            if pts is not None:
                return float(pts)
            by_period = side.get("pointsByScoringPeriod") or {}
            return sum(float(v or 0.0) for v in by_period.values())

        for match in raw.get("schedule") or []:
            if matchup_period is not None and _opt_int(
                match.get("matchupPeriodId")
            ) != int(matchup_period):
                continue
            sides = {s: (match.get(s) or {}) for s in ("home", "away")}
            mine = next(
                (v for v in sides.values()
                 if _opt_int(v.get("teamId")) == team_id), None,
            )
            if mine is None:
                continue
            theirs = next(
                (v for v in sides.values()
                 if _opt_int(v.get("teamId")) != team_id), None,
            )
            opp_id = _opt_int((theirs or {}).get("teamId"))
            return MatchupScore(
                matchup_period=int(matchup_period or 0),
                my_team_id=team_id,
                my_points=total(mine),
                opp_team_id=opp_id,
                opp_name=names.get(opp_id or -1, "" if opp_id is None else str(opp_id)),
                opp_points=0.0 if theirs is None else total(theirs),
            )
        return None

    def acquisitions_used(self, team_id: Optional[int] = None) -> Dict[int, int]:
        """``matchup_period -> acquisitions already made``, for the cap.

        From ``transactionCounter.matchupAcquisitionTotals``, which is the only
        per-matchup-period acquisition count ESPN publishes -- the sibling
        ``acquisitions`` field is season-to-date and the transaction log carries
        a ``scoringPeriodId`` with ``matchupPeriodId: null``, so neither answers
        "how many have we used this week". Observed 2026-10-21: ``{"1": 1}`` in
        the league where one add had been made, ``{}`` in the one where none
        had.

        An absent key means zero, so the caller gets a plain ``dict.get``. That
        is safe here in a way it is not for the games-played cap: the counter is
        maintained by ESPN for every team unconditionally, so an empty mapping
        is "no acquisitions yet" rather than "no data".
        """
        team_id = self.cfg.team_id if team_id is None else int(team_id)
        raw = self.league("mTeam")
        for team in raw.get("teams") or []:
            if int(team.get("id")) != team_id:
                continue
            totals = (team.get("transactionCounter") or {}).get(
                "matchupAcquisitionTotals"
            ) or {}
            out: Dict[int, int] = {}
            for period, count in totals.items():
                p, c = _opt_int(period), _opt_int(count)
                if p is not None and c is not None:
                    out[p] = c
            return out
        return {}

    def season_games_played(self, team_id: Optional[int] = None) -> Optional[int]:
        team_id = self.cfg.team_id if team_id is None else team_id
        raw = self.league("mTeam")
        for team in raw.get("teams") or []:
            if int(team.get("id")) == int(team_id):
                v = (team.get("valuesByStat") or {}).get(str(ids.GP_STAT))
                return None if v is None else int(v)
        return None

    # -- schedule ----------------------------------------------------------

    def scoring_period_dates(self) -> Dict[int, date]:
        """``scoringPeriodId -> US game date``, from ESPN's own period keys.

        ESPN's ``proGamesByScoringPeriod`` key is the authority on which period
        a game belongs to; this method only has to recover the *date* of each
        period, and the one thing that matters is the timezone it converts in.

        A tipoff is a UTC instant, and a 19:30 Pacific game is already past
        midnight UTC. Taking the UTC date therefore pushes every west-coast
        late game onto the following day. Probed across all 156 game days of
        the 2026-27 season: converting in **US Eastern** yields exactly one
        date per scoring period, with no period spanning two. Converting in UTC
        splits 30-odd of them.

        The predecessor of this method inverted the mapping to
        ``date -> period`` and resolved collisions with ``setdefault``, so the
        UTC spillover meant the winner depended on the iteration order of
        ESPN's team list -- a nondeterministic calendar, which is a bad thing
        for something that decides which day a lineup is written to.
        """
        url = "%s/apis/v3/games/%s/seasons/%d" % (
            ids.READ_HOST,
            ids.GAME,
            self.cfg.season,
        )
        raw = self._get(url, ("proTeamSchedules_wl",))
        seen: Dict[int, Set[date]] = {}
        for team in (raw.get("settings") or {}).get("proTeams") or []:
            for period, games in ((team.get("proGamesByScoringPeriod")) or {}).items():
                for g in games or []:
                    ts = (g or {}).get("date")
                    if ts is None:
                        continue
                    when = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc)
                    seen.setdefault(int(period), set()).add(
                        when.astimezone(ids.GAME_DATE_TZ).date()
                    )

        out: Dict[int, date] = {}
        for period, days in seen.items():
            if len(days) > 1:
                # Would mean the Eastern-date finding above has stopped holding.
                # Take the earliest and say so loudly rather than pick silently.
                log.error(
                    "scoring period %d spans %d US dates (%s); the calendar "
                    "model assumes one. Taking the earliest.",
                    period, len(days), sorted(days),
                )
            out[period] = min(days)
        return out

    def pro_team_scoring_periods(self) -> Dict[int, Dict[int, int]]:
        """``proTeamId -> {scoringPeriodId: games that period}``.

        The horizon the games-played budget is spent over, and the only form of
        the NBA schedule the optimizer consults, for two reasons:

        * ``proGamesByScoringPeriod`` is **already keyed by scoring period**, so
          no date arithmetic is involved and the All-Star break and season edges
          cannot introduce an off-by-one.
        * the keys are fantasy ``proTeamId``, the same id space as
          ``RosterEntry.pro_team_id``. Joining through the site API's team ids or
          through abbreviations would be a second mapping to keep correct.

        The predecessor of this method keyed by a date derived from the game's
        UTC timestamp, which is a day late for every west-coast evening tipoff.
        Dates are now only produced by :meth:`scoring_period_dates`, in one
        place, in one timezone.
        """
        url = "%s/apis/v3/games/%s/seasons/%d" % (
            ids.READ_HOST,
            ids.GAME,
            self.cfg.season,
        )
        raw = self._get(url, ("proTeamSchedules_wl",))
        out: Dict[int, Dict[int, int]] = {}
        for team in (raw.get("settings") or {}).get("proTeams") or []:
            tid = int(team.get("id"))
            by_period: Dict[int, int] = {}
            for period, games in ((team.get("proGamesByScoringPeriod")) or {}).items():
                n = len([g for g in (games or []) if g])
                if n:
                    by_period[int(period)] = n
            out[tid] = by_period
        return out

    # -- transactions ------------------------------------------------------

    def pending_transactions(self, team_id: Optional[int] = None) -> List[Mapping[str, Any]]:
        """Outstanding waiver claims and trade offers.

        Read before any write that could be a duplicate. This is the only
        reliable way to answer "did my timed-out POST actually land", and the
        reason ``espn/write.py`` refuses to blind-retry.
        """
        team_id = self.cfg.team_id if team_id is None else team_id
        raw = self._get(self._league_url, ("mPendingTransactions",))
        out = []
        for t in raw.get("pendingTransactions") or []:
            if int(t.get("teamId", -1)) == int(team_id):
                out.append(t)
        return out

    # -- projections -------------------------------------------------------

    def player_projections(
        self, player_ids: Iterable[PlayerId]
    ) -> Dict[PlayerId, Dict[str, Dict[StatId, float]]]:
        """Raw stat splits per player: ``{player: {split_key: {stat: value}}}``.

        This is the fallback projection path, and as of today it is the *primary*
        one -- DARKO's public snapshot is months stale (see sources/darko.py), so
        ESPN's ``statSourceId=1`` projection plus the last-15 split is what the
        optimizer actually runs on. Treated as a first-class source accordingly,
        not as an emergency hatch.
        """
        wanted = [int(p) for p in player_ids]
        if not wanted:
            return {}
        # No ``limit`` here, deliberately. ESPN rejects a limit that is not
        # accompanied by a sort with
        #     400 FILTER_LIMIT_MISSING_SORT
        #     "Filter: Limit request must be accompanied by a sort"
        # which is what this method did on every call until it was run against a
        # live league. ``filterIds`` already bounds the result set to exactly the
        # players asked for, so the limit was redundant as well as fatal.
        flt = {"players": {"filterIds": {"value": wanted}}}
        raw = self._get(self._league_url, ("kona_player_info",), fantasy_filter=flt)
        out: Dict[PlayerId, Dict[str, Dict[StatId, float]]] = {}
        for p in raw.get("players") or []:
            player = p.get("player") or p
            pid = int(player.get("id"))
            splits: Dict[str, Dict[StatId, float]] = {}
            for st in player.get("stats") or []:
                key = "%s%s" % (st.get("statSourceId"), st.get("statSplitTypeId"))
                stats = st.get("averageStats") or st.get("stats") or {}
                splits[key] = {int(k): float(v) for k, v in stats.items()}
            out[pid] = splits
        return out


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _opt_int(v: Any) -> Optional[int]:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _is_on_team(entry: Mapping[str, Any], team_id: int) -> bool:
    """Is this roster entry a player who is still actually on ``team_id``?

    Deliberately asymmetric: an entry is excluded only on **positive evidence**
    that the player has left -- a ``status`` that is present and not
    ``ONTEAM``, or an ``onTeamId`` that is present and not ours. When ESPN
    omits both fields the entry is kept.

    The asymmetry is the point. Wrongly keeping a departed player costs one
    rejected lineup write, which is loud and recoverable. Wrongly *excluding*
    a real player would silently shrink the roster below its slot count and
    leave him unmanaged, which is quiet and would be blamed on the optimizer.
    Both live leagues report ``ONTEAM`` plus a matching ``onTeamId`` for every
    player, so the common path is unaffected either way.
    """
    ppe = entry.get("playerPoolEntry") or {}

    status = ppe.get("status")
    if status is not None and str(status).upper() != "ONTEAM":
        return False

    on_team = _opt_int(ppe.get("onTeamId"))
    if on_team is not None and on_team != int(team_id):
        return False

    return True


def _parse_entry(raw: Mapping[str, Any]) -> RosterEntry:
    """Handle both roster entries and free-agent player wrappers.

    ``mRoster`` nests the player under ``playerPoolEntry.player``;
    ``kona_player_info`` sometimes puts it at ``player`` and sometimes at the
    top level. Normalizing here keeps the branch out of four call sites.
    """
    pool = raw.get("playerPoolEntry") or raw
    player = pool.get("player") or pool
    pid = int(player.get("id"))

    status_raw = str(player.get("injuryStatus") or "ACTIVE").upper()
    status = ids.INJURY_NORMALIZE.get(status_raw, "ACTIVE")
    if status == "ACTIVE" and player.get("injured"):
        # ESPN sometimes flags `injured` without a usable status string.
        status = "DAY_TO_DAY"

    eligible = frozenset(int(s) for s in (player.get("eligibleSlots") or []))

    # IR eligibility is NOT just "slot 13 is in eligibleSlots" -- ESPN lists the
    # IR slot broadly, so deriving it that way marks the whole roster eligible
    # and makes the write-layer IR check dead code. ESPN's actual rule is its
    # own and the rejection code for an ineligible IR move has not been observed,
    # so we require the stricter conjunction: ESPN offers the slot *and* the
    # player is actually Out. Conservative on purpose -- the cost of being wrong
    # here is a rejected transaction that takes the day's whole lineup with it.
    ir_ok = ids.IR_SLOT in eligible and status == "OUT"

    # The market readings. Present in both payload shapes -- verified live
    # 2026-10-21 against ``mRoster`` and ``kona_player_info`` both -- and read
    # with ``_opt_float``/``_opt_int`` rather than ``or 0`` so that a field ESPN
    # omitted stays distinguishable from one it reported as zero. See the note
    # on ``RosterEntry.percent_owned`` for why they are carried at all.
    ownership = player.get("ownership") or {}
    ranks = player.get("draftRanksByRankType") or {}
    standard = ranks.get("STANDARD") or {}

    return RosterEntry(
        player_id=pid,
        name=str(player.get("fullName") or player.get("name") or "player %d" % pid),
        current_slot=int(raw.get("lineupSlotId", ids.BENCH_SLOT)),
        eligible_slots=eligible,
        pro_team_id=int(player.get("proTeamId") or 0),
        injury_status=status,
        lineup_locked=bool(raw.get("lineupLocked") or raw.get("playerPoolEntry", {}).get("lineupLocked")),
        ir_eligible=ir_ok,
        # ``pool`` already normalized the two payload shapes above, which is
        # what makes one line enough: on ``mRoster`` this reads
        # ``playerPoolEntry.status``, and on ``kona_player_info`` the same
        # field sits on the wrapper that ``pool`` fell back to.
        acquisition_status=str(pool.get("status") or "").upper(),
        percent_owned=_opt_float(ownership.get("percentOwned")),
        draft_adp=_opt_float(ownership.get("averageDraftPosition")),
        draft_rank=_opt_int(standard.get("rank")),
        outlook=str(player.get("seasonOutlook") or "").strip(),
    )


def _matchup_windows(settings: Mapping[str, Any]) -> Dict[int, MatchupWindow]:
    """``matchupPeriod -> (first, last) scoringPeriodId``, when ESPN says.

    Returns ``{}`` on both live leagues, and that is the correct answer rather
    than a parse failure. ``scheduleSettings.matchupPeriods`` arrives
    **self-referential** -- ``{"1": [1], "2": [2], ...}`` alongside
    ``matchupPeriodLength: 1`` -- which decodes to "every matchup period is one
    scoring period long". It is not: matchup periods are Monday-Sunday weeks, and
    a one-day period could not accommodate a 40-game cap with ten starting
    slots. ESPN appears to use this field only in leagues whose weeks are
    explicitly configured.

    So a self-referential payload is detected and dropped here, at the parse
    boundary, instead of being handed on as 22 plausible-looking one-day
    ``MatchupWindow`` objects. The consumer is ``solve/calendar.py``, which
    models the weeks from the NBA calendar and cross-checks against ESPN's own
    ``currentMatchupPeriod``; a populated-but-wrong mapping here would silently
    win over a model that is actually verified.
    """
    sched = settings.get("scheduleSettings") or {}
    raw = sched.get("matchupPeriods") or {}
    out: Dict[int, MatchupWindow] = {}
    for period, members in raw.items():
        vals = [int(m) for m in (members or [])]
        if not vals:
            continue
        out[int(period)] = MatchupWindow(
            matchup_period=int(period),
            first_scoring_period=min(vals),
            last_scoring_period=max(vals),
        )

    degenerate = [
        w for w in out.values()
        if w.first_scoring_period == w.last_scoring_period == w.matchup_period
    ]
    if out and len(degenerate) == len(out):
        log.info(
            "scheduleSettings.matchupPeriods is self-referential (%d entries, "
            "each period mapping to itself); ignoring it and deriving matchup "
            "periods from the NBA calendar instead",
            len(out),
        )
        return {}
    return out
