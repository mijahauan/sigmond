"""Receiver channels screen — per-client view of live radiod channels.

For a chosen ``<client>@<reporter_id>`` instance, this screen shows
the radiod the client is consuming from and the receiver channels
(unique SSRCs) currently active for that client's configured
frequencies.  The point: "what is wspr-recorder@AC0G-B1 actually
processing right now, and are all expected channels up?"

Filtering strategy:
  * Read the client's per-instance config (or the singleton config a
    client like hf-timestd declares) to extract the radiod status
    address and the set of configured frequencies.
  * Run ka9q-python's ``discover_channels(status, ...)`` to fetch
    every live channel on that radiod.
  * Match by frequency: each client uses a distinct frequency set
    (WSPR sub-bands vs FT8 sub-bands vs HFDL bands vs CODAR
    sub-bands), so freq alone disambiguates which group belongs to
    the selected client.  Per-channel multicast destination is
    shown so the operator can also see the per-client RTP grouping.

This is purely read-only; the screen never mutates radiod state.

Per-client config parsing lives in each client repo, not here:
``<client>/<parser_file>`` is loaded at TUI time via
``importlib.util.spec_from_file_location``, with the path + callable
name declared in the client's
``[client_features.receiver_channels]`` deploy.toml block.  Adding
a new client to this screen is zero sigmond edits.  See
``sigmond/lib/sigmond/client_features.py`` for the schema and
``sigmond/docs/ADD-A-CLIENT.md`` for the operator-side checklist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Select, Static
from textual.worker import Worker, WorkerState

from ...client_features import (
    ReceiverChannelsFeature,
    load_receiver_channels_features,
    load_receiver_channels_parser,
)
from ...instance import display_reporter_id as _display_reporter_id
from ...instance import list_instances
from ...ka9q_encoding import decode_encoding as _decode_encoding


def _read_toml(path: Path) -> dict:
    import tomllib
    with open(path, "rb") as f:
        return tomllib.load(f)


# Module-level cache so the worker thread, which can't easily reach
# the screen's instance attributes safely, can rebuild a feature lookup
# without re-walking the catalog on every refresh.
_FEATURES_BY_CLIENT: dict[str, ReceiverChannelsFeature] = {}


def _refresh_feature_cache() -> list[ReceiverChannelsFeature]:
    """Reload the receiver-channels features and rebuild the by-client
    map.  Called once per TUI session (on screen construction) and on
    explicit refresh; cheap (< 10 ms) but not free, so don't call from
    the worker hot path."""
    features = load_receiver_channels_features()
    _FEATURES_BY_CLIENT.clear()
    for f in features:
        _FEATURES_BY_CLIENT[f.client] = f
    return features


def _client_config_path(feature: ReceiverChannelsFeature,
                        instance: str) -> Optional[Path]:
    """Return the config path for one (client, instance) selection.

    Singleton features use their declared ``config_path``.
    Per-instance features resolve ``/etc/<client>/<instance>.toml``
    and require it to exist."""
    if not feature.per_instance:
        p = Path(feature.config_path)
        return p if p.exists() else None
    per_instance = Path(f"/etc/{feature.client}/{instance}.toml")
    return per_instance if per_instance.exists() else None


def _instance_options(
    features: list[ReceiverChannelsFeature],
) -> list[tuple[str, str]]:
    """Build (label, value) pairs for the client@instance Select.

    Values are encoded as ``<client>|<reporter_id>`` (or just
    ``<client>|`` for singletons) so the screen can split them on
    dispatch.  Drop-in: the list is built entirely from features
    discovered in deploy.toml — nothing in this module mentions
    client names by hand."""
    options: list[tuple[str, str]] = []
    for feature in features:
        if feature.per_instance:
            try:
                for inst in list_instances(catalog_clients=[feature.client]):
                    label = (
                        f"{feature.client}@"
                        f"{_display_reporter_id(inst.reporter_id)}"
                    )
                    value = f"{feature.client}|{inst.reporter_id}"
                    options.append((label, value))
            except Exception:
                continue
        else:
            if Path(feature.config_path).exists():
                label = feature.client
                if feature.singleton_label:
                    label = f"{feature.client} {feature.singleton_label}"
                options.append((label, f"{feature.client}|"))
    return options


class ReceiverChannelsScreen(Vertical):
    """Per-client live view of radiod source + receiver channels."""

    DEFAULT_CSS = """
    ReceiverChannelsScreen {
        padding: 1;
    }
    ReceiverChannelsScreen .rc-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }
    ReceiverChannelsScreen #rc-controls {
        height: 3;
        margin-top: 1;
    }
    ReceiverChannelsScreen #rc-controls Select {
        width: 50;
    }
    ReceiverChannelsScreen #rc-summary {
        margin-top: 1;
        color: $text-muted;
    }
    ReceiverChannelsScreen #rc-status {
        color: $text-muted;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._features = _refresh_feature_cache()
        self._options = _instance_options(self._features)

    def compose(self):
        yield Static("Receiver channels — per-client view of live radiod state",
                     classes="rc-title")
        yield Static(
            "stage: client per-instance config + live radiod channel "
            "discovery (read-only).  Shows the radiod the selected "
            "client is consuming from and every channel (SSRC) the "
            "client's configured frequencies are mapped to.",
            classes="rc-body")
        with Horizontal(id="rc-controls"):
            opts = self._options or [("(no instances configured)", "")]
            yield Select(
                opts, value=opts[0][1], id="rc-instance",
                allow_blank=False,
            )
            yield Button("Refresh", id="rc-refresh", variant="default")

        yield Static("", id="rc-summary")
        table = DataTable(id="rc-channels", zebra_stripes=True)
        table.add_columns(
            "SSRC", "Freq (MHz)", "Preset", "Rate", "Encoding",
            "SNR (dB)", "Multicast dest",
        )
        yield table
        yield Static("idle — select a client to populate", id="rc-status")

    def on_mount(self) -> None:
        if self._options:
            self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rc-refresh":
            self._refresh()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "rc-instance":
            self._refresh()

    def _refresh(self) -> None:
        sel = self.query_one("#rc-instance", Select).value
        if not sel:
            self.query_one("#rc-status", Static).update(
                "[yellow]No client instance configured on this host.[/]")
            return
        self.query_one("#rc-status", Static).update(
            "[dim]Querying radiod (≤ 10 s)…[/]")
        self.run_worker(
            lambda: self._fetch(str(sel)),
            thread=True, group="rc", exclusive=True,
        )

    @staticmethod
    def _fetch(sel: str) -> dict:
        """Worker thread: read config, discover channels, filter, return."""
        result: dict = {"sel": sel}
        try:
            client, _, reporter = sel.partition("|")
            result["client"] = client
            result["reporter"] = reporter

            feature = _FEATURES_BY_CLIENT.get(client)
            if feature is None:
                result["error"] = (
                    f"no [client_features.receiver_channels] block "
                    f"declared in {client}/deploy.toml"
                )
                return result

            cfg_path = _client_config_path(feature, reporter)
            if cfg_path is None:
                if feature.per_instance:
                    where = f"/etc/{client}/{reporter}.toml"
                else:
                    where = feature.config_path
                result["error"] = f"config missing: {where}"
                return result
            result["config_path"] = str(cfg_path)
            cfg = _read_toml(cfg_path)

            parser = load_receiver_channels_parser(feature)
            if parser is None:
                result["error"] = (
                    f"could not load parser "
                    f"{feature.parser_file}:{feature.parser_attr} from "
                    f"{client} (check `smd admin diag drop-in {client}`)"
                )
                return result
            try:
                status_dns, configured_freqs, configured_encoding = parser(cfg)
            except Exception as exc:                     # noqa: BLE001
                result["error"] = (
                    f"client parser raised "
                    f"{exc.__class__.__name__}: {exc}"
                )
                return result
            # Normalize: parser may legitimately return any iterable
            # of ints for freqs; coerce so downstream code can rely on
            # a set[int].
            configured_freqs = {int(f) for f in (configured_freqs or [])}
            result["status_dns"] = status_dns
            result["configured_freqs"] = sorted(configured_freqs)
            result["configured_encoding"] = configured_encoding
            if not status_dns:
                result["error"] = (
                    "no radiod status address in config (look for "
                    "[radiod] status / [[radiod]] status / [ka9q] "
                    "status)"
                )
                return result

            try:
                from ka9q import discover_channels  # type: ignore
            except ImportError:
                result["error"] = "ka9q-python not installed"
                return result

            try:
                channels = discover_channels(
                    status_dns, listen_duration=10.0,
                )
            except Exception as exc:
                result["error"] = f"discover_channels: {exc}"
                return result

            # Filter live channels to ones the client actually owns.
            # Match by frequency AND encoding when the client config
            # declares an encoding — radiod / ka9q-python derives the
            # SSRC from (freq, preset, rate, encoding, client_id), so
            # a former config that used a different encoding leaves
            # stale channels at the same frequency.  Those zombies
            # share our multicast destination but aren't what the
            # client currently consumes; they only age out when their
            # LIFETIME tag expires (or the operator clears them
            # manually).  Encoding-aware filtering hides them.
            rows: list[dict] = []
            stale_at_freq = 0
            for ssrc, ch in channels.items():
                try:
                    freq_hz = int(round(float(ch.frequency)))
                except (TypeError, ValueError):
                    continue
                if configured_freqs and freq_hz not in configured_freqs:
                    continue
                ch_enc = getattr(ch, "encoding", None)
                if (configured_encoding is not None
                        and ch_enc is not None
                        and int(ch_enc) != configured_encoding):
                    stale_at_freq += 1
                    continue
                rows.append({
                    "ssrc": int(ssrc),
                    "frequency_hz": freq_hz,
                    "preset": getattr(ch, "preset", "?"),
                    "sample_rate": int(getattr(ch, "sample_rate", 0) or 0),
                    "encoding": ch_enc,
                    "snr": getattr(ch, "snr", None),
                    "multicast_address": getattr(ch, "multicast_address", ""),
                    "port": getattr(ch, "port", 0),
                })
            result["stale_at_freq"] = stale_at_freq

            rows.sort(key=lambda r: (r["frequency_hz"], r["ssrc"]))
            result["rows"] = rows
            result["total_channels"] = len(channels)
            return result
        except Exception as exc:
            result["error"] = f"unexpected: {exc}"
            return result

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS:
            return
        if event.worker.group != "rc":
            return
        data = event.worker.result or {}
        if not isinstance(data, dict):
            return

        status_widget = self.query_one("#rc-status", Static)
        summary = self.query_one("#rc-summary", Static)
        table = self.query_one("#rc-channels", DataTable)
        table.clear()

        if "error" in data:
            status_widget.update(f"[red]{data['error']}[/]")
            summary.update("")
            return

        configured_n = len(data.get("configured_freqs") or [])
        rows = data.get("rows") or []
        # Group by multicast destination to surface per-client RTP
        # grouping (helps the operator confirm channels really do
        # belong to this client and aren't another peer on the same
        # frequency).
        mcast_groups: dict[tuple, int] = {}
        for r in rows:
            key = (r["multicast_address"], r["port"])
            mcast_groups[key] = mcast_groups.get(key, 0) + 1

        enc_int = data.get("configured_encoding")
        enc_str = (_decode_encoding(enc_int) if enc_int is not None
                   else "any")
        stale = data.get("stale_at_freq", 0)
        stale_note = (f"  •  [yellow]{stale} stale channel(s) at matching "
                      f"freq with wrong encoding (zombies awaiting LIFETIME "
                      f"expiry)[/]") if stale else ""
        summary.update(
            f"radiod = [bold]{data.get('status_dns', '?')}[/]  •  "
            f"encoding = {enc_str}  •  "
            f"config = [dim]{data.get('config_path', '?')}[/]\n"
            f"{len(rows)} matching channel(s) "
            f"({configured_n} configured / "
            f"{data.get('total_channels', 0)} live on radiod)  "
            f"across {len(mcast_groups)} multicast destination(s)"
            f"{stale_note}"
        )

        for r in rows:
            ssrc = r["ssrc"]
            freq_mhz = f"{r['frequency_hz'] / 1_000_000:.6f}"
            preset = str(r["preset"])
            rate = f"{r['sample_rate']:,}"
            enc = _decode_encoding(r["encoding"])
            snr = r["snr"]
            if snr is None or snr == float("-inf"):
                snr_str = "—"
            else:
                try:
                    snr_str = f"{float(snr):+.1f}"
                except (TypeError, ValueError):
                    snr_str = "?"
            mcast = (
                f"{r['multicast_address']}:{r['port']}"
                if r["multicast_address"] else "—"
            )
            table.add_row(str(ssrc), freq_mhz, preset, rate, enc,
                          snr_str, mcast)

        if not rows:
            status_widget.update(
                "[yellow]no live channels match this client's configured "
                "frequencies — is the daemon running?[/]"
            )
        else:
            status_widget.update("")
