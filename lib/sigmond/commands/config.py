"""`smd config show`, `smd config migrate`, and `smd config identity`."""

from __future__ import annotations

import configparser
import json
import os
import sys
from pathlib import Path
from typing import Optional

from ..coordination import Host, load_coordination, render_env
from ..paths import COORDINATION_ENV, COORDINATION_PATH, WSPRDAEMON_CONF
from ..sysview import build_system_view
from ..ui import err, heading, info, ok, warn


# ---------------------------------------------------------------------------
# smd config show
# ---------------------------------------------------------------------------

def cmd_config_show(args) -> int:
    view = build_system_view()
    coord = view.coordination
    as_json = getattr(args, 'json', False)

    if as_json:
        payload = {
            "coordination": _coord_to_dict(coord),
            "clients": {
                name: _clientview_to_dict(cv)
                for name, cv in view.client_views.items()
            },
        }
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    heading('coordination')
    if coord.source_path:
        info(f'source: {coord.source_path}')
    else:
        info('source: (none — using defaults)')
    if coord.host.call or coord.host.grid:
        info(f'host: {coord.host.call} / {coord.host.grid}')

    if coord.radiods:
        print()
        print('  \033[1mradiod instances\033[0m')
        for rid, r in sorted(coord.radiods.items()):
            loc = 'local' if r.is_local else f'remote ({r.host})'
            print(f'    - {rid}  [{loc}]  samprate={r.samprate_hz or "?"}  status_dns={r.status_dns or "?"}')
    else:
        info('radiod instances: (none declared)')

    if coord.clients:
        print()
        print('  \033[1mclient instances\033[0m')
        for c in coord.clients:
            bind = f' → {c.radiod_id}' if c.radiod_id else ''
            print(f'    - {c.client_type}@{c.instance}{bind}')

    if view.client_views:
        heading('clients (read-only)')
        for name, cv in view.client_views.items():
            state = 'installed' if cv.installed else 'not installed'
            print(f'\n  \033[1m{name}\033[0m  [{state}]')
            if cv.config_path:
                info(f'config: {cv.config_path}')
            for issue in cv.issues:
                warn(issue)
            for iv in cv.instances:
                bits = [f'instance={iv.instance}']
                if iv.radiod_id:
                    bits.append(f'radiod={iv.radiod_id}')
                if iv.ka9q_channels:
                    bits.append(f'channels={iv.ka9q_channels}')
                if iv.frequencies_hz:
                    bits.append(f'freqs={len(iv.frequencies_hz)}')
                if iv.radiod_samprate_hz:
                    bits.append(f'samprate={iv.radiod_samprate_hz}')
                if iv.radiod_status_dns:
                    bits.append(f'status_dns={iv.radiod_status_dns}')
                print(f'    - {", ".join(bits)}')
    return 0


# ---------------------------------------------------------------------------
# smd config migrate
# ---------------------------------------------------------------------------

def cmd_config_migrate(args) -> int:
    source_path = Path(getattr(args, 'from_', None) or WSPRDAEMON_CONF)
    dest_path   = Path(getattr(args, 'to',    None) or COORDINATION_PATH)
    write       = bool(getattr(args, 'write', False))

    heading('config migrate')
    info(f'source: {source_path}')
    info(f'target: {dest_path}')

    if dest_path.exists() and not getattr(args, 'force', False):
        ok(f'{dest_path} already exists — no changes')
        info('use --force to overwrite or --write to a different --to path')
        return 0

    if not source_path.exists():
        err(f'{source_path} does not exist; nothing to migrate from')
        return 1

    toml_text = build_migrated_toml(source_path)

    if not write:
        print()
        sys.stdout.write(toml_text)
        sys.stdout.write('\n')
        info('(dry run — pass --write to save)')
        return 0

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(toml_text)
    ok(f'wrote {dest_path}')
    return 0


