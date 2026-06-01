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
    _cpus_to_range_str,
    _is_kernel_thread,
    affinity_report_to_dict,
    build_affinity_report,
    compute_affinity_plan,
    compute_host_cpu_layout,
    expand_template_instances,
    gather_capabilities,
    is_split_l3,
    l3_island_cpus_for,
    layout_shell_vars,
    parse_cmdline_cpu_param,
    parse_cpu_mask,
    parse_ht_pairs,
    recommended_isolcpus,
)
from unittest import mock


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

    def test_observed_mask_raw_field(self):
        # observed_mask_raw preserves /proc's exact string so thread
        # mask comparisons (which use the same format) match exactly.
        ua = UnitAffinity(
            unit='radiod@foo.service', role='radiod',
            observed_mask={0, 1}, observed_mask_raw='0-1',
        )
        self.assertEqual(ua.observed_mask_raw, '0-1')
        self.assertEqual(ua.observed_mask, {0, 1})


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


class FindContendingProcessesBenignFilterTests(unittest.TestCase):
    """Known-benign helpers inheriting radiod's cgroup cpuset (e.g.
    ``avahi-publish-s/a`` spawned by ka9q-radio) must NOT appear in
    the contention list — they can't be moved (kernel returns EINVAL)
    and have negligible CPU cost.  See `_BENIGN_RADIOD_HELPER_COMMS`.
    """

    def _fake_proc(self, monkeypatch, proc_entries):
        """Patch /proc walking + status reading with synthetic data.

        `proc_entries` is a dict mapping pid (str) → status dict.  Each
        status must contain at least 'Cpus_allowed_list' (and optionally
        'Name', 'PPid', 'Pid').  Missing PPid defaults to '1' so the
        entry is treated as a userspace process.
        """
        from sigmond import cpu as cpu_mod

        class _FakeEntry:
            def __init__(self, name):
                self.name = name

        def fake_iterdir(_path=None):
            return [_FakeEntry(pid) for pid in proc_entries]

        def fake_status(pid):
            s = dict(proc_entries.get(pid, {}))
            s.setdefault('PPid', '1')
            s.setdefault('Pid', pid)
            return s

        monkeypatch.setattr(cpu_mod.Path, 'iterdir', fake_iterdir)
        monkeypatch.setattr(cpu_mod, '_read_proc_status_fields', fake_status)

    def test_avahi_publish_helpers_excluded(self):
        # Use unittest's mock.patch instead of pytest's monkeypatch for
        # stdlib-only compat with the rest of the test suite.
        from unittest.mock import patch
        from sigmond import cpu as cpu_mod

        proc = {
            '3201851': {'Name': 'avahi-publish-s', 'Cpus_allowed_list': '0-1'},
            '3201852': {'Name': 'avahi-publish-a', 'Cpus_allowed_list': '0-1'},
            '3201853': {'Name': 'avahi-publish-s', 'Cpus_allowed_list': '0-1'},
            '3201854': {'Name': 'avahi-publish-a', 'Cpus_allowed_list': '0-1'},
            # A real userspace process on radiod cores — should still
            # be reported, proving the filter targets only the avahi
            # comms and isn't a blanket suppression.
            '9999':    {'Name': 'evil-pinned',     'Cpus_allowed_list': '0-1'},
        }

        class _FakeEntry:
            def __init__(self, name):
                self.name = name

        with patch.object(cpu_mod.Path, 'iterdir',
                          lambda self: [_FakeEntry(p) for p in proc]), \
             patch.object(cpu_mod, '_read_proc_status_fields',
                          lambda pid: {**proc[pid],
                                       'PPid': '1', 'Pid': pid}):
            results = cpu_mod.find_contending_processes({0, 1})

        comms = [r.comm for r in results]
        self.assertNotIn('avahi-publish-s', comms)
        self.assertNotIn('avahi-publish-a', comms)
        self.assertIn('evil-pinned', comms)

    def test_benign_prefix_constant_is_string_tuple(self):
        # Guard against the constant being broken to a single string
        # (which would make str.startswith treat each character as a
        # separate prefix).
        from sigmond.cpu import _BENIGN_RADIOD_HELPER_COMMS
        self.assertIsInstance(_BENIGN_RADIOD_HELPER_COMMS, tuple)
        self.assertTrue(all(isinstance(s, str)
                            for s in _BENIGN_RADIOD_HELPER_COMMS))


