"""Exact max-weight assignment of players to lineup slots.

Why exact and not greedy
------------------------
Greedy (sort players by value, drop each into its best open slot) is what every
public fantasy-lineup tool ships, and it is wrong in a way that is easy to
demonstrate. Slots ``{G, C}``, two players: Alice is G/C-eligible worth 30, Bob
is C-only worth 25. Greedy takes Alice first, puts her in G (or C), and then
either strands Bob or strands the other slot -- one of the two slots goes empty
and the lineup scores 30. The optimum starts Bob at C and Alice at G for 55.

Note the failure mode: greedy can start *strictly fewer players* than optimal,
not merely a worse set. On a GP-capped roster that is doubly bad, since an empty
slot is a wasted day that the cap will not give back.

Formulation
-----------
Max-weight bipartite matching, with **both constraints as ``<=``**:

    maximize    sum_{p,s} v[p,s] * x[p,s]
    subject to  sum_s x[p,s] <= 1          for each player   (may sit)
                sum_p x[p,s] <= cap[s]     for each slot     (may go empty)
                x binary

The inequalities are load-bearing. Benching is free, and when every eligible
player for a slot has negative marginal value (see ``budget.py``) leaving that
slot empty is the correct answer. An equality formulation cannot express that
and will start someone it should not.

The constraint matrix is totally unimodular, so the LP relaxation is integral
and we can solve the LP exactly by combinatorial means -- no MILP solver, and
no branch-and-bound.

Implementation
--------------
Jonker-Volgenant / Kuhn-Munkres with potentials, O(n^3), pure Python. Slots are
expanded into one column per unit of capacity, then the rectangular problem is
squared off with zero-weight dummies so that "unassigned" is representable as a
real matching edge. Instances are ~15 players x ~13 slot-units, i.e. a 28x28
matrix: tens of microseconds. ADR-001 -- this is why there is no scipy
dependency, and therefore no ``musllinux_1_2_aarch64`` wheel risk on the HA
Green's Cortex-A55 cores.

Ineligibility is expressed by **absence from the weights mapping**, never by a
weight of zero. Zero is a legitimate value (a player who will score nothing is
not the same as a player who cannot occupy the slot), and conflating them is how
you end up starting a center at point guard.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PlayerId = int
SlotId = int

__all__ = [
    "max_weight_assignment",
    "assignment_value",
    "greedy_assignment",
]


def max_weight_assignment(
    weights: Mapping[Tuple[PlayerId, SlotId], float],
    slot_capacity: Mapping[SlotId, int],
    *,
    locked: Optional[Mapping[PlayerId, SlotId]] = None,
) -> Dict[PlayerId, SlotId]:
    """Return the value-maximizing player -> slot map.

    Args:
        weights: ``(player, slot) -> value``. A pair that is absent is
            *forbidden*. Only pairs present here can appear in the result.
        slot_capacity: ``slot -> number of openings``. Slots absent or with
            capacity <= 0 are unusable.
        locked: players whose slot cannot change this run (ESPN
            ``lineupLocked``: their game has started). These are pinned, their
            capacity is consumed, and they are removed from the optimization.

    Returns:
        ``player -> slot`` for assigned players only. Players omitted from the
        result are benched, which is always a feasible and sometimes optimal
        outcome. Pinned players are included.

    Raises:
        ValueError: if a locked assignment is itself infeasible (over-capacity
            or duplicated), which indicates our view of the roster disagrees
            with ESPN's and means we must re-read rather than write.
    """
    capacity: Dict[SlotId, int] = {
        s: int(c) for s, c in slot_capacity.items() if int(c) > 0
    }
    result: Dict[PlayerId, SlotId] = {}

    # --- 1. Pin locked players and consume their capacity -------------------
    if locked:
        for pid, slot in locked.items():
            if capacity.get(slot, 0) <= 0:
                raise ValueError(
                    "locked player %s occupies slot %s which has no remaining "
                    "capacity; our roster view disagrees with ESPN" % (pid, slot)
                )
            capacity[slot] -= 1
            if capacity[slot] == 0:
                del capacity[slot]
            result[pid] = slot

    locked_ids = set(locked or ())

    # --- 2. Build the rectangular problem over free players/openings --------
    players: List[PlayerId] = sorted(
        {p for (p, s) in weights if p not in locked_ids and s in capacity}
    )
    # One column per unit of capacity. ``columns[j]`` is the slot that column
    # j represents; several columns can share a slot.
    columns: List[SlotId] = []
    for slot in sorted(capacity):
        columns.extend([slot] * capacity[slot])

    if not players or not columns:
        return result

    n_rows, n_cols = len(players), len(columns)
    size = n_rows + n_cols  # square padding: every row and column gets a dummy

    # ``forbid`` must dominate any achievable alternative so the optimizer
    # never prefers an illegal pair. Scaling it off the actual weight mass
    # keeps it finite (the JV inner loop needs finite arithmetic) while making
    # it provably worse than benching everyone.
    total_mass = sum(abs(v) for v in weights.values())
    forbid = total_mass * (size + 1.0) + 1.0

    # Min-cost square matrix. cost = -value; dummies cost 0 (free to bench, free
    # to leave a slot empty); forbidden real pairs cost +forbid.
    cost: List[List[float]] = [[0.0] * size for _ in range(size)]
    for i, pid in enumerate(players):
        for j, slot in enumerate(columns):
            v = weights.get((pid, slot))
            cost[i][j] = forbid if v is None else -float(v)

    col_of_row = _jv_min_cost_assignment(cost)

    for i, pid in enumerate(players):
        j = col_of_row[i]
        if j < n_cols and cost[i][j] < forbid:
            result[pid] = columns[j]
    return result


def assignment_value(
    assignment: Mapping[PlayerId, SlotId],
    weights: Mapping[Tuple[PlayerId, SlotId], float],
) -> float:
    """Total value of an assignment. Raises on an illegal pair."""
    total = 0.0
    for pid, slot in assignment.items():
        v = weights.get((pid, slot))
        if v is None:
            raise ValueError("illegal pair in assignment: %s -> %s" % (pid, slot))
        total += v
    return total


def greedy_assignment(
    weights: Mapping[Tuple[PlayerId, SlotId], float],
    slot_capacity: Mapping[SlotId, int],
) -> Dict[PlayerId, SlotId]:
    """The naive algorithm, kept **only** as a test baseline.

    Not used in production. Its presence is deliberate: the regression test
    asserts that the exact solver beats it on the known counterexample, so if
    someone later "simplifies" the solver the test fails loudly.
    """
    best: Dict[PlayerId, float] = {}
    for (pid, _slot), v in weights.items():
        if v > best.get(pid, float("-inf")):
            best[pid] = v
    remaining = {s: int(c) for s, c in slot_capacity.items() if int(c) > 0}
    out: Dict[PlayerId, SlotId] = {}
    for pid in sorted(best, key=lambda p: (-best[p], p)):
        options = [
            (v, s)
            for (p, s), v in weights.items()
            if p == pid and remaining.get(s, 0) > 0
        ]
        if not options:
            continue
        v, slot = max(options, key=lambda t: (t[0], -t[1]))
        out[pid] = slot
        remaining[slot] -= 1
    return out


# ---------------------------------------------------------------------------
# Jonker-Volgenant shortest-augmenting-path assignment, O(n^3).
# ---------------------------------------------------------------------------


def _jv_min_cost_assignment(cost: Sequence[Sequence[float]]) -> List[int]:
    """Minimum-cost perfect assignment on a square matrix.

    Returns ``col_of_row`` with one column index per row. All entries of
    ``cost`` must be finite -- the potential updates do arithmetic on them, and
    an infinity poisons ``delta``. Forbidden pairs therefore use a large finite
    sentinel chosen by the caller.
    """
    n = len(cost)
    if n == 0:
        return []
    inf = float("inf")

    # 1-indexed with a virtual row/column 0, which is what makes the augmenting
    # path bookkeeping below compact.
    u = [0.0] * (n + 1)  # row potentials
    v = [0.0] * (n + 1)  # column potentials
    match = [0] * (n + 1)  # match[j] = row matched to column j (0 = free)
    way = [0] * (n + 1)  # way[j] = predecessor column on the augmenting path

    for i in range(1, n + 1):
        match[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)

        # Grow a shortest-path tree from row i until it reaches a free column.
        while True:
            used[j0] = True
            i0 = match[j0]
            delta = inf
            j1 = 0
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            # Reduced costs stay non-negative: shift potentials by delta.
            for j in range(n + 1):
                if used[j]:
                    u[match[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if match[j0] == 0:
                break

        # Walk the path back, flipping matched/unmatched edges.
        while j0:
            j1 = way[j0]
            match[j0] = match[j1]
            j0 = j1

    col_of_row = [0] * n
    for j in range(1, n + 1):
        if match[j]:
            col_of_row[match[j] - 1] = j - 1
    return col_of_row
