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
    source: str                     # "deploy.toml:/opt/git/sigmond/psk-recorder/deploy.toml"
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

    conf_dir = systemd_config.get('conf_dir', '')

    # Process templated units: expand per instance
    for template in templated_units:
        kind = _unit_kind(template)
        expanded = _expand_template(component, template, source, kind,
                                    conf_dir=conf_dir)
        units.extend(expanded)

    return units


def _expand_template(
    component: str,
    template: str,
    source: str,
    kind: str,
    conf_dir: str = "",
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

    # Discover configured instances from a conf_dir (e.g. /etc/radio for radiod@)
    # Matches files like "radiod@<instance>.conf" and extracts <instance>.
    if conf_dir:
        base = template.split('@')[0]  # "radiod@.service" -> "radiod"
        conf_path = Path(conf_dir)
        if conf_path.exists():
            for cf in conf_path.glob(f"{base}@*.conf"):
                if cf.is_file() and not cf.is_symlink():
                    # "radiod@kfs-rx888-omni.conf" -> "kfs-rx888-omni"
                    instance_name = cf.stem[len(base) + 1:]
                    if instance_name:
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
    """Locate a component's deploy.toml.

    Wave 2 promoted this lookup into ``sigmond.discover``; this thin
    wrapper preserves the in-module symbol so existing tests that
    monkeypatch ``sigmond.lifecycle._find_deploy_toml`` keep working.
    """
    from .discover import find_deploy_toml
    return find_deploy_toml(component)


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

    conf_dir = systemd_config.get('conf_dir', '')

    for template in templated_units:
        kind = _unit_kind(template)
        expanded = _expand_template(component, template, source, kind,
                                    conf_dir=conf_dir)
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

# Baseline priorities sigmond can rely on when the catalog is unreachable
# or doesn't declare a value.  ``radiod`` has to start first regardless —
# every other client multicasts off it.  An explicit ``start_priority`` in
# any catalog entry (operator override or discovered deploy.toml) wins
# against the baseline.
_BASELINE_PRIORITIES: dict[str, int] = {
    'radiod':     0,
    'ka9q-radio': 0,
}
_DEFAULT_PRIORITY = 100


def _component_priorities() -> dict[str, int]:
    """Look up ``start_priority`` for every known component.

    For each catalog entry, an explicit ``start_priority`` wins; otherwise
    the baseline applies (radiod=0); otherwise the default (100).  Catalog
    failures degrade gracefully — ordering still produces a sensible
    result via the baseline.
    """
    priorities: dict[str, int] = dict(_BASELINE_PRIORITIES)
    try:
        from .catalog import load_catalog
        catalog = load_catalog()
    except Exception:
        return priorities

    for entry in catalog.values():
        if entry.start_priority is None:
            # Catalog didn't declare one — only the baseline (if it exists)
            # carries weight here.  Anything outside the baseline gets the
            # default at lookup time via dict.get(name, _DEFAULT_PRIORITY).
            continue
        priorities[entry.name] = entry.start_priority
        if entry.topology_alias:
            priorities[entry.topology_alias] = entry.start_priority
    return priorities


def order_units(
    units: list[UnitRef],
    coordination=None,
    priorities: Optional[dict[str, int]] = None,
) -> list[UnitRef]:
    """Order units for start, driven by per-component ``start_priority``.

    Each component's priority comes from the catalog (which Wave 2 sources
    primarily from each client's ``deploy.toml [client.lifecycle]
    start_priority`` field).  ``radiod`` ships priority 0 (always first);
    uploaders like ``wsprdaemon-client`` ship 900 (always last); everything
    else defaults to 100.

    Args:
        units: Flat list of resolved UnitRefs (already orphan-filtered).
        coordination: A ``Coordination`` object (from coordination.py).
            When two components share a priority, this provides a stable
            tiebreaker — declaration order from coordination.toml wins
            over alphabetical.
        priorities: Optional explicit priority map (component name → int).
            Tests pass this to avoid touching the on-disk catalog.

    Returns:
        New list with the same elements, reordered.
    """
    if not units:
        return []

    if priorities is None:
        priorities = _component_priorities()

    # Bucket units by component name.
    buckets: dict[str, list[UnitRef]] = {}
    for u in units:
        buckets.setdefault(u.component, []).append(u)

    # Tiebreaker order: components named in coordination.toml first
    # (declaration order), then alphabetical.
    coord_order: dict[str, int] = {}
    if coordination is not None and hasattr(coordination, 'clients'):
        for idx, ci in enumerate(coordination.clients):
            coord_order.setdefault(ci.client_type, idx)

    def _sort_key(name: str) -> tuple[int, int, str]:
        priority = priorities.get(name, _DEFAULT_PRIORITY)
        # Coordination-declared components win the secondary tier; they
        # get index 0..N. Anything else gets a sentinel after them.
        secondary = coord_order.get(name, len(coord_order) + 1)
        return (priority, secondary, name)

    ordered_names = sorted(buckets.keys(), key=_sort_key)

    result: list[UnitRef] = []
    for name in ordered_names:
        result.extend(buckets[name])
    return result
