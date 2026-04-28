"""WD-RAC configuration and status screen."""

from __future__ import annotations

import asyncio
import configparser
import json
import subprocess
import urllib.request
from pathlib import Path

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Static


_FRPC_CONFIG = Path('/etc/sigmond/frpc.toml')
_FRPC_INI    = Path('/etc/sigmond/frpc.ini')   # legacy fallback for pre-fill only
_PORT_BASE   = 35800
_WD_CONF     = Path('/etc/wsprdaemon/wsprdaemon.conf')
_ADMIN_URL   = 'http://127.0.0.1:7500'


def _read_frpc_toml() -> dict:
    """Read /etc/sigmond/frpc.toml, falling back to sudo when permission-denied."""
    import tomllib
    try:
        with open(_FRPC_CONFIG, 'rb') as f:
            return tomllib.load(f)
    except PermissionError:
        pass
    try:
        r = subprocess.run(
            ['sudo', 'cat', str(_FRPC_CONFIG)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            return tomllib.loads(r.stdout)
    except Exception:
        pass
    return {}


def _detect_rac_defaults_tui(current_id: str, current_num: str) -> tuple[str, str]:
    """Try to fill in rac_id / rac_number from the wsprdaemon v4 config."""
    if not _WD_CONF.exists():
        return current_id, current_num
    try:
        cfg = configparser.ConfigParser(
            comment_prefixes=(';', '#'),
            inline_comment_prefixes=(';', '#'),
            strict=False,
            interpolation=None,
        )
        cfg.read(_WD_CONF)
        new_id  = current_id
        new_num = current_num
        if not new_id:
            for section in cfg.sections():
                parts = section.split(':')
                if parts[0] == 'receiver' and len(parts) == 2:
                    call = cfg.get(section, 'call', fallback='').strip()
                    if call:
                        new_id = call
                        break
        if new_num in ('', '-1'):
            rac_val = (cfg.get('general', 'rac', fallback='').strip()
                       if cfg.has_section('general') else '')
            if rac_val:
                try:
                    int(rac_val)
                    new_num = rac_val
                except ValueError:
                    pass
        return new_id, new_num
    except Exception:
        return current_id, current_num


def _frpc_admin_status() -> dict | None:
    """Query the frpc client admin API.  Returns parsed JSON or None."""
    try:
        with urllib.request.urlopen(f'{_ADMIN_URL}/api/status', timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _service_active() -> bool:
    r = subprocess.run(
        ['systemctl', 'is-active', 'wd-rac'],
        capture_output=True, text=True,
    )
    return r.stdout.strip() == 'active'


def _service_enabled() -> bool:
    r = subprocess.run(
        ['systemctl', 'is-enabled', 'wd-rac'],
        capture_output=True, text=True,
    )
    return r.stdout.strip() == 'enabled'


def _render_status() -> str:
    """Build a Rich-markup status string from live service + admin API data."""
    active = _service_active()
    if not active:
        if not _FRPC_CONFIG.exists() and not _FRPC_INI.exists():
            return "[dim]Not configured — fill in the fields above and press Apply.[/dim]"
        if not _service_enabled():
            return "[dim]○ wd-rac disabled (press Apply & enable to re-activate)[/dim]"
        return "[red]● wd-rac.service stopped[/red]"

    data = _frpc_admin_status()
    if data is None:
        return "[yellow]● active — admin API not yet reachable (connecting…)[/yellow]"

    lines = ["[green]● active[/green]", ""]
    proxies = data.get('tcp', [])
    for p in proxies:
        name   = p.get('name', '?')
        status = p.get('status', '?')
        remote = p.get('remote_addr', '')
        local  = p.get('local_addr', '')
        label  = 'Web' if name.endswith('-WEB') else 'SSH'
        if status == 'running':
            icon = '[green]✓[/green]'
        else:
            err  = p.get('err', '')
            icon = f'[red]✗[/red] {err}' if err else '[red]✗[/red]'
        lines.append(f"  {icon}  {label:4s}  {local}  →  {remote}   ({name})")

    if not proxies:
        lines.append("  [dim](no proxies)[/dim]")
    return "\n".join(lines)


class RacScreen(Vertical):
    """RAC configuration + live tunnel status panel."""

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
        width: 16;
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
        self._rac_id     = ''
        self._rac_number = ''

        # frpc.toml is the authoritative source — it reflects what is actually
        # running.  Read it first (with sudo fallback for permission-denied).
        if _FRPC_CONFIG.exists():
            cfg = _read_frpc_toml()
            for proxy in cfg.get('proxies', []):
                if not proxy.get('name', '').endswith('-WEB'):
                    self._rac_id = proxy.get('name', '')
                    rp = proxy.get('remotePort', -1)
                    if rp >= _PORT_BASE:
                        self._rac_number = str(rp - _PORT_BASE)
                    break

        # Legacy frpc.ini fallback
        if not self._rac_id and _FRPC_INI.exists():
            try:
                cfg = configparser.ConfigParser()
                cfg.read(_FRPC_INI)
                sections = [s for s in cfg.sections()
                            if s != 'common' and not s.endswith('-WEB')]
                if sections:
                    self._rac_id = sections[0]
                    rp = cfg.getint(sections[0], 'remote_port', fallback=-1)
                    if rp >= _PORT_BASE:
                        self._rac_number = str(rp - _PORT_BASE)
            except Exception:
                pass

        # Last resort: wsprdaemon.conf auto-detection
        if not self._rac_id or self._rac_number in ('', '-1'):
            self._rac_id, self._rac_number = _detect_rac_defaults_tui(
                self._rac_id, self._rac_number
            )

    # ------------------------------------------------------------------
    def compose(self):
        yield Static("WD-RAC — Remote Access Channel", classes="section-title")
        yield Static(
            "frpc reverse tunnel for remote SSH and web access.\n"
            "Enter the two values provided by the RAC administrator.",
            id="rac-intro",
        )

        yield Static("Configuration", classes="section-title")
        with Horizontal(classes="field-row"):
            yield Label("RAC ID", classes="field-label")
            yield Input(self._rac_id, placeholder="e.g. AC0G-KA9Q",
                        id="rac-id-input")

        with Horizontal(classes="field-row"):
            yield Label("RAC number", classes="field-label")
            yield Input(self._rac_number, placeholder="integer from admin email",
                        id="rac-number-input")

        yield Static("", id="rac-result")
        with Horizontal(classes="button-row"):
            yield Button("Apply & enable", id="rac-apply", variant="success")
            yield Button("Disable", id="rac-disable", variant="error")

        yield Static("Tunnel Status", classes="section-title")
        yield Static("", id="rac-status")

    def on_mount(self) -> None:
        # Initial status read + start 2-second live poll.
        self._refresh_status()
        self.set_interval(2, self._refresh_status)

    # ------------------------------------------------------------------
    def _refresh_status(self) -> None:
        """Sync status render (fast local calls — no worker needed)."""
        try:
            self.query_one('#rac-status', Static).update(_render_status())
        except Exception:
            pass

    # ------------------------------------------------------------------
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'rac-apply':
            await self._do_apply()
        elif event.button.id == 'rac-disable':
            self._do_disable()

    async def _do_apply(self) -> None:
        rac_id         = self.query_one('#rac-id-input', Input).value.strip()
        rac_number_str = self.query_one('#rac-number-input', Input).value.strip()
        result_widget  = self.query_one('#rac-result', Static)

        if not rac_id:
            result_widget.update("[red]RAC ID is required.[/red]")
            return
        try:
            rac_number = int(rac_number_str)
            if rac_number < 0:
                raise ValueError
        except ValueError:
            result_widget.update("[red]RAC number must be a non-negative integer.[/red]")
            return

        try:
            self._write_rac_to_topology(rac_id, rac_number)
        except Exception as exc:
            result_widget.update(f"[red]Failed to update topology.toml: {exc}[/red]")
            return

        result_widget.update("[dim]Applying…[/dim]")
        smd_bin = str(Path(__file__).resolve().parents[4] / 'bin' / 'smd')
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, lambda: subprocess.run(
            ['sudo', 'python3', smd_bin, 'install', '--components', 'wd-rac', '--yes'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        ))
        if r.returncode == 0:
            result_widget.update(
                f"[green]✓ configured: {rac_id}, channel {rac_number}[/green]"
            )
        else:
            result_widget.update(
                f"[red]Install failed:\n{(r.stderr or r.stdout)[-400:]}[/red]"
            )
        # Refresh status immediately — service may have just restarted.
        self._refresh_status()

    def _do_disable(self) -> None:
        result_widget = self.query_one('#rac-result', Static)
        result_widget.update("[dim]Disabling…[/dim]")
        r = subprocess.run(
            ['sudo', 'systemctl', 'disable', '--now', 'wd-rac'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            result_widget.update("[yellow]○ wd-rac disabled and stopped.[/yellow]")
        else:
            result_widget.update(
                f"[red]Disable failed:\n{(r.stderr or r.stdout)[-400:]}[/red]"
            )
        try:
            self._set_topology_enabled(False)
        except Exception:
            pass
        self._refresh_status()

    def _set_topology_enabled(self, enabled: bool) -> None:
        """Flip the enabled flag in topology.toml for wd-rac."""
        import tomllib, tempfile
        from ...paths import TOPOLOGY_PATH

        raw: dict = {}
        if TOPOLOGY_PATH.exists():
            with open(TOPOLOGY_PATH, 'rb') as f:
                raw = tomllib.load(f)

        raw.setdefault('component', {}).setdefault('wd-rac', {})['enabled'] = enabled

        lines = [
            "# /etc/sigmond/topology.toml",
            "# Managed by smd tui. Manual edits are fine too.",
            "",
        ]
        for comp_name in sorted(raw.get('component', {})):
            cfg = raw['component'][comp_name]
            lines.append(f"[component.{comp_name}]")
            lines.append(f'enabled = {"true" if cfg.get("enabled", False) else "false"}')
            if not cfg.get('managed', True):
                lines.append("managed = false")
            if cfg.get('description'):
                lines.append(f'description = "{cfg["description"]}"')
            if comp_name == 'wd-rac':
                if cfg.get('rac_id'):
                    lines.append(f'rac_id = "{cfg["rac_id"]}"')
                if cfg.get('rac_number') is not None:
                    lines.append(f'rac_number = {cfg["rac_number"]}')
            lines.append("")

        content = "\n".join(lines) + "\n"
        with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as tf:
            tf.write(content)
            tf_path = tf.name
        try:
            subprocess.run(
                ['sudo', 'install', '-m', '644', tf_path, str(TOPOLOGY_PATH)],
                check=True, stdin=subprocess.DEVNULL,
            )
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def _write_rac_to_topology(self, rac_id: str, rac_number: int) -> None:
        """Persist rac_id and rac_number into the wd-rac topology component."""
        import tomllib, tempfile
        from ...paths import TOPOLOGY_PATH

        raw: dict = {}
        if TOPOLOGY_PATH.exists():
            with open(TOPOLOGY_PATH, 'rb') as f:
                raw = tomllib.load(f)

        rac_comp = raw.setdefault('component', {}).setdefault('wd-rac', {})
        rac_comp['enabled']    = True
        rac_comp['managed']    = False
        rac_comp['rac_id']     = rac_id
        rac_comp['rac_number'] = rac_number

        lines = [
            "# /etc/sigmond/topology.toml",
            "# Managed by smd tui. Manual edits are fine too.",
            "",
        ]
        for comp_name in sorted(raw.get('component', {})):
            cfg = raw['component'][comp_name]
            lines.append(f"[component.{comp_name}]")
            lines.append(f'enabled = {"true" if cfg.get("enabled", False) else "false"}')
            if not cfg.get('managed', True):
                lines.append("managed = false")
            if cfg.get('description'):
                lines.append(f'description = "{cfg["description"]}"')
            if comp_name == 'wd-rac':
                lines.append(f'rac_id = "{cfg["rac_id"]}"')
                lines.append(f'rac_number = {cfg["rac_number"]}')
            lines.append("")

        content = "\n".join(lines) + "\n"

        with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as tf:
            tf.write(content)
            tf_path = tf.name
        try:
            subprocess.run(
                ['sudo', 'install', '-m', '644', tf_path, str(TOPOLOGY_PATH)],
                check=True, stdin=subprocess.DEVNULL,
            )
        finally:
            Path(tf_path).unlink(missing_ok=True)
