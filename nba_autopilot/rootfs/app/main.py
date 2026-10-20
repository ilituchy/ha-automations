"""Entrypoint. Scheduler, action listener, ingress status page.

Modes:
    python3 /app/main.py --discover   one-shot league discovery, exits
    python3 /app/main.py --once       one pipeline run, exits
    python3 /app/main.py              daemon: scheduled runs + approval loop

Startup order is deliberate. Config, then ESPN auth, then HA reachability, then
the scheduler. Each of those is a thing that can be wrong in a way the user must
be told about, and finding out at 09:00 is worse than finding out at boot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import config as appconfig
import discover as discovery
from espn.read import EspnAuthError, EspnError, EspnRead, make_client
from espn.write import EspnWrite
from ha.bridge import Action, ActionEvent, HaBridge
from state import Store

log = logging.getLogger("autopilot")


def _ulid() -> str:
    """Lexicographically sortable run id.

    Sortable matters: run ids are embedded in notification action ids, so
    ordering them tells you which of two stale notifications is newer without
    a database lookup.
    """
    try:
        from ulid import ULID

        return str(ULID())
    except Exception:  # noqa: BLE001 - ulid is optional at dev time
        return datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")


class Autopilot:
    def __init__(self, cfg: appconfig.Config) -> None:
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.store = Store(cfg.db_path)
        self.http = make_client(cfg.espn())
        self.read = EspnRead(cfg.espn(), self.http)
        self.write = EspnWrite(
            cfg.espn(),
            self.http,
            self.read,
            journal=self.store,
            shadow_mode=cfg.shadow_mode,
            never_drop=cfg.never_drop,
            max_faab_bid=cfg.max_faab_bid,
        )
        self.ha = HaBridge(
            notify_device=cfg.ha_notify_device,
            owner_user_id=cfg.ha_owner_user_id,
            todo_entity=cfg.ha_todo_entity,
        )

    # -- preflight at boot -------------------------------------------------

    def startup_checks(self) -> bool:
        ok = True
        log.info("config: %s", json.dumps(self.cfg.redacted(), default=str))

        try:
            settings = self.read.league_settings()
            log.info(
                "ESPN OK: %s, %s, scoring period %d",
                settings.name,
                settings.scoring_type,
                settings.current_scoring_period,
            )
        except EspnAuthError as exc:
            log.error("ESPN auth failed: %s", exc)
            self._notify_problem(
                "ESPN cookie expired",
                "Autopilot cannot reach your league. Re-copy espn_s2 from your "
                "browser and update the app configuration.",
            )
            return False
        except EspnError as exc:
            log.error("ESPN unreachable: %s", exc)
            return False

        mine = self.read.my_team_ids()
        if self.cfg.espn_team_id not in mine:
            log.error(
                "team_id %d is not owned by this SWID (owned: %s). Refusing to "
                "run -- writing to a team you do not own is permitted by ESPN "
                "and would be publicly attributed to you.",
                self.cfg.espn_team_id,
                sorted(mine),
            )
            ok = False

        ext = self.ha.check_external_url()
        if ext is None:
            # Not fatal: lineup/IR autonomy needs no approval, so the app is
            # still useful. But add/drop approval cannot work, so say so.
            log.warning(
                "HA is not externally reachable. Autonomous lineup and IR moves "
                "will still run; add/drop approvals will not be actionable."
            )
        else:
            log.info("HA external access: %s", ext)

        stuck = self.store.stuck_writes()
        if stuck:
            log.error(
                "%d indeterminate write(s) from a previous run need "
                "reconciliation before new writes are safe: %s",
                len(stuck),
                [s["id"] for s in stuck],
            )
            ok = False

        if self.cfg.shadow_mode:
            log.warning(
                "SHADOW MODE: the full pipeline runs and notifies, but no "
                "request reaches ESPN. Turn this off once a few runs look right."
            )
        return ok

    def _notify_problem(self, title: str, message: str) -> None:
        try:
            self.ha.notify(title=title, message=message, tag="autopilot-problem",
                           actions=())
            self.ha.persistent_notification(title, message)
        except Exception as exc:  # noqa: BLE001
            log.error("could not deliver problem notification: %s", exc)

    # -- the daily run -----------------------------------------------------

    def run_once(self) -> str:
        run_id = _ulid()
        settings = self.read.league_settings()
        self.store.start_run(run_id, settings.current_scoring_period,
                             self.cfg.shadow_mode)
        log.info("=== run %s, scoring period %d ===", run_id,
                 settings.current_scoring_period)

        # NOTE: the projection + solve + propose pipeline is wired in the next
        # phase. Until it is, a run performs the read-only half -- which is
        # exactly what shadow mode is for, and what validates the ESPN read
        # layer against the real league before anything can write.
        roster = self.read.roster()
        log.info("roster: %d players, %d locked", len(roster),
                 sum(1 for p in roster if p.lineup_locked))
        if settings.has_gp_cap:
            log.info("games used: %s", self.read.games_used())

        self.store.finish_run(
            run_id,
            sources_used={"espn": "ok"},
            sources_missing={},
            shadow_prices={},
            category_weights={},
            notes=["read-only run: solver pipeline not yet wired"],
        )
        return run_id

    # -- approval loop -----------------------------------------------------

    async def handle_action(self, ev: ActionEvent) -> None:
        """Apply a tap. Already authorized by the bridge."""
        parts = ev.action_id.split(":")
        if len(parts) < 2:
            log.warning("unrecognized action id %r", ev.action_id)
            return
        verb = parts[-1]
        if verb in ("APPROVE_ALL", "REJECT_ALL"):
            approve = verb == "APPROVE_ALL"
            run_id = parts[0]
            for p in self.store.pending(run_id):
                self.store.decide(p.action_id, approve, ev.user_id or "?")
            log.info("%s for run %s", verb, run_id)
        else:
            decided = self.store.decide(
                ev.action_id, verb == "APPROVE", ev.user_id or "?"
            )
            if decided is None:
                return
        await self.execute_approved()

    async def execute_approved(self) -> None:
        for p in self.store.approved():
            if not self.store.claim_for_execution(p.id):
                # Lost the race to another tap or the scheduler. Correct
                # behaviour is to do nothing at all.
                log.info("proposal %s already claimed", p.action_id)
                continue
            try:
                result = self._execute(p)
                self.store.complete(p.id, bool(result and result.ok))
                self.ha.confirm(
                    tag="autopilot-%s" % p.run_id,
                    title="Autopilot: done",
                    message=p.rationale or p.kind,
                )
            except Exception as exc:  # noqa: BLE001 - must not kill the loop
                log.exception("executing %s failed", p.action_id)
                self.store.complete(p.id, False, str(exc))
                self._notify_problem("Autopilot: move failed", str(exc)[:400])

    def _execute(self, proposal: Any) -> Any:
        kind = proposal.kind
        payload = proposal.payload
        if kind == "LINEUP":
            return self.write.set_lineup(
                {int(k): int(v) for k, v in payload["target"].items()}
            )
        if kind in ("ADD", "DROP", "ADD_DROP"):
            return self.write.add_drop(
                add=payload.get("add"),
                drop=payload.get("drop"),
                bid=payload.get("bid"),
                waiver=bool(payload.get("waiver", True)),
            )
        raise ValueError("unknown proposal kind %r" % kind)

    # -- daemon ------------------------------------------------------------

    async def serve(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        queue: "asyncio.Queue[ActionEvent]" = asyncio.Queue()
        sched = AsyncIOScheduler(timezone=self.tz)

        for t in self.cfg.run_times:
            hour, minute = t.split(":")
            sched.add_job(
                lambda: asyncio.create_task(asyncio.to_thread(self.run_once)),
                CronTrigger(hour=int(hour), minute=int(minute), timezone=self.tz),
                name="run@%s" % t,
            )
            log.info("scheduled daily run at %s %s", t, self.cfg.timezone)

        # Expiry sweep. Approval must not carry over, and a proposal whose
        # notification was never tapped has to die on its own.
        sched.add_job(
            lambda: self.store.expire_stale(),
            CronTrigger(minute="*/15", timezone=self.tz),
            name="expire-sweep",
        )
        sched.start()

        listener = asyncio.create_task(self.ha.listen_actions(queue, stop=stop))
        try:
            while not stop.is_set():
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                await self.handle_action(ev)
        finally:
            listener.cancel()
            sched.shutdown(wait=False)
            self.store.close()
            self.http.close()
            log.info("stopped")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="nba-autopilot")
    ap.add_argument("--discover", action="store_true",
                    help="print league facts and exit")
    ap.add_argument("--once", action="store_true", help="one run, then exit")
    args = ap.parse_args(argv)

    try:
        cfg = appconfig.load()
    except appconfig.ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2
    appconfig.setup_logging(cfg.log_level)

    bot = Autopilot(cfg)

    if args.discover:
        try:
            report = discovery.report(bot.read)
        except EspnError as exc:
            log.error("discovery failed: %s", exc)
            return 1
        log.info("discovery complete; %d facts collected", len(report))
        return 0

    if not bot.startup_checks():
        log.error("startup checks failed; not scheduling runs")
        return 1

    if args.once:
        bot.run_once()
        return 0

    asyncio.run(bot.serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
