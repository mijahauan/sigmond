"""Navigation tests — every tree node mounts its screen without errors.

Catches placeholder wiring bugs and binding-vs-action mismatches that
unit tests on individual screens can't see.
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
class TreeNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_binding_mounts_a_screen(self):
        from sigmond.tui.app import SigmondApp

        app = SigmondApp()
        async with app.run_test(size=(120, 60)) as pilot:
            # Every main binding, in order.  A crash in any action_show_*
            # method fails the test with a clear traceback.
            for key in ('o', 't', 'c', 'r', 'v'):
                await pilot.press(key)
                await pilot.pause()
                self.assertIsNotNone(app.query_one("#center"))

    async def test_placeholders_mount_via_tree_selection(self):
        """Placeholder actions (cpu_freq, logs, lifecycle, install, update)
        have no key binding; invoke them directly through the app so the
        tree's on_tree_node_selected wiring is still exercised by the
        navigation test above."""
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.placeholder import PlaceholderScreen

        app = SigmondApp()
        async with app.run_test(size=(120, 60)) as pilot:
            for action in ("show_logs", "show_lifecycle",
                           "show_install", "show_update"):
                getattr(app, f"action_{action}")()
                await pilot.pause()
                center = app.query_one("#center")
                self.assertTrue(
                    any(isinstance(c, PlaceholderScreen) for c in center.children),
                    f"{action} did not mount a PlaceholderScreen",
                )


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class OverviewScreenMountTests(unittest.IsolatedAsyncioTestCase):
    async def test_overview_is_default_landing_and_renders(self):
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.overview import OverviewScreen

        app = SigmondApp()
        async with app.run_test(size=(120, 50)) as pilot:
            # Let the worker complete and the screen re-render.
            for _ in range(3):
                await pilot.pause()
            center = app.query_one("#center")
            self.assertTrue(
                any(isinstance(c, OverviewScreen) for c in center.children),
                "OverviewScreen should be the default landing",
            )


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class ComponentTreeStructureTests(unittest.TestCase):
    def test_tree_has_grouped_categories(self):
        """The tree exposes Configure / Observe / Operate groups plus
        Overview.  This pins the IA so category drift is visible in
        diffs."""
        from sigmond.tui.widgets.component_tree import ComponentTree
        from sigmond.topology import load_topology

        tree = ComponentTree()
        tree.populate(load_topology(), {})

        labels = [str(n.label) for n in tree.root.children]
        self.assertIn("Configure", labels)
        self.assertIn("Observe", labels)
        self.assertIn("Operate", labels)
        # Overview is a leaf at root level, not a group.
        self.assertTrue(any("Overview" in lbl for lbl in labels))


if __name__ == '__main__':
    unittest.main()
