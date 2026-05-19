"""`smd verifier report --target psk` — local-sink audit of the
FT8/FT4 spot forwarding queue.

The forwarding path is:

  psk-recorder  →  /var/lib/sigmond/sink.db (target_db='psk')
                →  hs-uploader  →  upstream PSKReporter path

This report audits the hop sigmond owns: the local SQLite sink.
`hs-uploader` drains `pending_uploads` in FIFO order and deletes each
row once it has been shipped upstream — so a row's *presence* in the
queue means "not yet forwarded", and its *age* is the signal:

  * in_flight — queued recently; the uploader is expected to still be
                working through it.  Normal.
  * stale     — queued longer ago than the in-flight window and STILL
                in the local queue.  The uploader is behind or failing
                — this is the operator's investigation cohort.

There is no "delivered" cohort: a delivered row has been deleted from
the queue, so the local sink cannot see it.  This report makes no
claim about upstream PSKReporter ingestion — that is out of band.

Plus cadence: did we miss any expected FT decode cycles?  FT8 cycles
are 15 s, FT4 cycles 7.5 s; in a healthy minute we expect 4 FT8
cycles and 8 FT4 cycles.  A sudden zero-count cycle is a recorder
hiccup; a long run of zeros is the symptom that radiod or
psk-recorder fell over.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


DEFAULT_SINK_DB = "/var/lib/sigmond/sink.db"
# A row queued more recently than this is still "in flight" — the
# uploader is expected to be working through it.  Older rows that are
# still in the queue are flagged stale.
DEFAULT_IN_FLIGHT_WINDOW_SEC = 300         # 5 min


# Per-spot identity:
#   * `epoch` floored to the second (FT timing carries seconds)
#   * `mode` lowercased, `tx_call` uppercased
#   * `frequency` in Hz (integer)
SpotKey = Tuple[int, str, str, int]


# ── Window parsing (shared shape with verifier_report._parse_window) ─────────

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


# ── Read rows from sink.db ───────────────────────────────────────────────────

@dataclass
class LocalRow:
    key:        SpotKey
    queued_at:  datetime
    rx_sign:    str
    mode:       str
    tx_call:    str
    frequency:  int


def _row_to_key(payload: dict) -> Optional[SpotKey]:
    """Build a SpotKey from a psk.spots payload_json.

    Returns None when essential fields are missing — those rows aren't
    forwardable and including them would inflate the cohorts spuriously.
    """
    try:
        ts_raw  = payload.get("time")
        mode    = (payload.get("mode") or "").lower()
        tx_call = (payload.get("tx_call") or "").upper()
        freq    = int(payload.get("frequency") or 0)
    except (TypeError, ValueError):
        return None
    if not mode or not tx_call or not freq:
        return None
    if isinstance(ts_raw, datetime):
        dt = ts_raw
    elif isinstance(ts_raw, (int, float)):
        dt = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
    elif isinstance(ts_raw, str):
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch_sec = int(dt.timestamp())
    return (epoch_sec, mode, tx_call, freq)


def read_local_rows(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    rx_sign: Optional[str] = None,
    forward_only: bool = True,
) -> List[LocalRow]:
    """Read psk.spots rows still queued in the local sink's pending_uploads.

    Every row this returns is, by definition, not yet forwarded — the
    uploader deletes rows on successful delivery.

    `forward_only`: when True (default), include only rows the producer
    flagged ``forward_to_pskreporter=true`` — the ``both`` / ``direct``
    modes don't go through this queue at all.
    """
    sql = (
        "SELECT payload_json, queued_at "
        "FROM pending_uploads "
        "WHERE target_db='psk' AND target_table='spots' "
        "  AND queued_at > ? "
        "ORDER BY queued_at"
    )
    out: List[LocalRow] = []
    for payload_json, queued_at in conn.execute(sql, (since_iso,)):
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if forward_only and not payload.get("forward_to_pskreporter", True):
            continue
        if rx_sign and (payload.get("rx_sign") or "") != rx_sign:
            continue
        key = _row_to_key(payload)
        if key is None:
            continue
        try:
            qdt = datetime.fromisoformat(queued_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            qdt = datetime.now(timezone.utc)
        if qdt.tzinfo is None:
            qdt = qdt.replace(tzinfo=timezone.utc)
        out.append(LocalRow(
            key=key, queued_at=qdt,
            rx_sign=str(payload.get("rx_sign") or ""),
            mode=key[1], tx_call=key[2], frequency=key[3],
        ))
    return out


# ── Cohort assignment ────────────────────────────────────────────────────────

@dataclass
class Cohorts:
    in_flight: List[LocalRow] = field(default_factory=list)
    stale:     List[LocalRow] = field(default_factory=list)


def classify(
    local_rows: List[LocalRow],
    *,
    now: datetime,
    in_flight_window_sec: int = DEFAULT_IN_FLIGHT_WINDOW_SEC,
) -> Cohorts:
    """Bucket each queued row into in_flight / stale by queue age.

    A row is *in_flight* when it was queued more recently than
    `in_flight_window_sec` ago — the uploader is expected to still be
    working through it.

    A row is *stale* when it was queued earlier than that window and is
    still sitting in the local queue — the uploader is behind or
    failing.  This is the operator's primary investigation cohort.
    """
    cutoff = now - timedelta(seconds=in_flight_window_sec)
    c = Cohorts()
    for row in local_rows:
        if row.queued_at >= cutoff:
            c.in_flight.append(row)
        else:
            c.stale.append(row)
    return c


def oldest_age_sec(local_rows: List[LocalRow], *, now: datetime) -> Optional[int]:
    """Age, in seconds, of the oldest row still in the queue (or None)."""
    if not local_rows:
        return None
    oldest = min(r.queued_at for r in local_rows)
    return max(0, int((now - oldest).total_seconds()))


# ── Cadence ─────────────────────────────────────────────────────────────────

# FT cycle lengths, seconds.  msk144 covered when we add it to the modes set.
_FT_CYCLES = {"ft8": 15.0, "ft4": 7.5}


@dataclass
class CycleStats:
    mode: str
    cycle_sec: float
    expected_cycles: int
    cycles_with_data: int
    cycles_zero: int
    total_spots: int


def cadence_stats(
    local_rows: List[LocalRow],
    *,
    since: datetime,
    until: datetime,
) -> List[CycleStats]:
    """How many FT cycles inside [since, until] had at least one decode?

    Cycle alignment: WSJT-X aligns to UTC second-since-epoch modulo the
    cycle length.  We bucket each local_row's `epoch` to its cycle, then
    count distinct buckets per mode.

    Note: because the uploader deletes rows on delivery, a long window
    only sees the cycles whose rows are still queued — cadence is most
    meaningful over a window close to the in-flight horizon.
    """
    out: List[CycleStats] = []
    s_epoch = int(since.timestamp())
    u_epoch = int(until.timestamp())
    for mode, sec in _FT_CYCLES.items():
        cycles_in_window = max(0, int((u_epoch - s_epoch) / sec))
        seen: Set[int] = set()
        spots = 0
        for row in local_rows:
            if row.mode != mode:
                continue
            bucket = int(row.key[0] // sec) * int(sec)
            if s_epoch <= bucket <= u_epoch:
                seen.add(bucket)
                spots += 1
        out.append(CycleStats(
            mode=mode, cycle_sec=sec,
            expected_cycles=cycles_in_window,
            cycles_with_data=len(seen),
            cycles_zero=max(0, cycles_in_window - len(seen)),
            total_spots=spots,
        ))
    return out


# ── Rendering ───────────────────────────────────────────────────────────────

def _fmt_age(sec: Optional[int]) -> str:
    if sec is None:
        return "-"
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    return f"{sec // 3600}h"


def _format_summary(
    *,
    window_label: str,
    rx_call: Optional[str],
    n_local: int,
    cohorts: Cohorts,
    oldest_sec: Optional[int],
) -> List[str]:
    out = []
    out.append(
        f"window: last {window_label}   "
        f"reporter: {rx_call or '(all)'}   target: local psk queue"
    )
    out.append("")
    if not n_local:
        out.append("psk rows in the local forwarding queue: 0")
        out.append("  (queue empty — either nothing decoded in window, or "
                   "hs-uploader has drained it; check PSK_DELIVERY_MODE)")
    else:
        f, s = len(cohorts.in_flight), len(cohorts.stale)
        fp = 100.0 * f / n_local
        sp = 100.0 * s / n_local
        out.append(f"psk rows still queued for forwarding: {n_local}")
        out.append(f"  in_flight: {f:6d}  ({fp:5.1f}%)  "
                   "queued recently — uploader expected to be working it")
        out.append(f"  stale:     {s:6d}  ({sp:5.1f}%)  "
                   "queued past the in-flight window, still not forwarded")
        out.append(f"  oldest queued row: {_fmt_age(oldest_sec)} ago")
        if s:
            out.append("  → stale rows present: hs-uploader is behind or "
                       "failing — check its journal")
    return out


def _format_cadence(stats: List[CycleStats]) -> List[str]:
    out = ["cadence (cycles with decodes / cycles expected):"]
    for s in stats:
        if s.expected_cycles == 0:
            out.append(f"  {s.mode:5s}  (window too small for one full cycle)")
            continue
        pct = 100.0 * s.cycles_with_data / s.expected_cycles
        out.append(
            f"  {s.mode:5s}  {s.cycles_with_data:5d}/{s.expected_cycles:<5d} "
            f"({pct:5.1f}%)   "
            f"{s.cycles_zero} zero-decode cycle(s), "
            f"{s.total_spots} spots total"
        )
    return out


def _format_stale(cohort: List[LocalRow], cap: int = 50) -> List[str]:
    out = []
    rows = sorted(cohort, key=lambda r: (r.key[0], r.tx_call))
    n = len(rows)
    if cap and n > cap:
        out.append(f"stale spots ({n} — showing first {cap} by spot time):")
        rows = rows[:cap]
    else:
        out.append(f"stale spots ({n}):")
    for r in rows:
        ts = datetime.fromtimestamp(r.key[0], tz=timezone.utc) \
                  .strftime("%H:%M:%S")
        out.append(
            f"  {ts}  {r.mode:4s}  {r.tx_call:<10}  "
            f"{r.frequency:>10} Hz  "
            f"queued={r.queued_at.strftime('%H:%M:%S')}"
        )
    return out


# ── Detect default rx_call ──────────────────────────────────────────────────

def _detect_default_rx_call(conn: sqlite3.Connection) -> Optional[str]:
    """Pick the most common rx_sign from recent psk.spots payloads as
    the default reporter when --rx-call isn't passed.  Lets `smd
    verifier report --target psk` Just Work on a single-receiver host.
    """
    try:
        row = conn.execute(
            "SELECT payload_json FROM pending_uploads "
            "WHERE target_db='psk' AND target_table='spots' "
            "ORDER BY id DESC LIMIT 200"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    seen: Dict[str, int] = {}
    for (pj,) in row:
        try:
            sign = json.loads(pj).get("rx_sign") or ""
        except (TypeError, ValueError):
            continue
        if sign:
            seen[sign] = seen.get(sign, 0) + 1
    if not seen:
        return None
    return sorted(seen.items(), key=lambda kv: -kv[1])[0][0]


# ── CLI entry point ─────────────────────────────────────────────────────────

def cmd_verifier_report_psk(args) -> int:
    """`smd verifier report --target psk` entry point."""
    try:
        window = _parse_window(args.window)
    except ValueError as exc:
        print(f"smd verifier report: {exc}", file=sys.stderr)
        return 2

    db_path = Path(args.db if hasattr(args, "db") and args.db
                   else DEFAULT_SINK_DB)
    if not db_path.exists():
        print(
            f"smd verifier report: sink db not found at {db_path}",
            file=sys.stderr,
        )
        return 2
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        print(f"smd verifier report: open sink db failed: {exc}",
              file=sys.stderr)
        return 2

    try:
        now = datetime.now(timezone.utc)
        since = now - window
        since_iso = since.isoformat(timespec="seconds")

        rx_call = getattr(args, "rx_call", None)
        if rx_call is None:
            rx_call = _detect_default_rx_call(conn)

        local_rows = read_local_rows(conn,
                                     since_iso=since_iso, rx_sign=rx_call)

        # argparse default is None (so we can detect "not passed"); turn
        # that into the module default here rather than at parser
        # construction time so the env-var path can also override.
        in_flight_sec = (getattr(args, "psk_in_flight_sec", None)
                         or DEFAULT_IN_FLIGHT_WINDOW_SEC)
        cohorts = classify(local_rows, now=now,
                           in_flight_window_sec=in_flight_sec)
        oldest_sec = oldest_age_sec(local_rows, now=now)

        cadence = cadence_stats(local_rows, since=since, until=now)

        if args.json:
            payload = {
                "target": "psk",
                "window": args.window,
                "rx_call": rx_call,
                "n_local": len(local_rows),
                "in_flight": len(cohorts.in_flight),
                "stale":     len(cohorts.stale),
                "oldest_queued_sec": oldest_sec,
                "cadence": [
                    {
                        "mode": c.mode, "cycle_sec": c.cycle_sec,
                        "expected_cycles": c.expected_cycles,
                        "cycles_with_data": c.cycles_with_data,
                        "cycles_zero": c.cycles_zero,
                        "total_spots": c.total_spots,
                    } for c in cadence
                ],
            }
            if getattr(args, "lost", False):
                payload["stale_spots"] = [
                    {
                        "epoch": r.key[0], "mode": r.mode,
                        "tx_call": r.tx_call, "frequency": r.frequency,
                        "queued_at": r.queued_at.isoformat(timespec="seconds"),
                    } for r in cohorts.stale
                ]
            print(json.dumps(payload, indent=2))
            return 0

        for line in _format_summary(
            window_label=args.window, rx_call=rx_call,
            n_local=len(local_rows), cohorts=cohorts, oldest_sec=oldest_sec,
        ):
            print(line)
        print()
        for line in _format_cadence(cadence):
            print(line)
        if getattr(args, "lost", False) and cohorts.stale:
            print()
            for line in _format_stale(cohorts.stale):
                print(line)
        return 0
    finally:
        conn.close()
