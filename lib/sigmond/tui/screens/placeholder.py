"""Placeholder screen for functions in the TUI IA that are not yet built.

Shown for screens whose CLI equivalent already works — the placeholder
points operators at the CLI so they aren't blocked, and makes the full
IA visible in the nav tree from day one.
"""

from __future__ import annotations

from typing import Optional

from textual.containers import Vertical
from textual.widgets import Static


class PlaceholderScreen(Vertical):
    """A read-only 'not built yet' pane with a CLI-equivalent hint."""

    DEFAULT_CSS = """
    PlaceholderScreen {
        padding: 2;
    }
    PlaceholderScreen .ph-title {
        text-style: bold;
        margin-bottom: 1;
    }
    PlaceholderScreen .ph-body {
        margin-bottom: 1;
        color: $text-muted;
    }
    PlaceholderScreen .ph-cli {
        margin-top: 1;
    }
    """

    def __init__(
        self,
        title: str,
        description: str = "",
        cli_hint: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._description = description
        self._cli_hint = cli_hint

    def compose(self):
        yield Static(self._title, classes="ph-title")
        if self._description:
            yield Static(self._description, classes="ph-body")
        yield Static("[yellow]Not yet implemented in the TUI.[/]", classes="ph-body")
        if self._cli_hint:
            yield Static(
                f"For now, use the CLI:\n\n  [cyan bold]{self._cli_hint}[/]",
                classes="ph-cli",
            )
