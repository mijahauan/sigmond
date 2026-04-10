"""Subprocess and root-check helpers."""

import os
import subprocess
import sys


def run(cmd: list, *, cwd=None, capture: bool = True, sudo: bool = False) -> subprocess.CompletedProcess:
    """Run a command, optionally prefixing with sudo when not already root."""
    if sudo and os.geteuid() != 0:
        cmd = ['sudo'] + cmd
    return subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True)


def need_root(cmd_name: str) -> bool:
    """Return True and print an error if the current process is not root."""
    if os.geteuid() != 0:
        print(f'smd {cmd_name}: must run as root (sudo smd {cmd_name})',
              file=sys.stderr)
        return True
    return False


# Back-compat aliases.
_run       = run
_need_root = need_root
