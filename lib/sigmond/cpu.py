"""CPU affinity and frequency management for the HamSCI suite.

Extracted from bin/smd so that both the CLI and the TUI can reuse the
same logic.  Pure functions (parsing, topology reading, plan computation)
have no side effects.  Functions that interact with systemd or the
filesystem accept a ``run_cmd`` callback for testability.

Architecture:
  - Radiod gets dedicated physical cores (one per instance, both HT siblings).
  - All other managed services share the remaining cores.
  - Enforcement uses CPUAffinity= (initial) + AllowedCPUs= (cgroup ceiling).
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Drop-in filename written by smd.
SMD_AFFINITY_DROP_IN = 'smd-cpu-affinity.conf'

# Drop-in filenames installed by other tools that smd supersedes.
FOREIGN_AFFINITY_DROP_INS = [
    'cpu-affinity.conf',          # hf-timestd setup-cpu-affinity.sh
    '99-wdctl-cpu-affinity.conf', # wd-ctl apply
]

# hf-timestd units that watch radiod for reinstalls and re-apply its drop-in.
HFTIMESTD_AFFINITY_UNITS = [
    'timestd-radiod-affinity.path',
    'timestd-radiod-affinity.service',
]

# Service template -> cpu group.  radiod instances handled separately.
AFFINITY_UNITS = {
    # wsprdaemon group
    'wd-decode@.service':                'other',
    'wd-ka9q-record@.service':           'other',
    'wd-kiwi-record@.service':           'other',
    'wd-post@.service':                  'other',
    'wd-upload-wsprnet@.service':        'other',
    'wd-upload-wsprdaemon@.service':     'other',
    'wd-ka9q-web@.service':              'other',
    'wd-spool-clean.service':            'other',
    # hf-timestd / grape group
    'timestd-core-recorder.service':     'other',
    'timestd-fusion.service':            'other',
    'timestd-l2-calibration.service':    'other',
    'timestd-metrology@.service':        'other',
    'timestd-physics.service':           'other',
    'timestd-vtec.service':              'other',
    'timestd-iono-reanalysis.service':   'other',
    'timestd-radiod-monitor.service':    'other',
    'timestd-chrony-monitor.service':    'other',
    'timestd-pipeline-watchdog.service': 'other',
    'timestd-prune.service':             'other',
    'timestd-raw-cleanup.service':       'other',
    'grape-daily.service':               'other',
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class AffinityPlan:
    """Result of compute_affinity_plan()."""
    radiod: dict = field(default_factory=dict)   # {unit_name: cpu_set}
    other_cpus: set = field(default_factory=set)  # CPUs for non-radiod services
    physical_cores: list = field(default_factory=list)  # list[set], one per core


@dataclass
class ThreadGroup:
    """A group of threads sharing the same CPU affinity mask."""
    mask: str
    cpus: set
    threads: list  # list[(tid, thread_name)]


# ---------------------------------------------------------------------------
# Pure functions (no side effects)
# ---------------------------------------------------------------------------

def cpu_list_str(cpus: set) -> str:
    """Return sorted space-separated CPU list, e.g. '2 3 12 13 14 15'."""
    return ' '.join(str(c) for c in sorted(cpus))


def parse_cpu_mask(s: str) -> set:
    """Parse a Cpus_allowed_list or systemd CPUAffinity string into a set.

    Handles space/comma-separated tokens and hyphenated ranges, e.g.
    '0-3 8 12-15'  ->  {0, 1, 2, 3, 8, 12, 13, 14, 15}
    """
    parts: set = set()
    for token in s.replace(',', ' ').split():
        if '-' in token:
            a, b = token.split('-', 1)
            try:
                parts.update(range(int(a), int(b) + 1))
            except ValueError:
                pass
        else:
            try:
                parts.add(int(token))
            except ValueError:
                pass
    return parts


def get_physical_cores() -> list[set]:
    """Return an ordered list of CPU sets, one set per physical core.

    Reads /sys/devices/system/cpu/cpu*/topology/thread_siblings_list so that
    HT sibling pairs are grouped.  Ordered by lowest logical CPU in each group.
    Falls back to treating each logical CPU as its own core (no HT, or VM).
    """
    seen: set = set()
    core_map: dict = {}
    n = os.cpu_count() or 1
    for cpu in range(n):
        p = Path(f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list')
        try:
            siblings = parse_cpu_mask(p.read_text().strip())
        except OSError:
            siblings = {cpu}
        key = frozenset(siblings)
        if key not in seen:
            seen.add(key)
            core_map[min(siblings)] = siblings
    return [core_map[k] for k in sorted(core_map)]


def read_proc_cpus(pid_or_tid: str) -> Optional[str]:
    """Return Cpus_allowed_list string for a PID or TID, or None on error."""
    try:
        text = Path(f'/proc/{pid_or_tid}/status').read_text()
        line = next((l for l in text.splitlines()
                     if l.startswith('Cpus_allowed_list:')), None)
        return line.split(':', 1)[1].strip() if line else None
    except OSError:
        return None


def thread_affinity_groups(pid: str) -> dict:
    """Return {cpu_mask: [(tid, thread_name), ...]} for every thread in pid."""
    groups: dict = {}
    task_dir = Path(f'/proc/{pid}/task')
    if not task_dir.exists():
        return groups
    for td in task_dir.iterdir():
        tid = td.name
        try:
            text = (td / 'status').read_text()
        except OSError:
            continue
        name_line = next((l for l in text.splitlines() if l.startswith('Name:')), '')
        tname = name_line.split(':', 1)[1].strip() if name_line else tid
        mask_line = next((l for l in text.splitlines()
                          if l.startswith('Cpus_allowed_list:')), '')
        mask = mask_line.split(':', 1)[1].strip() if mask_line else '?'
        groups.setdefault(mask, []).append((tid, tname))
    return groups


# ---------------------------------------------------------------------------
# Systemd queries (shell out but read-only)
# ---------------------------------------------------------------------------

def _run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command and capture output (internal helper)."""
    return subprocess.run(cmd, capture_output=True, text=True)


