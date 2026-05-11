"""Tests for sigmond.storage_migrate (ClickHouse → SQLite cleanup)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.storage_migrate import (
    BACKUP_SUFFIX, CH_DATA_DIRS, CH_PACKAGES, CH_SIGMOND_UNIT_FILE,
    CH_SIGMOND_VENV, DEFAULT_COORD_ENV, INSERTED_HEADER, NEUTRALIZED_PREFIX,
    SINK_DB_PATH, SINK_DIR, SINK_DIR_MODE, SINK_GROUP,
    SYSTEMD_DROPIN_BASENAME, SYSTEMD_DROPIN_CONTENT,
    HostProbe, NotConfirmed, RemovalPlan, Runner,
    _build_env_transform, _neutralize_clickhouse_lines,
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
        env_files=None,
        consumer_units=None,
        unit_users=None,
        existing_groups=(),
        user_group_membership=None,
        dirs=None,
        sandboxed_units=(),
    ):
        self._services = set(services)
        self._active = set(active_services)
        self._packages = set(packages)
        self._paths = set(paths)
        # env_files: dict mapping path -> file contents (None for "missing")
        self._env_files = env_files or {}
        # consumer_units: dict mapping env_path -> list of units
        self._consumer_units = consumer_units or {}
        # unit_users: dict mapping unit name -> running user
        self._unit_users = unit_users or {}
        # existing_groups: which groups exist on the "host"
        self._groups = set(existing_groups)
        # user_group_membership: dict mapping user -> list of groups
        self._user_groups = user_group_membership or {}
        # dirs: dict mapping path -> (mode, group_name); missing path = None
        self._dirs = dirs or {}
        # sandboxed_units: set of units whose ProtectSystem=strict
        # blocks the sink dir.  Membership = needs a drop-in.
        self._sandboxed_units = set(sandboxed_units)

    def service_exists(self, unit: str) -> bool:
        return unit in self._services

    def service_active(self, unit: str) -> bool:
        return unit in self._active

    def package_installed(self, pkg: str) -> bool:
        return pkg in self._packages

    def path_exists(self, path: str) -> bool:
        return path in self._paths

    def read_text(self, path):
        return self._env_files.get(path)

    def find_units_using_env_file(self, env_path):
        return list(self._consumer_units.get(env_path, []))

    def unit_user(self, unit):
        return self._unit_users.get(unit)

    def group_exists(self, group):
        return group in self._groups

    def user_groups(self, user):
        return list(self._user_groups.get(user, []))

    def dir_meta(self, path):
        return self._dirs.get(path)

    def unit_sandbox_blocks_sink(self, unit, sink_dir):
        return unit in self._sandboxed_units


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

    def __init__(self, command_returncodes=None, rewrite_should_raise=False):
        self.command_returncodes = command_returncodes or {}
        self.run_calls = []
        self.rmtree_calls = []
        self.unlink_calls = []
        self.rewrite_calls = []     # list of (path, transform_result)
        self.write_text_calls = []  # list of (path, content)
        self.rewrite_should_raise = rewrite_should_raise

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

    def rewrite_file(self, path, transform):
        if self.rewrite_should_raise:
            raise PermissionError(f"simulated EACCES on {path}")
        # Fake the read by handing transform an empty string — tests
        # that care about the transform itself test it directly via
        # _neutralize_clickhouse_lines.
        result = transform("")
        self.rewrite_calls.append((path, result))
        return path + BACKUP_SUFFIX

    def write_text(self, path, content):
        self.write_text_calls.append((path, content))


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


class TestEnvNeutralizationTransform(unittest.TestCase):
    """Direct tests for _neutralize_clickhouse_lines (the in-memory transform)."""

    def test_comments_out_live_clickhouse_lines(self):
        text = (
            "SIGMOND_CLICKHOUSE_URL=http://localhost:8123\n"
            "SIGMOND_CLICKHOUSE_USER=sigmond\n"
            "OTHER_VAR=keep_me\n"
        )
        out = _neutralize_clickhouse_lines(text)
        self.assertIn(
            f"{NEUTRALIZED_PREFIX}SIGMOND_CLICKHOUSE_URL=http://localhost:8123",
            out,
        )
        self.assertIn(
            f"{NEUTRALIZED_PREFIX}SIGMOND_CLICKHOUSE_USER=sigmond",
            out,
        )
        # Non-CH lines untouched.
        self.assertIn("OTHER_VAR=keep_me", out)

    def test_idempotent_on_already_commented_lines(self):
        # Re-running the verb should be a no-op on the env file.
        original = (
            f"{NEUTRALIZED_PREFIX}SIGMOND_CLICKHOUSE_URL=http://localhost:8123\n"
            "OTHER_VAR=value\n"
        )
        self.assertEqual(_neutralize_clickhouse_lines(original), original)

    def test_preserves_trailing_newline(self):
        with_nl = "SIGMOND_CLICKHOUSE_URL=x\n"
        without_nl = "SIGMOND_CLICKHOUSE_URL=x"
        self.assertTrue(_neutralize_clickhouse_lines(with_nl).endswith("\n"))
        self.assertFalse(_neutralize_clickhouse_lines(without_nl).endswith("\n"))

    def test_only_matches_clickhouse_prefix(self):
        # SIGMOND_SQLITE_* and other vars must NOT be commented out.
        text = (
            "SIGMOND_SQLITE_PATH=/var/lib/sigmond/sink.db\n"
            "SIGMOND_SOMETHING_ELSE=ok\n"
        )
        out = _neutralize_clickhouse_lines(text)
        self.assertEqual(out, text)


class TestPlanIncludesEnvAndConsumers(unittest.TestCase):

    def test_plan_picks_up_env_lines_and_consumers(self):
        env_text = (
            "SIGMOND_CLICKHOUSE_URL=http://localhost:8123\n"
            "SIGMOND_CLICKHOUSE_USER=sigmond\n"
            "OTHER_VAR=keep\n"
        )
        probe = FakeProbe(
            services=("clickhouse-server.service",),
            packages=("clickhouse-server",),
            paths=("/var/lib/clickhouse",),
            env_files={DEFAULT_COORD_ENV: env_text},
            consumer_units={DEFAULT_COORD_ENV: [
                "psk-recorder@my-rx888.service",
                "hf-timestd.service",
            ]},
        )
        plan = plan_clickhouse_removal(probe=probe)

        self.assertEqual(len(plan.env_lines_to_neutralize), 2)
        paths = {p for p, _l in plan.env_lines_to_neutralize}
        self.assertEqual(paths, {DEFAULT_COORD_ENV})
        # Already-commented or non-CH lines NOT in the plan.
        for _path, line in plan.env_lines_to_neutralize:
            self.assertTrue(line.startswith("SIGMOND_CLICKHOUSE_"))

        self.assertEqual(
            set(plan.consumers_to_restart),
            {"psk-recorder@my-rx888.service", "hf-timestd.service"},
        )

    def test_plan_omits_consumers_when_no_env_lines(self):
        # If coordination.env has no CH vars, there's no reason to
        # bounce the producers — they're already configured correctly.
        probe = FakeProbe(
            env_files={DEFAULT_COORD_ENV: "OTHER_VAR=keep\n"},
            consumer_units={DEFAULT_COORD_ENV: ["psk-recorder@a.service"]},
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.env_lines_to_neutralize, [])
        self.assertEqual(plan.consumers_to_restart, [])
        self.assertTrue(plan.is_empty)

    def test_plan_handles_missing_env_file(self):
        # Standalone host with no /etc/sigmond/coordination.env.
        probe = FakeProbe(env_files={DEFAULT_COORD_ENV: None})
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.env_lines_to_neutralize, [])
        self.assertEqual(plan.consumers_to_restart, [])


class TestExecuteRemovalOrdering(unittest.TestCase):

    def _full_plan_with_env(self) -> RemovalPlan:
        return RemovalPlan(
            services_to_stop=["clickhouse-server.service"],
            services_to_disable=["clickhouse-server.service"],
            packages_to_purge=["clickhouse-server"],
            paths_to_remove=["/var/lib/clickhouse"],
            files_to_remove=[],
            env_lines_to_neutralize=[
                (DEFAULT_COORD_ENV, "SIGMOND_CLICKHOUSE_URL=x"),
                (DEFAULT_COORD_ENV, "SIGMOND_CLICKHOUSE_USER=y"),
            ],
            consumers_to_restart=[
                "psk-recorder@my-rx888.service",
                "hf-timestd.service",
            ],
            confirmed=True,
        )

    def test_env_rewrite_happens_before_consumer_restart(self):
        plan = self._full_plan_with_env()
        runner = FakeRunner()
        execute_removal(plan, runner=runner)

        # The single rewrite call must come before the first systemctl
        # restart in the sequence of side effects.
        self.assertEqual(len(runner.rewrite_calls), 1)
        rewrite_path = runner.rewrite_calls[0][0]
        self.assertEqual(rewrite_path, DEFAULT_COORD_ENV)

        # All systemctl restarts must precede the first stop.
        verbs = [argv[1] for argv in runner.run_calls if argv[0] == "systemctl"]
        first_restart = verbs.index("restart")
        first_stop = verbs.index("stop")
        self.assertLess(first_restart, first_stop,
                        "consumer restarts must precede CH stop")

    def test_consumer_restart_invokes_each_unit(self):
        plan = self._full_plan_with_env()
        runner = FakeRunner()
        report = execute_removal(plan, runner=runner)

        restart_argvs = [argv for argv in runner.run_calls
                         if argv[0] == "systemctl" and argv[1] == "restart"]
        restarted_units = [argv[2] for argv in restart_argvs]
        self.assertEqual(
            set(restarted_units),
            {"psk-recorder@my-rx888.service", "hf-timestd.service"},
        )
        self.assertEqual(
            set(report.consumers_restarted),
            {"psk-recorder@my-rx888.service", "hf-timestd.service"},
        )

    def test_env_rewrite_deduplicates_paths(self):
        # Two env_lines for the same path → still only one rewrite.
        plan = RemovalPlan(
            env_lines_to_neutralize=[
                (DEFAULT_COORD_ENV, "SIGMOND_CLICKHOUSE_URL=x"),
                (DEFAULT_COORD_ENV, "SIGMOND_CLICKHOUSE_USER=y"),
                (DEFAULT_COORD_ENV, "SIGMOND_CLICKHOUSE_DB_PSK=z"),
            ],
            consumers_to_restart=[],
            confirmed=True,
        )
        runner = FakeRunner()
        execute_removal(plan, runner=runner)
        self.assertEqual(len(runner.rewrite_calls), 1)

    def test_env_rewrite_failure_recorded_as_error_and_continues(self):
        plan = self._full_plan_with_env()
        runner = FakeRunner(rewrite_should_raise=True)
        report = execute_removal(plan, runner=runner)
        self.assertTrue(any("rewrite" in err for err in report.errors))
        # Despite the rewrite failure, the rest of the migration runs
        # (consumers still restarted, packages still purged).
        self.assertTrue(report.consumers_restarted)
        self.assertTrue(report.stopped)
        self.assertTrue(report.purged)


class TestPlanIncludesGroupAndPermsSetup(unittest.TestCase):
    """Plan should ensure the sigmond group, producer-user membership,
    and a writable /var/lib/sigmond — without these the producers fall
    back to silent-noop after the env neutralization step."""

    def _probe_with_pskrec_consumer(
        self,
        *,
        existing_groups=(),
        user_group_membership=None,
        dirs=None,
        env_text="SIGMOND_CLICKHOUSE_URL=http://localhost:8123\n",
    ):
        return FakeProbe(
            env_files={DEFAULT_COORD_ENV: env_text},
            consumer_units={DEFAULT_COORD_ENV: [
                "psk-recorder@my-rx888.service",
            ]},
            unit_users={"psk-recorder@my-rx888.service": "pskrec"},
            existing_groups=existing_groups,
            user_group_membership=user_group_membership or {},
            dirs=dirs or {},
        )

    def test_plan_queues_group_creation_when_missing(self):
        probe = self._probe_with_pskrec_consumer(existing_groups=())
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.group_to_create, SINK_GROUP)

    def test_plan_skips_group_creation_when_present(self):
        probe = self._probe_with_pskrec_consumer(
            existing_groups=(SINK_GROUP,),
            user_group_membership={"pskrec": ["pskrec"]},
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertIsNone(plan.group_to_create)

    def test_plan_adds_producer_user_to_group_if_missing(self):
        probe = self._probe_with_pskrec_consumer(
            existing_groups=(SINK_GROUP,),
            user_group_membership={"pskrec": ["pskrec"]},
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.users_to_add_to_group, [("pskrec", SINK_GROUP)])

    def test_plan_skips_user_add_when_already_member(self):
        probe = self._probe_with_pskrec_consumer(
            existing_groups=(SINK_GROUP,),
            user_group_membership={"pskrec": ["pskrec", SINK_GROUP]},
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.users_to_add_to_group, [])

    def test_plan_queues_sink_dir_setup_when_missing(self):
        probe = self._probe_with_pskrec_consumer(
            existing_groups=(SINK_GROUP,),
            user_group_membership={"pskrec": [SINK_GROUP]},
            dirs={},  # SINK_DIR doesn't exist
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(
            plan.sink_dir_to_setup,
            (SINK_DIR, SINK_GROUP, SINK_DIR_MODE),
        )

    def test_plan_queues_sink_dir_setup_when_wrong_perms(self):
        probe = self._probe_with_pskrec_consumer(
            existing_groups=(SINK_GROUP,),
            user_group_membership={"pskrec": [SINK_GROUP]},
            dirs={SINK_DIR: (0o750, "root")},  # wrong group, wrong mode
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(
            plan.sink_dir_to_setup,
            (SINK_DIR, SINK_GROUP, SINK_DIR_MODE),
        )

    def test_plan_skips_sink_dir_setup_when_already_correct(self):
        probe = self._probe_with_pskrec_consumer(
            existing_groups=(SINK_GROUP,),
            user_group_membership={"pskrec": [SINK_GROUP]},
            dirs={SINK_DIR: (SINK_DIR_MODE, SINK_GROUP)},
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertIsNone(plan.sink_dir_to_setup)

    def test_plan_pins_sqlite_path_when_missing_from_env(self):
        probe = self._probe_with_pskrec_consumer(
            env_text="SIGMOND_CLICKHOUSE_URL=http://x\n",
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(
            plan.env_lines_to_set,
            [(DEFAULT_COORD_ENV, f"SIGMOND_SQLITE_PATH={SINK_DB_PATH}")],
        )

    def test_plan_does_not_re_pin_sqlite_path_when_already_set(self):
        probe = self._probe_with_pskrec_consumer(
            env_text=(
                "SIGMOND_CLICKHOUSE_URL=http://x\n"
                "SIGMOND_SQLITE_PATH=/operator/override/sink.db\n"
            ),
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.env_lines_to_set, [])

    def test_plan_with_root_unit_does_not_add_to_group(self):
        # Units running as root don't need group membership.
        probe = FakeProbe(
            env_files={DEFAULT_COORD_ENV: "SIGMOND_CLICKHOUSE_URL=x\n"},
            consumer_units={DEFAULT_COORD_ENV: ["some-root-svc.service"]},
            unit_users={"some-root-svc.service": "root"},
            existing_groups=(),
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertIsNone(plan.group_to_create)
        self.assertEqual(plan.users_to_add_to_group, [])

    def test_plan_no_consumers_no_group_or_perm_work(self):
        # Fresh host with no producers active → no group/perms churn.
        probe = FakeProbe(
            env_files={DEFAULT_COORD_ENV: "SIGMOND_CLICKHOUSE_URL=x\n"},
            consumer_units={DEFAULT_COORD_ENV: []},
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertIsNone(plan.group_to_create)
        self.assertEqual(plan.users_to_add_to_group, [])
        self.assertIsNone(plan.sink_dir_to_setup)
        self.assertEqual(plan.env_lines_to_set, [])  # no consumers ⇒ no pin


class TestEnvTransformWithAppends(unittest.TestCase):
    """`_build_env_transform` neutralizes CH lines AND appends new vars."""

    def test_appends_sqlite_path_when_missing(self):
        text = "SIGMOND_CLICKHOUSE_URL=http://x\n"
        transform = _build_env_transform(
            append_lines=[f"SIGMOND_SQLITE_PATH={SINK_DB_PATH}"],
        )
        out = transform(text)
        self.assertIn(INSERTED_HEADER, out)
        self.assertIn(f"SIGMOND_SQLITE_PATH={SINK_DB_PATH}", out)
        # CH line still gets neutralized.
        self.assertIn(f"{NEUTRALIZED_PREFIX}SIGMOND_CLICKHOUSE_URL=", out)

    def test_does_not_append_when_key_already_present(self):
        text = (
            "SIGMOND_CLICKHOUSE_URL=http://x\n"
            "SIGMOND_SQLITE_PATH=/operator/override/sink.db\n"
        )
        transform = _build_env_transform(
            append_lines=[f"SIGMOND_SQLITE_PATH={SINK_DB_PATH}"],
        )
        out = transform(text)
        # The verb's default path must NOT appear; operator's override
        # is preserved.
        self.assertNotIn(SINK_DB_PATH, out)
        self.assertIn("/operator/override/sink.db", out)
        # Header also doesn't appear since we appended nothing.
        self.assertNotIn(INSERTED_HEADER, out)

    def test_empty_appends_just_neutralizes(self):
        text = "SIGMOND_CLICKHOUSE_URL=x\nOTHER=y\n"
        transform = _build_env_transform(append_lines=[])
        out = transform(text)
        self.assertIn(f"{NEUTRALIZED_PREFIX}SIGMOND_CLICKHOUSE_URL=x", out)
        self.assertIn("OTHER=y", out)
        self.assertNotIn(INSERTED_HEADER, out)


class TestExecuteGroupAndPermsOrdering(unittest.TestCase):

    def test_group_user_dir_run_before_env_rewrite_and_restart(self):
        plan = RemovalPlan(
            group_to_create=SINK_GROUP,
            users_to_add_to_group=[("pskrec", SINK_GROUP)],
            sink_dir_to_setup=(SINK_DIR, SINK_GROUP, SINK_DIR_MODE),
            env_lines_to_neutralize=[(DEFAULT_COORD_ENV, "SIGMOND_CLICKHOUSE_URL=x")],
            env_lines_to_set=[(DEFAULT_COORD_ENV, f"SIGMOND_SQLITE_PATH={SINK_DB_PATH}")],
            consumers_to_restart=["psk-recorder@my-rx888.service"],
            confirmed=True,
        )
        runner = FakeRunner()
        report = execute_removal(plan, runner=runner)

        # Order: groupadd → usermod → install -d → (rewrite) → restart
        verbs_in_order = [argv[0] for argv in runner.run_calls]
        self.assertEqual(verbs_in_order[0], "groupadd")
        self.assertEqual(verbs_in_order[1], "usermod")
        self.assertEqual(verbs_in_order[2], "install")

        # The rewrite happens between install and restart.  FakeRunner
        # records rewrite separately, so we just assert the restart
        # comes after install -d.
        first_restart_idx = next(
            i for i, argv in enumerate(runner.run_calls)
            if argv[0] == "systemctl" and argv[1] == "restart"
        )
        install_idx = verbs_in_order.index("install")
        self.assertLess(install_idx, first_restart_idx)
        self.assertEqual(len(runner.rewrite_calls), 1)

        # Report mirrors the side effects.
        self.assertEqual(report.group_created, SINK_GROUP)
        self.assertEqual(report.users_added_to_group, [("pskrec", SINK_GROUP)])
        self.assertEqual(report.sink_dir_configured, SINK_DIR)
        self.assertEqual(
            report.env_lines_appended,
            [(DEFAULT_COORD_ENV, f"SIGMOND_SQLITE_PATH={SINK_DB_PATH}")],
        )

    def test_groupadd_rc9_is_idempotent_not_error(self):
        plan = RemovalPlan(
            group_to_create=SINK_GROUP, confirmed=True,
        )
        runner = FakeRunner(command_returncodes={("groupadd", "--system"): 9})
        report = execute_removal(plan, runner=runner)
        # Even though groupadd returned 9 (exists), report.errors stays
        # clean and report.group_created stays None (we didn't create it).
        self.assertEqual(report.errors, [])
        self.assertIsNone(report.group_created)

    def test_install_d_failure_recorded_as_error(self):
        plan = RemovalPlan(
            sink_dir_to_setup=(SINK_DIR, SINK_GROUP, SINK_DIR_MODE),
            confirmed=True,
        )
        runner = FakeRunner(command_returncodes={("install", "-d"): 1})
        report = execute_removal(plan, runner=runner)
        self.assertTrue(any("install -d" in err for err in report.errors))
        self.assertIsNone(report.sink_dir_configured)


class TestPlanQueuesSandboxDropins(unittest.TestCase):
    """Producer units with ProtectSystem=strict need a drop-in adding
    /var/lib/sigmond to ReadWritePaths — without it the sink dir is
    read-only inside the unit's namespace regardless of POSIX perms."""

    def _probe(self, *, sandboxed_units=(), existing_paths=()):
        return FakeProbe(
            env_files={DEFAULT_COORD_ENV: "SIGMOND_CLICKHOUSE_URL=x\n"},
            consumer_units={DEFAULT_COORD_ENV: [
                "psk-recorder@my-rx888.service",
                "hf-timestd.service",
            ]},
            unit_users={
                "psk-recorder@my-rx888.service": "pskrec",
                "hf-timestd.service": "hf-timestd",
            },
            existing_groups=(SINK_GROUP,),
            user_group_membership={
                "pskrec": [SINK_GROUP], "hf-timestd": [SINK_GROUP],
            },
            dirs={SINK_DIR: (SINK_DIR_MODE, SINK_GROUP)},
            sandboxed_units=sandboxed_units,
            paths=tuple(existing_paths),
        )

    def test_plan_queues_dropin_for_sandboxed_unit(self):
        probe = self._probe(sandboxed_units={
            "psk-recorder@my-rx888.service",
        })
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(len(plan.sandbox_dropins_to_write), 1)
        path, unit = plan.sandbox_dropins_to_write[0]
        self.assertEqual(unit, "psk-recorder@my-rx888.service")
        self.assertEqual(
            path,
            f"/etc/systemd/system/{unit}.d/{SYSTEMD_DROPIN_BASENAME}",
        )

    def test_plan_skips_dropin_for_non_sandboxed_unit(self):
        probe = self._probe(sandboxed_units=())
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.sandbox_dropins_to_write, [])

    def test_plan_idempotent_when_dropin_already_exists(self):
        # Existing drop-in at the expected path → no new write queued.
        dropin = (
            "/etc/systemd/system/psk-recorder@my-rx888.service.d/"
            + SYSTEMD_DROPIN_BASENAME
        )
        probe = self._probe(
            sandboxed_units={"psk-recorder@my-rx888.service"},
            existing_paths=(dropin,),
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.sandbox_dropins_to_write, [])

    def test_plan_dropin_triggers_consumer_restart(self):
        # Even without env / group / perm work, a sandbox drop-in
        # requires the unit to be restarted to remap its namespace.
        probe = FakeProbe(
            env_files={DEFAULT_COORD_ENV: ""},  # no CH env at all
            consumer_units={DEFAULT_COORD_ENV: ["psk-recorder@a.service"]},
            unit_users={"psk-recorder@a.service": "pskrec"},
            existing_groups=(SINK_GROUP,),
            user_group_membership={"pskrec": [SINK_GROUP]},
            dirs={SINK_DIR: (SINK_DIR_MODE, SINK_GROUP)},
            sandboxed_units={"psk-recorder@a.service"},
        )
        plan = plan_clickhouse_removal(probe=probe)
        self.assertEqual(plan.consumers_to_restart, ["psk-recorder@a.service"])


