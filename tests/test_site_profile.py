"""Tests for sigmond.site_profile + the PSWS push planner (Phase 2:
one-file identity for the golden-image model)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sigmond.site_profile import TEMPLATE, load_site_profile
from sigmond.commands.config import plan_psws_updates


def _write_profile(td: str, body: str) -> Path:
    p = Path(td) / "site-profile.toml"
    p.write_text(body)
    return p


FULL_PROFILE = """\
schema_version = 1

[station]
callsign    = "ac0g"
grid_square = "EM38ww"
latitude    = 38.93
longitude   = -92.33

[psws]
enabled       = true
station_id    = "S000418"
instrument_id = "367"

[psws.instruments]
"hf-timestd"   = "367"
"mag-recorder" = "RM3100"

[reporters]
reporter_id      = "ac0g/s"
wsprnet_call     = ""
pskreporter_call = ""
"""


class TestLoadSiteProfile(unittest.TestCase):

    def test_missing_file_returns_none(self):
        with TemporaryDirectory() as td:
            self.assertIsNone(load_site_profile(Path(td) / "nope.toml"))

    def test_template_parses_with_placeholders_cleaned(self):
        with TemporaryDirectory() as td:
            sp = load_site_profile(_write_profile(td, TEMPLATE))
        self.assertEqual(sp.call, "")          # <YOUR_CALL> cleaned
        self.assertEqual(sp.grid, "")
        self.assertFalse(sp.psws_enabled)
        self.assertEqual(sp.psws_instruments, {})
        self.assertEqual(sp.reporter_id, "")

    def test_full_profile_fields(self):
        with TemporaryDirectory() as td:
            sp = load_site_profile(_write_profile(td, FULL_PROFILE))
        self.assertEqual(sp.call, "AC0G")      # upcased
        self.assertEqual(sp.reporter_id, "AC0G/S")
        self.assertEqual(sp.effective_reporter_id, "AC0G/S")
        self.assertEqual(sp.psws_station_id, "S000418")
        self.assertEqual(sp.instrument_for("hf-timestd"), "367")
        self.assertEqual(sp.instrument_for("mag-recorder"), "RM3100")

    def test_reporter_id_defaults_to_callsign(self):
        with TemporaryDirectory() as td:
            sp = load_site_profile(_write_profile(td, """\
[station]
callsign = "AC0G"
"""))
        self.assertEqual(sp.effective_reporter_id, "AC0G")

    def test_legacy_single_instrument_id_is_hf_timestd(self):
        with TemporaryDirectory() as td:
            sp = load_site_profile(_write_profile(td, """\
[psws]
enabled       = true
station_id    = "S000418"
instrument_id = "172"
"""))
        self.assertEqual(sp.instrument_for("hf-timestd"), "172")
        # No map, no legacy claim for mag — stays empty (its own config
        # template default applies).
        self.assertEqual(sp.instrument_for("mag-recorder"), "")

    def test_instruments_map_wins_over_legacy(self):
        with TemporaryDirectory() as td:
            sp = load_site_profile(_write_profile(td, """\
[psws]
enabled       = true
station_id    = "S000418"
instrument_id = "172"

[psws.instruments]
"hf-timestd" = "367"
"""))
        self.assertEqual(sp.instrument_for("hf-timestd"), "367")


class _FakeState:
    def __init__(self, station="", instrument=""):
        self.station = station
        self.instrument = instrument
        self.config_exists = True


class TestPlanPswsUpdates(unittest.TestCase):

    def _profile(self, body=FULL_PROFILE):
        with TemporaryDirectory() as td:
            return load_site_profile(_write_profile(td, body))

    def test_unset_recorder_gets_both_fields(self):
        sp = self._profile()
        updates = plan_psws_updates(sp, "hf-timestd", _FakeState())
        self.assertEqual(updates, [
            ("station", "id", "S000418"),
            ("station", "instrument_id", "367"),
        ])

    def test_mag_uses_its_own_section_keys(self):
        sp = self._profile()
        updates = plan_psws_updates(sp, "mag-recorder", _FakeState())
        self.assertEqual(updates, [
            ("station", "psws_station_id", "S000418"),
            ("station", "instrument_id", "RM3100"),
        ])

    def test_current_recorder_yields_no_updates(self):
        sp = self._profile()
        st = _FakeState(station="S000418", instrument="367")
        self.assertEqual(plan_psws_updates(sp, "hf-timestd", st), [])

    def test_empty_profile_value_never_clobbers(self):
        sp = self._profile("""\
[psws]
enabled    = true
station_id = "S000418"
""")
        # mag has no instrument id anywhere in this profile; a
        # hand-configured value must survive.
        st = _FakeState(station="S000418", instrument="RM3100-custom")
        self.assertEqual(plan_psws_updates(sp, "mag-recorder", st), [])

    def test_changed_id_is_updated(self):
        sp = self._profile()
        st = _FakeState(station="S000001", instrument="367")
        updates = plan_psws_updates(sp, "hf-timestd", st)
        self.assertEqual(updates, [("station", "id", "S000418")])


if __name__ == "__main__":
    unittest.main()
