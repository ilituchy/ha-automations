"""ESPN Fantasy v3 write client. The only module in this codebase that mutates.

There is no dry run
-------------------
A prior investigation chased one and it does not exist: ``executionType`` accepts
exactly ``{EXECUTE, CANCEL}``, and ``VALIDATE`` returns
``400 {"messages":["Invalid Input."]}`` with no ``details[]`` to learn from.

What substitutes for it is atomicity. A rejected POST changes *nothing* --
ESPN validates the whole transaction before applying any of it. So
client-side preflight + a single POST + a read-back is as safe as a dry run,
provided the preflight is thorough. That is the bargain this module makes, and
``_preflight`` is the part that has to hold up its end.

Irreversibility
---------------
Lineup changes are free to get wrong; you just set them again. Adds and drops
are not. They are permanent, they are attributed to the user by name in the
league's public activity feed, and only a ``PENDING`` waiver claim can be
withdrawn (via ``executionType: CANCEL``). An executed drop of a player someone
else immediately claims is unrecoverable. Hence: lineup and IR moves run
autonomously, add/drop never does.

Timeouts
--------
A timed-out write is **indeterminate, not failed.** Retrying a lineup POST is
harmless; retrying a waiver claim double-spends FAAB. So there is no retry here
at all -- :class:`WriteIndeterminate` propagates and the caller must re-read
``pending_transactions`` and the roster to find out what actually happened.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import httpx

from espn import ids
from espn.read import EspnAuthError, EspnConfig, EspnError, EspnRead
from models import Move, RosterEntry

log = logging.getLogger(__name__)

PlayerId = int
SlotId = int


class PreflightError(EspnError):
    """A check failed. No request was sent and nothing changed."""


class WriteIndeterminate(EspnError):
    """The request may or may not have been applied. Reconcile before acting."""


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    shadow: bool
    kind: str
    items: Sequence[Mapping[str, Any]]
    transaction_id: Optional[str] = None
    proposed_date: Optional[int] = None
    status: Optional[str] = None
    raw: Optional[Mapping[str, Any]] = None
    messages: Sequence[str] = field(default_factory=tuple)


class EspnWrite:
    """Target-state in, diff out.

    Callers describe the roster they *want*; this class works out the minimum
    set of moves to get there. Nobody outside this module constructs a
    transaction item, which is what keeps the invariants enforceable.
    """

    def __init__(
        self,
        cfg: EspnConfig,
        http: httpx.Client,
        read: EspnRead,
        *,
        journal: Optional[Any] = None,
        shadow_mode: bool = True,
        never_drop: Iterable[str] = (),
        max_faab_bid: int = 0,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.read = read
        self.journal = journal
        self.shadow_mode = shadow_mode
        self.never_drop = {n.strip().lower() for n in never_drop if n.strip()}
        self.max_faab_bid = int(max_faab_bid)

    # -- url / transport ---------------------------------------------------

    @property
    def _url(self) -> str:
        return (
            "%s/apis/v3/games/%s/seasons/%d/segments/0/leagues/%d/transactions/"
            % (ids.WRITE_HOST, ids.GAME, self.cfg.season, self.cfg.league_id)
        )

    def _post(self, body: Mapping[str, Any], kind: str) -> WriteResult:
        """Single POST. No retries, ever. See the module docstring."""
        assert "isLeagueManager" not in body, "never claim commissioner powers"

        journal_id = None
        if self.journal is not None:
            journal_id = self.journal.record_attempt(kind, body)

        # Writes authenticate on espn_s2 alone. SWID is a member label, carried
        # in the payload's memberId where ESPN wants it, not as a credential.
        cookies = {"espn_s2": self.cfg.espn_s2}
        try:
            r = self.http.post(
                self._url,
                content=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                cookies=cookies,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if self.journal is not None:
                self.journal.record_indeterminate(journal_id, str(exc))
            raise WriteIndeterminate(
                "write %s timed out: %s. Do NOT retry -- re-read "
                "pending_transactions and the roster first." % (kind, exc)
            ) from exc

        if r.status_code in (401, 403):
            if self.journal is not None:
                self.journal.record_failure(journal_id, r.status_code, r.text[:500])
            raise EspnAuthError("write rejected %d -- espn_s2 expired" % r.status_code)

        payload: Mapping[str, Any]
        try:
            payload = r.json()
        except ValueError:
            payload = {"_body": r.text[:500]}

        if r.status_code >= 400:
            msgs = tuple(str(m) for m in (payload.get("messages") or ()))
            details = payload.get("details") or []
            if self.journal is not None:
                self.journal.record_failure(journal_id, r.status_code, json.dumps(payload)[:2000])
            raise EspnError(
                "ESPN rejected %s (%d): %s%s"
                % (
                    kind,
                    r.status_code,
                    "; ".join(msgs) or r.text[:200],
                    " details=%r" % (details,) if details else "",
                )
            )

        tx = payload if isinstance(payload, dict) else {}
        result = WriteResult(
            ok=True,
            shadow=False,
            kind=kind,
            items=tuple(body.get("items") or ()),
            transaction_id=_str_or_none(tx.get("id")),
            proposed_date=_int_or_none(tx.get("proposedDate")),
            status=_str_or_none(tx.get("status")),
            raw=tx,
            messages=tuple(str(m) for m in (tx.get("messages") or ())),
        )
        if self.journal is not None:
            self.journal.record_success(journal_id, result)
        return result

    def _shadow(self, kind: str, items: Sequence[Mapping[str, Any]], body: Mapping[str, Any]) -> WriteResult:
        log.warning(
            "SHADOW MODE: would POST %s with %d item(s): %s",
            kind,
            len(items),
            json.dumps(items),
        )
        if self.journal is not None:
            self.journal.record_shadow(kind, body)
        return WriteResult(ok=True, shadow=True, kind=kind, items=tuple(items))

    # -- lineup ------------------------------------------------------------

    def set_lineup(
        self,
        target: Mapping[PlayerId, SlotId],
        *,
        roster: Optional[Sequence[RosterEntry]] = None,
        scoring_period: Optional[int] = None,
        team_id: Optional[int] = None,
    ) -> WriteResult:
        """Move the roster to ``target``. Only the differences are sent.

        ``target`` must be **total**: every rostered player maps to a slot,
        including bench and IR. A partial map is rejected rather than
        interpreted, because "unmentioned" could plausibly mean either "leave
        alone" or "bench", and guessing wrong moves a starter.
        """
        team_id = self.cfg.team_id if team_id is None else int(team_id)
        current = list(roster if roster is not None else self.read.roster(team_id))
        settings = self.read.league_settings()
        period = (
            scoring_period
            if scoring_period is not None
            else settings.current_scoring_period
        )

        self._preflight_lineup(team_id, current, target, settings)

        items = [
            {
                "playerId": int(p.player_id),
                "type": "LINEUP",
                "fromLineupSlotId": int(p.current_slot),
                "toLineupSlotId": int(target[p.player_id]),
            }
            for p in current
            if target[p.player_id] != p.current_slot
        ]

        if not items:
            log.info("lineup already optimal; no request sent")
            return WriteResult(ok=True, shadow=self.shadow_mode, kind="LINEUP", items=())

        body = {
            "teamId": int(team_id),
            "type": "ROSTER",
            "memberId": self.cfg.swid,
            "scoringPeriodId": int(period),
            "executionType": ids.EXEC_EXECUTE,
            "items": items,
        }
        if self.shadow_mode:
            return self._shadow("LINEUP", items, body)
        return self._post(body, "LINEUP")

    # -- acquisitions ------------------------------------------------------

    def add_drop(
        self,
        *,
        add: Optional[PlayerId] = None,
        drop: Optional[PlayerId] = None,
        bid: Optional[int] = None,
        waiver: bool = True,
        roster: Optional[Sequence[RosterEntry]] = None,
        team_id: Optional[int] = None,
    ) -> WriteResult:
        """Add and/or drop. **Requires explicit human approval upstream.**

        This method does not itself check for approval -- that is the state
        machine's job -- but it is the irreversible one, so the preflight here
        is the strictest in the module.

        ``waiver=True`` submits a claim (``PENDING``, cancellable, spends FAAB);
        ``waiver=False`` is an instant free-agent add. Which one is correct is a
        property of the *target player*, not of the league -- see
        ``acquisition_status`` on :class:`RosterEntry` -- so the caller must
        decide and the preflight cross-checks it.

        Verified against a throwaway league on 2026-10-20: executed ADD,
        executed DROP, ``PENDING`` waiver claim with and without a drop, and
        cancellation of both.
        """
        team_id = self.cfg.team_id if team_id is None else int(team_id)
        current = list(roster if roster is not None else self.read.roster(team_id))
        settings = self.read.league_settings()

        self._preflight_add_drop(team_id, current, add, drop, bid, settings, waiver)

        items: List[Dict[str, Any]] = []
        if add is not None:
            items.append(
                {
                    "playerId": int(add),
                    "type": "ADD",
                    "fromTeamId": 0,
                    "toTeamId": int(team_id),
                }
            )
        if drop is not None:
            items.append(
                {
                    "playerId": int(drop),
                    "type": "DROP",
                    "fromTeamId": int(team_id),
                    "toTeamId": 0,
                }
            )

        # ESPN gates the permitted items against `type`; see ids.TRAN_*. A lone
        # DROP is the odd one out -- it is a ROSTER mutation, not an
        # acquisition, and sending it as FREEAGENT (which this did until it was
        # run against a live league) is refused outright with
        # TRAN_INVALID_ITEM_TYPE_COUNT. The converse holds too: ROSTER cannot
        # carry an ADD, so the branch has to be on the item set, not on
        # `waiver` alone.
        if waiver:
            tran_type = ids.TRAN_WAIVER
        elif add is None:
            tran_type = ids.TRAN_ROSTER
        else:
            tran_type = ids.TRAN_FREEAGENT

        body: Dict[str, Any] = {
            "teamId": int(team_id),
            "type": tran_type,
            "memberId": self.cfg.swid,
            "scoringPeriodId": int(settings.current_scoring_period),
            "executionType": ids.EXEC_EXECUTE,
            "items": items,
        }
        if waiver and bid is not None:
            body["bidAmount"] = int(bid)

        kind = "ADD_DROP" if (add is not None and drop is not None) else (
            "ADD" if add is not None else "DROP"
        )
        if self.shadow_mode:
            return self._shadow(kind, items, body)
        return self._post(body, kind)

    def cancel(self, transaction_id: str, *, team_id: Optional[int] = None) -> WriteResult:
        """Withdraw a ``PENDING`` transaction. Executed ones cannot be undone.

        **A cancel is a new transaction, not a mutation of the old one.** ESPN
        creates a fresh record with its own ``id``, ``status: CANCELED``, and a
        ``relatedTransactionId`` pointing at the withdrawn claim -- which is
        where the target id has to go. Putting it in ``id`` (the obvious
        reading, and what this method did until it was run against a live
        league) gets ``409 "Transaction with ID null could not be found"``:
        ESPN was reporting a null ``relatedTransactionId`` the whole time,
        while the ``id`` we sent was being discarded and replaced.

        ``type`` and ``scoringPeriodId`` are mandatory too, and are read off
        the live claim rather than assumed: omitting ``type`` gives
        ``TRAN_INVALID_TYPE`` (with an unsubstituted ``%s`` in the message),
        and omitting ``scoringPeriodId`` gives "can only be executed in the
        current scoring period". ``items`` may be empty -- ESPN back-fills them
        from the related transaction.

        Looking the claim up first also makes this fail closed: if it is not
        pending, there is nothing to withdraw and we say so instead of posting.
        """
        team_id = self.cfg.team_id if team_id is None else int(team_id)
        self._require_ownership(team_id)

        txn_id = str(transaction_id)
        pending = {
            str(t.get("id")): t for t in self.read.pending_transactions(team_id)
        }
        live = pending.get(txn_id)
        if live is None:
            raise PreflightError(
                "transaction %s is not pending for team %d (pending: %s). Only "
                "a PENDING transaction can be withdrawn; an executed add or "
                "drop is permanent."
                % (txn_id, team_id, sorted(pending) or "none")
            )

        body = {
            "teamId": int(team_id),
            "relatedTransactionId": txn_id,
            "type": str(live.get("type")),
            "scoringPeriodId": int(live.get("scoringPeriodId") or 0),
            "memberId": self.cfg.swid,
            "executionType": ids.EXEC_CANCEL,
            "items": [],
        }
        if self.shadow_mode:
            return self._shadow("CANCEL", (), body)
        return self._post(body, "CANCEL")

    # -- preflight ---------------------------------------------------------

    def _require_ownership(self, team_id: int) -> None:
        """Check 1. The one that prevents editing someone else's team.

        ESPN will happily apply a commissioner's edit to another team and
        return ``200 EXECUTED``. A ``team_id`` typo is therefore not a failure
        mode that ESPN protects us from -- it is a silent, public, attributed
        roster change on a leaguemate's team. So we verify locally, every call.
        """
        mine = self.read.my_team_ids()
        if not mine:
            raise PreflightError(
                "could not confirm team ownership: no team in league %d lists "
                "our SWID as an owner. Check the SWID option."
                % self.cfg.league_id
            )
        if int(team_id) not in mine:
            raise PreflightError(
                "refusing to write to team %d -- we own %s. A typo here would "
                "edit a leaguemate's roster and ESPN would allow it."
                % (team_id, sorted(mine))
            )

    def _preflight_lineup(
        self,
        team_id: int,
        current: Sequence[RosterEntry],
        target: Mapping[PlayerId, SlotId],
        settings: Any,
    ) -> None:
        self._require_ownership(team_id)

        by_id = {p.player_id: p for p in current}

        # 2. Totality: every rostered player must be assigned.
        missing = sorted(set(by_id) - set(target))
        if missing:
            raise PreflightError(
                "target lineup is partial; unassigned players: %s. Pass a total "
                "map including bench and IR." % missing
            )
        # 3. No phantom players.
        extra = sorted(set(target) - set(by_id))
        if extra:
            raise PreflightError("target names players not on the roster: %s" % extra)

        # 4. Locked players cannot move. ESPN rejects the whole transaction if
        #    one item touches a locked player, so one stale projection would
        #    otherwise discard the entire day's optimization.
        for p in current:
            if p.lineup_locked and target[p.player_id] != p.current_slot:
                raise PreflightError(
                    "%s is lineup-locked (game started) but target moves him "
                    "%s -> %s" % (p.name, ids.slot_name(p.current_slot),
                                  ids.slot_name(target[p.player_id]))
                )

        # 5. Slot eligibility, from ESPN's own eligibleSlots.
        for pid, slot in target.items():
            p = by_id[pid]
            if slot not in p.eligible_slots:
                raise PreflightError(
                    "%s is not eligible for %s (eligible: %s)"
                    % (
                        p.name,
                        ids.slot_name(slot),
                        sorted(ids.slot_name(s) for s in p.eligible_slots),
                    )
                )

        # 6. Slot capacity.
        counts: Dict[SlotId, int] = {}
        for slot in target.values():
            counts[slot] = counts.get(slot, 0) + 1
        for slot, n in counts.items():
            cap = settings.lineup_slot_counts.get(slot)
            if cap is None:
                raise PreflightError(
                    "target uses slot %s which this league does not have"
                    % ids.slot_name(slot)
                )
            if n > cap:
                raise PreflightError(
                    "target puts %d players in %s but capacity is %d"
                    % (n, ids.slot_name(slot), cap)
                )

        # 7. IR occupancy must match ESPN's eligibility, which is its own rule.
        #    We never infer it from injury status; guessing produces a rejected
        #    transaction that takes the whole lineup down with it.
        for pid, slot in target.items():
            if slot == ids.IR_SLOT and not by_id[pid].ir_eligible:
                raise PreflightError(
                    "%s is not IR-eligible per ESPN; refusing to place him on IR"
                    % by_id[pid].name
                )

        # 8. Roster size must be conserved -- a lineup write must never be able
        #    to add or remove a player.
        if len(target) != len(current):
            raise PreflightError(
                "roster size would change (%d -> %d); a LINEUP transaction must "
                "not add or drop" % (len(current), len(target))
            )

        # 9. Sanity: do not "optimize" everyone onto the bench. If every
        #    starting slot is empty while playable players sit, something is
        #    wrong upstream (all projections zero, every source missing) and
        #    writing it would throw away a scoring day.
        starters = [s for s in target.values() if ids.is_starting_slot(s)]
        if not starters and len(current) > 0:
            raise PreflightError(
                "target benches the entire roster; refusing. This usually means "
                "every projection source failed -- check the run record."
            )

    def _preflight_add_drop(
        self,
        team_id: int,
        current: Sequence[RosterEntry],
        add: Optional[PlayerId],
        drop: Optional[PlayerId],
        bid: Optional[int],
        settings: Any,
        waiver: bool = True,
    ) -> None:
        self._require_ownership(team_id)

        if add is None and drop is None:
            raise PreflightError("add_drop called with neither an add nor a drop")

        by_id = {p.player_id: p for p in current}

        if drop is not None:
            if drop not in by_id:
                raise PreflightError("cannot drop %d -- not on our roster" % drop)
            name = by_id[drop].name.strip().lower()
            if name in self.never_drop:
                raise PreflightError(
                    "%s is on the never_drop list" % by_id[drop].name
                )
            if by_id[drop].lineup_locked:
                raise PreflightError(
                    "%s is locked (game started); cannot drop today"
                    % by_id[drop].name
                )

        if add is not None:
            if add in by_id:
                raise PreflightError("player %d is already on our roster" % add)
            # An add without a paired drop requires a genuinely open spot. The
            # original requirement is explicit that pickups never happen
            # without a paired drop, so a full roster plus no drop is a bug
            # upstream, not something to resolve here by picking a victim.
            roster_cap = sum(settings.lineup_slot_counts.values())
            if drop is None and len(current) >= roster_cap:
                raise PreflightError(
                    "roster is full (%d/%d) and no drop was paired with this add"
                    % (len(current), roster_cap)
                )
            self._check_acquisition_mode(add, waiver)

        if bid is not None:
            if bid < 0:
                raise PreflightError("negative FAAB bid")
            if bid > self.max_faab_bid:
                raise PreflightError(
                    "bid %d exceeds configured max_faab_bid %d"
                    % (bid, self.max_faab_bid)
                )
            if settings.faab_budget is not None and bid > settings.faab_budget:
                raise PreflightError(
                    "bid %d exceeds league FAAB budget %d"
                    % (bid, settings.faab_budget)
                )

    def _check_acquisition_mode(self, add: PlayerId, waiver: bool) -> None:
        """Is an instant add even legal for this player?

        Whether a pickup is an instant add or a waiver claim is a property of
        the *player*, not the league: on 2026-10-20 one live league had all 300
        of its available players on waivers while the other had 299 free agents
        and one on waivers. So there is no league-level setting to read, and
        getting it wrong means the add simply never happens.

        The ``WAIVERS``-but-instant case is a hard error -- ESPN refuses it with
        ``TRAN_PLAYER_NOT_FREEAGENT``, so we are only converting a remote
        rejection into a local message that names the fix.

        The reverse (a claim on a free agent) is **only logged**. It is
        certainly suboptimal, since it defers until the next waiver run a
        pickup that could have been instant, but whether ESPN accepts it has
        not been observed, and refusing it outright would block a legitimate
        add on an unverified belief.
        """
        target = self.read.player_in_pool(add)
        if target is None:
            raise PreflightError(
                "ESPN has no player row for id %d in league %d; refusing to "
                "acquire a player we cannot identify" % (add, self.cfg.league_id)
            )
        status = target.acquisition_status
        if status == "ONTEAM":
            raise PreflightError(
                "%s (%d) is on a roster in this league, not available"
                % (target.name, add)
            )
        if target.must_claim_on_waivers and not waiver:
            raise PreflightError(
                "%s (%d) is on waivers, so an instant add is impossible -- "
                "submit a claim instead (waiver=True). ESPN would reject this "
                "with TRAN_PLAYER_NOT_FREEAGENT."
                % (target.name, add)
            )
        if waiver and status == "FREEAGENT":
            log.warning(
                "submitting a waiver claim for %s (%d), who ESPN reports as a "
                "free agent -- an instant add would have been possible and "
                "immediate",
                target.name, add,
            )


def _str_or_none(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None
