"""The review that sees a swap before the user does.

Three separable things are tested here, and they fail for different reasons:

* ``build_prompt`` is a pure function and the part most likely to be quietly
  wrong -- a reviewer that is never told the league keeps players, or never
  shown that the man going out is owned in 88% of leagues, will produce
  confident answers to the wrong question. These tests read the prompt.
* ``parse_verdict`` is the gate's lock. The thing that must not happen is a
  response the model never put a verdict in being read as assent, so every
  near-miss shape is asserted to raise.
* ``_review_addrop`` is the wiring, and what matters there is that a block
  returns no plan *and* says why -- *"it should be able to show why it rejected
  a request to add drop"* -- and that an approval never overwrites the computed
  rationale.

No test here touches the network. The judge takes an injectable client for
exactly that reason.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

import judge as judge_mod  # noqa: E402
from models import MatchupScore  # noqa: E402
from solve.addrop import AddDropPlan, Candidate  # noqa: E402

from tests.test_addrop_wiring import (  # noqa: E402
    _Harness,
    _League,
    _Read,
    funnel,
    funnel_target,
    open_funnel,
    open_funnel_target,
    player,
)


# ---------------------------------------------------------------------------
# fixtures


def cand(pid=90, name=None, **kw) -> Candidate:
    base = dict(
        player_id=pid,
        name=name or "P%d" % pid,
        per_game=24.0,
        surplus=12.0,
        games=3,
        p_play=1.0,
    )
    base.update(kw)
    return Candidate(**base)


def plan(**kw) -> AddDropPlan:
    base = dict(
        add=cand(90, "Better", percent_owned=63.6, draft_adp=121.8, draft_rank=121),
        drop=cand(
            2,
            "Weak",
            per_game=20.0,
            surplus=0.0,
            percent_owned=88.4,
            draft_adp=81.0,
            draft_rank=81,
        ),
        gain=12.0,
        reason="quality",
        rationale="Better projects 24.0/g against Weak's 20.0/g.",
        acquisitions_left=4,
    )
    base.update(kw)
    return AddDropPlan(**base)


class _Note:
    """``InjuryNote``'s read surface, which is all ``_report_for`` touches.

    A stub rather than the real dataclass because the real one is built by the
    parser from a live payload shape, and what is being tested here is that the
    reporter's own words reach the prompt -- not that the parser produced them.
    """

    def __init__(self, **kw):
        self.status_raw = kw.get("status_raw", "Out")
        self.status = kw.get("status", "OUT")
        self.reported_at = kw.get(
            "reported_at", datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)
        )
        self.return_date = kw.get("return_date", date(2026, 10, 24))
        self.out_for_season = kw.get("out_for_season", False)
        self.short_comment = kw.get("short_comment", "")
        self.long_comment = kw.get(
            "long_comment", "Dybantsa is expected to return this weekend."
        )


class _Resp:
    """``httpx.Response``'s three attributes that ``_post`` reads."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Records the one POST and returns a canned response."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self.responses:
            raise AssertionError("one POST more than the test expected")
        got = self.responses.pop(0)
        if isinstance(got, Exception):
            raise got
        return got


def verdict_payload(approve=True, reason="fine", concern=None):
    inp = {"approve": approve, "reason": reason}
    if concern is not None:
        inp["concern"] = concern
    return {
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "name": "record_verdict", "id": "t1", "input": inp}
        ],
    }


# ---------------------------------------------------------------------------
# the prompt


