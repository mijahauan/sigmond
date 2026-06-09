"""Pre-flight requirements check for `smd install <client>`.

Before doing any actual install work, walk the client's transitive
`requires` graph (declared in catalog.toml) and decide whether to
proceed.  ka9q-radio is special: it is NOT an absolute local-install
dependency — a remote radiod reachable on the LAN satisfies it just
fine, and sigmond's environment cache is what records that.

Decisions:
  - All deps installed locally → proceed silently.
  - ka9q-radio missing, no environment cache yet → abort and direct
    operator to `smd admin environment probe`.  That one probe informs every
    subsequent client install, so it's a one-time setup step, not a
    per-client tax.
  - ka9q-radio missing, cache shows >=1 remote radiod → dep is
    SATISFIED.  Print a brief notice and proceed.  If a local SDR is
    also attached (lsusb sees an RX-888 etc.), additionally offer an
    optional prompt "install ka9q-radio locally to use your SDR?"
    so the operator can opt into the richer setup.
  - ka9q-radio missing, no remote radiod, local SDR present → warn,
    recommend installing radiod here first, prompt to proceed anyway.
  - ka9q-radio missing, no remote radiod, no local SDR → warn (no data
    source), prompt to proceed anyway.

`--yes`/`-y` skips all prompts; a non-TTY stdin without `--yes` aborts
with a clear "re-run with --yes" message rather than silently choosing.
"""

from __future__ import annotations

import sys
import time
from typing import Dict, List, Tuple

from .catalog import CatalogEntry, get_entry, transitive_requires
from .discovery import load_cache
from .ui import err, heading, info, ok, warn


# Cache older than this is still consumed but the operator is warned.
STALE_AFTER_SECONDS = 3600


def check_requires(client: str,
                   catalog: Dict[str, CatalogEntry],
                   *,
                   yes: bool = False) -> bool:
    """Return True to proceed with install, False to abort."""
    entry = get_entry(client, catalog)
    if entry is None:
        # Let the install path surface its own clear "unknown client" error.
        return True

    missing = _unmet_requires(client, catalog)
    if not missing:
        return True

    ka9q_missing = any(name == "ka9q-radio" for name, _ in missing)
    cache = load_cache() if ka9q_missing else None

    # No environment scan yet → can't tell if a remote radiod would
    # satisfy ka9q-radio.  Direct the operator to probe and re-run.
    if ka9q_missing and not _cache_is_populated(cache):
        _render_no_cache(client, missing)
        if yes:
            warn("--yes passed; proceeding without environment data")
            return True
        err("aborting — run `smd admin environment probe` first, then re-run install.")
        return False

    radiod_obs = _filter_obs(cache, source="mdns", kind="radiod") if cache else []
    sdr_obs = _filter_obs(cache, source="usb_sdr", kind="sdr") if cache else []
    cache_age = _cache_age(cache) if cache else None
    cache_stale = cache_age is not None and cache_age > STALE_AFTER_SECONDS

    # ka9q-radio is satisfied if any remote radiod is reachable.  Drop it
    # from "missing" in that case so it doesn't trigger the warning path.
    ka9q_satisfied_by_lan = ka9q_missing and bool(radiod_obs)
    truly_missing = [(n, e) for n, e in missing
                     if not (n == "ka9q-radio" and ka9q_satisfied_by_lan)]

    # All real deps are satisfied (some locally, ka9q-radio via LAN).
    # Print a short notice; offer optional local-install prompt if a
    # local SDR is also present.
    if not truly_missing:
        _render_lan_satisfied(client, radiod_obs,
                               cache_stale=cache_stale, cache_age=cache_age)
        if sdr_obs and not yes and sys.stdin.isatty():
            return _prompt_optional_local_install(client, sdr_obs)
        if sdr_obs:
            # Non-interactive or --yes: mention the option but proceed.
            _render_local_sdr_note(sdr_obs)
        return True

    # Real unmet deps remain → full warning path.
    _render_warning(client, truly_missing, radiod_obs, sdr_obs,
                    cache_stale=cache_stale, cache_age=cache_age)

    if yes:
        warn("--yes passed; proceeding despite unmet requirements")
        return True

    if not sys.stdin.isatty():
        err("non-interactive stdin and --yes not passed; aborting.  "
            "Re-run with --yes to bypass this pre-flight check.")
        return False

    print()
    try:
        resp = input(f"Continue with {client} install anyway? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return resp.strip().lower().startswith("y")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unmet_requires(client: str,
                    catalog: Dict[str, CatalogEntry]
                    ) -> List[Tuple[str, CatalogEntry]]:
    """Return (name, entry) pairs for each transitive requirement that
    is not currently installed locally.  Skips requirements that aren't
    in the catalog at all — the install path's own validation will
    catch those.
    """
    out: List[Tuple[str, CatalogEntry]] = []
    for name in transitive_requires(client, catalog):
        if name == client:
            continue
        req = get_entry(name, catalog)
        if req is None:
            continue
        if req.is_installed():
            continue
        # Source-only deps (catalog entry with a repo URL but no
        # install_script — e.g. callhash, hs-uploader) are auto-cloned
        # by installer._clone_source_only_deps just before install.sh runs.
        # Don't report them as "missing" here; the auto-clone will satisfy them.
        # getattr() guards against test fakes that omit these fields.
        if getattr(req, 'repo', '') and not getattr(req, 'install_script', None):
            continue
        out.append((name, req))
    return out


