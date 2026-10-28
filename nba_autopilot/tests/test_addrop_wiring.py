"""How the add/drop decision reaches ESPN, or asks first.

``test_addrop.py`` covers the arithmetic. This covers the orchestration around
it, which has its own failure modes and none of them are arithmetic:

* every ESPN read here is optional, because the lineup has already been written
  by the time add/drop is considered and a pool fetch is not worth a run;
* the players that may not be dropped are the ones *this run* just started, not
  the ones ESPN had in a starting slot when the run began;
* and the asking is the whole point -- ``autonomy_add_drop`` defaults to false
  because a drop is irreversible and publicly attributed, so the default path
  has to produce a tappable proposal and no write at all.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

import main as main_mod  # noqa: E402
import pipeline  # noqa: E402
from solve import projections as projections_mod  # noqa: E402
from espn import ids  # noqa: E402
from ha import bridge as bridge_mod  # noqa: E402
from models import (  # noqa: E402
    P_PLAY_PRIOR,
    LeagueSettings,
    MatchupScore,
    Move,
    Projection,
    RosterEntry,
)
from solve.calendar import SeasonCalendar  # noqa: E402

PTS = ids.PTS
START, BENCH = 11, 12


class _Store:
    """Just enough of ``state.Store`` to see what was journalled."""

    class _Row:
        def __init__(self, pid, run_id, action_id, kind, payload, rationale,
                     delta, auto):
            self.id = pid
            # Kept because the lineup written after an acquisition must be
            # journalled under the run that *proposed* the swap, which may be
            # hours earlier than the tap that approved it.
            self.run_id = run_id
            self.action_id = action_id
            self.kind = kind
            self.payload = payload
            self.rationale = rationale
            self.value_delta = delta
            self.auto_approve = auto
            self.completed = None
            self.error = None

    def __init__(self, *, claimable: bool = True, forecasts_raise=None):
        self.rows = []
        self.claimable = claimable
        self.claims = []
        self.queued = []
        self.forecasts = []
        self.forecasts_raise = forecasts_raise

    def record_forecasts(self, run_id, *, league_id, scoring_period, rows):
        if self.forecasts_raise is not None:
            raise self.forecasts_raise
        rows = list(rows)
        self.forecasts.append({
            "run_id": run_id, "league_id": league_id,
            "scoring_period": scoring_period, "rows": rows,
        })
        return len(rows)

    def propose(self, run_id, kind, payload, *, rationale="", value_delta=0.0,
                auto_approve=False, declined=False):
        row = self._Row(len(self.rows) + 1, run_id,
                        "%s:%s:%d" % (run_id, kind, len(self.rows) + 1),
                        kind, dict(payload), rationale, value_delta,
                        auto_approve)
        # The real store writes BLOCKED straight into the row; here the flag
        # is enough, because what the tests ask is whether the row exists and
        # which kind of record it is.
        row.state = "BLOCKED" if declined else (
            "APPROVED" if auto_approve else "PROPOSED")
        self.rows.append(row)
        return row

    def pending(self):
        """What the approval queue would show. A declined row is never in it,
        which is the property the state machine's own tests prove and this one
        only has to reflect."""
        return [r for r in self.rows if r.state == "PROPOSED"]

    def claim_for_execution(self, pid):
        self.claims.append(pid)
        return self.claimable

    def approved(self):
        """What the approval loop finds waiting. Populated by a test, because
        the real query is ``state.Store``'s and is covered there."""
        return list(self.queued)

    def complete(self, pid, ok, error=None):
        self.rows[pid - 1].completed = ok
        self.rows[pid - 1].error = error


class _Ha:
    def __init__(self, *, explode: bool = False):
        self.sent = []
        self.problems = []
        self.cleared = []
        self.explode = explode

    def notify(self, *, title, message, tag, actions=(), time_sensitive=True):
        if self.explode:
            raise RuntimeError("push gateway down")
        self.sent.append({"title": title, "message": message, "tag": tag,
                          "actions": [a.action_id for a in actions]})

    def persistent_notification(self, title, message):
        self.problems.append((title, message))

    def confirm(self, *, tag, title, message):
        self.sent.append({"title": title, "message": message, "tag": tag,
                          "actions": []})

    def dismiss(self, tag):
        # Into its own list, not ``sent``. The distinction is the whole point:
        # a dismissal takes a card away and must not count as a push.
        self.cleared.append(tag)


class _Write:
    def __init__(self, *, explode: bool = False):
        self.calls = []
        self.lineups = []
        self.explode = explode

    def add_drop(self, *, add, drop, bid, waiver, scoring_period=None):
        if self.explode:
            raise RuntimeError("ESPN said no")
        self.calls.append({"add": add, "drop": drop, "bid": bid,
                           "waiver": waiver, "scoring_period": scoring_period})
        # ``shadow`` as well as ``ok``, because the rebalance that follows an
        # acquisition reads it to tell "ESPN accepted this" from "this never
        # left the process", and a stub missing the attribute would make every
        # shadow run look like a real one.
        return type("R", (), {"ok": True, "shadow": False})()

    def set_lineup(self, target, *, roster=None, scoring_period=None):
        self.lineups.append({"target": dict(target), "roster": roster,
                             "scoring_period": scoring_period})
        return type("R", (), {"ok": True, "shadow": False})()


class _Read:
    """The reads add/drop makes, each independently breakable.

    ``league_settings`` and ``roster`` are here for the post-acquisition
    rebalance only -- the add/drop decision itself is handed the roster the run
    already read. They return ``fresh_*`` rather than the harness's own copies
    so a test can prove the rebalance planned against the roster that *includes*
    the pickup, which is the entire point of re-reading.
    """

    def __init__(self, *, pool=(), pool_mean=45.0, pool_means=None, score=None,
                 used=None, fail=(), fresh_roster=(), fresh_settings=None):
        self.pool = list(pool)
        self.pool_mean = pool_mean
        # Per-player, for the tests about the *list* of swaps. One mean for the
        # whole pool makes every free agent interchangeable, so "best first"
        # would be a statement about tie-breaking rather than about ordering.
        self.pool_means = dict(pool_means or {})
        self.score = score
        self.used = used if used is not None else {}
        self.fail = set(fail)
        self.fresh_roster = list(fresh_roster)
        self.fresh_settings = fresh_settings
        self.asked = []

    def _maybe_fail(self, what):
        self.asked.append(what)
        if what in self.fail:
            raise RuntimeError("%s is unavailable" % what)

    def free_agents(self, limit: int = 200):
        self._maybe_fail("free_agents")
        return list(self.pool)

    def player_projections(self, ids_):
        self._maybe_fail("player_projections")
        # ``player -> split -> stat -> value``, and the split key has to be
        # ESPN's own: ``per_game_mean`` reads the *projected* season split and
        # nothing else, so a plausible-looking "season" key here would come
        # back as "no usable projection" and quietly value the whole pool at
        # zero -- which looks exactly like a pool nobody in it is good enough.
        return {
            int(p): {
                ids.SPLIT_SEASON_PROJECTED: {
                    PTS: self.pool_means.get(int(p), self.pool_mean)
                }
            }
            for p in ids_
        }

    def matchup_score(self, team_id=None, matchup_period=None):
        self._maybe_fail("matchup_score")
        return self.score

    def acquisitions_used(self, team_id=None):
        self._maybe_fail("acquisitions_used")
        return dict(self.used)

    def league_settings(self):
        self._maybe_fail("league_settings")
        return self.fresh_settings if self.fresh_settings is not None else settings()

    def roster(self):
        self._maybe_fail("roster")
        return list(self.fresh_roster)


