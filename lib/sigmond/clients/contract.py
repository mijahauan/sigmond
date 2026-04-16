"""Generic client-contract adapter.

Phase 2+: when a client implements the HamSCI client contract
(docs/CLIENT-CONTRACT.md), sigmond doesn't need a bespoke adapter
module — this generic ContractAdapter shells out to
`<binary> inventory --json` and `<binary> validate --json` and
translates the result into sigmond's internal ClientView.

A per-client adapter (e.g. clients/hftimestd.py) can subclass this and
override `binary_path` / `name`, then fall back to a hand-rolled file
read when the binary is missing — that's the migration path during
Phase 2 retrofits.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .base import ClientAdapter, ClientView, DiskWrite, InstanceView


SUPPORTED_CONTRACT_VERSION = "0.4"


class ContractAdapter(ClientAdapter):
    """Default adapter for any contract-conformant client."""

    name: str = ""               # subclass overrides
    binary: str = ""             # CLI command to invoke
    timeout_sec: float = 5.0     # inventory should be near-instant

    def find_binary(self) -> Optional[str]:
        """Locate the client's binary on $PATH (subclasses can override
        to look in venvs or non-standard locations)."""
        if not self.binary:
            return None
        return shutil.which(self.binary)

    def read_view(self) -> ClientView:
        view = ClientView(client_type=self.name or self.binary)
        binary = self.find_binary()
        if not binary:
            view.issues.append(f"{self.binary or 'client'}: not found on PATH")
            return view

        try:
            proc = subprocess.run(
                [binary, 'inventory', '--json'],
                capture_output=True, text=True,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired:
            view.issues.append(f"{binary} inventory: timed out after {self.timeout_sec}s")
            return view
        except OSError as exc:
            view.issues.append(f"{binary} inventory: {exc}")
            return view

        if proc.returncode != 0:
            stderr = (proc.stderr or '').strip()[:400]
            view.issues.append(
                f"{binary} inventory exit {proc.returncode}: {stderr}"
            )
            return view

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            view.issues.append(f"{binary} inventory: malformed JSON: {exc}")
            return view

        view.installed = True
        cfg_path = data.get('config_path')
        if cfg_path:
            view.config_path = Path(cfg_path)

        version = data.get('contract_version')
        if version:
            view.contract_version = str(version)
            if view.contract_version != SUPPORTED_CONTRACT_VERSION:
                view.issues.append(
                    f"contract_version mismatch: client reports "
                    f"{view.contract_version}, sigmond supports "
                    f"{SUPPORTED_CONTRACT_VERSION}"
                )

        if data.get('log_paths'):
            view.log_paths = data['log_paths']
        if data.get('log_level'):
            view.log_level = str(data['log_level'])

        deploy_path = data.get('deploy_toml_path')
        if deploy_path:
            view.deploy_toml_path = Path(deploy_path)

        for raw in (data.get('instances') or []):
            view.instances.append(_instance_from_contract(raw))

        for issue in (data.get('issues') or []):
            sev = issue.get('severity', 'warn')
            msg = issue.get('message', '')
            view.issues.append(f"[{sev}] {msg}")

        return view

    def validate_native(self) -> list:
        binary = self.find_binary()
        if not binary:
            return []
        try:
            proc = subprocess.run(
                [binary, 'validate', '--json'],
                capture_output=True, text=True,
                timeout=self.timeout_sec,
            )
            return (json.loads(proc.stdout) or {}).get('issues', [])
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return []


def _instance_from_contract(raw: dict) -> InstanceView:
    """Translate a contract inventory instance dict into an InstanceView."""
    iv = InstanceView(
        instance=raw.get('instance', 'default'),
        radiod_id=raw.get('radiod_id'),
        required_cores=list(raw.get('required_cores') or []),
        preferred_cores=str(raw.get('preferred_cores') or ''),
        frequencies_hz=list(raw.get('frequencies_hz') or []),
        ka9q_channels=int(raw.get('ka9q_channels') or 0),
        uses_timing_calibration=bool(raw.get('uses_timing_calibration', False)),
        provides_timing_calibration=bool(raw.get('provides_timing_calibration', False)),
    )
    for dw in (raw.get('disk_writes') or []):
        iv.disk_writes.append(DiskWrite(
            path=dw.get('path', ''),
            mb_per_day=float(dw.get('mb_per_day') or 0.0),
            retention_days=int(dw.get('retention_days') or 0),
        ))
    if raw.get('data_destination') is not None:
        iv.data_destination = str(raw['data_destination'])
    if raw.get('chain_delay_ns_applied') is not None:
        iv.chain_delay_ns_applied = int(raw['chain_delay_ns_applied'])
    if 'radiod_status_dns' in raw:
        iv.radiod_status_dns = str(raw['radiod_status_dns'] or '')
    if 'radiod_samprate_hz' in raw:
        iv.radiod_samprate_hz = int(raw['radiod_samprate_hz'] or 0)
    if 'radiod_max_channels' in raw:
        iv.radiod_max_channels = int(raw['radiod_max_channels'] or 0)
    return iv
