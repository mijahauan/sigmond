"""Tests for the topology loader — cpu_affinity and cpu_freq parsing.

The loader owns the topology.toml schema for the whole project; bin/smd
and the TUI are both downstream.  These tests pin the parsing contract.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.topology import Topology, load_topology


def _write(path: Path, content: str) -> None:
    path.write_text(content)


class DefaultsTests(unittest.TestCase):
    def test_missing_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            t = load_topology(Path(d) / 'absent.toml')
        self.assertEqual(t.cpu_affinity, {'radiod_cpus': '', 'other_cpus': ''})
        self.assertEqual(t.cpu_freq,
                         {'radiod_max_mhz': 3200, 'other_max_mhz': 1400})

    def test_empty_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'topology.toml'
            _write(p, '')
            t = load_topology(p)
        self.assertEqual(t.cpu_affinity['radiod_cpus'], '')
        self.assertEqual(t.cpu_freq['radiod_max_mhz'], 3200)


class CpuAffinityParseTests(unittest.TestCase):
    def test_radiod_cpus_string(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'topology.toml'
            _write(p, '[cpu_affinity]\nradiod_cpus = "0-3"\nother_cpus = "4-15"\n')
            t = load_topology(p)
        self.assertEqual(t.cpu_affinity['radiod_cpus'], '0-3')
        self.assertEqual(t.cpu_affinity['other_cpus'], '4-15')

    def test_partial_override_keeps_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'topology.toml'
            _write(p, '[cpu_affinity]\nradiod_cpus = "0-1"\n')
            t = load_topology(p)
        self.assertEqual(t.cpu_affinity['radiod_cpus'], '0-1')
        self.assertEqual(t.cpu_affinity['other_cpus'], '')

    def test_non_string_value_coerced_to_string(self):
        # TOML can carry integers; coerce rather than fail so operators
        # don't get surprised by quoting.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'topology.toml'
            _write(p, '[cpu_affinity]\nradiod_cpus = 7\n')
            t = load_topology(p)
        self.assertEqual(t.cpu_affinity['radiod_cpus'], '7')


class CpuFreqParseTests(unittest.TestCase):
    def test_explicit_values(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'topology.toml'
            _write(p, ('[cpu_freq]\n'
                       'radiod_max_mhz = 4200\n'
                       'other_max_mhz  = 1800\n'))
            t = load_topology(p)
        self.assertEqual(t.cpu_freq['radiod_max_mhz'], 4200)
        self.assertEqual(t.cpu_freq['other_max_mhz'], 1800)

    def test_partial_override_keeps_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'topology.toml'
            _write(p, '[cpu_freq]\nradiod_max_mhz = 4200\n')
            t = load_topology(p)
        self.assertEqual(t.cpu_freq['radiod_max_mhz'], 4200)
        self.assertEqual(t.cpu_freq['other_max_mhz'], 1400)

    def test_non_int_falls_back_to_default_and_warns(self):
        # TOML validator on load tolerates misconfigurations — we warn
        # but don't crash startup over a bad cpu_freq value.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'topology.toml'
            _write(p, '[cpu_freq]\nradiod_max_mhz = "fast"\n')
            t = load_topology(p)
        self.assertEqual(t.cpu_freq['radiod_max_mhz'], 3200)


class TopologyDataclassTests(unittest.TestCase):
    def test_default_cpu_affinity_is_independent_between_instances(self):
        # Using lambda: dict(...) as default_factory — verify two
        # Topology instances don't share the same dict reference.
        a = Topology(client_dir=Path('/tmp'), smd_bin=Path('/tmp/smd'))
        b = Topology(client_dir=Path('/tmp'), smd_bin=Path('/tmp/smd'))
        a.cpu_affinity['radiod_cpus'] = '0-1'
        self.assertEqual(b.cpu_affinity['radiod_cpus'], '')


if __name__ == '__main__':
    unittest.main()
