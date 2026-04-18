"""Sigmond TUI configurator.

Textual is a lazy runtime dependency -- this package is never imported
by the core smd CLI.  Only ``smd tui`` triggers the import.
"""


def launch():
    """Entry point called by bin/smd for ``smd tui`` (and the deprecated ``smd config edit`` alias)."""
    from .app import SigmondApp
    app = SigmondApp()
    app.run()
