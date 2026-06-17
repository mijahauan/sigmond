"""Tests for sigmond.upload — the per-instance upload-enable helper that
backs `smd config upload` and rule_upload_enabled."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond import upload


class TestUploadEnable(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())

    def _env(self, client, inst):
        return self.base / client / "env" / f"{inst}.env"

    def test_storage_form_slash_to_equals(self):
        self.assertEqual(upload.storage_instance("AC0G/S"), "AC0G=S")
        self.assertEqual(upload.storage_instance("my-rx888"), "my-rx888")

    def test_apply_enable_creates_and_sets_flag(self):
        path, flag, dests = upload.apply_enable(
            "wspr-recorder", "AC0G/S", True, base=str(self.base))
        self.assertEqual(flag, "WSPR_USE_HS_UPLOADER")
        self.assertEqual(path, self._env("wspr-recorder", "AC0G=S"))
        self.assertIn("WSPR_USE_HS_UPLOADER=1", path.read_text())
        self.assertIn("wsprdaemon.org", dests)

    def test_apply_preserves_other_keys(self):
        env = self._env("wspr-recorder", "AC0G=S")
        env.parent.mkdir(parents=True)
        env.write_text("# header\nWD_DECODE_VIA_DB=1\n")
        upload.apply_enable("wspr-recorder", "AC0G/S", True, base=str(self.base))
        body = env.read_text()
        self.assertIn("WD_DECODE_VIA_DB=1", body)        # untouched
        self.assertIn("# header", body)                  # comment preserved
        self.assertIn("WSPR_USE_HS_UPLOADER=1", body)

    def test_off_sets_zero_and_updates_in_place(self):
        upload.apply_enable("psk-recorder", "AC0G/S", True, base=str(self.base))
        upload.apply_enable("psk-recorder", "AC0G/S", False, base=str(self.base))
        body = self._env("psk-recorder", "AC0G=S").read_text()
        self.assertIn("PSK_USE_HS_UPLOADER=0", body)
        self.assertEqual(body.count("PSK_USE_HS_UPLOADER"), 1)  # updated, not appended

    def test_unknown_client_raises(self):
        with self.assertRaises(KeyError):
            upload.apply_enable("hfdl-recorder", "x", True, base=str(self.base))

    def test_edits_existing_noncanonical_spelling(self):
        # If a file already exists under the raw reporter id, edit it in place
        # rather than shadowing it with a new canonical file.
        env = self._env("wspr-recorder", "AC0G=S")
        env.parent.mkdir(parents=True)
        env.write_text("WSPR_USE_HS_UPLOADER=0\n")
        path, _, _ = upload.apply_enable(
            "wspr-recorder", "AC0G/S", True, base=str(self.base))
        self.assertEqual(path, env)
        self.assertIn("WSPR_USE_HS_UPLOADER=1", env.read_text())


if __name__ == "__main__":
    unittest.main()


class TestConfigUploadCommand(unittest.TestCase):
    """The `smd config upload` wrapper (arg handling + delegation)."""

    def _args(self, **kw):
        import argparse
        ns = argparse.Namespace(client=None, instance=None, on=False, off=False)
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_unknown_client_returns_1(self):
        from sigmond.commands.config import cmd_config_upload
        self.assertEqual(
            cmd_config_upload(self._args(client="hfdl-recorder")), 1)

    def test_change_without_instance_returns_1(self):
        from sigmond.commands.config import cmd_config_upload
        self.assertEqual(
            cmd_config_upload(self._args(client="wspr-recorder", on=True)), 1)

    def test_enable_delegates_to_apply(self):
        from sigmond.commands import config as C
        from sigmond import upload
        calls = []
        orig = upload.apply_enable
        upload.apply_enable = (lambda c, i, on, base="/etc":
                               (calls.append((c, i, on))
                                or (Path("/x.env"), "WSPR_USE_HS_UPLOADER",
                                    ["wsprnet.org"])))
        try:
            rc = C.cmd_config_upload(
                self._args(client="wspr-recorder", instance="AC0G/S", on=True))
        finally:
            upload.apply_enable = orig
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("wspr-recorder", "AC0G/S", True)])
