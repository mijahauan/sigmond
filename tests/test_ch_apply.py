"""Tests for sigmond.commands.ch_apply — the schema-migration runner.

Covers:
  * Discovery: walks the catalog root for `[clickhouse]` blocks,
    skipping repos without a schema_dir / with bad TOML / with a
    missing schema directory.
  * Migration execution: runs `[0-9]*.sql` files in order, halts a
    client's run on first error but continues to the next client.
  * Dry-run mode: lists what would happen without connecting.
  * Top-level entrypoint is a no-op when `[storage.clickhouse]` is
    absent.
"""
from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.commands.ch_apply import (
    ClientCh,
    MigrationResult,
    apply_ch_schemas,
    discover_clients_with_ch_schemas,
    list_migrations,
    run_client_migrations,
    summarise,
)
from sigmond.coordination import (
    ClickHouseStorage,
    Coordination,
    Storage,
)


def _coord_with_ch(ch: ClickHouseStorage = None) -> Coordination:
    return Coordination(storage=Storage(clickhouse=ch))


def _make_client_repo(
    catalog_root: Path,
    client_name: str,
    ch_block: str,
    schema_files: dict[str, str],
) -> Path:
    """Lay down a fake repo at catalog_root/client_name with a deploy.toml
    plus the named SQL files in the per-client schema_dir."""
    repo = catalog_root / client_name
    repo.mkdir(parents=True, exist_ok=True)
    deploy = textwrap.dedent(f"""
        [package]
        name = "{client_name}"
        version = "0.1.0"

        {ch_block}
    """)
    (repo / "deploy.toml").write_text(deploy)
    # Materialise schema files.
    for rel_path, content in schema_files.items():
        sql = repo / rel_path
        sql.parent.mkdir(parents=True, exist_ok=True)
        sql.write_text(content)
    return repo


class TestDiscovery(unittest.TestCase):

    def test_discovers_client_with_clickhouse_block(self):
        with _TempCatalog() as root:
            _make_client_repo(
                root, "psk-recorder",
                ch_block=textwrap.dedent("""
                    [clickhouse]
                    database       = "psk"
                    schema_dir     = "clickhouse/schema/psk"
                    schema_version = 2
                    required_min_ch = "23.8"
                """),
                schema_files={
                    "clickhouse/schema/psk/000_create_database.sql":
                        "CREATE DATABASE IF NOT EXISTS psk;",
                    "clickhouse/schema/psk/001_create_spots.sql":
                        "CREATE TABLE IF NOT EXISTS psk.spots (time DateTime) "
                        "ENGINE = MergeTree() ORDER BY time;",
                },
            )
            clients = discover_clients_with_ch_schemas(root)
            self.assertEqual(len(clients), 1)
            self.assertEqual(clients[0].client_name, "psk-recorder")
            self.assertEqual(clients[0].database, "psk")
            self.assertEqual(clients[0].schema_version, 2)
            self.assertTrue(str(clients[0].schema_dir).endswith(
                "psk-recorder/clickhouse/schema/psk"
            ))

    def test_skips_repo_without_clickhouse_block(self):
        with _TempCatalog() as root:
            _make_client_repo(
                root, "wspr-recorder",
                ch_block="",          # no [clickhouse]
                schema_files={},
            )
            clients = discover_clients_with_ch_schemas(root)
            self.assertEqual(clients, [])

    def test_skips_empty_schema_dir_reference(self):
        """wsprdaemon-client uses schema_dir='' to point at the wire-pinned
        WSPR schema in sigmond-clickhouse — no client-side schema work."""
        with _TempCatalog() as root:
            _make_client_repo(
                root, "wsprdaemon-client",
                ch_block=textwrap.dedent("""
                    [clickhouse]
                    database   = "wspr"
                    schema_dir = ""
                    schema_ref = "wsprdaemon:1"
                """),
                schema_files={},
            )
            clients = discover_clients_with_ch_schemas(root)
            self.assertEqual(clients, [])

    def test_skips_repo_with_missing_schema_directory(self):
        with _TempCatalog() as root:
            _make_client_repo(
                root, "broken-client",
                ch_block=textwrap.dedent("""
                    [clickhouse]
                    database   = "broken"
                    schema_dir = "schemas/that/do/not/exist"
                """),
                schema_files={},      # don't actually create the dir
            )
            clients = discover_clients_with_ch_schemas(root)
            self.assertEqual(clients, [])

    def test_orders_clients_by_directory_name(self):
        with _TempCatalog() as root:
            for name in ("zeta", "alpha", "beta"):
                _make_client_repo(
                    root, name,
                    ch_block=textwrap.dedent(f"""
                        [clickhouse]
                        database   = "{name}"
                        schema_dir = "schema/{name}"
                    """),
                    schema_files={
                        f"schema/{name}/000_create.sql":
                            f"CREATE DATABASE IF NOT EXISTS {name};",
                    },
                )
            clients = discover_clients_with_ch_schemas(root)
            self.assertEqual(
                [c.client_name for c in clients],
                ["alpha", "beta", "zeta"],
            )


