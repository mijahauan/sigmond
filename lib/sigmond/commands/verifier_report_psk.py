"""`smd verifier report --target psk` — round-trip verification of
psk.spots rows we sent via the wsprdaemon-tar transport.

The Phase 2 forwarding path is:

  psk-recorder ch_tailer  →  /var/lib/sigmond/sink.db (target_db='psk')
                          →  hs-uploader wsprdaemon-tar transport
                          →  wd{10,20,30}  psk.spots ClickHouse table
                          →  gw1-elected pskreporter_forwarder
                          →  pskreporter.info

PR 7 verifies the second arrow: did the rows we wrote locally
actually land in at least one wd*'s ``psk.spots``?  That's the
hop we own end-to-end (the producer is sigmond's local SQLite,
the consumer is wsprdaemon-server's ClickHouse).  The third
arrow's success — actual PSKReporter ingestion — is out of
band: there's no scraping path back from pskreporter.info, and
PSKReporter dedups so retries don't compound.

Three operator-facing cohorts, matching the WSPR-side report:

  * delivered — local row appears in at least one wd*.psk.spots
  * lost      — local row was queued > in-flight-window minutes
                ago, still absent from every wd*
  * in_flight — local row was queued recently; still inside the
                expected delivery window (tar cadence + ingest lag)

Plus cadence: did we miss any expected FT decode cycles?  FT8
cycles are 15 s, FT4 cycles 7.5 s; in a healthy minute we expect
4 FT8 cycles and 8 FT4 cycles.  A sudden zero-count cycle is a
recorder hiccup; a long run of zeros is the symptom that radiod
or psk-recorder fell over.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


DEFAULT_SINK_DB = "/var/lib/sigmond/sink.db"
DEFAULT_WD_URLS = (
    "http://wd10.wsprdaemon.org,"
    "http://wd20.wsprdaemon.org,"
    "http://wd30.wsprdaemon.org"
)
DEFAULT_IN_FLIGHT_WINDOW_SEC = 300         # 5 min — covers tar cadence + ingest
DEFAULT_HTTP_TIMEOUT_SEC = 5


# Per-spot identity that's robust across the wire:
#   * `epoch` is floored to the second (FT timing carries seconds; rounding
#     to the minute like WSPR would collapse multiple decodes per cycle)
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


# ── Local-side: read rows from sink.db ──────────────────────────────────────

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
    forwardable (they wouldn't have landed in upstream psk.spots either)
    and including them would inflate the "lost" cohort spuriously.
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
    """Read rows the local sink wrote into pending_uploads for psk.spots.

    `forward_only`: when True (default), include only rows the producer
    flagged ``forward_to_pskreporter=true``.  That's the cohort PR 7 is
    actually verifying — the ``both`` / ``direct`` modes don't go via
    wd* and we'd over-count "lost" otherwise.
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
        out.append(LocalRow(
            key=key, queued_at=qdt,
            rx_sign=str(payload.get("rx_sign") or ""),
            mode=key[1], tx_call=key[2], frequency=key[3],
        ))
    return out


# ── Upstream-side: query wd*.psk.spots over ClickHouse HTTP ─────────────────

@dataclass
class UpstreamResult:
    url:      str        # userinfo stripped — safe to log
    keys:     Set[SpotKey] = field(default_factory=set)
    max_time: Optional[datetime] = None
    rtt_ms:   int = 0
    error:    Optional[str] = None


def _split_userinfo(url: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Strip a ``user:pass@`` segment from an HTTP URL.

    Mirrors wspr-recorder's wsprdaemon_verifier helper so the report
    can take its --urls in the same form as the in-recorder thread.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is None:
        return url, None, None
    user = urllib.parse.unquote(parsed.username)
    password = (urllib.parse.unquote(parsed.password)
                if parsed.password is not None else "")
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    clean = urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment),
    )
    return clean, user, password


def _build_psk_query(reporter: Optional[str], window_min: int) -> str:
    where_parts = [
        f"time>=now('UTC')-INTERVAL {int(window_min)} MINUTE",
    ]
    if reporter:
        # rx_sign is taken from local config (not user input); ClickHouse
        # allows single quotes around string literals.
        where_parts.append(f"rx_sign='{reporter}'")
    where = " AND ".join(where_parts)
    return (
        "SELECT toUnixTimestamp(time) AS t, "
        "       lower(mode) AS mode, "
        "       upper(tx_call) AS tx, "
        "       frequency "
        f"FROM psk.spots "
        f"WHERE {where} "
        "FORMAT TabSeparated"
    )


