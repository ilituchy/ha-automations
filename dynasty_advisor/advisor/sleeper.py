"""Sleeper public API client (read-only by design -- the API cannot write).

VERIFIED LIVE 2026-11-11 (week 10, a 10-team superflex dynasty league)
=====================================================================
* docs.sleeper.com: "a read-only HTTP API" ... "you cannot modify contents via
  this API". Every documented endpoint is a GET; POST /state/nfl -> 404.
* ``rosters[].starters[i]`` aligns with ``roster_positions`` with ``BN``
  removed (that league lists no IR/TAXI entries in ``roster_positions``; their
  sizes are ``settings.reserve_slots`` / ``taxi_slots``). 0 eligibility
  mismatches across 108 filled slots including FLEX and SUPER_FLEX.
* An empty starting slot is the string ``"0"``. The player list still holds
  everyone, so bench = players - starters - reserve - taxi.
* ``reserve`` (IR) and ``taxi`` are subsets of ``players``.
* ``matchups/<week>`` starters equal the roster's starters for the current
  week (all 10 teams).
* Pending waiver claims are NOT visible: only processed claims appear in
  ``transactions`` (status ``complete`` / ``failed``). Not observed while a
  claim was actually pending -- treat as likely, not proven.
* Trades: ``creator`` is a USER id, ``consenter_ids`` are ROSTER ids. Only
  completed trades are exposed -- rejected/expired offers never appear.
* Display names change between seasons for the same user id
  (observed: one manager renamed between 2024 and 2025). Key on user id. A roster can change owner between
  seasons, so roster history is not manager history.
* ``/players/nfl`` is 14.7 MB / 12,229 records (the porting doc's "~5 MB" is
  out of date). Docs ask for at most one fetch per day.
* Cross-id fill on 253 rostered players: sportradar/rotowire/fantasy_data
  100%, espn_id/yahoo_id 26.5%, gsis 20%, pff 0%. ``depth_chart_order``
  99.6%. ``practice_participation`` 0% (Wed 11:45 ET) -- re-check late week.
"""

from __future__ import annotations

from typing import Any, List, Optional

from .http import CachedHttp

BASE = "https://api.sleeper.app/v1"
MIN = 60
HOUR = 3600


class Sleeper:
    def __init__(self, http: CachedHttp) -> None:
        self.http = http

    def _get(self, path: str, ttl: Optional[float]) -> Any:
        return self.http.get_json(BASE + path, ttl)

    # ``frozen`` = a completed season: cache forever.
    def state(self) -> dict:
        return self._get("/state/nfl", 15 * MIN)

    def user(self, username: str) -> Optional[dict]:
        return self._get("/user/%s" % username, 24 * HOUR)

    def leagues(self, user_id: str, season: str) -> List[dict]:
        return self._get("/user/%s/leagues/nfl/%s" % (user_id, season), 6 * HOUR) or []

    def league(self, league_id: str, frozen: bool = False) -> dict:
        return self._get("/league/%s" % league_id, None if frozen else HOUR)

    def rosters(self, league_id: str, frozen: bool = False) -> List[dict]:
        return self._get("/league/%s/rosters" % league_id, None if frozen else 10 * MIN) or []

    def users(self, league_id: str, frozen: bool = False) -> List[dict]:
        return self._get("/league/%s/users" % league_id, None if frozen else 6 * HOUR) or []

    def traded_picks(self, league_id: str) -> List[dict]:
        return self._get("/league/%s/traded_picks" % league_id, 10 * MIN) or []

    def drafts(self, league_id: str, frozen: bool = False) -> List[dict]:
        return self._get("/league/%s/drafts" % league_id, None if frozen else 6 * HOUR) or []

    def draft(self, draft_id: str) -> dict:
        return self._get("/draft/%s" % draft_id, 6 * HOUR)

    def draft_picks(self, draft_id: str, frozen: bool = False) -> List[dict]:
        return self._get("/draft/%s/picks" % draft_id, None if frozen else 6 * HOUR) or []

    def transactions(self, league_id: str, week: int, frozen: bool = False) -> List[dict]:
        return self._get(
            "/league/%s/transactions/%d" % (league_id, week), None if frozen else 10 * MIN
        ) or []

    def players(self) -> dict:
        return self._get("/players/nfl", 23 * HOUR)

    def trending(self, kind: str, hours: int = 48, limit: int = 50) -> List[dict]:
        return self._get(
            "/players/nfl/trending/%s?lookback_hours=%d&limit=%d" % (kind, hours, limit),
            2 * HOUR,
        ) or []
