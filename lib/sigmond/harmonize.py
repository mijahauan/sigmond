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
import os
import re
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


# The install-rendered placeholder recorder configs ship for the radiod status;
# `config init` replaces it.  A config that still carries it is unconfigured.
_RADIOD_STATUS_PLACEHOLDER = "<configure-via-config-init>"


def rule_radiod_status_configured(view: SystemView) -> RuleResult:
    """A recorder whose radiod status is still the install-rendered
    ``<configure-via-config-init>`` placeholder is UNCONFIGURED — it can't
    resolve radiod and won't decode.  This used to pass silently (validate skips
    an inactive instance), so a half-configured station read green; flag it so
    that can't happen again.  Caught live on the first greenfield reinstall:
    wspr-recorder came up with a placeholder radiod address."""
    bad = []
    affected = []
    for cv in view.client_views.values():
        for iv in cv.instances:
            if iv.radiod_id and _RADIOD_STATUS_PLACEHOLDER in iv.radiod_id:
                bad.append(f"{cv.client_type}@{iv.instance}")
                affected.append(cv.client_type)
    if bad:
        return RuleResult(
            "radiod_status_configured", "fail",
            f"unconfigured radiod status (placeholder) in {', '.join(sorted(bad))}"
            f" — run `smd config init <client> --reconfig`",
            sorted(set(affected)))
    return RuleResult("radiod_status_configured", "pass",
                      "no placeholder radiod addresses", [])


