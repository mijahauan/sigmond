"""Sigmond catalog of known HamSCI clients.

Answers "what clients could be installed on this host?" — independent of
topology (what IS enabled) and lifecycle (what units resolve to what).

Wave 2 architecture: the catalog has two sources.

* **Primary — discovery.** ``load_catalog()`` (no path) globs every
  ``/opt/git/*/deploy.toml`` and synthesizes a ``CatalogEntry`` from each
  client's own manifest.  This is how a drop-in client author gets sigmond
  to know about their client without editing any sigmond-side file.
* **Override — etc/catalog.toml.** Layered on top of discovery so operators
  can pin descriptions, repo URLs, or policy fields without editing a
  clone.  Pre-clone descriptions (the "what could I install?" question) also
  live here.

Tests pass an explicit ``path=`` to read a single file; the
``/opt/git`` glob only fires for the no-arg call.
"""

from __future__ import annotations

import os
import shutil
import tomllib
import warnings
from dataclasses import dataclass, field
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
    kind: str                                 # "client" | "server" | "infra" | "library" | "manager"
    description: str
    repo: str                                 # git URL
    uses: tuple[str, ...] = ()                # shared Python/library deps
    requires: tuple[str, ...] = ()            # component deps that must be enabled+installed
    contract: Optional[str] = None            # min contract version, None if N/A
    install_script: Optional[str] = None      # canonical installer path
    topology_alias: Optional[str] = None      # old topology name, e.g. "grape"
    # Lifecycle start priority (smaller = earlier).  None means "not declared
    # in the source TOML" — order_units then falls back to its baseline.
    # 0 = radiod (always first), 100 = default, 900 = uploaders (last).
    start_priority: Optional[int] = None

    def is_installed(self) -> bool:
        """Best-effort check that this entry is installed on the local host.

        Primary: repo cloned to /opt/git/<name> (production install path).
        Library kind: also importability from the current Python (the
                      authoritative signal for Python deps) and the dev
                      sibling locations ~/<name> and /opt/<name>.
        Fallback: install_script exists, or binary found in PATH.
        """
        # Use lexists rather than exists so a symlink at /opt/git/<name>
        # pointing at a target the current user can't traverse (e.g.
        # /opt/git/wsprdaemon-client -> /home/wsprdaemon/wsprdaemon-client,
        # where /home/wsprdaemon is mode 700) still registers as
        # installed. Path.exists() would raise PermissionError from stat()
        # and abort the whole `smd list --available` before the Infra
        # section ever prints.
        if os.path.lexists(str(Path('/opt/git') / self.name)):
            return True
        if self.kind == 'library':
            # Importability from the current Python wins: sigmond runs
            # inside the venv that clients will actually import from,
            # so if `find_spec` resolves, the dep is usable regardless
            # of where its source lives.
            import importlib.util
            import_name = self.name.removesuffix('-python').replace('-', '_')
            try:
                if importlib.util.find_spec(import_name) is not None:
                    return True
            except (ImportError, ValueError):
                pass
            # Dev siblings: `git clone` location before packaging.
            # Use os.path.lexists so test monkeypatches that stub out
            # filesystem presence cover this branch consistently with
            # the canonical /opt/git check above.
            home = os.path.expanduser('~')
            for candidate in (os.path.join(home, self.name),
                              os.path.join('/opt', self.name)):
                if os.path.lexists(candidate):
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


def _entry_from_toml_block(name: str, cfg: dict) -> CatalogEntry:
    """Build a CatalogEntry from a ``[client.<name>]`` TOML block."""
    raw_priority = cfg.get('start_priority')
    return CatalogEntry(
        name=name,
        kind=cfg.get('kind', 'client'),
        description=cfg.get('description', ''),
        repo=cfg.get('repo', ''),
        uses=tuple(cfg.get('uses', ())),
        requires=tuple(cfg.get('requires', ())),
        contract=cfg.get('contract') or None,
        install_script=cfg.get('install_script') or None,
        topology_alias=cfg.get('topology_alias') or None,
        start_priority=int(raw_priority) if raw_priority is not None else None,
    )


def _load_catalog_file(path: Path) -> dict[str, CatalogEntry]:
    with open(path, 'rb') as f:
        data = tomllib.load(f)
    entries: dict[str, CatalogEntry] = {}
    for name, cfg in (data.get('client') or {}).items():
        entries[name] = _entry_from_toml_block(name, cfg)
    return entries


def _synthesized_library_entries() -> dict[str, CatalogEntry]:
    """Library entries that aren't in catalog.toml but production code
    still needs to look up — primarily ka9q-python, which is pip-installed
    into the sigmond venv and not a topology component.
    """
    return {
        'ka9q-python': CatalogEntry(
            name='ka9q-python',
            kind='library',
            description='Python interface for ka9q-radio control and monitoring',
            repo='https://github.com/mijahauan/ka9q-python',
            uses=(),
            requires=(),
            contract=None,
            install_script=None,
        ),
    }


def load_catalog(path: Optional[Path] = None) -> dict[str, CatalogEntry]:
    """Load the catalog, keyed by client name.

    With an explicit ``path``: reads that single TOML file (single source).
    With no path: discovers entries from /opt/git/*/deploy.toml, then
    layers ``etc/catalog.toml`` on top as an operator override, and
    finally adds synthesized library entries (ka9q-python).

    Raises:
        FileNotFoundError: An explicit path was given but does not exist,
        or no catalog file is reachable in the default search locations.
    """
    if path is not None:
        if not path.exists():
            raise FileNotFoundError(
                f"sigmond catalog not found: {path}"
            )
        return _load_catalog_file(path)

    # Discovery primary.
    from .discover import discover_catalog_entries
    entries = discover_catalog_entries()

    # catalog.toml override (entries here win against discovery).
    catalog_file = find_catalog_file()
    if catalog_file is None:
        raise FileNotFoundError(
            "sigmond catalog not found in any of: "
            + ", ".join(str(p) for p in DEFAULT_CATALOG_PATHS)
        )
    entries.update(_load_catalog_file(catalog_file))

    # Synthesized library entries — only added if not already declared
    # by discovery or override.
    for name, entry in _synthesized_library_entries().items():
        entries.setdefault(name, entry)

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
    seen_lib_install: set[str] = set()

    for comp in sorted(enabled_set):
        entry = get_entry(comp, catalog)
        if entry is None:
            continue

        if not entry.is_installed():
            # Only suggest installation when there is something sigmond can
            # actually do: a repo to clone or an install_script to run.
            # Internally-managed infra has neither and is handled by smd
            # outside the normal catalog install path.
            if entry.repo or entry.install_script:
                items.append(('install', comp, f'sudo smd install {comp}'))

        for dep in transitive_requires(comp, catalog):
            key = (comp, dep)
            if key in seen_dep:
                continue
            seen_dep.add(key)

            dep_entry = catalog.get(dep)

            # Libraries (e.g. ka9q-python) are Python packages installed
            # into the sigmond venv; they don't belong in topology.toml.
            # Surface an install hint only if the current Python can't
            # import them; otherwise the dep is satisfied.
            if dep_entry is not None and dep_entry.kind == 'library':
                if (not dep_entry.is_installed()
                        and dep not in seen_lib_install):
                    seen_lib_install.add(dep)
                    items.append(('install', dep,
                                  f'sudo smd install {dep}'))
                continue

            if dep not in enabled_set:
                dep_desc = dep_entry.description if dep_entry else dep
                items.append((
                    'enable_dep',
                    f'{comp} requires {dep}',
                    f'enable {dep} in topology  ({dep_desc})',
                ))

    return items