class TestListMigrations(unittest.TestCase):

    def test_lists_numeric_prefixed_sql_files_in_order(self):
        with _TempCatalog() as root:
            d = root / "schema-test"
            d.mkdir()
            for name in (
                "002_alter.sql",
                "001_create.sql",
                "000_db.sql",
                "README.md",        # non-SQL — skipped
                "alter.sql",        # no numeric prefix — skipped
            ):
                (d / name).write_text("-- ok")
            files = list_migrations(d)
            self.assertEqual(
                [f.name for f in files],
                ["000_db.sql", "001_create.sql", "002_alter.sql"],
            )

    def test_returns_empty_for_missing_dir(self):
        self.assertEqual(list_migrations(Path("/nope/never")), [])


class FakeChClient:
    """Records every command() the runner issues."""

    def __init__(self, fail_on_substring: str | None = None):
        self.commands: list[str] = []
        self.fail_on_substring = fail_on_substring
        self.closed = False

    def command(self, sql: str) -> None:
        if self.fail_on_substring and self.fail_on_substring in sql:
            raise RuntimeError(f"simulated CH error on {self.fail_on_substring!r}")
        self.commands.append(sql)

    def close(self) -> None:
        self.closed = True


class TestRunClientMigrations(unittest.TestCase):

    def _client(self, root: Path) -> ClientCh:
        d = root / "schemas"
        d.mkdir()
        (d / "000_create.sql").write_text("CREATE DATABASE IF NOT EXISTS foo;")
        (d / "001_table.sql").write_text(
            "CREATE TABLE IF NOT EXISTS foo.t (x Int32) "
            "ENGINE = MergeTree() ORDER BY x;"
        )
        (d / "002_alter.sql").write_text(
            "ALTER TABLE foo.t ADD COLUMN IF NOT EXISTS y Int32;"
        )
        return ClientCh(
            client_name="test", repo_dir=root,
            database="foo", schema_dir=d,
            schema_version=3, required_min_ch=None,
        )

    def test_runs_each_file_in_order_on_clean_path(self):
        with _TempCatalog() as root:
            client = self._client(root)
            ch = FakeChClient()
            result = run_client_migrations(client, ch_client=ch, dry_run=False)
            self.assertEqual(len(ch.commands), 3)
            self.assertIn("CREATE DATABASE", ch.commands[0])
            self.assertEqual(result.error, None)
            self.assertEqual(len(result.applied), 3)
            self.assertEqual(result.skipped, [])

    def test_halts_after_first_error_and_records_it(self):
        with _TempCatalog() as root:
            client = self._client(root)
            ch = FakeChClient(fail_on_substring="ALTER TABLE")
            result = run_client_migrations(client, ch_client=ch, dry_run=False)
            # Two succeed; the ALTER fails; the run halts.
            self.assertEqual(len(ch.commands), 2)
            self.assertIsNotNone(result.error)
            self.assertIn("002_alter.sql", result.error)
            self.assertEqual(len(result.applied), 2)
            self.assertEqual(len(result.skipped), 1)

    def test_dry_run_does_not_invoke_client(self):
        with _TempCatalog() as root:
            client = self._client(root)
            ch = FakeChClient()           # would error if called
            result = run_client_migrations(client, ch_client=None, dry_run=True)
            self.assertEqual(ch.commands, [])
            self.assertEqual(len(result.applied), 3)
            self.assertTrue(all(a.startswith("(dry-run)") for a in result.applied))


