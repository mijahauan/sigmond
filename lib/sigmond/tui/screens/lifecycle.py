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
from textual.widgets import Button, DataTable, Select, Static
from textual.worker import Worker, WorkerState

from ..mutation import confirm_and_run
from .overview import _component_status


# Sentinel for the "no instance selected" Select value.
_NO_INSTANCE = "__none__"


def _list_templated_units() -> list[tuple[str, str]]:
    """Walk systemctl-loaded `<client>@<instance>.service` units.

    Returns a list of (unit_label, unit_name) suitable for a Select
    widget, sorted by client then instance.  Includes both active
    and currently-stopped-but-enabled units so the operator can
    start a stopped instance.

    Excludes transient inactive entries that systemd loaded as a
    side-effect of `systemctl status <typo>@<bogus>` — those have no
    persistent enable state and don't reflect anything actually
    configured.  list-units (without --all) excludes dead inactive
    units; list-unit-files contributes the persistently-enabled
    instances that may currently be stopped.
    """
    # Templated services we recognise here are the reporter-keyed
    # recorder clients (instance == reporter_id — mirrors
    # sigmond.instance._TEMPLATED_RECORDER_CLIENTS).  hf-timestd is
    # a singleton (one PSWS station_id per host); its per-frequency
    # `timestd-metrology@<channel>` workers are internal sub-services
    # of that one instance, not separate reporter instances, so they
    # are intentionally NOT listed here — they would invite the
    # operator to start/stop individual channels from the
    # "per-instance" surface as if they were peer instances of
    # hf-timestd, which they are not.
    known = ("psk-recorder", "wspr-recorder", "hfdl-recorder",
             "codar-sounder")

    states: dict[tuple[str, str], str] = {}

    # Currently-loaded units (active / failed; not dead inactive
    # transients).
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--no-legend", "--no-pager",
             "--plain", "--type=service"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result and result.returncode == 0:
        for line in (result.stdout or "").splitlines():
            cols = line.split(None, 4)
            if len(cols) < 4:
                continue
            unit_name = cols[0]
            active = cols[2]
            if not unit_name.endswith(".service"):
                continue
            base = unit_name[:-len(".service")]
            if "@" not in base:
                continue
            client, _, instance = base.partition("@")
            if client not in known or not instance:
                continue
            states[(client, instance)] = active

    # Persistently-enabled instances that may currently be stopped.
    try:
        result = subprocess.run(
            ["systemctl", "list-unit-files", "--no-legend", "--no-pager",
             "--type=service"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result and result.returncode == 0:
        for line in (result.stdout or "").splitlines():
            cols = line.split(None, 3)
            if len(cols) < 2:
                continue
            unit_name, state = cols[0], cols[1]
            if state != 'enabled':
                continue
            if not unit_name.endswith(".service"):
                continue
            base = unit_name[:-len(".service")]
            if "@" not in base:
                continue
            client, _, instance = base.partition("@")
            if client not in known or not instance:
                continue
            states.setdefault((client, instance), "inactive")

    out: list[tuple[str, str]] = []
    for (client, instance), active in sorted(states.items()):
        label = f"{client}@{instance}  [{active}]"
        unit_name = f"{client}@{instance}.service"
        out.append((label, unit_name))
    return out


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
    LifecycleScreen .lc-section {
        text-style: bold;
        margin-top: 2;
        margin-bottom: 1;
    }
    LifecycleScreen .lc-body {
        color: $text-muted;
        margin-bottom: 1;
    }
    LifecycleScreen #lc-i-row {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }
    LifecycleScreen #lc-i-row Button {
        margin-right: 1;
    }
    LifecycleScreen #lc-i-select {
        width: 60;
        margin-right: 2;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._target: Optional[str] = None   # None → "all", str → single component

    def compose(self):
        yield Static(
            "Lifecycle — start, stop, restart managed services",
            classes="lc-title")

        yield Static("[dim]loading…[/]", id="lc-status")

        with Horizontal(id="lc-btn-row"):
            yield Button("▶ Start all",   id="lc-start",   variant="success")
            yield Button("■ Stop all",    id="lc-stop",    variant="error")
            yield Button("↺ Restart all", id="lc-restart", variant="warning")
            yield Button("⟳ Reload all",  id="lc-reload",  variant="warning")
            yield Button("☐ All",         id="lc-clear",   variant="primary")

        yield Static(
            "[dim]Click a row to target one component; click again or press ☐ All to clear.[/]",
            id="lc-hint")

        table = DataTable(id="lc-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Component", "Status")
        yield table

        yield Static("", id="lc-last")
        yield Button("Refresh", id="lc-refresh", variant="default")

        # ---- Per-instance section --------------------------------------
        yield Static("Per-instance units", classes="lc-section")
        yield Static(
            "Target one specific `<client>@<instance>.service` unit. "
            "Lists every templated recorder unit known to systemctl "
            "(active or inactive).  Buttons here shell out to "
            "`sudo systemctl <verb> <unit>` directly — sigmond's "
            "lifecycle lock is for cross-component operations, which "
            "single-unit actions don't need.",
            classes="lc-body")
        instance_options = _list_templated_units()
        if not instance_options:
            instance_options = [("(no templated units found)", _NO_INSTANCE)]
        with Horizontal(id="lc-i-row"):
            yield Select(
                instance_options,
                value=instance_options[0][1] if instance_options else _NO_INSTANCE,
                id="lc-i-select",
                allow_blank=False,
            )
            yield Button("▶ Start",   id="lc-i-start",   variant="success")
            yield Button("■ Stop",    id="lc-i-stop",    variant="error")
            yield Button("↺ Restart", id="lc-i-restart", variant="warning")
            yield Button("⟳ Reload",  id="lc-i-reload",  variant="warning")

    def on_mount(self) -> None:
        self._refresh()

    # ------------------------------------------------------------------
    # button dispatch
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "lc-refresh":
            self._refresh()
            return
        if bid == "lc-clear":
            self._target = None
            self._update_button_labels()
            return
        if bid == "lc-start":
            self._verb_all("start") if self._target is None else self._verb_one("start", self._target)
        elif bid == "lc-stop":
            self._verb_all("stop") if self._target is None else self._verb_one("stop", self._target)
        elif bid == "lc-restart":
            self._verb_all("restart") if self._target is None else self._verb_one("restart", self._target)
        elif bid == "lc-reload":
            self._verb_all("reload") if self._target is None else self._verb_one("reload", self._target)
        # Per-instance buttons — shell directly to `sudo systemctl <verb> <unit>`.
        elif bid in ("lc-i-start", "lc-i-stop", "lc-i-restart", "lc-i-reload"):
            unit = self.query_one("#lc-i-select", Select).value
            if unit in (None, Select.BLANK, _NO_INSTANCE):
                self.query_one("#lc-last", Static).update(
                    "[red]Per-instance: select a unit first[/]")
                return
            verb_map = {
                "lc-i-start":   "start",
                "lc-i-stop":    "stop",
                "lc-i-restart": "restart",
                "lc-i-reload":  "reload-or-restart",
            }
            verb = verb_map[bid]
            self._verb_one_unit(verb, str(unit))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Click or Enter on a row: select it (or deselect if already selected)."""
        key = event.row_key.value if hasattr(event.row_key, 'value') else str(event.row_key)
        if self._target == key:
            self._target = None   # second click deselects
        else:
            self._target = key
        self._update_button_labels()

    def _update_button_labels(self) -> None:
        comp = self._target
        suffix = f" {comp}" if comp else " all"
        self.query_one("#lc-start",   Button).label = f"▶ Start{suffix}"
        self.query_one("#lc-stop",    Button).label = f"■ Stop{suffix}"
        self.query_one("#lc-restart", Button).label = f"↺ Restart{suffix}"
        self.query_one("#lc-reload",  Button).label = f"⟳ Reload{suffix}"

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

    def _verb_one_unit(self, verb: str, unit: str) -> None:
        """Per-instance action: shell directly to `sudo systemctl <verb> <unit>`.

        Bypasses sigmond's component-level lifecycle plumbing because
        single-unit actions don't need the cross-component lifecycle
        lock.  Sigmond's `smd start/stop/restart` still own the
        "all enabled" and "by component" paths above.
        """
        confirm_and_run(
            self.app,
            title=f"Confirm: systemctl {verb} {unit}",
            body=f"Run [bold]sudo systemctl {verb} {unit}[/].\n\nAre you sure?",
            cmd=['systemctl', verb, unit], sudo=True,
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
