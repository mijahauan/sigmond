"""smd admin timing — single idempotent reconciler for the GPSDO -> gpsd -> chrony ->
hf-timestd timing chain (docs/timing-chain-architecture.md, step 3).

Replaces the per-component watchdogs.  `smd admin timing [status]` reports chain
health; `smd admin timing reconcile` applies OWN-ONLY remediation — it NEVER restarts
a shared dependency to fix a downstream consumer (the cascade that put the GPS
reference on internet NTP).  It is the single actor allowed to act on the chain.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..ui import err, heading, info, ok, warn

# NTP SHM refclock segment keys (units 0-3; key == ascii "NTP0".."NTP3").
SHM_KEYS = {0: '0x4e545030', 1: '0x4e545031', 2: '0x4e545032', 3: '0x4e545033'}
METROLOGY_ENV_DIR = Path('/etc/hf-timestd/metrology-channels')


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
    return f


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
