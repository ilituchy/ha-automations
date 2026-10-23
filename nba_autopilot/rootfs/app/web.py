"""Ingress status page: the review surface for proposals needing approval.

Why this exists rather than notification taps alone: a notification is a
one-shot thing. Miss it, swipe it away, or read it on a locked screen and the
approval is gone until the proposal expires six hours later. This page is
always there, shows *why* a move was proposed next to the button that accepts
it, and survives being ignored.

Three properties are deliberate.

**It reuses the state machine; it does not reimplement it.** An approval from
this page calls the same ``Store.decide`` -> ``execute_approved`` pair a
notification tap calls, so the double-tap guard, the stale-proposal guard and
the audit trail are shared rather than duplicated. A second path to executing a
proposal is a second place for the ``PROPOSED`` check to be forgotten.

**Every URL it emits is relative.** Supervisor ingress serves the app under
``/api/hassio_ingress/<rotating-token>/``, so an absolute ``/decide`` would
escape the prefix and 404 against the HA frontend. There is an
``X-Ingress-Path`` header carrying the prefix, but not needing it at all is
better than using it correctly.

**Everything interpolated into the page is escaped.** Rationale strings contain
ESPN player names and, once the projection layer lands, LLM-generated text.
Both are untrusted input by the project's own rules, and this page renders
inside the authenticated HA frontend -- the single worst place to put an
injection.
"""

#
# **This module must not use ``from __future__ import annotations``.** That
# makes every annotation a string, and FastAPI resolves a route handler's
# annotations with ``get_type_hints``, which looks them up in *module* globals.
# ``Request`` is imported inside ``build_app`` to keep this file importable
# without FastAPI installed, so it is not a module global -- the name would not
# resolve, and FastAPI's fallback for a parameter whose type it cannot identify
# is to treat it as a required **query** parameter. The symptom is that every
# page load returns ``422 {"loc": ["query", "request"], "msg": "Field
# required"}``, which is how this was found: on the device, through the ingress
# proxy, because no test here imports FastAPI at all. See test_web.py.

import html
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs

log = logging.getLogger("autopilot.web")

#: Supervisor ingress authenticates the request before it reaches us and passes
#: the resolved Home Assistant user in these headers. The name is capitalized
#: as the Supervisor sends it; the lookups below are case-insensitive anyway
#: because Starlette normalizes header names.
H_USER_ID = "x-remote-user-id"
H_USER_NAME = "x-remote-user-display-name"

#: Only these kinds ever reach this page as decisions. Lineup and IR moves are
#: autonomous by configuration and are shown as already-applied history, not as
#: questions -- surfacing an Approve button for something that already happened
#: would be a lie about what the button does.
APPROVABLE = ("ADD", "DROP", "ADD_DROP")


def _e(v: Any) -> str:
    """Escape for HTML text content. Everything user- or ESPN-derived goes through here."""
    return html.escape(str(v), quote=True)


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


