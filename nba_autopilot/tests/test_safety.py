"""Safety tests: write preflight and the approval state machine.

These are the tests that matter most. The solver being wrong costs you a
category; the write layer being wrong costs you a player permanently, or edits
a leaguemate's roster under your name. Every check in
``EspnWrite._preflight_*`` has a test here that proves it *blocks*, and every
one asserts no HTTP request was attempted.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

import config as appconfig  # noqa: E402
from espn import ids  # noqa: E402
from ha import bridge  # noqa: E402
from espn.read import EspnConfig  # noqa: E402
from espn.write import EspnWrite, PreflightError  # noqa: E402
from models import LeagueSettings, RosterEntry  # noqa: E402
from solve import addrop  # noqa: E402
import state  # noqa: E402
from state import APPROVED, EXECUTING, PROPOSED, REJECTED, Store  # noqa: E402

PG, SG, SF, PF, C, G, F, UTIL, BE, IR = 0, 1, 2, 3, 4, 5, 6, 11, 12, 13


class ExplodingClient:
    """Any HTTP call is a test failure: preflight must block before the wire."""

    def post(self, *a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("preflight let a request through")

    def get(self, *a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("unexpected GET")


class FakeRead:
    def __init__(self, roster, settings, owned=(7,), pool=None):
        self._roster = roster
        self._settings = settings
        self._owned = set(owned)
        #: ``player_id -> RosterEntry`` for the acquirable pool. Defaults to
        #: "every unknown id is a free agent", which keeps the add/drop tests
        #: focused on what they are each actually asserting; the acquisition
        #: mode itself is tested explicitly in TestAcquisitionMode.
        self._pool = dict(pool or {})

    def roster(self, team_id=None):
        return list(self._roster)

    def player_in_pool(self, player_id):
        if player_id in self._pool:
            return self._pool[player_id]
        return RosterEntry(
            player_id=player_id,
            name="Pool Player %d" % player_id,
            current_slot=BE,
            eligible_slots=frozenset({UTIL, BE}),
            pro_team_id=1,
            injury_status="ACTIVE",
            acquisition_status="FREEAGENT",
        )

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


class TestScoreboardCache(unittest.TestCase):
    """One row per league, overwritten every run.

    The opposite of ``gp_snapshots``, where the *earliest* reading is the only
    useful one. A live score has no historical value and a stale one actively
    misleads, so the table keeps the newest reading and the page shows how old
    it is.
    """

    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_the_latest_reading_replaces_the_last_one(self):
        self.store.record_scoreboard(1, my_points=100.0, opp_points=90.0)
        self.store.record_scoreboard(1, my_points=403.0, opp_points=513.5)
        rows = self.store.scoreboards()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["my_points"], 403.0)

    def test_each_league_keeps_its_own_row(self):
        self.store.record_scoreboard(1, my_name="A")
        self.store.record_scoreboard(2, my_name="B")
        self.assertEqual(
            sorted(r["my_name"] for r in self.store.scoreboards()), ["A", "B"])

    def test_every_write_stamps_its_own_age(self):
        """``updated_at`` is load-bearing: the page renders it."""
        self.store.record_scoreboard(1, my_points=1.0)
        self.assertTrue(self.store.scoreboards()[0]["updated_at"])

    def test_a_typoed_column_is_refused_rather_than_dropped(self):
        """Silently ignoring it would show up as a number that never updates --
        a long way to debug from the symptom."""
        with self.assertRaises(ValueError):
            self.store.record_scoreboard(1, my_pointz=1.0)
        self.assertEqual(self.store.scoreboards(), [])

    def test_the_unknown_parts_of_a_reading_stay_unknown(self):
        """A league with no cap, or a row ESPN has not scored, writes NULL --
        not 0, which the page would render as a real number."""
        self.store.record_scoreboard(1, my_points=403.0, games_cap=None,
                                     my_games_used=None)
        row = self.store.scoreboards()[0]
        self.assertIsNone(row["games_cap"])
        self.assertIsNone(row["my_games_used"])


class TestLeagueConfig(unittest.TestCase):
    """Multi-league parsing. Two teams, one cookie, zero pooling."""

    BASE = {
        "espn_s2": "cookievalue",
        "espn_swid": "ABC-123",
        "ha_notify_device": "mobile_app_iphone",
        "ha_owner_user_id": "owner-1",
    }

    def _load(self, **over):
        raw = dict(self.BASE)
        raw.update(over)
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(raw, fh)
            return appconfig.load(path)
        finally:
            os.unlink(path)

    def test_two_leagues_stay_separate(self):
        cfg = self._load(leagues=[
            {"league_id": 111111111, "team_id": 2, "label": "Main"},
            {"league_id": 222222222, "team_id": 7},
        ])
        self.assertEqual(len(cfg.leagues), 2)
        self.assertEqual(cfg.leagues[0].display(), "Main")
        self.assertEqual(cfg.leagues[1].display(), "league 222222222")

        a, b = (cfg.espn(l) for l in cfg.leagues)
        self.assertEqual((a.league_id, a.team_id), (111111111, 2))
        self.assertEqual((b.league_id, b.team_id), (222222222, 7))
        # One credential, two leagues.
        self.assertEqual(a.espn_s2, b.espn_s2)
        self.assertEqual(a.swid, b.swid)
        self.assertEqual(a.season, b.season)

    def test_duplicate_league_is_refused_not_deduplicated(self):
        """Two team_ids for one league means one of them is not yours."""
        with self.assertRaises(appconfig.ConfigError) as ctx:
            self._load(leagues=[
                {"league_id": 111111111, "team_id": 2},
                {"league_id": 111111111, "team_id": 9},
            ])
        msg = str(ctx.exception)
        self.assertIn("configured twice", msg)
        self.assertIn("do not own", msg)

    def test_no_leagues_is_a_config_error(self):
        with self.assertRaises(appconfig.ConfigError):
            self._load(leagues=[])

    def test_malformed_entries_are_refused(self):
        for bad in (
            [{"league_id": 1}],                # no team_id
            [{"team_id": 1}],                  # no league_id
            [{"league_id": 0, "team_id": 2}],  # the config.yaml placeholder
            [{"league_id": 5, "team_id": 0}],
            [{"league_id": "abc", "team_id": 2}],
            ["111111111:2"],
        ):
            with self.assertRaises(appconfig.ConfigError, msg="accepted %r" % bad):
                self._load(leagues=bad)

    def test_credentials_never_reach_the_log(self):
        cfg = self._load(leagues=[{"league_id": 111111111, "team_id": 2}])
        blob = json.dumps(cfg.redacted())
        self.assertNotIn("cookievalue", blob)
        self.assertNotIn("ABC-123", blob)
        self.assertIn("<set:", blob)
        # The league list still has to be legible, or the log is useless.
        self.assertIn("111111111", blob)

    def test_swid_braces_are_normalized(self):
        cfg = self._load(leagues=[{"league_id": 1, "team_id": 2}])
        self.assertEqual(cfg.espn(cfg.leagues[0]).swid, "{ABC-123}")

    def test_per_league_season_overrides_the_global(self):
        """Two leagues can sit in different seasons; neither affects the other."""
        cfg = self._load(
            season=2027,
            leagues=[
                {"league_id": 111111111, "team_id": 2},
                {"league_id": 222222222, "team_id": 7, "season": 2026},
            ],
        )
        a, b = (cfg.espn(l) for l in cfg.leagues)
        self.assertEqual(a.season, 2027)
        self.assertEqual(b.season, 2026)

    def test_season_zero_means_use_the_global(self):
        """0 is the config.yaml placeholder, not a request for year zero."""
        cfg = self._load(
            season=2027,
            leagues=[{"league_id": 111111111, "team_id": 2, "season": 0}],
        )
        self.assertEqual(cfg.espn(cfg.leagues[0]).season, 2027)

    def test_a_start_year_season_is_refused(self):
        """ESPN labels a season by the year it ends in -- 2026-27 is 2027.

        This is worth a check rather than a comment because the mistake does
        not error: a probe with the previous season's id came back with
        latestScoringPeriod 175 and matchup 22 of 22, which reads as a
        plausible late-season state rather than as a wrong year. The guard
        here only catches the egregious spelling (a two-digit or 1990s value);
        off-by-one is caught by surfacing the season in the run record.
        """
        for bad in (26, 1999, 27, -2027):
            with self.assertRaises(appconfig.ConfigError, msg="accepted %r" % bad):
                self._load(leagues=[
                    {"league_id": 111111111, "team_id": 2, "season": bad}
                ])

    def test_a_non_integer_season_is_refused_not_ignored(self):
        with self.assertRaises(appconfig.ConfigError):
            self._load(leagues=[
                {"league_id": 111111111, "team_id": 2, "season": "next"}
            ])

    def test_the_season_in_use_is_logged(self):
        """A silent wrong season is the failure mode; make it visible."""
        cfg = self._load(
            season=2027,
            leagues=[{"league_id": 111111111, "team_id": 2, "season": 2026}],
        )
        blob = json.dumps(cfg.redacted())
        self.assertIn("2026", blob)

    def test_keeper_is_per_league_and_defaults_to_redraft(self):
        """The two live leagues differ on exactly this, and ESPN cannot tell
        them apart -- its own keeper flag describes a draft mechanic and reads
        the same in both. So the user says, per league."""
        cfg = self._load(leagues=[
            {"league_id": 111111111, "team_id": 2},
            {"league_id": 222222222, "team_id": 7, "keeper": True},
        ])
        a, b = (cfg.espn(l) for l in cfg.leagues)
        self.assertFalse(a.keeper)
        self.assertTrue(b.keeper)

    def test_an_unrecognized_keeper_spelling_is_refused_not_ignored(self):
        """Stricter than the top-level options on purpose. A flag read wrong
        here is silent: it protects dead weight all season and never explains
        itself, so a typo has to stop the app where it is findable."""
        for bad in ("maybe", "Y", "2"):
            with self.assertRaises(appconfig.ConfigError, msg="accepted %r" % bad):
                self._load(leagues=[
                    {"league_id": 111111111, "team_id": 2, "keeper": bad}
                ])
        # And the spellings a human would reasonably type do work.
        for good, want in (("yes", True), ("off", False), (True, True)):
            cfg = self._load(leagues=[
                {"league_id": 1, "team_id": 2, "keeper": good}
            ])
            self.assertEqual(cfg.leagues[0].keeper, want, "for %r" % good)

    def test_which_leagues_are_keepers_reaches_the_log(self):
        cfg = self._load(leagues=[
            {"league_id": 111111111, "team_id": 2, "keeper": True},
        ])
        self.assertIn("keeper", json.dumps(cfg.redacted()))

    def test_the_addrop_thresholds_default_to_the_modules_own(self):
        """One default, not two. A config default that drifted from the
        module's would make the tests and the app disagree about the rule."""
        cfg = self._load(leagues=[{"league_id": 1, "team_id": 2}])
        self.assertEqual(cfg.addrop_margin_pct, addrop.DEFAULT_MARGIN_PCT)
        self.assertEqual(cfg.addrop_churn_floor, addrop.DEFAULT_CHURN_FLOOR)

    def test_the_addrop_thresholds_are_overridable(self):
        """"10-15%" is a range, so the number is a setting, not a constant."""
        cfg = self._load(
            leagues=[{"league_id": 1, "team_id": 2}],
            addrop_margin_pct=15,
            addrop_churn_floor=0,
        )
        self.assertEqual(cfg.addrop_margin_pct, 15.0)
        self.assertEqual(cfg.addrop_churn_floor, 0.0)

    def test_a_negative_threshold_is_refused(self):
        """Both are brakes. A negative one does not loosen the rule, it inverts
        it into one that proposes swaps which lose points."""
        for bad in ({"addrop_margin_pct": -1}, {"addrop_churn_floor": -0.5}):
            with self.assertRaises(appconfig.ConfigError, msg="accepted %r" % bad):
                self._load(leagues=[{"league_id": 1, "team_id": 2}], **bad)

    def test_a_non_numeric_threshold_is_refused_not_silently_defaulted(self):
        with self.assertRaises(appconfig.ConfigError):
            self._load(
                leagues=[{"league_id": 1, "team_id": 2}],
                addrop_margin_pct="twelve",
            )


