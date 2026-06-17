"""Per-instance upload enablement — the shared source of truth for which
env flag turns a client's upstream upload on, used by both the
``smd config upload`` command and harmonize's ``rule_upload_enabled``.

Identity is owned by site-profile.toml / ``smd config render`` and
credentials by ``smd admin secrets`` — this module only owns the
per-instance ENABLE flag (the otherwise-invisible decode->upload gap).
"""
from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import List, Tuple

# client -> (enable-flag env var, [destination labels]).  Adding a row here
# wires the client into `smd config upload` AND rule_upload_enabled.
UPLOAD_ENABLE: dict[str, Tuple[str, List[str]]] = {
    "wspr-recorder":  ("WSPR_USE_HS_UPLOADER",
                       ["wsprnet.org", "wsprdaemon.org"]),
    "psk-recorder":   ("PSK_USE_HS_UPLOADER", ["pskreporter.info"]),
    "meteor-scatter": ("METEOR_SCATTER_USE_HS_UPLOADER", ["pskreporter.info"]),
}


def storage_instance(instance: str) -> str:
    """The per-instance env filename stem for a reporter/instance id.

    Reporter ids are stored path-safe with '/'->'=' (e.g. AC0G/S -> AC0G=S);
    legacy radiod-keyed instances (my-rx888) have no slash and pass through.
    Case is preserved — the env file is named with the id as provisioned.
    """
    return instance.replace("/", "=")


def env_path_for(client: str, instance: str, base: str = "/etc") -> Path:
    """Resolve the per-instance env file, preferring the canonical
    storage form and falling back to any already-present spelling."""
    env_dir = Path(base) / client / "env"
    canonical = env_dir / f"{storage_instance(instance)}.env"
    if canonical.exists():
        return canonical
    # Fall back to an existing file under a different spelling (e.g. the
    # raw reporter id, or a systemd-escaped name) so we edit, not shadow.
    for cand in {instance, storage_instance(instance.upper())}:
        p = env_dir / f"{cand}.env"
        if p.exists():
            return p
    return canonical


def set_env_flag(path: Path, key: str, value: str) -> None:
    """Set ``key=value`` in an env file, preserving every other line
    (comments, ordering, the decode flag).  Creates the file if absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    out, found = [], False
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s \
                and s.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")


def _chown_to_env_dir_owner(path: Path) -> None:
    """chown the env file to the env dir's owner (the service user) so the
    daemon can read it; no-op if not root / dir missing."""
    try:
        st = path.parent.stat()
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o644)
    except (PermissionError, FileNotFoundError):
        pass


def apply_enable(client: str, instance: str, on: bool, base: str = "/etc"):
    """Flip the upload enable flag for one instance.  Returns
    (env_path, flag, destinations).  Raises KeyError for an unknown
    client (one with no upstream upload path)."""
    if client not in UPLOAD_ENABLE:
        raise KeyError(client)
    flag, dests = UPLOAD_ENABLE[client]
    path = env_path_for(client, instance, base=base)
    set_env_flag(path, flag, "1" if on else "0")
    _chown_to_env_dir_owner(path)
    return path, flag, dests


def is_truthy(v) -> bool:
    return v is not None and str(v).strip().lower() not in (
        "", "0", "false", "no", "off")


def read_flag(path: Path, key: str):
    """Current value of ``key`` in an env file, or None if unset/missing."""
    try:
        for ln in Path(path).read_text().splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s \
                    and s.split("=", 1)[0].strip() == key:
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None
