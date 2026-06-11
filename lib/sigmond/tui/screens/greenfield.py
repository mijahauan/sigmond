"""Greenfield — the guided, CLI-free station bring-up wizard (#16 epic).

A thin Textual front-end over the ``smd bringup`` engine.  The operator
picks a station profile, enters station identity ONCE (reporter id + grid
required; callsign + PSWS station id optional), previews the plan, then
presses Begin — and the wizard streams ``smd bringup`` live in-TUI with a
plain-language verdict and fix-it actions at the end.

Design (see memory: sigmond-greenfield-tui-architecture):
  • We do NOT re-implement orchestration.  ``sigmond.bringup.build_plan``
    is pure and ``cmd_bringup`` is the executor — this screen just collects
    inputs and drives ``smd bringup --non-interactive``.
  • Run mode is CAPTURE-AND-STREAM: the bring-up runs in a worker and its
    output streams into a live modal.  Because a captured pipe has no TTY,
    we PRE-ELEVATE (``sudo -n -- env SIGMOND_ALLOW_SUDO=1 smd bringup …``)
    so smd's self-elevation never deadlocks on a password prompt.  When
    ``sudo -n`` needs a password we suspend once to cache creds, then stream.
  • Because the run is non-interactive, radiod is configured with antenna
    DEFAULTS — the verdict reminds the operator to fine-tune the antenna
    later with ``smd config edit radiod``.
  • Plan preview uses ``smd bringup … --dry-run`` which returns before any
    elevation, so it needs no sudo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button, Input, Label, RadioButton, RadioSet, Static,
)
from textual.worker import WorkerState

from ..mutation import ConfirmModal, UpdateOutputModal, suspend_and_run_sudo


def _smd_binary() -> str:
    """Resolve the smd entry point (mirrors install._smd_binary)."""
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


def _load_profiles() -> dict:
    try:
        from ...catalog import load_profiles
        return load_profiles()
    except Exception:                                   # noqa: BLE001
        return {}


# Profile presentation order + one-line "what you get" blurbs.  The catalog
# carries the authoritative client/infra lists; these are just operator-facing
# summaries for the radio buttons.
_PROFILE_ORDER = ["daisy", "dasi2", "client", "base"]
_PROFILE_BLURB = {
    "daisy": "Full local station — RX888 radiod + WSPR + PSK + timing + GPSDO",
    "dasi2": "Full station + magnetometer (DASI2 — needs an RM3100 on the bus)",
    "client": "Decode-only — bind a REMOTE radiod, no local SDR (WSPR + PSK + timing)",
    "base":   "Minimal — local radiod + timing only, no spot reporters",
}


class _BringupModal(UpdateOutputModal):
    """Streaming modal for the bring-up run that also remembers the exit code.

    ``UpdateOutputModal`` already streams a Popen's stdout+stderr line by line
    and shows a ✓/⚠ verdict by exit code; we subclass only to dismiss with the
    real return code so the Greenfield screen can render a verdict + fix-its.
    """

    def __init__(self, cmd: list, **kwargs) -> None:
        super().__init__("Guided bring-up — smd bringup", cmd, **kwargs)
        self._rc: int | None = None

    def on_worker_state_changed(self, event) -> None:        # noqa: D401
        super().on_worker_state_changed(event)
        if (event.worker.name == "uom-run"
                and event.state == WorkerState.SUCCESS):
            try:
                self._rc = event.worker.result[1]
            except Exception:                                # noqa: BLE001
                self._rc = None

    def _result_code(self) -> int:
        if self._rc is not None:
            return self._rc
        return 0 if self._done else 1

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "uom-dismiss":
            self.dismiss(self._result_code())

    def action_try_dismiss(self) -> None:
        if self._done:
            self.dismiss(self._result_code())


class GreenfieldScreen(Vertical):
    """Guided station bring-up — pick a profile, enter identity, go."""

    DEFAULT_CSS = """
    GreenfieldScreen { padding: 1; }
    GreenfieldScreen .gf-title { text-style: bold; margin-bottom: 1; }
    GreenfieldScreen .gf-section { text-style: bold; margin-top: 1; }
    GreenfieldScreen #gf-intro { color: $text-muted; margin-bottom: 1; }
    GreenfieldScreen RadioSet { height: auto; margin-bottom: 1; }
    GreenfieldScreen .gf-field { height: 3; }
    GreenfieldScreen .gf-field Label { width: 22; content-align: left middle; }
    GreenfieldScreen .gf-field Input { width: 36; }
    GreenfieldScreen #gf-remote-row { display: none; }
    GreenfieldScreen #gf-remote-row.show { display: block; }
    GreenfieldScreen #gf-actions { height: 3; margin-top: 1; }
    GreenfieldScreen #gf-actions Button { margin-right: 1; }
    GreenfieldScreen #gf-status { margin-top: 1; }
    GreenfieldScreen #gf-fixits { height: auto; margin-top: 1; display: none; }
    GreenfieldScreen #gf-fixits.show { display: block; }
    GreenfieldScreen #gf-fixits Button { margin-right: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._profiles = _load_profiles()
        self._profile_names = [p for p in _PROFILE_ORDER if p in self._profiles]
        # any catalog profiles we don't have a fixed order for, appended
        self._profile_names += [p for p in sorted(self._profiles)
                                if p not in self._profile_names]

    # ----- compose --------------------------------------------------------

    def compose(self):
        yield Static("Guided station bring-up", classes="gf-title")
        yield Static(
            "Pick a station profile and enter your identity once.  Preview the "
            "plan, then Begin — sigmond installs, configures, and starts the "
            "whole station, showing live progress.  No shell needed.",
            id="gf-intro")

        yield Static("1 · Station profile", classes="gf-section")
        with RadioSet(id="gf-profile"):
            for name in self._profile_names:
                blurb = _PROFILE_BLURB.get(name, "")
                label = f"{name}  —  {blurb}" if blurb else name
                yield RadioButton(label, value=(name == "daisy"),
                                  id=f"gf-prof-{name}")

        yield Static("2 · Station identity", classes="gf-section")
        with Horizontal(classes="gf-field"):
            yield Label("Reporter id *")
            yield Input(placeholder="e.g. AC0G/S", id="gf-reporter")
        with Horizontal(classes="gf-field"):
            yield Label("Grid square *")
            yield Input(placeholder="e.g. EM38ww", id="gf-grid")
        with Horizontal(classes="gf-field"):
            yield Label("Callsign")
            yield Input(placeholder="optional — e.g. AC0G", id="gf-callsign")
        with Horizontal(classes="gf-field"):
            yield Label("PSWS station id")
            yield Input(placeholder="optional", id="gf-psws")
        with Horizontal(classes="gf-field", id="gf-remote-row"):
            yield Label("Remote radiod DNS")
            yield Input(placeholder="e.g. bee3-status.local", id="gf-remote")

        with Horizontal(id="gf-actions"):
            yield Button("Preview plan", id="gf-preview", variant="default")
            yield Button("Begin bring-up", id="gf-begin", variant="success")

        yield Static("", id="gf-status")
        with Horizontal(id="gf-fixits"):
            yield Button("Edit antenna", id="gf-fix-antenna", variant="primary")
            yield Button("Open Validate", id="gf-fix-validate", variant="default")
            yield Button("Re-run bring-up", id="gf-fix-rerun", variant="default")

    def on_mount(self) -> None:
        self._sync_required_hints()

    # ----- profile-dependent UI ------------------------------------------

    def _selected_profile(self) -> str:
        rs = self.query_one("#gf-profile", RadioSet)
        btn = rs.pressed_button
        if btn is not None and btn.id and btn.id.startswith("gf-prof-"):
            return btn.id[len("gf-prof-"):]
        return self._profile_names[0] if self._profile_names else "daisy"

    def _profile_clients(self, name: str) -> list:
        prof = self._profiles.get(name)
        return list(getattr(prof, "clients", []) or [])

    def _profile_is_local(self, name: str) -> bool:
        prof = self._profiles.get(name)
        return bool(getattr(prof, "local_radiod_infra", []) or [])

    def _requirements(self, name: str) -> tuple[bool, bool]:
        """(needs_reporter, needs_grid) — mirrors cmd_bringup's logic."""
        clients = self._profile_clients(name)
        needs_reporter = any(c in clients
                             for c in ("wspr-recorder", "psk-recorder"))
        needs_grid = ("hf-timestd" in clients) or needs_reporter
        return needs_reporter, needs_grid

    def _sync_required_hints(self) -> None:
        name = self._selected_profile()
        needs_reporter, needs_grid = self._requirements(name)
        is_local = self._profile_is_local(name)

        # Star the truly-required fields for the chosen profile.
        rep_label = self.query_one("#gf-reporter").parent.query_one(Label)
        grid_label = self.query_one("#gf-grid").parent.query_one(Label)
        rep_label.update("Reporter id *" if needs_reporter else "Reporter id")
        grid_label.update("Grid square *" if needs_grid else "Grid square")

        # Remote-radiod DNS only matters for decode-only (no local infra).
        row = self.query_one("#gf-remote-row")
        row.set_class(not is_local, "show")

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._sync_required_hints()
        self.query_one("#gf-status", Static).update("")

    # ----- input gathering / validation ----------------------------------

    def _gather(self) -> dict:
        def val(wid: str) -> str:
            return self.query_one(wid, Input).value.strip()
        return {
            "profile": self._selected_profile(),
            "reporter": val("#gf-reporter"),
            "grid": val("#gf-grid"),
            "callsign": val("#gf-callsign"),
            "psws": val("#gf-psws"),
            "remote": val("#gf-remote"),
        }

    def _missing_required(self, g: dict) -> list:
        needs_reporter, needs_grid = self._requirements(g["profile"])
        missing = []
        if needs_reporter and not g["reporter"]:
            missing.append("Reporter id")
        if needs_grid and not g["grid"]:
            missing.append("Grid square")
        if not self._profile_is_local(g["profile"]) and not g["remote"]:
            missing.append("Remote radiod DNS")
        return missing

    def _build_argv(self, g: dict, *, dry_run: bool) -> list:
        argv = [_smd_binary(), "bringup", g["profile"], "--non-interactive"]
        if g["reporter"]:
            argv += ["--reporter", g["reporter"]]
        if g["grid"]:
            argv += ["--grid", g["grid"]]
        if g["callsign"]:
            argv += ["--callsign", g["callsign"]]
        if g["psws"]:
            argv += ["--psws-station-id", g["psws"]]
        if not self._profile_is_local(g["profile"]) and g["remote"]:
            argv += ["--remote-radiod", g["remote"]]
        if dry_run:
            argv.append("--dry-run")
        return argv

    # ----- button handlers ------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "gf-preview":
            self._preview()
        elif bid == "gf-begin":
            self._begin()
        elif bid == "gf-fix-antenna":
            self._edit_antenna()
        elif bid == "gf-fix-validate":
            self.app.action_show_validate()
        elif bid == "gf-fix-rerun":
            self.query_one("#gf-fixits").remove_class("show")
            self.query_one("#gf-status", Static).update("")

    def _preview(self) -> None:
        g = self._gather()
        # --dry-run makes no changes and needs no sudo; it prints the staged
        # plan + any environment blockers.  Stream it in the same modal.
        # PYTHONUNBUFFERED=1: smd's progress (_heading/_info/_ok via print) is
        # the PARENT process's stdout, which Python block-buffers on a non-TTY
        # pipe — so without this the stage scaffolding would dump all at once
        # at the end instead of streaming live.
        argv = ["env", "PYTHONUNBUFFERED=1", *self._build_argv(g, dry_run=True)]
        self.app.push_screen(_BringupModal(argv))

    def _begin(self) -> None:
        g = self._gather()
        missing = self._missing_required(g)
        status = self.query_one("#gf-status", Static)
        if missing:
            status.update(
                f"[yellow]Fill in: {', '.join(missing)} "
                f"(required for the {g['profile']} profile)[/]")
            return

        is_local = self._profile_is_local(g["profile"])
        clients = ", ".join(self._profile_clients(g["profile"])) or "(none)"
        body = (
            f"Install, configure, and start the [bold]{g['profile']}[/] station "
            f"({'LOCAL' if is_local else 'REMOTE'} radiod).\n\n"
            f"  clients:  {clients}\n"
            f"  reporter: {g['reporter'] or '—'}\n"
            f"  grid:     {g['grid'] or '—'}\n"
            f"  callsign: {g['callsign'] or '—'}\n\n"
            "This runs the full bring-up and may take a while (FFT wisdom can "
            "take minutes — or hours on a cold first build).  radiod is "
            "configured with [bold]antenna defaults[/]; fine-tune the antenna "
            "afterwards with the verdict's [bold]Edit antenna[/] action.")

        def _after_confirm(ok: bool) -> None:
            if ok:
                self._run_bringup(g)

        self.app.push_screen(
            ConfirmModal(title=f"Begin {g['profile']} bring-up?", body=body,
                         yes_label="Begin", yes_variant="success"),
            _after_confirm,
        )

    def _ensure_sudo(self) -> bool:
        """Make sure ``sudo -n`` will succeed for the streamed run.

        A streamed (captured) child can't field a password prompt, so we
        pre-cache credentials here on the main thread: try ``sudo -n true``;
        if it needs a password, suspend once and run ``sudo -v`` so the
        operator authenticates in the real terminal.  Returns True when the
        run can proceed passwordless from here on.
        """
        fast = subprocess.run(["sudo", "-n", "true"],
                              capture_output=True, text=True)
        if fast.returncode == 0:
            return True
        with self.app.suspend():
            print("\nsigmond needs administrator rights to bring up the "
                  "station.\n")
            r = subprocess.run(["sudo", "-v"])
        if r.returncode != 0:
            self.query_one("#gf-status", Static).update(
                "[red]Could not get administrator rights — bring-up "
                "cancelled.[/]")
            return False
        return True

    def _run_bringup(self, g: dict) -> None:
        if not self._ensure_sudo():
            return
        # Pre-elevate: when euid==0 smd's _need_root returns without prompting,
        # and the SIGMOND_ALLOW_SUDO marker satisfies its top-of-main guard.
        # PYTHONUNBUFFERED=1 forces smd's own progress lines (the parent
        # process's stdout) to flush per-line; without it Python block-buffers
        # them on the captured pipe and the stage/checkpoint scaffolding only
        # appears in one delayed dump at the end (confirmed on a live run).
        argv = ["sudo", "-n", "--", "env", "SIGMOND_ALLOW_SUDO=1",
                "PYTHONUNBUFFERED=1", *self._build_argv(g, dry_run=False)]

        def _after_run(rc) -> None:
            self._render_verdict(rc, g)

        self.app.push_screen(_BringupModal(argv), _after_run)

    def _render_verdict(self, rc, g: dict) -> None:
        status = self.query_one("#gf-status", Static)
        fixits = self.query_one("#gf-fixits")
        if rc == 0:
            status.update(
                f"[green]✔ {g['profile']} station brought up.[/]  "
                "Open [bold]Validate[/] for the full health report.  "
                "radiod uses antenna defaults — set your real antenna with "
                "[bold]Edit antenna[/].")
        else:
            status.update(
                f"[yellow]⚠ bring-up finished with issues (exit {rc}).[/]  "
                "Review the log above, then try [bold]Open Validate[/] or "
                "[bold]Logs[/] to see what needs attention.")
        fixits.add_class("show")
        # Refresh the app's system view so the tree / Overview reflect the
        # now-running station.
        try:
            self.app._load_system_view()
        except Exception:                                    # noqa: BLE001
            pass

    def _edit_antenna(self) -> None:
        # config edit is interactive (radiod's own wizard / $EDITOR), so this
        # one suspends to the real terminal — unlike the streamed bring-up.
        suspend_and_run_sudo(self.app, [_smd_binary(), "config", "edit", "radiod"])
