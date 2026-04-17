"""Right-panel contextual help and live system state."""

from textual.containers import VerticalScroll
from textual.widgets import Static


class ContextPanel(VerticalScroll):
    """Displays contextual help and live status for the active screen."""

    DEFAULT_CSS = """
    ContextPanel {
        padding: 1;
    }
    ContextPanel .ctx-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ContextPanel .ctx-body {
        color: $text-muted;
    }
    """

    def compose(self):
        yield Static("Context", classes="ctx-title", id="ctx-title")
        yield Static("Select an item to see details.", classes="ctx-body", id="ctx-body")

    def show_help(self, title: str, body: str) -> None:
        self.query_one("#ctx-title", Static).update(title)
        self.query_one("#ctx-body", Static).update(body)
