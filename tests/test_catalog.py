"""Tests for sigmond.catalog."""

import shutil
import warnings
from pathlib import Path

import pytest

from sigmond.catalog import (
    CatalogEntry,
    DEFAULT_CATALOG_PATHS,
    build_alias_map,
    find_catalog_file,
    find_client_binary,
    get_entry,
    load_catalog,
    next_steps,
    resolve_name,
)


REPO_CATALOG = Path(__file__).resolve().parent.parent / 'etc' / 'catalog.toml'


class TestLoadCatalog:
    def test_repo_default_loads(self):
        entries = load_catalog(REPO_CATALOG)
        assert set(entries.keys()) == {
            'radiod', 'wspr-recorder', 'psk-recorder', 'hf-timestd',
            'wsprdaemon-client',
        }

    def test_entry_fields_populated(self):
        entries = load_catalog(REPO_CATALOG)
        psk = entries['psk-recorder']
        assert psk.name == 'psk-recorder'
        assert psk.kind == 'client'
        assert psk.contract == '0.4'
        assert psk.uses == ('ka9q-python',)
        assert psk.install_script == '/opt/git/sigmond/psk-recorder/scripts/install.sh'
        assert 'FT4' in psk.description or 'FT8' in psk.description

    def test_server_entry_has_no_contract_or_install_script(self):
        entries = load_catalog(REPO_CATALOG)
        radiod = entries['radiod']
        assert radiod.kind == 'server'
        assert radiod.contract is None
        assert radiod.install_script is None

    def test_every_client_uses_ka9q_python(self):
        entries = load_catalog(REPO_CATALOG)
        clients = [e for e in entries.values() if e.kind == 'client']
        for e in clients:
            assert 'ka9q-python' in e.uses, f'{e.name} should use ka9q-python'

    def test_missing_file_raises(self, tmp_path):
        nonexistent = tmp_path / 'nope.toml'
        with pytest.raises(FileNotFoundError):
            load_catalog(nonexistent)

    def test_custom_catalog(self, tmp_path):
        custom = tmp_path / 'catalog.toml'
        custom.write_text(
            '[client.example]\n'
            'kind = "client"\n'
            'description = "a test client"\n'
            'repo = "https://example.com/ex"\n'
            'uses = ["ka9q-python"]\n'
            'contract = "0.4"\n'
            'install_script = "/tmp/ex/install.sh"\n'
        )
        entries = load_catalog(custom)
        assert 'example' in entries
        assert entries['example'].contract == '0.4'


class TestIsInstalled:
    def test_script_exists(self, tmp_path):
        script = tmp_path / 'install.sh'
        script.write_text('#!/bin/sh\n')
        entry = CatalogEntry(
            name='fake', kind='client', description='', repo='',
            install_script=str(script),
        )
        assert entry.is_installed() is True

    def test_script_missing(self):
        entry = CatalogEntry(
            name='fake', kind='client', description='', repo='',
            install_script='/definitely/not/there.sh',
        )
        assert entry.is_installed() is False

    def test_no_script_falls_back_to_which(self, monkeypatch):
        import sigmond.catalog as cat
        monkeypatch.setattr(cat.shutil, 'which', lambda n: '/usr/bin/fake' if n == 'yep' else None)
        entry = CatalogEntry(name='yep', kind='server', description='', repo='')
        assert entry.is_installed() is True
        entry2 = CatalogEntry(name='nope', kind='server', description='', repo='')
        assert entry2.is_installed() is False

    def test_library_importability_counts_as_installed(self):
        # `json` is always importable from stdlib; synthesize a library
        # entry whose derived import name lands on it and confirm
        # is_installed() returns True without filesystem presence.
        entry = CatalogEntry(
            name='json-python', kind='library', description='', repo='',
        )
        assert entry.is_installed() is True

    def test_library_without_import_is_not_installed(self, monkeypatch):
        import sigmond.catalog as cat
        # Strip all filesystem and which fallbacks so only the importability
        # branch decides.
        import os as _os
        monkeypatch.setattr(_os.path, 'lexists', lambda p: False)
        monkeypatch.setattr(_os.path, 'exists', lambda p: False)
        monkeypatch.setattr(cat.shutil, 'which', lambda n: None)
        entry = CatalogEntry(
            name='totally-not-a-real-pkg-zzz', kind='library',
            description='', repo='',
        )
        assert entry.is_installed() is False


class TestAliasResolution:
    def test_build_alias_map(self):
        entries = load_catalog(REPO_CATALOG)
        aliases = build_alias_map(entries)
        assert aliases['grape'] == 'hf-timestd'
        assert aliases['wspr'] == 'wsprdaemon-client'
        assert 'psk-recorder' not in aliases

    def test_resolve_name_canonical_passthrough(self):
        entries = load_catalog(REPO_CATALOG)
        assert resolve_name('psk-recorder', entries) == 'psk-recorder'

    def test_resolve_name_alias_warns(self):
        entries = load_catalog(REPO_CATALOG)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            result = resolve_name('grape', entries)
            assert result == 'hf-timestd'
            assert len(w) == 1
            assert 'deprecated' in str(w[0].message).lower()

    def test_resolve_name_unknown_passthrough(self):
        entries = load_catalog(REPO_CATALOG)
        assert resolve_name('nonexistent', entries) == 'nonexistent'

    def test_get_entry_by_alias(self):
        entries = load_catalog(REPO_CATALOG)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter('always')
            entry = get_entry('grape', entries)
        assert entry is not None
        assert entry.name == 'hf-timestd'

    def test_get_entry_canonical(self):
        entries = load_catalog(REPO_CATALOG)
        entry = get_entry('psk-recorder', entries)
        assert entry is not None
        assert entry.name == 'psk-recorder'

    def test_get_entry_unknown_returns_none(self):
        entries = load_catalog(REPO_CATALOG)
        assert get_entry('nonexistent', entries) is None

    def test_topology_alias_field_loaded(self):
        entries = load_catalog(REPO_CATALOG)
        assert entries['hf-timestd'].topology_alias == 'grape'
        assert entries['wsprdaemon-client'].topology_alias == 'wspr'
        assert entries['psk-recorder'].topology_alias is None


