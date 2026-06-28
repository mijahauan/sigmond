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
            'metrology': {'expected': 9, 'running': 9},
            'radiod': {'fresh': True, 'lag_s': 1.0, 'instance': 'radiod@test.service'},
            'ring_alarm': {'fresh': True, 'present': True, 'ok': True, 'failed': []}}


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


# --- radiod RTP-substrate slide detection + guarded restart ----------------

import json


def _status(wall_iso, last_sample_times):
    """A core-recorder-status.json string with the given channel sample times."""
    return json.dumps({
        'timestamp': wall_iso,
        'channels': {f'c{i}': {'last_sample_time': s}
                     for i, s in enumerate(last_sample_times)},
    })


def test_radiod_rtp_facts_measures_lag():
    # most-behind channel 900s behind wall; file fresh (now ≈ wall)
    import datetime as _dt
    wall = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    wall_iso, wall_ep = wall.isoformat(), wall.timestamp()
    txt = _status(wall_iso, [wall_ep - 900.0, wall_ep - 5.0])
    f = t.radiod_rtp_facts(txt, now=wall_ep + 10.0)
    assert f['fresh'] is True
    assert abs(f['lag_s'] - 900.0) < 0.01          # most-behind channel wins


def test_radiod_rtp_facts_stale_file_not_fresh():
    import datetime as _dt
    wall = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    txt = _status(wall.isoformat(), [wall.timestamp() - 1.0])
    f = t.radiod_rtp_facts(txt, now=wall.timestamp() + 999.0)   # 999s old file
    assert f['fresh'] is False


def test_radiod_rtp_facts_ignores_warming_up_channels():
    import datetime as _dt
    wall = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    ep = wall.timestamp()
    txt = _status(wall.isoformat(), [0, None, ep - 2.0])        # only the real one counts
    f = t.radiod_rtp_facts(txt, now=ep + 1.0)
    assert abs(f['lag_s'] - 2.0) < 0.01


def test_radiod_rtp_facts_bad_json():
    f = t.radiod_rtp_facts('not json', now=1.0)
    assert f == {'fresh': False, 'lag_s': None, 'file_age_s': None}


def _radiod(fresh=True, lag=1.0, instance='radiod@test.service'):
    return {'fresh': fresh, 'lag_s': lag, 'instance': instance}


def test_assess_radiod_slide_is_fail_with_restart_action():
    f = _healthy(); f['radiod'] = _radiod(lag=5510.0)
    link = next(l for l in t.assess(f) if l.name == 'radiod-rtp')
    assert link.status == 'fail' and link.action == 'restart-radiod'


def test_assess_radiod_warn_no_action():
    f = _healthy(); f['radiod'] = _radiod(lag=90.0)
    link = next(l for l in t.assess(f) if l.name == 'radiod-rtp')
    assert link.status == 'warn' and link.action == ''


def test_assess_radiod_stale_status_never_acts():
    f = _healthy(); f['radiod'] = _radiod(fresh=False, lag=None)
    link = next(l for l in t.assess(f) if l.name == 'radiod-rtp')
    assert link.status == 'warn' and link.action == ''         # down recorder ≠ restart radiod


def test_reconcile_radiod_slide_restarts_radiod(monkeypatch):
    monkeypatch.setattr(t, '_read_restart_state', lambda path: {})   # no cooldown
    f = _healthy(); f['radiod'] = _radiod(lag=5510.0)
    acts = t.reconcile(f, dry_run=True, run=_ok_run)
    assert any('would restart radiod@test.service' in a for a in acts)
    assert not any('restart chrony' in a for a in acts)
    assert not any('restart gpsd' in a for a in acts)


def test_radiod_restart_decision_restart_when_clear():
    d, _ = t.radiod_restart_decision({}, 'radiod@x.service', now=10_000.0)
    assert d == 'restart'


def test_radiod_restart_decision_cooldown():
    state = {'last_restart': 10_000.0, 'history': [10_000.0]}
    d, _ = t.radiod_restart_decision(state, 'radiod@x.service', now=10_060.0)  # 60s < 600s
    assert d == 'cooldown'


def test_radiod_restart_decision_escalates_after_cap():
    # 3 restarts within the last hour, last one >cooldown ago
    state = {'last_restart': 9000.0, 'history': [7300.0, 8000.0, 9000.0]}
    d, _ = t.radiod_restart_decision(state, 'radiod@x.service', now=10_000.0)
    assert d == 'escalate'


def test_radiod_restart_decision_no_instance():
    d, _ = t.radiod_restart_decision({}, None, now=10_000.0)
    assert d == 'no-instance'


# --- hot-ring ownership alarm + guarded recovery ---------------------------

def test_ring_alarm_facts_healthy():
    import datetime as _dt
    wall = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    txt = json.dumps({'timestamp': wall.isoformat(),
                      'ring_alarm': {'ok': True, 'failed_channels': {}}})
    f = t.ring_alarm_facts(txt, now=wall.timestamp() + 5)
    assert f['present'] and f['fresh'] and f['ok'] and f['failed'] == []


def test_ring_alarm_facts_foreign_owned():
    import datetime as _dt
    wall = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    txt = json.dumps({'timestamp': wall.isoformat(),
                      'ring_alarm': {'ok': False,
                                     'failed_channels': {'SHARED_10000': 'foreign-owned-shm'}}})
    f = t.ring_alarm_facts(txt, now=wall.timestamp() + 5)
    assert f['present'] and not f['ok'] and f['failed'] == ['SHARED_10000']


def test_ring_alarm_facts_absent_field_is_not_present():
    import datetime as _dt
    wall = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    txt = json.dumps({'timestamp': wall.isoformat()})   # older recorder, no ring_alarm
    f = t.ring_alarm_facts(txt, now=wall.timestamp() + 5)
    assert f['present'] is False and f['ok'] is True


def test_assess_ring_foreign_owned_fails_with_recover_action():
    f = _healthy()
    f['ring_alarm'] = {'fresh': True, 'present': True, 'ok': False, 'failed': ['SHARED_10000']}
    link = next(l for l in t.assess(f) if l.name == 'ring-shm')
    assert link.status == 'fail' and link.action == 'recover-rings'


def test_assess_ring_absent_field_emits_no_link():
    f = _healthy()
    f.pop('ring_alarm', None)
    assert not any(l.name == 'ring-shm' for l in t.assess(f))


def test_assess_ring_stale_status_never_acts():
    f = _healthy()
    f['ring_alarm'] = {'fresh': False, 'present': True, 'ok': False, 'failed': ['x']}
    link = next(l for l in t.assess(f) if l.name == 'ring-shm')
    assert link.status == 'warn' and link.action == ''


def test_reconcile_ring_alarm_restarts_recorder_only(monkeypatch):
    monkeypatch.setattr(t, '_read_restart_state', lambda path: {})   # no cooldown
    f = _healthy()
    f['ring_alarm'] = {'fresh': True, 'present': True, 'ok': False, 'failed': ['SHARED_10000']}
    acts = t.reconcile(f, dry_run=True, run=_ok_run)
    assert any('would restart timestd-core-recorder.service' in a for a in acts)
    assert not any('radiod' in a for a in acts)           # own component, not the shared SDR
    assert not any('restart chrony' in a for a in acts)
