"""Team profiles: who is contending, who is selling, and what each team lacks.

The owner's instruction (2026-11-11): "make sure to target losing teams too
because they will sell for a rebuild". So a team's *mode* is a first-class
input to trade generation, not a label in the report:

* **seller** -- losing record, or a weak team on both record and lineup
  strength. Sells veterans for picks and young players, and values its own
  veterans less than the market does.
* **contender** -- winning and strong. Buys win-now help; values picks less.
* **bubble** -- losing record but within reach of the last playoff seed.
  Leans toward selling, less hard than a seller.
* **middle** -- neither, priced at market.

Seller vs bubble is games back from the last playoff seed against games left:
in a 10-team, 6-seed league at week 10, a 4-5 team one game out is a bubble
team, not a seller.

Mode uses only facts available mid-season: record, points scored, and the
rest-of-season value of the team's best lineup. It deliberately ignores the
team's own pick stash, which would make a team that already sold look like a
contender.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Dict, List, Mapping

from .league import League, Team
from .lineup import best_lineup

POSITIONS = ("QB", "RB", "WR", "TE")


@dataclass
class TeamProfile:
    team: Team
    now_value: float  # rest-of-season value of best lineup
    lineup: Dict[str, str]
    pos_strength: Dict[str, float]
    needs: Dict[str, float] = field(default_factory=dict)  # >0 = below league median
    contend_index: float = 0.5  # 0 = worst, 1 = best
    finish_rank: int = 0  # 1 = best projected finish
    mode: str = "middle"
    mode_reason: str = ""
    avg_starter_age: float = 0.0
    games_back: float = 0.0


def _pct_ranks(values: Mapping[int, float]) -> Dict[int, float]:
    """0 for the lowest, 1 for the highest, ties averaged."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out: Dict[int, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        r = ((i + j) / 2) / (n - 1) if n > 1 else 0.5
        for k in range(i, j + 1):
            out[items[k][0]] = r
        i = j + 1
    return out


def build_team_profiles(league: League, values, positions_of, ages: Mapping[str, float]) -> Dict[int, TeamProfile]:
    slots = league.starter_slots
    profiles: Dict[int, TeamProfile] = {}
    for rid, t in league.teams.items():
        total, lineup = best_lineup(t.active, positions_of, values.now, slots)
        strength = {p: 0.0 for p in POSITIONS}
        starter_ages = []
        for pid in lineup:
            pos = (positions_of(pid) or ("",))[0]
            if pos in strength:
                strength[pos] += values.now(pid)
            if ages.get(pid):
                starter_ages.append(ages[pid])
        profiles[rid] = TeamProfile(
            team=t,
            now_value=total,
            lineup=lineup,
            pos_strength=strength,
            avg_starter_age=sum(starter_ages) / len(starter_ages) if starter_ages else 0.0,
        )

    for pos in POSITIONS:
        med = median(p.pos_strength[pos] for p in profiles.values())
        for p in profiles.values():
            p.needs[pos] = med - p.pos_strength[pos]

    wr = _pct_ranks({rid: p.team.win_pct for rid, p in profiles.items()})
    pf = _pct_ranks({rid: p.team.fpts for rid, p in profiles.items()})
    nv = _pct_ranks({rid: p.now_value for rid, p in profiles.items()})
    games = max((p.team.games for p in profiles.values()), default=0)
    # Early in a season the record is noise; lean on roster strength instead.
    w_rec = 0.15 + 0.35 * min(1.0, games / 10.0)
    w_pf = 0.2
    w_now = 1.0 - w_rec - w_pf
    for rid, p in profiles.items():
        p.contend_index = w_rec * wr[rid] + w_pf * pf[rid] + w_now * nv[rid]

    order = sorted(profiles, key=lambda r: -profiles[r].contend_index)
    n = len(order)
    playoff_teams = int(league.settings.get("playoff_teams") or max(2, n // 2))
    # The playoff line: the record of the last team currently in.
    seeds = sorted(profiles.values(), key=lambda p: (-p.team.win_pct, -p.team.fpts))
    line = seeds[min(playoff_teams, n) - 1].team
    reg_weeks = int(league.settings.get("playoff_week_start") or 15) - 1
    for i, rid in enumerate(order):
        p = profiles[rid]
        p.finish_rank = i + 1
        t = p.team
        p.games_back = max(0.0, (line.wins - t.wins + t.losses - line.losses) / 2.0)
        remaining = max(0, reg_weeks - t.games)
        in_now = p in seeds[:playoff_teams]
        pf_rank = _ordinal(1 + sum(1 for q in profiles.values() if q.team.fpts > t.fpts))
        if games >= 4 and t.win_pct < 0.5 and (p.games_back >= 2 or p.games_back > remaining / 2.0):
            p.mode = "seller"
            p.mode_reason = "%s, %s out of a playoff spot with %d to play, %s in points" % (
                t.record, _games(p.games_back), remaining, pf_rank)
        elif games >= 4 and t.win_pct < 0.5:
            p.mode = "bubble"
            p.mode_reason = "%s, %s out with %d to play, %s in points — may sell" % (
                t.record, _games(p.games_back), remaining, pf_rank)
        elif in_now and i < playoff_teams:
            p.mode = "contender"
            p.mode_reason = "%s, projected finish %s" % (t.record, _ordinal(i + 1))
        else:
            p.mode = "middle"
            p.mode_reason = "%s, projected finish %s" % (t.record, _ordinal(i + 1))
    return profiles


def _games(g: float) -> str:
    return ("%g game" % g) + ("" if g == 1 else "s")


def _ordinal(n: int) -> str:
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))
