# Ionospheric Scintillation Monitoring — Design Note & Implementation Plan

Status: **draft for review** — 2026-05-17
Scope: cross-client (`codar-sounder` + `hf-timestd`)
Audience: sigmond maintainers; HamSCI science review (detrend / window conventions)

---

## 1. Purpose

Add ionospheric **scintillation** observables — the S4 amplitude index, the
σ_φ phase index, and Doppler spectral width — to the sigmond HF client
suite, as a derived science product on signals the suite *already
receives*. No new RF, no new antennas, no new client.

Two existing clients become scintillation instruments:

- **codar-sounder** — fluctuations of dechirped CODAR returns (oblique
  chirp-sounder paths, 4–25 MHz).
- **hf-timestd** — fluctuations of WWV / WWVH / CHU time-standard carriers
  (continuous-wave paths).

The two are **complementary, not alternatives** (see §3). The end goal is a
multi-path, multi-station scintillation network: many propagation paths per
station, many stations in scattered locations, pooled into one dataset that
characterizes the spatial and temporal structure of ionospheric
irregularities.

## 2. Science rationale

Scintillation is the rapid fluctuation of a radio signal's amplitude and
phase caused by small-scale electron-density irregularities in the
ionosphere. The two standard indices (ITU-R P.531):

- **S4** — amplitude scintillation index: the normalized standard deviation
  of received signal intensity.
- **σ_φ** — phase scintillation index: the standard deviation of detrended
  carrier phase.

Kaeppler et al. (2022, *AMT* 15:4531–4545), the basis for codar-sounder,
used CODAR chirps for oblique *sounding* — group range, virtual height,
electron density — and **discarded the sweep-to-sweep fluctuation**. That
fluctuation *is* the scintillation. The same is true of hf-timestd's
carrier: it tracks phase for *timing* and collapses the rest. Extending the
science scope therefore needs no new data — only the retention and
characterization of what is already in the pipeline.

**Why multiple paths.** One path samples the ionosphere at one
pierce-point. Irregularities are spatially structured (patches, blobs,
gradients, drift). Many paths from one station → many pierce-points → local
structure. Many stations → a pierce-point mesh → regional structure,
irregularity drift velocity, and anisotropy. The science value scales with
**path diversity**, which is exactly why both instruments are wanted:

- CODAR transmitters are coastal, fixed, and numerous, giving oblique paths
  over a spread of azimuths and ranges — *spatial breadth*, plus
  range/mode resolution.
- WWV/WWVH/CHU are a few fixed long paths to Colorado, Hawaii, and Canada —
  *different geometry*, and a higher fluctuation sample rate (§3).

Their union is a richer path set than either alone.

## 3. The two instruments

| | **codar-sounder** | **hf-timestd** |
|---|---|---|
| Signal | CODAR FMCW chirps, 4–25 MHz, opportunistic | WWV/WWVH/CHU continuous carriers |
| Fluctuation series | per-sweep complex amplitude at a range bin | decimated complex carrier IQ |
| Native cadence | sweep repetition: **1 Hz** (0.5 Hz at 25/40 MHz) | GRAPE decimated IQ: **10 Hz** |
| Doppler span captured | ±0.5 Hz (1 Hz SRF) | ±5 Hz |
| Per-mode resolution | **yes** — range-resolved multi-peak trace | partial — metrology resolves arrivals; carrier currently aggregate |
| Path geometry | many oblique coastal paths, varied azimuth/range | few fixed long paths (CO / HI / Canada) |
| S4 sample population (60 s) | 60 samples (≈9 % S4 std error) | 600 samples (≈3 %) |
| Spatial coverage | broad (CODAR network) | narrow (3 source regions) |

Net: codar-sounder gives **spatial breadth + range/mode resolution at
modest cadence**; hf-timestd gives **fine temporal cadence and wider
Doppler span on a few long paths**. The disturbed-ionosphere Doppler
spread that exceeds codar's ±0.5 Hz window is exactly what hf-timestd's
±5 Hz captures cleanly — another reason to run both.

