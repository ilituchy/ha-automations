"""Sleeper NBA player index -- the ``GTD`` availability tier.

VERIFIED LIVE 2026-10-20
========================
``GET https://api.sleeper.app/v1/players/nba`` -> HTTP 200, **2,543,618 bytes
(~2.5 MB)**, ~0.12 s, **no authentication, no API key, no rate-limit headers**.
2,176 players (1,886 with ``active: true``).

Distinct ``injury_status`` values observed today (exact counts):

    None            2058   (no injury designation)
    "DTD"             69   day-to-day
    "Out"             42
    "IR"               4
    "GTD"              3   <-- the tier ESPN's feed does not emit

CONTRACTS.md's claim is **confirmed**: Sleeper carries ``GTD`` as a value
genuinely distinct from ``DTD``/``Out``, so a player can be surfaced as
game-time-decision rather than collapsed into ESPN's coarser
Out / Day-To-Day pair.

Note the vocabulary is *not* the NBA PDF's
(Available/Probable/Questionable/Doubtful/Out) -- Sleeper uses
DTD/GTD/Out/IR. :data:`SLEEPER_STATUS_MAP` normalises to the
``ACTIVE|GTD|OUT|IR_ELIGIBLE`` set from CONTRACTS.md section 1.

CROSSWALK TRAP (verified, important)
====================================
``espn_id`` exists as a *key* on the record but is **null for 100% of NBA
players** (0 of 2,176; also 0 of 1,886 active). ``stats_id`` is likewise null
for the players checked. So there is **no id-based ESPN join available from
Sleeper for NBA** -- it is an NFL-populated field. ``crosswalk.py`` must match
Sleeper by normalised name (+ team), and that is why its ``unmatched`` list is
load-bearing rather than decorative.

Useful ids that *are* populated: ``player_id`` (Sleeper's own, a string),
``sportradar_id``, ``rotowire_id``, ``swish_id``, ``fantasy_data_id``.

STALENESS
=========
The endpoint has no payload-level timestamp. Per-record ``news_updated`` is an
epoch **milliseconds** value; the max across all players is used as ``as_of``
-- that is the freshest real signal the feed offers. ``Last-Modified`` /
``Date`` response headers are used as a fallback. If neither yields a
timestamp we return ``None`` rather than assume freshness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Mapping, Optional

import httpx

from .base import (
    SourceError,
    SourceResult,
    build_result,
    fetch_bytes,
    guard,
    http_client,
)

__all__ = [
    "SLEEPER_URL",
    "SLEEPER_STATUS_MAP",
    "SleeperPlayer",
    "Sleeper",
    "normalize_status",
]

log = logging.getLogger(__name__)

SLEEPER_URL = "https://api.sleeper.app/v1/players/nba"

#: Sleeper vocabulary -> CONTRACTS.md section 1 normalised set.
#: ``GTD`` is kept distinct from ``DTD``: that distinction is the entire
#: reason this source exists. Both map to the GTD tier, but see
#: :attr:`SleeperPlayer.injury_status_raw` to recover which one it was.
SLEEPER_STATUS_MAP: Mapping[str, str] = {
    "GTD": "GTD",
    "DTD": "GTD",
    "QUESTIONABLE": "GTD",
    "DOUBTFUL": "OUT",
    "OUT": "OUT",
    "IR": "IR_ELIGIBLE",
    "NA": "OUT",
    "PUP": "IR_ELIGIBLE",
    "SUS": "OUT",
}


def normalize_status(raw: Optional[str]) -> str:
    """Map a raw Sleeper ``injury_status`` to the normalised vocabulary.

    ``None``/empty means no designation, which is ``ACTIVE``.
    An *unrecognised* value is surfaced as ``GTD`` (not ``ACTIVE``) and logged:
    a new Sleeper tier should degrade toward caution, never toward
    "definitely playing".
    """
    if raw is None:
        return "ACTIVE"
    key = str(raw).strip().upper()
    if not key:
        return "ACTIVE"
    mapped = SLEEPER_STATUS_MAP.get(key)
    if mapped is None:
        log.warning("sleeper: unrecognised injury_status %r -> GTD", raw)
        return "GTD"
    return mapped


@dataclass(frozen=True)
class SleeperPlayer:
    sleeper_id: str
    full_name: str
    first_name: Optional[str]
    last_name: Optional[str]
    team: Optional[str]
    position: Optional[str]
    active: bool
    status: Optional[str]
    injury_status_raw: Optional[str]
    injury_status: str
    injury_body_part: Optional[str]
    injury_notes: Optional[str]
    news_updated: Optional[datetime]
    espn_id: Optional[int]
    sportradar_id: Optional[str]
    search_full_name: Optional[str]


def _opt_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _opt_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _epoch_ms(v: Any) -> Optional[datetime]:
    """``news_updated`` is epoch **milliseconds** (verified: 1792447206532)."""
    n = _opt_int(v)
    if n is None or n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _to_player(pid: str, rec: Mapping[str, Any]) -> Optional[SleeperPlayer]:
    first = _opt_str(rec.get("first_name"))
    last = _opt_str(rec.get("last_name"))
    full = _opt_str(rec.get("full_name")) or " ".join(x for x in (first, last) if x)
    if not full:
        return None
    raw_status = _opt_str(rec.get("injury_status"))
    return SleeperPlayer(
        sleeper_id=str(pid),
        full_name=full,
        first_name=first,
        last_name=last,
        team=_opt_str(rec.get("team")) or _opt_str(rec.get("team_abbr")),
        position=_opt_str(rec.get("position")),
        active=bool(rec.get("active")),
        status=_opt_str(rec.get("status")),
        injury_status_raw=raw_status,
        injury_status=normalize_status(raw_status),
        injury_body_part=_opt_str(rec.get("injury_body_part")),
        injury_notes=_opt_str(rec.get("injury_notes")),
        news_updated=_epoch_ms(rec.get("news_updated")),
        espn_id=_opt_int(rec.get("espn_id")),
        sportradar_id=_opt_str(rec.get("sportradar_id")),
        search_full_name=_opt_str(rec.get("search_full_name")),
    )


class Sleeper:
    """Sleeper source.

    ``max_staleness`` defaults to 2 days and is measured against the newest
    ``news_updated`` in the feed. A quiet news day is normal, so the window is
    deliberately looser than the injury report's.
    """

    name = "sleeper"

    def __init__(
        self,
        max_staleness: timedelta = timedelta(days=2),
        url: str = SLEEPER_URL,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.max_staleness = max_staleness
        self.url = url
        self._client = client

    @guard
    def fetch(self) -> Optional[SourceResult]:
        # 2.5 MB: use a longer timeout than the default and read the raw
        # response so we can fall back to Last-Modified for as_of.
        resp = fetch_bytes(self.url, client=self._client, timeout=45.0)
        if resp.status_code != 200:
            raise SourceError("sleeper -> HTTP %s" % resp.status_code)
        try:
            raw = resp.json()
        except ValueError as exc:
            raise SourceError("sleeper returned non-JSON: %s" % exc) from exc
        if not isinstance(raw, dict):
            raise SourceError("sleeper payload is not an object")

        players: Dict[str, SleeperPlayer] = {}
        status_counts: Dict[str, int] = {}
        newest: Optional[datetime] = None
        for pid, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            p = _to_player(pid, rec)
            if p is None:
                continue
            players[p.sleeper_id] = p
            key = p.injury_status_raw or "<none>"
            status_counts[key] = status_counts.get(key, 0) + 1
            if p.news_updated is not None and (newest is None or p.news_updated > newest):
                newest = p.news_updated

        if not players:
            raise SourceError("sleeper decoded 0 players")

        as_of = newest
        if as_of is None:
            header = resp.headers.get("Last-Modified") or resp.headers.get("Date")
            if header:
                try:
                    parsed = parsedate_to_datetime(header)
                    if parsed is not None:
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        as_of = parsed.astimezone(timezone.utc)
                except (TypeError, ValueError):
                    as_of = None
        if as_of is None:
            log.warning("sleeper: no news_updated and no date header -> cannot prove freshness")
            return None

        log.info(
            "sleeper: %d players, injury_status distribution=%s",
            len(players),
            sorted(status_counts.items(), key=lambda kv: -kv[1]),
        )
        payload = {
            "players": players,
            "by_name": _index_by_name(players),
            "status_counts": status_counts,
            "player_count": len(players),
            "source_url": self.url,
        }
        return build_result(self.name, as_of, payload, self.max_staleness)


def _index_by_name(players: Mapping[str, SleeperPlayer]) -> Dict[str, SleeperPlayer]:
    """Name-keyed index, because ``espn_id`` is unusable for NBA (see module doc).

    Uses the same normalisation as ``crosswalk.py``. Collisions keep the
    ``active`` record; a genuine duplicate-name collision is logged.
    """
    from .crosswalk import normalize_name

    out: Dict[str, SleeperPlayer] = {}
    for p in players.values():
        key = normalize_name(p.full_name)
        if not key:
            continue
        prior = out.get(key)
        if prior is None:
            out[key] = p
        elif p.active and not prior.active:
            out[key] = p
        elif p.active and prior.active:
            log.debug("sleeper: duplicate active name %r (%s, %s)", key, prior.sleeper_id, p.sleeper_id)
    return out


def main() -> int:
    """Manual probe: ``python -m sources.sleeper``."""
    logging.basicConfig(level=logging.INFO)
    with http_client() as c:
        res = Sleeper(client=c).fetch()
        if res is None:
            print("sleeper: None")
            return 1
        print("as_of:", res.as_of.isoformat(), "players:", res.payload["player_count"])
        print("statuses:", res.payload["status_counts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
