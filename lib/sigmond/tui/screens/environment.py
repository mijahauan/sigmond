"""Environment screen — three-pane situational-awareness view.

Left:   declared peers (from /etc/sigmond/environment.toml)
Middle: observations from the last discovery pass
Right:  per-declared-peer delta status + any unknown-extras

Worker thread runs probes off the UI thread using the in-process
`sigmond.discovery` modules (same code path as `smd environment probe`).
"""

from __future__ import annotations

import time

from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static
from textual.worker import Worker, WorkerState

from ... import discovery
from ...discovery import reconciler as _reconciler
from ...environment import Environment, EnvironmentView, Observation, load_environment


STATUS_STYLE = {
    "healthy":       "[green]✓ healthy[/]",
    "degraded":      "[yellow]⚠ degraded[/]",
    "missing":       "[red]✗ missing[/]",
    "unknown-extra": "[cyan]? extra[/]",
}


def _run_probe(sources: list, timeout: float,
               force: bool, wanted_kind: str | None) -> dict:
    """Worker body — load manifest, probe, reconcile, cache, return view."""
    env = load_environment()
    actual = discovery.resolve_sources(env, None)
    actual = [s for s in actual if not sources or s in sources]
    limiter = discovery.RateLimiter()

    observations: list[Observation] = []
    errors: list[str] = []
    for src in actual:
        module = discovery.module_for_source(src)
        if module is None:
            continue
        try:
            obs = module.probe(env, timeout=timeout, limiter=limiter)
        except Exception as e:                   # noqa: BLE001
            errors.append(f"{src}: {e.__class__.__name__}: {e}")
            continue
        if wanted_kind:
            obs = [o for o in obs if o.kind == wanted_kind]
        observations.extend(obs)

    deltas = _reconciler.reconcile(env, observations)
    view = EnvironmentView(env=env, observations=observations,
                           deltas=deltas, probed_at=time.time())
    try:
        discovery.save_cache(view)
    except OSError:
        pass
    return {"view": view, "errors": errors, "sources": actual}


def _load_cached() -> EnvironmentView:
    env = load_environment()
    cache = discovery.load_cache()
    obs = [discovery.dict_to_obs(o) for o in cache.get("observations", [])]
    deltas = _reconciler.reconcile(env, obs)
    return EnvironmentView(env=env, observations=obs, deltas=deltas,
                           probed_at=float(cache.get("probed_at", 0.0) or 0.0))


