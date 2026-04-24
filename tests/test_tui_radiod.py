"""Tests for the radiod TUI screen.

The old 'Deep dive (gpsdo tui)' button and its ``_governor_serial_for_radiod``
helper lived here until GPSDO live became its own screen — those tests
have been removed alongside the dead code.  This file is kept so the
import path ``tests.test_tui_radiod`` remains stable and to host
future radiod-screen tests.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

try:
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class RadiodScreenImportTests(unittest.TestCase):
    def test_screen_imports_cleanly(self):
        from sigmond.tui.screens.radiod import RadiodScreen  # noqa: F401


if __name__ == "__main__":
    unittest.main()
