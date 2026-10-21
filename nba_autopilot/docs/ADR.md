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

## ADR-012 — The week comes from the NBA calendar, not from ESPN's `matchupPeriods`.

**Context:** the games-played cap is per matchup period, so pricing it needs to
know which scoring periods are left in the current week. `scheduleSettings.
matchupPeriods` is the obvious source and is what the first draft read.

Probing both live leagues shows it is **self-referential**: all 22 entries map
period *n* to `[n]`. Read literally it says every week is one day long, which
would make the remaining-games horizon exactly "today" and collapse `λ` to the
value of today's marginal game. The optimizer would then spend the whole weekly
budget by Tuesday while reporting a perfectly plausible shadow price. That is
the worst failure mode available here: wrong, confident, and invisible.

Two other probe findings bound the alternatives. Scoring periods **are** daily
and linear in US Eastern, so a calendar is one anchor date plus an offset. And
ESPN does report `currentMatchupPeriod` as a plain integer.

**Decision:** derive matchup periods from Monday–Sunday weeks anchored on the
season opener (`solve/calendar.py`), and discard `matchupPeriods` at the parse
boundary — `_matchup_windows()` detects the degenerate shape, logs it once, and
returns `{}` rather than letting a 1-day "week" reach the optimizer.

The derived model is then checked against ESPN on every run.
`SeasonCalendar.verify()` recomputes `currentMatchupPeriod` from
`currentScoringPeriod` and compares. This is the point of the design: a model
of someone else's calendar is a hypothesis, and this one reports its own
refutation. On disagreement the caller prices **no** cap rather than pricing a
wrong one — `λ = 0` plus a note naming the reason. An unpriced cap overspends
by at most the slack in one week and is visible in the notes; a mispriced cap
is silent.

`from_period_dates` likewise *records* nonlinearity in `linearity_errors` and
exposes `trustworthy`, instead of repairing the sequence. Repairing would mean
inventing a calendar, and the whole point is to notice when we no longer have
one. The All-Star break is the obvious candidate to break linearity later in
the season; this is how we will find out rather than guess.

## ADR-013 — Games used is reconstructed, cross-checked, and allowed to say "I don't know".

**Context:** `λ` needs games used so far this matchup period. Neither ESPN
source survives contact:

- `cumulativeScore.statBySlot` under `mMatchupScore` is the per-matchup figure
  the cap is actually enforced against, and it came back `None` on **every**
  matchup row of both leagues — including rows where individual players already
  showed a game played. Possibly it is only populated after a period closes.
- `teams[].valuesByStat["42"]` is confirmed-shape team games played, but
  season-to-date. Right number, wrong horizon.

**Amended 2026-10-21 — the first bullet was our bug, not ESPN's behaviour.**
A live probe found the row populated all along:
`{"15": {"statId": 42, "value": 2.0, "limitExceeded": false}}`. `games_used`
read a field named `score`, so every lookup missed and the method returned `{}`
on every call since it was written; `measure` then correctly reported
"statBySlot was empty" and worked around it via the season delta, and the
docstring wrote the workaround up as a fact about ESPN. Two further things the
same probe settled: the row is **one pooled entry at the team-total pseudo-slot
15**, matching the cap's own shape, so `starts_from_stat_by_slot` reads that
slot rather than summing "the starting slots" (the old sum agreed only by
accident — `is_starting_slot(15)` is True because 15 is neither bench nor IR);
and `schedule` carries the **whole season**, so the read filters on
`matchupPeriodId`. Nothing in the decision below changes — both readings are
still computed and cross-checked — but the reason has inverted: the
reconstruction is no longer load-bearing, it is the cross-check.

Worth recording how this hid. We were in matchup period 1, where
season-to-date and matchup-to-date are the *same number* by construction, so
the two readings agreed exactly (1 and 1, 4 and 4) and the agreement carried no
information. The unfiltered-`schedule` bug was invisible for a second reason:
the field-name bug kept the loop from ever finding a number to add. Both would
have surfaced together on the opening day of matchup period 2, as last week's
games charged against this week's cap. No amount of code reading would have
found it — the code and its own docstring agreed with each other.

