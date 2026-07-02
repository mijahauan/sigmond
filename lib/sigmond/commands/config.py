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

    # All rows go to stderr to match heading()/info() — otherwise stdout
    # (block-buffered when piped) reorders relative to the headings.
    def _row(s=''):
        print(s, file=sys.stderr)

    # ── coordination — the shared map every client reads ─────────────────────
    heading('coordination')
    info(str(coord.source_path) if coord.source_path
         else '(defaults — no coordination.toml)')
    info('cross-client settings every client reads: station identity + the '
         'radiod instance(s) clients bind to.')
    _call, _grid = coord.host.call or '', coord.host.grid or ''
    _row(f'    station   {(_call + " / " + _grid) if (_call or _grid) else "(not set)"}')
    if coord.radiods:
        for rid, r in sorted(coord.radiods.items()):
            loc = 'local' if r.is_local else f'remote {r.host}'
            sr = f'{r.samprate_hz} Hz' if r.samprate_hz else 'samprate ?'
            _row(f'    radiod    {rid}  [{loc}]  {sr}  →  {r.status_dns or "?"}')
    else:
        _row('    radiod    (none declared)')

    # ── clients — ordered foundation → no-config → needs-config ──────────────
    if not view.client_views:
        return 0
    try:
        from .. import psws
        _ps = {r: psws.read_state(r) for r in ('hf-timestd', 'mag-recorder')}
    except Exception:
        _ps = {}
    # ka9q-radio is fundamental (runs the radiod); wspr/psk auto-configure;
    # hf-timestd + mag-recorder need operator-supplied PSWS info → listed last.
    _ORDER = {'ka9q-radio': 0, 'wspr-recorder': 1, 'psk-recorder': 2,
              'hf-timestd': 3, 'mag-recorder': 4}
    names = sorted(view.client_views, key=lambda n: (_ORDER.get(n, 9), n))
    heading('clients')
    info('each binds to the radiod above; foundation first, the two that need '
         'PSWS info (same format) last.')
    W = max(len(n) for n in names)
    for name in names:
        cv = view.client_views[name]
        state = 'installed' if cv.installed else 'not installed'
        summary = _client_summary(name, cv)
        _row(f'    {name.ljust(W)}  [{state}]'
             + (f'   {summary}' if summary else ''))
        pad = ' ' * (W + 6)
        st = _ps.get(name)
        if st is not None and st.config_exists:
            # PSWS recorders: ONE consistent format for hf-timestd AND
            # mag-recorder (what the upload needs, or "ready").
            if st.configured:
                _row(f'    {pad}\033[32m✓\033[0m PSWS upload configured')
            else:
                _row(f'    {pad}\033[33m⚠\033[0m PSWS upload needs: '
                     f'{", ".join(st.issues)}')
                _row(f'    {pad}  → smd config {name} edit')
        else:
            for iss in cv.issues:
                txt = iss.split('] ', 1)[-1] if '] ' in iss[:8] else iss
                _row(f'    {pad}\033[33m⚠\033[0m {txt}')
    return 0