def get_radiod_instances() -> list[str]:
    """Return sorted list of radiod@*.service unit names known to systemd."""
    r = _run_capture(['systemctl', 'list-units', '--no-legend', '--no-pager',
                      '--all', '--output=json', 'radiod@*.service'])
    try:
        return sorted(u.get('unit', '') for u in json.loads(r.stdout)
                      if u.get('unit', ''))
    except Exception:
        return []


def get_radiod_cpus() -> set:
    """Return the set of CPU numbers assigned to radiod via systemd CPUAffinity."""
    r = _run_capture(['systemctl', 'list-units', '--no-legend', '--no-pager',
                      '--all', '--output=json', 'radiod@*.service'])
    try:
        units = json.loads(r.stdout)
    except Exception:
        units = []

    cpus: set = set()
    for u in units:
        name = u.get('unit', '')
        if not name:
            continue
        r2 = _run_capture(['systemctl', 'show', '-p', 'CPUAffinity', '--value', name])
        mask = r2.stdout.strip()
        if mask:
            cpus.update(parse_cpu_mask(mask))
    return cpus


# ---------------------------------------------------------------------------
# Plan computation
# ---------------------------------------------------------------------------

def compute_affinity_plan(
    topology_cpu_affinity: Optional[dict] = None,
) -> AffinityPlan:
    """Compute the CPU affinity plan from hardware topology and running radiod instances.

    Args:
        topology_cpu_affinity: The ``[cpu_affinity]`` section from topology.toml,
            e.g. ``{'radiod_cpus': '', 'other_cpus': ''}``.  Empty strings or
            None means auto-compute from hardware.

    Returns:
        AffinityPlan with radiod per-instance assignments and other_cpus pool.
    """
    cores = get_physical_cores()
    instances = get_radiod_instances()
    ca = topology_cpu_affinity or {}

    radiod_plan: dict = {}
    for i, unit in enumerate(instances):
        if i < len(cores):
            radiod_plan[unit] = cores[i]
        else:
            # More instances than cores — share the last one.
            radiod_plan[unit] = cores[-1] if cores else set()

    radiod_all: set = set()
    for cpus in radiod_plan.values():
        radiod_all.update(cpus)

    other_spec = ca.get('other_cpus', '').strip()
    if other_spec:
        other_cpus = parse_cpu_mask(other_spec)
    else:
        other_cpus = set(range(os.cpu_count() or 16)) - radiod_all

    return AffinityPlan(
        radiod=radiod_plan,
        other_cpus=other_cpus,
        physical_cores=cores,
    )


# ---------------------------------------------------------------------------
# Mutating operations (write drop-ins, remove foreign, apply runtime)
# ---------------------------------------------------------------------------

def render_drop_in(cpus: set, label: str) -> str:
    """Render a systemd drop-in file for CPU affinity."""
    cpu_str = cpu_list_str(cpus)
    return textwrap.dedent(f"""\
        # CPU affinity managed by smd — do not edit manually.
        # Role: {label}
        # Regenerate: sudo smd diag cpu-affinity --apply
        [Service]
        CPUAffinity=
        CPUAffinity={cpu_str}
        AllowedCPUs=
        AllowedCPUs={cpu_str}
    """)
