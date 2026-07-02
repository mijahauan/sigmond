"""Pre-capture wipe planner — the inverse of ``smd admin personalize``.

Builds the ordered action plan that strips a reference VM of everything
per-site — secrets, station keys, identity, per-instance configs, and
(optionally) accumulated data — so the VM is fit to capture as the DASI2
golden image.  Implements the PROVISIONING-INPUTS.md §9 "before
capturing" checklist; ``smd admin readiness --gate capture`` is the
final arbiter after execution.

Pure and path-injectable for tests; execution (root, service stops,
coordination/psws writers) lives in ``smd`` (``cmd_capture_prep``).
Shareable state stays: software, venvs, unit files, PHaRLAP/pyLAP
(decided 2026-06-14 — verify post-clone with `hf-timestd data sources`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Action:
    """One wipe step.  ``kind`` tells the executor what to do:

    * ``remove``            — delete a file (payload = path)
    * ``clear-tree``        — delete a directory's contents, keep the dir
    * ``reset-template``    — overwrite payload path with the site-profile scaffold
    * ``clear-coordination``— empty [host] identity + drop [station], re-render env
    * ``psws-placeholders`` — reset a PSWS recorder's ids to template placeholders
                              (payload = recorder name)
    * ``truncate``          — truncate a file to zero bytes (machine-id)
    * ``vacuum-journal``    — drop journald history
    * ``note``              — informational only
    """
    kind: str
    label: str
    payload: Optional[str] = None


# §4 delivered secrets + every station SSH key (keys self-generate per
# site — hs-uploader on first ship, or `smd config render` which also
# prints the pubkey to register).
SECRET_FILES = (
    '/etc/sigmond/frpc.toml',
    '/etc/hf-timestd/earthdata-netrc',
    '/etc/hs-uploader/keys/id_ed25519_host',
    '/etc/hs-uploader/keys/id_ed25519_host.pub',
    '/etc/hs-uploader/keys/id_ed25519',
    '/etc/hs-uploader/keys/id_ed25519.pub',
    '/home/timestd/.ssh/id_rsa_psws',
    '/home/timestd/.ssh/id_rsa_psws.pub',
)

# Accumulated per-site data: sink queues, upload cursors, learned
# callsign hashes, science archives, logs.  Directories are cleared,
# not removed (units expect them; ownership/modes preserved).
DATA_FILES = (
    '/var/lib/sigmond/sink.db',
    '/var/lib/sigmond/sink.db-wal',
    '/var/lib/sigmond/sink.db-shm',
    '/var/lib/sigmond/net-diag.json',
    '/var/lib/hs-uploader/watermarks.db',
    '/var/lib/hs-uploader/known_hosts',
    # FFT wisdom is per-CPU — a clone on different silicon must regenerate.
    '/etc/fftw/wisdomf',
)
DATA_TREES = (
    '/var/lib/timestd',
    '/var/lib/wspr-recorder',
    '/var/lib/psk-recorder',
    '/var/lib/mag-recorder',
    '/var/lib/meteor-scatter',
    '/var/log/wspr-recorder',
    '/var/log/psk-recorder',
    '/var/log/meteor-scatter',
    '/var/log/hf-timestd',
    '/var/log/sigmond',
)

# OS identity — wiped LAST; regenerated on the clone's first boot by
# `smd admin personalize --reset-identity`.
MACHINE_ID = '/etc/machine-id'
DBUS_MACHINE_ID = '/var/lib/dbus/machine-id'
SSH_HOST_KEY_GLOB = '/etc/ssh/ssh_host_*'
PERSONALIZED_SENTINEL = '/etc/sigmond/.personalized'
SITE_PROFILE = '/etc/sigmond/site-profile.toml'

# PSWS recorders whose configs carry pushed station/instrument ids
# (kept in sync with sigmond.psws.RECORDERS).
PSWS_RECORDERS = ('hf-timestd', 'mag-recorder')


def _p(root: Path, abs_path: str) -> Path:
    return root / abs_path.lstrip('/')


def _exists(p: Path) -> bool:
    """Existence probe for an unprivileged planner run.  A path behind a
    0700 directory (e.g. /home/timestd/.ssh/*) raises PermissionError —
    for a WIPE plan, "can't tell" conservatively plans the removal; the
    root executor's ``missing_ok`` unlink no-ops if it wasn't there."""
    try:
        return p.exists()
    except (PermissionError, OSError):
        return True


def _nonempty_dir(p: Path) -> bool:
    try:
        return p.is_dir() and any(p.iterdir())
    except (PermissionError, OSError):
        return True


def build_capture_plan(*,
                       keep_data: bool = False,
                       root: Path = Path('/'),
                       instances: Optional[list] = None,
                       ssh_host_keys: Optional[list] = None) -> list:
    """Ordered wipe plan.  Only existing paths produce actions, so the
    plan doubles as a report of what the image actually carries.

    ``instances``: per-instance recorder configs as
    ``(client, reporter_id, config_path, env_path)`` tuples — the
    executor flattens ``sigmond.instance.list_instances()`` into these;
    paths may be None/absent.
    ``ssh_host_keys``: expanded ssh_host_* list (injectable for tests).
    """
    plan: list[Action] = []

    # 1. secrets + station keys
    for f in SECRET_FILES:
        if _exists(_p(root, f)):
            plan.append(Action('remove', f'secret/key: {f}', f))

    # 2. identity — site profile back to scaffold, coordination emptied,
    #    PSWS ids back to placeholders, per-instance configs removed.
    if _exists(_p(root, SITE_PROFILE)):
        plan.append(Action('reset-template',
                           f'reset {SITE_PROFILE} to the scaffold',
                           SITE_PROFILE))
    plan.append(Action('clear-coordination',
                       'clear coordination [host] identity + [station] '
                       'block, re-render coordination.env'))
    for rec in PSWS_RECORDERS:
        plan.append(Action('psws-placeholders',
                           f'reset {rec} PSWS ids to template placeholders',
                           rec))
    for client, rid, cfg, env in (instances or []):
        if cfg and _exists(_p(root, str(cfg))):
            plan.append(Action('remove',
                               f'per-instance config: {client}@{rid} ({cfg})',
                               str(cfg)))
        if env and _exists(_p(root, str(env))):
            plan.append(Action('remove',
                               f'per-instance env: {client}@{rid}',
                               str(env)))

    # 3. accumulated data (skippable for debug captures)
    if not keep_data:
        for f in DATA_FILES:
            if _exists(_p(root, f)):
                plan.append(Action('remove', f'data: {f}', f))
        for tree in DATA_TREES:
            d = _p(root, tree)
            if _nonempty_dir(d):
                plan.append(Action('clear-tree',
                                   f'clear contents of {tree}/', tree))
        plan.append(Action('vacuum-journal',
                           'vacuum journald history (per-site logs)'))
    else:
        plan.append(Action('note', 'data kept (--keep-data)'))

    # 4. OS identity — last, so the gate/report steps above ran on a
    #    still-identified host.  The current SSH session survives; NEW
    #    connections need the clone's first boot to regenerate keys.
    if _exists(_p(root, PERSONALIZED_SENTINEL)):
        plan.append(Action('remove',
                           f'personalize sentinel: {PERSONALIZED_SENTINEL}',
                           PERSONALIZED_SENTINEL))
    if _exists(_p(root, MACHINE_ID)):
        plan.append(Action('truncate', f'truncate {MACHINE_ID}', MACHINE_ID))
    if _exists(_p(root, DBUS_MACHINE_ID)):
        plan.append(Action('remove', f'remove {DBUS_MACHINE_ID}',
                           DBUS_MACHINE_ID))
    keys = (ssh_host_keys if ssh_host_keys is not None
            else sorted(str(p) for p in _p(root, '/etc/ssh').glob('ssh_host_*')))
    for k in keys:
        plan.append(Action('remove', f'SSH host key: {k}', k))

    plan.append(Action('note',
                       'PHaRLAP/pyLAP stay baked in (image decision '
                       '2026-06-14) — do NOT remove /opt/pharlap*'))
    return plan
