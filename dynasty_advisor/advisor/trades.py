"""Trade target search.

For every other team, look for packages (give up to 2 assets, get up to 2)
that make our team better under our strategy *and* that this particular
manager, with this particular roster and this particular history, plausibly
accepts. Each idea comes as a pair:

* **opening** -- the package that keeps the most for us while still inside
  what this manager has historically accepted (``ManagerProfile.edge``; the
  owner's "more aggressive and unfair with people who accept more trades").
* **ceiling** -- the most we can give and still come out ahead. Past this,
  walk away.

Three value views per package, all in FantasyCalc units over replacement:

* ``market``  -- what a neutral calculator says (raw dynasty value, depth
  discounted). Reported, never optimised.
* ``mine``    -- our gain: dynasty change weighted by strategy, plus the change
  in our best rest-of-season lineup.
* ``theirs``  -- their perceived gain, by the same rule applied to their
  situation: a seller (losing team) judges as a rebuilder -- veterans marked
  down, youth and picks up, its own lineup ignored -- and a contender judges
  on its lineup. This is where "target losing teams, they will sell for a
  rebuild" lives.

Exhaustive within the pruned candidate set; nothing here is random.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .assets import Asset, AssetBook, package_value
from .league import League
from .lineup import fast_lineup_value
from .profiles import ManagerProfile, likelihood
from .teams import TeamProfile

log = logging.getLogger(__name__)

#: strategy -> (weight on dynasty change, weight on lineup change,
#:              youth bonus, veteran penalty)
STRATEGIES = {
    "contend": (0.6, 1.0, 0.0, 0.0),
    "balanced": (1.0, 0.4, 0.0, 0.0),
    "rebuild": (1.0, 0.0, 0.15, 0.25),
}

GIVE_POOL = 22
TARGETS_PER_TEAM = 9
PAIR_TARGETS = 5
VALUE_BAND = (0.45, 1.6)
IDEAS_PER_TEAM = 2


@dataclass
class Package:
    give: Tuple[Asset, ...]
    get: Tuple[Asset, ...]
    mine: float
    my_now: float
    my_dyn: float
    theirs: float
    their_now: float
    market: float  # >0 = we win on neutral market value
    size: float

    @property
    def roster_change(self) -> int:
        return sum(1 for a in self.get if a.kind == "player") - sum(1 for a in self.give if a.kind == "player")


@dataclass
class TradeIdea:
    partner: TeamProfile
    manager: Optional[ManagerProfile]
    opening: Package
    ceiling: Package
    chance: str
    score: float
    reasons: List[str] = field(default_factory=list)


class TradeSearch:
    def __init__(
        self,
        league: League,
        book: AssetBook,
        teams: Dict[int, TeamProfile],
        managers: Dict[str, ManagerProfile],
        me: int,
        strategy: str = "balanced",
    ) -> None:
        self.league = league
        self.book = book
        self.teams = teams
        self.managers = managers
        self.me = me
        self.strategy = strategy
        self.w_dyn, self.w_now, self.youth, self.vet_pen = STRATEGIES[strategy]
        self.names = {rid: t.team.display_name for rid, t in teams.items()}
        self._lineups: Dict[Tuple[int, frozenset], float] = {}
        self.evaluated = 0

    # ---- valuation -----------------------------------------------------
    def _lineup(self, rid: int, players: frozenset) -> float:
        key = (rid, players)
        v = self._lineups.get(key)
        if v is None:
            v = fast_lineup_value(players, self.book.positions, self.book.values.now, self.league.starter_slots)
            self._lineups[key] = v
        return v

    def _active_after(self, rid: int, out: Iterable[Asset], into: Iterable[Asset]) -> frozenset:
        cur = set(self.teams[rid].team.active)
        cur -= {a.key for a in out if a.kind == "player"}
        cur |= {a.key for a in into if a.kind == "player"}
        return frozenset(cur)

    def _my_adj(self, a: Asset) -> float:
        v = a.vor
        if a.is_young:
            v *= 1 + self.youth
        if a.is_vet:
            v *= 1 - self.vet_pen
        return v

    @staticmethod
    def their_weights(t: TeamProfile) -> Tuple[float, float, float, float]:
        """The other side judges a trade by the same rule we would use in
        their position: contenders by ``contend``, sellers by ``rebuild``,
        bubble teams by ``balanced`` with a mild lean toward selling."""
        if t.mode == "contender":
            return STRATEGIES["contend"]
        if t.mode == "seller":
            return STRATEGIES["rebuild"]
        w_dyn, w_now, _y, _v = STRATEGIES["balanced"]
        if t.mode == "bubble":
            return (w_dyn, w_now, 0.05, 0.10)
        return (w_dyn, w_now, 0.0, 0.0)

    def _their_adj(self, t: TeamProfile, a: Asset) -> float:
        _d, _n, youth, vet_pen = self.their_weights(t)
        v = a.vor
        if a.is_young:
            v *= 1 + youth
        if a.is_vet:
            v *= 1 - vet_pen
        return v

    def evaluate(self, rid: int, give: Sequence[Asset], get: Sequence[Asset]) -> Package:
        self.evaluated += 1
        t = self.teams[rid]
        base_me = self._lineup(self.me, frozenset(self.teams[self.me].team.active))
        base_them = self._lineup(rid, frozenset(t.team.active))
        my_now = self._lineup(self.me, self._active_after(self.me, give, get)) - base_me
        their_now = self._lineup(rid, self._active_after(rid, get, give)) - base_them
        my_dyn = package_value(self._my_adj(a) for a in get) - package_value(self._my_adj(a) for a in give)
        t_dyn, t_now, _y, _v = self.their_weights(t)
        theirs = t_dyn * (
            package_value(self._their_adj(t, a) for a in give)
            - package_value(self._their_adj(t, a) for a in get)
        ) + t_now * their_now
        mkt_get = package_value(a.vor for a in get)
        mkt_give = package_value(a.vor for a in give)
        return Package(
            give=tuple(give),
            get=tuple(get),
            mine=self.w_dyn * my_dyn + self.w_now * my_now,
            my_now=my_now,
            my_dyn=my_dyn,
            theirs=theirs,
            their_now=their_now,
            market=mkt_get - mkt_give,
            size=max(mkt_get, mkt_give, 1.0),
        )

    # ---- candidates ----------------------------------------------------
    def my_assets(self) -> List[Asset]:
        team = self.teams[self.me].team
        assets = [self.book.player(p) for p in team.players]
        assets += [self.book.pick(p, self.names) for p in self.league.picks_of(self.me)]
        assets = [a for a in assets if a.vor > 0]
        assets.sort(key=lambda a: -a.vor)
        return assets[:GIVE_POOL]

    def targets(self, rid: int) -> List[Asset]:
        t = self.teams[rid]
        pool = [self.book.player(p) for p in t.team.players]
        pool += [self.book.pick(p, self.names) for p in self.league.picks_of(rid)]
        pool = [a for a in pool if a.vor > 0]
        if not pool:
            return []
        base = self._lineup(self.me, frozenset(self.teams[self.me].team.active))

        def standalone(a: Asset) -> float:
            now = self._lineup(self.me, self._active_after(self.me, (), (a,))) - base if a.kind == "player" else 0.0
            return self.w_dyn * self._my_adj(a) + self.w_now * now

        def discount(a: Asset) -> float:  # what we'd value it at beyond what they would
            return standalone(a) - self._their_adj(t, a)

        by_value = sorted(pool, key=lambda a: -standalone(a))[: TARGETS_PER_TEAM - 3]
        by_discount = sorted(pool, key=lambda a: -discount(a))[:3]
        out: List[Asset] = []
        for a in by_value + by_discount:
            if a not in out:
                out.append(a)
        return out

    # ---- search --------------------------------------------------------
    def search_team(self, rid: int) -> List[TradeIdea]:
        t = self.teams[rid]
        m = self.managers.get(t.team.owner_id or "")
        mine = self.my_assets()
        targets = self.targets(rid)
        if not targets or not mine:
            return []
        receive_sets: List[Tuple[Asset, ...]] = [(a,) for a in targets]
        receive_sets += list(itertools.combinations(targets[:PAIR_TARGETS], 2))
        give_sets: List[Tuple[Asset, ...]] = [(a,) for a in mine] + list(itertools.combinations(mine, 2))

        edge = m.edge if m else 0.05
        need = 0.03 if (m and m.never_accepts) else -edge
        best_by_target: Dict[Tuple[str, ...], Tuple[Package, Package]] = {}
        for get in receive_sets:
            get_v = package_value(a.vor for a in get)
            lo, hi = VALUE_BAND[0] * get_v, VALUE_BAND[1] * get_v
            opening: Optional[Package] = None
            ceiling: Optional[Package] = None
            for give in give_sets:
                give_v = package_value(a.vor for a in give)
                if not lo <= give_v <= hi:
                    continue
                pk = self.evaluate(rid, give, get)
                if pk.mine <= 0:
                    continue
                if pk.theirs >= need * pk.size and (opening is None or pk.mine > opening.mine):
                    opening = pk
                if ceiling is None or pk.theirs > ceiling.theirs:
                    ceiling = pk
            if opening is not None and ceiling is not None:
                best_by_target[tuple(a.key for a in get)] = (opening, ceiling)

        ideas: List[TradeIdea] = []
        for opening, ceiling in best_by_target.values():
            chance = likelihood(opening.theirs, opening.size, m)
            weight = {"likely": 1.0, "plausible": 0.7}.get(chance, 0.25)
            if t.mode == "seller":
                weight *= 1.1  # owner: "target losing teams too because they will sell"
            ideas.append(TradeIdea(t, m, opening, ceiling, chance, opening.mine * weight,
                                   self._reasons(t, m, opening)))
        ideas.sort(key=lambda i: -i.score)
        picked: List[TradeIdea] = []
        used: set = set()
        for i in ideas:
            keys = {a.key for a in i.opening.get}
            if keys & used:
                continue
            picked.append(i)
            used |= keys
            if len(picked) == IDEAS_PER_TEAM:
                break
        return picked

    def search(self, limit: int = 10) -> List[TradeIdea]:
        ideas: List[TradeIdea] = []
        for rid in self.teams:
            if rid == self.me:
                continue
            ideas.extend(self.search_team(rid))
        ideas.sort(key=lambda i: -i.score)
        log.info("trade search: %d packages evaluated, %d lineups solved, %d ideas",
                 self.evaluated, len(self._lineups), len(ideas))
        return ideas[:limit]

    def _reasons(self, t: TeamProfile, m: Optional[ManagerProfile], pk: Package) -> List[str]:
        r = []
        if t.mode in ("seller", "bubble"):
            vets = [a.label for a in pk.get if a.is_vet]
            r.append("%s is %s (%s)%s" % (
                t.team.display_name, "selling" if t.mode == "seller" else "on the bubble", t.mode_reason,
                "; %s %s the kind of veteran a rebuilding team moves" % (
                    ", ".join(vets), "is" if len(vets) == 1 else "are") if vets else ""))
        elif t.mode == "contender":
            r.append("%s is contending (%s); their lineup changes by %+.0f" % (
                t.team.display_name, t.mode_reason, pk.their_now))
        needs = [pos for pos, gap in sorted(t.needs.items(), key=lambda kv: -kv[1]) if gap > 0][:2]
        fills = [a.label for a in pk.give if a.position in needs]
        if fills:
            r.append("they are thin at %s; %s fills it" % ("/".join(needs), ", ".join(fills)))
        if m:
            if m.never_accepts:
                r.append("%s has proposed %d trades and never accepted one" % (m.name, m.proposed))
            elif m.trades:
                r.append("%s accepted %d and proposed %d trades since %s%s" % (
                    m.name, m.accepted, m.proposed, m.seasons[0] if m.seasons else "?",
                    "; " + m.tendency() if m.tendency() else ""))
            else:
                r.append("%s has no trade history" % m.name)
        return r
