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


def _discover_radiod_from_config() -> tuple[str, str]:
    """Discover radiod instance name and status address from /etc/radio/.

    Parses radiod@*.conf files for the 'status = ...' line.
    Returns (instance_name, status_dns) or ('', '').
    """
    from pathlib import Path
    import re

    conf_dir = Path('/etc/radio')
    if not conf_dir.exists():
        return ('', '')

    for conf in sorted(conf_dir.glob('radiod@*.conf')):
        # Extract instance name from filename: radiod@foo.conf -> foo
        instance = conf.stem.split('@', 1)[1] if '@' in conf.stem else ''
        try:
            for line in conf.read_text().splitlines():
                line = line.strip()
                if line.startswith('status') and '=' in line:
                    # Parse: status = bee1-status.local  # comment
                    val = line.split('=', 1)[1].split('#')[0].strip()
                    if val:
                        return (instance, val)
        except OSError:
            continue
    return ('', '')


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
        Binding("r", "show_radiod", "Radiod"),
        Binding("c", "show_cpu_affinity", "CPU affinity"),
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
        from ..coordination import load_coordination

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

        self.coordination = load_coordination()

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

    def action_show_radiod(self, radiod_id: str = "") -> None:
        from .screens.radiod import RadiodScreen

        # Find the status_dns for this radiod from coordination.
        status_dns = ""
        if radiod_id and hasattr(self, 'coordination'):
            radiod = self.coordination.resolve_radiod(radiod_id)
            if radiod:
                status_dns = radiod.status_dns

        # If no specific radiod_id, try the first local one.
        if not radiod_id and hasattr(self, 'coordination'):
            for rid, r in self.coordination.radiods.items():
                radiod_id = rid
                status_dns = r.status_dns
                break

        # Fallback: discover from running radiod config files.
        if not status_dns:
            found_id, found_dns = _discover_radiod_from_config()
            if found_dns:
                status_dns = found_dns
                if not radiod_id:
                    radiod_id = found_id

        center = self.query_one("#center")
        center.remove_children()
        center.mount(RadiodScreen(radiod_id or "default", status_dns))

        ctx = self.query_one(ContextPanel)
        ctx.show_help(
            f"radiod: {radiod_id or 'default'}",
            "Live status from ka9q-python.\n\n"
            "Shows active channels, frontend health (GPSDO, "
            "calibration), and per-channel SNR.\n\n"
            "Press 'Deep dive' to launch ka9q-python's full "
            "TUI for detailed radiod control.",
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

    def action_show_cpu_affinity(self) -> None:
        from .screens.cpu_affinity import CPUAffinityScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(CPUAffinityScreen())

        ctx = self.query_one(ContextPanel)
        ctx.show_help(
            "CPU affinity",
            "Hardware topology, affinity plan, and observed state.\n\n"
            "Goal: keep radiod's USB3/FFT path uncontested by other "
            "processes — one physical core (HT pair) per radiod "
            "instance, everything else shares the rest.\n\n"
            "Read-only. To apply the plan, run:\n"
            "  sudo smd diag cpu-affinity --apply",
        )
