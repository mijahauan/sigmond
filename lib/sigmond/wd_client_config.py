"""Lightweight read/write wrapper for wsprdaemon v4 INI config.

Avoids importing wdlib (which is not a sigmond dependency).  Uses stdlib
configparser exactly as wdlib does so the files are interchangeable.

Key concepts
------------
Receiver   — [receiver:NAME] section; address, call, grid, password
BandMode   — [receiver:NAME:BAND] section; modes = "W2 F2 F5"
Schedule   — [schedule:LABEL] section; receiver = "band1 band2 ..."
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .paths import SIGMOND_CONF

WD_CONF_PATH = Path('/etc/wsprdaemon/wsprdaemon.conf')

# All WSPR-capable bands, display order (frequency ascending)
ALL_BANDS: list[str] = [
    "2200", "630", "160",
    "80", "80eu", "60", "60eu",
    "40", "30", "22",
    "20", "17", "15", "12", "10", "8", "6",
]

# Mode tokens
ALL_MODES: list[str] = ["W2", "F2", "F5", "F15", "F30", "I1"]

# Default bands pre-selected for KiwiSDR receivers (8 typical HF channels)
KIWI_DEFAULT_BANDS: list[str] = ["80", "40", "30", "20", "17", "15", "12", "10"]

# Default modes pre-populated when a band is first enabled
DEFAULT_BAND_MODES: dict[str, str] = {
    "2200": "W2 F2 F5 F15 F30",
    "630":  "W2 F2 F5 F15 F30",
}
_DEFAULT_MODES_STANDARD = "W2 F2 F5"

def default_modes(band: str) -> str:
    """Return the pre-populated mode string for a band."""
    return DEFAULT_BAND_MODES.get(band, _DEFAULT_MODES_STANDARD)


@dataclass
class WdReceiver:
    name:     str
    address:  str  = ""
    call:     str  = ""
    grid:     str  = ""
    password: str  = "NULL"
    # band -> space-separated modes string, e.g. {"80": "W2 F2 F5"}
    bands: dict[str, str] = field(default_factory=dict)

    @property
    def receiver_type(self) -> str:
        n = self.name.upper()
        if n.startswith('KA9Q_') and '_WWV' in n:
            return 'ka9q_wwv'
        if n.startswith('KA9Q_'):
            return 'ka9q'
        if n.startswith('KIWI_'):
            return 'kiwi'
        if n.startswith('MERG_'):
            return 'merge'
        return 'unknown'

    @property
    def is_merge(self) -> bool:
        return self.receiver_type == 'merge'


@dataclass
class WdConfig:
    receivers:     dict[str, WdReceiver] = field(default_factory=dict)
    ka9q_conf_name: str = ""
    rac:           str = ""
    schedule:      dict[str, dict[str, str]] = field(default_factory=dict)
    # slot label -> {receiver_name: "band1 band2 ..."}


def load_config(path: Path = WD_CONF_PATH) -> WdConfig:
    """Parse v4 INI.  Returns empty WdConfig if file absent or parse fails."""
    cfg = configparser.ConfigParser(
        comment_prefixes=(';', '#'),
        inline_comment_prefixes=(';', '#'),
        strict=False,
        interpolation=None,
    )
    result = WdConfig()
    if not path.exists():
        return result
    try:
        cfg.read(str(path))
    except Exception:
        return result

    if cfg.has_section('general'):
        g = cfg['general']
        result.ka9q_conf_name = g.get('ka9q_conf_name', '').strip()
        result.rac            = g.get('rac', '').strip()

    # Receivers
    for section in cfg.sections():
        parts = section.split(':')
        if parts[0] == 'receiver' and len(parts) == 2:
            s = cfg[section]
            result.receivers[parts[1]] = WdReceiver(
                name     = parts[1],
                address  = s.get('address',  '').strip(),
                call     = s.get('call',     '').strip(),
                grid     = s.get('grid',     '').strip(),
                password = s.get('password', 'NULL').strip() or 'NULL',
            )
        elif parts[0] == 'merge' and len(parts) == 2:
            s = cfg[section]
            sources = s.get('sources', '').split()
            result.receivers[parts[1]] = WdReceiver(
                name    = parts[1],
                address = ','.join(sources),
                call    = s.get('call', '').strip(),
                grid    = s.get('grid', '').strip(),
            )

    # Band modes
    for section in cfg.sections():
        parts = section.split(':')
        if len(parts) == 3 and parts[0] in ('receiver', 'merge'):
            rx_name, band = parts[1], parts[2]
            modes = cfg[section].get('modes', '').strip()
            if rx_name in result.receivers and modes:
                result.receivers[rx_name].bands[band] = modes

    # Schedule
    for section in cfg.sections():
        parts = section.split(':')
        if parts[0] == 'schedule' and len(parts) == 2:
            label = parts[1]
            slot: dict[str, str] = {}
            for key, val in cfg[section].items():
                if key == 'time':
                    continue
                slot[key.upper()] = val.strip()
            result.schedule[label] = slot

    return result


def save_config(wdc: WdConfig, path: Path = WD_CONF_PATH) -> None:
    """Write a v4 INI file.  Raises PermissionError / OSError on failure."""
    lines: list[str] = [
        "# wsprdaemon v4 configuration — managed by smd tui\n",
        "# Edit via Configure → wsprdaemon-client or directly with wd-ctl\n",
        "\n",
        "[general]\n",
    ]
    if wdc.ka9q_conf_name:
        lines.append(f"ka9q_conf_name = {wdc.ka9q_conf_name}\n")
    if wdc.rac:
        lines.append(f"rac = {wdc.rac}\n")
    lines.append("\n")

    for rx in wdc.receivers.values():
        if rx.is_merge:
            sources = rx.address.replace(',', ' ')
            lines += [
                f"[merge:{rx.name}]\n",
                f"sources = {sources}\n",
                f"call = {rx.call}\n",
                f"grid = {rx.grid}\n",
                "\n",
            ]
            for band, modes in sorted(rx.bands.items()):
                lines += [
                    f"[merge:{rx.name}:{band}]\n",
                    f"modes = {modes}\n",
                    "\n",
                ]
        else:
            lines += [
                f"[receiver:{rx.name}]\n",
                f"address  = {rx.address}\n",
                f"call     = {rx.call}\n",
                f"grid     = {rx.grid}\n",
                f"password = {rx.password}\n",
                "\n",
            ]
            for band, modes in sorted(rx.bands.items()):
                lines += [
                    f"[receiver:{rx.name}:{band}]\n",
                    f"modes = {modes}\n",
                    "\n",
                ]

    # Schedule — one slot per schedule key
    for label, slot in wdc.schedule.items():
        lines.append(f"[schedule:{label}]\n")
        lines.append("time = 00:00\n")
        for rx_name, bands_str in slot.items():
            lines.append(f"{rx_name} = {bands_str}\n")
        lines.append("\n")

    content = "".join(lines)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def is_v3_format(path: Path = WD_CONF_PATH) -> bool:
    """Return True if the file looks like v3 bash format (not v4 INI)."""
    if not path.exists():
        return False
    try:
        text = path.read_text(errors='replace')
        return 'RECEIVER_LIST' in text or text.strip().startswith('#!')
    except Exception:
        return False
