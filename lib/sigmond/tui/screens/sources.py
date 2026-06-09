"""Sources screen — TUI counterpart to `smd admin sources` (CLI-V2-SPEC.md §3 Wiring).

Per-client sensor-feed selection: which radiod control plane / KiwiSDR
(future: magnetometer, VLF) each recorder consumes from.  Read-and-apply
in the TUI; add/remove of individual selections still happens via the
CLI for now (`smd admin sources add <client> <kind>:<id>` /
`smd admin sources remove <client> <kind>:<id>`).

Mirrors the apply.py shape: a state view (sources list output) + a row
of action buttons + an output log + a status line.
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


class SourcesScreen(Vertical):
    """Per-client sensor-feed selection (radiod / KiwiSDR / future mag / vlf)."""

    DEFAULT_CSS = """
    SourcesScreen {
        padding: 1;
    }
    SourcesScreen .sc-title {
        text-style: bold;
        margin-bottom: 1;
    }
    SourcesScreen .sc-body {
        margin-bottom: 1;
        color: $text-muted;
    }
    SourcesScreen #sc-controls {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }
    SourcesScreen #sc-controls Button {
        margin-right: 1;
    }
    SourcesScreen #sc-output {
        height: 22;
        border: solid $primary-background;
    }
    SourcesScreen #sc-last {
        margin-top: 1;
        color: $text-muted;
    }
    SourcesScreen .sc-cli-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def compose(self):
        yield Static("Sources — per-client sensor-feed selection", classes="sc-title")
        yield Static(
            "Each recorder client consumes from one or more sensor feeds "
            "(radiod control plane, KiwiSDR; future: magnetometer, VLF).  "
            "Selections are stored at /etc/sigmond/clients/<client>.sources.toml.",
            classes="sc-body")
        yield Static(
            "Equivalent to:  [cyan]smd admin sources list[/]  /  "
            "[cyan]smd admin sources apply --dry-run[/]  /  "
            "[cyan bold]smd admin sources apply[/]",
            classes="sc-body")
        with Horizontal(id="sc-controls"):
            yield Button("Refresh list", id="sc-list", variant="primary")
            yield Button("Apply (dry-run)", id="sc-dry", variant="default")
            yield Button("Apply now", id="sc-run", variant="warning")
        yield RichLog(id="sc-output", highlight=False, markup=False,
                      max_lines=2000, wrap=False)
        yield Static("", id="sc-last")
        yield Static(
            "[dim]Edit selections via CLI for now:[/]\n"
            "  [cyan]smd admin sources add <client> <kind>:<id>[/]\n"
            "  [cyan]smd admin sources remove <client> <kind>:<id>[/]\n"
            "[dim]Then return here and press Apply.[/]\n"
            "\n"
            "[dim]Per-instance reporters (sigmond "
            "MULTI-INSTANCE-ARCHITECTURE.md §4) use the [/]"
            "[cyan]<client>@<reporter-id>[/]"
            "[dim] form once the sources CLI grows per-instance "
            "awareness (Phase 7 — pending):[/]\n"
            "  [cyan]smd admin sources add wspr-recorder@AC0G-B1 "
            "radiod:my-rx888[/]\n"
            "  [cyan]smd admin sources remove wspr-recorder@AC0G-B1 "
            "kiwi:grape-corner-1[/]\n"
            "[dim]Until then, selections are per-client.[/]",
            classes="sc-cli-hint", markup=True)

    def on_mount(self) -> None:
        # Load current state on first display.
        self._run_list()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sc-list":
            self._run_list()
        elif event.button.id == "sc-dry":
            self._run_dry()
        elif event.button.id == "sc-run":
            self._run_apply()

    def _run_list(self) -> None:
        cmd = [_smd_binary(), 'admin', 'sources', 'list']
        log = self.query_one("#sc-output", RichLog)
        log.clear()
        log.write(f"$ {' '.join(cmd)}")
        self.query_one("#sc-last", Static).update("[dim]loading current state…[/]")
        self.run_worker(lambda: self._exec(cmd), thread=True, name="sc-list")

    def _run_dry(self) -> None:
        cmd = [_smd_binary(), 'admin', 'sources', 'apply', '--dry-run']
        log = self.query_one("#sc-output", RichLog)
        log.clear()
        log.write(f"$ {' '.join(cmd)}")
        self.query_one("#sc-last", Static).update("[dim]running apply dry-run…[/]")
        self.run_worker(lambda: self._exec(cmd), thread=True, name="sc-dry")

    def _exec(self, cmd: list) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.app.call_from_thread(
                self.query_one("#sc-output", RichLog).write,
                f"[error: {exc}]",
            )
            self.app.call_from_thread(
                self.query_one("#sc-last", Static).update,
                f"[red]failed to launch: {exc}[/]",
            )
            return subprocess.CompletedProcess(cmd, 127, "", str(exc))

        log = self.query_one("#sc-output", RichLog)
        for line in (result.stdout or "").splitlines():
            self.app.call_from_thread(log.write, line)
        for line in (result.stderr or "").splitlines():
            self.app.call_from_thread(log.write, line)
        badge = ("[green]✔ exit 0[/]" if result.returncode == 0
                 else f"[red]✘ exit {result.returncode}[/]")
        self.app.call_from_thread(
            self.query_one("#sc-last", Static).update,
            f"{badge}  {' '.join(cmd)}",
        )
        return result

    def _run_apply(self) -> None:
        cmd = [_smd_binary(), 'admin', 'sources', 'apply']
        confirm_and_run(
            self.app,
            title="Apply sources selections?",
            body=("This renders every client's selection into its config file "
                  "(/etc/sigmond/clients/<client>.sources.toml is the source of "
                  "truth; this writes the [[source]] tables the recorders read).  "
                  "Run Apply (dry-run) first if you want to preview."),
            cmd=cmd, sudo=True,
            on_complete=self._after_apply,
        )

    def _after_apply(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#sc-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ exit 0[/]  {argv}")
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]  {argv}")
        # Refresh the list so the operator sees the new state.
        self._run_list()
