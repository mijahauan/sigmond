"""Tests for sigmond.purge — the full-clean path used by
``smd remove --purge`` and by ``smd remove <deprecated-name>``."""

from pathlib import Path
from unittest import mock

import pytest

from sigmond import purge


@pytest.fixture
def stub_paths(tmp_path, monkeypatch):
    """Redirect every GIT_BASE / ETC_BASE / VENV_BASE so the test
    operates in tmp_path instead of the host's real filesystem."""
    monkeypatch.setattr(purge, 'GIT_BASE', tmp_path / 'opt-git')
    monkeypatch.setattr(purge, 'ETC_BASE', tmp_path / 'etc')
    monkeypatch.setattr(purge, 'VENV_BASE', tmp_path / 'opt')
    return tmp_path


def _scaffold_client(stub_paths, name, *, deploy_toml=None,
                     with_venv=True, with_config=True):
    """Lay out a fake client install (source repo + venv + config) so
    plan_purge has something to find."""
    repo = stub_paths / 'opt-git' / name
    repo.mkdir(parents=True)
    if deploy_toml is not None:
        (repo / 'deploy.toml').write_text(deploy_toml)
    if with_venv:
        (stub_paths / 'opt' / name).mkdir(parents=True)
    if with_config:
        (stub_paths / 'etc' / name).mkdir(parents=True)
    return repo


class TestPlanPurge:
    def test_finds_everything_on_disk(self, stub_paths):
        _scaffold_client(stub_paths, 'old-client', deploy_toml='')
        plan = purge.plan_purge('old-client')
        assert plan['repo_dir'] is not None
        assert plan['venv_dir'] is not None
        assert plan['config_dir'] is not None

    def test_empty_plan_when_nothing_on_disk(self, stub_paths):
        plan = purge.plan_purge('nope')
        assert plan['repo_dir'] is None
        assert plan['venv_dir'] is None
        assert plan['config_dir'] is None
        assert plan['expanded_units'] == []
        assert plan['link_dsts'] == []

    def test_parses_units_and_link_steps_from_deploy_toml(
            self, stub_paths, monkeypatch):
        # Avoid hitting real systemctl for template expansion.
        monkeypatch.setattr(purge, '_running_template_instances',
                            lambda u: [])
        deploy = (
            '[systemd]\n'
            'units = ["old-client.service"]\n'
            '\n'
            '[[install.steps]]\n'
            'kind = "link"\n'
            'src  = "systemd/old-client.service"\n'
            'dst  = "/etc/systemd/system/old-client.service"\n'
        )
        _scaffold_client(stub_paths, 'old-client', deploy_toml=deploy)
        plan = purge.plan_purge('old-client')
        assert plan['declared_units'] == ['old-client.service']
        assert plan['expanded_units'] == ['old-client.service']
        assert plan['link_dsts'] == [
            Path('/etc/systemd/system/old-client.service')]


class TestExecutePurge:
    def test_removes_every_tree(self, stub_paths, monkeypatch):
        repo = _scaffold_client(stub_paths, 'old-client', deploy_toml='')
        venv = stub_paths / 'opt' / 'old-client'
        config = stub_paths / 'etc' / 'old-client'
        # Stub systemctl + symlink removal so the test stays hermetic.
        monkeypatch.setattr(purge.subprocess, 'run',
                            lambda *a, **kw: mock.Mock(returncode=0, stderr=''))
        plan = purge.plan_purge('old-client')
        rc = purge.execute_purge(plan)
        assert rc == 0
        assert not repo.exists()
        assert not venv.exists()
        assert not config.exists()

    def test_dry_run_makes_no_changes(self, stub_paths, monkeypatch):
        repo = _scaffold_client(stub_paths, 'old-client', deploy_toml='')
        monkeypatch.setattr(purge.subprocess, 'run',
                            lambda *a, **kw: mock.Mock(returncode=0, stderr=''))
        plan = purge.plan_purge('old-client')
        rc = purge.execute_purge(plan, dry_run=True)
        assert rc == 0
        assert repo.exists()      # still here

    def test_systemctl_failure_warns_but_continues(
            self, stub_paths, monkeypatch, capsys):
        """A failing systemctl-stop on an already-gone unit must not
        block the rm-rf phase — the purge is best-effort on the
        systemd side and authoritative on the filesystem side."""
        monkeypatch.setattr(purge, '_running_template_instances',
                            lambda u: [])
        deploy = (
            '[systemd]\n'
            'units = ["old-client.service"]\n'
        )
        repo = _scaffold_client(stub_paths, 'old-client',
                                deploy_toml=deploy, with_venv=False,
                                with_config=False)
        # Simulate systemctl errors that are NOT the "already gone" case
        # so the warning path actually fires.
        monkeypatch.setattr(
            purge.subprocess, 'run',
            lambda *a, **kw: mock.Mock(returncode=5, stderr='boom'),
        )
        plan = purge.plan_purge('old-client')
        rc = purge.execute_purge(plan)
        assert rc == 0
        assert not repo.exists()
        err = capsys.readouterr().err
        assert 'boom' in err


class TestDeprecatedOnDisk:
    def test_picks_up_dirs_only_in_deprecated_set(self, stub_paths):
        # Two on disk, only one in the deprecation list.
        (stub_paths / 'opt-git' / 'wsprdaemon-client').mkdir(parents=True)
        (stub_paths / 'opt-git' / 'still-supported').mkdir(parents=True)
        deprecated = {'wsprdaemon-client': mock.Mock()}
        assert purge.deprecated_on_disk(deprecated) == ['wsprdaemon-client']

    def test_skips_deprecated_entries_with_no_dir(self, stub_paths):
        deprecated = {'long-gone-client': mock.Mock()}
        assert purge.deprecated_on_disk(deprecated) == []
