"""Radiod status screen — lightweight coordinator view via ka9q-python.

Shows what sigmond cares about: channel count, active frequencies,
frontend health (GPSDO, calibration), and aggregate SNR.  For the
full deep-dive, launches ka9q-python's own TUI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState

GPSDO_RUN_DIR = Path("/run/gpsdo")


class RadiodScreen(Vertical):
    """Coordinator-level radiod status display."""

    DEFAULT_CSS = """
    RadiodScreen {
        padding: 1;
    }
    RadiodScreen .section-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    RadiodScreen #radiod-status {
        margin-top: 1;
        color: $text-muted;
    }
    RadiodScreen #radiod-deep-dive {
        margin-top: 1;
        width: auto;
    }
    """

    def __init__(self, radiod_id: str, status_dns: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._radiod_id = radiod_id
        self._status_dns = status_dns

    def compose(self):
        yield Static(f"radiod: {self._radiod_id}", classes="section-title")
        yield Static(f"status address: {self._status_dns or '(not configured)'}",
                     id="radiod-addr")

        yield Static("Frontend", classes="section-title")
        frontend = DataTable(id="radiod-frontend")
        frontend.add_columns("Parameter", "Value")
        yield frontend

        yield Static("Active Channels  [dim](select a row to deep-dive a specific SSRC)[/]",
                     classes="section-title")
        channels = DataTable(id="radiod-channels", cursor_type="row", zebra_stripes=True)
        channels.add_columns("SSRC", "Frequency (MHz)", "Preset", "Sample Rate", "SNR (dB)")
        yield channels

        yield Static("", id="radiod-status")
        yield Button("Deep dive (ka9q tui)", id="radiod-deep-dive", variant="primary")
        yield Button("Deep dive (gpsdo tui)", id="radiod-gpsdo-dive", variant="primary")
        yield Button("Refresh", id="radiod-refresh", variant="default")

    def on_mount(self) -> None:
        self._poll_radiod()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "radiod-deep-dive":
            self._launch_ka9q_tui()
        elif event.button.id == "radiod-gpsdo-dive":
            self._launch_gpsdo_tui()
        elif event.button.id == "radiod-refresh":
            self._poll_radiod()

    def _poll_radiod(self) -> None:
        """Kick off a background worker to query radiod."""
        if not self._status_dns:
            self.query_one("#radiod-status", Static).update(
                "[yellow]No status_dns configured in coordination.toml[/]"
            )
            return
        self.query_one("#radiod-status", Static).update("Querying radiod...")
        self.run_worker(self._fetch_status, thread=True)

    def _fetch_status(self) -> dict:
        """Worker thread: query radiod via ka9q-python."""
        try:
            from ka9q import RadiodControl, discover_channels
        except ImportError:
            return {"error": "ka9q-python not installed"}

        result = {"channels": [], "frontend": {}}
        try:
            channel_dict = discover_channels(self._status_dns, listen_duration=2.0)
            for ssrc, ch in channel_dict.items():
                result["channels"].append({
                    "ssrc": ssrc,
                    "frequency": ch.frequency,
                    "preset": ch.preset,
                    "sample_rate": ch.sample_rate,
                    "snr": getattr(ch, "snr", None),
                })
        except Exception as exc:
            result["error"] = f"discover_channels: {exc}"
            return result

        try:
            with RadiodControl(self._status_dns) as control:
                if result["channels"]:
                    # Poll first channel for frontend info.
                    ssrc = result["channels"][0]["ssrc"]
                    status = control.poll_status(ssrc, timeout=2.0)
                    if status:
                        d = status.to_dict()
                        fe = d.get("frontend", {})
                        result["frontend"] = {
                            "gpsdo_lock": fe.get("lock", "?"),
                            "calibration_ppm": fe.get("calibrate", "?"),
                            "reference_hz": fe.get("reference", "?"),
                            "ad_overrange": fe.get("ad_over", "?"),
                            "lna_gain": fe.get("lna_gain", "?"),
                            "mixer_gain": fe.get("mixer_gain", "?"),
                            "if_gain": fe.get("if_gain", "?"),
                        }
        except Exception as exc:
            result["frontend_error"] = str(exc)

        return result

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        result = event.worker.result

        status_widget = self.query_one("#radiod-status", Static)

        if "error" in result:
            status_widget.update(f"[red]{result['error']}[/]")
            return

        # Populate frontend table.
        fe_table = self.query_one("#radiod-frontend", DataTable)
        fe_table.clear()
        fe = result.get("frontend", {})
        if fe:
            for key, val in fe.items():
                label = key.replace("_", " ").title()
                fe_table.add_row(label, str(val))
        elif "frontend_error" in result:
            fe_table.add_row("Error", result["frontend_error"])
        else:
            fe_table.add_row("Status", "No frontend data available")

        # Populate channels table.
        ch_table = self.query_one("#radiod-channels", DataTable)
        ch_table.clear()
        channels = result.get("channels", [])
        for ch in sorted(channels, key=lambda c: c.get("frequency", 0)):
            freq_mhz = f"{ch['frequency'] / 1e6:.6f}" if ch.get("frequency") else "?"
            snr = f"{ch['snr']:.1f}" if ch.get("snr") is not None else "—"
            ch_table.add_row(
                str(ch.get("ssrc", "?")),
                freq_mhz,
                ch.get("preset", "?"),
                str(ch.get("sample_rate", "?")),
                snr,
            )

        n = len(channels)
        fe_note = ""
        if "frontend_error" in result:
            fe_note = " (frontend query failed)"
        status_widget.update(
            f"[green]{n} active channel{'s' if n != 1 else ''}{fe_note}[/]"
        )

    def _selected_ssrc(self) -> str | None:
        """Return the SSRC of the currently selected channels-table row, or None."""
        table = self.query_one("#radiod-channels", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
            row = table.get_row(key)
        except Exception:
            return None
        # First column is SSRC; rendered as string.
        return str(row[0]) if row else None

    def _launch_ka9q_tui(self) -> None:
        """Suspend sigmond's TUI and launch ka9q-python's TUI.

        When a channel row is selected, pass its SSRC via ``--ssrc`` so
        ka9q-python's TUI opens focused on that channel.  Without a
        selection, launch the radiod-wide view.
        """
        status_widget = self.query_one("#radiod-status", Static)

        if not self._status_dns:
            status_widget.update("[yellow]Cannot launch — no status_dns configured[/]")
            return

        ka9q_bin = shutil.which("ka9q")
        if not ka9q_bin:
            status_widget.update(
                "[red]ka9q binary not found on PATH — install ka9q-python in this venv[/]"
            )
            return

        cmd = [ka9q_bin, "tui", self._status_dns]
        ssrc = self._selected_ssrc()
        if ssrc:
            cmd.extend(["--ssrc", ssrc])

        with self.app.suspend():
            result = subprocess.run(cmd)

        if result.returncode != 0:
            status_widget.update(
                f"[red]ka9q tui exited {result.returncode} — cmd: {' '.join(cmd)}[/]"
            )

    def _launch_gpsdo_tui(self) -> None:
        """Suspend sigmond's TUI and launch gpsdo-monitor's TUI.

        When a gpsdo-monitor report declares this radiod in its
        ``governs`` list we pass that device's serial via ``--serial``
        so the TUI opens focused on the governor.  Without a match
        (no reports published yet, or this radiod has no governor)
        we launch the unfiltered view.
        """
        status_widget = self.query_one("#radiod-status", Static)

        gpsdo_bin = shutil.which("gpsdo-monitor")
        if not gpsdo_bin:
            status_widget.update(
                "[red]gpsdo-monitor binary not found on PATH — "
                "install gpsdo-monitor[tui] in this venv[/]"
            )
            return

        cmd = [gpsdo_bin, "tui"]
        serial = self._governor_serial_for_radiod()
        if serial:
            cmd.extend(["--serial", serial])

        with self.app.suspend():
            result = subprocess.run(cmd)

        if result.returncode != 0:
            status_widget.update(
                f"[red]gpsdo tui exited {result.returncode} — cmd: {' '.join(cmd)}[/]"
            )

    def _governor_serial_for_radiod(self) -> Optional[str]:
        """Find the first device in /run/gpsdo/*.json whose ``governs``
        list names this radiod.  Returns None when gpsdo-monitor hasn't
        published anything yet, when no device claims this radiod, or
        on any file read error (we want the TUI to launch unfiltered
        rather than fail).
        """
        if not GPSDO_RUN_DIR.is_dir():
            return None
        target_tokens = {f"radiod:{self._radiod_id}", self._radiod_id}
        for path in sorted(GPSDO_RUN_DIR.glob("*.json")):
            if path.name == "index.json":
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("schema") != "v1":
                continue
            governs = data.get("governs") or []
            if not isinstance(governs, list):
                continue
            if any(g in target_tokens for g in governs if isinstance(g, str)):
                device = data.get("device")
                if isinstance(device, dict) and device.get("serial"):
                    return str(device["serial"])
        return None
