"""Unit tests for the bring-up plan builder (pure; no I/O)."""
from sigmond.catalog import Profile
from sigmond.bringup import (
    build_plan, STAGE2, STAGE3A, STAGE3B, STAGE4, CLIENT_STAGGER_S,
)


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


def test_non_interactive_flag_makes_every_config_step_non_interactive():
    p = build_plan(_dasi2(), local_radiod=True, non_interactive=True)
    cfg = [s for s in p.steps if s.kind == 'config']
    assert cfg and all('--non-interactive' in s.argv for s in cfg)


def test_client_config_is_non_interactive_by_default_but_radiod_is_not():
    # Default bring-up: client config interviews run --non-interactive (their
    # own wizards can't run inside bring-up's nested terminal), but radiod —
    # sigmond's own inline text wizard — stays interactive.
    p = build_plan(_dasi2(), local_radiod=True, non_interactive=False)
    cfg = {s.label: s for s in p.steps if s.kind == 'config'}
    assert '--non-interactive' not in cfg['configure radiod'].argv
    for client in ('configure hf-timestd', 'configure wspr-recorder',
                   'configure psk-recorder', 'configure mag-recorder'):
        assert '--non-interactive' in cfg[client].argv, client


def test_skip_excludes_hardware_gated_client():
    # Environment-aware: a client whose hardware is absent (e.g. mag-recorder
    # with no magnetometer) is skipped, not scaffolded.
    p = build_plan(_dasi2(), local_radiod=True, skip=frozenset({'mag-recorder'}))
    assert 'install mag-recorder' not in _labels(p, 'install')
    assert 'configure mag-recorder' not in _labels(p, 'config')
    # the other clients are unaffected
    assert 'install wspr-recorder' in _labels(p, 'install')


def _stage4(plan):
    return [s for s in plan.steps if s.stage == STAGE4]


def test_stage4_streaming_gate_between_radiod_and_clients():
    # Local radiod: the radiod stack starts, THEN a streaming gate, THEN the
    # first radiod-bound client.  A client must never provision against a
    # not-yet-streaming radiod.
    s4 = _stage4(build_plan(_dasi2(), local_radiod=True))
    kinds = [s.kind for s in s4]
    assert kinds.count('wait-streaming') == 1
    gate = next(i for i, s in enumerate(s4) if s.kind == 'wait-streaming')
    radiod_start = next(i for i, s in enumerate(s4)
                        if s.kind == 'start' and 'radiod' in s.label)
    first_client = next(i for i, s in enumerate(s4) if '(staggered)' in s.label)
    assert radiod_start < gate < first_client


def test_stage4_radiod_bound_clients_are_staggered():
    s4 = _stage4(build_plan(_dasi2(), local_radiod=True))
    staggered = [s for s in s4 if '(staggered)' in s.label]
    # hf-timestd, wspr, psk (mag is independent); hf-timestd first.
    assert [s.argv[-1] for s in staggered] == [
        'hf-timestd', 'wspr-recorder', 'psk-recorder']
    assert all(s.settle_s == CLIENT_STAGGER_S for s in staggered)


def test_stage4_independent_client_started_unstaggered_after_bound():
    # mag-recorder present (not skipped): started on the independent track,
    # not gated/staggered against radiod.
    s4 = _stage4(build_plan(_dasi2(), local_radiod=True))
    indep = [s for s in s4 if '(independent)' in s.label]
    assert [s.argv[-1] for s in indep] == ['mag-recorder']
    assert all(s.settle_s == 0 for s in indep)


def test_stage4_remote_has_no_streaming_gate_but_still_staggers():
    s4 = _stage4(build_plan(_dasi2(), local_radiod=False,
                            remote_status_dns='x-status.local'))
    assert not any(s.kind == 'wait-streaming' for s in s4)
    assert not any('radiod' in s.label and s.kind == 'start' for s in s4)
    staggered = [s for s in s4 if '(staggered)' in s.label]
    assert staggered and all(s.settle_s == CLIENT_STAGGER_S for s in staggered)


def test_stage4_final_sweep_start_then_validate():
    s4 = _stage4(build_plan(_dasi2(), local_radiod=True))
    # last two stage-4 steps: an argument-less `smd start` sweep, then validate.
    assert s4[-1].kind == 'checkpoint' and s4[-1].check == 'validate'
    sweep = s4[-2]
    assert sweep.kind == 'start' and '--components' not in sweep.argv
