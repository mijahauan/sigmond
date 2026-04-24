"""Client adapters.

Each adapter knows how to read (Phase 1) and eventually write (Phase 2+)
a specific HamSCI client's coordination touch-points.  A generic
contract.py adapter (Phase 2) will replace most of these once clients ship
`<client> inventory --json` per docs/CLIENT-CONTRACT.md.
"""

from .base import ClientAdapter, ClientView, InstanceView
from .hftimestd import HfTimestdAdapter
from .radiod import RadiodAdapter
from .wspr import WsprAdapter

# Registry: topology component name → adapter class.
# Note: "grape" is the legacy topology key for hf-timestd.
REGISTRY = {
    'ka9q-radio': RadiodAdapter,
    'radiod':     RadiodAdapter,    # legacy topology alias
    'wspr':       WsprAdapter,
    'grape':      HfTimestdAdapter,
    'hf-timestd': HfTimestdAdapter,
}


def load_adapter(component_name: str) -> ClientAdapter | None:
    cls = REGISTRY.get(component_name)
    return cls() if cls else None


__all__ = [
    'ClientAdapter', 'ClientView', 'InstanceView',
    'HfTimestdAdapter', 'RadiodAdapter', 'WsprAdapter',
    'REGISTRY', 'load_adapter',
]
