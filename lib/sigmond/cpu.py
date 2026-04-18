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


# ---------------------------------------------------------------------------
# System capability gathering — read-only inspection of the host
# ---------------------------------------------------------------------------
#
# Motivation: sigmond's CPU affinity subsystem exists to keep radiod's
# USB3/FFT path uncontested on co-located hosts.  Per Phil Karn (ka9q-radio
# author), radiod needs only one physical core (HT sibling pair) per
# instance — not a full L3 island.  These capability functions surface the
# hardware layout so operators can reason about contention, and so the
# AffinityReport can flag governor/drop-in/runtime mismatches against the
# minimal-reservation plan.

PREFERRED_RADIOD_GOVERNORS = ('performance',)


@dataclass(frozen=True)
class CacheIsland:
    """A group of CPUs that share a given cache level."""
    level: int
    cache_type: str             # 'Unified' / 'Data' / 'Instruction'
    cpus: frozenset


@dataclass
class SystemCapabilities:
    """Read-only snapshot of host CPU topology and scheduling policy."""
    logical_cpus: int = 0
    physical_cores: list = field(default_factory=list)   # list[set]
    l2_islands: list = field(default_factory=list)       # list[CacheIsland]
    l3_islands: list = field(default_factory=list)       # list[CacheIsland]
    isolated_cpus: set = field(default_factory=set)
    cmdline_isolcpus: set = field(default_factory=set)
    cmdline_nohz_full: set = field(default_factory=set)
    cmdline_rcu_nocbs: set = field(default_factory=set)
    governors: dict = field(default_factory=dict)        # {cpu: governor}


def _read_text_or_none(path) -> Optional[str]:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def get_isolated_cpus() -> set:
    """CPUs kernel has isolated from the scheduler's default pool."""
    text = _read_text_or_none('/sys/devices/system/cpu/isolated')
    return parse_cpu_mask(text) if text else set()


def parse_cmdline_cpu_param(cmdline: str, key: str) -> set:
    """Return CPU set for a kernel cmdline key like ``isolcpus=0-3``.

    Tolerates leading flag tokens such as ``isolcpus=domain,managed_irq,0-3``
    — tokens that don't parse as CPU ranges are ignored (they're flags).
    """
    result: set = set()
    for token in cmdline.split():
        if not token.startswith(key + '='):
            continue
        value = token.split('=', 1)[1]
        for chunk in value.split(','):
            parsed = parse_cpu_mask(chunk.strip())
            if parsed:
                result.update(parsed)
    return result


def get_cmdline_params() -> dict:
    """Return cpu-policy kernel cmdline params as {name: set[int]}."""
    cmdline = _read_text_or_none('/proc/cmdline') or ''
    return {
        'isolcpus':  parse_cmdline_cpu_param(cmdline, 'isolcpus'),
        'nohz_full': parse_cmdline_cpu_param(cmdline, 'nohz_full'),
        'rcu_nocbs': parse_cmdline_cpu_param(cmdline, 'rcu_nocbs'),
    }


def get_cache_islands(level: int) -> list:
    """Unique cache islands at the given level (typically 2 or 3).

    Deduplicates across CPUs that share the same cache, returns sorted by
    lowest CPU in each island.
    """
    seen: dict = {}
    n = os.cpu_count() or 1
    for cpu in range(n):
        idx = 0
        while True:
            base = Path(f'/sys/devices/system/cpu/cpu{cpu}/cache/index{idx}')
            if not base.exists():
                break
            idx += 1
            lvl_raw = _read_text_or_none(base / 'level')
            try:
                lvl = int(lvl_raw)
            except (TypeError, ValueError):
                continue
            if lvl != level:
                continue
            ctype = _read_text_or_none(base / 'type') or ''
            cpu_list_raw = _read_text_or_none(base / 'shared_cpu_list')
            if not cpu_list_raw:
                continue
            cpus = frozenset(parse_cpu_mask(cpu_list_raw))
            if not cpus:
                continue
            key = (level, ctype, cpus)
            if key not in seen:
                seen[key] = CacheIsland(level=level, cache_type=ctype, cpus=cpus)
    return sorted(seen.values(), key=lambda isle: min(isle.cpus))


