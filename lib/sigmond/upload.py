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

# Clients whose upload needs a delivery-pipeline selection, not just the
# boolean enable flag.  psk-recorder's default pipeline is ``server-merge``,
# which ships every row to a wsprdaemon server that forwards to
# pskreporter.info on the node's behalf — so on a standalone node (no
# wsprdaemon-server SFTP) the boolean alone delivers nothing.  ``direct`` is
# the standalone-correct lever: the client POSTs straight to pskreporter.info.
# Enabling sets the default; merge-fleet nodes override (`--via server-merge`).
# meteor-scatter is direct-only by design (the knob was removed), and
# wspr-recorder's wsprnet path needs no pipeline selection — so neither
# appears here.  client -> (env var, default pipeline, valid choices).
DELIVERY_ON_ENABLE: dict[str, Tuple[str, str, List[str]]] = {
    "psk-recorder": ("PSK_DELIVERY_PIPELINES", "direct",
                     ["direct", "server-merge", "server-raw"]),
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


def apply_enable(client: str, instance: str, on: bool, base: str = "/etc",
                 delivery: "str | None" = None):
    """Flip the upload enable flag for one instance.  Returns
    (env_path, flag, destinations, delivery_set) where delivery_set is
    ``(env_key, value)`` if a delivery pipeline was also written, else None.

    When enabling a client that needs a delivery-pipeline selection (see
    DELIVERY_ON_ENABLE), the pipeline is set to ``delivery`` or the client's
    standalone-correct default — so the boolean flag alone never leaves the
    node routing to an unreachable path.  Disabling leaves the pipeline
    untouched.  Raises KeyError for an unknown client, ValueError for an
    invalid ``delivery`` choice."""
    if client not in UPLOAD_ENABLE:
        raise KeyError(client)
    flag, dests = UPLOAD_ENABLE[client]
    path = env_path_for(client, instance, base=base)
    set_env_flag(path, flag, "1" if on else "0")
    delivery_set = None
    if on and client in DELIVERY_ON_ENABLE:
        dkey, ddefault, dchoices = DELIVERY_ON_ENABLE[client]
        val = delivery or ddefault
        if val not in dchoices:
            raise ValueError(
                f"{dkey} must be one of {', '.join(dchoices)}; got {val!r}")
        set_env_flag(path, dkey, val)
        delivery_set = (dkey, val)
    elif delivery is not None:
        # --via passed for a client with no pipeline knob (or while disabling).
        raise ValueError(
            f"{client} has no delivery-pipeline selection"
            if client not in DELIVERY_ON_ENABLE else
            "delivery pipeline only applies when enabling (--on)")
    _chown_to_env_dir_owner(path)
    return path, flag, dests, delivery_set


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
