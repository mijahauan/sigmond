"""USB SDR probe — scans the local USB bus via lsusb and identifies
attached SDR devices by vendor/product ID.

Returns Observation(source="usb_sdr", kind="sdr", ...) for each device
found.  Serial numbers are pulled from lsusb -v when available (requires
read access to the USB device node, so may fail without udev rules).

This is a local-only probe; remote USB SDRs are discovered via the
ka9q-radio frontend query (source="ka9q_fe").
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Callable

from ..environment import Environment, Observation


# ---------------------------------------------------------------------------
# Known SDR USB VID:PID table
# ---------------------------------------------------------------------------

# (vid, pid) -> (friendly_type, chip_description)
KNOWN_SDR_DEVICES: dict[tuple[str, str], tuple[str, str]] = {
    ("0bda", "2832"): ("RTL-SDR",   "RTL2832U"),
    ("0bda", "2838"): ("RTL-SDR",   "RTL2832U"),
    ("0bda", "2831"): ("RTL-SDR",   "RTL2831U"),
    ("0bda", "2837"): ("RTL-SDR",   "RTL2837U"),
    ("0bda", "2840"): ("RTL-SDR",   "RTL2840"),
    ("04b4", "00bc"): ("RX-888",    "Cypress FX3"),    # RX-888 operating mode
    ("04b4", "00f1"): ("RX-888 Mk2","Cypress FX3"),   # RX-888 Mk2 operating mode
    ("04b4", "00f3"): ("RX-888 DFU","Cypress FX3"),   # RX-888 pre-firmware (DFU mode; radiod loads firmware)
    ("1d50", "6089"): ("HackRF",    "HackRF One"),
    ("1d50", "60a1"): ("Airspy",    "Airspy R2"),
    ("03eb", "800c"): ("Airspy HF+","Airspy HF+"),
    ("1df7", "2500"): ("SDRplay",   "RSP1"),
    ("1df7", "3000"): ("SDRplay",   "RSP1A"),
    ("1df7", "3010"): ("SDRplay",   "RSPdx"),
    ("1df7", "3020"): ("SDRplay",   "RSP2"),
    ("04d8", "fb31"): ("FunCube",   "FCD Pro+"),
    ("2500", "0020"): ("USRP",      "Ettus USRP B200"),
    ("0456", "b673"): ("ADALM-Pluto","AD9363"),
    ("f4b3", "0100"): ("RX-888",    "RX-888 MkII"),    # reported VID:PID on some firmware
}


def _default_lsusb(verbose: bool = False) -> str:
    cmd = ['lsusb', '-v'] if verbose else ['lsusb']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return result.stdout


def probe(env: Environment, *,
          timeout: float = 5.0,
          limiter=None,
          lsusb_runner: Callable = _default_lsusb,
          extract_serial: bool = True,
          ) -> list[Observation]:
    """Scan local USB bus for SDR devices.

    When ``extract_serial`` is True (the default), the probe also runs
    ``lsusb -v`` and pulls the iSerial number for each detected SDR.
    Serials let the radiod configurator pin a config to a specific
    physical SDR via the universal ``serial = "..."`` key recognised
    by every ka9q-radio frontend driver (rx888, airspy, airspyhf,
    sdrplay).  Serial extraction needs read access to the USB device
    nodes, which usually requires udev rules for the SDR vendor or
    root; when access is denied the iSerial line simply won't be
    present and the field comes back empty.
    """
    now = time.time()
    try:
        output = lsusb_runner(verbose=False)
    except Exception as e:
        return [Observation(
            source="usb_sdr", kind="sdr", id=None,
            endpoint="usb", fields={},
            observed_at=now, ok=False,
            error=f"lsusb failed: {e}",
        )]

    devices = _parse_lsusb(output)
    if not devices:
        return []

    serials: dict[tuple[str, str, str], str] = {}
    if extract_serial:
        try:
            verbose_output = lsusb_runner(verbose=True)
        except Exception:
            verbose_output = ""
        serials = _parse_iserials(verbose_output)

    out: list[Observation] = []
    idx_by_type: dict[tuple[str, str], int] = {}

    for dev in devices:
        key = (dev["vid"], dev["pid"])
        sdr_type, chip = KNOWN_SDR_DEVICES[key]
        n = idx_by_type.get(key, 0)
        idx_by_type[key] = n + 1

        serial = serials.get((dev["bus"], dev["device"],
                              dev["vid"] + ":" + dev["pid"]), "")

        obs_id = f"usb:{dev['vid']}:{dev['pid']}:{n}"
        out.append(Observation(
            source="usb_sdr",
            kind="sdr",
            id=obs_id,
            endpoint=f"bus {dev['bus']} dev {dev['device']}",
            fields={
                "sdr_type":  sdr_type,
                "chip":      chip,
                "vid":       dev["vid"],
                "pid":       dev["pid"],
                "usb_name":  dev.get("name", ""),
                "bus":       dev["bus"],
                "device":    dev["device"],
                "index":     n,
                "serial":    serial,
            },
            observed_at=now,
            ok=True,
        ))

    return out


# ---------------------------------------------------------------------------
# lsusb output parser
# ---------------------------------------------------------------------------

# Bus 003 Device 005: ID 04b4:00bc Cypress Semiconductor Corp. FX3 USB StreamER example
_LSUSB_RE = re.compile(
    r"Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)"
)


def _parse_lsusb(output: str) -> list[dict]:
    """Return list of dicts for lines matching a known SDR VID:PID."""
    found = []
    for line in output.splitlines():
        m = _LSUSB_RE.match(line.strip())
        if not m:
            continue
        bus, device, vid, pid, name = m.groups()
        if (vid.lower(), pid.lower()) in KNOWN_SDR_DEVICES:
            found.append({
                "bus":    bus.zfill(3),
                "device": device.zfill(3),
                "vid":    vid.lower(),
                "pid":    pid.lower(),
                "name":   name.strip(),
            })
    return found


# `lsusb -v` per-device header, e.g.:
#   Bus 003 Device 005: ID 04b4:00bc Cypress Semiconductor Corp. ...
# followed (eventually) by:
#       iSerial                 3 0123456789ABCDEF
_VERBOSE_HEADER_RE = re.compile(
    r"^Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})"
)
_ISERIAL_RE = re.compile(r"^\s*iSerial\s+\d+\s+(\S+)")


def _parse_iserials(verbose_output: str) -> dict[tuple[str, str, str], str]:
    """Walk ``lsusb -v`` output and return ``{(bus, dev, vid:pid) -> serial}``.

    Only entries whose VID:PID is in ``KNOWN_SDR_DEVICES`` are kept; lines
    without a permission-readable iSerial are silently skipped.  Serials
    are returned exactly as lsusb prints them — callers may want to strip
    a leading ``0x`` for use in radiod config (radiod accepts both forms,
    but the airspy convention is the bare hex).
    """
    out: dict[tuple[str, str, str], str] = {}
    cur_key: tuple[str, str, str] | None = None
    cur_known = False
    for line in (verbose_output or "").splitlines():
        m = _VERBOSE_HEADER_RE.match(line)
        if m:
            bus, dev, vid, pid = m.groups()
            cur_key = (bus.zfill(3), dev.zfill(3),
                       f"{vid.lower()}:{pid.lower()}")
            cur_known = (vid.lower(), pid.lower()) in KNOWN_SDR_DEVICES
            continue
        if not cur_known or cur_key is None:
            continue
        m2 = _ISERIAL_RE.match(line)
        if not m2:
            continue
        serial = m2.group(1).strip()
        # A device with no readable serial often shows iSerial as just "0"
        # (the index field) or empty; drop those.
        if serial in ("", "0"):
            continue
        out[cur_key] = serial
    return out