**Decision:** compute both on every run, cross-check them, and reconstruct the
weekly figure as a difference of two season-to-date readings — which requires
remembering the reading taken on the week's first day, hence `gp_snapshots`
(`state.py`). The snapshot is written `INSERT OR IGNORE` so the **earliest**
reading of a week wins; a later run must not overwrite the baseline with a
mid-week total.

Three asymmetries drive the details:

**On disagreement, take the larger.** Over-counting usage costs one marginal
start. Under-counting a hard cap costs a *legal lineup* later in the week.
Those are not comparable, so the tie is not split.

**An empty `statBySlot` is unknown, not zero.** `starts_from_stat_by_slot`
returns `None` for `{}`. "ESPN sent no per-slot data" and "ESPN says zero games
played" are different claims, and the first masquerading as the second would
read as a full budget and overspend immediately.

**A missing baseline is reported, not assumed.** If the add-on was down on
Monday, or was installed mid-week, the week has no baseline and the delta would
under-count by however many games were already played. `measure` refuses a
mid-week baseline explicitly and returns `trustworthy=False`.

`trustworthy=False` is a real answer that propagates: `pipeline.plan_lineup`
prices the cap only when the calendar **and** the usage reading are both
trustworthy, and otherwise runs plain daily maximization with a note saying
which input was missing. `usage.py` measures and never decides; declining to
act on a bad measurement is `main.py`'s job.

## ADR-014 — Today's lineup is a perfect matching, not a top-N selection.

**Context:** the natural reading of "set the optimal lineup" is: value every
player, take the best ten, bench the rest. The first draft of the weight
construction followed it, including only players with a game today.

Real roster shapes rule it out. League A has 10 starting slots, 3 bench slots
and 13 non-IR players — capacity equals roster size **exactly**. On opening
night only 4 of those 13 had an NBA game. There is no arrangement in which the
other 9 are benched; there are only 3 bench slots. Any plan that omits them
produces a non-total `target`, and `set_lineup` requires a slot for every
rostered player.

**Decision:** build the day's decision as a max-weight bipartite matching over
**every** (movable player, eligible slot in the capacity map) pair, and let
gameless players occupy starting slots at weight 0. That is not a workaround —
it is correct. The cap counts games *played*, so a starter with no game
consumes nothing, and there is no reason to prefer benching him.

Two consequences that were bugs before they were notes:

**Weights must be shifted strictly positive.** `max_weight_assignment` treats
both constraints as `<=` and may leave a row unassigned at cost 0, so a
negative-weight player — anyone below `λ` under a binding cap — would be
dropped from the result entirely, making `target` non-total and discarding the
whole day's lineup. The shift is `max(0, -min(rc)) + ε`, neutral to the optimum
because every player is assigned exactly once, and `ε` also carries the
stickiness tiebreak that prevents churn between equal-value arrangements.
`λ = +inf` is handled before the shift, since shifting by infinity is not a
thing.

**Locked players consume capacity wherever they are, bench included.** Routing
locked players through `locked=` rather than simply excluding them from the
matrix is what keeps a 4th locked bench player from being planned into a
3-slot bench.

`validate_target` then re-derives every constraint independently of the solver
that just satisfied them — totality, phantom players, locked movement,
eligibility, IR eligibility, per-slot capacity, whole-roster-benched. It is a
deliberate duplicate of the write-path preflight. A failure returns
`feasible=False` with the *current* lineup as the target, so an unsound plan is
a reported non-event rather than an exception or, worse, a write.

## ADR-015 — The transaction `type` is chosen from the items, not from the waiver flag.

