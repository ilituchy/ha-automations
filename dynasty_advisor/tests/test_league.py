from __future__ import annotations

import unittest

import _fixtures  # noqa: F401
from advisor.history import SeasonInfo, parse_trade
from advisor.league import check_picks_against_draft, parse_team, pick_ownership, tradable_seasons
from advisor.values import Values, parse_pick_name


class TestPicks(unittest.TestCase):
    TRADED = [
        {"season": "2026", "round": 1, "roster_id": 2, "owner_id": 1, "previous_owner_id": 2},
        {"season": "2027", "round": 1, "roster_id": 1, "owner_id": 3, "previous_owner_id": 1},
        {"season": "2027", "round": 2, "roster_id": 3, "owner_id": 2, "previous_owner_id": 3},
        # traded twice: the row carries the CURRENT owner
        {"season": "2027", "round": 2, "roster_id": 2, "owner_id": 1, "previous_owner_id": 3},
    ]

    def test_ownership_defaults_then_overrides(self):
        picks = pick_ownership(self.TRADED, [1, 2, 3], ["2027"], 2)
        own = {(p.season, p.round, p.original_roster): p.owner_roster for p in picks}
        self.assertEqual(own[("2027", 1, 1)], 3)
        self.assertEqual(own[("2027", 1, 2)], 2)
        self.assertEqual(own[("2027", 2, 3)], 2)
        self.assertEqual(own[("2027", 2, 2)], 1)
        self.assertEqual(len(picks), 6)

    def test_spent_season_rows_ignored(self):
        picks = pick_ownership(self.TRADED, [1, 2, 3], ["2027"], 2)
        self.assertFalse(any(p.season == "2026" for p in picks))

    def test_tradable_seasons(self):
        self.assertEqual(tradable_seasons("2026", [{"season": "2026", "status": "complete"}]), ("2027", "2028", "2029"))
        self.assertEqual(tradable_seasons("2027", [{"season": "2027", "status": "pre_draft"}]), ("2027", "2028", "2029"))

    def test_self_check_against_draft(self):
        draft = {"season": "2026", "slot_to_roster_id": {"1": 2, "2": 1, "3": 3}}
        picks = [{"round": 1, "draft_slot": 1, "roster_id": 1},  # roster 2's 1st, traded to 1
                 {"round": 1, "draft_slot": 2, "roster_id": 1},
                 {"round": 1, "draft_slot": 3, "roster_id": 3}]
        self.assertEqual(check_picks_against_draft(self.TRADED, draft, picks), [])
        picks[0]["roster_id"] = 2
        self.assertEqual(len(check_picks_against_draft(self.TRADED, draft, picks)), 1)


class TestRoster(unittest.TestCase):
    def test_bench_and_empty_slot(self):
        r = {"roster_id": 4, "owner_id": "u", "players": ["a", "b", "c", "d", "e"],
             "starters": ["a", "0"], "reserve": ["d"], "taxi": ["e"],
             "settings": {"wins": 3, "losses": 6, "fpts": 900, "fpts_decimal": 50}}
        t = parse_team(r, {"u": {"display_name": "x"}})
        self.assertEqual(t.bench, ("b", "c"))
        self.assertEqual(t.active, ("a", "b", "c"))
        self.assertAlmostEqual(t.fpts, 900.5)
        self.assertEqual(t.record, "3-6")


class TestHistory(unittest.TestCase):
    def test_creator_is_user_consenters_are_rosters(self):
        info = SeasonInfo("L", "2025", True, {1: "alice", 2: "bob"}, {"alice": "A", "bob": "B"})
        t = {"transaction_id": "t1", "type": "trade", "status": "complete", "status_updated": 5,
             "creator": "bob", "consenter_ids": [1, 2], "roster_ids": [1, 2],
             "adds": {"p1": 2, "p2": 1},
             "draft_picks": [{"season": "2026", "round": 1, "roster_id": 1, "owner_id": 2, "previous_owner_id": 1}],
             "waiver_budget": [{"sender": 2, "receiver": 1, "amount": 10}]}
        tr = parse_trade(t, info, 3)
        self.assertEqual(tr.creator, "bob")
        self.assertEqual(tr.accepters, ("alice",))
        self.assertEqual(tr.side("bob").players_in, ("p1",))
        self.assertEqual(tr.side("bob").picks_in[0].original_owner, "alice")
        self.assertEqual(tr.side("alice").faab_in, 10)
        self.assertEqual(tr.given_by("alice")[0], ("p1",))


class TestValues(unittest.TestCase):
    def test_pick_names(self):
        self.assertEqual(parse_pick_name("2027 1st (Early)"), ("2027", 1, "Early"))
        self.assertEqual(parse_pick_name("2028 4th"), ("2028", 4, None))
        self.assertIsNone(parse_pick_name("Josh Allen"))

    def test_pick_fallbacks(self):
        v = Values({}, {("2027", 1, "Early"): 5000.0, ("2027", 1, None): 3000.0, ("2028", 1, None): 2000.0})
        self.assertEqual(v.pick("2027", 1, "Early"), 5000.0)
        self.assertEqual(v.pick("2027", 1, "Late"), 3000.0)
        self.assertEqual(v.pick("2028", 1, "Mid"), 2000.0)
        self.assertAlmostEqual(v.pick("2030", 1), 1800.0)
        self.assertEqual(v.pick("2027", 3), 0.0)


if __name__ == "__main__":
    unittest.main()
