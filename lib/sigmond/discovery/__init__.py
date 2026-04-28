"""Discovery subsystem — probes the local network/filesystem for peers
declared in `environment.toml`, and the reconciler compares observed vs
declared.

Every probe module exposes::

    probe(env, *, timeout, limiter, **injected_transports) -> list[Observation]

Transports (subprocess runner, socket factory, urlopen, …) are injected so
probes are unit-testable without network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..environment import Environment, EnvironmentView, Observation
from ..paths import ENVIRONMENT_CACHE


# ---------------------------------------------------------------------------
# Default cadences (seconds).  Enforced by RateLimiter.
# ---------------------------------------------------------------------------

DEFAULT_CADENCE = {
    "mdns":          60.0,
    "multicast":     120.0,
    "ntp":           300.0,
    "http_kiwisdr":  300.0,
    "gpsdo":         30.0,
    "http_ka9q":     300.0,
    "http_gnss":     300.0,
    "snmp":          300.0,
    "usb_sdr":       60.0,
}

# Minimum gap between two probes of the same (source, target) even when
# the operator passes --force.  Prevents accidental flooding from a tight
# retry loop.
HARD_FLOOR = 5.0

ALL_SOURCES = ("mdns", "multicast", "ntp", "http_kiwisdr", "gpsdo",
               "http_ka9q", "http_gnss", "snmp", "usb_sdr")
ACTIVE_SOURCES = ("ntp", "http_kiwisdr", "http_ka9q", "http_gnss", "snmp", "usb_sdr")
PASSIVE_SOURCES = ("mdns", "multicast", "gpsdo")


# ---------------------------------------------------------------------------
# Source -> probe module dispatch.  Single source of truth used by both the
# CLI (`smd environment probe`) and the TUI Environment screen.
# ---------------------------------------------------------------------------

def module_for_source(src: str):
    """Return the discovery probe module for a source name, or None."""
    if src == "mdns":
        from . import mdns
        return mdns
    if src == "multicast":
        from . import multicast
        return multicast
    if src == "ntp":
        from . import ntp
        return ntp
    if src == "http_kiwisdr":
        from . import http_kiwisdr
        return http_kiwisdr
    if src == "gpsdo":
        from . import gpsdo
        return gpsdo
    if src == "http_ka9q":
        from . import http_ka9q
        return http_ka9q
    if src == "http_gnss":
        from . import http_gnss
        return http_gnss
    if src == "snmp":
        from . import snmp
        return snmp
    if src == "usb_sdr":
        from . import usb_sdr
        return usb_sdr
    return None


def targets_for_source(src: str, env: "Environment") -> list:
    """Return the list of declared targets a given source will probe.
    Used by the rate limiter to decide whether to skip an entire source
    when no target is due."""
    if src == "mdns":
        return ["_site"]                         # single broadcast target
    if src == "multicast":
        return [r.status_dns or r.host for r in env.radiods]
    if src == "ntp":
        return [t.host for t in env.time_sources]
    if src == "http_kiwisdr":
        return [k.host for k in env.kiwisdrs]
    if src == "gpsdo":
        return [g.host or "localhost" for g in env.gpsdos]
    if src == "http_ka9q":
        return [w.host for w in env.ka9q_webs]
    if src == "http_gnss":
        return [v.host for v in env.gnss_vtecs]
    if src == "snmp":
        return [n.host for n in env.network_devices]
    if src == "usb_sdr":
        return ["localhost"]                     # local-only probe
    return []


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

@dataclass
class RateLimiter:
    """Token-bucket-style limiter keyed by (source, target).

    This is a per-process throttle; callers that want to persist last-probe
    times across CLI invocations should seed the limiter from the cache.
    """
    cadence: dict = field(default_factory=lambda: dict(DEFAULT_CADENCE))
    hard_floor: float = HARD_FLOOR
    _last: dict = field(default_factory=dict)            # (source, target) -> float
    _clock: Callable[[], float] = field(default=time.time)

    def allow(self, source: str, target: str, *, force: bool = False) -> bool:
        now = self._clock()
        key = (source, target)
        last = self._last.get(key, 0.0)
        min_gap = self.hard_floor if force else self.cadence.get(source, 60.0)
        if now - last < min_gap:
            return False
        self._last[key] = now
        return True

    def seed(self, source: str, target: str, ts: float) -> None:
        self._last[(source, target)] = ts

    def last_seen(self, source: str, target: str) -> float:
        return self._last.get((source, target), 0.0)


# ---------------------------------------------------------------------------
# On-disk cache of the most recent EnvironmentView
# ---------------------------------------------------------------------------

def cache_path() -> Path:
    return ENVIRONMENT_CACHE


def save_cache(view: EnvironmentView, path: Optional[Path] = None) -> None:
    """Write the latest view to disk so successive CLI calls are instant.

    Silently skips if the cache directory is not writable (e.g. /var/lib/sigmond
    not yet created or owned by root).  The environment screen still works; it
    just won't persist the results across restarts.
    """
    p = path or cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return
    payload = {
        "probed_at": view.probed_at,
        "observations": [_obs_to_dict(o) for o in view.observations],
        "deltas": [
            {
                "kind":     d.kind,
                "id":       d.id,
                "status":   d.status,
                "detail":   d.detail,
                "observed": [_obs_to_dict(o) for o in d.observed],
            }
            for d in view.deltas
        ],
    }
    try:
        p.write_text(json.dumps(payload, indent=2))
    except PermissionError:
        pass


def load_cache(path: Optional[Path] = None) -> dict:
    """Read the cache, or return an empty skeleton if missing/corrupt."""
    p = path or cache_path()
    if not p.exists():
        return {"probed_at": 0.0, "observations": [], "deltas": []}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"probed_at": 0.0, "observations": [], "deltas": []}


def _obs_to_dict(o: Observation) -> dict:
    return {
        "source":      o.source,
        "kind":        o.kind,
        "id":          o.id,
        "endpoint":    o.endpoint,
        "fields":      o.fields,
        "observed_at": o.observed_at,
        "ok":          o.ok,
        "error":       o.error,
    }


def dict_to_obs(d: dict) -> Observation:
    return Observation(
        source=d.get("source", ""),
        kind=d.get("kind", ""),
        id=d.get("id"),
        endpoint=d.get("endpoint", ""),
        fields=dict(d.get("fields", {}) or {}),
        observed_at=float(d.get("observed_at", 0.0) or 0.0),
        ok=bool(d.get("ok", True)),
        error=str(d.get("error", "") or ""),
    )


# ---------------------------------------------------------------------------
# Source selection helper for `smd environment probe --source=…`
# ---------------------------------------------------------------------------

def resolve_sources(env: Environment, selected: Optional[str]) -> list:
    """Map --source argument to a list of source names, honouring
    discovery config and the `passive_only` flag."""
    if selected and selected != "all":
        wanted = [selected]
    else:
        wanted = list(ALL_SOURCES)

    if not env.discovery.mdns_enabled:
        wanted = [s for s in wanted if s != "mdns"]
    if not env.discovery.multicast_enabled:
        wanted = [s for s in wanted if s != "multicast"]
    if env.discovery.passive_only:
        wanted = [s for s in wanted if s in PASSIVE_SOURCES]

    return wanted
