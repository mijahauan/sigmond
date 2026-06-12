"""Lifecycle screen — TUI counterpart to `smd {start|stop|restart}`.

Lists every lifecycle-managed instance (one row per templated unit
on per-reporter clients, one row per service on singletons) and lets
the operator multi-select with checkboxes, then start / stop /
restart all checked rows in one batched action.

Selection model — checkbox column, first column:
  * Click a checkbox cell once to highlight it, again to toggle
    (Textual's native DataTable 2-click model).  A custom single-
    click intercept was tried via ``on_click`` and didn't engage
    cleanly across the cell-meta cases; the native gesture wins
    until / unless someone reverse-engineers the DataTable click
    pipeline more carefully.
  * Click the checkbox column header (□ / ▣ / ■) to toggle all rows.
  * Keyboard: Tab to focus the table, arrow keys to move the cursor,
    Enter to toggle the cursored row — single keystroke (no 2-press
    requirement for the keyboard path).

This is the "verb screens" pattern: pick N targets, apply one verb.
Drill-down screens elsewhere in the TUI keep their highlight-row
single-select gesture — see docs/TUI conventions.

Reload was removed deliberately: none of the sigmond clients
implement `ExecReload=` in their unit file, so `systemctl reload`
either no-ops or silently falls back to restart depending on
systemd's mood.  Restart is the only honest verb.

Dispatch rules:
  * instance rows → ``sudo systemctl <verb> <unit> [<unit> ...]``
    direct (single-unit and small-batch actions don't need sigmond's
    cross-component lifecycle lock).
  * component rows → ``smd <verb> --components <name>`` so
    sigmond's lifecycle CLI (which holds the lock per CONTRACT v0.5
    §5.5) orders the sub-units of multi-unit components like hf-timestd.

If both kinds are in a single batch they chain: instance batch first,
then component batch, with the operator's up-front confirm covering
both.
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


# Glyphs for the checkbox column.  The unchecked state is a clean
# outline (□); the checked state is a fully-filled black square (■)
# rather than the rounded ☐/☑ pair — the inside-check on ☑ renders as
# a thin diagonal stroke at small terminal-font sizes and is easy to
# miss.  Filled-vs-outline gives much higher contrast at a glance.
# The partial-selected glyph (▣) lives in the master header logic
# below and stays as a small-square-inside-outline since "partial"
# isn't a per-row state.
_CHK_OFF = "□"
_CHK_ON  = "■"


def _smd_binary() -> str:
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and os.path.basename(argv0) == 'smd':
        return argv0
    found = shutil.which('smd')
    return found or '/usr/local/bin/smd'


@dataclass
class _Row:
    """One row's worth of operator-meaningful state.

    Two flavours, distinguished by `kind`:

    - ``instance``: a single templated unit like
      ``psk-recorder@AC0G-B1.service``.  Action targets the unit
      directly via ``systemctl <verb> <unit>``.

    - ``component``: an aggregate of one or more units that move
      together (the hf-timestd singleton's many sub-services, a
      single-unit singleton like mag-recorder, etc.).  Action
      targets the component via ``smd <verb> --components <component>``
      so sigmond's lifecycle CLI picks the right ordering.
    """
    key: str                 # stable row key (unit name OR component name)
    display: str             # operator-facing label
    kind: str                # "instance" | "component"
    active: str              # aggregated systemctl ActiveState
    # Action plumbing:
    unit: Optional[str]      # for "instance" rows: the systemd unit
    component: str           # always present; for "component" rows this drives `smd <verb>`
    n_units: int             # how many systemd units this row aggregates
    n_active: int            # how many are currently active
    orphaned: bool = False


@dataclass
class _LifecycleData:
    rows: list[_Row] = field(default_factory=list)
    error: Optional[str] = None


def _systemctl_show_batch(units: list[str], properties: list[str]) -> dict[str, dict[str, str]]:
    """Batched `systemctl show -p P1 -p P2 ... --value=no <unit> <unit> ...`.

    Returns {unit: {property: value}}.  systemctl emits a blank line
    between unit records; we split on those.  Stable across systemd
    versions sigmond targets.
    """
    if not units:
        return {}
    try:
        result = subprocess.run(
            ["systemctl", "show"] + [f"-p{p}" for p in properties] + list(units),
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0 or not result.stdout:
        return {}

    # Each unit's record is N lines (one per property), then a blank
    # line.  Order matches the order units were passed in.
    records = result.stdout.split("\n\n")
    out: dict[str, dict[str, str]] = {}
    for unit, block in zip(units, records):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            fields[k] = v
        out[unit] = fields
    return out


def _aggregate_active(states: list[str]) -> str:
    """Aggregate per-unit ActiveState into one component-level token.

    "failed" wins over everything (any failure is a problem).
    Then "active" if all units are active, otherwise "partial"
    (some active, some not).  "inactive" if none active.
    """
    if not states:
        return "unknown"
    if "failed" in states:
        return "failed"
    n_active = sum(1 for s in states if s == "active")
    if n_active == len(states):
        return "active"
    if n_active == 0:
        return "inactive"
    return "partial"


def _gather() -> _LifecycleData:
    """Enumerate operator-meaningful lifecycle targets + their state.

    Source: `resolve_units()` for topology-enabled components.  Then
    fold into rows:

    - Components whose unit refs are all templated (one per instance —
      psk/wspr/hfdl/codar/hf-gps-tec recorder pattern) → one row per
      instance.  Action verb goes to `systemctl <verb> <unit>` direct.

    - Components with concrete or mixed unit refs (hf-timestd's many
      sub-services, single-unit singletons like mag-recorder /
      gpsdo-monitor / igmp-querier) → one row per component, aggregated.
      Action verb goes to `smd <verb> --components <component>` so
      sigmond's lifecycle CLI picks the right ordering.

    Library-kind catalog entries are excluded — no systemd presence.
    """
    data = _LifecycleData()
    try:
        from ...topology import load_topology
        from ...lifecycle import resolve_units
        from ...catalog import load_catalog

        topology = load_topology()
        enabled = topology.enabled_components()

        try:
            catalog = load_catalog()
            library_names = {n for n, e in catalog.items() if e.kind == 'library'}
        except Exception:
            library_names = set()

        candidates = [c for c in enabled if c not in library_names]

        try:
            lc_units = resolve_units(candidates, candidates)
        except ValueError as exc:
            data.error = f"unit resolution: {exc}"
            lc_units = []

        units = [u.unit for u in lc_units]
        props = _systemctl_show_batch(units, ["ActiveState"])

        # Group units by component.
        by_comp: dict[str, list] = {}
        for u in lc_units:
            by_comp.setdefault(u.component, []).append(u)

        rows: list[_Row] = []
        for comp, urefs in by_comp.items():
            # Templated components: per-reporter clients where every
            # UnitRef carries an `instance`.  One row per instance.
            if urefs and all(u.template and u.instance for u in urefs):
                for u in urefs:
                    state = props.get(u.unit, {}).get("ActiveState", "unknown")
                    rows.append(_Row(
                        key=u.unit,
                        display=u.unit.removesuffix(".service"),
                        kind="instance",
                        active=state,
                        unit=u.unit,
                        component=u.component,
                        n_units=1,
                        n_active=1 if state == "active" else 0,
                        orphaned=u.orphaned,
                    ))
                continue
            # Multi-unit or singleton component: collapse to one row.
            states = [props.get(u.unit, {}).get("ActiveState", "unknown")
                      for u in urefs]
            n_active = sum(1 for s in states if s == "active")
            rows.append(_Row(
                key=comp,
                display=comp,
                kind="component",
                active=_aggregate_active(states),
                unit=None,
                component=comp,
                n_units=len(urefs),
                n_active=n_active,
                orphaned=any(u.orphaned for u in urefs),
            ))

        rows.sort(key=lambda r: (r.component, r.display))
        data.rows = rows

    except Exception as exc:
        data.error = str(exc)
    return data


def _state_markup(row: _Row) -> str:
    """Single-cell state rendering, colour-aware.

    For component rows we additionally annotate the active/total
    fraction so the operator sees at a glance whether all sub-services
    are up (e.g. ``active 13/13``) or partial (``partial 11/13``).
    """
    s = row.active
    suffix = ""
    if row.kind == "component" and row.n_units > 1:
        suffix = f" {row.n_active}/{row.n_units}"
    if s == "active":
        return f"[green]active{suffix}[/]"
    if s == "failed":
        return f"[red]failed{suffix}[/]"
    if s == "partial":
        return f"[yellow]partial{suffix}[/]"
    if s in ("activating", "deactivating"):
        return f"[yellow]{s}{suffix}[/]"
    return f"[dim]{s}{suffix}[/]"


class LifecycleScreen(Vertical):
    """Multi-select instance lifecycle (start / stop / restart)."""

    DEFAULT_CSS = """
    LifecycleScreen {
        padding: 1;
    }
    LifecycleScreen .lc-title {
        text-style: bold;
        margin-bottom: 1;
    }
    LifecycleScreen #lc-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    LifecycleScreen #lc-refresh-row {
        height: 3;
        margin-bottom: 1;
    }
    LifecycleScreen #lc-refresh-row Button {
        margin-right: 1;
        min-width: 12;
    }
    LifecycleScreen #lc-table {
        margin-bottom: 1;
    }
    LifecycleScreen #lc-action-row {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
    }
    LifecycleScreen #lc-action-row Button {
        margin-right: 1;
        min-width: 14;
    }
    LifecycleScreen #lc-last {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Track checked rows by unit name (DataTable row_key).
        self._checked: set[str] = set()
        self._rows: list[_InstanceRow] = []
        # Cache the column key for the checkbox column so we can
        # surgically update_cell on toggle without re-rendering rows.
        self._chk_col_key = None

    def compose(self):
        yield Static("Lifecycle — managed instances", classes="lc-title")
        yield Static("[dim]loading…[/]", id="lc-status")

        # Refresh at top (read-only, small).
        with Horizontal(id="lc-refresh-row"):
            yield Button("↻ Refresh", id="lc-refresh", variant="default")

        # The table.  cursor_type="cell" lets us listen for clicks on
        # the checkbox cell specifically; clicks on other columns
        # still highlight the row but don't toggle (no surprise actions).
        table = DataTable(id="lc-table", cursor_type="cell", zebra_stripes=True)
        # First column header IS the master toggle — click it to
        # toggle all rows.  Header text shows current "select all" state.
        # Width 5 (was 3): one cell for the glyph plus four for padding,
        # so the box sits in the middle of its cell instead of jammed
        # against the cell border.  Easier to click without a steady hand.
        self._chk_col_key = table.add_column(_CHK_OFF, width=5, key="chk")
        table.add_column("Target", key="target")
        table.add_column("State",  key="state")
        yield table

        # Action buttons at the bottom.  Colour variants match the
        # semantic of the action (green=go, red=stop, yellow=cycle).
        # The count placeholder updates as the operator (de)selects rows.
        with Horizontal(id="lc-action-row"):
            yield Button("Start (0)",   id="lc-start",   variant="success")
            yield Button("Stop (0)",    id="lc-stop",    variant="error")
            yield Button("Restart (0)", id="lc-restart", variant="warning")

        yield Static("", id="lc-last")

    def on_mount(self) -> None:
        self._refresh()

    # ------------------------------------------------------------------
    # data loading
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self.query_one("#lc-status", Static).update("[dim]loading…[/]")
        # Don't blow away the checked set on refresh — the operator's
        # selection is intentional and should survive a state poll.
        # We DO drop checks for rows that no longer exist on disk.
        self.run_worker(_gather, thread=True, name="lc-gather")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        if event.worker.name == "lc-gather":
            data = event.worker.result
            if isinstance(data, _LifecycleData):
                self._render_data(data)
            return
        if event.worker.name == "lc-chain":
            # Chained second step (no fresh confirm dialog — already
            # approved up-front).  Route into the same completion handler.
            result = event.worker.result
            if isinstance(result, subprocess.CompletedProcess):
                self._after_verb_chained(result)
            return

    def _render_data(self, data: _LifecycleData) -> None:
        if data.error:
            self.query_one("#lc-status", Static).update(
                f"[yellow]partial: {data.error}[/]")

        self._rows = data.rows
        # Drop stale check entries (rows that disappeared since last
        # refresh — e.g. after a manual `smd component remove` elsewhere).
        present = {r.key for r in data.rows}
        self._checked &= present

        self._update_status_count()

        table = self.query_one("#lc-table", DataTable)
        table.clear()

        if not data.rows:
            table.add_row(_CHK_OFF, "(none)", "[dim]—[/]", key="__empty__")
        else:
            for r in data.rows:
                chk = _CHK_ON if r.key in self._checked else _CHK_OFF
                label = r.display
                if r.orphaned:
                    label = f"{label} [dim](orphaned)[/]"
                table.add_row(
                    chk, label, _state_markup(r),
                    key=r.key,
                )

        self._update_action_labels()
        self._update_master_toggle_header()

    # ------------------------------------------------------------------
    # selection model
    # ------------------------------------------------------------------

    def _toggle_row(self, row_key: str) -> None:
        """Flip one row's checked state.  Shared by mouse-click and
        keyboard-Enter paths."""
        if not isinstance(row_key, str) or row_key in ("", "__empty__"):
            return
        if row_key in self._checked:
            self._checked.discard(row_key)
        else:
            self._checked.add(row_key)
        # Surgical update — only the one cell, no re-render of the rest.
        chk = _CHK_ON if row_key in self._checked else _CHK_OFF
        table = self.query_one("#lc-table", DataTable)
        try:
            table.update_cell(row_key, "chk", chk)
        except Exception:
            # Row was removed between click and dispatch (refresh raced) —
            # ignore, the next render will catch up.
            pass
        self._update_action_labels()
        self._update_master_toggle_header()
        self._update_status_count()

    def on_data_table_cell_selected(
        self, event: DataTable.CellSelected
    ) -> None:
        """Toggle the row's checkbox when the operator activates the
        checkbox cell (mouse: first click highlights, second click
        activates; keyboard: arrow to cursor, Enter to activate).

        Two-click is Textual's native DataTable model; single-click
        toggle was tried via a custom ``on_click`` interceptor and
        didn't work cleanly across the cell-meta cases.  Sticking with
        the native gesture for now.

        Other columns: no-op (click highlights the cell, but doesn't
        mutate selection state — keeps the gesture deterministic).
        """
        col_key = event.coordinate.column
        if col_key != 0:
            return
        row_key = event.cell_key.row_key.value if event.cell_key.row_key else None
        if not isinstance(row_key, str):
            return
        self._toggle_row(row_key)

    def on_data_table_header_selected(
        self, event: DataTable.HeaderSelected
    ) -> None:
        """Click the checkbox column header to select-all / clear-all."""
        if event.column_key.value != "chk":
            return
        if not self._rows:
            return
        # If anything is checked, clear; otherwise check all.
        if self._checked:
            self._checked.clear()
        else:
            self._checked = {r.unit for r in self._rows}
        self._repaint_check_column()
        self._update_action_labels()
        self._update_master_toggle_header()
        self._update_status_count()

    def _repaint_check_column(self) -> None:
        table = self.query_one("#lc-table", DataTable)
        for r in self._rows:
            chk = _CHK_ON if r.key in self._checked else _CHK_OFF
            table.update_cell(r.key, "chk", chk)

    def _update_master_toggle_header(self) -> None:
        """Header glyph reflects current select-all state."""
        table = self.query_one("#lc-table", DataTable)
        if not self._rows:
            label = _CHK_OFF
        elif self._checked == {r.key for r in self._rows}:
            label = _CHK_ON
        elif self._checked:
            label = "▣"   # partial — some-but-not-all selected
        else:
            label = _CHK_OFF
        try:
            table.columns[table.get_column_index("chk")].label = label
            table.refresh()
        except Exception:
            # Textual API drift safety — header sync is cosmetic, not load-bearing.
            pass

    def _update_action_labels(self) -> None:
        n = len(self._checked)
        self.query_one("#lc-start",   Button).label = f"Start ({n})"
        self.query_one("#lc-stop",    Button).label = f"Stop ({n})"
        self.query_one("#lc-restart", Button).label = f"Restart ({n})"

    def _update_status_count(self) -> None:
        """Update just the trailing 'N checked' tail of the status line."""
        # Cheap to just re-derive; saves wiring a dedicated label.
        total = len(self._rows)
        active = sum(1 for r in self._rows if r.active == "active")
        self.query_one("#lc-status", Static).update(
            f"{total} managed instance(s) · "
            f"[green]{active} active[/], {total - active} not active · "
            f"{len(self._checked)} checked")

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "lc-refresh":
            self._refresh()
            return
        if bid not in ("lc-start", "lc-stop", "lc-restart"):
            return
        verb = bid.removeprefix("lc-")
        if not self._checked:
            self.query_one("#lc-last", Static).update(
                "[yellow]no rows checked — pick at least one before "
                f"clicking {verb.capitalize()}[/]")
            return

        # Build the dispatch.  Mixed batches are common (e.g. user picks
        # one instance row + one component row); we'd need two separate
        # invocations to honour sigmond's lifecycle lock for the
        # component half.  Split + run them in sequence — the
        # confirm_and_run dialog only fires once, and the second call
        # chains from the first's on_complete via a closure.
        by_key = {r.key: r for r in self._rows}
        # Defensive: filter to keys that still resolve to rows (drops
        # stale entries from before the last refresh) and stringify the
        # sort key so a mixed-type set never reaches `<`.
        relevant = sorted(
            (k for k in self._checked if k in by_key),
            key=str,
        )
        instance_units = [by_key[k].unit for k in relevant
                          if by_key[k].kind == "instance"]
        components = [by_key[k].component for k in relevant
                      if by_key[k].kind == "component"]

        # Build a human preview of what's going to run, then run.
        preview_lines = []
        if instance_units:
            preview_lines.append(
                "[bold]sudo systemctl " + verb + " "
                + " ".join(instance_units) + "[/]"
            )
        if components:
            preview_lines.append(
                "[bold]smd " + verb + " --components "
                + ",".join(components) + "[/]"
            )
        n = len(self._checked)
        body = (
            "\n".join(preview_lines)
            + f"\n\n{n} target(s) selected.  Continue?"
        )

        # Chain: run instance batch first, then component batch.  If
        # either is empty, that step is skipped.
        smd = _smd_binary()
        steps: list[list[str]] = []
        if instance_units:
            steps.append(["systemctl", verb] + instance_units)
        if components:
            steps.append([smd, verb, "--components", ",".join(components)])
        self._pending_steps = steps

        confirm_and_run(
            self.app,
            title=f"Confirm: {verb} ×{n}",
            body=body,
            cmd=steps[0], sudo=True,
            on_complete=self._after_verb_chained,
        )

    def _after_verb_chained(self, result: subprocess.CompletedProcess) -> None:
        """Step-completion callback.

        If there are more queued steps (e.g. operator selected a mix of
        instance rows and component rows), kick off the next one without
        re-prompting — the original confirm dialog already covered the
        whole batch.  Otherwise refresh and report.
        """
        # Mirror the per-step result to the bottom status line so the
        # operator sees the chain advance, not just the final outcome.
        last = self.query_one("#lc-last", Static)
        argv = ' '.join(result.args) if result.args else ''
        glyph = "[green]✔[/]" if result.returncode == 0 else "[red]✘[/]"
        last.update(f"{glyph} exit {result.returncode}  {argv}")

        # Advance.
        steps = getattr(self, "_pending_steps", []) or []
        if steps:
            steps.pop(0)
        if steps and result.returncode == 0:
            self._pending_steps = steps
            # Direct subprocess call rather than a fresh confirm dialog —
            # the operator already approved the batch up-front.
            self.run_worker(
                lambda: subprocess.run(["sudo", "-n", *steps[0]],
                                       capture_output=True),
                thread=True, name="lc-chain",
            )
            return
        # End of chain (or earlier step failed).  Refresh the state poll.
        self._pending_steps = []
        self._refresh()
