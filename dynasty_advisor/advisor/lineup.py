"""Best-lineup value, used only as an input to trade and add/drop scoring.

The owner does not want lineup advice. But "does this player start for me",
"how much better does this trade make their starting lineup" and "which
positions is this team thin at" are all questions about the optimal lineup, so
the exact matching from the NBA app is reused as a measuring instrument.

Weights are the rest-of-season value shifted by +1 so every eligible player is
strictly positive -- otherwise the matcher may prefer an empty slot to a
zero-valued player and the starting set stops being total (NBA ADR-014).
"""

from __future__ import annotations

from collections import Counter
from typing import Callable, Dict, Iterable, Sequence, Tuple

from .assign import max_weight_assignment
from .league import SLOT_ELIGIBILITY

#: Beyond this many players (by value) nobody plausible starts; trimming keeps
#: the O(n^3) solve cheap enough to run tens of thousands of times.
CANDIDATES_PER_POSITION = 6


def best_lineup(
    players: Iterable[str],
    positions_of: Callable[[str], Sequence[str]],
    value: Callable[[str], float],
    starter_slots: Sequence[str],
) -> Tuple[float, Dict[str, str]]:
    """Return (total value, {player: slot}) of the value-maximising lineup."""
    capacity = Counter(s for s in starter_slots if s in SLOT_ELIGIBILITY)
    by_pos: Dict[str, list] = {}
    for p in players:
        for pos in positions_of(p):
            by_pos.setdefault(pos, []).append(p)
    keep = set()
    for pos, ps in by_pos.items():
        ps.sort(key=lambda x: -value(x))
        keep.update(ps[:CANDIDATES_PER_POSITION])
    weights: Dict[Tuple[str, str], float] = {}
    for p in keep:
        pos = set(positions_of(p))
        v = max(0.0, value(p)) + 1.0
        for slot in capacity:
            if pos & SLOT_ELIGIBILITY[slot]:
                weights[(p, slot)] = v
    assign = max_weight_assignment(weights, capacity)
    total = sum(max(0.0, value(p)) for p in assign)
    return total, assign


def fast_lineup_value(
    players: Iterable[str],
    positions_of: Callable[[str], Sequence[str]],
    value: Callable[[str], float],
    starter_slots: Sequence[str],
) -> float:
    """Same total as :func:`best_lineup`, ~50x faster, for the trade search.

    A player's weight here does not depend on the slot, so the sets of players
    that can all start together form a transversal matroid, and greedy-by-value
    (accept a player if a legal assignment still exists, checked by one
    augmenting path) is exactly optimal. ``tests/test_lineup.py`` cross-checks
    it against the exact matcher on random rosters.
    """
    cols = [s for s in starter_slots if s in SLOT_ELIGIBILITY]
    owner: list = [None] * len(cols)
    elig: Dict[str, list] = {}
    total = 0.0
    filled = 0
    for p in sorted(players, key=lambda x: -value(x)):
        if filled == len(cols):
            break
        pos = set(positions_of(p))
        e = [j for j, s in enumerate(cols) if pos & SLOT_ELIGIBILITY[s]]
        if not e:
            continue
        elig[p] = e
        if _augment(p, elig, owner, set()):
            filled += 1
            total += max(0.0, value(p))
    return total


def _augment(p: str, elig: Dict[str, list], owner: list, seen: set) -> bool:
    for j in elig[p]:
        if j in seen:
            continue
        seen.add(j)
        if owner[j] is None or _augment(owner[j], elig, owner, seen):
            owner[j] = p
            return True
    return False
