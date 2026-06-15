"""Tests for sigmond.hardware — the CONTRACT §3 / Phase D readiness probe.

Hermetic: the client `inventory --json` call and the lsusb fallbacks are
monkeypatched so nothing touches the host bus or PATH.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond import hardware


class TestHardwareReady(unittest.TestCase):

    def _patch_inventory(self, value):
        orig = hardware.inventory_hardware_present
        hardware.inventory_hardware_present = lambda client: value
        self.addCleanup(lambda: setattr(
            hardware, 'inventory_hardware_present', orig))

    def _patch_legacy(self, mapping):
        orig = hardware._LEGACY_PROBES
        hardware._LEGACY_PROBES = mapping
        self.addCleanup(lambda: setattr(hardware, '_LEGACY_PROBES', orig))

    def test_inventory_is_authoritative_over_fallback(self):
        # Self-describe says False even though the lsusb fallback says True.
        self._patch_inventory(False)
        self._patch_legacy({'mag-recorder': lambda: True})
        self.assertIs(hardware.hardware_ready('mag-recorder'), False)

    def test_inventory_true_short_circuits(self):
        self._patch_inventory(True)
        self._patch_legacy({'mag-recorder': lambda: False})
        self.assertIs(hardware.hardware_ready('mag-recorder'), True)

    def test_falls_back_when_inventory_silent(self):
        # Client doesn't report the field -> lsusb fallback decides.
        self._patch_inventory(None)
        self._patch_legacy({'mag-recorder': lambda: True})
        self.assertIs(hardware.hardware_ready('mag-recorder'), True)

    def test_none_when_no_inventory_and_no_fallback(self):
        self._patch_inventory(None)
        self._patch_legacy({})
        self.assertIsNone(hardware.hardware_ready('whatever'))


class TestInventoryHardwarePresent(unittest.TestCase):

    def _patch(self, *, exe, run_result):
        import shutil
        import subprocess
        orig_which, orig_run = shutil.which, subprocess.run
        shutil.which = lambda name: exe
        subprocess.run = lambda *a, **k: run_result
        self.addCleanup(lambda: setattr(shutil, 'which', orig_which))
        self.addCleanup(lambda: setattr(subprocess, 'run', orig_run))

    class _R:
        def __init__(self, returncode, stdout):
            self.returncode, self.stdout = returncode, stdout

    def test_none_when_client_not_on_path(self):
        self._patch(exe=None, run_result=self._R(0, '{}'))
        self.assertIsNone(hardware.inventory_hardware_present('x'))

    def test_reads_bool_field(self):
        self._patch(exe='/usr/bin/x',
                    run_result=self._R(0, '{"hardware_present": true}'))
        self.assertIs(hardware.inventory_hardware_present('x'), True)

    def test_none_when_field_absent(self):
        self._patch(exe='/usr/bin/x', run_result=self._R(0, '{"client": "x"}'))
        self.assertIsNone(hardware.inventory_hardware_present('x'))

    def test_none_on_nonzero_exit(self):
        self._patch(exe='/usr/bin/x', run_result=self._R(1, ''))
        self.assertIsNone(hardware.inventory_hardware_present('x'))

    def test_none_on_unparseable_json(self):
        self._patch(exe='/usr/bin/x',
                    run_result=self._R(0, 'Logging configured\n{bad'))
        self.assertIsNone(hardware.inventory_hardware_present('x'))


if __name__ == '__main__':
    unittest.main()
