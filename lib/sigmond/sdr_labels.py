"""Persistent label store for discovered SDR devices.

Labels are stored at SIGMOND_STATE/sdr-labels.toml (sigmond-writable).
Key format:
  usb:<vid>:<pid>:<index>          local USB SDR (index = 0-based enumeration order)
  kiwisdr:<ip>:<port>              KiwiSDR found by LAN scan
  ka9q_fe:<host>:<frontend_name>   frontend served by a ka9q-radio instance
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .paths import SDR_LABELS_PATH


def load_labels(path: Path = SDR_LABELS_PATH) -> dict[str, str]:
    """Return {key: label} dict, empty if file absent or unreadable."""
    if not path.exists():
        return {}
    try:
        import tomllib
        with open(path, 'rb') as f:
            raw = tomllib.load(f)
        result = raw.get('labels', {})
        return {str(k): str(v) for k, v in result.items()}
    except Exception:
        return {}


def save_labels(labels: dict[str, str],
                path: Path = SDR_LABELS_PATH) -> None:
    """Write labels to disk.  Silently skips on permission failure."""
    lines = ["# sigmond SDR device labels — managed by smd tui\n", "[labels]\n"]
    for key, label in sorted(labels.items()):
        escaped_key   = key.replace('"', '\\"')
        escaped_label = label.replace('"', '\\"').replace('\\', '\\\\')
        lines.append(f'"{escaped_key}" = "{escaped_label}"\n')
    content = "".join(lines)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    except PermissionError:
        _sudo_write(content, path)


def set_label(key: str, label: str,
              path: Path = SDR_LABELS_PATH) -> None:
    labels = load_labels(path)
    if label:
        labels[key] = label
    else:
        labels.pop(key, None)
    save_labels(labels, path)


def get_label(key: str, path: Path = SDR_LABELS_PATH) -> str:
    return load_labels(path).get(key, "")


def _sudo_write(content: str, path: Path) -> None:
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as f:
            f.write(content)
            tmp = f.name
        subprocess.run(['sudo', 'cp', tmp, str(path)], capture_output=True, timeout=10)
        subprocess.run(['sudo', 'chown', f'{Path("/proc/self/loginuid").read_text().strip()}',
                        str(path)], capture_output=True, timeout=5)
    except Exception:
        pass
