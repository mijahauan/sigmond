"""Tests for sigmond.commands.radiod_fragments — Wave 3 channel-fragment applier."""

from pathlib import Path

import pytest

from sigmond.commands.radiod_fragments import (
    FragmentSpec,
    apply_fragments,
    collect_fragments,
)
from sigmond.coordination import Coordination, Host, Radiod


def _coord(radiods=None, call="N0CALL", grid="EM38"):
    coord = Coordination()
    coord.host = Host(call=call, grid=grid)
    if radiods:
        for rid, host in radiods.items():
            coord.radiods[rid] = Radiod(
                id=rid, host=host,
                status_dns=f"{rid}-status.local",
                samprate_hz=24000,
            )
    return coord


def _write_client(tmp_path: Path, name: str, deploy_body: str,
                  template_name: str = "etc/radiod-fragment.conf",
                  template_body: str = "[ch.${RADIOD_ID}]\nfreq = 14095600\n"):
    """Lay out /opt/git/<name>/{deploy.toml, etc/radiod-fragment.conf}."""
    repo = tmp_path / name
    (repo / 'etc').mkdir(parents=True, exist_ok=True)
    (repo / 'deploy.toml').write_text(deploy_body)
    (repo / template_name).write_text(template_body)
    return repo


# ---------------------------------------------------------------------------
# collect_fragments
# ---------------------------------------------------------------------------

class TestCollectFragments:

    def test_no_components(self, tmp_path):
        assert collect_fragments([], git_base=tmp_path) == []

    def test_skips_components_without_deploy_toml(self, tmp_path):
        (tmp_path / 'foo').mkdir()
        assert collect_fragments(['foo'], git_base=tmp_path) == []

    def test_skips_components_without_radiod_block(self, tmp_path):
        _write_client(tmp_path, 'foo',
                      '[package]\nname = "foo"\nversion = "0.1"\n')
        assert collect_fragments(['foo'], git_base=tmp_path) == []

    def test_collects_one_fragment(self, tmp_path):
        _write_client(tmp_path, 'psk-recorder', """
[package]
name = "psk-recorder"

[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""")
        specs = collect_fragments(['psk-recorder'], git_base=tmp_path)
        assert len(specs) == 1
        s = specs[0]
        assert s.client == 'psk-recorder'
        assert s.priority == 30
        assert s.target == '*'
        assert s.template_path == tmp_path / 'psk-recorder' / 'etc' / 'radiod-fragment.conf'
        assert s.filename == '30-psk-recorder.conf'

    def test_collects_multiple_fragments(self, tmp_path):
        _write_client(tmp_path, 'multi', """
[package]
name = "multi"

[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/a.conf"

[[radiod.fragment]]
priority = 40
target = "rx888-1"
template = "etc/b.conf"
""")
        (tmp_path / 'multi' / 'etc' / 'a.conf').write_text("a")
        (tmp_path / 'multi' / 'etc' / 'b.conf').write_text("b")
        specs = collect_fragments(['multi'], git_base=tmp_path)
        assert {s.priority for s in specs} == {30, 40}
        assert {s.target for s in specs} == {'*', 'rx888-1'}

    def test_accepts_legacy_content_template_key(self, tmp_path):
        """The plan named the field `content_template`; accept that as
        an alias for `template` so prototype clients don't break."""
        _write_client(tmp_path, 'legacy', """
[[radiod.fragment]]
priority = 50
target = "*"
content_template = "etc/radiod-fragment.conf"
""")
        specs = collect_fragments(['legacy'], git_base=tmp_path)
        assert len(specs) == 1

    def test_skips_fragment_without_template(self, tmp_path):
        _write_client(tmp_path, 'broken', """
[[radiod.fragment]]
priority = 50
target = "*"
""")
        assert collect_fragments(['broken'], git_base=tmp_path) == []

    def test_handles_unparseable_deploy_toml(self, tmp_path):
        _write_client(tmp_path, 'bad', "this is not valid TOML !!! [[\n")
        assert collect_fragments(['bad'], git_base=tmp_path) == []

    def test_default_priority(self, tmp_path):
        _write_client(tmp_path, 'p', """
[[radiod.fragment]]
target = "*"
template = "etc/radiod-fragment.conf"
""")
        specs = collect_fragments(['p'], git_base=tmp_path)
        assert specs[0].priority == 50  # _DEFAULT_PRIORITY


