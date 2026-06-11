"""Tests for sigmond.instance per-instance config scaffolding."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.instance import (
    _config_from_shared, _LEGACY_SHARED_CONFIG,
)

# Every templated recorder must have a shared-config path so create_instance
# can seed a COMPLETE per-instance config — regression for sigmond#16, where
# psk-recorder seeded a thin stub (no [[radiod]] block → daemon crash-loop)
# because create_instance hardcoded /etc/<client>/config.toml and psk's shared
# config is /etc/psk-recorder/psk-recorder-config.toml.
TEMPLATED_RECORDERS = ('wspr-recorder', 'psk-recorder',
                       'hfdl-recorder', 'codar-sounder')


def test_shared_config_map_covers_every_templated_recorder():
    for client in TEMPLATED_RECORDERS:
        assert client in _LEGACY_SHARED_CONFIG, client


def test_psk_shared_config_is_not_plain_config_toml():
    # The exact mismatch that caused the bug: psk's shared config is NOT
    # /etc/psk-recorder/config.toml.
    p = _LEGACY_SHARED_CONFIG['psk-recorder']
    assert p == Path('/etc/psk-recorder/psk-recorder-config.toml')
    assert p.name != 'config.toml'


def test_config_from_shared_keeps_radiod_block_and_prepends_instance():
    shared = (
        '[recorder]\n'
        'callsign = "AC0G"\n\n'
        '[[radiod]]\n'
        'status = "sigma-rx888mk2-status.local"\n\n'
        '[radiod.ft8]\n'
        'freq_hz = [14074000]\n'
    )
    out = _config_from_shared('psk-recorder', 'AC0G=S', shared)
    # the full shared body (the [[radiod]] block the daemon needs) is preserved
    assert '[[radiod]]' in out
    assert 'status = "sigma-rx888mk2-status.local"' in out
    assert '[radiod.ft8]' in out
    # and an [instance] block with the reporter id is prepended
    assert '[instance]' in out
    assert 'reporter_id = "AC0G=S"' in out
    assert out.index('[instance]') < out.index('[[radiod]]')


def test_config_from_shared_is_noop_when_instance_block_present():
    shared = '[instance]\nreporter_id = "X"\n\n[[radiod]]\nstatus = "s"\n'
    assert _config_from_shared('psk-recorder', 'AC0G=S', shared) == shared


if __name__ == '__main__':
    import unittest
    unittest.main()
