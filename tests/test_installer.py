"""Tests for sigmond.installer — catalog-driven client install."""

from pathlib import Path
from unittest import mock

import pytest

from sigmond.catalog import CatalogEntry
from sigmond.installer import (
    GIT_BASE,
    clone_repo,
    find_install_script,
    install_client,
    run_install_script,
)


def _entry(name='fake-client', **overrides):
    defaults = dict(
        name=name, kind='client', description='test',
        repo='https://github.com/test/fake-client',
        install_script=f'/opt/git/sigmond/{name}/scripts/install.sh',
    )
    defaults.update(overrides)
    return CatalogEntry(**defaults)


class TestCloneRepo:
    def test_clones_when_absent(self, tmp_path):
        entry = _entry()
        with mock.patch('sigmond.installer.subprocess.run') as m:
            m.return_value = mock.Mock(returncode=0)
            result = clone_repo(entry, base=tmp_path)
            assert result == tmp_path / 'fake-client'
            cmd = m.call_args[0][0]
            assert cmd[0] == 'git' and cmd[1] == 'clone'

    def test_pulls_when_exists_and_requested(self, tmp_path):
        """When pull_if_exists is True, advance the local checkout to
        match origin's default branch.  Implementation uses fetch +
        checkout -B (rather than git pull) so it survives a previously
        pinned-ref detached-HEAD state — see clone_repo() for details."""
        repo = tmp_path / 'fake-client'
        repo.mkdir()
        entry = _entry()
        with mock.patch('sigmond.installer.subprocess.run') as m:
            m.return_value = mock.Mock(returncode=0, stdout='origin/main\n')
            clone_repo(entry, base=tmp_path, pull_if_exists=True)
            invoked = [call_args[0][0] for call_args in m.call_args_list]
            assert any('fetch' in cmd for cmd in invoked)
            assert any('checkout' in cmd for cmd in invoked)

    def test_skips_when_exists_no_pull(self, tmp_path):
        repo = tmp_path / 'fake-client'
        repo.mkdir()
        entry = _entry()
        with mock.patch('sigmond.installer.subprocess.run') as m:
            result = clone_repo(entry, base=tmp_path, pull_if_exists=False)
            m.assert_not_called()
            assert result == repo

    def test_clone_failure_raises(self, tmp_path):
        entry = _entry()
        with mock.patch('sigmond.installer.subprocess.run') as m:
            m.return_value = mock.Mock(returncode=1, stderr='fatal: not found')
            with pytest.raises(RuntimeError, match='clone.*failed'):
                clone_repo(entry, base=tmp_path)

    def test_no_repo_url_raises(self, tmp_path):
        entry = _entry(repo='')
        with pytest.raises(RuntimeError, match='no repo URL'):
            clone_repo(entry, base=tmp_path)


class TestFindInstallScript:
    def test_finds_catalog_path(self, tmp_path):
        script = tmp_path / 'scripts' / 'install.sh'
        script.parent.mkdir()
        script.write_text('#!/bin/sh\n')
        entry = _entry(install_script=str(script))
        assert find_install_script(entry, tmp_path) == script

    def test_finds_relative_in_repo(self, tmp_path):
        script = tmp_path / 'scripts' / 'install.sh'
        script.parent.mkdir()
        script.write_text('#!/bin/sh\n')
        entry = _entry(install_script='/nonexistent/scripts/install.sh')
        assert find_install_script(entry, tmp_path) == script

    def test_returns_none_when_no_script_field(self, tmp_path):
        entry = _entry(install_script=None)
        assert find_install_script(entry, tmp_path) is None

    def test_returns_none_when_not_found(self, tmp_path):
        entry = _entry(install_script='/nonexistent/install.sh')
        assert find_install_script(entry, tmp_path) is None


