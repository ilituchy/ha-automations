"""Official NBA injury report PDF (``ak-static.cms.nba.com``).

VERIFIED LIVE 2026-10-20
========================
Host reachability: ``ak-static.cms.nba.com`` resolves via
``ak-static.cms.nba.com.edgekey.net -> e12399.dsce2.akamaiedge.net`` and
**answers normally**. It is a *different property* from ``cdn.nba.com`` /
``stats.nba.com`` (which are Akamai-403/blackholed from cloud egress) --
confirmed working from this machine.

URL format (confirmed by fetching real files):
    https://ak-static.cms.nba.com/referee/injury/Injury-Report_YYYY-MM-DD_HHPM.pdf

Historical probes all returned **HTTP 200** with valid PDFs, proving the path
template is correct:
    Injury-Report_2025-10-20_01PM.pdf  -> 200, 57,432 B, 1 page
    Injury-Report_2024-11-15_05PM.pdf  -> 200, 88,619 B
    Injury-Report_2024-01-15_01PM.pdf  -> 200, 83,289 B
    Injury-Report_2023-12-01_05PM.pdf  -> 200, 99,629 B, 9 pages

**Today (2026-10-20) every candidate timestamp returned 403.** The 403 body is
an S3 ``<Error><Code>AccessDenied</Code>`` document, *not* an Akamai WAF
challenge -- i.e. the object does not exist at that key yet. That is exactly
the "403 = not yet published" clean probe signal CONTRACTS.md describes, and
it is why 403 is excluded from :data:`~.base.RETRY_STATUSES`. A browser
User-Agent + nba.com Referer made no difference (still 403), which further
confirms it is key-absence rather than blocking.

WHY ``pypdf``
=============
Justified per the dependency constraint: ``pypdf`` is **pure Python** (no C
extension, no wheel/ABI concerns on aarch64 -- it installs from the universal
``py3-none-any`` wheel on a HA Green), has no transitive deps on 3.13, and is
the only non-stdlib addition. The alternatives were worse: ``pdfplumber``
pulls ``pdfminer.six`` + ``Pillow`` (C extensions), ``PyMuPDF`` is a compiled
MuPDF binding. There is no stdlib PDF reader, and the report is published only
as a PDF, so a parser is unavoidable. Import is **lazy** (inside the parse
function) so the rest of the package works even if pypdf is missing.

TEXT EXTRACTION
===============
pypdf emits one token per line -- extraction is extremely ragged:

    'Injury\\nReport:\\n10/20/25\\n01:30\\nPM\\nPage\\n1\\nof\\n1\\nGame\\nDate...'

So ``re.sub(r"\\s+", " ", text)`` normalisation is **mandatory** before any
parsing, per the brief. Normalised, the same text reads:

    'Injury Report: 12/01/23 05:30 PM Page 1 of 9 Game Date Game Time Matchup
     Team Player Name Current Status Reason 12/01/2023 07:00 (ET) WAS@ORL
     Washington Wizards Rollins, Ryan Out Injury/Illness - Right Knee; ...'

Row grammar (verified against the 2023-12-01 report, 9 pages):
``Last, First <Status> <Reason>`` where Status is one of
Available / Probable / Questionable / Doubtful / Out. The regex in
:data:`_ROW_RE` matched **126 of 126** status tokens on that file
(89 Out, 17 Questionable, 9 Available, 9 Probable, 2 Doubtful), plus 14
``NOT YET SUBMITTED`` team placeholders which are intentionally skipped.
Names include suffix forms (``Carter Jr., Wendell``) and mononym-ish
first names (``Jackson, GG``).

The header ``Injury Report: MM/DD/YY HH:MM PM`` is the authoritative
``as_of`` -- it is the report's own publication time (Eastern).
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import httpx

from .base import (
    RUN_TZ,
    SourceError,
    SourceResult,
    build_result,
    fetch_bytes,
    guard,
    http_client,
    utcnow,
)

__all__ = [
    "INJURY_URL_TEMPLATE",
    "INJURY_STATUS_MAP",
    "InjuryEntry",
    "InjuryReport",
    "candidate_urls",
    "normalize_pdf_text",
    "parse_report_text",
]

log = logging.getLogger(__name__)

INJURY_URL_TEMPLATE = (
    "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{hour}.pdf"
)

ET = ZoneInfo("America/New_York")

#: The NBA publishes on a fixed Eastern cadence. Ordered newest-first within a
#: day so the first 200 we hit is the freshest available report.
PUBLISH_HOURS: Tuple[str, ...] = (
    "08PM",
    "07PM",
    "06PM",
    "05PM",
    "04PM",
    "03PM",
    "02PM",
    "01PM",
    "12PM",
    "11AM",
    "10AM",
    "09AM",
    "08AM",
    "07AM",
    "06AM",
    "05AM",
    "01AM",
)

#: PDF vocabulary -> CONTRACTS.md section 1 normalised set.
INJURY_STATUS_MAP: Dict[str, str] = {
    "AVAILABLE": "ACTIVE",
    "PROBABLE": "GTD",
    "QUESTIONABLE": "GTD",
    "DOUBTFUL": "OUT",
    "OUT": "OUT",
}

_STATUSES = ("Available", "Probable", "Questionable", "Doubtful", "Out")

# 'Last, First' then a status token. Handles suffixes (Jr./Sr./II/III/IV) on the
# surname side and 1-2 token given names. Non-greedy on the reason so the next
# row's name is not swallowed.
_NAME = r"[A-Z][A-Za-z'’.\-]*(?:\s(?:Jr\.|Sr\.|II|III|IV|V))?"
_GIVEN = r"[A-Z][A-Za-z'’.\-]*(?:\s[A-Z][A-Za-z'’.\-]*)?"
_ROW_RE = re.compile(
    r"(?P<last>" + _NAME + r"),\s(?P<first>" + _GIVEN + r")\s+"
    r"(?P<status>" + "|".join(_STATUSES) + r")\b"
    r"(?P<reason>.*?)"
    r"(?=(?:" + _NAME + r",\s" + _GIVEN + r"\s+(?:" + "|".join(_STATUSES) + r")\b)"
    r"|(?:\d{2}/\d{2}/\d{4})|(?:\d{2}:\d{2}\s*\(ET\))|(?:NOT YET SUBMITTED)|$)",
    re.UNICODE,
)

_HEADER_RE = re.compile(
    r"Injury\s+Report:?\s+(\d{2})/(\d{2})/(\d{2,4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)

_GAME_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_MATCHUP_RE = re.compile(r"\b([A-Z]{3})@([A-Z]{3})\b")


@dataclass(frozen=True)
class InjuryEntry:
    """One player row from the report."""

    last_name: str
    first_name: str
    full_name: str
    status_raw: str
    status: str
    reason: str
    game_date: Optional[str]
    matchup: Optional[str]


def normalize_pdf_text(text: str) -> str:
    """Collapse all whitespace runs to single spaces.

    Mandatory: pypdf's extraction on these files is one-token-per-line, so
    every multi-word cell arrives newline-separated.
    """
    return re.sub(r"\s+", " ", text).strip()


def _extract_text(data: bytes) -> str:
    """Extract and normalise text from the PDF bytes. ``pypdf`` imported lazily."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise SourceError(
            "pypdf is required to parse the NBA injury report PDF (pure-Python, "
            "aarch64-safe); install it or disable this source"
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises assorted types
        raise SourceError("could not read injury report PDF: %s" % exc) from exc
    return normalize_pdf_text(" ".join(pages))


