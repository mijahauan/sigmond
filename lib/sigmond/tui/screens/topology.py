"""Topology editor screen — enable/disable components."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static, Switch

if TYPE_CHECKING:
    from ...topology import Topology


class TopologyScreen(Vertical):
    """Component toggle table with save button."""

    DEFAULT_CSS = """
    TopologyScreen {
        padding: 1;
    }
    TopologyScreen #topo-title {
        text-style: bold;
        margin-bottom: 1;
    }
    TopologyScreen #topo-save {
        margin-top: 1;
        width: auto;
    }
    TopologyScreen #topo-status {
        margin-top: 1;
        color: $success;
    }
    """

    def __init__(self, topology: Topology, catalog: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self._topology = topology
        self._catalog = catalog

    def compose(self):
        yield Static("Topology — enabled components", id="topo-title")
        table = DataTable(id="topo-table", cursor_type="row")
        table.add_columns("Component", "Enabled", "Managed", "Description")
        yield table
        yield Button("Save topology.toml", id="topo-save", variant="primary")
        yield Static("Click a row to select it, then click again or press Enter to toggle. Save when done.", id="topo-status")

    def on_mount(self) -> None:
        # Merge catalog entries not yet in topology so new clients are visible.
        for cat_name, entry in self._catalog.items():
            if cat_name not in self._topology.components:
                from ...topology import Component
                self._topology.components[cat_name] = Component(
                    name=cat_name,
                    enabled=False,
                    managed=True,
                    description=entry.description,
                )

        table = self.query_one("#topo-table", DataTable)
        for name in sorted(self._topology.components):
            comp = self._topology.components[name]
            desc = comp.description or ""
            if not desc and name in self._catalog:
                desc = self._catalog[name].description
            enabled_str = "✔ yes" if comp.enabled else "✘ no"
            managed_str = "yes" if comp.managed else "no"
            table.add_row(name, enabled_str, managed_str, desc, key=name)

    def _toggle_row(self, row_key) -> None:
        """Toggle enabled state for a row."""
        name = row_key.value if hasattr(row_key, 'value') else row_key
        comp = self._topology.components.get(name)
        if comp is None:
            return
        comp.enabled = not comp.enabled
        table = self.query_one("#topo-table", DataTable)
        enabled_str = "✔ yes" if comp.enabled else "✘ no"
        table.update_cell(name, "Enabled", enabled_str)
        self.query_one("#topo-status", Static).update("(unsaved changes)")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Toggle on Enter key or click on the already-highlighted row."""
        self._toggle_row(event.row_key)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "topo-save":
            self._save_topology()

    def _save_topology(self) -> None:
        """Write the current topology state to topology.toml."""
        from ...paths import TOPOLOGY_PATH
        self._write_topology_toml(TOPOLOGY_PATH)
        self.query_one("#topo-status", Static).update(
            f"Saved to {TOPOLOGY_PATH}"
        )
        # Refresh the component tree in the app.
        from ..widgets.component_tree import ComponentTree
        tree = self.app.query_one(ComponentTree)
        tree.populate(self._topology, self._catalog)

    def _write_topology_toml(self, path: Path) -> None:
        """Render topology as TOML and write to disk."""
        lines = [
            "# /etc/sigmond/topology.toml",
            "# Managed by smd tui. Manual edits are fine too.",
            "",
        ]
        for name in sorted(self._topology.components):
            comp = self._topology.components[name]
            lines.append(f"[component.{name}]")
            lines.append(f'enabled = {"true" if comp.enabled else "false"}')
            if not comp.managed:
                lines.append("managed = false")
            if comp.description:
                lines.append(f'description = "{comp.description}"')
            lines.append("")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
