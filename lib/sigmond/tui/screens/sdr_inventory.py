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
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static
from textual.worker import Worker, WorkerState

import re

from ...sdr_labels import SdrDeviceMeta, get_device, load_devices, set_device

_GRID_RE = re.compile(r'^[A-Ra-r]{2}[0-9]{2}([A-Xa-x]{2})?$')


def _normalize_grid(val: str) -> str:
    """Uppercase field+square, lowercase subsquare (convention: CM88mc not CM88MC)."""
    if len(val) == 6:
        return val[:4].upper() + val[4:].lower()
    return val.upper()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SdrEntry:
    key: str              # label-store key: usb:vid:pid:n | kiwisdr:ip:port | ka9q_fe:host:name
    source: str           # "usb_sdr" | "kiwisdr" | "ka9q_fe"
    sdr_type: str         # "RX-888" | "RX-888 Mk2" | "RTL-SDR" | "KiwiSDR" | ...
    location: str         # bus/dev string, IP:port, or host
    detail: str           # chip, version, frontend name, etc.
    status: str           # "ok" | "no response" | error string
    serial: str = ""      # USB serial number (iSerial from lsusb -v)
    users: str = ""       # KiwiSDR users/max
    gps: str = ""         # GPS status for KiwiSDR
    channels: int = 0     # KiwiSDR rx_chans from /status (0 = unknown)
    # metadata from label store
    label: str = ""
    call:  str = ""
    grid:  str = ""
    ttl:   int = 0        # ka9q-radio TTL (0 = local, 1 = ethernet multicast)


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _get_usb_serial(bus: str, device: str) -> str:
    """Return the USB iSerial string for a device, or empty string."""
    try:
        r = subprocess.run(
            ['lsusb', '-v', '-s', f'{int(bus)}:{int(device)}'],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r'iSerial\s+\d+\s+(\S+)', r.stdout)
        return m.group(1) if m else ""
    except Exception:
        return ""


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
        serial = _get_usb_serial(dev["bus"], dev["device"])
        entries.append(SdrEntry(
            key=label_key,
            source="usb_sdr",
            sdr_type=sdr_type,
            location=f"bus {dev['bus']} dev {dev['device']}",
            detail=f"{chip}  {dev.get('name', '')}".strip(),
            serial=serial,
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
    try:
        rx_chans = int(fields.get('rx_chans', '0') or '0')
    except ValueError:
        rx_chans = 0

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
        channels=rx_chans,
    )


def _scan_kiwis() -> list[SdrEntry]:
    subnets = _get_local_subnets()
    if not subnets:
        return []
    candidates = [f"{s}.{i}" for s in subnets for i in range(1, 255)]
    open_hosts: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        fmap = {ex.submit(_check_port, h, 8073, 0.6): h for h in candidates}
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

    # Inject manually-added remote ka9q SDRs (not discoverable by scan)
    detected_keys = {e.key for e in all_entries}
    for key, meta in sorted(devices.items()):
        if key.startswith("ka9q_remote:") and key not in detected_keys:
            address = key.split(":", 1)[1]
            all_entries.append(SdrEntry(
                key=key, source="ka9q_remote",
                sdr_type="ka9q remote",
                location=address,
                detail="manually added",
                status="ok",
                label=meta.label, call=meta.call,
                grid=meta.grid,   ttl=meta.ttl,
            ))

    changed = False
    for e in all_entries:
        meta = devices.get(e.key)
        if meta:
            e.label = meta.label
            e.call  = meta.call
            e.grid  = meta.grid
            e.ttl   = meta.ttl
            if e.channels > 0 and meta.channels == 0:
                meta.channels = e.channels
                changed = True
            elif e.channels == 0 and meta.channels > 0:
                e.channels = meta.channels
    if changed:
        try:
            from ...sdr_labels import save_devices
            save_devices(devices)
        except Exception:
            pass
    return all_entries


# ---------------------------------------------------------------------------
# Device metadata modal
# ---------------------------------------------------------------------------

class DeviceMetaModal(ModalScreen):
    """Edit name, reporter ID, and Maidenhead grid for an SDR device.

    Dismisses with (SdrDeviceMeta, copy_grid_to_all: bool) or None on cancel.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    DeviceMetaModal { align: center middle; }
    DeviceMetaModal > Vertical {
        width: 66;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }
    DeviceMetaModal #dm-header  { height: auto; margin-bottom: 1; align: left middle; }
    DeviceMetaModal #dm-title   { width: 1fr; color: $text-muted; }
    DeviceMetaModal #dm-x       { width: 7; min-width: 7; }
    DeviceMetaModal #dm-ttl     { margin-bottom: 0; }
    DeviceMetaModal .dm-key    { color: $text-muted; margin-bottom: 1; }
    DeviceMetaModal Label      { margin-bottom: 0; }
    DeviceMetaModal Input      { margin-bottom: 0; }
    DeviceMetaModal .dm-hint   { height: 1; color: $text-muted; margin-bottom: 1; }
    DeviceMetaModal .dm-err    { height: 1; color: $error;      margin-bottom: 1; }
    DeviceMetaModal .dm-ok     { height: 1; color: $success;    margin-bottom: 1; }
    DeviceMetaModal #dm-btns   { height: auto; margin-top: 1; }
    DeviceMetaModal #dm-btns-l { width: auto; }
    DeviceMetaModal #dm-spacer { width: 1fr; }
    DeviceMetaModal #dm-btns-r { width: auto; }
    DeviceMetaModal Button     { margin-right: 1; }
    """

    def __init__(self, meta: SdrDeviceMeta, **kwargs) -> None:
        super().__init__(**kwargs)
        self._meta = meta
        self._is_kiwi    = meta.key.startswith("kiwisdr:")
        self._is_usb_sdr = meta.key.startswith("usb:")
        self._id_touched = bool(meta.call)   # don't auto-fill if call already set

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="dm-header"):
                yield Static(f"[dim]{self._meta.key}[/]", id="dm-title")
                yield Button("[white bold]X[/]", id="dm-x", variant="error")


            yield Label("Configuration name")
            yield Input(value=self._meta.label,
                        placeholder="e.g. Omni  →  omni-hf.status",
                        id="dm-label")
            yield Static("[dim]becomes the radiod status stream: <name>-hf.status[/]",
                         classes="dm-hint", id="dm-label-hint")

            yield Label("Reporter ID")
            yield Input(value=self._meta.call,
                        placeholder="e.g. AI6VN-0  (any string accepted by wsprnet)",
                        id="dm-call")
            yield Static("[dim]auto-filled from name if left blank[/]",
                         classes="dm-hint", id="dm-call-hint")

            yield Label("Maidenhead grid square (4 or 6 characters)")
            yield Input(value=self._meta.grid,
                        placeholder="e.g. CM88mc  (subsquare letters auto-lowercased)",
                        id="dm-grid")
            yield Static("", classes="dm-hint", id="dm-grid-hint")

            if self._is_kiwi:
                yield Label("Max receive channels (from KiwiSDR /status rx_chans)")
                ch_val = str(self._meta.channels) if self._meta.channels else ""
                yield Input(value=ch_val,
                            placeholder="e.g. 4  (leave blank if unknown)",
                            id="dm-channels")
                hint = "[dim]auto-detected from /status rx_chans[/]" if self._meta.channels else ""
                yield Static(hint, classes="dm-hint", id="dm-channels-hint")

            if self._is_usb_sdr:
                yield Label("TTL (ka9q-radio multicast time-to-live)")
                yield Input(value=str(self._meta.ttl),
                            placeholder="0 = local only   1 = send out ethernet",
                            id="dm-ttl")
                yield Static("[dim]0 = multicast stays local;  1 = crosses ethernet switches[/]",
                             classes="dm-hint", id="dm-ttl-hint")

            with Horizontal(id="dm-btns"):
                with Horizontal(id="dm-btns-l"):
                    yield Button("💾 Save",            id="dm-save",     variant="success")
                Static("", id="dm-spacer")
                with Horizontal(id="dm-btns-r"):
                    yield Button("Copy grid → all unset", id="dm-copy-grid", variant="default")
                    yield Button("Clear all",  id="dm-clear",    variant="error")
                    yield Button("Cancel",     id="dm-cancel",   variant="error")

    def on_mount(self) -> None:
        self.query_one("#dm-label", Input).focus()
        self._validate_grid(self._meta.grid)

    # ── reactive input handling ──────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "dm-label":
            # Auto-fill reporter ID from name if user hasn't manually set it
            if not self._id_touched:
                name = event.value.strip()
                self.query_one("#dm-call", Input).value = name
        elif event.input.id == "dm-call":
            self._id_touched = True
            hint = self.query_one("#dm-call-hint", Static)
            if event.value.strip():
                hint.update("")
            else:
                hint.update("[dim]auto-filled from name if left blank[/]")
        elif event.input.id == "dm-grid":
            self._validate_grid(event.value)

    def _validate_grid(self, val: str) -> None:
        hint = self.query_one("#dm-grid-hint", Static)
        if not val.strip():
            hint.update("")
            hint.set_class(False, "dm-err")
            hint.set_class(False, "dm-ok")
            return
        if _GRID_RE.match(val.strip()):
            hint.update("✔ valid grid")
            hint.set_class(False, "dm-err")
            hint.set_class(True,  "dm-ok")
        else:
            hint.update("✗ must be 4 chars (AA00) or 6 chars (AA00aa — subsquare lowercase)")
            hint.set_class(True,  "dm-err")
            hint.set_class(False, "dm-ok")

    # ── buttons ──────────────────────────────────────────────────────────

    def _read_channels(self) -> int:
        if not self._is_kiwi:
            return self._meta.channels
        try:
            v = self.query_one("#dm-channels", Input).value.strip()
            return int(v) if v else 0
        except (ValueError, Exception):
            return 0

    def _read_ttl(self) -> int:
        if not self._is_usb_sdr:
            return self._meta.ttl
        try:
            v = self.query_one("#dm-ttl", Input).value.strip()
            return int(v) if v else 0
        except (ValueError, Exception):
            return 0

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "dm-save":
            grid = _normalize_grid(self.query_one("#dm-grid", Input).value.strip())
            if grid and not _GRID_RE.match(grid):
                self.query_one("#dm-grid-hint", Static).update(
                    "[bold]✗ fix grid before saving[/]")
                return
            call = self.query_one("#dm-call", Input).value.strip()
            if not call:
                call = self.query_one("#dm-label", Input).value.strip()
            self.dismiss((
                SdrDeviceMeta(
                    key=self._meta.key,
                    label=self.query_one("#dm-label", Input).value.strip(),
                    call=call.upper(),
                    grid=grid,
                    channels=self._read_channels(),
                    ttl=self._read_ttl(),
                ),
                False,
            ))
        elif bid == "dm-copy-grid":
            grid = _normalize_grid(self.query_one("#dm-grid", Input).value.strip())
            if grid and not _GRID_RE.match(grid):
                self.query_one("#dm-grid-hint", Static).update(
                    "[bold]✗ fix grid before copying[/]")
                return
            call = self.query_one("#dm-call", Input).value.strip()
            if not call:
                call = self.query_one("#dm-label", Input).value.strip()
            self.dismiss((
                SdrDeviceMeta(
                    key=self._meta.key,
                    label=self.query_one("#dm-label", Input).value.strip(),
                    call=call.upper(),
                    grid=grid,
                    channels=self._read_channels(),
                    ttl=self._read_ttl(),
                ),
                True,
            ))
        elif bid == "dm-clear":
            self.dismiss((SdrDeviceMeta(key=self._meta.key), False))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# radiod config generation
