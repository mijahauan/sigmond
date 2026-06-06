"""Unit tests for the bring-up plan builder (pure; no I/O)."""
from sigmond.catalog import Profile
from sigmond.bringup import build_plan, STAGE2, STAGE3A, STAGE3B


def _dasi2():
    return Profile(
        name='dasi2',
        clients=('hf-timestd', 'wspr-recorder', 'psk-recorder', 'mag-recorder'),
        local_radiod_infra=('igmp-querier', 'gpsdo-monitor'),
        optional=('ka9q-web',),
    )


def _labels(plan, kind=None):
    return [s.label for s in plan.steps if kind is None or s.kind == kind]


def test_local_plan_includes_radiod_stack_and_wisdom():
    p = build_plan(_dasi2(), local_radiod=True)
    installs = _labels(p, 'install')
    assert 'install ka9q-radio' in installs
    assert 'install igmp-querier' in installs
    assert 'install gpsdo-monitor' in installs
    assert any(s.kind == 'wisdom' for s in p.steps)
    assert any(s.kind == 'wait-wisdom' for s in p.steps)


def test_remote_plan_skips_radiod_stack_and_wisdom():
    p = build_plan(_dasi2(), local_radiod=False, remote_status_dns='x-status.local')
    installs = _labels(p, 'install')
    assert 'install ka9q-radio' not in installs
    assert 'install gpsdo-monitor' not in installs
    assert not any(s.kind == 'wait-wisdom' for s in p.steps)
    assert not any(s.kind == 'wisdom' for s in p.steps)
    # clients still install/configure against the remote radiod
    assert 'install wspr-recorder' in installs


def test_stage_assignment_timing_authority_and_independent():
    p = build_plan(_dasi2(), local_radiod=True)
    stage = {s.label: s.stage for s in p.steps if s.kind == 'config'}
    assert stage['configure hf-timestd'] == STAGE2       # timing authority first
    assert stage['configure mag-recorder'] == STAGE3B    # independent track
    assert stage['configure wspr-recorder'] == STAGE3A
    assert stage['configure psk-recorder'] == STAGE3A


def test_single_hard_checkpoint_is_radiod_configured():
    p = build_plan(_dasi2(), local_radiod=True)
    hard = [s for s in p.steps if s.hard]
    assert len(hard) == 1 and hard[0].check == 'radiod-configured'


def test_with_optional_toggles_ka9q_web():
    assert 'install ka9q-web' in _labels(
        build_plan(_dasi2(), local_radiod=True, with_optional=True), 'install')
    assert 'install ka9q-web' not in _labels(
        build_plan(_dasi2(), local_radiod=True, with_optional=False), 'install')


def test_start_is_last_action_and_final_checkpoint_is_validate():
    p = build_plan(_dasi2(), local_radiod=True)
    assert p.steps[-1].kind == 'checkpoint' and p.steps[-1].check == 'validate'
    assert any(s.kind == 'start' for s in p.steps)


def test_non_interactive_appends_flag_to_config_steps():
    p = build_plan(_dasi2(), local_radiod=True, non_interactive=True)
    cfg = [s for s in p.steps if s.kind == 'config']
    assert cfg and all('--non-interactive' in s.argv for s in cfg)
    p2 = build_plan(_dasi2(), local_radiod=True, non_interactive=False)
    assert all('--non-interactive' not in s.argv
               for s in p2.steps if s.kind == 'config')
