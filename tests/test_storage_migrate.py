"""Tests for sigmond.storage_migrate (ClickHouse → SQLite cleanup)."""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.storage_migrate import (
    CH_DATA_DIRS, CH_PACKAGES, CH_SIGMOND_UNIT_FILE, CH_SIGMOND_VENV,
    HostProbe, NotConfirmed, RemovalPlan, Runner,
    execute_removal, plan_clickhouse_removal,
)


class FakeProbe(HostProbe):
    """Lets a test declare exactly which artifacts exist on the 'host'."""

    def __init__(
        self,
        services=(),
        active_services=(),
        packages=(),
        paths=(),
    ):
        self._services = set(services)
        self._active = set(active_services)
        self._packages = set(packages)
        self._paths = set(paths)

    def service_exists(self, unit: str) -> bool:
        return unit in self._services

    def service_active(self, unit: str) -> bool:
        return unit in self._active

    def package_installed(self, pkg: str) -> bool:
        return pkg in self._packages

    def path_exists(self, path: str) -> bool:
        return path in self._paths


class TestPlanBuilding(unittest.TestCase):

    def test_empty_host_yields_empty_plan(self):
        plan = plan_clickhouse_removal(probe=FakeProbe())
        self.assertTrue(plan.is_empty)

    def test_active_clickhouse_install_full_plan(self):
        probe = FakeProbe(
            services=(
                "sigmond-clickhouse.service",
                "clickhouse-server.service",
            ),
            active_services=("clickhouse-server.service",),
            packages=("clickhouse-server", "clickhouse-client"),
            paths=(
                "/var/lib/clickhouse",
                "/etc/clickhouse-server",
                CH_SIGMOND_VENV,
                CH_SIGMOND_UNIT_FILE,
            ),
        )
        plan = plan_clickhouse_removal(probe=probe)

        self.assertFalse(plan.is_empty)
        # Both units flagged for disable; only the active one for stop.
        self.assertEqual(
            set(plan.services_to_disable),
            {"sigmond-clickhouse.service", "clickhouse-server.service"},
        )
        self.assertEqual(
            plan.services_to_stop, ["clickhouse-server.service"],
        )
        self.assertEqual(
            set(plan.packages_to_purge),
            {"clickhouse-server", "clickhouse-client"},
        )
        # Data dir, config dir, venv all in paths_to_remove.
        self.assertIn("/var/lib/clickhouse", plan.paths_to_remove)
        self.assertIn("/etc/clickhouse-server", plan.paths_to_remove)
        self.assertIn(CH_SIGMOND_VENV, plan.paths_to_remove)
        # Unit file in files_to_remove (not a directory).
        self.assertIn(CH_SIGMOND_UNIT_FILE, plan.files_to_remove)

    def test_partial_install_only_lists_what_exists(self):
        # Common state on a host where the package was installed but
        # sigmond-clickhouse was never set up.
        probe = FakeProbe(
            services=("clickhouse-server.service",),
            packages=("clickhouse-server",),
            paths=("/var/lib/clickhouse",),
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.services_to_disable, ["clickhouse-server.service"])
        self.assertNotIn("sigmond-clickhouse.service", plan.services_to_disable)
        self.assertEqual(plan.packages_to_purge, ["clickhouse-server"])
        self.assertEqual(plan.paths_to_remove, ["/var/lib/clickhouse"])
        self.assertEqual(plan.files_to_remove, [])


class FakeRunner(Runner):
    """Records calls; never touches the real filesystem or systemd."""

    def __init__(self, command_returncodes=None):
        self.command_returncodes = command_returncodes or {}
        self.run_calls = []
        self.rmtree_calls = []
        self.unlink_calls = []

    def run(self, argv: list) -> subprocess.CompletedProcess:
        self.run_calls.append(argv)
        # Return a CompletedProcess with the rc the test asked for, or 0.
        key = tuple(argv[:2])
        rc = self.command_returncodes.get(key, 0)
        return subprocess.CompletedProcess(
            argv, returncode=rc, stdout="", stderr="",
        )

    def rmtree(self, path: str) -> None:
        self.rmtree_calls.append(path)

    def unlink(self, path: str) -> None:
        self.unlink_calls.append(path)


