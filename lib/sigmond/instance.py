"""Per-reporter client instance model.

Implements MULTI-INSTANCE-ARCHITECTURE.md §3 (reporter ID format),
§4 (canonical file layout), and the file-side actions for §6
(`smd admin instance add` / `smd admin instance remove`).

An *instance* is one deployment context of a recorder client, keyed by
operator-meaningful reporter ID (e.g. `AC0G-B1`).  Each instance owns:

  /etc/<client>/<reporter_id>.toml                    (per-instance config)
  /etc/<client>/env/<reporter_id>.env                 (per-instance env)
  /etc/sigmond/clients/<client>@<reporter_id>.sources.toml
                                                      (per-instance sources)
  /var/lib/<client>/<reporter_id>/                    (state — systemd-managed)
  /var/log/<client>/<reporter_id>/                    (logs — systemd-managed)
  /run/<client>/<reporter_id>/                        (runtime — systemd-managed)

Sigmond writes config / env / sources stubs on `add`; the state/log/run
dirs are created automatically by systemd via StateDirectory= /
LogsDirectory= / RuntimeDirectory= when the unit first starts.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .paths import SIGMOND_CONF


# ---------------------------------------------------------------------------
# Reporter ID — path-safe by construction (MULTI-INSTANCE-ARCHITECTURE.md §3)
# ---------------------------------------------------------------------------
#
# A reporter ID is the operator-meaningful identifier for one recording
# station (e.g. WSPRnet would credit spots to `AI6VN/P` or `AC0G/B1`).
# Sigmond stores the ID as path-safe ASCII so it can serve double duty
# as a filename stem, systemd `%i`, and shell argv: the WSPRnet slash
# is stored as `=` (a sentinel character no operator naturally enters
# in a callsign-shaped name).  Hyphens are passed through verbatim
# because real operators DO use hyphens — e.g. `W1ABC-5` (a member
# identifier where the `-5` is intentional, not a slash) or
# `KP4MD-RPI-4` (a hyphen-suffixed station name).  Translating those
# back to `/5` or `/RPI-4` at display time would be incorrect; the
# `=`-only convention avoids that ambiguity.
#
#   user types  →  stored as   →  displayed as
#   AC0G/B1        AC0G=B1        AC0G/B1
#   AC0G-B1        AC0G-B1        AC0G-B1
#   W1ABC-5        W1ABC-5        W1ABC-5
#   KP4MD-RPI-4    KP4MD-RPI-4    KP4MD-RPI-4
#   AI6VN/P        AI6VN=P        AI6VN/P
#
# `=` is also the convention `hs-uploader/transports/wsprdaemon.py`
# already uses for the per-rx tar path component
# (`call.replace("/", "=")`), so the storage form lines up with what
# wsprdaemon.org's gateway already expects.

REPORTER_ID_REGEX = re.compile(r"^[A-Z0-9][A-Z0-9=-]*[A-Z0-9]$")


class InvalidReporterId(ValueError):
    """Raised when a reporter ID doesn't satisfy REPORTER_ID_REGEX."""


def validate_reporter_id(reporter_id: str) -> None:
    """Raise InvalidReporterId if `reporter_id` is not path-safe.

    Path-safe = uppercase alphanumerics, ASCII hyphens, and the `=`
    sentinel (which represents a `/` in the user's original entry).
    No leading or trailing separator; min length 2.  See module
    docstring above for the storage/display invariant.
    """
    if not isinstance(reporter_id, str) or not reporter_id:
        raise InvalidReporterId(
            "reporter ID must be a non-empty string"
        )
    if not REPORTER_ID_REGEX.match(reporter_id):
        raise InvalidReporterId(
            f"reporter ID {reporter_id!r} is not path-safe; "
            f"must match {REPORTER_ID_REGEX.pattern} "
            f"(uppercase alphanumerics, hyphens, `=`; no leading/"
            f"trailing separator; min length 2).  Hint: enter the "
            f"slash form (`AC0G/B1`) — sigmond stores it internally "
            f"as `AC0G=B1` and re-displays the slash."
        )