def build_migrated_toml(source_path: Path) -> str:
    """Extract cross-client settings from wsprdaemon.conf and return a
    coordination.toml document.  Pure: no filesystem writes."""
    cfg = configparser.ConfigParser(
        comment_prefixes=(';', '#'),
        inline_comment_prefixes=(';', '#'),
        strict=False,
        interpolation=None,
    )
    cfg.read(source_path)

    g = cfg['general'] if cfg.has_section('general') else {}

    call = _strip(g.get('reporter_call', '')) if g else ''
    grid = _strip(g.get('reporter_grid', '')) if g else ''

    ka9q_conf_name = _strip(g.get('ka9q_conf_name', '')) if g else ''
    ka9q_web_dns   = _strip(g.get('ka9q_web_dns', '')) if g else ''

    hf = cfg['hf-timestd'] if cfg.has_section('hf-timestd') else {}
    kw = cfg['ka9q-web']   if cfg.has_section('ka9q-web')   else {}

    lines = [
        '# /etc/sigmond/coordination.toml',
        f'# Migrated from {source_path}',
        '',
        '[host]',
    ]
    lines.append(f'call = "{call}"')
    lines.append(f'grid = "{grid}"')
    lines.append('')

    if ka9q_conf_name:
        lines.append(f'[radiod."{ka9q_conf_name}"]')
        lines.append('host        = "localhost"')
        if ka9q_web_dns:
            lines.append(f'status_dns  = "{ka9q_web_dns}"')
        else:
            lines.append(f'status_dns  = "{ka9q_conf_name}-status.local"')
        lines.append('samprate_hz = 0          # fill in from radiod.conf')
        lines.append('cores       = ""         # set once radiod is built')
        lines.append('')
    else:
        lines.append('# [radiod."<name>"] entries: none detected in source.')
        lines.append('# Add one per radiod instance this host should manage.')
        lines.append('')

    lines.append('[cpu]')
    lines.append(f'reserved_cpus         = "{_strip(g.get("reserved_cpus", "")) if g else ""}"')
    lines.append('suite_cores           = ""')
    lines.append('worker_cores          = ""')
    lines.append('owns_timestd_affinity = true')
    lines.append('')

    # [[clients.wspr]]
    if ka9q_conf_name:
        lines.append('[[clients.wspr]]')
        lines.append('instance  = "default"')
        lines.append(f'radiod_id = "{ka9q_conf_name}"')
        lines.append('')

    # [[clients.hf-timestd]] — only if source had [hf-timestd] enabled
    if hf and _strip(hf.get('enabled', 'false')).lower() in ('true', 'yes', '1'):
        lines.append('[[clients.hf-timestd]]')
        lines.append('instance         = "default"')
        if ka9q_conf_name:
            lines.append(f'radiod_id        = "{ka9q_conf_name}"')
        else:
            lines.append('# radiod_id        = "..."    # fill in')
        authority = _strip(hf.get('timing_authority', 'rtp'))
        lines.append(f'timing_authority = "{authority}"')
        physics = _strip(hf.get('physics_enabled', 'false')).lower() in ('true', 'yes', '1')
        lines.append(f'physics_enabled  = {"true" if physics else "false"}')
        lines.append('')

    # [[clients.ka9q-web]]
    if kw and _strip(kw.get('enabled', 'false')).lower() in ('true', 'yes', '1'):
        base_port = _strip(kw.get('base_port', '8081')) or '8081'
        if ka9q_conf_name:
            lines.append('[[clients.ka9q-web]]')
            lines.append(f'instance  = "{ka9q_conf_name}"')
            lines.append(f'radiod_id = "{ka9q_conf_name}"')
            lines.append(f'port      = {base_port}')
            lines.append('')

    # [[clients.rac]]
    rac_channel = _strip(g.get('rac', '')) if g else ''
    if rac_channel:
        rac_server = _strip(g.get('rac_server', 'remote.wsprdaemon.org')) or 'remote.wsprdaemon.org'
        lines.append('[[clients.rac]]')
        lines.append('instance = "default"')
        lines.append('enabled  = true')
        lines.append(f'channel  = {rac_channel}')
        lines.append(f'server   = "{rac_server}"')
        lines.append('# token via /etc/sigmond/secrets.env')
        lines.append('')

    lines.append('[disk_budget]')
    lines.append('root_path    = "/var"')
    lines.append('warn_percent = 80')
    lines.append('')

    return '\n'.join(lines)


