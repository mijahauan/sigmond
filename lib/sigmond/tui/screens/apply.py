"""Apply screen — TUI counterpart to `smd apply`.

Reconciles running services with the current coordination + topology
config.  Offers a dry-run preview (`smd apply --dry-run`, no sudo) and
a live apply (`sudo smd apply`, gated by a confirm modal).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, RichLog, Static

from ..mutation import confirm_and_run


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


class ApplyScreen(Vertical):
    """Reconcile running services with current config."""

    DEFAULT_CSS = """
    ApplyScreen {
        padding: 1;
    }
    ApplyScreen .ap-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ApplyScreen .ap-body {
        margin-bottom: 1;
        color: $text-muted;
    }
    ApplyScreen #ap-controls {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }
    ApplyScreen #ap-controls Button {
        margin-right: 1;
    }
    ApplyScreen #ap-output {
        height: 20;
        border: solid $primary-background;
    }
    ApplyScreen #ap-last {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def compose(self):
        yield Static("Apply", classes="ap-title")
        yield Static(
            "Reconciles the running system with topology + coordination "
            "config.  Dry-run prints the plan only; Apply performs it.",
            classes="ap-body")
        yield Static(
            "Equivalent to:  [cyan]smd apply --dry-run[/]  /  "
            "[cyan bold]sudo smd apply[/]",
            classes="ap-body")
        with Horizontal(id="ap-controls"):
            yield Button("Dry-run", id="ap-dry", variant="primary")
            yield Button("Apply now", id="ap-run", variant="warning")
        yield RichLog(id="ap-output", highlight=False, markup=False,
                      max_lines=2000, wrap=False)
        yield Static("", id="ap-last")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ap-dry":
            self._run_dry()
        elif event.button.id == "ap-run":
            self._run_apply()

    def _run_dry(self) -> None:
        cmd = [_smd_binary(), 'apply', '--dry-run']
        log = self.query_one("#ap-output", RichLog)
        log.clear()
        log.write(f"$ {' '.join(cmd)}")
        self.query_one("#ap-last", Static).update("[dim]running dry-run…[/]")
        self.run_worker(lambda: self._exec(cmd), thread=True, name="ap-dry")

    def _exec(self, cmd: list) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.app.call_from_thread(
                self.query_one("#ap-output", RichLog).write,
                f"[error: {exc}]",
            )
            self.app.call_from_thread(
                self.query_one("#ap-last", Static).update,
                f"[red]failed to launch: {exc}[/]",
            )
            return subprocess.CompletedProcess(cmd, 127, "", str(exc))

        log = self.query_one("#ap-output", RichLog)
        for line in (result.stdout or "").splitlines():
            self.app.call_from_thread(log.write, line)
        for line in (result.stderr or "").splitlines():
            self.app.call_from_thread(log.write, line)
        badge = ("[green]✔ exit 0[/]" if result.returncode == 0
                 else f"[red]✘ exit {result.returncode}[/]")
        self.app.call_from_thread(
            self.query_one("#ap-last", Static).update,
            f"{badge}  {' '.join(cmd)}",
        )
        return result

    def _run_apply(self) -> None:
        cmd = [_smd_binary(), 'apply']
        confirm_and_run(
            self.app,
            title="Run apply?",
            body=("This reconciles running services with the current "
                  "coordination + topology config.  Services may "
                  "restart.  Dry-run first if you want a preview."),
            cmd=cmd, sudo=True,
            on_complete=self._after_apply,
        )

    def _after_apply(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#ap-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ exit 0[/]  {argv}")
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]  {argv}")