class StatusPage:
    """Builds the view model. Separated from routing so it is testable without HTTP."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    def league_label(self, league_id: Optional[int]) -> str:
        for lg in getattr(self.bot, "leagues", []):
            if lg.ref.league_id == league_id:
                return lg.ref.display()
        return "league %s" % (league_id if league_id is not None else "?")

    def pending(self) -> List[Dict[str, Any]]:
        """Proposals awaiting a decision, newest run first."""
        out = []
        for p in self.bot.store.pending():
            if p.kind not in APPROVABLE:
                # An autonomous class should never sit in PROPOSED. If one does,
                # something set auto_approve wrongly and the right move is to
                # say so rather than to render a button for it.
                log.warning("pending proposal %s has non-approvable kind %r",
                            p.action_id, p.kind)
                continue
            league_id = self._league_of_run(p.run_id)
            out.append({
                "action_id": p.action_id,
                "kind": p.kind,
                # The id as well as the label, because the page groups this
                # list into the same per-league columns as the runs and the
                # column order has to come from the configured ids.
                "league_id": league_id,
                "league": self.league_label(league_id),
                "payload": dict(p.payload),
                "rationale": p.rationale,
                "value_delta": p.value_delta,
                "expires_at": p.expires_at,
            })
        out.reverse()
        return out

    def _league_of_run(self, run_id: str) -> Optional[int]:
        rows = self.bot.store.query(
            "SELECT league_id FROM runs WHERE run_id=?", (run_id,)
        )
        return rows[0]["league_id"] if rows else None

    def recent_runs(self, limit: int = 8) -> List[Dict[str, Any]]:
        rows = self.bot.store.query(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
        )
        runs = []
        for r in rows:
            props = self.bot.store.query(
                "SELECT * FROM proposals WHERE run_id=? ORDER BY id", (r["run_id"],)
            )
            runs.append({
                "run_id": r["run_id"],
                "league_id": r["league_id"],
                "league": self.league_label(r["league_id"]),
                "started_at": r["started_at"],
                "scoring_period": r["scoring_period"],
                "shadow_mode": bool(r["shadow_mode"]),
                "notes": _loads(r["notes"], []),
                "shadow_prices": _loads(r["shadow_prices"], {}),
                "proposals": [{
                    "kind": p["kind"],
                    "state": p["state"],
                    "rationale": p["rationale"],
                    "payload": _loads(p["payload"], {}),
                } for p in props],
            })
        return runs

    def league_order(self) -> List[Any]:
        """Configured league ids, in the order they were configured.

        Column order has to be stable across loads, and "whichever ran most
        recently" is not: both leagues run in the same second, so a refresh
        could swap the columns for no reason a reader could see.
        """
        return [lg.ref.league_id for lg in getattr(self.bot, "leagues", [])]

    def tz(self) -> Any:
        """The configured zone, or ``None`` to leave times as stored.

        ``Autopilot`` resolves ``cfg.timezone`` once at startup; reading its
        attribute rather than re-resolving the name keeps the page and the
        scheduler agreed about what "today" means, and keeps ``zoneinfo`` out
        of this module's import path.
        """
        return getattr(self.bot, "tz", None)

    def stuck(self) -> List[Mapping[str, Any]]:
        return self.bot.store.stuck_writes()

    def shadow_mode(self) -> bool:
        return bool(self.bot.cfg.shadow_mode)


# -- rendering -------------------------------------------------------------
#
# Hand-written HTML with inline CSS, deliberately. A template engine and a
# static-file mount are two more things to get wrong inside an ingress prefix,
# and this is one page.
#
# Home Assistant's own variables are named first in every declaration so a
# custom theme can still drive the page, but the *fallbacks* are what actually
# renders: ingress serves this document inside an iframe, and CSS custom
# properties do not cross a document boundary, so nothing the frontend defines
# on its own :root reaches us. Dark mode therefore cannot ride on
# ``--primary-background-color`` the way it does in the frontend -- it needs
# ``prefers-color-scheme``, which the iframe *does* inherit. The fallbacks are
# kept in two token sets below so the HA-theme path and the standalone path
# stay in agreement.

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f1f2f6;
  --card: #fff;
  --fg: #16181d;
  --dim: #626a78;
  --faint: #8d94a1;
  --line: rgba(16,18,27,.09);
  --lift: 0 1px 2px rgba(16,18,27,.05), 0 6px 20px -8px rgba(16,18,27,.14);
  --good: #0f8f5a;
  --on-good: #fff;
  --good-bg: rgba(15,143,90,.10);
  --bad: #cf3340;
  --bad-bg: rgba(207,51,64,.10);
  --warn: #a96508;
  --warn-bg: rgba(169,101,8,.10);
  --chip: rgba(16,18,27,.055);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #131417;
    --card: #1d1f25;
    --fg: #e8eaef;
    --dim: #9ba2b0;
    --faint: #767d8b;
    --line: rgba(255,255,255,.08);
    --lift: 0 1px 2px rgba(0,0,0,.4);
    --good: #43cc8d;
    --on-good: #08281a;   /* the dark green is light enough that white fails */
    --good-bg: rgba(67,204,141,.13);
    --bad: #ff6f77;
    --bad-bg: rgba(255,111,119,.13);
    --warn: #e8a33d;
    --warn-bg: rgba(232,163,61,.13);
    --chip: rgba(255,255,255,.07);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 22px 16px 56px;
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", sans-serif;
  background: var(--primary-background-color, var(--bg));
  color: var(--primary-text-color, var(--fg));
  -webkit-font-smoothing: antialiased;
  font-variant-numeric: tabular-nums;
}
.wrap { max-width: 660px; margin: 0 auto; }
/* Wide enough for two columns of runs, and only then. Everything above them
   is a single file of decisions and stays at a reading width in the middle,
   because a swap card stretched across a desktop is two names a foot apart. */
@media (min-width: 1000px) {
  .wrap { max-width: 1040px; }
  /* Capped but not centred. Both lists below are now full-width grids, and a
     header band floating 190px to the right of the first column reads as a
     different page sitting on top of this one. */
  .solo { max-width: 660px; }
}
/* One column per league. `auto-fit` rather than a fixed two: the columns
   collapse to one on a phone on their own, and a third league gets a column
   instead of being appended to somebody else's. */
.leagues {
  display: grid; align-items: start; gap: 0 16px;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
}
.lghead {
  font-size: 13px; font-weight: 700; letter-spacing: -.01em;
  margin: 0 0 8px; padding: 0 2px;
  color: var(--primary-text-color, var(--fg));
}
.lg + .lg .lghead { margin-top: 18px; }
@media (min-width: 1000px) { .lg + .lg .lghead { margin-top: 0; } }

h1 {
  font-size: 24px; font-weight: 700; letter-spacing: -.02em;
  margin: 0 0 10px;
}
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 26px; }
.chip {
  font-size: 12px; font-weight: 600; line-height: 1;
  padding: 6px 10px; border-radius: 999px;
  background: var(--chip); color: var(--secondary-text-color, var(--dim));
}
.chip.on { background: var(--good-bg); color: var(--good); }
.chip.hold { background: var(--warn-bg); color: var(--warn); }

h2 {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--secondary-text-color, var(--faint));
  margin: 30px 0 10px;
}
h2:first-of-type { margin-top: 0; }
h2 .count {
  font-size: 11px; letter-spacing: 0; padding: 2px 7px; border-radius: 999px;
  background: var(--bad-bg); color: var(--bad);
}

.card {
  background: var(--card-background-color, var(--card));
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 16px;
  margin-bottom: 10px;
  box-shadow: var(--lift);
}
.banner { display: flex; gap: 12px; align-items: flex-start; }
.banner .bar {
  flex: 0 0 3px; align-self: stretch; border-radius: 3px;
  background: var(--warn); min-height: 34px;
}
.banner.bad .bar { background: var(--bad); }
.banner b { font-size: 14px; }

.eyebrow {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; margin-bottom: 12px;
}
.eyebrow .league {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--secondary-text-color, var(--faint));
}
.delta {
  font-size: 12px; font-weight: 700; white-space: nowrap;
  padding: 4px 9px; border-radius: 999px;
  background: var(--good-bg); color: var(--good);
}
.delta.down { background: var(--bad-bg); color: var(--bad); }

.swap { display: grid; gap: 9px; }
.leg { display: flex; align-items: center; gap: 10px; min-width: 0; }
.pip {
  flex: 0 0 22px; height: 22px; border-radius: 999px;
  display: grid; place-items: center;
  font-size: 15px; font-weight: 700; line-height: 1;
}
/* A headshot as a background rather than an <img>: a player ESPN has no
   photo for then degrades to an empty disc instead of a broken-image icon,
   with no script to handle the error. */
.face {
  position: relative;
  flex: 0 0 36px; height: 36px; border-radius: 999px;
  background: var(--chip) center 18%/125% no-repeat;
  box-shadow: inset 0 0 0 1px var(--line);
}
.face.sm { flex-basis: 28px; height: 28px; }
/* The +/- rides on the corner of the headshot rather than beside it, so a
   swap is two faces in a column and not four things in a row. */
.face .pip {
  position: absolute; right: -3px; bottom: -3px;
  flex: 0 0 18px; height: 18px; font-size: 13px;
  box-shadow: 0 0 0 2px var(--card-background-color, var(--card));
}
.face.sm .pip { flex-basis: 16px; height: 16px; font-size: 12px; }
.pip.in { background: var(--good-bg); color: var(--good); }
.pip.out { background: var(--bad-bg); color: var(--bad); }
.who {
  font-size: 17px; font-weight: 600; letter-spacing: -.01em;
  overflow-wrap: anywhere;
}
.leg.out .who { color: var(--secondary-text-color, var(--dim)); }
/* A linked name keeps the colour of the name it replaced. Twenty blue names
   down a column reads as a navigation list, and the thing being looked at is
   the move, not the link. The underline only arrives on hover -- enough to
   answer "is this clickable" at the moment the question is asked. */
a.who {
  color: inherit; text-decoration: none;
  text-underline-offset: 2px; text-decoration-thickness: 1.5px;
}
a.who:hover, a.who:focus-visible { text-decoration: underline; }
a.who:focus-visible {
  outline: 2px solid var(--primary-color, #2b7de9);
  outline-offset: 3px; border-radius: 3px;
}
.tag {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .06em; padding: 3px 7px; border-radius: 6px;
  background: var(--chip); color: var(--secondary-text-color, var(--dim));
  white-space: nowrap;
}
.tag.hold { background: var(--warn-bg); color: var(--warn); }
.tag.on { background: var(--good-bg); color: var(--good); }
.tag.off { background: var(--bad-bg); color: var(--bad); }

/* The rationale carries the pricing arithmetic and the reviewer's caveat in
   full -- worth having, and four times the height of the move it explains, so
   none of it shows until it is asked for. A clamped lead line was tried first
   and read as a card leading with its own footnotes.
   `<details>` rather than script: this page ships no JavaScript. */
.why { margin: 13px 0 0; font-size: 13.5px; line-height: 1.55; }
.why > summary { list-style: none; cursor: pointer; }
.why > summary::-webkit-details-marker { display: none; }
.why > summary::after {
  content: "Show details";
  font-size: 12px; font-weight: 600;
  color: var(--primary-color, #2b7de9);
}
.why[open] > summary::after { content: "Hide details"; }
.why .detail {
  margin-top: 10px; padding-left: 12px;
  border-left: 2px solid var(--line);
  color: var(--secondary-text-color, var(--dim));
}
.why .detail p { margin: 0; }
.why .detail p + p { margin-top: 9px; }
.meta {
  font-size: 12px; line-height: 1.45;
  color: var(--secondary-text-color, var(--faint));
}
.eyebrow + .meta, .why + .meta { margin-top: 8px; }

.actions { display: flex; gap: 8px; margin-top: 16px; }
form { margin: 0; flex: 1; }
button {
  width: 100%; padding: 12px 0;
  font: inherit; font-size: 15px; font-weight: 600;
  border-radius: 11px; cursor: pointer;
  transition: filter .12s ease, background .12s ease;
}
button.ok {
  border: 0; color: var(--on-good); background: var(--good);
  box-shadow: 0 1px 2px rgba(16,18,27,.18);
}
button.no {
  border: 1px solid var(--line); background: transparent;
  color: var(--secondary-text-color, var(--dim));
}
button:hover { filter: brightness(1.06); }
button.no:hover { background: var(--chip); }
button:active { transform: translateY(1px); }

/* A run is not a disclosure. What it changed is the thing the page is open
   for, so it is on the surface of the card and nothing has to be tapped to
   reach it; only the working notes are behind a control. */
.run { padding: 0; }
.runline { padding: 14px 16px 0; min-width: 0; }
.runline .top {
  display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
  margin-bottom: 3px;
}
.runline .name { font-size: 15px; font-weight: 600; }
.runbody { padding: 6px 16px 16px; }
.props { list-style: none; margin: 0; padding: 0; }
.props li { padding: 10px 0; border-top: 1px solid var(--line); }
.props .head { display: flex; align-items: center; gap: 7px; }
.props .kind { font-size: 13px; font-weight: 700; }
.props .swap { margin-top: 9px; gap: 7px; }
.props .swap .who { font-size: 15px; }
.sechead {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--secondary-text-color, var(--faint));
  margin: 14px 0 8px; display: flex; align-items: center; gap: 7px;
}
.props li:first-child .sechead, .runbody > .sechead:first-child {
  margin-top: 4px;
}

/* One applied lineup change: who, and the two slots. */
.did { list-style: none; margin: 0; padding: 0; }
.did li {
  display: flex; align-items: center; gap: 11px;
  padding: 7px 0; min-width: 0;
}
.did li + li { border-top: 1px solid var(--line); }
.did .who { font-size: 15px; font-weight: 600; }
.did .body { flex: 1; min-width: 0; }
.did .top {
  display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap;
}
.did .slots {
  display: flex; align-items: center; gap: 6px; margin-top: 1px;
  font-size: 11px; font-weight: 700; letter-spacing: .04em;
  color: var(--secondary-text-color, var(--faint));
}
.did .slots .to { color: var(--good); }
.did .slots .arrow { opacity: .6; font-weight: 400; }
.did .note { font-size: 12px; color: var(--secondary-text-color, var(--dim)); }

details.logs { margin-top: 14px; border-top: 1px solid var(--line); }
details.logs > summary {
  list-style: none; cursor: pointer; padding: 11px 0 0;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .07em; color: var(--secondary-text-color, var(--faint));
}
details.logs > summary::-webkit-details-marker { display: none; }
details.logs > summary::after { content: " \\25B8"; }
details.logs[open] > summary::after { content: " \\25BE"; }
.notes { list-style: none; margin: 8px 0 0; padding: 0; }
.notes li {
  font-size: 12px; line-height: 1.5; padding: 3px 0 3px 11px;
  color: var(--secondary-text-color, var(--dim));
  border-left: 2px solid var(--line); overflow-wrap: anywhere;
}

.empty {
  color: var(--secondary-text-color, var(--dim));
  font-size: 13.5px;
}
.empty b { display: block; color: var(--primary-text-color, var(--fg));
           font-size: 15px; margin-bottom: 3px; }
footer {
  margin-top: 28px; text-align: center;
  font-size: 12px; color: var(--secondary-text-color, var(--faint));
}
"""

