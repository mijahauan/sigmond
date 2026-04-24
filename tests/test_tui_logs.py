"""Tests for the Logs screen."""

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
class LogsResolutionTests(unittest.TestCase):
    def test_installed_components_returns_mapping(self):
        from sigmond.tui.screens.logs import _installed_components
        result = _installed_components()
        self.assertIsInstance(result, dict)
        # Every value is a bool when non-empty.
        for comp, installed in result.items():
            self.assertIsInstance(comp, str)
            self.assertIsInstance(installed, bool)

    def test_resolve_unit_names_returns_empty_when_not_installed(self):
        from sigmond.tui.screens.logs import _resolve_unit_names
        # For a component that almost certainly has no deploy.toml on
        # this host, resolution returns an empty list — the wildcard
        # fallback was removed because it generates cryptic
        # "No data available" errors from journalctl.
        result = _resolve_unit_names("definitely-not-a-component-zzz")
        self.assertEqual(result, [])


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class LogsScreenMountTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_screen_mounts(self):
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.logs import LogsScreen

        app = SigmondApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.action_show_logs()
            await pilot.pause()
            center = app.query_one("#center")
            self.assertTrue(
                any(isinstance(c, LogsScreen) for c in center.children),
                "LogsScreen did not mount",
            )

    async def test_start_with_no_component_shows_prompt(self):
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.logs import LogsScreen

        app = SigmondApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.action_show_logs()
            await pilot.pause()
            screen = app.query_one(LogsScreen)
            # No component selected — Start should set a warning status.
            screen._start('journal')
            await pilot.pause()
            from textual.widgets import Static
            status = screen.query_one("#lg-status", Static)
            self.assertIn("Pick a component", str(status.render()))


if __name__ == '__main__':
    unittest.main()