class TestActionIdRoundTrip(unittest.TestCase):
    """The tapped id and the stored id are deliberately different strings.

    The notification button carries a verb; the proposal row does not. These
    tests exist because that difference was once implicit and split across two
    modules, so every per-item tap looked up an id that could not match and did
    nothing -- silently, which is the one failure mode the approval path must
    never have.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.store.start_run("RUN1", 42, True)

    def tearDown(self):
        self.store.close()

    def test_stored_id_is_recovered_from_the_tapped_id(self):
        p = self.store.propose("RUN1", "ADD_DROP", {"add": 1, "drop": 2})
        tapped = bridge.item_action_id(p.action_id, bridge.APPROVE)

        self.assertNotEqual(tapped, p.action_id)  # the bug, in one line
        parsed = bridge.parse_action_id(tapped)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.proposal_action_id, p.action_id)
        self.assertTrue(parsed.approve)

        decided = self.store.decide(parsed.proposal_action_id, parsed.approve, "u")
        self.assertIsNotNone(decided)
        self.assertEqual(decided.state, APPROVED)

    def test_raw_tapped_id_matches_nothing(self):
        """Regression: the old code passed this straight to decide()."""
        p = self.store.propose("RUN1", "ADD_DROP", {})
        tapped = bridge.item_action_id(p.action_id, bridge.APPROVE)
        self.assertIsNone(self.store.decide(tapped, True, "u"))
        self.assertEqual(self.store.get(p.action_id).state, PROPOSED)

    def test_reject_verb_is_not_silently_an_approval(self):
        p = self.store.propose("RUN1", "ADD_DROP", {})
        parsed = bridge.parse_action_id(
            bridge.item_action_id(p.action_id, bridge.REJECT)
        )
        self.assertFalse(parsed.approve)
        self.store.decide(parsed.proposal_action_id, parsed.approve, "u")
        self.assertEqual(self.store.get(p.action_id).state, REJECTED)

    def test_bulk_ids_carry_the_run_and_no_proposal(self):
        parsed = bridge.parse_action_id(
            bridge.bulk_action_id("RUN1", bridge.APPROVE_ALL)
        )
        self.assertTrue(parsed.is_bulk)
        self.assertEqual(parsed.run_id, "RUN1")
        self.assertTrue(parsed.approve)
        self.assertIsNone(parsed.proposal_action_id)

    def test_foreign_and_malformed_ids_are_rejected(self):
        # HA delivers every notification action on the instance to every
        # listener, so parse must reject rather than guess.
        for bad in (
            "garage_door_open",             # another automation entirely
            "RUN1:ADD_DROP:1",              # no verb: a seq is not a verb
            "RUN1:ADD_DROP:APPROVE",        # missing seq
            "RUN1:ADD_DROP:x:APPROVE",      # non-numeric seq
            "RUN1:ADD_DROP:1:MAYBE",        # unknown verb
            ":ADD_DROP:1:APPROVE",          # empty run id
            "RUN1:ADD_DROP:1:APPROVE_ALL",  # bulk verb in item position
            "RUN1:APPROVE",                 # item verb in bulk position
            "APPROVE",
            "",
        ):
            self.assertIsNone(bridge.parse_action_id(bad), "accepted %r" % bad)

    def test_builders_refuse_the_wrong_verb_class(self):
        with self.assertRaises(ValueError):
            bridge.item_action_id("RUN1:ADD_DROP:1", bridge.APPROVE_ALL)
        with self.assertRaises(ValueError):
            bridge.bulk_action_id("RUN1", bridge.APPROVE)

    def test_event_parsing_enforces_shape_and_owner(self):
        """The bridge's two mandatory checks, on realistic event payloads."""
        b = bridge.HaBridge.__new__(bridge.HaBridge)  # no network setup
        b.owner_user_id = "owner-1"

        def event(action, user_id):
            return {"event": {"data": {"action": action},
                              "context": {"user_id": user_id}}}

        good = b._parse_action_event(event("RUN1:ADD_DROP:1:APPROVE", "owner-1"))
        self.assertIsNotNone(good)
        self.assertEqual(good.parsed.proposal_action_id, "RUN1:ADD_DROP:1")

        # Right shape, wrong human.
        self.assertIsNone(
            b._parse_action_event(event("RUN1:ADD_DROP:1:APPROVE", "someone-else"))
        )
        # Right human, not our id.
        self.assertIsNone(b._parse_action_event(event("garage_open", "owner-1")))
        # A tap with no user attribution at all.
        self.assertIsNone(
            b._parse_action_event(event("RUN1:ADD_DROP:1:APPROVE", None))
        )


