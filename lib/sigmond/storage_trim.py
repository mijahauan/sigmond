"""SQLite sink janitor.

The sink (``/var/lib/sigmond/sink.db``) is a FIFO queue: producers
append rows; hs-uploader drains them and ``commit()`` deletes acked
rows.  If a producer has no consumer wired (currently hfdl.spots and
timestd.events on bee1 — no external upload destination is configured
for them), nothing ever deletes their rows and the queue grows
unbounded.

This module provides a TTL-based DELETE: rows older than a given
``max_age`` are removed regardless of which (target_db, target_table)
they belong to.  Acked rows would already be gone via the source's
``commit()``; unacked rows that have aged out are presumed
unreachable or unwanted.

Same shape as ``sigmond.storage_migrate``:

* ``plan_trim()`` is pure — it inspects the sink and returns a
  ``TrimPlan`` describing what would be deleted.  Safe to call as
  any user that can read sink.db.
* ``execute_trim()`` does the destructive DELETE, requires
  ``plan.confirmed = True`` (set only after ``--yes``).
* Pluggable ``opener`` callable so tests can substitute a fake
  connection without touching the filesystem.

The CLI verb (``smd admin storage trim``) is in ``bin/smd``; this module
stays library-only so the same logic is unit-testable + reusable.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple


DEFAULT_DB_PATH = "/var/lib/sigmond/sink.db"

# Floor for any retention window applied via this module.  Below this,
# the local SQLite sink stops serving its second-consumer role (a
# near-real-time spot store that other tools — e.g. Andrew Roland's
# scrapers, sigmond watchers — can query).  The number is conservative
# on purpose: a misconfigured 1-minute retention would silently strand
# rows the uploader hadn't shipped yet.  Operators who genuinely want
# tighter cleanup must set TRIM_MIN_FLOOR_SECONDS in the environment.
DEFAULT_MIN_RETENTION_SECONDS = 30 * 60


def min_retention_seconds(env: Optional[dict] = None) -> float:
    """Resolve the per-host minimum retention floor.

    Default is 30 min (DEFAULT_MIN_RETENTION_SECONDS).  Override via
    ``TRIM_MIN_FLOOR_SECONDS`` (env var, integer seconds).  Returns the
    default on any parse failure — the floor is a safety property, so
    a typo must never silently shrink it to zero.
    """
    e = env if env is not None else os.environ
    raw = (e.get("TRIM_MIN_FLOOR_SECONDS") or "").strip()
    if not raw:
        return float(DEFAULT_MIN_RETENTION_SECONDS)
    try:
        v = float(raw)
        return v if v > 0 else float(DEFAULT_MIN_RETENTION_SECONDS)
    except ValueError:
        return float(DEFAULT_MIN_RETENTION_SECONDS)


class RetentionTooShort(ValueError):
    """Raised when a caller asks for a retention below the min floor."""


# Per-target retention defaults (in minutes), used by `policy_from_env`
# when no env override is present.  Operators with different needs can
# bump any of these by setting the matching env var; the floor still
# applies post-resolution.
#
#   psk.spots   — FT8/FT4 spots from psk-recorder.  60 min keeps a full
#                 cycle of decoders' worth of data around for the wsprdaemon
#                 tar transport's next pickup + a buffer for the
#                 forthcoming smd admin verifier report to inspect lost spots.
#   wspr.spots  — WSPR spots from wspr-recorder.  Longer (24h) because
#                 the verifier's cross-server diff currently needs that
#                 window to catch wd20-style backlog catch-ups.
#   superdarn.detections — no hs-uploader transport wired yet (VT egress
#                 pending), so rows queue with nothing to drain them.
#                 30 days bounds the growth; when a transport does land
#                 it will start_at="now", so aged rows have archive
#                 value only, never delivery value.
#   timestd.events / hfdl.spots — no external consumer wired today; the
#                 sink is the archive.  Operators choose how long to
#                 keep before truncating.
_DEFAULT_RETENTION_MINUTES: Dict[Tuple[str, str], int] = {
    ("psk",  "spots"):  60,
    ("wspr", "spots"):  24 * 60,
    ("wspr", "noise"):  24 * 60,
    ("superdarn", "detections"):  30 * 24 * 60,
}

_ENV_OVERRIDES: Dict[Tuple[str, str], str] = {
    ("psk",  "spots"):  "PSK_RETENTION_MIN",
    ("wspr", "spots"):  "WSPR_RETENTION_MIN",
    ("wspr", "noise"):  "WSPR_RETENTION_MIN",
    ("superdarn", "detections"):  "SUPERDARN_RETENTION_MIN",
}


@dataclass
class RetentionPolicy:
    """Resolved per-target retention plan, ready for plan_trim()."""
    target_db: str
    target_table: str
    max_age_seconds: float
    source: str  # "env:VARNAME" or "default" — for human-readable reporting


def policy_from_env(env: Optional[dict] = None,
                    *,
                    floor_seconds: Optional[float] = None,
                    ) -> List[RetentionPolicy]:
    """Build the retention-policy list from env vars + module defaults.

    Each entry pins one (target_db, target_table) to a max-age in
    seconds.  Env overrides take an integer minute count:

        PSK_RETENTION_MIN=120    # 2 h, applies to psk.spots
        WSPR_RETENTION_MIN=2880  # 48 h, applies to wspr.spots + wspr.noise

    Values below the floor are clamped UP to the floor; the
    ``source`` field on the returned policy records both the env var
    and the clamp ("env:PSK_RETENTION_MIN (clamped to floor)") so
    `smd admin storage trim --all` can surface the clamp to the operator.
    """
    e = env if env is not None else os.environ
    floor = floor_seconds if floor_seconds is not None else min_retention_seconds(e)
    out: List[RetentionPolicy] = []
    for (db, tbl), default_min in _DEFAULT_RETENTION_MINUTES.items():
        env_var = _ENV_OVERRIDES.get((db, tbl))
        raw = (e.get(env_var) or "").strip() if env_var else ""
        source = "default"
        if raw:
            try:
                minutes = float(raw)
                if minutes > 0:
                    source = f"env:{env_var}"
                    seconds = minutes * 60.0
                else:
                    seconds = default_min * 60.0
            except ValueError:
                seconds = default_min * 60.0
        else:
            seconds = default_min * 60.0
        if seconds < floor:
            source = f"{source} (clamped to floor {int(floor)}s)"
            seconds = floor
        out.append(RetentionPolicy(
            target_db=db,
            target_table=tbl,
            max_age_seconds=seconds,
            source=source,
        ))
    return out


class NotConfirmed(Exception):
    """Raised when execute_trim is called without plan.confirmed=True."""


@dataclass
class TrimPlan:
    """Concrete description of what `execute_trim` would delete."""

    db_path: str
    cutoff_iso: str
    # [(target_db, target_table, row_count), ...] for rows older than cutoff.
    # Grouped + ordered by (target_db, target_table) for stable display.
    rows_per_target: List[Tuple[str, str, int]] = field(default_factory=list)
    # Optional filters narrowing the DELETE.  When set, only rows
    # matching ALL set filters are eligible.  None means "any value".
    # Used by operators who want different TTLs per producer — e.g.,
    # hfdl gets 24 h (live-shipped to airframes.io by dumphfdl, the
    # SQLite copy is just a local archive) while timestd.events
    # retains for the science archive's lifetime.
    target_db: Optional[str] = None
    target_table: Optional[str] = None
    confirmed: bool = False

    @property
    def total_rows(self) -> int:
        return sum(n for _, _, n in self.rows_per_target)

    @property
    def is_empty(self) -> bool:
        return self.total_rows == 0


@dataclass
class TrimReport:
    rows_deleted: int = 0
    errors: List[str] = field(default_factory=list)


Opener = Callable[[str], sqlite3.Connection]


def _default_opener(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=30.0)


def plan_trim(
    db_path: str,
    max_age_seconds: float,
    *,
    target_db: Optional[str] = None,
    target_table: Optional[str] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    opener: Optional[Opener] = None,
    enforce_floor: bool = True,
    floor_seconds: Optional[float] = None,
) -> TrimPlan:
    """Inspect `db_path` and report rows with `queued_at < now - max_age`.

    Optional filters narrow the eligible set:
      - ``target_db`` ("hfdl", "timestd", "psk", ...) restricts the
        DELETE to one logical sink.  Operators set this when they
        want different TTLs per producer.
      - ``target_table`` further narrows within a target_db.

    Empty plan when:
    - the sink db doesn't exist (no producer has flushed)
    - the pending_uploads table doesn't exist (same — first-flush race)
    - the sink db is unreadable to this user
    - no rows match the cutoff under the active filters

    None of these are errors at the planning layer; a `TrimPlan` with
    no rows means "nothing to do" and `execute_trim` becomes a no-op.
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    opener = opener or _default_opener
    if enforce_floor:
        floor = floor_seconds if floor_seconds is not None else min_retention_seconds()
        if max_age_seconds < floor:
            raise RetentionTooShort(
                f"max_age_seconds={max_age_seconds:.0f}s is below the "
                f"min retention floor ({int(floor)}s = {int(floor/60)}min). "
                f"Override with TRIM_MIN_FLOOR_SECONDS, or pass "
                f"enforce_floor=False if you know what you're doing."
            )
    cutoff_dt = now_fn() - timedelta(seconds=max_age_seconds)
    cutoff_iso = cutoff_dt.isoformat()
    plan = TrimPlan(
        db_path=db_path,
        cutoff_iso=cutoff_iso,
        target_db=target_db,
        target_table=target_table,
    )
    try:
        conn = opener(db_path)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
        return plan
    try:
        clauses = ["queued_at < ?"]
        params: list = [cutoff_iso]
        if target_db is not None:
            clauses.append("target_db = ?")
            params.append(target_db)
        if target_table is not None:
            clauses.append("target_table = ?")
            params.append(target_table)
        where = " AND ".join(clauses)
        cur = conn.execute(
            f"SELECT target_db, target_table, COUNT(*) "
            f"FROM pending_uploads "
            f"WHERE {where} "
            f"GROUP BY target_db, target_table "
            f"ORDER BY target_db, target_table",
            params,
        )
        plan.rows_per_target = [
            (str(db), str(tbl), int(n)) for db, tbl, n in cur.fetchall()
        ]
    except sqlite3.OperationalError:
        # pending_uploads doesn't exist yet — first-flush race; treat
        # as empty plan, not an error.
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return plan


