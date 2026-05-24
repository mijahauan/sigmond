"""Tests for sigmond.tui.format — pure-Python formatting helpers.

This module deliberately has no Textual dependency, so these tests
run in any environment with sigmond installed (unlike test_tui_timing
which imports a screen module that pulls in Textual at top level)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.tui.format import format_timing_line


def _inst(**fields):
    """Build a SimpleNamespace mocking the InstanceView fields the
    formatter reads.  Defaults to the boring case (all flags false,
    timing_authority_applied None)."""
    defaults = dict(
        provides_timing_calibration=False,
        uses_timing_calibration=False,
        timing_authority_applied=None,
    )
    defaults.update(fields)
    return SimpleNamespace(**defaults)


class FormatTimingLineProducerTests(unittest.TestCase):
    """Case 1 — instance is itself the §18 producer."""

    def test_producer_returns_distinctive_green_marker(self):
        line = format_timing_line(_inst(provides_timing_calibration=True))
        self.assertIsNotNone(line)
        self.assertIn("provides authority", line)
        self.assertIn("[green]", line)

    def test_producer_takes_precedence_over_applied(self):
        """If a client both provides AND has applied a peer authority
        (hypothetical future stratum-cascading hf-timestd), the
        producer label wins — that's the more interesting station-
        wide role to surface."""
        line = format_timing_line(_inst(
            provides_timing_calibration=True,
            timing_authority_applied={'tier': 'T5', 'source': 'peer'},
        ))
        self.assertIn("provides authority", line)


class FormatTimingLineAppliedTests(unittest.TestCase):
    """Case 2 — instance is actively subscribing to a §18 authority."""

    def test_basic_subscriber_t5_green(self):
        line = format_timing_line(_inst(
            timing_authority_applied={
                'source': 'hf-timestd@bee3',
                'tier': 'T5',
                'sigma_ns': 1200,
                'snapshot_age_s': 4.2,
            },
        ))
        self.assertIsNotNone(line)
        self.assertIn("T5", line)
        self.assertIn("[green]", line)
        self.assertIn("source=hf-timestd@bee3", line)
        self.assertIn("age=4.2s", line)

    def test_t6_also_green(self):
        """T5 and T6 are both ns-class hard-wired paths per the
        revised ARCHITECTURE-FIRST-PRINCIPLES.md §2 — both render
        green."""
        line = format_timing_line(_inst(
            timing_authority_applied={
                'source': 'hf-timestd@bee3',
                'tier': 'T6',
                'sigma_ns': 1,
                'snapshot_age_s': 0.5,
            },
        ))
        self.assertIn("[green]", line)
        self.assertIn("T6", line)

    def test_t4_yellow(self):
        """T4 is LAN-stratum-1 µs-to-ms class per the revised table —
        usable but not hard-deadline-grade."""
        line = format_timing_line(_inst(
            timing_authority_applied={
                'source': 'lan-ntp@bee2',
                'tier': 'T4',
                'sigma_ns': 500_000,   # 500 µs
                'snapshot_age_s': 30.0,
            },
        ))
        self.assertIn("[yellow]", line)
        self.assertIn("T4", line)

    def test_t3_or_lower_red(self):
        for tier in ('T3', 'T2', 'T1', 'T0'):
            line = format_timing_line(_inst(
                timing_authority_applied={
                    'source': 'fallback', 'tier': tier,
                    'sigma_ns': 5_000_000, 'snapshot_age_s': 60.0,
                },
            ))
            self.assertIn("[red]", line, f"tier {tier} should render red")
            self.assertIn(tier, line)

    def test_sigma_auto_scales_ns(self):
        line = format_timing_line(_inst(
            timing_authority_applied={
                'source': 'src', 'tier': 'T6',
                'sigma_ns': 500, 'snapshot_age_s': 1.0,
            },
        ))
        self.assertIn("σ=500 ns", line)

    def test_sigma_auto_scales_us(self):
        line = format_timing_line(_inst(
            timing_authority_applied={
                'source': 'src', 'tier': 'T5',
                'sigma_ns': 1200, 'snapshot_age_s': 1.0,
            },
        ))
        self.assertIn("σ=1.2 µs", line)

    def test_sigma_auto_scales_ms(self):
        line = format_timing_line(_inst(
            timing_authority_applied={
                'source': 'src', 'tier': 'T3',
                'sigma_ns': 3_500_000, 'snapshot_age_s': 1.0,
            },
        ))
        self.assertIn("σ=3.5 ms", line)

    def test_age_auto_scales_minutes(self):
        """Snapshot ages above 60 s render as minutes — important for
        spotting "this snapshot is dangerously stale" cases without
        squinting at a four-digit second count."""
        line = format_timing_line(_inst(
            timing_authority_applied={
                'source': 'src', 'tier': 'T5',
                'sigma_ns': 1000, 'snapshot_age_s': 180.0,
            },
        ))
        self.assertIn("age=3.0m", line)

    def test_missing_optional_fields_render_question_marks(self):
        """Defensive: a producer that hasn't fully populated the
        snapshot shouldn't crash the renderer — show '?' so the gap
        is operator-visible."""
        line = format_timing_line(_inst(
            timing_authority_applied={
                'source': 'partial', 'tier': 'T5',
                # sigma_ns + snapshot_age_s absent
            },
        ))
        self.assertIn("σ=?", line)
        self.assertIn("age=?", line)

    def test_unknown_tier_renders_red(self):
        """A tier string we don't recognise (future T7, malformed) is
        treated as low-quality (red) — the safe default; never crash
        on unfamiliar tier names."""
        line = format_timing_line(_inst(
            timing_authority_applied={
                'source': 'src', 'tier': 'T7',
                'sigma_ns': 1, 'snapshot_age_s': 1.0,
            },
        ))
        self.assertIn("[red]", line)
        self.assertIn("T7", line)


class FormatTimingLineCapableTests(unittest.TestCase):
    """Case 3 — instance is subscriber-capable but currently in default
    mode (authority unreachable, gated off, or never connected)."""

    def test_capable_but_default_mode(self):
        line = format_timing_line(_inst(
            uses_timing_calibration=True,
            timing_authority_applied=None,
        ))
        self.assertIsNotNone(line)
        self.assertIn("[yellow]", line)
        self.assertIn("subscriber-capable", line)
        self.assertIn("default mode", line)

    def test_applied_takes_precedence_over_capable(self):
        """If the client is capable AND currently applying, show the
        applied snapshot — capability becomes implicit."""
        line = format_timing_line(_inst(
            uses_timing_calibration=True,
            timing_authority_applied={
                'source': 'src', 'tier': 'T5',
                'sigma_ns': 1000, 'snapshot_age_s': 1.0,
            },
        ))
        self.assertIn("T5", line)
        self.assertNotIn("subscriber-capable", line)


class FormatTimingLineBoringTests(unittest.TestCase):
    """Case 4 — instance has no §18 role; emit no line at all to keep
    the Overview screen scannable."""

    def test_all_false_returns_none(self):
        self.assertIsNone(format_timing_line(_inst()))

    def test_explicit_none_for_applied_with_no_capability(self):
        self.assertIsNone(format_timing_line(_inst(
            uses_timing_calibration=False,
            timing_authority_applied=None,
        )))

    def test_non_dict_applied_value_treated_as_absent(self):
        """A garbage applied value (string, list) shouldn't be rendered
        as if it were a populated dict. Falls back to capability check."""
        for bad in ('hf-timestd', ['a'], 42, True):
            self.assertIsNone(format_timing_line(_inst(
                timing_authority_applied=bad,
            )), f"non-dict applied={bad!r} should yield None")


if __name__ == '__main__':
    unittest.main()
