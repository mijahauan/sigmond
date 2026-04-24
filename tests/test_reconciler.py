"""Tests for the discovery reconciler — match declared vs observed and
classify deltas."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.environment import (
    DeclaredGpsdo,
    DeclaredKiwi,
    DeclaredRadiod,
    DeclaredTimeSource,
    Environment,
    Observation,
)
from sigmond.discovery.reconciler import reconcile


def _env(**kw) -> Environment:
    return Environment(
        radiods=kw.get('radiods', []),
        kiwisdrs=kw.get('kiwisdrs', []),
        gpsdos=kw.get('gpsdos', []),
        time_sources=kw.get('time_sources', []),
    )


def _obs(**kw) -> Observation:
    defaults = dict(source='mdns', kind='radiod', id=None, endpoint='',
                    fields={}, observed_at=100.0, ok=True, error='')
    defaults.update(kw)
    return Observation(**defaults)


class MatchingTests(unittest.TestCase):
    def test_id_match_is_preferred(self):
        env = _env(radiods=[DeclaredRadiod(id='r1', host='bee1.local',
                                           status_dns='hf-status.local')])
        deltas = reconcile(env, [_obs(id='r1', endpoint='hf-status.local')])
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].status, 'healthy')
        self.assertEqual(deltas[0].id, 'r1')

    def test_host_match_backfills_id(self):
        env = _env(radiods=[DeclaredRadiod(id='r1', host='bee1.local',
                                           status_dns='hf-status.local')])
        obs = _obs(id=None, endpoint='bee1.local:5004')
        deltas = reconcile(env, [obs])
        self.assertEqual(deltas[0].status, 'healthy')
        self.assertEqual(deltas[0].id, 'r1')
        self.assertEqual(obs.id, 'r1')       # backfilled

    def test_declared_with_no_observation_is_missing(self):
        env = _env(kiwisdrs=[DeclaredKiwi(id='k1', host='kiwi.local')])
        deltas = reconcile(env, [])
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].status, 'missing')

    def test_failed_observation_does_not_satisfy_declared(self):
        env = _env(kiwisdrs=[DeclaredKiwi(id='k1', host='kiwi.local')])
        obs = _obs(kind='kiwisdr', id='k1', endpoint='kiwi.local:8073',
                   ok=False, error='connection refused')
        deltas = reconcile(env, [obs])
        self.assertEqual(deltas[0].status, 'missing')

    def test_unknown_extra(self):
        env = _env()
        obs = _obs(kind='kiwisdr', endpoint='rogue.local:8073',
                   source='mdns')
        deltas = reconcile(env, [obs])
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].status, 'unknown-extra')
        self.assertEqual(deltas[0].kind, 'kiwisdr')


class HintTests(unittest.TestCase):
    def test_radiod_gpsdo_lock_hint_triggers_degraded(self):
        env = _env(radiods=[DeclaredRadiod(
            id='r1', host='bee1.local', status_dns='hf-status.local',
            expect={'frontend': {'gpsdo_lock': True}})])
        obs = _obs(kind='radiod', id='r1', endpoint='hf-status.local',
                   fields={'frontend': {'gpsdo_lock': False}})
        deltas = reconcile(env, [obs])
        self.assertEqual(deltas[0].status, 'degraded')
        self.assertIn('gpsdo_lock', deltas[0].detail)

    def test_radiod_hint_passes_when_satisfied(self):
        env = _env(radiods=[DeclaredRadiod(
            id='r1', host='bee1.local', status_dns='hf-status.local',
            expect={'frontend': {'gpsdo_lock': True}})])
        obs = _obs(kind='radiod', id='r1', endpoint='hf-status.local',
                   fields={'frontend': {'gpsdo_lock': True}})
        deltas = reconcile(env, [obs])
        self.assertEqual(deltas[0].status, 'healthy')

    def test_kiwisdr_gps_expected_enforces_fix(self):
        env = _env(kiwisdrs=[DeclaredKiwi(
            id='k1', host='kiwi.local', gps_expected=True)])
        obs = _obs(kind='kiwisdr', id='k1', endpoint='kiwi.local:8073',
                   fields={'gps_fix': False})
        deltas = reconcile(env, [obs])
        self.assertEqual(deltas[0].status, 'degraded')

    def test_time_source_stratum_cap(self):
        env = _env(time_sources=[DeclaredTimeSource(
            id='ntp1', kind='ntp', host='time.nist.gov', stratum_max=2)])
        obs = _obs(kind='time_source', id='ntp1',
                   endpoint='time.nist.gov:123',
                   fields={'stratum': 4})
        deltas = reconcile(env, [obs])
        self.assertEqual(deltas[0].status, 'degraded')
        self.assertIn('stratum 4', deltas[0].detail)

    def test_gpsdo_unlocked_is_degraded(self):
        env = _env(gpsdos=[DeclaredGpsdo(
            id='g1', kind='leo-bodnar-mini', host='localhost')])
        obs = _obs(kind='gpsdo', id='g1', endpoint='/tmp/authority.json',
                   fields={'locked': False})
        deltas = reconcile(env, [obs])
        self.assertEqual(deltas[0].status, 'degraded')


if __name__ == '__main__':
    unittest.main()
