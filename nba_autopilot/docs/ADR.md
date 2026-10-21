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

## ADR-010 — Both leagues are points leagues. The category gradient stays anyway.

The handover spec, and every line of `solve/weights.py`, assumes head-to-head
**categories**: the objective `J = Σ_k Φ(z_k)`, weights `w_k = φ(z_k)/s_k`,
punting and clinching as emergent behaviours of the gradient. That machinery is
the most interesting part of the design.

Live discovery on 2026-10-20 says neither of the two leagues this app was built
for is a category league. Both report `scoringType: H2H_POINTS`, with seventeen
identical scoring items — points 1.0, steals 2.0, turnovers −1.0, double-double
4.0, triple-double 8.0, quadruple-double 15.0, and so on.

In a points league there is exactly one category, and it is the score. So:

* **The value function collapses to a dot product.** `v_p = Σ_k c_k · μ_pk`
  with `c_k` read straight off `scoringItems`. No z-scores, no `s_k`, nothing
  to estimate — ESPN *publishes* the exchange rate between a steal and a
  turnover.
* **There is no punting**, because there is nothing to punt. The behaviour the
  gradient produced for free has no analogue here.
* **The mean objective is exactly linear**, not locally linear. This is
  strictly stronger than the category case: there, weights depend on the
  projected margin, so the linearization is only a local model and every
  accept/reject in the outer loop has to be re-checked against the true
  `Σ_k Φ(z_k)` or the search can cycle between two lineups forever. Here the
  mean term needs no such guard.
* **Risk survives intact.** A points league is the degenerate one-category
  league, so `dP/dσ² = −(Δ/2s²)·w` still holds with "the category" being
  fantasy points: trailing means seek variance, leading means avoid it. This is
  implemented by building *one* `CategoryState` over projected point totals,
  not by writing a second risk model.

**Decision:** add `solve/value.py` as the single place the two league types
branch, and keep `solve/weights.py` in full. Deleting the category path would
be the wrong call for two reasons. It is the harder of the two models and would
have to be rebuilt from scratch for any future league; and the points path
*reuses* its risk machinery rather than merely sitting beside it, so the code is
load-bearing today even though no league is category-scored.

The branch happens once, in `build_value_model`, on ESPN's own `scoringType` —
never on a flag we set. Everything downstream — the assignment, the shadow
price — is indifferent to where values came from.

One guard falls out of this. A points value is a sum over games, which is only
valid for counting stats. A league that awarded points for FG% or for assists
*per game* could not be valued this way at all: summing a per-game average over
a four-game week inflates it fourfold, and summing a percentage is meaningless
at any horizon. Neither observed league does this — all seventeen items are
counting stats — so `ids.NON_ADDITIVE_STATS` and the `UnsupportedScoring` raise
cost nothing today. They exist because the failure they prevent is a *plausible
looking* ranking that is wrong about every player, which is the worst ratio of
damage to detectability available.

## ADR-011 — The games-played cap is one pooled weekly budget, not per-slot seasonal.

`solve/budget.py` was written against the GP cap as the spec described it: a
**per-lineup-slot, season-long integer**, giving one multiplier `λ_s` per slot.
The spec flagged this as its single largest open question. It was wrong on all
three counts.

Both live leagues send exactly one entry, keyed at slot **15** — a slot that
appears nowhere in either league's `lineupSlotCounts` — carrying `statId: 42`
(games played) and a *fractional* `limitValue`, alongside
`matchupLimitPerScoringPeriod: true`:

| league | starting slots | `limitValue` | `× 7` | per starter |
|---|---|---|---|---|
| A | 10 | 5.714286 = 40/7 | 40 | 4.000 |
| B | 11 | 6.285714 = 44/7 | 44 | 4.000 |

Three readings of this, and what settles between them:

1. *A cap on a Rookie slot* (slot 15's name in the position map). Rejected: the
   value **scales with starter count** across two independently-configured
   leagues. A cap attached to one slot would not track another league's roster
   shape, and 40 vs 44 against 10 vs 11 starters is exactly four per starter in
   both.
2. *A season-long total of 5.71.* Rejected as absurd on its face, and by
   `matchupLimitPerScoringPeriod: true`.
3. *A pooled team-wide per-scoring-period figure, renewing each matchup
   period.* Everything fits: the exact sevenths are a weekly number divided by
   seven days, and the acquisition limits in the same payload are divided the
   same way (5/7 and 7/7 against `matchupAcquisitionLimit`).

**Confirmed against the ESPN settings page on 2026-10-20**, which is why this
section states rather than hedges. Both leagues render a *Games Played Limits
(Maximums)* table whose only row is labelled **“All Players”** — 40 and 44
respectively. "All Players" is the pooled reading in ESPN's own words: there is
no per-position breakdown, so reading 1 is dead on the page as well as in the
payload. The period is settled by elimination, since a *season* cap of 40 games
for a whole roster would mean fielding about four players a week for six
months.

One correction the page forces: the `× 7 ÷ starters = 4.000` coincidence is not
a rule. The settings page shows a flat number a commissioner picked (or left at
ESPN's default, which evidently scales with roster size). It was good evidence
*against* the per-slot reading and is no evidence at all about how the number
is chosen, so nothing in the code derives the cap from starter count — it is
read, not computed.

**Decision:** model it as reading 3 — one scalar `λ`, priced over the remaining
days of the current matchup period, recovered by multiplying by
`ids.SCORING_PERIODS_PER_MATCHUP`.

The runtime checks stay in despite the confirmation. `remaining_budget` logs at
ERROR if observed usage exceeds the cap it computed, which is precisely what
would happen in week two if the cap were in fact season-long; the alternative —
silently clamping — would hide the one observation that could correct the
model. Keeping a falsification test after the hypothesis is confirmed is cheap,
and the confirmation came from a rendered web page rather than from the API
this code actually reads: a settings page can be redesigned, and ESPN can
change what it sends, independently of each other. `matchup_cap` likewise
*raises* on
limits keyed at real lineup slots instead of coercing them, because the pooled
and per-slot readings differ by an order of magnitude in how much they restrict
and guessing permissively burns the real cap without ever noticing.

Two consequences beyond the shape:

**The cap genuinely binds, more than the per-slot reading implied.** League B
carries 13 bench slots — a 24-player roster. At roughly 3.5 NBA games per
player per week that is ~84 startable player-games against a cap of 44, so
`ρ ≈ 0.52`: the optimizer must bench playable players most nights. League A is
milder at `ρ ≈ 0.87`. `utilization_ratio` is surfaced in the run record
specifically so that deliberate benching reads as arithmetic rather than a bug.

**Availability cancels out of the threshold.** The cap counts games *played*,
not started, so a player his NBA team sits consumes nothing. With `v_raw` the
value conditional on playing, benefit is `p_play · v_raw` and cost is
`λ · p_play`, so the reduced cost is `p_play · (v_raw − λ)` and `p_play`
factors out of the comparison entirely. **The gate is on the conditional value,
not the availability-weighted one.** A questionable star and a healthy role
player with the same *expected* line are therefore not the same bet under a
binding cap — the star is strictly better, because he only spends a game in the
branch where he actually produces. `p_play` still scales the reduced cost, so
it still orders the assignment; it just does not move the bar.

This is why `value.py` returns conditional values and why
`Projection.conditional_mean` / `conditional_var` exist. `conditional_var`
inverts the Bernoulli gate rather than merely dividing by `p_play`, because the
`p(1−p)E[X]²` term is availability risk rather than game-to-game variance, and
leaving it in would double-count it against the risk model that is already
pricing availability.
