"""Network diagnostics for multicast / IGMP deployment readiness.

Answers: is this host's network able to carry ka9q-radio multicast safely
between multiple hosts, or should radiod stay host-local (ttl=0)?

Two tiers of checks:
  * Tier 1 (unprivileged): interface enumeration, /proc/net/igmp parse
    (joined groups + negotiated IGMP version), dual-home / overlay
    detection, default-route interface.
  * Tier 2 (root): passive raw-socket listen on IPPROTO_IGMP to confirm
    querier presence, its source IP, version, and query interval.

Pure stdlib; runs on any Debian/Ubuntu sigmond target.
"""

from __future__ import annotations

import ipaddress
import json
import os
import select
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from sigmond.paths import SIGMOND_STATE

NET_DIAG_CACHE = SIGMOND_STATE / 'net-diag.json'
# How long a cached report is considered fresh. Longer than IGMP query
# interval (125s) so we don't thrash, short enough that a querier
# appearing/disappearing is noticed within a day of normal operator work.
CACHE_TTL_SECONDS = 24 * 3600


IGMP_TYPE_QUERY  = 0x11
IGMP_TYPE_V1_REP = 0x12
IGMP_TYPE_V2_REP = 0x16
IGMP_TYPE_V2_LV  = 0x17
IGMP_TYPE_V3_REP = 0x22

OVERLAY_DRIVERS = {
    'tailscale0', 'wg0', 'wg1',
}
OVERLAY_PREFIXES = ('tailscale', 'wg', 'zt', 'tun', 'tap', 'docker', 'br-',
                    'veth', 'cni', 'flannel', 'cilium')


# ---------------------------------------------------------------------------
# Dataclasses (all plain-dict friendly for --json)
# ---------------------------------------------------------------------------

@dataclass
class Interface:
    name:          str
    is_up:         bool
    is_loopback:   bool
    is_wireless:   bool
    is_bridge:     bool
    is_bond:       bool
    is_overlay:    bool
    bridge_members: list = field(default_factory=list)
    has_multicast: bool = False
    mtu:           int  = 0
    addrs_v4:      list = field(default_factory=list)   # "192.168.1.1/24"
    is_default_route: bool = False


@dataclass
class JoinedGroup:
    interface: str
    group:     str     # dotted-quad
    igmp_version: str  # "V1" / "V2" / "V3"


@dataclass
class Querier:
    interface:    str
    source:       str
    version:      int   # 1, 2, or 3
    max_resp_ms:  int
    group:        str   # "0.0.0.0" for general query
    qqic_s:       Optional[int] = None   # v3 only


@dataclass
class NetDiagReport:
    interfaces:     list
    joined_groups:  list
    queriers:       list
    listen_seconds: int
    listen_root:    bool
    listen_errors:  list
    classification: str           # see CLASSES below
    reasons:        list
    recommendation: str

    def to_dict(self) -> dict:
        return {
            'interfaces':     [asdict(i) for i in self.interfaces],
            'joined_groups':  [asdict(g) for g in self.joined_groups],
            'queriers':       [asdict(q) for q in self.queriers],
            'listen_seconds': self.listen_seconds,
            'listen_root':    self.listen_root,
            'listen_errors':  self.listen_errors,
            'classification': self.classification,
            'reasons':        self.reasons,
            'recommendation': self.recommendation,
        }


CLASSES = {
    'single-host-safe':   'Only loopback/overlay interfaces carry multicast; ttl=0 is the only safe setting.',
    'lan-capable':        'Wired LAN with querier present; multi-host radiod should work.',
    'lan-needs-querier':  'Wired LAN, no querier detected; streams will drop after snoop timeout unless you install one.',
    'lan-unsafe':         'Only Wi-Fi or a likely-dumb path available for multicast; not recommended for sustained traffic.',
    'multicast-blocked':  'No multicast-capable external interface.',
    'unknown':            'Could not classify with the information available.',
}


# ---------------------------------------------------------------------------
# Tier 1: interface enumeration
# ---------------------------------------------------------------------------

