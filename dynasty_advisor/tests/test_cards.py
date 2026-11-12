"""Player cards: scoring, schedule/bye logic, id validation and escaping."""

from __future__ import annotations

import unittest

import _fixtures  # noqa: F401
from advisor import cards


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def get_json(self, url, ttl):
        self.urls.append(url)
        for frag, body in self.routes.items():
            if frag in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise RuntimeError("unrouted " + url)


def week(n, opp, away, **stats):
    s = dict(gp=1, off_snp=50, tm_off_snp=60, **stats)
    return {"week": n, "opponent": opp, "is_away_team": away, "stats": s}


INDEX = {
    "version": 1, "season": "2026", "week": 3, "me": 1,
    "teams": {"1": "me", "2": "<b>rival</b>"},
    "scoring": {"rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0},
    "players": {
        "100": {"name": "<script>x</script>", "pos": "WR", "team": "AAA", "owner": 2, "espn": "9",
                "dyn": 5000, "now": 4000, "rank": 20, "prank": 8, "status": "Out", "body": "<i>knee</i>"},
    },
}

SCHEDULE = [
    {"week": 1, "home": "AAA", "away": "BBB", "status": "complete", "date": "2026-09-13"},
    {"week": 2, "home": "CCC", "away": "AAA", "status": "complete", "date": "2026-09-20"},
    # week 3: AAA has no game -> bye
    {"week": 4, "home": "AAA", "away": "DDD", "status": "pre_game", "date": "2026-10-04"},
    {"week": 5, "home": "EEE", "away": "AAA", "status": "pre_game", "date": "2026-10-11"},
]


def routes(**over):
    r = {
        "grouping=week&x": None,
        "projections/nfl": {"4": {"stats": {"rec": 5, "rec_yd": 60}}, "5": None},
        "schedule/nfl": SCHEDULE,
        "season=2026&grouping=week": {"1": week(1, "BBB", False, rec=6, rec_yd=80, rec_td=1),
                                      "2": week(2, "CCC", True, rec=2, rec_yd=15), "3": None},
        "season=2025": {"stats": {"gp": 16, "rec": 80, "rec_yd": 1000, "rec_td": 8}},
        "season=2024": {"stats": {}},
        "news/players": {"news": {"feed": [{"headline": "<img src=x onerror=y>", "story": "s",
                                            "published": "2026-09-21T10:00:00Z"}]}},
    }
    r.update(over)
    return {k: v for k, v in r.items() if v is not None}


class TestCards(unittest.TestCase):
    def test_points_use_league_scoring(self):
        self.assertAlmostEqual(cards.points({"rec": 6, "rec_yd": 80, "rec_td": 1, "pass_yd": 300},
                                            INDEX["scoring"]), 6 + 8 + 6)

    def test_card(self):
        v = cards.build_card(INDEX, "100", FakeHttp(routes()))
        self.assertEqual([r["week"] for r in v["log"]], [2, 1])  # newest first
        self.assertEqual(v["log"][1]["opp"], "BBB")
        self.assertEqual(v["log"][0]["opp"], "@CCC")
        self.assertAlmostEqual(v["log"][1]["fpts"], 20.0)
        labels = [s["label"] for s in v["splits"]]
        self.assertEqual(labels, ["2026", "2025"])  # 2024 had no games; no "Last 3" with 2 games
        self.assertAlmostEqual(v["splits"][1]["fpg"], (80 + 100 + 48) / 16)
        # Week 3 is a bye from the schedule; week 5's null projection is NOT a bye.
        up = v["upcoming"]
        self.assertEqual([u["week"] for u in up], [3, 4, 5])
        self.assertTrue(up[0].get("bye"))
        self.assertEqual(up[1]["opp"], "DDD")
        self.assertAlmostEqual(up[1]["fpts"], 11.0)
        self.assertEqual(up[2]["opp"], "@EEE")
        self.assertIsNone(up[2]["fpts"])
        self.assertFalse(up[2].get("bye"))

    def test_stats_outage_still_renders_bio(self):
        v = cards.build_card(INDEX, "100", FakeHttp(routes(**{"season=2026&grouping=week": RuntimeError("down")})))
        self.assertTrue(v["warnings"])
        self.assertIn("Dynasty value", cards.card_html(v))

    def test_everything_is_escaped(self):
        out = cards.card_html(cards.build_card(INDEX, "100", FakeHttp(routes())))
        for bad in ("<script>", "<img", "<b>rival", "<i>knee"):
            self.assertNotIn(bad, out)
        self.assertIn("&lt;script&gt;", out)

    def test_ids_are_validated(self):
        http = FakeHttp({})
        for bad in ("../x", "1 OR 1", "", "a" * 20, "100&x=1"):
            self.assertIn("error", cards.build_card(INDEX, bad, http))
        self.assertEqual(http.urls, [])
        self.assertIn("error", cards.build_card(None, "100", http))
        self.assertIn("error", cards.build_card(INDEX, "999", http))


if __name__ == "__main__":
    unittest.main()