class TestApplyChSchemas(unittest.TestCase):

    def test_noop_when_storage_clickhouse_not_configured(self):
        with _TempCatalog() as root:
            _make_client_repo(
                root, "psk-recorder",
                ch_block=textwrap.dedent("""
                    [clickhouse]
                    database   = "psk"
                    schema_dir = "schema/psk"
                """),
                schema_files={"schema/psk/000_db.sql": "CREATE DATABASE IF NOT EXISTS psk;"},
            )
            # No [storage.clickhouse] in coord — apply returns empty list.
            results = apply_ch_schemas(_coord_with_ch(None), catalog_root=root)
            self.assertEqual(results, [])

    def test_iterates_every_discovered_client(self):
        with _TempCatalog() as root:
            for name, db in (("psk-recorder", "psk"), ("hfdl-recorder", "hfdl")):
                _make_client_repo(
                    root, name,
                    ch_block=textwrap.dedent(f"""
                        [clickhouse]
                        database   = "{db}"
                        schema_dir = "clickhouse/schema/{db}"
                    """),
                    schema_files={
                        f"clickhouse/schema/{db}/000_db.sql":
                            f"CREATE DATABASE IF NOT EXISTS {db};",
                    },
                )
            ch = ClickHouseStorage(host="localhost", http_port=8123)
            captured: list[FakeChClient] = []

            def fake_factory(_storage):
                client = FakeChClient()
                captured.append(client)
                return client

            results = apply_ch_schemas(
                _coord_with_ch(ch),
                dry_run=False, catalog_root=root,
                client_factory=fake_factory,
            )
            self.assertEqual(len(results), 2)
            self.assertEqual({r.client_name for r in results},
                             {"psk-recorder", "hfdl-recorder"})
            self.assertEqual(len(captured), 1)            # one shared client
            self.assertEqual(len(captured[0].commands), 2)
            self.assertTrue(captured[0].closed)

    def test_one_client_failure_does_not_abort_the_others(self):
        """Failure on client A doesn't prevent client B from running."""
        with _TempCatalog() as root:
            _make_client_repo(
                root, "alpha",
                ch_block=textwrap.dedent("""
                    [clickhouse]
                    database   = "alpha"
                    schema_dir = "schema/alpha"
                """),
                schema_files={
                    "schema/alpha/000_create.sql":
                        "BROKEN SQL THAT WILL FAIL;",
                },
            )
            _make_client_repo(
                root, "beta",
                ch_block=textwrap.dedent("""
                    [clickhouse]
                    database   = "beta"
                    schema_dir = "schema/beta"
                """),
                schema_files={
                    "schema/beta/000_create.sql":
                        "CREATE DATABASE IF NOT EXISTS beta;",
                },
            )
            ch = ClickHouseStorage(host="localhost", http_port=8123)
            results = apply_ch_schemas(
                _coord_with_ch(ch),
                dry_run=False, catalog_root=root,
                client_factory=lambda _s: FakeChClient(fail_on_substring="BROKEN SQL"),
            )
            by_client = {r.client_name: r for r in results}
            self.assertIsNotNone(by_client["alpha"].error)
            self.assertIsNone(by_client["beta"].error)
            self.assertEqual(len(by_client["beta"].applied), 1)


class TestSummarise(unittest.TestCase):

    def test_renders_each_result(self):
        results = [
            MigrationResult("a", "alpha", ["a/000.sql", "a/001.sql"], [], None),
            MigrationResult("b", "beta", [], ["b/000.sql (boom)"], "b/000.sql: boom"),
            MigrationResult("c", "gamma", [], [], None),
        ]
        lines = summarise(results)
        self.assertIn("a (alpha): 2 migration(s) applied", lines[0])
        self.assertIn("b (beta): error", lines[1])
        self.assertIn("c (gamma): no migrations to run", lines[2])


# ── helpers ────────────────────────────────────────────────────────────────

class _TempCatalog:
    """tempfile.TemporaryDirectory that yields a Path."""

    def __enter__(self) -> Path:
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        return Path(self._td.name)

    def __exit__(self, *exc):
        self._td.cleanup()
        return False


if __name__ == "__main__":
    unittest.main()
