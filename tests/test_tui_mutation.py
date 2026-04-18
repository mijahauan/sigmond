"""Tests for the mutation plumbing and Lifecycle screen.

Covers: ConfirmModal yes/no flow, confirm_and_run calls through to the
command runner, Lifecycle screen mounts and builds the correct argv for
each verb.  The actual sudo/subprocess invocation is mocked out since
tests run unprivileged.
"""

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
class ConfirmModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_escape_cancels(self):
        from textual.app import App
        from sigmond.tui.mutation import ConfirmModal

        results = []

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    ConfirmModal("T", "B", cmd_preview="echo hi"),
                    lambda r: results.append(r),
                )

        app = Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

        self.assertEqual(results, [False])

    async def test_yes_button_confirms(self):
        from textual.app import App
        from sigmond.tui.mutation import ConfirmModal

        results = []

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    ConfirmModal("T", "B"),
                    lambda r: results.append(r),
                )

        app = Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            modal = app.screen
            modal.query_one("#cm-yes").press()
            await pilot.pause()

        self.assertEqual(results, [True])


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class ConfirmAndRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_declining_does_not_invoke_runner(self):
        from textual.app import App
        from sigmond.tui.mutation import confirm_and_run

        calls = []

        class Harness(App):
            async def on_mount(self):
                confirm_and_run(
                    self, "T", "B", cmd=["smd", "start"], sudo=True,
                    on_complete=lambda r: calls.append(r),
                )

        app = Harness()
        with patch("sigmond.tui.mutation.subprocess.run") as run_mock:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()

        run_mock.assert_not_called()
        self.assertEqual(calls, [])

    async def test_accepting_invokes_runner_with_sudo_prefix(self):
        """Patch suspend_and_run_sudo itself since Pilot doesn't support
        App.suspend() — we care that the right argv gets through to it."""
        from textual.app import App
        from sigmond.tui.mutation import confirm_and_run

        calls = []
        captured_argv = []
        fake_result = subprocess.CompletedProcess(args=[], returncode=0)

        def fake_runner(app_, cmd):
            captured_argv.append(cmd)
            return fake_result

        class Harness(App):
            async def on_mount(self):
                confirm_and_run(
                    self, "T", "B", cmd=["smd", "start"], sudo=True,
                    on_complete=lambda r: calls.append(r),
                )

        app = Harness()
        with patch("sigmond.tui.mutation.suspend_and_run_sudo",
                   side_effect=fake_runner):
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                modal = app.screen
                modal.query_one("#cm-yes").press()
                await pilot.pause()

        # suspend_and_run_sudo is what prepends 'sudo'; our fake runner
        # received the raw cmd, and we verify the contract at its boundary.
        self.assertEqual(captured_argv, [["smd", "start"]])
        self.assertEqual(calls, [fake_result])


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class LifecycleScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_screen_mounts(self):
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.lifecycle import LifecycleScreen

        app = SigmondApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.action_show_lifecycle()
            for _ in range(3):
                await pilot.pause()
            self.assertTrue(any(isinstance(c, LifecycleScreen)
                                for c in app.query_one("#center").children))

    async def test_verb_button_builds_correct_argv(self):
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.lifecycle import LifecycleScreen

        captured_argv = []
        fake_result = subprocess.CompletedProcess(args=[], returncode=0)

        def fake_runner(app_, cmd):
            captured_argv.append(cmd)
            return fake_result

        app = SigmondApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.action_show_lifecycle()
            for _ in range(3):
                await pilot.pause()

            with patch("sigmond.tui.mutation.suspend_and_run_sudo",
                       side_effect=fake_runner):
                app.query_one("#lc-start").press()
                await pilot.pause()
                modal = app.screen
                modal.query_one("#cm-yes").press()
                await pilot.pause()

        self.assertEqual(len(captured_argv), 1,
                         f"expected one runner call; got {captured_argv}")
        argv = captured_argv[0]
        # argv passed to suspend_and_run_sudo is the raw command; 'sudo'
        # is prepended inside the runner.
        self.assertTrue(argv[0].endswith('smd'),
                        f"expected smd binary; got {argv[0]}")
        self.assertEqual(argv[1], 'start')


if __name__ == '__main__':
    unittest.main()
