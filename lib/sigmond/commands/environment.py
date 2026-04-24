"""`smd environment` — situational-awareness inventory of peers around
this host.  Read-only, lock-free.

Subcommands:
    list      print declared manifest + latest cached delta status
    probe     run discovery, reconcile, write cache, summarise
    describe  deep view of one declared peer (or any cached observation)
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from typing import Optional

from ..environment import (
    Environment,
    EnvironmentView,
    Observation,
    load_environment,
)
from ..ui import err, heading, info, ok, warn
from .. import discovery
from ..discovery import reconciler as _reconciler


# ---------------------------------------------------------------------------
# smd environment list
# ---------------------------------------------------------------------------

def cmd_environment_list(args) -> int:
    env = load_environment()
    cache = discovery.load_cache()
    cached_obs = [discovery.dict_to_obs(o) for o in cache.get("observations", [])]
    deltas = _reconciler.reconcile(env, cached_obs)

    view = EnvironmentView(
        env=env,
        observations=cached_obs,
        deltas=deltas,
        probed_at=float(cache.get("probed_at", 0.0) or 0.0),
    )

    wanted_kind = getattr(args, "kind", None)

    if getattr(args, "json", False):
        payload = _view_to_dict(view, wanted_kind)
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    _print_human(view, wanted_kind)
    return 0


def _print_human(view: EnvironmentView, wanted_kind: Optional[str]) -> None:
    heading("environment")
    if view.env.source_path:
        info(f"manifest: {view.env.source_path}")
    if view.env.site.name:
        info(f"site: {view.env.site.name}")
    if view.probed_at:
        age = time.time() - view.probed_at
        info(f"last probe: {_fmt_age(age)} ago")
    else:
        info("last probe: never (run `smd environment probe`)")

    by_kind: dict = {}
    for d in view.deltas:
        by_kind.setdefault(d.kind, []).append(d)

    order = ("radiod", "kiwisdr", "gpsdo", "time_source")
    for kind in order:
        if wanted_kind and kind != wanted_kind:
            continue
        items = by_kind.get(kind, [])
        if not items:
            continue
        print(f"\n  \033[1m{kind}\033[0m")
        for delta in items:
            sym = _status_symbol(delta.status)
            host = _declared_host(delta.declared) or delta.id
            detail = f"  — {delta.detail}" if delta.detail else ""
            print(f"    {sym}  {delta.id:<24} [{host}]{detail}")


# ---------------------------------------------------------------------------
# smd environment probe
# ---------------------------------------------------------------------------

def cmd_environment_probe(args) -> int:
    env = load_environment()
    selected = getattr(args, "source", None) or "all"
    timeout  = float(getattr(args, "timeout", 3.0) or 3.0)
    force    = bool(getattr(args, "force", False))
    wanted_kind = getattr(args, "kind", None)

    sources = discovery.resolve_sources(env, selected)
    if not sources:
        err("no discovery sources enabled after filtering (check discovery.* in environment.toml)")
        return 1

    limiter = discovery.RateLimiter()
    # Seed limiter from cache so repeated --force still respects hard floor.
    cache = discovery.load_cache()
    for o in cache.get("observations", []):
        limiter.seed(o.get("source", ""),
                     _target_of(o),
                     float(o.get("observed_at", 0.0) or 0.0))

    observations: list[Observation] = []
    heading("environment probe")
    info(f"manifest: {env.source_path}")
    info(f"sources: {', '.join(sources)}")

    for src in sources:
        module = _module_for_source(src)
        if module is None:
            warn(f"unknown source {src!r}, skipping")
            continue
        if not _limiter_allows(limiter, src, env, force):
            info(f"{src}: skipped (within cadence; use --force to override)")
            continue
        try:
            obs = module.probe(env, timeout=timeout, limiter=limiter)
        except Exception as e:                   # noqa: BLE001
            err(f"{src}: probe raised {e.__class__.__name__}: {e}")
            continue
        if wanted_kind:
            obs = [o for o in obs if o.kind == wanted_kind]
        observations.extend(obs)
        good = sum(1 for o in obs if o.ok)
        info(f"{src}: {good}/{len(obs)} observations ok")

    deltas = _reconciler.reconcile(env, observations)
    view = EnvironmentView(env=env, observations=observations,
                           deltas=deltas, probed_at=time.time())
    try:
        discovery.save_cache(view)
    except OSError as e:
        warn(f"cache write failed: {e}")

    print()
    _print_summary(deltas)

    if getattr(args, "json", False):
        json.dump(_view_to_dict(view, wanted_kind),
                  sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")

    return 0 if not any(d.status == "missing" for d in deltas) else 2


def _print_summary(deltas: list) -> None:
    counts = {"healthy": 0, "degraded": 0, "missing": 0, "unknown-extra": 0}
    for d in deltas:
        counts[d.status] = counts.get(d.status, 0) + 1
    print(f"  \033[1msummary\033[0m  "
          f"\033[32m{counts['healthy']} healthy\033[0m, "
          f"\033[33m{counts['degraded']} degraded\033[0m, "
          f"\033[31m{counts['missing']} missing\033[0m, "
          f"\033[36m{counts['unknown-extra']} unknown-extra\033[0m")


# ---------------------------------------------------------------------------
# smd environment describe <peer-id>
# ---------------------------------------------------------------------------

def cmd_environment_describe(args) -> int:
    env  = load_environment()
    peer = getattr(args, "peer", None)
    if not peer:
        err("usage: smd environment describe <peer-id>")
        return 2

    declared = _find_declared(env, peer)
    cache = discovery.load_cache()
    all_obs = [discovery.dict_to_obs(o) for o in cache.get("observations", [])]

    matching_obs: list = []
    if declared is not None:
        dkind, d = declared
        matching_obs = [o for o in all_obs
                        if (o.id == d.id and o.kind == dkind)
                        or (o.kind == dkind and _host_of(o.endpoint) == d.host)]
    else:
        # No declared peer: try by hostname/endpoint
        matching_obs = [o for o in all_obs
                        if peer == o.id or peer == o.endpoint
                        or peer == _host_of(o.endpoint)]

    deltas = _reconciler.reconcile(env, all_obs)
    matching_delta = None
    for d in deltas:
        if declared and d.id == declared[1].id and d.kind == declared[0]:
            matching_delta = d
            break
        if not declared and (d.id == peer):
            matching_delta = d
            break

    if getattr(args, "json", False):
        payload = {
            "peer":     peer,
            "declared": _asdict(declared[1]) if declared else None,
            "kind":     declared[0] if declared else None,
            "observations": [_obs_asdict(o) for o in matching_obs],
            "delta":    _delta_asdict(matching_delta) if matching_delta else None,
        }
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    heading(f"environment describe {peer}")
    if declared is None and not matching_obs:
        err(f"no declared peer or cached observation matches {peer!r}")
        info("run `smd environment list` to see declared peers")
        return 1

    if declared:
        kind, d = declared
        info(f"kind: {kind}")
        for f in dataclasses.fields(d):
            v = getattr(d, f.name)
            if v in ("", [], {}, None):
                continue
            info(f"{f.name}: {v}")
    else:
        warn("no declared entry — this peer is unknown-extra")

    if matching_delta:
        sym = _status_symbol(matching_delta.status)
        print(f"\n  status: {sym} {matching_delta.status}"
              + (f" — {matching_delta.detail}" if matching_delta.detail else ""))

    if matching_obs:
        print(f"\n  \033[1mobservations ({len(matching_obs)})\033[0m")
        for o in matching_obs:
            mark = "\033[32m✓\033[0m" if o.ok else "\033[31m✗\033[0m"
            line = f"    {mark}  [{o.source}] {o.endpoint}"
            print(line)
            if o.error:
                print(f"        error: {o.error}")
            for k, v in sorted(o.fields.items()):
                print(f"        {k}: {_fmt_value(v)}")
    else:
        info("no cached observations — run `smd environment probe`")

    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _status_symbol(status: str) -> str:
    return {
        "healthy":       "\033[32m✓\033[0m",
        "degraded":      "\033[33m⚠\033[0m",
        "missing":       "\033[31m✗\033[0m",
        "unknown-extra": "\033[36m?\033[0m",
    }.get(status, "?")


def _declared_host(d) -> str:
    return getattr(d, "host", "") or ""


def _fmt_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _fmt_value(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


def _module_for_source(src: str):
    if src == "mdns":
        from ..discovery import mdns
        return mdns
    if src == "multicast":
        from ..discovery import multicast
        return multicast
    if src == "ntp":
        from ..discovery import ntp
        return ntp
    if src == "http_kiwisdr":
        from ..discovery import http_kiwisdr
        return http_kiwisdr
    if src == "gpsdo":
        from ..discovery import gpsdo
        return gpsdo
    return None


def _limiter_allows(limiter, src: str, env: Environment, force: bool) -> bool:
    targets = _targets_for_source(src, env)
    # Probe runs if *any* target is allowed; per-target throttling happens
    # in the probe itself if it wants more granular behaviour.  In v1 we
    # run or skip the whole source.
    for tgt in targets:
        if limiter.allow(src, tgt, force=force):
            return True
    return False


def _targets_for_source(src: str, env: Environment) -> list:
    if src == "mdns":
        return ["_site"]                         # single broadcast target
    if src == "multicast":
        return [r.status_dns or r.host for r in env.radiods]
    if src == "ntp":
        return [t.host for t in env.time_sources]
    if src == "http_kiwisdr":
        return [k.host for k in env.kiwisdrs]
    if src == "gpsdo":
        return [g.host or "localhost" for g in env.gpsdos]
    return []


def _target_of(obs_dict: dict) -> str:
    return _host_of(obs_dict.get("endpoint", "") or "")


def _host_of(endpoint: str) -> str:
    if not endpoint:
        return ""
    return endpoint.rsplit(":", 1)[0] if ":" in endpoint else endpoint


def _find_declared(env: Environment, peer: str):
    for kind, d in env.iter_declared():
        if d.id == peer:
            return kind, d
    return None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _asdict(obj) -> dict:
    if obj is None:
        return {}
    try:
        return dataclasses.asdict(obj)
    except TypeError:
        return {"repr": repr(obj)}


def _obs_asdict(o: Observation) -> dict:
    return {
        "source":      o.source,
        "kind":        o.kind,
        "id":          o.id,
        "endpoint":    o.endpoint,
        "fields":      o.fields,
        "observed_at": o.observed_at,
        "ok":          o.ok,
        "error":       o.error,
    }


def _delta_asdict(d) -> dict:
    return {
        "kind":     d.kind,
        "id":       d.id,
        "status":   d.status,
        "detail":   d.detail,
        "declared": _asdict(d.declared),
        "observed": [_obs_asdict(o) for o in d.observed],
    }


def _view_to_dict(view: EnvironmentView, wanted_kind: Optional[str]) -> dict:
    declared: dict = {}
    for kind, d in view.env.iter_declared():
        if wanted_kind and kind != wanted_kind:
            continue
        declared.setdefault(kind, []).append(_asdict(d))

    deltas = [_delta_asdict(d) for d in view.deltas
              if not wanted_kind or d.kind == wanted_kind]
    obs = [_obs_asdict(o) for o in view.observations
           if not wanted_kind or o.kind == wanted_kind]

    return {
        "site":         _asdict(view.env.site),
        "manifest_path": str(view.env.source_path) if view.env.source_path else None,
        "declared":     declared,
        "discovery":    _asdict(view.env.discovery),
        "probed_at":    view.probed_at,
        "observations": obs,
        "deltas":       deltas,
    }
