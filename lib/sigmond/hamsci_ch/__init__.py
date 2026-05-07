"""HamSCI ClickHouse writer-side primitive (CONTRACT §17.5).

Imported by sigmond clients that opt into a ClickHouse `data_sink`.
Reads `SIGMOND_CLICKHOUSE_*` env vars from coordination.env, batches
inserts, falls back to a no-op when CH is not configured so a client
remains standalone-safe (CONTRACT §17.5 item 4).

Sibling library to the future `hs-uploader` (reader/shipper side); the
two share schema knowledge through migration files but no code today.

Not threadsafe: instantiate one Writer per producer thread, or
serialize calls externally.
"""

from .writer import (
    BufferFull,
    ConnectionConfig,
    Writer,
)

__all__ = ["Writer", "BufferFull", "ConnectionConfig"]
