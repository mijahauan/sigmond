"""Storage-backend migration: from local ClickHouse to local SQLite.

Why this exists:
    SQLite is the sigmond sink (see hamsci_sink.Writer.from_env).
    On hosts that were installed back when ClickHouse was the only choice,
    `clickhouse-server` plus its data dir continue to consume 1-2 GB of
    RAM and several merge-CPU cores even when no producer writes to it.
    This module enumerates and removes those leftover artifacts so the
    host's resources go back to the SDR pipeline.

Surface:
    `plan_clickhouse_removal(probe=...)` → a `RemovalPlan` describing
    every artifact that exists on the host (service units, packages,
    data dirs, sigmond-clickhouse venv) PLUS any `SIGMOND_CLICKHOUSE_*`
    lines in `/etc/sigmond/coordination.env` and the producer services
    that consume that file (so they can be restarted onto SQLite).
    Inspection only — no side effects.

    `execute_removal(plan, runner=...)` → actually rewrites the env
    file, restarts consumers (so they pick up SQLite before CH dies),
    stops services, purges packages, deletes dirs.  Refuses to run
    unless the caller sets `confirmed=True` on the plan.

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
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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

# Where sigmond writes its shared producer-side env vars.  Producers
# (psk-recorder, hf-timestd, etc.) pull this via
# systemd EnvironmentFile=, so flipping SIGMOND_CLICKHOUSE_URL off here
# is what makes them fall through to the default-SQLite dispatch on
# next restart.
DEFAULT_COORD_ENV = "/etc/sigmond/coordination.env"

# Pattern for env-var assignments we want to neutralize.  Matches the
# whole `SIGMOND_CLICKHOUSE_*=...` family; already-commented lines
# (starting with `#`) won't match, so re-running the verb is a no-op.
_CH_ENV_RE = re.compile(r"^\s*SIGMOND_CLICKHOUSE_[A-Z_]*\s*=")

# Pattern for the SQLite path var.  Used to detect whether the operator
# (or a previous run of this verb) has already pinned an explicit path.
# re.MULTILINE because the line is rarely the first in the file.
_SQLITE_PATH_ENV_RE = re.compile(r"^\s*SIGMOND_SQLITE_PATH\s*=", re.MULTILINE)

# Prefix we prepend when commenting out a live env-var.  Distinctive
# enough to spot in a diff, and preserves the audit trail of what was
# set pre-migration.
NEUTRALIZED_PREFIX = "# pre-sqlite-migration: "

# Header we append above any auto-inserted SIGMOND_SQLITE_PATH=, so
# operators can grep "added by smd" and know what wrote it.
INSERTED_HEADER = "# Added by `smd storage migrate-to-sqlite` to pin the SQLite sink."

# Backup suffix for any file we rewrite.  Operator can `mv` the .bak
# back if the migration was a mistake.
BACKUP_SUFFIX = ".bak-pre-sqlite"

# Producer-side sink location.  Producers run as non-root users (pskrec,
# hf-timestd, etc.); putting the sink under
# /var/lib/sigmond with mode 0775 root:sigmond means every producer in
# the `sigmond` group can write rows here, and a single hs-uploader
# reader can drain everything from one place.  Don't change the path
# without also bumping the `hs-uploader` reader's default — they're
# expected to agree out of the box.
SINK_DIR = "/var/lib/sigmond"
SINK_DB_PATH = "/var/lib/sigmond/sink.db"
SINK_GROUP = "sigmond"
# 0o2775 = setgid (g+s) so files created inside inherit group=sigmond
# regardless of the producer's primary group.  Without setgid, the
# first producer's group (e.g. hfdlrec) would own sink.db's group,
# locking out other sigmond-group producers — observed in practice
# during the bee1 migration on 2026-05-12.
SINK_DIR_MODE = 0o2775
# Pre-create the sink db itself so it doesn't end up owned by whoever
# happens to flush first.  0o664 = sigmond-group-writable matching
# SqliteWriter's expectation; root:sigmond so any producer in the
# group can write but nobody else can read the queue.
SINK_DB_MODE = 0o664

# systemd drop-in template that grants a sandboxed consumer write
# access to the sink dir.  Producer units use ProtectSystem=strict
# plus a ReadWritePaths= list that doesn't include /var/lib/sigmond;
# without this drop-in the dir is read-only inside the unit's mount
# namespace (the perms on the host don't matter — systemd binds a
# read-only overlay) and SqliteWriter falls back to silent noop.
SYSTEMD_DROPIN_BASENAME = "sigmond-sqlite-sink.conf"
SYSTEMD_DROPIN_CONTENT = (
    "# Added by `smd storage migrate-to-sqlite` so this producer can\n"
    "# write to /var/lib/sigmond/sink.db.  Without it, ProtectSystem=strict\n"
    "# would make the sink dir read-only inside the unit's namespace and\n"
    "# producer flushes would silently turn into no-ops.\n"
    "[Service]\n"
    "ReadWritePaths=/var/lib/sigmond\n"
)


@dataclass
class RemovalPlan:
    """Concrete list of side effects `execute_removal` would perform."""

    services_to_stop: List[str] = field(default_factory=list)
    services_to_disable: List[str] = field(default_factory=list)
    packages_to_purge: List[str] = field(default_factory=list)
    paths_to_remove: List[str] = field(default_factory=list)
    files_to_remove: List[str] = field(default_factory=list)
    # (env_file_path, the_live_line_text) — recorded so the dry-run can
    # show operators exactly which lines will be commented out.
    env_lines_to_neutralize: List[Tuple[str, str]] = field(default_factory=list)
    # (env_file_path, "KEY=value") — lines to APPEND if not already present.
    # Currently used to pin SIGMOND_SQLITE_PATH so producers don't depend
    # on the writability-probe fallback (silent noop trap).
    env_lines_to_set: List[Tuple[str, str]] = field(default_factory=list)
    # Producer units to restart AFTER the env rewrite, so they reconnect
    # to the default SQLite sink before clickhouse-server is torn down.
    consumers_to_restart: List[str] = field(default_factory=list)
    # Group name to create (e.g. "sigmond") if missing — needed so
    # producer users share write access to /var/lib/sigmond/.
    group_to_create: Optional[str] = None
    # [(user, group), ...] producer users missing from the sink group.
    users_to_add_to_group: List[Tuple[str, str]] = field(default_factory=list)
    # (path, group, mode) — sink dir to create or fix permissions on.
    # None when the dir already matches expected mode + group.
    sink_dir_to_setup: Optional[Tuple[str, str, int]] = None
    # (path, group, mode) — sink db file to pre-create so the first
    # producer to flush doesn't inherit ownership and lock other
    # producers in the same group out.  None when the file already
    # exists with the expected group + mode.
    sink_db_to_pre_create: Optional[Tuple[str, str, int]] = None
    # [(dropin_path, unit_name), ...] systemd drop-ins to write so each
    # sandboxed consumer can see /var/lib/sigmond as read-write.
    # Required because producer units run with ProtectSystem=strict,
    # which masks the sink dir to read-only inside the unit's mount
    # namespace regardless of POSIX perms.
    sandbox_dropins_to_write: List[Tuple[str, str]] = field(default_factory=list)
    confirmed: bool = False

    @property
    def env_files_to_rewrite(self) -> List[str]:
        """Distinct env-file paths touched by either neutralize or set."""
        seen: List[str] = []
        for path, _line in self.env_lines_to_neutralize:
            if path not in seen:
                seen.append(path)
        for path, _line in self.env_lines_to_set:
            if path not in seen:
                seen.append(path)
        return seen

    def sqlite_path_for_env(self, env_path: str) -> Optional[str]:
        """Return the SIGMOND_SQLITE_PATH value queued for env_path, if any."""
        for path, line in self.env_lines_to_set:
            if path == env_path and line.startswith("SIGMOND_SQLITE_PATH="):
                return line.split("=", 1)[1]
        return None

    @property
    def is_empty(self) -> bool:
        return not (
            self.services_to_stop
            or self.services_to_disable
            or self.packages_to_purge
            or self.paths_to_remove
            or self.files_to_remove
            or self.env_lines_to_neutralize
            or self.env_lines_to_set
            or self.consumers_to_restart
            or self.group_to_create
            or self.users_to_add_to_group
            or self.sink_dir_to_setup
            or self.sink_db_to_pre_create
            or self.sandbox_dropins_to_write
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

    def read_text(self, path: str) -> Optional[str]:
        try:
            return Path(path).read_text()
        except (FileNotFoundError, PermissionError, IsADirectoryError):
            return None

    def find_units_using_env_file(self, env_path: str) -> List[str]:
        """Active service units whose EnvironmentFiles include env_path.

        Two systemctl calls per unit, so this is O(units) — fine for a
        sigmond host that has a handful of producer services, not for
        a general-purpose audit tool.
        """
        try:
            r = subprocess.run(
                ["systemctl", "list-units", "--no-pager", "--no-legend",
                 "--type=service", "--state=active"],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return []
        consumers: List[str] = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            unit = parts[0]
            if not unit.endswith(".service"):
                continue
            try:
                r2 = subprocess.run(
                    ["systemctl", "show", "-p", "EnvironmentFiles", unit],
                    capture_output=True, text=True, check=False,
                )
            except FileNotFoundError:
                continue
            if env_path in r2.stdout:
                consumers.append(unit)
        return consumers

    def unit_user(self, unit: str) -> Optional[str]:
        """`User=` from systemd unit metadata; `'root'` when empty."""
        try:
            r = subprocess.run(
                ["systemctl", "show", "-p", "User", unit],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return None
        for line in r.stdout.splitlines():
            if line.startswith("User="):
                user = line[len("User="):].strip()
                # systemd omits the value when User= isn't set, meaning
                # the unit runs as root.  Mirror that explicitly.
                return user or "root"
        return None

    def group_exists(self, group: str) -> bool:
        try:
            r = subprocess.run(
                ["getent", "group", group],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return False
        return r.returncode == 0

    def user_groups(self, user: str) -> List[str]:
        """All groups (primary + supplementary) the user belongs to."""
        try:
            r = subprocess.run(
                ["id", "-Gn", user],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return []
        if r.returncode != 0:
            return []
        return r.stdout.strip().split()

    def dir_meta(self, path: str) -> Optional[Tuple[int, str]]:
        """(mode_bits, group_name) for `path`, or None if it doesn't exist.

        Used by the plan to decide whether `install -d` is needed.
        Mode is masked with 0o7777 so setgid/sticky bits are visible —
        callers compare against SINK_DIR_MODE (0o2775) and a missing
        setgid bit triggers a remediation install -d.
        """
        try:
            st = Path(path).stat()
        except (FileNotFoundError, PermissionError):
            return None
        import grp
        try:
            group_name = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            group_name = str(st.st_gid)
        return (st.st_mode & 0o7777, group_name)

    def file_meta(self, path: str) -> Optional[Tuple[int, str]]:
        """(mode_bits, group_name) for a regular file at `path`.

        Returns None if the file doesn't exist OR exists but is not a
        regular file (the caller is expected to treat both cases as
        "pre-create needed").  Mode is masked with 0o7777 like
        dir_meta so setuid/setgid bits are visible to comparisons.
        """
        try:
            st = Path(path).stat()
        except (FileNotFoundError, PermissionError):
            return None
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode):
            return None
        import grp
        try:
            group_name = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            group_name = str(st.st_gid)
        return (st.st_mode & 0o7777, group_name)

    def unit_sandbox_blocks_sink(self, unit: str, sink_dir: str) -> bool:
        """True iff this unit's sandbox would make `sink_dir` read-only.

        Specifically: unit has ProtectSystem set to "strict" or "full"
        AND `sink_dir` doesn't appear in its ReadWritePaths list.
        Returns False on the cautious side when the systemctl query
        fails — we'd rather skip a drop-in than write a wrong one.
        """
        try:
            r = subprocess.run(
                ["systemctl", "show", "-p", "ProtectSystem",
                 "-p", "ReadWritePaths", unit],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return False
        if r.returncode != 0:
            return False
        protect = ""
        rwpaths = ""
        for line in r.stdout.splitlines():
            if line.startswith("ProtectSystem="):
                protect = line[len("ProtectSystem="):].strip()
            elif line.startswith("ReadWritePaths="):
                rwpaths = line[len("ReadWritePaths="):].strip()
        # ProtectSystem=true makes /usr + /boot read-only but leaves
        # /var writable — not a problem.  Only strict/full sandboxes
        # the sink dir.
        if protect not in ("strict", "full"):
            return False
        # ReadWritePaths is whitespace-separated; check substring after
        # splitting to avoid false positives like /var/lib/sigmond-foo.
        return sink_dir not in rwpaths.split()


def plan_clickhouse_removal(probe: Optional[HostProbe] = None) -> RemovalPlan:
    """Enumerate ClickHouse artifacts (and producer-side config) on this host."""
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

    # Producer-side env: comment out SIGMOND_CLICKHOUSE_* lines and
    # restart anything that depended on them.  Without this step the
    # producers keep hammering localhost:8123 with retry storms after
    # ClickHouse is torn down (which is exactly what bit us during the
    # first real run of this verb — see psk-recorder log fallout).
    env_text = p.read_text(DEFAULT_COORD_ENV)
    if env_text is not None:
        for line in env_text.splitlines():
            if _CH_ENV_RE.match(line):
                plan.env_lines_to_neutralize.append((DEFAULT_COORD_ENV, line))

    # Discover consumers regardless of whether there's anything to
    # neutralize — we still need them for group-membership and
    # sink-dir-perms checks below.  An empty consumer list means there
    # are no producers needing the sigmond group + writable sink path.
    consumers = []
    if env_text is not None:
        consumers = p.find_units_using_env_file(DEFAULT_COORD_ENV)
    # Restart decision is made at the very end (after group/perms
    # planning below) — see _maybe_queue_restarts at the bottom of
    # this function.

    # Pin SIGMOND_SQLITE_PATH explicitly in coordination.env if (a)
    # we're actually migrating FROM ClickHouse on this host (CH lines
    # to neutralize), and (b) no operator override is already in place.
    # Pinning avoids the silent-noop trap where Writer.from_env's
    # writability probe (run as the producer user) falls back to
    # no-op when /var/lib/sigmond isn't readable — silently dropping
    # every row.  Explicit pin → the SQLite writer tries to open the
    # path and FAILS LOUDLY if perms are wrong, which is exactly
    # what we want.
    if (
        env_text is not None
        and consumers
        and plan.env_lines_to_neutralize
        and not _SQLITE_PATH_ENV_RE.search(env_text)
    ):
        plan.env_lines_to_set.append(
            (DEFAULT_COORD_ENV, f"SIGMOND_SQLITE_PATH={SINK_DB_PATH}"),
        )

    # Producer users + group membership.  We check perms whenever
    # there are running consumers — that way the verb is idempotent
    # AND self-healing: a host that was partially migrated (CH
    # already gone but sigmond group / sink-dir perms never set up,
    # which is exactly the state we landed in after the first real
    # run of this verb) gets fixed on a re-invocation.  On a fresh
    # sigmond install the perms are already correct (set by the
    # installer), so this is a no-op.
    producer_users = set()
    for unit in consumers:
        user = p.unit_user(unit)
        if user and user != "root":
            producer_users.add(user)

    if producer_users:
        if not p.group_exists(SINK_GROUP):
            plan.group_to_create = SINK_GROUP
        for user in sorted(producer_users):
            groups = p.user_groups(user)
            if SINK_GROUP not in groups:
                plan.users_to_add_to_group.append((user, SINK_GROUP))

        meta = p.dir_meta(SINK_DIR)
        # We need install -d if the dir is missing OR its mode/group
        # doesn't match what producers expect.  `install -d` is
        # idempotent, so this just normalizes whatever was there.
        if meta is None or meta != (SINK_DIR_MODE, SINK_GROUP):
            plan.sink_dir_to_setup = (SINK_DIR, SINK_GROUP, SINK_DIR_MODE)

        # Pre-create sink.db itself with root:sigmond mode 0664 so the
        # first producer to flush doesn't own the file and lock other
        # producers in the group out.  Without this, on a fresh deploy
        # the first race-winner's UID owns sink.db and the default
        # umask (0022 → mode 0644) means group members can read but
        # not write — every other producer hits "attempt to write a
        # readonly database" until a human chmods the file.  Observed
        # on bee1 2026-05-12 during the production migration.
        db_meta = p.file_meta(SINK_DB_PATH)
        if db_meta is None or db_meta != (SINK_DB_MODE, SINK_GROUP):
            plan.sink_db_to_pre_create = (
                SINK_DB_PATH, SINK_GROUP, SINK_DB_MODE,
            )

        # systemd sandbox: any consumer with ProtectSystem=strict/full
        # needs /var/lib/sigmond in its ReadWritePaths.  Write a drop-in
        # per unit instead of patching the base unit, so the producer
        # client's own systemd file stays untouched.
        for unit in consumers:
            if p.unit_sandbox_blocks_sink(unit, SINK_DIR):
                dropin_dir = f"/etc/systemd/system/{unit}.d"
                dropin_path = f"{dropin_dir}/{SYSTEMD_DROPIN_BASENAME}"
                # Idempotent: only queue if the drop-in file doesn't
                # already exist with the expected content.  Operators
                # who hand-rolled their own drop-in are left alone.
                if not p.path_exists(dropin_path):
                    plan.sandbox_dropins_to_write.append((dropin_path, unit))

    # Restart consumers whenever ANY of:
    #   - env lines changed (CH neutralized or SQLite path pinned)
    #   - group membership changed (need re-exec for new supplementary
    #     group to take effect)
    #   - sink dir was just configured (defensive: consumers that
    #     started with the wrong perms have stale fd state)
    #   - a sandbox drop-in was added (systemd needs a daemon-reload
    #     + unit restart to remap the namespace)
    needs_restart = bool(
        plan.env_lines_to_neutralize
        or plan.env_lines_to_set
        or plan.users_to_add_to_group
        or plan.sink_dir_to_setup
        or plan.sink_db_to_pre_create
        or plan.sandbox_dropins_to_write
    )
    if needs_restart:
        plan.consumers_to_restart = list(consumers)

    return plan


@dataclass
class _ExecutionReport:
    """What execute_removal actually did, for logging and tests."""

    group_created: Optional[str] = None
    users_added_to_group: List[Tuple[str, str]] = field(default_factory=list)
    sink_dir_configured: Optional[str] = None
    sink_db_pre_created: Optional[str] = None
    sandbox_dropins_written: List[str] = field(default_factory=list)
    env_files_rewritten: List[str] = field(default_factory=list)
    env_lines_appended: List[Tuple[str, str]] = field(default_factory=list)
    consumers_restarted: List[str] = field(default_factory=list)
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

    def write_text(self, path: str, content: str) -> None:
        """Create parent dir if missing, then write `content` to `path`.

        Used for systemd drop-in files.  Not idempotent re: existing
        content — caller decides whether to call by checking
        `HostProbe.path_exists` first.
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def rewrite_file(self, path: str, transform: Callable[[str], str]) -> str:
        """Read, transform, write back.  Returns the backup path.

        Backup is written to `<path>.bak-pre-sqlite` so an operator can
        revert with a single `mv`.  We deliberately don't preserve mode
        bits — coordination.env is a root-owned 0644 file by convention,
        and the new file inherits that.
        """
        p = Path(path)
        original = p.read_text()
        backup = path + BACKUP_SUFFIX
        Path(backup).write_text(original)
        p.write_text(transform(original))
        return backup


