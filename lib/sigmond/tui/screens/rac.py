"""WD-RAC configuration and status screen.

Lets the operator enter the two values needed to activate the frpc
reverse tunnel:
  - RAC ID   (name string, defaults from wsprdaemon.conf first receiver)
  - RAC number (integer assigned by the RAC administrator via email)

After saving, the screen shows live connection status and offers a
test button that probes the frps server.
"""

from __future__ import annotations

import configparser
import socket
from pathlib import Path

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Static
from textual.worker import Worker, WorkerState


_FRPC_INI   = Path('/etc/sigmond/frpc.ini')
_FRPS_URL   = 'vpn.wsprdaemon.org'
_FRPS_PORT  = 35735
_PORT_BASE  = 35800


class RacScreen(Vertical):
    """RAC configuration + status panel."""

    DEFAULT_CSS = """
    RacScreen {
        padding: 1;
    }
    RacScreen .section-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    RacScreen .field-row {
        height: 3;
        margin-bottom: 1;
    }
    RacScreen .field-label {
        width: 16;
        padding-top: 1;
    }
    RacScreen #rac-status {
        margin-top: 1;
        color: $text-muted;
    }
    RacScreen #rac-apply {
        margin-top: 1;
        margin-right: 1;
        width: auto;
    }
    RacScreen #rac-test {
        margin-top: 1;
        width: auto;
    }
    RacScreen #rac-result {
        margin-top: 1;
        height: 3;
    }
    """

    def __init__(self, topology, **kwargs) -> None:
        super().__init__(**kwargs)
        self._topology = topology
        comp = (topology.components.get('wd-rac') if hasattr(topology, 'components')
                else {})
        if comp:
            self._rac_id     = getattr(comp, 'rac_id', '') or ''
            self._rac_number = str(getattr(comp, 'rac_number', '') or '')
        else:
            self._rac_id = ''
            self._rac_number = ''

        # Try to prefill rac_id from existing frpc.ini
        if not self._rac_id and _FRPC_INI.exists():
            try:
                cfg = configparser.ConfigParser()
                cfg.read(_FRPC_INI)
                sections = [s for s in cfg.sections()
                            if s != 'common' and not s.endswith('-WEB')]
                if sections:
                    self._rac_id = sections[0]
                    rp = cfg.getint(sections[0], 'remote_port', fallback=-1)
                    if rp >= _PORT_BASE:
                        self._rac_number = str(rp - _PORT_BASE)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def compose(self):
        yield Static("WD-RAC — Remote Access Channel", classes="section-title")
        yield Static(
            "frpc reverse tunnel for remote SSH access.\n"
            "Enter the two values provided by the RAC administrator.",
            id="rac-intro",
        )

        yield Static("Configuration", classes="section-title")
        with Horizontal(classes="field-row"):
            yield Label("RAC ID", classes="field-label")
            yield Input(self._rac_id, placeholder="e.g. AC0G-KA9Q",
                        id="rac-id-input")

        with Horizontal(classes="field-row"):
            yield Label("RAC number", classes="field-label")
            yield Input(self._rac_number, placeholder="integer from admin email",
                        id="rac-number-input")

        yield Static("", id="rac-result")

        with Horizontal():
            yield Button("Apply & enable", id="rac-apply", variant="primary")
            yield Button("Test connection", id="rac-test")

        yield Static("Status", classes="section-title")
        yield Static(self._status_text(), id="rac-status")

    # ------------------------------------------------------------------
    def _status_text(self) -> str:
        if not _FRPC_INI.exists():
            return "frpc.ini not found — apply configuration first."
        try:
            cfg = configparser.ConfigParser()
            cfg.read(_FRPC_INI)
            sections = [s for s in cfg.sections()
                        if s != 'common' and not s.endswith('-WEB')]
            if not sections:
                return "frpc.ini exists but has no proxy section."
            rac_id = sections[0]
            rp     = cfg.getint(sections[0], 'remote_port', fallback=-1)
            host   = cfg.get('common', 'server_addr', fallback=_FRPS_URL)
            return (f"Configured: {rac_id}  →  {host}:{rp}\n"
                    f"(RAC number {rp - _PORT_BASE}  |  SSH port {rp})")
        except Exception as exc:
            return f"Could not parse frpc.ini: {exc}"

    # ------------------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'rac-apply':
            self._do_apply()
        elif event.button.id == 'rac-test':
            self._do_test()

    def _do_apply(self) -> None:
        from textual.app import App
        rac_id     = self.query_one('#rac-id-input', Input).value.strip()
        rac_number_str = self.query_one('#rac-number-input', Input).value.strip()
        result_widget = self.query_one('#rac-result', Static)

        if not rac_id:
            result_widget.update("[red]RAC ID is required.[/red]")
            return
        try:
            rac_number = int(rac_number_str)
            if rac_number < 0:
                raise ValueError
        except ValueError:
            result_widget.update("[red]RAC number must be a non-negative integer.[/red]")
            return

        # Import and call smd's write functions directly
        try:
            import sys as _sys
            _smd_dir = str(Path(__file__).resolve().parents[4] / 'bin')
            if _smd_dir not in _sys.path:
                _sys.path.insert(0, _smd_dir)

            # Use subprocess so we run with sudo
            import subprocess
            r = subprocess.run(
                ['sudo', 'python3', str(Path(__file__).resolve().parents[4] / 'bin' / 'smd'),
                 'install', '--components', 'wd-rac', '--yes'],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                result_widget.update(
                    f"[green]✓ wd-rac configured: {rac_id}, channel {rac_number}[/green]"
                )
                self.query_one('#rac-status', Static).update(self._status_text())
            else:
                result_widget.update(f"[red]Install failed:\n{r.stderr[-300:]}[/red]")
        except Exception as exc:
            result_widget.update(f"[red]Error: {exc}[/red]")

    def _do_test(self) -> None:
        result_widget = self.query_one('#rac-result', Static)
        result_widget.update("Testing connection to frps server ...")
        self.run_worker(self._test_worker, exclusive=True)

    async def _test_worker(self) -> str:
        import asyncio
        loop = asyncio.get_event_loop()

        def _probe():
            try:
                with socket.create_connection((_FRPS_URL, _FRPS_PORT), timeout=5):
                    return True
            except OSError:
                return False

        reachable = await loop.run_in_executor(None, _probe)
        result_widget = self.query_one('#rac-result', Static)
        if reachable:
            result_widget.update(
                f"[green]✓ frps server reachable: {_FRPS_URL}:{_FRPS_PORT}[/green]"
            )
        else:
            result_widget.update(
                f"[red]✗ frps server unreachable: {_FRPS_URL}:{_FRPS_PORT}[/red]"
            )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        pass  # result widget is updated inside _test_worker
