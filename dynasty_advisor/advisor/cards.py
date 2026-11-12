"""Player cards: tap a name on the page, see who he is and how he has played.

Modelled on nba_autopilot's player card (rootfs/app/web.py): the name is a
real link to a real page, and a small script opens the same markup in a
dialog over the page instead of navigating away.

Two halves:

* **The index** (``<data>/players.json``), written by every run. Bio, injury,
  depth chart and FantasyCalc values for every fantasy-relevant player, plus
  the league's scoring settings and who rosters whom. A card can be opened for
  any name on the page without the web process loading Sleeper's 14.7 MB
  player dump.
* **Live reads** when a card is opened, through the shared on-disk cache:

  - ``GET api.sleeper.app/stats/nfl/player/<id>?season_type=regular&season=<y>&grouping=week``
    -> ``{"1": {week, opponent, is_away_team, team, stats{...}} | null, ...}``.
    Without ``grouping`` the same URL returns the season total.
  - ``GET api.sleeper.app/projections/nfl/player/<id>?...&grouping=week`` -> same
    shape, ``category: "proj"``.
  - ``GET api.sleeper.app/schedule/nfl/regular/<y>`` -> 272 games
    ``{week, home, away, date, status}``. A team with no game in a week is on
    its bye. (A null *projection* is not a bye: injured players get null too.)
  - ``GET site.api.espn.com/apis/fantasy/v3/games/ffl/news/players?playerId=<espn id>``
    -> Rotowire notes (``headline``, ``story``, ``published``). Keyed on the
    ESPN id Sleeper carries for each player.

  These stats endpoints are what sleeper.com's own player pages call; they are
  not in the documented v1 API, so every read here is optional: a card with no
  stats still shows the bio and values.

SCORING (verified 2026-11-11)
=============================
Fantasy points are recomputed from raw stats with the league's own
``scoring_settings``: ``sum(stats[k] * scoring[k])``. Checked against
``matchups/9`` ``players_points`` for 36 players in the owner's league:
36/36 equal to the hundredth. So past seasons are shown in *this* league's
scoring, not Sleeper's generic PPR.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .http import CachedHttp

log = logging.getLogger("advisor.cards")

INDEX_VERSION = 1
PLAYER_ID_RE = re.compile(r"^[0-9A-Z]{1,10}$")
FANTASY_POS = ("QB", "RB", "WR", "TE")
STATS_URL = "https://api.sleeper.app/stats/nfl/player/%s?season_type=regular&season=%s%s"
PROJ_URL = "https://api.sleeper.app/projections/nfl/player/%s?season_type=regular&season=%s&grouping=week"
NEWS_URL = "https://site.api.espn.com/apis/fantasy/v3/games/ffl/news/players?playerId=%s&limit=4"
SCHEDULE_URL = "https://api.sleeper.app/schedule/nfl/regular/%s"
HEADSHOT = "https://sleepercdn.com/content/nfl/players/thumb/%s.jpg"

#: Stat columns per position: (header, key or (numerator, denominator) for "a/b").
COLUMNS: Dict[str, List[Tuple[str, Any]]] = {
    "QB": [("CMP/ATT", ("pass_cmp", "pass_att")), ("Pass yds", "pass_yd"), ("TD", "pass_td"),
           ("INT", "pass_int"), ("Car", "rush_att"), ("Rush yds", "rush_yd"), ("Rush TD", "rush_td")],
    "RB": [("Car", "rush_att"), ("Rush yds", "rush_yd"), ("Rush TD", "rush_td"), ("Tgt", "rec_tgt"),
           ("Rec", "rec"), ("Rec yds", "rec_yd"), ("Rec TD", "rec_td")],
    "WR": [("Tgt", "rec_tgt"), ("Rec", "rec"), ("Rec yds", "rec_yd"), ("TD", "rec_td"),
           ("Car", "rush_att"), ("Rush yds", "rush_yd")],
}
COLUMNS["TE"] = COLUMNS["WR"]


def _e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


# ---------------------------------------------------------------------------
# Index, written by a run
# ---------------------------------------------------------------------------


def write_index(data_dir: str, league, players: Dict[str, dict], values, book, me: int) -> str:
    owners: Dict[str, int] = {}
    for rid, t in league.teams.items():
        for pid in t.players:
            owners[pid] = rid
    ranked = sorted(values.players.values(), key=lambda v: -v.dynasty)
    overall = {v.sleeper_id: i for i, v in enumerate(ranked, 1)}
    by_pos: Dict[str, int] = {}
    pos_rank: Dict[str, int] = {}
    for v in ranked:
        by_pos[v.position] = by_pos.get(v.position, 0) + 1
        pos_rank[v.sleeper_id] = by_pos[v.position]

    out: Dict[str, dict] = {}
    for pid, p in players.items():
        fp = p.get("fantasy_positions") or ([p["position"]] if p.get("position") else [])
        if not (set(fp) & set(FANTASY_POS)) and pid not in owners:
            continue
        if not p.get("team") and pid not in owners and pid not in values.players:
            continue  # retired / unsigned and nobody cares
        v = values.players.get(pid)
        out[pid] = {k: x for k, x in {
            "name": p.get("full_name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or pid,
            "pos": (fp or [p.get("position")])[0],
            "team": p.get("team"),
            "num": p.get("number"),
            "age": (v.age if v and v.age else p.get("age")),
            "ht": p.get("height"), "wt": p.get("weight"),
            "college": p.get("college"), "exp": p.get("years_exp"),
            "status": p.get("injury_status"), "body": p.get("injury_body_part"),
            "note": p.get("injury_notes"),
            "depth": p.get("depth_chart_order"), "depth_pos": p.get("depth_chart_position"),
            "espn": p.get("espn_id"),
            "dyn": round(v.dynasty) if v else None, "now": round(v.redraft) if v else None,
            "trend": round(v.trend30) if v else None,
            "rank": overall.get(pid), "prank": pos_rank.get(pid),
            "owner": owners.get(pid),
        }.items() if x not in (None, "", [])}
    body = {
        "version": INDEX_VERSION,
        "league": league.name, "season": league.season, "week": league.week, "me": me,
        "teams": {str(rid): t.display_name for rid, t in league.teams.items()},
        "scoring": {k: v for k, v in league.scoring.items() if v},
        "replacement": round(book.replacement),
        "players": out,
    }
    path = os.path.join(data_dir, "players.json")
    with open(path + ".tmp", "w") as f:
        json.dump(body, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    log.info("player index: %d players", len(out))
    return path


class Index:
    """``players.json``, reloaded when a run rewrites it."""

    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(data_dir, "players.json")
        self._lock = threading.Lock()
        self._mtime = None
        self._data: Optional[dict] = None

    def get(self) -> Optional[dict]:
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return None
        with self._lock:
            if m != self._mtime:
                try:
                    with open(self.path) as f:
                        d = json.load(f)
                    self._data = d if d.get("version") == INDEX_VERSION else None
                except (OSError, ValueError):
                    self._data = None
                self._mtime = m
            return self._data


# ---------------------------------------------------------------------------
# A card
# ---------------------------------------------------------------------------


def points(stats: Dict[str, float], scoring: Dict[str, float]) -> float:
    return sum(float(stats.get(k) or 0) * float(v) for k, v in scoring.items())


def _sum(rows: List[dict]) -> Dict[str, float]:
    tot: Dict[str, float] = {}
    for r in rows:
        for k, v in r["stats"].items():
            if isinstance(v, (int, float)):
                tot[k] = tot.get(k, 0.0) + v
    return tot


def _played(w: Optional[dict]) -> bool:
    if not w or not isinstance(w.get("stats"), dict):
        return False
    s = w["stats"]
    return bool(s.get("gp") or s.get("off_snp") or s.get("gms_active"))


def _opp(w: dict) -> str:
    o = w.get("opponent") or ""
    return ("@" + o if w.get("is_away_team") else o) if o else ""


def build_card(index: Optional[dict], pid: str, http: CachedHttp) -> Dict[str, Any]:
    if not PLAYER_ID_RE.match(pid or ""):
        return {"error": "That is not a player id."}
    if not index:
        return {"error": "Run the advisor once first — player cards are built from a run's data."}
    p = (index.get("players") or {}).get(pid)
    if p is None:
        return {"error": "No player with that id in this league's data."}
    season = int(index["season"])
    week = int(index.get("week") or 0)
    scoring = index.get("scoring") or {}
    pos = p.get("pos") or ""
    owner = p.get("owner")
    view: Dict[str, Any] = dict(p, pid=pid, season=season, week=week,
                                owner_name=(index.get("teams") or {}).get(str(owner)) if owner else None,
                                mine=owner is not None and owner == index.get("me"),
                                columns=[h for h, _ in COLUMNS.get(pos, [])])
    warnings = []

    # This season, week by week. Current season data changes every game day.
    log_rows, splits = [], []
    try:
        weekly = http.get_json(STATS_URL % (pid, season, "&grouping=week"), 3600) or {}
        played = sorted((w for w in weekly.values() if _played(w)), key=lambda w: int(w.get("week") or 0))
        for w in reversed(played):
            s = w["stats"]
            snaps = (100.0 * s["off_snp"] / s["tm_off_snp"]) if s.get("off_snp") and s.get("tm_off_snp") else None
            log_rows.append({"week": int(w["week"]), "opp": _opp(w), "fpts": points(s, scoring),
                             "snap": snaps, "cells": _cells(pos, s)})
        if played:
            splits.append(_split(str(season), [w["stats"] for w in played], pos, scoring))
            if len(played) > 3:
                splits.append(_split("Last 3", [w["stats"] for w in played[-3:]], pos, scoring))
    except Exception as e:  # noqa: BLE001 -- the card still has a bio
        log.warning("card %s: weekly stats unavailable: %s", pid, e)
        warnings.append("Stats are unavailable right now.")

    # Two previous seasons, totals. Completed seasons never change.
    for y in (season - 1, season - 2):
        try:
            tot = http.get_json(STATS_URL % (pid, y, ""), None)
            s = (tot or {}).get("stats") if isinstance(tot, dict) else None
            if s and s.get("gp"):
                splits.append(_split(str(y), [s], pos, scoring, games=int(s["gp"])))
        except Exception as e:  # noqa: BLE001
            log.info("card %s: %s totals unavailable: %s", pid, y, e)

    # The next three games, from the real schedule, projected in this league's scoring.
    upcoming = []
    team = p.get("team")
    try:
        sched = http.get_json(SCHEDULE_URL % season, 6 * 3600) or [] if team else []
        games = {}
        for g in sched:
            if team in (g.get("home"), g.get("away")):
                games[int(g["week"])] = g
        proj = {}
        try:
            proj = http.get_json(PROJ_URL % (pid, season), 6 * 3600) or {}
        except Exception as e:  # noqa: BLE001
            log.info("card %s: projections unavailable: %s", pid, e)
        last = max(games) if games else 0
        for wk in range(max(week, 1), last + 1):
            g = games.get(wk)
            if g is not None and g.get("status") == "complete":
                continue
            if g is None:
                upcoming.append({"week": wk, "opp": "Bye", "fpts": None, "bye": True})
            else:
                w = proj.get(str(wk))
                pts = points(w["stats"], scoring) if w and isinstance(w.get("stats"), dict) else None
                upcoming.append({"week": wk, "opp": ("@" + g["home"]) if g["away"] == team else g["away"],
                                 "fpts": pts, "date": g.get("date")})
            if len(upcoming) == 3:
                break
    except Exception as e:  # noqa: BLE001
        log.info("card %s: schedule unavailable: %s", pid, e)

    news = []
    if p.get("espn"):
        try:
            feed = ((http.get_json(NEWS_URL % p["espn"], 1800) or {}).get("news") or {}).get("feed") or []
            for n in feed[:4]:
                news.append({"when": _when(n.get("published")), "headline": n.get("headline"),
                             "story": n.get("story") if n.get("story") != n.get("headline") else None})
        except Exception as e:  # noqa: BLE001
            log.info("card %s: news unavailable: %s", pid, e)

    view.update(log=log_rows, splits=splits, upcoming=upcoming, news=news, warnings=warnings)
    return view


def _cells(pos: str, s: Dict[str, float]) -> List[str]:
    out = []
    for _h, key in COLUMNS.get(pos, []):
        if isinstance(key, tuple):
            out.append("%d/%d" % (s.get(key[0]) or 0, s.get(key[1]) or 0))
        else:
            out.append("{:,.0f}".format(s.get(key) or 0))
    return out


def _split(label: str, rows: List[dict], pos: str, scoring, games: Optional[int] = None) -> dict:
    tot = _sum([{"stats": r} for r in rows])
    gp = games if games is not None else len(rows)
    fp = points(tot, scoring)
    return {"label": label, "gp": gp, "fpts": fp, "fpg": fp / gp if gp else 0.0, "cells": _cells(pos, tot)}


def _day(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    return "%s %d/%d" % (dt.strftime("%a"), dt.month, dt.day)


def _when(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return iso[:10]
    return "%s %d" % (dt.strftime("%a %b"), dt.day)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

STATUS_WORD = {"Questionable": "warn", "Doubtful": "bad", "Out": "bad", "IR": "bad", "PUP": "bad",
               "Sus": "bad", "NA": "bad", "COV": "bad"}


def _n(x) -> str:
    return "{:,.0f}".format(x)


def _height(h) -> str:
    try:
        i = int(h)
    except (TypeError, ValueError):
        return _e(h or "")
    return "%d′%d″" % (i // 12, i % 12)


def _head(v: dict) -> str:
    pos = v.get("pos") or ""
    sub = " · ".join(x for x in (
        pos, v.get("team") or "Free agent", "#%s" % v["num"] if v.get("num") else "") if x)
    st = v.get("status")
    status = ("<span class='pstat %s'>%s%s</span>" % (STATUS_WORD.get(st, "warn"), _e(st),
                                                     " · " + _e(v["body"]) if v.get("body") else "")
              if st else "<span class='pstat ok'>Healthy</span>")
    face = ("<span class=face style=\"background-image:url('%s')\" role=img aria-label=''></span>" % _e(HEADSHOT % v["pid"])
            if pos in FANTASY_POS else "<span class=face></span>")
    facts = []
    if v.get("dyn") is not None:
        facts.append(("Dynasty value", _n(v["dyn"]), "#%s overall · %s%s" % (v.get("rank"), pos, v.get("prank"))))
    if v.get("now") is not None:
        facts.append(("Rest of season", _n(v["now"]), ""))
    if v.get("trend"):
        t = v["trend"]
        facts.append(("30-day trend", "<span class='delta %s'><i aria-hidden=true>%s</i>%s%s</span>" % (
            "up" if t > 0 else "down", "▲" if t > 0 else "▼", "+" if t > 0 else "−", _n(abs(t))), "", True))
    if v.get("age"):
        facts.append(("Age", "%.0f" % float(v["age"]) if isinstance(v["age"], (int, float)) else _e(v["age"]),
                      ("Rookie" if v.get("exp") == 0 else "%s yr%s exp" % (v["exp"], "" if v.get("exp") == 1 else "s"))
                      if v.get("exp") is not None else ""))
    if v.get("depth"):
        dp = v.get("depth_pos") or pos
        facts.append(("Depth chart", "%s%s" % (pos if dp in ("LWR", "RWR", "SWR") else dp, v["depth"]),
                      "slot" if dp == "SWR" else ""))
    facts.append(("Rostered by", "You" if v.get("mine") else v.get("owner_name") or "Free agent", ""))
    cells = []
    for f in facts:
        label, value, note = f[0], f[1], f[2]
        raw = len(f) > 3
        cells.append("<div><dt>%s</dt><dd>%s</dd>%s</div>" % (
            _e(label), value if raw else _e(value), "<span class=pnote>%s</span>" % _e(note) if note else ""))
    bio = " · ".join(x for x in (
        _height(v.get("ht")) if v.get("ht") else "", "%s lb" % v["wt"] if v.get("wt") else "",
        v.get("college") or "") if x)
    return ("<div class=phero>%s<div class=pwho><h2>%s</h2><div class=psub>%s %s</div>%s</div>"
            "<dl class=pfacts>%s</dl></div>" % (
                face, _e(v.get("name")), _e(sub), status,
                "<div class=pbio>%s</div>" % _e(bio) if bio else "", "".join(cells)))


def _table(head: List[str], rows: List[str]) -> str:
    return ("<div class=pscroll><table class=ptab><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>"
            % ("".join(head), "".join(rows)))


def _stats(v: dict) -> str:
    if not v["splits"]:
        return ""
    cols = v["columns"]
    head = ["<th class=plab>Season</th>", "<th>GP</th>", "<th class=fpts>FP/G</th>", "<th>FPTS</th>"] + [
        "<th>%s</th>" % _e(c) for c in cols]
    rows = ["<tr><th class=plab>%s</th><td>%d</td><td class=fpts>%.1f</td><td>%s</td>%s</tr>" % (
        _e(s["label"]), s["gp"], s["fpg"], _n(s["fpts"]), "".join("<td>%s</td>" % _e(c) for c in s["cells"]))
        for s in v["splits"]]
    return ("<section class=pblock><h3>Stats <span>in your league's scoring</span></h3>%s</section>"
            % _table(head, rows))


def _upcoming(v: dict) -> str:
    if not v["upcoming"]:
        return ""
    return "<section class=pblock><h3>Next up <span>projected</span></h3><div class=pnext>%s</div></section>" % "".join(
        "<div class=pgame><span class=pwk>Wk %d%s</span><b>%s</b><span class=pproj>%s</span></div>" % (
            u["week"], " · " + _e(_day(u["date"])) if u.get("date") else "", _e(u["opp"]),
            "" if u.get("bye") else "%.1f pts" % u["fpts"] if u["fpts"] is not None else "no projection")
        for u in v["upcoming"])


def _gamelog(v: dict) -> str:
    if not v["log"]:
        return ("<section class=pblock><h3>Game log</h3><p class=pnone>No games played this season.</p></section>"
                if not v["warnings"] else "")
    cols = v["columns"]
    head = ["<th class=plab>Wk</th>", "<th class=popp>Opp</th>", "<th class=fpts>FPTS</th>", "<th>Snap%</th>"] + [
        "<th>%s</th>" % _e(c) for c in cols]
    best = max(r["fpts"] for r in v["log"])
    rows = ["<tr><th class=plab>%d</th><td class=popp>%s</td><td class='fpts%s'>%.1f</td><td>%s</td>%s</tr>" % (
        r["week"], _e(r["opp"] or "—"), " top" if r["fpts"] == best and best > 0 else "", r["fpts"],
        "%.0f%%" % r["snap"] if r["snap"] is not None else "—", "".join("<td>%s</td>" % _e(c) for c in r["cells"]))
        for r in v["log"]]
    return "<section class=pblock><h3>Game log</h3>%s</section>" % _table(head, rows)


def _news(v: dict) -> str:
    items = v["news"]
    note = v.get("note")
    if not items and not note:
        return ""
    out = ["<section class=pblock><h3>News</h3>"]
    if note:
        out.append("<article class=pnews><div class=pwhen>Injury note</div><p class=phead2>%s</p></article>" % _e(note))
    for n in items:
        out.append("<article class=pnews><div class=pwhen>%s</div><p class=phead2>%s</p>%s</article>" % (
            _e(n["when"]), _e(n["headline"]), "<p class=pspin>%s</p>" % _e(n["story"]) if n.get("story") else ""))
    out.append("</section>")
    return "".join(out)


def card_html(v: dict) -> str:
    if v.get("error"):
        return "<div class=pcard><p class='pnone perr'>%s</p></div>" % _e(v["error"])
    warn = "".join("<p class=pnone>%s</p>" % _e(w) for w in v["warnings"])
    return "<div class=pcard>%s%s%s%s%s%s<p class=pcredit>Stats: Sleeper · News: Rotowire via ESPN · Values: FantasyCalc</p></div>" % (
        _head(v), "<section class=pblock>%s</section>" % warn if warn else "",
        _upcoming(v), _stats(v), _gamelog(v), _news(v))


CSS = """
/* player links */
a.pl { color:inherit; text-decoration:none; background-image:linear-gradient(var(--line-2),var(--line-2));
  background-size:100% 1px; background-position:0 100%; background-repeat:no-repeat; cursor:pointer; }