def _ip_json(*args) -> list:
    r = subprocess.run(['ip', '-j', *args], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def _default_route_ifname() -> Optional[str]:
    for route in _ip_json('route', 'show', 'default'):
        dev = route.get('dev')
        if dev:
            return dev
    return None


def _is_wireless(ifname: str) -> bool:
    return Path(f'/sys/class/net/{ifname}/wireless').exists()


def _is_bridge(ifname: str) -> bool:
    return Path(f'/sys/class/net/{ifname}/bridge').exists()


def _is_bond(ifname: str) -> bool:
    return Path(f'/sys/class/net/{ifname}/bonding').exists()


def _bridge_members(ifname: str) -> list:
    brif = Path(f'/sys/class/net/{ifname}/brif')
    if not brif.exists():
        return []
    try:
        return sorted(p.name for p in brif.iterdir())
    except OSError:
        return []


def _is_overlay(ifname: str, link_type: str) -> bool:
    if ifname in OVERLAY_DRIVERS:
        return True
    if link_type in ('none', 'tunnel', 'ipip', 'gre', 'sit'):
        return True
    return any(ifname.startswith(p) for p in OVERLAY_PREFIXES)


def enumerate_interfaces() -> list:
    default_iface = _default_route_ifname()
    out: list = []
    for raw in _ip_json('addr', 'show'):
        name = raw.get('ifname', '')
        if not name:
            continue
        flags = set(raw.get('flags', []))
        is_up = 'UP' in flags and raw.get('operstate', '').upper() in ('UP', 'UNKNOWN')
        addrs_v4 = [f"{a['local']}/{a['prefixlen']}"
                    for a in raw.get('addr_info', [])
                    if a.get('family') == 'inet']
        out.append(Interface(
            name=name,
            is_up=is_up,
            is_loopback='LOOPBACK' in flags,
            is_wireless=_is_wireless(name),
            is_bridge=_is_bridge(name),
            is_bond=_is_bond(name),
            is_overlay=_is_overlay(name, raw.get('link_type', '')),
            bridge_members=_bridge_members(name) if _is_bridge(name) else [],
            has_multicast='MULTICAST' in flags,
            mtu=int(raw.get('mtu', 0) or 0),
            addrs_v4=addrs_v4,
            is_default_route=(name == default_iface),
        ))
    return out


# ---------------------------------------------------------------------------
# Tier 1: /proc/net/igmp parse
# ---------------------------------------------------------------------------

def _hex_be_to_dotted(hex_be: str) -> str:
    """/proc/net/igmp renders IPs little-endian hex — e.g. 010000E0 = 224.0.0.1."""
    try:
        b = bytes.fromhex(hex_be)
    except ValueError:
        return '0.0.0.0'
    if len(b) != 4:
        return '0.0.0.0'
    return f'{b[3]}.{b[2]}.{b[1]}.{b[0]}'


def parse_proc_net_igmp(path: str = '/proc/net/igmp') -> tuple[list, dict]:
    """Return (joined_groups, iface_version_map).

    iface_version_map[iface] = "V1"|"V2"|"V3" — the version the kernel is
    currently running on that iface. V2 on a modern host means a v2 query
    was recently seen; i.e. a querier IS present. V3 is ambiguous (either
    a v3 querier, or no querier — v3 is the default absent queries).
    """
    groups: list = []
    ifver: dict = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return groups, ifver

    current_iface: Optional[str] = None
    for line in text.splitlines()[1:]:  # skip header
        if not line.strip():
            continue
        if not line.startswith('\t') and not line.startswith(' '):
            # Interface header line: "Idx\tDevice\t: Count Querier"
            parts = line.split()
            if len(parts) >= 5:
                # parts: [idx, device+":", count, "V1"/"V2"/"V3"]
                # device is "<name>" possibly suffixed with ":"
                dev = parts[1].rstrip(':')
                ver = parts[-1]
                current_iface = dev
                ifver[dev] = ver
            continue
        # Group line (indented): "\t\t<HEX>\t<users> <timer>\t<reporter>"
        parts = line.split()
        if current_iface and parts and len(parts[0]) == 8:
            grp = _hex_be_to_dotted(parts[0])
            groups.append(JoinedGroup(
                interface=current_iface,
                group=grp,
                igmp_version=ifver.get(current_iface, '?'),
            ))
    return groups, ifver


# ---------------------------------------------------------------------------
# Tier 2: passive IGMP query listen (root only)
# ---------------------------------------------------------------------------

def listen_for_queriers(seconds: int, interface: Optional[str] = None
                        ) -> tuple[list, list]:
    """Raw-socket listen on IPPROTO_IGMP for general queries.

    Returns (queriers, errors). Dedupes by (interface, source, version).
    Needs CAP_NET_RAW — if unavailable, returns ([], ['<reason>']).
    """
    errors: list = []
    seen: dict = {}

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IGMP)
    except PermissionError:
        return [], ['raw socket requires CAP_NET_RAW — re-run with sudo']
    except OSError as exc:
        return [], [f'raw socket: {exc}']

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if interface:
        try:
            # SO_BINDTODEVICE: bytes NUL-terminated, limited to IFNAMSIZ
            sock.setsockopt(socket.SOL_SOCKET, 25,  # SO_BINDTODEVICE
                            (interface + '\x00').encode())
        except (OSError, PermissionError) as exc:
            errors.append(f'bind-to-device {interface}: {exc} (listening on all)')

    deadline = time.monotonic() + seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r, _, _ = select.select([sock], [], [], min(remaining, 1.0))
            if not r:
                continue
            try:
                data, addr = sock.recvfrom(2048)
            except OSError as exc:
                errors.append(f'recvfrom: {exc}')
                break
            q = _parse_igmp_query(data, src_addr=addr[0])
            if q is None:
                continue
            # Kernel does not tell us the recv interface on AF_INET raw;
            # infer by matching src to the subnet of one of our ifaces.
            q.interface = _infer_iface_for_src(q.source) or ''
            key = (q.interface, q.source, q.version)
            if key not in seen:
                seen[key] = q
    finally:
        sock.close()

    return list(seen.values()), errors


