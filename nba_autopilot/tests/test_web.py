"""Ingress status page: escaping, relative URLs, and who may approve.

FastAPI is not installed in the development environment, which is why the two
things worth asserting here -- what goes into the HTML and who is allowed to
decide -- live in ``render`` and ``apply_decision`` rather than inside the app
factory. These tests need neither a server nor a socket.

The page is a second path to executing a proposal, and the first one took a
bug that silently refused every approval. So the tests below check the
*negative* cases hardest: a non-owner must be refused before the state machine
is touched at all, and a stale decision must not reach the executor.
"""

from __future__ import annotations

import asyncio
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

import web  # noqa: E402
from state import Proposal, Store  # noqa: E402


def _proposal(action_id="R1:ADD_DROP:0", kind="ADD_DROP", payload=None,
              rationale="", state="PROPOSED", pid=1):
    return Proposal(
        id=pid,
        run_id="R1",
        action_id=action_id,
        kind=kind,
        state=state,
        payload=payload if payload is not None else {},
        rationale=rationale,
        value_delta=0.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
    )


class FakeRef:
    def __init__(self, league_id, label):
        self.league_id = league_id
        self._label = label

    def display(self):
        return self._label


class FakeLeague:
    def __init__(self, league_id, label):
        self.ref = FakeRef(league_id, label)


class FakeCfg:
    def __init__(self, owner="", shadow_mode=False):
        self.ha_owner_user_id = owner
        self.shadow_mode = shadow_mode


class FakeStore:
    """Only the four things the page touches."""

    def __init__(self, pending=(), rows=None, stuck=()):
        self._pending = list(pending)
        self._rows = rows or {}
        self._stuck = list(stuck)
        self.decisions = []
        self.decide_result = _proposal()

    def pending(self, run_id=None):
        return list(self._pending)

    def stuck_writes(self):
        return list(self._stuck)

    def query(self, sql, args=()):
        for fragment, rows in self._rows.items():
            if fragment in sql:
                return rows
        return []

    def decide(self, action_id, approve, user_id):
        self.decisions.append((action_id, approve, user_id))
        return self.decide_result


class FakeBot:
    def __init__(self, store=None, cfg=None, leagues=()):
        self.store = store or FakeStore()
        self.cfg = cfg or FakeCfg()
        self.leagues = list(leagues)
        self.executed = 0

    async def execute_approved(self):
        self.executed += 1


class TestRenderEscaping(unittest.TestCase):
    """Rationale text carries ESPN player names and, soon, LLM output.

    Both are untrusted by the project's own rules, and this page renders inside
    the authenticated Home Assistant frontend -- which is the worst possible
    place to put an injection, because the session it would run in is the one
    that can reconfigure the instance.
    """

    def test_rationale_is_escaped(self):
        nasty = "<script>alert('x')</script>"
        bot = FakeBot(FakeStore(pending=[_proposal(rationale=nasty)]))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_player_name_is_escaped(self):
        bot = FakeBot(FakeStore(pending=[
            _proposal(payload={"add": 1, "add_name": 'O"<b>Neal'}),
        ]))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("<b>Neal", out)

    def test_action_id_cannot_break_out_of_its_attribute(self):
        """The id lands inside value='...', so a quote in it must not escape."""
        bot = FakeBot(FakeStore(pending=[
            _proposal(action_id="R1:ADD:0' autofocus onfocus='alert(1)"),
        ]))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("onfocus='alert(1)", out)
        self.assertIn("&#x27;", out)

    def test_run_notes_are_escaped(self):
        bot = FakeBot(FakeStore(rows={
            "FROM runs": [{
                "run_id": "R1", "league_id": 1, "started_at": "2026-10-21",
                "scoring_period": 5, "shadow_mode": 0,
                "notes": '["<img src=x onerror=alert(1)>"]',
                "shadow_prices": "{}",
            }],
        }))
        out = web.render(web.StatusPage(bot))
        self.assertNotIn("<img src=x", out)