def _strip(v) -> str:
    return str(v or '').strip()


# ---------------------------------------------------------------------------
# Serialization helpers (for --json output)
# ---------------------------------------------------------------------------

def _coord_to_dict(coord) -> dict:
    return {
        "host": coord.host.__dict__,
        "radiods": {rid: r.__dict__ for rid, r in coord.radiods.items()},
        "cpu": coord.cpu.__dict__,
        "clients": [
            {"client_type": c.client_type, "instance": c.instance,
             "radiod_id": c.radiod_id, "extras": c.extras}
            for c in coord.clients
        ],
        "disk_budget": coord.disk_budget.__dict__,
        "source_path": str(coord.source_path) if coord.source_path else None,
    }


def _clientview_to_dict(cv) -> dict:
    return {
        "client_type": cv.client_type,
        "installed":   cv.installed,
        "config_path": str(cv.config_path) if cv.config_path else None,
        "instances": [
            {
                "instance":      iv.instance,
                "radiod_id":     iv.radiod_id,
                "required_cores": iv.required_cores,
                "preferred_cores": iv.preferred_cores,
                "frequencies_hz": iv.frequencies_hz,
                "ka9q_channels":  iv.ka9q_channels,
                "disk_writes":    [dw.__dict__ for dw in iv.disk_writes],
                "uses_timing_calibration":     iv.uses_timing_calibration,
                "provides_timing_calibration": iv.provides_timing_calibration,
                "radiod_samprate_hz": iv.radiod_samprate_hz,
                "radiod_status_dns":  iv.radiod_status_dns,
                "radiod_max_channels": iv.radiod_max_channels,
            }
            for iv in cv.instances
        ],
        "issues": cv.issues,
    }


# ---------------------------------------------------------------------------
# smd config identity
# ---------------------------------------------------------------------------

