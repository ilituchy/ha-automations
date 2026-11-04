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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

import main as mainmod  # noqa: E402
from state import Store  # noqa: E402


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


class TestRedactHost(unittest.TestCase):
    """The startup log must not publish the instance's address.

    A Nabu Casa hostname is a long random label fronting this instance's login
    page, and app logs routinely get pasted into bug reports. The assertion
    that matters is the negative one: the random label must not survive.
    """

    def test_nabu_casa_label_is_gone(self):
        # Invented label of the right shape -- a real one would make this file
        # the leak it is testing against, and this repository is public.
        label = "a" * 20 + "bcdefghijkl"
        out = mainmod._redact_host("https://%s.ui.nabu.casa" % label)
        self.assertNotIn(label, out)
        # ...while still saying it is Nabu Casa, which is the diagnosis.
        self.assertEqual(out, "https://***.ui.nabu.casa")

    def test_subdomain_of_own_domain_is_gone(self):
        self.assertEqual(
            mainmod._redact_host("https://ha.example.com/lovelace"),
            "https://***.example.com/lovelace",
        )

    def test_apex_domain_is_left_alone(self):
        """Nothing random to hide, and `***.com` would be strictly worse."""
        self.assertEqual(mainmod._redact_host("https://example.com"),
                         "https://example.com")

    def test_ipv4_host_is_replaced_whole(self):
        """Address from RFC 5737 documentation space, deliberately not a real one."""
        out = mainmod._redact_host("http://203.0.113.7:8123/")
        for octet in ("203", "113", "7"):
            self.assertNotIn(octet, out)
        self.assertEqual(out, "http://***:8123/")

    def test_never_raises(self):
        """This runs inside a log call; raising there would mask the startup."""
        for bad in ("", "garbage", "://", "http://", None, 7):
            try:
                mainmod._redact_host(bad)
            except Exception as exc:  # noqa: BLE001
                self.fail("_redact_host(%r) raised %r" % (bad, exc))


PT = ZoneInfo("America/Los_Angeles")


def _bot(*run_times):
    """The slice of Autopilot that `missed_run` actually reads."""

    class _Cfg:
        pass

    class _Bot:
        pass

    cfg = _Cfg()
    cfg.run_times = tuple(run_times or ("09:00",))
    bot = _Bot()
    bot.cfg = cfg
    return bot


def _local(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=PT)


