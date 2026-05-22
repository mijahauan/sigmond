"""Reconcile the RX888 FX3 firmware variant on the local radiod host.

The RX888mk2's Cypress FX3 USB3 controller enters DFU (Device Firmware
Upgrade) mode at power-on; radiod uploads a firmware image into it
before sample capture can begin.  ka9q-radio's ``dist_path()`` lookup
order for ``SDDC_FX3.img`` is::

    1. /etc/radio/SDDC_FX3.img             (Confdir — local override)
    2. /usr/local/share/ka9q-radio/SDDC_FX3.img   (Pkgdatadir — stock)

Two firmware builds exist today:

- **stock** — the SDDC_FX3.img shipped in the ka9q-radio source tree
  (``share/SDDC_FX3.img``).  Installed by ``make install`` into
  Pkgdatadir.
- **david** — David's variant, more resilient to USB transient errors
  (recovers from short-bus stalls that wedge stock).  Currently the
  test-program firmware on B4-100; intended to become the default once
  field-validated.

This module manages which variant is active on the local host via a
single knob in ``/etc/sigmond/topology.toml``::

    [component.radiod.firmware]
    variant = "david"   # or "stock"; defaults to "david" if absent

Reconciliation is path-based:

- ``variant = "david"``:
    1. Ensure ``/etc/radio/SDDC_FX3.img`` matches sigmond's vendored
       copy at ``share/firmware/SDDC_FX3.img`` (sha256 compare).
    2. Defensively rename Pkgdatadir's ``SDDC_FX3.img`` to
       ``SDDC_FX3.img.stock`` if it exists — so a stale ``/etc/radio``
       wipe doesn't silently fall back to stock.
- ``variant = "stock"``:
    1. Remove ``/etc/radio/SDDC_FX3.img`` (so the Confdir override is
       gone).
    2. Restore Pkgdatadir's ``SDDC_FX3.img.stock`` → ``SDDC_FX3.img``
       if present, so radiod's fallback path finds it.

The reconciler is idempotent — identical state on disk means a no-op
return.  When the active firmware path actually changes (different
sha256, or path moves), the caller is told to restart the local
``radiod@*`` units so the FX3 re-DFUs into the new image.

Only the local radiod is reconfigured; remote radiods on other hosts
(bee1/bee2 etc.) each manage their own ``/etc/radio/`` independently.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import List, Tuple


CONFDIR_FW = Path('/etc/radio/SDDC_FX3.img')
PKGDATADIR_FW = Path('/usr/local/share/ka9q-radio/SDDC_FX3.img')
PKGDATADIR_STOCK_DISABLED = Path(
    '/usr/local/share/ka9q-radio/SDDC_FX3.img.stock'
)
VENDORED_DAVID = Path(
    '/opt/git/sigmond/sigmond/share/firmware/SDDC_FX3.img'
)


_VALID_VARIANTS = ('david', 'stock')


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(64 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _resolve_variant(topology: dict) -> str:
    """Read ``[component.radiod.firmware] variant`` from topology.toml.

    Defaults to ``"david"`` when the key is absent — the test-program
    firmware is the desired baseline on hosts that haven't been
    explicitly pinned to stock.
    """
    comp = topology.get('component', {}).get('radiod', {})
    fw = comp.get('firmware', {})
    variant = str(fw.get('variant', 'david')).strip().lower()
    if variant not in _VALID_VARIANTS:
        # Unknown variant — surface as a warning from the caller's
        # message list; return the safe default rather than erroring.
        return 'david'
    return variant


def detect_active_variant() -> str:
    """Best-effort detection of the variant radiod would load right now.

    Returns ``"david"``, ``"stock"``, or ``"unknown"`` if neither
    Confdir nor Pkgdatadir holds a recognisable image.  Used by
    ``smd list`` to show the operator what's currently live without
    requiring an apply.
    """
    if CONFDIR_FW.exists():
        try:
            confdir_sha = _sha256(CONFDIR_FW)
        except OSError:
            return 'unknown'
        try:
            david_sha = _sha256(VENDORED_DAVID)
        except OSError:
            david_sha = None
        if david_sha and confdir_sha == david_sha:
            return 'david'
        # Non-vendored binary at /etc/radio — could be a hand-placed
        # custom build.  Don't claim it's david; report unknown.
        return 'unknown'
    # No /etc/radio override; radiod will fall back to Pkgdatadir.
    if PKGDATADIR_FW.exists():
        return 'stock'
    return 'unknown'


def apply_firmware(
    topology: dict, *, dry_run: bool = False,
) -> Tuple[List[str], bool]:
    """Reconcile firmware variant against topology config.

    Returns ``(messages, needs_restart)``.  ``needs_restart`` is True
    when the active firmware path actually changed and the caller
    should restart the local radiod@*.service units so the FX3 reloads
    the new image.

    Failure modes (missing vendored binary, permission errors, etc.)
    degrade into warning messages — the rest of ``cmd_apply`` keeps
    running.
    """
    msgs: List[str] = []
    needs_restart = False

    variant = _resolve_variant(topology)

    if variant == 'david':
        return _apply_david(dry_run=dry_run, msgs=msgs)
    return _apply_stock(dry_run=dry_run, msgs=msgs)


def _apply_david(
    *, dry_run: bool, msgs: List[str],
) -> Tuple[List[str], bool]:
    needs_restart = False

    if not VENDORED_DAVID.exists():
        msgs.append(
            f"  warning: variant=david but vendored binary missing at "
            f"{VENDORED_DAVID} — leaving /etc/radio untouched"
        )
        return msgs, False

    want_sha = _sha256(VENDORED_DAVID)
    if CONFDIR_FW.exists():
        cur_sha = _sha256(CONFDIR_FW)
        if cur_sha == want_sha:
            # /etc/radio already has the right binary.
            pass
        else:
            if dry_run:
                msgs.append(
                    f"  (dry-run) would update {CONFDIR_FW} to match "
                    f"vendored david (sha {want_sha[:12]}…)"
                )
            else:
                _install_binary(VENDORED_DAVID, CONFDIR_FW)
                msgs.append(
                    f"firmware: updated {CONFDIR_FW} → david "
                    f"(sha {want_sha[:12]}…)"
                )
                needs_restart = True
    else:
        if dry_run:
            msgs.append(
                f"  (dry-run) would install {CONFDIR_FW} from vendored "
                f"david (sha {want_sha[:12]}…)"
            )
        else:
            _install_binary(VENDORED_DAVID, CONFDIR_FW)
            msgs.append(
                f"firmware: installed {CONFDIR_FW} → david "
                f"(sha {want_sha[:12]}…)"
            )
            needs_restart = True

    # Defensively rename stock in Pkgdatadir aside, so a future
    # /etc/radio wipe doesn't silently fall back to stock.
    if PKGDATADIR_FW.exists():
        if dry_run:
            msgs.append(
                f"  (dry-run) would rename {PKGDATADIR_FW} → "
                f"{PKGDATADIR_STOCK_DISABLED.name} (defense)"
            )
        else:
            try:
                PKGDATADIR_FW.rename(PKGDATADIR_STOCK_DISABLED)
                msgs.append(
                    f"firmware: renamed {PKGDATADIR_FW.name} → "
                    f"{PKGDATADIR_STOCK_DISABLED.name} (defense)"
                )
            except OSError as exc:
                msgs.append(
                    f"  warning: could not rename {PKGDATADIR_FW}: {exc}"
                )

    return msgs, needs_restart


def _apply_stock(
    *, dry_run: bool, msgs: List[str],
) -> Tuple[List[str], bool]:
    needs_restart = False

    # Remove /etc/radio override so radiod's lookup falls through to
    # Pkgdatadir.
    if CONFDIR_FW.exists():
        if dry_run:
            msgs.append(f"  (dry-run) would remove {CONFDIR_FW}")
        else:
            try:
                CONFDIR_FW.unlink()
                msgs.append(f"firmware: removed {CONFDIR_FW} (variant=stock)")
                needs_restart = True
            except OSError as exc:
                msgs.append(
                    f"  warning: could not remove {CONFDIR_FW}: {exc}"
                )
                return msgs, False

    # Restore the disabled-stock back to the canonical name.
    if PKGDATADIR_STOCK_DISABLED.exists() and not PKGDATADIR_FW.exists():
        if dry_run:
            msgs.append(
                f"  (dry-run) would restore "
                f"{PKGDATADIR_STOCK_DISABLED.name} → {PKGDATADIR_FW.name}"
            )
        else:
            try:
                PKGDATADIR_STOCK_DISABLED.rename(PKGDATADIR_FW)
                msgs.append(
                    f"firmware: restored {PKGDATADIR_FW.name} (variant=stock)"
                )
                needs_restart = True
            except OSError as exc:
                msgs.append(
                    f"  warning: could not restore "
                    f"{PKGDATADIR_STOCK_DISABLED}: {exc}"
                )

    if not PKGDATADIR_FW.exists() and not PKGDATADIR_STOCK_DISABLED.exists():
        msgs.append(
            f"  warning: variant=stock but no stock firmware found at "
            f"{PKGDATADIR_FW} or {PKGDATADIR_STOCK_DISABLED} — radiod "
            f"will fail to load firmware"
        )

    return msgs, needs_restart


def _install_binary(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` with mode 0644, owner root:radio.

    Matches the upstream ``share/Makefile``'s install line (mode 0664
    actually, but radiod only needs world-read; 0644 is the common
    default).  Atomic via temp-file + rename so a partial write can't
    leave radiod with a truncated image.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + '.tmp')
    shutil.copy2(src, tmp)
    tmp.chmod(0o644)
    # Best-effort chown to root:radio (matches /etc/radio/'s existing
    # files).  If chown fails (e.g. running as non-root in tests), the
    # caller has bigger problems — silently keep going so the binary
    # is still in place.
    try:
        import grp, os
        radio_gid = grp.getgrnam('radio').gr_gid
        os.chown(tmp, 0, radio_gid)
    except (KeyError, PermissionError, OSError):
        pass
    tmp.rename(dst)