## 4. Common observable definitions — the shared contract

Both clients **must compute the indices identically**, or cross-path,
cross-station, cross-instrument comparison is invalid. Definitions are
fixed here once; both implementations import this section.

Let `A(t)` be the complex amplitude of a single **resolved** path/mode over
a UTC-aligned analysis window.

- **Intensity** `I = |A|²`.
- **S4** `= sqrt( ⟨I²⟩ / ⟨I⟩² − 1 )` over the window, with `I` first
  normalized by its slow trend (running mean) so that geometry- and
  gain-driven drift does not inflate the index. S4 is ratio-normalized →
  **no absolute amplitude calibration required**, only gain stability over
  the window (the IQ preset must have AGC off).
- **σ_φ** `= std( φ_hp )` in radians, where `φ_hp` is the carrier phase
  after high-pass detrending (below).
- **Doppler spectral width** — from the power spectrum of `A` over the
  window: report the **RMS (2nd-moment) width** in Hz. (3 dB width is the
  alternative; see §8.)
- **Analysis window** — **60 s**, UTC-minute aligned (GNSS convention).
- **Detrending** — a high-pass filter with a **documented corner
  frequency** (proposed default **0.1 Hz**), applied identically in both
  clients, separating "scintillation" from background TEC/geometry drift.
  *Today both fragments in hf-timestd use only a degree-1 polynomial fit;
  that is a crude high-pass and is not adequate as the shared standard —
  see §8.*
- **Quality gating** — each record carries `snr_db`, `valid_fraction`
  (fraction of the window with usable samples), and `n_samples`. Windows
  below an SNR floor or valid-fraction floor are flagged, not dropped.

Phase coherence for σ_φ is provided by the fleet's GPSDO-disciplined
radiod and hf-timestd's timing authority — the suite is already
phase-disciplined to a degree most scintillation setups are not.

## 5. Data model — path-centric, network-ready

Each scintillation record is one tuple:

```
(station, instrument, tx_id, frequency, mode/layer, UTC-minute)
  → s4, sigma_phi_rad, doppler_spread_hz, snr_db, valid_fraction,
    n_samples, window_seconds, detrend_corner_hz, processing_version
```

- **Path identity** = TX identity + RX identity. codar: CODAR `station_id`
  + host/radiod. hf-timestd: time-station + carrier frequency + host/radiod.
- **Path geometry for aggregation** — TX and RX coordinates give the
  great-circle midpoint, path length, and azimuth; combined with the
  virtual height (codar-sounder already inverts this) they give the
  **ionospheric pierce point**, which is the coordinate scintillation
  should ultimately be mapped onto.
- **Common schema** — both clients emit the same field set so a downstream
  consumer can pool every station's every path. Records flow through the
  existing `hs-uploader` → PSWS path and the local SQLite sink unchanged;
  the network of scattered stations becomes a pierce-point mesh purely
  because the definitions and schema are shared.

## 6. Implementation plan — codar-sounder

Investigation refs are `file:line` in `codar-sounder/src/codar_sounder/`.

**What's there.** `dechirp()` builds `range_spectrum` — the
`[sweep_index, range_bin]` complex stack — at `core/dechirp.py:185`, then
immediately collapses it with the slow-time FFT. `range_spectrum` is a
**local variable, never returned**: the per-sweep complex amplitude/phase
needed for S4 and σ_φ is computed and discarded. `range_doppler`
(`dechirp.py:188`) — the per-bin Doppler spectrum — *is* returned in
`DechirpResult`, but `range_profile()` sums `|range_doppler|` over Doppler
before any consumer sees it. `TraceDetection` (`core/trace.py:58`) carries
only `group_range_km / snr_db / power / bin_index`.

**Favourable alignment.** CPI default is **60 s** (`coherent_seconds`,
`config/...template:105`) and sweep repetition is **1 Hz** → one CPI is
already exactly one 60 s / 60-sample scintillation window. No multi-CPI
accumulator is needed. (25/40 MHz stations sweep at 0.5 Hz → 30 samples;
note the larger S4 uncertainty.)