class _League:
    def __init__(self, read, write=None, keeper=False):
        self.name = "Test League"
        self.ref = type(
            "Ref",
            (),
            {"league_id": 111111111, "team_id": 2, "keeper": keeper},
        )()
        self.read = read
        self.write = write or _Write()


def settings(**kw) -> LeagueSettings:
    base = dict(
        league_id=111111111,
        season_id=2027,
        name="Test League",
        scoring_type="H2H_POINTS",
        lineup_slot_counts={START: 2, BENCH: 1},
        lineup_slot_stat_limits=None,
        scoring_items={PTS: 1.0},
        categories=frozenset(),
        waiver_mode="WAIVERS_TRADITIONAL",
        faab_budget=None,
        roster_lock_mode="INDIVIDUAL_GAME",
        current_scoring_period=1,
        current_matchup_period=1,
        matchup_period_boundaries={},
    )
    base.update(kw)
    return LeagueSettings(**base)


def player(pid, *, name=None, slot=START, team=1, acq="FREEAGENT") -> RosterEntry:
    return RosterEntry(
        player_id=pid,
        name=name or "P%d" % pid,
        current_slot=slot,
        eligible_slots=frozenset((START, BENCH)),
        pro_team_id=team,
        injury_status="ACTIVE",
        lineup_locked=False,
        acquisition_status=acq,
    )


def projection(pid, mean=30.0) -> Projection:
    """One of our players, priced the way ``build_projections`` prices the pool.

    ``p_play`` is the real ACTIVE prior and not 1.0 on purpose. Our side of a
    swap is hand-built here while the pool side goes through
    ``build_projections``, so a rounder number here would make every roster
    player very slightly more startable than every pool player and the games
    comparison would come out a tie-that-isn't -- a fixture artefact that reads
    like a policy.
    """
    return Projection(
        player_id=pid,
        mean={PTS: mean},
        var={PTS: 0.0},
        p_play=P_PLAY_PRIOR["ACTIVE"],
        minutes=30.0,
        source="test",
        as_of=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )


#: A Monday, so period 1 is the first day of matchup period 1 and the three
#: periods below are the whole of the horizon. Anchoring on a Tuesday the way
#: the real 2026-27 season does would work too, but it would put the week
#: boundary in the middle of the fixture for no gain here.
WEEK_START = date(2026, 10, 19)


def calendar(periods=(1, 2, 3)) -> SeasonCalendar:
    """A three-day matchup period, built the way ESPN's is: one date per day.

    There is no "periods per matchup" to pass -- the week grid is a property of
    the calendar, not of the fixture, which is the point of
    ``SeasonCalendar``: a test cannot quietly declare a week length the live
    season doesn't have.
    """
    return SeasonCalendar.from_period_dates(
        {p: WEEK_START + timedelta(days=p - 1) for p in periods}
    )


class _Harness:
    """One run's worth of add/drop inputs, with the orchestrator bypassed.

    ``Autopilot.__init__`` opens an HTTP client and a SQLite file, neither of
    which this is about, so the instance is built without it and given only the
    collaborators these two methods touch.

    ``judge`` defaults to ``None``, which is the no-API-key path and the one
    every test in this file wants: the arithmetic's proposal reaches the user
    unmodified. ``tests/test_judge.py`` passes a stub to exercise the other.
    """

    def __init__(self, *, autonomy=False, shadow=False, read=None, write=None,
                 store=None, ha=None, st=None, margin=12.0, floor=3.0,
                 never_drop=(), judge=None, autonomy_lineup=True,
                 max_moves=1):
        self.store = store or _Store()
        self.ha = ha or _Ha()
        self.cfg = type("Cfg", (), {
            "never_drop": tuple(never_drop),
            "addrop_margin_pct": margin,
            "addrop_churn_floor": floor,
            # One by default, so a test about *a* decision gets exactly one
            # pass of the funnel and its stub pool is not drained by attempts
            # it never asked for. The tests about the list say so explicitly.
            "addrop_max_moves": max_moves,
            "autonomy_add_drop": autonomy,
            # Defaults to true, as the real option does. It gates the rebalance
            # that follows an acquisition, not the acquisition.
            "autonomy_lineup": autonomy_lineup,
            "shadow_mode": shadow,
        })()
        self.app = object.__new__(main_mod.Autopilot)
        self.app.cfg = self.cfg
        self.app.store = self.store
        self.app.ha = self.ha
        self.app.judge = judge
        # No network from a unit test. Tests that care about what the reviewer
        # was told about a player set this to return one.
        self.app.bio_source = lambda ids: {}

        self.settings = st or settings()
        # Three players for two starting slots and one bench, so the roster is
        # full and a pickup has to displace somebody.
        self.roster = [player(1), player(2, name="Weak"),
                       player(3, slot=BENCH)]
        self.projections = {1: projection(1, 60.0), 2: projection(2, 5.0),
                            3: projection(3, 30.0)}
        self.values = {1: 60.0, 2: 5.0, 3: 30.0}
        self.lg = _League(
            read if read is not None else _Read(
                pool=[player(90, name="Better", slot=BENCH)]
            ),
            write,
        )
        self.cal = calendar()
        # ``settings_now`` consults the season calendar to correct ESPN's
        # lagging scoring period, so the cache it reads has to exist even on an
        # instance built without ``__init__``. Pre-seeded rather than left
        # unset, because the alternative is a live fetch from a test.
        self.app._calendar = self.cal
        self.schedule = {1: {1: 1, 2: 1, 3: 1}}

    def plan(self, target=None):
        target = target or {1: START, 2: START, 3: BENCH}
        return pipeline.LineupPlan(
            target=target, moves=(), lam=0.0, utilization=float("inf"),
            values=dict(self.values), notes=(),
        )

    def consider_all(self, target=None, injuries=None, lineup_written=True):
        # The score is read by the caller rather than by ``_consider_addrops``
        # -- ``run_league`` needs it for the status-page scoreboard too, and one
        # request serves both. Read it here the same way, so a test about a
        # failing score read is still testing the path the run takes.
        read_notes = []
        matchup = self.app._read_matchup(self.lg, 1, read_notes)
        # The spy is scoped to the call under test. Several tests below assert
        # that a decision was declined "without spending a single ESPN read",
        # and that claim is about ``_consider_addrops``; the score above is the
        # caller's, charged to the run whether or not add/drop is considered.
        del self.lg.read.asked[:]
        decisions, notes, declined = self.app._consider_addrops(
            self.lg, self.settings, self.roster, self.projections,
            self.plan(target),
            cal=self.cal, matchup_period=1, schedule=self.schedule,
            injuries=injuries, matchup=matchup, lineup_written=lineup_written,
        )
        # Kept on the harness rather than returned: the swaps a run turned
        # down are what one small group of tests is about, and threading a
        # third value through forty call sites would make every other test
        # mention something it does not care about.
        self.declined = list(declined)
        return decisions, read_notes + notes

    def consider(self, target=None, injuries=None, lineup_written=True):
        """The best swap only, or ``None``, plus every note.

        Most of the tests below are about one decision -- what blocks it, what
        it says, what it writes -- and reading ``[0]`` in each of them would be
        noise obscuring which tests are actually about the list.
        """
        decisions, notes = self.consider_all(
            target=target, injuries=injuries, lineup_written=lineup_written)
        return (decisions[0] if decisions else None), notes

    def offer(self, decision):
        return self.app._offer_addrop("RUN1", self.lg, decision, 1)


