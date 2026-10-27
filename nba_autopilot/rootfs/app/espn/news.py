"""ESPN's per-player fantasy news feed. Public, and not part of the league API.

This is the one ESPN endpoint the app reads that needs **no cookie** and knows
nothing about a league -- a player's news is a property of the player. Taken
from ESPN's own player card, which requests it alongside ``kona_playercard``:

    site.api.espn.com/apis/fantasy/v3/games/fba/news/players?days=30&playerId=N

Three kinds of item come back, distinguished by ``type``:

* ``Rotowire`` -- the per-player note. ``description`` is what happened and
  ``story`` is the analysis ESPN labels "Spin". Plain text, no markup.
* ``Story`` -- a full ESPN article that merely mentions him. ``story`` is HTML
  and runs to several thousand words.
* ``Media`` -- a highlight video, whose ``description`` is its own title
  repeated and which carries nothing readable at all.

Only the first is a note *about him*, so only the first is kept. The other two
are what makes ESPN's own news page mostly video thumbnails.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional

import httpx

from models import NewsItem

log = logging.getLogger(__name__)

NEWS_URL = "https://site.api.espn.com/apis/fantasy/v3/games/fba/news/players"

#: ESPN's own card asks for 30. Kept the same so that what this shows and what
#: ESPN shows cannot drift into disagreeing about how far back "recent" goes.
DEFAULT_DAYS = 30

#: The only item type that is a note about one player. See the module docstring.
PLAYER_NOTE = "rotowire"


def _published(value: Any) -> Optional[datetime]:
    """ESPN's ISO-8601 with a ``Z``, which ``fromisoformat`` rejects before 3.11."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse(payload: Any, limit: int) -> List[NewsItem]:
    """The player notes out of one feed response, newest first.

    Separate from the fetch so the shape can be tested without a socket, which
    is the same split every source in ``sources/`` uses.
    """
    # ``v3`` nests the feed one level deeper than ``v2`` did -- ``{"news":
    # {"feed": [...]}}`` against ``{"feed": [...]}`` -- and returns a
    # well-formed, empty-looking response if you read it at the wrong depth.
    # Both are accepted so the shape, not the version number, decides.
    root = payload or {}
    feed = root.get("feed")
    if feed is None:
        feed = (root.get("news") or {}).get("feed")
    out: List[NewsItem] = []
    for item in feed or []:
        if str(item.get("type") or "").lower() != PLAYER_NOTE:
            continue
        # ``description`` rather than ``headline``: they carry the same
        # sentence, but ESPN truncates the headline mid-word at about ninety
        # characters and does not truncate the description.
        out.append(NewsItem(
            published=_published(item.get("published")),
            source=str(item.get("type") or ""),
            headline=str(item.get("description") or "").strip(),
            spin=str(item.get("story") or "").strip(),
        ))
        if len(out) >= limit:
            break
    return out


def fetch(
    player_id: int,
    *,
    limit: int = 6,
    days: int = DEFAULT_DAYS,
    client: Optional[httpx.Client] = None,
    timeout: float = 4.0,
) -> List[NewsItem]:
    """Recent notes about one player, or ``[]``.

    Never raises. The card is worth rendering without its news -- the stats and
    the game log are the half that decides a start/sit -- so a slow or broken
    news feed must not be able to take the page down with it. The timeout is
    short for the same reason: this is one of two calls a page load waits on.

    A client may be passed in, but the default is a fresh one **without** the
    league cookies: this host does not need them, and a credential not sent is
    a credential that cannot leak.
    """
    owned = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        r = http.get(NEWS_URL, params={"days": int(days),
                                       "playerId": int(player_id)})
        if r.status_code >= 400:
            log.debug("news for %s: ESPN returned %d", player_id, r.status_code)
            return []
        return parse(r.json(), limit)
    except Exception as exc:  # noqa: BLE001
        log.debug("news for %s is unavailable: %s", player_id, exc)
        return []
    finally:
        if owned:
            http.close()
