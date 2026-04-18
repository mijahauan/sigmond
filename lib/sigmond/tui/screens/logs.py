"""Logs screen — TUI counterpart to `smd log <client>` (read-only modes).

Per-component `journalctl --follow` or `tail -f` of inventory file-logs.
Streams output live into a RichLog widget via a background worker that
shells out to the same commands `smd log` uses.

The log-level mutation mode of `smd log --level` lives on the CLI for now;
it's a mutation and belongs on the Lifecycle/Operate side of the IA.
"""

from __future__ import annotations

import subprocess
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, RichLog, Select, Static
from textual.worker import get_current_worker


def _enabled_components() -> list:
    try:
        from ...topology import load_topology
        return load_topology().enabled_components()
    except Exception:
        return []


def _resolve_unit_names(component: str) -> list:
    """Best-effort: resolve systemd units for a component, else fall back
    to a wildcard.  Mirrors how bin/smd.cmd_log picks units."""
    try:
        from ...topology import load_topology
        from ...lifecycle import resolve_units

        topology = load_topology()
        all_enabled = topology.enabled_components()
        units = resolve_units([component], all_enabled)
        names = [u.unit for u in units if not u.orphaned]
        if names:
            return names
    except Exception:
        pass
    return [f"{component}*"]


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
        height: 1fr;
        border: solid $primary-background;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._proc: Optional[subprocess.Popen] = None

    def compose(self):
        yield Static("Logs — live tail per component", classes="lg-title")
        with Horizontal(id="lg-controls"):
            options = [(c, c) for c in _enabled_components()]
            yield Select(options=options, id="lg-picker",
                         prompt="Component\u2026", allow_blank=True)
            yield Button("Follow journal", id="lg-journal", variant="primary")
            yield Button("Tail files",     id="lg-files",   variant="default")
            yield Button("Stop",           id="lg-stop",    variant="warning")
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

        if mode == 'journal':
            units = _resolve_unit_names(comp)
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
