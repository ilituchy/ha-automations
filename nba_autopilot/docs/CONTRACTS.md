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
    def roster(self, team_id: int, scoring_period: int | None = None) -> list[RosterEntry]
    def free_agents(self, limit: int = 200) -> list[RosterEntry]
    def games_used(self, team_id: int) -> dict[SlotId, int]   # per matchup period
    def schedule_map(self) -> ScheduleMap        # date <-> scoringPeriodId, per-team game dates
    def pending_transactions(self, team_id: int) -> list[dict]
    def my_team_ids(self) -> set[int]            # teams whose owners[] contains our SWID
```

`LeagueSettings` must expose, verbatim from ESPN (no defaults invented):
`scoring_type`, `lineup_slot_counts: dict[SlotId,int]`,
`lineup_slot_stat_limits: dict[SlotId,int] | None` (the GP cap — **absent means
no cap, which is a different thing from a cap of zero**), `scoring_items`
(statId -> points or category flag), `waiver_mode`, `faab_budget`,
`roster_lock_mode`, `matchup_period_boundaries`.

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
def set_lineup(self, team_id: int, target: Mapping[PlayerId, SlotId]) -> WriteResult
def add_drop(self, team_id: int, add: PlayerId | None, drop: PlayerId | None,
             bid: int | None = None) -> WriteResult
def cancel(self, team_id: int, transaction_id: str) -> WriteResult
```

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
) -> dict[PlayerId, SlotId]
```
Exact. Both constraints are `<=`, not `=` — benching is free and sometimes
optimal. Ineligible or no-game pairs are **absent from `weights`**, which is
not the same as weight 0. (ADR-002.)

```python
# solve/budget.py
def shadow_prices(
    candidates: Mapping[SlotId, list[float]],   # future candidate start values
    remaining:  Mapping[SlotId, int],           # remaining GP budget per slot
) -> dict[SlotId, float]                        # lambda_s
```
`lambda_s` = the `remaining[s]`-th largest element of `candidates[s]`, or `0.0`
when budget exceeds opportunities. Decision rule: start p in s iff `v > lambda_s`.

```python
# solve/weights.py
def category_weights(
    my_totals: Mapping[StatId, float], opp_totals: Mapping[StatId, float],
    my_var: Mapping[StatId, float],   opp_var: Mapping[StatId, float],
) -> dict[StatId, float]                        # w_k = phi(z_k)/s_k

def player_value(proj: Projection, w: Mapping[StatId, float],
                 risk: Mapping[StatId, float]) -> float
```
A tossup category is worth ~90x a decided one at the margin. Punting and
clinch-detection are emergent — there is no special-case code for either, and
adding any is a bug.

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
