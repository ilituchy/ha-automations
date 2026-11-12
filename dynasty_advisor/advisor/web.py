"""The Home Assistant page: run the advisor on demand and read its reports.

``python3 -m advisor.web`` -- serves on ``ADVISOR_PORT`` (default 8099).

Served through Supervisor ingress, which puts it in the HA sidebar behind HA's
own login. Lessons carried over from nba_autopilot/rootfs/app/web.py, each of
which cost that project a debugging session:

* **Every URL is relative.** Ingress serves the app under
  ``/api/hassio_ingress/<rotating-token>/``; an absolute ``/run`` would escape
  the prefix and 404 against the HA frontend.
* **Everything interpolated is escaped.** Reports carry player and manager
  names from Sleeper and FantasyCalc -- untrusted text, rendered inside the
  authenticated HA frontend.
* **Inline CSS, no theme variables.** Ingress renders in an iframe and CSS
  custom properties do not cross the document boundary; dark mode rides on
  ``prefers-color-scheme``, which the iframe does inherit.
* **Show that something is happening.** Inside the ingress iframe the
  browser's loading indicator is invisible, and a run takes 20-60 s on the
  Green. The page shows a spinner and refreshes itself until the run ends.
* **Only the ingress proxy may connect** (``172.30.32.2``) when
  ``ADVISOR_INGRESS_ONLY=1``, so the port is useless even if published.

Running is a plain form POST, a 303 back to the page, and a meta refresh while
a run is in flight. The only script opens player cards in a dialog
(``cards.JS``); every player name is also a real link to ``player?id=...``, so
the page works without it.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import cards, notify, page
from .http import CachedHttp
from .run import RunError, run_once
from .trades import STRATEGIES

log = logging.getLogger("advisor.web")

DATA = os.environ.get("ADVISOR_DATA", os.path.expanduser("~/.local/share/dynasty_advisor"))
OPTIONS = os.environ.get("ADVISOR_OPTIONS", os.path.expanduser("~/.config/dynasty_advisor/config.json"))
INGRESS_ONLY = os.environ.get("ADVISOR_INGRESS_ONLY") == "1"
INGRESS_PROXY = {"172.30.32.2"}
REPORT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d{6})?\.md$")
STRATEGY_CHOICES = ["auto"] + sorted(STRATEGIES)
STRATEGY_HELP = {
    "auto": "follow my team's position",
    "contend": "win now",
    "balanced": "even weight",
    "rebuild": "youth and picks",
}


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def load_options() -> Dict:
    try:
        with open(OPTIONS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Run state: one run at a time, in a background thread.
# ---------------------------------------------------------------------------


class Runner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.error: Optional[str] = None
        self.last: Optional[str] = None
        self.summary: Optional[str] = None
        self.notified: Optional[str] = None

    def start(self, strategy: str, *, send: bool = False, why: str = "manual") -> bool:
        with self.lock:
            if self.running:
                log.info("run requested (%s) while one is in progress; ignored", why)
                return False
            self.running = True
            self.started = time.time()
            self.error = None
        threading.Thread(target=self._run, args=(strategy, send, why), daemon=True).start()
        return True

    def _notify(self, opts: Dict, title: str, message: str, urgent: bool = False) -> None:
        service = (opts.get("notify_service") or "").strip()
        if not service:
            log.info("no notify_service configured; not notifying")
            return
        try:
            notify.send(service, title, message, urgent=urgent)
            self.notified = "Sent to %s" % service
        except Exception as e:  # noqa: BLE001 -- the report still exists; say so on the page
            self.notified = None
            self.error = (self.error + " · " if self.error else "") + "Notification failed: %s" % e
            log.error("notification failed: %s", e)

    def _run(self, strategy: str, send: bool, why: str) -> None:
        opts = load_options()
        self.notified = None
        try:
            username = (opts.get("username") or "").strip()
            if not username:
                raise RunError("Set your Sleeper username in the app's Configuration tab.")
            league = str(opts.get("league_id") or "").strip() or None
            log.info("run: strategy=%s league=%s", strategy, "set" if league else "auto-detect")
            res = run_once(username, league, strategy, DATA)
            self.last = os.path.basename(res.report_path)
            self.summary = "%s strategy · %d API calls · %d trade packages checked · %.0fs" % (
                res.strategy, res.calls, res.evaluated, res.seconds)
            log.info("run done (%s): %s (%s)", why, self.last, self.summary)
            if send:
                self._notify(opts, res.title, res.message, res.urgent)
        except RunError as e:
            self.error = str(e)
            log.warning("run refused: %s", e)
            if send:
                self._notify(opts, "Dynasty Advisor: run didn't happen", str(e))
        except Exception as e:  # noqa: BLE001 -- surfaced on the page, full trace in the log
            self.error = "Run failed: %s: %s" % (type(e).__name__, e)
            log.error("run failed:\n%s", traceback.format_exc())
            if send:
                self._notify(opts, "Dynasty Advisor: run failed", self.error)
        finally:
            with self.lock:
                self.running = False
                self.finished = time.time()


RUNNER = Runner()
INDEX = cards.Index(DATA)
_CARD_HTTP: List[CachedHttp] = []


def card_view(pid: str) -> Dict:
    if not _CARD_HTTP:
        _CARD_HTTP.append(CachedHttp(os.path.join(DATA, "cache")))
    try:
        return cards.build_card(INDEX.get(), pid, _CARD_HTTP[0])
    except Exception as e:  # noqa: BLE001 -- one bad card must not take down the page
        log.error("card %s failed:\n%s", pid, traceback.format_exc())
        return {"error": "Could not build this card: %s" % type(e).__name__}


def _doc(title: str, body: str, refresh: str = "") -> str:
    return """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=referrer content=no-referrer>%s<title>%s</title><style>%s%s</style></head>
