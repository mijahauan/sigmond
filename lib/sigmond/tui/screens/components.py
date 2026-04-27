"""Software Versions screen — catalog components, install state, git refs, version policy.

Shows every catalog entry with:
  - install status (present at /opt/git/<name>)
  - current HEAD ref (git branch@sha)
  - version policy from topology.toml (latest / pinned ref / ignore)

Selecting a row reveals a detail panel with the recent git log for that
component.  Three buttons let the operator change the version policy for the
selected component and save it back to topology.toml.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState

from ..mutation import confirm_and_run


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
    """
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


def _git_log(repo_dir: Path, n: int = 15) -> list[str]:
    """Return the last n log entries prefixed with their sequential commit index."""
    try:
        r_count = _git(repo_dir, 'rev-list', '--count', 'HEAD')
        total = (int(r_count.stdout.strip())
                 if r_count.returncode == 0 and r_count.stdout.strip().isdigit()
                 else 0)
        r = _git(repo_dir, 'log', f'-{n}', '--format=%h %s (%as)', timeout=8)
        if r.returncode != 0:
            return []
        result = []
        for i, line in enumerate(r.stdout.splitlines()):
            if not line.strip():
                continue
            idx = total - i
            parts = line.split(' ', 1)
            sha  = parts[0]
            rest = parts[1] if len(parts) > 1 else ''
            result.append(f"#{idx:<5} {sha}  {rest}")
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
                 '--format=%h %s (%as)', timeout=8)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        result = []
        for i, line in enumerate(r.stdout.splitlines()):
            if not line.strip():
                continue
            idx = total - i
            parts = line.split(' ', 1)
            sha  = parts[0]
            rest = parts[1] if len(parts) > 1 else ''
            result.append(f"#{idx:<5} {sha}  {rest}")
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

        current_ref    = _git_ref(repo_dir)        if repo_dir else '—'
        commit_idx     = _git_commit_idx(repo_dir) if repo_dir else '—'
        behind         = _git_behind(repo_dir)     if repo_dir else '—'
        log_lines      = _git_log(repo_dir)        if repo_dir else []
        ahead_log_lines = _git_log_ahead(repo_dir) if repo_dir else []

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
            log_lines=log_lines,
            ahead_log_lines=ahead_log_lines,
        ))

    return view


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
        height: 14;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-detail {
        height: auto;
        min-height: 6;
        border: solid $primary-background;
        padding: 1;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-actions {
        height: auto;
        margin-bottom: 1;
    }
    ComponentsScreen #cv-actions Button {
        margin-right: 1;
    }
    ComponentsScreen #cv-refresh {
        border: tall $primary;
        color: $primary-lighten-2;
        background: $panel;
    }
    ComponentsScreen #cv-latest {
        border: tall $success;
        color: $success-lighten-2;
        background: $panel;
    }
    ComponentsScreen #cv-last {
        color: $text-muted;
    }
    """

    def __init__(self, topology_components: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self._topo_components = topology_components  # name → Component
        self._rows: list[_ComponentRow] = []
        self._detail_name: Optional[str] = None   # name of row shown in detail panel

    def compose(self):
        yield Static("Software Versions", classes="cv-title")
        yield Static(
            "Select a row to see its git history. "
            "Use the buttons below to set the version policy for that component.",
            id="cv-hint",
        )
        yield Static("[dim]loading…[/]", id="cv-status")
        table = DataTable(id="cv-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Name", "Kind", "Installed", "Current Ref", "Commit #", "Policy")
        yield table
        yield Static("(select a row to see git history)", id="cv-detail")
        with Horizontal(id="cv-actions"):
            yield Button("↑ Update now",           id="cv-update",  variant="success")
            yield Button("⊙ Pin to current",       id="cv-pin",     variant="warning")
            yield Button("⟳ Fetch + Refresh",      id="cv-fetch",   variant="primary")
            yield Button("⟳ Refresh (local)",      id="cv-refresh", variant="default")
            yield Button("↑ Set policy: latest",   id="cv-latest",  variant="default")
            yield Button("✕ Set policy: ignore",   id="cv-ignore",  variant="error")
        yield Static("", id="cv-last")

    def on_mount(self) -> None:
        self._refresh()

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
            inst_cell = ("[green]✔[/]" if row.installed else "[dim]✘[/]")
            policy_cell = self._policy_markup(row.version_policy)
            ref_cell = Text(row.current_ref, no_wrap=True)
            idx_cell = self._commit_idx_markup(row.commit_idx, row.behind)
            table.add_row(
                row.name, row.kind, inst_cell,
                ref_cell, idx_cell, policy_cell,
                key=row.name,
            )

    def _policy_markup(self, policy: str) -> str:
        if policy == "latest":
            return "[green]latest[/]"
        if policy == "ignore":
            return "[dim]ignore[/]"
        return f"[yellow]pin: {policy}[/]"

    def _commit_idx_markup(self, idx: str, behind: str) -> str:
        if idx == "—":
            return "[dim]—[/]"
        if behind == "—" or behind == "0":
            behind_tag = "[green]✔[/]" if behind == "0" else ""
            return f"#{idx}  {behind_tag}".strip()
        try:
            n = int(behind)
            return f"#{idx}  [yellow]+{n} behind[/]"
        except ValueError:
            return f"#{idx}"

    # ------------------------------------------------------------------
    # row selection → detail panel
    # ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        name = event.row_key.value if hasattr(event.row_key, 'value') else str(event.row_key)
        row = next((r for r in self._rows if r.name == name), None)
        if row is None:
            return
        self._detail_name = name
        self._show_detail(row)

    def _show_detail(self, row: _ComponentRow) -> None:
        detail = self.query_one("#cv-detail", Static)
        lines: list[str] = []
        lines.append(f"[bold]{row.name}[/]  ({row.kind})")
        lines.append(f"[dim]{row.description}[/]")
        if row.repo:
            lines.append(f"[dim]repo:[/] {row.repo}")
        lines.append(f"[dim]installed:[/] {'yes' if row.installed else 'no'}")
        lines.append(f"[dim]current ref:[/] {row.current_ref}")
        lines.append(f"[dim]version policy:[/] {self._policy_markup(row.version_policy)}")
        if row.ahead_log_lines:
            lines.append("")
            lines.append(f"[yellow]↑ on remote, not yet pulled ({len(row.ahead_log_lines)}):[/]")
            for log_entry in row.ahead_log_lines[:15]:
                safe = log_entry.replace('[', r'\[')
                lines.append(f"  [yellow]{safe}[/]")
            lines.append("[dim]  — run 'Update now' to pull these[/]")
        if row.log_lines:
            lines.append("")
            label = "[dim]local commits:[/]" if row.ahead_log_lines else "[dim]recent commits:[/]"
            lines.append(label)
            for log_entry in row.log_lines[:10]:
                safe = log_entry.replace('[', r'\[')
                lines.append(f"  [cyan]{safe}[/]")
        elif row.installed and row.repo_dir:
            lines.append("[dim](git history unavailable — may need root)[/]")
        elif not row.installed:
            lines.append("[dim](not installed — no git history)[/]")
        detail.update("\n".join(lines))

    # ------------------------------------------------------------------
    # version policy buttons
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cv-refresh":
            self._refresh()
        elif bid == "cv-fetch":
            self._refresh(do_fetch=True)
        elif bid == "cv-update":
            self._update_selected()
        elif bid in ("cv-latest", "cv-pin", "cv-ignore"):
            self._set_policy(bid)

    def _update_selected(self) -> None:
        row = self._selected_row()
        last = self.query_one("#cv-last", Static)
        if row is None:
            last.update("[yellow]Select a component row first.[/]")
            return
        if not row.installed:
            last.update(f"[yellow]{row.name} is not installed — nothing to update.[/]")
            return
        if row.version_policy == "ignore":
            last.update(f"[yellow]{row.name} has policy=ignore — set policy to 'latest' first.[/]")
            return

        smd = _smd_binary()

        def _after_update(result) -> None:
            if result is None or result.returncode != 0:
                last.update(
                    f"[yellow]⚠ {row.name}: update finished with errors — "
                    f"scroll up in your terminal (outside the TUI) to read the output.[/]"
                )
            else:
                last.update(f"[green]✔ {row.name} updated.[/]")
            self._refresh()

        behind_str = f"  ({row.behind} commits behind)" if row.behind not in ("—", "0") else ""
        confirm_and_run(
            self.app,
            title=f"Update {row.name}?",
            body=(
                f"Pull the latest commits for [bold]{row.name}[/] and re-apply.\n\n"
                f"Current ref: {row.current_ref}{behind_str}"
            ),
            cmd=[smd, 'update', '--components', row.name],
            sudo=True,
            on_complete=_after_update,
        )

    def _selected_row(self) -> Optional[_ComponentRow]:
        # The detail panel tracks the last explicitly selected row.
        # cursor_row resets to 0 after every table.clear(), so it can't be
        # used reliably — the detail panel is the authoritative "selection".
        if self._detail_name:
            row = next((r for r in self._rows if r.name == self._detail_name), None)
            if row is not None:
                return row
        # Fall back to cursor position if nothing has been shown yet.
        table = self.query_one("#cv-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        idx = table.cursor_row
        if not 0 <= idx < len(self._rows):
            return None
        return self._rows[idx]

    def _set_policy(self, button_id: str) -> None:
        row = self._selected_row()
        if row is None:
            self.query_one("#cv-last", Static).update(
                "[yellow]Select a component row first.[/]")
            return

        if button_id == "cv-latest":
            new_policy = "latest"
        elif button_id == "cv-ignore":
            new_policy = "ignore"
        else:  # cv-pin
            if row.current_ref == "—":
                self.query_one("#cv-last", Static).update(
                    "[yellow]Cannot pin — component not installed or git ref unavailable.[/]")
                return
            # Strip branch prefix if present (main@abc1234 → abc1234)
            sha = row.current_ref.split("@")[-1] if "@" in row.current_ref else row.current_ref
            new_policy = sha

        # Write to topology.toml
        self._write_version_policy(row.name, new_policy)

    def _write_version_policy(self, name: str, policy: str) -> None:
        """Persist the version policy for one component to topology.toml."""
        from ...paths import TOPOLOGY_PATH
        from ...topology import load_topology

        last = self.query_one("#cv-last", Static)
        try:
            # Reload from disk so we don't overwrite concurrent changes.
            topo = load_topology(TOPOLOGY_PATH)
            comp = topo.components.get(name)
            if comp is None:
                last.update(f"[yellow]{name} not in topology — policy not saved.[/]")
                return
            comp.version = policy
            # Re-use the topology screen's writer via the same logic.
            self._write_topology_toml(topo, TOPOLOGY_PATH)
            # Update our local cache so the table refreshes without a full reload.
            local_comp = self._topo_components.get(name)
            if local_comp is not None:
                local_comp.version = policy
            row = next((r for r in self._rows if r.name == name), None)
            if row is not None:
                row.version_policy = policy
                table = self.query_one("#cv-table", DataTable)
                try:
                    table.update_cell(name, table.columns[4].key,
                                      self._policy_markup(policy))
                except Exception:
                    pass
            last.update(
                f"[green]✔[/]  {name}: version policy set to "
                f"[bold]{policy}[/]  (saved to {TOPOLOGY_PATH})"
            )
        except PermissionError:
            ok = self._sudo_write_topology(topo, TOPOLOGY_PATH)
            if ok:
                last.update(
                    f"[green]✔[/]  {name}: version policy set to "
                    f"[bold]{policy}[/]  (saved via sudo to {TOPOLOGY_PATH})"
                )
            else:
                last.update(
                    f"[red]Permission denied writing {TOPOLOGY_PATH}.[/]  "
                    f"Fix with: [bold]sudo chown $(whoami) {TOPOLOGY_PATH}[/]"
                )
        except Exception as exc:
            last.update(f"[red]Error saving policy: {exc}[/]")

    def _write_topology_toml(self, topo, path: Path) -> None:
        """Minimal topology.toml writer (mirrors topology screen's writer)."""
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

    def _sudo_write_topology(self, topo, path: Path) -> bool:
        """Fall back to writing topology.toml via sudo tee (no TUI suspend needed)."""
        import os, tempfile, subprocess as sp
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
            r = sp.run(['sudo', 'cp', tmp, str(path)], capture_output=True)
            return r.returncode == 0
        except Exception:
            return False
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