def parse_user_reporter_id(user_input: str) -> str:
    """Convert a user's reporter-ID entry into the path-safe storage form.

    Users may type the WSPRnet/callsign form with a forward slash
    (e.g. `AC0G/B1`, `AI6VN/P`) or the direct storage form with
    hyphens-only (e.g. `W1ABC-5`).  We uppercase, strip whitespace,
    and substitute `/` with the `=` sentinel.  Hyphens — which are
    legitimate in callsigns like `W1ABC-5` — are left alone.

    Raises InvalidReporterId if the result isn't path-safe.  Inverse
    of `display_reporter_id`.
    """
    if not isinstance(user_input, str):
        raise InvalidReporterId("reporter ID must be a string")
    storage = user_input.strip().upper().replace("/", "=")
    validate_reporter_id(storage)
    return storage


def display_reporter_id(reporter_id: str) -> str:
    """Render a stored reporter ID for user-facing display.

    The `=` sentinels in the stored form mark where the user
    originally typed `/`; revert them.  Hyphens are user-intentional
    and stay.  Inverse of `parse_user_reporter_id`.
    """
    return reporter_id.replace("=", "/")


# Back-compat alias.  `to_wsprnet_form` previously did a mechanical
# first-hyphen → slash conversion (which mis-translated hyphenated
# callsigns like `W1ABC-5` → `W1ABC/5`).  The new convention stores
# slashes as `=` and reverts them at display time, so this is now
# identical to display_reporter_id.  Callers may use either name.
to_wsprnet_form = display_reporter_id


# ---------------------------------------------------------------------------
# Canonical file layout (§4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstancePaths:
    """Resolved canonical paths for one instance."""
    client: str
    reporter_id: str

    config: Path        # /etc/<client>/<reporter_id>.toml
    env: Path           # /etc/<client>/env/<reporter_id>.env
    sources: Path       # /etc/sigmond/clients/<client>@<reporter_id>.sources.toml
    state_dir: Path     # /var/lib/<client>/<reporter_id>/  (systemd-managed)
    log_dir: Path       # /var/log/<client>/<reporter_id>/  (systemd-managed)
    run_dir: Path       # /run/<client>/<reporter_id>/      (systemd-managed)
    unit_name: str      # <client>@<reporter_id>.service
    unit_template: str  # <client>@.service (in client repo's systemd/ dir)


def instance_paths(client: str, reporter_id: str) -> InstancePaths:
    """Resolve all canonical paths for (client, reporter_id).

    Does NOT touch the filesystem; just returns the path objects.
    Reporter ID is validated; raises InvalidReporterId on a bad name.
    """
    validate_reporter_id(reporter_id)
    if not client or "/" in client or client.startswith("."):
        raise ValueError(f"bad client name: {client!r}")
    etc_client = Path("/etc") / client
    return InstancePaths(
        client=client,
        reporter_id=reporter_id,
        config=etc_client / f"{reporter_id}.toml",
        env=etc_client / "env" / f"{reporter_id}.env",
        sources=SIGMOND_CONF / "clients" / f"{client}@{reporter_id}.sources.toml",
        state_dir=Path("/var/lib") / client / reporter_id,
        log_dir=Path("/var/log") / client / reporter_id,
        run_dir=Path("/run") / client / reporter_id,
        unit_name=f"{client}@{reporter_id}.service",
        unit_template=f"{client}@.service",
    )


# ---------------------------------------------------------------------------
# Instance enumeration (`smd admin instance list`)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Instance:
    """A discovered instance: paths plus an existence summary."""
    paths: InstancePaths
    has_config: bool
    has_env: bool
    has_sources: bool

    @property
    def client(self) -> str:
        return self.paths.client

    @property
    def reporter_id(self) -> str:
        return self.paths.reporter_id


def _safe_exists(path: Path) -> bool:
    """Path.exists() that swallows PermissionError.

    `/etc/<client>/` for service-user-owned components (hf-gps-tec,
    mag-recorder, hf-timestd, …) is mode 0750; an unprivileged
    operator running `smd tui` can't stat its children.  Python 3.12+
    (and Debian's 3.11.2 backport) re-raises PermissionError from
    Path.exists() instead of returning False, which would crash
    list_instances() rather than degrade.  Treat "can't tell" as
    "yes" — the caller is enumerating files we found a different way
    (e.g. by systemctl unit name), so the path is presumed present.
    """
    try:
        return path.exists()
    except PermissionError:
        return True


