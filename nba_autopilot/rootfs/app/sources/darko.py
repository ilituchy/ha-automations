"""DARKO DPM projections (per-100 + minutes + pace).

VERIFIED LIVE 2026-10-20
========================
``https://www.darko.app/__data.json`` -> HTTP 200, ~165 KB,
``content-type: application/json``. ``darko.app`` 307-redirects to
``www.darko.app``; the old ``apanalytics.shinyapps.io/DARKO`` Shiny app still
answers 200 but is not a machine-readable surface, so the SvelteKit route is
primary.

The payload uses SvelteKit's **index-pointer encoding**: a flat ``nodes[i].data``
array where every value is either a literal or an *integer index into that same
array*. :func:`decode_sveltekit_data` resolves it (see the docstring there).

Tables are encoded **column-major** as ``{"keys": <idx>, "values": <idx>}``,
where ``keys`` points at a list of column-name pointers and ``values`` at a
list of column-array pointers. Verified: 26 columns x 518 players.

Columns actually present (verified):
    nba_id, player_name, team_name, tm_id, position, age, season,
    rookie_season, career_game_num, dpm, o_dpm, d_dpm, box_dpm, on_off_dpm,
    x_minutes, x_pace, x_pts_100, x_ast_100, x_fg_pct, x_fg3_pct, x_ft_pct,
    sal_market_fixed, actual_salary, surplus_value, _rank, season_games

STALENESS -- THE HARD FAIL
==========================
This is the finding that matters, and it is bad news:

* ``asOf`` is present in the root object but **points at a null slot** -- i.e.
  DARKO publishes the field and leaves it empty. It is NOT a usable timestamp.
* The only date string in the whole index payload is
  ``ratingsThrough: "2026-06-13"`` -- the 2026 NBA Finals. A June date on
  2026-10-20 is **four months stale**.
* Corroborating: ``season_games == 0`` for all 518 rows, and ``season == 2027``
  for all of them. These are **preseason priors**, not in-season ratings.

Per CONTRACTS.md ("a July snapshot in October is a hard fail, not a warning")
this module therefore reports itself STALE and ``fetch()`` returns ``None``
today. The caller must fall back to ESPN ``statSourceId=1``. We deliberately do
*not* fudge ``as_of`` to "now" to make the pipeline feel healthier -- that would
silently feed June priors into October lineup decisions.

``as_of`` is derived from ``ratingsThrough`` (the honest number). If DARKO
starts populating ``asOf``, that wins automatically.

A per-player route ``/player/<nba_id>/__data.json`` also exists and carries a
richer per-100 set (stl/blk/tov/orb/drb/fga/fg3a/fta). Its ``playerInfo.date``
is the player's *next scheduled game* (``future_game: 1``), NOT a snapshot
time -- do not mistake it for ``as_of``. :meth:`Darko.fetch_player` exposes it
for enrichment, but staleness is still judged on ``ratingsThrough``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

import httpx

from .base import (
    SourceError,
    SourceResult,
    build_result,
    fetch_json,
    guard,
    http_client,
)

__all__ = [
    "DARKO_INDEX_URL",
    "DARKO_PLAYER_URL",
    "DarkoPlayer",
    "Darko",
    "decode_sveltekit_data",
    "decode_sveltekit_payload",
]

log = logging.getLogger(__name__)

DARKO_INDEX_URL = "https://www.darko.app/__data.json"
DARKO_PLAYER_URL = "https://www.darko.app/player/{nba_id}/__data.json"

#: Sentinel used by SvelteKit for a hole in the array.
_HOLE = -1


@dataclass(frozen=True)
class DarkoPlayer:
    """One decoded DARKO row. ``nba_id`` is the NBA ``personId``."""

    nba_id: int
    name: str
    team_name: Optional[str]
    tm_id: Optional[int]
    position: Optional[str]
    season: Optional[int]
    season_games: Optional[int]
    minutes: Optional[float]
    pace: Optional[float]
    pts_100: Optional[float]
    ast_100: Optional[float]
    fg_pct: Optional[float]
    fg3_pct: Optional[float]
    ft_pct: Optional[float]
    dpm: Optional[float]
    extra: Mapping[str, Any]


def decode_sveltekit_data(flat: Sequence[Any]) -> Any:
    """Resolve SvelteKit's index-pointer array into plain Python.

    Encoding, as verified against the live payload:

    * ``flat[0]`` is the root value.
    * An ``int`` in a value position is an **index into ``flat``**, not a
      number. ``-1`` is a hole (``None``).
    * ``dict`` values map key -> pointer.
    * ``list`` values are lists of pointers.
    * Literals (``str``, ``float``, ``bool``, ``None``) resolve to themselves.
    * ``{"keys": p, "values": p}`` is a column-major table; it is left intact
      here (resolved by :func:`_table_to_rows`) because turning it into rows
      needs the column semantics.

    Cycles are possible in principle (SvelteKit dedupes aggressively), so a
    visiting set guards the recursion.
    """

    def resolve(ptr: Any, seen: Set[int]) -> Any:
        if isinstance(ptr, bool) or not isinstance(ptr, int):
            # Already a literal (bool must be checked before int).
            return ptr
        if ptr == _HOLE:
            return None
        if ptr < 0 or ptr >= len(flat):
            return None
        if ptr in seen:
            return None
        node = flat[ptr]
        if isinstance(node, bool) or node is None or isinstance(node, (str, float)):
            return node
        if isinstance(node, int):
            # A pointer-to-pointer. Follow it once with this index marked.
            return resolve(node, seen | {ptr})
        nxt = seen | {ptr}
        if isinstance(node, list):
            return [resolve(p, nxt) for p in node]
        if isinstance(node, dict):
            return {k: resolve(v, nxt) for k, v in node.items()}
        return node

    if not flat:
        return None
    return resolve(0, set())


def decode_sveltekit_payload(raw: Mapping[str, Any]) -> Sequence[Any]:
    """Pull the flat data array out of a ``__data.json`` envelope.

    Shape: ``{"type": "data", "nodes": [null, {"type": "data", "data": [...]}]}``.
    ``{"type": "skip"}`` nodes appear when SvelteKit is asked to revalidate
    only part of the tree (e.g. with ``?x-sveltekit-invalidated=001``, which
    returns a 58-byte all-skip body) -- treated as "no data".
    """
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        raise SourceError("DARKO payload has no 'nodes' array")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "data" and isinstance(node.get("data"), list):
            return node["data"]
    raise SourceError("DARKO payload contained no data node (all skipped?)")


def _table_to_rows(
    flat: Sequence[Any], table: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """Expand a column-major ``{"keys","values"}`` table into row dicts."""
    key_ptr = table.get("keys")
    val_ptr = table.get("values")
    if not isinstance(key_ptr, int) or not isinstance(val_ptr, int):
        raise SourceError("DARKO table missing keys/values pointers")
    col_ptrs = flat[key_ptr]
    val_ptrs = flat[val_ptr]
    if not isinstance(col_ptrs, list) or not isinstance(val_ptrs, list):
        raise SourceError("DARKO table keys/values are not arrays")

    names: List[str] = []
    for p in col_ptrs:
        nm = flat[p] if isinstance(p, int) and 0 <= p < len(flat) else p
        names.append(str(nm))

    columns: Dict[str, List[Any]] = {}
    for name, vp in zip(names, val_ptrs):
        if not isinstance(vp, int) or not (0 <= vp < len(flat)):
            columns[name] = []
            continue
        arr = flat[vp]
        if not isinstance(arr, list):
            columns[name] = []
            continue
        resolved: List[Any] = []
        for cell in arr:
            if isinstance(cell, bool) or not isinstance(cell, int):
                resolved.append(cell)
            elif cell == _HOLE:
                resolved.append(None)
            elif 0 <= cell < len(flat):
                v = flat[cell]
                resolved.append(None if isinstance(v, (list, dict)) else v)
            else:
                resolved.append(None)
        columns[name] = resolved

    length = max((len(c) for c in columns.values()), default=0)
    rows: List[Dict[str, Any]] = []
    for i in range(length):
        row: Dict[str, Any] = {}
        for name in names:
            col = columns.get(name) or []
            row[name] = col[i] if i < len(col) else None
        rows.append(row)
    return rows


def _as_float(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> Optional[int]:
    f = _as_float(v)
    return int(f) if f is not None else None


_CORE = frozenset(
    {
        "nba_id",
        "player_name",
        "team_name",
        "tm_id",
        "position",
        "season",
        "season_games",
        "x_minutes",
        "x_pace",
        "x_pts_100",
        "x_ast_100",
        "x_fg_pct",
        "x_fg3_pct",
        "x_ft_pct",
        "dpm",
    }
)


def _row_to_player(row: Mapping[str, Any]) -> Optional[DarkoPlayer]:
    nba_id = _as_int(row.get("nba_id"))
    name = row.get("player_name")
    if nba_id is None or not isinstance(name, str) or not name.strip():
        return None
    extra = {k: v for k, v in row.items() if k not in _CORE}
    return DarkoPlayer(
        nba_id=nba_id,
        name=name.strip(),
        team_name=row.get("team_name") if isinstance(row.get("team_name"), str) else None,
        tm_id=_as_int(row.get("tm_id")),
        position=row.get("position") if isinstance(row.get("position"), str) else None,
        season=_as_int(row.get("season")),
        season_games=_as_int(row.get("season_games")),
        minutes=_as_float(row.get("x_minutes")),
        pace=_as_float(row.get("x_pace")),
        pts_100=_as_float(row.get("x_pts_100")),
        ast_100=_as_float(row.get("x_ast_100")),
        fg_pct=_as_float(row.get("x_fg_pct")),
        fg3_pct=_as_float(row.get("x_fg3_pct")),
        ft_pct=_as_float(row.get("x_ft_pct")),
        dpm=_as_float(row.get("dpm")),
        extra=extra,
    )


def _parse_date(value: Any) -> Optional[datetime]:
    """Parse ``YYYY-MM-DD`` or a full ISO timestamp into tz-aware UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            d = datetime.strptime(text, "%Y-%m-%d").date()
            return datetime.combine(d, dtime(0, 0), tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Darko:
    """DARKO source. Returns ``None`` when the published snapshot is stale.

    ``max_staleness`` defaults to 7 days: DARKO normally refreshes nightly
    in-season, so a week of slack tolerates a short outage while still
    rejecting an offseason snapshot by a wide margin.
    """

    name = "darko"

    def __init__(
        self,
        max_staleness: timedelta = timedelta(days=7),
        url: str = DARKO_INDEX_URL,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.max_staleness = max_staleness
        self.url = url
        self._client = client

    # -- internals -----------------------------------------------------
    def _decode(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        flat = decode_sveltekit_payload(raw)
        root = flat[0] if flat else None
        if not isinstance(root, dict):
            raise SourceError("DARKO root node is not an object")

        as_of_raw: Optional[str] = None
        for field in ("asOf", "ratingsThrough"):
            ptr = root.get(field)
            if isinstance(ptr, int) and 0 <= ptr < len(flat):
                val = flat[ptr]
                if isinstance(val, str) and val.strip():
                    as_of_raw = val
                    log.debug("DARKO as_of taken from %s=%s", field, val)
                    break
            elif isinstance(ptr, str) and ptr.strip():
                as_of_raw = ptr
                break

        players_ptr = root.get("players")
        rows: List[Dict[str, Any]] = []
        if isinstance(players_ptr, int) and 0 <= players_ptr < len(flat):
            table = flat[players_ptr]
            if isinstance(table, dict) and "keys" in table and "values" in table:
                rows = _table_to_rows(flat, table)
            elif isinstance(table, list):
                rows = [r for r in decode_sveltekit_data(flat) or [] if isinstance(r, dict)]
        return {"as_of_raw": as_of_raw, "rows": rows}

    # -- public API ----------------------------------------------------
    @guard
    def fetch(self) -> Optional[SourceResult]:
        raw = fetch_json(self.url, client=self._client)
        if not isinstance(raw, dict):
            raise SourceError("DARKO returned a non-object body")
        decoded = self._decode(raw)

        as_of = _parse_date(decoded["as_of_raw"])
        if as_of is None:
            # No honest timestamp => we cannot prove freshness => hard fail.
            log.warning(
                "DARKO published no usable asOf/ratingsThrough (raw=%r); "
                "treating as stale, fall back to ESPN statSourceId=1",
                decoded["as_of_raw"],
            )
            return None

        players: Dict[int, DarkoPlayer] = {}
        for row in decoded["rows"]:
            p = _row_to_player(row)
            if p is not None:
                players[p.nba_id] = p
        if not players:
            raise SourceError("DARKO decoded 0 players")

        played = sum(1 for p in players.values() if (p.season_games or 0) > 0)
        if played == 0:
            log.warning(
                "DARKO: all %d rows have season_games=0 -- this is a PRESEASON "
                "PRIOR snapshot (ratingsThrough=%s), not in-season ratings",
                len(players),
                decoded["as_of_raw"],
            )

        payload = {
            "players": players,
            "as_of_raw": decoded["as_of_raw"],
            "player_count": len(players),
            "players_with_games": played,
            "source_url": self.url,
        }
        return build_result(self.name, as_of, payload, self.max_staleness)

    @guard
    def fetch_player(self, nba_id: int) -> Optional[Dict[str, Any]]:
        """Richer per-100 set for one player (stl/blk/tov/reb/attempts).

        Returns the decoded ``playerInfo`` mapping, or ``None``. NOTE: its
        ``date`` field is the next scheduled game, not a snapshot time.
        """
        url = DARKO_PLAYER_URL.format(nba_id=int(nba_id))
        raw = fetch_json(url, client=self._client)
        if not isinstance(raw, dict):
            return None
        flat = decode_sveltekit_payload(raw)
        root = flat[0] if flat else None
        if not isinstance(root, dict):
            return None
        ptr = root.get("playerInfo")
        if not isinstance(ptr, int) or not (0 <= ptr < len(flat)):
            return None
        info = flat[ptr]
        if not isinstance(info, dict):
            return None
        out: Dict[str, Any] = {}
        for k, v in info.items():
            if isinstance(v, bool) or not isinstance(v, int):
                out[k] = v
            elif v == _HOLE:
                out[k] = None
            elif 0 <= v < len(flat):
                val = flat[v]
                out[k] = None if isinstance(val, (list, dict)) else val
            else:
                out[k] = None
        return out


def main() -> int:
    """Manual probe: ``python -m sources.darko``."""
    logging.basicConfig(level=logging.INFO)
    with http_client() as c:
        src = Darko(client=c)
        res = src.fetch()
        if res is None:
            print("darko: FETCH RETURNED None (stale or unreachable)")
            raw = fetch_json(src.url, client=c)
            if isinstance(raw, dict):
                print("  decoded as_of_raw =", src._decode(raw)["as_of_raw"])
            return 1
        print("darko as_of:", res.as_of.isoformat())
        print("players:", res.payload["player_count"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
