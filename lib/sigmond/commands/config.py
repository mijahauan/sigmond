"""`smd config show`, `smd config migrate`, and `smd config identity`."""

from __future__ import annotations

import configparser
import json
import os
import sys
from pathlib import Path
from typing import Optional

from ..coordination import (
    Host, Station, load_coordination, render_env, write_host_identity,
)
from ..paths import COORDINATION_ENV, COORDINATION_PATH
from ..site_profile import (
    SITE_PROFILE_PATH, TEMPLATE as SITE_PROFILE_TEMPLATE, load_site_profile,
)
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
    heading('config migrate')
    source = getattr(args, 'from_', None)
    if not source:
        info('config migrate requires --from <path> to a legacy source config')
        return 2
    source_path = Path(source)
    dest_path   = Path(getattr(args, 'to',    None) or COORDINATION_PATH)
    write       = bool(getattr(args, 'write', False))

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
    publishes them so client config wizards (hf-timestd,
    psk-recorder, …) can use them as defaults instead of
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


# ---------------------------------------------------------------------------
# smd config refresh
# ---------------------------------------------------------------------------

def cmd_config_refresh(args) -> int:
    """Re-render /etc/sigmond/coordination.env from coordination.toml.

    `smd config identity` and `smd config init radiod` both keep the env
    file in sync as they write coordination.toml.  This verb is for the
    other case: the operator hand-edited coordination.toml and wants the
    env file to reflect it without re-running a wizard.

    With --dry-run, prints the rendered content to stdout instead of
    writing — useful for diff-ing against the live file.
    """
    coord = load_coordination(COORDINATION_PATH)
    if coord.source_path is None:
        warn(f'{COORDINATION_PATH} does not exist — nothing to render')
        info('  start with: smd config identity   (and: smd config init radiod)')
        return 1

    env_text = render_env(coord)

    if getattr(args, 'dry_run', False):
        print(env_text, end='' if env_text.endswith('\n') else '\n')
        return 0

    try:
        COORDINATION_ENV.parent.mkdir(parents=True, exist_ok=True)
        COORDINATION_ENV.write_text(env_text)
    except PermissionError:
        err(f'permission denied writing {COORDINATION_ENV}; re-run smd as root')
        return 1
    except OSError as exc:
        err(f'failed to write {COORDINATION_ENV}: {exc}')
        return 1

    ok(f'rendered {COORDINATION_ENV}')
    n_lines = sum(1 for line in env_text.splitlines()
                  if line and not line.startswith('#'))
    info(f'  {n_lines} env var(s) emitted from {COORDINATION_PATH}')
    return 0


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


# ---------------------------------------------------------------------------
# smd config render  (site-profile.toml -> coordination.toml/.env)
# ---------------------------------------------------------------------------

def cmd_config_render(args) -> int:
    """Render coordination.toml/.env from /etc/sigmond/site-profile.toml.

    site-profile.toml is the single non-secret per-site source of truth. This
    maps [station] -> coordination [host] (call/grid/lat/lon) and PSWS/reporter
    identity -> a coordination [station] block, then re-renders coordination.env
    that all clients consume. Secrets are out of scope (see `smd admin secrets`).
    """
    if getattr(args, 'init', False):
        if SITE_PROFILE_PATH.exists():
            info(f'{SITE_PROFILE_PATH} already exists — left unchanged')
            return 0
        try:
            SITE_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            SITE_PROFILE_PATH.write_text(SITE_PROFILE_TEMPLATE)
        except PermissionError:
            err(f'permission denied writing {SITE_PROFILE_PATH}; re-run smd as root')
            return 1
        ok(f'wrote {SITE_PROFILE_PATH} — edit it, then run: smd config render')
        return 0

    profile = load_site_profile(SITE_PROFILE_PATH)
    if profile is None:
        err(f'{SITE_PROFILE_PATH} not found')
        info('  scaffold one with:  smd config render --init')
        return 1

    problems = []
    if not profile.call:
        problems.append('station.callsign is empty')
    if not profile.grid and not (profile.lat or profile.lon):
        problems.append('need station.grid_square or latitude/longitude')
    if profile.psws_enabled and not (profile.psws_station_id and profile.psws_instrument_id):
        problems.append('psws.enabled but station_id/instrument_id missing')
    if problems:
        for p in problems:
            err(p)
        return 1

    station = Station(
        psws_id=profile.psws_station_id if profile.psws_enabled else '',
        instrument_id=profile.psws_instrument_id if profile.psws_enabled else '',
        wsprnet_call=profile.effective_wsprnet_call,
        pskreporter_call=profile.effective_pskreporter_call,
    )

    if getattr(args, 'dry_run', False):
        coord = load_coordination(COORDINATION_PATH)
        coord.host = Host(call=profile.call, grid=profile.grid,
                          lat=profile.lat, lon=profile.lon)
        coord.station = station
        heading('config render (dry-run)')
        info(f'source: {SITE_PROFILE_PATH}')
        print(render_env(coord), end='')
        return 0

    try:
        write_host_identity(call=profile.call, grid=profile.grid,
                            lat=profile.lat, lon=profile.lon,
                            path=COORDINATION_PATH)
        _patch_station_block(COORDINATION_PATH, station)
    except PermissionError:
        err(f'permission denied writing {COORDINATION_PATH}; re-run smd as root')
        return 1
    ok(f'updated {COORDINATION_PATH} ([host] + [station]) from {SITE_PROFILE_PATH.name}')

    coord = load_coordination(COORDINATION_PATH)
    try:
        COORDINATION_ENV.parent.mkdir(parents=True, exist_ok=True)
        COORDINATION_ENV.write_text(render_env(coord))
    except PermissionError:
        warn(f'wrote coordination.toml but could not refresh {COORDINATION_ENV} '
             '(permission denied) — re-run as root')
        return 0
    ok(f'rendered {COORDINATION_ENV}')

    print()
    loc = profile.grid or f'{profile.lat},{profile.lon}'
    info(f'operator: {profile.call} / {loc}')
    if profile.psws_enabled:
        info(f'PSWS: station {profile.psws_station_id} / instrument {profile.psws_instrument_id}')
    info('clients pick up the new values on next service start or reload.')
    return 0


