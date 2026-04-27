"""wsprdaemon-client configuration screen.

Single band × receiver matrix.  Rows come from the SDR inventory; columns
are the 17 WSPR bands (2200m–6m, including 8m).  Each cell shows the active
decode modes or "—" if that band is not configured for that receiver.

Click a receiver name to toggle it on (green) or off (red).  Disabled
receivers are shown but excluded from save.  Click a band cell to edit
modes; double-click to fill all empty bands first then edit the cell.
KiwiSDR rows are capped at their rx_chans channel count; default bands
are 80/40/30/20/17/15/12/10m.

The generated /etc/wsprdaemon/wsprdaemon.conf uses receiver names derived
from SDR type (KA9Q_N for USB/ka9q-radio; KIWI_N for KiwiSDR) and call/grid
from the SDR inventory.
"""

from __future__ import annotations

import subprocess
import time
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Static
from textual.worker import Worker, WorkerState

from ..mutation import UpdateOutputModal

from ...wd_client_config import (
    ALL_BANDS, ALL_MODES, KIWI_DEFAULT_BANDS, WD_CONF_PATH,
    WdConfig, WdReceiver,
    default_modes, is_v3_format, load_config, save_config,
)
from ...sdr_labels import SdrDeviceMeta, load_devices


# ---------------------------------------------------------------------------
# Row data model — one per SDR inventory entry
# ---------------------------------------------------------------------------

class ReceiverRow:
    """Bridges an SDR inventory entry to a WdReceiver in the config."""

    def __init__(self, meta: SdrDeviceMeta, rx: WdReceiver) -> None:
        self.meta    = meta    # from SDR inventory
        self.rx      = rx      # WdReceiver (may have empty bands initially)
        # Start enabled only if bands are already configured; new/unconfigured
        # receivers (e.g. KiwiSDRs found by mDNS with no config yet) start
        # disabled so the user must explicitly opt them in.
        self.enabled = bool(rx.bands)

    @property
    def display_name(self) -> str:
        return self.meta.label or self.rx.name or self.meta.key

    @property
    def name(self) -> str:
        return self.rx.name

    @property
    def channel_limit(self) -> int:
        """Max simultaneous bands for this receiver; 0 = unlimited (USB/ka9q).
        KiwiSDR defaults to 8 if the scan hasn't stored rx_chans yet."""
        if not self.meta.key.startswith("kiwisdr:"):
            return 0
        return self.meta.channels if self.meta.channels > 0 else 8


def _trim_kiwi_bands(rx: WdReceiver, limit: int) -> None:
    """Trim a KiwiSDR receiver's band dict down to *limit* entries.

    Retention priority: KIWI_DEFAULT_BANDS first, then whatever else was
    configured, in original insertion order.
    """
    if not limit or len(rx.bands) <= limit:
        return
    kept: dict[str, str] = {}
    for band in KIWI_DEFAULT_BANDS:
        if band in rx.bands and len(kept) < limit:
            kept[band] = rx.bands[band]
    for band, modes in rx.bands.items():
        if len(kept) >= limit:
            break
        if band not in kept:
            kept[band] = modes
    rx.bands = kept


def _make_wd_name(meta: SdrDeviceMeta, existing_names: set[str]) -> str:
    """Derive a wsprdaemon receiver name from an SDR inventory entry."""
    key = meta.key
    if key.startswith("kiwisdr:"):
        prefix = "KIWI"
    else:
        prefix = "KA9Q"
    n = 0
    while f"{prefix}_{n}" in existing_names:
        n += 1
    return f"{prefix}_{n}"


