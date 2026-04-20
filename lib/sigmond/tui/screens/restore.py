"""Restore screen — browse for a sigmond config backup and apply it."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Iterable

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DirectoryTree, Label, Static

from ..mutation import confirm_and_run


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    return shutil.which('smd') or '/usr/local/sbin/smd'


class _BackupTree(DirectoryTree):
    """DirectoryTree that shows only directories and sigmond backup tarballs."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [
            p for p in paths
            if p.is_dir() or (
                p.is_file() and p.name.endswith('.tar.gz')
                and 'sigmond' in p.name.lower()
            )
        ]


class RestoreScreen(Vertical):
    """Browse for a sigmond config backup tarball and restore it."""

    DEFAULT_CSS = """
    RestoreScreen { padding: 1; }
    RestoreScreen .rs-title { text-style: bold; margin-bottom: 1; }
    RestoreScreen #rs-tree { height: 1fr; border: solid $primary-background; }
    RestoreScreen #rs-selected { margin-top: 1; color: $text-muted; }
    RestoreScreen #rs-actions { height: 3; margin-top: 1; }
    RestoreScreen #rs-actions Button { margin-right: 1; }
    RestoreScreen #rs-last { margin-top: 1; }
    """

    def compose(self):
        yield Static("Restore configuration", classes="rs-title")
        yield Static(
            "Browse to a  sigmond-config-*.tar.gz  file and press Enter or click twice to select it.",
            id="rs-hint",
        )
        yield _BackupTree(Path.home(), id="rs-tree")
        yield Static("No file selected", id="rs-selected")
        with Horizontal(id="rs-actions"):
            yield Button("Restore selected", id="rs-restore", variant="warning", disabled=True)
            yield Button("Refresh", id="rs-refresh", variant="default")
        yield Static("", id="rs-last")

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        self._selected = event.path
        self.query_one("#rs-selected", Static).update(
            f"[bold]Selected:[/] {event.path}"
        )
        self.query_one("#rs-restore", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "rs-refresh":
            tree = self.query_one("#rs-tree", _BackupTree)
            tree.reload()
        elif bid == "rs-restore":
            self._do_restore()

    def _do_restore(self) -> None:
        path = getattr(self, '_selected', None)
        if not path:
            return
        cmd = [_smd_binary(), 'config', 'restore', '--input', str(path)]
        confirm_and_run(
            self.app,
            title="Restore configuration?",
            body=(
                f"Extract [bold]{path.name}[/] over the live system.\n\n"
                "This will overwrite:\n"
                "  /etc/sigmond/   /etc/radio/   /etc/wsprdaemon/\n"
                "  /etc/hf-timestd/   /etc/psk-recorder/\n"
                "  systemd units, sudoers, cron, logrotate\n\n"
                "Services will [bold]not[/] be restarted automatically.\n"
                "Run  [bold]sudo smd apply[/]  afterwards to reconcile."
            ),
            cmd=cmd, sudo=True,
            on_complete=self._after_restore,
        )

    def _after_restore(self, result) -> None:
        last = self.query_one("#rs-last", Static)
        if result.returncode == 0:
            last.update(
                "[green]✔ restore complete[/]  —  run [bold]sudo smd apply[/] to restart services"
            )
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]")