def _parse_header_as_of(text: str) -> Optional[datetime]:
    """Parse ``Injury Report: 10/20/25 01:30 PM`` (Eastern) into UTC."""
    m = _HEADER_RE.search(text)
    if not m:
        return None
    mm, dd, yy, hh, mi, ampm = m.groups()
    year = int(yy)
    if year < 100:
        year += 2000
    hour = int(hh) % 12
    if ampm.upper() == "PM":
        hour += 12
    try:
        local = datetime(year, int(mm), int(dd), hour, int(mi), tzinfo=ET)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def parse_report_text(text: str) -> Dict[str, Any]:
    """Parse normalised report text into entries + metadata.

    ``game_date`` / ``matchup`` are carried forward from the most recent
    occurrence, mirroring the PDF's layout where those cells are only printed
    on the first row of each group.
    """
    entries: List[InjuryEntry] = []
    cur_date: Optional[str] = None
    cur_matchup: Optional[str] = None

    # Walk the text, tracking group context by position.
    markers: List[Tuple[int, str, str]] = []
    for m in _GAME_DATE_RE.finditer(text):
        markers.append((m.start(), "date", m.group(1)))
    for m in _MATCHUP_RE.finditer(text):
        markers.append((m.start(), "matchup", "%s@%s" % (m.group(1), m.group(2))))
    markers.sort(key=lambda t: t[0])

    rows = list(_ROW_RE.finditer(text))
    mi = 0
    for row in rows:
        while mi < len(markers) and markers[mi][0] < row.start():
            kind, val = markers[mi][1], markers[mi][2]
            if kind == "date":
                cur_date = val
            else:
                cur_matchup = val
            mi += 1
        last = row.group("last").strip()
        first = row.group("first").strip()
        status_raw = row.group("status").strip()
        reason = normalize_pdf_text(row.group("reason") or "")
        entries.append(
            InjuryEntry(
                last_name=last,
                first_name=first,
                full_name="%s %s" % (first, last),
                status_raw=status_raw,
                status=INJURY_STATUS_MAP.get(status_raw.upper(), "GTD"),
                reason=reason,
                game_date=cur_date,
                matchup=cur_matchup,
            )
        )

    not_submitted = len(re.findall(r"NOT YET SUBMITTED", text))
    return {
        "entries": entries,
        "as_of_header": _parse_header_as_of(text),
        "not_yet_submitted": not_submitted,
        "entry_count": len(entries),
    }


