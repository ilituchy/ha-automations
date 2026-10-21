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
from zoneinfo import ZoneInfo

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

#: The slot id ESPN hangs a league-wide ``lineupSlotStatLimits`` entry on. It is
#: named "Rookie" in the position map and is *not* a usable lineup slot in either
#: observed league -- it appears nowhere in ``lineupSlotCounts``. Treating the
#: entry as a cap on a Rookie slot would make it inert; treating it as a team
#: total is what the numbers support, because the value scales with the number of
#: starting slots: the 10-starter league reports ``40/7`` and the 11-starter
#: league ``44/7``, i.e. exactly four games per starting slot per matchup period
#: in both. A cap on one slot would not track another league's starter count.
TEAM_TOTAL_LIMIT_SLOT: SlotId = 15

#: The divisor ESPN applies to ``lineupSlotStatLimits``
#: (``matchupLimitPerScoringPeriod: true``), which is why those values arrive as
#: repeating decimals rather than the round numbers on the settings page.
#: Multiply by this to recover the per-matchup cap: ``40/7 * 7 = 40``.
#:
#: It is a **fixed divisor, not the length of any particular matchup period.**
#: Matchup periods are Monday-Sunday weeks, so a season that opens on a Tuesday
#: has a six-day first week -- and its cap is still 40, because the cap is per
#: matchup period. Reading this constant as "days in this week" and recomputing
#: from it would shrink the opening week's budget by a seventh.
SCORING_PERIODS_PER_MATCHUP: int = 7

#: Timezone to resolve a tipoff instant into the US game date ESPN groups by.
#:
#: Load-bearing, and the UTC default is wrong: a 19:30 Pacific tipoff is already
#: past midnight UTC, so a UTC date pushes every west-coast late game onto the
#: following day. Verified across all 156 game days of the 2026-27 season --
#: in Eastern, every scoring period resolves to exactly one date; in UTC, around
#: thirty of them straddle two.
GAME_DATE_TZ = ZoneInfo("America/New_York")

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
    26: "APG",
    27: "BPG",
    28: "MPG",
    29: "PPG",
    30: "RPG",
    31: "SPG",
    32: "TOPG",
    33: "3PG",
    34: "PPM",
    35: "A/TO",
    36: "STR",
    37: "DD",
    38: "TD",
    39: "QD",
    40: "MIN",
    41: "GS",
    42: "GP",
    43: "TW",
    44: "FTR",
}

#: Stats that are **not additive across games**: percentages, per-game averages
#: and ratios. The whole points-league value function is a dot product of
#: projected per-game stats against ``scoringItems``, which is only valid for
#: counting stats -- summing a per-game average over a 4-game week gives 4x the
#: real figure, and summing FG% is meaningless at any horizon.
#:
#: Neither observed league scores on any of these (all 17 of their items are
#: counting stats), so this exists to make a league that *does* fail loudly in
#: ``solve/value.py`` rather than quietly mis-rank every player.
NON_ADDITIVE_STATS: FrozenSet[StatId] = frozenset(
    {19, 20, 21, 22}  # FG% FT% 3P% AFG%
    | {26, 27, 28, 29, 30, 31, 32, 33}  # per-game averages
    | {34, 35, 36, 44}  # PPM, A/TO, STR, FTR
)

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
