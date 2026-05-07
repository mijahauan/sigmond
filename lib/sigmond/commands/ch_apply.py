"""ClickHouse schema migration runner — invoked by ``smd apply``.

Walks every installed client's ``deploy.toml`` for a ``[clickhouse]``
block, then runs the SQL migrations declared by each.  Idempotent (the
migration files use ``CREATE … IF NOT EXISTS`` and ``ADD COLUMN IF NOT
EXISTS`` so repeated runs are safe).

Discovered shapes (one ``[clickhouse]`` block per repo):

    [clickhouse]
    database         = "psk"
    schema_dir       = "clickhouse/schema/psk"      # relative to repo root
    schema_version   = 2
    required_min_ch  = "23.8"

A repo with ``schema_dir = ""`` (or no key) is skipped — that's the
``wsprdaemon-client`` shape, which references the wire-pinned WSPR
schema vendored inside ``sigmond-clickhouse`` and so doesn't ship its
own DDL.

When ``[storage.clickhouse]`` is absent from coordination.toml the
runner is a clean no-op — operators who haven't opted into CH stay
file-only.

This module is the single place that owns "apply CH schemas across the
whole sigmond suite" — both ``smd apply`` and any future ``smd ch
apply`` verb call into it.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from sigmond.coordination import Coordination, ClickHouseStorage

log = logging.getLogger(__name__)

CATALOG_REPO_ROOT = Path("/opt/git/sigmond")


@dataclass(frozen=True)
class ClientCh:
    """Per-client ClickHouse-schema binding discovered from deploy.toml."""

    client_name: str
    repo_dir: Path
    database: str
    schema_dir: Path        # absolute (repo_dir / [clickhouse].schema_dir)
    schema_version: int
    required_min_ch: Optional[str]


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of running migrations for one client."""

    client_name: str
    database: str
    applied: List[str]      # filenames in execution order
    skipped: List[str]      # filenames that errored (with error appended)
    error: Optional[str]    # first fatal error (None on success)


# ── discovery ──────────────────────────────────────────────────────────────

