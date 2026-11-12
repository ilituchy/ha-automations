"""One advisor run, callable from the CLI and from the Home Assistant page."""

from __future__ import annotations

import logging
import re
import os
import time
from dataclasses import dataclass
from typing import Optional

from . import cards, review
from .adddrop import recommend
from .assets import AssetBook
from .history import load_trades, walk_chain
from .http import CachedHttp
from .league import load_league
from .profiles import build_manager_profiles
import json

from .report import build_view, journal, render, summarize
from .sleeper import Sleeper
from .teams import build_team_profiles
from .trades import STRATEGIES, TradeSearch
from .values import load_values

log = logging.getLogger("advisor")


class RunError(Exception):
    """A run that cannot proceed for a reason the user can fix (bad username,
    wrong league id). Shown verbatim on the page."""


@dataclass
class RunResult:
    report_path: str
    journal_path: str
    markdown: str
    strategy: str
    calls: int
    seconds: float
    evaluated: int
    title: str = ""
    message: str = ""
    urgent: bool = False


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().replace(" jr", "").replace(" sr", "").replace(" iii", "").replace(" ii", ""))


def resolve_players(names, roster, book):
    """Owner-typed names (or Sleeper ids) -> ids on the owner's roster.
    Forgiving about case, punctuation and Jr./III suffixes."""
    by_name = {_norm(book.name(p)): p for p in roster}
    keep, unknown = set(), []
    for n in names or ():
        n = str(n).strip()
        if not n:
            continue
        pid = n if n in roster else by_name.get(_norm(n))
        if pid:
            keep.add(pid)
        else:
            unknown.append(n)
    return keep, unknown


def league_facts(league) -> dict:
    """Format facts the reviewer needs and the view does not carry."""
    sc = league.scoring
    rec = float(sc.get("rec") or 0)
    bits = ["%g PPR" % rec if rec else "non-PPR", "%g-point passing TDs" % float(sc.get("pass_td") or 4)]
    if sc.get("bonus_rec_te"):
        bits.append("TE premium +%g per catch" % float(sc["bonus_rec_te"]))
    return {"slots": list(league.starter_slots), "superflex": league.superflex, "scoring": ", ".join(bits)}


def run_once(
    username: str,
    league_id: Optional[str],
    strategy: str,
    data_dir: str,
    *,
    offline: bool = False,
    n_trades: int = 10,
    untouchable=(),
    api_key: str = "",
    ai_model: str = "",
) -> RunResult:
    if strategy != "auto" and strategy not in STRATEGIES:
        raise RunError("unknown strategy %r" % strategy)
    t0 = time.time()
    http = CachedHttp(os.path.join(data_dir, "cache"), offline=offline)
    sl = Sleeper(http)
    state = sl.state()
    user = sl.user(username)
    if not user:
        raise RunError("No Sleeper user named %r." % username)
    uid = user["user_id"]
    if not league_id:
        leagues = [l for l in sl.leagues(uid, state["league_season"]) if (l.get("settings") or {}).get("type") == 2]
        if len(leagues) != 1:
            raise RunError("Set league_id. Dynasty leagues found for %s: %s" % (
                username, ", ".join("%s (%s)" % (l["name"], l["league_id"]) for l in leagues) or "none"))
        league_id = leagues[0]["league_id"]

    # Name the week explicitly, never let the provider pick (porting doc §6.3).
    week = int(state.get("week") or 0)
    league = load_league(sl, league_id, week)
    me_team = league.team_of_user(uid)
    if not me_team:
        raise RunError("%s has no roster in league %s." % (username, league_id))
    me = me_team.roster_id

    players = sl.players()
    values = load_values(http, league, data_dir)

    def positions(pid):
        p = players.get(pid) or {}
        fp = p.get("fantasy_positions") or ([p["position"]] if p.get("position") else [])
        return tuple(fp) if fp else (("DEF",) if pid.isalpha() else ())

    ages = {pid: (values.players[pid].age or 0) for pid in values.players}
    teams = build_team_profiles(league, values, positions, ages)
    book = AssetBook(league, values, players, {rid: p.finish_rank for rid, p in teams.items()})

    seasons = walk_chain(sl, league_id)
    trades = load_trades(sl, seasons, week)
    names = {t.owner_id: t.display_name for t in league.teams.values() if t.owner_id}
    managers = build_manager_profiles(trades, seasons, book, names)

    if strategy == "auto":
        strategy = {"contender": "contend", "seller": "rebuild"}.get(teams[me].mode, "balanced")
        log.info("strategy auto -> %s (your team is %s)", strategy, teams[me].mode)
    keep, unknown = resolve_players(untouchable, league.teams[me].players, book)
    if keep:
        log.info("untouchable: %s", ", ".join(book.name(p) for p in sorted(keep)))
    search = TradeSearch(league, book, teams, managers, me, strategy, untouchable=keep)
    ideas = search.search(n_trades)
    ad = recommend(league, book, me, sl.trending("add"), strategy)

    md = render(league, me, strategy, teams, managers, ideas, ad, book, values.unchanged_days)
    os.makedirs(os.path.join(data_dir, "reports"), exist_ok=True)
    out = os.path.join(data_dir, "reports", time.strftime("%Y-%m-%d-%H%M%S") + ".md")
    with open(out + ".tmp", "w") as f:
        f.write(md)
    os.replace(out + ".tmp", out)
    view = build_view(league, me, strategy, teams, managers, ideas, ad, book, values.unchanged_days,
                      untouchable=keep, unknown_untouchable=unknown)
    ipath = cards.write_index(data_dir, league, players, values, book, me)
    if api_key:
        with open(ipath) as f:
            index = json.load(f)
        review.review_view(view, index, league_facts(league), api_key=api_key,
                           model=ai_model or review.DEFAULT_MODEL, http=http)
    vpath = out[:-3] + ".json"
    with open(vpath + ".tmp", "w") as f:
        json.dump(view, f, separators=(",", ":"))
    os.replace(vpath + ".tmp", vpath)
    jpath = journal(data_dir, league, strategy, ideas, ad, book)
    skip = review.passed(view)
    title, message, urgent = summarize(league, [i for n, i in enumerate(ideas, 1) if n not in skip], ad)
    return RunResult(out, jpath, md, strategy, http.calls, time.time() - t0, search.evaluated,
                     title, message, urgent)
