"""Configuration screen — consolidates the three legacy screens
(Instance, Client config, Config view) into one instance-centric
surface that matches the operator's mental model:

    "set up wspr-recorder@AC0G-B1 from scratch"

is one atomic workflow — pick a client, pick a reporter ID, fill in
the per-instance config, enable the unit.  The three legacy screens
each owned one verb of that workflow and forced the operator to
bounce between them.  The CLI side already has the right
abstraction (`smd admin instance {list, add, edit, remove, enable, disable}`
is one namespace); this screen mirrors it.

Layout:

  ┌───────────────────────────────────────────────────────────────┐
  │ Configuration                                                 │
  │ [Coordination] Station: … · Timing: … · Radiods: …  [Migrate] │
  │ N instances configured · M active                             │
  │ [Refresh] [+ Add instance…] [Edit…] [Remove…]                 │
  │ ┌─────────────────────────────────────────────────────────┐   │
  │ │ Client │ Reporter │ Sources │ State │ has_config? …     │   │
  │ ├─────────────────────────────────────────────────────────┤   │
  │ │ …                                                       │   │
  │ └─────────────────────────────────────────────────────────┘   │
  │ status / output                                               │
  └───────────────────────────────────────────────────────────────┘

Single-select row cursor (highlight) — actions target the cursored
row.  This is a drill-down screen (each row → one action on one
instance), so highlight-row is the right gesture; the checkbox/
multi-select pattern from Lifecycle is reserved for verb screens
where N targets get one verb.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Label, RichLog, Static


try:
    from ...instance import (
        display_reporter_id as _display,
        parse_user_reporter_id as _parse_rid,
    )
except ImportError:
    def _display(rid: str) -> str:
        return rid.replace("=", "/")

    def _parse_rid(s: str) -> str:
        return s.strip().replace("/", "-").upper()


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


# Clients that accept per-instance config files (matches
# sigmond.instance._TEMPLATED_RECORDER_CLIENTS plus hf-gps-tec and
# mag-recorder, which are singletons today but plumb reporter_id
# through their per-instance config schema).
_INSTANCE_CAPABLE_CLIENTS = (
    "codar-sounder",
    "hf-gps-tec",
    "hfdl-recorder",
    "mag-recorder",
    "psk-recorder",
    "wspr-recorder",
)


def _coordination_summary() -> str:
    """One-line summary of the host's coordination state — station
    callsign/grid, timing authority, declared radiod set."""
    try:
        from ...coordination import load_coordination
        coord = load_coordination()
    except Exception as exc:
        return f"[dim]coordination: unavailable ({exc})[/]"
    bits: list[str] = []
    station = getattr(coord, "station", None) or {}
    call = station.get("callsign") or station.get("call") or ""
    grid = station.get("grid_square") or station.get("grid") or ""
    if call:
        bits.append(f"Station: [bold]{call}[/]" + (f"/{grid}" if grid else ""))
    timing = getattr(coord, "timing", None) or {}
    authority = timing.get("authority", "rtp")
    bits.append(f"Timing: [bold]{authority}[/]")
    radiods = getattr(coord, "radiods", None) or {}
    if radiods:
        names = sorted(radiods.keys())
        if len(names) <= 3:
            bits.append(f"Radiods: [bold]{', '.join(names)}[/]")
        else:
            bits.append(f"Radiods: [bold]{', '.join(names[:3])}[/] +"
                        f"{len(names) - 3}")
    return " · ".join(bits) if bits else "[dim]no coordination declared[/]"


def _sources_for(client: str, reporter_id: str) -> str:
    """Read the per-instance sources file and render a one-line summary.

    Returns "[dim]—[/]" when the file is missing or unreadable (operator
    hasn't picked sources yet — common right after `smd admin instance add`).
    """
    try:
        from ...instance import instance_paths
        paths = instance_paths(client, reporter_id)
    except Exception:
        return "[dim]—[/]"
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        with open(paths.sources, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, Exception):
        return "[dim]—[/]"
    src_list = doc.get("source") or doc.get("sources") or []
    if not isinstance(src_list, list) or not src_list:
        return "[dim](none)[/]"
    rendered = []
    for s in src_list:
        if isinstance(s, dict):
            rendered.append(s.get("name") or s.get("ref") or "?")
        elif isinstance(s, str):
            rendered.append(s)
    return ", ".join(rendered) if rendered else "[dim]—[/]"


class ConfigurationScreen(Vertical):
    """One screen for the (instance × config) workflow."""

    DEFAULT_CSS = """
    ConfigurationScreen {
        padding: 1;
    }
    ConfigurationScreen .cf-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ConfigurationScreen #cf-coord-row {
        height: 1;
        margin-bottom: 1;
    }
    ConfigurationScreen #cf-coord-summary {
        width: 1fr;
    }
    ConfigurationScreen #cf-migrate {
        min-width: 12;
    }
    ConfigurationScreen #cf-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    ConfigurationScreen #cf-actions-row {
        height: 3;
        margin-bottom: 1;
    }
    ConfigurationScreen #cf-actions-row Button {
        margin-right: 1;
        min-width: 16;
    }
    ConfigurationScreen #cf-table {
        margin-bottom: 1;
        height: 12;
    }
    ConfigurationScreen #cf-add-row {
        height: 3;
        margin-bottom: 1;
    }
    ConfigurationScreen #cf-add-row Label {
        width: 10;
        content-align: left middle;
    }
    ConfigurationScreen #cf-add-client {
        width: 22;
        margin-right: 2;
    }
    ConfigurationScreen #cf-add-reporter {
        width: 20;
        margin-right: 2;
    }
    ConfigurationScreen #cf-add-row Button {
        margin-right: 1;
    }
    ConfigurationScreen #cf-output {
        height: 12;
        border: solid $primary-background;
    }
    ConfigurationScreen #cf-last {
        margin-top: 1;
        color: $text-muted;
    }
    ConfigurationScreen .cf-section {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Cache of last-rendered instance rows so action handlers can
        # resolve the cursored row back to (client, reporter_id).
        self._instances: list = []

    def compose(self):
        yield Static("Configuration — per-instance setup",
                     classes="cf-title")

        # Coordination summary + Migrate button on one line.
        with Horizontal(id="cf-coord-row"):
            yield Static(_coordination_summary(),
                         id="cf-coord-summary", markup=True)
            yield Button("Migrate", id="cf-migrate", variant="default")

        yield Static("[dim]loading…[/]", id="cf-status")

        # Actions row.  Refresh first (read-only), then per-row verbs.
        with Horizontal(id="cf-actions-row"):
            yield Button("↻ Refresh", id="cf-refresh", variant="default")
            yield Button("Edit selected…", id="cf-edit", variant="primary")
            yield Button("Remove selected…", id="cf-remove",
                         variant="warning")

        # Instance table.  Single-select cursor.
        table = DataTable(id="cf-table", cursor_type="row",
                          zebra_stripes=True)
        table.add_columns("Client", "Reporter ID", "Sources",
                          "Cfg/Env/Srcs", "Unit")
        yield table

        # Add-instance inline form (sits below the table because the
        # operator does this less often than browse + edit).
        yield Static("Add instance", classes="cf-section")
        with Horizontal(id="cf-add-row"):
            yield Label("Client")
            yield Input("",
                        id="cf-add-client",
                        placeholder=" / ".join(_INSTANCE_CAPABLE_CLIENTS[:3])
                                    + " / …")
            yield Label("Reporter")
            yield Input("",
                        id="cf-add-reporter",
                        placeholder="e.g. AC0G/B1 or AC0G-B2")
            yield Button("Add (dry-run)", id="cf-add-dry",
                         variant="default")
            yield Button("Add", id="cf-add-run", variant="success")

        # Subprocess output + last-status footer.
        yield RichLog(id="cf-output", highlight=False, markup=False,
                      max_lines=2000, wrap=False)
        yield Static("[dim]idle[/]", id="cf-last", markup=True)

    def on_mount(self) -> None:
        self._refresh()

    # ------------------------------------------------------------------
    # listing
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        try:
            from ...instance import list_instances
        except Exception as exc:
            self._set_last(f"[red]error: cannot import sigmond.instance: "
                           f"{exc}[/]")
            return
        instances = list_instances()
        self._instances = instances
        # Refresh coordination line too — Migrate may have just landed.
        self.query_one("#cf-coord-summary", Static).update(
            _coordination_summary())

        table = self.query_one("#cf-table", DataTable)
        table.clear()

        active_states = _batch_unit_active(
            [f"{i.client}@{i.reporter_id}.service" for i in instances])

        for i in instances:
            chk = ("✓" if i.has_config else "-") + "/" \
                + ("✓" if i.has_env else "-") + "/" \
                + ("✓" if i.has_sources else "-")
            sources = _sources_for(i.client, i.reporter_id)
            unit = f"{i.client}@{i.reporter_id}.service"
            state = active_states.get(unit, "unknown")
            unit_cell = _unit_markup(state)
            table.add_row(i.client, _display(i.reporter_id),
                          sources, chk, unit_cell,
                          key=f"{i.client}|{i.reporter_id}")

        total = len(instances)
        active = sum(1 for s in active_states.values() if s == "active")
        self.query_one("#cf-status", Static).update(
            f"{total} instance(s) configured · "
            f"[green]{active} active[/], {total - active} not active")
        if not instances:
            self._set_last(
                "[dim]no per-reporter instances yet — fill the form below "
                "to add one, or click [bold]Migrate[/] to convert legacy "
                "radiod-keyed deployments[/]")
        else:
            self._set_last(
                "[dim]select a row + click Edit or Remove to act on one "
                "instance[/]")

    # ------------------------------------------------------------------
    # button dispatch
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cf-refresh":
            self._refresh()
        elif bid == "cf-edit":
            self._do_edit()
        elif bid == "cf-remove":
            self._do_remove()
        elif bid == "cf-add-dry":
            self._do_add(dry_run=True)
        elif bid == "cf-add-run":
            self._do_add(dry_run=False)
        elif bid == "cf-migrate":
            self._do_migrate_dry()

    def _selected(self) -> Optional[tuple[str, str]]:
        """Resolve the cursored row to (client, reporter_id); None if
        no row is cursored."""
        table = self.query_one("#cf-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(
                table.cursor_coordinate).row_key
        except Exception:
            return None
        key_value = getattr(row_key, "value", "")
        if not isinstance(key_value, str) or "|" not in key_value:
            return None
        client, reporter = key_value.split("|", 1)
        return client, reporter

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _do_edit(self) -> None:
        """Route the operator into the right edit surface.

        Preference order:
          1. In-TUI Textual wizard — when the client implements the JSON
             config-roundtrip contract (CLIENT-CONTRACT §14:
             ``config show --json --defaults`` + ``config apply --json -``).
             Polished, consistent look-and-feel under sigmond; doesn't
             suspend the TUI.  Probed via a short ``config show --json``
             call with the per-instance config path.
          2. Whiptail / $EDITOR via ``smd config edit`` — fall back
             when the client doesn't expose the contract yet (most
             pre-existing clients today).  TUI suspends, the client's
             own wizard owns the terminal.

        The probe runs synchronously against the selected client with a
        5s timeout — cheap enough to do inline with the click.
        """
        sel = self._selected()
        if sel is None:
            self._set_last("[yellow]select a row first[/]")
            return
        client, reporter = sel

        # Per-instance config-file path the wizard / fallback edits.
        try:
            from ...instance import instance_paths
            paths = instance_paths(client, reporter)
            config_path = str(paths.config)
        except Exception:
            config_path = None

        # Probe for the JSON contract.  Routing to the wizard wins when
        # both the binary is on PATH and `config show --json` returns
        # exit 0 + JSON-shaped stdout.
        try:
            from ...catalog import find_client_binary
            client_bin = find_client_binary(client)
        except Exception:
            client_bin = None

        if (client_bin and config_path
                and self._client_supports_json_contract(client_bin, config_path)):
            self._open_textual_wizard(client, client_bin, config_path)
            return

        # Fall back to whiptail.
        from ..mutation import confirm_and_run
        cmd = [_smd_binary(), 'config', 'edit', client, reporter]
        confirm_and_run(
            self.app,
            title=f"Edit {client}@{_display(reporter)}?",
            body=(
                f"{client} doesn't expose the JSON config-roundtrip "
                f"contract yet, so we fall back to its native wizard.\n\n"
                f"Run [bold]smd config edit {client} {reporter}[/]\n\n"
                "The TUI suspends so the client's whiptail wizard (or its "
                "$EDITOR fallback) owns the terminal.  Returns here with "
                "the exit code when the editor exits."
            ),
            cmd=cmd, sudo=True,
            on_complete=self._after_mutation,
        )

    @staticmethod
    def _client_supports_json_contract(client_bin: str,
                                       config_path: str) -> bool:
        """Quick probe: does `<binary> config show --json --config <path>`
        succeed?  Lightweight — we only need to know whether the contract
        is wired, not parse the output here.  5s timeout matches the
        diag-drop-in lint's tolerance for Python-heavy clients with
        slow cold-start imports."""
        try:
            r = subprocess.run(
                [client_bin, 'config', 'show', '--json',
                 '--config', config_path],
                capture_output=True, text=True,
                timeout=5.0, check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if r.returncode != 0:
            return False
        # JSON shape sanity — stdout should start with `{`.  Whitespace
        # / log noise from older clients gets stripped first.
        return r.stdout.lstrip().startswith("{")

    def _open_textual_wizard(self, client: str, client_bin: str,
                             config_path: str) -> None:
        """Push the in-TUI Textual config wizard for one (client, instance).

        The wizard is a ModalScreen — it dismisses with True on a
        successful Save (operator chose Save AND the client's
        ``config apply`` accepted the payload) or False on Cancel /
        Escape.  We refresh the instance table either way to pick up
        any side effects.
        """
        from .textual_wizard import TextualConfigWizardScreen

        def _after_wizard(saved: bool) -> None:
            tag = _display(config_path.rsplit("/", 1)[-1].removesuffix(".toml"))
            if saved:
                self._set_last(
                    f"[green]✔ saved via in-TUI wizard[/]  {client} @ {tag}")
            else:
                self._set_last(
                    f"[dim]cancelled in-TUI wizard[/]  {client} @ {tag}")
            self._refresh()

        self.app.push_screen(
            TextualConfigWizardScreen(
                client_name=client,
                client_bin=client_bin,
                config_path=config_path,
            ),
            _after_wizard,
        )

    def _do_remove(self) -> None:
        from ..mutation import confirm_and_run
        sel = self._selected()
        if sel is None:
            self._set_last("[yellow]select a row first[/]")
            return
        client, reporter = sel
        cmd = [_smd_binary(), 'admin', 'instance', 'remove', client, reporter,
               '--yes']
        confirm_and_run(
            self.app,
            title=f"Remove {client}@{_display(reporter)}?",
            body=(
                f"Run [bold]smd admin instance remove {client} {reporter} "
                f"--yes[/]\n\n"
                "Removes the per-instance config / env / sources files.  "
                "Does NOT stop the running unit — `smd admin instance disable` "
                "is a separate step.  Use [italic]--purge[/] from the CLI "
                "to also delete state / log dirs."
            ),
            cmd=cmd, sudo=True,
            on_complete=self._after_mutation,
        )

    def _do_add(self, *, dry_run: bool) -> None:
        from ..mutation import confirm_and_run
        client = self.query_one("#cf-add-client", Input).value.strip()
        reporter_raw = self.query_one("#cf-add-reporter", Input).value.strip()
        if not client or not reporter_raw:
            self._set_last("[red]Add: client and reporter ID both required[/]")
            return
        if client not in _INSTANCE_CAPABLE_CLIENTS:
            self._set_last(
                f"[red]Add: '{client}' isn't a known instance-capable "
                f"client.  Pick one of: {', '.join(_INSTANCE_CAPABLE_CLIENTS)}[/]"
            )
            return
        try:
            reporter = _parse_rid(reporter_raw)
        except Exception as exc:
            self._set_last(f"[red]Add: bad reporter ID: {exc}[/]")
            return
        cmd = [_smd_binary(), 'admin', 'instance', 'add', client, reporter]
        if dry_run:
            cmd.append('--dry-run')
            self._exec_async(cmd)
            return
        confirm_and_run(
            self.app,
            title=f"Add instance {client}@{_display(reporter)}?",
            body=(
                f"Run [bold]smd admin instance add {client} {reporter}[/]\n\n"
                "Creates per-instance config / env / sources skeletons.  "
                "Does NOT enable or start the systemd unit — that's a "
                "follow-up step (Edit the config, then "
                "`smd admin instance enable` from the CLI)."
            ),
            cmd=cmd, sudo=True,
            on_complete=self._after_mutation,
        )

    def _do_migrate_dry(self) -> None:
        """Dry-run scan.  Live migration is interactive (prompts the
        operator per candidate) and is CLI-only — the dry-run here
        surfaces what's eligible without changing anything."""
        cmd = [_smd_binary(), 'admin', 'instance', 'migrate']
        self._exec_async(cmd)

    # ------------------------------------------------------------------
    # subprocess plumbing
    # ------------------------------------------------------------------

    def _exec_async(self, cmd: list) -> None:
        log = self.query_one("#cf-output", RichLog)
        log.clear()
        log.write(f"$ {' '.join(cmd)}")
        self._set_last("[dim]running…[/]")
        self.run_worker(lambda: self._exec(cmd), thread=True, name="cf-exec")

    def _exec(self, cmd: list) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.app.call_from_thread(self._log, f"[error: {exc}]")
            self.app.call_from_thread(
                self._set_last, f"[red]failed: {exc}[/]")
            return subprocess.CompletedProcess(cmd, 127, "", str(exc))
        for line in (result.stdout or "").splitlines():
            self.app.call_from_thread(self._log, line)
        for line in (result.stderr or "").splitlines():
            self.app.call_from_thread(self._log, line)
        badge = ("[green]✔ exit 0[/]" if result.returncode == 0
                 else f"[red]✘ exit {result.returncode}[/]")
        self.app.call_from_thread(
            self._set_last, f"{badge}  {' '.join(cmd)}")
        return result

    def _log(self, msg: str) -> None:
        self.query_one("#cf-output", RichLog).write(msg)

    def _set_last(self, msg: str) -> None:
        self.query_one("#cf-last", Static).update(msg)

    def _after_mutation(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#cf-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ exit 0[/]  {argv}")
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]  {argv}")
        self._refresh()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _batch_unit_active(units: list[str]) -> dict[str, str]:
    """Batched ActiveState lookup.  Returns {unit: state}; missing or
    unknown units map to "unknown"."""
    if not units:
        return {}
    try:
        r = subprocess.run(
            ["systemctl", "show", "-pActiveState", "--value", *units],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {u: "unknown" for u in units}
    if r.returncode != 0:
        return {u: "unknown" for u in units}
    states = [s.strip() for s in (r.stdout or "").splitlines() if s.strip()]
    out: dict[str, str] = {}
    for u, s in zip(units, states + ["unknown"] * len(units)):
        out[u] = s or "unknown"
    return out


def _unit_markup(state: str) -> str:
    if state == "active":
        return "[green]active[/]"
    if state == "failed":
        return "[red]failed[/]"
    if state in ("activating", "deactivating"):
        return f"[yellow]{state}[/]"
    return f"[dim]{state}[/]"
