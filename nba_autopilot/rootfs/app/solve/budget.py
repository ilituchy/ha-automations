"""The games-played cap, priced as a shadow cost.

The problem
-----------
ESPN leagues can cap how many games a team may *play* -- not start -- inside a
matchup period (``settings.rosterSettings.lineupSlotStatLimits``). With a cap,
starting a player today is not free: it consumes a unit of a scarce budget that
could have been spent on a better game later in the week. Every tool that
ignores this burns its cap by Thursday and fields a half-empty lineup on the
weekend, when the slates are biggest.

What the cap actually is
------------------------
This module was first written against the assumption that the cap is **per
lineup slot and season-long**. Two live leagues say it is neither. Both send a
single entry, keyed at slot 15 -- a slot that appears nowhere in their
``lineupSlotCounts`` -- and both send it divided by seven, along
``matchupLimitPerScoringPeriod: true``:

    10 starting slots -> limitValue 40/7   ->  40 games per matchup period
    11 starting slots -> limitValue 44/7   ->  44 games per matchup period

So it is one **pooled, team-wide, per-matchup-period** budget that renews each
week. The scaling with starter count is what rules out the per-slot reading: a
cap attached to one slot would not track another league's roster shape.

That is a strictly simpler problem than the per-slot season one, and a more
binding one. A deep roster of 24 with eleven starting slots has roughly 77
startable player-games in a week against a cap of 44, so the optimizer must
bench playable players on purpose most nights. ``utilization_ratio`` is what
makes that explicable instead of looking like a bug.

The reduction
-------------
Relax the cap into the objective with a single multiplier ``lambda``:

    L = sum_days sum_{p,s} (v_ps - lambda * c_p) x_ps + lambda * B

where ``c_p`` is the expected budget cost of starting p. Every coupling term
between days vanishes, so today's problem is exactly the single-day assignment
from ``assign.py`` run on reduced costs. The whole multi-period structure
collapses into one number, and the decision rule is

    start p  iff  v_p > lambda

``lambda`` is the opportunity cost of one game played -- the value of the
marginal game you give up later by playing one now.

Availability cancels out
------------------------
The cap counts games *played*, which is the one thing that makes this
interesting. A player who is benched by his NBA team consumes nothing. So with
``v_raw`` the value conditional on playing:

    benefit = p_play * v_raw        cost = lambda * p_play
    reduced cost = p_play * (v_raw - lambda)

``p_play`` factors out of the comparison entirely. **The gate is on the
conditional value, not the availability-weighted one.** A questionable star and
a healthy role player with the same expected line are therefore not the same
bet under a binding cap: the star is better, because he only spends a game in
the branch where he actually produces. This is why ``value.py`` returns
conditional values and why ``Projection.conditional_mean`` exists.

``p_play`` still scales the reduced cost, so it still orders the assignment --
it just does not move the threshold.

Estimating lambda
-----------------
At the optimum ``lambda`` is the ``B_rem``-th largest value among candidate
future games: spend the budget on the top ``B_rem`` opportunities and the
cutoff is the value of the last one taken. With fractional costs the
corresponding rule is to walk the candidates in descending value accumulating
``c_p`` until the budget is exhausted, which is what :func:`shadow_price` does.

A fixed threshold is not a lazy approximation, but the justification is weaker
here than it was under the season-long reading. For a k-unit prophet inequality
a static threshold is ``(1 - O(1/sqrt(k)))``-competitive; ``k`` is now the
*weekly* cap, around 40, rather than a season's 82. That is roughly a 16%
theoretical gap at worst, still inside projection error, and the cap renewing
weekly means a mistake is bounded to one matchup instead of compounding over a
season. Both boundary cases still fall out of the formula:

* slack budget (``rho >= 1``): ``lambda = 0``. Start everybody.
* exhausted (``remaining <= 0``): ``+inf``, so every reduced cost is negative
  and the assignment leaves the lineup empty.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from espn import ids
from models import LeagueSettings, StatLimit

SlotId = int
PlayerId = int

log = logging.getLogger(__name__)

__all__ = [
    "GamesBudget",
    "PooledCapUnsupported",
    "matchup_cap",
    "remaining_budget",
    "shadow_price",
    "utilization_ratio",
    "reduced_cost_weights",
]


class PooledCapUnsupported(RuntimeError):
    """The league's cap is not the pooled team-wide shape we can price.

    Raised rather than coerced. The two readings -- a team total of 40 versus a
    per-slot cap of 40 on one slot -- differ by an order of magnitude in how
    much they restrict, and guessing wrong in the permissive direction burns
    the real cap without ever noticing.
    """


@dataclass(frozen=True)
class GamesBudget:
    """The games-played budget for the current matchup period.

    ``cap`` and ``used`` are floats because ESPN's own value is fractional and
    because expected consumption is fractional under uncertain availability.
    Rounding either to an int here would quietly bias the budget by up to a
    full game, in a quantity where one game is 2.5% of the week.
    """

    cap: float
    used: float

    @property
    def remaining(self) -> float:
        return self.cap - self.used

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0


def matchup_cap(settings: LeagueSettings) -> Optional[float]:
    """Per-matchup-period games-played cap, or ``None`` if uncapped.

    Recovers the round number from ESPN's per-scoring-period fraction by
    multiplying by :data:`espn.ids.SCORING_PERIODS_PER_MATCHUP`.

    Raises:
        PooledCapUnsupported: if the limits are keyed at real lineup slots
            rather than at the team-total pseudo-slot, or if there is more than
            one entry. Neither has been observed; both would mean the cap is
            per-slot, which this module no longer models.
    """
    limits: Optional[Mapping[SlotId, StatLimit]] = settings.lineup_slot_stat_limits
    if not limits:
        return None

    unexpected = [s for s in limits if s != ids.TEAM_TOTAL_LIMIT_SLOT]
    if unexpected:
        raise PooledCapUnsupported(
            "league %d keys lineupSlotStatLimits at slot(s) %s; only the "
            "team-total pseudo-slot %d is modelled, and a per-slot cap needs "
            "a multiplier per slot rather than one pooled lambda"
            % (settings.league_id,
               ", ".join("%d (%s)" % (s, ids.slot_name(s)) for s in unexpected),
               ids.TEAM_TOTAL_LIMIT_SLOT)
        )

    limit = limits[ids.TEAM_TOTAL_LIMIT_SLOT]
    if limit.stat_id != ids.GP_STAT:
        raise PooledCapUnsupported(
            "league %d caps stat %d (%s), not games played; this module only "
            "knows how to price a games-played budget"
            % (settings.league_id, limit.stat_id, ids.stat_name(limit.stat_id))
        )
    return limit.limit_value * ids.SCORING_PERIODS_PER_MATCHUP


def remaining_budget(
    settings: LeagueSettings,
    games_used_this_matchup: float,
) -> Optional[GamesBudget]:
    """The budget, or ``None`` when the league has no cap.

    ``None`` propagates: no cap means :func:`shadow_price` returns zero and the
    optimizer reduces to plain daily maximization. This is why
    ``LeagueSettings.lineup_slot_stat_limits`` is ``Optional`` rather than
    defaulting to an empty dict -- "no cap" and "a cap of zero" must not be the
    same value anywhere in this codebase.

    Logs loudly if usage has already passed the cap. That is the cheap
    self-falsifying check on the per-matchup reading: if the cap were in fact
    season-long, observed usage would sail past the weekly number in week two
    and keep going, and this is where that shows up.
    """
    cap = matchup_cap(settings)
    if cap is None:
        return None
    used = float(games_used_this_matchup)
    if used > cap:
        log.error(
            "league %d has played %.1f games this matchup period against a "
            "cap read as %.1f. Either the cap is not per-matchup-period (see "
            "the note in solve/budget.py) or games_used is counting the whole "
            "season. The optimizer will treat the budget as exhausted.",
            settings.league_id, used, cap,
        )
    return GamesBudget(cap=cap, used=used)


def shadow_price(
    candidates: Sequence[Tuple[float, float]],
    budget: Optional[GamesBudget],
) -> float:
    """Price one game played, for the rest of this matchup period.

    Args:
        candidates: ``(conditional_value, expected_cost)`` for every
            (player, game-day) pair still startable this matchup period, today
            included. ``expected_cost`` is the player's ``p_play`` -- the
            expected games the start consumes. Order is irrelevant; the
            function sorts by value.

            Values must be **conditional on playing**, matching
            ``value.py``. Passing availability-weighted values here would
            double-discount and lower the threshold, which errs toward
            over-spending the cap.
        budget: from :func:`remaining_budget`. ``None`` means uncapped.

    Returns:
        ``lambda``. ``0.0`` when the budget is slack, ``+inf`` when exhausted.
    """
    if budget is None:
        return 0.0
    if budget.exhausted:
        return float("inf")

    ordered = sorted(candidates, key=lambda vc: vc[0], reverse=True)
    spent = 0.0
    for value, cost in ordered:
        spent += max(float(cost), 0.0)
        if spent >= budget.remaining:
            # The marginal game -- the last one the budget reaches. Its value
            # is what we forgo by spending a unit here instead of there.
            return float(value)
    # The budget outlasts every opportunity we have: nothing is scarce.
    return 0.0


def utilization_ratio(
    candidates: Sequence[Tuple[float, float]],
    budget: Optional[GamesBudget],
) -> float:
    """``rho = remaining / total expected cost of every candidate``.

    Diagnostic, surfaced in the run record. ``rho >= 1`` means the cap is not
    binding. Values well below 1 are the interesting ones: that is where the
    optimizer starts benching playable players on purpose, and this number is
    what makes that explicable to a human rather than looking like a bug. Both
    observed leagues sit under 1 -- the 24-player roster materially so.
    """
    if budget is None:
        return float("inf")
    opportunities = sum(max(float(c), 0.0) for _, c in candidates)
    if opportunities <= 0.0:
        return float("inf")
    return max(budget.remaining, 0.0) / opportunities


def reduced_cost_weights(
    values: Mapping[PlayerId, float],
    eligibility: Mapping[PlayerId, Iterable[SlotId]],
    lam: float,
    costs: Optional[Mapping[PlayerId, float]] = None,
    *,
    keep_nonpositive: bool = False,
) -> Dict[Tuple[PlayerId, SlotId], float]:
    """Build the ``assign.py`` weight mapping from per-player values.

    Args:
        values: ``player -> conditional value`` for today. A player with no
            game today must be **absent** from this mapping, not present with
            0.0 -- the assignment is allowed to leave slots empty, and a
            present-but-zero player is a claim that he could fill one.
        eligibility: ``player -> slots he may occupy``, straight from ESPN's
            ``eligibleSlots``. Never derived from a position map.
        lam: the shadow price from :func:`shadow_price`.
        costs: ``player -> p_play``. Defaults to 1.0, i.e. certain to play.
        keep_nonpositive: include pairs whose reduced cost is <= 0. Normally
            False: dropping them shrinks the matrix and, because benching is
            free, cannot change the optimum. Set True in tests to confirm that
            invariant rather than assume it.

    Returns:
        ``(player, slot) -> p_play * (v - lambda)``. The threshold is on ``v``
        alone, per the availability-cancellation note above, while ``p_play``
        scales the magnitude so the assignment still prefers the player more
        likely to actually play among two that clear the bar.

        Every pair is dropped when ``lambda`` is infinite, rather than carried
        as ``-inf``, since the assignment routine requires finite arithmetic.
    """
    out: Dict[Tuple[PlayerId, SlotId], float] = {}
    if lam == float("inf"):
        return out
    for player, v in values.items():
        margin = float(v) - lam
        cost = 1.0 if costs is None else max(float(costs.get(player, 1.0)), 0.0)
        rc = cost * margin
        if rc <= 0.0 and not keep_nonpositive:
            continue
        for slot in eligibility.get(player, ()):
            out[(player, slot)] = rc
    return out
