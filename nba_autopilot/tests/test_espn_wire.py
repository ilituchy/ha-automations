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

import dataclasses
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

import main as main_mod  # noqa: E402
from espn import ids  # noqa: E402
from espn.read import EspnConfig, EspnRead  # noqa: E402
from espn.write import EspnWrite, PreflightError  # noqa: E402
from models import LeagueSettings, RosterEntry  # noqa: E402
from solve.calendar import SeasonCalendar  # noqa: E402

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


class TestMarketSignals(unittest.TestCase):
    """The ownership block, read off the exact live shape.

    Field spellings copied from a 2026-10-21 probe of both payloads. They are
    nested two different ways -- ``ownership.*`` flat, ``draftRanksByRankType``
    keyed by rank type -- and a wrong guess at either reads as "ESPN told us
    nothing", which is precisely the state the judge is supposed to be able to
    tell apart from a real zero.
    """

    def entry(self, **over):
        e = _entry(1, "Market Guy", PG)
        player = e["playerPoolEntry"]["player"]
        player["ownership"] = {
            "percentOwned": 88.43,
            "averageDraftPosition": 81.0,
            "percentStarted": 70.1,
            "auctionValueAverage": 4.2,
        }
        player["draftRanksByRankType"] = {
            "STANDARD": {"rank": 81, "rankType": "STANDARD", "auctionValue": 4},
            "ROTO": {"rank": 77, "rankType": "ROTO", "auctionValue": 5},
        }
        player["seasonOutlook"] = "A rookie with a first-overall pedigree."
        player.update(over)
        return e

    def test_the_four_readings_come_off_the_wire(self):
        got = _reader([self.entry()]).roster()[0]
        self.assertAlmostEqual(got.percent_owned, 88.43)
        self.assertAlmostEqual(got.draft_adp, 81.0)
        # STANDARD, not ROTO: the two disagree, so which one is read is a
        # decision rather than an implementation detail.
        self.assertEqual(got.draft_rank, 81)
        self.assertEqual(got.outlook, "A rookie with a first-overall pedigree.")

    def test_an_absent_block_reads_as_unknown_not_as_zero(self):
        """``None``, not ``0.0``. A player nobody owns is not a missing field.

        Both end up falsy, which is exactly why this needs a test: a judge told
        "owned in 0% of leagues" about a player ESPN simply said nothing about
        has been handed a fact that is not one.
        """
        bare = _entry(2, "No Data", PG)
        got = _reader([bare]).roster()[0]
        self.assertIsNone(got.percent_owned)
        self.assertIsNone(got.draft_adp)
        self.assertIsNone(got.draft_rank)
        self.assertEqual(got.outlook, "")

    def test_a_genuine_zero_survives(self):
        got = _reader([
            self.entry(ownership={"percentOwned": 0.0, "averageDraftPosition": 0.0})
        ]).roster()[0]
        self.assertEqual(got.percent_owned, 0.0)
        self.assertEqual(got.draft_adp, 0.0)


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


