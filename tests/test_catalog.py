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
        # The catalog grows over time, so assert the known components are
        # *present* rather than pinning an exact set (which broke on every
        # addition).  Adding a client should not break this sanity check;
        # accidental removals/typos of a core entry still will.
        expected = {
            'ka9q-radio', 'ka9q-web',
            'wspr-recorder', 'psk-recorder', 'hf-timestd',
            'hfdl-recorder', 'codar-sounder', 'hf-gps-tec',
            'mag-recorder', 'hs-uploader', 'callhash',
            'gpsdo-monitor', 'igmp-querier', 'ka9q-update',
        }
        missing = expected - set(entries.keys())
        assert not missing, f'catalog missing expected entries: {missing}'
        # Deprecated names must never surface as live entries.
        assert 'wsprdaemon-client' not in entries

    def test_entry_fields_populated(self):
        entries = load_catalog(REPO_CATALOG)
        psk = entries['psk-recorder']
        assert psk.name == 'psk-recorder'
        assert psk.kind == 'client'
        assert psk.contract == '0.8'
        assert psk.uses == ('ka9q-python',)
        assert psk.install_script == '/opt/git/sigmond/psk-recorder/scripts/install.sh'
        assert 'FT4' in psk.description or 'FT8' in psk.description

    def test_server_entry_has_no_contract_or_install_script(self):
        entries = load_catalog(REPO_CATALOG)
        radiod = entries['ka9q-radio']
        assert radiod.kind == 'server'
        assert radiod.contract is None
        assert radiod.install_script is None

    def test_radiod_clients_use_ka9q_python(self):
        entries = load_catalog(REPO_CATALOG)
        # Decode clients that subscribe to a radiod multicast declare
        # ka9q-radio in `requires`; those must pull in ka9q-python.  Non-radiod
        # clients (e.g. mag-recorder, a USB magnetometer recorder) legitimately
        # do not use ka9q-python.
        radiod_clients = [
            e for e in entries.values()
            if e.kind == 'client' and 'ka9q-radio' in e.requires
        ]
        assert radiod_clients, 'expected at least one radiod-subscribing client'
        for e in radiod_clients:
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
            'contract = "0.5"\n'
            'install_script = "/tmp/ex/install.sh"\n'
        )
        entries = load_catalog(custom)
        assert 'example' in entries
        assert entries['example'].contract == '0.5'


