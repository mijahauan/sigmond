"""Read per-client `[client_features]` declarations from each repo's
``deploy.toml`` and surface them to the orchestrator (TUI dropdowns,
CLI helpers).

Why this exists
---------------
Before this module, screens like ``tui/screens/activity.py`` and
``tui/screens/verifier.py`` carried hand-maintained dicts mapping
watch verbs to client names, flags they accept, etc.  Adding a new
contract-conformant client meant editing those hardcodes *and* the
client repo — defeating the "drop-in" promise.

A client now declares its UI hooks in its own ``deploy.toml``:

    [client_features.watch]
    verb         = "hf-gps-tec"   # `smd watch <verb>` — default: client name
    description  = "PRN-beacon detection records (per freq/window)"
    verbose      = true           # CLI accepts `-v` / `--verbose`
    per_instance = true           # CLI accepts `--instance REPORTER_ID`

Omitting ``[client_features.watch]`` means the client has no watch
target; it stays out of the dropdown.

Lookup is best-effort and never raises: a missing repo, an unreadable
``deploy.toml``, a malformed block — each silently drops that client
from the result.  The screens that consume this fall back to the
hardcoded meta-watchers (``ka9q``, ``uploads``, ``verifier``) regardless.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # py<3.11
    import tomli as tomllib  # type: ignore[no-redef]

from .catalog import load_catalog
from .topology import load_topology


# Where installed client repos live.  Mirrors catalog.is_installed()'s
# primary check.  Tests can monkeypatch this to point at a tmpdir.
REPO_ROOT = Path("/opt/git/sigmond")


@dataclass(frozen=True)
class WatchFeature:
    """One client's `smd watch` surface, as declared in its deploy.toml."""

    client: str          # catalog name (e.g. "hf-gps-tec")
    verb: str            # `smd watch <verb>` — usually == client
    description: str
    verbose: bool        # CLI accepts -v / --verbose
    per_instance: bool   # CLI accepts --instance REPORTER_ID


def _read_deploy_toml(client: str, repo_root: Path) -> Optional[dict]:
    """Parse <repo_root>/<client>/deploy.toml; None on any failure."""
    p = repo_root / client / "deploy.toml"
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _parse_watch_feature(client: str, deploy: dict) -> Optional[WatchFeature]:
    """Extract a WatchFeature from a parsed deploy.toml dict, or None
    if the block is absent / malformed."""
    block = deploy.get("client_features", {}).get("watch")
    if not isinstance(block, dict):
        return None
    description = block.get("description")
    if not isinstance(description, str) or not description.strip():
        # description is required when the block is present — anything
        # else means the dropdown row would be unlabeled.
        return None
    return WatchFeature(
        client=client,
        verb=str(block.get("verb", client)),
        description=description,
        verbose=bool(block.get("verbose", False)),
        per_instance=bool(block.get("per_instance", False)),
    )


def load_watch_features(repo_root: Path = REPO_ROOT) -> list[WatchFeature]:
    """Return every enabled+installed client's WatchFeature, in topology
    order (matches the order the orchestrator already uses for listings).

    Filtering:
      - skips clients that aren't enabled in topology
      - skips clients whose catalog entry says is_installed() is False
      - skips clients whose deploy.toml is missing/unreadable/malformed
      - skips clients with no [client_features.watch] block

    The returned list is safe to render directly; callers add their own
    meta-watcher rows (ka9q / uploads / verifier) alongside it.
    """
    try:
        topo = load_topology()
        catalog = load_catalog()
    except OSError:
        return []

    out: list[WatchFeature] = []
    for client in topo.enabled_components():
        entry = catalog.get(client)
        if entry is None or not entry.is_installed():
            continue
        deploy = _read_deploy_toml(client, repo_root)
        if deploy is None:
            continue
        feature = _parse_watch_feature(client, deploy)
        if feature is not None:
            out.append(feature)
    return out


__all__ = ["WatchFeature", "load_watch_features"]