ESPN's `/transactions/` endpoint takes a `type` alongside an `items` array, and
the two are not independent: **ESPN validates the permitted item types against
the transaction type** and refuses the whole transaction on a mismatch. This
was not documented anywhere we could find, and the code shipped with
`"WAIVER" if waiver else "FREEAGENT"` — which is wrong for any drop.

Established by probing a throwaway league, since a rejected POST changes
nothing and so each variant is a free no-op:

| `type` | permitted items | result |
| --- | --- | --- |
| `FREEAGENT` | at most one `ADD`, optional `DROP` | immediate |
| `WAIVER` | one `ADD`, optional `DROP` | `PENDING` |
| `ROSTER` | `DROP` only | immediate |

A lone `DROP` as `FREEAGENT` is refused with `TRAN_INVALID_ITEM_TYPE_COUNT`:
*"TransactionType FREEAGENT may only contain one item for MoveType: ADD."*
That drop needs `ROSTER` — the same type lineup moves use, which reads oddly
until you notice ESPN models a drop as a roster mutation rather than as an
acquisition. Symmetrically, `ROSTER` cannot carry an `ADD`
(`TRAN_INVALID_MOVETYPE`).

**A misread worth recording.** We first took that message to mean "exactly one
item total" and concluded `FREEAGENT` could not express an add-and-drop pair.
It can: the constraint is on the count of `ADD` items. The corrected reading
was only reachable because ESPN reports the two failures with *different*
codes — `TRAN_INVALID_ITEM_TYPE_COUNT` for a bad item set versus
`TRAN_PLAYER_NOT_FREEAGENT` for a bad player — which makes a rejection
informative about which rule was tripped even when the transaction can never
succeed. That discriminator is what let us map the table without executing
most of it.

So the branch is on the item set. The consequence of getting this wrong was
not subtle: **every** add/drop the app made would have been rejected, and the
first one was.

Also settled here: a timed-out add/drop still must not be retried, and this
makes the reason sharper. The failure modes are now distinguishable by error
code, so a reconciling read can tell "ESPN refused the shape" from "ESPN
refused the player" without guessing.

## ADR-016 — A dropped player stays on the roster read, so `roster()` filters.

After a successful `DROP`, ESPN keeps the player in the team's `roster.entries`
array — **still carrying his old `lineupSlotId`** — until waivers process. The
only fields that distinguish him are `playerPoolEntry.status` (`WAIVERS`) and
`onTeamId` (`0`). This is not read-host caching: 8 polls over 25 seconds
returned the identical payload.

`EspnRead.roster()` previously returned every entry. That is a bug with teeth
in two places. The optimizer would assign a lineup slot to a player who is not
on the team, which ESPN rejects — losing the whole day's lineup, not just that
one move. And the exact-capacity matching of ADR-014 would be fitting 13
players into 13 slots when one of them is a ghost, which is infeasible for a
reason nothing in the logs would explain.

So `roster()` filters, and `_is_on_team()` is **deliberately asymmetric**: an
entry is excluded only on *positive evidence* of departure — a `status` present
and not `ONTEAM`, or an `onTeamId` present and not ours. When ESPN omits both
fields the entry is kept.

The asymmetry follows from the costs, which are not symmetric. Wrongly keeping
a departed player costs one rejected write: loud, immediate, recoverable.
Wrongly *excluding* a real player silently shrinks the roster below its slot
count and leaves him unmanaged all week — quiet, and it would be blamed on the
optimizer. Both live leagues report `ONTEAM` plus a matching `onTeamId` for
every player, so the common path is identical either way and the asymmetry only
decides the ambiguous case.

Each exclusion is logged with the player id and the fields that triggered it.
This is rare enough to be worth a line, and when it happens it is the
explanation for a roster that is one short of its slots.

## ADR-017 — Acquisition mode is a property of the player, not the league.

Whether a pickup is an instant add or a waiver claim looks like a league
setting. It is not. On 2026-10-20 one live league had **all 300** of its
available players on `WAIVERS`, while the other had **299 `FREEAGENT` and one
on `WAIVERS`** — within a single league, in the same read.

