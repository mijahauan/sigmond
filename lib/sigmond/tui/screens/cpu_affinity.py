"""CPU affinity screen — hardware topology, plan, observed state, contention.

Read-only.  Renders from ``build_affinity_report()``; no mutations.
Mutation lives on the CLI (``sudo smd diag cpu-affinity --apply``) for now.

Motivation: help operators see whether radiod's USB3/FFT path is actually
protected on this host — which CPUs it owns, whether governor/drop-ins
are in order, and whether any other process is pinned to its cores.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Optional

from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static

from ...paths import TOPOLOGY_PATH


def _format_cpu_list(cpus) -> str:
    """Render a CPU set as a compact range list, e.g. '0-3, 8, 10-11'."""
    if not cpus:
        return "(none)"
    sorted_cpus = sorted(cpus)
    parts: list = []
    start = prev = sorted_cpus[0]
    for c in sorted_cpus[1:]:
        if c == prev + 1:
            prev = c
            continue
        parts.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = c
    parts.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(parts)


def _load_cpu_affinity_config() -> Optional[dict]:
    """Return [cpu_affinity] from topology.toml, or None when absent."""
    try:
        with open(TOPOLOGY_PATH, 'rb') as f:
            return tomllib.load(f).get('cpu_affinity')
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return None


def _l3_island_for_core(core: set, l3_islands: list):
    """Return the CacheIsland that fully contains ``core``, or None."""
    for isle in l3_islands:
        if core.issubset(isle.cpus):
            return isle
    return None


class CPUAffinityScreen(Vertical):
    """Visualize CPU topology, affinity plan, and observed state."""

    DEFAULT_CSS = """
    CPUAffinityScreen {
        padding: 1;
    }
    CPUAffinityScreen #cpu-title {
        text-style: bold;
        margin-bottom: 1;
    }
    CPUAffinityScreen Static {
        margin-bottom: 1;
    }
    CPUAffinityScreen #cpu-rerun {
        margin-top: 1;
        width: auto;
    }
    """

    def compose(self):
        yield Static("CPU affinity — hardware, plan, observed", id="cpu-title")
        yield Static("loading\u2026", id="cpu-hw")
        yield Static("", id="cpu-coremap")
        yield Static("", id="cpu-plan")
        table = DataTable(id="cpu-observed")
        table.add_columns("Unit", "PID", "Role",
                          "systemd", "observed", "drop-in")
        yield table
        yield Static("", id="cpu-contention")
        yield Static("", id="cpu-warnings")
        yield Button("Re-run", id="cpu-rerun", variant="default")

    def on_mount(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cpu-rerun":
            self._refresh()

    def _refresh(self) -> None:
        try:
            from ...cpu import build_affinity_report
            cfg = _load_cpu_affinity_config()
            report = build_affinity_report(cfg)
        except Exception as exc:
            self.query_one("#cpu-hw", Static).update(
                f"[red]error building affinity report: {exc}[/]"
            )
            return

        self._render_hardware(report)
        self._render_coremap(report)
        self._render_plan(report)
        self._render_observed(report)
        self._render_contention(report)
        self._render_warnings(report)

    def _render_hardware(self, report) -> None:
        caps = report.capabilities
        gov_summary = _governor_summary(caps.governors)
        lines = [
            "[bold]Hardware[/]",
            (f"  {caps.logical_cpus} logical CPUs  \u2022  "
             f"{len(caps.physical_cores)} physical cores  \u2022  "
             f"{len(caps.l3_islands)} L3 island"
             f"{'s' if len(caps.l3_islands) != 1 else ''}"),
            f"  Governors: {gov_summary}",
            (f"  isolcpus: {_format_cpu_list(caps.cmdline_isolcpus)}  \u2022  "
             f"nohz_full: {_format_cpu_list(caps.cmdline_nohz_full)}"),
        ]
        self.query_one("#cpu-hw", Static).update("\n".join(lines))

    def _render_coremap(self, report) -> None:
        caps = report.capabilities
        radiod_cpus = report.radiod_cpus
        lines = [
            "[bold]Core map[/]  "
            "[red]\u25cf[/] radiod  "
            "[green]\u25cb[/] other  "
            "\u00b7 unused",
            "",
        ]

        # Group physical cores by L3 island (cores that span islands go in
        # an 'uncached' group — unusual but we handle it).
        grouped: dict = {}
        uncached: list = []
        for core in caps.physical_cores:
            isle = _l3_island_for_core(core, caps.l3_islands)
            if isle is None:
                uncached.append(core)
            else:
                grouped.setdefault(isle, []).append(core)

        for isle in sorted(grouped, key=lambda x: min(x.cpus)):
            lines.append(
                f"  L3 island  CPUs [{_format_cpu_list(isle.cpus)}]")
            for core in grouped[isle]:
                lines.append(self._core_row(core, radiod_cpus))
            lines.append("")
        if uncached:
            lines.append("  (cores outside L3 topology)")
            for core in uncached:
                lines.append(self._core_row(core, radiod_cpus))

        # Trim trailing blank line.
        while lines and lines[-1] == "":
            lines.pop()
        self.query_one("#cpu-coremap", Static).update("\n".join(lines))

    def _core_row(self, core: set, radiod_cpus: set) -> str:
        sorted_cpus = sorted(core)
        is_radiod = bool(core & radiod_cpus)
        glyph = "[red]\u25cf[/]" if is_radiod else "[green]\u25cb[/]"
        role = "radiod" if is_radiod else "other"
        siblings = " ".join(f"{c:>2}" for c in sorted_cpus)
        return (f"    core  CPUs [{siblings}]  "
                f"{glyph * len(sorted_cpus)}  {role}")

    def _render_plan(self, report) -> None:
        lines = ["[bold]Plan[/]"]
        if report.plan.radiod:
            for unit, cpus in sorted(report.plan.radiod.items()):
                lines.append(
                    f"  {unit}  \u2192  CPUs [{_format_cpu_list(cpus)}]")
        else:
            lines.append("  (no radiod instances detected)")
        lines.append(
            f"  other pool                      \u2192  CPUs "
            f"[{_format_cpu_list(report.plan.other_cpus)}]  "
            f"({len(report.plan.other_cpus)} CPUs)")
        self.query_one("#cpu-plan", Static).update("\n".join(lines))

    def _render_observed(self, report) -> None:
        table = self.query_one("#cpu-observed", DataTable)
        table.clear()
        for ua in report.units:
            pid = ua.main_pid or "—"
            sysd = _format_cpu_list(ua.systemd_mask) if ua.systemd_mask else "—"
            obs  = _format_cpu_list(ua.observed_mask) if ua.observed_mask else "—"
            if ua.mask_mismatch:
                obs = f"[yellow]{obs}[/]"
            drop = "smd" if ua.drop_in_present else (
                "[yellow]foreign[/]" if ua.foreign_drop_ins else "[dim]none[/]")
            table.add_row(ua.unit, pid, ua.role, sysd, obs, drop)

    def _render_contention(self, report) -> None:
        total = len(report.contention)
        pinned = [c for c in report.contention if not c.is_default]
        default = total - len(pinned)
        lines = ["[bold]Contention on radiod cores[/]"]
        if total == 0:
            lines.append("  [green]no processes observed on radiod cores[/]")
        else:
            if pinned:
                lines.append(
                    f"  [yellow]\u26a0[/]  {len(pinned)} process"
                    f"{'es' if len(pinned) != 1 else ''} explicitly pinned:")
                for c in pinned[:8]:
                    lines.append(
                        f"     \u2022 {c.comm} ({c.pid})  "
                        f"allowed=[{_format_cpu_list(c.allowed)}]  "
                        f"overlap=[{_format_cpu_list(c.overlap)}]")
                if len(pinned) > 8:
                    lines.append(f"     \u2026 (+{len(pinned) - 8} more)")
            if default:
                lines.append(
                    f"  [dim]\u2022 {default} process"
                    f"{'es' if default != 1 else ''} with default "
                    "full-range affinity (benign unless scheduled there)[/]")
        self.query_one("#cpu-contention", Static).update("\n".join(lines))

    def _render_warnings(self, report) -> None:
        lines = ["[bold]Warnings[/]"]
        if not report.warnings:
            lines.append("  [green]none[/]")
        else:
            for w in report.warnings:
                lines.append(f"  [yellow]\u26a0[/]  {w}")
        self.query_one("#cpu-warnings", Static).update("\n".join(lines))


def _governor_summary(governors: dict) -> str:
    """Return e.g. 'schedutil (16/16)' or 'performance (8/16), powersave (8/16)'."""
    if not governors:
        return "(no cpufreq sysfs)"
    total = len(governors)
    counts: dict = {}
    for gov in governors.values():
        counts[gov] = counts.get(gov, 0) + 1
    parts = [f"{gov} ({n}/{total})" for gov, n in sorted(counts.items())]
    return ", ".join(parts)
