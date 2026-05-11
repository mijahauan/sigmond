"""Tests for sigmond.hamsci_ch.SqliteWriter (CONTRACT §17.5 alt backend)."""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.hamsci_ch import BufferFull, SqliteConfig, SqliteWriter, Writer
from sigmond.hamsci_ch.sqlite_writer import (
    HEALTH_DEGRADED, HEALTH_NOOP, HEALTH_OK, HEALTH_UNREACHABLE,
    _resolve_db_alias,
)


def _temp_db_path() -> str:
    """Caller-owned temp file path; we delete in tearDown."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    Path(f.name).unlink()  # let sqlite create the file fresh
    return f.name


class TestNoOpMode(unittest.TestCase):
    """No env vars → noop. Standalone-safe.  Mirrors the CH writer's contract."""

    def test_from_env_no_path_yields_noop(self):
        w = SqliteWriter.from_env(table="spots", mode="psk", env={})
        self.assertTrue(w.is_noop)
        self.assertEqual(w.health, HEALTH_NOOP)

    def test_noop_insert_does_nothing(self):
        w = SqliteWriter.from_env(table="spots", mode="psk", env={})
        w.insert([{"a": 1}, {"a": 2}])
        self.assertEqual(w.buffered, 0)
        w.flush()
        w.close()


class TestConfigAndAlias(unittest.TestCase):

    def test_config_from_env_strips_blank(self):
        self.assertIsNone(SqliteConfig.from_env({"SIGMOND_SQLITE_PATH": ""}))
        self.assertIsNone(SqliteConfig.from_env({"SIGMOND_SQLITE_PATH": "   "}))

    def test_config_from_env_returns_path(self):
        cfg = SqliteConfig.from_env({"SIGMOND_SQLITE_PATH": "/tmp/sink.db"})
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.path, "/tmp/sink.db")

    def test_resolve_db_alias_uses_env_then_falls_back(self):
        env = {"SIGMOND_SQLITE_DB_PSK": "psk_local"}
        self.assertEqual(_resolve_db_alias("psk", env), "psk_local")
        self.assertEqual(_resolve_db_alias("hfdl", env), "hfdl")


class TestEnabledWriter(unittest.TestCase):

    def setUp(self):
        self.db_path = _temp_db_path()
        self.env = {"SIGMOND_SQLITE_PATH": self.db_path}

    def tearDown(self):
        p = Path(self.db_path)
        if p.exists():
            p.unlink()
        # WAL/SHM sidecars
        for suffix in ("-wal", "-shm"):
            sidecar = Path(self.db_path + suffix)
            if sidecar.exists():
                sidecar.unlink()

    def _writer(self, **kwargs) -> SqliteWriter:
        return SqliteWriter.from_env(
            table="spots", mode="psk", env=self.env, batch_rows=3, **kwargs,
        )

    def _queue_rows(self) -> list:
        # Table is created lazily on first flush; treat "not yet" as empty.
        if not Path(self.db_path).exists():
            return []
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='pending_uploads'"
            )
            if cur.fetchone() is None:
                return []
            cur = conn.execute(
                "SELECT target_db, target_table, schema_version, "
                "payload_json, queued_at FROM pending_uploads ORDER BY id"
            )
            return list(cur.fetchall())
        finally:
            conn.close()

    def test_buffers_until_batch_threshold(self):
        w = self._writer()
        w.insert([{"a": 1}, {"a": 2}])
        self.assertEqual(w.buffered, 2)
        self.assertEqual(self._queue_rows(), [])  # not flushed yet
        w.insert([{"a": 3}])  # crosses batch_rows=3
        rows = self._queue_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(w.buffered, 0)
        self.assertEqual(w.health, HEALTH_OK)

    def test_explicit_flush_drains_buffer(self):
        w = self._writer()
        w.insert([{"a": 1}])
        w.flush()
        self.assertEqual(len(self._queue_rows()), 1)
        self.assertEqual(w.buffered, 0)

    def test_payload_is_json_with_target_metadata(self):
        w = self._writer()
        w.insert([{"frequency": 14074000, "mode": "ft8", "score": 17}])
        w.flush()
        rows = self._queue_rows()
        self.assertEqual(len(rows), 1)
        target_db, target_table, schema_version, payload_json, queued_at = rows[0]
        self.assertEqual(target_db, "psk")
        self.assertEqual(target_table, "spots")
        self.assertEqual(schema_version, 0)
        decoded = json.loads(payload_json)
        self.assertEqual(decoded["frequency"], 14074000)
        self.assertEqual(decoded["mode"], "ft8")
        # queued_at parses as ISO8601 UTC.
        parsed = datetime.fromisoformat(queued_at)
        self.assertIsNotNone(parsed.tzinfo)

    def test_datetime_serializes_to_iso(self):
        w = self._writer()
        t = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        w.insert([{"time": t}])
        w.flush()
        payload = json.loads(self._queue_rows()[0][3])
        self.assertEqual(payload["time"], t.isoformat())

    def test_alias_overrides_database_from_env(self):
        env = {**self.env, "SIGMOND_SQLITE_DB_PSK": "psk_alt"}
        w = SqliteWriter.from_env(
            table="spots", mode="psk", env=env, batch_rows=1,
        )
        w.insert([{"x": 1}])
        rows = self._queue_rows()
        self.assertEqual(rows[0][0], "psk_alt")

    def test_close_flushes_and_closes_conn(self):
        w = self._writer()
        w.insert([{"a": 1}])
        w.close()
        self.assertEqual(len(self._queue_rows()), 1)

    def test_context_manager_flushes(self):
        with SqliteWriter.from_env(
            table="spots", mode="psk", env=self.env, batch_rows=10,
        ) as w:
            w.insert([{"x": 1}])
        self.assertEqual(len(self._queue_rows()), 1)

    def test_schema_version_persisted(self):
        w = SqliteWriter.from_env(
            table="spots", mode="psk", env=self.env, batch_rows=1,
            schema_version=7,
        )
        w.insert([{"x": 1}])
        self.assertEqual(self._queue_rows()[0][2], 7)

    def test_multiple_tables_coexist_in_one_db(self):
        spots = SqliteWriter.from_env(
            table="spots", mode="psk", env=self.env, batch_rows=1,
        )
        noise = SqliteWriter.from_env(
            table="noise", mode="wspr", env=self.env, batch_rows=1,
        )
        spots.insert([{"freq": 14074000}])
        noise.insert([{"floor": -120}])
        rows = self._queue_rows()
        targets = {(r[0], r[1]) for r in rows}
        self.assertEqual(targets, {("psk", "spots"), ("wspr", "noise")})


