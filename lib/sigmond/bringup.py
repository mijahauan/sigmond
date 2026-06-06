"""Bring-up orchestration engine (install-orchestration design, Phase B).

Turns a catalog ``Profile`` into an ordered, conditional sequence of bring-up
``Step``s — install -> per-client config interview -> background FFT wisdom ->
start, with checkpoints between.  Plan-building (:func:`build_plan`) is pure and
unit-testable; execution lives in ``smd`` (``cmd_bringup``) so this module stays
free of subprocess/TTY/root concerns and the TUI can render a plan before running
it.

See docs/install-orchestration-design.md for the staged model this implements.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# FFT wisdom artifact radiod imports at startup; Stage 4 waits on it (local).
WISDOM_FILE = Path('/etc/fftw/wisdomf')

# Stage labels (also the progress-group headers the executor prints).
STAGE1 = 'Stage 1 — radiod + host tuning'
STAGE2 = 'Stage 2 — hf-timestd (timing authority)'
STAGE3A = 'Stage 3a — radiod-bound clients'
STAGE3B = 'Stage 3b — independent clients'
STAGE4 = 'Stage 4 — start + verify'

# hf-timestd gets its own stage (timing authority, before consumers);
# mag-recorder is radiod-independent (§16) and runs on the 3b track.  Both are
# therefore skipped by the radiod-bound 3a loop.
_TIMING_AUTHORITY = 'hf-timestd'
_INDEPENDENT = frozenset({'mag-recorder'})


@dataclass
class Step:
    """One bring-up action.  ``argv`` is what the executor runs (an ``smd`` or
    ``systemctl`` invocation); empty for ``note``/``wait-wisdom`` steps that the
    executor handles specially."""
    stage: str
    label: str
    kind: str                       # install|config|tune|wisdom|start|checkpoint|note|wait-wisdom
    argv: list = field(default_factory=list)
    hard: bool = False              # checkpoint: abort the run on failure
    background: bool = False         # fire-and-don't-wait
    check: str = ''                 # checkpoint probe id (see executor._probe)


@dataclass
class Plan:
    profile: str
    local_radiod: bool
    remote_status_dns: Optional[str]
    steps: list = field(default_factory=list)


def build_plan(profile, *, local_radiod: bool,
               remote_status_dns: Optional[str] = None,
               smd: str = 'smd', with_optional: bool = False,
               non_interactive: bool = False) -> Plan:
    """Pure: a profile + radiod locality -> the ordered Step list.

    ``local_radiod`` gates the entire radiod stack (infra, ka9q-radio, tuning,
    radiod config, FFT wisdom).  When False the host binds a remote radiod and
    none of that — nor the wisdom wait — applies.  ``mag-recorder`` is emitted
    on the independent 3b track regardless of locality.
    """
    steps: list = []

    def install(stage: str, comp: str) -> None:
        steps.append(Step(stage, f'install {comp}', 'install',
                          argv=[smd, 'install', '--components', comp, '--yes']))

    def configure(stage: str, client: str) -> None:
        argv = [smd, 'config', 'init', client]
        label = f'configure {client}'
        if non_interactive:
            argv.append('--non-interactive')
            label += ' (non-interactive)'
        steps.append(Step(stage, label, 'config', argv=argv))

    def checkpoint(stage: str, label: str, check: str, hard: bool = False) -> None:
        steps.append(Step(stage, f'checkpoint: {label}', 'checkpoint',
                          check=check, hard=hard))

    # --- Stage 1: radiod stack (local only) ---
    if local_radiod:
        for infra in profile.local_radiod_infra:
            install(STAGE1, infra)
        install(STAGE1, 'ka9q-radio')
        if with_optional:
            for opt in profile.optional:
                install(STAGE1, opt)
        steps.append(Step(STAGE1, 'apply host tuning (affinity / governor / rmem)',
                          'tune', argv=[smd, 'apply']))
        configure(STAGE1, 'radiod')
        steps.append(Step(STAGE1, 'launch FFT wisdom planner', 'wisdom',
                          argv=['systemctl', 'start', '--no-block',
                                'sigmond-wisdom.service'], background=True))
        checkpoint(STAGE1, 'radiod configured', check='radiod-configured', hard=True)
    else:
        dns = remote_status_dns or 'auto-discover'
        steps.append(Step(STAGE1, f'using remote radiod ({dns}) — skipping radiod '
                          'stack, gpsdo-monitor, and FFT wisdom', 'note'))

    # --- Stage 2: hf-timestd (timing authority; radiod-bound) ---
    if _TIMING_AUTHORITY in profile.clients:
        install(STAGE2, _TIMING_AUTHORITY)
        configure(STAGE2, _TIMING_AUTHORITY)
        checkpoint(STAGE2, 'hf-timestd configured', check=f'configured:{_TIMING_AUTHORITY}')

    # --- Stage 3a: radiod-bound spot clients ---
    for client in profile.clients:
        if client == _TIMING_AUTHORITY or client in _INDEPENDENT:
            continue
        install(STAGE3A, client)
        configure(STAGE3A, client)
        checkpoint(STAGE3A, f'{client} configured', check=f'configured:{client}')

    # --- Stage 3b: independent clients (no radiod, no wisdom wait) ---
    for client in profile.clients:
        if client in _INDEPENDENT:
            install(STAGE3B, client)
            configure(STAGE3B, client)
            checkpoint(STAGE3B, f'{client} configured', check=f'configured:{client}')

    # --- Stage 4: start (wait for wisdom first on a local radiod) ---
    if local_radiod:
        steps.append(Step(STAGE4, 'wait for FFT wisdom before starting radiod',
                          'wait-wisdom'))
    steps.append(Step(STAGE4, 'start all components (priority-ordered)', 'start',
                      argv=[smd, 'start']))
    checkpoint(STAGE4, 'final validate', check='validate')

    return Plan(profile=profile.name, local_radiod=local_radiod,
                remote_status_dns=remote_status_dns, steps=steps)
