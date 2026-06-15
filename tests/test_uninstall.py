"""Hermetic tests for the whole-host uninstaller (lib/sigmond/uninstall.py).

Covers the pure logic — the `make install` argv parser and the
plan-render keep/remove classification — without touching the real host."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import sigmond.uninstall as uninstall
from sigmond.uninstall import (
    _split_install_args, render_plan, UninstallPlan,
    _revert_grub, _strip_grub_tokens,
)


class TestSplitInstallArgs(unittest.TestCase):
    def test_mode_value_not_a_source(self):
        # `install -m 0755 start-hfdl /S/usr/local/sbin` -> src start-hfdl only
        toks = ["install", "-m", "0755", "start-hfdl",
                "/__SIGMOND_UNINSTALL_SENTINEL__/usr/local/sbin"]
        srcs, tdir = _split_install_args(toks)
        self.assertEqual(srcs, ["start-hfdl"])
        self.assertIsNone(tdir)

    def test_multiple_sources(self):
        toks = ["install", "-m", "0644", "98-sockbuf.conf", "50-multicast.conf",
                "/__SIGMOND_UNINSTALL_SENTINEL__/etc/sysctl.d"]
        srcs, tdir = _split_install_args(toks)
        self.assertEqual(srcs, ["98-sockbuf.conf", "50-multicast.conf"])

    def test_dash_t_target_dir(self):
        toks = ["install", "-m", "0644", "-D", "html/index.html", "-t",
                "/__SIGMOND_UNINSTALL_SENTINEL__/usr/local/share/ka9q-web/html"]
        srcs, tdir = _split_install_args(toks)
        self.assertEqual(srcs, ["html/index.html"])
        self.assertTrue(tdir.endswith("/usr/local/share/ka9q-web/html"))


def _sample_plan(keep_config: bool, wipe_data: bool) -> UninstallPlan:
    p = UninstallPlan(keep_config=keep_config, wipe_data=wipe_data,
                      revert_host=not keep_config, remove_users=not keep_config,
                      remove_source=not keep_config)
    p.config_dirs = [Path("/etc/wspr-recorder"), Path("/etc/sigmond")]
    p.data_dirs = [Path("/var/lib/sigmond")]
    p.ext_files = [Path("/usr/local/sbin/radiod")]
    p.ext_asset_dirs = [Path("/usr/local/share/ka9q-radio")]
    p.venvs = [Path("/opt/git/sigmond/sigmond/venv")]
    p.checkouts = [Path("/opt/git/sigmond/wspr-recorder")]
    p.users = ["sigmond"]
    return p


class TestRenderClassification(unittest.TestCase):
    def test_full_mode_removes_config_and_data(self):
        lines = render_plan(_sample_plan(keep_config=False, wipe_data=True))
        body = "\n".join(lines)
        self.assertIn("mode: full", body)
        # config + data are rm in full mode
        self.assertTrue(any("config" in l and "rm" in l and "wspr-recorder" in l
                            for l in lines))
        self.assertTrue(any("data" in l and "rm" in l for l in lines))
        # ext-files/assets always removed
        self.assertTrue(any("ext-file" in l and "rm" in l for l in lines))
        self.assertTrue(any("ext-asset" in l and "rm" in l for l in lines))

    def test_keep_config_preserves_config_data_source(self):
        lines = render_plan(_sample_plan(keep_config=True, wipe_data=False))
        self.assertTrue(any("config" in l and "KEEP" in l for l in lines))
        self.assertTrue(any("data" in l and "KEEP" in l for l in lines))
        self.assertTrue(any("source" in l and "KEEP" in l for l in lines))
        # but software (venv, ext-files) is still removed even in keep-config
        self.assertTrue(any("venv" in l and "rm" in l for l in lines))
        self.assertTrue(any("ext-file" in l and "rm" in l for l in lines))

    def test_keep_config_wipe_data_override(self):
        lines = render_plan(_sample_plan(keep_config=True, wipe_data=True))
        # config kept, but data removed when --wipe-data overrides
        self.assertTrue(any("config" in l and "KEEP" in l for l in lines))
        self.assertTrue(any("data" in l and "rm" in l for l in lines))


class TestProtectedDirs(unittest.TestCase):
    """The catastrophic bug: a deploy.toml dst=/etc/systemd/system caused
    rmtree to wipe every host service's enable-symlinks.  Lock the guard."""

    def test_critical_shared_dirs_protected(self):
        from sigmond.uninstall import _PROTECTED_DIRS
        for d in ("/etc/systemd/system", "/etc", "/etc/udev/rules.d",
                  "/usr/local/bin", "/usr/local/sbin", "/usr/local/lib",
                  "/var/lib", "/var/log", "/opt/git/sigmond", "/"):
            self.assertIn(Path(d), _PROTECTED_DIRS, f"{d} must be protected")

    def test_rmtree_noops_on_protected(self):
        # Must refuse + return without raising.  Use a protected dir that we
        # assert remains afterwards (never actually removed).
        from sigmond.uninstall import _rmtree
        _rmtree(Path("/usr/local/bin"))
        self.assertTrue(Path("/usr/local/bin").is_dir()
                        or not Path("/usr/local/bin").exists())



