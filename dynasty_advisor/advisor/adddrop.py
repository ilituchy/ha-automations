"""Add/drop recommendations.

In dynasty a pickup is judged on two clocks at once: does he help the
rest-of-season lineup, and is he worth a roster spot for the years after. The
score blends both by strategy, the same weights the trade search uses.

Inputs per candidate: FantasyCalc dynasty + rest-of-season value, Sleeper's
trending adds (league-wide demand across all Sleeper leagues), depth chart
order and injury status. A free agent is any player not on any roster in
this league.

Roster mechanics come first: a player who is already IR-eligible under this
league's rules but sits on the active roster is costing a spot for nothing,
so "move X to IR" is listed before any add, because it may make the drop
unnecessary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

from .assets import Asset, AssetBook
from .league import League
from .lineup import fast_lineup_value
from .trades import STRATEGIES

STARTABLE = {"QB", "RB", "WR", "TE"}
SWAP_MARGIN = 350.0


@dataclass
class Candidate:
    asset: Asset
    score: float
    now_delta: float
    trending: int
    depth: Optional[int]
    injury: Optional[str]
    nfl_team: Optional[str]


@dataclass
class AddDropReport:
    ir_moves: List[Asset] = field(default_factory=list)  # IR-eligible, sitting on the active roster
    reserve_open: int = 0
    active_count: int = 0
    active_max: int = 0
    faab_left: Optional[int] = None
    adds: List[Candidate] = field(default_factory=list)
    trending_unvalued: List[Candidate] = field(default_factory=list)
    starters: frozenset = frozenset()
    drops: List[Candidate] = field(default_factory=list)
    swaps: List[tuple] = field(default_factory=list)  # (add, drop or None, gain)
    open_spots: int = 0  # active spots free once the IR moves are made


def recommend(
    league: League,
    book: AssetBook,
    me: int,
    trending_adds: Sequence[Mapping],
    strategy: str = "balanced",
    n: int = 8,
) -> AddDropReport:
    w_dyn, w_now, _y, _v = STRATEGIES[strategy]
    team = league.teams[me]
    slots = league.starter_slots
    idx = book.index
    trend = {str(t.get("player_id")): int(t.get("count") or 0) for t in trending_adds}
    rep = AddDropReport()

    # --- roster mechanics ------------------------------------------------
    allowed = league.reserve_statuses()
    rep.reserve_open = int(league.settings.get("reserve_slots") or 0) - len(team.reserve)
    # List every eligible player, longest absences (IR/PUP) first; the owner
    # picks which ones fill the open spots.
    severity = {"IR": 0, "PUP": 0, "Out": 1}
    rep.ir_moves = sorted(
        (book.player(p) for p in team.active if (idx.get(p) or {}).get("injury_status") in allowed),
        key=lambda a: (severity.get((idx.get(a.key) or {}).get("injury_status"), 2), a.dyn),
    )
    rep.active_count = len(team.active)
    rep.active_max = league.max_active
    budget = league.settings.get("waiver_budget")
    if budget:
        rep.faab_left = int(budget) - team.faab_used

    ir_now = {a.key for a in rep.ir_moves[: max(0, rep.reserve_open)]}
    active = frozenset(team.active) - ir_now
    base = fast_lineup_value(active, book.positions, book.values.now, slots)
    rep.starters = frozenset(team.starters)

    # One-for-one roster moves compare RAW dynasty value: the roster spot is
    # exactly what is being exchanged, so value-over-replacement (which zeroes
    # most free agents) would hide the difference that matters.
    def cand(pid: str, now_delta: float) -> Candidate:
        a = book.player(pid)
        info = idx.get(pid) or {}
        t = trend.get(pid, 0)
        score = w_dyn * a.dyn + w_now * now_delta
        return Candidate(a, score, now_delta, t, info.get("depth_chart_order"),
                         info.get("injury_status"), info.get("team"))

    # --- free agents -----------------------------------------------------
    rostered = league.rostered()
    pool = set(book.unrostered_values()) | {p for p in trend if p not in rostered}
    adds: List[Candidate] = []
    for pid in pool:
        info = idx.get(pid) or {}
        if not info.get("active") or not info.get("team"):
            continue
        if not set(book.positions(pid)) & STARTABLE:
            continue
        now = fast_lineup_value(active | {pid}, book.positions, book.values.now, slots) - base
        c = cand(pid, now)
        if c.asset.dyn > 0 or now > 0:
            adds.append(c)
        elif c.trending:
            # Sleeper trending counts are cumulative across every Sleeper
            # league (millions), so they are shown, never scored: a role change
            # the value feed has not priced yet, worth a human look.
            if c.depth in (1, 2):
                rep.trending_unvalued.append(c)
    adds.sort(key=lambda c: -c.score)
    rep.adds = adds[:n]
    rep.trending_unvalued.sort(key=lambda c: -c.trending)
    rep.trending_unvalued = rep.trending_unvalued[:5]

    # --- drop candidates: the cheapest active players to lose -------------
    drops: List[Candidate] = []
    for pid in active:
        if not set(book.positions(pid)) & STARTABLE:
            continue  # K/DEF are unvalued by the feed; never auto-suggest dropping them
        now = fast_lineup_value(active - {pid}, book.positions, book.values.now, slots) - base
        c = cand(pid, now)
        # Cost of losing him: his value PLUS the lineup value that goes with
        # him. ``now`` is <= 0 here, so it is subtracted. (Adding it made the
        # most important starters look cheapest -- caught on the HA page.)
        c.score = w_dyn * c.asset.dyn - w_now * now
        drops.append(c)
    drops.sort(key=lambda c: c.score)
    rep.drops = drops[:n]

    # --- moves: free spots first, then swaps only when clearly better -----
    rep.open_spots = max(0, league.max_active - len(active))
    adds_left = list(rep.adds)
    for _ in range(rep.open_spots):
        if adds_left:
            add = adds_left.pop(0)
            rep.swaps.append((add, None, add.score))
    used = set()
    for add in adds_left:
        for drop in rep.drops:
            if drop.asset.key in used:
                continue
            both = fast_lineup_value(active - {drop.asset.key} | {add.asset.key},
                                     book.positions, book.values.now, slots) - base
            gain = w_dyn * (add.asset.dyn - drop.asset.dyn) + w_now * both
            # Fringe dynasty values (~1,000) are noisy; demand a real gap, and
            # never weaken the current lineup to make the swap.
            if gain > SWAP_MARGIN and both >= 0:
                rep.swaps.append((add, drop, gain))
                used.add(drop.asset.key)
            break
    return rep