class NotConfirmed(Exception):
    """Raised when `execute_removal` is called on an unconfirmed plan."""


def _neutralize_clickhouse_lines(text: str) -> str:
    """Prefix every live `SIGMOND_CLICKHOUSE_*=` line with the comment marker.

    Idempotent: lines already starting with `#` don't match `_CH_ENV_RE`.
    Trailing newline of the original file is preserved.
    """
    lines = text.splitlines()
    out = []
    for line in lines:
        if _CH_ENV_RE.match(line):
            out.append(f"{NEUTRALIZED_PREFIX}{line}")
        else:
            out.append(line)
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + suffix


def _build_env_transform(
    *, append_lines: Optional[List[str]] = None,
) -> Callable[[str], str]:
    """Build a rewrite function that neutralizes CH lines + appends new ones.

    `append_lines` are added only if a line with the same `KEY=` prefix
    isn't already present, so re-running the verb is a no-op.  Each
    auto-inserted block gets the `INSERTED_HEADER` comment above it
    so operators can identify what wrote it.
    """
    appends = append_lines or []

    def transform(text: str) -> str:
        lines = text.splitlines()
        existing_keys = set()
        for line in lines:
            m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", line)
            if m:
                existing_keys.add(m.group(1))

        out = []
        for line in lines:
            if _CH_ENV_RE.match(line):
                out.append(f"{NEUTRALIZED_PREFIX}{line}")
            else:
                out.append(line)

        new_lines = []
        for entry in appends:
            key = entry.split("=", 1)[0]
            if key in existing_keys:
                continue
            new_lines.append(entry)
            existing_keys.add(key)

        if new_lines:
            # Blank-line gap before the inserted block keeps the file
            # readable when there are several env-var sections.
            if out and out[-1].strip():
                out.append("")
            out.append(INSERTED_HEADER)
            out.extend(new_lines)

        suffix = "\n" if text.endswith("\n") else ""
        return "\n".join(out) + suffix

    return transform


