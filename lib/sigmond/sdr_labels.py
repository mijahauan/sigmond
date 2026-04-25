"""Persistent metadata store for discovered SDR devices.

Stored at SIGMOND_STATE/sdr-labels.toml.  Each entry keyed by device key:
  usb:<vid>:<pid>:<index>          local USB SDR
  kiwisdr:<ip>:<port>              KiwiSDR found by LAN scan
  ka9q_fe:<host>:<frontend_name>   frontend served by ka9q-radio

TOML format:
  [device."usb:04b4:00bc:0"]
  label = "RX-888 HF"        # friendly display name
  call  = "AI6VN-0"          # WSPR reporter callsign
  grid  = "CM88mc"           # Maidenhead grid square
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .paths import SDR_LABELS_PATH


@dataclass
class SdrDeviceMeta:
    key:      str
    label:    str = ""
    call:     str = ""
    grid:     str = ""
    channels: int = 0   # KiwiSDR: max simultaneous receive channels; 0 = unlimited/unknown


def load_devices(path: Path = SDR_LABELS_PATH) -> dict[str, SdrDeviceMeta]:
    """Return {key: SdrDeviceMeta} dict, empty if file absent or unreadable."""
    if not path.exists():
        return {}
    try:
        import tomllib
        with open(path, 'rb') as f:
            raw = tomllib.load(f)
    except Exception:
        return {}

    result: dict[str, SdrDeviceMeta] = {}

    # New rich format: [device."key"] sections
    for key, val in (raw.get('device', {}) or {}).items():
        if isinstance(val, dict):
            try:
                channels = int(val.get('channels', 0) or 0)
            except (ValueError, TypeError):
                channels = 0
            result[key] = SdrDeviceMeta(
                key=key,
                label=str(val.get('label', '') or ''),
                call=str(val.get('call', '')  or ''),
                grid=str(val.get('grid', '')  or ''),
                channels=channels,
            )

    # Backward-compat: flat [labels] section from earlier format
    for key, val in (raw.get('labels', {}) or {}).items():
        if key not in result:
            result[key] = SdrDeviceMeta(key=key, label=str(val or ''))

    return result


def save_devices(devices: dict[str, SdrDeviceMeta],
                 path: Path = SDR_LABELS_PATH) -> None:
    """Write device metadata to disk."""
    lines = [
        "# sigmond SDR device metadata — managed by smd tui\n",
        "# Keys: usb:<vid>:<pid>:<n>  |  kiwisdr:<ip>:<port>  |  ka9q_fe:<host>:<name>\n",
        "\n",
    ]
    for key in sorted(devices):
        d = devices[key]
        if not (d.label or d.call or d.grid or d.channels):
            continue
        ek = key.replace('"', '\\"')
        lines.append(f'[device."{ek}"]\n')
        if d.label:
            lines.append(f'label    = "{_esc(d.label)}"\n')
        if d.call:
            lines.append(f'call     = "{_esc(d.call)}"\n')
        if d.grid:
            lines.append(f'grid     = "{_esc(d.grid)}"\n')
        if d.channels:
            lines.append(f'channels = {d.channels}\n')
        lines.append('\n')

    content = "".join(lines)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    except PermissionError:
        _sudo_write(content, path)


def get_device(key: str, path: Path = SDR_LABELS_PATH) -> SdrDeviceMeta:
    return load_devices(path).get(key, SdrDeviceMeta(key=key))


def set_device(meta: SdrDeviceMeta, path: Path = SDR_LABELS_PATH) -> None:
    devices = load_devices(path)
    devices[meta.key] = meta
    save_devices(devices, path)


# ---------------------------------------------------------------------------
# Convenience shims (keep callers of old get_label / set_label working)
# ---------------------------------------------------------------------------

def load_labels(path: Path = SDR_LABELS_PATH) -> dict[str, str]:
    return {k: d.label for k, d in load_devices(path).items() if d.label}


def get_label(key: str, path: Path = SDR_LABELS_PATH) -> str:
    return get_device(key, path).label


def set_label(key: str, label: str, path: Path = SDR_LABELS_PATH) -> None:
    d = get_device(key, path)
    d.label = label
    set_device(d, path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"')


def _sudo_write(content: str, path: Path) -> None:
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as f:
            f.write(content)
            tmp = f.name
        subprocess.run(['sudo', 'cp', tmp, str(path)],
                       capture_output=True, timeout=10)
    except Exception:
        pass
