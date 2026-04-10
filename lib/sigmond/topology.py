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


@dataclass
class Topology:
    client_dir: Path
    smd_bin: Path
    components: dict = field(default_factory=dict)

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
    'radiod':   Component('radiod',   enabled=True,  managed=True,  description='ka9q-radio SDR daemon'),
    'wspr':     Component('wspr',     enabled=True,  managed=True,  description='WSPR/FST4W decoder and uploader'),
    'ka9q-web': Component('ka9q-web', enabled=True,  managed=True,  description='ka9q-web radiod status UI'),
    'grape':    Component('grape',    enabled=True,  managed=False, description='GRAPE hf-timestd recording'),
    'rac':      Component('rac',      enabled=False, managed=False, description='Remote access channel (frpc)'),
}

_DEFAULT_CLIENT_DIR = Path('/home/wsprdaemon/wsprdaemon-client')
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

    for name, cfg in raw.get('component', {}).items():
        components[name] = Component(
            name=name,
            enabled=cfg.get('enabled', False),
            managed=cfg.get('managed', True),
            description=cfg.get('description', ''),
        )

    return Topology(client_dir=client_dir, smd_bin=smd_bin, components=components)


def enabled_components(topology: Topology, only: Optional[list] = None) -> list:
    """Backwards-compatible free function."""
    return topology.enabled_components(only)
