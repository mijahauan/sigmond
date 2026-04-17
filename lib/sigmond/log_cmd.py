"""smd log — journal tailing, file-log tailing, and runtime log-level control.

Implements the sigmond side of client contract §10 (log_paths) and §11
(runtime log level via coordination.env + SIGHUP).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .catalog import find_client_binary
from .paths import COORDINATION_ENV


def client_env_key(client_name: str) -> str:
    """'psk-recorder' → 'PSK_RECORDER_LOG_LEVEL'."""
    return client_name.upper().replace('-', '_') + '_LOG_LEVEL'


# ── inventory log_paths ──────────────────────────────────────────────

def get_inventory_log_paths(client_name: str, timeout: float = 5.0) -> Optional[dict]:
    """Shell out to `<client> inventory --json` and return the log_paths dict."""
    binary = find_client_binary(client_name)
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, 'inventory', '--json'],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        return data.get('log_paths')
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def flatten_log_paths(obj: dict | str, _result: list[str] | None = None) -> list[str]:
    """Recursively flatten a nested log_paths dict into a list of file paths."""
    if _result is None:
        _result = []
    if isinstance(obj, str):
        _result.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            flatten_log_paths(v, _result)
    return _result


# ── journal follow ───────────────────────────────────────────────────

def follow_journal(units: list[str]) -> int:
    """exec into journalctl --follow for the given systemd units."""
    cmd = ['journalctl', '--follow', '--no-hostname', '-n', '50']
    for u in units:
        cmd.extend(['-u', u])
    os.execvp(cmd[0], cmd)


# ── file follow ──────────────────────────────────────────────────────

def follow_files(paths: list[str]) -> int:
    """exec into tail -f for the given file paths."""
    existing = [p for p in paths if Path(p).exists()]
    if not existing:
        return 1
    cmd = ['tail', '-f', '-n', '20'] + existing
    os.execvp(cmd[0], cmd)


# ── log level ────────────────────────────────────────────────────────

def set_log_level(
    client_name: Optional[str],
    level: str,
    env_path: Path = COORDINATION_ENV,
) -> str:
    """Write a log-level entry into coordination.env.

    If client_name is None, writes CLIENT_LOG_LEVEL (generic fallback).
    Returns the key that was written.
    """
    level = level.upper()
    if level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        raise ValueError(f"invalid log level: {level}")

    key = client_env_key(client_name) if client_name else 'CLIENT_LOG_LEVEL'
    _upsert_env_line(env_path, key, level)
    return key


def _upsert_env_line(env_path: Path, key: str, value: str) -> None:
    """Insert or update KEY=VALUE in an env file, preserving other lines."""
    lines: list[str] = []
    found = False

    if env_path.exists():
        lines = env_path.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.startswith(f'{key}='):
                lines[i] = f'{key}={value}'
                found = True
                break

    if not found:
        if lines and not lines[-1].startswith('#') and lines[-1].strip():
            lines.append('')
        lines.append(f'{key}={value}')

    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=env_path.parent, suffix='.env.tmp')
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        os.replace(tmp_path, env_path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def send_sighup(units: list[str]) -> list[str]:
    """Send SIGHUP via systemctl kill --signal=HUP to each unit.

    Returns list of units that failed.
    """
    failed = []
    for unit in units:
        r = subprocess.run(
            ['systemctl', 'kill', '--signal=HUP', unit],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            failed.append(unit)
    return failed
