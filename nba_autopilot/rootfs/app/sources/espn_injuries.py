"""ESPN's NBA injuries page -- structured status plus attributed beat reporting.

VERIFIED LIVE 2026-10-21
========================
``GET https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries``
-> **HTTP 200, 896,974 bytes, unauthenticated**. No cookies, no API key, no
Referer needed; ``site.web.api.espn.com`` serves the identical body. 28 teams,
80 notes on the probe.

**This path must never carry fantasy cookies.** :meth:`EspnInjuries.fetch`
builds its own client rather than accepting the caller's, so a bug elsewhere
cannot leak ``espn_s2``/``SWID`` onto a public endpoint. That is a deliberate
inversion of the usual "inject the client" convention in this package.

Shape
-----
``{timestamp, status, season, injuries: [ {id, displayName, injuries: [note]} ]}``
and each note::

    {"id": "-58751",
     "status": "Out",                       # or "Day-To-Day" -- these two only
     "date": "2026-10-19T19:38Z",
     "shortComment": "Gueye underwent surgery Tuesday to repair ...",
     "longComment": "Gueye will be re-evaluated by medical staff in 3-to-4 ...",
     "type": {"name": "INJURY_STATUS_OUT", "abbreviation": "O"},
     "details": {"fantasyStatus": {"abbreviation": "OUT"}, "type": "Foot",
                 "side": "Left", "returnDate": "2026-11-20"},
     "athlete": {"displayName": ..., "links": [...], "team": {...}}}

Observed vocabulary on the probe: ``status`` was **only** ``Out`` (52) or
``Day-To-Day`` (28); ``details.fantasyStatus.abbreviation`` was ``OUT`` (49),
``GTD`` (28) or ``OFS`` (3, out for season).

Why two tiers
-------------
Tier 1 is that structured ``status``. It is coarse: ``Day-To-Day`` covers
everything from "probable, will play" to "has already been ruled out tonight",
which is a 0.9-vs-0.0 difference in ``p_play`` that the field cannot express.

Tier 2 recovers it from ``shortComment``, which is **attributed, timestamped
beat reporting** -- ESPN bundles the reporter's own sentence into the feed
("Hawkins (illness) is out for Wednesday's game against the Raptors, K.C.
Johnson of Chicago Sports Network reports"). So there is no scraping and no
language model anywhere in this module: the wording is already structured
enough for a regex, and :data:`_COMMENT_RULES` records the phrase it matched
so a human can audit every refinement in the run notes.

Measured against the 80 live notes, the rules refined **25 of 28**
``Day-To-Day`` notes -- 6 probable, 9 questionable, 2 doubtful, 8 already out
for tonight -- with no classification that contradicted an eyeball read. The
three misses fall back to the structured status rather than guess: a
"participated in Sunday's practice" or "went through a full practice" note
carries no game-level verdict, and inventing one is worse than staying coarse.

The ceiling rule
----------------
A comment bucket is only accepted up to the ceiling its structured status
allows (:data:`_CEILING`): ``Day-To-Day`` tops out at ``PROBABLE`` and ``Out``
at ``OUT``. Appearing on this page at all means ESPN does not consider the
player cleared, so no amount of optimistic wording may promote a note to
``ACTIVE``; and a comment reading "probable" next to a structured ``Out`` is a
mixed/stale note, not permission to start the player. Clamps are recorded on
the note (``clamped``) instead of being silently applied.

Freshness hazard
----------------
The page ``timestamp`` is effectively the request time, so it is a weak
staleness signal; the real freshness is per note. Game-level wording
("out for Wednesday's game") is spent once that game tips, so every note
carries ``reported_at`` and callers must age it themselves -- a Monday note
saying "out Tuesday" is not evidence about Friday.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .base import (
    SourceError,
    SourceResult,
    build_result,
    fetch_json,
    guard,
    http_client,
)

__all__ = [
    "ESPN_INJURIES_URL",
    "STATUS_MAP",
    "FANTASY_STATUS_MAP",
    "InjuryNote",
    "EspnInjuries",
    "athlete_id",
    "classify_comment",
    "parse",
]

log = logging.getLogger(__name__)

ESPN_INJURIES_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
)

#: Staleness window applied when the page ``timestamp`` is missing and the
#: newest note's own date has to stand in for it. It is deliberately wider
#: than the default: the page timestamp is the request time and so is always
#: seconds old, whereas the newest note can legitimately be a day old on a
#: quiet news day. Reusing the narrow window there would take the source dark
#: precisely when nothing is wrong with it.
FALLBACK_MAX_STALENESS = timedelta(hours=36)

#: ESPN's structured ``status`` -> a ``models.P_PLAY_PRIOR`` key.
STATUS_MAP: Dict[str, str] = {
    "OUT": "OUT",
    "DAY-TO-DAY": "DAY_TO_DAY",
    "DAY TO DAY": "DAY_TO_DAY",
    "GTD": "GTD",
    "ACTIVE": "ACTIVE",
    "AVAILABLE": "ACTIVE",
    "PROBABLE": "PROBABLE",
    "QUESTIONABLE": "QUESTIONABLE",
    "DOUBTFUL": "DOUBTFUL",
}

#: ``details.fantasyStatus.abbreviation`` -> a ``P_PLAY_PRIOR`` key. ``OFS``
#: ("out for season") is the one that matters for drop decisions rather than
#: for tonight's lineup.
FANTASY_STATUS_MAP: Dict[str, str] = {
    "OUT": "OUT",
    "OFS": "OUT",
    "GTD": "GTD",
    "DTD": "DAY_TO_DAY",
    "P": "PROBABLE",
    "Q": "QUESTIONABLE",
    "D": "DOUBTFUL",
    "ACTIVE": "ACTIVE",
}

#: Availability order, worst first. Used only for the ceiling comparison.
_ORDER: Tuple[str, ...] = (
    "OUT",
    "DOUBTFUL",
    "QUESTIONABLE",
    "GTD",
    "DAY_TO_DAY",
    "PROBABLE",
    "ACTIVE",
)
_RANK = {name: i for i, name in enumerate(_ORDER)}

#: The best bucket each structured status may be refined *up* to. See
#: "The ceiling rule" in the module docstring.
_CEILING: Dict[str, str] = {
    "OUT": "OUT",
    "DAY_TO_DAY": "PROBABLE",
    "GTD": "PROBABLE",
}

# Ordered most-decisive-first; the first rule that matches wins. OUT leads
# because its phrasings routinely contain a weaker word ("has returned to
# individual on-court activity but won't play") and the negation forms
# ("not expected to play") must be seen before the positive ACTIVE rule.
_COMMENT_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "OUT",
        re.compile(
            r"\b(?:is|was|has\s+been|have\s+been)\s+"
            r"(?:listed\s+as\s+|officially\s+|ruled\s+)?out\b"
            r"|\bruled\s+out\b"
            r"|\bwon'?t\s+(?:play|suit\s+up|travel|debut|be\s+(?:available|ready))\b"
            r"|\bwill\s+not\s+(?:play|suit\s+up|travel|debut|be\s+(?:available|ready))\b"
            r"|\bwill\s+miss\b"
            r"|\bis\s+(?:set|expected)\s+to\s+miss\b"
            r"|\b(?:is|remains)\s+out\s+(?:indefinitely|for)\b"
            r"|\bnot\s+expected\s+to\s+play\b",
            re.IGNORECASE,
        ),
    ),
    ("DOUBTFUL", re.compile(r"\bdoubtful\b", re.IGNORECASE)),
    ("QUESTIONABLE", re.compile(r"\bquestionable\b", re.IGNORECASE)),
    ("PROBABLE", re.compile(r"\bprobable\b", re.IGNORECASE)),
    (
        "ACTIVE",
        re.compile(
            r"\b(?:is|has\s+been)\s+(?:fully\s+)?cleared\b"
            r"|\bis\s+(?:expected|set)\s+to\s+(?:play|return)\b"
            r"|\bwill\s+(?:play|return|start)\b"
            r"|\bis\s+available\b",
            re.IGNORECASE,
        ),
    ),
)

#: ``https://www.espn.com/nba/player/_/id/4712863/mouhamed-gueye``
_ATHLETE_ID_RE = re.compile(r"/id/(\d+)\b")

#: ``2026-10-19T19:38Z`` -- ESPN omits the seconds, so ``fromisoformat`` on
#: Python 3.9 cannot take it directly.
_NOTE_DATE_FMTS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ")


@dataclass(frozen=True)
class InjuryNote:
    """One player's current injury note, tier 1 and tier 2 side by side.

    ``status`` is ESPN's structured verdict and ``comment_bucket`` is what the
    beat reporter said; ``bucket`` is the one callers should use. Keeping all
    three means a run record can show *why* a player was benched, and a
    disagreement stays visible instead of being resolved behind the scenes.
    """

    athlete_id: Optional[str]
    full_name: str
    team_abbrev: Optional[str]
    status_raw: str
    status: str
    bucket: str
    comment_bucket: Optional[str]
    comment_phrase: Optional[str]
    clamped: bool
    short_comment: str
    long_comment: str
    reported_at: Optional[datetime]
    return_date: Optional[date]
    out_for_season: bool
    note_id: Optional[str]

    @property
    def comment_matched(self) -> bool:
        """True when the beat report, not the structured status, set ``bucket``."""
        return self.comment_bucket is not None

    @property
    def disagrees(self) -> bool:
        """True when tier 2 landed somewhere other than tier 1.

        Not an error -- it is the entire point of tier 2 -- but it is what the
        run notes should surface, because it is also how a bad regex would
        first show itself.
        """
        return self.comment_bucket is not None and self.comment_bucket != self.status


def athlete_id(athlete: Mapping[str, Any]) -> Optional[str]:
    """Pull ESPN's numeric athlete id out of the player's links.

    The note itself carries no athlete id -- only ``links[].href`` does. A
    player whose links are missing or reshaped returns ``None`` rather than a
    fabricated id; :func:`parse` reports those names in its unidentified list
    so they can fall back to name-key joining through the crosswalk.
    """
    for link in athlete.get("links") or ():
        href = link.get("href") if isinstance(link, Mapping) else None
        if not href:
            continue
        m = _ATHLETE_ID_RE.search(href)
        if m:
            return m.group(1)
    return None


def classify_comment(comment: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Read a game-level verdict out of the beat reporter's sentence.

    Returns ``(bucket, matched_phrase)``, or ``(None, None)`` when the comment
    carries no verdict -- which is the common and correct outcome for practice
    updates ("went through a full practice", "participated in Sunday's
    practice"). Callers fall back to the structured status there; they must not
    interpolate a guess.
    """
    text = comment or ""
    for bucket, rx in _COMMENT_RULES:
        m = rx.search(text)
        if m:
            return bucket, m.group(0)
    return None, None


