"""Tests for sigmond.hamsci_ch.Writer (CONTRACT §17.5)."""

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.hamsci_ch import BufferFull, ConnectionConfig, Writer
from sigmond.hamsci_ch.writer import (
    HEALTH_NOOP, HEALTH_OK, HEALTH_STALE_SCHEMA,
    HEALTH_UNREACHABLE, HEALTH_DEGRADED,
    resolve_db_alias,
)


class FakeQueryResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeClient:
    """Records calls so tests can assert on what the writer sent.

    `describe_columns` is the schema the fake returns from DESCRIBE.
    `fail_insert_n` lets a test simulate transient failures: if >0,
    the next N inserts raise.
    """

    def __init__(self, describe_columns=None, fail_insert_n=0,
                 fail_describe=False):
        self.describe_columns = describe_columns or [
            ("time", "DateTime64(3)"),
            ("snr_db", "Float32"),
        ]
        self.fail_insert_n = fail_insert_n
        self.fail_describe = fail_describe
        self.inserts = []          # list of (table, rows)
        self.queries = []
        self.closed = False

    def query(self, sql):
        self.queries.append(sql)
        if self.fail_describe:
            raise RuntimeError("simulated describe failure")
        return FakeQueryResult(self.describe_columns)

    def insert(self, table, rows, column_names=None):
        # Real clickhouse_connect accepts an optional `column_names=`
        # kwarg; writer.py uses it when inserting list-of-dicts so
        # default columns (e.g. ingested_at) get populated server-side.
        # The fake stores it alongside the rows for tests that care.
        if self.fail_insert_n > 0:
            self.fail_insert_n -= 1
            raise RuntimeError("simulated insert failure")
        self.inserts.append((table, list(rows), column_names))

    def close(self):
        self.closed = True


def _factory_for(client: FakeClient):
    return lambda _cfg: client


class TestNoOpMode(unittest.TestCase):
    """When SIGMOND_CLICKHOUSE_URL is unset, all Writer methods are no-ops
    so a client running standalone (no sigmond) keeps working."""

    def test_from_env_no_url_yields_noop(self):
        w = Writer.from_env(table="spots", mode="psk", env={})
        self.assertTrue(w.is_noop)
        self.assertEqual(w.health, HEALTH_NOOP)

    def test_noop_insert_does_nothing(self):
        w = Writer.from_env(table="spots", mode="psk", env={})
        w.insert([{"a": 1}, {"a": 2}])
        self.assertEqual(w.buffered, 0)
        w.flush()
        w.close()