def _patch_station_block(path: Path, station: Station) -> None:
    """Rewrite the [station] block of coordination.toml in place; other blocks
    (including [host]) are preserved verbatim. Mirrors _patch_host_block."""
    body = ['[station]']
    if station.psws_id:
        body.append(f'psws_id = "{station.psws_id}"')
    if station.instrument_id:
        body.append(f'instrument_id = "{station.instrument_id}"')
    if station.wsprnet_call:
        body.append(f'wsprnet_call = "{station.wsprnet_call}"')
    if station.pskreporter_call:
        body.append(f'pskreporter_call = "{station.pskreporter_call}"')
    body.append('')

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(body) + '\n')
        return

    lines = path.read_text().splitlines()
    out: list = []
    in_block = False
    found = False
    for line in lines:
        s = line.strip()
        if s == '[station]':
            in_block = True
            found = True
            out.extend(body)
            continue
        if in_block:
            if s.startswith('['):
                in_block = False
                out.append(line)
            continue
        out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append('')
        out.extend(body)
    path.write_text('\n'.join(out).rstrip() + '\n')


# ---------------------------------------------------------------------------
# smd config upload — flip a recorder instance's per-instance upload enable flag
# ---------------------------------------------------------------------------

def cmd_config_upload(args) -> int:
    """Enable/disable a recorder instance's upstream upload.

    This flips ONLY the per-instance enable flag (e.g. WSPR_USE_HS_UPLOADER)
    in /etc/<client>/env/<instance>.env.  Identity is owned by
    `smd config render` (site-profile) and credentials by `smd admin secrets`
    — they are not touched here.  Pairs with harmonize's rule_upload_enabled.
    """
    from .. import upload
    client = args.client
    if client not in upload.UPLOAD_ENABLE:
        err(f"{client} has no upstream upload path.")
        info("upload-capable clients: "
             + ", ".join(sorted(upload.UPLOAD_ENABLE)))
        return 1
    flag, dests = upload.UPLOAD_ENABLE[client]
    delivery_knob = upload.DELIVERY_ON_ENABLE.get(client)  # (key, default, choices) or None
    instance = getattr(args, "instance", None)
    on = getattr(args, "on", False)
    off = getattr(args, "off", False)
    via = getattr(args, "via", None)

    # No --on/--off -> read-only status view across the per-instance envs.
    if not (on or off):
        env_dir = Path("/etc") / client / "env"
        envs = sorted(env_dir.glob("*.env")) if env_dir.is_dir() else []
        heading(f"{client} upload ({' / '.join(dests)})  —  flag {flag}")
        if not envs:
            info("no per-instance env files yet "
                 "(run `smd admin instance add` first).")
            return 0
        for e in envs:
            val = upload.read_flag(e, flag)
            state = "ON " if upload.is_truthy(val) else "off"
            extra = ""
            if delivery_knob:
                dkey = delivery_knob[0]
                dval = upload.read_flag(e, dkey)
                extra = f"  [{dkey}={dval if dval is not None else 'unset (default server-merge)'}]"
            print(f"    {state}  {e.stem}    "
                  f"({flag}={val if val is not None else 'unset'}){extra}")
        info(f"enable:  smd config upload {client} <instance> --on")
        return 0

    if not instance:
        err("an instance is required to change the flag, e.g. "
            f"`smd config upload {client} AC0G/S --on`")
        return 1
    try:
        path, flag, dests, delivery_set = upload.apply_enable(
            client, instance, on, delivery=via)
    except ValueError as e:
        err(str(e))
        if delivery_knob:
            info(f"valid --via choices: {', '.join(delivery_knob[2])}")
        return 1
    ok(f"{'enabled' if on else 'disabled'} upload for {client}@{instance}  "
       f"({flag}={'1' if on else '0'})  ->  {', '.join(dests)}")
    if delivery_set:
        dkey, dval = delivery_set
        info(f"delivery: {dkey}={dval}"
             + ("  (standalone direct-to-pskreporter)" if dval == "direct"
                else "  (via wsprdaemon server)"))
    info(f"wrote {path}")
    if on:
        info("identity: `smd config render` (site-profile)  ·  "
             "credentials: `smd admin secrets`")
    info(f"restart to apply:  sudo systemctl restart '{client}@{instance}'")
    return 0