def _client_summary(name: str, cv) -> str:
    """One compact phrase per client for `smd config show`."""
    if not cv.installed:
        return ''
    if name == 'ka9q-radio':
        rids = sorted({iv.radiod_id for iv in cv.instances if iv.radiod_id})
        return f'runs radiod {", ".join(rids)}' if rids else 'runs the local radiod'
    if name == 'mag-recorder':
        return 'RM3100 magnetometer (no radiod)'
    ch = sum((iv.ka9q_channels or 0) for iv in cv.instances)
    return f'{ch} channels' if ch else ''


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
        if getattr(args, 'if_present', False):
            # Bring-up plans include an unconditional render step; a host
            # without a site profile (legacy prompt-driven identity) is a
            # quiet no-op, not an error.
            info(f'{SITE_PROFILE_PATH} not present — skipping render')
            return 0
        err(f'{SITE_PROFILE_PATH} not found')
        info('  scaffold one with:  smd config render --init')
        return 1

    problems = []
    if not profile.call:
        problems.append('station.callsign is empty')
    if not profile.grid and not (profile.lat or profile.lon):
        problems.append('need station.grid_square or latitude/longitude')
    if profile.psws_enabled and not profile.psws_station_id:
        problems.append('psws.enabled but station_id missing')
    if problems:
        for p in problems:
            err(p)
        return 1

    station = Station(
        psws_id=profile.psws_station_id if profile.psws_enabled else '',
        instrument_id=(profile.instrument_for('hf-timestd')
                       if profile.psws_enabled else ''),
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

    if profile.psws_enabled:
        _push_psws_identity(profile)
        _report_station_key(profile)

    print()
    loc = profile.grid or f'{profile.lat},{profile.lon}'
    info(f'operator: {profile.call} / {loc}')
    if profile.psws_enabled:
        info(f'PSWS: station {profile.psws_station_id}'
             + ''.join(f' / {rec}={profile.instrument_for(rec)}'
                       for rec in ('hf-timestd', 'mag-recorder')
                       if profile.instrument_for(rec)))
    info('clients pick up the new values on next service start or reload.')
    return 0


def plan_psws_updates(profile, recorder: str, state) -> list:
    """(section, key, value) writes that bring one PSWS recorder's own
    config file in line with the site profile.

    The uploader manifest resolves ``{station_id}``/``{instrument_id}``
    from each recorder's config (psws.read_state), so the profile must
    be pushed THROUGH to those files — coordination [station] alone
    only feeds wizard defaults. Empty desired values never clobber a
    configured one (a hand-configured host keeps its ids when the
    profile leaves them blank)."""
    from .. import psws
    spec = psws.RECORDERS[recorder]
    updates = []
    want_station = profile.psws_station_id
    want_instrument = profile.instrument_for(recorder)
    if want_station and state.station != want_station:
        updates.append(('.'.join(spec['station'][:-1]),
                        spec['station'][-1], want_station))
    if want_instrument and state.instrument != want_instrument:
        updates.append(('.'.join(spec['instrument'][:-1]),
                        spec['instrument'][-1], want_instrument))
    return updates


def _push_psws_identity(profile) -> None:
    """Push PSWS station/instrument ids into each installed recorder."""
    from .. import psws
    for recorder in psws.RECORDERS:
        try:
            state = psws.read_state(recorder)
        except Exception as exc:                          # noqa: BLE001
            warn(f'psws: could not read {recorder} state: {exc}')
            continue
        if not state.config_exists:
            info(f'psws: {recorder} config not present yet — skipped '
                 '(re-run `smd config render` after its config init)')
            continue
        updates = plan_psws_updates(profile, recorder, state)
        if not updates:
            info(f'psws: {recorder} already current')
            continue
        try:
            psws._set_fields(recorder, updates)
        except Exception as exc:                          # noqa: BLE001
            warn(f'psws: could not write {recorder} config: {exc}')
            continue
        ok('psws: ' + recorder + ' ← '
           + ', '.join(f'{key}={val}' for _, key, val in updates))


def _report_station_key(profile) -> None:
    """Ensure the station SSH key exists and remind the operator to
    register its pubkey on the PSWS portal.

    One host key serves every SFTP destination (single-host uploader
    model); hs-uploader also self-generates it on first ship, but
    generating it HERE gives the operator the pubkey to register
    BEFORE the first upload attempt."""
    import subprocess
    from .. import psws
    key = Path('/etc/hs-uploader/keys/id_ed25519_host')
    pub = Path(str(key) + '.pub')
    if not key.exists():
        try:
            key.parent.mkdir(parents=True, exist_ok=True)
            import socket
            subprocess.run(
                ['ssh-keygen', '-q', '-t', 'ed25519', '-f', str(key),
                 '-N', '', '-C', f'hs-uploader@{socket.gethostname()}'],
                check=True)
            subprocess.run(['chown', 'hsupload:hsupload', str(key),
                            str(pub)], check=False)
            os.chmod(key, 0o600)
            ok(f'generated station SSH key {key}')
        except Exception as exc:                          # noqa: BLE001
            warn(f'could not generate station key {key}: {exc} — '
                 'hs-uploader will self-generate on first upload')
            return
    try:
        pubkey = pub.read_text().strip()
    except OSError:
        return
    print()
    info(f'REGISTER this public key for station {profile.psws_station_id} '
         f'at {psws.PSWS_PORTAL}:')
    print(f'    {pubkey}')


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
