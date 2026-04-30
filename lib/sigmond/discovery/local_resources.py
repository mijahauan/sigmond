"""Local-resources probe — gathers host-side counters relevant to
packet-loss diagnostics in the RX888 → radiod → RTP pipeline.

Counters captured (subset emitted depends on what the operator declared
in [local_system]):

  cpu_per_core    %usr / %sys / %soft / %idle deltas, per logical CPU,
                  computed against the previous run's /proc/stat snapshot.
  udp             RcvbufErrors / InErrors / InCsumErrors as rates over
                  the inter-run interval.
  nics            For each declared NIC: rx_missed_errors,
                  rx_no_buffer_count, rx_fifo_errors, rx_dropped,
                  multicast — current values from `ethtool -S`.
  irqs            For each declared handler in irq_pins: per-core
                  interrupt counts from /proc/interrupts, plus the list
                  of cores that received any interrupts (for drift
                  detection by the reconciler).
  usb             If any usb_devices declared: count of URB / overrun /
                  reset error lines in `dmesg --since -60sec`.  Coarse
                  by design — dmesg lines don't carry vendor:product
                  info, so per-device attribution waits for a follow-up
                  that talks to RX888 firmware control endpoints.

The probe is pure given its inputs: every external dependency
(/proc reads, ethtool, dmesg, the snapshot store) is injectable so
tests run with no network, no subprocess, no filesystem.

First-run behaviour: when no previous snapshot exists, the rate-based
fields (cpu, udp) emit zeros and `interval_s=0`.  Absolute counters
(nics, irqs, usb) emit normally.  Rates appear on the second run
onward.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..environment import Environment, Observation
from . import load_snapshot, save_snapshot


# ---------------------------------------------------------------------------
# Default transports
# ---------------------------------------------------------------------------

def _default_read_proc(path: str) -> str:
    return Path(path).read_text()


def _default_run_ethtool(iface: str, timeout: float) -> str:
    try:
        proc = subprocess.run(
            ["ethtool", "-S", iface],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _default_read_dmesg(since_seconds: int, timeout: float) -> str:
    try:
        proc = subprocess.run(
            ["dmesg", "--ctime", "--since", f"-{since_seconds}sec"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return ""


# ---------------------------------------------------------------------------
# probe entrypoint
# ---------------------------------------------------------------------------

# NIC counter names we keep from `ethtool -S`.  Anything else is dropped.
_NIC_COUNTERS = (
    "rx_missed_errors",
    "rx_no_buffer_count",
    "rx_fifo_errors",
    "rx_dropped",
    "multicast",
)

_DMESG_USB_PATTERNS = (
    re.compile(r"\burb\b.*\b(?:error|fail)", re.IGNORECASE),
    re.compile(r"\boverrun\b", re.IGNORECASE),
    re.compile(r"\busb\b.*\breset\b", re.IGNORECASE),
)


def probe(env: Environment, *,
          timeout: float = 5.0,
          limiter=None,
          read_proc: Callable[[str], str] = _default_read_proc,
          run_ethtool: Callable[[str, float], str] = _default_run_ethtool,
          read_dmesg: Callable[[int, float], str] = _default_read_dmesg,
          load_prev: Optional[Callable[[str], Optional[dict]]] = None,
          save_curr: Optional[Callable[[str, dict], None]] = None,
          clock: Callable[[], float] = time.time,
          dmesg_window_seconds: int = 60,
          ) -> list[Observation]:
    """Gather host-side resource counters into a single Observation.

    See module docstring for the field shape.  ``load_prev`` and
    ``save_curr`` default to the cache-backed helpers in
    ``discovery/__init__.py``; tests inject in-memory equivalents.
    """
    declared = env.local_system
    now = clock()

    if load_prev is None:
        load_prev = load_snapshot
    if save_curr is None:
        save_curr = save_snapshot

    errors: list[str] = []

    # ---- /proc reads (all soft-fail) ----
    proc_stat = _safe_read(read_proc, "/proc/stat", errors)
    proc_net_snmp = _safe_read(read_proc, "/proc/net/snmp", errors)
    proc_interrupts = _safe_read(read_proc, "/proc/interrupts", errors)

    # ---- current raw snapshots ----
    cur_cpu = _parse_proc_stat(proc_stat)
    cur_udp = _parse_proc_net_snmp_udp(proc_net_snmp)
    cur_irq = _parse_proc_interrupts(
        proc_interrupts, declared.irq_pins.keys()
    )

    # ---- previous raw snapshot for delta math ----
    prev = load_prev("local_resources") or {}
    prev_at = float(prev.get("captured_at", 0.0) or 0.0)
    interval_s = max(0.0, now - prev_at) if prev_at > 0 else 0.0

    # ---- derived rates / drift ----
    cpu_per_core = _delta_cpu(prev.get("cpu", {}), cur_cpu)
    udp_rates = _delta_udp(prev.get("udp", {}), cur_udp, interval_s)
    irq_observed = _summarise_irq(cur_irq, declared.irq_pins)

    # ---- per-NIC ethtool snapshots (no rate; absolute counters) ----
    nic_fields: dict = {}
    for nic in declared.nics:
        nic_fields[nic] = _parse_ethtool(run_ethtool(nic, timeout))

    # ---- USB error count from dmesg (only if operator cares) ----
    if declared.usb_devices:
        dmesg_out = read_dmesg(dmesg_window_seconds, timeout)
        usb_fields = _parse_dmesg_usb(
            dmesg_out, declared.usb_devices, dmesg_window_seconds
        )
    else:
        usb_fields = {}

    fields: dict = {
        "cpu_per_core": cpu_per_core,
        "udp": {**udp_rates, "interval_s": interval_s},
        "nics": nic_fields,
        "irqs": irq_observed,
        "usb": usb_fields,
    }
    if errors:
        fields["errors"] = errors

    # Persist current raw snapshot for the next run's delta math.  Done
    # last so a parser exception above doesn't leave a half-written
    # snapshot for the next run to misread.
    save_curr("local_resources", {
        "captured_at": now,
        "cpu": cur_cpu,
        "udp": cur_udp,
        "irq": cur_irq,
    })

    return [Observation(
        source="local_resources",
        kind="local_system",
        id="localhost",
        endpoint="localhost",
        fields=fields,
        observed_at=now,
        ok=not errors,
        error="; ".join(errors) if errors else "",
    )]


def _safe_read(reader: Callable[[str], str], path: str,
               errors: list[str]) -> str:
    try:
        return reader(path)
    except (OSError, ValueError) as e:
        errors.append(f"{path}: {e.__class__.__name__}")
        return ""


# ---------------------------------------------------------------------------
# /proc/stat — per-core jiffy counters
# ---------------------------------------------------------------------------

# Fields after the cpuN label, in order:
# user nice system idle iowait irq softirq steal guest guest_nice
_CPU_FIELDS = ("user", "nice", "system", "idle", "iowait",
               "irq", "softirq", "steal", "guest", "guest_nice")


def _parse_proc_stat(text: str) -> dict:
    """Return ``{"cpu0": {field: jiffies, ...}, ...}`` for per-core lines.

    The aggregate ``cpu`` line (no digit) is skipped — sigmond cares
    about per-core load, not the average.
    """
    out: dict = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        head = parts[0]
        if not head.startswith("cpu") or head == "cpu":
            continue
        try:
            int(head[3:])
        except ValueError:
            continue
        vals = []
        for token in parts[1:1 + len(_CPU_FIELDS)]:
            try:
                vals.append(int(token))
            except ValueError:
                vals.append(0)
        # Pad short rows (older kernels lack guest/guest_nice).
        vals.extend([0] * (len(_CPU_FIELDS) - len(vals)))
        out[head] = dict(zip(_CPU_FIELDS, vals))
    return out


def _delta_cpu(prev: dict, cur: dict) -> list[dict]:
    """Return one dict per core with derived percentages.

    First run (prev empty) yields zeros for the rates — without a
    baseline, computing percentages over absolute since-boot jiffy
    counts would produce lifetime-averages masquerading as
    interval-rates, which is misleading for packet-loss diagnostics.
    """
    out: list[dict] = []
    for label in sorted(cur.keys(),
                        key=lambda k: int(k[3:]) if k[3:].isdigit() else 0):
        c = cur[label]
        p = prev.get(label, {}) if prev else {}
        if not p:
            out.append({
                "core": int(label[3:]) if label[3:].isdigit() else 0,
                "usr": 0.0, "sys": 0.0, "soft": 0.0, "idle": 0.0,
                "total_jiffies": 0,
            })
            continue
        delta = {f: c.get(f, 0) - p.get(f, 0) for f in _CPU_FIELDS}
        total = sum(delta.values())
        if total <= 0:
            out.append({
                "core": int(label[3:]) if label[3:].isdigit() else 0,
                "usr": 0.0, "sys": 0.0, "soft": 0.0, "idle": 0.0,
                "total_jiffies": 0,
            })
            continue
        out.append({
            "core": int(label[3:]),
            "usr":  round(100.0 * delta["user"] / total, 2),
            "sys":  round(100.0 * delta["system"] / total, 2),
            "soft": round(100.0 * delta["softirq"] / total, 2),
            "idle": round(100.0 * delta["idle"] / total, 2),
            "total_jiffies": total,
        })
    return out


# ---------------------------------------------------------------------------
# /proc/net/snmp — UDP block
# ---------------------------------------------------------------------------

def _parse_proc_net_snmp_udp(text: str) -> dict:
    """Extract the ``Udp:`` data row, keyed by header field names."""
    header: list[str] = []
    for line in text.splitlines():
        if not line.startswith("Udp:"):
            continue
        rest = line[len("Udp:"):].strip().split()
        if not header:
            header = rest
            continue
        # Second Udp: line is the values.
        out: dict = {}
        for name, raw in zip(header, rest):
            try:
                out[name] = int(raw)
            except ValueError:
                pass
        return out
    return {}


def _delta_udp(prev: dict, cur: dict, interval_s: float) -> dict:
    """Return RcvbufErrors / InErrors rates plus absolute current values.

    On first run (interval_s=0 or no prev) the rates are 0 but the
    absolute counts come through, so the operator can still see whether
    *any* loss has occurred since boot.
    """
    keys = ("RcvbufErrors", "InErrors", "InCsumErrors")
    out: dict = {}
    for k in keys:
        cur_v = int(cur.get(k, 0) or 0)
        out[f"{_snake(k)}_total"] = cur_v
        if interval_s > 0 and prev:
            delta = cur_v - int(prev.get(k, 0) or 0)
            out[f"{_snake(k)}_rate"] = round(max(0, delta) / interval_s, 4)
        else:
            out[f"{_snake(k)}_rate"] = 0.0
    return out


def _snake(camel: str) -> str:
    """RcvbufErrors → rcvbuf_errors."""
    return re.sub(r"([a-z])([A-Z])", r"\1_\2", camel).lower()


# ---------------------------------------------------------------------------
# /proc/interrupts
# ---------------------------------------------------------------------------

def _parse_proc_interrupts(text: str, handler_names: Iterable[str]) -> dict:
    """Sum per-CPU counts per declared handler.

    Multiple IRQ lines may match a single handler name (e.g. ``xhci_hcd``
    appears once per MSI vector); their per-CPU columns are summed.
    Non-numeric tokens are skipped (the trailing chip/handler name is
    text, not a column).
    """
    handlers = set(handler_names)
    if not handlers:
        return {}

    lines = text.splitlines()
    if not lines:
        return {}

    # First line is the CPU header: "           CPU0       CPU1 ..."
    n_cpus = sum(1 for tok in lines[0].split() if tok.startswith("CPU"))
    if n_cpus == 0:
        return {}

    out: dict = {h: [0] * n_cpus for h in handlers}
    for line in lines[1:]:
        if ":" not in line:
            continue
        _, rest = line.split(":", 1)
        tokens = rest.split()
        if len(tokens) < n_cpus:
            continue
        # Identify handler from trailing tokens.
        trailing = " ".join(tokens[n_cpus:])
        matched = None
        for h in handlers:
            if h in trailing:
                matched = h
                break
        if matched is None:
            continue
        for i in range(n_cpus):
            try:
                out[matched][i] += int(tokens[i])
            except ValueError:
                pass
    return out


def _summarise_irq(cur_irq: dict, declared_pins: dict) -> dict:
    """Return per-handler observation: which cores actually received
    interrupts vs. the cores the operator declared.

    The reconciler decides whether a mismatch is degraded — this layer
    only reports facts.
    """
    out: dict = {}
    for handler, counts in cur_irq.items():
        observed = [i for i, n in enumerate(counts) if n > 0]
        out[handler] = {
            "expected_cores": list(declared_pins.get(handler, [])),
            "observed_cores": observed,
            "per_core_count": list(counts),
        }
    return out


# ---------------------------------------------------------------------------
# ethtool -S
# ---------------------------------------------------------------------------

# `     rx_missed_errors: 0`
_ETHTOOL_LINE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(\d+)\s*$")


def _parse_ethtool(text: str) -> dict:
    """Keep only the counters in ``_NIC_COUNTERS``."""
    out: dict = {}
    for line in text.splitlines():
        m = _ETHTOOL_LINE.match(line)
        if not m:
            continue
        name, val = m.group(1), m.group(2)
        if name in _NIC_COUNTERS:
            out[name] = int(val)
    return out


# ---------------------------------------------------------------------------
# dmesg-based USB error counters
# ---------------------------------------------------------------------------

def _parse_dmesg_usb(text: str, declared_devices: list,
                     window_s: int) -> dict:
    """Coarse-grained USB error tally from dmesg over ``window_s``.

    Lines aren't reliably attributable to specific vendor:product IDs,
    so the count is host-wide.  ``declared_devices`` is recorded for
    context (so the field's presence is a clear signal that the operator
    cares about USB) but not used for filtering yet.
    """
    counts = {"urb_errors": 0, "overruns": 0, "resets": 0}
    for line in text.splitlines():
        if _DMESG_USB_PATTERNS[0].search(line):
            counts["urb_errors"] += 1
        if _DMESG_USB_PATTERNS[1].search(line):
            counts["overruns"] += 1
        if _DMESG_USB_PATTERNS[2].search(line):
            counts["resets"] += 1
    counts["window_seconds"] = window_s
    counts["watched_devices"] = list(declared_devices)
    return counts
