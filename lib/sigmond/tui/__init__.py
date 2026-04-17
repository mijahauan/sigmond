"""Sigmond TUI configurator.

Textual is a lazy runtime dependency -- this package is never imported
by the core smd CLI.  Only ``smd config edit`` triggers the import.
"""


def launch():
    """Entry point called by bin/smd for ``smd config edit``."""
    from .app import SigmondApp
    app = SigmondApp()
    app.run()