`acquisitionSettings` cannot answer this. Both leagues report
`acquisitionType: WAIVERS_TRADITIONAL`; the difference is entirely in each
player's own `playerPoolEntry.status`, which changes as players clear waivers.
Any league-level inference would be wrong in the second league immediately and
in the first one by the weekend.

So `RosterEntry` carries `acquisition_status`, populated from the same field on
both roster and player-pool reads, and `_check_acquisition_mode()` consults the
*target player* before an add. An instant add of a `WAIVERS` player raises
`PreflightError` locally, converting a remote `TRAN_PLAYER_NOT_FREEAGENT` into
a message that names the fix.

The reverse — a claim on a free agent — is **logged, not blocked.** It is
certainly suboptimal, deferring to the next waiver run a pickup that could have
been instant. But whether ESPN accepts it has not been observed, and the
throwaway league had no free agents to test it with. Refusing a legitimate add
on the strength of an unverified belief is the worse error, so the asymmetry
here matches ADR-016's: block what is known to fail, report what is merely
suspected.

This also means the not-yet-built `solve/addrop.py` must read
`must_claim_on_waivers` per candidate rather than deciding once per league.

## ADR-018 — A pickup is priced against two duals, and the keeper flag is a rank gate.

`solve/addrop.py` had to answer "is this swap worth being permanent" in points,
and the obvious answer — compare per-game projections — is wrong in both
directions. A 30-point pickup whose games all land on nights our lineup is
already full of better players is worth nothing; a 15-point pickup on a night we
cannot fill a starting slot is worth the whole 15. The requirement said so in
its own words: *"even if we pick up a player who gives us 15 points that 15
points more than we would have had and that could decide a matchup."*

**Decision:** price every candidate against both scarce things, each with the
dual the rest of the codebase already computes —

    surplus = sum over his game-days d of  p_play * max(v - max(lambda, theta_d), 0)

where `lambda` is the games-played cap price from `budget.shadow_price` and
`theta_d` is the day-`d` starting-slot price from `addrop.slot_prices`. Sharing
`lambda` with the lineup plan rather than recomputing it is deliberate: pricing
the same cap twice with two different numbers would let the two layers disagree
about what a game is worth. Games played then drives the decision *by
construction* rather than as a side condition, because `theta_d` collapses to
zero on a light slate.

Two consequences worth stating, because both look like bugs until you see the
arithmetic:

**`surplus` and `v` are different metrics with different jobs.** `surplus` is
points this week and is what the gain is measured in; `v` is per-game quality
and is what the 12% margin gate and the keeper gate are measured in. Keeping
them apart is what lets a season-ending injury be a free drop in a redraft
league (`surplus` 0, because `p_play` is 0) while remaining a protected asset in
a keeper league (`v` unchanged, because the conditional value of a player who
will not play is still his line).

**The games comparison is on `p_play * games`, not on `games`.** The first
version compared the schedule, and the schedule does not know who is hurt — a
player out for the season still has three games on it, so neither the quality
reason nor the games reason could fire and a season-ending injury came out
permanently undroppable, contradicting the module's own docstring. The fix is
one derived property, `Candidate.startable_games`. "Is he injured" still appears
nowhere as a rule: the redraft drop is chosen by the lowest-`p_play` tie-break
and justified as a games reason.

**The keeper flag is a gate on the droppable set, by rank, not a price.** In a
keeper league the honest thing would be to value future seasons, and that number
would be fabricated. So only the bottom `DEFAULT_KEEPER_DROP_FRACTION` of the
roster by `v` may be dropped at all, and the approval card says which players
those were. The flag is **asked for in config, not read from ESPN**: ESPN's own
keeper flag describes a draft mechanic and reads identically in a dynasty league
and a redraft one. It defaults to `false` because the two mistakes are not
symmetric — treating a keeper league as redraft proposes a drop you can look at
and reject, while treating a redraft league as keeper quietly protects dead
weight all season and never mentions it. For the same reason its parser is
stricter than the top-level option reader: an unrecognized spelling stops the
app at startup rather than silently reading as false.