class TestRunInstallScript:
    def test_runs_with_sudo(self, tmp_path):
        script = tmp_path / 'scripts' / 'install.sh'
        script.parent.mkdir()
        script.write_text('#!/bin/sh\n')
        entry = _entry(install_script=str(script))
        with mock.patch('sigmond.installer.subprocess.run') as m:
            m.return_value = mock.Mock(returncode=0)
            assert run_install_script(entry, tmp_path) is True
            cmd = m.call_args[0][0]
            assert cmd[0] == 'sudo'

    def test_passes_yes_flag(self, tmp_path):
        script = tmp_path / 'scripts' / 'install.sh'
        script.parent.mkdir()
        script.write_text('#!/bin/sh\n')
        entry = _entry(install_script=str(script))
        with mock.patch('sigmond.installer.subprocess.run') as m:
            m.return_value = mock.Mock(returncode=0)
            run_install_script(entry, tmp_path, yes=True)
            cmd = m.call_args[0][0]
            assert '--yes' in cmd

    def test_dry_run_skips_execution(self, tmp_path):
        script = tmp_path / 'scripts' / 'install.sh'
        script.parent.mkdir()
        script.write_text('#!/bin/sh\n')
        entry = _entry(install_script=str(script))
        with mock.patch('sigmond.installer.subprocess.run') as m:
            assert run_install_script(entry, tmp_path, dry_run=True) is True
            m.assert_not_called()

    def test_returns_false_on_failure(self, tmp_path):
        script = tmp_path / 'scripts' / 'install.sh'
        script.parent.mkdir()
        script.write_text('#!/bin/sh\n')
        entry = _entry(install_script=str(script))
        with mock.patch('sigmond.installer.subprocess.run') as m:
            m.return_value = mock.Mock(returncode=1)
            assert run_install_script(entry, tmp_path) is False

    def test_returns_false_when_no_script(self, tmp_path):
        entry = _entry(install_script=None)
        assert run_install_script(entry, tmp_path) is False


class TestInstallClient:
    def test_no_install_script_and_no_repo_returns_false(self):
        entry = _entry(name='radiod', kind='server',
                       install_script=None, repo='')
        assert install_client(entry) is False

    def test_full_flow(self, tmp_path):
        script = tmp_path / 'fake-client' / 'scripts' / 'install.sh'
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/sh\n')
        entry = _entry(install_script=str(script))
        with mock.patch('sigmond.installer.clone_repo', return_value=tmp_path / 'fake-client'):
            with mock.patch('sigmond.installer.subprocess.run') as m:
                m.return_value = mock.Mock(returncode=0)
                assert install_client(entry) is True

    def test_source_only_dep_is_auto_cloned(self, tmp_path, monkeypatch):
        """A consumer's `requires` entry that is a pure source dep
        (repo set, no install_script, not yet on disk) is cloned to
        /opt/git/sigmond/<dep> before the consumer's install.sh runs.
        Mirrors the wspr-recorder / psk-recorder / mag-recorder →
        callhash / hs-uploader relationship (catalog entry with a repo
        URL but no install_script).

        Pins GIT_BASE to tmp_path so the dep-already-on-disk check
        doesn't see a real /opt/git/sigmond/<dep> on a host where the
        consumer is actually installed."""
        monkeypatch.setattr('sigmond.installer.GIT_BASE', tmp_path)
        repo = tmp_path / 'mag-recorder'
        repo.mkdir()
        script = repo / 'install.sh'
        script.write_text('#!/bin/sh\n')
        entry = _entry(name='mag-recorder', install_script=None,
                       requires=('hs-uploader',))
        dep = CatalogEntry(
            name='hs-uploader', kind='library', description='Python source dep',
            repo='https://github.com/HamSCI/hs-uploader',
            install_script=None,
        )
        catalog = {'mag-recorder': entry, 'hs-uploader': dep}
        with mock.patch('sigmond.installer.clone_repo') as cr:
            cr.side_effect = [repo, tmp_path / 'hs-uploader']
            with mock.patch('sigmond.installer.subprocess.run') as m:
                m.return_value = mock.Mock(returncode=0)
                assert install_client(entry, catalog=catalog) is True
                # clone_repo should have been called twice: once for the
                # consumer (pull_if_exists=False, the default), then again
                # for the hs-uploader source dep.
                cloned_names = [c.args[0].name for c in cr.call_args_list]
                assert cloned_names == ['mag-recorder', 'hs-uploader']

    def test_no_catalog_script_discovers_in_repo(self, tmp_path):
        """mag-recorder pattern: catalog has no install_script, but the
        repo carries install.sh which gets discovered post-clone."""
        repo = tmp_path / 'fake-client'
        repo.mkdir()
        script = repo / 'install.sh'
        script.write_text('#!/bin/sh\n')
        entry = _entry(install_script=None)
        with mock.patch('sigmond.installer.clone_repo', return_value=repo):
            with mock.patch('sigmond.installer.subprocess.run') as m:
                m.return_value = mock.Mock(returncode=0)
                assert install_client(entry) is True
                # Verify it actually invoked sudo bash <script>.
                invoked_cmds = [c.args[0] for c in m.call_args_list]
                assert any(
                    cmd[0] == 'sudo' and str(script) in cmd
                    for cmd in invoked_cmds
                )


