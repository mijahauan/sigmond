"""wsprdaemon-client (wspr) adapter — read-only in Phase 1.

Parses /etc/wsprdaemon/wsprdaemon.conf (v4 INI) with configparser — no
wdlib import.  Exposes one InstanceView per receiver so the per-radiod
harmonization rules can reason about wspr receivers that are bound to
different radiod_ids.

Phase 2 replaces this with the generic contract adapter once
wsprdaemon-client ships `wd-ctl inventory --json`.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Optional

from ..paths import WSPRDAEMON_CONF
from .base import ClientAdapter, ClientView, DiskWrite, InstanceView


class WsprAdapter(ClientAdapter):
    name = "wspr"

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or WSPRDAEMON_CONF

    def read_view(self) -> ClientView:
        view = ClientView(client_type=self.name, config_path=self.config_path)
        if not self.config_path.exists():
            view.issues.append(f"{self.config_path} not present")
            return view

        cfg = configparser.ConfigParser(
            comment_prefixes=(';', '#'),
            inline_comment_prefixes=(';', '#'),
            strict=False,
            interpolation=None,
        )
        try:
            cfg.read(self.config_path)
        except configparser.Error as exc:
            view.issues.append(f"failed to parse {self.config_path}: {exc}")
            return view

        view.installed = True

        default_radiod_id = ""
        if cfg.has_section('general'):
            default_radiod_id = cfg['general'].get('ka9q_conf_name', '').strip()

        # Collect receivers and their radiod bindings.  One InstanceView per
        # receiver.  "instance" name = receiver name; "radiod_id" = the
        # radiod_name field, falling back to ka9q_conf_name from [general].
        receivers_seen = False
        for section in cfg.sections():
            parts = section.split(':')
            if parts[0] != 'receiver' or len(parts) != 2:
                continue
            receivers_seen = True
            rx_name = parts[1]
            s = cfg[section]
            radiod_name = s.get('radiod_name', '').strip() or default_radiod_id or None
            view.instances.append(InstanceView(
                instance=rx_name,
                radiod_id=radiod_name,
                preferred_cores="worker",
                uses_timing_calibration=False,
            ))

        if not receivers_seen:
            # Single placeholder instance so the host still appears in
            # harmonize rules and radiod_resolution can still check
            # ka9q_conf_name.
            view.instances.append(InstanceView(
                instance="default",
                radiod_id=default_radiod_id or None,
                preferred_cores="worker",
            ))

        # Disk writes are at /var/spool/wsprdaemon/ — one spool per host.
        # MB/day and retention are unknown without deeper schedule parsing,
        # left as 0.0 in Phase 1.
        view.instances[0].disk_writes.append(
            DiskWrite(path="/var/spool/wsprdaemon", mb_per_day=0.0, retention_days=0)
        )
        return view
