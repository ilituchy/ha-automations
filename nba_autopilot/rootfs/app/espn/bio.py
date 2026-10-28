"""Who a player *is*: how old, how long in the league, where he was drafted.

None of this is in the fantasy API. That API knows what a man is projected to
score and what share of leagues hold him, and those two numbers are a redraft
market's opinion of this season. In a dynasty league they are the wrong
question about a nineteen-year-old: a top-five pick who is not productive yet
is held in few leagues *because* he is not productive yet, and that reads as
"replaceable" to anything that only sees ownership.

So this reads ESPN's public NBA athlete record, which carries age, years of
experience and the draft slot. It is a different host and a different API from
the fantasy one -- no cookie, nothing private -- and the ids are the same
space, verified against three players whose fantasy ids returned their own
bios (Ace Bailey 4873138, Bobby Portis 3064482, LeBron James 1966).

Only the add/drop reviewer reads this, and only in a keeper league. Nothing in
``solve/`` does: how old a player is has no bearing on who should start
tonight, and letting it reach the optimizer would be a way of quietly playing
for next season with this week's lineup.

Like :mod:`espn.news` and :mod:`espn.live`, nothing here raises.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

from models import PlayerBio

log = logging.getLogger("espn.bio")

ATHLETE_URL = (
    "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
    "athletes/%d"
)

#: A birthday is the only field here that changes, and it changes once a year.
#: Long enough that a week of runs costs one request per player.
TTL = 7 * 24 * 3600.0

#: ``player_id -> (deadline, bio or None)``. A miss is cached too: a player
#: ESPN has no record for will still have none tomorrow.
_CACHE: Dict[int, Tuple[float, Optional[PlayerBio]]] = {}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse(player_id: int, payload: Any) -> Optional[PlayerBio]:
    """One athlete record, or ``None`` if there is nothing worth carrying.

    ``None`` rather than an empty bio, so the prompt builder can leave the line
    out entirely. A line reading "age unknown, draft unknown" is worse than no
    line: it invites the reviewer to reason about an absence.
    """
    if not isinstance(payload, Mapping):
        return None
    draft = payload.get("draft") or {}
    exp = payload.get("experience") or {}
    bio = PlayerBio(
        player_id=int(player_id),
        age=_int(payload.get("age")),
        experience_years=_int(exp.get("years")),
        draft_year=_int(draft.get("year")),
        draft_round=_int(draft.get("round")),
        draft_pick=_int(draft.get("selection")),
    )
    return bio if bio.known() else None


def fetch(player_id: Any, client: Optional[httpx.Client] = None,
          timeout: float = 6.0) -> Optional[PlayerBio]:
    """One player's record, cached. Never raises.

    The default client carries no cookies, for the reason given in
    :func:`espn.news.fetch`: this endpoint needs none, and a credential not
    sent is a credential that cannot leak.
    """
    pid = _int(player_id)
    if pid is None:
        return None
    now = time.monotonic()
    hit = _CACHE.get(pid)
    if hit is not None and now < hit[0]:
        return hit[1]
    own = client is None
    http = client or httpx.Client(
        timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    bio: Optional[PlayerBio] = None
    try:
        r = http.get(ATHLETE_URL % pid, timeout=timeout)
        r.raise_for_status()
        bio = parse(pid, r.json())
    except Exception as exc:  # noqa: BLE001
        log.info("no NBA record for player %s (%s); the reviewer will see "
                 "no age or draft slot for him", pid, exc)
    finally:
        if own:
            http.close()
    _CACHE[pid] = (now + TTL, bio)
    return bio


def fetch_many(player_ids: Any,
               client: Optional[httpx.Client] = None) -> Dict[int, PlayerBio]:
    """``{player_id: PlayerBio}`` for the ones that have a record.

    One client for the lot. Called with the two men in a swap, so this is two
    requests on the proposals that get as far as the reviewer and none on the
    ones that do not.
    """
    wanted = [p for p in (_int(x) for x in player_ids or ()) if p is not None]
    if not wanted:
        return {}
    own = client is None
    http = client or httpx.Client(
        timeout=6.0, headers={"User-Agent": "Mozilla/5.0"})
    try:
        found = {}
        for pid in dict.fromkeys(wanted):
            bio = fetch(pid, http)
            if bio is not None:
                found[pid] = bio
        return found
    finally:
        if own:
            http.close()
