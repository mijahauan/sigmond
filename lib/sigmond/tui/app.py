"""Sigmond TUI — three-panel configurator.

Left:   component tree with health indicators
Center: active screen (topology editor, validate, etc.)
Right:  contextual help and live system state
"""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header

from .widgets.component_tree import ComponentTree
from .widgets.context_panel import ContextPanel


class SigmondApp(App):
    """Dr. SigMonD TUI configurator."""

    TITLE = "Dr. SigMonD"
    SUB_TITLE = "Signal Monitor Daemon — Configurator"

    CSS = """
    #main {
        height: 1fr;
    }
    #left {
        width: 24;
        border-right: solid $primary-background;
        padding: 0 1;
    }
    #center {
        width: 1fr;
        padding: 0 1;
    }
    #right {
        width: 32;
        border-left: solid $primary-background;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("t", "show_topology", "Topology"),
        Binding("v", "show_validate", "Validate"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield ComponentTree(id="left")
            yield VerticalScroll(id="center")
            yield ContextPanel(id="right")
        yield Footer()

    def on_mount(self) -> None:
        self._load_system_view()
        self.action_show_topology()

    def _load_system_view(self) -> None:
        """Load topology, catalog, and coordination for all screens."""
        from ..topology import load_topology
        from ..catalog import load_catalog

        try:
            self.topology = load_topology()
        except Exception:
            from ..topology import Topology
            self.topology = Topology(
                client_dir=__import__('pathlib').Path('/opt/git'),
                smd_bin=__import__('pathlib').Path('/usr/local/sbin/smd'),
            )

        try:
            self.catalog = load_catalog()
        except FileNotFoundError:
            self.catalog = {}

        # Populate the component tree.
        tree = self.query_one(ComponentTree)
        tree.populate(self.topology, self.catalog)

    def action_show_topology(self) -> None:
        from .screens.topology import TopologyScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(TopologyScreen(self.topology, self.catalog))

        ctx = self.query_one(ContextPanel)
        ctx.show_help(
            "Topology",
            "Enable or disable components for this host.\n\n"
            "Enabled components will be managed by smd start/stop.\n"
            "Save writes changes to /etc/sigmond/topology.toml.",
        )

    def action_show_validate(self) -> None:
        from .screens.validate import ValidateScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(ValidateScreen())

        ctx = self.query_one(ContextPanel)
        ctx.show_help(
            "Validate",
            "Runs cross-client harmonization rules.\n\n"
            "Rules check: radiod resolution, frequency coverage, "
            "CPU isolation, timing chain, disk budget, and channel count.",
        )
