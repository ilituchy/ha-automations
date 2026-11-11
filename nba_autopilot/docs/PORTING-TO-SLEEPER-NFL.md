# Building the football version on Sleeper — handoff from the NBA/ESPN app

Written 2026-11-11, after three weeks of building and operating
`nba_autopilot` (an ESPN fantasy basketball autopilot running as a Home
Assistant add-on). This is for whoever builds the Sleeper/NFL equivalent.

It is not a design document for the new app. It is the list of things this
project learned the expensive way, sorted by whether they transfer. Where a
lesson cost a real roster move, that is said, because the cost is the argument.

**How to read it.** Sections 1–3 are facts you can rely on. Section 4 is
architecture that transfers and is worth copying rather than rediscovering.
Section 5 is what changes shape for football. Section 6 is the operational
material — the integration failures that took whole mornings — and it is the
most transferable part of the whole document, because none of it is about
basketball. Section 7 is about how to work, not what to build.

Anything marked **VERIFY** is an inference I did not confirm against a live
league. Confirm it before building on it; this project's worst days all began
with an unverified assumption that looked obvious.

---

## 1. The one fact that decides the architecture

**Sleeper's public API is read-only.** From the official docs at
https://docs.sleeper.com (read 2026-11-11), verbatim:

> The Sleeper API is a read-only HTTP API that is free to use for
> non-commercial purposes… No API Token is necessary, as you cannot modify
> contents via this API.

So the NBA app's whole write half — `espn/write.py`, the preflight, the
transaction journal, the two autonomy switches, the "did ESPN actually apply
it" problem — **has no counterpart**. You cannot set a lineup, claim a waiver
or drop a player through the documented API.

That is not a smaller project. It is a different one. The NBA app is an
*actuator* with a recommender attached; the Sleeper app can only be a
*recommender* with a very good notification and review surface. Decide which
of these you are building in the first hour, because almost every downstream
choice follows from it:

- No write means no irreversibility, which means **the add/drop autonomy
  question disappears** and with it a large amount of safety machinery.
- It also means the app's output is a *recommendation a human executes*, so
  the quality bar moves from "don't make a bad move" to "be worth reading".
  A recommendation nobody acts on is worth zero, and a daily notification
  nobody taps is worse than zero.
- The status page stops being a record of what the app did and becomes the
  primary product.

There is an undocumented private API behind the Sleeper app itself (the
mobile/web client clearly writes somehow). Using it means reverse-engineering
an authenticated endpoint that the operator has explicitly not published, on
an account that can be banned, for a service whose terms offer the public API
free only for non-commercial use. I am flagging that it exists and that I did
not investigate it. That is the owner's call, not the agent's — do not go
looking for it on your own initiative.

---

## 2. Verified Sleeper API facts

All read from https://docs.sleeper.com on 2026-11-11 unless noted.

**Base:** `https://api.sleeper.app/v1`. No auth, no token, CORS open.

**Rate limit:** "stay under 1000 API calls per minute, otherwise you risk
being IP-blocked." That is generous — the NBA app makes a few dozen calls per
run — but it is an IP block, not a 429-and-back-off, so treat it as a hard
ceiling and cache aggressively.

**Errors:** 400, 404, 429, 500, 503. There is no documented retry guidance.

| What | Endpoint |
|---|---|
| user → id | `/user/<username>` |
| leagues | `/user/<user_id>/leagues/nfl/<season>` |
| one league | `/league/<league_id>` |
| rosters | `/league/<league_id>/rosters` |
| league members | `/league/<league_id>/users` |
| weekly matchups | `/league/<league_id>/matchups/<week>` |
| transactions | `/league/<league_id>/transactions/<round>` (round = week) |
| playoff bracket | `/league/<league_id>/winners_bracket`, `/losers_bracket` |
| traded picks | `/league/<league_id>/traded_picks` |
| season state | `/state/nfl` |
| drafts / picks | `/league/<league_id>/drafts`, `/draft/<draft_id>/picks` |
| all players | `/players/nfl` (also `?position=QB&active=true`) |
| trending | `/players/nfl/trending/<add\|drop>?lookback_hours=24&limit=25` |

