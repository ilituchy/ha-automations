"""What one player-game is worth. The single branch between league types.

Both of the leagues this app was built for turned out to be ``H2H_POINTS``, not
``H2H_CATEGORY``. That does not change the optimizer -- the assignment in
``assign.py`` and the shadow price in ``budget.py`` are indifferent to where
values come from -- but it does change the value function, and the whole point
of this module is that it changes it in *one* place.

Points leagues
--------------
A matchup is won on a single scalar: total fantasy points. A player-game is
worth the dot product of his projected line against ``scoringItems``,

    v_p = sum_k c_k * mu_pk

with ``c_k`` read straight off the league settings. There is no z-score, no
category weight, no punting, because there is only one category. Both observed
leagues price, for instance, a steal at 2.0 and a turnover at -1.0, so the
coefficients are already the exchange rate and there is nothing to estimate.

This makes the objective **exactly linear** in the start/bench decisions, which
is stronger than the category case: there the weights depend on the projected
margin, so the linearization is only local and every accept/reject has to be
re-checked against the true ``sum_k Phi(z_k)``. Here the mean term needs no
such loop. See ``docs/ADR.md`` ADR-010.

Categories leagues
------------------
Unchanged, and deliberately kept: ``weights.py`` still holds the gradient
machinery. A future league of either type plugs in here without the caller
learning which it got.

Risk
----
A points league is the degenerate one-category league, so the variance logic in
``weights.py`` still applies verbatim -- trailing means seek variance, leading
means avoid it -- with the single "category" being fantasy points. That is
expressed by building one :class:`~solve.weights.CategoryState` over the
projected point totals rather than by writing a second risk model.

Conditional, not expected
-------------------------
Every value here is **conditional on the player suiting up**, i.e. it is *not*
multiplied by ``p_play``. That is not an oversight and it matters, because
``budget.py`` prices the games-played cap against exactly this quantity. See
the availability-cancellation note there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence

from espn import ids
from models import LeagueSettings, Projection
from solve import weights

StatId = int
PlayerId = int

log = logging.getLogger(__name__)

__all__ = [
    "ValueModel",
    "PointsValue",
    "CategoryValue",
    "build_value_model",
    "UnsupportedScoring",
]


class UnsupportedScoring(RuntimeError):
    """The league scores on something this module cannot value correctly.

    Raised rather than approximated. An unsupported scoring item does not make
    the optimizer a little bit wrong, it makes every player comparison wrong in
    a way that looks like a plausible ranking, which is the failure mode with
    the worst ratio of damage to detectability.
    """


@dataclass(frozen=True)
class PointsValue:
    """``v_p = sum_k c_k * mu_pk``, with ``c_k`` from ``scoringItems``."""

    coefficients: Mapping[StatId, float]
    #: Our projected point total for the matchup so far plus to-come, and the
    #: opponent's, when known. ``None`` disables the risk term -- which is the
    #: right default early in a matchup, when neither total is informative.
    state: Optional["weights.CategoryState"] = None

    def conditional_value(self, projection: Projection) -> float:
        """Value of this game *given the player plays*. Excludes ``p_play``."""
        mean = projection.conditional_mean()
        total = 0.0
        for stat, coeff in self.coefficients.items():
            total += coeff * mean.get(stat, 0.0)
        if self.state is not None:
            total += self._risk_term(projection)
        return total

    def _risk_term(self, projection: Projection) -> float:
        """Variance adjustment, signed by whether we are ahead or behind.

        The player's point *variance* is ``sum_k c_k^2 * var_pk`` under the
        same independence assumption the category path makes across stats. The
        coefficient is squared because variance scales with the square of a
        linear transform -- using ``c_k`` unsquared here would let a negative
        coefficient like a turnover's -1.0 subtract variance, which is not a
        thing that can happen.
        """
        var = projection.conditional_var()
        point_var = 0.0
        for stat, coeff in self.coefficients.items():
            point_var += coeff * coeff * var.get(stat, 0.0)
        risk = weights.risk_coefficients([self.state])
        return risk.get(self.state.stat_id, 0.0) * point_var


@dataclass(frozen=True)
class CategoryValue:
    """Thin adapter onto the existing gradient model in ``weights.py``."""

    states: Sequence["weights.CategoryState"]
    include_risk: bool = True

    def conditional_value(self, projection: Projection) -> float:
        return weights.player_value(
            projection.conditional_mean(),
            projection.conditional_var(),
            self.states,
            include_risk=self.include_risk,
        )


#: Either of the two above. A protocol in spirit; spelled as a union in the
#: docstring rather than a ``typing.Protocol`` to keep this importable on the
#: Python the app actually ships with.
ValueModel = object


#: Stat id the synthetic one-category state for a points league is keyed by.
#: Not a real ESPN stat -- fantasy points have no stat id, because in a points
#: league they are the score rather than a statistic. Negative so it can never
#: collide with a real id.
FANTASY_POINTS_PSEUDO_STAT: StatId = -1


def build_value_model(
    settings: LeagueSettings,
    *,
    my_points: Optional[float] = None,
    opp_points: Optional[float] = None,
    my_points_var: Optional[float] = None,
    opp_points_var: Optional[float] = None,
    category_states: Optional[Sequence["weights.CategoryState"]] = None,
):
    """Pick the value function for this league. The only place that branches.

    Args:
        settings: as parsed from ESPN. ``scoring_type`` decides, not a flag we
            set ourselves.
        my_points/opp_points: projected *final* matchup point totals, ours and
            theirs. Supply both to enable the variance term; supply neither
            early in a matchup, when the projection is mostly noise and the
            risk sign would flip on nothing.
        category_states: required for a categories league, ignored otherwise.

    Raises:
        UnsupportedScoring: if the league is neither type, or scores on a stat
            that cannot be summed across games.
    """
    if settings.is_points:
        coeffs = _checked_coefficients(settings)
        state = None
        if my_points is not None and opp_points is not None:
            state = weights.CategoryState(
                stat_id=FANTASY_POINTS_PSEUDO_STAT,
                my_mean=float(my_points),
                opp_mean=float(opp_points),
                my_var=float(my_points_var or 0.0),
                opp_var=float(opp_points_var or 0.0),
            )
        return PointsValue(coefficients=coeffs, state=state)

    if settings.is_categories:
        if not category_states:
            raise UnsupportedScoring(
                "league %d is category-scored but no category states were "
                "supplied; the weights are a function of the current margin "
                "and cannot be defaulted" % settings.league_id
            )
        return CategoryValue(states=tuple(category_states))

    raise UnsupportedScoring(
        "league %d reports scoring_type %r, which is neither points nor "
        "categories" % (settings.league_id, settings.scoring_type)
    )


def _checked_coefficients(settings: LeagueSettings) -> Dict[StatId, float]:
    """``scoringItems`` as a dot-product coefficient map, or refuse.

    The dot product is only valid for stats that add up across games. A league
    that awards points for FG% or for assists *per game* cannot be valued this
    way at all: summing a per-game average over a four-game week inflates it
    fourfold. Neither observed league does this -- all seventeen of their items
    are counting stats -- so the check costs nothing today and is the
    difference between a loud failure and a silently wrong ranking if a third
    league is configured differently.
    """
    bad = sorted(set(settings.scoring_items) & ids.NON_ADDITIVE_STATS)
    if bad:
        raise UnsupportedScoring(
            "league %d scores on non-additive stat(s) %s; a points value is a "
            "sum over games and these do not sum. Valuing them needs the "
            "ratio chain rule from weights.py, which is not wired into the "
            "points path."
            % (settings.league_id,
               ", ".join("%d (%s)" % (s, ids.stat_name(s)) for s in bad))
        )
    scored = {int(k) for k in settings.scoring_items}
    if ids.REB in scored and scored & {ids.OREB, ids.DREB}:
        raise UnsupportedScoring(
            "league %d scores both total rebounds and the offensive/defensive "
            "split. ESPN projects only the total, so "
            "solve.projections.derive_scored_stats fills in the components -- "
            "and a league pricing the total as well would be paid twice for "
            "the same rebound. Refused rather than guessed: which of the two "
            "the league means is not something this module can know."
            % settings.league_id
        )
    coeffs = {int(k): float(v) for k, v in settings.scoring_items.items()}
    if not coeffs:
        raise UnsupportedScoring(
            "league %d is points-scored but reports no scoringItems with "
            "point values" % settings.league_id
        )
    return coeffs
