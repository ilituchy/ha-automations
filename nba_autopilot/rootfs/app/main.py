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
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from zoneinfo import ZoneInfo

import config as appconfig
import discover as discovery
import pipeline
import usage as usage_mod
from espn import ids
from espn.read import EspnAuthError, EspnError, EspnRead, make_client
from espn.write import EspnWrite
import ha.bridge as bridge_mod
from ha.bridge import Action, ActionEvent, HaBridge
from sources.espn_injuries import EspnInjuries
from solve import projections as projections_mod
from solve import value as value_mod
from solve.calendar import SeasonCalendar
from state import Store

log = logging.getLogger("autopilot")

#: How long one fetch of ESPN's injuries page is reused. Short because the
#: page is the late-breaking input -- a player ruled out at 6:40pm for a 7pm
#: tip is exactly the case this source exists to catch -- and long enough that
#: every league in a single run cycle shares one request.
INJURY_NOTE_TTL = timedelta(minutes=15)

#: Distinguishes "not built yet" from "built, and the answer was None". Without
#: it a failed calendar build would be retried on every league of every run,
#: which means re-downloading a multi-megabyte season document to get the same
#: failure.
_UNSET = object()


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


def _redact_host(url: str) -> str:
    """``https://abc123.ui.nabu.casa`` -> ``https://***.ui.nabu.casa``.

    Keeps everything diagnostically useful -- the scheme, and whether this is
    Nabu Casa or a self-hosted domain -- and drops the one label that is
    effectively a secret: the first, which is where the random part lives in
    both ``abc123.ui.nabu.casa`` and ``ha.example.com``.

    Three labels is the threshold. An apex domain has no subdomain to hide, so
    redacting ``example.com`` into ``***.com`` would destroy the useful half of
    a string that was not sensitive to begin with.

    A bare IPv4 address is replaced whole. Dropping only its first octet would
    satisfy the label rule while still publishing most of a network address,
    which is the opposite of the point.
    """
    try:
        if "://" in url:
            scheme, _, rest = url.partition("://")
            prefix = scheme + "://"
        else:
            prefix, rest = "", url
        host, slash, path = rest.partition("/")
        hostname, colon, port = host.partition(":")
        labels = hostname.split(".")
        if len(labels) == 4 and all(l.isdigit() for l in labels):
            labels = ["***"]
        elif len(labels) >= 3:
            labels[0] = "***"
        return "%s%s%s%s%s%s" % (
            prefix, ".".join(labels), colon, port, slash, path,
        )
    except Exception:  # noqa: BLE001 - a log line must never raise
        return "<unparseable>"


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
        # Season-scoped, league-independent, and expensive to fetch. See
        # :meth:`season_calendar`.
        self._calendar: Any = _UNSET
        self._schedule: Optional[Dict[int, Dict[int, int]]] = None
        # League-independent but *not* season-scoped: injuries move hourly, so
        # this one carries a TTL rather than living for the process. See
        # :meth:`injury_notes`.
        self._injuries: Any = None
        self._injuries_at: Optional[datetime] = None

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
            # Only the fact matters here, not the address. A Nabu Casa hostname
            # is a long random label that fronts this instance's login page,
            # and app logs get pasted into bug reports; printing the whole
            # thing on every start buys nothing the scheme and suffix don't.
            log.info("HA external access: OK (%s)", _redact_host(ext))

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

    def season_calendar(self) -> Optional[SeasonCalendar]:
        """The NBA calendar, built once per process.

        Derived from the pro schedule, not from any league's settings, so it is
        the same object for every configured league -- and the payload it comes
        from is multi-megabyte, which is the practical reason it is cached
        rather than the conceptual one.
        """
        if self._calendar is _UNSET:
            try:
                dates = self.leagues[0].read.scoring_period_dates()
                self._calendar = SeasonCalendar.from_period_dates(dates)
            except EspnError as exc:
                log.error("could not build the season calendar: %s", exc)
                self._calendar = None
        return self._calendar

    def pro_schedule(self) -> Mapping[int, Mapping[int, int]]:
        """``proTeamId -> {scoringPeriod: games}``, built once per process."""
        if self._schedule is None:
            try:
                self._schedule = self.leagues[0].read.pro_team_scoring_periods()
            except EspnError as exc:
                log.error("could not read the pro schedule: %s", exc)
                self._schedule = {}
        return self._schedule

    def injury_notes(self) -> Optional[Any]:
        """ESPN's injuries page, shared across leagues, refreshed on a TTL.

        Unlike the calendar and the pro schedule this is not season-static --
        a note can flip two hours before tip -- so caching it for the life of
        the process would mean a lineup written at 7pm acted on the morning's
        news. The TTL is short enough that every run inside one cycle shares
        one fetch and the next cycle gets fresh data.

        Returns the ``SourceResult`` or ``None``; ``None`` is a degraded run,
        not a failed one, and the caller records it in ``sources_missing``.
        """
        now = datetime.now(timezone.utc)
        if (
            self._injuries is not None
            and self._injuries_at is not None
            and now - self._injuries_at < INJURY_NOTE_TTL
        ):
            return self._injuries
        self._injuries = EspnInjuries().fetch()
        self._injuries_at = now
        return self._injuries

    def _availability_overrides(
        self, roster: Sequence[Any], injuries: Optional[Any]
    ) -> Tuple[Dict[int, Tuple[str, str]], List[str]]:
        """Build ``player_id -> (status, label)`` from the injuries page.

        The join is a plain id match: ESPN's fantasy ``playerId`` and its site
        ``athlete`` id are the same number (verified 2026-10-21 -- Mouhamed
        Gueye is 4712863 in both the fantasy player list and the injuries
        page), so this source needs no crosswalk. Players the page could not
        give an id for fall back to a normalised-name lookup, which is why
        both indexes are carried in the payload.

        This **reports** disagreement rather than resolving it. ESPN's fantasy
        ``injuryStatus`` stays the floor -- ``p_play_for`` only honours an
        override that is *worse* -- so what comes back from here is a note
        about what the beat reporters said, and the run record keeps both
        readings.
        """
        if injuries is None:
            return {}, []

        payload = injuries.payload
        by_id = payload.get("by_athlete_id") or {}
        by_name = payload.get("by_name") or {}
        from sources.crosswalk import normalize_name

        overrides: Dict[int, Tuple[str, str]] = {}
        notes: List[str] = []
        for entry in roster:
            note = by_id.get(str(entry.player_id))
            matched_on = "id"
            if note is None:
                note = by_name.get(normalize_name(entry.name))
                matched_on = "name"
            if note is None:
                continue

            label = "espn-injuries" if not note.comment_matched else "espn-beat-report"
            overrides[entry.player_id] = (note.bucket, label)

            espn_status = entry.availability
            if note.bucket != espn_status:
                notes.append(
                    "%s: ESPN fantasy says %s, injuries page says %s%s"
                    % (
                        entry.name,
                        espn_status,
                        note.bucket,
                        " (%r)" % note.comment_phrase if note.comment_phrase else "",
                    )
                )
            if matched_on == "name":
                log.info(
                    "espn_injuries: matched %s by name, not id", entry.name
                )
        return overrides, notes

    def _calendar_for(
        self, settings: Any
    ) -> Tuple[Optional[SeasonCalendar], int, List[str]]:
        """The calendar, this league's matchup period, and any complaints.

        The cross-check runs here rather than in a test because it is checking
        ESPN's behaviour, not ours. If the week convention ever changes, the
        model keeps returning plausible numbers and only this comparison
        notices; a calendar that fails it is downgraded to ``None``, which
        switches cap pricing off rather than letting it be wrong.
        """
        notes: List[str] = []
        cal = self.season_calendar()
        if cal is None:
            return None, int(settings.current_matchup_period), [
                "no season calendar: the games-played cap cannot be priced"
            ]
        problem = cal.verify(
            settings.current_scoring_period, settings.current_matchup_period
        )
        if problem:
            log.error(
                "league %d: season calendar disagrees with ESPN -- %s. "
                "Not pricing the games-played cap this run.",
                settings.league_id, problem,
            )
            return None, int(settings.current_matchup_period), [
                "calendar cross-check failed: %s" % problem
            ]
        matchup = cal.matchup_period_of(settings.current_scoring_period)
        notes.append(cal.describe(matchup))
        return cal, matchup, notes

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

        notes: List[str] = []
        sources_used: Dict[str, str] = {"espn": "ok"}
        sources_missing: Dict[str, str] = {}

        roster = lg.read.roster()
        log.info("roster: %d players, %d locked", len(roster),
                 sum(1 for p in roster if p.lineup_locked))

        cal, matchup_period, cal_notes = self._calendar_for(settings)
        notes.extend(cal_notes)
        schedule = self.pro_schedule()
        if not schedule:
            sources_missing["pro-schedule"] = "unavailable"

        injuries = self.injury_notes()
        overrides, injury_notes = self._availability_overrides(roster, injuries)
        notes.extend(injury_notes)
        if injuries is None:
            sources_missing["espn-injuries"] = "unavailable or stale"
            notes.append(
                "no injuries page this run: availability falls back to ESPN's "
                "fantasy status alone"
            )
        else:
            parse = injuries.payload.get("parse") or {}
            # The join count is here, not just the page counts, because it is
            # the only number that can tell a healthy quiet run from a broken
            # one. Observed on-device 2026-10-21: 80 notes parsed, 61 refined,
            # and exactly 2 of 24 rostered players on the page -- both already
            # OUT in ESPN's fantasy feed, so the run produced no notes at all.
            # Without "2 of 24" in the record, that silence looks identical to
            # the join matching nobody because the id spaces drifted apart.
            sources_used["espn-injuries"] = (
                "%d notes, %d refined by beat report, %d of %d rostered "
                "players on the page"
                % (
                    parse.get("notes", 0),
                    parse.get("refined_by_comment", 0),
                    len(overrides),
                    len(roster),
                )
            )

        splits = lg.read.player_projections([e.player_id for e in roster])
        projections, availability, skipped = projections_mod.build_projections(
            roster, splits, as_of=datetime.now(timezone.utc), statuses=overrides
        )
        if skipped:
            notes.append(
                "no projection for: %s" % ", ".join(e.name for e in skipped)
            )
        if not projections:
            # Every value would be zero, so the "optimal" lineup would be
            # whatever the tie-breaks happened to produce. Writing that is worse
            # than leaving yesterday's lineup alone.
            sources_missing["espn-projection"] = "no players projected"
            notes.append("aborting: no usable projections for any player")
            self._finish(run_id, sources_used, sources_missing, {}, {}, notes)
            self._notify_problem(
                "Autopilot: no projections",
                "%s: ESPN returned no usable projections, so no lineup was "
                "computed. Yesterday's lineup is untouched." % lg.name,
            )
            return run_id

        games_used = usage_mod.measure(
            lg.read,
            self.store,
            league_id=lg.ref.league_id,
            team_id=lg.ref.team_id,
            calendar=cal,
            scoring_period=settings.current_scoring_period,
            matchup_period=matchup_period,
        ) if settings.has_gp_cap else None
        if games_used is not None:
            log.info("games used: %s", games_used.describe())
            notes.append(games_used.describe())

        plan = pipeline.plan_lineup(
            settings,
            roster,
            projections,
            scoring_period=settings.current_scoring_period,
            matchup_period=matchup_period,
            schedule=schedule,
            calendar=cal,
            games_used=games_used,
            availability=availability,
        )
        notes.extend(plan.notes)
        for line in pipeline.summarize(plan, roster).splitlines():
            log.info("  %s", line)

        coefficients = getattr(
            value_mod.build_value_model(settings), "coefficients", {}
        )
        # Keyed by the pseudo-slot ESPN hangs the pooled cap on, so the stored
        # dual is self-describing when it is read back out of the journal.
        shadow_prices = (
            {ids.TEAM_TOTAL_LIMIT_SLOT: plan.lam} if plan.cap else {}
        )

        if not plan.feasible:
            self._finish(run_id, sources_used, sources_missing, shadow_prices,
                         coefficients, notes)
            self._notify_problem(
                "Autopilot: lineup not applied",
                "%s: the computed lineup failed its own validation, so nothing "
                "was sent to ESPN. Reasons:\n%s"
                % (lg.name, "\n".join(n for n in plan.notes
                                      if n.startswith("invalid target"))),
            )
            return run_id

        if plan.changed:
            self._apply_lineup(run_id, lg, plan, roster, settings)
        else:
            log.info("lineup already optimal; nothing to do")
            notes.append("no change needed")

        self._finish(run_id, sources_used, sources_missing, shadow_prices,
                     coefficients, notes)
        return run_id

    def _finish(
        self,
        run_id: str,
        sources_used: Mapping[str, str],
        sources_missing: Mapping[str, str],
        shadow_prices: Mapping[int, float],
        category_weights: Mapping[int, float],
        notes: List[str],
    ) -> None:
        self.store.finish_run(
            run_id,
            sources_used=sources_used,
            sources_missing=sources_missing,
            shadow_prices=shadow_prices,
            category_weights=category_weights,
            notes=notes,
        )

    def _apply_lineup(
        self,
        run_id: str,
        lg: "League",
        plan: "pipeline.LineupPlan",
        roster: Any,
        settings: Any,
    ) -> None:
        """Journal, execute, notify. Lineup and IR only -- never an acquisition.

        ``auto_approve=True`` is the autonomy decision, and it still writes a
        proposal row: autonomy changes who approves, never whether there is an
        audit trail. The claim is still taken, so a scheduled run and a tap can
        never both execute the same plan.

        ``shadow_mode`` is enforced inside ``EspnWrite``, not here. There is one
        place that decides whether a packet reaches ESPN, and it is the same
        place that builds the packet.
        """
        payload = {
            "target": {str(k): int(v) for k, v in plan.target.items()},
            "scoring_period": int(settings.current_scoring_period),
        }
        rationale = "; ".join(
            "%s %s->%s" % (m.player_name, ids.slot_name(m.from_slot or -1),
                           ids.slot_name(m.to_slot or -1))
            for m in plan.moves
        )
        proposal = self.store.propose(
            run_id, "LINEUP", payload, rationale=rationale,
            value_delta=sum(m.value_delta for m in plan.moves),
            auto_approve=True,
        )
        if not self.store.claim_for_execution(proposal.id):
            log.warning("lineup proposal %s already claimed", proposal.action_id)
            return
        try:
            result = lg.write.set_lineup(
                {int(k): int(v) for k, v in plan.target.items()},
                roster=roster,
                scoring_period=settings.current_scoring_period,
            )
            self.store.complete(proposal.id, bool(result and result.ok))
        except Exception as exc:  # noqa: BLE001 - one league must not kill the rest
            log.exception("lineup write failed for %s", lg.name)
            self.store.complete(proposal.id, False, str(exc))
            self._notify_problem("Autopilot: lineup write failed",
                                 "%s: %s" % (lg.name, str(exc)[:400]))
            return

        title = "%s: %d move(s)%s" % (
            lg.name, len(plan.moves), " [shadow]" if self.cfg.shadow_mode else "",
        )
        try:
            self.ha.notify(
                title=title,
                message=pipeline.summarize(plan, roster),
                tag="autopilot-lineup-%d" % lg.ref.league_id,
                actions=(),
                time_sensitive=False,
            )
        except Exception as exc:  # noqa: BLE001 - a failed push is not a failed run
            log.error("could not deliver lineup notification: %s", exc)

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

    async def scheduled_run(self) -> None:
        """The daily run, as the scheduler invokes it.

        **This must stay a coroutine function.** ``AsyncIOScheduler`` only
        awaits the job on the event loop when it *is* one; a plain function is
        handed to a worker thread instead, where there is no running loop. The
        first version of this was a ``lambda`` calling ``asyncio.create_task``,
        which meant every scheduled run died with ``RuntimeError: no running
        event loop`` -- the job was dispatched to a thread, so the one thing it
        did needed the loop it no longer had. Nothing caught it in testing
        because the failure needs a real fire to happen at all.

        ``run_once`` is blocking (ESPN reads, SQLite, notifications), so it goes
        to a thread deliberately -- just via ``await``, from inside the loop,
        rather than from a thread that is pretending to be in one.
        """
        await asyncio.to_thread(self.run_once)

    async def _serve_web(self) -> None:
        """The ingress status page, isolated so its failure is survivable.

        A crash here must not take the daemon down with it. The page is how you
        *see* proposals; the notification is how you get told about them, and
        losing the former while keeping the latter is a degraded app rather than
        a dead one. So the exception is logged and the task ends quietly.
        """
        try:
            import web

            await web.serve(self, self.cfg.ingress_port)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception(
                "the ingress status page stopped; scheduled runs and "
                "notification approvals are unaffected"
            )

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
                self.scheduled_run,
                CronTrigger(hour=int(hour), minute=int(minute), timezone=self.tz),
                name="run@%s" % t,
                # The default grace is one second. On a job that fires once a
                # day, a loop that happens to be busy for a second at 09:00
                # would discard the entire day's run as a misfire. An hour is
                # still far short of the next fire, so this cannot double up.
                misfire_grace_time=3600,
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
        # The review surface. Deliberately a sibling task rather than a
        # precondition: if uvicorn cannot bind, the scheduler and the
        # notification path must keep working, because they are what make the
        # app useful and the page is only the nicer way to answer it.
        web_task = asyncio.create_task(self._serve_web())
        try:
            while not stop.is_set():
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                await self.handle_action(ev)
        finally:
            listener.cancel()
            web_task.cancel()
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
