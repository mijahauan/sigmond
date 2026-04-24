"""NTP probe — SNTPv4 mode-3 query against each declared NTP/hf-timestd
time_source.  Stdlib only (socket + struct)."""

from __future__ import annotations

import socket
import struct
import subprocess
import time
from typing import Callable, Optional

from ..environment import Environment, Observation


# NTP epoch is 1900-01-01; Unix epoch is 1970-01-01.
_NTP_EPOCH_OFFSET = 2_208_988_800
_MODE_CLIENT = 3
_VERSION = 4
_NTP_PORT = 123


def _default_socket_factory():
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _default_chronyc_runner(timeout: float) -> str:
    """Run `chronyc -c sources` for localhost NTP visibility."""
    try:
        proc = subprocess.run(
            ["chronyc", "-c", "sources"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def probe(env: Environment, *,
          timeout: float = 2.0,
          limiter=None,
          socket_factory: Callable = _default_socket_factory,
          chronyc_runner: Optional[Callable] = _default_chronyc_runner,
          ) -> list[Observation]:
    if env.discovery.passive_only:
        return []

    now = time.time()
    out: list[Observation] = []

    for t in env.time_sources:
        if t.kind == "ptp":
            continue                             # v1: PTP is out of scope for active probes
        if t.host in ("localhost", "127.0.0.1", "::1") and chronyc_runner is not None:
            out.append(_probe_local_chrony(t, chronyc_runner, timeout, now))
            continue
        out.append(_probe_remote(t, socket_factory, timeout, now))

    return out


# ---------------------------------------------------------------------------
# Remote SNTP query
# ---------------------------------------------------------------------------

def _probe_remote(declared, socket_factory, timeout, now) -> Observation:
    packet = struct.pack("!B B B b 11I",
                         (0 << 6) | (_VERSION << 3) | _MODE_CLIENT,
                         0, 0, 0,
                         0, 0, 0, 0,
                         0, 0, 0, 0, 0, 0, 0)
    try:
        sock = socket_factory()
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (declared.host, _NTP_PORT))
            data, _ = sock.recvfrom(1024)
        finally:
            sock.close()
    except Exception as e:                       # noqa: BLE001
        return Observation(
            source="ntp", kind="time_source", id=declared.id,
            endpoint=f"{declared.host}:{_NTP_PORT}",
            fields={}, observed_at=now, ok=False,
            error=f"ntp query failed: {e}",
        )

    if len(data) < 48:
        return Observation(
            source="ntp", kind="time_source", id=declared.id,
            endpoint=f"{declared.host}:{_NTP_PORT}",
            fields={}, observed_at=now, ok=False,
            error=f"short ntp response ({len(data)} bytes)",
        )

    li_vn_mode, stratum, poll, precision = struct.unpack("!B B B b", data[:4])
    root_delay      = struct.unpack("!I", data[4:8])[0] / 65536.0
    root_dispersion = struct.unpack("!I", data[8:12])[0] / 65536.0
    refid           = data[12:16]
    tx_sec, tx_frac = struct.unpack("!II", data[40:48])
    tx_unix = (tx_sec - _NTP_EPOCH_OFFSET) + tx_frac / 2**32

    return Observation(
        source="ntp", kind="time_source", id=declared.id,
        endpoint=f"{declared.host}:{_NTP_PORT}",
        fields={
            "stratum":         stratum,
            "poll":            poll,
            "precision":       precision,
            "root_delay":      root_delay,
            "root_dispersion": root_dispersion,
            "refid":           _format_refid(refid, stratum),
            "offset_seconds":  tx_unix - now,
            "mode":            li_vn_mode & 0b111,
        },
        observed_at=now, ok=True,
    )


def _format_refid(refid: bytes, stratum: int) -> str:
    if stratum == 1:
        # Stratum-1: a 4-char source identifier (e.g. "GPS", "PPS").
        return refid.rstrip(b"\x00").decode("ascii", errors="replace")
    # Stratum ≥2: refid is the IPv4 of the upstream server.
    return ".".join(str(b) for b in refid)


# ---------------------------------------------------------------------------
# Localhost: parse `chronyc -c sources`
# ---------------------------------------------------------------------------

def _probe_local_chrony(declared, runner, timeout, now) -> Observation:
    out = runner(timeout)
    if not out:
        return Observation(
            source="ntp", kind="time_source", id=declared.id,
            endpoint="chronyc sources",
            fields={}, observed_at=now, ok=False,
            error="chronyc unavailable or empty",
        )

    sources: list = []
    best_stratum: Optional[int] = None
    for raw in out.splitlines():
        parts = raw.split(",")
        # chronyc -c sources CSV columns (chrony ≥4):
        # mode,state,name,stratum,poll,reach,lastRx,lastSample,
        # originalOffset,error,unused,unused,unused,unused,unused
        if len(parts) < 8:
            continue
        try:
            stratum = int(parts[3])
        except ValueError:
            continue
        entry = {
            "mode":    parts[0],
            "state":   parts[1],
            "name":    parts[2],
            "stratum": stratum,
            "poll":    parts[4],
            "reach":   parts[5],
            "last_rx": parts[6],
        }
        sources.append(entry)
        if parts[1] in ("*", "sync") and (best_stratum is None or stratum < best_stratum):
            best_stratum = stratum

    return Observation(
        source="ntp", kind="time_source", id=declared.id,
        endpoint="chronyc sources",
        fields={
            "sources": sources,
            "stratum": best_stratum if best_stratum is not None else 0,
        },
        observed_at=now, ok=True,
    )
