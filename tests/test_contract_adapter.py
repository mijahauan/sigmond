"""Tests for the generic client-contract adapter.

Uses the canonical hf-timestd v7.0.0 inventory/validate JSON captured
under tests/fixtures/ as a frozen reference.  The point is to detect
regressions where `ContractAdapter` stops parsing real client output
as the contract evolves.
"""

import json
import sys
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.clients.contract import (
    ContractAdapter, SUPPORTED_CONTRACT_VERSION, _instance_from_contract,
)


FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ''):
        self.stdout     = stdout
        self.stderr     = stderr
        self.returncode = returncode


class _HFTimestdAdapter(ContractAdapter):
    name   = "hf-timestd"
    binary = "hf-timestd"


class InstanceParserTests(unittest.TestCase):
    """_instance_from_contract should round-trip every v0.2 field."""

    def setUp(self):
        self.raw = _load('hf-timestd-inventory.json')['instances'][0]

    def test_instance_identity(self):
        iv = _instance_from_contract(self.raw)
        self.assertEqual(iv.instance, "default")
        self.assertIsNone(iv.radiod_id)

    def test_frequencies_preserved(self):
        iv = _instance_from_contract(self.raw)
        self.assertEqual(len(iv.frequencies_hz), 9)
        self.assertIn(2500000, iv.frequencies_hz)
        self.assertIn(14670000, iv.frequencies_hz)

    def test_ka9q_channels(self):
        iv = _instance_from_contract(self.raw)
        self.assertEqual(iv.ka9q_channels, 9)

    def test_data_destination_v02(self):
        iv = _instance_from_contract(self.raw)
        self.assertEqual(iv.data_destination, "239.45.120.115")

    def test_timing_roles(self):
        iv = _instance_from_contract(self.raw)
        self.assertFalse(iv.uses_timing_calibration)
        self.assertTrue(iv.provides_timing_calibration)

    def test_disk_writes(self):
        iv = _instance_from_contract(self.raw)
        self.assertEqual(len(iv.disk_writes), 1)
        self.assertEqual(iv.disk_writes[0].path, "/var/lib/timestd")

    def test_radiod_status_dns(self):
        iv = _instance_from_contract(self.raw)
        self.assertEqual(iv.radiod_status_dns, "bee3-status.local")

    def test_chain_delay_absent_stays_none(self):
        """hf-timestd v7.0.0 does not yet publish chain_delay_ns_applied;
        the adapter must leave it as None, not default to 0."""
        iv = _instance_from_contract(self.raw)
        self.assertIsNone(iv.chain_delay_ns_applied)

    def test_chain_delay_populated_when_present(self):
        raw = dict(self.raw)
        raw['chain_delay_ns_applied'] = 4250
        iv = _instance_from_contract(raw)
        self.assertEqual(iv.chain_delay_ns_applied, 4250)


