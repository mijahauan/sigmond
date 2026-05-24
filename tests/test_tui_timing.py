"""Tests for the Timing TUI screen.

Covers the pure-function parsers (parse_sources / parse_tracking) and
the formatter helpers separately from the live screen — those are the
parts most likely to break if chrony's CSV output changes between
versions.  A small mount-test confirms the screen plumbs into the app
correctly without needing a running chronyd.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

try:
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


class ParserTests(unittest.TestCase):
    """Parsers are pure functions — no Textual needed."""

    def test_parse_sources_two_refclocks_two_servers(self):
        """Current bee1 chronyc layout per project_hf_pps_t5_direct_2026-05-23
        (commit 4c14712): TSL1 dropped entirely, TSL2 renamed FUSE,
        TSL3 renamed HPPS. SHM unit 0 unused; SHM unit 1 = FUSE;
        SHM unit 2 = HPPS."""
        from sigmond.tui.screens.timing import parse_sources
        sample = (
            "#,?,FUSE,0,4,111,29,-0.000378765,-0.000378765,0.000600000\n"
            "#,?,HPPS,0,0,0,20,-0.000001919,-0.000006823,0.000055000\n"
            "^,*,192.168.1.80,1,4,377,7,0.000007242,0.000005859,0.000102877\n"
            "^,?,132.163.96.3,1,8,377,84,-0.000826960,-0.000831875,0.013113145\n"
        )
        rows = parse_sources(sample)
        self.assertEqual(len(rows), 4)
        names = [r.name for r in rows]
        self.assertEqual(names, ['FUSE', 'HPPS', '192.168.1.80',
                                 '132.163.96.3'])
        # Spot-check a few fields.
        self.assertEqual(rows[0].mode, '#')
        self.assertEqual(rows[1].name, 'HPPS')
        self.assertAlmostEqual(rows[1].sample_error_sec, 5.5e-5)
        self.assertEqual(rows[2].state, '*')
        self.assertEqual(format_round(rows[2].last_offset_sec), '7.242e-06')

    def test_parse_sources_skips_malformed_rows(self):
        from sigmond.tui.screens.timing import parse_sources
        sample = (
            "#,?,FUSE,0,4,111,23,0.001,0.001,0.002\n"
            "trash row\n"
            "#,?,HPPS,not_a_number,4,111,29,-0.0003,-0.0003,0.0006\n"
            "^,*,server,1,4,377,7,0.000007,0.000005,0.000102\n"
        )
        rows = parse_sources(sample)
        # Only the well-formed rows survive.
        self.assertEqual([r.name for r in rows], ['FUSE', 'server'])

    def test_parse_sources_empty_input(self):
        from sigmond.tui.screens.timing import parse_sources
        self.assertEqual(parse_sources(""), [])
        self.assertEqual(parse_sources("\n\n"), [])

    def test_parse_tracking_full_row(self):
        from sigmond.tui.screens.timing import parse_tracking
        sample = (
            "C0A80150,192.168.1.80,2,1778242874.833737496,0.000001075,"
            "-0.000001383,0.000001174,-85.209,0.006,0.050,"
            "0.000158451,0.000031079,11.3,Normal\n"
        )
        t = parse_tracking(sample)
        self.assertIsNotNone(t)
        self.assertEqual(t.ref_id_name, '192.168.1.80')
        self.assertEqual(t.stratum, 2)
        self.assertAlmostEqual(t.last_offset_sec, -1.383e-6)
        self.assertAlmostEqual(t.root_dispersion_sec, 31.079e-6)
        self.assertEqual(t.leap_status, 'Normal')

    def test_parse_tracking_short_row_returns_none(self):
        from sigmond.tui.screens.timing import parse_tracking
        # Only 5 fields — chrony would never emit this, but defensive.
        self.assertIsNone(parse_tracking("a,b,c,d,e\n"))
        self.assertIsNone(parse_tracking(""))
        self.assertIsNone(parse_tracking(None or ""))


class FormatterTests(unittest.TestCase):

    def test_format_offset_auto_scales(self):
        from sigmond.tui.screens.timing import format_offset
        self.assertEqual(format_offset(0.0), '+0.0 ns')
        self.assertEqual(format_offset(2.3e-9), '+2.3 ns')
        self.assertEqual(format_offset(-2.3e-6), '-2.30 µs')
        self.assertEqual(format_offset(1.5e-3), '+1.50 ms')
        self.assertEqual(format_offset(-2.5), '-2.500 s')

    def test_format_age(self):
        from sigmond.tui.screens.timing import format_age
        self.assertEqual(format_age(0), 'now')
        self.assertEqual(format_age(-1), 'now')
        self.assertEqual(format_age(45), '45s')
        self.assertEqual(format_age(125), '2m05')
        self.assertEqual(format_age(3725), '1h02')

    def test_format_reach_bitcount(self):
        from sigmond.tui.screens.timing import format_reach
        self.assertEqual(format_reach(0), '0/8')
        self.assertEqual(format_reach(255), '8/8')
        # 0o111 = decimal 73, three bits set.
        self.assertEqual(format_reach(73), '3/8')
        # 0o377 = decimal 255, eight bits set.
        self.assertEqual(format_reach(0o377), '8/8')

    def test_sparkline_pad_and_render(self):
        from sigmond.tui.screens.timing import sparkline, SPARKS
        # Empty — full-width pad.
        self.assertEqual(sparkline([], width=8), ' ' * 8)
        # Flat series — middle band.
        flat = sparkline([1.0, 1.0, 1.0], width=8)
        self.assertEqual(len(flat), 8)
        # Pad chars on the left, all blocks on the right.
        self.assertTrue(flat.startswith(' ' * 5))
        # Range — uses both extremes.
        ramp = sparkline(list(range(8)), width=8)
        self.assertEqual(ramp[0], SPARKS[0])
        self.assertEqual(ramp[-1], SPARKS[-1])


def format_round(x: float) -> str:
    """Tiny helper for assertions to avoid float-precision flakiness."""
    return f"{x:.3e}"


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class ScreenMountTests(unittest.IsolatedAsyncioTestCase):

    async def test_timing_mounts_via_action(self):
        # Mount the screen with chronyc unavailable so the test does
        # not require a running chrony.  The screen should still come
        # up cleanly and show the 'unavailable' status.
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.timing import TimingScreen

        with patch('sigmond.tui.screens.timing._run_chronyc',
                   return_value=None):
            app = SigmondApp()
            async with app.run_test(size=(140, 50)) as pilot:
                app.action_show_timing()
                for _ in range(3):
                    await pilot.pause()
                center = app.query_one("#center")
                self.assertTrue(
                    any(isinstance(c, TimingScreen)
                        for c in center.children),
                    "TimingScreen did not mount",
                )

    async def test_timing_renders_with_mock_chronyc(self):
        from sigmond.tui.app import SigmondApp
        from sigmond.tui.screens.timing import TimingScreen
        from textual.widgets import DataTable

        sources_csv = (
            "#,?,FUSE,0,4,111,5,0.001,0.001,0.002\n"
            "#,?,HPPS,0,0,0,1,-0.000002,-0.000002,0.000055\n"
            "^,*,192.168.1.80,1,4,377,3,0.000005,0.000005,0.000100\n"
        )
        tracking_csv = (
            "C0A80150,192.168.1.80,2,1778242874.0,0.0000010,"
            "-0.0000010,0.0000010,-85.0,0.0,0.05,"
            "0.0001,0.00003,1.0,Normal\n"
        )

        def fake_run(args):
            if 'sources' in args:
                return sources_csv
            if 'tracking' in args:
                return tracking_csv
            return None

        with patch('sigmond.tui.screens.timing._run_chronyc',
                   side_effect=fake_run):
            app = SigmondApp()
            async with app.run_test(size=(140, 50)) as pilot:
                app.action_show_timing()
                for _ in range(3):
                    await pilot.pause()
                screen = app.query_one(TimingScreen)
                table = screen.query_one("#timing-table", DataTable)
                self.assertEqual(table.row_count, 3)


if __name__ == '__main__':
    unittest.main()
