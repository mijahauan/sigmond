"""Timing screen — live chrony-source comparison with TSL3 as reference.

The default ``chronyc sources`` view shows every offset relative to the
*system clock*, which forces mental subtraction to compare two sources.
On bee1 the natural reference is TSL3 (BPSK PPS, sigma ~55 µs at 96 kHz
matched-filter), so this screen pivots: TSL3 = 0, every other source
shows Δ-from-TSL3 with a 60-sample Unicode sparkline so transient
events (e.g. Costas-loop excursions) are visible at a glance instead of
having to grep journals.

Header shows ``chronyc tracking`` output framed for the question that
actually matters when reasoning about timestamp confidence: where does
chrony think the kernel clock currently sits relative to UTC, and what
is the conservative bound (root dispersion) on that estimate?

Refresh: ``set_interval(1.0)`` — light enough that the running cost is
negligible, fast enough to track the ~13-s Costas excursions visible in
the underlying calibrator.
"""

from __future__ import annotations

import csv
import io
import subprocess
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

from textual.containers import Vertical
from textual.widgets import DataTable, Static


# Refresh cadence and history depth.  60 samples at 1 Hz = the last
# minute of trace per source, which spans a full Costas excursion plus
# margin on each side.
POLL_SEC = 1.0
HISTORY = 60

# Unicode "block elements" for the sparkline, low → high.
SPARKS = "▁▂▃▄▅▆▇█"


@dataclass
class SourceRow:
    """One parsed row from ``chronyc -c sources``."""
    mode: str
    state: str
    name: str
    stratum: int
    poll: int
    reach: int
    last_rx_sec: float
    last_offset_sec: float          # adjusted offset (ch's combined view)
    measured_offset_sec: float      # raw measurement
    sample_error_sec: float         # 1-sigma estimate


@dataclass
class TrackingRow:
    """``chronyc -c tracking`` summary."""
    ref_id_hex: str
    ref_id_name: str
    stratum: int
    last_offset_sec: float
    rms_offset_sec: float
    root_delay_sec: float
    root_dispersion_sec: float
    leap_status: str


