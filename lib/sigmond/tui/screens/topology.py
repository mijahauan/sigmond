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
        self._enabled_col = None  # ColumnKey for the Enabled column

    def compose(self):
        yield Static("Topology — enabled components", id="topo-title")
        yield DataTable(id="topo-table", cursor_type="row")
        yield Button("Save topology.toml", id="topo-save", variant="primary")
        yield Static(
            "Click a row to select it, then click again or press Enter to toggle. Save when done.",
            id="topo-status",
        )

    def on_mount(self) -> None:
        table = self.query_one("#topo-table", DataTable)
        # Capture column keys so update_cell can reference them by key, not label.
        _comp_col, self._enabled_col, _mgd_col, _desc_col = table.add_columns(
            "Component", "Enabled", "Managed", "Description"
        )

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

        for name in sorted(self._topology.components):
            comp = self._topology.components[name]
            desc = comp.description or ""
            if not desc and name in self._catalog:
                desc = self._catalog[name].description
            enabled_str = "✔ yes" if comp.enabled else "✘ no"
            managed_str = "yes" if comp.managed else "no"
            table.add_row(name, enabled_str, managed_str, desc, key=name)

    def _set_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a single component and refresh its table row."""
        comp = self._topology.components.get(name)
        if comp is None:
            return
        comp.enabled = enabled
        table = self.query_one("#topo-table", DataTable)
        enabled_str = "✔ yes" if comp.enabled else "✘ no"
        try:
            table.update_cell(name, self._enabled_col, enabled_str)
        except Exception:
            pass  # row may not exist yet if catalog entry isn't in topology

    def _toggle_row(self, row_key) -> None:
        """Toggle a component with full cascade logic.

        Turning ON:  auto-enable every transitive dependency.
        Turning OFF: auto-disable any dep that is now orphaned — i.e. no
                     remaining enabled component requires it.
        """
        from sigmond.catalog import transitive_requires
        name = row_key.value if hasattr(row_key, 'value') else row_key
        comp = self._topology.components.get(name)
        if comp is None:
            return

        turning_on = not comp.enabled
        self._set_enabled(name, turning_on)

        if turning_on and self._catalog:
            auto_enabled: list[str] = []
            for dep in transitive_requires(name, self._catalog):
                dep_comp = self._topology.components.get(dep)
                if dep_comp is not None and not dep_comp.enabled:
                    self._set_enabled(dep, True)
                    auto_enabled.append(dep)
            msg = (f"(unsaved — also enabled: {', '.join(auto_enabled)})"
                   if auto_enabled else "(unsaved changes)")

        elif not turning_on and self._catalog:
            # Collect the enabled set after this disable (name is already off).
            enabled_now = {n for n, c in self._topology.components.items() if c.enabled}
            auto_disabled: list[str] = []
            for dep in transitive_requires(name, self._catalog):
                dep_comp = self._topology.components.get(dep)
                if dep_comp is None or not dep_comp.enabled:
                    continue
                # Keep the dep if any currently-enabled component still needs it.
                still_needed = any(
                    dep in transitive_requires(other, self._catalog)
                    for other in enabled_now
                    if other != dep
                )
                if not still_needed:
                    self._set_enabled(dep, False)
                    auto_disabled.append(dep)
                    enabled_now.discard(dep)
            msg = (f"(unsaved — also disabled: {', '.join(auto_disabled)})"
                   if auto_disabled else "(unsaved changes)")

        else:
            msg = "(unsaved changes)"

        self.query_one("#topo-status", Static).update(msg)

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
