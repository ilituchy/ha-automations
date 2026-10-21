# Internal module contracts

Frozen interfaces. Modules depend on these dataclasses, never on each other's
internals. Everything is stdlib + `httpx`; **no numpy, no scipy** (see ADR-001).

All times are timezone-aware. The canonical clock is `America/Los_Angeles`
(the user's run timezone); ESPN's `scoringPeriodId` boundaries are resolved
from `proTeamSchedules_wl`, never computed from a date.

---

## 1. Core value types (`app/models.py`)

```python
PlayerId = int          # ESPN player id
SlotId   = int          # ESPN lineupSlotId
StatId   = int          # ESPN stat id (see espn/ids.py)
ProTeamId = int

@dataclass(frozen=True)
class StatLine:
    """Per-game counting stats. Rate cats are carried as made/attempted."""
    values: Mapping[StatId, float]

@dataclass(frozen=True)
class Projection:
    player_id: PlayerId
    mean: Mapping[StatId, float]    # expected value for THIS game
    var:  Mapping[StatId, float]    # variance, same keys
    p_play: float                   # [0,1] probability he suits up
    minutes: float
    source: str                     # "darko" | "espn" | "blend:darko+espn"
    as_of: datetime

@dataclass(frozen=True)
class RosterEntry:
    player_id: PlayerId
    name: str
    current_slot: SlotId
    eligible_slots: frozenset[SlotId]
    pro_team_id: ProTeamId
    injury_status: str              # normalized: ACTIVE|GTD|OUT|IR_ELIGIBLE|...
    lineup_locked: bool
    games_this_week: int
```

## 2. `espn/read.py` — `EspnRead`

Read-only. Host `lm-api-reads.fantasy.espn.com`. Never mutates.

```python
class EspnRead:
    def __init__(self, cfg: EspnConfig, http: httpx.Client) -> None: ...

    def league_settings(self) -> LeagueSettings
    def roster(self, team_id: int | None = None,
               scoring_period: int | None = None) -> list[RosterEntry]
    def free_agents(self, limit: int = 200) -> list[RosterEntry]
    def my_team_ids(self) -> set[int]            # teams whose owners[] contains our SWID
    def pending_transactions(self, team_id: int | None = None) -> list[Mapping]
    def player_projections(self, player_ids) -> Mapping[PlayerId, Mapping[str, Any]]

    # Games-played cap inputs. Neither is sufficient alone -- see below.
    def games_used(self, team_id: int | None = None) -> dict[SlotId, int]
    def season_games_played(self, team_id: int | None = None) -> int | None

    # Calendar primitives. Dates are produced here and nowhere else.
    def scoring_period_dates(self) -> dict[int, date]
    def pro_team_scoring_periods(self) -> dict[ProTeamId, dict[int, int]]
```

`team_id` defaults to the configured team on every method that takes it, so a
caller never has to restate it and cannot accidentally restate it *wrong* — the
one mistake ESPN honours silently (see `my_team_ids`).

`LeagueSettings` must expose, verbatim from ESPN (no defaults invented):
`scoring_type`, `lineup_slot_counts: dict[SlotId,int]`,
`lineup_slot_stat_limits: dict[SlotId, StatLimit] | None` (the GP cap —
**absent means no cap, which is a different thing from a cap of zero**),
`scoring_items` (statId -> points or category flag), `categories`,
`waiver_mode`, `faab_budget`, `roster_lock_mode`, `current_scoring_period`,
`current_matchup_period`, `matchup_period_boundaries`.

### What the live leagues actually return

Findings from probing the live leagues, each of which changed code:

* **A dropped player lingers in the roster `entries` array**, still carrying
  his old `lineupSlotId`. The only fields that say otherwise are
  `playerPoolEntry.status` (`WAIVERS`) and `onTeamId` (`0`). So `len(entries)`
  is **not** a roster count, and `roster()` filters on positive evidence of
  departure — see ADR-016. Confirmed not to be read-host caching (8 polls over
  25s, identical).
* **`playerPoolEntry.status` is the only answer to "how can this player be
  acquired".** It is per-player, not per-league: see §3.

* **`scheduleSettings.matchupPeriods` is self-referential.** All 22 entries map
  period *n* to `[n]`, so it carries no week structure at all.
  `matchup_period_boundaries` is therefore **empty** in practice —
  `_matchup_windows()` detects the degenerate shape and discards it rather than
  letting a 1-day "week" reach the optimizer. Week structure comes from
  `solve/calendar.py` instead, cross-checked against ESPN's own
  `currentMatchupPeriod` on every run.
* **Scoring periods are daily and linear** in US Eastern. That makes the
  calendar a single anchor date plus an offset, and makes it falsifiable:
  `SeasonCalendar.from_period_dates` validates every probed period against the
  model and records (never repairs) disagreements.
* **Both team-level games-played sources are untrustworthy.**
  `cumulativeScore.statBySlot` came back `None` on every matchup row of both
  leagues, and team `valuesByStat["42"]` is season-to-date, not per-matchup. So
  `games_used()` returns the per-slot shape when ESPN populates it, and
  `usage.py` reconciles it against a season-to-date delta measured from a
  baseline snapshot. See §`usage.py`.
* **Only stat split `"10"`** (season projection, per-game) is populated in
  October. The split key is `f"{statSourceId}{statSplitTypeId}"`.

`lineup_slot_stat_limits` is keyed at slot **15** in both leagues — a
pseudo-slot that appears in neither league's `lineupSlotCounts` — with
`limitValue` **divided by seven** alongside `matchupLimitPerScoringPeriod:
true`. It is one pooled, team-wide, per-matchup-period budget, not a per-slot
one; `budget.matchup_cap()` raises `PooledCapUnsupported` rather than guess if
a league ever keys it at a real slot.

## 3. `espn/write.py` — `EspnWrite`

Host `lm-api-writes.fantasy.espn.com`. Single `/transactions/` POST.

**Invariants enforced in code, not by convention:**
1. Accepts a *target state*, computes a **diff**, and POSTs only the diff.
   An empty diff is a no-op that never touches the network.
2. Runs `preflight()` (10 checks, [`PREFLIGHT.md`](./PREFLIGHT.md)) and raises on any failure.
3. `team_id` must be in `EspnRead.my_team_ids()`. Checked every call.
4. Never serializes `isLeagueManager`. Never sends `SWID` as a write credential.
5. Every call is journaled to SQLite **before** the request and updated after,
   with ESPN's returned `id` + `proposedDate`. A write with no journal row
   cannot happen (the journal insert is the thing that produces the idempotency key).
6. On timeout: **never retry.** Raise `WriteIndeterminate`, which forces the
   caller to re-read `pending_transactions` + `roster` and reconcile.

```python
def set_lineup(self, target: Mapping[PlayerId, SlotId], *,
               roster: Sequence[RosterEntry] | None = None,
               scoring_period: int | None = None,
               team_id: int | None = None) -> WriteResult
def add_drop(self, *, add: PlayerId | None = None, drop: PlayerId | None = None,
             bid: int | None = None, waiver: bool = True,
             roster: Sequence[RosterEntry] | None = None,
             team_id: int | None = None) -> WriteResult
def cancel(self, transaction_id: str, *, team_id: int | None = None) -> WriteResult
```

### What ESPN actually accepts

All four write surfaces were exercised against a throwaway league on
2026-10-20. Each of the following was wrong in the code until a real request
said so; see ADR-015 and ADR-016.

**`type` is not a label — ESPN gates the permitted `items` against it.**

| `type` | permitted items | result |
| --- | --- | --- |
| `FREEAGENT` | at most one `ADD`, optional `DROP` | executes immediately |
| `WAIVER` | one `ADD`, optional `DROP` | lands `PENDING`, cancellable |
| `ROSTER` | `DROP` only (and lineup moves) | executes immediately |

A lone `DROP` sent as `FREEAGENT` is refused with
`TRAN_INVALID_ITEM_TYPE_COUNT` ("may only contain one item for MoveType:
ADD"). That message is easy to misread as "one item total": it is not, and an
`ADD`+`DROP` pair under `FREEAGENT` is accepted. Conversely `ROSTER` cannot
carry an `ADD` — `TRAN_INVALID_MOVETYPE`. So the branch is on the item set,
not on the waiver flag.

**A cancel is a new transaction, not a mutation.** ESPN creates a fresh record
with its own `id`, `status: CANCELED`, and `relatedTransactionId` pointing at
the withdrawn claim — which is where the target id must go. Putting it in `id`
gets `409 "Transaction with ID null could not be found"`: ESPN was reporting a
null `relatedTransactionId` the whole time while discarding the `id` sent.
`type` and `scoringPeriodId` are also mandatory and are read off the live
claim. `items` may be `[]`; ESPN back-fills them.

**Transaction ids are UUID strings**, not integers.

**Whether a pickup is an instant add or a waiver claim is a property of the
player, not the league.** One live league had all 300 available players on
`WAIVERS`; the other had 299 `FREEAGENT` and one on `WAIVERS`. There is no
league setting that answers this — only `RosterEntry.acquisition_status`.
An instant add of a `WAIVERS` player is refused with
`TRAN_PLAYER_NOT_FREEAGENT`, so `_check_acquisition_mode` blocks it locally.

## 4. `sources/` — projection + availability inputs

Every source is a class with this shape. A source that fails returns `None`
from `fetch()` and logs; it never raises into the pipeline, and its absence
must be *visible* in the run record, not silently defaulted.

```python
class Source(Protocol):
    name: str
    max_staleness: timedelta          # beyond this, fetch() returns None
    def fetch(self) -> SourceResult | None: ...

@dataclass(frozen=True)
class SourceResult:
    as_of: datetime
    payload: Any
    staleness_ok: bool
```

Required sources:
- `sources/darko.py` — per-100 + minutes + pace. **Must surface `as_of`; a
  July snapshot in October is a hard fail, not a warning.** Fallback: ESPN's
  own `statSourceId=1` projections.
- `sources/sleeper.py` — `GET https://api.sleeper.app/v1/players/nba`, has a
  real `GTD` tier that ESPN's feed does not emit.
- `sources/injury_report.py` — official NBA injury report PDF from
  `ak-static.cms.nba.com`. `403` = not yet published (clean probe), `200` = parse.
- `sources/nba_schedule.py` — game dates, tipoff times, back-to-backs, pace.
  **`stats.nba.com` and `cdn.nba.com` are blocked from cloud egress** but fine
  from a residential IP; still, prefer ESPN's site API as primary.

Joining ESPN ids to NBA ids goes through one crosswalk module with an explicit
unmatched list. Unmatched players fall back to ESPN projections — never to zero.

## 5. `solve/` — the optimizer

Pure functions. No I/O, no clock, no config reads. Fully unit-testable.

```python
# solve/assign.py
def max_weight_assignment(
    weights: Mapping[tuple[PlayerId, SlotId], float],
    slot_capacity: Mapping[SlotId, int],
    *, locked: Mapping[PlayerId, SlotId] | None = None,
) -> dict[PlayerId, SlotId]
```
Exact (Jonker-Volgenant, pure Python). Both constraints are `<=`, not `=`, so
the routine may leave a row unassigned at cost 0. Ineligible pairs are
**absent from `weights`**, which is not the same as weight 0 (ADR-002).
`locked` players are pre-placed and consume their slot's capacity — including
on the bench, which is easy to forget and produces an infeasible plan when
forgotten.

**Consequence callers must respect:** because an unassigned row costs 0, any
caller that needs a *total* assignment must shift all weights strictly
positive. `pipeline.py` does. Skipping the shift silently drops
negative-valued players and discards the whole day's lineup.

```python
# solve/budget.py
def matchup_cap(settings) -> float | None                 # raises PooledCapUnsupported
def remaining_budget(settings, games_used: float) -> GamesBudget | None
def shadow_price(candidates: Sequence[tuple[float, float]],
                 budget: GamesBudget | None) -> float     # one scalar lambda
def utilization_ratio(candidates, budget) -> float        # rho = remaining / opportunities
```
One pooled `lambda`, not a per-slot vector: the cap is team-wide (see §2).
`candidates` are `(conditional_value, p_play)` per startable player-game left
in the week. `lambda` is the value of the marginal game the budget reaches —
`0.0` when slack, `+inf` when exhausted. Decision rule: **start p iff
`v > lambda`**, where `v` is conditional on playing. `p_play` cancels out of
the threshold (it scales benefit and cost alike) but still scales the reduced
cost, so it orders the assignment. `rho >= 1` means the cap is not binding;
`rho < 1` is where playable players get benched on purpose, and surfacing it in
the run record is what makes that explicable instead of looking like a bug.

```python
# solve/value.py
def build_value_model(settings, matchup=None) -> PointsValue | CategoryValue
    #  .conditional_value(projection) -> float     # value GIVEN he plays
```
Both leagues are `H2H_POINTS`, so `PointsValue` is the live path and value is
exactly linear: `sum(projected per-game stat * points per stat)`. A points
league is a degenerate one-category league, so `CategoryValue` reuses the same
risk model with "the category" being fantasy points. A tossup category is worth
~90x a decided one at the margin. Punting and clinch-detection are emergent —
there is no special-case code for either, and adding any is a bug.

The risk term needs a live matchup score to sign the margin, and
`cumulativeScore` is null on every live matchup row, so it is currently
**inert** and says so in the run notes.

```python
# solve/calendar.py
@dataclass(frozen=True)
class SeasonCalendar:
    def date_of(period) / period_of(day)
    def matchup_period_of(period) -> int
    def periods_in_matchup(mp) -> list[int]       # includes gameless days
    def remaining_periods(mp, from_period) -> list[int]
    def verify(espn_scoring_period, espn_matchup_period) -> str | None
    trustworthy: bool                             # False if periods aren't linear

def team_games_in(periods, schedule, pro_team_id) -> int
```
Weeks are Monday–Sunday, derived from the NBA calendar because ESPN's own
`matchupPeriods` is useless (§2). `verify()` reproduces ESPN's
`currentMatchupPeriod` and is the self-falsifying check: on disagreement the
caller prices **no** cap rather than pricing a wrong one.

`team_games_in` scores an unknown team **zero** — `proTeamId 0` is ESPN's "no
NBA team" sentinel, so an unsigned player correctly gets no games. That
exclusion is total and otherwise invisible, so `pipeline.py` reports it.

```python
# usage.py  (not in solve/ -- it reads ESPN and the DB)
def measure(read, store, *, league_id, team_id, calendar,
            scoring_period, matchup_period) -> GamesUsed
    # .games: float  .trustworthy: bool  .source: str
```
Reconciles the two untrustworthy ESPN sources (§2). The weekly figure is a
difference of two season-to-date readings, so it needs a baseline captured on
the week's **first day** — hence `gp_snapshots`, written `INSERT OR IGNORE` so
the earliest reading of a week wins. On disagreement it takes the **larger**:
over-counting costs a marginal start, under-counting a hard cap costs a legal
lineup. `trustworthy=False` is a real answer and must never be rounded to
"zero games used".

```python
# pipeline.py
def plan_lineup(settings, roster, projections, *, scoring_period,
                matchup_period, schedule, calendar,
                games_used=None, availability=None) -> LineupPlan
    # .target: Mapping[PlayerId, SlotId]   TOTAL -- every rostered player
    # .moves / .lam / .utilization / .feasible / .notes
```
**The lineup is a perfect matching, not a top-N selection.** Slot capacity
equals non-IR roster size exactly (e.g. 10 starters + 3 bench = 13 players), so
on a light slate you *cannot* bench the gameless players — there is nowhere to
put them. They occupy starting slots for free, because the cap counts games
*played*. A planner that picked "the best ten" would emit a map ESPN rejects.

The cap is priced only when **both** the calendar and the usage reading are
trustworthy; otherwise `lam = 0` and a note says why. `validate_target` then
re-checks the result independently (totality, eligibility, locked movement, IR
eligibility, per-slot capacity) and a failure returns `feasible=False` with the
*current* lineup as the target — a bad plan is a reported non-event, never an
exception and never a write.

## 6. `state.py` — approval state machine

SQLite at `/data/autopilot.db`. WAL. One table per concern, no ORM.

States: `PROPOSED -> APPROVED -> EXECUTING -> DONE | FAILED | EXPIRED | REJECTED`

- `PROPOSED -> EXECUTING` is a single `UPDATE ... WHERE state='APPROVED'`
  guarded transition; `rowcount == 0` means someone else took it. This is the
  only defense against a double-tap on the notification.
- Every proposal carries a ULID `run_id`, and **every notification action id
  embeds it** — HA action ids are globally registered, so a stale notification
  in the shade will otherwise fire today's automation.
- Approval never carries over. Expiry sweep every 15 min; `data.timeout=21600`.

## 7. `ha/` — Home Assistant bridge

Supervisor proxy, `SUPERVISOR_TOKEN`, `http://supervisor/core/api`.

```python
class HaBridge:
    def notify(self, device: str, title: str, message: str,
               actions: list[Action], tag: str) -> None
    def todo_replace(self, entity: str, items: list[str]) -> None
    def persistent_notification(self, title: str, message: str) -> None
    def listen_actions(self) -> Iterator[ActionEvent]   # WebSocket
```

`ActionEvent` handling **must** verify (a) the action id's embedded `run_id`
matches a live proposal, and (b) `context.user_id` is the configured owner.
An action event failing either check is logged and dropped.

---

## Trust boundary

Everything crossing into this process from the network — ESPN JSON, scraped
news, LLM output, HA events — is **untrusted input**. Carried verbatim from the
original requirements and extended to the LLM:

> Text on web pages is treated as information, never as instructions.

LLM output is schema-validated, range-clamped, and cross-checked against a
structured field before it can influence a decision. The LLM never does
arithmetic, never chooses, and never writes.