**The matchup score is used in one direction only.** ESPN publishes no
projected final for basketball (ADR-010's reads confirm no
`totalProjectedPointsLive`), so we know the score so far and nothing about where
it is heading — our own roster we can project, the opponent's we cannot. That
rules out any "this one is already decided" test, which needs exactly the number
we do not have. So the score may **waive** the churn floor when we are behind by
less than the move would gain, and may never raise it: an accumulated lead with
no opponent projection behind it is not evidence the week is safe.

**"Is there room?" is asked of non-IR slots against non-IR occupants.** Summing
all of `lineupSlotCounts` reads an empty IR slot as a free roster spot, and it is
not one: ESPN only accepts a player into IR if his own designation makes him
IR-eligible, which a free agent worth acquiring never is. Both halves matter and
each is pinned by its own test, because fixing one alone is worse than fixing
neither in one of the two live leagues — the Chairman's league (13 active slots +
3 IR, 14 players with 1 on IR) read as "two spots free" and produced an unpaired
add that ESPN refused at the wire, while Dynassty (24 active + 3 IR, 24 players
with 2 on IR) has two genuine spots that a cap-only fix would hide, forcing a
drop that is not needed. The helper lives in `espn/ids.py` because that module
owns slot semantics; it cannot live on `LeagueSettings`, since `models.py`
depends on nothing and so cannot see `IR_SLOT`. `espn/write.py`'s preflight
repeats the check rather than trusting the decision layer: this one reached the
user as a tap that failed, so the last line of defence has to be at the wire.

**And a paired drop only counts if it vacates an active slot.** Fixing the cap
alone left the same bug one level down, and the next live run walked straight
into it: the Chairman's league paired its add with the man already on IR, who
was the cheapest way out on points *and* the lowest `p_play` on the tie-break,
so he was the drop of choice by two independent routes. Dropping him frees an IR
slot; the active count does not move; ESPN refuses add+drop as readily as it
refused the unpaired add. So `Candidate` carries one slot fact, `on_ir`, and when
there is no active room the IR occupants come out of the droppable set, named in
the run's notes alongside the `never_drop` and locked exclusions — which is the
status page, not the approval card: the card carries the rationale for the move
that *was* proposed. They stay in the droppable set when there *is* room — the
exclusion is about making room, not about protecting them, and a note explaining
an absence of room must not be shown where there is room.

**Rejected:** a discount rate converting keeper value into this week's points
(invented number, changes decisions); having `plan_addrop` itself return a
ranked list of its best several swaps — the drop side and the open-spot
arithmetic both depend on what the roster holds, so the second-best swap
against today's roster is usually the same pickup paired with the same man
going out, and a list of those is one decision wearing several hats. A run
*does* now offer several swaps, by calling this layer again against the roster
each accepted swap leaves behind; see ADR-021. And bidding zero in a FAAB
league rather than declining was rejected too — it would
submit, reliably lose, spend an acquisition, appear in the activity feed, and
teach the user the feature is broken. Neither live league uses FAAB, so
`plan_addrop` declines on `uses_faab` instead.

## ADR-019 — A model reviews every proposed swap, and can only block it.

**Status:** accepted, forced by two consecutive live runs.

ADR-018's pricing is correct and its blindness is load-bearing: `surplus` is
points over the rest of this scoring period, and that is answerable *because* it
ignores everything else. The cost of that is a player who will not suit up again
this week being worth exactly zero, and zero is the cheapest thing on the roster
to give away. A season-ending injury and a sprained ankle are the same number.

Observed live on 2026-10-20 and 2026-10-21: the layer proposed dropping an
88%-owned first-overall rookie, parked on IR with a short-term injury, on
back-to-back runs. Nothing in the arithmetic was wrong. The requirement arrived
in one sentence — *"it rewcommended dropping [him]... number one overall draft
pick. this is why we need ai options in here"* — and the scope in the next:

