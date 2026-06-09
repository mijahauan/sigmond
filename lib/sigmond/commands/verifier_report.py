"""`smd admin verifier report` — windowed report of wsprnet upload audit.

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


def _format_in_flight_lines(rows: List[tuple]) -> List[str]:
    """One line per in-flight spot, sorted by uploaded_at desc so the
    most-recent batches are at the top (those are most-likely-pending
    waiting for the next verifier pass; older ones are nearer the 2 h
    timeout and more likely to flip to lost soon).
    """
    out = []
    for spot_key, uploaded_at in rows:
        parts = spot_key.split("|", 2)
        if len(parts) != 3:
            continue
        t, tx, freq = parts
        try:
            ts_hhmm = t[11:16]
        except IndexError:
            ts_hhmm = t
        out.append(
            f"  {ts_hhmm}  {tx:<10}  {freq:>10} Hz  "
            f"uploaded={uploaded_at[:19]}"
        )
    return out


def _format_delivered_lines(rows: List[tuple]) -> List[str]:
    """One line per delivered spot, sorted by spot time.  Shows the
    lag between upload and verification so the operator can spot
    abnormally slow wsprnet indexing.
    """
    out = []
    for spot_key, uploaded_at, verified_at in rows:
        parts = spot_key.split("|", 2)
        if len(parts) != 3:
            continue
        t, tx, freq = parts
        try:
            ts_hhmm = t[11:16]
        except IndexError:
            ts_hhmm = t
        # Compute lag (verified_at - uploaded_at) in seconds — useful
        # signal when one server's indexing is slower than another's.
        lag_part = ""
        try:
            u = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
            v = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
            lag = int((v - u).total_seconds())
            lag_part = f"  lag={lag:>4}s"
        except (ValueError, AttributeError):
            pass
        out.append(
            f"  {ts_hhmm}  {tx:<10}  {freq:>10} Hz{lag_part}"
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


def _in_flight_query(
    conn: sqlite3.Connection,
    rx_call: Optional[str],
    since_iso: str,
) -> List[tuple]:
    """Spots uploaded in the window that haven't been verified at
    wspr.rx yet and haven't aged out either — typically the most
    recent few WSPR cycles waiting for the next verifier pass.
    """
    where_clauses = [
        "uploaded_at >= ?",
        "verified_at IS NULL",
        "dropped_at IS NULL",
    ]
    params: list = [since_iso]
    if rx_call:
        where_clauses.append("rx_call = ?")
        params.append(rx_call)
    where = " AND ".join(where_clauses)
    return list(conn.execute(
        f"""
        SELECT spot_key, uploaded_at
        FROM wsprnet_audit
        WHERE {where}
        ORDER BY uploaded_at DESC, spot_key
        """,
        params,
    ))


def _per_cycle_counts(
    conn: sqlite3.Connection,
    rx_call: Optional[str],
    since_iso: str,
) -> dict:
    """Return ``{cycle_time_iso: spot_count}`` for every WSPR cycle
    that has at least one spot in the audit table.

    The audit's spot_key prefix ``YYYY-MM-DDTHH:MM:00Z`` already
    encodes the cycle minute (the verifier truncates each spot's
    epoch to its WSPR cycle), so we GROUP BY that prefix.
    """
    where_clauses = ["uploaded_at >= ?"]
    params: list = [since_iso]
    if rx_call:
        where_clauses.append("rx_call = ?")
        params.append(rx_call)
    where = " AND ".join(where_clauses)
    out: dict = {}
    for cycle_time, n in conn.execute(
        f"""
        SELECT SUBSTR(spot_key, 1, 20) AS cycle_time, COUNT() AS n
          FROM wsprnet_audit
         WHERE {where}
      GROUP BY cycle_time
      ORDER BY cycle_time
        """,
        params,
    ):
        out[cycle_time] = n
    return out


def _expected_cycles(since: datetime, until: datetime) -> List[str]:
    """List of every even-UTC-minute timestamp in [since, until],
    formatted to match the audit's spot_key time prefix
    (``YYYY-MM-DDTHH:MM:00Z``).  WSPR cycles always start on even
    minutes regardless of the local clock.
    """
    # Round ``since`` UP to the next even minute, ``until`` DOWN to
    # the prior even minute, then step by 2 min.
    s = since.replace(second=0, microsecond=0)
    if s.minute % 2 != 0:
        s = s + timedelta(minutes=1)
    elif s < since:
        s = s + timedelta(minutes=2)
    u = until.replace(second=0, microsecond=0)
    if u.minute % 2 != 0:
        u = u - timedelta(minutes=1)
    out: List[str] = []
    t = s
    while t <= u:
        out.append(t.strftime("%Y-%m-%dT%H:%M:00Z"))
        t = t + timedelta(minutes=2)
    return out


def _cadence_analysis(
    expected: List[str],
    cycle_counts: dict,
    low_threshold_frac: float = 0.33,
) -> dict:
    """Compute cycle-cadence health.

    ``low_threshold_frac`` is the fraction of the median below which
    a cycle is flagged as "low".  0.33 means "less than a third of
    the median is suspicious"; on a 40-spot/cycle site, anything
    under ~13 spots qualifies.  Picked so transient propagation
    dips don't trigger but a partial-decode (OOM mid-cycle, decoder
    crash) does.
    """
    present = set(cycle_counts.keys())
    missing = [c for c in expected if c not in present]
    counts = sorted(cycle_counts.values())
    median = counts[len(counts) // 2] if counts else 0
    low_cutoff = max(1, int(median * low_threshold_frac))
    low: List[tuple] = [
        (c, cycle_counts[c]) for c in expected
        if c in present and cycle_counts[c] < low_cutoff
    ]
    return {
        "expected": len(expected),
        "present": len(present),
        "missing": missing,
        "median": median,
        "low_cutoff": low_cutoff,
        "low": low,
        "min": counts[0] if counts else 0,
        "max": counts[-1] if counts else 0,
    }


def _format_cadence(c: dict) -> List[str]:
    """Render cadence summary + per-bucket lists."""
    out = []
    pct = (100.0 * c["present"] / c["expected"]) if c["expected"] else 0
    out.append(
        f"cadence: cycles_expected={c['expected']} "
        f"cycles_with_spots={c['present']} ({pct:.1f}%)  "
        f"median_spots/cycle={c['median']} "
        f"range=[{c['min']},{c['max']}]"
    )
    if c["missing"]:
        out.append(f"  missing cycles ({len(c['missing'])}):")
        for cyc in c["missing"]:
            # cyc = "YYYY-MM-DDTHH:MM:00Z" → show HH:MM
            try:
                hhmm = cyc[11:16]
            except IndexError:
                hhmm = cyc
            out.append(f"    {hhmm} UTC  (no spots — daemon restart? OOM?)")
    if c["low"]:
        out.append(
            f"  low-count cycles ({len(c['low'])}; "
            f"<{c['low_cutoff']} spots vs median {c['median']}):"
        )
        for cyc, n in c["low"]:
            try:
                hhmm = cyc[11:16]
            except IndexError:
                hhmm = cyc
            out.append(f"    {hhmm} UTC  {n:>3} spots")
    if not c["missing"] and not c["low"]:
        out.append("  no missing cycles, no low-count anomalies — healthy")
    return out


def _rejected_query(
    conn: sqlite3.Connection,
    rx_call: Optional[str],
    since_iso: str,
) -> List[tuple]:
    """Spots wsprnet explicitly rejected at upload time.

    Only meaningful when ``WSPRNET_BATCH_SIZE=1`` is set on the
    uploading host — that's the only mode where wsprnet's
    aggregate "M out of N added" response answers per-spot.  When
    the upload batch's ``(n_posted=1, n_added=0)`` tuple holds,
    the spot tied to that batch was the one wsprnet rejected.

    For larger batches the rejection is unattributable to a
    specific spot and rows here return empty even if the batch
    aggregate shows some rejections.
    """
    where_clauses = [
        "a.uploaded_at >= ?",
        "b.n_posted = 1",
        "b.n_added = 0",
    ]
    params: list = [since_iso]
    if rx_call:
        where_clauses.append("a.rx_call = ?")
        params.append(rx_call)
    where = " AND ".join(where_clauses)
    return list(conn.execute(
        f"""
        SELECT a.spot_key, a.uploaded_at, a.dropped_at
          FROM wsprnet_audit a
          JOIN wsprnet_audit_batch b ON a.batch_id = b.id
         WHERE {where}
      ORDER BY a.uploaded_at, a.spot_key
        """,
        params,
    ))


def _silent_drop_query(
    conn: sqlite3.Connection,
    rx_call: Optional[str],
    since_iso: str,
) -> List[tuple]:
    """Spots wsprnet ACK'd as added (``n_added=1``) but never indexed
    into ``wspr.rx``.  The smoking-gun cohort for "wsprnet is
    silently dropping accepted spots".

    Per-spot meaningful only with ``WSPRNET_BATCH_SIZE=1`` (same
    reason as :func:`_rejected_query`).  For larger batches we can't
    say which specific spot wsprnet ack'd-but-didn't-index, only
    that some did.
    """
    where_clauses = [
        "a.uploaded_at >= ?",
        "b.n_posted = 1",
        "b.n_added = 1",
        "a.verified_at IS NULL",
        "a.dropped_at IS NOT NULL",
    ]
    params: list = [since_iso]
    if rx_call:
        where_clauses.append("a.rx_call = ?")
        params.append(rx_call)
    where = " AND ".join(where_clauses)
    return list(conn.execute(
        f"""
        SELECT a.spot_key, a.uploaded_at, a.dropped_at
          FROM wsprnet_audit a
          JOIN wsprnet_audit_batch b ON a.batch_id = b.id
         WHERE {where}
      ORDER BY a.uploaded_at, a.spot_key
        """,
        params,
    ))


def _per_spot_breakdown_available(
    conn: sqlite3.Connection,
    rx_call: Optional[str],
    since_iso: str,
) -> bool:
    """True iff every batch in the window has ``n_posted <= 1``.
    Used to decide whether to surface the rejected / silent_drop
    distinction in the default summary — otherwise the wsprnet
    aggregate response can't be attributed to individual spots.
    """
    where_clauses = ["uploaded_at >= ?"]
    params: list = [since_iso]
    if rx_call:
        where_clauses.append("rx_call = ?")
        params.append(rx_call)
    where = " AND ".join(where_clauses)
    row = conn.execute(
        f"""
        SELECT COUNT() AS total,
               SUM(CASE WHEN n_posted <= 1 THEN 1 ELSE 0 END) AS singles
          FROM wsprnet_audit_batch
         WHERE {where}
        """,
        params,
    ).fetchone()
    if not row or not row[0]:
        return False
    total, singles = row[0], row[1] or 0
    return singles == total


def _delivered_query(
    conn: sqlite3.Connection,
    rx_call: Optional[str],
    since_iso: str,
) -> List[tuple]:
    """Spots that round-tripped successfully: uploaded then matched
    in wspr.rx by the WsprnetVerifier.  Returned with both timestamps
    so the formatter can compute upload→verify lag.
    """
    where_clauses = [
        "uploaded_at >= ?",
        "verified_at IS NOT NULL",
    ]
    params: list = [since_iso]
    if rx_call:
        where_clauses.append("rx_call = ?")
        params.append(rx_call)
    where = " AND ".join(where_clauses)
    return list(conn.execute(
        f"""
        SELECT spot_key, uploaded_at, verified_at
        FROM wsprnet_audit
        WHERE {where}
        ORDER BY spot_key
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
    """`smd admin verifier report` entry point."""
    try:
        window = _parse_window(args.window)
    except ValueError as exc:
        print(f"smd admin verifier report: {exc}", file=sys.stderr)
        return 2

    db_path = Path(args.db if hasattr(args, "db") and args.db
                   else DEFAULT_SINK_DB)
    if not db_path.exists():
        print(
            f"smd admin verifier report: sink db not found at {db_path}",
            file=sys.stderr,
        )
        return 2

    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5.0,
        )
    except sqlite3.Error as exc:
        print(
            f"smd admin verifier report: open sink db failed: {exc}",
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
                "smd admin verifier report: wsprnet_audit table not present.\n"
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
        # Fetch each cohort's per-spot list when the flag asks for it
        # OR when --json is on (so the JSON document is complete).
        want_lost = args.lost or args.json
        want_in_flight = getattr(args, 'in_flight', False) or args.json
        want_delivered = getattr(args, 'delivered', False) or args.json
        want_cadence = getattr(args, 'cadence', False) or args.json
        want_rejected = getattr(args, 'rejected', False) or args.json
        want_silent_drop = getattr(args, 'silent_drop', False) or args.json
        lost_rows = _lost_query(conn, rx_call, since_iso) \
            if want_lost else []
        in_flight_rows = _in_flight_query(conn, rx_call, since_iso) \
            if want_in_flight else []
        delivered_rows = _delivered_query(conn, rx_call, since_iso) \
            if want_delivered else []
        rejected_rows = _rejected_query(conn, rx_call, since_iso) \
            if want_rejected else []
        silent_drop_rows = _silent_drop_query(conn, rx_call, since_iso) \
            if want_silent_drop else []
        per_spot_mode = _per_spot_breakdown_available(
            conn, rx_call, since_iso,
        )
        cadence = None
        if want_cadence:
            cycle_counts = _per_cycle_counts(conn, rx_call, since_iso)
            cadence = _cadence_analysis(
                _expected_cycles(since, datetime.now(timezone.utc)),
                cycle_counts,
            )

        if args.json:
            payload = {
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
                "in_flight": [
                    {
                        "spot_key": row[0],
                        "uploaded_at": row[1],
                    } for row in in_flight_rows
                ],
                "delivered": [
                    {
                        "spot_key": row[0],
                        "uploaded_at": row[1],
                        "verified_at": row[2],
                    } for row in delivered_rows
                ],
            }
            if cadence is not None:
                payload["cadence"] = cadence
            payload["per_spot_mode"] = per_spot_mode
            payload["rejected"] = [
                {
                    "spot_key": row[0],
                    "uploaded_at": row[1],
                    "dropped_at": row[2],
                } for row in rejected_rows
            ]
            payload["silent_drop"] = [
                {
                    "spot_key": row[0],
                    "uploaded_at": row[1],
                    "dropped_at": row[2],
                } for row in silent_drop_rows
            ]
            print(json.dumps(payload, indent=2))
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
        if not per_spot_mode:
            print()
            print(
                "note: set WSPRNET_BATCH_SIZE=1 in the wsprnet uploader's "
                "env to distinguish rejected-at-upload from silent-drop "
                "(today's batches mix many spots per POST; wsprnet's "
                "aggregate response can't be attributed to specific spots)"
            )
        if cadence is not None:
            print()
            for line in _format_cadence(cadence):
                print(line)
        if args.lost:
            print()
            if lost_rows:
                print(f"lost spots ({len(lost_rows)}):")
                for line in _format_lost_lines(lost_rows):
                    print(line)
            else:
                print("lost spots: (none in window)")
        if getattr(args, 'in_flight', False):
            print()
            if in_flight_rows:
                print(f"in-flight spots ({len(in_flight_rows)}):")
                for line in _format_in_flight_lines(in_flight_rows):
                    print(line)
            else:
                print("in-flight spots: (none in window)")
        if getattr(args, 'delivered', False):
            print()
            if delivered_rows:
                print(f"delivered spots ({len(delivered_rows)}):")
                for line in _format_delivered_lines(delivered_rows):
                    print(line)
            else:
                print("delivered spots: (none in window)")
        if getattr(args, 'rejected', False):
            print()
            if rejected_rows:
                print(
                    f"rejected spots ({len(rejected_rows)}) — "
                    "wsprnet explicitly said 'not added':"
                )
                for line in _format_lost_lines(rejected_rows):
                    print(line)
            else:
                msg = "rejected spots: (none in window)"
                if not per_spot_mode:
                    msg = (
                        "rejected spots: (per-spot mode not enabled — "
                        "set WSPRNET_BATCH_SIZE=1 to attribute rejection)"
                    )
                print(msg)
        if getattr(args, 'silent_drop', False):
            print()
            if silent_drop_rows:
                print(
                    f"silently-dropped spots ({len(silent_drop_rows)}) — "
                    "wsprnet ack'd as added, then never indexed:"
                )
                for line in _format_lost_lines(silent_drop_rows):
                    print(line)
            else:
                msg = "silently-dropped spots: (none in window)"
                if not per_spot_mode:
                    msg = (
                        "silently-dropped spots: (per-spot mode not "
                        "enabled — set WSPRNET_BATCH_SIZE=1 to detect)"
                    )
                print(msg)
        if getattr(args, 'suppressed', False):
            print()
            rows = _suppressed_query(conn, rx_call)
            if rows:
                print(
                    f"suppressed callsigns ({len(rows)}) — wsprnet "
                    "consistently rejected, no longer fed to wsprd/jt9:"
                )
                for r in rows:
                    rxc, call, rc, first, last, supp = r
                    print(
                        f"  {rxc:<10} {call:<14} "
                        f"rejected={rc:<4} "
                        f"first={first[:19]}  last={last[:19]}  "
                        f"suppressed_since={supp[:19]}"
                    )
                print()
                print(
                    "  manual override: smd admin verifier rehabilitate "
                    "<rx_call> <call>"
                )
            else:
                print("suppressed callsigns: (none — nothing auto-filtered)")
        return 0
    finally:
        conn.close()


def _suppressed_query(conn, rx_call):
    """Read the wsprnet_reject_cache rows that are currently
    suppressed.  Returns [(rx_call, call, rejected_count,
    first_rejected, last_rejected, suppressed_at)].  Empty list when
    the table doesn't exist yet (host hasn't shipped a batch since the
    negative-cache feature landed)."""
    existing = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='wsprnet_reject_cache'"
    ).fetchone()
    if not existing:
        return []
    if rx_call:
        cur = conn.execute(
            """
            SELECT rx_call, call, rejected_count, first_rejected,
                   last_rejected, suppressed_at
              FROM wsprnet_reject_cache
             WHERE suppressed_at IS NOT NULL AND rx_call = ?
          ORDER BY last_rejected DESC
            """,
            (rx_call,),
        )
    else:
        cur = conn.execute(
            """
            SELECT rx_call, call, rejected_count, first_rejected,
                   last_rejected, suppressed_at
              FROM wsprnet_reject_cache
             WHERE suppressed_at IS NOT NULL
          ORDER BY last_rejected DESC
            """,
        )
    return list(cur)