# ---------------------------------------------------------------------------
# every read is optional
# ---------------------------------------------------------------------------


class TestDegradation(unittest.TestCase):
    """*"the lineup is already applied"* -- so nothing here may raise.

    Each of these would have been a dead run if add/drop ran before the lineup
    write instead of after it.
    """

    def test_a_failed_pool_read_is_a_note_not_an_exception(self):
        h = _Harness(read=_Read(fail=("free_agents",)))
        decision, notes = h.consider()
        self.assertIsNone(decision)
        self.assertIn("pool read failed", " ".join(notes))
        self.assertEqual(h.store.rows, [])

    def test_a_failed_pool_projection_read_is_a_note(self):
        h = _Harness(read=_Read(
            pool=[player(90, name="Better")], fail=("player_projections",),
        ))
        decision, notes = h.consider()
        self.assertIsNone(decision)
        self.assertIn("pool projections failed", " ".join(notes))

    def test_a_failed_score_read_still_decides(self):
        """The score can only waive the churn floor, so losing it costs a
        waiver we might not have wanted, not the decision."""
        h = _Harness(read=_Read(
            pool=[player(90, name="Better")], fail=("matchup_score",),
        ))
        decision, notes = h.consider()
        self.assertIsNotNone(decision)
        self.assertTrue(decision.proposed)
        self.assertIn("matchup score unavailable", " ".join(notes))

    def test_a_failed_acquisition_counter_read_still_decides(self):
        """Degrading to "unlimited" is the right direction: the worst case is a
        proposal ESPN refuses, against a cap it is itself enforcing."""
        h = _Harness(read=_Read(
            pool=[player(90, name="Better")], fail=("acquisitions_used",),
        ))
        decision, notes = h.consider()
        self.assertTrue(decision.proposed)
        self.assertIn("acquisition counter unavailable", " ".join(notes))

    def test_no_matchup_row_is_noted_and_is_not_a_tie(self):
        h = _Harness(read=_Read(pool=[player(90, name="Better")], score=None))
        _decision, notes = h.consider()
        self.assertIn("no matchup row this period", " ".join(notes))

    def test_a_category_league_declines_rather_than_inventing_points(self):
        h = _Harness(st=settings(scoring_type="H2H_CATEGORY"))
        decision, notes = h.consider()
        self.assertIsNone(decision)
        self.assertIn("arithmetic is in points", " ".join(notes))
        # And it declined before spending a single ESPN read on it.
        self.assertEqual(h.lg.read.asked, [])

    def test_an_untrustworthy_calendar_declines(self):
        """Without a horizon there is no way to count a pickup's games, which
        is the one thing the requirement called the biggest consideration."""
        h = _Harness()
        h.cal = None
        decision, notes = h.consider()
        self.assertIsNone(decision)
        self.assertIn("no trustworthy horizon", " ".join(notes))
        self.assertEqual(h.lg.read.asked, [])


# ---------------------------------------------------------------------------
# what gets protected
# ---------------------------------------------------------------------------


class TestStartersComeFromThePlan(unittest.TestCase):
    def test_the_protected_starters_are_this_runs_not_espns(self):
        """P2 sits on ESPN's roster in a starting slot and the optimizer is
        about to bench him, so he is droppable. P3 is the reverse: benched on
        ESPN and started by this run, so he is not -- his game may be tipping
        off by the time a tap arrives.
        """
        h = _Harness(read=_Read(pool=[player(90, name="Better")]))
        # P3 started, P2 benched: the opposite of the current roster slots.
        decision, _notes = h.consider(
            target={1: START, 3: START, 2: BENCH}
        )
        self.assertTrue(decision.proposed)
        self.assertEqual(decision.drop.name, "Weak")

        # And with P2 started instead, he is off the table and the swap goes
        # after P3 instead -- the protection redirects the drop, it does not
        # switch add/drop off.
        decision, notes = h.consider(target={1: START, 2: START, 3: BENCH})
        self.assertEqual(decision.drop.name, "P3")
        self.assertIn("Weak (starting today)", " ".join(notes))

    def test_never_drop_reaches_the_decision_from_config(self):
        h = _Harness(read=_Read(pool=[player(90, name="Better")]),
                     never_drop=("weak",))
        decision, notes = h.consider(target={1: START, 3: START, 2: BENCH})
        self.assertIn("never_drop", " ".join(notes))
        self.assertNotEqual(
            decision.drop.name if decision and decision.drop else "", "Weak"
        )

    def test_the_configured_thresholds_reach_the_decision(self):
        """A floor nobody can clear proves the number is actually plumbed
        rather than defaulted inside ``plan_addrop``."""
        h = _Harness(read=_Read(pool=[player(90, name="Better")]),
                     floor=10_000.0)
        decision, notes = h.consider()
        self.assertIsNone(decision)
        self.assertIn("10000.0-point churn floor", " ".join(notes))


class TestTheRosterAddDropSeesIsThePostWriteOne(unittest.TestCase):
    """Add/drop runs second. The slots it was handed are already stale.

    Most of that staleness is harmless -- which bench a man sits on does not
    change whether he can be dropped -- but IR is not an active roster spot,
    so a player the lineup just activated has taken one, and counting his old
    IR slot reports room that is gone.

    Live, 2026-10-28, both leagues in the same run: the lineup brought AJ
    Dybantsa off IR at 09:36:01, two seconds later add/drop counted 12 of 13
    and offered Toumani Camara into the open spot, and forty minutes later the
    approval reached the preflight and ESPN's own count said 13 of 13 --
    "roster is full (13/13 active spots) and no drop was paired with this add".
    """

    def roster(self):
        # Two starting slots and a bench, with the third man parked on IR: two
        # of three active spots used, so there is room for a free pickup.
        return [player(1), player(2, name="Weak"),
                player(3, name="Hurt", slot=ids.IR_SLOT)]

    def test_activating_a_player_off_ir_closes_the_open_spot(self):
        before = self.roster()
        cap = ids.active_roster_cap(settings().lineup_slot_counts)
        self.assertLess(ids.active_roster_use(e.current_slot for e in before),
                        cap)
        after = main_mod._after_lineup(
            before, main_mod.pipeline.LineupPlan(
                target={1: START, 2: BENCH, 3: START}, moves=(), lam=0.0,
                utilization=float("inf"), values={}, notes=()))
        self.assertEqual(cap,
                         ids.active_roster_use(e.current_slot for e in after))

    def test_it_does_not_mutate_the_roster_the_rest_of_the_run_holds(self):
        before = self.roster()
        main_mod._after_lineup(before, main_mod.pipeline.LineupPlan(
            target={1: START, 2: BENCH, 3: START}, moves=(), lam=0.0,
            utilization=float("inf"), values={}, notes=()))
        self.assertEqual(ids.IR_SLOT, before[2].current_slot)

    def test_a_player_the_plan_says_nothing_about_keeps_his_slot(self):
        after = main_mod._after_lineup(
            self.roster(), main_mod.pipeline.LineupPlan(
                target={1: START}, moves=(), lam=0.0,
                utilization=float("inf"), values={}, notes=()))
        self.assertEqual(ids.IR_SLOT, after[2].current_slot)

    def test_the_unpaired_add_is_offered_before_the_activation_and_not_after(self):
        """The whole bug, at the layer that decides it."""
        h = _Harness(read=_Read(pool=[player(90, name="Better", slot=BENCH)]))
        h.roster = self.roster()
        h.projections = {1: projection(1, 60.0), 2: projection(2, 5.0),
                         3: projection(3, 30.0)}
        h.values = dict(h.projections and {1: 60.0, 2: 5.0, 3: 30.0})
        # The write failed, so he really is still on IR and the spot is open.
        stale, _ = h.consider(target={1: START, 2: BENCH, 3: START},
                              lineup_written=False)
        self.assertIsNotNone(stale)
        self.assertIsNone(stale.drop, "an open spot means nothing goes out")

        # The write landed, so it is not.
        fresh, _ = h.consider(target={1: START, 2: BENCH, 3: START})
        if fresh is not None:
            self.assertIsNotNone(
                fresh.drop,
                "the spot is taken, so any add has to pair with a drop")

    def test_the_correction_is_on_by_default_so_it_cannot_be_forgotten(self):
        """A caller who omits the flag must not get the 2026-10-28 behaviour
        back. Wrong in the safe direction is the point of the default."""
        import inspect
        sig = inspect.signature(main_mod.Autopilot._consider_addrops)
        self.assertIs(True, sig.parameters["lineup_written"].default)


