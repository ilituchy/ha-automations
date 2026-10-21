"""App configuration, read from Supervisor's ``/data/options.json``.

Supervisor materializes the user's Configuration tab into that file, so we read
it directly rather than calling back through ``bashio``/the Supervisor API --
one less dependency on the network being up at boot. Environment variables
override, which is what makes local development possible without a Supervisor.

Secrets note, surfaced in the UI text too: HA ``password``-typed options are
masked in the frontend but stored **plaintext on disk** and included
**plaintext in HA backups**. Backup encryption is the user's job and the docs
say so.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from espn import ids
from espn.read import EspnConfig
from solve import addrop

log = logging.getLogger(__name__)

OPTIONS_PATH = os.environ.get("AUTOPILOT_OPTIONS", "/data/options.json")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeagueRef:
    """One team in one league.

    A league is the unit of optimization, not the account: category weights,
    the games-played budget and the roster are all league-local, and a move
    that is right in one league is unrelated to the other. Nothing is ever
    pooled across leagues except the ESPN cookie and the notification channel.
    """

    league_id: int
    team_id: int
    label: str
    #: Per-league override of the global ``season``. ``0`` means "use the
    #: global value". Exists because a wrong season fails *quietly*: ESPN
    #: answers every request for a finished season perfectly happily, just with
    #: final standings. Probing a live league with the previous season's id
    #: returned ``latestScoringPeriod: 175`` and matchup 22 of 22, which looks
    #: like a plausible late-season state rather than like an error. Two
    #: leagues can also legitimately diverge -- a keeper league rolled over on
    #: a different date is enough.
    season: int = 0
    #: Whether this league keeps players between seasons.
    #:
    #: Not read from ESPN. ``isKeeperLeague`` describes a draft mechanic and
    #: both live leagues report the same value for it, so it does not tell the
    #: two apart -- only the user knows which roster he is building for next
    #: year. It changes exactly one decision, in ``solve/addrop.py``: whether a
    #: player who is out for the season is a free drop or an asset with value
    #: past this week that we are not in a position to price.
    #:
    #: Defaulted to redraft because the two failure modes are not symmetric.
    #: Treating a keeper league as redraft proposes a drop the user can look at
    #: and reject; treating a redraft league as keeper quietly protects dead
    #: weight all season and never says so.
    keeper: bool = False

    def display(self) -> str:
        return self.label or ("league %d" % self.league_id)


@dataclass(frozen=True)
class Config:
    leagues: Sequence[LeagueRef]
    espn_s2: str
    espn_swid: str
    ha_notify_device: str
    ha_owner_user_id: str
    ha_todo_entity: str
    timezone: str
    run_times: Sequence[str]
    autonomy_lineup: bool
    autonomy_ir: bool
    autonomy_add_drop: bool
    never_drop: Sequence[str]
    max_faab_bid: int
    #: How much better per game a pickup must be than the player going out, in
    #: percent, to justify a swap on quality alone. An option rather than a
    #: constant because the requirement was stated as a range -- "10-15%" --
    #: and the middle of a range is a choice, not a fact.
    addrop_margin_pct: float
    #: Minimum points gained before a swap is worth being permanent. Not zero:
    #: every roster has some positive-gain swap available on any given day, and
    #: taking all of them spends a five-a-week acquisition budget on rounding
    #: error. Waived when the move would by itself erase the current deficit.
    addrop_churn_floor: float
    shadow_mode: bool
    log_level: str
    season: int
    db_path: str
    ingress_port: int

    def espn(self, league: LeagueRef) -> EspnConfig:
        """Per-league client config. The credentials are the only shared part."""
        return EspnConfig(
            league_id=league.league_id,
            team_id=league.team_id,
            espn_s2=self.espn_s2,
            swid=self.espn_swid,
            season=league.season or self.season,
            keeper=league.keeper,
        )

    def redacted(self) -> Dict[str, Any]:
        """Safe to log. Used on every startup so the run record shows config."""
        out = {
            k: v
            for k, v in self.__dict__.items()
            if k not in ("espn_s2", "espn_swid", "leagues")
        }
        out["leagues"] = [
            {
                "league_id": l.league_id,
                "team_id": l.team_id,
                "label": l.display(),
                "season": l.season or self.season,
                "keeper": l.keeper,
            }
            for l in self.leagues
        ]
        out["espn_s2"] = "<set:%d chars>" % len(self.espn_s2) if self.espn_s2 else "<unset>"
        out["espn_swid"] = "<set>" if self.espn_swid else "<unset>"
        return out


def _normalize_swid(raw: str) -> str:
    """ESPN wants ``{UUID}`` with braces. Users paste it both ways."""
    s = (raw or "").strip()
    if not s:
        return s
    if not s.startswith("{"):
        s = "{" + s
    if not s.endswith("}"):
        s = s + "}"
    return s


#: Accepted spellings for a league-entry boolean, both ways.
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def _league_bool(where: str, key: str, v: Any) -> bool:
    """Parse a per-league flag, raising on a spelling we do not recognize.

    Stricter than the top-level option reader, which treats anything it does
    not recognize as false. That is fine for a flag whose wrong value shows up
    in the next run's log; it is not fine for ``keeper``, whose wrong value is
    *silent* -- it protects dead weight all season and never explains itself.
    A typo should stop the app at startup, where it is findable.
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ConfigError(
        "%s: %s must be true or false, got %r" % (where, key, v)
    )


