"""Tests for the discovery cache file: load/save_cache (existing) and
the new load_snapshot/save_snapshot helpers used by probes that need
to remember a raw counter reading across runs."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.discovery import (
    load_snapshot, save_snapshot, save_cache, load_cache,
)
from sigmond.environment import Environment, EnvironmentView, Observation


class SnapshotHelpersTests(unittest.TestCase):
    def test_load_returns_none_when_cache_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'absent.json'
            self.assertIsNone(load_snapshot('local_resources', path=p))

    def test_load_returns_none_when_source_absent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cache.json'
            p.write_text(json.dumps({"previous_snapshots": {}}))
            self.assertIsNone(load_snapshot('local_resources', path=p))

    def test_load_returns_none_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cache.json'
            p.write_text('{not valid json')
            self.assertIsNone(load_snapshot('local_resources', path=p))

    def test_save_then_load_round_trips(self):
        snap = {
            "captured_at": 1234567890.0,
            "udp": {"RcvbufErrors": 17, "InErrors": 0},
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cache.json'
            save_snapshot('local_resources', snap, path=p)
            got = load_snapshot('local_resources', path=p)
        self.assertEqual(got, snap)

    def test_save_preserves_other_sources(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cache.json'
            save_snapshot('source_a', {"a": 1}, path=p)
            save_snapshot('source_b', {"b": 2}, path=p)
            self.assertEqual(load_snapshot('source_a', path=p), {"a": 1})
            self.assertEqual(load_snapshot('source_b', path=p), {"b": 2})

    def test_save_overwrites_same_source(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cache.json'
            save_snapshot('local_resources', {"v": 1}, path=p)
            save_snapshot('local_resources', {"v": 2}, path=p)
            self.assertEqual(load_snapshot('local_resources', path=p),
                             {"v": 2})


class SaveCachePreservesSnapshotsTests(unittest.TestCase):
    """Regression: a plain save_cache(view) call must not wipe
    previous_snapshots written by probes."""

    def _empty_view(self) -> EnvironmentView:
        return EnvironmentView(env=Environment(), observations=[],
                               deltas=[], probed_at=99.0)

    def test_save_cache_does_not_clobber_snapshots(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cache.json'
            save_snapshot('local_resources', {"v": 1}, path=p)
            save_cache(self._empty_view(), path=p)
            self.assertEqual(load_snapshot('local_resources', path=p),
                             {"v": 1})
            # And the view payload made it in too:
            cache = load_cache(path=p)
            self.assertEqual(cache.get("probed_at"), 99.0)

    def test_save_cache_into_empty_dir_then_save_snapshot(self):
        # Reverse order: view written first, then snapshot added later.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cache.json'
            save_cache(self._empty_view(), path=p)
            save_snapshot('local_resources', {"v": 1}, path=p)
            # Both must be readable independently.
            self.assertEqual(load_snapshot('local_resources', path=p),
                             {"v": 1})
            self.assertEqual(load_cache(path=p).get("probed_at"), 99.0)

    def test_save_cache_with_no_existing_file_writes_normally(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cache.json'
            view = self._empty_view()
            view.observations.append(Observation(
                source='snmp', kind='network_device', id='sw1',
                endpoint='10.0.0.1', fields={}, observed_at=100.0, ok=True,
            ))
            save_cache(view, path=p)
            cache = load_cache(path=p)
            self.assertEqual(len(cache["observations"]), 1)
            # No previous_snapshots key when none has been written.
            self.assertNotIn("previous_snapshots", cache)


if __name__ == '__main__':
    unittest.main()