def _build_rows(
    inventory: dict[str, SdrDeviceMeta],
    existing_conf: WdConfig,
) -> list[ReceiverRow]:
    """Merge SDR inventory + existing conf into a list of ReceiverRows.

    Priority order:
      1. Inventory entries that match an existing conf receiver by address
      2. Inventory entries not yet in the conf  (empty band set)
      3. Conf receivers with no matching inventory entry  (kept as-is)
    """
    rows: list[ReceiverRow] = []
    used_names: set[str] = set()

    # Build address → WdReceiver map for fast lookup (addressed receivers only)
    addr_to_rx: dict[str, WdReceiver] = {}
    for rx in existing_conf.receivers.values():
        if rx.address:
            addr_to_rx[rx.address] = rx

    # --- Inventory entries only (conf receivers with no matching inventory entry
    #     are intentionally dropped — user should add them to SDR inventory first) ---
    for meta in inventory.values():
        if meta.key in existing_conf.excluded_keys:
            continue   # explicitly hidden by the user
        # Derive address from the inventory key
        if meta.key.startswith("kiwisdr:"):
            address = meta.key.replace("kiwisdr:", "")  # "ip:port"
        elif meta.key.startswith("ka9q_fe:"):
            parts = meta.key.split(":", 2)
            address = parts[2] if len(parts) > 2 else ""
        elif meta.key.startswith("ka9q_remote:"):
            address = meta.key.replace("ka9q_remote:", "")
        else:
            address = ""   # USB SDR — address set by user

        # Match to existing conf receiver.
        # For USB SDRs (no address) match by the name that would be generated
        # (KA9Q_0, KA9Q_1, …) so that an already-configured receiver isn't lost
        # when the screen is reloaded.
        if address:
            existing_rx = addr_to_rx.get(address)
        else:
            existing_rx = existing_conf.receivers.get(_make_wd_name(meta, used_names))

        if existing_rx:
            rx = existing_rx
            used_names.add(rx.name)
        else:
            name = _make_wd_name(meta, used_names)
            used_names.add(name)
            rx = WdReceiver(
                name=name,
                address=address,
                call=meta.call,
                grid=meta.grid,
            )

        # Fill call/grid from inventory if conf doesn't have them
        if not rx.call and meta.call:
            rx.call = meta.call
        if not rx.grid and meta.grid:
            rx.grid = meta.grid

        # Enforce channel limit for KiwiSDRs (trims excess bands on load)
        if meta.key.startswith("kiwisdr:"):
            _trim_kiwi_bands(rx, meta.channels if meta.channels > 0 else 8)

        rows.append(ReceiverRow(meta=meta, rx=rx))

    return rows


# ---------------------------------------------------------------------------
# Mode toggle modal
# ---------------------------------------------------------------------------

