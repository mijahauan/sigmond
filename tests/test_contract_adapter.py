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

    def test_v02_client_on_v03_sigmond_warns(self):
        """hf-timestd v7.0.0 reports contract_version 0.2.  A v0.3
        sigmond must still parse it successfully but emit a mismatch
        warning — v0.2 clients remain operational, not rejected."""
        view = self._run()
        self.assertTrue(view.installed)
        self.assertTrue(
            any('contract_version mismatch' in i for i in view.issues),
            f"expected mismatch issue for v0.2 client, got {view.issues}",
        )

    def test_v03_client_no_mismatch(self):
        raw = json.loads(self.inventory_json)
        raw['contract_version'] = '0.3'
        view = self._run(stdout=json.dumps(raw))
        self.assertEqual(view.contract_version, '0.3')
        self.assertFalse(
            any('contract_version mismatch' in i for i in view.issues),
            f"unexpected mismatch issue for v0.3 client: {view.issues}",
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


if __name__ == '__main__':
    unittest.main()