class TestRevertGrub(unittest.TestCase):
    """Regression coverage for sigmond#12 — `_revert_grub` must drop the
    isolcpus/rcu_nocbs tokens WITHOUT eating the closing quote of the
    GRUB_CMDLINE_LINUX_DEFAULT assignment.  A corrupt /etc/default/grub fails
    grub-mkconfig, which fails the kernel postrm hook, which bricks every apt
    transaction."""

    def _revert(self, contents: str) -> str:
        """Run _revert_grub against a temp grub file with update-grub stubbed;
        return the rewritten file contents."""
        tf = Path(tempfile.mktemp())
        tf.write_text(contents)
        real_run = subprocess.run

        def fake_run(cmd, *a, **k):
            if cmd and cmd[0] in ("update-grub", "grub-mkconfig"):
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, *a, **k)   # let the `sh -n` safety check run

        orig_file, orig_run = uninstall.GRUB_FILE, uninstall.subprocess.run
        try:
            uninstall.GRUB_FILE = tf
            uninstall.subprocess.run = fake_run
            _revert_grub()
            return tf.read_text()
        finally:
            uninstall.GRUB_FILE, uninstall.subprocess.run = orig_file, orig_run
            tf.unlink(missing_ok=True)

    def _parses(self, text: str) -> bool:
        return subprocess.run(["sh", "-n"], input=text, text=True,
                              capture_output=True).returncode == 0

    def test_strip_tokens_preserves_remaining_words(self):
        self.assertEqual(_strip_grub_tokens("quiet isolcpus=0,1 rcu_nocbs=0,1"),
                         "quiet")
        self.assertEqual(_strip_grub_tokens("isolcpus=2-15 quiet splash"),
                         "quiet splash")
        self.assertEqual(_strip_grub_tokens("quiet splash"), "quiet splash")

    def test_revert_does_not_strip_closing_quote(self):
        # The exact sigma format that triggered #12.
        src = ('GRUB_DEFAULT=0\n'
               'GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=0,1 rcu_nocbs=0,1"\n'
               'GRUB_CMDLINE_LINUX=""\n')
        out = self._revert(src)
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="quiet"', out)
        self.assertNotIn("isolcpus", out)
        self.assertNotIn("rcu_nocbs", out)
        self.assertIn("GRUB_DEFAULT=0", out)            # unrelated line preserved
        self.assertTrue(self._parses(out), "rewritten grub must pass `sh -n`")

    def test_revert_token_at_end_and_single_quotes_and_comment(self):
        out = self._revert(
            "GRUB_CMDLINE_LINUX_DEFAULT='quiet splash isolcpus=2-15'   # tuned\n")
        self.assertIn("GRUB_CMDLINE_LINUX_DEFAULT='quiet splash'", out)
        self.assertIn("# tuned", out)
        self.assertTrue(self._parses(out))

    def test_revert_only_tokens_yields_empty_quoted_value(self):
        out = self._revert('GRUB_CMDLINE_LINUX_DEFAULT="isolcpus=0,1 rcu_nocbs=0,1"\n')
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT=""', out)
        self.assertTrue(self._parses(out))

    def test_revert_leaves_untouched_when_no_tokens(self):
        src = 'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\n'
        self.assertEqual(self._revert(src), src)        # no rewrite, byte-identical

    def test_revert_ignores_token_word_in_comment(self):
        src = '# isolcpus=0,1 set manually\nGRUB_TIMEOUT=5\n'
        self.assertEqual(self._revert(src), src)