Things worth knowing before you design around them:

- **`/players/nfl` is ~5 MB and the docs say to call it at most once per
  day.** Cache it on disk with the fetch timestamp. The filtered forms
  (`?position=`, `?active=true`) are much smaller and are the right call for
  a narrow need. The NBA equivalent was 2.5 MB and ~0.12 s; budget similarly.
- **The roster object is the lineup.** `rosters[].starters` is an *ordered
  list of player ids*; `players` is everyone; `reserve` is IR. The bench is
  `players` minus `starters` minus `reserve`. There is no per-player slot id
  the way ESPN has `lineupSlotId` — **position is conveyed by index**.
  **VERIFY:** the index is widely understood to align with the league's
  `roster_positions` array with `BN`/`IR`/`TAXI` removed, and an empty slot
  is believed to appear as `"0"`. The docs do not say either. Confirm both
  against the live league before writing a line of optimizer code, because
  every slot decision depends on it.
- **Player ids are strings**, and defences are team abbreviations (`"DET"`,
  `"PHI"`) sitting in the same id space as numeric player ids. Type your
  `PlayerId` as `str` from the start. The NBA app used `int` because ESPN's
  are ints; copying that here will cost you a refactor.
- **`league.roster_positions` and `league.scoring_settings`** give you the
  slot shape and the exact scoring rules as data. This is strictly better
  than ESPN, where scoring had to be assembled from `scoringItems` and stat
  ids. Use it: project *stats*, then score them through the league's own
  settings, rather than consuming someone's pre-scored "fantasy points" that
  were computed under different rules.
- **`/state/nfl`** returns `week`, `leg`, `season_type` and `display_week`,
  and `display_week` can differ from `week`. See §6.3 for why a provider's
  idea of "what period is it" deserves more suspicion than it looks like it
  deserves.
- **Player records carry cross-ids**: `espn_id`, `yahoo_id`, `rotowire_id`,
  `rotoworld_id`, `sportradar_id`, `fantasy_data_id`, plus `search_full_name`
  / `search_first_name` / `search_last_name` already normalised for matching.
  **This is the opposite of the NBA situation**, where this project verified
  that `espn_id` is null for 100% of 2,176 NBA players and name matching was
  the only join available (see `rootfs/app/sources/sleeper.py`, which
  documents that measurement). For NFL these fields are NFL-populated and
  should give you real id joins to other data sources. **VERIFY the fill
  rate** rather than assuming — the docs' own Tom Brady example shows
  `"espn_id": ""`, so "present as a key" is not "populated".
- **Injury fields**: `injury_status`, `injury_start_date`,
  `practice_participation`, plus `depth_chart_position` and
  `depth_chart_order`. The last two have no NBA analogue and are a genuinely
  strong availability and volume signal. The NBA app spent real effort
  inferring role from minutes trends; football hands you the depth chart.
- **Attribution**: the docs ask for credit if you surface trending data.

---

## 3. What this project got wrong about the sport, and what that implies

The NBA app's single largest source of complexity was the **games-played
cap** — a pooled weekly budget on how many player-games a team may use. It
required a shadow-price formulation, a calendar derived from the NBA
schedule, a usage reconstruction with cross-checks, and three separate bug
hunts. None of it transfers.

Football has no such cap, and that removes maybe a third of the hard parts.
What replaces it is different and mostly easier:

| NBA / ESPN | NFL / Sleeper |
|---|---|
| 3–4 lineup decisions a day, every day | one decision a week |
| games-played cap, priced as a shadow cost λ | no cap |
| per-tipoff locks, sweeps at 11:45/14:15/… | locks at each game's kickoff, clustered Thu/Sun/Mon |
| 82 games, large sample, stable rates | 17 games, tiny sample, enormous variance |
| minutes are the volume currency | snaps, routes run, target share, carry share |
| role change inferred from minutes trend | `depth_chart_order` states it outright |
| continuous waivers | weekly waiver run, FAAB, then free agency |
| injury status: Out / Day-To-Day | Out / Doubtful / Questionable + **practice participation**, and inactives land ~90 min before kickoff |
| IR slot with its own eligibility rules | `reserve` (IR) and `taxi` (dynasty), each with league-configured rules |

