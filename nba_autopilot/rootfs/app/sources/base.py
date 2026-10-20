"""Shared plumbing for every ``sources/`` module.

Contract (``docs/CONTRACTS.md`` section 4):

* Every source is a class exposing ``name``, ``max_staleness`` and ``fetch()``.
* ``fetch()`` returns ``SourceResult | None``. It **never raises into the
  pipeline** -- see :func:`guard`, which converts any exception into ``None``
  plus a log line.
* Staleness is a *hard fail*: a result older than ``max_staleness`` is
  discarded (``fetch()`` returns ``None``), it is not downgraded to a warning.

Dependencies are stdlib + ``httpx`` only (ADR-001: no numpy/scipy/pandas).
``from __future__ import annotations`` keeps the PEP 604/585 annotations below
parseable on Python 3.9 (local test box) while the container runs 3.13.
"""

from __future__ import annotations

import functools
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, TypeVar, Union
from zoneinfo import ZoneInfo

import httpx

__all__ = [
    "RUN_TZ",
    "SourceError",
    "SourceResult",
    "Source",
    "guard",
    "fetch_json",
    "fetch_bytes",
    "http_client",
    "utcnow",
    "evaluate_staleness",
    "build_result",
]

log = logging.getLogger(__name__)

#: The canonical clock from CONTRACTS.md. Used only for *display* / date-key
#: resolution; all comparisons happen in UTC on tz-aware datetimes.
RUN_TZ = ZoneInfo("America/Los_Angeles")

# A residential-looking UA. ak-static.cms.nba.com and ESPN's site API both
# serve us fine without one, but DARKO sits behind a SvelteKit edge that is
# happier with a real UA, and it costs nothing to be consistent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 0.75

#: HTTP statuses worth retrying. 403 is deliberately **absent**: for the NBA
#: injury report a 403 is a clean "not published yet" probe signal, not a
#: transient error, and retrying it just wastes the backoff budget.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 408})

T = TypeVar("T")


class SourceError(RuntimeError):
    """Raised *inside* a source. :func:`guard` stops it at the boundary."""


@dataclass(frozen=True)
class SourceResult:
    """Frozen per CONTRACTS.md section 4.

    ``staleness_ok`` is retained in the record even though a stale source
    returns ``None`` from ``fetch()``: callers that build the run record want
    to show "DARKO was stale" rather than "DARKO was absent".
    """

    as_of: datetime
    payload: Any
    staleness_ok: bool


class Source(Protocol):
    """Structural type every source satisfies."""

    name: str
    max_staleness: timedelta

    def fetch(self) -> Optional[SourceResult]:
        ...


def utcnow() -> datetime:
    """Timezone-aware now. Centralised so tests can monkeypatch one symbol."""
    return datetime.now(timezone.utc)


def guard(
    method: Callable[..., Optional[T]],
) -> Callable[..., Optional[T]]:
    """Decorator: turn *any* exception into ``None`` + a log line.

    This is the hard boundary promised by the contract -- a source must never
    raise into the pipeline. Applied to ``fetch()`` implementations.
    ``KeyboardInterrupt``/``SystemExit`` are re-raised: those are operator
    intent, not source failure.
    """

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Optional[T]:
        owner = getattr(args[0], "name", None) if args else None
        label = owner or getattr(method, "__qualname__", "source")
        try:
            return method(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except httpx.HTTPError as exc:
            log.warning("source %s: transport failure: %s", label, exc)
            return None
        except SourceError as exc:
            log.warning("source %s: %s", label, exc)
            return None
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            log.exception("source %s: unexpected failure: %s", label, exc)
            return None

    return wrapper


def http_client(
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Mapping[str, str]] = None,
) -> httpx.Client:
    """A configured client. Callers own the lifetime (use as a contextmanager)."""
    merged: Dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        merged.update(headers)
    return httpx.Client(
        timeout=httpx.Timeout(timeout),
        headers=merged,
        follow_redirects=True,
    )


