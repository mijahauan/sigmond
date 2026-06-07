"""Hermetic tests for the per-component lifecycle track derivation."""
import unittest
from sigmond.component_state import (
    applicable_stages, stage_progress, ComponentState,
)

FULL = ["available", "downloaded", "installed", "configured", "enabled", "running"]


class TestApplicableStages(unittest.TestCase):
    def test_library_terminal_at_installed(self):
        self.assertEqual(applicable_stages("x", {}, "library"),
                         ["available", "downloaded", "installed"])

    def test_tool_no_units_no_config_terminal_at_installed(self):
        self.assertEqual(applicable_stages("x", {}, "infra"),
                         ["available", "downloaded", "installed"])

    def test_service_no_config_skips_configured(self):
        d = {"systemd": {"units": ["x.service"]}}
        self.assertEqual(applicable_stages("x", d, "infra"),
                         ["available", "downloaded", "installed",
                          "enabled", "running"])

    def test_client_full_track(self):
        d = {"systemd": {"templated_units": ["x@.service"]},
             "contract": {"config": {"init": ["x"]}}}
        self.assertEqual(applicable_stages("x", d, "client"), FULL)

    def test_render_step_implies_configured(self):
        d = {"systemd": {"units": ["x.service"]},
             "install": {"steps": [{"kind": "render", "dst": "/etc/x/x.toml"}]}}
        self.assertIn("configured", applicable_stages("x", d, "client"))


class TestStageProgress(unittest.TestCase):
    def test_downloaded_position(self):
        st = ComponentState("x", cloned=True, installed=False, configured=False,
                            enabled=False, active=False)
        pos, reached, nxt = stage_progress(st, FULL)
        self.assertEqual((pos, reached, nxt), (1, "downloaded", "installed"))

    def test_service_installed_next_is_enabled_not_configured(self):
        # no-config service: 'configured' isn't in the track, so installed -> enabled
        track = ["available", "downloaded", "installed", "enabled", "running"]
        st = ComponentState("x", cloned=True, installed=True, configured=True,
                            enabled=False, active=False)
        pos, reached, nxt = stage_progress(st, track)
        self.assertEqual((reached, nxt), ("installed", "enabled"))

    def test_running_is_terminal(self):
        st = ComponentState("x", cloned=True, installed=True, configured=True,
                            enabled=True, active=True)
        pos, reached, nxt = stage_progress(st, FULL)
        self.assertEqual((reached, nxt), ("running", None))


if __name__ == "__main__":
    unittest.main()
