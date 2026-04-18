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
        Binding("o", "show_overview", "Overview"),
        Binding("t", "show_topology", "Topology"),
        Binding("c", "show_cpu_affinity", "CPU affinity"),
        Binding("r", "show_radiod", "Radiod"),
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
        # Default landing is the Topology screen until the Overview screen
        # lands (next commit).  Keeps parity with the pre-restructure TUI.
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

    def _mount_placeholder(self, title: str, description: str,
                           cli_hint: str, help_title: str,
                           help_body: str) -> None:
        from .screens.placeholder import PlaceholderScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(PlaceholderScreen(
            title=title, description=description, cli_hint=cli_hint))
        self.query_one(ContextPanel).show_help(help_title, help_body)

    def action_show_overview(self) -> None:
        self._mount_placeholder(
            title="Overview",
            description=(
                "Service health matrix, client inventory rollup, and "
                "CPU-affinity one-liner — a single landing screen that "
                "mirrors `smd status`."),
            cli_hint="smd status",
            help_title="Overview",
            help_body=(
                "High-level health and inventory for every managed "
                "component plus the CPU-affinity summary line.\n\n"
                "Coming soon — currently shows a placeholder."),
        )

    def action_show_cpu_freq(self) -> None:
        self._mount_placeholder(
            title="CPU frequency",
            description=(
                "Per-CPU scaling_max_freq view with the policy from "
                "[cpu_freq] in topology.toml — high for radiod cores, "
                "power-efficient elsewhere."),
            cli_hint="smd diag cpu-freq  (then --apply with sudo to set)",
            help_title="CPU frequency",
            help_body=(
                "Shows current scaling_max_freq per CPU against the "
                "topology [cpu_freq] policy.  Coming soon."),
        )

    def action_show_logs(self) -> None:
        self._mount_placeholder(
            title="Logs",
            description=(
                "Pick a component → tail its journal or file logs "
                "inline."),
            cli_hint="smd log <component>  [--files | --level LEVEL]",
            help_title="Logs",
            help_body=(
                "Per-component journal and file-log tailing.\n\n"
                "Coming soon."),
        )

    def action_show_lifecycle(self) -> None:
        self._mount_placeholder(
            title="Lifecycle",
            description=(
                "Start / stop / restart / reload managed services, "
                "with confirmation and live progress output."),
            cli_hint="sudo smd {start|stop|restart|reload}",
            help_title="Lifecycle",
            help_body=(
                "Manage the lifecycle of sigmond-managed services.\n\n"
                "Mutations acquire the lifecycle lock "
                "(/var/lib/sigmond/lifecycle.lock) and stream output "
                "into a log pane.  Coming soon."),
        )

    def action_show_install(self) -> None:
        self._mount_placeholder(
            title="Install",
            description=(
                "Browse the catalog of known clients and install a "
                "selected one."),
            cli_hint="sudo smd install [<client>]   |   smd list --available",
            help_title="Install",
            help_body=(
                "Catalog browser + installer.  Delegates to each "
                "client's own install.sh.  Coming soon."),
        )

    def action_show_update(self) -> None:
        self._mount_placeholder(
            title="Update",
            description=(
                "Pull the latest code and re-apply the current "
                "configuration."),
            cli_hint="sudo smd update",
            help_title="Update",
            help_body=(
                "Pull + apply in one step.  Coming soon."),
        )
