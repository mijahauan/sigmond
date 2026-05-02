"""Topology registry loader.

Topology answers 'what components are installed on this host and
does smd manage them?'.  It is deliberately separate from the
coordination config, which answers 'how do the components talk to
each other?'.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .paths import TOPOLOGY_PATH
from .ui import warn


@dataclass
class Component:
    name: str
    enabled: bool = False
    managed: bool = True
    description: str = ""
    # Version policy for `smd update`:
    #   "latest"  — always pull the newest commit (default)
    #   "ignore"  — skip this component during updates
    #   any other — a specific git ref (commit sha, branch, or tag) to pin to
    version: str = "latest"
    rac_id: str = ""        # frpc proxy name, e.g. "AI6VN-0" (RAC tunnel)
    rac_number: int = -1    # integer assigned by RAC administrator


# CPU affinity and frequency defaults.  See CLAUDE.md + the cpu-affinity
# design memory — empty strings mean 'auto-compute from hardware'.
_DEFAULT_CPU_AFFINITY = {
    'radiod_cpus': '',    # e.g. "0-1"
    'other_cpus':  '',    # e.g. "2-15"
}
_DEFAULT_CPU_FREQ = {
    'radiod_max_mhz': 3200,
    'other_max_mhz':  1400,
}


@dataclass
class Topology:
    client_dir: Path
    smd_bin: Path
    components: dict = field(default_factory=dict)
    cpu_affinity: dict = field(default_factory=lambda: dict(_DEFAULT_CPU_AFFINITY))
    cpu_freq: dict = field(default_factory=lambda: dict(_DEFAULT_CPU_FREQ))

    def enabled_components(self, only: Optional[list] = None) -> list:
        names = [n for n, c in self.components.items() if c.enabled]
        if only:
            names = [n for n in names if n in only]
        return sorted(names)

    def is_enabled(self, name: str) -> bool:
        c = self.components.get(name)
        return bool(c and c.enabled)

    def is_managed(self, name: str) -> bool:
        c = self.components.get(name)
        return bool(c and c.enabled and c.managed)


_DEFAULT_COMPONENTS = {
    'ka9q-radio':         Component('ka9q-radio',         enabled=False, managed=True,  description='ka9q-radio SDR daemon'),
    'hf-timestd':         Component('hf-timestd',         enabled=False, managed=True,  description='HF time-standard analyzer (WWV/WWVH/CHU/BPM)'),
    'psk-recorder':       Component('psk-recorder',       enabled=False, managed=True,  description='FT4/FT8 spot recorder for PSKReporter'),
    'wspr-recorder':      Component('wspr-recorder',      enabled=False, managed=True,  description='WSPR/FST4W audio recorder (period-aligned WAVs)'),
    'wsprdaemon-client':  Component('wsprdaemon-client',  enabled=False, managed=True,  description='WSPR decoder + poster + uploader'),
    'ka9q-web':           Component('ka9q-web',           enabled=False, managed=True,  description='ka9q-web radiod status UI'),
}

_DEFAULT_CLIENT_DIR = Path('/opt/git/sigmond/wsprdaemon-client')
_DEFAULT_SMD_BIN    = Path('/usr/local/sbin/smd')


def load_topology(path: Path = TOPOLOGY_PATH,
                  client_dir_override: Optional[str] = None) -> Topology:
    """Load topology.toml, or return defaults when the file is absent."""
    import tomllib

    client_dir = _DEFAULT_CLIENT_DIR
    smd_bin    = _DEFAULT_SMD_BIN
    components = {n: Component(**c.__dict__) for n, c in _DEFAULT_COMPONENTS.items()}

    if client_dir_override:
        client_dir = Path(client_dir_override)

    if not path.exists():
        warn(f'No topology file at {path} — using defaults')
        return Topology(client_dir=client_dir, smd_bin=smd_bin, components=components)

    with open(path, 'rb') as f:
        raw = tomllib.load(f)

    sig = raw.get('sigmond', {})
    if 'wsprdaemon_client' in sig:
        client_dir = Path(sig['wsprdaemon_client'])
    if client_dir_override:
        client_dir = Path(client_dir_override)
    if 'smd_bin' in sig:
        smd_bin = Path(sig['smd_bin'])

    # Legacy topology name → canonical catalog name (mirrors catalog topology_alias).
    _RENAMES = {'radiod': 'ka9q-radio'}

    for name, cfg in raw.get('component', {}).items():
        canonical = _RENAMES.get(name, name)
        rac_number = -1
        try:
            rac_number = int(cfg.get('rac_number', -1))
        except (TypeError, ValueError):
            pass
        raw_ver = str(cfg.get('version', 'latest') or 'latest').strip()
        components[canonical] = Component(
            name=canonical,
            enabled=cfg.get('enabled', False),
            managed=cfg.get('managed', True),
            description=cfg.get('description', ''),
            version=raw_ver,
            rac_id=str(cfg.get('rac_id', '') or '').strip(),
            rac_number=rac_number,
        )

    cpu_affinity = dict(_DEFAULT_CPU_AFFINITY)
    ca = raw.get('cpu_affinity', {})
    if 'radiod_cpus' in ca:
        cpu_affinity['radiod_cpus'] = str(ca['radiod_cpus'])
    if 'other_cpus' in ca:
        cpu_affinity['other_cpus'] = str(ca['other_cpus'])

    cpu_freq = dict(_DEFAULT_CPU_FREQ)
    cf = raw.get('cpu_freq', {})
    if 'radiod_max_mhz' in cf:
        try:
            cpu_freq['radiod_max_mhz'] = int(cf['radiod_max_mhz'])
        except (TypeError, ValueError):
            warn(f"topology cpu_freq.radiod_max_mhz not an int: {cf['radiod_max_mhz']!r}")
    if 'other_max_mhz' in cf:
        try:
            cpu_freq['other_max_mhz'] = int(cf['other_max_mhz'])
        except (TypeError, ValueError):
            warn(f"topology cpu_freq.other_max_mhz not an int: {cf['other_max_mhz']!r}")

    return Topology(
        client_dir=client_dir,
        smd_bin=smd_bin,
        components=components,
        cpu_affinity=cpu_affinity,
        cpu_freq=cpu_freq,
    )


def enabled_components(topology: Topology, only: Optional[list] = None) -> list:
    """Backwards-compatible free function."""
    return topology.enabled_components(only)


# ---------------------------------------------------------------------------
# Mutating writers — line-based to preserve operator comments + ordering.
# ---------------------------------------------------------------------------


def set_component_enabled(name: str,
                          enabled: bool,
                          path: Path = TOPOLOGY_PATH,
                          description: Optional[str] = None) -> bool:
    """Set [component.<name>].enabled = true|false in topology.toml.

    Idempotent — returns True only if the file actually changed.

    Behavior:
      - Existing section with `enabled = ...` line: rewrite that line.
      - Existing section without `enabled = ...`: insert one after the header.
      - No section at all: append a new [component.<name>] block at EOF.
    """
    val = 'true' if enabled else 'false'
    target_header = f'[component.{name}]'

    if path.exists():
        original = path.read_text()
        lines = original.splitlines()
    else:
        original = ''
        lines = []

    new_lines: list[str] = []
    in_section = False
    enabled_handled = False
    section_found = False
    section_header_idx = -1

    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith('[') and stripped.endswith(']')

        if is_header:
            if in_section and not enabled_handled:
                new_lines.insert(section_header_idx + 1, f'enabled = {val}')
                enabled_handled = True
            in_section = (stripped == target_header)
            if in_section:
                section_found = True
                section_header_idx = len(new_lines)

        if in_section and stripped.startswith('enabled') and '=' in stripped:
            indent = line[:len(line) - len(line.lstrip())]
            new_lines.append(f'{indent}enabled = {val}')
            enabled_handled = True
            continue

        new_lines.append(line)

    if in_section and not enabled_handled:
        new_lines.insert(section_header_idx + 1, f'enabled = {val}')
        enabled_handled = True
        section_found = True

    if not section_found:
        while new_lines and new_lines[-1].strip() == '':
            new_lines.pop()
        if new_lines:
            new_lines.append('')
        new_lines.append(target_header)
        new_lines.append(f'enabled = {val}')
        if description:
            new_lines.append(f'description = "{description}"')

    new_text = '\n'.join(new_lines)
    if not new_text.endswith('\n'):
        new_text += '\n'

    if new_text == original:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text)
    return True


def remove_component(name: str, path: Path = TOPOLOGY_PATH) -> bool:
    """Remove the [component.<name>] section from topology.toml.

    Idempotent — returns False if no such section was present.
    """
    if not path.exists():
        return False

    original = path.read_text()
    lines = original.splitlines()
    target_header = f'[component.{name}]'

    new_lines: list[str] = []
    skipping = False
    found = False

    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith('[') and stripped.endswith(']')

        if skipping:
            if is_header and stripped != target_header:
                skipping = False
                new_lines.append(line)
            continue

        if stripped == target_header:
            skipping = True
            found = True
            while new_lines and new_lines[-1].strip() == '':
                new_lines.pop()
            continue

        new_lines.append(line)

    if not found:
        return False

    new_text = '\n'.join(new_lines).rstrip() + '\n'
    path.write_text(new_text)
    return True
