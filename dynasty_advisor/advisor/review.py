"""A second opinion on each trade idea and pickup, from a model that knows who
players are.

Why this exists
---------------
The engine prices trades in FantasyCalc value, an age curve and a lineup
delta. It is blind to everything those numbers do not encode: that a backup
quarterback is starting one week only, that a running back just lost his job,
that a 2028 first from the league's best team is a late pick, that a
partner already has three tight ends. The basketball app's reviewer exists for
the same reason; this is the football version of it.

What it is and is not
---------------------
A **reviewer, not an author**. It never names a new trade or a new player. It
is handed the ideas the engine already priced, with every number that priced
them, plus each roster and recent ESPN news, and asked for a verdict and a
reason on each. The numbers on the page stay the engine's; the review sits
beside them.

One request per run reviews everything together, so the model can compare
ideas against each other ("this is the same RB need as #2, done better").

Failure is visible, not silent
------------------------------
Unlike the basketball app, nothing here is executed: every idea is advice
the owner acts on by hand in Sleeper. So a reviewer that cannot be reached
does not hide the ideas. They are shown unreviewed under a banner saying why.
An idea the reviewer **passes on** is moved to a collapsed list with its
reason, not deleted, so a judgment call stays checkable.

The SDK is imported lazily: the rest of the app is standard library only, and
runs (and its tests) without the package installed or a key configured.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("advisor.review")

DEFAULT_MODEL = "claude-opus-5-5"
NEWS_URL = "https://site.api.espn.com/apis/fantasy/v3/games/ffl/news/players?playerId=%s&limit=3"
NEWS_TTL = 1800
NEWS_MAX_DAYS = 14
VERDICTS = ("go", "consider", "pass")

SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {
            "type": "string",
            "description": "Two or three sentences on where this team stands and what it most needs, as context for the verdicts.",
        },
        "trades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "The idea's number as given."},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "take": {"type": "string", "description": "One or two sentences: the deciding fact."},
                    "watch": {"type": "string", "description": "Optional caveat or negotiating tip. Empty if none."},
                },
                "required": ["n", "verdict", "take", "watch"],
                "additionalProperties": False,
            },
        },
        "moves": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "take": {"type": "string"},
                },
                "required": ["n", "verdict", "take"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "trades", "moves"],
    "additionalProperties": False,
}

SYSTEM = """\
You review trade ideas and waiver pickups for one team in a Sleeper dynasty \
fantasy football league, just before the owner reads them. A statistical engine \
produced every idea. Your job is to add the context its numbers cannot see.

What the engine already does well, so do not redo it: it prices every asset in \
FantasyCalc dynasty and rest-of-season value, applies an age curve (peak QB 30, \
RB 25, WR 27, TE 28), refuses any trade that loses long-term value for this \
team, protects untouchable and young rising players, and estimates how likely \
the other manager is to accept from their trade history and team situation. \
Trust that arithmetic and do not re-derive it.

What it cannot see, which is your job: role and depth-chart changes, injuries \
and how long they last, a starter who is only filling in for a week or two, a \
player whose market value lags what just happened to him, how good a future \
draft pick is likely to be given who owns it, whether this team actually needs \
what it is getting given its starting slots, and whether the other manager \
plausibly says yes given their roster. Use the news provided; it is more \
recent than your own knowledge. If your knowledge and the provided data \
disagree, trust the data and say so.

Verdicts:
- go: the case holds; you would send this offer.
- consider: reasonable, but it depends on something the owner should weigh \
(name it).
- pass: the numbers miss something that makes this a bad idea for this owner. \
Name the specific fact. "Feels risky" is not a reason.

This is a dynasty league: rosters carry over, so long-term value matters, and \
it is the thing a single season's numbers get most wrong. Be blunt and \
specific, and name players. Write for the owner, in plain sentences, at most \
two per field. Review every trade idea and every pickup you are given, using \
the numbers shown for each.\
"""


