"""Tests for sigmond.log_cmd."""

import os
from pathlib import Path
from unittest import mock

import pytest

from sigmond.log_cmd import (
    client_env_key,
    flatten_log_paths,
    get_inventory_log_paths,
    set_log_level,
    _upsert_env_line,
    send_sighup,
)


class TestClientEnvKey:
    def test_simple(self):
        assert client_env_key('psk-recorder') == 'PSK_RECORDER_LOG_LEVEL'

    def test_hf_timestd(self):
        assert client_env_key('hf-timestd') == 'HF_TIMESTD_LOG_LEVEL'

    def test_wsprdaemon_client(self):
        assert client_env_key('wsprdaemon-client') == 'WSPRDAEMON_CLIENT_LOG_LEVEL'

    def test_wspr_recorder(self):
        assert client_env_key('wspr-recorder') == 'WSPR_RECORDER_LOG_LEVEL'


class TestFlattenLogPaths:
    def test_simple_dict(self):
        paths = flatten_log_paths({
            'process': '/var/log/foo/foo.log',
            'spots': '/var/log/foo/spots.log',
        })
        assert sorted(paths) == ['/var/log/foo/foo.log', '/var/log/foo/spots.log']

    def test_nested_dict(self):
        paths = flatten_log_paths({
            'process': '/var/log/psk/main.log',
            'spots': {
                'ft8': '/var/log/psk/ft8.log',
                'ft4': '/var/log/psk/ft4.log',
            },
        })
        assert sorted(paths) == [
            '/var/log/psk/ft4.log',
            '/var/log/psk/ft8.log',
            '/var/log/psk/main.log',
        ]

    def test_bare_string(self):
        paths = flatten_log_paths('/var/log/x.log')
        assert paths == ['/var/log/x.log']

    def test_empty(self):
        assert flatten_log_paths({}) == []


class TestUpsertEnvLine:
    def test_creates_new_file(self, tmp_path):
        env = tmp_path / 'coordination.env'
        _upsert_env_line(env, 'FOO', 'bar')
        assert env.read_text() == 'FOO=bar\n'

    def test_updates_existing_key(self, tmp_path):
        env = tmp_path / 'coordination.env'
        env.write_text('# header\nFOO=old\nBAR=baz\n')
        _upsert_env_line(env, 'FOO', 'new')
        lines = env.read_text().splitlines()
        assert '# header' in lines
        assert 'FOO=new' in lines
        assert 'BAR=baz' in lines

    def test_appends_new_key(self, tmp_path):
        env = tmp_path / 'coordination.env'
        env.write_text('# header\nEXISTING=yes\n')
        _upsert_env_line(env, 'NEW_KEY', 'val')
        text = env.read_text()
        assert 'EXISTING=yes' in text
        assert 'NEW_KEY=val' in text

    def test_creates_parent_dirs(self, tmp_path):
        env = tmp_path / 'sub' / 'dir' / 'coordination.env'
        _upsert_env_line(env, 'X', 'Y')
        assert env.read_text() == 'X=Y\n'


class TestSetLogLevel:
    def test_client_specific(self, tmp_path):
        env = tmp_path / 'coordination.env'
        key = set_log_level('psk-recorder', 'debug', env_path=env)
        assert key == 'PSK_RECORDER_LOG_LEVEL'
        assert 'PSK_RECORDER_LOG_LEVEL=DEBUG' in env.read_text()

    def test_generic_fallback(self, tmp_path):
        env = tmp_path / 'coordination.env'
        key = set_log_level(None, 'WARNING', env_path=env)
        assert key == 'CLIENT_LOG_LEVEL'
        assert 'CLIENT_LOG_LEVEL=WARNING' in env.read_text()

    def test_invalid_level_raises(self, tmp_path):
        env = tmp_path / 'coordination.env'
        with pytest.raises(ValueError, match='invalid log level'):
            set_log_level('foo', 'VERBOSE', env_path=env)

    def test_update_preserves_other_keys(self, tmp_path):
        env = tmp_path / 'coordination.env'
        env.write_text('STATION_CALL=AC0G\nPSK_RECORDER_LOG_LEVEL=INFO\n')
        set_log_level('psk-recorder', 'debug', env_path=env)
        text = env.read_text()
        assert 'STATION_CALL=AC0G' in text
        assert 'PSK_RECORDER_LOG_LEVEL=DEBUG' in text
        assert 'INFO' not in text


class TestGetInventoryLogPaths:
    def test_missing_binary_returns_none(self, monkeypatch):
        monkeypatch.setattr('sigmond.log_cmd.shutil.which', lambda n: None)
        assert get_inventory_log_paths('nonexistent') is None

    def test_parses_log_paths(self, monkeypatch):
        import sigmond.log_cmd as mod
        monkeypatch.setattr(mod.shutil, 'which', lambda n: '/usr/bin/fake')
        fake_result = mock.Mock(
            returncode=0,
            stdout='{"log_paths": {"process": "/var/log/x.log"}}',
        )
        monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **kw: fake_result)
        result = get_inventory_log_paths('fake')
        assert result == {'process': '/var/log/x.log'}

    def test_no_log_paths_returns_none(self, monkeypatch):
        import sigmond.log_cmd as mod
        monkeypatch.setattr(mod.shutil, 'which', lambda n: '/usr/bin/fake')
        fake_result = mock.Mock(returncode=0, stdout='{"client": "fake"}')
        monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **kw: fake_result)
        assert get_inventory_log_paths('fake') is None


class TestSendSighup:
    def test_success(self, monkeypatch):
        import sigmond.log_cmd as mod
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0)
        monkeypatch.setattr(mod.subprocess, 'run', fake_run)
        failed = send_sighup(['foo.service', 'bar.service'])
        assert failed == []
        assert len(calls) == 2

    def test_partial_failure(self, monkeypatch):
        import sigmond.log_cmd as mod
        def fake_run(cmd, **kw):
            unit = cmd[-1]
            return mock.Mock(returncode=1 if 'bad' in unit else 0)
        monkeypatch.setattr(mod.subprocess, 'run', fake_run)
        failed = send_sighup(['good.service', 'bad.service'])
        assert failed == ['bad.service']
