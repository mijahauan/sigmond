"""Sigmond TUI — three-panel configurator.

Left:   component tree with health indicators
Center: active screen (topology editor, validate, etc.)
Right:  contextual help and live system state
"""

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header
from textual.worker import WorkerState

from .widgets.component_tree import ComponentTree
from .widgets.context_panel import ContextPanel
from .widgets.panel_splitter import PanelSplitter


def _sigmond_version_string() -> str:
    """Return a short version string like 'v0.2.0-dev (#123)' for the header."""
    import subprocess
    try:
        from sigmond import __version__ as _ver
    except Exception:
        _ver = "?"
    repo = Path(__file__).resolve().parent.parent.parent.parent
    try:
        r = subprocess.run(
            ['git', '-C', str(repo), 'rev-list', '--count', 'HEAD'],
            capture_output=True, text=True, timeout=3)
        idx = r.stdout.strip()
        if idx:
            return f"v{_ver} (#{idx})"
    except Exception:
        pass
    return f"v{_ver}"


def _check_sigmond_version() -> dict:
    """Worker: fetch origin and check how many commits HEAD is behind.

    Returns a dict with keys: current, latest, behind (int), repo.
    Returns {} on any failure (no .git, no network, etc.).
    """
    import os, subprocess

    repo = Path(__file__).resolve().parent.parent.parent.parent
    if not (repo / '.git').exists():
        return {}

    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    try:
        idx_r = subprocess.run(
            ['git', '-C', str(repo), 'rev-list', '--count', 'HEAD'],
            capture_output=True, text=True, timeout=5)
        current_idx = idx_r.stdout.strip()

        subprocess.run(
            ['git', '-C', str(repo), 'fetch', '--quiet', 'origin'],
            capture_output=True, text=True, timeout=20, env=env)

        behind_r = subprocess.run(
            ['git', '-C', str(repo), 'rev-list', '--count', 'HEAD..origin/main'],
            capture_output=True, text=True, timeout=5)
        s = behind_r.stdout.strip()
        behind = int(s) if behind_r.returncode == 0 and s.isdigit() else 0

        latest_idx_r = subprocess.run(
            ['git', '-C', str(repo), 'rev-list', '--count', 'origin/main'],
            capture_output=True, text=True, timeout=5)
        latest_idx = latest_idx_r.stdout.strip()

        return {'current': current_idx, 'latest': latest_idx,
                'behind': behind, 'repo': str(repo)}
    except Exception:
        return {}


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
    SUB_TITLE = "Signal Monitor Daemon — Install / Configure / Monitor"

    CSS = """
    #main {
        height: 1fr;
    }
    #left {
        width: 30;
        padding: 0 1;
    }
    #center {
        width: 1fr;
        padding: 0 1;
    }
    #center > * {
        height: auto;
    }
    #right {
        width: 36;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("o", "show_overview", "Overview"),
        Binding("t", "show_topology", "Topology"),
        Binding("c", "show_cpu_affinity", "CPU affinity"),
        Binding("r", "show_radiod", "Radiod"),
        Binding("a", "show_rac", "RAC"),
        Binding("v", "show_validate", "Validate"),
        Binding("b", "show_backup", "Backup"),
        Binding("R", "show_restore", "Restore"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield ComponentTree(id="left")
            yield PanelSplitter(target_id="left", sign=1, min_width=20)
            yield VerticalScroll(id="center")
            yield PanelSplitter(target_id="right", sign=-1, min_width=24)
            yield ContextPanel(id="right")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = (
            "Signal Monitor Daemon — Install / Configure / Monitor"
            f"    {_sigmond_version_string()}"
        )
        self._load_system_view()
        self.action_show_overview()
        self.run_worker(_check_sigmond_version, thread=True,
                        name="sigmond-version-check")

    def on_worker_state_changed(self, event) -> None:
        if event.worker.name != "sigmond-version-check":
            return
        if event.state != WorkerState.SUCCESS:
            return
        info = event.worker.result or {}
        behind = info.get('behind', 0)
        current = info.get('current', '')
        ver_tag = f"v{__import__('sigmond').__version__} (#{current})" if current else f"v{__import__('sigmond').__version__}"
        if behind > 0:
            self.sub_title = (
                "Signal Monitor Daemon — Install / Configure / Monitor"
                f"    {ver_tag}  ⚠ {behind} update(s) available"
            )
            self._prompt_sigmond_update(info)
        elif info:
            self.sub_title = (
                "Signal Monitor Daemon — Install / Configure / Monitor"
                f"    {ver_tag}  ✔ up to date"
            )

    def _prompt_sigmond_update(self, info: dict) -> None:
        """Show a modal if sigmond is behind origin/main."""
        import subprocess as sp
        from .mutation import ConfirmModal

        behind  = info['behind']
        current = info.get('current', '?')
        latest  = info.get('latest', '?')
        repo    = info.get('repo', '')

        def _on_choice(do_update: bool) -> None:
            if not do_update:
                return
            # Check for local changes before attempting pull.
            dirty = sp.run(
                ['git', '-C', repo, 'status', '--porcelain'],
                capture_output=True, text=True)
            if dirty.stdout.strip():
                self.notify(
                    "Update aborted: uncommitted local changes present.\n"
                    "Run `git stash` then `git pull` manually in the sigmond repo.",
                    severity="error", timeout=8)
                return
            with self.suspend():
                result = sp.run(
                    ['git', '-C', repo, 'pull', '--ff-only'], check=False)
            if result.returncode != 0:
                self.notify(
                    f"git pull failed (exit {result.returncode}) — "
                    "update manually: cd ~/sigmond && git pull",
                    severity="error", timeout=8)
                return
            self.notify(
                "sigmond updated — please restart `smd tui` to run the new version.",
                severity="information", timeout=6)
            self.set_timer(4, self.exit)

        self.push_screen(
            ConfirmModal(
                title="sigmond update available",
                body=(
                    f"sigmond is [bold]{behind} commit(s)[/] behind origin/main.\n\n"
                    f"  Running:  [dim]#{current}[/]\n"
                    f"  Latest:   [bold]#{latest}[/]\n\n"
                    "Update now? sigmond will pull and exit so you can restart.\n"
                    "  [dim](choose Continue to skip and keep running as-is)[/]"
                ),
                yes_label="Update & Exit",
                yes_variant="warning",
                no_label="Continue",
                no_variant="success",
            ),
            _on_choice,
        )

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

    def action_show_components(self) -> None:
        from .screens.components import ComponentsScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(ComponentsScreen(self.topology.components))

        self.query_one(ContextPanel).show_help(
            "Software versions",
            "Every catalog component with its install status, "
            "current git ref, and version policy.\n\n"
            "Version policies:\n"
            "  latest — always pull the newest commit\n"
            "  ignore — skip during smd update\n"
            "  <ref>  — pin to a specific commit / branch\n\n"
            "Select a row to see the recent git history for that "
            "component, then use the buttons to change its policy.\n\n"
            "Changes are written to /etc/sigmond/topology.toml "
            "(requires write permission — run sudo smd tui if needed).",
        )

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

    def action_show_gpsdo(self) -> None:
        from .screens.gpsdo import GpsdoScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(GpsdoScreen())

        self.query_one(ContextPanel).show_help(
            "GPSDO live",
            "Live status from gpsdo-monitor reports in /run/gpsdo/.\n\n"
            "Shows each attached Leo Bodnar GPSDO: PLL lock, GPS fix, "
            "antenna health, A-level hint, output frequencies, and the "
            "radiod(s) it governs.\n\n"
            "Select a device row, then 'Deep dive' to launch "
            "gpsdo-monitor's full TUI focused on that device.",
        )

    def action_show_kiwisdr(self) -> None:
        from .screens.kiwisdr import KiwiSDRScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(KiwiSDRScreen())

        self.query_one(ContextPanel).show_help(
            "KiwiSDR live",
            "Discovers all KiwiSDRs on the local LAN by scanning port 8073.\n\n"
            "For each KiwiSDR found, fetches /status and /gps to show:\n"
            "  • Name and software version\n"
            "  • Connected users / max users\n"
            "  • GPS fix status and fix count\n"
            "  • Uptime and antenna description\n\n"
            "Press 'Rescan' to run a fresh port scan.",
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
        from .screens.overview import OverviewScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(OverviewScreen())

        self.query_one(ContextPanel).show_help(
            "Overview",
            "Service health, client inventory, and the CPU-affinity "
            "summary — everything `smd status` shows, in one place.\n\n"
            "Read-only.  Use the Configure, Observe, and Operate "
            "sections in the tree for specifics.",
        )

    def action_show_cpu_freq(self) -> None:
        from .screens.cpu_freq import CPUFreqScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(CPUFreqScreen())

        self.query_one(ContextPanel).show_help(
            "CPU frequency",
            "Per-CPU scaling_max_freq view against the [cpu_freq] "
            "policy in topology.toml.\n\n"
            "Radiod cores get high clock to keep the USB3/FFT path "
            "fed; the rest stay power-efficient.\n\n"
            "Read-only.  To apply:\n"
            "  sudo smd diag cpu-freq --apply",
        )

    def action_show_logs(self) -> None:
        from .screens.logs import LogsScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(LogsScreen())

        self.query_one(ContextPanel).show_help(
            "Logs",
            "Pick a component, then 'Follow journal' for "
            "`journalctl -u <unit> --follow` or 'Tail files' for "
            "the inventory log_paths.\n\n"
            "Press 'Stop' before switching components.  The log "
            "pane caps at 2000 lines.\n\n"
            "To change log level, use the CLI for now:\n"
            "  smd log <component> --level DEBUG",
        )

    def action_show_lifecycle(self) -> None:
        from .screens.lifecycle import LifecycleScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(LifecycleScreen())

        self.query_one(ContextPanel).show_help(
            "Lifecycle",
            "Start, stop, restart, or reload managed services.\n\n"
            "Every action pops a confirmation dialog with the exact "
            "command that will run.  On accept, the TUI suspends and "
            "`sudo smd <verb>` runs in the real terminal — you'll see "
            "the password prompt and live output there.  Returns to "
            "the TUI with the exit code.\n\n"
            "The CLI holds the lifecycle lock; the TUI does not.",
        )

    def action_show_install(self) -> None:
        from .screens.install import InstallScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(InstallScreen())

        self.query_one(ContextPanel).show_help(
            "Install",
            "Catalog of every known HamSCI client and server, with "
            "per-entry install status.\n\n"
            "Arrow to a row → 'Install selected' to install one, or "
            "'Install all missing' to run a catalog walk via "
            "`sudo smd install`.\n\n"
            "Each entry is installed via its own canonical install.sh; "
            "sigmond delegates, not duplicates.",
        )

    def action_show_backup(self) -> None:
        from .screens.backup import BackupScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(BackupScreen())

        self.query_one(ContextPanel).show_help(
            "Backup",
            "Snapshot every config file needed to restore this installation "
            "after an OS reinstall.\n\n"
            "Covers: sigmond topology, radiod channels, wsprdaemon.conf + "
            "env/ + certs, hf-timestd, psk-recorder, systemd units, "
            "sudoers, cron, logrotate.\n\n"
            "Saves to ~/sigmond-config-<hostname>-<date>.tar.gz\n\n"
            "Restore workflow:\n"
            "  ./install.sh\n"
            "  sudo tar xzf sigmond-config-*.tar.gz -C /\n"
            "  sudo smd apply",
        )

    def action_show_restore(self) -> None:
        from .screens.restore import RestoreScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(RestoreScreen())

        self.query_one(ContextPanel).show_help(
            "Restore",
            "Browse for a  sigmond-config-*.tar.gz  backup file and "
            "extract it over the live system.\n\n"
            "The tree starts in your home directory and shows only "
            "sigmond backup archives.\n\n"
            "Navigate with arrow keys, expand folders with Enter, "
            "select a file with Enter or double-click.\n\n"
            "After restore, run  sudo smd apply  to reconcile any "
            "service state changes.",
        )

    def action_show_rac(self) -> None:
        from .screens.rac import RacScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(RacScreen(self.topology))

        self.query_one(ContextPanel).show_help(
            "Remote Access Channel",
            "Configures frpc to open an authenticated reverse tunnel "
            "back to vpn.wsprdaemon.org.\n\n"
            "Two values are needed:\n"
            "  RAC ID — your site name (defaults to first receiver call)\n"
            "  RAC number — integer assigned by the RAC admin (emailed)\n\n"
            "After pressing 'Apply & enable', sigmond downloads the frpc "
            "binary, writes /etc/sigmond/frpc.ini, and starts the tunnel service.\n\n"
            "Once running, an admin can SSH to this site via:\n"
            "  ssh -p <35800+n> wsprdaemon@vpn.wsprdaemon.org",
        )

    def action_show_apply(self) -> None:
        from .screens.apply import ApplyScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(ApplyScreen())

        self.query_one(ContextPanel).show_help(
            "Apply",
            "Reconciles running services with the current topology + "
            "coordination config.\n\n"
            "Dry-run prints the plan without touching the system.  "
            "Apply performs it via `sudo smd apply` — services may "
            "restart.\n\n"
            "Safe to re-run — the CLI is idempotent.",
        )

    def action_show_config(self) -> None:
        from .screens.config_show import ConfigShowScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(ConfigShowScreen())

        self.query_one(ContextPanel).show_help(
            "Config view",
            "Read-only snapshot of coordination + client config.\n\n"
            "Equivalent to `smd config show`.  Shows radiod instances, "
            "their scope (local/remote), and which clients have "
            "declared contract-compliant inventory.\n\n"
            "'Migrate config' upgrades coordination to the latest "
            "schema (`sudo smd config migrate`).",
        )

    def action_show_diag_net(self) -> None:
        from .screens.diag_net import DiagNetScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(DiagNetScreen())

        self.query_one(ContextPanel).show_help(
            "Diag: network",
            "Classifies IGMP behavior so radiod multicast stays safe.\n\n"
            "Fast scan: unprivileged enumeration of interfaces + "
            "/proc/net/igmp (no wait).\n\n"
            "Full listen: runs `sudo smd diag net --listen <s>` to "
            "observe IGMP queries on the wire.  Requires passwordless "
            "sudo or you'll see an error here — fall back to a terminal "
            "if so.",
        )

    def action_show_environment(self) -> None:
        from .screens.environment import EnvironmentScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(EnvironmentScreen())

        self.query_one(ContextPanel).show_help(
            "Environment",
            "Situational awareness — declared peers vs observed.\n\n"
            "Reads /etc/sigmond/environment.toml (the operator-declared "
            "site baseline) and reconciles it against live discovery:\n"
            "  • mDNS browse (passive)\n"
            "  • ka9q-radio multicast status (passive)\n"
            "  • local gpsdo-monitor authority.json (passive)\n"
            "  • NTP SNTPv4 query (active)\n"
            "  • KiwiSDR /status + /gps (active)\n\n"
            "Active probes are rate-limited and skipped entirely when "
            "discovery.passive_only = true in the manifest.\n\n"
            "Keys: p probe all · m/n/k source-only · r reload manifest",
        )

    def action_show_update(self) -> None:
        from .screens.update import UpdateScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(UpdateScreen(self.topology.components))

        self.query_one(ContextPanel).show_help(
            "Update",
            "Pull the latest code for every installed catalog component.\n\n"
            "Update selected — pull one component by name.\n"
            "Update all — pull all components whose policy is not 'ignore'.\n"
            "Dry run — preview what would change without touching anything.\n\n"
            "Version policies (set under Configure → Software versions):\n"
            "  latest — always pull newest commit (default)\n"
            "  ignore — skip this component during updates\n"
            "  <ref>  — pin to a specific commit / branch / tag\n\n"
            "Equivalent to `sudo smd update [--components <name>]`.",
        )