# ---------------------------------------------------------------------------
# asking, and not asking
# ---------------------------------------------------------------------------


class TestOffering(unittest.TestCase):
    def _decided(self, **kw):
        h = _Harness(read=_Read(pool=[player(90, name="Better", acq="WAIVERS")]),
                     **kw)
        decision, _notes = h.consider(target={1: START, 3: START, 2: BENCH})
        self.assertTrue(decision.proposed)
        return h, decision

    def test_without_autonomy_it_asks_and_writes_nothing(self):
        h, decision = self._decided()
        h.offer(decision)

        self.assertEqual(len(h.store.rows), 1)
        row = h.store.rows[0]
        self.assertEqual(row.kind, "ADD_DROP")
        self.assertFalse(row.auto_approve)
        # Nothing claimed and nothing written: the move waits for a tap.
        self.assertEqual(h.store.claims, [])
        self.assertEqual(h.lg.write.calls, [])

        sent = h.ha.sent[0]
        self.assertIn("approve add/drop?", sent["title"])
        self.assertEqual(
            sent["actions"],
            ["RUN1:ADD_DROP:1:APPROVE", "RUN1:ADD_DROP:1:REJECT"],
        )

    def test_with_autonomy_it_writes_and_still_journals(self):
        """Autonomy changes who approves, never whether there is a record."""
        h, decision = self._decided(autonomy=True)
        h.offer(decision)

        row = h.store.rows[0]
        self.assertTrue(row.auto_approve)
        self.assertTrue(row.completed)
        self.assertEqual(h.store.claims, [row.id])
        self.assertEqual(len(h.lg.write.calls), 1)
        # No buttons on a move that has already happened.
        self.assertEqual(h.ha.sent[0]["actions"], [])

    def test_an_unclaimable_proposal_is_not_written_twice(self):
        """The claim is what stops a tap on a stale notification and a
        scheduled run from both executing the same swap."""
        h, decision = self._decided(autonomy=True, store=_Store(claimable=False))
        h.offer(decision)
        self.assertEqual(h.lg.write.calls, [])

    def test_a_failed_write_is_recorded_and_surfaced(self):
        h, decision = self._decided(autonomy=True, write=_Write(explode=True))
        h.offer(decision)
        row = h.store.rows[0]
        self.assertFalse(row.completed)
        self.assertIn("ESPN said no", row.error)
        self.assertTrue(h.ha.problems)

    def test_a_failed_push_leaves_the_proposal_approvable(self):
        """The database row is the record, not the notification. Losing the
        push costs the tap, not the move -- the status page still lists it."""
        h, decision = self._decided(ha=_Ha(explode=True))
        h.offer(decision)
        self.assertEqual(len(h.store.rows), 1)
        self.assertEqual(h.store.rows[0].kind, "ADD_DROP")

    def test_the_payload_carries_both_names_and_no_bid(self):
        """``web.py`` renders the card from this payload alone, so a name
        missing here means a second ESPN read at approval time to answer a
        question we already knew the answer to."""
        h, decision = self._decided()
        h.offer(decision)
        payload = h.store.rows[0].payload
        self.assertEqual(payload["add"], 90)
        self.assertEqual(payload["add_name"], "Better")
        self.assertEqual(payload["drop_name"], "Weak")
        self.assertIsNotNone(payload["drop"])
        # Read off the player, not the league (ADR-017).
        self.assertTrue(payload["waiver"])
        # Not 0, which would read as a deliberate minimum bid.
        self.assertIsNone(payload["bid"])
        self.assertEqual(payload["matchup_period"], 1)
        self.assertIn(payload["reason"], ("quality", "games", "open-spot"))

    def test_shadow_mode_says_so_in_the_title(self):
        h, decision = self._decided(shadow=True)
        h.offer(decision)
        self.assertIn("[shadow]", h.ha.sent[0]["title"])

    def test_the_rationale_is_what_the_user_is_shown(self):
        h, decision = self._decided()
        h.offer(decision)
        self.assertEqual(h.store.rows[0].rationale, decision.rationale)
        self.assertEqual(h.ha.sent[0]["message"], decision.rationale)
        self.assertAlmostEqual(h.store.rows[0].value_delta, decision.gain)


class TestScoreIsReadNotIgnored(unittest.TestCase):
    def test_being_behind_by_less_than_the_gain_reaches_the_decision(self):
        """The one thing the score is allowed to do, end to end: the run reads
        it, and a move that could flip the scoreboard skips the churn floor."""
        behind = MatchupScore(
            matchup_period=1, my_team_id=2, my_points=99.0,
            opp_team_id=6, opp_name="Them", opp_points=100.0,
        )
        # A pickup barely better than the man going out: 33 a game against his
        # 29, over a slot priced at 30, is worth +8.8 for the week -- under the
        # 10-point floor on its own, and far more than a one-point deficit.
        h = _Harness(
            read=_Read(pool=[player(90, name="Hair Better")], pool_mean=33.0,
                       score=behind),
            floor=10.0,
        )
        h.values[2] = 29.0
        h.projections[2] = projection(2, 29.0)
        decision, notes = h.consider(target={1: START, 3: START, 2: BENCH})
        self.assertIn("churn floor is waived", " ".join(notes))
        self.assertTrue(decision.proposed)


# ---------------------------------------------------------------------------
# the lineup follows the roster
# ---------------------------------------------------------------------------


def _notes(*pids, p_play=0.9, status="ACTIVE", source="espn"):
    """``player_id -> AvailabilityNote``, as ``build_projections`` returns."""
    return {
        pid: projections_mod.AvailabilityNote(
            player_id=pid, status=status, status_source=source, p_play=p_play)
        for pid in pids
    }


def lineup_work(plan, **kw):
    """A ``LineupWork`` with everything but the plan stubbed out.

    Only ``plan`` and ``roster``-shaped arguments change what the rebalance
    does; the rest of the dataclass is what ``run_league`` reports on, and
    filling it here would be inventing evidence for assertions nobody makes.
    """
    base = dict(
        plan=plan, projections={}, availability={}, skipped=(), overrides={},
        injuries=None, cal=None, matchup_period=1, schedule={},
        games_used=None, notes=(), sources_used={}, sources_missing={},
    )
    base.update(kw)
    return main_mod.LineupWork(**base)


