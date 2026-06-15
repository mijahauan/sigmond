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


def _checkout_ref(repo_dir: Path, ref: str) -> None:
    """Check out ``ref``, deepening a shallow clone if the ref isn't reachable.

    sigmond#13: install.sh pre-clones every catalog repo with ``--depth 1`` for
    a fast switch-on later.  But a pinned ref older than that single commit —
    e.g. ka9q-radio's ka9q-python-compat pin — isn't in the shallow history, so
    a plain ``git fetch origin`` doesn't bring it and ``git checkout <pin>``
    fails with "fatal: reference is not a tree" / "unable to read tree".  That
    aborts the whole install; pre-fix it bricked the first greenfield bring-up.

    Building the EXACT pin matters: ka9q-radio's wire-protocol headers are what
    ka9q-web and ka9q-python adapt to, so we must never silently settle for the
    shallow HEAD.  When the ref is missing we deepen (``fetch --unshallow``) and
    only then check it out — or fail loudly if the pin genuinely doesn't exist.
    """
    reachable = _git(repo_dir, 'rev-parse', '--verify', '--quiet',
                     f'{ref}^{{commit}}')
    if reachable.returncode != 0:
        shallow = _git(repo_dir, 'rev-parse', '--is-shallow-repository')
        if shallow.stdout.strip() == 'true':
            un = _git(repo_dir, 'fetch', '--unshallow', 'origin')
            if un.returncode != 0:
                raise RuntimeError(
                    f"git fetch --unshallow failed in {repo_dir} (needed to "
                    f"reach pinned ref {ref!r}): {un.stderr.strip()}"
                )
    r = _git(repo_dir, 'checkout', ref)
    if r.returncode != 0:
        raise RuntimeError(
            f"git checkout {ref!r} failed in {repo_dir}: {r.stderr.strip()}"
        )


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
                # Shallow-aware: deepen the clone if `ref` (e.g. ka9q-radio's
                # compat pin) isn't in the --depth 1 history (sigmond#13).
                _checkout_ref(repo_dir, ref)
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
        # A fresh clone here is full-depth, but route through the same helper so
        # a future shallow clone path stays correct (sigmond#13).
        _checkout_ref(repo_dir, ref)
    _apply_canonical_perms(repo_dir)
    return repo_dir


def find_install_script(entry: CatalogEntry, repo_dir: Path) -> Optional[Path]:
    """Locate the install script, preferring the actual repo over the catalog path.

    When the catalog entry has no ``install_script`` (clients like
    mag-recorder rely on convention rather than an explicit catalog pin),
    fall back to the standard in-repo locations.  This lets the installer
    clone a repo first and discover its install.sh post-clone.
    """
    if entry.install_script:
        catalog_path = Path(entry.install_script)
        if catalog_path.exists():
            return catalog_path
        relative = catalog_path.name
    else:
        relative = 'install.sh'
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


def apply_systemd_enables(repo_dir: Path, dry_run: bool = False) -> list[str]:
    """Ensure every non-template unit in `[systemd] units` is enabled.

    A unit can be installed (link/copy step in deploy.toml writes the file
    into /etc/systemd/system/) yet still not be wanted by multi-user.target
    at boot — because nothing ever ran `systemctl enable`.  That is exactly
    the failure mode that left B4-100's wsprdaemon.target sitting linked
    but inactive after the 2026-05-16 power outage.

    Reads `[systemd] units = [...]` from the client's deploy.toml and, for
    each *non-template* entry (templates can't be enabled directly; their
    instances are wanted by a parent target instead), runs
    `systemctl is-enabled <unit>` and, if the state is `linked`,
    `disabled`, or `indirect`, runs `systemctl enable <unit>` to create
    the missing `*.wants/<unit>` symlink.

    Idempotent — already-enabled units are skipped.  Static / masked /
    alias / generated / transient units are left alone (no [Install]
    section to act on, or operator-locked).
    """
    deploy_toml = repo_dir / 'deploy.toml'
    if not deploy_toml.exists():
        return []
    try:
        with open(deploy_toml, 'rb') as f:
            config = tomllib.load(f)
    except Exception as exc:
        return [f"warning: could not read {deploy_toml}: {exc}"]

    units = config.get('systemd', {}).get('units', [])
    msgs: list[str] = []
    for unit in units:
        # Template units (`foo@.service`) can't be enabled directly;
        # their instances are wanted by a parent target.
        if '@.' in unit:
            continue
        r = subprocess.run(
            ['systemctl', 'is-enabled', unit],
            capture_output=True, text=True, check=False,
        )
        state = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else ''
        if state in ('enabled', 'enabled-runtime', 'static',
                     'masked', 'alias', 'generated', 'transient'):
            continue
        if state not in ('linked', 'disabled', 'indirect'):
            # Unknown state — surface it, don't try to fix.
            msgs.append(f"  warning: {unit}: is-enabled returned {state!r} (skipped)")
            continue
        # Skip units that have no [Install] section — systemctl reports
        # them as `linked` (because the file under /etc/systemd/system is
        # itself a symlink into the repo), but `systemctl enable` is a
        # no-op and just emits a stderr explanation.  Such units are
        # typically helpers pulled in by another unit's wants/, not boot-
        # time entry points.  Detect by parsing the unit text rather than
        # trusting systemctl's exit code post-enable.
        cat = subprocess.run(
            ['systemctl', 'cat', unit],
            capture_output=True, text=True, check=False,
        )
        if cat.returncode == 0:
            install_section = False
            in_install = False
            for line in cat.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    in_install = (stripped == '[Install]')
                    continue
                if in_install and stripped.startswith((
                    'WantedBy=', 'RequiredBy=', 'UpheldBy=', 'Also=',
                    'Alias=', 'DefaultInstance=',
                )):
                    install_section = True
                    break
            if not install_section:
                continue  # no [Install] keys → can't be enabled, skip silently
        if dry_run:
            msgs.append(f"  (dry-run) would enable {unit} (was: {state})")
            continue
        r2 = subprocess.run(
            ['systemctl', 'enable', unit],
            capture_output=True, text=True, check=False,
        )
        if r2.returncode == 0:
            msgs.append(f"  enabled {unit} (was: {state})")
        else:
            err = (r2.stderr or r2.stdout).strip().splitlines()[-1] if (r2.stderr or r2.stdout) else 'unknown error'
            msgs.append(f"  warning: could not enable {unit}: {err}")
    return msgs


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

    # Also link EVERY systemd unit file shipped in the repo's systemd/ dir
    # (services, timers, targets, ...), not just the ones with an explicit
    # [[install.steps]] link.  The [systemd] lifecycle list and the unit deps
    # (a .timer triggers its .service, a .service has OnFailure=, etc.) assume
    # all the component's units are installed; authors routinely ship a unit
    # without a link step (e.g. hf-timestd's grape-*.timer AND the
    # grape-*.service they trigger), so `smd start` fails 'Unit not found'.
    # Drop-in *.conf files are not units (handled by their own steps); skip.
    _UNIT_EXTS = ('.service', '.timer', '.target', '.socket', '.path',
                  '.mount', '.slice')
    systemd_dir = repo_dir / 'systemd'
    for src in (sorted(systemd_dir.iterdir()) if systemd_dir.is_dir() else []):
        if not src.is_file() or src.suffix not in _UNIT_EXTS:
            continue
        src = src.resolve()
        dst = Path('/etc/systemd/system') / src.name
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


