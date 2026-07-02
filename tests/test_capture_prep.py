"""Tests for sigmond.capture_prep — the pre-capture wipe planner."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sigmond.capture_prep import Action, build_capture_plan


def _touch(root: Path, rel: str, body: str = "x") -> Path:
    p = root / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


class TestBuildCapturePlan(unittest.TestCase):

    def _kinds(self, plan):
        return [(a.kind, a.payload) for a in plan]

    def test_bare_root_still_plans_identity_and_notes(self):
        with TemporaryDirectory() as td:
            plan = build_capture_plan(root=Path(td), ssh_host_keys=[])
        kinds = {a.kind for a in plan}
        # Always present regardless of what exists on disk:
        self.assertIn("clear-coordination", kinds)
        self.assertIn("psws-placeholders", kinds)
        self.assertIn("vacuum-journal", kinds)
        self.assertIn("note", kinds)  # PHaRLAP stays
        # Nothing to remove on a bare root.
        self.assertNotIn("remove", kinds)

    def test_secrets_and_keys_planned_when_present(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _touch(root, "/etc/sigmond/frpc.toml")
            _touch(root, "/etc/hs-uploader/keys/id_ed25519_host")
            _touch(root, "/etc/hs-uploader/keys/id_ed25519_host.pub")
            plan = build_capture_plan(root=root, ssh_host_keys=[])
        removed = [a.payload for a in plan if a.kind == "remove"]
        self.assertIn("/etc/sigmond/frpc.toml", removed)
        self.assertIn("/etc/hs-uploader/keys/id_ed25519_host", removed)
        self.assertIn("/etc/hs-uploader/keys/id_ed25519_host.pub", removed)

    def test_site_profile_reset_not_removed(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _touch(root, "/etc/sigmond/site-profile.toml", "[station]\n")
            plan = build_capture_plan(root=root, ssh_host_keys=[])
        resets = [a for a in plan if a.kind == "reset-template"]
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0].payload, "/etc/sigmond/site-profile.toml")

    def test_instances_removed(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            cfg = _touch(root, "/etc/wspr-recorder/AC0G=S.toml")
            env = _touch(root, "/etc/wspr-recorder/env/AC0G=S.env")
            plan = build_capture_plan(
                root=root, ssh_host_keys=[],
                instances=[("wspr-recorder", "AC0G/S",
                            "/etc/wspr-recorder/AC0G=S.toml",
                            "/etc/wspr-recorder/env/AC0G=S.env")])
        removed = [a.payload for a in plan if a.kind == "remove"]
        self.assertIn("/etc/wspr-recorder/AC0G=S.toml", removed)
        self.assertIn("/etc/wspr-recorder/env/AC0G=S.env", removed)

    def test_absent_instance_paths_skipped(self):
        with TemporaryDirectory() as td:
            plan = build_capture_plan(
                root=Path(td), ssh_host_keys=[],
                instances=[("psk-recorder", "AC0G/S", None,
                            "/etc/psk-recorder/env/AC0G=S.env")])
        self.assertEqual([a for a in plan if a.kind == "remove"], [])

    def test_keep_data_skips_data_and_journal(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _touch(root, "/var/lib/sigmond/sink.db")
            _touch(root, "/var/lib/timestd/raw/x.bin")
            plan = build_capture_plan(root=root, keep_data=True,
                                      ssh_host_keys=[])
        kinds = {a.kind for a in plan}
        self.assertNotIn("clear-tree", kinds)
        self.assertNotIn("vacuum-journal", kinds)
        removed = [a.payload for a in plan if a.kind == "remove"]
        self.assertNotIn("/var/lib/sigmond/sink.db", removed)

    def test_data_planned_by_default(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _touch(root, "/var/lib/sigmond/sink.db")
            _touch(root, "/var/lib/timestd/raw/x.bin")
            _touch(root, "/etc/fftw/wisdomf")
            plan = build_capture_plan(root=root, ssh_host_keys=[])
        removed = [a.payload for a in plan if a.kind == "remove"]
        cleared = [a.payload for a in plan if a.kind == "clear-tree"]
        self.assertIn("/var/lib/sigmond/sink.db", removed)
        self.assertIn("/etc/fftw/wisdomf", removed)  # per-CPU wisdom
        self.assertIn("/var/lib/timestd", cleared)

    def test_empty_data_tree_not_cleared(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "var/lib/timestd").mkdir(parents=True)
            plan = build_capture_plan(root=root, ssh_host_keys=[])
        self.assertEqual([a for a in plan if a.kind == "clear-tree"], [])

    def test_os_identity_wiped_last(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _touch(root, "/etc/machine-id", "abc")
            _touch(root, "/etc/sigmond/.personalized")
            _touch(root, "/etc/sigmond/frpc.toml")
            plan = build_capture_plan(
                root=root,
                ssh_host_keys=["/etc/ssh/ssh_host_ed25519_key"])
        payloads = [a.payload for a in plan]
        # secrets first, machine-id truncation and ssh keys at the end
        self.assertLess(payloads.index("/etc/sigmond/frpc.toml"),
                        payloads.index("/etc/machine-id"))
        truncs = [a for a in plan if a.kind == "truncate"]
        self.assertEqual(truncs[0].payload, "/etc/machine-id")
        self.assertEqual(payloads[-2], "/etc/ssh/ssh_host_ed25519_key")
        # PHaRLAP note is the closing line
        self.assertEqual(plan[-1].kind, "note")

    def test_actions_are_dataclass_with_labels(self):
        with TemporaryDirectory() as td:
            plan = build_capture_plan(root=Path(td), ssh_host_keys=[])
        for a in plan:
            self.assertIsInstance(a, Action)
            self.assertTrue(a.label)


if __name__ == "__main__":
    unittest.main()