> "the ai should be called to validate any recoomendations for add drops before
> i see them. ai knows the context around first round picks, famous players,
> past stats. we should be prompting the ai to be a judge of weather the
> statisical value of the add drop makes sense. it should read recent reports,
> and be able to show why it rejected a request to add drop"

**Decision:** `judge.py` reviews each fully-priced proposal before it is offered
to the user, and its only power is to withhold it.

**It gates, it does not author.** It is never shown the pool, never asked who to
pick up, never asked to rank anything. Every number in the prompt is one that
already moved the decision, and the verdict is a boolean plus a reason. This is
what keeps the whole thing auditable: a rejection is checkable against the case
it was shown, and an approval contributes no claim of its own. A model that
could *propose* a swap would be a second valuation layer with no units, and
there would be no way to tell its recall from its taste.

**`models.Move.rationale` is untouched.** That invariant — *"never written by an
LLM without the numbers being present and checkable"* — survives because the
card still carries what `addrop._rationale` computed. A caveat raised on an
approval is appended to it, never substituted for it.

**Ownership share is the signal, and it is carried but never priced.** ADR-018's
inputs are points and percent-per-game; `percent_owned` is neither, and adding
it as a term would mean inventing an exchange rate between "the league rosters
him" and "fantasy points". So `Candidate` carries the four market readings —
ownership share, average draft position, ESPN's preseason rank, the season
outlook — and `solve/addrop.py` uses none of them. They exist for the layer that
reasons in prose, which is the only one that can use "thousands of other owners
disagree with this valuation" without fabricating a coefficient.
`tests/test_addrop.py::TestMarketSignalsAreCarriedNotPriced` fails if a later
version quietly starts pricing them.

**Fail-closed on error, fail-off with no key.** Configuring a key *is* the
statement that swaps should be reviewed, so once one is present a proposal the
judge could not reach a verdict on is not shown — a timed-out request is not
permission to make an irreversible, publicly attributed move unexamined. With no
key there is no gate and ADR-018's behaviour stands unchanged. `Verdict.errored`
keeps the two apart in the run notes, because a single boolean would make a week
of API outages read as a week of good judgment. The on/off state is derived from
the presence of the key rather than from a separate switch: two facts that can
contradict each other is one more than the feature needs.

**The verdict is a forced tool call.** `tool_choice` is pinned to
`record_verdict`, and a response without it raises rather than reading as
assent. The alternative is regexing free text for a yes/no that gates an
irreversible action, and the near-misses all produce *something*: a refusal, a
`max_tokens` truncation mid-call, a model that does not support tools. Treating
"something" as approval would fail open on exactly the inputs that confused it.

**Rejected:** letting the judge pick the add as well (no units, unauditable, and
a second valuation layer the first one cannot check); a hard `percent_owned`
threshold in `plan_addrop` instead of a model — it blocks the keeper-league
rookie and also blocks every legitimate drop of a widely-held player in a bad
role, with no way to tell those apart and no sentence to show the user;
re-fetching the injuries page inside the review (the run already holds it, and
two fetches is two chances to judge a swap against a report about someone else);
and letting the judge's own words replace the computed rationale, which would
void the `Move.rationale` invariant for the sake of a nicer-reading card.

## ADR-020 — The lineup is re-optimized the moment an acquisition lands, not on the next run.

**Status:** accepted.

A pickup who sits on the bench is worth nothing. The day's lineup is written in
the first half of `run_league`, against a roster that does not yet contain him,
and `_consider_addrop` runs *after* that — deliberately, because the add/drop
decision needs to know which players this run just started in order to protect
them. So every successful acquisition leaves a lineup that was optimal for a
roster that no longer exists.