# ---------------------------------------------------------------------------
# apply_fragments
# ---------------------------------------------------------------------------

class TestApplyFragments:

    def _setup(self, tmp_path, radiods=None, **kwargs):
        if radiods is None:
            radiods = {'rx888-1': 'localhost'}
        coord = _coord(radiods=radiods)
        config_dir = tmp_path / 'etc-radio'
        config_dir.mkdir()
        return coord, config_dir

    def test_no_fragments_returns_empty(self, tmp_path):
        coord, config_dir = self._setup(tmp_path)
        msgs = apply_fragments(coord, [], git_base=tmp_path, config_dir=config_dir)
        assert msgs == []

    def test_no_radiods_warns(self, tmp_path):
        coord, config_dir = self._setup(tmp_path, radiods={})
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""")
        msgs = apply_fragments(coord, ['psk-recorder'],
                               git_base=tmp_path, config_dir=config_dir)
        assert any('no radiod instances' in m for m in msgs)

    def test_writes_one_fragment(self, tmp_path):
        coord, config_dir = self._setup(tmp_path)
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""", template_body="[ch.PSK]\nfreq = 14095600\nradiod = ${RADIOD_ID}\n")
        msgs = apply_fragments(coord, ['psk-recorder'],
                               git_base=tmp_path, config_dir=config_dir)
        target = config_dir / 'radiod@rx888-1.conf.d' / '30-psk-recorder.conf'
        assert target.exists()
        body = target.read_text()
        assert 'radiod = rx888-1' in body
        assert any('wrote' in m for m in msgs)

    def test_idempotent_on_second_run(self, tmp_path):
        """Two consecutive applies — second produces 'unchanged' lines, no writes."""
        coord, config_dir = self._setup(tmp_path)
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""")
        apply_fragments(coord, ['psk-recorder'],
                        git_base=tmp_path, config_dir=config_dir)
        target = config_dir / 'radiod@rx888-1.conf.d' / '30-psk-recorder.conf'
        first_mtime = target.stat().st_mtime_ns

        msgs = apply_fragments(coord, ['psk-recorder'],
                               git_base=tmp_path, config_dir=config_dir)
        assert all('unchanged' in m for m in msgs)
        assert target.stat().st_mtime_ns == first_mtime

    def test_dry_run_does_not_write(self, tmp_path):
        coord, config_dir = self._setup(tmp_path)
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""")
        msgs = apply_fragments(coord, ['psk-recorder'],
                               git_base=tmp_path, config_dir=config_dir,
                               dry_run=True)
        target = config_dir / 'radiod@rx888-1.conf.d' / '30-psk-recorder.conf'
        assert not target.exists()
        assert any('would create' in m for m in msgs)

    def test_dry_run_distinguishes_update_from_create(self, tmp_path):
        coord, config_dir = self._setup(tmp_path)
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""", template_body="content v1\n")
        apply_fragments(coord, ['psk-recorder'],
                        git_base=tmp_path, config_dir=config_dir)
        # bump the template; dry-run should report 'would update'
        (tmp_path / 'psk-recorder' / 'etc' / 'radiod-fragment.conf').write_text("content v2\n")
        msgs = apply_fragments(coord, ['psk-recorder'],
                               git_base=tmp_path, config_dir=config_dir,
                               dry_run=True)
        assert any('would update' in m for m in msgs)

    def test_target_star_writes_to_every_radiod(self, tmp_path):
        coord, config_dir = self._setup(tmp_path,
            radiods={'rx888-1': 'localhost', 'rx888-2': 'remote'})
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""")
        apply_fragments(coord, ['psk-recorder'],
                        git_base=tmp_path, config_dir=config_dir)
        assert (config_dir / 'radiod@rx888-1.conf.d' / '30-psk-recorder.conf').exists()
        assert (config_dir / 'radiod@rx888-2.conf.d' / '30-psk-recorder.conf').exists()

    def test_target_radiod_id_substitution_broadcasts(self, tmp_path):
        """target = "${RADIOD_ID}" means apply to every declared radiod,
        with the variable filled in per-write."""
        coord, config_dir = self._setup(tmp_path,
            radiods={'rx888-1': 'localhost', 'rx888-2': 'remote'})
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "${RADIOD_ID}"
template = "etc/radiod-fragment.conf"
""", template_body="bound = ${RADIOD_ID}\n")
        apply_fragments(coord, ['psk-recorder'],
                        git_base=tmp_path, config_dir=config_dir)
        b1 = (config_dir / 'radiod@rx888-1.conf.d' / '30-psk-recorder.conf').read_text()
        b2 = (config_dir / 'radiod@rx888-2.conf.d' / '30-psk-recorder.conf').read_text()
        assert 'bound = rx888-1' in b1
        assert 'bound = rx888-2' in b2

    def test_target_literal_id_writes_only_there(self, tmp_path):
        coord, config_dir = self._setup(tmp_path,
            radiods={'rx888-1': 'localhost', 'rx888-2': 'remote'})
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "rx888-2"
template = "etc/radiod-fragment.conf"
""")
        apply_fragments(coord, ['psk-recorder'],
                        git_base=tmp_path, config_dir=config_dir)
        assert not (config_dir / 'radiod@rx888-1.conf.d').exists()
        assert (config_dir / 'radiod@rx888-2.conf.d' / '30-psk-recorder.conf').exists()

    def test_target_unknown_id_warns(self, tmp_path):
        coord, config_dir = self._setup(tmp_path,
            radiods={'rx888-1': 'localhost'})
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "nonexistent-rx"
template = "etc/radiod-fragment.conf"
""")
        msgs = apply_fragments(coord, ['psk-recorder'],
                               git_base=tmp_path, config_dir=config_dir)
        assert any("did not resolve" in m for m in msgs)

    def test_radiod_id_arg_scopes_to_one_instance(self, tmp_path):
        """The smd config init radiod hook passes radiod_id to apply
        fragments only for the freshly-created instance."""
        coord, config_dir = self._setup(tmp_path,
            radiods={'rx888-1': 'localhost', 'rx888-2': 'remote'})
        _write_client(tmp_path, 'psk-recorder', """
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""")
        apply_fragments(coord, ['psk-recorder'],
                        git_base=tmp_path, config_dir=config_dir,
                        radiod_id='rx888-2')
        assert not (config_dir / 'radiod@rx888-1.conf.d').exists()
        assert (config_dir / 'radiod@rx888-2.conf.d' / '30-psk-recorder.conf').exists()

    def test_missing_template_warns_but_continues(self, tmp_path):
        coord, config_dir = self._setup(tmp_path)
        repo = tmp_path / 'broken'
        repo.mkdir()
        (repo / 'deploy.toml').write_text("""
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/missing.conf"
""")
        msgs = apply_fragments(coord, ['broken'],
                               git_base=tmp_path, config_dir=config_dir)
        assert any('not readable' in m for m in msgs)

    def test_station_variables_available_in_template(self, tmp_path):
        coord = _coord(radiods={'rx888-1': 'localhost'},
                       call='AC0G', grid='EM38ww')
        config_dir = tmp_path / 'etc-radio'
        config_dir.mkdir()
        _write_client(tmp_path, 'p', """
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""", template_body="call = ${STATION_CALL}\ngrid = ${STATION_GRID}\n")
        apply_fragments(coord, ['p'],
                        git_base=tmp_path, config_dir=config_dir)
        body = (config_dir / 'radiod@rx888-1.conf.d' / '30-p.conf').read_text()
        assert 'call = AC0G' in body
        assert 'grid = EM38ww' in body

    def test_missing_var_in_template_left_as_placeholder(self, tmp_path):
        """safe_substitute leaves unknown ${VAR} tokens in place — no crash."""
        coord, config_dir = self._setup(tmp_path)
        _write_client(tmp_path, 'p', """
[[radiod.fragment]]
priority = 30
target = "*"
template = "etc/radiod-fragment.conf"
""", template_body="value = ${TOTALLY_UNKNOWN_VAR}\n")
        apply_fragments(coord, ['p'],
                        git_base=tmp_path, config_dir=config_dir)
        body = (config_dir / 'radiod@rx888-1.conf.d' / '30-p.conf').read_text()
        assert '${TOTALLY_UNKNOWN_VAR}' in body
