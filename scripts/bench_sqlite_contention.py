#!/usr/bin/env python3
"""Phase-1 benchmark for MULTI-INSTANCE-ARCHITECTURE.md §10.

Validates whether the per-process-per-reporter model is viable by
running N=4 worker processes, each owning its own `hamsci_sink.Writer`,
all writing to a shared temp `sink.db` at WSPR-cycle and FT8-cycle
cadences.  Measures per-flush latency tail and SQLITE_BUSY errors.

Gating thresholds (from spec §10 Phase 1):
  - p99 flush latency < 50 ms for WSPR-cycle burst
  - p99 flush latency < 25 ms for FT8-cycle burst
  - zero SQLITE_BUSY / OperationalError under WAL + 30 s busy_timeout

Run from the sigmond venv:
    /opt/git/sigmond/sigmond/venv/bin/python scripts/bench_sqlite_contention.py
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path

# In-tree import: ensure /opt/git/sigmond/sigmond/lib is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.hamsci_sink import SqliteConfig, Writer  # noqa: E402


# ---------------------------------------------------------------------------
# Realistic spot payload — matches the wspr-recorder / psk-recorder shape
# without depending on those repos at runtime.  Field count + byte size
# matter for SQLite write cost; values are dummies.
# ---------------------------------------------------------------------------
def make_spot_payload(writer_id: int, cycle: int, band: int, spot_n: int) -> dict:
    return {
        "schema_version":   1,
        "cycle_key":        f"2026-05-25T12:{(cycle * 2) % 60:02d}:00Z",
        "host_id":          "bench-host",
        "radiod_id":        f"my-rx888-{writer_id}",
        "rx_source":        f"radiod:my-rx888-{writer_id}",
        "rx_call":          f"AC0G",
        "rx_grid":          "EM38",
        "reporter_id":      f"AC0G-B{writer_id}",
        "band":             f"{band}m",
        "freq_hz":          14097100 + band,
        "callsign":         f"DUM{spot_n:03d}",
        "grid":             "FN42",
        "snr_db":           -15.0 + (spot_n % 30),
        "drift_hz_per_s":   0.0,
        "mode":             "wspr",
        "power_dbm":        20.0,
        "antenna":          "loop",
        "sdr":              "rx888-mk2",
    }


# ---------------------------------------------------------------------------
# Worker — one per reporter.  Runs `cycles` bursts, each burst inserting
# `spots_per_burst` rows in one Writer.flush().  Synchronizes burst start
# across workers via the Barrier so contention is realistic.
# ---------------------------------------------------------------------------
def worker(
    writer_id: int,
    db_path: str,
    mode: str,                  # "wspr" or "ft8"
    cycles: int,
    spots_per_burst: int,
    inter_cycle_sleep_s: float,
    barrier: mp.Barrier,
    results_q: mp.Queue,
) -> None:
    try:
        cfg = SqliteConfig(path=db_path)
        writer = Writer(
            database=mode,
            table="spots",
            schema_version=1,
            batch_rows=spots_per_burst + 10,   # don't auto-flush mid-burst
            auto_flush_seconds=3600,           # disable age-based flush
            config=cfg,
        )

        flush_latencies_ms: list[float] = []
        errors: list[str] = []

        for cycle in range(cycles):
            # Synchronize burst start across all workers.
            barrier.wait(timeout=30)

            # Build the burst payload (timed separately so we can isolate
            # the flush cost).
            spots = [
                make_spot_payload(writer_id, cycle, band, n)
                for band in range(17)               # 17 WSPR bands
                for n in range(spots_per_burst // 17)
            ]
            spots += [
                make_spot_payload(writer_id, cycle, 0, n)
                for n in range(spots_per_burst - len(spots))
            ]
            try:
                writer.insert(spots)
                # batch_rows is large, so insert() buffered them; we flush
                # explicitly to time the SQLite write.
                t0 = time.perf_counter()
                writer.flush()
                t1 = time.perf_counter()
                flush_latencies_ms.append((t1 - t0) * 1000.0)
                # hamsci_sink.Writer.flush() catches all exceptions, logs a
                # warning, and RETAINS the buffer (production-realistic:
                # transient lock failures retry on the next flush).  A
                # non-empty buffer post-flush means the flush silently
                # failed — count it as a contention event.
                if writer.buffered > 0:
                    errors.append(
                        f"cycle={cycle}: silent flush retry "
                        f"({writer.buffered} rows still buffered)"
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"cycle={cycle}: {type(exc).__name__}: {exc}")

            # Inter-cycle gap (sped up vs prod: 0.5 s instead of 120 s for WSPR)
            time.sleep(inter_cycle_sleep_s)

        results_q.put({
            "writer_id": writer_id,
            "latencies_ms": flush_latencies_ms,
            "errors": errors,
        })

    except Exception as exc:  # noqa: BLE001
        # Capture worker-level crash so the parent doesn't hang.
        results_q.put({
            "writer_id": writer_id,
            "latencies_ms": [],
            "errors": [
                f"worker crash: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}"
            ],
        })


# ---------------------------------------------------------------------------
# Scenario driver
# ---------------------------------------------------------------------------
def run_scenario(
    name: str,
    n_writers: int,
    cycles: int,
    spots_per_burst: int,
    inter_cycle_sleep_s: float,
    p99_threshold_ms: float,
) -> bool:
    """Run one scenario; return True on green, False on red."""
    print(f"\n=== Scenario: {name} ===")
    print(f"  writers={n_writers}  cycles={cycles}  "
          f"spots/burst={spots_per_burst}  gap={inter_cycle_sleep_s}s  "
          f"p99 threshold={p99_threshold_ms} ms")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "sink.db")

        # Pre-initialize the schema so the workers don't race on first-
        # flush DDL.  Without this, all N workers do `PRAGMA WAL` + DDL
        # simultaneously and the loser gets "database is locked" until
        # the winner commits — which masquerades as steady-state
        # contention but is really a startup phenomenon.  In production
        # the DB is pre-created by `smd storage migrate-to-sqlite`.
        prewarm_cfg = SqliteConfig(path=db_path)
        prewarm = Writer(database="prewarm", table="spots",
                         schema_version=1, batch_rows=1,
                         config=prewarm_cfg)
        prewarm.insert([{"prewarm": True}])
        prewarm.flush()
        prewarm.close()

        barrier = mp.Barrier(n_writers)
        results_q: mp.Queue = mp.Queue()

        procs = [
            mp.Process(
                target=worker,
                args=(
                    i + 1,
                    db_path,
                    "wspr" if "WSPR" in name else "ft8",
                    cycles,
                    spots_per_burst,
                    inter_cycle_sleep_s,
                    barrier,
                    results_q,
                ),
            )
            for i in range(n_writers)
        ]

        t0 = time.perf_counter()
        for p in procs:
            p.start()

        results = []
        for _ in range(n_writers):
            results.append(results_q.get(timeout=300))

        for p in procs:
            p.join(timeout=10)

        wall_s = time.perf_counter() - t0

    all_latencies: list[float] = []
    all_errors: list[str] = []
    for r in sorted(results, key=lambda r: r["writer_id"]):
        all_latencies.extend(r["latencies_ms"])
        all_errors.extend(r["errors"])

    n_inserts = n_writers * cycles * spots_per_burst
    n_flushes = len(all_latencies)
    if n_flushes == 0:
        print("  RESULT: RED — no successful flushes recorded")
        print("  Errors:")
        for e in all_errors[:5]:
            print(f"    {e}")
        return False

    p50 = statistics.median(all_latencies)
    p95 = statistics.quantiles(all_latencies, n=20)[18] if len(all_latencies) >= 20 else max(all_latencies)
    p99 = statistics.quantiles(all_latencies, n=100)[98] if len(all_latencies) >= 100 else max(all_latencies)
    mean = statistics.mean(all_latencies)
    p_max = max(all_latencies)

    print(f"  Wall time:     {wall_s:.2f} s")
    print(f"  Inserts total: {n_inserts:,}")
    print(f"  Flushes:       {n_flushes}  (one per writer per cycle)")
    print(f"  Flush latency: mean={mean:.1f} ms  p50={p50:.1f} ms  "
          f"p95={p95:.1f} ms  p99={p99:.1f} ms  max={p_max:.1f} ms")
    print(f"  Errors:        {len(all_errors)}")

    for e in all_errors[:5]:
        print(f"    {e}")

    green = (p99 < p99_threshold_ms) and (len(all_errors) == 0)
    print(f"  RESULT: {'GREEN' if green else 'RED'} "
          f"({'p99 under threshold, no errors' if green else 'see numbers above'})")
    return green


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--writers", type=int, default=4)
    parser.add_argument("--quick", action="store_true",
                        help="3 cycles per scenario instead of 5")
    args = parser.parse_args()

    cycles = 3 if args.quick else 5

    # WSPR scenario: 17 bands × ~10 spots per band = ~170 spots per burst,
    # bursts every 0.5 s (real cadence is 120 s).
    wspr_green = run_scenario(
        name="WSPR-cycle burst",
        n_writers=args.writers,
        cycles=cycles,
        spots_per_burst=170,
        inter_cycle_sleep_s=0.5,
        p99_threshold_ms=50.0,
    )

    # FT8 scenario: smaller bursts but tighter cadence.
    ft8_green = run_scenario(
        name="FT8-cycle burst",
        n_writers=args.writers,
        cycles=cycles * 2,                # 2× more cycles for FT8
        spots_per_burst=80,
        inter_cycle_sleep_s=0.2,
        p99_threshold_ms=25.0,
    )

    print("\n=== Phase-1 verdict ===")
    if wspr_green and ft8_green:
        print("GREEN — per-process-per-reporter model is viable for "
              "expected reporter counts (N≤4 per host).")
        print("MULTI-INSTANCE-ARCHITECTURE.md §10 Phase 2+ unblocked.")
        return 0
    else:
        print("RED — investigate before proceeding with Phase 2+.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
