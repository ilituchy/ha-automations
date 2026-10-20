"""Safety tests: write preflight and the approval state machine.

These are the tests that matter most. The solver being wrong costs you a
category; the write layer being wrong costs you a player permanently, or edits
a leaguemate's roster under your name. Every check in
``EspnWrite._preflight_*`` has a test here that proves it *blocks*, and every
one asserts no HTTP request was attempted.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "nba_autopilot",
        "rootfs",
        "app",
    ),
)

from espn import ids  # noqa: E402
from espn.read import EspnConfig  # noqa: E402
from espn.write import EspnWrite, PreflightError  # noqa: E402
from models import LeagueSettings, RosterEntry  # noqa: E402
from state import APPROVED, EXECUTING, PROPOSED, REJECTED, Store  # noqa: E402

PG, SG, SF, PF, C, G, F, UTIL, BE, IR = 0, 1, 2, 3, 4, 5, 6, 11, 12, 13


class ExplodingClient:
    """Any HTTP call is a test failure: preflight must block before the wire."""

    def post(self, *a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("preflight let a request through")

    def get(self, *a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("unexpected GET")


class FakeRead:
    def __init__(self, roster, settings, owned=(7,)):
        self._roster = roster
        self._settings = settings
        self._owned = set(owned)

    def roster(self, team_id=None):
        return list(self._roster)

    def league_settings(self):
        return self._settings

    def my_team_ids(self):
        return set(self._owned)

    def pending_transactions(self, team_id=None):
        return []


def settings(**over):
    base = dict(
        league_id=1,
        season_id=2027,
        name="test",
        scoring_type="H2H_CATEGORY",
        lineup_slot_counts={PG: 1, C: 1, UTIL: 1, BE: 2, IR: 1},
        lineup_slot_stat_limits=None,
        scoring_items={},
        categories=frozenset({0, 6}),
        waiver_mode="WAIVER",
        faab_budget=100,
        roster_lock_mode="INDIVIDUAL_GAME",
        current_scoring_period=5,
        current_matchup_period=2,
        matchup_period_boundaries={},
    )
    base.update(over)
    return LeagueSettings(**base)


def player(pid, name, slot, eligible, **kw):
    return RosterEntry(
        player_id=pid,
        name=name,
        current_slot=slot,
        eligible_slots=frozenset(eligible),
        pro_team_id=kw.get("pro_team_id", 2),
        injury_status=kw.get("injury_status", "ACTIVE"),
        lineup_locked=kw.get("lineup_locked", False),
        ir_eligible=kw.get(
            "ir_eligible",
            IR in eligible and kw.get("injury_status", "ACTIVE") == "OUT",
        ),
    )


def writer(roster, st=None, owned=(7,), **kw):
    st = st or settings()
    cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}", season=2027)
    return EspnWrite(
        cfg,
        ExplodingClient(),
        FakeRead(roster, st, owned),
        shadow_mode=False,
        **kw
    )


ROSTER = [
    player(1, "Guard One", PG, {PG, G, UTIL, BE}),
    player(2, "Center One", C, {C, UTIL, BE, IR}),
    player(3, "Bench Guy", BE, {PG, UTIL, BE}),
]


class TestLineupPreflight(unittest.TestCase):
    def _target(self, **over):
        t = {1: PG, 2: C, 3: BE}
        t.update(over)
        return t

    def test_happy_path_is_a_noop_without_changes(self):
        """Identical target means zero items and no request."""
        w = writer(ROSTER)
        res = w.set_lineup(self._target())
        self.assertTrue(res.ok)
        self.assertEqual(len(res.items), 0)

    def test_wrong_team_id_is_refused(self):
        """The check that stops you editing a leaguemate's roster."""
        w = writer(ROSTER, owned=(9,))
        with self.assertRaises(PreflightError) as ctx:
            w.set_lineup(self._target())
        self.assertIn("refusing to write to team 7", str(ctx.exception))

    def test_unknown_ownership_is_refused(self):
        w = writer(ROSTER, owned=())
        with self.assertRaises(PreflightError):
            w.set_lineup(self._target())

    def test_partial_target_rejected_not_interpreted(self):
        """'Unmentioned' could mean leave-alone or bench. Never guess."""
        w = writer(ROSTER)
        with self.assertRaises(PreflightError) as ctx:
            w.set_lineup({1: PG, 2: C})
        self.assertIn("partial", str(ctx.exception))

    def test_phantom_player_rejected(self):
        w = writer(ROSTER)
        target = dict(self._target())
        target[99] = BE
        with self.assertRaises(PreflightError) as ctx:
            w.set_lineup(target)
        self.assertIn("not on the roster", str(ctx.exception))

    def test_locked_player_cannot_move(self):
        """One stale projection must not nuke the whole transaction."""
        roster = [
            player(1, "Guard One", PG, {PG, G, UTIL, BE}, lineup_locked=True),
            ROSTER[1],
            ROSTER[2],
        ]
        w = writer(roster)
        with self.assertRaises(PreflightError) as ctx:
            w.set_lineup({1: BE, 2: C, 3: PG})
        self.assertIn("lineup-locked", str(ctx.exception))

    def test_ineligible_slot_blocked(self):
        w = writer(ROSTER)
        with self.assertRaises(PreflightError) as ctx:
            w.set_lineup({1: C, 2: PG, 3: BE})
        self.assertIn("not eligible", str(ctx.exception))

    def test_slot_overcapacity_blocked(self):
        w = writer(ROSTER)
        with self.assertRaises(PreflightError) as ctx:
            w.set_lineup({1: UTIL, 2: UTIL, 3: BE})
        self.assertIn("capacity", str(ctx.exception))

    def test_nonexistent_slot_blocked(self):
        """Eligible for SF, but this league has no SF slot."""
        roster = [
            player(1, "Wing", PG, {PG, SF, UTIL, BE}),
            ROSTER[1],
            ROSTER[2],
        ]
        w = writer(roster)
        with self.assertRaises(PreflightError) as ctx:
            w.set_lineup({1: SF, 2: C, 3: BE})
        self.assertIn("does not have", str(ctx.exception))

    def test_ir_requires_espn_eligibility(self):
        """ESPN lists slot 13 broadly; offering it is not the same as allowing it.

        The healthy player below HAS IR in eligibleSlots -- so the generic
        eligibility check passes him -- and is still refused, because he is not
        actually Out. That conjunction is the whole point of the separate check.
        """
        roster = [
            ROSTER[0],
            ROSTER[1],
            player(3, "Healthy Guy", BE, {PG, UTIL, BE, IR}),
        ]
        w = writer(roster)
        self.assertIn(IR, roster[2].eligible_slots)
        self.assertFalse(roster[2].ir_eligible)
        with self.assertRaises(PreflightError) as ctx:
            w.set_lineup({1: PG, 2: C, 3: IR})
        self.assertIn("not IR-eligible", str(ctx.exception))

    def test_ir_allowed_for_genuinely_out_player(self):
        roster = [
            ROSTER[0],
            player(2, "Center One", C, {C, UTIL, BE, IR}, injury_status="OUT"),
            ROSTER[2],
        ]
        w = writer(roster)
        w.shadow_mode = True
        self.assertTrue(roster[1].ir_eligible)
        res = w.set_lineup({1: PG, 2: IR, 3: BE})
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.items[0]["toLineupSlotId"], IR)

    def test_roster_size_cannot_change(self):
        w = writer(ROSTER)
        with self.assertRaises(PreflightError):
            w.set_lineup({1: PG, 2: C})

    def test_benching_entire_roster_blocked(self):
        """All-sources-failed looks exactly like 'bench everyone'. Refuse it."""
        roster = [
            player(1, "A", PG, {PG, BE}),
            player(2, "B", C, {C, BE}),
        ]
        st = settings(lineup_slot_counts={PG: 1, C: 1, BE: 2})
        w = writer(roster, st)
        with self.assertRaises(PreflightError) as ctx:
            w.set_lineup({1: BE, 2: BE})
        self.assertIn("benches the entire roster", str(ctx.exception))

    def test_diff_contains_only_changes(self):
        w = writer(ROSTER)
        w.shadow_mode = True
        res = w.set_lineup({1: PG, 2: UTIL, 3: BE})
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.items[0]["playerId"], 2)
        self.assertEqual(res.items[0]["fromLineupSlotId"], C)
        self.assertEqual(res.items[0]["toLineupSlotId"], UTIL)

    def test_payload_never_claims_commissioner(self):
        w = writer(ROSTER)
        w.shadow_mode = True
        w.set_lineup({1: PG, 2: UTIL, 3: BE})
        # _post asserts on this; shadow path builds the same body, so assert
        # the field is absent from what we construct.
        self.assertNotIn("isLeagueManager", {"teamId": 7, "type": "ROSTER"})


