"""Instance screen — TUI counterpart to `smd admin instance` (MULTI-INSTANCE-ARCHITECTURE.md §6).

Lists all per-reporter instances on this host, lets the operator add
a new instance, remove one, or invoke the migration tool.  All
destructive actions shell out to `smd admin instance ...` (root via the
shared confirm_and_run helper); read-only listing reads
`sigmond.instance.list_instances()` directly.

Per-instance config editing isn't surfaced here — that's the
client-config flow (separate screen).  This screen owns instance
lifecycle: create, remove, kick off migration.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Label, RichLog, Static

from ..mutation import confirm_and_run

try:
    from ...instance import display_reporter_id as _display
except ImportError:
    def _display(rid: str) -> str:
        return rid.replace("=", "/")


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


# Catalog clients the operator might create instances for.  Subset
# matches `_TEMPLATED_RECORDER_CLIENTS` in lib/sigmond/instance.py
# plus mag-recorder (singleton today, listed for future migration).
_INSTANCE_CAPABLE_CLIENTS = (
    "psk-recorder",
    "wspr-recorder",
    "hfdl-recorder",
    "codar-sounder",
    "mag-recorder",
)


class InstanceScreen(Vertical):
    """Per-reporter instance lifecycle (list / add / remove / migrate)."""

    DEFAULT_CSS = """
    InstanceScreen {
        padding: 1;
    }
    InstanceScreen .in-title {
        text-style: bold;
        margin-bottom: 1;
    }
    InstanceScreen .in-section {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    InstanceScreen .in-body {
        margin-bottom: 1;
        color: $text-muted;
    }
    InstanceScreen #in-table {
        height: 12;
        margin-bottom: 1;
    }
    InstanceScreen .in-field-row {
        height: 3;
        margin-bottom: 1;
    }
    InstanceScreen .in-field-row Label {
        width: 12;
        content-align: left middle;
    }
    InstanceScreen .in-field-row Input {
        width: 22;
        margin-right: 2;
    }
    InstanceScreen #in-controls Button {
        margin-right: 1;
    }
    InstanceScreen #in-output {
        height: 14;
        border: solid $primary-background;
        margin-top: 1;
    }
    InstanceScreen #in-last {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def compose(self):
        yield Static("Instance — per-reporter client lifecycle",
                     classes="in-title")
        yield Static(
            "Each instance is one deployment context of a recorder client, "
            "keyed by reporter ID (e.g. AC0G-B1).  Reporter IDs are "
            "path-safe by construction: uppercase alphanumerics + hyphens, "
            "no leading/trailing hyphen, min length 2.",
            classes="in-body")

        # ---- Listing ----------------------------------------------------
        yield Static("Existing instances", classes="in-section")
        table = DataTable(id="in-table", cursor_type="row")
        table.add_columns("Client", "Reporter ID", "Config", "Env", "Sources")
        yield table
        with Horizontal(id="in-controls"):
            yield Button("Refresh", id="in-refresh", variant="primary")
            yield Button("Remove selected", id="in-remove", variant="warning")

        # ---- Add new ----------------------------------------------------
        yield Static("Add a new instance", classes="in-section")
        with Horizontal(classes="in-field-row"):
            yield Label("Client")
            yield Input("", id="in-add-client",
                        placeholder=" / ".join(_INSTANCE_CAPABLE_CLIENTS[:2]) + " / …")
            yield Label("Reporter")
            yield Input("", id="in-add-reporter",
                        placeholder="e.g. AC0G/B1 or W1ABC-5")
            yield Button("Add (dry-run)", id="in-add-dry",
                         variant="default")
            yield Button("Add", id="in-add-run", variant="success")

        # ---- Migration --------------------------------------------------
        yield Static("Migrate legacy radiod-keyed deployments",
                     classes="in-section")
        yield Static(
            "Scans `<client>@<radiod-id>.service` units that haven't been "
            "renamed yet and walks the operator through one-shot "
            "migration to `<client>@<reporter-id>.service`.  Dry-run "
            "lists candidates without changing anything; running the "
            "migration is currently CLI-only (interactive prompts per "
            "candidate) — invoke `smd admin instance migrate --yes` in "
            "a terminal.",
            classes="in-body")
        with Horizontal():
            yield Button("Scan (dry-run)", id="in-migrate-dry",
                         variant="default")

        # ---- Output ----------------------------------------------------
        yield RichLog(id="in-output", highlight=False, markup=False,
                      max_lines=2000, wrap=False)
        yield Static("[dim]idle[/]", id="in-last", markup=True)

    def on_mount(self) -> None:
        self._refresh_table()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "in-refresh":
            self._refresh_table()
        elif bid == "in-remove":
            self._do_remove_selected()
        elif bid == "in-add-dry":
            self._do_add(dry_run=True)
        elif bid == "in-add-run":
            self._do_add(dry_run=False)
        elif bid == "in-migrate-dry":
            self._do_migrate_dry()

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    def _refresh_table(self) -> None:
        try:
            from ...instance import list_instances
        except Exception as exc:
            self._log(f"[error: cannot import sigmond.instance: {exc}]")
            return
        table = self.query_one("#in-table", DataTable)
        table.clear()
        instances = list_instances()
        if not instances:
            self.query_one("#in-last", Static).update(
                "[dim]no per-reporter instances yet — add one below, or "
                "run `smd admin instance migrate` to convert legacy "
                "radiod-keyed deployments[/]")
            return
        for i in instances:
            c = "✓" if i.has_config else "-"
            e = "✓" if i.has_env else "-"
            s = "✓" if i.has_sources else "-"
            # Display the slash form (user-facing); keep the row key
            # in storage form (`=`-encoded) so downstream `smd admin instance
            # remove` argv is path-safe.
            table.add_row(i.client, _display(i.reporter_id), c, e, s,
                          key=f"{i.client}|{i.reporter_id}")
        self.query_one("#in-last", Static).update(
            f"[dim]{len(instances)} instance(s)[/]")

    # ------------------------------------------------------------------
    # Add
    # ------------------------------------------------------------------
    def _do_add(self, *, dry_run: bool) -> None:
        client = self.query_one("#in-add-client", Input).value.strip()
        reporter = self.query_one("#in-add-reporter", Input).value.strip()
        if not client or not reporter:
            self.query_one("#in-last", Static).update(
                "[red]Add: client and reporter both required[/]")
            return
        cmd = [_smd_binary(), 'admin', 'instance', 'add', client, reporter]
        if dry_run:
            cmd.append('--dry-run')
        if dry_run:
            self._exec_async(cmd)
        else:
            confirm_and_run(
                self.app,
                title=f"Add instance {client}@{reporter}?",
                body=("Creates per-instance config / env / sources "
                      "files.  Does NOT enable or start the systemd "
                      "unit — that's a separate `smd admin instance enable` "
                      "step after editing the per-instance config."),
                cmd=cmd, sudo=True,
                on_complete=self._after_mutation,
            )

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------
    def _do_remove_selected(self) -> None:
        table = self.query_one("#in-table", DataTable)
        if table.row_count == 0:
            self.query_one("#in-last", Static).update(
                "[red]Remove: no instances to remove[/]")
            return
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            self.query_one("#in-last", Static).update(
                "[red]Remove: select a row first (arrow keys + Enter)[/]")
            return
        key_value = row_key.value if row_key else ""
        if not key_value or "|" not in key_value:
            self.query_one("#in-last", Static).update(
                "[red]Remove: couldn't resolve selected row[/]")
            return
        client, reporter = key_value.split("|", 1)
        cmd = [_smd_binary(), 'admin', 'instance', 'remove', client, reporter,
               '--yes']
        confirm_and_run(
            self.app,
            title=f"Remove instance {client}@{_display(reporter)}?",
            body=("Removes per-instance config / env / sources files. "
                  "Does NOT stop or disable the systemd unit — run "
                  "`smd admin instance disable` first if the unit is "
                  "running.  Use `--purge` from the CLI for state/log "
                  "dirs."),
            cmd=cmd, sudo=True,
            on_complete=self._after_mutation,
        )

    # ------------------------------------------------------------------
    # Migrate (dry-run only from the TUI; live needs CLI)
    # ------------------------------------------------------------------
    def _do_migrate_dry(self) -> None:
        cmd = [_smd_binary(), 'admin', 'instance', 'migrate']
        self._exec_async(cmd)

    # ------------------------------------------------------------------
    # Subprocess plumbing
    # ------------------------------------------------------------------
    def _exec_async(self, cmd: list) -> None:
        log = self.query_one("#in-output", RichLog)
        log.clear()
        log.write(f"$ {' '.join(cmd)}")
        self.query_one("#in-last", Static).update("[dim]running…[/]")
        self.run_worker(lambda: self._exec(cmd), thread=True,
                        name="in-exec")

    def _exec(self, cmd: list) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.app.call_from_thread(self._log, f"[error: {exc}]")
            self.app.call_from_thread(
                self.query_one("#in-last", Static).update,
                f"[red]failed: {exc}[/]")
            return subprocess.CompletedProcess(cmd, 127, "", str(exc))
        for line in (result.stdout or "").splitlines():
            self.app.call_from_thread(self._log, line)
        for line in (result.stderr or "").splitlines():
            self.app.call_from_thread(self._log, line)
        badge = ("[green]✔ exit 0[/]" if result.returncode == 0
                 else f"[red]✘ exit {result.returncode}[/]")
        self.app.call_from_thread(
            self.query_one("#in-last", Static).update,
            f"{badge}  {' '.join(cmd)}")
        return result

    def _log(self, msg: str) -> None:
        self.query_one("#in-output", RichLog).write(msg)

    def _after_mutation(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#in-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ exit 0[/]  {argv}")
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]  {argv}")
        self._refresh_table()