def after_the_swap(**kw):
    """A harness whose *next* roster read includes the pickup.

    Four players for two starting slots, the new man better than one of the
    incumbents -- so an optimal lineup has to move somebody, which is what
    makes "did it rebalance" an answerable question.
    """
    h = _Harness(**kw)
    h.lg.read.fresh_roster = [
        player(1), player(2, name="Weak"), player(3, slot=BENCH),
        player(90, name="Better", slot=BENCH),
    ]
    return h


def swap_payload(**kw):
    """The journalled payload of an executed add/drop, as ``_offer_addrop``
    writes it and the approval loop reads it back."""
    base = {"add": 90, "add_name": "Better", "drop": 2, "drop_name": "Weak",
            "bid": None, "waiver": False, "matchup_period": 1,
            "reason": "quality"}
    base.update(kw)
    return base


def write_result(ok=True, shadow=False):
    return type("R", (), {"ok": ok, "shadow": shadow})()


def moved_plan():
    """A feasible plan that starts the pickup over the weak incumbent."""
    return pipeline.LineupPlan(
        target={1: START, 90: START, 2: BENCH, 3: BENCH},
        moves=(
            Move(kind="LINEUP", player_id=90, player_name="Better",
                 from_slot=BENCH, to_slot=START, value_delta=15.0),
            Move(kind="LINEUP", player_id=2, player_name="Weak",
                 from_slot=START, to_slot=BENCH, value_delta=-5.0),
        ),
        lam=0.0, utilization=float("inf"),
        values={1: 60.0, 2: 5.0, 3: 30.0, 90: 45.0}, notes=(),
    )


class TestRebalanceAfterTheSwap(unittest.TestCase):
    """*"remember when i make an add, we have to potentially rebalance the
    lineup"*.

    The lineup was written earlier in the run, against a roster that did not
    contain the pickup. Every test here is about one of two failures: starting
    the new man when the roster really did change, or claiming to have done so
    when it did not.

    These call ``rebalance_after_swap`` directly rather than through
    ``_offer_addrop``, because the guards are about the *result* of a write and
    the harness's own pool produces exactly one of the several shapes a write
    result can take.
    """

    # -- the roster did not actually change -------------------------------

    def test_a_failed_write_is_not_rebalanced(self):
        """Nothing moved, so there is nothing to re-plan -- and re-planning
        anyway would write a lineup for a roster that never existed."""
        h = after_the_swap()
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result(ok=False))
        self.assertEqual(h.lg.read.asked, [])
        self.assertEqual(h.lg.write.lineups, [])

    def test_a_missing_result_is_not_rebalanced(self):
        h = after_the_swap()
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(), None)
        self.assertEqual(h.lg.write.lineups, [])

    def test_shadow_mode_does_not_rebalance(self):
        """The swap never left the process, so the roster on ESPN is the one
        the lineup was already optimized against."""
        h = after_the_swap()
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result(shadow=True))
        self.assertEqual(h.lg.read.asked, [])
        self.assertEqual(h.lg.write.lineups, [])

    def test_a_pending_waiver_claim_does_not_rebalance(self):
        """A claim is not an acquisition. He is not ours until it processes,
        and the drop has not happened either -- so the *current* roster is
        unchanged and the lineup standing against it is still the right one."""
        h = after_the_swap()
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(waiver=True),
                                   write_result())
        self.assertEqual(h.lg.read.asked, [])
        self.assertEqual(h.lg.write.lineups, [])

    def test_autonomy_lineup_off_leaves_the_lineup_alone(self):
        """Approving a swap says who should be on the roster, not who should be
        in the starting five. The lineup flag is the answer to the second."""
        h = after_the_swap(autonomy_lineup=False)
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result())
        self.assertEqual(h.lg.read.asked, [])
        self.assertEqual(h.lg.write.lineups, [])
        self.assertEqual(h.store.rows, [])

    # -- the roster changed and something went wrong ----------------------

    def test_a_failed_re_read_is_surfaced_and_does_not_raise(self):
        """The swap already happened. A failure here must not look like a
        failed swap -- but it must not be silent either, because the lineup now
        reflects a roster that no longer exists."""
        h = after_the_swap()
        h.lg.read.fail = {"roster"}
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result())
        self.assertEqual(h.lg.write.lineups, [])
        self.assertEqual(len(h.ha.problems), 1)
        title, message = h.ha.problems[0]
        self.assertIn("not rebalanced", title)
        self.assertIn("Better", message)

    def test_an_infeasible_plan_is_loud(self):
        """Everywhere else an unusable plan means "yesterday's lineup stands",
        which is safe. Here it may mean starting a player who was just
        dropped."""
        h = after_the_swap()
        plan = dataclasses.replace(moved_plan(), feasible=False)
        h.app.plan_from_roster = lambda *a, **kw: lineup_work(plan)
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result())
        self.assertEqual(h.lg.write.lineups, [])
        self.assertTrue(h.ha.problems)

    def test_no_projections_at_all_is_loud(self):
        """``plan`` is ``None`` when ESPN projected nobody -- a refusal to
        plan, not a plan of zeroes. Same consequence as infeasible."""
        h = after_the_swap()
        h.app.plan_from_roster = lambda *a, **kw: lineup_work(None)
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result())
        self.assertEqual(h.lg.write.lineups, [])
        self.assertTrue(h.ha.problems)

    def test_a_failure_applying_the_lineup_does_not_escape(self):
        """One caller is the approval loop. An exception escaping this would
        stop every *other* pending approval from being executed, which is a
        much worse outcome than one player on the bench for a night."""
        h = after_the_swap()
        h.app.plan_from_roster = lambda *a, **kw: lineup_work(moved_plan())

        def boom(*a, **kw):
            raise RuntimeError("database is locked")

        h.app._apply_lineup = boom
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result())
        self.assertTrue(h.ha.problems)

    # -- the roster changed and the lineup follows ------------------------

    def test_an_already_optimal_lineup_writes_nothing(self):
        """A pickup who belongs on the bench belongs on the bench. No moves
        means no write and no notification -- not "rebalanced: 0 moves"."""
        h = after_the_swap()
        unchanged = dataclasses.replace(moved_plan(), moves=())
        h.app.plan_from_roster = lambda *a, **kw: lineup_work(unchanged)
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result())
        self.assertEqual(h.lg.write.lineups, [])
        self.assertEqual(h.store.rows, [])
        self.assertEqual(h.ha.sent, [])

    def test_it_plans_against_the_roster_it_just_read(self):
        """The whole point. Planning against the roster the run started with
        would re-derive the lineup that does not contain the pickup."""
        h = after_the_swap()
        seen = {}

        def record(lg, settings, roster):
            seen["roster"] = list(roster)
            seen["settings"] = settings
            return lineup_work(moved_plan())

        h.app.plan_from_roster = record
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result())
        self.assertIn(90, [e.player_id for e in seen["roster"]])
        self.assertIsNotNone(seen["settings"])

    def test_the_new_lineup_is_written_and_journalled_under_the_run(self):
        """Journalled under the run that proposed the swap, because that is the
        decision this follows from -- even when the approval tap came hours
        later."""
        h = after_the_swap()
        h.app.plan_from_roster = lambda *a, **kw: lineup_work(moved_plan())
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result())

        self.assertEqual(len(h.lg.write.lineups), 1)
        wrote = h.lg.write.lineups[0]
        self.assertEqual(wrote["target"][90], START)
        self.assertEqual(wrote["target"][2], BENCH)
        # The fresh roster, not the one the run started with: ``set_lineup``
        # needs it to find the slot ids ESPN wants.
        self.assertIn(90, [e.player_id for e in wrote["roster"]])

        self.assertEqual(len(h.store.rows), 1)
        row = h.store.rows[0]
        self.assertEqual(row.kind, "LINEUP")
        self.assertEqual(row.run_id, "RUN1")
        self.assertTrue(row.completed)
        self.assertEqual(h.ha.problems, [])

    def test_the_journalled_payload_names_every_move(self):
        """The status page shows the moves one by one and cannot recover them
        from ``target``: the difference is against the roster as it was, which
        is not journalled. So they are written down at the point of the write."""
        h = after_the_swap()
        h.app.plan_from_roster = lambda *a, **kw: lineup_work(moved_plan())
        h.app.rebalance_after_swap("RUN1", h.lg, swap_payload(),
                                   write_result())

        moves = h.store.rows[0].payload["moves"]
        self.assertEqual(
            [(m["name"], m["from"], m["to"]) for m in moves],
            [("Better", "BE", "UTIL"), ("Weak", "UTIL", "BE")],
        )