def _list_units_for_client(client: str) -> list[str]:
    """systemctl-based fallback when /etc/<client>/ is unreadable.

    Returns the reporter_id portion of every `<client>@<reporter>.service`
    currently loaded or persistently enabled.  Works without any
    /etc read access.
    """
    import subprocess
    rids: set[str] = set()
    pattern = f"{client}@*.service"
    for sub_args in (
        ["list-units", pattern, "--no-legend", "--no-pager", "--plain"],
        ["list-unit-files", pattern, "--no-legend", "--no-pager"],
    ):
        try:
            r = subprocess.run(
                ["systemctl", *sub_args],
                capture_output=True, text=True, check=False, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        for line in (r.stdout or "").splitlines():
            unit = line.split()[0] if line.split() else ""
            if not unit.endswith(".service"):
                continue
            base = unit[: -len(".service")]
            if "@" not in base:
                continue
            cli, _, rid = base.partition("@")
            if cli != client or not rid:
                continue
            try:
                validate_reporter_id(rid)
            except InvalidReporterId:
                continue
            rids.add(rid)
    return sorted(rids)


def list_instances(catalog_clients: Optional[list[str]] = None) -> list[Instance]:
    """Walk /etc/<client>/<reporter_id>.toml across known clients.

    `catalog_clients`: list of client names to consider; if None,
    walks every /etc/<X>/ directory and reports each *.toml file
    whose stem is a valid reporter ID.  Defaults to None.

    Returns instances sorted by (client, reporter_id).  Files whose
    stems aren't valid reporter IDs (e.g. the legacy
    `wspr-recorder-config.toml` shape) are silently skipped — those
    are pre-multi-instance deployments that haven't been migrated
    yet, handled by `smd admin instance migrate`.

    Operator-callable: when /etc/<client>/ is service-user-owned
    (mode 0750 — hf-gps-tec, mag-recorder, hf-timestd, …) the glob
    silently returns nothing for an unprivileged caller.  In that
    case we fall back to a systemctl unit enumeration so the operator
    still sees their instances in `smd tui` Configuration without
    needing root.
    """
    results: list[Instance] = []
    etc = Path("/etc")

    if catalog_clients is None:
        try:
            client_dirs = [
                p for p in etc.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ] if etc.exists() else []
        except (PermissionError, OSError):
            client_dirs = []
    else:
        client_dirs = [etc / c for c in catalog_clients]

    for client_dir in client_dirs:
        try:
            is_dir = client_dir.is_dir()
        except (PermissionError, OSError):
            is_dir = True   # presumed present; we'll fall back to systemctl below
        if not is_dir:
            continue
        client = client_dir.name

        # Primary path: glob /etc/<client>/*.toml.
        rids_seen: set[str] = set()
        try:
            cfg_files = sorted(client_dir.glob("*.toml"))
        except (PermissionError, OSError):
            cfg_files = []
        for cfg in cfg_files:
            stem = cfg.stem
            try:
                validate_reporter_id(stem)
            except InvalidReporterId:
                continue
            rids_seen.add(stem)
            paths = instance_paths(client, stem)
            results.append(Instance(
                paths=paths,
                has_config=_safe_exists(paths.config),
                has_env=_safe_exists(paths.env),
                has_sources=_safe_exists(paths.sources),
            ))

        # Fallback path: directory unreadable (or no .toml found yet but
        # the operator may have units enabled via `smd admin instance add`'s
        # systemd step).  systemctl works without /etc read access.
        if not cfg_files:
            for rid in _list_units_for_client(client):
                if rid in rids_seen:
                    continue
                paths = instance_paths(client, rid)
                results.append(Instance(
                    paths=paths,
                    has_config=_safe_exists(paths.config),
                    has_env=_safe_exists(paths.env),
                    has_sources=_safe_exists(paths.sources),
                ))

    results.sort(key=lambda i: (i.client, i.reporter_id))
    return results


def get_instance(client: str, reporter_id: str) -> Optional[Instance]:
    """Return Instance for (client, reporter_id) if any file exists.

    Returns None if no per-instance file (config / env / sources)
    is present — i.e., the instance has not been created.
    """
    paths = instance_paths(client, reporter_id)
    has_config = paths.config.exists()
    has_env = paths.env.exists()
    has_sources = paths.sources.exists()
    if not (has_config or has_env or has_sources):
        return None
    return Instance(
        paths=paths,
        has_config=has_config,
        has_env=has_env,
        has_sources=has_sources,
    )


# ---------------------------------------------------------------------------
# File scaffolding (`smd admin instance add` / `remove`)
# ---------------------------------------------------------------------------

# Header lines written into each stub file so an operator opening one
# in an editor knows what created it and what it's for.
def _stub_header(client: str, reporter_id: str, kind: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"# {kind} for {client}@{reporter_id}\n"
        f"# Created by `smd admin instance add` on {ts}.\n"
        f"# See docs/MULTI-INSTANCE-ARCHITECTURE.md for the canonical "
        f"layout.\n"
    )


def _config_stub(client: str, reporter_id: str) -> str:
    return (
        _stub_header(client, reporter_id, "Per-instance config")
        + "\n"
        f"[instance]\n"
        f'reporter_id = "{reporter_id}"\n'
        "\n"
        "# Source-keys this instance consumes from.  Use\n"
        '#   smd admin sources add ' + client + '@' + reporter_id + ' <kind>:<id>\n'
        "# to populate.  See `smd admin sources list` for what's discoverable.\n"
        "sources = []\n"
        "\n"
        "[instance.metadata]\n"
        '# antenna  = "loop"            # operator description\n'
        '# sdr      = "rx888-mk2"       # SDR model / serial / friendly name\n'
        "\n"
        "# Client-specific sections follow.  Run `smd admin instance edit\n"
        f"# {client} {reporter_id}` to invoke the client's config flow.\n"
    )


def _config_from_shared(client: str, reporter_id: str, shared_body: str) -> str:
    """Seed a per-instance config from the client's shared config.toml.

    The per-instance config is the complete source of truth (no inheritance,
    MULTI-INSTANCE-ARCHITECTURE.md §5), so we keep the full shared body
    (bands, radiod status, channel defaults, …) and prepend the [instance]
    block — the same shape the migration path produces.  Guards against a
    shared config that already carries its own [instance] table.
    """
    if "[instance]" in shared_body:
        return shared_body
    instance_block = (
        "[instance]\n"
        f'reporter_id = "{reporter_id}"\n'
        "sources = []\n"
        "\n"
    )
    return (
        _stub_header(client, reporter_id, "Per-instance config")
        + f"# Seeded from /etc/{client}/config.toml (the shared config).\n\n"
        + instance_block
        + shared_body
    )


def _instance_env_defaults(client: str) -> "dict[str, str]":
    """Greenfield per-instance env defaults the client declares in its
    deploy.toml ``[contract.instance_env]`` table.

    sigmond seeds these KEY=VALUE pairs into a new instance's env file so a
    sigmond-managed (greenfield) host gets the client's intended runtime
    mode without hand-editing.  Example: wspr-recorder declares
    ``WD_DECODE_VIA_DB = "1"`` because a sigmond host has no legacy
    ``wd-decode@*`` chain, so decode must run in-process or no spots are
    ever produced.  Returns {} when the client declares none / has no
    deploy.toml.
    """
    deploy = Path("/opt/git/sigmond") / client / "deploy.toml"
    try:
        with open(deploy, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    raw = (data.get("contract") or {}).get("instance_env") or {}
    out: "dict[str, str]" = {}
    for k, v in raw.items():
        # TOML may type `1` / `true` as int/bool; env files are strings.
        out[k] = ("1" if v else "0") if isinstance(v, bool) else str(v)
    return out


def _env_stub(client: str, reporter_id: str) -> str:
    body = (
        _stub_header(client, reporter_id, "Per-instance env")
        + "\n"
        f"# Loaded by {client}@{reporter_id}.service via\n"
        f"#   EnvironmentFile=-/etc/{client}/env/{reporter_id}.env\n"
    )
    defaults = _instance_env_defaults(client)
    if defaults:
        body += (
            "# Seeded from the client's deploy.toml [contract.instance_env]\n"
            "# (greenfield defaults \u2014 safe to edit).\n"
        )
        for k, v in defaults.items():
            body += f"{k}={v}\n"
    else:
        body += "# Empty by default; add KEY=VALUE lines as the client requires.\n"
    return body


def _sources_stub(client: str, reporter_id: str) -> str:
    return (
        _stub_header(client, reporter_id, "Per-instance sources selection")
        + "\n"
        "# Rendered by `smd admin sources apply` from the instance config's\n"
        "# `sources = [...]` list.  Don't hand-edit; use\n"
        f"#   smd admin sources add {client}@{reporter_id} <kind>:<id>\n"
        f"#   smd admin sources remove {client}@{reporter_id} <kind>:<id>\n"
        "selections = []\n"
    )


class InstanceExists(RuntimeError):
    """Raised by create_instance when any per-instance file already exists."""


def create_instance(
    client: str,
    reporter_id: str,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> InstancePaths:
    """Initialize the per-instance config/env/sources files.

    Does NOT enable or start the systemd unit (per spec §6).  Does
    NOT create state/log/run dirs — systemd handles those via
    StateDirectory= / LogsDirectory= / RuntimeDirectory= when the
    unit first starts.

    Raises InstanceExists if any of the three files exist and
    `force=False`.  With `force=True`, existing files are left in
    place; only missing files are created.

    With `dry_run=True`, returns the paths that WOULD be created
    without touching the filesystem.
    """
    paths = instance_paths(client, reporter_id)

    existing = [
        p for p in (paths.config, paths.env, paths.sources)
        if p.exists()
    ]
    if existing and not force:
        existing_list = ", ".join(str(p) for p in existing)
        raise InstanceExists(
            f"instance {client}@{reporter_id} already has files: "
            f"{existing_list}.  Use --force to keep them and create "
            f"only missing files, or `smd admin instance remove` first."
        )

    if dry_run:
        return paths

    # Per-client config dir must exist (created by `smd install`)
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    # env subdir
    paths.env.parent.mkdir(parents=True, exist_ok=True)
    # sigmond's clients dir
    paths.sources.parent.mkdir(parents=True, exist_ok=True)

    if not paths.config.exists():
        # Prefer seeding from the client's shared config so the per-instance
        # file is complete (bands, radiod binding, …) — a bare stub fails the
        # client's "no frequencies configured" check.  Falls back to the stub
        # when no shared config exists yet.
        shared = Path("/etc") / client / "config.toml"
        if shared.exists():
            paths.config.write_text(
                _config_from_shared(client, reporter_id, shared.read_text()))
        else:
            paths.config.write_text(_config_stub(client, reporter_id))
    if not paths.env.exists():
        paths.env.write_text(_env_stub(client, reporter_id))
    if not paths.sources.exists():
        paths.sources.write_text(_sources_stub(client, reporter_id))

    return paths


def remove_instance(
    client: str,
    reporter_id: str,
    *,
    purge: bool = False,
    dry_run: bool = False,
) -> list[Path]:
    """Remove per-instance files (config/env/sources) and optionally
    the state/log/run dirs (`--purge`).

    Does NOT stop or disable the systemd unit; the caller is
    responsible for that ordering.

    Returns the list of paths that WERE removed (or that WOULD be
    removed with `dry_run=True`).  Best-effort: missing files are
    silently skipped.
    """
    paths = instance_paths(client, reporter_id)

    file_targets = [paths.config, paths.env, paths.sources]
    dir_targets: list[Path] = []
    if purge:
        dir_targets = [paths.state_dir, paths.log_dir, paths.run_dir]

    removed: list[Path] = []
    for f in file_targets:
        if f.exists() and not f.is_dir():
            removed.append(f)
            if not dry_run:
                try:
                    f.unlink()
                except OSError:
                    pass
    for d in dir_targets:
        if d.exists() and d.is_dir():
            removed.append(d)
            if not dry_run:
                try:
                    shutil.rmtree(d)
                except OSError:
                    pass

    return removed


# ---------------------------------------------------------------------------
# Migration (`smd admin instance migrate`)
# (MULTI-INSTANCE-ARCHITECTURE.md §6 + §10 Phase 8)
# ---------------------------------------------------------------------------

# Clients that are templated (`<client>@.service`) and need migration.
# mag-recorder is intentionally excluded (singleton service, not a template);
# its template conversion happens out-of-band.
_TEMPLATED_RECORDER_CLIENTS = (
    "psk-recorder",
    "wspr-recorder",
    "hfdl-recorder",
    "codar-sounder",
)


@dataclass(frozen=True)
class MigrationCandidate:
    """A legacy <client>@<old>.service that needs migration.

    `old_instance` is the systemd `%i` (typically a radiod id like
    `my-rx888`).  `signals` lists which evidence sources flagged this
    candidate ("env_file", "systemd_unit", or both).
    """
    client: str
    old_instance: str
    has_env_file: bool
    has_systemd_unit: bool
    unit_active: bool

    @property
    def unit_name(self) -> str:
        return f"{self.client}@{self.old_instance}.service"


def detect_migration_candidates() -> list[MigrationCandidate]:
    """Walk env files + systemctl-listed units; surface candidates.

    Combines two signals:
      1. /etc/<client>/env/<name>.env where <name> isn't a valid
         reporter_id and <client> is a templated recorder.
      2. systemctl-loaded `<client>@<name>.service` units where
         <name> isn't a valid reporter_id.

    Returns a deduped + sorted list.
    """
    import subprocess

    found: dict[tuple[str, str], MigrationCandidate] = {}

    # Source 1 — per-instance env files
    for client in _TEMPLATED_RECORDER_CLIENTS:
        env_dir = Path("/etc") / client / "env"
        if not env_dir.is_dir():
            continue
        for env_file in sorted(env_dir.glob("*.env")):
            name = env_file.stem
            try:
                validate_reporter_id(name)
                continue                # already reporter-keyed; skip
            except InvalidReporterId:
                pass
            key = (client, name)
            found[key] = MigrationCandidate(
                client=client, old_instance=name,
                has_env_file=True,
                has_systemd_unit=False,
                unit_active=False,
            )

    # Source 2 — systemctl loaded units (templated `<client>@<name>.service`)
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--all", "--no-legend",
             "--no-pager", "--plain", "--type=service"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        result = None

    if result is not None and result.returncode == 0:
        for line in (result.stdout or "").splitlines():
            cols = line.split(None, 4)
            if len(cols) < 4:
                continue
            unit_name, load, active, _sub = cols[0], cols[1], cols[2], cols[3]
            if not unit_name.endswith(".service"):
                continue
            base = unit_name[:-len(".service")]
            if "@" not in base:
                continue
            client, _, name = base.partition("@")
            if client not in _TEMPLATED_RECORDER_CLIENTS:
                continue
            if not name:
                continue
            try:
                validate_reporter_id(name)
                continue                # already reporter-keyed
            except InvalidReporterId:
                pass
            key = (client, name)
            existing = found.get(key)
            unit_active = (active == "active")
            if existing:
                found[key] = MigrationCandidate(
                    client=client, old_instance=name,
                    has_env_file=existing.has_env_file,
                    has_systemd_unit=True,
                    unit_active=unit_active,
                )
            else:
                found[key] = MigrationCandidate(
                    client=client, old_instance=name,
                    has_env_file=False,
                    has_systemd_unit=True,
                    unit_active=unit_active,
                )

    return sorted(found.values(),
                  key=lambda c: (c.client, c.old_instance))


# ---------------------------------------------------------------------------
# Per-candidate migration
# ---------------------------------------------------------------------------

# Legacy shared config path per client (matches the resolve_config_path
# DEFAULT_CONFIG_PATH in each client repo's config.py).
_LEGACY_SHARED_CONFIG = {
    "psk-recorder":   Path("/etc/psk-recorder/psk-recorder-config.toml"),
    "wspr-recorder":  Path("/etc/wspr-recorder/config.toml"),
    "hfdl-recorder":  Path("/etc/hfdl-recorder/hfdl-recorder-config.toml"),
    "codar-sounder":  Path("/etc/codar-sounder/codar-sounder-config.toml"),
}


def _migration_config_header(client: str, old: str, reporter_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    legacy = _LEGACY_SHARED_CONFIG.get(client, Path("(unknown legacy path)"))
    return (
        f"# Per-instance config for {client}@{reporter_id}\n"
        f"# Created by `smd admin instance migrate` on {ts}\n"
        f"# (migrated from legacy unit {client}@{old}.service).\n"
        f"#\n"
        f"# This file is a copy of {legacy} with an [instance]\n"
        f"# block prepended.  Per-instance config currently contains the\n"
        f"# FULL shared content; operators may trim unrelated\n"
        f"# [[radiod]] / [[band]] / [[source]] blocks that don't apply\n"
        f"# to this reporter.  See docs/MULTI-INSTANCE-ARCHITECTURE.md\n"
        f"# for the canonical layout.\n"
        f"\n"
        f"[instance]\n"
        f'reporter_id = "{reporter_id}"\n'
        f"\n"
        f"[instance.metadata]\n"
        f'# antenna  = "loop"            # operator description\n'
        f'# sdr      = "rx888-mk2"       # SDR model / serial / friendly name\n'
        f"\n"
        f"# --- Original shared-config content follows ---\n"
        f"\n"
    )


def migrate_one_instance(
    client: str,
    old_instance: str,
    reporter_id: str,
    *,
    dry_run: bool = True,
) -> list[str]:
    """Perform the per-candidate migration steps in order.

    Returns a list of step descriptions for the operator's log.
    With `dry_run=True`, no filesystem or systemd changes happen —
    just reports what WOULD be done.

    Steps (per spec §6):
      1. stop + disable old <client>@<old>.service
      2. create per-instance config (copy shared + prepend [instance])
      3. mv env file
      4. mv data dir (/var/lib/<client>/<old> → /var/lib/<client>/<reporter>)
      5. mv log dir (/var/log/<client>/<old> → /var/log/<client>/<reporter>)
      6. mv systemd drop-in dir
      7. daemon-reload
      8. enable + start new unit

    Raises InvalidReporterId on bad reporter_id; all other failures
    are accumulated into the returned step list (best-effort).
    """
    import subprocess

    validate_reporter_id(reporter_id)
    new_paths = instance_paths(client, reporter_id)
    old_unit = f"{client}@{old_instance}.service"
    new_unit = new_paths.unit_name

    old_env = Path("/etc") / client / "env" / f"{old_instance}.env"
    old_state = Path("/var/lib") / client / old_instance
    old_log = Path("/var/log") / client / old_instance
    old_dropin = Path("/etc/systemd/system") / f"{old_unit}.d"
    new_dropin = Path("/etc/systemd/system") / f"{new_unit}.d"

    steps: list[str] = []

    def _do(desc: str, func) -> None:
        steps.append(("would " if dry_run else "") + desc)
        if dry_run:
            return
        try:
            func()
        except Exception as exc:
            steps.append(f"  ERROR: {exc}")

    def _run(*cmd: str) -> None:
        result = subprocess.run(
            list(cmd), capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{' '.join(cmd)} exit {result.returncode}: "
                f"{(result.stderr or '').strip()}"
            )

    # 1. Stop + disable old unit
    _do(f"systemctl stop {old_unit}",
        lambda: _run("systemctl", "stop", old_unit))
    _do(f"systemctl disable {old_unit}",
        lambda: _run("systemctl", "disable", old_unit))

    # 2. Create per-instance config from shared (if not already present)
    if new_paths.config.exists():
        steps.append(f"per-instance config {new_paths.config} already exists; skip")
    else:
        legacy_shared = _LEGACY_SHARED_CONFIG.get(client)
        if legacy_shared is None or not legacy_shared.exists():
            steps.append(
                f"WARN: no legacy shared config for {client} at "
                f"{legacy_shared} — per-instance config not created. "
                f"Operator must create {new_paths.config} manually."
            )
        else:
            def _write_config():
                new_paths.config.parent.mkdir(parents=True, exist_ok=True)
                header = _migration_config_header(client, old_instance, reporter_id)
                body = legacy_shared.read_text()
                new_paths.config.write_text(header + body)
            _do(f"create {new_paths.config} from {legacy_shared} + [instance] block",
                _write_config)

    # 3. mv env file
    if old_env.exists():
        def _mv_env():
            new_paths.env.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_env), str(new_paths.env))
        _do(f"mv {old_env} → {new_paths.env}", _mv_env)

    # 4. mv data dir
    if old_state.exists():
        _do(f"mv {old_state} → {new_paths.state_dir}",
            lambda: shutil.move(str(old_state), str(new_paths.state_dir)))

    # 5. mv log dir
    if old_log.exists():
        _do(f"mv {old_log} → {new_paths.log_dir}",
            lambda: shutil.move(str(old_log), str(new_paths.log_dir)))

    # 6. mv systemd drop-in dir
    if old_dropin.exists():
        _do(f"mv {old_dropin} → {new_dropin}",
            lambda: shutil.move(str(old_dropin), str(new_dropin)))

    # 7. systemctl daemon-reload
    _do("systemctl daemon-reload",
        lambda: _run("systemctl", "daemon-reload"))

    # 8. Enable + start new unit
    _do(f"systemctl enable --now {new_unit}",
        lambda: _run("systemctl", "enable", "--now", new_unit))

    return steps
