# PSWS ⇄ Sigmond Traceability Mapping

**Purpose.** This document is the durable, sigmond-side map between the
HamSCI **PSWS Design Charette** board
([github.com/orgs/HamSCI/projects/6](https://github.com/orgs/HamSCI/projects/6))
and the sigmond implementation. It exists because the two efforts have
**different owners and different altitudes**: the board collects
community *intent* for the whole PSWS network (server/database/API +
analysis + coordination, largely UA-led); sigmond is one concrete
*implementation* of the **station edge** of that network. The gap
between them is normal — the job of this file is to make the boundary
explicit and every crossing traceable, not to merge the two lists.

This file is the **source of truth we control.** The board is influenced,
not owned, so links/comments there mirror this file rather than the
reverse. See the companion [PSWS-INTERFACE-BOUNDARY.md](PSWS-INTERFACE-BOUNDARY.md)
for the contract at the seam (the upload interface), which is where most
scope questions actually get decided.

---

## The scope rule

One rule sorts most items without discussion:

> **A feature is sigmond's if it requires the station** — local
> hardware / RF / timing / CPU access, or it must act *before* upload
> (acquisition, timing authority, local QA, lifecycle, shipping). It is
> **upstream (PSWS)** if it operates on *post-upload, aggregated* data,
> or is storage / API / presentation / cross-station. Items needing
> *both halves* are **shared**, and they split **at the upload
> interface** (see PSWS-INTERFACE-BOUNDARY.md).

Only genuinely shared items need a human decision, and that decision is
always "where does the line through the upload interface fall?"

## Tag legend

| Tag | Meaning |
|-----|---------|
| **Owned** | Sigmond implements this; it is (or should be) a sigmond repo/component. |
| **Shared** | Sigmond provides the station-side half; PSWS provides the rest. Split at the upload interface. |
| **Enables** | Sigmond does not implement the item, but is a data *source* the item consumes. |
| **Future** | Candidate sigmond scope; not built, not yet committed. |
| **Upstream** | Out of scope for sigmond — server / database / website / org. Listed so "the software covers it" is never silently assumed. |
| **Done** | Closed on the board; recorded for completeness. |

> Relationships are **many-to-many.** `hf-timestd` serves two board
> items; "Sensor Integration" spans four components. Do not force 1:1.

---

## Board → sigmond matrix

Grouped by the board's own status columns. Item numbers are the board's
display order (see the source board for canonical issue numbers).

### Instrumentation (sigmond's home column)

| # | Board item | Tag | Sigmond component(s) | Note |
|---|-----------|-----|----------------------|------|
| 1 | Centralized Remote Admin Access of PSWS Nodes (body = "HamSCI RAC Network Architecture") | **Owned** | `rac` (frpc reverse tunnel) | The board item is effectively a spec for what `rac` already does. Prime candidate to link first. |
| 2 | ka9q-python Integration | **Owned** | `ka9q-python` | Every RF client subscribes to radiod RTP through it; sigmond is the integrator. Item links the repo directly. |
| 3 | Time-Pulse Injected Precision Time-Stamping (TS-1 PPS-timecode into RX888 stream) | **Owned** | `hf-timestd` (timing authority), TS-1/GPSDO chain, `gpsdo-monitor` | Sigmond stations are GPSDO-disciplined, TS-1 time-injected; hf-timestd publishes the RTP↔UTC offset + tier. |
| 8 | Automated within-band check w/ feedback to operator (±5 Hz spectral clipping) | **Shared** | `smd admin validate` / `harmonize.py`; client `inventory`/`validate` | Sigmond owns the station-side check; the operator-feedback loop is the shared half. See #21. |
| 11 | 4-Channel Receiver (beam-forming / AoA) | **Enables / Future** | `superdarn-sounder` (multi-channel/Doppler roadmap), multi-radiod lifecycle | Hardware-led; sigmond would manage the multi-instance lifecycle. |
| 34 | RX888 Replacement | **Enables** | `ka9q-radio`/`radiod` target hardware | Hardware effort (WB6CXC). Software-transparent if ka9q-radio supports the successor. |
| 35 | WSPRDaemon cybersecurity | **Shared** | `wspr-recorder`, `hs-uploader`, sink, `rac` | Touches station upload + remote-access surface; org-led policy. |
| 36 | Station Calibration | **Shared** | `wspr-recorder` (signal+noise at SMA) | Station-side measurement; see #15. |

### PSWS Repository (UA) — server/database column

| # | Board item | Tag | Sigmond component(s) | Note |
|---|-----------|-----|----------------------|------|
| 4 | Migrate File Server Dev from Agile to GitHub | **Upstream** | — | Server dev process. |
| 5 | Migration from WW0WWV Server | **Enables** | `hs-uploader` (`PswsMagnetometerSftp`, Grape upload path) | Sigmond stations upload to the PSWS server, not WW0WWV; relevant as the producing side. |
| 16 | HAPI Interface to PSWS Database | **Upstream** | — | Server API. |
| 17 | Madrigal backend to PSWS database | **Upstream** | — | Server API. |
| 18 | Daily Multiplot by Email | **Upstream** | — | Server-side plotting. |
| 19 | Request Level-0 Data Product from Station Cache | **Future / Shared** | shared sink (`/var/lib/sigmond/sink.db`), `hs-uploader`, heartbeat | The "station cache" *is* the sigmond sink; on-demand Level-0 pull would extend hs-uploader + a heartbeat-response path. Not built. |
| 20 | Database query (network health) | **Enables** | `smd status`, contract `inventory --json` | Sigmond emits the per-station health/inventory the query aggregates. |
| 21 | Auto feedback to stations on data quality | **Shared** | contract `validate --json`, `smd status` | Closing-the-loop half of #8/#22. Server raises the flag; station presents it. |
| 22 | Automated QA metadata | **Shared** | client `inventory`/`validate`, sink | Station can emit QA metadata alongside data; server stores/standardizes it. |
| 23 | VLF Receiver Data Ingress | **Upstream** | — | Different instrument class (vlfrx-tools); not a sigmond client today. |
| 24 | Simplified data download | **Upstream** | — | Server/website. |
| 25 | Improved PSWS System Registration (upload keys, metadata) | **Shared** | `hs-uploader` (`PswsMagnetometerSftp`: station-id + portal SSH key), station `identity` | The station-side registration/keying handshake. The single most important *shared* item — it defines the interface. See PSWS-INTERFACE-BOUNDARY.md §Registration. |
| 26 | Map filtering (by instrument) | **Upstream** | — | Website. Sigmond's per-instrument metadata enables it (Enables, weakly). |
| 38 | Enhance watchdog to ingest Grape 2 fldigi files | **Upstream** | — | Grape watchdog, not a sigmond client. |
| 39 | Gantt-like Station Availability View | **Enables** | heartbeat / upload cadence | Sigmond's upload heartbeat is the availability signal the view would plot. |

### Analysis & Visualization

| # | Board item | Tag | Sigmond component(s) | Note |
|---|-----------|-----|----------------------|------|
| 6 | Feature Annotation | **Upstream** | — | Website/wiki. |
| 7 | Realtime Spectrogram + mag data view | **Enables** | `mag-recorder` (mag stream), `radiod` spectra, `ka9q-web` | Sigmond produces both data streams; rendering is upstream. |
| 28 | Python API for Doppler, level, frequency spread | **Enables / Future** | `hamsci-dsp`, `superdarn-sounder` (Doppler), `hf-timestd` (dTEC) | The DSP these computations wrap already lives in sigmond; exposing it as the network's Python API is a feed-upward candidate. |
| 29 | WWV/WWVH Disambiguation | **Owned** | `hf-timestd` (WWV/WWVH/CHU/BPM analyzer) | Precisely hf-timestd's analysis domain. |
| 30 | Sonification Integration | **Upstream** | — | Annotation/UX. |
| 31 | Website Accessibility | **Upstream** | — | Website. |
| 37 | Sensor Integration (spot + spectrum + magnetometer) | **Owned (concept) / Enables** | `psk-recorder`, `wspr-recorder`, `mag-recorder`, `hf-tec`, shared sink | Sigmond *is* a multi-sensor node fusing exactly these in one sink — the strongest conceptual match outside Instrumentation. |
| 40 | Nowcasting capability (flare detection via WSPR) | **Enables** | `wspr-recorder` spot/noise data | Sigmond is the data source; nowcasting logic is upstream/analysis. |

### Coordination

| # | Board item | Tag | Note |
|---|-----------|-----|------|
| 9 | Helpdesk/Ticketing System | **Upstream** | Org tooling. |
| 10 | HamSCI PII standard | **Upstream** | Policy. Sigmond must *comply* (grid/lat-lon precision in identity) but doesn't define it. |
| 12 | HamSCI Challenges (objectives org) | **Upstream** | Org/website. |
| 13 | PSWS in YO | **Upstream** | Outreach. |
| 14 | Pacific Measurements | **Enables** | A station running sigmond could monitor the named beacons; not a software feature. |
| 15 | Field strength estimation / Calibration Stations | **Shared** | `wspr-recorder` measures signal+noise at SMA; calibration standard is upstream. See #36. |
| 42 | Hardware Stock | **Upstream** | Logistics. |
| 43 | Grape 1 Education Kit | **Upstream** | Different instrument (TAPR through-hole kit). |

### Done (recorded for completeness)

| # | Board item | Tag | Note |
|---|-----------|-----|------|
| 27 | GNU Radio Integration | **Upstream / Done** | DigitalRF source block — server/tooling. |
| 32 | Test | **Done** | Test issue. |
| 33 | Automatic email alerts when station goes down | **Done / Shared** | Sigmond emits the health/heartbeat signal (`smd status`, `gpsdo-monitor`, `rac`); alert delivery is upstream. |
| 41 | Daily Plots by Email | **Done / Upstream** | Server-side. |

---

## Component → board reverse index

| Sigmond component | Serves board item(s) |
|-------------------|----------------------|
| `rac` | 1 (owned) |
| `ka9q-python` | 2 (owned) |
| `hf-timestd` | 3, 29 (owned); 28 (Doppler/dTEC) |
| `gpsdo-monitor` | 3, 33 |
| `wspr-recorder` | 15, 35, 36, 40 |
| `mag-recorder` | 5, 7, 37 |
| `psk-recorder` | 37; (Phase-D merge → 25 interface) |
| `hf-tec`, `codar-sounder`, `hfdl-recorder`, `meteor-scatter` | 37 (sensor breadth), 28 |
| `hamsci-dsp`, `superdarn-sounder` | 28, 11 |
| `hs-uploader` + shared sink | 5, 19, 25, 39 |
| `smd` core (validate / harmonize / status / contract) | 8, 20, 21, 22, 33 |
| `ka9q-radio` / `radiod` | underlies 2, 7, 11, 15, 34 |

---

## Gaps to feed *upward* (sigmond capabilities with no board item)

The charette explicitly invites feedback, so these are the items to
*propose* back onto the board — implementation informing planning. These
are capabilities sigmond has (or is building) that the network plan does
not yet name:

1. **Timing-authority tiering** — `hf-timestd` publishes an RTP↔UTC
   offset *and a tier* that every other RF client labels its data
   against. The network has no standard for propagating per-station
   timing provenance; this is a candidate PSWS metadata standard.
2. **Within-band / data-quality QA at the edge** — sigmond's
   harmonization + per-client `validate` already catch many artefacts
   *before* upload. Board items 8/21/22 describe the *server* half;
   the *station* half is unproposed. Worth pairing with them.
3. **Multi-sensor station fusion** — item 37 imagines combining sensor
   types; sigmond already co-registers spot + spectrum + magnetometer +
   coded-beacon TEC at one GPS-governed node. Propose the *station* as
   the unit of fusion, not just the database.
4. **Per-station capability/health inventory** — the contract's
   `inventory --json` is a ready-made feed for items 20 (network health
   query) and 39 (availability view). Propose it as the standard
   heartbeat payload.

> Infrastructure that deliberately has **no** board item (CPU pinning,
> lifecycle manager, catalog/topology, the sink itself): these are
> *means, not ends.* They stay off the board on purpose. Record them
> here as intentional, so their absence is never read as an oversight.

---

## Maintenance cadence

This file rides the existing **Monday PSWS meeting**, not a new process:

- New board item → tag it here under the scope rule.
- New sigmond capability → either propose it upward (add to "Gaps to
  feed upward") or mark it intentional infrastructure.
- Shared item → clarify *where it splits* in
  [PSWS-INTERFACE-BOUNDARY.md](PSWS-INTERFACE-BOUNDARY.md), not here.
- Mirror **Owned/Shared** rows onto the board as issue/PR links + a
  one-line status comment. Start with items 1 (`rac`) and 2
  (`ka9q-python`), which are already titled after the components.

Ownership asymmetry is deliberate: we fully control this file and the
interface doc; the board we influence. Keep the source of truth here.