def cmd_verifier_rehabilitate(args) -> int:
    """`smd admin verifier rehabilitate <rx_call> <call>` — operator override.

    Clears ``suppressed_at`` and zeros the counters for one row in
    ``wsprnet_reject_cache``.  The next ``CallsignDB.write_*`` pass
    will re-include the call, letting wsprd see it again.  Idempotent
    — re-running on an already-active call returns success with
    ``changed=0``.  Requires write access to sink.db so it self-
    elevates via the calling smd wrapper.
    """
    db_path = Path(
        args.db if hasattr(args, "db") and args.db
        else DEFAULT_SINK_DB
    )
    if not db_path.exists():
        print(
            f"smd admin verifier rehabilitate: sink db not found at {db_path}",
            file=sys.stderr,
        )
        return 2
    rx_call = (args.rx_call or "").strip()
    call = (args.call or "").strip().upper()
    if not rx_call or not call:
        print(
            "smd admin verifier rehabilitate: both rx_call and call are required",
            file=sys.stderr,
        )
        return 2
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error as exc:
        print(
            f"smd admin verifier rehabilitate: open sink db failed: {exc}",
            file=sys.stderr,
        )
        return 2
    try:
        existing = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='wsprnet_reject_cache'"
        ).fetchone()
        if not existing:
            print(
                "smd admin verifier rehabilitate: wsprnet_reject_cache "
                "table not present yet — nothing to rehabilitate."
            )
            return 0
        with conn:
            cur = conn.execute(
                """
                UPDATE wsprnet_reject_cache
                   SET suppressed_at = NULL,
                       rejected_count = 0
                 WHERE rx_call = ? AND call = ?
                """,
                (rx_call, call),
            )
            changed = cur.rowcount
        if changed:
            print(
                f"rehabilitated {call} for {rx_call} — wsprd will "
                "be re-fed this callsign on the next decode cycle"
            )
        else:
            print(
                f"no matching cache row for ({rx_call}, {call}) — "
                "nothing to do"
            )
        return 0
    finally:
        conn.close()