def _parse_note_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in _NOTE_DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    log.debug("espn_injuries: unparseable note date %r", raw)
    return None


def _parse_return_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        log.debug("espn_injuries: unparseable returnDate %r", raw)
        return None


def _structured_status(note: Mapping[str, Any]) -> Tuple[str, str]:
    """``(status_raw, normalised)`` from ``status``, then ``fantasyStatus``.

    ``status`` is the documented field; the fantasy abbreviation is the
    tiebreaker when ESPN ships a status string we have not seen, because it
    uses a smaller and more stable vocabulary.
    """
    raw = str(note.get("status") or "").strip()
    mapped = STATUS_MAP.get(raw.upper())
    if mapped:
        return raw, mapped
    details = note.get("details") or {}
    fantasy = (details.get("fantasyStatus") or {}).get("abbreviation")
    if fantasy:
        mapped = FANTASY_STATUS_MAP.get(str(fantasy).strip().upper())
        if mapped:
            log.debug(
                "espn_injuries: unknown status %r, using fantasyStatus %r",
                raw,
                fantasy,
            )
            return raw or str(fantasy), mapped
    # Unknown in both fields: the conservative read of an injury-page entry is
    # that the player is not reliably available, not that they are fine.
    log.warning("espn_injuries: unmappable status %r -> GTD", raw)
    return raw, "GTD"


