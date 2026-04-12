"""ClientAdapter base class and the view dataclasses adapters return.

The view types mirror what `<client> inventory --json` will return once
the client contract lands.  For Phase 1 each adapter builds them by
reading the client's native config directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class DiskWrite:
    path:           str
    mb_per_day:     float = 0.0
    retention_days: int = 0


@dataclass
class InstanceView:
    instance:                   str
    radiod_id:                  Optional[str] = None
    required_cores:             list = field(default_factory=list)
    preferred_cores:            str = ""
    frequencies_hz:             list = field(default_factory=list)
    ka9q_channels:              int = 0
    disk_writes:                list = field(default_factory=list)    # list[DiskWrite]
    uses_timing_calibration:    bool = False
    provides_timing_calibration: bool = False
    data_destination:           Optional[str] = None                  # v0.2 §7
    chain_delay_ns_applied:     Optional[int] = None                  # v0.2 §8
    # radiod-only metadata: populated by RadiodAdapter, ignored elsewhere.
    radiod_samprate_hz:         int = 0
    radiod_status_dns:          str = ""
    radiod_max_channels:        int = 0


@dataclass
class ClientView:
    client_type:      str
    installed:        bool = False
    config_path:      Optional[Path] = None
    contract_version: Optional[str] = None                            # v0.2 added
    log_paths:        Optional[dict] = None                           # v0.3 §10
    log_level:        Optional[str] = None                            # v0.3 §11
    instances:        list = field(default_factory=list)              # list[InstanceView]
    issues:           list = field(default_factory=list)              # list[str]


class ClientAdapter:
    """Base class.  Phase 1 adapters only need read_view()."""

    name: str = "<override-me>"

    def read_view(self) -> ClientView:
        """Return a read-only snapshot of the client's coordination view.

        Implementations must never raise on missing config; instead
        return a ClientView with installed=False and (optionally) an
        entry in .issues describing what was missing.
        """
        raise NotImplementedError

    def validate_native(self) -> list:
        """Per-client validate pass.  Returns list of ValidationIssue dicts.

        Phase 1 may return an empty list.
        """
        return []