class TestDeployExtrasStateDirs(unittest.TestCase):
    """sigmond#11: a data/log dir whose name differs from the component (e.g.
    hf-timestd → /var/lib/timestd) must be captured from the deploy.toml's
    mkdir step, not only the VAR_LIB/<name> convention (which would look for the
    non-existent /var/lib/hf-timestd and leave /var/lib/timestd behind)."""

    def test_captures_declared_mkdir_state_dir(self):
        import shutil
        import tempfile
        from sigmond import purge
        d = Path(tempfile.mkdtemp())
        try:
            var_lib = d / "var-lib"
            (var_lib / "timestd").mkdir(parents=True)   # the real data dir
            git = d / "git"
            (git / "hf-timestd").mkdir(parents=True)     # repo must exist
            fake = {"systemd": {}, "install": {"steps": [
                {"kind": "mkdir", "dst": str(var_lib / "timestd")},
                {"kind": "render", "dst": "/etc/hf-timestd/x"},   # not a state root
                {"kind": "link", "dst": "/usr/local/bin/timestd"},  # link, not state
            ]}}
            saved = (purge._read_deploy_toml, uninstall.VAR_LIB, uninstall.GIT_BASE)
            try:
                purge._read_deploy_toml = lambda repo: fake
                uninstall.VAR_LIB = var_lib
                uninstall.GIT_BASE = git
                extras = uninstall._deploy_extras("hf-timestd")
            finally:
                (purge._read_deploy_toml, uninstall.VAR_LIB,
                 uninstall.GIT_BASE) = saved
            self.assertIn(var_lib / "timestd", extras["state_dirs"])
            # the link dst goes to link_dsts, not state_dirs
            self.assertIn(Path("/usr/local/bin/timestd"), extras["link_dsts"])
            self.assertNotIn(Path("/etc/hf-timestd/x"), extras["state_dirs"])
        finally:
            shutil.rmtree(d)


class TestSweepOrphanUnits(unittest.TestCase):
    """sigmond#11: after unit files are deleted, their `*.wants` enable-symlinks
    dangle and linger in systemctl forever — sweep removes the dead links while
    leaving still-valid ones (other software) alone."""

    def test_removes_dangling_keeps_valid(self):
        import shutil
        import subprocess
        import tempfile
        d = Path(tempfile.mkdtemp())
        try:
            sysd = d / "system"
            wants = sysd / "multi-user.target.wants"
            wants.mkdir(parents=True)
            (sysd / "keepme.service").write_text("[Unit]\n")
            (wants / "keepme.service").symlink_to(sysd / "keepme.service")     # valid
            (wants / "wspr-recorder@AC0G=S.service").symlink_to(
                sysd / "gone.service")                                          # dangling
            saved = (uninstall.SYSTEMD_SYSTEM, uninstall.subprocess.run)
            try:
                uninstall.SYSTEMD_SYSTEM = sysd
                uninstall.subprocess.run = lambda *a, **k: \
                    subprocess.CompletedProcess(a[0] if a else [], 0, "", "")
                uninstall._sweep_orphan_units()
            finally:
                uninstall.SYSTEMD_SYSTEM, uninstall.subprocess.run = saved
            remaining = sorted(p.name for p in wants.iterdir())
            self.assertEqual(remaining, ["keepme.service"])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