#: Proposal states, mapped onto the three tag colours. Anything unlisted gets
#: the neutral chip, which is the right default for a state added later: a new
#: state painted green by accident reads as "this happened" when it may not
#: have.
STATE_TAGS = {
    "DONE": "on", "APPROVED": "on",
    "FAILED": "off", "REJECTED": "off", "EXPIRED": "off",
    "PROPOSED": "hold", "EXECUTING": "hold",
}

#: What ``state`` calls a write that landed. Spelled out rather than imported,
#: so the page has no reason to reach into the state machine; a test holds the
#: two in step. The first draft of this page guessed ``EXECUTED`` -- which is
#: ESPN's word on the wire, not ours -- and so painted every completed run
#: with the neutral chip and none of them with the green one.
DONE = "DONE"

#: ``ADD_DROP`` is the database's word for it, not a person's.
KIND_LABELS = {
    "ADD_DROP": "Add / drop", "ADD": "Add", "DROP": "Drop",
    "LINEUP": "Lineup", "IR": "IR move",
}


def _kind(kind: Any) -> str:
    key = str(kind).upper()
    return KIND_LABELS.get(key, key.replace("_", " ").capitalize())


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


#: ESPN's public headshot CDN. No cookies and no ids of ours go with the
#: request, but it is a third party learning which players are on the page, so
#: it is one URL in one place rather than scattered through the markup.
HEADSHOT = "https://a.espncdn.com/i/headshots/nba/players/full/%d.png"


