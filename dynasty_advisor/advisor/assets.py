"""One value vocabulary for players and picks, so trades can mix them.

Value-over-replacement
----------------------
Raw FantasyCalc values are not additive for trades: two 900-point bench
players are not worth one 1,800-point starter, because the receiver has to cut
someone to make room and the 900s are barely better than what is free on the
waiver wire. So every asset is valued *over replacement*:

    vor = max(0, value - R)

where ``R`` is the median dynasty value of the best ``n_teams`` unrostered
players -- what anyone could pick up today. Picks get the same treatment,
since a pick becomes a roster spot when it is used. This has a useful side
effect: 4th-round picks (FantasyCalc ~800) come out near zero, which matches
how dynasty leagues actually treat them.

Pick tiers
----------
FantasyCalc splits the nearest pick class into Early / Mid / Late. A pick's
tier comes from the *original* team's projected finish (worst three -> Early,
best three -> Late) -- the same reason a rebuilding team's 1st is the
valuable one.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Dict, Iterable, Mapping, Optional, Sequence

from .league import League, Pick, round_name
from .values import Values

#: Age at which a player is a "veteran" a rebuilding team should sell, and at
#: or below which he is "young" -- what a rebuilder buys. By position because
#: running backs age years faster than quarterbacks.
VET_AGE = {"QB": 32, "RB": 26, "WR": 28, "TE": 29}
YOUNG_AGE = {"QB": 26, "RB": 24, "WR": 25, "TE": 25}

#: Dynasty age curve. FantasyCalc's dynasty values already price age in, but
#: they are a market average; the owner's rule is sharper ("i wouldnt want to
#: trade for travis kelce in dynasty"), so every strategy applies this curve
#: on top, scaled by the strategy's ``age`` strength.
#: Peak age by position, and how much value falls per year past it.
PEAK_AGE = {"QB": 30, "RB": 25, "WR": 27, "TE": 28}
DECLINE = {"QB": 0.06, "RB": 0.12, "WR": 0.09, "TE": 0.09}
#: Credit per year younger than peak, capped at YOUTH_CAP.
YOUTH_RATE, YOUTH_CAP, DECLINE_CAP = 0.04, 0.20, 0.70
#: A draft pick is the youngest asset there is.
PICK_YOUTH = 0.10


def age_curve(a: "Asset") -> float:
    """Unscaled adjustment for an asset's age: +0.20 for a 22-year-old WR,
    -0.70 for a 37-year-old TE, 0 at peak or with no age."""
    if a.kind == "pick":
        return PICK_YOUTH
    if a.age is None or a.position not in PEAK_AGE:
        return 0.0
    d = a.age - PEAK_AGE[a.position]
    if d < 0:
        return min(YOUTH_CAP, -d * YOUTH_RATE)
    return -min(DECLINE_CAP, d * DECLINE[a.position])


def age_adjusted(a: "Asset", strength: float) -> float:
    return a.vor * max(0.0, 1.0 + strength * age_curve(a))


@dataclass(frozen=True)
class Asset:
    key: str
    kind: str  # "player" | "pick"
    label: str
    position: str
    age: Optional[float]
    dyn: float  # raw dynasty value
    now: float  # raw rest-of-season value
    vor: float  # dynasty value over replacement
    pick: Optional[Pick] = None
    trend: float = 0.0  # FantasyCalc 30-day value change

    @property
    def is_vet(self) -> bool:
        return self.kind == "player" and self.age is not None and self.age >= VET_AGE.get(self.position, 99)

    @property
    def is_core(self) -> bool:
        """A young player whose value is still climbing: the asset a dynasty
        team builds around. Offered only for no-loss, no-veteran returns."""
        return self.kind == "player" and self.trend > 0 and self.age is not None \
            and self.age <= YOUNG_AGE.get(self.position, 0)

    @property
    def is_young(self) -> bool:
        if self.kind == "pick":
            return True
        return self.age is not None and self.age <= YOUNG_AGE.get(self.position, 0)


class AssetBook:
    def __init__(
        self,
        league: League,
        values: Values,
        players_index: Mapping[str, Mapping],
        finish_rank: Mapping[int, int],
    ) -> None:
        """``finish_rank``: roster_id -> projected finish, 1 = best."""
        self.league = league
        self.values = values
        self.index = players_index
        self.finish_rank = dict(finish_rank)
        self.replacement = self._replacement()
        self._cache: Dict[str, Asset] = {}

    # ---- helpers -------------------------------------------------------
    def positions(self, pid: str) -> Sequence[str]:
        p = self.index.get(pid) or {}
        fp = p.get("fantasy_positions") or ([p["position"]] if p.get("position") else [])
        if not fp and pid.isalpha():
            return ("DEF",)
        return tuple(fp)

    def name(self, pid: str) -> str:
        p = self.index.get(pid) or {}
        if p.get("full_name"):
            return p["full_name"]
        if p.get("first_name"):
            return "%s %s" % (p.get("first_name"), p.get("last_name") or "")
        v = self.values.players.get(pid)
        return v.name if v else pid

    def unrostered_values(self) -> Iterable[str]:
        rostered = self.league.rostered()
        return (pid for pid in self.values.players if pid not in rostered)

    def _replacement(self) -> float:
        n = max(1, self.league.total_rosters)
        free = sorted((self.values.dyn(p) for p in self.unrostered_values()), reverse=True)
        top = free[:n]
        return float(median(top)) if top else 0.0

    def tier(self, pick: Pick) -> Optional[str]:
        if pick.season != min(self.league.tradable_seasons):
            return None
        n = len(self.finish_rank) or 1
        rank = self.finish_rank.get(pick.original_roster, (n + 1) // 2)
        third = max(1, round(n * 0.3))
        if rank > n - third:
            return "Early"
        if rank <= third:
            return "Late"
        return "Mid"

    # ---- assets --------------------------------------------------------
    def player(self, pid: str) -> Asset:
        a = self._cache.get(pid)
        if a:
            return a
        pv = self.values.players.get(pid)
        info = self.index.get(pid) or {}
        pos = (self.positions(pid) or ("",))[0]
        age = pv.age if pv and pv.age else info.get("age")
        dyn = pv.dynasty if pv else 0.0
        a = Asset(
            key=pid,
            kind="player",
            label=self.name(pid),
            position=pos,
            age=float(age) if age else None,
            dyn=dyn,
            now=pv.redraft if pv else 0.0,
            vor=max(0.0, dyn - self.replacement),
            trend=pv.trend30 if pv else 0.0,
        )
        self._cache[pid] = a
        return a

    def pick(self, pick: Pick, owner_names: Mapping[int, str]) -> Asset:
        a = self._cache.get(pick.key)
        if a:
            return a
        tier = self.tier(pick)
        dyn = self.values.pick(pick.season, pick.round, tier)
        orig = owner_names.get(pick.original_roster, "roster %d" % pick.original_roster)
        label = "%s %s%s" % (pick.season, round_name(pick.round), " (%s)" % tier if tier else "")
        if pick.original_roster != pick.owner_roster:
            label += " via %s" % orig
        a = Asset(
            key=pick.key,
            kind="pick",
            label=label,
            position="PICK",
            age=None,
            dyn=dyn,
            now=0.0,
            vor=max(0.0, dyn - self.replacement),
            pick=pick,
        )
        self._cache[pick.key] = a
        return a


#: Package consolidation: the 2nd, 3rd... pieces of a package count for less,
#: because one great player beats two good ones on a roster with limited
#: starting slots. Industry-standard trade calculators apply the same idea.
DEPTH_WEIGHTS = (1.0, 0.8, 0.65, 0.5, 0.4)


def package_value(vals: Iterable[float]) -> float:
    vs = sorted((v for v in vals), reverse=True)
    return sum(v * DEPTH_WEIGHTS[min(i, len(DEPTH_WEIGHTS) - 1)] for i, v in enumerate(vs))
