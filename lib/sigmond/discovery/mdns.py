"""mDNS / Avahi browse — passive discovery of local peers.

Shell out to `avahi-browse -rpt <service>` and parse the `=;…` lines
Avahi emits.  No new runtime deps; if `avahi-browse` is missing we return
an empty list with a single failed Observation recording the reason.

One ``avahi-browse`` invocation per service type — passing multiple
positional service args to a single ``avahi-browse`` call errors with
"Too many arguments" (the binary accepts at most one).  Before the
2026-05-19 fix this module silently returned zero observations on
every host that had more than one configured service; ka9q-radio
shows up as ``_ka9q-ctl._udp`` and was missing from every probe
report despite the LAN being full of radiods.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Callable

from ..environment import Environment, Observation


# Services relevant to a HamSCI site.  ka9q-radio (since 2025) and KiwiSDR
# both publish mDNS; chrony advertises NTP.  Add new service types here
# rather than spreading them across the codebase.
SERVICES = (
    "_kiwisdr._tcp",
    "_ntp._udp",
    "_hftimestd._tcp",   # future: hf-timestd peers could advertise
    "_ka9q-ctl._udp",
)


def _default_runner(services: tuple, timeout: float) -> str:
    """Run ``avahi-browse -rpt <svc>`` once per service and concatenate
    the resolved-line output.

    ``timeout`` is the *per-service* budget, not the aggregate.  In
    practice each call returns in well under a second once the cache
    is warm; the timeout exists to bound startup latency if Avahi is
    misbehaving on the host.
    """
    if shutil.which("avahi-browse") is None:
        raise FileNotFoundError("avahi-browse not found on PATH")
    chunks: list[str] = []
    for svc in services:
        # -rpt: resolve, parseable, terminate (single-shot).
        proc = subprocess.run(
            ["avahi-browse", "-rpt", svc],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        chunks.append(proc.stdout)
    return "".join(chunks)


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


def _decode_avahi_escapes(s: str) -> str:
    """Decode Avahi's ``\\NNN`` octal escapes back to the original char.

    radiod publishes service names like ``AC0G\\032\\064EM38ww\\032B1``
    in the parseable output (space=040 oct = 032 decimal? — Avahi uses
    DECIMAL three-digit escapes, not octal).  Translate those back to
    the human-readable form before exposing in ``fields``.
    """
    import re as _re

    def _sub(m):
        try:
            return chr(int(m.group(1)))
        except (ValueError, OverflowError):
            return m.group(0)
    return _re.sub(r"\\(\d{3})", _sub, s)


def _parse(stdout: str, now: float) -> list[Observation]:
    """Parse avahi-browse -pt output.  Resolved lines start with '='.

    The same logical service is announced separately on every (iface,
    proto) pair the host listens on — ens18/IPv4, ens18/IPv6, lo/IPv4.
    Keep only one Observation per (kind, mdns_name, address) so a
    single radiod doesn't appear three times in inventory.
    """
    out: list[Observation] = []
    seen: set[tuple[str, str, str]] = set()
    for line in stdout.splitlines():
        if not line.startswith("="):
            continue
        parts = line.split(";")
        # Expected fields (resolved, parseable, terminate mode):
        # =;IF;PROTO;NAME;TYPE;DOMAIN;HOSTNAME;ADDRESS;PORT;TXT
        if len(parts) < 9:
            continue
        iface, proto = parts[1], parts[2]
        name = _decode_avahi_escapes(parts[3])
        svc_type = parts[4]
        hostname, address, port = parts[6], parts[7], parts[8]
        txt = parts[9] if len(parts) > 9 else ""
        kind = _KIND_FOR_SERVICE.get(svc_type, "")
        if not kind:
            continue
        key = (kind, name, address)
        if key in seen:
            continue
        seen.add(key)
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
