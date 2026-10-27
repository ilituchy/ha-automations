#!/usr/bin/env python3
"""Re-run the measurements behind ``docs/opponent-defence.md``.

    python3 tools/archive.py --seasons 2023 2024 2025 2026 2027 --db /tmp/nba.db
    python3 tools/measure_defence.py --db /tmp/nba.db

Reports, in the order that decides whether the adjustment should exist at all:

1. **reliability** -- split each season's games into odd and even scoring
   periods, fit both halves independently, correlate the cells. If the halves
   disagree, the spread is noise and there is nothing to model.
2. **which grouping of positions** carries that signal. The answer was not the
   five positions; it was a single centre / not-centre split.
3. **year over year**, which decides whether a prior is usable in October when
   the current season is five games old.
4. **lineup value**, in fantasy points per week, which is the only unit that
   answers "is this worth a moving part".

Stdlib only (ADR-001). Pure functions over a SQLite archive, no network.
"""

from __future__ import annotations

import argparse
import collections
import math
import random
import sqlite3

MIN_PLAYER_GAMES = 20
MIN_BASELINE_FP = 10.0
PRIOR = 25.0

def pearson(pairs):
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    n = len(pairs)
    if n < 3:
        return None, n
    ax = sum(a for a, _ in pairs) / n
    bx = sum(b for _, b in pairs) / n
    sa = math.sqrt(sum((a - ax) ** 2 for a, _ in pairs))
    sb = math.sqrt(sum((b - bx) ** 2 for _, b in pairs))
    if sa == 0 or sb == 0:
        return None, n
    return sum((a - ax) * (b - bx) for a, b in pairs) / (sa * sb), n


def spearman_brown(r):
    """Half-length correlation -> full-length reliability."""
    return None if r is None else 2 * r / (1 + r)


def load(db, season, half=None):
    """Leave-one-out ratios: ``(opponent, centre, parity, fp / his average)``.

    Leave-one-out because including a game in its own baseline lets a 40-point
    night raise the bar it is measured against, which drags every ratio toward
    1.0 and understates whatever is really there.
    """
    total = collections.defaultdict(float)
    count = collections.Counter()
    raw = []
    for pid, centre, sp, opp, fp in db.execute(
            "SELECT player_id, centre, sp, opp, fp FROM pg "
            "WHERE season=? AND opp IS NOT NULL AND mins>0", (season,)):
        total[pid] += fp
        count[pid] += 1
        raw.append((pid, bool(centre), sp, opp, fp))
    out = []
    for pid, centre, sp, opp, fp in raw:
        n = count[pid]
        if n < MIN_PLAYER_GAMES:
            continue
        base = (total[pid] - fp) / (n - 1)
        if base < MIN_BASELINE_FP:
            continue
        if half is not None and (sp <= 87) != (half == 0):
            continue
        out.append((opp, centre, sp % 2, fp / base, base, fp))
    return out


def fit(rows):
    """``(team multiplier, centre adjustment)``, both shrunk toward neutral."""
    if not rows:
        return {}, {}
    grand = sum(r[3] for r in rows) / len(rows)
    by_team = collections.defaultdict(list)
    for opp, _c, _h, ratio, _b, _f in rows:
        by_team[opp].append(ratio)
    team = {}
    for opp, vals in by_team.items():
        w = len(vals) / (len(vals) + PRIOR * 5)
        team[opp] = 1.0 + w * (sum(vals) / len(vals) - grand)
    by_centre = collections.defaultdict(list)
    for opp, centre, _h, ratio, _b, _f in rows:
        if centre:
            by_centre[opp].append(ratio / team.get(opp, 1.0))
    adj = {}
    for opp, vals in by_centre.items():
        w = len(vals) / (len(vals) + PRIOR * 2.5)
        adj[opp] = w * (sum(vals) / len(vals) - grand)
    return team, adj


def multiplier(team, adj, opp, centre):
    m = team.get(opp, 1.0)
    if centre:
        m *= 1.0 + adj.get(opp, 0.0)
    return m