class TestExecuteRemoval(unittest.TestCase):

    def _full_plan(self) -> RemovalPlan:
        return RemovalPlan(
            services_to_stop=["clickhouse-server.service"],
            services_to_disable=[
                "sigmond-clickhouse.service",
                "clickhouse-server.service",
            ],
            packages_to_purge=["clickhouse-server", "clickhouse-client"],
            paths_to_remove=["/var/lib/clickhouse", CH_SIGMOND_VENV],
            files_to_remove=[CH_SIGMOND_UNIT_FILE],
            confirmed=False,
        )

    def test_refuses_without_confirmation(self):
        plan = self._full_plan()
        runner = FakeRunner()
        with self.assertRaises(NotConfirmed):
            execute_removal(plan, runner=runner)
        # No side effects attempted.
        self.assertEqual(runner.run_calls, [])
        self.assertEqual(runner.rmtree_calls, [])
        self.assertEqual(runner.unlink_calls, [])

    def test_confirmed_plan_executes_in_order(self):
        plan = self._full_plan()
        plan.confirmed = True
        runner = FakeRunner()
        report = execute_removal(plan, runner=runner)

        # Stop ran before disable, disable before purge.
        verbs = [argv[1] for argv in runner.run_calls if argv[0] == "systemctl"]
        # systemd actions: stop, then 2x disable, then daemon-reload (since
        # services_to_disable is non-empty).
        self.assertEqual(verbs[0], "stop")
        self.assertEqual(verbs[1:3], ["disable", "disable"])
        self.assertEqual(verbs[-1], "daemon-reload")

        # apt-get purge invoked with both packages.
        apt_calls = [argv for argv in runner.run_calls if argv[0] == "apt-get"]
        self.assertEqual(len(apt_calls), 1)
        self.assertIn("clickhouse-server", apt_calls[0])
        self.assertIn("clickhouse-client", apt_calls[0])

        # Directories removed.
        self.assertIn("/var/lib/clickhouse", runner.rmtree_calls)
        self.assertIn(CH_SIGMOND_VENV, runner.rmtree_calls)

        # Files unlinked.
        self.assertIn(CH_SIGMOND_UNIT_FILE, runner.unlink_calls)

        # Report mirrors what was done.
        self.assertEqual(report.stopped, ["clickhouse-server.service"])
        self.assertEqual(set(report.purged),
                         {"clickhouse-server", "clickhouse-client"})
        self.assertEqual(report.errors, [])

    def test_failed_stop_recorded_as_error(self):
        plan = self._full_plan()
        plan.confirmed = True
        runner = FakeRunner(command_returncodes={
            ("systemctl", "stop"): 5,
        })
        report = execute_removal(plan, runner=runner)
        self.assertTrue(any("stop" in err for err in report.errors))
        # But execution continues — packages still purged.
        self.assertIn("clickhouse-server", report.purged)

    def test_failed_purge_recorded_as_error(self):
        plan = self._full_plan()
        plan.confirmed = True
        runner = FakeRunner(command_returncodes={
            ("apt-get", "purge"): 100,
        })
        report = execute_removal(plan, runner=runner)
        self.assertTrue(any("apt-get purge" in err for err in report.errors))
        self.assertEqual(report.purged, [])
        # Dirs still removed (separate failure domain).
        self.assertIn("/var/lib/clickhouse", report.removed_paths)

    def test_empty_plan_executes_no_side_effects(self):
        plan = RemovalPlan(confirmed=True)
        runner = FakeRunner()
        report = execute_removal(plan, runner=runner)
        self.assertEqual(runner.run_calls, [])
        self.assertEqual(runner.rmtree_calls, [])
        self.assertEqual(runner.unlink_calls, [])
        self.assertEqual(report.errors, [])


if __name__ == "__main__":
    unittest.main()
