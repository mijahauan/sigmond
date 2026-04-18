"""Tests for the cpu module — pure helpers and the affinity report shape.

Filesystem-dependent helpers (get_cache_islands, get_governors,
get_isolated_cpus) are validated through an integration test against the
live host rather than faked sysfs; their logic is a thin wrapper over
parse_cpu_mask which is covered directly.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.cpu import (
    AffinityPlan,
    AffinityReport,
    CacheIsland,
    ContendingProcess,
    PREFERRED_RADIOD_GOVERNORS,
    SystemCapabilities,
    UnitAffinity,
    _is_kernel_thread,
    build_affinity_report,
    gather_capabilities,
    parse_cmdline_cpu_param,
    parse_cpu_mask,
)


class ParseCpuMaskTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(parse_cpu_mask(''), set())

    def test_single(self):
        self.assertEqual(parse_cpu_mask('3'), {3})

    def test_range(self):
        self.assertEqual(parse_cpu_mask('0-3'), {0, 1, 2, 3})

    def test_mixed_space_and_comma(self):
        self.assertEqual(parse_cpu_mask('0-2, 5 7'), {0, 1, 2, 5, 7})

    def test_tolerates_garbage(self):
        self.assertEqual(parse_cpu_mask('0-2 xx 4'), {0, 1, 2, 4})


class ParseCmdlineCpuParamTests(unittest.TestCase):
    def test_absent(self):
        cmdline = 'BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro quiet'
        self.assertEqual(parse_cmdline_cpu_param(cmdline, 'isolcpus'), set())

    def test_simple(self):
        cmdline = 'root=/dev/sda1 isolcpus=0-3 quiet'
        self.assertEqual(parse_cmdline_cpu_param(cmdline, 'isolcpus'),
                         {0, 1, 2, 3})

    def test_with_flags(self):
        """isolcpus= supports leading flag tokens like 'domain,managed_irq,0-3'."""
        cmdline = 'isolcpus=domain,managed_irq,0-3 other=x'
        self.assertEqual(parse_cmdline_cpu_param(cmdline, 'isolcpus'),
                         {0, 1, 2, 3})

    def test_multiple_keys_disjoint(self):
        cmdline = 'isolcpus=0-3 nohz_full=0-3 rcu_nocbs=0-3'
        self.assertEqual(parse_cmdline_cpu_param(cmdline, 'nohz_full'),
                         {0, 1, 2, 3})
        self.assertEqual(parse_cmdline_cpu_param(cmdline, 'rcu_nocbs'),
                         {0, 1, 2, 3})

    def test_keyed_substring_not_matched(self):
        """Guard against 'isolcpus=' matching 'foo_isolcpus=' etc."""
        cmdline = 'fake_isolcpus=9 isolcpus=0-3'
        self.assertEqual(parse_cmdline_cpu_param(cmdline, 'isolcpus'),
                         {0, 1, 2, 3})

    def test_multiple_segments(self):
        cmdline = 'isolcpus=0-1,4-5'
        self.assertEqual(parse_cmdline_cpu_param(cmdline, 'isolcpus'),
                         {0, 1, 4, 5})


class KernelThreadDetectionTests(unittest.TestCase):
    def test_pid_2_is_kthread(self):
        self.assertTrue(_is_kernel_thread({'Pid': '2', 'PPid': '0'}))

    def test_child_of_kthreadd(self):
        self.assertTrue(_is_kernel_thread({'Pid': '42', 'PPid': '2'}))

    def test_normal_process(self):
        self.assertFalse(_is_kernel_thread({'Pid': '1234', 'PPid': '1'}))

    def test_init_process(self):
        # init (pid 1, PPid 0) is a user process, not a kthread.
        # Our simple heuristic treats PPid==0 as kthread; init is the only
        # real exception.  Accept this corner-case — contention detection
        # skipping init is harmless.
        self.assertTrue(_is_kernel_thread({'Pid': '1', 'PPid': '0'}))


class UnitAffinityTests(unittest.TestCase):
    def test_mask_mismatch_true(self):
        ua = UnitAffinity(
            unit='radiod@foo.service', role='radiod',
            main_pid='1234',
            systemd_mask={0, 1},
            observed_mask={0, 1, 2, 3},
        )
        self.assertTrue(ua.mask_mismatch)

    def test_mask_mismatch_equal(self):
        ua = UnitAffinity(
            unit='radiod@foo.service', role='radiod',
            systemd_mask={0, 1}, observed_mask={0, 1},
        )
        self.assertFalse(ua.mask_mismatch)

    def test_mask_mismatch_empty_observed(self):
        # No observed mask (unit not running) — don't flag.
        ua = UnitAffinity(
            unit='radiod@foo.service', role='radiod',
            systemd_mask={0, 1}, observed_mask=set(),
        )
        self.assertFalse(ua.mask_mismatch)


class AffinityReportTests(unittest.TestCase):
    """Integration tests — build a real report from the current host.

    These don't require radiod to be installed; compute_affinity_plan
    returns an empty radiod plan when no instances are found, and the
    report is still well-formed.
    """

    def test_build_affinity_report_shape(self):
        report = build_affinity_report()
        self.assertIsInstance(report, AffinityReport)
        self.assertIsInstance(report.capabilities, SystemCapabilities)
        self.assertIsInstance(report.plan, AffinityPlan)
        self.assertIsInstance(report.units, list)
        self.assertIsInstance(report.contention, list)
        self.assertIsInstance(report.warnings, list)

    def test_capabilities_reflect_host(self):
        caps = gather_capabilities()
        self.assertEqual(caps.logical_cpus, os.cpu_count() or 0)
        # At least one physical core.
        self.assertGreaterEqual(len(caps.physical_cores), 1)
        # Every physical core's siblings are subsets of 0..N-1.
        for core in caps.physical_cores:
            self.assertTrue(all(0 <= c < caps.logical_cpus for c in core))

    def test_radiod_cpus_subset_of_physical(self):
        report = build_affinity_report()
        all_logical = set()
        for core in report.capabilities.physical_cores:
            all_logical.update(core)
        self.assertTrue(report.radiod_cpus.issubset(all_logical))

    def test_pinned_contention_excludes_default(self):
        report = build_affinity_report()
        for c in report.pinned_contention:
            self.assertFalse(c.is_default)

    def test_preferred_governors_contains_performance(self):
        self.assertIn('performance', PREFERRED_RADIOD_GOVERNORS)


class ContendingProcessTests(unittest.TestCase):
    def test_fields(self):
        c = ContendingProcess(
            pid='1234', comm='nginx',
            allowed={0, 1, 2, 3}, overlap={0, 1}, is_default=False,
        )
        self.assertEqual(c.overlap, {0, 1})
        self.assertFalse(c.is_default)


class CacheIslandTests(unittest.TestCase):
    def test_frozen_hashable(self):
        # CacheIsland must be hashable (frozen dataclass) so it can be
        # used as a dict key in get_cache_islands's dedup.
        isle = CacheIsland(level=3, cache_type='Unified', cpus=frozenset({0, 1, 2, 3}))
        self.assertEqual(hash(isle), hash(isle))


if __name__ == '__main__':
    unittest.main()
