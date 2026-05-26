"""Tests for sigmond.radiod_migrate — Phase 5 per-host migration."""

import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.radiod_migrate import (
    Candidate, CoordRewrite,
    apply_coord_rewrite, detect_candidates, detect_coord_rewrite,
    rewrite,
)


# ---------------------------------------------------------------------------
# rewrite() — per-file in-memory transformation
# ---------------------------------------------------------------------------

class TestRewriteArraySchema(unittest.TestCase):
    """psk / hfdl shape: [[radiod]] id + radiod_status → status."""

    def test_renames_field_and_drops_id(self):
        body = (
            '[station]\n'
            'callsign = "AC0G"\n'
            '\n'
            '[[radiod]]\n'
            'id            = "my-rx888"\n'
            'radiod_status = "bee1-status.local"\n'
            '\n'
            '[radiod.ft8]\n'
            'freqs_hz = []\n'
        )
        out = rewrite(body, "array", "radiod_status", "bee1-status.local")
        self.assertIn('status = "bee1-status.local"', out)
        self.assertNotIn('id            = "my-rx888"', out)
        # `radiod_status` line is gone (renamed in place to `status`).
        self.assertNotIn('radiod_status', out)
        # Other sections preserved.
        self.assertIn('[station]', out)
        self.assertIn('[radiod.ft8]', out)

    def test_preserves_comments(self):
        body = (
            '[[radiod]]\n'
            '# this comment stays\n'
            'id            = "my-rx888"\n'
            'radiod_status = "bee1-status.local"   # mDNS, never IP\n'
        )
        out = rewrite(body, "array", "radiod_status", "bee1-status.local")
        self.assertIn('# this comment stays', out)
        self.assertIn('# mDNS, never IP', out)


class TestRewriteCodarSchema(unittest.TestCase):
    """codar variant: [[radiod]] id + status_dns → status."""

    def test_renames_status_dns(self):
        body = (
            '[[radiod]]\n'
            'id         = "ac0g-bee1-rx888"\n'
            'status_dns = "bee1-status.local"\n'
        )
        out = rewrite(body, "array", "status_dns", "bee1-status.local")
        self.assertIn('status = "bee1-status.local"', out)
        self.assertNotIn('status_dns', out)
        self.assertNotIn('id         = "ac0g-bee1-rx888"', out)


class TestRewriteSingletonSchema(unittest.TestCase):
    """wspr: [radiod] status_address → status (no `id` to remove)."""

    def test_renames_field_no_id_removal(self):
        body = (
            '[radiod]\n'
            'status_address = "bee1-status.local"\n'
            'port = 5004\n'
        )
        out = rewrite(body, "singleton", "status_address", "bee1-status.local")
        self.assertIn('status = "bee1-status.local"', out)
        self.assertNotIn('status_address', out)
        # `port` line preserved.
        self.assertIn('port = 5004', out)


class TestRewriteKa9qSchema(unittest.TestCase):
    """hf-timestd: [ka9q] status_address → status (under [ka9q] block)."""

    def test_renames_under_ka9q(self):
        body = (
            '[ka9q]\n'
            'status_address = "bee1-status.local"\n'
            'auto_create_channels = true\n'
        )
        out = rewrite(body, "ka9q", "status_address", "bee1-status.local")
        self.assertIn('status = "bee1-status.local"', out)
        self.assertNotIn('status_address', out)
        self.assertIn('auto_create_channels = true', out)


# ---------------------------------------------------------------------------
# detect_candidates() — walks /etc/<client>/ tree
# ---------------------------------------------------------------------------

class TestDetectCandidates(unittest.TestCase):

    def test_detects_psk_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "psk-recorder").mkdir()
            cfg = root / "psk-recorder" / "AC0G-B1.toml"
            cfg.write_text(
                '[[radiod]]\n'
                'id            = "my-rx888"\n'
                'radiod_status = "bee1-status.local"\n'
            )
            cands = detect_candidates(etc_root=root)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.client, "psk-recorder")
        self.assertEqual(c.legacy_field, "radiod_status")
        self.assertEqual(c.current_status, "bee1-status.local")
        self.assertEqual(c.schema_kind, "array")

    def test_skips_already_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "psk-recorder").mkdir()
            cfg = root / "psk-recorder" / "AC0G-B1.toml"
            cfg.write_text(
                '[[radiod]]\n'
                'status = "bee1-status.local"\n'
            )
            cands = detect_candidates(etc_root=root)
        self.assertEqual(cands, [])

    def test_detects_wspr_and_hftimestd(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "wspr-recorder").mkdir()
            (root / "wspr-recorder" / "config.toml").write_text(
                '[radiod]\nstatus_address = "wspr.local"\n'
            )
            (root / "hf-timestd").mkdir()
            (root / "hf-timestd" / "timestd-config.toml").write_text(
                '[ka9q]\nstatus_address = "timestd.local"\n'
            )
            cands = detect_candidates(etc_root=root)
        clients = sorted(c.client for c in cands)
        self.assertEqual(clients, ["hf-timestd", "wspr-recorder"])

    def test_no_candidates_on_empty_tree(self):
        with tempfile.TemporaryDirectory() as d:
            cands = detect_candidates(etc_root=Path(d))
        self.assertEqual(cands, [])


# ---------------------------------------------------------------------------
# coordination.toml block-key rewrite
# ---------------------------------------------------------------------------

class TestCoordRewrite(unittest.TestCase):

    def test_detect_mismatched_key(self):
        with tempfile.TemporaryDirectory() as d:
            coord = Path(d) / "coordination.toml"
            coord.write_text(
                '[host]\ncall = "AC0G"\n'
                '\n'
                '[radiod."ac0g-bee1-rx888"]\n'
                'status_dns = "bee1-status.local"\n'
                'samprate_hz = 129600000\n'
            )
            rw = detect_coord_rewrite(coord)
        self.assertIsNotNone(rw)
        self.assertEqual(rw.old_key, "ac0g-bee1-rx888")
        self.assertEqual(rw.new_key, "bee1-status.local")

    def test_no_rewrite_when_already_aligned(self):
        with tempfile.TemporaryDirectory() as d:
            coord = Path(d) / "coordination.toml"
            coord.write_text(
                '[radiod."bee1-status.local"]\n'
                'samprate_hz = 129600000\n'
            )
            rw = detect_coord_rewrite(coord)
        self.assertIsNone(rw)

    def test_apply_renames_block_and_clients(self):
        with tempfile.TemporaryDirectory() as d:
            coord = Path(d) / "coordination.toml"
            coord.write_text(
                '[radiod."ac0g-bee1-rx888"]\n'
                'status_dns = "bee1-status.local"\n'
                'samprate_hz = 129600000\n'
                '\n'
                '[[clients.psk-recorder]]\n'
                'instance  = "AC0G-B1"\n'
                'radiod_id = "ac0g-bee1-rx888"\n'
            )
            rw = detect_coord_rewrite(coord)
            assert rw is not None
            out = apply_coord_rewrite(rw)
        self.assertIn('[radiod."bee1-status.local"]', out)
        self.assertNotIn('[radiod."ac0g-bee1-rx888"]', out)
        self.assertNotIn('status_dns', out)
        self.assertIn('radiod_id = "bee1-status.local"', out)
        self.assertNotIn('radiod_id = "ac0g-bee1-rx888"', out)


if __name__ == "__main__":
    unittest.main()
