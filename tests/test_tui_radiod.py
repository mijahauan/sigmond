"""Tests for the radiod TUI screen's gpsdo-TUI launcher helper.

We don't drive the full Textual screen — we just exercise the small
helper that maps a radiod_id to the serial of whichever gpsdo-monitor
report declares `governs = ["radiod:<id>"]`, so the "Deep dive (gpsdo
tui)" button can pass `--serial` to the client's TUI.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

try:
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class GovernorSerialLookupTests(unittest.TestCase):
    def setUp(self):
        from sigmond.tui.screens import radiod
        self._module = radiod
        self._tmp = Path(tempfile.mkdtemp())
        self._original = radiod.GPSDO_RUN_DIR
        radiod.GPSDO_RUN_DIR = self._tmp
        # One radiod screen bound to "main"; _governor_serial_for_radiod
        # only depends on _radiod_id, so we can use __new__ to bypass
        # Textual's widget __init__ machinery.
        self._screen = radiod.RadiodScreen.__new__(radiod.RadiodScreen)
        self._screen._radiod_id = "main"

    def tearDown(self):
        self._module.GPSDO_RUN_DIR = self._original
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, name: str, payload: dict) -> None:
        (self._tmp / f"{name}.json").write_text(json.dumps(payload))

    def _good(self, serial: str, governs: list) -> dict:
        return {
            "schema": "v1",
            "device": {"model": "lbe-1421", "serial": serial},
            "governs": governs,
        }

    def test_missing_run_dir_returns_none(self):
        self._module.GPSDO_RUN_DIR = self._tmp / "does-not-exist"
        self.assertIsNone(self._screen._governor_serial_for_radiod())

    def test_empty_dir_returns_none(self):
        self.assertIsNone(self._screen._governor_serial_for_radiod())

    def test_matches_prefixed_token(self):
        self._write("LBE-A", self._good("LBE-A", ["radiod:main"]))
        self.assertEqual(self._screen._governor_serial_for_radiod(), "LBE-A")

    def test_matches_bare_radiod_id_fallback(self):
        # Some older tools may write the id without the prefix; accept
        # both rather than force a format war with the operator.
        self._write("LBE-A", self._good("LBE-A", ["main"]))
        self.assertEqual(self._screen._governor_serial_for_radiod(), "LBE-A")

    def test_no_match_returns_none(self):
        self._write("LBE-A", self._good("LBE-A", ["radiod:aux"]))
        self.assertIsNone(self._screen._governor_serial_for_radiod())

    def test_index_json_is_ignored(self):
        (self._tmp / "index.json").write_text(json.dumps(
            self._good("LBE-INDEX", ["radiod:main"])
        ))
        self.assertIsNone(self._screen._governor_serial_for_radiod())

    def test_wrong_schema_ignored(self):
        payload = self._good("LBE-A", ["radiod:main"])
        payload["schema"] = "v2"
        self._write("LBE-A", payload)
        self.assertIsNone(self._screen._governor_serial_for_radiod())

    def test_malformed_json_tolerated(self):
        (self._tmp / "bad.json").write_text("{ not json")
        self._write("good", self._good("GOOD", ["radiod:main"]))
        # The bad file is skipped; the good one still returns its serial.
        self.assertEqual(self._screen._governor_serial_for_radiod(), "GOOD")

    def test_first_match_wins_when_multiple(self):
        # glob(...) is sorted, so the lexicographically first filename wins.
        self._write("B", self._good("B", ["radiod:main"]))
        self._write("A", self._good("A", ["radiod:main"]))
        self.assertEqual(self._screen._governor_serial_for_radiod(), "A")


if __name__ == "__main__":
    unittest.main()
