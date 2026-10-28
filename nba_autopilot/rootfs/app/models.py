"""Core value types. Shared by every layer; depends on nothing.

See ``docs/CONTRACTS.md`` section 1. Everything here is frozen and hashable so
a whole run's inputs can be snapshotted into the journal and replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, FrozenSet, Mapping, Optional

PlayerId = int
SlotId = int
StatId = int
ProTeamId = int

#: Normalized availability tiers, worst-to-best. ESPN's own feed only emits
#: ACTIVE / DAY_TO_DAY / OUT; ``GTD`` and the PDF's QUESTIONABLE/DOUBTFUL tiers
#: come from Sleeper and the official injury report respectively. Keeping them
#: distinct matters: DTD players usually play, DOUBTFUL players usually do not.
AVAILABILITY_ORDER = (
    "OUT",
    "DOUBTFUL",
    "QUESTIONABLE",
    "GTD",
    "DAY_TO_DAY",
    "PROBABLE",
    "ACTIVE",
)

#: Default probability a player suits up, by normalized tier. These are priors,
#: overridden by any source that supplies a real per-player number. Deliberately
#: conservative at the OUT end: starting a player who does not play costs a full
#: slot *and* a game off the cap, which is the worst outcome available to us.
P_PLAY_PRIOR = {
    "OUT": 0.0,
    "DOUBTFUL": 0.25,
    "QUESTIONABLE": 0.50,
    "GTD": 0.55,
    "DAY_TO_DAY": 0.75,
    "PROBABLE": 0.90,
    "ACTIVE": 0.98,
}


@dataclass(frozen=True)
class Projection:
    """Expected production for a *single specific game*.

    ``mean``/``var`` are already multiplied through by ``p_play`` only if
    ``p_play_applied`` is True. Keeping that explicit avoids the classic
    double-discount bug where availability is folded in twice -- once by the
    projection source and once by the optimizer.
    """

    player_id: PlayerId
    mean: Mapping[StatId, float]
    var: Mapping[StatId, float]
    p_play: float
    minutes: float
    source: str
    as_of: datetime
    p_play_applied: bool = False

    #: What the player has actually done lately, and how much of it is in
    #: ``mean``. Carried for the rationale, not for the optimizer -- which
    #: reads ``mean`` and nothing else. A reader who has just watched someone
    #: score 51 needs to see that number *and* the weight it was given.
    #: ``form_weight`` rises with the size of the recent sample rather than
    #: switching on at a threshold; see ``solve/projections.FORM_PRIOR_GAMES``.
    form_mean: Optional[Mapping[StatId, float]] = None
    form_games: float = 0.0
    form_weight: float = 0.0

    def expected(self) -> Dict[StatId, float]:
        """Availability-weighted mean. Safe to call regardless of provenance."""
        if self.p_play_applied:
            return dict(self.mean)
        return {k: v * self.p_play for k, v in self.mean.items()}

    def conditional_mean(self) -> Dict[StatId, float]:
        """Mean *given the player suits up* -- ``p_play`` divided back out.

        The counterpart to :meth:`expected`. ``solve/budget.py`` prices the
        games-played cap against the conditional value rather than the expected
        one, because a player only burns a game off the cap in the branch where
        he plays; see the availability-cancellation note there. Recovering it
        here keeps that one subtraction honest no matter which convention the
        projection source used.

        A ``p_play`` of zero leaves the mean untouched: the conditional line of
        a player who will not play is still his line, and the caller's budget
        arithmetic multiplies by zero anyway. Dividing would raise.
        """
        if not self.p_play_applied or self.p_play <= 0.0:
            return dict(self.mean)
        return {k: v / self.p_play for k, v in self.mean.items()}

    def conditional_var(self) -> Dict[StatId, float]:
        """Variance given the player suits up. See :meth:`conditional_mean`.

        When ``p_play`` has been applied the stored variance is the Bernoulli-
        gated one, so undoing it means inverting
        ``Var(B*X) = p*Var(X) + p*(1-p)*E[X]^2`` rather than just dividing --
        the second term is availability risk, not game-to-game variance, and
        leaving it in would double-count it against the risk model that is
        already pricing availability separately.
        """
        if not self.p_play_applied or self.p_play <= 0.0:
            return dict(self.var)
        p = self.p_play
        cond_mean = self.conditional_mean()
        out: Dict[StatId, float] = {}
        for k, gated in self.var.items():
            mu = cond_mean.get(k, 0.0)
            out[k] = max((gated - p * (1.0 - p) * mu * mu) / p, 0.0)
        return out

    def expected_var(self) -> Dict[StatId, float]:
        """Variance of a Bernoulli-gated random variable.

        ``Var(B*X) = p*Var(X) + p*(1-p)*E[X]^2``. The second term is the
        availability risk itself and is the whole reason a GTD star is not the
        same bet as a healthy role player with the same expected line.
        """
        if self.p_play_applied:
            return dict(self.var)
        p = self.p_play
        out: Dict[StatId, float] = {}
        for k, v in self.var.items():
            mu = self.mean.get(k, 0.0)
            out[k] = p * v + p * (1.0 - p) * mu * mu
        return out


@dataclass(frozen=True)
class RosterEntry:
    player_id: PlayerId
    name: str
    current_slot: SlotId
    eligible_slots: FrozenSet[SlotId]
    pro_team_id: ProTeamId
    injury_status: str
    lineup_locked: bool = False
    games_this_week: int = 0
    #: True when ESPN itself says this player may occupy an IR slot. We never
    #: infer this from injury status -- ESPN's eligibility rule is its own and
    #: guessing it produces rejected writes.
    ir_eligible: bool = False
    #: ESPN's ``playerPoolEntry.status``: ``ONTEAM``, ``FREEAGENT``,
    #: ``WAIVERS``, or ``""`` when not reported.
    #:
    #: This decides *how* a player may be acquired, and it is a property of the
    #: player rather than of the league. On 2026-10-20 one live league had its
    #: entire available pool on waivers while the other had 299 free agents and
    #: exactly one player on waivers -- so "does this league use waivers" is
    #: the wrong question, and only this field answers the right one. An
    #: instant add of a ``WAIVERS`` player is refused with
    #: ``TRAN_PLAYER_NOT_FREEAGENT``.
    acquisition_status: str = ""
    #: ``ownership.percentOwned`` -- the share of ESPN leagues rostering him.
    #:
    #: The four fields below are *market* readings, and none of them is an
    #: input to any arithmetic. They exist because the week-surplus model is
    #: deliberately blind to everything outside this week, which is correct for
    #: valuing a start and badly wrong for valuing a drop: a first-round pick
    #: who is out for a fortnight has a surplus of exactly zero and so becomes
    #: the cheapest player on the roster to give away. Observed live on
    #: 2026-10-21 -- the layer proposed dropping an 88%-owned asset twice in
    #: two runs. These are what make "the rest of the league disagrees with
    #: this valuation" a thing the app can see at all.
    #:
    #: ``None`` rather than ``0.0`` when ESPN did not report: a genuinely
    #: unowned player and a missing field must not read the same, since one of
    #: them is evidence and the other is its absence.
    percent_owned: Optional[float] = None
    #: ``ownership.averageDraftPosition``. Where the market drafted him.
    draft_adp: Optional[float] = None
    #: ``draftRanksByRankType.STANDARD.rank`` -- ESPN's own preseason ranking,
    #: which is a *fantasy* ranking and not an NBA one. Worth keeping distinct
    #: from the draft position because they disagree, and the disagreement is
    #: informative: the player this was built for went first overall in the NBA
    #: draft and is ranked 81st here.
    draft_rank: Optional[int] = None
    #: ``seasonOutlook`` -- ESPN's own prose on the player's season. Not parsed
    #: and not scored; carried verbatim for a reader, human or otherwise.
    outlook: str = ""

    @property
    def availability(self) -> str:
        return self.injury_status if self.injury_status in P_PLAY_PRIOR else "ACTIVE"

    @property
    def must_claim_on_waivers(self) -> bool:
        """True when acquiring this player needs a claim, not an instant add.

        An unreported status counts as *not* requiring a claim: the field is
        absent exactly on roster reads, where acquisition does not apply, and
        the caller should not be pushed into submitting claims there.
        """
        return self.acquisition_status == "WAIVERS"


@dataclass(frozen=True)
class StatLimit:
    """One entry of ``rosterSettings.lineupSlotStatLimits``, as ESPN sends it.

    The shape was assumed to be ``{slot_id: int}`` and is actually
    ``{slot_id: {"statId": int, "limitValue": float}}``. Verified against two
    live leagues on 2026-10-20, where the assumed shape raised ``TypeError``
    on the first read.

    ``limit_value`` is **fractional and per scoring period**: the observed
    values are exactly ``40/7`` and ``44/7``, alongside an
    ``matchupAcquisitionLimit`` of exactly ``5/7`` and ``7/7`` with
    ``matchupLimitPerScoringPeriod: true``. It is stored raw and un-rounded,
    because the conversion to a per-matchup integer is an interpretation and
    this class is the record of what ESPN actually said.
    """

    slot_id: SlotId
    stat_id: StatId
    limit_value: float


@dataclass(frozen=True)
class LeagueSettings:
    """Verbatim from ESPN. No invented defaults -- see the note on the GP cap."""

    league_id: int
    season_id: int
    name: str
    scoring_type: str
    lineup_slot_counts: Mapping[SlotId, int]
    #: ``None`` means *no cap configured*, which is categorically different from
    #: a cap of zero. Conflating the two either disables the optimizer's budget
    #: logic or benches the entire roster. Always check for None explicitly.
    #:
    #: Keyed by the slot id ESPN attaches the limit to. Both observed leagues
    #: use a single entry keyed at slot 15, a slot that appears nowhere in
    #: ``lineup_slot_counts`` -- so in practice this is one league-wide cap on
    #: games played rather than a family of per-slot caps.
    lineup_slot_stat_limits: Optional[Mapping[SlotId, StatLimit]]
    scoring_items: Mapping[StatId, float]
    categories: FrozenSet[StatId]
    waiver_mode: str
    #: ``None`` whenever ESPN reports ``isUsingAcquisitionBudget: false``,
    #: *even though* it still sends a non-zero ``acquisitionBudget``. Both
    #: observed leagues send ``100`` while running traditional waiver priority,
    #: so trusting the number would let the app try to bid FAAB in a league
    #: that has no FAAB.
    faab_budget: Optional[int]
    roster_lock_mode: str
    current_scoring_period: int
    current_matchup_period: int
    matchup_period_boundaries: Mapping[int, "MatchupWindow"]
    #: ``acquisitionSettings.matchupAcquisitionLimit``, raw and un-rounded.
    #: ``None`` means unlimited -- which is also what ESPN's separate
    #: season-long ``acquisitionLimit`` field spells as ``-1``, on both
    #: observed leagues.
    #:
    #: Stored raw for the same reason :class:`StatLimit` is: the value is
    #: fractional and per *scoring* period, and the conversion to a per-matchup
    #: integer is an interpretation rather than a reading. Observed live
    #: 2026-10-21 -- ``0.7142857142857143`` (= 5/7) and ``1.0`` (= 7/7), both
    #: alongside ``matchupLimitPerScoringPeriod: true``.
    #:
    #: Defaulted rather than required because this field was added after the
    #: type was in use, and a league whose settings predate the parse change is
    #: better off *uncapped* here than falsely capped: an unlimited reading
    #: wastes a rejected tap at worst, while a spurious cap of zero would
    #: silently switch acquisitions off altogether.
    acquisition_limit: Optional[float] = None
    #: ``acquisitionSettings.matchupLimitPerScoringPeriod``. True on both
    #: observed leagues, and what makes
    #: ``solve.addrop.acquisitions_per_matchup`` multiply the limit back up
    #: rather than take it at face value.
    acquisition_limit_per_scoring_period: bool = False
    #: Per-league keeper/dynasty flag, from the app's own configuration rather
    #: than from ESPN -- ESPN's ``isKeeperLeague`` describes a draft mechanic
    #: and both observed leagues report the same value for it, so it does not
    #: distinguish the two. It is carried here because it changes exactly one
    #: decision, in ``solve/addrop.py``: whether a player who is out for the
    #: season is a free drop (redraft) or an asset with value past this week
    #: that we are not in a position to price (keeper).
    keeper: bool = False
    #: ``status.latestScoringPeriod`` exactly as ESPN reported it, kept beside
    #: ``current_scoring_period`` because ``main`` overwrites that one with the
    #: period today's *date* falls in. The two disagree for part of every day:
    #: this field is the latest period ESPN has results for, which lags the day
    #: it is in, while the write service only accepts the current one. See
    #: ``Autopilot.today_period``.
    #:
    #: The cross-check in ``SeasonCalendar.verify`` wants this one rather than
    #: the corrected period: it is asking whether our week model reproduces
    #: ESPN's ``currentMatchupPeriod``, and ESPN's two numbers lag together, so
    #: pairing a corrected period with a lagging matchup would report a
    #: disagreement every week boundary that is ours and not ESPN's.
    espn_latest_scoring_period: int = 0

    @property
    def is_categories(self) -> bool:
        return self.scoring_type.upper().startswith("H2H_CATEGORY") or bool(
            self.categories
        )

    @property
    def is_points(self) -> bool:
        return self.scoring_type.upper().startswith("H2H_POINTS")

    @property
    def has_gp_cap(self) -> bool:
        return bool(self.lineup_slot_stat_limits)

    @property
    def uses_faab(self) -> bool:
        return self.faab_budget is not None


@dataclass(frozen=True)
class MatchupScore:
    """This week's head-to-head score as ESPN has it *so far*.

    Accumulated, not projected. ESPN publishes no projected total for
    basketball -- there is no ``totalProjectedPointsLive`` on either side of a
    live row -- so anything forward-looking has to be built by projecting both
    rosters, and this type deliberately holds only the part that was read.

    ``opp_team_id`` is ``Optional`` because a bye week has a row with one side.
    A bye is not a 0-0 tie and must not be valued like one.

    Everything below ``opp_points`` is for the person reading the status page
    rather than for the solver, and all of it comes off the same request the
    score does -- the identity fields from ``mTeam``, the games-used figures
    from the two ``cumulativeScore`` blocks on the one matchup row. Defaulted,
    because a caller that only wants the score should not have to supply them.
    """

    matchup_period: int
    my_team_id: int
    my_points: float
    opp_team_id: Optional[int]
    opp_name: str
    opp_points: float
    #: Our own team name. ESPN sends ``location``/``nickname`` as null on both
    #: live leagues, so ``name`` is the only full name there is, with ``abbrev``
    #: the only short one.
    my_name: str = ""
    my_abbrev: str = ""
    opp_abbrev: str = ""
    #: ``teams[].logo``. Present for every team on both live leagues, but see
    #: ``espn/read.matchup_score`` -- a custom one is an arbitrary URL chosen by
    #: a league member, not necessarily an ESPN-hosted image.
    my_logo: str = ""
    opp_logo: str = ""
    #: Games each side has spent against the weekly cap, as ESPN itself counts
    #: them. ``None`` where the row has no pooled entry yet.
    #:
    #: Not the same number as ``usage.measure`` returns for us: that one is
    #: reconciled against a season-to-date delta and may be the larger of the
    #: two when they disagree, because the solver must not under-count a hard
    #: cap. This is the figure shown on ESPN's own scoreboard, which is the one
    #: to put in front of a human; the reconciliation lives in the run notes.
    my_games_used: Optional[int] = None
    opp_games_used: Optional[int] = None
    #: ``scoringPeriodId -> points``, each side day by day, straight off the
    #: same matchup row the totals come from. ESPN publishes it for every week
    #: that has started and ``null`` for one that has not, and the keys are the
    #: scoring periods of that week -- so this is what lets a reader see which
    #: day of a finished week the match was actually lost on without a request
    #: per day.
    my_by_period: Mapping[int, float] = field(default_factory=dict)
    opp_by_period: Mapping[int, float] = field(default_factory=dict)
    #: ``WIN``, ``LOSS``, ``TIE``, or ``""`` while the week is still running.
    #:
    #: Normalized here because ESPN reports ``winner`` as ``HOME``/``AWAY``,
    #: which is only meaningful to something that knows which side of the row
    #: we are on -- and that is known exactly once, where the row is read.
    #: ``""`` is also the honest answer for a bye and for a league whose
    #: schedule does not carry the field.
    outcome: str = ""

    @property
    def final(self) -> bool:
        """Has this week been decided? Drives the word, not the arithmetic."""
        return self.outcome in ("WIN", "LOSS", "TIE")

    @property
    def margin(self) -> float:
        """Ours minus theirs. Positive means we are ahead."""
        return self.my_points - self.opp_points

    def describe(self) -> str:
        if self.opp_team_id is None:
            return "matchup period %d: %.0f points, no opponent (bye)" % (
                self.matchup_period, self.my_points,
            )
        return "matchup period %d: %.0f-%.0f vs %s (%+.0f)" % (
            self.matchup_period, self.my_points, self.opp_points,
            self.opp_name or "team %d" % self.opp_team_id, self.margin,
        )


@dataclass(frozen=True)
class MatchupWindow:
    matchup_period: int
    first_scoring_period: int
    last_scoring_period: int


@dataclass(frozen=True)
class GameRef:
    """One NBA game as it matters to us.

    ``slate_date`` is the *US game date* ESPN groups by; ``tipoff_utc`` is the
    real instant. These differ for west-coast late games and treating the UTC
    date as the slate date invents phantom back-to-backs.
    """

    slate_date: str  # YYYY-MM-DD
    tipoff_utc: datetime
    home_team: ProTeamId
    away_team: ProTeamId

    def opponent_of(self, team: ProTeamId) -> Optional[ProTeamId]:
        if team == self.home_team:
            return self.away_team
        if team == self.away_team:
            return self.home_team
        return None


@dataclass(frozen=True)
class ProGame:
    """One NBA game as the status page needs it: who, when, home or away.

    Distinct from :class:`GameRef`, which the solver uses and which is keyed by
    slate date because that is what a back-to-back is counted in. This one is
    keyed by ESPN's own ``scoringPeriodId`` and carries the side and the
    opponent, because what a reader wants under a player's name is "@DAL, 5:00
    pm" and neither half of that is derivable from a game count.

    ``tipoff_utc`` is ``None`` only when ESPN has not set a time yet, which it
    flags with ``time_tbd``; the two are separate so that a missing timestamp
    and a genuinely undecided one do not render the same.
    """

    scoring_period: int
    pro_team_id: ProTeamId
    opponent_id: ProTeamId
    home: bool
    tipoff_utc: Optional[datetime] = None
    time_tbd: bool = False


@dataclass(frozen=True)
class DayEntry:
    """A roster entry as of one scoring period, with that day's score.

    Composition rather than more fields on :class:`RosterEntry`: that type
    describes a player's standing *now* -- his market, his eligibility, whether
    he may be dropped -- and is what the optimizer reads. A day's points belong
    to the (player, day) pair and to nothing else, and the page asks for them
    about the opponent's roster as readily as about ours.

    ``points`` is ``None`` when ESPN has no scored row for that player on that
    day: he has not played yet, or does not play at all. Zero means he played
    and scored nothing, which is a different and much worse fact.

    ``minutes`` comes off the same row and costs no second request. It is what
    turns a number into a rate: 12.0 in six minutes and 12.0 in thirty-six are
    the same cell and opposite facts.
    """

    player: "RosterEntry"
    points: Optional[float] = None
    minutes: Optional[float] = None


@dataclass(frozen=True)
class Move:
    """A single proposed change. The unit of approval and of journaling."""

    kind: str  # LINEUP | IR | ADD | DROP | ADD_DROP | CANCEL
    player_id: PlayerId
    player_name: str
    from_slot: Optional[SlotId] = None
    to_slot: Optional[SlotId] = None
    paired_player_id: Optional[PlayerId] = None
    paired_player_name: Optional[str] = None
    bid: Optional[int] = None
    #: Human-readable justification, assembled from numbers we already computed.
    #: Never written by an LLM without the numbers being present and checkable.
    rationale: str = ""
    value_delta: float = 0.0

    @property
    def requires_approval(self) -> bool:
        return self.kind in ("ADD", "DROP", "ADD_DROP")


@dataclass(frozen=True)
class RunRecord:
    """Everything needed to explain or replay one run. Persisted verbatim.

    The computed duals (``shadow_prices``) and category weights are stored, not
    just the resulting moves, because when a decision looks wrong the question
    is almost always "what did it think the margin was", and that lives here.
    """

    run_id: str
    started_at: datetime
    scoring_period: int
    sources_used: Mapping[str, str]
    sources_missing: Mapping[str, str]
    shadow_prices: Mapping[SlotId, float]
    category_weights: Mapping[StatId, float]
    moves: tuple
    shadow_mode: bool
    notes: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# the player card
# ---------------------------------------------------------------------------
#
# What ESPN shows when you click a name in its own tables, rebuilt here because
# that card is client state with no URL of its own -- in ESPN's markup the name
# is an ``<a>`` carrying no ``href``, and the address bar does not move when it
# opens. See ``web.FANTASY_URL``.
#
# These four types are *display* types. Nothing in ``solve/`` reads them, and
# they deliberately carry ESPN's own applied fantasy points rather than a
# number recomputed here: the card exists to be checked against ESPN's, so a
# rounding difference between the two would read as a bug in the app even when
# the app was right.


@dataclass(frozen=True)
class StatSplit:
    """One row of the Stats table: a window, averaged, plus its fantasy total.

    ``fantasy_total`` is the sum over the window and ``fantasy_average`` the
    per-game figure, because ESPN's card shows the first in the table and the
    second in the header, and showing either one in both places makes the two
    disagree with each other.
    """

    key: str
    label: str
    games: int
    minutes: float = 0.0
    points: float = 0.0
    rebounds: float = 0.0
    assists: float = 0.0
    steals: float = 0.0
    blocks: float = 0.0
    turnovers: float = 0.0
    fantasy_total: float = 0.0
    fantasy_average: float = 0.0


@dataclass(frozen=True)
class GameLogRow:
    """One game he actually played.

    A scheduled game he did not play carries an empty stat dict from ESPN and
    is dropped rather than rendered as a line of zeroes -- "did not play" and
    "played and scored nothing" are different facts, and only the second one
    belongs in an average.

    ``pro_team`` is the team he played this game *for*, which is why the
    opponent is not resolved here: a traded player's fixtures are only right
    if each one is looked up against the team on its own row, and that lookup
    needs the pro schedule, which this module does not have. The view layer
    joins the two.
    """

    scoring_period: int
    pro_team: ProTeamId = 0
    minutes: float = 0.0
    points: float = 0.0
    rebounds: float = 0.0
    assists: float = 0.0
    steals: float = 0.0
    blocks: float = 0.0
    turnovers: float = 0.0
    fantasy_points: float = 0.0


@dataclass(frozen=True)
class NewsItem:
    """One Rotowire note: what happened, and what ESPN makes of it.

    ``spin`` is the analysis paragraph. It is the half worth reading -- the
    headline is usually the box score line, which the game log above it
    already shows.
    """

    published: Optional[datetime] = None
    source: str = ""
    headline: str = ""
    spin: str = ""


@dataclass(frozen=True)
class PlayerCard:
    """Everything the card shows about one player in one league.

    ``manager`` is the fantasy team that rosters him, or an empty string when
    nobody does -- the field answers "can I have him", which is the question a
    name on this app's pages is usually being clicked to settle.
    """

    player_id: PlayerId
    name: str
    pro_team_id: ProTeamId = 0
    positions: tuple = ()
    injury_status: str = "ACTIVE"
    position_rank: Optional[int] = None
    percent_owned: Optional[float] = None
    acquisition_status: str = ""
    on_team_id: int = 0
    manager: str = ""
    outlook: str = ""
    splits: tuple = ()
    game_log: tuple = ()
    news: tuple = ()


@dataclass(frozen=True)
class LiveGame:
    """One NBA game that has tipped off, from ESPN's public scoreboard.

    A display type, like the player-card types above it: nothing in ``solve/``
    reads it, because where a game is has no bearing on who should be started
    tomorrow. It exists so that a roster row showing 12.0 can also say whether
    that is four minutes of work or thirty-four.

    ``score`` is the team this row is indexed under -- the mapping holds both
    sides of a game, each from its own point of view -- so a player's own team
    is always the first number without the caller working out which side he is
    on. ``detail`` is ESPN's own phrase ("Halftime", "End of 2nd"), kept
    because a period number cannot express either of those.
    """

    pro_team_id: ProTeamId
    final: bool = False
    period: int = 0
    clock: str = ""
    detail: str = ""
    score: Optional[int] = None
    opp_score: Optional[int] = None
