"""Lifecycle screen — TUI counterpart to `smd {start|stop|restart|reload}`.

Pick a component (or all), press a verb button.  A confirmation modal
previews the exact ``sudo smd <verb>`` command; on accept, the TUI
suspends, the CLI runs in the real terminal (operator sees password
prompt + live output), and the TUI resumes with an exit-code readout
and a fresh state table.

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
from textual.widgets import Button, DataTable, Select, Static
from textual.worker import Worker, WorkerState

from ..mutation import confirm_and_run


_ALL_COMPONENTS = "(all enabled)"


def _smd_binary() -> str:
    """Find the smd binary.  Prefer the one that launched this process
    (so dev runs of bin/smd keep using bin/smd, not a stale system copy).
    Fall back to $PATH lookup, then to the canonical install path.

    Guards against being run from pytest/__main__.py etc. by checking the
    basename is actually 'smd' before trusting sys.argv[0]."""
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


@dataclass
class _LifecycleData:
    components: list = field(default_factory=list)   # enabled component names
    units_by_component: dict = field(default_factory=dict)  # comp -> list[UnitRef]
    unit_states: dict = field(default_factory=dict)  # unit -> state
    error: Optional[str] = None


def _gather() -> _LifecycleData:
    data = _LifecycleData()
    try:
        from ...topology import load_topology
        from ...lifecycle import resolve_units
        from ..screens.overview import _batch_is_active

        topology = load_topology()
        data.components = topology.enabled_components()

        try:
            units = resolve_units(data.components, data.components)
        except ValueError as exc:
            data.error = f"unit resolution: {exc}"
            return data

        for u in units:
            data.units_by_component.setdefault(u.component, []).append(u)

        data.unit_states = _batch_is_active([u.unit for u in units])
    except Exception as exc:
        data.error = str(exc)
    return data


def _state_badge(state: str) -> str:
    if state == 'active':
        return '[green]\u2714 active[/]'
    if state == 'inactive':
        return '[dim]\u25cb inactive[/]'
    if state == 'failed':
        return '[red]\u2718 failed[/]'
    if state in ('activating', 'reloading', 'deactivating'):
        return f'[yellow]\u25b6 {state}[/]'
    return f'[yellow]? {state}[/]'


class LifecycleScreen(Vertical):
    """Start / stop / restart / reload managed services."""

    DEFAULT_CSS = """
    LifecycleScreen {
        padding: 1;
    }
    LifecycleScreen .lc-title {
        text-style: bold;
        margin-bottom: 1;
    }
    LifecycleScreen #lc-controls {
        height: 3;
        margin-bottom: 1;
    }
    LifecycleScreen #lc-controls Button {
        margin-right: 1;
    }
    LifecycleScreen .lc-section {
        text-style: bold;
        margin-top: 1;
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
            "Lifecycle — start, stop, restart, reload managed services",
            classes="lc-title")

        yield Static("[dim]loading\u2026[/]", id="lc-status")

        with Horizontal(id="lc-controls"):
            yield Select(options=[(_ALL_COMPONENTS, _ALL_COMPONENTS)],
                         id="lc-picker", prompt="Scope\u2026",
                         value=_ALL_COMPONENTS, allow_blank=False)
            yield Button("Start",   id="lc-start",   variant="success")
            yield Button("Stop",    id="lc-stop",    variant="error")
            yield Button("Restart", id="lc-restart", variant="warning")
            yield Button("Reload",  id="lc-reload",  variant="primary")

        yield Static("Current state", classes="lc-section")
        table = DataTable(id="lc-table")
        table.add_columns("Component", "Unit", "State")
        yield table

        yield Static("", id="lc-last")
        yield Button("Refresh", id="lc-refresh", variant="default")

    def on_mount(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "lc-refresh":
            self._refresh()
            return
        verb_map = {
            "lc-start":   "start",
            "lc-stop":    "stop",
            "lc-restart": "restart",
            "lc-reload":  "reload",
        }
        verb = verb_map.get(bid)
        if verb:
            self._run_verb(verb)

    def _refresh(self) -> None:
        self.query_one("#lc-status", Static).update("[dim]loading\u2026[/]")
        self.run_worker(_gather, thread=True, name="lc-gather")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        data = event.worker.result
        if not isinstance(data, _LifecycleData):
            return
        self._render_data(data)

    def _render_data(self, data: _LifecycleData) -> None:
        # Update the picker options to match the enabled components.
        picker = self.query_one("#lc-picker", Select)
        options = [(_ALL_COMPONENTS, _ALL_COMPONENTS)]
        options.extend((c, c) for c in data.components)
        picker.set_options(options)

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
        if not data.units_by_component:
            table.add_row("[dim](none)[/]", "[dim]no managed services[/]", "")
            return
        for comp in sorted(data.units_by_component):
            units = data.units_by_component[comp]
            for i, u in enumerate(units):
                state = data.unit_states.get(u.unit, 'unknown')
                orphan = "  [yellow][orphaned][/]" if u.orphaned else ""
                table.add_row(
                    comp if i == 0 else "",
                    f"{u.unit}{orphan}",
                    _state_badge(state),
                )

    def _current_scope(self) -> Optional[str]:
        picker = self.query_one("#lc-picker", Select)
        value = picker.value
        if value is None or value is Select.NULL:
            return None
        return str(value)

    def _run_verb(self, verb: str) -> None:
        scope = self._current_scope()
        if scope is None:
            self.query_one("#lc-last", Static).update(
                "[yellow]pick a scope first[/]")
            return

        cmd = [_smd_binary(), verb]
        if scope != _ALL_COMPONENTS:
            cmd.extend(['--components', scope])

        scope_label = 'all enabled components' if scope == _ALL_COMPONENTS else scope
        title = f"Confirm: {verb} {scope_label}"
        body = (f"This will run `sudo smd {verb}` against "
                f"{scope_label}.\n\nAre you sure?")

        self.query_one("#lc-last", Static).update(
            f"[dim]waiting for confirmation of {verb}\u2026[/]")
        confirm_and_run(
            self.app, title=title, body=body, cmd=cmd, sudo=True,
            on_complete=self._after_verb,
        )

    def _after_verb(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#lc-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]\u2714 exit 0[/]  {argv}")
        else:
            last.update(f"[red]\u2718 exit {result.returncode}[/]  {argv}")
        # Refresh the state table so the caller sees the new reality.
        self._refresh()
