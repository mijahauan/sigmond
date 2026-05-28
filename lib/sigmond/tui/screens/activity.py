"""Activity screen — TUI counterpart to `smd watch <target>` (CLI-V2-SPEC.md §3 Observation).

Live-tail of per-target recorder + uploader + verifier activity.  One
screen with a target selector instead of seven per-target screens —
mirrors the CLI's single `watch` verb with sub-targets.

Subprocess management modeled on logs.py: spawn `smd watch <target>`
with line-buffered stdout, stream into a RichLog, terminate on Stop
or screen unmount.  At most one subprocess per screen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, RichLog, Select, Static, Switch
from textual.worker import get_current_worker

try:
    from ...instance import display_reporter_id as _display_reporter_id
except ImportError:
    def _display_reporter_id(rid: str) -> str:
        return rid.replace("=", "/")


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/sbin/smd'


# Meta-watchers — not per-client, so they don't (and can't) declare
# themselves via the drop-in `[client_features.watch]` block.  Keep
# hardcoded; merged below with the per-client list.
_META_WATCH_TARGETS = [
    ("ka9q",     "ka9q-radio upstream drift check"),
    ("uploads",  "All upload activity (WSPRnet + wsprdaemon.org + PSK Reporter)"),
    ("verifier", "wsprnet upload-then-verify audit (lost / in-flight / delivered)"),
]


def _build_target_tables() -> tuple[list[tuple[str, str]], dict[str, str], frozenset[str]]:
    """Resolve (WATCH_TARGETS, _TARGET_TO_CLIENT, _VERBOSE_CAPABLE_TARGETS)
    from per-client `[client_features.watch]` declarations plus the
    hardcoded meta-watcher set.

    This is the drop-in seam: every contract-conformant client whose
    deploy.toml ships a `[client_features.watch]` block appears in the
    Activity dropdown automatically — no edits here required for a new
    client.  See lib/sigmond/client_features.py.

    Resolved once at module import (TUI restart picks up new clients);
    if the loader fails for any reason we degrade to the meta-only set.
    """
    targets: list[tuple[str, str]] = []
    target_to_client: dict[str, str] = {}
    verbose_capable: set[str] = set()
    try:
        from ...client_features import load_watch_features
        for f in load_watch_features():
            targets.append((f.verb, f.description))
            if f.per_instance:
                target_to_client[f.verb] = f.client
            if f.verbose:
                verbose_capable.add(f.verb)
    except Exception:
        # Loader is best-effort; fall back to meta-only rather than
        # crash the whole TUI on a malformed deploy.toml somewhere.
        pass
    targets.extend(_META_WATCH_TARGETS)
    return targets, target_to_client, frozenset(verbose_capable)


WATCH_TARGETS, _TARGET_TO_CLIENT, _VERBOSE_CAPABLE_TARGETS = _build_target_tables()

# Sentinel value for the "no instance filter" choice in the instance
# dropdown.  Selecting this means we don't pass --instance to smd watch.
_INSTANCE_ALL = "__all__"


class ActivityScreen(Vertical):
    """Live tail of `smd watch <target>` output."""

    DEFAULT_CSS = """
    ActivityScreen {
        padding: 1;
    }
    ActivityScreen .ac-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ActivityScreen .ac-body {
        margin-bottom: 1;
        color: $text-muted;
    }
    ActivityScreen #ac-controls {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }
    ActivityScreen #ac-controls Button {
        margin-right: 1;
    }
    ActivityScreen #ac-target {
        width: 60;
        margin-right: 2;
    }
    ActivityScreen #ac-instance {
        width: 30;
        margin-right: 2;
    }
    ActivityScreen .ac-verbose-label {
        margin-left: 2;
        padding-top: 1;
        width: 4;
        color: $text-muted;
    }
    ActivityScreen #ac-verbose {
        margin-right: 1;
    }
    ActivityScreen #ac-output {
        height: 24;
        border: solid $primary-background;
    }
    ActivityScreen #ac-last {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._proc: Optional[subprocess.Popen] = None

    @staticmethod
    def _instance_options_for(target: str) -> list:
        """Build the (label, value) list for the instance Select widget.

        For per-recorder targets (wspr/psk/hfdl/codar), enumerate
        configured per-instance reporter IDs and known legacy radiod-
        keyed names.  For meta targets, return a single disabled-shape
        sentinel.
        """
        client = _TARGET_TO_CLIENT.get(target)
        if client is None:
            return [("(no instance dimension)", _INSTANCE_ALL)]
        options: list = [("(all instances)", _INSTANCE_ALL)]
        try:
            from ...instance import (
                list_instances, detect_migration_candidates,
            )
        except Exception:
            return options
        # Configured per-instance reporter IDs
        for i in list_instances(catalog_clients=[client]):
            options.append((i.reporter_id, i.reporter_id))
        # Legacy radiod-keyed instances (unmigrated yet) — useful since
        # smd watch can still target them by name.
        try:
            for c in detect_migration_candidates():
                if c.client == client:
                    label = f"{c.old_instance} (legacy)"
                    options.append((label, c.old_instance))
        except Exception:
            pass
        return options

    def compose(self):
        yield Static("Activity — live tail by target", classes="ac-title")
        yield Static(
            "Per-target live view of recorder, uploader, and verifier "
            "activity.  Equivalent to running `smd watch <target>` in a "
            "terminal; one subprocess per screen.",
            classes="ac-body")
        with Horizontal(id="ac-controls"):
            yield Select(
                [(label, target) for target, label in
                 ((t, f"{t}  —  {desc}") for t, desc in WATCH_TARGETS)],
                value="wspr",
                id="ac-target",
                allow_blank=False,
            )
            # Per-instance dropdown (sigmond MULTI-INSTANCE-ARCHITECTURE
            # §3 / §8) — populated on target change.  For meta targets
            # (ka9q / uploads / verifier) the dropdown is disabled.
            yield Select(
                self._instance_options_for("wspr"),
                value=_INSTANCE_ALL,
                id="ac-instance",
                allow_blank=False,
            )
            yield Button("Start", id="ac-start", variant="primary")
            yield Button("Stop", id="ac-stop", variant="warning", disabled=True)
            yield Button("Clear", id="ac-clear", variant="default")
            # Verbose toggle — appends `-v` to the spawned `smd
            # watch <target>` when the target supports it
            # (wspr / psk / hfdl / codar / mag).  No-op for the
            # meta-target watches (ka9q / uploads / verifier) whose
            # event stream is already the canonical detail.
            yield Static("-v", classes="ac-verbose-label")
            yield Switch(value=False, id="ac-verbose")
        yield RichLog(id="ac-output", highlight=False, markup=False,
                      max_lines=5000, wrap=False)
        yield Static("[dim]idle — pick a target and press Start[/]",
                     id="ac-last", markup=True)

    def on_unmount(self) -> None:
        # Always tear down the subprocess when the screen goes away.
        self._stop(user_requested=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ac-start":
            self._start()
        elif event.button.id == "ac-stop":
            self._stop(user_requested=True)
        elif event.button.id == "ac-clear":
            self.query_one("#ac-output", RichLog).clear()

    def on_select_changed(self, event: Select.Changed) -> None:
        """Repopulate the instance dropdown when the target changes."""
        if event.select.id != "ac-target":
            return
        target = str(event.value) if event.value is not None else ""
        if not target or target == Select.BLANK:
            return
        instance_sel = self.query_one("#ac-instance", Select)
        instance_sel.set_options(self._instance_options_for(target))
        instance_sel.value = _INSTANCE_ALL

    def _start(self) -> None:
        # Stop any in-flight watch first.  Switching target while one
        # is running implicitly replaces it.
        self._stop(user_requested=False)

        target = self.query_one("#ac-target", Select).value
        if not target or target == Select.BLANK:
            self.query_one("#ac-last", Static).update(
                "[red]no target selected[/]")
            return

        cmd = [_smd_binary(), 'watch', str(target)]
        # Per-instance filter (sigmond MULTI-INSTANCE-ARCHITECTURE.md §8).
        # Only applies to per-recorder targets (meta targets ignore).
        instance_val = self.query_one("#ac-instance", Select).value
        if (target in _TARGET_TO_CLIENT
                and instance_val not in (None, Select.BLANK, _INSTANCE_ALL)):
            cmd += ['--instance', str(instance_val)]
        # Verbose toggle — only append for targets that accept `-v`.
        # The Switch can stay on as the operator changes targets; we
        # just silently drop the flag when the new target doesn't
        # support it instead of raising an error or auto-toggling.
        if (target in _VERBOSE_CAPABLE_TARGETS
                and self.query_one("#ac-verbose", Switch).value):
            cmd += ['-v']
        log = self.query_one("#ac-output", RichLog)
        log.write(f"$ {' '.join(cmd)}")
        self.query_one("#ac-last", Static).update(
            f"[dim]streaming {target}…[/]")
        self.query_one("#ac-start", Button).disabled = True
        self.query_one("#ac-stop", Button).disabled = False
        self.run_worker(lambda: self._tail(cmd), thread=True, name="ac-tail")

    def _tail(self, cmd: list) -> None:
        """Worker body — stream subprocess stdout into the RichLog widget.

        stderr is merged into stdout so launch failures surface in the
        same pane.  Exits when the subprocess closes stdout or when the
        worker is cancelled (Stop / screen unmount).
        """
        worker = get_current_worker()
        log = self.query_one("#ac-output", RichLog)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.app.call_from_thread(
                log.write, f"[error launching {cmd[0]}: {exc}]")
            self.app.call_from_thread(self._reset_buttons)
            return

        try:
            for line in iter(self._proc.stdout.readline, ''):
                if worker.is_cancelled:
                    break
                self.app.call_from_thread(log.write, line.rstrip('\n'))
        finally:
            try:
                if self._proc and self._proc.stdout:
                    self._proc.stdout.close()
            except Exception:
                pass
            rc = None
            try:
                if self._proc:
                    rc = self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            self._proc = None
            self.app.call_from_thread(
                log.write,
                f"[exit {rc if rc is not None else 'terminated'}]")
            self.app.call_from_thread(self._reset_buttons)

    def _reset_buttons(self) -> None:
        """Re-enable Start, disable Stop — called when the worker exits."""
        try:
            self.query_one("#ac-start", Button).disabled = False
            self.query_one("#ac-stop", Button).disabled = True
            self.query_one("#ac-last", Static).update("[dim]idle[/]")
        except Exception:
            # Screen already unmounted — buttons gone; safe to ignore.
            pass

    def _stop(self, user_requested: bool) -> None:
        """Terminate the watch subprocess if any; cancel the worker."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except Exception:
                    pass

        # Cancel any running tail worker on this screen.
        try:
            for w in list(self.workers):
                if w.name == "ac-tail":
                    w.cancel()
        except Exception:
            pass

        if user_requested:
            try:
                self.query_one("#ac-last", Static).update(
                    "[dim]stopped[/]")
            except Exception:
                pass
