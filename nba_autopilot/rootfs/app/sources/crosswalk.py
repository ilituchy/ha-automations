"""The one crosswalk: ESPN playerId <-> NBA personId <-> Sleeper id.

Per CONTRACTS.md section 4: "Joining ESPN ids to NBA ids goes through one
crosswalk module with an explicit unmatched list. Unmatched players fall back
to ESPN projections -- never to zero."

WHY THIS IS NAME-BASED (verified, and it is not a shortcut)
==========================================================
The obvious join would be Sleeper's ``espn_id`` field. **It is null for 100%
of NBA players** -- measured on the live 2026-10-20 payload: 0 of 2,176 total
and 0 of 1,886 active records carry it. ``stats_id`` is likewise null. It is an
NFL-populated field that exists but is never filled for ``sport: "nba"``.

DARKO keys on the **NBA personId** (verified: ``nba_id`` 203999 = Jokic,
1641705 = Wembanyama, matching ``/player/<id>`` routes and NBA headshot ids).
ESPN uses its own ids. Nothing in either feed carries the other's id.

So there is **no id-based path between ESPN and NBA/Sleeper ids from these
sources**, and normalised-name matching (+ team as a tiebreaker) is the only
option available without adding a dependency. That is precisely why the
``unmatched`` list is a first-class output: every name-match scheme fails on
someone, and the contract requires that failure be *visible* and route to the
ESPN-projection fallback rather than to a zero.

MATCHING STRATEGY (strict -> loose, first hit wins, all recorded)
=================================================================
1. ``exact``       -- normalised full name is unique on both sides.
2. ``team``        -- normalised name + same team, used to break a name
                      collision (e.g. two players named 'Jaylen Williams').
3. ``initial_last``-- first-initial + last name, unique on both sides.
   Catches ``Nikola Jokic`` vs ``Nikola Jokić`` leftovers, ``Cam``/``Cameron``,
   ``Herb``/``Herbert``, etc.

Deliberately **no fuzzy/edit-distance matching.** A wrong join silently feeds
one player's projection into another's lineup slot, which is worse than an
honest ``unmatched`` entry that falls back to ESPN. Ambiguous candidates are
reported as unmatched, never guessed.

Normalisation (:func:`normalize_name`) strips accents via NFKD (so ``Jokić`` ->
``jokic``, ``Dončić`` -> ``doncic`` -- both real cases in these feeds),
lowercases, **deletes** apostrophes (``D'Angelo`` -> ``dangelo``; replacing
them with a space would give ``d angelo`` and silently fail to join), drops
remaining punctuation, and removes the generational suffixes ESPN and the NBA
PDF disagree about (``Carter Jr., Wendell`` -> ``wendell carter``).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .base import SourceResult, guard, utcnow

__all__ = [
    "normalize_name",
    "initial_last",
    "PlayerLink",
    "CrosswalkResult",
    "Crosswalk",
    "TEAM_ALIASES",
]

log = logging.getLogger(__name__)

#: Suffixes that appear inconsistently across ESPN / NBA PDF / Sleeper.
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")
#: Apostrophes are *deleted*, not spaced: "D'Angelo" must normalise to
#: "dangelo" to match feeds that write "DAngelo"/"Dangelo". Replacing them
#: with a space instead yields "d angelo", which silently fails to join.
_APOSTROPHE_RE = re.compile(r"['’ʼ´`]")

#: Abbreviation drift between ESPN and the NBA PDF / Sleeper. Normalised to
#: the ESPN form on the left-hand side of each pair.
TEAM_ALIASES: Mapping[str, str] = {
    "NOP": "NO",
    "NOH": "NO",
    "NY": "NYK",
    "NYN": "NYK",
    "GS": "GSW",
    "SA": "SAS",
    "UTAH": "UTA",
    "UTH": "UTA",
    "WSH": "WAS",
    "PHO": "PHX",
    "BKN": "BKN",
    "BRK": "BKN",
    "CHO": "CHA",
    "SAC": "SAC",
}


def _strip_accents(text: str) -> str:
    """NFKD-decompose and drop combining marks. ``Jokić`` -> ``Jokic``."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_name(name: Optional[str]) -> str:
    """Canonical form for cross-source name matching.

    Lowercase, accent-stripped, punctuation-free, suffix-free, single-spaced.
    Handles the ``Last, First`` form the NBA PDF uses by flipping on the comma.

    Suffix removal is **position-independent**, which matters: the NBA PDF puts
    the suffix on either side of the comma (``Carter Jr., Wendell`` *and*
    ``Smith, Jabari Jr.``). Flipping the latter yields ``jabari jr smith``,
    where a tail-only stripper would leave ``jr`` stranded mid-string and fail
    to match ESPN's ``Jabari Smith Jr.``. Both now collapse correctly.
    A lone-token name is never stripped (so a player actually surnamed e.g.
    "V" cannot be erased).
    """
    if not name:
        return ""
    text = str(name).strip()
    if not text:
        return ""
    # NBA PDF emits 'Carter Jr., Wendell'; flip to 'Wendell Carter Jr.'
    if "," in text:
        head, _, tail = text.partition(",")
        if tail.strip():
            text = "%s %s" % (tail.strip(), head.strip())
    text = _strip_accents(text).lower()
    text = text.replace("&", " and ")
    text = _APOSTROPHE_RE.sub("", text)
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in _WS_RE.split(text) if t]
    if len(tokens) > 1:
        kept = [t for t in tokens if t not in _SUFFIXES]
        # Guard against a name that is *only* suffix tokens.
        if kept:
            tokens = kept
    return " ".join(tokens)


