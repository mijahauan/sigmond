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
            return "available"
        return "missing"

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

        Lines up with the table in the design doc — every line either
        states the stage or names the next command.
        """
        if not self.cloned:
            return "missing — repo not cloned"
        if not self.installed:
            return f"available — needs: sudo smd install {self.name}"
        if not self.configured:
            return f"installed — needs: sudo smd config init {self.name}"
        if not self.enabled:
            return f"configured — enable with: sudo smd enable {self.name}"
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
    enabled = _topology_enabled(topology, name) if topology is not None else False

    if not cloned:
        return ComponentState(
            name=name, cloned=False, installed=False, configured=False,
            enabled=enabled, active=False,
        )

    if deploy is None:
        # Component is cloned but doesn't ship a deploy.toml (radiod's
        # upstream ka9q-radio is the canonical example).  Detect active
        # via the lifecycle fallback shim, then trust reality: if it's
        # running, it must be installed AND configured.
        active, active_n, total_n = _active_via_lifecycle(name, alias)
        return ComponentState(
            name=name,
            cloned=True,
            installed=active,    # reality wins
            configured=active,   # reality wins
            enabled=enabled,
            active=active,
            active_unit_count=active_n,
            inactive_unit_count=max(0, total_n - active_n),
        )

    produces = _produces_paths(deploy)
    # When the deploy.toml doesn't declare [build].produces, we have no
    # filesystem fingerprint to verify against — trust the cloned state
    # (assume installed).  Components that *do* declare produces get the
    # real check.  This is the "older deploy.toml convention" path.
    installed = (not produces) or all(p.exists() for p in produces)

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