def _face(player_id: Any, extra: str = "", badge: str = "") -> str:
    """An avatar disc for a player id, or a plain one if the id is unusable.

    ``int()`` is the escaping here: the value lands inside a ``style``
    attribute, where ``_e`` would not be enough on its own, so nothing but a
    number can ever reach the URL.
    """
    try:
        style = " style='background-image:url(%s)'" % (HEADSHOT % int(player_id))
    except (TypeError, ValueError):
        style = ""
    return "<span class='face %s'%s>%s</span>" % (extra, style, badge)


#: ESPN's own player page. The numeric id is the whole path -- ESPN redirects
#: to the slugged URL itself -- so no name of ours has to be made URL-safe.
PLAYER_URL = "https://www.espn.com/nba/player/_/id/%d"


def _who(player_id: Any, name: Any, cls: str = "who") -> str:
    """A player's name, linked to his ESPN page when we know his id.

    Every name on this page names a real player the reader may want to read up
    on, and the id to do it with is already in hand at both call sites. New tab,
    because the page it leaves is the one with the Approve button on it.

    ``int()`` again rather than ``_e()``: the id is interpolated into a URL, and
    the only safe thing to put in a URL is a number. An id we cannot parse --
    an old row, a name with no id behind it -- renders the bare name rather
    than a link to nowhere.
    """
    label = _e(name)
    try:
        href = PLAYER_URL % int(player_id)
    except (TypeError, ValueError):
        return "<span class=%s>%s</span>" % (cls, label)
    # noopener as well as noreferrer: the new tab must not keep a handle on
    # the authenticated frontend it was opened from.
    return ("<a class='%s link' href='%s' target=_blank "
            "rel='noopener noreferrer'>%s</a>" % (cls, href, label))