def _cache_is_populated(cache) -> bool:
    if not cache:
        return False
    return float(cache.get("probed_at") or 0) > 0


def _cache_age(cache) -> float:
    if not cache:
        return float("inf")
    probed = float(cache.get("probed_at") or 0)
    if probed <= 0:
        return float("inf")
    return time.time() - probed


def _filter_obs(cache, *, source: str, kind: str) -> List[dict]:
    if not cache:
        return []
    return [o for o in (cache.get("observations") or [])
            if o.get("source") == source
            and o.get("kind") == kind
            and o.get("ok", True)]


def _prompt_optional_local_install(client: str,
                                     sdr_obs: List[dict]) -> bool:
    """Offer to install ka9q-radio locally when an SDR is attached.
    Returns True to proceed with the original client install, False to
    abort so the operator can run `smd install ka9q-radio` first.
    """
    _render_local_sdr_note(sdr_obs)
    print()
    try:
        resp = input("Install ka9q-radio locally first to use this "
                     "SDR? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return True  # default: proceed with original install
    if resp.strip().lower().startswith("y"):
        info(f"→ install ka9q-radio first, then re-run:")
        info(f"     smd install ka9q-radio")
        info(f"     smd config init radiod")
        info(f"     smd install {client}")
        return False
    return True


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _render_no_cache(client: str,
                     missing: List[Tuple[str, CatalogEntry]]) -> None:
    heading(f"pre-flight: {client}")
    warn(f"{client} declares dependencies that aren't all satisfied:")
    for name, _ in missing:
        info(f"missing: {name}")
    info("")
    info("no environment scan has been run on this host yet, so sigmond")
    info("can't tell you what's reachable on the LAN or attached locally.")
    info("the same scan informs every subsequent client install — running")
    info("it once is a one-time setup step, not per-client.")
    info("")
    info("→ run:  smd admin environment probe")
    info("        smd admin environment list      (to confirm what was found)")
    info(f"        smd install {client}    (re-run after the probe)")


def _render_lan_satisfied(client: str,
                          radiod_obs: List[dict],
                          *,
                          cache_stale: bool = False,
                          cache_age: float = None) -> None:
    heading(f"pre-flight: {client}")
    if cache_stale and cache_age is not None:
        warn(f"environment cache is {int(cache_age // 60)} min old "
             f"(>{STALE_AFTER_SECONDS // 60} min) — re-run `smd "
             f"environment probe` if the LAN has changed since.")
    ok(f"ka9q-radio satisfied: {len(radiod_obs)} radiod instance(s) "
       f"reachable on LAN (from environment cache)")
    for o in radiod_obs:
        fields = o.get("fields") or {}
        label = fields.get("name") or o.get("endpoint") or "(unnamed)"
        info(f"  - {label}")


def _render_local_sdr_note(sdr_obs: List[dict]) -> None:
    info("")
    info(f"note: {len(sdr_obs)} SDR device(s) attached locally:")
    for o in sdr_obs:
        fields = o.get("fields") or {}
        sdr_type = fields.get("sdr_type", "?")
        bus = fields.get("bus", "?")
        dev = fields.get("device", "?")
        info(f"  - {sdr_type} (bus {bus} dev {dev})")
    info("you could install ka9q-radio here to use this SDR as an "
         "additional source.")


def _render_warning(client: str,
                    missing: List[Tuple[str, CatalogEntry]],
                    radiod_obs: List[dict],
                    sdr_obs: List[dict],
                    *,
                    cache_stale: bool = False,
                    cache_age: float = None) -> None:
    heading(f"pre-flight: {client}")
    if cache_stale and cache_age is not None:
        warn(f"environment cache is {int(cache_age // 60)} min old "
             f"(>{STALE_AFTER_SECONDS // 60} min) — re-run `smd "
             f"environment probe` if the LAN has changed since.")
    warn(f"{client} declares dependencies that aren't all satisfied:")
    for name, _ in missing:
        info(f"missing: {name}")
        if name == "ka9q-radio":
            _explain_radiod_gap(radiod_obs, sdr_obs)


def _explain_radiod_gap(radiod_obs: List[dict],
                         sdr_obs: List[dict]) -> None:
    # If we got here for ka9q-radio, radiod_obs is empty by definition
    # (otherwise ka9q-radio would've been dropped from missing).
    info("  ↳ no radiod instances on LAN (per cached environment scan)")
    if sdr_obs:
        info(f"  ↳ but {len(sdr_obs)} SDR device(s) attached locally:")
        for o in sdr_obs:
            fields = o.get("fields") or {}
            sdr_type = fields.get("sdr_type", "?")
            bus = fields.get("bus", "?")
            dev = fields.get("device", "?")
            info(f"     - {sdr_type} (bus {bus} dev {dev})")
        info("  ↳ recommended: install radiod here first so this "
             "client has a data source:")
        info("       smd install ka9q-radio")
        info("       smd config init radiod")
    else:
        info("  ↳ and no local SDR detected either — this client "
             "will have no data source")
