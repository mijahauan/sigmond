"""Auto-discovery of installed clients from their deploy.toml manifests.

Sigmond's design goal (per Wave 2 of the drop-in client architecture) is
that a contract-conformant client author writes a ``deploy.toml`` and runs
``smd install <name>`` — sigmond auto-discovers everything from that
manifest, with no edits to ``etc/catalog.toml`` required.

This module is the canonical place to look up a client's deploy.toml and
synthesize a ``CatalogEntry`` from it.  ``catalog.load_catalog()`` calls
into here for its primary source; ``etc/catalog.toml`` is layered on top
as an operator override.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Optional


GIT_BASE = Path('/opt/git/sigmond')


def find_deploy_toml(component: str) -> Optional[Path]:
    """Find the deploy.toml for a component.

    Search order:
    1. Via ``<component> inventory --json`` → deploy_toml_path field (v0.5)
    2. Pattern A canonical: /opt/git/sigmond/<component>/deploy.toml
    3. None (caller should fall back to a shim or skip)

    Catches PermissionError on the canonical path so a component whose
    /opt/git/sigmond/<name>/ is behind a restrictive permission mask (e.g.
    wsprdaemon-client → /home/wsprdaemon/... at mode 700) doesn't abort
    higher-level commands like `smd list`.
    """
    try:
        binary = shutil.which(component)
        if binary:
            result = subprocess.run(
                [binary, "inventory", "--json"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if deploy_path := data.get('deploy_toml_path'):
                    p = Path(deploy_path)
                    if p.exists():
                        return p
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError):
        pass

    canonical = GIT_BASE / component / 'deploy.toml'
    try:
        if canonical.exists():
            return canonical
    except PermissionError:
        pass

    return None


def find_client_repo(name: str) -> Optional[Path]:
    """Locate a client's repo at /opt/git/sigmond/<name> (the Pattern A canonical
    location).  Returns None if the directory does not exist."""
    repo = GIT_BASE / name
    try:
        if repo.exists():
            return repo
    except PermissionError:
        pass
    return None


def _read_deploy_toml(deploy_path: Path) -> Optional[dict]:
    try:
        with open(deploy_path, 'rb') as f:
            return tomllib.load(f)
    except (OSError, PermissionError, tomllib.TOMLDecodeError):
        return None


def synthesize_catalog_entry(deploy_path: Path):
    """Build a CatalogEntry from a deploy.toml.

    Reads the optional ``[client]`` block (Wave 2 spec) for identity +
    policy.  Falls back to ``[package]`` for clients that haven't migrated
    yet.  Returns None when the file can't be parsed.

    Local import of ``CatalogEntry`` keeps this module free of import
    cycles when ``catalog.py`` calls back into us.
    """
    from .catalog import CatalogEntry

    data = _read_deploy_toml(deploy_path)
    if data is None:
        return None

    client_block = data.get('client') or {}
    package_block = data.get('package') or {}

    # Prefer the explicit [client].name (the Wave 2 contract field).  Fall
    # back to the install-directory name — that's the canonical sigmond
    # identity (matches /opt/git/sigmond/<name>/, topology.toml, catalog keys).
    # The deploy.toml's [package].name often differs (e.g. 'wsprdaemon'
    # vs the directory 'wsprdaemon-client') and would split a single
    # client into two catalog entries.
    name = client_block.get('name') or deploy_path.parent.name
    if not name:
        return None

    kind = client_block.get('kind') or package_block.get('kind') or 'client'
    description = client_block.get('description') or package_block.get('description', '')
    repo = client_block.get('repo', '')
    # Fall back to the source repo's actual remote when the deploy.toml
    # didn't declare one.  Without this, components like hfdl-recorder /
    # gpsdo-monitor that have a real GitHub remote but no [client] repo=
    # in their deploy.toml are silently skipped by `smd update`.
    if not repo:
        try:
            import subprocess as _sp
            r = _sp.run(
                ['git', '-C', str(deploy_path.parent),
                 'remote', 'get-url', 'origin'],
                capture_output=True, text=True, check=False,
            )
            if r.returncode == 0:
                url = r.stdout.strip()
                # Convert SSH form to HTTPS so root can pull without an SSH key.
                if url.startswith('git@github.com:'):
                    url = 'https://github.com/' + url[len('git@github.com:'):]
                if url.endswith('.git'):
                    url = url[:-4]
                if url.startswith(('http://', 'https://', 'git@')):
                    repo = url
        except Exception:
            pass  # best-effort; leave repo empty
    requires = tuple(client_block.get('requires') or ())
    uses = tuple(client_block.get('uses') or ())
    contract = (client_block.get('contract')
                or package_block.get('contract_version')
                or None)
    if contract == "":
        contract = None
    aliases = client_block.get('aliases') or ()
    topology_alias = aliases[0] if aliases else (client_block.get('topology_alias') or None)

    lifecycle_block = client_block.get('lifecycle') or {}
    raw_priority = lifecycle_block.get('start_priority')
    start_priority = int(raw_priority) if raw_priority is not None else None

    install_script = client_block.get('install_script')
    if not install_script:
        candidate = deploy_path.parent / 'scripts' / 'install.sh'
        if candidate.exists():
            install_script = str(candidate)
        else:
            candidate = deploy_path.parent / 'install.sh'
            if candidate.exists():
                install_script = str(candidate)

    return CatalogEntry(
        name=name,
        kind=kind,
        description=description,
        repo=repo,
        uses=uses,
        requires=requires,
        contract=contract or None,
        install_script=install_script or None,
        topology_alias=topology_alias,
        start_priority=start_priority,
    )


def discover_catalog_entries(base: Path = GIT_BASE) -> dict:
    """Glob ``base/*/deploy.toml`` and synthesize CatalogEntry objects.

    Returns ``{name: CatalogEntry}``.  Silently skips any deploy.toml that
    can't be parsed — discovery should never crash a top-level command.
    """
    entries: dict = {}
    try:
        children = list(base.iterdir())
    except (OSError, PermissionError):
        return entries

    for child in children:
        deploy = child / 'deploy.toml'
        try:
            if not deploy.exists():
                continue
        except PermissionError:
            continue
        entry = synthesize_catalog_entry(deploy)
        if entry is not None:
            entries[entry.name] = entry
    return entries
