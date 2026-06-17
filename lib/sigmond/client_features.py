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
    verb         = "hf-tec"   # `smd watch <verb>` — default: client name
    description  = "PRN-beacon detection records (per freq/window)"
    verbose      = true           # CLI accepts `-v` / `--verbose`
    per_instance = true           # CLI accepts `--instance REPORTER_ID`

    [client_features.verifier]
    verb         = "wspr"         # `smd admin verifier report --target <verb>`
    description  = "WSPRnet upload audit (lost / in-flight / delivered)"
    kind         = "spot_queue"   # "spot_queue" (--rx-call / --lost / --in-flight
                                  # / --delivered / --cadence flags apply) or
                                  # "local_db" (audits a per-client product DB,
                                  # flags don't apply — e.g. hf-timestd)
    per_instance = true           # adds the instance dropdown row

    [client_features.receiver_channels]
    description     = "FT4/FT8 spot channels"
    per_instance    = true                              # per-instance vs singleton
    parser_file     = "src/psk_recorder/sigmond_tui.py" # path relative to repo root
    parser_attr     = "parse_receiver_channels"         # callable in that file
    # Singleton-only (per_instance = false) extras:
    singleton_label = "(singleton)"                     # suffix on dropdown label
    config_path     = "/etc/hf-timestd/timestd-config.toml"  # absolute config path

Omitting a block means the client doesn't appear in that screen.

Lookup is best-effort and never raises: a missing repo, an unreadable
``deploy.toml``, a malformed block — each silently drops that client
from the result.  The TUI screens that consume this fall back to their
hardcoded meta rows regardless.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

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

    client: str          # catalog name (e.g. "hf-tec")
    verb: str            # `smd watch <verb>` — usually == client
    description: str
    verbose: bool        # CLI accepts -v / --verbose
    per_instance: bool   # CLI accepts --instance REPORTER_ID


# Recognised values for `[client_features.verifier].kind`.  spot_queue
# is the wspr/psk shape (audit a remote upload queue with --rx-call /
# --lost / --in-flight / --delivered / --cadence flags); local_db is
# the hf-timestd shape (audit a per-client product DB on disk; flags
# don't apply).  Anything else → rejected at parse time.
VERIFIER_KINDS = frozenset({"spot_queue", "local_db"})


@dataclass(frozen=True)
class VerifierFeature:
    """One client's `smd admin verifier report` surface, as declared in its
    deploy.toml."""

    client: str          # catalog name (e.g. "wspr-recorder")
    verb: str            # `smd admin verifier report --target <verb>`
    description: str
    kind: str            # one of VERIFIER_KINDS
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


@dataclass(frozen=True)
class ReceiverChannelsFeature:
    """One client's Receiver Channels TUI surface, as declared in its
    deploy.toml.  The parser is a function pointer into client-owned
    code; sigmond never knows the client's config schema directly."""

    client: str               # catalog name (e.g. "psk-recorder")
    description: str
    per_instance: bool        # True → instance dropdown; False → singleton entry
    parser_file: str          # path relative to repo root (e.g. "src/foo/sigmond_tui.py")
    parser_attr: str          # callable name in that file
    singleton_label: str      # suffix on dropdown label (only used when per_instance=False)
    config_path: str          # absolute config path (only used when per_instance=False)


def _parse_receiver_channels_feature(
    client: str, deploy: dict,
) -> Optional[ReceiverChannelsFeature]:
    """Extract a ReceiverChannelsFeature from a parsed deploy.toml
    dict, or None if the block is absent / malformed.

    Required keys: description, per_instance, parser_file, parser_attr.
    Singleton clients additionally need config_path.  parser_file is
    validated for existence here so a typo surfaces in
    `smd admin diag drop-in` instead of as a TUI-time KeyError.
    """
    block = deploy.get("client_features", {}).get("receiver_channels")
    if not isinstance(block, dict):
        return None
    description = block.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    parser_file = block.get("parser_file")
    parser_attr = block.get("parser_attr")
    if not isinstance(parser_file, str) or not parser_file.strip():
        return None
    if not isinstance(parser_attr, str) or not parser_attr.strip():
        return None
    per_instance = bool(block.get("per_instance", True))
    singleton_label = str(block.get("singleton_label", "") or "")
    config_path = str(block.get("config_path", "") or "")
    if not per_instance and not config_path:
        # Singleton clients must point at an absolute config file —
        # there's no /etc/<client>/<instance>.toml fallback for them.
        return None
    return ReceiverChannelsFeature(
        client=client,
        description=description,
        per_instance=per_instance,
        parser_file=parser_file,
        parser_attr=parser_attr,
        singleton_label=singleton_label,
        config_path=config_path,
    )


