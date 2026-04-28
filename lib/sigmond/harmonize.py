"""Cross-client harmonization rules.

Each rule is a pure function that takes a SystemView and returns a
RuleResult.  Rules tolerate missing peers and remote radiods — when a
component referenced by a rule is absent, the rule returns a 'pass'
result with a 'skipped' message instead of failing.  This keeps sigmond
standalone-safe.

The six rules are the ones documented in tui-configurator.md, adjusted
for the multi-radiod architecture in coordination.toml:

  1. radiod_resolution   — every client instance's radiod_id resolves
  2. frequency_coverage  — per radiod, consumed freqs within samprate
  3. cpu_isolation       — local radiod cores disjoint from workers
  4. timing_chain        — hf-timestd <-> consumers on the same radiod
  5. disk_budget         — local disk writes fit the warn threshold
  6. channel_count       — per radiod, sum of channel demands <= max
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .sysview import SystemView

# Path to the gpsdo-monitor daemon's per-device JSON drop.  Exposed as a
# module-level constant so tests can redirect it to a tmp dir.
GPSDO_RUN_DIR = Path("/run/gpsdo")

# Path to net.core.rmem_max.  Module-level so tests can redirect it to
# a tmp file with controlled contents.
RCVBUF_PROC_PATH = Path("/proc/sys/net/core/rmem_max")

# ka9q-python's per-socket SO_RCVBUF request (ka9q/multi_stream.py:264 in
# ka9q-python upstream).  Linux silently doubles SO_RCVBUF on success,
# so an 8 MiB request becomes a 16 MiB buffer iff rmem_max permits.
KA9Q_PYTHON_SO_RCVBUF_REQUEST = 8 * 1024 * 1024     # 8 MiB

# Smallest rmem_max that lets ka9q-python's request through after the
# kernel-side doubling.  Below this, every multicast subscriber drops.
RCVBUF_FLOOR_BYTES = 2 * KA9Q_PYTHON_SO_RCVBUF_REQUEST              # 16 MiB

# Headroom for a multi-client station (3+ clients sharing one radiod
# multicast group).  At Debian 12 defaults (~212 KB) any HamSCI client
# drops; at the floor a single client survives but adding a second
# starts dropping; at the recommended value the trio + hfdl-recorder
# fit comfortably.
RCVBUF_RECOMMENDED_BYTES = 64 * 1024 * 1024                         # 64 MiB


@dataclass
class RuleResult:
    rule:     str
    severity: str           # "pass" | "warn" | "fail"
    message:  str
    affected: list = field(default_factory=list)


def _parse_cores(spec: str) -> set:
    """Parse a systemd-style core list like '0-3,5,7' into a set of ints."""
    out: set = set()
    if not spec:
        return out
    for chunk in str(spec).split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        if '-' in chunk:
            lo, _, hi = chunk.partition('-')
            try:
                for i in range(int(lo), int(hi) + 1):
                    out.add(i)
            except ValueError:
                continue
        else:
            try:
                out.add(int(chunk))
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------

def rule_radiod_resolution(view: SystemView) -> RuleResult:
    coord = view.coordination
    if not coord.clients:
        return RuleResult("radiod_resolution", "pass",
                          "no client instances declared", [])
    unresolved = []
    affected = []
    for c in coord.clients:
        if c.radiod_id is None:
            continue
        if c.radiod_id not in coord.radiods:
            unresolved.append(f"{c.client_type}@{c.instance} → {c.radiod_id}")
            affected.append(c.client_type)
    if unresolved:
        return RuleResult(
            "radiod_resolution", "fail",
            f"unresolved radiod_id references: {', '.join(unresolved)}",
            sorted(set(affected)),
        )
    return RuleResult("radiod_resolution", "pass",
                      f"all {len(coord.clients)} client instance(s) resolve", [])


def rule_frequency_coverage(view: SystemView) -> RuleResult:
    """Per-radiod: any consumed frequency must fit within samprate/2 of
    the radiod's tuning range.  Phase 1 uses a coarse heuristic — we just
    check that requested frequencies are below samprate_hz (Nyquist
    envelope) since we don't know radiod's RF tuning without a full conf
    parse."""
    coord = view.coordination
    violations = []
    checked = 0
    for rid, radiod in coord.radiods.items():
        if not radiod.is_local:
            continue    # remote radiod — someone else's problem
        if not radiod.samprate_hz:
            continue    # no coverage info, skip quietly
        consumers = _consumers_of(view, rid)
        for ctype, iv in consumers:
            for hz in iv.frequencies_hz:
                checked += 1
                if hz > radiod.samprate_hz:
                    violations.append(f"{ctype}@{iv.instance} wants {hz} Hz > samprate {radiod.samprate_hz}")
    if violations:
        return RuleResult(
            "frequency_coverage", "fail",
            "; ".join(violations),
            [],
        )
    if checked == 0:
        return RuleResult("frequency_coverage", "pass",
                          "skipped (no local radiod samprate declared)", [])
    return RuleResult("frequency_coverage", "pass",
                      f"{checked} frequency claim(s) within samprate", [])


def rule_cpu_isolation(view: SystemView) -> RuleResult:
    coord = view.coordination
    worker_cores = _parse_cores(coord.cpu.worker_cores)
    reserved     = _parse_cores(coord.cpu.reserved_cpus)
    overlaps     = []
    checked      = 0

    for rid, radiod in coord.radiods.items():
        if not radiod.is_local:
            continue
        rcores = _parse_cores(radiod.cores)
        if not rcores:
            continue
        checked += 1
        bad = rcores & worker_cores
        if bad:
            overlaps.append(f"radiod '{rid}' cores {sorted(bad)} overlap worker_cores")
        bad = rcores & reserved
        if bad:
            overlaps.append(f"radiod '{rid}' cores {sorted(bad)} overlap reserved_cpus")

    if overlaps:
        return RuleResult("cpu_isolation", "fail", "; ".join(overlaps), [])
    if checked == 0:
        return RuleResult("cpu_isolation", "pass",
                          "skipped (no local radiod core claims)", [])
    return RuleResult("cpu_isolation", "pass",
                      f"checked {checked} local radiod(s); no overlap", [])


def rule_timing_chain(view: SystemView) -> RuleResult:
    coord = view.coordination
    timestd = coord.instances_of("hf-timestd")
    if not timestd:
        return RuleResult("timing_chain", "pass",
                          "skipped (hf-timestd not declared)", [])
    problems = []
    covered = 0
    for ts in timestd:
        if not ts.radiod_id:
            continue
        # Any other client bound to the same radiod that needs calibration?
        consumers = [c for c in coord.clients
                     if c.client_type != "hf-timestd"
                     and c.radiod_id == ts.radiod_id]
        if not consumers:
            continue
        covered += 1
        # Phase 1 can't inspect clients' native uses_timing_calibration flag
        # from coordination.toml alone — defer to the contract retrofit.
        # Just note that the chain exists.
    if covered:
        return RuleResult("timing_chain", "pass",
                          f"{covered} radiod(s) have timing-chain pairing", [])
    return RuleResult("timing_chain", "pass",
                      "hf-timestd declared; no other client shares its radiod(s)", [])


def rule_disk_budget(view: SystemView) -> RuleResult:
    budget = view.coordination.disk_budget
    try:
        usage = shutil.disk_usage(budget.root_path)
    except (FileNotFoundError, PermissionError) as exc:
        return RuleResult("disk_budget", "warn",
                          f"cannot stat {budget.root_path}: {exc}", [])
    pct_used = int(round(100 * (usage.total - usage.free) / max(usage.total, 1)))
    if pct_used >= budget.warn_percent:
        return RuleResult("disk_budget", "warn",
                          f"{budget.root_path} at {pct_used}% (threshold {budget.warn_percent}%)",
                          [])
    return RuleResult("disk_budget", "pass",
                      f"{budget.root_path} at {pct_used}% (threshold {budget.warn_percent}%)",
                      [])


def rule_channel_count(view: SystemView) -> RuleResult:
    coord = view.coordination
    per_radiod: dict = {}
    for c in coord.clients:
        if not c.radiod_id:
            continue
        # Phase 1 cannot read a client's ka9q_channels count from
        # coordination.toml alone — we need the client_view.  For each
        # radiod, sum channels across every consumer instance we have a
        # view for.
        pass
    # Cross-reference client_views for the actual channel demands.
    for client_name, cv in view.client_views.items():
        for iv in cv.instances:
            if not iv.radiod_id or not iv.ka9q_channels:
                continue
            per_radiod.setdefault(iv.radiod_id, 0)
            per_radiod[iv.radiod_id] += iv.ka9q_channels

    if not per_radiod:
        return RuleResult("channel_count", "pass",
                          "no channel-count info available", [])
    # Phase 1: no per-radiod maximum is known (we'd need to parse radiod.conf
    # dynamic channel limits).  Just report the totals so operators see them.
    totals = ', '.join(f"{rid}:{n}" for rid, n in sorted(per_radiod.items()))
    return RuleResult("channel_count", "pass",
                      f"channel demand — {totals}", [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _consumers_of(view: SystemView, radiod_id: str) -> list:
    """Return list of (client_type, InstanceView) bound to the given radiod."""
    out = []
    for cv in view.client_views.values():
        for iv in cv.instances:
            if iv.radiod_id == radiod_id:
                out.append((cv.client_type, iv))
    # Also include any coordination-declared instances whose client_view
    # may not be in topology — skip for now, keep it simple.
    return out


def rule_cpu_isolation_runtime(view: SystemView) -> RuleResult:
    """Runtime counterpart to rule_cpu_isolation.

    The declared rule checks coordination.toml's internal consistency.
    This rule checks that the *running* system honors the design objective:
    radiod's USB3/FFT path on its reserved cores, uncontested.

    Looks at: pinned processes overlapping radiod cores,
    sched_setaffinity overrides of systemd's CPUAffinity, foreign
    drop-ins still on disk, radiod units running without smd's drop-in,
    and non-'performance' governor on radiod cores.

    Skips cleanly when there's no local radiod in the view, when no
    radiod unit is actually running, or when the live host check can't
    complete (e.g. systemctl unavailable) — this keeps the rule
    standalone-safe in the same spirit as the declared rules.
    """
    local_radiods = [r for r in view.coordination.radiods.values() if r.is_local]
    if not local_radiods:
        return RuleResult("cpu_isolation_runtime", "pass",
                          "skipped (no local radiod)", [])

    try:
        from .cpu import build_affinity_report
        report = build_affinity_report()
    except Exception as exc:
        return RuleResult("cpu_isolation_runtime", "pass",
                          f"runtime check unavailable: {exc}", [])

    if not report.radiod_cpus:
        return RuleResult("cpu_isolation_runtime", "pass",
                          "skipped (no radiod units running)", [])

    issues: list = []
    affected: set = set()

    pinned = [c for c in report.contention if not c.is_default]
    if pinned:
        issues.append(
            f"{len(pinned)} pinned process(es) overlap radiod cores")
        affected.update(c.comm for c in pinned)

    overrides = [u.unit for u in report.units
                 if u.role == 'radiod' and u.mask_mismatch]
    if overrides:
        issues.append(
            f"sched_setaffinity override on: {', '.join(overrides)}")

    foreign = [u.unit for u in report.units if u.foreign_drop_ins]
    if foreign:
        issues.append(
            f"foreign drop-ins still present on: {', '.join(foreign)}")

    unenforced = [u.unit for u in report.units
                  if u.role == 'radiod' and u.main_pid and not u.drop_in_present]
    if unenforced:
        issues.append(
            f"radiod running without smd drop-in: {', '.join(unenforced)}")

    bad_gov = [(cpu, report.capabilities.governors[cpu])
               for cpu in sorted(report.radiod_cpus)
               if report.capabilities.governors.get(cpu)
               and report.capabilities.governors[cpu] != 'performance']
    if bad_gov:
        sample = ', '.join(f"cpu{c}={g}" for c, g in bad_gov[:4])
        more = f' (+{len(bad_gov) - 4} more)' if len(bad_gov) > 4 else ''
        issues.append(
            f"governor not 'performance' on radiod cores: {sample}{more}")

    if issues:
        return RuleResult("cpu_isolation_runtime", "warn",
                          "; ".join(issues), sorted(affected))
    return RuleResult(
        "cpu_isolation_runtime", "pass",
        f"radiod cores {sorted(report.radiod_cpus)} uncontested", [])


def rule_gpsdo_governor_coverage(view: SystemView) -> RuleResult:
    """Each local radiod should have exactly one gpsdo-monitor device
    declaring `governs = ["radiod:<id>"]`.

    - 0 governors → warn (A-level witness missing for that radiod)
    - 1 governor  → pass
    - ≥2          → fail (ambiguous authority; hf-timestd cannot pick)

    Reads the runtime drop `/run/gpsdo/*.json` written by the
    `gpsdo-monitor` daemon. Skips cleanly (pass) when the directory is
    absent, empty, or contains only stale/index files — which is the
    right default before gpsdo-monitor is deployed on a host.
    """
    if not GPSDO_RUN_DIR.is_dir():
        return RuleResult("gpsdo_governor_coverage", "pass",
                          f"skipped (no {GPSDO_RUN_DIR})", [])
    files = [p for p in GPSDO_RUN_DIR.glob("*.json") if p.name != "index.json"]
    if not files:
        return RuleResult("gpsdo_governor_coverage", "pass",
                          "skipped (no gpsdo-monitor reports present)", [])

    local_radiods = [r for r in view.coordination.radiods.values() if r.is_local]
    if not local_radiods:
        return RuleResult("gpsdo_governor_coverage", "pass",
                          "skipped (no local radiod)", [])

    # {radiod_id: [serial, ...]} of who claims to govern what.
    governed: dict = {}
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("schema") != "v1":
            continue
        serial = path.stem
        device = data.get("device")
        if isinstance(device, dict) and device.get("serial"):
            serial = str(device["serial"])
        governs = data.get("governs") or []
        if not isinstance(governs, list):
            continue
        for token in governs:
            if not isinstance(token, str):
                continue
            # gpsdo-monitor's convention is "radiod:<id>"; strip the
            # prefix to compare against coordination.toml's radiod.id.
            rid = token.split(":", 1)[1] if token.startswith("radiod:") else token
            governed.setdefault(rid, []).append(serial)

    issues: list = []
    severity = "pass"
    one_governor_count = 0
    affected: set = set()
    for radiod in local_radiods:
        claimants = governed.get(radiod.id, [])
        if not claimants:
            issues.append(f"{radiod.id}: no gpsdo-monitor governor declared")
            if severity == "pass":
                severity = "warn"
            affected.add(radiod.id)
        elif len(claimants) == 1:
            one_governor_count += 1
        else:
            issues.append(
                f"{radiod.id}: {len(claimants)} governors "
                f"({', '.join(sorted(claimants))})"
            )
            severity = "fail"
            affected.add(radiod.id)

    if severity == "pass":
        return RuleResult(
            "gpsdo_governor_coverage", "pass",
            f"{one_governor_count} local radiod(s) each have one governor",
            [],
        )
    return RuleResult("gpsdo_governor_coverage", severity,
                      "; ".join(issues), sorted(affected))


def rule_kernel_rcvbuf_adequate(view: SystemView) -> RuleResult:
    """Check that net.core.rmem_max is large enough for ka9q-python's
    SO_RCVBUF request to be honoured by the kernel.

    Several HamSCI clients (psk-recorder, wspr-recorder, hf-timestd,
    hfdl-recorder, ka9q-web) subscribe to the same radiod multicast
    group via ka9q-python, which calls ``setsockopt(SO_RCVBUF, 8 MiB)``
    per UDP socket.  Linux doubles SO_RCVBUF on success, so the kernel
    actually allocates 16 MiB iff ``rmem_max`` permits.

    On a stock Debian 12 host ``rmem_max`` defaults to 212 992 bytes —
    far below ka9q-python's request — so per-socket buffers fill within
    seconds under aggregate radiod load (60+ channels at ~30 MB/s) and
    the kernel drops packets.  ka9q-python's resequencer surfaces the
    drops as ``"Lost packet recovery: skip to seq=…"``.  No single
    client can safely raise ``rmem_max`` from its own systemd unit, so
    sigmond owns the cross-client kernel parameter.

    The remediation in the message field is exact and copy-paste-able;
    a future ``smd apply`` extension can write the drop-in itself.
    """
    try:
        rmem_max = int(RCVBUF_PROC_PATH.read_text().strip())
    except (OSError, ValueError):
        return RuleResult(
            "kernel_rcvbuf_adequate", "pass",
            f"skipped: cannot read {RCVBUF_PROC_PATH}", [],
        )

    affected = sorted({c.client_type for c in view.coordination.clients})
    remediation = (
        f"echo 'net.core.rmem_max = {RCVBUF_RECOMMENDED_BYTES}' "
        f"| sudo tee /etc/sysctl.d/99-sigmond-multicast.conf "
        f"&& sudo sysctl --load=/etc/sysctl.d/99-sigmond-multicast.conf "
        f"&& sudo systemctl restart 'hfdl-recorder@*' 'psk-recorder@*' "
        f"'wspr-recorder@*' timestd-core-recorder.service"
    )

    if rmem_max < RCVBUF_FLOOR_BYTES:
        return RuleResult(
            "kernel_rcvbuf_adequate", "fail",
            f"net.core.rmem_max={rmem_max} bytes "
            f"({rmem_max // (1024*1024)} MiB) is below the floor of "
            f"{RCVBUF_FLOOR_BYTES // (1024*1024)} MiB; ka9q-python's "
            f"{KA9Q_PYTHON_SO_RCVBUF_REQUEST // (1024*1024)} MiB "
            f"per-socket request will be clamped and any multicast "
            f"subscriber will drop packets.  Remediate: {remediation}",
            affected,
        )

    if rmem_max < RCVBUF_RECOMMENDED_BYTES:
        return RuleResult(
            "kernel_rcvbuf_adequate", "warn",
            f"net.core.rmem_max={rmem_max // (1024*1024)} MiB is "
            f"adequate for one client but multi-client stations should "
            f"raise to {RCVBUF_RECOMMENDED_BYTES // (1024*1024)} MiB "
            f"for headroom across the suite "
            f"(psk-recorder, wspr-recorder, hf-timestd, hfdl-recorder).  "
            f"Remediate: {remediation}",
            affected,
        )

    return RuleResult(
        "kernel_rcvbuf_adequate", "pass",
        f"net.core.rmem_max={rmem_max // (1024*1024)} MiB",
        [],
    )


def _ka9q_radio_source_dir() -> Optional[Path]:
    """Locate a ka9q-radio source checkout on the local host.

    Tries the canonical Pattern A path first, then the per-user dev
    sibling.  Returns the first one that contains a ``.git`` dir.
    Returns None if neither is present (the rule then skips cleanly).
    """
    import os
    home = os.path.expanduser('~')
    for candidate in (Path('/opt/git/ka9q-radio'),
                      Path(home) / 'ka9q-radio'):
        if (candidate / '.git').exists():
            return candidate
    return None


def _git_head(repo_dir: Path) -> Optional[str]:
    """Read HEAD's full SHA from a git checkout without spawning git.

    Pure-stdlib so this works in sigmond's headless-first environment
    even if /usr/bin/git is missing.  Returns None on any error.
    """
    head_path = repo_dir / '.git' / 'HEAD'
    try:
        head_text = head_path.read_text().strip()
    except OSError:
        return None
    if head_text.startswith('ref: '):
        ref_path = repo_dir / '.git' / head_text[5:]
        try:
            return ref_path.read_text().strip()
        except OSError:
            # Packed refs fallback.
            try:
                packed = (repo_dir / '.git' / 'packed-refs').read_text()
            except OSError:
                return None
            target_ref = head_text[5:]
            for line in packed.splitlines():
                if line.endswith(' ' + target_ref):
                    return line.split(' ', 1)[0].strip()
            return None
    # Detached HEAD — text is the SHA itself.
    if len(head_text) >= 40 and all(c in '0123456789abcdef' for c in head_text[:40]):
        return head_text
    return None


def rule_ka9q_python_compat(view: SystemView) -> RuleResult:
    """Verify ka9q-python's pinned ka9q-radio commit matches the local
    ka9q-radio source HEAD.

    ka9q-python ships ``ka9q.compat.KA9Q_RADIO_COMMIT`` — the SHA
    against which ``ka9q/types.py`` was last validated by
    ``scripts/sync_types.py --apply`` (parsing C enum headers from
    ka9q-radio).  Drift here means Python clients (psk-recorder,
    wspr-recorder, hf-timestd, hfdl-recorder) decode wire-protocol
    enums against the wrong commit's headers — silent breakage at
    decode time.

    Both directions are real: an operator who ``git pull && make
    install``s ka9q-radio without re-syncing ka9q-python, AND a
    ka9q-python pin bump that hasn't been followed by a ka9q-radio
    rebuild yet, both produce drift.  Severity is ``warn`` rather than
    ``fail`` because operators are sometimes legitimately mid-upgrade.
    """
    try:
        from ka9q.compat import KA9Q_RADIO_COMMIT  # noqa: F401
    except ImportError:
        return RuleResult(
            "ka9q_python_compat", "pass",
            "skipped: ka9q-python not importable from sigmond's Python", [],
        )
    expected = KA9Q_RADIO_COMMIT
    if not expected or expected == "unknown":
        return RuleResult(
            "ka9q_python_compat", "pass",
            "skipped: ka9q-python pin is empty/unknown", [],
        )

    source_dir = _ka9q_radio_source_dir()
    if source_dir is None:
        return RuleResult(
            "ka9q_python_compat", "pass",
            "skipped: no ka9q-radio source checkout found at "
            "/opt/git/ka9q-radio or ~/ka9q-radio", [],
        )
    actual = _git_head(source_dir)
    if actual is None:
        return RuleResult(
            "ka9q_python_compat", "pass",
            f"skipped: cannot resolve HEAD of {source_dir}", [],
        )

    affected = sorted({c.client_type for c in view.coordination.clients})

    if actual.startswith(expected) or expected.startswith(actual):
        return RuleResult(
            "ka9q_python_compat", "pass",
            f"ka9q-radio HEAD {actual[:12]} matches ka9q-python pin",
            [],
        )

    remediation = (
        "If ka9q-radio is the source of truth: rerun "
        "`ka9q-update/install-ka9q.sh` (it pins ka9q-radio to "
        f"ka9q-python's expected commit {expected[:12]}). "
        "If ka9q-python should advance: run "
        "`python scripts/sync_types.py --apply` in ka9q-python against "
        f"the new ka9q-radio at {actual[:12]}, commit, then "
        "rerun the installer."
    )
    return RuleResult(
        "ka9q_python_compat", "warn",
        f"ka9q-python pin expects ka9q-radio at {expected[:12]} "
        f"but {source_dir} HEAD is {actual[:12]}.  "
        f"Python clients may decode wire-protocol enums against the "
        f"wrong header revision.  {remediation}",
        affected,
    )


ALL_RULES = [
    rule_radiod_resolution,
    rule_frequency_coverage,
    rule_cpu_isolation,
    rule_timing_chain,
    rule_disk_budget,
    rule_channel_count,
    rule_ka9q_python_compat,
]

# Rules that read live /sys, /proc, and systemctl state.  Kept out of
# ALL_RULES so unit tests with hand-built views don't pick up host state.
ALL_RUNTIME_RULES = [
    rule_cpu_isolation_runtime,
    rule_gpsdo_governor_coverage,
    rule_kernel_rcvbuf_adequate,
]


def run_all(view: SystemView, include_runtime: bool = False) -> list:
    rules = ALL_RULES + (ALL_RUNTIME_RULES if include_runtime else [])
    return [rule(view) for rule in rules]


def worst_severity(results: list) -> str:
    order = {"pass": 0, "warn": 1, "fail": 2}
    worst = "pass"
    for r in results:
        if order.get(r.severity, 0) > order.get(worst, 0):
            worst = r.severity
    return worst
