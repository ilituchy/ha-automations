"""NBA schedule: game dates, tipoff times, back-to-backs.

VERIFIED LIVE 2026-10-20 -- THE ENDPOINT WE SETTLED ON
======================================================
    https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD

HTTP 200, ~40 KB for a 3-game day. Unauthenticated. Chosen as primary per
CONTRACTS.md ("prefer ESPN's site API") because ``stats.nba.com`` /
``cdn.nba.com`` are unreliable/blocked from cloud egress.

Response shape (trimmed, real):

    {"leagues": [...], "events": [
       {"id": "401909088",
        "date": "2026-10-20T19:00Z",
        "name": "Boston Celtics at Detroit Pistons",
        "shortName": "BOS @ DET",
        "season": {...},
        "status": {"type": {"name": "STATUS_SCHEDULED"}},
        "competitions": [{"id": ..., "date": ..., "neutralSite": false,
            "competitors": [
              {"homeAway": "home", "team": {"id": "8",  "abbreviation": "DET"}},
              {"homeAway": "away", "team": {"id": "2",  "abbreviation": "BOS"}}]}]}
    ], "provider": {...}}

``team.id`` is ESPN's numeric team id (DET=8, BOS=2), which lines up with
``ProTeamId`` in CONTRACTS.md section 1.

TWO VERIFIED GOTCHAS
====================
1. **Date ranges are not supported.** ``?dates=20261020-20261031`` returns
   **HTTP 400** (an 80-byte error body, not JSON). Only a single ``YYYYMMDD``
   works, so a date window must be fetched **one day per request**. Verified
   working: 20261020 -> 3 games, 20261021 -> 11 games, 20261022 -> 2 games.

2. **ESPN groups by US game date, not UTC date.** Querying ``20261020``
   returns an event dated ``2026-10-21T01:30Z`` (the 9:30pm ET Thunder/Spurs
   tip). So back-to-back detection must key off the **queried date**, never
   ``datetime(event.date).date()`` -- doing the latter splits a single slate
   across two days and invents phantom back-to-backs. :class:`GameRef` keeps
   ``slate_date`` (the queried key) separate from ``tipoff_utc``.

Back-to-backs are computed as consecutive ``slate_date`` values per team, so
they are only correct across the range actually fetched; a B2B whose first leg
precedes the window is invisible. :meth:`NbaSchedule.fetch` therefore fetches
one extra lead-in day before ``start`` and excludes it from ``games``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import httpx

from .base import (
    RUN_TZ,
    SourceError,
    SourceResult,
    build_result,
    fetch_json,
    guard,
    http_client,
    utcnow,
)

__all__ = [
    "ESPN_SCOREBOARD_URL",
    "GameRef",
    "NbaSchedule",
    "detect_back_to_backs",
]

log = logging.getLogger(__name__)

ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    "?dates={date}"
)


@dataclass(frozen=True)
class GameRef:
    """One scheduled game.

    ``slate_date`` is ESPN's US game-date grouping key (the date we queried).
    ``tipoff_utc`` is the real instant and may fall on the following UTC day.
    """

    event_id: str
    slate_date: date
    tipoff_utc: Optional[datetime]
    home_team_id: Optional[int]
    away_team_id: Optional[int]
    home_abbr: Optional[str]
    away_abbr: Optional[str]
    status: Optional[str]
    neutral_site: bool

    @property
    def team_ids(self) -> Tuple[Optional[int], Optional[int]]:
        return (self.home_team_id, self.away_team_id)

    def tipoff_local(self) -> Optional[datetime]:
        """Tipoff in the canonical run timezone (America/Los_Angeles)."""
        if self.tipoff_utc is None:
            return None
        return self.tipoff_utc.astimezone(RUN_TZ)


def _parse_iso(value: Any) -> Optional[datetime]:
    """ESPN emits ``2026-10-20T19:00Z`` (no seconds)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _opt_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_events(raw: Mapping[str, Any], slate: date) -> List[GameRef]:
    events = raw.get("events")
    if events is None:
        return []
    if not isinstance(events, list):
        raise SourceError("ESPN scoreboard 'events' is not a list")
    out: List[GameRef] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        comps = ev.get("competitions")
        comp = comps[0] if isinstance(comps, list) and comps else {}
        if not isinstance(comp, dict):
            comp = {}
        home_id = away_id = None
        home_ab = away_ab = None
        competitors = comp.get("competitors")
        if isinstance(competitors, list):
            for c in competitors:
                if not isinstance(c, dict):
                    continue
                team = c.get("team") if isinstance(c.get("team"), dict) else {}
                tid = _opt_int(team.get("id"))
                ab = team.get("abbreviation")
                ab = ab if isinstance(ab, str) else None
                if c.get("homeAway") == "home":
                    home_id, home_ab = tid, ab
                elif c.get("homeAway") == "away":
                    away_id, away_ab = tid, ab
        status = None
        st = ev.get("status")
        if isinstance(st, dict) and isinstance(st.get("type"), dict):
            name = st["type"].get("name")
            status = name if isinstance(name, str) else None
        out.append(
            GameRef(
                event_id=str(ev.get("id") or ""),
                slate_date=slate,
                tipoff_utc=_parse_iso(ev.get("date") or comp.get("date")),
                home_team_id=home_id,
                away_team_id=away_id,
                home_abbr=home_ab,
                away_abbr=away_ab,
                status=status,
                neutral_site=bool(comp.get("neutralSite")),
            )
        )
    return out


