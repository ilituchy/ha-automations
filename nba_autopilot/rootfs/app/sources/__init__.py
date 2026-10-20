"""Projection + availability inputs (CONTRACTS.md section 4).

Every source exposes ``name``, ``max_staleness`` and ``fetch() -> SourceResult
| None``. A source that fails -- unreachable, unparseable, or **stale** --
returns ``None`` and logs; it never raises into the pipeline. Its absence must
be visible in the run record, never silently defaulted.

Live-verification status as of 2026-10-20 (details in each module's docstring):

* ``nba_schedule`` -- OK. ESPN site API scoreboard, 200. Primary.
* ``sleeper``      -- OK. 200, ~2.5 MB, unauthenticated. Real ``GTD`` tier.
* ``injury_report``-- Host reachable; today's report not yet published (403 =
  clean probe signal). Parser validated against real historical PDFs.
* ``darko``        -- Reachable and decodable, but publishes a **June
  offseason snapshot** (``ratingsThrough=2026-06-13``, ``asOf`` null,
  ``season_games=0`` for all 518 rows). Per the contract this is a hard fail,
  so ``Darko.fetch()`` returns ``None`` and the **ESPN ``statSourceId=1``
  fallback is the live projection path today**.

The sub-imports are lazy-friendly: importing this package does not require
``pypdf`` (only :mod:`sources.injury_report` parsing does, and it imports
lazily).
"""

from __future__ import annotations

from .base import (
    RUN_TZ,
    Source,
    SourceError,
    SourceResult,
    build_result,
    evaluate_staleness,
    fetch_bytes,
    fetch_json,
    guard,
    http_client,
    utcnow,
)
from .crosswalk import Crosswalk, CrosswalkResult, PlayerLink, normalize_name
from .darko import Darko, DarkoPlayer, decode_sveltekit_data, decode_sveltekit_payload
from .injury_report import InjuryEntry, InjuryReport, normalize_pdf_text
from .nba_schedule import GameRef, NbaSchedule, detect_back_to_backs
from .sleeper import Sleeper, SleeperPlayer, normalize_status

__all__ = [
    # protocol + shared types
    "Source",
    "SourceResult",
    "SourceError",
    "RUN_TZ",
    # shared helpers
    "fetch_json",
    "fetch_bytes",
    "http_client",
    "guard",
    "build_result",
    "evaluate_staleness",
    "utcnow",
    # sources
    "NbaSchedule",
    "GameRef",
    "detect_back_to_backs",
    "Darko",
    "DarkoPlayer",
    "decode_sveltekit_data",
    "decode_sveltekit_payload",
    "Sleeper",
    "SleeperPlayer",
    "normalize_status",
    "InjuryReport",
    "InjuryEntry",
    "normalize_pdf_text",
    "Crosswalk",
    "CrosswalkResult",
    "PlayerLink",
    "normalize_name",
]
