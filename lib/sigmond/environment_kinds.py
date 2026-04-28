"""Registry of environment-manifest entity kinds.

Adding a new infrastructure-peer kind to sigmond used to mean editing
nine touch-points across five files: a dataclass and loader block in
``environment.py``, an ``iter_declared`` yield, an expect-classifier
branch in ``discovery/reconciler.py``, an order-tuple entry in
``commands/environment.py``, the TUI ``_declared_extra`` rendering, and
the discovery dispatch tables.

This module pushes that knowledge behind a single ``KindSpec`` registry.
Each spec captures everything kind-specific: how to parse the TOML row,
how to filter empty defaults out of ``iter_declared``, the reconciler
hint, and the TUI's extras-column rendering.

The discovery-source dispatch (``ALL_SOURCES``, ``module_for_source``,
``targets_for_source`` in ``discovery/__init__.py``) is intentionally
*not* part of this registry — sources are a per-probe concept, while
this registry is per-kind.  A new kind without a new source rides on
existing dispatch; a new source without a new kind likewise.

To add a new kind: append a single ``KindSpec`` entry below, plus the
matching dataclass next to its peers in ``environment.py`` and a list
field on ``Environment`` named ``plural``.  No other sigmond edits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import environment as _env_mod


# ---------------------------------------------------------------------------
# Per-kind classifier and rendering helpers
# ---------------------------------------------------------------------------
#
# These were inlined into reconciler.py and tui/screens/environment.py
# until this refactor.  Each takes a single declared instance (and, for
# classifiers, the list of successful observations) and returns either
# a (status, detail) tuple or a one-line render string.  Pure functions;
# no side effects.

def _radiod_classifier(declared, good_obs: list) -> tuple:
    expect = declared.expect or {}
    merged: dict = {}
    for o in good_obs:
        merged.update(o.fields)
    for dotted_key, wanted in _flatten(expect).items():
        actual = _dig(merged, dotted_key)
        if actual is None:
            continue
        if actual != wanted:
            return "degraded", (
                f"expect {dotted_key}={wanted!r} but observed {actual!r}"
            )
    return "healthy", ""


def _kiwisdr_classifier(declared, good_obs: list) -> tuple:
    if declared.gps_expected:
        merged: dict = {}
        for o in good_obs:
            merged.update(o.fields)
        if merged.get("gps_fix") is False:
            return "degraded", (
                "gps_expected=true but observed gps_fix=false"
            )
    return "healthy", ""


def _time_source_classifier(declared, good_obs: list) -> tuple:
    if declared.stratum_max:
        merged: dict = {}
        for o in good_obs:
            merged.update(o.fields)
        stratum = merged.get("stratum")
        if isinstance(stratum, int) and stratum > declared.stratum_max:
            return "degraded", (
                f"stratum {stratum} exceeds max {declared.stratum_max}"
            )
    return "healthy", ""


def _gpsdo_classifier(declared, good_obs: list) -> tuple:
    merged: dict = {}
    for o in good_obs:
        merged.update(o.fields)
    if merged.get("locked") is False:
        return "degraded", "GPSDO reports unlocked"
    return "healthy", ""


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def _dig(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# TUI extras-column renderers.

def _radiod_extra(d) -> str:
    return d.status_dns or "—"


def _kiwisdr_extra(d) -> str:
    return f"port {d.port}"


def _gpsdo_extra(d) -> str:
    return d.kind or "—"


def _time_source_extra(d) -> str:
    bits = [d.kind]
    if d.stratum_max:
        bits.append(f"≤S{d.stratum_max}")
    return " ".join(bits) or "—"


def _ka9q_web_extra(d) -> str:
    bits = [f"port {d.port}"]
    if d.role:
        bits.append(d.role)
    return " ".join(bits)


def _gnss_vtec_extra(d) -> str:
    bits = [f"port {d.port}"]
    if d.source:
        bits.append(d.source)
    return " ".join(bits)


def _network_device_extra(d) -> str:
    return d.kind or "—"


def _igmp_querier_extra(d) -> str:
    return d.version or "—"


def _igmp_snooper_extra(d) -> str:
    if d.vlans:
        return f"vlans={','.join(str(v) for v in d.vlans)}"
    return d.interface or "—"


def _local_system_extra(d) -> str:
    bits = []
    if d.cpu_governor:
        bits.append(d.cpu_governor)
    if d.sdrs:
        bits.append(f"{len(d.sdrs)} sdr(s)")
    return " ".join(bits) or "—"


# Loader/parser builders.  Each returns a ``parse(raw_row) -> dataclass | None``
# closure that accepts a TOML row dict and produces the corresponding
# dataclass instance, or ``None`` for invalid rows (no ``id``).

def _parse_radiod(r: dict):
    if not r.get('id'):
        return None
    return _env_mod.DeclaredRadiod(
        id=str(r.get('id', '') or ''),
        host=str(r.get('host', '') or ''),
        status_dns=str(r.get('status_dns', '') or ''),
        role=str(r.get('role', '') or ''),
        expect=dict(r.get('expect', {}) or {}),
    )


def _parse_kiwisdr(k: dict):
    if not k.get('id'):
        return None
    return _env_mod.DeclaredKiwi(
        id=str(k.get('id', '') or ''),
        host=str(k.get('host', '') or ''),
        port=int(k.get('port', 8073) or 8073),
        gps_expected=bool(k.get('gps_expected', False)),
    )


def _parse_gpsdo(g: dict):
    if not g.get('id'):
        return None
    return _env_mod.DeclaredGpsdo(
        id=str(g.get('id', '') or ''),
        kind=str(g.get('kind', '') or ''),
        host=str(g.get('host', '') or ''),
        authority_json=str(g.get('authority_json', '') or ''),
        serves=list(g.get('serves', []) or []),
    )


def _parse_time_source(t: dict):
    if not t.get('id'):
        return None
    return _env_mod.DeclaredTimeSource(
        id=str(t.get('id', '') or ''),
        kind=str(t.get('kind', '') or ''),
        host=str(t.get('host', '') or ''),
        authority_json=str(t.get('authority_json', '') or ''),
        stratum_max=int(t.get('stratum_max', 0) or 0),
    )


def _parse_ka9q_web(w: dict):
    if not w.get('id'):
        return None
    return _env_mod.DeclaredKa9qWeb(
        id=str(w.get('id', '') or ''),
        host=str(w.get('host', '') or ''),
        port=int(w.get('port', 8080) or 8080),
        role=str(w.get('role', '') or ''),
        expect=dict(w.get('expect', {}) or {}),
    )


def _parse_gnss_vtec(v: dict):
    if not v.get('id'):
        return None
    return _env_mod.DeclaredGnssVtec(
        id=str(v.get('id', '') or ''),
        host=str(v.get('host', '') or ''),
        port=int(v.get('port', 8080) or 8080),
        source=str(v.get('source', '') or ''),
        expect=dict(v.get('expect', {}) or {}),
    )


def _parse_network_device(n: dict):
    if not n.get('id'):
        return None
    return _env_mod.DeclaredNetworkDevice(
        id=str(n.get('id', '') or ''),
        kind=str(n.get('kind', '') or ''),
        host=str(n.get('host', '') or ''),
        community=str(n.get('community', '') or ''),
        expect=dict(n.get('expect', {}) or {}),
    )


def _parse_igmp_querier(q: dict):
    if not q.get('id'):
        return None
    return _env_mod.DeclaredIgmpQuerier(
        id=str(q.get('id', '') or ''),
        host=str(q.get('host', '') or ''),
        interface=str(q.get('interface', '') or ''),
        version=str(q.get('version', 'IGMPv3') or 'IGMPv3'),
        expect=dict(q.get('expect', {}) or {}),
    )


def _parse_igmp_snooper(s: dict):
    if not s.get('id'):
        return None
    return _env_mod.DeclaredIgmpSnooper(
        id=str(s.get('id', '') or ''),
        host=str(s.get('host', '') or ''),
        interface=str(s.get('interface', '') or ''),
        vlans=list(s.get('vlans', []) or []),
        expect=dict(s.get('expect', {}) or {}),
    )


def _parse_local_system(raw: dict):
    """Single-table form — never returns None; the iter_filter decides
    whether the result is operator-declared enough to surface."""
    raw = raw or {}
    return _env_mod.DeclaredLocalSystem(
        id="localhost",
        cpu_affinity=list(raw.get('cpu_affinity', []) or []),
        cpu_governor=str(raw.get('cpu_governor', '') or ''),
        sdrs=list(raw.get('sdrs', []) or []),
        expect=dict(raw.get('expect', {}) or {}),
    )


def _local_system_is_declared(ls) -> bool:
    """An empty default DeclaredLocalSystem is treated as 'no local
    declaration' so it doesn't show up as a phantom missing delta on
    every host.  Operator must populate at least one field."""
    return bool(ls.cpu_affinity or ls.cpu_governor or ls.sdrs or ls.expect)


# ---------------------------------------------------------------------------
# KindSpec
# ---------------------------------------------------------------------------

@dataclass
class KindSpec:
    """Everything sigmond needs to know about one environment kind."""

    name: str                                          # "kiwisdr"
    plural: str                                        # "kiwisdrs"  (Environment field name)
    toml_key: str                                      # "kiwisdr"   (TOML section name)
    parse: Callable[[dict], Optional[Any]]             # raw row -> dataclass instance | None
    list_form: bool = True                             # array-of-tables (True) vs single table
    iter_filter: Optional[Callable[[Any], bool]] = None    # only yield if filter(d)
    expect_classifier: Optional[Callable[[Any, list], tuple]] = None
    tui_extra: Optional[Callable[[Any], str]] = None       # TUI extras-column render


# Insertion order is the canonical display order for `smd environment list`
# (the order tuple in commands/environment.py used to be hand-maintained;
# now it derives from this dict's keys).
REGISTRY: dict = {
    "radiod": KindSpec(
        name="radiod", plural="radiods", toml_key="radiod",
        parse=_parse_radiod,
        expect_classifier=_radiod_classifier,
        tui_extra=_radiod_extra,
    ),
    "kiwisdr": KindSpec(
        name="kiwisdr", plural="kiwisdrs", toml_key="kiwisdr",
        parse=_parse_kiwisdr,
        expect_classifier=_kiwisdr_classifier,
        tui_extra=_kiwisdr_extra,
    ),
    "gpsdo": KindSpec(
        name="gpsdo", plural="gpsdos", toml_key="gpsdo",
        parse=_parse_gpsdo,
        expect_classifier=_gpsdo_classifier,
        tui_extra=_gpsdo_extra,
    ),
    "time_source": KindSpec(
        name="time_source", plural="time_sources", toml_key="time_source",
        parse=_parse_time_source,
        expect_classifier=_time_source_classifier,
        tui_extra=_time_source_extra,
    ),
    "ka9q_web": KindSpec(
        name="ka9q_web", plural="ka9q_webs", toml_key="ka9q_web",
        parse=_parse_ka9q_web,
        tui_extra=_ka9q_web_extra,
    ),
    "gnss_vtec": KindSpec(
        name="gnss_vtec", plural="gnss_vtecs", toml_key="gnss_vtec",
        parse=_parse_gnss_vtec,
        tui_extra=_gnss_vtec_extra,
    ),
    "network_device": KindSpec(
        name="network_device", plural="network_devices",
        toml_key="network_device",
        parse=_parse_network_device,
        tui_extra=_network_device_extra,
    ),
    "igmp_querier": KindSpec(
        name="igmp_querier", plural="igmp_queriers", toml_key="igmp_querier",
        parse=_parse_igmp_querier,
        tui_extra=_igmp_querier_extra,
    ),
    "igmp_snooper": KindSpec(
        name="igmp_snooper", plural="igmp_snoopers", toml_key="igmp_snooper",
        parse=_parse_igmp_snooper,
        tui_extra=_igmp_snooper_extra,
    ),
    "local_system": KindSpec(
        name="local_system", plural="local_system", toml_key="local_system",
        parse=_parse_local_system,
        list_form=False,
        iter_filter=_local_system_is_declared,
        tui_extra=_local_system_extra,
    ),
}


# Kinds in declaration order — used by iter_declared and CLI list ordering.
# Note local_system is yielded first historically (pre-other kinds) because
# the original iter_declared put it there; preserve that ordering here so
# behavioural tests don't shift.
ITER_ORDER: tuple = (
    "local_system",
    "radiod",
    "kiwisdr",
    "gpsdo",
    "time_source",
    "ka9q_web",
    "gnss_vtec",
    "network_device",
    "igmp_querier",
    "igmp_snooper",
)


# Kinds in CLI/TUI display order — preserves the original `_print_human`
# tuple, which historically put local_system last (presentation choice
# distinct from iter order).
DISPLAY_ORDER: tuple = (
    "radiod",
    "kiwisdr",
    "gpsdo",
    "time_source",
    "ka9q_web",
    "gnss_vtec",
    "network_device",
    "igmp_querier",
    "igmp_snooper",
    "local_system",
)