class TestPromptCarriesTheCase(unittest.TestCase):
    """What the reviewer is shown, since it can only judge what it is given."""

    def test_both_players_arrive_with_their_numbers(self):
        p = judge_mod.build_prompt(
            league_name="Dynassty", keeper=True, plan=plan()
        )
        self.assertIn("COMING IN: Better", p)
        self.assertIn("GOING OUT: Weak", p)
        self.assertIn("24.0 points per game", p)
        self.assertIn("20.0 points per game", p)
        # The gain, not just the conclusion: a 12-point case and a 70-point one
        # deserve different amounts of benefit of the doubt.
        self.assertIn("+12.0 points gained", p)
        self.assertIn("Better projects 24.0/g", p)

    def test_the_market_readings_reach_both_sides(self):
        """The ownership share is the signal the whole gate turns on.

        It is the only number in the prompt that is thousands of other owners'
        answer to the question being asked, and the one the points model is
        structurally unable to use.
        """
        p = judge_mod.build_prompt(
            league_name="Dynassty", keeper=True, plan=plan()
        )
        self.assertIn("owned in 88% of ESPN leagues", p)
        self.assertIn("owned in 64% of ESPN leagues", p)
        self.assertIn("ESPN's preseason fantasy rank 81", p)

    def test_keeper_and_redraft_are_told_apart(self):
        kept = judge_mod.build_prompt(
            league_name="Dynassty", keeper=True, plan=plan()
        )
        redraft = judge_mod.build_prompt(
            league_name="The Chairman's", keeper=False, plan=plan()
        )
        self.assertIn("value beyond this week", kept)
        self.assertNotIn("value beyond this week", redraft)
        self.assertIn("only this season matters", redraft)
        self.assertNotIn("only this season matters", kept)

    def test_an_unpaired_add_says_nothing_is_given_up(self):
        """A free roster spot is a different question and must not read as one.

        With no ``drop`` the earlier version of this prompt simply omitted the
        GOING OUT block, which leaves a reviewer to infer from an absence.
        """
        p = judge_mod.build_prompt(
            league_name="Dynassty", keeper=True, plan=plan(drop=None)
        )
        self.assertIn("GOING OUT: nobody", p)
        self.assertIn("free roster spot", p)

    def test_the_beat_report_is_quoted_not_summarized(self):
        p = judge_mod.build_prompt(
            league_name="Dynassty",
            keeper=True,
            plan=plan(),
            reports={2: _Note()},
        )
        self.assertIn("Dybantsa is expected to return this weekend.", p)
        self.assertIn("listed Out", p)
        self.assertIn("reported 2026-10-19", p)
        self.assertIn("expected back 2026-10-24", p)

    def test_out_for_the_season_is_said_loudly(self):
        p = judge_mod.build_prompt(
            league_name="Dynassty",
            keeper=True,
            plan=plan(),
            reports={2: _Note(out_for_season=True)},
        )
        self.assertIn("OUT FOR THE SEASON", p)

    def test_the_matchup_and_the_budget_appear_when_known(self):
        p = judge_mod.build_prompt(
            league_name="Dynassty",
            keeper=True,
            plan=plan(),
            matchup=MatchupScore(
                matchup_period=1,
                my_team_id=7,
                my_points=110.0,
                opp_team_id=3,
                opp_name="Them",
                opp_points=92.0,
            ),
            acquisitions_left=7,
        )
        self.assertIn("110-92 vs Them", p)
        self.assertIn("Acquisitions left this week: 7", p)

    def test_what_is_unknown_is_left_out_rather_than_guessed(self):
        p = judge_mod.build_prompt(
            league_name="Dynassty", keeper=True, plan=plan()
        )
        self.assertNotIn("Current matchup", p)
        self.assertNotIn("Acquisitions left", p)

    def test_the_ir_slot_is_mentioned(self):
        """Being parked on IR is the shape that produced this whole feature."""
        p = judge_mod.build_prompt(
            league_name="Dynassty",
            keeper=True,
            plan=plan(drop=cand(2, "Weak", surplus=0.0, on_ir=True)),
        )
        self.assertIn("injured-reserve slot", p)

    def test_a_long_outlook_is_truncated_not_dropped(self):
        long = "word " * 400
        p = judge_mod.build_prompt(
            league_name="Dynassty",
            keeper=True,
            plan=plan(add=cand(90, "Better", outlook=long)),
        )
        self.assertIn("ESPN's season outlook: word word", p)
        self.assertIn("...", p)
        self.assertLess(len(p), 2500)


# ---------------------------------------------------------------------------
# the lock


