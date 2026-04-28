"""`smd config init|edit <client>` — invoke a client's configuration
interview as defined in CONTRACT-v0.5 §14.

Sigmond never writes inside a client's config files.  These verbs just
shell out to the entry points the client advertises in its
`deploy.toml [contract.config]` block, with a stable env var bag built
from coordination.toml + environment.toml + each installed client's
inventory.  When a client doesn't advertise an entry point, sigmond
falls back to opening `$EDITOR` on the client's `config_path`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Optional

from ..coordination import Coordination, load_coordination
from ..environment import load_environment
from ..lifecycle import _find_deploy_toml
from ..ui import err, heading, info, ok, warn


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def cmd_config_init(args) -> int:
    return _dispatch(args, verb="init")


def cmd_config_edit_client(args) -> int:
    return _dispatch(args, verb="edit")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _dispatch(args, *, verb: str) -> int:
    client = getattr(args, "client", None)
    instance = getattr(args, "instance", None) or None
    if not client:
        err(f"usage: smd config {verb} <client> [<instance>]")
        return 2

    # radiod is sigmond-owned (it's the upstream that all clients consume
    # from, not a HamSCI contract client).  Special-case to a wizard
    # instead of going through deploy.toml [contract.config].
    if client == "radiod":
        from . import radiod_config
        # Synthesize the args shape radiod_config expects.
        if instance and not getattr(args, "instance", None):
            args.instance = instance
        if verb == "init":
            return radiod_config.cmd_radiod_init(args)
        return radiod_config.cmd_radiod_edit(args)

    deploy = _find_deploy_toml(client)
    if deploy is None:
        err(f"{client}: deploy.toml not found "
            f"(client may not be installed; try `smd install {client}`)")
        return 1

    cfg_block = _read_contract_config(deploy)
    entry = cfg_block.get(verb)
    if entry:
        return _run_client_entrypoint(client, deploy, entry, verb,
                                       instance=instance)

    # Fallback paths (CONTRACT-v0.5 §14.4)
    return _fallback(client, deploy, verb, instance=instance)


# ---------------------------------------------------------------------------
# deploy.toml [contract.config] reader
# ---------------------------------------------------------------------------

def _read_contract_config(deploy_path: Path) -> dict:
    try:
        with open(deploy_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        warn(f"failed to read {deploy_path}: {e}")
        return {}
    return ((data.get("contract") or {}).get("config") or {})


# ---------------------------------------------------------------------------
# Run the client's advertised entry point
# ---------------------------------------------------------------------------

def _run_client_entrypoint(client: str, deploy_path: Path,
                            entry, verb: str,
                            *, instance: Optional[str] = None) -> int:
    """`entry` is either a string (single executable, optionally relative
    to the repo root) or a list (argv form: [exe, arg1, arg2, ...])."""
    repo_root = deploy_path.parent

    if isinstance(entry, list):
        if not entry:
            err(f"{client}: [contract.config].{verb} is an empty list")
            return 1
        exe = Path(str(entry[0]))
        extra_args = [str(a) for a in entry[1:]]
    elif isinstance(entry, str):
        exe = Path(entry)
        extra_args = []
    else:
        err(f"{client}: [contract.config].{verb} must be a string or a list, "
            f"got {type(entry).__name__}")
        return 1

    if not exe.is_absolute():
        exe = repo_root / exe

    if not exe.exists():
        err(f"{client}: [contract.config].{verb} resolves to {exe}, "
            f"but that file does not exist")
        return 1
    if not os.access(exe, os.X_OK):
        err(f"{client}: {exe} is not executable")
        return 1

    env = _build_env_bag(client=client, instance=instance)
    label = f"{client}" + (f"@{instance}" if instance else "")
    heading(f"config {verb} {label}")
    argv = [str(exe), *extra_args]
    info(f"invoking: {' '.join(argv)}")
    info(f"vars: {', '.join(sorted(env_keys_set(env))) or '(none)'}")
    print()

    full_env = os.environ.copy()
    full_env.update(env)
    try:
        proc = subprocess.run(argv, env=full_env, check=False)
    except OSError as e:
        err(f"failed to invoke {exe}: {e}")
        return 1
    return proc.returncode


# ---------------------------------------------------------------------------
# Fallbacks when the client has no [contract.config]
# ---------------------------------------------------------------------------

def _fallback(client: str, deploy_path: Path, verb: str,
              *, instance: Optional[str] = None) -> int:
    if verb == "init":
        rendered = _find_render_template(deploy_path)
        heading(f"config init {client}")
        warn(f"{client}: no [contract.config].init declared in {deploy_path}")
        if rendered:
            info(f"config template: {rendered}")
            info("the operator may copy/edit this template manually, or the "
                 "client may render it on first run")
        else:
            info("no template advertised; consult the client's documentation")
        return 0

    # verb == "edit"
    config_path = _config_path_from_inventory(client) or _config_path_from_deploy(deploy_path)
    if not config_path:
        err(f"{client}: cannot determine config path "
            f"(no [contract.config].edit, no `inventory --json` config_path, "
            f"and no [install] render step)")
        return 1
    if not config_path.exists():
        err(f"{client}: config file {config_path} does not exist "
            f"(run `smd config init {client}` first)")
        return 1

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    heading(f"config edit {client}")
    info(f"editing: {config_path}")
    info(f"editor:  {editor}")
    print()
    try:
        proc = subprocess.run([editor, str(config_path)], check=False)
    except (OSError, FileNotFoundError) as e:
        err(f"failed to invoke {editor}: {e}")
        return 1
    return proc.returncode


def _find_render_template(deploy_path: Path) -> Optional[Path]:
    """Look for the first `kind = "render"` step in deploy.toml whose dst
    looks like the canonical config (e.g. /etc/<client>/<client>.toml)."""
    try:
        with open(deploy_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    repo_root = deploy_path.parent
    for step in (data.get("install", {}).get("steps") or []):
        if step.get("kind") != "render":
            continue
        src = step.get("src")
        if not src:
            continue
        return repo_root / src
    return None


def _config_path_from_inventory(client: str) -> Optional[Path]:
    """Ask `<client> inventory --json` where its config lives."""
    binary = shutil.which(client)
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "inventory", "--json"],
            capture_output=True, text=True, timeout=5.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    import json
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    cp = data.get("config_path")
    return Path(cp) if cp else None


def _config_path_from_deploy(deploy_path: Path) -> Optional[Path]:
    """Last-resort: pull the dst of the first render step that points
    under /etc/."""
    try:
        with open(deploy_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for step in (data.get("install", {}).get("steps") or []):
        if step.get("kind") != "render":
            continue
        dst = step.get("dst")
        if dst and str(dst).startswith("/etc/"):
            return Path(dst)
    return None


# ---------------------------------------------------------------------------
# Env var bag (CONTRACT-v0.5 §14.3)
# ---------------------------------------------------------------------------

def _build_env_bag(*, client: Optional[str] = None,
                   instance: Optional[str] = None) -> dict:
    env: dict = {}

    coord = load_coordination()
    if coord.host.call:
        env["STATION_CALL"] = coord.host.call
    if coord.host.grid:
        env["STATION_GRID"] = coord.host.grid
    if coord.host.lat:
        env["STATION_LAT"] = str(coord.host.lat)
    if coord.host.lon:
        env["STATION_LON"] = str(coord.host.lon)

    if instance:
        env["SIGMOND_INSTANCE"] = instance

    # CONTRACT-v0.5 §14.3: COUNT is always set; INDEX only when the
    # invocation maps to a specific declared radiod.
    env["SIGMOND_RADIOD_COUNT"] = str(len(coord.radiods))
    radiod_index = _resolve_radiod_index(coord, client, instance)
    if radiod_index:
        env["SIGMOND_RADIOD_INDEX"] = str(radiod_index)

    radiod_status = _resolve_radiod_status(coord, client, instance)
    if radiod_status:
        env["SIGMOND_RADIOD_STATUS"] = radiod_status

    time_source = _resolve_time_source()
    if time_source:
        env["SIGMOND_TIME_SOURCE"] = time_source

    gnss_vtec = _resolve_gnss_vtec()
    if gnss_vtec:
        env["SIGMOND_GNSS_VTEC"] = gnss_vtec

    return env


def _resolve_radiod_index(coord: Coordination,
                          client: Optional[str],
                          instance: Optional[str]) -> int:
    """Return the 1-based declaration-order position of this instance's
    radiod, or 0 when not resolvable.

    1. If `<instance>` is given and a `[[clients.<client>]]` entry
       matches with a known radiod_id, return that radiod's index.
    2. Else if exactly one radiod is declared, return 1.
    3. Else 0 (caller omits the env var).
    """
    radiod_ids = list(coord.radiods.keys())
    if client and instance:
        for c in coord.clients:
            if c.client_type != client or c.instance != instance:
                continue
            if c.radiod_id and c.radiod_id in coord.radiods:
                return radiod_ids.index(c.radiod_id) + 1
            break
    if len(radiod_ids) == 1:
        return 1
    return 0


def _resolve_radiod_status(coord: Coordination,
                           client: Optional[str],
                           instance: Optional[str]) -> str:
    """CONTRACT-v0.5 §14.3 resolution rule:
    1. If <instance> matches a [[clients.<client>]] entry whose
       radiod_id resolves, use that radiod's status_dns.
    2. Else if exactly one radiod is declared, use it.
    3. Else empty.
    """
    if client and instance:
        for c in coord.clients:
            if c.client_type != client:
                continue
            if c.instance != instance:
                continue
            if c.radiod_id and c.radiod_id in coord.radiods:
                return coord.radiods[c.radiod_id].status_dns or ""
            break

    if len(coord.radiods) == 1:
        only = next(iter(coord.radiods.values()))
        return only.status_dns or ""

    return ""


def _resolve_time_source() -> str:
    """Prefer an installed hf-timestd; fall back to environment.toml's
    declared time_sources; otherwise empty."""
    # 1. Installed hf-timestd via inventory
    binary = shutil.which("hf-timestd")
    if binary:
        try:
            proc = subprocess.run(
                [binary, "inventory", "--json"],
                capture_output=True, text=True, timeout=5.0, check=False,
            )
            if proc.returncode == 0:
                import json
                data = json.loads(proc.stdout) or {}
                # hf-timestd's web API listens on 8000 by convention
                # (see web-api/ — the running service binds there).
                # Inventory may add an explicit endpoint later; for now
                # localhost:8000 is the well-known address.
                if data.get("installed", True):
                    return "hf-timestd@localhost:8000"
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    # 2. Declared time_source in environment.toml
    env_manifest = load_environment()
    for ts in env_manifest.time_sources:
        if not ts.host:
            continue
        return f"{ts.kind or 'ntp'}@{ts.host}"

    return ""


def _resolve_gnss_vtec() -> str:
    """Pull GNSS-VTEC endpoint from hf-timestd inventory if it advertises
    one (commons.gnss_vtec).  Empty otherwise."""
    binary = shutil.which("hf-timestd")
    if not binary:
        return ""
    try:
        proc = subprocess.run(
            [binary, "inventory", "--json"],
            capture_output=True, text=True, timeout=5.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    import json
    try:
        data = json.loads(proc.stdout) or {}
    except json.JSONDecodeError:
        return ""
    return str((data.get("commons") or {}).get("gnss_vtec") or "")


def env_keys_set(env: dict) -> list:
    """Return the env var names actually set (for the human banner)."""
    return [k for k, v in env.items() if v]
