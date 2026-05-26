"""Radiod status screen — three-stage drill-down (LAN → radiod → SSRC).

Stage 1 — LAN-wide radiod discovery
  On mount, calls ``ka9q.discovery.discover_radiod_services()`` to
  enumerate every radiod publishing on the LAN.  The top-of-screen
  Select widget lists them as ``"<name>  ({hostname})"``; the
  initial pick is whatever the caller passed in (the "default"
  radiod from coordination.toml) or the first discovered entry.
  "Refresh radiods" re-runs discovery without remounting.

Stage 2 — main view (channels list)
  Once a radiod is selected, queries it via
  ``discover_channels(status, 10s)`` and ``RadiodControl.poll_status``
  to render the Frontend table and the Active Channels list.
  Frontend keys are the live ka9q-python ones (rf_gain / rf_atten /
  if_power / fe edges / calibrate / ad_over / samples_since_over /
  input_samprate / description); older dead keys (reference / lock /
  mixer_gain / if_gain / lna_gain) were dropped.

Stage 3 — deep dive (per-SSRC)
  Select a channel row + "Deep dive" → in-screen get/set panel for
  that one SSRC: full poll_status read-out + editable Apply
  controls for tuning, filter, gain/AGC, squelch, output, lifetime,
  description.  Each Apply only fires the setters whose value
  actually changed against the baseline poll_status snapshot.
  Double-click a row → external ``ka9q tui --ssrc <N>`` shortcut.

Read paths use ``ka9q-python``'s public API
(``discover_radiod_services`` / ``discover_channels`` /
``RadiodControl.poll_status``).  Write paths use the matching
``set_*`` methods on ``RadiodControl``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Select, Static, Switch
from textual.worker import Worker, WorkerState


# Window (seconds) during which two RowSelected events on the same
# SSRC count as a "double-click" and trigger the external `ka9q tui`
# launcher.  Larger than a typical OS double-click threshold so the
# operator has room over SSH where round-trip latency adds jitter.
_DOUBLE_SELECT_WINDOW_S = 0.6


def _find_tool(name: str) -> Optional[str]:
    """Locate a venv-installed CLI tool.  Checks sys.executable's
    sibling first (sigmond's own venv), then $PATH."""
    venv_bin = Path(sys.executable).parent / name
    if venv_bin.is_file():
        return str(venv_bin)
    return shutil.which(name)


# ---------------------------------------------------------------------------
# Frontend table — fields we surface from the FIRST channel's poll_status
# `frontend` dict.  Each tuple is (key, display label, optional formatter).
# Updated 2026-05-26 against ka9q-python's live output on bee1; keys that
# weren't populated for the rx888 ("reference", "lock", "mixer_gain",
# "if_gain", "lna_gain") were dropped.  If a future front-end populates
# them again, just add them back here.
# ---------------------------------------------------------------------------

def _fmt_db(v: Any) -> str:
    if v is None: return "—"
    try: return f"{float(v):+.1f} dB"
    except (TypeError, ValueError): return str(v)

def _fmt_hz(v: Any) -> str:
    if v is None: return "—"
    try: return f"{float(v) / 1e6:.6f} MHz"
    except (TypeError, ValueError): return str(v)

def _fmt_int(v: Any) -> str:
    if v is None: return "—"
    try: return f"{int(v):,}"
    except (TypeError, ValueError): return str(v)

def _fmt_str(v: Any) -> str:
    if v is None: return "—"
    return str(v)

def _fmt_bool(v: Any) -> str:
    if v is None: return "—"
    return "yes" if v else "no"

_FRONTEND_FIELDS = [
    ("description",       "Description",            _fmt_str),
    ("input_samprate",    "Input sample rate",      _fmt_int),
    ("fe_low_edge",       "Front-end low edge",     _fmt_hz),
    ("fe_high_edge",      "Front-end high edge",    _fmt_hz),
    ("rf_agc",            "RF AGC",                 _fmt_bool),
    ("rf_gain",           "RF gain",                _fmt_db),
    ("rf_atten",          "RF attenuator",          _fmt_db),
    ("rf_level_cal",      "RF level cal",           _fmt_db),
    ("if_power",          "IF power",               _fmt_db),
    ("calibrate",         "Calibration (ppm)",      _fmt_str),
    ("ad_over",           "A/D overrange events",   _fmt_int),
    ("samples_since_over","Samples since over",     _fmt_int),
]


# ---------------------------------------------------------------------------
# Encoding map for the output_encoding Select.  Mirrors ka9q-python's
# Encoding enum (S16LE=1, S16BE=2, OPUS=3, F32=4, AX25=5, F32BE=8).
# ---------------------------------------------------------------------------

_ENCODING_OPTIONS = [
    ("s16le", "1"),
    ("s16be", "2"),
    ("opus",  "3"),
    ("f32",   "4"),
    ("ax25",  "5"),
    ("f32be", "8"),
]
_ENCODING_INT_TO_NAME = {int(v): label for label, v in _ENCODING_OPTIONS}


class RadiodScreen(Vertical):
    """Coordinator-level radiod status + per-SSRC deep-dive."""

    DEFAULT_CSS = """
    RadiodScreen {
        padding: 1;
    }
    RadiodScreen .section-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    RadiodScreen #radiod-status {
        margin-top: 1;
        color: $text-muted;
    }
    RadiodScreen #radiod-selector-row {
        height: 3;
        margin-bottom: 1;
    }
    RadiodScreen #radiod-selector {
        width: 60;
        margin-right: 2;
    }
    RadiodScreen #radiod-addr {
        color: $text-muted;
    }
    RadiodScreen #radiod-main-buttons {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }
    RadiodScreen #radiod-main-buttons Button,
    RadiodScreen #dd-buttons Button {
        margin-right: 1;
    }
    /* Constrain the channels DataTable so the buttons row above
       stays visible at all terminal sizes once the table is full. */
    RadiodScreen #radiod-channels {
        height: 1fr;
        min-height: 12;
    }
    RadiodScreen .dd-row {
        height: 3;
        margin-bottom: 1;
    }
    RadiodScreen .dd-row Input,
    RadiodScreen .dd-row Select {
        width: 16;
        margin-right: 1;
    }
    RadiodScreen .dd-row Static {
        width: 22;
        padding-top: 1;
    }
    RadiodScreen .dd-section {
        text-style: bold;
        margin-top: 1;
    }
    RadiodScreen #dd-readout {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, radiod_id: str = "", status_dns: str = "",
                 **kwargs) -> None:
        super().__init__(**kwargs)
        # Caller-supplied "preferred" radiod (typically from
        # coordination.toml) — used as the initial Select value if it
        # appears in LAN discovery.  Empty string = "pick the first
        # discovered radiod".
        self._initial_radiod_id = radiod_id
        self._initial_status_dns = status_dns
        # Currently-selected radiod — populated by Stage 1 discovery
        # (or pre-seeded from the constructor args before the first
        # Stage 2 query fires).
        self._radiod_id = radiod_id
        self._status_dns = status_dns
        # Cached `discover_radiod_services()` result so the Select
        # widget can map ``hostname → friendly name`` after a pick.
        self._discovered: list[dict] = []
        # Current deep-dive SSRC (None when in main view).
        self._dd_ssrc: Optional[int] = None
        # Last poll_status payload for the current deep-dive SSRC,
        # used to detect "what changed" on Apply so we only call the
        # setters whose value actually moved.
        self._dd_baseline: dict = {}
        # Double-select tracking — two RowSelected events on the
        # same channels-table row within _DOUBLE_SELECT_WINDOW_S
        # launch ka9q-python's external TUI focused on that SSRC.
        self._last_select_ts: float = 0.0
        self._last_select_ssrc: Optional[int] = None

    # ------------------------------------------------------------------
    # compose
    # ------------------------------------------------------------------

    def compose(self):
        # Stage 1 — radiod selector, populated by LAN-wide mDNS
        # discovery on mount.  Stays visible at the top of the
        # screen across stage transitions so the operator can pivot
        # between radiods without backing out of the deep dive.
        yield Static("Radiods on the LAN", classes="section-title")
        with Horizontal(id="radiod-selector-row"):
            yield Select(
                [("(discovering…)", "")], value="",
                id="radiod-selector", allow_blank=False,
            )
            yield Button("Refresh radiods", id="radiod-rediscover",
                         variant="default")
        yield Static("", id="radiod-addr")

        # Main view (channels list) and deep-dive view (per-SSRC
        # get/set) share the screen via a ContentSwitcher.  Both
        # views are mounted up-front so widget queries always
        # resolve; the switcher just toggles which one is visible.
        with ContentSwitcher(initial="main-view", id="radiod-switcher"):
            with Vertical(id="main-view"):
                yield Static("Frontend", classes="section-title")
                frontend = DataTable(id="radiod-frontend")
                frontend.add_columns("Parameter", "Value")
                yield frontend

                yield Static(
                    "Active Channels  [dim](select a row + 'Deep dive' "
                    "→ in-screen get/set panel; double-click a row "
                    "→ launch external ka9q TUI on that SSRC)[/]",
                    classes="section-title", markup=True,
                )
                # Buttons go ABOVE the channels table so they stay
                # visible after the table fills with 60+ rows post-
                # poll.  Putting them after the DataTable pushed
                # them off-screen as soon as the operator had real
                # data — exactly when the operator wanted to click
                # Deep dive.
                with Horizontal(id="radiod-main-buttons"):
                    yield Button("Deep dive", id="radiod-deep-dive",
                                 variant="primary")
                    yield Button("Refresh", id="radiod-refresh",
                                 variant="default")
                channels = DataTable(
                    id="radiod-channels", cursor_type="row",
                    zebra_stripes=True,
                )
                channels.add_columns(
                    "SSRC", "Frequency (MHz)", "Preset", "Sample Rate",
                    "Encoding", "SNR (dB)",
                )
                yield channels

            with Vertical(id="deep-dive-view"):
                yield Static("Deep dive — (select an SSRC first)",
                             id="dd-title", classes="section-title")

                yield Static("Tuning", classes="dd-section")
                with Horizontal(classes="dd-row"):
                    yield Static("Frequency (Hz):")
                    yield Input(id="dd-frequency")
                    yield Static("Preset:")
                    yield Input(id="dd-preset")
                    yield Static("Sample rate:")
                    yield Input(id="dd-sample-rate")
                    yield Button("Apply", id="dd-apply-tuning",
                                 variant="primary")

                yield Static("Filter", classes="dd-section")
                with Horizontal(classes="dd-row"):
                    yield Static("Low edge (Hz):")
                    yield Input(id="dd-low-edge")
                    yield Static("High edge (Hz):")
                    yield Input(id="dd-high-edge")
                    yield Button("Apply", id="dd-apply-filter",
                                 variant="primary")

                yield Static("Gain / AGC / Squelch",
                             classes="dd-section")
                with Horizontal(classes="dd-row"):
                    yield Static("Gain (dB):")
                    yield Input(id="dd-gain")
                    yield Static("AGC:")
                    yield Switch(value=False, id="dd-agc")
                    yield Static("Squelch open/close (dB):")
                    yield Input(id="dd-squelch-open")
                    yield Input(id="dd-squelch-close")
                    yield Button("Apply", id="dd-apply-gain",
                                 variant="primary")

                yield Static("Output", classes="dd-section")
                with Horizontal(classes="dd-row"):
                    yield Static("Encoding:")
                    yield Select(_ENCODING_OPTIONS, value="4",
                                 id="dd-encoding", allow_blank=False)
                    yield Static("Lifetime (frames):")
                    yield Input(id="dd-lifetime")
                    yield Static("Description:")
                    yield Input(id="dd-description")
                    yield Button("Apply", id="dd-apply-output",
                                 variant="primary")

                yield Static("Live stats", classes="dd-section")
                yield Static("", id="dd-readout", markup=True)

                with Horizontal(id="dd-buttons"):
                    yield Button("Refresh", id="dd-refresh",
                                 variant="default")
                    yield Button("◀ Back", id="dd-back", variant="warning")

        yield Static("", id="radiod-status")

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        # Stage 1 — LAN-wide radiod discovery first.  Once it returns
        # we either auto-select the caller-supplied radiod (if it
        # shows up in discovery) or the first entry, then fire the
        # Stage 2 channels poll for that radiod.
        self.query_one("#radiod-status", Static).update(
            "[dim]Discovering radiods on the LAN…[/]")
        self.run_worker(self._fetch_radiods, thread=True,
                        group="rd-discover")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "radiod-deep-dive":
            self._enter_deep_dive()
        elif bid == "radiod-refresh":
            self._poll_radiod()
        elif bid == "radiod-rediscover":
            self.query_one("#radiod-status", Static).update(
                "[dim]Re-discovering radiods…[/]")
            self.run_worker(self._fetch_radiods, thread=True,
                            group="rd-discover")
        elif bid == "dd-refresh":
            self._refresh_deep_dive()
        elif bid == "dd-back":
            self._exit_deep_dive()
        elif bid == "dd-apply-tuning":
            self._apply_tuning()
        elif bid == "dd-apply-filter":
            self._apply_filter()
        elif bid == "dd-apply-gain":
            self._apply_gain()
        elif bid == "dd-apply-output":
            self._apply_output()

    def on_select_changed(self, event: Select.Changed) -> None:
        """When the operator picks a different radiod from the Stage 1
        selector, re-run the Stage 2 channels poll against that
        radiod.  Other Select widgets (`#dd-encoding`) ignore."""
        if event.select.id != "radiod-selector":
            return
        new_dns = str(event.value) if event.value is not None else ""
        if not new_dns or new_dns == self._status_dns:
            return
        # Promote the new selection.
        self._status_dns = new_dns
        # Find the friendly name + id from the cached discovery
        # result; fall back to the hostname if we can't find a match.
        match = next((r for r in self._discovered
                      if r.get("hostname") == new_dns), None)
        self._radiod_id = (match.get("name") if match else new_dns) or new_dns
        # If we were in the deep-dive view for the old radiod, drop
        # back to the channels list — an SSRC from one radiod is
        # meaningless on another.
        if self._dd_ssrc is not None:
            self._exit_deep_dive()
        self._render_radiod_addr_line()
        self._poll_radiod()

    # ------------------------------------------------------------------
    # main view — radiod-wide poll
    # ------------------------------------------------------------------

    def _fetch_radiods(self) -> dict:
        """Worker — enumerate radiods broadcasting on the LAN."""
        try:
            from ka9q.discovery import discover_radiod_services
        except ImportError:
            return {"error": "ka9q-python not installed in sigmond's venv"}
        try:
            results = discover_radiod_services(timeout=5.0) or []
        except Exception as exc:
            return {"error": f"discover_radiod_services: {exc}"}
        return {"radiods": list(results)}

    def _render_radiods(self, data: dict) -> None:
        if "error" in data:
            self.query_one("#radiod-status", Static).update(
                f"[red]{data['error']}[/]")
            return
        radiods = data.get("radiods") or []
        self._discovered = radiods
        selector = self.query_one("#radiod-selector", Select)
        if not radiods:
            selector.set_options([("(no radiods on the LAN)", "")])
            selector.value = ""
            self.query_one("#radiod-status", Static).update(
                "[yellow]No radiods broadcasting on the LAN.  Plug in / "
                "start radiod and click 'Refresh radiods'.[/]")
            return
        # Build (label, hostname) tuples; hostname is the
        # Stage-2 query target.
        options = [
            (f"{r.get('name', '?')}  ({r.get('hostname', '?')})",
             r.get('hostname', ''))
            for r in radiods
            if r.get('hostname')
        ]
        selector.set_options(options)
        # Pick the caller-supplied preference if it's in the list;
        # otherwise the first discovered radiod.
        wanted = self._initial_status_dns
        chosen = next((opt for _label, opt in options if opt == wanted),
                      options[0][1])
        # Update internal state FIRST so the SelectChanged handler
        # fired by the next assignment sees no change and skips its
        # own Stage 2 dispatch (otherwise we'd start two polls).
        self._status_dns = chosen
        match = next((r for r in radiods if r.get("hostname") == chosen),
                     None)
        if match:
            self._radiod_id = match.get("name", chosen)
        selector.value = chosen
        self._render_radiod_addr_line()
        self.query_one("#radiod-status", Static).update(
            f"[green]{len(radiods)} radiod(s) on the LAN[/]")
        # Kick off Stage 2 for the newly-selected radiod.
        self._poll_radiod()

    def _render_radiod_addr_line(self) -> None:
        """Show '<friendly name>  •  status: <hostname>' under the
        Select so the operator sees what they're querying."""
        match = next(
            (r for r in self._discovered
             if r.get("hostname") == self._status_dns), None)
        if match:
            addr = match.get("address", "?")
            port = match.get("port", "?")
            txt = (f"  •  status: {self._status_dns}"
                   f"  ({addr}:{port})")
        else:
            txt = (f"  •  status: {self._status_dns or '(none)'}")
        self.query_one("#radiod-addr", Static).update(
            f"[bold]{self._radiod_id or '(unknown radiod)'}[/]{txt}",
        )

    def _poll_radiod(self) -> None:
        if not self._status_dns:
            self.query_one("#radiod-status", Static).update(
                "[yellow]No radiod selected[/]")
            return
        self.query_one("#radiod-status", Static).update(
            f"[dim]Querying {self._radiod_id or self._status_dns}…[/]")
        self.run_worker(self._fetch_status, thread=True, group="rd-main")

    def _fetch_status(self) -> dict:
        try:
            from ka9q import RadiodControl, discover_channels
        except ImportError:
            return {"error": "ka9q-python not installed in sigmond's venv"}

        result: dict = {"channels": [], "frontend": {}}
        try:
            channel_dict = discover_channels(self._status_dns,
                                             listen_duration=10.0)
            for ssrc, ch in channel_dict.items():
                result["channels"].append({
                    "ssrc": int(ssrc),
                    "frequency": getattr(ch, "frequency", 0.0),
                    "preset":    getattr(ch, "preset", "?"),
                    "sample_rate": int(getattr(ch, "sample_rate", 0) or 0),
                    "encoding":  getattr(ch, "encoding", None),
                    "snr":       getattr(ch, "snr", None),
                })
        except Exception as exc:
            result["error"] = f"discover_channels: {exc}"
            return result

        # Frontend metadata rides inside each channel's status payload.
        # Pick the first SSRC; one poll yields the rx888-side state we
        # care about.
        try:
            with RadiodControl(self._status_dns) as control:
                if result["channels"]:
                    ssrc = result["channels"][0]["ssrc"]
                    status = control.poll_status(ssrc, timeout=2.0)
                    if status:
                        d = status.to_dict()
                        result["frontend"] = d.get("frontend", {}) or {}
        except Exception as exc:
            result["frontend_error"] = str(exc)

        return result

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        data = event.worker.result or {}
        if not isinstance(data, dict):
            return
        if event.worker.group == "rd-discover":
            self._render_radiods(data)
        elif event.worker.group == "rd-main":
            self._render_main(data)
        elif event.worker.group == "rd-deep":
            self._render_deep_dive(data)
        elif event.worker.group == "rd-apply":
            self._on_apply_result(data)

    def _render_main(self, result: dict) -> None:
        status_widget = self.query_one("#radiod-status", Static)
        if "error" in result:
            status_widget.update(f"[red]{result['error']}[/]")
            return

        # Frontend table — today's keys.
        fe_table = self.query_one("#radiod-frontend", DataTable)
        fe_table.clear()
        fe = result.get("frontend", {}) or {}
        if fe:
            for key, label, fmt in _FRONTEND_FIELDS:
                fe_table.add_row(label, fmt(fe.get(key)))
        elif "frontend_error" in result:
            fe_table.add_row("Error", result["frontend_error"])
        else:
            fe_table.add_row("Status", "No frontend data available")

        # Channels table.
        ch_table = self.query_one("#radiod-channels", DataTable)
        ch_table.clear()
        channels = result.get("channels", [])
        for ch in sorted(channels, key=lambda c: c.get("frequency", 0)):
            freq = ch.get("frequency") or 0
            freq_mhz = f"{freq / 1e6:.6f}" if freq else "?"
            snr = ch.get("snr")
            snr_s = f"{float(snr):.1f}" if isinstance(snr, (int, float)) and snr != float("-inf") else "—"
            enc = ch.get("encoding")
            enc_s = _ENCODING_INT_TO_NAME.get(int(enc), str(enc)) if enc is not None else "?"
            ch_table.add_row(
                str(ch.get("ssrc", "?")),
                freq_mhz,
                ch.get("preset", "?"),
                str(ch.get("sample_rate", "?")),
                enc_s,
                snr_s,
            )

        n = len(channels)
        fe_note = " (frontend query failed)" if "frontend_error" in result else ""
        status_widget.update(
            f"[green]{n} active channel{'s' if n != 1 else ''}{fe_note}[/]"
        )

    # ------------------------------------------------------------------
    # deep-dive view
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # double-select → launch external ka9q TUI on that SSRC
    # ------------------------------------------------------------------

    def on_data_table_row_selected(
            self, event: DataTable.RowSelected) -> None:
        """Two RowSelected events on the same row within
        _DOUBLE_SELECT_WINDOW_S launch the external `ka9q tui`
        focused on that SSRC.

        Textual fires RowSelected on Enter and on click-when-already-
        highlighted.  Single Enter or first click just moves the
        cursor (RowHighlighted) — the operator has to act twice to
        trigger this path.  The in-screen Deep-dive panel remains
        the primary access via the button; this is the "I want the
        full ka9q-python tool with its own keybindings" shortcut.
        """
        if event.data_table.id != "radiod-channels":
            return
        try:
            row = event.data_table.get_row(event.row_key)
            ssrc = int(row[0]) if row else None
        except (TypeError, ValueError, KeyError):
            return
        if ssrc is None:
            return

        now = time.monotonic()
        within_window = (now - self._last_select_ts) <= _DOUBLE_SELECT_WINDOW_S
        same_row = (self._last_select_ssrc == ssrc)
        if within_window and same_row:
            # Reset so a third event doesn't relaunch immediately.
            self._last_select_ts = 0.0
            self._last_select_ssrc = None
            self._launch_ka9q_tui(ssrc)
        else:
            self._last_select_ts = now
            self._last_select_ssrc = ssrc

    def _launch_ka9q_tui(self, ssrc: int) -> None:
        """Suspend sigmond's TUI and run `ka9q tui <status> --ssrc N`.

        Restores sigmond's TUI on exit.  Surfaces non-zero exit
        codes (e.g. ka9q missing from the venv) in the radiod-status
        widget; the in-screen Deep-dive panel remains the primary
        path either way.
        """
        status_widget = self.query_one("#radiod-status", Static)
        ka9q_bin = _find_tool("ka9q")
        if not ka9q_bin:
            status_widget.update(
                "[red]ka9q binary not found in sigmond's venv — "
                "use the in-screen Deep-dive panel instead.[/]")
            return
        if not self._status_dns:
            status_widget.update(
                "[yellow]Cannot launch ka9q TUI — no status_dns "
                "configured[/]")
            return
        cmd = [ka9q_bin, "tui", self._status_dns, "--ssrc", str(ssrc)]
        try:
            with self.app.suspend():
                result = subprocess.run(cmd)
        except Exception as exc:
            status_widget.update(
                f"[red]launch ka9q tui failed: {exc}[/]")
            return
        if result.returncode != 0:
            status_widget.update(
                f"[red]ka9q tui exited {result.returncode} "
                f"(cmd: {' '.join(cmd)})[/]")
        else:
            status_widget.update(
                f"[green]ka9q tui closed cleanly for ssrc={ssrc}[/]")

    def _selected_ssrc(self) -> Optional[int]:
        table = self.query_one("#radiod-channels", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
            row = table.get_row(key)
        except Exception:
            return None
        try:
            return int(row[0]) if row else None
        except (TypeError, ValueError):
            return None

    def _enter_deep_dive(self) -> None:
        ssrc = self._selected_ssrc()
        if ssrc is None:
            self.query_one("#radiod-status", Static).update(
                "[yellow]Select a channel row first, then Deep dive[/]")
            return
        self._dd_ssrc = ssrc
        self.query_one("#radiod-switcher", ContentSwitcher).current = "deep-dive-view"
        self._refresh_deep_dive()

    def _exit_deep_dive(self) -> None:
        self._dd_ssrc = None
        self._dd_baseline = {}
        self.query_one("#radiod-switcher", ContentSwitcher).current = "main-view"

    def _refresh_deep_dive(self) -> None:
        if self._dd_ssrc is None:
            return
        self.query_one("#dd-readout", Static).update("[dim]polling…[/]")
        ssrc = self._dd_ssrc
        self.run_worker(
            lambda: self._fetch_one(ssrc),
            thread=True, group="rd-deep", exclusive=True,
        )

    def _fetch_one(self, ssrc: int) -> dict:
        try:
            from ka9q import RadiodControl
        except ImportError:
            return {"error": "ka9q-python not installed"}
        try:
            with RadiodControl(self._status_dns) as control:
                status = control.poll_status(ssrc, timeout=2.0)
        except Exception as exc:
            return {"error": f"poll_status: {exc}"}
        if status is None:
            return {"error": f"no status for ssrc={ssrc}"}
        d = status.to_dict()
        d["_ssrc"] = ssrc
        return d

    def _render_deep_dive(self, data: dict) -> None:
        if "error" in data:
            self.query_one("#dd-readout", Static).update(f"[red]{data['error']}[/]")
            return
        ssrc = data.get("_ssrc")
        freq = data.get("frequency")
        preset = data.get("preset", "?")
        freq_str = f"{float(freq) / 1e6:.6f} MHz" if freq else "?"
        self.query_one("#dd-title", Static).update(
            f"Deep dive — ssrc={ssrc} ({freq_str} {preset})")

        self._dd_baseline = data
        self._set_input("#dd-frequency", freq)
        self._set_input("#dd-preset", preset)
        self._set_input("#dd-sample-rate", data.get("output_samprate"))
        self._set_input("#dd-low-edge", data.get("low_edge"))
        self._set_input("#dd-high-edge", data.get("high_edge"))
        self._set_input("#dd-gain", data.get("gain"))
        agc_sw = self.query_one("#dd-agc", Switch)
        agc_sw.value = bool(data.get("agc_enable"))
        self._set_input("#dd-squelch-open", data.get("squelch_open"))
        self._set_input("#dd-squelch-close", data.get("squelch_close"))
        enc = data.get("output_encoding")
        if enc is not None:
            self.query_one("#dd-encoding", Select).value = str(int(enc))
        self._set_input("#dd-lifetime", data.get("lifetime"))
        self._set_input("#dd-description", data.get("description"))

        # Live stats — read-only stuff worth seeing at a glance.
        bp = data.get("baseband_power")
        nd = data.get("noise_density")
        snr = data.get("pll", {}).get("snr") if isinstance(data.get("pll"), dict) else None
        readout_lines = [
            f"Output packets: {_fmt_int(data.get('output_data_packets'))}  "
            f"errors: {_fmt_int(data.get('output_errors'))}  "
            f"filter drops: {_fmt_int(data.get('filter_drops'))}",
            f"Baseband: {_fmt_db(bp)}   "
            f"Noise density: {_fmt_db(nd)} /Hz   "
            f"PLL SNR: {_fmt_db(snr)}",
            f"Output: {data.get('output_data_dest_socket', {}).get('address', '?')}:"
            f"{data.get('output_data_dest_socket', {}).get('port', '?')}  "
            f"TTL={data.get('output_ttl', '?')}  "
            f"packet type={data.get('rtp_pt', '?')}",
        ]
        self.query_one("#dd-readout", Static).update("\n".join(readout_lines))

    def _set_input(self, sel: str, value: Any) -> None:
        widget = self.query_one(sel, Input)
        widget.value = "" if value is None else str(value)

    def _get_input(self, sel: str) -> str:
        return self.query_one(sel, Input).value.strip()

    # ------------------------------------------------------------------
    # Apply handlers — call RadiodControl setters in a worker thread.
    # ------------------------------------------------------------------

    def _spawn_apply(self, label: str, fn) -> None:
        """Run `fn(control)` in a thread; surface the outcome in
        #dd-readout."""
        self.query_one("#dd-readout", Static).update(
            f"[dim]applying {label}…[/]")
        ssrc = self._dd_ssrc

        def _job() -> dict:
            try:
                from ka9q import RadiodControl
                with RadiodControl(self._status_dns) as control:
                    fn(control, ssrc)
            except Exception as exc:
                return {"error": f"{label}: {exc}"}
            return {"_apply_label": label}

        self.run_worker(_job, thread=True, group="rd-apply", exclusive=True)

    def _on_apply_result(self, data: dict) -> None:
        if "error" in data:
            self.query_one("#dd-readout", Static).update(f"[red]{data['error']}[/]")
            return
        label = data.get("_apply_label", "?")
        self.query_one("#dd-readout", Static).update(
            f"[green]applied {label}; re-reading…[/]")
        # Re-poll so the operator sees the post-apply state.
        self._refresh_deep_dive()

    def _apply_tuning(self) -> None:
        try:
            freq_hz = float(self._get_input("#dd-frequency"))
            preset = self._get_input("#dd-preset") or None
            rate_s = self._get_input("#dd-sample-rate")
            rate = int(rate_s) if rate_s else None
        except ValueError as exc:
            self.query_one("#dd-readout", Static).update(f"[red]tuning: {exc}[/]")
            return
        base = self._dd_baseline

        def _fn(control, ssrc):
            if freq_hz != float(base.get("frequency") or 0.0):
                control.set_frequency(ssrc=ssrc, frequency_hz=freq_hz)
            if preset and preset != str(base.get("preset", "")):
                control.set_preset(ssrc=ssrc, preset=preset)
            if rate is not None and rate != int(base.get("output_samprate") or 0):
                control.set_sample_rate(ssrc=ssrc, sample_rate=rate)
        self._spawn_apply("tuning", _fn)

    def _apply_filter(self) -> None:
        try:
            low = float(self._get_input("#dd-low-edge"))
            high = float(self._get_input("#dd-high-edge"))
        except ValueError as exc:
            self.query_one("#dd-readout", Static).update(f"[red]filter: {exc}[/]")
            return
        base = self._dd_baseline
        unchanged = (low == float(base.get("low_edge") or 0.0)
                     and high == float(base.get("high_edge") or 0.0))
        if unchanged:
            self.query_one("#dd-readout", Static).update(
                "[dim]filter: no change[/]")
            return

        def _fn(control, ssrc):
            control.set_filter(ssrc=ssrc, low_edge=low, high_edge=high)
        self._spawn_apply("filter", _fn)

    def _apply_gain(self) -> None:
        try:
            gain = float(self._get_input("#dd-gain"))
            agc_on = bool(self.query_one("#dd-agc", Switch).value)
            sq_open = float(self._get_input("#dd-squelch-open"))
            sq_close = float(self._get_input("#dd-squelch-close"))
        except ValueError as exc:
            self.query_one("#dd-readout", Static).update(f"[red]gain: {exc}[/]")
            return
        base = self._dd_baseline

        def _fn(control, ssrc):
            if gain != float(base.get("gain") or 0.0):
                control.set_gain(ssrc=ssrc, gain=gain)
            if agc_on != bool(base.get("agc_enable")):
                control.set_agc(ssrc=ssrc, enable=1 if agc_on else 0)
            old_o = float(base.get("squelch_open") or 0.0)
            old_c = float(base.get("squelch_close") or 0.0)
            if sq_open != old_o or sq_close != old_c:
                control.set_squelch(ssrc=ssrc, open_db=sq_open,
                                    close_db=sq_close)
        self._spawn_apply("gain/AGC/squelch", _fn)

    def _apply_output(self) -> None:
        try:
            enc = int(self.query_one("#dd-encoding", Select).value)
            lt_s = self._get_input("#dd-lifetime")
            lifetime = int(lt_s) if lt_s else None
            desc = self._get_input("#dd-description")
        except (TypeError, ValueError) as exc:
            self.query_one("#dd-readout", Static).update(f"[red]output: {exc}[/]")
            return
        base = self._dd_baseline

        def _fn(control, ssrc):
            if enc != int(base.get("output_encoding") or 0):
                control.set_output_encoding(ssrc=ssrc, encoding=enc)
            if lifetime is not None and lifetime != (base.get("lifetime") or 0):
                control.set_channel_lifetime(ssrc=ssrc, lifetime=lifetime)
            if desc and desc != str(base.get("description", "")):
                control.set_description(ssrc=ssrc, description=desc)
        self._spawn_apply("output", _fn)