def discover_clients_with_ch_schemas(
    catalog_root: Path = CATALOG_REPO_ROOT,
) -> List[ClientCh]:
    """Return every installed client whose ``deploy.toml`` has a
    ``[clickhouse]`` block with a non-empty ``schema_dir``.

    Empty ``schema_dir`` is treated as "this client uses someone
    else's vendored schema" (e.g. wsprdaemon-client points at
    sigmond-clickhouse's wire-pinned WSPR DDL) and is skipped here —
    such clients land via the sigmond-clickhouse repo's own
    ``[clickhouse]`` entry.
    """
    out: List[ClientCh] = []
    if not catalog_root.exists():
        return out

    for repo_dir in sorted(catalog_root.iterdir()):
        if not repo_dir.is_dir():
            continue
        deploy_toml = repo_dir / "deploy.toml"
        if not deploy_toml.exists():
            continue
        try:
            doc = tomllib.loads(deploy_toml.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("ch_apply: cannot read %s: %s", deploy_toml, exc)
            continue

        ch = doc.get("clickhouse")
        if not isinstance(ch, dict):
            continue
        schema_dir_rel = (ch.get("schema_dir") or "").strip()
        if not schema_dir_rel:
            # Empty: client references vendored schema elsewhere.
            continue
        database = (ch.get("database") or "").strip()
        if not database:
            log.warning("ch_apply: %s has [clickhouse] without database name; skipping",
                        deploy_toml)
            continue
        schema_dir = (repo_dir / schema_dir_rel).resolve()
        if not schema_dir.exists():
            log.warning("ch_apply: %s declares schema_dir=%s but the directory "
                        "doesn't exist; skipping", repo_dir.name, schema_dir_rel)
            continue
        out.append(ClientCh(
            client_name=repo_dir.name,
            repo_dir=repo_dir,
            database=database,
            schema_dir=schema_dir,
            schema_version=int(ch.get("schema_version", 1)),
            required_min_ch=ch.get("required_min_ch"),
        ))
    return out


# ── migration runner ───────────────────────────────────────────────────────

def list_migrations(schema_dir: Path) -> List[Path]:
    """Return ``[0-9]*.sql`` files in lexical order — same convention
    used by ``sigmond-clickhouse migrate``."""
    if not schema_dir.is_dir():
        return []
    return sorted(schema_dir.glob("[0-9]*.sql"))


def run_client_migrations(
    client_ch: ClientCh,
    *,
    ch_client,
    dry_run: bool = False,
) -> MigrationResult:
    """Apply each migration file under ``client_ch.schema_dir`` to CH.

    ``ch_client`` is a connected ``clickhouse_connect`` client (the
    caller is responsible for connection management — this module
    deliberately doesn't import the driver).  Each ``.sql`` file is
    executed via ``ch_client.command(sql)``.  Failure on any file
    halts the run for THIS client; subsequent migrations in the same
    client are not attempted.  The error is reported but does not
    propagate to other clients (the caller iterates).
    """
    applied: List[str] = []
    skipped: List[str] = []
    error: Optional[str] = None

    for sql_file in list_migrations(client_ch.schema_dir):
        rel = f"{client_ch.client_name}/{sql_file.name}"
        if dry_run:
            applied.append(f"(dry-run) {rel}")
            continue
        try:
            sql = sql_file.read_text()
        except OSError as exc:
            error = f"read {sql_file}: {exc}"
            skipped.append(f"{rel} ({exc})")
            break
        try:
            ch_client.command(sql)
            applied.append(rel)
            log.info("ch_apply: applied %s", rel)
        except Exception as exc:                 # noqa: BLE001 — surface any driver error
            error = f"{rel}: {exc}"
            skipped.append(f"{rel} ({exc})")
            log.error("ch_apply: failed %s: %s", rel, exc)
            break

    return MigrationResult(
        client_name=client_ch.client_name,
        database=client_ch.database,
        applied=applied,
        skipped=skipped,
        error=error,
    )


# ── top-level entrypoint ───────────────────────────────────────────────────

def apply_ch_schemas(
    coord: Coordination,
    *,
    dry_run: bool = False,
    catalog_root: Path = CATALOG_REPO_ROOT,
    client_factory=None,
) -> List[MigrationResult]:
    """Apply CH schemas across every installed client.  No-op when
    ``coord.storage.clickhouse`` is unconfigured.

    Returns one ``MigrationResult`` per client that was processed.
    Returns an empty list when CH isn't configured (operator stays
    file-only).

    ``client_factory`` is for tests: a callable that returns a
    fake ch_client when called with a ``ClickHouseStorage`` config.
    Default constructs a ``clickhouse_connect`` client via the same
    env-var path the rest of the suite uses.
    """
    storage = coord.storage.clickhouse if coord.storage else None
    if storage is None:
        log.debug("ch_apply: [storage.clickhouse] not configured; skipping")
        return []

    clients = discover_clients_with_ch_schemas(catalog_root)
    if not clients:
        log.debug("ch_apply: no installed clients have a [clickhouse] block")
        return []

    factory = client_factory or _default_client_factory
    if dry_run:
        # Dry-run path: don't connect.  Each migration is just listed.
        return [
            run_client_migrations(c, ch_client=None, dry_run=True)
            for c in clients
        ]

    ch_client = factory(storage)
    try:
        return [
            run_client_migrations(c, ch_client=ch_client, dry_run=False)
            for c in clients
        ]
    finally:
        try:
            ch_client.close()
        except Exception:
            pass


def _default_client_factory(storage: ClickHouseStorage):
    """Construct a real ``clickhouse_connect`` client.

    Lazy-imported so sigmond's core stays stdlib-only when CH isn't
    in use.  Raises ``ImportError`` with a clear message when the
    optional package isn't installed.
    """
    try:
        import clickhouse_connect  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "ch_apply: clickhouse-connect not installed.  "
            "`pip install sigmond[clickhouse]` (or add the dep to your venv) "
            "to enable schema migrations."
        ) from e

    password = ""
    pwfile = storage.password_file
    if pwfile and Path(pwfile).exists():
        try:
            password = Path(pwfile).read_text().strip()
        except OSError:
            pass

    return clickhouse_connect.get_client(
        host=storage.host,
        port=storage.http_port,
        username=storage.user,
        password=password,
    )


# ── result rendering helpers (used by smd apply) ───────────────────────────

def summarise(results: Iterable[MigrationResult]) -> List[str]:
    """Format a result list as terminal-friendly lines (no colour codes —
    the caller adds those)."""
    out: List[str] = []
    for r in results:
        if r.error:
            out.append(f"{r.client_name} ({r.database}): error — {r.error}")
        elif r.applied:
            out.append(
                f"{r.client_name} ({r.database}): "
                f"{len(r.applied)} migration(s) applied"
            )
        else:
            out.append(f"{r.client_name} ({r.database}): no migrations to run")
    return out
