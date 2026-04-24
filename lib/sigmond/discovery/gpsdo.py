"""GPSDO probe — reads gpsdo-monitor's authority.json file(s) from the
local filesystem.  Remote hosts are seen only via mDNS/NTP in v1; SSH
into other hosts is out of scope.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from ..environment import Environment, Observation


def _default_reader(path: str) -> str:
    return Path(path).read_text()


def probe(env: Environment, *,
          timeout: float = 1.0,
          limiter=None,
          reader: Callable = _default_reader,
          ) -> list[Observation]:
    now = time.time()
    out: list[Observation] = []
    for g in env.gpsdos:
        if g.host not in ("localhost", "127.0.0.1", "::1", ""):
            # Remote — relies on mDNS/NTP to spot it.  Emit nothing here.
            continue
        if not g.authority_json:
            continue
        try:
            raw = reader(g.authority_json)
        except FileNotFoundError as e:
            out.append(Observation(
                source="gpsdo", kind="gpsdo", id=g.id,
                endpoint=g.authority_json, fields={},
                observed_at=now, ok=False,
                error=f"authority.json missing: {e}",
            ))
            continue
        except Exception as e:                   # noqa: BLE001
            out.append(Observation(
                source="gpsdo", kind="gpsdo", id=g.id,
                endpoint=g.authority_json, fields={},
                observed_at=now, ok=False,
                error=f"read failed: {e}",
            ))
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            out.append(Observation(
                source="gpsdo", kind="gpsdo", id=g.id,
                endpoint=g.authority_json, fields={},
                observed_at=now, ok=False,
                error=f"invalid JSON: {e}",
            ))
            continue

        fields = _extract_fields(data)
        out.append(Observation(
            source="gpsdo", kind="gpsdo", id=g.id,
            endpoint=g.authority_json,
            fields=fields, observed_at=now, ok=True,
        ))
    return out


def _extract_fields(data: dict) -> dict:
    """Pull the stable set of fields from gpsdo-monitor's authority.json.

    Schema varies by version, so we tolerate absent keys."""
    out: dict = {
        "locked":        _bool_or_none(data.get("locked")),
        "sats":          data.get("sats"),
        "fix_type":      data.get("fix_type") or data.get("fix"),
        "tic_seconds":   data.get("tic_seconds") or data.get("tic"),
        "holdover":      data.get("holdover"),
        "authority":     data.get("authority"),
        "last_update":   data.get("last_update") or data.get("updated_at"),
        "device":        data.get("device") or data.get("serial"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _bool_or_none(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "yes", "locked", "1"):
        return True
    if s in ("false", "no", "unlocked", "0"):
        return False
    return None
