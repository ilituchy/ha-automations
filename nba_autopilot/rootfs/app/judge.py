"""A second opinion on a proposed swap, from a model that knows who players are.

Why this exists
---------------
``solve/addrop.py`` is a good week-surplus model and a poor judge of assets,
and those are different jobs. Its whole value comes from being blind to
everything outside the current matchup period -- that blindness is what makes
"how many points does this swap gain us this week" answerable at all. But the
same blindness means a player who will not suit up in the next five days has a
surplus of exactly zero, and zero is the cheapest thing on the roster to give
away. It cannot distinguish a season-ending injury from a sprained ankle,
because to that model they are the same number.

Observed on the two live leagues: the layer proposed dropping an 88%-owned
rookie with a first-overall NBA pedigree, parked on IR with a short-term
injury, on back-to-back runs. In its own terms nothing was wrong. The user's
instruction, verbatim:

    "the ai should be called to validate any recoomendations for add drops
    before i see them. ai knows the context around first round picks, famous
    players, past stats. we should be prompting the ai to be a judge of weather
    the statisical value of the add drop makes sense. it should read recent
    reports, and be able to show why it rejected a request to add drop"

So this is a **gate, not an author**. It never names a player, never proposes
a swap, never ranks the pool. It is handed one fully-priced proposal with every
number that produced it, and asked a single question: does the statistical case
hold up against what you know that these numbers cannot encode. Keeping it on
that side of the line is what makes it auditable -- a rejection is checkable
against the numbers it was shown, and an approval adds no claim of its own.

Where it sits in the invariants
-------------------------------
``models.Move.rationale`` says: *"Never written by an LLM without the numbers
being present and checkable."* That holds here. The rationale on the card
remains the one ``addrop._rationale`` assembled out of computed values; this
module contributes a *verdict* plus a reason for it, recorded beside the
numbers rather than in place of them.

ADR-001 is stdlib plus ``httpx``, which is what this uses. It is deliberately
not an SDK dependency: one POST to one endpoint does not justify a package, and
the request body below is the entire contract.

Fail-closed, and why that is not the same as fail-off
-----------------------------------------------------
Configuring an API key is a statement that swaps should be judged before they
are shown. So once a key is present, a proposal this module could not reach a
verdict on **is not shown** -- the call failing is not permission to proceed
unexamined, because the move it would license is irreversible and publicly
attributed.

With no key configured there is no gate at all and the previous behaviour
stands unchanged. That asymmetry is the point: the feature is opt-in, and
opting in cannot quietly degrade back to opted-out the first time a network
call times out.

The forced tool call
--------------------
The verdict comes back as a forced tool invocation rather than as JSON in
prose. Parsing a model's free text for a yes/no that gates an irreversible
action is a failure mode waiting to happen -- "I would not recommend this" and
``{"approve": false}`` are not the same artifact, and the first one needs a
regex nobody can prove correct. With ``tool_choice`` pinned to one tool the
shape is the API's problem rather than ours, and a response that still does not
carry it is an error, which fails closed like any other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import httpx

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "MAX_OUTLOOK_CHARS",
    "JudgeError",
    "Verdict",
    "AddDropJudge",
    "build_prompt",
    "parse_verdict",
]

log = logging.getLogger(__name__)


class JudgeError(RuntimeError):
    """The judge could not reach a verdict. Always fails the proposal closed."""

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

#: Sonnet rather than the largest model available. This is one call per
#: proposal per run -- a handful a day -- so cost is not the argument; the
#: argument is that the task is bounded recall plus a yes/no on a case that
#: arrives fully priced, and latency sits inside a scheduled run the user is
#: waiting on. Overridable, because a user who wants a different trade should
#: not have to edit the app to get it.
DEFAULT_MODEL = "claude-sonnet-4-6"

#: Seconds. Long enough for a considered answer, short enough that a hung
#: endpoint does not hold a run open; a timeout fails closed, so the cost of
#: this being too short is a swap deferred to tomorrow.
DEFAULT_TIMEOUT = 45.0

#: ESPN's season outlook runs to a few hundred words per player and there are
#: two of them in the prompt. Truncated rather than dropped: the first lines
#: carry the role and the pedigree, which is the part that matters here.
MAX_OUTLOOK_CHARS = 700

VERDICT_TOOL = {
    "name": "record_verdict",
    "description": (
        "Record your judgment on the proposed swap. Call this exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "approve": {
                "type": "boolean",
                "description": (
                    "True if the swap should be shown to the user for "
                    "approval. False to block it."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why, in one or two sentences, addressed to the team's "
                    "owner. Name the specific fact that decided it. If you are "
                    "blocking the swap, say what the numbers missed."
                ),
            },
            "concern": {
                "type": "string",
                "description": (
                    "Optional. A caveat worth stating even though you are "
                    "approving. Leave empty if there is none."
                ),
            },
        },
        "required": ["approve", "reason"],
    },
}

SYSTEM = """\
You are reviewing proposed fantasy basketball transactions for the owner of \
the team, immediately before he is asked to approve them. You are the last \
check between a statistical model and an irreversible, publicly attributed \
move.

