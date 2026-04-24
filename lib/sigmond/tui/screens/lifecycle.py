"""Lifecycle screen — TUI counterpart to `smd {start|stop|restart}`.

Shows all topology-enabled components and their service states.  Highlight a
row and use the "Selected" buttons to act on one component, or use the "All"
buttons to act on every enabled component at once.

All mutations go through the CLI.  The CLI holds the lifecycle lock
(CONTRACT v0.5 §5.5); the TUI does not duplicate that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState

from ..mutation import confirm_and_run
from .overview import _component_status


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


@dataclass
class _LifecycleData:
    components: list = field(default_factory=list)          # all enabled component names
    units_by_component: dict = field(default_factory=dict)  # comp -> list[UnitRef]
    unit_states: dict = field(default_factory=dict)         # unit -> state string
    error: Optional[str] = None


def _gather() -> _LifecycleData:
    """Gather lifecycle state for topology-enabled components that have services.

    Only components with actual systemd units (or a well-known service pattern)
    are included.  Library-kind entries and client programs with no independent
    service (e.g. wspr-recorder) are excluded automatically.
    """
    data = _LifecycleData()
    try:
        from ...topology import load_topology
        from ...lifecycle import resolve_units
        from ...catalog import load_catalog
        from ..screens.overview import (
            _batch_is_active, _discover_service_units, _UNIT_PATTERNS)

        topology = load_topology()
        enabled = topology.enabled_components()

        # Exclude library-kind components (no systemd presence at all).
        try:
            catalog = load_catalog()
            library_names = {n for n, e in catalog.items() if e.kind == 'library'}
        except Exception:
            library_names = set()

        candidates = [c for c in enabled if c not in library_names]

        try:
            lc_units = resolve_units(candidates, candidates)
        except ValueError as exc:
            data.error = f"unit resolution: {exc}"
            lc_units = []

        for u in lc_units:
            data.units_by_component.setdefault(u.component, []).append(u)

        for comp in candidates:
            if comp in data.units_by_component:
                continue
            fallback = _discover_service_units(comp)
            data.units_by_component.setdefault(comp, []).extend(fallback)

        # Keep only components that either have units discovered OR have a known
        # service pattern (meaning they can have units once configured).
        # This drops pure-client programs like wspr-recorder that are invoked by
        # another component and have no independent systemd service.
        def _is_service_component(comp: str) -> bool:
            if data.units_by_component.get(comp):
                return True
            pattern = _UNIT_PATTERNS.get(comp)
            return pattern is not None  # None means library; absent means unknown client

        data.components = [c for c in candidates if _is_service_component(c)]

        all_unit_names = [u.unit for units in data.units_by_component.values()
                          for u in units]
        data.unit_states = _batch_is_active(all_unit_names)

    except Exception as exc:
        data.error = str(exc)
    return data



class LifecycleScreen(Vertical):
    """Start / stop / restart managed services."""

    DEFAULT_CSS = """
    LifecycleScreen {
        padding: 1;
    }
    LifecycleScreen .lc-title {
        text-style: bold;
        margin-bottom: 1;
    }
    LifecycleScreen #lc-btn-row {
        height: 3;
        margin-top: 1;
        margin-bottom: 0;
    }
    LifecycleScreen #lc-btn-row Button {
        margin-right: 1;
    }
    LifecycleScreen #lc-table {
        margin-top: 1;
        margin-bottom: 0;
    }
    LifecycleScreen #lc-hint {
        color: $text-muted;
        margin-top: 0;
        margin-bottom: 0;
    }
    LifecycleScreen #lc-last {
        margin-top: 1;
        color: $text-muted;
    }
    LifecycleScreen #lc-refresh {
        margin-top: 1;
        width: auto;
    }
    """

    def compose(self):
        yield Static(
            "Lifecycle — start, stop, restart managed services",
            classes="lc-title")

        yield Static("[dim]loading…[/]", id="lc-status")

        with Horizontal(id="lc-btn-row"):
            yield Button("▶ Start all",   id="lc-start",   variant="success")
            yield Button("■ Stop all",    id="lc-stop",    variant="error")
            yield Button("↺ Restart all", id="lc-restart", variant="warning")

        yield Static(
            "[dim]Highlight a row to target a single component.[/]",
            id="lc-hint")

        table = DataTable(id="lc-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Component", "Status")
        yield table

        yield Static("", id="lc-last")
        yield Button("Refresh", id="lc-refresh", variant="default")

    def on_mount(self) -> None:
        self._refresh()
        self._update_button_labels()

    # ------------------------------------------------------------------
    # button dispatch
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "lc-refresh":
            self._refresh()
            return
        comp = self._selected_component()
        if bid == "lc-start":
            self._verb_all("start") if comp is None else self._verb_one("start", comp)
        elif bid == "lc-stop":
            self._verb_all("stop") if comp is None else self._verb_one("stop", comp)
        elif bid == "lc-restart":
            self._verb_all("restart") if comp is None else self._verb_one("restart", comp)

    def on_data_table_cursor_moved(self, event: DataTable.CursorMoved) -> None:
        self._update_button_labels()

    def _update_button_labels(self) -> None:
        comp = self._selected_component()
        suffix = f" {comp}" if comp else " all"
        self.query_one("#lc-start",   Button).label = f"▶ Start{suffix}"
        self.query_one("#lc-stop",    Button).label = f"■ Stop{suffix}"
        self.query_one("#lc-restart", Button).label = f"↺ Restart{suffix}"

    # ------------------------------------------------------------------
    # data loading
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self.query_one("#lc-status", Static).update("[dim]loading…[/]")
        self.run_worker(_gather, thread=True, name="lc-gather")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        data = event.worker.result
        if not isinstance(data, _LifecycleData):
            return
        self._render_data(data)

    def _render_data(self, data: _LifecycleData) -> None:
        if data.error:
            self.query_one("#lc-status", Static).update(
                f"[yellow]partial: {data.error}[/]")
        else:
            total_units = sum(len(v) for v in data.units_by_component.values())
            active = sum(1 for s in data.unit_states.values() if s == 'active')
            self.query_one("#lc-status", Static).update(
                f"{len(data.components)} enabled component(s), "
                f"{active}/{total_units} service(s) active")

        table = self.query_one("#lc-table", DataTable)
        table.clear()

        if not data.components:
            table.add_row("[dim](none)[/]", "[dim]no enabled components[/]")
            self._update_button_labels()
            return

        for comp in sorted(data.components):
            units = data.units_by_component.get(comp, [])
            status = _component_status(units, data.unit_states)
            table.add_row(comp, status, key=comp)

        self._update_button_labels()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _selected_component(self) -> Optional[str]:
        """Return the component name for the highlighted row, or None if none."""
        table = self.query_one("#lc-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row = list(table.get_row_at(table.cursor_row))
            comp = str(row[0]) if row else ""
            return comp if comp and not comp.startswith('[') else None
        except Exception:
            return None

    def _verb_all(self, verb: str) -> None:
        smd = _smd_binary()
        confirm_and_run(
            self.app,
            title=f"Confirm: {verb} all",
            body=f"Run [bold]sudo smd {verb}[/] on all enabled components.\n\nAre you sure?",
            cmd=[smd, verb], sudo=True,
            on_complete=self._after_verb,
        )

    def _verb_one(self, verb: str, comp: str) -> None:
        smd = _smd_binary()
        confirm_and_run(
            self.app,
            title=f"Confirm: {verb} {comp}",
            body=f"Run [bold]sudo smd {verb} --components {comp}[/].\n\nAre you sure?",
            cmd=[smd, verb, '--components', comp], sudo=True,
            on_complete=self._after_verb,
        )

    def _after_verb(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#lc-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ exit 0[/]  {argv}")
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]  {argv}")
        self._refresh()
