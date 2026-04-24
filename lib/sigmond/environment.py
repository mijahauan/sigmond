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
    discovery: DiscoveryCfg = field(default_factory=DiscoveryCfg)
    source_path: Optional[Path] = None

    def iter_declared(self):
        """Yield (kind, declared) for every declared peer."""
        for r in self.radiods:      yield ("radiod",      r)
        for k in self.kiwisdrs:     yield ("kiwisdr",     k)
        for g in self.gpsdos:       yield ("gpsdo",       g)
        for t in self.time_sources: yield ("time_source", t)


@dataclass
class Observation:
    source: str                      # "mdns" | "multicast" | "ntp" | "http_kiwisdr" | "gpsdo"
    kind: str                        # "radiod" | "kiwisdr" | "gpsdo" | "time_source"
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
        discovery=discovery,
        source_path=path,
    )
