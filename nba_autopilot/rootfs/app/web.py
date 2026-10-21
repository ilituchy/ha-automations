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
from typing import Any, Dict, List, Mapping, Optional
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
            out.append({
                "action_id": p.action_id,
                "kind": p.kind,
                "league": self.league_label(self._league_of_run(p.run_id)),
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

    def stuck(self) -> List[Mapping[str, Any]]:
        return self.bot.store.stuck_writes()

    def shadow_mode(self) -> bool:
        return bool(self.bot.cfg.shadow_mode)


# -- rendering -------------------------------------------------------------
#
# Hand-written HTML with inline CSS, deliberately. A template engine and a
# static-file mount are two more things to get wrong inside an ingress prefix,
# and this is one page. The CSS variables mirror the HA frontend's own so the
# page does not look foreign in the sidebar, and so dark mode works without a
# media query.

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px;
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--primary-background-color, #f5f5f5);
  color: var(--primary-text-color, #212121);
}
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em;
     opacity: .6; margin: 24px 0 8px; font-weight: 600; }
.sub { opacity: .6; font-size: 13px; margin: 0 0 20px; }
.card {
  background: var(--card-background-color, #fff);
  border-radius: 12px; padding: 14px 16px; margin-bottom: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,.12);
}
.banner { border-left: 4px solid #ff9800; }
.banner.bad { border-left-color: #f44336; }
.move { font-size: 16px; font-weight: 600; margin-bottom: 2px; }
.move .verb { display: inline-block; min-width: 46px; font-size: 12px;
              letter-spacing: .06em; opacity: .55; }
.why { opacity: .75; font-size: 14px; margin: 8px 0 0; }
.meta { opacity: .55; font-size: 12px; margin-top: 6px; }
.row { display: flex; gap: 10px; margin-top: 14px; }
button {
  flex: 1; padding: 11px 0; font-size: 15px; font-weight: 600;
  border: 0; border-radius: 9px; cursor: pointer; color: #fff;
}
button.ok { background: #2e7d32; }
button.no { background: #6c6c6c; }
button:active { opacity: .75; }
ul { margin: 6px 0 0; padding-left: 18px; }
li { margin: 2px 0; }
.state { font-size: 11px; padding: 1px 6px; border-radius: 5px;
         background: rgba(127,127,127,.18); margin-left: 6px; }
.empty { opacity: .5; font-style: italic; }
form { margin: 0; }
"""


def render(page: StatusPage, *, user: str = "") -> str:
    pending = page.pending()
    parts: List[str] = [
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>NBA Autopilot</title><style>%s</style></head><body>" % CSS,
        "<h1>NBA Autopilot</h1>",
    ]

    if page.shadow_mode():
        parts.append(
            "<div class='card banner'><b>Shadow mode</b><div class=meta>"
            "Decisions are recorded and reported, but nothing is sent to ESPN. "
            "Approving below marks the move approved and logs the request it "
            "would have made.</div></div>"
        )

    stuck = page.stuck()
    if stuck:
        parts.append(
            "<div class='card banner bad'><b>%d write(s) in an unknown state</b>"
            "<div class=meta>A POST timed out and ESPN may or may not have "
            "applied it. Runs are paused until this is reconciled.</div></div>"
            % len(stuck)
        )

    parts.append("<h2>Needs approval</h2>")
    if not pending:
        parts.append("<div class='card empty'>Nothing waiting.</div>")
    for p in pending:
        parts.append("<div class=card>")
        parts.append("<div class=meta>%s</div>" % _e(p["league"]))
        for verb, pid in (("DROP", p["payload"].get("drop")),
                          ("ADD", p["payload"].get("add"))):
            if pid:
                name = p["payload"].get("%s_name" % verb.lower()) or "player %s" % pid
                suffix = ""
                if verb == "ADD" and p["payload"].get("waiver"):
                    suffix = " <span class=state>waiver claim</span>"
                parts.append("<div class=move><span class=verb>%s</span>%s%s</div>"
                             % (verb, _e(name), suffix))
        if p["rationale"]:
            parts.append("<p class=why>%s</p>" % _e(p["rationale"]))
        if p["value_delta"]:
            parts.append("<div class=meta>projected value %+.2f</div>"
                         % p["value_delta"])
        parts.append(
            "<div class=row>"
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

    parts.append("<h2>Recent runs</h2>")
    runs = page.recent_runs()
    if not runs:
        parts.append("<div class='card empty'>No runs recorded yet.</div>")
    for r in runs:
        parts.append("<div class=card>")
        parts.append("<div class=move>%s<span class=state>period %s</span>%s</div>"
                     % (_e(r["league"]), _e(r["scoring_period"]),
                        "<span class=state>shadow</span>" if r["shadow_mode"] else ""))
        parts.append("<div class=meta>%s</div>" % _e(r["started_at"]))
        if r["proposals"]:
            parts.append("<ul>")
            for p in r["proposals"]:
                parts.append("<li>%s<span class=state>%s</span>%s</li>" % (
                    _e(p["kind"]),
                    _e(p["state"]),
                    ("<div class=meta>%s</div>" % _e(p["rationale"]))
                    if p["rationale"] else "",
                ))
            parts.append("</ul>")
        else:
            parts.append("<div class='meta empty'>no moves proposed</div>")
        if r["notes"]:
            parts.append("<ul>")
            for n in r["notes"]:
                parts.append("<li class=meta>%s</li>" % _e(n))
            parts.append("</ul>")
        parts.append("</div>")

    if user:
        parts.append("<div class=meta style='margin-top:20px'>signed in as %s</div>"
                     % _e(user))
    parts.append("</body></html>")
    return "".join(parts)


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
