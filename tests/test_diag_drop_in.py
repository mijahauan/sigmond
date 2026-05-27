"""Tests for sigmond.diag_drop_in — `smd diag drop-in <client>` checks.

The check functions are pure (no global state), so each can be
exercised independently with controlled inputs.  Integration tests
build a fake-client tmpdir tree and monkeypatch the subprocess /
catalog / topology entry points.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

_LIB = Path(__file__).resolve().parent.parent / 'lib'
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from sigmond import diag_drop_in as ddi  # noqa: E402


def _make_proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Per-check tests — small, isolated, no subprocess unless we patch it.
# ---------------------------------------------------------------------------


class RepoPresentTests(unittest.TestCase):

    def test_ok_when_directory_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "foo").mkdir()
            c = ddi._check_repo_present("foo", root / "foo")
            self.assertEqual(c.status, "ok")

    def test_fail_when_directory_missing(self):
        with tempfile.TemporaryDirectory() as td:
            c = ddi._check_repo_present("foo", Path(td) / "foo")
            self.assertEqual(c.status, "fail")
            self.assertIn("no such directory", c.detail)
            self.assertIn("git clone", c.remedy)


class DeployTomlTests(unittest.TestCase):

    def test_returns_parsed_dict_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "deploy.toml").write_text(textwrap.dedent("""
                [package]
                name = "foo"
            """))
            check, data = ddi._check_deploy_toml(root)
            self.assertEqual(check.status, "ok")
            self.assertEqual(data["package"]["name"], "foo")

    def test_fail_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            check, data = ddi._check_deploy_toml(Path(td))
            self.assertEqual(check.status, "fail")
            self.assertIsNone(data)

    def test_fail_when_malformed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "deploy.toml").write_text("this = is = bad")
            check, data = ddi._check_deploy_toml(root)
            self.assertEqual(check.status, "fail")
            self.assertIsNone(data)


class ContractVersionTests(unittest.TestCase):

    def test_ok_when_matches(self):
        c = ddi._check_contract_version(
            {"package": {"contract_version": "0.8"}}, "0.8")
        self.assertEqual(c.status, "ok")

    def test_warn_on_skew(self):
        c = ddi._check_contract_version(
            {"package": {"contract_version": "0.6"}}, "0.8")
        self.assertEqual(c.status, "warn")

    def test_fail_when_missing(self):
        c = ddi._check_contract_version({"package": {}}, "0.8")
        self.assertEqual(c.status, "fail")
        self.assertIn("contract_version", c.remedy)


class BinaryOnPathTests(unittest.TestCase):

    def test_ok_when_present(self):
        with mock.patch("sigmond.diag_drop_in.shutil.which",
                        return_value="/usr/local/bin/foo"):
            c, binary = ddi._check_binary_on_path("foo")
        self.assertEqual(c.status, "ok")
        self.assertEqual(binary, "/usr/local/bin/foo")

    def test_fail_when_missing(self):
        with mock.patch("sigmond.diag_drop_in.shutil.which",
                        return_value=None):
            c, binary = ddi._check_binary_on_path("foo")
        self.assertEqual(c.status, "fail")
        self.assertIsNone(binary)


class JsonSubcommandTests(unittest.TestCase):

    def test_parses_stdout_on_success(self):
        with mock.patch("sigmond.diag_drop_in.subprocess.run",
                        return_value=_make_proc(
                            stdout='{"version": "1.2.3"}')):
            r = ddi._run_json_subcommand("/bin/foo", "version")
        self.assertEqual(r["exit"], 0)
        self.assertEqual(r["payload"], {"version": "1.2.3"})
        self.assertFalse(r["error"])

    def test_timeout_surfaces_as_error(self):
        with mock.patch("sigmond.diag_drop_in.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(
                            cmd="x", timeout=5)):
            r = ddi._run_json_subcommand("/bin/foo", "inventory")
        self.assertIn("timed out", r["error"])
        self.assertIsNone(r["exit"])

    def test_malformed_json_surfaces_as_error(self):
        with mock.patch("sigmond.diag_drop_in.subprocess.run",
                        return_value=_make_proc(stdout='{not json')):
            r = ddi._run_json_subcommand("/bin/foo", "version")
        self.assertEqual(r["exit"], 0)
        self.assertIsNone(r["payload"])
        self.assertIn("not parseable", r["error"])


class InventoryOperatorCallableTests(unittest.TestCase):
    """The headline operator-callable rule from CLIENT-CONTRACT §3."""

    def _patch_run(self, **kw):
        return mock.patch(
            "sigmond.diag_drop_in.subprocess.run",
            return_value=_make_proc(**kw),
        )

    def test_ok_when_exit_zero_clean_payload(self):
        payload = json.dumps({"client": "x", "instances": []})
        with self._patch_run(stdout=payload):
            c = ddi._check_inventory_operator_callable("x", "/bin/x")
        self.assertEqual(c.status, "ok")

    def test_warn_when_exit_zero_but_fail_issue_present(self):
        """The degraded-inventory path: exit 0 (good — sigmond will
        mark installed=True), but client reports a fail-severity
        issue.  That's a warn, not a fail — the drop-in is intact."""
        payload = json.dumps({
            "instances": [],
            "issues": [{"severity": "fail", "message": "config not readable"}],
        })
        with self._patch_run(stdout=payload):
            c = ddi._check_inventory_operator_callable("x", "/bin/x")
        self.assertEqual(c.status, "warn")
        self.assertIn("config not readable", c.detail)

    def test_fail_when_exit_nonzero(self):
        with self._patch_run(returncode=1, stderr="boom"):
            c = ddi._check_inventory_operator_callable("x", "/bin/x")
        self.assertEqual(c.status, "fail")
        self.assertIn("MUST exit 0", c.remedy)

    def test_fail_when_no_json_on_stdout(self):
        with self._patch_run(returncode=0, stdout=""):
            c = ddi._check_inventory_operator_callable("x", "/bin/x")
        self.assertEqual(c.status, "fail")


