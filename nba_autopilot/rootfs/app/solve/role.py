"""Has this player's role changed since ESPN wrote his projection?

The problem
-----------
``solve/projections.per_game_mean`` blends ESPN's season projection with a
recent split, weighted by how much of the recent split there is --
``games / (games + FORM_PRIOR_GAMES)``, capped. That shrinkage is the right
answer to one question: *how much of a small sample should I believe?* It is
measured and it should not be touched.

It is the wrong answer to a different question that looks identical from the
outside: *is the thing I am shrinking toward still true?* The shrinkage assumes
the projection and the recent window describe the same player with the same job.
When a player's minutes change, they do not. ESPN projected Nikola Jovic at
24.6 minutes a night; he is playing 13.2. The projection is not a noisy estimate
of his current role, it is an accurate estimate of a role he no longer has, and
averaging it in does not add information -- it adds a known error.

So this module answers only that second question, and the single thing it
returns is *how much to trust the recent window instead*. It does not produce a
projection, a value, or a points adjustment. One signal, one job.

Why minutes and not points
--------------------------
Points move for two reasons: the player changed, or the ball bounced. Minutes
move for one: somebody decided. A 50-point game is one sample of a noisy
quantity; a jump from 24 minutes to 37 is a coaching decision that persists.

Measured on 1,588 player-days from this season's box scores, within player
(each man against his own mean), fantasy points run **+1.33 per extra minute**
with an R-squared of 0.255, and the relationship is linear from -12 to +10
minutes -- no flattening at either end. Minutes alone explain a quarter of a
player's game-to-game variation. That coefficient is carried here as
:data:`POINTS_PER_MINUTE` for the *rationale* only: it turns a minutes
divergence into the sentence "worth about eight points a game", which is what a
reader needs. Nothing in the arithmetic multiplies by it, because the recent
window already contains the effect and counting it twice would double it.

Against the projection, not against the season
----------------------------------------------
The divergence is measured against **ESPN's projected minutes**, not against
his season average. That is the whole point: the projection is what the blend
pulls toward, so the question is whether the projection's assumption about his
role still holds. Measuring against the season average instead answers a
question nobody asked and misses the most valuable case -- Anthony Black's
season minutes and his recent minutes agree, and both are five above the 22.8
ESPN projected him for. His projection has been stale since the first week.

Three guards, each of which cost accuracy when it was missing
-------------------------------------------------------------
* **ACTIVE only.** A hurt player's minutes collapse is an availability fact and
  ``projections.p_play_for`` already prices it. Jaren Jackson Jr's last-7 window
  was a single ten-minute game he left injured; read as a role change it moved
  his value from 34.3 to 16.0 against an actual 30.7, which was worse by itself
  than the rule was better everywhere else.
* **A minimum of three games in the window** (:data:`MIN_WINDOW_GAMES`), for the
  same reason stated the other way round. The original form-weight comment
  warned that "a single blowout or a six-minute injury return cannot move a
  projection"; that warning applies here and this is how it is honoured.
* **A deadband** (:data:`DEADBAND_MINUTES`). Every player's recent minutes
  wobble a minute or two against any projection. Below the deadband the model
  was already right -- measured mean error +0.5 points for players inside it --
  so there is nothing to fix and moving them only adds noise.

What it is worth
----------------
Fitted against actual production over the following three games, on 31
player-seasons across both live rosters: mean absolute error **4.45 -> 4.09**
and RMSE **5.59 -> 5.50**. On the thirteen players the rule actually moves, MAE
**4.14 -> 3.30**, a fifth of the error gone. Both metrics improve, which is the
bar: an earlier version cut MAE and *raised* RMSE, trading typical accuracy for
occasional large misses, and that is the wrong trade for a rule that benches
people.

What it cannot see
------------------
A role change that has not happened yet. Anfernee Simons' minutes are **below**
his projection, so this rule shrinks him -- and his actual production is far
above it, because LeBron James and Joel Embiid have been sitting and the
minutes they vacate land on him. No backward-looking window contains tonight's
absence. This rule gets him wrong and will keep getting him wrong; that case
needs the teammate availability read, which is reported to the judge rather
than folded into the arithmetic. See ``solve/absence.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from espn import ids
from models import StatId

__all__ = [
    "DEADBAND_MINUTES",
    "FULL_SHIFT_MINUTES",
    "WEIGHT_CEILING",
    "MIN_WINDOW_GAMES",
    "POINTS_PER_MINUTE",
    "RoleRead",
    "read_role",
]

#: Minutes of divergence below which nothing happens. Every recent window
#: wobbles against every projection; players inside this band were already
#: being projected well (measured mean error +0.5 points a game), so there is
#: no error here to correct and correcting it anyway only adds noise.
DEADBAND_MINUTES: float = 3.0

#: Divergence at which the projection is treated as fully stale -- the recent
#: window gets :data:`WEIGHT_CEILING` and the projection whatever is left.
#:
#: Chosen, not fitted, and bounded deliberately. The evidence behind it: across
#: both rosters the model under-rated players whose minutes were up five or more
#: by 3.1 points a game and over-rated those down five or more by 7.1, while
#: players inside five were projected almost exactly right. So the error becomes
#: material around five minutes and is unambiguous by twelve -- Ace Bailey went
#: 24, 22, 29, 34, 40, 37. Twelve is the far end of that, which keeps the ramp
#: gentle in the region where the evidence is thin.
FULL_SHIFT_MINUTES: float = 12.0

#: The most weight the recent window may take on a fully-confirmed role change.
#:
#: Above ``projections.FORM_WEIGHT_CAP`` (0.75) on purpose: that cap exists so a
#: *small sample* never fully displaces the projection, and it is the right
#: number for that. This is the other case -- the projection is not imprecise,
#: it is describing the wrong job -- and there the thing to be careful of is the
#: window's own noise, not the projection's absence. Short of 1.0 for that
#: reason: a last-7 window is one to three games.
WEIGHT_CEILING: float = 0.90

#: Games a window needs before its minutes mean anything. See the guards note
#: in the module docstring -- this is the one that keeps an injury-truncated
#: single game from being read as a demotion.
MIN_WINDOW_GAMES: float = 3.0

#: Fantasy points per extra minute, within player. Measured on 1,588
#: player-days this season: beta = +1.332, R-squared 0.255, linear across
#: -12 to +10 minutes.
#:
#: **For the rationale only.** Nothing here multiplies by it. The recent window
#: already contains the production the extra minutes produced, so adding a
#: points term on top would count the same minutes twice.
POINTS_PER_MINUTE: float = 1.33


@dataclass(frozen=True)
class RoleRead:
    """One player's role, compared with the role ESPN projected him into.

    ``split`` is the recent window to blend and ``weight_ceiling`` how far the
    form weight may be lifted toward it. A read with ``divergence`` of zero is
    the ordinary case and changes nothing: ``split`` is the usual last-15 and
    the caller's own shrinkage weight stands.
    """

    #: The recent split to blend -- ``ids.SPLIT_LAST_15`` normally, or
    #: ``ids.SPLIT_LAST_7`` when the role is still moving.
    split: str = ids.SPLIT_LAST_15
    #: Recent minutes minus ESPN's projected minutes, on :attr:`split`. Zero
    #: when there is no usable read, which is not the same as "no change" but
    #: is treated the same way -- see :meth:`changed`.
    divergence: float = 0.0
    #: Minutes ESPN projected, and minutes he is actually playing. Carried so
    #: the rationale can state both rather than only their difference.
    projected_minutes: float = 0.0
    recent_minutes: float = 0.0
    #: Games in :attr:`split`.
    games: float = 0.0

    @property
    def changed(self) -> bool:
        """Past the deadband, in either direction."""
        return abs(self.divergence) > DEADBAND_MINUTES

    @property
    def trust(self) -> float:
        """How stale the projection is, from 0.0 to 1.0.

        Linear in the divergence past the deadband. Not a probability and not
        calibrated as one -- it is the interpolation parameter between "believe
        the usual shrinkage" and "believe the recent window".
        """
        if not self.changed:
            return 0.0
        span = max(FULL_SHIFT_MINUTES - DEADBAND_MINUTES, 1e-9)
        return min((abs(self.divergence) - DEADBAND_MINUTES) / span, 1.0)

    def adjust(self, weight: float) -> float:
        """The caller's shrinkage weight, lifted toward :data:`WEIGHT_CEILING`.

        Never lowers it. A role change is a reason to believe the recent window
        *more*, and a player whose sample is already large enough to earn a
        weight above the ceiling keeps it.
        """
        w = float(weight)
        if w >= WEIGHT_CEILING:
            return w
        return w + (WEIGHT_CEILING - w) * self.trust

    @property
    def points_implied(self) -> float:
        """The divergence in fantasy points a game, at the league-average rate.

        A rule of thumb and described as one, not a projection of this player.
        :data:`POINTS_PER_MINUTE` is a *population* coefficient -- how much any
        player's score moves when his own minutes move night to night -- and
        applying it to a difference in *role* over-states the case for a
        low-usage man: Nikola Jovic scores about 0.6 fantasy points a minute,
        so his eleven lost minutes are worth nearer seven points than the
        fifteen this returns. Checked against his actual production, which came
        in six points below his projection, not fifteen.

        Kept anyway, because minutes are not the unit anybody decides in and
        the alternative is a reader doing this multiplication in his head with
        no coefficient at all. :meth:`describe` names the rate so the number
        reads as the scaling it is. See :data:`POINTS_PER_MINUTE` for why
        nothing in the arithmetic multiplies by it.
        """
        return self.divergence * POINTS_PER_MINUTE

    def describe(self) -> str:
        """One sentence, or empty when there is nothing to say.

        Empty rather than "no change detected": this is written onto a card
        beside a dozen other lines, and a line per player saying nothing
        happened is how a reader learns to skip the section.
        """
        if not self.changed:
            return ""
        window = "last 7 days" if self.split == ids.SPLIT_LAST_7 else "last 15 days"
        return (
            "Role %s: ESPN projects %.1f minutes a night, he is playing %.1f "
            "over the %s (%.0f game(s)) -- %+.1f minutes, or roughly %+.0f "
            "fantasy points a game at the league-average %.2f per minute."
            % (
                "expanded" if self.divergence > 0 else "shrunk",
                self.projected_minutes, self.recent_minutes, window,
                self.games, self.divergence, self.points_implied,
                POINTS_PER_MINUTE,
            )
        )


def _window(
    splits: Mapping[str, Mapping[StatId, float]], key: str
) -> "tuple[Optional[float], float]":
    """``(minutes, games)`` for one split, or ``(None, 0.0)``."""
    s = splits.get(key) or {}
    if not s:
        return None, 0.0
    minutes = s.get(ids.MINUTES)
    games = float(s.get(ids.GP_STAT, 0.0) or 0.0)
    return (None if minutes is None else float(minutes)), games


def read_role(
    splits: Mapping[str, Mapping[StatId, float]],
    *,
    active: bool = True,
) -> RoleRead:
    """Compare the minutes he is playing with the minutes he was projected.

    Args:
        splits: the per-player split map from ``EspnRead.player_projections``.
        active: whether ESPN lists him ``ACTIVE``. False short-circuits to an
            empty read -- an injured man's minutes are ``p_play``'s business,
            and the guards note in the module docstring says what happens when
            that distinction is skipped.

    Returns a read that changes nothing whenever the evidence is missing or
    inside the deadband, so a caller can apply it unconditionally.
    """
    if not active:
        return RoleRead()

    projected, _pg = _window(splits, ids.SPLIT_SEASON_PROJECTED)
    if projected is None or projected <= 0.0:
        # No projected minutes means no role to have diverged from. Nothing to
        # say, and in particular not "he has lost all his minutes".
        return RoleRead()

    long_min, long_games = _window(splits, ids.SPLIT_LAST_15)
    if long_min is None or long_games < MIN_WINDOW_GAMES:
        return RoleRead()

    split, minutes, games = ids.SPLIT_LAST_15, long_min, long_games
    divergence = long_min - projected

    short_min, short_games = _window(splits, ids.SPLIT_LAST_7)
    if short_min is not None and short_games >= MIN_WINDOW_GAMES:
        short_div = short_min - projected
        # The shorter window only when it says something *more* than the long
        # one and says it past the deadband. Ungated, this cut mean error and
        # raised RMSE -- a worse estimate dressed up as a fresher one.
        if abs(short_div) > abs(divergence) and abs(short_div) > DEADBAND_MINUTES:
            split, minutes, games, divergence = (
                ids.SPLIT_LAST_7, short_min, short_games, short_div,
            )

    return RoleRead(
        split=split,
        divergence=divergence,
        projected_minutes=projected,
        recent_minutes=minutes,
        games=games,
    )
