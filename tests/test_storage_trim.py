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
    RetentionTooShort, DEFAULT_MIN_RETENTION_SECONDS,
    min_retention_seconds, policy_from_env,
    plan_trim_all, execute_trim_all,
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


# ---- min retention floor (PR 4) ------------------------------------------


class TestMinRetentionFloor(unittest.TestCase):
    """The 30-min floor guards against operators accidentally shrinking
    retention below what the slowest downstream consumer needs to drain.
    """

    def test_default_floor_is_30_min(self):
        self.assertEqual(min_retention_seconds(env={}), 30 * 60)

    def test_env_override_changes_floor(self):
        self.assertEqual(
            min_retention_seconds(env={"TRIM_MIN_FLOOR_SECONDS": "3600"}),
            3600.0,
        )

    def test_env_override_garbage_falls_back_to_default(self):
        # Typo / non-numeric must NOT silently zero the floor.
        self.assertEqual(
            min_retention_seconds(env={"TRIM_MIN_FLOOR_SECONDS": "soon"}),
            30 * 60,
        )

    def test_plan_trim_below_floor_raises(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        try:
            conn = _seed_db(tmp.name)
            conn.close()
            with self.assertRaises(RetentionTooShort):
                plan_trim(tmp.name, max_age_seconds=60)  # 1 min < 30 min
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_plan_trim_enforce_floor_false_bypasses(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        try:
            conn = _seed_db(tmp.name)
            conn.close()
            # Below-floor age is allowed when caller opts out (used by
            # plan_trim_all after policy_from_env already clamped).
            plan = plan_trim(
                tmp.name, max_age_seconds=60, enforce_floor=False,
            )
            self.assertEqual(plan.total_rows, 0)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


# ---- policy_from_env (PR 4) ----------------------------------------------


class TestPolicyFromEnv(unittest.TestCase):

    def test_defaults_when_no_env(self):
        policies = policy_from_env(env={})
        by_target = {(p.target_db, p.target_table): p for p in policies}
        self.assertEqual(by_target[("psk",  "spots")].max_age_seconds,
                         60 * 60)             # default 60 min
        self.assertEqual(by_target[("wspr", "spots")].max_age_seconds,
                         24 * 60 * 60)        # default 24 h
        self.assertEqual(by_target[("wspr", "noise")].max_age_seconds,
                         24 * 60 * 60)
        self.assertEqual(
            by_target[("superdarn", "detections")].max_age_seconds,
            30 * 24 * 60 * 60)                # default 30 days
        for p in policies:
            self.assertEqual(p.source, "default")

    def test_psk_env_override_applies(self):
        policies = policy_from_env(env={"PSK_RETENTION_MIN": "120"})
        psk = [p for p in policies if (p.target_db, p.target_table)
               == ("psk", "spots")][0]
        self.assertEqual(psk.max_age_seconds, 120 * 60)
        self.assertEqual(psk.source, "env:PSK_RETENTION_MIN")

    def test_wspr_env_overrides_apply_to_both_spots_and_noise(self):
        policies = policy_from_env(env={"WSPR_RETENTION_MIN": "180"})
        wspr = [p for p in policies if p.target_db == "wspr"]
        for p in wspr:
            self.assertEqual(p.max_age_seconds, 180 * 60)
            self.assertEqual(p.source, "env:WSPR_RETENTION_MIN")

    def test_below_floor_clamps_up(self):
        # PSK retention of 10 min must be clamped to the 30-min floor.
        policies = policy_from_env(env={"PSK_RETENTION_MIN": "10"})
        psk = [p for p in policies if (p.target_db, p.target_table)
               == ("psk", "spots")][0]
        self.assertEqual(psk.max_age_seconds, 30 * 60)
        self.assertIn("clamped to floor", psk.source)

    def test_zero_or_negative_env_falls_back_to_default(self):
        policies = policy_from_env(env={"PSK_RETENTION_MIN": "0"})
        psk = [p for p in policies if (p.target_db, p.target_table)
               == ("psk", "spots")][0]
        # 0 means "use default" (60 min), which is above the floor.
        self.assertEqual(psk.max_age_seconds, 60 * 60)

    def test_superdarn_env_override_applies(self):
        policies = policy_from_env(env={"SUPERDARN_RETENTION_MIN": "1440"})
        sd = [p for p in policies if (p.target_db, p.target_table)
              == ("superdarn", "detections")][0]
        self.assertEqual(sd.max_age_seconds, 1440 * 60)
        self.assertEqual(sd.source, "env:SUPERDARN_RETENTION_MIN")

    def test_garbage_env_falls_back_to_default(self):
        policies = policy_from_env(env={"PSK_RETENTION_MIN": "tomorrow"})
        psk = [p for p in policies if (p.target_db, p.target_table)
               == ("psk", "spots")][0]
        self.assertEqual(psk.max_age_seconds, 60 * 60)


# ---- plan_trim_all + execute_trim_all (PR 4) -----------------------------


class TestPlanTrimAll(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.tmp.close()
        self.db_path = self.tmp.name
        self.now = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.now_fn = lambda: self.now
        conn = _seed_db(self.db_path)
        # Three psk rows: two old (3h ago, beyond 60min default), one fresh.
        _insert(conn, target_db="psk",  target_table="spots",
                queued_at=self.now - timedelta(hours=3))
        _insert(conn, target_db="psk",  target_table="spots",
                queued_at=self.now - timedelta(hours=2))
        _insert(conn, target_db="psk",  target_table="spots",
                queued_at=self.now - timedelta(minutes=10))
        # Two wspr rows: one old (48h, beyond 24h default), one fresh (1h).
        _insert(conn, target_db="wspr", target_table="spots",
                queued_at=self.now - timedelta(hours=48))
        _insert(conn, target_db="wspr", target_table="spots",
                queued_at=self.now - timedelta(hours=1))
        conn.close()

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_default_policies_plan_correct_per_target(self):
        # With defaults: psk=60m, wspr=24h. Two psk rows are >60m old,
        # one wspr row is >24h old.
        plans = plan_trim_all(
            self.db_path,
            policies=policy_from_env(env={}),
            now_fn=self.now_fn,
        )
        by_target = {(pp.policy.target_db, pp.policy.target_table): pp
                     for pp in plans}
        self.assertEqual(by_target[("psk", "spots")].plan.total_rows, 2)
        self.assertEqual(by_target[("wspr", "spots")].plan.total_rows, 1)
        # wspr.noise has no rows in this fixture → empty plan.
        self.assertEqual(by_target[("wspr", "noise")].plan.total_rows, 0)

    def test_execute_all_deletes_per_policy(self):
        plans = plan_trim_all(
            self.db_path,
            policies=policy_from_env(env={}),
            now_fn=self.now_fn,
        )
        for pp in plans:
            pp.plan.confirmed = True
        reports = execute_trim_all(plans)
        deleted = {(pol.target_db, pol.target_table): rep.rows_deleted
                   for pol, rep in reports}
        self.assertEqual(deleted[("psk", "spots")], 2)
        self.assertEqual(deleted[("wspr", "spots")], 1)
        self.assertEqual(deleted[("wspr", "noise")], 0)

    def test_execute_all_unconfirmed_raises(self):
        plans = plan_trim_all(
            self.db_path,
            policies=policy_from_env(env={}),
            now_fn=self.now_fn,
        )
        with self.assertRaises(NotConfirmed):
            execute_trim_all(plans)


if __name__ == "__main__":
    unittest.main()
