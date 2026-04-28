"""Reconciliation — match observations to declared peers and classify."""

from __future__ import annotations

from typing import Iterable

from ..environment import (
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
    """Apply per-kind `expect.*` hints.  Returns (status, detail).

    Per-kind classifier logic now lives in ``environment_kinds.KindSpec``
    next to the dataclass and TUI rendering for the same kind.  Kinds
    without a registered classifier (or unknown kinds) default to
    "healthy", "" — same as the old fall-through.
    """
    from ..environment_kinds import REGISTRY
    spec = REGISTRY.get(kind)
    if spec is None or spec.expect_classifier is None:
        return "healthy", ""
    return spec.expect_classifier(declared, good_obs)