class TestTheApprovalLoopRebalancesToo(unittest.TestCase):
    """The tap path, which is the default one.

    ``autonomy_add_drop`` is false out of the box, so in normal use the swap
    does not happen during the run -- it happens when the user taps Approve,
    which may be hours later. That is exactly why the rebalance cannot wait for
    the next scheduled run: by then the games it was meant to win are over.
    """

    def _h(self, **kw):
        h = after_the_swap(**kw)
        # The real one reads the run's league out of SQLite; there is no
        # database here, and whose roster a tap lands on is ``_execute``'s
        # question, covered by ``tests/test_safety.py``.
        h.app.league_for_run = lambda run_id: h.lg
        h.app.plan_from_roster = lambda *a, **kw: lineup_work(moved_plan())
        return h

    def _queue(self, h, kind, payload):
        row = h.store.propose("RUN1", kind, payload, rationale="r",
                              auto_approve=False)
        h.store.queued.append(row)
        return row

    def test_an_approved_swap_starts_the_man_it_just_added(self):
        h = self._h()
        swap = self._queue(h, "ADD_DROP", swap_payload())
        asyncio.run(h.app.execute_approved())
        self.assertTrue(swap.completed)
        self.assertEqual(len(h.lg.write.calls), 1)
        self.assertEqual(len(h.lg.write.lineups), 1)
        self.assertEqual(h.lg.write.lineups[0]["target"][90], START)

    def test_approving_a_swap_takes_the_card_down_and_says_nothing(self):
        """*"i get a notification when i accept the add drop request. dont need
        that."*

        It was worse than redundant. The confirmation went out under
        ``autopilot-<run_id>`` while the card it was meant to replace carried
        ``autopilot-addrop-<league>-<proposal>``, so iOS treated them as
        unrelated: a second push arrived *and* the original kept offering
        Approve and Reject for a swap that had already gone through.
        """
        h = self._h()
        swap = self._queue(h, "ADD_DROP", swap_payload())
        asyncio.run(h.app.execute_approved())
        self.assertTrue(swap.completed)

        self.assertEqual(
            h.ha.cleared,
            [bridge_mod.addrop_tag(h.lg.ref.league_id, swap.action_id)],
        )
        # The only push is the rebalance, which is news he did not make
        # himself. Nothing titled like a receipt.
        self.assertEqual(len(h.ha.sent), 1)
        self.assertNotIn("done", h.ha.sent[0]["title"].lower())

    def test_a_swap_that_espn_refused_leaves_the_card_alone(self):
        """The buttons must survive a failed write. The move did not happen, so
        the question is still open and the card is still the way to answer it --
        clearing it would leave the failure notification as the only trace of a
        decision the user can no longer act on."""
        h = self._h(write=_Write(explode=True))
        h.app.league_for_run = lambda run_id: h.lg
        self._queue(h, "ADD_DROP", swap_payload())
        asyncio.run(h.app.execute_approved())
        self.assertEqual(h.ha.cleared, [])
        self.assertTrue(h.ha.problems)

    def test_an_approved_lineup_is_not_rebalanced_again(self):
        """A LINEUP proposal *is* the lineup. Re-planning after writing it
        would be a second write of the same decision, and the roster it was
        computed against has not changed."""
        h = self._h()
        self._queue(h, "LINEUP", {"target": {"1": START, "3": BENCH}})
        asyncio.run(h.app.execute_approved())
        self.assertEqual(len(h.lg.write.lineups), 1)
        # No second proposal, which is what a rebalance would have journalled.
        self.assertEqual(len(h.store.rows), 1)

    def test_a_refused_swap_is_not_rebalanced(self):
        """ESPN said no, so the roster is unchanged -- and the loop must carry
        on to the next approval rather than die on this one."""
        h = self._h(write=_Write(explode=True))
        h.app.league_for_run = lambda run_id: h.lg
        h.lg.read.fresh_roster = [player(1), player(90, name="Better")]
        swap = self._queue(h, "ADD_DROP", swap_payload())
        asyncio.run(h.app.execute_approved())
        self.assertFalse(swap.completed)
        self.assertEqual(h.lg.write.lineups, [])
        self.assertTrue(h.ha.problems)


# ---------------------------------------------------------------------------
# several swaps a run
# ---------------------------------------------------------------------------


def funnel(*, max_moves=3, pool_means=None, st=None, used=None, **kw):
    """A roster with room for several swaps, and a pool to make them from.

    The default ``_Harness`` holds one droppable player and one free agent,
    which is everything the single-decision tests need and nothing the list
    needs: with one of each there is no second swap to find, so "it stopped
    after one" would be true of a funnel that was never built.

    Three starting slots and three bench, all six filled, so every pickup has
    to pay for itself with a drop -- the open-spot path is a different branch
    and is covered where it is decided.

    The shape is dictated by the arithmetic rather than chosen for brevity.
    Only a player who can *start* earns anything, so a pickup has to beat the
    cheapest occupied starting slot, and that price rises with every swap the
    simulation accepts: after two pickups the good slots are held by the
    pickups. A pool that descends slowly enough to stay above the rising price
    is therefore the only pool from which three swaps exist to be found.
    """
    means = pool_means or {90: 70.0, 91: 60.0, 92: 50.0}
    h = _Harness(
        max_moves=max_moves,
        st=st or settings(lineup_slot_counts={START: 3, BENCH: 3}),
        read=_Read(
            pool=[player(p, name="FA%d" % p, slot=BENCH) for p in sorted(means)],
            pool_means=means,
            used=used,
        ),
        **kw
    )
    h.roster = [player(1), player(2), player(3),
                player(4, name="Weak", slot=BENCH),
                player(5, name="Weaker", slot=BENCH),
                player(6, name="Weakest", slot=BENCH)]
    # The bench ascends while the pool descends, so swap one is the biggest
    # gain and swap three the smallest. Strictly decreasing rather than merely
    # non-increasing, so "best first" is falsifiable.
    means_by_pid = {1: 30.0, 2: 20.0, 3: 10.0, 4: 1.0, 5: 2.0, 6: 3.0}
    h.projections = {p: projection(p, m) for p, m in means_by_pid.items()}
    h.values = dict(means_by_pid)
    return h


def funnel_target():
    return {1: START, 2: START, 3: START, 4: BENCH, 5: BENCH, 6: BENCH}