#: ESPN's slot abbreviations, as read by someone who did not write them. Only
#: the ones that are not already the word people use get an entry.
SLOT_WORDS = {"BE": "Bench"}


def _slot(name: Any) -> str:
    return SLOT_WORDS.get(str(name), str(name))


def _swap(payload: Mapping[str, Any], *, small: bool = False) -> str:
    """The two legs of an add/drop. Shared by the queue and the history.

    Add before drop: the move is read as "who am I getting", and the man
    leaving is the cost of it, so he belongs second and dimmer.
    """
    out = ["<div class=swap>"]
    for verb, pid in (("ADD", payload.get("add")), ("DROP", payload.get("drop"))):
        if not pid:
            continue
        name = payload.get("%s_name" % verb.lower()) or "player %s" % pid
        side = "in" if verb == "ADD" else "out"
        tags = ""
        if verb == "ADD" and payload.get("waiver"):
            tags = " <span class='tag hold'>waiver claim</span>"
        badge = "<span class='pip %s'>%s</span>" % (
            side, "+" if verb == "ADD" else "−")
        out.append("<div class='leg %s'>%s%s%s</div>" % (
            side, _face(pid, "sm" if small else "", badge),
            _who(pid, name), tags,
        ))
    out.append("</div>")
    return "".join(out)


def _applied(moves: List[Mapping[str, Any]]) -> str:
    """The lineup changes a run actually made, one row each."""
    out = ["<ul class=did>"]
    for m in moves:
        reason = _move_reason(m)
        out.append(
            "<li>%s<div class=body><div class=top>"
            "%s<span class=slots>%s"
            "<span class=arrow>&rarr;</span><span class=to>%s</span>"
            "</span></div>%s</div></li>" % (
                _face(m.get("player_id")),
                _who(m.get("player_id"),
                     m.get("name") or "player %s" % m.get("player_id")),
                _e(_slot(m.get("from"))), _e(_slot(m.get("to"))),
                ("<div class=note>%s</div>" % _e(reason)) if reason else "",
            )
        )
    out.append("</ul>")
    return "".join(out)