class TestCheckoutRefShallow:
    """Regression for sigmond#13 — _checkout_ref must deepen a shallow clone to
    reach a pinned ref that's older than the --depth 1 history (install.sh
    pre-clones every catalog repo shallow; the ka9q-radio compat pin then isn't
    present and `git checkout <pin>` fails with 'unable to read tree')."""

    @staticmethod
    def _git(repo, *args):
        import subprocess
        return subprocess.run(['git', '-C', str(repo), *args],
                              capture_output=True, text=True)

    def _make_upstream(self, path):
        """A 2-commit upstream repo; return (old_sha, new_sha)."""
        path.mkdir()
        g = lambda *a: self._git(path, *a)
        g('init', '-q', '-b', 'main')
        g('config', 'user.email', 't@t'); g('config', 'user.name', 't')
        (path / 'f').write_text('v1\n'); g('add', '.'); g('commit', '-qm', 'old')
        old = g('rev-parse', 'HEAD').stdout.strip()
        (path / 'f').write_text('v2\n'); g('add', '.'); g('commit', '-qm', 'new')
        new = g('rev-parse', 'HEAD').stdout.strip()
        return old, new

    def test_deepens_shallow_clone_to_reach_pin(self, tmp_path):
        from sigmond.installer import _checkout_ref
        upstream = tmp_path / 'up'
        old, new = self._make_upstream(upstream)

        repo = tmp_path / 'clone'
        # shallow depth-1 clone == what install.sh does; only `new` is present.
        self._git(tmp_path, 'clone', '--quiet', '--depth', '1',
                  f'file://{upstream}', str(repo))
        assert self._git(repo, 'rev-parse',
                         '--is-shallow-repository').stdout.strip() == 'true'
        # the #13 precondition: the older pin is NOT reachable yet
        assert self._git(repo, 'rev-parse', '--verify', '--quiet',
                         f'{old}^{{commit}}').returncode != 0

        # clone_repo fetches origin before delegating to _checkout_ref
        self._git(repo, 'fetch', 'origin')
        _checkout_ref(repo, old)

        assert self._git(repo, 'rev-parse', 'HEAD').stdout.strip() == old
        assert self._git(repo, 'rev-parse',
                         '--is-shallow-repository').stdout.strip() == 'false'

    def test_noop_on_full_clone_already_has_ref(self, tmp_path):
        from sigmond.installer import _checkout_ref
        upstream = tmp_path / 'up'
        old, new = self._make_upstream(upstream)
        repo = tmp_path / 'clone'
        self._git(tmp_path, 'clone', '--quiet',
                  f'file://{upstream}', str(repo))   # full clone
        _checkout_ref(repo, old)                     # reachable -> plain checkout
        assert self._git(repo, 'rev-parse', 'HEAD').stdout.strip() == old

    def test_raises_on_genuinely_missing_ref(self, tmp_path):
        import pytest
        from sigmond.installer import _checkout_ref
        upstream = tmp_path / 'up'
        self._make_upstream(upstream)
        repo = tmp_path / 'clone'
        self._git(tmp_path, 'clone', '--quiet', f'file://{upstream}', str(repo))
        with pytest.raises(RuntimeError):
            _checkout_ref(repo, 'deadbeef' * 5)      # 40-char sha that doesn't exist