def query_psk_server(
    url: str,
    *,
    reporter: Optional[str],
    window_min: int,
    timeout_sec: int = DEFAULT_HTTP_TIMEOUT_SEC,
) -> UpstreamResult:
    """Issue one read against a wd*'s ``psk.spots`` table.

    Returns the per-server set of SpotKey tuples and the most-recent
    time observed (so the caller can detect a stale server even when
    its set is non-empty).  Failures are recorded on the result rather
    than raised — one bad server shouldn't blank the whole report.
    """
    clean_url, user, password = _split_userinfo(url)
    res = UpstreamResult(url=clean_url)
    sql = _build_psk_query(reporter, window_min)
    qs = urllib.parse.urlencode({"query": sql})
    full_url = f"{clean_url}/?{qs}"
    req = urllib.request.Request(full_url)
    if user is not None:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as r:
            body = r.read().decode("utf-8", errors="replace").strip()
    except (urllib.error.URLError, OSError) as exc:
        res.error = str(exc)
        return res
    res.rtt_ms = int((time.monotonic() - t0) * 1000)

    max_epoch = 0
    for line in body.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            epoch = int(parts[0])
            mode  = parts[1].lower()
            tx    = parts[2].upper()
            freq  = int(parts[3])
        except ValueError:
            continue
        res.keys.add((epoch, mode, tx, freq))
        if epoch > max_epoch:
            max_epoch = epoch
    if max_epoch:
        res.max_time = datetime.fromtimestamp(max_epoch, tz=timezone.utc)
    return res


def query_all(
    urls: Iterable[str],
    *,
    reporter: Optional[str],
    window_min: int,
    timeout_sec: int = DEFAULT_HTTP_TIMEOUT_SEC,
) -> List[UpstreamResult]:
    """Fan out one round-trip per server in parallel.  Order of the
    returned list mirrors the input order so the operator's diff is
    stable across runs.
    """
    urls = list(urls)
    with ThreadPoolExecutor(max_workers=max(1, len(urls))) as ex:
        futs = [ex.submit(query_psk_server, u,
                          reporter=reporter, window_min=window_min,
                          timeout_sec=timeout_sec)
                for u in urls]
        return [f.result() for f in futs]


# ── Cohort assignment ────────────────────────────────────────────────────────

@dataclass
class Cohorts:
    delivered: List[LocalRow] = field(default_factory=list)
    lost:      List[LocalRow] = field(default_factory=list)
    in_flight: List[LocalRow] = field(default_factory=list)


def classify(
    local_rows: List[LocalRow],
    upstream_union: FrozenSet[SpotKey],
    *,
    now: datetime,
    in_flight_window_sec: int = DEFAULT_IN_FLIGHT_WINDOW_SEC,
) -> Cohorts:
    """Bucket each local row into delivered / lost / in_flight.

    A row is *delivered* iff its SpotKey appears in at least one wd*'s
    upstream set — the union over all responding servers.  The wsprdaemon
    tars usually fan out to multiple wd servers, so success on any single
    one is enough.

    A row is *in-flight* when it was queued more recently than
    `in_flight_window_sec` ago and isn't in the upstream union — we
    don't yet expect it to be there.

    A row is *lost* when it was queued earlier than the window and
    still isn't in the upstream union.  This is the operator's primary
    investigation cohort.
    """
    cutoff = now - timedelta(seconds=in_flight_window_sec)
    c = Cohorts()
    for row in local_rows:
        if row.key in upstream_union:
            c.delivered.append(row)
        elif row.queued_at >= cutoff:
            c.in_flight.append(row)
        else:
            c.lost.append(row)
    return c


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

    Reported per-mode so the operator can see a partial outage that hit
    only FT4 (radiod restart between ft4 cycles, etc.) without summing
    away the asymmetry.
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

