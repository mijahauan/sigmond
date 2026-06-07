"""Lifecycle state inference for sigmond-managed components.

A component progresses through these stages:

    available  →  installed  →  configured  →  enabled  →  running

`available`   the source repo is cloned at /opt/git/sigmond/<name>/ but the
              operator hasn't run any setup steps yet (no [build] produces
              artifacts on disk).

`installed`   the deploy.toml's [build] produces paths all exist (typically
              /opt/<name>/venv/bin/<name> and friends — `smd install` ran).

`configured`  every [[install.steps]] kind="render" dst path exists (typically
              /etc/<name>/<name>-config.toml — the operator ran a config
              wizard or manually populated the file).

`enabled`     /etc/sigmond/topology.toml has [component.<name>] enabled = true.

`running`     at least one of the deploy.toml [systemd] units is active.

Constraint: `smd start` / `smd restart` REFUSE to operate on components that
are not yet configured.  The state model is the single source of truth for
that check.

This module is pure (no side effects) — readers query the filesystem and
topology; they don't mutate anything.
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

GIT_BASE = Path("/opt/git/sigmond")


@dataclass(frozen=True)
class ComponentState:
    """Inferred lifecycle state for one component."""

    name: str

    # Filesystem signals (each independently true/false)
    cloned:     bool   # source repo exists at /opt/git/sigmond/<name>/
    installed:  bool   # all [build] produces paths exist
    configured: bool   # all [[install.steps]] render dst paths exist
    enabled:    bool   # topology entry exists with enabled = true
    active:     bool   # at least one [systemd] unit is currently active

    # Number of declared units that are inactive (for display)
    inactive_unit_count: int = 0
    active_unit_count:   int = 0

    # ----- derived properties -----

    @property
    def stage(self) -> str:
        """Single keyword summarising the most-advanced stage reached."""
        if self.active:
            return "running"
        if self.enabled:
            return "enabled"
        if self.configured:
            return "configured"
        if self.installed:
            return "installed"
        if self.cloned:
            return "downloaded"   # source cloned but not yet built
        return "available"        # in the catalog, not downloaded

    @property
    def can_start(self) -> bool:
        """True iff a `smd start` of this component is safe to attempt.

        Hard gate: must be installed AND configured.  Sigmond refuses to
        invoke systemctl on a component that hasn't been through both
        `smd install` and `smd config init` — would either fail with a
        cryptic error from the unit file or silently produce no output.
        """
        return self.installed and self.configured

    @property
    def display_status(self) -> str:
        """Operator-friendly one-liner naming the next command needed.

        Drops the "sudo " prefix from suggested commands — sigmond now
        auto-elevates via sudo when invoked as a non-root user, so the
        prefix is just noise.
        """
        if not self.cloned:
            return f"available — needs: smd install {self.name}"
        if not self.installed:
            return f"downloaded — needs: smd install {self.name}"
        if not self.configured:
            return f"installed — needs: smd config init {self.name}"
        if not self.enabled:
            return f"configured — enable with: smd enable {self.name}"
        if not self.active:
            return "enabled, stopped"
        return "enabled, running"


# ---------------------------------------------------------------------------
# Detection helpers (pure)
# ---------------------------------------------------------------------------


def _read_deploy_toml(name: str) -> Optional[dict]:
    """Return parsed deploy.toml for a component, or None if missing."""
    path = GIT_BASE / name / "deploy.toml"
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _read_shim_deploy(name: str) -> Optional[dict]:
    """Synthesized lifecycle shim for deploy-less components (non-conformant
    infra like igmp-querier carries its [systemd] units here)."""
    path = Path("/etc/sigmond/clients") / f"{name}.deploy.toml"
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _produces_paths(deploy: dict) -> list[Path]:
    """Paths that should exist after a successful `smd install`.

    Reads [build].produces (a list of absolute path strings).
    """
    build = deploy.get("build") or {}
    produces = build.get("produces") or []
    return [Path(p) for p in produces if isinstance(p, str)]


def _render_dst_paths(deploy: dict) -> list[Path]:
    """Paths that the install steps' kind="render" entries produce.

    These are the config files an operator would populate (template
    -> /etc/<name>/<name>-config.toml etc.).  A component is "configured"
    iff every render dst exists.
    """
    install = deploy.get("install") or {}
    steps = install.get("steps") or []
    out: list[Path] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("kind") == "render":
            dst = step.get("dst")
            if isinstance(dst, str):
                out.append(Path(dst))
    return out


def _systemd_unit_names(deploy: dict) -> list[str]:
    """Concrete + templated unit names the deploy.toml claims this component
    contributes to systemd."""
    sd = deploy.get("systemd") or {}
    out: list[str] = []
    for u in sd.get("units") or []:
        if isinstance(u, str):
            out.append(u)
    for u in sd.get("templated_units") or []:
        if isinstance(u, str):
            out.append(u)
    return out


def _expand_running_instances(template: str) -> list[str]:
    """Given a templated unit name like 'foo@.service', enumerate currently
    active instances via `systemctl list-units --state=active`."""
    if "@." not in template:
        return [template]
    pattern = template.replace("@.", "@*.")
    r = subprocess.run(
        ["systemctl", "list-units", "--type=service", "--no-legend",
         "--no-pager", "--state=active", pattern],
        capture_output=True, text=True,
    )
    out: list[str] = []
    for line in r.stdout.splitlines():
        parts = line.strip().split()
        if parts:
            out.append(parts[0])
    return out


def _any_unit_active(deploy: dict) -> tuple[bool, int, int]:
    """Returns (any_active, active_count, total_instance_count).

    `total_instance_count` is best-effort — for templated units we count
    only the active instances (we don't know "how many should there be"
    without parsing the operator's per-instance config), but for concrete
    units we always count exactly one.
    """
    units = _systemd_unit_names(deploy)
    if not units:
        return (False, 0, 0)
    active_n = 0
    total = 0
    for u in units:
        if "@." in u:
            instances = _expand_running_instances(u)
            active_n += len(instances)
            total += len(instances)
        else:
            total += 1
            r = subprocess.run(
                ["systemctl", "is-active", "--quiet", u],
                capture_output=True,
            )
            if r.returncode == 0:
                active_n += 1
    return (active_n > 0, active_n, total)


def _topology_enabled(topology, name: str) -> bool:
    """Read enabled flag for a component from a topology object.

    Returns False if the component isn't declared in the topology at all.
    """
    components = getattr(topology, "components", None)
    if components is None and isinstance(topology, dict):
        components = topology.get("components") or {}
    if not components:
        return False
    comp = components.get(name) if hasattr(components, "get") else None
    if comp is None:
        return False
    enabled = getattr(comp, "enabled", None)
    if enabled is None and hasattr(comp, "get"):
        enabled = comp.get("enabled")
    return bool(enabled)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _active_via_lifecycle(*candidate_names: str) -> tuple[bool, int, int]:
    """Fallback active-check for components without a deploy.toml.

    Tries each candidate name in turn against sigmond.lifecycle.resolve_units,
    which has a hardcoded shim for components like ka9q-radio (where the
    unit set isn't declared in a deploy.toml because the upstream C project
    doesn't carry sigmond contract metadata).  The catalog entry's name
    and its topology_alias both feed in here — radiod's catalog name is
    "radiod" but the shim is keyed under "ka9q-radio".
    """
    try:
        from sigmond.lifecycle import resolve_units
    except ImportError:
        return (False, 0, 0)
    for name in candidate_names:
        if not name:
            continue
        try:
            units = resolve_units([name], [name])
        except Exception:
            continue
        if not units:
            continue
        active_n = 0
        total = 0
        for u in units:
            if u.orphaned:
                continue
            total += 1
            r = subprocess.run(
                ["systemctl", "is-active", "--quiet", u.unit],
                capture_output=True,
            )
            if r.returncode == 0:
                active_n += 1
        return (active_n > 0, active_n, total)
    return (False, 0, 0)


SYSTEMD_SYSTEM = Path("/etc/systemd/system")

# Ordered lifecycle, 1..6 — drives the progress display.
STAGES = ("available", "downloaded", "installed",
          "configured", "enabled", "running")
_STAGE_NEXT = {
    "available":  "smd install {n}",
    "downloaded": "smd install {n}",
    "installed":  "smd config init {n}",
    "configured": "smd enable {n}",
    "enabled":    "smd start {n}",
    "running":    "",
}


def stage_index(stage: str) -> int:
    """1-based position (available=1 .. running=6); 0 if unknown."""
    try:
        return STAGES.index(stage) + 1
    except ValueError:
        return 0


def progress_bar(stage: str, total: int = 6,
                 filled: str = "\u25cf", empty: str = "\u25cb") -> str:
    """A 6-segment progress glyph, filled up to the current stage."""
    n = stage_index(stage)
    return filled * n + empty * max(0, total - n)


def next_hint(stage: str, name: str) -> str:
    """The next command to run from this stage (empty when nothing left)."""
    tmpl = _STAGE_NEXT.get(stage, "")
    return tmpl.format(n=name) if tmpl else ""


def _needs_config(deploy) -> bool:
    """A component needs a 'configured' stage iff it declares a [contract.config]
    entry or a kind='render' install step (an operator-populated file)."""
    if not deploy:
        return False
    if (deploy.get("contract") or {}).get("config"):
        return True
    for step in (deploy.get("install") or {}).get("steps") or []:
        if isinstance(step, dict) and step.get("kind") == "render":
            return True
    return False


def _deployless_runs(name: str) -> bool:
    """Deploy-less components that are nonetheless long-running services."""
    return name in ("ka9q-radio", "ka9q-web", "radiod")


def applicable_stages(name: str, deploy=None, kind: str = None) -> list:
    """The lifecycle stages that actually apply to this component.

    Libraries/tools terminate at 'installed'; components with no systemd units
    skip enable/run; components with no config step skip 'configured'.  So the
    track length itself conveys how involved a package is.
    """
    base = ["available", "downloaded", "installed"]
    if kind == "library":
        # Editable source deps: cloning IS installing (no build/config/enable;
        # consumers pip-install -e from the clone).  Download == install.
        return ["available", "installed"]
    if deploy is None:
        deploy = _read_deploy_toml(name) or _read_shim_deploy(name)
    has_units = (bool(_systemd_unit_names(deploy)) if deploy
                 else _deployless_runs(name))
    stages = list(base)
    if _needs_config(deploy):
        stages.append("configured")
    if has_units:
        stages += ["enabled", "running"]
    return stages


def stage_progress(state: "ComponentState", track: list) -> tuple:
    """Return (pos, reached_stage, next_stage) within *track* — the furthest
    contiguous stage the component has satisfied."""
    satisfied = {
        "available":  True,
        "downloaded": state.cloned,
        "installed":  state.installed,
        "configured": state.configured,
        "enabled":    state.enabled,
        "running":    state.active,
    }
    pos = 0
    for i, stg in enumerate(track):
        if satisfied.get(stg, False):
            pos = i
        else:
            break
    nxt = track[pos + 1] if pos + 1 < len(track) else None
    return pos, track[pos], nxt


def _install_artifacts_present(name: str, deploy) -> bool:
    """'smd install ran' signal when no [build].produces is declared: a clone
    is not enough — look for real build/link artifacts."""
    base = GIT_BASE / name
    if (base / "venv").exists():
        return True
    for d in (Path("/usr/local/bin"), Path("/usr/local/sbin")):
        if (d / name).exists():
            return True
    if deploy is not None:
        for u in _systemd_unit_names(deploy):
            if (SYSTEMD_SYSTEM / u).exists():
                return True
        for step in (deploy.get("install") or {}).get("steps") or []:
            if isinstance(step, dict) and step.get("kind") == "link":
                dst = step.get("dst")
                if isinstance(dst, str) and Path(dst).exists():
                    return True
    return False


def _radiod_built(name: str) -> bool:
    """ka9q-radio's binary lives at /usr/local/sbin/radiod (name != binary)."""
    if name in ("ka9q-radio", "radiod"):
        return Path("/usr/local/sbin/radiod").exists()
    if name == "ka9q-web":
        return Path("/usr/local/sbin/ka9q-web").exists()
    return False


def compute_state(name: str, topology=None, alias: str = None) -> ComponentState:
    """Compute the lifecycle state for one component name.

    `topology` may be a sigmond.topology.Topology object or a dict-like
    structure with a `components` mapping.  When None, the component's
    `enabled` flag is reported as False (caller can pass a loaded
    topology if they want richer reporting).

    `alias` is the catalog entry's topology_alias when set — used as a
    second-chance lookup against sigmond.lifecycle's fallback shims.
    radiod's catalog name is "radiod" but the shim is keyed on
    "ka9q-radio" (the URL stem).  Without alias, components with a
    different catalog name vs lifecycle key would never match.
    """
    cloned = (GIT_BASE / name).exists()
    deploy = _read_deploy_toml(name) if cloned else None
    # Topology may key the entry under either the catalog name or the
    # topology_alias (radiod's catalog name is "radiod" but topology
    # has [component.ka9q-radio]).  Check both.
    enabled = False
    if topology is not None:
        enabled = (_topology_enabled(topology, name) or
                   (bool(alias) and _topology_enabled(topology, alias)))

    if not cloned:
        return ComponentState(
            name=name, cloned=False, installed=False, configured=False,
            enabled=enabled, active=False,
        )

    if deploy is None:
        # Component is cloned but doesn't ship a deploy.toml (radiod's
        # upstream ka9q-radio is the canonical example — no sigmond
        # install.sh, the binary lives at /usr/local/sbin/radiod from
        # an out-of-band ka9q-update build).  No filesystem fingerprint
        # we can verify against, so just trust the cloned state: assume
        # installed AND configured.  Display will show "enabled, stopped"
        # or "enabled, running" — the only states meaningful for a
        # build-it-yourself component.
        active, active_n, total_n = _active_via_lifecycle(name, alias)
        built = active or _install_artifacts_present(name, None) or _radiod_built(name)
        return ComponentState(
            name=name,
            cloned=True,
            installed=built,
            configured=built,
            enabled=enabled,
            active=active,
            active_unit_count=active_n,
            inactive_unit_count=max(0, total_n - active_n),
        )

    produces = _produces_paths(deploy)
    # Prefer the declared [build].produces fingerprint.  When absent (most
    # clients don't declare it), a clone alone is NOT "installed" — infer from
    # real build artifacts (venv / linked binary / linked units) instead.
    if produces:
        installed = all(p.exists() for p in produces)
    else:
        installed = _install_artifacts_present(name, deploy)

    renders = _render_dst_paths(deploy)
    # When there are no render steps declared, treat as configured by
    # default — the component declares no operator-supplied state.
    configured = (not renders) or all(p.exists() for p in renders)

    enabled = _topology_enabled(topology, name) if topology is not None else False

    active, active_n, total_n = _any_unit_active(deploy)

    # Reality wins: a component whose declared units are currently active
    # must be both installed AND configured by definition (systemd would
    # not be running it otherwise).  This bypasses brittle filesystem
    # fingerprinting for components like radiod whose binary lives outside
    # the source tree (/usr/local/sbin/radiod) and whose deploy.toml may
    # not declare [build].produces.
    if active:
        installed = True
        configured = True

    return ComponentState(
        name=name,
        cloned=cloned,
        installed=installed,
        configured=configured,
        enabled=enabled,
        active=active,
        active_unit_count=active_n,
        inactive_unit_count=max(0, total_n - active_n),
    )