def _parse_leagues(opt: Any, req: Any) -> List[LeagueRef]:
    """Build the league list from options, or from flat env vars in dev.

    Duplicate ``league_id`` is rejected rather than deduplicated. Two entries
    for one league means two different ``team_id`` values were configured for
    it, and exactly one of them is a team the user does not own -- the single
    most dangerous misconfiguration available, because ESPN will honour a write
    to a leaguemate's roster with ``200 EXECUTED``. Guessing which entry was
    intended is not a thing this function should do.
    """
    raw = opt("leagues")

    # Dev/CI escape hatch: AUTOPILOT_ESPN_LEAGUE_ID + AUTOPILOT_ESPN_TEAM_ID
    # describe a single league without needing JSON in an env var.
    if not raw:
        flat_league = opt("espn_league_id")
        flat_team = opt("espn_team_id")
        if flat_league and flat_team:
            raw = [{"league_id": flat_league, "team_id": flat_team}]

    if isinstance(raw, str):
        # An env var carrying the whole list.
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ConfigError("leagues is not valid JSON: %s" % exc)

    if not raw:
        raise ConfigError(
            "no leagues configured. Add at least one entry with league_id and "
            "team_id in the app's Configuration tab."
        )
    if isinstance(raw, Mapping):
        raw = [raw]

    out: List[LeagueRef] = []
    seen: Dict[int, int] = {}
    for i, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ConfigError("leagues[%d] is not a mapping: %r" % (i, entry))
        try:
            league_id = int(entry["league_id"])
            team_id = int(entry["team_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(
                "leagues[%d] needs integer league_id and team_id (%s)" % (i, exc)
            )
        if league_id <= 0 or team_id <= 0:
            raise ConfigError(
                "leagues[%d]: league_id and team_id must be positive" % i
            )
        try:
            season = int(entry.get("season") or 0)
        except (TypeError, ValueError):
            raise ConfigError(
                "leagues[%d]: season must be an integer, got %r"
                % (i, entry.get("season"))
            )
        # ESPN labels a season by the year it *ends* in, so the 2026-27 NBA
        # season is 2027. Rejecting the start-year spelling is worth a branch
        # because the mistake resolves to a real, finished season rather than
        # to an error.
        if season and not (2000 <= season <= 2100):
            raise ConfigError(
                "leagues[%d]: season %d is not a plausible ESPN season id. "
                "ESPN labels a season by the year it ends in, so the 2026-27 "
                "season is 2027." % (i, season)
            )
        if league_id in seen:
            raise ConfigError(
                "league %d is configured twice (entries %d and %d) with "
                "team_id %d and %d. Exactly one of those is a team you do not "
                "own; remove the wrong one rather than letting this be guessed."
                % (league_id, seen[league_id], i, out[seen[league_id]].team_id,
                   team_id)
            )
        seen[league_id] = i
        out.append(
            LeagueRef(
                league_id=league_id,
                team_id=team_id,
                label=str(entry.get("label") or "").strip(),
                season=season,
                keeper=_league_bool(
                    "leagues[%d]" % i, "keeper", entry.get("keeper")
                ),
            )
        )
    return out


def load(path: Optional[str] = None) -> Config:
    path = path or OPTIONS_PATH
    raw: Dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        log.warning("%s not found; falling back to environment only", path)

    def opt(key: str, default: Any = None) -> Any:
        env = os.environ.get("AUTOPILOT_" + key.upper())
        if env is not None:
            return env
        return raw.get(key, default)

    def req(key: str) -> Any:
        v = opt(key)
        if v is None or v == "":
            raise ConfigError(
                "required option '%s' is missing. Set it in the app's "
                "Configuration tab." % key
            )
        return v

    def as_bool(v: Any, default: bool) -> bool:
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def as_float(key: str, default: float) -> float:
        """A non-negative number, or a startup failure naming the option.

        Both add/drop thresholds are *brakes*: a negative margin or a negative
        floor does not loosen them, it inverts them into a rule that proposes
        swaps which lose points. Rejecting that at startup is cheaper than
        discovering it in the activity feed.
        """
        v = opt(key)
        if v is None or v == "":
            return float(default)
        try:
            out = float(v)
        except (TypeError, ValueError):
            raise ConfigError("%s must be a number, got %r" % (key, v))
        if out < 0.0:
            raise ConfigError("%s must not be negative, got %r" % (key, v))
        return out

    def as_list(v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return [str(p).strip() for p in v if str(p).strip()]

    run_times = as_list(opt("run_times", ["09:00"])) or ["09:00"]
    for t in run_times:
        parts = t.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ConfigError("run_times entry %r is not HH:MM" % t)
        if not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59):
            raise ConfigError("run_times entry %r is out of range" % t)

    cfg = Config(
        leagues=_parse_leagues(opt, req),
        espn_s2=str(req("espn_s2")).strip(),
        espn_swid=_normalize_swid(str(req("espn_swid"))),
        ha_notify_device=str(req("ha_notify_device")).strip(),
        ha_owner_user_id=str(req("ha_owner_user_id")).strip(),
        ha_todo_entity=str(opt("ha_todo_entity", "todo.fantasy_autopilot")),
        timezone=str(opt("timezone", "America/Los_Angeles")),
        run_times=tuple(run_times),
        autonomy_lineup=as_bool(opt("autonomy_lineup"), True),
        autonomy_ir=as_bool(opt("autonomy_ir"), True),
        autonomy_add_drop=as_bool(opt("autonomy_add_drop"), False),
        never_drop=tuple(as_list(opt("never_drop", []))),
        max_faab_bid=int(opt("max_faab_bid", 0) or 0),
        addrop_margin_pct=as_float(
            "addrop_margin_pct", addrop.DEFAULT_MARGIN_PCT
        ),
        addrop_churn_floor=as_float(
            "addrop_churn_floor", addrop.DEFAULT_CHURN_FLOOR
        ),
        shadow_mode=as_bool(opt("shadow_mode"), True),
        log_level=str(opt("log_level", "info")).lower(),
        season=int(opt("season", 0) or 0) or ids.season_id(),
        db_path=str(os.environ.get("AUTOPILOT_DB", "/data/autopilot.db")),
        ingress_port=int(os.environ.get("AUTOPILOT_INGRESS_PORT", "8099")),
    )

    # The autonomy ceiling. The user's instruction was "do anything as much as
    # possible" for lineups but "ask for confirmation on add/drops", and
    # add/drop is the irreversible, publicly-attributed class of move. So
    # enabling it is possible but never silent.
    if cfg.autonomy_add_drop:
        log.warning(
            "autonomy_add_drop is ENABLED: adds and drops will execute without "
            "confirmation. These are irreversible and publicly attributed in "
            "the league activity feed."
        )
    if not cfg.shadow_mode:
        log.warning("shadow_mode is OFF: writes will reach ESPN.")
    return cfg


LOG_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=LOG_LEVELS.get(level.lower(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
