"""Sigmond catalog-driven client installer.

Clones a client repo to /opt/git/<name> and runs its canonical install.sh.
Each client's install.sh is authoritative — sigmond delegates, not duplicates.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from .catalog import CatalogEntry

GIT_BASE = Path('/opt/git')


def clone_repo(
    entry: CatalogEntry,
    *,
    base: Path = GIT_BASE,
    pull_if_exists: bool = False,
) -> Path:
    """Clone or update a client repo.

    Returns the repo directory path.
    Raises RuntimeError on clone/pull failure.
    """
    repo_dir = base / entry.name
    if repo_dir.exists():
        if pull_if_exists:
            # If remote is SSH but catalog has HTTPS, normalize — root has no SSH host keys.
            if entry.repo and entry.repo.startswith('https://'):
                cur = subprocess.run(
                    ['git', '-C', str(repo_dir), 'remote', 'get-url', 'origin'],
                    capture_output=True, text=True,
                )
                cur_url = cur.stdout.strip()
                if cur_url.startswith('git@'):
                    https_url = entry.repo.rstrip('/')
                    if not https_url.endswith('.git'):
                        https_url += '.git'
                    subprocess.run(
                        ['git', '-C', str(repo_dir), 'remote', 'set-url', 'origin', https_url],
                        capture_output=True, text=True,
                    )
            r = subprocess.run(
                ['git', '-C', str(repo_dir), 'pull', '--ff-only'],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"git pull failed in {repo_dir}: {r.stderr.strip()}"
                )
        return repo_dir

    if not entry.repo:
        raise RuntimeError(f"{entry.name}: no repo URL in catalog")

    base.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ['git', 'clone', entry.repo, str(repo_dir)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git clone {entry.repo} failed: {r.stderr.strip()}"
        )
    return repo_dir


def find_install_script(entry: CatalogEntry, repo_dir: Path) -> Optional[Path]:
    """Locate the install script, preferring the actual repo over the catalog path."""
    if not entry.install_script:
        return None
    catalog_path = Path(entry.install_script)
    if catalog_path.exists():
        return catalog_path
    relative = catalog_path.name
    for candidate in (
        repo_dir / 'scripts' / relative,
        repo_dir / 'scripts' / 'install.sh',
        repo_dir / 'install.sh',
    ):
        if candidate.exists():
            return candidate
    return None


def run_install_script(
    entry: CatalogEntry,
    repo_dir: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
) -> bool:
    """Run a client's install.sh via sudo.

    Returns True on success, False on failure.
    """
    script = find_install_script(entry, repo_dir)
    if script is None:
        return False

    cmd = ['sudo', 'bash', str(script)]
    if yes:
        cmd.append('--yes')

    if dry_run:
        return True

    r = subprocess.run(cmd, capture_output=False)
    return r.returncode == 0


def install_client(
    entry: CatalogEntry,
    *,
    dry_run: bool = False,
    yes: bool = False,
    pull: bool = False,
) -> bool:
    """Full install flow: clone repo + run install.sh.

    Returns True on success, False if the client can't be installed this way.
    """
    if not entry.install_script:
        return False

    repo_dir = clone_repo(entry, pull_if_exists=pull)
    return run_install_script(entry, repo_dir, dry_run=dry_run, yes=yes)
