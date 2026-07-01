"""Tests for sigmond.uploader_manifest — the Stage 6 manifest generator.

Covers the TOML serializer round-trip, placeholder substitution (incl. the
skip-on-missing-identity path), and an end-to-end generate that reproduces the
pipeline shape hs_uploader.pipeline_factory consumes.
"""

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond import uploader_manifest as um
from sigmond.coordination import Coordination, Host, Radiod


class SerializerTests(unittest.TestCase):
    def test_roundtrip_nested_and_scalars(self):
        identity = {"call": "AC0G/S", "grid": "EM38ww",
                    "ssh_key_file": "/etc/hs-uploader/keys/id_ed25519_host"}
        pipelines = [
            {
                "name": "wspr-wsprnet",
                "batch_limit": 900,
                "source": {
                    "type": "sqlite",
                    "accepted_schema_versions": [1, 2],
                    "delete_on_commit": False,
                    "dedup_partition_by": ["time", "callsign", "band"],
                },
                "transport": {"type": "wsprnet", "version": "4.0"},
                "retry": {"base": 2.0, "cap_sec": 900.0},
            },
            {
                "name": "psk-pskreporter",
                "source": {
                    "type": "sqlite",
                    "extra_where": [["tx_call", "!=", ""],
                                    ["mode", "IN", ["ft8", "ft4"]]],
                },
                "transport": {
                    "type": "wsprdaemon_tar",
                    "servers": ["gw1", "gw2"],
                    "ftp_fallback": {"servers": ["gw2"], "ftp_user": "x"},
                },
            },
        ]
        text = um.render_manifest(pipelines, identity)
        parsed = tomllib.loads(text)

        self.assertEqual(parsed["identity"], identity)
        self.assertEqual(parsed["daemon"]["pump_interval_sec"], 30)
        self.assertEqual([p["name"] for p in parsed["pipeline"]],
                         ["wspr-wsprnet", "psk-pskreporter"])
        p0 = parsed["pipeline"][0]
        self.assertEqual(p0["source"]["accepted_schema_versions"], [1, 2])
        self.assertIs(p0["source"]["delete_on_commit"], False)
        self.assertEqual(p0["retry"]["cap_sec"], 900.0)
        # array-of-arrays
        self.assertEqual(parsed["pipeline"][1]["source"]["extra_where"],
                         [["tx_call", "!=", ""], ["mode", "IN", ["ft8", "ft4"]]])
        # nested-nested table
        self.assertEqual(
            parsed["pipeline"][1]["transport"]["ftp_fallback"]["ftp_user"], "x")

    def test_value_escaping(self):
        self.assertEqual(um._toml_value('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(um._toml_value(True), "true")
        self.assertEqual(um._toml_value(2.0), "2.0")


class SubstitutionTests(unittest.TestCase):
    def test_subst_tracks_used_and_missing(self):
        used, missing = set(), set()
        out = um._subst(
            {"a": "{call}", "b": ["{grid}", "x"], "c": {"d": "radiod={radiod_status}"}},
            {"call": "AC0G", "grid": "EM38ww", "radiod_status": None},
            used, missing)
        self.assertEqual(out["a"], "AC0G")
        self.assertEqual(out["b"], ["EM38ww", "x"])
        # unresolved token is left intact and flagged
        self.assertEqual(out["c"]["d"], "radiod={radiod_status}")
        self.assertEqual(missing, {"radiod_status"})
        self.assertEqual(used, {"call", "grid", "radiod_status"})


def _coord():
    return Coordination(
        host=Host(call="AC0G", grid="EM38ww"),
        radiods={"sigma-status.local": Radiod(id="sigma-status.local")},
    )


class _Topo:
    def __init__(self, names):
        self._names = names

    def enabled_components(self, only=None):
        return list(self._names)


class _State:
    def __init__(self, station="", instrument=""):
        self.station = station
        self.instrument = instrument


class CollectTests(unittest.TestCase):
    def _write_deploy(self, body: str) -> Path:
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(body)
        f.close()
        return Path(f.name)

    GRAPE = """
[[hs_uploader.pipeline]]
name = "grape-psws"
[hs_uploader.pipeline.source]
type = "filetree"
root = "/var/lib/timestd/upload"
table = "grape.dataset"
[hs_uploader.pipeline.transport]
type = "psws_dataset"
instrument_id = "{instrument_id}"
sftp_user = "{station_id}"
name = "psws-grape-sftp:host:{station_id}"
"""

    def test_skip_when_identity_missing(self):
        deploy = self._write_deploy(self.GRAPE)
        with mock.patch.object(um, "find_deploy_toml", return_value=deploy), \
             mock.patch.object(um, "list_instances", return_value=[]), \
             mock.patch.object(um.psws, "is_psws_recorder", return_value=True), \
             mock.patch.object(um.psws, "read_state",
                               return_value=_State(station="", instrument="")):
            pls = um.collect_pipelines(_Topo(["hf-timestd"]), _coord())
        self.assertEqual(pls, [])

    def test_substituted_when_identity_present(self):
        deploy = self._write_deploy(self.GRAPE)
        with mock.patch.object(um, "find_deploy_toml", return_value=deploy), \
             mock.patch.object(um, "list_instances", return_value=[]), \
             mock.patch.object(um.psws, "is_psws_recorder", return_value=True), \
             mock.patch.object(um.psws, "read_state",
                               return_value=_State(station="S000418",
                                                   instrument="367")):
            pls = um.collect_pipelines(_Topo(["hf-timestd"]), _coord())
        self.assertEqual(len(pls), 1)
        t = pls[0]["transport"]
        self.assertEqual(t["instrument_id"], "367")
        self.assertEqual(t["sftp_user"], "S000418")
        self.assertEqual(t["name"], "psws-grape-sftp:host:S000418")

    def test_generate_endtoend_parses(self):
        deploy = self._write_deploy(self.GRAPE)
        with mock.patch.object(um, "find_deploy_toml", return_value=deploy), \
             mock.patch.object(um, "list_instances", return_value=[]), \
             mock.patch.object(um.psws, "is_psws_recorder", return_value=True), \
             mock.patch.object(um.psws, "read_state",
                               return_value=_State(station="S000418",
                                                   instrument="367")), \
             mock.patch.object(um, "host_key_file", return_value="/k"):
            text = um.generate(_Topo(["hf-timestd"]), _coord())
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["identity"]["call"], "AC0G")  # no wspr instance
        self.assertEqual(parsed["identity"]["grid"], "EM38ww")
        self.assertEqual(parsed["identity"]["station_id"], "S000418")
        self.assertEqual([p["name"] for p in parsed["pipeline"]], ["grape-psws"])

    # Shared pipeline both psk-recorder and meteor-scatter declare (MSK144
    # rides the psk.spots stream) — must dedup to exactly one.
    SHARED = """
[[hs_uploader.pipeline]]
name = "psk-pskreporter"
batch_limit = 500
[hs_uploader.pipeline.source]
type = "sqlite"
database = "psk"
table = "spots"
extra_where = [["mode", "IN", ["ft8", "ft4", "msk144"]]]
[hs_uploader.pipeline.transport]
type = "pskreporter"
decoding_software = "psk-recorder/0.1 (radiod={radiod_status})"
"""

    def test_dedup_shared_pipeline_by_name(self):
        deploy = self._write_deploy(self.SHARED)
        # both clients resolve to identical declarations
        with mock.patch.object(um, "find_deploy_toml", return_value=deploy), \
             mock.patch.object(um, "list_instances", return_value=[]):
            pls = um.collect_pipelines(
                _Topo(["psk-recorder", "meteor-scatter"]), _coord())
        self.assertEqual([p["name"] for p in pls], ["psk-pskreporter"])
        self.assertEqual(
            pls[0]["source"]["extra_where"],
            [["mode", "IN", ["ft8", "ft4", "msk144"]]])
        # {radiod_status} substituted from coordination's radiod
        self.assertIn("sigma-status.local",
                      pls[0]["transport"]["decoding_software"])


if __name__ == "__main__":
    unittest.main()
