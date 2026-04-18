"""Tests for the CPU affinity TUI screen — pure helpers + headless mount.

The mount test uses Textual's Pilot to verify the screen builds without
raising against the live host's AffinityReport.  This catches template
errors and wiring issues that a pure-function test can't see.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

# Skip the whole module if Textual is unavailable — core sigmond stays
# stdlib-only; TUI is an optional dep.
try:
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class FormatCpuListTests(unittest.TestCase):
    def setUp(self):
        from sigmond.tui.screens.cpu_affinity import _format_cpu_list
        self._format = _format_cpu_list

    def test_empty(self):
        self.assertEqual(self._format(set()), "(none)")

    def test_single(self):
        self.assertEqual(self._format({3}), "3")

    def test_contiguous_range(self):
        self.assertEqual(self._format({0, 1, 2, 3}), "0-3")

    def test_split_ranges(self):
        self.assertEqual(self._format({0, 1, 2, 3, 8, 9, 10}), "0-3, 8-10")

    def test_singletons_and_range(self):
        self.assertEqual(self._format({0, 2, 4, 5, 6}), "0, 2, 4-6")


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class L3IslandLookupTests(unittest.TestCase):
    def setUp(self):
        from sigmond.cpu import CacheIsland
        from sigmond.tui.screens.cpu_affinity import _l3_island_for_core
        self._lookup = _l3_island_for_core
        self._islands = [
            CacheIsland(level=3, cache_type='Unified',
                        cpus=frozenset({0, 1, 2, 3, 4, 5, 6, 7})),
            CacheIsland(level=3, cache_type='Unified',
                        cpus=frozenset({8, 9, 10, 11, 12, 13, 14, 15})),
        ]

    def test_core_in_first_island(self):
        isle = self._lookup({0, 1}, self._islands)
        self.assertIsNotNone(isle)
        self.assertIn(0, isle.cpus)

    def test_core_in_second_island(self):
        isle = self._lookup({12, 13}, self._islands)
        self.assertIsNotNone(isle)
        self.assertIn(12, isle.cpus)

    def test_core_straddling_islands(self):
        # A core whose siblings span both L3 islands — shouldn't match either.
        isle = self._lookup({7, 8}, self._islands)
        self.assertIsNone(isle)

    def test_no_islands(self):
        self.assertIsNone(self._lookup({0, 1}, []))


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class GovernorSummaryTests(unittest.TestCase):
    def setUp(self):
        from sigmond.tui.screens.cpu_affinity import _governor_summary
        self._summary = _governor_summary

    def test_empty(self):
        self.assertIn("no cpufreq", self._summary({}))

    def test_uniform(self):
        govs = {i: 'schedutil' for i in range(16)}
        self.assertEqual(self._summary(govs), "schedutil (16/16)")

    def test_mixed(self):
        govs = {i: ('performance' if i < 2 else 'powersave') for i in range(16)}
        out = self._summary(govs)
        self.assertIn("performance (2/16)", out)
        self.assertIn("powersave (14/16)", out)


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class ScreenMountTests(unittest.IsolatedAsyncioTestCase):
    """Mount the screen under Pilot and verify no exceptions during render.

    This exercises _render_hardware, _render_coremap, _render_plan,
    _render_observed, _render_contention, and _render_warnings against
    the current host's real AffinityReport.
    """

    async def test_mounts_without_error(self):
        from sigmond.tui.app import SigmondApp

        app = SigmondApp()
        async with app.run_test() as pilot:
            # Default screen on mount is Topology; switch to CPU affinity.
            await pilot.press("c")
            await pilot.pause()
            # If we got here without raising, the screen's on_mount and
            # all _render_* methods ran successfully.
            center = app.query_one("#center")
            # The screen root should be a CPUAffinityScreen instance.
            from sigmond.tui.screens.cpu_affinity import CPUAffinityScreen
            self.assertTrue(
                any(isinstance(child, CPUAffinityScreen)
                    for child in center.children),
                f"CPUAffinityScreen not mounted; children={list(center.children)}",
            )


if __name__ == '__main__':
    unittest.main()
