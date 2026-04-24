"""Overview screen — TUI counterpart to ``smd status``.

One landing pane that rolls up service health, client inventory, and
the CPU-affinity summary.  Read-only; mutations live in the Lifecycle,
Install, and Update screens.

Data-gathering runs in a background worker so the UI stays responsive
when systemctl calls are slow.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Optional

from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState


@dataclass
class _OverviewData:
    units_by_component: dict = field(default_factory=dict)   # comp -> list[UnitRef]
    unit_states: dict = field(default_factory=dict)          # unit -> state string
    action_items: list = field(default_factory=list)         # (kind, subject, action) tuples
    topology_enabled: set = field(default_factory=set)       # components enabled in topology
    view: object = None                                       # SystemView | None
    affinity: object = None                                   # AffinityReport | None
    error: Optional[str] = None


# Per-component well-known systemd unit patterns for components that
# don't ship a deploy.toml.  None means "library / no systemd presence".
_UNIT_PATTERNS: dict = {
    'ka9q-radio':    'radiod@*.service',
    'igmp-querier':  'igmp-querier.service',
    'ka9q-python':   None,
    'ka9q-web':      'ka9q-web.service',
    'gpsdo-monitor': 'gpsdo-monitor.service',
}


def _discover_service_units(comp: str) -> list:
    """Query systemctl for units belonging to a component without deploy.toml.

    Returns a list of UnitRef-compatible objects (uses lifecycle.UnitRef).
    Returns an empty list if the component has no systemd presence.
    """
    import json
    from ...lifecycle import UnitRef

    pattern = _UNIT_PATTERNS.get(comp, f'{comp}@*.service')
    if pattern is None:
        return []  # library — no units expected

    try:
        r = subprocess.run(
            ['systemctl', 'list-units', pattern, '--all', '--output=json'],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            items = json.loads(r.stdout)
            refs = []
            for it in items:
                unit = it.get('unit') or it.get('name', '')
                if unit:
                    kind = unit.rsplit('.', 1)[-1] if '.' in unit else 'service'
                    refs.append(UnitRef(component=comp, unit=unit,
                                        template=None, instance=None,
                                        kind=kind, source='systemctl'))
            return refs
    except Exception:
        pass
    return []


def _batch_is_active(unit_names: list) -> dict:
    """Call ``systemctl is-active unit1 unit2 ...`` once and return
    {unit: state}.  systemd returns a line per unit on stdout in the
    same order, and exits non-zero if any unit is inactive — which we
    ignore because we want the per-unit verdict from stdout either way.
    """
    if not unit_names:
        return {}
    try:
        r = subprocess.run(
            ['systemctl', 'is-active', *unit_names],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return {u: 'unknown' for u in unit_names}
    lines = r.stdout.strip().split('\n')
    result: dict = {}
    for i, unit in enumerate(unit_names):
        result[unit] = lines[i].strip() if i < len(lines) else 'unknown'
    return result


def _gather_overview() -> _OverviewData:
    """Build _OverviewData from the live host.  Intended to run in a
    background worker; does multiple systemctl calls."""
    data = _OverviewData()
    try:
        from ...topology import load_topology
        from ...lifecycle import resolve_units
        from ...catalog import load_catalog, next_steps
        from ...sysview import build_system_view
        from ...cpu import build_affinity_report

        topology = load_topology()
        enabled = topology.enabled_components()
        data.topology_enabled = set(enabled)

        catalog = load_catalog()

        # Step 1: resolve lifecycle-tracked units for topology-enabled components.
        try:
            lc_units = resolve_units(enabled, enabled)
        except ValueError as exc:
            data.error = f"unit resolution: {exc}"
            lc_units = []

        for u in lc_units:
            data.units_by_component.setdefault(u.component, []).append(u)

        # Step 2: expand to ALL installed catalog components so the overview
        # shows the complete picture regardless of topology enable state.
        installed_comps = {
            name for name, entry in catalog.items() if entry.is_installed()
        }
        all_comps = installed_comps | set(enabled)

        for comp in sorted(all_comps):
            if comp in data.units_by_component:
                continue  # already has lifecycle-tracked units
            fallback = _discover_service_units(comp)
            # Always register the component (even empty) so it appears in the table.
            data.units_by_component.setdefault(comp, []).extend(fallback)

        # Step 3: batch is-active for all units discovered.
        all_unit_names = [u.unit for units in data.units_by_component.values()
                          for u in units]
        data.unit_states = _batch_is_active(all_unit_names)

        try:
            data.view = build_system_view(topology=topology)
        except Exception as exc:
            data.view = None
            data.error = (data.error or "") + f" system_view: {exc}"

        try:
            data.affinity = build_affinity_report(dict(topology.cpu_affinity))
        except Exception:
            data.affinity = None

        try:
            data.action_items = next_steps(enabled, catalog)
        except Exception:
            data.action_items = []

    except Exception as exc:
        data.error = str(exc)
    return data


def _component_status(units: list, unit_states: dict) -> str:
    """Summarise all units of a component into one status string."""
    if not units:
        return "[dim]— no units[/]"
    states = [unit_states.get(u.unit, 'unknown') for u in units]
    n = len(states)
    n_active   = sum(1 for s in states if s == 'active')
    n_failed   = sum(1 for s in states if s == 'failed')
    n_inactive = sum(1 for s in states if s == 'inactive')
    n_trans    = sum(1 for s in states
                     if s in ('activating', 'reloading', 'deactivating'))
    if n_failed:
        return f"[red]✘ {n_failed} failed[/]" + (
            f"  ({n_active} active)" if n_active else "")
    if n_trans:
        return f"[yellow]▶ transitioning[/] ({n_active}/{n} active)"
    if n_active == n:
        return "[green]✔ running[/]" + (f" ({n} units)" if n > 1 else "")
    if n_active > 0:
        return f"[yellow]▶ partial[/] ({n_active}/{n} active)"
    return "[dim]○ stopped[/]" + (f" ({n} units)" if n > 1 else "")


class OverviewScreen(Vertical):
    """Service health + client inventory + CPU-affinity summary."""

    DEFAULT_CSS = """
    OverviewScreen {
        padding: 1;
    }
    OverviewScreen .ov-title {
        text-style: bold;
        margin-bottom: 1;
    }
    OverviewScreen .ov-section {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    OverviewScreen Static {
        margin-bottom: 0;
    }
    OverviewScreen #ov-summary {
        margin-bottom: 0;
    }
    OverviewScreen #ov-actions {
        margin-bottom: 1;
    }
    OverviewScreen #ov-inventory,
    OverviewScreen #ov-cpu {
        margin-top: 0;
        margin-bottom: 1;
    }
    OverviewScreen #ov-refresh {
        margin-top: 1;
        width: auto;
    }
    """

    def compose(self):
        yield Static("Overview", classes="ov-title")
        yield Static("[dim]loading…[/]", id="ov-summary")
        yield Static("", id="ov-actions")
        yield Static("Service health", classes="ov-section")
        table = DataTable(id="ov-services")
        table.add_columns("Component", "Status")
        yield table
        yield Static("Client inventory", classes="ov-section")
        yield Static("", id="ov-inventory")
        yield Static("CPU affinity", classes="ov-section")
        yield Static("", id="ov-cpu")
        yield Button("Refresh", id="ov-refresh", variant="default")

    def on_mount(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ov-refresh":
            self._refresh()

    def _refresh(self) -> None:
        self.query_one("#ov-summary", Static).update("[dim]loading…[/]")
        self.query_one("#ov-actions", Static).update("")
        self.run_worker(_gather_overview, thread=True)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        data = event.worker.result
        if not isinstance(data, _OverviewData):
            return
        self._render_data(data)

    def _render_data(self, data: _OverviewData) -> None:
        total_units = sum(len(v) for v in data.units_by_component.values())
        active = sum(1 for s in data.unit_states.values() if s == 'active')
        failed = sum(1 for s in data.unit_states.values() if s == 'failed')

        enabled_count = len(data.topology_enabled)
        summary = f"{len(data.units_by_component)} components ({enabled_count} enabled in topology), {active}/{total_units} services active"
        if failed:
            summary += f", [red]{failed} failed[/]"
        if data.error:
            summary += f"  [yellow](partial: {data.error})[/]"
        self.query_one("#ov-summary", Static).update(summary)

        self._render_actions(data)
        self._render_services(data)
        self._render_inventory(data)
        self._render_cpu(data)

    def _render_actions(self, data: _OverviewData) -> None:
        widget = self.query_one("#ov-actions", Static)
        if not data.action_items:
            widget.update("")
            return
        lines = ["[bold yellow]⚠ Action needed:[/]"]
        for kind, subject, action in data.action_items:
            if kind == 'install':
                lines.append(f"  [yellow]• {subject}[/] — not yet installed")
                lines.append(f"    [dim]run:  {action}[/]")
            elif kind == 'enable_dep':
                lines.append(f"  [yellow]• {subject}[/]")
                lines.append(f"    [dim]fix:  {action}[/]")
        widget.update("\n".join(lines))

    def _render_services(self, data: _OverviewData) -> None:
        table = self.query_one("#ov-services", DataTable)
        table.clear()
        if not data.units_by_component:
            table.add_row("[dim](none)[/]", "[dim]no managed services[/]")
            return
        for comp in sorted(data.units_by_component):
            units = data.units_by_component[comp]
            in_topo = comp in data.topology_enabled
            # "unmanaged" = installed/active but not under sigmond topology control
            comp_label = comp if in_topo else f"[dim]{comp} (unmanaged)[/]"
            status = _component_status(units, data.unit_states)
            table.add_row(comp_label, status)

    def _render_inventory(self, data: _OverviewData) -> None:
        widget = self.query_one("#ov-inventory", Static)
        if data.view is None or not data.view.client_views:
            widget.update("[dim](no client inventory available)[/]")
            return

        lines: list = []
        for name, cv in sorted(data.view.client_views.items()):
            if not cv.installed:
                continue
            contract = cv.contract_version or '?'
            header = f"[bold]{name}[/]  contract={contract}"
            lines.append(header)
            for inst in cv.instances:
                parts: list = []
                if inst.ka9q_channels:
                    parts.append(f"{inst.ka9q_channels} ch")
                if inst.frequencies_hz:
                    parts.append(f"{len(inst.frequencies_hz)} freqs")
                meta = f'  ({", ".join(parts)})' if parts else ""
                lines.append(f"    • {inst.instance}{meta}")
            for issue in cv.issues:
                lines.append(f"    [yellow]⚠ {issue}[/]")
            lines.append("")

        while lines and lines[-1] == "":
            lines.pop()
        widget.update("\n".join(lines) if lines else "[dim](no installed clients)[/]")

    def _render_cpu(self, data: _OverviewData) -> None:
        from pathlib import Path
        widget = self.query_one("#ov-cpu", Static)
        r = data.affinity
        if r is None or not r.radiod_cpus:
            ka9q_installed = Path('/opt/git/ka9q-radio').exists()
            if ka9q_installed:
                widget.update("[dim](ka9q-radio installed — no active radiod@ service; start it to see affinity)[/]")
            else:
                widget.update("[dim](ka9q-radio not installed)[/]")
            return

        lines = [f"radiod cores: {sorted(r.radiod_cpus)}  "
                 f"(other pool: {len(r.plan.other_cpus)} CPUs)"]
        foreign = sum(len(u.foreign_drop_ins) for u in r.units)
        unenforced = sum(1 for u in r.units
                         if u.role == 'radiod' and u.main_pid
                         and not u.drop_in_present)
        pinned = sum(1 for c in r.contention if not c.is_default)
        bad_gov = [cpu for cpu in r.radiod_cpus
                   if r.capabilities.governors.get(cpu) not in (None, 'performance')]

        warnings: list = []
        if unenforced:
            warnings.append(f"{unenforced} radiod unit(s) unenforced")
        if foreign:
            warnings.append(f"{foreign} foreign drop-in(s)")
        if pinned:
            warnings.append(f"{pinned} pinned process(es) overlap")
        if bad_gov:
            warnings.append(f"governor not 'performance' on cpus {sorted(bad_gov)}")

        if warnings:
            for w in warnings:
                lines.append(f"  [yellow]⚠[/] {w}")
            lines.append("  [dim]fix:  sudo smd diag cpu-affinity --apply[/]")
        else:
            lines.append("  [green]✔ plan applied; no contention or foreign drop-ins[/]")
        widget.update("\n".join(lines))
