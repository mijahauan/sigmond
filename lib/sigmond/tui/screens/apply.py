"""Apply screen — TUI counterpart to `smd apply`.

Reconciles running services with the current coordination + topology
config.  The screen surfaces three things:

  * **Pending pane** (top): automatically runs ``smd apply --dry-run``
    on mount and shows only the lines that announce a change.  Gives a
    passive answer to "do I have edits sitting here unapplied?" without
    the operator having to click anything.  Cached for
    ``_CACHE_TTL_SEC`` seconds.

  * **Dry-run button**: re-runs the full preview into the log pane.

  * **Apply button**: ``smd apply`` gated by a confirm modal.
    On success, invalidates the pending cache and re-renders.

The Pending pane uses the classifier in ``sigmond.apply_pending`` —
kept in a textual-free module so the classification rules are
unit-testable without the TUI runtime.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, RichLog, Static
from textual.worker import Worker, WorkerState

from ...apply_pending import classify_line as _classify_line
from ...apply_pending import strip_ansi as _strip_ansi
from ..mutation import confirm_and_run


# How long to trust a cached pending result before re-running dry-run
# on screen mount.  30s keeps navigation snappy while the operator
# tabs between Topology / Coordination / Apply; the manual Refresh
# button always bypasses the cache.
_CACHE_TTL_SEC = 30.0


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/bin/smd'


class ApplyScreen(Vertical):
    """Reconcile running services with current config; preview pending."""

    DEFAULT_CSS = """
    ApplyScreen {
        padding: 1;
    }
    ApplyScreen .ap-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ApplyScreen .ap-body {
        margin-bottom: 1;
        color: $text-muted;
    }
    ApplyScreen #ap-pending-header {
        text-style: bold;
        margin-top: 1;
    }
    ApplyScreen #ap-pending {
        height: 8;
        border: solid $primary-background;
        margin-bottom: 1;
    }
    ApplyScreen #ap-controls {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }
    ApplyScreen #ap-controls Button {
        margin-right: 1;
    }
    ApplyScreen #ap-output {
        height: 16;
        border: solid $primary-background;
    }
    ApplyScreen #ap-last {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # (computed_at, pending_lines, warning_lines, error_str_or_None).
        # None until first dry-run completes.
        self._cache: Optional[tuple[float, list[str], list[str], Optional[str]]] = None

    def compose(self):
        yield Static("Apply", classes="ap-title")
        yield Static(
            "Reconciles the running system with topology + coordination "
            "config.  The Pending pane below previews changes that would "
            "happen on the next apply; the buttons re-run the full "
            "dry-run or commit it.",
            classes="ap-body")

        yield Static("[dim]Pending: computing…[/]", id="ap-pending-header")
        yield RichLog(id="ap-pending", highlight=False, markup=True,
                      max_lines=200, wrap=False)

        yield Static(
            "Equivalent to:  [cyan]smd apply --dry-run[/]  /  "
            "[cyan bold]smd apply[/]",
            classes="ap-body")
        with Horizontal(id="ap-controls"):
            yield Button("Refresh pending", id="ap-refresh", variant="default")
            yield Button("Dry-run (full)", id="ap-dry", variant="primary")
            yield Button("Apply now", id="ap-run", variant="warning")
        yield RichLog(id="ap-output", highlight=False, markup=False,
                      max_lines=2000, wrap=False)
        yield Static("", id="ap-last")

    def on_mount(self) -> None:
        self._refresh_pending(force=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ap-refresh":
            self._refresh_pending(force=True)
        elif event.button.id == "ap-dry":
            self._run_dry()
        elif event.button.id == "ap-run":
            self._run_apply()

    # ---------------------------------------------------------------- pending

    def _refresh_pending(self, *, force: bool) -> None:
        """Render from cache or kick a worker to recompute.

        Cached for _CACHE_TTL_SEC unless force=True (Refresh button or
        post-apply auto-refresh).  The worker is exclusive within its
        group so rapid clicks cancel in-flight runs."""
        if not force and self._cache is not None:
            ts, pending, warnings, err = self._cache
            age = time.time() - ts
            if age < _CACHE_TTL_SEC:
                self._render_pending(pending, warnings, age=age, error=err)
                return
        self.query_one("#ap-pending-header", Static).update(
            "[dim]Pending: computing…[/]")
        self.query_one("#ap-pending", RichLog).clear()
        self.run_worker(self._classify_dry_run, thread=True,
                        group="ap-pending", exclusive=True)

    def _classify_dry_run(self) -> dict:
        """Worker thread: run `smd apply --dry-run` and bucket output."""
        cmd = [_smd_binary(), 'apply', '--dry-run']
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                stdin=subprocess.DEVNULL, timeout=30,
            )
        except (OSError, subprocess.SubprocessError,
                subprocess.TimeoutExpired) as exc:
            return {"error": str(exc), "pending": [], "warnings": []}
        pending: list[str] = []
        warnings: list[str] = []
        all_lines = ((result.stdout or "").splitlines()
                     + (result.stderr or "").splitlines())
        for raw in all_lines:
            kind = _classify_line(raw)
            stripped = _strip_ansi(raw).strip()
            if kind == 'pending':
                pending.append(stripped)
            elif kind == 'warning':
                warnings.append(stripped)
        return {"pending": pending, "warnings": warnings,
                "exit": result.returncode, "error": None}

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        # Only handle the pending-classifier worker.  The full-dry-run
        # worker writes results synchronously via app.call_from_thread
        # inside _exec and does not consume this event.
        if event.worker.group != "ap-pending":
            return
        if event.state == WorkerState.SUCCESS:
            data = event.worker.result or {}
            pending = data.get("pending", [])
            warnings = data.get("warnings", [])
            err = data.get("error")
            self._cache = (time.time(), pending, warnings, err)
            self._render_pending(pending, warnings, age=0.0, error=err)
        elif event.state == WorkerState.ERROR:
            self.query_one("#ap-pending-header", Static).update(
                "[red]Pending: dry-run worker crashed[/]")

    def _render_pending(self, pending: list[str], warnings: list[str],
                        *, age: float, error: Optional[str] = None) -> None:
        header = self.query_one("#ap-pending-header", Static)
        pane = self.query_one("#ap-pending", RichLog)
        pane.clear()
        age_suffix = (f"  [dim](computed {int(age)}s ago)[/]"
                      if age >= 5 else "")
        if error:
            header.update(
                f"[red]Pending: dry-run failed — {error}[/]{age_suffix}")
            return
        n_pending = len(pending)
        n_warn = len(warnings)
        if n_pending == 0 and n_warn == 0:
            header.update(
                f"[green]Pending: 0 (system in sync with config)[/]"
                f"{age_suffix}")
            pane.write("[dim]no pending changes — nothing to apply[/]")
            return
        bits = []
        if n_pending:
            bits.append(f"[yellow]{n_pending} change(s) would be applied[/]")
        if n_warn:
            bits.append(f"[red]{n_warn} warning(s)[/]")
        header.update("Pending: " + " · ".join(bits) + age_suffix)
        for line in pending:
            pane.write(f"[yellow]→[/] {line}")
        for line in warnings:
            pane.write(f"[red]⚠[/] {line}")

    # -------------------------------------------------------- full dry / apply

    def _run_dry(self) -> None:
        cmd = [_smd_binary(), 'apply', '--dry-run']
        log = self.query_one("#ap-output", RichLog)
        log.clear()
        log.write(f"$ {' '.join(cmd)}")
        self.query_one("#ap-last", Static).update("[dim]running dry-run…[/]")
        self.run_worker(lambda: self._exec(cmd), thread=True, name="ap-dry")

    def _exec(self, cmd: list) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.app.call_from_thread(
                self.query_one("#ap-output", RichLog).write,
                f"[error: {exc}]",
            )
            self.app.call_from_thread(
                self.query_one("#ap-last", Static).update,
                f"[red]failed to launch: {exc}[/]",
            )
            return subprocess.CompletedProcess(cmd, 127, "", str(exc))

        log = self.query_one("#ap-output", RichLog)
        for line in (result.stdout or "").splitlines():
            self.app.call_from_thread(log.write, line)
        for line in (result.stderr or "").splitlines():
            self.app.call_from_thread(log.write, line)
        badge = ("[green]✔ exit 0[/]" if result.returncode == 0
                 else f"[red]✘ exit {result.returncode}[/]")
        self.app.call_from_thread(
            self.query_one("#ap-last", Static).update,
            f"{badge}  {' '.join(cmd)}",
        )
        return result

    def _run_apply(self) -> None:
        cmd = [_smd_binary(), 'apply']
        confirm_and_run(
            self.app,
            title="Run apply?",
            body=("This reconciles running services with the current "
                  "coordination + topology config.  Services may "
                  "restart.  Dry-run first if you want a preview."),
            cmd=cmd, sudo=True,
            on_complete=self._after_apply,
        )

    def _after_apply(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#ap-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ exit 0[/]  {argv}")
            # Apply consumed the pending diff — invalidate cache and
            # re-classify so the Pending pane reflects post-apply state.
            self._cache = None
            self._refresh_pending(force=True)
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]  {argv}")
