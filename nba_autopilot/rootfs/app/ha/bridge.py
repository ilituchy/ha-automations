"""Home Assistant bridge: notifications, todo list, and inbound action taps.

Everything goes through the Supervisor proxy at ``http://supervisor/core/api``
authenticated with ``SUPERVISOR_TOKEN``, so the app never needs a long-lived
access token and the user never pastes one.

The asymmetry that shapes this module
-------------------------------------
Sending a push needs nothing from the user's network: Core talks outbound to
Apple's APNs via the mobile_app integration. But the **tap coming back** is a
webhook into HA, so it requires HA to be externally reachable. If it is not,
the notification arrives, the user taps Approve, and nothing happens -- the
worst failure mode available, because it is silent and it happens precisely
when the user is away from home. This app assumes Nabu Casa / HA Cloud, and
:meth:`check_external_url` verifies it at startup rather than discovering it on
the first approval.

Action ids are globally registered
----------------------------------
HA registers notification actions globally rather than per-notification, so a
bare ``APPROVE`` action id means yesterday's notification -- still sitting in
the notification shade -- will fire today's automation. Every action id this
module emits therefore embeds the run id, and ``state.decide`` re-checks it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Iterable, List, Mapping, Optional, Sequence

import httpx

log = logging.getLogger(__name__)

SUPERVISOR_CORE = "http://supervisor/core/api"
SUPERVISOR_WS = "ws://supervisor/core/websocket"

#: 6 hours, matching the proposal TTL in state.py. iOS dismisses the
#: notification when it elapses, so a stale Approve button stops being tappable
#: at roughly the same moment the proposal stops being valid.
NOTIFICATION_TIMEOUT = 21600


#: Tap verbs. These are the only four strings that may terminate an action id.
APPROVE = "APPROVE"
REJECT = "REJECT"
APPROVE_ALL = "APPROVE_ALL"
REJECT_ALL = "REJECT_ALL"

_ITEM_VERBS = (APPROVE, REJECT)
_BULK_VERBS = (APPROVE_ALL, REJECT_ALL)


@dataclass(frozen=True)
class ParsedAction:
    """A structurally valid tap.

    ``proposal_action_id`` is the id as ``state.py`` stores it -- the verb
    stripped off. That distinction is the whole point of this type: the verb
    belongs to the button, not to the proposal, and conflating them means the
    database lookup silently matches nothing.
    """

    run_id: str
    verb: str
    proposal_action_id: Optional[str]  # None for bulk taps

    @property
    def approve(self) -> bool:
        return self.verb in (APPROVE, APPROVE_ALL)

    @property
    def is_bulk(self) -> bool:
        return self.proposal_action_id is None


def item_action_id(proposal_action_id: str, verb: str) -> str:
    """``<run>:<kind>:<seq>`` + verb. The only place item ids are built."""
    if verb not in _ITEM_VERBS:
        raise ValueError("bad item verb %r" % verb)
    return "%s:%s" % (proposal_action_id, verb)


def bulk_action_id(run_id: str, verb: str) -> str:
    if verb not in _BULK_VERBS:
        raise ValueError("bad bulk verb %r" % verb)
    return "%s:%s" % (run_id, verb)


def parse_action_id(action_id: str) -> Optional[ParsedAction]:
    """Inverse of the two builders. ``None`` if the id is not ours.

    Shape is enforced rather than assumed. HA registers notification actions
    globally and delivers them to every listener, so this function sees ids
    belonging to unrelated automations and must reject them without guessing.
    """
    parts = str(action_id).split(":")
    verb = parts[-1]

    if verb in _BULK_VERBS:
        # <run_id>:<VERB>
        if len(parts) != 2 or not parts[0]:
            return None
        return ParsedAction(run_id=parts[0], verb=verb, proposal_action_id=None)

    if verb in _ITEM_VERBS:
        # <run_id>:<kind>:<seq>:<VERB>
        if len(parts) != 4 or not parts[0] or not parts[1]:
            return None
        if not parts[2].isdigit():
            return None
        return ParsedAction(
            run_id=parts[0],
            verb=verb,
            proposal_action_id=":".join(parts[:3]),
        )

    return None


@dataclass(frozen=True)
class Action:
    action_id: str
    title: str
    destructive: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"action": self.action_id, "title": self.title}
        if self.destructive:
            # iOS-only; renders the button in red.
            d["destructive"] = True
        return d


@dataclass(frozen=True)
class ActionEvent:
    action_id: str
    user_id: Optional[str]
    raw: Mapping[str, Any]
    parsed: Optional[ParsedAction] = None


class HaBridge:
    def __init__(
        self,
        *,
        notify_device: str,
        owner_user_id: str,
        todo_entity: str,
        token: Optional[str] = None,
        base_url: str = SUPERVISOR_CORE,
        ws_url: str = SUPERVISOR_WS,
    ) -> None:
        self.notify_device = notify_device
        self.owner_user_id = owner_user_id
        self.todo_entity = todo_entity
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.base_url = base_url
        self.ws_url = ws_url
        if not self.token:
            log.error("SUPERVISOR_TOKEN is unset; HA calls will fail")
        self.http = httpx.Client(
            timeout=15.0,
            headers={
                "Authorization": "Bearer %s" % self.token,
                "Content-Type": "application/json",
            },
        )

    # -- core REST ---------------------------------------------------------

    def call_service(
        self, domain: str, service: str, data: Mapping[str, Any]
    ) -> Any:
        r = self.http.post(
            "%s/services/%s/%s" % (self.base_url, domain, service),
            content=json.dumps(data).encode("utf-8"),
        )
        if r.status_code >= 400:
            raise RuntimeError(
                "HA %s.%s failed %d: %s"
                % (domain, service, r.status_code, r.text[:300])
            )
        try:
            return r.json()
        except ValueError:
            return None

    def check_external_url(self) -> Optional[str]:
        """Confirm HA is reachable from outside before relying on taps.

        Returns a usable external URL, or ``None`` with a loud log line. A
        local-only install still receives notifications but can never receive an
        approval, so this is a startup-time check, not a runtime surprise.

        The presence of the ``cloud`` component proves nothing. Verified against
        a live Nabu Casa instance: ``external_url`` is ``None`` and ``cloud`` is
        in ``components`` whether or not the remote connection is actually up or
        the subscription is current. Only ``cloud/status`` distinguishes
        "approvals will work" from "approvals will silently do nothing", so this
        asks for the connection state rather than inferring it from a feature
        list.
        """
        try:
            r = self.http.get("%s/config" % self.base_url)
            cfg = r.json() if r.status_code < 400 else {}
        except Exception as exc:  # noqa: BLE001 - startup diagnostic only
            log.error("could not read HA config: %s", exc)
            return None

        ext = cfg.get("external_url")
        if ext:
            # An explicit external_url is the user's own statement of intent;
            # take it over anything inferred.
            return ext

        if "cloud" not in (cfg.get("components") or []):
            log.error(
                "HA reports no external_url and no cloud component. Push "
                "notifications will arrive but APPROVAL TAPS WILL SILENTLY DO "
                "NOTHING. Enable Nabu Casa / HA Cloud or set an external URL."
            )
            return None

        status = self.cloud_status()
        if status is None:
            log.error(
                "The cloud component is loaded but cloud/status did not answer. "
                "Treating remote access as unavailable rather than assuming it "
                "works, because assuming it works fails silently at approval."
            )
            return None

        if not status.get("logged_in"):
            log.error("Not signed in to HA Cloud. Approval taps will not arrive.")
            return None
        if not status.get("active_subscription"):
            log.error(
                "HA Cloud subscription is not active. Remote access is down, so "
                "approval taps will not arrive."
            )
            return None
        if not status.get("remote_connected"):
            log.error(
                "HA Cloud remote access is not connected (certificate status: "
                "%s). Approval taps will not arrive until it reconnects. Enable "
                "Remote Control in Settings -> Home Assistant Cloud.",
                status.get("remote_certificate_status"),
            )
            return None

        domain = status.get("remote_domain")
        if not domain:
            log.error("HA Cloud reports connected but exposes no remote domain.")
            return None
        return "https://%s" % domain

    def cloud_status(self) -> Optional[Dict[str, Any]]:
        """One-shot ``cloud/status``. Returns ``None`` on any failure.

        Synchronous on purpose: this runs from ``startup_checks`` before the
        event loop exists, so it owns a short-lived loop of its own.
        """
        try:
            return asyncio.run(self._ws_command({"type": "cloud/status"}))
        except Exception as exc:  # noqa: BLE001 - startup diagnostic only
            log.warning("cloud/status failed: %s", exc)
            return None

    async def _ws_command(self, msg: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Send one command on a throwaway socket and return its result."""
        import websockets  # imported late so the rest of the module is testable

        async with websockets.connect(self.ws_url, max_size=8 * 1024 * 1024) as ws:
            await self._ws_handshake(ws)
            await ws.send(json.dumps(dict(msg, id=1)))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") != 1 or reply.get("type") != "result":
                    continue
                if not reply.get("success"):
                    log.warning("HA ws command %s failed: %s",
                                msg.get("type"), reply.get("error"))
                    return None
                return reply.get("result")

    # -- outbound notification --------------------------------------------

    def notify(
        self,
        title: str,
        message: str,
        *,
        actions: Sequence[Action] = (),
        tag: str,
        time_sensitive: bool = True,
        group: str = "fantasy-autopilot",
    ) -> None:
        """Send an actionable push.

        ``tag`` makes a later notification *replace* this one rather than stack,
        which is how the "executed" confirmation lands on top of the "approve?"
        prompt instead of leaving a dead button in the shade.
        """
        data: Dict[str, Any] = {
            "tag": tag,
            "group": group,
            "url": "/lovelace/fantasy",
        }
        if actions:
            data["actions"] = [a.to_dict() for a in actions]
            data["timeout"] = NOTIFICATION_TIMEOUT
        if time_sensitive:
            # iOS: deliver through Focus modes. This is the one notification
            # class where being an hour late means the lineup already locked.
            data["push"] = {"interruption-level": "time-sensitive"}
        self.call_service(
            "notify",
            self.notify_device,
            {"title": title, "message": message, "data": data},
        )

    def confirm(self, tag: str, title: str, message: str) -> None:
        """Replace an actionable notification with a non-actionable result."""
        self.notify(tag=tag, title=title, message=message, actions=(),
                    time_sensitive=False)

    def persistent_notification(self, title: str, message: str) -> None:
        """Durable archive in the HA sidebar; survives the push being dismissed."""
        self.call_service(
            "persistent_notification",
            "create",
            {"title": title, "message": message, "notification_id": "fantasy_autopilot"},
        )

    # -- todo list as the per-item review surface -------------------------

    def todo_replace(self, items: Sequence[Mapping[str, str]]) -> None:
        """Rewrite the review list.

        The ``todo`` integration gives per-item approval for free: native
        check-off UI on mobile and ``todo.item_completed`` is a real automation
        trigger. That beats a custom frontend, and it sidesteps the push-action
        button cap entirely -- the notification only needs three coarse buttons
        because the fine-grained decisions live here.
        """
        try:
            existing = self.call_service(
                "todo",
                "get_items",
                {"entity_id": self.todo_entity},
            ) or {}
        except RuntimeError as exc:
            log.warning("todo.get_items failed (%s); list may be missing", exc)
            existing = {}

        for item in _extract_todo_items(existing, self.todo_entity):
            try:
                self.call_service(
                    "todo",
                    "remove_item",
                    {"entity_id": self.todo_entity, "item": item},
                )
            except RuntimeError as exc:
                log.debug("could not remove todo item %r: %s", item, exc)

        for it in items:
            self.call_service(
                "todo",
                "add_item",
                {
                    "entity_id": self.todo_entity,
                    "item": it["summary"],
                    "description": it.get("description", ""),
                },
            )

    # -- inbound action taps (WebSocket) ----------------------------------

    async def listen_actions(
        self, queue: "asyncio.Queue[ActionEvent]", *, stop: Optional[asyncio.Event] = None
    ) -> None:
        """Subscribe to ``mobile_app_notification_action`` and enqueue taps.

        Reconnects with backoff: this is a long-lived subscription and a
        dropped socket means approvals stop working silently, which is the one
        thing this module exists to prevent.
        """
        import websockets  # imported late so the rest of the module is testable

        backoff = 1.0
        while not (stop and stop.is_set()):
            try:
                async with websockets.connect(self.ws_url, max_size=8 * 1024 * 1024) as ws:
                    await self._ws_handshake(ws)
                    await ws.send(
                        json.dumps(
                            {
                                "id": 2,
                                "type": "subscribe_events",
                                "event_type": "mobile_app_notification_action",
                            }
                        )
                    )
                    log.info("subscribed to mobile_app_notification_action")
                    backoff = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "event":
                            continue
                        ev = self._parse_action_event(msg)
                        if ev is not None:
                            await queue.put(ev)
            except Exception as exc:  # noqa: BLE001 - must never die
                log.warning("action listener dropped (%s); reconnecting in %.0fs",
                            exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _ws_handshake(self, ws: Any) -> None:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "auth_ok":
            raise RuntimeError("HA websocket auth failed: %r" % reply)

    def _parse_action_event(self, msg: Mapping[str, Any]) -> Optional[ActionEvent]:
        """Validate and authorize a tap.

        Two checks, both mandatory. The action id must look like ours (the
        run-id prefix), and ``context.user_id`` must be the configured owner --
        otherwise any HA user on the instance could approve a roster move.
        An event failing either check is logged and dropped, never processed.
        """
        event = msg.get("event") or {}
        data = event.get("data") or {}
        action_id = data.get("action")
        if not action_id:
            return None

        parsed = parse_action_id(action_id)
        if parsed is None:
            # Not ours. Every HA notification action on the instance arrives
            # here, so this is the common case and not worth a warning.
            log.debug("ignoring foreign action %r", action_id)
            return None

        user_id = ((event.get("context") or {}).get("user_id"))
        if self.owner_user_id and user_id != self.owner_user_id:
            log.warning(
                "dropping action %r from user_id=%r (expected %r)",
                action_id, user_id, self.owner_user_id,
            )
            return None
        return ActionEvent(
            action_id=str(action_id), user_id=user_id, raw=data, parsed=parsed
        )


def _extract_todo_items(resp: Any, entity_id: str) -> List[str]:
    """``todo.get_items`` nests results under the entity id; shape varies."""
    if not isinstance(resp, Mapping):
        return []
    bucket = resp.get(entity_id) or resp.get("items") or {}
    if isinstance(bucket, Mapping):
        bucket = bucket.get("items") or []
    out: List[str] = []
    for item in bucket or []:
        if isinstance(item, Mapping) and item.get("summary"):
            out.append(str(item["summary"]))
    return out
