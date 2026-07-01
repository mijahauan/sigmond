"""Generate the single-host hs-uploader manifest from each client's deploy.toml.

The single-host uploader daemon (``hs-uploader.service``, user ``hsupload``,
one host key) drains every outbound path on a host by reading
``/etc/hs-uploader/pipelines.toml``.  Stage 6 stops hand-writing that file:
each client declares its outbound pipeline(s) in its own ``deploy.toml`` as
``[[hs_uploader.pipeline]]`` blocks — manifest-native pipeline shape (the same
``[[pipeline]]`` shape ``hs_uploader.pipeline_factory`` consumes) but with
``{placeholder}`` tokens for the per-site identity.  This module reads the
enabled clients' declarations, substitutes identity read from where it already
lives, and renders the host manifest.

Identity is read from the *current* authoritative configs — no new store:

* ``{call}`` / ``{call_pathsafe}`` — the wspr reporter id (e.g. ``AC0G/S`` ->
  ``AC0G_S``) from :func:`sigmond.instance.list_instances`, else
  coordination ``[host].call``.
* ``{grid}`` — coordination ``[host].grid``.
* ``{radiod_status}`` — first radiod status DNS in coordination.
* ``{sink_path}`` / ``{ssh_key_file}`` — fixed host paths.
* ``{station_id}`` / ``{instrument_id}`` — PSWS ids for the *declaring* client,
  via :data:`sigmond.psws.RECORDERS` (grape -> hf-timestd config, mag -> mag
  config).

A pipeline whose required identity is missing/placeholder is **skipped with a
warning** (mirrors ``upload_creds`` "upload info missing") rather than emitted
broken.  The rendered manifest reproduces the same derived watermark keys
(``source_id``/``dest_id``/``table``) as a correctly hand-written one, so the
daemon inherits its cursors with no backlog re-ship.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Optional

from . import psws
from .coordination import Coordination, load_coordination
from .discover import find_deploy_toml
from .instance import display_reporter_id, list_instances
from .topology import Topology, load_topology

logger = logging.getLogger(__name__)

MANIFEST_PATH = Path("/etc/hs-uploader/pipelines.toml")
KEYS_DIR = Path("/etc/hs-uploader/keys")
SINK_PATH = "/var/lib/sigmond/sink.db"
PUMP_INTERVAL_SEC = 30

# Reporter-keyed clients whose per-instance reporter id is the host's upstream
# identity (display form, e.g. AC0G/S).  Single-rx hosts have exactly one.
_REPORTER_CLIENT = "wspr-recorder"


# --------------------------------------------------------------------------
# identity resolution (read from current configs)
# --------------------------------------------------------------------------


def host_key_file() -> str:
    """The one host SSH key.  Prefer the ``_host``-suffixed name the
    single-host daemon uses; fall back to the legacy shared name."""
    preferred = KEYS_DIR / "id_ed25519_host"
    legacy = KEYS_DIR / "id_ed25519"
    if legacy.exists() and not preferred.exists():
        return str(legacy)
    return str(preferred)


def _first_radiod_status(coord: Coordination) -> Optional[str]:
    for r in coord.radiods.values():
        dns = r.effective_status_dns
        if dns:
            return dns
    return None


def reporter_call(coord: Coordination,
                  instances: Optional[list] = None) -> Optional[str]:
    """The host's upstream reporter id in display form (``AC0G/S``).

    The wspr-recorder instance's reporter id is canonical (one per
    radiod+antenna); a single-rx host has exactly one.  Falls back to the
    coordination ``[host].call`` when no wspr instance exists."""
    if instances is None:
        try:
            instances = list_instances()
        except Exception:  # pragma: no cover - defensive
            instances = []
    reporters = [display_reporter_id(i.reporter_id)
                 for i in instances if i.client == _REPORTER_CLIENT]
    if reporters:
        return sorted(reporters)[0]
    return coord.host.call or None


def _psws_station_for_host(coord: Coordination) -> Optional[str]:
    """The host PSWS station id for the base ``[identity]`` block.

    Coordination may carry it in ``[station].psws_id``; otherwise derive it
    from the first PSWS-capable client that has one configured (hf-timestd's
    GRAPE station on a typical host)."""
    sid = getattr(coord.station, "psws_id", "") or ""
    if sid:
        return sid
    for client in ("hf-timestd", "mag-recorder"):
        try:
            st = psws.read_state(client)
        except Exception:
            continue
        if st.station:
            return st.station
    return None


def resolve_tokens(client: str, coord: Coordination,
                   call: Optional[str]) -> dict:
    """Substitution map for one client's pipeline declarations.

    Values are ``None`` when unresolved; a pipeline that *uses* an unresolved
    token is skipped by :func:`collect_pipelines`."""
    tokens: dict = {
        "call": call,
        "call_pathsafe": call.replace("/", "_") if call else None,
        "grid": coord.host.grid or None,
        "radiod_status": _first_radiod_status(coord),
        "sink_path": SINK_PATH,
        "ssh_key_file": host_key_file(),
    }
    if psws.is_psws_recorder(client):
        try:
            st = psws.read_state(client)
        except Exception:
            st = None
        tokens["station_id"] = (st.station or None) if st else None
        tokens["instrument_id"] = (st.instrument or None) if st else None
    return tokens


# --------------------------------------------------------------------------
# placeholder substitution
# --------------------------------------------------------------------------


def _subst(obj, tokens: dict, used: set, missing: set):
    if isinstance(obj, str):
        out = obj
        for tok, val in tokens.items():
            ph = "{" + tok + "}"
            if ph in out:
                used.add(tok)
                if val is None:
                    missing.add(tok)
                else:
                    out = out.replace(ph, val)
        return out
    if isinstance(obj, list):
        return [_subst(x, tokens, used, missing) for x in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, tokens, used, missing) for k, v in obj.items()}
    return obj


def _pipeline_decls(deploy_path: Path) -> list:
    """Read the ``[[hs_uploader.pipeline]]`` array from a client deploy.toml."""
    try:
        with open(deploy_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("uploader-manifest: cannot read %s: %s", deploy_path, exc)
        return []
    block = data.get("hs_uploader") or {}
    decls = block.get("pipeline") or []
    return decls if isinstance(decls, list) else []


def collect_pipelines(topology: Optional[Topology] = None,
                      coord: Optional[Coordination] = None) -> list:
    """Every enabled client's outbound pipeline(s), placeholders substituted.

    Returns a list of pipeline dicts ready for :func:`render_manifest`.  A
    pipeline whose required identity is unresolved is skipped with a warning."""
    topology = topology or load_topology()
    coord = coord or load_coordination()
    call = reporter_call(coord)

    pipelines: list = []
    by_name: dict = {}
    for client in topology.enabled_components():
        deploy = find_deploy_toml(client)
        if not deploy:
            continue
        decls = _pipeline_decls(deploy)
        if not decls:
            continue
        tokens = resolve_tokens(client, coord, call)
        for decl in decls:
            used: set = set()
            missing: set = set()
            rendered = _subst(decl, tokens, used, missing)
            name = rendered.get("name") or f"{client}-pipeline"
            if missing:
                logger.warning(
                    "uploader-manifest: skipping pipeline %s (%s) — "
                    "unresolved identity: %s",
                    name, client, ", ".join(sorted(missing)))
                continue
            # Dedup by name: two clients may declare the SAME shared pipeline
            # (e.g. psk-recorder + meteor-scatter both declare psk-pskreporter
            # because MSK144 rides the psk.spots stream).  Emit it once; warn
            # if the resolved bodies actually differ (keep the first).
            if name in by_name:
                if rendered != by_name[name]:
                    logger.warning(
                        "uploader-manifest: pipeline %s declared twice with "
                        "differing bodies (%s); keeping the first", name, client)
                continue
            by_name[name] = rendered
            pipelines.append(rendered)
    return pipelines


def build_identity(coord: Coordination, call: Optional[str]) -> dict:
    ident: dict = {}
    if call:
        ident["call"] = call
    if coord.host.grid:
        ident["grid"] = coord.host.grid
    sid = _psws_station_for_host(coord)
    if sid:
        ident["station_id"] = sid
    ident["ssh_key_file"] = host_key_file()
    return ident


# --------------------------------------------------------------------------
# TOML serialization (stdlib has no writer; the manifest shape is bounded)
# --------------------------------------------------------------------------


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot serialize {type(v).__name__} to TOML: {v!r}")


def _emit(header: Optional[str], d: dict, lines: list, *, array: bool = False) -> None:
    if header is not None:
        lines.append(f"[[{header}]]" if array else f"[{header}]")
    scalars = [(k, v) for k, v in d.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in d.items() if isinstance(v, dict)]
    for k, v in scalars:
        lines.append(f"{k} = {_toml_value(v)}")
    for k, v in tables:
        lines.append("")
        sub = f"{header}.{k}" if header else k
        _emit(sub, v, lines)


def render_manifest(pipelines: list, identity: dict,
                    *, pump_interval_sec: int = PUMP_INTERVAL_SEC) -> str:
    """Render the full ``pipelines.toml`` text from resolved pieces."""
    lines: list = [
        "# HamSCI single-host uploader manifest — GENERATED by sigmond.",
        "# Source: each enabled client's deploy.toml [[hs_uploader.pipeline]]",
        "# blocks, with per-site identity substituted. Regenerate with:",
        "#   smd admin uploader manifest --write",
        "# Drained by `hs-uploader serve` (hs-uploader.service, user hsupload).",
        "# DO NOT hand-edit — changes are overwritten on the next apply.",
        "",
    ]
    _emit("identity", identity, lines)
    lines.append("")
    _emit("daemon", {"pump_interval_sec": pump_interval_sec}, lines)
    for p in pipelines:
        lines.append("")
        _emit("pipeline", p, lines, array=True)
    return "\n".join(lines) + "\n"


def generate(topology: Optional[Topology] = None,
             coord: Optional[Coordination] = None) -> str:
    """Full pipeline: collect declarations + identity, render the manifest."""
    topology = topology or load_topology()
    coord = coord or load_coordination()
    call = reporter_call(coord)
    pipelines = collect_pipelines(topology, coord)
    identity = build_identity(coord, call)
    return render_manifest(pipelines, identity)
