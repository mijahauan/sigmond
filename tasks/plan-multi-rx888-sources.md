# Multi-RX888 (and multi-radiod, multi-KiwiSDR) source selection

Started 2026-05-19.  Each client (wspr-recorder, psk-recorder, future
HFDL/CODAR/etc.) is currently bound to exactly one radiod instance.
The goal is to let a host be served by **zero-or-more local** and
**zero-or-more remote** SDR-bearing radiod (and KiwiSDR) sources, and
to let the operator choose **per-client** which of those sources each
client decodes from.

Working example asymmetric topology:

    radiod@local-rx888-A      ──┐                    ┌── wspr-recorder@A+B  (decode both)
    radiod@local-rx888-B      ──┤                    │
    radiod@remote-bee1        ──┼─ source pool ──────┼── psk-recorder@A    (decode local A only)
    kiwisdr@grape-corner-1    ──┘                    └── ka9q-web@A+B+remote

When multiple wspr-recorders produce spots for the same WSPR cycle on
the same band, the uploader must deduplicate **for wsprnet only**,
keeping the highest-SNR spot per (cycle, call, freq).  Wsprdaemon.org
still receives every receiver's spots (the diversity tier consumes
them).

---

## Existing infrastructure we'll reuse

- `lib/sigmond/discovery/`
  - `usb_sdr.py` — local USB enumeration (lsusb-based)
  - `http_kiwisdr.py` — port-8073 + /status.json probe
  - `http_ka9q.py` — HTTP probe to ka9q-web instances (one per peer host)
  - `mdns.py` — service discovery via avahi-browse
  - `local_resources.py` — running services on this host
- `lib/sigmond/tui/screens/sdr_inventory.py` — unified SDR list, operator label edits
- `/var/lib/sigmond/sdr-labels.toml` — label store with friendly names + grid/call
- `lib/sigmond/sdr_labels.py` — read/write helpers

What's *missing* from discovery:
- Direct radiod control-plane discovery (multicast status, not just HTTP/ka9q-web).
  A radiod can be running without ka9q-web.

---

## Design rules

1. **Discovery yields candidates; topology selects.** Discovery just
   enumerates what's reachable.  An operator-controlled selection
   layer (per-client) picks which candidates actually feed each
   client.
2. **Per-client selection, not global.** psk-recorder may pick the
   local RX888 only; wspr-recorder may pick both local and remote.
   Stored in `/etc/sigmond/clients/<client>.sources.toml` (or a
   single `clients-sources.toml` keyed by client).
3. **Stable IDs.** Sources are referenced by a stable key
   (`usb:vid:pid:serial`, `radiod:<status_address>`,
   `kiwisdr:<ip>:<port>`).  IP-only references break when DHCP
   reshuffles; serial+label survive.
4. **Headless first.** Selection must work via `smd sources …` CLI;
   TUI is additive.  The CLI is the source of truth.
5. **Clients are dumb consumers of their selection.** No client
   re-runs discovery; sigmond renders the selection to whatever
   the client expects in its env/config.

---

## Plan (one PR per phase, smallest viable slice)

### Phase 0 — alignment (this doc) — DONE when user approves

- [x] Capture the requirement set
- [x] Inventory existing infrastructure
- [ ] User reviews this doc, confirms approach, names the precise
      scope of phase 1

### Phase 1 — radiod control-plane discovery

Sigmond currently knows radiod via ka9q-web HTTP probes only.  Add a
multicast-control probe so a bare radiod (no ka9q-web) shows up in
the SDR inventory.

- [ ] `lib/sigmond/discovery/radiod_control.py` — probe a configured
      list of multicast status addresses, parse the metadata reply,
      emit `Observation(source="radiod", kind="sdr_source", ...)`
- [ ] Extend `sdr_inventory.py` with the new source type
- [ ] mDNS hook — radiod advertises `_radiod-status._udp` (or
      similar); auto-populate the probe list
- [ ] Tests against a recorded radiod metadata reply (no live deps)

### Phase 2 — source selection model (CLI only)

- [ ] `lib/sigmond/sources.py` — data model:
        `SourceSelection(client="wspr-recorder", selected=[<keys>])`
- [ ] On-disk: `/etc/sigmond/clients/<client>.sources.toml`
- [ ] `smd sources list` — show every client and which sources it
      consumes from