Deferring the fix to the next scheduled run costs a day of the gain the swap was
approved for, and the default `run_times` is a single entry. In a week with three
days left that is a third of the points. Worse, the common case is the *tap*
path: `autonomy_add_drop` is false out of the box, so the swap usually executes
in `execute_approved`, hours after the run that proposed it and with no run
scheduled before the night's games.

So `rebalance_after_swap` re-reads the roster from ESPN, re-plans, and writes.
Both call sites use it — the autonomy branch of `_offer_addrop`, after its
notification, and `execute_approved` after a successful tap. The notification
order is the swap first and the lineup second, because the lineup change is a
consequence of the swap and reading them the other way round is reading the
answer before the question.

**It re-reads rather than patching the roster in memory.** Simulating the swap —
removing the dropped entry, appending the added one — would be cheaper by one
HTTP request and would produce a lineup computed against a roster nobody ever
confirmed. ESPN's response to an add/drop does not enumerate the resulting
roster, slots are assigned by ESPN on acquisition, and `roster()` already filters
the ghost entries ESPN leaves behind (ADR-016). The authority on what the roster
is after a write is the read after the write.

**`plan_from_roster` exists so there is one chain, not two.** `plan_lineup` takes
eight arguments that each change the answer, and the inputs behind them are a
calendar read, a pro-schedule read, an injuries join, a projections fetch and a
games-used measurement. Two call sites assembling that by hand is two sets of
eight chances to compute a different lineup for the same roster and never notice,
because both would look like valid lineups. The three slow reads behind it are
cached on the instance, so the second call costs one projections fetch rather
than a second multi-megabyte season document.

**Three results mean the roster did not change, and none of them rebalance.** A
pending waiver claim — he is not ours until ESPN processes it overnight, and the
drop has not happened either, so the lineup already standing is the correct one
for the roster we actually have. `shadow_mode`, where the packet never left the
process. And a refused write. All three would otherwise produce a confident log
line about a rebalance that rebalanced nothing. The waiver test is on
`payload["waiver"]`, not on `result.kind`: `add_drop` returns `ADD_DROP` for an
instant free-agent pickup and for a `PENDING` claim alike (ADR-015), so the kind
cannot tell them apart.

**It honours `autonomy_lineup`.** That flag is the standing answer to "may this
app move players without asking", and an add/drop approval is not a second
answer to it — approving a swap says who should be on the roster, not who should
be in the starting five. Note that `run_league`'s own lineup write does *not*
consult the flag today; gating only the new path is deliberate, because making
the flag work everywhere means a lineup approval card and is its own decision.

**It never raises.** It runs after an irreversible, already-journalled
transaction, and one of its two callers is the approval loop — an exception
escaping would stop every *other* pending approval from executing. A bench spot
for a night must not cost the queue. An unusable plan afterwards is the one case
it is deliberately *loud* about, with a notification rather than a log line:
everywhere else "no usable plan" means yesterday's lineup stands, which is safe,
but here the roster underneath that lineup just changed and it may be starting a
player who is no longer on the team.

**Rejected:** rebalancing on the next scheduled run (the gain arrives after the
games); patching the roster in memory; a second `plan_addrop` pass in the same
breath, so a rebalance could cascade into another swap — one irreversible move
per approval, and the N-swap funnel is a separate decision with its own
acquisition accounting; and re-notifying when the plan came back unchanged,
which would train the user to ignore the one notification that means something.

## ADR-021 — A run offers up to N swaps, each priced against the one above it.

**Status:** accepted. *"i also think you should recomend multiple adds."*

One swap a run was never a statement about what is worth doing; it was a
statement about what `plan_addrop` can answer in one call. Points matter in both
live leagues, the acquisition budget is five to seven a week, and a run that
finds three worthwhile moves and shows the best one leaves the other two until
tomorrow — which in a four-day matchup period is a quarter of the week. The
cap is now `addrop_max_moves`, default 3, schema-bounded to 1–5.