class EnvironmentScreen(Vertical):
    """Situational-awareness inventory."""

    BINDINGS = [
        ("p", "probe_all",        "Probe all sources"),
        ("m", "probe_mdns",       "mDNS only"),
        ("n", "probe_ntp",        "NTP only"),
        ("k", "probe_kiwi",       "KiwiSDR only"),
        ("w", "probe_ka9q_web",   "ka9q-web only"),
        ("v", "probe_gnss",       "GNSS-VTEC only"),
        ("s", "probe_snmp",       "Network (SNMP) only"),
        ("u", "probe_usb",        "USB SDR only"),
        ("r", "reload_manifest",  "Reload manifest"),
    ]

    DEFAULT_CSS = """
    EnvironmentScreen {
        padding: 1;
    }
    EnvironmentScreen .env-title {
        text-style: bold;
        margin-bottom: 1;
    }
    EnvironmentScreen #env-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    EnvironmentScreen .env-section {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    EnvironmentScreen #env-panes {
        height: 1fr;
    }
    EnvironmentScreen #env-panes > Vertical {
        width: 1fr;
        margin-right: 1;
    }
    """

    def compose(self):
        yield Static("Environment — site inventory", classes="env-title")
        yield Static("", id="env-status")

        with Horizontal(id="env-panes"):
            with Vertical():
                yield Static("Declared", classes="env-section")
                dt = DataTable(id="env-declared")
                dt.add_columns("Kind", "Id", "Host", "Extra")
                yield dt
            with Vertical():
                yield Static("Observed", classes="env-section")
                ot = DataTable(id="env-observed")
                ot.add_columns("Source", "Kind", "Peer", "Endpoint", "OK")
                yield ot
            with Vertical():
                yield Static("Deltas", classes="env-section")
                xt = DataTable(id="env-deltas")
                xt.add_columns("Status", "Kind", "Id", "Detail")
                yield xt

        yield Static(
            "[dim]p=all  m=mdns  n=ntp  k=kiwi  w=ka9q-web  v=gnss-vtec  "
            "s=snmp  u=usb  r=reload[/]",
            id="env-hint")

    def on_mount(self) -> None:
        self._render_view(_load_cached(), banner="cached")

    # ------------------------------------------------------------------ actions

    def action_probe_all(self) -> None:
        self._kick(sources=[], label="all")

    def action_probe_mdns(self) -> None:
        self._kick(sources=["mdns"], label="mdns")

    def action_probe_ntp(self) -> None:
        self._kick(sources=["ntp"], label="ntp")

    def action_probe_kiwi(self) -> None:
        self._kick(sources=["http_kiwisdr"], label="kiwi")

    def action_probe_ka9q_web(self) -> None:
        self._kick(sources=["http_ka9q"], label="ka9q-web")

    def action_probe_gnss(self) -> None:
        self._kick(sources=["http_gnss"], label="gnss-vtec")

    def action_probe_snmp(self) -> None:
        self._kick(sources=["snmp"], label="snmp")

    def action_probe_usb(self) -> None:
        self._kick(sources=["usb_sdr"], label="usb_sdr")

    def action_reload_manifest(self) -> None:
        self._render_view(_load_cached(), banner="manifest reloaded")

    # ------------------------------------------------------------------ worker

    def _kick(self, sources: list, label: str) -> None:
        self.query_one("#env-status", Static).update(
            f"[dim]probing {label}…[/]")
        self.run_worker(
            lambda: _run_probe(sources, timeout=3.0, force=False, wanted_kind=None),
            thread=True, name="env-probe",
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "env-probe":
            return
        if event.state != WorkerState.SUCCESS:
            return
        result = event.worker.result
        if not isinstance(result, dict) or "view" not in result:
            return
        view: EnvironmentView = result["view"]
        errs = result.get("errors") or []
        banner = f"probe complete  ({len(result.get('sources') or [])} sources)"
        if errs:
            banner += f"  [yellow]errors: {len(errs)}[/]"
        self._render_view(view, banner=banner, errors=errs)

    # ------------------------------------------------------------------ render

    def _render_view(self, view: EnvironmentView,
                banner: str = "",
                errors: list | None = None) -> None:
        status = ""
        if view.env.source_path:
            status += f"manifest: {view.env.source_path}  "
        if view.probed_at:
            age = time.time() - view.probed_at
            status += f"last probe: {int(age)}s ago  "
        else:
            status += "last probe: never  "
        if banner:
            status += f"[green]{banner}[/]"
        if errors:
            status += "  [red]" + "; ".join(errors[:3]) + "[/]"
        self.query_one("#env-status", Static).update(status)

        # Declared
        dt = self.query_one("#env-declared", DataTable)
        dt.clear()
        for kind, d in view.env.iter_declared():
            extra = _declared_extra(kind, d)
            dt.add_row(kind, d.id, getattr(d, "host", "") or "—", extra)
        if dt.row_count == 0:
            dt.add_row("[dim]—[/]", "[dim]no manifest[/]", "", "")

        # Observed
        ot = self.query_one("#env-observed", DataTable)
        ot.clear()
        for o in view.observations:
            peer = o.id or "[dim]—[/]"
            mark = "[green]✓[/]" if o.ok else "[red]✗[/]"
            ot.add_row(o.source, o.kind, peer, o.endpoint, mark)
        if ot.row_count == 0:
            ot.add_row("[dim]—[/]", "", "no observations yet", "", "")

        # Deltas
        xt = self.query_one("#env-deltas", DataTable)
        xt.clear()
        order = {"missing": 0, "degraded": 1, "unknown-extra": 2, "healthy": 3}
        for d in sorted(view.deltas, key=lambda x: order.get(x.status, 9)):
            xt.add_row(
                STATUS_STYLE.get(d.status, d.status),
                d.kind, d.id, d.detail or "—",
            )
        if xt.row_count == 0:
            xt.add_row("[dim]—[/]", "", "", "")


def _declared_extra(kind: str, d) -> str:
    if kind == "radiod":
        return d.status_dns or "—"
    if kind == "kiwisdr":
        return f"port {d.port}"
    if kind == "gpsdo":
        return d.kind or "—"
    if kind == "time_source":
        bits = [d.kind]
        if d.stratum_max:
            bits.append(f"≤S{d.stratum_max}")
        return " ".join(bits) or "—"
    if kind == "ka9q_web":
        bits = [f"port {d.port}"]
        if d.role:
            bits.append(d.role)
        return " ".join(bits)
    if kind == "gnss_vtec":
        bits = [f"port {d.port}"]
        if d.source:
            bits.append(d.source)
        return " ".join(bits)
    if kind == "network_device":
        return d.kind or "—"
    if kind == "igmp_querier":
        return d.version or "—"
    if kind == "igmp_snooper":
        if d.vlans:
            return f"vlans={','.join(str(v) for v in d.vlans)}"
        return d.interface or "—"
    if kind == "local_system":
        bits = []
        if d.cpu_governor:
            bits.append(d.cpu_governor)
        if d.sdrs:
            bits.append(f"{len(d.sdrs)} sdr(s)")
        return " ".join(bits) or "—"
    return "—"
