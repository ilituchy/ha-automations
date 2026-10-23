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
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from espn import ids
from models import P_PLAY_PRIOR, Projection, RosterEntry

PlayerId = int
StatId = int

log = logging.getLogger(__name__)

__all__ = [
    "PHI",
    "MIN_GAMES_FOR_FORM",
    "NBA_SEASON_GAMES",
    "AvailabilityNote",
    "per_game_mean",
    "recent_form",
    "form_weight",
    "variance_from_mean",
    "p_play_for",
    "build_projections",
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
    """
    return {stat: PHI * abs(float(mu)) for stat, mu in mean.items()}


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
    source_name: str = "espn-projection",
) -> "tuple":
    """Build the per-player projections. Returns ``(projections, notes, skipped)``.

    Args:
        roster: whose projections to build.
        splits: from :meth:`espn.read.EspnRead.player_projections`.
        statuses: ``player_id -> (status, source_label)`` overrides from
            Sleeper or the injury report. Absent means ESPN's status stands.

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
