"""Sigmond catalog of known HamSCI clients.

Answers "what clients could be installed on this host?" — independent of
topology (what IS enabled) and lifecycle (what units resolve to what).

Wave 2 architecture: the catalog has three layers, applied in order of
increasing precedence.  Each layer is a *sparse overlay* — fields
present in a higher layer override the same field from a lower layer;
fields absent in a higher layer fall through unchanged.

1. **Discovery (lowest).** ``load_catalog()`` (no path) globs every
   ``/opt/git/sigmond/*/deploy.toml`` and synthesizes a base entry from
   each client's own manifest.  Drop-in clients work with zero sigmond-
   side edits.
2. **Repo default — ``etc/catalog.toml`` shipped with sigmond.** Adds
   entries that can't be discovered (e.g. ``ka9q-radio`` which has no
   ``/opt/git/sigmond/`` checkout) and overrides specific fields.
3. **Operator override — ``/etc/sigmond/catalog.toml`` (highest).** Per-
   host pins.  New entries in the repo file propagate automatically;
   they no longer require copying the file into ``/etc``.

The shift from "first-file-wins, whole-entry-replacement" to layered
sparse overlay was driven by silent drift: an operator file that
predated a repo-side entry would shadow the whole catalog, hiding new
clients (and source-only deps like callhash / hs-uploader) until
manually re-synced.

Tests pass an explicit ``path=`` to read a single file as-is; the
discovery glob and overlay logic only fire for the no-arg call.
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
class DeprecatedEntry:
    """A client that used to ship with sigmond but no longer does.

    Listed via ``[deprecated.<name>]`` blocks in catalog files.  Names
    that appear here are *excluded* from the live catalog returned by
    ``load_catalog()`` so a stale ``/opt/git/sigmond/<name>/deploy.toml``
    cannot revive a removed client by discovery.  ``smd list`` reports
    deprecated entries separately when their repo dir still exists on
    disk so the operator can run ``smd remove <name> --purge``.
    """
    name: str
    removed_in: str = ''            # commit ref / version
    reason: str = ''
    replaced_by: tuple[str, ...] = ()
    # Absolute paths to also rm -rf during ``smd remove``.  The default
    # plan removes /opt/git/sigmond/<name>, /opt/<name>, and /etc/<name>;
    # use this when the legacy install used differently-named dirs
    # (e.g. wsprdaemon-client laid its config at /etc/wsprdaemon, not
    # /etc/wsprdaemon-client).
    extra_paths: tuple[str, ...] = ()


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
    # Human label for the host-specific hardware this component needs to run
    # (e.g. "magnetometer (RM3100 / Pololu USB-I2C)").  Set => the component is
    # hardware-gated: sigmond reads its `inventory --json hardware_present` and,
    # when the hardware is absent, marks it core-but-dormant (Phase D).  None =>
    # not hardware-gated.  Declared here so the gated set lives in config, not
    # code (harmonize._hardware_gated_registry).
    hardware_gated: Optional[str] = None

    def is_installed(self) -> bool:
        """Best-effort check that this entry is installed on the local host.

        Primary: repo cloned to /opt/git/sigmond/<name> (production install path).
        Library kind: also importability from the current Python (the
                      authoritative signal for Python deps) and the dev
                      sibling locations ~/<name> and /opt/<name>.
        Fallback: install_script exists, or binary found in PATH.
        """
        # Use lexists rather than exists so a symlink at /opt/git/sigmond/<name>
        # pointing at a target the current user can't traverse (e.g.
        # /opt/git/sigmond/wsprdaemon-client -> /home/wsprdaemon/wsprdaemon-client,
        # where /home/wsprdaemon is mode 700) still registers as
        # installed. Path.exists() would raise PermissionError from stat()
        # and abort the whole `smd list --available` before the Infra
        # section ever prints.
        if os.path.lexists(str(Path('/opt/git/sigmond') / self.name)):
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
            except (ImportError, ValueError, PermissionError, OSError):
                pass
            # Dev siblings: `git clone` location before packaging.
            # Use os.path.lexists so test monkeypatches that stub out
            # filesystem presence cover this branch consistently with
            # the canonical /opt/git/sigmond check above.
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
    """Locate the highest-precedence existing catalog file.

    Kept for backward compatibility with callers that want a single path
    (e.g. for display).  ``load_catalog()`` itself now layers every
    existing file rather than reading just one.
    """
    for p in DEFAULT_CATALOG_PATHS:
        if p.exists():
            return p
    return None


def _layer_paths_in_application_order() -> list[Path]:
    """Return every existing catalog file in lowest→highest precedence
    order (repo file first, operator override last)."""
    # DEFAULT_CATALOG_PATHS is in highest→lowest precedence; reverse for
    # application order so each subsequent layer overrides the previous.
    return [p for p in reversed(DEFAULT_CATALOG_PATHS) if p.exists()]


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
        hardware_gated=cfg.get('hardware_gated') or None,
    )


def _entry_to_block(entry: CatalogEntry) -> dict:
    """Inverse of ``_entry_from_toml_block`` — convert a CatalogEntry
    back to the same dict shape that a TOML block produces.  Used to
    feed discovery-synthesized entries through the same per-field
    overlay code path as file-loaded entries."""
    block: dict = {
        'kind': entry.kind,
        'description': entry.description,
        'repo': entry.repo,
        'uses': list(entry.uses),
        'requires': list(entry.requires),
    }
    if entry.contract is not None:
        block['contract'] = entry.contract
    if entry.install_script is not None:
        block['install_script'] = entry.install_script
    if entry.topology_alias is not None:
        block['topology_alias'] = entry.topology_alias
    if entry.start_priority is not None:
        block['start_priority'] = entry.start_priority
    if entry.hardware_gated is not None:
        block['hardware_gated'] = entry.hardware_gated
    return block


def _load_raw_blocks(path: Path) -> dict[str, dict]:
    """Read a catalog file and return its ``[client.<name>]`` blocks as
    raw dicts (no CatalogEntry conversion).  Returning the raw blocks
    is what makes per-field overlay across layers possible — keys
    absent from the block stay absent so the lower layer's value
    shows through."""
    with open(path, 'rb') as f:
        data = tomllib.load(f)
    return dict((data.get('client') or {}).items())


def _load_deprecated_blocks(path: Path) -> dict[str, dict]:
    """Read a catalog file and return its ``[deprecated.<name>]`` blocks
    as raw dicts.  Same sparse-overlay shape as ``_load_raw_blocks``."""
    with open(path, 'rb') as f:
        data = tomllib.load(f)
    return dict((data.get('deprecated') or {}).items())


def load_deprecated() -> dict[str, DeprecatedEntry]:
    """Sparse-overlay every layer's ``[deprecated.*]`` blocks and return
    the result.  Operators can add host-specific deprecations in
    ``/etc/sigmond/catalog.toml`` the same way they can override client
    fields — partial blocks add to the repo-default list rather than
    replacing it."""
    merged: dict[str, dict] = {}
    for layer_path in _layer_paths_in_application_order():
        for name, block in _load_deprecated_blocks(layer_path).items():
            existing = merged.get(name) or {}
            merged[name] = {**existing, **block}
    return {
        name: DeprecatedEntry(
            name=name,
            removed_in=block.get('removed_in', ''),
            reason=block.get('reason', ''),
            replaced_by=tuple(block.get('replaced_by', ())),
            extra_paths=tuple(block.get('extra_paths', ())),
        )
        for name, block in merged.items()
    }


@dataclass(frozen=True)
class Profile:
    """A named station bundle from a ``[profile.<name>]`` catalog block.

    Groups the clients + local-radiod infra that make up a station role so
    ``smd install --profile`` (and the TUI one-shot install) can install the
    set together.  Pure-python libraries are implicit — auto-pulled as client
    siblings.  ``local_radiod_infra`` applies only when radiod is local.
    """
    name: str
    description: str = ''
    clients: tuple = ()
    local_radiod_infra: tuple = ()
    optional: tuple = ()


def _load_profile_blocks(path: Path) -> dict[str, dict]:
    """Read a catalog file's ``[profile.<name>]`` blocks as raw dicts."""
    with open(path, 'rb') as f:
        data = tomllib.load(f)
    return dict((data.get('profile') or {}).items())


