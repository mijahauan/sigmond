"""HamSCI sink writer primitives (CONTRACT §17).

Producer clients call `Writer.from_env(...)` to get a local-sink
writer.  The backend is SQLite — a store-and-forward queue under
sigmond's state dir that the separate `hs-uploader` drains upstream:

- `SIGMOND_SQLITE_PATH` set → writer at that path (explicit override).
- unset                    → `/var/lib/sigmond/sink.db` if its
  directory is writable, else no-op (preserves standalone-safety for
  clients running outside a sigmond install).

SQLite suits a sigmond client host: the local sink is just a buffer
for `hs-uploader`, and a daemon-backed columnar store would burn
1-2 GB of RAM and several merge-CPU cores for no benefit there.

`BufferFull` is the exception the writer raises on prolonged sink
failure rather than silently losing rows.

Not threadsafe: instantiate one writer per producer thread, or
serialize calls externally.
"""

from .writer import (
    BufferFull,
    SqliteConfig,
    Writer,
)

__all__ = [
    "Writer",
    "BufferFull",
    "SqliteConfig",
]
