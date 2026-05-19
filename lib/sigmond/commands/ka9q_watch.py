"""`smd ka9q-watch` — flag upstream ka9q-radio changes that could break clients.

Thin wrapper around ka9q-python's ``scripts/check_upstream_drift.py``:
locates the ka9q-python and ka9q-radio source trees, runs the checker
with ``--json``, and renders the result with sigmond's UI conventions.

Severity model (from the underlying script):

  pass  — no upstream commits, or upstream advanced but no header changed
  warn  — header changed but no stream-critical field affected
  fail  — a stream-critical TLV/enum value shifted (RTP delivery at risk)

Exit code is the script's: 0 on pass/warn, 1 on fail, 2 on setup error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..ui import err, heading, info, ok, warn


# Standard sigmond install location for client/library checkouts.
SIGMOND_GIT_ROOT = Path("/opt/git/sigmond")

# Where the running radiod binary lives. Same path on every install.
INSTALLED_RADIOD = Path("/usr/local/sbin/radiod")

# 40-char lowercase hex — git SHA-1, embedded in radiod binaries since
# upstream commit 2b66f820 (May 2026) via config_paths.h's GIT_HASH.
_SHA_RE = re.compile(rb"\b([0-9a-f]{40})\b")

# Dev-mode fallbacks — looked at only when the standard path is absent.
DEV_LOCATIONS = [
    Path("/home/wsprdaemon"),
    Path.home(),
]

_SEVERITY_GLYPH = {
    "pass": "\033[32m✓\033[0m",
    "warn": "\033[33m⚠\033[0m",
    "fail": "\033[31m✗\033[0m",
}


def _path_exists(p: Path) -> bool:
    """Path.exists() that treats PermissionError as 'not present'.

    `/home/wsprdaemon` is typically 0750 and owned by the wsprdaemon
    service user, so probing `/home/wsprdaemon/ka9q-python` from any
    other user trips a PermissionError out of os.stat.  We treat
    unreadable candidates as "doesn't exist for us" — the next
    candidate in the search path gets a chance.  Other OSErrors
    (e.g. ELOOP, ENAMETOOLONG) are likewise swallowed.
    """
    try:
        return p.exists()
    except (PermissionError, OSError):
        return False


def _resolve_path(name: str, override: Optional[str], env_var: str) -> Optional[Path]:
    """Resolve a checkout path with this priority:
       1. explicit --flag override
       2. environment variable
       3. /opt/git/sigmond/<name>
       4. dev fallbacks (~, /home/wsprdaemon)
    """
    if override:
        p = Path(override).expanduser().resolve()
        return p if _path_exists(p) else None

    env_val = os.environ.get(env_var)
    if env_val:
        p = Path(env_val).expanduser().resolve()
        return p if _path_exists(p) else None

    standard = SIGMOND_GIT_ROOT / name
    if _path_exists(standard):
        return standard.resolve()

    for base in DEV_LOCATIONS:
        candidate = base / name
        if _path_exists(candidate):
            return candidate.resolve()

    return None


def _read_installed_commit(binary: Path) -> Optional[str]:
    """Extract the GIT_HASH from a radiod binary, or None if absent.

    Upstream commit 2b66f820 (May 2026) embeds the build's git SHA via
    config_paths.h's GIT_HASH macro.  Older builds simply don't carry the
    hash, in which case we can't tell what's installed — return None and
    let the caller surface a warning.
    """
    if not binary.exists():
        return None
    try:
        data = binary.read_bytes()
    except OSError:
        return None
    matches = _SHA_RE.findall(data)
    # The binary contains exactly one embedded SHA (its own GIT_HASH).
    # Returning the first match avoids false positives on the off chance
    # any 40-hex string lives elsewhere in the data segment.
    return matches[0].decode() if matches else None


def _scan_compat_for_pin(compat: Path) -> Optional[str]:
    try:
        text = compat.read_text()
    except OSError:
        return None
    # Tolerate `KA9Q_RADIO_COMMIT = "..."` and the typed-annotation form
    # `KA9Q_RADIO_COMMIT: str = "..."`.  The body between the name and
    # the literal is anything up to the equals sign on the same line.
    m = re.search(
        r'KA9Q_RADIO_COMMIT\s*(?::[^=\n]*)?=\s*["\']([0-9a-f]{40})["\']',
        text,
    )
    return m.group(1) if m else None


# Per-client venvs that ship ka9q as an installed package.  We look here
# when no ka9q-python source checkout is available (the common B4-style
# install: clients pip-install ka9q from their venvs, no source tree).
_VENV_KA9Q_GLOBS = [
    "/opt/hf-timestd/venv/lib/python*/site-packages/ka9q/compat.py",
    "/opt/psk-recorder/venv/lib/python*/site-packages/ka9q/compat.py",
    "/opt/wspr-recorder/venv/lib/python*/site-packages/ka9q/compat.py",
]


def _read_pin_commit(py_root: Optional[Path]) -> Optional[str]:
    """Read ka9q-python's KA9Q_RADIO_COMMIT pin without importing.

    Search order:
      1. Source checkout passed in (py_root/src/ka9q/compat.py)
      2. Installed venvs (/opt/<client>/venv/.../site-packages/ka9q/compat.py)

    We never `import ka9q` — the watcher must work even when the Python
    running it has no ka9q in its path (sigmond's stdlib-only core).
    """
    if py_root is not None:
        compat = py_root / "src" / "ka9q" / "compat.py"
        if compat.exists():
            sha = _scan_compat_for_pin(compat)
            if sha:
                return sha

    import glob
    for pattern in _VENV_KA9Q_GLOBS:
        for path in glob.glob(pattern):
            sha = _scan_compat_for_pin(Path(path))
            if sha:
                return sha

    return None


def _ancestor(repo: Path, ancestor_sha: str, descendant_sha: str) -> Optional[bool]:
    """True iff ancestor is reachable from descendant in `repo`'s history.

    Returns None if either SHA isn't in the local clone (e.g., needs fetch).
    """
    res = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor",
         ancestor_sha, descendant_sha],
        capture_output=True, text=True,
    )
    if res.returncode == 0:
        return True
    if res.returncode == 1:
        return False
    # Any other code (commonly 128) = SHA missing from local clone.
    return None


def _check_installed_vs_pin(radio_root: Optional[Path],
                             py_root: Optional[Path]) -> dict:
    """Compare /usr/local/sbin/radiod's embedded commit to ka9q-python's pin.

    Severity:
      pass — installed == pin, or installed is a descendant of pin.
      fail — installed predates pin: features ka9q-python expects may be
             missing (e.g., LIFETIME tag added in commit 0f8b622).
      warn — unrelated history, or either SHA not present in the local
             ka9q-radio clone (likely needs fetch).
    """
    installed = _read_installed_commit(INSTALLED_RADIOD)
    pin = _read_pin_commit(py_root)

    out = {
        "installed_sha": installed,
        "installed_path": str(INSTALLED_RADIOD),
        "pin_sha": pin,
    }
    if installed is None:
        out["severity"] = "warn"
        out["summary"] = (
            f"cannot read commit from {INSTALLED_RADIOD} — "
            "binary missing or pre-2b66f820 (no GIT_HASH)"
        )
        return out
    if pin is None:
        out["severity"] = "warn"
        out["summary"] = "cannot read KA9Q_RADIO_COMMIT pin from ka9q-python"
        return out

    if installed == pin:
        out["severity"] = "pass"
        out["summary"] = "installed radiod matches ka9q-python pin"
        return out

    if radio_root is None:
        out["severity"] = "warn"
        out["summary"] = (
            f"installed {installed[:12]} ≠ pin {pin[:12]} — cannot tell "
            "which is older without a ka9q-radio checkout"
        )
        return out

    pin_is_ancestor = _ancestor(radio_root, pin, installed)
    if pin_is_ancestor is True:
        out["severity"] = "pass"
        out["summary"] = (
            "installed radiod is newer than pin (descendant) — OK"
        )
        return out
    if pin_is_ancestor is False:
        # Installed predates pin: the python expects features the binary
        # may not implement.  This is the trap that hid LIFETIME silently
        # ignoring our keep-alive on B4-100 in May 2026.
        out["severity"] = "fail"
        out["summary"] = (
            "installed radiod PREDATES ka9q-python pin — features "
            "expected by clients may be missing (e.g., LIFETIME tag)"
        )
        return out
    # _ancestor returned None → SHA(s) missing from the local clone.
    out["severity"] = "warn"
    out["summary"] = (
        "cannot compare installed vs pin — one or both commits missing "
        "from local ka9q-radio clone (run `git fetch`)"
    )
    return out


def _render_installed_check(check: dict) -> None:
    sev = check.get("severity", "warn")
    glyph = _SEVERITY_GLYPH.get(sev, "?")
    print()
    print(f"  {glyph}  {check.get('summary', '(no summary)')}")
    inst = check.get("installed_sha")
    pin = check.get("pin_sha")
    if inst:
        print(f"     installed: {inst[:12]}  ({check.get('installed_path')})")
    if pin:
        print(f"     pin:       {pin[:12]}  (ka9q-python KA9Q_RADIO_COMMIT)")
    if sev == "fail":
        print()
        info("Rebuild + reinstall ka9q-radio from a checkout containing")
        info(f"the pin commit, then `make install` from /opt/git/sigmond/ka9q-radio.")


def _render_human(report: dict) -> None:
    sev = report.get("severity", "fail")
    glyph = _SEVERITY_GLYPH.get(sev, "?")
    summary = report.get("summary") or report.get("error") or "(no summary)"

    heading('ka9q-watch')
    print(f"  {glyph}  {summary}")

    pin  = report.get("pin")
    up   = report.get("upstream_sha")
    ref  = report.get("upstream_ref")
    if pin:
        print(f"     pin:      {pin[:12]}")
    if up:
        print(f"     upstream: {up[:12]}  ({ref or '?'})")

    commits = report.get("commits") or []
    if commits:
        print(f"     commits:  {len(commits)} ahead")
        for c in commits[-10:]:
            mark = "H" if c.get("touches_headers") else " "
            print(f"       [{mark}] {c['sha'][:12]}  {c['subject']}")
        if len(commits) > 10:
            print(f"       … {len(commits) - 10} earlier commit(s) elided")

    for d in report.get("header_deltas") or []:
        sym = _SEVERITY_GLYPH.get(d.get("severity", "warn"), "?")
        print(f"     {sym}  {d['header']} ({d['enum']}):")
        for c in d.get("changes", []):
            csym = _SEVERITY_GLYPH.get(c.get("severity", "warn"), "?")
            kind = c.get("kind")
            name = c.get("name", "?")
            if kind == "added":
                detail = f"+{name} = {c.get('head')}"
            elif kind == "removed":
                detail = f"-{name}  (was {c.get('pin')})"
            elif kind == "value_changed":
                detail = f"~{name}: {c.get('pin')} → {c.get('head')}"
            else:
                detail = f"?{name}"
            print(f"         {csym}  {detail}  — {c.get('reason', '')}")

    if sev == "fail":
        print()
        info("Do NOT advance the ka9q-radio pin until ka9q-python is updated")
        info("to handle the changed fields, or downstream RTP clients will break.")


def cmd_ka9q_watch(args) -> int:
    py_root = _resolve_path("ka9q-python", getattr(args, "ka9q_python", None),
                            "KA9Q_PYTHON_PATH")
    radio_root = _resolve_path("ka9q-radio", getattr(args, "ka9q_radio", None),
                               "KA9Q_RADIO_PATH")

    # Installed-vs-pin check is independent of source checkouts — it
    # only needs the installed radiod binary and any venv shipping
    # ka9q.compat.  Always run it; surface its result even when the
    # upstream-drift check can't run.
    installed_check = _check_installed_vs_pin(radio_root, py_root)

    # Drift check needs both checkouts AND the bundled drift script.
    drift_disabled_reason: Optional[str] = None
    if py_root is None:
        drift_disabled_reason = "ka9q-python checkout not found"
    elif radio_root is None:
        drift_disabled_reason = "ka9q-radio checkout not found"

    if drift_disabled_reason is not None:
        # Render the installed-vs-pin result alone.
        if getattr(args, "json", False):
            json.dump({
                "upstream_drift": {"severity": "skip",
                                   "summary": drift_disabled_reason},
                "installed_vs_pin": installed_check,
            }, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 1 if installed_check.get("severity") == "fail" else 0
        heading("ka9q-watch")
        warn(f"upstream drift check skipped — {drift_disabled_reason}")
        _render_installed_check(installed_check)
        return 1 if installed_check.get("severity") == "fail" else 0

    script = py_root / "scripts" / "check_upstream_drift.py"
    if not script.exists():
        err(f"drift checker missing: {script}")
        info("ka9q-python may be older than the watcher; pull latest.")
        # Still surface the installed-vs-pin check.
        _render_installed_check(installed_check)
        return 1 if installed_check.get("severity") == "fail" else 2

    python_bin = shutil.which("python3") or sys.executable
    cmd = [python_bin, str(script),
           "--ka9q-radio", str(radio_root),
           "--remote", getattr(args, "remote", None) or "origin",
           "--branch", getattr(args, "branch", None) or "main",
           "--json"]
    if getattr(args, "no_fetch", False):
        cmd.append("--no-fetch")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        err(f"could not invoke {script}: {exc}")
        return 2

    # Parse JSON if present; otherwise surface stderr.
    report: Optional[dict] = None
    if proc.stdout.strip():
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass

    # Second check: is the *installed* radiod binary in sync with the
    # pin?  This catches the silent-failure mode where the upstream pin
    # is fine but the operator forgot to rebuild + reinstall, so a stale
    # /usr/local/sbin/radiod silently ignores the protocol features
    # ka9q-python expects.  Drove a 9-hour misdiagnosis on B4-100 in
    # May 2026 (LIFETIME tag dropped by April-vintage radiod).
    installed_check = _check_installed_vs_pin(radio_root, py_root)

    if getattr(args, "json", False):
        # Combine the upstream-drift report and the installed-vs-pin
        # check under one document so consumers get both signals.
        combined = {
            "upstream_drift": report,
            "installed_vs_pin": installed_check,
        }
        json.dump(combined, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        # Return whichever exit code is more severe.
        rc = proc.returncode or 0
        if installed_check.get("severity") == "fail":
            rc = max(rc, 1)
        return rc

    if report is None:
        err("drift checker emitted no JSON")
        if proc.stderr:
            info(proc.stderr.strip())
        # Still surface the installed-vs-pin check — it's independent.
        _render_installed_check(installed_check)
        rc = proc.returncode or 2
        if installed_check.get("severity") == "fail":
            rc = max(rc, 1)
        return rc

    _render_human(report)
    _render_installed_check(installed_check)
    rc = proc.returncode
    if installed_check.get("severity") == "fail":
        rc = max(rc, 1)
    return rc