class TestParseVerdictNeverFailsOpen(unittest.TestCase):
    def test_the_tool_call_is_read(self):
        v = judge_mod.parse_verdict(
            verdict_payload(approve=False, reason="He is a first-round pick.",
                            concern="Revisit after the All-Star break."),
            model="m",
        )
        self.assertFalse(v.approve)
        self.assertEqual(v.reason, "He is a first-round pick.")
        self.assertEqual(v.concern, "Revisit after the All-Star break.")
        self.assertEqual(v.model, "m")
        self.assertFalse(v.errored)

    def test_prose_without_a_verdict_is_an_error_not_an_approval(self):
        """The failure mode this whole design exists to prevent.

        "I would not recommend this" is a block in English and an approval to
        any code that treats a non-empty response as success.
        """
        with self.assertRaises(judge_mod.JudgeError) as caught:
            judge_mod.parse_verdict(
                {
                    "stop_reason": "end_turn",
                    "content": [
                        {"type": "text", "text": "I would not recommend this."}
                    ],
                },
                model="m",
            )
        # The words are quoted, because otherwise the only diagnostic for a
        # model that stopped using the tool is silence.
        self.assertIn("would not recommend", str(caught.exception))
        self.assertIn("end_turn", str(caught.exception))

    def test_a_truncated_tool_call_is_an_error(self):
        """``max_tokens`` can cut a tool call off mid-argument."""
        with self.assertRaises(judge_mod.JudgeError):
            judge_mod.parse_verdict(
                {
                    "stop_reason": "max_tokens",
                    "content": [
                        {"type": "tool_use", "name": "record_verdict", "input": {}}
                    ],
                },
                model="m",
            )

    def test_some_other_tool_is_not_a_verdict(self):
        with self.assertRaises(judge_mod.JudgeError):
            judge_mod.parse_verdict(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "something_else",
                            "input": {"approve": True},
                        }
                    ]
                },
                model="m",
            )

    def test_an_empty_response_is_an_error(self):
        with self.assertRaises(judge_mod.JudgeError):
            judge_mod.parse_verdict({"content": []}, model="m")

    def test_a_verdict_with_no_reason_still_says_something(self):
        v = judge_mod.parse_verdict(
            verdict_payload(approve=False, reason="  "), model="m"
        )
        self.assertEqual(v.reason, "no reason given")


# ---------------------------------------------------------------------------
# the call


class TestReviewSwallowsEveryFailureIntoABlock(unittest.TestCase):
    def test_a_good_response_comes_back_as_a_verdict(self):
        client = _Client(_Resp(200, verdict_payload(True, "Replaceable.")))
        j = judge_mod.AddDropJudge("k-test", model="m", client=client)
        v = j.review(league_name="Dynassty", keeper=True, plan=plan())
        self.assertTrue(v.approve)
        self.assertEqual(v.reason, "Replaceable.")

    def test_the_verdict_tool_is_forced(self):
        """Pinned ``tool_choice`` is what makes the parser's job well-posed."""
        client = _Client(_Resp(200, verdict_payload()))
        judge_mod.AddDropJudge("k-test", model="m", client=client).review(
            league_name="Dynassty", keeper=True, plan=plan()
        )
        body = client.calls[0]["json"]
        self.assertEqual(
            body["tool_choice"], {"type": "tool", "name": "record_verdict"}
        )
        self.assertEqual(body["model"], "m")
        self.assertIn("last check", body["system"])

    def test_the_key_travels_in_the_header_and_not_the_body(self):
        client = _Client(_Resp(200, verdict_payload()))
        judge_mod.AddDropJudge("k-test", model="m", client=client).review(
            league_name="Dynassty", keeper=True, plan=plan()
        )
        call = client.calls[0]
        self.assertEqual(call["headers"]["x-api-key"], "k-test")
        self.assertEqual(
            call["headers"]["anthropic-version"], judge_mod.API_VERSION
        )
        self.assertNotIn("k-test", repr(call["json"]))

    def test_an_http_error_blocks_and_is_marked_as_unreached(self):
        client = _Client(_Resp(401, None, text="invalid x-api-key"))
        v = judge_mod.AddDropJudge("k-test", client=client).review(
            league_name="Dynassty", keeper=True, plan=plan()
        )
        self.assertFalse(v.approve)
        self.assertTrue(v.errored)
        # The cause is carried, because the alternative is a user watching
        # swaps stop being proposed with no explanation anywhere.
        self.assertIn("401", v.reason)
        self.assertIn("invalid x-api-key", v.reason)

    def test_a_transport_failure_blocks(self):
        client = _Client(RuntimeError("connect timeout"))
        v = judge_mod.AddDropJudge("k-test", client=client).review(
            league_name="Dynassty", keeper=True, plan=plan()
        )
        self.assertFalse(v.approve)
        self.assertTrue(v.errored)
        self.assertIn("connect timeout", v.reason)

    def test_a_missing_verdict_blocks(self):
        client = _Client(_Resp(200, {"stop_reason": "end_turn", "content": []}))
        v = judge_mod.AddDropJudge("k-test", client=client).review(
            league_name="Dynassty", keeper=True, plan=plan()
        )
        self.assertFalse(v.approve)
        self.assertTrue(v.errored)

    def test_an_unreached_judge_describes_itself_as_unreached(self):
        """A degraded run has to read as a fault, not as a judgment.

        Otherwise a week of outages is indistinguishable on the status page
        from a week of the reviewer being unimpressed.
        """
        blocked = judge_mod.Verdict(approve=False, reason="He is a keeper.")
        unreached = judge_mod.Verdict(
            approve=False, reason="timeout", errored=True
        )
        self.assertIn("blocked it", blocked.describe())
        self.assertIn("could not be reached", unreached.describe())


