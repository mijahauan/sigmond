"""Tier-1 dispatch helpers for per-client whiptail wizards.

Public surface (re-exported from the package's __init__):

  is_wizard_available(args, wizard_path) -> bool
  exec_wizard(wizard_path, *, extra_env=None, parse="kv" | "json" | None,
              extra_args=None) -> WizardResult
  WizardResult dataclass

See ../README.md for the observed contract this captures and the
adoption sketch per client.

The dispatch is intentionally minimal: it does not know about menu
loops, ask-helpers, the leading-dash whiptail workaround, the JSON
config-apply protocol, or per-key help.toml sidecars.  Those live in
each client's own scripts/config-wizard.sh and stay there until a
fourth client adopts the full-walker shape that would justify
extracting them too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# Bumped on incompatible dispatch-API changes.  Clients can pin via
# `from sigmond.wizard_dispatch import SIGMOND_WIZARD_DISPATCH_API` if
# they want to fail fast on a version mismatch.
SIGMOND_WIZARD_DISPATCH_API = "1"


@dataclass
class WizardResult:
    """What `exec_wizard` returns.

    `returncode`     subprocess exit code.  0 == success / clean cancel.
    `fields`         parsed wizard stdout, shape depends on `parse=`:
                       - `"kv"`   -> dict[str, str]
                       - `"json"` -> dict (the top-level JSON object)
                       - `None`   -> None
                     `{}` (or None) on clean cancel / no-op.
    `stderr`         the wizard's stderr forwarded for caller logging.
    `error`          short human-readable string when the wizard failed
                     for a reason other than operator cancel (OSError
                     on exec, JSON decode error, etc.).
    """
    returncode: int
    fields: Optional[dict]
    stderr: str
    error: Optional[str]

    @property
    def ok(self) -> bool:
        """True when the wizard ran cleanly (including operator cancel)."""
        return self.returncode == 0 and self.error is None


def is_wizard_available(args, wizard_path) -> bool:
    """Decide whether the whiptail wizard should run for this invocation.

    Returns True iff all four conditions hold:

      1. The caller did NOT pass --non-interactive (we honour
         `getattr(args, "non_interactive", False)`; an args object
         that lacks the attribute is treated as interactive).
      2. Both stdin and stdout are TTYs (whiptail needs both; piping
         either disables the wizard so scripted runs use the legacy
         path automatically).
      3. `whiptail` is on PATH (the operator may have a minimal host
         with no whiptail package -- the legacy stdin path still
         works on those).
      4. `wizard_path` is an existing, executable file (the script
         may not have been installed yet on dev hosts).

    Pure check; no side effects.  Caller is expected to fall back to
    its legacy stdin-prompt path when this returns False.
    """
    if getattr(args, "non_interactive", False):
        return False
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return False
    if shutil.which("whiptail") is None:
        return False
    p = Path(wizard_path)
    if not p.is_file() or not os.access(p, os.X_OK):
        return False
    return True


def exec_wizard(
    wizard_path,
    *,
    extra_env: Optional[dict] = None,
    parse: Optional[str] = "kv",
    extra_args: Optional[list] = None,
    interactive: Optional[bool] = None,
) -> WizardResult:
    """Spawn the wizard, forward args, parse stdout per `parse=`.

    `parse="kv"`   parse stdout as KEY=VALUE lines (wspr-recorder shape).
    `parse="json"` parse stdout as a single JSON object (potential
                   future caller; not used by any existing client today).
    `parse=None`   don't parse; .fields will be None.  Use this when
                   the wizard writes its own side effects (e.g.
                   mag-recorder / psk-recorder wizards that pipe JSON
                   to `<client> config apply` themselves).

    `interactive`  when True, the child INHERITS the parent's stdio so
                   whiptail can render dialogs to the operator's
                   terminal -- AND parse is short-circuited (fields
                   will always be None, stderr will always be empty),
                   because the operator saw both directly.  When
                   False, stdout/stderr are captured into pipes so
                   the caller can parse stdout per `parse=` and read
                   stderr for logging.

                   Default:
                     parse=None         -> interactive=True
                     parse="kv"/"json"  -> interactive=False

                   The default exists because a wizard that doesn't
                   echo data on stdout (parse=None) is almost
                   certainly one whose only stdout traffic IS the
                   whiptail UI -- which has to reach the terminal,
                   else the operator sees nothing and the wizard
                   exits silently.

                   For parse="kv"/"json" the wizard's UI rendering
                   happens via fd-swapping like `3>&1 1>&2 2>&3`
                   inside the script -- captured stdout is the
                   structured result, not the UI.

                   Explicit interactive=True with parse="kv"/"json"
                   is allowed (rare: a wizard that writes its UI
                   AND its structured result to stdout, no fd swap)
                   but the parse step is skipped -- fields=None.

    Any OSError from `subprocess.run` is caught and surfaced as
    `WizardResult.error`; the caller is then expected to fall back
    to its legacy stdin-prompt path.

    The child inherits the parent's env merged with `extra_env`.
    Two conventions every existing wizard relies on:

      <CLIENT>_CLI       absolute path to the Python CLI binary the
                         operator invoked (`sys.argv[0]`), so the
                         shell wizard can shell back to e.g.
                         `psk-recorder config apply` without rediscovering it.
      <CLIENT>_HELP_TOML path to the per-key help sidecar (for clients
                         that have one).

    The caller sets these in `extra_env` -- this module doesn't
    invent or hard-code key names.
    """
    if parse not in (None, "kv", "json"):
        raise ValueError(f"parse must be None, 'kv', or 'json'; got {parse!r}")

    if interactive is None:
        interactive = (parse is None)

    env = {**os.environ, **(extra_env or {})}
    cmd: list = [str(wizard_path)]
    if extra_args:
        cmd.extend(str(a) for a in extra_args)

    # Stdio strategy: interactive -> child inherits parent's stdio so
    # whiptail can render to the terminal.  Non-interactive -> capture
    # both so the caller can parse stdout and log stderr.
    try:
        if interactive:
            proc = subprocess.run(cmd, env=env, check=False)
        else:
            proc = subprocess.run(
                cmd, env=env, capture_output=True, text=True, check=False,
            )
    except OSError as exc:
        return WizardResult(returncode=1, fields=None,
                            stderr="", error=f"exec failed: {exc}")

    # Interactive runs have nothing to parse and no captured stderr to
    # forward; the operator already saw both directly on the terminal.
    if interactive:
        return WizardResult(
            returncode=proc.returncode,
            fields=None,
            stderr="",
            error=None,
        )

    fields: Optional[dict]
    if parse is None:
        fields = None
    elif parse == "kv":
        fields = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            fields[k.strip().lower()] = v.strip()
    else:  # parse == "json"
        out = proc.stdout.strip()
        if not out:
            fields = {}
        else:
            try:
                fields = json.loads(out)
            except json.JSONDecodeError as exc:
                return WizardResult(
                    returncode=proc.returncode,
                    fields=None,
                    stderr=proc.stderr,
                    error=f"wizard stdout was not valid JSON: {exc}",
                )
            if not isinstance(fields, dict):
                return WizardResult(
                    returncode=proc.returncode,
                    fields=None,
                    stderr=proc.stderr,
                    error=f"wizard JSON top-level was {type(fields).__name__}, expected object",
                )

    return WizardResult(
        returncode=proc.returncode,
        fields=fields,
        stderr=proc.stderr,
        error=None,
    )
