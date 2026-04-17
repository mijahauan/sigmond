"""Validate screen — runs harmonization rules and displays results."""

from textual.containers import Vertical
from textual.widgets import Button, DataTable, Static


class ValidateScreen(Vertical):
    """Run cross-client harmonization rules and display results."""

    DEFAULT_CSS = """
    ValidateScreen {
        padding: 1;
    }
    ValidateScreen #val-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ValidateScreen #val-rerun {
        margin-top: 1;
        width: auto;
    }
    """

    def compose(self):
        yield Static("Validate — harmonization rules", id="val-title")
        table = DataTable(id="val-table")
        table.add_columns("Rule", "Result", "Details")
        yield table
        yield Button("Re-run", id="val-rerun", variant="default")

    def on_mount(self) -> None:
        self._run_validation()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "val-rerun":
            self._run_validation()

    def _run_validation(self) -> None:
        table = self.query_one("#val-table", DataTable)
        table.clear()

        try:
            from ...sysview import build_system_view
            from ...harmonize import run_all
            view = build_system_view()
            results = run_all(view)
        except Exception as exc:
            table.add_row("error", "[red]fail[/]", str(exc))
            return

        for r in results:
            if r.severity == "pass":
                badge = "[green]\u2714 pass[/]"
            elif r.severity == "warn":
                badge = "[yellow]\u26a0 warn[/]"
            else:
                badge = "[red]\u2718 fail[/]"
            table.add_row(r.rule, badge, r.message)