# ---------------------------------------------------------------------------
# the wiring


class _Judge:
    """A judge that answers however the test says, and records what it saw."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.seen = []

    def review(self, **kw):
        self.seen.append(kw)
        return self.verdict


class TestTheGateSitsBeforeTheUser(unittest.TestCase):
    def harness(self, verdict=None, keeper=False):
        h = _Harness(judge=_Judge(verdict) if verdict is not None else None)
        h.lg = _League(
            _Read(pool=[player(90, name="Better", slot=12)]), keeper=keeper
        )
        return h

    def test_with_no_key_the_proposal_is_untouched(self):
        h = self.harness()
        decision, notes = h.consider()
        self.assertTrue(decision.proposed)
        self.assertEqual([n for n in notes if "review" in n], [])

    def test_an_approval_leaves_the_computed_rationale_alone(self):
        """The ``Move.rationale`` invariant: the numbers stay checkable."""
        h = self.harness(judge_mod.Verdict(True, "Weak is replaceable."))
        decision, notes = h.consider()
        self.assertTrue(decision.proposed)
        self.assertNotIn("Reviewer", decision.rationale)
        self.assertTrue(any("cleared review" in n for n in notes))
        self.assertTrue(any("Weak is replaceable." in n for n in notes))

    def test_a_block_withholds_the_swap_and_says_why(self):
        h = self.harness(
            judge_mod.Verdict(False, "He went first overall and is owned in 88%.")
        )
        decision, notes = h.consider()
        self.assertIsNone(decision)
        joined = " | ".join(notes)
        self.assertIn("first overall", joined)
        self.assertIn("blocked it", joined)
        # Both players named, so the note reads on its own: a status page line
        # saying only "blocked it" cannot be checked against anything.
        self.assertIn("Better for P3", joined)

    def test_an_unreachable_reviewer_also_withholds_it(self):
        h = self.harness(
            judge_mod.Verdict(False, "connect timeout", errored=True)
        )
        decision, notes = h.consider()
        self.assertIsNone(decision)
        self.assertIn("could not be reached", " | ".join(notes))

    def test_a_caveat_is_appended_to_the_rationale_not_substituted(self):
        h = self.harness(
            judge_mod.Verdict(
                True, "Fine on the numbers.", concern="Thin at centre after this."
            )
        )
        decision, _notes = h.consider()
        self.assertIn("Thin at centre after this.", decision.rationale)
        # The computed half survives: the reviewer adds to the record, it does
        # not replace it.
        self.assertIn("per game against", decision.rationale)
        self.assertIn("points over the rest of the week", decision.rationale)

    def test_the_reviewer_is_told_which_league_this_is(self):
        h = self.harness(judge_mod.Verdict(True, "ok"), keeper=True)
        h.consider()
        seen = h.app.judge.seen[0]
        self.assertTrue(seen["keeper"])
        self.assertEqual(seen["league_name"], "Test League")
        self.assertIs(seen["plan"].add.player_id, 90)

    def test_a_run_with_nothing_to_propose_spends_no_call(self):
        """Nothing to veto, so there is nothing to ask about."""
        h = self.harness(judge_mod.Verdict(True, "ok"))
        h.lg = _League(_Read(pool=[]), keeper=False)
        decision, _notes = h.consider()
        self.assertIsNone(decision)
        self.assertEqual(h.app.judge.seen, [])


class _PerSwapJudge:
    """A reviewer with an opinion about each swap rather than about all of them.

    The single-verdict ``_Judge`` above cannot express the case the list cares
    about -- one swap blocked and the next one fine -- and that case is the
    whole reason a block does not end the run.
    """

    def __init__(self, decide):
        self.decide = decide
        self.seen = []

    def review(self, **kw):
        self.seen.append(kw)
        return self.decide(kw["plan"])


class TestTheReviewerSeesEverySwap(unittest.TestCase):
    """A run offering several swaps shows the reviewer each of them, and the
    two kinds of refusal mean opposite things for the ones still to come."""

    def test_every_offer_was_reviewed(self):
        h = funnel(judge=_PerSwapJudge(lambda p: judge_mod.Verdict(True, "ok")))
        decisions, _notes = h.consider_all(target=funnel_target())
        self.assertEqual(len(decisions), 3)
        self.assertEqual(
            [k["plan"].add.name for k in h.app.judge.seen],
            ["FA90", "FA91", "FA92"],
        )

    def test_a_block_is_about_that_swap_and_the_run_carries_on(self):
        """*"this is why we need ai options in here"* -- the reviewer is there
        to stop one bad idea, not to end the run that had it."""
        h = funnel(judge=_PerSwapJudge(
            lambda p: judge_mod.Verdict(False, "FA90 is hurt.")
            if p.add.name == "FA90" else judge_mod.Verdict(True, "ok")
        ))
        decisions, notes = h.consider_all(target=funnel_target())
        self.assertEqual([d.add.name for d in decisions], ["FA91", "FA92"])
        self.assertIn("FA90 is hurt.", " | ".join(notes))

    def test_a_blocked_pair_is_not_put_forward_again(self):
        """The verdict is prose, so there is no telling which half it objected
        to. Re-offering a pair that shares a half with a rejected one is asking
        the reviewer the same question twice."""
        h = funnel(judge=_PerSwapJudge(
            lambda p: judge_mod.Verdict(False, "no")
            if p.add.name == "FA90" else judge_mod.Verdict(True, "ok")
        ))
        decisions, _notes = h.consider_all(target=funnel_target())
        self.assertNotIn("FA90", [d.add.name for d in decisions])
        self.assertNotIn("Weak", [d.drop.name for d in decisions])

    def test_an_unreachable_reviewer_stops_the_list_entirely(self):
        """Not a judgment about this swap, so the next one would be withheld
        for the same non-reason. One wasted request, not three."""
        h = funnel(judge=_PerSwapJudge(
            lambda p: judge_mod.Verdict(False, "connect timeout", errored=True)
        ))
        decisions, notes = h.consider_all(target=funnel_target())
        self.assertEqual(decisions, [])
        self.assertEqual(len(h.app.judge.seen), 1)
        self.assertIn("stopped looking for further swaps", " | ".join(notes))

    def test_the_reviewer_reads_the_card_the_user_would(self):
        """Including the line saying this one's number assumes the swap above
        it. The reviewer is being asked whether the case holds up, and the case
        for the second swap is conditional -- hiding that from the reviewer
        while showing it to the user would be two different questions."""
        h = funnel(judge=_PerSwapJudge(lambda p: judge_mod.Verdict(True, "ok")))
        decisions, _notes = h.consider_all(target=funnel_target())
        self.assertNotIn("Priced as the", h.app.judge.seen[0]["plan"].rationale)
        self.assertIn(
            "second move of this run", h.app.judge.seen[1]["plan"].rationale
        )
        # And what the reviewer saw is what the proposal carries, bar a caveat
        # the reviewer itself may add.
        self.assertEqual(
            h.app.judge.seen[1]["plan"].rationale, decisions[1].rationale
        )


class TestABlockIsNotAMove(unittest.TestCase):
    """*"lets give the judge a max of 5 possible options. the best options we
    have. its totally fine if they say bad to all of them, thats why we have
    the judge!"*

    ``addrop_max_moves`` counts the swaps that reach the user. A veto is not a
    swap, so it must not spend that budget -- and the bound on the other side
    is a separate, larger count of how many candidates the reviewer is shown.
    """

    def test_a_blocked_swap_does_not_spend_a_move(self):
        """Two moves asked for and the first candidate refused still yields
        two. Charging the veto to the move budget would have shown one, and
        the swap never offered was one the reviewer had approved.
        """
        h = funnel(max_moves=2, judge=_PerSwapJudge(
            lambda p: judge_mod.Verdict(False, "no")
            if p.add.name == "FA90" else judge_mod.Verdict(True, "ok")
        ))
        decisions, _notes = h.consider_all(target=funnel_target())
        self.assertEqual([d.add.name for d in decisions], ["FA91", "FA92"])

    def test_the_reviewer_is_shown_at_most_five_candidates(self):
        """The counterweight. A pool deep enough and a reviewer that refuses
        everything would otherwise walk the whole pool, at a request and
        several seconds each, to arrive at the same empty list.
        """
        h = open_funnel(judge=_PerSwapJudge(
            lambda p: judge_mod.Verdict(False, "not him either")
        ))
        decisions, notes = h.consider_all(target=open_funnel_target())
        self.assertEqual(decisions, [])
        self.assertEqual(len(h.app.judge.seen), 5)
        # Six were available, so stopping was the budget and not the pool.
        self.assertEqual(len(h.lg.read.pool), 6)
        self.assertIn(
            "stopped after 5 candidate(s): the reviewer blocked 5 of them",
            " | ".join(notes),
        )

    def test_the_move_budget_still_ends_the_list_first(self):
        """The review budget is a ceiling on looks, not a target. A reviewer
        that approves everything must still stop at ``addrop_max_moves``.
        """
        h = open_funnel(max_moves=3, judge=_PerSwapJudge(
            lambda p: judge_mod.Verdict(True, "ok")
        ))
        decisions, notes = h.consider_all(target=open_funnel_target())
        self.assertEqual(len(decisions), 3)
        self.assertEqual(len(h.app.judge.seen), 3)
        self.assertNotIn("the reviewer blocked", " | ".join(notes))

    def test_a_card_found_after_a_rejection_is_not_priced_on_it(self):
        """The conditional-pricing line counts accepted swaps, not attempts.

        A blocked candidate is never applied to the simulated roster, so the
        swap found after one is still priced against the real roster. Counting
        attempts told the user his first card assumed a swap "above it" that
        the reviewer had thrown away -- a sentence that would make him discount
        a number needing no discount, and look for a card that did not exist.
        """
        h = funnel(judge=_PerSwapJudge(
            lambda p: judge_mod.Verdict(False, "no")
            if p.add.name == "FA90" else judge_mod.Verdict(True, "ok")
        ))
        decisions, _notes = h.consider_all(target=funnel_target())
        self.assertEqual([d.add.name for d in decisions], ["FA91", "FA92"])
        self.assertNotIn("Priced as the", decisions[0].rationale)
        self.assertIn("the swap above it is approved", decisions[1].rationale)


if __name__ == "__main__":
    unittest.main()
