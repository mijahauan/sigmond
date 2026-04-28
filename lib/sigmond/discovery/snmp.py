"""Network device SNMP probe — queries switches, routers, and other
network devices via SNMP.  Uses subprocess to call snmpwalk/snmpget
if available, otherwise returns empty list."""

from __future__ import annotations

import subprocess
import time
from typing import Callable, List, Optional

from ..environment import Environment, Observation


# SNMP OIDs of interest
_OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"      # sysDescr
_OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"     # sysUpTime
_OID_SYS_CONTACT = "1.3.6.1.2.1.1.4.0"    # sysContact
_OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"       # sysName
_OID_IF_NUMBER = "1.3.6.1.2.1.2.1.0"      # ifNumber
_OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"     # ifDescr


def _default_snmp_runner(args: List[str], timeout: float) -> str:
    """Run snmp* command via subprocess."""
    try:
        proc = subprocess.run(
            args,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def probe(env: Environment, *,
          timeout: float = 3.0,
          limiter=None,
          snmp_runner: Optional[Callable] = _default_snmp_runner,
          ) -> list[Observation]:
    if env.discovery.passive_only:
        return []

    # Check if snmp tools are available
    if snmp_runner is None:
        return []

    now = time.time()
    out: list[Observation] = []

    for n in env.network_devices:
        out.append(_probe_one(n, snmp_runner, timeout, now))

    return out


def _probe_one(declared, snmp_runner, timeout, now) -> Observation:
    endpoint = declared.host

    # Build snmpget command
    community = declared.community or "public"
    args = [
        "snmpget",
        "-v", "2c",
        "-c", community,
        "-t", str(timeout),
        "-Ov",  # OID as string, value only
        endpoint,
        _OID_SYS_DESCR,
        _OID_SYS_UPTIME,
        _OID_SYS_NAME,
    ]

    output = snmp_runner(args, timeout)

    if not output:
        return Observation(
            source="snmp", kind="network_device", id=declared.id,
            endpoint=endpoint, fields={}, observed_at=now,
            ok=False, error="snmpget failed or snmp tools not installed",
        )

    fields: dict = _parse_snmp_output(output)
    fields["kind"] = declared.kind

    return Observation(
        source="snmp", kind="network_device", id=declared.id,
        endpoint=endpoint, fields=fields, observed_at=now, ok=True,
    )


def _parse_snmp_output(output: str) -> dict:
    """Parse snmpget output into fields dict."""
    out: dict = {}

    for line in output.splitlines():
        if "=" not in line:
            continue

        # Format: OID = TYPE VALUE  or  OID = VALUE
        parts = line.split("=", 1)
        if len(parts) != 2:
            continue

        oid = parts[0].strip()
        value = parts[1].strip()

        # Remove type prefix (e.g., "STRING: ", "Timeticks: ")
        for prefix in ("STRING:", "Timeticks:", "INTEGER:", "OID:", "Hex-STRING:"):
            if value.startswith(prefix):
                value = value[len(prefix):].strip()
                break

        if _OID_SYS_DESCR in oid:
            out["description"] = value.strip('"')
        elif _OID_SYS_UPTIME in oid:
            out["uptime"] = value
        elif _OID_SYS_NAME in oid:
            out["name"] = value.strip('"')

    return out