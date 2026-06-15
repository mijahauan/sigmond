"""Headless operator smoke for the sigmond TUI.

Mounts SigmondApp via Textual's Pilot test harness and verifies:

  1. Vocabulary alignment — the three CLI-V2-SPEC-sensitive code paths
     (`smd admin log set-level`, `smd component update`, `smd watch ka9q`)
     emit the canonical argv when their screen actions fire.
  2. Per-instance dropdowns — the six MULTI-INSTANCE-ARCHITECTURE §8
     screens (activity / verifier / logs / lifecycle / client_config
     plus the new Instance screen) mount with a Select widget at the
     expected ID and their option lists contain a real reporter_id
     when the host has migrated instances.

Run after a `smd admin instance migrate` (or any TUI vocabulary change) to
confirm the screens still match what the CLI accepts.

Usage:
    /opt/git/sigmond/sigmond/venv/bin/python scripts/tui_walk.py

Exit code 0 on full pass, 1 on any failure.

Requires:
    - `textual` (production venv `/opt/git/sigmond/sigmond/venv` has it; the dev
      venv `.venv` needs `[tui]` extras via scripts/dev-setup.sh).
    - On a freshly installed host with no migrated instances, content
      checks may report empty option lists — that's a host-state
      issue, not a code regression.  The mount + vocabulary checks
      pass on any host.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

# Make `import sigmond` work when run from anywhere — resolve against
# this script's location (scripts/) → repo root → lib/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "lib"))

from sigmond.tui.app import SigmondApp  # noqa: E402

PASS = "\033[32m✔\033[0m"
FAIL = "\033[31m✘\033[0m"
SKIP = "\033[33m—\033[0m"

results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))


async def walk() -> None:
    # ---- Vocabulary check 1: LogsScreen `log set-level` ----
    app = SigmondApp()
    async with app.run_test(size=(140, 60)) as pilot:
        app.action_show_logs()
        for _ in range(4):
            await pilot.pause()

        from sigmond.tui.screens.logs import LogsScreen
        logs = next((c for c in app.query_one("#center").children
                     if isinstance(c, LogsScreen)), None)
        if logs is None:
            record("logs: screen mounts", FAIL, "no LogsScreen in #center")
        else:
            record("logs: screen mounts", PASS)
            from textual.widgets import Select
            picker = logs.query_one("#lg-picker", Select)
            installed = [c for c in logs._install_state if logs._install_state.get(c)]
            if not installed:
                record("logs: set-level argv", SKIP, "no installed components on host")
            else:
                picker.value = installed[0]
                level_pick = logs.query_one("#lg-level", Select)
                level_pick.value = "DEBUG"
                await pilot.pause()

                captured: list[list[str]] = []

                def fake_confirm(app_, *, title, body, cmd, sudo, on_complete):
                    captured.append(cmd)

                with patch("sigmond.tui.screens.logs.confirm_and_run",
                           side_effect=fake_confirm):
                    logs._set_level()
                    await pilot.pause()

                if not captured:
                    record("logs: set-level argv", FAIL, "no confirm_and_run call")
                else:
                    argv = captured[0]
                    ok = (len(argv) >= 6 and argv[1] == "admin"
                          and argv[2] == "log" and argv[3] == "set-level"
                          and argv[4] == installed[0])
                    record("logs: set-level argv", PASS if ok else FAIL,
                           " ".join(str(x) for x in argv))

    # ---- Vocabulary check 2: ComponentsScreen `component update` ----
    app = SigmondApp()
    async with app.run_test(size=(140, 60)) as pilot:
        app.action_show_components()
        for _ in range(4):
            await pilot.pause()

        from sigmond.tui.screens.components import (
            ComponentsScreen, _ComponentRow,
        )
        comps = next((c for c in app.query_one("#center").children
                     if isinstance(c, ComponentsScreen)), None)
        if comps is None:
            record("components: screen mounts", FAIL)
        else:
            record("components: screen mounts", PASS)
            comps._rows = [_ComponentRow(
                name="psk-recorder", kind="client",
                description="", repo="", installed=True,
                repo_dir=None, current_ref="main@abc1234",
                version_policy="latest", enabled=True,
                behind="2", ahead="0", dirty=False)]
            comps._selected = {"psk-recorder"}
            captured = []

            class _StubModal:
                def __init__(self, *a, **kw):
                    captured.append(kw.get("cmd"))

            with patch("sigmond.tui.screens.components.ConfirmModal", autospec=True), \
                 patch("sigmond.tui.screens.components.UpdateOutputModal",
                       side_effect=_StubModal), \
                 patch.object(app, "push_screen",
                              lambda screen, callback=None:
                              (callback(True) if callback else None)):
                comps._bulk_update()
                await pilot.pause()

            if not captured:
                record("components: update argv", FAIL, "no modal captured")
            else:
                argv = captured[0]
                ok = (argv and "component" in argv and "update" in argv
                      and "--apply" not in argv)
                record("components: update argv (no --apply)",
                       PASS if ok else FAIL, " ".join(argv))

    # ---- Vocabulary check 3: Ka9qWatchScreen `watch ka9q` ----
    from sigmond.tui.screens import ka9q_watch as kw_mod

    captured_cmd: list[list[str]] = []

    def fake_subprocess_run(cmd, **kw):
        captured_cmd.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout='{"verdict": "green", "details": []}', stderr="",
        )

    with patch("sigmond.tui.screens.ka9q_watch.subprocess.run",
               side_effect=fake_subprocess_run):
        kw_mod._run(no_fetch=True)

    smd_calls = [c for c in captured_cmd
                 if c and len(c) >= 3 and isinstance(c[0], str)
                 and c[0].endswith("smd")]
    if not smd_calls:
        record("ka9q_watch: _run argv", FAIL,
               f"no smd call (captured {len(captured_cmd)} non-smd calls)")
    else:
        argv = smd_calls[0]
        ok = (len(argv) >= 4 and argv[1] == "watch" and argv[2] == "ka9q"
              and "--json" in argv and "ka9q-watch" not in argv)
        record("ka9q_watch: _run argv", PASS if ok else FAIL, " ".join(argv))

    # ---- Per-instance dropdown coverage + content check ----
    from textual.widgets import Select

    def _opt_values(select_widget):
        opts = getattr(select_widget, "_options", None) or []
        return [v for (_label, v) in opts]

    # (name, action, instance_selector, stage1_selector, stage1_value)
    # stage1_value=None means the instance dropdown is pre-populated at mount.
    dropdown_specs = [
        ("activity",      "show_activity",      "#ac-instance",
         "#ac-target",  "psk"),
        ("verifier",      "show_verifier",      "#vf-instance",
         "#vf-target",  "psk"),
        ("logs",          "show_logs",          "#lg-instance",
         "#lg-picker",  "psk-recorder"),
        ("lifecycle",     "show_lifecycle",     "#lc-i-select",
         None,          None),
        ("client_config", "show_client_config", "#cc-instance",
         "#cc-table",   "psk-recorder"),
    ]

    app = SigmondApp()
    async with app.run_test(size=(140, 60)) as pilot:
        for name, action, instance_sel, stage1_sel, stage1_val in dropdown_specs:
            try:
                getattr(app, f"action_{action}")()
            except AttributeError:
                record(f"{name}: nav action exists", FAIL, f"no action_{action}")
                continue
            for _ in range(3):
                await pilot.pause()

            try:
                widget = app.screen.query_one(instance_sel)
            except Exception as e:
                record(f"{name}: per-instance dropdown mounts ({instance_sel})",
                       FAIL, repr(e)[:80])
                continue
            record(f"{name}: per-instance dropdown mounts ({instance_sel})",
                   PASS, type(widget).__name__)

            if stage1_sel and stage1_val is not None:
                try:
                    if name == "client_config":
                        # DataTable-driven flow; drive the helper directly
                        # rather than synthesise a row-click event.
                        from sigmond.tui.screens.client_config import (
                            _instance_options_for_client,
                        )
                        widget.set_options(_instance_options_for_client(stage1_val))
                        await pilot.pause()
                    else:
                        stage1 = app.screen.query_one(stage1_sel, Select)
                        stage1.value = stage1_val
                        for _ in range(4):
                            await pilot.pause()
                except Exception as e:
                    record(f"{name}: stage-1 trigger ({stage1_sel}={stage1_val!r})",
                           FAIL, repr(e)[:80])
                    continue

            try:
                widget = app.screen.query_one(instance_sel, Select)
                values = _opt_values(widget)
            except Exception as e:
                record(f"{name}: content read", FAIL, repr(e)[:80])
                continue

            # Reporter-ID-shape heuristic: any option that has at least
            # one uppercase letter and isn't a sentinel or legacy entry.
            # Lifecycle stores full unit names (`<client>@<id>.service`);
            # other screens store just the reporter_id.  Both contain
            # uppercase characters in a real reporter ID and neither
            # sentinel (`__all__` / `__none__`) nor legacy label
            # (`(legacy)`) does.  On a fresh / unmigrated host this
            # returns SKIP rather than FAIL — it's host-state, not code.
            reporter_like = [v for v in values
                             if isinstance(v, str)
                             and v not in ("__all__", "__none__")
                             and "(legacy)" not in v
                             and any(ch.isupper() for ch in v)]
            if reporter_like:
                record(f"{name}: dropdown lists a configured instance",
                       PASS, f"e.g. {reporter_like[0]!r}")
            else:
                record(f"{name}: dropdown lists a configured instance",
                       SKIP,
                       f"no migrated-instance entries (options: {values})")

        # Instance screen — new top-level screen under Installation.
        try:
            app.action_show_instance()
        except AttributeError:
            record("instance: nav action exists", FAIL, "no action_show_instance")
        else:
            for _ in range(4):
                await pilot.pause()
            from sigmond.tui.screens.instance import InstanceScreen
            inst_screen = next(
                (c for c in app.query_one("#center").children
                 if isinstance(c, InstanceScreen)),
                None,
            )
            if inst_screen is None:
                record("instance: new screen mounts", FAIL,
                       "InstanceScreen not in #center")
            else:
                record("instance: new screen mounts", PASS)
                from textual.widgets import DataTable
                try:
                    table = inst_screen.query_one(DataTable)
                    rows = [str(table.get_row_at(i))
                            for i in range(table.row_count)]
                    if table.row_count == 0:
                        record("instance: DataTable populated", SKIP,
                               "no instances on host (run `smd admin instance "
                               "migrate --yes` to populate)")
                    else:
                        record("instance: DataTable populated", PASS,
                               f"{table.row_count} row(s); sample: {rows[0]}")
                except Exception as e:
                    record("instance: table content", FAIL, repr(e)[:80])


def main() -> int:
    asyncio.run(walk())
    print()
    print(f"{'SCREEN / CHECK':<60} {'STATUS':<8} DETAIL")
    print("-" * 100)
    fail_count = 0
    skip_count = 0
    for name, status, detail in results:
        print(f"{name:<60} {status:<8} {detail}")
        if status == FAIL:
            fail_count += 1
        elif status == SKIP:
            skip_count += 1
    print("-" * 100)
    print(f"{len(results)} checks, {fail_count} FAIL, {skip_count} SKIP")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