The model that produced this proposal is good at one thing and blind to \
another. It prices the rest of the current scoring week exactly: how many \
points a pickup would actually add, given who else is playing that night, how \
many starting slots are free, and what a game off the games-played cap costs. \
Trust that arithmetic. Do not re-derive it and do not argue with the \
projections.

What it cannot see is everything outside this week. A player who will not play \
in the next few days is worth zero to it, whether he is out for the season or \
out until Friday. It does not know who is a \
returning All-Star, which injuries are three-day injuries, or anything at all \
about a player beyond the next few days. That is the gap you are filling, and \
the only gap.

Block the swap when the player going out is worth materially more than this \
week's number says and the player coming in is not worth giving that up for. \
The usual shapes: a high-pedigree player on a short-term injury; an \
established producer in a temporary slump or a brief role change; a young \
player whose season has not started yet. Ownership share is the single most \
useful number you are given -- it is thousands of other owners' judgment of \
exactly the question you are being asked, and a player held in most leagues \
while contributing nothing this week is being held for a reason.

Approve the swap when the case holds: the player going out is genuinely \
replaceable, or the gain is large enough to be worth the asset, or the league \
is a redraft league where only this season matters.

In a keeper or dynasty league, one more thing, and it is the thing the \
numbers are worst at. **A young player on a rookie contract is an asset \
whether or not he is producing yet, and ownership share will not tell you so \
-- it will tell you the opposite.** Most leagues are redraft leagues, so a \
nineteen-year-old lottery pick averaging eight minutes is held in very few of \
them, and the low number you are shown is thousands of owners answering a \
question you are not being asked. You are given each player's age, draft year \
and draft slot for exactly this. Treat a high pick in his first or second \
season as a hold by default: a cold start is the ordinary beginning of a \
career, not evidence that he is replaceable, and a roster spot is a cheap \
price for the two or three years it takes to find out. Block a swap that \
sends one out for a veteran who is better this week, and say so plainly. The \
exception is a gain large enough to decide a matchup that is actually close, \
and you are shown the score to judge that with.

This cuts both ways. A journeyman in his tenth season who is hot this \
fortnight is not a dynasty asset, and holding him over a prospect because he \
is outscoring him this week is the same mistake pointing the other way.

Two things to hold on to. First, a block costs the owner real points he was \
shown a case for, so "this feels aggressive" is not a reason -- name the \
specific fact the numbers missed. Second, a keeper league and a redraft league \
are different questions, and you are told which one this is: in a redraft \
league next season is not a consideration at all, and a prospect is worth \
exactly what he scores between now and April.

