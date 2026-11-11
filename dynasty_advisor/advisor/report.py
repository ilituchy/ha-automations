"""Render a run as Markdown, and journal its inputs and outputs as JSON.

The journal answers "why did it tell me that last Tuesday" (porting doc §4):
every idea is stored with the values it was computed from.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .adddrop import AddDropReport, Candidate
from .assets import Asset, AssetBook
from .league import League
from .profiles import ManagerProfile
from .teams import TeamProfile
from .trades import Package, TradeIdea
from .values import ATTRIBUTION


def _assets(xs) -> str:
    return " + ".join(
        "**%s**" % a.label
        + (" (%s, %s, %s)" % (a.position, _age(a), _v(a.dyn)) if a.kind == "player" else " (%s)" % _v(a.dyn))
        for a in xs)


def _v(x: float) -> str:
    return "{:,.0f}".format(x)


def _age(a: Asset) -> str:
    return "%.0f" % a.age if a.age else "?"


def _pkg_line(pk: Package) -> str:
    s = "Give %s for %s." % (_assets(pk.give), _assets(pk.get))
    s += " Neutral market: you %s %.0f." % ("gain" if pk.market >= 0 else "pay", abs(pk.market))
    s += " Your lineup rest-of-season: %+.0f." % pk.my_now
    if pk.roster_change > 0:
        s += " You'd need to drop %d." % pk.roster_change
    return s


def render(
    league: League,
    me: int,
    strategy: str,
    teams: Dict[int, TeamProfile],
    managers: Dict[str, ManagerProfile],
    ideas: List[TradeIdea],
    ad: AddDropReport,
    book: AssetBook,
    values_unchanged_days: int,
) -> str:
    my = league.teams[me]
    L: List[str] = []
    L.append("# %s — dynasty advisor" % league.name)
    L.append("")
    L.append("%s · week %d · %s (%s) · strategy **%s**" % (
        datetime.now().strftime("%a %b %d %Y %H:%M"), league.week, my.display_name, my.record, strategy))
    L.append("")
    dl = league.trade_deadline
    if dl:
        if league.week > dl:
            L.append("> **Trades are closed** (deadline week %d). Ideas below are for the offseason." % dl)
        elif league.week >= dl - 1:
            L.append("> **Trade deadline: week %d.** Current week is %d — act now or wait for the offseason." % (dl, league.week))
        L.append("")
    if league.pick_check:
        L.append("> ⚠️ Pick ownership self-check FAILED against the completed draft (%d mismatches). Pick lists below may be wrong." % len(league.pick_check))
        L.append("")
    if values_unchanged_days >= 3:
        L.append("> ⚠️ FantasyCalc values unchanged for %d days — the feed may be stale." % values_unchanged_days)
        L.append("")

    # ---- trades ---------------------------------------------------------
    L.append("## Trade targets")
    L.append("")
    if not ideas:
        L.append("No trade found that helps you and that the other manager plausibly accepts.")
    for i, idea in enumerate(ideas, 1):
        t = idea.partner
        L.append("### %d. %s — %s · %s" % (i, t.team.display_name, t.mode, idea.chance))
        L.append("")
        L.append("- **Open with:** " + _pkg_line(idea.opening))
        if idea.ceiling is not idea.opening and {a.key for a in idea.ceiling.give} != {a.key for a in idea.opening.give}:
            L.append("- **Walk-away ceiling:** " + _pkg_line(idea.ceiling))
        else:
            L.append("- **Walk-away ceiling:** don't add to the opening offer.")
        for r in idea.reasons:
            L.append("- " + r)
        L.append("")

    # ---- add/drop -------------------------------------------------------
    L.append("## Add / drop")
    L.append("")
    L.append("Active roster %d/%d · IR spots open %d · open spots after IR moves %d%s" % (
        ad.active_count, ad.active_max, max(0, ad.reserve_open), ad.open_spots,
        " · FAAB left $%d" % ad.faab_left if ad.faab_left is not None else ""))
    L.append("")
    if ad.ir_moves and ad.reserve_open > 0:
        L.append("- **IR first:** %d open IR spot%s, %d eligible player%s on your active roster: %s. Each move frees an active spot at no cost." % (
            ad.reserve_open, "" if ad.reserve_open == 1 else "s", len(ad.ir_moves), "" if len(ad.ir_moves) == 1 else "s",
            ", ".join("%s (%s)" % (a.label, (book.index.get(a.key) or {}).get("injury_status")) for a in ad.ir_moves)))
    if ad.swaps:
        for add, drop, gain in ad.swaps:
            if drop is None:
                L.append("- **Add %s** into an open spot (%s, %s, dynasty %s). %s" % (
                    add.asset.label, add.nfl_team, add.asset.position, _v(add.asset.dyn), _cand_note(add)))
            else:
                L.append("- **Add %s, drop %s** (dynasty %s for %s). %s" % (
                    add.asset.label, drop.asset.label, _v(add.asset.dyn), _v(drop.asset.dyn), _cand_note(add)))
    elif not ad.ir_moves:
        L.append("- No add clears the bar over your weakest active player.")
    L.append("")
    L.append("| Top free agents | Pos | Age | Dynasty | Rest-of-season | Starts for you | Trending adds |")
    L.append("|---|---|---|---|---|---|---|")
    for c in ad.adds:
        L.append("| %s (%s) | %s | %s | %.0f | %.0f | %s | %s |" % (
            c.asset.label, c.nfl_team or "FA", c.asset.position, _age(c.asset), c.asset.dyn, c.asset.now,
            "yes (%+.0f)" % c.now_delta if c.now_delta > 0 else "no", c.trending or ""))
    L.append("")
    if ad.trending_unvalued:
        L.append("Trending on Sleeper with no FantasyCalc value yet (worth a look, not scored): " + ", ".join(
            "%s (%s %s, depth #%s)" % (c.asset.label, c.nfl_team, c.asset.position, c.depth) for c in ad.trending_unvalued))
        L.append("")
    L.append("Cheapest to lose: " + ", ".join("%s (%s%s)" % (
        c.asset.label, _v(c.asset.dyn), ", starting now" if c.asset.key in ad.starters else "") for c in ad.drops[:5]))
    L.append("")

    # ---- landscape ------------------------------------------------------
    L.append("## League landscape")
    L.append("")
    L.append("| Proj. | Team | Record | PF | Lineup value | Mode | Thinnest at |")
    L.append("|---|---|---|---|---|---|---|")
    for p in sorted(teams.values(), key=lambda p: p.finish_rank):
        need = max(p.needs.items(), key=lambda kv: kv[1])
        L.append("| %d | %s%s | %s | %.0f | %.0f | %s | %s |" % (
            p.finish_rank, p.team.display_name, " (you)" if p.team.roster_id == me else "", p.team.record,
            p.team.fpts, p.now_value, p.mode, need[0] if need[1] > 0 else "—"))
    L.append("")

    L.append("## Managers")
    L.append("")
    L.append("Only completed trades are visible — rejected offers never show up — so these are counts, not acceptance rates.")
    L.append("")
    L.append("| Manager | Accepted | Proposed | Softness | Opening edge | Tendency |")
    L.append("|---|---|---|---|---|---|")
    cur = {t.owner_id for t in league.teams.values()}
    for m in sorted((m for m in managers.values() if m.user_id in cur), key=lambda m: -m.softness):
        L.append("| %s | %d | %d | %.2f | %s | %s |" % (
            m.name, m.accepted, m.proposed, m.softness,
            "require a clear win for them" if m.never_accepts else "%.0f%%" % (100 * m.edge), m.tendency() or ""))
    L.append("")

    L.append("## Your picks")
    L.append("")
    names = {rid: t.team.display_name for rid, t in teams.items()}
    for p in league.picks_of(me):
        a = book.pick(p, names)
        L.append("- %s — %.0f" % (a.label, a.dyn))
    L.append("")
    L.append("---")
    L.append("Values are FantasyCalc dynasty values. \"Lineup rest-of-season\" is the change in your best starting lineup's FantasyCalc rest-of-season value (same units). Trades are compared over replacement (R = %.0f, "
             "the median of the best %d free agents). Past trades are judged at today's values (hindsight). "
             "%s" % (book.replacement, league.total_rosters, ATTRIBUTION))
    return "\n".join(L) + "\n"


def _cand_note(c: Candidate) -> str:
    bits = []
    if c.depth:
        bits.append("depth chart #%s" % c.depth)
    if c.injury:
        bits.append(c.injury)
    if c.trending:
        bits.append("%d adds in 48h across Sleeper" % c.trending)
    return "; ".join(bits)


def journal(data_dir: str, league: League, strategy: str, ideas: List[TradeIdea], ad: AddDropReport, book: AssetBook) -> str:
    d = os.path.join(data_dir, "runs")
    os.makedirs(d, exist_ok=True)

    def pk(p: Package) -> dict:
        return {
            "give": [[a.key, a.label, a.dyn, a.now, a.vor] for a in p.give],
            "get": [[a.key, a.label, a.dyn, a.now, a.vor] for a in p.get],
            "mine": round(p.mine, 1), "my_now": round(p.my_now, 1), "my_dyn": round(p.my_dyn, 1),
            "theirs": round(p.theirs, 1), "market": round(p.market, 1),
        }

    body = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "season": league.season,
        "week": league.week,
        "strategy": strategy,
        "replacement": book.replacement,
        "values_fetched_at": book.values.fetched_at,
        "pick_check_mismatches": league.pick_check,
        "trades": [{"partner": i.partner.team.display_name, "mode": i.partner.mode, "chance": i.chance,
                    "opening": pk(i.opening), "ceiling": pk(i.ceiling), "reasons": i.reasons} for i in ideas],
        "ir_moves": [a.label for a in ad.ir_moves],
        "moves": [[a.asset.label, d_.asset.label if d_ else None, round(g, 1)] for a, d_, g in ad.swaps],
        "adds": [[c.asset.key, c.asset.label, round(c.score, 1)] for c in ad.adds],
    }
    path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + ".json")
    with open(path, "w") as f:
        json.dump(body, f, indent=1)
    return path


def _short(a: Asset) -> str:
    return a.label.split(" via ")[0]


def summarize(league: League, ideas: List[TradeIdea], ad: AddDropReport) -> Tuple[str, str, bool]:
    """(title, message, urgent) for the phone notification. Short: it is
    read on a lock screen, and the page has the full reasoning."""
    dl = league.trade_deadline
    trades_open = not dl or league.week <= dl
    deadline_week = bool(dl) and league.week >= dl - 1 and trades_open
    lines: List[str] = []
    if trades_open:
        for i in ideas[:3]:
            lines.append("%s (%s): give %s for %s" % (
                i.partner.team.display_name, i.partner.mode,
                " + ".join(_short(a) for a in i.opening.give),
                " + ".join(_short(a) for a in i.opening.get)))
    moves = []
    if ad.ir_moves and ad.reserve_open > 0:
        moves.append("IR: " + ", ".join(a.label for a in ad.ir_moves[: ad.reserve_open]))
    adds = [a.asset.label for a, d, _g in ad.swaps]
    if adds:
        moves.append("Add: " + ", ".join(adds[:3]))
    if moves:
        lines.append(" · ".join(moves))
    n = len(ideas) if trades_open else 0
    if deadline_week:
        title = "Trade deadline week %d — %d trade idea%s" % (dl, n, "" if n == 1 else "s")
    elif n:
        title = "%d trade idea%s%s" % (n, "" if n == 1 else "s", ", plus roster moves" if moves else "")
    elif moves:
        title = "Roster moves this week"
    else:
        title = "Nothing worth doing this week"
    return "Dynasty Advisor: " + title, "\n".join(lines) or "Open the report for details.", deadline_week


# ---------------------------------------------------------------------------
# Structured view of a run, for the Home Assistant page. Same facts as the
# Markdown, as data, so the page can lay them out properly.
# ---------------------------------------------------------------------------


def _asset_view(a: Asset) -> dict:
    via = None
    label = a.label
    if " via " in label:
        label, via = label.split(" via ", 1)
    return {"name": label, "kind": a.kind, "pos": a.position, "age": round(a.age) if a.age else None,
            "value": round(a.dyn), "via": via}


def _pkg_view(pk: Package) -> dict:
    return {"give": [_asset_view(a) for a in pk.give], "get": [_asset_view(a) for a in pk.get],
            "market": round(pk.market), "lineup": round(pk.my_now), "roster_change": pk.roster_change}


def _notes(idea: TradeIdea) -> List[str]:
    t, m, pk = idea.partner, idea.manager, idea.opening
    notes = []
    vets = [a.label for a in pk.get if a.is_vet]
    if t.mode in ("seller", "bubble") and vets:
        notes.append("%s %s the kind of veteran a rebuilding team moves" % (
            " and ".join(vets), "is" if len(vets) == 1 else "are"))
    if t.mode == "contender":
        if pk.their_now >= 0:
            notes.append("Strengthens their lineup by {:,.0f} — the kind of upgrade a contender buys".format(pk.their_now))
        else:
            notes.append("Weakens their lineup by {:,.0f} now, for long-term value — a contender may balk".format(-pk.their_now))
    needs = [pos for pos, gap in sorted(t.needs.items(), key=lambda kv: -kv[1]) if gap > 0][:2]
    fills = [a.label for a in pk.give if a.position in needs]
    if fills:
        notes.append("They're thin at %s — %s fills it" % ("/".join(needs), " and ".join(fills)))
    if m and m.never_accepts:
        notes.append("%s has proposed %d trades and never accepted one" % (m.name, m.proposed))
    elif m and m.tendency():
        notes.append("Tendency: %s" % m.tendency())
    return notes


def build_view(league: League, me: int, strategy: str, teams: Dict[int, TeamProfile],
               managers: Dict[str, ManagerProfile], ideas: List[TradeIdea], ad: AddDropReport,
               book: AssetBook, values_unchanged_days: int) -> dict:
    my = league.teams[me]
    mine = teams[me]
    dl = league.trade_deadline
    if not dl:
        deadline = None
    elif league.week > dl:
        deadline = {"week": dl, "state": "closed"}
    else:
        deadline = {"week": dl, "state": "this week" if league.week >= dl - 1 else "week %d" % dl,
                    "weeks_left": dl - league.week}
    warnings = []
    if league.pick_check:
        warnings.append("Pick ownership self-check failed against the completed draft (%d mismatches); pick lists may be wrong." % len(league.pick_check))
    if values_unchanged_days >= 3:
        warnings.append("FantasyCalc values unchanged for %d days — the feed may be stale." % values_unchanged_days)
    names = {rid: t.team.display_name for rid, t in teams.items()}
    cur = {t.owner_id for t in league.teams.values()}
    max_now = max((p.now_value for p in teams.values()), default=1) or 1
    budget = league.settings.get("waiver_budget")
    return {
        "version": 1,
        "league": league.name,
        "season": league.season,
        "week": league.week,
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "strategy": strategy,
        "me": {"name": my.display_name, "record": my.record, "finish": mine.finish_rank, "mode": mine.mode,
               "teams": len(teams), "faab_left": ad.faab_left, "faab_budget": int(budget) if budget else None},
        "deadline": deadline,
        "warnings": warnings,
        "trades": [{
            "partner": i.partner.team.display_name,
            "mode": i.partner.mode,
            "context": i.partner.mode_reason,
            "chance": i.chance,
            "accepted": i.manager.accepted if i.manager else None,
            "proposed": i.manager.proposed if i.manager else None,
            "since": i.manager.seasons[0] if i.manager and i.manager.seasons else None,
            "opening": _pkg_view(i.opening),
            "ceiling": None if {a.key for a in i.ceiling.give} == {a.key for a in i.opening.give}
            and {a.key for a in i.ceiling.get} == {a.key for a in i.opening.get} else _pkg_view(i.ceiling),
            "notes": _notes(i),
        } for i in ideas],
        "moves": {
            "active": ad.active_count, "active_max": ad.active_max,
            "reserve_open": max(0, ad.reserve_open), "open_spots": ad.open_spots,
            "ir": [{"name": a.label, "pos": a.position, "status": (book.index.get(a.key) or {}).get("injury_status")}
                   for a in ad.ir_moves],
            "actions": [{"add": dict(_asset_view(a.asset), team=a.nfl_team, depth=a.depth, injury=a.injury,
                                     starts=a.now_delta > 0),
                         "drop": _asset_view(d.asset) if d else None} for a, d, _g in ad.swaps],
            "free_agents": [dict(_asset_view(c.asset), team=c.nfl_team, now=round(c.asset.now),
                                 starts=round(c.now_delta) if c.now_delta > 0 else 0, trending=c.trending,
                                 depth=c.depth, injury=c.injury) for c in ad.adds],
            "trending_unvalued": [dict(_asset_view(c.asset), team=c.nfl_team, depth=c.depth) for c in ad.trending_unvalued],
            "cheapest": [dict(_asset_view(c.asset), starter=c.asset.key in ad.starters) for c in ad.drops[:5]],
        },
        "teams": [{
            "rank": p.finish_rank, "name": p.team.display_name, "me": p.team.roster_id == me,
            "record": p.team.record, "pf": round(p.team.fpts), "lineup": round(p.now_value),
            "lineup_share": round(p.now_value / max_now, 3), "mode": p.mode,
            "thin": max(p.needs.items(), key=lambda kv: kv[1])[0] if max(p.needs.values()) > 0 else None,
        } for p in sorted(teams.values(), key=lambda p: p.finish_rank)],
        "managers": [{
            "name": m.name, "accepted": m.accepted, "proposed": m.proposed, "softness": m.softness,
            "edge": None if m.never_accepts else round(100 * m.edge), "tendency": m.tendency(),
            "me": m.user_id == my.owner_id,
        } for m in sorted((m for m in managers.values() if m.user_id in cur), key=lambda m: -m.softness)],
        "picks": [dict(_asset_view(book.pick(p, names)), season=p.season, round=p.round)
                  for p in league.picks_of(me)],
        "replacement": round(book.replacement),
        "attribution": ATTRIBUTION,
    }