class TestMissedRun(unittest.TestCase):
    """The startup catch-up for a fire nobody was alive to receive.

    APScheduler's default job store is in memory: jobs are registered afresh at
    every container start and the next fire is computed from *now*, so a fire
    that lands while the process is dead is dropped silently.
    `misfire_grace_time` does not cover it -- that forgives a scheduler that
    was running and busy, not one that did not exist.
    """

    def call(self, bot, now, last=None):
        return mainmod.Autopilot.missed_run(bot, now, last)

    # -- is anything due at all -------------------------------------------

    def test_before_the_run_time_nothing_is_due(self):
        """A box that boots at 08:00 waits for 09:00 like any other morning."""
        self.assertIsNone(self.call(_bot("09:00"), _local(2026, 11, 4, 8)))

    def test_run_time_passed_with_no_run_on_record_is_due(self):
        due = self.call(_bot("09:00"), _local(2026, 11, 4, 9, 30))
        self.assertEqual(due, _local(2026, 11, 4, 9))

    def test_a_run_after_the_fire_means_it_happened(self):
        """The ordinary case, and the one every deploy restart hits."""
        self.assertIsNone(self.call(
            _bot("09:00"),
            _local(2026, 11, 4, 9, 30),
            _local(2026, 11, 4, 9, 0),
        ))

    def test_a_run_before_the_fire_does_not_count(self):
        """Yesterday's run is not today's run."""
        due = self.call(
            _bot("09:00"),
            _local(2026, 11, 4, 9, 30),
            _local(2026, 11, 3, 9, 0),
        )
        self.assertEqual(due, _local(2026, 11, 4, 9))

    def test_never_run_before_is_handled(self):
        """First boot of a fresh install, after 09:00."""
        due = self.call(_bot("09:00"), _local(2026, 11, 4, 10), None)
        self.assertEqual(due, _local(2026, 11, 4, 9))

    # -- the comparison crosses time zones --------------------------------

    def test_last_run_is_compared_correctly_across_zones(self):
        """`started_at` is UTC; the fire time is local. Both are aware.

        17:00Z is 09:00 PT on this date. A naive string or wall-clock
        comparison would read 17:00 as *after* the 09:30 local boot and skip a
        run that was genuinely missed -- or, with the sign the other way,
        repeat one that happened.
        """
        self.assertIsNone(self.call(
            _bot("09:00"),
            _local(2026, 11, 4, 9, 30),
            datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc),
        ))
        # 16:00Z is 08:00 PT -- an hour *before* the fire, so still due.
        self.assertEqual(
            self.call(
                _bot("09:00"),
                _local(2026, 11, 4, 9, 30),
                datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc),
            ),
            _local(2026, 11, 4, 9),
        )

    # -- the grace window --------------------------------------------------

    def test_too_late_is_not_worth_doing(self):
        """A box that boots at 16:00 is into tip-off; it waits for tomorrow."""
        self.assertIsNone(self.call(_bot("09:00"), _local(2026, 11, 4, 16)))

    def test_the_grace_boundary_still_runs(self):
        """Exactly `CATCHUP_GRACE` late is late, not too late."""
        now = _local(2026, 11, 4, 9) + mainmod.CATCHUP_GRACE
        self.assertEqual(self.call(_bot("09:00"), now), _local(2026, 11, 4, 9))

    def test_one_second_past_the_boundary_does_not(self):
        now = _local(2026, 11, 4, 9) + mainmod.CATCHUP_GRACE + timedelta(seconds=1)
        self.assertIsNone(self.call(_bot("09:00"), now))

    def test_the_window_clears_the_earliest_tip_off(self):
        """The constant is tied to a fact, so assert the fact.

        The earliest NBA tip-off is seven hours after the 09:00 fire; a
        catch-up must still land before it or it is writing into slots ESPN is
        about to lock.
        """
        self.assertLess(mainmod.CATCHUP_GRACE, timedelta(hours=7))

    # -- several configured times ------------------------------------------

    def test_two_passed_times_take_the_later(self):
        """Doing the earlier one first is the same work twice, with worse inputs."""
        due = self.call(_bot("09:00", "12:00"), _local(2026, 11, 4, 12, 30))
        self.assertEqual(due, _local(2026, 11, 4, 12))

    def test_only_the_passed_one_counts(self):
        due = self.call(_bot("09:00", "17:00"), _local(2026, 11, 4, 9, 30))
        self.assertEqual(due, _local(2026, 11, 4, 9))

    def test_unsorted_run_times_are_handled(self):
        """`run_times` is user config and arrives in whatever order they typed."""
        due = self.call(_bot("12:00", "09:00"), _local(2026, 11, 4, 12, 30))
        self.assertEqual(due, _local(2026, 11, 4, 12))

    # -- idempotence across the restarts a deploy causes -------------------

    def test_three_restarts_in_four_minutes_run_it_once(self):
        """2026-11-04 had container starts at 09:15, 09:17 and 09:18.

        Each one calls this. Only a day with no run on record may produce work.
        """
        last = None
        fired = []
        for hh, mm in ((9, 15), (9, 17), (9, 18)):
            due = self.call(_bot("09:00"), _local(2026, 11, 4, hh, mm), last)
            if due is not None:
                fired.append(due)
                last = _local(2026, 11, 4, hh, mm)  # the run it just started
        self.assertEqual(fired, [_local(2026, 11, 4, 9)])

    def test_the_reboot_that_prompted_this(self):
        """The live incident, with its real timings.

        HA's own `system_execute_reboot` fixup rebooted the Green at 09:12:09
        to commit an OS update; the add-on was back at 09:15. That day's run
        had finished at 09:00:31 and was safe. Shift the reboot eleven minutes
        earlier and the same restart must notice the hole.
        """
        boot = _local(2026, 11, 4, 9, 15)
        ran = datetime(2026, 11, 4, 17, 0, 31, tzinfo=timezone.utc)  # 09:00:31 PT
        self.assertIsNone(self.call(_bot("09:00"), boot, ran))
        # ...and the counterfactual: rebooted at 08:58, so 09:00 never fired.
        self.assertEqual(
            self.call(_bot("09:00"), boot, datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)),
            _local(2026, 11, 4, 9),
        )


class TestLastRunStarted(unittest.TestCase):
    """What the catch-up reads to decide whether the day already ran."""

    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_empty_database(self):
        """A fresh install has no runs; None must not be an error."""
        self.assertIsNone(self.store.last_run_started())

    def test_returns_an_aware_datetime(self):
        """Naive would compare-raise against the aware local fire time."""
        self.store.start_run("RUN1", 5, True)
        got = self.store.last_run_started()
        self.assertIsNotNone(got)
        self.assertIsNotNone(got.tzinfo)
        self.assertLess(abs((datetime.now(timezone.utc) - got).total_seconds()), 60)

    def test_returns_the_latest_of_several(self):
        """One row per league per run, so a day writes several."""
        for i in range(3):
            self.store.start_run("RUN%d" % i, 5, True, league_id=i)
        rows = self.store.query("SELECT run_id, started_at FROM runs")
        newest = max(r["started_at"] for r in rows)
        self.assertEqual(self.store.last_run_started(),
                         datetime.fromisoformat(newest))

    def test_a_started_run_counts_even_if_it_failed(self):
        """`start_run` lands before any ESPN write.

        A run that began and then threw must not be retried on every restart;
        a crash loop would otherwise hammer ESPN once per container start.
        """
        self.store.start_run("RUN1", 5, True)
        self.assertIsNotNone(self.store.last_run_started())

    def test_an_unparseable_timestamp_does_not_raise(self):
        """This is read during startup; raising here would stop the daemon."""
        self.store.start_run("RUN1", 5, True)
        self.store.db.execute("UPDATE runs SET started_at='not a date'")
        self.store.db.commit()
        self.assertIsNone(self.store.last_run_started())


if __name__ == "__main__":
    unittest.main()
