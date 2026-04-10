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

import shutil
from dataclasses import dataclass, field
from typing import Optional

from .sysview import SystemView


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


ALL_RULES = [
    rule_radiod_resolution,
    rule_frequency_coverage,
    rule_cpu_isolation,
    rule_timing_chain,
    rule_disk_budget,
    rule_channel_count,
]


def run_all(view: SystemView) -> list:
    return [rule(view) for rule in ALL_RULES]


def worst_severity(results: list) -> str:
    order = {"pass": 0, "warn": 1, "fail": 2}
    worst = "pass"
    for r in results:
        if order.get(r.severity, 0) > order.get(worst, 0):
            worst = r.severity
    return worst
