"""Multicast probe — listens briefly on each declared radiod's status
group to enumerate channels, then polls radiod for frontend info.

Wraps ka9q-python's `discover_channels` + `RadiodControl` — the same
building blocks already used by [tui/screens/radiod.py].
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from ..environment import Environment, Observation


def _default_discoverer(status_dns: str, listen_duration: float) -> dict:
    """Production discoverer that shells into ka9q-python."""
    try:
        from ka9q import discover_channels                         # type: ignore
    except ImportError as e:
        raise RuntimeError(f"ka9q-python not available: {e}") from e
    return discover_channels(status_dns, listen_duration=listen_duration)


def _default_control_factory(status_dns: str):
    try:
        from ka9q import RadiodControl                              # type: ignore
    except ImportError as e:
        raise RuntimeError(f"ka9q-python not available: {e}") from e
    return RadiodControl(status_dns)


def probe(env: Environment, *,
          timeout: float = 2.5,
          limiter=None,
          discoverer: Callable = _default_discoverer,
          control_factory: Optional[Callable] = _default_control_factory
          ) -> list[Observation]:
    if not env.discovery.multicast_enabled:
        return []

    out: list[Observation] = []
    listen = max(1.0, min(timeout, 5.0))
    now = time.time()

    for r in env.radiods:
        if not r.status_dns:
            continue
        try:
            channels = discoverer(r.status_dns, listen)
        except Exception as e:                   # noqa: BLE001
            out.append(Observation(
                source="multicast", kind="radiod", id=r.id,
                endpoint=r.status_dns, fields={},
                observed_at=now, ok=False,
                error=f"discover_channels: {e}",
            ))
            continue

        fields: dict = {
            "channels": [],
        }
        first_ssrc = None
        for ssrc, ch in (channels or {}).items():
            first_ssrc = first_ssrc or ssrc
            fields["channels"].append({
                "ssrc":        ssrc,
                "frequency":   getattr(ch, "frequency", None),
                "preset":      getattr(ch, "preset", None),
                "sample_rate": getattr(ch, "sample_rate", None),
            })

        if first_ssrc is not None and control_factory is not None:
            try:
                with control_factory(r.status_dns) as control:
                    status = control.poll_status(first_ssrc, timeout=listen)
                    if status is not None:
                        d = status.to_dict() if hasattr(status, "to_dict") else {}
                        fe = d.get("frontend", {}) or {}
                        fields["frontend"] = {
                            "gpsdo_lock":      fe.get("lock"),
                            "calibration_ppm": fe.get("calibrate"),
                            "reference_hz":    fe.get("reference"),
                        }
            except Exception as e:                # noqa: BLE001
                fields["frontend_error"] = str(e)

        out.append(Observation(
            source="multicast", kind="radiod", id=r.id,
            endpoint=r.status_dns, fields=fields,
            observed_at=now, ok=True,
        ))

    return out
