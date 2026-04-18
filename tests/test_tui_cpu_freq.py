"""Tests for the CPU frequency TUI screen."""

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
class FreqDataGatheringTests(unittest.TestCase):
    def test_gather_returns_shape(self):
        from sigmond.tui.screens.cpu_freq import _FreqData, _gather_freq_data

        d = _gather_freq_data()
        self.assertIsInstance(d, _FreqData)
        self.assertIsInstance(d.rows, list)
        # Every row's cpu index falls inside the logical CPU count.
        import os
        cpu_count = os.cpu_count() or 1
        for row in d.rows:
            self.assertLess(row.cpu, cpu_count)
            self.assertIn(row.role, ('radiod', 'other'))

    def test_policy_defaults_from_topology(self):
        from sigmond.tui.screens.cpu_freq import _gather_freq_data
        from sigmond.topology import load_topology

        d = _gather_freq_data()
        topo = load_topology()
        self.assertEqual(d.radiod_max_mhz,
                         int(topo.cpu_freq.get('radiod_max_mhz', 3200)))
        self.assertEqual(d.other_max_mhz,
                         int(topo.cpu_freq.get('other_max_mhz', 1400)))


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class ScreenMountTests(unittest.IsolatedAsyncioTestCase):
    async def test_cpu_freq_mounts_via_tree(self):
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.cpu_freq import CPUFreqScreen

        app = SigmondApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.action_show_cpu_freq()
            for _ in range(3):
                await pilot.pause()
            center = app.query_one("#center")
            self.assertTrue(
                any(isinstance(c, CPUFreqScreen) for c in center.children),
                "CPUFreqScreen did not mount",
            )


if __name__ == '__main__':
    unittest.main()