class TestUnreachableHandling(unittest.TestCase):
    """SQLite is local, so 'unreachable' means disk-full / readonly /
    locked-too-long.  We simulate with a connect_factory that fails."""

    def test_transient_failure_keeps_buffer_marks_unreachable(self):
        attempts = {"n": 0}

        def factory(cfg):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise sqlite3.OperationalError("simulated disk error")
            return sqlite3.connect(":memory:")

        w = SqliteWriter.from_env(
            table="spots", mode="psk",
            env={"SIGMOND_SQLITE_PATH": "/nonexistent/dir/sink.db"},
            batch_rows=2, connect_factory=factory,
        )
        w.insert([{"a": 1}, {"a": 2}])  # triggers flush; first attempt fails
        self.assertEqual(w.health, HEALTH_UNREACHABLE)
        self.assertEqual(w.buffered, 2)
        # Next flush succeeds against the in-memory connection.
        w.flush()
        self.assertEqual(w.health, HEALTH_OK)
        self.assertEqual(w.buffered, 0)

    def test_buffer_overflow_raises_buffer_full(self):
        def always_fail(cfg):
            raise sqlite3.OperationalError("simulated disk full")

        w = SqliteWriter.from_env(
            table="spots", mode="psk",
            env={"SIGMOND_SQLITE_PATH": "/nonexistent/dir/sink.db"},
            batch_rows=3, connect_factory=always_fail,
        )
        with self.assertRaises(BufferFull):
            for i in range(7):
                w.insert([{"i": i}])
        self.assertEqual(w.health, HEALTH_DEGRADED)