def candidate_urls(
    now: Optional[datetime] = None,
    days_back: int = 2,
    hours: Sequence[str] = PUBLISH_HOURS,
) -> List[str]:
    """Enumerate candidate URLs, newest-first.

    The filename embeds an Eastern publication timestamp, and there is no index
    to list, so finding "the current one" means probing. Ordering is
    newest-first and we stop at the first 200, so the common case is 1-2
    requests. 403s are cheap (263-byte S3 error docs).
    """
    ref = (now or utcnow()).astimezone(ET)
    out: List[str] = []
    for delta in range(0, max(1, days_back) + 1):
        day = (ref - timedelta(days=delta)).date()
        for hour in hours:
            out.append(
                INJURY_URL_TEMPLATE.format(date=day.strftime("%Y-%m-%d"), hour=hour)
            )
    return out


class InjuryReport:
    """Official injury report source.

    ``fetch()`` returns ``None`` when no report is published yet (all 403) --
    an expected, non-error condition early in the day. ``last_probe`` records
    what happened so the run record can distinguish "not published" from
    "host unreachable", which matter very differently.
    """

    name = "injury_report"

    def __init__(
        self,
        max_staleness: timedelta = timedelta(hours=18),
        client: Optional[httpx.Client] = None,
        days_back: int = 2,
    ) -> None:
        self.max_staleness = max_staleness
        self._client = client
        self.days_back = days_back
        self.last_probe: Dict[str, Any] = {}

    @guard
    def fetch(self) -> Optional[SourceResult]:
        urls = candidate_urls(days_back=self.days_back)
        probed = 0
        unpublished = 0
        transport_errors = 0
        found: Optional[Tuple[str, bytes]] = None

        for url in urls:
            probed += 1
            try:
                # 403 is a real answer ("not published"), so allow it through
                # without retrying. Only 1 retry: we have many candidates.
                resp = fetch_bytes(
                    url,
                    client=self._client,
                    timeout=25.0,
                    retries=2,
                    ok_statuses=frozenset({403, 404}),
                )
            except httpx.HTTPError as exc:
                transport_errors += 1
                log.debug("injury report probe %s: transport error %s", url, exc)
                continue
            if resp.status_code == 200 and resp.content[:4] == b"%PDF":
                found = (url, resp.content)
                break
            if resp.status_code in (403, 404):
                unpublished += 1
                continue
            log.debug("injury report probe %s -> HTTP %s", url, resp.status_code)

        self.last_probe = {
            "probed": probed,
            "unpublished_403_404": unpublished,
            "transport_errors": transport_errors,
            "found_url": found[0] if found else None,
        }

        if found is None:
            if transport_errors and not unpublished:
                raise SourceError(
                    "injury report: host unreachable (%d transport errors, %d probes)"
                    % (transport_errors, probed)
                )
            log.info(
                "injury report: not published yet (%d/%d probes returned 403/404)",
                unpublished,
                probed,
            )
            return None

        url, data = found
        text = _extract_text(data)
        parsed = parse_report_text(text)
        as_of = parsed["as_of_header"]
        if as_of is None:
            # Fall back to the timestamp embedded in the filename we matched.
            as_of = _as_of_from_url(url)
        if as_of is None:
            log.warning("injury report %s: no parseable timestamp -> stale", url)
            return None

        entries: List[InjuryEntry] = parsed["entries"]
        if not entries and parsed["not_yet_submitted"] == 0:
            raise SourceError("injury report %s parsed 0 rows and 0 placeholders" % url)

        log.info(
            "injury report %s: %d entries, %d team placeholders, as_of=%s",
            url,
            len(entries),
            parsed["not_yet_submitted"],
            as_of.isoformat(),
        )
        payload = {
            "entries": entries,
            "by_name": _index_entries(entries),
            "not_yet_submitted": parsed["not_yet_submitted"],
            "source_url": url,
            "probe": dict(self.last_probe),
        }
        return build_result(self.name, as_of, payload, self.max_staleness)


