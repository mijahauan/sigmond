"""ka9q-watch screen — TUI surface for `smd watch ka9q`.

Read-only check (no sudo, no mutation): shells out to
``smd watch ka9q --json`` and renders the structured report.  Two
independent sub-reports surface here:

* ``upstream_drift`` — what's between the ka9q-python pin
  (``KA9Q_RADIO_COMMIT`` in setup.cfg) and the live ka9q-radio
  ``origin/main``.  Lists every commit between them with a flag
  for whether the commit touched a stream-critical header, plus
  per-header field-level deltas when a header DID change.

* ``installed_vs_pin`` — the SHA of the radiod binary currently
  installed on the host vs the SHA ka9q-python's RTP parser was
  written against.  Warns when the installed radiod has advanced
  past the pin without ka9q-python keeping pace (RTP parsers
  can silently mis-frame).

Severity colors apply to both sub-reports:
    green  — pass
    yellow — warn
    red    — fail
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
    Ka9qWatchScreen .kw-section {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    Ka9qWatchScreen #kw-installed {
        margin-bottom: 1;
    }
    """

    def compose(self):
        yield Static("ka9q-watch — upstream ka9q-radio drift",
                     classes="kw-title", markup=True)

        # upstream_drift: pin vs origin/main
        yield Static("Pin vs origin/main", classes="kw-section")
        yield Static("[dim]loading…[/]", id="kw-summary", markup=True)
        yield Static("", id="kw-pins", markup=True)

        commits = DataTable(id="kw-commits", cursor_type="row",
                            zebra_stripes=True)
        commits.add_columns("H", "Commit", "Subject")
        yield commits

        deltas = DataTable(id="kw-deltas", cursor_type="row",
                           zebra_stripes=True)
        deltas.add_columns("Sev", "Header", "Change", "Why it matters")
        yield deltas

        # installed_vs_pin: SHA of /usr/local/sbin/radiod vs the pin
        yield Static("Installed radiod vs pin", classes="kw-section")
        yield Static("", id="kw-installed", markup=True)

        with Horizontal(id="kw-actions"):
            yield Button("Refresh (no fetch)", id="kw-refresh",
                         variant="primary")
            yield Button("Refresh + git fetch", id="kw-fetch",
                         variant="warning")

        yield Static(
            "[dim]Read-only check — no sudo required.  "
            "Pin-vs-upstream compares KA9Q_RADIO_COMMIT in "
            "ka9q-python's setup.cfg against ka9q-radio's "
            "origin/main; red rows are stream-critical field "
            "shifts where RTP delivery to clients would break "
            "if ka9q-python advanced its pin without code "
            "changes.  Installed-vs-pin compares the SHA of the "
            "running radiod binary on this host against the pin — "
            "warns when the host has advanced past the pin so "
            "the RTP parser may silently mis-frame.[/]",
            id="kw-hint", markup=True,
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
        installed = self.query_one("#kw-installed", Static)
        commits.clear()
        deltas.clear()

        if result.report is None:
            summary.update(
                f"[red]✘ checker failed (exit {result.returncode})[/]")
            pins.update(result.stderr.strip()[:400] or "(no stderr)")
            installed.update("")
            return

        # `smd watch ka9q --json` returns
        #   {"upstream_drift": {...}, "installed_vs_pin": {...}}
        # since commit f44c51b.  Read both sub-reports.
        drift = result.report.get("upstream_drift") or {}
        sev = drift.get("severity", "fail")
        badge = _SEV_BADGE.get(sev, sev)
        summary.update(f"{badge}  {drift.get('summary', '(no summary)')}")

        pin = drift.get("pin", "") or ""
        up  = drift.get("upstream_sha") or ""
        ref = drift.get("upstream_ref") or "?"
        pin_s = pin[:12] if pin else "(none)"
        up_s  = up[:12] if up else "(none)"
        pins.update(
            f"pin: [bold]{pin_s}[/]   upstream: [bold]{up_s}[/] ({ref})"
        )

        for c in drift.get("commits") or []:
            mark = "[yellow]H[/]" if c.get("touches_headers") else " "
            commits.add_row(mark, c.get("sha", "")[:12],
                            c.get("subject", ""))

        for d in drift.get("header_deltas") or []:
            for ch in d.get("changes", []):
                csev = ch.get("severity", "warn")
                cbadge = _SEV_BADGE.get(csev, csev)
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
                deltas.add_row(cbadge,
                               f"{d.get('header', '?')} ({d.get('enum', '?')})",
                               chg, ch.get("reason", ""))

        # Second sub-report: installed radiod vs pin.
        ivp = result.report.get("installed_vs_pin") or {}
        if ivp:
            ibadge = _SEV_BADGE.get(ivp.get("severity", "warn"),
                                    ivp.get("severity", "warn"))
            inst = (ivp.get("installed_sha") or "")[:12] or "(unknown)"
            pin2 = (ivp.get("pin_sha") or "")[:12] or "(unknown)"
            path = ivp.get("installed_path", "?")
            installed.update(
                f"{ibadge}  {ivp.get('summary', '')}\n"
                f"installed: [bold]{inst}[/]  "
                f"({path})\n"
                f"pin:       [bold]{pin2}[/]"
            )
        else:
            installed.update("[dim](no installed-vs-pin section)[/]")
