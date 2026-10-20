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

    def expected(self) -> Dict[StatId, float]:
        """Availability-weighted mean. Safe to call regardless of provenance."""
        if self.p_play_applied:
            return dict(self.mean)
        return {k: v * self.p_play for k, v in self.mean.items()}

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

    @property
    def availability(self) -> str:
        return self.injury_status if self.injury_status in P_PLAY_PRIOR else "ACTIVE"


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
