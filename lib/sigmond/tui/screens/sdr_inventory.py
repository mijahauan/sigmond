"""SDR Inventory screen — unified view of all SDR receivers visible to this host.

Three source types:
  usb_sdr     Local USB SDRs (RX-888, RTL-SDR, etc.) detected via lsusb
  kiwisdr     KiwiSDRs on the LAN found by port-8073 scan + /status probe
  ka9q_fe     Frontends being served by ka9q-radio instances (local or remote)

Each row has an operator-assignable label stored in
/var/lib/sigmond/sdr-labels.toml.  Labels are used by configuration
screens (wsprdaemon-client, psk-recorder, etc.) to refer to devices by
name rather than IP/bus address.
"""

from __future__ import annotations

import concurrent.futures
import json
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static
from textual.worker import Worker, WorkerState

from ...sdr_labels import SdrDeviceMeta, get_device, load_devices, set_device


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SdrEntry:
    key: str              # label-store key: usb:vid:pid:n | kiwisdr:ip:port | ka9q_fe:host:name
    source: str           # "usb_sdr" | "kiwisdr" | "ka9q_fe"
    sdr_type: str         # "RX-888" | "RTL-SDR" | "KiwiSDR" | "ka9q frontend" | ...
    location: str         # bus/dev string, IP:port, or host
    detail: str           # chip, version, frontend name, etc.
    status: str           # "ok" | "no response" | error string
    users: str = ""       # KiwiSDR users/max
    gps: str = ""         # GPS status for KiwiSDR
    # metadata from label store
    label: str = ""
    call:  str = ""
    grid:  str = ""


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _scan_usb() -> list[SdrEntry]:
    from ...discovery.usb_sdr import KNOWN_SDR_DEVICES, _parse_lsusb
    try:
        result = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=10)
        devices = _parse_lsusb(result.stdout)
    except Exception as e:
        return [SdrEntry(
            key="usb:error", source="usb_sdr",
            sdr_type="USB", location="local",
            detail="", status=f"lsusb failed: {e}",
        )]

    entries = []
    idx_by_key: dict[tuple, int] = {}
    for dev in devices:
        k = (dev["vid"], dev["pid"])
        sdr_type, chip = KNOWN_SDR_DEVICES[k]
        n = idx_by_key.get(k, 0)
        idx_by_key[k] = n + 1
        label_key = f"usb:{dev['vid']}:{dev['pid']}:{n}"
        entries.append(SdrEntry(
            key=label_key,
            source="usb_sdr",
            sdr_type=sdr_type,
            location=f"bus {dev['bus']} dev {dev['device']}",
            detail=f"{chip}  {dev.get('name', '')}".strip(),
            status="ok",
        ))

    if not entries:
        entries.append(SdrEntry(
            key="usb:none", source="usb_sdr",
            sdr_type="—", location="local",
            detail="no SDR USB devices found", status="none",
        ))
    return entries


def _check_port(host: str, port: int, timeout: float) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        ok = s.connect_ex((host, port)) == 0
        s.close()
        return ok
    except Exception:
        return False


