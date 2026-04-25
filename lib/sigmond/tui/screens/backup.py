"""Backup screen — snapshot all sigmond + client config to a local tar.gz."""

from __future__ import annotations

import os
import shutil
import sys

from textual.containers import Vertical
from textual.widgets import Button, Static

from ..mutation import confirm_and_run


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    return shutil.which('smd') or '/usr/local/sbin/smd'


_WHAT = """\
Paths included (skipped silently if absent):

  /etc/sigmond/          topology, catalog
  /etc/radio/            radiod channel configs
  /etc/wsprdaemon/       wsprdaemon.conf, env/, certs, frpc.toml
  /etc/hf-timestd/       timestd receiver configs
  /etc/psk-recorder/     PSK recorder config
  /etc/systemd/system/wsprdaemon.*
  /etc/sudoers.d/wsprdaemon
  /etc/cron.d/  (sigmond-managed entries)
  /etc/logrotate.d/  (sigmond-managed entries)

Saved to  ~/sigmond-config-<hostname>-<date>.tar.gz

Restore after OS reinstall:
  ./install.sh
  sudo tar xzf sigmond-config-*.tar.gz -C /
  sudo smd apply
"""


class BackupScreen(Vertical):
    """Snapshot all configuration to a local tar.gz."""

    DEFAULT_CSS = """
    BackupScreen { padding: 1; }
    BackupScreen .bs-title { text-style: bold; margin-bottom: 1; }
    BackupScreen #bs-what { margin-bottom: 1; color: $text-muted; }
    BackupScreen #bs-last { margin-top: 1; }
    """

    def compose(self):
        yield Static("Backup configuration", classes="bs-title")
        yield Static(_WHAT, id="bs-what")
        yield Button("Create backup now", id="bs-run", variant="success")
        yield Static("", id="bs-last")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "bs-run":
            return
        confirm_and_run(
            self.app,
            title="Create config backup?",
            body=_WHAT,
            cmd=[_smd_binary(), 'config', 'backup'],
            sudo=True,
            on_complete=self._after_backup,
        )

    def _after_backup(self, result) -> None:
        last = self.query_one("#bs-last", Static)
        if result.returncode == 0:
            last.update("[green]✔ backup complete — check ~/ for the tar.gz[/]")
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]")
