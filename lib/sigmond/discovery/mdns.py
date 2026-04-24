"""mDNS / Avahi browse — passive discovery of local peers.

Shell out to `avahi-browse -rpt <service>…` and parse the `=;…` lines
Avahi emits.  No new runtime deps; if `avahi-browse` is missing we return
an empty list with a single failed Observation recording the reason.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Callable

from ..environment import Environment, Observation


# Services relevant to a HamSCI site.  `_ka9q-ctl._udp` is aspirational —
# ka9q-radio does not publish mDNS today, but KiwiSDR and NTP/chrony do.
SERVICES = (
    "_kiwisdr._tcp",
    "_ntp._udp",
    "_hftimestd._tcp",   # future: hf-timestd peers could advertise
    "_ka9q-ctl._udp",
)


def _default_runner(services: tuple, timeout: float) -> str:
    if shutil.which("avahi-browse") is None:
        raise FileNotFoundError("avahi-browse not found on PATH")
    args = ["avahi-browse", "-rpt"]
    args.extend(services)
    # -rpt: resolve, parseable, terminate (single-shot).
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.stdout


def probe(env: Environment, *,
          timeout: float = 3.0,
          limiter=None,
          runner: Callable = _default_runner) -> list[Observation]:
    if not env.discovery.mdns_enabled:
        return []

    now = time.time()
    try:
        stdout = runner(SERVICES, timeout)
    except FileNotFoundError as e:
        return [Observation(
            source="mdns", kind="", id=None, endpoint="", fields={},
            observed_at=now, ok=False, error=str(e),
        )]
    except subprocess.TimeoutExpired:
        return [Observation(
            source="mdns", kind="", id=None, endpoint="", fields={},
            observed_at=now, ok=False, error=f"avahi-browse timed out after {timeout}s",
        )]
    except Exception as e:                      # noqa: BLE001 — surface any runner error
        return [Observation(
            source="mdns", kind="", id=None, endpoint="", fields={},
            observed_at=now, ok=False, error=f"avahi-browse failed: {e}",
        )]

    return _parse(stdout, now)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_KIND_FOR_SERVICE = {
    "_kiwisdr._tcp":   "kiwisdr",
    "_ntp._udp":       "time_source",
    "_hftimestd._tcp": "time_source",
    "_ka9q-ctl._udp":  "radiod",
}


def _parse(stdout: str, now: float) -> list[Observation]:
    """Parse avahi-browse -pt output.  Resolved lines start with '='."""
    out: list[Observation] = []
    for line in stdout.splitlines():
        if not line.startswith("="):
            continue
        parts = line.split(";")
        # Expected fields (resolved, parseable, terminate mode):
        # =;IF;PROTO;NAME;TYPE;DOMAIN;HOSTNAME;ADDRESS;PORT;TXT
        if len(parts) < 9:
            continue
        iface, proto, name, svc_type = parts[1], parts[2], parts[3], parts[4]
        hostname, address, port = parts[6], parts[7], parts[8]
        txt = parts[9] if len(parts) > 9 else ""
        kind = _KIND_FOR_SERVICE.get(svc_type, "")
        if not kind:
            continue
        fields = {
            "mdns_name":    name,
            "mdns_service": svc_type,
            "iface":        iface,
            "proto":        proto,
            "address":      address,
            "txt":          txt.strip('"'),
        }
        # For NTP we want to differentiate from hf-timestd specifically.
        if svc_type == "_hftimestd._tcp":
            fields["time_kind"] = "hf-timestd"
        elif svc_type == "_ntp._udp":
            fields["time_kind"] = "ntp"
        endpoint = f"{hostname}:{port}" if port else hostname
        out.append(Observation(
            source="mdns",
            kind=kind,
            id=None,
            endpoint=endpoint,
            fields=fields,
            observed_at=now,
            ok=True,
        ))
    return out