class TestExecuteSandboxDropins(unittest.TestCase):

    def test_dropins_written_with_canonical_content(self):
        plan = RemovalPlan(
            sandbox_dropins_to_write=[
                ("/etc/systemd/system/psk-recorder@a.service.d/"
                 + SYSTEMD_DROPIN_BASENAME,
                 "psk-recorder@a.service"),
            ],
            consumers_to_restart=["psk-recorder@a.service"],
            confirmed=True,
        )
        runner = FakeRunner()
        report = execute_removal(plan, runner=runner)
        self.assertEqual(len(runner.write_text_calls), 1)
        path, content = runner.write_text_calls[0]
        self.assertIn("ReadWritePaths=/var/lib/sigmond", content)
        self.assertEqual(content, SYSTEMD_DROPIN_CONTENT)
        self.assertIn(path, report.sandbox_dropins_written)

    def test_daemon_reload_run_after_dropin_writes(self):
        plan = RemovalPlan(
            sandbox_dropins_to_write=[
                ("/etc/systemd/system/a.service.d/" + SYSTEMD_DROPIN_BASENAME,
                 "a.service"),
            ],
            consumers_to_restart=["a.service"],
            confirmed=True,
        )
        runner = FakeRunner()
        execute_removal(plan, runner=runner)
        # daemon-reload must come AFTER write_text and BEFORE restart.
        verbs = [argv for argv in runner.run_calls if argv[0] == "systemctl"]
        reload_idx = next(i for i, v in enumerate(verbs)
                          if v[1] == "daemon-reload")
        restart_idx = next(i for i, v in enumerate(verbs)
                           if v[1] == "restart")
        self.assertLess(reload_idx, restart_idx)


class TestRunnerRewriteFile(unittest.TestCase):
    """Direct test of Runner.rewrite_file against the real filesystem."""

    def test_rewrite_writes_backup_and_applies_transform(self):
        original = (
            "SIGMOND_CLICKHOUSE_URL=http://localhost:8123\n"
            "OTHER_VAR=keep\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env",
                                         delete=False) as f:
            f.write(original)
            path = f.name
        try:
            runner = Runner()
            backup = runner.rewrite_file(path, _neutralize_clickhouse_lines)

            self.assertEqual(backup, path + BACKUP_SUFFIX)
            # Original preserved in backup.
            self.assertEqual(Path(backup).read_text(), original)
            # Transform applied to live file.
            new_text = Path(path).read_text()
            self.assertIn(NEUTRALIZED_PREFIX, new_text)
            self.assertIn("OTHER_VAR=keep", new_text)
        finally:
            Path(path).unlink(missing_ok=True)
            Path(path + BACKUP_SUFFIX).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