**The funnel re-prices rather than ranks.** `_consider_addrops` calls
`plan_addrop` in a loop, and between calls it advances a *simulated* roster and
pool with `_apply_swap`. This is the whole design, and the alternative —
asking the solver for its best N swaps against one roster — is wrong rather
than merely cruder. The drop side is "the cheapest man who may go", and the
open-spot arithmetic is `active_roster_cap` against `active_roster_use`; both
are properties of the roster. Rank against a fixed roster and the second and
third entries are the same weakest player paired with the second- and
third-best pickup: three cards of which exactly one can be approved, which is
the bug that prompted this, not a feature.

**The acquisition lands on the bench in the simulation.** `BENCH_SLOT`, which
is where ESPN puts one, and more importantly what keeps `active_roster_use`
consistent: a drop-plus-add leaves the count where it was, an add into an open
spot raises it by one. Which lineup slot he eventually starts in is not this
layer's business — that is the rebalance after the write (ADR-020).

**`just_added` is a separate protection from `never_drop`.** A man the loop has
just picked up is undroppable for the rest of the run: "add him, then drop him"
is two cards that cancel out and one acquisition spent on nothing. The
arithmetic would normally never choose him — he was taken for being better than
everyone — but a thin pool and an unlucky schedule can make the newest bench
player the cheapest way out. It is its own parameter and its own note only
because a user reading `never_drop` against a man the same run proposes to pick
up would reasonably conclude the option was misconfigured.

**The acquisition counter advances too, so the budget is real.** `sim_used`
increments per accepted swap, which means `remaining_acquisitions` refuses the
fourth swap of a week that had three left by exactly the guard that would have
refused it on tomorrow's run. A counter ESPN would not give us stays `None` —
unknown is not spent — and the funnel then runs to the option's ceiling, as it
would in a league with no limit.

**What is held fixed, and what that costs.** `lam` and `theta` come from the
lineup plan computed at the top of the run and are *not* re-solved per
hypothetical roster. Re-solving would mean a full lineup optimization per
attempt for an estimate of an estimate. The cost is real: the second swap's gain
is priced with the first swap's duals, and after two pickups the starting slots
are held by the pickups, so the true price of a starting slot has risen. The
estimates therefore decay down the list, which is why the list is short, why it
is strictly best-first, and why every card after the first says so in words —
*"Priced as the second move of this run, assuming the swap above it is
approved."* An approximation the user is told about is a different thing from one
they are not.

**A blocked swap and an unreachable reviewer are opposite signals.** The review
(ADR-019) runs per attempt, inside the loop, so the reviewer reads the same card
the user would — the conditional-pricing line included. A *judgment* is about
one pair: the loop retires both halves (the add by id, the drop by name) and
carries on, because the verdict is prose and there is no telling which half it
objected to, and re-offering a pair sharing a half is asking the same question
twice. A reviewer that *could not be reached* is not a judgment about anything,
so the loop stops entirely rather than spending the remaining attempts failing
the same way and withholding every one of them for the same non-reason. That
distinction is why `_review_addrop` returns `(plan, notes, unreachable)` instead
of just the first two.

**Each swap is its own approval, with its own notification tag.** Three swaps
are three rows, three cards and three taps — approve any subset, in any order.
The tag is now `autopilot-addrop-<league>-<proposal>`: iOS treats the tag as an
identity, so one tag per league meant the third notification replaced the first
two on the lock screen and the user was asked one question and never saw the
others. The league id stays in front so two leagues can never collide whatever
the proposal ids turn out to be.

**Rejected:** one notification listing all three with a basket approval (a swap
is irreversible and publicly attributed; "yes to all of that" is not a thing the
user should be able to say in one tap, and the later entries are the least
certain); re-solving the lineup per hypothetical roster (cost, for an estimate
whose honesty is better served by saying it is conditional); letting the loop
continue past an unreachable reviewer so the user at least sees the unreviewed
swaps — a timed-out request is not permission, which was already settled in
ADR-019 and does not become permission in bulk; and a default of 1, which would
have shipped the mechanism switched off.