# ---------------------------------------------------------------------------

_RADIOD_CONF_DIR = Path('/etc/radio')

# SDR types for which we auto-generate a radiod instance config.
_RX888_TYPES = {"RX-888", "RX-888 Mk2", "FX3 SDR"}


def _config_name(label: str) -> str:
    """Sanitize a device label into a valid DNS/systemd instance name."""
    name = label.lower().strip()
    name = re.sub(r'[^a-z0-9]+', '-', name)
    return name.strip('-') or 'rx888'


def _build_rx888_conf(config_name: str, device_index: int, ttl: int = 0) -> str:
    """Generate a minimal two-section radiod config for an RX-888."""
    ttl_comment = "  # set to 1 to send multicast out ethernet" if ttl == 0 else ""
    return (
        f"# radiod instance config — generated by smd tui\n"
        f"\n"
        f"[global]\n"
        f"hardware = rx888\n"
        f"status = {config_name}-hf.status\n"
        f"data = {config_name}-hf-pcm.local\n"
        f"samprate = 12k\n"
        f"mode = usb\n"
        f"ttl = {ttl}{ttl_comment}\n"
        f"\n"
        f"[rx888]\n"
        f"device = \"rx888\"\n"
        f"number = {device_index}\n"
        f"gain = 0\n"
        f"samprate = 129m600000\n"
        f"#samprate = 64m800000\n"
    )


