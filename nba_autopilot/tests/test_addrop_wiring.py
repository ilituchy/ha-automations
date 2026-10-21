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
from espn import ids  # noqa: E402
from models import (  # noqa: E402
    P_PLAY_PRIOR,
    LeagueSettings,
    MatchupScore,
    Projection,
    RosterEntry,
)
from solve.calendar import SeasonCalendar  # noqa: E402

PTS = ids.PTS
START, BENCH = 11, 12


class _Store:
    """Just enough of ``state.Store`` to see what was journalled."""

    class _Row:
        def __init__(self, pid, action_id, kind, payload, rationale, delta, auto):
            self.id = pid
            self.action_id = action_id
            self.kind = kind
            self.payload = payload
            self.rationale = rationale
            self.value_delta = delta
            self.auto_approve = auto
            self.completed = None
            self.error = None

    def __init__(self, *, claimable: bool = True):
        self.rows = []
        self.claimable = claimable
        self.claims = []

    def propose(self, run_id, kind, payload, *, rationale="", value_delta=0.0,
                auto_approve=False):
        row = self._Row(len(self.rows) + 1,
                        "%s:%s:%d" % (run_id, kind, len(self.rows) + 1),
                        kind, dict(payload), rationale, value_delta,
                        auto_approve)
        self.rows.append(row)
        return row

    def claim_for_execution(self, pid):
        self.claims.append(pid)
        return self.claimable

    def complete(self, pid, ok, error=None):
        self.rows[pid - 1].completed = ok
        self.rows[pid - 1].error = error


class _Ha:
    def __init__(self, *, explode: bool = False):
        self.sent = []
        self.problems = []
        self.explode = explode

    def notify(self, *, title, message, tag, actions=(), time_sensitive=True):
        if self.explode:
            raise RuntimeError("push gateway down")
        self.sent.append({"title": title, "message": message, "tag": tag,
                          "actions": [a.action_id for a in actions]})

    def persistent_notification(self, title, message):
        self.problems.append((title, message))


class _Write:
    def __init__(self, *, explode: bool = False):
        self.calls = []
        self.explode = explode

    def add_drop(self, *, add, drop, bid, waiver):
        if self.explode:
            raise RuntimeError("ESPN said no")
        self.calls.append({"add": add, "drop": drop, "bid": bid,
                           "waiver": waiver})
        return type("R", (), {"ok": True})()


class _Read:
    """The four reads add/drop makes, each independently breakable."""

    def __init__(self, *, pool=(), pool_mean=45.0, score=None, used=None,
                 fail=()):
        self.pool = list(pool)
        self.pool_mean = pool_mean
        self.score = score
        self.used = used if used is not None else {}
        self.fail = set(fail)
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
            int(p): {ids.SPLIT_SEASON_PROJECTED: {PTS: self.pool_mean}}
            for p in ids_
        }

    def matchup_score(self, team_id=None, matchup_period=None):
        self._maybe_fail("matchup_score")
        return self.score

    def acquisitions_used(self, team_id=None):
        self._maybe_fail("acquisitions_used")
        return dict(self.used)


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
                 never_drop=(), judge=None):
        self.store = store or _Store()
        self.ha = ha or _Ha()
        self.cfg = type("Cfg", (), {
            "never_drop": tuple(never_drop),
            "addrop_margin_pct": margin,
            "addrop_churn_floor": floor,
            "autonomy_add_drop": autonomy,
            "shadow_mode": shadow,
        })()
        self.app = object.__new__(main_mod.Autopilot)
        self.app.cfg = self.cfg
        self.app.store = self.store
        self.app.ha = self.ha
        self.app.judge = judge

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
        self.schedule = {1: {1: 1, 2: 1, 3: 1}}

    def plan(self, target=None):
        target = target or {1: START, 2: START, 3: BENCH}
        return pipeline.LineupPlan(
            target=target, moves=(), lam=0.0, utilization=float("inf"),
            values=dict(self.values), notes=(),
        )

    def consider(self, target=None, injuries=None):
        return self.app._consider_addrop(
            self.lg, self.settings, self.roster, self.projections,
            self.plan(target),
            cal=self.cal, matchup_period=1, schedule=self.schedule,
            injuries=injuries,
        )

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
            decision.drop.name if decision.drop else "", "Weak"
        )

    def test_the_configured_thresholds_reach_the_decision(self):
        """A floor nobody can clear proves the number is actually plumbed
        rather than defaulted inside ``plan_addrop``."""
        h = _Harness(read=_Read(pool=[player(90, name="Better")]),
                     floor=10_000.0)
        decision, notes = h.consider()
        self.assertFalse(decision.proposed)
        self.assertIn("10000.0-point churn floor", " ".join(notes))


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


if __name__ == "__main__":
    unittest.main()
