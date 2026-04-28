"""Client adapters.

Each adapter knows how to read (Phase 1) and eventually write (Phase 2+)
a specific HamSCI client's coordination touch-points.

Wave 2 collapsed dispatch around the generic ``ContractAdapter`` (which
shells out to ``<client> inventory|validate --json`` per the contract).
The legacy bespoke adapters survive only for clients that have a
file-read fallback path the contract surface doesn't yet replace —
``RadiodAdapter`` (radiod isn't contract-conformant by design),
``HfTimestdAdapter`` and ``WsprAdapter`` (they wrap ContractAdapter with a
TOML/INI fallback for older binaries that lack ``inventory``).  New
clients don't need a registry entry: ``load_adapter`` finds them via the
catalog and returns a generic ``ContractAdapter`` automatically.
"""

from .base import ClientAdapter, ClientView, InstanceView
from .contract import ContractAdapter
from .hftimestd import HfTimestdAdapter
from .radiod import RadiodAdapter
from .wspr import WsprAdapter


# Bespoke adapters that wrap or replace ContractAdapter for clients that
# need file-read fallbacks.  Keyed by topology component name (and aliases
# where the topology still uses old names like 'grape').
REGISTRY = {
    'ka9q-radio': RadiodAdapter,
    'radiod':     RadiodAdapter,    # legacy topology alias
    'wspr':       WsprAdapter,
    'wsprdaemon-client': WsprAdapter,
    'grape':      HfTimestdAdapter,
    'hf-timestd': HfTimestdAdapter,
}


_CATALOG_CACHE = None


def _cached_catalog():
    """Cache load_catalog per process — avoids re-globbing /opt/git for
    every adapter lookup during a single command invocation."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        try:
            from ..catalog import load_catalog
            _CATALOG_CACHE = load_catalog()
        except Exception:
            _CATALOG_CACHE = {}
    return _CATALOG_CACHE


def load_adapter(component_name: str) -> ClientAdapter | None:
    """Resolve a component name to a ClientAdapter.

    Lookup order:
    1. Bespoke adapter from REGISTRY (legacy clients with file-read fallback).
    2. Catalog entry with ``contract`` set → generic ``ContractAdapter``.
    3. None.
    """
    cls = REGISTRY.get(component_name)
    if cls is not None:
        return cls()

    catalog = _cached_catalog()
    from ..catalog import get_entry
    entry = get_entry(component_name, catalog)
    if entry and entry.contract:
        adapter = ContractAdapter()
        adapter.name = component_name
        adapter.binary = component_name
        return adapter
    return None


__all__ = [
    'ClientAdapter', 'ClientView', 'InstanceView',
    'ContractAdapter', 'HfTimestdAdapter', 'RadiodAdapter', 'WsprAdapter',
    'REGISTRY', 'load_adapter',
]
