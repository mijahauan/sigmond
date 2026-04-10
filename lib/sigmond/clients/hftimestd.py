"""hf-timestd adapter — read-only in Phase 1.

Phase 1 reads /etc/hf-timestd/timestd-config.toml directly.  Once the
hf-timestd retrofit lands (Phase 2), this adapter is replaced by the
generic contract.py adapter that shells out to `timestd inventory
--json`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..paths import HF_TIMESTD_CONF
from .base import ClientAdapter, ClientView, DiskWrite, InstanceView


class HfTimestdAdapter(ClientAdapter):
    name = "hf-timestd"

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or HF_TIMESTD_CONF

    def read_view(self) -> ClientView:
        view = ClientView(client_type=self.name, config_path=self.config_path)
        if not self.config_path.exists():
            view.issues.append(f"{self.config_path} not present")
            return view

        import tomllib
        try:
            with open(self.config_path, 'rb') as f:
                raw = tomllib.load(f)
        except (OSError, Exception) as exc:
            view.issues.append(f"failed to parse {self.config_path}: {exc}")
            return view

        view.installed = True

        status_dns = (raw.get('ka9q', {}) or {}).get('status_address', '')
        recorder = raw.get('recorder', {}) or {}
        data_root = recorder.get('production_data_root', '/var/lib/timestd')

        freqs = []
        chan_groups = recorder.get('channel_group', {}) or {}
        for group in chan_groups.values():
            for ch in (group.get('channels', []) or []):
                hz = ch.get('frequency_hz')
                if hz:
                    freqs.append(int(hz))

        provides_timing = (raw.get('timing', {}) or {}).get('authority', '') != ""

        iv = InstanceView(
            instance="default",
            radiod_id=None,    # Phase 1 has no coordination.toml binding yet
            preferred_cores="worker",
            frequencies_hz=freqs,
            ka9q_channels=len(freqs),
            disk_writes=[DiskWrite(path=data_root, mb_per_day=0.0, retention_days=0)],
            provides_timing_calibration=provides_timing,
        )
        # Surface the client's own status DNS so the radiod_resolution rule
        # can check it matches coordination.toml when both are present.
        iv.radiod_status_dns = status_dns
        view.instances.append(iv)
        return view
