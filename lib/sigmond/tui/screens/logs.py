"""Logs screen — TUI counterpart to `smd log <client>`.

Read-only modes: per-component `journalctl --follow` or `tail -f` of
inventory file-logs.  Streams subprocess output live into a RichLog
widget.  Mutation mode: set CLIENT_LOG_LEVEL via `sudo smd log
<client> --level <LEVEL>`, gated by a confirm modal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, RichLog, Select, Static
from textual.worker import get_current_worker

from ..mutation import confirm_and_run


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


def _installed_components() -> dict:
    """Map enabled component name -> bool (installed on this host).

    Components enabled in topology but absent from the catalog default
    to ``True`` so we don't accidentally hide something the catalog
    hasn't been taught about.
    """
    try:
        from ...topology import load_topology
        from ...catalog import load_catalog
    except Exception:
        return {}
    try:
        enabled = load_topology().enabled_components()
    except Exception:
        return {}
    try:
        catalog = load_catalog()
    except Exception:
        catalog = {}
    result: dict = {}
    for comp in enabled:
        entry = catalog.get(comp)
        result[comp] = entry.is_installed() if entry is not None else True
    return result


# Sentinel for the "no instance filter" Select value.
_INSTANCE_ALL = "__all__"

# Per-recorder components that have templated systemd units; the
# instance selector applies only when one of these is chosen.
_TEMPLATED_COMPONENTS = (
    "psk-recorder",
    "wspr-recorder",
    "hfdl-recorder",
    "codar-sounder",
)


def _instance_options_for_component(component: str) -> list:
    """Build the (label, value) list for the per-instance dropdown.

    Returns the all-instances sentinel + every instance unit known to
    systemctl for this component (configured per-instance reporter IDs
    AND legacy radiod-keyed instances).  For non-templated components
    returns the sentinel only.
    """
    options: list = [("(all instances)", _INSTANCE_ALL)]
    if component not in _TEMPLATED_COMPONENTS:
        return options
    try:
        from ...instance import (
            list_instances, detect_migration_candidates,
        )
    except Exception:
        return options
    for i in list_instances(catalog_clients=[component]):
        options.append((i.reporter_id, i.reporter_id))
    try:
        for c in detect_migration_candidates():
            if c.client == component:
                label = f"{c.old_instance} (legacy)"
                options.append((label, c.old_instance))
    except Exception:
        pass
    return options


def _resolve_unit_names(component: str) -> list:
    """Resolve systemd units for a component.

    Returns the list of resolved unit names (empty list if the component
    is enabled in topology but not yet installed, i.e. no deploy.toml
    and no fallback shim).  Never returns a wildcard — callers must
    handle the empty case with an actionable message to the operator.
    """
    try:
        from ...topology import load_topology
        from ...lifecycle import resolve_units

        topology = load_topology()
        all_enabled = topology.enabled_components()
        units = resolve_units([component], all_enabled)
        return [u.unit for u in units if not u.orphaned]
    except Exception:
        return []


def _resolve_log_paths(component: str) -> list:
    """Return inventory log_paths for a component, flattened to a list."""
    try:
        from ...log_cmd import flatten_log_paths, get_inventory_log_paths

        paths = get_inventory_log_paths(component)
        if not paths:
            return []
        return flatten_log_paths(paths)
    except Exception:
        return []


class LogsScreen(Vertical):
    """Per-component live log tailing."""

    DEFAULT_CSS = """
    LogsScreen {
        padding: 1;
    }
    LogsScreen .lg-title {
        text-style: bold;
        margin-bottom: 1;
    }
    LogsScreen #lg-controls {
        height: 3;
        margin-bottom: 1;
    }
    LogsScreen #lg-controls Button {
        margin-right: 1;
    }
    LogsScreen #lg-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    LogsScreen #lg-output {
        height: 22;
        border: solid $primary-background;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._proc: Optional[subprocess.Popen] = None
        self._install_state: dict = {}

    def compose(self):
        yield Static("Logs — live tail per component", classes="lg-title")
        with Horizontal(id="lg-controls"):
            self._install_state = _installed_components()
            options = [
                (c if installed else f"{c}  (not installed)", c)
                for c, installed in sorted(self._install_state.items())
            ]
            yield Select(options=options, id="lg-picker",
                         prompt="Component\u2026", allow_blank=True)
            # Per-instance unit selector (sigmond
            # MULTI-INSTANCE-ARCHITECTURE.md \u00a78).  Populated when the
            # component picker selects a templated recorder.  Blank
            # default \u2192 follow the existing component-aggregate journal
            # (one `-u <client>@<inst>.service` per known unit).
            yield Select(options=[("(all instances)", _INSTANCE_ALL)],
                         value=_INSTANCE_ALL, id="lg-instance",
                         prompt="Instance\u2026", allow_blank=True)
            yield Button("Follow journal", id="lg-journal", variant="primary")
            yield Button("Tail files",     id="lg-files",   variant="default")
            yield Button("Stop",           id="lg-stop",    variant="warning")
        with Horizontal(id="lg-level-row"):
            level_opts = [(lvl, lvl) for lvl in
                          ("DEBUG", "INFO", "WARN", "ERROR")]
            yield Select(options=level_opts, id="lg-level",
                         prompt="Log level\u2026", allow_blank=True)
            yield Button("Set level", id="lg-set-level",
                         variant="warning")
        yield Static("", id="lg-status")
        yield RichLog(id="lg-output", highlight=False, markup=False,
                      max_lines=2000, wrap=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "lg-journal":
            self._start('journal')
        elif bid == "lg-files":
            self._start('files')
        elif bid == "lg-stop":
            self._stop(user_requested=True)
        elif bid == "lg-set-level":
            self._set_level()

    def on_select_changed(self, event: Select.Changed) -> None:
        """Repopulate the instance dropdown when component changes."""
        if event.select.id != "lg-picker":
            return
        comp = str(event.value) if event.value is not None else ""
        if not comp or event.value is Select.NULL:
            return
        instance_sel = self.query_one("#lg-instance", Select)
        instance_sel.set_options(_instance_options_for_component(comp))
        instance_sel.value = _INSTANCE_ALL

    def _set_level(self) -> None:
        comp = self._current_component()
        if not comp:
            self._set_status("[yellow]Pick a component first[/]")
            return
        if self._install_state.get(comp, True) is False:
            self._set_status(
                f"[yellow]{comp} is not installed — "
                f"install before changing log level[/]")
            return
        picker = self.query_one("#lg-level", Select)
        level = picker.value
        if level is None or level is Select.NULL:
            self._set_status("[yellow]Pick a log level first[/]")
            return
        level = str(level)
        cmd = [_smd_binary(), 'log', comp, '--level', level]
        confirm_and_run(
            self.app,
            title=f"Set {comp} log level to {level}?",
            body=(f"Writes CLIENT_LOG_LEVEL_{comp.upper().replace('-', '_')}"
                  f"={level} to coordination.env and sends SIGHUP to the "
                  f"unit(s) so the new level takes effect immediately."),
            cmd=cmd, sudo=True,
            on_complete=self._after_set_level,
        )

    def _after_set_level(self, result: subprocess.CompletedProcess) -> None:
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            self._set_status(f"[green]✔ exit 0[/]  {argv}")
        else:
            self._set_status(f"[red]✘ exit {result.returncode}[/]  {argv}")

    def on_unmount(self) -> None:
        # Prevent zombie tails when the screen swaps out.
        self._stop(user_requested=False)

    def _current_component(self) -> Optional[str]:
        picker = self.query_one("#lg-picker", Select)
        value = picker.value
        # Select.NULL is the "nothing selected" sentinel.  Select.BLANK
        # exists in older/newer Textuals as a boolean, not a sentinel —
        # compare identity against NULL specifically.
        if value is None or value is Select.NULL:
            return None
        return str(value)

    def _set_status(self, text: str) -> None:
        self.query_one("#lg-status", Static).update(text)

    def _start(self, mode: str) -> None:
        self._stop(user_requested=False)

        comp = self._current_component()
        if not comp:
            self._set_status("[yellow]Pick a component first[/]")
            return

        if self._install_state.get(comp, True) is False:
            self._set_status(
                f"[yellow]{comp}: enabled in topology but not installed. "
                f"Install first:  [cyan]sudo smd install {comp}[/][/]"
            )
            return

        if mode == 'journal':
            units = _resolve_unit_names(comp)
            if not units:
                self._set_status(
                    f"[yellow]{comp}: no systemd units resolved "
                    f"(no deploy.toml or shim). Is it fully installed?[/]"
                )
                return
            # Per-instance filter (sigmond MULTI-INSTANCE-ARCHITECTURE.md §8):
            # narrow the unit list to the chosen instance only.
            instance = self.query_one("#lg-instance", Select).value
            if instance not in (None, Select.NULL, Select.BLANK, _INSTANCE_ALL):
                target_unit = f"{comp}@{instance}.service"
                if target_unit in units:
                    units = [target_unit]
                else:
                    # Operator selected an instance that isn't in
                    # _resolve_unit_names's list — could be a legacy
                    # radiod-keyed unit.  Pass it directly to journalctl;
                    # journalctl will report "no entries" cleanly if
                    # the unit doesn't actually exist.
                    units = [target_unit]
            cmd = ['journalctl', '--follow', '--no-hostname', '-n', '50']
            for u in units:
                cmd.extend(['-u', u])
            status = f"following journal for: {', '.join(units)}"
        elif mode == 'files':
            from pathlib import Path
            paths = _resolve_log_paths(comp)
            existing = [p for p in paths if Path(p).exists()]
            if not existing:
                self._set_status(
                    f"[yellow]{comp}: no existing log files "
                    f"(inventory log_paths empty or files missing)[/]")
                return
            cmd = ['tail', '-f', '-n', '20', *existing]
            status = f"tailing {len(existing)} file(s): {', '.join(existing)}"
        else:
            return

        log = self.query_one("#lg-output", RichLog)
        log.clear()
        log.write(f"$ {' '.join(cmd)}")
        self._set_status(status)
        self.run_worker(lambda: self._tail(cmd), thread=True, name="lg-tail")

    def _tail(self, cmd: list) -> None:
        """Worker body — stream subprocess stdout into the RichLog widget.

        Any stderr is merged into stdout so errors like 'journalctl:
        unit not found' land in the log pane.  The worker exits when
        either the subprocess closes stdout (command exited) or the
        worker is cancelled (user pressed Stop or the screen unmounted).
        """
        worker = get_current_worker()
        log = self.query_one("#lg-output", RichLog)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.app.call_from_thread(log.write, f"[error launching {cmd[0]}: {exc}]")
            return

        try:
            for line in iter(self._proc.stdout.readline, ''):
                if worker.is_cancelled:
                    break
                self.app.call_from_thread(log.write, line.rstrip('\n'))
        finally:
            try:
                if self._proc and self._proc.stdout:
                    self._proc.stdout.close()
            except Exception:
                pass
            rc = None
            try:
                if self._proc:
                    rc = self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            self._proc = None
            self.app.call_from_thread(
                log.write, f"[exit {rc if rc is not None else 'terminated'}]")

    def _stop(self, user_requested: bool) -> None:
        """Terminate the tail process if any; cancel the worker."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except Exception:
                    pass

        # Cancel any running tail worker on this screen.
        try:
            for w in list(self.workers):
                if w.name == "lg-tail":
                    w.cancel()
        except Exception:
            pass

        if user_requested:
            self._set_status("stopped")
