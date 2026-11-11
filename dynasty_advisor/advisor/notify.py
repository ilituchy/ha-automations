"""Phone notifications through Home Assistant, and the run schedule.

Notifications go through the Supervisor's Core proxy
(``http://supervisor/core/api``) with ``SUPERVISOR_TOKEN`` -- the app needs
``homeassistant_api: true`` and nothing else. Every outbound call is logged by
service name and title before it is sent (porting doc §6.1).

TAP DESTINATION (verified 2026-11-11)
=====================================
An ingress app that declares ``panel_title`` gets a top-level sidebar panel,
and the frontend route is ``/<slug>`` -- NOT ``/hassio/ingress/<slug>``,
which the companion app silently resolves to the home dashboard
(nba_autopilot hit exactly this, see its ha/bridge.py). ``get_panels`` on the
owner's instance reported ``url_path: d0cfb82c_dynasty_advisor`` for this app,
i.e. the slug. The slug is read from ``/addons/self/info`` at runtime, never
hardcoded, so a repository-hash change cannot break the link.

SCHEDULE (measured 2026-11-11)
==============================
Of 274 processed waiver claims in the league's 2026 season, 163 cleared at
00:xx PT going into **Wednesday** and the rest at 00:xx PT on other days
(daily waivers). The default "tue 18:00" therefore lands six hours before the
run that matters. Entries are ``"<day> HH:MM"`` in the container's local time,
which Supervisor sets to the HA timezone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

log = logging.getLogger("advisor.notify")

SUPERVISOR = "http://supervisor"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_ENTRY = re.compile(r"^\s*(mon|tue|wed|thu|fri|sat|sun|daily)\s+([01]?\d|2[0-3]):([0-5]\d)\s*$", re.I)
#: A scheduled time missed by up to this much (add-on restarting, Green busy)
#: still fires; later than that it waits for the next occurrence.
GRACE = timedelta(minutes=20)


def _token() -> Optional[str]:
    return os.environ.get("SUPERVISOR_TOKEN")


def _request(method: str, url: str, body: Optional[dict] = None, timeout: float = 15.0):
    tok = _token()
    if not tok:
        raise RuntimeError("no SUPERVISOR_TOKEN (not running as a Home Assistant app)")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


_LINK: List[Optional[str]] = []


def deep_link() -> Optional[str]:
    """``/<own slug>``, or None if it cannot be resolved (then no tap URL)."""
    if _LINK:
        return _LINK[0]
    link = None
    try:
        info = _request("GET", SUPERVISOR + "/addons/self/info") or {}
        slug = (info.get("data") or {}).get("slug")
        if slug:
            link = "/" + slug
    except Exception as e:  # noqa: BLE001 -- a dead link is survivable
        log.warning("could not resolve own slug for the notification link: %s", e)
    _LINK.append(link)
    return link


def send(service: str, title: str, message: str, *, urgent: bool = False) -> None:
    service = service.split(".", 1)[-1]  # accept "notify.mobile_app_x" or "mobile_app_x"
    if not re.match(r"^[a-z0-9_]+$", service):
        raise ValueError("bad notify service name %r" % service)
    data = {"tag": "dynasty-advisor", "group": "dynasty-advisor"}
    link = deep_link()
    if link:
        data["url"] = link
    if urgent:
        data["push"] = {"interruption-level": "time-sensitive"}
    log.info("notify.%s: %r (%d chars, link=%s, urgent=%s)", service, title, len(message), link, urgent)
    _request("POST", SUPERVISOR + "/core/api/services/notify/" + service,
             {"title": title, "message": message, "data": data})


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def parse_schedule(entries) -> List[Tuple[Optional[int], int, int]]:
    """``["tue 18:00", "daily 09:30"]`` -> ``[(1, 18, 0), (None, 9, 30)]``.
    Bad entries are logged and skipped rather than crashing the app."""
    out = []
    for e in entries or ():
        m = _ENTRY.match(str(e))
        if not m:
            log.warning("ignoring schedule entry %r (expected e.g. 'tue 18:00')", e)
            continue
        day = None if m.group(1).lower() == "daily" else DAYS.index(m.group(1).lower())
        out.append((day, int(m.group(2)), int(m.group(3))))
    return out


def due(entries, now: datetime, fired: set) -> List[str]:
    """Keys of entries whose most recent occurrence is within GRACE of
    ``now`` and has not fired yet."""
    keys = []
    for day, hh, mm in entries:
        occ = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if occ > now:
            occ -= timedelta(days=1)
        if day is not None and occ.weekday() != day:
            continue
        if now - occ > GRACE:
            continue
        key = "%s@%s" % (occ.strftime("%Y-%m-%d %H:%M"), "daily" if day is None else DAYS[day])
        if key not in fired:
            keys.append(key)
    return keys


def describe(entries) -> str:
    def clock(h, m):
        return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")
    return ", ".join("%s %s" % ("Daily" if d is None else DAYS[d].title(), clock(h, m)) for d, h, m in entries)


class Scheduler:
    def __init__(self, state_path: str, get_entries: Callable[[], list], fire: Callable[[str], None]) -> None:
        self.state_path = state_path
        self.get_entries = get_entries
        self.fire = fire
        self.fired = self._load()

    def _load(self) -> set:
        try:
            with open(self.state_path) as f:
                return set(json.load(f))
        except (OSError, ValueError):
            return set()

    def _save(self) -> None:
        keep = sorted(self.fired)[-50:]
        with open(self.state_path + ".tmp", "w") as f:
            json.dump(keep, f)
        os.replace(self.state_path + ".tmp", self.state_path)

    def tick(self, now: Optional[datetime] = None) -> List[str]:
        keys = due(parse_schedule(self.get_entries()), now or datetime.now(), self.fired)
        for k in keys:
            self.fired.add(k)
            self._save()
            log.info("scheduled run %s", k)
            self.fire(k)
        return keys

    def start(self) -> None:
        def loop():
            while True:
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 -- the loop must survive
                    log.exception("scheduler tick failed")
                time.sleep(30)
        threading.Thread(target=loop, daemon=True, name="scheduler").start()
