"""Tests for `smd config init|edit radiod` (CONTRACT-v0.5 §14.4)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sigmond.commands import client_config, radiod_config
from sigmond.environment import Observation


def _ns(**kwargs):
    base = dict(non_interactive=True, reconfig=False,
                instance=None, client="radiod")
    base.update(kwargs)
    return SimpleNamespace(**base)


def _sdr(sdr_type: str, serial: str = "", index: int = 0,
         bus: str = "003", device: str = "005",
         vid: str = "04b4", pid: str = "00bc") -> Observation:
    return Observation(
        source="usb_sdr", kind="sdr",
        id=f"usb:{vid}:{pid}:{index}",
        endpoint=f"bus {bus} dev {device}",
        fields={
            "sdr_type": sdr_type,
            "chip":     "Cypress FX3" if "RX-888" in sdr_type else "",
            "vid":      vid, "pid": pid,
            "bus":      bus, "device": device,
            "index":    index, "serial": serial,
            "usb_name": "",
        },
        observed_at=0.0, ok=True,
    )


# ---------------------------------------------------------------------------
# Profile lookup
# ---------------------------------------------------------------------------

class ProfileLookupTests(unittest.TestCase):
    def test_rx888_profile(self):
        p = radiod_config._profile_for("RX-888")
        self.assertEqual(p["section"], "rx888")
        self.assertIn("samprate", p["defaults"])

    def test_airspy_profile(self):
        p = radiod_config._profile_for("Airspy")
        self.assertEqual(p["section"], "airspy")

    def test_unknown_profile_falls_back(self):
        p = radiod_config._profile_for("MysteryRadio")
        self.assertEqual(p["section"], "frontend")


# ---------------------------------------------------------------------------
# Serial formatting
# ---------------------------------------------------------------------------

class SerialFormatTests(unittest.TestCase):
    def test_strip_leading_0x(self):
        self.assertEqual(
            radiod_config._format_serial("Airspy", "0x62CC68FF21146A17"),
            "62CC68FF21146A17",
        )

    def test_pass_through_bare_hex(self):
        self.assertEqual(
            radiod_config._format_serial("RX-888", "0123456789ABCDEF"),
            "0123456789ABCDEF",
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class RenderTests(unittest.TestCase):
    def _plan(self, **overrides) -> dict:
        base = {
            "instance_id": "bee1-rx888",
            "status_dns":  "bee1-rx888-status.local",
            "description": "AC0G T3FD",
            "frontend":    "rx888",
            "frontend_defaults": "samprate    = 64800000",
            "serial":      "0123456789ABCDEF",
            "serial_line": 'serial      = "0123456789ABCDEF"',
            "iface":       "eth0",
            "target":      Path("/etc/radio/radiod@bee1-rx888.conf"),
            "sdr_type":    "RX-888",
        }
        base.update(overrides)
        return base

    def test_renders_all_substitutions(self):
        body = radiod_config._render(self._plan())
        self.assertIn("[global]", body)
        self.assertIn("hardware  = rx888", body)
        self.assertIn("status    = bee1-rx888-status.local", body)
        self.assertIn("[rx888]", body)
        self.assertIn('description = "AC0G T3FD"', body)
        self.assertIn('serial      = "0123456789ABCDEF"', body)
        self.assertIn("samprate    = 64800000", body)
        self.assertIn("iface     = eth0", body)

    def test_render_omits_serial_line_when_unknown(self):
        plan = self._plan(
            serial="",
            serial_line='# serial   = "<run with udev access for stable binding>"',
        )
        body = radiod_config._render(plan)
        self.assertIn("# serial", body)
        self.assertNotIn('serial      = "', body)

    def test_coordination_block_has_canonical_keys(self):
        block = radiod_config._coord_block(self._plan())
        self.assertIn('[radiod."bee1-rx888"]', block)
        self.assertIn('host        = "localhost"', block)
        self.assertIn('status_dns  = "bee1-rx888-status.local"', block)
        self.assertIn('radio_conf  = "/etc/radio/radiod@bee1-rx888.conf"', block)


# ---------------------------------------------------------------------------
# Coordination append
# ---------------------------------------------------------------------------

class AppendCoordinationTests(unittest.TestCase):
    def test_appends_when_block_absent(self):
        with tempfile.TemporaryDirectory() as d:
            coord = Path(d) / "coordination.toml"
            coord.write_text('[host]\ncall = "AC0G"\n')
            block = '[radiod."bee1-rx888"]\nhost = "localhost"\n'
            with mock.patch.object(radiod_config, "COORDINATION_PATH", coord):
                radiod_config._append_coordination([block], _ns())
            text = coord.read_text()
            self.assertIn('[host]', text)
            self.assertIn('[radiod."bee1-rx888"]', text)

    def test_skips_when_block_already_present(self):
        with tempfile.TemporaryDirectory() as d:
            coord = Path(d) / "coordination.toml"
            initial = (
                '[host]\ncall = "AC0G"\n'
                '\n[radiod."bee1-rx888"]\nhost = "localhost"\n'
            )
            coord.write_text(initial)
            block = '[radiod."bee1-rx888"]\nhost = "localhost"\n'
            with mock.patch.object(radiod_config, "COORDINATION_PATH", coord):
                radiod_config._append_coordination([block], _ns())
            self.assertEqual(coord.read_text(), initial)


# ---------------------------------------------------------------------------
# Init wizard end-to-end (non-interactive path)
# ---------------------------------------------------------------------------

class InitWizardTests(unittest.TestCase):
    def test_writes_one_config_per_sdr(self):
        with tempfile.TemporaryDirectory() as d:
            radio_dir = Path(d) / "radio"
            coord_path = Path(d) / "coordination.toml"
            sdrs = [
                _sdr("RX-888", serial="0123456789ABCDEF",
                     index=0, bus="003", device="005"),
                _sdr("Airspy HF+", serial="62CC68FF21146A17",
                     index=0, bus="003", device="008",
                     vid="03eb", pid="800c"),
            ]
            with mock.patch.object(radiod_config, "_discover_sdrs",
                                   return_value=sdrs), \
                 mock.patch.object(radiod_config, "RADIOD_CONFIG_DIR",
                                   radio_dir), \
                 mock.patch.object(radiod_config, "COORDINATION_PATH",
                                   coord_path), \
                 mock.patch.object(radiod_config, "_suggest_iface",
                                   return_value="eth0"), \
                 mock.patch("socket.gethostname",
                            return_value="bee1.local"):
                rc = radiod_config.cmd_radiod_init(_ns())

            self.assertEqual(rc, 0)
            written = sorted(p.name for p in radio_dir.glob("radiod@*.conf"))
            self.assertEqual(len(written), 2)
            # Default ids derive from hostname + sdr family.
            self.assertTrue(any("rx888" in n for n in written))
            self.assertTrue(any("airspyhfp" in n for n in written))
            # Each gets its own .conf.d/.
            for p in radio_dir.glob("radiod@*.conf"):
                self.assertTrue((radio_dir / f"{p.name}.d").is_dir())

            # coordination.toml gets one block per SDR.
            text = coord_path.read_text()
            self.assertEqual(text.count('[radiod."'), 2)
            # Each rendered config locks to the right serial.
            rx_text = (radio_dir / written[
                next(i for i, n in enumerate(written) if "rx888" in n)
            ]).read_text()
            self.assertIn('serial      = "0123456789ABCDEF"', rx_text)

    def test_no_sdrs_yields_actionable_error(self):
        with mock.patch.object(radiod_config, "_discover_sdrs",
                               return_value=[]):
            rc = radiod_config.cmd_radiod_init(_ns())
        self.assertEqual(rc, 1)

    def test_refuses_overwrite_without_reconfig(self):
        with tempfile.TemporaryDirectory() as d:
            radio_dir = Path(d) / "radio"
            radio_dir.mkdir()
            existing = radio_dir / "radiod@bee1-rx888.conf"
            existing.write_text("# existing\n")
            sdrs = [_sdr("RX-888", serial="abc")]
            with mock.patch.object(radiod_config, "_discover_sdrs",
                                   return_value=sdrs), \
                 mock.patch.object(radiod_config, "RADIOD_CONFIG_DIR",
                                   radio_dir), \
                 mock.patch.object(radiod_config, "COORDINATION_PATH",
                                   Path(d) / "coordination.toml"), \
                 mock.patch.object(radiod_config, "_suggest_iface",
                                   return_value="eth0"), \
                 mock.patch.object(radiod_config, "_default_instance_id",
                                   return_value="bee1-rx888"), \
                 mock.patch("socket.gethostname", return_value="bee1"):
                rc = radiod_config.cmd_radiod_init(_ns())
            self.assertEqual(rc, 1)
            self.assertEqual(existing.read_text(), "# existing\n")


# ---------------------------------------------------------------------------
# Dispatcher routing
# ---------------------------------------------------------------------------

class DispatcherRoutingTests(unittest.TestCase):
    def test_init_radiod_routes_to_radiod_config(self):
        called = {}

        def fake_init(args):
            called["init"] = True
            return 0

        ns = SimpleNamespace(client="radiod", instance=None)
        with mock.patch.object(radiod_config, "cmd_radiod_init", fake_init):
            rc = client_config.cmd_config_init(ns)
        self.assertEqual(rc, 0)
        self.assertTrue(called.get("init"))

    def test_edit_radiod_routes_to_radiod_config(self):
        called = {}

        def fake_edit(args):
            called["edit"] = True
            return 0

        ns = SimpleNamespace(client="radiod", instance="bee1")
        with mock.patch.object(radiod_config, "cmd_radiod_edit", fake_edit):
            rc = client_config.cmd_config_edit_client(ns)
        self.assertEqual(rc, 0)
        self.assertTrue(called.get("edit"))


if __name__ == "__main__":
    unittest.main()
