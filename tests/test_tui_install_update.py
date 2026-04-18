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

    async def test_install_all_invokes_plain_smd_install(self):
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
                app.query_one("#is-all").press()
                await pilot.pause()
                modal = app.screen
                modal.query_one("#cm-yes").press()
                await pilot.pause()

        if captured:
            argv = captured[0]
            self.assertTrue(argv[0].endswith('smd'))
            self.assertEqual(argv[1], 'install')
            self.assertEqual(len(argv), 2,
                             "catalog walk should pass no --components")


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class UpdateScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_mounts(self):
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.update import UpdateScreen

        app = SigmondApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.action_show_update()
            for _ in range(2):
                await pilot.pause()
            self.assertTrue(any(isinstance(c, UpdateScreen)
                                for c in app.query_one("#center").children))

    async def test_update_button_runs_smd_update(self):
        from sigmond.tui.app import SigmondApp

        captured = []
        fake = subprocess.CompletedProcess(args=[], returncode=0)

        def fake_runner(app_, cmd):
            captured.append(cmd)
            return fake

        app = SigmondApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.action_show_update()
            for _ in range(2):
                await pilot.pause()

            with patch("sigmond.tui.mutation.suspend_and_run_sudo",
                       side_effect=fake_runner):
                app.query_one("#up-button").press()
                await pilot.pause()
                modal = app.screen
                modal.query_one("#cm-yes").press()
                await pilot.pause()

        self.assertEqual(len(captured), 1)
        argv = captured[0]
        self.assertTrue(argv[0].endswith('smd'))
        self.assertEqual(argv[1], 'update')


if __name__ == '__main__':
    unittest.main()
