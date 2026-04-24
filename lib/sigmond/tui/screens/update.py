"""Update screen — pull latest code for every installed catalog component.

Replaces the old single-button screen with a live table of all installed
components showing their current git ref and version policy, plus:

  • Update selected — run `smd update --components <name>` for one component
  • Update all      — run `smd update` (respects version policies: skips
                      'ignore' components and checks out pinned refs)
  • Dry run         — run `smd update --dry-run` to preview what would change
  • Refresh         — re-scan /opt/git/ for current refs

The CLI does all the real work; the TUI provides visibility, confirmation
gate, and exit-code readout.
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


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


@dataclass
class _ComponentStatus:
    name: str
    kind: str
    installed: bool
    repo_dir: Optional[Path]
    current_ref: str   # e.g. "main@abc1234" or "—"
    version_policy: str
    last_subject: str  # first line of last commit message, or ""


@dataclass
class _UpdateView:
    rows: list[_ComponentStatus] = field(default_factory=list)
    error: Optional[str] = None


def _git_ref(repo_dir: Path) -> str:
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


def _git_last_subject(repo_dir: Path) -> str:
    try:
        r = subprocess.run(
            ['git', '-c', f'safe.directory={repo_dir}',
             '-C', str(repo_dir), 'log', '-1', '--format=%s'],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()[:72] if r.returncode == 0 else ""
    except Exception:
        return ""


def _gather(topology_components: dict) -> _UpdateView:
    """Worker: scan catalog + /opt/git for current state."""
    view = _UpdateView()
    try:
        from ...catalog import load_catalog
        catalog = load_catalog()
    except FileNotFoundError as exc:
        view.error = f"catalog not found: {exc}"
        return view
    except Exception as exc:
        view.error = str(exc)
        return view

    for name in sorted(catalog):
        entry = catalog[name]
        if not entry.repo:
            continue  # no git repo; skip
        repo_dir = _find_repo_dir(name, entry.repo)
        installed = repo_dir is not None or entry.is_installed()

        current_ref  = _git_ref(repo_dir)  if repo_dir else '—'
        last_subject = _git_last_subject(repo_dir) if repo_dir else ''

        comp = topology_components.get(name)
        policy = (comp.version if comp else 'latest') or 'latest'

        view.rows.append(_ComponentStatus(
            name=name,
            kind=entry.kind,
            installed=installed,
            repo_dir=repo_dir,
            current_ref=current_ref,
            version_policy=policy,
            last_subject=last_subject,
        ))

    return view


class UpdateScreen(Vertical):
    """Pull the latest code for all installed catalog components."""

    DEFAULT_CSS = """
    UpdateScreen {
        padding: 1;
    }
    UpdateScreen .up-title {
        text-style: bold;
        margin-bottom: 1;
    }
    UpdateScreen #up-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    UpdateScreen #up-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    UpdateScreen #up-table {
        height: 14;
        margin-bottom: 1;
    }
    UpdateScreen #up-actions {
        height: auto;
        margin-bottom: 1;
    }
    UpdateScreen #up-actions Button {
        margin-right: 1;
    }
    UpdateScreen #up-last {
        color: $text-muted;
    }
    """

    def __init__(self, topology_components: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self._topo_components = topology_components
        self._rows: list[_ComponentStatus] = []

    def compose(self):
        yield Static("Software Update — pull latest code", classes="up-title")
        yield Static(
            "Select a component and press 'Update selected', or "
            "'Update all' to pull every component that isn't set to ignore.\n"
            "Version policies are set under Configure → Software versions.",
            id="up-hint",
        )
        yield Static("[dim]loading…[/]", id="up-status")
        table = DataTable(id="up-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Name", "Kind", "Current Ref", "Policy", "Last commit")
        yield table
        with Horizontal(id="up-actions"):
            yield Button("↑ Update selected", id="up-one",     variant="primary")
            yield Button("↑ Update all",      id="up-all",     variant="warning")
            yield Button("⤓ Dry run",         id="up-dry",     variant="default")
            yield Button("⟳ Refresh",         id="up-refresh", variant="default")
        yield Static("", id="up-last")

    def on_mount(self) -> None:
        self._refresh_data()

    # ------------------------------------------------------------------
    # data loading
    # ------------------------------------------------------------------

    def _refresh_data(self) -> None:
        self.query_one("#up-status", Static).update("[dim]scanning…[/]")
        topo = dict(self._topo_components)
        self.run_worker(lambda: _gather(topo), thread=True, name="up-gather")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "up-gather":
            return
        if event.state != WorkerState.SUCCESS:
            return
        view = event.worker.result
        if isinstance(view, _UpdateView):
            self._render_data(view)

    def _render_data(self, view: _UpdateView) -> None:
        try:
            self._render_data_inner(view)
        except Exception as exc:
            try:
                self.query_one("#up-last", Static).update(
                    f"[red]render error: {exc}[/]"
                )
            except Exception:
                pass

    def _render_data_inner(self, view: _UpdateView) -> None:
        status = self.query_one("#up-status", Static)
        if view.error:
            status.update(f"[red]error[/]  {view.error}")
            return

        installed = sum(1 for r in view.rows if r.installed)
        ignored   = sum(1 for r in view.rows if r.version_policy == 'ignore')
        status.update(
            f"{len(view.rows)} components  •  "
            f"[green]{installed} installed[/]  •  "
            f"[dim]{ignored} ignored[/]"
        )

        table = self.query_one("#up-table", DataTable)
        table.clear()
        self._rows = list(view.rows)

        for row in view.rows:
            policy_cell = self._policy_markup(row.version_policy)
            # Use Text objects for user-generated content to avoid Rich markup
            # parsing issues (e.g. commit subjects containing [...] fragments).
            if row.installed:
                ref_cell = Text(row.current_ref, no_wrap=True)
                if row.last_subject:
                    subject_cell = Text(row.last_subject[:55], no_wrap=True)
                else:
                    subject_cell = Text.from_markup("[dim]—[/]")
            else:
                ref_cell = Text("(not installed)", no_wrap=True, style="dim")
                subject_cell = Text("", no_wrap=True)
            table.add_row(
                row.name, row.kind, ref_cell, policy_cell, subject_cell,
                key=row.name,
            )

    def _policy_markup(self, policy: str) -> str:
        if policy == "latest":
            return "[green]latest[/]"
        if policy == "ignore":
            return "[dim]ignore[/]"
        return f"[yellow]pin:{policy[:12]}[/]"

    # ------------------------------------------------------------------
    # button actions
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "up-refresh":
            self._refresh_data()
        elif bid == "up-one":
            self._update_selected()
        elif bid == "up-all":
            self._update_all()
        elif bid == "up-dry":
            self._dry_run()

    def _selected_row(self) -> Optional[_ComponentStatus]:
        table = self.query_one("#up-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        idx = table.cursor_row
        if not 0 <= idx < len(self._rows):
            return None
        return self._rows[idx]

    def _update_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            self.query_one("#up-last", Static).update(
                "[yellow]Select a component row first.[/]")
            return
        if not row.installed:
            self.query_one("#up-last", Static).update(
                f"[yellow]{row.name} is not installed — "
                f"install it first with: [bold]sudo smd install {row.name}[/]")
            return
        if row.version_policy == 'ignore':
            self.query_one("#up-last", Static).update(
                f"[yellow]{row.name} is set to 'ignore' — "
                "change its policy under Configure → Software versions.[/]")
            return

        smd = _smd_binary()
        cmd = [smd, 'update', '--components', row.name]
        confirm_and_run(
            self.app,
            title=f"Update {row.name}?",
            body=(
                f"Pull the latest code for [bold]{row.name}[/] "
                f"(currently [dim]{row.current_ref}[/]).\n\n"
                f"Policy: [bold]{row.version_policy}[/]\n\n"
                "Running services may restart if unit files changed."
            ),
            cmd=cmd, sudo=True,
            on_complete=self._after_update,
        )

    def _update_all(self) -> None:
        ignored = [r.name for r in self._rows if r.version_policy == 'ignore']
        not_installed = [r.name for r in self._rows if not r.installed]
        smd = _smd_binary()
        cmd = [smd, 'update']
        body_parts = [
            "Pull the latest code for every installed component "
            "whose version policy is not 'ignore'.",
        ]
        if ignored:
            body_parts.append(f"\nSkipping (ignored): {', '.join(ignored)}")
        if not_installed:
            body_parts.append(f"\nNot installed (will skip): {', '.join(not_installed)}")
        body_parts.append("\nRunning services may restart if unit files changed.")
        confirm_and_run(
            self.app,
            title="Update all components?",
            body="\n".join(body_parts),
            cmd=cmd, sudo=True,
            on_complete=self._after_update,
        )

    def _dry_run(self) -> None:
        """Run `smd update --dry-run` and show output in a suspended terminal."""
        smd = _smd_binary()
        cmd = [smd, 'update', '--dry-run']
        confirm_and_run(
            self.app,
            title="Dry-run update?",
            body=(
                "Shows what `smd update` would do without making changes.\n\n"
                "No git pulls, no service restarts."
            ),
            cmd=cmd, sudo=False,
            on_complete=self._after_dry_run,
        )

    def _after_update(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#up-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ exit 0[/]  {argv}")
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]  {argv}")
        self._refresh_data()

    def _after_dry_run(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#up-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ dry-run complete (exit 0)[/]  {argv}")
        else:
            last.update(f"[yellow]dry-run exit {result.returncode}[/]  {argv}")