def _get_local_subnets() -> list[str]:
    subnets: list[str] = []
    try:
        r = subprocess.run(['ip', 'route', 'show'],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if not parts or '/' not in parts[0]:
                continue
            net, plen = parts[0].split('/', 1)
            try:
                if int(plen) < 8:
                    continue
            except ValueError:
                continue
            octets = net.split('.')
            if len(octets) != 4:
                continue
            if net.startswith('127.') or net.startswith('169.254.'):
                continue
            prefix = '.'.join(octets[:3])
            if prefix not in subnets:
                subnets.append(prefix)
    except Exception:
        pass
    if not subnets:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if not ip.startswith('127.'):
                parts = ip.split('.')
                if len(parts) == 4:
                    subnets.append('.'.join(parts[:3]))
        except Exception:
            pass
    return subnets


def _fetch(url: str, timeout: float):
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        body = resp.read()
        return body.decode('utf-8', errors='replace') if isinstance(body, bytes) else body
    except Exception as e:
        return e


def _probe_kiwi(host: str, port: int) -> SdrEntry:
    key = f"kiwisdr:{host}:{port}"
    base = f"http://{host}:{port}"
    body = _fetch(f"{base}/status", timeout=4.0)
    if isinstance(body, Exception):
        return SdrEntry(key=key, source="kiwisdr", sdr_type="KiwiSDR",
                        location=f"{host}:{port}", detail="",
                        status=f"error: {body}")

    fields: dict = {}
    for line in body.splitlines():
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        fields[k.strip()] = v.strip()

    name    = fields.get('name', '')
    version = fields.get('sw_version', '')
    users   = fields.get('users', '')
    umax    = fields.get('users_max', '')
    fixes   = fields.get('fixes', '')

    gps_body = _fetch(f"{base}/gps", timeout=3.0)
    gps_fix = None
    if not isinstance(gps_body, Exception):
        try:
            gdata = json.loads(gps_body.strip())
            has_fix = gdata.get('fix') or gdata.get('has_fix')
            if has_fix is not None:
                gps_fix = bool(has_fix)
            elif isinstance(gdata.get('fixes'), int):
                gps_fix = int(gdata['fixes']) > 0
        except Exception:
            pass

    if fixes:
        try:
            gps_fix = int(fixes) > 0
        except ValueError:
            pass

    gps_str = ""
    if gps_fix is True:
        gps_str = f"[green]✔[/] {fixes} fixes" if fixes else "[green]✔ fix[/]"
    elif gps_fix is False:
        gps_str = "[yellow]no fix[/]"

    users_str = f"{users}/{umax}" if users and umax else (users or "")

    return SdrEntry(
        key=key,
        source="kiwisdr",
        sdr_type="KiwiSDR",
        location=f"{host}:{port}",
        detail=f"{name}  v{version}".strip(" v") if name or version else "",
        status="ok",
        users=users_str,
        gps=gps_str,
    )


def _scan_kiwis() -> list[SdrEntry]:
    subnets = _get_local_subnets()
    if not subnets:
        return []
    candidates = [f"{s}.{i}" for s in subnets for i in range(1, 255)]
    open_hosts: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        fmap = {ex.submit(_check_port, h, 8073, 0.3): h for h in candidates}
        for fut in concurrent.futures.as_completed(fmap):
            try:
                if fut.result():
                    open_hosts.append(fmap[fut])
            except Exception:
                pass
    if not open_hosts:
        return []
    results: list[SdrEntry] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        fmap2 = {ex.submit(_probe_kiwi, h, 8073): h for h in open_hosts}
        for fut in concurrent.futures.as_completed(fmap2):
            try:
                results.append(fut.result())
            except Exception as e:
                h = fmap2[fut]
                results.append(SdrEntry(
                    key=f"kiwisdr:{h}:8073", source="kiwisdr",
                    sdr_type="KiwiSDR", location=f"{h}:8073",
                    detail="", status=f"error: {e}"))
    return sorted(results, key=lambda x: x.location)


def _scan_ka9q_frontends() -> list[SdrEntry]:
    """Query local ka9q-radio for its frontend list via ka9q-python."""
    entries: list[SdrEntry] = []
    try:
        from ka9q import RadiodControl, discover_channels  # type: ignore
    except ImportError:
        return entries

    # Find radiod config files to get status DNS names.
    import glob
    status_names: list[str] = []
    for conf_file in glob.glob('/etc/radio/radiod@*.conf'):
        try:
            content = open(conf_file).read()
            for line in content.splitlines():
                line = line.strip()
                if line.lower().startswith('status') and '=' in line:
                    _, val = line.split('=', 1)
                    val = val.strip().strip('"').strip("'")
                    if val:
                        status_names.append(val)
                        break
        except Exception:
            pass

    for status_dns in status_names:
        try:
            with RadiodControl(status_dns) as ctrl:
                fe = ctrl.get_frontend_info() if hasattr(ctrl, 'get_frontend_info') else None
                if fe:
                    name = getattr(fe, 'name', '') or status_dns
                    desc = getattr(fe, 'description', '') or ''
                    key = f"ka9q_fe:{status_dns}:{name}"
                    entries.append(SdrEntry(
                        key=key, source="ka9q_fe",
                        sdr_type="ka9q frontend",
                        location=status_dns,
                        detail=desc,
                        status="ok",
                    ))
        except Exception:
            pass

    return entries


def _gather_all() -> list[SdrEntry]:
    usb     = _scan_usb()
    kiwis   = _scan_kiwis()
    ka9q_fe = _scan_ka9q_frontends()
    all_entries = usb + kiwis + ka9q_fe
    devices = load_devices()
    for e in all_entries:
        meta = devices.get(e.key)
        if meta:
            e.label = meta.label
            e.call  = meta.call
            e.grid  = meta.grid
    return all_entries


# ---------------------------------------------------------------------------
# Device metadata modal (label + callsign + grid)
# ---------------------------------------------------------------------------

class DeviceMetaModal(ModalScreen[Optional[SdrDeviceMeta]]):
    """Edit label, WSPR callsign, and Maidenhead grid for an SDR device."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    DeviceMetaModal { align: center middle; }
    DeviceMetaModal > Vertical {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }
    DeviceMetaModal .dm-key   { color: $text-muted; margin-bottom: 1; }
    DeviceMetaModal .dm-field { margin-bottom: 1; }
    DeviceMetaModal Label     { margin-bottom: 0; }
    DeviceMetaModal Input     { margin-bottom: 1; }
    DeviceMetaModal Horizontal { height: auto; align: right middle; margin-top: 1; }
    DeviceMetaModal Button    { margin-left: 1; }
    """

    def __init__(self, meta: SdrDeviceMeta, **kwargs) -> None:
        super().__init__(**kwargs)
        self._meta = meta

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[dim]{self._meta.key}[/]", classes="dm-key")
            yield Label("Name / description")
            yield Input(value=self._meta.label, placeholder="e.g. RX-888 Omni",
                        id="dm-label")
            yield Label("WSPR reporter callsign")
            yield Input(value=self._meta.call, placeholder="e.g. AI6VN-0",
                        id="dm-call")
            yield Label("Maidenhead grid square")
            yield Input(value=self._meta.grid, placeholder="e.g. CM88mc",
                        id="dm-grid")
            with Horizontal():
                yield Button("Cancel", id="dm-cancel", variant="default")
                yield Button("Clear",  id="dm-clear",  variant="warning")
                yield Button("Save",   id="dm-save",   variant="success")

    def on_mount(self) -> None:
        self.query_one("#dm-label", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dm-save":
            self.dismiss(SdrDeviceMeta(
                key=self._meta.key,
                label=self.query_one("#dm-label", Input).value.strip(),
                call =self.query_one("#dm-call",  Input).value.strip().upper(),
                grid =self.query_one("#dm-grid",  Input).value.strip(),
            ))
        elif event.button.id == "dm-clear":
            self.dismiss(SdrDeviceMeta(key=self._meta.key))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------

class SdrInventoryScreen(Vertical):
    """Unified SDR receiver inventory — USB, KiwiSDR LAN, ka9q-radio frontends."""

    BINDINGS = [
        Binding("r", "rescan",    "Rescan"),
        Binding("e", "edit_label","Label"),
    ]

    DEFAULT_CSS = """
    SdrInventoryScreen { padding: 1; }
    SdrInventoryScreen .sdr-title { text-style: bold; margin-bottom: 1; }
    SdrInventoryScreen #sdr-status { margin-bottom: 1; }
    SdrInventoryScreen #sdr-btn-row { height: 3; margin-top: 1; }
    SdrInventoryScreen #sdr-btn-row Button { margin-right: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entries: list[SdrEntry] = []

    def compose(self) -> ComposeResult:
        yield Static("SDR Inventory — USB, KiwiSDR LAN, ka9q-radio", classes="sdr-title")
        yield Static("[dim]scanning…[/]", id="sdr-status")

        table = DataTable(id="sdr-table", zebra_stripes=True, cursor_type="row")
        table.add_columns("Source", "Type", "Location", "Detail", "Users", "GPS", "Name", "Call", "Grid")
        yield table

        with Horizontal(id="sdr-btn-row"):
            yield Button("↺ Rescan",   id="sdr-rescan", variant="default")
            yield Button("✎ Edit",     id="sdr-label",  variant="primary")

    def on_mount(self) -> None:
        self._rescan()

    def action_rescan(self) -> None:
        self._rescan()

    def action_edit_label(self) -> None:
        self._open_label_modal()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sdr-rescan":
            self._rescan()
        elif event.button.id == "sdr-label":
            self._open_label_modal()

    def _rescan(self) -> None:
        self.query_one("#sdr-status", Static).update(
            "[dim]scanning USB bus, LAN port 8073, ka9q-radio…[/]")
        self.query_one("#sdr-table", DataTable).clear()
        self.run_worker(_gather_all, thread=True, name="sdr-gather")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "sdr-gather":
            return
        if event.state == WorkerState.ERROR:
            self.query_one("#sdr-status", Static).update(
                f"[red]scan error: {event.worker.error}[/]")
            return
        if event.state != WorkerState.SUCCESS:
            return
        self._entries = event.worker.result or []
        self._render_entries()

    def _render_entries(self) -> None:
        table = self.query_one("#sdr-table", DataTable)
        table.clear()

        usb_ok  = sum(1 for e in self._entries if e.source == "usb_sdr" and e.status == "ok")
        kiwi_ok = sum(1 for e in self._entries if e.source == "kiwisdr" and e.status == "ok")
        ka9q_ok = sum(1 for e in self._entries if e.source == "ka9q_fe"  and e.status == "ok")
        self.query_one("#sdr-status", Static).update(
            f"USB: [bold]{usb_ok}[/]  ·  "
            f"KiwiSDR: [bold]{kiwi_ok}[/]  ·  "
            f"ka9q frontends: [bold]{ka9q_ok}[/]  "
            f"[dim]— press e to label selected row[/]"
        )

        src_labels = {
            "usb_sdr": "[cyan]USB[/]",
            "kiwisdr": "[blue]KiwiSDR[/]",
            "ka9q_fe": "[magenta]ka9q[/]",
        }
        for e in self._entries:
            src_cell = src_labels.get(e.source, e.source)
            type_cell = e.sdr_type
            if e.status not in ("ok", "none"):
                type_cell = f"[red]{e.sdr_type}[/]"
            name_cell = f"[green]{e.label}[/]" if e.label else "[dim]—[/]"
            call_cell = f"[cyan]{e.call}[/]"   if e.call  else "[dim]—[/]"
            grid_cell = e.grid if e.grid else "[dim]—[/]"
            table.add_row(
                src_cell, type_cell, e.location,
                e.detail[:35] if e.detail else "[dim]—[/]",
                e.users or "[dim]—[/]",
                e.gps   or "[dim]—[/]",
                name_cell, call_cell, grid_cell,
                key=e.key,
            )

    def _open_label_modal(self) -> None:
        table = self.query_one("#sdr-table", DataTable)
        idx = table.cursor_row
        if idx < 0 or idx >= len(self._entries):
            return
        entry = self._entries[idx]
        current_meta = SdrDeviceMeta(
            key=entry.key, label=entry.label,
            call=entry.call, grid=entry.grid,
        )

        def _after(new_meta: Optional[SdrDeviceMeta]) -> None:
            if new_meta is None:
                return
            set_device(new_meta)
            entry.label = new_meta.label
            entry.call  = new_meta.call
            entry.grid  = new_meta.grid
            self._render_entries()

        self.app.push_screen(DeviceMetaModal(meta=current_meta), _after)
