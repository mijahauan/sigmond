"""Sigmond coordination config.

`/etc/sigmond/coordination.toml` is the authoritative source for everything
that no single client owns: station identity, the named radiod instances
(local or remote), CPU budget for the local host, and the registry of
client instances bound to those radiods.

A client's own native config file remains authoritative for that client.
Sigmond reads this file, runs harmonization rules, and writes the
flattened view to `/etc/sigmond/coordination.env` which clients consume
via systemd `EnvironmentFile=-`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .paths import COORDINATION_PATH


@dataclass
class Host:
    call: str = ""
    grid: str = ""
    lat:  float = 0.0
    lon:  float = 0.0


@dataclass
class Radiod:
    id: str
    host: str = "localhost"
    status_dns: str = ""
    samprate_hz: int = 0
    cores: str = ""
    radio_conf: str = ""

    @property
    def is_local(self) -> bool:
        return self.host in ("localhost", "127.0.0.1", "::1", "")


@dataclass
class Cpu:
    suite_cores:           str = ""
    worker_cores:          str = ""
    reserved_cpus:         str = ""
    owns_timestd_affinity: bool = True


@dataclass
class ClientInstance:
    client_type: str              # "hf-timestd", "wspr", "ka9q-web", ...
    instance:    str = "default"
    radiod_id:   Optional[str] = None
    extras:      dict = field(default_factory=dict)

    @property
    def unit_name(self) -> str:
        # Systemd sanitization: instance names should be %i-safe.
        return f"{self.client_type}@{self.instance}"


@dataclass
class DiskBudget:
    root_path:    str = "/var"
    warn_percent: int = 80


@dataclass
class Coordination:
    host:    Host                  = field(default_factory=Host)
    radiods: dict                  = field(default_factory=dict)   # id -> Radiod
    cpu:     Cpu                   = field(default_factory=Cpu)
    clients: list                  = field(default_factory=list)   # list[ClientInstance]
    disk_budget: DiskBudget        = field(default_factory=DiskBudget)
    source_path: Optional[Path]    = None

    def instances_of(self, client_type: str) -> list:
        return [c for c in self.clients if c.client_type == client_type]

    def instances_bound_to(self, radiod_id: str) -> list:
        return [c for c in self.clients if c.radiod_id == radiod_id]

    def local_radiods(self) -> list:
        return [r for r in self.radiods.values() if r.is_local]

    def resolve_radiod(self, radiod_id: Optional[str]) -> Optional[Radiod]:
        if not radiod_id:
            return None
        return self.radiods.get(radiod_id)


def load_coordination(path: Path = COORDINATION_PATH) -> Coordination:
    """Load coordination.toml, or return empty defaults when absent."""
    import tomllib

    if not path.exists():
        return Coordination()

    with open(path, 'rb') as f:
        raw = tomllib.load(f)

    return parse_coordination(raw, source_path=path)


def parse_coordination(raw: dict, source_path: Optional[Path] = None) -> Coordination:
    """Parse a pre-loaded TOML dict into a Coordination object."""
    host_raw = raw.get('host', {}) or {}
    host = Host(
        call=host_raw.get('call', ''),
        grid=host_raw.get('grid', ''),
        lat=float(host_raw.get('lat', 0.0) or 0.0),
        lon=float(host_raw.get('lon', 0.0) or 0.0),
    )

    radiods: dict = {}
    for rid, rcfg in (raw.get('radiod', {}) or {}).items():
        rcfg = rcfg or {}
        radiods[rid] = Radiod(
            id=rid,
            host=rcfg.get('host', 'localhost'),
            status_dns=rcfg.get('status_dns', ''),
            samprate_hz=int(rcfg.get('samprate_hz', 0) or 0),
            cores=rcfg.get('cores', ''),
            radio_conf=rcfg.get('radio_conf', ''),
        )

    cpu_raw = raw.get('cpu', {}) or {}
    cpu = Cpu(
        suite_cores=cpu_raw.get('suite_cores', ''),
        worker_cores=cpu_raw.get('worker_cores', ''),
        reserved_cpus=cpu_raw.get('reserved_cpus', ''),
        owns_timestd_affinity=bool(cpu_raw.get('owns_timestd_affinity', True)),
    )

    clients: list = []
    clients_raw = raw.get('clients', {}) or {}
    for ctype, entries in clients_raw.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            entry = entry or {}
            instance_name = entry.get('instance', 'default')
            radiod_id     = entry.get('radiod_id')
            extras = {k: v for k, v in entry.items()
                      if k not in ('instance', 'radiod_id')}
            clients.append(ClientInstance(
                client_type=ctype,
                instance=instance_name,
                radiod_id=radiod_id,
                extras=extras,
            ))

    disk_raw = raw.get('disk_budget', {}) or {}
    disk = DiskBudget(
        root_path=disk_raw.get('root_path', '/var'),
        warn_percent=int(disk_raw.get('warn_percent', 80) or 80),
    )

    return Coordination(
        host=host,
        radiods=radiods,
        cpu=cpu,
        clients=clients,
        disk_budget=disk,
        source_path=source_path,
    )


# ---------------------------------------------------------------------------
# Rendering — coordination.toml → coordination.env
# ---------------------------------------------------------------------------

def _env_key(*parts) -> str:
    """Build an ENV_KEY from pieces, uppercasing and sanitizing."""
    raw = '_'.join(str(p) for p in parts if p)
    out = []
    for ch in raw:
        if ch.isalnum():
            out.append(ch.upper())
        else:
            out.append('_')
    # Collapse runs of underscores.
    collapsed = []
    prev_underscore = False
    for ch in out:
        if ch == '_':
            if not prev_underscore:
                collapsed.append(ch)
            prev_underscore = True
        else:
            collapsed.append(ch)
            prev_underscore = False
    return ''.join(collapsed).strip('_')


def _passthrough_extras_for(client_type: str) -> list:
    """Read ``[client.coordination] passthrough_extras`` from a client's
    ``/opt/git/sigmond/<client>/deploy.toml``.  Returns ``[]`` when the file is
    absent, unreadable, or the key is unset — clients opt in by shipping
    the block in their deploy.toml.  Wave 2 will move the canonical
    deploy.toml lookup into ``discover.py``; this minimal reader keeps
    Wave 1 free of cross-module surgery.
    """
    import tomllib
    path = Path(f"/opt/git/sigmond/{client_type}/deploy.toml")
    try:
        if not path.exists():
            return []
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except (OSError, PermissionError, tomllib.TOMLDecodeError):
        return []
    coord_block = (data.get('client', {}) or {}).get('coordination', {}) or {}
    keys = coord_block.get('passthrough_extras', []) or []
    return [str(k) for k in keys if isinstance(k, str)]


def render_env(coord: Coordination,
               passthrough_lookup: Optional[Callable[[str], list]] = None) -> str:
    """Render the coordination as KEY=VALUE lines suitable for systemd
    EnvironmentFile=.

    ``passthrough_lookup`` is the function that, given a client_type,
    returns the list of ``extras`` keys that client wants piped into
    coordination.env.  Defaults to reading the client's deploy.toml.
    Tests inject a fake lookup to avoid touching ``/opt/git/sigmond``.
    """
    if passthrough_lookup is None:
        passthrough_lookup = _passthrough_extras_for

    lines = [
        '# Generated by sigmond — do not edit.',
        '# Source: /etc/sigmond/coordination.toml',
        '',
    ]
    if coord.host.call:
        lines.append(f'STATION_CALL={coord.host.call}')
    if coord.host.grid:
        lines.append(f'STATION_GRID={coord.host.grid}')
    if coord.host.lat:
        lines.append(f'STATION_LAT={coord.host.lat}')
    if coord.host.lon:
        lines.append(f'STATION_LON={coord.host.lon}')
    if any((coord.host.call, coord.host.grid, coord.host.lat, coord.host.lon)):
        lines.append('')

    for rid, r in sorted(coord.radiods.items()):
        prefix = _env_key('RADIOD', rid)
        lines.append(f'{prefix}_HOST={r.host}')
        if r.status_dns:
            lines.append(f'{prefix}_STATUS={r.status_dns}')
        if r.samprate_hz:
            lines.append(f'{prefix}_SAMPRATE={r.samprate_hz}')
        lines.append('')

    for c in coord.clients:
        if not c.radiod_id:
            continue
        prefix = _env_key(c.client_type, c.instance)
        lines.append(f'{prefix}_RADIOD={c.radiod_id}')
    if any(c.radiod_id for c in coord.clients):
        lines.append('')

    # Generic extras passthrough — clients opt in via deploy.toml
    # [client.coordination] passthrough_extras = [...].  Cache the lookup
    # per client_type so we don't re-read deploy.toml for every instance.
    keys_by_type: dict = {}
    emitted: set = set()                    # (client_type, instance, key)
    for c in coord.clients:
        if c.client_type not in keys_by_type:
            keys_by_type[c.client_type] = passthrough_lookup(c.client_type)
        wanted = keys_by_type[c.client_type]
        if not wanted:
            continue
        for key in wanted:
            if key not in c.extras:
                continue
            line = f'{_env_key(c.client_type, c.instance, key)}={c.extras[key]}'
            lines.append(line)
            emitted.add((c.client_type, c.instance, key))

    # Legacy ka9q-web port hardcode — kept as fallback for instances whose
    # deploy.toml hasn't yet declared passthrough_extras.  Removed in
    # Wave 2 once all clients ship the [client.coordination] block.
    for c in coord.clients:
        if c.client_type == 'ka9q-web' and 'port' in c.extras:
            if (c.client_type, c.instance, 'port') in emitted:
                continue
            prefix = _env_key('KA9Q_WEB', c.instance)
            lines.append(f'{prefix}_PORT={c.extras["port"]}')

    return '\n'.join(lines).rstrip() + '\n'