class ReviewError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def _player_line(p: Dict[str, Any]) -> str:
    bits = ["%s %s" % (p.get("pos") or "?", p.get("name"))]
    if p.get("age"):
        bits.append("age %.0f" % float(p["age"]))
    if p.get("team"):
        bits.append(p["team"])
    if p.get("prank"):
        bits.append("%s%d by dynasty value" % (p.get("pos") or "", p["prank"]))
    if p.get("dyn") is not None:
        bits.append("dynasty %s" % p["dyn"])
    if p.get("now") is not None:
        bits.append("rest-of-season %s" % p["now"])
    if p.get("depth"):
        bits.append("depth chart %s%s" % (p.get("depth_pos") or "", p["depth"]))
    if p.get("status"):
        inj = p["status"] + (" (%s)" % p["body"] if p.get("body") else "")
        bits.append("injury: " + inj)
    return ", ".join(bits)


def _roster(index: Dict[str, Any], rid: int, limit: int = 0) -> List[str]:
    rows = [p for p in index["players"].values() if p.get("owner") == rid and p.get("pos") in ("QB", "RB", "WR", "TE")]
    out: List[str] = []
    for pos in ("QB", "RB", "WR", "TE"):
        ps = sorted((p for p in rows if p["pos"] == pos), key=lambda p: -(p.get("dyn") or 0))
        if limit:
            ps = ps[:limit]
        out.extend("  " + _player_line(p) for p in ps)
    return out


def _rid(index: Dict[str, Any], name: str) -> Optional[int]:
    for rid, n in (index.get("teams") or {}).items():
        if n == name:
            return int(rid)
    return None


def _asset(a: Dict[str, Any], index: Dict[str, Any]) -> str:
    if a.get("kind") == "pick":
        return "PICK %s%s (value %s)" % (a.get("name"), " via " + a["via"] if a.get("via") else "", a.get("value"))
    p = (index.get("players") or {}).get(a.get("id") or "")
    return _player_line(p) if p else "%s %s (value %s)" % (a.get("pos"), a.get("name"), a.get("value"))


def _pkg(pk: Dict[str, Any], index: Dict[str, Any]) -> List[str]:
    out = ["  You give:"] + ["    " + _asset(a, index) for a in pk["give"]]
    out += ["  You get:"] + ["    " + _asset(a, index) for a in pk["get"]]
    rc = pk.get("roster_change") or 0
    out.append("  Engine numbers: market value %+d for you, your lineup rest-of-season %+d%s" % (
        pk.get("market") or 0, pk.get("lineup") or 0,
        ", you must drop %d to fit" % rc if rc > 0 else ""))
    return out