class TestMatchupScoreDisplayFields(unittest.TestCase):
    """The identity and games-used fields the status page renders.

    All of it rides on the request the score already makes -- ``mTeam`` carries
    the names and logos, and the opponent's ``cumulativeScore`` sits on the same
    matchup row as ours. That is why the page costs no extra ESPN read, and it
    is worth a test: if a future edit split these into a second request the
    cost would be invisible from the call site.
    """

    def test_names_logos_and_both_sides_games_come_off_the_one_request(self):
        read, http = _score_reader(
            [{"matchupPeriodId": 1,
              "home": _side(7, points=110.0, gp=12.0),
              "away": _side(6, points=92.0, gp=14.0)}],
            teams=[{"id": 7, "name": "Us", "abbrev": "US",
                    "logo": "https://g.espncdn.com/a.svg"},
                   {"id": 6, "name": "Them", "abbrev": "THM",
                    "logo": "https://imgflip.com/b.png"}],
        )
        got = read.matchup_score()
        self.assertEqual(got.my_name, "Us")
        self.assertEqual(got.my_abbrev, "US")
        self.assertEqual(got.my_logo, "https://g.espncdn.com/a.svg")
        self.assertEqual(got.opp_name, "Them")
        self.assertEqual(got.opp_abbrev, "THM")
        self.assertEqual(got.opp_logo, "https://imgflip.com/b.png")
        self.assertEqual(got.my_games_used, 12)
        self.assertEqual(got.opp_games_used, 14)
        # One request, and ``mTeam`` is on it. ``_get`` does no caching, so a
        # second read here would be a second round trip on every run.
        self.assertEqual(len(http.views), 1)
        self.assertIn("mTeam", http.views[0])

    def test_an_unscored_row_reports_no_games_rather_than_zero(self):
        """"Not reported" and "nobody has played" look alike and are not."""
        read, _ = _score_reader([
            {"matchupPeriodId": 1, "home": _side(7, points=0.0),
             "away": _side(6, points=0.0)},
        ])
        got = read.matchup_score()
        self.assertIsNone(got.my_games_used)
        self.assertIsNone(got.opp_games_used)

    def test_a_team_espn_sends_no_name_for_falls_back_to_its_id(self):
        read, _ = _score_reader(
            [{"matchupPeriodId": 1, "home": _side(7), "away": _side(6)}],
            teams=[{"id": 7, "abbrev": "US"}],
        )
        got = read.matchup_score()
        self.assertEqual(got.my_name, "US")
        self.assertEqual(got.opp_name, "6")
        self.assertEqual(got.opp_logo, "")

    def test_a_bye_names_nobody(self):
        read, _ = _score_reader(
            [{"matchupPeriodId": 1, "home": _side(7, points=50.0, gp=5.0)}],
            teams=[{"id": 7, "name": "Us"}],
        )
        got = read.matchup_score()
        self.assertIsNone(got.opp_team_id)
        self.assertEqual(got.opp_name, "")
        self.assertIsNone(got.opp_games_used)
        self.assertEqual(got.my_games_used, 5)


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


# ---------------------------------------------------------------------------
# finding 5: `latestScoringPeriod` lags the period writes are accepted for
# ---------------------------------------------------------------------------


def _autopilot(cal):
    """An ``Autopilot`` with nothing on it but the calendar cache.

    ``__init__`` opens an HTTP client and a SQLite file; ``today_period``
    touches neither.
    """
    bot = object.__new__(main_mod.Autopilot)
    bot._calendar = cal
    return bot


def _calendar(first_date, days=100):
    """A linear season of ``days`` periods starting at period 1."""
    return SeasonCalendar.from_period_dates(
        {p: first_date + timedelta(days=p - 1) for p in range(1, days + 1)}
    )


