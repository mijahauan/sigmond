"""Left-panel component tree with health indicators."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from textual.widgets import Tree

if TYPE_CHECKING:
    from ...topology import Topology


class ComponentTree(Tree):
    """Navigable tree of screens and components."""

    def __init__(self, **kwargs) -> None:
        super().__init__("SigMonD", **kwargs)

    def populate(self, topology: Topology, catalog: dict) -> None:
        """Build the tree from topology and catalog data."""
        self.clear()
        self.root.expand()

        # Screen nodes.
        self.root.add_leaf("\u2630 Topology", data={"screen": "topology"})

        # Component nodes.
        for name, comp in sorted(topology.components.items()):
            if not comp.enabled:
                continue
            health = _check_health(name)
            icon = "\u2714" if health else "\u2718"
            style = "green" if health else "red"
            label = f"[{style}]{icon}[/] {name}"
            desc = comp.description or (
                catalog[name].description if name in catalog else ""
            )
            if desc:
                label += f"  [dim]{desc}[/]"
            self.root.add_leaf(label, data={"component": name})

        self.root.add_leaf("\u2714 Validate", data={"screen": "validate"})

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data:
            return
        if data.get("screen") == "topology":
            self.app.action_show_topology()
        elif data.get("screen") == "validate":
            self.app.action_show_validate()


def _check_health(component: str) -> bool:
    """Quick check if any unit for this component is active."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", f"{component}*"],
            capture_output=True, text=True, timeout=2,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False
