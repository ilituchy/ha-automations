"""When to look at the lineup again, after the morning run.

The daily run fires at 09:00 and the earliest NBA tip-off is seven hours
later. Everything that happens in those seven hours -- a starter downgraded to
OUT, a late scratch, a coach ruling someone out at shootaround -- was invisible
to it, and until this module nothing looked again.

**The loss is measured, not assumed.** Across every completed scoring period of
the 2026-27 season, counting days where a player in a starting slot recorded
zero while his NBA team played:

===============  ===================  =========================  ===========
League           days with a DNP      days a legal sub existed    recoverable
===============  ===================  =========================  ===========
Chairman's       3                    0                          0.0
Dynassty         3                    2                          45.0
===============  ===================  =========================  ===========

The two that mattered were both solvable from the bench alone. On 2026-11-04
Trey Murphy III sat in Dynassty's ``F`` slot and scored nothing while Bobby
Portis (24.5) and Ziaire Williams (23.5) -- both ``F``-eligible, both with
games -- sat behind him. ESPN's own feed said ``PROBABLE`` at 09:00 and
``OUT`` the next morning; the news landed in between. The same day in
Chairman's the identical scratch cost nothing, because the only other
``F``-eligible men were already starting and the two bench players with games
were guards. That asymmetry is why this schedules a *re-optimization* rather
than a slot patch: whether a scratch is recoverable at all is a question only
the solver can answer.

Why tip-off-relative rather than another fixed hour: ESPN locks a player's slot
when his game starts, so the deadline is per-player, not per-day. A fixed
second run at 15:00 would be two hours early for a 16:00 tip and three hours
late for the news that breaks at 18:00 before a 19:30 one. Anchoring to the
tip-offs that actually exist on today's slate spends the same handful of
requests where they can still change something.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence, Set

log = logging.getLogger(__name__)

#: How long before a tip-off to re-check. Long enough that the write lands
#: before ESPN locks the slot even if the read is slow, short enough to be
#: after the beat reporters file. Inactive lists are usually official by
#: 30 minutes out, and "ruled out" language appears well before that.
LEAD = timedelta(minutes=45)

#: Tip-offs closer together than this share one sweep. A full NBA slate puts
#: six games at 19:00 ET and the rest on the half hour; without coalescing
#: that is eight re-optimizations to answer one question. The bucket must stay
#: under ``LEAD`` or a coalesced sweep could land after the earliest tip in its
#: own bucket -- see ``test_coalescing_never_lands_after_a_tip``.
BUCKET = timedelta(minutes=20)

#: A hard ceiling on sweeps per day. Nothing in a real slate approaches it;
#: it exists so a malformed feed with a hundred parsed tip-offs cannot fill
#: the scheduler. The earliest ones are kept, because an unchecked early game
#: is the one that locks first.
MAX_SWEEPS = 8


def sweep_times(
    games: Iterable[object],
    teams: Set[int],
    now: datetime,
    *,
    lead: timedelta = LEAD,
    bucket: timedelta = BUCKET,
    max_sweeps: int = MAX_SWEEPS,
) -> List[datetime]:
    """Times to re-check the lineup today, soonest first.

    ``games`` are :class:`sources.nba_schedule.GameRef`-shaped: anything with
    ``tipoff_utc`` and ``team_ids``. ``teams`` are the pro-team ids we have a
    player on -- a game between two teams we do not roster cannot change our
    lineup, so it buys no sweep.

    Only times strictly after ``now`` are returned. A tip-off already inside
    the lead window is dropped rather than scheduled in the past: the slot is
    about to lock, and a sweep that cannot finish before it does is a request
    spent on nothing.
    """
    wanted: List[datetime] = []
    for g in games:
        tip = getattr(g, "tipoff_utc", None)
        if tip is None:
            # `startTimeTBD`. There is no instant to anchor to, and guessing
            # one would schedule a sweep against a fiction.
            continue
        ids = [t for t in (getattr(g, "team_ids", ()) or ()) if t is not None]
        if not any(int(t) in teams for t in ids):
            continue
        at = tip.astimezone(now.tzinfo) - lead
        if at > now:
            wanted.append(at)

    wanted.sort()
    out: List[datetime] = []
    for at in wanted:
        if out and at - out[-1] < bucket:
            # Keep the earlier one. It is before the later tip as well, so it
            # still covers that game; the reverse is not true.
            continue
        out.append(at)
    if len(out) > max_sweeps:
        log.warning("schedule produced %d sweep times; keeping the first %d",
                    len(out), max_sweeps)
        out = out[:max_sweeps]
    return out


def describe(times: Sequence[datetime], *, now: Optional[datetime] = None) -> str:
    """One log line naming the plan, for the startup and post-run messages."""
    if not times:
        return "no lineup sweeps today (no upcoming tip-offs for our players)"
    return "%d lineup sweep(s) today: %s" % (
        len(times), ", ".join(t.strftime("%H:%M") for t in times),
    )