def execute_trim(
    plan: TrimPlan,
    *,
    opener: Optional[Opener] = None,
) -> TrimReport:
    """Apply the plan: DELETE rows with `queued_at < plan.cutoff_iso`.

    Requires `plan.confirmed = True`; raise `NotConfirmed` otherwise so
    a forgotten `--yes` doesn't silently destroy data.  Idempotent: a
    second call with the same plan deletes 0 rows.

    Failures (sink unreadable, transient lock, etc.) are recorded on
    the report rather than raised — the caller can decide whether a
    partial trim is OK.
    """
    if not plan.confirmed:
        raise NotConfirmed(
            "execute_trim refused: plan.confirmed=False. Set "
            "plan.confirmed=True only after operator approval (smd "
            "storage trim requires --yes)."
        )
    report = TrimReport()
    if plan.is_empty:
        return report
    opener = opener or _default_opener
    conn: Optional[sqlite3.Connection] = None
    clauses = ["queued_at < ?"]
    params: list = [plan.cutoff_iso]
    if plan.target_db is not None:
        clauses.append("target_db = ?")
        params.append(plan.target_db)
    if plan.target_table is not None:
        clauses.append("target_table = ?")
        params.append(plan.target_table)
    where = " AND ".join(clauses)
    try:
        conn = opener(plan.db_path)
        with conn:
            cur = conn.execute(
                f"DELETE FROM pending_uploads WHERE {where}",
                params,
            )
            report.rows_deleted = int(cur.rowcount)
    except Exception as exc:
        report.errors.append(f"DELETE on {plan.db_path}: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return report


@dataclass
class PolicyPlan:
    """One resolved policy paired with its planned trim."""
    policy: RetentionPolicy
    plan: TrimPlan


def plan_trim_all(
    db_path: str,
    *,
    policies: Optional[List[RetentionPolicy]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    opener: Optional[Opener] = None,
) -> List[PolicyPlan]:
    """Walk every retention policy and plan a per-(db,table) trim.

    Returns one PolicyPlan per policy, in the order policies were
    given (or the env-derived default order).  Empty plans are kept —
    they're still operationally interesting ("no rows to trim for
    psk.spots — uploader is keeping up").
    """
    policies = policies if policies is not None else policy_from_env()
    out: List[PolicyPlan] = []
    for pol in policies:
        plan = plan_trim(
            db_path,
            pol.max_age_seconds,
            target_db=pol.target_db,
            target_table=pol.target_table,
            now_fn=now_fn,
            opener=opener,
            # policy_from_env already clamped; don't double-enforce.
            enforce_floor=False,
        )
        out.append(PolicyPlan(policy=pol, plan=plan))
    return out


def execute_trim_all(
    policy_plans: List[PolicyPlan],
    *,
    opener: Optional[Opener] = None,
) -> List[Tuple[RetentionPolicy, TrimReport]]:
    """Execute every plan in `policy_plans` and return per-policy reports.

    Each ``plan.confirmed`` must already be True (the CLI sets it after
    ``--yes``).  Any single un-confirmed plan raises ``NotConfirmed``
    immediately — no partial execution.
    """
    out: List[Tuple[RetentionPolicy, TrimReport]] = []
    for pp in policy_plans:
        report = execute_trim(pp.plan, opener=opener)
        out.append((pp.policy, report))
    return out


def parse_duration(spec: str) -> float:
    """Parse `30s`, `5m`, `2h`, `7d` (or a bare number = seconds).

    Returns seconds as a float.  Raises ValueError on a malformed
    spec — caller (CLI verb) should surface that to the operator.
    """
    s = spec.strip().lower()
    if not s:
        raise ValueError("empty duration")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in units:
        head, suffix = s[:-1], s[-1]
        try:
            value = float(head)
        except ValueError as e:
            raise ValueError(f"invalid number in duration {spec!r}: {e}")
        if value < 0:
            raise ValueError(f"negative duration not allowed: {spec!r}")
        return value * units[suffix]
    # Bare number = seconds.
    try:
        value = float(s)
    except ValueError as e:
        raise ValueError(f"invalid duration {spec!r}: {e}")
    if value < 0:
        raise ValueError(f"negative duration not allowed: {spec!r}")
    return value
