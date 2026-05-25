"""Full client purge — the operator escape hatch for deprecated clients.

``smd remove <name> --purge`` (or ``smd remove <name>`` when ``<name>``
is in the deprecation list) routes here.  In order:

1. Stop and disable every systemd unit declared by the client's
   ``deploy.toml`` (read it BEFORE deleting anything).
2. Remove the symlinks created by the deploy.toml ``kind="link"`` install
   steps (typically ``/etc/systemd/system/<unit>``).
3. ``systemctl daemon-reload``.
4. ``rm -rf`` the venv at ``/opt/<name>/`` if present.
5. ``rm -rf`` the source repo at ``/opt/git/sigmond/<name>/``.
6. ``rm -rf`` the config dir at ``/etc/<name>/``.

Refuses to act on a name that's neither in the deprecation list nor on
disk — better to surface "nothing to purge" than to power through a
typo.  The caller (bin/smd) supplies confirmation prompts; this module
just executes the plan and prints what it's doing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Optional

GIT_BASE = Path('/opt/git/sigmond')
ETC_BASE = Path('/etc')
VENV_BASE = Path('/opt')
SYSTEMD_SYSTEM = Path('/etc/systemd/system')


def _read_deploy_toml(repo_dir: Path) -> Optional[dict]:
    deploy = repo_dir / 'deploy.toml'
    if not deploy.exists():
        return None
    try:
        with open(deploy, 'rb') as f:
            return tomllib.load(f)
    except (OSError, ValueError) as exc:
        print(f"  warning: could not read {deploy}: {exc}", file=sys.stderr)
        return None


def _running_template_instances(unit: str) -> list[str]:
    """For a template unit ``foo@.service`` return the currently-running
    ``foo@instance.service`` names.  Used so stop/disable hits each
    real instance, not the template skeleton (which systemctl rejects)."""
    template = unit.replace('@.', '@')
    r = subprocess.run(
        ['systemctl', 'list-units', '--type=service', '--all',
         '--no-legend', '--plain', f'{template}*'],
        capture_output=True, text=True, check=False,
    )
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]


def _expand_units(declared: list[str]) -> list[str]:
    """Turn the deploy.toml ``units`` list (which may contain template
    skeletons like ``radiod@.service``) into concrete unit names."""
    out: list[str] = []
    for unit in declared:
        if '@.' in unit:
            out.extend(_running_template_instances(unit))
        else:
            out.append(unit)
    return out


def plan_purge(name: str, *, extra_paths: tuple[str, ...] = ()) -> dict:
    """Compute what a purge of *name* would touch.  Pure inspection —
    no side effects.  Returned dict is suitable for both human display
    and as the input to ``execute_purge``.

    *extra_paths* (typically pulled from a ``DeprecatedEntry``) are
    absolute paths added to the rm-rf list.  Missing ones are silently
    dropped so a deprecation block can list paths that may or may not
    exist on a given host."""
    repo_dir = GIT_BASE / name
    deploy = _read_deploy_toml(repo_dir) if repo_dir.exists() else None

    declared_units: list[str] = []
    link_dsts: list[Path] = []
    if deploy is not None:
        declared_units = list(deploy.get('systemd', {}).get('units', []))
        for step in deploy.get('install', {}).get('steps', []):
            if step.get('kind') == 'link' and step.get('dst'):
                link_dsts.append(Path(step['dst']))

    extras = [Path(p) for p in extra_paths if Path(p).exists()]

    return {
        'name': name,
        'repo_dir': repo_dir if repo_dir.exists() else None,
        'venv_dir': VENV_BASE / name if (VENV_BASE / name).exists() else None,
        'config_dir': ETC_BASE / name if (ETC_BASE / name).exists() else None,
        'extra_dirs': extras,
        'declared_units': declared_units,
        'expanded_units': _expand_units(declared_units),
        'link_dsts': link_dsts,
    }


def render_plan(plan: dict) -> list[str]:
    """One-line-per-thing human description of the purge plan."""
    out: list[str] = []
    if plan['expanded_units']:
        out.append(f"  systemd:   stop+disable {', '.join(plan['expanded_units'])}")
    for dst in plan['link_dsts']:
        if dst.is_symlink() or dst.exists():
            out.append(f"  symlink:   rm {dst}")
    if plan['venv_dir']:
        out.append(f"  venv:      rm -rf {plan['venv_dir']}")
    if plan['repo_dir']:
        out.append(f"  source:    rm -rf {plan['repo_dir']}")
    if plan['config_dir']:
        out.append(f"  config:    rm -rf {plan['config_dir']}")
    for extra in plan.get('extra_dirs', ()):
        out.append(f"  extra:     rm -rf {extra}")
    if not out:
        out.append(f"  (nothing on disk for {plan['name']})")
    return out


def execute_purge(plan: dict, *, dry_run: bool = False) -> int:
    """Execute the plan returned by ``plan_purge``.  Returns 0 on
    success, non-zero on the first hard failure.  Best-effort: a
    failed systemctl-stop on a unit that's already gone is logged
    but doesn't abort the rest."""
    name = plan['name']

    if dry_run:
        for line in render_plan(plan):
            print(line)
        return 0

    # 1. Stop + disable units.  Stop first so disable doesn't race
    # against an in-flight start.
    for unit in plan['expanded_units']:
        r = subprocess.run(['systemctl', 'stop', unit],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0 and 'not loaded' not in r.stderr:
            print(f"  warning: systemctl stop {unit}: {r.stderr.strip()}",
                  file=sys.stderr)
    for unit in plan['declared_units']:
        # Templates can't be disabled directly; disabling the skeleton
        # is what removes the *.wants/ symlinks pointing at instances.
        r = subprocess.run(['systemctl', 'disable', unit],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0 and 'No such file' not in r.stderr \
                and 'does not exist' not in r.stderr:
            print(f"  warning: systemctl disable {unit}: {r.stderr.strip()}",
                  file=sys.stderr)

    # 2. Remove symlinks the deploy.toml installed.
    for dst in plan['link_dsts']:
        try:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
                print(f"  removed {dst}")
        except OSError as exc:
            print(f"  warning: rm {dst}: {exc}", file=sys.stderr)

    # 3. daemon-reload so systemd forgets the just-removed units.
    subprocess.run(['systemctl', 'daemon-reload'],
                   capture_output=True, check=False)

    # 4-6. rm -rf each tree.  shutil.rmtree handles the depth; we just
    # log each top-level we successfully removed.
    for key, label in (('venv_dir', 'venv'),
                       ('repo_dir', 'source'),
                       ('config_dir', 'config')):
        target = plan[key]
        if target is None:
            continue
        try:
            shutil.rmtree(target)
            print(f"  removed {label}: {target}")
        except OSError as exc:
            print(f"  error: rm -rf {target}: {exc}", file=sys.stderr)
            return 1
    for extra in plan.get('extra_dirs', ()):
        try:
            shutil.rmtree(extra)
            print(f"  removed extra: {extra}")
        except OSError as exc:
            print(f"  error: rm -rf {extra}: {exc}", file=sys.stderr)
            return 1

    return 0


def deprecated_on_disk(deprecated: dict) -> list[str]:
    """Return the names from *deprecated* that still have a checkout
    under /opt/git/sigmond/.  Used by ``smd list`` to surface the
    "you have a deprecated client lingering" hint."""
    return sorted(name for name in deprecated
                  if (GIT_BASE / name).exists())