def rule_frequency_coverage(view: SystemView) -> RuleResult:
    """Per-radiod: any consumed frequency must fit within samprate/2 of
    the radiod's tuning range.  Phase 1 uses a coarse heuristic — we just
    check that requested frequencies are below samprate_hz (Nyquist
    envelope) since we don't know radiod's RF tuning without a full conf
    parse.

    The rule needs three things to actually check anything:
      1. At least one local radiod declared in coordination.toml
      2. That radiod has a samprate_hz declared
      3. At least one bound client publishes frequencies_hz via its
         client-inventory (today: hf-timestd's `provides_timing_*`
         block; psk/wspr/hfdl/codar would need to add inventory
         entries listing the bands they consume — CLIENT-CONTRACT
         work, not coordination.toml work).

    Skip diagnostics differentiate which of the three is missing so
    operators know whether to edit coordination.toml or the client's
    own config / inventory.
    """
    coord = view.coordination
    violations = []
    checked = 0
    local_radiods = 0
    local_radiods_with_samprate = 0
    total_consumers = 0
    for rid, radiod in coord.radiods.items():
        if not radiod.is_local:
            continue    # remote radiod — someone else's problem
        local_radiods += 1
        if not radiod.samprate_hz:
            continue    # operator hasn't declared coverage info
        local_radiods_with_samprate += 1
        consumers = _consumers_of(view, rid)
        total_consumers += len(consumers)
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
    if checked > 0:
        return RuleResult("frequency_coverage", "pass",
                          f"{checked} frequency claim(s) within samprate", [])
    # checked == 0 — diagnose which of the three preconditions is missing.
    if local_radiods == 0:
        msg = "skipped (no local radiod declared)"
    elif local_radiods_with_samprate == 0:
        msg = "skipped (local radiod samprate_hz not declared)"
    elif total_consumers == 0:
        msg = "skipped (no client instances bound to local radiod)"
    else:
        # Consumers exist but none publish frequencies_hz.  This is the
        # CLIENT-CONTRACT inventory gap: psk/wspr/hfdl/codar daemons
        # would need to expose their band list via the client-view
        # inventory mechanism for this check to fire.  Until then the
        # rule passes-via-skip rather than warning.
        msg = ("skipped (no client publishes frequencies_hz via "
               "inventory — CLIENT-CONTRACT enhancement)")
    return RuleResult("frequency_coverage", "pass", msg, [])


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

    # Governor expectation is configurable via topology.toml
    # [cpu_affinity] radiod_governor.  Default 'performance' for
    # greenfield (max throughput); operators with thermal budgets
    # can set 'schedutil' or another governor and the check
    # respects that without flagging it.
    expected_gov = view.topology.cpu_affinity.get('radiod_governor',
                                                  'performance')
    bad_gov = [(cpu, report.capabilities.governors[cpu])
               for cpu in sorted(report.radiod_cpus)
               if report.capabilities.governors.get(cpu)
               and report.capabilities.governors[cpu] != expected_gov]
    if bad_gov:
        sample = ', '.join(f"cpu{c}={g}" for c, g in bad_gov[:4])
        more = f' (+{len(bad_gov) - 4} more)' if len(bad_gov) > 4 else ''
        issues.append(
            f"governor not {expected_gov!r} on radiod cores: {sample}{more}")

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

    Tries the canonical sigmond install path first, then the legacy
    /opt/git path (pre-Pattern A), then the per-user dev sibling.
    Returns the first one that contains a ``.git`` dir.  Returns None
    if none are present (the rule then skips cleanly).
    """
    import os
    home = os.path.expanduser('~')
    for candidate in (Path('/opt/git/sigmond/ka9q-radio'),
                      Path('/opt/git/ka9q-radio'),
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
            "/opt/git/sigmond/ka9q-radio, /opt/git/ka9q-radio, "
            "or ~/ka9q-radio", [],
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
        "`smd install` (sigmond's in-tree builder checks out ka9q-radio at "
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


def rule_data_path_upstream(view: SystemView) -> RuleResult:
    """CONTRACT-v0.5 §16.3.1: meta-clients name an upstream sibling.

    For every instance whose ``data_path.kind == "file"`` and whose
    ``details.upstream_client`` is set, verify the named sibling exists
    in the catalog.  A meta-client that names an unknown upstream is
    not a hard failure — the meta-client may still function — but it
    means sigmond cannot cross-reference the upstream's inventory for
    the radiod-side facts that live there, so we surface a warn.
    """
    from .catalog import load_catalog, get_entry

    try:
        catalog = load_catalog()
    except Exception:
        return RuleResult(
            "data_path_upstream", "pass",
            "skipped (catalog unavailable)", [],
        )

    unresolved: list[str] = []
    affected: list[str] = []
    checked = 0
    for cv in view.client_views.values():
        for iv in cv.instances:
            dp = iv.data_path
            if not isinstance(dp, dict):
                continue
            if dp.get("kind") != "file":
                continue
            details = dp.get("details") or {}
            upstream = details.get("upstream_client")
            if not upstream:
                continue            # replay/test data — no upstream named
            checked += 1
            if get_entry(upstream, catalog) is None:
                unresolved.append(
                    f"{cv.client_type}@{iv.instance} → {upstream}"
                )
                affected.append(cv.client_type)

    if unresolved:
        return RuleResult(
            "data_path_upstream", "warn",
            "meta-client(s) name upstream sibling not in catalog: "
            + "; ".join(unresolved),
            sorted(set(affected)),
        )
    if checked == 0:
        return RuleResult(
            "data_path_upstream", "pass",
            "skipped (no meta-client instances declare upstream)", [],
        )
    return RuleResult(
        "data_path_upstream", "pass",
        f"all {checked} meta-client upstream(s) resolved", [],
    )


def rule_timing_reference(view: SystemView) -> RuleResult:
    """Runtime health of the local GPS timing-reference chain
    (GPSDO -> gpsd -> chrony -> hf-timestd).  Observability only; remediation
    lives in `smd admin timing reconcile`.  See docs/timing-chain-architecture.md.
    Skipped on hosts with no local gpsd (remote-radiod / no GPS)."""
    if not (shutil.which("gpsd") or Path("/usr/sbin/gpsd").exists()):
        return RuleResult("timing_reference", "pass",
                          "skipped (no local gpsd / GPS timing chain)", [])
    try:
        from .commands.timing import gather_facts, assess
        links = assess(gather_facts(quick=True))
    except Exception as exc:                       # noqa: BLE001
        return RuleResult("timing_reference", "pass",
                          f"skipped (timing probe failed: {exc})", [])
    by = {l.name: l for l in links}
    fails = [l for l in links if l.status == "fail"]
    warns = [l for l in links if l.status == "warn"]
    chrony = by.get("chrony")
    gps = by.get("gps-feed")
    summary = (chrony.detail if chrony and chrony.status == "ok"
               else (gps.detail if gps else "chain present"))
    if fails:
        detail = "; ".join(f"{l.name}: {l.detail}" for l in fails)
        return RuleResult("timing_reference", "fail",
                          f"{detail}.  Remediate: smd admin timing reconcile",
                          [l.name for l in fails])
    if warns:
        return RuleResult("timing_reference", "warn",
                          f"{summary}; warming/incomplete: "
                          f"{', '.join(l.name for l in warns)}",
                          [l.name for l in warns])
    return RuleResult("timing_reference", "pass", summary, [])


def rule_wspr_decode_enabled(view: SystemView) -> RuleResult:
    """Runtime: an *active* wspr-recorder instance with WD_DECODE_VIA_DB
    unset runs in recorder-only mode — it captures period WAVs but never
    decodes them, so it produces ZERO spots, silently.  On a sigmond host
    there is no legacy ``wd-decode@*`` bash chain to pick up the slack, so
    this is a real (and otherwise invisible) misconfiguration.

    The greenfield default is now seeded from the client's deploy.toml
    ``[contract.instance_env]`` (WD_DECODE_VIA_DB=1); this rule catches
    instances provisioned before that, or hand-edited envs.  Skipped when
    wspr-recorder isn't active, or when a legacy wsprdaemon-client decode
    chain is present (recorder-only is intentional there)."""
    import subprocess
    env_dir = Path("/etc/wspr-recorder/env")
    if not env_dir.is_dir():
        return RuleResult("wspr_decode_enabled", "pass",
                          "skipped (no wspr-recorder configured)", [])
    if Path("/opt/wsprdaemon-client/bin/decoders").is_dir():
        return RuleResult("wspr_decode_enabled", "pass",
                          "skipped (legacy wsprdaemon-client decode chain present)", [])
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=active",
             "--no-legend", "--plain", "wspr-recorder@*.service"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:                                  # noqa: BLE001
        return RuleResult("wspr_decode_enabled", "pass",
                          "skipped (systemctl unavailable)", [])
    active = [ln.split()[0] for ln in out.splitlines() if ln.strip()]
    if not active:
        return RuleResult("wspr_decode_enabled", "pass",
                          "skipped (no active wspr-recorder instance)", [])

    def _decode_on(unit: str) -> bool:
        inst = unit[len("wspr-recorder@"):-len(".service")]
        forms = {inst}
        if "\\x" in inst:
            try:
                forms.add(inst.encode().decode("unicode_escape"))
            except Exception:                          # noqa: BLE001
                pass
        for name in forms:
            envf = env_dir / f"{name}.env"
            if not envf.exists():
                continue
            for line in envf.read_text().splitlines():
                line = line.strip()
                if line.startswith("WD_DECODE_VIA_DB") and "=" in line:
                    return line.split("=", 1)[1].strip() not in ("", "0")
        return False

    recorder_only = [u[len("wspr-recorder@"):-len(".service")]
                     for u in active if not _decode_on(u)]
    if recorder_only:
        return RuleResult(
            "wspr_decode_enabled", "warn",
            "wspr-recorder in recorder-only mode (WD_DECODE_VIA_DB unset) — "
            "captures WAVs but produces NO spots: " + ", ".join(recorder_only)
            + ".  Fix: add WD_DECODE_VIA_DB=1 to the instance env "
            "(/etc/wspr-recorder/env/<id>.env) and restart the unit.",
            ["wspr-recorder"])
    return RuleResult("wspr_decode_enabled", "pass",
                      f"decode enabled on {len(active)} active instance(s)", [])


from . import hardware


def _hardware_ready(component: str):
    """Tri-state hardware readiness — indirection so tests can monkeypatch."""
    return hardware.hardware_ready(component)


# Map of component -> human hardware label for hardware-gated core components.
# DECLARED IN CONFIG, not code: each is the `hardware_gated` field of the
# component's catalog entry (etc/catalog.toml [client.<name>]).  Readiness comes
# from the client's own `inventory --json hardware_present` self-describe
# (CONTRACT §3 / Phase D) via sigmond.hardware.  Cached per-process; tests
# monkeypatch _hardware_gated_registry (and _hardware_ready / _unit_active) to
# stay hermetic.
_HW_GATED_CACHE: Optional[dict] = None


def _hardware_gated_registry() -> dict:
    """Load {component: hardware_label} from the catalog's `hardware_gated`
    declarations.  Cached; empty on any load error (gating simply goes dark
    rather than crashing a rule)."""
    global _HW_GATED_CACHE
    if _HW_GATED_CACHE is None:
        reg: dict = {}
        try:
            from .catalog import load_catalog
            for name, entry in load_catalog().items():
                label = getattr(entry, "hardware_gated", None)
                if label:
                    reg[name] = label
        except Exception:                              # noqa: BLE001
            reg = {}
        _HW_GATED_CACHE = reg
    return _HW_GATED_CACHE


def _unit_active(pattern: str) -> bool:
    """True when at least one active service unit matches ``pattern``."""
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=active",
             "--no-legend", "--plain", pattern],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:                                  # noqa: BLE001
        return False
    return any(ln.strip() for ln in out.splitlines())


def rule_hardware_gated_core(view: SystemView) -> RuleResult:
    """Runtime: a hardware-gated core component (e.g. mag-recorder) that is
    ENABLED in topology — i.e. expected on this host — but whose hardware is
    absent is legitimately *dormant*, not broken.

    Without this rule such a component simply vanishes from ``validate``: a
    dasi2 host with no magnetometer reads as silently incomplete.  This rule
    makes it visible as "core-but-gated (expected, dormant)" so the host reads
    as complete-with-one-client-dormant.  When the hardware IS present but the
    component isn't running, that's an actionable gap (warn).  Components not
    enabled here (e.g. under the base/client profiles) are skipped."""
    dormant: list = []
    not_running: list = []
    affected: list = []
    for comp, hw_label in _hardware_gated_registry().items():
        if not view.is_enabled(comp):
            continue                       # not expected on this host
        ready = _hardware_ready(comp)
        if ready is None:
            continue                       # readiness unknown — don't guess
        if not ready:
            dormant.append(f"{comp} (no {hw_label})")
        elif not _unit_active(f"{comp}.service"):
            not_running.append(f"{comp} ({hw_label} present)")
            affected.append(comp)
    if not_running:
        return RuleResult(
            "hardware_gated_core", "warn",
            "hardware present but core component not running: "
            + "; ".join(not_running)
            + " — configure + start it (smd config init <name>; smd start)",
            sorted(set(affected)))
    if dormant:
        return RuleResult(
            "hardware_gated_core", "pass",
            "core-but-gated (expected, dormant — hardware absent): "
            + "; ".join(dormant), [])
    return RuleResult("hardware_gated_core", "pass",
                      "skipped (no hardware-gated core components enabled)", [])


def dormant_reason(component: str, *, enabled: bool):
    """For per-component status surfaces (TUI/CLI): the human hardware label
    when ``component`` is an *enabled*, hardware-gated core component whose
    hardware is absent — i.e. it should read as **dormant** rather than just
    stopped/running.  Returns None otherwise (not gated, hardware present, not
    enabled, or readiness unknown).  Shares rule_hardware_gated_core's source of
    truth so the components table and ``smd validate`` never disagree."""
    if not enabled:
        return None
    label = _hardware_gated_registry().get(component)
    if label is None:
        return None
    return label if _hardware_ready(component) is False else None


# ---------------------------------------------------------------------------
# Delivered per-site secrets (read-only presence/validity check).
#
# Cross-ref: bin/smd `smd admin secrets` owns the authoritative installer and
# validators for these same files; this rule is the validate-side check. Keep
# the two format checks in sync. SSH-key secrets self-generate on-host and are
# intentionally out of scope. See PROVISIONING-INPUTS.md §10.
# ---------------------------------------------------------------------------
_SECRET_EARTHDATA = Path('/etc/hf-timestd/earthdata-netrc')
_SECRET_FRPC = Path('/etc/sigmond/frpc.toml')
# hs-uploader SFTP key (wspr -> wsprdaemon.org).  It SELF-GENERATES 0600 on
# first upload, so absence is valid (like earthdata); we only flag a present
# key with wrong perms.  The un-automatable step — enrolling its PUBLIC key
# with wsprdaemon.org — is surfaced by the runbook and `smd config upload`.
_SECRET_HS_UPLOADER_KEY = Path('/etc/hs-uploader/keys/id_ed25519')


def _secret_key_perms_problem(path: Path) -> Optional[str]:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return None
    return f'mode {oct(mode)} (must be 0600)' if mode != 0o600 else None


def _secret_earthdata_problem(path: Path) -> Optional[str]:
    # Unreadable (0600, run as non-root) → can't validate; don't flag.
    if not os.access(path, os.R_OK):
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    if 'urs.earthdata.nasa.gov' not in text:
        return "missing 'machine urs.earthdata.nasa.gov'"
    if 'YOUR_' in text:
        return 'contains placeholder values'
    return None


def _secret_frpc_problem(path: Path) -> Optional[str]:
    if not os.access(path, os.R_OK):
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    m = re.search(r'token\s*=\s*"([^"]*)"', text)
    if not m or not m.group(1) or 'FROM_WD_ADMIN' in m.group(1) \
            or m.group(1).startswith('<'):
        return 'token is a placeholder/empty'
    return None


def rule_secrets(view: SystemView) -> RuleResult:
    """Delivered per-site secrets present/valid where needed.

    Earthdata is optional enrichment — its absence is a valid choice, so only a
    present-but-broken file is flagged. RAC's frpc.toml is required when the rac
    component is enabled; a placeholder token is flagged whenever the file
    exists. Run as root for full content validation (0600 files).
    """
    problems = []
    affected = []

    if _SECRET_EARTHDATA.exists():
        p = _secret_earthdata_problem(_SECRET_EARTHDATA)
        if p:
            problems.append(f'earthdata-netrc: {p}')
            affected.append(str(_SECRET_EARTHDATA))

    rac_on = view.is_enabled('rac')
    if rac_on and not _SECRET_FRPC.exists():
        problems.append('rac enabled but /etc/sigmond/frpc.toml is missing')
        affected.append(str(_SECRET_FRPC))
    elif _SECRET_FRPC.exists():
        p = _secret_frpc_problem(_SECRET_FRPC)
        if p:
            problems.append(f'frpc.toml: {p}')
            affected.append(str(_SECRET_FRPC))

    # hs-uploader SFTP key: only a present-but-wrong-perms key is a problem
    # (it self-generates 0600; absence resolves on first upload run).
    if _SECRET_HS_UPLOADER_KEY.exists():
        p = _secret_key_perms_problem(_SECRET_HS_UPLOADER_KEY)
        if p:
            problems.append(f'hs-uploader key: {p}')
            affected.append(str(_SECRET_HS_UPLOADER_KEY))

    if problems:
        return RuleResult('secrets', 'warn', '; '.join(problems), affected)
    return RuleResult('secrets', 'pass',
                      'delivered secrets present/valid (SSH keys self-generate; '
                      'optional secrets may be absent)', [])


# ---------------------------------------------------------------------------
# Upload-enable visibility (the decode->upload gap)
#
# Surfaces the otherwise-invisible "this instance decodes but never uploads"
# state: a radiod-keyed recorder whose per-instance env doesn't set its upload
# enable flag.  Mirrors rule_wspr_decode_enabled (checked per <client>@<inst>).
# Deliberately narrow and deferential: per-installation IDENTITY is owned by
# site-profile.toml / `smd config render` (coordination.env), and CREDENTIALS
# by `smd admin secrets` (+ rule_secrets) — this rule re-checks NEITHER.  It
# only reports the per-instance enable posture so onboarding isn't silent.
# ---------------------------------------------------------------------------

def _upload_truthy(v) -> bool:
    return v is not None and str(v).strip().lower() not in ("", "0", "false", "no", "off")


def _read_env_file(path: Path) -> dict:
    out: dict = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:                                      # noqa: BLE001
        pass
    return out


def _resolve_instance_env(client: str, inst: str) -> dict:
    """Per-instance env for `<client>@<inst>`.  Handles systemd's escaped
    instance form (e.g. AC0G\\x3dS) vs the env-file name (AC0G=S.env)."""
    env_dir = Path(f"/etc/{client}/env")
    forms = {inst}
    if "\\x" in inst:
        try:
            forms.add(inst.encode().decode("unicode_escape"))
        except Exception:                                  # noqa: BLE001
            pass
    for name in forms:
        f = env_dir / f"{name}.env"
        if f.exists():
            return _read_env_file(f)
    return {}


def _active_instances(client: str) -> list:
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=active",
             "--no-legend", "--plain", f"{client}@*.service"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:                                      # noqa: BLE001
        return []
    names = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        u = ln.split()[0]
        if u.startswith(f"{client}@") and u.endswith(".service"):
            names.append(u[len(f"{client}@"):-len(".service")])
    return names


# Per-client upload enable flag (env var) -> the destination(s) it gates.
# Intentionally tiny — the same in-code idiom as rule_wspr_decode_enabled's
# WD_DECODE_VIA_DB.  No contract surface: identity is site-profile's, the
# credential is `smd admin secrets`'.
from .upload import UPLOAD_ENABLE as _UPLOAD_ENABLE


def _client_installed(client: str) -> bool:
    """Indirection so tests can monkeypatch installed-client detection."""
    return Path(f"/etc/{client}").exists()


def rule_upload_enabled(view: SystemView) -> RuleResult:
    """Runtime: flag any ACTIVE radiod-keyed recorder instance whose
    per-instance env does not enable upload — it decodes to the local sink but
    silently never ships.  Off MAY be intentional on a decode-only /
    merge-feeder node, so this is a soft warn for visibility, not a hard fail.
    Identity (site-profile) and credentials (`smd admin secrets`) are owned
    elsewhere and intentionally not re-checked here."""
    coord = _read_env_file(Path("/etc/sigmond/coordination.env"))
    off = []
    for client, (flag, dest) in _UPLOAD_ENABLE.items():
        if not _client_installed(client):
            continue
        for inst in _active_instances(client):
            env = dict(coord)
            env.update(_resolve_instance_env(client, inst))
            if not _upload_truthy(env.get(flag)):
                off.append(f"{client}@{inst} -> {' / '.join(dest)}")
    if not off:
        return RuleResult("upload_enabled", "pass",
                          "active recorder instances have upload enabled "
                          "(or none installed/active)", [])
    return RuleResult(
        "upload_enabled", "warn",
        "decoding but NOT uploading (spots stay in the local sink): "
        + "; ".join(off)
        + ".  If this host should report, set the enable flag in "
          "/etc/<client>/env/<id>.env (identity comes from `smd config "
          "render`; credentials from `smd admin secrets`).  Ignore on a "
          "decode-only / merge-feeder node.",
        sorted({o.split("@")[0] for o in off}))


ALL_RULES = [
    rule_radiod_resolution,
    rule_radiod_status_configured,
    rule_frequency_coverage,
    rule_cpu_isolation,
    rule_timing_chain,
    rule_disk_budget,
    rule_channel_count,
    rule_ka9q_python_compat,
    rule_data_path_upstream,
]

# Rules that read live /sys, /proc, and systemctl state.  Kept out of
# ALL_RULES so unit tests with hand-built views don't pick up host state.
ALL_RUNTIME_RULES = [
    rule_cpu_isolation_runtime,
    rule_gpsdo_governor_coverage,
    rule_kernel_rcvbuf_adequate,
    rule_timing_reference,
    rule_wspr_decode_enabled,
    rule_hardware_gated_core,
    rule_secrets,
    rule_upload_enabled,
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
