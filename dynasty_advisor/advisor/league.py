"""League, team and draft-pick model.

PICK OWNERSHIP (verified 2026-11-11)
====================================
Sleeper has no "picks owned" endpoint. Ownership is reconstructed as:
every roster owns its own R1..R<draft_rounds> for each tradable season, then
each ``traded_picks`` row ``(season, round, roster_id=original) -> owner_id``
overrides that default. Applied to a completed 2026 rookie draft, this
predicted the actual picker for **40 of 40 picks** (``draft.slot_to_roster_id``
gives each slot's original roster). The same check runs on every load
(:func:`check_picks_against_draft`) so a change in Sleeper's semantics is
noticed rather than silently believed.

``traded_picks`` keeps rows for seasons whose draft is already complete; those
picks are spent and are filtered out by :func:`tradable_seasons`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

log = logging.getLogger(__name__)

PlayerId = str
EMPTY_SLOT = "0"
NON_STARTING = {"BN", "IR", "TAXI"}

#: Which player positions may occupy which slot. Anything absent is forbidden
#: (ineligibility is absence, never a zero weight -- NBA ADR-002).
SLOT_ELIGIBILITY: Mapping[str, Set[str]] = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}

ROUND_NAMES = {1: "1st", 2: "2nd", 3: "3rd"}


def round_name(r: int) -> str:
    return ROUND_NAMES.get(r, "%dth" % r)


@dataclass(frozen=True)
class Pick:
    season: str
    round: int
    original_roster: int
    owner_roster: int

    @property
    def key(self) -> str:
        return "pick:%s:%d:%d" % (self.season, self.round, self.original_roster)


@dataclass
class Team:
    roster_id: int
    owner_id: Optional[str]
    display_name: str
    team_name: Optional[str]
    players: Tuple[PlayerId, ...]
    starters: Tuple[PlayerId, ...]
    reserve: Tuple[PlayerId, ...]
    taxi: Tuple[PlayerId, ...]
    wins: int = 0
    losses: int = 0
    ties: int = 0
    fpts: float = 0.0
    fpts_against: float = 0.0
    ppts: float = 0.0
    faab_used: int = 0

    @property
    def active(self) -> Tuple[PlayerId, ...]:
        hold = set(self.reserve) | set(self.taxi)
        return tuple(p for p in self.players if p not in hold)

    @property
    def bench(self) -> Tuple[PlayerId, ...]:
        hold = set(self.reserve) | set(self.taxi) | set(self.starters)
        return tuple(p for p in self.players if p not in hold)

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def win_pct(self) -> float:
        g = self.games
        return (self.wins + 0.5 * self.ties) / g if g else 0.5

    @property
    def record(self) -> str:
        return "%d-%d%s" % (self.wins, self.losses, "-%d" % self.ties if self.ties else "")


@dataclass
class League:
    league_id: str
    name: str
    season: str
    week: int
    roster_positions: Tuple[str, ...]
    settings: Dict
    scoring: Dict
    teams: Dict[int, Team]
    picks: List[Pick]
    tradable_seasons: Tuple[str, ...]
    total_rosters: int = 0
    pick_check: List[str] = field(default_factory=list)

    @property
    def starter_slots(self) -> Tuple[str, ...]:
        return tuple(p for p in self.roster_positions if p not in NON_STARTING)

    @property
    def max_active(self) -> int:
        """Active roster limit = starters + BN (IR and taxi are extra)."""
        return sum(1 for p in self.roster_positions if p not in ("IR", "TAXI"))

    @property
    def superflex(self) -> bool:
        return "SUPER_FLEX" in self.roster_positions or self.roster_positions.count("QB") > 1

    @property
    def trade_deadline(self) -> Optional[int]:
        d = self.settings.get("trade_deadline")
        return int(d) if d else None

    def team_of_user(self, user_id: str) -> Optional[Team]:
        for t in self.teams.values():
            if t.owner_id == user_id:
                return t
        return None

    def rostered(self) -> Set[PlayerId]:
        return {p for t in self.teams.values() for p in t.players}

    def picks_of(self, roster_id: int) -> List[Pick]:
        return sorted(
            (p for p in self.picks if p.owner_roster == roster_id),
            key=lambda p: (p.season, p.round, p.original_roster),
        )

    def reserve_statuses(self) -> Set[str]:
        """Injury statuses this league lets a player sit on IR with."""
        s = self.settings
        ok = {"IR", "PUP"}
        for key, status in (
            ("reserve_allow_out", "Out"),
            ("reserve_allow_doubtful", "Doubtful"),
            ("reserve_allow_sus", "Sus"),
            ("reserve_allow_na", "NA"),
            ("reserve_allow_cov", "COV"),
            ("reserve_allow_dnr", "DNR"),
        ):
            if s.get(key):
                ok.add(status)
        return ok


def _num(settings: Mapping, whole: str, dec: str) -> float:
    return float(settings.get(whole) or 0) + float(settings.get(dec) or 0) / 100.0


def parse_team(roster: Mapping, users_by_id: Mapping[str, Mapping]) -> Team:
    owner = roster.get("owner_id")
    u = users_by_id.get(owner or "", {})
    s = roster.get("settings") or {}
    return Team(
        roster_id=int(roster["roster_id"]),
        owner_id=owner,
        display_name=u.get("display_name") or "roster %s" % roster["roster_id"],
        team_name=((u.get("metadata") or {}).get("team_name") or "").strip() or None,
        players=tuple(roster.get("players") or ()),
        starters=tuple(roster.get("starters") or ()),
        reserve=tuple(roster.get("reserve") or ()),
        taxi=tuple(roster.get("taxi") or ()),
        wins=int(s.get("wins") or 0),
        losses=int(s.get("losses") or 0),
        ties=int(s.get("ties") or 0),
        fpts=_num(s, "fpts", "fpts_decimal"),
        fpts_against=_num(s, "fpts_against", "fpts_against_decimal"),
        ppts=_num(s, "ppts", "ppts_decimal"),
        faab_used=int(s.get("waiver_budget_used") or 0),
    )


def tradable_seasons(season: str, drafts: Iterable[Mapping], horizon: int = 3) -> Tuple[str, ...]:
    """Seasons whose picks still exist. The current season's picks are spent
    once its rookie draft is complete."""
    s = int(season)
    done = any(str(d.get("season")) == season and d.get("status") == "complete" for d in drafts)
    start = s + 1 if done else s
    return tuple(str(y) for y in range(start, start + horizon))


def pick_ownership(
    traded: Iterable[Mapping],
    roster_ids: Sequence[int],
    seasons: Sequence[str],
    rounds: int,
) -> List[Pick]:
    owner: Dict[Tuple[str, int, int], int] = {}
    for season in seasons:
        for rnd in range(1, rounds + 1):
            for rid in roster_ids:
                owner[(season, rnd, rid)] = rid
    for t in traded:
        k = (str(t["season"]), int(t["round"]), int(t["roster_id"]))
        if k in owner:
            owner[k] = int(t["owner_id"])
    return [Pick(s, r, o, own) for (s, r, o), own in sorted(owner.items())]


def check_picks_against_draft(
    traded: Iterable[Mapping], draft: Mapping, draft_picks: Iterable[Mapping]
) -> List[str]:
    """Self-check of the ownership rule against a completed draft.
    Returns human-readable mismatches (empty = rule holds)."""
    season = str(draft.get("season"))
    s2r = draft.get("slot_to_roster_id") or {}
    override = {
        (int(t["round"]), int(t["roster_id"])): int(t["owner_id"])
        for t in traded
        if str(t["season"]) == season
    }
    bad = []
    for p in draft_picks:
        orig = s2r.get(str(p.get("draft_slot")))
        if orig is None:
            bad.append("no slot_to_roster_id for slot %s" % p.get("draft_slot"))
            continue
        expected = override.get((int(p["round"]), int(orig)), int(orig))
        if expected != int(p["roster_id"]):
            bad.append(
                "%s R%s slot %s: expected roster %s, actual %s"
                % (season, p["round"], p["draft_slot"], expected, p["roster_id"])
            )
    return bad


def load_league(sleeper, league_id: str, week: int) -> League:
    raw = sleeper.league(league_id)
    users = {u["user_id"]: u for u in sleeper.users(league_id)}
    teams = {}
    for r in sleeper.rosters(league_id):
        t = parse_team(r, users)
        teams[t.roster_id] = t
    drafts = sleeper.drafts(league_id)
    traded = sleeper.traded_picks(league_id)
    settings = raw.get("settings") or {}
    seasons = tradable_seasons(raw["season"], drafts)
    picks = pick_ownership(traded, sorted(teams), seasons, int(settings.get("draft_rounds") or 0))

    check: List[str] = []
    for d in drafts:
        if d.get("status") == "complete":
            full = sleeper.draft(d["draft_id"])
            dp = sleeper.draft_picks(d["draft_id"], frozen=True)
            check = check_picks_against_draft(traded, full, dp)
            if check:
                log.warning("pick-ownership self-check FAILED (%d mismatches): %s", len(check), check[:3])
            else:
                log.info("pick-ownership self-check: %d/%d picks match %s draft", len(dp), len(dp), d.get("season"))

    return League(
        league_id=league_id,
        name=raw.get("name") or league_id,
        season=str(raw["season"]),
        week=week,
        roster_positions=tuple(raw.get("roster_positions") or ()),
        settings=settings,
        scoring=raw.get("scoring_settings") or {},
        teams=teams,
        picks=picks,
        tradable_seasons=seasons,
        total_rosters=int(raw.get("total_rosters") or len(teams)),
        pick_check=check,
    )