class ClientFeaturesTests(unittest.TestCase):

    def test_info_when_block_absent(self):
        c = ddi._check_client_features({}, "watch")
        self.assertEqual(c.status, "info")

    def test_ok_when_watch_block_well_formed(self):
        deploy = {"client_features": {"watch": {
            "verb": "x", "description": "live tail of x"}}}
        c = ddi._check_client_features(deploy, "watch")
        self.assertEqual(c.status, "ok")

    def test_fail_when_description_missing(self):
        deploy = {"client_features": {"watch": {"verb": "x"}}}
        c = ddi._check_client_features(deploy, "watch")
        self.assertEqual(c.status, "fail")

    def test_verifier_kind_must_be_recognised(self):
        deploy = {"client_features": {"verifier": {
            "description": "x", "kind": "spot_queue"}}}
        self.assertEqual(
            ddi._check_client_features(deploy, "verifier").status, "ok")

        deploy["client_features"]["verifier"]["kind"] = "newfangled"
        c = ddi._check_client_features(deploy, "verifier")
        self.assertEqual(c.status, "fail")
        self.assertIn("kind", c.detail)


# ---------------------------------------------------------------------------
# Integration: run_checks() against a synthetic client tree.
# ---------------------------------------------------------------------------


class _StubEntry:
    def __init__(self, installed=True, kind="client", contract="0.7"):
        self._installed = installed
        self.kind = kind
        self.contract = contract
        self.name = "synthetic"

    def is_installed(self):
        return self._installed


class _StubAdapter:
    def __init__(self, view):
        self._view = view

    def read_view(self):
        return self._view


class RunChecksIntegrationTests(unittest.TestCase):

    def setUp(self):
        self.td_ctx = tempfile.TemporaryDirectory()
        self.root = Path(self.td_ctx.name)
        self.addCleanup(self.td_ctx.cleanup)

    def _make_synthetic_repo(self):
        repo = self.root / "synthetic"
        repo.mkdir()
        (repo / "deploy.toml").write_text(textwrap.dedent("""
            [package]
            name = "synthetic"
            contract_version = "0.7"

            [client_features.watch]
            verb = "synthetic"
            description = "live tail of synthetic detections"

            [client_features.verifier]
            verb = "synthetic"
            description = "synthetic local audit"
            kind = "local_db"
        """))
        return repo

    def test_repo_missing_short_circuits(self):
        # No filesystem changes — nothing is created in self.root.
        checks = ddi.run_checks("missing", repo_root=self.root)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, "fail")
        self.assertTrue(ddi.has_failure(checks))

    def test_full_green_path(self):
        self._make_synthetic_repo()

        from sigmond.clients.base import ClientView
        view = ClientView(client_type="synthetic", installed=True,
                          contract_version="0.7", instances=[])

        with mock.patch(
            "sigmond.diag_drop_in.shutil.which",
            return_value="/usr/local/bin/synthetic",
        ), mock.patch(
            "sigmond.diag_drop_in.subprocess.run",
            return_value=_make_proc(
                stdout=json.dumps({"version": "0.1.0", "instances": []})),
        ), mock.patch(
            "sigmond.catalog.load_catalog",
            return_value={"synthetic": _StubEntry()},
        ), mock.patch(
            "sigmond.topology.load_topology",
        ) as mt, mock.patch(
            "sigmond.clients.load_adapter",
            return_value=_StubAdapter(view),
        ):
            mt.return_value.enabled_components.return_value = ["synthetic"]
            checks = ddi.run_checks("synthetic", repo_root=self.root,
                                    supported_contract_version="0.7")

        statuses = {c.name: c.status for c in checks}
        self.assertEqual(statuses["repo present"], "ok")
        self.assertEqual(statuses["deploy.toml parseable"], "ok")
        self.assertEqual(statuses["[package].contract_version"], "ok")
        self.assertEqual(statuses["binary on PATH"], "ok")
        self.assertEqual(statuses["[client_features.watch]"], "ok")
        self.assertEqual(statuses["[client_features.verifier]"], "ok")
        self.assertEqual(statuses["catalog entry"], "ok")
        self.assertEqual(statuses["catalog.is_installed()"], "ok")
        self.assertEqual(statuses["topology enabled"], "ok")
        self.assertEqual(statuses["sigmond ContractAdapter view"], "ok")
        self.assertFalse(ddi.has_failure(checks))


# ---------------------------------------------------------------------------
# Render + has_failure plumbing.
# ---------------------------------------------------------------------------


class RenderTests(unittest.TestCase):

    def test_render_includes_one_line_per_check(self):
        checks = [
            ddi.Check("a", "ok", "all good"),
            ddi.Check("b", "fail", "broken", "fix it"),
        ]
        out = ddi.render(checks)
        self.assertIn("a", out)
        self.assertIn("b", out)
        self.assertIn("fix it", out)  # remedy line shown for failures

    def test_has_failure_detects_any_fail(self):
        self.assertFalse(ddi.has_failure([ddi.Check("a", "ok")]))
        self.assertFalse(ddi.has_failure(
            [ddi.Check("a", "ok"), ddi.Check("b", "warn")]))
        self.assertTrue(ddi.has_failure(
            [ddi.Check("a", "ok"), ddi.Check("b", "fail")]))


if __name__ == "__main__":
    unittest.main()