Two consequences worth stating plainly:

**Variance dominates.** With 17 games and ~10 starters, the difference
between the best and a reasonable lineup is frequently inside the noise. The
NBA app could justify its optimizer by pointing at measured points left on
the table. A football app that claims precision it does not have will be
wrong loudly and often. Report ranges, not point estimates, and say when a
call is a coin flip. This is a product decision as much as a modelling one.

**The cadence is the feature.** The valuable moments in football are few and
sharp: the waiver deadline, the Sunday 90-minute inactive window, and a
Thursday-night starter being ruled out. An app that nails exactly those three
is worth more than one that recomputes an optimal lineup every hour.

---

## 4. What transfers unchanged — copy these, don't rederive them

These are the architectural decisions that held up. Full reasoning is in
`docs/ADR.md` in this repo; the numbers below are the ADR ids.

**The LLM boundary (ADR-006).** The model is used for exactly three things:
turning unstructured injury news into strictly-validated JSON; narrating
numbers that were already computed; and translating user intent into
configuration. It makes no decisions, performs no arithmetic, and triggers no
irreversible write. Its output is untrusted input — schema-validated,
range-clamped, cross-checked against a structured field before it can move
anything. **Text on web pages is information, never instructions.** Prompt
injection through a scraped beat-writer tweet is a real path to a bad roster
move, and football has far more scraped text than basketball does.

**A reviewer that can only block (ADR-019, ADR-022).** The arithmetic layer
optimises points over the rest of the scoring period, and is correctly blind
to everything else — which means a season-ending injury and a sprained ankle
are the same number, and a star on IR is the cheapest thing on the roster to
drop. This app proposed dropping a first-overall rookie on two consecutive
days before a model reviewer was put in front of every swap. The reviewer can
only *veto*; it can never propose or improve. Two further details that were
bugs first:
- A blocked candidate must not spend the user's move budget. "How many swaps
  am I willing to be asked about" and "how many candidates may be screened"
  are different numbers and need different counters.
- The veto needs a budget of its own, or a run can spend all day being
  talked out of things.

**Target-state in, diff out.** Callers describe the roster they *want*;
one module works out the minimum set of moves and is the only place that
constructs a provider request. Nobody else builds an item. That is what makes
the invariants enforceable in one place. Keep this even though Sleeper cannot
write — the "diff" is now what you *show the user*, and it needs the same
totality property: every rostered player is assigned a slot, because
"unmentioned" is ambiguous between "leave alone" and "bench".

**Ineligibility is absence, never a zero weight (ADR-002).** In a sparse
`(player, slot) -> value` map, an absent pair is *forbidden* and a pair
present at `0.0` is *legal and worthless*. Conflating them lets the optimizer
start a kicker at QB whenever the alternatives are negative. Football's FLEX
and SUPERFLEX slots make the eligibility map richer than basketball's, so
this matters more, not less.

**Both constraints are `≤`, not `=` (ADR-003).** Leaving a starting slot
empty is occasionally correct. An equality formulation cannot express it.

**The lineup is a perfect matching, not a top-N selection (ADR-014).** The
natural reading — value everyone, start the best N — breaks the moment
capacity equals roster size, which happens constantly. Build it as a
max-weight bipartite matching over every (player, eligible slot) pair. Two
things that were bugs before they were notes: weights must be shifted
strictly positive, or the matcher prefers leaving a negative-valued player
unassigned and you get a non-total target; and stickiness needs an explicit
bonus for the player's current slot, or the lineup churns on ties.

