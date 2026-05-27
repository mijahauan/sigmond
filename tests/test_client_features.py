"""Tests for sigmond.client_features — the drop-in seam.

A new contract-conformant client repo, with a `[client_features.watch]`
block in its deploy.toml, must surface in `load_watch_features()` with
zero edits to sigmond itself.  These tests pin that promise.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

_LIB = Path(__file__).resolve().parent.parent / 'lib'
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from sigmond.client_features import (  # noqa: E402
    WatchFeature,
    _parse_watch_feature,
    _read_deploy_toml,
    load_watch_features,
)


# ---------------------------------------------------------------------------
# _parse_watch_feature — pure dict-in, dataclass-or-None-out.
# ---------------------------------------------------------------------------

class ParseWatchFeatureTests(unittest.TestCase):

    def test_happy_path(self):
        deploy = {
            "client_features": {
                "watch": {
                    "verb": "psk",
                    "description": "PSK Reporter ft8/ft4 cycles + flushes",
                    "verbose": True,
                    "per_instance": True,
                },
            },
        }
        f = _parse_watch_feature("psk-recorder", deploy)
        self.assertEqual(
            f,
            WatchFeature(
                client="psk-recorder",
                verb="psk",
                description="PSK Reporter ft8/ft4 cycles + flushes",
                verbose=True,
                per_instance=True,
            ),
        )

    def test_verb_defaults_to_client_name(self):
        deploy = {
            "client_features": {
                "watch": {"description": "x"},
            },
        }
        f = _parse_watch_feature("hf-gps-tec", deploy)
        self.assertIsNotNone(f)
        self.assertEqual(f.verb, "hf-gps-tec")

    def test_verbose_and_per_instance_default_to_false(self):
        deploy = {
            "client_features": {
                "watch": {"description": "x"},
            },
        }
        f = _parse_watch_feature("foo", deploy)
        self.assertIsNotNone(f)
        self.assertFalse(f.verbose)
        self.assertFalse(f.per_instance)

    def test_missing_block_returns_none(self):
        self.assertIsNone(_parse_watch_feature("foo", {}))
        self.assertIsNone(_parse_watch_feature("foo", {"client_features": {}}))

    def test_block_must_be_a_table(self):
        # `watch = "psk"` instead of `[client_features.watch]` table
        deploy = {"client_features": {"watch": "psk"}}
        self.assertIsNone(_parse_watch_feature("foo", deploy))

    def test_description_is_required(self):
        # An unlabeled dropdown row is worse than a missing one — skip.
        self.assertIsNone(_parse_watch_feature(
            "foo", {"client_features": {"watch": {"verb": "x"}}}))
        self.assertIsNone(_parse_watch_feature(
            "foo", {"client_features": {"watch": {"description": ""}}}))
        self.assertIsNone(_parse_watch_feature(
            "foo", {"client_features": {"watch": {"description": "  "}}}))
        self.assertIsNone(_parse_watch_feature(
            "foo", {"client_features": {"watch": {"description": 42}}}))


# ---------------------------------------------------------------------------
# _read_deploy_toml — filesystem glue.
# ---------------------------------------------------------------------------

class ReadDeployTomlTests(unittest.TestCase):

    def test_returns_parsed_dict_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "demo").mkdir()
            (root / "demo" / "deploy.toml").write_text(textwrap.dedent("""
                [package]
                name = "demo"

                [client_features.watch]
                description = "demo"
            """))
            self.assertEqual(
                _read_deploy_toml("demo", root)["package"]["name"], "demo")

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_read_deploy_toml("nope", Path(td)))

    def test_malformed_toml_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "broken").mkdir()
            (root / "broken" / "deploy.toml").write_text("this = is = not = toml")
            self.assertIsNone(_read_deploy_toml("broken", root))


# ---------------------------------------------------------------------------
# load_watch_features — integration with topology + catalog.
# ---------------------------------------------------------------------------

@dataclass
class _StubEntry:
    installed: bool

    def is_installed(self) -> bool:
        return self.installed


class _StubTopology:
    def __init__(self, enabled):
        self._enabled = enabled

    def enabled_components(self):
        return self._enabled


def _write_deploy(root: Path, client: str, body: str) -> None:
    (root / client).mkdir()
    (root / client / "deploy.toml").write_text(textwrap.dedent(body))


class LoadWatchFeaturesTests(unittest.TestCase):

    def setUp(self):
        self.td_ctx = tempfile.TemporaryDirectory()
        self.root = Path(self.td_ctx.name)
        self.addCleanup(self.td_ctx.cleanup)

    def _run(self, enabled, catalog):
        with mock.patch(
            "sigmond.client_features.load_topology",
            return_value=_StubTopology(enabled),
        ), mock.patch(
            "sigmond.client_features.load_catalog",
            return_value=catalog,
        ):
            return load_watch_features(repo_root=self.root)

    def test_picks_up_drop_in_client(self):
        """The headline promise: drop a repo with [client_features.watch],
        enable it in topology, see it in the result.  Zero sigmond edits."""
        _write_deploy(self.root, "newthing", """
            [client_features.watch]
            verb = "newthing"
            description = "live tail of newthing detections"
            verbose = true
            per_instance = true
        """)
        out = self._run(
            enabled=["newthing"],
            catalog={"newthing": _StubEntry(installed=True)},
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].verb, "newthing")
        self.assertTrue(out[0].per_instance)

    def test_skips_disabled_client(self):
        _write_deploy(self.root, "off", """
            [client_features.watch]
            description = "off"
        """)
        out = self._run(
            enabled=[],
            catalog={"off": _StubEntry(installed=True)},
        )
        self.assertEqual(out, [])

    def test_skips_uninstalled_client(self):
        _write_deploy(self.root, "ghost", """
            [client_features.watch]
            description = "ghost"
        """)
        out = self._run(
            enabled=["ghost"],
            catalog={"ghost": _StubEntry(installed=False)},
        )
        self.assertEqual(out, [])

    def test_skips_client_without_watch_block(self):
        _write_deploy(self.root, "silent", """
            [package]
            name = "silent"
        """)
        out = self._run(
            enabled=["silent"],
            catalog={"silent": _StubEntry(installed=True)},
        )
        self.assertEqual(out, [])

    def test_skips_client_with_missing_deploy_toml(self):
        # Client enabled+installed in catalog but its repo isn't under
        # our (test) repo_root.  Drop-in needs to be best-effort: a
        # half-installed client must not crash the loader.
        out = self._run(
            enabled=["missing"],
            catalog={"missing": _StubEntry(installed=True)},
        )
        self.assertEqual(out, [])

    def test_preserves_topology_order(self):
        # Two clients, both ready — result is in topology-enabled order.
        _write_deploy(self.root, "alpha", """
            [client_features.watch]
            description = "alpha"
        """)
        _write_deploy(self.root, "bravo", """
            [client_features.watch]
            description = "bravo"
        """)
        out = self._run(
            enabled=["bravo", "alpha"],          # bravo first in topology
            catalog={"bravo": _StubEntry(installed=True),
                     "alpha": _StubEntry(installed=True)},
        )
        self.assertEqual([f.verb for f in out], ["bravo", "alpha"])


if __name__ == "__main__":
    unittest.main()
