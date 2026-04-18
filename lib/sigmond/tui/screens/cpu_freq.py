"""CPU frequency screen — read-only view of `smd diag cpu-freq`.

Shows the [cpu_freq] policy from topology.toml and each CPU's current
scaling_max_freq against it.  Apply lives on the CLI (requires root).

Motivation sits in the CPU-affinity memory: radiod cores need high
clock to keep the USB3/FFT path fed; everything else can stay
power-efficient.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState


@dataclass
class _FreqRow:
    cpu: int
    role: str                      # 'radiod' | 'other'
    current_mhz: Optional[int]
    target_mhz: int
    matches: bool
    note: str = ''


@dataclass
class _FreqData:
    radiod_max_mhz: int = 3200
    other_max_mhz: int = 1400
    radiod_cpus: set = field(default_factory=set)
    rows: list = field(default_factory=list)     # list[_FreqRow]
    error: Optional[str] = None


def _gather_freq_data() -> _FreqData:
    """Collect per-CPU scaling_max_freq against the topology.toml policy."""
    data = _FreqData()
    try:
        from ...topology import load_topology
        from ...cpu import get_radiod_cpus

        topology = load_topology()
        data.radiod_max_mhz = int(topology.cpu_freq.get('radiod_max_mhz', 3200))
        data.other_max_mhz = int(topology.cpu_freq.get('other_max_mhz', 1400))
        try:
            data.radiod_cpus = get_radiod_cpus()
        except Exception:
            data.radiod_cpus = set()
    except Exception as exc:
        data.error = str(exc)
        return data

    cpu_count = os.cpu_count() or 1
    for cpu in range(cpu_count):
        in_radiod = cpu in data.radiod_cpus
        role = 'radiod' if in_radiod else 'other'
        target_mhz = data.radiod_max_mhz if in_radiod else data.other_max_mhz

        freq_dir = Path(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq')
        if not freq_dir.exists():
            data.rows.append(_FreqRow(
                cpu=cpu, role=role,
                current_mhz=None, target_mhz=target_mhz,
                matches=False, note='no cpufreq sysfs',
            ))
            continue

        try:
            current_khz = int((freq_dir / 'scaling_max_freq').read_text().strip())
            current_mhz = current_khz // 1000
            data.rows.append(_FreqRow(
                cpu=cpu, role=role,
                current_mhz=current_mhz, target_mhz=target_mhz,
                matches=(current_mhz == target_mhz),
            ))
        except (OSError, ValueError) as exc:
            data.rows.append(_FreqRow(
                cpu=cpu, role=role,
                current_mhz=None, target_mhz=target_mhz,
                matches=False, note=f'read error: {exc}',
            ))
    return data


class CPUFreqScreen(Vertical):
    """Visualize CPU frequency policy and per-CPU state."""

    DEFAULT_CSS = """
    CPUFreqScreen {
        padding: 1;
    }
    CPUFreqScreen .cf-title {
        text-style: bold;
        margin-bottom: 1;
    }
    CPUFreqScreen Static {
        margin-bottom: 1;
    }
    CPUFreqScreen #cf-apply-hint {
        margin-top: 1;
        color: $text-muted;
    }
    CPUFreqScreen #cf-refresh {
        margin-top: 1;
        width: auto;
    }
    """

    def compose(self):
        yield Static("CPU frequency — policy and per-CPU state",
                     classes="cf-title")
        yield Static("[dim]loading\u2026[/]", id="cf-policy")
        table = DataTable(id="cf-table")
        table.add_columns("CPU", "Role", "Current MHz", "Target MHz", "Status")
        yield table
        yield Static("", id="cf-mismatch")
        yield Static(
            "To apply the policy:\n"
            "  [cyan bold]sudo smd diag cpu-freq --apply[/]",
            id="cf-apply-hint")
        yield Button("Refresh", id="cf-refresh", variant="default")

    def on_mount(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cf-refresh":
            self._refresh()

    def _refresh(self) -> None:
        self.query_one("#cf-policy", Static).update("[dim]loading\u2026[/]")
        self.run_worker(_gather_freq_data, thread=True)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        data = event.worker.result
        if not isinstance(data, _FreqData):
            return
        self._render_data(data)

    def _render_data(self, data: _FreqData) -> None:
        policy_widget = self.query_one("#cf-policy", Static)
        if data.error:
            policy_widget.update(f"[red]error: {data.error}[/]")
            return

        radiod_cpus_str = (sorted(data.radiod_cpus) if data.radiod_cpus
                           else "(no radiod running)")
        policy_lines = [
            f"[bold]Policy[/]: radiod CPUs \u2192 {data.radiod_max_mhz} MHz max  "
            f"\u2022  other CPUs \u2192 {data.other_max_mhz} MHz max",
            f"[bold]Radiod CPUs[/] (CPUAffinity): {radiod_cpus_str}",
        ]
        policy_widget.update("\n".join(policy_lines))

        table = self.query_one("#cf-table", DataTable)
        table.clear()
        mismatches = 0
        for r in data.rows:
            cur = f"{r.current_mhz}" if r.current_mhz is not None else "—"
            target = f"{r.target_mhz}"
            if r.note:
                status = f"[yellow]{r.note}[/]"
            elif r.matches:
                status = "[green]\u2714 ok[/]"
            else:
                status = "[yellow]\u2192 needs apply[/]"
                mismatches += 1
            role_fmt = ("[red]radiod[/]" if r.role == 'radiod' else "other")
            table.add_row(str(r.cpu), role_fmt, cur, target, status)

        mismatch_widget = self.query_one("#cf-mismatch", Static)
        if mismatches:
            mismatch_widget.update(
                f"[yellow]\u26a0[/]  {mismatches} CPU(s) do not match the policy")
        else:
            mismatch_widget.update(
                "[green]\u2714[/]  every CPU matches the policy")
