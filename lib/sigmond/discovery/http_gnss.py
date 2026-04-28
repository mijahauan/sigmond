"""GNSS-VTEC HTTP probe — GETs /api/tec/status against each declared
GNSS-VTEC server (typically hf-timestd's web API).  stdlib urllib; 3s timeout."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from ..environment import Environment, Observation


def _default_urlopen(url: str, timeout: float):
    return urllib.request.urlopen(url, timeout=timeout)


def probe(env: Environment, *,
          timeout: float = 3.0,
          limiter=None,
          urlopen: Callable = _default_urlopen,
          ) -> list[Observation]:
    if env.discovery.passive_only:
        return []

    now = time.time()
    out: list[Observation] = []
    for v in env.gnss_vtecs:
        out.append(_probe_one(v, urlopen, timeout, now))
    return out


def _probe_one(declared, urlopen, timeout, now) -> Observation:
    base = f"http://{declared.host}:{declared.port}"
    endpoint = f"{declared.host}:{declared.port}"

    # Try /api/tec/status first (hf-timestd API), fall back to /status
    status = _fetch(urlopen, f"{base}/api/tec/status", timeout)
    if isinstance(status, Exception):
        # Fall back to /status
        status = _fetch(urlopen, f"{base}/status", timeout)
        if isinstance(status, Exception):
            return Observation(
                source="http_gnss", kind="gnss_vtec", id=declared.id,
                endpoint=endpoint, fields={}, observed_at=now,
                ok=False, error=f"HTTP probe failed: {status}",
            )

    fields: dict = _parse_gnss_status(status, declared.source)

    return Observation(
        source="http_gnss", kind="gnss_vtec", id=declared.id,
        endpoint=endpoint, fields=fields, observed_at=now, ok=True,
    )


def _fetch(urlopen: Callable, url: str, timeout: float):
    try:
        resp = urlopen(url, timeout)
        body = resp.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        return body
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return e
    except Exception as e:                       # noqa: BLE001
        return e


# ---------------------------------------------------------------------------
# Parsers — hf-timestd /api/tec/status returns JSON.
# ---------------------------------------------------------------------------

def _parse_gnss_status(body: str, source: str) -> dict:
    out: dict = {}
    out["source"] = source
    
    # Try JSON first
    try:
        data = json.loads(body)
        out["version"] = data.get("version", "")
        out["name"] = data.get("name", "")
        out["uptime"] = data.get("uptime", "")
        out["stations"] = data.get("stations", 0)
        out["satellites"] = data.get("satellites", 0)
        out["tec_ready"] = data.get("tec_ready", False)
        out["last_update"] = data.get("last_update", "")
        
        # Extract TEC-specific fields if available
        if "tec" in data:
            out["tec_min"] = data["tec"].get("min", 0)
            out["tec_max"] = data["tec"].get("max", 0)
            out["tec_mean"] = data["tec"].get("mean", 0)
        
        return out
    except (json.JSONDecodeError, AttributeError):
        pass
    
    # Fall back to line-oriented parsing
    for line in (body or "").splitlines():
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        
        if key in ("version", "name", "uptime", "source", "last_update"):
            out[key] = val
        elif key in ("stations", "satellites"):
            try:
                out[key] = int(val)
            except ValueError:
                pass
    
    return out