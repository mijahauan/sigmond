"""hf-timestd adapter.

Phase 2 (current): prefers shelling out to `hf-timestd inventory
--json` per the client contract.  Falls back to reading
/etc/hf-timestd/timestd-config.toml directly when the binary isn't
present (so a sigmond install on a host with an older hf-timestd
keeps working).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from ..paths import HF_TIMESTD_CONF
from .base import ClientAdapter, ClientView, DiskWrite, InstanceView
from .contract import ContractAdapter


# Common locations where the hf-timestd CLI might live.
_HFTIMESTD_BIN_CANDIDATES = (
    "/usr/local/bin/hf-timestd",
    "/opt/hf-timestd/venv/bin/hf-timestd",
)


class HfTimestdAdapter(ClientAdapter):
    name = "hf-timestd"

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or HF_TIMESTD_CONF

    def _find_binary(self) -> Optional[str]:
        on_path = shutil.which("hf-timestd")
        if on_path:
            return on_path
        for cand in _HFTIMESTD_BIN_CANDIDATES:
            if Path(cand).is_file():
                return cand
        return None

    def read_view(self) -> ClientView:
        # Phase 2: prefer the contract surface if hf-timestd >= 6.12.x
        binary = self._find_binary()
        if binary:
            contract = ContractAdapter()
            contract.name = self.name
            contract.binary = binary
            view = contract.read_view()
            # If the binary lacks an `inventory` subcommand (older
            # hf-timestd) the ContractAdapter returns issues like
            # "exit 2: invalid choice: 'inventory'".  Detect that and
            # fall through to the direct file read.
            if view.installed or not any(
                "invalid choice" in iss or "inventory" in iss
                for iss in view.issues
            ):
                return view

        return self._read_direct()

    def _read_direct(self) -> ClientView:
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
