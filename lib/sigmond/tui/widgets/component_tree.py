"""Left-panel navigation tree — grouped by intent (Configure/Observe/Operate).

The tree is the TUI's primary navigation surface.  Groups mirror the
operator's mental model: "I want to see what's running" (Observe),
"I want to change something" (Configure), "I want to do something"
(Operate).  Components do not appear as top-level entries — they show
up inside screens (Overview rollup, Radiod live, Lifecycle, Logs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Tree

if TYPE_CHECKING:
    from ...topology import Topology


class ComponentTree(Tree):
    """Grouped navigation tree for the sigmond TUI."""

    def __init__(self, **kwargs) -> None:
        super().__init__("SigMonD", **kwargs)

    def populate(self, topology: "Topology", catalog: dict) -> None:
        """Build the tree.  topology and catalog are accepted for
        parity with the prior signature and for future per-screen
        health rollups; current tree is static w.r.t. host state."""
        del topology, catalog  # unused for now; reserved for per-node health

        self.clear()
        self.root.expand()

        self.root.add_leaf("\u25a3 Overview", data={"screen": "overview"})

        configure = self.root.add("Configure", expand=True)
        configure.add_leaf("\u2630 Topology",     data={"screen": "topology"})
        configure.add_leaf("\u2699 CPU affinity", data={"screen": "cpu_affinity"})
        configure.add_leaf("\u21f5 CPU frequency", data={"screen": "cpu_freq"})
        configure.add_leaf("\u2193 Backup",        data={"screen": "backup"})
        configure.add_leaf("\u2191 Restore",       data={"screen": "restore"})

        observe = self.root.add("Observe", expand=True)
        observe.add_leaf("\u25c9 Radiod live", data={"screen": "radiod"})
        observe.add_leaf("\u2261 Logs",        data={"screen": "logs"})
        observe.add_leaf("\u2714 Validate",    data={"screen": "validate"})

        operate = self.root.add("Operate", expand=True)
        operate.add_leaf("\u21bb Lifecycle", data={"screen": "lifecycle"})
        operate.add_leaf("+ Install",        data={"screen": "install"})
        operate.add_leaf("\u2191 Update",    data={"screen": "update"})

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data:
            return
        screen = data.get("screen")
        if screen == "overview":
            self.app.action_show_overview()
        elif screen == "topology":
            self.app.action_show_topology()
        elif screen == "cpu_affinity":
            self.app.action_show_cpu_affinity()
        elif screen == "cpu_freq":
            self.app.action_show_cpu_freq()
        elif screen == "radiod":
            self.app.action_show_radiod()
        elif screen == "logs":
            self.app.action_show_logs()
        elif screen == "validate":
            self.app.action_show_validate()
        elif screen == "lifecycle":
            self.app.action_show_lifecycle()
        elif screen == "install":
            self.app.action_show_install()
        elif screen == "update":
            self.app.action_show_update()
        elif screen == "backup":
            self.app.action_show_backup()
        elif screen == "restore":
            self.app.action_show_restore()
