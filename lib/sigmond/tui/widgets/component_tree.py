"""Left-panel navigation tree — grouped by operator workflow.

The tree is the TUI's primary navigation surface.  Groups mirror the
four mental-model phases an operator moves through:

  Installation — first-time setup, rarely revisited.
  Maintenance — routine changes that keep the install healthy.
  Debugging   — diagnose + watch when something looks wrong.
  Monitoring  — day-to-day "is it working" surfaces.

Components do not appear as top-level entries — they show up inside
screens (Overview rollup, Radiod live, Lifecycle, Logs).  See
docs/TUI-FUNCTION-INVENTORY.md for the capability → screen mapping
that drove this layout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Tree

if TYPE_CHECKING:
    from ...topology import Topology


class ComponentTree(Tree):
    """Grouped navigation tree for the sigmond TUI."""

    def __init__(self, **kwargs) -> None:
        super().__init__("SigMonD", **kwargs)

    def populate(self, topology: "Topology", catalog: dict) -> None:
        """Build the tree.  topology and catalog are accepted for
        parity with the prior signature and for future per-screen
        health rollups; current tree is static w.r.t. host state."""
        del topology, catalog  # unused for now; reserved for per-node health

        self.clear()
        self.root.expand()

        self.root.add_leaf("\u25a3 Overview", data={"screen": "overview"})

        # Installation leads — everything that happens before a component can
        # run on this host \u2014 in the three-step arc an operator follows
        # once, top to bottom: \u2460 download & install (which also enables
        # the component in topology), \u2461 configure, \u2462 enable/start/stop.
        # Topology is no longer a leaf \u2014 it's derived state, surfaced by
        # step \u2462.  Guided bring-up runs all three for a whole profile.
        # Under-the-hood knobs live under Advanced, below.
        installation = self.root.add("Installation", expand=True)
        installation.add_leaf("\u2728 Guided bring-up",        data={"screen": "greenfield"})
        installation.add_leaf("\u2460 Download & install",     data={"screen": "install"})
        installation.add_leaf("\u2461 Configure",              data={"screen": "configuration"})
        installation.add_leaf("\u2462 Enable / start / stop",  data={"screen": "lifecycle"})

        monitoring = self.root.add("Monitoring", expand=True)
        monitoring.add_leaf("\u2316 Environment",        data={"screen": "environment"})
        monitoring.add_leaf("\u29b5 Timing & Authority", data={"screen": "timing_authority"})
        monitoring.add_leaf("\u2299 Annotation Quality", data={"screen": "annotation_quality"})
        monitoring.add_leaf("\u26a1 Activity",          data={"screen": "activity"})
        monitoring.add_leaf("\u25d0 GPSDO live",        data={"screen": "gpsdo"})
        monitoring.add_leaf("\u25c9 ka9q-radio live",   data={"screen": "radiod"})
        monitoring.add_leaf("\u25b6 KiwiSDR live",      data={"screen": "kiwisdr"})
        monitoring.add_leaf("\u2316 Receiver channels", data={"screen": "receiver_channels"})
        monitoring.add_leaf("\u21c6 RAC tunnel",        data={"screen": "rac"})
        monitoring.add_leaf("\u2b22 Resources",         data={"screen": "resources"})


        # Maintenance: ongoing operational changes once components are
        # running \u2014 apply config edits, per-instance source assignment,
        # save/restore the host's overall config.  (Lifecycle moved up to
        # Installation step \u2462 Enable / start / stop.)
        maintenance = self.root.add("Maintenance", expand=True)
        maintenance.add_leaf("\u21c4 Apply",            data={"screen": "apply"})
        maintenance.add_leaf("\u2604 Sources",          data={"screen": "sources"})
        maintenance.add_leaf("\u2193 Backup",           data={"screen": "backup"})
        maintenance.add_leaf("\u2191 Restore",          data={"screen": "restore"})

        debugging = self.root.add("Debugging", expand=True)
        debugging.add_leaf("\u2261 Logs",               data={"screen": "logs"})
        debugging.add_leaf("\u2697 Verifier",           data={"screen": "verifier"})
        debugging.add_leaf("\u2714 Validate",           data={"screen": "validate"})
        debugging.add_leaf("\u2726 Diag: net",          data={"screen": "diag_net"})
        debugging.add_leaf("\u25ce ka9q-watch",         data={"screen": "ka9q_watch"})

        # Advanced / under-the-hood: knobs you set rarely, displaced from the
        # Installation arc so the three steps stay uncluttered (install-redesign
        # \u00a75 \u2014 SDR inventory stays here, NOT folded into Configure).
        advanced = self.root.add("Advanced", expand=False)
        advanced.add_leaf("\u2691 Software versions", data={"screen": "components"})
        advanced.add_leaf("\u229e SDR inventory",     data={"screen": "sdr_inventory"})
        advanced.add_leaf("\u2699 CPU affinity",      data={"screen": "cpu_affinity"})
        advanced.add_leaf("\u21f5 CPU frequency",     data={"screen": "cpu_freq"})
        advanced.add_leaf("\u2a09 FFT Wisdom",        data={"screen": "fft_wisdom"})

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data:
            return
        screen = data.get("screen")
        if screen == "overview":
            self.app.action_show_overview()
        elif screen == "greenfield":
            self.app.action_show_greenfield()
        elif screen == "topology":
            self.app.action_show_topology()
        elif screen == "components":
            self.app.action_show_components()
        elif screen == "cpu_affinity":
            self.app.action_show_cpu_affinity()
        elif screen == "cpu_freq":
            self.app.action_show_cpu_freq()
        elif screen == "radiod":
            self.app.action_show_radiod()
        elif screen == "receiver_channels":
            self.app.action_show_receiver_channels()
        elif screen == "resources":
            self.app.action_show_resources()
        elif screen == "gpsdo":
            self.app.action_show_gpsdo()
        elif screen == "authority":
            self.app.action_show_authority()
        elif screen == "timing_authority":
            self.app.action_show_timing_authority()
        elif screen == "annotation_quality":
            self.app.action_show_annotation_quality()
        elif screen == "timing":
            self.app.action_show_timing()
        elif screen == "kiwisdr":
            self.app.action_show_kiwisdr()
        elif screen == "sdr_inventory":
            self.app.action_show_sdr_inventory()
        elif screen == "logs":
            self.app.action_show_logs()
        elif screen == "validate":
            self.app.action_show_validate()
        elif screen == "lifecycle":
            self.app.action_show_lifecycle()
        elif screen == "install":
            self.app.action_show_install()
        elif screen == "rac":
            self.app.action_show_rac()
        elif screen == "backup":
            self.app.action_show_backup()
        elif screen == "restore":
            self.app.action_show_restore()
        elif screen == "apply":
            self.app.action_show_apply()
        elif screen == "sources":
            self.app.action_show_sources()
        elif screen == "instance":
            self.app.action_show_instance()
        elif screen == "configuration":
            self.app.action_show_configuration()
        elif screen == "activity":
            self.app.action_show_activity()
        elif screen == "verifier":
            self.app.action_show_verifier()
        elif screen == "fft_wisdom":
            self.app.action_show_fft_wisdom()
        elif screen == "config_show":
            self.app.action_show_config()
        elif screen == "client_config":
            self.app.action_show_client_config()
        elif screen == "ka9q_watch":
            self.app.action_show_ka9q_watch()
        elif screen == "diag_net":
            self.app.action_show_diag_net()
        elif screen == "environment":
            self.app.action_show_environment()
