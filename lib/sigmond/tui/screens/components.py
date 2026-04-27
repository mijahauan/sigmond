"""Software Versions screen — catalog components, install state, git refs, version policy.

Shows every catalog entry with:
  - install status (present at /opt/git/<name>)
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
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState

from ..mutation import ConfirmModal, UpdateOutputModal


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    return shutil.which('smd') or '/usr/local/sbin/smd'


_OPT_GIT = Path('/opt/git')


def _find_repo_dir(name: str, repo_url: str) -> Optional[Path]:
    """Return the cloned repo path for a catalog entry.

    Checks /opt/git/<name> first.  Falls back to /opt/git/<url-stem> so that
    entries like 'radiod' (repo at /opt/git/ka9q-radio) are handled correctly.
    Sigmond itself lives next to this file, not under /opt/git.
    """
    # Sigmond manages itself — its repo is the parent of this library.
    # components.py lives at <repo>/lib/sigmond/tui/screens/components.py,
    # so parents[4] is the repo root.
    if name == 'sigmond':
        self_dir = Path(__file__).resolve().parents[4]
        if (self_dir / '.git').exists():
            return self_dir

    primary = _OPT_GIT / name
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
    commit_idx: str = "—"   # total commit count, e.g. "247"
    behind: str = "—"       # commits behind origin/main, e.g. "3" or "0"
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


def _gather(topology_components: dict, do_fetch: bool = False) -> _ComponentsView:
    """Worker: load catalog + topology, scan /opt/git, collect git refs."""
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
        log_lines       = _git_log(repo_dir)             if repo_dir else []
        ahead_log_lines = _git_log_ahead(repo_dir)       if repo_dir else []
        last_commit_date, last_commit_ts = (
            _git_last_commit_date(repo_dir) if repo_dir else ('—', 0.0)
        )

        comp = topology_components.get(name)
        policy = (comp.version if comp else 'latest') or 'latest'

        view.rows.append(_ComponentRow(
            name=name,
            kind=entry.kind,
            description=entry.description,
            repo=entry.repo,
            installed=installed,
            repo_dir=repo_dir,
            current_ref=current_ref,
            version_policy=policy,
            commit_idx=commit_idx,
            behind=behind,
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
                    "↑ Update this",
                    id="cdm-update",
                    variant="success",
                    disabled=not row.installed,
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
            self._do_update()
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
                    cmd=['sudo', smd, 'update', '--components', row.name],
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
                cmd_preview=f"sudo {smd} update --components {row.name}",
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

    def compose(self) -> ComposeResult:
        yield Static("Software Versions", classes="cv-title")
        yield Static(
            "Double-click a row to view details, set policy, or update that component.",
            id="cv-hint",
        )
        yield Static("[dim]fetching from remote…[/]", id="cv-status")
        table = DataTable(id="cv-table", cursor_type="row", zebra_stripes=True)
        table.add_columns(
            "Name", "Kind", "Installed", "Current Ref", "Commit #", "Sync", "Last Commit", "Policy"
        )
        yield table
        with Horizontal(id="cv-actions"):
            yield Button("⟳ Fetch + Refresh", id="cv-fetch",      variant="success")
            yield Button("↑ Update All Now",  id="cv-update-all", variant="warning")
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

        installed_count = sum(1 for r in view.rows if r.installed)
        status.update(
            f"{len(view.rows)} components  •  "
            f"[green]{installed_count} installed[/]  •  "
            f"{len(view.rows) - installed_count} not installed"
        )

        table = self.query_one("#cv-table", DataTable)
        table.clear()
        self._rows = list(view.rows)

        for row in view.rows:
            inst_cell   = ("[green]✔[/]" if row.installed else "[dim]✘[/]")
            policy_cell = self._policy_markup(row.version_policy)
            ref_cell    = Text(row.current_ref, no_wrap=True)
            idx_cell    = f"#{row.commit_idx}" if row.commit_idx != "—" else "[dim]—[/]"
            sync_cell   = self._sync_markup(row.commit_idx, row.behind)
            date_cell   = (
                f"[dim]{row.last_commit_date}[/]"
                if row.last_commit_date == "—"
                else row.last_commit_date
            )
            table.add_row(
                row.name, row.kind, inst_cell,
                ref_cell, idx_cell, sync_cell, date_cell, policy_cell,
                key=row.name,
            )

    def _policy_markup(self, policy: str) -> str:
        if policy == "latest":
            return "[green]latest[/]"
        if policy == "ignore":
            return "[dim]ignore[/]"
        return f"[yellow]pin: {policy}[/]"

    def _sync_markup(self, idx: str, behind: str) -> str:
        """Return the sync-status cell: ✔, +N behind, or blank."""
        if idx == "—":
            return "[dim]—[/]"
        if behind == "0":
            return "[green]✔[/]"
        if behind == "—":
            return ""
        try:
            n = int(behind)
            return f"[yellow]+{n} behind[/]"
        except ValueError:
            return ""

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
        if bid == "cv-fetch":
            self._refresh(do_fetch=True)
        elif bid == "cv-update-all":
            self._update_all()

    def _update_all(self) -> None:
        smd = _smd_binary()

        def _after_confirm(confirmed: bool) -> None:
            if not confirmed:
                return

            def _after_modal(_result: object) -> None:
                self.query_one("#cv-last", Static).update(
                    "[dim]update complete — refreshing…[/]"
                )
                self._refresh()

            self.app.push_screen(
                UpdateOutputModal(
                    title="Update All Components",
                    cmd=['sudo', smd, 'update'],
                ),
                _after_modal,
            )

        self.app.push_screen(
            ConfirmModal(
                title="Update All Components?",
                body=(
                    "Pull the latest commits for all managed components and re-apply.\n\n"
                    "Components with policy=[bold]ignore[/] will be skipped."
                ),
                cmd_preview=f"sudo {smd} update",
            ),
            _after_confirm,
        )