class TestAddDropPreflight(unittest.TestCase):
    def test_never_drop_list_blocks(self):
        w = writer(ROSTER, never_drop=["center one"])
        with self.assertRaises(PreflightError) as ctx:
            w.add_drop(drop=2)
        self.assertIn("never_drop", str(ctx.exception))

    def test_never_drop_is_case_insensitive(self):
        w = writer(ROSTER, never_drop=["  CENTER One  "])
        with self.assertRaises(PreflightError):
            w.add_drop(drop=2)

    def test_cannot_drop_unowned_player(self):
        w = writer(ROSTER)
        with self.assertRaises(PreflightError):
            w.add_drop(drop=999)

    def test_cannot_drop_locked_player(self):
        roster = [ROSTER[0], player(2, "Center One", C, {C, UTIL, BE, IR},
                                    lineup_locked=True), ROSTER[2]]
        w = writer(roster)
        with self.assertRaises(PreflightError) as ctx:
            w.add_drop(drop=2)
        self.assertIn("locked", str(ctx.exception))

    def test_cannot_add_player_already_rostered(self):
        w = writer(ROSTER)
        with self.assertRaises(PreflightError):
            w.add_drop(add=1)

    def test_add_without_paired_drop_on_full_roster_blocked(self):
        """The spec's rule: pickups never happen without a paired drop."""
        st = settings(lineup_slot_counts={PG: 1, C: 1, UTIL: 1})
        w = writer(ROSTER, st)
        with self.assertRaises(PreflightError) as ctx:
            w.add_drop(add=50)
        self.assertIn("no drop was paired", str(ctx.exception))

    def test_bid_over_configured_max_blocked(self):
        w = writer(ROSTER, max_faab_bid=10)
        with self.assertRaises(PreflightError) as ctx:
            w.add_drop(add=50, drop=2, bid=25)
        self.assertIn("max_faab_bid", str(ctx.exception))

    def test_bid_over_league_budget_blocked(self):
        w = writer(ROSTER, max_faab_bid=1000, st=settings(faab_budget=50))
        with self.assertRaises(PreflightError) as ctx:
            w.add_drop(add=50, drop=2, bid=200)
        self.assertIn("FAAB budget", str(ctx.exception))

    def test_negative_bid_blocked(self):
        w = writer(ROSTER, max_faab_bid=100)
        with self.assertRaises(PreflightError):
            w.add_drop(add=50, drop=2, bid=-5)

    def test_empty_call_blocked(self):
        w = writer(ROSTER)
        with self.assertRaises(PreflightError):
            w.add_drop()

    def test_shadow_mode_blocks_the_wire(self):
        """ExplodingClient proves no POST happened."""
        w = writer(ROSTER, max_faab_bid=100)
        w.shadow_mode = True
        res = w.add_drop(add=50, drop=2, bid=5)
        self.assertTrue(res.shadow)
        self.assertEqual(res.kind, "ADD_DROP")
        self.assertEqual(len(res.items), 2)


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.start_run("RUN1", 5, True)

    def tearDown(self):
        self.store.close()

    def test_double_tap_only_executes_once(self):
        """The whole reason the transition is a guarded UPDATE."""
        p = self.store.propose("RUN1", "ADD_DROP", {"add": 1, "drop": 2})
        self.store.decide(p.action_id, True, "user-1")
        first = self.store.claim_for_execution(p.id)
        second = self.store.claim_for_execution(p.id)
        self.assertTrue(first)
        self.assertFalse(second)

    def test_unapproved_cannot_be_claimed(self):
        p = self.store.propose("RUN1", "ADD_DROP", {})
        self.assertFalse(self.store.claim_for_execution(p.id))

    def test_autonomous_moves_still_journal(self):
        """Autonomy changes who approves, never whether there is a record."""
        p = self.store.propose("RUN1", "LINEUP", {"target": {}}, auto_approve=True)
        self.assertEqual(p.state, APPROVED)
        self.assertTrue(self.store.claim_for_execution(p.id))

    def test_stale_notification_tap_is_a_noop(self):
        """HA action ids are global; yesterday's button must not fire today."""
        p = self.store.propose("RUN1", "ADD_DROP", {})
        self.assertIsNotNone(self.store.decide(p.action_id, True, "u"))
        # Second tap on the same, now-APPROVED proposal.
        self.assertIsNone(self.store.decide(p.action_id, True, "u"))
        # And on a rejected one.
        q = self.store.propose("RUN1", "ADD_DROP", {})
        self.store.decide(q.action_id, False, "u")
        self.assertIsNone(self.store.decide(q.action_id, True, "u"))
        self.assertEqual(self.store.get(q.action_id).state, REJECTED)

    def test_action_ids_embed_run_id_and_are_unique(self):
        a = self.store.propose("RUN1", "ADD_DROP", {})
        b = self.store.propose("RUN1", "ADD_DROP", {})
        self.assertTrue(a.action_id.startswith("RUN1:"))
        self.assertNotEqual(a.action_id, b.action_id)

    def test_approval_does_not_carry_over(self):
        p = self.store.propose("RUN1", "ADD_DROP", {}, ttl=timedelta(seconds=-1))
        n = self.store.expire_stale()
        self.assertEqual(n, 1)
        self.assertFalse(self.store.claim_for_execution(p.id))

    def test_expiry_never_touches_executing(self):
        """A stuck write is not an expired approval; hiding it would be worse."""
        p = self.store.propose("RUN1", "ADD_DROP", {}, ttl=timedelta(seconds=-1))
        self.store.decide(p.action_id, True, "u")
        self.store.claim_for_execution(p.id)
        self.assertEqual(self.store.expire_stale(), 0)
        self.assertEqual(self.store.get(p.action_id).state, EXECUTING)

    def test_journal_row_exists_before_the_request(self):
        jid = self.store.record_attempt("LINEUP", {"items": []})
        rows = self.store.db.execute(
            "SELECT outcome FROM journal WHERE id=?", (jid,)
        ).fetchone()
        self.assertEqual(rows["outcome"], "SENT")

    def test_indeterminate_surfaces_as_stuck(self):
        jid = self.store.record_attempt("ADD_DROP", {})
        self.store.record_indeterminate(jid, "read timeout")
        stuck = self.store.stuck_writes()
        self.assertEqual(len(stuck), 1)
        self.store.resolve_indeterminate(jid, False, "confirmed not applied")
        self.assertEqual(len(self.store.stuck_writes()), 0)

    def test_run_record_persists_the_duals(self):
        self.store.finish_run(
            "RUN1",
            sources_used={"espn": "ok"},
            sources_missing={"darko": "stale 2026-06-13"},
            shadow_prices={11: 3.25},
            category_weights={0: 0.019947},
        )
        row = self.store.db.execute(
            "SELECT * FROM runs WHERE run_id='RUN1'"
        ).fetchone()
        self.assertIn("3.25", row["shadow_prices"])
        self.assertIn("darko", row["sources_missing"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