def _player_ids(view: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    for t in view.get("trades") or []:
        for side in ("give", "get"):
            ids += [a["id"] for a in t["opening"][side] if a.get("id")]
    for a in (view.get("moves") or {}).get("actions") or []:
        ids += [x["id"] for x in (a["add"], a.get("drop")) if x and x.get("id")]
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def fetch_news(http, index: Dict[str, Any], ids: Sequence[str], now: Optional[datetime] = None) -> Dict[str, List[str]]:
    """Recent ESPN headlines per player. Best effort: a miss is just no news."""
    now = now or datetime.now(timezone.utc)
    players = index.get("players") or {}

    def one(pid: str):
        espn = (players.get(pid) or {}).get("espn")
        if not espn:
            return pid, []
        try:
            feed = ((http.get_json(NEWS_URL % espn, NEWS_TTL) or {}).get("news") or {}).get("feed") or []
        except Exception as e:  # noqa: BLE001 - news is optional
            log.info("review: news for %s unavailable: %s", pid, e)
            return pid, []
        out = []
        for n in feed:
            when = n.get("published") or ""
            try:
                dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                continue
            if (now - dt).days > NEWS_MAX_DAYS:
                continue
            text = " ".join(str(n.get("story") or n.get("description") or "").split())
            if len(text) > 400:
                text = text[:400].rsplit(" ", 1)[0] + "..."
            out.append("%s: %s%s" % (dt.date().isoformat(), n.get("headline") or "", (" -- " + text) if text else ""))
        return pid, out

    with ThreadPoolExecutor(max_workers=8) as ex:
        return {pid: lines for pid, lines in ex.map(one, ids) if lines}


def build_prompt(view: Dict[str, Any], index: Dict[str, Any], league: Dict[str, Any],
                 news: Optional[Dict[str, List[str]]] = None) -> str:
    """The whole case, as one plain-text brief. Pure, so it is testable."""
    news = news or {}
    me = view["me"]
    L: List[str] = []
    L.append("League: %d teams, starting lineup %s%s, scoring %s." % (
        me["teams"], " ".join(league.get("slots") or ()),
        " (superflex: a QB can fill SUPER_FLEX, so QBs are worth far more than in 1-QB leagues)" if league.get("superflex") else "",
        league.get("scoring") or "standard"))
    L.append("Season %s, week %s. Trade deadline: %s." % (
        view["season"], view["week"],
        "none" if not view.get("deadline") else "week %s (%s)" % (view["deadline"]["week"], view["deadline"]["state"])))
    L.append("")
    L.append("THIS TEAM: %s, record %s, projected to finish %s of %d, %s. Strategy for this run: %s." % (
        me["name"], me["record"], me["finish"], me["teams"], me["mode"], view["strategy"]))
    prot = view.get("protected") or []
    unt = [p["name"] for p in prot if p.get("untouchable")]
    core = [p["name"] for p in prot if not p.get("untouchable")]
    if unt:
        L.append("Untouchable (the owner's choice; never offered): " + ", ".join(unt))
    if core:
        L.append("Young and rising (offered only when nothing coming back is a veteran and no long-term value is "
                 "lost, so seeing one in an idea is expected): " + ", ".join(core))
    L.append("Roster:")
    L.extend(_roster(index, index["me"]))
    picks = view.get("picks") or []
    if picks:
        L.append("Draft picks: " + "; ".join(
            "%s%s (value %s)" % (p["name"], " via " + p["via"] if p.get("via") else "", p.get("value")) for p in picks))
    L.append("")
    L.append("LEAGUE STANDINGS (projected finish): " + "; ".join(
        "%d. %s %s %s" % (t["rank"], t["name"], t["record"], t["mode"]) for t in view.get("teams") or []))
    L.append("")

    trades = view.get("trades") or []
    L.append("TRADE IDEAS (%d)" % len(trades))
    for n, t in enumerate(trades, 1):
        L.append("")
        L.append("Idea %d: with %s (%s; %s). Engine's acceptance estimate: %s." % (
            n, t["partner"], t["mode"], t.get("context") or "", t["chance"]))
        L.extend(_pkg(t["opening"], index))
        for note in t.get("notes") or ():
            L.append("  Engine note: " + note)
        if t.get("accepted") is not None:
            L.append("  %s has accepted %d and proposed %d trades since %s." % (
                t["partner"], t["accepted"], t["proposed"], t.get("since") or "?"))
        rid = _rid(index, t["partner"])
        if rid is not None:
            L.append("  %s's roster (top at each position):" % t["partner"])
            L.extend("  " + r for r in _roster(index, rid, limit=4))
    L.append("")

    actions = (view.get("moves") or {}).get("actions") or []
    L.append("WAIVER PICKUPS (%d)" % len(actions))
    for n, a in enumerate(actions, 1):
        add, drop = a["add"], a.get("drop")
        L.append("Pickup %d: add %s%s%s" % (
            n, _asset(add, index), "; drop " + _asset(drop, index) if drop else " into an open roster spot",
            "; the engine says he would start for this team" if add.get("starts") else ""))
    L.append("")

    if news:
        L.append("RECENT NEWS (ESPN, last %d days)" % NEWS_MAX_DAYS)
        names = {pid: (index["players"].get(pid) or {}).get("name", pid) for pid in news}
        for pid, lines in news.items():
            L.append("%s:" % names[pid])
            L.extend("  " + x for x in lines)
        L.append("")
    L.append("Review every trade idea (by its idea number) and every pickup (by its pickup number).")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def parse(payload_text: str, n_trades: int, n_moves: int) -> Dict[str, Any]:
    try:
        data = json.loads(payload_text)
    except ValueError as e:
        raise ReviewError("reviewer returned invalid JSON: %s" % e)
    out = {"overview": str(data.get("overview") or "").strip(), "trades": {}, "moves": {}}
    for key, limit in (("trades", n_trades), ("moves", n_moves)):
        for r in data.get(key) or ():
            try:
                n = int(r.get("n"))
            except (TypeError, ValueError):
                continue
            v = str(r.get("verdict") or "")
            if 1 <= n <= limit and v in VERDICTS:
                out[key][n] = {"verdict": v, "take": str(r.get("take") or "").strip(),
                               "watch": str(r.get("watch") or "").strip()}
    return out


def call_claude(api_key: str, model: str, prompt: str, client: Any = None) -> str:
    """One Messages request with a JSON-schema output. Returns the JSON text."""
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise ReviewError("the anthropic package is not installed")
        client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=2)
    try:
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except Exception as e:  # noqa: BLE001 - every API failure is reported the same way
        raise ReviewError("%s: %s" % (type(e).__name__, str(e)[:300]))
    if resp.stop_reason == "refusal":
        raise ReviewError("the model declined to review this run")
    if resp.stop_reason == "max_tokens":
        raise ReviewError("the review was cut off (max_tokens)")
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
    if not text:
        raise ReviewError("no review in the response (stop_reason=%s)" % resp.stop_reason)
    u = getattr(resp, "usage", None)
    if u is not None:
        log.info("review: %s, %s in / %s out tokens", getattr(resp, "model", model),
                 getattr(u, "input_tokens", "?"), getattr(u, "output_tokens", "?"))
    return text