<body><main>%s</main>%s%s</body></html>""" % (refresh, _e(title), page.CSS, cards.CSS, body, cards.SHELL, cards.JS)


def render_player(pid: str, frag: bool) -> str:
    v = card_view(pid)
    card = cards.card_html(v)
    if frag:
        return card
    return _doc("%s · Dynasty Advisor" % (v.get("name") or "Player"),
                "<div class=ppage><a class=back href='./'>‹ Dynasty Advisor</a>%s</div>" % card)


def list_reports(limit: int = 20) -> List[str]:
    d = os.path.join(DATA, "reports")
    try:
        names = [n for n in os.listdir(d) if REPORT_RE.match(n)]
    except OSError:
        return []
    # Old-format names (YYYY-MM-DD.md) sort as midnight of that day.
    return sorted(names, key=lambda n: n[:-3] if len(n) > 13 else n[:-3] + "-000000", reverse=True)[:limit]


def _clock(dt: datetime) -> str:
    # No "%-I": that flag is a glibc extension and the add-on runs on musl.
    return "%d:%02d %s" % (dt.hour % 12 or 12, dt.minute, "AM" if dt.hour < 12 else "PM")


def report_label(name: str) -> str:
    stem = name[:-3]
    for fmt in ("%Y-%m-%d-%H%M%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(stem, fmt)
        except ValueError:
            continue
        day = "%s %d" % (dt.strftime("%a %b"), dt.day)
        return day if fmt == "%Y-%m-%d" else "%s, %s" % (day, _clock(dt))
    return stem


# ---------------------------------------------------------------------------
# Markdown -> HTML for the report's own, known subset. Escape first, then
# apply the few inline rules, so nothing in the text can become markup.
# ---------------------------------------------------------------------------

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> str:
    t = _e(text)
    t = _BOLD.sub(r"<strong>\1</strong>", t)
    return t


def md_to_html(md: str) -> str:
    out: List[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("### "):
            out.append("<h3>%s</h3>" % _inline(line[4:]))
        elif line.startswith("## "):
            out.append("<h2>%s</h2>" % _inline(line[3:]))
        elif line.startswith("# "):
            out.append("<h1>%s</h1>" % _inline(line[2:]))
        elif line.strip() == "---":
            out.append("<hr>")
        elif line.startswith("> "):
            buf = []
            while i < len(lines) and lines[i].startswith("> "):
                buf.append(_inline(lines[i][2:]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % "<br>".join(buf))
            continue
        elif line.startswith("- "):
            buf = []
            while i < len(lines) and lines[i].startswith("- "):
                buf.append("<li>%s</li>" % _inline(lines[i][2:]))
                i += 1
            out.append("<ul>%s</ul>" % "".join(buf))
            continue
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            if len(rows) >= 2 and all(set(c) <= set("-: ") for c in rows[1]):
                head, body = rows[0], rows[2:]
            else:
                head, body = None, rows
            t = ["<div class=scroll><table>"]
            if head:
                t.append("<thead><tr>%s</tr></thead>" % "".join("<th>%s</th>" % _inline(c) for c in head))
            t.append("<tbody>%s</tbody>" % "".join(
                "<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in r) for r in body))
            t.append("</table></div>")
            out.append("".join(t))
            continue
        else:
            buf = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#|>|- |\||---)", lines[i]):
                buf.append(_inline(lines[i]))
                i += 1
            out.append("<p>%s</p>" % "<br>".join(buf))
            continue
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def _load_view(name: str) -> Optional[Dict]:
    try:
        with open(os.path.join(DATA, "reports", name[:-3] + ".json")) as f:
            v = json.load(f)
        return v if isinstance(v, dict) and v.get("version") == 1 else None
    except (OSError, ValueError):
        return None


def render_page(selected: Optional[str]) -> str:
    opts = load_options()
    reports = list_reports()
    name = selected if selected in reports else (reports[0] if reports else None)
    body, sub = "", "Sleeper dynasty trade & roster advisor"
    if name:
        view = _load_view(name)
        if view is not None:
            body = page.render_view(view)
            sub = "%s · week %s · %s" % (view.get("league"), view.get("week"), report_label(name))
        else:  # reports from before structured output: render the Markdown
            try:
                with open(os.path.join(DATA, "reports", name)) as f:
                    body = "<div class='card trade legacy'>%s</div>" % md_to_html(f.read())
                sub = report_label(name)
            except OSError:
                body = "<div class=card>Could not read that report.</div>"
    r = RUNNER
    refresh = "<meta http-equiv=refresh content=3>" if r.running else ""
    default = opts.get("strategy") or "auto"
    select = "".join(
        "<option value='%s'%s>%s — %s</option>" % (
            _e(s), " selected" if s == default else "", _e(s.title()), _e(STRATEGY_HELP.get(s, "")))
        for s in STRATEGY_CHOICES)
    if r.running:
        button = "<button disabled><span class=spin></span>Running… %ds</button>" % int(time.time() - (r.started or time.time()))
    else:
        button = "<button type=submit>Run now</button>"
    status = []
    if r.running:
        status.append("Pulling your league, four seasons of trades and today's values — usually under a minute.")
    elif r.summary and r.finished:
        status.append("Last run %s · %s%s" % (_clock(datetime.fromtimestamp(r.finished)), r.summary,
                                              " · " + r.notified if r.notified else ""))
    sched = notify.parse_schedule(opts.get("schedule") or [])
    svc = (opts.get("notify_service") or "").strip()
    status.append(("Scheduled %s%s" % (notify.describe(sched), " → %s" % svc if svc else " (no phone notifications)"))
                  if sched else "No scheduled runs")
    check = ("<label class=check><input type=checkbox name=notify value=1%s> Notify my phone</label>" % (" disabled" if r.running else "")
             if svc else "")
    err = ""
    if r.error and not r.running:
        err += "<div class='banner err'><span aria-hidden=true>⚠︎</span><div>%s</div></div>" % _e(r.error)
    if not opts.get("username"):
        err += "<div class='banner err'><span aria-hidden=true>⚠︎</span><div>No Sleeper username set. Open Settings → Apps → Dynasty Advisor → Configuration.</div></div>"
    hist = ""
    if len(reports) > 1:
        hist = "<section><div class=sec-head><h2>Past runs</h2></div><div class=history>%s</div></section>" % "".join(
            "<a href='?r=%s'%s>%s</a>" % (_e(n), " class=cur" if n == name else "", _e(report_label(n))) for n in reports)
    if not name:
        body = "<div class='card trade'><p>No reports yet. Press <b>Run now</b>.</p></div>"
    return _doc("Dynasty Advisor", """