class CacheIslandTests(unittest.TestCase):
    def test_frozen_hashable(self):
        # CacheIsland must be hashable (frozen dataclass) so it can be
        # used as a dict key in get_cache_islands's dedup.
        isle = CacheIsland(level=3, cache_type='Unified', cpus=frozenset({0, 1, 2, 3}))
        self.assertEqual(hash(isle), hash(isle))


class ExpandTemplateInstancesTests(unittest.TestCase):
    def test_non_template_returns_self(self):
        # A concrete unit name comes back unchanged as a single-entry list.
        self.assertEqual(expand_template_instances('some-non-template.service'),
                         ['some-non-template.service'])

    def test_template_is_list(self):
        # On the live host this may be empty or populated — just assert
        # the shape.  Content is validated through integration tests.
        result = expand_template_instances('wd-decode@.service')
        self.assertIsInstance(result, list)
        for name in result:
            self.assertTrue(name.startswith('wd-decode@'))


class AffinityReportToDictTests(unittest.TestCase):
    def test_is_json_serializable(self):
        import json

        report = build_affinity_report()
        payload = affinity_report_to_dict(report)
        # Must survive json.dumps without custom encoders.
        dumped = json.dumps(payload)
        self.assertIsInstance(dumped, str)
        roundtrip = json.loads(dumped)
        # Spot-check a few keys.
        self.assertIn('capabilities', roundtrip)
        self.assertIn('plan', roundtrip)
        self.assertIn('units', roundtrip)
        self.assertIn('warnings', roundtrip)
        self.assertIn('radiod_cpus', roundtrip)

    def test_set_fields_become_sorted_lists(self):
        report = build_affinity_report()
        d = affinity_report_to_dict(report)
        # radiod_cpus (set) → sorted list
        self.assertIsInstance(d['radiod_cpus'], list)
        self.assertEqual(d['radiod_cpus'], sorted(d['radiod_cpus']))
        # capabilities.isolated_cpus (set) → sorted list
        self.assertIsInstance(d['capabilities']['isolated_cpus'], list)
        # each physical core sorted
        for core in d['capabilities']['physical_cores']:
            self.assertEqual(core, sorted(core))

    def test_thread_groups_summarized(self):
        # affinity_report_to_dict deliberately summarizes threads by
        # count rather than dumping every thread — the text renderer
        # handles that.
        report = build_affinity_report()
        d = affinity_report_to_dict(report)
        for unit in d['units']:
            self.assertIsInstance(unit['thread_group_count'], int)
            self.assertNotIn('thread_groups', unit)


class ParseHtPairsTests(unittest.TestCase):
    def test_sequential(self):
        self.assertEqual(parse_ht_pairs('0,1 2,3 4,5'), [[0, 1], [2, 3], [4, 5]])

    def test_split(self):
        self.assertEqual(parse_ht_pairs('0,8 1,9 2,10'), [[0, 8], [1, 9], [2, 10]])

    def test_range_form(self):
        self.assertEqual(parse_ht_pairs('0-1 2-3'), [[0, 1], [2, 3]])

    def test_singletons(self):
        self.assertEqual(parse_ht_pairs('0 1 2'), [[0], [1], [2]])

    def test_empty(self):
        self.assertEqual(parse_ht_pairs(''), [])


def _seq_pairs(n_cpus):
    return [[i, i + 1] for i in range(0, n_cpus, 2)]


def _split_pairs(n_cpus):
    half = n_cpus // 2
    return [[i, i + half] for i in range(half)]


