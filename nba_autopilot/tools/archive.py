#!/usr/bin/env python3
"""Pull seasons of ESPN per-game actuals into a local SQLite archive.

Not imported by the add-on. Needs no credentials -- the endpoint it reads is
public, which is also why it is safe to run from a laptop.

    python3 tools/archive.py --seasons 2023 2024 2025 2026 2027 --db /tmp/nba.db

Deliberately parses through ``sources.defense.parse_schedule`` and
``parse_player_games`` rather than reimplementing them. The point of an archive
is to check what the application believes; an archive built by a second,
subtly different parser checks the parser instead. The two ESPN traps that
cost real debugging time -- a players response carrying two seasons, and a
schedule response carrying no season id -- live in those functions and so are
exercised here too.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rootfs", "app"))

from espn import ids  # noqa: E402
from sources import defense  # noqa: E402

HOST = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons"
FILTER = '{"players":{"limit":3000,"offset":0}}'
#: Raw stat ids kept alongside the scored total, for the checks that are not
#: about opponents: the OREB share, and double-double calibration.
RAW = {"pts": ids.PTS, "reb": ids.REB, "ast": ids.AST, "oreb": ids.OREB,
       "dreb": ids.DREB, "stl": ids.STL, "blk": ids.BLK, "dd": ids.DD,
       "td": ids.TD, "mins": ids.MINUTES}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pg (
  season INT, player_id INT, sp INT, team INT, opp INT,
  centre INT, fp REAL,
  %s,
  PRIMARY KEY (season, player_id, sp)
);
CREATE INDEX IF NOT EXISTS i_opp ON pg(season, opp, centre);
CREATE INDEX IF NOT EXISTS i_pl  ON pg(season, player_id);
""" % ", ".join("%s REAL" % k for k in RAW)


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def cached(path, url, headers=None):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return json.load(open(path))
    payload = get(url, headers)
    with open(path, "w") as fh:
        json.dump(payload, fh)
    time.sleep(3)   # unmetered and public, but not an invitation
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--cache", default="/tmp/nba-archive-cache")
    ap.add_argument("--league", type=int, required=True,
                    help="read scoring coefficients from this league id. Must "
                         "be a publicly viewable league -- this tool sends no "
                         "cookie, and a private league answers 401. No "
                         "default: a league id identifies a real person's "
                         "team and does not belong in a public repository.")
    args = ap.parse_args()
    os.makedirs(args.cache, exist_ok=True)

    settings = get("%s/%d/segments/0/leagues/%d?view=mSettings"
                   % (HOST, max(args.seasons), args.league))
    items = ((settings.get("settings") or {})
             .get("scoringSettings") or {}).get("scoringItems") or []
    coefficients = {int(i["statId"]): float(i["points"]) for i in items}
    if not coefficients:
        raise SystemExit("league %d did not return scoring items -- is it "
                         "private? pass a public --league" % args.league)
    print("scoring coefficients from league %d: %d items"
          % (args.league, len(coefficients)))

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)

    # Every season's calendar, because a players response carries last
    # season's games as well as this season's.
    schedule = {}
    for season in sorted(set(args.seasons) | {min(args.seasons) - 1}):
        raw = cached("%s/sched_%d.json" % (args.cache, season),
                     "%s/%d?view=proTeamSchedules_wl" % (HOST, season))
        schedule.update(defense.parse_schedule(raw, season))
    print("schedule entries: %d" % len(schedule))

    for season in sorted(args.seasons):
        payload = cached(
            "%s/players_%d.json" % (args.cache, season),
            "%s/%d/players?view=kona_player_info" % (HOST, season),
            {"X-Fantasy-Filter": FILTER})
        games = defense.parse_player_games(payload, schedule, coefficients)
        # The raw stats the scored total hides, keyed the same way so they
        # join back onto what the production parser produced.
        raw_by_key = {}
        for pl in payload or []:
            for row in pl.get("stats") or []:
                if (row.get("statSourceId") != ids.STAT_SOURCE_ACTUAL
                        or row.get("statSplitTypeId")
                        != ids.SPLIT_ONE_SCORING_PERIOD):
                    continue
                st = row.get("stats") or {}
                if not st:
                    continue
                key = (row.get("seasonId"), pl.get("id"),
                       row.get("scoringPeriodId"))
                raw_by_key[key] = [float(st.get(str(s), 0.0) or 0.0)
                                   for s in RAW.values()]
        rows = []
        for g in games:
            extra = raw_by_key.get((g.season, g.player_id, g.scoring_period))
            if extra is None:
                continue
            rows.append([g.season, g.player_id, g.scoring_period, g.pro_team,
                         g.opponent, int(g.centre_eligible), g.points] + extra)
        db.executemany(
            "INSERT OR REPLACE INTO pg VALUES (%s)"
            % ",".join("?" * (7 + len(RAW))), rows)
        db.commit()
        by_season = {}
        for g in games:
            by_season[g.season] = by_season.get(g.season, 0) + 1
        print("%d: %6d rows stored  (payload carried seasons %s)"
              % (season, len(rows),
                 ", ".join("%d:%d" % kv for kv in sorted(by_season.items()))))

    total = db.execute("SELECT COUNT(*) FROM pg").fetchone()[0]
    print("archive total: %d player-games -> %s" % (total, args.db))


if __name__ == "__main__":
    main()