def _parse_verifier_feature(client: str, deploy: dict) -> Optional[VerifierFeature]:
    """Extract a VerifierFeature from a parsed deploy.toml dict, or None
    if the block is absent / malformed."""
    block = deploy.get("client_features", {}).get("verifier")
    if not isinstance(block, dict):
        return None
    description = block.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    kind = block.get("kind")
    if kind not in VERIFIER_KINDS:
        # Unknown kind → silently skip rather than expose half-wired
        # behavior in the dropdown.
        return None
    return VerifierFeature(
        client=client,
        verb=str(block.get("verb", client)),
        description=description,
        kind=str(kind),
        per_instance=bool(block.get("per_instance", False)),
    )


def _walk_enabled_installed(repo_root: Path):
    """Yield (client_name, parsed_deploy_dict) for every enabled+installed
    client whose deploy.toml parses.  Shared by the per-feature loaders."""
    try:
        topo = load_topology()
        catalog = load_catalog()
    except OSError:
        return
    for client in topo.enabled_components():
        entry = catalog.get(client)
        if entry is None or not entry.is_installed():
            continue
        deploy = _read_deploy_toml(client, repo_root)
        if deploy is None:
            continue
        yield client, deploy


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
    out: list[WatchFeature] = []
    for client, deploy in _walk_enabled_installed(repo_root):
        feature = _parse_watch_feature(client, deploy)
        if feature is not None:
            out.append(feature)
    return out


def load_verifier_features(repo_root: Path = REPO_ROOT) -> list[VerifierFeature]:
    """Return every enabled+installed client's VerifierFeature, in
    topology order.  Same filtering rules as load_watch_features; the
    verifier screen has no meta-rows so the loader output IS the
    dropdown content."""
    out: list[VerifierFeature] = []
    for client, deploy in _walk_enabled_installed(repo_root):
        feature = _parse_verifier_feature(client, deploy)
        if feature is not None:
            out.append(feature)
    return out


def load_receiver_channels_features(
    repo_root: Path = REPO_ROOT,
) -> list[ReceiverChannelsFeature]:
    """Return every enabled+installed client's ReceiverChannelsFeature,
    in topology order.  Same filtering rules as load_watch_features.
    The Receiver Channels TUI screen uses this list as the *complete*
    set of supported clients — no hardcoded allowlist anywhere."""
    out: list[ReceiverChannelsFeature] = []
    for client, deploy in _walk_enabled_installed(repo_root):
        feature = _parse_receiver_channels_feature(client, deploy)
        if feature is not None:
            out.append(feature)
    return out


# Per-(repo_root, parser_file) cache of loaded parsers.  Refreshing the
# Receiver Channels screen is the hot path; avoid re-parsing the same
# parser module on every click.
_PARSER_CACHE: dict[tuple, Callable[[dict], Any]] = {}


def load_receiver_channels_parser(
    feature: ReceiverChannelsFeature,
    repo_root: Path = REPO_ROOT,
) -> Optional[Callable[[dict], Any]]:
    """Import the parser declared in ``feature`` and return the callable.

    Uses ``importlib.util.spec_from_file_location`` so the client's
    parser doesn't need to be importable by package name from sigmond's
    venv — only the path on disk needs to resolve.  Cached per
    (repo_root, parser_file).

    Returns None on any failure: missing file, import error, missing
    attribute, attribute not callable.  Callers degrade gracefully —
    the TUI shows an error row, never crashes.
    """
    cache_key = (str(repo_root), feature.client, feature.parser_file, feature.parser_attr)
    cached = _PARSER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    parser_path = repo_root / feature.client / feature.parser_file
    if not parser_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        # Unique module name per client so two clients with the same
        # filename don't collide in sys.modules.
        f"_sigmond_receiver_channels_parser_{feature.client}".replace("-", "_"),
        parser_path,
    )
    if spec is None or spec.loader is None:
        return None
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:                                    # noqa: BLE001
        return None
    fn = getattr(module, feature.parser_attr, None)
    if not callable(fn):
        return None
    _PARSER_CACHE[cache_key] = fn
    return fn


__all__ = [
    "WatchFeature", "load_watch_features",
    "VerifierFeature", "VERIFIER_KINDS", "load_verifier_features",
    "ReceiverChannelsFeature", "load_receiver_channels_features",
    "load_receiver_channels_parser",
]
