"""Update screen — TUI counterpart to `smd update`.

One action: pull the latest code and re-apply.  The CLI does the real
work (`git pull` in the client dir followed by `smd install`).  TUI
provides the confirmation gate + exit-code readout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

from textual.containers import Vertical
from textual.widgets import Button, Static

from ..mutation import confirm_and_run


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


class UpdateScreen(Vertical):
    """Pull the latest code and re-apply."""

    DEFAULT_CSS = """
    UpdateScreen {
        padding: 2;
    }
    UpdateScreen .up-title {
        text-style: bold;
        margin-bottom: 1;
    }
    UpdateScreen .up-body {
        margin-bottom: 1;
        color: $text-muted;
    }
    UpdateScreen #up-button {
        margin-top: 1;
        width: auto;
    }
    UpdateScreen #up-last {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def compose(self):
        yield Static("Update", classes="up-title")
        yield Static(
            "Pulls the latest wsprdaemon-client and re-runs the "
            "catalog install pass (deps, unit files, scripts).",
            classes="up-body")
        yield Static(
            "Equivalent to:  [cyan bold]sudo smd update[/]",
            classes="up-body")
        yield Button("Update now", id="up-button", variant="primary")
        yield Static("", id="up-last")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "up-button":
            return

        cmd = [_smd_binary(), 'update']
        confirm_and_run(
            self.app,
            title="Run update?",
            body=("This will `git pull` in the wsprdaemon-client dir "
                  "and re-run the install pass for every configured "
                  "component.  Running services may restart."),
            cmd=cmd, sudo=True,
            on_complete=self._after_update,
        )

    def _after_update(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#up-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]\u2714 exit 0[/]  {argv}")
        else:
            last.update(f"[red]\u2718 exit {result.returncode}[/]  {argv}")