class ModeModal(ModalScreen):
    """Toggle decode modes for one (receiver, band) cell.
    Dismisses with space-separated modes string, "" to disable, or None to cancel.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ModeModal { align: center middle; }
    ModeModal > Vertical {
        width: 52;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }
    ModeModal .mm-title  { text-style: bold; margin-bottom: 1; }
    ModeModal .mm-hint   { color: $text-muted; margin-bottom: 1; }
    ModeModal Checkbox   { margin-bottom: 0; }
    ModeModal #mm-btns   { height: auto; margin-top: 1; }
    ModeModal #mm-btns-l { width: auto; }
    ModeModal #mm-spacer { width: 1fr; }
    ModeModal #mm-btns-r { width: auto; }
    ModeModal Button     { margin-right: 1; }
    """

    # I1 (IQ archive) is supported in the config format but not offered in the UI
    _UI_MODES = ["W2", "F2", "F5", "F15", "F30"]

    _MODE_DESCS = {
        "W2":  "W2  — WSPR 2-minute (standard)",
        "F2":  "F2  — FST4W 2-minute",
        "F5":  "F5  — FST4W 5-minute",
        "F15": "F15 — FST4W 15-minute",
        "F30": "F30 — FST4W 30-minute",
    }

    def __init__(self, rx_name: str, band: str,
                 current_modes: str, default_modes_str: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rx    = rx_name
        self._band  = band
        self._current = set(current_modes.split()) if current_modes.strip() else None
        # None means "was empty" — we'll pre-populate with defaults
        self._defaults = set(default_modes_str.split())

    def compose(self) -> ComposeResult:
        active = self._current if self._current is not None else self._defaults
        with Vertical():
            yield Static(f"Modes: {self._rx} / {self._band}m", classes="mm-title")
            if self._current is None:
                yield Static("[dim]pre-populated with band defaults[/]", classes="mm-hint")
            for mode in self._UI_MODES:
                yield Checkbox(
                    self._MODE_DESCS.get(mode, mode),
                    value=(mode in active),
                    id=f"mm-{mode}",
                )
            with Horizontal(id="mm-btns"):
                with Horizontal(id="mm-btns-l"):
                    yield Button("💾 Save",    id="mm-save",    variant="success")
                Static("", id="mm-spacer")
                with Horizontal(id="mm-btns-r"):
                    yield Button("Disable",    id="mm-disable", variant="error")
                    yield Button("Cancel",     id="mm-cancel",  variant="default")

    def on_mount(self) -> None:
        try:
            self.query_one(f"#mm-{self._UI_MODES[0]}", Checkbox).focus()
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mm-save":
            modes = [m for m in self._UI_MODES
                     if self.query_one(f"#mm-{m}", Checkbox).value]
            self.dismiss(" ".join(modes) if modes else "")
        elif event.button.id == "mm-disable":
            self.dismiss("")
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------

def _cell_visible_width(modes_str: str) -> int:
    """Visible character count for a modes cell (markup-stripped, for column sizing)."""
    if not modes_str.strip():
        return 1  # "—"
    # W2 renders as "W" (1 char); I1 renders as "I" (1 char); others render as-is
    abbrev_w = {"W2": 1, "I1": 1}
    tokens = modes_str.split()
    widths = [abbrev_w.get(t, len(t)) for t in tokens]
    return sum(widths) + max(len(widths) - 1, 0)  # sum + (n-1) "·" separators


def _modes_cell_plain(modes_str: str) -> str:
    """Plain-text version of _modes_cell — no markup, safe to wrap in [red]/[green]."""
    if not modes_str.strip():
        return "—"
    return "·".join(
        "W" if t == "W2" else ("I" if t == "I1" else t)
        for t in modes_str.split()
    )


def _modes_cell(modes_str: str) -> str:
    if not modes_str.strip():
        return "[dim]—[/]"
    abbrev = []
    for t in modes_str.split():
        if t == "W2":    abbrev.append("[green]W[/]")
        elif t == "F2":  abbrev.append("F2")
        elif t == "F5":  abbrev.append("F5")
        elif t == "F15": abbrev.append("F15")
        elif t == "F30": abbrev.append("F30")
        elif t == "I1":  abbrev.append("[dim]I[/]")
        else:            abbrev.append(t)
    return "·".join(abbrev)


class WdClientScreen(Vertical):
    """wsprdaemon-client — band × receiver configuration grid."""

    BINDINGS = [
        Binding("s", "save",  "Save"),
        Binding("a", "apply", "Apply"),
        Binding("r", "reload","Reload"),
    ]

    DEFAULT_CSS = """
    WdClientScreen { padding: 1; }
    WdClientScreen .wd-title  { text-style: bold; margin-bottom: 1; }
    WdClientScreen #wd-status { margin-bottom: 1; }
    WdClientScreen #wd-btn-row { height: 3; margin-top: 1; }
    WdClientScreen #wd-btn-row Button { margin-right: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows: list[ReceiverRow] = []
        self._excluded_keys: set[str] = set()
        self._dirty = False
        self._last_cell: tuple[int, int] = (-1, -1)
        self._last_cell_time: float = 0.0

    def compose(self) -> ComposeResult:
        yield Static("wsprdaemon-client Configuration", classes="wd-title")
        yield Static("[dim]loading…[/]", id="wd-status")

        yield DataTable(id="wd-table", zebra_stripes=True, cursor_type="cell")

        with Horizontal(id="wd-btn-row"):
            yield Button("↺ Reload",          id="wd-reload",  variant="primary")
            yield Button("💾 Save",           id="wd-save",    variant="success")
            yield Button("▶ Apply (wd-ctl)", id="wd-apply",   variant="warning")
            yield Button("🗑 Delete receiver", id="wd-delete",  variant="error")

    def on_mount(self) -> None:
        self._load()

    # ------------------------------------------------------------------
    # loading

    def _load(self) -> None:
        self.query_one("#wd-status", Static).update("[dim]reading SDR inventory + config…[/]")
        self.run_worker(self._worker_load, thread=True, name="wd-load")

    def _worker_load(self) -> tuple:
        inventory = load_devices()
        conf      = load_config()
        return inventory, conf

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "wd-load":
            if event.state == WorkerState.SUCCESS:
                inventory, conf = event.worker.result
                self._excluded_keys = set(conf.excluded_keys)
                self._rows = _build_rows(inventory, conf)
                self._dirty = False
                self._refresh_table()
                self._refresh_status()

    def _refresh_status(self) -> None:
        if is_v3_format():
            self.query_one("#wd-status", Static).update(
                "[yellow]v3 bash config — run [bold]sudo wd-ctl migrate-config[/] to convert[/]")
            return
        enabled = sum(1 for r in self._rows if r.rx.bands and r.enabled)
        total   = len(self._rows)
        bands   = sum(len(r.rx.bands) for r in self._rows if r.enabled)
        self.query_one("#wd-status", Static).update(
            f"{total} receiver(s) · [green]{enabled} active[/] · {bands} band assignments · "
            f"[dim]{WD_CONF_PATH}[/]"
            + (" [yellow]· unsaved changes[/]" if self._dirty else "")
        )

    def _refresh_table(self) -> None:
        table = self.query_one("#wd-table", DataTable)

        # Compute per-column widths from actual data so every cell is visible
        rx_width = max(
            (max(len(row.display_name), len(row.rx.call)) for row in self._rows),
            default=10,
        )
        rx_width = max(rx_width, 8)

        band_widths: dict[str, int] = {}
        for band in ALL_BANDS:
            w = len(band)  # header is the floor
            for row in self._rows:
                w = max(w, _cell_visible_width(row.rx.bands.get(band, "")))
            band_widths[band] = w

        table.clear(columns=True)
        table.add_column("Receiver", width=rx_width)
        for band in ALL_BANDS:
            table.add_column(band, width=band_widths[band])

        first_configured = next(
            (i for i, row in enumerate(self._rows) if row.rx.bands), None
        )

        for row in self._rows:
            name_color = "green" if row.enabled else "red"
            name_cell = f"[{name_color}]{row.display_name}[/]"
            if row.rx.call:
                name_cell += f"\n[dim]{row.rx.call}[/]"
            if row.channel_limit:
                configured = len(row.rx.bands)
                ch_color = "yellow" if configured >= row.channel_limit else "dim"
                name_cell += f"\n[{ch_color}]{configured}/{row.channel_limit} ch[/]"
            cells = [name_cell]
            for band in ALL_BANDS:
                if row.enabled:
                    cells.append(_modes_cell(row.rx.bands.get(band, "")))
                else:
                    cells.append(f"[red]{_modes_cell_plain(row.rx.bands.get(band, ''))}[/]")
            table.add_row(*cells, key=row.name)

        if first_configured is not None:
            table.move_cursor(row=first_configured)

    # ------------------------------------------------------------------
    # cell click / double-click → mode modal

    def _populate_row_defaults(self, receiver_row: ReceiverRow) -> bool:
        """Fill undefined bands with defaults up to the channel limit (0 = unlimited).
        KiwiSDR uses KIWI_DEFAULT_BANDS order; other receivers use ALL_BANDS order.
        Returns True if any band was changed."""
        limit = receiver_row.channel_limit
        is_kiwi = receiver_row.meta.key.startswith("kiwisdr:")
        bands_source = KIWI_DEFAULT_BANDS if is_kiwi else ALL_BANDS
        changed = False
        for band in bands_source:
            if limit and len(receiver_row.rx.bands) >= limit:
                break
            if band not in receiver_row.rx.bands:
                receiver_row.rx.bands[band] = default_modes(band)
                changed = True
        if changed:
            self._dirty = True
            self._refresh_table()
            self._refresh_status()
        return changed

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        col     = event.coordinate.column
        row_idx = event.coordinate.row
        if row_idx < 0 or row_idx >= len(self._rows):
            return
        receiver_row = self._rows[row_idx]

        # Detect double-click / double-Enter on same cell (≤ 0.5 s)
        now  = time.monotonic()
        cell = (row_idx, col)
        is_double = (cell == self._last_cell and now - self._last_cell_time <= 0.5)
        self._last_cell       = cell
        self._last_cell_time  = now

        # Click on receiver-name column: toggle enabled/disabled
        if col == 0:
            receiver_row.enabled = not receiver_row.enabled
            if receiver_row.enabled and not receiver_row.rx.bands:
                # Just enabled with no bands yet → auto-populate defaults
                self._populate_row_defaults(receiver_row)
                return   # _populate_row_defaults handles refresh
            self._dirty = True
            self._refresh_table()
            self._refresh_status()
            state = "enabled" if receiver_row.enabled else "disabled"
            self.query_one("#wd-status", Static).update(
                f"[dim]{receiver_row.display_name}: {state}[/]")
            return

        band  = ALL_BANDS[col - 1]
        limit = receiver_row.channel_limit

        # Double-click on a band cell: first populate all undefined bands, then edit this one
        if is_double:
            self._populate_row_defaults(receiver_row)

        current = receiver_row.rx.bands.get(band, "")

        # Warn if adding a new band would exceed the channel limit
        if limit and not current and len(receiver_row.rx.bands) >= limit:
            self.query_one("#wd-status", Static).update(
                f"[yellow]{receiver_row.display_name}: already using all "
                f"{limit} channel(s) — disable a band before adding another[/]"
            )
            return
        dfl     = default_modes(band)

        def _after(new_modes: Optional[str]) -> None:
            if new_modes is None:
                return
            if new_modes:
                receiver_row.rx.bands[band] = new_modes
            else:
                receiver_row.rx.bands.pop(band, None)
            self._dirty = True
            self._refresh_table()
            self._refresh_status()

        self.app.push_screen(
            ModeModal(rx_name=receiver_row.display_name,
                      band=band,
                      current_modes=current,
                      default_modes_str=dfl),
            _after,
        )

    # ------------------------------------------------------------------
    # buttons

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "wd-save":
            self.action_save()
        elif bid == "wd-apply":
            self.action_apply()
        elif bid == "wd-reload":
            self.action_reload()
        elif bid == "wd-delete":
            self._delete_selected_receiver()

    def _delete_selected_receiver(self) -> None:
        table = self.query_one("#wd-table", DataTable)
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._rows):
            return
        row = self._rows[row_idx]
        name = row.display_name
        self._excluded_keys.add(row.meta.key)
        self._rows.pop(row_idx)
        self._dirty = True
        self._refresh_table()
        self.query_one("#wd-status", Static).update(
            f"[yellow]Deleted {name} — save to hide permanently[/]")

    def action_reload(self) -> None:
        self._dirty = False
        self._load()

    def action_save(self) -> None:
        # Auto-populate undefined bands (up to channel limit) for active receivers
        needs_refresh = False
        for row in self._rows:
            if row.rx.bands and row.enabled:
                limit = row.channel_limit
                is_kiwi = row.meta.key.startswith("kiwisdr:")
                bands_source = KIWI_DEFAULT_BANDS if is_kiwi else ALL_BANDS
                for band in bands_source:
                    if limit and len(row.rx.bands) >= limit:
                        break
                    if band not in row.rx.bands:
                        row.rx.bands[band] = default_modes(band)
                        needs_refresh = True
        if needs_refresh:
            self._dirty = True
            self._refresh_table()

        # Build a WdConfig from current rows (only receivers with at least one band)
        wdc = WdConfig()
        # Preserve general settings from existing conf
        existing = load_config()
        wdc.ka9q_conf_name = existing.ka9q_conf_name
        # RAC is managed independently by Sigmond — do not write it to wsprdaemon.conf
        wdc.excluded_keys = set(self._excluded_keys)
        for row in self._rows:
            if not row.rx.bands or not row.enabled:
                continue   # skip unconfigured or disabled receivers
            rx = row.rx
            # Ensure call/grid from inventory take precedence over stale conf values
            if row.meta.call:
                rx.call = row.meta.call
            if row.meta.grid:
                rx.grid = row.meta.grid
            wdc.receivers[rx.name] = rx
        # Build a simple always-on schedule
        slot: dict[str, str] = {}
        for rx in wdc.receivers.values():
            if rx.bands:
                slot[rx.name] = " ".join(sorted(rx.bands.keys()))
        if slot:
            wdc.schedule["main"] = slot
        try:
            save_config(wdc)
            self._dirty = False
            self._refresh_status()
            self.query_one("#wd-status", Static).update(
                f"[green]Saved → {WD_CONF_PATH}[/]")
        except PermissionError:
            self._save_via_sudo(wdc)
        except Exception as e:
            self.query_one("#wd-status", Static).update(f"[red]Save failed: {e}[/]")

    def _save_via_sudo(self, wdc: WdConfig) -> None:
        import tempfile, pathlib
        try:
            tmp = pathlib.Path(tempfile.mktemp(suffix='.conf'))
            save_config(wdc, tmp)
            r = subprocess.run(
                ['sudo', 'cp', str(tmp), str(WD_CONF_PATH)],
                capture_output=True, timeout=15,
            )
            if r.returncode == 0:
                self._dirty = False
                self.query_one("#wd-status", Static).update(
                    f"[green]Saved via sudo → {WD_CONF_PATH}[/]")
            else:
                self.query_one("#wd-status", Static).update(
                    f"[red]sudo cp failed: {r.stderr.decode()[:80]}[/]")
        except Exception as e:
            self.query_one("#wd-status", Static).update(f"[red]Save failed: {e}[/]")

    def action_apply(self) -> None:
        if self._dirty:
            self.action_save()

        import shutil as _shutil, sys as _sys, os as _os
        argv0 = _os.path.abspath(_sys.argv[0]) if _sys.argv and _sys.argv[0] else ""
        smd = (argv0 if argv0 and _os.path.isfile(argv0)
               and _os.path.basename(argv0) == 'smd'
               else _shutil.which('smd') or '/usr/local/sbin/smd')

        def _after_modal(_result: object) -> None:
            self.query_one("#wd-status", Static).update("[dim]apply complete — reloading…[/]")
            self._load()

        self.app.push_screen(
            UpdateOutputModal(
                title="Apply wsprdaemon-client configuration",
                # Run smd apply instead of wd-ctl directly so that CPU affinity
                # is set first and upload services are restarted last.
                cmd=['sudo', smd, 'apply'],
            ),
            _after_modal,
        )