def _apply_ceiling(status: str, comment_bucket: Optional[str]) -> Tuple[str, bool]:
    """Resolve tier 1 against tier 2. Returns ``(bucket, clamped)``."""
    if comment_bucket is None:
        return status, False
    ceiling = _CEILING.get(status, status)
    if _RANK.get(comment_bucket, 0) > _RANK.get(ceiling, 0):
        return ceiling, True
    return comment_bucket, False


def _parse_note(team: Mapping[str, Any], note: Mapping[str, Any]) -> InjuryNote:
    athlete = note.get("athlete") or {}
    details = note.get("details") or {}
    fantasy = (details.get("fantasyStatus") or {}).get("abbreviation")

    name = str(athlete.get("displayName") or "").strip()
    if not name:
        first = str(athlete.get("firstName") or "").strip()
        last = str(athlete.get("lastName") or "").strip()
        name = ("%s %s" % (first, last)).strip()

    team_abbrev = (athlete.get("team") or {}).get("abbreviation") or team.get(
        "abbreviation"
    )

    status_raw, status = _structured_status(note)
    short_comment = str(note.get("shortComment") or "")
    comment_bucket, phrase = classify_comment(short_comment)
    bucket, clamped = _apply_ceiling(status, comment_bucket)

    if clamped:
        log.info(
            "espn_injuries: %s comment said %s but status is %s -> clamped to %s "
            "(phrase %r)",
            name,
            comment_bucket,
            status,
            bucket,
            phrase,
        )
    elif comment_bucket and comment_bucket != status:
        log.info(
            "espn_injuries: %s refined %s -> %s on %r",
            name,
            status,
            bucket,
            phrase,
        )

    return InjuryNote(
        athlete_id=athlete_id(athlete),
        full_name=name,
        team_abbrev=str(team_abbrev) if team_abbrev else None,
        status_raw=status_raw,
        status=status,
        bucket=bucket,
        comment_bucket=comment_bucket,
        comment_phrase=phrase,
        clamped=clamped,
        short_comment=short_comment,
        long_comment=str(note.get("longComment") or ""),
        reported_at=_parse_note_date(note.get("date")),
        return_date=_parse_return_date(details.get("returnDate")),
        out_for_season=str(fantasy or "").strip().upper() == "OFS",
        note_id=str(note["id"]) if note.get("id") is not None else None,
    )


