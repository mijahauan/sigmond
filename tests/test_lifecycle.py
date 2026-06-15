"""Tests for sigmond lifecycle resolution."""

import fcntl
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from sigmond.lifecycle import (
    resolve_units, UnitRef, _unit_kind, _expand_template,
    lifecycle_lock, order_units,
)


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

    def test_global_config_toml_not_an_instance(self, tmp_path, monkeypatch):
        """A global ``*-config.toml`` must not be read as a per-instance config.

        Regression: hf-timestd ships /etc/hf-timestd/timestd-config.toml, whose
        stem was added to ``configured`` — inventing a phantom
        ``timestd-metrology@timestd-config`` unit AND, by making ``configured``
        non-empty, falsely flagging every real systemd-discovered channel
        (CHU_*/WWV_*/SHARED_*) as orphaned.
        """
        component = 'hf-timestd'
        deploy_toml = tmp_path / 'deploy.toml'
        deploy_toml.write_text(
            '[systemd]\ntemplated_units = ["timestd-metrology@.service"]\n'
        )
        etc_dir = tmp_path / 'etc'
        etc_dir.mkdir()
        (etc_dir / 'timestd-config.toml').write_text('# global config, not an instance')

        monkeypatch.setattr('sigmond.lifecycle._find_deploy_toml',
                            lambda comp: deploy_toml if comp == component else None)

        def fake_path(p):
            s = str(p)
            if s == f'/etc/{component}':
                return etc_dir              # real dir → real *.toml glob
            if s.startswith('/etc/'):
                m = mock.Mock(spec=Path)
                m.exists.return_value = False
                m.glob.return_value = []
                return m
            return Path(p)
        monkeypatch.setattr('sigmond.lifecycle.Path', fake_path)

        def fake_run(cmd, *a, **k):
            if 'list-units' in cmd:
                return mock.Mock(returncode=0, stdout=(
                    '[{"unit": "timestd-metrology@CHU_14670.service"}, '
                    '{"unit": "timestd-metrology@WWV_20000.service"}]'))
            return mock.Mock(returncode=0, stdout=(
                'timestd-metrology@CHU_14670.service enabled enabled\n'
                'timestd-metrology@WWV_20000.service enabled enabled\n'))

        with mock.patch('sigmond.lifecycle.subprocess.run', side_effect=fake_run):
            units = resolve_units([component], [component])

        names = {u.unit for u in units}
        assert 'timestd-metrology@timestd-config.service' not in names  # no phantom
        assert 'timestd-metrology@CHU_14670.service' in names
        assert 'timestd-metrology@WWV_20000.service' in names
        assert all(not u.orphaned for u in units)  # none falsely orphaned


def _mock_path(path_str, env_dir=None):
    """Helper to mock Path for env dir existence checks.

    The env_dir mock returns matching env files.  Every other path
    starting with ``/etc/`` is returned as a non-existent Mock so
    real-filesystem entries on the test host (e.g. /etc/psk-recorder/
    when bee1 is the runner) don't leak in as discovered instances.
    """
    if env_dir and 'env' in str(path_str):
        p = mock.Mock(spec=Path)
        p.exists.return_value = True
        p.glob.return_value = list(env_dir.glob('*.env'))
        return p
    if str(path_str).startswith('/etc/'):
        p = mock.Mock(spec=Path)
        p.exists.return_value = False
        p.glob.return_value = []
        return p
    return Path(path_str)


# ---------------------------------------------------------------------------
# Lifecycle lock tests (CONTRACT §5.5)
# ---------------------------------------------------------------------------

class TestLifecycleLock:

    def test_lock_acquired_and_released(self, tmp_path, monkeypatch):
        lock_file = tmp_path / 'lifecycle.lock'
        monkeypatch.setattr('sigmond.lifecycle.LIFECYCLE_LOCK', lock_file)

        with lifecycle_lock(reason='test'):
            assert lock_file.exists()
        # Lock file still exists (that's fine), but the lock is released.
        assert lock_file.exists()

    def test_contention_raises_system_exit(self, tmp_path, monkeypatch):
        lock_file = tmp_path / 'lifecycle.lock'
        monkeypatch.setattr('sigmond.lifecycle.LIFECYCLE_LOCK', lock_file)

        # Hold the lock via a raw fd.
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(SystemExit, match='another lifecycle operation'):
                with lifecycle_lock(reason='contention-test'):
                    pass  # should never reach here
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_lock_creates_parent_dir(self, tmp_path, monkeypatch):
        lock_file = tmp_path / 'nested' / 'dir' / 'lifecycle.lock'
        monkeypatch.setattr('sigmond.lifecycle.LIFECYCLE_LOCK', lock_file)

        with lifecycle_lock(reason='mkdir-test'):
            assert lock_file.parent.exists()


