"""Tests for the harmonize rules.

These build SystemView objects by hand so they're independent of any
real /etc state on the host.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.clients.base import ClientView, InstanceView
from sigmond.coordination import (
    ClientInstance, Coordination, Cpu, DiskBudget, Host, Radiod,
)
from sigmond import harmonize
from sigmond.harmonize import (
    ALL_RULES, ALL_RUNTIME_RULES, _parse_cores,
    rule_cpu_isolation, rule_cpu_isolation_runtime,
    rule_frequency_coverage, rule_gpsdo_governor_coverage,
    rule_radiod_resolution, rule_timing_chain,
    run_all, worst_severity,
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


class TestRuleCpuIsolationRuntime(unittest.TestCase):
    """rule_cpu_isolation_runtime is listed in ALL_RUNTIME_RULES, not
    ALL_RULES, so hand-built unit tests don't pick up live host state.
    These tests cover the skip paths and the dispatch wiring."""

    def test_no_radiods_skipped(self):
        view = _make_view(Coordination())
        result = rule_cpu_isolation_runtime(view)
        self.assertEqual(result.severity, "pass")
        self.assertIn("no local radiod", result.message)

    def test_remote_only_skipped(self):
        coord = Coordination(radiods={
            "remote": Radiod(id="remote", host="other.local"),
        })
        view = _make_view(coord)
        result = rule_cpu_isolation_runtime(view)
        self.assertEqual(result.severity, "pass")
        self.assertIn("no local radiod", result.message)

    def test_build_affinity_report_failure_tolerated(self):
        from unittest.mock import patch

        coord = Coordination(radiods={
            "k3lr-rx888": Radiod(id="k3lr-rx888", host="localhost"),
        })
        view = _make_view(coord)

        with patch("sigmond.cpu.build_affinity_report",
                   side_effect=RuntimeError("no systemctl on this host")):
            result = rule_cpu_isolation_runtime(view)
        self.assertEqual(result.severity, "pass")
        self.assertIn("runtime check unavailable", result.message)

    def test_run_all_default_excludes_runtime(self):
        coord = Coordination()
        view = _make_view(coord)
        names = {r.rule for r in run_all(view)}
        self.assertNotIn("cpu_isolation_runtime", names)

    def test_run_all_include_runtime_adds_runtime_rules(self):
        coord = Coordination()
        view = _make_view(coord)
        names = {r.rule for r in run_all(view, include_runtime=True)}
        self.assertIn("cpu_isolation_runtime", names)
        for r in ALL_RULES:
            # Every declared rule is still present.
            expected_name = r(view).rule
            self.assertIn(expected_name, names)

    def test_runtime_rule_catalog_stable(self):
        # Catches accidental addition/removal of runtime rules — update
        # this test along with ALL_RUNTIME_RULES if you change the set.
        self.assertEqual(
            [r.__name__ for r in ALL_RUNTIME_RULES],
            ["rule_cpu_isolation_runtime", "rule_gpsdo_governor_coverage"],
        )


class TestRuleGpsdoGovernorCoverage(unittest.TestCase):
    """gpsdo-monitor declares `governs = ["radiod:<id>"]` in each
    /run/gpsdo/<serial>.json it writes. The rule enforces that every
    local radiod has exactly one device claiming to govern it."""

    def _redirect_run_dir(self, tmp: Path) -> None:
        """Point the module-level GPSDO_RUN_DIR at a tmp path for this
        test. unittest.addCleanup restores the original afterwards."""
        original = harmonize.GPSDO_RUN_DIR
        harmonize.GPSDO_RUN_DIR = tmp
        self.addCleanup(lambda: setattr(harmonize, "GPSDO_RUN_DIR", original))

    def _write_report(self, tmp: Path, serial: str, governs: list) -> None:
        (tmp / f"{serial}.json").write_text(
            json.dumps({
                "schema": "v1",
                "device": {"model": "lbe-1421", "serial": serial},
                "governs": governs,
                "a_level_hint": "A1",
            })
        )

    def _tmp(self) -> Path:
        import shutil
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def test_missing_dir_is_skipped(self) -> None:
        tmp = self._tmp()
        self._redirect_run_dir(tmp / "does-not-exist")
        r = rule_gpsdo_governor_coverage(_make_view(Coordination()))
        self.assertEqual(r.severity, "pass")
        self.assertIn("skipped", r.message)

    def test_empty_dir_is_skipped(self) -> None:
        tmp = self._tmp()
        self._redirect_run_dir(tmp)
        coord = Coordination(radiods={
            "main": Radiod(id="main", host="localhost"),
        })
        r = rule_gpsdo_governor_coverage(_make_view(coord))
        self.assertEqual(r.severity, "pass")
        self.assertIn("no gpsdo-monitor reports", r.message)

    def test_index_json_alone_is_skipped(self) -> None:
        tmp = self._tmp()
        (tmp / "index.json").write_text('{"schema": "v1"}')
        self._redirect_run_dir(tmp)
        r = rule_gpsdo_governor_coverage(_make_view(Coordination(radiods={
            "main": Radiod(id="main", host="localhost"),
        })))
        self.assertEqual(r.severity, "pass")

    def test_single_governor_passes(self) -> None:
        tmp = self._tmp()
        self._write_report(tmp, "LBE-A", ["radiod:main"])
        self._redirect_run_dir(tmp)
        coord = Coordination(radiods={
            "main": Radiod(id="main", host="localhost"),
        })
        r = rule_gpsdo_governor_coverage(_make_view(coord))
        self.assertEqual(r.severity, "pass")
        self.assertIn("one governor", r.message)

    def test_missing_governor_warns(self) -> None:
        tmp = self._tmp()
        # Report exists but doesn't govern this radiod.
        self._write_report(tmp, "LBE-A", ["radiod:other"])
        self._redirect_run_dir(tmp)
        coord = Coordination(radiods={
            "main": Radiod(id="main", host="localhost"),
        })
        r = rule_gpsdo_governor_coverage(_make_view(coord))
        self.assertEqual(r.severity, "warn")
        self.assertIn("main", r.message)
        self.assertIn("main", r.affected)

    def test_multiple_governors_fail(self) -> None:
        tmp = self._tmp()
        self._write_report(tmp, "LBE-A", ["radiod:main"])
        self._write_report(tmp, "LBE-B", ["radiod:main"])
        self._redirect_run_dir(tmp)
        coord = Coordination(radiods={
            "main": Radiod(id="main", host="localhost"),
        })
        r = rule_gpsdo_governor_coverage(_make_view(coord))
        self.assertEqual(r.severity, "fail")
        self.assertIn("2 governors", r.message)
        self.assertIn("LBE-A", r.message)
        self.assertIn("LBE-B", r.message)

    def test_multi_radiod_mixed_verdict_is_worst_severity(self) -> None:
        tmp = self._tmp()
        # main has two governors (fail), aux has none (warn) — fail wins.
        self._write_report(tmp, "A", ["radiod:main"])
        self._write_report(tmp, "B", ["radiod:main"])
        self._redirect_run_dir(tmp)
        coord = Coordination(radiods={
            "main": Radiod(id="main", host="localhost"),
            "aux":  Radiod(id="aux",  host="localhost"),
        })
        r = rule_gpsdo_governor_coverage(_make_view(coord))
        self.assertEqual(r.severity, "fail")
        self.assertIn("main", r.affected)
        self.assertIn("aux", r.affected)

    def test_remote_radiod_is_ignored(self) -> None:
        tmp = self._tmp()
        # gpsdo-monitor on THIS host has nothing to do with a remote radiod.
        self._redirect_run_dir(tmp)
        self._write_report(tmp, "A", ["radiod:main"])
        coord = Coordination(radiods={
            "main":   Radiod(id="main",   host="localhost"),
            "remote": Radiod(id="remote", host="other.local"),
        })
        r = rule_gpsdo_governor_coverage(_make_view(coord))
        # "main" covered, "remote" outside our scope → pass.
        self.assertEqual(r.severity, "pass")

    def test_no_local_radiod_is_skipped(self) -> None:
        tmp = self._tmp()
        self._write_report(tmp, "A", ["radiod:somewhere"])
        self._redirect_run_dir(tmp)
        coord = Coordination(radiods={
            "remote": Radiod(id="remote", host="other.local"),
        })
        r = rule_gpsdo_governor_coverage(_make_view(coord))
        self.assertEqual(r.severity, "pass")
        self.assertIn("no local radiod", r.message)

    def test_malformed_json_is_tolerated(self) -> None:
        tmp = self._tmp()
        (tmp / "bad.json").write_text("{ not valid json")
        self._write_report(tmp, "good", ["radiod:main"])
        self._redirect_run_dir(tmp)
        coord = Coordination(radiods={
            "main": Radiod(id="main", host="localhost"),
        })
        r = rule_gpsdo_governor_coverage(_make_view(coord))
        # Malformed file ignored; the good report still counts.
        self.assertEqual(r.severity, "pass")

    def test_wrong_schema_is_ignored(self) -> None:
        tmp = self._tmp()
        (tmp / "v2.json").write_text(json.dumps({
            "schema": "v2", "governs": ["radiod:main"],
        }))
        self._redirect_run_dir(tmp)
        coord = Coordination(radiods={
            "main": Radiod(id="main", host="localhost"),
        })
        r = rule_gpsdo_governor_coverage(_make_view(coord))
        # Treated as no governor → warn.
        self.assertEqual(r.severity, "warn")


if __name__ == "__main__":
    unittest.main()
