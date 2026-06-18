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


# Common locations where the hf-timestd CLI might live.  Order:
# canonical system install first, repo venv second, PATH last.
# Pre-fix we used PATH first, which on a developer host could
# resolve to a stale /home/<user>/.local/bin/hf-timestd shim
# pointing at a pre-consolidation source tree — the shim's import
# blew up before `inventory --json` could even run, ContractAdapter
# recorded contract_version=None, and the Overview screen showed
# "?".
_HFTIMESTD_BIN_CANDIDATES = (
    "/usr/local/bin/hf-timestd",
    "/opt/git/sigmond/hf-timestd/venv/bin/hf-timestd",
    "/opt/hf-timestd/venv/bin/hf-timestd",
)


class HfTimestdAdapter(ClientAdapter):
    name = "hf-timestd"

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or HF_TIMESTD_CONF

    def _candidate_binaries(self) -> list[str]:
        """Return every plausible hf-timestd CLI path on this host,
        canonical-first then PATH last."""
        seen: set[str] = set()
        out: list[str] = []
        for cand in _HFTIMESTD_BIN_CANDIDATES:
            if Path(cand).is_file() and cand not in seen:
                out.append(cand)
                seen.add(cand)
        on_path = shutil.which("hf-timestd")
        if on_path and on_path not in seen:
            out.append(on_path)
        return out

    def _find_binary(self) -> Optional[str]:
        # Back-compat for callers that just want "any" working path —
        # they're rare and they don't actually invoke the binary, so
        # returning the first candidate is fine.
        cands = self._candidate_binaries()
        return cands[0] if cands else None

    def read_view(self) -> ClientView:
        # Phase 2: prefer the contract surface if hf-timestd >= 6.12.x.
        # Try every candidate binary in canonical-first order so a
        # broken dev shim on PATH doesn't poison the result.
        last_view: Optional[ClientView] = None
        for binary in self._candidate_binaries():
            contract = ContractAdapter()
            contract.name = self.name
            contract.binary = binary
            view = contract.read_view()
            # A permission-denied parse is NOT a real fault — it just means the
            # contract binary ran as the operator and couldn't read the 0640
            # service-user config.  Fall through to _read_direct, which reads it
            # via sudo -n.  (Without this, `smd config show` shows a spurious
            # "failed to parse: Permission denied".)
            _perm = any("Permission denied" in iss for iss in view.issues)
            if view.installed and not _perm:
                return view
            # If the binary lacks an `inventory` subcommand (older
            # hf-timestd) we fall through to the direct file read.
            if any("invalid choice" in iss or "inventory" in iss
                   for iss in view.issues):
                last_view = view
                break    # don't keep probing other binaries — they're
                         # the same code, would fail the same way
            if not _perm:
                last_view = view

        if last_view is not None and last_view.installed:
            return last_view
        return self._read_direct()

    def _read_direct(self) -> ClientView:
        view = ClientView(client_type=self.name, config_path=self.config_path)
        if not self.config_path.exists():
            view.issues.append(f"{self.config_path} not present")
            return view

        import tomllib
        # timestd-config.toml is 0640 timestd:timestd, but `smd config show`
        # runs as the operator — a direct read hits EACCES.  Fall back to
        # passwordless `sudo -n cat` (no-op when already root) so we report the
        # real config instead of a spurious "failed to parse: Permission denied".
        try:
            text = self.config_path.read_text()
        except PermissionError:
            import os
            import subprocess
            sudo = [] if os.geteuid() == 0 else ['sudo', '-n']
            r = subprocess.run([*sudo, 'cat', str(self.config_path)],
                               capture_output=True, text=True, check=False)
            if r.returncode != 0:
                view.installed = True
                view.issues.append(
                    f"cannot read {self.config_path} (permission denied; "
                    f"run `smd config show` as root to inspect it)")
                return view
            text = r.stdout
        except OSError as exc:
            view.issues.append(f"failed to read {self.config_path}: {exc}")
            return view
        try:
            raw = tomllib.loads(text)
        except Exception as exc:
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