def lineup_sim(rows, team, adj, *, roster=13, start=10, trials=40000,
               changed_only=False, seed=11):
    """Points per day from starting the top ``start`` of a random ``roster``.

    The honest unit. Accuracy metrics flatter an adjustment that is right on
    average; a lineup only cares whether it reordered the right two players.
    """
    rnd = random.Random(seed)
    by_day = collections.defaultdict(list)
    for r in rows:
        by_day[r[2]].append(r)
    days = [d for d, v in by_day.items() if len(v) >= roster]
    if not days:
        return 0.0, 0.0, 0
    gains = []
    for _ in range(trials):
        pool = rnd.sample(by_day[rnd.choice(days)], roster)
        base = sorted(pool, key=lambda r: -r[4])
        adjusted = sorted(
            pool, key=lambda r: -r[4] * multiplier(team, adj, r[0], r[1]))
        if changed_only and ({id(x) for x in base[:start]}
                             == {id(x) for x in adjusted[:start]}):
            continue
        gains.append(sum(r[5] for r in adjusted[:start])
                     - sum(r[5] for r in base[:start]))
    if not gains:
        return 0.0, 0.0, 0
    m = sum(gains) / len(gains)
    sd = math.sqrt(sum((g - m) ** 2 for g in gains) / len(gains))
    return m, sd / math.sqrt(len(gains)), len(gains)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--trials", type=int, default=40000)
    args = ap.parse_args()
    db = sqlite3.connect(args.db)
    db.row_factory = None
    seasons = [r[0] for r in db.execute(
        "SELECT season FROM pg GROUP BY season HAVING COUNT(*) > 5000 "
        "ORDER BY season")]
    print("complete seasons in the archive: %s\n"
          % ", ".join(str(s) for s in seasons))

    data = {s: load(db, s) for s in seasons}
    print("1. SPLIT-HALF RELIABILITY (odd vs even scoring periods)")
    for s in seasons:
        a = [r for r in data[s] if r[2] == 0]
        b = [r for r in data[s] if r[2] == 1]
        ta, ia = fit(a)
        tb, ib = fit(b)
        rt, _ = pearson([(ta.get(k), tb.get(k)) for k in set(ta) | set(tb)])
        ri, _ = pearson([(ia.get(k), ib.get(k)) for k in set(ia) | set(ib)])
        fmt = lambda r: " n/a " if r is None else "%+.3f" % r
        print("   %d   team %s   centre %s"
              % (s, fmt(spearman_brown(rt)), fmt(spearman_brown(ri))))

    print("\n2. YEAR OVER YEAR")
    fits = {s: fit(data[s]) for s in seasons}
    for x, y in zip(seasons, seasons[1:]):
        rt, _ = pearson([(fits[x][0].get(k), fits[y][0].get(k))
                         for k in set(fits[x][0]) & set(fits[y][0])])
        ri, _ = pearson([(fits[x][1].get(k), fits[y][1].get(k))
                         for k in set(fits[x][1]) & set(fits[y][1])])
        fmt = lambda r: " n/a " if r is None else "%+.3f" % r
        print("   %d -> %d   team %s   centre %s" % (x, y, fmt(rt), fmt(ri)))

    print("\n3. EFFECT SIZE (pooled, shrunk)")
    allrows = [r for s in seasons for r in data[s]]
    team, adj = fit(allrows)
    vals = list(team.values())
    mean = sum(vals) / len(vals)
    print("   team multiplier  sd %.4f  range %.4f .. %.4f"
          % (math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)),
             min(vals), max(vals)))
    if adj:
        print("   centre adjust    range %+.1f%% .. %+.1f%%"
              % (100 * min(adj.values()), 100 * max(adj.values())))

    print("\n4. LINEUP VALUE (fit first half -> test second, %d trials)"
          % args.trials)
    tr = [r for s in seasons for r in load(db, s, half=0)]
    te = [r for s in seasons for r in load(db, s, half=1)]
    team, adj = fit(tr)
    for label, kw in (("all matchups", {}),
                      ("only days it changed the lineup",
                       {"changed_only": True})):
        m, se, n = lineup_sim(te, team, adj, trials=args.trials, **kw)
        print("   %-34s %+.3f fp/day  [%+.3f, %+.3f]  %+.2f fp/week  (n=%d)"
              % (label, m, m - 1.96 * se, m + 1.96 * se, m * 7, n))
    print("\n   For scale: mean |error| per game %.1f fp, mean adjustment "
          "%.2f fp."
          % (sum(abs(r[4] - r[5]) for r in te) / len(te),
             sum(abs(multiplier(team, adj, r[0], r[1]) - 1.0) * r[4]
                 for r in te) / len(te)))


if __name__ == "__main__":
    main()
