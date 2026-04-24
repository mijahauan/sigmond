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


ALL_RULES = [
    rule_radiod_resolution,
    rule_frequency_coverage,
    rule_cpu_isolation,
    rule_timing_chain,
    rule_disk_budget,
    rule_channel_count,
]

# Rules that read live /sys, /proc, and systemctl state.  Kept out of
# ALL_RULES so unit tests with hand-built views don't pick up host state.
ALL_RUNTIME_RULES = [
    rule_cpu_isolation_runtime,
    rule_gpsdo_governor_coverage,
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
