"""`smd verifier report` — windowed report of wsprnet upload audit.

Reads the ``wsprnet_audit`` and ``wsprnet_audit_batch`` tables in
``/var/lib/sigmond/sink.db`` (populated by
``wspr_recorder.wsprnet_audit`` from
``WSPRNET_AUDIT=1``-enabled hosts) and answers the operator's
question: of the spots we shipped to wsprnet, how many actually
landed in ``wspr.rx``, and what are the IDs of the ones that
didn't?

The "lost" cohort — uploaded, never appeared in ``wspr.rx`` within
2 h — combines two indistinguishable failure modes wsprnet's API
won't separate for us:

  * Rejected at upload time (counted in the batch's ``N-M``
    not-added number from ``N out of M spot(s) added``).
  * Accepted at upload time, then silently dropped before
    indexing into ``wspr.rx``.

The batch-acceptance rate (sum of ``n_added`` over sum of
``n_posted``) attributes the gross rejection count to "rejected at
upload" in aggregate; the per-spot lost list lets the operator see
the actual spot identities even though we can't say which of the
two failure modes hit each one.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional


DEFAULT_SINK_DB = "/var/lib/sigmond/sink.db"


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


def _format_summary(
    *,
    window_label: str,
    rx_call: str,
    n_posted: int,
    n_added: int,
    delivered: int,
    lost: int,
    in_flight: int,
) -> str:
    """Plain-text summary block.  Stable widths so it lines up under
    a fixed-width terminal — operators read these in a journalctl
    pager or piped into ``less``.
    """
    n_audit = delivered + lost + in_flight
    out = []
    out.append(f"window: last {window_label}   reporter: {rx_call}")
    out.append("")
    if n_posted:
        accept_pct = 100.0 * n_added / n_posted
        out.append(
            f"batches: posted={n_posted} added={n_added} "
            f"(wsprnet accepted {accept_pct:.1f}%)"
        )
    else:
        out.append("batches: (no upload activity in window)")
    if n_audit:
        out.append(f"spots in audit: {n_audit}")
        d_pct = 100.0 * delivered / n_audit if n_audit else 0
        l_pct = 100.0 * lost / n_audit if n_audit else 0
        f_pct = 100.0 * in_flight / n_audit if n_audit else 0
        out.append(f"  delivered: {delivered:6d}  ({d_pct:5.1f}%)  "
                   "verified in wspr.rx")
        out.append(f"  lost:      {lost:6d}  ({l_pct:5.1f}%)  "
                   "uploaded but never in wspr.rx (rejected OR "
                   "silently dropped)")
        out.append(f"  in_flight: {in_flight:6d}  ({f_pct:5.1f}%)  "
                   "still inside the verifier's 2 h wait window")
    else:
        out.append("spots in audit: 0 (nothing to report)")
    return "\n".join(out)


def _format_lost_lines(rows: List[tuple]) -> List[str]:
    """One line per lost spot, sorted by time then tx_sign."""
    out = []
    for spot_key, uploaded_at, dropped_at in rows:
        # spot_key is "YYYY-MM-DDTHH:MM:00Z|TX_SIGN|FREQ_HZ"
        parts = spot_key.split("|", 2)
        if len(parts) != 3:
            continue
        t, tx, freq = parts
        # Trim to "HH:MM" for compactness; uploaded_at gives the day
        try:
            ts_hhmm = t[11:16]
        except IndexError:
            ts_hhmm = t
        out.append(
            f"  {ts_hhmm}  {tx:<10}  {freq:>10} Hz  "
            f"uploaded={uploaded_at[:19]}  dropped={dropped_at[:19]}"
        )
    return out


def _summary_query(
    conn: sqlite3.Connection,
    rx_call: Optional[str],
    since_iso: str,
) -> dict:
    """Aggregate counts for the window.  ``rx_call=None`` means
    aggregate across all reporters present in the audit (useful when
    a host runs two ``wspr-recorder@<id>`` instances and the operator
    wants a host-wide view)."""
    where_clauses = ["uploaded_at >= ?"]
    params: list = [since_iso]
    if rx_call:
        where_clauses.append("rx_call = ?")
        params.append(rx_call)
    where = " AND ".join(where_clauses)
    row = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN verified_at IS NOT NULL THEN 1 ELSE 0 END) AS delivered,
          SUM(CASE WHEN dropped_at IS NOT NULL AND verified_at IS NULL
                   THEN 1 ELSE 0 END) AS lost,
          SUM(CASE WHEN verified_at IS NULL AND dropped_at IS NULL
                   THEN 1 ELSE 0 END) AS in_flight
        FROM wsprnet_audit
        WHERE {where}
        """,
        params,
    ).fetchone()
    delivered = (row[0] or 0) if row else 0
    lost = (row[1] or 0) if row else 0
    in_flight = (row[2] or 0) if row else 0

    where_clauses = ["uploaded_at >= ?"]
    params = [since_iso]
    if rx_call:
        where_clauses.append("rx_call = ?")
        params.append(rx_call)
    where = " AND ".join(where_clauses)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(n_posted), 0),
               COALESCE(SUM(n_added), 0)
        FROM wsprnet_audit_batch
        WHERE {where}
        """,
        params,
    ).fetchone()
    n_posted = row[0] or 0
    n_added = row[1] or 0

    return {
        "delivered": delivered,
        "lost": lost,
        "in_flight": in_flight,
        "n_posted": n_posted,
        "n_added": n_added,
    }


def _lost_query(
    conn: sqlite3.Connection,
    rx_call: Optional[str],
    since_iso: str,
) -> List[tuple]:
    where_clauses = [
        "uploaded_at >= ?",
        "dropped_at IS NOT NULL",
        "verified_at IS NULL",
    ]
    params: list = [since_iso]
    if rx_call:
        where_clauses.append("rx_call = ?")
        params.append(rx_call)
    where = " AND ".join(where_clauses)
    return list(conn.execute(
        f"""
        SELECT spot_key, uploaded_at, dropped_at
        FROM wsprnet_audit
        WHERE {where}
        ORDER BY uploaded_at, spot_key
        """,
        params,
    ))


def _detect_default_rx_call(conn: sqlite3.Connection) -> Optional[str]:
    """If exactly one rx_call appears in the audit, return it.
    Otherwise return None so the caller falls back to all-reporters.
    Helps the common single-receiver host (most installs today) skip
    the ``--rx-call`` flag.
    """
    row = conn.execute(
        "SELECT DISTINCT rx_call FROM wsprnet_audit LIMIT 2"
    ).fetchall()
    if len(row) == 1:
        return row[0][0]
    return None


def cmd_verifier_report(args) -> int:
    """`smd verifier report` entry point."""
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
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5.0,
        )
    except sqlite3.Error as exc:
        print(
            f"smd verifier report: open sink db failed: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        # Audit table absent → host hasn't enabled WSPRNET_AUDIT=1
        # yet.  Friendly diagnostic instead of a SQL error.
        existing = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='wsprnet_audit'"
        ).fetchone()
        if not existing:
            print(
                "smd verifier report: wsprnet_audit table not present.\n"
                "  Enable per-spot auditing by setting WSPRNET_AUDIT=1 in\n"
                "  /etc/wspr-recorder/env/<id>.env and restarting\n"
                "  wspr-recorder@<id>.  The table is created on first\n"
                "  uploader batch after that.",
                file=sys.stderr,
            )
            return 2

        rx_call = getattr(args, "rx_call", None)
        if rx_call is None:
            rx_call = _detect_default_rx_call(conn)
        since = datetime.now(timezone.utc) - window
        since_iso = since.isoformat(timespec="seconds")

        summary = _summary_query(conn, rx_call, since_iso)
        lost_rows = _lost_query(conn, rx_call, since_iso) \
            if (args.lost or args.json) else []

        if args.json:
            print(json.dumps({
                "window": args.window,
                "rx_call": rx_call,
                "summary": summary,
                "lost": [
                    {
                        "spot_key": row[0],
                        "uploaded_at": row[1],
                        "dropped_at": row[2],
                    } for row in lost_rows
                ],
            }, indent=2))
            return 0

        print(_format_summary(
            window_label=args.window,
            rx_call=rx_call or "(all)",
            n_posted=summary["n_posted"],
            n_added=summary["n_added"],
            delivered=summary["delivered"],
            lost=summary["lost"],
            in_flight=summary["in_flight"],
        ))
        if args.lost:
            if lost_rows:
                print()
                print(f"lost spots ({len(lost_rows)}):")
                for line in _format_lost_lines(lost_rows):
                    print(line)
            else:
                print()
                print("lost spots: (none in window)")
        return 0
    finally:
        conn.close()
