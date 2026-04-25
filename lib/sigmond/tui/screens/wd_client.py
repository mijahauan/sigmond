"""wsprdaemon-client configuration screen.

Displays a receiver × band grid.  Each cell shows which decode modes are
active for that (receiver, band) pair.  Click a cell (or press Enter) to
toggle modes via a small modal.

Layout
------
  Title + status line
  Receiver metadata table  (Name | Type | Address | Call | Grid)
  Band × mode grid         (scrolls horizontally for all 16 bands)
  Button row               (Add Receiver | Remove | Save | Apply)

Reads /etc/wsprdaemon/wsprdaemon.conf (v4 INI).  Warns if v3 bash
format is detected and offers to launch the migration command.
"""

from __future__ import annotations

import subprocess
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select, Static
from textual.worker import Worker, WorkerState

from ...wd_client_config import (
    ALL_BANDS, ALL_MODES, WD_CONF_PATH,
    WdConfig, WdReceiver,
    is_v3_format, load_config, save_config,
)
from ...sdr_labels import load_devices


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _modes_cell(modes_str: str) -> str:
    """Compact display for a modes string, e.g. 'W2 F2 F5' → 'W·F2·F5'."""
    if not modes_str.strip():
        return "[dim]—[/]"
    tokens = modes_str.split()
    abbrev = []
    for t in tokens:
        if t == "W2":   abbrev.append("W")
        elif t == "F2":  abbrev.append("F2")
        elif t == "F5":  abbrev.append("F5")
        elif t == "F15": abbrev.append("F15")
        elif t == "F30": abbrev.append("F30")
        elif t == "I1":  abbrev.append("I")
        else:            abbrev.append(t)
    return "·".join(abbrev)


def _smd_binary() -> str:
    import os, sys, shutil
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    return shutil.which('smd') or '/usr/local/sbin/smd'


# ---------------------------------------------------------------------------
# Mode toggle modal
# ---------------------------------------------------------------------------