**Pure Python, no numpy/scipy (ADR-001).** A 28×28 assignment solves in tens
of microseconds in pure Python. scipy costs ~90 MB of container image, a
platform wheel dependency, and a floor on where the code can be tested. The
dev machine here has no numpy; pure Python means the solver is testable
everywhere. This was never regretted. **Note for the port:** I twice needed
max-weight bipartite matching inside an ad-hoc audit script and was glad it
was importable with no dependencies.

**A league is the unit of optimization; nothing is pooled (ADR-007).** Two
leagues means two of everything — scoring weights, budgets, rankings — even
when the same player is on both rosters. Pooling is always the tempting first
design and always wrong. Sleeper makes this easier by handing you
`scoring_settings` per league; use it per league.

**Availability factors out of the threshold, not the ordering.** With value
conditional on playing, `reduced_cost = p_play × (v − λ)`. `p_play` scales
the ordering but does not move the gate. The practical consequence is that
projections should be **conditional on the player suiting up**, with
availability carried as a separate probability. Mixing them double-discounts.
Football's Questionable tag makes this much more live than basketball's.

**A dropped player lingers in the roster read (ADR-016).** ESPN kept dropped
players in `entries` with their old slot, flagged only by a status field, so
`len(entries)` was not a roster count and the exact-capacity matching would
happily assign a slot to a ghost. **VERIFY the Sleeper analogue** — what a
roster looks like between a waiver claim being submitted and processed, and
whether a pending claim appears in `players` or only in `transactions`.

**The approval state machine and the journal.** Every proposal gets a row
before anything happens, a claim before it executes, and a completion
afterwards. Autonomy changes *who approves*, never *whether there is an audit
trail*. Even read-only, keep the journal: it is how you answer "why did it
tell me that last Tuesday", and it is what the status page renders.

---

## 5. Home Assistant deployment, if you reuse that shell

`docs/DEPLOY.md` in this repo is short and all of it was learned by being
burned. Condensed:

- The Supervisor caches a clone of the repo, so deploying is three steps:
  `git push`, `POST /store/reload`, `POST /addons/<slug>/rebuild`.
- **`/store/reload` fails intermittently**, leaving the cache on the previous
  commit, and the subsequent rebuild builds *that* while reporting the same
  error it reports on success. The add-on restarts cleanly either way. Every
  signal lies at once.
- **A reload that returns success has only *started* the pull.** A rebuild two
  seconds later reads a half-written cache. Retry the reload until it
  succeeds, then wait ~45 s, then rebuild.
- Therefore: **always verify with a canary — a value only the new code can
  print.** Pick it before deploying. `version` cannot serve, because the
  add-on pins one.
- Supervisor is scriptable over the HA **WebSocket** API
  (`{"type": "supervisor/api", "endpoint": …, "method": "post"}`). The REST
  proxy serves GETs but will not carry the POSTs you need (ADR-009).
- There is no run-now endpoint. Triggering a one-off run means writing the
  options object with an extra run time and restarting. **That object carries
  every secret the add-on holds and the endpoint replaces it wholesale** — so
  read it, mutate it, write it back, and never print it.

---

## 6. The operational lessons — the expensive part

None of this is about basketball. All of it is about integrating with a
fantasy provider's API, and I expect most of it to recur on Sleeper in some
form.

### 6.1 A rejected write leaves no trace, so log the packet before you send it

On 2026-11-10 ESPN refused a lineup three times with `Too many players in the
SG slot (maximum 1)`. The target had exactly one player in SG. The roster's
SG was empty. A rejected transaction creates no row on ESPN's side and echoes
nothing back, so the *only* evidence in existence was the name of the slot.
Reconstructing what had been sent took a morning of reading logs and probing
the live API.

**Log every outbound mutation, by name and human-readable field, before it
goes.** It costs one line. Its absence costs a day. This applies to anything
you POST, including to an LLM.

(Read-only Sleeper has no writes — but the same argument applies to *inputs*.
Log the inputs to each recommendation, because "why did it say that" is the
question you will actually be asked, and a projection you did not record is a
projection you cannot defend.)

### 6.2 HTTP 200 does not mean applied