def open_funnel(*, max_moves=3, pool_means=None, **kw):
    """A roster with a free spot and a deep pool, for counting reviews.

    ``funnel`` cannot be used to count how many candidates the reviewer is
    shown, because every swap there costs a drop and each block retires the
    name it objected to: after three refusals there is nobody left to drop and
    the loop ends for want of a drop candidate rather than for want of looks.

    Here the roster is two players against a cap of six, so the branch taken is
    the open-spot one, nothing goes out, and a block retires only the pickup.
    The pool therefore survives being refused and the loop ends where the
    review budget says, which is the thing under test.
    """
    means = pool_means or {90: 70.0, 91: 60.0, 92: 50.0,
                           93: 40.0, 94: 30.0, 95: 20.0}
    h = _Harness(
        max_moves=max_moves,
        st=settings(lineup_slot_counts={START: 3, BENCH: 3}),
        read=_Read(
            pool=[player(p, name="FA%d" % p, slot=BENCH) for p in sorted(means)],
            pool_means=means,
        ),
        **kw
    )
    h.roster = [player(1), player(2)]
    h.projections = {1: projection(1, 30.0), 2: projection(2, 20.0)}
    h.values = {1: 30.0, 2: 20.0}
    return h


def open_funnel_target():
    return {1: START, 2: START}


class TestSeveralSwapsARun(unittest.TestCase):
    """*"i also think you should recomend multiple adds"*.

    A run that sees three worthwhile moves and shows the best one leaves the
    other two until tomorrow, and in a four-day week tomorrow is a quarter of
    it. The list is bounded, ordered, and each entry is priced against the
    roster the entries above it leave behind.
    """

    def test_three_worthwhile_swaps_become_three_offers(self):
        decisions, _notes = funnel().consider_all(target=funnel_target())
        self.assertEqual(len(decisions), 3)
        self.assertTrue(all(d.proposed for d in decisions))

    def test_best_first(self):
        decisions, _notes = funnel().consider_all(target=funnel_target())
        gains = [d.gain for d in decisions]
        self.assertEqual(gains, sorted(gains, reverse=True))

    def test_no_pair_is_offered_twice(self):
        """The bug this whole mechanism exists to avoid: three cards naming
        three different pickups and the same single man going out, of which
        exactly one can be approved."""
        decisions, _notes = funnel().consider_all(target=funnel_target())
        self.assertEqual(
            [d.add.name for d in decisions], ["FA90", "FA91", "FA92"]
        )
        self.assertEqual(
            [d.drop.name for d in decisions], ["Weak", "Weaker", "Weakest"]
        )

    def test_nobody_is_added_and_then_dropped_in_the_same_run(self):
        """Two cards that cancel out, and one acquisition spent on nothing."""
        decisions, _notes = funnel().consider_all(target=funnel_target())
        added = {d.add.name for d in decisions}
        self.assertFalse(added & {d.drop.name for d in decisions if d.drop})

    def test_the_option_is_the_ceiling(self):
        decisions, _notes = funnel(max_moves=2).consider_all(
            target=funnel_target()
        )
        self.assertEqual(len(decisions), 2)

    def test_one_is_the_old_behaviour_exactly(self):
        decisions, _notes = funnel(max_moves=1).consider_all(
            target=funnel_target()
        )
        self.assertEqual(len(decisions), 1)

    def test_the_list_ends_when_the_pool_does(self):
        """Asking for three when only one pickup is worth having. The notes of
        the attempt that found nothing are what explain where it stopped."""
        h = funnel(pool_means={90: 60.0})
        decisions, notes = h.consider_all(target=funnel_target())
        self.assertEqual(len(decisions), 1)
        self.assertTrue(notes)

    def test_the_acquisition_budget_caps_the_real_total(self):
        """Two left in the period, so the third attempt is refused by the same
        guard that refuses it on a second run of the day -- the simulated
        counter advances as the list grows."""
        h = funnel(
            st=settings(lineup_slot_counts={START: 3, BENCH: 3},
                        acquisition_limit=2.0),
            used={1: 0},
        )
        decisions, notes = h.consider_all(target=funnel_target())
        self.assertEqual(len(decisions), 2)
        self.assertIn("no acquisitions left", " ".join(notes))

    def test_a_counter_ESPN_would_not_give_us_does_not_stop_the_list(self):
        """``acquisitions_used`` unavailable means "unknown", not "spent". The
        simulated count has nothing to advance, and the list runs to the
        option's ceiling as it would with no limit at all."""
        h = funnel()
        h.lg.read.fail = {"acquisitions_used"}
        decisions, notes = h.consider_all(target=funnel_target())
        self.assertEqual(len(decisions), 3)
        self.assertIn("acquisition counter unavailable", " ".join(notes))

    def test_the_budget_is_reported_once_not_counted_down(self):
        h = funnel(
            st=settings(lineup_slot_counts={START: 3, BENCH: 3},
                        acquisition_limit=9.0),
            used={1: 0},
        )
        _decisions, notes = h.consider_all(target=funnel_target())
        self.assertEqual(
            sum(1 for n in notes if "acquisition(s) left" in n), 1
        )

    def test_the_second_card_says_its_number_assumes_the_first(self):
        """The gain was computed against a roster that only exists if the swap
        above it goes through, and a user tapping this one in isolation is
        entitled to know that."""
        decisions, _notes = funnel().consider_all(target=funnel_target())
        self.assertNotIn("Priced as the", decisions[0].rationale)
        self.assertIn("second move of this run", decisions[1].rationale)
        self.assertIn("the swap above it is approved", decisions[1].rationale)
        self.assertIn("the 2 swaps above it are approved",
                      decisions[2].rationale)

    def test_the_first_card_is_priced_against_the_real_roster(self):
        """Not against a simulation of anything. Whatever the list does, the
        best swap has to be the one a single-swap run would have offered."""
        many, _n1 = funnel().consider_all(target=funnel_target())
        one, _n2 = funnel(max_moves=1).consider_all(target=funnel_target())
        self.assertEqual(many[0].add.name, one[0].add.name)
        self.assertEqual(many[0].drop.name, one[0].drop.name)
        self.assertAlmostEqual(many[0].gain, one[0].gain)


class TestEachOfferStandsAlone(unittest.TestCase):
    """Three swaps are three approvals, not one basket."""

    def test_three_proposals_and_three_notifications(self):
        h = funnel()
        decisions, _notes = h.consider_all(target=funnel_target())
        for d in decisions:
            h.offer(d)
        self.assertEqual(len(h.store.rows), 3)
        self.assertEqual(len(h.ha.sent), 3)

    def test_each_notification_has_its_own_tag(self):
        """iOS treats the tag as an identity. One tag for the league means the
        third swap's notification replaces the first two on the lock screen,
        and the user is asked one question and never sees the others."""
        h = funnel()
        decisions, _notes = h.consider_all(target=funnel_target())
        for d in decisions:
            h.offer(d)
        tags = [s["tag"] for s in h.ha.sent]
        self.assertEqual(len(set(tags)), 3)
        # Still league-scoped in front, so two leagues can never collide
        # whatever their proposal ids turn out to be.
        self.assertTrue(all(t.startswith("autopilot-addrop-111111111-")
                            for t in tags))

    def test_each_card_carries_its_own_pair(self):
        h = funnel()
        decisions, _notes = h.consider_all(target=funnel_target())
        for d in decisions:
            h.offer(d)
        self.assertEqual(
            [(r.payload["add_name"], r.payload["drop_name"])
             for r in h.store.rows],
            [("FA90", "Weak"), ("FA91", "Weaker"), ("FA92", "Weakest")],
        )

    def test_approving_one_does_not_approve_the_others(self):
        """Each row is independently claimable, so a tap on the second card
        leaves the first and third exactly where they were."""
        h = funnel()
        decisions, _notes = h.consider_all(target=funnel_target())
        for d in decisions:
            h.offer(d)
        self.assertTrue(h.app.store.claim_for_execution(h.store.rows[1].id))
        self.assertEqual(
            [r.completed for r in h.store.rows], [None, None, None]
        )