def initial_last(name: Optional[str]) -> str:
    """First-initial + last token, e.g. ``n jokic``. Empty when ambiguous."""
    norm = normalize_name(name)
    if not norm:
        return ""
    parts = norm.split(" ")
    if len(parts) < 2:
        return ""
    return "%s %s" % (parts[0][:1], parts[-1])


def normalize_team(abbr: Optional[str]) -> str:
    if not abbr:
        return ""
    key = str(abbr).strip().upper()
    return TEAM_ALIASES.get(key, key)


@dataclass(frozen=True)
class PlayerLink:
    """A resolved identity across the sources we actually have ids for."""

    espn_id: Optional[int]
    nba_person_id: Optional[int]
    sleeper_id: Optional[str]
    name: str
    normalized: str
    team: Optional[str]
    match_method: str


@dataclass
class CrosswalkResult:
    """Join output. ``unmatched`` is the contract-mandated explicit list."""

    links: Dict[int, PlayerLink] = field(default_factory=dict)
    by_nba_id: Dict[int, PlayerLink] = field(default_factory=dict)
    by_sleeper_id: Dict[str, PlayerLink] = field(default_factory=dict)
    #: Human-readable "name (espn_id=N): reason" strings, per the contract's
    #: ``unmatched: list[str]``.
    unmatched: List[str] = field(default_factory=list)
    unmatched_espn_ids: List[int] = field(default_factory=list)
    method_counts: Dict[str, int] = field(default_factory=dict)
    ambiguous: List[str] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return len(self.links)

    def nba_id_for(self, espn_id: int) -> Optional[int]:
        link = self.links.get(espn_id)
        return link.nba_person_id if link else None

    def sleeper_id_for(self, espn_id: int) -> Optional[str]:
        link = self.links.get(espn_id)
        return link.sleeper_id if link else None


def _build_index(
    entries: Iterable[Tuple[str, Optional[str], Any]],
) -> Tuple[Dict[str, Any], Dict[str, List[Any]], Dict[Tuple[str, str], Any], Dict[str, List[Any]]]:
    """Index a side of the join.

    Returns ``(by_name_unique, by_name_all, by_name_team, by_initial_unique)``.
    ``by_name_unique`` omits colliding keys so a collision can never silently
    resolve to an arbitrary record.
    """
    by_all: Dict[str, List[Any]] = {}
    by_team: Dict[Tuple[str, str], Any] = {}
    by_initial_all: Dict[str, List[Any]] = {}
    for name, team, obj in entries:
        norm = normalize_name(name)
        if not norm:
            continue
        by_all.setdefault(norm, []).append(obj)
        t = normalize_team(team)
        if t:
            by_team.setdefault((norm, t), obj)
        il = initial_last(name)
        if il:
            by_initial_all.setdefault(il, []).append(obj)
    by_unique = {k: v[0] for k, v in by_all.items() if len(v) == 1}
    by_initial_unique = {k: v[0] for k, v in by_initial_all.items() if len(v) == 1}
    return by_unique, by_all, by_team, by_initial_unique


