"""site-profile.toml — the single non-secret per-site source of truth.

One operator-edited file at ``/etc/sigmond/site-profile.toml`` captures the
per-installation *identity* (station, PSWS ids, reporter calls, hardware hints)
that would otherwise be entered into several client wizards. ``smd config
render`` translates it into ``coordination.toml`` / ``coordination.env`` — the
established distribution channel every client already reads.

This file holds **no secrets** (credentials live in their own 0600 paths and are
delivered via ``smd admin secrets``). See docs/PROVISIONING-INPUTS.md §8.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .paths import COORDINATION_PATH

SITE_PROFILE_PATH = COORDINATION_PATH.parent / "site-profile.toml"

TEMPLATE = """\
# /etc/sigmond/site-profile.toml — single non-secret per-site source of truth.
# Edit, then run:  sudo smd config render
# NO secrets here — credentials are installed via `smd admin secrets`.
schema_version = 1

[station]
callsign     = "<YOUR_CALL>"       # e.g. AC0G
grid_square  = "<YOUR_GRID>"       # e.g. EM38ww  (or set latitude/longitude)
# latitude   = 38.93
# longitude  = -92.33
# elevation_m = 200
description  = ""                  # free text (antenna / receiver)

[psws]                             # HamSCI PSWS / GRAPE (optional)
enabled       = false
station_id    = ""                 # e.g. S000082
instrument_id = ""                 # e.g. 172

[reporters]                        # default to [station].callsign when blank
wsprnet_call     = ""
pskreporter_call = ""

[host]
hostname = ""                      # radiod instance/mDNS names derive from this

[hardware]                         # hints; radiod config remains authoritative
sdr            = ""                # e.g. rx888
sdr_serial     = ""
radiod_status  = ""                # e.g. sigma-rx888mk2-status.local
timing         = ""                # e.g. gps_pps
gnss_vtec_host = ""

[secrets]
# Declared so `smd admin validate` knows which delivered secrets to expect.
require = []                       # e.g. ["earthdata", "rac"]
"""


@dataclass
class SiteProfile:
    call: str = ""
    grid: str = ""
    lat: float = 0.0
    lon: float = 0.0
    elevation_m: float = 0.0
    description: str = ""
    psws_enabled: bool = False
    psws_station_id: str = ""
    psws_instrument_id: str = ""
    wsprnet_call: str = ""
    pskreporter_call: str = ""
    hostname: str = ""
    sdr: str = ""
    sdr_serial: str = ""
    radiod_status: str = ""
    timing: str = ""
    gnss_vtec_host: str = ""
    secrets_require: list = field(default_factory=list)
    source_path: Optional[Path] = None

    @property
    def effective_wsprnet_call(self) -> str:
        return self.wsprnet_call or self.call

    @property
    def effective_pskreporter_call(self) -> str:
        return self.pskreporter_call or self.call


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_site_profile(path: Path = SITE_PROFILE_PATH) -> Optional[SiteProfile]:
    """Parse the site profile, or return None if the file does not exist."""
    if not path.is_file():
        return None
    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    st = data.get("station", {}) or {}
    psws = data.get("psws", {}) or {}
    rep = data.get("reporters", {}) or {}
    host = data.get("host", {}) or {}
    hw = data.get("hardware", {}) or {}
    sec = data.get("secrets", {}) or {}

    def _clean(s) -> str:
        s = str(s or "").strip()
        # treat unfilled <...> placeholders as empty
        return "" if (s.startswith("<") and s.endswith(">")) else s

    return SiteProfile(
        call=_clean(st.get("callsign")).upper(),
        grid=_clean(st.get("grid_square")),
        lat=_f(st.get("latitude")),
        lon=_f(st.get("longitude")),
        elevation_m=_f(st.get("elevation_m")),
        description=_clean(st.get("description")),
        psws_enabled=bool(psws.get("enabled", False)),
        psws_station_id=_clean(psws.get("station_id")),
        psws_instrument_id=_clean(psws.get("instrument_id")),
        wsprnet_call=_clean(rep.get("wsprnet_call")).upper(),
        pskreporter_call=_clean(rep.get("pskreporter_call")).upper(),
        hostname=_clean(host.get("hostname")),
        sdr=_clean(hw.get("sdr")),
        sdr_serial=_clean(hw.get("sdr_serial")),
        radiod_status=_clean(hw.get("radiod_status")),
        timing=_clean(hw.get("timing")),
        gnss_vtec_host=_clean(hw.get("gnss_vtec_host")),
        secrets_require=list(sec.get("require", []) or []),
        source_path=path,
    )
