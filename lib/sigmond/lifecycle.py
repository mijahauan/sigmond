"""Sigmond lifecycle management — systemd unit resolution and control.

Implements §5 of CONTRACT-v0.5-DRAFT.md: unit enumeration from deploy.toml,
templated-unit instance discovery, and lifecycle verb scope.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


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
            ["systemctl", "list-units", f"{template}@*.{kind}", "--all", "--output=json"],
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
        orphaned = instance not in configured
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

    # Try canonical location
    canonical = Path(f"/opt/git/{component}/deploy.toml")
    if canonical.exists():
        return canonical

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