a.pl:hover { background-image:linear-gradient(var(--accent),var(--accent)); color:var(--accent); }

/* the dialog */
#pcard { position:fixed; inset:0; margin:auto; border:0; padding:0; width:min(620px, calc(100vw - 20px));
  max-height:min(88vh, 900px); border-radius:18px; background:var(--surface); color:var(--text);
  box-shadow:0 10px 40px rgba(0,0,0,.28); overflow:hidden; }
#pcard::backdrop { background:rgba(10,10,8,.5); }
#pcard .pbody { max-height:inherit; overflow-y:auto; overscroll-behavior:contain; }
.pclose { position:sticky; top:0; z-index:2; height:0; text-align:right; }
.pclose button { position:relative; top:12px; right:12px; width:32px; height:32px; padding:0; border-radius:999px;
  font-size:19px; line-height:1; background:var(--raised); color:var(--text-2); box-shadow:inset 0 0 0 1px var(--line-2); }
.pclose button:hover { color:var(--text); filter:none; }
.ppage { max-width:680px; margin:0 auto; }
.ppage .pcard { background:var(--surface); border:1px solid var(--line); border-radius:18px; box-shadow:var(--shadow); overflow:hidden; }
.back { display:inline-block; margin:0 0 12px; font-size:14px; color:var(--text-2); text-decoration:none; }
.back:hover { color:var(--accent); }

