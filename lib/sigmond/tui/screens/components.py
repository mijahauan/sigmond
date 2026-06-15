"""Software Versions screen — catalog components, install state, git refs, version policy.

Shows every catalog entry with:
  - install status (present at /opt/git/sigmond/<name>)
  - current HEAD ref (git branch@sha)
  - version policy from topology.toml (latest / pinned ref / ignore)

Double-click a row to open ComponentDetailModal where git history, policy,
and per-component update are available.  The main screen has two actions:
"Update All Now" and "Fetch + Refresh".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time as _time
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState

from ..mutation import ConfirmModal, UpdateOutputModal


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    return shutil.which('smd') or '/usr/local/bin/smd'


# Sigmond clients live under /opt/git/sigmond/ (the sigmond namespace).
# Non-sigmond infra repos (ka9q-radio, ka9q-web, ka9q-python) live in
# the general-use /opt/git/ space; the fallback below resolves catalog
# entries whose canonical name differs from their repo-stem (e.g.
# 'radiod' → /opt/git/ka9q-radio).
_OPT_GIT_SIGMOND = Path('/opt/git/sigmond')
_OPT_GIT = Path('/opt/git')


def _find_repo_dir(name: str, repo_url: str) -> Optional[Path]:
    """Return the cloned repo path for a catalog entry.

    Checks /opt/git/sigmond/<name> first (sigmond's namespace).  Falls
    back to /opt/git/<url-stem> for non-sigmond infra repos like
    'radiod' whose clone lives at /opt/git/ka9q-radio.
    Sigmond itself lives next to this file, not under /opt/git.
    """
    # Sigmond manages itself — its repo is the parent of this library.
    # components.py lives at <repo>/lib/sigmond/tui/screens/components.py,
    # so parents[4] is the repo root.
    if name == 'sigmond':
        self_dir = Path(__file__).resolve().parents[4]
        if (self_dir / '.git').exists():
            return self_dir

    primary = _OPT_GIT_SIGMOND / name
    if primary.exists():
        return primary
    if repo_url:
        stem = repo_url.rstrip('/').rsplit('/', 1)[-1].removesuffix('.git')
        if stem and stem != name:
            fallback = _OPT_GIT / stem
            if fallback.exists():
                return fallback
    return None


@dataclass
class _ComponentRow:
    name: str
    kind: str
    description: str
    repo: str
    installed: bool
    repo_dir: Optional[Path]
    current_ref: str         # e.g. "main@abc1234" or "—"
    version_policy: str      # "latest" | "ignore" | "<ref>"
    enabled: bool = False    # topology.toml: [component.X] enabled = true/false
    lifecycle: str = "—"    # "running" | "stopped" | "available" | "missing" | "dormant"
    gated_reason: str = ""   # when lifecycle=="dormant": absent hardware label
    commit_idx: str = "—"   # total commit count, e.g. "247"
    behind: str = "—"       # commits behind origin/main, e.g. "3" or "0"
    ahead: str = "—"        # local commits not pushed (mirror of behind)
    dirty: bool = False      # uncommitted/unstaged changes in working tree
    dirty_reason: str = ""   # short explanation when dirty=True
    last_commit_date: str = "—"   # YYYY-MM-DD of most recent local commit
    last_commit_ts: float = 0.0   # unix timestamp for sorting
    log_lines: list[str] = field(default_factory=list)
    ahead_log_lines: list[str] = field(default_factory=list)  # remote-only commits not yet pulled


@dataclass
class _ComponentsView:
    rows: list[_ComponentRow] = field(default_factory=list)
    error: Optional[str] = None


def _git(repo_dir: Path, *args, timeout: int = 5) -> subprocess.CompletedProcess:
    """Run a git command in repo_dir, bypassing safe.directory for root-owned repos."""
    return subprocess.run(
        ['git', '-c', f'safe.directory={repo_dir}', '-C', str(repo_dir), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _git_ref(repo_dir: Path) -> str:
    """Return a short human-readable ref for HEAD, or '—' on failure."""
    try:
        branch = subprocess.run(
            ['git', '-c', f'safe.directory={repo_dir}',
             '-C', str(repo_dir), 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, timeout=5,
        )
        sha = subprocess.run(
            ['git', '-c', f'safe.directory={repo_dir}',
             '-C', str(repo_dir), 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5,
        )
        b = branch.stdout.strip()
        s = sha.stdout.strip()
        if b and b != 'HEAD' and s:
            return f'{b}@{s}'
        if s:
            # Detached HEAD: find which local branch(es) contain this commit.
            r = subprocess.run(
                ['git', '-c', f'safe.directory={repo_dir}',
                 '-C', str(repo_dir), 'branch', '--contains', 'HEAD',
                 '--format=%(refname:short)'],
                capture_output=True, text=True, timeout=5,
            )
            branches = [l.strip() for l in r.stdout.splitlines()
                        if l.strip() and not l.strip().startswith('(')]
            if 'main' in branches:
                return f'main@{s}'
            if branches:
                return f'{branches[0]}@{s}'
        return s or '—'
    except Exception:
        return '—'


def _git_last_commit_date(repo_dir: Path) -> tuple[str, float]:
    """Return (YYYY-MM-DD, unix_timestamp) of the most recent local commit."""
    try:
        r = _git(repo_dir, 'log', '-1', '--format=%as %at')
        parts = r.stdout.strip().split()
        if r.returncode == 0 and len(parts) >= 2:
            return parts[0], float(parts[1])
        if r.returncode == 0 and len(parts) == 1:
            return parts[0], 0.0
    except Exception:
        pass
    return "—", 0.0


def _git_log(repo_dir: Path, n: int = 15) -> list[str]:
    """Return the last n log entries as '#idx  sha  date  subject'."""
    try:
        r_count = _git(repo_dir, 'rev-list', '--count', 'HEAD')
        total = (int(r_count.stdout.strip())
                 if r_count.returncode == 0 and r_count.stdout.strip().isdigit()
                 else 0)
        r = _git(repo_dir, 'log', f'-{n}', '--format=%h %as %s', timeout=8)
        if r.returncode != 0:
            return []
        result = []
        for i, line in enumerate(r.stdout.splitlines()):
            if not line.strip():
                continue
            idx = total - i
            parts = line.split(' ', 2)
            sha  = parts[0]
            date = parts[1] if len(parts) > 1 else ''
            subj = parts[2] if len(parts) > 2 else ''
            result.append(f"#{idx:<5}  {sha}  {date}  {subj}")
        return result
    except Exception:
        return []


def _git_log_ahead(repo_dir: Path, n: int = 20) -> list[str]:
    """Return commits on origin/main that are ahead of local HEAD (not yet pulled)."""
    try:
        r_count = _git(repo_dir, 'rev-list', '--count', 'origin/main')
        total = (int(r_count.stdout.strip())
                 if r_count.returncode == 0 and r_count.stdout.strip().isdigit()
                 else 0)
        r = _git(repo_dir, 'log', f'-{n}', 'HEAD..origin/main',
                 '--format=%h %as %s', timeout=8)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        result = []
        for i, line in enumerate(r.stdout.splitlines()):
            if not line.strip():
                continue
            idx = total - i
            parts = line.split(' ', 2)
            sha  = parts[0]
            date = parts[1] if len(parts) > 1 else ''
            subj = parts[2] if len(parts) > 2 else ''
            result.append(f"#{idx:<5}  {sha}  {date}  {subj}")
        return result
    except Exception:
        return []


def _git_fetch(repo_dir: Path) -> bool:
    """Run git fetch origin in the background, return True on success."""
    try:
        r = _git(repo_dir, 'fetch', 'origin', '--prune', timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _git_commit_idx(repo_dir: Path) -> str:
    """Return total commit count for HEAD as a string, e.g. '247'."""
    try:
        r = _git(repo_dir, 'rev-list', '--count', 'HEAD')
        v = r.stdout.strip()
        return v if r.returncode == 0 and v.isdigit() else "—"
    except Exception:
        return "—"


def _git_behind(repo_dir: Path) -> str:
    """Return how many commits HEAD is behind origin/main (local cache only)."""
    try:
        r = _git(repo_dir, 'rev-list', '--count', 'HEAD..origin/main')
        v = r.stdout.strip()
        return v if r.returncode == 0 and v.isdigit() else "—"
    except Exception:
        return "—"


def _to_int(s: str) -> int:
    """Parse a string-numeric field, returning 0 for any non-int value
    (including the "—" sentinel used for "not applicable")."""
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _git_ahead(repo_dir: Path) -> str:
    """Return how many local commits are not yet on origin/main."""
    try:
        r = _git(repo_dir, 'rev-list', '--count', 'origin/main..HEAD')
        v = r.stdout.strip()
        return v if r.returncode == 0 and v.isdigit() else "—"
    except Exception:
        return "—"


def _git_is_dirty(repo_dir: Path) -> tuple[bool, str]:
    """True if the working tree has uncommitted, staged, or untracked changes.

    Returns (dirty, reason).  Reason is a short summary suitable for a
    table cell ("staged", "unstaged", "untracked", or a comma-joined mix);
    empty when clean.  Untracked files count — a half-finished edit on
    the side of an `apply` should still show up so the operator notices
    before pulling on top of it.
    """
    try:
        r = _git(repo_dir, 'status', '--porcelain=v1', '--untracked-files=normal')
        if r.returncode != 0:
            return False, ""
        flags: set[str] = set()
        for line in r.stdout.splitlines():
            if not line:
                continue
            head = line[:2]
            if head.startswith('??'):
                flags.add('untracked')
                continue
            staged_ch, work_ch = head[0], head[1]
            if staged_ch != ' ':
                flags.add('staged')
            if work_ch != ' ':
                flags.add('unstaged')
        if not flags:
            return False, ""
        order = ['staged', 'unstaged', 'untracked']
        return True, ','.join(f for f in order if f in flags)
    except Exception:
        return False, ""


def _gather(topology_components: dict, do_fetch: bool = False) -> _ComponentsView:
    """Worker: load catalog + topology, scan /opt/git/sigmond, collect git refs."""
    view = _ComponentsView()
    try:
        from ...catalog import load_catalog
        catalog = load_catalog()
    except FileNotFoundError as exc:
        view.error = f"catalog not found: {exc}"
        return view
    except Exception as exc:
        view.error = str(exc)
        return view

    # Lifecycle inference reuses the CLI's component_state module so the
    # TUI's STATUS column matches `smd list` exactly.  Import is best-effort
    # — if the module fails to load (e.g. sigmond lib path unresolved), we
    # fall back to a coarse "installed/missing" label.
    try:
        from ...component_state import compute_state as _compute_state
        from ...topology import load_topology as _load_topology
        from ...paths import TOPOLOGY_PATH as _TOPOLOGY_PATH
        _full_topo = _load_topology(_TOPOLOGY_PATH)
    except Exception:
        _compute_state = None
        _full_topo = None

    # Collect repo dirs first so we can batch-fetch before reading state.
    repo_dirs: list[tuple[str, Path]] = []
    for name in sorted(catalog):
        entry = catalog[name]
        if not entry.repo:
            continue
        repo_dir = _find_repo_dir(name, entry.repo)
        if repo_dir:
            repo_dirs.append((name, repo_dir))

    if do_fetch:
        seen: set[Path] = set()
        for _, rd in repo_dirs:
            if rd not in seen:
                seen.add(rd)
                _git_fetch(rd)

    for name in sorted(catalog):
        entry = catalog[name]
        if not entry.repo:
            continue  # no git repo; not version-manageable here
        repo_dir = _find_repo_dir(name, entry.repo)
        installed = repo_dir is not None or entry.is_installed()

        current_ref     = _git_ref(repo_dir)             if repo_dir else '—'
        commit_idx      = _git_commit_idx(repo_dir)      if repo_dir else '—'
        behind          = _git_behind(repo_dir)          if repo_dir else '—'
        ahead           = _git_ahead(repo_dir)           if repo_dir else '—'
        dirty, dirty_reason = _git_is_dirty(repo_dir)    if repo_dir else (False, "")
        log_lines       = _git_log(repo_dir)             if repo_dir else []
        ahead_log_lines = _git_log_ahead(repo_dir)       if repo_dir else []
        last_commit_date, last_commit_ts = (
            _git_last_commit_date(repo_dir) if repo_dir else ('—', 0.0)
        )

        # Topology may key components by either the canonical catalog
        # name or the topology_alias (legacy name, often the systemd
        # service name).  Fall back through the alias so a host whose
        # topology.toml still has [component.radiod] resolves correctly
        # against the renamed [client.ka9q-radio] catalog entry.
        comp = (topology_components.get(name)
                or (topology_components.get(entry.topology_alias)
                    if getattr(entry, 'topology_alias', None) else None))
        policy = (comp.version if comp else 'latest') or 'latest'
        enabled = bool(comp.enabled) if comp else False

        # Derive lifecycle keyword from the individual ComponentState flags
        # in order: cloned → installed → configured → enabled → active.
        # state.stage skips this ordering and returns "enabled" whenever
        # topology says enabled=true, which mis-labels missing components
        # the operator hasn't actually installed yet.
        lifecycle = "missing"
        if _compute_state is not None and _full_topo is not None:
            try:
                st = _compute_state(
                    name, _full_topo,
                    alias=getattr(entry, 'topology_alias', None),
                )
                if not st.cloned:
                    lifecycle = "missing"
                elif not st.installed:
                    lifecycle = "available"
                elif not st.configured:
                    lifecycle = "needs cfg"
                elif not st.enabled:
                    lifecycle = "configured"
                elif not st.active:
                    lifecycle = "stopped"
                else:
                    lifecycle = "running"
            except Exception:
                lifecycle = "installed" if installed else "missing"
        else:
            lifecycle = "installed" if installed else "missing"

        # Hardware-gated overlay: an enabled core component whose hardware is
        # absent reads as "dormant", not stopped/running — same source of truth
        # as `smd admin validate`'s rule_hardware_gated_core (harmonize.dormant_reason).
        gated_reason = ""
        try:
            from ...harmonize import dormant_reason as _dormant_reason
            reason = _dormant_reason(name, enabled=enabled)
            if reason:
                lifecycle = "dormant"
                gated_reason = reason
        except Exception:
            pass  # best-effort overlay; never break the table

        view.rows.append(_ComponentRow(
            name=name,
            kind=entry.kind,
            description=entry.description,
            repo=entry.repo,
            installed=installed,
            repo_dir=repo_dir,
            current_ref=current_ref,
            version_policy=policy,
            enabled=enabled,
            lifecycle=lifecycle,
            gated_reason=gated_reason,
            commit_idx=commit_idx,
            behind=behind,
            ahead=ahead,
            dirty=dirty,
            dirty_reason=dirty_reason,
            last_commit_date=last_commit_date,
            last_commit_ts=last_commit_ts,
            log_lines=log_lines,
            ahead_log_lines=ahead_log_lines,
        ))

    # Sort: most recently committed local code first.
    view.rows.sort(key=lambda r: r.last_commit_ts, reverse=True)
    return view


# ---------------------------------------------------------------------------
# Module-level topology writers (shared by ComponentsScreen and modal)
# ---------------------------------------------------------------------------

def _write_topology_toml(topo, path: Path) -> None:
    """Write topology.toml from a loaded Topology object."""
    lines = [
        "# /etc/sigmond/topology.toml",
        "# Managed by smd tui. Manual edits are fine too.",
        "",
    ]
    for comp_name in sorted(topo.components):
        comp = topo.components[comp_name]
        lines.append(f"[component.{comp_name}]")
        lines.append(f'enabled = {"true" if comp.enabled else "false"}')
        if not comp.managed:
            lines.append("managed = false")
        if comp.version and comp.version != "latest":
            lines.append(f'version = "{comp.version}"')
        if comp.description:
            lines.append(f'description = "{comp.description}"')
        if comp.rac_id:
            lines.append(f'rac_id = "{comp.rac_id}"')
        if comp.rac_number >= 0:
            lines.append(f'rac_number = {comp.rac_number}')
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _sudo_write_topology(topo, path: Path) -> bool:
    """Fall back to writing topology.toml via sudo cp from a temp file."""
    lines = [
        "# /etc/sigmond/topology.toml",
        "# Managed by smd tui. Manual edits are fine too.",
        "",
    ]
    for comp_name in sorted(topo.components):
        comp = topo.components[comp_name]
        lines.append(f"[component.{comp_name}]")
        lines.append(f'enabled = {"true" if comp.enabled else "false"}')
        if not comp.managed:
            lines.append("managed = false")
        if comp.version and comp.version != "latest":
            lines.append(f'version = "{comp.version}"')
        if comp.description:
            lines.append(f'description = "{comp.description}"')
        if comp.rac_id:
            lines.append(f'rac_id = "{comp.rac_id}"')
        if comp.rac_number >= 0:
            lines.append(f'rac_number = {comp.rac_number}')
        lines.append("")
    content = "\n".join(lines) + "\n"
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix='.toml')
        os.write(fd, content.encode())
        os.close(fd)
        r = subprocess.run(['sudo', 'cp', tmp, str(path)], capture_output=True)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _apply_version_policy(
    name: str,
    policy: str,
    topo_components: dict,
) -> tuple[bool, str]:
    """Write version policy for one component to topology.toml.

    Also updates topo_components in-place so the caller's cache stays current.
    Returns (success, message).
    """
    from ...paths import TOPOLOGY_PATH
    from ...topology import load_topology

    try:
        topo = load_topology(TOPOLOGY_PATH)
    except Exception as exc:
        return False, f"Error loading topology: {exc}"

    comp = topo.components.get(name)
    if comp is None:
        return False, f"{name} not in topology — policy not saved."
    comp.version = policy

    # Update caller's in-memory cache.
    local_comp = topo_components.get(name)
    if local_comp is not None:
        local_comp.version = policy

    try:
        _write_topology_toml(topo, TOPOLOGY_PATH)
        return True, f"{name}: policy → {policy}  (saved)"
    except PermissionError:
        ok = _sudo_write_topology(topo, TOPOLOGY_PATH)
        if ok:
            return True, f"{name}: policy → {policy}  (saved via sudo)"
        from ...paths import TOPOLOGY_PATH as _TP
        return False, f"Permission denied writing {_TP}"
    except Exception as exc:
        return False, f"Error saving policy: {exc}"


# ---------------------------------------------------------------------------
# ComponentDetailModal
# ---------------------------------------------------------------------------

class ComponentDetailModal(ModalScreen):
    """Per-component detail: git log, version policy controls, and update."""

    BINDINGS = [Binding("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    ComponentDetailModal { align: center middle; }
    ComponentDetailModal > Vertical {
        width: 90%;
        height: 88%;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }
    ComponentDetailModal #cdm-header {
        height: auto;
        margin-bottom: 1;
        border-bottom: solid $primary-background;
        padding-bottom: 1;
    }
    ComponentDetailModal #cdm-title {
        text-style: bold;
        margin-bottom: 0;
    }
    ComponentDetailModal #cdm-scroll {
        height: 1fr;
        border: solid $surface;
        padding: 0 1;
        background: $background;
        margin-bottom: 1;
    }
    ComponentDetailModal #cdm-btn-row {
        height: auto;
        margin-bottom: 1;
    }
    ComponentDetailModal #cdm-btn-row Button {
        margin-right: 1;
    }
    ComponentDetailModal #cdm-status {
        color: $text-muted;
    }
    """

    def __init__(self, row: _ComponentRow, topo_components: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self._row = row
        self._topo_components = topo_components
        self._changed = False

    def compose(self) -> ComposeResult:
        row = self._row
        with Vertical():
            with Vertical(id="cdm-header"):
                yield Static(
                    f"[bold]{row.name}[/]  [dim]({row.kind})[/]",
                    id="cdm-title",
                )
                if row.description:
                    yield Static(f"[dim]{row.description}[/]")
                if row.repo:
                    yield Static(f"[dim]repo:[/] {row.repo}")
                yield Static(
                    f"[dim]ref:[/] {row.current_ref}  "
                    f"[dim]last commit:[/] {row.last_commit_date}  "
                    f"[dim]installed:[/] "
                    + ("[green]yes[/]" if row.installed else "[red]no[/]"),
                )
                yield Static(
                    f"[dim]policy:[/] {self._policy_markup(row.version_policy)}",
                    id="cdm-policy",
                )
            with ScrollableContainer(id="cdm-scroll"):
                yield Static("", id="cdm-log")
            with Horizontal(id="cdm-btn-row"):
                yield Button(
                    "↑ Update this" if row.installed else "+ Install",
                    id="cdm-update",
                    variant="success",
                )
                yield Button("↑ Set: latest",    id="cdm-latest",  variant="success")
                yield Button("⊙ Pin to current", id="cdm-pin",     variant="warning",
                             disabled=(row.current_ref == "—"))
                yield Button("✕ Set: ignore",    id="cdm-ignore",  variant="error")
                yield Button("Close",            id="cdm-close",   variant="default")
            yield Static("", id="cdm-status")

    def on_mount(self) -> None:
        self._render_log()

    def _policy_markup(self, policy: str) -> str:
        if policy == "latest":
            return "[green]latest[/]"
        if policy == "ignore":
            return "[dim]ignore[/]"
        return f"[yellow]pin: {policy}[/]"

    def _render_log(self) -> None:
        row = self._row
        lines: list[str] = []
        if row.ahead_log_lines:
            lines.append(
                f"[yellow]↑ {len(row.ahead_log_lines)} commit(s) on remote, not yet pulled:[/]"
            )
            for entry in row.ahead_log_lines[:20]:
                safe = entry.replace('[', r'\[')
                lines.append(f"  [yellow]{safe}[/]")
            lines.append("[dim]  — click 'Update this' to pull these[/]")
            lines.append("")
        if row.log_lines:
            label = "[dim]local commits:[/]" if row.ahead_log_lines else "[dim]recent commits:[/]"
            lines.append(label)
            for entry in row.log_lines:
                safe = entry.replace('[', r'\[')
                lines.append(f"  [cyan]{safe}[/]")
        elif row.installed and row.repo_dir:
            lines.append("[dim](git history unavailable — may need root)[/]")
        elif not row.installed:
            lines.append("[dim](not installed — no git history)[/]")
        self.query_one("#cdm-log", Static).update("\n".join(lines))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cdm-close":
            self.dismiss(self._changed)
        elif bid == "cdm-update":
            if self._row.installed:
                self._do_update()
            else:
                self._do_install()
        elif bid in ("cdm-latest", "cdm-pin", "cdm-ignore"):
            self._set_policy(bid)

    def _set_policy(self, button_id: str) -> None:
        row = self._row
        if button_id == "cdm-latest":
            new_policy = "latest"
        elif button_id == "cdm-ignore":
            new_policy = "ignore"
        else:  # cdm-pin
            sha = row.current_ref.split("@")[-1] if "@" in row.current_ref else row.current_ref
            new_policy = sha

        ok, msg = _apply_version_policy(row.name, new_policy, self._topo_components)
        status = self.query_one("#cdm-status", Static)
        if ok:
            row.version_policy = new_policy
            self._changed = True
            self.query_one("#cdm-policy", Static).update(
                f"[dim]policy:[/] {self._policy_markup(new_policy)}"
            )
            status.update(f"[green]✔[/]  {msg}")
        else:
            status.update(f"[red]{msg}[/]")

    def _do_install(self) -> None:
        row = self._row
        smd = _smd_binary()

        body_lines = [f"Clone and install [bold]{row.name}[/]"]
        if row.kind:
            body_lines[0] += f" [dim]({row.kind})[/]"
        body_lines[0] += "."
        if row.description:
            body_lines.append("")
            body_lines.append(row.description)
        body_lines.append("")
        body_lines.append(
            "sigmond will dispatch to the right build path: a catalog "
            "install.sh for clients that ship one, or for C projects "
            "(ka9q-radio) a native build that apt-installs the library "
            "deps and then runs `make install`."
        )

        def _after_confirm(confirmed: bool) -> None:
            if not confirmed:
                return

            def _after_modal(_result: object) -> None:
                self._changed = True
                self.query_one("#cdm-status", Static).update(
                    f"[dim]{row.name}: install complete — reopen the screen to refresh.[/]"
                )

            self.app.push_screen(
                UpdateOutputModal(
                    title=f"Installing {row.name}",
                    cmd=[smd, 'install', row.name],
                ),
                _after_modal,
            )

        self.app.push_screen(
            ConfirmModal(
                title=f"Install {row.name}?",
                body="\n".join(body_lines),
                cmd_preview=f"{smd} install {row.name}",
            ),
            _after_confirm,
        )

    def _do_update(self) -> None:
        row = self._row
        if not row.installed:
            self.query_one("#cdm-status", Static).update(
                f"[yellow]{row.name} is not installed — nothing to update.[/]"
            )
            return
        if row.version_policy == "ignore":
            self.query_one("#cdm-status", Static).update(
                f"[yellow]{row.name} has policy=ignore — set policy to 'latest' first.[/]"
            )
            return

        smd = _smd_binary()
        behind_str = (
            f"  ({row.behind} commits behind)"
            if row.behind not in ("—", "0") else ""
        )

        def _after_confirm(confirmed: bool) -> None:
            if not confirmed:
                return

            def _after_modal(_result: object) -> None:
                self._changed = True
                self.query_one("#cdm-status", Static).update(
                    f"[dim]{row.name}: update complete.[/]"
                )

            self.app.push_screen(
                UpdateOutputModal(
                    title=f"Updating {row.name}",
                    cmd=[smd, 'component', 'update', '--components', row.name],
                ),
                _after_modal,
            )

        self.app.push_screen(
            ConfirmModal(
                title=f"Update {row.name}?",
                body=(
                    f"Pull the latest commits for [bold]{row.name}[/] and re-apply.\n\n"
                    f"Current ref: {row.current_ref}{behind_str}"
                ),
                cmd_preview=f"{smd} update --components {row.name}",
            ),
            _after_confirm,
        )

    def action_dismiss_modal(self) -> None:
        self.dismiss(self._changed)


# ---------------------------------------------------------------------------
# ComponentsScreen
# ---------------------------------------------------------------------------

class ComponentsScreen(Vertical):
    """Software versions — component install status, git refs, and version policy."""

    BINDINGS = [
        Binding("space",  "toggle_select", "Toggle selection", show=True),
        Binding("a",      "select_all",    "Select all visible", show=False),
        Binding("n",      "clear_select",  "Clear selection",  show=False),
    ]

    DEFAULT_CSS = """
    ComponentsScreen {
        padding: 1;
    }
    ComponentsScreen .cv-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-table {
        height: 1fr;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-actions {
        height: auto;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-actions Button {
        margin-right: 1;
    }
    ComponentsScreen #cv-filters {
        height: auto;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-filters Button {
        margin-right: 1;
        min-width: 9;
    }
    ComponentsScreen #cv-summary {
        color: $text-muted;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-last {
        color: $text-muted;
    }
    """

    def __init__(self, topology_components: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self._topo_components = topology_components  # name → Component
        self._rows: list[_ComponentRow] = []
        self._last_click_name: Optional[str] = None
        self._last_click_time: float = 0.0
        self._selected: set[str] = set()  # row names currently checked
        self._filter: str = "all"          # all|missing|behind|dirty|ahead

    def compose(self) -> ComposeResult:
        yield Static("Software Versions", classes="cv-title")
        yield Static(
            "Space toggles selection · Enter or double-click opens details · "
            "filters limit the visible rows.",
            id="cv-hint",
        )
        with Horizontal(id="cv-filters"):
            yield Button("All",       id="cv-filter-all",     variant="primary")
            yield Button("Missing",   id="cv-filter-missing", variant="default")
            yield Button("Behind",    id="cv-filter-behind",  variant="default")
            yield Button("Dirty",     id="cv-filter-dirty",   variant="default")
            yield Button("Ahead",     id="cv-filter-ahead",   variant="default")
        yield Static("[dim]fetching from remote…[/]", id="cv-status")
        table = DataTable(id="cv-table", cursor_type="row", zebra_stripes=True)
        table.add_columns(
            "Sel", "Name", "En", "Status",
            "Index", "Behind", "Ahead", "Dirty",
            "Date", "Policy",
        )
        yield table
        yield Static("", id="cv-summary")
        with Horizontal(id="cv-actions"):
            yield Button("+ Install",        id="cv-install",  variant="success")
            yield Button("↑ Update",         id="cv-update",   variant="warning")
            yield Button("⇄ Toggle enable",  id="cv-toggle",   variant="primary")
            yield Button("⟳ Refresh",        id="cv-fetch",    variant="default")
            yield Button("Detail…",          id="cv-detail",   variant="default")
        yield Static("", id="cv-last")

    def on_mount(self) -> None:
        # Auto-fetch on first load so the table shows remote-ahead state immediately.
        self._refresh(do_fetch=True)

    # ------------------------------------------------------------------
    # data loading
    # ------------------------------------------------------------------

    def _refresh(self, do_fetch: bool = False) -> None:
        msg = "[dim]fetching from remote, then loading…[/]" if do_fetch else "[dim]loading…[/]"
        self.query_one("#cv-status", Static).update(msg)
        topo = dict(self._topo_components)
        self.run_worker(
            lambda: _gather(topo, do_fetch=do_fetch),
            thread=True, name="cv-gather",
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "cv-gather":
            return
        if event.state != WorkerState.SUCCESS:
            return
        view = event.worker.result
        if isinstance(view, _ComponentsView):
            self._render_data(view)

    def _render_data(self, view: _ComponentsView) -> None:
        status = self.query_one("#cv-status", Static)
        if view.error:
            status.update(f"[red]{view.error}[/]")
            return

        self._rows = list(view.rows)
        self._render_table()
        self._render_summary()

    def _matches_filter(self, row: _ComponentRow) -> bool:
        f = self._filter
        if f == "all":
            return True
        if f == "missing":
            return row.lifecycle == "missing"
        if f == "behind":
            return _to_int(row.behind) > 0
        if f == "dirty":
            return row.dirty
        if f == "ahead":
            return _to_int(row.ahead) > 0
        return True

    def _render_table(self) -> None:
        """Re-render the DataTable from self._rows + self._filter + self._selected.

        Called whenever the underlying data refreshes, the filter changes,
        or the selection set changes.  Each render is a full clear+repopulate
        — cheap because we have ~10 rows.
        """
        table = self.query_one("#cv-table", DataTable)
        table.clear()
        for row in self._rows:
            if not self._matches_filter(row):
                continue
            # Unicode checkboxes — literal "[x]" / "[ ]" are mis-parsed as
            # Rich markup tags (style="x", "= ") and render blank.
            sel = "[green]☑[/]" if row.name in self._selected else "☐"
            en  = (
                "[green]✓[/]" if row.enabled
                else ("[dim]·[/]" if row.lifecycle != "missing" else "[dim]—[/]")
            )
            status_cell = self._lifecycle_markup(row.lifecycle)
            idx_cell    = row.commit_idx if row.commit_idx != "—" else "[dim]—[/]"
            behind_cell = self._behind_markup(row.behind)
            ahead_cell  = self._ahead_markup(row.ahead)
            dirty_cell  = (
                f"[red]![/] [dim]{row.dirty_reason}[/]"
                if row.dirty
                else ("[dim]·[/]" if row.repo_dir else "[dim]—[/]")
            )
            date_cell   = (
                f"[dim]{row.last_commit_date}[/]"
                if row.last_commit_date == "—"
                else row.last_commit_date
            )
            policy_cell = self._policy_markup(row.version_policy)
            table.add_row(
                sel, row.name, en, status_cell,
                idx_cell, behind_cell, ahead_cell, dirty_cell,
                date_cell, policy_cell,
                key=row.name,
            )

    def _render_summary(self) -> None:
        """Update #cv-status (totals) and #cv-summary (selection breakdown)."""
        rows = self._rows
        n_total    = len(rows)
        n_missing  = sum(1 for r in rows if r.lifecycle == "missing")
        n_behind   = sum(1 for r in rows if _to_int(r.behind) > 0)
        n_dirty    = sum(1 for r in rows if r.dirty)
        n_ahead    = sum(1 for r in rows if _to_int(r.ahead) > 0)
        dormant    = [r for r in rows if r.lifecycle == "dormant"]
        status_line = (
            f"{n_total} components  •  "
            f"[red]{n_missing} missing[/]  •  "
            f"[yellow]{n_behind} behind[/]  •  "
            f"[red]{n_dirty} dirty[/]  •  "
            f"[yellow]{n_ahead} ahead[/]"
        )
        if dormant:
            # Expected on a host missing optional hardware (e.g. no GPSDO / no
            # magnetometer) — informational, not an error.
            detail = ", ".join(f"{r.name} ({r.gated_reason})" for r in dormant)
            status_line += f"  •  [cyan]{len(dormant)} dormant[/] [dim]— hardware absent: {detail}[/]"
        self.query_one("#cv-status", Static).update(status_line)

        sel = self._selected
        if not sel:
            self.query_one("#cv-summary", Static).update(
                "[dim]nothing selected — space toggles the focused row.[/]"
            )
            return

        installable = [r for r in rows if r.name in sel and r.lifecycle == "missing"]
        updatable   = [r for r in rows if r.name in sel and _to_int(r.behind) > 0
                       and not r.dirty]
        skipped     = [r for r in rows if r.name in sel
                       and r not in installable and r not in updatable]
        self.query_one("#cv-summary", Static).update(
            f"selected: {len(sel)}  •  "
            f"[green]{len(installable)} installable[/]  •  "
            f"[yellow]{len(updatable)} updatable[/]  •  "
            f"[dim]{len(skipped)} skipped[/]"
        )

    def _policy_markup(self, policy: str) -> str:
        if policy == "latest":
            return "[green]latest[/]"
        if policy == "ignore":
            return "[dim]ignore[/]"
        return f"[yellow]pin: {policy}[/]"

    def _lifecycle_markup(self, stage: str) -> str:
        if stage == "running":
            return "[green]running[/]"
        if stage == "stopped":
            return "[yellow]stopped[/]"
        if stage == "dormant":
            return "[cyan]dormant[/]"
        if stage == "missing":
            return "[red]missing[/]"
        if stage == "available":
            return "[yellow]available[/]"
        if stage in ("needs cfg", "configured"):
            return f"[yellow]{stage}[/]"
        return f"[dim]{stage}[/]"

    def _behind_markup(self, behind: str) -> str:
        if behind == "—" or behind == "":
            return "[dim]—[/]"
        if behind == "0":
            return "0"
        return f"[yellow]{behind}[/]"

    def _ahead_markup(self, ahead: str) -> str:
        if ahead == "—" or ahead == "":
            return "[dim]—[/]"
        if ahead == "0":
            return "0"
        return f"[yellow]{ahead}[/]"

    # ------------------------------------------------------------------
    # row selection — double-click opens detail modal
    # ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        name = event.row_key.value if hasattr(event.row_key, 'value') else str(event.row_key)
        row = next((r for r in self._rows if r.name == name), None)
        if row is None:
            return

        now = _time.monotonic()
        if name == self._last_click_name and (now - self._last_click_time) < 0.6:
            # Double-click detected — open modal.
            self._last_click_name = None
            self._last_click_time = 0.0
            self._open_detail_modal(row)
        else:
            self._last_click_name = name
            self._last_click_time = now

    def _open_detail_modal(self, row: _ComponentRow) -> None:
        def _after_modal(changed: bool) -> None:
            if changed:
                self._refresh()

        self.app.push_screen(
            ComponentDetailModal(row=row, topo_components=self._topo_components),
            _after_modal,
        )

    # ------------------------------------------------------------------
    # button handlers
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid is None:
            return

        # Filter pills — set self._filter and re-render the table.
        filter_map = {
            "cv-filter-all":     "all",
            "cv-filter-missing": "missing",
            "cv-filter-behind":  "behind",
            "cv-filter-dirty":   "dirty",
            "cv-filter-ahead":   "ahead",
        }
        if bid in filter_map:
            self._filter = filter_map[bid]
            self._update_filter_button_variants()
            self._render_table()
            self._render_summary()
            return

        if bid == "cv-fetch":
            self._refresh(do_fetch=True)
        elif bid == "cv-install":
            self._bulk_install()
        elif bid == "cv-update":
            self._bulk_update()
        elif bid == "cv-toggle":
            self._bulk_toggle_enable()
        elif bid == "cv-detail":
            self._open_focused_detail()

    def _update_filter_button_variants(self) -> None:
        """Highlight the active filter pill, dim the rest."""
        for fid, fname in [
            ("cv-filter-all",     "all"),
            ("cv-filter-missing", "missing"),
            ("cv-filter-behind",  "behind"),
            ("cv-filter-dirty",   "dirty"),
            ("cv-filter-ahead",   "ahead"),
        ]:
            try:
                btn = self.query_one(f"#{fid}", Button)
                btn.variant = "primary" if self._filter == fname else "default"
            except Exception:
                pass

    # ------------------------------------------------------------------
    # selection actions (keybindings)
    # ------------------------------------------------------------------

    def _visible_rows(self) -> list[_ComponentRow]:
        return [r for r in self._rows if self._matches_filter(r)]

    def action_toggle_select(self) -> None:
        table = self.query_one("#cv-table", DataTable)
        cursor = table.cursor_row
        visible = self._visible_rows()
        if cursor is None or cursor < 0 or cursor >= len(visible):
            return
        name = visible[cursor].name
        if name in self._selected:
            self._selected.discard(name)
        else:
            self._selected.add(name)
        self._render_table()
        self._render_summary()
        # Restore cursor after clear+repopulate.
        try:
            table.move_cursor(row=cursor)
        except Exception:
            pass

    def action_select_all(self) -> None:
        self._selected.update(r.name for r in self._visible_rows())
        self._render_table()
        self._render_summary()

    def action_clear_select(self) -> None:
        self._selected.clear()
        self._render_table()
        self._render_summary()

    # ------------------------------------------------------------------
    # bulk actions (button handlers)
    # ------------------------------------------------------------------

    def _bulk_install(self) -> None:
        """Install all selected rows whose lifecycle == "missing"."""
        targets = [r for r in self._rows
                   if r.name in self._selected and r.lifecycle == "missing"]
        if not targets:
            self.query_one("#cv-last", Static).update(
                "[yellow]no missing components in selection — nothing to install.[/]"
            )
            return

        smd = _smd_binary()
        names = [r.name for r in targets]
        names_csv = ','.join(names)
        cmd = [smd, 'install', '--components', names_csv, '--yes']
        skipped = len(self._selected) - len(targets)
        skipped_note = (
            f"\n\n[dim]{skipped} other selected row(s) skipped — already installed.[/]"
            if skipped else ""
        )

        def _after_confirm(confirmed: bool) -> None:
            if not confirmed:
                return

            def _after_modal(_result: object) -> None:
                self.query_one("#cv-last", Static).update(
                    f"[dim]install of {len(targets)} component(s) complete — refreshing…[/]"
                )
                self._refresh()

            self.app.push_screen(
                UpdateOutputModal(
                    title=f"Installing {len(targets)} component(s)",
                    cmd=cmd,
                ),
                _after_modal,
            )

        self.app.push_screen(
            ConfirmModal(
                title=f"Install {len(targets)} component(s)?",
                body=(
                    f"Will install: [bold]{', '.join(names)}[/]"
                    f"{skipped_note}"
                ),
                cmd_preview=' '.join(cmd),
            ),
            _after_confirm,
        )

    def _bulk_update(self) -> None:
        """Update all selected rows that are behind upstream and not dirty.

        Dirty rows are deliberately skipped: pulling on top of uncommitted
        local changes is a footgun.  The user can stash/commit first, or
        use the per-component detail modal to force-update one at a time.
        """
        targets = [r for r in self._rows
                   if r.name in self._selected
                   and _to_int(r.behind) > 0
                   and not r.dirty]
        if not targets:
            self.query_one("#cv-last", Static).update(
                "[yellow]no clean+behind components in selection — nothing to update.[/]"
            )
            return

        smd = _smd_binary()
        names = [r.name for r in targets]
        names_csv = ','.join(names)
        cmd = [smd, 'component', 'update', '--components', names_csv]
        skipped = len(self._selected) - len(targets)
        skipped_note = (
            f"\n\n[dim]{skipped} other selected row(s) skipped (dirty / up-to-date / missing).[/]"
            if skipped else ""
        )

        def _after_confirm(confirmed: bool) -> None:
            if not confirmed:
                return

            def _after_modal(_result: object) -> None:
                self.query_one("#cv-last", Static).update(
                    f"[dim]update of {len(targets)} component(s) complete — refreshing…[/]"
                )
                self._refresh()

            self.app.push_screen(
                UpdateOutputModal(
                    title=f"Updating {len(targets)} component(s)",
                    cmd=cmd,
                ),
                _after_modal,
            )

        self.app.push_screen(
            ConfirmModal(
                title=f"Update {len(targets)} component(s)?",
                body=(
                    f"Will pull and re-apply: [bold]{', '.join(names)}[/]"
                    f"{skipped_note}"
                ),
                cmd_preview=' '.join(cmd),
            ),
            _after_confirm,
        )

    def _bulk_toggle_enable(self) -> None:
        """Flip the [component.X] enabled flag in topology.toml for each selected row.

        Writes via the existing _write_topology_toml / _sudo_write_topology
        path so failures fall back to a sudo cp.  Refreshes the data layer
        after so the EN/STATUS columns reflect the new state.
        """
        if not self._selected:
            self.query_one("#cv-last", Static).update(
                "[yellow]no rows selected — nothing to toggle.[/]"
            )
            return

        from ...paths import TOPOLOGY_PATH
        from ...topology import load_topology

        try:
            topo = load_topology(TOPOLOGY_PATH)
        except Exception as exc:
            self.query_one("#cv-last", Static).update(
                f"[red]error loading topology: {exc}[/]"
            )
            return

        flipped: list[str] = []
        skipped: list[str] = []
        for name in sorted(self._selected):
            comp = topo.components.get(name)
            if comp is None:
                skipped.append(name)
                continue
            comp.enabled = not comp.enabled
            flipped.append(f"{name}={'on' if comp.enabled else 'off'}")
            local = self._topo_components.get(name)
            if local is not None:
                local.enabled = comp.enabled

        if not flipped:
            self.query_one("#cv-last", Static).update(
                f"[yellow]no selected rows are in topology.toml: {', '.join(skipped)}[/]"
            )
            return

        try:
            _write_topology_toml(topo, TOPOLOGY_PATH)
            via = "saved"
        except PermissionError:
            if not _sudo_write_topology(topo, TOPOLOGY_PATH):
                self.query_one("#cv-last", Static).update(
                    f"[red]permission denied writing {TOPOLOGY_PATH}[/]"
                )
                return
            via = "saved via sudo"
        except Exception as exc:
            self.query_one("#cv-last", Static).update(
                f"[red]error saving topology: {exc}[/]"
            )
            return

        self.query_one("#cv-last", Static).update(
            f"[green]✔[/] toggled: {', '.join(flipped)}  ({via})"
        )
        self._refresh()

    def _open_focused_detail(self) -> None:
        """Open the per-component detail modal for the row under the cursor."""
        table = self.query_one("#cv-table", DataTable)
        cursor = table.cursor_row
        visible = self._visible_rows()
        if cursor is None or cursor < 0 or cursor >= len(visible):
            self.query_one("#cv-last", Static).update(
                "[yellow]move the cursor to a row first.[/]"
            )
            return
        self._open_detail_modal(visible[cursor])
