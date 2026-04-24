"""KiwiSDR HTTP probe — GETs /status and /gps against each declared
KiwiSDR.  stdlib urllib; 3s timeout; one or two requests per host."""

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
    for k in env.kiwisdrs:
        out.append(_probe_one(k, urlopen, timeout, now))
    return out


def _probe_one(declared, urlopen, timeout, now) -> Observation:
    base = f"http://{declared.host}:{declared.port}"
    endpoint = f"{declared.host}:{declared.port}"

    status = _fetch(urlopen, f"{base}/status", timeout)
    if isinstance(status, Exception):
        return Observation(
            source="http_kiwisdr", kind="kiwisdr", id=declared.id,
            endpoint=endpoint, fields={}, observed_at=now,
            ok=False, error=f"/status failed: {status}",
        )

    fields: dict = _parse_kiwi_status(status)

    # /gps is supplementary — failure here is not fatal to the probe.
    gps = _fetch(urlopen, f"{base}/gps", timeout)
    if not isinstance(gps, Exception):
        fields.update(_parse_kiwi_gps(gps))

    return Observation(
        source="http_kiwisdr", kind="kiwisdr", id=declared.id,
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
# Parsers — KiwiSDR /status is line-oriented KEY=VALUE, /gps is JSON-ish.
# ---------------------------------------------------------------------------

_STATUS_KEYS_OF_INTEREST = {
    "name":          "name",
    "sw_name":       "sw_name",
    "sw_version":    "sw_version",
    "users":         "users",
    "users_max":     "users_max",
    "offline":       "offline",
    "beacon":        "beacon",
    "gps":           "gps_raw",
    "fixes":         "fixes",
    "antenna":       "antenna",
    "loc":           "loc",
    "grid":          "grid",
    "asl":           "asl",
    "uptime":        "uptime",
}


def _parse_kiwi_status(body: str) -> dict:
    out: dict = {}
    for line in (body or "").splitlines():
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        mapped = _STATUS_KEYS_OF_INTEREST.get(key)
        if not mapped:
            continue
        # Normalise ints where obvious.
        if mapped in ("users", "users_max", "fixes"):
            try:
                out[mapped] = int(val)
                continue
            except ValueError:
                pass
        out[mapped] = val
    return out


def _parse_kiwi_gps(body: str) -> dict:
    body = (body or "").strip()
    if not body:
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {"gps_raw": body[:200]}

    out: dict = {}
    # Observed fields vary by firmware; extract what's useful.
    if "fixes" in data:
        out["fixes"] = data.get("fixes")
    if "lat" in data and "lon" in data:
        out["lat"] = data.get("lat")
        out["lon"] = data.get("lon")
    # Common boolean shapes for fix state
    has_fix = data.get("fix") or data.get("has_fix")
    if has_fix is not None:
        out["gps_fix"] = bool(has_fix)
    elif isinstance(data.get("fixes"), int):
        out["gps_fix"] = data["fixes"] > 0
    return out
