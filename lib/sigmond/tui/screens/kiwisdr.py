"""KiwiSDR Live screen — discovers KiwiSDRs on the local LAN by scanning
port 8073, then fetches /status and /gps from each host that responds."""

from __future__ import annotations

import concurrent.futures
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class KiwiInfo:
    host: str
    port: int = 8073
    name: str = ""
    sw_version: str = ""
    users: Optional[int] = None
    users_max: Optional[int] = None
    gps_fixes: Optional[int] = None
    gps_fix: Optional[bool] = None
    uptime: str = ""
    antenna: str = ""
    loc: str = ""
    grid: str = ""
    offline: str = ""
    error: str = ""
    ok: bool = True
    probed_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _get_local_subnets() -> list[str]:
    """Return /24 prefixes to scan, e.g. ['192.168.1', '10.0.0']."""
    subnets: list[str] = []
    try:
        result = subprocess.run(
            ['ip', 'route', 'show'],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if not parts or '/' not in parts[0]:
                continue
            dest = parts[0]
            try:
                net, prefixlen_s = dest.split('/', 1)
                if int(prefixlen_s) < 8:
                    continue
                octets = net.split('.')
                if len(octets) != 4:
                    continue
                if net.startswith('127.') or net.startswith('169.254.'):
                    continue
                prefix = '.'.join(octets[:3])
                if prefix not in subnets:
                    subnets.append(prefix)
            except (ValueError, IndexError):
                continue
    except Exception:
        pass

    if not subnets:
        # fallback: infer from our own IP
        try:
            my_ip = socket.gethostbyname(socket.gethostname())
            if not my_ip.startswith('127.'):
                parts = my_ip.split('.')
                if len(parts) == 4:
                    subnets.append('.'.join(parts[:3]))
        except Exception:
            pass

    return subnets


def _check_port(host: str, port: int, timeout: float) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _fetch(url: str, timeout: float) -> str | Exception:
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        body = resp.read()
        return body.decode('utf-8', errors='replace') if isinstance(body, bytes) else body
    except Exception as e:
        return e


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_status_into(body: str, info: KiwiInfo) -> None:
    for line in body.splitlines():
        if '=' not in line:
            continue
        key, val = line.split('=', 1)
        key, val = key.strip(), val.strip()
        if key == 'name':
            info.name = val
        elif key == 'sw_version':
            info.sw_version = val
        elif key == 'users':
            try:
                info.users = int(val)
            except ValueError:
                pass
        elif key == 'users_max':
            try:
                info.users_max = int(val)
            except ValueError:
                pass
        elif key == 'fixes':
            try:
                info.gps_fixes = int(val)
                info.gps_fix = info.gps_fixes > 0
            except ValueError:
                pass
        elif key == 'uptime':
            info.uptime = val
        elif key == 'antenna':
            info.antenna = val
        elif key == 'loc':
            info.loc = val
        elif key == 'grid':
            info.grid = val
        elif key == 'offline':
            info.offline = val


def _parse_gps_into(body: str, info: KiwiInfo) -> None:
    body = body.strip()
    if not body:
        return
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return
    if 'fixes' in data:
        try:
            info.gps_fixes = int(data['fixes'])
        except (TypeError, ValueError):
            pass
    has_fix = data.get('fix') or data.get('has_fix')
    if has_fix is not None:
        info.gps_fix = bool(has_fix)
    elif isinstance(info.gps_fixes, int):
        info.gps_fix = info.gps_fixes > 0


# ---------------------------------------------------------------------------
# Worker body (runs in a thread)
# ---------------------------------------------------------------------------

def _scan_and_probe() -> list[KiwiInfo]:
    """Scan local /24 subnet(s) for port 8073, then probe each host."""
    subnets = _get_local_subnets()
    if not subnets:
        return []

    candidates = [f"{s}.{i}" for s in subnets for i in range(1, 255)]
    port = 8073

    open_hosts: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        fut_map = {ex.submit(_check_port, h, port, 0.3): h for h in candidates}
        for fut in concurrent.futures.as_completed(fut_map):
            try:
                if fut.result():
                    open_hosts.append(fut_map[fut])
            except Exception:
                pass

    if not open_hosts:
        return []

    results: list[KiwiInfo] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        probe_futs = {ex.submit(_probe_one, h, port): h for h in open_hosts}
        for fut in concurrent.futures.as_completed(probe_futs):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append(KiwiInfo(host=probe_futs[fut], port=port,
                                        ok=False, error=str(e)))

    return sorted(results, key=lambda x: x.host)


def _probe_one(host: str, port: int) -> KiwiInfo:
    info = KiwiInfo(host=host, port=port)
    base = f"http://{host}:{port}"

    body = _fetch(f"{base}/status", timeout=4.0)
    if isinstance(body, Exception):
        info.ok = False
        info.error = str(body)
        return info

    _parse_status_into(body, info)

    gps_body = _fetch(f"{base}/gps", timeout=3.0)
    if not isinstance(gps_body, Exception):
        _parse_gps_into(gps_body, info)

    return info


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------

class KiwiSDRScreen(Vertical):
    """KiwiSDR live status — LAN port-8073 discovery + /status + /gps."""

    DEFAULT_CSS = """
    KiwiSDRScreen {
        padding: 1;
    }
    KiwiSDRScreen .kiwi-title {
        text-style: bold;
        margin-bottom: 1;
    }
    KiwiSDRScreen #kiwi-status {
        margin-bottom: 1;
    }
    KiwiSDRScreen #kiwi-table {
        margin-top: 0;
    }
    KiwiSDRScreen #kiwi-btn-row {
        height: 3;
        margin-top: 1;
    }
    """

    def compose(self):
        yield Static("KiwiSDR Live — LAN port-8073 discovery", classes="kiwi-title")
        yield Static("[dim]scanning…[/]", id="kiwi-status")

        table = DataTable(id="kiwi-table", zebra_stripes=True)
        table.add_columns("Host", "Name", "Version", "Users", "GPS", "Uptime", "Antenna / Location")
        yield table

        with Horizontal(id="kiwi-btn-row"):
            yield Button("Rescan", id="kiwi-rescan", variant="default")

    def on_mount(self) -> None:
        self._scan()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "kiwi-rescan":
            self._scan()

    def _scan(self) -> None:
        self.query_one("#kiwi-status", Static).update(
            "[dim]scanning LAN for port 8073…[/]")
        table = self.query_one("#kiwi-table", DataTable)
        table.clear()
        self.run_worker(_scan_and_probe, thread=True, name="kiwi-scan")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "kiwi-scan":
            return
        if event.state == WorkerState.ERROR:
            self.query_one("#kiwi-status", Static).update(
                f"[red]scan failed: {event.worker.error}[/]")
            return
        if event.state != WorkerState.SUCCESS:
            return
        self._render_results(event.worker.result or [])

    def _render_results(self, results: list[KiwiInfo]) -> None:
        table = self.query_one("#kiwi-table", DataTable)
        table.clear()

        if not results:
            self.query_one("#kiwi-status", Static).update(
                "[yellow]no KiwiSDRs found on port 8073[/]")
            table.add_row("—", "[dim]none found[/]", "", "", "", "", "")
            return

        ok_count = sum(1 for r in results if r.ok)
        self.query_one("#kiwi-status", Static).update(
            f"Found [bold]{len(results)}[/] KiwiSDR(s) · "
            f"{ok_count} responding · "
            f"[dim]last scan: just now[/]"
        )

        for r in results:
            host_cell = f"{r.host}:{r.port}"

            if not r.ok:
                table.add_row(host_cell, "[red]error[/]", "", "",
                              "", "", r.error[:50])
                continue

            name = r.name or "[dim]—[/]"
            ver  = r.sw_version or "[dim]—[/]"

            if r.users is not None and r.users_max is not None:
                users = f"{r.users}/{r.users_max}"
            elif r.users is not None:
                users = str(r.users)
            else:
                users = "[dim]—[/]"

            if r.gps_fix is True:
                gps = f"[green]✔[/] {r.gps_fixes or 0} fixes"
            elif r.gps_fix is False:
                gps = "[yellow]no fix[/]"
            elif r.gps_fixes is not None:
                gps = f"{r.gps_fixes} fixes"
            else:
                gps = "[dim]—[/]"

            location = r.antenna or r.loc or r.grid or "[dim]—[/]"

            table.add_row(host_cell, name, ver, users, gps,
                          r.uptime or "[dim]—[/]", location[:35])
