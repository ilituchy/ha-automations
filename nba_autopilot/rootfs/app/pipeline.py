"""From roster to target lineup. The layer that assembles the solver.

``main.py`` does the I/O; this module does the arithmetic and returns a plan.
Keeping them apart is what lets the whole decision be tested without an HTTP
client and replayed from a journal row.

**The shape of the problem is not "pick the best starters."** It is "pick who
sits," and the difference is load-bearing. League 1 has ten starting slots and
three bench slots, so thirteen non-IR players must land in thirteen places; on a
light slate where only eight of them have games, you cannot bench the other
five -- there is nowhere to put them. Players without a game today therefore
occupy starting slots for free: the games-played cap counts games **played**, so
an empty-slate starter costs nothing and scores nothing.

That makes the assignment a *perfect* matching over (non-IR players) x (starting
slots + bench slots), not a selection of the top ten. Every non-IR player is
present in the weight map at every slot he is eligible for, valued at

    w(p, s) = p_play_p * (v_p - lambda)   if p has a game today
    w(p, s) = 0                           otherwise, and for the bench

so a player whose conditional value does not clear the shadow price is benched
by the optimizer preferring the zero, and a gameless player is indifferent --
which is correct, and is why a tiny stickiness bonus is added for staying put
(see :data:`STICKINESS`). Without it the optimizer would reshuffle indifferent
players every single run and fill the league's public activity feed with noise.

Three things this module deliberately does **not** do:

* It does not price the cap unless both the calendar and the games-used reading
  are trustworthy. An unpriced cap makes the optimizer spendthrift; a
  mis-priced one makes it confidently wrong, and only the second is hard to
  notice.
* It does not emit a target it has not re-validated against the same rules
  ``espn/write.py`` enforces. Generating a write that preflight will reject
  costs the whole day's lineup, so the checks are duplicated here on purpose --
  once as a guard, once as a gate.
* It does not add or drop. That is irreversible, requires explicit approval,
  and lives elsewhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from espn import ids
from models import LeagueSettings, Move, PlayerId, Projection, RosterEntry, SlotId
from solve import assign, budget as budget_mod, value as value_mod
from solve.calendar import SeasonCalendar, team_games_in
from solve.projections import (
    AvailabilityNote,
    variance_from_mean,
)
from usage import GamesUsed

log = logging.getLogger(__name__)

#: Bonus for leaving a player where he already is, as a fraction of the largest
#: value in play. Breaks ties toward no-op, and is scaled rather than absolute
#: so it cannot outrank a real difference in any league's point scale -- an
#: absolute epsilon that is negligible in a points league could dominate in a
#: category league's gradient units.
STICKINESS: float = 1e-6


@dataclass(frozen=True)
class LineupPlan:
    """A complete target lineup plus everything needed to explain it."""

    #: ``player -> slot`` for **every** rostered player. Total, as
    #: ``EspnWrite.set_lineup`` requires.
    target: Mapping[PlayerId, SlotId]
    moves: Tuple[Move, ...]
    #: The dual on the games-played cap, in value units per game.
    lam: float
    #: ``remaining / opportunities``. Above 1 the cap is not binding.
    utilization: float
    values: Mapping[PlayerId, float]
    notes: Tuple[str, ...]
    #: False when the target failed its own validation. ``main.py`` must not
    #: propose an infeasible plan; the notes say why.
    feasible: bool = True
    cap: Optional[float] = None
    games_used: Optional[float] = None
    #: Players ESPN has stopped calling IR-eligible -- they got healthy -- who
    #: could not be given an active slot because every one is taken. They are
    #: left sitting in IR, which is illegal but which no item of our write
    #: creates, and they stay there until a roster spot opens.
    #:
    #: Structured rather than only a note, because the only fix is an add/drop
    #: and ``main.py`` has to be able to act on it rather than parse prose.
    ir_stranded: Tuple[PlayerId, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.moves)


def plan_lineup(
    settings: LeagueSettings,
    roster: Sequence[RosterEntry],
    projections: Mapping[PlayerId, Projection],
    *,
    scoring_period: int,
    matchup_period: int,
    schedule: Mapping[int, Mapping[int, int]],
    calendar: Optional[SeasonCalendar],
    games_used: Optional[GamesUsed] = None,
    availability: Optional[Mapping[PlayerId, AvailabilityNote]] = None,
) -> LineupPlan:
    """Build today's target lineup.

    Args:
        projections: from ``solve.projections.build_projections``. A player
            absent here has no usable projection and is valued at zero rather
            than excluded -- he still has to stand somewhere.
        schedule: ``EspnRead.pro_team_scoring_periods()``.
        calendar: ``None`` or untrustworthy disables cap pricing.
        games_used: ``None`` or untrustworthy disables cap pricing.
    """
    notes: List[str] = []
    by_id = {e.player_id: e for e in roster}

    model = value_mod.build_value_model(settings)
    # The risk term is left inert deliberately, and not for lack of a score:
    # ``read.matchup_score`` does return one, and ``main.run_league`` reads it
    # every run for the add/drop layer. What is missing is the *projected*
    # final -- ESPN publishes no ``totalProjectedPointsLive`` for basketball on
    # either side of a live row -- so an accumulated margin is not evidence of
    # where the week is heading, and signing the risk term by it would make
    # today's lineup chase a lead that may not exist. Changing this changes
    # live lineup writes, so it is a separate decision from reading the number.
    notes.append(
        "risk term inert: ESPN publishes no projected final for basketball, "
        "so the accumulated margin is not a lead to protect or chase"
    )

    plays_today = {
        pid: bool((schedule.get(int(e.pro_team_id)) or {}).get(int(scoring_period)))
        for pid, e in by_id.items()
    }

    # ``proTeamId 0`` is ESPN's "no NBA team" sentinel, and
    # ``calendar.team_games_in`` scores it zero games -- which is right, an
    # unsigned player cannot play. But the consequence is total and silent: he
    # is unstartable every day of the week and consumes no budget, so he never
    # appears in a move, a bench note, or the shadow price. Two rostered
    # players were in exactly this state on the first live run.
    #
    # Reported rather than repaired. If he really is unsigned this is the
    # strongest drop signal the roster has; if ESPN simply lost his team, a
    # productive player is being benched all week and the only way anyone finds
    # out is a note that says so.
    teamless = sorted(
        (e.name for e in roster
         if not int(e.pro_team_id or 0) and e.current_slot != ids.IR_SLOT),
    )
    if teamless:
        notes.append(
            "no NBA team (ESPN proTeamId 0), so unstartable all week and "
            "excluded from the cap: %s" % ", ".join(teamless)
        )

    values: Dict[PlayerId, float] = {}
    for pid, proj in projections.items():
        if pid in by_id:
            values[pid] = float(model.conditional_value(proj))

    # -- price the cap -----------------------------------------------------

    lam = 0.0
    utilization = float("inf")
    cap = budget_mod.matchup_cap(settings)
    used: Optional[float] = None
    gb = None

    if cap is None:
        notes.append("league has no games-played cap")
    elif calendar is None or not calendar.trustworthy:
        notes.append(
            "games-played cap NOT priced: no trustworthy season calendar, so "
            "the remaining-games horizon is unknown"
        )
    elif games_used is None or not games_used.trustworthy:
        notes.append(
            "games-played cap NOT priced: %s"
            % (games_used.note if games_used else "no games-used reading")
        )
    else:
        used = games_used.games
        gb = budget_mod.remaining_budget(settings, used)
        horizon = calendar.remaining_periods(matchup_period, scoring_period)
        candidates = _cap_candidates(roster, values, projections, horizon, schedule)
        lam = budget_mod.shadow_price(candidates, gb)
        utilization = budget_mod.utilization_ratio(candidates, gb)
        notes.append(
            "cap %.0f, used %.0f, %d day(s) left in matchup period %d, "
            "lambda %.2f, utilization %.2f"
            % (cap, used, len(horizon), matchup_period, lam, utilization)
        )

    # -- partition the roster ----------------------------------------------

    fixed: Dict[PlayerId, SlotId] = {}
    pinned: Dict[PlayerId, SlotId] = {}
    movable: List[RosterEntry] = []

    for e in roster:
        if e.lineup_locked:
            # ESPN rejects the entire transaction if one item touches a locked
            # player, so a locked player's slot is a hard constraint rather than
            # a preference.
            #
            # Pinned via the assignment rather than just recorded, and that
            # includes a locked player sitting on the **bench**: his bench slot
            # is occupied, and if the optimizer does not know that, it fills all
            # three bench slots underneath him and the resulting map exceeds
            # bench capacity. IR is excluded from the capacity map entirely, so
            # a locked IR player is recorded instead of pinned.
            if e.current_slot == ids.IR_SLOT:
                fixed[e.player_id] = e.current_slot
            else:
                pinned[e.player_id] = e.current_slot
            continue
        if e.current_slot == ids.IR_SLOT and e.ir_eligible:
            # Still legitimately parked. IR consumes neither a starting slot nor
            # a bench slot, and an OUT player gains nothing from being moved.
            fixed[e.player_id] = ids.IR_SLOT
            continue
        movable.append(e)

    vacating = [
        e for e in movable
        if e.current_slot == ids.IR_SLOT and not e.ir_eligible
    ]
    if vacating:
        # ESPN's IR eligibility is its own rule and we never infer it; a player
        # who has lost it must come off IR or the next write is rejected.
        notes.append(
            "must come off IR (no longer IR-eligible per ESPN): %s"
            % ", ".join(e.name for e in vacating)
        )

    # -- park newly-OUT players on IR to free bench space ------------------

    ir_capacity = int(settings.lineup_slot_counts.get(ids.IR_SLOT, 0))
    # ``vacating`` players are physically on IR right now and are counted as
    # occupying it, even though we are about to try to seat them: ESPN checks
    # capacity against the slots they hold today, so a parking move that assumes
    # their slots are free can exceed IR capacity and lose the write.
    ir_taken = (
        sum(1 for s in fixed.values() if s == ids.IR_SLOT) + len(vacating)
    )
    ir_free = max(ir_capacity - ir_taken, 0)
    if ir_free:
        # Lowest value first: parking is free either way, so the tie-break is
        # which player we are least likely to have to un-park in a hurry. This
        # is a tie-break, not a decision -- every candidate here is OUT today.
        parkable = sorted(
            (e for e in movable
             if e.ir_eligible and e.current_slot != ids.IR_SLOT),
            key=lambda e: values.get(e.player_id, 0.0),
        )
        for e in parkable[:ir_free]:
            fixed[e.player_id] = ids.IR_SLOT
        if parkable[:ir_free]:
            notes.append(
                "to IR: %s" % ", ".join(e.name for e in parkable[:ir_free])
            )
        parked = {e.player_id for e in parkable[:ir_free]}
        movable = [e for e in movable if e.player_id not in parked]

    # -- returnees with nowhere to sit -------------------------------------

    # A healthy man coming off IR needs an active spot, and if the roster is
    # already full there is not one. Freeing it means dropping somebody, which
    # this layer may not do, so he stays parked for today and is reported up by
    # id for the add/drop layer to act on.
    #
    # Forced here rather than left to the matching, because the matching would
    # otherwise choose its own victim: with every active slot eligible to
    # somebody, it seats the returnee and strands whichever player it liked
    # least -- possibly one sitting legally on the bench, who then fails
    # validation and costs the whole day's lineup. Which man stays on IR has to
    # be our decision, and the cheapest returnee is the one to leave there.
    #
    # Accepted approximation: a returnee we *do* seat releases the IR slot he
    # holds, and the parking pass above could have used it. Parking has already
    # run by this point, so that slot goes unused today. It under-parks by at
    # most a slot or two and errs toward holding fewer players on IR than we are
    # entitled to, which is the safe direction -- never toward an illegal write.
    stranded_entries: List[RosterEntry] = []
    if vacating:
        active_cap = ids.active_roster_cap(settings.lineup_slot_counts)
        excess = (len(pinned) + len(movable)) - active_cap
        if excess > 0:
            leave = sorted(
                vacating, key=lambda e: values.get(e.player_id, 0.0),
            )[:excess]
            for e in leave:
                fixed[e.player_id] = ids.IR_SLOT
            held = {e.player_id for e in leave}
            movable = [e for e in movable if e.player_id not in held]
            stranded_entries.extend(leave)

    # -- the assignment ----------------------------------------------------

    capacity: Dict[SlotId, int] = {
        int(s): int(n)
        for s, n in settings.lineup_slot_counts.items()
        if int(n) > 0 and int(s) != ids.IR_SLOT
    }
    exhausted = lam == float("inf")
    rc: Dict[PlayerId, float] = {}
    for e in movable:
        proj = projections.get(e.player_id)
        if not plays_today.get(e.player_id) or proj is None:
            rc[e.player_id] = 0.0
        elif exhausted:
            # No budget left: every game played from here is one we are not
            # allowed to spend, so a player with a game today is strictly worse
            # than one without. A finite penalty rather than -inf, because the
            # assignment routine needs finite arithmetic.
            rc[e.player_id] = -1.0
        else:
            rc[e.player_id] = float(proj.p_play) * (
                values.get(e.player_id, 0.0) - lam
            )

    scale = max([abs(x) for x in rc.values()] + [1.0])
    stick = STICKINESS * scale
    # Every weight is shifted strictly positive. This is not cosmetic: slot
    # capacity exactly equals the non-IR roster size, so *every* movable player
    # must be placed somewhere -- and ``max_weight_assignment`` is free to leave
    # a player unassigned, which it would prefer over a negative weight. With a
    # negative-valued player left out, the resulting map is not total and the
    # whole day's lineup is discarded.
    #
    # The shift is neutral to the optimum: it adds the same constant to every
    # slot a player could occupy, and the matching assigns each player exactly
    # once, so the total moves by a constant and the argmax does not move at all.
    shift = max(0.0, -min(rc.values(), default=0.0)) + stick

    weights: Dict[Tuple[PlayerId, SlotId], float] = {}
    for e in movable:
        for slot in sorted(e.eligible_slots):
            if slot not in capacity:
                continue
            w = 0.0 if slot == ids.BENCH_SLOT else rc[e.player_id]
            weights[(e.player_id, slot)] = (
                w + shift + (stick if slot == e.current_slot else 0.0)
            )

    try:
        assigned = assign.max_weight_assignment(
            weights, capacity, locked=pinned or None
        )
    except ValueError as exc:
        # Our roster view disagrees with ESPN's. Re-reading is the only correct
        # response, and that is the caller's next run.
        notes.append("assignment infeasible: %s" % exc)
        return LineupPlan(
            target={e.player_id: e.current_slot for e in roster},
            moves=(),
            lam=lam,
            utilization=utilization,
            values=values,
            notes=tuple(notes),
            feasible=False,
            cap=cap,
            games_used=used,
        )

    target: Dict[PlayerId, SlotId] = dict(fixed)
    for pid, slot in assigned.items():
        target[pid] = slot

    unplaced = [e for e in movable if e.player_id not in target]
    for e in unplaced:
        # The matching left him out: every slot he is eligible for was taken by
        # someone the optimizer preferred. Leaving him where he is keeps the map
        # total; the validation below decides whether that is legal.
        target[e.player_id] = e.current_slot
    if unplaced:
        notes.append(
            "%d player(s) not placed by the assignment, left in place: %s"
            % (len(unplaced), ", ".join(e.name for e in unplaced))
        )

    # Healthy men we could not seat: the ones held back above, plus -- belt and
    # braces -- any the matching still left out despite the roster arithmetic
    # saying there was room. The only way to seat them is to drop somebody,
    # which this layer is not allowed to do, so they are reported upward by id
    # for the add/drop layer to act on.
    for e in unplaced:
        if (e.current_slot == ids.IR_SLOT and not e.ir_eligible
                and e not in stranded_entries):
            stranded_entries.append(e)
    stranded = tuple(e.player_id for e in stranded_entries)
    for e in stranded_entries:
        notes.append(
            "%s is healthy and has to come off IR, but all %d active spot(s) "
            "are taken -- he cannot be started until somebody is dropped"
            % (e.name, ids.active_roster_cap(settings.lineup_slot_counts))
        )

    problems = validate_target(settings, roster, target)
    if problems:
        for p in problems:
            log.error("rejected target lineup: %s", p)
        notes.extend("invalid target: %s" % p for p in problems)
        return LineupPlan(
            target={e.player_id: e.current_slot for e in roster},
            moves=(),
            lam=lam,
            utilization=utilization,
            values=values,
            notes=tuple(notes),
            feasible=False,
            cap=cap,
            games_used=used,
            # Carried even here. An infeasible lineup is the one case where the
            # add/drop layer most needs to know a drop would unblock it.
            ir_stranded=stranded,
        )

    moves = _moves_for(
        roster, target, values, projections, plays_today, lam, availability,
        model,
    )
    return LineupPlan(
        target=target,
        moves=moves,
        lam=lam,
        utilization=utilization,
        values=values,
        notes=tuple(notes),
        feasible=True,
        ir_stranded=stranded,
        cap=cap,
        games_used=used,
    )


def _cap_candidates(
    roster: Sequence[RosterEntry],
    values: Mapping[PlayerId, float],
    projections: Mapping[PlayerId, Projection],
    horizon: Sequence[int],
    schedule: Mapping[int, Mapping[int, int]],
) -> List[Tuple[float, float]]:
    """``(conditional_value, p_play)`` per startable player-game left this week.

    One entry per game, because the cap is spent per game: a four-game week
    from one player is four separate opportunities competing with everybody
    else's.

    Values are **conditional on playing**, matching ``value.py`` and the
    threshold convention in ``budget.shadow_price``. The same per-game value is
    used for every day in the window, which is exact rather than approximate
    here: the projection is a season per-game average with no opponent or
    rest adjustment, so it is day-invariant by construction.

    Locked players' games today are included even though they are not choices
    any more. They are committed spends against the same budget, and omitting
    them would price the cap as if those games were free.
    """
    out: List[Tuple[float, float]] = []
    for e in roster:
        proj = projections.get(e.player_id)
        if proj is None:
            continue
        games = team_games_in(horizon, schedule, e.pro_team_id)
        if games <= 0:
            continue
        out.extend([(float(values.get(e.player_id, 0.0)), float(proj.p_play))] * games)
    return out


def validate_target(
    settings: LeagueSettings,
    roster: Sequence[RosterEntry],
    target: Mapping[PlayerId, SlotId],
) -> List[str]:
    """Re-check a target against the rules ``espn/write.py`` enforces.

    Deliberate duplication. ``_preflight_lineup`` raises, which is right at the
    write boundary and wrong here: a plan that would be rejected should be
    *replaced by no plan*, with a note, not turned into an exception that aborts
    the run and leaves yesterday's lineup unexamined. Running the same checks
    twice costs microseconds and converts a hard failure into a reported one.
    """
    problems: List[str] = []
    by_id = {e.player_id: e for e in roster}

    missing = sorted(set(by_id) - set(target))
    if missing:
        problems.append("unassigned players: %s" % missing)
    extra = sorted(set(target) - set(by_id))
    if extra:
        problems.append("players not on the roster: %s" % extra)

    for e in roster:
        slot = target.get(e.player_id)
        if slot is None:
            continue
        if e.lineup_locked and slot != e.current_slot:
            problems.append(
                "%s is lineup-locked but would move %s -> %s"
                % (e.name, ids.slot_name(e.current_slot), ids.slot_name(slot))
            )
        if slot not in e.eligible_slots:
            problems.append(
                "%s is not eligible for %s" % (e.name, ids.slot_name(slot))
            )
        if (slot == ids.IR_SLOT and not e.ir_eligible
                and slot != e.current_slot):
            # Only when the target *puts* him there. A player ESPN has already
            # parked on IR and has since declared healthy is in an illegal slot
            # before we touch anything, and ``set_lineup`` sends only the
            # differences -- so leaving him alone sends no item for him and
            # cannot be what a rejection is about.
            #
            # This used to veto the target either way, which made one healthy
            # player on a full roster cost the league its entire lineup write:
            # he could not be placed, the plan came back infeasible, and the
            # starters nobody was arguing about went unset too. Refusing to
            # create the illegal state is right; refusing to work around a
            # pre-existing one is how a bad situation got worse.
            problems.append("%s is not IR-eligible per ESPN" % e.name)

    counts: Dict[SlotId, int] = {}
    for slot in target.values():
        counts[slot] = counts.get(slot, 0) + 1
    for slot, n in sorted(counts.items()):
        capacity = settings.lineup_slot_counts.get(slot)
        if capacity is None:
            problems.append("slot %s does not exist in this league"
                            % ids.slot_name(slot))
        elif n > int(capacity):
            problems.append(
                "%d players in %s but capacity is %d"
                % (n, ids.slot_name(slot), int(capacity))
            )

    if roster and not [s for s in target.values() if ids.is_starting_slot(s)]:
        problems.append("target benches the entire roster")
    return problems


def _form_bit(proj: Projection, model: Any) -> Optional[str]:
    """What the player has actually scored lately, and its weight. Or ``None``.

    Priced through the same value model as the projection, so the two numbers
    in the rationale are comparable. A raw sum of ESPN's stat line would be in
    different units from ``value`` and invite exactly the wrong comparison.

    Stated even when the weight is zero, which is the point of the line. "Too
    few to count" is a defensible answer to *why is this man benched the day
    after 51 points*; saying nothing is not, and saying nothing is what the
    rationale did before.
    """
    form = proj.form_mean
    if not form:
        return None
    priced = model.conditional_value(
        replace(proj, mean=form, var=variance_from_mean(form))
    )
    games = proj.form_games
    plural = "" if games == 1 else "s"
    if proj.form_weight > 0.0:
        return "last-15 %.1f from %.0f game%s, %.0f%% of the mean" % (
            priced, games, plural, proj.form_weight * 100.0)
    # Reachable only when ESPN sent a recent split with no games in it.
    return "last-15 %.1f from %.0f game%s, not counted" % (priced, games, plural)


def _moves_for(
    roster: Sequence[RosterEntry],
    target: Mapping[PlayerId, SlotId],
    values: Mapping[PlayerId, float],
    projections: Mapping[PlayerId, Projection],
    plays_today: Mapping[PlayerId, bool],
    lam: float,
    availability: Optional[Mapping[PlayerId, AvailabilityNote]],
    model: Any = None,
) -> Tuple[Move, ...]:
    """One :class:`Move` per actual change, with the numbers behind it.

    The rationale is assembled from quantities already computed -- value,
    shadow price, availability, recent form -- and never from prose generated
    elsewhere.

    ``model`` defaults to ``None`` so that a caller who only wants the slot
    changes is not forced to build a value model; the form clause is simply
    omitted, as it is for a player ESPN has no recent split for.
    """
    out: List[Move] = []
    for e in roster:
        slot = target.get(e.player_id, e.current_slot)
        if slot == e.current_slot:
            continue
        kind = "IR" if ids.IR_SLOT in (slot, e.current_slot) else "LINEUP"
        v = values.get(e.player_id)
        bits = []
        if v is not None and plays_today.get(e.player_id):
            bits.append("value %.1f vs lambda %.1f" % (v, lam))
        elif not plays_today.get(e.player_id):
            bits.append("no game today")
        proj = projections.get(e.player_id)
        if proj is not None and model is not None:
            form = _form_bit(proj, model)
            if form:
                bits.append(form)
        note = (availability or {}).get(e.player_id)
        if note is not None:
            bits.append(note.describe())
        out.append(
            Move(
                kind=kind,
                player_id=e.player_id,
                player_name=e.name,
                from_slot=e.current_slot,
                to_slot=slot,
                rationale="%s -> %s (%s)" % (
                    ids.slot_name(e.current_slot), ids.slot_name(slot),
                    "; ".join(bits) or "slot reshuffle",
                ),
                value_delta=float(v or 0.0),
            )
        )
    return tuple(out)


def summarize(plan: LineupPlan, roster: Sequence[RosterEntry]) -> str:
    """Human-readable lineup summary for a notification body."""
    by_id = {e.player_id: e for e in roster}
    starters = sorted(
        ((ids.slot_name(s), by_id[p].name if p in by_id else str(p))
         for p, s in plan.target.items() if ids.is_starting_slot(s)),
        key=lambda t: t[0],
    )
    lines = ["%s: %s" % (slot, name) for slot, name in starters]
    if plan.moves:
        lines.append("")
        lines.extend("%s %s" % (m.player_name, m.rationale) for m in plan.moves)
    return "\n".join(lines)
