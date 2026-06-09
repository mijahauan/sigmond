"""`smd admin verifier report --target timestd` — local-product audit of
the hf-timestd metrology pipeline.

Unlike wspr/psk, hf-timestd does NOT stage through sigmond's
`pending_uploads` queue.  After the 2026-05 HDF5→SQLite migration
(hf-timestd commits 08097cc / 7179cdb / 0276a0d) all L1/L2 data
products land directly in ``/var/lib/timestd/phase2/timestd.db``;
the historical pending_uploads path is dormant.  This verifier
therefore audits the local product table — the canonical signal of
"is hf-timestd still producing useful data" — rather than a
delivery queue.

The audit:

  * Per-channel cadence: each metrology channel is expected to
    write one row per UTC minute boundary (the `minute_boundary_utc`
    column).  Count distinct minute boundaries in --window and
    compare against the expected count (= window seconds / 60).
  * Last-write recency: how recently each channel wrote its newest
    row.  A channel that hasn't written for >dark-after-sec is
    flagged DARK.  Operator's investigation cohort.

There is no "stale queue" cohort because there is no queue: rows
land directly in the persistent product DB.  Restoring delivery to
a HamSCI sink is a separate design question.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional


DEFAULT_TIMESTD_DB = "/var/lib/timestd/phase2/timestd.db"
# A channel that hasn't written for longer than this is flagged dark.
# 5 min is generous against a 1/min cadence: a brief hiccup (one missed
# write) shouldn't trip it, but a sustained outage will.
DEFAULT_DARK_AFTER_SEC = 300


def _parse_window(spec: str) -> timedelta:
    """Accept ``1h``, ``30m``, ``2d`` — small DSL for the --window flag."""
    m = re.fullmatch(r"(\d+)\s*([smhd])", spec.strip().lower())
    if not m:
        raise ValueError(
            f"--window expects e.g. '1h', '30m', '24h', '7d'; got {spec!r}"
        )
    n = int(m.group(1))
    unit = {"s": "seconds", "m": "minutes",
            "h": "hours", "d": "days"}[m.group(2)]
    return timedelta(**{unit: n})


@dataclass
class ChannelStats:
    channel:           str
    minutes_with_data: int
    expected_minutes:  int
    last_row_utc:      Optional[datetime]
    avg_snr_db:        Optional[float]
    detected_count:    int

    @property
    def cadence_pct(self) -> float:
        if self.expected_minutes <= 0:
            return 0.0
        return 100.0 * self.minutes_with_data / self.expected_minutes

    def last_seen_sec(self, now: datetime) -> Optional[int]:
        if self.last_row_utc is None:
            return None
        return int((now - self.last_row_utc).total_seconds())

    def is_dark(self, now: datetime, dark_after_sec: int) -> bool:
        age = self.last_seen_sec(now)
        return age is None or age > dark_after_sec


def _read_channel_stats(
    conn: sqlite3.Connection,
    *,
    since: datetime,
    expected_minutes: int,
) -> List[ChannelStats]:
    """One ChannelStats row per channel that has produced data since
    `since`.  Channels with zero rows in the window don't show up here
    — the caller compares against a configured channel list to detect
    fully-dark channels if needed.
    """
    # ISO-8601 with the `T` separator matches the column format
    # (text comparison is correct over fixed-width ISO timestamps).
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT channel, "
        "       COUNT(DISTINCT minute_boundary_utc) AS minutes_with_data, "
        "       MAX(timestamp_utc) AS last_row, "
        "       AVG(snr_db) AS avg_snr, "
        "       SUM(tone_detected) AS detected "
        "FROM L1_metrology_measurements "
        "WHERE timestamp_utc >= ? "
        "GROUP BY channel "
        "ORDER BY channel",
        (since_iso,),
    ).fetchall()

    out: List[ChannelStats] = []
    for chan, minutes, last_iso, avg_snr, detected in rows:
        last_dt: Optional[datetime] = None
        if last_iso:
            try:
                last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
            except ValueError:
                last_dt = None
        out.append(ChannelStats(
            channel=chan,
            minutes_with_data=int(minutes or 0),
            expected_minutes=expected_minutes,
            last_row_utc=last_dt,
            avg_snr_db=float(avg_snr) if avg_snr is not None else None,
            detected_count=int(detected or 0),
        ))
    return out


def _fmt_age(sec: Optional[int]) -> str:
    if sec is None:
        return "(never)"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    if sec < 86400:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    return f"{sec // 86400}d{(sec % 86400) // 3600:02d}h"


def _format_summary(
    *,
    window_label: str,
    stats: List[ChannelStats],
    dark: List[ChannelStats],
    now: datetime,
) -> List[str]:
    out = [
        f"window: last {window_label}   "
        f"target: hf-timestd L1_metrology_measurements",
        "",
    ]
    n = len(stats)
    n_healthy = n - len(dark)
    out.append(f"channels producing in window: {n}")
    out.append(
        f"  healthy: {n_healthy}    "
        f"dark (last write > 5 min ago): {len(dark)}"
    )
    if dark:
        out.append("  → dark channels — metrology workers may be stuck "
                   "or radiod RTP feed is missing the frequency")
    return out


def _format_per_channel(stats: List[ChannelStats], now: datetime,
                       dark_after_sec: int) -> List[str]:
    out = ["per-channel cadence (distinct UTC minutes / expected):"]
    for s in stats:
        age = _fmt_age(s.last_seen_sec(now))
        flag = "  DARK" if s.is_dark(now, dark_after_sec) else ""
        snr = f"{s.avg_snr_db:5.1f}dB" if s.avg_snr_db is not None else "    —"
        out.append(
            f"  {s.channel:14s}  "
            f"{s.minutes_with_data:5d}/{s.expected_minutes:<5d} "
            f"({s.cadence_pct:5.1f}%)   "
            f"last={age:>8s}  avg_snr={snr}{flag}"
        )
    return out


def cmd_verifier_report_timestd(args) -> int:
    """`smd admin verifier report --target timestd` entry point."""
    try:
        window = _parse_window(args.window)
    except ValueError as exc:
        print(f"smd admin verifier report: {exc}", file=sys.stderr)
        return 2

    db_path = Path(
        getattr(args, "timestd_db", None) or DEFAULT_TIMESTD_DB
    )
    if not db_path.exists():
        print(
            f"smd admin verifier report: timestd db not found at {db_path}",
            file=sys.stderr,
        )
        return 2
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5.0
        )
    except sqlite3.Error as exc:
        print(f"smd admin verifier report: open timestd db failed: {exc}",
              file=sys.stderr)
        return 2

    dark_after_sec = int(
        getattr(args, "timestd_dark_after_sec", None) or DEFAULT_DARK_AFTER_SEC
    )

    try:
        now = datetime.now(timezone.utc)
        since = now - window
        expected_minutes = max(int(window.total_seconds() // 60), 1)

        stats = _read_channel_stats(
            conn, since=since, expected_minutes=expected_minutes,
        )
        dark = [s for s in stats if s.is_dark(now, dark_after_sec)]

        if getattr(args, "json", False):
            payload = {
                "target": "timestd",
                "window": args.window,
                "expected_minutes": expected_minutes,
                "dark_after_sec": dark_after_sec,
                "channels": [
                    {
                        "channel": s.channel,
                        "minutes_with_data": s.minutes_with_data,
                        "cadence_pct": round(s.cadence_pct, 1),
                        "last_row_utc": (s.last_row_utc.isoformat(
                            timespec="seconds") if s.last_row_utc else None),
                        "last_seen_sec": s.last_seen_sec(now),
                        "avg_snr_db": (round(s.avg_snr_db, 2)
                                       if s.avg_snr_db is not None else None),
                        "detected_count": s.detected_count,
                        "dark": s.is_dark(now, dark_after_sec),
                    } for s in stats
                ],
            }
            print(json.dumps(payload, indent=2))
            return 0

        for line in _format_summary(
            window_label=args.window, stats=stats, dark=dark, now=now,
        ):
            print(line)
        print()
        for line in _format_per_channel(stats, now, dark_after_sec):
            print(line)
        return 0
    finally:
        conn.close()
