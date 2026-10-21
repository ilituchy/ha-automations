"""The season calendar: scoring periods, dates, and matchup-period windows.

This module exists because ESPN tells you two numbers -- ``latestScoringPeriod``
and ``currentMatchupPeriod`` -- and nothing usable about the relationship
between them. ``scheduleSettings.matchupPeriods`` *looks* like the answer and is
not: on both live leagues it comes back self-referential,
``{1: [1], 2: [2], ...}`` with ``matchupPeriodLength: 1``, which would say every
matchup period is one day long. It is not; the games-played cap of 40 would be
unreachable in a one-day period with ten starting slots.

So the mapping has to be modelled, and a model that decides how much of a
weekly budget is left needs to be checkable. Two findings from probing the live
2026-27 season make that possible:

1. **Scoring periods are strictly daily and linear.** Converted in US Eastern
   (see :data:`espn.ids.GAME_DATE_TZ`), every one of the 156 game days resolves
   to exactly one date, and ``date(N) == date(first) + (N - first)`` days holds
   with zero mismatches -- including across the gameless days, which still
   consume a period number.
2. **Matchup periods are Monday-Sunday weeks.** Week 1 of 2026-27 is scoring
   periods 1-6 (Tue 2026-10-20 opener through Sun 10-25), week 2 is 7-13, and
   so on. Independently: this model reproduces ESPN's own
   ``currentMatchupPeriod`` on both leagues.

That second point is the important one, because it is a *falsifiable* model
rather than an assumption. :meth:`SeasonCalendar.verify` re-checks it against
ESPN's own two numbers on every run, and ``main.py`` declines to price the cap
when it disagrees. The failure mode being guarded against is quiet: a week
boundary off by one day prices the budget against the wrong number of remaining
games, which does not error -- it just benches the wrong players for a week.

Nothing here is derived from the *fantasy* schedule, only from the NBA one, so
one calendar is valid for every league in a season.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

#: Weekday a matchup period starts on, ``date.weekday()`` convention
#: (0 == Monday). Probed, not assumed -- see the module docstring.
MATCHUP_START_WEEKDAY: int = 0

#: Days in a matchup period. Distinct from
#: :data:`espn.ids.SCORING_PERIODS_PER_MATCHUP`, which happens to be the same
#: number but means something else entirely: that one is the fixed divisor ESPN
#: applies to the cap, and it stays 7 even for a six-day opening week. This one
#: is the stride of the week grid. They are kept separate so that if ESPN ever
#: changes either, changing one does not silently move the other.
DAYS_PER_MATCHUP: int = 7


def _week_start(day: date) -> date:
    """The Monday of ``day``'s week."""
    return day - timedelta(days=(day.weekday() - MATCHUP_START_WEEKDAY) % 7)


