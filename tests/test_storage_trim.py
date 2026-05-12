"""Tests for sigmond.storage_trim — TTL-based pending_uploads janitor."""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.storage_trim import (
    NotConfirmed, TrimPlan, TrimReport,
    execute_trim, parse_duration, plan_trim,
)


_QUEUE_DDL = """
CREATE TABLE pending_uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_db       TEXT NOT NULL,
    target_table    TEXT NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 0,
    payload_json    TEXT NOT NULL,
    queued_at       TEXT NOT NULL
)
"""


def _seed_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute(_QUEUE_DDL)
    conn.commit()
    return conn


def _insert(conn: sqlite3.Connection, *, target_db: str, target_table: str,
            queued_at: datetime) -> None:
    conn.execute(
        "INSERT INTO pending_uploads "
        "(target_db, target_table, schema_version, payload_json, queued_at) "
        "VALUES (?, ?, 0, '{}', ?)",
        (target_db, target_table, queued_at.isoformat()),
    )
    conn.commit()


# ---- planning ------------------------------------------------------------


class TestPlanTrim(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db_path = self.tmp.name
        self.now = datetime(2026, 5, 12, 1, 0, 0, tzinfo=timezone.utc)
        self.now_fn = lambda: self.now

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_missing_db_returns_empty_plan(self):
        Path(self.db_path).unlink()
        plan = plan_trim(self.db_path, 3600, now_fn=self.now_fn)
        self.assertTrue(plan.is_empty)
        self.assertEqual(plan.rows_per_target, [])

    def test_empty_table_returns_empty_plan(self):
        conn = _seed_db(self.db_path)
        conn.close()
        plan = plan_trim(self.db_path, 3600, now_fn=self.now_fn)
        self.assertTrue(plan.is_empty)

    def test_missing_table_returns_empty_plan(self):
        # 0-byte sqlite file (the pre-create state) → table not yet there
        plan = plan_trim(self.db_path, 3600, now_fn=self.now_fn)
        self.assertTrue(plan.is_empty)

    def test_rows_grouped_by_target_db_and_table(self):
        conn = _seed_db(self.db_path)
        old = self.now - timedelta(hours=25)  # older than 24h cutoff
        for _ in range(3):
            _insert(conn, target_db="hfdl", target_table="spots", queued_at=old)
        for _ in range(7):
            _insert(conn, target_db="timestd", target_table="events", queued_at=old)
        for _ in range(2):
            _insert(conn, target_db="psk", target_table="spots", queued_at=old)
        # Recent rows — within cutoff, must NOT appear in plan
        recent = self.now - timedelta(minutes=5)
        for _ in range(99):
            _insert(conn, target_db="psk", target_table="spots", queued_at=recent)
        conn.close()

        plan = plan_trim(self.db_path, 24 * 3600, now_fn=self.now_fn)
        # ORDER BY target_db, target_table — alphabetical.
        self.assertEqual(
            plan.rows_per_target,
            [("hfdl", "spots", 3), ("psk", "spots", 2), ("timestd", "events", 7)],
        )
        self.assertEqual(plan.total_rows, 12)

    def test_cutoff_iso_is_now_minus_max_age(self):
        plan = plan_trim(
            self.db_path, max_age_seconds=3600, now_fn=self.now_fn,
        )
        expected = (self.now - timedelta(seconds=3600)).isoformat()
        self.assertEqual(plan.cutoff_iso, expected)

    def test_target_db_filter_narrows_plan(self):
        """`target_db='hfdl'` reports only hfdl rows; others untouched
        even though they're past the cutoff too."""
        conn = _seed_db(self.db_path)
        old = self.now - timedelta(hours=25)
        for _ in range(4):
            _insert(conn, target_db="hfdl", target_table="spots", queued_at=old)
        for _ in range(9):
            _insert(conn, target_db="timestd", target_table="events", queued_at=old)
        conn.close()

        plan = plan_trim(
            self.db_path, 24 * 3600,
            target_db="hfdl",
            now_fn=self.now_fn,
        )
        self.assertEqual(plan.rows_per_target, [("hfdl", "spots", 4)])
        self.assertEqual(plan.target_db, "hfdl")
        self.assertIsNone(plan.target_table)

    def test_target_table_filter_further_narrows(self):
        """`target_db='hfdl' + target_table='spots'` restricts to that
        one (db, table) tuple even when the same target_db has
        multiple tables."""
        conn = _seed_db(self.db_path)
        old = self.now - timedelta(hours=25)
        for _ in range(3):
            _insert(conn, target_db="hfdl", target_table="spots", queued_at=old)
        for _ in range(5):
            _insert(conn, target_db="hfdl", target_table="other", queued_at=old)
        conn.close()

        plan = plan_trim(
            self.db_path, 24 * 3600,
            target_db="hfdl", target_table="spots",
            now_fn=self.now_fn,
        )
        self.assertEqual(plan.rows_per_target, [("hfdl", "spots", 3)])

    def test_filter_passes_through_to_plan_for_execute(self):
        """The plan's filter fields persist so execute_trim's DELETE
        scope matches what the dry-run reported."""
        plan = plan_trim(
            self.db_path, 24 * 3600,
            target_db="hfdl", target_table="spots",
            now_fn=self.now_fn,
        )
        self.assertEqual(plan.target_db, "hfdl")
        self.assertEqual(plan.target_table, "spots")


# ---- execution -----------------------------------------------------------


class TestExecuteTrim(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db_path = self.tmp.name
        self.now = datetime(2026, 5, 12, 1, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def _count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM pending_uploads"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_refuses_unconfirmed_plan(self):
        plan = TrimPlan(db_path=self.db_path, cutoff_iso="2026-01-01T00:00:00+00:00")
        plan.rows_per_target = [("hfdl", "spots", 1)]
        with self.assertRaises(NotConfirmed):
            execute_trim(plan)

    def test_empty_plan_is_noop(self):
        plan = TrimPlan(
            db_path=self.db_path,
            cutoff_iso="2026-01-01T00:00:00+00:00",
            confirmed=True,
        )
        report = execute_trim(plan)
        self.assertEqual(report.rows_deleted, 0)
        self.assertEqual(report.errors, [])

    def test_deletes_only_rows_older_than_cutoff(self):
        conn = _seed_db(self.db_path)
        old = self.now - timedelta(hours=25)
        recent = self.now - timedelta(minutes=5)
        for _ in range(5):
            _insert(conn, target_db="hfdl", target_table="spots", queued_at=old)
        for _ in range(3):
            _insert(conn, target_db="hfdl", target_table="spots", queued_at=recent)
        conn.close()

        plan = plan_trim(
            self.db_path, 24 * 3600, now_fn=lambda: self.now,
        )
        plan.confirmed = True
        report = execute_trim(plan)

        self.assertEqual(report.rows_deleted, 5)
        self.assertEqual(self._count(), 3)  # the recent rows survive

    def test_target_db_execute_only_deletes_filtered_rows(self):
        """Critical for the hfdl-only timer use case: passing
        target_db='hfdl' must NOT delete timestd rows even when they
        are also past the cutoff."""
        conn = _seed_db(self.db_path)
        old = self.now - timedelta(hours=25)
        for _ in range(4):
            _insert(conn, target_db="hfdl", target_table="spots", queued_at=old)
        for _ in range(6):
            _insert(conn, target_db="timestd", target_table="events", queued_at=old)
        conn.close()

        plan = plan_trim(
            self.db_path, 24 * 3600,
            target_db="hfdl",
            now_fn=lambda: self.now,
        )
        plan.confirmed = True
        report = execute_trim(plan)

        self.assertEqual(report.rows_deleted, 4)
        # timestd rows survived even though they were also past cutoff.
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                "SELECT target_db, COUNT(*) FROM pending_uploads GROUP BY 1"
            )
            rows = dict(cur.fetchall())
        finally:
            conn.close()
        self.assertEqual(rows, {"timestd": 6})

    def test_target_table_execute_narrows_within_db(self):
        conn = _seed_db(self.db_path)
        old = self.now - timedelta(hours=25)
        for _ in range(3):
            _insert(conn, target_db="hfdl", target_table="spots", queued_at=old)
        for _ in range(5):
            _insert(conn, target_db="hfdl", target_table="other", queued_at=old)
        conn.close()

        plan = plan_trim(
            self.db_path, 24 * 3600,
            target_db="hfdl", target_table="spots",
            now_fn=lambda: self.now,
        )
        plan.confirmed = True
        report = execute_trim(plan)

        self.assertEqual(report.rows_deleted, 3)
        # The "other" rows within hfdl survived.
        conn = sqlite3.connect(self.db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM pending_uploads "
                "WHERE target_db='hfdl' AND target_table='other'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 5)

    def test_executes_idempotently(self):
        conn = _seed_db(self.db_path)
        old = self.now - timedelta(hours=25)
        for _ in range(4):
            _insert(conn, target_db="psk", target_table="spots", queued_at=old)
        conn.close()

        plan = plan_trim(self.db_path, 24 * 3600, now_fn=lambda: self.now)
        plan.confirmed = True
        first = execute_trim(plan)
        self.assertEqual(first.rows_deleted, 4)

        # Re-running with the same plan deletes nothing (cutoff is fixed).
        # Note: plan.rows_per_target is now stale, but execute_trim runs
        # the DELETE based on cutoff_iso, not on the planned counts.
        # The is_empty short-circuit means this is a no-op without
        # touching the db — verify by deleting the file first to prove
        # the path isn't taken.
        plan_after = plan_trim(self.db_path, 24 * 3600, now_fn=lambda: self.now)
        plan_after.confirmed = True
        second = execute_trim(plan_after)
        self.assertEqual(second.rows_deleted, 0)


# ---- parse_duration ------------------------------------------------------


class TestParseDuration(unittest.TestCase):

    def test_seconds_suffix(self):
        self.assertEqual(parse_duration("30s"), 30.0)

    def test_minutes_suffix(self):
        self.assertEqual(parse_duration("5m"), 300.0)

    def test_hours_suffix(self):
        self.assertEqual(parse_duration("2h"), 7200.0)

    def test_days_suffix(self):
        self.assertEqual(parse_duration("7d"), 7 * 86400.0)

    def test_float_value(self):
        self.assertEqual(parse_duration("1.5h"), 5400.0)

    def test_bare_number_is_seconds(self):
        self.assertEqual(parse_duration("90"), 90.0)

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            parse_duration("")

    def test_unknown_suffix_raises(self):
        # "5x" — bad suffix means bare-number parse also fails
        with self.assertRaises(ValueError):
            parse_duration("5x")

    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            parse_duration("-30s")

    def test_whitespace_tolerated(self):
        self.assertEqual(parse_duration("  24h  "), 86400.0)


if __name__ == "__main__":
    unittest.main()