<header class=head><div class=brand><h1>Dynasty Advisor</h1><div class=sub>%s</div></div>
<form class=run method=post action=run><select name=strategy aria-label=Strategy%s>%s</select>%s%s</form></header>
<div class=status>%s</div>
%s%s%s""" % (_e(sub), " disabled" if r.running else "", select, check, button,
             "".join("<span>%s</span>" % _e(x) for x in status), err, body, hist), refresh)


class Handler(BaseHTTPRequestHandler):
    server_version = "DynastyAdvisor/0.1"

    def log_message(self, fmt, *args):  # route through logging, not stderr
        log.debug("%s %s", self.client_address[0], fmt % args)

    def _allowed(self) -> bool:
        if INGRESS_ONLY and self.client_address[0] not in INGRESS_PROXY:
            self.send_error(403)
            return False
        return True

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._allowed():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.rstrip("/").endswith("/player") or u.path == "player":
            pid = (q.get("id") or [""])[0]
            return self._send(200, render_player(pid, (q.get("frag") or [""])[0] == "1"))
        if u.path.rstrip("/") not in ("", "/index.html"):
            if u.path.endswith("/health"):
                return self._send(200, "ok", "text/plain")
            return self.send_error(404)
        sel = (q.get("r") or [None])[0]
        if sel and not REPORT_RE.match(sel):
            sel = None
        self._send(200, render_page(sel))

    def do_POST(self):
        if not self._allowed():
            return
        if not urlparse(self.path).path.rstrip("/").endswith("run"):
            return self.send_error(404)
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        strategy = (form.get("strategy") or ["auto"])[0]
        if strategy not in STRATEGY_CHOICES:
            strategy = "auto"
        RUNNER.start(strategy, send=(form.get("notify") or [""])[0] == "1", why="manual")
        self.send_response(303)
        self.send_header("Location", "./")  # relative: stays inside the ingress prefix
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    logging.basicConfig(level=os.environ.get("ADVISOR_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("ADVISOR_PORT", "8099"))
    host = os.environ.get("ADVISOR_HOST", "0.0.0.0" if INGRESS_ONLY else "127.0.0.1")
    os.makedirs(DATA, exist_ok=True)
    log.info("dynasty advisor page on %s:%d (ingress only: %s, data: %s)", host, port, INGRESS_ONLY, DATA)
    if os.environ.get("ADVISOR_SCHEDULE", "1") == "1":
        def fire(key: str) -> None:
            opts = load_options()
            strategy = opts.get("strategy") or "auto"
            RUNNER.start(strategy if strategy in STRATEGY_CHOICES else "auto", send=True, why="scheduled " + key)
        sched = notify.Scheduler(os.path.join(DATA, "schedule_state.json"),
                                 lambda: load_options().get("schedule") or [], fire)
        log.info("schedule: %s", notify.describe(notify.parse_schedule(load_options().get("schedule") or [])) or "none")
        sched.start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
