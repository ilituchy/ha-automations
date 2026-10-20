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

        Returns the external URL, or ``None`` with a loud log line. A local-only
        install still receives notifications but can never receive an approval,
        so this is a startup-time check, not a runtime surprise.
        """
        try:
            r = self.http.get("%s/config" % self.base_url)
            cfg = r.json() if r.status_code < 400 else {}
        except Exception as exc:  # noqa: BLE001 - startup diagnostic only
            log.error("could not read HA config: %s", exc)
            return None
        ext = cfg.get("external_url")
        cloud = "cloud" in (cfg.get("components") or [])
        if not ext and not cloud:
            log.error(
                "HA reports no external_url and no cloud component. Push "
                "notifications will arrive but APPROVAL TAPS WILL SILENTLY DO "
                "NOTHING. Enable Nabu Casa / HA Cloud or set an external URL."
            )
            return None
        return ext or "cloud"

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
        user_id = ((event.get("context") or {}).get("user_id"))
        if self.owner_user_id and user_id != self.owner_user_id:
            log.warning(
                "dropping action %r from user_id=%r (expected %r)",
                action_id, user_id, self.owner_user_id,
            )
            return None
        return ActionEvent(action_id=str(action_id), user_id=user_id, raw=data)


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