class TestRenderUrls(unittest.TestCase):
    """Ingress serves the app under /api/hassio_ingress/<token>/.

    An absolute URL escapes that prefix and 404s against the frontend, and it
    does so only on the device -- never in a local test that hits the app
    directly. So the assertion is on the emitted markup.
    """

    def test_form_action_is_relative(self):
        bot = FakeBot(FakeStore(pending=[_proposal()]))
        out = web.render(web.StatusPage(bot))
        self.assertIn("action=decide", out)
        self.assertNotIn("action=/decide", out)
        self.assertNotIn('action="/', out)

    def test_no_absolute_paths_anywhere(self):
        bot = FakeBot(FakeStore(pending=[_proposal()]))
        out = web.render(web.StatusPage(bot))
        for attr in ("href=/", "src=/", "action=/"):
            self.assertNotIn(attr, out, "absolute URL would escape the prefix")


class TestStatusPage(unittest.TestCase):
    def test_empty_page_renders(self):
        """The common case on a fresh install: no runs, no proposals."""
        out = web.render(web.StatusPage(FakeBot()))
        self.assertIn("Nothing waiting.", out)
        self.assertIn("No runs recorded yet.", out)

    def test_non_approvable_kinds_are_not_offered_a_button(self):
        """A LINEUP in PROPOSED means auto_approve was set wrongly.

        Rendering Approve for it would claim the move has not happened yet,
        when an autonomous kind is written and executed in the same breath.
        """
        store = FakeStore(pending=[_proposal(kind="LINEUP")])
        page = web.StatusPage(FakeBot(store))
        with self.assertLogs("autopilot.web", level="WARNING"):
            self.assertEqual(page.pending(), [])

    def test_league_label_comes_from_config(self):
        bot = FakeBot(
            FakeStore(
                pending=[_proposal()],
                rows={"league_id FROM runs": [{"league_id": 7}]},
            ),
            leagues=[FakeLeague(7, "Dynassty")],
        )
        self.assertEqual(web.StatusPage(bot).pending()[0]["league"], "Dynassty")

    def test_unknown_league_does_not_raise(self):
        """A run from a league since removed from config still has to render."""
        bot = FakeBot(FakeStore(pending=[_proposal()]), leagues=[])
        self.assertIn("league", web.StatusPage(bot).pending()[0]["league"])

    def test_shadow_banner_tracks_config(self):
        on = web.render(web.StatusPage(FakeBot(cfg=FakeCfg(shadow_mode=True))))
        off = web.render(web.StatusPage(FakeBot(cfg=FakeCfg(shadow_mode=False))))
        self.assertIn("Shadow mode", on)
        self.assertNotIn("Shadow mode", off)

    def test_stuck_writes_are_surfaced(self):
        bot = FakeBot(FakeStore(stuck=[{"id": 3}, {"id": 4}]))
        out = web.render(web.StatusPage(bot))
        self.assertIn("2 write(s) in an unknown state", out)

    def test_malformed_stored_json_does_not_break_the_page(self):
        """`notes` is written by us, but a half-written row must still render."""
        bot = FakeBot(FakeStore(rows={
            "FROM runs": [{
                "run_id": "R1", "league_id": 1, "started_at": "x",
                "scoring_period": 1, "shadow_mode": 1,
                "notes": "{not json", "shadow_prices": None,
            }],
        }))
        out = web.render(web.StatusPage(bot))
        self.assertIn("no moves proposed", out)