def _starts(work, roster, scoring_period=4):
    return main_mod._starts_today(work, roster, scoring_period)


class TestGamesStartingToday(unittest.TestCase):
    """``_starts_today``: what tonight will cost against the weekly cap.

    The status page adds this to ESPN's games-used to answer "how many will
    have been played after today". It counts under the same two conditions ESPN
    does -- a *starting* slot, and an NBA team playing on this scoring period --
    because a number derived any other way would quietly disagree with the
    scoreboard it sits next to.
    """

    def work(self, plan=None, schedule=None):
        return main_mod.LineupWork(
            plan=plan, projections={}, availability={}, skipped=(),
            overrides={}, injuries=None, cal=None, matchup_period=1,
            schedule={1: {4: 1}} if schedule is None else schedule,
            games_used=None, notes=(), sources_used={}, sources_missing={},
        )

    def roster(self):
        # Players 1 and 2 are on pro team 1, which plays on period 4; player 3
        # is on team 2, which does not.
        return [player(1), player(2), player(3, team=2)]

    def test_only_starters_whose_team_plays_today_are_counted(self):
        plan = _Harness().plan({1: START, 2: BENCH, 3: START})
        self.assertEqual(_starts(self.work(plan), self.roster()), 1)

    def test_it_measures_the_lineup_about_to_be_written(self):
        """Not the one ESPN currently has: by the time the page is read, the
        write has happened."""
        before = _Harness().plan({1: BENCH, 2: BENCH, 3: BENCH})
        after = _Harness().plan({1: START, 2: START, 3: BENCH})
        self.assertEqual(_starts(self.work(before), self.roster()), 0)
        self.assertEqual(_starts(self.work(after), self.roster()), 2)

    def test_an_aborted_run_falls_back_to_what_espn_already_has(self):
        """No plan is not a quiet night -- whatever is set is what will play."""
        roster = [player(1), player(2, slot=BENCH), player(3, team=2)]
        self.assertEqual(_starts(self.work(None), roster), 1)

    def test_no_pro_schedule_means_no_claim_at_all(self):
        """``None``, not 0: the page says nothing rather than implying a quiet
        night it has no evidence for."""
        plan = _Harness().plan({1: START, 2: START, 3: START})
        self.assertIsNone(_starts(self.work(plan, schedule={}), self.roster()))

    def test_a_night_with_no_games_is_zero_rather_than_unknown(self):
        plan = _Harness().plan({1: START, 2: START, 3: START})
        self.assertEqual(
            _starts(self.work(plan, schedule={1: {9: 1}}), self.roster()), 0)


if __name__ == "__main__":
    unittest.main()


class TestTheRunRecordsWhatItBelieved(unittest.TestCase):
    """The forecast write, which nothing downstream reads today.

    It is wired in anyway because the numbers are perishable: ESPN overwrites
    its season projection as the season moves, so the only chance to record
    what a lineup was set on is the morning it is set. That makes this the one
    write whose *absence* is invisible until someone wants to score a decision
    weeks later and finds nothing -- which is exactly why it is tested here, at
    the wiring, and not only at the store.
    """

    def _record(self, **kw):
        h = _Harness(**kw)
        plan = h.plan({1: START, 2: START, 3: BENCH})
        work = lineup_work(plan, projections=h.projections,
                           availability=_notes(1, 2, 3))
        h.app._record_forecasts("RUN1", h.lg, h.settings, work, h.roster)
        return h

    def test_one_row_per_priced_player(self):
        h = self._record()
        self.assertEqual(len(h.store.forecasts), 1)
        wrote = h.store.forecasts[0]
        self.assertEqual(wrote["run_id"], "RUN1")
        self.assertEqual(
            sorted(r["player_id"] for r in wrote["rows"]), [1, 2, 3])

    def test_started_follows_the_slot_and_not_the_value(self):
        """A player can be priced highly and still sit. That gap is the
        decision the regret metric exists to catch, so the row must record
        where he actually went."""
        h = self._record()
        rows = {r["player_id"]: r for r in h.store.forecasts[0]["rows"]}
        self.assertEqual(rows[1]["started"], 1)
        self.assertEqual(rows[3]["started"], 0)
        # ...and the benched man is the second-best player on the roster.
        self.assertGreater(rows[3]["projected"], rows[2]["projected"])
        self.assertEqual(rows[2]["started"], 1)

    def test_the_projection_stored_is_the_one_the_optimizer_used(self):
        h = self._record()
        rows = {r["player_id"]: r for r in h.store.forecasts[0]["rows"]}
        for pid, value in h.values.items():
            self.assertAlmostEqual(rows[pid]["projected"], value)

    def test_names_are_denormalized_off_the_roster(self):
        """So a player since dropped is still readable a month later."""
        h = self._record()
        rows = {r["player_id"]: r for r in h.store.forecasts[0]["rows"]}
        self.assertEqual(rows[2]["name"], "Weak")

    def test_availability_is_stored_beside_the_projection_not_inside_it(self):
        """``projected`` is conditional on playing, the same conditioning the
        actual it will be compared against has. ``p_play`` is a separate
        question with a separate answer."""
        h = self._record()
        rows = {r["player_id"]: r for r in h.store.forecasts[0]["rows"]}
        self.assertAlmostEqual(rows[1]["p_play"], 0.9)
        self.assertEqual(rows[1]["status"], "ACTIVE")
        self.assertEqual(rows[1]["status_source"], "espn")
        # Not pre-multiplied: 60.0 is the conditional value, not 54.0.
        self.assertAlmostEqual(rows[1]["projected"], 60.0)

    def test_a_run_that_planned_nothing_writes_nothing(self):
        """No plan is a refusal, not a lineup of zeros."""
        h = _Harness()
        h.app._record_forecasts("RUN1", h.lg, h.settings,
                                lineup_work(None), h.roster)
        self.assertEqual(h.store.forecasts, [])

    def test_a_failing_write_never_costs_the_run_the_lineup(self):
        """Bookkeeping runs before the ESPN write. It may not raise."""
        h = _Harness(store=_Store(forecasts_raise=RuntimeError("disk full")))
        plan = h.plan({1: START, 2: START, 3: BENCH})
        work = lineup_work(plan, projections=h.projections,
                           availability=_notes(1, 2, 3))
        h.app._record_forecasts("RUN1", h.lg, h.settings, work, h.roster)
        self.assertEqual(h.store.forecasts, [])

    def test_a_player_with_no_availability_note_still_gets_a_row(self):
        h = _Harness()
        plan = h.plan({1: START, 2: START, 3: BENCH})
        work = lineup_work(plan, projections=h.projections, availability={})
        h.app._record_forecasts("RUN1", h.lg, h.settings, work, h.roster)
        rows = h.store.forecasts[0]["rows"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["p_play"] is None for r in rows))
        self.assertTrue(all(r["status"] == "" for r in rows))
