"""Opponent defence: how much a team suppresses the fantasy scoring it faces.

Every number in this module was measured against five complete seasons of
ESPN's own per-game actuals (2021-22 through 2025-26, 131,986 player-games),
pulled from the public, unauthenticated endpoint this module fetches. The
measurements are in ``docs/opponent-defence.md``; the ones that shaped the code
are repeated here because they are the reason it has the shape it has.

**What is real.** A team's effect on the fantasy production it concedes is a
genuine, persistent property. Splitting each season's games into odd and even
scoring periods and fitting both halves independently gives a Spearman-Brown
reliability of **0.75 - 0.90** (mean 0.84) across the five seasons, and last
season's number predicts this season's at **r = 0.43 - 0.62**. The spread is
small: a standard deviation of **3.4%**, with the extremes at 0.92 and 1.07.

**What is not real.** Splitting that effect across five positions is mostly
noise -- reliability **0.25**, year over year **0.15**. The apparent structure
collapses to a single axis: *centres*. Defined as eligibility at the ``C``
slot, which is the only test available at run time, the centre interaction
reliability is **0.56** and persists year over year at **0.35**. Rim
protection is a structural team property; "soft against shooting guards
specifically" is not. So this module fits exactly two things per team -- an
overall multiplier and a centre adjustment -- and deliberately refuses to fit
the other four positions.

**What it is worth.** Honest numbers, because they are small. Simulating
lineups out of sample (fit on the first half of each season, choose 10 of a
random 13 on the second half, 40,000 trials) the adjustment is worth
**+0.178 fantasy points per day, 95% CI [+0.111, +0.244]** -- about **+1.2 a
week**. On the ~17% of days where it actually changes the lineup it is worth
**+1.0 points per day**, which is where all of the value lives. For scale, the
rebound fix in the same units was worth +31 and +55 points a week, and the
mean adjustment here is 0.81 points against a mean per-game error of 9.8. This
is a real effect that is also a very small one -- roughly 25 to 45 times
smaller than the last thing fixed here -- and it is wired in because it is
nearly free once the archive exists, not because it will win a matchup.

That ratio is also the reason the fit is shrunk hard and the reason nothing
here is allowed to fail loudly: a 3% correction is never worth a run.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import httpx

from espn import ids

from .base import SourceResult, build_result, fetch_json, guard, utcnow

__all__ = [
    "PlayerGame",
    "TeamDefense",
    "Defense",
    "parse_schedule",
    "parse_player_games",
    "fit_defense",
    "PRIOR_GAMES",
    "CENTRE_PRIOR_GAMES",
    "YEAR_PERSISTENCE",
    "MIN_PLAYER_GAMES",
    "MIN_BASELINE_FP",
]

log = logging.getLogger(__name__)

#: Shrinkage denominators, in player-games. A team cell fills ~5x faster than
#: a centre cell, so they are not the same number. Both were chosen so that a
#: full season lands near the measured reliability rather than over-fitting it:
#: at ~760 games a team cell keeps 86% of its raw signal, at ~150 a centre cell
#: keeps 70%.
PRIOR_GAMES: float = 125.0
CENTRE_PRIOR_GAMES: float = 62.5

#: How much of last season's fitted effect carries into this one. The measured
#: year-over-year correlation of the team effect, 2021-22 -> 2025-26: +0.546,
#: +0.434, +0.622, +0.609. Rounded down, because a prior that is too strong in
#: October is the failure mode with no feedback to correct it.
YEAR_PERSISTENCE: float = 0.55

#: A player needs this many games before his own average is a usable baseline,
#: and the baseline needs to be this large before a ratio against it means
#: anything. Below 10 fp/g the denominator is small enough that one good night
#: reads as a 300% opponent effect.
MIN_PLAYER_GAMES: int = 20
MIN_BASELINE_FP: float = 10.0

#: Below this many usable player-games the fit is abandoned rather than
#: published. An empty table is a correct answer; a table fitted on 40 games is
#: a wrong one that looks the same.
MIN_TOTAL_GAMES: int = 2000

_HOST = ids.READ_HOST
_FILTER = '{"players":{"limit":3000,"offset":0}}'


@dataclass(frozen=True)
class PlayerGame:
    """One player's one game, reduced to what the fit needs.

    ``centre_eligible`` rather than a position: ESPN's ``defaultPositionId``
    is not on :class:`models.RosterEntry` and so is not available when the
    multiplier is applied. Fitting on a field the application cannot read back
    would be fitting on something else. Re-measured both ways -- centre
    interaction reliability 0.593 by ``defaultPositionId``, 0.558 by C-slot
    eligibility -- so the honest definition costs almost nothing.
    """

    season: int
    player_id: int
    scoring_period: int
    pro_team: int
    opponent: int
    centre_eligible: bool
    minutes: float
    points: float


@dataclass(frozen=True)
class TeamDefense:
    """The fitted table: two numbers per NBA team, plus its provenance.

    ``team`` is a multiplier around 1.0, ``centre`` an additive adjustment
    around 0.0 applied on top of it for centre-eligible players only. They are
    kept apart rather than pre-multiplied into 60 cells so that a reader can
    see which half of a 1.09 came from the defence overall and which from the
    centre axis -- and because the two are shrunk against different priors.
    """

    team: Mapping[int, float] = field(default_factory=dict)
    centre: Mapping[int, float] = field(default_factory=dict)
    seasons: Tuple[int, ...] = ()
    games: int = 0

    def multiplier(self, opponent: Optional[int], centre_eligible: bool) -> float:
        """The factor to apply to a player's projected line. Always finite.

        Returns exactly 1.0 for an unknown or missing opponent, which is the
        same answer as "this team is perfectly average" and the only safe one:
        a player whose opponent could not be resolved must not be moved by
        this module, in either direction.
        """
        if opponent is None:
            return 1.0
        m = float(self.team.get(int(opponent), 1.0))
        if centre_eligible:
            m *= 1.0 + float(self.centre.get(int(opponent), 0.0))
        # A hard clamp, not an assertion. The fit cannot currently produce
        # anything near these bounds -- the widest cell over five seasons is
        # 0.92 * (1 - 0.066) = 0.857 -- so tripping this means the input
        # changed shape, and a 20% cap on a 3% effect fails quietly in the
        # right direction.
        return max(0.80, min(m, 1.20))

    @property
    def empty(self) -> bool:
        return not self.team


def parse_schedule(
    payload: Any,
    season: int,
) -> Dict[Tuple[int, int, int], int]:
    """``(season, scoring_period, pro_team) -> opponent`` from ``proTeamSchedules_wl``.

    Keyed by season as well as period because the player payload for season Y
    ships season Y-1's rows alongside it (``seasons/2027`` returns 1,033 live
    rows and all 26,460 of 2026), and looking last season's games up against
    this season's calendar silently resolved 13,128 of them to the wrong
    opponent the first time this was written.

    *season* is passed in rather than read out because **this payload does not
    contain it**: verified 2026-10-26 against ``seasons/2027``, whose only top
    level keys are ``display`` and ``settings``, with no ``seasonId`` on the
    response, on ``settings``, or on any individual game. The caller asked for
    a specific season's calendar and is the only thing that knows which.
    """
    out: Dict[Tuple[int, int, int], int] = {}
    settings = (payload or {}).get("settings") or {}
    for team in settings.get("proTeams") or []:
        tid = team.get("id")
        if tid is None:
            continue
        for period, games in (team.get("proGamesByScoringPeriod") or {}).items():
            for g in games or []:
                if not g:
                    continue
                home, away = g.get("homeProTeamId"), g.get("awayProTeamId")
                if home is None or away is None:
                    continue
                opp = away if home == tid else home
                out[(int(season), int(period), int(tid))] = int(opp)
    return out


def parse_player_games(
    payload: Any,
    schedule: Mapping[Tuple[int, int, int], int],
    coefficients: Mapping[int, float],
) -> List[PlayerGame]:
    """Every per-game actual in *payload*, scored with *coefficients*.

    The rows wanted are ``statSourceId == 0`` (actual, not projection) and
    ``statSplitTypeId == 5`` (one scoring period). Each carries its own
    ``seasonId`` and its own ``proTeamId`` -- the team the player was on *for
    that game*, which is what makes a mid-season trade resolve correctly
    instead of attributing his whole season to wherever he ended up.
    """
    out: List[PlayerGame] = []
    for pl in payload or []:
        pid = pl.get("id")
        if pid is None:
            continue
        centre = ids.CENTER_SLOT in (pl.get("eligibleSlots") or [])
        for row in pl.get("stats") or []:
            if row.get("statSourceId") != ids.STAT_SOURCE_ACTUAL:
                continue
            if row.get("statSplitTypeId") != ids.SPLIT_ONE_SCORING_PERIOD:
                continue
            stats = row.get("stats") or {}
            if not stats:
                continue
            season, period = row.get("seasonId"), row.get("scoringPeriodId")
            team = row.get("proTeamId")
            if season is None or period is None or team is None:
                continue
            key = (int(season), int(period), int(team))
            opp = schedule.get(key)
            if opp is None:
                continue
            fp = 0.0
            for stat, coeff in coefficients.items():
                fp += coeff * float(stats.get(str(stat), 0.0) or 0.0)
            out.append(PlayerGame(
                season=int(season),
                player_id=int(pid),
                scoring_period=int(period),
                pro_team=int(team),
                opponent=int(opp),
                centre_eligible=centre,
                minutes=float(stats.get(str(ids.MINUTES), 0.0) or 0.0),
                points=fp,
            ))
    return out


def _ratios(games: Iterable[PlayerGame]) -> List[Tuple[int, bool, float]]:
    """``(opponent, centre_eligible, points / his own average)`` per game.

    The baseline is **leave-one-out**: a player's average over his *other*
    games in that season. Including the game in its own baseline would let a
    40-point night raise the bar it is then measured against, which drags every
    ratio toward 1.0 and understates whatever is there.
    """
    by_player: Dict[Tuple[int, int], List[PlayerGame]] = collections.defaultdict(list)
    for g in games:
        if g.minutes > 0.0:
            by_player[(g.season, g.player_id)].append(g)
    out: List[Tuple[int, bool, float]] = []
    for rows in by_player.values():
        n = len(rows)
        if n < MIN_PLAYER_GAMES:
            continue
        total = sum(r.points for r in rows)
        for r in rows:
            base = (total - r.points) / (n - 1)
            if base < MIN_BASELINE_FP:
                continue
            out.append((r.opponent, r.centre_eligible, r.points / base))
    return out


def _fit_one_season(
    rows: List[Tuple[int, bool, float]],
    prior_team: Mapping[int, float],
    prior_centre: Mapping[int, float],
) -> Tuple[Dict[int, float], Dict[int, float]]:
    """Fit one season's rows, shrinking toward a prior instead of toward 1.0.

    Shrinking toward last season's fitted number rather than toward neutral is
    what makes this usable in October: with five games played the weight on
    the current season is near zero and the answer is last season's, discounted
    by :data:`YEAR_PERSISTENCE`. With a full season it is almost entirely the
    current one. No special case for "early in the season" -- the sample size
    does that on its own.
    """
    if not rows:
        return dict(prior_team), dict(prior_centre)
    grand = sum(r for _o, _c, r in rows) / len(rows)
    by_team: Dict[int, List[float]] = collections.defaultdict(list)
    for opp, _c, r in rows:
        by_team[opp].append(r)
    team: Dict[int, float] = dict(prior_team)
    for opp, vals in by_team.items():
        w = len(vals) / (len(vals) + PRIOR_GAMES)
        raw = sum(vals) / len(vals) - grand + 1.0
        team[opp] = w * raw + (1.0 - w) * prior_team.get(opp, 1.0)
    by_centre: Dict[int, List[float]] = collections.defaultdict(list)
    for opp, centre, r in rows:
        if centre:
            by_centre[opp].append(r / team.get(opp, 1.0))
    centre: Dict[int, float] = dict(prior_centre)
    for opp, vals in by_centre.items():
        w = len(vals) / (len(vals) + CENTRE_PRIOR_GAMES)
        raw = sum(vals) / len(vals) - grand
        centre[opp] = w * raw + (1.0 - w) * prior_centre.get(opp, 0.0)
    return team, centre


def fit_defense(games: Iterable[PlayerGame]) -> TeamDefense:
    """Fit the table, oldest season first so each one priors the next.

    Returns an empty :class:`TeamDefense` -- every multiplier 1.0 -- when there
    is not enough to fit. That is a deliberate answer and not an error: the
    caller cannot tell a fit from a shrug by looking at the numbers, so the
    difference has to be in whether there are any.
    """
    rows = list(games)
    seasons = sorted({g.season for g in rows})
    usable = 0
    team: Dict[int, float] = {}
    centre: Dict[int, float] = {}
    for season in seasons:
        ratios = _ratios([g for g in rows if g.season == season])
        usable += len(ratios)
        # Carry the previous season forward at less than full strength before
        # it becomes this season's prior -- measured persistence, not 1.0.
        team = {k: 1.0 + (v - 1.0) * YEAR_PERSISTENCE for k, v in team.items()}
        centre = {k: v * YEAR_PERSISTENCE for k, v in centre.items()}
        team, centre = _fit_one_season(ratios, team, centre)
    if usable < MIN_TOTAL_GAMES:
        log.warning(
            "opponent defence: only %d usable player-games (need %d) -- "
            "publishing an empty table, every multiplier 1.0", usable,
            MIN_TOTAL_GAMES)
        return TeamDefense(seasons=tuple(seasons), games=usable)
    return TeamDefense(team=team, centre=centre, seasons=tuple(seasons),
                       games=usable)


class Defense:
    """Opponent-defence source.

    Reads the **public** player endpoint -- no ``espn_s2``, no league id, no
    cookie of any kind -- so it works whether or not the league credentials are
    healthy, and a dead cookie cannot take the projection's opponent term with
    it. One request returns the current season *and* the previous one, which is
    where the prior comes from for free.

    ``max_staleness`` is nominal. This payload carries no timestamp of its own,
    so there is nothing to measure an age against; the real gate is
    :data:`MIN_TOTAL_GAMES`, which fails the fit on insufficiency rather than
    on age. The attribute exists because the contract requires it and because a
    caller that caches the fitted table needs a refresh interval -- the table
    moves slowly enough that refetching 25 MB more than weekly is waste.
    """

    name = "opponent-defense"

    def __init__(
        self,
        season: int,
        coefficients: Mapping[int, float],
        *,
        client: Optional[httpx.Client] = None,
        max_staleness: timedelta = timedelta(days=7),
    ) -> None:
        self.season = int(season)
        self.coefficients = dict(coefficients)
        self.client = client
        self.max_staleness = max_staleness

    @guard
    def fetch(self) -> Optional[SourceResult]:
        sched = fetch_json(
            "%s/apis/v3/games/%s/seasons/%d?view=proTeamSchedules_wl"
            % (_HOST, ids.GAME, self.season),
            client=self.client,
        )
        schedule = parse_schedule(sched, self.season)
        # Last season's calendar as well: the player payload ships last
        # season's games and they cannot be resolved without it.
        prior = fetch_json(
            "%s/apis/v3/games/%s/seasons/%d?view=proTeamSchedules_wl"
            % (_HOST, ids.GAME, self.season - 1),
            client=self.client,
        )
        schedule.update(parse_schedule(prior, self.season - 1))
        players = fetch_json(
            "%s/apis/v3/games/%s/seasons/%d/players?view=kona_player_info"
            % (_HOST, ids.GAME, self.season),
            client=self.client,
            headers={"X-Fantasy-Filter": _FILTER},
        )
        table = fit_defense(
            parse_player_games(players, schedule, self.coefficients))
        if table.empty:
            return None
        log.info("opponent defence fitted from %d player-games across %s",
                 table.games, ", ".join(str(s) for s in table.seasons))
        return build_result(self.name, utcnow(), table, self.max_staleness)