class TestScoringPeriodLag(unittest.TestCase):
    """ESPN's two services disagree about which period it is.

    Observed live 2026-10-23: the 09:00 run read ``latestScoringPeriod: 3``
    and addressed its lineup to period 3; the write service answered ``409
    TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT`` on both leagues, because by the
    date it was already period 4. The read-side number had rolled over around
    midday Eastern the day before -- the 09:00 run read 2 and the 09:54 run
    read 3, with no NBA game having finished in between.
    """

    def setUp(self):
        # 2026-10-20 is period 1 of the live season, so today is period 4.
        self.today = datetime.now(ids.GAME_DATE_TZ).date()
        self.cal = _calendar(self.today - timedelta(days=3))

    def test_the_date_wins_when_espn_lags(self):
        bot = _autopilot(self.cal)
        period, note = bot.today_period(
            _settings(current_scoring_period=3, espn_latest_scoring_period=3)
        )
        self.assertEqual(period, 4)
        self.assertIn("scoring period 3", note)

    def test_agreement_is_silent(self):
        bot = _autopilot(self.cal)
        period, note = bot.today_period(
            _settings(current_scoring_period=4, espn_latest_scoring_period=4)
        )
        self.assertEqual(period, 4)
        self.assertIsNone(note)

    def test_no_calendar_leaves_espns_number_alone(self):
        """Degrading to the lagging number beats inventing one."""
        period, note = _autopilot(None).today_period(
            _settings(current_scoring_period=3, espn_latest_scoring_period=3)
        )
        self.assertEqual(period, 3)
        self.assertIsNone(note)

    def test_an_untrustworthy_calendar_leaves_it_alone_too(self):
        """A non-linear calendar cannot answer "which period is today"."""
        dates = {p: self.today - timedelta(days=3 - p + 1) for p in (1, 2, 3)}
        dates[4] = self.today + timedelta(days=9)  # breaks the linear model
        bot = _autopilot(SeasonCalendar.from_period_dates(dates))
        self.assertFalse(bot._calendar.trustworthy)
        period, _ = bot.today_period(
            _settings(current_scoring_period=3, espn_latest_scoring_period=3)
        )
        self.assertEqual(period, 3)

    def test_the_offseason_is_not_a_disagreement(self):
        """ESPN keeps reporting a period in July. Today is outside the season
        then, so the date has nothing to say and must not overrule it."""
        bot = _autopilot(_calendar(self.today + timedelta(days=60), days=10))
        period, note = bot.today_period(
            _settings(current_scoring_period=7, espn_latest_scoring_period=7)
        )
        self.assertEqual(period, 7)
        self.assertIsNone(note)

    def test_espns_raw_number_survives_the_correction(self):
        """``main`` overwrites ``current_scoring_period``; the cross-check in
        ``SeasonCalendar.verify`` still needs ESPN's own pair."""
        s = dataclasses.replace(
            _settings(current_scoring_period=3, espn_latest_scoring_period=3),
            current_scoring_period=4,
        )
        self.assertEqual(s.current_scoring_period, 4)
        self.assertEqual(s.espn_latest_scoring_period, 3)


class TestWritesCarryTheCallersPeriod(unittest.TestCase):
    """The corrected period has to reach the packet, or none of this matters."""

    def test_add_drop_sends_what_it_was_given(self):
        w, http = _writer()
        w.add_drop(add=50, drop=3, waiver=False, scoring_period=4)
        self.assertEqual(http.last["scoringPeriodId"], 4)

    def test_add_drop_falls_back_to_settings(self):
        w, http = _writer()
        w.add_drop(add=50, drop=3, waiver=False)
        self.assertEqual(http.last["scoringPeriodId"], 5)  # _settings()

    def test_set_lineup_sends_what_it_was_given(self):
        w, http = _writer()
        w.set_lineup({1: UTIL, 2: BE, 3: BE}, scoring_period=4)
        self.assertEqual(http.last["scoringPeriodId"], 4)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The two reads the status page added, both probed against a live league on
# 2026-10-24. Each test below is a thing the obvious implementation gets wrong.
# ---------------------------------------------------------------------------


def _day_entry(pid, name, slot, *, stats=(), locked=False, pro_team=2,
               on_team=7):
    return {
        "playerId": pid,
        "lineupSlotId": slot,
        "playerPoolEntry": {
            "status": "ONTEAM",
            "onTeamId": on_team,
            "lineupLocked": locked,
            # The field a page must *not* read for a day's score. See below.
            "appliedStatTotal": 29.5,
            "player": {
                "id": pid,
                "fullName": name,
                "proTeamId": pro_team,
                "eligibleSlots": [slot, UTIL, BE],
                "injuryStatus": "ACTIVE",
                "stats": list(stats),
            },
        },
    }


def _stat(period, applied, *, source=0, split=5):
    return {
        "scoringPeriodId": period,
        "statSourceId": source,
        "statSplitTypeId": split,
        "appliedTotal": applied,
    }


class _DayClient:
    """Answers ``mRoster`` for several teams, and records the query it got."""

    def __init__(self, teams):
        self.teams = teams
        self.params = None

    def get(self, url, params=None, headers=None):
        self.params = list(params or [])
        return _Resp({"teams": self.teams})

    def post(self, *a, **k):
        raise AssertionError("read path must not POST")


def _day_reader(teams):
    cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}", season=2027)
    return EspnRead(cfg, _DayClient(teams))


