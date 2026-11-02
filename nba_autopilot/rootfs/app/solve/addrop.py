"""Whether to swap a roster spot: one add, one drop, or neither.

This is the irreversible layer. ``pipeline.py`` can be wrong about today's
lineup and simply be right tomorrow; a drop is permanent, publicly attributed
in the league's activity feed, and spends a weekly acquisition that cannot be
earned back. So the bar is not "is this an improvement" -- almost anything is,
by a hair -- it is "is this an improvement worth being permanent", and
everything below exists to make that second question answerable in points.

What a pickup is actually worth
-------------------------------
The naive answer is "his per-game projection", and it is wrong in both
directions. A 30-point-per-game pickup is worth nothing if his games all fall
on nights our lineup is already full of better players, and a 15-point pickup
is worth the whole 15 on a night we cannot fill a starting slot at all.

So a candidate is priced against **two** scarce things, each with its own dual:

* ``lambda`` -- the games-played cap, from :func:`solve.budget.shadow_price`.
  One number for the whole week. This is why the two live leagues behave so
  differently: at ``lambda = 0.00`` an extra game is free upside, and at
  ``lambda = 20.20`` a pool player projecting 18 a game is worth *negative*
  points, because playing him costs a game we would rather spend on someone
  better later in the week.
* ``theta_d`` -- the price of a starting slot on day ``d``, from
  :func:`slot_prices`. Zero on a night when fewer of our players have games
  than we have slots to fill, and otherwise the conditional value of the worst
  starter we would have to bench to make room.

A candidate's value for the rest of the week is then

    surplus = sum over his game-days d of  p_play * max(v - max(lambda, theta_d), 0)

and that single expression is what makes games played the primary driver rather
than a side condition. On a light slate ``theta_d`` collapses to zero and every
positive-value body is pure gain -- which is the requirement in its original
words: *"even if we pick up a player who gives us 15 points that 15 points more
than we would have had."* On a heavy slate ``theta_d`` is high and the same
pickup is worth nothing, correctly, because he would sit.

``max(..., 0)`` and not a signed difference, because a player who does not
clear the hurdle on a given night is simply benched that night. He cannot cost
us points by existing.

Two metrics, two different jobs
-------------------------------
``surplus`` is **points this week**, and it is what the gain is measured in.
``v`` -- the per-game conditional value -- is **quality**, and it is what the
margin and keeper gates are measured in. Keeping them apart is what lets a
season-ending injury be a free drop in a redraft league (surplus 0, because
``p_play`` is 0) while still being a protected asset in a keeper league (``v``
unchanged, because the conditional value of a player who will not play is still
his line).

The gates
---------
A proposal must clear a gain floor **and** have a reason:

1. **Quality** -- the add is at least :data:`DEFAULT_MARGIN_PCT` percent better
   per game than the player going out. The requirement's own framing: *"if
   theres a player that we think is doing 10-15% better than any of our lowest
   players across the board, we should make a move."*
2. **Games** -- the add simply has more startable games left this week than the
   player going out, where "startable" is ``p_play * games`` and not what the
   NBA schedule says, because the schedule does not know who is hurt. This is
   the case the quality gate would wrongly block: a solid player with no games
   left is worth less *this week* than a mediocre one with three, and refusing
   that swap is refusing free points.

Either reason suffices; the points gain is required regardless, so a "reason"
can never license a move that loses points.

The keeper asymmetry
--------------------
In a keeper league we are not choosing a lineup, we are choosing a roster we
keep. *"Being out for the season there does not automatically mean we'd want to
drop them. different from the other league."* The temptation is to invent a
horizon and convert future seasons into this week's points; that number would
be fabricated, and this codebase does not invent values that change decisions.

So the keeper flag acts as a **gate on the droppable set, by rank** rather than
as a price: only the bottom :data:`DEFAULT_KEEPER_DROP_FRACTION` of the roster
by per-game conditional value may be dropped at all. An injured star ranks near
the top on ``v`` and is therefore untouchable no matter how worthless he is
this week, which is the intended behaviour stated as a rule instead of guessed
at with a discount rate.

What the matchup score is and is not used for
---------------------------------------------
ESPN publishes no projected total for basketball, so we know the score *so far*
and nothing about where it is heading -- we have our own roster to project but
not the opponent's. That rules out any "this one is already decided" test,
which would need exactly the number we do not have.

It is used in one direction only: when we are behind by less than the move
would gain, the move can flip the scoreboard by itself, and the churn floor
drops to zero. *"that could decide a matchup."* The floor is never *raised* for
being ahead -- an accumulated lead with no opponent projection behind it is not
evidence the week is safe.

Boundaries
----------
Nothing here does I/O, and nothing here writes. One call returns **at most one
swap**, the best one against the roster it was given, with a rationale.
``main.py`` decides whether that becomes a proposal the user taps or (under
``autonomy_add_drop``) a transaction.

One swap per call, deliberately, even though a run may offer several. This
layer is not the place to rank: the drop side and the open-spot arithmetic both
depend on what the roster holds, so the second-best swap against today's roster
is usually *not* the second swap to make -- it is the same pickup paired with
the same man going out. A caller that wants more calls again with the roster
the first swap leaves behind, which is what ``_consider_addrops`` does, passing
``just_added`` so the new man is not immediately given back (ADR-021).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import (
    Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple,
)

from espn import ids
from models import (
    LeagueSettings,
    MatchupScore,
    PlayerId,
    Projection,
    RosterEntry,
)
from solve.calendar import game_days_for

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MARGIN_PCT",
    "DEFAULT_CHURN_FLOOR",
    "DEFAULT_KEEPER_DROP_FRACTION",
    "REASON_QUALITY",
    "REASON_GAMES",
    "REASON_OPEN",
    "REASON_IR",
    "Candidate",
    "AddDropPlan",
    "acquisitions_per_matchup",
    "remaining_acquisitions",
    "slot_prices",
    "week_surplus",
    "plan_addrop",
    "plan_ir_activation",
]

#: How much better per game an add must be than the player going out, in
#: percent, to justify a swap on quality alone. The requirement said "10-15%";
#: 12 is the middle of it, and it is a config option rather than a constant for
#: exactly that reason -- the user named a range, not a number.
DEFAULT_MARGIN_PCT: float = 12.0

#: Minimum week-surplus gain, in fantasy points, before a swap is worth being
#: permanent. Not zero: every roster has some positive-gain swap available on
#: any given day, and acting on all of them spends a five-per-week acquisition
#: budget on rounding error. Drops to zero when the move would by itself erase
#: the current deficit -- see the matchup-score note in the module docstring.
DEFAULT_CHURN_FLOOR: float = 3.0

#: In a keeper league, the fraction of the roster -- ranked by per-game
#: conditional value, worst first -- that may be dropped at all. A quarter of
#: the 24-player dynasty roster is six players, which is about the number a
#: human would describe as "our lowest players across the board".
DEFAULT_KEEPER_DROP_FRACTION: float = 0.25

REASON_QUALITY = "quality"
REASON_GAMES = "games"
#: Not a third gate -- the degenerate case where nothing goes out, so there is
#: no player to be a percentage better than and no games to compare against.
REASON_OPEN = "open-spot"
#: A healthy player on IR with no active spot to come back to. The only reason
#: that proposes a drop with no add, and the only one that is not an upgrade:
#: ESPN will not let him stay on IR, so the cost of *not* acting is the whole
#: day's lineup, not a few points of foregone gain.
REASON_IR = "ir-activation"


@dataclass(frozen=True)
class Candidate:
    """One player priced for the rest of this matchup period.

    Used for both sides of a swap, because the arithmetic is identical: what an
    add is worth and what a drop costs are the same quantity, measured on the
    same remaining days against the same slot prices.
    """

    player_id: PlayerId
    name: str
    #: Per-game value **conditional on playing**, matching ``value.py`` and the
    #: threshold convention in ``budget.shadow_price``. Quality.
    per_game: float
    #: Expected points added over the rest of the matchup period, net of what
    #: a start costs in cap and slot. Points.
    surplus: float
    #: Days left this matchup period on which his NBA team plays. The NBA
    #: schedule, nothing more -- see :attr:`startable_games`.
    games: int
    p_play: float
    #: True when acquiring him needs a waiver claim rather than an instant add.
    #: A property of the player, never of the league (ADR-017).
    waiver: bool = False
    #: True when he currently sits in an IR slot. The one slot fact this type
    #: carries, and it is here because dropping him frees an IR slot rather
    #: than an active one -- so on a roster whose active slots are all taken,
    #: he is not a way to make room for a healthy pickup.
    on_ir: bool = False
    #: False when ESPN has him on **no NBA team** -- ``proTeamId`` of 0. He
    #: cannot be started by anybody until somebody signs him, so he cannot
    #: score, and no projection of his is a projection of anything.
    #:
    #: Needed as its own fact because none of the others catch it. His
    #: ``p_play`` is a healthy 0.75, because availability is read off an injury
    #: designation and he has none. His :attr:`per_game` is 32.3, because
    #: ESPN's preseason projection was written while he still had a team and
    #: still says 62 games. Only :attr:`games` knows, at zero, and that is also
    #: what a man on a bye week looks like.
    on_nba_team: bool = True
    #: ESPN's market readings, carried verbatim from :class:`RosterEntry` and
    #: used by **nothing in this module**. Every gate above is priced in points
    #: or in percent-per-game, and adding "but the league likes him" as a term
    #: would mean inventing an exchange rate between ownership share and fantasy
    #: points. They are here so the layer that *does* reason in prose -- the
    #: judge in ``judge.py`` -- can see what the arithmetic cannot, and so the
    #: rationale can quote it. See :attr:`RosterEntry.percent_owned`.
    percent_owned: Optional[float] = None
    draft_adp: Optional[float] = None
    draft_rank: Optional[int] = None
    outlook: str = ""

    @property
    def startable_games(self) -> float:
        """Games we can actually expect out of him: ``p_play * games``.

        The games comparison has to be made on this and not on :attr:`games`,
        because :attr:`games` is the NBA schedule and the schedule does not
        know who is hurt -- a player out for the season still has three games
        on it. This is the whole of why a season-ending injury loses the games
        comparison, and so becomes the drop of choice, without "is he injured"
        appearing anywhere as a rule.
        """
        return max(float(self.p_play), 0.0) * float(self.games)

    def describe(self) -> str:
        return "%s (%.1f/game, %d game(s) left x %.0f%% to play, %+.1f pts)" % (
            self.name, self.per_game, self.games,
            max(self.p_play, 0.0) * 100.0, self.surplus,
        )

    def describe_market(self) -> str:
        """The market readings, or an explicit statement that there are none.

        Never an empty string and never a zero stood in for a missing field: a
        reader who is being asked to judge a drop needs to know the difference
        between "nobody owns him" and "we do not know who owns him", and those
        are the two readings most likely to be confused.
        """
        bits = []
        if self.percent_owned is not None:
            bits.append("owned in %.0f%% of ESPN leagues" % self.percent_owned)
        if self.draft_adp is not None:
            bits.append("drafted on average at %.0f" % self.draft_adp)
        if self.draft_rank is not None:
            bits.append("ESPN's preseason fantasy rank %d" % self.draft_rank)
        return "; ".join(bits) if bits else "no market readings reported"


@dataclass(frozen=True)
class AddDropPlan:
    """At most one swap, plus why -- or why not."""

    add: Optional[Candidate] = None
    drop: Optional[Candidate] = None
    #: On a :data:`REASON_IR` plan, the healthy player the drop makes room for.
    #: Separate from :attr:`add` and not a substitute for it: he is already
    #: ours, so the write must not try to acquire him, and a layer that read
    #: him out of ``add`` would spend an acquisition claiming a player off the
    #: free-agent pool who is not in it. ``None`` on every other reason.
    activating: Optional[Candidate] = None
    #: Expected points gained over the rest of the matchup period.
    gain: float = 0.0
    #: :data:`REASON_QUALITY`, :data:`REASON_GAMES`, :data:`REASON_OPEN` or
    #: :data:`REASON_IR`; empty when not proposed.
    reason: str = ""
    rationale: str = ""
    notes: Tuple[str, ...] = ()
    #: Acquisitions left this matchup period. ``None`` means unlimited.
    acquisitions_left: Optional[int] = None

    @property
    def proposed(self) -> bool:
        """True when there is something for the user to approve.

        Usually that is an add: a lone drop forfeits a roster spot for nothing,
        so on the quality and games gates an add is what makes a plan real.

        The exception is :data:`REASON_IR` -- a healthy player stuck on IR with
        no active spot to return to. There the spot *is* the point: we are not
        buying an upgrade, we are paying for roster legality, and the player
        coming back is already ours. See :func:`plan_ir_activation`.
        """
        return self.add is not None or self.drop is not None

    def describe(self) -> str:
        if not self.proposed:
            return "no add/drop: %s" % (self.notes[0] if self.notes else "no reason")
        if self.activating is not None:
            return "drop %s to activate %s off IR; net %+.1f pts (%s)" % (
                self.drop.describe() if self.drop is not None else "nobody",
                self.activating.describe(), self.gain, self.reason,
            )
        if self.add is None:
            return "drop %s to open a roster spot (%s)" % (
                self.drop.describe(), self.reason,
            )
        bits = ["add %s" % self.add.describe()]
        if self.drop is not None:
            bits.append("drop %s" % self.drop.describe())
        bits.append("net %+.1f pts (%s)" % (self.gain, self.reason))
        return "; ".join(bits)


# ---------------------------------------------------------------------------
# the acquisition cap
# ---------------------------------------------------------------------------


def acquisitions_per_matchup(settings: LeagueSettings) -> Optional[int]:
    """How many acquisitions the league allows per matchup period.

    ``None`` means unlimited, which is what both ``matchupAcquisitionLimit``
    absent and the season-long ``acquisitionLimit: -1`` mean.

    ESPN sends the limit **divided by the number of scoring periods in a
    matchup**, the same way it sends the games-played cap -- ``5/7`` and ``7/7``
    on the two live leagues, alongside ``matchupLimitPerScoringPeriod: true``.
    So the raw value is multiplied back up.

    **Rounded, not truncated.** The quotient is a repeating decimal, and
    ``int()`` truncates towards zero. The spelling ESPN actually sends,
    ``0.7142857142857143``, does multiply back to exactly ``5.0`` -- all 17
    significant digits of the double are on the wire, so the round-trip is
    exact. That is luck, not a guarantee: ``0.7142857 * 7`` is
    ``4.999999900000001``, so one shortened field would silently reduce a
    league with five acquisitions a week to four, for no reason a reader of the
    settings page could ever discover.
    """
    limit = settings.acquisition_limit
    if limit is None or limit <= 0:
        return None
    if settings.acquisition_limit_per_scoring_period:
        limit = limit * ids.SCORING_PERIODS_PER_MATCHUP
    return int(round(limit))


def remaining_acquisitions(
    settings: LeagueSettings,
    used: Optional[Mapping[int, int]],
    matchup_period: int,
) -> Optional[int]:
    """Acquisitions left this matchup period. ``None`` means unlimited.

    An absent key in ``used`` is a real zero rather than missing data, unlike
    the games-played reading: ESPN maintains
    ``transactionCounter.matchupAcquisitionTotals`` for every team
    unconditionally, so an empty mapping means nobody has picked anybody up.
    """
    cap = acquisitions_per_matchup(settings)
    if cap is None:
        return None
    spent = int((used or {}).get(int(matchup_period), 0) or 0)
    return max(cap - spent, 0)


# ---------------------------------------------------------------------------
# the two duals
# ---------------------------------------------------------------------------


def slot_prices(
    roster: Sequence[RosterEntry],
    values: Mapping[PlayerId, float],
    *,
    starting_slots: int,
    horizon: Sequence[int],
    schedule: Mapping[int, Mapping[int, int]],
) -> Dict[int, float]:
    """``day -> theta_d``: what a starting slot costs on each remaining day.

    On a day when fewer of our players have games than we have starting slots,
    a slot is free and ``theta_d`` is 0.0 -- there is literally nobody to bench
    to make room, so a newcomer's whole conditional value is gain. Otherwise
    every slot is occupied and the price of one is the conditional value of the
    **worst player currently filling one**, because that is who a newcomer
    displaces.

    Computed on the roster as it stands, including the player a swap would send
    out. When that player would have started on a given day, his own value is
    therefore still in the ordering and ``theta_d`` is one rank too high. The
    bias is deliberate: it under-states the gain, so the error can only ever
    suppress a marginal swap, never manufacture one. For an irreversible move
    that is the direction to be wrong in.

    Players sitting on IR are **excluded**, and that is correctness rather than
    bias: the price of a starting slot is what the worst man filling one is
    worth, and a man on IR fills nothing -- he cannot be started at all, so he
    is not who a newcomer displaces. Counting him sets the price off a player
    who is not in the running for it.

    That mattered most on the decision where the IR man is the subject: in
    :func:`plan_ir_activation` the returnee is on the roster being priced, so
    with him in the ordering he set the very threshold his own surplus was then
    measured against. Two starting slots and a returnee second-best by value
    priced the slot at exactly his value, his surplus came out 0.0, and an
    activation worth 50 points a week was never proposed -- by arithmetic that
    looked, in the notes, like a considered decline.

    Values are conditional on playing, not availability-weighted, matching the
    threshold convention in ``budget.shadow_price``.
    """
    prices: Dict[int, float] = {}
    n = max(int(starting_slots), 0)
    for day in horizon:
        playing = sorted(
            (
                float(values.get(e.player_id, 0.0))
                for e in roster
                if int(e.current_slot) != ids.IR_SLOT
                and (schedule.get(int(e.pro_team_id)) or {}).get(int(day))
            ),
            reverse=True,
        )
        if n <= 0 or len(playing) < n:
            prices[int(day)] = 0.0
        else:
            prices[int(day)] = max(playing[n - 1], 0.0)
    return prices


def week_surplus(
    *,
    per_game: float,
    p_play: float,
    game_days: Iterable[int],
    lam: float,
    prices: Mapping[int, float],
) -> float:
    """Expected points this player adds over ``game_days``.

    ``p_play * max(v - max(lambda, theta_d), 0)`` summed over his game days.
    See the module docstring for where each term comes from.

    An exhausted cap arrives here as ``lam = inf``, which makes every term
    zero: with no games left to spend, nobody we could acquire can be started,
    so nobody we could acquire is worth anything. That is the correct answer and
    not a degenerate one.
    """
    if lam == float("inf"):
        return 0.0
    total = 0.0
    for day in game_days:
        hurdle = max(float(lam), float(prices.get(int(day), 0.0)))
        total += max(float(per_game) - hurdle, 0.0)
    return total * max(float(p_play), 0.0)


# ---------------------------------------------------------------------------
# pricing and droppability, shared by both decisions
# ---------------------------------------------------------------------------


def _pricer(
    values: Mapping[PlayerId, float],
    projections: Mapping[PlayerId, Projection],
    horizon: Sequence[int],
    schedule: Mapping[int, Mapping[int, int]],
    lam: float,
    prices: Mapping[int, float],
) -> Callable[[RosterEntry], Candidate]:
    """A function turning a roster entry into a priced :class:`Candidate`.

    A factory rather than a closure inside the caller, because both decisions
    in this module price players the same way and have to: a drop proposed by
    one and a drop proposed by the other would otherwise be comparable only by
    coincidence.
    """

    def candidate(e: RosterEntry) -> Candidate:
        proj = projections.get(e.player_id)
        p_play = float(proj.p_play) if proj is not None else 0.0
        days = game_days_for(horizon, schedule, e.pro_team_id)
        per_game = float(values.get(e.player_id, 0.0))
        return Candidate(
            player_id=e.player_id,
            name=e.name,
            per_game=per_game,
            surplus=week_surplus(
                per_game=per_game, p_play=p_play,
                game_days=days, lam=lam, prices=prices,
            ),
            games=len(days),
            p_play=p_play,
            waiver=e.must_claim_on_waivers,
            on_ir=int(e.current_slot) == ids.IR_SLOT,
            on_nba_team=int(e.pro_team_id) != 0,
            percent_owned=e.percent_owned,
            draft_adp=e.draft_adp,
            draft_rank=e.draft_rank,
            outlook=e.outlook,
        )

    return candidate


@dataclass(frozen=True)
class _Droppable:
    """Who may be given away, who may not, and how full the roster is."""

    priced: Mapping[PlayerId, Candidate]
    droppable: Tuple[Candidate, ...]
    blocked: Tuple[str, ...]
    notes: Tuple[str, ...]
    roster_cap: int
    roster_use: int
    open_spot: bool


def _droppable_set(
    settings: LeagueSettings,
    roster: Sequence[RosterEntry],
    candidate: Callable[[RosterEntry], Candidate],
    *,
    starters: Optional[Iterable[PlayerId]] = None,
    never_drop: Iterable[str] = (),
    just_added: Iterable[PlayerId] = (),
    review_blocked: Iterable[str] = (),
    keeper_drop_fraction: float = DEFAULT_KEEPER_DROP_FRACTION,
) -> _Droppable:
    """The droppability rules, in one place.

    Shared by :func:`plan_addrop` and :func:`plan_ir_activation` so that the
    protections a user configured hold whichever decision is asking. Stating
    them twice is how ``never_drop`` comes to mean one thing on an upgrade and
    another on a roster-legality drop, which is the version of this bug that
    would cost somebody a player he said to keep.

    What it deliberately does **not** decide is whether a man sitting on IR may
    go: that answer differs between the callers -- see the note at the IR filter
    in :func:`plan_addrop` -- so ``droppable`` includes him and the caller
    filters.
    """
    notes: List[str] = []
    protected = {n.strip().lower() for n in never_drop if n.strip()}
    vetoed = {n.strip().lower() for n in review_blocked if n.strip()}
    started = {int(p) for p in (starters or ())}
    fresh = {int(p) for p in just_added}
    mine = [candidate(e) for e in roster]
    priced = {c.player_id: c for c in mine}
    by_value = sorted(mine, key=lambda c: c.per_game)

    if settings.keeper:
        # A rank gate, not a value gate. See the keeper note in the module
        # docstring: converting "we keep this player" into this week's points
        # would mean inventing a horizon, so the rule is stated as a rule.
        keep_rank = max(int(round(len(mine) * float(keeper_drop_fraction))), 1)
        eligible_by_rank = {c.player_id for c in by_value[:keep_rank]}
        # A man on no NBA team is never protected by it, whatever his rank.
        #
        # The gate's premise is that a player we keep has value beyond this
        # week; a player nobody has signed has no value in any week until that
        # changes, and the rank it would protect him on is a reading of a
        # projection written while he still had a team. Two of them -- Jaden
        # Ivey at 32.3 a game and Cam Thomas at 39.3, both at ``proTeamId`` 0,
        # both still projected for 60-odd games -- ranked 20th and 23rd of 27
        # on a live dynasty roster and were protected from being dropped for a
        # whole season while holding two of its twenty-four spots.
        teamless = {c.player_id for c in mine if not c.on_nba_team}
        eligible_by_rank |= teamless
        notes.append(
            "keeper league: only the %d lowest-valued of %d players may be "
            "dropped, and being out for the season is not by itself a reason"
            % (keep_rank, len(mine))
        )
        if teamless:
            notes.append(
                "on no NBA team, so droppable whatever the keeper gate says: %s"
                % ", ".join(sorted(priced[p].name for p in teamless))
            )
    else:
        eligible_by_rank = {c.player_id for c in mine}

    droppable: List[Candidate] = []
    blocked: List[str] = []
    for e in roster:
        cand = priced[e.player_id]
        if e.name.strip().lower() in protected:
            blocked.append("%s (never_drop)" % e.name)
            continue
        if e.name.strip().lower() in vetoed:
            blocked.append("%s (the reviewer blocked a swap sending him out)"
                           % e.name)
            continue
        if int(e.player_id) in fresh:
            blocked.append("%s (picked up earlier this run)" % e.name)
            continue
        if e.lineup_locked:
            blocked.append("%s (locked)" % e.name)
            continue
        if int(e.player_id) in started:
            blocked.append("%s (starting today)" % e.name)
            continue
        if cand.player_id not in eligible_by_rank:
            continue
        droppable.append(cand)

    # Non-IR slots against non-IR occupants: see ``ids.active_roster_cap``. The
    # pair has to be measured the same way on both sides, or a player parked on
    # IR reads as using up a slot he does not occupy.
    roster_cap = ids.active_roster_cap(settings.lineup_slot_counts)
    roster_use = ids.active_roster_use(e.current_slot for e in roster)
    return _Droppable(
        priced=priced,
        droppable=tuple(droppable),
        blocked=tuple(blocked),
        notes=tuple(notes),
        roster_cap=roster_cap,
        roster_use=roster_use,
        open_spot=roster_use < roster_cap,
    )


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


def plan_addrop(
    settings: LeagueSettings,
    roster: Sequence[RosterEntry],
    pool: Sequence[RosterEntry],
    values: Mapping[PlayerId, float],
    projections: Mapping[PlayerId, Projection],
    *,
    scoring_period: int,
    matchup_period: int,
    horizon: Sequence[int],
    schedule: Mapping[int, Mapping[int, int]],
    lam: float = 0.0,
    starters: Optional[Iterable[PlayerId]] = None,
    matchup: Optional[MatchupScore] = None,
    acquisitions_used: Optional[Mapping[int, int]] = None,
    never_drop: Iterable[str] = (),
    just_added: Iterable[PlayerId] = (),
    review_blocked: Iterable[str] = (),
    margin_pct: float = DEFAULT_MARGIN_PCT,
    churn_floor: float = DEFAULT_CHURN_FLOOR,
    keeper_drop_fraction: float = DEFAULT_KEEPER_DROP_FRACTION,
    pool_limit: int = 40,
) -> AddDropPlan:
    """Decide at most one swap for the rest of this matchup period.

    Args:
        values: ``player -> per-game conditional value``, for **our players and
            the pool both**. A pool player missing here is valued at zero,
            which makes him unproposable rather than invisible.
        projections: likewise both sides; supplies ``p_play``.
        horizon: remaining scoring periods this matchup period, from
            ``calendar.remaining_periods``. Empty means the week is over and
            nothing is worth acquiring for it.
        lam: the games-played dual from the lineup plan. Shared deliberately:
            pricing the same cap twice with two different numbers would let the
            two layers disagree about what a game is worth.
        starters: players the lineup plan puts in a starting slot today. Never
            droppable -- their game may already be under way, and a drop that
            empties a started slot cannot be undone.
        never_drop: configured names, matched case-insensitively the same way
            ``espn/write.py`` matches them, so this layer refuses what preflight
            would refuse rather than proposing a move that dies at the wire.
        just_added: players an earlier swap *in the same run* put on the
            roster. Undroppable, and separate from ``never_drop`` only so that
            the note says which of the two reasons applied -- a user reading
            "never_drop" against a man the same run proposes to pick up would
            reasonably conclude the option was misconfigured.
        review_blocked: names the reviewer's veto has retired for the rest of
            the run, matched like ``never_drop``. Separate for the same reason
            ``just_added`` is: these are the app's own doing, and reporting them
            as ``never_drop`` told a user whose ``never_drop`` is empty that
            three of his players were in it.
    """
    notes: List[str] = []
    left = remaining_acquisitions(settings, acquisitions_used, matchup_period)

    def nothing() -> AddDropPlan:
        """No swap, but carrying every note that explains why not."""
        return AddDropPlan(notes=tuple(notes), acquisitions_left=left)

    if left is not None and left <= 0:
        notes.append(
            "no acquisitions left in matchup period %d (limit %s)"
            % (matchup_period, acquisitions_per_matchup(settings))
        )
        return nothing()
    if not horizon:
        notes.append(
            "no scoring periods left in matchup period %d, so a pickup could "
            "not play for us this week" % matchup_period
        )
        return nothing()
    if not pool:
        notes.append("no available players to consider")
        return nothing()
    if settings.uses_faab:
        # Neither live league does, so there is no bid policy and no way to
        # test one. Bidding zero would technically submit and reliably lose,
        # which is worse than declining: it spends an acquisition, shows up in
        # the activity feed, and teaches the user the feature is broken.
        notes.append(
            "league uses FAAB (budget %s) and this layer has no bidding "
            "policy, so it proposes nothing rather than bidding 0"
            % settings.faab_budget
        )
        return nothing()
    if lam == float("inf"):
        notes.append(
            "games-played budget exhausted, so an acquisition could not be "
            "started this week"
        )
        return nothing()

    starting_slots = sum(
        int(n) for s, n in settings.lineup_slot_counts.items()
        if int(n) > 0 and int(s) not in (ids.BENCH_SLOT, ids.IR_SLOT)
    )
    prices = slot_prices(
        roster, values,
        starting_slots=starting_slots, horizon=horizon, schedule=schedule,
    )

    candidate = _pricer(values, projections, horizon, schedule, lam, prices)

    # -- who may go out ----------------------------------------------------

    rule = _droppable_set(
        settings, roster, candidate,
        starters=starters, never_drop=never_drop, just_added=just_added,
        review_blocked=review_blocked,
        keeper_drop_fraction=keeper_drop_fraction,
    )
    notes.extend(rule.notes)
    priced = rule.priced
    droppable = list(rule.droppable)
    blocked = list(rule.blocked)
    roster_cap, roster_use, open_spot = (
        rule.roster_cap, rule.roster_use, rule.open_spot,
    )

    # Dropping a man off IR frees an IR slot, not an active one. So when every
    # active slot is taken he is no way to make room for a healthy pickup, and
    # pairing the add with him would be refused at the wire exactly as the
    # unpaired add was. He stays droppable when there is already room, because
    # then the pairing is a choice rather than the thing making the add legal.
    if not open_spot:
        for c in droppable:
            if c.on_ir:
                blocked.append("%s (on IR, which frees no active spot)" % c.name)
        droppable = [c for c in droppable if not c.on_ir]

    if blocked:
        notes.append("not droppable: %s" % ", ".join(sorted(blocked)))

    if not droppable and not open_spot:
        notes.append(
            "roster is full (%d/%d active spots) and nobody holding one is "
            "droppable, so a pickup has nowhere to go" % (roster_use, roster_cap)
        )
        return nothing()

    # -- who could come in -------------------------------------------------

    ours = {e.player_id for e in roster}
    adds = sorted(
        (candidate(e) for e in pool if e.player_id not in ours),
        key=lambda c: c.surplus,
        reverse=True,
    )[: max(int(pool_limit), 1)]
    if not adds or adds[0].surplus <= 0.0:
        notes.append(
            "nobody available clears the hurdle: lambda %.2f, slot prices %s"
            % (lam, _describe_prices(prices))
        )
        return nothing()

    # -- the floor ---------------------------------------------------------

    floor = max(float(churn_floor), 0.0)
    # The cheapest way out, which is an open roster spot when there is one --
    # it costs nothing. Computed before the gates so the floor and the
    # "best available" note describe the same swap.
    cheapest_out = min(
        [c.surplus for c in droppable] + ([0.0] if open_spot else [])
    )
    best_possible = adds[0].surplus - cheapest_out
    decisive = (
        matchup is not None
        and matchup.opp_team_id is not None
        and matchup.margin < 0.0
        and best_possible >= -matchup.margin
    )
    if decisive:
        floor = 0.0
        notes.append(
            "%s; a swap worth %+.1f could erase that by itself, so the churn "
            "floor is waived" % (matchup.describe(), best_possible)
        )
    elif matchup is not None:
        notes.append(matchup.describe())

    # -- pair them ---------------------------------------------------------

    pairs: List[Tuple[float, str, Candidate, Optional[Candidate]]] = []
    outs: List[Optional[Candidate]] = list(droppable)
    if open_spot:
        # An unpaired add is legal when the roster is not full, and it is
        # strictly better than any swap: nothing goes out. Offered alongside
        # the swaps rather than instead of them so the comparison is explicit.
        outs.append(None)

    for add in adds:
        for out in outs:
            gain = add.surplus - (out.surplus if out is not None else 0.0)
            if gain <= floor:
                continue
            reason = _reason_for(add, out, margin_pct)
            if not reason:
                continue
            pairs.append((gain, reason, add, out))

    if not pairs:
        notes.append(
            "best available swap gains %+.1f pts, under the %.1f-point churn "
            "floor or without a quality or games reason"
            % (best_possible, floor)
        )
        return nothing()

    pairs.sort(key=_pair_key)
    gain, reason, add, out = pairs[0]
    return AddDropPlan(
        add=add,
        drop=out,
        gain=gain,
        reason=reason,
        rationale=_rationale(
            add, out, gain, reason, lam, prices, margin_pct, matchup, decisive,
        ),
        notes=tuple(notes),
        acquisitions_left=left,
    )


def plan_ir_activation(
    settings: LeagueSettings,
    roster: Sequence[RosterEntry],
    values: Mapping[PlayerId, float],
    projections: Mapping[PlayerId, Projection],
    *,
    stranded: Iterable[PlayerId],
    matchup_period: int,
    horizon: Sequence[int],
    schedule: Mapping[int, Mapping[int, int]],
    lam: float = 0.0,
    starters: Optional[Iterable[PlayerId]] = None,
    matchup: Optional[MatchupScore] = None,
    never_drop: Iterable[str] = (),
    just_added: Iterable[PlayerId] = (),
    review_blocked: Iterable[str] = (),
    keeper_drop_fraction: float = DEFAULT_KEEPER_DROP_FRACTION,
) -> AddDropPlan:
    """Free an active spot for a healthy player stuck on IR.

    ``pipeline.plan_lineup`` reports these men up by id in
    :attr:`~pipeline.LineupPlan.ir_stranded`: ESPN has withdrawn their IR
    eligibility, every active slot is taken, and the lineup layer is not
    allowed to drop anybody, so it forces them back onto IR and says so. That
    is the correct thing for it to do and it is also a dead end -- a player
    parked there scores for nobody, and nothing about tomorrow changes it. The
    stranding is **permanent until something drops a player**, which is this
    layer's job and only this layer's job.

    Found on a live roster after a week of it: Alperen Sengun, ``ACTIVE``, no
    longer IR-eligible, sat on IR in both leagues and played 33 minutes for
    42.0 points that counted for neither. He was the second-most valuable
    player on one of the two rosters. The lineup notes had been saying so every
    morning, to nobody.

    Deliberately **not** :func:`plan_addrop` with the returnee passed in as an
    add:

    * He is already ours. An add would spend an acquisition -- a real, capped,
      weekly resource -- claiming a player who is not in the pool to claim.
    * The gain here is a lower bound, not an estimate. Every other reason in
      this module compares two futures in which we field a legal lineup either
      way; this one compares fielding him against never fielding him again. So
      the churn floor does not apply: see the gate below.

    What it does not do is propose dropping the stranded man himself. When he
    is worth less than anybody droppable that is the honest answer, and it is a
    different and more dangerous decision than this one -- it gives away a
    player to tidy up a slot. The declining note names him so the owner can
    make that call.

    Args:
        stranded: ``LineupPlan.ir_stranded``. Ids not on the roster are
            ignored rather than an error: the roster is re-read between the
            lineup plan and here on some paths, and a man who left in between
            is no longer a problem.
        values, projections, horizon, schedule, lam: priced exactly as in
            :func:`plan_addrop`, through the same :func:`_pricer`. The two
            decisions have to agree about what a player is worth or the one
            that fires second undoes the other's reasoning.
        starters, never_drop, just_added, review_blocked, keeper_drop_fraction:
            the droppability rules, applied through :func:`_droppable_set` so
            that a man the user protected is protected here too.
    """
    notes: List[str] = []

    def nothing() -> AddDropPlan:
        return AddDropPlan(notes=tuple(notes))

    mine = {int(e.player_id): e for e in roster}
    want = [mine[int(p)] for p in stranded if int(p) in mine]
    if not want:
        # Not a note. This is the overwhelmingly common case -- no healthy man
        # is stuck -- and a line saying so every morning in every run's notes
        # is noise that trains the reader to skip the section.
        return nothing()

    if not horizon:
        notes.append(
            "%s stuck on IR, but matchup period %d has no scoring periods "
            "left, so activating now would not play him -- the next run will "
            "price it against a fresh week"
            % (", ".join(e.name for e in want), matchup_period)
        )
        return nothing()

    starting_slots = sum(
        int(n) for s, n in settings.lineup_slot_counts.items()
        if int(n) > 0 and int(s) not in (ids.BENCH_SLOT, ids.IR_SLOT)
    )
    prices = slot_prices(
        roster, values,
        starting_slots=starting_slots, horizon=horizon, schedule=schedule,
    )
    candidate = _pricer(values, projections, horizon, schedule, lam, prices)

    rule = _droppable_set(
        settings, roster, candidate,
        starters=starters, never_drop=never_drop, just_added=just_added,
        review_blocked=review_blocked,
        keeper_drop_fraction=keeper_drop_fraction,
    )
    notes.extend(rule.notes)

    # The best of them, where the lineup layer stranded the cheapest. The two
    # halves agree on purpose: it seats as many returnees as it has room for
    # and holds back the ones it cannot, so the one it held back last is the
    # one most worth paying a roster spot for.
    returnees = sorted(
        (rule.priced[int(e.player_id)] for e in want),
        key=lambda c: (c.surplus, c.per_game), reverse=True,
    )
    back = returnees[0]
    if len(returnees) > 1:
        notes.append(
            "%d healthy player(s) stuck on IR; taking the most valuable first "
            "(%s), the rest next run: %s"
            % (len(returnees), back.name,
               ", ".join(c.name for c in returnees[1:]))
        )

    if rule.open_spot:
        # Then he was never stranded, whatever the lineup plan said, and a drop
        # would be paying for a spot we already have. Said out loud because the
        # two layers disagreeing about roster occupancy is a bug worth seeing
        # rather than a condition worth silently tolerating.
        notes.append(
            "%s is healthy on IR but %d of %d active spots are in use, so "
            "there is already room for him -- no drop needed, and the lineup "
            "plan should have seated him"
            % (back.name, rule.roster_use, rule.roster_cap)
        )
        return nothing()

    # Dropping a man off IR frees an IR slot, not an active one -- the same
    # reasoning as the IR filter in ``plan_addrop``, and it rules out the
    # returnee himself along with everybody else parked beside him.
    outs = [c for c in rule.droppable if not c.on_ir]
    blocked = list(rule.blocked) + [
        "%s (on IR, which frees no active spot)" % c.name
        for c in rule.droppable if c.on_ir
    ]
    if blocked:
        notes.append("not droppable: %s" % ", ".join(sorted(blocked)))

    if not outs:
        notes.append(
            "%s is healthy and cannot come off IR: all %d active spots are "
            "taken and nobody holding one may be dropped, so he scores for "
            "nobody until that changes"
            % (back.name, rule.roster_cap)
        )
        return nothing()

    # Cheapest out, ordered exactly as :func:`_pair_key` orders a swap's drop
    # side: a man on no NBA team first, then the least likely to suit up, then
    # the lowest projection. Stating it differently here is how the two
    # decisions come to disagree about who is expendable -- and the ordering
    # matters most in exactly the case this function handles, where half the
    # bench has a surplus of zero and the tie-break *is* the decision.
    out = min(
        outs,
        key=lambda c: (c.surplus, 0 if not c.on_nba_team else 1,
                       c.p_play, c.per_game),
    )
    gain = back.surplus - out.surplus

    # Strictly positive, and no churn floor. The floor exists to stop a weekly
    # acquisition budget being spent on rounding error (see
    # ``DEFAULT_CHURN_FLOOR``); neither half of that applies here. A drop
    # spends no acquisition, and ``gain`` is measured over this week's horizon
    # while the stranding it ends is permanent -- so the real gain is this
    # number plus every week after it. Requiring it to clear a three-point bar
    # measured on the pessimistic half of that would decline activations that
    # are plainly right.
    if gain <= 0.0:
        notes.append(
            "%s is healthy on IR, but the cheapest player we may drop for him "
            "is %s at %+.1f against his %+.1f, so activating him would cost "
            "%.1f points this week -- he stays parked, and giving him away "
            "instead is a call for the owner"
            % (back.name, out.name, out.surplus, back.surplus, -gain)
        )
        return nothing()

    return AddDropPlan(
        drop=out,
        activating=back,
        gain=gain,
        reason=REASON_IR,
        rationale=_ir_rationale(back, out, gain, lam, prices, matchup,
                                rule.roster_cap),
        notes=tuple(notes),
    )


def _ir_rationale(
    back: Candidate,
    out: Candidate,
    gain: float,
    lam: float,
    prices: Mapping[int, float],
    matchup: Optional[MatchupScore],
    roster_cap: int,
) -> str:
    """The IR activation, in numbers a human can check.

    Written apart from :func:`_rationale` rather than as a fourth branch of it,
    because the sentence it has to produce is not the same shape: there is no
    player coming in, the comparison is against zero rather than against an
    incumbent, and the thing being bought is a roster spot.
    """
    lines = [
        "Activate %s off IR by dropping %s. ESPN has taken away %s's IR "
        "eligibility while he is healthy, so he occupies an IR slot he cannot "
        "score from, and all %d active spots are full -- he plays again only "
        "if somebody goes."
        % (back.name, out.name, back.name, roster_cap),
        "He has %d game(s) left this week and projects %.1f per game, worth "
        "%+.1f points; %s is worth %+.1f over the same days. Net %+.1f."
        % (back.games, back.per_game, back.surplus,
           out.name, out.surplus, gain),
        "That %+.1f counts this week only. He is stuck until a drop frees a "
        "spot, so every week after this one is the same number again."
        % gain,
        "A game off the cap prices at %.2f; a starting slot prices at %s."
        % (lam, _describe_prices(prices)),
    ]
    if matchup is not None:
        lines.append(matchup.describe().capitalize() + ".")
    return " ".join(lines)

def _pair_key(
    pair: Tuple[float, str, Candidate, Optional[Candidate]]
) -> Tuple[float, int, float, float, float, int]:
    """Sort key for candidate swaps, best first.

    Gain decides it. The rest is a tie-break, and ties are common rather than
    exotic: every bench player with no game left this week has a surplus of
    exactly zero, so on a Saturday half the bench is indistinguishable on
    points alone.

    The tie-break is **a man on no NBA team first**, then lowest ``p_play``,
    then lowest per-game value. A player who will not suit up cannot help us at
    any point, while two healthy players with nothing left this week can both
    still matter if someone ahead of them is ruled out tomorrow. That ordering
    is also what makes a season-ending injury the drop of choice in a redraft
    league without "is he injured" appearing anywhere as a rule -- it falls out
    of ``p_play``.

    Teamlessness has to come before ``p_play`` rather than fall out of it,
    because it does not: a man nobody has signed carries no injury designation,
    so his ``p_play`` is the healthy 0.75 prior and he sorts *behind* every
    genuinely injured player and behind anyone with a lower projection. On a
    live roster that put Jaden Ivey, who could not be started by anybody,
    behind two men who could.

    Player id last, so a run is reproducible rather than dependent on roster
    ordering.
    """
    gain, _reason, add, out = pair
    return (
        -gain,
        # An open roster spot sorts ahead of every real drop: nothing goes out.
        0 if out is None else (0 if not out.on_nba_team else 1),
        out.p_play if out is not None else -1.0,
        out.per_game if out is not None else -1.0,
        -add.per_game,
        int(add.player_id),
    )


def _reason_for(
    add: Candidate, out: Optional[Candidate], margin_pct: float
) -> str:
    """Which of the two justifications this pair satisfies, if either.

    Checked in the requirement's own order: quality first, because that is the
    rule as stated, and games second, because that is the case the quality rule
    would wrongly exclude.
    """
    if out is None:
        # Nothing is going out, so there is nothing to be a given percentage
        # better than. An open roster spot filled with a positive-surplus
        # player is upside with no downside.
        return REASON_OPEN
    threshold = out.per_game * (1.0 + max(float(margin_pct), 0.0) / 100.0)
    if add.per_game >= threshold:
        return REASON_QUALITY
    # Startable games, not scheduled games: the epsilon is float noise cover,
    # not a policy knob -- how *much* more the games are worth is the gain's
    # job, and the gain has already cleared the churn floor by the time we are
    # asked for a reason.
    if add.startable_games > out.startable_games + 1e-9:
        return REASON_GAMES
    return ""


def _describe_prices(prices: Mapping[int, float]) -> str:
    if not prices:
        return "none"
    return ", ".join(
        "p%d=%.1f" % (day, prices[day]) for day in sorted(prices)
    )


def _rationale(
    add: Candidate,
    out: Optional[Candidate],
    gain: float,
    reason: str,
    lam: float,
    prices: Mapping[int, float],
    margin_pct: float,
    matchup: Optional[MatchupScore],
    decisive: bool,
) -> str:
    """One paragraph a human can check the arithmetic of.

    Shown on the approval card, so every number that moved the decision appears
    here: an approval the user cannot audit is not really an approval.
    """
    lines = []
    if out is None:
        lines.append(
            "Add %s into an open roster spot. He has %d game(s) left this "
            "week and projects %.1f per game, worth %+.1f points."
            % (add.name, add.games, add.per_game, add.surplus)
        )
    elif reason == REASON_QUALITY:
        pct = (
            (add.per_game / out.per_game - 1.0) * 100.0
            if out.per_game > 0 else float("inf")
        )
        lines.append(
            "Add %s, drop %s. %.1f per game against %.1f -- %s better, past "
            "the %.0f%% bar. Worth %+.1f points over the rest of the week "
            "(%+.1f in, %+.1f out)."
            % (add.name, out.name, add.per_game, out.per_game,
               "%.0f%%" % pct if pct != float("inf") else "unboundedly",
               margin_pct, gain, add.surplus, out.surplus)
        )
    else:
        # Two decimals, and the raw games and play probability behind them.
        # ``startable_games`` is games times ``p_play``, so two players with the
        # same number of games and a three-point difference in availability come
        # out 1.92 against 1.86 -- which one decimal renders as "1.9 expected
        # game(s) against 1.9", a sentence that reads as a contradiction of the
        # clause before it. Seen on a live card.
        lines.append(
            "Add %s, drop %s -- for the games, not the quality. %.2f expected "
            "game(s) against %.2f (%d x %.0f%% against %d x %.0f%%), worth "
            "%+.1f points over the rest of the week (%+.1f in, %+.1f out)."
            % (add.name, out.name, add.startable_games, out.startable_games,
               add.games, add.p_play * 100.0, out.games, out.p_play * 100.0,
               gain, add.surplus, out.surplus)
        )
    lines.append(
        "A game off the cap prices at %.2f; a starting slot prices at %s."
        % (lam, _describe_prices(prices))
    )
    if decisive and matchup is not None:
        lines.append(
            "We are behind %.0f-%.0f against %s, and this is worth more than "
            "the gap."
            % (matchup.my_points, matchup.opp_points, matchup.opp_name)
        )
    elif matchup is not None:
        lines.append(matchup.describe().capitalize() + ".")
    if add.waiver:
        lines.append(
            "He is on waivers, so this is a claim: it stays pending until "
            "ESPN's next waiver run and can be cancelled before then."
        )
    return " ".join(lines)
