import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from time_tracking.tracker import (
    MICROSECONDS_PER_SECOND,
    InsufficientWorkTime,
    TimeTracker,
    TrackerAlreadyOpen,
    format_duration,
    start_of_day,
)


MINUTE = 60 * MICROSECONDS_PER_SECOND


class TimeTrackerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "test.sqlite3"
        self.sunday = date(2026, 11, 22)
        self.monday = self.sunday + timedelta(days=1)
        self.now = start_of_day(self.monday) - 10 * MINUTE

    def open_tracker(self):
        tracker = TimeTracker(self.path, clock=lambda: self.now)
        self.addCleanup(tracker.close)
        return tracker

    def test_splits_a_session_at_midnight_and_the_start_of_the_week(self):
        tracker = self.open_tracker()
        tracker.start()
        self.now += 25 * MINUTE
        tracker.checkpoint()

        self.assertEqual(tracker.total(self.sunday, self.sunday), 10 * MINUTE)
        self.assertEqual(tracker.total(self.monday, self.monday), 15 * MINUTE)
        self.assertEqual(tracker.total(self.monday, self.monday + timedelta(days=6)), 15 * MINUTE)
        self.assertEqual(tracker.total(self.sunday, self.monday), 25 * MINUTE)

    def test_paused_time_is_excluded_between_sessions(self):
        tracker = self.open_tracker()
        tracker.start()
        self.now += 3 * MINUTE
        tracker.stop()
        self.now += 2 * MINUTE
        tracker.start()
        self.now += MINUTE
        tracker.stop()

        self.assertEqual(tracker.total(self.sunday, self.sunday), 4 * MINUTE)
        self.assertFalse(tracker.is_running)

    def test_close_saves_running_time_without_resuming_on_reopen(self):
        tracker = TimeTracker(self.path, clock=lambda: self.now)
        tracker.start()
        self.now += 4 * MINUTE
        tracker.close()
        self.now += MINUTE

        reopened = self.open_tracker()
        self.assertFalse(reopened.is_running)
        self.assertEqual(reopened.total(self.sunday, self.sunday), 4 * MINUTE)

    def test_forced_exit_preserves_last_checkpoint_without_resuming(self):
        subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os
import sys
from pathlib import Path
from time_tracking.tracker import TimeTracker

times = iter(map(int, sys.argv[2:]))
tracker = TimeTracker(Path(sys.argv[1]), clock=lambda: next(times))
tracker.start()
tracker.checkpoint()
os._exit(0)
""",
                str(self.path),
                str(self.now),
                str(self.now + 2 * MINUTE),
            ],
            check=True,
        )

        reopened = self.open_tracker()
        self.assertFalse(reopened.is_running)
        self.assertEqual(reopened.total(self.sunday, self.sunday), 2 * MINUTE)

    def test_database_is_owned_by_one_running_tracker(self):
        self.open_tracker()
        with self.assertRaises(TrackerAlreadyOpen):
            TimeTracker(self.path)

    def test_adjustments_affect_only_reports_that_include_the_selected_day(self):
        tracker = self.open_tracker()
        tracker.start()
        self.now += 25 * MINUTE
        tracker.stop()
        tracker.adjust_minutes(self.sunday, 60)
        tracker.adjust_minutes(self.monday, -9)

        self.assertEqual(tracker.total(self.sunday, self.sunday), 70 * MINUTE)
        self.assertEqual(tracker.total(self.monday, self.monday + timedelta(days=6)), 6 * MINUTE)
        self.assertEqual(tracker.total(self.sunday, self.monday), 76 * MINUTE)

    def test_subtraction_uses_the_active_session_and_rejects_a_negative_day(self):
        tracker = self.open_tracker()
        tracker.start()
        self.now += 7 * MINUTE
        tracker.adjust_minutes(self.sunday, -6)
        with self.assertRaises(InsufficientWorkTime):
            tracker.adjust_minutes(self.sunday, -2)

        self.assertTrue(tracker.is_running)
        self.assertEqual(tracker.total(self.sunday, self.sunday), MINUTE)
        tracker.adjust_minutes(self.sunday, -1)
        self.assertEqual(tracker.total(self.sunday, self.sunday), 0)
        self.now += 2 * MINUTE
        tracker.stop()
        self.assertEqual(tracker.total(self.sunday, self.sunday), 2 * MINUTE)

    def test_legacy_database_keeps_sessions_and_persists_adjustments(self):
        with closing(sqlite3.connect(self.path)) as legacy:
            legacy.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at INTEGER, ended_at INTEGER)")
            legacy.execute(
                "INSERT INTO sessions (started_at, ended_at) VALUES (?, ?)",
                (self.now, self.now + 8 * MINUTE),
            )
            legacy.commit()

        tracker = TimeTracker(self.path, clock=lambda: self.now)
        self.assertEqual(tracker.total(self.sunday, self.sunday), 8 * MINUTE)
        tracker.adjust_minutes(self.sunday, 80)
        tracker.close()

        reopened = self.open_tracker()
        self.assertEqual(reopened.total(self.sunday, self.sunday), 88 * MINUTE)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute("SELECT started_at, ended_at FROM sessions").fetchall(), [
                (self.now, self.now + 8 * MINUTE),
            ])

    def test_displays_total_hours_even_when_over_a_day(self):
        self.assertEqual(format_duration((25 * 60 + 7) * MINUTE), "25 ч 07 мин")


if __name__ == "__main__":
    unittest.main()