class TestDayRosterWire(unittest.TestCase):
    """``day_rosters`` -- the lineup *as it stood*, and that day's points.

    Probed on 2026-10-24: the same request for period 4 and period 5 returned
    different ``lineupSlotId`` values for the same players, and period 4 came
    back with every player locked. So rewinding is real, and the page can show
    a day already played.
    """

    def test_the_requested_period_is_sent(self):
        r = _day_reader([{"id": 7, "roster": {"entries": []}}])
        r.day_rosters(5)
        self.assertIn(("scoringPeriodId", "5"), r.http.params)

    def test_the_days_points_come_from_the_split_row_not_the_running_total(self):
        """``appliedStatTotal`` is a trap and this is the whole test.

        ESPN populates it even for a period with no games played, carrying the
        *previous* day's number -- 29.5 on the live probe, for a player who had
        not tipped off. A page built on it reports last night's scores as
        today's, every morning, and looks right while doing it.
        """
        teams = [{"id": 7, "roster": {"entries": [
            _day_entry(1, "Played", 0, stats=[_stat(5, 31.5)]),
            _day_entry(2, "Not yet", 1, stats=[_stat(4, 42.0)]),
        ]}}]
        rows = {d.player.name: d.points for d in _day_reader(teams).day_rosters(5)[7]}
        self.assertEqual(rows["Played"], 31.5)
        self.assertIsNone(rows["Not yet"])

    def test_a_zero_is_a_score_and_not_an_absence(self):
        """He played and scored nothing. That is not the same as a dash."""
        teams = [{"id": 7, "roster": {"entries": [
            _day_entry(1, "Blanked", 0, stats=[_stat(5, 0.0)]),
        ]}}]
        rows = _day_reader(teams).day_rosters(5)[7]
        self.assertEqual(rows[0].points, 0.0)
        self.assertIsNotNone(rows[0].points)

    def test_a_projection_row_for_the_same_day_is_not_the_score(self):
        """``statSourceId`` 1 is ESPN's projection and would read as fact."""
        teams = [{"id": 7, "roster": {"entries": [
            _day_entry(1, "Projected only", 0,
                       stats=[_stat(5, 38.0, source=1)]),
        ]}}]
        self.assertIsNone(_day_reader(teams).day_rosters(5)[7][0].points)

    def test_a_season_split_for_the_same_day_is_not_the_score(self):
        """Split 0 is the season total and is 2900 points, not tonight's 31."""
        teams = [{"id": 7, "roster": {"entries": [
            _day_entry(1, "Season total", 0, stats=[_stat(5, 2917.5, split=0)]),
        ]}}]
        self.assertIsNone(_day_reader(teams).day_rosters(5)[7][0].points)

    def test_every_team_comes_back_from_one_request(self):
        """A head-to-head costs one request, which is why the page can afford it."""
        teams = [
            {"id": 7, "roster": {"entries": [_day_entry(1, "Mine", 0)]}},
            {"id": 6, "roster": {"entries": [
                _day_entry(2, "Theirs", 0, on_team=6)]}},
        ]
        got = _day_reader(teams).day_rosters(5)
        self.assertEqual(sorted(got), [6, 7])

    def test_team_ids_filters_without_a_second_request(self):
        teams = [
            {"id": 7, "roster": {"entries": [_day_entry(1, "Mine", 0)]}},
            {"id": 6, "roster": {"entries": [
                _day_entry(2, "Theirs", 0, on_team=6)]}},
        ]
        r = _day_reader(teams)
        self.assertEqual(list(r.day_rosters(5, team_ids=[7])), [7])

    def test_a_departed_player_is_excluded_here_too(self):
        """Same ghost as ``roster``: he lingers in ``entries`` with his slot."""
        teams = [{"id": 7, "roster": {"entries": [
            _day_entry(1, "Still here", 0),
            _day_entry(2, "Dropped", 1, on_team=0),
        ]}}]
        rows = _day_reader(teams).day_rosters(5)[7]
        self.assertEqual([d.player.name for d in rows], ["Still here"])

    def test_the_lock_flag_survives(self):
        """What stops the Team page offering to move a man whose game started."""
        teams = [{"id": 7, "roster": {"entries": [
            _day_entry(1, "Tipped off", 0, locked=True),
        ]}}]
        self.assertTrue(_day_reader(teams).day_rosters(5)[7][0].player.lineup_locked)