class ModeModal(ModalScreen[Optional[str]]):
    """Toggle decode modes for one (receiver, band) cell."""

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
    ModeModal Horizontal { height: auto; align: right middle; margin-top: 1; }
    ModeModal Button     { margin-left: 1; }
    """

    _MODE_DESCS = {
        "W2":  "W2  — WSPR 2-minute (standard)",
        "F2":  "F2  — FST4W 2-minute",
        "F5":  "F5  — FST4W 5-minute",
        "F15": "F15 — FST4W 15-minute",
        "F30": "F30 — FST4W 30-minute",
        "I1":  "I1  — IQ archive only (no decode)",
    }

    def __init__(self, rx_name: str, band: str, current_modes: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rx   = rx_name
        self._band = band
        self._current = set(current_modes.split()) if current_modes.strip() else set()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Modes for {self._rx} / {self._band}m", classes="mm-title")
            yield Static("Check modes to enable; uncheck to disable.", classes="mm-hint")
            for mode in ALL_MODES:
                yield Checkbox(
                    self._MODE_DESCS.get(mode, mode),
                    value=(mode in self._current),
                    id=f"mm-{mode}",
                )
            with Horizontal():
                yield Button("Cancel",   id="mm-cancel", variant="default")
                yield Button("Disable",  id="mm-disable",variant="warning")
                yield Button("Save",     id="mm-save",   variant="success")

    def on_mount(self) -> None:
        # Focus first checkbox
        try:
            self.query_one(f"#mm-{ALL_MODES[0]}", Checkbox).focus()
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mm-save":
            modes = [m for m in ALL_MODES
                     if self.query_one(f"#mm-{m}", Checkbox).value]
            self.dismiss(" ".join(modes) if modes else "")
        elif event.button.id == "mm-disable":
            self.dismiss("")
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Add receiver modal
# ---------------------------------------------------------------------------

class AddReceiverModal(ModalScreen[Optional[WdReceiver]]):
    """Enter details for a new receiver."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    AddReceiverModal { align: center middle; }
    AddReceiverModal > Vertical {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }
    AddReceiverModal Label   { margin-bottom: 0; }
    AddReceiverModal Input   { margin-bottom: 1; }
    AddReceiverModal Select  { margin-bottom: 1; }
    AddReceiverModal Horizontal { height: auto; align: right middle; margin-top: 1; }
    AddReceiverModal Button  { margin-left: 1; }
    """

    def __init__(self, sdr_suggestions: list[tuple[str, str]], **kwargs) -> None:
        super().__init__(**kwargs)
        self._suggestions = sdr_suggestions  # [(display, address), ...]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Receiver name (e.g. KA9Q_0 or KIWI_0)")
            yield Input(placeholder="KA9Q_0", id="ar-name")
            yield Label("Address (multicast DNS or host:port)")
            if self._suggestions:
                opts = [(f"{d} — {a}", a) for d, a in self._suggestions]
                yield Select(
                    options=[(label, val) for label, val in opts],
                    prompt="Pick from SDR inventory…",
                    id="ar-addr-select",
                    allow_blank=True,
                )
            yield Input(placeholder="k3lr-wspr-pcm.local  or  192.168.1.100:8073",
                        id="ar-address")
            yield Label("WSPR reporter callsign")
            yield Input(placeholder="AI6VN-0", id="ar-call")
            yield Label("Maidenhead grid square")
            yield Input(placeholder="CM88mc",  id="ar-grid")
            with Horizontal():
                yield Button("Cancel", id="ar-cancel", variant="default")
                yield Button("Add",    id="ar-add",    variant="success")

    def on_mount(self) -> None:
        self.query_one("#ar-name", Input).focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "ar-addr-select" and event.value:
            self.query_one("#ar-address", Input).value = str(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ar-add":
            name    = self.query_one("#ar-name",    Input).value.strip().upper()
            address = self.query_one("#ar-address", Input).value.strip()
            call    = self.query_one("#ar-call",    Input).value.strip().upper()
            grid    = self.query_one("#ar-grid",    Input).value.strip()
            if not name or not address:
                return
            self.dismiss(WdReceiver(name=name, address=address,
                                    call=call, grid=grid))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------

class WdClientScreen(Vertical):
    """wsprdaemon-client configuration — receiver × band grid editor."""

    BINDINGS = [
        Binding("s", "save",  "Save"),
        Binding("a", "apply", "Apply"),
    ]

    DEFAULT_CSS = """
    WdClientScreen { padding: 1; }
    WdClientScreen .wd-title  { text-style: bold; margin-bottom: 1; }
    WdClientScreen #wd-status { margin-bottom: 1; }
    WdClientScreen #wd-rx-table  { margin-bottom: 0; }
    WdClientScreen #wd-band-table { margin-top: 0; }
    WdClientScreen .wd-section { text-style: bold; margin-top: 1; margin-bottom: 0; }
    WdClientScreen #wd-btn-row { height: 3; margin-top: 1; }
    WdClientScreen #wd-btn-row Button { margin-right: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._config: WdConfig = WdConfig()
        self._dirty = False

    def compose(self) -> ComposeResult:
        yield Static("wsprdaemon-client Configuration", classes="wd-title")
        yield Static("[dim]loading…[/]", id="wd-status")

        yield Static("Receivers", classes="wd-section")
        rx_table = DataTable(id="wd-rx-table", zebra_stripes=True, cursor_type="row")
        rx_table.add_columns("Name", "Type", "Address", "Call", "Grid")
        yield rx_table

        yield Static("Band / Mode Matrix  (click cell to toggle modes)", classes="wd-section")
        band_table = DataTable(id="wd-band-table", zebra_stripes=True, cursor_type="cell")
        band_table.add_columns("Receiver", *ALL_BANDS)
        yield band_table

        with Horizontal(id="wd-btn-row"):
            yield Button("+ Add receiver",    id="wd-add",    variant="default")
            yield Button("− Remove selected", id="wd-remove", variant="error")
            yield Button("💾 Save",           id="wd-save",   variant="success")
            yield Button("▶ Apply (wd-ctl)", id="wd-apply",  variant="warning")

    def on_mount(self) -> None:
        self._load()

    # ------------------------------------------------------------------
    # loading

    def _load(self) -> None:
        self.query_one("#wd-status", Static).update("[dim]reading config…[/]")
        self.run_worker(self._worker_load, thread=True, name="wd-load")

    def _worker_load(self) -> WdConfig:
        return load_config()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "wd-load":
            if event.state == WorkerState.SUCCESS:
                self._config = event.worker.result
                self._refresh_view()
        elif event.worker.name == "wd-apply-run":
            if event.state == WorkerState.SUCCESS:
                rc, out = event.worker.result
                if rc == 0:
                    self.query_one("#wd-status", Static).update(
                        "[green]wd-ctl apply succeeded[/]")
                else:
                    self.query_one("#wd-status", Static).update(
                        f"[red]wd-ctl apply exited {rc}[/]  {out[:120]}")

    def _refresh_view(self) -> None:
        if is_v3_format():
            self.query_one("#wd-status", Static).update(
                "[yellow]v3 bash config detected — run "
                "[bold]sudo wd-ctl migrate-config[/] to convert to v4[/]")
            return

        n_rx = len(self._config.receivers)
        total_bands = sum(len(rx.bands) for rx in self._config.receivers.values())
        self.query_one("#wd-status", Static).update(
            f"{n_rx} receiver(s) · {total_bands} band assignment(s) · "
            f"[dim]{WD_CONF_PATH}[/]"
            + (" [yellow]unsaved changes[/]" if self._dirty else "")
        )

        self._populate_rx_table()
        self._populate_band_table()

    def _populate_rx_table(self) -> None:
        table = self.query_one("#wd-rx-table", DataTable)
        table.clear()
        type_color = {"ka9q": "cyan", "kiwi": "blue", "merge": "magenta",
                      "ka9q_wwv": "cyan", "unknown": "dim"}
        for rx in self._config.receivers.values():
            tc = type_color.get(rx.receiver_type, "")
            type_cell = (f"[{tc}]{rx.receiver_type}[/]"
                         if tc and tc != "dim" else f"[dim]{rx.receiver_type}[/]")
            table.add_row(
                rx.name, type_cell, rx.address or "[dim]—[/]",
                rx.call or "[dim]—[/]", rx.grid or "[dim]—[/]",
                key=rx.name,
            )

    def _populate_band_table(self) -> None:
        table = self.query_one("#wd-band-table", DataTable)
        table.clear()
        for rx in self._config.receivers.values():
            cells = [rx.name]
            for band in ALL_BANDS:
                modes = rx.bands.get(band, "")
                cells.append(_modes_cell(modes))
            table.add_row(*cells, key=rx.name)

    # ------------------------------------------------------------------
    # cell click → mode modal

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        if event.data_table.id != "wd-band-table":
            return
        col_idx = event.coordinate.column
        row_idx = event.coordinate.row
        if col_idx == 0:
            return  # receiver name column
        band = ALL_BANDS[col_idx - 1]
        # Get receiver name from row key
        try:
            row_key = event.data_table.get_row_index(row_idx)
        except Exception:
            row_key = None
        rx_names = list(self._config.receivers.keys())
        if row_idx < 0 or row_idx >= len(rx_names):
            return
        rx_name = rx_names[row_idx]
        rx = self._config.receivers.get(rx_name)
        if rx is None:
            return

        current_modes = rx.bands.get(band, "")

        def _after(new_modes: Optional[str]) -> None:
            if new_modes is None:
                return
            if new_modes:
                rx.bands[band] = new_modes
            else:
                rx.bands.pop(band, None)
            self._dirty = True
            self._populate_band_table()
            self._refresh_status()

        self.app.push_screen(
            ModeModal(rx_name=rx_name, band=band, current_modes=current_modes),
            _after,
        )

    def _refresh_status(self) -> None:
        n_rx = len(self._config.receivers)
        total_bands = sum(len(rx.bands) for rx in self._config.receivers.values())
        self.query_one("#wd-status", Static).update(
            f"{n_rx} receiver(s) · {total_bands} band assignment(s) · "
            f"[dim]{WD_CONF_PATH}[/]"
            + (" [yellow]unsaved changes[/]" if self._dirty else "")
        )

    # ------------------------------------------------------------------
    # buttons

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "wd-save":
            self.action_save()
        elif bid == "wd-apply":
            self.action_apply()
        elif bid == "wd-add":
            self._add_receiver()
        elif bid == "wd-remove":
            self._remove_selected()

    def action_save(self) -> None:
        try:
            save_config(self._config)
            self._dirty = False
            self._refresh_status()
            self.query_one("#wd-status", Static).update(
                f"[green]Saved to {WD_CONF_PATH}[/]")
        except PermissionError:
            self._save_via_sudo()
        except Exception as e:
            self.query_one("#wd-status", Static).update(f"[red]Save failed: {e}[/]")

    def _save_via_sudo(self) -> None:
        import tempfile
        try:
            with tempfile.NamedTemporaryFile('w', suffix='.conf', delete=False) as f:
                from ...wd_client_config import save_config as _sc
                # write to tmp
                import io
                # rebuild content via save_config to a temp path
                tmp_path = __import__('pathlib').Path(f.name)
            save_config(self._config, tmp_path)
            r = subprocess.run(
                ['sudo', 'cp', str(tmp_path), str(WD_CONF_PATH)],
                capture_output=True, timeout=15,
            )
            if r.returncode == 0:
                self._dirty = False
                self.query_one("#wd-status", Static).update(
                    f"[green]Saved via sudo to {WD_CONF_PATH}[/]")
            else:
                self.query_one("#wd-status", Static).update(
                    f"[red]sudo cp failed: {r.stderr.decode()[:80]}[/]")
        except Exception as e:
            self.query_one("#wd-status", Static).update(f"[red]Save failed: {e}[/]")

    def action_apply(self) -> None:
        if self._dirty:
            self.action_save()
        self.query_one("#wd-status", Static).update("[dim]running wd-ctl apply…[/]")
        self.run_worker(self._worker_apply, thread=True, name="wd-apply-run")

    def _worker_apply(self) -> tuple[int, str]:
        r = subprocess.run(
            ['sudo', 'wd-ctl', 'apply'],
            capture_output=True, text=True, timeout=120,
        )
        return r.returncode, (r.stdout + r.stderr)[-500:]

    def _add_receiver(self) -> None:
        # Build suggestions from SDR inventory
        devices = load_devices()
        suggestions: list[tuple[str, str]] = []
        for meta in devices.values():
            if meta.key.startswith("kiwisdr:"):
                ip_port = meta.key.replace("kiwisdr:", "")
                display = f"{meta.label or 'KiwiSDR'} ({ip_port})"
                suggestions.append((display, ip_port))
            elif meta.key.startswith("usb:"):
                display = meta.label or meta.key
                suggestions.append((display, ""))
            elif meta.key.startswith("ka9q_fe:"):
                parts = meta.key.split(":", 2)
                addr = parts[2] if len(parts) > 2 else ""
                display = f"{meta.label or 'ka9q'} ({addr})"
                suggestions.append((display, addr))

        def _after(rx: Optional[WdReceiver]) -> None:
            if rx is None:
                return
            self._config.receivers[rx.name] = rx
            self._dirty = True
            self._refresh_view()

        self.app.push_screen(AddReceiverModal(sdr_suggestions=suggestions), _after)

    def _remove_selected(self) -> None:
        table = self.query_one("#wd-rx-table", DataTable)
        idx = table.cursor_row
        rx_names = list(self._config.receivers.keys())
        if idx < 0 or idx >= len(rx_names):
            return
        name = rx_names[idx]
        self._config.receivers.pop(name, None)
        self._dirty = True
        self._refresh_view()
