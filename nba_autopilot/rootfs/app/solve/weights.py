"""Category weights as the gradient of win probability.

What we are actually maximizing
-------------------------------
In a head-to-head categories league the objective is not "total stats". It is

    J = sum_k P(win category k) = sum_k Phi(z_k),    z_k = Delta_k / s_k

where ``Delta_k`` is our projected final margin in category k and ``s_k`` the
standard deviation of that margin. Points-league scoring is the degenerate case
where the weights are constants read off ``scoringItems``.

Differentiating gives the marginal value of one more unit of category k:

    w_k = dJ/dmu_k = phi(z_k) / s_k

and a player's value is the inner product ``v_hat_p = sum_k w_k * mu_pk``.

Why this is the whole algorithm
-------------------------------
``phi`` is the standard normal density, so ``w_k`` collapses toward zero as a
category becomes decided in *either* direction. Numerically, with ``s_k = 1``:

    z = -3  ->  w = 0.000222      (lost; nothing you add will win it)
    z =  0  ->  w = 0.019947      (coin flip; every unit moves the needle)
    z = +3  ->  w = 0.000222      (won; nothing you add is needed)

A tossup category is worth about **90x** a decided one at the margin.

Two behaviours that fantasy tools hand-code as special cases are emergent here
and must stay emergent:

* **Punting.** A category you are structurally far behind gets a near-zero
  weight automatically, so the optimizer stops spending roster value on it. No
  "punt list" config, no flag.
* **Clinching.** A category already won also gets a near-zero weight, so effort
  moves to contested ones.

If you ever feel the urge to add an ``if punting:`` branch, the gradient is
already doing it, and the branch will fight it. Adding one is a bug.

Ratio categories
----------------
FG% and FT% are not additive, so they do not fit ``sum_k w_k mu_pk`` directly.
Chain rule through the ratio ``R = M / A``:

    dJ/dx_p |_k = w_k * a_p * (r_p - R) / A
                = w_k * (made_p - R * att_p) / A

A player helps your percentage iff his own rate beats the team's, scaled by his
attempt volume. That is exactly Z-score's "impact / volume weighting" -- except
derived rather than asserted, which means it stays correct when the team rate
moves instead of being calibrated to a league-average assumption.

Note the variance subtlety: ``M`` and ``A`` are strongly positively correlated
(a player who shoots more makes more). Computing ``Var(M/A)`` by the delta
method while ignoring ``Cov(M, A)`` badly *overstates* ratio variance -- and
``s_k`` sits in the **denominator** of ``w_k``, so the error does not stay local:
it silently deflates the weight on both percentage categories relative to the
counting ones. See :func:`ratio_margin_sd`.

Risk
----
Differentiating with respect to the margin variance:

    dP_k/d(sigma_k^2) = -(Delta_k / (2 s_k^2)) * w_k

The sign is the negative of ``sign(Delta_k)``: **trailing in a category means
you should seek variance, leading means you should avoid it.** Down 3 blocks on
the last day, the high-variance big man is the correct start even though his
mean is lower. This gives a variance-aware value

    v_hat_p = sum_k [ w_k * mu_pk - (Delta_k / (2 s_k^2)) * w_k * sigma^2_pk ]

which is one extra term, not a different algorithm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence

StatId = int
PlayerId = int

__all__ = [
    "CategoryState",
    "RatioSpec",
    "normal_pdf",
    "normal_cdf",
    "category_weights",
    "win_probability",
    "player_value",
    "ratio_contribution",
    "ratio_margin_sd",
    "points_league_weights",
]

#: Floor on the margin SD. Prevents a division blowup when a category's
#: projected variance collapses (e.g. one game left, both sides locked), which
#: would otherwise produce an effectively infinite weight on a category that is
#: in fact already decided.
MIN_MARGIN_SD = 1e-6

#: Typical correlation between a player's makes and attempts within a game.
#: Used by the delta method for ratio-category variance. High by construction:
#: makes are a binomial draw *from* attempts. Treat as a tunable prior, not a
#: measured constant -- it is documented here so the assumption is visible
#: rather than buried.
MADE_ATT_CORRELATION = 0.9


def normal_pdf(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass(frozen=True)
class RatioSpec:
    """Describes a percentage category in terms of its components."""

    made_stat: StatId
    att_stat: StatId
    #: Projected *team* season/matchup totals, ours.
    team_made: float
    team_att: float

    @property
    def team_rate(self) -> float:
        return self.team_made / self.team_att if self.team_att > 0 else 0.0


@dataclass(frozen=True)
class CategoryState:
    """Our projected standing in one category for the current matchup.

    ``my_mean``/``opp_mean`` are *projected final totals* for the matchup --
    games already played plus everything still to come. Using only remaining
    games is a common and severe error: it discards the lead or deficit that
    determines the weight.
    """

    stat_id: StatId
    my_mean: float
    opp_mean: float
    my_var: float
    opp_var: float
    #: Turnovers. Fewer is better, so the sign of the gradient flips.
    lower_is_better: bool = False
    ratio: Optional[RatioSpec] = None

    @property
    def margin(self) -> float:
        """``Delta_k``, signed so that positive always means "winning"."""
        d = self.my_mean - self.opp_mean
        return -d if self.lower_is_better else d

    @property
    def margin_sd(self) -> float:
        """``s_k``. Assumes our total and theirs are independent, which they
        are: different players, different games."""
        return max(math.sqrt(max(self.my_var + self.opp_var, 0.0)), MIN_MARGIN_SD)

    @property
    def z(self) -> float:
        return self.margin / self.margin_sd


def win_probability(states: Sequence[CategoryState]) -> float:
    """``J = sum_k Phi(z_k)`` -- expected categories won.

    This is the *true* objective. The linearization in
    :func:`category_weights` is only a local model of it, so every accept/reject
    decision in the outer optimization loop must be made on this function and
    never on the linearized score. Accepting on the linearization lets the
    search cycle between two lineups forever, each looking better than the other
    under its own expansion point.
    """
    return sum(normal_cdf(s.z) for s in states)


def category_weights(states: Sequence[CategoryState]) -> Dict[StatId, float]:
    """``stat_id -> w_k``, with the lower-is-better sign already folded in.

    Signed so that ``player_value`` is always a plain sum: a turnover-heavy
    player gets a negative contribution without the caller special-casing him.
    """
    out: Dict[StatId, float] = {}
    for s in states:
        w = normal_pdf(s.z) / s.margin_sd
        out[s.stat_id] = -w if s.lower_is_better else w
    return out


def risk_coefficients(states: Sequence[CategoryState]) -> Dict[StatId, float]:
    """``stat_id -> -(Delta_k / (2 s_k^2)) * |w_k|``.

    Multiply by a player's per-category variance and add. Positive when we are
    behind (variance is good), negative when ahead (variance is bad).
    """
    out: Dict[StatId, float] = {}
    for s in states:
        w = normal_pdf(s.z) / s.margin_sd
        out[s.stat_id] = -(s.margin / (2.0 * s.margin_sd * s.margin_sd)) * w
    return out


def ratio_margin_sd(spec: RatioSpec, made_var: float, att_var: float) -> float:
    """Delta-method SD of ``M / A``, including the makes/attempts covariance.

    ``Var(M/A) ~= (1/A^2) Var(M) + (M^2/A^4) Var(A) - 2 (M/A^3) Cov(M, A)``

    The third term is the one that is usually dropped, and dropping it inflates
    the result -- often by a factor of several, since the covariance is large
    and positive. Because ``s_k`` divides ``w_k``, an inflated ratio SD quietly
    tells the optimizer that FG% and FT% barely matter.
    """
    m, a = spec.team_made, spec.team_att
    if a <= 0:
        return MIN_MARGIN_SD
    cov = MADE_ATT_CORRELATION * math.sqrt(max(made_var, 0.0) * max(att_var, 0.0))
    var = (
        made_var / (a * a)
        + (m * m * att_var) / (a ** 4)
        - 2.0 * (m / (a ** 3)) * cov
    )
    return max(math.sqrt(max(var, 0.0)), MIN_MARGIN_SD)


def ratio_contribution(
    spec: RatioSpec,
    weight: float,
    player_made: float,
    player_att: float,
) -> float:
    """Marginal value of starting this player, for one ratio category.

    ``w_k * (made_p - R * att_p) / A``. Note there is no division by
    ``player_att``, so a zero-attempt player contributes exactly zero rather
    than producing a NaN -- which is the right answer and also the safe one.
    """
    if spec.team_att <= 0:
        return 0.0
    return weight * (player_made - spec.team_rate * player_att) / spec.team_att


def player_value(
    mean: Mapping[StatId, float],
    variance: Mapping[StatId, float],
    states: Sequence[CategoryState],
    *,
    include_risk: bool = True,
) -> float:
    """``v_hat_p`` -- one player's marginal contribution to expected cats won.

    Counting categories use ``w_k * mu_pk``; ratio categories route through
    :func:`ratio_contribution` using their component made/attempt stats.
    """
    w = category_weights(states)
    risk = risk_coefficients(states) if include_risk else {}
    total = 0.0
    for s in states:
        wk = w[s.stat_id]
        if s.ratio is not None:
            total += ratio_contribution(
                s.ratio,
                wk,
                mean.get(s.ratio.made_stat, 0.0),
                mean.get(s.ratio.att_stat, 0.0),
            )
            # Risk on a ratio category is second-order through two correlated
            # components; deliberately omitted rather than approximated badly.
            continue
        total += wk * mean.get(s.stat_id, 0.0)
        if include_risk:
            total += risk.get(s.stat_id, 0.0) * variance.get(s.stat_id, 0.0)
    return total


def points_league_weights(scoring_items: Mapping[StatId, float]) -> Dict[StatId, float]:
    """Points leagues: the weights are just the scoring coefficients.

    Kept as a named function so the call site reads the same in both league
    types and the branch happens once, at configuration time.
    """
    return {int(k): float(v) for k, v in scoring_items.items()}
