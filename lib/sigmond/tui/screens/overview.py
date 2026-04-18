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
    view: object = None                                       # SystemView | None
    affinity: object = None                                   # AffinityReport | None
    error: Optional[str] = None


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
        from ...sysview import build_system_view
        from ...cpu import build_affinity_report

        topology = load_topology()
        enabled = topology.enabled_components()

        try:
            units = resolve_units(enabled, enabled)
        except ValueError as exc:
            data.error = f"unit resolution: {exc}"
            return data

        for u in units:
            data.units_by_component.setdefault(u.component, []).append(u)

        data.unit_states = _batch_is_active([u.unit for u in units])

        try:
            data.view = build_system_view(topology=topology)
        except Exception as exc:
            data.view = None
            data.error = (data.error or "") + f" system_view: {exc}"

        try:
            data.affinity = build_affinity_report(dict(topology.cpu_affinity))
        except Exception:
            data.affinity = None
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
    if state == 'activating' or state == 'reloading':
        return f'[yellow]\u25b6 {state}[/]'
    return f'[yellow]? {state}[/]'


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
        yield Static("[dim]loading\u2026[/]", id="ov-summary")
        yield Static("Service health", classes="ov-section")
        table = DataTable(id="ov-services")
        table.add_columns("Component", "Unit", "State")
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
        self.query_one("#ov-summary", Static).update("[dim]loading\u2026[/]")
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

        summary = f"{len(data.units_by_component)} component(s), {active}/{total_units} services active"
        if failed:
            summary += f", [red]{failed} failed[/]"
        if data.error:
            summary += f"  [yellow](partial: {data.error})[/]"
        self.query_one("#ov-summary", Static).update(summary)

        self._render_services(data)
        self._render_inventory(data)
        self._render_cpu(data)

    def _render_services(self, data: _OverviewData) -> None:
        table = self.query_one("#ov-services", DataTable)
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
                lines.append(f"    \u2022 {inst.instance}{meta}")
            for issue in cv.issues:
                lines.append(f"    [yellow]\u26a0 {issue}[/]")
            lines.append("")

        # Trim trailing blank line.
        while lines and lines[-1] == "":
            lines.pop()
        widget.update("\n".join(lines) if lines else "[dim](no installed clients)[/]")

    def _render_cpu(self, data: _OverviewData) -> None:
        widget = self.query_one("#ov-cpu", Static)
        r = data.affinity
        if r is None or not r.radiod_cpus:
            widget.update("[dim](no local radiod)[/]")
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
                lines.append(f"  [yellow]\u26a0[/] {w}")
            lines.append("  [dim]fix:  sudo smd diag cpu-affinity --apply[/]")
        else:
            lines.append("  [green]\u2714 plan applied; no contention or foreign drop-ins[/]")
        widget.update("\n".join(lines))
