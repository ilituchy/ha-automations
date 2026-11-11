"""Manager trade profiles: who accepts, who proposes, who overpays.

The owner's instruction (2026-11-11): "analyze each users previous trade
history, so we understand who is likely to accept and if so, we can get a
better deal. might be more aggressive and unfair with people who accept more
trades."

What the data can and cannot say
--------------------------------
* **Accepts are observable; rejections are not.** The public API returns only
  completed trades, so "softness" is built from how often a manager *accepted*
  someone else's proposal, per season they were in the league -- never from a
  rate.
* **Hindsight.** FantasyCalc has no history endpoint, so "did they overpay" is
  judged at *today's* values. A fair 2024 trade can look lopsided now because
  a player broke out. It is reported as a hindsight number and only nudges
  softness; it never drives it. From 2026-11-11 a daily value snapshot is kept
  so future trades can be judged at the values of their own day.
* **Multi-team trades** are counted for activity but excluded from the value
  balance, because adds alone do not say who sent what.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

from .history import SeasonInfo, Trade

#: How much of a package's value we may tilt in our favour in an opening
#: offer, at softness 0 and softness 1.
MIN_EDGE = 0.04
MAX_EDGE = 0.18


@dataclass
class ManagerProfile:
    user_id: str
    name: str
    seasons: List[str] = field(default_factory=list)
    proposed: int = 0
    accepted: int = 0
    by_season: Counter = field(default_factory=Counter)
    partners: Counter = field(default_factory=Counter)
    last_trade_ms: int = 0
    picks_in: int = 0
    picks_out: int = 0
    young_in: int = 0
    vets_in: int = 0
    hindsight_net: float = 0.0  # value received - given, at today's values
    hindsight_n: int = 0
    softness: float = 0.0  # 0..1

    @property
    def trades(self) -> int:
        return self.proposed + self.accepted

    @property
    def edge(self) -> float:
        """Fraction of a package's value an opening offer may keep for us."""
        return MIN_EDGE + (MAX_EDGE - MIN_EDGE) * self.softness

    @property
    def never_accepts(self) -> bool:
        return self.accepted == 0 and len(self.seasons) >= 2

    def tendency(self) -> str:
        bits = []
        net = self.picks_in - self.picks_out
        if net >= 3:
            bits.append("collects picks")
        elif net <= -3:
            bits.append("spends picks")
        if self.young_in > self.vets_in + 2:
            bits.append("buys youth")
        elif self.vets_in > self.young_in + 2:
            bits.append("buys veterans")
        if self.hindsight_n >= 3:
            per = self.hindsight_net / self.hindsight_n
            if per < -400:
                bits.append("has given up value in past trades (hindsight)")
            elif per > 400:
                bits.append("has won past trades (hindsight)")
        return ", ".join(bits)


def build_manager_profiles(
    trades: Sequence[Trade],
    seasons: Sequence[SeasonInfo],
    book,
    current_names: Mapping[str, str],
) -> Dict[str, ManagerProfile]:
    profiles: Dict[str, ManagerProfile] = {}

    def get(uid: str) -> ManagerProfile:
        if uid not in profiles:
            name = current_names.get(uid)
            if not name:
                for s in reversed(seasons):
                    if uid in s.names:
                        name = s.names[uid] + " (left league)"
                        break
            profiles[uid] = ManagerProfile(user_id=uid, name=name or uid)
        return profiles[uid]

    for s in seasons:
        for uid in s.roster_owner.values():
            if uid:
                get(uid).seasons.append(s.season)

    for t in trades:
        two_party = len(t.sides) == 2
        for side in t.sides:
            m = get(side.user_id)
            if side.user_id == t.creator:
                m.proposed += 1
            else:
                m.accepted += 1
            m.by_season[t.season] += 1
            m.last_trade_ms = max(m.last_trade_ms, t.ts_ms)
            for other in t.parties:
                if other != side.user_id:
                    m.partners[other] += 1
            m.picks_in += len(side.picks_in)
            for pid in side.players_in:
                a = book.player(pid)
                if a.is_young:
                    m.young_in += 1
                elif a.is_vet:
                    m.vets_in += 1
            if two_party:
                given_players, given_picks = t.given_by(side.user_id)
                m.picks_out += len(given_picks)
                got = sum(book.player(p).dyn for p in side.players_in) + sum(
                    book.values.pick(pk.season, pk.round) for pk in side.picks_in)
                gave = sum(book.player(p).dyn for p in given_players) + sum(
                    book.values.pick(pk.season, pk.round) for pk in given_picks)
                m.hindsight_net += got - gave
                m.hindsight_n += 1

    _score_softness(profiles, seasons)
    return profiles


def _score_softness(profiles: Dict[str, ManagerProfile], seasons: Sequence[SeasonInfo]) -> None:
    """Accepts per season in the league, recent seasons weighted double,
    normalised so the league's most accepting manager is 1.0."""
    recent = {s.season for s in seasons[-2:]}
    raw: Dict[str, float] = {}
    for uid, m in profiles.items():
        n = len(m.seasons) or 1
        weighted = 0.0
        for s in m.seasons:
            w = 2.0 if s in recent else 1.0
            weighted += w
        # accepts can't be split by season from the counters above without the
        # trades, so approximate: share of trades that were accepts, times
        # recent-weighted activity per season.
        share = m.accepted / m.trades if m.trades else 0.0
        activity = sum(m.by_season[s] * (2.0 if s in recent else 1.0) for s in m.seasons) / weighted
        raw[uid] = share * activity
    top = max(raw.values(), default=0.0) or 1.0
    for uid, m in profiles.items():
        soft = raw[uid] / top
        if m.hindsight_n >= 3 and m.hindsight_net / m.hindsight_n < -400:
            soft = min(1.0, soft + 0.1)
        m.softness = 0.0 if m.never_accepts else round(soft, 3)


def likelihood(their_gain: float, size: float, m: Optional[ManagerProfile]) -> str:
    """Rough read on whether this manager says yes."""
    if m is None:
        return "unknown"
    if m.never_accepts:
        return "long shot (has never accepted a trade)"
    rel = their_gain / size if size else 0.0
    if rel >= 0.02 or (rel >= -0.02 and m.softness >= 0.5):
        return "likely"
    if rel >= -m.edge:
        return "plausible"
    return "long shot"
