"""Reconciliation — match observations to declared peers and classify."""

from __future__ import annotations

from typing import Iterable

from ..environment import (
    DeclaredGpsdo,
    DeclaredKiwi,
    DeclaredRadiod,
    DeclaredTimeSource,
    Delta,
    Environment,
    Observation,
)


def reconcile(env: Environment, observations: Iterable[Observation]) -> list[Delta]:
    """Produce Delta records for every (kind, declared) and every
    (kind, observed-but-unmatched) combination.

    Matching strategy per observation:
      1. exact id match (if observation.id is set and known)
      2. (kind, host) match against declared records
      3. unknown-extra

    Status classification:
      * healthy        — matched and any `expect.*` hints on a radiod pass
      * degraded       — matched but an `expect.*` hint failed
      * missing        — declared, no matching successful observation
      * unknown-extra  — observation that matched no declaration
    """
    obs_list = list(observations)

    # Index declared peers by (kind, id) and (kind, host)
    declared_by_id   = {}
    declared_by_host = {}
    for kind, d in env.iter_declared():
        declared_by_id[(kind, getattr(d, 'id', ''))] = d
        declared_by_host.setdefault((kind, getattr(d, 'host', '')), []).append(d)

    # Group observations per declared peer
    matched: dict = {}                          # (kind, id) -> list[Observation]
    unmatched: list = []                        # list[Observation]
    for o in obs_list:
        d = None
        if o.id and (o.kind, o.id) in declared_by_id:
            d = declared_by_id[(o.kind, o.id)]
        else:
            candidates = declared_by_host.get((o.kind, _host_from_endpoint(o.endpoint)), [])
            if len(candidates) == 1:
                d = candidates[0]
        if d is None:
            unmatched.append(o)
        else:
            key = (o.kind, d.id)
            matched.setdefault(key, []).append(o)
            # Backfill the id onto the observation so the UI groups it cleanly
            o.id = d.id

    deltas: list[Delta] = []

    # Declared peers — healthy / degraded / missing
    for kind, d in env.iter_declared():
        obs = matched.get((kind, d.id), [])
        good = [o for o in obs if o.ok]
        if not good:
            deltas.append(Delta(
                kind=kind, id=d.id, status="missing",
                detail="no successful observation",
                declared=d, observed=obs,
            ))
            continue
        status, detail = _classify_hints(kind, d, good)
        deltas.append(Delta(
            kind=kind, id=d.id, status=status, detail=detail,
            declared=d, observed=good,
        ))

    # Observations that matched nothing declared
    for o in unmatched:
        if not o.ok:
            continue                            # failed probe of something we don't even expect
        deltas.append(Delta(
            kind=o.kind,
            id=o.id or o.endpoint or f"<{o.source}>",
            status="unknown-extra",
            detail=f"observed via {o.source}",
            declared=None,
            observed=[o],
        ))

    return deltas


def _host_from_endpoint(endpoint: str) -> str:
    if not endpoint:
        return ""
    # "host:port" -> "host"; pure hostnames pass through
    return endpoint.rsplit(":", 1)[0] if ":" in endpoint else endpoint


def _classify_hints(kind: str, declared, good_obs: list) -> tuple:
    """Apply per-kind `expect.*` hints.  Returns (status, detail)."""
    if kind == "radiod" and isinstance(declared, DeclaredRadiod):
        expect = declared.expect or {}
        # Merge observed fields from all successful observations; later
        # observations win.  Typical sources: mdns, multicast.
        merged: dict = {}
        for o in good_obs:
            merged.update(o.fields)
        for dotted_key, wanted in _flatten(expect).items():
            actual = _dig(merged, dotted_key)
            if actual is None:
                continue                        # no signal either way; don't flag
            if actual != wanted:
                return "degraded", f"expect {dotted_key}={wanted!r} but observed {actual!r}"
        return "healthy", ""

    if kind == "kiwisdr" and isinstance(declared, DeclaredKiwi):
        if declared.gps_expected:
            merged = {}
            for o in good_obs:
                merged.update(o.fields)
            gps = merged.get("gps_fix")
            if gps is False:
                return "degraded", "gps_expected=true but observed gps_fix=false"
        return "healthy", ""

    if kind == "time_source" and isinstance(declared, DeclaredTimeSource):
        if declared.stratum_max:
            merged = {}
            for o in good_obs:
                merged.update(o.fields)
            stratum = merged.get("stratum")
            if isinstance(stratum, int) and stratum > declared.stratum_max:
                return "degraded", f"stratum {stratum} exceeds max {declared.stratum_max}"
        return "healthy", ""

    if kind == "gpsdo" and isinstance(declared, DeclaredGpsdo):
        merged = {}
        for o in good_obs:
            merged.update(o.fields)
        if merged.get("locked") is False:
            return "degraded", "GPSDO reports unlocked"
        return "healthy", ""

    return "healthy", ""


def _flatten(d: dict, prefix: str = "") -> dict:
    """Flatten {'frontend': {'gpsdo_lock': True}} -> {'frontend.gpsdo_lock': True}."""
    out: dict = {}
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def _dig(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur
