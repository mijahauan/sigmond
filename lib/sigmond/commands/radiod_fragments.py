"""Apply radiod channel fragments declared in client deploy.toml files.

Per CONTRACT v0.5 §15, a client author declares one or more channel
contributions in their ``deploy.toml``::

    [[radiod.fragment]]
    priority = 30                       # NN in 30-foo.conf
    target   = "${RADIOD_ID}"           # "*", a literal id, or ${VAR}
    template = "etc/radiod-fragment.conf"   # path inside the repo

Sigmond reads each enabled component's deploy.toml during ``smd apply``,
resolves ``target`` against ``coord.radiods``, renders the template via
``string.Template`` (stdlib `${VAR}` interpolation) using a small bag of
coordination-derived variables, and writes the result to::

    /etc/radio/radiod@<id>.conf.d/<NN>-<client>.conf

The applier is idempotent — identical content is left in place (sha256
compare).  ``dry_run`` mode reports what would change without touching
disk.  Failures (missing template, unparseable deploy.toml) degrade
gracefully into warning lines; the rest of ``cmd_apply`` continues.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Optional

from ..coordination import Coordination


RADIOD_CONFIG_DIR = Path('/etc/radio')
GIT_BASE = Path('/opt/git')

_DEFAULT_PRIORITY = 50      # mid-range when a fragment forgets to say


@dataclass(frozen=True)
class FragmentSpec:
    """One ``[[radiod.fragment]]`` declaration from a client's deploy.toml."""
    client: str
    priority: int
    target: str               # raw — "*", a literal id, or ${RADIOD_ID}
    template_path: Path

    @property
    def filename(self) -> str:
        return f"{self.priority:02d}-{self.client}.conf"


def collect_fragments(
    components: list[str],
    git_base: Path = GIT_BASE,
) -> list[FragmentSpec]:
    """Enumerate FragmentSpec from each component's deploy.toml.

    Components without a deploy.toml or without a ``[[radiod.fragment]]``
    block are silently skipped — that's the common case.
    """
    specs: list[FragmentSpec] = []
    for comp in components:
        deploy = git_base / comp / 'deploy.toml'
        try:
            if not deploy.exists():
                continue
        except PermissionError:
            continue
        try:
            with open(deploy, 'rb') as f:
                data = tomllib.load(f)
        except (OSError, PermissionError, tomllib.TOMLDecodeError):
            continue
        radiod_block = data.get('radiod') or {}
        for fragment in radiod_block.get('fragment') or []:
            template_rel = (fragment.get('template')
                            or fragment.get('content_template'))
            if not template_rel:
                continue
            specs.append(FragmentSpec(
                client=comp,
                priority=int(fragment.get('priority', _DEFAULT_PRIORITY)),
                target=str(fragment.get('target', '*')),
                template_path=git_base / comp / template_rel,
            ))
    return specs


def _resolve_targets(spec: FragmentSpec, radiod_ids: list[str]) -> list[str]:
    """Expand ``spec.target`` to a list of concrete radiod instance ids.

    Substitution rules:
    * ``"*"`` → every declared radiod
    * ``"${RADIOD_ID}"`` (or any single-var template) → every declared radiod
      (broadcast variable; the template gets the rid filled in per-write)
    * Any other literal → that exact id, if declared
    """
    raw = spec.target.strip()
    if not raw or raw == '*':
        return list(radiod_ids)
    # A target of "${RADIOD_ID}" means "this fragment applies to every
    # declared radiod, with RADIOD_ID being filled in per render".
    if raw == '${RADIOD_ID}':
        return list(radiod_ids)
    return [raw] if raw in radiod_ids else []


def _coordination_variables(coord: Coordination, radiod_id: str) -> dict[str, str]:
    """Build the variable bag a fragment template can interpolate.

    Mirrors the keys ``coordination.render_env`` emits, scoped to the
    fragment's target radiod instance.  Templates use ``${VAR}`` syntax.
    """
    variables: dict[str, str] = {'RADIOD_ID': radiod_id}

    radiod = coord.radiods.get(radiod_id)
    if radiod is not None:
        if radiod.host:
            variables['RADIOD_HOST'] = radiod.host
        if radiod.status_dns:
            variables['RADIOD_STATUS'] = radiod.status_dns
        if radiod.samprate_hz:
            variables['RADIOD_SAMPRATE'] = str(radiod.samprate_hz)

    if coord.host.call:
        variables['STATION_CALL'] = coord.host.call
    if coord.host.grid:
        variables['STATION_GRID'] = coord.host.grid
    if coord.host.lat:
        variables['STATION_LAT'] = str(coord.host.lat)
    if coord.host.lon:
        variables['STATION_LON'] = str(coord.host.lon)

    return variables


def _render(template_path: Path, variables: dict[str, str]) -> Optional[str]:
    """Read and ${VAR}-interpolate a template; return None if unreadable."""
    try:
        body = template_path.read_text()
    except (OSError, PermissionError):
        return None
    return Template(body).safe_substitute(variables)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def apply_fragments(
    coord: Coordination,
    components: list[str],
    *,
    dry_run: bool = False,
    git_base: Path = GIT_BASE,
    config_dir: Path = RADIOD_CONFIG_DIR,
    radiod_id: Optional[str] = None,
) -> list[str]:
    """Render and write every declared radiod fragment to disk.

    Args:
        coord: parsed coordination — used for variable interpolation and
            to enumerate target radiod instances.
        components: which clients to scan (typically the enabled set).
        dry_run: if True, report would-be changes without touching disk.
        git_base: override for tests.
        config_dir: override for tests; production is /etc/radio.
        radiod_id: if set, only emit fragments targeting this single
            radiod instance (used by ``smd config init radiod`` to wire
            up a freshly-created instance without re-applying every
            fragment for unrelated radiods).

    Returns a list of human-readable status lines — one per fragment
    written, skipped (idempotent), or reported as a warning.  Caller
    decides how to present them; this function never raises on a bad
    deploy.toml or missing template, only on programmer errors.
    """
    msgs: list[str] = []
    specs = collect_fragments(components, git_base=git_base)
    if not specs:
        return msgs

    if radiod_id is not None:
        radiod_ids = [radiod_id] if radiod_id in coord.radiods else []
    else:
        radiod_ids = sorted(coord.radiods.keys())

    if not radiod_ids:
        msgs.append(
            "warning: no radiod instances declared in coordination.toml; "
            "skipping radiod fragments"
        )
        return msgs

    for spec in specs:
        targets = _resolve_targets(spec, radiod_ids)
        if not targets:
            msgs.append(
                f"warning: {spec.client}: target {spec.target!r} did not "
                f"resolve to any declared radiod (have: {', '.join(radiod_ids)})"
            )
            continue

        for rid in targets:
            variables = _coordination_variables(coord, rid)
            body = _render(spec.template_path, variables)
            if body is None:
                msgs.append(
                    f"warning: {spec.client}: template not readable: "
                    f"{spec.template_path}"
                )
                continue

            target_dir = config_dir / f"radiod@{rid}.conf.d"
            target_path = target_dir / spec.filename

            existing: Optional[str] = None
            try:
                if target_path.exists():
                    existing = target_path.read_text()
            except (OSError, PermissionError):
                existing = None

            if existing is not None and _content_hash(existing) == _content_hash(body):
                msgs.append(f"  unchanged: {target_path}")
                continue

            if dry_run:
                action = "would update" if existing is not None else "would create"
                msgs.append(f"  (dry-run) {action} {target_path}")
                continue

            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path.write_text(body)
            except (OSError, PermissionError) as exc:
                msgs.append(
                    f"warning: {spec.client}: failed to write {target_path}: {exc}"
                )
                continue

            verb = "updated" if existing is not None else "wrote"
            msgs.append(f"  {verb} {target_path}")

    return msgs
