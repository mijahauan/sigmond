"""Install screen — TUI counterpart to `smd install`.

Catalog browser with install status per entry.  Row cursor picks a
target; 'Install selected' or 'Install all missing' shells out to the
CLI with confirmation + suspend/sudo (same pattern as Lifecycle).

The CLI does the real work — clone to /opt/git/<name>, run the
client's canonical install.sh — so the TUI stays thin.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState

from ..mutation import confirm_and_run


def _smd_binary() -> str:
    """See lifecycle._smd_binary — same logic, duplicated short helper."""
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


@dataclass
class _CatalogView:
    entries: list = field(default_factory=list)   # list[CatalogEntry]
    error: Optional[str] = None


def _gather_catalog() -> _CatalogView:
    view = _CatalogView()
    try:
        from ...catalog import load_catalog
        catalog = load_catalog()
        # Exclude entries with no repo URL — those have no git workflow.
        view.entries = sorted(
            (e for e in catalog.values() if e.repo),
            key=lambda e: (e.kind, e.name),
        )
    except FileNotFoundError as exc:
        view.error = f"catalog not found: {exc}"
    except Exception as exc:
        view.error = str(exc)
    return view


class InstallScreen(Vertical):
    """Browse the catalog and install clients."""

    DEFAULT_CSS = """
    InstallScreen {
        padding: 1;
    }
    InstallScreen .is-title {
        text-style: bold;
        margin-bottom: 1;
    }
    InstallScreen #is-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    InstallScreen #is-table {
        height: 14;
    }
    InstallScreen #is-actions {
        height: 3;
        margin-top: 1;
    }
    InstallScreen #is-actions Button {
        margin-right: 1;
    }
    InstallScreen #is-last {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def compose(self):
        yield Static("Software Install — catalog of known clients",
                     classes="is-title")
        yield Static("[dim]loading\u2026[/]", id="is-status")
        table = DataTable(id="is-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Kind", "Name", "Description", "Status")
        yield table
        with Horizontal(id="is-actions"):
            yield Button("Install selected", id="is-one", variant="primary")
            yield Button("Install all missing", id="is-all", variant="warning")
            yield Button("Refresh", id="is-refresh", variant="default")
        yield Static("", id="is-last")

    def on_mount(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "is-refresh":
            self._refresh()
        elif bid == "is-one":
            self._install_selected()
        elif bid == "is-all":
            self._install_all_missing()

    def _refresh(self) -> None:
        self.query_one("#is-status", Static).update("[dim]loading\u2026[/]")
        self.run_worker(_gather_catalog, thread=True, name="is-gather")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        data = event.worker.result
        if not isinstance(data, _CatalogView):
            return
        self._render_data(data)

    def _render_data(self, view: _CatalogView) -> None:
        status = self.query_one("#is-status", Static)
        if view.error:
            status.update(f"[red]{view.error}[/]")
            return

        installed = sum(1 for e in view.entries if e.is_installed())
        status.update(
            f"{len(view.entries)} entries  "
            f"\u2022  [green]{installed} installed[/]  "
            f"\u2022  {len(view.entries) - installed} not installed")

        table = self.query_one("#is-table", DataTable)
        table.clear()
        self._entries = list(view.entries)
        for entry in view.entries:
            state = ("[green]\u2714 installed[/]"
                     if entry.is_installed()
                     else "[dim]\u2718 not installed[/]")
            table.add_row(entry.kind, entry.name,
                          entry.description[:60], state)

    def _selected_entry(self):
        table = self.query_one("#is-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        idx = table.cursor_row
        if not 0 <= idx < len(getattr(self, '_entries', [])):
            return None
        return self._entries[idx]

    def _install_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.query_one("#is-last", Static).update(
                "[yellow]pick a row first[/]")
            return
        if entry.is_installed():
            self.query_one("#is-last", Static).update(
                f"[dim]{entry.name} already installed[/]")
            return

        cmd = [_smd_binary(), 'install', entry.name]
        confirm_and_run(
            self.app,
            title=f"Install {entry.name}?",
            body=(f"Clone and install [bold]{entry.name}[/] "
                  f"({entry.kind}).\n\n{entry.description}"),
            cmd=cmd, sudo=True,
            on_complete=self._after_install,
        )

    def _install_all_missing(self) -> None:
        entries = getattr(self, '_entries', [])
        missing = [e for e in entries if not e.is_installed()]
        if not missing:
            self.query_one("#is-last", Static).update(
                "[green]nothing to install[/]")
            return

        names = ', '.join(e.name for e in missing)
        components_arg = ','.join(e.name for e in missing)
        cmd = [_smd_binary(), 'install', '--components', components_arg, '--yes']
        confirm_and_run(
            self.app,
            title=f"Install {len(missing)} missing entries?",
            body=(f"Installing: {names}\n\n"
                  f"Each client's own install.sh handles the details. "
                  f"Existing installs are left alone."),
            cmd=cmd, sudo=True,
            on_complete=self._after_install,
        )

    def _after_install(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#is-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]\u2714 exit 0[/]  {argv}")
        else:
            last.update(f"[red]\u2718 exit {result.returncode}[/]  {argv}")
        self._refresh()