def _parse_igmp_query(pkt: bytes, src_addr: str) -> Optional[Querier]:
    if len(pkt) < 20:
        return None
    ihl = (pkt[0] & 0x0F) * 4
    if ihl < 20 or len(pkt) < ihl + 8:
        return None
    igmp = pkt[ihl:]
    igmp_type = igmp[0]
    if igmp_type != IGMP_TYPE_QUERY:
        return None
    max_resp_byte = igmp[1]
    group = socket.inet_ntoa(igmp[4:8])
    # IGMPv1: max_resp = 0; v2: non-zero max_resp in 1/10s, 8-byte packet;
    # v3: packet length >= 12 with S/QRV/QQIC trailer.
    if len(igmp) >= 12:
        version = 3
        qqic = igmp[9]
        qqic_s = _qqic_to_seconds(qqic)
        max_resp_ms = _v3_max_resp_to_ms(max_resp_byte)
    elif max_resp_byte == 0:
        version = 1
        qqic_s = None
        max_resp_ms = 0
    else:
        version = 2
        qqic_s = None
        max_resp_ms = max_resp_byte * 100
    return Querier(
        interface='',
        source=src_addr,
        version=version,
        max_resp_ms=max_resp_ms,
        group=group,
        qqic_s=qqic_s,
    )


def _v3_max_resp_to_ms(byte: int) -> int:
    if byte < 128:
        return byte * 100
    exp = (byte >> 4) & 0x07
    mant = byte & 0x0F
    return ((mant | 0x10) << (exp + 3)) * 100


def _qqic_to_seconds(byte: int) -> int:
    if byte < 128:
        return byte
    exp = (byte >> 4) & 0x07
    mant = byte & 0x0F
    return (mant | 0x10) << (exp + 3)


def _infer_iface_for_src(src: str) -> Optional[str]:
    try:
        src_ip = ipaddress.ip_address(src)
    except ValueError:
        return None
    for iface in enumerate_interfaces():
        for cidr in iface.addrs_v4:
            try:
                net = ipaddress.ip_interface(cidr).network
            except ValueError:
                continue
            if src_ip in net:
                return iface.name
    return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(interfaces: list, groups: list, queriers: list,
             listen_root: bool) -> tuple[str, list, str]:
    reasons: list = []

    external = [i for i in interfaces
                if i.is_up and not i.is_loopback and i.has_multicast]
    wired = [i for i in external
             if not i.is_wireless and not i.is_overlay and not i.is_bridge]
    wireless_only_paths = [i for i in external
                           if i.is_wireless and not i.is_overlay]
    overlay = [i for i in external if i.is_overlay]
    default_iface = next((i for i in interfaces if i.is_default_route), None)

    # No external multicast-capable iface: host-local only
    if not external:
        reasons.append('no external multicast-capable interface detected')
        return 'single-host-safe', reasons, (
            'Keep ttl=0 in radiod configs. Multi-host multicast is not '
            'reachable from this host.')

    # Wireless-only path means the only way off-host is Wi-Fi
    if wireless_only_paths and not wired and not overlay:
        reasons.append(f'only multicast-capable external iface is Wi-Fi '
                       f'({wireless_only_paths[0].name})')
        return 'lan-unsafe', reasons, (
            'Do not bind radiod to a Wi-Fi interface for sustained multicast; '
            'use ttl=0 or add a wired path.')

    # Look for querier evidence on wired interfaces
    wired_names = {i.name for i in wired}
    querier_on_wired = [q for q in queriers if q.interface in wired_names]
    v2_downgrade = [g for g in groups
                    if g.interface in wired_names and g.igmp_version == 'V2']

    if querier_on_wired:
        q = querier_on_wired[0]
        reasons.append(f'IGMPv{q.version} querier {q.source} observed on '
                       f'{q.interface}')
        return 'lan-capable', reasons, (
            'LAN has a querier; multi-host radiod with ttl=1 is supported. '
            'Verify your switch has IGMP snooping enabled so the querier '
            'prunes multicast to interested ports.')

    if v2_downgrade:
        ifaces = sorted({g.interface for g in v2_downgrade})
        reasons.append(f'kernel IGMP state is V2 on {", ".join(ifaces)} '
                       f'(downgrade implies a v2 querier has been seen)')
        return 'lan-capable', reasons, (
            'A querier appears to be present (kernel downgrade to IGMPv2). '
            'Re-run `sudo smd diag net` to confirm with a raw-socket listen.')

    if wired:
        if not listen_root:
            reasons.append('no querier seen in /proc/net/igmp state; '
                           'passive listen skipped (needs root)')
            return 'unknown', reasons, (
                'Re-run `sudo smd diag net --listen 130` to passively listen '
                'for IGMP queries before deciding whether to install one.')
        reasons.append(f'listened {queriers and "" or "≥125s"} on wired iface, '
                       f'no IGMP general query observed')
        return 'lan-needs-querier', reasons, (
            'No querier on the segment. Either enable one on your router/switch '
            'or install igmp-querier (see docs). Until then, keep ttl=0.')

    reasons.append('only overlay/tunnel interfaces are multicast-capable')
    if default_iface and default_iface.is_overlay:
        reasons.append(f'default route is via overlay {default_iface.name}')
    return 'single-host-safe', reasons, (
        'Overlays (Tailscale, ZeroTier, WireGuard) generally do not carry '
        'multicast between peers. Keep ttl=0.')


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------

