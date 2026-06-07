"""Install screen — TUI counterpart to `smd install`.

Catalog browser with install status per entry.  Row cursor picks a
target; 'Install selected' or 'Install all missing' shells out to the
CLI with confirmation + suspend/sudo (same pattern as Lifecycle).

The CLI does the real work — clone to /opt/git/sigmond/<name>, run the
client's canonical install.sh — so the TUI stays thin.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState

from ..mutation import confirm_and_run
from ...component_state import (
    ComponentState, compute_state, applicable_stages, stage_progress,
    _read_deploy_toml,
)

# Command that ADVANCES a component to reach the named next stage.
_CMD_TO_REACH = {
    "downloaded": "smd install {n}",
    "installed":  "smd install {n}",
    "configured": "smd config init {n}",
    "enabled":    "smd enable {n}",
    "running":    "smd start {n}",
}


def _entry_progress(entry) -> dict:
    """Per-component lifecycle track + current position.  The track length
    varies by capability: libraries/tools stop at 'installed'; service infra
    with no config skips 'configured'; clients ride the full pipeline."""
    name = entry.name
    deploy = _read_deploy_toml(name)
    track = applicable_stages(name, deploy, entry.kind)
    if entry.kind == "library":
        import importlib.util
        cloned = os.path.lexists(f"/opt/git/sigmond/{name}")
        mod = name.removesuffix("-python").replace("-", "_")
        try:
            importable = importlib.util.find_spec(mod) is not None
        except Exception:
            importable = False
        # A cloned source dep is ready (installed) — consumers editable-install
        # from it; importability in sigmond's own venv is a bonus, not required.
        state = ComponentState(name=name, cloned=cloned,
                               installed=cloned or importable,
                               configured=False, enabled=False, active=False)
    else:
        state = compute_state(name, None,
                              alias=getattr(entry, "topology_alias", None))
    pos, reached, nxt = stage_progress(state, track)
    return {"reached": reached, "next": nxt, "pos": pos,
            "total": len(track), "is_lib": entry.kind == "library"}


def _entry_stage(entry) -> tuple:
    """(reached_stage, is_library) \u2014 used by summary / missing / guard."""
    pr = _entry_progress(entry)
    return (pr["reached"], pr["is_lib"])


def _stage_cell(entry) -> str:
    """Rich progress cell: a bar sized to THIS component's track, filled to its
    current stage, with the next command (or a check when complete)."""
    pr = _entry_progress(entry)
    pos, total, reached, nxt = pr["pos"], pr["total"], pr["reached"], pr["next"]
    bar = "\u25cf" * (pos + 1) + "\u25cb" * (total - pos - 1)
    if nxt:
        cmd = _CMD_TO_REACH.get(nxt, "smd " + nxt + " {n}").format(n=entry.name)
        tail = "  [dim]\u00b7 next: " + cmd + "[/]"
    else:
        tail = "  [green]\u2713[/]"
    colour = "green" if pos >= 2 else "yellow"   # 'installed' (idx 2) or beyond
    tag = " [dim](dep)[/]" if pr["is_lib"] else ""
    return (f"[{colour}]{bar}[/]  {reached}{tag} "
            f"[dim]({pos + 1}/{total})[/]{tail}")


def _smd_binary() -> str:
    """See lifecycle._smd_binary — same logic, duplicated short helper."""
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


@dataclass
class _CatalogView:
    entries: list = field(default_factory=list)   # list[CatalogEntry]
    error: Optional[str] = None
    pulled: Optional[str] = None


def _gather_catalog(pull: bool = False) -> _CatalogView:
    view = _CatalogView()
    if pull:
        # Pull the latest sigmond so new HamSCI clients added to catalog.toml
        # appear as "Available".  Best-effort (public HTTPS remote needs no
        # auth); a failure just falls back to the on-disk catalog.
        try:
            r = subprocess.run(
                ["git", "-C", "/opt/git/sigmond/sigmond",
                 "-c", "safe.directory=*", "pull", "--ff-only"],
                capture_output=True, text=True, timeout=30, check=False)
            view.pulled = "up to date" if "up to date" in (r.stdout + r.stderr) \
                else ("updated" if r.returncode == 0 else "pull failed")
        except Exception:
            view.pulled = "pull failed"
    try:
        from ...catalog import load_catalog
        catalog = load_catalog()
        # Exclude entries with no repo URL — those have no git workflow.
        _KIND_ORDER = {"server": 0, "infra": 1, "client": 2, "library": 3}
        view.entries = sorted(
            (e for e in catalog.values() if e.repo),
            key=lambda e: (_KIND_ORDER.get(e.kind, 2), e.name),
        )
    except FileNotFoundError as exc:
        view.error = f"catalog not found: {exc}"
    except Exception as exc:
        view.error = str(exc)
    return view


class InstallScreen(Vertical):
    """Browse the catalog and install clients."""

    DEFAULT_CSS = """
    InstallScreen {
        padding: 1;
    }
    InstallScreen .is-title {
        text-style: bold;
        margin-bottom: 1;
    }
    InstallScreen #is-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    InstallScreen #is-table {
        height: auto;
        max-height: 30;
    }
    InstallScreen #is-actions {
        height: 3;
        margin-top: 1;
    }
    InstallScreen #is-actions Button {
        margin-right: 1;
    }
    InstallScreen #is-profiles, InstallScreen #is-bringups {
        height: 3;
        margin-top: 1;
    }
    InstallScreen #is-profiles Button, InstallScreen #is-bringups Button {
        margin-right: 2;
    }
    InstallScreen #is-last {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def compose(self):
        yield Static("Software Install — catalog of known clients",
                     classes="is-title")
        yield Static("[dim]loading\u2026[/]", id="is-status")
        table = DataTable(id="is-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Kind", "Name", "Description", "Progress  (download \u2192 install \u2192 configure \u2192 enable \u2192 run)")
        yield table
        with Horizontal(id="is-actions"):
            yield Button("Install selected", id="is-one", variant="primary")
            yield Button("Install enabled", id="is-enabled", variant="success")
            yield Button("Refresh", id="is-refresh", variant="default")
        try:
            from ...catalog import load_profiles as _lp
            _profiles = sorted(_lp())
        except Exception:
            _profiles = []
        if _profiles:
            yield Static("Station bundles (one-shot install):", classes="is-title")
            with Horizontal(id="is-profiles"):
                for _pname in _profiles:
                    yield Button(f"Install {_pname} station",
                                 id=f"is-profile-{_pname}", variant="success")
            yield Static("Guided bring-up (install \u2192 configure \u2192 start):",
                         classes="is-title")
            with Horizontal(id="is-bringups"):
                for _pname in _profiles:
                    yield Button(f"Bring up {_pname} (guided)",
                                 id=f"is-bringup-{_pname}", variant="primary")
        yield Static("", id="is-last")

    def on_mount(self) -> None:
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "is-refresh":
            self._refresh(pull=True)
        elif bid == "is-one":
            self._install_selected()
        elif bid == "is-enabled":
            self._install_enabled()
        elif bid and bid.startswith("is-profile-"):
            self._install_profile(bid[len("is-profile-"):])
        elif bid and bid.startswith("is-bringup-"):
            self._bringup_profile(bid[len("is-bringup-"):])

    def _refresh(self, pull: bool = False) -> None:
        self.query_one("#is-status", Static).update(
            "[dim]git pull + reload\u2026[/]" if pull else "[dim]loading\u2026[/]")
        self.run_worker(lambda: _gather_catalog(pull), thread=True, name="is-gather")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        data = event.worker.result
        if not isinstance(data, _CatalogView):
            return
        self._render_data(data)

    def _render_data(self, view: _CatalogView) -> None:
        status = self.query_one("#is-status", Static)
        if view.error:
            status.update(f"[red]{view.error}[/]")
            return

        def _needs_install(e):
            stage, _ = _entry_stage(e)
            return stage in ("available", "downloaded")
        pending = sum(1 for e in view.entries if _needs_install(e))
        ready = len(view.entries) - pending
        pulled = f"  [dim](catalog: {view.pulled})[/]" if view.pulled else ""
        status.update(
            f"{len(view.entries)} entries  "
            f"\u2022  [green]{ready} built+[/]  "
            f"\u2022  [yellow]{pending} need install[/]{pulled}")

        table = self.query_one("#is-table", DataTable)
        table.clear()
        self._entries = list(view.entries)
        for entry in view.entries:
            table.add_row(entry.kind, entry.name,
                          entry.description[:48], _stage_cell(entry))

    def _selected_entry(self):
        table = self.query_one("#is-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        idx = table.cursor_row
        if not 0 <= idx < len(getattr(self, '_entries', [])):
            return None
        return self._entries[idx]

    def _install_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.query_one("#is-last", Static).update(
                "[yellow]pick a row first[/]")
            return
        stage, _ = _entry_stage(entry)
        if stage not in ("available", "downloaded"):
            self.query_one("#is-last", Static).update(
                f"[dim]{entry.name} already built ({stage})[/]")
            return

        cmd = [_smd_binary(), 'install', entry.name]
        confirm_and_run(
            self.app,
            title=f"Install {entry.name}?",
            body=(f"Clone and install [bold]{entry.name}[/] "
                  f"({entry.kind}).\n\n{entry.description}"),
            cmd=cmd, sudo=True,
            on_complete=self._after_install,
        )

    def _install_enabled(self) -> None:
        cmd = [_smd_binary(), 'install', '--yes']
        confirm_and_run(
            self.app,
            title="Install the topology-enabled components?",
            body=("Installs every component this host's [bold]Topology[/] has "
                  "enabled \u2014 set the station shape on the Topology screen "
                  "first (or use a profile / guided bring-up below).\n\n"
                  "Already-built components are left alone."),
            cmd=cmd, sudo=True,
            on_complete=self._after_install,
        )

    def _bringup_profile(self, pname: str) -> None:
        # Bring-up is an interactive, multi-stage sequence (per-client config
        # interviews + a long FFT-wisdom wait), so it can't run through the
        # capture-and-modal mutation runner.  Suspend the TUI, hand the
        # terminal to `smd bringup`, then resume and refresh.
        smd = _smd_binary()
        try:
            with self.app.suspend():
                subprocess.run(['sudo', smd, 'bringup', '--profile', pname])
        except Exception as exc:                       # noqa: BLE001
            self.app.notify(f"bringup failed to launch: {exc}",
                            severity="error", timeout=8)
            return
        self.app.notify(f"bringup '{pname}' finished — review the terminal output",
                        timeout=6)
        self._refresh()

    def _install_profile(self, pname: str) -> None:
        try:
            from ...catalog import load_profiles
            prof = load_profiles().get(pname)
        except Exception as exc:                       # noqa: BLE001
            self.query_one("#is-last", Static).update(f"[red]{exc}[/]")
            return
        if prof is None:
            self.query_one("#is-last", Static).update(
                f"[red]unknown profile {pname}[/]")
            return
        cmd = [_smd_binary(), 'install', '--profile', pname, '--yes']
        body = (
            f"Install the [bold]{pname}[/] station bundle (assumes local radiod):\n\n"
            f"  foundation: ka9q-radio\n"
            f"  infra:      {', '.join(prof.local_radiod_infra) or '(none)'}\n"
            f"  clients:    {', '.join(prof.clients)}\n\n"
            f"Enables each in topology and runs its install. "
            f"Optional add-ons ({', '.join(prof.optional) or 'none'}) install separately.")
        confirm_and_run(
            self.app,
            title=f"Install {pname} station?",
            body=body, cmd=cmd, sudo=True,
            on_complete=self._after_install,
        )

    def _after_install(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#is-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]\u2714 exit 0[/]  {argv}")
        else:
            last.update(f"[red]\u2718 exit {result.returncode}[/]  {argv}")
        self._refresh()
