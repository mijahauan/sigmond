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
    nics: list = field(default_factory=list)             # NIC names to harvest ethtool stats for
    usb_devices: list = field(default_factory=list)      # USB vendor:product IDs to track
    irq_pins: dict = field(default_factory=dict)         # handler-name -> expected core list
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

        Order and content are driven by the ``environment_kinds.REGISTRY``;
        kinds with an ``iter_filter`` (currently only ``local_system``)
        are skipped when the filter returns False.
        """
        # Lazy import to avoid a circular import at module load time —
        # environment_kinds imports from environment for the dataclasses.
        from .environment_kinds import REGISTRY, ITER_ORDER

        for kind_name in ITER_ORDER:
            spec = REGISTRY[kind_name]
            attr = getattr(self, spec.plural, None)
            if attr is None:
                continue
            if spec.list_form:
                for item in attr:
                    yield (spec.name, item)
            else:
                # Single-table form (e.g. local_system).
                if spec.iter_filter is None or spec.iter_filter(attr):
                    yield (spec.name, attr)


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
    """Load environment.toml, or return an empty manifest when absent.

    Per-kind parsing is driven by ``environment_kinds.REGISTRY`` — each
    spec carries the dict→dataclass parser, the TOML key, and whether the
    section is array-of-tables (most kinds) or a single table
    (``local_system``).  Adding a new kind no longer requires editing
    this loader; just add a ``KindSpec`` entry.
    """
    import tomllib

    # Lazy import to avoid the circular dependency at module-import time
    # (environment_kinds parsers reference Declared* dataclasses defined
    # above in this module).
    from .environment_kinds import REGISTRY

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

    # Drive every kind's parser from the registry.  Result lands in a
    # by-plural dict so we can splat it into Environment(**kwargs) at the
    # end without naming each plural here.
    parsed: dict = {}
    for spec in REGISTRY.values():
        if spec.list_form:
            rows = raw.get(spec.toml_key, []) or []
            items = [obj for obj in (spec.parse(row) for row in rows)
                     if obj is not None]
            parsed[spec.plural] = items
        else:
            # Single-table form — pass the whole dict (or {} if absent).
            parsed[spec.plural] = spec.parse(raw.get(spec.toml_key, {}) or {})

    d = raw.get('discovery', {}) or {}
    discovery = DiscoveryCfg(
        mdns_enabled=bool(d.get('mdns_enabled', True)),
        multicast_enabled=bool(d.get('multicast_enabled', True)),
        passive_only=bool(d.get('passive_only', False)),
        background_interval=int(d.get('background_interval', 900) or 0),
    )

    return Environment(
        site=site,
        discovery=discovery,
        source_path=path,
        **parsed,
    )
