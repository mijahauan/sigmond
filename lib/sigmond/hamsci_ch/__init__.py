"""HamSCI sink writer primitives (CONTRACT §17.5).

Producer clients call `Writer.from_env(...)` and get back a backend
chosen at construction time from the environment:

- `SIGMOND_CLICKHOUSE_URL` set  → ClickHouse `Writer` (explicit opt-in;
  matches upstream wsprdaemon-server shape; heavier, OLAP-grade).
- `SIGMOND_SQLITE_PATH` set     → `SqliteWriter` at that path
  (explicit override).
- Neither set                   → `SqliteWriter` at the default sigmond
  state path `/var/lib/sigmond/sink.db`, IF the directory is writable.
  Otherwise no-op (preserves standalone-safety for clients running
  outside a sigmond install).

SQLite is the default because a sigmond client host's local sink is
just a store-and-forward buffer for the future `hs-uploader`; running
ClickHouse-as-buffer would burn 1-2 GB of RAM and several merge-CPU
cores for no benefit there.  Hosts that need the upstream columnar
tier opt in explicitly with `SIGMOND_CLICKHOUSE_URL`.

Both writers expose the same `insert/flush/close/health/is_noop/
buffered` interface, so callers don't branch.  `BufferFull` is the
single exception type either backend raises on prolonged sink failure.

Sibling library to the future `hs-uploader` (reader/shipper side); the
two share schema knowledge through migration files (CH) or JSON
payloads in the queue table (SQLite) but no code today.

Not threadsafe: instantiate one writer per producer thread, or
serialize calls externally.
"""

from .writer import (
    BufferFull,
    ConnectionConfig,
    Writer,
)
from .sqlite_writer import (
    SqliteConfig,
    SqliteWriter,
)

__all__ = [
    "Writer",
    "SqliteWriter",
    "BufferFull",
    "ConnectionConfig",
    "SqliteConfig",
]
