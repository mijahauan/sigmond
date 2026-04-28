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
    # sigmond infra group
    'wd-rac.service':       'other',
    'igmp-querier.service': 'other',
    'gpsdo-monitor.service':    'other',
    'ka9q-web.service':         'other',
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

    If the OS reports every CPU as its own sole sibling (common under hypervisors
    that don't pass through SMT topology), falls back to assuming consecutive
    pairs: {0,1}, {2,3}, {4,5}, … so that radiod instances are each assigned
    two adjacent CPUs rather than one.
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

    cores = [core_map[k] for k in sorted(core_map)]

    # If every entry is a singleton the hypervisor isn't exposing HT topology.
    # Assume consecutive pairs so each radiod instance gets two CPUs.
    if all(len(c) == 1 for c in cores) and len(cores) > 1:
        cpu_list = sorted(cpu for c in cores for cpu in c)
        paired: list[set] = []
        for i in range(0, len(cpu_list), 2):
            if i + 1 < len(cpu_list):
                paired.append({cpu_list[i], cpu_list[i + 1]})
            else:
                paired.append({cpu_list[i]})
        return paired

    return cores


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
    """Return sorted list of radiod@*.service unit names.

    The authoritative source is /etc/radio/radiod@*.conf (matching the
    ka9q-radio shim deploy.toml conf_dir).  Instances with a conf file
    exist regardless of whether they have ever been started.  Drop-in dirs
    are NOT used as a source because they are artifacts of past smd runs and
    will persist after an instance is removed, creating ghost entries.

    systemctl list-units is checked as a secondary source for units that
    are currently loaded in systemd (e.g. active or recently stopped) but
    whose conf file may have been removed mid-session.
    """
    found: set[str] = set()

    # Source 1: conf files in /etc/radio/ — authoritative configured instances
    conf_dir = Path('/etc/radio')
    if conf_dir.exists():
        for cf in conf_dir.glob('radiod@*.conf'):
            if cf.is_file() and not cf.is_symlink():
                instance = cf.stem[len('radiod@'):]
                if instance:
                    found.add(f'radiod@{instance}.service')

    # Source 2: loaded units (active, failed, inactive-but-loaded)
    r = _run_capture(['systemctl', 'list-units', '--no-legend', '--no-pager',
                      '--all', '--output=json', 'radiod@*.service'])
    try:
        for u in json.loads(r.stdout):
            name = u.get('unit', '')
            if name:
                found.add(name)
    except Exception:
        pass

    return sorted(found)


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
    # Raw Cpus_allowed_list string from /proc (e.g. '0-1' or '2-7,10-15').
    # Preserved separately because thread_groups keys use this exact format,
    # so display/comparison code can match thread masks against the process
    # mask without normalizing through parse_cpu_mask + cpu_list_str.
    observed_mask_raw: str = ""
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


def _template_name(unit: str) -> Optional[str]:
    """Return template name for a templated instance, or None for concrete units.

    e.g. 'wd-decode@KA9Q_0-10.service' → 'wd-decode@.service'
    """
    if '@' not in unit:
        return None
    at_idx = unit.index('@')
    dot_idx = unit.rfind('.')
    if dot_idx <= at_idx:
        return None
    return unit[:at_idx + 1] + '.' + unit[dot_idx + 1:]


def _read_drop_in_cpus(path: Path) -> set:
    """Parse the effective CPUAffinity from an smd-cpu-affinity.conf file.

    Returns the last non-empty CPUAffinity= value (systemd uses the last
    assignment, and the file always resets then sets).
    """
    try:
        for line in reversed(path.read_text().splitlines()):
            line = line.strip()
            if line.startswith('CPUAffinity=') and line != 'CPUAffinity=':
                return parse_cpu_mask(line.split('=', 1)[1])
    except OSError:
        pass
    return set()


def _find_smd_drop_in(unit: str) -> Optional[Path]:
    """Return the smd drop-in Path that applies to this unit, or None.

    Checks the per-instance drop-in first, then the template-level drop-in.
    Template-level drop-ins (e.g. wd-decode@.service.d/) are written by
    ``smd diag cpu-affinity --apply`` and apply to all instances of that template.
    """
    inst_path = _smd_drop_in_path(unit)
    if inst_path.exists():
        return inst_path
    tmpl = _template_name(unit)
    if tmpl:
        tmpl_path = _smd_drop_in_path(tmpl)
        if tmpl_path.exists():
            return tmpl_path
    return None


def _foreign_drop_in_paths(unit: str) -> list:
    """Return existing foreign drop-in Paths for this unit and its template."""
    found = []
    for name in FOREIGN_AFFINITY_DROP_INS:
        p = Path(f'/etc/systemd/system/{unit}.d/{name}')
        if p.exists():
            found.append(p)
    tmpl = _template_name(unit)
    if tmpl:
        for name in FOREIGN_AFFINITY_DROP_INS:
            p = Path(f'/etc/systemd/system/{tmpl}.d/{name}')
            if p.exists() and p not in found:
                found.append(p)
    return found


def _systemctl_main_pid(unit: str) -> Optional[str]:
    r = _run_capture(['systemctl', 'show', '-p', 'MainPID', '--value', unit])
    pid = r.stdout.strip()
    return pid if pid and pid != '0' else None


def _systemctl_cpu_affinity(unit: str) -> set:
    r = _run_capture(['systemctl', 'show', '-p', 'CPUAffinity', '--value', unit])
    return parse_cpu_mask(r.stdout.strip())


def expand_template_instances(template: str) -> list:
    """Expand a systemd template unit name to the concrete instances
    known to systemd.  Non-template names are returned as a single-entry
    list containing the name unchanged.

    Prefer expand_all_template_instances() for bulk expansion — this
    single-template version is kept for call sites that need just one.
    """
    if '@.' not in template:
        return [template]
    pattern = template.replace('@.service', '@*.service')
    r = _run_capture(['systemctl', 'list-units', '--no-legend', '--no-pager',
                      '--all', '--output=json', pattern])
    try:
        units = json.loads(r.stdout)
    except Exception:
        return []
    return sorted(u.get('unit', '') for u in units if u.get('unit', ''))


def _expand_all_templates_bulk(templates: dict) -> dict[str, list[str]]:
    """Expand all template unit names to concrete instances in ONE systemctl call.

    Returns a dict mapping each template name to its list of concrete instances.
    Non-template names map to [name] directly without a subprocess call.
    """
    non_templates = {t: [t] for t in templates if '@.' not in t}
    tmpl_list = [t for t in templates if '@.' in t]
    if not tmpl_list:
        return non_templates

    patterns = [t.replace('@.service', '@*.service') for t in tmpl_list]
    r = _run_capture(
        ['systemctl', 'list-units', '--no-legend', '--no-pager',
         '--all', '--output=json'] + patterns
    )
    all_units: list = []
    try:
        all_units = json.loads(r.stdout)
    except Exception:
        pass

    # Map each returned unit back to the template it came from.
    result: dict[str, list[str]] = {t: [] for t in tmpl_list}
    result.update(non_templates)
    for u in all_units:
        name = u.get('unit', '')
        if not name:
            continue
        # Match e.g. 'wd-decode@foo.service' back to 'wd-decode@.service'
        at_idx = name.find('@')
        dot_idx = name.rfind('.')
        if at_idx >= 0 and dot_idx > at_idx:
            tmpl_key = name[:at_idx + 1] + '.' + name[dot_idx + 1:]
            if tmpl_key in result:
                result[tmpl_key].append(name)
    for key in result:
        result[key] = sorted(result[key]) if result[key] != [key] else result[key]
    return result


def observe_unit(unit: str, role: str) -> UnitAffinity:
    """Collect observed affinity facts for a single unit."""
    ua = UnitAffinity(unit=unit, role=role)
    ua.main_pid = _systemctl_main_pid(unit)
    ua.systemd_mask = _systemctl_cpu_affinity(unit)
    if ua.main_pid:
        proc = read_proc_cpus(ua.main_pid)
        ua.observed_mask_raw = proc or ""
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


def _proc_unit_pid_map() -> dict[str, str]:
    """Walk /proc once and return {unit_name: main_pid_str}.

    Reads each process's cgroup file to extract its systemd unit name.
    The main PID is the smallest PID in the unit's cgroup — the ExecStart
    process is always spawned first and thus has the lowest PID.

    ~40-60 ms on a busy system; avoids a slow ``systemctl show`` call.
    """
    unit_pids: dict[str, list[int]] = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / 'cgroup').read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if not line.startswith('0::'):
                continue
            # cgroup path: /system.slice/system-wd\x2ddecode.slice/wd-decode@foo.service
            unit = line[3:].rsplit('/', 1)[-1]
            if unit.endswith('.service'):
                unit_pids.setdefault(unit, []).append(int(entry.name))
            break
    return {unit: str(min(pids)) for unit, pids in unit_pids.items()}


