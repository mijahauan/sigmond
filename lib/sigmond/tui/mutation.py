"""Shared mutation plumbing for the TUI.

Two pieces that every mutation screen (Lifecycle, Install, Update)
reuses:

  1. ConfirmModal — a blocking yes/no dialog.  Operator must
     arrow-and-Enter on Yes; Escape cancels.  Default focus is No.
  2. suspend_and_run_sudo — suspend the app, run the command under
     sudo (so the operator can type their password in the real
     terminal and see live CLI output), then resume the app.  The
     CLI's own output appears directly in the terminal; the TUI is
     not an intermediary.  This is the pattern Deep Dive (radiod
     screen) already uses for ka9q's own TUI.

Net effect: mutations run as `sudo smd <verb>` exactly as they would
from the shell, with the TUI providing the confirmation gate and an
after-the-fact exit-code readout.  No parallel Python lifecycle-lock
management — the `smd` subprocess acquires the lock per CONTRACT
v0.5 §5.5 and releases it on exit.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation.  Dismisses with True on Yes, False otherwise."""

    BINDINGS = [
        Binding("escape", "dismiss_no", "No", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Grid {
        width: 68;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }
    ConfirmModal #cm-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ConfirmModal #cm-cmd {
        margin: 1 0;
        color: $text-muted;
    }
    ConfirmModal Horizontal {
        height: auto;
        align: right middle;
    }
    ConfirmModal Button {
        margin-left: 1;
    }
    """

    def __init__(self, title: str, body: str, cmd_preview: Optional[str] = None,
                 yes_label: str = "Yes",   yes_variant: str = "primary",
                 no_label:  str = "Cancel", no_variant:  str = "default",
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._body = body
        self._cmd_preview = cmd_preview
        self._yes_label = yes_label
        self._yes_variant = yes_variant
        self._no_label = no_label
        self._no_variant = no_variant

    def compose(self) -> ComposeResult:
        with Grid():
            yield Static(self._title, id="cm-title")
            yield Static(self._body, id="cm-body")
            if self._cmd_preview:
                yield Static(f"\n[dim]$ {self._cmd_preview}[/]", id="cm-cmd")
            with Horizontal():
                yield Button(self._no_label,  id="cm-no",  variant=self._no_variant)
                yield Button(self._yes_label, id="cm-yes", variant=self._yes_variant)

    def on_mount(self) -> None:
        # Default focus on No so a stray Enter cancels rather than proceeds.
        self.query_one("#cm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cm-yes":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_dismiss_no(self) -> None:
        self.dismiss(False)


def suspend_and_run_sudo(app: App, cmd: list) -> subprocess.CompletedProcess:
    """Suspend the TUI, run ``sudo <cmd>`` in the real terminal, resume.

    The operator can enter their password and watch progress as they
    would from the shell.  Returns the CompletedProcess; callers can
    inspect returncode without parsing output (the CLI already did the
    rendering).
    """
    argv = ['sudo', *cmd]
    with app.suspend():
        result = subprocess.run(argv, check=False)
    return result


def confirm_and_run(
    app: App,
    title: str,
    body: str,
    cmd: list,
    sudo: bool = True,
    on_complete: Optional[Callable[[subprocess.CompletedProcess], None]] = None,
) -> None:
    """Push a ConfirmModal.  If the operator accepts, run the command
    (with sudo by default) and call ``on_complete`` back on the main
    thread with the result.

    Non-blocking — returns immediately after pushing the modal.
    """
    cmd_preview = ' '.join(('sudo', *cmd) if sudo else cmd)

    def _after_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        runner = suspend_and_run_sudo if sudo else _run_plain
        result = runner(app, cmd)
        if on_complete is not None:
            on_complete(result)

    app.push_screen(
        ConfirmModal(title=title, body=body, cmd_preview=cmd_preview),
        _after_confirm,
    )


def _run_plain(app: App, cmd: list) -> subprocess.CompletedProcess:
    """Non-sudo variant for completeness; still suspends so CLI output
    lands in the terminal."""
    with app.suspend():
        result = subprocess.run(cmd, check=False)
    return result
