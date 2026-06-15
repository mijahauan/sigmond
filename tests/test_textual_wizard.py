"""Tests for the in-TUI Textual config wizard.

Covers the two pieces of glue between the JSON contract and the
Textual widgets:

  - ``load_config_via_show`` (subprocess shape, JSON parse, error paths)
  - ``load_help_toml`` (per-client help sidecar discovery + parse)
  - ``help_label`` (title-or-fallback lookup)
  - the ``TextualConfigWizardScreen`` happy path (loads → edits → saves)
  - cancel path (no save)
  - apply-failure rendering (stderr surfaced, modal stays open)
  - dirty-only payload (only changed leaves are sent)
  - ``[[radiod]]`` per-block scalar editing + full-array rebuild on save

The subprocess layer is patched in every test; this file runs
unprivileged and never touches /etc/.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

try:
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


# ---------------------------------------------------------------------------
# load_config_via_show — straight subprocess glue, no Textual needed.
# ---------------------------------------------------------------------------

class LoadConfigViaShowTests(unittest.TestCase):

    def _make_proc(self, *, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr,
        )

    def test_success_returns_parsed_dict(self):
        from sigmond.tui.screens.textual_wizard import load_config_via_show
        canned = {"station": {"callsign": "W1ABC", "grid_square": "FN42"}}
        with mock.patch("subprocess.run",
                        return_value=self._make_proc(stdout=json.dumps(canned))):
            data, err = load_config_via_show("/usr/bin/fake")
        self.assertEqual(err, "")
        self.assertEqual(data, canned)

    def test_nonzero_exit_returns_error(self):
        from sigmond.tui.screens.textual_wizard import load_config_via_show
        with mock.patch("subprocess.run",
                        return_value=self._make_proc(returncode=2,
                                                     stderr="boom\n")):
            data, err = load_config_via_show("/usr/bin/fake")
        self.assertIsNone(data)
        self.assertIn("exited 2", err)
        self.assertIn("boom", err)

    def test_bad_json_returns_error(self):
        from sigmond.tui.screens.textual_wizard import load_config_via_show
        with mock.patch("subprocess.run",
                        return_value=self._make_proc(stdout="not json")):
            data, err = load_config_via_show("/usr/bin/fake")
        self.assertIsNone(data)
        self.assertIn("not JSON", err)

    def test_non_object_top_level_returns_error(self):
        from sigmond.tui.screens.textual_wizard import load_config_via_show
        with mock.patch("subprocess.run",
                        return_value=self._make_proc(stdout="[1,2,3]")):
            data, err = load_config_via_show("/usr/bin/fake")
        self.assertIsNone(data)
        self.assertIn("expected object", err)

    def test_oserror_returns_error(self):
        from sigmond.tui.screens.textual_wizard import load_config_via_show
        with mock.patch("subprocess.run", side_effect=OSError("no such file")):
            data, err = load_config_via_show("/usr/bin/missing")
        self.assertIsNone(data)
        self.assertIn("failed to exec", err)

    def test_config_path_appends_arg(self):
        """When config_path is given, ``--config <path>`` lands in argv
        (per MULTI-INSTANCE-ARCHITECTURE.md §4 per-instance files)."""
        from sigmond.tui.screens.textual_wizard import load_config_via_show
        captured = {}
        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="{}", stderr="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            data, err = load_config_via_show(
                "/usr/bin/fake", config_path="/etc/psk-recorder/AC0G-B1.toml")
        self.assertEqual(err, "")
        self.assertEqual(data, {})
        self.assertEqual(
            captured["argv"],
            ["/usr/bin/fake", "config", "show", "--json", "--defaults",
             "--config", "/etc/psk-recorder/AC0G-B1.toml"],
        )

    def test_omitted_config_path_leaves_argv_clean(self):
        """Default (None) → argv is the bare 5-element form so the
        client picks its own default config."""
        from sigmond.tui.screens.textual_wizard import load_config_via_show
        captured = {}
        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="{}", stderr="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            load_config_via_show("/usr/bin/fake")
        self.assertNotIn("--config", captured["argv"])


# ---------------------------------------------------------------------------
# Screen happy-path: load → edit → save.  Uses Textual's run_test pilot.
# ---------------------------------------------------------------------------

CANNED_SHOW = {
    "station": {"callsign": "W1ABC", "grid_square": "FN42"},
    "paths":   {"spool_dir": "/var/lib/psk-recorder",
                "keep_wav": False},
    "processing": {"radiod_lifetime_frames": 6000},
    "radiod": [
        {"id": "rx1", "radiod_status": "rx1-status.local"},
    ],
}

# Multi-instance fixture: two [[radiod]] blocks, each with nested
# sub-tables (ft4/ft8 with freqs_hz lists) that should NOT be rendered
# editable but MUST survive a save round-trip byte-for-byte.
CANNED_SHOW_MULTI_RADIOD = {
    "station": {"callsign": "W1ABC", "grid_square": "FN42"},
    "radiod": [
        {
            "id": "rx1",
            "radiod_status": "rx1-status.local",
            "ft8": {"freqs_hz": [7074000, 14074000], "preset": "usb",
                    "sample_rate": 12000, "encoding": "s16be"},
            "ft4": {"freqs_hz": [7047500], "preset": "usb",
                    "sample_rate": 12000, "encoding": "s16be"},
        },
        {
            "id": "rx2",
            "radiod_status": "rx2-status.local",
            "ft8": {"freqs_hz": [10136000, 21074000], "preset": "usb",
                    "sample_rate": 12000, "encoding": "s16be"},
        },
    ],
}


@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class TextualConfigWizardScreenTests(unittest.IsolatedAsyncioTestCase):

    async def _launch(self, *, load_result=(CANNED_SHOW, ""),
                      apply_runner=None):
        """Spin up a Textual harness that pushes the wizard on mount.

        Returns (app, results-list).  Tests use app.run_test() as a
        context manager — wait for the load worker to finish via
        pilot.pause(), then drive the screen.
        """
        from textual.app import App
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                    ),
                    lambda saved: results.append(saved),
                )

        captured_payloads: list[bytes] = []

        def default_apply_runner(app_, cmd, stdin_bytes, sudo):
            captured_payloads.append(stdin_bytes)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=b"", stderr=b"",
            )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=load_result), \
             mock.patch.object(tw, "run_with_stdin",
                               side_effect=apply_runner or default_apply_runner):
            yield_obj = (Harness(), results, captured_payloads)
            self._yield = yield_obj
            return yield_obj

    async def test_loads_and_renders_form(self):
        """Subprocess returns canned JSON → scalar widgets appear with
        their initial values populated."""
        from textual.app import App
        from textual.widgets import Input, Switch
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=(CANNED_SHOW, "")):
            app = Harness()
            async with app.run_test(size=(120, 40)) as pilot:
                # Wait for on_mount + worker dispatch.
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                # Loader filled in the placeholder; scroll has section
                # headers + Inputs/Switch by now.
                callsign = modal.query_one("#tw-fld-station-callsign", Input)
                self.assertEqual(callsign.value, "W1ABC")
                grid = modal.query_one("#tw-fld-station-grid_square", Input)
                self.assertEqual(grid.value, "FN42")
                # paths.spool_dir is help.toml-hidden (install-canonical path),
                # so the wizard intentionally does NOT render it.  keep_wav is
                # an operator knob and still renders.
                self.assertEqual(len(modal.query("#tw-fld-paths-spool_dir")), 0)
                keep_wav = modal.query_one("#tw-fld-paths-keep_wav", Switch)
                self.assertFalse(keep_wav.value)
                frames = modal.query_one(
                    "#tw-fld-processing-radiod_lifetime_frames", Input)
                self.assertEqual(frames.value, "6000")

    async def test_save_pipes_dirty_subset_only(self):
        """Change only ``station.callsign`` → only that field reaches
        ``config apply --json -``.  Untouched leaves stay out of the payload."""
        from textual.app import App
        from textual.widgets import Input
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []
        captured_payloads: list[bytes] = []
        captured_cmds: list = []

        def fake_apply(app_, cmd, stdin_bytes, sudo):
            captured_cmds.append((list(cmd), sudo))
            captured_payloads.append(stdin_bytes)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=b"", stderr=b"",
            )

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=(CANNED_SHOW, "")), \
             mock.patch.object(tw, "run_with_stdin",
                               side_effect=fake_apply):
            app = Harness()
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                callsign = modal.query_one("#tw-fld-station-callsign", Input)
                callsign.value = "K1XYZ"
                await pilot.pause()
                modal.query_one("#tw-save").press()
                for _ in range(10):
                    await pilot.pause()

        self.assertEqual(results, [True], "dismiss(True) on apply success")
        self.assertEqual(len(captured_payloads), 1)
        payload = json.loads(captured_payloads[0].decode("utf-8"))
        self.assertEqual(payload, {"station": {"callsign": "K1XYZ"}},
                         "only the dirty leaf is in the payload")
        self.assertEqual(captured_cmds[0][0],
                         ["/usr/bin/fake-psk", "config", "apply",
                          "--json", "-"])
        self.assertTrue(captured_cmds[0][1], "apply runs under sudo=True")

    async def test_save_with_no_changes_is_noop(self):
        """Save without editing → no subprocess call, status shows hint,
        screen stays open."""
        from textual.app import App
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []
        captured_payloads: list[bytes] = []

        def fake_apply(app_, cmd, stdin_bytes, sudo):
            captured_payloads.append(stdin_bytes)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=b"", stderr=b"")

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=(CANNED_SHOW, "")), \
             mock.patch.object(tw, "run_with_stdin",
                               side_effect=fake_apply):
            app = Harness()
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                modal.query_one("#tw-save").press()
                for _ in range(5):
                    await pilot.pause()

        self.assertEqual(captured_payloads, [],
                         "apply not called when nothing changed")
        self.assertEqual(results, [],
                         "screen stayed open (no dismiss)")

    async def test_cancel_discards(self):
        """Cancel → dismiss(False), no apply call, even after editing."""
        from textual.app import App
        from textual.widgets import Input
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []
        captured_payloads: list[bytes] = []

        def fake_apply(app_, cmd, stdin_bytes, sudo):
            captured_payloads.append(stdin_bytes)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=b"", stderr=b"")

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=(CANNED_SHOW, "")), \
             mock.patch.object(tw, "run_with_stdin",
                               side_effect=fake_apply):
            app = Harness()
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                modal.query_one("#tw-fld-station-callsign", Input).value = "K1XYZ"
                await pilot.pause()
                modal.query_one("#tw-cancel").press()
                await pilot.pause()

        self.assertEqual(captured_payloads, [])
        self.assertEqual(results, [False])

    async def test_apply_failure_keeps_modal_open(self):
        """``config apply`` exits nonzero → stderr rendered, modal stays
        open, Save button re-enabled so the operator can fix and retry."""
        from textual.app import App
        from textual.widgets import Input, Static, Button
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []

        def fake_apply(app_, cmd, stdin_bytes, sudo):
            return subprocess.CompletedProcess(
                args=cmd, returncode=2, stdout=b"",
                stderr=b"config apply: section(s) not writable via apply: ['station']\n",
            )

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=(CANNED_SHOW, "")), \
             mock.patch.object(tw, "run_with_stdin",
                               side_effect=fake_apply):
            app = Harness()
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                modal.query_one("#tw-fld-station-callsign", Input).value = "K1XYZ"
                await pilot.pause()
                modal.query_one("#tw-save").press()
                for _ in range(10):
                    await pilot.pause()

                self.assertEqual(results, [],
                                 "modal stayed open after apply failure")
                status_text = str(modal.query_one("#tw-status", Static).render())
                self.assertIn("exited 2", status_text)
                self.assertIn("section(s) not writable", status_text)
                self.assertFalse(
                    modal.query_one("#tw-save", Button).disabled,
                    "Save button re-enabled for retry",
                )

    async def test_loader_error_renders_inline(self):
        """``config show`` fails → error rendered in the form area; no
        widgets appear; Save stays disabled."""
        from textual.app import App
        from textual.widgets import Button
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=(None, "boom: command not found")):
            app = Harness()
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                self.assertTrue(modal.query_one("#tw-save", Button).disabled)


# ---------------------------------------------------------------------------
# help.toml — load + label lookup.
# ---------------------------------------------------------------------------

class LoadHelpTomlTests(unittest.TestCase):

    def test_returns_empty_when_absent(self):
        from sigmond.tui.screens import textual_wizard as tw
        with mock.patch.object(
            tw, "_help_toml_candidates",
            return_value=[Path("/nonexistent/help.toml")],
        ):
            self.assertEqual(tw.load_help_toml("psk-recorder"), {})

    def test_parses_valid_toml(self):
        from sigmond.tui.screens import textual_wizard as tw
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "help.toml"
            p.write_text(textwrap.dedent("""\
                [station.callsign]
                title = "Amateur callsign"
                help = "your call"
                required = true

                [radiod.id]
                title = "Radiod block id"
                """))
            with mock.patch.object(
                tw, "_help_toml_candidates", return_value=[p],
            ):
                data = tw.load_help_toml("psk-recorder")
        self.assertEqual(data["station"]["callsign"]["title"],
                         "Amateur callsign")
        self.assertTrue(data["station"]["callsign"]["required"])
        self.assertEqual(data["radiod"]["id"]["title"], "Radiod block id")

    def test_swallows_parse_errors(self):
        """A broken help.toml must not block the wizard — operator help
        is a UX nicety, not a contract dependency."""
        from sigmond.tui.screens import textual_wizard as tw
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "help.toml"
            p.write_text("this is not valid TOML = = =")
            with mock.patch.object(
                tw, "_help_toml_candidates", return_value=[p],
            ):
                self.assertEqual(tw.load_help_toml("x"), {})


class InstanceTagFromPathTests(unittest.TestCase):

    def test_extracts_reporter_id(self):
        from sigmond.tui.screens.textual_wizard import TextualConfigWizardScreen
        self.assertEqual(
            TextualConfigWizardScreen._instance_tag_from_path(
                "/etc/psk-recorder/AC0G-B1.toml"),
            "AC0G-B1",
        )
        self.assertEqual(
            TextualConfigWizardScreen._instance_tag_from_path(
                "/etc/wspr-recorder/W4UK-WEST.toml"),
            "W4UK-WEST",
        )

    def test_legacy_shared_config_returns_empty(self):
        """Legacy shared paths shouldn't render as if they were a
        reporter id."""
        from sigmond.tui.screens.textual_wizard import TextualConfigWizardScreen
        self.assertEqual(
            TextualConfigWizardScreen._instance_tag_from_path(
                "/etc/psk-recorder/psk-recorder-config.toml"),
            "",
        )
        self.assertEqual(
            TextualConfigWizardScreen._instance_tag_from_path(
                "/etc/wspr-recorder/config.toml"),
            "",
        )


class HelpLabelTests(unittest.TestCase):

    def test_returns_title_when_present(self):
        from sigmond.tui.screens.textual_wizard import help_label
        help_data = {"station": {"callsign": {"title": "Amateur callsign"}}}
        self.assertEqual(help_label(help_data, "station", "callsign"),
                         "Amateur callsign")

    def test_falls_back_to_bare_key(self):
        from sigmond.tui.screens.textual_wizard import help_label
        self.assertEqual(help_label({}, "station", "callsign"), "callsign")

    def test_falls_back_when_section_missing(self):
        from sigmond.tui.screens.textual_wizard import help_label
        self.assertEqual(
            help_label({"other": {}}, "station", "callsign"),
            "callsign",
        )

    def test_falls_back_when_title_blank(self):
        from sigmond.tui.screens.textual_wizard import help_label
        self.assertEqual(
            help_label({"s": {"k": {"title": ""}}}, "s", "k"),
            "k",
        )


# ---------------------------------------------------------------------------
# Multi-instance [[radiod]] editing.
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAS_TEXTUAL, "textual not installed")
class RadiodBlockEditingTests(unittest.IsolatedAsyncioTestCase):

    async def test_each_block_renders_with_array_field_ids(self):
        """Two [[radiod]] blocks → ``tw-arr-radiod-{0,1}-{id,radiod_status}``
        widgets exist; nested ft4/ft8 sub-tables are NOT rendered as
        editable widgets."""
        from textual.app import App
        from textual.widgets import Input
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=(CANNED_SHOW_MULTI_RADIOD, "")):
            app = Harness()
            async with app.run_test(size=(140, 50)) as pilot:
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                # Block 0
                self.assertEqual(
                    modal.query_one("#tw-arr-radiod-0-id", Input).value, "rx1")
                self.assertEqual(
                    modal.query_one("#tw-arr-radiod-0-radiod_status",
                                    Input).value,
                    "rx1-status.local")
                # Block 1
                self.assertEqual(
                    modal.query_one("#tw-arr-radiod-1-id", Input).value, "rx2")
                self.assertEqual(
                    modal.query_one("#tw-arr-radiod-1-radiod_status",
                                    Input).value,
                    "rx2-status.local")
                # ft8 inside a block must NOT have a widget — array-of-
                # tables sub-tables are stay-out-of-the-TUI by design.
                from textual.css.query import NoMatches
                with self.assertRaises(NoMatches):
                    modal.query_one("#tw-arr-radiod-0-ft8")

    async def test_instance_reporter_id_renders_read_only(self):
        """``[instance].reporter_id`` MUST match the filename per
        MULTI-INSTANCE-ARCHITECTURE.md §5 — the wizard renders it as
        a read-only display, never an Input.  Editing it from inside
        the form would orphan the file and the daemon's load-time
        sanity check would reject it."""
        from textual.app import App
        from textual.css.query import NoMatches
        from textual.widgets import Input, Static
        from sigmond.tui.screens import textual_wizard as tw

        per_instance_show = {
            "instance": {"reporter_id": "AC0G-B1"},
            "station":  {"callsign": "AC0G/B1", "grid_square": "EM38ww"},
        }
        results: list = []

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                        config_path="/etc/psk-recorder/AC0G-B1.toml",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=(per_instance_show, "")):
            app = Harness()
            async with app.run_test(size=(140, 50)) as pilot:
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                with self.assertRaises(NoMatches):
                    modal.query_one("#tw-fld-instance-reporter_id", Input)
                # The value still shows up somewhere in the rendered
                # form (operators need to know which instance they're
                # editing).
                rendered = " ".join(
                    str(s.render()) for s in modal.query(Static))
                self.assertIn("AC0G-B1", rendered)
                self.assertIn("locked", rendered)

    async def test_apply_threads_config_path_through_per_instance(self):
        """When the screen is constructed with ``config_path``, BOTH
        the loader's ``config show`` and the saver's ``config apply``
        get ``--config <path>`` appended — per MULTI-INSTANCE-
        ARCHITECTURE.md §4, the operator's intent is to edit one
        per-instance file."""
        from textual.app import App
        from textual.widgets import Input
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []
        captured_cmds: list = []
        captured_show_args: list = []

        def fake_show(client_bin, config_path=None):
            captured_show_args.append((client_bin, config_path))
            return CANNED_SHOW, ""

        def fake_apply(app_, cmd, stdin_bytes, sudo):
            captured_cmds.append(list(cmd))
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=b"", stderr=b"")

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                        config_path="/etc/psk-recorder/AC0G-B1.toml",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               side_effect=fake_show), \
             mock.patch.object(tw, "run_with_stdin",
                               side_effect=fake_apply):
            app = Harness()
            async with app.run_test(size=(140, 50)) as pilot:
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                modal.query_one(
                    "#tw-fld-station-callsign", Input).value = "AC0G/B1"
                await pilot.pause()
                modal.query_one("#tw-save").press()
                for _ in range(10):
                    await pilot.pause()

        self.assertEqual(results, [True])
        # Show was called with the per-instance path.
        self.assertEqual(
            captured_show_args,
            [("/usr/bin/fake-psk", "/etc/psk-recorder/AC0G-B1.toml")],
        )
        # Apply argv threaded the per-instance path too.
        self.assertEqual(len(captured_cmds), 1)
        argv = captured_cmds[0]
        self.assertIn("--config", argv)
        self.assertIn("/etc/psk-recorder/AC0G-B1.toml", argv)
        # ``-`` (stdin) must be the trailing positional.
        self.assertEqual(argv[-1], "-")

    async def test_editing_one_block_sends_full_list_with_others_intact(self):
        """Edit rx2's ``radiod_status`` → payload contains the full
        2-element ``radiod`` list, with rx1 byte-identical and rx2's
        radiod_status updated.  Nested ft4/ft8 sub-tables survive
        unchanged."""
        from textual.app import App
        from textual.widgets import Input
        from sigmond.tui.screens import textual_wizard as tw

        results: list = []
        captured_payloads: list[bytes] = []

        def fake_apply(app_, cmd, stdin_bytes, sudo):
            captured_payloads.append(stdin_bytes)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=b"", stderr=b"")

        class Harness(App):
            async def on_mount(self):
                self.push_screen(
                    tw.TextualConfigWizardScreen(
                        client_name="psk-recorder",
                        client_bin="/usr/bin/fake-psk",
                    ),
                    lambda saved: results.append(saved),
                )

        with mock.patch.object(tw, "load_config_via_show",
                               return_value=(CANNED_SHOW_MULTI_RADIOD, "")), \
             mock.patch.object(tw, "run_with_stdin",
                               side_effect=fake_apply):
            app = Harness()
            async with app.run_test(size=(140, 50)) as pilot:
                for _ in range(10):
                    await pilot.pause()
                modal = app.screen
                rx2_status = modal.query_one(
                    "#tw-arr-radiod-1-radiod_status", Input)
                rx2_status.value = "rx2-status-new.local"
                await pilot.pause()
                modal.query_one("#tw-save").press()
                for _ in range(10):
                    await pilot.pause()

        self.assertEqual(results, [True])
        self.assertEqual(len(captured_payloads), 1)
        payload = json.loads(captured_payloads[0].decode("utf-8"))
        # The payload must include the FULL radiod list (overlay-wins
        # would drop any block left out).
        self.assertIn("radiod", payload)
        self.assertEqual(len(payload["radiod"]), 2)
        # rx1 untouched, including its ft4/ft8 nested tables.
        self.assertEqual(payload["radiod"][0]["id"], "rx1")
        self.assertEqual(payload["radiod"][0]["radiod_status"],
                         "rx1-status.local")
        self.assertEqual(payload["radiod"][0]["ft8"]["freqs_hz"],
                         [7074000, 14074000])
        self.assertEqual(payload["radiod"][0]["ft4"]["freqs_hz"],
                         [7047500])
        # rx2 has the edit applied; its ft8 sub-table is preserved.
        self.assertEqual(payload["radiod"][1]["id"], "rx2")
        self.assertEqual(payload["radiod"][1]["radiod_status"],
                         "rx2-status-new.local")
        self.assertEqual(payload["radiod"][1]["ft8"]["freqs_hz"],
                         [10136000, 21074000])
        # No station edits ⇒ no station key in the payload (dirty-only).
        self.assertNotIn("station", payload)


# ---------------------------------------------------------------------------
# run_with_stdin — sudo fast-path / non-sudo path.
# ---------------------------------------------------------------------------

class RunWithStdinTests(unittest.TestCase):
    def test_non_sudo_pipes_stdin_and_returns(self):
        from sigmond.tui.mutation import run_with_stdin
        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"ok", stderr=b"")
            result = run_with_stdin(
                app=mock.Mock(),
                cmd=["/usr/bin/echo", "hi"],
                stdin_bytes=b"payload",
                sudo=False,
            )
        self.assertEqual(result.returncode, 0)
        called_kwargs = run_mock.call_args.kwargs
        self.assertEqual(called_kwargs["input"], b"payload")
        self.assertTrue(called_kwargs["capture_output"])

    def test_sudo_fast_path_when_nopasswd(self):
        """``sudo -n`` returns 0 → that's the final result, no fallback."""
        from sigmond.tui.mutation import run_with_stdin
        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b"")
            result = run_with_stdin(
                app=mock.Mock(),
                cmd=["/usr/bin/whatever"],
                stdin_bytes=b"data",
                sudo=True,
            )
        self.assertEqual(result.returncode, 0)
        # sudo -n was prepended.
        argv = run_mock.call_args.args[0]
        self.assertEqual(argv[:2], ["sudo", "-n"])
        self.assertEqual(run_mock.call_count, 1)

    def test_sudo_failure_unrelated_to_password_does_not_suspend(self):
        """``sudo -n`` returns nonzero with NO 'password required' marker
        → that's a real failure, not a missing password.  Should NOT
        fall back to suspended mode."""
        from sigmond.tui.mutation import run_with_stdin
        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=[], returncode=2, stdout=b"",
                stderr=b"some other failure")
            mock_app = mock.Mock()
            result = run_with_stdin(
                app=mock_app, cmd=["/usr/bin/fake"],
                stdin_bytes=b"x", sudo=True,
            )
        self.assertEqual(result.returncode, 2)
        # Only the sudo -n call happened; no app.suspend() invoked.
        self.assertEqual(run_mock.call_count, 1)
        mock_app.suspend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