def _write_radiod_conf(entry: SdrEntry, meta: SdrDeviceMeta) -> Optional[str]:
    """Write /etc/radio/radiod@<name>.conf for a USB SDR device.

    Returns an error string on failure, or None on success.
    """
    if entry.sdr_type not in _RX888_TYPES:
        return f"no config template for SDR type '{entry.sdr_type}'"

    try:
        device_index = int(entry.key.split(':')[-1])
    except (ValueError, IndexError):
        device_index = 0

    config_name = _config_name(meta.label)
    content = _build_rx888_conf(config_name, device_index, ttl=meta.ttl)

    dest = _RADIOD_CONF_DIR / f'radiod@{config_name}.conf'
    try:
        r = subprocess.run(
            ['sudo', 'tee', str(dest)],
            input=content, text=True,
            capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            return f"sudo tee failed: {r.stderr.strip()}"
    except Exception as e:
        return f"write failed: {e}"

    return None


def _verify_radiod_conf(config_name: str) -> Optional[str]:
    """Read back the written conf file and verify the status line.

    Returns an error string on mismatch/missing, or None on success.
    """
    dest = _RADIOD_CONF_DIR / f'radiod@{config_name}.conf'
    try:
        content = dest.read_text()
    except Exception as e:
        return f"cannot read {dest.name}: {e}"

    expected = f"{config_name}-hf.status"
    for line in content.splitlines():
        s = line.strip()
        if s.startswith('#'):
            continue
        if s.lower().startswith('status') and '=' in s:
            _, val = s.split('=', 1)
            actual = val.strip()
            if actual == expected:
                return None
            return f"status mismatch: file has '{actual}', expected '{expected}'"
    return f"status line not found in {dest.name}"


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------

class SdrInventoryScreen(Vertical):
    """Unified SDR receiver inventory — USB, KiwiSDR LAN, ka9q-radio frontends."""

    BINDINGS = [
        Binding("r", "rescan",      "Rescan"),
        Binding("e", "edit_label",  "Label"),
        Binding("d", "delete_entry","Delete"),
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
        self._last_click_row: int = -1
        self._last_click_time: float = 0.0

    def compose(self) -> ComposeResult:
        yield Static("SDR Inventory — USB, KiwiSDR LAN, ka9q-radio", classes="sdr-title")
        yield Static("[dim]scanning…[/]", id="sdr-status")

        table = DataTable(id="sdr-table", zebra_stripes=True, cursor_type="row")
        table.add_columns("Source", "Type", "Location", "Detail", "Serial", "Users", "GPS", "Config name", "Reporter ID", "Grid", "TTL")
        yield table

        with Horizontal(id="sdr-btn-row"):
            yield Button("↺ Rescan",        id="sdr-rescan",       variant="success")
            yield Button("✎ Edit",          id="sdr-label",        variant="primary")
            yield Button("🗑 Remove",        id="sdr-delete",       variant="error")
            yield Button("+ Add remote SDR", id="sdr-add-remote",  variant="default")

    def on_mount(self) -> None:
        self._rescan()

    def action_rescan(self) -> None:
        self._rescan()

    def action_edit_label(self) -> None:
        self._open_label_modal()

    def action_delete_entry(self) -> None:
        self._delete_selected()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Double-click or two quick Enter presses opens the edit modal."""
        now = time.monotonic()
        row = event.cursor_row
        if row == self._last_click_row and (now - self._last_click_time) <= 0.5:
            self._last_click_row = -1
            self._open_label_modal()
        else:
            self._last_click_row = row
            self._last_click_time = now

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sdr-rescan":
            self._rescan()
        elif event.button.id == "sdr-label":
            self._open_label_modal()
        elif event.button.id == "sdr-delete":
            self._delete_selected()
        elif event.button.id == "sdr-add-remote":
            self._open_add_remote_modal()

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
            "usb_sdr":    "[cyan]USB local[/]",
            "kiwisdr":    "[blue]KiwiSDR[/]",
            "ka9q_fe":    "[magenta]ka9q local[/]",
            "ka9q_remote":"[yellow]ka9q remote[/]",
        }
        for e in self._entries:
            src_cell = src_labels.get(e.source, e.source)
            type_cell = e.sdr_type
            if e.status not in ("ok", "none"):
                type_cell = f"[red]{e.sdr_type}[/]"
            name_cell   = f"[green]{e.label}[/]" if e.label else "[dim]—[/]"
            call_cell   = f"[cyan]{e.call}[/]"   if e.call  else "[dim]—[/]"
            grid_cell   = e.grid if e.grid else "[dim]—[/]"
            serial_cell = f"[dim]{e.serial}[/]"  if e.serial else "[dim]—[/]"
            if e.source == "usb_sdr" and e.status == "ok":
                ttl_cell = str(e.ttl)
            else:
                ttl_cell = "[dim]—[/]"
            table.add_row(
                src_cell, type_cell, e.location,
                e.detail[:35] if e.detail else "[dim]—[/]",
                serial_cell,
                e.users or "[dim]—[/]",
                e.gps   or "[dim]—[/]",
                name_cell, call_cell, grid_cell, ttl_cell,
                key=e.key,
            )

    def _delete_selected(self) -> None:
        table = self.query_one("#sdr-table", DataTable)
        idx = table.cursor_row
        if idx < 0 or idx >= len(self._entries):
            return
        entry = self._entries[idx]
        devices = load_devices()
        if entry.key in devices:
            del devices[entry.key]
            from ...sdr_labels import save_devices
            save_devices(devices)
        self._entries.pop(idx)
        self._render_entries()
        note = ""
        if entry.source == "usb_sdr":
            note = " (will reappear on rescan while device is attached)"
        self.query_one("#sdr-status", Static).update(
            f"[yellow]Removed {entry.label or entry.key}{note}[/]")

    def _open_add_remote_modal(self) -> None:
        from textual.screen import ModalScreen as _MS
        from textual.widgets import Input as _In, Label as _Lb

        class AddRemoteModal(_MS):
            BINDINGS = [Binding("escape", "cancel", "Cancel")]
            DEFAULT_CSS = """
            AddRemoteModal { align: center middle; }
            AddRemoteModal > Vertical {
                width: 66; height: auto; padding: 1 2;
                background: $panel; border: thick $primary;
            }
            AddRemoteModal Label  { margin-bottom: 0; }
            AddRemoteModal Input  { margin-bottom: 1; }
            AddRemoteModal Button { margin-right: 1; }
            AddRemoteModal #ar-err { height: 1; color: $error; }
            """
            def compose(self):
                with Vertical():
                    yield Static("[bold]Add remote ka9q-radio SDR[/]")
                    yield Static("[dim]For an RX-888 at another location whose multicast stream you receive.[/]")
                    yield _Lb("Status stream address (mDNS hostname or IP)")
                    yield _In(placeholder="e.g. southwest-hf.status  or  192.168.1.10",
                              id="ar-addr")
                    yield _Lb("Configuration name (label)")
                    yield _In(placeholder="e.g. KFS-Southwest", id="ar-label")
                    yield _Lb("Reporter ID")
                    yield _In(placeholder="e.g. KFS-SW", id="ar-call")
                    yield _Lb("Grid")
                    yield _In(placeholder="e.g. CM88mc", id="ar-grid")
                    yield Static("", id="ar-err")
                    with Horizontal():
                        yield Button("💾 Save",  id="ar-save",   variant="success")
                        yield Button("Cancel",   id="ar-cancel", variant="error")

            def on_mount(self):
                self.query_one("#ar-addr", _In).focus()

            def on_button_pressed(self, event: Button.Pressed) -> None:
                if event.button.id == "ar-save":
                    addr  = self.query_one("#ar-addr",  _In).value.strip()
                    label = self.query_one("#ar-label", _In).value.strip()
                    call  = self.query_one("#ar-call",  _In).value.strip().upper()
                    grid  = _normalize_grid(self.query_one("#ar-grid", _In).value.strip())
                    if not addr:
                        self.query_one("#ar-err", Static).update("Address is required")
                        return
                    self.dismiss((addr, label, call, grid))
                else:
                    self.dismiss(None)

            def action_cancel(self):
                self.dismiss(None)

        def _after(result) -> None:
            if result is None:
                return
            addr, label, call, grid = result
            key = f"ka9q_remote:{addr}"
            meta = SdrDeviceMeta(key=key, label=label, call=call, grid=grid)
            set_device(meta)
            entry = SdrEntry(
                key=key, source="ka9q_remote",
                sdr_type="ka9q remote",
                location=addr, detail="manually added",
                status="ok",
                label=label, call=call, grid=grid,
            )
            self._entries.append(entry)
            self._render_entries()
            self.query_one("#sdr-status", Static).update(
                f"[green]✔ Added remote SDR: {label or addr}[/]")

        self.app.push_screen(AddRemoteModal(), _after)

    def _open_label_modal(self) -> None:
        table = self.query_one("#sdr-table", DataTable)
        idx = table.cursor_row
        if idx < 0 or idx >= len(self._entries):
            return
        entry = self._entries[idx]
        current_meta = SdrDeviceMeta(
            key=entry.key, label=entry.label,
            call=entry.call, grid=entry.grid,
            channels=entry.channels, ttl=entry.ttl,
        )

        def _after(result) -> None:
            if result is None:
                return
            new_meta, copy_grid_to_all = result
            set_device(new_meta)
            entry.label    = new_meta.label
            entry.call     = new_meta.call
            entry.grid     = new_meta.grid
            entry.channels = new_meta.channels
            entry.ttl      = new_meta.ttl
            if copy_grid_to_all and new_meta.grid:
                devices = load_devices()
                for e2 in self._entries:
                    if e2.key != entry.key and not e2.grid:
                        d2 = devices.get(e2.key, SdrDeviceMeta(key=e2.key))
                        d2.grid = new_meta.grid
                        e2.grid = new_meta.grid
                        set_device(d2)
            self._render_entries()

            if entry.sdr_type in _RX888_TYPES and new_meta.label:
                err = _write_radiod_conf(entry, new_meta)
                if err:
                    self.query_one("#sdr-status", Static).update(
                        f"[red]radiod config error: {err}[/]")
                else:
                    cname = _config_name(new_meta.label)
                    verr = _verify_radiod_conf(cname)
                    if verr:
                        self.query_one("#sdr-status", Static).update(
                            f"[yellow]⚠ written but verify failed: {verr}[/]")
                    else:
                        self.query_one("#sdr-status", Static).update(
                            f"[green]✔ radiod@{cname}.conf written and verified[/]")

        self.app.push_screen(DeviceMetaModal(meta=current_meta), _after)