def _format_summary(
    *,
    window_label: str,
    rx_call: Optional[str],
    n_local: int,
    cohorts: Cohorts,
    upstream: List[UpstreamResult],
) -> List[str]:
    out = []
    out.append(
        f"window: last {window_label}   "
        f"reporter: {rx_call or '(all)'}   target: psk.spots"
    )
    out.append("")
    if not n_local:
        out.append("local rows queued for forwarding in window: 0")
        out.append("  (nothing to verify — check WSPRDAEMON_INGEST_PSK / "
                   "PSK_DELIVERY_MODE on this host)")
    else:
        d, l, f = (len(cohorts.delivered), len(cohorts.lost),
                   len(cohorts.in_flight))
        dp = 100.0 * d / n_local
        lp = 100.0 * l / n_local
        fp = 100.0 * f / n_local
        out.append(f"local rows queued for forwarding: {n_local}")
        out.append(f"  delivered: {d:6d}  ({dp:5.1f}%)  "
                   "in at least one wd*.psk.spots")
        out.append(f"  lost:      {l:6d}  ({lp:5.1f}%)  "
                   "queued > in-flight window, still absent everywhere")
        out.append(f"  in_flight: {f:6d}  ({fp:5.1f}%)  "
                   "still inside the expected delivery window")
    out.append("")
    out.append("upstream servers:")
    for r in upstream:
        if r.error:
            out.append(f"  {r.url:50s}  ERROR: {r.error}")
        else:
            mt = r.max_time.isoformat(timespec='seconds') if r.max_time else '-'
            out.append(f"  {r.url:50s}  rows={len(r.keys):6d}  "
                       f"max_time={mt}  rtt={r.rtt_ms}ms")
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


def _format_lost(cohort: List[LocalRow], cap: int = 50) -> List[str]:
    out = []
    rows = sorted(cohort, key=lambda r: (r.key[0], r.tx_call))
    n = len(rows)
    if cap and n > cap:
        out.append(f"lost spots ({n} — showing first {cap} by spot time):")
        rows = rows[:cap]
    else:
        out.append(f"lost spots ({n}):")
    for r in rows:
        ts = datetime.fromtimestamp(r.key[0], tz=timezone.utc) \
                  .strftime("%H:%M:%S")
        out.append(
            f"  {ts}  {r.mode:4s}  {r.tx_call:<10}  "
            f"{r.frequency:>10} Hz  "
            f"queued={r.queued_at.strftime('%H:%M:%S')}"
        )
    return out


# ── Default URL resolution ──────────────────────────────────────────────────

def _resolve_urls(arg_value: Optional[str]) -> List[str]:
    """Pick the source for --urls in priority order:

      1. --urls explicit flag
      2. WSPRDAEMON_VERIFY_URLS env var (shared with wspr-recorder's verifier)
      3. DEFAULT_WD_URLS

    Empty entries are skipped so a trailing comma in the env var doesn't
    produce a zero-host probe.
    """
    raw = arg_value or os.environ.get("WSPRDAEMON_VERIFY_URLS") \
        or DEFAULT_WD_URLS
    return [u.strip() for u in raw.split(",") if u.strip()]


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

        urls = _resolve_urls(getattr(args, "psk_urls", None))
        window_min = max(1, int(window.total_seconds() // 60))
        upstream = query_all(urls, reporter=rx_call, window_min=window_min)
        upstream_union: Set[SpotKey] = set()
        for r in upstream:
            upstream_union |= r.keys

        # argparse default is None (so we can detect "not passed"); turn
        # that into the module default here rather than at parser
        # construction time so the env-var path can also override.
        in_flight_sec = (getattr(args, "psk_in_flight_sec", None)
                         or DEFAULT_IN_FLIGHT_WINDOW_SEC)
        cohorts = classify(local_rows, frozenset(upstream_union),
                           now=now, in_flight_window_sec=in_flight_sec)

        cadence = cadence_stats(local_rows, since=since, until=now)

        if args.json:
            payload = {
                "target": "psk",
                "window": args.window,
                "rx_call": rx_call,
                "n_local": len(local_rows),
                "delivered": len(cohorts.delivered),
                "lost":      len(cohorts.lost),
                "in_flight": len(cohorts.in_flight),
                "upstream": [
                    {
                        "url": r.url,
                        "rows": len(r.keys),
                        "max_time": (r.max_time.isoformat(timespec="seconds")
                                     if r.max_time else None),
                        "rtt_ms": r.rtt_ms,
                        "error": r.error,
                    } for r in upstream
                ],
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
                payload["lost_spots"] = [
                    {
                        "epoch": r.key[0], "mode": r.mode,
                        "tx_call": r.tx_call, "frequency": r.frequency,
                        "queued_at": r.queued_at.isoformat(timespec="seconds"),
                    } for r in cohorts.lost
                ]
            print(json.dumps(payload, indent=2))
            return 0

        for line in _format_summary(
            window_label=args.window, rx_call=rx_call,
            n_local=len(local_rows), cohorts=cohorts, upstream=upstream,
        ):
            print(line)
        print()
        for line in _format_cadence(cadence):
            print(line)
        if getattr(args, "lost", False) and cohorts.lost:
            print()
            for line in _format_lost(cohorts.lost):
                print(line)
        return 0
    finally:
        conn.close()
