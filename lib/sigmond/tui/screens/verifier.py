"""Verifier screen — TUI counterpart to `smd verifier {report,rehabilitate}`.

Two complementary actions on one screen because they share an
operator workflow: see a spot wsprnet dropped → rehabilitate that
specific callsign so wsprd/jt9 are re-fed the call on the next
cycle.  CLI-V2-SPEC.md §3 Data quality (`verifier`).

Report section:
  smd verifier report --window <DUR> --target <wspr|psk> [--lost ...]
                      [--rx-call CALL]
Rehabilitate section:
  sudo smd verifier rehabilitate <rx_call> <call>
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Input, Label, RichLog, Select, Static

from ..mutation import confirm_and_run


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


# Sentinel for the "no instance filter" Select value.
_INSTANCE_ALL = "__all__"

# verifier target → templated recorder client (the spot rows come from
# its instances).  Used to populate the per-instance dropdown.
_TARGET_TO_CLIENT = {
    "wspr": "wspr-recorder",
    "psk":  "psk-recorder",
}


class VerifierScreen(Vertical):
    """Wsprnet upload audit (report) + per-callsign suppression clear (rehabilitate)."""

    DEFAULT_CSS = """
    VerifierScreen {
        padding: 1;
    }
    VerifierScreen .vf-title {
        text-style: bold;
        margin-bottom: 1;
    }
    VerifierScreen .vf-section {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    VerifierScreen .vf-body {
        margin-bottom: 1;
        color: $text-muted;
    }
    VerifierScreen .vf-field-row {
        height: 3;
        margin-bottom: 1;
    }
    VerifierScreen .vf-field-row Label {
        width: 12;
        content-align: left middle;
    }
    VerifierScreen .vf-field-row Input {
        width: 22;
        margin-right: 2;
    }
    VerifierScreen .vf-field-row Select {
        width: 14;
        margin-right: 2;
    }
    VerifierScreen .vf-checks {
        height: 3;
        margin-bottom: 1;
    }
    VerifierScreen .vf-checks Checkbox {
        margin-right: 2;
    }
    VerifierScreen #vf-controls Button {
        margin-right: 1;
    }
    VerifierScreen #vf-output {
        height: 18;
        border: solid $primary-background;
        margin-top: 1;
    }
    VerifierScreen #vf-last {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def compose(self):
        yield Static("Verifier — wsprnet upload audit + rehabilitate",
                     classes="vf-title")
        yield Static(
            "Reports the cohort of spots that were uploaded but never "
            "appeared in wspr.rx (lost / in-flight / delivered breakdown), "
            "and lets you clear a negative-cache suppression so wsprd/jt9 "
            "are re-fed a callsign on the next cycle.",
            classes="vf-body")

        # ---- Report section ---------------------------------------------
        yield Static("Report", classes="vf-section")
        with Horizontal(classes="vf-field-row"):
            yield Label("Target")
            yield Select(
                [("wspr", "wspr"), ("psk", "psk")],
                value="wspr", id="vf-target", allow_blank=False,
            )
            yield Label("Instance")
            yield Select(
                self._instance_options_for("wspr"),
                value=_INSTANCE_ALL, id="vf-instance",
                allow_blank=False,
            )
            yield Label("Window")
            yield Input("1h", id="vf-window",
                        placeholder="e.g. 1h, 24h, 7d")
            yield Label("RX call")
            yield Input("", id="vf-rxcall",
                        placeholder="auto-detect")
        with Horizontal(classes="vf-checks"):
            yield Checkbox("Lost", id="vf-lost")
            yield Checkbox("In-flight", id="vf-inflight")
            yield Checkbox("Delivered", id="vf-delivered")
            yield Checkbox("Cadence", id="vf-cadence")
        with Horizontal(id="vf-controls"):
            yield Button("Run report", id="vf-run", variant="primary")
            yield Button("Clear output", id="vf-clear", variant="default")
        yield RichLog(id="vf-output", highlight=False, markup=False,
                      max_lines=2000, wrap=False)
        yield Static("[dim]idle[/]", id="vf-last", markup=True)

        # ---- Rehabilitate section ---------------------------------------
        yield Static("Rehabilitate (clear suppression)",
                     classes="vf-section")
        yield Static(
            "Clears the wsprnet_reject_cache entry for one "
            "(rx_call, call) pair so wsprd/jt9 are re-fed that callsign "
            "on the next cycle.  Useful after wsprnet has stopped "
            "silently dropping the call.  Requires root.",
            classes="vf-body")
        with Horizontal(classes="vf-field-row"):
            yield Label("RX call")
            yield Input("", id="vf-reh-rxcall",
                        placeholder="e.g. AC0G/B4")
            yield Label("TX call")
            yield Input("", id="vf-reh-call",
                        placeholder="e.g. W4UK/P")
            yield Button("Rehabilitate", id="vf-reh-run", variant="warning")

    @staticmethod
    def _instance_options_for(target: str) -> list:
        """Build the (label, value) list for the instance Select widget."""
        client = _TARGET_TO_CLIENT.get(target)
        options: list = [("(all instances)", _INSTANCE_ALL)]
        if client is None:
            return options
        try:
            from ...instance import (
                list_instances, detect_migration_candidates,
            )
        except Exception:
            return options
        for i in list_instances(catalog_clients=[client]):
            options.append((i.reporter_id, i.reporter_id))
        try:
            for c in detect_migration_candidates():
                if c.client == client:
                    label = f"{c.old_instance} (legacy)"
                    options.append((label, c.old_instance))
        except Exception:
            pass
        return options

    def on_select_changed(self, event: Select.Changed) -> None:
        """Repopulate instance dropdown on target change; auto-fill
        RX call when an instance is picked."""
        sel_id = event.select.id
        if sel_id == "vf-target":
            target = str(event.value) if event.value is not None else ""
            if not target or target == Select.BLANK:
                return
            instance_sel = self.query_one("#vf-instance", Select)
            instance_sel.set_options(self._instance_options_for(target))
            instance_sel.value = _INSTANCE_ALL
        elif sel_id == "vf-instance":
            inst = event.value
            if inst in (None, Select.BLANK, _INSTANCE_ALL):
                return
            # Render reporter-id back into WSPRnet slash form for the
            # rx_call input (AC0G-B1 → AC0G/B1).  Operator can still
            # hand-type to override.
            try:
                from ...instance import to_wsprnet_form
                rxcall = to_wsprnet_form(str(inst))
            except Exception:
                rxcall = str(inst)
            self.query_one("#vf-rxcall", Input).value = rxcall

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "vf-run":
            self._run_report()
        elif event.button.id == "vf-clear":
            self.query_one("#vf-output", RichLog).clear()
            self.query_one("#vf-last", Static).update("[dim]idle[/]")
        elif event.button.id == "vf-reh-run":
            self._run_rehabilitate()

    def _run_report(self) -> None:
        target = self.query_one("#vf-target", Select).value
        window = self.query_one("#vf-window", Input).value.strip() or "1h"
        rxcall = self.query_one("#vf-rxcall", Input).value.strip()

        cmd = [_smd_binary(), 'verifier', 'report',
               '--target', str(target),
               '--window', window]
        if rxcall:
            cmd += ['--rx-call', rxcall]
        if self.query_one("#vf-lost", Checkbox).value:
            cmd.append('--lost')
        if self.query_one("#vf-inflight", Checkbox).value:
            cmd.append('--in-flight')
        if self.query_one("#vf-delivered", Checkbox).value:
            cmd.append('--delivered')
        if self.query_one("#vf-cadence", Checkbox).value:
            cmd.append('--cadence')

        log = self.query_one("#vf-output", RichLog)
        log.clear()
        log.write(f"$ {' '.join(cmd)}")
        self.query_one("#vf-last", Static).update(
            "[dim]running report…[/]")
        self.run_worker(lambda: self._exec(cmd), thread=True, name="vf-report")

    def _exec(self, cmd: list) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.app.call_from_thread(
                self.query_one("#vf-output", RichLog).write,
                f"[error: {exc}]",
            )
            self.app.call_from_thread(
                self.query_one("#vf-last", Static).update,
                f"[red]failed to launch: {exc}[/]",
            )
            return subprocess.CompletedProcess(cmd, 127, "", str(exc))

        log = self.query_one("#vf-output", RichLog)
        for line in (result.stdout or "").splitlines():
            self.app.call_from_thread(log.write, line)
        for line in (result.stderr or "").splitlines():
            self.app.call_from_thread(log.write, line)
        badge = ("[green]✔ exit 0[/]" if result.returncode == 0
                 else f"[red]✘ exit {result.returncode}[/]")
        self.app.call_from_thread(
            self.query_one("#vf-last", Static).update,
            f"{badge}  {' '.join(cmd)}",
        )
        return result

    def _run_rehabilitate(self) -> None:
        rxcall = self.query_one("#vf-reh-rxcall", Input).value.strip()
        call = self.query_one("#vf-reh-call", Input).value.strip()
        if not rxcall or not call:
            self.query_one("#vf-last", Static).update(
                "[red]Rehabilitate: both RX call and TX call required[/]")
            return

        cmd = [_smd_binary(), 'verifier', 'rehabilitate', rxcall, call]
        confirm_and_run(
            self.app,
            title="Rehabilitate callsign?",
            body=(f"Clears the wsprnet_reject_cache suppression for "
                  f"rx_call=[{rxcall}] call=[{call}].  wsprd/jt9 will be "
                  f"re-fed this callsign on the next decode cycle. "
                  f"Reversible — wsprnet may re-suppress on next reject."),
            cmd=cmd, sudo=True,
            on_complete=self._after_rehabilitate,
        )

    def _after_rehabilitate(self, result: subprocess.CompletedProcess) -> None:
        last = self.query_one("#vf-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        if result.returncode == 0:
            last.update(f"[green]✔ exit 0[/]  {argv}")
        else:
            last.update(f"[red]✘ exit {result.returncode}[/]  {argv}")
