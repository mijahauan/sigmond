"""Tests for sigmond.instance per-instance config scaffolding."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

from sigmond.instance import (
    _config_from_shared, _LEGACY_SHARED_CONFIG,
    _instance_env_defaults, _env_stub,
    display_reporter_id, parse_user_reporter_id,
)

_WSPR_DEPLOY = Path('/opt/git/sigmond/wspr-recorder/deploy.toml')

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


def test_reporter_call_round_trip():
    # The on-air slash form round-trips through the path-safe '=' storage form.
    assert parse_user_reporter_id('AC0G/S') == 'AC0G=S'
    assert display_reporter_id('AC0G=S') == 'AC0G/S'
    # Hyphenated SSIDs are user-intentional and stay untouched.
    assert display_reporter_id('W1ABC-5') == 'W1ABC-5'


def test_instance_env_substitutes_reporter_call_placeholder():
    # {reporter_call} in a client's [contract.instance_env] expands to the
    # display (slash) form; {reporter_id} stays the path-safe storage form.
    # Verified against wspr-recorder's real deploy.toml, which declares
    # WD_RECEIVER_CALL = "{reporter_call}" so the internal '=' never reaches
    # wsprnet (the AC0G=S vs AC0G/S reporter-id question).
    if not _WSPR_DEPLOY.exists():
        return  # wspr-recorder checkout absent in this env; nothing to assert
    env = _instance_env_defaults('wspr-recorder', 'AC0G=S')
    assert env['WD_DECODE_VIA_DB'] == '1'
    assert env['WD_RECEIVER_CALL'] == 'AC0G/S'
    assert '{reporter_call}' not in env['WD_RECEIVER_CALL']
    # And it lands in the rendered per-instance env file.
    stub = _env_stub('wspr-recorder', 'AC0G=S')
    assert 'WD_RECEIVER_CALL=AC0G/S' in stub


if __name__ == '__main__':
    import unittest
    unittest.main()
