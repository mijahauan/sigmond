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

# Settle inserted after each radiod-bound client starts, so it finishes
# provisioning its channels before the next client contends for radiod's
# control plane.  Simultaneous provisioning starves radiod and yields 0
# channels (see docs/install-orchestration-design.md / greenfield notes).
CLIENT_STAGGER_S = 20

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

# Clients that take a per-reporter instance (`<client>@<reporter-id>`).  When a
# reporter id is supplied, bring-up creates + enables the instance instead of
# starting a base-config unit.  Both wspr- and psk-recorder seed a complete
# per-instance config from their shared config (the [[radiod]] block + bands),
# so `<client>@<reporter>` runs directly (sigmond#16).  (Multi-radiod *source
# selection* for psk via `sources apply` is still deferred, but irrelevant to
# the single-radiod reporter-keyed path bring-up uses.)
_REPORTER_KEYED = frozenset({'wspr-recorder', 'psk-recorder'})


@dataclass
class Step:
    """One bring-up action.  ``argv`` is what the executor runs (an ``smd`` or
    ``systemctl`` invocation); empty for ``note``/``wait-wisdom`` steps that the
    executor handles specially."""
    stage: str
    label: str
    kind: str                       # install|config|tune|wisdom|start|checkpoint|note|wait-wisdom|wait-streaming
    argv: list = field(default_factory=list)
    hard: bool = False              # checkpoint: abort the run on failure
    background: bool = False         # fire-and-don't-wait
    check: str = ''                 # checkpoint probe id (see executor._probe)
    settle_s: int = 0               # executor sleeps this long after the step


@dataclass
class Plan:
    profile: str
    local_radiod: bool
    remote_status_dns: Optional[str]
    steps: list = field(default_factory=list)