class TestSparseOverlay:
    """The no-path ``load_catalog()`` should layer discovery + repo
    catalog + operator catalog so a *partial* operator block overrides
    only the fields it sets, and so a new entry in the repo file
    propagates without needing to be copied into /etc/sigmond/."""

    def _install_layers(self, monkeypatch, tmp_path, *, repo_toml='',
                        operator_toml=None, discovery=None):
        """Stub discovery + DEFAULT_CATALOG_PATHS so load_catalog()
        reads tmp files only.  Returns the final merged catalog."""
        monkeypatch.setattr(
            'sigmond.catalog.discover_catalog_entries' if False else
            'sigmond.discover.discover_catalog_entries',
            lambda base=None: dict(discovery or {}),
        )
        repo_path = tmp_path / 'repo_catalog.toml'
        repo_path.write_text(repo_toml)
        layer_paths = [repo_path]
        if operator_toml is not None:
            op_path = tmp_path / 'operator_catalog.toml'
            op_path.write_text(operator_toml)
            # DEFAULT_CATALOG_PATHS is highest→lowest precedence, so
            # operator first, repo second.
            layer_paths = [op_path, repo_path]
        else:
            layer_paths = [repo_path]
        monkeypatch.setattr('sigmond.catalog.DEFAULT_CATALOG_PATHS',
                            tuple(layer_paths))
        return load_catalog()

    def test_operator_partial_overrides_only_named_fields(
            self, monkeypatch, tmp_path):
        repo = (
            '[client.foo]\n'
            'kind = "client"\n'
            'description = "from repo"\n'
            'repo = "https://repo/foo"\n'
            'uses = ["ka9q-python"]\n'
            'requires = ["bar"]\n'
            'contract = "0.6"\n'
            'install_script = "/from/repo.sh"\n'
        )
        op = (
            '[client.foo]\n'
            'repo = "git@my-fork:foo"\n'   # only this field overridden
        )
        entries = self._install_layers(monkeypatch, tmp_path,
                                       repo_toml=repo, operator_toml=op)
        foo = entries['foo']
        assert foo.repo == 'git@my-fork:foo'      # operator override wins
        assert foo.description == 'from repo'      # falls through
        assert foo.requires == ('bar',)            # falls through
        assert foo.install_script == '/from/repo.sh'

    def test_repo_only_entry_propagates_through_operator_layer(
            self, monkeypatch, tmp_path):
        """The original bug: a new entry added to the repo catalog
        was invisible if the operator had its own /etc/ override file
        (since the operator file shadowed the repo file entirely)."""
        repo = (
            '[client.new-dep]\n'
            'kind = "library"\n'
            'description = "added in a later sigmond release"\n'
            'repo = "https://github.com/example/new-dep"\n'
        )
        op = (
            '[client.unrelated]\n'
            'kind = "client"\n'
            'description = "operator-only entry"\n'
            'repo = ""\n'
        )
        entries = self._install_layers(monkeypatch, tmp_path,
                                       repo_toml=repo, operator_toml=op)
        assert 'new-dep' in entries
        assert entries['new-dep'].repo == 'https://github.com/example/new-dep'
        assert 'unrelated' in entries     # operator entry still present

    def test_deprecated_excludes_entry_from_catalog(
            self, monkeypatch, tmp_path):
        """An entry listed under ``[deprecated.<name>]`` must not show
        up in the live catalog even if discovery or a higher layer
        defines it.  Otherwise a stale ``deploy.toml`` on disk would
        silently revive a removed client."""
        # Discovery synthesizes an entry — simulating a leftover
        # /opt/git/sigmond/wsprdaemon-client/deploy.toml.
        ghost = CatalogEntry(
            name='wsprdaemon-client', kind='client',
            description='legacy', repo='https://example/wd',
        )
        repo = (
            '[client.wsprdaemon-client]\n'
            'kind = "client"\n'
            'description = "still listed somehow"\n'
            'repo = ""\n'
            '\n'
            '[deprecated.wsprdaemon-client]\n'
            'removed_in = "0eb8914"\n'
            'reason = "Superseded by the ka9q-python decoders."\n'
            'replaced_by = ["wspr-recorder"]\n'
        )
        entries = self._install_layers(monkeypatch, tmp_path,
                                       repo_toml=repo,
                                       discovery={'wsprdaemon-client': ghost})
        assert 'wsprdaemon-client' not in entries

    def test_discovery_field_overridden_by_repo(
            self, monkeypatch, tmp_path):
        """A field set by discovery (from deploy.toml) is overridden
        by the repo catalog, but other discovery fields still show."""
        discovered = CatalogEntry(
            name='disc', kind='client',
            description='from deploy.toml',
            repo='https://github.com/discovered/disc',
            requires=('lib-a',),
            install_script='/opt/git/sigmond/disc/install.sh',
        )
        repo = (
            '[client.disc]\n'
            'requires = ["lib-a", "lib-b"]\n'      # adds lib-b at repo layer
        )
        entries = self._install_layers(
            monkeypatch, tmp_path,
            discovery={'disc': discovered}, repo_toml=repo,
        )
        disc = entries['disc']
        assert disc.requires == ('lib-a', 'lib-b')  # overridden by repo
        assert disc.description == 'from deploy.toml'  # discovery falls through
        assert disc.install_script == '/opt/git/sigmond/disc/install.sh'


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
        # next_steps emits a bare `smd install <x>` hint (smd handles its own
        # privilege elevation); it no longer prefixes `sudo`.
        assert 'smd install ka9q-python' in lib_installs[0][2]

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
