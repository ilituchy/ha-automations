"""Tests for ESPN's actual wire behavior, as observed against live leagues.

Every assertion in this file encodes something that was *wrong* in the code
until a real request proved it, during a write-surface probe against a
throwaway league on 2026-10-20. They are regression tests in the strict sense:
each one fails against the implementation that shipped before that probe.

The four findings:

1. A dropped player lingers in the roster ``entries`` array, still carrying his
   old ``lineupSlotId``, distinguished only by ``status``/``onTeamId``. So
   ``len(entries)`` is not a roster count.
2. ESPN gates permitted ``items`` against the transaction ``type``. A lone DROP
   needs ``ROSTER``; ``FREEAGENT`` refuses it outright.
3. A cancel is a *new* transaction pointing at the old one via
   ``relatedTransactionId``, not a mutation addressed by ``id``.
4. Whether a pickup is an instant add or a waiver claim is a property of the
   player, not of the league.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

from espn import ids  # noqa: E402
from espn.read import EspnConfig, EspnRead  # noqa: E402
from espn.write import EspnWrite, PreflightError  # noqa: E402
from models import LeagueSettings, RosterEntry  # noqa: E402

PG, C, UTIL, BE, IR = 0, 4, 11, 12, 13


# ---------------------------------------------------------------------------
# read side: the roster ghost
# ---------------------------------------------------------------------------


def _entry(pid, name, slot, *, status="ONTEAM", on_team=7, omit=()):
    """One ``mRoster`` entry. ``omit`` drops keys entirely, to test absence."""
    ppe = {
        "status": status,
        "onTeamId": on_team,
        "player": {
            "id": pid,
            "fullName": name,
            "proTeamId": 2,
            "eligibleSlots": [slot, UTIL, BE],
            "injuryStatus": "ACTIVE",
        },
    }
    for key in omit:
        ppe.pop(key, None)
    return {"playerId": pid, "lineupSlotId": slot, "playerPoolEntry": ppe}


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload


class _RosterClient:
    def __init__(self, entries):
        self._entries = entries

    def get(self, url, params=None, headers=None):
        return _Resp({"teams": [{"id": 7, "roster": {"entries": self._entries}}]})

    def post(self, *a, **k):
        raise AssertionError("read path must not POST")


def _reader(entries):
    cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}", season=2027)
    return EspnRead(cfg, _RosterClient(entries))


class TestRosterGhost(unittest.TestCase):
    def test_dropped_player_is_excluded_despite_keeping_his_slot(self):
        """The exact live shape: a dropped player still sitting in slot BE.

        Before the fix this returned 3 players, and the optimizer would then
        assign a lineup slot to someone no longer on the team -- which ESPN
        rejects, losing the whole day's lineup.
        """
        got = _reader([
            _entry(1, "Guard One", PG),
            _entry(2, "Center One", C),
            _entry(3, "Dropped Guy", BE, status="WAIVERS", on_team=0),
        ]).roster()

        self.assertEqual([p.player_id for p in got], [1, 2])
        self.assertNotIn("Dropped Guy", [p.name for p in got])

    def test_full_roster_of_onteam_players_is_untouched(self):
        """Both live leagues are 100% ONTEAM; the filter must be a no-op there."""
        got = _reader([
            _entry(1, "Guard One", PG),
            _entry(2, "Center One", C),
            _entry(3, "Bench Guy", BE),
        ]).roster()
        self.assertEqual([p.player_id for p in got], [1, 2, 3])

    def test_missing_status_and_on_team_keeps_the_player(self):
        """The asymmetry is deliberate: exclude only on positive evidence.

        Wrongly keeping a departed player costs one rejected write, which is
        loud. Wrongly excluding a real player silently shrinks the roster below
        its slot count and leaves him unmanaged, which is quiet and would be
        blamed on the optimizer.
        """
        got = _reader([
            _entry(1, "Guard One", PG, omit=("status", "onTeamId")),
            _entry(2, "Center One", C, omit=("status",)),
            _entry(3, "Bench Guy", BE, omit=("onTeamId",)),
        ]).roster()
        self.assertEqual([p.player_id for p in got], [1, 2, 3])

    def test_player_belonging_to_another_team_is_excluded(self):
        got = _reader([
            _entry(1, "Guard One", PG),
            _entry(2, "Someone Elses", C, status="ONTEAM", on_team=3),
        ]).roster()
        self.assertEqual([p.player_id for p in got], [1])

    def test_acquisition_status_is_carried_off_the_roster_read(self):
        got = _reader([_entry(1, "Guard One", PG)]).roster()
        self.assertEqual(got[0].acquisition_status, "ONTEAM")
        self.assertFalse(got[0].must_claim_on_waivers)


# ---------------------------------------------------------------------------
# write side: transaction type, cancel shape, acquisition mode
# ---------------------------------------------------------------------------


class _CapturingClient:
    """Records POST bodies and replies with a plausible executed transaction."""

    def __init__(self, status="EXECUTED"):
        self.bodies = []
        self._status = status

    def post(self, url, content=None, headers=None, cookies=None):
        body = json.loads(content.decode("utf-8"))
        self.bodies.append(body)
        return _Resp({
            "id": "dc39b83f-3bf9-4f0f-9b14-71472ea6f7fa",
            "status": self._status,
            "items": body.get("items") or [],
            "proposedDate": 1792546976430,
        })

    def get(self, *a, **k):
        raise AssertionError("write path must not GET directly")

    @property
    def last(self):
        return self.bodies[-1]


def _settings(**over):
    base = dict(
        league_id=1,
        season_id=2027,
        name="test",
        scoring_type="H2H_POINTS",
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


def _player(pid, name, slot, eligible=(UTIL, BE), **kw):
    return RosterEntry(
        player_id=pid,
        name=name,
        current_slot=slot,
        eligible_slots=frozenset(eligible),
        pro_team_id=2,
        injury_status=kw.get("injury_status", "ACTIVE"),
        acquisition_status=kw.get("acquisition_status", "ONTEAM"),
    )


class _FakeRead:
    def __init__(self, roster, pending=(), pool=None):
        self._roster = list(roster)
        self._pending = list(pending)
        self._pool = dict(pool or {})

    def roster(self, team_id=None):
        return list(self._roster)

    def league_settings(self):
        return _settings()

    def my_team_ids(self):
        return {7}

    def pending_transactions(self, team_id=None):
        return list(self._pending)

    def player_in_pool(self, player_id):
        return self._pool.get(player_id)


ROSTER = [
    _player(1, "Guard One", PG),
    _player(2, "Center One", C),
    _player(3, "Bench Guy", BE),
]

FREE_AGENT = _player(50, "Free Agent", BE, acquisition_status="FREEAGENT")
ON_WAIVERS = _player(51, "Waiver Guy", BE, acquisition_status="WAIVERS")


def _writer(roster=ROSTER, pending=(), pool=None, status="EXECUTED"):
    cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}", season=2027)
    http = _CapturingClient(status)
    pool = pool if pool is not None else {50: FREE_AGENT, 51: ON_WAIVERS}
    w = EspnWrite(cfg, http, _FakeRead(roster, pending, pool), shadow_mode=False)
    return w, http


class TestTransactionType(unittest.TestCase):
    """ESPN gates ``items`` against ``type``; the branch must be on the items."""

    def test_lone_drop_is_a_roster_transaction(self):
        """FREEAGENT here gets TRAN_INVALID_ITEM_TYPE_COUNT from ESPN.

        This is the bug that would have broken *every* drop the app made.
        """
        w, http = _writer()
        w.add_drop(drop=3, waiver=False)
        self.assertEqual(http.last["type"], ids.TRAN_ROSTER)
        self.assertEqual([i["type"] for i in http.last["items"]], ["DROP"])

    def test_instant_add_is_a_freeagent_transaction(self):
        w, http = _writer()
        w.add_drop(add=50, waiver=False)
        self.assertEqual(http.last["type"], ids.TRAN_FREEAGENT)

    def test_instant_add_with_a_paired_drop_is_still_freeagent(self):
        """FREEAGENT permits at most one ADD item -- a DROP alongside is fine.

        Worth pinning down explicitly: ESPN's rejection message for the lone
        drop reads "may only contain one item for MoveType: ADD", which is
        easy to misread as "only one item total". It is not, and reading it
        that way would have sent add+drop as ROSTER, which ESPN refuses with
        TRAN_INVALID_MOVETYPE because ROSTER cannot carry an ADD.
        """
        w, http = _writer()
        w.add_drop(add=50, drop=3, waiver=False)
        self.assertEqual(http.last["type"], ids.TRAN_FREEAGENT)
        self.assertEqual(
            sorted(i["type"] for i in http.last["items"]), ["ADD", "DROP"]
        )

    def test_waiver_claim_is_a_waiver_transaction_with_or_without_a_drop(self):
        w, http = _writer()
        w.add_drop(add=51, waiver=True)
        self.assertEqual(http.last["type"], ids.TRAN_WAIVER)
        w.add_drop(add=51, drop=3, waiver=True)
        self.assertEqual(http.last["type"], ids.TRAN_WAIVER)
        self.assertEqual(
            sorted(i["type"] for i in http.last["items"]), ["ADD", "DROP"]
        )


PENDING_CLAIM = {
    "id": "407f4271-5fe8-4fd6-8268-babfdf1c3542",
    "type": "WAIVER",
    "status": "PENDING",
    "scoringPeriodId": 1,
    "items": [{"playerId": 1966, "type": "ADD"}],
}


class TestCancelShape(unittest.TestCase):
    def test_cancel_addresses_the_claim_via_related_transaction_id(self):
        """``id`` is discarded and replaced by ESPN; the target goes elsewhere.

        Sending it as ``id`` yields 409 "Transaction with ID null could not be
        found" -- ESPN was reporting a null ``relatedTransactionId`` all along.
        """
        w, http = _writer(pending=[PENDING_CLAIM], status="CANCELED")
        w.cancel(PENDING_CLAIM["id"])
        body = http.last
        self.assertEqual(body["relatedTransactionId"], PENDING_CLAIM["id"])
        self.assertNotIn("id", body)
        self.assertEqual(body["executionType"], ids.EXEC_CANCEL)

    def test_cancel_echoes_type_and_scoring_period_from_the_live_claim(self):
        """Both are mandatory, and neither can be assumed.

        Omitting ``type`` gives TRAN_INVALID_TYPE; omitting
        ``scoringPeriodId`` gives "can only be executed in the current scoring
        period". Note the claim's period (1) differs from the league's current
        one (5) in this fixture, so a test that passed by coincidence would
        fail here.
        """
        w, http = _writer(pending=[PENDING_CLAIM], status="CANCELED")
        w.cancel(PENDING_CLAIM["id"])
        self.assertEqual(http.last["type"], "WAIVER")
        self.assertEqual(http.last["scoringPeriodId"], 1)

    def test_cancelling_something_not_pending_never_reaches_the_wire(self):
        w, http = _writer(pending=[PENDING_CLAIM])
        with self.assertRaises(PreflightError) as caught:
            w.cancel("00000000-0000-0000-0000-000000000000")
        self.assertIn("not pending", str(caught.exception))
        self.assertEqual(http.bodies, [])


class TestUnpairedAddNeedsAnActiveSpot(unittest.TestCase):
    """The last line of defence against the IR-slot miscount.

    ``_settings`` is 5 active slots (PG, C, UTIL, 2x BE) plus 1 IR, so a roster
    of 5 healthy players is full however many IR slots sit empty. Summing all
    six slot counts said otherwise, and the decision layer acted on it: a live
    league was proposed an unpaired add that ESPN refused at the wire. Preflight
    is the half of the fix that holds even if a caller gets the arithmetic wrong
    again, which is why it is checked here and not only in ``solve/addrop``.
    """

    FULL = ROSTER + [
        _player(4, "Bench Two", BE),
        _player(5, "Util One", UTIL),
        _player(6, "Hurt Guy", IR, injury_status="OUT"),
    ]

    def test_an_empty_ir_slot_does_not_license_an_unpaired_add(self):
        w, _http = _writer(roster=self.FULL)
        with self.assertRaises(PreflightError) as caught:
            w.add_drop(add=50, waiver=False)
        self.assertIn("5/5 active spots", str(caught.exception))

    def test_the_same_add_goes_through_once_a_drop_is_paired_with_it(self):
        """The refusal is of the *unpaired* add, not of the pickup."""
        w, http = _writer(roster=self.FULL)
        w.add_drop(add=50, drop=3, waiver=False)
        self.assertEqual(
            sorted(i["type"] for i in http.last["items"]), ["ADD", "DROP"]
        )

    def test_a_drop_off_ir_does_not_make_room_for_the_add_either(self):
        """Pairing is not a formality -- the drop has to vacate an *active*
        slot. Dropping the man on IR frees an IR slot, so the active count
        does not move and ESPN refuses add+drop exactly as readily."""
        w, _http = _writer(roster=self.FULL)
        with self.assertRaises(PreflightError) as caught:
            w.add_drop(add=50, drop=6, waiver=False)
        self.assertIn("Hurt Guy", str(caught.exception))
        self.assertIn("frees no active spot", str(caught.exception))

    def test_a_player_on_ir_leaves_his_active_spot_free(self):
        """Five players, one of them on IR, in a league with 5 active slots.

        Four healthy men in five slots: there is a real spot, and preflight
        must not invent a fullness that would force a needless drop. The count
        has to skip IR occupants for the same reason the cap has to skip IR
        slots -- fixing only one side turns this add into a refusal.
        """
        w, http = _writer(roster=ROSTER + [
            _player(4, "Bench Two", BE),
            _player(6, "Hurt Guy", IR, injury_status="OUT"),
        ])
        w.add_drop(add=50, waiver=False)
        self.assertEqual([i["type"] for i in http.last["items"]], ["ADD"])


class TestAcquisitionMode(unittest.TestCase):
    def test_instant_add_of_a_waivers_player_is_blocked_locally(self):
        """ESPN would reject this; we say so before spending the request."""
        w, http = _writer()
        with self.assertRaises(PreflightError) as caught:
            w.add_drop(add=51, waiver=False)
        self.assertIn("on waivers", str(caught.exception))
        self.assertEqual(http.bodies, [])

    def test_claim_for_a_waivers_player_is_allowed(self):
        w, http = _writer()
        w.add_drop(add=51, waiver=True)
        self.assertEqual(len(http.bodies), 1)

    def test_adding_a_player_on_another_roster_is_blocked(self):
        rostered = _player(52, "Taken Guy", BE, acquisition_status="ONTEAM")
        w, http = _writer(pool={52: rostered})
        with self.assertRaises(PreflightError) as caught:
            w.add_drop(add=52, waiver=False)
        self.assertIn("not available", str(caught.exception))
        self.assertEqual(http.bodies, [])

    def test_unknown_player_id_is_blocked(self):
        w, http = _writer(pool={})
        with self.assertRaises(PreflightError) as caught:
            w.add_drop(add=999, waiver=False)
        self.assertIn("no player row", str(caught.exception))
        self.assertEqual(http.bodies, [])

    def test_a_drop_does_not_consult_the_pool(self):
        """A drop is not an acquisition; requiring a pool row would break it."""
        w, http = _writer(pool={})
        w.add_drop(drop=3, waiver=False)
        self.assertEqual(len(http.bodies), 1)


# ---------------------------------------------------------------------------
# read side: the matchup row, probed live on 2026-10-21
# ---------------------------------------------------------------------------


class _PayloadClient:
    """Serves one fixed payload for every view. Records the views asked for."""

    def __init__(self, payload):
        self._payload = payload
        self.views = []

    def get(self, url, params=None, headers=None):
        # ``params`` is a list of ``(key, value)`` pairs, because ESPN wants
        # repeated ``view=`` keys rather than one comma-joined value.
        self.views.append([v for k, v in (params or []) if k == "view"])
        return _Resp(self._payload)

    def post(self, *a, **k):
        raise AssertionError("read path must not POST")


def _side(team_id, *, points=None, by_period=None, gp=None, gp_field="value"):
    side = {"teamId": team_id}
    if points is not None:
        side["totalPoints"] = points
    if by_period is not None:
        side["pointsByScoringPeriod"] = by_period
    if gp is not None:
        side["cumulativeScore"] = {
            "statBySlot": {
                str(ids.TEAM_TOTAL_LIMIT_SLOT): {
                    "statId": ids.GP_STAT,
                    gp_field: gp,
                    "limitExceeded": False,
                }
            }
        }
    return side


def _score_reader(schedule, *, teams=(), current_matchup=1):
    cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}", season=2027)
    http = _PayloadClient({
        "status": {"currentMatchupPeriod": current_matchup},
        "schedule": schedule,
        "teams": list(teams),
    })
    return EspnRead(cfg, http), http


class TestGamesUsedWire(unittest.TestCase):
    """``statBySlot`` carries ``value``, and ``schedule`` is the whole season.

    Both of these shipped wrong. The field was read as ``score``, so every
    lookup missed and ``games_used`` returned ``{}`` on every call since it was
    written; and the season-long ``schedule`` was summed unfiltered, which was
    invisible only because the first bug kept the loop from ever finding a
    number to add. Caught by a live probe, not by reading the code -- the code
    and its own docstring agreed with each other.
    """

    def test_reads_the_value_field_espn_actually_sends(self):
        read, _ = _score_reader([
            {"matchupPeriodId": 1,
             "home": _side(7, gp=2.0),
             "away": _side(4)},
        ])
        self.assertEqual(read.games_used(), {ids.TEAM_TOTAL_LIMIT_SLOT: 2})

    def test_the_old_score_spelling_still_reads(self):
        """Kept as a fallback rather than assuming one probe saw every shape."""
        read, _ = _score_reader([
            {"matchupPeriodId": 1, "home": _side(7, gp=3.0, gp_field="score")},
        ])
        self.assertEqual(read.games_used(), {ids.TEAM_TOTAL_LIMIT_SLOT: 3})

    def test_other_matchup_periods_do_not_count_against_this_week(self):
        """The failure this would have caused on the first day of week 2: last
        week's games charged to this week's cap, arriving at Sunday unable to
        start anyone."""
        read, _ = _score_reader([
            {"matchupPeriodId": 1, "home": _side(7, gp=38.0)},
            {"matchupPeriodId": 2, "home": _side(7, gp=2.0)},
        ], current_matchup=2)
        self.assertEqual(read.games_used(), {ids.TEAM_TOTAL_LIMIT_SLOT: 2})
        self.assertEqual(
            read.games_used(matchup_period=1),
            {ids.TEAM_TOTAL_LIMIT_SLOT: 38},
        )

    def test_an_unscored_row_contributes_nothing_not_zero(self):
        read, _ = _score_reader([{"matchupPeriodId": 1, "home": _side(7)}])
        self.assertEqual(read.games_used(), {})


class TestMatchupScoreWire(unittest.TestCase):
    def test_reads_both_sides_and_names_the_opponent(self):
        read, _ = _score_reader(
            [{"matchupPeriodId": 1,
              "home": _side(7, points=110.0),
              "away": _side(6, points=92.0)}],
            teams=[{"id": 7, "name": "Us"},
                   {"id": 6, "name": "Ryan's Remarkable Team"}],
        )
        got = read.matchup_score()
        self.assertEqual(got.my_points, 110.0)
        self.assertEqual(got.opp_points, 92.0)
        self.assertEqual(got.opp_team_id, 6)
        self.assertEqual(got.opp_name, "Ryan's Remarkable Team")
        self.assertEqual(got.margin, 18.0)

    def test_falls_back_to_the_per_period_breakdown(self):
        """``totalPoints`` and the sum of ``pointsByScoringPeriod`` agreed on
        every live row; the sum is for a row ESPN has not totalled yet."""
        read, _ = _score_reader([
            {"matchupPeriodId": 1,
             "home": _side(7, by_period={"1": 20.0, "2": 25.0}),
             "away": _side(4, by_period={"1": 30.0})},
        ])
        got = read.matchup_score()
        self.assertEqual(got.my_points, 45.0)
        self.assertEqual(got.opp_points, 30.0)

    def test_a_bye_is_not_a_tie(self):
        """One-sided row: there is no opponent, and valuing it as 0-0 would
        make every marginal pickup look like it decides a matchup."""
        read, _ = _score_reader([
            {"matchupPeriodId": 1, "home": _side(7, points=50.0)},
        ])
        got = read.matchup_score()
        self.assertIsNone(got.opp_team_id)
        self.assertEqual(got.opp_points, 0.0)

    def test_no_row_at_all_is_none(self):
        read, _ = _score_reader([
            {"matchupPeriodId": 1, "home": _side(3), "away": _side(4)},
        ])
        self.assertIsNone(read.matchup_score())


class TestAcquisitionsUsedWire(unittest.TestCase):
    def test_counts_are_keyed_by_matchup_period(self):
        cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}",
                         season=2027)
        http = _PayloadClient({"teams": [
            {"id": 7, "transactionCounter": {
                "acquisitions": 9,
                "matchupAcquisitionTotals": {"1": 1, "2": 3},
            }},
        ]})
        self.assertEqual(EspnRead(cfg, http).acquisitions_used(), {1: 1, 2: 3})

    def test_an_empty_counter_means_none_used_not_unknown(self):
        """Unlike the games-played cap: ESPN maintains this counter for every
        team unconditionally, so absence is a real zero."""
        cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}",
                         season=2027)
        http = _PayloadClient({"teams": [
            {"id": 7, "transactionCounter": {"matchupAcquisitionTotals": {}}},
        ]})
        self.assertEqual(EspnRead(cfg, http).acquisitions_used(), {})


if __name__ == "__main__":
    unittest.main()
