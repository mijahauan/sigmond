"""Tests for the environment manifest loader."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.environment import load_environment


def _write(d: Path, content: str) -> Path:
    p = d / 'environment.toml'
    p.write_text(content)
    return p


class LoaderDefaultsTests(unittest.TestCase):
    def test_missing_file_returns_empty_env(self):
        with tempfile.TemporaryDirectory() as d:
            env = load_environment(Path(d) / 'absent.toml')
        self.assertEqual(env.site.name, '')
        self.assertEqual(env.radiods, [])
        self.assertEqual(env.kiwisdrs, [])
        self.assertEqual(env.gpsdos, [])
        self.assertEqual(env.time_sources, [])
        self.assertTrue(env.discovery.mdns_enabled)

    def test_empty_file_loads_clean(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), '')
            env = load_environment(p)
        self.assertEqual(env.source_path, p)
        self.assertEqual(env.radiods, [])


class LoaderContentTests(unittest.TestCase):
    MANIFEST = """
[site]
name = "AC0G-EN34"

[[radiod]]
id         = "bee1-hf"
host       = "bee1.local"
status_dns = "hf-status.local"
role       = "primary"
expect.frontend.gpsdo_lock = true

[[radiod]]
id         = "bee2-vhf"
host       = "bee2.local"
status_dns = "vhf-status.local"

[[kiwisdr]]
id   = "kiwi-east"
host = "kiwi1.local"
port = 8073
gps_expected = true

[[gpsdo]]
id             = "bodnar-bee1"
kind           = "leo-bodnar-mini"
host           = "localhost"
authority_json = "/tmp/authority.json"
serves         = ["bee1-hf"]

[[time_source]]
id          = "upstream-ntp"
kind        = "ntp"
host        = "time.nist.gov"
stratum_max = 2

[discovery]
mdns_enabled = false
passive_only = true
background_interval = 0
"""

    def test_site_and_radiods(self):
        with tempfile.TemporaryDirectory() as d:
            env = load_environment(_write(Path(d), self.MANIFEST))
        self.assertEqual(env.site.name, "AC0G-EN34")
        self.assertEqual(len(env.radiods), 2)
        r0 = env.radiods[0]
        self.assertEqual(r0.id, "bee1-hf")
        self.assertEqual(r0.status_dns, "hf-status.local")
        self.assertEqual(r0.expect, {"frontend": {"gpsdo_lock": True}})

    def test_kiwisdrs_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            env = load_environment(_write(Path(d), self.MANIFEST))
        self.assertEqual(len(env.kiwisdrs), 1)
        self.assertEqual(env.kiwisdrs[0].port, 8073)
        self.assertTrue(env.kiwisdrs[0].gps_expected)

    def test_discovery_cfg_honoured(self):
        with tempfile.TemporaryDirectory() as d:
            env = load_environment(_write(Path(d), self.MANIFEST))
        self.assertFalse(env.discovery.mdns_enabled)
        self.assertTrue(env.discovery.passive_only)
        self.assertEqual(env.discovery.background_interval, 0)

    def test_iter_declared_yields_all(self):
        with tempfile.TemporaryDirectory() as d:
            env = load_environment(_write(Path(d), self.MANIFEST))
        kinds = [k for k, _ in env.iter_declared()]
        self.assertEqual(kinds.count("radiod"), 2)
        self.assertEqual(kinds.count("kiwisdr"), 1)
        self.assertEqual(kinds.count("gpsdo"), 1)
        self.assertEqual(kinds.count("time_source"), 1)

    def test_rows_without_id_are_skipped(self):
        manifest = """
[[radiod]]
id = "keeps"
host = "ok.local"

[[radiod]]
host = "no-id.local"
"""
        with tempfile.TemporaryDirectory() as d:
            env = load_environment(_write(Path(d), manifest))
        self.assertEqual(len(env.radiods), 1)
        self.assertEqual(env.radiods[0].id, "keeps")


if __name__ == '__main__':
    unittest.main()
