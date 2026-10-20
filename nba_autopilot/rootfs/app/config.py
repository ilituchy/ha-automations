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

log = logging.getLogger(__name__)

OPTIONS_PATH = os.environ.get("AUTOPILOT_OPTIONS", "/data/options.json")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    espn_league_id: int
    espn_team_id: int
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
    shadow_mode: bool
    log_level: str
    season: int
    db_path: str
    ingress_port: int

    def espn(self) -> EspnConfig:
        return EspnConfig(
            league_id=self.espn_league_id,
            team_id=self.espn_team_id,
            espn_s2=self.espn_s2,
            swid=self.espn_swid,
            season=self.season,
        )

    def redacted(self) -> Dict[str, Any]:
        """Safe to log. Used on every startup so the run record shows config."""
        out = {
            k: v
            for k, v in self.__dict__.items()
            if k not in ("espn_s2", "espn_swid")
        }
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
        espn_league_id=int(req("espn_league_id")),
        espn_team_id=int(req("espn_team_id")),
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
