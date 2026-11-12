"""Team modes, manager softness and trade search on a synthetic league."""

from __future__ import annotations

import unittest

from _fixtures import index_for, make_book, make_league, make_values, player
from advisor.assets import package_value
from advisor.history import SeasonInfo, Trade, TradeSide
from advisor.profiles import build_manager_profiles
from advisor.teams import build_team_profiles
from advisor.trades import TradeSearch

PLAYERS = [
    # roster 1 (us): strong WRs, weak RB
    player("q1", "QB", 27, 6000, 6000), player("q1b", "QB", 25, 2500, 2500),
    player("w1", "WR", 25, 7000, 6500), player("w2", "WR", 24, 6000, 5000), player("w3", "WR", 23, 4000, 2000),
    player("r1", "RB", 29, 1600, 1500), player("r1b", "RB", 30, 1300, 1200), player("t1", "TE", 26, 3000, 3000),
    # roster 2 (losing seller): veteran RBs it should move
    player("q2", "QB", 33, 2500, 4000), player("q2b", "QB", 34, 1300, 1500),
    player("r2", "RB", 28, 4000, 6000), player("r2b", "RB", 27, 3500, 5000),
    player("w4", "WR", 30, 1500, 2000), player("w5", "WR", 31, 1400, 1500), player("t2", "TE", 31, 1400, 1500),
    # roster 3 (contender)
    player("q3", "QB", 28, 7000, 7000), player("q3b", "QB", 29, 3000, 3500),
    player("r3", "RB", 26, 6000, 6000), player("r3b", "RB", 27, 4500, 4500),
    player("w6", "WR", 26, 6500, 6500), player("w7", "WR", 27, 5000, 5000), player("t3", "TE", 27, 4000, 4000),
    # free agents (set replacement level)
    player("fa1", "WR", 26, 600, 300), player("fa2", "RB", 25, 500, 200), player("fa3", "TE", 28, 400, 100),
]
ROSTERS = {
    1: ["q1", "q1b", "w1", "w2", "w3", "r1", "r1b", "t1"],
    2: ["q2", "q2b", "r2", "r2b", "w4", "w5", "t2"],
    3: ["q3", "q3b", "r3", "r3b", "w6", "w7", "t3"],
}
RECORDS = {1: (6, 3, 1300), 2: (1, 8, 1000), 3: (8, 1, 1500)}


def build():
    league = make_league(ROSTERS, RECORDS)
    values = make_values(PLAYERS)
    index = index_for(PLAYERS)
    pos = lambda p: tuple(index.get(p, {}).get("fantasy_positions") or ())
    ages = {p.sleeper_id: p.age for p in PLAYERS}
    teams = build_team_profiles(league, values, pos, ages)
    book = make_book(league, values, index, {rid: t.finish_rank for rid, t in teams.items()})
    return league, values, index, teams, book


class TestTeams(unittest.TestCase):
    def test_modes(self):
        _l, _v, _i, teams, _b = build()
        self.assertEqual(teams[2].mode, "seller")
        self.assertEqual(teams[3].mode, "contender")
        self.assertEqual(teams[3].finish_rank, 1)

    def test_bubble_is_not_seller(self):
        rec = {1: (5, 4, 1300), 2: (4, 5, 1290), 3: (6, 3, 1400)}
        league = make_league(ROSTERS, rec)
        values = make_values(PLAYERS)
        index = index_for(PLAYERS)
        teams = build_team_profiles(league, values, lambda p: (index[p]["position"],), {})
        # playoff_teams=2: line is 5-4, team 2 is one game out with 5 to play.
        self.assertEqual(teams[2].mode, "bubble")


class TestManagers(unittest.TestCase):
    def test_softness_from_accepts(self):
        _l, _v, _i, _t, book = build()
        seasons = [SeasonInfo("a", "2025", True, {1: "u1", 2: "u2", 3: "u3"}, {}),
                   SeasonInfo("b", "2026", False, {1: "u1", 2: "u2", 3: "u3"}, {})]

        def tr(i, creator, other):
            return Trade(str(i), "2026", 1, i, creator,
                         (TradeSide(creator, ("fa1",), (), 0), TradeSide(other, ("fa2",), (), 0)))
        trades = [tr(1, "u1", "u2"), tr(2, "u1", "u2"), tr(3, "u3", "u2"), tr(4, "u3", "u1")]
        m = build_manager_profiles(trades, seasons, book, {"u1": "one", "u2": "two", "u3": "three"})
        self.assertEqual((m["u2"].accepted, m["u2"].proposed), (3, 0))
        self.assertEqual(m["u2"].softness, 1.0)
        self.assertTrue(m["u3"].never_accepts)
        self.assertEqual(m["u3"].softness, 0.0)
        self.assertGreater(m["u2"].edge, m["u1"].edge)