class ReadViewTests(unittest.TestCase):
    """ContractAdapter.read_view against the hf-timestd v7.0.0 fixture."""

    def setUp(self):
        self.inventory_json = (FIXTURES / 'hf-timestd-inventory.json').read_text()
        self.adapter = _HFTimestdAdapter()

    def _run(self, stdout=None, returncode=0, stderr=''):
        stdout = self.inventory_json if stdout is None else stdout
        with mock.patch.object(
            self.adapter, 'find_binary', return_value='/usr/local/bin/hf-timestd'
        ), mock.patch(
            'sigmond.clients.contract.subprocess.run',
            return_value=FakeCompleted(stdout, returncode, stderr),
        ):
            return self.adapter.read_view()

    def test_installed_and_client_type(self):
        view = self._run()
        self.assertTrue(view.installed)
        self.assertEqual(view.client_type, "hf-timestd")

    def test_contract_version_captured(self):
        view = self._run()
        self.assertEqual(view.contract_version, "0.2")

    def test_v02_client_on_v04_sigmond_warns(self):
        """hf-timestd v7.0.0 reports contract_version 0.2.  A v0.4
        sigmond must still parse it successfully but emit a mismatch
        warning — older clients remain operational, not rejected."""
        view = self._run()
        self.assertTrue(view.installed)
        self.assertTrue(
            any('contract_version mismatch' in i for i in view.issues),
            f"expected mismatch issue for v0.2 client, got {view.issues}",
        )

    def test_v03_client_on_v04_sigmond_warns(self):
        raw = json.loads(self.inventory_json)
        raw['contract_version'] = '0.3'
        view = self._run(stdout=json.dumps(raw))
        self.assertEqual(view.contract_version, '0.3')
        self.assertTrue(
            any('contract_version mismatch' in i for i in view.issues),
            f"expected mismatch issue for v0.3 client on v0.4 sigmond, got {view.issues}",
        )

    def test_v04_client_no_mismatch(self):
        raw = json.loads(self.inventory_json)
        raw['contract_version'] = '0.4'
        view = self._run(stdout=json.dumps(raw))
        self.assertEqual(view.contract_version, '0.4')
        self.assertFalse(
            any('contract_version mismatch' in i for i in view.issues),
            f"unexpected mismatch issue for v0.4 client: {view.issues}",
        )

    def test_contract_version_mismatch_raises_issue(self):
        raw = json.loads(self.inventory_json)
        raw['contract_version'] = '0.1'
        view = self._run(stdout=json.dumps(raw))
        self.assertEqual(view.contract_version, '0.1')
        self.assertTrue(
            any('contract_version mismatch' in i for i in view.issues),
            f"expected mismatch issue, got {view.issues}",
        )

    def test_config_path_captured(self):
        view = self._run()
        self.assertEqual(view.config_path, Path('/etc/hf-timestd/timestd-config.toml'))

    def test_instances_parsed(self):
        view = self._run()
        self.assertEqual(len(view.instances), 1)
        inst = view.instances[0]
        self.assertEqual(inst.data_destination, "239.45.120.115")
        self.assertEqual(inst.ka9q_channels, 9)

    def test_binary_missing_reports_issue(self):
        with mock.patch.object(self.adapter, 'find_binary', return_value=None):
            view = self.adapter.read_view()
        self.assertFalse(view.installed)
        self.assertTrue(any('not found on PATH' in i for i in view.issues))

    def test_nonzero_exit_reports_issue(self):
        view = self._run(stdout='', returncode=2, stderr='config missing')
        self.assertFalse(view.installed)
        self.assertTrue(any('exit 2' in i for i in view.issues))

    def test_malformed_json_reports_issue(self):
        view = self._run(stdout='not json at all')
        self.assertFalse(view.installed)
        self.assertTrue(any('malformed JSON' in i for i in view.issues))

    def test_stdout_banner_breaks_parse(self):
        """Stdout-cleanliness guard is a contract requirement.  If a
        client accidentally prints a banner before the JSON body, the
        adapter must surface it as a parse error, not silently succeed.
        This pins the expected failure mode so a future 'helpful'
        adapter change doesn't mask violations."""
        dirty = 'Logging configured\n' + self.inventory_json
        view = self._run(stdout=dirty)
        self.assertFalse(view.installed)
        self.assertTrue(any('malformed JSON' in i for i in view.issues))


class V03FieldTests(unittest.TestCase):
    """Tests for v0.3 additions: log_paths and log_level."""

    def setUp(self):
        self.inventory_json = (FIXTURES / 'hf-timestd-inventory.json').read_text()
        self.adapter = _HFTimestdAdapter()

    def _run(self, stdout):
        with mock.patch.object(
            self.adapter, 'find_binary', return_value='/usr/local/bin/hf-timestd'
        ), mock.patch(
            'sigmond.clients.contract.subprocess.run',
            return_value=FakeCompleted(stdout),
        ):
            return self.adapter.read_view()

    def test_log_paths_captured(self):
        raw = json.loads(self.inventory_json)
        raw['contract_version'] = '0.3'
        raw['log_paths'] = {
            'process': '/var/log/hf-timestd/core-recorder.log',
        }
        view = self._run(json.dumps(raw))
        self.assertEqual(view.log_paths, {
            'process': '/var/log/hf-timestd/core-recorder.log',
        })

    def test_log_paths_absent_stays_none(self):
        view = self._run(self.inventory_json)
        self.assertIsNone(view.log_paths)

    def test_log_level_captured(self):
        raw = json.loads(self.inventory_json)
        raw['contract_version'] = '0.3'
        raw['log_level'] = 'DEBUG'
        view = self._run(json.dumps(raw))
        self.assertEqual(view.log_level, 'DEBUG')

    def test_log_level_absent_stays_none(self):
        view = self._run(self.inventory_json)
        self.assertIsNone(view.log_level)


