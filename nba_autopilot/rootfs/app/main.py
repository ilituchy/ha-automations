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
import ha.bridge as bridge_mod
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


class League:
    """One league's read/write pair, plus the label used in notifications.

    Leagues share the HTTP client (same cookie, same hosts, and httpx pools
    connections), the store and the HA bridge. They share nothing else: the
    optimizer, the games-played budget and the roster are all league-local,
    and every proposal names its league because a notification reading only
    "drop Smith" is unactionable when two teams are in play.
    """

    def __init__(self, ref: appconfig.LeagueRef, cfg: appconfig.Config,
                 http: Any, store: Store) -> None:
        self.ref = ref
        self.name = ref.display()
        self.espn = cfg.espn(ref)
        self.read = EspnRead(self.espn, http)
        self.write = EspnWrite(
            self.espn,
            http,
            self.read,
            journal=store,
            shadow_mode=cfg.shadow_mode,
            never_drop=cfg.never_drop,
            max_faab_bid=cfg.max_faab_bid,
        )

    def __repr__(self) -> str:
        return "<League %d team=%d>" % (self.ref.league_id, self.ref.team_id)


class Autopilot:
    def __init__(self, cfg: appconfig.Config) -> None:
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.store = Store(cfg.db_path)
        # One client across leagues: identical credentials and hosts, so a
        # second client would buy nothing but a second connection pool.
        self.http = make_client(cfg.espn(cfg.leagues[0]))
        self.leagues = [League(ref, cfg, self.http, self.store)
                        for ref in cfg.leagues]
        self.ha = HaBridge(
            notify_device=cfg.ha_notify_device,
            owner_user_id=cfg.ha_owner_user_id,
            todo_entity=cfg.ha_todo_entity,
        )

    def league_for_run(self, run_id: str) -> Optional[League]:
        """Map a stored run back to its league, to execute an approved tap.

        A tap carries only a run id, so this is the step that decides whose
        roster a move lands on. It returns ``None`` rather than defaulting to
        the first league: writing to the wrong league is precisely what the
        ownership check exists to prevent, and a silent fallback would route
        around that check instead of tripping it.
        """
        row = self.store.db.execute(
            "SELECT league_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None or row["league_id"] is None:
            log.error("run %s has no league recorded; refusing to execute", run_id)
            return None
        for lg in self.leagues:
            if lg.ref.league_id == int(row["league_id"]):
                return lg
        log.error(
            "run %s belongs to league %s, which is no longer configured; "
            "refusing to execute", run_id, row["league_id"],
        )
        return None

    # -- preflight at boot -------------------------------------------------

    def startup_checks(self) -> bool:
        ok = True
        log.info("config: %s", json.dumps(self.cfg.redacted(), default=str))

        for lg in self.leagues:
            try:
                settings = lg.read.league_settings()
                log.info(
                    "ESPN OK: [%d] %s, %s, scoring period %d",
                    lg.ref.league_id,
                    settings.name,
                    settings.scoring_type,
                    settings.current_scoring_period,
                )
            except EspnAuthError as exc:
                # One cookie serves every league, so an auth failure is global.
                # Say it once and stop rather than repeating it per league.
                log.error("ESPN auth failed: %s", exc)
                self._notify_problem(
                    "ESPN cookie expired",
                    "Autopilot cannot reach your leagues. Re-copy espn_s2 from "
                    "your browser and update the app configuration.",
                )
                return False
            except EspnError as exc:
                log.error("league %d unreachable: %s", lg.ref.league_id, exc)
                ok = False
                continue

            mine = lg.read.my_team_ids()
            if lg.ref.team_id not in mine:
                log.error(
                    "league %d: team_id %d is not owned by this SWID (owned: "
                    "%s). Refusing to run -- writing to a team you do not own "
                    "is permitted by ESPN and would be publicly attributed to "
                    "you.",
                    lg.ref.league_id,
                    lg.ref.team_id,
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

    def run_once(self) -> List[str]:
        """Run every configured league. Returns the run ids.

        A league that throws does not stop the others. They are independent
        problems that happen to share a cookie, and skipping today's lineup in
        league B because league A's roster fetch timed out would be a cost with
        no corresponding benefit.
        """
        run_ids: List[str] = []
        for lg in self.leagues:
            try:
                run_ids.append(self.run_league(lg))
            except EspnError as exc:
                log.error("league %s failed: %s", lg.name, exc)
            except Exception:  # noqa: BLE001 - one league must not kill the rest
                log.exception("league %s raised", lg.name)
        return run_ids

    def run_league(self, lg: "League") -> str:
        run_id = _ulid()
        settings = lg.read.league_settings()
        self.store.start_run(
            run_id,
            settings.current_scoring_period,
            self.cfg.shadow_mode,
            league_id=lg.ref.league_id,
        )
        log.info("=== run %s | %s | scoring period %d ===", run_id,
                 settings.name, settings.current_scoring_period)

        # NOTE: the projection + solve + propose pipeline is wired in the next
        # phase. Until it is, a run performs the read-only half -- which is
        # exactly what shadow mode is for, and what validates the ESPN read
        # layer against the real leagues before anything can write.
        roster = lg.read.roster()
        log.info("roster: %d players, %d locked", len(roster),
                 sum(1 for p in roster if p.lineup_locked))
        if settings.has_gp_cap:
            log.info("games used: %s", lg.read.games_used())

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
        """Apply a tap. Already authorized and shape-checked by the bridge.

        The verb is carried by the button, not by the proposal, so it must be
        stripped before any database lookup -- ``parsed.proposal_action_id`` is
        the id as ``state.py`` stores it. Passing the raw tapped id here matches
        no row and fails silently, which is the failure this whole module is
        built to prevent.
        """
        parsed = ev.parsed or bridge_mod.parse_action_id(ev.action_id)
        if parsed is None:
            log.warning("unrecognized action id %r", ev.action_id)
            return

        if parsed.is_bulk:
            for p in self.store.pending(parsed.run_id):
                self.store.decide(p.action_id, parsed.approve, ev.user_id or "?")
            log.info("%s for run %s", parsed.verb, parsed.run_id)
        else:
            decided = self.store.decide(
                parsed.proposal_action_id, parsed.approve, ev.user_id or "?"
            )
            if decided is None:
                # Already decided, expired, or never existed. A stale tap on an
                # old notification lands here and correctly does nothing.
                log.info("tap on %r changed nothing", parsed.proposal_action_id)
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
        lg = self.league_for_run(proposal.run_id)
        if lg is None:
            raise ValueError(
                "cannot route %s to a league; not executing" % proposal.action_id
            )
        kind = proposal.kind
        payload = proposal.payload
        if kind == "LINEUP":
            return lg.write.set_lineup(
                {int(k): int(v) for k, v in payload["target"].items()}
            )
        if kind in ("ADD", "DROP", "ADD_DROP"):
            return lg.write.add_drop(
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
        failures = 0
        for lg in bot.leagues:
            log.info("")
            log.info("################ league %d (team %d) ################",
                     lg.ref.league_id, lg.ref.team_id)
            try:
                report = discovery.report(lg.read)
            except EspnError as exc:
                log.error("discovery failed for league %d: %s",
                          lg.ref.league_id, exc)
                failures += 1
                continue
            log.info("discovery complete; %d facts collected", len(report))
        return 1 if failures else 0

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
