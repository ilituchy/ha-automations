"""Games-played accounting: how much of this week's cap has been spent.

One number -- games used so far in the current matchup period -- feeds the
shadow price in ``solve/budget.py``, and getting it wrong is not a rounding
problem. Over-count and the optimizer hoards a budget it has already been given;
under-count and it overspends a hard cap and arrives at Sunday with players it
is not allowed to start.

ESPN offers two readings and both are now usable:

* ``cumulativeScore.statBySlot`` under ``mMatchupScore`` is the per-matchup
  figure the cap is actually enforced against. This module spent its first
  weeks reporting that it "came back ``None`` on every matchup row of both
  leagues", and that was wrong -- it was ``EspnRead.games_used`` reading a
  field named ``score`` when ESPN sends ``value``. The real row is
  ``{"15": {"statId": 42, "value": 2.0, "limitExceeded": false}}``, which also
  settles the shape question: **one pooled entry at the team-total pseudo-slot**,
  matching what ``solve/budget.py`` already assumes, not a figure per lineup
  slot.
* ``teams[].valuesByStat["42"]`` is season-to-date team games played. Confirmed
  shape, wrong horizon.

The weekly number can therefore be *read*, and is also still reconstructed as a
difference of two season-to-date readings -- which means remembering the one
taken at the start of the week, hence ``gp_snapshots`` in ``state.py``. Both
readings are computed on every run, logged and cross-checked, and **when they
disagree the direct reading wins**.

That rule used to be "the larger wins", on the argument that the two errors are
not symmetric: treating the budget as more spent than it is costs a marginal
start, treating it as less spent costs a legal lineup. The argument was sound
and the premise was not -- it assumed the two readings were equally plausible,
and they are not. ``statBySlot`` is the counter ESPN enforces the cap against.
The delta is a *reconstruction*, and it rests on two things that can be wrong
on their own: a baseline snapshot taken at the right moment, and this app's
**derived** matchup-period boundaries -- derived because ESPN's own
``matchupPeriods`` map is self-referential, and demonstrably not what ESPN is
using, since it reports a scoring period one behind the calendar's every run.

The first informative comparison was the opening day of matchup period 2 --
before that, matchup-to-date and season-to-date are the same number by
construction, so the agreement on 2026-10-21 (1 and 1, 4 and 4) carried no
information. That comparison happened on 2026-10-28 and the reconstruction
lost, in both leagues at once: ``statBySlot`` said 10 and 8, the delta said 25
and 18. Counting the lineups of every completed scoring period of the matchup
period by hand gave 10 and 8. Under the old rule the optimizer priced a 44-game
cap as 19 games from exhaustion when 34 were left, which put the shadow price
at 32.8 and benched a player averaging 44.6.

Nothing here decides anything. It returns a measurement plus how much to
believe it, and ``main.py`` is what declines to act on a bad one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence

from espn import ids
from solve.calendar import SeasonCalendar

log = logging.getLogger(__name__)

#: How games used was arrived at, worst to best.
SOURCE_UNKNOWN = "unknown"
SOURCE_SEASON_DELTA = "season-delta"
SOURCE_STAT_BY_SLOT = "statBySlot"
SOURCE_BOTH = "both-agree"


@dataclass(frozen=True)
class GamesUsed:
    """A measurement of this matchup period's spend, with its provenance.

    ``trustworthy`` is False when the number is a guess rather than a reading.
    The caller's options then are to run the optimizer with no cap pricing (it
    degrades to plain daily maximization, which is correct but spendthrift) or
    to stay in shadow mode. It must not be to pretend the number is zero.
    """

    games: float
    source: str
    trustworthy: bool
    from_stat_by_slot: Optional[int] = None
    from_season_delta: Optional[int] = None
    note: str = ""

    def describe(self) -> str:
        bits = ["%.0f games used (%s)" % (self.games, self.source)]
        if self.from_stat_by_slot is not None:
            bits.append("statBySlot=%d" % self.from_stat_by_slot)
        if self.from_season_delta is not None:
            bits.append("season-delta=%d" % self.from_season_delta)
        if self.note:
            bits.append(self.note)
        return "; ".join(bits)


class _Reader(Protocol):
    """The slice of ``EspnRead`` this module uses. Keeps tests from needing
    an HTTP client to exercise the reconciliation."""

    def games_used(self) -> Mapping[int, int]: ...
    def season_games_played(self) -> Optional[int]: ...


class _Snapshots(Protocol):
    """The slice of ``state.Store`` this module uses."""

    def record_gp_snapshot(self, **kwargs: object) -> None: ...
    def gp_snapshot(
        self, league_id: int, team_id: int, matchup_period: int
    ) -> Optional[Mapping[str, object]]: ...


def starts_from_stat_by_slot(by_slot: Mapping[int, int]) -> Optional[int]:
    """Read this matchup period's games used out of ``statBySlot``.

    ESPN sends **one pooled entry, at the team-total pseudo-slot** -- the same
    slot ``solve/budget.py`` reads the cap itself from, which is what makes the
    two numbers comparable. Live, both leagues:

        {"15": {"statId": 42, "value": 2.0, "limitExceeded": false}}

    So this reads ``ids.TEAM_TOTAL_LIMIT_SLOT`` specifically rather than
    summing "the starting slots". The sum was the per-slot cap reading that
    ``budget.py``
    abandoned, and it only ever agreed with the pooled reading by accident:
    ``ids.is_starting_slot(15)`` is True because 15 is not bench or IR, so the
    one real entry fell inside the sum. Any genuine per-slot payload would have
    been silently added up into a number the cap is not enforced against.

    Other keys are logged and ignored rather than summed in or raised on. A
    per-slot payload is the shape ``budget.matchup_cap`` refuses outright, but
    refusing here would take down a run over the *measurement* while the cap
    itself is still readable; this module's job is to return a number plus how
    much to believe it and let ``main.py`` decline.

    Returns ``None`` when the pooled entry is absent, including for an empty
    mapping: "ESPN sent no per-slot data" and "ESPN says zero games" are
    different claims, and the first must not masquerade as the second.
    """
    if not by_slot:
        return None

    pooled: Optional[int] = None
    extra = []
    for slot, value in by_slot.items():
        if int(slot) == ids.TEAM_TOTAL_LIMIT_SLOT:
            if value is not None:
                pooled = int(value)
        elif value is not None:
            extra.append((int(slot), int(value)))

    if extra:
        log.error(
            "statBySlot carried entries at real lineup slot(s) %s alongside "
            "%sthe pooled team-total slot %d. That is the per-slot cap shape "
            "this codebase does not model (see budget.PooledCapUnsupported); "
            "ignoring them, so games used may be under-read.",
            ", ".join("%d (%s)=%d" % (s, ids.slot_name(s), v) for s, v in extra),
            "" if pooled is not None else "no entry at ",
            ids.TEAM_TOTAL_LIMIT_SLOT,
        )

    return pooled


def measure(
    read: _Reader,
    store: Optional[_Snapshots],
    *,
    league_id: int,
    team_id: int,
    calendar: Optional[SeasonCalendar],
    scoring_period: int,
    matchup_period: int,
) -> GamesUsed:
    """Best available reading of games used this matchup period.

    Also *writes* the baseline snapshot when today is the first scoring period
    of the week, which is the only moment the baseline can be captured. A run
    that skips this -- because the add-on was down, or because the week started
    before it was installed -- leaves the week without a baseline, which is
    reported rather than papered over.
    """
    by_slot = dict(read.games_used() or {})
    direct = starts_from_stat_by_slot(by_slot)

    season_gp = read.season_games_played()
    delta: Optional[int] = None
    baseline_note = ""

    if store is not None and season_gp is not None and calendar is not None:
        periods = calendar.periods_in_matchup(matchup_period)
        is_first_day = bool(periods) and int(scoring_period) == periods[0]
        store.record_gp_snapshot(
            league_id=int(league_id),
            team_id=int(team_id),
            matchup_period=int(matchup_period),
            scoring_period=int(scoring_period),
            season_gp=int(season_gp),
            is_baseline=is_first_day,
        )
        snap = store.gp_snapshot(int(league_id), int(team_id), int(matchup_period))
        if snap is None:
            baseline_note = "no baseline snapshot for matchup period %d" % matchup_period
        elif not int(snap.get("is_baseline") or 0):
            baseline_note = (
                "baseline for matchup period %d was taken on scoring period %s, "
                "not the period's first day (%s); season-delta would under-count"
                % (matchup_period, snap.get("scoring_period"),
                   periods[0] if periods else "?")
            )
        else:
            delta = max(int(season_gp) - int(snap.get("season_gp") or 0), 0)
    elif season_gp is None:
        baseline_note = "ESPN reported no season games-played for this team"
    elif calendar is None:
        baseline_note = "no season calendar; cannot locate the week's first day"

    if direct is not None and delta is not None:
        if direct == delta:
            return GamesUsed(
                games=float(direct),
                source=SOURCE_BOTH,
                trustworthy=True,
                from_stat_by_slot=direct,
                from_season_delta=delta,
            )
        log.error(
            "games-used sources disagree for league %d team %d matchup period "
            "%d: statBySlot says %d, season-to-date delta says %d. Taking "
            "statBySlot, which is the counter the cap is enforced against; "
            "see the note in usage.py.",
            league_id, team_id, matchup_period, direct, delta,
        )
        return GamesUsed(
            games=float(direct),
            source=SOURCE_STAT_BY_SLOT,
            trustworthy=True,
            from_stat_by_slot=direct,
            from_season_delta=delta,
            note="sources disagreed; took statBySlot (the enforced counter)",
        )

    if direct is not None:
        return GamesUsed(
            games=float(direct),
            source=SOURCE_STAT_BY_SLOT,
            trustworthy=True,
            from_stat_by_slot=direct,
            note=baseline_note,
        )

    if delta is not None:
        return GamesUsed(
            games=float(delta),
            source=SOURCE_SEASON_DELTA,
            trustworthy=True,
            from_season_delta=delta,
            note="statBySlot was empty; reconstructed from season totals",
        )

    log.warning(
        "no usable games-used reading for league %d team %d matchup period %d "
        "(%s); the games-played cap cannot be priced this run",
        league_id, team_id, matchup_period, baseline_note or "both sources empty",
    )
    return GamesUsed(
        games=0.0,
        source=SOURCE_UNKNOWN,
        trustworthy=False,
        note=baseline_note or "both sources empty",
    )


def opportunities(
    *,
    periods: Sequence[int],
    schedule: Mapping[int, Mapping[int, int]],
    pro_team_id: int,
) -> int:
    """Games one player's NBA team plays over ``periods``.

    A thin re-export of ``solve.calendar.team_games_in`` so callers assembling
    the candidate list for :func:`solve.budget.shadow_price` do not have to
    reach into two modules for one idea.
    """
    from solve.calendar import team_games_in

    return team_games_in(periods, schedule, pro_team_id)