def _batch_systemctl_show(unit_names: list[str]) -> dict[str, dict[str, str]]:
    """Fetch MainPID and CPUAffinity for all units in a single systemctl call.

    Returns {unit_name: {'MainPID': ..., 'CPUAffinity': ...}}.
    """
    if not unit_names:
        return {}
    r = _run_capture(
        ['systemctl', 'show', '-p', 'MainPID,CPUAffinity'] + unit_names
    )
    result: dict[str, dict[str, str]] = {}
    current_unit_idx = 0
    current_fields: dict[str, str] = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            # Blank line separates units in multi-unit output.
            if current_unit_idx < len(unit_names):
                result[unit_names[current_unit_idx]] = current_fields
                current_unit_idx += 1
                current_fields = {}
            continue
        if '=' in line:
            k, _, v = line.partition('=')
            current_fields[k.strip()] = v.strip()
    if current_fields and current_unit_idx < len(unit_names):
        result[unit_names[current_unit_idx]] = current_fields
    return result


def build_affinity_report(
    topology_cpu_affinity: Optional[dict] = None,
) -> AffinityReport:
    """Build a complete affinity report from live host state.

    Read-only: reads /sys, /proc, and shells out to ``systemctl show``.
    No mutations.  Uses batched systemctl calls to minimise subprocess count.
    """
    caps = gather_capabilities()
    plan = compute_affinity_plan(topology_cpu_affinity)

    # Expand all AFFINITY_UNITS templates in a single systemctl list-units call.
    expanded = _expand_all_templates_bulk(AFFINITY_UNITS)

    # Collect all unit names we need to observe.
    radiod_unit_names = list(plan.radiod.keys())
    other_unit_names: list[str] = []
    for units_for_template in expanded.values():
        other_unit_names.extend(units_for_template)

    all_unit_names = radiod_unit_names + other_unit_names

    # Build unit→PID map from /proc in one fast walk (~50 ms).
    # CPUAffinity is read directly from drop-in files — no systemctl show needed.
    proc_pid_map = _proc_unit_pid_map()

    units: list = []
    radiod_main_pids: set = set()

    def _observe_unit_fast(unit: str, role: str) -> UnitAffinity:
        ua = UnitAffinity(unit=unit, role=role)
        ua.main_pid = proc_pid_map.get(unit)
        # Read CPUAffinity directly from the drop-in file (instance or template level).
        drop_in = _find_smd_drop_in(unit)
        if drop_in is not None:
            ua.systemd_mask = _read_drop_in_cpus(drop_in)
            ua.drop_in_present = True
        if ua.main_pid:
            proc = read_proc_cpus(ua.main_pid)
            ua.observed_mask_raw = proc or ""
            ua.observed_mask = parse_cpu_mask(proc) if proc else set()
            ua.thread_groups = thread_affinity_groups(ua.main_pid)
        ua.foreign_drop_ins = [str(p) for p in _foreign_drop_in_paths(unit)]
        return ua

    for unit in radiod_unit_names:
        ua = _observe_unit_fast(unit, role='radiod')
        units.append(ua)
        if ua.main_pid:
            radiod_main_pids.add(ua.main_pid)
    for unit in other_unit_names:
        units.append(_observe_unit_fast(unit, role='other'))

    radiod_cpus_set: set = set()
    for cpus in plan.radiod.values():
        radiod_cpus_set.update(cpus)

    # Only scan /proc for contention when at least one radiod instance is
    # actually running — if none are active the walk is wasted work.
    if radiod_main_pids:
        contention = find_contending_processes(
            radiod_cpus_set,
            exclude_pids=radiod_main_pids,
        )
    else:
        contention = []

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

    # Foreign drop-in warnings, deduplicated by path.
    # Skip entirely when smd's own drop-in (smd-cpu-affinity.conf, which sorts
    # after 99-wdctl-cpu-affinity.conf) is already in place on every affected
    # unit — CPU affinity is correct; the leftover file is harmless dead weight
    # that will be removed on the next smd apply.
    _foreign_paths: dict = {}          # path -> list[UnitAffinity]
    for ua in units:
        for path in ua.foreign_drop_ins:
            _foreign_paths.setdefault(path, []).append(ua)
    for path, affected_uas in sorted(_foreign_paths.items()):
        if all(ua.drop_in_present for ua in affected_uas):
            continue  # smd drop-in overrides this; no action needed
        affected_names = [ua.unit for ua in affected_uas]
        if len(affected_names) == 1:
            warnings.append(
                f"foreign drop-in on {affected_names[0]}: {path} "
                "— run: sudo smd apply"
            )
        else:
            dir_name = Path(path).parent.name
            tmpl = dir_name[:-2] if dir_name.endswith('.d') else dir_name
            warnings.append(
                f"foreign drop-in affects {len(affected_names)} {tmpl} instances: "
                f"{path} — run: sudo smd apply"
            )

    for ua in units:
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


