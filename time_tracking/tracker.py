import fcntl
import sqlite3
import time
from collections.abc import Callable
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path


MICROSECONDS_PER_SECOND = 1_000_000
MICROSECONDS_PER_MINUTE = 60 * MICROSECONDS_PER_SECOND
MAX_SQLITE_INTEGER = (1 << 63) - 1


def timestamp() -> int:
    return time.time_ns() // 1_000


def start_of_day(day: date) -> int:
    local_midnight = datetime.combine(day, day_time.min)
    return int(local_midnight.timestamp()) * MICROSECONDS_PER_SECOND


def format_duration(microseconds: int) -> str:
    minutes = microseconds // MICROSECONDS_PER_MINUTE
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


class TrackerAlreadyOpen(RuntimeError):
    pass


class InsufficientWorkTime(ValueError):
    pass


class TimeTracker:
    def __init__(self, path: Path, clock: Callable[[], int] = timestamp):
        self._clock = clock
        self._session_id: int | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lock.close()
            raise TrackerAlreadyOpen from error
        except OSError:
            self._lock.close()
            raise

        try:
            self._connection = sqlite3.connect(path)
            try:
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY,
                        started_at INTEGER NOT NULL,
                        ended_at INTEGER NOT NULL CHECK (ended_at >= started_at)
                    );
                    CREATE INDEX IF NOT EXISTS sessions_end ON sessions (ended_at);
                    CREATE TABLE IF NOT EXISTS daily_adjustments (
                        day TEXT PRIMARY KEY NOT NULL,
                        delta INTEGER NOT NULL
                    );
                    """
                )
            except sqlite3.Error:
                self._connection.close()
                raise
        except sqlite3.Error:
            self._lock.close()
            raise

    @property
    def is_running(self) -> bool:
        return self._session_id is not None

    def start(self) -> None:
        if self.is_running:
            return
        now = self._clock()
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO sessions (started_at, ended_at) VALUES (?, ?)", (now, now)
            )
        self._session_id = cursor.lastrowid

    def checkpoint(self) -> None:
        if not self.is_running:
            return
        # A committed end time preserves progress even if the process is killed.
        with self._connection:
            self._connection.execute(
                "UPDATE sessions SET ended_at = MAX(ended_at, ?) WHERE id = ?",
                (self._clock(), self._session_id),
            )

    def stop(self) -> None:
        self.checkpoint()
        self._session_id = None

    def close(self) -> None:
        self.stop()
        self._connection.close()
        self._lock.close()

    def adjust_minutes(self, day: date, minutes: int) -> None:
        if minutes == 0:
            raise ValueError("Adjustment must be nonzero")
        change = minutes * MICROSECONDS_PER_MINUTE
        self.checkpoint()
        day_key = day.isoformat()
        with self._connection:
            row = self._connection.execute(
                "SELECT delta FROM daily_adjustments WHERE day = ?", (day_key,)
            ).fetchone()
            previous = row[0] if row else 0
            if previous < 0 or change < 0:
                recorded = self._session_total(day, day)
                previous = max(previous, -recorded)
                if recorded + previous + change < 0:
                    raise InsufficientWorkTime

            delta = previous + change
            if abs(delta) > MAX_SQLITE_INTEGER:
                raise ValueError("Adjustment is out of range")
            self._connection.execute(
                """
                INSERT INTO daily_adjustments (day, delta) VALUES (?, ?)
                ON CONFLICT (day) DO UPDATE SET delta = excluded.delta
                """,
                (day_key, delta),
            )

    def total(self, first_day: date, last_day: date) -> int:
        if first_day > last_day:
            raise ValueError("first_day must not be after last_day")
        recorded = self._session_total(first_day, last_day)
        adjustments = self._connection.execute(
            "SELECT day, delta FROM daily_adjustments WHERE day BETWEEN ? AND ?",
            (first_day.isoformat(), last_day.isoformat()),
        )
        adjustment_total = 0
        for day_key, delta in adjustments:
            if delta < 0:
                # Calendar boundaries may shift when the Mac changes time zones.
                day = date.fromisoformat(day_key)
                day_total = recorded if first_day == last_day else self._session_total(day, day)
                delta = max(delta, -day_total)
            adjustment_total += delta
        return recorded + adjustment_total

    def _session_total(self, first_day: date, last_day: date) -> int:
        start = start_of_day(first_day)
        end = (
            MAX_SQLITE_INTEGER
            if last_day == date.max
            else start_of_day(last_day + timedelta(days=1))
        )
        (duration,) = self._connection.execute(
            """
            SELECT COALESCE(SUM(MIN(ended_at, :end) - MAX(started_at, :start)), 0)
            FROM sessions
            WHERE ended_at > :start AND started_at < :end
            """,
            {"start": start, "end": end},
        ).fetchone()
        return duration