ESPN accepted a transaction, returned 200, and silently discarded one of its
items. The discarded item was supposed to free a starting slot; the move that
filled the slot then overflowed it, and the *entire* transaction was rejected
— one bad item cost the whole day's lineup. The cause was a field we were not
reading: the roster *entry*'s `injuryStatus` carries transaction state
(`NORMAL`, `FUTURE_TO_IR`) while the *player*'s carries medical status
(`OUT`). Same name, different object, completely different meaning.

Generalisable: **when a provider has two fields with the same name on
different objects, assume they mean different things until you have checked.**

### 6.3 Read and write sides can disagree about what time it is

At 09:00 on 2026-11-10, ESPN's settings read reported scoring period 21 while
the write endpoint had already advanced to 22. An unqualified roster read
during that window returns the *previous* period's lineup, so every
"from this slot" assertion we sent described yesterday.

Sleeper has exactly the shape for this to recur: `/state/nfl` returns both
`week` and `display_week` and explicitly says they can differ, and matchups
are fetched per week. **Decide which week you mean, name it in every request
that accepts it, and log it.** Never let the provider pick.

### 6.4 Read APIs lag writes — do not verify by immediately re-reading

Having been burned by the silent discard, the obvious fix is to re-read the
roster after a write and compare. Measured: ESPN's read API lagged a
confirmed write by **over a minute**. A naive read-back check reports
failures that did not happen, which is worse than no check. If you need
confirmation, confirm against the transaction list, with a delay, or not at
all.

### 6.5 When you cannot reproduce it, remove the question instead of guessing

For the SG rejection I eliminated every candidate explanation by probing the
live API — item order is irrelevant, in-transaction swaps are accepted, an
off-IR move into a slot vacated in the same transaction is accepted, a
refused IR move reports itself as an IR error, a write to a non-current
period is refused with its own message. I never found the rule.

The fix does not try to. It re-sends the same target as a short series of
transactions ordered so that **every move lands in a slot that is already
empty** — which cannot overflow under any counting rule, in any item order.
That is a weaker, dumber, and entirely sufficient guarantee.

**Shipping a guess dressed as a diagnosis is the failure mode to avoid.** If
you cannot explain it, say so, and build something whose correctness does not
depend on the explanation.

### 6.6 Measure the premise before you trust the argument

The optimizer once priced an exhausted games budget at `+infinity`, which
made every playable player worse than a benched one and fielded a guaranteed
zero. The fix rests on an argument — starting weakly dominates benching once
the cap is blown — but the argument has a premise: that the provider still
counts over-cap games. I checked it across 36 team-weeks (official total
equals the plain sum of starters, in all 36, including 9 over-cap weeks)
before trusting it. **Write the measurement into the docstring next to the
argument.** Future-you will want to know whether it was reasoned or observed.

### 6.7 After fixing the cause, grep for the consumers

The `+infinity` fix was declared done after one module changed. The actual
mechanism of the harm was a *different* module's branch keyed on that same
infinity, left behind as dead code that was not dead. It was found only by
grepping for every consumer of the value. Then an invariant test was added
sweeping 210 parameter combinations asserting the value is never infinite or
NaN — which is the kind of test that makes the class of bug impossible rather
than the instance.

### 6.8 Audits need exact methods, not plausible ones

A first attempt to measure "points lost to rationing" produced 909 points, by
crediting one bench player against six different empty slots on the same day.
The honest number, from exact max-weight matching, was 36. A second audit
compared past weeks against the *current* roster and produced large spurious
deltas, because add/drop churn means the roster of two weeks ago is not this
one. **When you report a number to the user, the method has to be exact, and
you have to be the one who tries to break it first.**

### 6.9 Treat the provider's quirks as documentation

Every surprise in this project ended up as a named section in `DOCS.md` or an
ADR, with the date it was observed and the evidence. That file is the reason
the third occurrence of a problem took minutes instead of hours. Do the same
from day one; the Sleeper equivalents will be different but there will be
just as many.

---

## 7. How to work on this, for the agent