def _run_chronyc(args: List[str]) -> Optional[str]:
    """Run chronyc in CSV mode.  Returns stdout on success, None on
    any failure (chrony down, command not found, timeout)."""
    try:
        proc = subprocess.run(
            ['chronyc', '-c'] + args,
            capture_output=True, text=True, timeout=2.0,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def parse_sources(csv_text: str) -> List[SourceRow]:
    """Parse the CSV output of ``chronyc -c sources``.  Format (10
    columns): mode, state, name, stratum, poll, reach, last_rx_sec,
    last_adjusted, last_measured, sample_error.  Skips malformed rows
    rather than raising — a partial display is more useful than a
    crash when chrony emits an unexpected source line."""
    rows: List[SourceRow] = []
    if not csv_text:
        return rows
    reader = csv.reader(io.StringIO(csv_text))
    for parts in reader:
        if len(parts) < 10:
            continue
        try:
            rows.append(SourceRow(
                mode=parts[0],
                state=parts[1],
                name=parts[2],
                stratum=int(parts[3]),
                poll=int(parts[4]),
                reach=int(parts[5]),
                last_rx_sec=float(parts[6]),
                last_offset_sec=float(parts[7]),
                measured_offset_sec=float(parts[8]),
                sample_error_sec=float(parts[9]),
            ))
        except (ValueError, IndexError):
            continue
    return rows


def parse_tracking(csv_text: str) -> Optional[TrackingRow]:
    """Parse the CSV output of ``chronyc -c tracking``.  Format (14
    columns): ref_id_hex, ref_id_name, stratum, ref_time, system_time,
    last_offset, rms_offset, frequency, residual_freq, skew,
    root_delay, root_dispersion, update_interval, leap_status."""
    if not csv_text or not csv_text.strip():
        return None
    parts = next(csv.reader(io.StringIO(csv_text)), None)
    if not parts or len(parts) < 14:
        return None
    try:
        return TrackingRow(
            ref_id_hex=parts[0],
            ref_id_name=parts[1],
            stratum=int(parts[2]),
            last_offset_sec=float(parts[5]),
            rms_offset_sec=float(parts[6]),
            root_delay_sec=float(parts[10]),
            root_dispersion_sec=float(parts[11]),
            leap_status=parts[13],
        )
    except (ValueError, IndexError):
        return None


def format_offset(seconds: float) -> str:
    """Auto-scale a duration into the most natural unit (ns / µs / ms /
    s).  Includes an explicit sign so trace lines stay aligned in
    width regardless of polarity."""
    sign = '+' if seconds >= 0 else '-'
    abs_s = abs(seconds)
    if abs_s < 1e-6:
        return f"{sign}{abs_s * 1e9:.1f} ns"
    if abs_s < 1e-3:
        return f"{sign}{abs_s * 1e6:.2f} µs"
    if abs_s < 1.0:
        return f"{sign}{abs_s * 1e3:.2f} ms"
    return f"{sign}{abs_s:.3f} s"


def format_age(seconds: float) -> str:
    """Format last-sample age.  Negative or zero treated as 'now'."""
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m{int(seconds) % 60:02d}"
    return f"{int(seconds / 3600)}h{int((seconds % 3600) / 60):02d}"


def format_reach(reach: int) -> str:
    """Reach as 'N/8' (popcount of the 8-bit register).  This is more
    intuitive than the raw decimal/octal value — N/8 immediately tells
    you fraction of recent polls that succeeded."""
    if reach < 0 or reach > 255:
        return "?"
    return f"{bin(reach).count('1')}/8"


def sparkline(values: List[float], width: int = HISTORY) -> str:
    """Render a list of floats as a Unicode sparkline.  Pads with
    leading spaces so a partial-history source still renders aligned
    with full-history ones.  Auto-scales to the actual range so a flat
    series and a noisy one both use the full vertical resolution."""
    if not values:
        return ' ' * width
    pad = max(0, width - len(values))
    use = list(values)[-width:]
    lo = min(use)
    hi = max(use)
    span = hi - lo
    if span <= 0:
        # Flat trace — render at the middle band so it's visible.
        bar = SPARKS[len(SPARKS) // 2] * len(use)
    else:
        bar = ''.join(
            SPARKS[
                max(0, min(len(SPARKS) - 1,
                           int((v - lo) / span * (len(SPARKS) - 1))))
            ]
            for v in use
        )
    return (' ' * pad) + bar


def _row_color(name: str, delta_sec: float) -> str:
    """Colour-code a row's name by how close it is to TSL3.  TSL3
    itself is bold (it's the reference); everything else gets graded
    green/yellow/red against thresholds chosen for stratum-1 NTP
    quality vs. publicly-routed ms-jitter sources."""
    if name == 'TSL3':
        return f"[bold]{name}[/]"
    abs_s = abs(delta_sec)
    if abs_s < 10e-6:           # <10 µs — chrony-quality
        return f"[green]{name}[/]"
    if abs_s < 1e-3:            # <1 ms — usable LAN
        return f"[yellow]{name}[/]"
    return f"[red]{name}[/]"     # ≥1 ms — public-internet-grade


class TimingScreen(Vertical):
    """Live chrony source comparison with TSL3 as the reference."""

    DEFAULT_CSS = """
    TimingScreen { padding: 1; }
    TimingScreen .section-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    TimingScreen #timing-utc {
        margin-top: 1;
        margin-bottom: 1;
    }
    TimingScreen #timing-status {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Per-source ring of (Δ-from-TSL3 in seconds) values.
        self._history: Dict[str, deque] = {}

    def compose(self):
        yield Static("Timing — chrony sources (TSL3 reference)",
                     classes="section-title")
        yield Static("", id="timing-utc")

        table = DataTable(id="timing-table", zebra_stripes=True,
                          cursor_type="row")
        table.add_columns(
            "Source", "Δ from TSL3", "Reach", "Age",
            "σ (sample)", f"trace ({HISTORY}s)",
        )
        yield table

        yield Static("", id="timing-status")

    def on_mount(self) -> None:
        self._refresh()
        # Live update every POLL_SEC.  set_interval runs on the
        # Textual event loop so chronyc subprocess calls block the UI
        # very briefly (~10-30 ms).  If that ever becomes visible,
        # move the chronyc invocation into a worker thread the way
        # cpu_freq.py does.
        self.set_interval(POLL_SEC, self._refresh)

    def _refresh(self) -> None:
        sources_csv = _run_chronyc(['sources'])
        tracking_csv = _run_chronyc(['tracking'])
        status = self.query_one("#timing-status", Static)
        utc = self.query_one("#timing-utc", Static)
        table = self.query_one("#timing-table", DataTable)

        if sources_csv is None:
            status.update(
                "[red]chronyc unavailable — chrony not running, "
                "or chronyc not in PATH[/]"
            )
            return

        sources = parse_sources(sources_csv)
        tracking = parse_tracking(tracking_csv or "")

        tsl3 = next((s for s in sources if s.name == 'TSL3'), None)
        if not tsl3:
            status.update(
                "[yellow]No TSL3 source in chronyc output — "
                "is the BPSK refclock configured?[/]"
            )
            return

        # Update history per source.
        for s in sources:
            delta = s.last_offset_sec - tsl3.last_offset_sec
            hist = self._history.setdefault(
                s.name, deque(maxlen=HISTORY)
            )
            hist.append(delta)

        # Header summary: kernel-vs-UTC.
        if tracking:
            ref = tracking.ref_id_name
            utc.update(
                f"Kernel clock vs UTC: "
                f"[bold]{format_offset(tracking.last_offset_sec)}[/] "
                f"(RMS {format_offset(tracking.rms_offset_sec)}, "
                f"root dispersion ±{format_offset(tracking.root_dispersion_sec)}) "
                f"— ref [cyan]{ref}[/]  leap: {tracking.leap_status}"
            )
        else:
            utc.update("[yellow]chronyc tracking unavailable[/]")

        # Body table.
        table.clear()
        for s in sources:
            delta = s.last_offset_sec - tsl3.last_offset_sec
            delta_str = "[bold]ref[/]" if s.name == 'TSL3' else format_offset(delta)
            spark = sparkline(list(self._history.get(s.name, [])))
            table.add_row(
                _row_color(s.name, delta),
                delta_str,
                format_reach(s.reach),
                format_age(s.last_rx_sec),
                format_offset(s.sample_error_sec),
                spark,
            )

        status.update(
            f"[dim]{len(sources)} source"
            f"{'s' if len(sources) != 1 else ''} — "
            f"refresh {POLL_SEC:.0f}s, history {HISTORY}s[/]"
        )