class TestWriterFromEnvDispatch(unittest.TestCase):
    """`Writer.from_env` must hand back a SqliteWriter when SQLITE_PATH set,
    a ClickHouse Writer when only CLICKHOUSE_URL set, and a noop otherwise."""

    def setUp(self):
        self.db_path = _temp_db_path()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.db_path + suffix)
            if p.exists():
                p.unlink()

    def test_sqlite_path_selects_sqlite_writer(self):
        w = Writer.from_env(
            table="spots", mode="psk",
            env={"SIGMOND_SQLITE_PATH": self.db_path},
        )
        self.assertIsInstance(w, SqliteWriter)
        self.assertFalse(w.is_noop)

    def test_clickhouse_url_selects_clickhouse_writer(self):
        w = Writer.from_env(
            table="spots", mode="psk",
            env={"SIGMOND_CLICKHOUSE_URL": "http://localhost:8123"},
        )
        self.assertIsInstance(w, Writer)
        self.assertNotIsInstance(w, SqliteWriter)
        self.assertFalse(w.is_noop)

    def test_clickhouse_wins_when_both_env_vars_set(self):
        # ClickHouse is an explicit opt-in (operator set the URL on
        # purpose), so when both vars are present it takes precedence
        # over a stale or coexisting SIGMOND_SQLITE_PATH.
        w = Writer.from_env(
            table="spots", mode="psk",
            env={
                "SIGMOND_SQLITE_PATH": self.db_path,
                "SIGMOND_CLICKHOUSE_URL": "http://localhost:8123",
            },
        )
        self.assertIsInstance(w, Writer)
        self.assertNotIsInstance(w, SqliteWriter)

    def test_neither_set_with_no_default_dir_yields_noop(self):
        # When /var/lib/sigmond doesn't exist and can't be created, the
        # fallback is no-op (preserves standalone-safety).  We force this
        # by monkeypatching the writability probe to return False.
        from sigmond.hamsci_ch import writer as writer_mod
        original = writer_mod._default_sqlite_writable
        writer_mod._default_sqlite_writable = lambda _p: False
        try:
            w = Writer.from_env(table="spots", mode="psk", env={})
            self.assertIsInstance(w, Writer)
            self.assertTrue(w.is_noop)
        finally:
            writer_mod._default_sqlite_writable = original

    def test_neither_set_with_writable_default_yields_sqlite(self):
        # The new default: SQLite at /var/lib/sigmond/sink.db when the
        # parent dir is writable.  Inject a temp dir so the test doesn't
        # need /var/lib/sigmond on the host.
        tmpdir = tempfile.mkdtemp()
        try:
            from sigmond.hamsci_ch import writer as writer_mod
            original_path = writer_mod._DEFAULT_SQLITE_PATH
            writer_mod._DEFAULT_SQLITE_PATH = str(Path(tmpdir) / "sink.db")
            try:
                w = Writer.from_env(table="spots", mode="psk", env={})
                self.assertIsInstance(w, SqliteWriter)
                self.assertFalse(w.is_noop)
            finally:
                writer_mod._DEFAULT_SQLITE_PATH = original_path
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestTimeBasedAutoFlush(unittest.TestCase):
    """auto_flush_seconds bounds in-memory residency.  Without it a slow
    stream's buffer could sit for hours before the first write to disk —
    a data-loss-on-crash trap and the same bug that bit psk-recorder
    on its first SQLite run."""

    def setUp(self):
        self.db_path = _temp_db_path()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.db_path + suffix)
            if p.exists():
                p.unlink()

    def _row_count(self) -> int:
        if not Path(self.db_path).exists():
            return 0
        conn = sqlite3.connect(self.db_path)
        try:
            r = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='pending_uploads'"
            ).fetchone()
            if r is None:
                return 0
            return conn.execute(
                "SELECT count(*) FROM pending_uploads"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_age_trigger_fires_when_seconds_elapsed(self):
        # batch_rows=1000 (high) so size never trips; auto_flush=0.05s.
        w = SqliteWriter.from_env(
            table="spots", mode="psk",
            env={"SIGMOND_SQLITE_PATH": self.db_path},
            batch_rows=1000, auto_flush_seconds=0.05,
        )
        w.insert([{"a": 1}])
        self.assertEqual(self._row_count(), 0)  # not flushed yet
        import time as _time
        _time.sleep(0.08)
        w.insert([{"a": 2}])
        # After the sleep, the next insert sees age >= threshold and
        # flushes the whole accumulated buffer.
        self.assertEqual(self._row_count(), 2)

    def test_age_trigger_disabled_when_zero(self):
        w = SqliteWriter.from_env(
            table="spots", mode="psk",
            env={"SIGMOND_SQLITE_PATH": self.db_path},
            batch_rows=10, auto_flush_seconds=0,
        )
        w.insert([{"a": 1}])
        import time as _time
        _time.sleep(0.05)
        w.insert([{"a": 2}])
        # With auto_flush_seconds=0, no age trigger; under batch_rows
        # threshold so still buffered.
        self.assertEqual(self._row_count(), 0)
        self.assertEqual(w.buffered, 2)


class TestDispatchScalesBatchRowsForSqlite(unittest.TestCase):
    """`Writer.from_env` should hand SqliteWriter a small default
    batch_rows even when the caller relies on the CH-shaped default
    of 50_000 — otherwise slow streams sit in memory for hours."""

    def setUp(self):
        self.db_path = _temp_db_path()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.db_path + suffix)
            if p.exists():
                p.unlink()

    def test_default_batch_rows_scaled_down_for_sqlite(self):
        from sigmond.hamsci_ch.sqlite_writer import DEFAULT_SQLITE_BATCH_ROWS
        w = Writer.from_env(
            table="spots", mode="psk",
            env={"SIGMOND_SQLITE_PATH": self.db_path},
            # No batch_rows arg — caller uses Writer.from_env's default.
        )
        self.assertIsInstance(w, SqliteWriter)
        self.assertEqual(w.batch_rows, DEFAULT_SQLITE_BATCH_ROWS)

    def test_explicit_batch_rows_honored(self):
        w = Writer.from_env(
            table="spots", mode="psk",
            env={"SIGMOND_SQLITE_PATH": self.db_path},
            batch_rows=42,
        )
        self.assertIsInstance(w, SqliteWriter)
        self.assertEqual(w.batch_rows, 42)


if __name__ == "__main__":
    unittest.main()
