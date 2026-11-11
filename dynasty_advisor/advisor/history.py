"""Trade history across every season of a dynasty league.

A dynasty league is a new ``league_id`` each season, linked backwards by
``previous_league_id``. :func:`walk_chain` follows it to the first season.
Each season carries its own roster_id -> owner map, because rosters can change
hands (verified: one roster changed owner for 2026), and its own display names,
because users rename themselves (verified: same user id, two names).

Trade identity semantics (verified 2026-11-11):
  ``creator``        -> USER id of whoever proposed the trade
  ``consenter_ids``  -> ROSTER ids of the parties
So "who proposed" and "who accepted" are both recoverable. What is NOT
recoverable is any offer that was rejected or expired: the public API only
returns completed trades. Any "acceptance rate" is therefore a count of
accepts, never a rate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeasonInfo:
    league_id: str
    season: str
    complete: bool
    roster_owner: Dict[int, str]  # roster_id -> user_id
    names: Dict[str, str]  # user_id -> display name that season


@dataclass(frozen=True)
class TradePick:
    season: str
    round: int
    original_owner: Optional[str]  # user id of the roster the pick came from


@dataclass(frozen=True)
class TradeSide:
    user_id: str
    players_in: Tuple[str, ...]
    picks_in: Tuple[TradePick, ...]
    faab_in: int


@dataclass(frozen=True)
class Trade:
    transaction_id: str
    season: str
    week: int
    ts_ms: int
    creator: Optional[str]
    sides: Tuple[TradeSide, ...]

    @property
    def parties(self) -> Tuple[str, ...]:
        return tuple(s.user_id for s in self.sides)

    @property
    def accepters(self) -> Tuple[str, ...]:
        return tuple(u for u in self.parties if u != self.creator)

    def side(self, user_id: str) -> Optional[TradeSide]:
        for s in self.sides:
            if s.user_id == user_id:
                return s
        return None

    def given_by(self, user_id: str) -> Tuple[Tuple[str, ...], Tuple[TradePick, ...]]:
        """Players and picks this user sent away (= what the others received)."""
        players: List[str] = []
        picks: List[TradePick] = []
        for s in self.sides:
            if s.user_id != user_id:
                players.extend(s.players_in)
                picks.extend(s.picks_in)
        # In a 2-team trade everything the other side received came from us.
        # In a 3-team trade we cannot attribute senders from adds alone, so the
        # caller should treat multi-party trades as approximate.
        return tuple(players), tuple(picks)


def walk_chain(sleeper, league_id: str) -> List[SeasonInfo]:
    """Current season plus every predecessor, oldest first."""
    out: List[SeasonInfo] = []
    lid: Optional[str] = league_id
    first = True
    seen = set()
    while lid and lid not in ("0", "None") and lid not in seen:
        seen.add(lid)
        raw = sleeper.league(lid, frozen=not first)
        complete = raw.get("status") == "complete"
        frozen = complete and not first
        rosters = sleeper.rosters(lid, frozen=frozen)
        users = sleeper.users(lid, frozen=frozen)
        out.append(
            SeasonInfo(
                league_id=lid,
                season=str(raw["season"]),
                complete=complete,
                roster_owner={int(r["roster_id"]): r.get("owner_id") for r in rosters},
                names={u["user_id"]: u.get("display_name") or u["user_id"] for u in users},
            )
        )
        lid = raw.get("previous_league_id")
        first = False
    out.reverse()
    return out


def parse_trade(t: Mapping, info: SeasonInfo, week: int) -> Trade:
    owner = info.roster_owner
    sides: Dict[int, Dict] = {
        int(rid): {"players": [], "picks": [], "faab": 0} for rid in t.get("roster_ids") or ()
    }
    for pid, rid in (t.get("adds") or {}).items():
        sides.setdefault(int(rid), {"players": [], "picks": [], "faab": 0})["players"].append(pid)
    for dp in t.get("draft_picks") or ():
        sides.setdefault(int(dp["owner_id"]), {"players": [], "picks": [], "faab": 0})["picks"].append(
            TradePick(str(dp["season"]), int(dp["round"]), owner.get(int(dp["roster_id"])))
        )
    for wb in t.get("waiver_budget") or ():
        sides.setdefault(int(wb["receiver"]), {"players": [], "picks": [], "faab": 0})["faab"] += int(
            wb.get("amount") or 0
        )
    return Trade(
        transaction_id=str(t["transaction_id"]),
        season=info.season,
        week=week,
        ts_ms=int(t.get("status_updated") or t.get("created") or 0),
        creator=t.get("creator"),
        sides=tuple(
            TradeSide(
                user_id=owner.get(rid) or "roster:%d" % rid,
                players_in=tuple(v["players"]),
                picks_in=tuple(v["picks"]),
                faab_in=v["faab"],
            )
            for rid, v in sorted(sides.items())
        ),
    )


def load_trades(sleeper, seasons: Sequence[SeasonInfo], current_week: int) -> List[Trade]:
    trades: Dict[str, Trade] = {}
    for info in seasons:
        last = 18 if info.complete else min(18, current_week + 1)
        for week in range(0, last + 1):
            for t in sleeper.transactions(info.league_id, week, frozen=info.complete):
                if t.get("type") != "trade" or t.get("status") != "complete":
                    continue
                tid = str(t["transaction_id"])
                if tid not in trades:
                    trades[tid] = parse_trade(t, info, week)
    out = sorted(trades.values(), key=lambda x: x.ts_ms)
    log.info("trade history: %d completed trades over %d seasons", len(out), len(seasons))
    return out
