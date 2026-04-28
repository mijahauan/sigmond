"""Tests for `smd config init|edit <client>` dispatch (CONTRACT-v0.5 §14)."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.commands import client_config
from sigmond.coordination import (
    ClientInstance, Coordination, Host, Radiod,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executable(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _coord_with(call: str = "", grid: str = "",
                lat: float = 0.0, lon: float = 0.0,
                radiods: dict | None = None,
                clients: list | None = None) -> Coordination:
    return Coordination(
        host=Host(call=call, grid=grid, lat=lat, lon=lon),
        radiods=radiods or {},
        clients=clients or [],
    )


# ---------------------------------------------------------------------------
# Reading [contract.config] from deploy.toml
# ---------------------------------------------------------------------------

class ContractConfigReadTests(unittest.TestCase):
    def test_returns_block_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            dt = Path(d) / 'deploy.toml'
            dt.write_text(
                '[contract.config]\n'
                'init = "scripts/setup.sh"\n'
                'edit = "scripts/edit.sh"\n'
            )
            block = client_config._read_contract_config(dt)
        self.assertEqual(block, {
            'init': 'scripts/setup.sh',
            'edit': 'scripts/edit.sh',
        })

    def test_returns_empty_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            dt = Path(d) / 'deploy.toml'
            dt.write_text('[package]\nname = "x"\n')
            block = client_config._read_contract_config(dt)
        self.assertEqual(block, {})

    def test_returns_empty_on_unreadable(self):
        # Nonexistent file is treated as 'no contract.config'.
        block = client_config._read_contract_config(Path('/nonexistent/x.toml'))
        self.assertEqual(block, {})


# ---------------------------------------------------------------------------
# Env var bag
# ---------------------------------------------------------------------------

class EnvBagTests(unittest.TestCase):
    def test_populates_station_vars(self):
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=_coord_with(call='AC0G',
                                                        grid='EM38',
                                                        lat=39.1, lon=-94.5)), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag()

        self.assertEqual(env['STATION_CALL'], 'AC0G')
        self.assertEqual(env['STATION_GRID'], 'EM38')
        self.assertEqual(env['STATION_LAT'], '39.1')
        self.assertEqual(env['STATION_LON'], '-94.5')

    def test_omits_empty_vars(self):
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=_coord_with()), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag()
        self.assertNotIn('STATION_CALL', env)
        self.assertNotIn('SIGMOND_TIME_SOURCE', env)
        self.assertNotIn('SIGMOND_RADIOD_STATUS', env)

    def test_unambiguous_radiod_status_set(self):
        coord = _coord_with(radiods={
            'main': Radiod(id='main', host='localhost',
                           status_dns='hf-status.local'),
        })
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=coord), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag()
        self.assertEqual(env['SIGMOND_RADIOD_STATUS'], 'hf-status.local')

    def test_multiple_radiods_omits_status_var(self):
        coord = _coord_with(radiods={
            'a': Radiod(id='a', status_dns='a.local'),
            'b': Radiod(id='b', status_dns='b.local'),
        })
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=coord), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag()
        self.assertNotIn('SIGMOND_RADIOD_STATUS', env)

    def test_instance_resolves_per_client_radiod(self):
        coord = _coord_with(
            radiods={
                'a': Radiod(id='a', status_dns='a.local'),
                'b': Radiod(id='b', status_dns='b.local'),
            },
            clients=[
                ClientInstance(client_type='wspr-recorder',
                               instance='radiod-0', radiod_id='a'),
                ClientInstance(client_type='wspr-recorder',
                               instance='radiod-1', radiod_id='b'),
            ],
        )
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=coord), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag(
                client='wspr-recorder', instance='radiod-1')
        self.assertEqual(env['SIGMOND_INSTANCE'], 'radiod-1')
        self.assertEqual(env['SIGMOND_RADIOD_STATUS'], 'b.local')

    def test_no_instance_falls_back_to_unambiguous(self):
        coord = _coord_with(
            radiods={'only': Radiod(id='only', status_dns='only.local')},
        )
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=coord), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag()
        self.assertEqual(env['SIGMOND_RADIOD_STATUS'], 'only.local')
        self.assertNotIn('SIGMOND_INSTANCE', env)

    def test_radiod_count_always_set(self):
        # Empty env still surfaces COUNT=0 so clients can branch reliably.
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=_coord_with()), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag()
        self.assertEqual(env['SIGMOND_RADIOD_COUNT'], '0')
        # No instance and no radiods → no INDEX.
        self.assertNotIn('SIGMOND_RADIOD_INDEX', env)

    def test_radiod_index_for_single(self):
        coord = _coord_with(
            radiods={'only': Radiod(id='only', status_dns='only.local')},
        )
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=coord), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag()
        self.assertEqual(env['SIGMOND_RADIOD_COUNT'], '1')
        self.assertEqual(env['SIGMOND_RADIOD_INDEX'], '1')

    def test_radiod_index_per_instance_in_declaration_order(self):
        # 'a' is declared first → index 1; 'b' → index 2.
        coord = _coord_with(
            radiods={
                'a': Radiod(id='a', status_dns='a.local'),
                'b': Radiod(id='b', status_dns='b.local'),
            },
            clients=[
                ClientInstance(client_type='psk-recorder',
                               instance='radiod-0', radiod_id='a'),
                ClientInstance(client_type='psk-recorder',
                               instance='radiod-1', radiod_id='b'),
            ],
        )
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=coord), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag(
                client='psk-recorder', instance='radiod-1')
        self.assertEqual(env['SIGMOND_RADIOD_COUNT'], '2')
        self.assertEqual(env['SIGMOND_RADIOD_INDEX'], '2')

    def test_radiod_index_omitted_when_ambiguous(self):
        # Multiple radiods declared but no instance arg → no INDEX.
        coord = _coord_with(
            radiods={
                'a': Radiod(id='a'),
                'b': Radiod(id='b'),
            },
        )
        with mock.patch.object(client_config, 'load_coordination',
                               return_value=coord), \
             mock.patch.object(client_config, '_resolve_time_source',
                               return_value=''), \
             mock.patch.object(client_config, '_resolve_gnss_vtec',
                               return_value=''):
            env = client_config._build_env_bag()
        self.assertEqual(env['SIGMOND_RADIOD_COUNT'], '2')
        self.assertNotIn('SIGMOND_RADIOD_INDEX', env)


# ---------------------------------------------------------------------------
# Entry-point invocation
# ---------------------------------------------------------------------------

class RunEntrypointTests(unittest.TestCase):
    def test_relative_path_resolves_against_repo_root(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            scripts = repo / 'scripts'
            scripts.mkdir()
            captured = repo / 'env-dump.txt'
            _make_executable(scripts / 'init.sh',
                f'#!/bin/sh\nenv | grep -E "^(STATION|SIGMOND)_" '
                f'| sort > "{captured}"\n')
            deploy = repo / 'deploy.toml'
            deploy.write_text('[contract.config]\ninit = "scripts/init.sh"\n')

            with mock.patch.object(client_config, '_build_env_bag',
                                   return_value={'STATION_CALL': 'W1ABC',
                                                 'STATION_GRID': 'FN42'}):
                rc = client_config._run_client_entrypoint(
                    'fake', deploy, 'scripts/init.sh', verb='init')

            self.assertEqual(rc, 0)
            body = captured.read_text()
            self.assertIn('STATION_CALL=W1ABC', body)
            self.assertIn('STATION_GRID=FN42', body)

    def test_absolute_path_used_directly(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            script = _make_executable(d_path / 'edit.sh',
                                      '#!/bin/sh\nexit 7\n')
            deploy = d_path / 'deploy.toml'
            deploy.write_text(f'[contract.config]\nedit = "{script}"\n')

            with mock.patch.object(client_config, '_build_env_bag',
                                   return_value={}):
                rc = client_config._run_client_entrypoint(
                    'fake', deploy, str(script), verb='edit')

        self.assertEqual(rc, 7)

    def test_missing_file_errors(self):
        with tempfile.TemporaryDirectory() as d:
            deploy = Path(d) / 'deploy.toml'
            deploy.write_text('[contract.config]\ninit = "missing.sh"\n')
            rc = client_config._run_client_entrypoint(
                'fake', deploy, 'missing.sh', verb='init')
        self.assertEqual(rc, 1)

    def test_argv_form_passes_extra_args(self):
        with tempfile.TemporaryDirectory() as d:
            d_path = Path(d)
            captured = d_path / 'argv.txt'
            script = _make_executable(d_path / 'cli.sh',
                f'#!/bin/sh\nprintf "%s\\n" "$@" > "{captured}"\n')
            deploy = d_path / 'deploy.toml'
            deploy.write_text('[contract.config]\n'
                              f'init = ["{script}", "config", "init"]\n')

            with mock.patch.object(client_config, '_build_env_bag',
                                   return_value={}):
                rc = client_config._run_client_entrypoint(
                    'fake', deploy,
                    [str(script), "config", "init"],
                    verb='init')

            self.assertEqual(rc, 0)
            self.assertEqual(captured.read_text().splitlines(),
                             ['config', 'init'])


# ---------------------------------------------------------------------------
# Fallback paths (no [contract.config])
# ---------------------------------------------------------------------------

class FallbackTests(unittest.TestCase):
    def test_init_fallback_points_at_render_template(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / 'config').mkdir()
            (repo / 'config' / 'foo.toml.template').write_text('# template\n')
            deploy = repo / 'deploy.toml'
            deploy.write_text(
                '[[install.steps]]\n'
                'kind = "render"\n'
                'src  = "config/foo.toml.template"\n'
                'dst  = "/etc/foo/foo.toml"\n'
            )
            rc = client_config._fallback('foo', deploy, verb='init')
        self.assertEqual(rc, 0)

    def test_edit_fallback_invokes_editor_on_config_path(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            cfg = repo / 'fake.toml'
            cfg.write_text('# placeholder\n')
            deploy = repo / 'deploy.toml'
            deploy.write_text(
                '[[install.steps]]\n'
                'kind = "render"\n'
                'src  = "tpl.toml"\n'
                f'dst  = "/etc/foo/foo.toml"\n'
            )

            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return SimpleNamespace(returncode=0)

            with mock.patch.dict(os.environ,
                                 {'EDITOR': '/bin/true'}, clear=False), \
                 mock.patch.object(client_config, '_config_path_from_inventory',
                                   return_value=cfg), \
                 mock.patch('subprocess.run', side_effect=fake_run):
                rc = client_config._fallback('foo', deploy, verb='edit')

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], '/bin/true')
        self.assertEqual(calls[0][1], str(cfg))

    def test_edit_fallback_errors_when_no_config_path(self):
        with tempfile.TemporaryDirectory() as d:
            deploy = Path(d) / 'deploy.toml'
            deploy.write_text('[package]\nname = "x"\n')

            with mock.patch.object(client_config, '_config_path_from_inventory',
                                   return_value=None):
                rc = client_config._fallback('foo', deploy, verb='edit')

        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------

class DispatchTests(unittest.TestCase):
    def test_no_client_arg_returns_2(self):
        ns = SimpleNamespace(client=None)
        self.assertEqual(client_config.cmd_config_init(ns), 2)
        self.assertEqual(client_config.cmd_config_edit_client(ns), 2)

    def test_unknown_client_returns_1(self):
        with mock.patch.object(client_config, '_find_deploy_toml',
                               return_value=None):
            ns = SimpleNamespace(client='not-a-client')
            self.assertEqual(client_config.cmd_config_init(ns), 1)


if __name__ == '__main__':
    unittest.main()