Steps:

1. **Surface the complex stack.** Add `range_spectrum`
   (`[sweep, range_bin]` complex) to `DechirpResult` (`dechirp.py:45`).
   It already exists at line 185; stop discarding it.
2. **New module `core/scintillation.py`.** Input: the per-sweep complex
   series `range_spectrum[:, bin_index]` for a detected peak. Output:
   `s4`, `sigma_phi_rad`, `doppler_spread_hz`, `valid_fraction`,
   `n_samples`, per the §4 definitions. Doppler width needs **no new FFT** —
   `range_doppler[:, bin_index]` is already that spectrum; compute the RMS
   width over `doppler_axis_hz`. Phase detrend: remove the linear ramp at
   the peak Doppler bin, then high-pass per §4.
3. **Thread the bin through.** `find_f_region_peaks()` already yields
   `bin_index` per `TraceDetection` — the pipeline can index
   `range_spectrum` with it directly; no trace-record change required.
4. **Compute per peak.** In `_TransmitterPipeline.process_cpi()`
   (`core/daemon.py:154`), after `find_f_region_peaks()`, call the new
   module for each peak. Per-peak ⇒ **per-mode for free** — peaks are
   already range-resolved by layer.
5. **Schema.** Add `s4`, `sigma_phi_rad`, `doppler_spread_hz`,
   `scint_valid_fraction`, `scint_n_samples` to: the JSONL record in
   `core/output.py:106`; the `codar.spots` DDL
   (`schema/codar/001_create_spots.sql`); and `_ch_row_for()`
   (`daemon.py:262`).
6. **Tests** — synthetic chirp with injected amplitude/phase modulation of
   known S4/σ_φ; assert recovery within tolerance.

**Correctness items to verify during implementation:**

- The dechirp replica is applied identically to every sweep
  (`rx_matrix * conj(replica)`), so the per-sweep phase is coherent by
  construction — σ_φ is meaningful. Confirm the TDMA `phase_offset_samples`
  path preserves this.
- Two unresolved modes in the *same* range bin contaminate the index;
  the index inherits whatever resolution `find_f_region_peaks` achieves.
- 60 (or 30) samples is a modest population — record `n_samples` and
  document the ≈9 % (≈13 %) S4 standard error.

Effort: **moderate-small** — one new module, one field surfaced, schema
additions. The hard machinery (IQ ingest, dechirp, per-mode trace,
disciplined timebase) is all built.

## 7. Implementation plan — hf-timestd

Investigation refs are `file:line` in `hf-timestd/`.

**What's there — this half is consolidation, not greenfield.** Three
scintillation code paths already exist:

- `core/advanced_signal_analysis.py:871` — `calculate_scintillation_indices()`
  computes S4 and σ_φ correctly (ITU-R P.531, `ScintillationResult`
  dataclass). **Dead code: only the test suite calls it.**
- `core/wwv_test_signal.py:1516` — per-frequency S4 from the WWV/WWVH
  audio test-signal tones. **Wired** into the `test_signal` product — but
  fires only twice per hour, WWV/WWVH only.
- `web-api/services/scintillation_service.py` — σ_φ and an SNR-proxy
  "tick_s4", computed **at read time** from the `tick_phase` HDF5, **never
  persisted**.

There is **no scintillation data product** and **no Doppler-width
computation** (only PNG spectrograms from `grape/spectrogram.py`).

**Best signal source.** The GRAPE **10 Hz decimated complex carrier IQ**
(`DecimatedBuffer`, `grape/decimated_buffer.py` — 600 samples/min/channel,
retained on disk) is continuous and carries true amplitude *and* phase.
Today only the spectrogram and the uploader read it; no scintillation code
touches it. It is the right input for a proper S4 + σ_φ + Doppler-width
product.

Steps:

1. **Promote the dead module.** Reuse `calculate_scintillation_indices()`
   from `advanced_signal_analysis.py:871`; adapt its input to the GRAPE
   600-sample complex series. Upgrade its detrend from the degree-1 polyfit
   to the §4 high-pass.
