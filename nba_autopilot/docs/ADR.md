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

## ADR-007 — A league is the unit of optimization. Nothing is pooled.

**Status:** accepted.

Two real leagues are in play, and the shape of that changes the data model,
not just a loop bound. (The ids themselves live in the app's options, not in
this repository -- they aren't credentials, but they aren't anyone else's
business either, and the tests prove nothing with real ones that they don't
prove with `111111111`.)

The tempting read is "one account, two rosters" — share the optimizer, sum the
games-played budget, rank all 30 players together. Every part of that is wrong:

- **Category weights are league-local.** `w_k = φ(z_k)/s_k` is derived from the
  *opponent distribution in that league*. A league where everyone punts FT% has
  a different gradient than one where nobody does. Averaging the two produces
  weights that are optimal for neither.
- **The games-played cap is per-league** and often configured differently.
  Spending it against a pooled budget overspends in one league and underspends
  in the other.
- **A player can be on both rosters.** Pooled ranking would treat the two
  copies as fungible, so a start/bench decision in league A would be able to
  justify itself with league B's schedule.

What *is* shared is the cookie (one ESPN account), the HTTP client (same hosts,
so httpx can reuse connections), the notification channel, and the database.

**Consequences:**
- `Config.leagues: List[LeagueRef]`; a `League` object owns its own `EspnRead`,
  `EspnWrite` and optimizer state, and borrows the http client and store.
- `run_once()` loops leagues and catches per-league. Skipping league B's lineup
  because league A's roster fetch timed out would be a cost with no benefit.
- `runs` and `journal` carry `league_id`.
- Every proposal names its league. A notification reading only *"drop Smith"*
  is unactionable when two teams are in play.
- `league_for_run()` returns `None` rather than defaulting to the first league.
  Writing to the wrong league is exactly what the ownership check exists to
  prevent; a silent fallback would route around that check instead of tripping
  it.
- Duplicate `league_id` in options is a `ConfigError`, not a dedup. Two
  `team_id` values for one league means one of them is a team the user does not
  own — the most dangerous misconfiguration available here, because ESPN
  honours a write to a leaguemate's roster with `200 EXECUTED`.
- `EspnAuthError` during startup checks aborts immediately rather than
  continuing to the next league: one cookie serves every league, so the failure
  is global and retrying it per league just multiplies the 401s.

The app also moved into a shared `ha-automations` repository at
`nba_autopilot/`, since it's the first of several intended automations. HA app
repositories are already one-directory-per-app, so this costs nothing
structurally; `docs/` and `tests/` moved under the app because they document
and test the app, while `tools/` stayed at the root because it talks to Home
Assistant rather than to any one app.

## ADR-008 — One module owns the notification action-id format.

**Status:** accepted after the approval path was found to be non-functional.

Approval ids were built in one place and parsed in another, and the two formats
were never reconciled, because nothing in the codebase yet constructs a real
proposal. The bug was invisible to the test suite and would have been invisible
in production too, in the worst way:

`main.handle_action` read the verb as `parts[-1]` and then passed the *whole*
tapped `action_id` to `store.decide()`, which matches `WHERE action_id = ?`
against the stored `<run>:<kind>:<seq>`. The tapped id carries a `:<VERB>`
suffix, so it matched zero rows — and in the format without the suffix,
`parts[-1]` is the sequence number, so `verb == "APPROVE"` is `False` and every
tap becomes a rejection.

So: taps did nothing, and the audit log said the user had rejected the move.
It failed closed, which is the right direction to fail, but "the approve button
is decorative" is not a state a system whose entire safety story is *ask before
anything irreversible* can be in.

The lesson isn't "write a test for handle_action". It's that a wire format with
two independent implementations has no single definition, and this one spans a
process boundary (the id travels to an iPhone and comes back), so there's
nothing to type-check it.

**Consequences:** `ha/bridge.py` exports the builders and the parser —
`item_action_id`, `bulk_action_id`, `parse_action_id` — and a frozen
`ParsedAction` carrying `run_id`, `verb` and `proposal_action_id` (`None` for
bulk taps). The verb belongs to the button, not to the proposal, and conflating
them means the database lookup silently matches nothing. The builders reject a
verb from the wrong class. Seven round-trip tests assert that a stored id is
recovered from the tapped id, that the raw tapped id matches nothing, that
`REJECT` is not silently an approval, and that ten malformed or foreign ids are
refused.

Foreign ids log at `debug`, not `warning`: every notification action on the
instance arrives on the same `mobile_app_notification_action` event bus, so
other integrations' taps are expected traffic, not anomalies.

## ADR-009 — Supervisor is scriptable over WebSocket, not over REST.

**Status:** accepted, forced by the live instance.

Verified on 2026-10-20 against HA 2026.10.2 with a long-lived access token
belonging to the owner (admin, owner): every `/api/hassio/*` path returns
**401**, including `supervisor/info`, `addons`, `store/repositories` and
`core/info`. The token is not the problem — `/api/config/config_entries/entry`
returns 200 with 20 entries on the same token. The `hassio` REST proxy simply
does not accept long-lived tokens.

The WebSocket `supervisor/api` command, on the same token, works.

**Consequence:** `tools/hactl.py` speaks raw RFC 6455 (stdlib only — no
machine in this project has `websockets`, `aiohttp` or `websocket-client`, and
verifying the approval round-trip requires an actual socket to receive the
`mobile_app_notification_action` event on). Anything Supervisor-related goes
through `hactl.py ws '{"type":"supervisor/api",...}'`, and `bridge.py`'s
`cloud_status()` is synchronous on purpose because it runs from
`startup_checks` before the event loop exists.

The token lives in `~/.ha_token` (mode 600) and is read from the file, never
passed on a command line — argv is visible in `ps`.
