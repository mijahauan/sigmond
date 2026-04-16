"""Tests for sigmond lifecycle resolution."""

import sys
from pathlib import Path
from unittest import mock

import pytest

from sigmond.lifecycle import resolve_units, UnitRef, _unit_kind, _expand_template


class TestUnitKind:
    """Test unit kind detection."""

    def test_service(self):
        assert _unit_kind('foo.service') == 'service'
        assert _unit_kind('psk-recorder@.service') == 'service'

    def test_timer(self):
        assert _unit_kind('foo.timer') == 'timer'
        assert _unit_kind('foo-daily.timer') == 'timer'

    def test_target(self):
        assert _unit_kind('foo.target') == 'target'
        assert _unit_kind('timestd-metrology.target') == 'target'

    def test_unknown(self):
        assert _unit_kind('foo.socket') == 'unknown'


class TestResolveUnits:
    """Test unit resolution from deploy.toml."""

    def test_resolve_concrete_units(self, tmp_path, monkeypatch):
        """Test resolving concrete (non-templated) units."""
        component = 'test-client'
        deploy_toml = tmp_path / 'deploy.toml'
        deploy_toml.write_text("""
[systemd]
units = ["test.service", "test-daily.timer"]
""")

        monkeypatch.setattr('sigmond.lifecycle._find_deploy_toml',
                           lambda comp: deploy_toml if comp == component else None)

        units = resolve_units([component], [component])

        assert len(units) == 2
        assert all(u.component == component for u in units)
        assert all(not u.orphaned for u in units)
        assert {u.unit for u in units} == {'test.service', 'test-daily.timer'}
        assert {u.template for u in units} == {None}

    def test_resolve_templated_units(self, tmp_path, monkeypatch):
        """Test resolving templated units with instance expansion."""
        component = 'psk-recorder'
        deploy_toml = tmp_path / 'deploy.toml'
        deploy_toml.write_text("""
[systemd]
templated_units = ["psk-recorder@.service"]
""")

        env_dir = tmp_path / 'env'
        env_dir.mkdir()
        (env_dir / 'default.env').write_text('# instance default')
        (env_dir / 'lf.env').write_text('# instance lf')

        monkeypatch.setattr('sigmond.lifecycle._find_deploy_toml',
                           lambda comp: deploy_toml if comp == component else None)
        monkeypatch.setattr('sigmond.lifecycle.Path',
                           lambda p: _mock_path(p, env_dir if 'env' in str(p) else None))

        with mock.patch('sigmond.lifecycle.subprocess.run') as mock_run:
            # Mock systemctl list-units to return no known orphaned instances
            mock_run.return_value = mock.Mock(returncode=1, stdout='')

            units = resolve_units([component], [component])

        assert len(units) == 2
        unit_names = {u.unit for u in units}
        assert 'psk-recorder@default.service' in unit_names
        assert 'psk-recorder@lf.service' in unit_names

    def test_backward_compat_templated_in_units(self, tmp_path, monkeypatch):
        """Test backward compatibility: templated names in 'units' key."""
        component = 'psk-recorder'
        deploy_toml = tmp_path / 'deploy.toml'
        deploy_toml.write_text("""
[systemd]
units = ["psk-recorder@.service"]
""")

        env_dir = tmp_path / 'env'
        env_dir.mkdir()
        (env_dir / 'default.env').write_text('# instance default')

        monkeypatch.setattr('sigmond.lifecycle._find_deploy_toml',
                           lambda comp: deploy_toml if comp == component else None)
        monkeypatch.setattr('sigmond.lifecycle.Path',
                           lambda p: _mock_path(p, env_dir if 'env' in str(p) else None))

        with mock.patch('sigmond.lifecycle.subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout='')
            with pytest.warns(DeprecationWarning, match='deprecated'):
                units = resolve_units([component], [component])

        assert len(units) == 1
        assert units[0].unit == 'psk-recorder@default.service'

    def test_unknown_component_fails(self):
        """Test that unknown components raise ValueError."""
        with pytest.raises(ValueError, match='not found'):
            resolve_units(['unknown-component'], ['other-component'])


def _mock_path(path_str, env_dir=None):
    """Helper to mock Path for env dir existence checks."""
    if env_dir and 'env' in str(path_str):
        p = mock.Mock(spec=Path)
        p.exists.return_value = True
        p.glob.return_value = list(env_dir.glob('*.env'))
        return p
    else:
        return Path(path_str)