def run(listen_seconds: int = 130, interface: Optional[str] = None
        ) -> NetDiagReport:
    interfaces = enumerate_interfaces()
    groups, _ = parse_proc_net_igmp()
    listen_root = (os.geteuid() == 0)
    if listen_root and listen_seconds > 0:
        queriers, listen_errors = listen_for_queriers(listen_seconds, interface)
    else:
        queriers, listen_errors = [], []
        if not listen_root:
            listen_errors.append('passive listen skipped (needs root)')
    classification, reasons, recommendation = classify(
        interfaces, groups, queriers, listen_root)
    return NetDiagReport(
        interfaces=interfaces,
        joined_groups=groups,
        queriers=queriers,
        listen_seconds=listen_seconds if listen_root else 0,
        listen_root=listen_root,
        listen_errors=listen_errors,
        classification=classification,
        reasons=reasons,
        recommendation=recommendation,
    )


# ---------------------------------------------------------------------------
# Cache: persist the most recent report so fast verbs can read classification
# without re-running the 130 s passive listen on every invocation.
# ---------------------------------------------------------------------------

def save_cache(report: NetDiagReport, path: Path = NET_DIAG_CACHE) -> None:
    """Write report plus a wall-clock timestamp. Silently skips on EPERM."""
    payload = {
        'timestamp': time.time(),
        'report':    report.to_dict(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)
    except (OSError, PermissionError):
        pass


def load_cache(path: Path = NET_DIAG_CACHE) -> Optional[dict]:
    """Return {'timestamp': float, 'report': dict, 'age_s': float} or None."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    ts = payload.get('timestamp')
    rep = payload.get('report')
    if not isinstance(ts, (int, float)) or not isinstance(rep, dict):
        return None
    return {'timestamp': ts, 'report': rep, 'age_s': max(0.0, time.time() - ts)}


def is_cache_fresh(cache: Optional[dict], ttl: int = CACHE_TTL_SECONDS) -> bool:
    return cache is not None and cache['age_s'] < ttl


def run_cached(listen_seconds: int = 130, interface: Optional[str] = None,
               force: bool = False, ttl: int = CACHE_TTL_SECONDS,
               ) -> tuple[NetDiagReport, bool]:
    """Run net-diag or return cached report. Returns (report, from_cache).

    A fresh cache always wins unless force=True. If we run live, the
    result is written back only if the listen actually happened as root —
    unprivileged runs can be inconclusive ('unknown') and shouldn't
    evict a good root-captured report.
    """
    if not force:
        cache = load_cache()
        if is_cache_fresh(cache, ttl):
            return _report_from_dict(cache['report']), True

    report = run(listen_seconds=listen_seconds, interface=interface)
    if report.listen_root and report.classification != 'unknown':
        save_cache(report)
    return report, False


def _report_from_dict(d: dict) -> NetDiagReport:
    return NetDiagReport(
        interfaces=[Interface(**i) for i in d.get('interfaces', [])],
        joined_groups=[JoinedGroup(**g) for g in d.get('joined_groups', [])],
        queriers=[Querier(**q) for q in d.get('queriers', [])],
        listen_seconds=d.get('listen_seconds', 0),
        listen_root=d.get('listen_root', False),
        listen_errors=d.get('listen_errors', []),
        classification=d.get('classification', 'unknown'),
        reasons=d.get('reasons', []),
        recommendation=d.get('recommendation', ''),
    )