2. **Doppler width — new.** FFT the 600-sample complex window; compute the
   RMS Doppler width per §4. `grape/spectrogram.py`'s STFT is a code
   reference, but it emits PNGs — a numeric width routine is needed.
3. **New product `l2_scintillation_v1.json`.** Add the schema under
   `src/hf_timestd/schemas/`, register in `schemas/registry.json`, add a
   writer following the existing product pattern in
   `core/metrology_service.py` (HDF5 + the in-progress SQLite path).
4. **Per-channel computation.** Per channel per UTC minute, read the GRAPE
   window, compute the indices, write the product.
5. **Reconcile the three paths.** The new GRAPE-based product is the
   canonical continuous one. The test-signal S4 (`wwv_test_signal.py`)
   stays as an *independent cross-check* — it is a different measurement
   (audio tones, twice/hour). The web-api `scintillation_service.py`
   switches from recomputing σ_φ to **reading the new product**.
6. **Tests** — synthetic carrier with injected modulation of known
   S4/σ_φ; assert recovery.

**Correctness items:**

- **SHARED channels.** `SHARED_2500/5000/10000/15000` carry WWV + WWVH +
  BPM on one channel; three sources beating together mimic amplitude
  scintillation but are multi-station interference. **Phase 1: restrict
  clean single-source S4 to WWV-only frequencies (`WWV_20000`,
  `WWV_25000`) and CHU**; treat SHARED channels only after station
  discrimination is applied (`wwvh_discrimination.py` exists).
- Phase 1 computes **per-channel (aggregate-carrier)** scintillation,
  matching GNSS whole-signal practice. **Per-mode** scintillation (using
  the `all_arrivals` / `propagation_mode_solver` resolution) is a Phase 3
  extension.
- 10 Hz IQ → 5 Hz Nyquist, comfortable for sub-Hz HF fading.

Effort: **moderate** — the S4/σ_φ math exists; the work is wiring it to
GRAPE, the new product schema, the Doppler-width routine, the detrend
upgrade, and SHARED-channel handling.

## 8. Science choices flagged for HamSCI / Kaeppler review

These encode into the schema, so settle them with a domain scientist
**before** locking it:

1. **Detrend high-pass corner** — proposed 0.1 Hz. The current hf-timestd
   linear-polyfit detrend is not a defensible shared standard.
2. **Analysis window** — proposed 60 s, UTC-aligned.
3. **Doppler spectral width definition** — RMS (2nd-moment) vs 3 dB width.
4. **Per-mode vs aggregate** — Phase 1 is aggregate for hf-timestd,
   per-mode for codar-sounder (free there); confirm this is acceptable.
5. **Quality-gating thresholds** — SNR floor, valid-fraction floor.

## 9. Sequencing

- **Phase 0** — agree §4 definitions (needs scientist input).
- **Phase 1** — codar-sounder S4/σ_φ/Doppler-width (cleanest: data is
  already 60 s / 1 Hz aligned, per-mode free).
- **Phase 1′** — hf-timestd: promote the dead module, wire to the GRAPE
  buffer, add the product. Runs in parallel (different repo).
- **Phase 2** — multi-station aggregation: pierce-point geometry, the
  PSWS upload schema, cross-station pooling.
- **Phase 3 (optional)** — per-mode hf-timestd; polarization scintillation
  once codar-sounder's deferred crossed-dipole AOA antenna lands (this
  ties to the STM's "polarized signals" clause).

Once this note is approved, the per-repo Phase-1 steps move into
`codar-sounder/tasks/todo.md` and `hf-timestd/tasks/todo.md`.

## 10. Non-goals

- **No standalone scintillation client.** Investigation confirmed both
  host clients already carry the signal and most of the machinery; a
  separate client would re-derive carrier tracking hf-timestd already has.
  The leverage is in the two feature-adds.
- No real-time alerting; no GNSS L-band scintillation (out of band).
