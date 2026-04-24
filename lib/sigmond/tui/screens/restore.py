"""Restore screen — pick a sigmond config backup and apply it."""

from __future__ import annotations

import os
import pwd
import shutil
import sys
from pathlib import Path

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, ListItem, ListView, Static

from ..mutation import confirm_and_run


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    return shutil.which('smd') or '/usr/local/sbin/smd'


def _real_home() -> Path:
    """Return the invoking user's home dir even when running under sudo."""
    sudo_user = os.environ.get('SUDO_USER')
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def _find_backups() -> list[Path]:
    """Glob sigmond-config-*.tar.gz in the real user's home dir."""
    home = _real_home()
    return sorted(home.glob('sigmond-config-*.tar.gz'), reverse=True)


class RestoreScreen(Vertical):
    """Pick a sigmond config backup file and restore it."""

    DEFAULT_CSS = """
    RestoreScreen { padding: 1; }
    RestoreScreen .rs-title { text-style: bold; margin-bottom: 1; }
    RestoreScreen #rs-hint  { color: $text-muted; margin-bottom: 1; }
    RestoreScreen #rs-list  { height: 14; border: solid $primary-background; }
    RestoreScreen #rs-selected { margin-top: 1; color: $text-muted; }
    RestoreScreen #rs-actions  { height: 3; margin-top: 1; }
    RestoreScreen #rs-actions Button { margin-right: 1; }
    RestoreScreen #rs-last { margin-top: 1; }
    """

    def compose(self):
        yield Static("Restore configuration", classes="rs-title")
        yield Static(
            f"Backups found in {_real_home()}  —  select one and press Restore.",
            id="rs-hint",
        )
        yield ListView(id="rs-list")
        yield Static("[dim]No file selected[/]", id="rs-selected")
        with Horizontal(id="rs-actions"):
            yield Button("Restore selected", id="rs-restore",
                         variant="warning", disabled=True)
            yield Button("Refresh", id="rs-refresh", variant="default")
        yield Static("", id="rs-last")

    def on_mount(self) -> None:
        self._selected: Path | None = None
        self._refresh_list()

    def _refresh_list(self) -> None:
        lv = self.query_one("#rs-list", ListView)
        lv.clear()
        backups = _find_backups()
        if backups:
            for p in backups:
                kb = p.stat().st_size // 1024
                lv.append(ListItem(Static(f"{p.name}  [dim]({kb} KB)[/]"),
                                   name=str(p)))
            self.query_one("#rs-hint", Static).update(
                f"{len(backups)} backup(s) in {_real_home()}  —  select one and press Restore."
            )
        else:
            lv.append(ListItem(Static("[dim]No sigmond-config-*.tar.gz files found[/]")))
            self.query_one("#rs-hint", Static).update(
                f"No backups found in {_real_home()}.  "
                "Run  [bold]smd config backup[/]  first."
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = event.item.name
        if not name:
            return
        self._selected = Path(name)
        self.query_one("#rs-selected", Static).update(
            f"[bold]Selected:[/] {self._selected.name}"
        )
        self.query_one("#rs-restore", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rs-refresh":
            self._selected = None
            self.query_one("#rs-restore", Button).disabled = True
            self.query_one("#rs-selected", Static).update("[dim]No file selected[/]")
            self._refresh_list()
        elif event.button.id == "rs-restore":
            self._do_restore()

    def _do_restore(self) -> None:
        if not self._selected:
            return
        kb = self._selected.stat().st_size // 1024
        confirm_and_run(
            self.app,
            title="Restore configuration?",
            body=(
                f"Extract [bold]{self._selected.name}[/]  ({kb} KB)\n"
                "over the live system.\n\n"
                "Overwrites:\n"
                "  /etc/sigmond/   /etc/radio/   /etc/wsprdaemon/\n"
                "  /etc/hf-timestd/   /etc/psk-recorder/\n"
                "  systemd units, sudoers, cron, logrotate\n\n"
                "Services are [bold]not[/] restarted automatically —\n"
                "run  [bold]sudo smd apply[/]  afterwards."
            ),
            cmd=[_smd_binary(), 'config', 'restore', '--input', str(self._selected)],
            sudo=True,
            on_complete=self._after_restore,
        )

    def _after_restore(self, result) -> None:
        last = self.query_one("#rs-last", Static)
        if result.returncode == 0:
            last.update(
                "[green]✔ restore complete[/]  —  run [bold]sudo smd apply[/] "
                "to restart services"
            )
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]")