def cmd_config_identity(args) -> int:
    """Capture or display sigmond's operator identity (callsign + grid).

    These values live in the [host] block of coordination.toml and are
    rendered to coordination.env as STATION_CALL / STATION_GRID /
    STATION_LAT / STATION_LON (CLIENT-CONTRACT v0.5 §14.2).  Sigmond
    publishes them so client config wizards (wsprdaemon-client,
    hf-timestd, psk-recorder, …) can use them as defaults instead of
    re-prompting for the same fields per-client.
    """
    coord = load_coordination(COORDINATION_PATH)

    if getattr(args, 'json', False):
        json.dump({"call": coord.host.call,
                   "grid": coord.host.grid,
                   "lat":  coord.host.lat,
                   "lon":  coord.host.lon},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    arg_call = (getattr(args, 'call', None) or '').strip().upper() or None
    arg_grid = (getattr(args, 'grid', None) or '').strip()         or None
    arg_lat  = getattr(args, 'lat', None)
    arg_lon  = getattr(args, 'lon', None)

    interactive = (arg_call is None and arg_grid is None
                   and arg_lat is None and arg_lon is None
                   and sys.stdin.isatty())

    if interactive:
        heading('operator identity')
        info('callsign + grid live in /etc/sigmond/coordination.toml [host];')
        info('client wizards read STATION_CALL/STATION_GRID from coordination.env')
        info('and use them as defaults instead of re-prompting per-client.')
        print()
        new_call = _prompt_default('callsign',                        coord.host.call).upper()
        new_grid = _prompt_default('grid (Maidenhead 4/6 char)',      coord.host.grid)
        lat_s    = _prompt_default('latitude  (decimal, optional)',   _fmt_float(coord.host.lat))
        lon_s    = _prompt_default('longitude (decimal, optional)',   _fmt_float(coord.host.lon))
        new_lat  = float(lat_s) if lat_s.strip() else 0.0
        new_lon  = float(lon_s) if lon_s.strip() else 0.0
    else:
        new_call = arg_call if arg_call is not None else coord.host.call
        new_grid = arg_grid if arg_grid is not None else coord.host.grid
        new_lat  = float(arg_lat) if arg_lat is not None else coord.host.lat
        new_lon  = float(arg_lon) if arg_lon is not None else coord.host.lon

    if not new_call:
        err('callsign cannot be empty (pass --call CALL or fill in the prompt)')
        return 1
    if new_grid and not _looks_like_grid(new_grid):
        warn(f'grid "{new_grid}" does not look like a Maidenhead locator (proceeding anyway)')

    coord.host = Host(call=new_call, grid=new_grid, lat=new_lat, lon=new_lon)

    try:
        _patch_host_block(COORDINATION_PATH, coord.host)
    except PermissionError:
        err(f'permission denied writing {COORDINATION_PATH}; re-run smd as root')
        return 1
    ok(f'updated {COORDINATION_PATH}')

    try:
        env_text = render_env(coord)
        COORDINATION_ENV.parent.mkdir(parents=True, exist_ok=True)
        COORDINATION_ENV.write_text(env_text)
    except PermissionError:
        warn(f'wrote coordination.toml but could not refresh {COORDINATION_ENV} '
             '(permission denied) — re-run as root to publish env vars')
        return 0
    ok(f'rendered {COORDINATION_ENV}')

    print()
    info(f'operator: {coord.host.call} / {coord.host.grid}')
    info('clients pick up the new values on next service start or reload.')
    return 0


def _prompt_default(label: str, default: str) -> str:
    s = input(f'  {label} [{default}]: ').strip()
    return s if s else default


def _fmt_float(v: float) -> str:
    return '' if not v else f'{v:g}'


def _looks_like_grid(s: str) -> bool:
    """Crude Maidenhead 4/6/8-char locator check."""
    s = s.upper().strip()
    if len(s) not in (4, 6, 8):
        return False
    return (s[0:2].isalpha() and s[2:4].isdigit()
            and (len(s) == 4 or s[4:6].isalpha())
            and (len(s) <= 6 or s[6:8].isdigit()))


def _patch_host_block(path: Path, host: Host) -> None:
    """Rewrite the [host] block of coordination.toml in place.

    Other sections are preserved verbatim; only the [host] body is
    replaced (or a [host] block prepended if absent).  Comments inside
    the [host] block are not preserved — the body is rebuilt from the
    Host dataclass fields.
    """
    new_block = ['[host]', f'call = "{host.call}"', f'grid = "{host.grid}"']
    if host.lat:
        new_block.append(f'lat  = {host.lat}')
    if host.lon:
        new_block.append(f'lon  = {host.lon}')
    new_block.append('')

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        header = ['# /etc/sigmond/coordination.toml',
                  '# Authored by `smd config identity`.', '']
        path.write_text('\n'.join(header + new_block) + '\n')
        return

    lines = path.read_text().splitlines()
    out: list[str] = []
    in_host = False
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped == '[host]':
            in_host = True
            found = True
            out.extend(new_block)
            continue
        if in_host:
            if stripped.startswith('['):
                in_host = False
                out.append(line)
            # else: drop — the old [host] body is replaced above
            continue
        out.append(line)

    if not found:
        # Insert [host] after any leading comments / blank lines.
        i = 0
        while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith('#')):
            i += 1
        out = lines[:i] + new_block + lines[i:]

    path.write_text('\n'.join(out).rstrip() + '\n')
