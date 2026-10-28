"""Where an NBA game actually is, right now.

The fantasy API knows what a player has *scored* today and nothing about the
game he is scoring it in. A roster row with 12.0 beside it is the same row
whether he has played four minutes or thirty-four, and those are opposite
facts to somebody deciding whether a lineup is winning.

So this reads ESPN's public NBA scoreboard, which is a different host and a
different API from the fantasy one: no cookie, no league, and nothing on it
that is private to anybody. It is indexed by ESPN's pro team id, which the
scoreboard and the fantasy API agree on -- verified by joining the two on a
live slate, where only the abbreviations differ (the scoreboard says ``NY``
and ``SA`` where the fantasy API says ``NYK`` and ``SAS``; both call them 18
and 24). The abbreviation is never read here for that reason.

Like :mod:`espn.news`, nothing in here raises: a page that cannot say what
quarter it is should still say everything else it knows.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

import httpx

from models import LiveGame

log = logging.getLogger("espn.live")

#: Today's games. Takes an optional ``dates=YYYYMMDD``, which this does not
#: pass: the only day whose clock is worth showing is the one being played.
SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)

#: ESPN's ``status.type.state``. Anything else is treated as "not started".
IN_PROGRESS = "in"
FINAL = "post"


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse(payload: Any) -> Dict[int, LiveGame]:
    """``{proTeamId: LiveGame}``, with both sides of each game indexed.

    Indexed under both teams for the same reason the pro schedule is: the
    caller has a player, a player has one team, and pairing them up again at
    the call site would be work with a bug in it.

    A game that has not tipped off is skipped entirely rather than returned
    with a state of ``"pre"``. The page already knows how to say "7:30 pm"
    from the schedule, and that is a better answer than an empty clock.
    """
    out: Dict[int, LiveGame] = {}
    for event in (payload or {}).get("events") or []:
        comp = ((event or {}).get("competitions") or [{}])[0] or {}
        status = comp.get("status") or {}
        kind = status.get("type") or {}
        state = str(kind.get("state") or "")
        if state not in (IN_PROGRESS, FINAL):
            continue
        sides = []
        for c in comp.get("competitors") or []:
            tid = _int((c.get("team") or {}).get("id"))
            if tid is not None:
                sides.append((tid, _int(c.get("score"))))
        if len(sides) != 2:
            continue
        period = _int(status.get("period")) or 0
        clock = str(status.get("displayClock") or "")
        # ESPN's own words for the cases a period number cannot express:
        # "Halftime", "End of 2nd", "Final/OT". Carried rather than
        # reconstructed, and only used where it beats the period.
        detail = str(kind.get("shortDetail") or kind.get("detail") or "")
        for i, (tid, score) in enumerate(sides):
            out[tid] = LiveGame(
                pro_team_id=tid,
                final=state == FINAL,
                period=period,
                clock=clock,
                detail=detail,
                score=score,
                opp_score=sides[1 - i][1],
            )
    return out


def fetch(client: Optional[httpx.Client] = None,
          timeout: float = 6.0) -> Dict[int, LiveGame]:
    """The current slate, or an empty mapping. Never raises.

    The default client is a fresh one with no cookies on it. This endpoint
    needs none, and a credential not sent is a credential that cannot leak --
    the same reasoning as :func:`espn.news.fetch`.

    The timeout is short on purpose. This is decoration on a page that is
    useful without it, so it is not worth a page load waiting on.
    """
    own = client is None
    http = client or httpx.Client(
        timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    try:
        r = http.get(SCOREBOARD_URL, timeout=timeout)
        r.raise_for_status()
        return parse(r.json())
    except Exception as exc:  # noqa: BLE001
        log.info("could not read the NBA scoreboard (%s); "
                 "the page will show tipoff times instead", exc)
        return {}
    finally:
        if own:
            http.close()


def label(game: Optional[LiveGame]) -> str:
    """``Q2 1:16``, ``Half``, ``End Q3``, ``Final``, ``Final/OT``.

    ESPN's ``shortDetail`` says ``1:16 - 2nd``, which is three characters
    longer in a column measured in characters -- but it is also the only
    thing that knows about halftime and the end of a period, so those cases
    defer to it and the common one does not.
    """
    if game is None:
        return ""
    if game.final:
        return "Final/OT" if game.period > 4 else "Final"
    low = game.detail.lower()
    if "halftime" in low:
        return "Half"
    if low.startswith("end of") or low.startswith("end "):
        return "End %s" % _period(game.period) if game.period else game.detail
    stage = _period(game.period)
    if not stage:
        return game.detail
    clock = game.clock.strip()
    return "%s %s" % (stage, clock) if clock else stage


def _period(period: int) -> str:
    """``3`` -> ``Q3``; overtime counts from one again."""
    if period <= 0:
        return ""
    if period <= 4:
        return "Q%d" % period
    extra = period - 4
    return "OT" if extra == 1 else "%dOT" % extra