def _as_of_from_url(url: str) -> Optional[datetime]:
    m = re.search(r"Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})(AM|PM)\.pdf", url)
    if not m:
        return None
    day, hh, ampm = m.groups()
    hour = int(hh) % 12
    if ampm == "PM":
        hour += 12
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
        return datetime(d.year, d.month, d.day, hour, 30, tzinfo=ET).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _index_entries(entries: Sequence[InjuryEntry]) -> Dict[str, InjuryEntry]:
    """Name-keyed index. Later (more severe) rows do not overwrite earlier ones.

    A player appears once per report, so a collision means two different people
    normalising to the same key -- logged, first wins.
    """
    from .crosswalk import normalize_name

    out: Dict[str, InjuryEntry] = {}
    for e in entries:
        key = normalize_name(e.full_name)
        if not key:
            continue
        if key in out:
            log.debug("injury report: duplicate name key %r", key)
            continue
        out[key] = e
    return out


def main() -> int:
    """Manual probe: ``python -m sources.injury_report``."""
    logging.basicConfig(level=logging.INFO)
    with http_client() as c:
        src = InjuryReport(client=c)
        res = src.fetch()
        print("probe:", src.last_probe)
        if res is None:
            print("injury_report: None (not published or stale)")
            return 1
        print("as_of:", res.as_of.isoformat())
        for e in res.payload["entries"][:10]:
            print("  ", e.full_name, "|", e.status_raw, "->", e.status, "|", e.reason[:50])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