def execute_removal(
    plan: RemovalPlan,
    runner: Optional[Runner] = None,
) -> _ExecutionReport:
    """Execute a confirmed removal plan.  Caller must set plan.confirmed.

    Ordering rationale:

    0. Rewrite env file first.  Consumers won't actually see the change
       until they restart, but doing it before the restarts means the
       single restart cycle is enough to reconfigure them — no second
       pass needed if something else (a sigmond auto-restart) trips it.

    1. Restart consumers BEFORE we tear down ClickHouse.  After restart
       they're using the default-SQLite sink, so subsequent decode
       cycles don't waste a batch hammering a dying CH instance.

    2-6: Tear down CH itself (stop, disable, purge, remove dirs).
    """
    if not plan.confirmed:
        raise NotConfirmed(
            "execute_removal refused: plan.confirmed=False.  Set "
            "plan.confirmed=True only after operator approval (smd "
            "storage migrate-to-sqlite requires --yes)."
        )

    r = runner or Runner()
    report = _ExecutionReport()

    # 0a. Create the sigmond group if missing.  System groups (--system)
    # get GIDs below 1000 to keep them out of the human-user range.
    if plan.group_to_create:
        res = r.run(["groupadd", "--system", plan.group_to_create])
        if res.returncode == 0:
            report.group_created = plan.group_to_create
        elif res.returncode == 9:
            # rc=9 from groupadd means "already exists" — idempotent.
            pass
        else:
            report.errors.append(
                f"groupadd {plan.group_to_create}: rc={res.returncode}"
            )

    # 0b. Add each producer user to the sink group.  usermod -aG is
    # idempotent for already-member users (rc=0, no change).
    for user, group in plan.users_to_add_to_group:
        res = r.run(["usermod", "-aG", group, user])
        if res.returncode == 0:
            report.users_added_to_group.append((user, group))
        else:
            report.errors.append(
                f"usermod -aG {group} {user}: rc={res.returncode}"
            )

    # 0c. Create/normalize /var/lib/sigmond perms via `install -d`.
    # `install -d` is the standard Linux idiom for "idempotent mkdir
    # with mode + owner + group" — one tool, no race between mkdir
    # and chmod/chgrp.  Mode is 0o2775 (setgid), so files producers
    # create inside inherit group=sigmond — protects against the
    # WAL/SHM ownership trap (those sidecar files are created by the
    # first producer to flush, not pre-created by us).
    if plan.sink_dir_to_setup:
        path, group, mode = plan.sink_dir_to_setup
        res = r.run([
            "install", "-d", "-m", f"{mode:o}", "-g", group, path,
        ])
        if res.returncode == 0:
            report.sink_dir_configured = path
        else:
            report.errors.append(
                f"install -d {path}: rc={res.returncode} "
                f"stderr={res.stderr.strip()[:200]}"
            )

    # 0c-bis. Pre-create sink.db with root:sigmond mode 0664.  Without
    # this, the first producer to flush owns the file and other
    # producers in the sigmond group can't write to it (default umask
    # 0022 → mode 0644).  `install -m -g /dev/null target` is the
    # one-shot atomic idiom: creates an empty regular file with the
    # declared mode + group + owner=root, idempotent against re-runs.
    # SqliteWriter's CREATE TABLE IF NOT EXISTS populates the schema
    # on first flush — opening an existing 0-byte file is a valid
    # SQLite startup state (the WAL/SHM files come into existence
    # only after the journal_mode=WAL pragma, and inherit setgid from
    # the parent dir per step 0c above).
    if plan.sink_db_to_pre_create:
        db_path, group, mode = plan.sink_db_to_pre_create
        res = r.run([
            "install", "-m", f"{mode:o}", "-g", group,
            "/dev/null", db_path,
        ])
        if res.returncode == 0:
            report.sink_db_pre_created = db_path
        else:
            report.errors.append(
                f"install {db_path}: rc={res.returncode} "
                f"stderr={res.stderr.strip()[:200]}"
            )

    # 0d. Rewrite env files: neutralize CH lines + append any pinned
    # SIGMOND_SQLITE_PATH that wasn't already set.  One rewrite per
    # distinct path, even if there are several queued mutations.
    for env_path in plan.env_files_to_rewrite:
        appends: List[str] = []
        for path, line in plan.env_lines_to_set:
            if path == env_path:
                appends.append(line)
        try:
            r.rewrite_file(env_path, _build_env_transform(append_lines=appends))
            report.env_files_rewritten.append(env_path)
            for line in appends:
                report.env_lines_appended.append((env_path, line))
        except Exception as e:
            report.errors.append(f"rewrite {env_path}: {e}")

    # 0e. systemd sandbox drop-ins: producers with ProtectSystem=strict
    # need /var/lib/sigmond explicitly in ReadWritePaths.  We write a
    # per-unit drop-in rather than patching the client's base unit so
    # client projects stay unmodified.  daemon-reload happens after
    # the writes so systemd picks the new fragments up before restart.
    if plan.sandbox_dropins_to_write:
        for dropin_path, unit in plan.sandbox_dropins_to_write:
            try:
                r.write_text(dropin_path, SYSTEMD_DROPIN_CONTENT)
                report.sandbox_dropins_written.append(dropin_path)
            except Exception as e:
                report.errors.append(
                    f"write {dropin_path}: {e}"
                )
        # One daemon-reload after the batch.  The subsequent restart
        # loop is what actually remaps each unit's mount namespace.
        r.run(["systemctl", "daemon-reload"])

    # 1. Restart producers so they pick up the new env + group + sandbox
    # config BEFORE we kill CH.  Re-exec is what makes them see the new
    # supplementary group from step 0b; in-process getgroups() reflects
    # creds at process start, so a SIGHUP wouldn't be enough.
    for unit in plan.consumers_to_restart:
        res = r.run(["systemctl", "restart", unit])
        if res.returncode == 0:
            report.consumers_restarted.append(unit)
        else:
            report.errors.append(f"restart {unit}: rc={res.returncode}")

    # 2. Stop CH services so package purge / data removal doesn't race.
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

    # 3. Purge Debian packages — `apt-get purge -y` is the only verb
    # that drops both binaries and conffiles.  Skip if dpkg isn't
    # present (non-Debian host).
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

    # 4. Remove data / config / log dirs.  Done after package purge so
    # the postrm scripts can't see (and re-create) directories we
    # intend to delete.
    for path in plan.paths_to_remove:
        r.rmtree(path)
        report.removed_paths.append(path)

    for path in plan.files_to_remove:
        r.unlink(path)
        report.removed_files.append(path)

    # 5. Tell systemd we removed unit files.  Best-effort; not fatal.
    if plan.services_to_disable:
        r.run(["systemctl", "daemon-reload"])

    return report