def parse(doc: Mapping[str, Any]) -> Tuple[List[InjuryNote], List[str]]:
    """Parse the whole page.

    Returns ``(notes, unidentified)`` where *unidentified* names the players
    whose ESPN athlete id could not be recovered from their links. Per
    ``docs/CONTRACTS.md`` section 4 that list is explicit rather than implied:
    those notes are still returned (with ``athlete_id=None``) and must be
    joined by name key through the crosswalk, never dropped.
    """
    teams = doc.get("injuries")
    if not isinstance(teams, list):
        raise SourceError(
            "espn injuries: expected a list at .injuries, got %s" % type(teams).__name__
        )

    notes: List[InjuryNote] = []
    unidentified: List[str] = []
    for team in teams:
        if not isinstance(team, Mapping):
            continue
        for raw in team.get("injuries") or ():
            if not isinstance(raw, Mapping):
                continue
            note = _parse_note(team, raw)
            if not note.full_name:
                log.warning("espn_injuries: dropping a note with no athlete name")
                continue
            notes.append(note)
            if note.athlete_id is None:
                unidentified.append(note.full_name)

    if unidentified:
        log.warning(
            "espn_injuries: %d/%d notes without an athlete id: %s",
            len(unidentified),
            len(notes),
            ", ".join(sorted(unidentified)),
        )
    return notes, unidentified


def index_by_athlete_id(notes: Sequence[InjuryNote]) -> Dict[str, InjuryNote]:
    """Athlete-id-keyed index, worst bucket wins a collision.

    A player should appear once, so a duplicate id means two notes for one
    person; taking the less available of the two is the read that cannot
    accidentally start someone who has been ruled out.
    """
    out: Dict[str, InjuryNote] = {}
    for note in notes:
        if note.athlete_id is None:
            continue
        prev = out.get(note.athlete_id)
        if prev is None or _RANK.get(note.bucket, 0) < _RANK.get(prev.bucket, 0):
            out[note.athlete_id] = note
    return out


