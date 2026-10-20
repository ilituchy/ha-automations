#!/usr/bin/env python3
"""Read-only reconnaissance against a live ESPN fantasy league.

This exists because every assumption the optimizer makes about league shape --
scoring mode, slot counts, the games-played cap, whether FAAB exists -- is a
guess until a real league confirms it, and three of those guesses were already
wrong. Run it against a league before trusting the app with that league.

It cannot write: it builds an ``EspnRead``, which only knows the read host and
only issues GET.

Cookies come from ``~/.espn_cookies`` (line 1 ``espn_s2``, line 2 ``SWID``),
never from argv -- argv is visible in ``ps`` to every user on the box.

    chmod 600 ~/.espn_cookies
    tools/espn_probe.py 111111111:2 222222222:7
    tools/espn_probe.py --season 2026 --dump /tmp/espn-disc 111111111:2

``--dump DIR`` writes the raw ``mSettings``+``mStatus``+``mNav`` response per
league, so a parse can be re-checked offline without re-hitting ESPN.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from fractions import Fraction
from pathlib import Path
from typing import List, Tuple

APP = Path(__file__).resolve().parent.parent / "nba_autopilot" / "rootfs" / "app"
sys.path.insert(0, str(APP))

from espn import ids  # noqa: E402
from espn.read import EspnConfig, EspnRead, make_client  # noqa: E402

COOKIE_FILE = Path.home() / ".espn_cookies"
#: The NBA season ESPN is currently serving. ESPN labels a season by the year it
#: *ends* in, so the 2026-27 season is ``2027``. Guessing this wrong is quiet:
#: a finished season answers every request happily, just with final standings.
DEFAULT_SEASON = 2027


def load_cookies() -> Tuple[str, str]:
    if not COOKIE_FILE.exists():
        sys.exit(
            "missing %s\n"
            "  line 1: espn_s2\n"
            "  line 2: SWID (with the braces)\n"
            "  then: chmod 600 %s" % (COOKIE_FILE, COOKIE_FILE)
        )
    mode = COOKIE_FILE.stat().st_mode & 0o077
    if mode:
        sys.exit("%s is group/world readable (mode %o); chmod 600 it"
                 % (COOKIE_FILE, COOKIE_FILE.stat().st_mode & 0o777))
    lines = [ln.strip() for ln in COOKIE_FILE.read_text().splitlines() if ln.strip()]
    if len(lines) < 2:
        sys.exit("%s needs two lines: espn_s2 then SWID" % COOKIE_FILE)
    return lines[0], lines[1]


def parse_target(arg: str) -> Tuple[int, int]:
    """``league_id:team_id``. Both required -- team id gates every write."""
    try:
        league, team = arg.split(":", 1)
        return int(league), int(team)
    except ValueError:
        sys.exit("bad target %r; want LEAGUE_ID:TEAM_ID" % arg)


def as_fraction(x: float) -> str:
    """Render a float as a small fraction when it is one, else blank.

    The GP cap and the acquisition limit both arrive as repeating decimals
    (5.714285714285714), and the fact that they are exact sevenths is the entire
    evidence that they are per-scoring-period values over a seven-day matchup.
    Showing 40/7 instead of 5.714 is the difference between a clue and noise.
    """
    f = Fraction(x).limit_denominator(32)
    if abs(float(f) - x) > 1e-9:
        return ""
    return " = %d/%d" % (f.numerator, f.denominator) if f.denominator != 1 else ""


def probe(league_id: int, team_id: int, season: int, s2: str, swid: str,
          dump: Path | None) -> None:
    cfg = EspnConfig(league_id=league_id, team_id=team_id,
                     espn_s2=s2, swid=swid, season=season)
    with make_client(cfg) as http:
        api = EspnRead(cfg, http)
        raw = api.league("mSettings", "mStatus", "mNav")
        if dump:
            dump.mkdir(parents=True, exist_ok=True)
            path = dump / ("%d-%d.json" % (league_id, season))
            path.write_text(json.dumps(raw, indent=2, sort_keys=True))
            print("  raw dumped to %s" % path)

        st = api.league_settings()
        owned = api.my_team_ids()

        print("\n=== %s  (league %d, season %d) ===" % (st.name.strip(), league_id, season))
        print("  scoring       : %s  (points=%s categories=%s)"
              % (st.scoring_type, st.is_points, st.is_categories))
        print("  period        : scoring %d, matchup %d of %d"
              % (st.current_scoring_period, st.current_matchup_period,
                 len(st.matchup_period_boundaries)))

        # Ownership is the precondition for every write. ESPN will happily
        # execute a commissioner's edit against a team that is not yours and
        # attribute it to you in the public activity feed, so the app refuses
        # to write unless the configured team id appears here.
        ok = "OK" if team_id in owned else "*** NOT YOURS ***"
        print("  my team       : %d %s  (owned: %s)"
              % (team_id, ok, sorted(owned) or "none"))

        print("  starting slots:")
        starters = 0
        for slot, n in sorted(st.lineup_slot_counts.items()):
            tag = ids.SLOT_NAMES.get(slot, "slot %d" % slot)
            bench = slot in ids.NON_SCORING_SLOTS
            if not bench:
                starters += n
            print("      %-5s x%-2d %s" % (tag, n, "(not a starting slot)" if bench else ""))
        print("      -> %d players score each day" % starters)

        if st.has_gp_cap:
            print("  games-played cap:")
            for slot, lim in sorted(st.lineup_slot_stat_limits.items()):
                known = slot in st.lineup_slot_counts
                print("      slot %d stat %d: %.6f%s  %s"
                      % (slot, lim.stat_id, lim.limit_value,
                         as_fraction(lim.limit_value),
                         "" if known else "(slot absent from lineupSlotCounts"
                                          " -> league-wide, not per-slot)"))
                # Two readings to compare against the settings page. If the
                # value is per-scoring-period, x7 is the per-matchup cap; and
                # if that number divides evenly by the number of starting
                # slots, the league is almost certainly expressing "N games
                # per starter per matchup" rather than a flat team total.
                weekly = lim.limit_value * 7
                print("        x7 = %.2f  (per-matchup cap if per-scoring-period)"
                      % weekly)
                if starters:
                    print("        /%d starters = %.3f games per starter"
                          % (starters, weekly / starters))
        else:
            print("  games-played cap: none")

        print("  waivers       : %s" % st.waiver_mode)
        print("  FAAB          : %s" % (("%d budget" % st.faab_budget)
                                        if st.uses_faab else "not in use"))
        print("  lock mode     : %s" % st.roster_lock_mode)

        print("  scoring items : %d" % len(st.scoring_items))
        for stat, pts in sorted(st.scoring_items.items(), key=lambda kv: -abs(kv[1])):
            name = ids.STAT_NAMES.get(stat)
            print("      %+7.2f  %-6s %s" % (pts, "(%d)" % stat,
                                             name or "*** UNKNOWN STAT ***"))


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", metavar="LEAGUE_ID:TEAM_ID")
    ap.add_argument("--season", type=int, default=DEFAULT_SEASON,
                    help="season ESPN labels by its ending year (default %d)"
                         % DEFAULT_SEASON)
    ap.add_argument("--dump", type=Path, default=None,
                    help="directory to write the raw settings response into")
    args = ap.parse_args(argv)

    s2, swid = load_cookies()
    for target in args.targets:
        league_id, team_id = parse_target(target)
        probe(league_id, team_id, args.season, s2, swid, args.dump)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