def _moves_of(proposal: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The per-move detail of a lineup write, however old the row is.

    Runs journaled before the payload carried ``moves`` only have the one-line
    rationale ``"Name UTIL->BE; Name BE->UTIL"``, which is enough for the slots
    and the name but not for a headshot. Recovering it is all-or-nothing: a
    single bit that does not parse falls the whole proposal back to the plain
    rationale row, rather than showing a half-read list as if it were complete.
    """
    payload = proposal.get("payload")
    moves = payload.get("moves") if isinstance(payload, Mapping) else None
    if isinstance(moves, list) and moves:
        return [m for m in moves if isinstance(m, Mapping)]
    out: List[Mapping[str, Any]] = []
    for bit in str(proposal.get("rationale") or "").split("; "):
        name, sep, slots = bit.strip().rpartition(" ")
        src, arrow, dst = slots.partition("->")
        if not (name and sep and arrow and src and dst):
            return []
        out.append({"name": name, "from": src, "to": dst})
    return out


def _move_reason(move: Mapping[str, Any]) -> str:
    """The parenthetical from a move's rationale, without the slots again.

    ``pipeline`` writes ``"UTIL -> BE (no game today)"``; the slots are already
    shown as pills beside it, so only the reason is wanted. If the format ever
    changes the whole string is shown rather than nothing.
    """
    why = str(move.get("why") or "")
    head = "%s -> %s (" % (move.get("from", ""), move.get("to", ""))
    if why.startswith(head) and why.endswith(")"):
        return why[len(head):-1]
    return why


def _clock(dt: datetime) -> str:
    """``9:54 am``. Built by hand because ``%-I`` is a glibc extension.

    The add-on runs on musl, where the dash flag is not supported and the
    format string comes back through unexpanded.
    """
    return "%d:%02d %s" % (dt.hour % 12 or 12, dt.minute,
                           "am" if dt.hour < 12 else "pm")


def _when(raw: Any, tz: Any = None, now: Optional[datetime] = None) -> str:
    """A stored timestamp as a person reads it, in the configured zone.

    Runs are recorded in UTC, and ``datetime.isoformat`` keeps microseconds --
    ``2026-10-22T16:54:18.033797+00:00`` for a run the user watched happen at
    breakfast. Anything unparseable is handed back untouched rather than
    hidden: a timestamp that does not fit the format is still evidence.
    """
    text = str(raw)
    try:
        dt = datetime.fromisoformat(text[:-1] + "+00:00"
                                    if text.endswith("Z") else text)
    except (TypeError, ValueError):
        return text
    if dt.tzinfo is None:        # every writer uses UTC; see state.py
        dt = dt.replace(tzinfo=timezone.utc)
    if tz is not None:
        dt = dt.astimezone(tz)
    now = now or datetime.now(dt.tzinfo)
    days = (now.date() - dt.date()).days
    if days == 0:
        return "Today, %s" % _clock(dt)
    if days == 1:
        return "Yesterday, %s" % _clock(dt)
    return "%s %d %s, %s" % (dt.strftime("%a"), dt.day, dt.strftime("%b"),
                             _clock(dt))


def _chunks(text: Any) -> List[str]:
    """Rationale split into paragraphs on blank lines.

    The reviewer's caveat and the pricing detail arrive in one string separated
    by ``\\n\\n``; rendered as a single run of text they read as one
    undifferentiated wall, which is most of why this page looked unfinished.
    """
    return [c.strip() for c in str(text).split("\n\n") if c.strip()]


def _why(text: Any) -> str:
    """The rationale, every paragraph of it, behind one disclosure.

    Nothing of it shows closed -- not a lead line, not the first sentence. It
    is the shadow prices, the games-used arithmetic and the reviewer's caveat,
    and a card has one job above the fold: say what the move is and let it be
    answered. Asked for directly: "ALL the subtext should be hidden in show
    details which includes the calculations and reasoning."
    """
    chunks = _chunks(text)
    if not chunks:
        return ""
    return (
        "<details class=why><summary></summary><div class=detail>%s</div>"
        "</details>" % "".join("<p>%s</p>" % _e(c) for c in chunks)
    )


def _league_columns(page: StatusPage,
                    rows: List[Dict[str, Any]],
                    *, all_leagues: bool = False
                    ) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """``(label, rows)`` per league, in the order the leagues were configured.

    Both lists on this page are grouped the same way, because a reader
    comparing two leagues wants the same league in the same place in both.
    Order comes from the config rather than from the data: both leagues run in
    the same second, so "whichever is newest" could swap the columns between
    two refreshes for no reason a reader could see.

    ``all_leagues`` keeps a column for a league with nothing in it, which is
    the difference between "this one has nothing to approve" and "this one is
    missing from the page".
    """
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    if all_leagues:
        for key in page.league_order():
            groups[key] = []
    for r in rows:
        groups.setdefault(r["league_id"], []).append(r)
    order = [k for k in page.league_order() if k in groups]
    order += [k for k in groups if k not in order]  # a league since removed
    return [(page.league_label(k), groups[k]) for k in order]


def render(page: StatusPage, *, user: str = "") -> str:
    pending = page.pending()
    shadow = page.shadow_mode()
    stuck = page.stuck()
    parts: List[str] = [
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>NBA Autopilot</title><style>%s</style></head><body>"
        "<main class=wrap>" % CSS,
        "<h1 class=solo>NBA Autopilot</h1>",
    ]

    # The chips answer, above the fold, the two questions the page is opened
    # with: is it actually writing to ESPN, and does it want something from me.
    parts.append("<div class='chips solo'>")
    parts.append("<span class='chip hold'>Shadow mode</span>" if shadow
                 else "<span class='chip on'>Live</span>")
    parts.append(
        "<span class=chip>%d waiting on you</span>" % len(pending) if pending
        else "<span class=chip>Nothing waiting</span>"
    )
    parts.append("</div>")

    if shadow:
        parts.append(
            "<div class='card banner solo'><div class=bar></div><div>"
            "<b>Shadow mode</b><div class=meta>"
            "Decisions are recorded and reported, but nothing is sent to ESPN. "
            "Approving below marks the move approved and logs the request it "
            "would have made.</div></div></div>"
        )

    if stuck:
        parts.append(
            "<div class='card banner bad solo'><div class=bar></div><div>"
            "<b>%s in an unknown state</b><div class=meta>"
            "A POST timed out and ESPN may or may not have applied it. Runs are "
            "paused until this is reconciled.</div></div></div>"
            % _plural(len(stuck), "write")
        )

    parts.append("<h2 class=solo>Needs approval%s</h2>"
                 % ("<span class=count>%d</span>" % len(pending) if pending else ""))
    if not pending:
        # Say *why* it is empty. Only add/drop needs a human, and lineup moves
        # are applied without one, so an empty queue right after a notification
        # that said "2 move(s)" reads as a broken page rather than as a working
        # one. Reported from the phone: tapped the notification, found nothing
        # to approve, reasonably assumed something had gone wrong.
        parts.append(
            "<div class='card empty solo'><b>Nothing waiting.</b>"
            "Lineup and IR moves are applied automatically and appear under "
            "Recent runs; only add/drop asks first.</div>"
        )
    # A column per league, same as the runs below and in the same order, so a
    # waiting swap sits above the runs of the league it belongs to. The league
    # name comes off the card's eyebrow with it -- the column heading says it
    # once instead of every card saying it again -- and the kind takes that
    # slot, which is what the eyebrow was actually missing.
    if pending:
        parts.append("<div class=leagues>")
        for label, column in _league_columns(page, pending, all_leagues=True):
            parts.append("<section class=lg><h3 class=lghead>%s</h3>"
                         % _e(label))
            if not column:
                parts.append("<div class='card empty'>"
                             "<b>Nothing waiting.</b></div>")
            for p in column:
                parts.append("<div class=card>")
                parts.append(
                    "<div class=eyebrow><span class=league>%s</span>%s</div>" % (
                        _e(_kind(p["kind"])),
                        ("<span class='delta%s'>%+.1f pts</span>"
                         % ("" if p["value_delta"] > 0 else " down",
                            p["value_delta"]))
                        if p["value_delta"] else "",
                    ))
                parts.append(_swap(p["payload"]))
                if p["rationale"]:
                    parts.append(_why(p["rationale"]))
                parts.append(
                    "<div class=actions>"
                    "<form method=post action=decide>"
                    "<input type=hidden name=action_id value='%s'>"
                    "<input type=hidden name=verb value=APPROVE>"
                    "<button class=ok type=submit>Approve</button></form>"
                    "<form method=post action=decide>"
                    "<input type=hidden name=action_id value='%s'>"
                    "<input type=hidden name=verb value=REJECT>"
                    "<button class=no type=submit>Reject</button></form>"
                    "</div>" % (_e(p["action_id"]), _e(p["action_id"]))
                )
                parts.append("</div>")
            parts.append("</section>")
        parts.append("</div>")

    parts.append("<h2 class=solo>Recent runs</h2>")
    runs = page.recent_runs()
    tz = page.tz()
    if not runs:
        parts.append("<div class='card empty solo'><b>No runs recorded yet.</b>"
                     "The first scheduled run will appear here.</div>")
    else:
        # A column per league rather than one interleaved list. Two leagues run
        # a second apart, so the single column alternated between them and
        # every card had to be read to find out whose it was.
        parts.append("<div class=leagues>")
        for label, column in _league_columns(page, runs):
            parts.append("<section class=lg><h3 class=lghead>%s</h3>"
                         % _e(label))
            _runs(parts, column, tz)
            parts.append("</section>")
        parts.append("</div>")

    if user:
        parts.append("<footer>signed in as %s</footer>" % _e(user))
    parts.append("</main></body></html>")
    return "".join(parts)


def _runs(parts: List[str], runs: List[Dict[str, Any]], tz: Any) -> None:
    """One league's runs, newest first.

    Every run shows what it changed without being opened first. Collapsing the
    whole run was the fix for a body that was a dozen notes long; now that the
    notes are behind their own control the body is a few moves, and hiding
    those made the page's one answer the thing hardest to reach.
    """
    for r in runs:
        # Lineup and IR writes are split out of the proposal list: they are the
        # only ones nobody approved, so they are the only ones the page has to
        # account for move by move. Everything else is a card above, or a row
        # somebody already answered.
        applied, rows = [], []
        for p in r["proposals"]:
            moves = (_moves_of(p)
                     if str(p["kind"]).upper() in ("LINEUP", "IR") else [])
            (applied if moves else rows).append((p, moves))
        nmoves = sum(len(m) for _, m in applied)

        # The note count is not repeated here: the dropdown below carries it,
        # and a run that changed nothing should say so in one line rather than
        # advertise seventeen notes about not doing anything.
        counts = [_plural(nmoves + len(rows), "move")] if nmoves or rows else []
        parts.append("<div class='card run'>")
        # The league is the column heading now, so the run leads with when it
        # ran -- which is what tells two of the same league's runs apart.
        parts.append(
            "<div class=runline><div class=top><span class=name>%s</span>"
            "<span class=tag>period %s</span>%s</div>%s</div>"
            % (_e(_when(r["started_at"], tz)), _e(r["scoring_period"]),
               "<span class='tag hold'>shadow</span>" if r["shadow_mode"] else "",
               ("<div class=meta>%s</div>" % " &middot; ".join(counts))
               if counts else "")
        )
        parts.append("<div class=runbody>")
        for p, moves in applied:
            state = str(p["state"]).upper()
            parts.append(
                "<div class=sechead>%s%s</div>" % (
                    "Set automatically" if state == DONE
                    else _e(_kind(p["kind"])),
                    "" if state == DONE else
                    "<span class='tag %s'>%s</span>" % (
                        STATE_TAGS.get(state, ""), _e(state.lower())),
                )
            )
            parts.append(_applied(moves))
        if rows:
            parts.append("<ul class=props>")
            for p, _m in rows:
                payload = p["payload"] if isinstance(p["payload"], Mapping) else {}
                swap = (_swap(payload, small=True)
                        if payload.get("add") or payload.get("drop") else "")
                parts.append(
                    "<li><div class=head><span class=kind>%s</span>"
                    "<span class='tag %s'>%s</span></div>%s%s</li>" % (
                        _e(_kind(p["kind"])),
                        STATE_TAGS.get(str(p["state"]).upper(), ""),
                        _e(str(p["state"]).lower()),
                        swap,
                        _why(p["rationale"]) if p["rationale"] else "",
                    )
                )
            parts.append("</ul>")
        if not applied and not rows:
            parts.append("<div class='meta empty'>no moves proposed</div>")
        if r["notes"]:
            # Working notes, not results: the games-used arithmetic, the
            # shadow prices, every player it refused to drop and why. Useful
            # when something looks wrong, and noise the rest of the time, so
            # the run opens on what it *did* and keeps the reasoning one tap
            # away.
            parts.append(
                "<details class=logs><summary>Logs (%d)</summary>"
                "<ul class=notes>" % len(r["notes"])
            )
            for n in r["notes"]:
                parts.append("<li>%s</li>" % _e(n))
            parts.append("</ul></details>")
        parts.append("</div></div>")


# -- deciding --------------------------------------------------------------

#: Two short fields of ``a=b&c=d``. The cap exists so an oversized body is
#: refused instead of buffered; the endpoint is reachable only through
#: authenticated ingress, so this is hygiene rather than a defence.
MAX_FORM_BYTES = 4096


async def read_form(request: Any) -> Dict[str, str]:
    """Parse the urlencoded POST body. First value wins; missing keys are absent.

    First rather than last because that is what Starlette's own ``FormData.get``
    returns, and because for a field whose two legal values are APPROVE and
    REJECT, "the earliest thing that was said" is the more conservative reading
    of a body that repeats itself.

    ``request.form()`` cannot be used. Starlette's form parser asserts on
    ``python-multipart`` for *every* content type, not just
    ``multipart/form-data``, because it borrows that package's Content-Type
    parser before it dispatches -- so the assertion fires on a plain
    ``application/x-www-form-urlencoded`` body too. That was found on the
    device as a 500: ``AssertionError: The `python-multipart` library must be
    installed to use form parsing``. Declaring the fields as ``Form(...)``
    would have hit the same wall by a different route.

    Adding the dependency would mean another wheel to resolve for musl and
    another pin to keep current, to parse two fields that ``parse_qs`` -- which
    is stdlib, and is what Starlette would ultimately have called -- handles
    directly.
    """
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_FORM_BYTES:
        log.warning("refusing a %d-byte form body", declared)
        return {}
    raw = await request.body()
    if len(raw) > MAX_FORM_BYTES:  # a chunked body declares no length
        log.warning("refusing a %d-byte form body", len(raw))
        return {}
    parsed = parse_qs(raw.decode("utf-8", "replace"))
    return {k: v[0] for k, v in parsed.items() if v}


NOT_OWNER = (
    "This Home Assistant user is not the configured owner of this app, so it "
    "cannot approve roster moves."
)


async def apply_decision(bot: Any, *, action_id: str, verb: str,
                         user_id: str) -> Optional[str]:
    """Authorize and apply one decision. ``None`` on success, else a refusal.

    Deliberately a module-level function rather than a closure inside
    ``build_app``: this is the security boundary of the page, and FastAPI is not
    installed in the development environment, so anything living inside the app
    factory is untestable there. A check that cannot be tested where it is
    written is a check that will eventually be wrong.

    Ingress has already authenticated the request against Home Assistant -- an
    unauthenticated one never reaches this process. What is decided here is
    *which* authenticated user: other household members must not be able to
    drop a player from this roster. It mirrors the notification-tap check in
    ``ha/bridge.py``, including failing closed when the id is absent.
    """
    owner = bot.cfg.ha_owner_user_id
    if owner and user_id != owner:
        log.warning("refusing decision on %s from user_id=%r (expected owner)",
                    action_id, user_id or None)
        return NOT_OWNER

    approve = str(verb).upper() == "APPROVE"
    decided = bot.store.decide(action_id, approve, user_id or "ingress")
    if decided is None:
        # Already decided, expired, or gone. Same no-op as a stale tap: the
        # redirect re-renders and the proposal simply is not there.
        log.info("ingress decision on %r changed nothing", action_id)
    elif approve:
        await bot.execute_approved()
    return None


# -- routing ---------------------------------------------------------------


def build_app(bot: Any):
    """The FastAPI app. Imported late so the rest of the module stays testable."""
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse

    page = StatusPage(bot)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    seen_headers = {"done": False}

    def identify(request: Request) -> tuple:
        """``(user_id, display_name)`` as ingress reports them.

        Logged once per process at info, because the exact header names are the
        one thing here that is taken from documentation rather than observed,
        and a silent mismatch would make every approval fail closed with no
        clue why.
        """
        uid = request.headers.get(H_USER_ID) or ""
        name = request.headers.get(H_USER_NAME) or ""
        if not seen_headers["done"]:
            seen_headers["done"] = True
            log.info(
                "ingress identity headers present: %s (user resolved: %s)",
                sorted(k for k in request.headers if k.startswith("x-remote-")),
                "yes" if uid else "NO -- approvals from the page will be refused",
            )
        return uid, name

    # Both handlers are `async def` on purpose. A sync handler would be run in
    # Starlette's threadpool, adding more threads contending for the store's
    # single connection; the page's queries are a handful of indexed reads over
    # tens of rows, so serving them on the loop is cheaper than the contention.
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        _, name = identify(request)
        return HTMLResponse(render(page, user=name))

    @app.post("/decide")
    async def decide(request: Request):
        # Neither `Form(...)` nor `request.form()`; see `read_form`. The two
        # fields need no validation beyond "is it the owner" and "does it match
        # a PROPOSED row", both of which happen in `apply_decision`.
        form = await read_form(request)
        uid, _ = identify(request)
        refusal = await apply_decision(
            bot,
            action_id=str(form.get("action_id") or ""),
            verb=str(form.get("verb") or ""),
            user_id=uid,
        )
        if refusal:
            return PlainTextResponse(refusal, status_code=403)
        # 303 so a refresh re-reads the page instead of resubmitting the form,
        # and relative so it resolves inside the ingress prefix.
        return RedirectResponse(".", status_code=303)

    return app


async def serve(bot: Any, port: int) -> None:
    """Run the page until cancelled.

    Bound to ``0.0.0.0`` inside the container and never published on the host:
    ``config.yaml`` declares ``ingress_port`` with no ``ports:`` key, so the
    only route in is through the authenticated Supervisor ingress proxy.
    """
    import uvicorn

    config = uvicorn.Config(
        build_app(bot),
        host="0.0.0.0",
        port=int(port),
        log_level="warning",   # uvicorn's access log would duplicate every poll
        access_log=False,
    )
    server = uvicorn.Server(config)
    # uvicorn installs signal handlers by default, which would fight the ones
    # `Autopilot.serve` installed for graceful shutdown.
    server.install_signal_handlers = lambda: None
    log.info("status page listening on :%d (ingress only)", port)
    await server.serve()