class TestTrades(unittest.TestCase):
    def test_seller_marks_down_its_veterans(self):
        league, _v, _i, teams, book = build()
        s = TradeSearch(league, book, teams, {}, 1, "contend")
        vet = book.player("r2")  # 28-year-old RB on the seller
        self.assertLess(s._their_adj(teams[2], vet), vet.vor)
        young = book.player("w3")
        self.assertGreater(s._their_adj(teams[2], young), young.vor)

    def test_contender_counts_its_lineup(self):
        league, _v, _i, teams, book = build()
        s = TradeSearch(league, book, teams, {}, 1, "contend")
        # Taking the contender's starting RB for a bench WR of equal market
        # value hurts their lineup; they must see that as a loss.
        pk = s.evaluate(3, (book.player("w3"),), (book.player("r3b"),))
        self.assertLess(pk.their_now, 0)
        self.assertLess(pk.theirs, s.their_weights(teams[3])[0] * (book.player("w3").vor - book.player("r3b").vor) + 1e-9)

    def test_search_targets_seller_veterans_for_a_contender(self):
        league, _v, _i, teams, book = build()
        ideas = TradeSearch(league, book, teams, {}, 1, "contend").search(5)
        self.assertTrue(ideas)
        top = ideas[0]
        self.assertGreater(top.opening.mine, 0)
        got = {a.key for a in top.opening.get}
        self.assertTrue(got & {"r2", "r2b", "q2"}, "expected the seller's veterans, got %s" % got)
        self.assertEqual(top.partner.team.roster_id, 2)

    def test_package_depth_discount(self):
        self.assertEqual(package_value([1000.0]), 1000.0)
        self.assertLess(package_value([500.0, 500.0]), 1000.0)
        self.assertEqual(package_value([]), 0.0)


class TestAge(unittest.TestCase):
    def test_age_curve(self):
        from advisor.assets import Asset, age_curve
        def a(pos, age):
            return Asset("x", "player", "x", pos, age, 1000, 1000, 1000)
        self.assertAlmostEqual(age_curve(a("WR", 22)), 0.20)
        self.assertAlmostEqual(age_curve(a("TE", 37)), -0.70)  # Kelce: capped decline
        self.assertEqual(age_curve(a("WR", 27)), 0.0)
        self.assertLess(age_curve(a("RB", 28)), age_curve(a("QB", 33)))  # RBs age fastest

    def test_contender_will_not_buy_old_for_young(self):
        """Owner: "i wouldnt want to trade for travis kelce in dynasty"."""
        league, _v, _i, teams, book = build()
        s = TradeSearch(league, book, teams, {}, 1, "contend")
        # A young WR for a seller's 33-year-old QB that starts for us now.
        pk = s.evaluate(2, (book.player("w3"),), (book.player("q2"),))
        self.assertFalse(s.acceptable_for_me(pk))
        for idea in s.search(10):
            self.assertGreaterEqual(idea.opening.my_dyn, -0.05 * idea.opening.size)

    def test_core_players_only_for_no_loss_no_veterans(self):
        from advisor.trades import Package
        from advisor.assets import Asset
        core = Asset("c", "player", "c", "WR", 22, 3000, 1000, 2000, trend=50)
        vet = Asset("v", "player", "v", "TE", 33, 3000, 3000, 2000)
        young = Asset("y", "player", "y", "RB", 23, 3000, 3000, 2000)
        self.assertTrue(core.is_core)
        mk = lambda get, dyn: Package((core,), (get,), 100, 100, dyn, 0, 0, 0, 2000)
        self.assertFalse(TradeSearch.acceptable_for_me(mk(vet, 10)))
        self.assertFalse(TradeSearch.acceptable_for_me(mk(young, -10)))
        self.assertTrue(TradeSearch.acceptable_for_me(mk(young, 10)))

    def test_untouchable_never_offered(self):
        from advisor.run import resolve_players
        league, _v, _i, teams, book = build()
        keep, unknown = resolve_players(["Pw1", "nobody", "w2"], league.teams[1].players, book)
        self.assertEqual(keep, {"w1", "w2"})
        self.assertEqual(unknown, ["nobody"])
        s = TradeSearch(league, book, teams, {}, 1, "contend", untouchable=keep)
        self.assertFalse({"w1", "w2"} & {a.key for a in s.my_assets()})
        for idea in s.search(10):
            self.assertFalse({"w1", "w2"} & {a.key for a in idea.opening.give + idea.ceiling.give})


class TestAddDrop(unittest.TestCase):
    def test_starters_are_not_cheapest_to_lose(self):
        from advisor.adddrop import recommend
        league, _v, index, teams, book = build()
        for strategy in ("contend", "balanced", "rebuild"):
            rep = recommend(league, book, 1, [], strategy)
            cheapest = rep.drops[0].asset.key
            # q1 (6000, starting QB) and w1 (7000, starting WR) must never lead the list.
            self.assertNotIn(cheapest, {"q1", "w1", "w2"}, strategy)
            costs = [c.score for c in rep.drops]
            self.assertEqual(costs, sorted(costs))

    def test_season_ending_free_agents_are_not_suggested(self):
        from dataclasses import replace
        from advisor.adddrop import out_for_season, recommend
        self.assertTrue(out_for_season({"injury_status": "IR"}, 0))
        self.assertTrue(out_for_season({"injury_status": "Out", "injury_notes": "Out for the season (ACL)"}, 50))
        self.assertFalse(out_for_season({"injury_status": "IR"}, 300))   # short stint, still priced this year
        self.assertFalse(out_for_season({"injury_status": "Out"}, 0))    # Out is week to week
        self.assertFalse(out_for_season({}, 0))
        league, values, index, teams, book = build()
        top = recommend(league, book, 1, [], "balanced").adds[0].asset.key
        book.index[top] = dict(book.index[top], injury_status="IR")
        values.players[top] = replace(values.players[top], redraft=0.0)
        rep = recommend(league, book, 1, [], "balanced")
        self.assertNotIn(top, [c.asset.key for c in rep.adds])
        self.assertNotIn(top, [a.asset.key for a, _d, _g in rep.swaps])
        self.assertEqual(rep.skipped_injured, 1)


if __name__ == "__main__":
    unittest.main()
