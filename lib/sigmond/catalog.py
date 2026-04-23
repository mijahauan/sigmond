"""Sigmond static catalog of known HamSCI clients.

Answers "what clients could be installed on this host?" — independent of
topology (what IS enabled) and lifecycle (what units resolve to what).

The catalog is intentionally small and bounded.  Add a new entry to
etc/catalog.toml when a new client joins the suite.
"""

from __future__ import annotations

import shutil
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Operator override, then repo default.
_REPO_CATALOG = Path(__file__).resolve().parent.parent.parent / 'etc' / 'catalog.toml'
DEFAULT_CATALOG_PATHS: tuple[Path, ...] = (
    Path('/etc/sigmond/catalog.toml'),
    _REPO_CATALOG,
)


@dataclass(frozen=True)
class CatalogEntry:
    """A known client or server in the HamSCI suite."""
    name: str                                 # "psk-recorder"
    kind: str                                 # "client" | "server"
    description: str
    repo: str                                 # git URL
    uses: tuple[str, ...] = ()                # shared Python/library deps
    requires: tuple[str, ...] = ()            # component deps that must be enabled+installed
    contract: Optional[str] = None            # min contract version, None if N/A
    install_script: Optional[str] = None      # canonical installer path
    topology_alias: Optional[str] = None      # old topology name, e.g. "grape"

    def is_installed(self) -> bool:
        """Best-effort check that this entry is installed on the local host.

        Primary: repo cloned to /opt/git/<name> (installer always uses this path).
        Fallback: install_script exists, or binary found in PATH.
        """
        if (Path('/opt/git') / self.name).exists():
            return True
        if self.install_script:
            return Path(self.install_script).exists()
        return shutil.which(self.name) is not None


def find_client_binary(name: str) -> Optional[str]:
    """Locate a client's CLI binary.

    Search order:
    1. System PATH
    2. Pattern A venv: /opt/<name>/venv/bin/<name>
    """
    found = shutil.which(name)
    if found:
        return found
    venv_bin = Path(f'/opt/{name}/venv/bin/{name}')
    if venv_bin.exists():
        return str(venv_bin)
    return None


def find_catalog_file() -> Optional[Path]:
    """Locate the catalog file. Operator override beats repo default."""
    for p in DEFAULT_CATALOG_PATHS:
        if p.exists():
            return p
    return None


def load_catalog(path: Optional[Path] = None) -> dict[str, CatalogEntry]:
    """Load the catalog, keyed by client name.

    Args:
        path: Explicit path, or None to use the default search order.

    Raises:
        FileNotFoundError: No catalog file found at any search location.
    """
    if path is None:
        path = find_catalog_file()
    if path is None or not path.exists():
        raise FileNotFoundError(
            "sigmond catalog not found in any of: "
            + ", ".join(str(p) for p in DEFAULT_CATALOG_PATHS)
        )

    with open(path, 'rb') as f:
        data = tomllib.load(f)

    entries: dict[str, CatalogEntry] = {}
    for name, cfg in data.get('client', {}).items():
        entries[name] = CatalogEntry(
            name=name,
            kind=cfg.get('kind', 'client'),
            description=cfg.get('description', ''),
            repo=cfg.get('repo', ''),
            uses=tuple(cfg.get('uses', ())),
            requires=tuple(cfg.get('requires', ())),
            contract=cfg.get('contract') or None,
            install_script=cfg.get('install_script') or None,
            topology_alias=cfg.get('topology_alias') or None,
        )
    return entries


def build_alias_map(entries: dict[str, CatalogEntry]) -> dict[str, str]:
    """Build a map from topology aliases to canonical names."""
    aliases: dict[str, str] = {}
    for entry in entries.values():
        if entry.topology_alias:
            aliases[entry.topology_alias] = entry.name
    return aliases


def resolve_name(name: str, entries: dict[str, CatalogEntry]) -> str:
    """Resolve a name (canonical or alias) to the canonical catalog name.

    Emits a deprecation warning if an alias is used.
    Returns the input unchanged if it's not an alias.
    """
    if name in entries:
        return name
    aliases = build_alias_map(entries)
    if name in aliases:
        canonical = aliases[name]
        warnings.warn(
            f"component name '{name}' is deprecated; use '{canonical}'",
            DeprecationWarning,
            stacklevel=2,
        )
        return canonical
    return name


def get_entry(
    name: str, entries: dict[str, CatalogEntry]
) -> Optional[CatalogEntry]:
    """Look up a catalog entry by canonical name or topology alias."""
    canonical = resolve_name(name, entries)
    return entries.get(canonical)


def transitive_requires(
    name: str,
    catalog: dict[str, 'CatalogEntry'],
) -> list[str]:
    """Return all transitive component dependencies of *name* in install order.

    Uses depth-first traversal with cycle detection.  The returned list is
    ordered so that every dependency appears before the components that need it
    (i.e. safe install order), and *name* itself is excluded.
    """
    ordered: list[str] = []
    visited: set[str] = set()

    def _visit(comp: str) -> None:
        if comp in visited:
            return
        visited.add(comp)
        entry = get_entry(comp, catalog)
        if entry is None:
            return
        for dep in entry.requires:
            _visit(dep)
        if comp != name:
            ordered.append(comp)

    entry = get_entry(name, catalog)
    if entry:
        for dep in entry.requires:
            _visit(dep)

    return ordered


def next_steps(
    enabled_components: list[str],
    catalog: dict[str, 'CatalogEntry'],
) -> list[tuple[str, str, str]]:
    """Return actionable items for enabled components.

    Each item is a (kind, subject, action) tuple:
      kind     — 'install' | 'enable_dep'
      subject  — component name or dependency description
      action   — human-readable instruction

    Checks two things per enabled component:
    1. Is it installed on disk?  If not → suggest smd install.
    2. Are all transitive dependencies enabled?  If not → suggest enabling.
       Missing deps of missing deps are surfaced immediately, not iteratively.
    """
    enabled_set = set(enabled_components)
    items: list[tuple[str, str, str]] = []
    seen_dep: set[tuple[str, str]] = set()

    for comp in sorted(enabled_set):
        entry = get_entry(comp, catalog)
        if entry is None:
            continue

        if not entry.is_installed():
            items.append(('install', comp, f'sudo smd install {comp}'))

        for dep in transitive_requires(comp, catalog):
            key = (comp, dep)
            if key in seen_dep:
                continue
            seen_dep.add(key)
            if dep not in enabled_set:
                dep_entry = catalog.get(dep)
                dep_desc = dep_entry.description if dep_entry else dep
                items.append((
                    'enable_dep',
                    f'{comp} requires {dep}',
                    f'enable {dep} in topology  ({dep_desc})',
                ))

    return items
