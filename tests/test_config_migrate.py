"""Tests for `smd config migrate` — extraction from wsprdaemon.conf."""

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.commands.config import build_migrated_toml
from sigmond.coordination import parse_coordination


V4_INI = """\
[general]
reporter_call    = AI6VN
reporter_grid    = CM88mc
ka9q_conf_name   = k3lr-rx888
ka9q_web_dns     = k3lr-hf-status.local
rac              = 117
rac_server       = remote.wsprdaemon.org
reserved_cpus    = 11

[receiver:KA9Q_0]
type = ka9q
call = AI6VN-0
grid = CM88mc
radiod_name = k3lr-rx888
address = k3lr-wspr-pcm.local

[hf-timestd]
enabled          = true
timing_authority = rtp
physics_enabled  = false

[ka9q-web]
enabled   = true
base_port = 8081
"""


class TestBuildMigratedToml(unittest.TestCase):

    def test_migration_roundtrips_through_parser(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
            f.write(V4_INI)
            src = Path(f.name)
        try:
            toml_text = build_migrated_toml(src)

            # Must be valid TOML
            raw = tomllib.loads(toml_text)
            coord = parse_coordination(raw)

            self.assertEqual(coord.host.call, "AI6VN")
            self.assertEqual(coord.host.grid, "CM88mc")

            self.assertIn("k3lr-rx888", coord.radiods)
            self.assertEqual(coord.radiods["k3lr-rx888"].status_dns,
                             "k3lr-hf-status.local")
            self.assertTrue(coord.radiods["k3lr-rx888"].is_local)

            self.assertEqual(coord.cpu.reserved_cpus, "11")

            wspr = coord.instances_of("wspr")
            self.assertEqual(len(wspr), 1)
            self.assertEqual(wspr[0].radiod_id, "k3lr-rx888")

            hftimes = coord.instances_of("hf-timestd")
            self.assertEqual(len(hftimes), 1)
            self.assertEqual(hftimes[0].radiod_id, "k3lr-rx888")
            self.assertEqual(hftimes[0].extras.get("timing_authority"), "rtp")

            kaweb = coord.instances_of("ka9q-web")
            self.assertEqual(len(kaweb), 1)
            self.assertEqual(kaweb[0].extras.get("port"), 8081)

            rac = coord.instances_of("rac")
            self.assertEqual(len(rac), 1)
            self.assertEqual(rac[0].extras.get("channel"), 117)
        finally:
            src.unlink()

    def test_migration_tolerates_minimal_input(self):
        minimal = "[general]\nreporter_call = N0CALL\n"
        with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
            f.write(minimal)
            src = Path(f.name)
        try:
            toml_text = build_migrated_toml(src)
            raw = tomllib.loads(toml_text)
            coord = parse_coordination(raw)
            self.assertEqual(coord.host.call, "N0CALL")
            # No radiod section when ka9q_conf_name is absent.
            self.assertEqual(coord.radiods, {})
        finally:
            src.unlink()


if __name__ == "__main__":
    unittest.main()
