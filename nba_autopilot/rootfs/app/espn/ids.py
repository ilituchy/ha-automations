"""ESPN ``fba`` identifier maps.

Sourced from the ``cwendt94/espn-api`` basketball constants (990-star library,
actively maintained) and cross-checked against live league responses.

Design rule: **the optimizer never consults these maps.** Slot eligibility comes
from each player's own ``eligibleSlots`` array and slot capacity from
``settings.rosterSettings.lineupSlotCounts``, both keyed by raw integer id. These
names exist for human-readable output and for the two genuinely semantic facts
below (which slots do not score, and which stat is the GP counter). Keeping the
semantic surface this small is deliberate: it is the part that can silently rot
when ESPN changes something, so there is as little of it as possible.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, FrozenSet, Optional

SlotId = int
StatId = int
ProTeamId = int

GAME = "fba"
READ_HOST = "https://lm-api-reads.fantasy.espn.com"
WRITE_HOST = "https://lm-api-writes.fantasy.espn.com"

SLOT_NAMES: Dict[SlotId, str] = {
    0: "PG",
    1: "SG",
    2: "SF",
    3: "PF",
    4: "C",
    5: "G",
    6: "F",
    7: "SG/SF",
    8: "G/F",
    9: "PF/C",
    10: "F/C",
    11: "UTIL",
    12: "BE",
    13: "IR",
    15: "Rookie",
}

#: Slots that produce no fantasy scoring. This is one of only two semantic
#: assumptions in this module -- ESPN does not flag it in the payload, so it
#: cannot be read. If ESPN ever adds a slot type, the symptom is a lineup that
#: "optimizes" players onto the bench, which the preflight bench-count check in
#: ``espn/write.py`` is there to catch.
BENCH_SLOT: SlotId = 12
IR_SLOT: SlotId = 13
NON_SCORING_SLOTS: FrozenSet[SlotId] = frozenset({BENCH_SLOT, IR_SLOT})

STAT_NAMES: Dict[StatId, str] = {
    0: "PTS",
    1: "BLK",
    2: "STL",
    3: "AST",
    4: "OREB",
    5: "DREB",
    6: "REB",
    7: "EJ",
    8: "FF",
    9: "PF",
    10: "TF",
    11: "TO",
    12: "DQ",
    13: "FGM",
    14: "FGA",
    15: "FTM",
    16: "FTA",
    17: "3PM",
    18: "3PA",
    19: "FG%",
    20: "FT%",
    21: "3P%",
    22: "AFG%",
    23: "FGMI",
    24: "FTMI",
    25: "3PMI",
    37: "DD",
    38: "TD",
    40: "MIN",
    41: "GS",
    42: "GP",
}

PTS, BLK, STL, AST, REB = 0, 1, 2, 3, 6
TO, FGM, FGA, FTM, FTA, TPM = 11, 13, 14, 15, 16, 17
FG_PCT, FT_PCT, TP_PCT = 19, 20, 21
MINUTES, GAMES_STARTED, GAMES_PLAYED = 40, 41, 42

#: The GP counter. The second and last semantic assumption here: this is the
#: stat id that ``lineupSlotStatLimits`` caps, and the one to read out of
#: ``cumulativeScore.statBySlot``.
GP_STAT: StatId = GAMES_PLAYED

#: Categories where a lower total wins the category.
LOWER_IS_BETTER: FrozenSet[StatId] = frozenset({TO, 9, 7, 8, 10, 12, 23, 24, 25})

#: Percentage categories and their (made, attempted) components. These cannot be
#: summed across players, so ``solve/weights.py`` routes them through the
#: ratio chain rule instead.
RATIO_COMPONENTS: Dict[StatId, "tuple"] = {
    FG_PCT: (FGM, FGA),
    FT_PCT: (FTM, FTA),
    TP_PCT: (TPM, 18),
}

NINE_CAT: FrozenSet[StatId] = frozenset(
    {PTS, REB, AST, STL, BLK, TPM, TO, FG_PCT, FT_PCT}
)

PRO_TEAMS: Dict[ProTeamId, str] = {
    0: "FA",
    1: "ATL",
    2: "BOS",
    3: "NOP",
    4: "CHI",
    5: "CLE",
    6: "DAL",
    7: "DEN",
    8: "DET",
    9: "GSW",
    10: "HOU",
    11: "IND",
    12: "LAC",
    13: "LAL",
    14: "MIA",
    15: "MIL",
    16: "MIN",
    17: "BKN",
    18: "NYK",
    19: "ORL",
    20: "PHI",
    21: "PHX",
    22: "POR",
    23: "SAC",
    24: "SAS",
    25: "OKC",
    26: "UTA",
    27: "WAS",
    28: "TOR",
    29: "MEM",
    30: "CHA",
}

#: Stat-split keys are ``f"{statSourceId}{statSplitTypeId}"``.
#:
#: ``statSplitTypeId`` is **game-specific** and the football convention
#: (4/5/6 = last 7/15/30) is wrong for basketball. Verified against a live
#: ``fba`` league by games-played counts on each split: 3 -> 7 -> 14 -> 73,
#: which only lines up with 1/2/3.
SPLIT_SEASON_TOTAL = "00"
SPLIT_SEASON_PROJECTED = "10"
SPLIT_LAST_7 = "01"
SPLIT_LAST_15 = "02"
SPLIT_LAST_30 = "03"

#: ESPN's roster feed emits only these three. There is deliberately no
#: ``QUESTIONABLE`` here -- ESPN does not publish that tier, which is the entire
#: reason ``sources/sleeper.py`` and the official injury report exist. Mapping
#: ESPN's ``DAY_TO_DAY`` onto "questionable" would invent precision we do not
#: have.
INJURY_NORMALIZE: Dict[str, str] = {
    "ACTIVE": "ACTIVE",
    "NORMAL": "ACTIVE",
    "PLAYER_INFO_UNDEFINED": "ACTIVE",
    "DAY_TO_DAY": "DAY_TO_DAY",
    "OUT": "OUT",
    "SUSPENSION": "OUT",
    "INJURY_RESERVE": "OUT",
    "FIFTEEN_DAY_DL": "OUT",
    "SIXTY_DAY_DL": "OUT",
    "SEVEN_DAY_DL": "OUT",
}

#: ``executionType`` on a transaction. The enum is exactly these two: a prior
#: investigation of a supposed ``VALIDATE``/``FULL_PROJECTION`` dry-run found it
#: does not exist (``VALIDATE`` returns ``400 {"messages":["Invalid Input."]}``
#: with no ``details[]``). ``FULL_PROJECTION`` is a read-side stat-split concept
#: that someone conflated with a write mode. There is no dry run; see
#: ``docs/PREFLIGHT.md`` for what we do instead.
EXEC_EXECUTE = "EXECUTE"
EXEC_CANCEL = "CANCEL"


def slot_name(slot: SlotId) -> str:
    return SLOT_NAMES.get(slot, "slot%d" % slot)


def stat_name(stat: StatId) -> str:
    return STAT_NAMES.get(stat, "stat%d" % stat)


def pro_team(team: ProTeamId) -> str:
    return PRO_TEAMS.get(team, "T%d" % team)


def is_starting_slot(slot: SlotId) -> bool:
    return slot not in NON_SCORING_SLOTS


def season_id(today: Optional[date] = None) -> int:
    """ESPN labels a season by the calendar year it *ends* in.

    The 2026-27 NBA season is ``seasonId=2027``. The rollover sits in the
    offseason, so August is a safe cut point. Corroborated by DARKO's payload
    reporting ``season: 2027`` in October 2026.
    """
    d = today or date.today()
    return d.year + 1 if d.month >= 8 else d.year
