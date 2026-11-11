"""Synthetic league builders. No real league data lives in this public repo."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from advisor.assets import AssetBook  # noqa: E402
from advisor.league import League, Team, pick_ownership  # noqa: E402
from advisor.values import PlayerValue, Values  # noqa: E402

SLOTS = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "BN", "BN", "BN", "BN")


def player(pid, pos, age, dyn, now):
    return PlayerValue(pid, "P%s" % pid, pos, age, dyn, now, 0)


def make_league(rosters, records, positions=SLOTS, picks_traded=(), seasons=("2027", "2028")):
    """rosters: {roster_id: [pid, ...]}, records: {roster_id: (wins, losses, fpts)}"""
    teams = {}
    for rid, ps in rosters.items():
        w, l, pf = records[rid]
        teams[rid] = Team(rid, "u%d" % rid, "team%d" % rid, None, tuple(ps), tuple(), tuple(), tuple(),
                          wins=w, losses=l, fpts=pf)
    picks = pick_ownership(picks_traded, sorted(teams), seasons, 2)
    return League("L", "Test", "2026", 10, tuple(positions),
                  {"draft_rounds": 2, "playoff_teams": 2, "playoff_week_start": 15, "trade_deadline": 11},
                  {"rec": 1.0}, teams, picks, tuple(seasons), total_rosters=len(teams))


def make_values(players, picks=None):
    return Values({p.sleeper_id: p for p in players},
                  picks or {("2027", 1, None): 3000.0, ("2027", 2, None): 1500.0,
                            ("2028", 1, None): 2500.0, ("2028", 2, None): 1300.0})


def index_for(players):
    return {p.sleeper_id: {"fantasy_positions": [p.position], "position": p.position, "full_name": p.name,
                           "active": True, "team": "NFL", "age": p.age} for p in players}


def make_book(league, values, index, finish=None):
    return AssetBook(league, values, index, finish or {rid: rid for rid in league.teams})
