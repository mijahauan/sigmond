"""SystemView — the merged picture the harmonize rules see.

Assembled once per command invocation from:
  - the sigmond coordination config
  - every enabled client adapter's read_view()

Rules then operate purely on the SystemView and never touch the
filesystem themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .clients import REGISTRY, load_adapter
from .clients.base import ClientView
from .coordination import Coordination, load_coordination
from .environment import EnvironmentView
from .paths import COORDINATION_PATH
from .topology import Topology, load_topology


@dataclass
class SystemView:
    coordination: Coordination
    topology: Topology
    client_views: dict = field(default_factory=dict)    # component name -> ClientView
    environment: Optional[EnvironmentView] = None       # populated only by `smd environment`

    def is_enabled(self, component: str) -> bool:
        return self.topology.is_enabled(component)

    def client(self, name: str) -> Optional[ClientView]:
        return self.client_views.get(name)

    def all_instances(self):
        """Iterate (client_type, instance) across all client views."""
        for cv in self.client_views.values():
            for iv in cv.instances:
                yield cv.client_type, iv


def build_system_view(topology: Optional[Topology] = None,
                      coordination: Optional[Coordination] = None) -> SystemView:
    """Load everything needed to run harmonize rules."""
    topo = topology or load_topology()
    coord = coordination if coordination is not None else load_coordination(COORDINATION_PATH)

    client_views: dict = {}
    for comp in topo.enabled_components():
        if comp not in REGISTRY:
            continue
        # hf-timestd appears under both 'grape' (legacy) and 'hf-timestd' keys;
        # dedupe by reading each client type only once.
        adapter = load_adapter(comp)
        if adapter is None:
            continue
        cv = adapter.read_view()
        # Keep the topology component name as the dict key so downstream
        # code can re-ask "is 'grape' enabled?" without knowing the
        # adapter's canonical .name.
        client_views[comp] = cv

    return SystemView(coordination=coord, topology=topo, client_views=client_views)