# ---------------------------------------------------------------------------
# Serialization — JSON-safe dict for smd diag cpu-affinity --json and
# future consumers.  Sets become sorted lists; frozensets in CacheIsland
# are flattened.  Thread groups are summarized by count rather than dumped
# in full — the text renderer is the right tool for per-thread detail.
# ---------------------------------------------------------------------------

def _island_to_dict(isle: CacheIsland) -> dict:
    return {
        'level': isle.level,
        'cache_type': isle.cache_type,
        'cpus': sorted(isle.cpus),
    }


def affinity_report_to_dict(report: AffinityReport) -> dict:
    """Render AffinityReport as a JSON-safe dict."""
    caps = report.capabilities
    return {
        'capabilities': {
            'logical_cpus':      caps.logical_cpus,
            'physical_cores':    [sorted(c) for c in caps.physical_cores],
            'l2_islands':        [_island_to_dict(i) for i in caps.l2_islands],
            'l3_islands':        [_island_to_dict(i) for i in caps.l3_islands],
            'isolated_cpus':     sorted(caps.isolated_cpus),
            'cmdline_isolcpus':  sorted(caps.cmdline_isolcpus),
            'cmdline_nohz_full': sorted(caps.cmdline_nohz_full),
            'cmdline_rcu_nocbs': sorted(caps.cmdline_rcu_nocbs),
            'governors':         {str(cpu): gov
                                  for cpu, gov in sorted(caps.governors.items())},
        },
        'plan': {
            'radiod':         {unit: sorted(cpus)
                               for unit, cpus in report.plan.radiod.items()},
            'other_cpus':     sorted(report.plan.other_cpus),
            'physical_cores': [sorted(c) for c in report.plan.physical_cores],
        },
        'radiod_cpus': sorted(report.radiod_cpus),
        'units': [
            {
                'unit':               u.unit,
                'role':               u.role,
                'main_pid':           u.main_pid,
                'systemd_mask':       sorted(u.systemd_mask),
                'observed_mask':      sorted(u.observed_mask),
                'mask_mismatch':      u.mask_mismatch,
                'drop_in_present':    u.drop_in_present,
                'foreign_drop_ins':   list(u.foreign_drop_ins),
                'thread_group_count': len(u.thread_groups),
            }
            for u in report.units
        ],
        'contention': [
            {
                'pid':        c.pid,
                'comm':       c.comm,
                'allowed':    sorted(c.allowed),
                'overlap':    sorted(c.overlap),
                'is_default': c.is_default,
            }
            for c in report.contention
        ],
        'warnings': list(report.warnings),
    }
