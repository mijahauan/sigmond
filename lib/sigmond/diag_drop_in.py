"""`smd admin diag drop-in <client>` — walk a single client's drop-in surface
and report green/red.

The point of "drop-in" is that a contract-conformant client repo, placed
at ``/opt/git/sigmond/<name>/`` and enabled in topology, should Just Work
through every sigmond surface: catalog discovery, ``smd install``,
``smd list``, the TUI Activity / Verifier / Config screens.  This module
checks each of those surfaces individually for one client, so the author
sees exactly which step is broken — instead of "it doesn't show up
anywhere, why?".

The check logic lives here (importable, testable in isolation) and the
CLI wiring lives in ``bin/smd`` (``cmd_diag_drop_in``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # py<3.11
    import tomli as tomllib  # type: ignore[no-redef]


# Repo install location.  Mirrors catalog.is_installed()'s primary path
# and client_features.REPO_ROOT.  Override in tests via run_checks().
REPO_ROOT = Path("/opt/git/sigmond")

# Inventory/validate subprocess deadline.  Originally set to 5s on the
# "inventory should be near-instant" assumption; in practice a Python
# client with a heavy import graph (hf-timestd: numpy, scipy, soundfile,
# digital_rf, pandas, etc.) needs 3-5s just for interpreter cold-start
# before its inventory builder runs.  10s gives that headroom while
# still catching genuinely stuck inventory (network reads, control
# socket hangs) which the original 5s aim at.
SUBPROCESS_TIMEOUT_SEC = 10.0


Status = str  # 'ok' | 'warn' | 'fail' | 'info'


@dataclass
class Check:
    """One row of the drop-in report."""

    name: str
    status: Status
    detail: str = ""
    remedy: str = ""

    @property
    def is_failure(self) -> bool:
        return self.status == "fail"


# ---------------------------------------------------------------------------
# Individual checks — each takes the precomputed context and returns one
# Check.  Splitting them out keeps the unit tests small and the CLI
# rendering uniform.
# ---------------------------------------------------------------------------


def _check_repo_present(client: str, repo_path: Path) -> Check:
    if os.path.lexists(str(repo_path)):
        return Check("repo present", "ok", str(repo_path))
    return Check(
        "repo present", "fail",
        f"no such directory: {repo_path}",
        f"git clone <your-repo> {repo_path}",
    )


def _check_deploy_toml(repo_path: Path) -> tuple[Check, Optional[dict]]:
    p = repo_path / "deploy.toml"
    if not p.exists():
        return Check(
            "deploy.toml present", "fail",
            f"no such file: {p}",
            "see docs/ADD-A-CLIENT.md §2 — every client ships a deploy.toml",
        ), None
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Check(
            "deploy.toml parseable", "fail",
            f"{p}: {exc}",
            "fix TOML syntax",
        ), None
    return Check("deploy.toml parseable", "ok", str(p)), data


def _check_contract_version(deploy: dict, supported: str) -> Check:
    pkg = deploy.get("package", {}) or {}
    cv = pkg.get("contract_version")
    if not cv:
        return Check(
            "[package].contract_version", "fail",
            "missing from [package] block",
            f"add `contract_version = \"{supported}\"` to [package]",
        )
    cv = str(cv)
    if cv == supported:
        return Check("[package].contract_version", "ok",
                     f"{cv} (matches sigmond)")
    return Check(
        "[package].contract_version", "warn",
        f"client reports {cv}, sigmond supports {supported}",
        "skew is tolerated within the same major; bump when you adopt new fields",
    )


def _check_binary_on_path(client: str) -> tuple[Check, Optional[str]]:
    binary = shutil.which(client)
    if binary:
        return Check("binary on PATH", "ok", binary), binary
    return Check(
        "binary on PATH", "fail",
        f"`{client}` not on $PATH",
        "ensure your [install] step links the binary into /usr/local/bin",
    ), None


def _run_json_subcommand(binary: str, verb: str) -> dict:
    """Invoke `<binary> <verb> --json` and return a dict describing the
    outcome: keys `exit`, `stdout`, `stderr`, `payload` (parsed JSON or
    None), `error` (string if subprocess/json error)."""
    try:
        r = subprocess.run(
            [binary, verb, "--json"],
            capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"exit": None, "stdout": "", "stderr": "",
                "payload": None,
                "error": f"timed out after {SUBPROCESS_TIMEOUT_SEC}s"}
    except OSError as exc:
        return {"exit": None, "stdout": "", "stderr": "",
                "payload": None, "error": f"spawn failed: {exc}"}
    out = {"exit": r.returncode, "stdout": r.stdout, "stderr": r.stderr,
           "payload": None, "error": ""}
    if r.stdout.strip():
        try:
            out["payload"] = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            out["error"] = f"stdout not parseable JSON: {exc}"
    return out


def _check_version_subcommand(client: str, binary: str) -> Check:
    res = _run_json_subcommand(binary, "version")
    if res["error"]:
        return Check(f"{client} version --json", "fail", res["error"])
    if res["exit"] != 0:
        return Check(
            f"{client} version --json", "fail",
            f"exit {res['exit']}: {res['stderr'].strip()[:200]}",
            "version --json must exit 0 (contract §3)",
        )
    if res["payload"] is None:
        return Check(f"{client} version --json", "fail",
                     "no JSON on stdout")
    return Check(f"{client} version --json", "ok",
                 f"version={res['payload'].get('version')}")


def _check_inventory_operator_callable(client: str, binary: str) -> Check:
    """The headline rule: `inventory --json` MUST exit 0 even when the
    operator can't read service-user-owned config.  If it doesn't,
    sigmond's ContractAdapter never sets `installed = True` and the
    TUI Config view reports the client as "not installed"."""
    res = _run_json_subcommand(binary, "inventory")
    if res["error"]:
        return Check(f"{client} inventory --json (operator-callable)",
                     "fail", res["error"])
    if res["exit"] != 0:
        return Check(
            f"{client} inventory --json (operator-callable)", "fail",
            f"exit {res['exit']}: {res['stderr'].strip()[:200]}",
            "inventory MUST exit 0 even on degraded paths "
            "(see hf-tec _degraded_inventory_payload)",
        )
    if res["payload"] is None:
        return Check(f"{client} inventory --json (operator-callable)",
                     "fail", "no JSON on stdout")
    issues = (res["payload"].get("issues") or [])
    fails = [i for i in issues
             if isinstance(i, dict) and i.get("severity") == "fail"]
    if fails:
        msg = fails[0].get("message", "")[:200]
        return Check(
            f"{client} inventory --json (operator-callable)", "warn",
            f"exit 0 (good) — but client reports degraded: {msg}",
            "inventory is parseable; runtime issues are a separate concern",
        )
    return Check(f"{client} inventory --json (operator-callable)", "ok",
                 f"exit 0, {len(res['payload'].get('instances') or [])} instance(s)")


def _check_validate_subcommand(client: str, binary: str) -> Check:
    """Validate's exit code is informational (a client may legitimately
    fail validate without a contract violation, e.g. waiting for a
    transmitter)."""
    res = _run_json_subcommand(binary, "validate")
    if res["error"]:
        return Check(f"{client} validate --json", "warn", res["error"])
    if res["payload"] is None:
        return Check(f"{client} validate --json", "warn",
                     "no JSON on stdout — exit "
                     f"{res['exit']}")
    n_issues = len((res["payload"].get("issues") or []))
    if res["exit"] == 0:
        return Check(f"{client} validate --json", "ok",
                     f"exit 0, {n_issues} issue(s)")
    return Check(f"{client} validate --json", "info",
                 f"exit {res['exit']}, {n_issues} issue(s) — "
                 "(non-zero is OK for transient state)")


def _check_client_features(deploy: dict, screen: str,
                           client: str = "", repo_root: Optional[Path] = None,
                           ) -> Check:
    block = deploy.get("client_features", {}).get(screen)
    name = f"[client_features.{screen}]"
    if not isinstance(block, dict):
        return Check(name, "info", "absent — client won't appear in this screen")
    desc = block.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return Check(name, "fail",
                     "present but missing required `description`")
    if screen == "verifier":
        kind = block.get("kind")
        if kind not in {"spot_queue", "local_db"}:
            return Check(name, "fail",
                         f"kind={kind!r} — must be 'spot_queue' or 'local_db'")
    if screen == "receiver_channels":
        # parser_file must resolve to a real file on disk, and
        # parser_attr must be a non-empty string.  We do NOT exec
        # the module here (too easy to import-error during diag);
        # the TUI itself surfaces import errors with a clear path.
        parser_file = block.get("parser_file")
        parser_attr = block.get("parser_attr")
        if not isinstance(parser_file, str) or not parser_file.strip():
            return Check(name, "fail",
                         "missing required `parser_file` (path relative to "
                         "repo root, e.g. \"src/foo/sigmond_tui.py\")")
        if not isinstance(parser_attr, str) or not parser_attr.strip():
            return Check(name, "fail",
                         "missing required `parser_attr` (callable name in "
                         "parser_file)")
        if client and repo_root is not None:
            target = repo_root / client / parser_file
            if not target.is_file():
                return Check(name, "fail",
                             f"parser_file does not resolve: {target}",
                             "create the parser module, or fix parser_file "
                             "in deploy.toml")
        per_instance = bool(block.get("per_instance", True))
        if not per_instance:
            config_path = block.get("config_path")
            if not isinstance(config_path, str) or not config_path.strip():
                return Check(name, "fail",
                             "per_instance=false requires an absolute "
                             "`config_path` to the singleton config")
        return Check(name, "ok",
                     f"parser={parser_file}:{parser_attr}, "
                     f"per_instance={per_instance}")
    return Check(name, "ok", f"verb={block.get('verb')!r}, "
                 f"description={desc[:60]!r}")


def _check_catalog_entry(client: str) -> tuple[Check, object]:
    try:
        from .catalog import load_catalog
        catalog = load_catalog()
    except Exception as exc:
        return Check("catalog entry", "warn",
                     f"could not load catalog: {exc}"), None
    entry = catalog.get(client)
    if entry is None:
        return Check(
            "catalog entry", "warn",
            "no entry — auto-discovery may pick it up, but other hosts "
            "won't see it in `smd list --available` until you add a "
            "[client.<name>] block to sigmond/etc/catalog.toml",
        ), None
    return Check("catalog entry", "ok",
                 f"kind={entry.kind!r}, contract={entry.contract!r}"), entry


def _check_catalog_is_installed(entry) -> Check:
    if entry is None:
        return Check("catalog.is_installed()", "info",
                     "skipped — no catalog entry")
    if entry.is_installed():
        return Check("catalog.is_installed()", "ok", "True")
    return Check(
        "catalog.is_installed()", "fail",
        "False — catalog can't see this client even though we found "
        f"the repo at {REPO_ROOT / entry.name}",
        "check entry's install_script / repo path in the catalog",
    )


def _check_topology_enabled(client: str) -> Check:
    try:
        from .topology import load_topology
        topo = load_topology()
    except Exception as exc:
        return Check("topology enabled", "warn",
                     f"could not load topology: {exc}")
    enabled = list(topo.enabled_components())
    if client in enabled:
        return Check("topology enabled", "ok", "True")
    return Check(
        "topology enabled", "info",
        "False — operator-disabled in /etc/sigmond/topology.toml "
        "(not a drop-in failure; TUI surfaces still skip it correctly)",
    )


def _check_sigmond_view(client: str) -> Check:
    """End-to-end: what does sigmond's ContractAdapter see?  This is
    what the TUI Config view renders, so it's the operator's truth."""
    try:
        from .clients import load_adapter
    except Exception as exc:
        return Check("sigmond ContractAdapter view", "warn",
                     f"could not load adapter dispatch: {exc}")
    adapter = load_adapter(client)
    if adapter is None:
        return Check(
            "sigmond ContractAdapter view", "warn",
            "no adapter resolved — client isn't in catalog with "
            "`contract` set",
        )
    try:
        view = adapter.read_view()
    except Exception as exc:
        return Check("sigmond ContractAdapter view", "fail",
                     f"read_view raised: {exc}")
    if view.installed:
        return Check("sigmond ContractAdapter view", "ok",
                     f"installed=True, contract={view.contract_version}, "
                     f"{len(view.instances)} instance(s)")
    issues = "; ".join(view.issues[:2])[:200] if view.issues else "(no issues reported)"
    return Check(
        "sigmond ContractAdapter view", "fail",
        f"installed=False — {issues}",
        "the TUI Config view will say 'not installed' until this is fixed",
    )


# ---------------------------------------------------------------------------
# Top-level: run all checks against one client.
# ---------------------------------------------------------------------------


def run_checks(client: str, repo_root: Path = REPO_ROOT,
               supported_contract_version: Optional[str] = None) -> list[Check]:
    """Run every drop-in check for `client` and return the list in
    rendering order.  Never raises — failures become fail-status rows."""
    if supported_contract_version is None:
        try:
            from .clients.contract import SUPPORTED_CONTRACT_VERSION as scv
            supported_contract_version = scv
        except Exception:
            supported_contract_version = "0.0"

    repo_path = repo_root / client
    checks: list[Check] = []

    repo_check = _check_repo_present(client, repo_path)
    checks.append(repo_check)
    if repo_check.is_failure:
        return checks   # everything downstream needs the repo

    deploy_check, deploy = _check_deploy_toml(repo_path)
    checks.append(deploy_check)
    if deploy is None:
        return checks

    checks.append(_check_contract_version(deploy, supported_contract_version))

    binary_check, binary = _check_binary_on_path(client)
    checks.append(binary_check)
    if binary is not None:
        checks.append(_check_version_subcommand(client, binary))
        checks.append(_check_inventory_operator_callable(client, binary))
        checks.append(_check_validate_subcommand(client, binary))

    checks.append(_check_client_features(deploy, "watch"))
    checks.append(_check_client_features(deploy, "verifier"))
    checks.append(_check_client_features(deploy, "receiver_channels",
                                         client=client, repo_root=repo_root))

    catalog_check, entry = _check_catalog_entry(client)
    checks.append(catalog_check)
    checks.append(_check_catalog_is_installed(entry))
    checks.append(_check_topology_enabled(client))
    checks.append(_check_sigmond_view(client))

    return checks


def render(checks: list[Check]) -> str:
    """Plain-text rendering — colour-coded glyphs match bin/smd's
    _ok / _warn / _err / _info palette."""
    glyphs = {"ok": "\033[32m✓\033[0m", "warn": "\033[33m⚠\033[0m",
              "fail": "\033[31m✗\033[0m", "info": "  "}
    lines = []
    width = max((len(c.name) for c in checks), default=20)
    for c in checks:
        glyph = glyphs.get(c.status, "?")
        line = f"  {glyph}  {c.name.ljust(width)}  {c.detail}"
        lines.append(line)
        if c.remedy and c.status in {"fail", "warn"}:
            lines.append(f"     \033[90m→\033[0m \033[90m{c.remedy}\033[0m")
    return "\n".join(lines)


def has_failure(checks: list[Check]) -> bool:
    """True if any check is fail-status — drives the CLI's exit code."""
    return any(c.is_failure for c in checks)


__all__ = [
    "Check",
    "Status",
    "REPO_ROOT",
    "SUBPROCESS_TIMEOUT_SEC",
    "run_checks",
    "render",
    "has_failure",
]