class _ScheduleClient:
    def __init__(self, pro_teams):
        self.pro_teams = pro_teams

    def get(self, url, params=None, headers=None):
        return _Resp({"settings": {"proTeams": self.pro_teams}})

    def post(self, *a, **k):
        raise AssertionError("read path must not POST")


class TestProGamesWire(unittest.TestCase):
    """``pro_games_by_period`` -- who, when, and which side.

    The payload shape is from the live probe: ``awayProTeamId``,
    ``homeProTeamId``, ``date`` in epoch milliseconds, ``startTimeTBD``.
    """

    GAME = {"awayProTeamId": 11, "homeProTeamId": 3,
            "date": 1792627200000, "scoringPeriodId": 2}

    def _read(self):
        teams = [
            {"id": 3, "proGamesByScoringPeriod": {"2": [dict(self.GAME)]}},
            {"id": 11, "proGamesByScoringPeriod": {"2": [dict(self.GAME)]}},
        ]
        cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}",
                         season=2027)
        return EspnRead(cfg, _ScheduleClient(teams)).pro_games_by_period()

    def test_both_sides_are_indexed_so_no_pairing_is_needed(self):
        games = self._read()[2]
        self.assertEqual(sorted(games), [3, 11])

    def test_the_side_is_read_from_which_team_the_entry_is_under(self):
        games = self._read()[2]
        self.assertTrue(games[3].home)
        self.assertFalse(games[11].home)

    def test_each_side_names_the_other_as_the_opponent(self):
        games = self._read()[2]
        self.assertEqual(games[3].opponent_id, 11)
        self.assertEqual(games[11].opponent_id, 3)

    def test_the_tipoff_is_a_utc_instant_from_epoch_milliseconds(self):
        when = self._read()[2][3].tipoff_utc
        self.assertEqual(when.tzinfo, timezone.utc)
        self.assertEqual(int(when.timestamp() * 1000), self.GAME["date"])

    def test_a_game_with_no_time_yet_is_dated_none_and_flagged(self):
        teams = [{"id": 3, "proGamesByScoringPeriod": {"2": [
            {"awayProTeamId": 11, "homeProTeamId": 3, "startTimeTBD": True},
        ]}}]
        cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}",
                         season=2027)
        game = EspnRead(cfg, _ScheduleClient(teams)).pro_games_by_period()[2][3]
        self.assertIsNone(game.tipoff_utc)
        self.assertTrue(game.time_tbd)


class _CountingScheduleClient(_ScheduleClient):
    def __init__(self, pro_teams):
        super().__init__(pro_teams)
        self.gets = 0

    def get(self, url, params=None, headers=None):
        self.gets += 1
        return super().get(url, params, headers)


class TestProSchedulePayloadIsFetchedOnce(unittest.TestCase):
    """Three parses of one response, not three responses.

    It is several megabytes and season-static, and three methods want
    different things out of it: a date per period, a game count per team, and
    the games themselves. Fetching it per caller showed up as three identical
    lines in the log the first time the status page was opened.
    """

    def _reader(self):
        teams = [{"id": 3, "abbrev": "X", "proGamesByScoringPeriod": {"2": [
            {"awayProTeamId": 11, "homeProTeamId": 3, "date": 1792627200000},
        ]}}]
        cfg = EspnConfig(league_id=1, team_id=7, espn_s2="s2", swid="{X}",
                         season=2027)
        return EspnRead(cfg, _CountingScheduleClient(teams))

    def test_all_three_parsers_share_one_request(self):
        r = self._reader()
        r.scoring_period_dates()
        r.pro_team_scoring_periods()
        r.pro_games_by_period()
        self.assertEqual(r.http.gets, 1)

    def test_each_parser_still_answers_its_own_question(self):
        r = self._reader()
        self.assertEqual(r.pro_team_scoring_periods()[3], {2: 1})
        self.assertEqual(r.pro_games_by_period()[2][3].opponent_id, 11)
        self.assertIn(2, r.scoring_period_dates())


