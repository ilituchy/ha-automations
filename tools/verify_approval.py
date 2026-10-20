#!/usr/bin/env python3
"""End-to-end check of the approval round-trip. Sends nothing to ESPN.

This verifies the one path that cannot be unit-tested: a push leaves HA, lands
on the phone, the user taps a button, and the tap arrives back as an event this
app recognises and attributes to the right person.

It is the highest-value check in the project because its failure mode is
silent. If remote access is down, the notification still arrives and the button
still looks tappable -- nothing surfaces until an approval is needed and
quietly does not happen.

Run it from a dev box against the real instance:

    HA_URL=http://homeassistant.local:8123 tools/verify_approval.py

Subscribes *before* sending, so a fast tap cannot be missed.
"""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "nba_autopilot", "rootfs", "app"))

from hactl import HaWs, _base, _token  # noqa: E402

from ha import bridge  # noqa: E402

RUN_ID = "VERIFY"
PROPOSAL_ID = "%s:ADD_DROP:1" % RUN_ID
WAIT_SECONDS = float(os.environ.get("VERIFY_WAIT", "180"))


def main() -> int:
    owner = os.environ.get("HA_OWNER_USER_ID", "")
    device = os.environ.get("HA_NOTIFY_DEVICE", "mobile_app_iphone")

    ws = HaWs()
    me = ws.command({"type": "auth/current_user"}).get("result", {})
    if not owner:
        owner = me.get("id", "")
    print("instance : %s (ha %s)" % (_base(), ws.ha_version))
    print("owner    : %s  %s" % (me.get("name"), owner))

    b = bridge.HaBridge(
        notify_device=device,
        owner_user_id=owner,
        todo_entity=os.environ.get("HA_TODO_ENTITY", "todo.shopping_list"),
        token=_token(),
        base_url=_base() + "/api",
        ws_url=_base().replace("http", "ws", 1) + "/api/websocket",
    )

    external = b.check_external_url()
    print("external : %s" % external)
    if external is None:
        print("\nFAIL: no verified external access. A tap cannot reach HA.")
        return 1

    # Subscribe first. A tap that arrives before the subscription is a tap this
    # tool would report as a failure, which would be a lie about the system.
    ws.subscribe("mobile_app_notification_action")

    approve = bridge.item_action_id(PROPOSAL_ID, bridge.APPROVE)
    reject = bridge.item_action_id(PROPOSAL_ID, bridge.REJECT)
    print("actions  : %s | %s" % (approve, reject))

    b.notify(
        title="Autopilot: connection test",
        message=(
            "This is a test of the approval path. No roster move exists and "
            "nothing will be sent to ESPN. Tap Approve to confirm the tap "
            "reaches Home Assistant."
        ),
        tag="autopilot-verify",
        actions=(
            bridge.Action(approve, "Approve"),
            bridge.Action(reject, "Reject", destructive=True),
        ),
    )
    print("\nsent. waiting %.0fs for a tap...\n" % WAIT_SECONDS)
    sys.stdout.flush()

    deadline = time.time() + WAIT_SECONDS
    for event in ws.events(deadline):
        if event.get("event_type") != "mobile_app_notification_action":
            continue
        raw = {"event": event}
        print("raw event: %s" % json.dumps(event.get("data", {}))[:300])
        print("context  : user_id=%s" % (event.get("context") or {}).get("user_id"))

        parsed = b._parse_action_event(raw)
        if parsed is None:
            print("\nREJECTED by the bridge (foreign id or wrong user).")
            continue

        print("\nPASS")
        print("  tapped id   : %s" % parsed.action_id)
        print("  proposal id : %s" % parsed.parsed.proposal_action_id)
        print("  verb        : %s (approve=%s)"
              % (parsed.parsed.verb, parsed.parsed.approve))
        print("  attributed  : %s" % parsed.user_id)
        b.confirm(
            tag="autopilot-verify",
            title="Autopilot: test passed",
            message="The approval path works. Nothing was sent to ESPN.",
        )
        ws.close()
        return 0

    print("TIMEOUT: no tap arrived.")
    print("  The push may not have been delivered, or the tap did not reach HA.")
    ws.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