def _request(
    url: str,
    *,
    client: Optional[httpx.Client],
    timeout: float,
    retries: int,
    backoff: float,
    headers: Optional[Mapping[str, str]],
    ok_statuses: frozenset,
) -> httpx.Response:
    """Shared retry/backoff core for :func:`fetch_json` / :func:`fetch_bytes`.

    Retries only on :data:`RETRY_STATUSES` and transport errors, with
    exponential backoff plus jitter (so a HA restart loop does not hammer a
    recovering host in lockstep). Honours ``Retry-After`` when present.
    """
    owns = client is None
    active = client or http_client(timeout=timeout, headers=headers)
    last_exc: Optional[BaseException] = None
    try:
        for attempt in range(retries):
            try:
                resp = active.get(url, headers=dict(headers) if headers else None)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == retries - 1:
                    raise
                delay = backoff * (2**attempt) + random.uniform(0, backoff / 2)
                log.debug("GET %s transport error (%s); retry in %.2fs", url, exc, delay)
                time.sleep(delay)
                continue

            if resp.status_code in ok_statuses or resp.status_code < 400:
                return resp
            if resp.status_code in RETRY_STATUSES and attempt < retries - 1:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else -1.0
                except ValueError:
                    delay = -1.0
                if delay < 0:
                    delay = backoff * (2**attempt) + random.uniform(0, backoff / 2)
                log.debug(
                    "GET %s -> %s; retry in %.2fs", url, resp.status_code, delay
                )
                time.sleep(delay)
                continue
            return resp
        # Loop exhausted without returning: only reachable if the final
        # iteration hit `continue`, which cannot happen (guarded above).
        raise SourceError("retries exhausted for %s (%s)" % (url, last_exc))
    finally:
        if owns:
            active.close()


def fetch_json(
    url: str,
    *,
    client: Optional[httpx.Client] = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    headers: Optional[Mapping[str, str]] = None,
) -> Union[Dict[str, Any], list]:
    """GET *url* and parse JSON, with timeout/retry/backoff.

    Raises :class:`SourceError` on a non-2xx or unparseable body. Callers are
    expected to be wrapped in :func:`guard`, which converts that to ``None``.
    """
    resp = _request(
        url,
        client=client,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
        headers=headers,
        ok_statuses=frozenset(),
    )
    if resp.status_code >= 400:
        raise SourceError("GET %s -> HTTP %s" % (url, resp.status_code))
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError("GET %s returned non-JSON: %s" % (url, exc)) from exc


def fetch_bytes(
    url: str,
    *,
    client: Optional[httpx.Client] = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    headers: Optional[Mapping[str, str]] = None,
    ok_statuses: frozenset = frozenset(),
) -> httpx.Response:
    """GET *url* returning the raw response (for PDFs and probe endpoints).

    ``ok_statuses`` lets a caller treat a nominally-failing status as a real
    answer -- the injury report passes ``{403}`` because 403 means
    "not published yet", which is information rather than an error.
    """
    return _request(
        url,
        client=client,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
        headers=headers,
        ok_statuses=ok_statuses,
    )


def evaluate_staleness(as_of: datetime, max_staleness: timedelta) -> bool:
    """True when *as_of* is within *max_staleness* of now.

    A naive ``as_of`` is rejected outright: CONTRACTS.md requires tz-aware
    times, and silently assuming UTC is how a July snapshot sneaks through as
    "fresh". Future timestamps are tolerated up to one hour of clock skew.
    """
    if as_of.tzinfo is None:
        log.warning("naive as_of %s rejected (tz-aware required)", as_of)
        return False
    age = utcnow() - as_of
    if age < -timedelta(hours=1):
        log.warning("as_of %s is implausibly in the future", as_of)
        return False
    return age <= max_staleness


def build_result(
    name: str,
    as_of: datetime,
    payload: Any,
    max_staleness: timedelta,
) -> Optional[SourceResult]:
    """Assemble a :class:`SourceResult`, or ``None`` when stale.

    The single place the "staleness is a hard fail" rule is enforced, so no
    source can forget it. The log line is at WARNING and names the age, which
    is what makes the absence visible in the run record.
    """
    ok = evaluate_staleness(as_of, max_staleness)
    if not ok:
        log.warning(
            "source %s STALE: as_of=%s age=%s exceeds max_staleness=%s -> None",
            name,
            as_of.isoformat(),
            utcnow() - as_of,
            max_staleness,
        )
        return None
    return SourceResult(as_of=as_of, payload=payload, staleness_ok=True)
