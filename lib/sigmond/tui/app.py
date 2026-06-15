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

    TITLE = "SigMonD"
    SUB_TITLE = "Signal Monitor Daemon — Monitor / Maintain / Debug / Install"

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

    /* App-wide Switch colour convention: ON = green, OFF = red.
       Textual's default already paints the ON-state slider with
       `$success` (green) — we just override the OFF-state slider
       (default `$panel`, a mid-grey that disappears on dark themes)
       to use `$error` so disabled/false reads as a clear "no".
       Applies to every Switch in the TUI: wizard, Activity verbose
       toggle, radiod deep-dive AGC, etc. */
    Switch > .switch--slider {
        color: $error;
    }
    Switch:hover > .switch--slider {
        color: $error 80%;
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
        Binding("C", "show_client_config", "Client config"),
        Binding("K", "show_ka9q_watch", "ka9q-watch"),
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
            "Signal Monitor Daemon — Monitor / Maintain / Debug / Install"
            f"    {_sigmond_version_string()}"
        )
        self._load_system_view()
        # Greenfield-aware landing: a host with nothing enabled yet is
        # mid-install — lead with the guided Greenfield bring-up wizard
        # rather than an empty Overview.  Once any component is enabled,
        # land on Overview.
        try:
            greenfield = not self.topology.enabled_components()
        except Exception:
            greenfield = False
        if greenfield:
            self.action_show_greenfield()
        else:
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
                "Signal Monitor Daemon — Monitor / Maintain / Debug / Install"
                f"    {ver_tag}  ⚠ {behind} update(s) available"
            )
            self._prompt_sigmond_update(info)
        elif info:
            self.sub_title = (
                "Signal Monitor Daemon — Monitor / Maintain / Debug / Install"
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
                client_dir=__import__('pathlib').Path('/opt/git/sigmond'),
                smd_bin=__import__('pathlib').Path('/usr/local/bin/smd'),
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
            "List",
            "Every catalog component with its install status, current "
            "git ref, divergence from upstream, and version policy.\n\n"
            "Version policies:\n"
            "  latest — always pull the newest commit\n"
            "  ignore — skip when applying updates (developer mode)\n"
            "  <ref>  — pin to a specific commit / branch\n\n"
            "Select a row to see the recent git history for that "
            "component, then use the buttons to change its policy.\n\n"
            "Changes are written to /etc/sigmond/topology.toml "
            "(requires write permission — run smd tui if needed).\n\n"
            "CLI equivalent: `smd list` (status); `smd component update` "
            "(pull + reapply per policy).",
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

    def action_show_resources(self) -> None:
        from .screens.resources import ResourcesScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(ResourcesScreen())

        self.query_one(ContextPanel).show_help(
            "Resources",
            "System + sigmond storage summary.  Four sections:\n\n"
            "• System — memory / load / uptime from /proc.\n"
            "• Filesystems — mountpoint capacity for the FS roots "
            "sigmond writes into (/var, /tmp, /dev/shm) with free "
            "space.\n"
            "• Sigmond data — per-client spool / log sizes, and "
            "where a daily-rotated file pattern can be detected "
            "(mag-recorder samples-YYYY-MM-DD.jsonl; codar "
            "<station>/YYYY/MM/DD.jsonl), an average daily growth "
            "figure and a 'days @ growth' estimate against the "
            "owning mount's free space.\n"
            "• SQLite sink — per-(db, table) row counts in sink.db's "
            "pending_uploads queue, with oldest/newest queued "
            "timestamps and a lag column so the operator can spot a "
            "stalled consumer (e.g. wspr verifier behind on its "
            "sweep).\n\n"
            "Read-only, no sudo.  Permission-denied subtrees are "
            "silently counted in the Notes column.",
        )

    def action_show_receiver_channels(self) -> None:
        from .screens.receiver_channels import ReceiverChannelsScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(ReceiverChannelsScreen())

        self.query_one(ContextPanel).show_help(
            "Receiver channels",
            "For a chosen <client>@<reporter_id>, shows the radiod "
            "the client is consuming from and every live channel "
            "(unique SSRC) on that radiod whose frequency matches "
            "the client's per-instance config.\n\n"
            "Useful for confirming that every WSPR / FT8 / FT4 / "
            "HFDL / CODAR sub-band the client expects has an "
            "active receiver SSRC, and for spotting orphans or "
            "frequency mismatches between config and live state.\n\n"
            "Read-only: this screen never mutates radiod state.",
        )

    def action_show_authority(self) -> None:
        from .screens.authority import AuthorityScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(AuthorityScreen())

        self.query_one(ContextPanel).show_help(
            "Authority — substrate view",
            "Live view of hf-timestd's /run/hf-timestd/authority.json — "
            "the per-cycle (A, T) annotation that drives every §18 "
            "consumer's notion of UTC.\n\n"
            "Shows the active tier (T6 / T5 / T4 / T3 / …) with its "
            "rtp_to_utc offset and σ, the snapshot's publication "
            "age (red when stale beyond 60 s), the governor radiod, "
            "available tiers + witnesses, and any cross-check "
            "disagreement flags.\n\n"
            "This is the substrate view: what hf-timestd thinks the "
            "timing budget is.  For what chrony's selection algorithm "
            "does with this information, see Observe / Timing.\n\n"
            "Per ARCHITECTURE-FIRST-PRINCIPLES.md §5: chrony is a "
            "downstream consumer, not the architectural design "
            "centre.\n\n"
            "Refresh: 1 s.  authority.json itself ticks every ~30 s.",
        )

    def action_show_annotation_quality(self) -> None:
        from .screens.annotation_quality import AnnotationQualityScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(AnnotationQualityScreen())

        self.query_one(ContextPanel).show_help(
            "Annotation Quality — per-consumer science verdict",
            "Per-stream view of how the global RTP→UTC authority is "
            "currently labelling each running science consumer's data.\n\n"
            "One row per running consumer instance (timestd-metrology@*, "
            "wspr-recorder@*, psk-recorder@*, hfdl-recorder@*, "
            "codar-sounder@*, mag-recorder) with the active tier, "
            "honest σ, and verdict colour attached.\n\n"
            "Verdict thresholds (per the 2026-05-24 substrate eval):\n"
            "  GREEN  σ < 100 µs   — science-grade annotation\n"
            "  YELLOW σ < 10 ms    — degraded but usable for envelope-\n"
            "                         detection science (WWV-class)\n"
            "  RED    σ ≥ 10 ms    — V1 anchor-staleness regime; \n"
            "                         downstream consumers should gate\n\n"
            "The substrate panel beneath explains *why* the verdict is "
            "what it is: local-minus-source residual, breach state, and "
            "recapture history from the core-recorder drift monitor.\n\n"
            "Companion screens:\n"
            "  Monitoring / Authority — substrate view of authority.json\n"
            "  Monitoring / Timing    — chrony facade (downstream)\n\n"
            "Refresh: 1 s.  Authority publishes every ~30 s; substrate "
            "drift-monitor block updates every ~1 s.",
        )

    def action_show_timing(self) -> None:
        from .screens.timing import TimingScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(TimingScreen())

        self.query_one(ContextPanel).show_help(
            "Timing — chrony sources",
            "Live chrony source comparison with HPPS (T6 path: TS-1 "
            "BPSK-PPS via the RX-888 ADC) as the reference.\n\n"
            "Each row shows Δ-from-HPPS (not from the system clock, "
            "which is what 'chronyc sources' shows by default), reach "
            "as N/8, sample age, sigma, and a Unicode sparkline of "
            "the last 60 seconds.\n\n"
            "The header shows kernel-clock-vs-UTC plus root "
            "dispersion (chrony's conservative bound on its UTC "
            "estimate).\n\n"
            "This is the chrony-facade view (one downstream "
            "consumer's selection algorithm).  For the underlying "
            "substrate state — what hf-timestd thinks the timing "
            "budget is — see Observe / Authority.\n\n"
            "Refresh: 1 s.  History: 60 s.",
        )

    def action_show_sdr_inventory(self) -> None:
        from .screens.sdr_inventory import SdrInventoryScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(SdrInventoryScreen())

        self.query_one(ContextPanel).show_help(
            "SDR Inventory",
            "Discovers all SDR receivers visible to this host:\n\n"
            "  USB  — local SDRs on the USB bus (RX-888, RTL-SDR,\n"
            "         Airspy HF+, LimeSDR, etc.) detected via lsusb\n\n"
            "  KiwiSDR — LAN port-8073 scan + /status + /gps probe\n\n"
            "  ka9q — frontends being served by ka9q-radio instances\n\n"
            "Select a row and press [bold]e[/] (or the Label button) to "
            "assign a name — e.g. 'Omni', 'Kiwi North', 'RX-888 HF'.  "
            "Labels are stored in /var/lib/sigmond/sdr-labels.toml and "
            "used by configuration screens to refer to devices by name.",
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
            "  smd admin diag cpu-affinity --apply",
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
            "Read-only.  Use the Monitoring, Maintenance, Debugging, "
            "and Installation sections in the tree for specifics.",
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
            "  smd admin diag cpu-freq --apply",
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
            "  smd admin log set-level <component> DEBUG",
        )

    def action_show_lifecycle(self) -> None:
        from .screens.lifecycle import LifecycleScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(LifecycleScreen())

        self.query_one(ContextPanel).show_help(
            "Lifecycle",
            "Pick one or more targets with the checkboxes, then click "
            "Start / Stop / Restart at the bottom.  Each action button "
            "shows the current selection count.\n\n"
            "Selection (Textual's native 2-click model):\n"
            "  • Click a checkbox cell once to highlight it, again to "
            "toggle.\n"
            "  • Click the column header (□ / ▣ / ■) to select-all or "
            "clear-all.  ▣ = some-but-not-all checked.\n"
            "  • Keyboard: Tab to focus the table, arrows to move the "
            "cursor, Enter to toggle the cursored row (single keystroke).\n\n"
            "Row types:\n"
            "  • instance rows (psk-recorder@AC0G-B1 etc.) act via "
            "`sudo systemctl <verb> <unit>` direct — single-unit actions "
            "don't need the cross-component lifecycle lock.\n"
            "  • component rows (hf-timestd, mag-recorder, gpsdo-monitor) "
            "act via `smd <verb> --components <name>` so sigmond's "
            "lifecycle CLI orders the sub-units.  Component rows show "
            "an active/total fraction (e.g. partial 21/24).\n\n"
            "Mixed selections chain: one confirm dialog up front, then "
            "the instance batch runs, then the component batch.",
        )

    def action_show_greenfield(self) -> None:
        from .screens.greenfield import GreenfieldScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(GreenfieldScreen())

        self.query_one(ContextPanel).show_help(
            "Guided bring-up",
            "The CLI-free path from a blank host to a running station.\n\n"
            "Pick a station profile, enter your identity once (reporter id "
            "+ grid are required; callsign + PSWS id optional), and press "
            "Begin.  Sigmond installs the software, configures every "
            "component, builds FFT wisdom, and starts the station — "
            "streaming live progress.\n\n"
            "Profiles:\n"
            "  dasi2  — canonical DASI2 station (RX888 + WSPR + PSK + timing "
            "+ GPSDO + magnetometer)\n"
            "  base   — local radiod + timing only\n"
            "  client — decode-only against a REMOTE radiod\n\n"
            "radiod is configured with antenna defaults; fine-tune the "
            "antenna afterwards with the verdict's Edit antenna action "
            "(`smd config edit radiod`).\n\n"
            "CLI equivalent: `smd bringup <profile>`.",
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
            "`smd install`.\n\n"
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
            "  smd apply",
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
            "After restore, run  smd apply  to reconcile any "
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
            "Apply performs it via `smd apply` — services may "
            "restart.\n\n"
            "Safe to re-run — the CLI is idempotent.",
        )

    def action_show_configuration(self) -> None:
        from .screens.configuration import ConfigurationScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(ConfigurationScreen())

        self.query_one(ContextPanel).show_help(
            "Configuration",
            "Per-instance setup, in one place.  Replaces the legacy "
            "Client config + Config view + Instance screens during the "
            "prototype window.\n\n"
            "Each row is one configured `<client>@<reporter-id>` "
            "instance.  The Client column says which recorder; Reporter "
            "ID is operator-meaningful and path-safe (e.g. AC0G-B1, "
            "shown in user-facing AC0G/B1 form); Sources is the per-"
            "instance sources file's first entry; Cfg/Env/Srcs is a "
            "✓/- triple for the three files that make up an instance; "
            "Unit is the systemctl ActiveState.\n\n"
            "Workflow for a fresh instance:\n"
            "  1. Fill the [bold]Client[/] + [bold]Reporter[/] inputs "
            "in the Add row and click Add.  Sigmond creates the per-"
            "instance config / env / sources skeletons.  Reporter ID is "
            "parsed leniently — `AC0G/B1` becomes `AC0G-B1` internally.\n"
            "  2. Click Edit on the new row to open the client's "
            "config wizard (whiptail, or `$EDITOR` fallback).  Fill in "
            "antenna / SDR / per-mode settings.\n"
            "  3. Enable + start the unit from the Lifecycle screen "
            "(or `smd admin instance enable <client> <reporter>` from "
            "the CLI).\n\n"
            "Edit existing: select a row, click Edit.  Remove: select a "
            "row, click Remove (does NOT stop the unit — "
            "`smd admin instance disable` is a separate step).\n\n"
            "Migrate: scans for legacy radiod-keyed deployments and "
            "shows what would convert.  The actual interactive "
            "migration prompts per candidate and is CLI-only — run "
            "`smd admin instance migrate --yes` in a terminal.",
        )

    def action_show_instance(self) -> None:
        from .screens.instance import InstanceScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(InstanceScreen())

        self.query_one(ContextPanel).show_help(
            "Instance",
            "Per-reporter client instance lifecycle (sigmond's "
            "MULTI-INSTANCE-ARCHITECTURE.md §3).\n\n"
            "Each instance is one deployment context of a recorder "
            "client (psk-recorder, wspr-recorder, hfdl-recorder, "
            "codar-sounder, mag-recorder) keyed by an operator-"
            "meaningful reporter ID (e.g. AC0G-B1).\n\n"
            "Listing: read-only view of /etc/<client>/<reporter-id>.toml "
            "files across known clients.  Refresh re-walks the catalog.\n\n"
            "Add: creates per-instance config / env / sources files "
            "(does NOT enable or start the unit — that's `smd admin instance "
            "enable` after editing the config).\n\n"
            "Remove: deletes per-instance files.  Doesn't touch the "
            "systemd unit (run `smd admin instance disable` first if "
            "the unit is running) or state/log/run dirs (use `--purge` "
            "from the CLI for that).\n\n"
            "Migrate: scans for legacy radiod-keyed deployments "
            "(`<client>@<radiod-id>.service`).  Dry-run lists "
            "candidates here; the actual interactive migration is "
            "CLI-only — run `smd admin instance migrate --yes` in a "
            "terminal.",
        )

    def action_show_sources(self) -> None:
        from .screens.sources import SourcesScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(SourcesScreen())

        self.query_one(ContextPanel).show_help(
            "Sources",
            "Per-client sensor-feed selection — which radiod control "
            "plane or KiwiSDR (future: magnetometer, VLF) each recorder "
            "consumes from.\n\n"
            "Selections live at /etc/sigmond/clients/<client>.sources.toml. "
            "Refresh re-runs `smd admin sources list`; Apply (dry-run) previews "
            "what would be written; Apply runs `smd admin sources apply` "
            "to render the selections into each client's config.\n\n"
            "Add/remove of individual selections is CLI-only for now:\n"
            "  smd admin sources add <client> <kind>:<id>\n"
            "  smd admin sources remove <client> <kind>:<id>\n"
            "Then return here and press Apply.",
        )

    def action_show_activity(self) -> None:
        from .screens.activity import ActivityScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(ActivityScreen())

        self.query_one(ContextPanel).show_help(
            "Activity",
            "Live tail of per-target recorder, uploader, and verifier "
            "activity — equivalent to `smd watch <target>` in a terminal.\n\n"
            "Pick a target (wspr, psk, hfdl, codar, ka9q, uploads, "
            "verifier), press Start to stream the watcher's stdout into "
            "the output pane.  Switching targets while one is running "
            "implicitly replaces it.\n\n"
            "Stop terminates the subprocess (SIGTERM, then SIGKILL after "
            "2 s).  Clear empties the output without stopping the stream. "
            "Leaving the screen also stops the stream — no orphaned "
            "subprocesses.",
        )

    def action_show_timing_authority(self) -> None:
        from .screens.timing_authority import TimingAuthorityScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(TimingAuthorityScreen())

        self.query_one(ContextPanel).show_help(
            "Timing & Authority",
            "Combined monitoring view: hf-timestd's substrate "
            "authority (active tier / σ / witnesses, read from "
            "/run/hf-timestd/authority.json) on top, plus chrony's "
            "facade view (sources vs HPPS, root dispersion) below.\n\n"
            "The natural reading order is top-down: what hf-timestd "
            "thinks the timing budget is, then what chrony does with "
            "that information. Operators gating hard-deadline captures "
            "should consult the Authority section; operators tracking "
            "host-clock health watch the Timing section.",
        )

    def action_show_verifier(self) -> None:
        from .screens.verifier import VerifierScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(VerifierScreen())

        self.query_one(ContextPanel).show_help(
            "Verifier",
            "Wsprnet upload audit (report) + per-callsign suppression "
            "clear (rehabilitate).\n\n"
            "Report shows the cohort of spots that uploaded but never "
            "appeared in wspr.rx — broken down into lost / in-flight / "
            "delivered / rejected. Toggle the detail checkboxes to list "
            "individual spots under the summary. Window accepts s/m/h/d "
            "suffix (default 1h). Target switches between the WSPRnet "
            "upload path (wspr) and the FT8/FT4 SQLite forwarding queue "
            "(psk).\n\n"
            "Rehabilitate clears the wsprnet_reject_cache suppression "
            "for one (rx_call, call) pair so wsprd/jt9 are re-fed the "
            "callsign on the next decode cycle. Requires root; "
            "confirm-modal-gated.",
        )

    def action_show_fft_wisdom(self) -> None:
        from .screens.fft_wisdom import FFTWisdomScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(FFTWisdomScreen())

        self.query_one(ContextPanel).show_help(
            "FFT Wisdom",
            "Generates the FFTW wisdom file that radiod needs to plan "
            "its FFT transforms efficiently.\n\n"
            "Small channel-inverse transforms (cob*) finish in seconds. "
            "The large forward real transforms (rof3240000 for an RX888 "
            "@ 129.6 MHz) can take hours on the first run.\n\n"
            "All managed services are stopped while planning runs so "
            "they don't compete for CPU.  The planner is pinned to one "
            "CPU core to prevent migration.\n\n"
            "Once wisdom is built, run Apply to start radiod and the "
            "decoder chain.",
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
            "schema (`smd config migrate`).",
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
            "Full listen: runs `smd admin diag net --listen <s>` to "
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

    def action_show_client_config(self) -> None:
        from .screens.client_config import ClientConfigScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(ClientConfigScreen())

        self.query_one(ContextPanel).show_help(
            "Client config",
            "Run a client's first-run wizard or edit its config file.\n\n"
            "Init wizard — `smd config init <client>` — invokes "
            "the entry point each client advertises in its deploy.toml "
            "[contract.config].init.  radiod uses the sigmond-owned "
            "wizard (probe USB SDRs, render radiod@<id>.conf).\n\n"
            "Edit config — `smd config edit <client>` — invokes "
            "the client's edit hook, or falls back to $EDITOR on the "
            "config file.\n\n"
            "Library-kind catalog entries (e.g. ka9q-python) are "
            "excluded — they have no operator-facing config.",
        )

    def action_show_ka9q_watch(self) -> None:
        from .screens.ka9q_watch import Ka9qWatchScreen
        center = self.query_one("#center")
        center.remove_children()
        center.mount(Ka9qWatchScreen())

        self.query_one(ContextPanel).show_help(
            "ka9q-watch",
            "Compare the pinned ka9q-radio commit (ka9q_radio_compat) "
            "against origin/main.  Read-only — no sudo, no mutation.\n\n"
            "Severity:\n"
            "  green  — no upstream change, or no header touched\n"
            "  yellow — header touched but no stream-critical field\n"
            "  red    — stream-critical field shifted; advancing the "
            "pin without updating ka9q-python would break RTP "
            "delivery to clients.\n\n"
            "Refresh — re-run with cached refs.\n"
            "Refresh + git fetch — pull latest from upstream first.\n\n"
            "CLI equivalent: `smd watch ka9q`.",
        )

    # The old action_show_update mounted a duplicate UpdateScreen.  The
    # List screen (action_show_components) now does both display and
    # apply, so this action is just an alias kept so existing keybindings
    # and component_tree clicks keep working.
    def action_show_update(self) -> None:
        self.action_show_components()
