from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime

import _fixtures  # noqa: F401
from advisor.notify import Scheduler, describe, due, parse_schedule

TUE = datetime(2026, 11, 10, 18, 0)  # a Tuesday


class TestSchedule(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_schedule(["tue 18:00", "Daily 9:05", "sat 10:00"]),
                         [(1, 18, 0), (None, 9, 5), (5, 10, 0)])
        self.assertEqual(parse_schedule(["tuesday 6pm", "tue 25:00", ""]), [])
        self.assertEqual(describe(parse_schedule(["tue 18:00", "daily 09:05"])), "Tue 6:00 PM, Daily 9:05 AM")

    def test_due_window(self):
        e = parse_schedule(["tue 18:00"])
        self.assertEqual(len(due(e, TUE.replace(minute=0), set())), 1)
        self.assertEqual(len(due(e, TUE.replace(minute=19), set())), 1)   # inside grace
        self.assertEqual(due(e, TUE.replace(minute=21), set()), [])       # missed: wait a week
        self.assertEqual(due(e, TUE.replace(hour=17, minute=59), set()), [])
        self.assertEqual(due(e, datetime(2026, 11, 11, 18, 0), set()), [])  # Wednesday

    def test_midnight_wrap(self):
        e = parse_schedule(["mon 23:55"])
        self.assertEqual(len(due(e, datetime(2026, 11, 10, 0, 5), set())), 1)  # Tue 00:05, 10 min late

    def test_fires_once_even_across_restart(self):
        fired = []
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            s = Scheduler(path, lambda: ["tue 18:00"], fired.append)
            s.tick(TUE)
            s.tick(TUE.replace(minute=1))
            Scheduler(path, lambda: ["tue 18:00"], fired.append).tick(TUE.replace(minute=2))
        self.assertEqual(len(fired), 1)


if __name__ == "__main__":
    unittest.main()
