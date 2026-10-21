"""Scheduler tests: the daily run must actually fire.

This file exists because of a bug that no unit test could have caught and that
the live instance surfaced within two minutes of being pointed at a near-future
run time. The job was registered as::

    lambda: asyncio.create_task(asyncio.to_thread(self.run_once))

``AsyncIOScheduler`` awaits a job on the event loop only when the job is a
coroutine function; anything else it hands to ``run_in_executor``. So the
lambda ran on a worker thread, where ``asyncio.create_task`` found no running
loop and raised. Every scheduled run would have failed, forever, while startup
logged "scheduled daily run at 09:00" and looked healthy.

The lesson generalises past this one call: the contract between us and
APScheduler is *is the job a coroutine function*, and that is a property worth
asserting directly rather than inferring from a successful import.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

import main as mainmod  # noqa: E402


class TestScheduledRun(unittest.TestCase):
    def test_job_is_a_coroutine_function(self):
        """The dispatch rule AsyncIOScheduler uses, asserted on our side.

        ``AsyncIOExecutor._do_submit_job`` branches on exactly this predicate.
        If it is ever False again, the job silently moves to a thread.
        """
        self.assertTrue(
            asyncio.iscoroutinefunction(mainmod.Autopilot.scheduled_run),
            "scheduled_run must be `async def`; see this module's docstring",
        )

    def test_awaiting_it_runs_the_run(self):
        """And the coroutine has to actually invoke the run, off the loop."""
        calls = []
        loop_thread = None

        class FakeBot:
            def run_once(self):
                import threading

                calls.append(threading.current_thread())

        bot = FakeBot()

        async def go():
            nonlocal loop_thread
            import threading

            loop_thread = threading.current_thread()
            await mainmod.Autopilot.scheduled_run(bot)

        asyncio.run(go())

        self.assertEqual(len(calls), 1, "run_once was not called")
        # The blocking work must not land on the event loop thread, or a slow
        # ESPN read would stall the approval-tap listener for its duration.
        self.assertIsNot(
            calls[0], loop_thread,
            "run_once ran on the event loop thread; it blocks and must not",
        )

    def test_it_takes_no_arguments_beyond_self(self):
        """APScheduler is given no args, so the signature must not need any."""
        sig = inspect.signature(mainmod.Autopilot.scheduled_run)
        required = [
            p for n, p in sig.parameters.items()
            if n != "self" and p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        self.assertEqual(required, [], "scheduled_run must be callable with no args")


if __name__ == "__main__":
    unittest.main()