def _clone_source_only_deps(
    entry: CatalogEntry,
    catalog: Optional[dict],
    *,
    base: Optional[Path] = None,
    dry_run: bool = False,
) -> None:
    """Auto-clone source-only deps declared in ``entry.requires``.

    A "source-only dep" is a catalog entry with a ``repo`` URL but no
    ``install_script`` — the consumer's install.sh / pyproject.toml
    references the checkout directly (e.g. ``callhash`` and
    ``hs-uploader`` are declared as ``[tool.uv.sources]`` editable
    siblings by wspr-recorder / psk-recorder / mag-recorder).
    Sigmond's job is to ensure the source tree exists where the
    consumer expects it.

    No-op when ``catalog`` is None (test paths / older callers).
    Already-cloned deps are left alone.

    Resolves ``base`` at call time (not via a default-arg binding) so
    tests can monkeypatch ``GIT_BASE`` to keep the dep-already-on-disk
    check away from a real ``/opt/git/sigmond/`` on the host.
    """
    if not catalog:
        return
    if base is None:
        base = GIT_BASE
    for dep_name in entry.requires:
        dep = catalog.get(dep_name)
        if dep is None or not dep.repo or dep.install_script:
            continue
        dep_dir = base / dep_name
        if dep_dir.exists():
            continue
        if dry_run:
            print(f"  (dry-run) would clone source dep {dep_name} from {dep.repo}")
            continue
        print(f"  cloning source dep {dep_name} from {dep.repo}")
        try:
            clone_repo(dep, base=base)
        except RuntimeError as exc:
            print(f"  warning: could not clone {dep_name}: {exc}",
                  file=sys.stderr)


def install_client(
    entry: CatalogEntry,
    *,
    dry_run: bool = False,
    yes: bool = False,
    pull: bool = False,
    catalog: Optional[dict] = None,
) -> bool:
    """Full install flow: clone repo + run install.sh + deploy.toml link steps.

    Returns True on success, False if the client can't be installed this way.

    A missing ``install_script`` in the catalog is no longer fatal: as long
    as the entry has a repo URL we clone it and look for an install.sh
    inside.  Clients like mag-recorder rely on this convention rather than
    pinning the script path in the catalog.

    When *catalog* is supplied, any required catalog entry that's a pure
    source dep (has a ``repo`` URL but no ``install_script``) is cloned
    to ``/opt/git/sigmond/<name>`` before the consumer's install.sh runs.
    """
    if not entry.install_script and not entry.repo:
        return False

    repo_dir = clone_repo(entry, pull_if_exists=pull)
    _clone_source_only_deps(entry, catalog, dry_run=dry_run)
    script = find_install_script(entry, repo_dir)
    if script is None:
        # No install.sh anywhere — fall back to deploy.toml-only install
        # (link steps + systemd enables).  Only counts as success if the
        # deploy.toml actually defined something to do.
        deploy = repo_dir / 'deploy.toml'
        if not deploy.exists():
            print(f"[err] {entry.name}: no install_script and no deploy.toml in {repo_dir}",
                  file=sys.stderr)
            return False
        ok = True
    else:
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
