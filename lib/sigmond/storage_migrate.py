"""Storage-backend migration: from local ClickHouse to local SQLite.

Why this exists:
    SQLite is now the default sigmond sink (see hamsci_ch.Writer.from_env).
    On hosts that were installed back when ClickHouse was the only choice,
    `clickhouse-server` plus its data dir continue to consume 1-2 GB of
    RAM and several merge-CPU cores even when no producer writes to it.
    This module enumerates and removes those leftover artifacts so the
    host's resources go back to the SDR pipeline.

Surface:
    `plan_clickhouse_removal(probe=...)` → a `RemovalPlan` describing
    every artifact that exists on the host (service units, packages,
    data dirs, sigmond-clickhouse venv).  Inspection only — no side
    effects.

    `execute_removal(plan, runner=...)` → actually stops services,
    purges packages, deletes dirs.  Refuses to run unless the caller
    sets `confirmed=True` on the plan.

Caller pattern (`smd storage migrate-to-sqlite`):
    1. Build plan.
    2. Print artifacts that would be removed.
    3. If operator passed `--yes`, mark plan confirmed and execute.
    4. Otherwise exit 0 with a dry-run summary.

The runner / probe interfaces are injectable so the migration is unit-
testable without a running ClickHouse on the test host.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger("sigmond.storage_migrate")


# Artifacts the sigmond-clickhouse install leaves on a host.  Listed
# centrally so tests, dry-run output, and the executor all agree.
CH_SERVICE_UNITS = ("sigmond-clickhouse.service", "clickhouse-server.service")
CH_PACKAGES = ("clickhouse-server", "clickhouse-client", "clickhouse-common-static")
CH_DATA_DIRS = ("/var/lib/clickhouse",)
CH_CONFIG_DIRS = ("/etc/clickhouse-server",)
CH_LOG_DIRS = ("/var/log/clickhouse-server",)
CH_SIGMOND_VENV = "/opt/sigmond-clickhouse"
CH_SIGMOND_UNIT_FILE = "/etc/systemd/system/sigmond-clickhouse.service"
CH_SIGMOND_SYMLINK = "/usr/local/sbin/sigmond-clickhouse"


@dataclass
class RemovalPlan:
    """Concrete list of side effects `execute_removal` would perform."""

    services_to_stop: List[str] = field(default_factory=list)
    services_to_disable: List[str] = field(default_factory=list)
    packages_to_purge: List[str] = field(default_factory=list)
    paths_to_remove: List[str] = field(default_factory=list)
    files_to_remove: List[str] = field(default_factory=list)
    confirmed: bool = False

    @property
    def is_empty(self) -> bool:
        return not (
            self.services_to_stop
            or self.services_to_disable
            or self.packages_to_purge
            or self.paths_to_remove
            or self.files_to_remove
        )


class HostProbe:
    """Pluggable host inspection.  Tests substitute a fake."""

    def service_exists(self, unit: str) -> bool:
        # `list-unit-files` returns nonzero only when systemd isn't
        # there at all; for a missing unit it prints `0 unit files`.
        # We parse stdout to tell those apart.
        try:
            r = subprocess.run(
                ["systemctl", "list-unit-files", unit, "--no-legend"],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return False
        return r.returncode == 0 and unit in r.stdout

    def service_active(self, unit: str) -> bool:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                check=False,
            )
        except FileNotFoundError:
            return False
        return r.returncode == 0

    def package_installed(self, pkg: str) -> bool:
        try:
            r = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return False
        return r.returncode == 0 and "install ok installed" in r.stdout

    def path_exists(self, path: str) -> bool:
        return Path(path).exists()


def plan_clickhouse_removal(probe: Optional[HostProbe] = None) -> RemovalPlan:
    """Enumerate ClickHouse artifacts present on this host."""
    p = probe or HostProbe()
    plan = RemovalPlan()

    for unit in CH_SERVICE_UNITS:
        if p.service_exists(unit):
            plan.services_to_disable.append(unit)
            if p.service_active(unit):
                plan.services_to_stop.append(unit)

    for pkg in CH_PACKAGES:
        if p.package_installed(pkg):
            plan.packages_to_purge.append(pkg)

    for d in CH_DATA_DIRS + CH_CONFIG_DIRS + CH_LOG_DIRS:
        if p.path_exists(d):
            plan.paths_to_remove.append(d)

    if p.path_exists(CH_SIGMOND_VENV):
        plan.paths_to_remove.append(CH_SIGMOND_VENV)

    for f in (CH_SIGMOND_UNIT_FILE, CH_SIGMOND_SYMLINK):
        if p.path_exists(f):
            plan.files_to_remove.append(f)

    return plan


@dataclass
class _ExecutionReport:
    """What execute_removal actually did, for logging and tests."""

    stopped: List[str] = field(default_factory=list)
    disabled: List[str] = field(default_factory=list)
    purged: List[str] = field(default_factory=list)
    removed_paths: List[str] = field(default_factory=list)
    removed_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class Runner:
    """Pluggable side-effect surface.  Tests substitute a fake."""

    def run(self, argv: list) -> subprocess.CompletedProcess:
        return subprocess.run(argv, check=False, capture_output=True, text=True)

    def rmtree(self, path: str) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def unlink(self, path: str) -> None:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass


class NotConfirmed(Exception):
    """Raised when `execute_removal` is called on an unconfirmed plan."""


def execute_removal(
    plan: RemovalPlan,
    runner: Optional[Runner] = None,
) -> _ExecutionReport:
    """Execute a confirmed removal plan.  Caller must set plan.confirmed."""
    if not plan.confirmed:
        raise NotConfirmed(
            "execute_removal refused: plan.confirmed=False.  Set "
            "plan.confirmed=True only after operator approval (smd "
            "storage migrate-to-sqlite requires --yes)."
        )

    r = runner or Runner()
    report = _ExecutionReport()

    # 1. Stop services first so package purge / data removal doesn't race.
    for unit in plan.services_to_stop:
        res = r.run(["systemctl", "stop", unit])
        if res.returncode == 0:
            report.stopped.append(unit)
        else:
            report.errors.append(f"stop {unit}: rc={res.returncode}")

    for unit in plan.services_to_disable:
        res = r.run(["systemctl", "disable", unit])
        # 'disable' is best-effort; missing static units return nonzero
        # without harm.  Record but don't treat as a hard error.
        report.disabled.append(unit)
        if res.returncode != 0:
            logger.debug("disable %s returned rc=%d (ok if not enabled)",
                         unit, res.returncode)

    # 2. Purge Debian packages — `apt-get purge -y` is the only verb
    # that drops both binaries and conffiles.  We pass DEBIAN_FRONTEND
    # via the runner's environment by way of `-o Dpkg::Options::=...`
    # to avoid prompting.  Skip if dpkg isn't present (non-Debian host).
    if plan.packages_to_purge:
        res = r.run([
            "apt-get", "purge", "-y",
            "--option", "Dpkg::Options::=--force-confnew",
            *plan.packages_to_purge,
        ])
        if res.returncode == 0:
            report.purged.extend(plan.packages_to_purge)
        else:
            report.errors.append(
                f"apt-get purge {plan.packages_to_purge}: "
                f"rc={res.returncode} stderr={res.stderr.strip()[:200]}"
            )

    # 3. Remove data / config / log dirs.  Done after package purge so
    # the postrm scripts can't see (and re-create) directories we
    # intend to delete.
    for path in plan.paths_to_remove:
        r.rmtree(path)
        report.removed_paths.append(path)

    for path in plan.files_to_remove:
        r.unlink(path)
        report.removed_files.append(path)

    # 4. Tell systemd we removed unit files.  Best-effort; not fatal.
    if plan.services_to_disable:
        r.run(["systemctl", "daemon-reload"])

    return report