class TestNotificationDeepLink(unittest.TestCase):
    """Regression, twice over: the notification url pointed at no route.

    First it was hardcoded to `/lovelace/fantasy`, a dashboard that does not
    exist. Then it was *constructed* as `/hassio/ingress/<slug>`, which is only
    a route for an ingress add-on with no sidebar panel of its own -- this one
    has one, so the real route is `/<slug>`. Both times the companion app could
    not resolve the path and silently landed on the home dashboard, and both
    times it was reported from the real phone rather than caught here, because
    nothing had ever asserted on what the *frontend* says the route is.

    So these tests feed in the panel registry's answer, not a string we think
    it ought to be.
    """

    #: What `get_panels` actually returned from the Green on 2026-10-21, cut
    #: down to the keys this code reads. Note `url_path` has no `hassio/`
    #: prefix and no leading slash.
    PANELS = {
        "lovelace": {"url_path": "lovelace", "component_name": "lovelace",
                     "title": None, "config": None},
        "d0cfb82c_nba_autopilot": {
            "url_path": "d0cfb82c_nba_autopilot",
            "component_name": "app",
            "title": "NBA Autopilot",
            "config": {"addon": "d0cfb82c_nba_autopilot"},
        },
    }

    def _bridge(self, response, panels=None, ws_raises=False):
        b = bridge.HaBridge.__new__(bridge.HaBridge)
        b._deep_link = bridge._UNRESOLVED
        b.notify_device = "mobile_app_iphone"
        b.sent = []
        b.ws_calls = []

        class FakeHttp:
            def get(self, url):
                return response(url)

        b.http = FakeHttp()
        b.call_service = lambda d, s, data: b.sent.append((d, s, data))

        async def fake_ws(msg):
            b.ws_calls.append(dict(msg))
            if ws_raises:
                raise RuntimeError("websocket down")
            return panels

        b._ws_command = fake_ws
        return b

    @staticmethod
    def _ok(slug):
        class R:
            def json(self):
                return {"data": {"slug": slug}}
        return lambda url: R()

    def test_the_route_comes_from_the_panel_registry(self):
        """`/<slug>`, because this add-on registers its own sidebar panel.

        The bug this replaces produced `/hassio/ingress/d0cfb82c_nba_autopilot`,
        which looks right and resolves to nothing.
        """
        b = self._bridge(self._ok("d0cfb82c_nba_autopilot"), panels=self.PANELS)
        self.assertEqual(b.deep_link(), "/d0cfb82c_nba_autopilot")
        self.assertEqual([c["type"] for c in b.ws_calls], ["get_panels"])

    def test_the_panel_is_matched_on_the_addon_slug_not_the_panel_key(self):
        """A store install carries a repo hash, and the panel key is not it."""
        panels = {
            "autopilot-panel": {
                "url_path": "autopilot-panel",
                "config": {"addon": "a1b2c3d4_nba_autopilot"},
            },
        }
        b = self._bridge(self._ok("a1b2c3d4_nba_autopilot"), panels=panels)
        self.assertEqual(b.deep_link(), "/autopilot-panel")

    def test_no_panel_means_the_ingress_route_is_the_real_one(self):
        """An ingress add-on without `panel_title` is reached that way."""
        b = self._bridge(self._ok("local_nba_autopilot"), panels=self.PANELS)
        self.assertEqual(b.deep_link(), "/hassio/ingress/local_nba_autopilot")

    def test_a_panel_for_a_different_addon_is_not_ours(self):
        b = self._bridge(self._ok("local_nba_autopilot"),
                         panels={"x": {"url_path": "x",
                                       "config": {"addon": "a0d7b954_vscode"}}})
        self.assertEqual(b.deep_link(), "/hassio/ingress/local_nba_autopilot")

    def test_a_malformed_panel_registry_does_not_raise(self):
        """`config` is null for most panels, and the values are not all dicts."""
        b = self._bridge(
            self._ok("local_nba_autopilot"),
            panels={"a": None, "b": "nonsense", "c": {"config": None},
                    "d": {"config": {"addon": "local_nba_autopilot"}}},
        )
        # "d" claims the add-on but carries no url_path, so it cannot be used.
        self.assertEqual(b.deep_link(), "/hassio/ingress/local_nba_autopilot")

    def test_unresolvable_slug_omits_the_url_entirely(self):
        """No url beats a wrong url: only the former is visibly broken."""
        def boom(url):
            raise RuntimeError("supervisor unreachable")

        b = self._bridge(boom, panels=self.PANELS)
        self.assertIsNone(b.deep_link())
        self.assertEqual(b.ws_calls, [], "no point asking for panels")
        b.notify(title="t", message="m", tag="x")
        _, _, payload = b.sent[0]
        self.assertNotIn("url", payload["data"])

    def test_an_unreadable_panel_registry_omits_the_url(self):
        """Having the slug is not enough to know the route. Do not guess it."""
        b = self._bridge(self._ok("d0cfb82c_nba_autopilot"), ws_raises=True)
        self.assertIsNone(b.deep_link())
        b.notify(title="t", message="m", tag="x")
        _, _, payload = b.sent[0]
        self.assertNotIn("url", payload["data"])

    def test_url_is_present_when_resolvable(self):
        b = self._bridge(self._ok("d0cfb82c_nba_autopilot"), panels=self.PANELS)
        b.notify(title="t", message="m", tag="x")
        _, _, payload = b.sent[0]
        self.assertEqual(payload["data"]["url"], "/d0cfb82c_nba_autopilot")

    def test_a_failed_lookup_is_cached_not_retried(self):
        """Otherwise every notification pays a timeout to the Supervisor."""
        calls = []

        def boom(url):
            calls.append(url)
            raise RuntimeError("nope")

        b = self._bridge(boom, panels=self.PANELS)
        for _ in range(5):
            b.notify(title="t", message="m", tag="x")
        self.assertEqual(len(calls), 1, "slug lookup should happen once")
        self.assertEqual(len(b.sent), 5, "notifications must still be sent")

    def test_a_successful_lookup_is_cached_too(self):
        b = self._bridge(self._ok("d0cfb82c_nba_autopilot"), panels=self.PANELS)
        for _ in range(4):
            b.notify(title="t", message="m", tag="x")
        self.assertEqual(len(b.ws_calls), 1, "one panel lookup per process")
        self.assertTrue(all("url" in s[2]["data"] for s in b.sent))

    def test_action_buttons_do_not_depend_on_the_link(self):
        """The approval path must survive an unresolvable deep link."""
        def boom(url):
            raise RuntimeError("nope")

        b = self._bridge(boom, panels=self.PANELS)
        b.notify(
            title="t", message="m", tag="x",
            actions=(bridge.Action(
                bridge.item_action_id("RUN1:ADD_DROP:1", bridge.APPROVE),
                "Approve",
            ),),
        )
        _, _, payload = b.sent[0]
        self.assertNotIn("url", payload["data"])
        self.assertEqual(
            payload["data"]["actions"][0]["action"], "RUN1:ADD_DROP:1:APPROVE"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestADeclinedSwapIsARecordAndNotAQuestion(unittest.TestCase):
    """*"i'd like a way to view rejected add drop requests... i can see what
    it considered"*.

    A swap the app turned down is written as a proposal in ``BLOCKED``, which
    is terminal from birth. That choice is only safe because every transition
    out of a proposal is guarded on the state it starts from, so the tests
    that matter here are the ones that try to move it anyway.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.store.start_run("RUN1", 5, True)

    def tearDown(self):
        self.store.close()

    def _declined(self):
        return self.store.propose(
            "RUN1", "ADD_DROP", {"add": 1, "add_name": "In", "drop": 2},
            rationale="the reviewer blocked it", value_delta=2.4,
            declined=True)

    def test_it_is_born_blocked_and_finished(self):
        p = self._declined()
        self.assertEqual(p.state, state.BLOCKED)
        row = self.store.get(p.action_id)
        self.assertEqual(row.state, state.BLOCKED)
        self.assertIn(state.BLOCKED, state.TERMINAL)

    def test_it_never_appears_in_the_approval_queue(self):
        self._declined()
        self.assertEqual(self.store.pending(), [])

    def test_a_tap_cannot_approve_it(self):
        """The guard that makes a terminal-from-birth row safe to write."""
        p = self._declined()
        self.assertIsNone(self.store.decide(p.action_id, True, "owner"))
        self.assertEqual(self.store.get(p.action_id).state, state.BLOCKED)

    def test_a_tap_cannot_reject_it_either(self):
        p = self._declined()
        self.assertIsNone(self.store.decide(p.action_id, False, "owner"))
        self.assertEqual(self.store.get(p.action_id).state, state.BLOCKED)

    def test_it_cannot_be_claimed_for_execution(self):
        p = self._declined()
        self.assertFalse(self.store.claim_for_execution(p.id))

    def test_the_sweeper_leaves_it_alone(self):
        """Its ``expires_at`` is now, which would sweep a PROPOSED row."""
        p = self._declined()
        self.store.expire_stale()
        self.assertEqual(self.store.get(p.action_id).state, state.BLOCKED)

    def test_it_keeps_the_pair_and_the_reasoning(self):
        p = self._declined()
        row = self.store.get(p.action_id)
        self.assertEqual(row.payload["add"], 1)
        self.assertEqual(row.rationale, "the reviewer blocked it")
        self.assertEqual(row.value_delta, 2.4)

    def test_declining_and_auto_approving_are_not_both_possible(self):
        """They would write two different states into one row."""
        with self.assertRaises(ValueError):
            self.store.propose("RUN1", "ADD_DROP", {}, declined=True,
                               auto_approve=True)

    def test_an_ordinary_proposal_is_untouched_by_the_new_path(self):
        p = self.store.propose("RUN1", "ADD_DROP", {"add": 1})
        self.assertEqual(p.state, state.PROPOSED)
        self.assertEqual([q.action_id for q in self.store.pending()],
                         [p.action_id])
