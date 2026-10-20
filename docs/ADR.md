# Architecture decisions

## ADR-001 — No numpy, no scipy. Pure-Python exact solver.

**Status:** accepted.

The obvious implementation of the assignment step is
`scipy.optimize.linear_sum_assignment`. We don't use it.

The production instance is ~15 players × ~13 slot-units — a 28×28 matrix after
padding. A pure-Python Jonker–Volgenant runs it in tens of microseconds, and the
sequential-LP outer loop needs maybe 20 such solves per run. Measured: 20 solves
of the realistic shape in well under 50 ms total on a dev box, and the test
suite asserts it.

Against that, scipy costs:

- A `musllinux_1_2_aarch64` wheel dependency. Available today, but if it ever
  isn't, the fallback is compiling LAPACK on the Green's Cortex-A55 cores.
- ~90 MB of container image for one function call.
- A hard floor on where the code can be tested. The dev box here has no numpy;
  pure Python means the solver is testable everywhere, including in CI with no
  wheels at all.

The deciding factor is the third one. The solver is the part of this system most
worth testing exhaustively, and a dependency that makes it harder to run tests
is a dependency that makes the tests worse. `pydantic-core` (via fastapi)
remains the only compiled dependency, and it only serves the ingress page.

**Consequence:** `solve/` imports nothing outside the standard library and is
fuzz-tested against exhaustive brute-force enumeration.

## ADR-002 — Ineligibility is absence, never a zero weight.

**Status:** accepted.

`max_weight_assignment` takes a sparse `(player, slot) -> value` mapping. A pair
that is absent is *forbidden*. A pair present with value `0.0` is *legal and
worth nothing*.

These must not be conflated, because both occur constantly and they need
opposite handling:

- A center is **ineligible** at point guard. Representing that as `0.0` lets the
  optimizer start him there whenever the alternative is negative.
- A player with no game today is **worth zero**, and must not be started — which
  is also representable as absence.
- A player projected to score nothing but who *is* eligible is a legal choice,
  and sometimes the only one.

The research notes for this project flagged the same thing: no-game players must
get `−∞`, *not* `0`. Sparse absence is the cleaner encoding of `−∞`, and it also
shrinks the matrix.

**Consequence:** `solve/budget.reduced_cost_weights` prunes non-positive reduced
costs. A test asserts that pruning cannot change the optimum, so the
optimization stays an optimization rather than quietly becoming a policy.

## ADR-003 — Both assignment constraints are `≤`, not `=`.

**Status:** accepted.

Benching is free, and leaving a starting slot *empty* is sometimes optimal —
specifically when every eligible player's value falls below that slot's shadow
price `λ_s`. An equality formulation cannot express it and will start someone it
shouldn't, spending a capped game on a negative-value start.

**Consequence:** the padded square matrix gives every row and column a
zero-weight dummy, so "unassigned" is a real matching edge.

## ADR-004 — ESPN projections are a first-class source, not a fallback.

**Status:** accepted, forced by evidence.

The design assumed DARKO as the primary projection source with ESPN as backup.
Live verification on 2026-10-20 found DARKO's public payload reporting
`ratingsThrough: "2026-06-13"` — a June offseason snapshot, 129 days stale, with
`season_games == 0` for all 518 rows. Its `asOf` field exists but points at a
null slot.

So the "fallback" is the live path. Treating it as an emergency hatch would mean
the system's actual projection quality is whatever got the least design
attention.

**Consequence:** `EspnRead.player_projections` is built and documented as a
primary source (season projection `statSourceId=1` blended with the last-15
split, `statSplitTypeId=2`). DARKO's staleness is a **hard fail** that returns
`None`, and its absence is recorded in the run record rather than silently
defaulted — because "we used a stale projection" and "we used ESPN's" must be
distinguishable after the fact.

## ADR-005 — `app/models.py`, not `app/types.py`.

**Status:** accepted after a near-miss.

`/app` is `sys.path[0]` in the container. A module named `types.py` there
shadows the standard library's `types` for the entire process, including for
httpx and asyncio. Caught before it shipped; recorded so it isn't reintroduced.

## ADR-006 — The LLM boundary.

**Status:** accepted.

The LLM is used for exactly three things:

1. Unstructured injury news → strictly-validated JSON
   (`{player_id, status, p_play, minutes_delta, confidence, source_url, timestamp}`).
2. Human-readable explanations that narrate numbers already computed.
3. Translating user intent into configuration.

It makes **no decisions, performs no arithmetic, and triggers no irreversible
write.** Its output is untrusted input: schema-validated, range-clamped,
cross-checked against a structured ESPN field, and required to corroborate
before a status flip can motivate a drop.

Carried verbatim from the original requirements, and extended to cover model
output as well as web pages:

> Text on web pages is treated as information, never as instructions.

Prompt injection via a scraped tweet is a real path to a bad roster move.