- [ ] `smd sources add <client> <source-key>`
- [ ] `smd sources remove <client> <source-key>`
- [ ] `smd sources apply` — render to each client's env/config and
      restart if changed
- [ ] Bash completion: dynamic source-key completion from inventory
- [ ] Tests: round-trip selection, apply diff detection

### Phase 3 — multi-radiod wspr-recorder

The single-tenant wspr-recorder must instantiate one ReceiverManager
per selected source, all writing to the same `sink.db` (so the
uploader sees one queue).  Each spot gets an `rx_source` tag so
downstream can disambiguate.

- [ ] `wspr_recorder.config` accepts `sources = [...]` list
- [ ] Multi-ReceiverManager wiring in `__main__.py`
- [ ] Spot payload schema: add `rx_source` field (already has
      `radiod_id` + `host_id`; new field is the operator-assigned
      label or fall back to `radiod_id`)
- [ ] Memory footprint review: per-source ring buffers (17 bands ×
      N sources × ~6 MB) — set MemoryHigh accordingly
- [ ] Tests: synthetic two-source ingest hits both ReceiverManagers
- [ ] B4-100 bring-up doc

### Phase 4 — multi-radiod psk-recorder

Same shape as Phase 3 but for psk-recorder.  Defer until Phase 3
proves the pattern; psk has tighter latency budgets (FT4 7.5s
cycles), so we want the wspr model validated first.

### Phase 5 — wsprnet dedup at the uploader

When two wspr-recorders see the same WSPR cycle (rx_source A, B)
they both write `pending_uploads` rows for the same (cycle, call,
freq).

- [ ] In `hs_uploader_shim` wsprnet path: group rows by
      `(cycle_iso, callsign, frequency_hz)`, keep the row with
      max(snr_db).
- [ ] Wsprdaemon.org tar path: ship **all** rows (diversity tier).
- [ ] Audit: extend `wsprnet_audit` to record which rx_source's
      spot was selected for upload, so `smd verifier report` can
      show per-source contribution.
- [ ] Tests: two-source synthetic fixture, verify uploaded payload
      has one row per (cycle, call, freq) with highest SNR.

### Phase 6 — TUI source-selection screen

After the CLI is solid, layer a Textual screen on top for the same
operations.  Reuse `sdr_inventory.py`'s rendering for the source
catalogue; add a "✓" toggle column per client.

---

## Open questions / pending decisions

1. **Sink topology** — DECIDED 2026-05-19: **one** shared
   `/var/lib/sigmond/sink.db`.  Every wspr-recorder ReceiverManager
   writes to it; hs-uploader drains.  Phase 3 must confirm SQLite
   write concurrency under multi-source load and add the `rx_source`
   tag to each spot row so the dedup query in Phase 5 can group by
   `(cycle_iso, callsign, frequency_hz)` and pick max(snr_db).
2. **Remote sink writes.**  When the selected radiod is on another
   host, does wspr-recorder run locally and pull RTP from the
   remote, or does the remote run its own recorder and we pull
   spots?  Today's `ka9q-radio` multicast model lets us pull RTP
   over the LAN, so the local-recorder-with-remote-RTP path is
   already viable.  But it costs bandwidth (12 kHz × 24 bit × 17
   bands × N receivers ≈ 100 Mbps for full WSPR coverage).
   Decision deferred to Phase 3 design.
3. **KiwiSDR as a source.**  KiwiSDRs don't speak ka9q-radio RTP;
   they have their own WebSocket protocol.  Treating a KiwiSDR as
   a wspr-recorder source needs a new SourceAdapter.  Probably
   defer until after Phase 5 — KiwiSDR coverage is currently
   handled by separate `kiwirecorder` infrastructure outside
   sigmond.

---

## Phase-1 scoping (proposed, awaiting user confirmation)

If we agree Phase 1 is the right starting point, the minimal
deliverable is:

- One new file `lib/sigmond/discovery/radiod_control.py`
- A handful of new rows in `sdr_inventory.py`
- An mDNS service entry for `_radiod-status._udp` (or a configured
  peer list as fallback if mDNS is unworkable in some networks)
- No client-facing changes yet — pure observability

That keeps the diff small enough to land cleanly and unblocks Phase
2's CLI from a richer inventory.