def get_governors() -> dict:
    """Return {cpu: governor} for every CPU exposing cpufreq."""
    out: dict = {}
    n = os.cpu_count() or 1
    for cpu in range(n):
        gov = _read_text_or_none(
            f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor')
        if gov:
            out[cpu] = gov
    return out


def gather_capabilities() -> SystemCapabilities:
    """Snapshot host CPU topology and scheduling policy."""
    cmdline = get_cmdline_params()
    return SystemCapabilities(
        logical_cpus=os.cpu_count() or 0,
        physical_cores=get_physical_cores(),
        l2_islands=get_cache_islands(2),
        l3_islands=get_cache_islands(3),
        isolated_cpus=get_isolated_cpus(),
        cmdline_isolcpus=cmdline['isolcpus'],
        cmdline_nohz_full=cmdline['nohz_full'],
        cmdline_rcu_nocbs=cmdline['rcu_nocbs'],
        governors=get_governors(),
    )


# ---------------------------------------------------------------------------
# Observed affinity state — runtime facts per managed unit
# ---------------------------------------------------------------------------

@dataclass
class UnitAffinity:
    """Observed affinity state for one systemd unit."""
    unit: str
    role: str                               # 'radiod' | 'other'
    main_pid: Optional[str] = None
    systemd_mask: set = field(default_factory=set)
    observed_mask: set = field(default_factory=set)
    thread_groups: dict = field(default_factory=dict)
    drop_in_present: bool = False
    foreign_drop_ins: list = field(default_factory=list)

    @property
    def mask_mismatch(self) -> bool:
        """True if systemd said one thing and the process is actually on another."""
        return (bool(self.systemd_mask)
                and bool(self.observed_mask)
                and self.systemd_mask != self.observed_mask)


def _smd_drop_in_path(unit: str) -> Path:
    return Path(f'/etc/systemd/system/{unit}.d/{SMD_AFFINITY_DROP_IN}')


def _foreign_drop_in_paths(unit: str) -> list:
    d = Path(f'/etc/systemd/system/{unit}.d')
    return [d / name for name in FOREIGN_AFFINITY_DROP_INS]


def _systemctl_main_pid(unit: str) -> Optional[str]:
    r = _run_capture(['systemctl', 'show', '-p', 'MainPID', '--value', unit])
    pid = r.stdout.strip()
    return pid if pid and pid != '0' else None


def _systemctl_cpu_affinity(unit: str) -> set:
    r = _run_capture(['systemctl', 'show', '-p', 'CPUAffinity', '--value', unit])
    return parse_cpu_mask(r.stdout.strip())


def observe_unit(unit: str, role: str) -> UnitAffinity:
    """Collect observed affinity facts for a single unit."""
    ua = UnitAffinity(unit=unit, role=role)
    ua.main_pid = _systemctl_main_pid(unit)
    ua.systemd_mask = _systemctl_cpu_affinity(unit)
    if ua.main_pid:
        proc = read_proc_cpus(ua.main_pid)
        ua.observed_mask = parse_cpu_mask(proc) if proc else set()
        ua.thread_groups = thread_affinity_groups(ua.main_pid)
    ua.drop_in_present = _smd_drop_in_path(unit).exists()
    ua.foreign_drop_ins = [str(p) for p in _foreign_drop_in_paths(unit) if p.exists()]
    return ua


# ---------------------------------------------------------------------------
# Runtime contention — non-radiod processes allowed on radiod cores
# ---------------------------------------------------------------------------

@dataclass
class ContendingProcess:
    """A process whose Cpus_allowed_list intersects the radiod reservation."""
    pid: str
    comm: str
    allowed: set
    overlap: set
    is_default: bool                # allowed spans every CPU on the host


def _read_proc_status_fields(pid: str) -> dict:
    out: dict = {}
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if ':' not in line:
                continue
            k, v = line.split(':', 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _is_kernel_thread(status: dict) -> bool:
    # Kernel threads are children of kthreadd (pid 2) or pid 2 itself.
    return status.get('PPid') in ('2', '0') or status.get('Pid') == '2'


def find_contending_processes(
    radiod_cpus: set,
    exclude_pids: Optional[set] = None,
) -> list:
    """Return ContendingProcess entries for non-kernel processes whose
    CPU affinity mask intersects ``radiod_cpus``.

    Runtime contention is a scheduling-permission signal, not a live
    placement observation — a process may be *allowed* on a radiod core
    without being scheduled there right now.  Processes with default
    (full-range) affinity are reported with ``is_default=True`` so callers
    can choose to summarize them rather than list each one.

    Cost: a single ``/proc`` walk (~hundreds of PIDs).  Call per report, not
    in a loop.
    """
    exclude = exclude_pids or set()
    results: list = []
    if not radiod_cpus:
        return results
    all_cpus = set(range(os.cpu_count() or 1))
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = entry.name
        if pid in exclude:
            continue
        status = _read_proc_status_fields(pid)
        if not status or _is_kernel_thread(status):
            continue
        mask_raw = status.get('Cpus_allowed_list', '')
        if not mask_raw:
            continue
        allowed = parse_cpu_mask(mask_raw)
        overlap = allowed & radiod_cpus
        if not overlap:
            continue
        comm = status.get('Name', '') or pid
        results.append(ContendingProcess(
            pid=pid,
            comm=comm,
            allowed=allowed,
            overlap=overlap,
            is_default=(allowed >= all_cpus),
        ))
    return results


# ---------------------------------------------------------------------------
# Full report — capabilities + plan + observed + warnings
# ---------------------------------------------------------------------------

@dataclass
class AffinityReport:
    """Everything an operator or a TUI screen needs in one object."""
    capabilities: SystemCapabilities
    plan: AffinityPlan
    units: list = field(default_factory=list)          # list[UnitAffinity]
    contention: list = field(default_factory=list)     # list[ContendingProcess]
    warnings: list = field(default_factory=list)       # list[str]

    @property
    def radiod_cpus(self) -> set:
        """Union of every CPU the plan dedicates to radiod."""
        out: set = set()
        for cpus in self.plan.radiod.values():
            out.update(cpus)
        return out

    @property
    def pinned_contention(self) -> list:
        """Contending processes that are explicitly pinned (not default mask)."""
        return [c for c in self.contention if not c.is_default]


def build_affinity_report(
    topology_cpu_affinity: Optional[dict] = None,
) -> AffinityReport:
    """Build a complete affinity report from live host state.

    Read-only: reads /sys, /proc, and shells out to ``systemctl show``.
    No mutations.
    """
    caps = gather_capabilities()
    plan = compute_affinity_plan(topology_cpu_affinity)

    units: list = []
    radiod_main_pids: set = set()
    for unit in plan.radiod.keys():
        ua = observe_unit(unit, role='radiod')
        units.append(ua)
        if ua.main_pid:
            radiod_main_pids.add(ua.main_pid)
    for unit in AFFINITY_UNITS:
        # Template units (e.g. wd-decode@.service) show the template's
        # defaults here; per-instance observation happens elsewhere.
        units.append(observe_unit(unit, role='other'))

    radiod_cpus_set: set = set()
    for cpus in plan.radiod.values():
        radiod_cpus_set.update(cpus)

    contention = find_contending_processes(
        radiod_cpus_set,
        exclude_pids=radiod_main_pids,
    )

    warnings: list = []

    for cpu in sorted(radiod_cpus_set):
        gov = caps.governors.get(cpu)
        if gov and gov not in PREFERRED_RADIOD_GOVERNORS:
            warnings.append(
                f"governor {gov!r} on radiod cpu{cpu} — expected 'performance' "
                "for uncontested USB3/FFT throughput"
            )

    isol = caps.cmdline_isolcpus or caps.isolated_cpus
    if isol and radiod_cpus_set and not radiod_cpus_set.issubset(isol):
        outside = sorted(radiod_cpus_set - isol)
        warnings.append(
            f"radiod plan uses cpus outside isolated pool: {outside} "
            f"(isolated={sorted(isol)})"
        )

    for ua in units:
        for path in ua.foreign_drop_ins:
            warnings.append(
                f"foreign drop-in on {ua.unit}: {path} "
                "— smd will remove on --apply"
            )
        if ua.role == 'radiod' and ua.main_pid and not ua.drop_in_present:
            warnings.append(
                f"{ua.unit} running without smd drop-in — affinity not enforced"
            )
        if ua.mask_mismatch:
            warnings.append(
                f"{ua.unit}: observed cpus {sorted(ua.observed_mask)} "
                f"differ from systemd CPUAffinity {sorted(ua.systemd_mask)} "
                "(sched_setaffinity override — AllowedCPUs cgroup ceiling defeats this)"
            )

    pinned = [c for c in contention if not c.is_default]
    if pinned:
        sample = ', '.join(f'{c.comm}({c.pid})' for c in pinned[:5])
        more = f' (+{len(pinned) - 5} more)' if len(pinned) > 5 else ''
        warnings.append(
            f"{len(pinned)} pinned process(es) overlap radiod cores: {sample}{more}"
        )

    return AffinityReport(
        capabilities=caps,
        plan=plan,
        units=units,
        contention=contention,
        warnings=warnings,
    )
