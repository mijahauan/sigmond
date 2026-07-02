"""Tests for sigmond.readiness — the golden-image / site gate."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sigmond import readiness
from sigmond.readiness import (
    CheckResult, GateReport,
    capture_cleanliness_checks, detect_gate, run_gate, site_checks,
    structural_checks, _probe_version,
)


class _FakeCoord:
    def __init__(self, call='', grid=''):
        class _H:
            pass
        self.host = _H()
        self.host.call = call
        self.host.grid = grid


class _FakeRule:
    def __init__(self, rule, severity):
        self.rule = rule
        self.severity = severity


# ---- GateReport -----------------------------------------------------------


class TestGateReport(unittest.TestCase):

    def test_ready_iff_no_fail(self):
        rep = GateReport(gate='capture', profile='dasi2', results=[
            CheckResult('a', 'pass'),
            CheckResult('b', 'warn'),
            CheckResult('c', 'skip'),
        ])
        self.assertTrue(rep.ready)
        rep.results.append(CheckResult('d', 'fail'))
        self.assertFalse(rep.ready)

    def test_as_dict_shape(self):
        rep = GateReport(gate='site', profile='dasi2', results=[
            CheckResult('a', 'pass', 'ok', component='psk-recorder'),
        ])
        d = rep.as_dict()
        self.assertEqual(d['gate'], 'site')
        self.assertTrue(d['ready'])
        self.assertEqual(d['results'][0]['component'], 'psk-recorder')
        json.dumps(d)  # serializable


# ---- version probe --------------------------------------------------------


class TestVersionProbe(unittest.TestCase):

    def test_good_json_passes(self):
        class R:
            returncode = 0
            stdout = '{"version": "1.2.3"}'
            stderr = ''
        ok, detail = _probe_version('x', runner=lambda *a, **k: R())
        self.assertTrue(ok)
        self.assertIn('1.2.3', detail)

    def test_nonzero_exit_fails(self):
        class R:
            returncode = 1
            stdout = ''
            stderr = 'ModuleNotFoundError: ka9q'
        ok, detail = _probe_version('x', runner=lambda *a, **k: R())
        self.assertFalse(ok)
        self.assertIn('ModuleNotFoundError', detail)

    def test_non_json_fails(self):
        class R:
            returncode = 0
            stdout = 'not json'
            stderr = ''
        ok, detail = _probe_version('x', runner=lambda *a, **k: R())
        self.assertFalse(ok)

    def test_oserror_fails(self):
        def boom(*a, **k):
            raise OSError('no such binary')
        ok, detail = _probe_version('x', runner=boom)
        self.assertFalse(ok)


# ---- structural checks ----------------------------------------------------


class TestStructuralChecks(unittest.TestCase):

    def _fake_profile(self, names):
        return patch.object(readiness, 'profile_components',
                            lambda p, with_optional=False: list(names))

    def test_unknown_profile_fails(self):
        def raise_key(p, with_optional=False):
            raise KeyError(p)
        with patch.object(readiness, 'profile_components', raise_key):
            results = structural_checks('nope')
        self.assertEqual(results[0].status, 'fail')

    def test_missing_repo_fails_component(self):
        with TemporaryDirectory() as td:
            with self._fake_profile(['ghost-client']):
                results = structural_checks(
                    'dasi2', suite_root=Path(td),
                    unit_dir=Path(td),
                    probe=lambda b: (True, 'v'))
        by = {(r.component, r.name): r for r in results}
        self.assertEqual(by[('ghost-client', 'installed')].status, 'fail')

    def test_full_pass_for_healthy_client(self):
        with TemporaryDirectory() as td:
            root = Path(td) / 'suite'
            units = Path(td) / 'units'
            (root / 'good-client').mkdir(parents=True)
            units.mkdir()
            venv_bin = root / 'good-client' / 'venv' / 'bin'
            venv_bin.mkdir(parents=True)
            (venv_bin / 'good-client').write_text('#!/bin/sh\n')
            (units / 'good-client@.service').write_text('[Unit]\n')
            deploy = {
                'build': {'produces': [str(venv_bin / 'good-client')]},
                'systemd': {'units': ['good-client@.service']},
            }
            fake_entry = type('E', (), {'kind': 'client'})()
            with self._fake_profile(['good-client']), \
                 patch.object(readiness, '_load_deploy',
                              lambda name: deploy), \
                 patch('sigmond.catalog.load_catalog',
                       lambda: {'good-client': fake_entry}), \
                 patch('sigmond.catalog.find_client_binary',
                       lambda name: '/usr/local/bin/good-client'), \
                 patch('shutil.which', lambda b: '/usr/local/sbin/radiod'):
                results = structural_checks(
                    'dasi2', suite_root=root, unit_dir=units,
                    probe=lambda b: (True, 'version 1.0'))
        statuses = {(r.component, r.name): r.status for r in results}
        self.assertEqual(statuses[('good-client', 'installed')], 'pass')
        self.assertEqual(statuses[('good-client', 'built')], 'pass')
        self.assertEqual(statuses[('good-client', 'imports')], 'pass')
        self.assertEqual(statuses[('good-client', 'unit')], 'pass')
        self.assertEqual(statuses[(None, 'radiod')], 'pass')

    def test_missing_unit_and_artefact_fail(self):
        with TemporaryDirectory() as td:
            root = Path(td) / 'suite'
            units = Path(td) / 'units'
            (root / 'bad-client').mkdir(parents=True)
            units.mkdir()
            deploy = {
                'build': {'produces': [str(root / 'bad-client/venv/bin/x')]},
                'systemd': {'units': ['bad-client@.service']},
            }
            fake_entry = type('E', (), {'kind': 'client'})()
            with self._fake_profile(['bad-client']), \
                 patch.object(readiness, '_load_deploy',
                              lambda name: deploy), \
                 patch('sigmond.catalog.load_catalog',
                       lambda: {'bad-client': fake_entry}), \
                 patch('sigmond.catalog.find_client_binary',
                       lambda name: None):
                results = structural_checks(
                    'dasi2', suite_root=root, unit_dir=units,
                    probe=lambda b: (True, 'v'))
        statuses = {(r.component, r.name): r.status for r in results}
        self.assertEqual(statuses[('bad-client', 'built')], 'fail')
        self.assertEqual(statuses[('bad-client', 'imports')], 'fail')
        self.assertEqual(statuses[('bad-client', 'unit')], 'fail')

    def test_infra_kind_skips_probe(self):
        with TemporaryDirectory() as td:
            root = Path(td) / 'suite'
            units = Path(td) / 'units'
            (root / 'igmp-querier').mkdir(parents=True)
            units.mkdir()
            fake_entry = type('E', (), {'kind': 'infra'})()
            with self._fake_profile(['igmp-querier']), \
                 patch.object(readiness, '_load_deploy', lambda name: None), \
                 patch('sigmond.catalog.load_catalog',
                       lambda: {'igmp-querier': fake_entry}), \
                 patch('sigmond.catalog.find_client_binary',
                       lambda name: None):
                results = structural_checks(
                    'dasi2', suite_root=root, unit_dir=units,
                    probe=lambda b: (False, 'should not be called'))
        statuses = {(r.component, r.name): r.status for r in results}
        self.assertEqual(statuses[('igmp-querier', 'imports')], 'skip')


# ---- capture cleanliness --------------------------------------------------


class TestCaptureCleanliness(unittest.TestCase):

    def test_clean_image_passes(self):
        with TemporaryDirectory() as td:
            results = capture_cleanliness_checks(
                sentinel=Path(td) / '.personalized',
                forbidden=((Path(td) / 'frpc.toml', 'rac creds'),),
                coordination_loader=lambda: _FakeCoord('', ''))
        self.assertTrue(all(r.status == 'pass' for r in results))

    def test_baked_identity_fails(self):
        with TemporaryDirectory() as td:
            results = capture_cleanliness_checks(
                sentinel=Path(td) / '.personalized',
                forbidden=(),
                coordination_loader=lambda: _FakeCoord('AC0G', 'EM38ww'))
        by = {r.name: r for r in results}
        self.assertEqual(by['clean:identity'].status, 'fail')
        self.assertIn('AC0G', by['clean:identity'].detail)

    def test_baked_secret_fails(self):
        with TemporaryDirectory() as td:
            secret = Path(td) / 'frpc.toml'
            secret.write_text('token = "x"\n')
            results = capture_cleanliness_checks(
                sentinel=Path(td) / '.personalized',
                forbidden=((secret, 'rac creds'),),
                coordination_loader=lambda: _FakeCoord())
        by = {r.detail: r for r in results if r.name == 'clean:secrets'}
        self.assertTrue(any(r.status == 'fail' for r in by.values()))

    def test_personalized_sentinel_fails(self):
        with TemporaryDirectory() as td:
            sentinel = Path(td) / '.personalized'
            sentinel.write_text('personalized_at=x\n')
            results = capture_cleanliness_checks(
                sentinel=sentinel, forbidden=(),
                coordination_loader=lambda: _FakeCoord())
        by = {r.name: r for r in results}
        self.assertEqual(by['clean:personalized'].status, 'fail')


# ---- site checks ----------------------------------------------------------


class TestSiteChecks(unittest.TestCase):

    def test_configured_site_passes(self):
        with TemporaryDirectory() as td:
            sentinel = Path(td) / '.personalized'
            sentinel.write_text('personalized_at=x\n')
            results = site_checks(
                sentinel=sentinel,
                coordination_loader=lambda: _FakeCoord('AC0G', 'EM38ww'),
                harmonize_runner=lambda: [_FakeRule('r1', 'pass')])
        self.assertTrue(all(r.status == 'pass' for r in results))

    def test_missing_identity_fails(self):
        with TemporaryDirectory() as td:
            results = site_checks(
                sentinel=Path(td) / '.personalized',
                coordination_loader=lambda: _FakeCoord('', ''),
                harmonize_runner=lambda: [])
        by = {r.name: r for r in results}
        self.assertEqual(by['site:identity'].status, 'fail')
        # No sentinel on a hand-built box is a warning, not a failure.
        self.assertEqual(by['site:personalized'].status, 'warn')

    def test_harmonize_fail_fails_gate(self):
        with TemporaryDirectory() as td:
            results = site_checks(
                sentinel=Path(td) / '.personalized',
                coordination_loader=lambda: _FakeCoord('AC0G', 'EM38ww'),
                harmonize_runner=lambda: [_FakeRule('bad', 'fail'),
                                          _FakeRule('meh', 'warn')])
        by = {r.name: r for r in results}
        self.assertEqual(by['site:validate'].status, 'fail')
        self.assertIn('bad', by['site:validate'].detail)

    def test_harmonize_warn_only_warns(self):
        with TemporaryDirectory() as td:
            results = site_checks(
                sentinel=Path(td) / '.personalized',
                coordination_loader=lambda: _FakeCoord('AC0G', 'EM38ww'),
                harmonize_runner=lambda: [_FakeRule('meh', 'warn')])
        by = {r.name: r for r in results}
        self.assertEqual(by['site:validate'].status, 'warn')


# ---- gate detection + orchestration ---------------------------------------


class TestGateDetectAndRun(unittest.TestCase):

    def test_detect_site_when_identity(self):
        with TemporaryDirectory() as td:
            gate = detect_gate(
                sentinel=Path(td) / '.personalized',
                coordination_loader=lambda: _FakeCoord('AC0G', ''))
        self.assertEqual(gate, 'site')

    def test_detect_site_when_sentinel(self):
        with TemporaryDirectory() as td:
            sentinel = Path(td) / '.personalized'
            sentinel.write_text('x\n')
            gate = detect_gate(
                sentinel=sentinel,
                coordination_loader=lambda: _FakeCoord('', ''))
        self.assertEqual(gate, 'site')

    def test_detect_capture_when_bare(self):
        with TemporaryDirectory() as td:
            gate = detect_gate(
                sentinel=Path(td) / '.personalized',
                coordination_loader=lambda: _FakeCoord('', ''))
        self.assertEqual(gate, 'capture')

    def test_run_gate_composes(self):
        rep = run_gate(
            'capture', profile='dasi2',
            structural=lambda p, with_optional=False: [
                CheckResult('installed', 'pass', component='x')],
            capture=lambda: [CheckResult('clean:identity', 'pass')],
            site=lambda: [CheckResult('site:identity', 'fail')])
        self.assertEqual(rep.gate, 'capture')
        self.assertTrue(rep.ready)
        names = {r.name for r in rep.results}
        self.assertIn('clean:identity', names)
        self.assertNotIn('site:identity', names)

    def test_run_gate_auto_uses_detect(self):
        rep = run_gate(
            'auto', profile='dasi2',
            structural=lambda p, with_optional=False: [],
            capture=lambda: [CheckResult('clean:identity', 'pass')],
            site=lambda: [CheckResult('site:identity', 'pass')],
            detect=lambda: 'site')
        self.assertEqual(rep.gate, 'site')
        self.assertEqual(rep.results[0].name, 'site:identity')


if __name__ == '__main__':
    unittest.main()
