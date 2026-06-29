"""smd admin timing — single idempotent reconciler for the GPSDO -> gpsd -> chrony ->
hf-timestd timing chain (docs/timing-chain-architecture.md, step 3).

Replaces the per-component watchdogs.  `smd admin timing [status]` reports chain
health; `smd admin timing reconcile` applies OWN-ONLY remediation — it NEVER restarts
a shared dependency to *paper over* a downstream consumer's health (the cascade that
put the GPS reference on internet NTP).  It is the single actor allowed to act on the
chain.

The one shared component it will restart is **radiod itself, and only on a
radiod-intrinsic fault**: when radiod's own RTP timestamps slide behind UTC, the
timing substrate is corrupt for *every* client (hf-timestd, psk-recorder,
wspr-recorder…), not just one consumer — recorders drop the stale data outright.
Restarting radiod re-latches it to the (healthy) GPS/PPS reference and protects data
quality fleet-wide, so it is a legitimate chain-substrate repair rather than the
scapegoat-a-shared-dep anti-pattern.  The restart is guarded: it fires only when the
recorder's status file is fresh (recorder alive) and the measured lag exceeds the
recorder's own stale-drop limit, and it is rate-limited by a cooldown + an
escalation cap (after which it suspends and asks for a human).

It also watches the recorder's `ring_alarm` (hot-ring ownership) and, on a
foreign-owned-shm alarm, restarts the recorder — an hf-timestd OWN component,
so fully within the own-only rule — whose root ExecStartPre clears the stale
segment.  Same cooldown/escalation guard.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from ..ui import err, heading, info, ok, warn

# NTP SHM refclock segment keys (units 0-3; key == ascii "NTP0".."NTP3").
SHM_KEYS = {0: '0x4e545030', 1: '0x4e545031', 2: '0x4e545032', 3: '0x4e545033'}
METROLOGY_ENV_DIR = Path('/etc/hf-timestd/metrology-channels')

# radiod RTP-substrate health.  The hf-timestd core-recorder publishes a status
# snapshot with a wall-clock `timestamp` and per-channel `last_sample_time`
# (RTP-derived); their difference is how far radiod's RTP stream lags real time.
RECORDER_STATUS_FILE = Path('/var/lib/timestd/status/core-recorder-status.json')
RADIOD_LAG_WARN_S = 60.0            # watch
RADIOD_LAG_FAIL_S = 150.0          # past the recorder's own 120 s stale-drop limit → act
RECORDER_STATUS_FRESH_S = 120.0    # only trust the lag if the status file is this fresh
RADIOD_RESTART_STATE = Path('/run/sigmond/radiod-restart.json')
RADIOD_RESTART_COOLDOWN_S = 600.0  # ≥10 min between radiod restarts (it needs time to re-sync)
RADIOD_RESTART_MAX_PER_HOUR = 3    # after this many in an hour: suspend + escalate to a human
# The recorder also publishes a `ring_alarm` field when a channel's hot ring
# (the metrology feed) is unavailable — typically a foreign-owned stale SysV
# segment it cannot reclaim.  Recovery = restart the recorder, whose root
# ExecStartPre `clean-stale-rings` removes the foreign segment.
RING_RECOVER_STATE = Path('/run/sigmond/ring-recover.json')
# hf-timestd's RadiodTimingWatchdog publishes the latest GROSS RTP↔UTC mapping
# jump here (a >2 s thrash, distinct from the ordinary sub-second slide).  The
# lag check above can't see a thrash — the mapping is momentarily self-consistent
# so `last_sample_time` stays current — so this is a separate, complementary link.
WATCHDOG_STATUS_FILE = Path('/var/lib/timestd/status/radiod-timing-watchdog.json')
WATCHDOG_INCIDENT_RECENT_S = 3600.0   # surface a thrash incident for an hour after it fires


@dataclass
class Link:
    name: str
    status: str            # 'ok' | 'warn' | 'fail'
    detail: str
    action: str = ''       # remediation id consumed by reconcile()


# --- pure parsers (unit-testable) -----------------------------------------

def parse_sources(text: str) -> dict:
    """`chronyc sources` -> {name: {'reach': int, 'sel': '#*'|'^-'|...}}."""
    out: dict = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[0] or parts[0][0] not in '#^':
            continue
        try:
            out[parts[1]] = {'reach': int(parts[4]), 'sel': parts[0]}
        except (ValueError, IndexError):
            continue
    return out


def parse_tracking(text: str) -> dict:
    d: dict = {}
    for line in text.splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            d[k.strip()] = v.strip()
    return {'ref': d.get('Reference ID', ''), 'stratum': d.get('Stratum', ''),
            'leap': d.get('Leap status', '')}


def parse_shm(text: str) -> dict:
    """`ipcs -m` -> {unit: {'owner': str, 'perm': str}} for NTP0-3."""
    out: dict = {}
    for line in text.splitlines():
        for unit, key in SHM_KEYS.items():
            if key in line:
                p = line.split()
                if len(p) >= 4:
                    out[unit] = {'owner': p[2], 'perm': p[3]}
    return out


def _parse_iso_epoch(ts) -> Optional[float]:
    """ISO-8601 string -> POSIX seconds (UTC-aware), or None if unparseable."""
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def radiod_rtp_facts(status_text: str, now: float) -> dict:
    """Parse core-recorder-status.json -> radiod RTP-substrate health.

    Returns {'fresh': bool, 'lag_s': float|None, 'file_age_s': float|None}.

    ``lag_s`` is the recorder's own measurement at write time
    (status.timestamp − the most-behind channel's last_sample_time), so it is
    independent of how stale the file is; ``fresh`` separately reports whether
    the recorder is currently alive and writing.  Channels with no positive
    sample time yet (just-created, no data) are ignored so a warming-up channel
    never looks like a slide.
    """
    try:
        d = json.loads(status_text)
    except (ValueError, TypeError):
        return {'fresh': False, 'lag_s': None, 'file_age_s': None}
    wall = _parse_iso_epoch(d.get('timestamp'))
    if wall is None:
        return {'fresh': False, 'lag_s': None, 'file_age_s': None}
    file_age = now - wall
    fresh = 0 <= file_age < RECORDER_STATUS_FRESH_S
    samples = [c.get('last_sample_time') for c in d.get('channels', {}).values()
               if isinstance(c.get('last_sample_time'), (int, float))
               and c.get('last_sample_time') > 0]
    lag = (wall - min(samples)) if samples else None
    return {'fresh': fresh, 'lag_s': lag, 'file_age_s': file_age}


def radiod_timing_facts(status_text: str, now: float) -> dict:
    """Parse radiod-timing-watchdog.json -> latest gross RTP↔UTC thrash incident.

    Written by hf-timestd's RadiodTimingWatchdog on a >2 s mapping jump (the
    thrash), carrying a verdict that isolates GPS-source-bad from radiod-bad.
    Returns {'present': bool, 'age_s': float|None, 'verdict', 'severity',
    'detail', 'delta_sec'}.
    """
    try:
        d = json.loads(status_text)
    except (ValueError, TypeError):
        return {'present': False}
    epoch = _parse_iso_epoch(d.get('ts'))
    return {
        'present': True,
        'age_s': (now - epoch) if epoch is not None else None,
        'verdict': d.get('verdict'),
        'severity': d.get('severity'),
        'detail': d.get('detail', ''),
        'delta_sec': d.get('delta_sec'),
    }


def ring_alarm_facts(status_text: str, now: float) -> dict:
    """Parse core-recorder-status.json -> hot-ring ownership/health.

    Returns {'fresh': bool, 'present': bool, 'ok': bool, 'failed': [chan,...]}.
    ``present`` is False for an older recorder that doesn't emit ``ring_alarm``
    (so we never act on its absence); ``fresh`` mirrors radiod_rtp_facts.
    """
    try:
        d = json.loads(status_text)
    except (ValueError, TypeError):
        return {'fresh': False, 'present': False, 'ok': True, 'failed': []}
    wall = _parse_iso_epoch(d.get('timestamp'))
    fresh = wall is not None and 0 <= (now - wall) < RECORDER_STATUS_FRESH_S
    alarm = d.get('ring_alarm')
    if not isinstance(alarm, dict):
        return {'fresh': fresh, 'present': False, 'ok': True, 'failed': []}
    failed = sorted((alarm.get('failed_channels') or {}).keys())
    return {'fresh': fresh, 'present': True,
            'ok': bool(alarm.get('ok', True)) and not failed, 'failed': failed}


def radiod_restart_decision(state: dict, instance: Optional[str], now: float) -> tuple:
    """Pure cooldown/escalation gate for a radiod restart.

    Returns ``(decision, detail)`` where decision is one of
    ``'no-instance' | 'cooldown' | 'escalate' | 'restart'``.
    """
    if not instance:
        return ('no-instance', 'no radiod@ instance found — cannot restart')
    last = state.get('last_restart', 0) or 0
    recent = [t for t in state.get('history', []) if now - t < 3600]
    if now - last < RADIOD_RESTART_COOLDOWN_S:
        return ('cooldown', f'within restart cooldown ({int(now - last)}s since last)')
    if len(recent) >= RADIOD_RESTART_MAX_PER_HOUR:
        return ('escalate',
                f'{len(recent)} restarts in the last hour — auto-restart suspended')
    return ('restart', f'restarting {instance}')


# --- assessment (pure): facts -> chain health -----------------------------

def assess(facts: dict) -> list:
    links: list = []
    shm = facts.get('shm', {})
    srcs = facts.get('sources', {})
    met = facts.get('metrology', {})

    missing = [u for u in SHM_KEYS if u not in shm]
    badperm = [u for u in SHM_KEYS if u in shm and shm[u]['perm'] != '666']
    if not shm:
        links.append(Link('shm', 'fail', 'no NTP SHM segments present', 'restart-precreate'))
    elif missing:
        links.append(Link('shm', 'warn', f'NTP{missing} missing', 'restart-precreate'))
    elif badperm:
        links.append(Link('shm', 'warn', f'NTP{badperm} not 0666 — ownership-race risk', ''))
    else:
        links.append(Link('shm', 'ok', 'NTP0-3 present at 0666'))

    if not facts.get('gpsd_active'):
        links.append(Link('gpsd', 'fail', 'gpsd not active', 'restart-gpsd'))
    elif facts.get('gps_fix') is False:
        links.append(Link('gpsd', 'warn', 'gpsd active but no GPS fix (antenna/sky) — hardware', ''))
    else:
        links.append(Link('gpsd', 'ok', 'gpsd active' + (' (GPS fix)' if facts.get('gps_fix') else '')))

    gps_reach = max(srcs.get('LG29', {}).get('reach', 0), srcs.get('PPS', {}).get('reach', 0))
    if gps_reach > 0:
        links.append(Link('gps-feed', 'ok', f'LG29/PPS reach {gps_reach}'))
    elif facts.get('gpsd_active') and facts.get('gps_fix'):
        links.append(Link('gps-feed', 'fail', 'LG29/PPS reach 0 despite a GPS fix — SHM feed broken', 'restart-gpsd'))
    else:
        links.append(Link('gps-feed', 'warn', 'LG29/PPS reach 0 (no fix yet)', ''))

    if not facts.get('chrony_active'):
        links.append(Link('chrony', 'fail', 'chrony not active', 'restart-chrony'))
    else:
        trk = facts.get('tracking', {})
        selected = [n for n, v in srcs.items() if v['sel'][:2] == '#*']
        if selected:
            links.append(Link('chrony', 'ok', f'selected {selected[0]} (stratum {trk.get("stratum")})'))
        else:
            links.append(Link('chrony', 'warn', f'not locked to a local refclock (ref {trk.get("ref")})', ''))

    if 'FUSE' not in srcs:
        links.append(Link('fuse', 'warn', 'FUSE refclock not configured in chrony', ''))
    elif srcs['FUSE']['reach'] > 0:
        links.append(Link('fuse', 'ok', f"FUSE reach {srcs['FUSE']['reach']}"))
    elif met.get('expected', 0) and met.get('running', 0) < met['expected']:
        links.append(Link('fuse', 'fail', f"FUSE reach 0; metrology {met['running']}/{met['expected']} running", 'start-metrology'))
    else:
        links.append(Link('fuse', 'warn', 'FUSE reach 0 (warming up / no HF solution)', ''))

    exp = met.get('expected', 0)
    if exp:
        run = met.get('running', 0)
        if run < exp:
            links.append(Link('metrology', 'fail', f'{run}/{exp} instances running', 'start-metrology'))
        else:
            links.append(Link('metrology', 'ok', f'{run}/{exp} running'))

    # radiod RTP substrate: a slide behind UTC corrupts timing for ALL clients.
    # Absent/stale recorder status => can't tell (warn, never act — a down
    # recorder must not trigger a radiod restart).
    rad = facts.get('radiod', {})
    lag = rad.get('lag_s')
    if not rad.get('fresh'):
        links.append(Link('radiod-rtp', 'warn', 'recorder status missing/stale — RTP lag unknown', ''))
    elif lag is None:
        links.append(Link('radiod-rtp', 'warn', 'recorder status has no channel sample times', ''))
    elif lag >= RADIOD_LAG_FAIL_S:
        links.append(Link('radiod-rtp', 'fail',
                          f'radiod RTP {lag:.0f}s behind UTC — recorders dropping data', 'restart-radiod'))
    elif lag >= RADIOD_LAG_WARN_S:
        links.append(Link('radiod-rtp', 'warn', f'radiod RTP {lag:.0f}s behind UTC (watching)', ''))
    else:
        links.append(Link('radiod-rtp', 'ok', f'radiod RTP {lag:.0f}s behind UTC'))

    # radiod RTP↔UTC THRASH watchdog (complements radiod-rtp lag above): a gross
    # mapping jump — seconds to minutes (the 2026-06-29 outage) — that the lag
    # check can't see because the mapping stays momentarily self-consistent.
    # hf-timestd's watchdog captures a gpsd/chrony evidence bundle + a
    # GPS-source-vs-radiod verdict on each episode; we surface it for an hour.
    rt = facts.get('radiod_timing', {})
    rt_age = rt.get('age_s')
    if rt.get('present') and rt_age is not None and rt_age <= WATCHDOG_INCIDENT_RECENT_S:
        sev = 'fail' if rt.get('severity') == 'fail' else 'warn'
        links.append(Link('radiod-timing', sev,
                          f"{rt.get('verdict')} {rt_age / 60:.0f}m ago — {rt.get('detail', '')}", ''))
    else:
        links.append(Link('radiod-timing', 'ok', 'no recent RTP↔UTC thrash incident'))

    # Hot-ring ownership: a foreign-owned stale SysV segment starves a
    # channel's metrology feed (frozen L1).  Only act when the recorder is
    # fresh and explicitly reports the alarm (older recorders omit it).
    ra = facts.get('ring_alarm', {})
    if not ra.get('present'):
        pass  # field absent (older recorder) — nothing to assert
    elif not ra.get('fresh'):
        links.append(Link('ring-shm', 'warn', 'recorder status stale — ring health unknown', ''))
    elif not ra.get('ok'):
        links.append(Link('ring-shm', 'fail',
                          f"hot ring unavailable for {', '.join(ra.get('failed', [])) or '?'} "
                          f"(foreign-owned shm) — metrology starving", 'recover-rings'))
    else:
        links.append(Link('ring-shm', 'ok', 'hot rings owned + healthy'))
    return links


# --- I/O: gather facts + own-only remediation -----------------------------

def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 1, '', str(e))


def _gps_fix(run) -> Optional[bool]:
    r = run(['timeout', '4', 'gpspipe', '-w', '-n', '6'])
    if getattr(r, 'returncode', 1) != 0 or not r.stdout:
        return None
    modes = [int(m.split(':')[1]) for m in re.findall(r'"mode":[0-9]', r.stdout)]
    return (max(modes) >= 2) if modes else None


def gather_facts(run: Callable = _run, quick: bool = False) -> dict:
    f: dict = {}
    f['shm'] = parse_shm(run(['ipcs', '-m']).stdout)
    f['gpsd_active'] = run(['systemctl', 'is-active', 'gpsd']).stdout.strip() == 'active'
    f['chrony_active'] = run(['systemctl', 'is-active', 'chrony']).stdout.strip() == 'active'
    f['sources'] = parse_sources(run(['chronyc', 'sources']).stdout)
    f['tracking'] = parse_tracking(run(['chronyc', 'tracking']).stdout)
    f['gps_fix'] = None if quick else _gps_fix(run)
    try:
        expected = len(list(METROLOGY_ENV_DIR.glob('*.env')))
    except OSError:
        expected = 0
    running = sum(1 for l in run(['systemctl', 'list-units', 'timestd-metrology@*',
                                  '--no-legend', '--no-pager']).stdout.splitlines()
                  if ' running' in l)
    f['metrology'] = {'expected': expected, 'running': running}

    try:
        status_text = RECORDER_STATUS_FILE.read_text()
    except OSError:
        status_text = ''
    now = time.time()
    f['radiod'] = radiod_rtp_facts(status_text, now)
    f['radiod']['instance'] = _radiod_instance(run)
    f['ring_alarm'] = ring_alarm_facts(status_text, now)
    try:
        wd_text = WATCHDOG_STATUS_FILE.read_text()
    except OSError:
        wd_text = ''
    f['radiod_timing'] = radiod_timing_facts(wd_text, now)
    return f


def _radiod_instance(run) -> Optional[str]:
    """Discover the active radiod@<id>.service unit (the shared SDR daemon)."""
    r = run(['systemctl', 'list-units', 'radiod@*.service',
             '--no-legend', '--no-pager', '--plain'])
    for line in getattr(r, 'stdout', '').splitlines():
        parts = line.split()
        if parts and parts[0].startswith('radiod@'):
            return parts[0]
    return None


def _read_restart_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_restart_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        pass


def _restart_radiod(facts, dry_run, run) -> str:
    """Guarded restart of the shared radiod when its RTP slid behind UTC.

    Cooldown + escalation-capped (see radiod_restart_decision).  radiod is the
    timing substrate for every client, so this is a deliberate, rate-limited
    repair of the faulting component — not a downstream-consumer workaround.
    """
    instance = facts.get('radiod', {}).get('instance')
    now = time.time()
    decision, detail = radiod_restart_decision(
        _read_restart_state(RADIOD_RESTART_STATE), instance, now)
    if decision == 'no-instance':
        return f'radiod RTP behind UTC but {detail}'
    if decision == 'cooldown':
        return f'radiod RTP behind UTC — {detail}; not restarting again'
    if decision == 'escalate':
        return f'radiod RTP STILL behind — {detail}; MANUAL INTERVENTION needed for {instance}'
    if dry_run:
        return f'(dry-run) would restart {instance} (radiod RTP behind UTC)'
    r = run(['systemctl', 'restart', instance])
    succeeded = getattr(r, 'returncode', 1) == 0
    if succeeded:
        state = _read_restart_state(RADIOD_RESTART_STATE)
        history = [t for t in state.get('history', []) if now - t < 3600]
        history.append(now)
        _write_restart_state(RADIOD_RESTART_STATE, {'last_restart': now, 'history': history})
    return (f'restart {instance} (radiod RTP behind UTC): '
            + ('ok' if succeeded else 'FAILED: ' + (getattr(r, 'stderr', '') or '').strip()))


def _recover_rings(facts, dry_run, run) -> str:
    """Guarded recovery of the core-recorder hot rings on a ring-ownership alarm.

    A foreign-owned SysV ring segment (e.g. a stale `radio`-owned segment at an
    hf-timestd ring key) can only be removed by its owner or root, so the
    timestd recorder cannot reclaim it and its metrology consumer starves.
    Restarting the recorder runs its root ExecStartPre `clean-stale-rings`,
    which clears the foreign segment so the recorder recreates a self-owned
    ring.  Cooldown + escalation-capped, same as the radiod-rtp guard.
    """
    unit = 'timestd-core-recorder.service'
    now = time.time()
    decision, detail = radiod_restart_decision(
        _read_restart_state(RING_RECOVER_STATE), unit, now)
    chans = ', '.join(sorted(facts.get('ring_alarm', {}).get('failed', []))) or 'recorder'
    if decision == 'cooldown':
        return f'ring ownership alarm ({chans}) — {detail}; not restarting again'
    if decision == 'escalate':
        return (f'ring ownership alarm ({chans}) PERSISTS — {detail}; MANUAL '
                f'INTERVENTION needed (check `ipcs -m` for foreign-owned ring keys)')
    if dry_run:
        return f'(dry-run) would restart {unit} to clean foreign rings ({chans})'
    r = run(['systemctl', 'restart', unit])
    succeeded = getattr(r, 'returncode', 1) == 0
    if succeeded:
        state = _read_restart_state(RING_RECOVER_STATE)
        history = [t for t in state.get('history', []) if now - t < 3600]
        history.append(now)
        _write_restart_state(RING_RECOVER_STATE, {'last_restart': now, 'history': history})
    return (f'restart {unit} to clean foreign rings ({chans}): '
            + ('ok' if succeeded else 'FAILED: ' + (getattr(r, 'stderr', '') or '').strip()))


def _start_missing_metrology(run, dry_run) -> str:
    chans = [p.stem for p in METROLOGY_ENV_DIR.glob('*.env')] if METROLOGY_ENV_DIR.is_dir() else []
    acted = []
    for ch in sorted(chans):
        unit = f'timestd-metrology@{ch}.service'
        if run(['systemctl', 'is-active', unit]).stdout.strip() != 'active':
            if dry_run:
                acted.append(ch + '(dry)')
            else:
                r = run(['systemctl', 'start', unit])
                acted.append(ch if getattr(r, 'returncode', 1) == 0 else ch + '(FAIL)')
    return ', '.join(acted) or 'none down'


_REMEDIES = {
    'restart-precreate': (['systemctl', 'restart', 'sigmond-shm-precreate.service'],
                          'recreate NTP SHM segments (0666)'),
    'restart-gpsd':      (['systemctl', 'restart', 'gpsd'],
                          'restart gpsd (re-establish GPS SHM feed)'),
    'restart-chrony':    (['systemctl', 'restart', 'chrony'],
                          'restart chrony (reconciler is the ONLY allowed chrony-restarter)'),
}


def reconcile(facts: dict, *, dry_run: bool, run: Callable = _run) -> list:
    actions: list = []
    needed: list = []
    for link in assess(facts):
        act = link.action
        if not act:
            continue
        if link.status == 'fail' or (link.status == 'warn' and act == 'restart-precreate'):
            if act not in needed:
                needed.append(act)
    for rem in needed:
        if rem == 'start-metrology':
            actions.append(f"{'(dry-run) ' if dry_run else ''}start down metrology writers: {_start_missing_metrology(run, dry_run)}")
            continue
        if rem == 'restart-radiod':
            actions.append(_restart_radiod(facts, dry_run, run))
            continue
        if rem == 'recover-rings':
            actions.append(_recover_rings(facts, dry_run, run))
            continue
        cmd, desc = _REMEDIES[rem]
        if dry_run:
            actions.append(f"(dry-run) would {desc}")
        else:
            r = run(cmd)
            actions.append(f"{desc}: {'ok' if getattr(r, 'returncode', 1) == 0 else 'FAILED: ' + (r.stderr or '').strip()}")
    if not needed:
        actions.append('chain healthy — nothing to reconcile')
    return actions


# --- CLI -------------------------------------------------------------------

def cmd_timing(args) -> int:
    verb = getattr(args, 'timing_cmd', None) or 'status'
    facts = gather_facts()
    links = assess(facts)
    _emit = {'ok': ok, 'warn': warn, 'fail': err}

    heading(f'timing chain ({verb})')
    for link in links:
        _emit.get(link.status, info)(f"{link.name:10s} {link.detail}")
    fails = [l for l in links if l.status == 'fail']

    if verb == 'reconcile':
        if os.geteuid() != 0:
            err('reconcile needs root — run: smd admin timing reconcile')
            return 1
        heading('reconcile (own-only)')
        for a in reconcile(facts, dry_run=getattr(args, 'dry_run', False)):
            (warn if 'FAILED' in a else (info if ('healthy' in a or 'dry-run' in a) else ok))(a)
        return 0

    if fails:
        warn(f'{len(fails)} link(s) failing — fix: smd admin timing reconcile')
        return 1
    ok('timing chain healthy')
    return 0
