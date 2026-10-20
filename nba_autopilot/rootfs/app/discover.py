"""League discovery. Answers the questions the original spec left open.

Run this first, before enabling anything. It reads only, and it prints the
facts that determine how the optimizer must be configured:

* Is the GP cap enabled at all, and is it per-season or per-matchup-period?
  (The spec listed this as an open question. ``solve/budget.py`` behaves
  completely differently depending on the answer, so guessing is not an option.)
* Points league or categories? This selects between
  ``points_league_weights`` and the gradient weights.
* Which categories, and which direction each one scores.
* Lineup lock mode -- per-player at tipoff, or first-game-of-day.
* Waivers vs instant adds, and the FAAB budget.
* Do we actually own the team id we were given?

Invoke with ``python3 /app/main.py --discover``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from espn import ids
from espn.read import EspnRead
from models import LeagueSettings

log = logging.getLogger(__name__)


def report(read: EspnRead) -> Dict[str, Any]:
    s = read.league_settings()
    out: Dict[str, Any] = {}
    lines: List[str] = []

    def say(fmt: str, *args: Any) -> None:
        line = fmt % args if args else fmt
        lines.append(line)
        log.info("%s", line)

    say("league %d (%s), season %d", s.league_id, s.name, s.season_id)
    say("scoring type: %s", s.scoring_type)

    # -- ownership ---------------------------------------------------------
    mine = read.my_team_ids()
    owned = read.cfg.team_id in mine
    say(
        "team ownership: configured team_id=%d, SWID owns %s -> %s",
        read.cfg.team_id,
        sorted(mine) or "nothing",
        "OK" if owned else "MISMATCH (writes will be refused)",
    )
    out["owns_configured_team"] = owned
    out["owned_team_ids"] = sorted(mine)

    # -- scoring -----------------------------------------------------------
    if s.is_categories:
        cats = sorted(s.categories)
        say("CATEGORIES league, %d categories:", len(cats))
        for c in cats:
            direction = "lower wins" if c in ids.LOWER_IS_BETTER else "higher wins"
            ratio = " (ratio of %s/%s)" % tuple(
                ids.stat_name(x) for x in ids.RATIO_COMPONENTS[c]
            ) if c in ids.RATIO_COMPONENTS else ""
            say("    %-5s id=%-3d %s%s", ids.stat_name(c), c, direction, ratio)
        say("-> gradient category weights active (solve/weights.py)")
        if set(cats) == set(ids.NINE_CAT):
            say("   this is the standard 9-cat set")
    else:
        say("POINTS league. scoring coefficients:")
        for stat, pts in sorted(s.scoring_items.items()):
            if pts:
                say("    %-5s id=%-3d %+.2f", ids.stat_name(stat), stat, pts)
        say("-> fixed weights from scoringItems (solve/weights.points_league_weights)")
    out["is_categories"] = s.is_categories
    out["categories"] = sorted(s.categories)

    # -- roster shape ------------------------------------------------------
    say("lineup slots:")
    starting = 0
    for slot, n in sorted(s.lineup_slot_counts.items()):
        kind = "bench/IR" if slot in ids.NON_SCORING_SLOTS else "starting"
        if slot not in ids.NON_SCORING_SLOTS:
            starting += n
        say("    %-6s id=%-3d x%d  (%s)", ids.slot_name(slot), slot, n, kind)
    say("    -> %d starting spots, %d total roster spots",
        starting, sum(s.lineup_slot_counts.values()))
    out["lineup_slot_counts"] = dict(s.lineup_slot_counts)
    out["starting_spots"] = starting

    # -- the GP cap, the spec's biggest open question ----------------------
    if not s.has_gp_cap:
        say("GP CAP: not configured. The budget layer is inert; every day is")
        say("        solved independently and nothing is conserved.")
    else:
        say("GP CAP: ENABLED (lineupSlotStatLimits)")
        for slot, cap in sorted((s.lineup_slot_stat_limits or {}).items()):
            say("    %-6s id=%-3d limit=%d", ids.slot_name(slot), slot, cap)
        used = read.games_used()
        season_gp = read.season_games_played()
        say("    games used THIS MATCHUP PERIOD (cumulativeScore.statBySlot):")
        if used:
            for slot, n in sorted(used.items()):
                say("        %-6s %d", ids.slot_name(slot), n)
        else:
            say("        (none reported yet)")
        say("    games used THIS SEASON (teams[].valuesByStat[42]): %s",
            "n/a" if season_gp is None else season_gp)
        # The discriminator: if the per-matchup numbers can plausibly reach the
        # limit inside one week, the cap is weekly. If only a season total can,
        # it is seasonal. State both and let the human confirm -- ESPN does not
        # label which semantics are in force.
        say("    -> compare these two against the limits above to settle")
        say("       per-season vs per-matchup. They disagree only if the cap is")
        say("       weekly, in which case the matchup figure is the one to use.")
        out["gp_limits"] = dict(s.lineup_slot_stat_limits or {})
        out["games_used_matchup"] = used
        out["games_used_season"] = season_gp
    out["has_gp_cap"] = s.has_gp_cap

    # -- locks and acquisitions -------------------------------------------
    say("lineup lock mode: %s", s.roster_lock_mode)
    say("    (determines whether a player locks at his own tipoff or whether")
    say("     the whole lineup locks at the day's first game -- this sets how")
    say("     late the daily run can safely be scheduled)")
    say("waiver/acquisition mode: %s", s.waiver_mode)
    say("FAAB budget: %s", "none (waiver priority)" if s.faab_budget is None
        else s.faab_budget)
    out["roster_lock_mode"] = s.roster_lock_mode
    out["waiver_mode"] = s.waiver_mode
    out["faab_budget"] = s.faab_budget

    say("current scoring period: %d, matchup period: %d",
        s.current_scoring_period, s.current_matchup_period)
    out["current_scoring_period"] = s.current_scoring_period
    out["current_matchup_period"] = s.current_matchup_period

    # -- roster ------------------------------------------------------------
    roster = read.roster()
    say("roster (%d players):", len(roster))
    for p in sorted(roster, key=lambda r: (r.current_slot, r.name)):
        flags = []
        if p.lineup_locked:
            flags.append("LOCKED")
        if p.ir_eligible:
            flags.append("IR-ok")
        if p.injury_status != "ACTIVE":
            flags.append(p.injury_status)
        say(
            "    %-6s %-24s %-4s %s",
            ids.slot_name(p.current_slot),
            p.name,
            ids.pro_team(p.pro_team_id),
            " ".join(flags),
        )
    out["roster_size"] = len(roster)
    out["ir_eligible_now"] = [p.name for p in roster if p.ir_eligible]

    pending = read.pending_transactions()
    if pending:
        say("PENDING transactions: %d (cancellable)", len(pending))
        for t in pending:
            say("    id=%s type=%s status=%s", t.get("id"), t.get("type"),
                t.get("status"))
    out["pending_transactions"] = len(pending)

    out["_report_lines"] = lines
    return out
