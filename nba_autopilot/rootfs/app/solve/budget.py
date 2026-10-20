"""The games-played cap, priced as a shadow cost.

The problem
-----------
ESPN leagues can cap games started per position per season
(``settings.rosterSettings.lineupSlotStatLimits``). With a cap, starting a
player today is not free: it consumes a unit of a scarce, non-renewable budget
that could have been spent on a better game later. Every tool that ignores this
burns its cap in December and fields a half-empty lineup in March.

Naively this is a multi-period stochastic integer program. It isn't, though.

The reduction
-------------
Relax the season-long cap constraints into the objective with multipliers
``lambda_s``:

    L = sum_days sum_{p,s} (v_ps - lambda_s) x_ps + sum_s lambda_s * B_s

Every coupling term between days has vanished. The Lagrangian **decouples
across days**, so today's problem is exactly the single-day assignment from
``assign.py`` run on *reduced costs* ``v_ps - lambda_s``. The entire multi-period
structure collapses into one number per slot, and the decision rule is:

    start p in slot s  iff  v_hat_p > lambda_s

``lambda_s`` is the opportunity cost of one start in slot s -- the value of the
marginal game you are giving up later by playing one now.

Estimating lambda
-----------------
At the optimum, ``lambda_s`` is the ``B_s^rem``-th largest value among all
candidate future starts for that slot: spend the budget on the top ``B_s^rem``
opportunities, and the cutoff is the value of the last one you take. This is the
reservation value from the order statistics of the remaining schedule.

This static threshold is not a lazy approximation. For a k-unit prophet
inequality a fixed threshold is ``(1 - O(1/sqrt(k)))``-competitive, and here
``k`` is the per-position season cap -- on the order of 82. At that scale a
static reservation value is within a couple of percent of the optimal adaptive
policy, which is comfortably inside our projection error. Solving the full
stochastic MILP would be optimizing a quantity we cannot measure that precisely.

Two boundary cases matter and both fall out of the formula:

* ``remaining >= opportunities`` (slack budget, ``rho >= 1``): ``lambda_s = 0``.
  Start everybody; there is nothing to conserve.
* ``remaining <= 0``: the slot is exhausted. Expressed as ``+inf``, which makes
  every reduced cost negative, which makes the assignment leave the slot empty.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SlotId = int

__all__ = [
    "shadow_prices",
    "remaining_budget",
    "reduced_cost_weights",
    "utilization_ratio",
]


def remaining_budget(
    limits: Optional[Mapping[SlotId, int]],
    used: Mapping[SlotId, int],
) -> Optional[Dict[SlotId, int]]:
    """Remaining starts per slot, or ``None`` when the league has no cap.

    ``None`` propagates: no cap means ``shadow_prices`` returns all zeros and
    the optimizer reduces to plain daily maximization. This is why
    ``LeagueSettings.lineup_slot_stat_limits`` is ``Optional`` rather than
    defaulting to an empty dict -- "no cap" and "a cap of zero" must not be the
    same value anywhere in this codebase.
    """
    if not limits:
        return None
    return {s: int(cap) - int(used.get(s, 0)) for s, cap in limits.items()}


def shadow_prices(
    candidates: Mapping[SlotId, Sequence[float]],
    remaining: Optional[Mapping[SlotId, int]],
) -> Dict[SlotId, float]:
    """Price one start in each slot.

    Args:
        candidates: ``slot -> values of every candidate future start`` for the
            rest of the horizon, today included. One entry per
            (player, game-day) pair that *could* fill that slot. Order is
            irrelevant; the function sorts.
        remaining: ``slot -> starts left``, from :func:`remaining_budget`.
            ``None`` means the league is uncapped.

    Returns:
        ``slot -> lambda_s``. ``0.0`` when budget is slack, ``+inf`` when the
        budget is exhausted.
    """
    if remaining is None:
        return {s: 0.0 for s in candidates}

    out: Dict[SlotId, float] = {}
    for slot, budget in remaining.items():
        vals = sorted(candidates.get(slot, ()), reverse=True)
        if budget <= 0:
            out[slot] = float("inf")
        elif budget >= len(vals):
            # More budget than opportunities: nothing is scarce.
            out[slot] = 0.0
        else:
            # The value of the marginal start we would forgo.
            out[slot] = float(vals[budget - 1])
    # Slots with candidates but no configured limit are uncapped.
    for slot in candidates:
        out.setdefault(slot, 0.0)
    return out


def utilization_ratio(
    candidates: Mapping[SlotId, Sequence[float]],
    remaining: Optional[Mapping[SlotId, int]],
) -> Dict[SlotId, float]:
    """``rho_s = budget / opportunities``. Diagnostic, surfaced in the run record.

    ``rho >= 1`` means the cap is not binding for that slot. Values well below 1
    are the interesting ones: that is where the optimizer starts benching
    playable players on purpose, and the number is what makes that explicable
    to a human rather than looking like a bug.
    """
    if remaining is None:
        return {s: float("inf") for s in candidates}
    out: Dict[SlotId, float] = {}
    for slot in set(candidates) | set(remaining):
        opps = len(candidates.get(slot, ()))
        budget = remaining.get(slot)
        if budget is None:
            out[slot] = float("inf")
        elif opps == 0:
            out[slot] = float("inf")
        else:
            out[slot] = float(budget) / float(opps)
    return out


def reduced_cost_weights(
    values: Mapping[int, float],
    eligibility: Mapping[int, Iterable[SlotId]],
    lam: Mapping[SlotId, float],
    *,
    keep_nonpositive: bool = False,
) -> Dict[Tuple[int, SlotId], float]:
    """Build the ``assign.py`` weight mapping from per-player values.

    Args:
        values: ``player -> v_hat`` for today. A player with no game today must
            be **absent** from this mapping, not present with 0.0.
        eligibility: ``player -> slots he may occupy``, straight from ESPN's
            ``eligibleSlots``. We never derive this from a position map.
        lam: shadow prices from :func:`shadow_prices`.
        keep_nonpositive: include pairs whose reduced cost is <= 0. Normally
            False: dropping them shrinks the matrix and, because benching is
            free, cannot change the optimum. Set True in tests to confirm that
            invariant rather than assume it.

    Returns:
        ``(player, slot) -> v_hat - lambda_s``, suitable for
        :func:`~solve.assign.max_weight_assignment`. Pairs for exhausted slots
        (``lambda = inf``) are dropped entirely rather than carried as ``-inf``,
        since the assignment routine requires finite arithmetic.
    """
    out: Dict[Tuple[int, SlotId], float] = {}
    for player, v in values.items():
        for slot in eligibility.get(player, ()):
            price = lam.get(slot, 0.0)
            if price == float("inf"):
                continue
            rc = float(v) - price
            if rc > 0.0 or keep_nonpositive:
                out[(player, slot)] = rc
    return out
