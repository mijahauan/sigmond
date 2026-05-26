"""ka9q-watch screen — TUI surface for `smd watch ka9q`.

Read-only check (no sudo, no mutation): shells out to
``smd watch ka9q --json`` and renders the structured report.

Severity colors:
    green  — pass (no upstream commits, or upstream advanced but no header touched)
    yellow — warn (header changed, but no stream-critical field affected)
    red    — fail (a stream-critical field was removed or its TLV value shifted)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Static
from textual.worker import Worker, WorkerState


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


@dataclass
class _RunResult:
    report: Optional[dict]
    stderr: str
    returncode: int


def _run(no_fetch: bool) -> _RunResult:
    cmd = [_smd_binary(), "watch", "ka9q", "--json"]
    if no_fetch:
        cmd.append("--no-fetch")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _RunResult(report=None, stderr=str(exc), returncode=2)

    report: Optional[dict] = None
    if proc.stdout.strip():
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass
    return _RunResult(report=report, stderr=proc.stderr, returncode=proc.returncode)


_SEV_BADGE = {
    "pass": "[green]✔ pass[/]",
    "warn": "[yellow]⚠ warn[/]",
    "fail": "[red]✘ fail[/]",
}


class Ka9qWatchScreen(Vertical):
    """Show ka9q-radio drift report; refresh on demand."""

    DEFAULT_CSS = """
    Ka9qWatchScreen {
        padding: 1;
    }
    Ka9qWatchScreen .kw-title {
        text-style: bold;
        margin-bottom: 1;
    }
    Ka9qWatchScreen #kw-summary {
        margin-bottom: 1;
    }
    Ka9qWatchScreen #kw-pins {
        color: $text-muted;
        margin-bottom: 1;
    }
    Ka9qWatchScreen #kw-commits {
        height: 8;
        margin-bottom: 1;
    }
    Ka9qWatchScreen #kw-deltas {
        height: 10;
        margin-bottom: 1;
    }
    Ka9qWatchScreen #kw-actions {
        height: 3;
    }
    Ka9qWatchScreen #kw-actions Button {
        margin-right: 1;
    }
    Ka9qWatchScreen #kw-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def compose(self):
        yield Static("ka9q-watch — upstream ka9q-radio drift", classes="kw-title")
        yield Static("[dim]loading…[/]", id="kw-summary")
        yield Static("", id="kw-pins")

        commits = DataTable(id="kw-commits", cursor_type="row", zebra_stripes=True)
        commits.add_columns("H", "Commit", "Subject")
        yield commits

        deltas = DataTable(id="kw-deltas", cursor_type="row", zebra_stripes=True)
        deltas.add_columns("Sev", "Header", "Change", "Why it matters")
        yield deltas

        with Horizontal(id="kw-actions"):
            yield Button("Refresh (no fetch)", id="kw-refresh", variant="primary")
            yield Button("Refresh + git fetch", id="kw-fetch",   variant="warning")

        yield Static(
            "[dim]Read-only check — no sudo required.  Compares the pinned "
            "ka9q-radio commit (ka9q_radio_compat) against origin/main.  "
            "Red rows indicate stream-critical fields whose value or "
            "presence changed upstream — RTP delivery to clients would "
            "break if ka9q-python advanced its pin without code changes.[/]",
            id="kw-hint",
        )

    def on_mount(self) -> None:
        self._refresh(no_fetch=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "kw-refresh":
            self._refresh(no_fetch=True)
        elif event.button.id == "kw-fetch":
            self._refresh(no_fetch=False)

    def _refresh(self, *, no_fetch: bool) -> None:
        self.query_one("#kw-summary", Static).update("[dim]running…[/]")
        self.run_worker(lambda: _run(no_fetch),
                        thread=True, name="kw-run")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        result = event.worker.result
        if not isinstance(result, _RunResult):
            return
        self._render_result(result)

    def _render_result(self, result: _RunResult) -> None:
        summary = self.query_one("#kw-summary", Static)
        pins    = self.query_one("#kw-pins",    Static)
        commits = self.query_one("#kw-commits", DataTable)
        deltas  = self.query_one("#kw-deltas",  DataTable)
        commits.clear()
        deltas.clear()

        if result.report is None:
            summary.update(f"[red]✘ checker failed (exit {result.returncode})[/]")
            pins.update(result.stderr.strip()[:400] or "(no stderr)")
            return

        rep = result.report
        sev = rep.get("severity", "fail")
        badge = _SEV_BADGE.get(sev, sev)
        summary.update(f"{badge}  {rep.get('summary', '(no summary)')}")

        pin = rep.get("pin", "")
        up  = rep.get("upstream_sha") or ""
        ref = rep.get("upstream_ref") or "?"
        pins.update(
            f"pin: [bold]{pin[:12]}[/]   upstream: [bold]{up[:12]}[/] ({ref})"
        )

        for c in rep.get("commits") or []:
            mark = "[yellow]H[/]" if c.get("touches_headers") else " "
            commits.add_row(mark, c["sha"][:12], c["subject"])

        for d in rep.get("header_deltas") or []:
            for ch in d.get("changes", []):
                csev = ch.get("severity", "warn")
                badge = _SEV_BADGE.get(csev, csev)
                kind = ch.get("kind", "?")
                name = ch.get("name", "?")
                if kind == "added":
                    chg = f"+{name} = {ch.get('head')}"
                elif kind == "removed":
                    chg = f"-{name}  (was {ch.get('pin')})"
                elif kind == "value_changed":
                    chg = f"~{name}: {ch.get('pin')} → {ch.get('head')}"
                else:
                    chg = f"?{name}"
                deltas.add_row(badge, f"{d['header']} ({d['enum']})",
                               chg, ch.get("reason", ""))
