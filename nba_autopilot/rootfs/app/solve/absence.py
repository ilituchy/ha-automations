"""Who else is missing from this player's NBA team tonight.

Why this is reported and not priced
-----------------------------------
``solve/role.py`` reads a role change that has already shown up in the box
scores. It cannot see one that has not happened yet, and the clearest example
on either live roster is Anfernee Simons: his recent minutes are **below** the
ones ESPN projected, so that module shrinks him -- while his actual production
runs far above both, because LeBron James and Joel Embiid have been sitting and
the minutes they vacate land on him. No backward-looking window contains
tonight's absence. Only the injury report does.

So the data is worth having. Turning it into a number is a different matter,
and the attempt failed honestly:

    Fitting "minutes vacated by absent team-mates" against "minutes our man
    actually gained", on 246 team-days reconstructed from this season's box
    scores, gave a slope of **+0.014** -- indistinguishable from nothing -- with
    buckets running -0.018, +0.003, +0.020, +0.126 in no order at all.

That is not evidence the mechanism is absent; it is evidence this measurement
cannot see it. Three reasons, all structural:

* **The proxy is wrong.** "Recorded no minutes" bundles injured, rested,
  did-not-play-coach's-decision and cut together, and a rested starter's
  minutes are redistributed quite differently from an injured one's.
* **Visibility is partial.** The archive only holds players rostered in the two
  live leagues -- typically one to six of a team's nine-man rotation -- so most
  of the vacated minutes land on players it cannot see at all.
* **Position is everything and the average destroys it.** A backup centre
  absorbs nearly all of a starting centre's minutes; a guard absorbs none of
  them. Averaged over positions the effect cancels, which is roughly what the
  slope above says.

Rather than invent a coefficient and quietly move every projection with it,
this module states the fact and leaves the judgment to the layer that can
reason about position: the reviewer in ``judge.py``, which is shown this
alongside the swap and can say "Simons is a guard, Embiid is a centre, but
LeBron out means the ball-handling minutes have to go somewhere". That is
precisely the kind of thing the arithmetic cannot do and the reason the
reviewer exists.

If the share is ever to be measured, what is needed is a per-position archive
of (who was actually out, who gained minutes) over months, not the two weeks of
partial box scores available here.

Status weighting
----------------
Ruled out and merely doubtful are kept apart rather than summed, because they
are different facts and the reader needs to know which he is looking at. A
doubtful team-mate may well play, in which case nothing is vacated at all --
and that is exactly the case where a single blended number would mislead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from models import PlayerId, ProPlayer, ProTeamId, RosterEntry

__all__ = [
    "MIN_ROTATION_MINUTES",
    "MIN_VACATED_MINUTES",
    "TOP_TEAMMATES_SHOWN",
    "AbsenceRead",
    "read_absences",
]

#: Minutes a season average must clear for a man to count as part of a
#: rotation at all. A starter's threshold rather than a rotation player's,
#: deliberately: at 12 minutes, twenty of the twenty-seven players on the
#: dynasty roster had a "material" absence on their NBA team, which is not a
#: signal, it is a list. Somebody is always hurt somewhere. What is worth a
#: reader's attention is a *starter-sized* hole.
MIN_ROTATION_MINUTES: float = 20.0

#: Minutes that have to be missing in total before any of it is said out loud
#: -- one full starter's night. One doubtful reserve is not news, and the cost
#: of saying so anyway is that the lines which matter stop being read.
MIN_VACATED_MINUTES: float = 24.0

#: How many absent team-mates a description names. The rest are counted.
#: A card is read in a few seconds and a list of nine names is not read at all.
TOP_TEAMMATES_SHOWN: int = 3


@dataclass(frozen=True)
class AbsenceRead:
    """Who is missing from one player's NBA team, and how big a part they are.

    ``out`` and ``doubtful`` are minutes, not counts: the question is how much
    of a rotation is gone, and two eight-minute reserves are not one Embiid.
    """

    player_id: PlayerId
    #: Season minutes belonging to team-mates ESPN has ruled out.
    out_minutes: float = 0.0
    #: The same for team-mates listed but not ruled out.
    doubtful_minutes: float = 0.0
    #: ``(name, minutes, ruled_out)``, biggest first.
    absent: Tuple[Tuple[str, float, bool], ...] = ()
    #: His own season minutes, so a reader can see the absence against the size
    #: of the man who might absorb it.
    own_minutes: float = 0.0

    @property
    def material(self) -> bool:
        """Is there enough missing to be worth a reader's attention?

        One full starter's night, counting out and doubtful together -- see
        :data:`MIN_VACATED_MINUTES`. Below that the sentence this would produce
        is noise on a card that has a decision to make, and the measured cost
        of getting this wrong is that nobody reads the section.
        """
        return (
            self.out_minutes + self.doubtful_minutes
        ) >= MIN_VACATED_MINUTES

    def describe(self) -> str:
        """One sentence, or empty when nothing material is missing.

        Deliberately states minutes and names and **no inference**. It does not
        say he will play more, because this module does not know that -- see the
        module docstring on the share that could not be measured.
        """
        if not self.material:
            return ""
        bits = []
        for name, minutes, ruled in self.absent[:TOP_TEAMMATES_SHOWN]:
            bits.append("%s (%.0f min/g, %s)" % (
                name, minutes, "out" if ruled else "doubtful",
            ))
        more = len(self.absent) - len(bits)
        if more > 0:
            bits.append("and %d more" % more)
        totals = []
        if self.out_minutes > 0:
            totals.append("%.0f minutes ruled out" % self.out_minutes)
        if self.doubtful_minutes > 0:
            totals.append("%.0f doubtful" % self.doubtful_minutes)
        return (
            "Team-mates missing: %s -- %s against his own %.1f minutes a night."
            % (", ".join(bits), " and ".join(totals), self.own_minutes)
        )


def read_absences(
    roster: Sequence[RosterEntry],
    pro_players: Mapping[ProTeamId, Iterable[ProPlayer]],
    *,
    min_rotation_minutes: float = MIN_ROTATION_MINUTES,
) -> Dict[PlayerId, AbsenceRead]:
    """One read per rostered player, from ``EspnRead.pro_team_players``.

    A player whose NBA team is missing from ``pro_players`` gets an empty read
    rather than no entry: callers write "nothing to report" into a card, and a
    missing key would have each of them decide separately what that means.

    The player himself is never counted among his own absent team-mates, which
    is not hypothetical -- he is in the same list, and if he is the one listed
    OUT then his own minutes would read as an opportunity for himself.
    """
    out: Dict[PlayerId, AbsenceRead] = {}
    for entry in roster:
        mates = list(pro_players.get(entry.pro_team_id) or ())
        own = next(
            (m.minutes for m in mates if int(m.player_id) == int(entry.player_id)),
            0.0,
        )
        absent: List[Tuple[str, float, bool]] = []
        out_min = doubtful_min = 0.0
        for m in mates:
            if int(m.player_id) == int(entry.player_id):
                continue
            if m.minutes < float(min_rotation_minutes):
                continue
            if m.out:
                out_min += m.minutes
                absent.append((m.name, m.minutes, True))
            elif m.doubtful:
                doubtful_min += m.minutes
                absent.append((m.name, m.minutes, False))
        absent.sort(key=lambda a: -a[1])
        out[entry.player_id] = AbsenceRead(
            player_id=entry.player_id,
            out_minutes=out_min,
            doubtful_minutes=doubtful_min,
            absent=tuple(absent),
            own_minutes=own,
        )
    return out