def detect_back_to_backs(games: Iterable[GameRef]) -> Dict[int, Set[date]]:
    """Map team id -> set of ``slate_date`` on which that team is on the 2nd
    night of a back-to-back.

    Uses ``slate_date`` (ESPN's US game date), never the UTC tipoff date --
    see the module docstring for why that distinction is load-bearing.
    """
    by_team: Dict[int, Set[date]] = {}
    for g in games:
        for tid in g.team_ids:
            if tid is None:
                continue
            by_team.setdefault(tid, set()).add(g.slate_date)
    out: Dict[int, Set[date]] = {}
    for tid, days in by_team.items():
        second: Set[date] = set()
        for d in days:
            if (d - timedelta(days=1)) in days:
                second.add(d)
        out[tid] = second
    return out


class NbaSchedule:
    """Schedule source backed by ESPN's public site API.

    One HTTP request per calendar day (the API rejects ranges -- verified 400),
    plus one lead-in day so a back-to-back straddling the window start is
    still detected.
    """

    name = "nba_schedule"

    def __init__(
        self,
        max_staleness: timedelta = timedelta(hours=12),
        days_ahead: int = 8,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.max_staleness = max_staleness
        self.days_ahead = days_ahead
        self._client = client

    def _fetch_day(self, day: date) -> List[GameRef]:
        url = ESPN_SCOREBOARD_URL.format(date=day.strftime("%Y%m%d"))
        raw = fetch_json(url, client=self._client)
        if not isinstance(raw, dict):
            raise SourceError("ESPN scoreboard returned non-object for %s" % day)
        return _parse_events(raw, day)

    @guard
    def fetch(self, start: Optional[date] = None) -> Optional[SourceResult]:
        now = utcnow()
        begin = start or now.astimezone(RUN_TZ).date()
        lead_in = begin - timedelta(days=1)

        games: List[GameRef] = []
        lead_in_games: List[GameRef] = []
        days_ok = 0
        days_failed: List[str] = []

        for offset in range(-1, max(1, self.days_ahead)):
            day = begin + timedelta(days=offset)
            try:
                found = self._fetch_day(day)
            except SourceError as exc:
                # One bad day must not lose the whole window; record it.
                days_failed.append(day.isoformat())
                log.warning("schedule: day %s failed: %s", day, exc)
                continue
            days_ok += 1
            if day == lead_in:
                lead_in_games.extend(found)
            else:
                games.extend(found)

        if days_ok == 0:
            raise SourceError("ESPN scoreboard: every day in the window failed")

        b2b = detect_back_to_backs(list(games) + list(lead_in_games))

        by_team: Dict[int, List[GameRef]] = {}
        for g in games:
            for tid in g.team_ids:
                if tid is not None:
                    by_team.setdefault(tid, []).append(g)
        for lst in by_team.values():
            lst.sort(key=lambda g: (g.slate_date, g.tipoff_utc or now))

        dates_by_team: Dict[int, List[date]] = {
            tid: sorted({g.slate_date for g in lst}) for tid, lst in by_team.items()
        }

        log.info(
            "schedule: %d games across %d days (%d failed), %d teams",
            len(games),
            days_ok,
            len(days_failed),
            len(by_team),
        )
        payload = {
            "games": games,
            "games_by_team": by_team,
            "dates_by_team": dates_by_team,
            "back_to_back_second_nights": b2b,
            "window_start": begin,
            "window_end": begin + timedelta(days=max(1, self.days_ahead) - 1),
            "days_fetched": days_ok,
            "days_failed": days_failed,
            "source_url": ESPN_SCOREBOARD_URL.format(date=begin.strftime("%Y%m%d")),
        }
        # The scoreboard is generated on request: as_of is now. Staleness here
        # guards against a cached/frozen response, not against a stale feed.
        return build_result(self.name, now, payload, self.max_staleness)


def main() -> int:
    """Manual probe: ``python -m sources.nba_schedule``."""
    logging.basicConfig(level=logging.INFO)
    with http_client() as c:
        res = NbaSchedule(days_ahead=3, client=c).fetch()
        if res is None:
            print("nba_schedule: None")
            return 1
        for g in res.payload["games"]:
            print(
                "  ",
                g.slate_date,
                g.away_abbr,
                "@",
                g.home_abbr,
                g.tipoff_utc.isoformat() if g.tipoff_utc else "?",
            )
        b2b = {k: sorted(v) for k, v in res.payload["back_to_back_second_nights"].items() if v}
        print("teams on 2nd night of B2B:", b2b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