These are the process notes, and they are not generic advice — each one is
something that went wrong here.

- **Verify before asserting.** The owner's recurring question was "is this
  actually fixed?", and he was right to keep asking: twice something had been
  reported fixed when only part of it was. Check the live system, quote what
  it returned, and distinguish "the tests pass" from "I watched it work".
- **Say what you did not verify.** A fix that is unit-tested but has never
  fired against the live API is a fix with an asterisk. Put the asterisk in.
- **Probing a live account is a real action.** I ran write probes against the
  owner's actual team to find the SG bug. That was the right call — it was
  hours before kickoff, every probe was reversible, and one of them rescued a
  player who would otherwise have been locked into a bench slot. But it was
  only right because of those conditions. Check the clock, check
  reversibility, and revert immediately.
- **A background watcher beats a guess.** Several conclusions came from
  tailing the add-on log through the next scheduled run rather than
  predicting what it would do.
- **Keep a scratchpad of throwaway analysis scripts and keep them around.**
  Roughly twenty accumulated here, and the second and third incidents reused
  them.
- **The owner's one-line requirements are often the whole spec.** "remember
  when i make an add, we have to potentially rebalance the lineup" produced
  an entire subsystem (ADR-020). Treat them as such and write them into the
  ADR verbatim, which makes it obvious later which decisions were his and
  which were the agent's.
- **Secrets discipline.** This repo is public. Every commit runs a grep for
  the cookie names, the SWID, the HA hostname and the user id. Two credential
  leaks into a transcript happened here from printing a whole config object —
  so never print a config blob, print the named fields you need. League ids
  are not credentials but they identify a real person's team, and they stay
  out of the repo.

---

## 8. Suggested build order

1. **Confirm §1.** Read-only or not, settled, in writing, before anything
   else. If the answer is read-only, write the product definition for an
   advisory app and get it agreed.
2. **Pin the roster model.** Fetch the real league. Confirm how `starters`
   maps to `roster_positions`, what an empty slot looks like, where IR and
   taxi sit, what a pending waiver claim does to the roster. Write it down
   with the date, the way `sources/sleeper.py` does.
3. **Scoring from `scoring_settings`.** Build the league's scoring function
   from its own settings and test it by reproducing a completed week's
   `points` field exactly from the box score. If you cannot reproduce a week
   that already happened, nothing downstream is trustworthy.
4. **Projections.** Decide the source, measure its staleness, and have a
   fallback. This project assumed its preferred source was good and found it
   129 days stale with zeroed games on first contact (ADR-004) — so the
   provider's own projections became a first-class source rather than a
   backup. Check the timestamp on day one.
5. **The matching.** Lift `solve/assign.py` and the weight construction more
   or less whole. FLEX and SUPERFLEX are just richer eligibility sets.
6. **Availability.** `injury_status` + `practice_participation` +
   `depth_chart_order`, with the Sunday inactives window as a scheduled
   check. This is where football's real edge is.
7. **The reviewer and the surface.** Only once the numbers are right.

---

## 9. Files in this repo worth reading directly

- `docs/ADR.md` — 23 decisions with their reasoning and the evidence that
  forced them. The most useful single file here.
- `docs/CONTRACTS.md` — module boundaries and the trust boundary.
- `docs/PREFLIGHT.md` — what substitutes for a dry run when the API has none.
- `docs/DEPLOY.md` — the Home Assistant deployment trap, in full.
- `DOCS.md` — the user-facing manual; the provider-quirk sections near
  "How a run works" are the field notes.
- `rootfs/app/solve/budget.py` — the best example in the repo of a docstring
  that records what was measured, not just what was decided.
- `rootfs/app/sources/sleeper.py` — this project's existing Sleeper client,
  for NBA. Its header documents the live measurements, including the id
  crosswalk trap. Start here; the shape is already right.
- `rootfs/app/espn/write.py` — read the module docstring and
  `_preflight_lineup` even though Sleeper has no writes. The preflight is a
  checklist of everything a fantasy provider can reject you for.
