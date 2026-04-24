"""Draggable vertical divider for the three-panel TUI layout.

Renders as a thin bar between panels.  Mouse down + drag adjusts the
width of the target panel (``target_id``).  ``sign`` controls direction:
  +1  drag right → target grows  (use for the left panel)
  -1  drag right → target shrinks (use for the right panel)
"""

from __future__ import annotations

from textual import events
from textual.widget import Widget


class PanelSplitter(Widget):
    """1-cell-wide draggable divider that resizes an adjacent panel."""

    DEFAULT_CSS = """
    PanelSplitter {
        width: 1;
        height: 100%;
        background: $primary-background;
        color: $text-muted;
    }
    PanelSplitter:hover {
        background: $accent;
        color: $text;
    }
    """

    def __init__(self, target_id: str, sign: int = 1, min_width: int = 16, **kwargs) -> None:
        super().__init__(**kwargs)
        self._target_id = target_id
        self._sign = sign
        self._min_width = min_width
        self._dragging = False
        self._drag_start_x = 0
        self._start_width = 0

    def render(self) -> str:
        return "│"

    def on_mouse_down(self, event: events.MouseDown) -> None:
        target = self.app.query_one(f"#{self._target_id}")
        self._start_width = target.size.width
        self._drag_start_x = event.screen_x
        self._dragging = True
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        dx = event.screen_x - self._drag_start_x
        new_width = max(self._min_width, self._start_width + self._sign * dx)
        target = self.app.query_one(f"#{self._target_id}")
        target.styles.width = int(new_width)
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.capture_mouse(False)
        event.stop()
