"""Tip-off lineup sweeps: when to look again, and what a sweep is allowed to be.

The 09:00 run cannot see news that breaks after it. Measured across every
completed scoring period of 2026-27: three days in each league had a player
recording zero in a starting slot while his NBA team played, and in Dynassty
two of those were recoverable from the bench for 45.0 points. On 2026-11-04
Trey Murphy III held Dynassty's ``F`` slot at zero while Bobby Portis (24.5)
and Ziaire Williams (23.5), both F-eligible with games, sat behind him.

These tests pin the scheduling arithmetic, which is the part with edges.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

import main as main_mod  # noqa: E402
import sweep as sweep_mod  # noqa: E402
from models import Move  # noqa: E402
from state import Store  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")


class _Game:
    """A GameRef as `sweep_times` reads it: a tip instant and two teams."""

    def __init__(self, tip_utc, home, away):
        self.tipoff_utc = tip_utc
        self.home_team_id = home
        self.away_team_id = away

    @property
    def team_ids(self):
        return (self.home_team_id, self.away_team_id)


def _tip(hh, mm=0, day=4):
    """A tip-off at this Pacific wall time on 2026-11-%(day)d, held as UTC."""
    return datetime(2026, 11, day, hh, mm, tzinfo=PT).astimezone(timezone.utc)


def _now(hh, mm=0, day=4):
    return datetime(2026, 11, day, hh, mm, tzinfo=PT)


class TestSweepTimes(unittest.TestCase):
    def test_one_game_gives_one_sweep_at_lead(self):
        out = sweep_mod.sweep_times([_Game(_tip(16), 3, 9)], {3}, _now(9))
        self.assertEqual(out, [_now(15, 15)])

    def test_a_game_we_have_nobody_in_is_ignored(self):
        """Two teams we do not roster cannot change our lineup."""
        out = sweep_mod.sweep_times([_Game(_tip(16), 3, 9)], {21}, _now(9))
        self.assertEqual(out, [])

    def test_either_side_of_the_matchup_counts(self):
        """Our player may be on the home or the away team."""
        for teams in ({3}, {9}):
            out = sweep_mod.sweep_times([_Game(_tip(16), 3, 9)], teams, _now(9))
            self.assertEqual(out, [_now(15, 15)], teams)

    def test_a_tbd_tipoff_is_skipped(self):
        """`startTimeTBD` has no instant; guessing one schedules a fiction."""
        out = sweep_mod.sweep_times([_Game(None, 3, 9)], {3}, _now(9))
        self.assertEqual(out, [])

    def test_a_game_already_inside_the_lead_window_is_dropped(self):
        """The slot is about to lock; a sweep that cannot beat it is waste."""
        out = sweep_mod.sweep_times([_Game(_tip(16), 3, 9)], {3}, _now(15, 30))
        self.assertEqual(out, [])

    def test_a_game_already_tipped_is_dropped(self):
        out = sweep_mod.sweep_times([_Game(_tip(16), 3, 9)], {3}, _now(18))
        self.assertEqual(out, [])

    def test_times_come_back_in_order(self):
        games = [_Game(_tip(19, 30), 3, 9), _Game(_tip(16), 1, 2),
                 _Game(_tip(17, 30), 5, 6)]
        out = sweep_mod.sweep_times(games, {3, 1, 5}, _now(9))
        self.assertEqual(out, [_now(15, 15), _now(16, 45), _now(18, 45)])

    # -- coalescing --------------------------------------------------------

    def test_a_full_slate_at_one_tip_time_is_one_sweep(self):
        """Six games at 19:00 ET is one question, not six."""
        games = [_Game(_tip(16), t, t + 1) for t in range(1, 12, 2)]
        out = sweep_mod.sweep_times(games, set(range(1, 13)), _now(9))
        self.assertEqual(out, [_now(15, 15)])

    def test_tips_within_the_bucket_share_a_sweep(self):
        games = [_Game(_tip(16), 1, 2), _Game(_tip(16, 10), 3, 4)]
        out = sweep_mod.sweep_times(games, {1, 3}, _now(9))
        self.assertEqual(out, [_now(15, 15)])

    def test_tips_beyond_the_bucket_get_their_own(self):
        games = [_Game(_tip(16), 1, 2), _Game(_tip(16, 30), 3, 4)]
        out = sweep_mod.sweep_times(games, {1, 3}, _now(9))
        self.assertEqual(out, [_now(15, 15), _now(15, 45)])

    def test_coalescing_never_lands_after_a_tip(self):
        """The property that makes dropping the later time safe.

        A coalesced sweep keeps the *earlier* of the two, so it is before both
        tips. Were BUCKET ever raised past LEAD, the kept time could fall
        after the earlier game had already started -- the sweep would run
        against a locked slot and the coalescing would have cost a game.
        """
        self.assertLess(sweep_mod.BUCKET, sweep_mod.LEAD)
        games = [_Game(_tip(16), 1, 2), _Game(_tip(16, 19), 3, 4)]
        out = sweep_mod.sweep_times(games, {1, 3}, _now(9))
        self.assertEqual(len(out), 1)
        self.assertLess(out[0], _tip(16).astimezone(PT))

    def test_the_ceiling_keeps_the_earliest(self):
        """An early game locks first, so it is the one that must be covered."""
        games = [_Game(_tip(12 + i, 0), i + 1, 50 + i) for i in range(10)]
        out = sweep_mod.sweep_times(games, set(range(1, 11)), _now(8),
                                    max_sweeps=3)
        self.assertEqual(out, [_now(11, 15), _now(12, 15), _now(13, 15)])

    # -- the day this was built for ---------------------------------------

    def test_the_murphy_slate(self):
        """2026-11-04: the sweep that would have caught it.

        The Pelicans tipped at 17:00 Pacific. A 09:00 run that saw PROBABLE
        gets a second look at 16:15, by which point the beat had him out.
        """
        games = [_Game(_tip(17), 3, 19)]
        out = sweep_mod.sweep_times(games, {3}, _now(9, 1))
        self.assertEqual(out, [_now(16, 15)])
        self.assertLess(out[0], _tip(17).astimezone(PT))

    def test_no_games_means_no_sweeps(self):
        """An off night costs nothing: no jobs, no requests, no notification."""
        self.assertEqual(sweep_mod.sweep_times([], {3}, _now(9)), [])
        self.assertIn("no lineup sweeps", sweep_mod.describe([]))

    def test_describe_names_the_times(self):
        out = sweep_mod.describe([_now(15, 15), _now(18, 45)])
        self.assertIn("15:15", out)
        self.assertIn("18:45", out)
        self.assertIn("2", out)


class TestSweepRunsAreNotDailyRuns(unittest.TestCase):
    """A sweep must not convince the startup catch-up that 09:00 happened.

    Both write a `runs` row, because both reach ESPN and both must be
    auditable. Only one of them is the day's run.
    """

    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_a_sweep_is_invisible_to_the_catch_up(self):
        self.store.start_run("SWEEP1", 17, True, kind="sweep")
        self.assertIsNone(self.store.last_run_started())

    def test_a_daily_run_is_visible(self):
        self.store.start_run("DAILY1", 17, True)
        self.assertIsNotNone(self.store.last_run_started())

    def test_a_sweep_does_not_mask_an_earlier_daily_run(self):
        """The ordinary afternoon: 09:00 ran, a sweep followed."""
        self.store.start_run("DAILY1", 17, True)
        daily = self.store.last_run_started()
        self.store.start_run("SWEEP1", 17, True, kind="sweep")
        self.assertEqual(self.store.last_run_started(), daily)

    def test_a_null_kind_counts_as_daily(self):
        """Rows written before the column existed were all daily runs."""
        self.store.start_run("OLD1", 17, True)
        self.store.db.execute("UPDATE runs SET kind=NULL")
        self.store.db.commit()
        self.assertIsNotNone(self.store.last_run_started())

    def test_the_row_records_which_it_was(self):
        self.store.start_run("DAILY1", 17, True)
        self.store.start_run("SWEEP1", 17, True, kind="sweep")
        got = {r["run_id"]: r["kind"]
               for r in self.store.query("SELECT run_id, kind FROM runs")}
        self.assertEqual(got, {"DAILY1": "daily", "SWEEP1": "sweep"})


# ---------------------------------------------------------------------------
# What a sweep is allowed to be
# ---------------------------------------------------------------------------


class _Plan:
    def __init__(self, moves=(), feasible=True, lam=0.0, cap=None, target=None):
        self.moves = tuple(moves)
        self.feasible = feasible
        self.lam = lam
        self.cap = cap
        self.notes = ()
        # `pipeline.summarize` logs the resulting lineup, so the plan has to
        # carry one even when the test is about something else.
        self.target = dict(target or {})

    @property
    def changed(self):
        return bool(self.moves)


class _Work:
    def __init__(self, plan):
        self.plan = plan


class _Read:
    def __init__(self):
        self.calls = 0

    def roster(self, *a, **k):
        self.calls += 1
        return []


class _League:
    name = "Test League"

    def __init__(self):
        self.read = _Read()
        self.ref = type("Ref", (), {"league_id": 7, "team_id": 1})()


class _Ha:
    def __init__(self):
        self.sent = []

    def notify(self, **kw):
        self.sent.append(kw)

    def persistent_notification(self, *a, **k):
        pass


class _Settings:
    name = "Test League"
    current_scoring_period = 17


def _app(plan, *, applied=True):
    """An Autopilot with only what `sweep_league` touches."""
    app = object.__new__(main_mod.Autopilot)
    app.cfg = type("Cfg", (), {"shadow_mode": False})()
    app.store = Store(":memory:")
    app.ha = _Ha()
    app.leagues = [_League()]
    app.settings_now = lambda lg: (_Settings(), None)
    app.plan_from_roster = lambda lg, st, roster: _Work(plan)
    app._applied = []
    def _apply(run_id, lg, pl, roster, st):
        app._applied.append(run_id)
        return applied
    app._apply_lineup = _apply
    app._finish = lambda *a, **k: None
    # A tripwire: a sweep that reaches for the pool has stopped being
    # lineup-only, which is the property that makes it cheap and repeatable.
    def _boom(*a, **k):
        raise AssertionError("a sweep must not consider add/drops")
    app._consider_addrops = _boom
    app._offer_addrop = _boom
    return app


class TestSweepLeague(unittest.TestCase):
    def tearDown(self):
        pass

    def test_no_change_writes_nothing_and_says_nothing(self):
        """The common case. Most sweeps find the morning was right."""
        app = _app(_Plan(moves=()))
        self.assertFalse(app.sweep_league(app.leagues[0]))
        self.assertEqual(app._applied, [])
        self.assertEqual(app.ha.sent, [])
        self.assertEqual(app.store.query("SELECT run_id FROM runs"), [])
        app.store.close()

    def test_a_change_is_applied_journalled_and_notified(self):
        mv = Move(kind="LINEUP", player_id=1, player_name="Trey Murphy III",
                  from_slot=6, to_slot=12)
        app = _app(_Plan(moves=[mv]))
        self.assertTrue(app.sweep_league(app.leagues[0]))
        self.assertEqual(len(app._applied), 1)
        rows = app.store.query("SELECT run_id, kind FROM runs")
        self.assertEqual([r["kind"] for r in rows], ["sweep"])
        self.assertEqual(rows[0]["run_id"], app._applied[0])
        self.assertEqual(len(app.ha.sent), 1)
        app.store.close()

    def test_the_notification_names_who_moved_and_where(self):
        mv = Move(kind="LINEUP", player_id=1, player_name="Trey Murphy III",
                  from_slot=6, to_slot=12)
        app = _app(_Plan(moves=[mv]))
        app.sweep_league(app.leagues[0])
        body = app.ha.sent[0]["message"]
        self.assertIn("Trey Murphy III", body)
        self.assertIn("F", body)
        self.assertIn("BE", body)
        self.assertIn("Test League", body)
        app.store.close()

    def test_a_refused_write_is_not_announced(self):
        """ESPN rejecting the packet is not a lineup change."""
        mv = Move(kind="LINEUP", player_id=1, player_name="X",
                  from_slot=6, to_slot=12)
        app = _app(_Plan(moves=[mv]), applied=False)
        self.assertFalse(app.sweep_league(app.leagues[0]))
        self.assertEqual(app.ha.sent, [])
        app.store.close()

    def test_an_infeasible_plan_touches_nothing(self):
        """Silent by design: the morning run already notifies on this."""
        mv = Move(kind="LINEUP", player_id=1, player_name="X",
                  from_slot=6, to_slot=12)
        app = _app(_Plan(moves=[mv], feasible=False))
        self.assertFalse(app.sweep_league(app.leagues[0]))
        self.assertEqual(app._applied, [])
        self.assertEqual(app.ha.sent, [])
        self.assertEqual(app.store.query("SELECT run_id FROM runs"), [])
        app.store.close()

    def test_no_projections_touches_nothing(self):
        app = _app(None)
        self.assertFalse(app.sweep_league(app.leagues[0]))
        self.assertEqual(app.ha.sent, [])
        app.store.close()

    def test_a_move_with_no_from_slot_does_not_crash_the_message(self):
        """`Move.from_slot` is Optional and the notification is not worth a
        traceback that would also lose the write's announcement."""
        mv = Move(kind="LINEUP", player_id=1, player_name="X", to_slot=12)
        app = _app(_Plan(moves=[mv]))
        self.assertTrue(app.sweep_league(app.leagues[0]))
        self.assertIn("?", app.ha.sent[0]["message"])
        app.store.close()

    def test_one_league_failing_does_not_stop_the_others(self):
        """Same contract as `run_once`; they are independent problems."""
        app = _app(_Plan(moves=()))
        bad, good = _League(), _League()
        app.leagues = [bad, good]
        seen = []
        def _one(lg):
            seen.append(lg)
            if lg is bad:
                raise RuntimeError("roster read exploded")
            return False
        app.sweep_league = _one
        app.sweep_once()
        self.assertEqual(seen, [bad, good])
        app.store.close()

    def test_a_notification_failure_does_not_lose_the_write(self):
        """The swap already reached ESPN; a dead push must not undo that."""
        mv = Move(kind="LINEUP", player_id=1, player_name="X",
                  from_slot=6, to_slot=12)
        app = _app(_Plan(moves=[mv]))
        def _raise(**kw):
            raise RuntimeError("notify is down")
        app.ha.notify = _raise
        self.assertTrue(app.sweep_league(app.leagues[0]))
        self.assertEqual(len(app._applied), 1)
        app.store.close()


if __name__ == "__main__":
    unittest.main()
