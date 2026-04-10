"""Tests for the harmonize rules.

These build SystemView objects by hand so they're independent of any
real /etc state on the host.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.clients.base import ClientView, InstanceView
from sigmond.coordination import (
    ClientInstance, Coordination, Cpu, DiskBudget, Host, Radiod,
)
from sigmond.harmonize import (
    ALL_RULES, _parse_cores, rule_cpu_isolation, rule_frequency_coverage,
    rule_radiod_resolution, rule_timing_chain, run_all, worst_severity,
)
from sigmond.sysview import SystemView
from sigmond.topology import Topology


def _make_view(coord: Coordination, client_views: dict | None = None) -> SystemView:
    topology = Topology(
        client_dir=None,        # not used by rules
        smd_bin=None,
        components={},
    )
    return SystemView(
        coordination=coord,
        topology=topology,
        client_views=client_views or {},
    )


class TestParseCores(unittest.TestCase):

    def test_single(self):
        self.assertEqual(_parse_cores("3"), {3})

    def test_range(self):
        self.assertEqual(_parse_cores("0-3"), {0, 1, 2, 3})

    def test_mixed(self):
        self.assertEqual(_parse_cores("0-1,5,7-8"), {0, 1, 5, 7, 8})

    def test_empty(self):
        self.assertEqual(_parse_cores(""), set())
        self.assertEqual(_parse_cores(None), set())


class TestRadiodResolution(unittest.TestCase):

    def test_passes_when_no_clients(self):
        r = rule_radiod_resolution(_make_view(Coordination()))
        self.assertEqual(r.severity, "pass")

    def test_fails_on_missing_reference(self):
        coord = Coordination(
            clients=[ClientInstance("hf-timestd", "default", radiod_id="no-such")],
        )
        r = rule_radiod_resolution(_make_view(coord))
        self.assertEqual(r.severity, "fail")
        self.assertIn("no-such", r.message)

    def test_passes_when_referenced(self):
        coord = Coordination(
            radiods={"k3lr": Radiod(id="k3lr")},
            clients=[ClientInstance("hf-timestd", "default", radiod_id="k3lr")],
        )
        r = rule_radiod_resolution(_make_view(coord))
        self.assertEqual(r.severity, "pass")


class TestCpuIsolation(unittest.TestCase):

    def test_no_overlap(self):
        coord = Coordination(
            radiods={"k3lr": Radiod(id="k3lr", host="localhost", cores="0-1")},
            cpu=Cpu(worker_cores="2-11"),
        )
        r = rule_cpu_isolation(_make_view(coord))
        self.assertEqual(r.severity, "pass")

    def test_overlap_is_fail(self):
        coord = Coordination(
            radiods={"k3lr": Radiod(id="k3lr", host="localhost", cores="0-3")},
            cpu=Cpu(worker_cores="2-11"),
        )
        r = rule_cpu_isolation(_make_view(coord))
        self.assertEqual(r.severity, "fail")
        self.assertIn("overlap", r.message)

    def test_remote_radiod_skipped(self):
        coord = Coordination(
            radiods={"shack": Radiod(id="shack", host="remote.local", cores="0-3")},
            cpu=Cpu(worker_cores="2-11"),
        )
        r = rule_cpu_isolation(_make_view(coord))
        self.assertEqual(r.severity, "pass")
        self.assertIn("skipped", r.message)


class TestFrequencyCoverage(unittest.TestCase):

    def test_within_samprate(self):
        coord = Coordination(
            radiods={"k3lr": Radiod(id="k3lr", host="localhost", samprate_hz=64_800_000)},
        )
        cv = ClientView(client_type="hf-timestd", installed=True)
        cv.instances.append(InstanceView(
            instance="default",
            radiod_id="k3lr",
            frequencies_hz=[2_500_000, 5_000_000, 10_000_000],
        ))
        r = rule_frequency_coverage(_make_view(coord, {"grape": cv}))
        self.assertEqual(r.severity, "pass")

    def test_over_samprate(self):
        coord = Coordination(
            radiods={"k3lr": Radiod(id="k3lr", host="localhost", samprate_hz=10_000_000)},
        )
        cv = ClientView(client_type="hf-timestd", installed=True)
        cv.instances.append(InstanceView(
            instance="default",
            radiod_id="k3lr",
            frequencies_hz=[25_000_000],
        ))
        r = rule_frequency_coverage(_make_view(coord, {"grape": cv}))
        self.assertEqual(r.severity, "fail")

    def test_no_samprate_skipped(self):
        coord = Coordination(
            radiods={"k3lr": Radiod(id="k3lr", host="localhost", samprate_hz=0)},
        )
        cv = ClientView(client_type="hf-timestd", installed=True)
        cv.instances.append(InstanceView(
            instance="default",
            radiod_id="k3lr",
            frequencies_hz=[25_000_000],
        ))
        r = rule_frequency_coverage(_make_view(coord, {"grape": cv}))
        self.assertEqual(r.severity, "pass")


class TestTimingChain(unittest.TestCase):

    def test_skipped_when_no_hftimestd(self):
        r = rule_timing_chain(_make_view(Coordination()))
        self.assertEqual(r.severity, "pass")
        self.assertIn("skipped", r.message)

    def test_solo_hftimestd_is_pass(self):
        coord = Coordination(
            radiods={"k3lr": Radiod(id="k3lr")},
            clients=[ClientInstance("hf-timestd", "default", radiod_id="k3lr")],
        )
        r = rule_timing_chain(_make_view(coord))
        self.assertEqual(r.severity, "pass")


class TestStandaloneSafe(unittest.TestCase):
    """The multi-radiod standalone-safe shape from the plan: two radiods
    (one local, one remote), two hf-timestd instances bound to
    different radiods.  All rules must run without failure."""

    def test_multi_radiod_fixture(self):
        coord = Coordination(
            radiods={
                "k3lr-rx888":  Radiod(id="k3lr-rx888", host="localhost",
                                      samprate_hz=64_800_000, cores="0-1"),
                "shack-airspy": Radiod(id="shack-airspy", host="shack.local",
                                       samprate_hz=10_000_000),
            },
            cpu=Cpu(worker_cores="2-11"),
            clients=[
                ClientInstance("hf-timestd", "rx888-a", radiod_id="k3lr-rx888"),
                ClientInstance("hf-timestd", "airspy-b", radiod_id="shack-airspy"),
            ],
        )
        view = _make_view(coord)
        results = run_all(view)
        self.assertEqual(worst_severity(results), "pass",
                         msg=f"unexpected severity; results={results}")


if __name__ == "__main__":
    unittest.main()
