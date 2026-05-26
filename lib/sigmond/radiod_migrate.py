"""Per-host radiod-identification migration (Phase 5).

Walks each enabled client's config under /etc/<client>/ + the host's
coordination.toml and rewrites legacy radiod-identifier fields to the
new canonical `status` field per docs/RADIOD-IDENTIFICATION.md §3.1.

Schemas handled:
  psk-recorder, hfdl-recorder  — [[radiod]] id + radiod_status   → status
  codar-sounder                — [[radiod]] id + status_dns      → status
  wspr-recorder                — [radiod]  status_address        → status
  hf-timestd                   — [ka9q]    status_address        → status
  coordination.toml            — [radiod."<old-label>"] renamed to
                                 [radiod."<multicast>"] when the block
                                 has a status_dns field that doesn't
                                 match the key.

Line-based rewriting (regex per legacy field) preserves comments and
formatting.  Idempotent: configs already using `status` skip cleanly.

Read-only by default (dry-run); pass `--yes` (in the CLI verb) to
actually rewrite files.  Per-client restarts are NOT performed here —
operators bounce daemons themselves after reviewing the diff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Per-client schema variants.
#   psk/hfdl: [[radiod]] id + radiod_status
#   codar:    [[radiod]] id + status_dns
#   wspr:     [radiod]  status_address     (singleton, not array)
#   hf-timestd: [ka9q]  status_address     (under [ka9q], not [radiod])
_SCHEMA_BY_CLIENT = {
    "psk-recorder":  ("array",     "radiod_status"),
    "hfdl-recorder": ("array",     "radiod_status"),
    "codar-sounder": ("array",     "status_dns"),
    "wspr-recorder": ("singleton", "status_address"),
    "hf-timestd":    ("ka9q",      "status_address"),
}


@dataclass(frozen=True)
class Candidate:
    """A config file that has at least one legacy radiod field to migrate."""
    config_path: Path
    client: str
    legacy_field: str       # e.g. "radiod_status", "status_address", "status_dns"
    current_status: str     # the multicast name found in the legacy field
    schema_kind: str        # "array" | "singleton" | "ka9q"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_candidates(etc_root: Path = Path("/etc")) -> list[Candidate]:
    """Walk /etc/<client>/*.toml looking for legacy schema patterns.

    Returns one Candidate per file that needs rewriting.  Files already
    using `status` (no legacy field present) produce no candidate.
    """
    candidates: list[Candidate] = []
    for client, (schema_kind, legacy_field) in _SCHEMA_BY_CLIENT.items():
        client_dir = etc_root / client
        if not client_dir.is_dir():
            continue
        for path in sorted(client_dir.glob("*.toml")):
            try:
                body = path.read_text()
            except OSError:
                continue
            status = _extract_legacy_status(body, schema_kind, legacy_field)
            if status:
                candidates.append(Candidate(
                    config_path=path,
                    client=client,
                    legacy_field=legacy_field,
                    current_status=status,
                    schema_kind=schema_kind,
                ))
    return candidates


def _extract_legacy_status(body: str, schema_kind: str,
                           legacy_field: str) -> Optional[str]:
    """Return the value of the legacy field if present AND `status` isn't
    already set in the same block.  None means nothing to migrate.

    Implementation note: a precise TOML-block-aware parse would be
    safer, but the schema is small enough that a simple "find legacy
    line; check there's no nearby `status =` line" heuristic works.
    """
    # Match the legacy line (anchored to start-of-line within block).
    legacy_pat = re.compile(
        rf'^\s*{re.escape(legacy_field)}\s*=\s*"([^"]+)"', re.MULTILINE
    )
    m = legacy_pat.search(body)
    if not m:
        return None
    # Already migrated?  Any `status = "..."` line counts as already
    # present (per-block check is more elaborate; we accept false
    # negatives in mixed-state configs and let operators re-run).
    if re.search(r'^\s*status\s*=\s*"[^"]+"', body, re.MULTILINE):
        return None
    return m.group(1)


# ---------------------------------------------------------------------------
# Rewriting (per-file, in-memory)
# ---------------------------------------------------------------------------

def rewrite(body: str, schema_kind: str, legacy_field: str,
            status_value: str) -> str:
    """Return the rewritten file body with the new `status` field in
    place of the legacy field.

    For "array" schema (psk/hfdl/codar): also removes the `id = "..."`
    line in the same block, since `id` is fully redundant once `status`
    is the canonical identifier.

    For "singleton" / "ka9q" schemas (wspr / hf-timestd): no `id` field
    to remove; just rename the field.
    """
    # Step 1: replace the legacy field's name with `status` (preserving
    # the quoted value).  `^\s*` matches leading whitespace; trailing
    # comments after the closing quote are preserved.
    legacy_pat = re.compile(
        rf'^(\s*){re.escape(legacy_field)}(\s*=\s*"[^"]+")',
        re.MULTILINE,
    )
    body = legacy_pat.sub(rf'\1status\2', body, count=1)

    # Step 2: for array-schema blocks, also strip the now-redundant
    # `id = "..."` line.  Only the FIRST [[radiod]] block's `id` line
    # is removed (mirrors how the recorders' Phase 3 code reads only
    # the first match).  Operators with multi-radiod legacy configs
    # need to migrate manually — out of scope for the auto-rewriter.
    if schema_kind == "array":
        id_pat = re.compile(r'^\s*id\s*=\s*"[^"]+"\s*\n', re.MULTILINE)
        body = id_pat.sub('', body, count=1)

    return body


# ---------------------------------------------------------------------------
# Coordination.toml block-key rewrite
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoordRewrite:
    """A [radiod.<old>] block in coordination.toml that should be
    re-keyed by its multicast name."""
    coord_path: Path
    old_key: str
    new_key: str  # the value of status_dns inside the block


def detect_coord_rewrite(
    coord_path: Path = Path("/etc/sigmond/coordination.toml"),
) -> Optional[CoordRewrite]:
    """Find a `[radiod."<X>"]` block whose key isn't already the
    multicast hostname.  Returns one rewrite (single-radiod hosts are
    the common case); multi-radiod hosts get the first mismatch and
    can re-run after applying.
    """
    if not coord_path.exists():
        return None
    body = coord_path.read_text()
    # Block-body match: any number of lines (including blank ones) up
    # until the next top-level section header `[...]` or end-of-file.
    # The `(?!\[)` lookahead inside the body avoids gobbling the next
    # section header into the body match.
    block_pat = re.compile(
        r'^\[radiod\."([^"]+)"\]\s*\n((?:(?!^\[).*\n)*)',
        re.MULTILINE,
    )
    for m in block_pat.finditer(body):
        key = m.group(1)
        block_body = m.group(2)
        status_dns_m = re.search(
            r'^\s*status_dns\s*=\s*"([^"]+)"', block_body, re.MULTILINE,
        )
        if status_dns_m:
            dns = status_dns_m.group(1)
            if dns != key:
                return CoordRewrite(
                    coord_path=coord_path,
                    old_key=key,
                    new_key=dns,
                )
    return None


def apply_coord_rewrite(rewrite: CoordRewrite) -> str:
    """Return the rewritten coordination.toml body.

    Renames the `[radiod."<old>"]` block header to use the new key,
    drops the now-redundant `status_dns` field, and updates any
    `[[clients.X]] radiod_id = "<old>"` references to point at the
    new key.
    """
    body = rewrite.coord_path.read_text()
    # Block header.
    body = re.sub(
        rf'^\[radiod\."{re.escape(rewrite.old_key)}"\]',
        f'[radiod."{rewrite.new_key}"]',
        body, count=1, flags=re.MULTILINE,
    )
    # Drop the status_dns line (now redundant — the block key IS it).
    body = re.sub(
        r'^\s*status_dns\s*=\s*"[^"]+"\s*\n',
        '',
        body, count=1, flags=re.MULTILINE,
    )
    # Update [[clients.X]] radiod_id refs.
    body = re.sub(
        rf'^(\s*radiod_id\s*=\s*)"{re.escape(rewrite.old_key)}"',
        rf'\1"{rewrite.new_key}"',
        body, flags=re.MULTILINE,
    )
    return body