/* the card */
.phero { display:grid; grid-template-columns:auto 1fr; gap:0 14px; align-items:center; padding:18px 18px 16px; }
#pcard .pwho { padding-right:40px; }
.face { width:64px; height:64px; border-radius:50%; background:var(--sunk) center/cover no-repeat;
  box-shadow:inset 0 0 0 1px var(--line); flex:none; }
.pwho { min-width:0; }
.pwho h2 { font-size:21px; line-height:1.2; }
.psub { font-size:13px; color:var(--text-2); margin-top:3px; display:flex; flex-wrap:wrap; align-items:center; gap:4px 8px; }
.pbio { font-size:12px; color:var(--text-3); margin-top:2px; }
.pstat { font-size:11px; font-weight:650; padding:1px 8px; border-radius:999px; border:1px solid var(--line-2); color:var(--text); }
.pstat::before { content:""; display:inline-block; width:6px; height:6px; border-radius:50%; margin-right:5px; vertical-align:1px; background:var(--good); }
.pstat.warn::before { background:var(--bubble); } .pstat.bad::before { background:var(--bad); }
.pfacts { grid-column:1 / -1; margin:16px 0 0; display:grid; grid-template-columns:repeat(auto-fill, minmax(112px, 1fr)); gap:10px; }
.pfacts div { background:var(--sunk); border:1px solid var(--line); border-radius:12px; padding:8px 10px; min-width:0; }
.pfacts dt { font-size:11px; color:var(--text-2); white-space:nowrap; }
.pfacts dd { margin:1px 0 0; font-size:17px; font-weight:650; line-height:1.25; font-variant-numeric:tabular-nums;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pnote { display:block; font-size:11px; color:var(--text-3); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.pblock { border-top:1px solid var(--line); padding:14px 18px 16px; }
.pblock h3 { font-size:12px; font-weight:650; letter-spacing:.05em; text-transform:uppercase; color:var(--text-2); margin-bottom:10px; }
.pblock h3 span { font-weight:500; letter-spacing:0; text-transform:none; color:var(--text-3); margin-left:6px; }
.pnext { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:8px; }
.pgame { border:1px solid var(--line); border-radius:12px; padding:8px 10px; display:flex; flex-direction:column; }
.pwk { font-size:11px; color:var(--text-3); } .pgame b { font-size:15px; } .pproj { font-size:12px; color:var(--text-2); font-variant-numeric:tabular-nums; }
.pscroll { overflow-x:auto; margin:0 -18px; padding:0 18px; }
.ptab { border-collapse:collapse; width:100%; font-size:13px; font-variant-numeric:tabular-nums; }
.ptab th, .ptab td { padding:6px 0 6px 12px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--line); }
.ptab thead th { font-size:11px; font-weight:600; color:var(--text-3); }
.ptab tbody tr:last-child th, .ptab tbody tr:last-child td { border-bottom:none; }
.ptab .plab { text-align:left; padding-left:0; font-weight:600; color:var(--text); }
.ptab thead .plab { color:var(--text-3); }
.ptab .popp { text-align:left; color:var(--text-2); }
.ptab td.fpts { font-weight:700; color:var(--text); }
.ptab td.fpts.top { box-shadow:inset 0 -2px 0 var(--accent); }
.pnews + .pnews { margin-top:12px; padding-top:12px; border-top:1px solid var(--line); }
.pwhen { font-size:11px; letter-spacing:.03em; text-transform:uppercase; color:var(--text-3); }
.phead2 { margin:3px 0 0; font-size:14px; font-weight:600; line-height:1.4; }
.pspin { margin:5px 0 0; font-size:13px; line-height:1.55; color:var(--text-2); }
.pnone { margin:0; font-size:13px; color:var(--text-2); }
.perr { padding:22px 18px; }
.pcredit { margin:0; padding:10px 18px 16px; font-size:11px; color:var(--text-3); border-top:1px solid var(--line); }
@media (max-width:520px) { .pnext { grid-template-columns:repeat(3, minmax(88px,1fr)); overflow-x:auto; } .face { width:56px; height:56px; } }
"""

#: The empty dialog the script fills. Inert until a name is tapped.
SHELL = ("<dialog id=pcard aria-label='Player card'><form method=dialog class=pclose>"
         "<button aria-label=Close>&times;</button></form><div class=pbody></div></dialog>")

#: A progressive enhancement, as in nba_autopilot: every name is a real link to
#: ``player?id=...``. With the script blocked or <dialog> unsupported, the
#: click navigates to the same card rendered as a page. Modified clicks
#: (cmd/ctrl/middle) are left alone so they still open a tab.
JS = """<script>
(function () {
  var dlg = document.getElementById('pcard');
  if (!dlg || typeof dlg.showModal !== 'function' || !window.fetch) return;
  var body = dlg.querySelector('.pbody'), token = 0;
  document.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a[data-card]') : null;
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    var mine = ++token, href = a.getAttribute('href');
    body.innerHTML = '<p class="pnone perr">Loading\\u2026</p>';
    dlg.showModal();
    fetch(href + '&frag=1', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (h) { if (mine === token) { body.innerHTML = h; body.scrollTop = 0; } })
      .catch(function () { if (mine === token) window.location = href; });
  });
  dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener('close', function () { body.innerHTML = ''; });
})();
</script>"""
