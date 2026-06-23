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

    async def test_no_stale_placeholders_remain(self):
        """Every non-binding action should mount a real screen — no
        PlaceholderScreen should survive into the mounted widget tree
        after the last mutation screen (Update) landed."""
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.placeholder import PlaceholderScreen

        app = SigmondApp()
        async with app.run_test(size=(120, 60)) as pilot:
            for action in ("show_overview", "show_topology",
                           "show_cpu_affinity", "show_cpu_freq",
                           "show_radiod", "show_gpsdo", "show_logs",
                           "show_validate", "show_diag_net",
                           "show_lifecycle", "show_apply",
                           "show_config", "show_install", "show_update"):
                getattr(app, f"action_{action}")()
                await pilot.pause()
                center = app.query_one("#center")
                self.assertFalse(
                    any(isinstance(c, PlaceholderScreen)
                        for c in center.children),
                    f"{action} mounted a stale PlaceholderScreen",
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
        """The tree exposes the four operator-workflow groups
        (Monitoring / Maintenance / Debugging / Installation) plus
        Overview as a root-level leaf.  This pins the IA so category
        drift is visible in diffs.  See docs/TUI-FUNCTION-INVENTORY.md
        for the category rationale."""
        from sigmond.tui.widgets.component_tree import ComponentTree
        from sigmond.topology import load_topology

        tree = ComponentTree()
        tree.populate(load_topology(), {})

        labels = [str(n.label) for n in tree.root.children]
        self.assertIn("Monitoring", labels)
        self.assertIn("Maintenance", labels)
        self.assertIn("Debugging", labels)
        self.assertIn("Installation", labels)
        self.assertIn("Advanced", labels)
        # Overview is a leaf at root level, not a group.
        self.assertTrue(any("Overview" in lbl for lbl in labels))

    def test_installation_is_the_three_step_arc(self):
        """Installation collapses to the guided + ①②③ arc; Topology is no
        longer a leaf (derived state, surfaced by step ③).  See
        docs/install-redesign.md Stage 3."""
        from sigmond.tui.widgets.component_tree import ComponentTree
        from sigmond.topology import load_topology

        tree = ComponentTree()
        tree.populate(load_topology(), {})

        inst = next(n for n in tree.root.children
                    if str(n.label) == "Installation")
        screens = [leaf.data.get("screen") for leaf in inst.children]
        self.assertEqual(
            screens, ["greenfield", "install", "configuration", "lifecycle"])
        # Topology must not appear as a primary nav leaf anywhere.
        all_screens = [leaf.data.get("screen")
                       for grp in tree.root.children
                       for leaf in grp.children if leaf.data]
        self.assertNotIn("topology", all_screens)


if __name__ == '__main__':
    unittest.main()
