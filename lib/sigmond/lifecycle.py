"""Sigmond lifecycle management — systemd unit resolution and control.

Implements §5 of CONTRACT-v0.5-DRAFT.md: unit enumeration from deploy.toml,
templated-unit instance discovery, and lifecycle verb scope.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .paths import LIFECYCLE_LOCK


@dataclass(frozen=True)
class UnitRef:
    """Reference to a systemd unit managed by sigmond lifecycle."""
    component: str                  # "psk-recorder"
    unit: str                       # "psk-recorder@default.service"
    template: Optional[str]         # "psk-recorder@.service" or None (concrete units)
    instance: Optional[str]         # "default" or None (for concrete units)
    kind: str                       # "service" | "target" | "timer"
    source: str                     # "deploy.toml:/opt/git/psk-recorder/deploy.toml"
                                    # or "fallback:hardcoded"
    orphaned: bool = False          # in known but not configured


def resolve_units(
    components: list[str],
    enabled_components: list[str],
    topology_toml_path: Optional[Path] = None,
) -> list[UnitRef]:
    """Resolve lifecycle-managed units for the given components.

    Args:
        components: Component names to resolve (e.g. ['psk-recorder', 'hf-timestd']).
        enabled_components: The full list of enabled components from topology.
                            Used to validate that requested components exist.
        topology_toml_path: Optional path to topology.toml for context (unused in v0.5
                            but kept for extensibility).

    Returns:
        A flat list of UnitRef, one per resolved unit (expanded for templated instances).
        Orphaned instances are included with orphaned=True.

    Raises:
        ValueError: If a component does not exist in enabled_components and is not
                    known to have a fallback shim.
    """
    units: list[UnitRef] = []
    seen_components = set()

    for comp in components:
        if comp not in enabled_components and not _has_fallback_shim(comp):
            raise ValueError(
                f"component '{comp}' not found in enabled components and no fallback shim exists"
            )
        seen_components.add(comp)
        units.extend(_resolve_component_units(comp))

    return units


def _resolve_component_units(component: str) -> list[UnitRef]:
    """Resolve all lifecycle-managed units for a single component."""
    units: list[UnitRef] = []

    # Attempt to load the component's deploy.toml
    deploy_toml = _find_deploy_toml(component)
    if deploy_toml is None:
        # Fall back to hardcoded shim
        return _load_fallback_shim(component)

    try:
        with open(deploy_toml, 'rb') as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        warnings.warn(f"failed to read {deploy_toml}: {exc}; using fallback", stacklevel=2)
        return _load_fallback_shim(component)

    systemd_config = config.get('systemd', {})
    concrete_units = systemd_config.get('units', [])
    templated_units = systemd_config.get('templated_units', [])

    # Normalize templated names found in 'units' → 'templated_units' with warning
    deprecated_templates = [u for u in concrete_units if '@' in u]
    if deprecated_templates:
        warnings.warn(
            f"{component}: templated unit names in 'units' key is deprecated; "
            f"move {deprecated_templates} to 'templated_units'",
            DeprecationWarning,
            stacklevel=2,
        )
        templated_units = list(set(templated_units) | set(deprecated_templates))
        concrete_units = [u for u in concrete_units if '@' not in u]

    source = f"deploy.toml:{deploy_toml}"

    # Process concrete units
    for unit in concrete_units:
        kind = _unit_kind(unit)
        units.append(
            UnitRef(
                component=component,
                unit=unit,
                template=None,
                instance=None,
                kind=kind,
                source=source,
            )
        )

    # Process templated units: expand per instance
    for template in templated_units:
        kind = _unit_kind(template)
        expanded = _expand_template(component, template, source, kind)
        units.extend(expanded)

    return units


def _expand_template(
    component: str,
    template: str,
    source: str,
    kind: str,
) -> list[UnitRef]:
    """Expand a templated unit (e.g., 'psk-recorder@.service') into per-instance UnitRefs.

    Returns:
        List of UnitRef for configured instances, with orphaned instances marked.
    """
    units: list[UnitRef] = []

    # Discover configured instances from env files
    env_dir = Path(f"/etc/{component}/env")
    configured = set()
    if env_dir.exists():
        for env_file in env_dir.glob("*.env"):
            instance_name = env_file.stem
            configured.add(instance_name)

    # Discover known instances from systemctl
    known = set(configured)
    try:
        result = subprocess.run(
            ["systemctl", "list-units", template.replace('@.', '@*.'), "--all", "--output=json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            units_json = json.loads(result.stdout) if result.stdout.strip() else []
            for unit_info in units_json:
                unit_name = unit_info.get('unit') or unit_info.get('name', '')
                # Extract instance from unit name, e.g., 'psk-recorder@default.service' → 'default'
                if '@' in unit_name and unit_name.endswith(f".{kind}"):
                    instance_name = unit_name.split('@')[1].rsplit('.', 1)[0]
                    known.add(instance_name)
    except (json.JSONDecodeError, subprocess.SubprocessError):
        pass

    # Create UnitRef for each known instance
    for instance in sorted(known):
        unit_name = template.replace('@.', f"@{instance}.")
        # Only orphan if sigmond owns instance config (env files exist) but
        # this instance isn't among them. If no env files exist the client
        # manages its own instances and nothing is orphaned.
        orphaned = bool(configured) and instance not in configured
        units.append(
            UnitRef(
                component=component,
                unit=unit_name,
                template=template,
                instance=instance,
                kind=kind,
                source=source,
                orphaned=orphaned,
            )
        )

    return units


def _find_deploy_toml(component: str) -> Optional[Path]:
    """Find the deploy.toml for a component.

    Search order:
    1. Via `<component> inventory --json` → deploy_toml_path field (v0.5)
    2. Pattern A canonical: /opt/git/<component>/deploy.toml
    3. None (use fallback)
    """
    # Try via inventory first
    try:
        binary = shutil.which(component)
        if binary:
            result = subprocess.run(
                [binary, "inventory", "--json"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if deploy_path := data.get('deploy_toml_path'):
                    p = Path(deploy_path)
                    if p.exists():
                        return p
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError):
        pass

    # Try canonical location. Catch PermissionError so a component
    # whose /opt/git/<name>/ is behind a restrictive permission mask
    # (e.g. wsprdaemon-client -> /home/wsprdaemon/... at mode 700)
    # doesn't abort the whole `smd list` for every other component.
    # Returning None falls through to the shim fallback, which is the
    # right answer when we can't read the install tree ourselves.
    canonical = Path(f"/opt/git/{component}/deploy.toml")
    try:
        if canonical.exists():
            return canonical
    except PermissionError:
        pass

    return None


def _has_fallback_shim(component: str) -> bool:
    """Check if a fallback shim exists for a non-contract component."""
    shim = Path(f"/etc/sigmond/clients/{component}.deploy.toml")
    return shim.exists()


def _load_fallback_shim(component: str) -> list[UnitRef]:
    """Load the fallback shim deploy.toml for non-contract components."""
    shim_path = Path(f"/etc/sigmond/clients/{component}.deploy.toml")
    if not shim_path.exists():
        return []

    try:
        with open(shim_path, 'rb') as f:
            config = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        warnings.warn(f"failed to read fallback shim {shim_path}: {exc}", stacklevel=2)
        return []

    units: list[UnitRef] = []
    systemd_config = config.get('systemd', {})
    concrete_units = systemd_config.get('units', [])
    templated_units = systemd_config.get('templated_units', [])

    source = f"fallback:{shim_path}"

    for unit in concrete_units:
        kind = _unit_kind(unit)
        units.append(
            UnitRef(
                component=component,
                unit=unit,
                template=None,
                instance=None,
                kind=kind,
                source=source,
            )
        )

    for template in templated_units:
        kind = _unit_kind(template)
        expanded = _expand_template(component, template, source, kind)
        units.extend(expanded)

    return units


def _unit_kind(unit_name: str) -> str:
    """Extract unit kind from unit name (e.g., 'foo.service' → 'service')."""
    if '.service' in unit_name:
        return 'service'
    elif '.timer' in unit_name:
        return 'timer'
    elif '.target' in unit_name:
        return 'target'
    else:
        return 'unknown'


# ---------------------------------------------------------------------------
# Lifecycle lock (CONTRACT §5.5)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def lifecycle_lock(reason: str = ""):
    """Acquire an exclusive flock on the lifecycle lock file.

    Every mutating verb (install, apply, start, stop, restart, reload,
    update) must hold this lock.  Read-only verbs (list, status) are
    lock-free.

    Uses LOCK_NB so a second ``smd`` fails immediately rather than
    queueing behind the first.
    """
    lock_path = LIFECYCLE_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit(
            f"smd: another lifecycle operation is in progress "
            f"(lock held on {lock_path})"
        )
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# Start ordering (CONTRACT §5.4)
# ---------------------------------------------------------------------------

def order_units(
    units: list[UnitRef],
    coordination=None,
) -> list[UnitRef]:
    """Order units for start: radiod first (if present), then clients in
    coordination.toml declaration order.

    Args:
        units: Flat list of resolved UnitRefs (already orphan-filtered).
        coordination: A ``Coordination`` object (from coordination.py).
            If provided, client ordering follows the ``clients`` list.
            If None, non-radiod components sort alphabetically.

    Returns:
        New list with the same elements, reordered.
    """
    # Bucket units by component name.
    buckets: dict[str, list[UnitRef]] = {}
    for u in units:
        buckets.setdefault(u.component, []).append(u)

    # Build the component ordering.
    ordered_names: list[str] = []

    # ka9q-radio always first (if present).
    if 'ka9q-radio' in buckets:
        ordered_names.append('ka9q-radio')

    # Clients in coordination.toml declaration order.
    if coordination is not None and hasattr(coordination, 'clients'):
        seen = set(ordered_names)
        for ci in coordination.clients:
            name = ci.client_type
            if name not in seen and name in buckets:
                ordered_names.append(name)
                seen.add(name)

    # Anything remaining (not in coordination) goes last, alphabetically.
    remaining = sorted(n for n in buckets if n not in set(ordered_names))
    ordered_names.extend(remaining)

    # Flatten buckets in order.
    result: list[UnitRef] = []
    for name in ordered_names:
        result.extend(buckets[name])
    return result
