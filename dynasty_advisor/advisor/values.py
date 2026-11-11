"""Player and pick values from FantasyCalc.

SOURCE (checked 2026-11-11)
===========================
``GET https://api.fantasycalc.com/values/current?isDynasty=true&numQbs=2&numTeams=10&ppr=1``
-> HTTP 200, 328 KB, 420 rows (396 players + 24 picks). No auth. This is the
endpoint fantasycalc.com's own site uses; it is not a documented public API,
so it can change without notice -- the loader fails loudly rather than
degrading to zero values.

* Each player row has ``player.sleeperId``, so the join to Sleeper is by id,
  not by name.
* ``value`` = dynasty value, ``redraftValue`` = rest-of-season value, both in
  the same units, both in one response.
* Picks cover 2027 rounds 1-4 with Early/Mid/Late tiers plus a generic value,
  and 2028 / 2029 rounds 1-4 generic only.
* K and DEF are not valued. Players outside the top ~400 are absent, which
  this module treats as value 0 (replacement level or below).
* **There is no history endpoint** (tried /players/<id>/values,
  /values/history: 404). Past trades can only be judged at today's values,
  which is hindsight. To build real history, every run snapshots the day's
  values to ``<data>/values/YYYY-MM-DD.json``.
* No payload timestamp. Staleness is measured by comparing against earlier
  snapshots: identical values for several days means the feed has stopped.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

from .http import CachedHttp

log = logging.getLogger(__name__)

FC_URL = (
    "https://api.fantasycalc.com/values/current"
    "?isDynasty=true&numQbs={qbs}&numTeams={teams}&ppr={ppr}"
)
ATTRIBUTION = "Player and pick values: FantasyCalc (fantasycalc.com). Trending data: Sleeper."

_PICK_RE = re.compile(r"^(\d{4}) (\d)(?:st|nd|rd|th)(?: \((Early|Mid|Late)\))?$")


@dataclass(frozen=True)
class PlayerValue:
    sleeper_id: str
    name: str
    position: str
    age: Optional[float]
    dynasty: float
    redraft: float
    trend30: float


def parse_pick_name(name: str) -> Optional[Tuple[str, int, Optional[str]]]:
    m = _PICK_RE.match(name.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


class Values:
    def __init__(
        self,
        players: Mapping[str, PlayerValue],
        picks: Mapping[Tuple[str, int, Optional[str]], float],
        fetched_at: Optional[float] = None,
        unchanged_days: int = 0,
    ) -> None:
        self.players = dict(players)
        self.picks = dict(picks)
        self.fetched_at = fetched_at
        self.unchanged_days = unchanged_days

    def dyn(self, pid: str) -> float:
        v = self.players.get(pid)
        return v.dynasty if v else 0.0

    def now(self, pid: str) -> float:
        v = self.players.get(pid)
        return v.redraft if v else 0.0

    def pick(self, season: str, rnd: int, tier: Optional[str] = None) -> float:
        for key in ((season, rnd, tier), (season, rnd, None)):
            if key in self.picks:
                return self.picks[key]
        # Beyond the feed's horizon: decay the latest known season's value.
        known = sorted({s for (s, r, _t) in self.picks if r == rnd})
        if known and season > known[-1]:
            return self.picks[(known[-1], rnd, None)] * 0.9
        return 0.0


def _qbs(league) -> int:
    return 2 if league.superflex else 1


def _ppr(league) -> str:
    rec = float(league.scoring.get("rec") or 0)
    return "1" if rec >= 1 else "0.5" if rec >= 0.5 else "0"


def parse_rows(rows) -> Tuple[Dict[str, PlayerValue], Dict[Tuple[str, int, Optional[str]], float]]:
    players: Dict[str, PlayerValue] = {}
    picks: Dict[Tuple[str, int, Optional[str]], float] = {}
    for row in rows:
        p = row.get("player") or {}
        if p.get("position") == "PICK":
            k = parse_pick_name(p.get("name") or "")
            if k:
                picks[k] = float(row.get("value") or 0)
            continue
        sid = p.get("sleeperId")
        if not sid:
            continue
        players[str(sid)] = PlayerValue(
            sleeper_id=str(sid),
            name=p.get("name") or str(sid),
            position=p.get("position") or "",
            age=p.get("maybeAge"),
            dynasty=float(row.get("value") or 0),
            redraft=float(row.get("redraftValue") or 0),
            trend30=float(row.get("trend30Day") or 0),
        )
    return players, picks


def load_values(http: CachedHttp, league, data_dir: str) -> Values:
    url = FC_URL.format(qbs=_qbs(league), teams=league.total_rosters, ppr=_ppr(league))
    rows = http.get_json(url, 6 * 3600)
    if not isinstance(rows, list) or len(rows) < 100:
        raise RuntimeError("FantasyCalc returned %r rows; refusing to run on empty values" % (
            len(rows) if isinstance(rows, list) else type(rows).__name__))
    players, picks = parse_rows(rows)
    unchanged = _snapshot_and_measure(data_dir, players, picks)
    log.info("values: %d players, %d picks (unchanged for %d prior snapshot days)",
             len(players), len(picks), unchanged)
    return Values(players, picks, http.fetched_at(url), unchanged)


def _snapshot_and_measure(data_dir: str, players, picks) -> int:
    d = os.path.join(data_dir, "values")
    os.makedirs(d, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    body = {
        "players": {k: [v.dynasty, v.redraft] for k, v in sorted(players.items())},
        "picks": {"%s|%d|%s" % (s, r, t or ""): v for (s, r, t), v in sorted(picks.items(), key=str)},
    }
    path = os.path.join(d, today + ".json")
    with open(path + ".tmp", "w") as f:
        json.dump(body, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    unchanged = 0
    for prev in sorted(glob.glob(os.path.join(d, "*.json")), reverse=True):
        if os.path.basename(prev) >= today + ".json":
            continue
        try:
            with open(prev) as f:
                if json.load(f) != body:
                    break
        except (OSError, ValueError):
            break
        unchanged += 1
    return unchanged