class TestMatchupOutcomeWire(unittest.TestCase):
    """``winner`` is HOME/AWAY, which means nothing without knowing our side.

    This is the only place that knows it, so this is the only place that can
    normalize it. Probed on 2026-10-26: one live league won week 1 from the
    away side (``winner: AWAY``) and the other lost it from the away side
    (``winner: HOME``), which is exactly the pair that a naive reading gets
    backwards for one of them.
    """

    def _read(self, *, winner, my_side):
        them = "away" if my_side == "home" else "home"
        match = {"matchupPeriodId": 1, "winner": winner,
                 my_side: _side(7, points=1190.0),
                 them: _side(6, points=1221.5)}
        reader, _ = _score_reader([match])
        return reader.matchup_score(matchup_period=1)

    def test_winning_from_the_home_side(self):
        self.assertEqual(self._read(winner="HOME", my_side="home").outcome, "WIN")

    def test_winning_from_the_away_side(self):
        self.assertEqual(self._read(winner="AWAY", my_side="away").outcome, "WIN")

    def test_losing_from_the_away_side(self):
        self.assertEqual(self._read(winner="HOME", my_side="away").outcome, "LOSS")

    def test_losing_from_the_home_side(self):
        self.assertEqual(self._read(winner="AWAY", my_side="home").outcome, "LOSS")

    def test_a_week_still_running_has_no_outcome_and_is_not_final(self):
        score = self._read(winner="UNDECIDED", my_side="home")
        self.assertEqual(score.outcome, "")
        self.assertFalse(score.final)

    def test_a_decided_week_is_final(self):
        self.assertTrue(self._read(winner="HOME", my_side="home").final)

    def test_a_missing_winner_field_is_undecided_rather_than_a_loss(self):
        match = {"matchupPeriodId": 1,
                 "home": _side(7, points=10.0), "away": _side(6, points=5.0)}
        reader, _ = _score_reader([match])
        self.assertEqual(reader.matchup_score(matchup_period=1).outcome, "")


class TestMatchupByPeriodWire(unittest.TestCase):
    """``pointsByScoringPeriod`` -- the week day by day, free with the totals.

    Keyed by ``scoringPeriodId`` as strings, ``null`` for a week that has not
    started, partial for one in progress. A missing key is "not played yet"
    and must not read as zero, the same distinction the per-player scores
    make.
    """

    def _read(self, mine, theirs=None):
        match = {"matchupPeriodId": 1,
                 "home": _side(7, points=1190.0, by_period=mine),
                 "away": _side(6, points=1221.5, by_period=theirs)}
        reader, _ = _score_reader([match])
        return reader.matchup_score(matchup_period=1)

    def test_the_keys_become_integers(self):
        score = self._read({"1": 45.0, "2": 317.5})
        self.assertEqual(score.my_by_period, {1: 45.0, 2: 317.5})

    def test_both_sides_are_read(self):
        score = self._read({"1": 45.0}, {"1": 52.0})
        self.assertEqual(score.opp_by_period, {1: 52.0})

    def test_a_week_that_has_not_started_is_empty_not_missing(self):
        score = self._read(None)
        self.assertEqual(score.my_by_period, {})

    def test_a_null_day_is_dropped_rather_than_read_as_zero(self):
        """Nothing played and nothing scored are different answers."""
        score = self._read({"1": 45.0, "2": None})
        self.assertEqual(score.my_by_period, {1: 45.0})
        self.assertNotIn(2, score.my_by_period)

    def test_a_genuine_zero_is_kept(self):
        self.assertEqual(self._read({"7": 0.0}).my_by_period, {7: 0.0})

    def test_an_unparseable_day_is_skipped_and_the_rest_survive(self):
        score = self._read({"1": 45.0, "oops": "nope"})
        self.assertEqual(score.my_by_period, {1: 45.0})