class TestApplyDecision(unittest.TestCase):
    def test_non_owner_is_refused_before_the_state_machine(self):
        """The refusal must happen first, not be undone afterwards."""
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        with self.assertLogs("autopilot.web", level="WARNING"):
            refusal = asyncio.run(web.apply_decision(
                bot, action_id="R1:ADD:0", verb="APPROVE",
                user_id="someone-else",
            ))
        self.assertEqual(refusal, web.NOT_OWNER)
        self.assertEqual(bot.store.decisions, [], "decide() must not be called")
        self.assertEqual(bot.executed, 0)

    def test_missing_user_id_fails_closed(self):
        """If the ingress header name is ever wrong, refuse rather than allow."""
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        with self.assertLogs("autopilot.web", level="WARNING"):
            refusal = asyncio.run(web.apply_decision(
                bot, action_id="R1:ADD:0", verb="APPROVE", user_id="",
            ))
        self.assertEqual(refusal, web.NOT_OWNER)
        self.assertEqual(bot.store.decisions, [])

    def test_owner_approval_executes(self):
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        refusal = asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="APPROVE", user_id="owner-abc",
        ))
        self.assertIsNone(refusal)
        self.assertEqual(bot.store.decisions, [("R1:ADD:0", True, "owner-abc")])
        self.assertEqual(bot.executed, 1)

    def test_rejection_does_not_execute(self):
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="REJECT", user_id="owner-abc",
        ))
        self.assertEqual(bot.store.decisions, [("R1:ADD:0", False, "owner-abc")])
        self.assertEqual(bot.executed, 0, "a rejection must write nothing to ESPN")

    def test_unknown_verb_is_not_an_approval(self):
        """Anything that is not APPROVE is a rejection; the button is the only
        source of the verb, so a mangled one must not fall through to approve."""
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="approve-ish", user_id="owner-abc",
        ))
        self.assertEqual(bot.store.decisions[0][1], False)
        self.assertEqual(bot.executed, 0)

    def test_verb_is_case_insensitive(self):
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="approve", user_id="owner-abc",
        ))
        self.assertEqual(bot.executed, 1)

    def test_stale_decision_does_not_reach_the_executor(self):
        """`decide` returning None means the row was not in PROPOSED.

        Calling `execute_approved` anyway would be harmless today, because the
        claim is still guarded -- but it would make this page's correctness
        depend on a guard two layers down instead of on its own logic.
        """
        bot = FakeBot(cfg=FakeCfg(owner="owner-abc"))
        bot.store.decide_result = None
        refusal = asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="APPROVE", user_id="owner-abc",
        ))
        self.assertIsNone(refusal)
        self.assertEqual(bot.executed, 0)

    def test_unset_owner_allows_the_decision(self):
        """`ha_owner_user_id` is optional; ingress has already authenticated."""
        bot = FakeBot(cfg=FakeCfg(owner=""))
        refusal = asyncio.run(web.apply_decision(
            bot, action_id="R1:ADD:0", verb="APPROVE", user_id="whoever",
        ))
        self.assertIsNone(refusal)
        self.assertEqual(bot.executed, 1)


class TestAnnotationsAreResolvable(unittest.TestCase):
    """`from __future__ import annotations` in web.py breaks every page load.

    FastAPI resolves a route handler's annotations with ``get_type_hints``,
    which consults *module* globals. ``Request`` is imported inside
    ``build_app`` so this file stays importable without FastAPI, so under that
    future import the name is a string that resolves nowhere -- and FastAPI's
    fallback for a parameter it cannot type is a required **query** parameter.
    Every GET then returns ``422 {"loc": ["query", "request"]}``.

    This happened. It was found on the device, through the ingress proxy,
    because nothing in this file imports FastAPI and so nothing here exercises
    the route layer. This test is the cheap proxy for the expensive discovery:
    the future import binds the name ``annotations`` in the module namespace,
    which is observable without FastAPI being installed at all.
    """

    def test_module_does_not_use_postponed_annotations(self):
        self.assertIsNone(
            getattr(web, "annotations", None),
            "web.py must not use `from __future__ import annotations`; "
            "see this test's docstring",
        )

    def test_route_annotations_are_not_string_literals(self):
        """The other half of the same mistake: quoting the annotation by hand.

        Removing the future import is not enough if a handler is written
        ``request: "Request"`` -- a string literal stays a string, and
        ``get_type_hints`` fails on it identically.
        """
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "rootfs", "app", "web.py",
        )
        with open(path) as fh:
            source = fh.read()
        self.assertNotIn('request: "Request"', source)
        self.assertIn("request: Request", source)


class TestStoreQuery(unittest.TestCase):
    """`Store.query` exists so the page never touches `store.db` unlocked.

    A run executes on a worker thread, so an unlocked cursor on the one shared
    connection would interleave with its writes.
    """

    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_returns_plain_dicts(self):
        self.store.start_run("R1", 5, True, league_id=7)
        rows = self.store.query("SELECT * FROM runs")
        self.assertEqual(len(rows), 1)
        self.assertIs(type(rows[0]), dict)
        self.assertEqual(rows[0]["league_id"], 7)

    def test_takes_the_lock(self):
        held = []

        class Watch:
            def __enter__(inner):
                held.append(True)
                return inner

            def __exit__(inner, *a):
                held.append(False)
                return False

        self.store._lock = Watch()
        self.store.query("SELECT 1")
        self.assertEqual(held, [True, False], "query() must hold the store lock")

    def test_page_does_not_touch_the_connection_directly(self):
        """Guards the whole point of the helper against a future edit."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "rootfs", "app", "web.py",
        )
        with open(path) as fh:
            self.assertNotIn("store.db", fh.read())


if __name__ == "__main__":
    unittest.main()