class ComputeHostCpuLayoutTests(unittest.TestCase):
    def test_sequential_16cpu_matches_legacy(self):
        """Sequential host, 1 radiod: must reproduce the old hardcoded
        layout (radiod 0,1; workers 2..13; identity vCPU map; 7 cores)."""
        lay = compute_host_cpu_layout(_seq_pairs(16), local_radiod_count=1)
        self.assertEqual(lay['radiod_cpus'], [0, 1])
        self.assertEqual(lay['worker_cpus'], list(range(2, 14)))
        self.assertEqual(lay['vcpu_to_pcpu'], list(range(14)))  # identity
        self.assertEqual(lay['isolcpus'], list(range(14)))
        self.assertEqual(lay['vm_cores'], 7)
        self.assertEqual(lay['vm_vcpu_count'], 14)

    def test_split_16cpu_interleaves(self):
        """Split host (cpu0<->cpu8): radiod must get a REAL sibling pair
        {0,8}, and the vCPU map must interleave so guest pairs land on
        host pairs."""
        lay = compute_host_cpu_layout(_split_pairs(16), local_radiod_count=1)
        self.assertEqual(lay['radiod_cpus'], [0, 8])           # real sibling pair
        self.assertEqual(lay['vcpu_to_pcpu'][:2], [0, 8])      # guest core0 -> host core0
        self.assertEqual(lay['vcpu_to_pcpu'][2:4], [1, 9])     # guest core1 -> host core1
        self.assertEqual(
            lay['vcpu_to_pcpu'],
            [0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14],
        )
        self.assertEqual(lay['worker_cpus'], [1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14])
        self.assertEqual(lay['isolcpus'], [0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14])
        self.assertEqual(lay['vm_cores'], 7)

    def test_two_local_radiods_sequential(self):
        """Second local radiod gets the next physical core's pair."""
        lay = compute_host_cpu_layout(_seq_pairs(16), local_radiod_count=2)
        self.assertEqual(lay['radiod_pairs'], [[0, 1], [2, 3]])
        self.assertEqual(lay['radiod_cpus'], [0, 1, 2, 3])
        self.assertEqual(lay['worker_cpus'], list(range(4, 14)))

    def test_two_local_radiods_split(self):
        lay = compute_host_cpu_layout(_split_pairs(16), local_radiod_count=2)
        self.assertEqual(lay['radiod_pairs'], [[0, 8], [1, 9]])
        self.assertEqual(lay['radiod_cpus'], [0, 8, 1, 9])

    def test_reserves_last_pair_for_host(self):
        lay = compute_host_cpu_layout(_seq_pairs(16), local_radiod_count=1)
        # host CPUs 14,15 (last physical core) are NOT in the VM set.
        self.assertNotIn(14, lay['isolcpus'])
        self.assertNotIn(15, lay['isolcpus'])

    def test_rejects_non_smt(self):
        with self.assertRaises(ValueError):
            compute_host_cpu_layout([[0], [1], [2], [3]], local_radiod_count=1)

    def test_rejects_too_few_pairs(self):
        # 2 pairs, 1 radiod, 1 reserved host -> 0 worker pairs -> error.
        with self.assertRaises(ValueError):
            compute_host_cpu_layout(_seq_pairs(4), local_radiod_count=1)


class CpuRangeStrTests(unittest.TestCase):
    def test_contiguous(self):
        self.assertEqual(_cpus_to_range_str([0, 1, 2, 3]), '0-3')

    def test_split(self):
        self.assertEqual(
            _cpus_to_range_str([0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14]),
            '0-6,8-14',
        )

    def test_singletons_and_runs(self):
        self.assertEqual(_cpus_to_range_str([0, 2, 3, 4, 7]), '0,2-4,7')


class LayoutShellVarsTests(unittest.TestCase):
    def test_sequential_render(self):
        lay = compute_host_cpu_layout(_seq_pairs(16), local_radiod_count=1)
        out = layout_shell_vars(lay)
        self.assertIn('RADIOD_CPUS="0 1"', out)
        self.assertIn('WORKER_CPUS="2 3 4 5 6 7 8 9 10 11 12 13"', out)
        self.assertIn('VCPU_TO_PCPU="0 1 2 3 4 5 6 7 8 9 10 11 12 13"', out)
        self.assertIn('ISOLCPUS_RANGE="0-13"', out)
        self.assertIn('VM_CORES="7"', out)

    def test_split_render_isolcpus_is_noncontiguous(self):
        lay = compute_host_cpu_layout(_split_pairs(16), local_radiod_count=1)
        out = layout_shell_vars(lay)
        self.assertIn('ISOLCPUS_RANGE="0-6,8-14"', out)
        self.assertIn('VCPU_TO_PCPU="0 8 1 9 2 10 3 11 4 12 5 13 6 14"', out)


