"""`smd start <name>` auto-enables an installed-but-disabled component.

Naming a component to start expresses "I want this running here", so the
forward path collapses to install -> start (no separate `smd enable`).  Bare
`smd start` (no args) must NOT auto-enable anything, and a named-but-not-
installed component must fall through to the start gate, not leave a stray
enabled=true.  See bin/smd `_autoenable_named_on_start`.
"""
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_smd():
    # bin/smd re-execs into the production venv unless told not to; suppress
    # that so importing the script just defines its functions.
    os.environ.setdefault("SIGMOND_NO_VENV_REEXEC", "1")
    loader = importlib.machinery.SourceFileLoader("smd_under_test", str(REPO / "bin" / "smd"))
    spec = importlib.util.spec_from_loader("smd_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


smd = _load_smd()


class _Args:
    def __init__(self, names=None, components=None):
        self.names = names or []
        self.components = components


class StartAutoEnableTests(unittest.TestCase):
    def setUp(self):
        self.tdir = Path(tempfile.mkdtemp())
        self.topo = self.tdir / "topology.toml"
        self.topo.write_text(
            "[component.psk-recorder]\nenabled = true\n\n"
            "[component.hfdl-recorder]\nenabled = false\n")
        import sigmond.component_state as cs
        self._cs = cs
        self._orig = cs.compute_state

    def tearDown(self):
        self._cs.compute_state = self._orig

    def _set_installed(self, installed: bool):
        self._cs.compute_state = (
            lambda name, topology=None, alias=None:
            type("S", (), {"installed": installed})())

    def test_named_installed_but_disabled_is_enabled(self):
        self._set_installed(True)
        changed = smd._autoenable_named_on_start(
            _Args(names=["hfdl-recorder"]), self.topo)
        self.assertTrue(changed)
        seg = self.topo.read_text().split("hfdl-recorder", 1)[1]
        self.assertIn("enabled = true", seg)

    def test_no_name_does_not_autoenable(self):
        self._set_installed(True)
        self.assertFalse(smd._autoenable_named_on_start(_Args(), self.topo))
        # 'all' is the explicit "everything enabled" keyword — also a no-op.
        self.assertFalse(
            smd._autoenable_named_on_start(_Args(names=["all"]), self.topo))

    def test_named_not_installed_is_not_enabled(self):
        self._set_installed(False)
        changed = smd._autoenable_named_on_start(
            _Args(names=["codar-sounder"]), self.topo)
        self.assertFalse(changed)
        self.assertNotIn("codar-sounder", self.topo.read_text())

    def test_legacy_components_flag_also_autoenables(self):
        self._set_installed(True)
        changed = smd._autoenable_named_on_start(
            _Args(components="hfdl-recorder"), self.topo)
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