def load_profiles() -> dict[str, Profile]:
    """Sparse-overlay every layer's ``[profile.*]`` blocks (same shape as
    ``load_deprecated``) and return them.  Operators add or extend profiles
    in ``/etc/sigmond/catalog.toml`` the same way they override client
    fields."""
    merged: dict[str, dict] = {}
    for layer_path in _layer_paths_in_application_order():
        for name, block in _load_profile_blocks(layer_path).items():
            existing = merged.get(name) or {}
            merged[name] = {**existing, **block}
    return {
        name: Profile(
            name=name,
            description=block.get('description', ''),
            clients=tuple(block.get('clients', ())),
            local_radiod_infra=tuple(block.get('local_radiod_infra', ())),
            optional=tuple(block.get('optional', ())),
        )
        for name, block in merged.items()
    }


def _load_catalog_file(path: Path) -> dict[str, CatalogEntry]:
    """Single-file load (used by the explicit-path branch of
    ``load_catalog`` and by tests).  No layering."""
    return {
        name: _entry_from_toml_block(name, block)
        for name, block in _load_raw_blocks(path).items()
    }


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

    With an explicit ``path``: reads that single TOML file as-is, no
    layering (preserves the single-source semantics tests rely on).

    With no path: builds the merged catalog by sparse per-field overlay
    across three layers, lowest precedence first:

        1. Discovery — synthesized from each
           ``/opt/git/sigmond/<name>/deploy.toml``.
        2. ``etc/catalog.toml`` shipped with sigmond.
        3. ``/etc/sigmond/catalog.toml`` operator override.

    For each layer, only the keys present in the block override the
    same keys from earlier layers — missing keys fall through.  New
    entries at any layer are added; entries at higher layers always
    win on the fields they declare.

    Synthesized library entries (e.g. ka9q-python) are added at the
    end if no layer declared them.

    Raises:
        FileNotFoundError: An explicit path was given but does not
        exist, or no catalog file is reachable in the default search
        locations.
    """
    if path is not None:
        if not path.exists():
            raise FileNotFoundError(
                f"sigmond catalog not found: {path}"
            )
        return _load_catalog_file(path)

    # Layer 1 (lowest): discovery.  Convert to raw-block form so the
    # overlay logic below treats every layer uniformly.
    from .discover import discover_catalog_entries
    merged: dict[str, dict] = {
        name: _entry_to_block(entry)
        for name, entry in discover_catalog_entries().items()
    }

    # Layers 2+: each catalog file in application order (lowest→highest
    # precedence).  Per-key dict merge so a sparse operator block
    # overrides only the fields it sets.
    layer_paths = _layer_paths_in_application_order()
    if not layer_paths and not merged:
        raise FileNotFoundError(
            "sigmond catalog not found in any of: "
            + ", ".join(str(p) for p in DEFAULT_CATALOG_PATHS)
        )
    for layer_path in layer_paths:
        for name, block in _load_raw_blocks(layer_path).items():
            existing = merged.get(name) or {}
            merged[name] = {**existing, **block}

    # Drop anything the deprecation list declares.  A stale deploy.toml
    # on disk (e.g. /opt/git/sigmond/wsprdaemon-client) is no longer
    # silently revived through discovery — ``smd remove --purge`` is
    # the operator-facing escape hatch instead.
    deprecated_names = set(load_deprecated().keys())
    entries: dict[str, CatalogEntry] = {
        name: _entry_from_toml_block(name, block)
        for name, block in merged.items()
        if name not in deprecated_names
    }

    # Synthesized library entries — only added if not already declared
    # by discovery or any layer, and not deprecated.
    for name, entry in _synthesized_library_entries().items():
        if name in deprecated_names:
            continue
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
                items.append(('install', comp, f'smd install {comp}'))

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
                                  f'smd install {dep}'))
                continue

            if dep not in enabled_set:
                dep_desc = dep_entry.description if dep_entry else dep
                items.append((
                    'enable_dep',
                    f'{comp} requires {dep}',
                    f'enable {dep} in topology  ({dep_desc})',
                ))

    return items
