"""Turn ESPN stat splits + availability into :class:`models.Projection`.

This is the layer between "what the sources said" and "what the optimizer
consumes". It exists as its own module because it is where every scale and
provenance mistake lives, and those mistakes are invisible downstream: a lineup
built from a mis-scaled projection looks exactly like a lineup built from a good
one.

Which split
-----------
ESPN's stat splits are keyed ``f"{statSourceId}{statSplitTypeId}"``. Probed
against both live leagues on 2026-10-20, opening day of the 2026-27 season:

    "10"  season projection   30-31 stats, per-game averages      <-- used
    "00"  season actual       45 stats, or EMPTY
    "01"  last 7              45 stats, or EMPTY
    "02"  last 15             45 stats, or EMPTY
    "03"  last 30             45 stats, or EMPTY
    "05"  undocumented        45 stats, same values as 00-03

Of three sampled players in one league, one had played a single six-minute game
and two had empty actuals. **In October the recent-form splits carry no signal
at all**, and blending them in would be blending in noise with a confident face.
So the projection is ESPN's own, unblended, until there is history to blend --
see :data:`MIN_GAMES_FOR_FORM`.

``statSplitTypeId`` 05 is left unmapped on purpose. It duplicated 00-03 in every
sample, and guessing at an identifier we have never seen documented is how you
end up averaging a stat into itself.

Stats ESPN scores and does not project
--------------------------------------
The split above is not in the same stat space as the league's scoring, and the
mismatch cost real points before it was found. Both live leagues score

* ``OREB`` (4) at 1.5 and ``DREB`` (5) at 1.0, with **no coefficient on total
  ``REB``** -- and split ``10`` publishes only the total;
* ``DD``/``TD``/``QD`` (37/38/39) at 4.0/8.0/15.0 -- and split ``10`` publishes
  none of the three for any player on either roster.

``PointsValue.conditional_value`` sums ``coeff * mean.get(stat, 0.0)`` over the
scored stats, so both gaps resolved to zero silently. Every rebound in both
leagues was worth nothing. Replaying week 1 of 2026-27 against the actual box
scores: Walker Kessler, 10.8 projected rebounds a game, was priced at 19.94
fantasy points and scored 41.3; the best legal lineup each day would have
scored 9% more than what was started, and the same three names -- Kessler,
Donovan Clingan, Zach Edey -- kept turning up on the bench behind guards.

The bug was never in the value model, which was correctly summing what it was
given. It was that nothing bridged the two stat spaces. :func:`derive_scored_stats`
is that bridge, and it lives here because this is the module whose whole job is
turning "what the sources said" into "what the optimizer consumes".

Mean residual against week 1 was near zero overall in both leagues and strongly
positive for rebounders -- +10.4 fantasy points a game for players above seven
rebounds against -1.7 for the rest, in the league where it was worst. A bias
that is uniform cancels out of a ranking; a bias that tracks position is the
one kind a lineup optimizer cannot absorb.

Per-game, except where it is not
--------------------------------
Split ``10`` is per-game averages -- ``PTS`` equals ``PPG``, ``MIN`` equals
``MPG`` -- with **two exceptions sitting in the same dictionary**: ``GP`` (72.0
for Luka Doncic) and nothing else. ``GP`` is a season total. Leaving it in the
mean would let a league that scores games-played multiply 72 by a coefficient,
and more importantly it is a scale mismatch hiding in plain sight. It is pulled
out and reported separately as a durability signal, which is a better use for it
anyway.

The non-additive stats (percentages, per-game averages, ratios) are dropped for
the same reason: ``solve/value.py`` refuses a league that *scores* them, but
they should not be in a mean that anything sums over either.

Variance
--------
ESPN publishes no variance, so it is modelled: ``Var = PHI * mu`` per counting
stat, i.e. Poisson with overdispersion. ``PHI > 1`` because box-score counts are
more variable than Poisson -- minutes themselves fluctuate, which mixes the rate.

This is the weakest number in the module and it is deliberately quarantined:
variance only reaches a decision through the risk term in ``value.py``, which is
inert unless a matchup score is supplied. Today nothing supplies one, so ``PHI``
currently affects nothing. When it does start to matter, it should be fit
against observed game logs rather than left at a guess.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Dict,
    FrozenSet,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from espn import ids
from solve import weights
from models import P_PLAY_PRIOR, Projection, RosterEntry

PlayerId = int
StatId = int

log = logging.getLogger(__name__)

__all__ = [
    "PHI",
    "MIN_GAMES_FOR_FORM",
    "NBA_SEASON_GAMES",
    "AvailabilityNote",
    "OREB_SHARE_DEFAULT",
    "BONUS_PRIOR_GAMES",
    "DOUBLE_DIGIT_STATS",
    "DOUBLE_DIGIT",
    "BONUS_STATS",
    "offensive_rebound_share",
    "modelled_bonus_rates",
    "observed_bonus_rates",
    "derive_scored_stats",
    "per_game_mean",
    "recent_form",
    "form_weight",
    "variance_from_mean",
    "p_play_for",
    "build_projections",
    "apply_matchup",
    "UNSCALED_BY_MATCHUP",
]

#: Overdispersion factor in ``Var = PHI * mu``. See the module docstring --
#: this is a modelling assumption, not a measurement.
PHI: float = 1.6

#: Games of real history before the recent-form splits are worth consulting.
#: Until then :func:`per_game_mean` uses ESPN's projection alone. Chosen so that
#: a single blowout or a six-minute injury return cannot move a projection.
MIN_GAMES_FOR_FORM: int = 10

#: Denominator for turning ESPN's projected season games into a rate.
NBA_SEASON_GAMES: int = 82

#: How much weight the durability prior gets against the injury-status prior.
#: At 0.5, ESPN projecting 72 of 82 games pulls an undesignated player from
#: 0.98 down to about 0.92.
#:
#: Not 1.0, because a player who is going to miss a game usually carries a
#: designation on the day, and we would then be counting the same absence twice.
#: Not 0.0, because our designation feed is thin: ESPN's fantasy feed publishes
#: no QUESTIONABLE tier, DARKO's public snapshot is stale, and the official
#: injury report 403s until it is published. The durability prior is the hedge
#: against that thinness.
#:
#: ``sources/espn_injuries.py`` (2026-10-21) narrowed that gap: it recovers a
#: real probable/questionable/doubtful split from the beat-report sentence, so
#: the designation feed is no longer as thin as this weight was chosen for.
#: The weight is left where it is until there is *measured* evidence to move
#: it -- it only ever applies to a player with no designation at all, which is
#: exactly the case the injuries page says nothing about.
DURABILITY_WEIGHT: float = 0.5

#: Share of a player's rebounds that are offensive, when he has no history to
#: read it off. Roughly the league-wide split.
#:
#: The fallback is low-stakes in a way worth stating, because it looks like a
#: guess that matters and is not one. With both observed leagues pricing OREB
#: at 1.5 and DREB at 1.0, a rebound is worth 1.125 at a 25% share and 1.175 at
#: 35% -- so even a share that is wrong by a third of its own value moves a
#: ten-rebound big man by half a point. What *does* matter is that the rebound
#: is priced at all; see :func:`derive_scored_stats`.
OREB_SHARE_DEFAULT: float = 0.25

#: Pseudo-games of the modelled bonus rate that an observed rate must outweigh.
#:
#: Shrinkage weight is ``games / (games + BONUS_PRIOR_GAMES)``, so three games
#: of history counts for 23% and twenty games for 67%. Deliberately a gentler
#: curve than :data:`MIN_GAMES_FOR_FORM`, which gates form out entirely below
#: ten games: a double-double count is a *direct* observation of the quantity
#: being estimated rather than a noisy proxy for it, so it should start
#: contributing immediately. It still must not take over early, because a
#: three-game zero is perfectly ordinary for a player who will average 0.4.
BONUS_PRIOR_GAMES: float = 10.0

#: The five stats a double-double is counted over.
DOUBLE_DIGIT_STATS: Tuple[StatId, ...] = (
    ids.PTS, ids.REB, ids.AST, ids.STL, ids.BLK,
)

#: What counts as "double" in double-double.
DOUBLE_DIGIT: float = 10.0

#: ``how many categories in double digits -> the stat ESPN credits``.
#:
#: Treated as **mutually exclusive**: a triple-double is credited as a TD and
#: not also as a DD. ESPN's convention here is not documented and could not be
#: read off either live league in October -- no rostered player had recorded a
#: triple-double yet, so ``DD`` and ``TD`` were both zero and the two
#: conventions are indistinguishable from that.
#:
#: Exclusive is the assumption that errs small: if ESPN in fact credits both,
#: this under-prices a near-triple-double player by the 4.0 DD coefficient on
#: the fraction of games he gets there, which for the handful of players with a
#: non-trivial TD rate is well under a point a game. The reverse assumption
#: would over-price him, and an over-priced player gets *started*, which is the
#: error that costs a lineup slot rather than merely failing to gain one.
#:
#: Checkable as soon as there is history: a player with ``TD > 0`` whose ``DD``
#: is at least as large settles it. The ``forecasts`` table exists so that
#: this kind of question stops needing a guess.
BONUS_STATS: Mapping[int, StatId] = {2: ids.DD, 3: ids.TD, 4: ids.QD}

#: Stats to exclude from the production mean. The non-additive set plus the two
#: that are not per-game quantities at all.
_EXCLUDED: frozenset = ids.NON_ADDITIVE_STATS | {ids.GP_STAT, ids.GAMES_STARTED}


@dataclass(frozen=True)
class AvailabilityNote:
    """Why a player's ``p_play`` came out where it did. Goes in the rationale.

    Kept as data rather than a formatted string so the status page and the
    notification can render it differently, and so a test can assert on the
    inputs instead of on prose.
    """

    player_id: PlayerId
    status: str
    status_source: str
    p_play: float
    projected_season_games: Optional[float] = None

    def describe(self) -> str:
        bits = ["%s (%s)" % (self.status, self.status_source)]
        if self.projected_season_games is not None:
            bits.append("ESPN projects %.0f/%d games"
                        % (self.projected_season_games, NBA_SEASON_GAMES))
        return "p_play %.2f: %s" % (self.p_play, ", ".join(bits))


def offensive_rebound_share(
    splits: Mapping[str, Mapping[StatId, float]],
) -> float:
    """What fraction of this player's rebounds are offensive.

    Read off his own actuals when he has any, because the spread across
    positions is wide -- a rim-running centre is near 35% and a point guard
    near 12% -- and that spread is the whole reason not to apply one number to
    everybody. Falls back to :data:`OREB_SHARE_DEFAULT`.
    """
    actual = splits.get(ids.SPLIT_SEASON_TOTAL) or {}
    off = float(actual.get(ids.OREB, 0.0) or 0.0)
    deff = float(actual.get(ids.DREB, 0.0) or 0.0)
    if off + deff <= 0.0:
        return OREB_SHARE_DEFAULT
    return max(min(off / (off + deff), 1.0), 0.0)


def _p_double_digits(mu: float) -> float:
    """``P(X >= 10)`` for a counting stat with mean ``mu``.

    Normal approximation with a continuity correction, variance ``PHI * mu`` --
    the same overdispersion the rest of the module assumes, so there is one
    variance model here and not two that can disagree.

    The approximation is poor in the tails of a small-mean count, and that is
    tolerable because of *where* it is consulted. A stat whose mean is far from
    ten contributes a probability near zero or near one, and the approximation
    gets both of those right for the same reason it gets the middle roughly
    right. The only players whose answer this materially decides are the ones
    averaging close to ten, where a normal is a reasonable shape.
    """
    if mu <= 0.0:
        return 0.0
    sd = math.sqrt(PHI * mu)
    if sd <= 0.0:
        return 1.0 if mu >= DOUBLE_DIGIT else 0.0
    return 1.0 - weights.normal_cdf(((DOUBLE_DIGIT - 0.5) - mu) / sd)


def modelled_bonus_rates(mean: Mapping[StatId, float]) -> Dict[StatId, float]:
    """Double/triple/quadruple-double rates implied by a projected line.

    ESPN prices these -- 4.0, 8.0 and 15.0 in both observed leagues -- and does
    not project them: split ``10`` carries no stat 37, 38 or 39 for any player
    on either roster. So the rate has to be modelled, and this is the model:
    treat the five counting stats as independent, get each one's chance of
    reaching double digits, and read off the distribution of how many clear it.

    The counters are **cumulative**: a triple-double is also a double-double
    and ESPN pays both, so the rate for each is the chance of clearing *at
    least* that many categories, not exactly that many.

    Independence is wrong, and wrong in a known direction: points and rebounds
    are positively correlated through minutes, so the true chance of *both*
    clearing ten is higher than the product. This therefore under-states the
    bonus for exactly the players who earn it most. It is used anyway because
    the alternative on the table was leaving the term at zero, which
    under-states them by the whole coefficient, and because the observed rate
    takes over as history accumulates -- see :func:`derive_scored_stats`.
    """
    ps = [_p_double_digits(float(mean.get(s, 0.0) or 0.0))
          for s in DOUBLE_DIGIT_STATS]
    # Distribution of the number of categories in double digits, by convolution
    # over the five independent indicators. Exact given independence, and five
    # terms is small enough that there is no reason to approximate it.
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for k, mass in enumerate(dist):
            nxt[k] += mass * (1.0 - p)
            nxt[k + 1] += mass * p
        dist = nxt
    # **Cumulative, not exclusive.** Every one of the 149 triple-doubles in the
    # 2024-25 archive carries stat 37 = 1.0 as well -- 149 of 149, no
    # exceptions -- so ESPN counts a triple-double as a double-double too and
    # pays both coefficients. Each bonus is therefore ``P(at least count)``, a
    # tail sum. ``dist[count]`` is ``P(exactly count)``, which dropped the 4.0
    # on every triple-double and, worse, modelled a different quantity than the
    # observed rate it gets blended with in :func:`derive_scored_stats`: split
    # ``00`` counts them cumulatively too, so the two halves of that blend
    # disagreed about what they were measuring.
    return {
        stat: math.fsum(dist[count:]) if count < len(dist) else 0.0
        for count, stat in BONUS_STATS.items()
    }


def observed_bonus_rates(
    splits: Mapping[str, Mapping[StatId, float]],
) -> Tuple[Dict[StatId, float], float]:
    """``(his own per-game bonus rates, games behind them)``.

    Split ``00`` reports these the same way it reports every other counting
    stat -- as a per-game average, with ``GP`` alongside as the only total in
    the dictionary. So a player with one double-double in three games arrives
    here as ``0.33`` and needs no division. Probed on 2026-10-26: Luka Doncic
    came back ``DD 0.33, TD 0.0, GP 3.0``.
    """
    actual = splits.get(ids.SPLIT_SEASON_TOTAL) or {}
    games = float(actual.get(ids.GP_STAT, 0.0) or 0.0)
    rates = {
        stat: float(actual.get(stat, 0.0) or 0.0)
        for stat in BONUS_STATS.values()
        if stat in actual
    }
    return rates, games


#: Stats a matchup multiplier must not touch. The non-additive ones because
#: scaling a percentage by 3% is not a statement about anything, and the three
#: counters because a hard opponent does not change how many games a player
#: plays -- that is the availability model's question, and ``GP_STAT`` in
#: particular is read by the games-played budget, which would quietly start
#: spending a fractional cap.
UNSCALED_BY_MATCHUP: FrozenSet[StatId] = frozenset(
    ids.NON_ADDITIVE_STATS | {ids.MINUTES, ids.GAMES_STARTED, ids.GAMES_PLAYED}
)


def apply_matchup(
    mean: Mapping[StatId, float],
    multiplier: float,
) -> Dict[StatId, float]:
    """Scale a projected line by one opponent multiplier.

    Applied to the raw line *before* :func:`derive_scored_stats`, which is the
    whole reason the two are ordered this way: a centre facing the league's
    best rim protection is genuinely less likely to reach ten rebounds, so the
    suppression has to reach the double-double term too. Scaling the line and
    then deriving gets that for free; deriving first and then scaling would
    apply the opponent twice to the bonus and once to everything else.

    A multiplier of exactly 1.0 returns a copy unchanged rather than a
    rebuilt dict of products, so the common path -- no fitted table, or an
    opponent the table has never seen -- is bit-for-bit what it was before
    this module existed.
    """
    out = dict(mean)
    if multiplier == 1.0:
        return out
    for stat, value in mean.items():
        if stat not in UNSCALED_BY_MATCHUP:
            out[stat] = float(value) * multiplier
    return out


def derive_scored_stats(
    mean: Mapping[StatId, float],
    splits: Mapping[str, Mapping[StatId, float]],
) -> Dict[StatId, float]:
    """Stats the league scores that ESPN's projection does not publish.

    Two gaps, both found by replaying week 1 of the 2026-27 season against what
    the leagues actually paid out, and both of which had been quietly pricing
    rebounders at a fraction of their worth:

    * **Rebounds.** Split ``10`` publishes total ``REB`` (6). Both live leagues
      score ``OREB`` (4) at 1.5 and ``DREB`` (5) at 1.0 and have **no
      coefficient on plain ``REB`` at all**. ``PointsValue.conditional_value``
      does ``mean.get(stat, 0.0)`` per scored stat, so every rebound in both
      leagues was worth exactly nothing. Walker Kessler, 10.8 projected
      rebounds a game, was priced at 19.94 fantasy points and scored 41.3.
    * **Double-doubles.** Priced at 4.0/8.0/15.0 and never projected. See
      :func:`modelled_bonus_rates`.

    Neither is a tweak to the value model: the value model was right and was
    being handed a mean with holes in it where the league's money was. Which is
    why this lives here, in the layer whose job is "what the sources said"
    turning into "what the optimizer consumes", and returns *additions* to the
    mean rather than editing it in place -- the provenance of a derived number
    should be legible, and a reader comparing this against split ``10`` should
    be able to see which stats ESPN never sent.

    The split of ``REB`` into its components **keeps** ``REB`` as well. A league
    that scored both would then double-count, which is why ``value.py`` refuses
    one rather than this module trying to guess which the league meant.
    """
    out: Dict[StatId, float] = {}

    rebounds = float(mean.get(ids.REB, 0.0) or 0.0)
    if rebounds > 0.0 and ids.OREB not in mean and ids.DREB not in mean:
        share = offensive_rebound_share(splits)
        out[ids.OREB] = rebounds * share
        out[ids.DREB] = rebounds * (1.0 - share)

    modelled = modelled_bonus_rates(mean)
    observed, games = observed_bonus_rates(splits)
    w = games / (games + BONUS_PRIOR_GAMES) if games > 0 else 0.0
    for stat, rate in modelled.items():
        if stat in mean:
            # ESPN started publishing it. Its own number beats our model.
            continue
        seen = observed.get(stat)
        out[stat] = (w * float(seen) + (1.0 - w) * rate) if seen is not None \
            else rate

    return out


def form_weight(games: float, has_recent: bool) -> float:
    """How much of the last-15 split goes into the mean. Zero means none.

    Split out of :func:`per_game_mean` so that the rationale can state the
    weight without recomputing it, and so the gate has one definition. The
    curve is unchanged: nothing below :data:`MIN_GAMES_FOR_FORM`, then
    ``games/82`` capped at half.
    """
    if not has_recent or games < MIN_GAMES_FOR_FORM:
        return 0.0
    # Capped so form never fully displaces the projection: a hot fortnight is
    # partly real and partly a small sample, and the projection already embeds
    # the prior.
    return min(games / float(NBA_SEASON_GAMES), 0.5)


def recent_form(
    splits: Mapping[str, Mapping[StatId, float]],
) -> Tuple[Optional[Dict[StatId, float]], float, float]:
    """``(last-15 per-game mean, season games played, blend weight)``.

    The mean is returned whether or not it is used, because the page shows what
    the model is *discounting* as well as what it acted on. A reader who has
    just watched a player go for 51 is owed the number and the reason it did
    not move the lineup, rather than a rationale that silently omits it.
    """
    recent = {
        int(k): float(v)
        for k, v in (splits.get(ids.SPLIT_LAST_15) or {}).items()
        if int(k) not in _EXCLUDED
    }
    games = float((splits.get(ids.SPLIT_SEASON_TOTAL) or {}).get(ids.GP_STAT, 0.0))
    return (recent or None), games, form_weight(games, bool(recent))


def per_game_mean(
    splits: Mapping[str, Mapping[StatId, float]],
) -> Optional[Dict[StatId, float]]:
    """Per-game production means for one player, or ``None`` if unprojectable.

    ``None`` rather than an empty dict, and never zeros. A player we cannot
    project must be *absent* from the optimizer's value map so that no slot is
    filled on the strength of a fabricated line; a dict of zeros is a claim that
    he will score nothing, which is a different and much more confident claim
    than "we do not know".

    Blends recent form only once there is enough of it -- see
    :data:`MIN_GAMES_FOR_FORM`.
    """
    projected = splits.get(ids.SPLIT_SEASON_PROJECTED) or {}
    if not projected:
        return None

    mean = {
        int(k): float(v)
        for k, v in projected.items()
        if int(k) not in _EXCLUDED
    }
    if not mean:
        return None

    recent, games, w = recent_form(splits)
    if recent and w > 0.0:
        for stat in list(mean):
            if stat in recent:
                mean[stat] = (1.0 - w) * mean[stat] + w * float(recent[stat])
        log.debug("blended last-15 at weight %.2f over %.0f games", w, games)

    return mean


def variance_from_mean(mean: Mapping[StatId, float]) -> Dict[StatId, float]:
    """``Var = PHI * mu``, floored at zero. See the module docstring.

    Applied to the magnitude, so a stat that arrives negative (none do today;
    ESPN sends misses as positive ``FGMI``) still gets a non-negative variance.
    A negative variance would silently invert the sign of the risk term.

    The bonus stats are the exception, because they are not counts. A
    double-double happens or it does not, so its per-game mean *is* a
    probability and its variance is ``p(1-p)``. Running them through
    ``PHI * mu`` would hand a 0.27 double-double rate a variance of 0.43 --
    larger than the 0.25 a Bernoulli can reach at all, and nearly twice its
    true 0.20 -- which inflates the risk term for precisely the rebounders
    this release exists to stop under-pricing.
    """
    out: Dict[StatId, float] = {}
    for stat, mu in mean.items():
        m = abs(float(mu))
        if stat in BONUS_STATS.values():
            p = min(m, 1.0)
            out[stat] = p * (1.0 - p)
        else:
            out[stat] = PHI * m
    return out


def p_play_for(
    entry: RosterEntry,
    *,
    status: Optional[str] = None,
    status_source: str = "espn",
    projected_season_games: Optional[float] = None,
) -> AvailabilityNote:
    """Probability this player suits up today.

    Args:
        entry: the roster entry, whose ESPN ``injury_status`` is the floor.
        status: a better status from Sleeper or the official injury report.
            Only *honoured when it is worse* than ESPN's -- see below.
        projected_season_games: ESPN's own ``GP`` projection, the durability
            signal pulled out of the per-game split.

    An external source may only make a player **less** available, never more.
    The asymmetry is on purpose: the sources disagree on freshness as much as on
    substance, and the two errors are not symmetric. Benching a player who turns
    out to play costs one slot of production. Starting a player who is ruled out
    costs the slot *and* a game off the cap, and it is the error a human notices.
    """
    espn_status = entry.availability
    chosen, source = espn_status, "espn"
    if status and status in P_PLAY_PRIOR:
        if P_PLAY_PRIOR[status] < P_PLAY_PRIOR[espn_status]:
            chosen, source = status, status_source

    p = P_PLAY_PRIOR[chosen]

    gp = None
    if projected_season_games is not None and projected_season_games > 0:
        gp = float(projected_season_games)
        rate = min(gp / float(NBA_SEASON_GAMES), 1.0)
        # Only an undesignated player gets the durability haircut. A player who
        # is already DOUBTFUL today has his absence priced by the designation;
        # multiplying again would double-count the same missed game.
        if chosen == "ACTIVE":
            p *= (1.0 - DURABILITY_WEIGHT) + DURABILITY_WEIGHT * rate

    return AvailabilityNote(
        player_id=entry.player_id,
        status=chosen,
        status_source=source,
        p_play=max(min(p, 1.0), 0.0),
        projected_season_games=gp,
    )


def build_projections(
    roster: Sequence[RosterEntry],
    splits: Mapping[PlayerId, Mapping[str, Mapping[StatId, float]]],
    *,
    as_of: datetime,
    statuses: Optional[Mapping[PlayerId, "tuple"]] = None,
    opponent: Optional[Mapping[PlayerId, float]] = None,
    source_name: str = "espn-projection",
) -> "tuple":
    """Build the per-player projections. Returns ``(projections, notes, skipped)``.

    Args:
        roster: whose projections to build.
        splits: from :meth:`espn.read.EspnRead.player_projections`.
        statuses: ``player_id -> (status, source_label)`` overrides from
            Sleeper or the injury report. Absent means ESPN's status stands.
        opponent: ``player_id -> opponent multiplier`` from
            :meth:`sources.defense.TeamDefense.multiplier`. Absent, or missing
            a player, means 1.0 -- no opponent adjustment, which is also what
            an unresolved opponent must mean.

    ``skipped`` lists players with no usable projection. They are reported
    rather than defaulted, because a player who vanishes from the value map
    silently is indistinguishable from a player the optimizer decided to bench.
    """
    projections: Dict[PlayerId, Projection] = {}
    notes: Dict[PlayerId, AvailabilityNote] = {}
    skipped: list = []

    for entry in roster:
        by_split = splits.get(entry.player_id) or {}
        mean = per_game_mean(by_split)
        if mean is None:
            skipped.append(entry)
            continue
        # Opponent first, then the derived stats -- see :func:`apply_matchup`
        # for why that order is load-bearing rather than arbitrary.
        if opponent is not None:
            mean = apply_matchup(
                mean, float(opponent.get(entry.player_id, 1.0)))
        # After the form blend, not before: the rebound split has to divide the
        # mean the optimizer will actually see, and the bonus model has to read
        # the same line.
        mean.update(derive_scored_stats(mean, by_split))

        projected_games = (
            by_split.get(ids.SPLIT_SEASON_PROJECTED) or {}
        ).get(ids.GP_STAT)

        form, form_games, form_w = recent_form(by_split)

        override = (statuses or {}).get(entry.player_id)
        note = p_play_for(
            entry,
            status=override[0] if override else None,
            status_source=override[1] if override else "espn",
            projected_season_games=projected_games,
        )
        notes[entry.player_id] = note

        projections[entry.player_id] = Projection(
            player_id=entry.player_id,
            mean=mean,
            var=variance_from_mean(mean),
            p_play=note.p_play,
            minutes=float(mean.get(ids.MINUTES, 0.0)),
            source=source_name,
            as_of=as_of,
            # The mean is conditional on playing: ESPN's per-game projection is
            # production in games he appears in, and its own GP projection is a
            # separate field. Claiming otherwise here is the double-discount bug
            # that `Projection.p_play_applied` exists to prevent.
            p_play_applied=False,
            form_mean=form,
            form_games=form_games,
            form_weight=form_w,
        )

    if skipped:
        log.warning(
            "no usable projection for %d player(s); they are excluded from the "
            "lineup rather than valued at zero: %s",
            len(skipped),
            [e.name for e in skipped],
        )
    return projections, notes, skipped