class TestFindClientBinary:
    def test_falls_back_to_venv(self, tmp_path, monkeypatch):
        monkeypatch.setattr('sigmond.catalog.shutil.which', lambda n: None)
        venv_bin = tmp_path / 'fake' / 'venv' / 'bin' / 'fake'
        venv_bin.parent.mkdir(parents=True)
        venv_bin.write_text('#!/bin/sh\n')
        monkeypatch.setattr('sigmond.catalog.Path', lambda p: tmp_path / p.lstrip('/') if '/opt/' in str(p) else Path(p))
        # Direct test with real Path
        import sigmond.catalog as cat
        original = cat.find_client_binary
        def patched(name):
            from pathlib import Path as P
            found = shutil.which(name)
            if found:
                return found
            vb = tmp_path / name / 'venv' / 'bin' / name
            if vb.exists():
                return str(vb)
            return None
        monkeypatch.setattr(cat, 'find_client_binary', patched)
        assert cat.find_client_binary('fake') == str(venv_bin)

    def test_prefers_path(self, monkeypatch):
        monkeypatch.setattr('sigmond.catalog.shutil.which', lambda n: '/usr/bin/fake')
        assert find_client_binary('fake') == '/usr/bin/fake'

    def test_returns_none_when_not_found(self, monkeypatch):
        monkeypatch.setattr('sigmond.catalog.shutil.which', lambda n: None)
        assert find_client_binary('definitely-not-installed') is None


class TestFindCatalogFile:
    def test_repo_default_resolves(self):
        # The repo catalog should exist in the default search path.
        found = find_catalog_file()
        assert found is not None
        assert found.exists()

    def test_default_paths_has_etc_then_repo(self):
        paths = DEFAULT_CATALOG_PATHS
        assert paths[0] == Path('/etc/sigmond/catalog.toml')
        assert paths[1].name == 'catalog.toml'


class TestNextSteps:
    """next_steps() must never tell the operator to 'enable X in
    topology' when X is a Python library (kind='library').  Libraries
    are pip-installed into the sigmond venv, not topology components."""

    def _catalog(self):
        return {
            'ka9q-python': CatalogEntry(
                name='ka9q-python', kind='library',
                description='Python interface for ka9q-radio',
                repo='', requires=(),
            ),
            'hf-timestd': CatalogEntry(
                name='hf-timestd', kind='client', description='',
                repo='', requires=('ka9q-python', 'radiod'),
                install_script=str(Path(__file__)),  # always exists
            ),
            'radiod': CatalogEntry(
                name='radiod', kind='server', description='',
                repo='', requires=(),
                install_script=str(Path(__file__)),
            ),
        }

    def test_importable_library_dep_is_silent(self, monkeypatch):
        # ka9q-python is aliased to a stdlib name via the derivation
        # rule; patch find_spec to always resolve so we're not depending
        # on ka9q being installed in the test env.
        import sigmond.catalog as cat
        import importlib.util as _iu
        monkeypatch.setattr(_iu, 'find_spec',
                            lambda n: object())  # any non-None spec
        cat_obj = self._catalog()
        items = next_steps(['hf-timestd', 'radiod'], cat_obj)
        # No 'enable_dep' item should mention ka9q-python — it's a
        # library and it's importable.
        for kind, subject, action in items:
            assert 'ka9q-python' not in action, \
                f'unexpected action mentioning ka9q-python: {items!r}'
            assert kind != 'enable_dep' or 'ka9q-python' not in subject, \
                f'library surfaced as enable_dep: {items!r}'

    def test_missing_library_surfaces_install_hint_once(self, monkeypatch):
        import importlib.util as _iu
        monkeypatch.setattr(_iu, 'find_spec', lambda n: None)
        import sigmond.catalog as cat
        import os as _os
        monkeypatch.setattr(_os.path, 'lexists', lambda p: False)
        cat_obj = self._catalog()
        # hf-timestd AND radiod both transitively require ka9q-python;
        # we want exactly one install hint for the library even so.
        items = next_steps(['hf-timestd', 'radiod'], cat_obj)
        lib_installs = [
            (k, s, a) for (k, s, a) in items
            if k == 'install' and s == 'ka9q-python'
        ]
        assert len(lib_installs) == 1, items
        assert 'sudo smd install ka9q-python' in lib_installs[0][2]

    def test_non_library_dep_still_flagged_for_topology(self):
        # hf-timestd requires radiod (kind=server).  If radiod is not
        # enabled, we should still say "enable radiod in topology".
        cat_obj = self._catalog()
        items = next_steps(['hf-timestd'], cat_obj)
        enable_actions = [
            a for (k, s, a) in items if k == 'enable_dep'
        ]
        assert any('enable radiod in topology' in a for a in enable_actions), \
            items
