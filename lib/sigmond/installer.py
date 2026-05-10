"""Sigmond catalog-driven client installer.

Clones a client repo to /opt/git/sigmond/<name> and runs its canonical install.sh.
Each client's install.sh is authoritative — sigmond delegates, not duplicates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Optional

from .catalog import CatalogEntry

GIT_BASE = Path('/opt/git/sigmond')


def _apply_canonical_perms(repo_dir: Path) -> None:
    """Chown a freshly-cloned repo to sigmond:sigmond + setgid on dirs.

    `git clone` invoked as root (the smd install case) leaves the repo
    owned root:root, which means human users in the sigmond group can't
    edit sources without sudo.  Sigmond's own install.sh applies the same
    treatment to /opt/git/sigmond/* once at install time; mirror that
    here so newly-installed clients are immediately group-writable.

    Also adds a system-wide git safe.directory entry so any user can run
    plain `git` against the new repo without per-user config.

    Best-effort: missing `sigmond` user, missing tools, or non-root
    invocations are logged and skipped rather than failing the install.
    """
    if not repo_dir.exists():
        return

    # chown -R sigmond:sigmond
    if shutil.which('chown'):
        r = subprocess.run(
            ['chown', '-R', 'sigmond:sigmond', str(repo_dir)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(
                f"[warn] chown sigmond:sigmond {repo_dir} failed: "
                f"{r.stderr.strip()}",
                file=sys.stderr,
            )
            return

    # chmod -R g+rwX  (group read/write, exec only on dirs/already-exec)
    subprocess.run(
        ['chmod', '-R', 'g+rwX', str(repo_dir)],
        capture_output=True, text=True,
    )

    # setgid on directories so new files inherit the sigmond group
    subprocess.run(
        ['find', str(repo_dir), '-type', 'd', '-exec', 'chmod', 'g+s', '{}', '+'],
        capture_output=True, text=True,
    )

    # System-wide safe.directory so plain `git` works for any user.
    existing = subprocess.run(
        ['git', 'config', '--system', '--get-all', 'safe.directory'],
        capture_output=True, text=True,
    )
    if str(repo_dir) not in existing.stdout.splitlines():
        subprocess.run(
            ['git', 'config', '--system', '--add',
             'safe.directory', str(repo_dir)],
            capture_output=True, text=True,
        )


def _git(repo_dir: Path, *git_args: str) -> subprocess.CompletedProcess:
    """Run a `git -C <repo_dir>` command with safe.directory pre-set.

    Sigmond often invokes git as root over repos owned by the `sigmond`
    system user — that trips git's dubious-ownership check.  Passing
    `-c safe.directory=<repo_dir>` per call avoids the need for any
    /etc/gitconfig edit and works regardless of who's running.
    """
    return subprocess.run(
        ['git', '-c', f'safe.directory={repo_dir}',
         '-C', str(repo_dir), *git_args],
        capture_output=True, text=True,
    )


def _normalize_remote_url(repo_dir: Path, https_url: str) -> None:
    """Switch the origin remote to HTTPS if it's currently SSH.

    Root typically has no SSH host keys, so HTTPS is safer for automated pulls.
    """
    cur = _git(repo_dir, 'remote', 'get-url', 'origin')
    if cur.stdout.strip().startswith('git@'):
        normalized = https_url.rstrip('/')
        if not normalized.endswith('.git'):
            normalized += '.git'
        _git(repo_dir, 'remote', 'set-url', 'origin', normalized)


def git_head_ref(repo_dir: Path) -> str:
    """Return a short human-readable ref for the current HEAD (e.g. 'main@abc1234')."""
    branch = _git(repo_dir, 'rev-parse', '--abbrev-ref', 'HEAD')
    sha    = _git(repo_dir, 'rev-parse', '--short', 'HEAD')
    b = branch.stdout.strip()
    s = sha.stdout.strip()
    if b and b != 'HEAD' and s:
        return f'{b}@{s}'
    return s or '?'


def clone_repo(
    entry: CatalogEntry,
    *,
    base: Path = GIT_BASE,
    pull_if_exists: bool = False,
    ref: Optional[str] = None,
) -> Path:
    """Clone or update a client repo.

    If *ref* is given, fetch origin then check out that commit/branch/tag.
    Otherwise pull --ff-only to advance to the latest upstream HEAD.
    Returns the repo directory path.
    Raises RuntimeError on clone/pull/checkout failure.
    """
    repo_dir = base / entry.name
    if repo_dir.exists():
        if pull_if_exists or ref is not None:
            if entry.repo and entry.repo.startswith('https://'):
                _normalize_remote_url(repo_dir, entry.repo)
            if ref is not None:
                r = _git(repo_dir, 'fetch', 'origin')
                if r.returncode != 0:
                    raise RuntimeError(
                        f"git fetch failed in {repo_dir}: {r.stderr.strip()}"
                    )
                r = _git(repo_dir, 'checkout', ref)
                if r.returncode != 0:
                    raise RuntimeError(
                        f"git checkout {ref!r} failed in {repo_dir}: {r.stderr.strip()}"
                    )
            else:
                # Fetch first, then reset to origin's default branch.
                # git pull --ff-only fails when HEAD is detached (e.g. after
                # a previous pinned-ref checkout), so we use fetch + checkout -B.
                r = _git(repo_dir, 'fetch', 'origin')
                if r.returncode != 0:
                    raise RuntimeError(
                        f"git fetch failed in {repo_dir}: {r.stderr.strip()}"
                    )
                # Discover the remote's default branch (usually main).
                sym = _git(repo_dir, 'symbolic-ref',
                           '--short', 'refs/remotes/origin/HEAD')
                if sym.returncode == 0 and sym.stdout.strip():
                    remote_branch = sym.stdout.strip()           # e.g. origin/main
                    local_branch  = remote_branch.split('/', 1)[-1]   # e.g. main
                else:
                    remote_branch = 'origin/main'
                    local_branch  = 'main'
                # checkout -B resets the local branch to match origin whether
                # we're currently on a branch or in detached HEAD.
                r = _git(repo_dir, 'checkout', '-B',
                         local_branch, remote_branch)
                if r.returncode != 0:
                    raise RuntimeError(
                        f"git checkout {local_branch} failed in {repo_dir}: "
                        f"{r.stderr.strip()}"
                    )
        return repo_dir

    if not entry.repo:
        raise RuntimeError(f"{entry.name}: no repo URL in catalog")

    base.mkdir(parents=True, exist_ok=True)
    # `git clone` itself doesn't trip dubious-ownership (it creates the
    # repo so it owns the just-made .git/), so no safe.directory needed.
    r = subprocess.run(
        ['git', 'clone', entry.repo, str(repo_dir)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git clone {entry.repo} failed: {r.stderr.strip()}"
        )
    if ref is not None:
        r = _git(repo_dir, 'checkout', ref)
        if r.returncode != 0:
            raise RuntimeError(
                f"git checkout {ref!r} failed after clone: {r.stderr.strip()}"
            )
    _apply_canonical_perms(repo_dir)
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

    # Pull sigmond's identity bag (STATION_*, SIGMOND_RADIOD_*) into the
    # env so the client's install.sh and any wizard it spawns see the
    # CLIENT-CONTRACT v0.5 §14.2 vars.  Without this, install.sh's wizards
    # (e.g. hf-timestd's setup-station.sh) re-prompt for callsign / grid
    # / multicast on every fresh install — sudo's env_reset would have
    # stripped them even if smd's caller had them in its shell.
    env = dict(os.environ)
    try:
        with open('/etc/sigmond/coordination.env') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k, v = k.strip(), v.strip()
                if k:
                    env.setdefault(k, v)
    except OSError:
        pass

    # sudo's default env_reset drops everything not on secure_path; the
    # contract bag has to be explicitly preserved.  Listed exhaustively
    # rather than `--preserve-env` (which keeps *everything*) to avoid
    # leaking unrelated user shell state into the install context.
    preserve = ','.join([
        'STATION_CALL', 'STATION_GRID', 'STATION_LAT', 'STATION_LON',
        'SIGMOND_INSTANCE', 'SIGMOND_RADIOD_COUNT',
        'SIGMOND_RADIOD_INDEX', 'SIGMOND_RADIOD_STATUS',
        'SIGMOND_TIME_SOURCE', 'SIGMOND_GNSS_VTEC',
    ])

    cmd = ['sudo', f'--preserve-env={preserve}', 'bash', str(script)]
    if yes:
        cmd.append('--yes')

    if dry_run:
        return True

    r = subprocess.run(cmd, env=env, capture_output=False)
    return r.returncode == 0


def apply_deploy_toml_links(repo_dir: Path, dry_run: bool = False) -> list[str]:
    """Execute 'link' kind install steps from deploy.toml that are missing or wrong.

    Creates symlinks declared in [[install.steps]] with kind="link".  Skips
    steps where the symlink already points at the correct target.  Returns a
    list of human-readable messages (one per action taken or error).

    Safe to call on every apply — link creation is idempotent.
    """
    deploy_toml = repo_dir / 'deploy.toml'
    if not deploy_toml.exists():
        return []
    try:
        with open(deploy_toml, 'rb') as f:
            config = tomllib.load(f)
    except Exception as exc:
        return [f"warning: could not read {deploy_toml}: {exc}"]

    msgs: list[str] = []
    for step in config.get('install', {}).get('steps', []):
        if step.get('kind') != 'link':
            continue
        src_rel = step.get('src', '')
        dst_str = step.get('dst', '')
        if not src_rel or not dst_str:
            continue
        src  = (repo_dir / src_rel).resolve()
        dst  = Path(dst_str)
        if not src.exists():
            continue
        # Already correct?
        if dst.is_symlink() and dst.resolve() == src:
            continue
        if dry_run:
            msgs.append(f"  (dry-run) would link {dst} → {src}")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            dst.symlink_to(src)
            msgs.append(f"  linked {dst.name} → {src}")
        except OSError as exc:
            msgs.append(f"  warning: could not link {dst}: {exc}")
    return msgs


def install_client(
    entry: CatalogEntry,
    *,
    dry_run: bool = False,
    yes: bool = False,
    pull: bool = False,
) -> bool:
    """Full install flow: clone repo + run install.sh + deploy.toml link steps.

    Returns True on success, False if the client can't be installed this way.
    """
    if not entry.install_script:
        return False

    repo_dir = clone_repo(entry, pull_if_exists=pull)
    ok = run_install_script(entry, repo_dir, dry_run=dry_run, yes=yes)
    # Apply any deploy.toml link steps not covered by install.sh (idempotent).
    link_msgs = apply_deploy_toml_links(repo_dir, dry_run=dry_run)
    if link_msgs:
        for msg in link_msgs:
            print(msg)
        # Reload systemd if we wrote any new unit files.
        if not dry_run and any('/etc/systemd' in m for m in link_msgs
                               if not m.startswith('  warning')):
            subprocess.run(['systemctl', 'daemon-reload'],
                           capture_output=True)
    return ok
