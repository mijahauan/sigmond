"""Config screen — TUI counterpart to `smd config show` and `smd config migrate`.

Pulls JSON from `smd config show --json`, renders coordination + client
summaries, and offers a Migrate button for `sudo smd config migrate`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState

from ..mutation import confirm_and_run


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


def _fetch_config() -> dict:
    """Run `smd config show --json` and parse.  Returns a dict with
    either ``{"payload": {...}}`` on success or ``{"error": "..."}``."""
    cmd = [_smd_binary(), 'config', 'show', '--json']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           check=False, stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"launch failed: {exc}"}
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout or "").strip()[:400]
                or f"exit {r.returncode}"}
    try:
        return {"payload": json.loads(r.stdout)}
    except json.JSONDecodeError as exc:
        return {"error": f"invalid JSON from smd config show: {exc}"}


class ConfigShowScreen(Vertical):
    """Read-only view of coordination + client config, with Migrate button."""

    DEFAULT_CSS = """
    ConfigShowScreen {
        padding: 1;
    }
    ConfigShowScreen .cs-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ConfigShowScreen .cs-section {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    ConfigShowScreen #cs-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    ConfigShowScreen #cs-controls {
        height: 3;
        margin-top: 1;
    }
    ConfigShowScreen #cs-controls Button {
        margin-right: 1;
    }
    """

    def compose(self):
        yield Static("Config — coordination + clients", classes="cs-title")
        yield Static("[dim]loading…[/]", id="cs-status")

        yield Static("Coordination", classes="cs-section")
        yield Static("", id="cs-coord")

        yield Static("Radiod instances", classes="cs-section")
        rtab = DataTable(id="cs-radiods")
        rtab.add_columns("ID", "Scope", "Samprate (Hz)", "Status DNS")
        yield rtab

        yield Static("Clients", classes="cs-section")
        ctab = DataTable(id="cs-clients")
        ctab.add_columns("Name", "Installed", "Contract", "Instances")
        yield ctab

        with Horizontal(id="cs-controls"):
            yield Button("Refresh", id="cs-refresh", variant="default")
            yield Button("Migrate config", id="cs-migrate", variant="warning")

    def on_mount(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cs-refresh":
            self._refresh()
        elif event.button.id == "cs-migrate":
            self._migrate()

    def _refresh(self) -> None:
        self.query_one("#cs-status", Static).update("[dim]loading…[/]")
        self.run_worker(_fetch_config, thread=True, name="cs-fetch")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "cs-fetch":
            return
        if event.state != WorkerState.SUCCESS:
            return
        result = event.worker.result
        if not isinstance(result, dict):
            return
        if "error" in result:
            self.query_one("#cs-status", Static).update(
                f"[red]smd config show failed: {result['error']}[/]")
            return
        self._render_payload(result["payload"])

    def _render_payload(self, payload: dict) -> None:
        coord = payload.get("coordination") or {}
        clients = payload.get("clients") or {}

        self.query_one("#cs-status", Static).update(
            f"[green]loaded[/]  coordination + "
            f"{len(clients)} client(s)")

        lines: list = []
        src = coord.get("source_path") or "(defaults)"
        lines.append(f"source: {src}")
        host = coord.get("host") or {}
        if host.get("call") or host.get("grid"):
            lines.append(f"host: {host.get('call', '?')} / "
                         f"{host.get('grid', '?')}")
        self.query_one("#cs-coord", Static).update("\n".join(lines) or "(none)")

        rtab = self.query_one("#cs-radiods", DataTable)
        rtab.clear()
        for rid, r in sorted((coord.get("radiods") or {}).items()):
            scope = "local" if r.get("is_local") else f"remote ({r.get('host', '?')})"
            rtab.add_row(rid, scope,
                         str(r.get("samprate_hz") or "?"),
                         str(r.get("status_dns") or "?"))

        ctab = self.query_one("#cs-clients", DataTable)
        ctab.clear()
        for name, cv in sorted(clients.items()):
            installed = "[green]yes[/]" if cv.get("installed") else "[dim]no[/]"
            contract = cv.get("contract_version") or "—"
            instances = cv.get("instances") or []
            ctab.add_row(name, installed, str(contract), str(len(instances)))

    def _migrate(self) -> None:
        cmd = [_smd_binary(), 'config', 'migrate']
        confirm_and_run(
            self.app,
            title="Run config migrate?",
            body=("Upgrade coordination config to the latest schema.  "
                  "Re-reads topology + coordination and writes the "
                  "migrated form back.  A backup is made first."),
            cmd=cmd, sudo=True,
            on_complete=self._after_migrate,
        )

    def _after_migrate(self, result: subprocess.CompletedProcess) -> None:
        status = self.query_one("#cs-status", Static)
        if result.returncode == 0:
            status.update("[green]✔ migrate exit 0[/] — refreshing…")
            self._refresh()
        else:
            status.update(
                f"[red]✘ migrate exit {result.returncode}[/] — "
                "see terminal for details")