@dataclass(frozen=True)
class SeasonCalendar:
    """Scoring period <-> date <-> matchup period, for one NBA season.

    Built by :meth:`from_period_dates` from
    ``EspnRead.scoring_period_dates()``. The stored form is deliberately tiny --
    an anchor period, its date, and the season's bounds -- because the mapping
    is linear and keeping the full dict would invite code that trusts an
    individual entry over the model, which is exactly the inconsistency
    :attr:`linearity_errors` is here to surface.
    """

    #: Lowest scoring period with NBA games, and its US game date. The anchor
    #: for every conversion below.
    first_period: int
    first_date: date
    #: Highest scoring period with NBA games. Windows are clamped to it, so a
    #: matchup period that runs past the end of the season reports only the days
    #: that exist.
    last_period: int
    #: Periods that actually have at least one NBA game. Gameless days inside a
    #: week still occupy a period number and still appear in
    #: :meth:`periods_in_matchup`; they are excluded from
    #: :meth:`game_periods_in_matchup`.
    game_periods: FrozenSet[int]
    #: ``(period, observed_date, modelled_date)`` for every probed period whose
    #: real date disagrees with the linear model. Empty on the live season.
    #: Non-empty means :attr:`trustworthy` is False and the cap must not be
    #: priced from this calendar.
    linearity_errors: Tuple[Tuple[int, date, date], ...] = ()

    # -- construction ------------------------------------------------------

    @classmethod
    def from_period_dates(
        cls, period_dates: Mapping[int, date]
    ) -> Optional["SeasonCalendar"]:
        """Build from ``scoringPeriodId -> US game date``.

        Returns ``None`` for an empty mapping rather than raising: a season
        document with no games in it is a plausible transient (ESPN serving a
        season that has not been published yet), and the caller's response to
        "no calendar" is the same as its response to "calendar not trustworthy".

        Every entry is checked against the linear model and disagreements are
        recorded rather than fixed. Repairing them would mean choosing which
        source to believe, and there is no principled way to make that choice
        from inside this function -- the useful behaviour is to refuse to price
        the budget, which is what :attr:`trustworthy` lets the caller do.
        """
        if not period_dates:
            log.error("no scoring period dates; cannot build a season calendar")
            return None

        first_period = min(period_dates)
        first_date = period_dates[first_period]
        errors: List[Tuple[int, date, date]] = []
        for period, observed in sorted(period_dates.items()):
            modelled = first_date + timedelta(days=period - first_period)
            if observed != modelled:
                errors.append((period, observed, modelled))

        if errors:
            log.error(
                "scoring periods are not daily-linear: %d of %d probed periods "
                "disagree with the model (e.g. period %d is %s, model says %s). "
                "The games-played cap will not be priced from this calendar.",
                len(errors), len(period_dates),
                errors[0][0], errors[0][1], errors[0][2],
            )

        return cls(
            first_period=first_period,
            first_date=first_date,
            last_period=max(period_dates),
            game_periods=frozenset(period_dates),
            linearity_errors=tuple(errors),
        )

    @property
    def trustworthy(self) -> bool:
        """True when the linear day model held across every probed period."""
        return not self.linearity_errors

    # -- period <-> date ---------------------------------------------------

    def date_of(self, period: int) -> date:
        """US game date of a scoring period. Linear, so it answers for
        gameless days and for periods outside the season too."""
        return self.first_date + timedelta(days=int(period) - self.first_period)

    def period_of(self, day: date) -> int:
        """Inverse of :meth:`date_of`.

        May return a period below :attr:`first_period` or above
        :attr:`last_period` for a date outside the season. That is intentional --
        :meth:`periods_in_matchup` relies on it to compute a week window and
        then clamps, which is cleaner than special-casing the first and last
        weeks of the season.
        """
        return self.first_period + (day - self.first_date).days

    def in_season(self, period: int) -> bool:
        return self.first_period <= int(period) <= self.last_period

    # -- matchup periods ---------------------------------------------------

    def matchup_period_of(self, period: int) -> int:
        """Which matchup period a scoring period falls in. 1-based.

        Week 1 is the week *containing* the season opener, so an opener on a
        Tuesday gives a six-day week 1 -- which is the real shape of 2026-27 and
        the reason the cap must not be rescaled by the number of days in a week.
        """
        anchor = _week_start(self.first_date)
        return ((_week_start(self.date_of(period)) - anchor).days
                // DAYS_PER_MATCHUP) + 1

    def periods_in_matchup(self, matchup_period: int) -> List[int]:
        """Every scoring period in a matchup period, clamped to the season.

        Includes periods with no NBA games on them. That matters for the budget:
        the cap is spent over the whole week, so "which days are left in this
        week" and "which of those days my players have games on" are different
        questions, and conflating them in the first one would make the horizon
        depend on the roster.
        """
        start = (_week_start(self.first_date)
                 + timedelta(days=DAYS_PER_MATCHUP * (int(matchup_period) - 1)))
        lo = max(self.period_of(start), self.first_period)
        hi = min(self.period_of(start + timedelta(days=DAYS_PER_MATCHUP - 1)),
                 self.last_period)
        if hi < lo:
            return []
        return list(range(lo, hi + 1))

    def game_periods_in_matchup(self, matchup_period: int) -> List[int]:
        """:meth:`periods_in_matchup`, minus the days with no NBA games."""
        return [p for p in self.periods_in_matchup(matchup_period)
                if p in self.game_periods]

    def remaining_periods(
        self, matchup_period: int, from_period: int, *, inclusive: bool = True
    ) -> List[int]:
        """The horizon the remaining budget is spent over.

        ``inclusive`` keeps today in the window, which is the right default:
        today's lineup is one of the decisions being priced, so excluding it
        would price the cap as if today's starts were free.
        """
        first = int(from_period) if inclusive else int(from_period) + 1
        return [p for p in self.periods_in_matchup(matchup_period) if p >= first]

    def matchup_period_bounds(self, matchup_period: int) -> Optional[Tuple[date, date]]:
        """``(first, last)`` US game date of a matchup period, clamped."""
        periods = self.periods_in_matchup(matchup_period)
        if not periods:
            return None
        return self.date_of(periods[0]), self.date_of(periods[-1])

    # -- the self-falsifying check -----------------------------------------

    def verify(
        self, espn_scoring_period: int, espn_matchup_period: int
    ) -> Optional[str]:
        """Check the week model against ESPN's own two numbers.

        Returns ``None`` when they agree, otherwise a human-readable reason.
        Cheap -- both numbers are already in ``LeagueSettings`` -- and it is the
        only thing standing between a changed ESPN week convention and a season
        of quietly mis-priced budgets, so it runs every run rather than in a
        test.

        A scoring period outside the season is not treated as a disagreement:
        ESPN reports ``latestScoringPeriod`` during the preseason and the
        offseason too, and off-season is not evidence about week boundaries.
        """
        if not self.trustworthy:
            return ("scoring periods are not daily-linear (%d mismatches)"
                    % len(self.linearity_errors))
        period = int(espn_scoring_period)
        if not self.in_season(period):
            log.info(
                "ESPN reports scoring period %d, outside the season's %d..%d; "
                "skipping the matchup-period cross-check",
                period, self.first_period, self.last_period,
            )
            return None
        ours = self.matchup_period_of(period)
        if ours != int(espn_matchup_period):
            return (
                "week model says scoring period %d (%s) is matchup period %d, "
                "ESPN says %d" % (period, self.date_of(period), ours,
                                  int(espn_matchup_period))
            )
        return None

    def describe(self, matchup_period: int) -> str:
        bounds = self.matchup_period_bounds(matchup_period)
        if bounds is None:
            return "matchup period %d: outside the season" % matchup_period
        games = len(self.game_periods_in_matchup(matchup_period))
        return "matchup period %d: %s..%s, %d game days" % (
            matchup_period, bounds[0], bounds[1], games,
        )


def team_games_in(
    periods: Iterable[int],
    schedule: Mapping[int, Mapping[int, int]],
    pro_team_id: int,
) -> int:
    """How many games one NBA team plays over ``periods``.

    ``schedule`` is ``EspnRead.pro_team_scoring_periods()``:
    ``proTeamId -> {scoringPeriodId: games}``. Keyed in the fantasy
    ``proTeamId`` space, the same one as ``RosterEntry.pro_team_id``, so there
    is no id translation here to get wrong.

    An unknown team contributes zero rather than raising. ``proTeamId 0`` is
    ESPN's free-agent sentinel and shows up on real roster entries, so a missing
    key is an expected input, not a bug.
    """
    by_period = schedule.get(int(pro_team_id)) or {}
    return sum(int(by_period.get(int(p), 0)) for p in periods)


def game_days_for(
    periods: Sequence[int],
    schedule: Mapping[int, Mapping[int, int]],
    pro_team_id: int,
) -> List[int]:
    """The subset of ``periods`` on which ``pro_team_id`` plays."""
    by_period = schedule.get(int(pro_team_id)) or {}
    return [int(p) for p in periods if by_period.get(int(p))]


def opportunity_counts(
    periods: Sequence[int],
    schedule: Mapping[int, Mapping[int, int]],
    pro_team_ids: Iterable[int],
) -> Dict[int, int]:
    """``proTeamId -> games in window``, for a set of teams.

    Convenience for the budget layer, which needs one count per rostered
    player's team to build the candidate list that prices the cap.
    """
    return {
        int(t): team_games_in(periods, schedule, t)
        for t in {int(x) for x in pro_team_ids}
    }
