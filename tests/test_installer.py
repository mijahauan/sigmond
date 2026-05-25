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

    def test_source_only_dep_is_auto_cloned(self, tmp_path):
        """A consumer's `requires` entry that is a pure source dep
        (repo set, no install_script, not yet on disk) is cloned to
        /opt/git/sigmond/<dep> before the consumer's install.sh runs.
        Mirrors the mag-recorder → mag-usb relationship."""
        repo = tmp_path / 'mag-recorder'
        repo.mkdir()
        script = repo / 'install.sh'
        script.write_text('#!/bin/sh\n')
        entry = _entry(name='mag-recorder', install_script=None,
                       requires=('mag-usb',))
        dep = CatalogEntry(
            name='mag-usb', kind='library', description='C source dep',
            repo='https://github.com/wittend/mag-usb',
            install_script=None,
        )
        catalog = {'mag-recorder': entry, 'mag-usb': dep}
        with mock.patch('sigmond.installer.clone_repo') as cr:
            cr.side_effect = [repo, tmp_path / 'mag-usb']
            with mock.patch('sigmond.installer.subprocess.run') as m:
                m.return_value = mock.Mock(returncode=0)
                assert install_client(entry, catalog=catalog) is True
                # clone_repo should have been called twice: once for the
                # consumer (pull_if_exists=False, the default), then again
                # for the mag-usb source dep.
                cloned_names = [c.args[0].name for c in cr.call_args_list]
                assert cloned_names == ['mag-recorder', 'mag-usb']

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