# ---------------------------------------------------------------------------
# Start ordering tests (CONTRACT §5.4)
# ---------------------------------------------------------------------------

def _unit(component, name='default', kind='service'):
    """Helper to create a UnitRef for testing."""
    unit_str = f'{component}@{name}.{kind}' if name else f'{component}.{kind}'
    return UnitRef(
        component=component,
        unit=unit_str,
        template=f'{component}@.{kind}' if name else None,
        instance=name if name else None,
        kind=kind,
        source='test',
    )


class TestOrderUnits:

    def test_radiod_first(self):
        units = [
            _unit('hf-timestd'),
            _unit('radiod', name=None),
            _unit('psk-recorder'),
        ]
        ordered = order_units(units)
        assert ordered[0].component == 'radiod'

    def test_coordination_order(self):
        """Within EQUAL start_priority, clients follow coordination.toml
        declaration order (the secondary tiebreaker)."""
        units = [
            _unit('psk-recorder'),
            _unit('hf-timestd'),
            _unit('radiod', name=None),
            _unit('wspr-recorder'),
        ]
        # Simulate a Coordination with clients in a specific order.
        coord = mock.Mock()
        coord.clients = [
            mock.Mock(client_type='wspr-recorder'),
            mock.Mock(client_type='hf-timestd'),
            mock.Mock(client_type='psk-recorder'),
        ]
        # Explicit equal priorities so coordination order is the sole
        # determinant (the on-disk catalog gives hf-timestd a lower priority,
        # which is covered by test_priority_overrides_coordination).
        priorities = {'radiod': 0, 'wspr-recorder': 100,
                      'hf-timestd': 100, 'psk-recorder': 100}
        ordered = order_units(units, coordination=coord, priorities=priorities)
        names = [u.component for u in ordered]
        assert names == ['radiod', 'wspr-recorder', 'hf-timestd', 'psk-recorder']

    def test_priority_overrides_coordination(self):
        """start_priority is the PRIMARY key — a lower-priority component
        starts before coordination-earlier ones (e.g. the hf-timestd timing
        authority ahead of wspr/psk, which bringup Stage 4 relies on)."""
        units = [
            _unit('psk-recorder'),
            _unit('wspr-recorder'),
            _unit('hf-timestd'),
            _unit('radiod', name=None),
        ]
        coord = mock.Mock()
        coord.clients = [
            mock.Mock(client_type='wspr-recorder'),
            mock.Mock(client_type='psk-recorder'),
            mock.Mock(client_type='hf-timestd'),
        ]
        priorities = {'radiod': 0, 'hf-timestd': 50,
                      'wspr-recorder': 100, 'psk-recorder': 100}
        ordered = order_units(units, coordination=coord, priorities=priorities)
        names = [u.component for u in ordered]
        # hf-timestd (50) jumps ahead of wspr/psk (100) despite being last in
        # coordination order; wspr before psk by coordination tiebreak.
        assert names == ['radiod', 'hf-timestd', 'wspr-recorder', 'psk-recorder']

    def test_no_radiod_kiwi_only(self):
        """Ordering works when radiod is absent (kiwi-only station)."""
        units = [
            _unit('wsprdaemon-client'),
            _unit('psk-recorder'),
        ]
        ordered = order_units(units)
        # No radiod, so just alphabetical.
        names = [u.component for u in ordered]
        assert names == ['psk-recorder', 'wsprdaemon-client']

    def test_without_coordination(self):
        """Without coordination, non-radiod components sort alphabetically."""
        units = [
            _unit('psk-recorder'),
            _unit('radiod', name=None),
            _unit('hf-timestd'),
        ]
        ordered = order_units(units, coordination=None)
        names = [u.component for u in ordered]
        assert names == ['radiod', 'hf-timestd', 'psk-recorder']

    def test_empty_input(self):
        assert order_units([]) == []

    def test_preserves_instances_within_component(self):
        """Multiple instances of the same component stay in original order."""
        units = [
            _unit('psk-recorder', name='lf'),
            _unit('psk-recorder', name='default'),
            _unit('radiod', name=None),
        ]
        ordered = order_units(units)
        psk_units = [u for u in ordered if u.component == 'psk-recorder']
        assert psk_units[0].instance == 'lf'
        assert psk_units[1].instance == 'default'