def _isle(level, cpus, ctype='Unified'):
    return CacheIsland(level=level, cache_type=ctype, cpus=frozenset(cpus))


# 5700U-style split L3: two 8-thread islands.
SPLIT_L3 = [_isle(3, range(0, 8)), _isle(3, range(8, 16))]
# Unified L3: one island over all 16 threads.
UNIFIED_L3 = [_isle(3, range(0, 16))]


class L3HelperTests(unittest.TestCase):
    def test_l3_island_for_split(self):
        # radiod on {0,1} owns the whole 0-7 island.
        self.assertEqual(l3_island_cpus_for(SPLIT_L3, {0, 1}), set(range(8)))
        # radiod on {8,9} owns the 8-15 island.
        self.assertEqual(l3_island_cpus_for(SPLIT_L3, {8, 9}), set(range(8, 16)))

    def test_l3_island_for_unified(self):
        self.assertEqual(l3_island_cpus_for(UNIFIED_L3, {0, 1}), set(range(16)))

    def test_l3_island_no_topology_returns_input(self):
        self.assertEqual(l3_island_cpus_for([], {0, 1}), {0, 1})

    def test_is_split_l3(self):
        self.assertTrue(is_split_l3(SPLIT_L3, 16))
        self.assertFalse(is_split_l3(UNIFIED_L3, 16))
        self.assertFalse(is_split_l3([], 16))


class CacheAwarePlanTests(unittest.TestCase):
    """compute_affinity_plan must segregate other work off radiod's whole L3
    island on a split-L3 host, and leave behaviour unchanged when unified."""

    def _plan(self, l3, ca=None, instances=('radiod@rx.service',),
              cores=None):
        cores = cores or [{0, 1}, {2, 3}, {4, 5}, {6, 7},
                          {8, 9}, {10, 11}, {12, 13}, {14, 15}]
        with mock.patch('sigmond.cpu.get_physical_cores', return_value=cores), \
             mock.patch('sigmond.cpu.get_radiod_instances',
                        return_value=list(instances)):
            return compute_affinity_plan(ca, l3_islands=l3, logical_cpus=16)

    def test_split_excludes_whole_island(self):
        plan = self._plan(SPLIT_L3)
        self.assertTrue(plan.cache_split)
        self.assertEqual(plan.radiod['radiod@rx.service'], {0, 1})
        self.assertEqual(plan.radiod_l3_cpus, set(range(8)))
        # Other work confined to the *other* island only.
        self.assertEqual(plan.other_cpus, set(range(8, 16)))
        # 2-7 share radiod's L3 → reserved idle, used by no one.
        self.assertEqual(plan.reserved_idle_cpus, {2, 3, 4, 5, 6, 7})
        self.assertEqual(recommended_isolcpus(plan), set(range(8)))

    def test_unified_is_legacy_behaviour(self):
        plan = self._plan(UNIFIED_L3)
        self.assertFalse(plan.cache_split)
        # Only radiod's own cores excluded; everything else available.
        self.assertEqual(plan.other_cpus, set(range(16)) - {0, 1})
        self.assertEqual(plan.reserved_idle_cpus, set())
        self.assertEqual(recommended_isolcpus(plan), {0, 1})

    def test_explicit_other_cpus_override_wins(self):
        plan = self._plan(SPLIT_L3, ca={'other_cpus': '10-15'})
        self.assertEqual(plan.other_cpus, {10, 11, 12, 13, 14, 15})

    def test_cache_aware_opt_out(self):
        plan = self._plan(SPLIT_L3, ca={'cache_aware': False})
        # Segregation disabled → legacy: only radiod's cores excluded.
        self.assertEqual(plan.other_cpus, set(range(16)) - {0, 1})
        self.assertEqual(plan.reserved_idle_cpus, set())

    def test_cache_aware_opt_out_string(self):
        plan = self._plan(SPLIT_L3, ca={'cache_aware': 'false'})
        self.assertEqual(plan.other_cpus, set(range(16)) - {0, 1})

    def test_no_radiod_instances_no_segregation(self):
        plan = self._plan(SPLIT_L3, instances=())
        self.assertEqual(plan.radiod_l3_cpus, set())
        self.assertEqual(plan.reserved_idle_cpus, set())


if __name__ == '__main__':
    unittest.main()
