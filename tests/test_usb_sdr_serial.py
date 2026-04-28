"""Tests for usb_sdr probe — VID/PID detection and iSerial extraction."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.discovery import usb_sdr
from sigmond.environment import Environment


# Sample `lsusb` output: a Cypress FX3 (RX888 mode), an Airspy HF+, an
# unrelated webcam, and a hub.  All real values from a HamSCI test rig.
_LSUSB_PLAIN = """\
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 003 Device 005: ID 04b4:00bc Cypress Semiconductor Corp. FX3
Bus 003 Device 008: ID 03eb:800c Airspy HF+
Bus 003 Device 002: ID 046d:c52b Logitech, Inc. Unifying Receiver
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
"""

_LSUSB_VERBOSE = """\
Bus 003 Device 005: ID 04b4:00bc Cypress Semiconductor Corp. FX3
Device Descriptor:
  bLength                18
  idVendor           0x04b4 Cypress Semiconductor Corp.
  idProduct          0x00bc
  iManufacturer           1 Cypress
  iProduct                2 FX3
  iSerial                 3 0123456789ABCDEF

Bus 003 Device 008: ID 03eb:800c Airspy HF+
Device Descriptor:
  bLength                18
  idVendor           0x03eb
  idProduct          0x800c
  iSerial                 3 62CC68FF21146A17

Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Device Descriptor:
  iSerial                 3 SHOULD_NOT_LEAK
"""


class _Runner:
    """Stand-in for `lsusb`: returns plain or verbose output on demand."""

    def __init__(self, plain: str, verbose: str):
        self.plain = plain
        self.verbose = verbose
        self.calls: list[bool] = []

    def __call__(self, verbose: bool = False) -> str:
        self.calls.append(verbose)
        return self.verbose if verbose else self.plain


class ProbeTests(unittest.TestCase):
    def test_only_known_sdr_vid_pids_yield_observations(self):
        runner = _Runner(_LSUSB_PLAIN, _LSUSB_VERBOSE)
        obs = usb_sdr.probe(Environment(), lsusb_runner=runner,
                            extract_serial=False)
        kinds = sorted(o.fields["sdr_type"] for o in obs)
        self.assertEqual(kinds, ["Airspy HF+", "RX-888"])
        # Hubs and the webcam must not appear.
        for o in obs:
            self.assertNotIn("Logitech", o.fields.get("usb_name", ""))
            self.assertNotIn("hub", o.fields.get("usb_name", "").lower())
        # extract_serial=False means lsusb -v is never called.
        self.assertNotIn(True, runner.calls)

    def test_extract_serial_populates_field(self):
        runner = _Runner(_LSUSB_PLAIN, _LSUSB_VERBOSE)
        obs = usb_sdr.probe(Environment(), lsusb_runner=runner,
                            extract_serial=True)
        # Now lsusb -v must have been invoked exactly once.
        self.assertEqual(runner.calls.count(True), 1)
        by_type = {o.fields["sdr_type"]: o.fields for o in obs}
        self.assertEqual(by_type["RX-888"]["serial"],
                         "0123456789ABCDEF")
        self.assertEqual(by_type["Airspy HF+"]["serial"],
                         "62CC68FF21146A17")

    def test_serial_not_leaked_from_unrelated_devices(self):
        runner = _Runner(_LSUSB_PLAIN, _LSUSB_VERBOSE)
        obs = usb_sdr.probe(Environment(), lsusb_runner=runner,
                            extract_serial=True)
        for o in obs:
            self.assertNotEqual(o.fields["serial"], "SHOULD_NOT_LEAK")

    def test_missing_iserial_returns_empty_string(self):
        plain = (
            "Bus 003 Device 005: ID 04b4:00bc Cypress Semiconductor Corp.\n"
        )
        verbose_no_serial = (
            "Bus 003 Device 005: ID 04b4:00bc Cypress Semiconductor Corp.\n"
            "Device Descriptor:\n"
            "  iSerial                 0 \n"
        )
        runner = _Runner(plain, verbose_no_serial)
        obs = usb_sdr.probe(Environment(), lsusb_runner=runner,
                            extract_serial=True)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].fields["serial"], "")

    def test_lsusb_failure_returns_error_observation(self):
        def _broken(verbose=False):
            raise RuntimeError("lsusb exploded")
        obs = usb_sdr.probe(Environment(), lsusb_runner=_broken,
                            extract_serial=True)
        self.assertEqual(len(obs), 1)
        self.assertFalse(obs[0].ok)
        self.assertIn("lsusb", obs[0].error)

    def test_verbose_runner_failure_does_not_break_plain_path(self):
        # If `lsusb -v` errors out, the plain detection still works and
        # the serial field comes back empty.
        class _PartialRunner:
            def __init__(self): self.calls = 0
            def __call__(self, verbose: bool = False) -> str:
                self.calls += 1
                if verbose:
                    raise RuntimeError("permission denied")
                return _LSUSB_PLAIN
        r = _PartialRunner()
        obs = usb_sdr.probe(Environment(), lsusb_runner=r,
                            extract_serial=True)
        self.assertEqual(len(obs), 2)
        for o in obs:
            self.assertEqual(o.fields["serial"], "")


if __name__ == "__main__":
    unittest.main()
