# Preflight: what substitutes for a dry run

ESPN has no dry run. `executionType` accepts exactly `{EXECUTE, CANCEL}`;
`VALIDATE` returns `400 {"messages":["Invalid Input."]}` with no `details[]`.
(`FULL_PROJECTION`, which turns up in forum posts as a supposed dry-run mode, is
a read-side *stat split* concept that someone conflated with a write mode.)

What makes this survivable is atomicity: **a rejected POST changes nothing.**
ESPN validates the entire transaction before applying any item of it. So
client-side preflight + one POST + a read-back is equivalent in safety to a dry
run — *provided the preflight is thorough.* This file is the audit of that
proviso.

Every check is sourced from a read we already perform for other reasons, so
preflight costs no extra requests.

## Lineup writes — `EspnWrite._preflight_lineup`

| # | Check | Source | Why it exists |
|---|---|---|---|
| 1 | `team_id ∈ my_team_ids()` | `mTeam` → `teams[].owners` | ESPN honours a commissioner's edit to **another** team with `200 EXECUTED`. A `team_id` typo is not an error ESPN protects you from — it is a silent, public, attributed roster change on a leaguemate's team. |
| 2 | Target is total | roster | "Unmentioned" could mean leave-alone or bench. Guessing moves a starter. |
| 3 | No phantom players | roster | A stale roster view would otherwise send items for players we don't have. |
| 4 | No locked player moves | `lineupLocked` | ESPN rejects the **whole** transaction if one item touches a locked player, so a single stale projection would discard the entire day's optimization. |
| 5 | Slot eligibility | `player.eligibleSlots` | Never derived from a position map — ESPN's own array is authoritative and multi-position eligibility is league-configurable. |
| 6 | Slot capacity | `lineupSlotCounts` | Over-filling a slot is a guaranteed rejection. |
| 7 | IR occupancy | `eligibleSlots` **∧** `injuryStatus == OUT` | ESPN lists slot 13 broadly, so `IR ∈ eligibleSlots` alone marks the whole roster eligible and makes this check dead code. The conjunction is deliberate and deliberately conservative. **The rejection code for an ineligible IR move has not been observed** — see Unverified below. |
| 8 | Roster size conserved | roster | A `LINEUP` transaction must be structurally incapable of adding or dropping. |
| 9 | Not benching everyone | target | "All projection sources failed" and "bench the whole roster" produce identical output. Refusing costs one skipped day; executing costs a scoring day. |
| 10 | Empty diff ⇒ no request | diff | The cheapest check. Most days the lineup is already right. |

## Acquisition writes — `EspnWrite._preflight_add_drop`

Stricter, because the moves are irreversible and permanently attributed in the
league's public activity feed. Only a `PENDING` waiver claim can be withdrawn.

| # | Check | Why |
|---|---|---|
| 1 | Ownership | As above. |
| 2 | Drop target is rostered | — |
| 3 | Drop target not in `never_drop` | User-configured, case-insensitive. |
| 4 | Drop target not locked | — |
| 5 | Add target not already rostered | — |
| 6 | Add is paired with a drop when the roster is full | The original requirement is explicit that pickups never happen without a paired drop. A full roster plus no drop is an upstream bug, not something to resolve here by picking a victim. |
| 7 | `0 ≤ bid ≤ max_faab_bid` | User-configured ceiling. |
| 8 | `bid ≤ league faab_budget` | — |
| 9 | Not empty | — |
| 10 | Human approval | Enforced by `state.py`, not here — `claim_for_execution` is the gate. |

## After the write

1. Journal row is inserted **before** the socket is written. A write with no
   journal row cannot happen.
2. ESPN's returned `id`, `proposedDate` and `status` are journaled.
3. Read back the roster / `mPendingTransactions` to confirm.

## Timeouts are indeterminate, not failed

There is **no retry anywhere in the write path.** A replayed lineup POST is
harmless; a replayed waiver claim double-spends FAAB. A timeout raises
`WriteIndeterminate`, the journal row becomes `INDETERMINATE`, and
`Store.stuck_writes()` surfaces it. `startup_checks` refuses to run while any
unreconciled indeterminate write exists.

## Unverified write surfaces

Honest list. The lineup path's packet shape is the best-documented one; these
four have **never been observed executing** against a live league:

1. IR move rejection code for an ineligible player.
2. Executing `FREEAGENT` (instant add).
3. A standalone `ROSTER`-type drop.
4. Any GP-cap / position-limit `TRAN_*` rejection code.

This is why `shadow_mode` defaults to **true** and why the recommended sequence
is: discovery run → shadow runs → enable lineup/IR writes → only then gate-approve
add/drops. Probing items 1–4 belongs in a throwaway league, not on a live roster.