class Crosswalk:
    """Builds the ESPN <-> NBA <-> Sleeper identity map.

    Not a network source -- it joins payloads the other sources produced --
    but it implements the same ``Source``-shaped surface (``name``,
    ``max_staleness``, ``fetch``) so the pipeline can treat it uniformly and
    record its ``as_of`` alongside the rest.
    """

    name = "crosswalk"

    def __init__(self, max_staleness: timedelta = timedelta(days=7)) -> None:
        self.max_staleness = max_staleness

    def build(
        self,
        espn_players: Sequence[Mapping[str, Any]],
        darko_players: Optional[Mapping[int, Any]] = None,
        sleeper_players: Optional[Mapping[str, Any]] = None,
    ) -> CrosswalkResult:
        """Join the three sides.

        ``espn_players`` is the authoritative left side: dicts with at least
        ``id``/``player_id`` and ``name``/``fullName``, optionally
        ``team``/``proTeamAbbr``. DARKO and Sleeper payloads are the dicts those
        sources put in ``payload["players"]``.
        """
        result = CrosswalkResult()

        darko_entries: List[Tuple[str, Optional[str], Any]] = []
        for nba_id, p in (darko_players or {}).items():
            nm = getattr(p, "name", None) or (p.get("player_name") if isinstance(p, Mapping) else None)
            tm = getattr(p, "team_name", None) or (p.get("team_name") if isinstance(p, Mapping) else None)
            if nm:
                darko_entries.append((nm, _team_abbr_from_name(tm), (nba_id, p)))
        d_uniq, d_all, d_team, d_init = _build_index(darko_entries)

        sleeper_entries: List[Tuple[str, Optional[str], Any]] = []
        for sid, p in (sleeper_players or {}).items():
            nm = getattr(p, "full_name", None) or (p.get("full_name") if isinstance(p, Mapping) else None)
            tm = getattr(p, "team", None) or (p.get("team") if isinstance(p, Mapping) else None)
            if nm:
                sleeper_entries.append((nm, tm, (sid, p)))
        s_uniq, s_all, s_team, s_init = _build_index(sleeper_entries)

        for rec in espn_players:
            espn_id = _coerce_espn_id(rec)
            raw_name = (
                rec.get("name")
                or rec.get("fullName")
                or rec.get("full_name")
                or rec.get("player_name")
            )
            norm = normalize_name(raw_name if isinstance(raw_name, str) else None)
            team = normalize_team(
                rec.get("team") or rec.get("proTeamAbbr") or rec.get("team_abbr")
            )
            if espn_id is None or not norm:
                label = "%s (espn_id=%s)" % (raw_name or "<unnamed>", espn_id)
                result.unmatched.append("%s: missing id or name" % label)
                if espn_id is not None:
                    result.unmatched_espn_ids.append(espn_id)
                continue

            nba_hit, nba_method = _resolve(norm, team, raw_name, d_uniq, d_all, d_team, d_init)
            slp_hit, slp_method = _resolve(norm, team, raw_name, s_uniq, s_all, s_team, s_init)

            nba_person_id: Optional[int] = None
            if nba_hit is not None:
                nba_person_id = _as_int(nba_hit[0])
            sleeper_id: Optional[str] = None
            if slp_hit is not None:
                sleeper_id = str(slp_hit[0])

            if nba_person_id is None and sleeper_id is None:
                reason = "no DARKO or Sleeper match"
                if norm in d_all and len(d_all[norm]) > 1:
                    reason = "ambiguous DARKO name (%d candidates)" % len(d_all[norm])
                    result.ambiguous.append("%s (espn_id=%s)" % (raw_name, espn_id))
                elif norm in s_all and len(s_all[norm]) > 1:
                    reason = "ambiguous Sleeper name (%d candidates)" % len(s_all[norm])
                    result.ambiguous.append("%s (espn_id=%s)" % (raw_name, espn_id))
                result.unmatched.append("%s (espn_id=%d): %s" % (raw_name, espn_id, reason))
                result.unmatched_espn_ids.append(espn_id)
                continue

            method = nba_method or slp_method or "none"
            link = PlayerLink(
                espn_id=espn_id,
                nba_person_id=nba_person_id,
                sleeper_id=sleeper_id,
                name=str(raw_name),
                normalized=norm,
                team=team or None,
                match_method=method,
            )
            result.links[espn_id] = link
            if nba_person_id is not None:
                result.by_nba_id[nba_person_id] = link
            if sleeper_id is not None:
                result.by_sleeper_id[sleeper_id] = link
            result.method_counts[method] = result.method_counts.get(method, 0) + 1

        log.info(
            "crosswalk: matched %d, unmatched %d, methods=%s",
            result.matched_count,
            len(result.unmatched),
            sorted(result.method_counts.items()),
        )
        if result.unmatched:
            log.info(
                "crosswalk unmatched (fall back to ESPN projections, never zero): %s",
                result.unmatched[:20],
            )
        return result

    @guard
    def fetch(
        self,
        espn_players: Optional[Sequence[Mapping[str, Any]]] = None,
        darko_players: Optional[Mapping[int, Any]] = None,
        sleeper_players: Optional[Mapping[str, Any]] = None,
    ) -> Optional[SourceResult]:
        """``Source``-shaped wrapper. ``as_of`` is build time (pure join)."""
        if not espn_players:
            log.warning("crosswalk: no ESPN players supplied")
            return None
        res = self.build(espn_players, darko_players, sleeper_players)
        return SourceResult(as_of=utcnow(), payload=res, staleness_ok=True)


