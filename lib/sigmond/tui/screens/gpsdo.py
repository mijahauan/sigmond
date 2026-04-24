"""GPSDO live screen — coordinator view of gpsdo-monitor reports.

Reads ``/run/gpsdo/*.json`` (schema v1) and renders per-device health,
output configuration, and the radiod(s) each device governs.  For the
full deep-dive, suspends sigmond and launches ``gpsdo-monitor tui``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static

GPSDO_RUN_DIR = Path("/run/gpsdo")


def _find_tool(name: str) -> Optional[str]:
    """Look for a venv-installed tool next to sys.executable before PATH."""
    venv_bin = Path(sys.executable).parent / name
    if venv_bin.is_file():
        return str(venv_bin)
    return shutil.which(name)


def _load_reports() -> list[dict]:
    """Return a list of per-device report dicts from /run/gpsdo/*.json."""
    if not GPSDO_RUN_DIR.is_dir():
        return []
    reports: list[dict] = []
    for path in sorted(GPSDO_RUN_DIR.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("schema") == "v1":
            reports.append(data)
    return reports


def _yn(val: object) -> str:
    if val is True:
        return "[green]yes[/]"
    if val is False:
        return "[red]no[/]"
    return "[dim]—[/]"


def _hz_mhz(hz: object) -> str:
    if isinstance(hz, (int, float)) and hz:
        return f"{hz / 1e6:.6f} MHz"
    return "[dim]—[/]"


class GpsdoScreen(Vertical):
    """Per-device GPSDO live status."""

    DEFAULT_CSS = """
    GpsdoScreen {
        padding: 1;
    }
    GpsdoScreen .section-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    GpsdoScreen #gpsdo-status {
        margin-top: 1;
        color: $text-muted;
    }
    GpsdoScreen Button {
        margin-top: 1;
        width: auto;
    }
    """

    def compose(self):
        yield Static("GPSDO live", classes="section-title")
        yield Static("", id="gpsdo-status")

        yield Static("Devices", classes="section-title")
        devices = DataTable(id="gpsdo-devices", cursor_type="row",
                            zebra_stripes=True)
        devices.add_columns(
            "Serial", "Model", "A-level", "PLL", "GPS fix",
            "Sats", "Antenna", "Governs",
        )
        yield devices

        yield Static("Outputs", classes="section-title")
        outputs = DataTable(id="gpsdo-outputs", zebra_stripes=True)
        outputs.add_columns("Serial", "OUT1", "OUT2", "PPS")
        yield outputs

        yield Button("Deep dive (gpsdo tui)", id="gpsdo-dive",
                     variant="primary")
        yield Button("Refresh", id="gpsdo-refresh", variant="default")

    def on_mount(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "gpsdo-dive":
            self._launch_gpsdo_tui()
        elif event.button.id == "gpsdo-refresh":
            self._refresh()

    def _refresh(self) -> None:
        status = self.query_one("#gpsdo-status", Static)
        dev_table = self.query_one("#gpsdo-devices", DataTable)
        out_table = self.query_one("#gpsdo-outputs", DataTable)
        dev_table.clear()
        out_table.clear()

        if not GPSDO_RUN_DIR.is_dir():
            status.update(
                "[yellow]/run/gpsdo not present — is gpsdo-monitor running?[/]"
            )
            return

        reports = _load_reports()
        if not reports:
            status.update(
                "[yellow]No gpsdo reports published yet in /run/gpsdo/[/]"
            )
            return

        for r in reports:
            dev = r.get("device") or {}
            health = r.get("health") or {}
            outputs = r.get("outputs") or {}
            serial = dev.get("serial", "?")
            governs = ", ".join(r.get("governs") or []) or "[dim]—[/]"

            a_level = r.get("a_level_hint", "?")
            a_badge = (f"[green]{a_level}[/]" if a_level == "A1"
                       else f"[yellow]{a_level}[/]")

            dev_table.add_row(
                serial,
                dev.get("model", "?"),
                a_badge,
                _yn(health.get("pll_locked")),
                str(health.get("gps_fix") or "—"),
                str(health.get("sats_used") or "—"),
                _yn(health.get("antenna_ok")),
                governs,
            )
            out_table.add_row(
                serial,
                _hz_mhz(outputs.get("out1_hz")),
                _hz_mhz(outputs.get("out2_hz")),
                _yn(outputs.get("pps_enabled")),
            )

        n = len(reports)
        status.update(
            f"[green]{n} device{'s' if n != 1 else ''} reporting[/]"
        )

    def _selected_serial(self) -> str | None:
        table = self.query_one("#gpsdo-devices", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            key = table.coordinate_to_cell_key(
                (table.cursor_row, 0)).row_key
            row = table.get_row(key)
        except Exception:
            return None
        return str(row[0]) if row else None

    def _launch_gpsdo_tui(self) -> None:
        status = self.query_one("#gpsdo-status", Static)

        gpsdo_bin = _find_tool("gpsdo-monitor")
        if not gpsdo_bin:
            status.update(
                "[red]gpsdo-monitor binary not found — "
                "install gpsdo-monitor[tui] in this venv[/]"
            )
            return

        cmd = [gpsdo_bin, "tui"]
        serial = self._selected_serial()
        if serial:
            cmd.extend(["--serial", serial])

        with self.app.suspend():
            result = subprocess.run(cmd)

        if result.returncode != 0:
            status.update(
                f"[red]gpsdo tui exited {result.returncode} — "
                f"cmd: {' '.join(cmd)}[/]"
            )
        else:
            self._refresh()
