"""ClickHouse writer for HamSCI clients (CONTRACT §17.5).

Behavior:
- Reads connection facts from `SIGMOND_CLICKHOUSE_*` env vars at construction.
- No-op when `SIGMOND_CLICKHOUSE_URL` is unset (standalone-safe).
- Buffers rows; flushes at `batch_rows` (default 50k, matching wsprdaemon-server).
- On flush failure, retains the buffer and reports `health = "unreachable"`.
- Beyond `2 * batch_rows` buffered, `insert()` raises `BufferFull` —
  silent loss is a contract violation (CONTRACT §17.5 item 2).
- Validates table existence on first connect; optionally checks a column
  hash for schema-drift detection (`health = "stale-schema"`).
- `clickhouse-connect` is lazy-imported only when the writer actually
  connects, so sigmond's core stays stdlib-only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger("sigmond.hamsci_ch")


# Health values per CONTRACT §17.3 (plus `noop` for the standalone case).
HEALTH_OK = "ok"
HEALTH_UNREACHABLE = "unreachable"
HEALTH_STALE_SCHEMA = "stale-schema"
HEALTH_DEGRADED = "degraded"
HEALTH_NOOP = "noop"


# Default SQLite buffer path used when no backend is explicitly configured.
# Lives under sigmond's state dir so operators can find it for backup,
# disk-budget accounting, and the future hs-uploader's reader.
_DEFAULT_SQLITE_PATH = "/var/lib/sigmond/sink.db"


def _default_sqlite_writable(path: str) -> bool:
    """True iff the default SQLite path is usable without explicit config.

    A directory is "usable" when (a) it already exists and is writable
    by the current process, OR (b) its parent exists and is writable
    (so the directory itself can be created on first connect).  This
    keeps standalone clients — no sigmond install, no /var/lib/sigmond
    — falling back to no-op instead of erroring on every flush.
    """
    parent = Path(path).parent
    if parent.exists():
        return os.access(parent, os.W_OK)
    grandparent = parent.parent
    return grandparent.exists() and os.access(grandparent, os.W_OK)


class BufferFull(Exception):
    """Buffer reached `2 * batch_rows` while CH was unreachable.

    Raised by `Writer.insert()` so the caller cannot silently lose rows.
    Callers handle this however they like (sidecar file, drop-with-metric,
    refuse-new-work) — the contract just forbids silent loss.
    """


@dataclass
class ConnectionConfig:
    """Resolved ClickHouse connection from coordination.env (§17.5 item 1)."""

    url: str
    user: str = "default"
    password_file: Optional[str] = None

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> Optional["ConnectionConfig"]:
        """Build from os.environ (or override). Returns None if URL unset."""
        e = env if env is not None else os.environ
        url = (e.get("SIGMOND_CLICKHOUSE_URL") or "").strip()
        if not url:
            return None
        return cls(
            url=url,
            user=e.get("SIGMOND_CLICKHOUSE_USER", "default"),
            password_file=e.get("SIGMOND_CLICKHOUSE_PASSWORD_FILE") or None,
        )

    def password(self) -> str:
        """Read password from `password_file`. Empty string if absent."""
        if not self.password_file:
            return ""
        try:
            return Path(self.password_file).read_text().strip()
        except OSError:
            return ""


def resolve_db_alias(mode: str, env: Optional[dict] = None) -> str:
    """Resolve `SIGMOND_CLICKHOUSE_DB_<MODE>` (§17.5 item 1)."""
    e = env if env is not None else os.environ
    return e.get(f"SIGMOND_CLICKHOUSE_DB_{mode.upper()}", mode)


class Writer:
    """Writer for one `<database>.<table>` sink.

    Use `Writer.from_env(...)` to construct from coordination.env.  Pass
    a `client_factory` in tests to inject a fake CH client without a
    running server.
    """

    def __init__(
        self,
        database: str,
        table: str,
        *,
        schema_version: int = 0,
        expected_column_hash: Optional[str] = None,
        batch_rows: int = 50_000,
        config: Optional[ConnectionConfig] = None,
        client_factory: Optional[Callable[[ConnectionConfig], Any]] = None,
    ) -> None:
        self.database = database
        self.table = table
        self.schema_version = schema_version
        self.expected_column_hash = expected_column_hash
        self.batch_rows = batch_rows
        self._buffer_max = batch_rows * 2
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._buffer: list = []
        self._client: Any = None
        self._schema_checked = False
        self._health = HEALTH_NOOP if config is None else HEALTH_OK

    @classmethod
    def from_env(
        cls,
        table: str,
        *,
        mode: str,
        database: Optional[str] = None,
        schema_version: int = 0,
        expected_column_hash: Optional[str] = None,
        batch_rows: int = 50_000,
        env: Optional[dict] = None,
        client_factory: Optional[Callable[[ConnectionConfig], Any]] = None,
    ) -> Any:
        """Build a writer from coordination.env.

        Backend selection (in order):
        1. `SIGMOND_CLICKHOUSE_URL` set → ClickHouse `Writer` (explicit
           opt-in; matches upstream wsprdaemon-server shape).
        2. `SIGMOND_SQLITE_PATH` set → `SqliteWriter` at that path
           (explicit override; useful for tests or unusual layouts).
        3. Neither set → `SqliteWriter` at the default sigmond path
           `/var/lib/sigmond/sink.db`, IF that directory is writable.
           Otherwise no-op (preserves standalone-safety for clients
           running outside a sigmond install).

        SQLite is the default because a sigmond client host's local
        sink is just a store-and-forward buffer for `hs-uploader`;
        ClickHouse-as-buffer would burn 1-2 GB of RAM and several
        merge-CPU cores for no benefit there.  Hosts that need the
        upstream-grade columnar tier opt in with `SIGMOND_CLICKHOUSE_URL`.

        `mode` is the per-mode key (`wspr`, `psk`, `hfdl`, `codar`,
        `timestd`).  The actual database name is resolved through
        `SIGMOND_CLICKHOUSE_DB_<MODE>` (or `SIGMOND_SQLITE_DB_<MODE>` on
        the SQLite path) so operators can rename per-host without client
        changes.  Pass `database=` to bypass the alias.

        Returns a `Writer` or `SqliteWriter`; both expose the same
        `insert/flush/close/health/is_noop/buffered` interface.
        """
        e = env if env is not None else os.environ
        ch_url = (e.get("SIGMOND_CLICKHOUSE_URL") or "").strip()
        sqlite_path = (e.get("SIGMOND_SQLITE_PATH") or "").strip()

        # Default to SQLite at the sigmond state path when neither
        # backend is explicitly configured.  Only do so when the parent
        # directory is writable — a true standalone client (no sigmond
        # install) should not silently start writing to /var/lib/sigmond.
        if not ch_url and not sqlite_path:
            default_path = _DEFAULT_SQLITE_PATH
            if _default_sqlite_writable(default_path):
                sqlite_path = default_path

        if sqlite_path and not ch_url:
            # Lazy import keeps the CH path free of sqlite3 import cost
            # and avoids a hard cycle between writer.py and sqlite_writer.py.
            from .sqlite_writer import (
                SqliteWriter, DEFAULT_SQLITE_BATCH_ROWS,
            )
            # When the caller passes the CH-appropriate 50_000 default
            # (i.e., didn't override), scale down to the SQLite default.
            # Without this, a producer like psk-recorder would accumulate
            # ~3 hours of spots in-memory before the first flush — a
            # silent data-loss-on-crash trap.  Callers that genuinely
            # want larger batches just pass an explicit batch_rows.
            sqlite_batch_rows = (
                DEFAULT_SQLITE_BATCH_ROWS if batch_rows == 50_000
                else batch_rows
            )
            effective_env = dict(e)
            effective_env["SIGMOND_SQLITE_PATH"] = sqlite_path
            return SqliteWriter.from_env(
                table=table,
                mode=mode,
                database=database,
                schema_version=schema_version,
                batch_rows=sqlite_batch_rows,
                env=effective_env,
            )
        cfg = ConnectionConfig.from_env(env)
        actual_db = database or resolve_db_alias(mode, env)
        return cls(
            database=actual_db,
            table=table,
            schema_version=schema_version,
            expected_column_hash=expected_column_hash,
            batch_rows=batch_rows,
            config=cfg,
            client_factory=client_factory,
        )

    @property
    def health(self) -> str:
        return self._health

    @property
    def is_noop(self) -> bool:
        return self._config is None

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def insert(self, rows: Sequence) -> None:
        """Buffer rows; auto-flush when `batch_rows` is reached.

        Raises `BufferFull` if the buffer would exceed `2 * batch_rows`
        (CH unreachable for too long).
        """
        if self.is_noop or not rows:
            return
        self._buffer.extend(rows)
        if len(self._buffer) > self._buffer_max:
            self._health = HEALTH_DEGRADED
            buffered = len(self._buffer)
            self._buffer = self._buffer[: self._buffer_max]
            raise BufferFull(
                f"hamsci_ch buffer overflow: {buffered} rows pending, "
                f"max {self._buffer_max} (CH unreachable at "
                f"{self._config.url if self._config else '?'})"
            )
        if len(self._buffer) >= self.batch_rows:
            self.flush()

    def flush(self) -> None:
        """Force a flush. Quiet on transient failures (buffer retained)."""
        if self.is_noop or not self._buffer:
            return
        try:
            client = self._connect()
            if not self._schema_checked:
                self._verify_schema(client)
            # clickhouse-connect's client.insert(table, rows) accepts
            # either a list-of-lists with explicit column_names, or a
            # list-of-dicts.  When given dicts WITHOUT column_names it
            # fails with "Insert data column count does not match column
            # names" because the table has DEFAULT columns (e.g.
            # ingested_at) that aren't in the row dicts.  Convert dict
            # rows to (data, column_names) form so the inserted columns
            # are explicit and DEFAULT columns get populated server-side.
            if self._buffer and isinstance(self._buffer[0], dict):
                column_names = list(self._buffer[0].keys())
                data = [[row.get(c) for c in column_names] for row in self._buffer]
                client.insert(
                    f"{self.database}.{self.table}", data,
                    column_names=column_names,
                )
            else:
                client.insert(f"{self.database}.{self.table}", self._buffer)
            self._buffer = []
            if self._health != HEALTH_STALE_SCHEMA:
                self._health = HEALTH_OK
        except BufferFull:
            raise
        except Exception as e:
            self._client = None
            # Preserve degraded (structural error like missing table) over
            # unreachable (transient network).  Different remediation paths.
            if self._health != HEALTH_DEGRADED:
                self._health = HEALTH_UNREACHABLE
            logger.warning(
                "hamsci_ch: flush failed for %s.%s (%d rows buffered): %s",
                self.database, self.table, len(self._buffer), e,
            )

    def close(self) -> None:
        try:
            self.flush()
        finally:
            if self._client is not None and hasattr(self._client, "close"):
                try:
                    self._client.close()
                except Exception:
                    pass
            self._client = None

    def __enter__(self) -> "Writer":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _connect(self) -> Any:
        if self._client is None and self._config is not None:
            self._client = self._client_factory(self._config)
        return self._client

    def _verify_schema(self, client: Any) -> None:
        try:
            result = client.query(f"DESCRIBE TABLE {self.database}.{self.table}")
            rows = getattr(result, "result_rows", None) or list(result)
        except Exception as e:
            self._health = HEALTH_DEGRADED
            raise RuntimeError(
                f"hamsci_ch: table {self.database}.{self.table} "
                f"does not exist or DESCRIBE failed: {e}"
            ) from e

        if self.expected_column_hash:
            import hashlib
            sig = "\n".join(f"{r[0]}:{r[1]}" for r in rows)
            actual = hashlib.sha256(sig.encode()).hexdigest()[:16]
            if actual != self.expected_column_hash:
                self._health = HEALTH_STALE_SCHEMA
                logger.warning(
                    "hamsci_ch: schema hash mismatch for %s.%s "
                    "(expected %s, got %s) — proceeding; CH will reject "
                    "inserts if columns are incompatible",
                    self.database, self.table,
                    self.expected_column_hash, actual,
                )

        self._schema_checked = True


def _default_client_factory(config: ConnectionConfig) -> Any:
    """Lazy-import `clickhouse_connect` so sigmond core stays stdlib-only."""
    try:
        import clickhouse_connect  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "hamsci_ch.Writer requires clickhouse-connect. Install with "
            "`pip install sigmond[clickhouse]` or add the dep to your client's venv."
        ) from e

    from urllib.parse import urlparse

    u = urlparse(config.url)
    return clickhouse_connect.get_client(
        host=u.hostname,
        port=u.port or 8123,
        username=config.user,
        password=config.password(),
    )