class ValidateNativeTests(unittest.TestCase):

    def setUp(self):
        self.validate_json = (FIXTURES / 'hf-timestd-validate.json').read_text()
        self.adapter = _HFTimestdAdapter()

    def test_clean_validate_returns_empty(self):
        with mock.patch.object(
            self.adapter, 'find_binary', return_value='/usr/local/bin/hf-timestd'
        ), mock.patch(
            'sigmond.clients.contract.subprocess.run',
            return_value=FakeCompleted(self.validate_json),
        ):
            issues = self.adapter.validate_native()
        self.assertEqual(issues, [])

    def test_validate_issues_pass_through(self):
        payload = json.dumps({
            "ok": False,
            "issues": [
                {"severity": "warn",  "instance": "default", "message": "storage near quota"},
                {"severity": "error", "instance": "default", "message": "missing radiod"},
            ],
        })
        with mock.patch.object(
            self.adapter, 'find_binary', return_value='/usr/local/bin/hf-timestd'
        ), mock.patch(
            'sigmond.clients.contract.subprocess.run',
            return_value=FakeCompleted(payload),
        ):
            issues = self.adapter.validate_native()
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0]['severity'], 'warn')
        self.assertEqual(issues[1]['severity'], 'error')


class ReadQualityTests(unittest.TestCase):
    """`read_quality()` reads the optional `<binary> quality --json`
    surface (CLIENT-CONTRACT.md §17 — first implemented by hf-timestd
    for sigmond's packet-loss diagnostic loop).  Failure modes are all
    silent (return None) — a client that doesn't implement the
    subcommand is not a sigmond-level issue; it just means the
    consumer-quality classifier in Phase 7e gets nothing to classify.
    """

    def setUp(self):
        self.adapter = _HFTimestdAdapter()

    def _patch_run(self, stdout='', returncode=0, side_effect=None):
        target = 'sigmond.clients.contract.subprocess.run'
        if side_effect is not None:
            return mock.patch(target, side_effect=side_effect)
        return mock.patch(target,
                          return_value=FakeCompleted(stdout, returncode))

    def test_binary_not_on_path_returns_none(self):
        with mock.patch.object(self.adapter, 'find_binary',
                               return_value=None):
            self.assertIsNone(self.adapter.read_quality())

    def test_subprocess_timeout_returns_none(self):
        with mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'), \
             self._patch_run(side_effect=__import__('subprocess')
                                          .TimeoutExpired('hf-timestd', 5)):
            self.assertIsNone(self.adapter.read_quality())

    def test_nonzero_exit_returns_none(self):
        # Older client without the 'quality' subcommand: argparse exits
        # non-zero with usage on stderr.
        with mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'), \
             self._patch_run(stdout='', returncode=2):
            self.assertIsNone(self.adapter.read_quality())

    def test_malformed_json_returns_none(self):
        with mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'), \
             self._patch_run(stdout='{not json', returncode=0):
            self.assertIsNone(self.adapter.read_quality())

    def test_non_dict_top_level_returns_none(self):
        # Defensive: a client emitting a JSON list at top level isn't
        # contract-conformant.  Don't pass it through.
        with mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'), \
             self._patch_run(stdout='["not", "a", "dict"]', returncode=0):
            self.assertIsNone(self.adapter.read_quality())

    def test_normal_payload_passes_through(self):
        payload = {
            "schema_version": 1, "client": "hf-timestd",
            "captured_at": 1000.0,
            "recorders": [
                {"description": "WWV_5000",
                 "completeness_pct": 99.97,
                 "packets_lost_total": 7,
                 "packets_lost_rate": 0.02},
            ],
            "summary": {"recorder_count": 1, "total_packets_lost": 7},
            "stale_seconds": 1.5,
        }
        with mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'), \
             self._patch_run(stdout=json.dumps(payload), returncode=0):
            got = self.adapter.read_quality()
        self.assertEqual(got, payload)

    def test_error_payload_still_returned(self):
        # Daemon stopped → CLI exits 0 with error marker.  The adapter
        # passes it through; Phase 7e's caller decides what to do.
        payload = {"client": "hf-timestd",
                   "error": "snapshot_missing",
                   "snapshot_path": "/run/hf-timestd/quality.json"}
        with mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'), \
             self._patch_run(stdout=json.dumps(payload), returncode=0):
            got = self.adapter.read_quality()
        self.assertEqual(got["error"], "snapshot_missing")


class QualityIntegrationTests(unittest.TestCase):
    """Phase 7e wiring: read_view populates ClientView.quality, and
    validate_native emits a stale-snapshot warning when the daemon's
    snapshot writer has stalled."""

    def setUp(self):
        self.adapter = _HFTimestdAdapter()
        self.inv_json = (FIXTURES / 'hf-timestd-inventory.json').read_text()

    def _patch(self, *, inventory_payload: str, quality_payload: Optional[str],
               quality_returncode: int = 0):
        """Patch subprocess.run to dispatch on the requested subcommand
        — `inventory --json` returns one fixture, `quality --json`
        another (or simulates an unsupported subcommand)."""
        def fake_run(args, **_kw):
            if 'quality' in args:
                if quality_payload is None:
                    return FakeCompleted('', returncode=2)
                return FakeCompleted(quality_payload,
                                     returncode=quality_returncode)
            return FakeCompleted(inventory_payload, returncode=0)

        return mock.patch('sigmond.clients.contract.subprocess.run',
                          side_effect=fake_run), \
               mock.patch.object(self.adapter, 'find_binary',
                                 return_value='/usr/bin/hf-timestd')

    def test_read_view_populates_quality(self):
        q_payload = json.dumps({
            "client": "hf-timestd", "stale_seconds": 0.5,
            "summary": {"recorder_count": 9},
            "recorders": [],
        })
        run_patch, bin_patch = self._patch(inventory_payload=self.inv_json,
                                           quality_payload=q_payload)
        with run_patch, bin_patch:
            view = self.adapter.read_view()
        self.assertIsNotNone(view.quality)
        self.assertEqual(view.quality["stale_seconds"], 0.5)
        self.assertEqual(view.quality["summary"]["recorder_count"], 9)

    def test_read_view_quality_none_when_subcommand_unsupported(self):
        run_patch, bin_patch = self._patch(inventory_payload=self.inv_json,
                                           quality_payload=None)
        with run_patch, bin_patch:
            view = self.adapter.read_view()
        self.assertIsNone(view.quality)

    def test_validate_native_warns_on_stale_snapshot(self):
        q_payload = json.dumps({
            "client": "hf-timestd", "stale_seconds": 90.0,  # > 30s
            "recorders": [],
        })
        # validate --json returns no native issues; the staleness
        # warning is the only thing the adapter emits.
        validate_payload = json.dumps({"ok": True, "issues": []})

        def fake_run(args, **_kw):
            if 'quality' in args:
                return FakeCompleted(q_payload, returncode=0)
            return FakeCompleted(validate_payload, returncode=0)

        with mock.patch('sigmond.clients.contract.subprocess.run',
                        side_effect=fake_run), \
             mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'):
            issues = self.adapter.validate_native()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['severity'], 'warn')
        self.assertIn('stale', issues[0]['message'])
        self.assertIn('90.0s', issues[0]['message'])

    def test_validate_native_silent_when_snapshot_fresh(self):
        q_payload = json.dumps({
            "client": "hf-timestd", "stale_seconds": 0.5,
            "recorders": [],
        })
        validate_payload = json.dumps({"ok": True, "issues": []})

        def fake_run(args, **_kw):
            if 'quality' in args:
                return FakeCompleted(q_payload, returncode=0)
            return FakeCompleted(validate_payload, returncode=0)

        with mock.patch('sigmond.clients.contract.subprocess.run',
                        side_effect=fake_run), \
             mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'):
            issues = self.adapter.validate_native()
        self.assertEqual(issues, [])

    def test_validate_native_silent_when_quality_unsupported(self):
        # A client without the quality subcommand: no stale-warning
        # ever, regardless of native validate state.
        validate_payload = json.dumps({"ok": True, "issues": []})

        def fake_run(args, **_kw):
            if 'quality' in args:
                return FakeCompleted('', returncode=2)  # unsupported
            return FakeCompleted(validate_payload, returncode=0)

        with mock.patch('sigmond.clients.contract.subprocess.run',
                        side_effect=fake_run), \
             mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'):
            issues = self.adapter.validate_native()
        self.assertEqual(issues, [])

    def test_validate_native_passes_through_native_issues(self):
        # Existing native validate issues survive the stale-snapshot
        # extension untouched.
        q_payload = json.dumps({
            "client": "hf-timestd", "stale_seconds": 1.0,
            "recorders": [],
        })
        validate_payload = json.dumps({
            "ok": False,
            "issues": [{"severity": "warn", "instance": "default",
                        "message": "storage near quota"}],
        })

        def fake_run(args, **_kw):
            if 'quality' in args:
                return FakeCompleted(q_payload, returncode=0)
            return FakeCompleted(validate_payload, returncode=0)

        with mock.patch('sigmond.clients.contract.subprocess.run',
                        side_effect=fake_run), \
             mock.patch.object(self.adapter, 'find_binary',
                               return_value='/usr/bin/hf-timestd'):
            issues = self.adapter.validate_native()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['message'], 'storage near quota')


if __name__ == '__main__':
    unittest.main()