class TestConnectionConfig(unittest.TestCase):

    def test_from_env_loads_url_user_password_path(self):
        cfg = ConnectionConfig.from_env({
            "SIGMOND_CLICKHOUSE_URL": "http://localhost:8123",
            "SIGMOND_CLICKHOUSE_USER": "sigmond",
            "SIGMOND_CLICKHOUSE_PASSWORD_FILE": "/etc/secret",
        })
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.url, "http://localhost:8123")
        self.assertEqual(cfg.user, "sigmond")
        self.assertEqual(cfg.password_file, "/etc/secret")

    def test_password_returns_empty_when_file_missing(self):
        cfg = ConnectionConfig(
            url="http://x", password_file="/nonexistent/secret"
        )
        self.assertEqual(cfg.password(), "")

    def test_password_reads_and_strips(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("hunter2\n")
            path = f.name
        try:
            cfg = ConnectionConfig(url="http://x", password_file=path)
            self.assertEqual(cfg.password(), "hunter2")
        finally:
            Path(path).unlink()

    def test_resolve_db_alias_uses_env_then_falls_back(self):
        env = {"SIGMOND_CLICKHOUSE_DB_PSK": "psk_local"}
        self.assertEqual(resolve_db_alias("psk", env), "psk_local")
        self.assertEqual(resolve_db_alias("hfdl", env), "hfdl")


class TestEnabledWriter(unittest.TestCase):
    """Writer connected to a fake CH client."""

    def _writer(self, **kwargs) -> tuple[Writer, FakeClient]:
        client = FakeClient(**kwargs)
        env = {"SIGMOND_CLICKHOUSE_URL": "http://localhost:8123"}
        w = Writer.from_env(
            table="spots", mode="psk", env=env,
            batch_rows=3,                          # small for testing
            client_factory=_factory_for(client),
        )
        return w, client

    def test_alias_resolves_database_from_env(self):
        client = FakeClient()
        env = {
            "SIGMOND_CLICKHOUSE_URL": "http://localhost:8123",
            "SIGMOND_CLICKHOUSE_DB_PSK": "psk_alt",
        }
        w = Writer.from_env(
            table="spots", mode="psk", env=env,
            client_factory=_factory_for(client),
        )
        w.insert([{"x": 1}])
        w.flush()
        self.assertEqual(client.inserts[0][0], "psk_alt.spots")

    def test_buffers_until_batch_threshold(self):
        w, client = self._writer()
        w.insert([{"a": 1}, {"a": 2}])
        self.assertEqual(client.inserts, [])             # not flushed yet
        self.assertEqual(w.buffered, 2)
        w.insert([{"a": 3}])                              # crosses batch_rows=3
        self.assertEqual(len(client.inserts), 1)
        self.assertEqual(len(client.inserts[0][1]), 3)
        self.assertEqual(w.buffered, 0)
        self.assertEqual(w.health, HEALTH_OK)

    def test_explicit_flush_drains_buffer(self):
        w, client = self._writer()
        w.insert([{"a": 1}])
        w.flush()
        self.assertEqual(len(client.inserts), 1)
        self.assertEqual(w.buffered, 0)

    def test_close_flushes_and_closes_client(self):
        w, client = self._writer()
        w.insert([{"a": 1}])
        w.close()
        self.assertEqual(len(client.inserts), 1)
        self.assertTrue(client.closed)

    def test_context_manager_closes(self):
        client = FakeClient()
        env = {"SIGMOND_CLICKHOUSE_URL": "http://localhost:8123"}
        with Writer.from_env(
            table="spots", mode="psk", env=env, batch_rows=10,
            client_factory=_factory_for(client),
        ) as w:
            w.insert([{"x": 1}])
        self.assertTrue(client.closed)
        self.assertEqual(len(client.inserts), 1)

    def test_describe_runs_once_at_first_flush(self):
        w, client = self._writer()
        w.insert([{"a": 1}])
        w.flush()
        w.insert([{"a": 2}])
        w.flush()
        # Only one DESCRIBE — schema check is one-shot
        describe_count = sum(1 for q in client.queries if q.startswith("DESCRIBE"))
        self.assertEqual(describe_count, 1)


class TestSchemaHashCheck(unittest.TestCase):

    def test_matching_hash_keeps_health_ok(self):
        client = FakeClient(describe_columns=[
            ("time", "DateTime64(3)"),
            ("snr_db", "Float32"),
        ])
        # Compute the hash the writer would compute.
        import hashlib
        sig = "time:DateTime64(3)\nsnr_db:Float32"
        expected = hashlib.sha256(sig.encode()).hexdigest()[:16]
        env = {"SIGMOND_CLICKHOUSE_URL": "http://localhost:8123"}
        w = Writer.from_env(
            table="spots", mode="psk", env=env,
            expected_column_hash=expected, batch_rows=1,
            client_factory=_factory_for(client),
        )
        w.insert([{"x": 1}])
        self.assertEqual(w.health, HEALTH_OK)

    def test_mismatched_hash_marks_stale_but_proceeds(self):
        client = FakeClient(describe_columns=[("time", "DateTime")])
        env = {"SIGMOND_CLICKHOUSE_URL": "http://localhost:8123"}
        w = Writer.from_env(
            table="spots", mode="psk", env=env,
            expected_column_hash="0000000000000000", batch_rows=1,
            client_factory=_factory_for(client),
        )
        w.insert([{"x": 1}])
        self.assertEqual(w.health, HEALTH_STALE_SCHEMA)
        # Insert still went through — stale-schema is a warning, not a block.
        self.assertEqual(len(client.inserts), 1)

    def test_describe_failure_marks_degraded(self):
        client = FakeClient(fail_describe=True)
        env = {"SIGMOND_CLICKHOUSE_URL": "http://localhost:8123"}
        w = Writer.from_env(
            table="spots", mode="psk", env=env, batch_rows=1,
            client_factory=_factory_for(client),
        )
        w.insert([{"x": 1}])    # buffers
        # Buffer remains; flush failed silently with degraded health.
        self.assertEqual(w.health, HEALTH_DEGRADED)
        self.assertEqual(len(client.inserts), 0)


class TestUnreachableHandling(unittest.TestCase):
    """CONTRACT §17.5 item 2: connection failure is non-fatal; silent loss
    is forbidden. Buffer up to 2x batch_rows; raise BufferFull beyond."""

    def test_transient_failure_keeps_buffer_marks_unreachable(self):
        client = FakeClient(fail_insert_n=1)
        env = {"SIGMOND_CLICKHOUSE_URL": "http://localhost:8123"}
        w = Writer.from_env(
            table="spots", mode="psk", env=env, batch_rows=2,
            client_factory=_factory_for(client),
        )
        w.insert([{"a": 1}, {"a": 2}])  # triggers flush; first insert fails
        self.assertEqual(w.health, HEALTH_UNREACHABLE)
        self.assertEqual(w.buffered, 2)         # buffer retained
        # Next flush succeeds (fail_insert_n already decremented to 0).
        w.flush()
        self.assertEqual(w.health, HEALTH_OK)
        self.assertEqual(w.buffered, 0)
        self.assertEqual(len(client.inserts), 1)

    def test_buffer_overflow_raises_buffer_full(self):
        # batch_rows=3 means buffer_max=6. Insert 7 with CH always failing.
        client = FakeClient(fail_insert_n=999)
        env = {"SIGMOND_CLICKHOUSE_URL": "http://localhost:8123"}
        w = Writer.from_env(
            table="spots", mode="psk", env=env, batch_rows=3,
            client_factory=_factory_for(client),
        )
        # First flush attempt fails silently at row 3, buffer retained.
        # Second flush attempt fails at row 6, buffer retained.
        # Adding 1 more would push to 7 > buffer_max=6 → BufferFull.
        with self.assertRaises(BufferFull):
            for i in range(7):
                w.insert([{"i": i}])
        self.assertEqual(w.health, HEALTH_DEGRADED)


if __name__ == "__main__":
    unittest.main()