def _resolve(
    norm: str,
    team: str,
    raw_name: Any,
    by_unique: Mapping[str, Any],
    by_all: Mapping[str, List[Any]],
    by_team: Mapping[Tuple[str, str], Any],
    by_initial: Mapping[str, Any],
) -> Tuple[Optional[Any], Optional[str]]:
    """Strict-to-loose resolution. Returns ``(hit, method)``."""
    hit = by_unique.get(norm)
    if hit is not None:
        return hit, "exact"
    if team:
        hit = by_team.get((norm, team))
        if hit is not None:
            return hit, "team"
    # Only fall to initial+last when the full name was not merely ambiguous.
    if norm not in by_all:
        il = initial_last(raw_name)
        if il:
            hit = by_initial.get(il)
            if hit is not None:
                return hit, "initial_last"
    return None, None


_TEAM_NAME_TO_ABBR: Mapping[str, str] = {
    "atlanta hawks": "ATL",
    "boston celtics": "BOS",
    "brooklyn nets": "BKN",
    "charlotte hornets": "CHA",
    "chicago bulls": "CHI",
    "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL",
    "denver nuggets": "DEN",
    "detroit pistons": "DET",
    "golden state warriors": "GSW",
    "houston rockets": "HOU",
    "indiana pacers": "IND",
    "la clippers": "LAC",
    "los angeles clippers": "LAC",
    "los angeles lakers": "LAL",
    "memphis grizzlies": "MEM",
    "miami heat": "MIA",
    "milwaukee bucks": "MIL",
    "minnesota timberwolves": "MIN",
    "new orleans pelicans": "NO",
    "new york knicks": "NYK",
    "oklahoma city thunder": "OKC",
    "orlando magic": "ORL",
    "philadelphia 76ers": "PHI",
    "phoenix suns": "PHX",
    "portland trail blazers": "POR",
    "sacramento kings": "SAC",
    "san antonio spurs": "SAS",
    "toronto raptors": "TOR",
    "utah jazz": "UTA",
    "washington wizards": "WAS",
}


def _team_abbr_from_name(team_name: Any) -> Optional[str]:
    """DARKO carries full team names ('San Antonio Spurs'); map to an abbr."""
    if not isinstance(team_name, str) or not team_name.strip():
        return None
    return _TEAM_NAME_TO_ABBR.get(team_name.strip().lower())


def _coerce_espn_id(rec: Mapping[str, Any]) -> Optional[int]:
    for key in ("id", "player_id", "playerId", "espn_id"):
        if key in rec:
            v = rec[key]
            if v is None or isinstance(v, bool):
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _as_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