def build_plan(profile, *, local_radiod: bool,
               remote_status_dns: Optional[str] = None,
               smd: str = 'smd', with_optional: bool = False,
               non_interactive: bool = False, skip=frozenset(),
               dormant=frozenset(),
               reporter: Optional[str] = None) -> Plan:
    """Pure: a profile + radiod locality -> the ordered Step list.

    ``local_radiod`` gates the entire radiod stack (infra, ka9q-radio, tuning,
    radiod config, FFT wisdom).  When False the host binds a remote radiod and
    none of that — nor the wisdom wait — applies.  ``mag-recorder`` is emitted
    on the independent 3b track regardless of locality.

    ``dormant`` names hardware-gated components whose device is absent: they
    still install + configure + enable (so they light up when the hardware is
    later attached and `smd start` — which skips dormant components — runs
    again), but their "configured" checkpoint is SOFT, so a config step that
    can't fully complete without the device doesn't abort the whole bring-up
    (docs/install-redesign.md §3).  Distinct from ``skip`` (excluded entirely).
    """
    steps: list = []

    def install(stage: str, comp: str) -> None:
        # Enable in topology BEFORE installing.  `smd install --components` builds
        # the component but does NOT flip topology `enabled=true` (only
        # `smd install --profile` does, via set_component_enabled).  Without this
        # the whole station stays `enabled=false`: `smd status`/`validate` see
        # nothing "declared" and the Stage-4 `smd start` steps have nothing to
        # start (radiod is left disabled/inactive).  `smd enable` is idempotent,
        # so re-running bring-up is a no-op here.
        steps.append(Step(stage, f'enable {comp}', 'enable',
                          argv=[smd, 'enable', comp]))
        steps.append(Step(stage, f'install {comp}', 'install',
                          argv=[smd, 'install', '--components', comp, '--yes']))

    def configure(stage: str, client: str) -> None:
        argv = [smd, 'config', 'init', client]
        label = f'configure {client}'
        # Client config interviews default to --non-interactive: each client's
        # own wizard (e.g. psk-recorder's whiptail) can't render/read input
        # inside bring-up's nested terminal, and sigmond already supplies the
        # essentials (callsign / grid / radiod) via the env — the operator
        # fine-tunes later with `smd config edit <client>`.  radiod is the
        # exception: it's sigmond's own inline text wizard (works here, and the
        # operator sets the antenna), so it stays interactive unless the whole
        # bring-up was invoked with --non-interactive.
        if non_interactive or client != 'radiod':
            argv.append('--non-interactive')
        if non_interactive:
            label += ' (non-interactive)'
        steps.append(Step(stage, label, 'config', argv=argv))

    def checkpoint(stage: str, label: str, check: str, hard: bool = False) -> None:
        steps.append(Step(stage, f'checkpoint: {label}', 'checkpoint',
                          check=check, hard=hard))

    # --- Stage 1: radiod stack (local only) ---
    if local_radiod:
        for infra in profile.local_radiod_infra:
            if infra in skip:           # hardware-gated infra absent (e.g. no GPSDO)
                continue
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
    if _TIMING_AUTHORITY in profile.clients and _TIMING_AUTHORITY not in skip:
        install(STAGE2, _TIMING_AUTHORITY)
        configure(STAGE2, _TIMING_AUTHORITY)
        checkpoint(STAGE2, 'hf-timestd configured',
                   check=f'configured:{_TIMING_AUTHORITY}', hard=True)

    # --- Stage 3a: radiod-bound spot clients ---
    for client in profile.clients:
        if (client == _TIMING_AUTHORITY or client in _INDEPENDENT
                or client in skip):
            continue
        install(STAGE3A, client)
        configure(STAGE3A, client)
        checkpoint(STAGE3A, f'{client} configured',
                   check=f'configured:{client}', hard=True)
        # Create the per-reporter instance from the base config (status + bands
        # the config step just wrote).  `instance add` only scaffolds the files;
        # Stage 4 enables/starts it (staggered).  Without a reporter id the
        # client falls back to its base-config start.
        if reporter and client in _REPORTER_KEYED:
            # --force makes re-add idempotent (create only missing files, leave
            # existing ones) so re-running bring-up doesn't error on an instance
            # that's already scaffolded.
            steps.append(Step(STAGE3A,
                              f'create reporter instance {client}@{reporter}',
                              'enable',
                              argv=[smd, 'admin', 'instance', 'add', '--force',
                                    client, reporter]))

    # --- Stage 3b: independent clients (no radiod, no wisdom wait) ---
    for client in profile.clients:
        if client in _INDEPENDENT and client not in skip:
            install(STAGE3B, client)
            configure(STAGE3B, client)
            # Soft checkpoint for a dormant (hardware-absent) client: its config
            # step may not fully complete without the device, and that must not
            # abort the bring-up — it lights up when the hardware is attached.
            checkpoint(STAGE3B, f'{client} configured',
                       check=f'configured:{client}',
                       hard=client not in dormant)

    # Provision the shared hs-uploader watermark dir.  Recorder units list
    # /var/lib/hs-uploader in ReadWritePaths under ProtectSystem=strict, so it
    # MUST exist before they start or systemd aborts the sandbox with
    # 226/NAMESPACE.  It's normally created by hs-uploader/install.sh, which
    # bring-up doesn't invoke (hs-uploader is a source-only sibling), so create
    # it here — root:sigmond, setgid + group-writable like /var/lib/sigmond, so
    # every HamSCI recorder user (in the sigmond group) can write.  `install -d`
    # is idempotent.
    steps.append(Step(STAGE4, 'provision shared hs-uploader watermark dir', 'tune',
                      argv=['install', '-d', '-m', '2775', '-o', 'root',
                            '-g', 'sigmond', '/var/lib/hs-uploader']))

    # Re-render the site profile now that stages 1-3 created every client's
    # config: this pushes the PSWS station/instrument ids from
    # site-profile.toml THROUGH into each recorder's own config file — the
    # earlier pre-config render could only seed coordination (the client
    # files didn't exist yet).  Quiet no-op on hosts without a site profile
    # (legacy prompt-driven identity) and idempotent otherwise.  MUST come
    # before the manifest step, which resolves {station_id}/{instrument_id}
    # from those client configs.
    steps.append(Step(STAGE4, 'render site profile (PSWS ids into client '
                              'configs)', 'tune',
                      argv=[smd, 'config', 'render', '--if-present']))

    # Generate the single-host uploader manifest from each enabled client's
    # deploy.toml [[hs_uploader.pipeline]] declarations (identity substituted
    # from coordination + per-client configs), then enable + start the daemon.
    # Runs after clients are installed+configured (stages 1-3), so PSWS ids /
    # reporter ids exist; idempotent (a no-op restart when nothing changed).
    steps.append(Step(STAGE4, 'generate hs-uploader manifest + enable daemon',
                      'tune',
                      argv=[smd, 'admin', 'uploader', 'manifest',
                            '--write', '--enable']))

    # Heal any leftover legacy config before starting: a stale client config
    # from a prior install (e.g. the legacy `status_address` field) that
    # `config init` refused to overwrite would otherwise fail to load.  This
    # rewrites it to the canonical `status` schema and canonicalizes the radiod
    # identity in coordination.toml.  Idempotent — a no-op when nothing is
    # legacy, so it's harmless on a truly-clean host.
    steps.append(Step(STAGE4, 'migrate any legacy config to the canonical radiod '
                              'schema', 'tune',
                      argv=[smd, 'admin', 'radiod', 'migrate', '--yes']))

    # --- Stage 4: start, ORDERED so clients never provision against a cold
    # radiod.  radiod reaches systemd 'active' (forked) ~10 s — and on a cold
    # start up to ~3 min — before it logs 'rx888 running' (actually streaming);
    # a client started in that window provisions 0 channels.  So on a local
    # radiod: start the radiod stack, WAIT for streaming + a settle margin,
    # THEN start the radiod-bound clients staggered.  Independent clients (mag)
    # aren't gated on radiod.  A final idempotent `smd start` sweeps up anything
    # enabled-but-unstarted (e.g. inert rac stays inert). ---
    radiod_bound = [c for c in profile.clients
                    if c not in skip and c not in _INDEPENDENT]
    independent = [c for c in profile.clients
                   if c not in skip and c in _INDEPENDENT]

    if local_radiod:
        steps.append(Step(STAGE4, 'wait for FFT wisdom before starting radiod',
                          'wait-wisdom'))
        radiod_stack = ['ka9q-radio'] + [i for i in profile.local_radiod_infra
                                         if i not in skip]
        steps.append(Step(STAGE4, 'start radiod + local-radiod infra', 'start',
                          argv=[smd, 'start', '--components', ','.join(radiod_stack)]))
        steps.append(Step(STAGE4,
                          'wait for radiod streaming (rx888 running) + settle',
                          'wait-streaming'))

    for client in radiod_bound:
        if reporter and client in _REPORTER_KEYED:
            # `instance enable` does systemctl enable --now on the per-reporter
            # unit (wspr-recorder@<reporter>), so it both declares and starts it.
            steps.append(Step(STAGE4, f'start {client}@{reporter} (staggered)', 'start',
                              argv=[smd, 'admin', 'instance', 'enable', client, reporter],
                              settle_s=CLIENT_STAGGER_S))
        else:
            steps.append(Step(STAGE4, f'start {client} (staggered)', 'start',
                              argv=[smd, 'start', '--components', client],
                              settle_s=CLIENT_STAGGER_S))
    for client in independent:
        steps.append(Step(STAGE4, f'start {client} (independent)', 'start',
                          argv=[smd, 'start', '--components', client]))

    steps.append(Step(STAGE4, 'start any remaining enabled components', 'start',
                      argv=[smd, 'start']))
    checkpoint(STAGE4, 'final validate', check='validate')

    return Plan(profile=profile.name, local_radiod=local_radiod,
                remote_status_dns=remote_status_dns, steps=steps)
