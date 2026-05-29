"""Best-effort hardware-presence probes per catalog component.

Powers the Topology screen's "Detected" column so the operator can
make a default-deploy decision informed by what's actually plugged
into this host.

Each probe returns a `Presence` enum value:

  yes  — strong evidence the prerequisite hardware is on this host
  no   — strong evidence it is NOT here (probe ran, found nothing)
  na   — component has no hardware prerequisite (libraries, infra,
         remote-source clients) — the column should show "—"
  unknown — probe failed (subprocess error, missing tool, etc.)

The probes intentionally avoid expensive operations — no opening of
serial ports, no firmware queries — they're called every time the
operator opens the Topology screen.  Cheap-and-shallow: lsusb for
USB devices, filesystem glob for device nodes.

Mapping component → probe is a small table at the bottom of the
file; adding a new client that depends on new hardware is one line.
"""

from __future__ import annotations

import enum
import shutil
import subprocess
from pathlib import Path
from typing import Callable


class Presence(enum.Enum):
    YES = "yes"
    NO = "no"
    NA = "na"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def _probe_sdr() -> Presence:
    """Any USB SDR known to ka9q-radio present?

    Routes through `sigmond.discovery.usb_sdr.probe()` — the same code
    path the Environment screen runs, so a host where Environment shows
    "rx888 observed" also shows YES here.  Avoids duplicating the
    VID:PID table and the lsusb-parsing edge cases.
    """
    try:
        from . import discovery
        from .discovery import usb_sdr
        from .environment import load_environment
    except Exception:
        return Presence.UNKNOWN
    try:
        env = load_environment()
    except Exception:
        # No environment.toml on a greenfield host — synthesize an empty
        # one; probe() doesn't read environment.toml for usb_sdr beyond
        # using it as a manifest source for the declared-vs-observed
        # split, which we don't need here.
        from .environment import Environment
        env = Environment(declared={}, version=None, path=None)
    try:
        observations = usb_sdr.probe(env, timeout=2.0,
                                     limiter=discovery.RateLimiter())
    except Exception:
        return Presence.UNKNOWN
    # Filter to successful observations of kind="sdr".  probe() returns
    # an error-shaped Observation when lsusb itself fails — treat that
    # as UNKNOWN, not NO.
    healthy = [o for o in observations
               if getattr(o, "ok", True) and getattr(o, "kind", "") == "sdr"]
    errored = [o for o in observations if not getattr(o, "ok", True)]
    if errored and not healthy:
        return Presence.UNKNOWN
    return Presence.YES if healthy else Presence.NO


def _probe_magnetometer() -> Presence:
    """RM3100 USB magnetometer presence — check the /dev node sigmond's
    mag-recorder unit expects, plus the udev-named symlink the bundled
    mag-usb rules create."""
    candidates = [
        Path("/dev/ttyMAG0"),
        # udev-named alternates from the bundled mag-usb rules (best-effort).
        Path("/dev/mag-rm3100"),
    ]
    for p in candidates:
        try:
            if p.exists():
                return Presence.YES
        except (PermissionError, OSError):
            # Device node parent unreadable for our UID — treat as
            # "can't tell" rather than "no".
            return Presence.UNKNOWN
    # No magic node — look for the upstream USB device directly so we
    # can still say "yes" when udev hasn't run or the rules aren't
    # installed yet (typical right after the operator just plugs it in).
    return _lsusb_has(["10c4:ea60"])   # CP210x bridge, the RM3100 board's USB IC


def _probe_gpsdo() -> Presence:
    """Leo Bodnar Mini Precision GPS Reference Clock presence —
    USB CDC-ACM device with the LBE-1421 / LBE-1423 / Mini VID:PID."""
    return _lsusb_has([
        "1d50:60b2",   # LBE-1420
        "1d50:60b3",   # LBE-1421
        "1d50:60bc",   # LBE-1423
        "1d50:60c5",   # Mini Precision GPS reference clock
    ])


def _lsusb_has(vid_pids: list[str]) -> Presence:
    """Run `lsusb` and look for any of the supplied "vvvv:pppp" strings.

    Returns UNKNOWN if lsusb isn't installed or fails; YES if any match;
    NO otherwise.  Cheap and synchronous — typically completes in <50ms.
    """
    if not shutil.which("lsusb"):
        return Presence.UNKNOWN
    try:
        r = subprocess.run(
            ["lsusb"], capture_output=True, text=True, timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return Presence.UNKNOWN
    if r.returncode != 0:
        return Presence.UNKNOWN
    text = (r.stdout or "").lower()
    for vp in vid_pids:
        if vp.lower() in text:
            return Presence.YES
    return Presence.NO


# ---------------------------------------------------------------------------
# Component → probe table
# ---------------------------------------------------------------------------

_PROBES: dict[str, Callable[[], Presence]] = {
    "ka9q-radio":    _probe_sdr,
    "mag-recorder":  _probe_magnetometer,
    "gpsdo-monitor": _probe_gpsdo,
}

# Components whose presence column is intentionally blank — libraries,
# pure-client recorders that read from any (local OR remote) radiod via
# multicast, etc.  These rows show "—" rather than "?" to avoid
# implying a check was attempted.
_NO_HARDWARE_DEP: set[str] = {
    # Per-recorder clients listen to multicast — local OR remote radiod.
    # They have no hardware dependency of their own; the prerequisite is
    # "a radiod somewhere", which the operator declares via Configuration.
    "psk-recorder",
    "wspr-recorder",
    "hfdl-recorder",
    "codar-sounder",
    "hf-gps-tec",
    "hf-timestd",
    # Libraries and infra: no hardware to detect.
    "ka9q-python",
    "callhash",
    "hs-uploader",
    "igmp-querier",
    "ka9q-update",
    "onion",
}


def detect_for(component: str) -> Presence:
    """Hardware-presence verdict for one catalog component.

    The decision tree:
      1. component name is in the no-hardware-dep allowlist → NA
      2. component has a registered probe → run it
      3. otherwise → NA (unknown components default to no requirement)
    """
    if component in _NO_HARDWARE_DEP:
        return Presence.NA
    probe = _PROBES.get(component)
    if probe is None:
        return Presence.NA
    try:
        return probe()
    except Exception:
        return Presence.UNKNOWN


def detect_all(components: list[str]) -> dict[str, Presence]:
    """Run every probe for a list of component names.

    Probes are independent and cheap, so we just iterate.  Returns
    {component: Presence}.
    """
    return {c: detect_for(c) for c in components}


__all__ = ["Presence", "detect_for", "detect_all"]
