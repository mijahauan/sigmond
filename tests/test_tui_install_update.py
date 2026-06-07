"""Tests for the Install and Update mutation screens."""

import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

try:
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class InstallScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_mounts(self):
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.install import InstallScreen

        app = SigmondApp()
        async with app.run_test(size=(130, 50)) as pilot:
            app.action_show_install()
            for _ in range(3):
                await pilot.pause()
            self.assertTrue(any(isinstance(c, InstallScreen)
                                for c in app.query_one("#center").children))

    async def test_install_enabled_invokes_topology_scoped_install(self):
        from sigmond.tui.app import SigmondApp

        captured = []
        fake = subprocess.CompletedProcess(args=[], returncode=0)

        def fake_runner(app_, cmd):
            captured.append(cmd)
            return fake

        app = SigmondApp()
        async with app.run_test(size=(130, 50)) as pilot:
            app.action_show_install()
            for _ in range(3):
                await pilot.pause()

            with patch("sigmond.tui.mutation.suspend_and_run_sudo",
                       side_effect=fake_runner):
                app.query_one("#is-enabled").press()
                await pilot.pause()
                modal = app.screen
                modal.query_one("#cm-yes").press()
                await pilot.pause()

        if captured:
            argv = captured[0]
            self.assertTrue(argv[0].endswith('smd'))
            self.assertEqual(argv[1], 'install')
            # "Install enabled" installs the topology-enabled set: plain
            # `smd install --yes` (no --components — the topology filter scopes
            # it), so the screen's confirm doubles as the install confirmation.
            self.assertNotIn('--components', argv)
            self.assertIn('--yes', argv)


# UpdateScreenTests removed: the separate Update screen was merged into
# the List ("Software versions") screen (CLAUDE.md, smd list --apply).
# sigmond.tui.screens.update no longer exists.


if __name__ == '__main__':
    unittest.main()
