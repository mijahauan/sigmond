"""ka9q-web HTTP probe — GETs /status.json against each declared ka9q-web
instance.  stdlib urllib; 3s timeout."""

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
    for w in env.ka9q_webs:
        out.append(_probe_one(w, urlopen, timeout, now))
    return out


def _probe_one(declared, urlopen, timeout, now) -> Observation:
    base = f"http://{declared.host}:{declared.port}"
    endpoint = f"{declared.host}:{declared.port}"

    # Try /status.json first (preferred), fall back to /status
    status = _fetch(urlopen, f"{base}/status.json", timeout)
    if isinstance(status, Exception):
        # Fall back to /status
        status = _fetch(urlopen, f"{base}/status", timeout)
        if isinstance(status, Exception):
            return Observation(
                source="http_ka9q", kind="ka9q_web", id=declared.id,
                endpoint=endpoint, fields={}, observed_at=now,
                ok=False, error=f"HTTP probe failed: {status}",
            )

    fields: dict = _parse_ka9q_status(status)

    return Observation(
        source="http_ka9q", kind="ka9q_web", id=declared.id,
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
# Parsers — ka9q-web /status.json returns JSON, /status is line-oriented.
# ---------------------------------------------------------------------------

def _parse_ka9q_status(body: str) -> dict:
    out: dict = {}
    
    # Try JSON first
    try:
        data = json.loads(body)
        out["version"] = data.get("version", "")
        out["name"] = data.get("name", "")
        out["uptime"] = data.get("uptime", "")
        out["channels"] = data.get("channels", 0)
        out["mode"] = data.get("mode", "")
        out["cpu_percent"] = data.get("cpu_percent", 0)
        out["memory_mb"] = data.get("memory_mb", 0)
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
        
        if key in ("version", "name", "uptime", "mode"):
            out[key] = val
        elif key in ("channels", "cpu_percent", "memory_mb"):
            try:
                out[key] = int(val)
            except ValueError:
                pass
    
    return out