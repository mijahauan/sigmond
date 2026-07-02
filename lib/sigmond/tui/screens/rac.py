"""RAC (Remote Access Channel) configuration and status screen.

Drives the CURRENT sigmond-rac model: the WD admin assigns a per-station
``user`` / ``token`` / unique ``remotePort``(s) on gw2; activation fills
those into the install-rendered ``/etc/sigmond/frpc.toml.template`` and
writes ``/etc/sigmond/frpc.toml``, which un-gates ``wd-rac.service``
(ConditionPathExists).  The component is named ``sigmond-rac``; the unit
keeps the legacy name ``wd-rac.service`` (RAC-C-005 — do not conflate).

The previous screen drove a defunct ``wd-rac`` topology component with a
``rac_number`` → port-base-35800 scheme that the current repo never
reads; its Apply produced no working tunnel.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Static

from ...rac_config import (
    FRPC_CONFIG, FRPC_TEMPLATE, placeholder, read_frpc_values,
    render_frpc_config,
)
from ..mutation import ConfirmModal, confirm_and_run, suspend_and_run_sudo


def _smd_binary() -> str:
    """Resolve the smd CLI binary (same helper as the other mutation
    screens — see backup.py / apply.py)."""
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    return shutil.which('smd') or '/usr/local/bin/smd'


_ADMIN_URL = 'http://127.0.0.1:7500'


def _read_text_sudo(path: Path) -> str:
    """Read a root-owned 0640 file, falling back to sudo -n cat."""
    try:
        return path.read_text()
    except PermissionError:
        r = subprocess.run(['sudo', '-n', 'cat', str(path)],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        return r.stdout if r.returncode == 0 else ''
    except OSError:
        return ''


def _frpc_admin_status() -> dict | None:
    """Query the frpc client admin API.  Returns parsed JSON or None."""
    try:
        with urllib.request.urlopen(f'{_ADMIN_URL}/api/status',
                                    timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _service_active() -> bool:
    r = subprocess.run(['systemctl', 'is-active', 'wd-rac'],
                       capture_output=True, text=True)
    return r.stdout.strip() == 'active'


def _service_enabled() -> bool:
    r = subprocess.run(['systemctl', 'is-enabled', 'wd-rac'],
                       capture_output=True, text=True)
    return r.stdout.strip() == 'enabled'


def _render_status() -> str:
    """Build a Rich-markup status string from live service + admin API."""
    active = _service_active()
    if not active:
        if not Path(FRPC_CONFIG).exists():
            return ("[dim]Not activated — enter the WD-admin assignment "
                    "above and press Activate.[/dim]")
        if not _service_enabled():
            return "[dim]○ wd-rac disabled (press Activate to re-enable)[/dim]"
        return "[red]● wd-rac.service stopped[/red]"

    data = _frpc_admin_status()
    if data is None:
        return ("[yellow]● active — admin API not yet reachable "
                "(connecting…)[/yellow]")

    lines = ["[green]● active[/green]", ""]
    proxies = data.get('tcp', [])
    for p in proxies:
        name = p.get('name', '?')
        status = p.get('status', '?')
        remote = p.get('remote_addr', '')
        local = p.get('local_addr', '')
        label = 'Web' if name.endswith('-WEB') else 'SSH'
        if status == 'running':
            icon = '[green]✓[/green]'
        else:
            err = p.get('err', '')
            icon = f'[red]✗[/red] {err}' if err else '[red]✗[/red]'
        lines.append(f"  {icon}  {label:4s}  {local}  →  {remote}   ({name})")
    if not proxies:
        lines.append("  [dim](no proxies)[/dim]")
    return "\n".join(lines)


class RacScreen(Vertical):
    """RAC activation + live tunnel status panel."""

    DEFAULT_CSS = """
    RacScreen {
        padding: 1;
    }
    RacScreen .section-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    RacScreen .field-row {
        height: 3;
        margin-bottom: 1;
    }
    RacScreen .field-label {
        width: 18;
        padding-top: 1;
    }
    RacScreen .button-row {
        height: 3;
        margin-top: 1;
    }
    RacScreen #rac-apply {
        width: auto;
        margin-right: 2;
    }
    RacScreen #rac-disable {
        width: auto;
    }
    RacScreen #rac-result {
        margin-top: 1;
        height: 3;
    }
    RacScreen #rac-status {
        margin-top: 1;
    }
    """

    def __init__(self, topology, **kwargs) -> None:
        super().__init__(**kwargs)
        self._topology = topology
        # Pre-fill from the live config (authoritative), else the
        # install-rendered template (carries the proxy name).
        vals = {'user': '', 'token': '', 'ssh_port': 0, 'web_port': 0,
                'proxy': ''}
        for src in (Path(FRPC_CONFIG), Path(FRPC_TEMPLATE)):
            text = _read_text_sudo(src) if src.exists() else ''
            if text:
                vals = read_frpc_values(text)
                break
        self._user = '' if placeholder(vals['user']) else vals['user']
        self._token = '' if placeholder(vals['token']) else vals['token']
        self._ssh_port = str(vals['ssh_port'] or '')
        self._web_port = str(vals['web_port'] or '')
        self._proxy = vals['proxy']

    # ------------------------------------------------------------------
    def compose(self):
        yield Static("RAC — Remote Access Channel (sigmond-rac)",
                     classes="section-title")
        yield Static(
            "frpc reverse tunnel to gw2.wsprdaemon.org for remote SSH/web "
            "support.\nEnter this station's assignment from the WD admin "
            "(user, token, unique remotePort(s))."
            + (f"\nProxy: {self._proxy}" if self._proxy else ""),
            id="rac-intro",
        )

        yield Static("Activation", classes="section-title")
        with Horizontal(classes="field-row"):
            yield Label("RAC user", classes="field-label")
            yield Input(self._user, placeholder="from WD admin",
                        id="rac-user-input")
        with Horizontal(classes="field-row"):
            yield Label("RAC token", classes="field-label")
            yield Input(self._token, placeholder="from WD admin",
                        password=True, id="rac-token-input")
        with Horizontal(classes="field-row"):
            yield Label("SSH remotePort", classes="field-label")
            yield Input(self._ssh_port,
                        placeholder="unique per station, e.g. 35802",
                        id="rac-ssh-port-input")
        with Horizontal(classes="field-row"):
            yield Label("Web remotePort", classes="field-label")
            yield Input(self._web_port,
                        placeholder="optional — blank if not assigned",
                        id="rac-web-port-input")

        yield Static("", id="rac-result")
        with Horizontal(classes="button-row"):
            yield Button("Activate", id="rac-apply", variant="success")
            yield Button("Disable", id="rac-disable", variant="error")

        yield Static("Tunnel Status", classes="section-title")
        yield Static("", id="rac-status")

    def on_mount(self) -> None:
        self._refresh_status()
        self.set_interval(2, self._refresh_status)

    # ------------------------------------------------------------------
    def _refresh_status(self) -> None:
        try:
            self.query_one('#rac-status', Static).update(_render_status())
        except Exception:
            pass

    # ------------------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'rac-apply':
            self._do_apply()
        elif event.button.id == 'rac-disable':
            self._do_disable()

    def _do_apply(self) -> None:
        """Validate the assignment, render frpc.toml from the template,
        and enable the service.  Installs sigmond-rac first when the
        template isn't on disk yet."""
        result = self.query_one('#rac-result', Static)
        user = self.query_one('#rac-user-input', Input).value.strip()
        token = self.query_one('#rac-token-input', Input).value.strip()
        ssh_s = self.query_one('#rac-ssh-port-input', Input).value.strip()
        web_s = self.query_one('#rac-web-port-input', Input).value.strip()

        if not user or placeholder(user):
            result.update("[red]RAC user is required.[/red]")
            return
        if not token or placeholder(token):
            result.update("[red]RAC token is required.[/red]")
            return
        try:
            ssh_port = int(ssh_s)
            if not (1024 <= ssh_port <= 65535):
                raise ValueError
        except ValueError:
            result.update("[red]SSH remotePort must be a port number "
                          "(1024-65535).[/red]")
            return
        web_port = 0
        if web_s:
            try:
                web_port = int(web_s)
                if not (1024 <= web_port <= 65535):
                    raise ValueError
            except ValueError:
                result.update("[red]Web remotePort must be blank or a "
                              "port number (1024-65535).[/red]")
                return

        def _on_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            result.update("[dim]Activating…[/dim]")

            # 1. Ensure sigmond-rac is installed (renders the template).
            if not Path(FRPC_TEMPLATE).exists():
                r = suspend_and_run_sudo(
                    self.app,
                    [_smd_binary(), 'install', 'sigmond-rac', '--yes'])
                if r.returncode != 0 or not Path(FRPC_TEMPLATE).exists():
                    result.update("[red]✘ sigmond-rac install failed — "
                                  "no template to activate.[/red]")
                    return

            # 2. Fill the assignment into the template -> frpc.toml.
            template = _read_text_sudo(Path(FRPC_TEMPLATE))
            if not template:
                result.update(f"[red]✘ cannot read {FRPC_TEMPLATE}[/red]")
                return
            rendered = render_frpc_config(template, user=user, token=token,
                                          ssh_port=ssh_port,
                                          web_port=web_port)
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(
                        'w', suffix='.toml', delete=False) as tf:
                    tf.write(rendered)
                    tmp = tf.name
                subprocess.run(
                    ['sudo', 'install', '-m', '600', '-o', 'root',
                     '-g', 'root', tmp, FRPC_CONFIG],
                    check=True, stdin=subprocess.DEVNULL)
            except Exception as exc:                       # noqa: BLE001
                result.update(f"[red]✘ could not write {FRPC_CONFIG}: "
                              f"{exc}[/red]")
                return
            finally:
                if tmp:
                    Path(tmp).unlink(missing_ok=True)

            # 3. Enable + (re)start the tunnel.
            r = subprocess.run(
                ['sudo', 'systemctl', 'enable', '--now', 'wd-rac'],
                capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                subprocess.run(['sudo', 'systemctl', 'restart', 'wd-rac'],
                               capture_output=True,
                               stdin=subprocess.DEVNULL)
                result.update(f"[green]✓ activated: user {user}, "
                              f"SSH port {ssh_port}"
                              + (f", web port {web_port}" if web_port
                                 else "") + "[/]")
            else:
                result.update(f"[red]✘ systemctl enable exited "
                              f"{r.returncode}[/]")
            self._refresh_status()

        self.app.push_screen(
            ConfirmModal(
                title="Activate RAC tunnel?",
                body=(f"Will write /etc/sigmond/frpc.toml (user={user!r}, "
                      f"SSH remotePort={ssh_port}"
                      + (f", web remotePort={web_port}" if web_port
                         else ", no web proxy")
                      + ") from the installed template, then enable + "
                        "start wd-rac.service."),
                cmd_preview=f"install -m 600 <rendered> {FRPC_CONFIG} && "
                            "systemctl enable --now wd-rac",
            ),
            _on_confirm,
        )

    def _do_disable(self) -> None:
        result = self.query_one('#rac-result', Static)

        def _on_complete(r: subprocess.CompletedProcess) -> None:
            if r.returncode == 0:
                result.update("[yellow]○ wd-rac disabled and stopped. "
                              "frpc.toml kept — Activate re-enables.[/]")
            else:
                result.update(f"[red]✘ systemctl disable exited "
                              f"{r.returncode}[/]")
            self._refresh_status()

        confirm_and_run(
            self.app,
            title="Disable RAC?",
            body=("Will stop wd-rac.service and disable it from "
                  "auto-start.\nRemote SSH/Web access via the tunnel will "
                  "no longer be available.\n"
                  "/etc/sigmond/frpc.toml is kept for re-activation."),
            cmd=['systemctl', 'disable', '--now', 'wd-rac'],
            sudo=True,
            on_complete=_on_complete,
        )