def index_by_name(notes: Sequence[InjuryNote]) -> Dict[str, InjuryNote]:
    """Normalised-name index, for notes the athlete id could not cover."""
    from .crosswalk import normalize_name

    out: Dict[str, InjuryNote] = {}
    for note in notes:
        key = normalize_name(note.full_name)
        if not key:
            continue
        prev = out.get(key)
        if prev is None or _RANK.get(note.bucket, 0) < _RANK.get(prev.bucket, 0):
            out[key] = note
    return out


class EspnInjuries:
    """The ESPN injuries page as a :class:`~.base.Source`.

    Takes no client. This endpoint is public and this module builds a bare,
    cookie-free client for it on purpose -- the fantasy session cookies live
    in the same process, and the only reliable way to guarantee they never
    reach ``site.api.espn.com`` is to make it impossible to hand them in.
    """

    name = "espn_injuries"

    def __init__(
        self,
        max_staleness: timedelta = timedelta(hours=6),
        timeout: float = 15.0,
    ) -> None:
        self.max_staleness = max_staleness
        self.timeout = timeout
        self.last_parse: Dict[str, Any] = {}

    @guard
    def fetch(self) -> Optional[SourceResult]:
        # A fresh client per fetch, with no cookie jar to inherit.
        with http_client(timeout=self.timeout) as client:
            doc = fetch_json(
                ESPN_INJURIES_URL, client=client, timeout=self.timeout, retries=2
            )
        if not isinstance(doc, Mapping):
            raise SourceError(
                "espn injuries: expected an object, got %s" % type(doc).__name__
            )

        notes, unidentified = parse(doc)
        if not notes:
            raise SourceError("espn injuries: parsed 0 notes")

        window = self.max_staleness
        as_of = _parse_note_date(doc.get("timestamp"))
        if as_of is None:
            # The page timestamp is the weaker signal anyway; the newest note
            # is a defensible stand-in and keeps a format change from killing
            # the source outright. See FALLBACK_MAX_STALENESS for why the
            # window widens with it.
            dates = [n.reported_at for n in notes if n.reported_at]
            as_of = max(dates) if dates else None
            if as_of is not None:
                window = max(window, FALLBACK_MAX_STALENESS)
                log.warning(
                    "espn_injuries: page timestamp absent; using newest note %s "
                    "with a %s window",
                    as_of.isoformat(),
                    window,
                )
        if as_of is None:
            log.warning("espn_injuries: no parseable timestamp -> treating as stale")
            return None

        refined = sum(1 for n in notes if n.comment_matched)
        clamped = sum(1 for n in notes if n.clamped)
        self.last_parse = {
            "notes": len(notes),
            "refined_by_comment": refined,
            "clamped": clamped,
            "unidentified": list(unidentified),
        }
        log.info(
            "espn_injuries: %d notes, %d refined by beat report, %d clamped, "
            "%d without an athlete id, as_of=%s",
            len(notes),
            refined,
            clamped,
            len(unidentified),
            as_of.isoformat(),
        )

        payload = {
            "notes": notes,
            "by_athlete_id": index_by_athlete_id(notes),
            "by_name": index_by_name(notes),
            "unidentified": list(unidentified),
            "source_url": ESPN_INJURIES_URL,
            "parse": dict(self.last_parse),
        }
        return build_result(self.name, as_of, payload, window)


def main() -> int:
    """Manual probe: ``python -m sources.espn_injuries``."""
    logging.basicConfig(level=logging.INFO)
    res = EspnInjuries().fetch()
    if res is None:
        print("espn_injuries: None (unreachable or stale)")
        return 1
    print("as_of:", res.as_of.isoformat())
    for note in res.payload["notes"]:
        mark = "*" if note.comment_matched else " "
        print(
            "%s %-24s %-11s -> %-13s %s"
            % (
                mark,
                note.full_name,
                note.status,
                note.bucket,
                (note.comment_phrase or note.short_comment)[:48],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
