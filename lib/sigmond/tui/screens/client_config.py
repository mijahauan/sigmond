"""Client config screen — TUI counterpart to `smd config init|edit <client>`.

Lists installed clients/servers and shells out to the CLI's per-client
configuration verbs (CONTRACT-v0.5 §14):

  - Init wizard  → `sudo smd config init <client>`
  - Edit config  → `sudo smd config edit <client>`

The CLI handles dispatch to the client's advertised entry point (or to
the sigmond-owned wizard for radiod), the env-var bag, and the
`$EDITOR` fallback when no entry point is declared.  The TUI just picks
the target and suspends so the wizard / editor owns the terminal.

Library-kind catalog entries (e.g. ka9q-python) are excluded — they
have no operator-facing config.
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
from .textual_wizard import TextualConfigWizardScreen


# Sentinel for the per-instance dropdown "no instance" value.
_NO_INSTANCE = "__none__"

# Templated recorder clients — these have per-instance dimension.
_TEMPLATED_RECORDER_CLIENTS = (
    "psk-recorder",
    "wspr-recorder",
    "hfdl-recorder",
    "codar-sounder",
)


def _instance_options_for_client(client: str) -> list:
    """Build (label, value) list for the per-instance dropdown."""
    options: list = [("(no instance)", _NO_INSTANCE)]
    if client not in _TEMPLATED_RECORDER_CLIENTS:
        return options
    try:
        from ...instance import (
            list_instances, detect_migration_candidates,
        )
    except Exception:
        return options
    for i in list_instances(catalog_clients=[client]):
        options.append((i.reporter_id, i.reporter_id))
    try:
        for c in detect_migration_candidates():
            if c.client == client:
                label = f"{c.old_instance} (legacy)"
                options.append((label, c.old_instance))
    except Exception:
        pass
    return options


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


@dataclass
class _ClientView:
    entries: list = field(default_factory=list)   # list[CatalogEntry], filtered + sorted
    error: Optional[str] = None


def _gather() -> _ClientView:
    """Catalog rows whose first-run wizards or edit hooks are operator-facing.

    Filters:
      - drop kind == 'library' (no config, e.g. ka9q-python)
      - drop entries without a repo (no install workflow at all)
    Sort: kind, then name.  radiod sorts naturally next to other clients.
    """
    view = _ClientView()
    try:
        from ...catalog import load_catalog
        catalog = load_catalog()
        entries = [e for e in catalog.values()
                   if e.kind != 'library' and e.repo]
        view.entries = sorted(entries, key=lambda e: (e.kind, e.name))
    except FileNotFoundError as exc:
        view.error = f"catalog not found: {exc}"
    except Exception as exc:
        view.error = str(exc)
    return view


class ClientConfigScreen(Vertical):
    """Run a client's first-run wizard or edit its config."""

    DEFAULT_CSS = """
    ClientConfigScreen {
        padding: 1;
    }
    ClientConfigScreen .cc-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ClientConfigScreen #cc-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    ClientConfigScreen #cc-table {
        height: 14;
    }
    ClientConfigScreen #cc-actions {
        height: 3;
        margin-top: 1;
    }
    ClientConfigScreen #cc-actions Button {
        margin-right: 1;
    }
    ClientConfigScreen #cc-instance-row {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }
    ClientConfigScreen #cc-instance-row Button {
        margin-right: 1;
    }
    ClientConfigScreen #cc-instance {
        width: 30;
        margin-right: 2;
    }
    ClientConfigScreen #cc-hint {
        color: $text-muted;
        margin-top: 0;
    }
    ClientConfigScreen #cc-last {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def compose(self):
        yield Static("Client config — first-run wizard / edit",
                     classes="cc-title")
        yield Static("[dim]loading…[/]", id="cc-status")

        table = DataTable(id="cc-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Kind", "Name", "Description", "Installed")
        yield table

        with Horizontal(id="cc-actions"):
            yield Button("⚙ Init wizard",  id="cc-init",   variant="primary")
            yield Button("✎ Edit config",  id="cc-edit",   variant="default")
            yield Button("✎ Edit in-TUI",  id="cc-edit-tui", variant="default")
            yield Button("Refresh",        id="cc-refresh", variant="default")

        # Per-instance row (sigmond MULTI-INSTANCE-ARCHITECTURE.md §8).
        # Populated when a templated recorder row is selected; "Edit
        # per-instance" runs `smd instance edit <client> <reporter-id>`
        # (Phase-2 stub today — points at $EDITOR — see spec §6).
        with Horizontal(id="cc-instance-row"):
            yield Select(
                options=[("(none)", _NO_INSTANCE)], value=_NO_INSTANCE,
                id="cc-instance", allow_blank=False,
            )
            yield Button("✎ Edit per-instance",
                         id="cc-edit-instance", variant="default")

        yield Static(
            "[dim]Init / Edit run the whiptail wizard (suspends the TUI). "
            "Edit in-TUI opens an in-process Textual form for scalar keys "
            "(no suspend); arrays-of-tables still need the whiptail "
            "wizard's $EDITOR option.  radiod uses the sigmond-owned "
            "wizard.  Edit per-instance is the new per-reporter path: "
            "pick a row, then pick the instance from the dropdown.[/]",
            id="cc-hint")

        yield Static("", id="cc-last")

    def on_mount(self) -> None:
        self._refresh()

    # ------------------------------------------------------------------
    # buttons
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cc-refresh":
            self._refresh()
        elif bid == "cc-init":
            self._run_verb("init")
        elif bid == "cc-edit":
            self._run_verb("edit")
        elif bid == "cc-edit-tui":
            self._open_textual_wizard()
        elif bid == "cc-edit-instance":
            self._edit_per_instance()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Repopulate the per-instance dropdown when the operator
        picks a different client row."""
        entry = self._selected_entry()
        if entry is None:
            return
        instance_sel = self.query_one("#cc-instance", Select)
        instance_sel.set_options(_instance_options_for_client(entry.name))
        instance_sel.value = _NO_INSTANCE

    def _edit_per_instance(self) -> None:
        entry = self._selected_entry()
        last = self.query_one("#cc-last", Static)
        if entry is None:
            last.update("[yellow]pick a row first[/]")
            return
        instance_val = self.query_one("#cc-instance", Select).value
        if instance_val in (None, Select.BLANK, _NO_INSTANCE):
            last.update(
                "[yellow]Pick an instance from the dropdown — "
                "use the regular Edit buttons above for client-wide "
                "config[/]")
            return
        cmd = [_smd_binary(), 'instance', 'edit', entry.name,
               str(instance_val)]
        # Phase-2 stub today — points at $EDITOR — but the wiring is
        # ready for when the per-client refactor (post-Phase 5) lets
        # this drive the client's config flow with the per-instance
        # path injected.  Suspends the TUI exactly like the other
        # Edit buttons.
        self.app.suspend()
        try:
            subprocess.run(cmd, check=False)
        finally:
            self.app.resume()
        last.update(f"[green]✓[/] ran: {' '.join(cmd)}")

    # ------------------------------------------------------------------
    # data loading
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self.query_one("#cc-status", Static).update("[dim]loading…[/]")
        self.run_worker(_gather, thread=True, name="cc-gather")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        data = event.worker.result
        if not isinstance(data, _ClientView):
            return
        self._render_view(data)

    def _render_view(self, view: _ClientView) -> None:
        status = self.query_one("#cc-status", Static)
        if view.error:
            status.update(f"[red]{view.error}[/]")
            return

        installed = sum(1 for e in view.entries if e.is_installed())
        status.update(
            f"{len(view.entries)} configurable entries  "
            f"•  [green]{installed} installed[/]  "
            f"•  {len(view.entries) - installed} not installed")

        table = self.query_one("#cc-table", DataTable)
        table.clear()
        self._entries = list(view.entries)
        for entry in view.entries:
            mark = ("[green]✔[/]" if entry.is_installed()
                    else "[dim]✘[/]")
            table.add_row(entry.kind, entry.name,
                          entry.description[:60], mark)

    # ------------------------------------------------------------------
    # verb dispatch
    # ------------------------------------------------------------------

    def _selected_entry(self):
        table = self.query_one("#cc-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        idx = table.cursor_row
        if not 0 <= idx < len(getattr(self, '_entries', [])):
            return None
        return self._entries[idx]

    def _run_verb(self, verb: str) -> None:
        entry = self._selected_entry()
        last = self.query_one("#cc-last", Static)
        if entry is None:
            last.update("[yellow]pick a row first[/]")
            return
        if not entry.is_installed():
            last.update(
                f"[yellow]{entry.name} is not installed — "
                f"run Install first[/]")
            return

        cmd = [_smd_binary(), 'config', verb, entry.name]
        title = "Run first-run wizard?" if verb == "init" else "Edit config?"
        body = (
            f"Run [bold]sudo smd config {verb} {entry.name}[/]\n\n"
            "The TUI will suspend so the wizard / editor owns the "
            "terminal.  When it exits you'll return here with the exit "
            "code shown below."
        )
        confirm_and_run(
            self.app,
            title=title,
            body=body,
            cmd=cmd, sudo=True,
            on_complete=self._after_run,
        )

    def _open_textual_wizard(self) -> None:
        entry = self._selected_entry()
        last = self.query_one("#cc-last", Static)
        if entry is None:
            last.update("[yellow]pick a row first[/]")
            return
        if not entry.is_installed():
            last.update(
                f"[yellow]{entry.name} is not installed — "
                f"run Install first[/]")
            return

        try:
            from ...catalog import find_client_binary
        except ImportError:
            last.update("[red]catalog module missing find_client_binary[/]")
            return
        client_bin = find_client_binary(entry.name)
        if not client_bin:
            last.update(
                f"[yellow]{entry.name}: no CLI binary found "
                f"(not in PATH and no /opt/{entry.name}/venv/bin/{entry.name})[/]"
            )
            return

        def _after_wizard(saved: Optional[bool]) -> None:
            if saved:
                last.update(
                    f"[green]✔ saved via in-TUI wizard[/]  {entry.name}"
                )
                self._refresh()
            else:
                last.update(f"[dim]cancelled in-TUI wizard[/]  {entry.name}")

        self.app.push_screen(
            TextualConfigWizardScreen(
                client_name=entry.name,
                client_bin=client_bin,
            ),
            _after_wizard,
        )

    def _after_run(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#cc-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ exit 0[/]  {argv}")
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]  {argv}")
        self._refresh()
