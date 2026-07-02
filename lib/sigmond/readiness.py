"""Golden-image / site readiness gate — machine-checkable.

Answers "is this VM confirmed functioning?" as a pass/fail gate instead
of runbook prose.  Two gates share the structural core:

* ``capture`` — is this VM fit to capture as the DASI2 golden image?
  Every profile component installed, its venv artefacts present, its
  contract binary answers ``version --json`` (proving the venv imports),
  its systemd unit files installed, radiod present — AND the image is
  clean of per-site identity/secrets (nothing baked in that
  ``smd admin personalize`` + site configuration should supply).

* ``site`` — is this cloned VM fit to run as a station?  The same
  structural core PLUS personalization done, station identity present,
  and harmonization (`smd admin validate`) reporting no failures.

Pure logic lives here (stdlib only, injectable paths for tests); the
CLI wrapper is ``sigmond.commands.readiness``.  See
docs/PROVISIONING-INPUTS.md §9 for the golden-image model this gates.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11 fallback unused
    tomllib = None


# Sentinel written by `smd admin personalize` on a cloned VM.
PERSONALIZED_SENTINEL = Path('/etc/sigmond/.personalized')

# Per-site artefacts that must NOT be baked into a captured image.
# Each is (path, why).  SSH keys self-generate on-host (hs-uploader
# transports call ensure_ssh_key on first use), so a baked private key
# is both unnecessary and a cross-site credential leak.
CAPTURE_FORBIDDEN = (
    (Path('/etc/sigmond/frpc.toml'),
     'RAC tunnel credentials (per-site user/token/remotePort)'),
    (Path('/etc/hs-uploader/keys/id_ed25519_host'),
     'station SSH private key (self-generates on first use per site)'),
    (Path('/etc/hs-uploader/keys/id_ed25519'),
     'station SSH private key (self-generates on first use per site)'),
)

# Probe timeout for `<client> version --json` — venv python start plus
# imports; generous so a cold page cache doesn't flake the gate.
VERSION_PROBE_TIMEOUT_SEC = 45.0


@dataclass
class CheckResult:
    """One gate check.  ``status``: pass | warn | fail | skip."""
    name: str
    status: str
    detail: str = ''
    component: Optional[str] = None

    def as_dict(self) -> dict:
        d = {'name': self.name, 'status': self.status, 'detail': self.detail}
        if self.component:
            d['component'] = self.component
        return d


@dataclass
class GateReport:
    gate: str
    profile: str
    results: list = field(default_factory=list)

    @property
    def counts(self) -> dict:
        c = {'pass': 0, 'warn': 0, 'fail': 0, 'skip': 0}
        for r in self.results:
            c[r.status] = c.get(r.status, 0) + 1
        return c

    @property
    def ready(self) -> bool:
        return self.counts['fail'] == 0

    def as_dict(self) -> dict:
        return {
            'gate': self.gate,
            'profile': self.profile,
            'ready': self.ready,
            'counts': self.counts,
            'results': [r.as_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# component enumeration
# ---------------------------------------------------------------------------

def profile_components(profile_name: str, *, with_optional: bool = False):
    """Resolve a catalog profile to its component name list.

    Raises KeyError when the profile doesn't exist (caller reports).
    """
    from .catalog import load_profiles
    prof = load_profiles()[profile_name]
    names = list(prof.clients) + list(prof.local_radiod_infra)
    if with_optional:
        names += list(prof.optional)
    return names


# ---------------------------------------------------------------------------
# structural checks (shared by both gates)
# ---------------------------------------------------------------------------

def _load_deploy(component: str) -> Optional[dict]:
    from .discover import find_deploy_toml
    path = find_deploy_toml(component)
    if path is None or tomllib is None:
        return None
    try:
        with open(path, 'rb') as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _probe_version(binary: str, *,
                   timeout: float = VERSION_PROBE_TIMEOUT_SEC,
                   runner=subprocess.run) -> tuple[bool, str]:
    """Run ``<binary> version --json``; a parseable JSON reply proves the
    venv resolves every import.  Returns (ok, detail)."""
    try:
        r = runner([binary, 'version', '--json'],
                   capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f'probe error: {exc}'
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or '').strip()[-200:]
        return False, f'exit {r.returncode}: {tail}'
    try:
        info = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return False, 'non-JSON version output'
    version = info.get('version') or info.get('client_version') or '?'
    return True, f'version {version}'


def structural_checks(profile_name: str = 'dasi2', *,
                      with_optional: bool = False,
                      suite_root: Path = Path('/opt/git/sigmond'),
                      unit_dir: Path = Path('/etc/systemd/system'),
                      probe=_probe_version) -> list:
    """Every profile component: installed, built, probeable, unit files in."""
    from .catalog import load_catalog, find_client_binary

    results: list[CheckResult] = []
    try:
        names = profile_components(profile_name, with_optional=with_optional)
    except KeyError:
        return [CheckResult('profile', 'fail',
                            f'unknown profile {profile_name!r}')]
    results.append(CheckResult(
        'profile', 'pass',
        f'{profile_name}: {len(names)} components ({", ".join(names)})'))

    entries = load_catalog()

    for name in names:
        entry = entries.get(name)
        kind = entry.kind if entry else '?'

        # 1. Source tree present.
        repo = suite_root / name
        if not os.path.lexists(str(repo)):
            results.append(CheckResult(
                'installed', 'fail', f'{repo} missing', component=name))
            continue
        results.append(CheckResult('installed', 'pass', str(repo),
                                   component=name))

        deploy = _load_deploy(name)

        # 2. Build artefacts ([build].produces — typically the venv CLI).
        produces = (deploy or {}).get('build', {}).get('produces', [])
        missing = [p for p in produces if not Path(p).exists()]
        if missing:
            results.append(CheckResult(
                'built', 'fail',
                f'missing build artefacts: {", ".join(missing)}',
                component=name))
        elif produces:
            results.append(CheckResult(
                'built', 'pass', f'{len(produces)} artefact(s)',
                component=name))

        # 3. Contract probe — proves the venv imports end-to-end.
        binary = find_client_binary(name)
        if binary and kind == 'client':
            ok, detail = probe(binary)
            results.append(CheckResult(
                'imports', 'pass' if ok else 'fail', detail, component=name))
        elif kind == 'client':
            results.append(CheckResult(
                'imports', 'fail', 'client binary not found', component=name))
        else:
            results.append(CheckResult(
                'imports', 'skip', f'kind={kind} (no contract probe)',
                component=name))

        # 4. systemd unit files installed (plain + templated declarations).
        sysd = (deploy or {}).get('systemd', {})
        units = list(sysd.get('units', [])) + list(
            sysd.get('templated_units', []))
        for unit in units:
            path = unit_dir / unit
            if os.path.lexists(str(path)):
                results.append(CheckResult('unit', 'pass', unit,
                                           component=name))
            else:
                results.append(CheckResult(
                    'unit', 'fail', f'{unit} not installed', component=name))

    # 5. Native radiod — the dasi2 profile is a local-radiod station.
    radiod = shutil.which('radiod') or (
        '/usr/local/sbin/radiod'
        if Path('/usr/local/sbin/radiod').exists() else None)
    results.append(CheckResult(
        'radiod', 'pass' if radiod else 'fail',
        radiod or 'radiod binary not found (native build missing)'))

    return results


# ---------------------------------------------------------------------------
# capture gate — image cleanliness
# ---------------------------------------------------------------------------

def capture_cleanliness_checks(
        *,
        sentinel: Path = PERSONALIZED_SENTINEL,
        forbidden=CAPTURE_FORBIDDEN,
        coordination_loader=None) -> list:
    """The image must carry NO per-site identity or secrets."""
    results: list[CheckResult] = []

    if sentinel.exists():
        results.append(CheckResult(
            'clean:personalized', 'fail',
            f'{sentinel} exists — this VM was personalized for a site; '
            'capture from a pre-personalize image'))
    else:
        results.append(CheckResult('clean:personalized', 'pass',
                                   'no personalize sentinel'))

    for path, why in forbidden:
        if path.exists():
            results.append(CheckResult(
                'clean:secrets', 'fail', f'{path} present — {why}'))
        else:
            results.append(CheckResult('clean:secrets', 'pass',
                                       f'{path.name} absent'))

    # Station identity must not be baked in.
    call, grid = _host_identity(coordination_loader)
    if call or grid:
        results.append(CheckResult(
            'clean:identity', 'fail',
            f'coordination [host] carries identity (call={call!r} '
            f'grid={grid!r}) — wipe before capture'))
    else:
        results.append(CheckResult('clean:identity', 'pass',
                                   'no station identity baked in'))
    return results


def _host_identity(loader=None) -> tuple[str, str]:
    if loader is None:
        from .coordination import load_coordination
        loader = load_coordination
    try:
        coord = loader()
    except Exception:
        return '', ''
    host = getattr(coord, 'host', None)
    return (getattr(host, 'call', '') or '',
            getattr(host, 'grid', '') or '')


# ---------------------------------------------------------------------------
# site gate — cloned VM configured and coherent
# ---------------------------------------------------------------------------

def site_checks(*,
                sentinel: Path = PERSONALIZED_SENTINEL,
                coordination_loader=None,
                harmonize_runner=None) -> list:
    results: list[CheckResult] = []

    if sentinel.exists():
        results.append(CheckResult('site:personalized', 'pass',
                                   sentinel.read_text().strip()[:120]))
    else:
        # Warn, not fail: a hand-built (non-cloned) station is legitimate.
        results.append(CheckResult(
            'site:personalized', 'warn',
            f'{sentinel} absent — run `smd admin personalize` on cloned VMs'))

    call, grid = _host_identity(coordination_loader)
    if call and grid:
        results.append(CheckResult('site:identity', 'pass',
                                   f'call={call} grid={grid}'))
    else:
        results.append(CheckResult(
            'site:identity', 'fail',
            'station identity incomplete — set call+grid via '
            'site-profile.toml + `smd config render` (or `smd config identity`)'))

    if harmonize_runner is None:
        def harmonize_runner():
            from . import harmonize
            from .sysview import build_system_view
            return harmonize.run_all(build_system_view(),
                                     include_runtime=True)
    try:
        rules = harmonize_runner()
    except Exception as exc:
        results.append(CheckResult('site:validate', 'fail',
                                   f'harmonization crashed: {exc}'))
        return results

    fails = [r for r in rules if r.severity == 'fail']
    warns = [r for r in rules if r.severity == 'warn']
    if fails:
        results.append(CheckResult(
            'site:validate', 'fail',
            f'{len(fails)} failing rule(s): '
            + ', '.join(r.rule for r in fails)))
    elif warns:
        results.append(CheckResult(
            'site:validate', 'warn',
            f'0 fail, {len(warns)} warn: '
            + ', '.join(r.rule for r in warns)))
    else:
        results.append(CheckResult('site:validate', 'pass',
                                   'all harmonization rules pass'))
    return results


# ---------------------------------------------------------------------------
# gate orchestration
# ---------------------------------------------------------------------------

def detect_gate(*, sentinel: Path = PERSONALIZED_SENTINEL,
                coordination_loader=None) -> str:
    """Auto-pick the gate: identity or a personalize sentinel means this
    VM is (becoming) a station → site; otherwise it's image stock →
    capture."""
    if sentinel.exists():
        return 'site'
    call, grid = _host_identity(coordination_loader)
    return 'site' if (call or grid) else 'capture'


def run_gate(gate: str = 'auto', *,
             profile: str = 'dasi2',
             with_optional: bool = False,
             structural=structural_checks,
             capture=capture_cleanliness_checks,
             site=site_checks,
             detect=detect_gate) -> GateReport:
    if gate == 'auto':
        gate = detect()
    report = GateReport(gate=gate, profile=profile)
    report.results.extend(
        structural(profile, with_optional=with_optional))
    if gate == 'capture':
        report.results.extend(capture())
    else:
        report.results.extend(site())
    return report
