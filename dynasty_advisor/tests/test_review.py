"""AI review: prompt contents, parsing, failure handling, page rendering."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import _fixtures  # noqa: F401
from advisor import page, review

INDEX = {
    "me": 1,
    "teams": {"1": "me", "2": "rival"},
    "players": {
        "10": {"name": "Young Back", "pos": "RB", "age": 23.4, "team": "AAA", "dyn": 4000, "now": 3000,
               "owner": 2, "espn": "77", "status": "Questionable", "body": "Ankle"},
        "11": {"name": "Old Wideout", "pos": "WR", "age": 31.0, "team": "BBB", "dyn": 1500, "now": 2500, "owner": 1},
        "12": {"name": "Star Back", "pos": "RB", "age": 24.0, "team": "CCC", "dyn": 9000, "now": 9000, "owner": 1},
        "13": {"name": "Waiver Guy", "pos": "WR", "age": 25.0, "team": "DDD", "dyn": 300, "now": 400},
    },
}
LEAGUE = {"slots": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX"], "superflex": True, "scoring": "1 PPR"}


def asset(pid, value, pos="RB"):
    return {"kind": "player", "id": pid, "name": INDEX["players"][pid]["name"], "pos": pos, "value": value}


def view():
    pk = {"give": [asset("11", 1500, "WR"), {"kind": "pick", "name": "2028 1st", "pos": "PICK", "value": 2000}],
          "get": [asset("10", 4000)], "market": 500, "lineup": 300, "roster_change": 0}
    return {
        "season": "2026", "week": 10, "strategy": "balanced", "deadline": {"week": 12, "state": "week 12"},
        "me": {"name": "me", "record": "5-4", "finish": 5, "teams": 2, "mode": "bubble"},
        "protected": [dict(asset("12", 9000), untouchable=True)],
        "picks": [{"name": "2028 1st", "value": 2000, "season": "2028", "round": 1}],
        "teams": [{"rank": 1, "name": "rival", "record": "7-2", "mode": "contender", "me": False, "pf": 900,
                   "lineup": 5000, "lineup_share": 1.0, "thin": None}],
        "managers": [], "replacement": 100, "attribution": "",
        "trades": [
            {"partner": "rival", "mode": "contender", "context": "7-2", "chance": "likely", "opening": pk,
             "notes": ["They're thin at WR"], "accepted": 3, "proposed": 1, "since": "2024"},
            {"partner": "rival", "mode": "contender", "context": "7-2", "chance": "plausible", "opening": pk, "notes": []},
        ],
        "moves": {"actions": [{"add": dict(asset("13", 300, "WR"), starts=True), "drop": None}],
                  "active": 20, "active_max": 21, "reserve_open": 0, "open_spots": 1,
                  "ir": [], "free_agents": [], "trending_unvalued": [], "cheapest": []},
    }


class FakeMessages:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.kwargs = resp, exc, None

    def create(self, **kw):
        self.kwargs = kw
        if self.exc:
            raise self.exc
        return self.resp


def client_returning(data, stop="end_turn"):
    text = data if isinstance(data, str) else json.dumps(data)
    resp = SimpleNamespace(stop_reason=stop, model="m", usage=None,
                           content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])
    msgs = FakeMessages(resp)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


GOOD = {"overview": "Bubble team, thin at RB.",
        "trades": [{"n": 1, "verdict": "go", "take": "Young Back fills RB2.", "watch": "Ask for their 2nd too."},
                   {"n": 2, "verdict": "pass", "take": "Same deal, worse.", "watch": ""},
                   {"n": 9, "verdict": "go", "take": "out of range", "watch": ""}],
        "moves": [{"n": 1, "verdict": "consider", "take": "Only a bye-week fill."}]}


class FakeHttp:
    def __init__(self, body):
        self.body = body

    def get_json(self, url, ttl):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class TestReview(unittest.TestCase):
    def test_prompt_carries_the_case(self):
        news = {"10": ["2026-11-10: Young Back named starter"]}
        p = review.build_prompt(view(), INDEX, LEAGUE, news)
        for s in ("superflex", "SUPER_FLEX", "Old Wideout", "age 31", "Young Back", "injury: Questionable (Ankle)",
                  "PICK 2028 1st", "market value +500", "Idea 1", "Idea 2", "Pickup 1", "would start",
                  "Untouchable (the owner's choice; never offered): Star Back", "named starter", "rival's roster", "accepted 3"):
            self.assertIn(s, p)

    def test_review_annotates_and_gates(self):
        client, msgs = client_returning(GOOD)
        v = review.review_view(view(), INDEX, LEAGUE, api_key="k", client=client)
        self.assertEqual(v["review"]["overview"], "Bubble team, thin at RB.")
        self.assertEqual(v["trades"][0]["review"]["verdict"], "go")
        self.assertEqual(v["moves"]["actions"][0]["review"]["verdict"], "consider")
        self.assertEqual(review.passed(v), {2})
        self.assertNotIn("error", v["review"])
        kw = msgs.kwargs
        self.assertEqual(kw["model"], review.DEFAULT_MODEL)
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("tool_choice", kw)
        html = page.render_view(v)
        self.assertIn("The reviewer passed on 1 idea", html)
        self.assertIn("Young Back fills RB2.", html)
        self.assertIn("Ask for their 2nd too.", html)
        self.assertIn("Only a bye-week fill.", html)
        self.assertIn("1 idea, best first", html)

    def test_failures_leave_ideas_unreviewed(self):
        bad = [
            client_returning("not json")[0],
            client_returning(GOOD, stop="refusal")[0],
            client_returning(GOOD, stop="max_tokens")[0],
            SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(exc=RuntimeError("401 invalid x-api-key")))),
        ]
        for c in bad:
            v = review.review_view(view(), INDEX, LEAGUE, api_key="k", client=c)
            self.assertIn("error", v["review"])
            self.assertFalse(any("review" in t for t in v["trades"]))
            self.assertEqual(review.passed(v), set())
            html = page.render_view(v)
            self.assertIn("AI review unavailable", html)
            self.assertIn("2 ideas, best first", html)

    def test_reviewer_text_is_escaped(self):
        evil = {"overview": "<script>x</script>", "trades": [{"n": 1, "verdict": "go", "take": "<img src=x>", "watch": ""}],
                "moves": []}
        v = review.review_view(view(), INDEX, LEAGUE, api_key="k", client=client_returning(evil)[0])
        html = page.render_view(v)
        self.assertNotIn("<script>x", html)
        self.assertNotIn("<img src=x>", html)

    def test_news_is_recent_only_and_optional(self):
        now = datetime(2026, 11, 12, tzinfo=timezone.utc)
        feed = {"news": {"feed": [
            {"headline": "Named starter", "story": "He starts.", "published": "2026-11-10T12:00:00Z"},
            {"headline": "Old news", "story": "", "published": "2026-09-01T12:00:00Z"}]}}
        got = review.fetch_news(FakeHttp(feed), INDEX, ["10", "11"], now=now)
        self.assertEqual(list(got), ["10"])
        self.assertEqual(len(got["10"]), 1)
        self.assertIn("Named starter -- He starts.", got["10"][0])
        self.assertEqual(review.fetch_news(FakeHttp(RuntimeError("down")), INDEX, ["10"], now=now), {})

    def test_no_ideas_no_call(self):
        v = view()
        v["trades"], v["moves"]["actions"] = [], []
        client, msgs = client_returning(GOOD)
        review.review_view(v, INDEX, LEAGUE, api_key="k", client=client)
        self.assertIsNone(msgs.kwargs)


if __name__ == "__main__":
    unittest.main()
