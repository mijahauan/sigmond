"""Diag net screen — TUI counterpart to `smd diag net`.

Classifies the host's network environment for multi-host radiod safety.
Tier 1 checks run unprivileged; Tier 2 (raw-socket IGMP listen) needs
root.  This screen runs `sudo smd diag net --json` in a worker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Static
from textual.worker import Worker, WorkerState


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


def _run_diag(listen_s: int, use_sudo: bool) -> dict:
    cmd = [_smd_binary(), 'diag', 'net', '--json',
           '--listen', str(listen_s)]
    argv = ['sudo', '-n', *cmd] if use_sudo else cmd
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           check=False, stdin=subprocess.DEVNULL,
                           timeout=max(listen_s + 30, 60))
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"launch failed: {exc}"}
    except subprocess.TimeoutExpired:
        return {"error": "smd diag net timed out"}
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[:400]
        if 'sudo: a password is required' in msg or 'sudo:' in msg[:10]:
            return {"error": "sudo requires a password — "
                    "run `sudo smd diag net` in a terminal for Tier-2 listen"}
        return {"error": msg or f"exit {r.returncode}"}
    try:
        return {"payload": json.loads(r.stdout)}
    except json.JSONDecodeError as exc:
        return {"error": f"invalid JSON: {exc}"}


def _classification_badge(cls: str) -> str:
    if cls == "lan-safe":
        return "[green]✔ lan-safe[/]"
    if cls == "lan-capable":
        return "[green]~ lan-capable[/]"
    if cls == "lan-needs-querier":
        return "[yellow]⚠ lan-needs-querier[/]"
    if cls == "lan-unsafe":
        return "[red]✘ lan-unsafe[/]"
    return f"[yellow]? {cls}[/]"


class DiagNetScreen(Vertical):
    """Network environment classification."""

    DEFAULT_CSS = """
    DiagNetScreen {
        padding: 1;
    }
    DiagNetScreen .dn-title {
        text-style: bold;
        margin-bottom: 1;
    }
    DiagNetScreen .dn-section {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    DiagNetScreen #dn-controls {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }
    DiagNetScreen #dn-controls Input {
        width: 12;
        margin-right: 1;
    }
    DiagNetScreen #dn-controls Button {
        margin-right: 1;
    }
    DiagNetScreen #dn-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    DiagNetScreen #dn-classification {
        margin-top: 1;
        text-style: bold;
    }
    DiagNetScreen #dn-recommendation {
        color: $text-muted;
        margin-bottom: 1;
    }
    """

    def compose(self):
        yield Static("Diag: network", classes="dn-title")
        yield Static(
            "Classifies IGMP behavior so radiod multicast stays safe on "
            "LANs without a querier.  Tier-2 listen needs root.",
            id="dn-status")

        with Horizontal(id="dn-controls"):
            yield Input(value="10", id="dn-listen",
                        placeholder="listen s")
            yield Button("Fast scan", id="dn-fast", variant="primary")
            yield Button("Full listen (sudo)", id="dn-full",
                         variant="warning")

        yield Static("", id="dn-classification")
        yield Static("", id="dn-recommendation")

        yield Static("Interfaces", classes="dn-section")
        it = DataTable(id="dn-ifaces")
        it.add_columns("Name", "Up", "Default route", "Multicast",
                       "v4 addrs", "Tags")
        yield it

        yield Static("IGMP queriers", classes="dn-section")
        qt = DataTable(id="dn-queriers")
        qt.add_columns("Version", "Source", "Interface")
        yield qt

        yield Static("Reasons", classes="dn-section")
        yield Static("", id="dn-reasons")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dn-fast":
            self._start(use_sudo=False, listen_default=0)
        elif event.button.id == "dn-full":
            self._start(use_sudo=True, listen_default=30)

    def _start(self, use_sudo: bool, listen_default: int) -> None:
        listen_s = listen_default
        try:
            listen_s = int(self.query_one("#dn-listen", Input).value or "0")
        except ValueError:
            pass
        if not use_sudo:
            listen_s = 0
        mode = "Tier-2 listen (sudo)" if use_sudo else "Tier-1 fast scan"
        self.query_one("#dn-status", Static).update(
            f"[dim]running {mode}, listen={listen_s}s…[/]")
        self.run_worker(
            lambda: _run_diag(listen_s, use_sudo),
            thread=True, name="dn-run",
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "dn-run":
            return
        if event.state != WorkerState.SUCCESS:
            return
        result = event.worker.result
        if not isinstance(result, dict):
            return
        if "error" in result:
            self.query_one("#dn-status", Static).update(
                f"[red]{result['error']}[/]")
            return
        self._render_report(result["payload"])

    def _render_report(self, d: dict) -> None:
        self.query_one("#dn-status", Static).update(
            f"[green]scan complete[/]  listen={d.get('listen_seconds', 0)}s  "
            f"root={bool(d.get('listen_root'))}")

        cls = d.get("classification", "unknown")
        self.query_one("#dn-classification", Static).update(
            _classification_badge(cls))
        self.query_one("#dn-recommendation", Static).update(
            d.get("recommendation") or "")

        it = self.query_one("#dn-ifaces", DataTable)
        it.clear()
        for i in d.get("interfaces") or []:
            if i.get("is_loopback"):
                continue
            tags: list = []
            if i.get("is_wireless"):
                tags.append("wireless")
            if i.get("is_bridge"):
                tags.append("bridge")
            if i.get("is_bond"):
                tags.append("bond")
            if i.get("is_overlay"):
                tags.append("overlay")
            it.add_row(
                i.get("name", "?"),
                "[green]up[/]" if i.get("is_up") else "[dim]down[/]",
                "[cyan]default[/]" if i.get("is_default_route") else "",
                "yes" if i.get("has_multicast") else "[red]no[/]",
                ", ".join(i.get("addrs_v4") or []) or "—",
                ", ".join(tags) or "—",
            )

        qt = self.query_one("#dn-queriers", DataTable)
        qt.clear()
        for q in d.get("queriers") or []:
            qt.add_row(f"v{q.get('version', '?')}",
                       str(q.get("source", "?")),
                       str(q.get("interface") or "—"))
        if not d.get("queriers"):
            qt.add_row("[dim]—[/]",
                       "[dim]no queriers observed[/]", "")

        reasons = d.get("reasons") or []
        self.query_one("#dn-reasons", Static).update(
            "\n".join(f"  • {r}" for r in reasons) if reasons
            else "[dim](none)[/]")
