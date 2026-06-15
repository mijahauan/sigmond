"""Hardware-readiness probes (CONTRACT §3 / install-orchestration Phase D).

A hardware-gated client reports its own readiness via the top-level
``hardware_present`` field of ``<client> inventory --json`` — the client
detects its own hardware (CONTRACT §3) rather than sigmond hard-coding USB
IDs.  :func:`hardware_ready` consults that self-describe first and falls back
to a per-client lsusb probe only while a client has not yet emitted the field,
so detection keeps working across the transition.

Tri-state throughout:
  * ``True``  — the client's required hardware is present (or the client is in
    a no-hardware mode, e.g. mag-recorder's simulator, and can still produce).
  * ``False`` — the client requires hardware that is absent.
  * ``None``  — not hardware-gated, the client doesn't implement the field and
    has no fallback, or readiness could not be determined.  Callers must treat
    ``None`` as "don't gate / unknown", never as absent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Callable, Optional


def _lsusb() -> str:
    try:
        return subprocess.run(["lsusb"], capture_output=True, text=True,
                              timeout=5).stdout
    except Exception:                                  # noqa: BLE001
        return ""


def _magnetometer_present() -> bool:
    """RM3100 via its Pololu USB-I2C adapter (udev symlink or 1ffb:2502/2503)."""
    if os.path.exists("/dev/ttyMAG0"):
        return True
    return bool(re.search(r"1ffb:250[23]|pololu", _lsusb(), re.I))


def _sdr_present() -> bool:
    """RX888 / RX888mk2 — a Cypress FX3 (04b4:00f0/00f1/00f3) on the USB bus."""
    return bool(re.search(r"rx888|04b4:00f[013]", _lsusb(), re.I))


def _gpsdo_present() -> bool:
    """Leo Bodnar GPSDO (USB/HID) — VID 1dd2 (LBE-1420/1421/mini) on the bus."""
    return bool(re.search(r"1dd2:|leo bodnar", _lsusb(), re.I))


# Legacy lsusb fallbacks, keyed by client/component name.  Consulted ONLY when
# the client's `inventory --json` does not report `hardware_present` (or has no
# inventory CLI — e.g. upstream ka9q-radio, whose SDR sigmond must detect
# itself).  As each client emits the field, its entry here becomes vestigial.
_LEGACY_PROBES: dict[str, Callable[[], bool]] = {
    "mag-recorder":  _magnetometer_present,
    "gpsdo-monitor": _gpsdo_present,
    "ka9q-radio":    _sdr_present,
}


def inventory_hardware_present(client: str) -> Optional[bool]:
    """Read ``hardware_present`` from ``<client> inventory --json``.

    Returns the reported bool, or ``None`` when the client has no CLI on PATH,
    the call fails, the JSON is unparseable, or the field is absent/non-bool."""
    exe = shutil.which(client)
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "inventory", "--json"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
    except Exception:                                  # noqa: BLE001
        return None
    val = data.get("hardware_present") if isinstance(data, dict) else None
    return val if isinstance(val, bool) else None


def hardware_ready(client: str) -> Optional[bool]:
    """Tri-state hardware readiness for ``client`` (see module docstring).

    The client's own ``inventory --json hardware_present`` is authoritative;
    the lsusb fallback applies only when the client doesn't report it."""
    val = inventory_hardware_present(client)
    if val is not None:
        return val
    probe = _LEGACY_PROBES.get(client)
    return probe() if probe else None