def review_view(view: Dict[str, Any], index: Dict[str, Any], league: Dict[str, Any], *,
                api_key: str, model: str = DEFAULT_MODEL, http=None, client: Any = None) -> Dict[str, Any]:
    """Annotate ``view`` in place: each trade and pickup gains ``review``, and
    ``view["review"]`` records the overview or the error. Never raises."""
    trades = view.get("trades") or []
    actions = (view.get("moves") or {}).get("actions") or []
    meta: Dict[str, Any] = {"model": model}
    view["review"] = meta
    if not trades and not actions:
        meta["overview"] = ""
        return view
    try:
        news = fetch_news(http, index, _player_ids(view)) if http is not None else {}
        prompt = build_prompt(view, index, league, news)
        log.debug("review prompt:\n%s", prompt)
        got = parse(call_claude(api_key, model, prompt, client), len(trades), len(actions))
    except Exception as e:  # noqa: BLE001 - the page shows the ideas unreviewed
        log.error("review failed, showing ideas unreviewed: %s", e)
        meta["error"] = str(e)[:300]
        return view
    meta["overview"] = got["overview"]
    for n, t in enumerate(trades, 1):
        if n in got["trades"]:
            t["review"] = got["trades"][n]
    for n, a in enumerate(actions, 1):
        if n in got["moves"]:
            a["review"] = got["moves"][n]
    missing = len(trades) - len(got["trades"]) + len(actions) - len(got["moves"])
    if missing:
        meta["missing"] = missing
    return view


def passed(view: Dict[str, Any]) -> set:
    """1-based numbers of the trade ideas the reviewer passed on."""
    return {n for n, t in enumerate(view.get("trades") or [], 1)
            if (t.get("review") or {}).get("verdict") == "pass"}
