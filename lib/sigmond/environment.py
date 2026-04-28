"""Environment manifest — the operator-declared picture of what should be
around this host.

Read by `smd environment` and the TUI Environment screen, and reconciled
against live discovery observations.  This is distinct from `topology.py`
(what this host runs) and `coordination.py` (how local components talk).
Environment answers: 'what peers should I expect to see on the network,
and does the observed picture match?'.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from .paths import ENVIRONMENT_PATH
from .ui import warn


DeltaStatus = Literal["healthy", "missing", "unknown-extra", "degraded"]


@dataclass
class Site:
    name: str = ""
    description: str = ""


@dataclass
class DeclaredRadiod:
    id: str
    host: str
    status_dns: str = ""
    role: str = ""
    expect: dict = field(default_factory=dict)


@dataclass
class DeclaredKiwi:
    id: str
    host: str
    port: int = 8073
    gps_expected: bool = False


@dataclass
class DeclaredGpsdo:
    id: str
    kind: str
    host: str
    authority_json: str = ""
    serves: list = field(default_factory=list)


@dataclass
class DeclaredTimeSource:
    id: str
    kind: str            # "hf-timestd" | "ntp" | "ptp"
    host: str
    authority_json: str = ""
    stratum_max: int = 0


@dataclass
class DeclaredKa9qWeb:
    """A ka9q-web instance (local or remote)."""
    id: str
    host: str
    port: int = 8080
    role: str = ""       # "primary" | "secondary" | "archive"
    expect: dict = field(default_factory=dict)


@dataclass
class DeclaredGnssVtec:
    """A GNSS-VTEC server providing ionospheric data."""
    id: str
    host: str
    port: int = 8080
    source: str = ""     # "uBlox" | "Septentrio" | "Skytraq" | "generic"
    expect: dict = field(default_factory=dict)


@dataclass
class DeclaredNetworkDevice:
    """A network device (switch, router, access point)."""
    id: str
    kind: str            # "switch" | "router" | "ap" | "firewall" | "gateway"
    host: str
    community: str = ""  # SNMP community (optional)
    expect: dict = field(default_factory=dict)


@dataclass
class DeclaredLocalSystem:
    """Local system resources (CPU, SDR hardware, etc.)."""
    id: str = "localhost"
    cpu_affinity: list = field(default_factory=list)      # CPU cores available
    cpu_governor: str = ""                                # "performance" | "powersave" | etc.
    sdrs: list = field(default_factory=list)             # SDR devices present
    expect: dict = field(default_factory=dict)


@dataclass
class DeclaredIgmpQuerier:
    """An IGMP querier (typically a network switch or router)."""
    id: str
    host: str
    interface: str = ""                                   # e.g., "eth0" or "192.168.1.1"
    version: str = "IGMPv3"                               # "IGMPv2" | "IGMPv3"
    expect: dict = field(default_factory=dict)


@dataclass
class DeclaredIgmpSnooper:
    """An IGMP snooper (switch with IGMP snooping enabled)."""
    id: str
    host: str
    interface: str = ""                                   # e.g., "eth0"
    vlans: list = field(default_factory=list)            # VLANs with snooping enabled
    expect: dict = field(default_factory=dict)


@dataclass
class DiscoveryCfg:
    mdns_enabled: bool = True
    multicast_enabled: bool = True
    passive_only: bool = False
    background_interval: int = 900


@dataclass
class Environment:
    site: Site = field(default_factory=Site)
    radiods: list = field(default_factory=list)          # DeclaredRadiod
    kiwisdrs: list = field(default_factory=list)          # DeclaredKiwi
    gpsdos: list = field(default_factory=list)            # DeclaredGpsdo
    time_sources: list = field(default_factory=list)      # DeclaredTimeSource
    ka9q_webs: list = field(default_factory=list)         # DeclaredKa9qWeb
    gnss_vtecs: list = field(default_factory=list)        # DeclaredGnssVtec
    network_devices: list = field(default_factory=list)   # DeclaredNetworkDevice
    igmp_queriers: list = field(default_factory=list)     # DeclaredIgmpQuerier
    igmp_snoopers: list = field(default_factory=list)     # DeclaredIgmpSnooper
    local_system: DeclaredLocalSystem = field(default_factory=DeclaredLocalSystem)
    discovery: DiscoveryCfg = field(default_factory=DiscoveryCfg)
    source_path: Optional[Path] = None

    def iter_declared(self):
        """Yield (kind, declared) for every declared peer.

        local_system is only yielded when the operator has actually
        declared something for it (a non-empty [local_system] block in
        environment.toml).  An empty default DeclaredLocalSystem is
        treated as "no local declaration" so it doesn't show up as a
        phantom missing delta on every host.
        """
        if _local_system_is_declared(self.local_system):
            yield ("local_system", self.local_system)
        for r in self.radiods:      yield ("radiod",      r)
        for k in self.kiwisdrs:     yield ("kiwisdr",     k)
        for g in self.gpsdos:       yield ("gpsdo",       g)
        for t in self.time_sources: yield ("time_source", t)
        for w in self.ka9q_webs:    yield ("ka9q_web",    w)
        for v in self.gnss_vtecs:   yield ("gnss_vtec",   v)
        for n in self.network_devices: yield ("network_device", n)
        for q in self.igmp_queriers: yield ("igmp_querier", q)
        for s in self.igmp_snoopers: yield ("igmp_snooper", s)


def _local_system_is_declared(ls: "DeclaredLocalSystem") -> bool:
    return bool(ls.cpu_affinity or ls.cpu_governor or ls.sdrs or ls.expect)


@dataclass
class Observation:
    source: str                      # "mdns" | "multicast" | "ntp" | "http_kiwisdr" | "gpsdo" | "http_ka9q" | "http_gnss" | "snmp" | "igmp" | "local"
    kind: str                        # "radiod" | "kiwisdr" | "gpsdo" | "time_source" | "ka9q_web" | "gnss_vtec" | "network_device" | "igmp_querier" | "igmp_snooper" | "local_system"
    id: Optional[str]                # matched declared id, else None
    endpoint: str                    # hostname:port or mcast group
    fields: dict = field(default_factory=dict)
    observed_at: float = 0.0
    ok: bool = True
    error: str = ""


@dataclass
class Delta:
    kind: str
    id: str
    status: DeltaStatus
    detail: str = ""
    declared: Optional[Any] = None
    observed: list = field(default_factory=list)         # Observation


@dataclass
class EnvironmentView:
    env: Environment
    observations: list = field(default_factory=list)     # Observation
    deltas: list = field(default_factory=list)           # Delta
    probed_at: float = 0.0


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_environment(path: Path = ENVIRONMENT_PATH) -> Environment:
    """Load environment.toml, or return an empty manifest when absent."""
    import tomllib

    if not path.exists():
        warn(f'No environment manifest at {path} — using empty manifest')
        return Environment(source_path=path)

    with open(path, 'rb') as f:
        raw = tomllib.load(f)

    site_raw = raw.get('site', {}) or {}
    site = Site(
        name=str(site_raw.get('name', '') or ''),
        description=str(site_raw.get('description', '') or ''),
    )

    radiods = [
        DeclaredRadiod(
            id=str(r.get('id', '') or ''),
            host=str(r.get('host', '') or ''),
            status_dns=str(r.get('status_dns', '') or ''),
            role=str(r.get('role', '') or ''),
            expect=dict(r.get('expect', {}) or {}),
        )
        for r in (raw.get('radiod', []) or [])
        if r.get('id')
    ]

    kiwis = [
        DeclaredKiwi(
            id=str(k.get('id', '') or ''),
            host=str(k.get('host', '') or ''),
            port=int(k.get('port', 8073) or 8073),
            gps_expected=bool(k.get('gps_expected', False)),
        )
        for k in (raw.get('kiwisdr', []) or [])
        if k.get('id')
    ]

    gpsdos = [
        DeclaredGpsdo(
            id=str(g.get('id', '') or ''),
            kind=str(g.get('kind', '') or ''),
            host=str(g.get('host', '') or ''),
            authority_json=str(g.get('authority_json', '') or ''),
            serves=list(g.get('serves', []) or []),
        )
        for g in (raw.get('gpsdo', []) or [])
        if g.get('id')
    ]

    time_sources = [
        DeclaredTimeSource(
            id=str(t.get('id', '') or ''),
            kind=str(t.get('kind', '') or ''),
            host=str(t.get('host', '') or ''),
            authority_json=str(t.get('authority_json', '') or ''),
            stratum_max=int(t.get('stratum_max', 0) or 0),
        )
        for t in (raw.get('time_source', []) or [])
        if t.get('id')
    ]

    ka9q_webs = [
        DeclaredKa9qWeb(
            id=str(w.get('id', '') or ''),
            host=str(w.get('host', '') or ''),
            port=int(w.get('port', 8080) or 8080),
            role=str(w.get('role', '') or ''),
            expect=dict(w.get('expect', {}) or {}),
        )
        for w in (raw.get('ka9q_web', []) or [])
        if w.get('id')
    ]

    gnss_vtecs = [
        DeclaredGnssVtec(
            id=str(v.get('id', '') or ''),
            host=str(v.get('host', '') or ''),
            port=int(v.get('port', 8080) or 8080),
            source=str(v.get('source', '') or ''),
            expect=dict(v.get('expect', {}) or {}),
        )
        for v in (raw.get('gnss_vtec', []) or [])
        if v.get('id')
    ]

    network_devices = [
        DeclaredNetworkDevice(
            id=str(n.get('id', '') or ''),
            kind=str(n.get('kind', '') or ''),
            host=str(n.get('host', '') or ''),
            community=str(n.get('community', '') or ''),
            expect=dict(n.get('expect', {}) or {}),
        )
        for n in (raw.get('network_device', []) or [])
        if n.get('id')
    ]

    igmp_queriers = [
        DeclaredIgmpQuerier(
            id=str(q.get('id', '') or ''),
            host=str(q.get('host', '') or ''),
            interface=str(q.get('interface', '') or ''),
            version=str(q.get('version', 'IGMPv3') or 'IGMPv3'),
            expect=dict(q.get('expect', {}) or {}),
        )
        for q in (raw.get('igmp_querier', []) or [])
        if q.get('id')
    ]

    igmp_snoopers = [
        DeclaredIgmpSnooper(
            id=str(s.get('id', '') or ''),
            host=str(s.get('host', '') or ''),
            interface=str(s.get('interface', '') or ''),
            vlans=list(s.get('vlans', []) or []),
            expect=dict(s.get('expect', {}) or {}),
        )
        for s in (raw.get('igmp_snooper', []) or [])
        if s.get('id')
    ]

    local_system = DeclaredLocalSystem(
        id="localhost",
        cpu_affinity=list(raw.get('local_system', {}).get('cpu_affinity', []) or []),
        cpu_governor=str(raw.get('local_system', {}).get('cpu_governor', '') or ''),
        sdrs=list(raw.get('local_system', {}).get('sdrs', []) or []),
        expect=dict(raw.get('local_system', {}).get('expect', {}) or {}),
    )

    d = raw.get('discovery', {}) or {}
    discovery = DiscoveryCfg(
        mdns_enabled=bool(d.get('mdns_enabled', True)),
        multicast_enabled=bool(d.get('multicast_enabled', True)),
        passive_only=bool(d.get('passive_only', False)),
        background_interval=int(d.get('background_interval', 900) or 0),
    )

    return Environment(
        site=site,
        radiods=radiods,
        kiwisdrs=kiwis,
        gpsdos=gpsdos,
        time_sources=time_sources,
        ka9q_webs=ka9q_webs,
        gnss_vtecs=gnss_vtecs,
        network_devices=network_devices,
        igmp_queriers=igmp_queriers,
        igmp_snoopers=igmp_snoopers,
        local_system=local_system,
        discovery=discovery,
        source_path=path,
    )
