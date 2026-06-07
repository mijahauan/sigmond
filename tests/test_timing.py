"""Unit tests for the timing-chain reconciler (pure logic, no live I/O)."""
import subprocess
from sigmond.commands import timing as t

SOURCES = ("MS Name\n"
           "#- LG29 0 4 377 21 +37ms\n"
           "#* PPS 0 4 377 23 -2ns\n"
           "#- FUSE 0 4 224 62 -15ms\n"
           "^? pool 2 6 0 - +0\n")
SHM = ("0x4e545030 8 root 666 96 1\n0x4e545031 9 root 666 96\n"
       "0x4e545032 1 root 666 96\n0x4e545033 2 root 666 96\n")


def _ok_run(c):
    return subprocess.CompletedProcess(c, 0, '', '')


def _healthy():
    return {'shm': t.parse_shm(SHM), 'gpsd_active': True, 'gps_fix': True,
            'chrony_active': True, 'sources': t.parse_sources(SOURCES),
            'tracking': {'stratum': '1', 'ref': 'PPS'},
            'metrology': {'expected': 9, 'running': 9}}


def test_parse_sources():
    s = t.parse_sources(SOURCES)
    assert s['PPS'] == {'reach': 377, 'sel': '#*'}
    assert s['FUSE']['reach'] == 224


def test_parse_shm():
    shm = t.parse_shm(SHM)
    assert set(shm) == {0, 1, 2, 3} and shm[0]['perm'] == '666'


def test_healthy_chain_all_ok():
    assert all(l.status == 'ok' for l in t.assess(_healthy()))


def test_reconcile_healthy_is_noop():
    assert t.reconcile(_healthy(), dry_run=True, run=_ok_run) == \
        ['chain healthy — nothing to reconcile']


def test_metrology_down_starts_writers_not_chrony():
    f = _healthy()
    f['metrology'] = {'expected': 9, 'running': 3}
    f['sources'] = dict(f['sources'])
    f['sources']['FUSE'] = {'reach': 0, 'sel': '#?'}
    acts = t.reconcile(f, dry_run=True, run=_ok_run)
    assert any('metrology' in a for a in acts)
    assert not any('restart chrony' in a for a in acts)        # own-only


def test_chrony_dead_allows_chrony_restart():
    f = _healthy(); f['chrony_active'] = False
    assert any('restart chrony' in a for a in t.reconcile(f, dry_run=True, run=_ok_run))


def test_gpsd_dead_restarts_gpsd_only():
    f = _healthy(); f['gpsd_active'] = False
    acts = t.reconcile(f, dry_run=True, run=_ok_run)
    assert any('restart gpsd' in a for a in acts)
    assert not any('restart chrony' in a for a in acts)        # own-only