Call record_verdict exactly once.\
"""


@dataclass(frozen=True)
class Verdict:
    """Approve or block, with a reason either way.

    ``errored`` separates "the judge looked and said no" from "the judge could
    not be reached". Both block the proposal, and they are not the same event:
    the first is working as intended and belongs on the status page as a
    decision, the second is a degraded run and belongs there as a fault. A
    single boolean would make a week of outages look like a week of good
    judgment.
    """

    approve: bool
    reason: str
    #: Stated even on an approval, so a caveat the model thought worth raising
    #: is not thrown away just because it did not change the answer.
    concern: str = ""
    model: str = ""
    errored: bool = False

    def describe(self) -> str:
        if self.errored:
            return "the reviewer could not be reached: %s" % self.reason
        head = "the reviewer approved it" if self.approve else "the reviewer blocked it"
        out = "%s: %s" % (head, self.reason)
        if self.concern:
            out += " (caveat: %s)" % self.concern
        return out


def _outlook(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) <= MAX_OUTLOOK_CHARS:
        return text
    return text[:MAX_OUTLOOK_CHARS].rsplit(" ", 1)[0] + "..."


def _report_for(note: Any) -> str:
    """One player's beat reporting, as ``sources/espn_injuries.py`` parsed it.

    Attributed and timestamped, which is why it is quoted rather than
    summarized: this is ESPN's own bundled reporting and the reviewer should be
    reading the words the reporter wrote, not our regex's opinion of them.
    """
    if note is None:
        return ""
    bits = []
    status = getattr(note, "status_raw", "") or getattr(note, "status", "")
    if status:
        bits.append("listed %s" % status)
    reported = getattr(note, "reported_at", None)
    if reported is not None:
        bits.append("reported %s" % reported.date().isoformat())
    ret = getattr(note, "return_date", None)
    if ret is not None:
        bits.append("expected back %s" % ret.isoformat())
    if getattr(note, "out_for_season", False):
        bits.append("DESCRIBED AS OUT FOR THE SEASON")
    head = ", ".join(bits)
    body = (
        getattr(note, "long_comment", "")
        or getattr(note, "short_comment", "")
        or ""
    )
    body = " ".join(body.split())
    if head and body:
        return "%s. Report: %s" % (head, body)
    return head or body


def _side(label: str, cand: Any, note: Any, bio: Any = None) -> List[str]:
    lines = [
        "%s: %s" % (label, cand.name),
        "  projects %.1f points per game when he plays" % cand.per_game,
        "  %d game(s) left this week, %.0f%% chance of suiting up"
        % (cand.games, max(cand.p_play, 0.0) * 100.0),
        "  worth %+.1f points to us over the rest of the week" % cand.surplus,
        "  market: %s" % cand.describe_market(),
    ]
    if getattr(cand, "on_ir", False):
        lines.append("  currently parked in an injured-reserve slot")
    # Only ever populated for a keeper league. In a redraft league a player's
    # age is not a fact about his value, and putting it in front of a reader
    # who has been told next season does not count is an invitation to weigh
    # it anyway.
    if bio is not None and getattr(bio, "known", lambda: False)():
        lines.append("  who he is: %s" % bio.describe())
    report = _report_for(note)
    if report:
        lines.append("  injury news: %s" % report)
    outlook = _outlook(getattr(cand, "outlook", ""))
    if outlook:
        lines.append("  ESPN's season outlook: %s" % outlook)
    return lines


def build_prompt(
    *,
    league_name: str,
    keeper: bool,
    plan: Any,
    matchup: Any = None,
    reports: Optional[Mapping[int, Any]] = None,
    bios: Optional[Mapping[int, Any]] = None,
    acquisitions_left: Optional[int] = None,
) -> str:
    """The whole case, in the order a reader needs it.

    Separated from the HTTP call so the prompt is testable as a pure function.
    It is the part most likely to be wrong and the part least likely to be
    exercised by a test that needs a network.

    Everything that moved the decision appears here, including the numbers the
    reviewer is told not to argue with -- a judge shown only the conclusion
    cannot tell a 70-point swap from a 4-point one, and those deserve different
    amounts of benefit of the doubt.
    """
    reports = reports or {}
    bios = bios or {}
    lines: List[str] = []
    lines.append("League: %s" % league_name)
    lines.append(
        "League type: %s"
        % (
            "keeper/dynasty -- rosters carry over between seasons, so a player "
            "has value beyond this week"
            if keeper
            else "redraft -- the roster is dissolved at the end of the season, "
            "so only this season matters"
        )
    )
    if matchup is not None:
        lines.append("Current matchup: %s" % matchup.describe())
    if acquisitions_left is not None:
        lines.append(
            "Acquisitions left this week: %d" % acquisitions_left
        )
    lines.append("")
    lines.extend(_side("COMING IN", plan.add, reports.get(plan.add.player_id),
                       bios.get(plan.add.player_id)))
    lines.append("")
    if plan.drop is None:
        lines.append(
            "GOING OUT: nobody. There is a free roster spot, so this is a "
            "pickup with nothing given up for it."
        )
    else:
        lines.extend(
            _side("GOING OUT", plan.drop, reports.get(plan.drop.player_id),
                  bios.get(plan.drop.player_id))
        )
    lines.append("")
    lines.append(
        "The model's verdict: %+.1f points gained over the rest of the week, "
        "on the grounds of %s." % (plan.gain, plan.reason or "no stated reason")
    )
    lines.append("Its own wording of the case: %s" % plan.rationale)
    lines.append("")
    lines.append(
        "Does this swap make sense? Call record_verdict with your answer."
    )
    return "\n".join(lines)


def parse_verdict(payload: Mapping[str, Any], *, model: str) -> Verdict:
    """Pull the forced tool call out of a Messages response.

    A response that does not contain it is an error rather than an implicit
    approval. The distinction matters because the most likely causes -- a
    refusal, a ``max_tokens`` truncation mid-tool-call, a model that does not
    support tools -- all produce *something*, and treating "something" as
    assent would let the gate fail open on exactly the inputs that confused it.
    """
    for block in payload.get("content") or ():
        if not isinstance(block, Mapping):
            continue
        if block.get("type") != "tool_use":
            continue
        if block.get("name") != VERDICT_TOOL["name"]:
            continue
        got = block.get("input")
        if not isinstance(got, Mapping) or "approve" not in got:
            break
        return Verdict(
            approve=bool(got.get("approve")),
            reason=str(got.get("reason") or "").strip() or "no reason given",
            concern=str(got.get("concern") or "").strip(),
            model=model,
        )
    stop = payload.get("stop_reason")
    text = " ".join(
        str(b.get("text") or "")
        for b in (payload.get("content") or ())
        if isinstance(b, Mapping) and b.get("type") == "text"
    ).strip()
    raise JudgeError(
        "no verdict in the response (stop_reason=%r)%s"
        % (stop, (": " + text[:200]) if text else "")
    )


class AddDropJudge:
    """Reviews one proposal per call. Holds no state between them.

    Deliberately not a ``sources/`` module. Those cache on a staleness window
    and return ``None`` when they cannot answer, because a missing projection
    degrades a run rather than deciding anything. This answers a question about
    one specific proposal, is never reusable across two of them, and its
    inability to answer *is* a decision.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.timeout = float(timeout)
        self._client = client

    def review(
        self,
        *,
        league_name: str,
        keeper: bool,
        plan: Any,
        matchup: Any = None,
        reports: Optional[Mapping[int, Any]] = None,
        bios: Optional[Mapping[int, Any]] = None,
        acquisitions_left: Optional[int] = None,
    ) -> Verdict:
        """Judge one swap. Never raises -- a failure comes back as a block.

        The exception is swallowed here rather than at the call site because
        there is exactly one correct response to every way this can fail, and
        it is the same response: do not show the proposal, and say why. Leaving
        that to the caller invites a ``try`` that logs and continues.
        """
        prompt = build_prompt(
            league_name=league_name,
            keeper=keeper,
            plan=plan,
            matchup=matchup,
            reports=reports,
            bios=bios,
            acquisitions_left=acquisitions_left,
        )
        log.debug("judge prompt:\n%s", prompt)
        try:
            payload = self._post(prompt)
            verdict = parse_verdict(payload, model=self.model)
        except Exception as exc:  # noqa: BLE001 - every failure is one answer
            log.error("add/drop review failed, blocking the swap: %s", exc)
            return Verdict(
                approve=False,
                reason=str(exc)[:300],
                model=self.model,
                errored=True,
            )
        log.info("add/drop review: %s", verdict.describe())
        return verdict

    def _post(self, prompt: str) -> Dict[str, Any]:
        body = {
            "model": self.model,
            # Room for a two-sentence reason and the tool envelope, and no
            # room for an essay. A truncated tool call fails closed, so this
            # being too small costs a deferred swap rather than a bad one.
            "max_tokens": 1024,
            "system": SYSTEM,
            "tools": [VERDICT_TOOL],
            "tool_choice": {"type": "tool", "name": VERDICT_TOOL["name"]},
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        client = self._client
        own = client is None
        if own:
            client = httpx.Client(timeout=httpx.Timeout(self.timeout))
        try:
            r = client.post(API_URL, json=body, headers=headers)
        finally:
            if own:
                client.close()
        if r.status_code != 200:
            # The body is quoted because it carries the actual cause -- an
            # invalid key, an unknown model name, a rate limit -- and that line
            # is the only thing standing between the user and "it just stopped
            # proposing swaps". The key is in the *request* headers, never in
            # the response, so there is nothing to redact here.
            raise JudgeError(
                "Anthropic API returned %d: %s"
                % (r.status_code, (r.text or "")[:300])
            )
        try:
            return r.json()
        except ValueError as exc:
            raise JudgeError("Anthropic API returned non-JSON: %s" % exc)
