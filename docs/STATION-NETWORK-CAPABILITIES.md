# What a Network of Stations Can Deliver

**Scope:** This document describes the scientific capability that emerges when
many DASI2-class stations — each running the sigmond client suite — operate as a
coordinated mesh rather than as isolated receivers. Two axes of diversity
compound: **spatial** (many receivers, §2) and **modal** (several different
GPS-governed ionospheric instruments at each node, §3). It also explains why
that capability is **resilient to the loss of any single transmitter**,
including CHU (§4). `hf-timestd` is used as the worked example throughout, but it
is one instrument among several.

**Audience:** Station operators, HamSCI collaborators, and anyone weighing the
value of adding a node to the network.

**Status:** Forward-looking. Single-station capability is implemented and live
(see [`hf-timestd/docs/PHYSICS.md`](../../hf-timestd/docs/PHYSICS.md)); the
network capability described here is what those stations enable *collectively*.

---

## 1. The one-station picture, briefly

A single DASI2 station measures path-integrated ionospheric quantities along a
handful of fixed great-circle paths — from the receiver to each HF time
standard it can hear (WWV, WWVH, CHU, BPM). Its primary ionospheric science
product is **carrier-phase differential TEC (dTEC)**: the rate of change of
total electron content along each path, at ~6 mTECU/min sensitivity and
minute-level cadence, anchored to an absolute scale by a local dual-frequency
GNSS receiver (overhead VTEC, ±1 TECU).

This is genuinely useful — it sees travelling ionospheric disturbances (TIDs),
solar-flare TEC enhancements, the diurnal cycle, and storm-time dynamics. But a
single station has a hard, physics-imposed ceiling: **it cannot spatially
localize what it detects.** Each measurement is an integral along a line. One
vertex with a few azimuths cannot separate "a disturbance moving north at
200 m/s" from a dozen other geometries that would produce the same path-integral
time series. The single station is a *perturbation sensor*. It is not an imager.

(dTEC is also only *one* of the node's ionospheric observables — the same node
carries coded-beacon TEC, oblique sounding, opportunistic propagation sensing,
and a magnetometer. §3 returns to that; §2 first develops the spatial argument
using dTEC alone.)

## 2. What changes with many stations

A mesh of identical stations breaks the single-vertex limitation, because the
**reflection points (ionospheric pierce points) of many receivers tile a
two-dimensional region** instead of radiating from one point. The same modest
hardware, replicated and coordinated, becomes a distributed-aperture instrument.

### 2.1 TID imaging, not just detection

Today a single station infers TID velocity and azimuth by cross-correlating its
own paths (a 3-path time-difference-of-arrival solve). With *N* receivers all
watching the *same* transmitters, a disturbance's wavefront can be tracked as it
sweeps across the grid of pierce points. That yields true two-dimensional
propagation vectors, horizontal wavelength, and the dispersion of medium- and
large-scale TIDs — the kind of result that demands many baselines and long
baselines, both of which a citizen-science HF mesh supplies cheaply. The
cross-*path* correlation each station already computes becomes cross-*site*
correlation, with vastly better-conditioned geometry.

### 2.2 Common-illuminator differencing (the biggest multiplier)

Many receivers viewing the same WWV transmission share the **transmitter-side
phase and clock exactly**. Differencing two receivers' dTEC on a common
transmitter cancels the transmitter contribution and isolates the *difference in
ionospheric path* between the two pierce points — a direct gradient measurement,
with the transmitter oscillator removed for free. This is the single largest
scientific gain from going one-to-many: it converts a constellation of
absolute-scale-ambiguous single-path measurements into a set of clean,
differential gradient sensors.

### 2.3 Tomographic and assimilative TEC

Overlapping slant paths crossing the same airspace at many angles are precisely
the input an ionospheric tomography or data-assimilation scheme wants. Each
station's local GNSS VTEC provides an absolute anchor; the dense HF slant
network provides the structure *between* anchors. Where ground-based GNSS-TID
coverage is sparse — oceans, low station-density regions — an HF mesh using time
standards as illuminators can fill in.

### 2.4 Mesoscale climatology

Occurrence rates, preferred propagation directions, and the seasonal / diurnal /
solar-cycle dependence of MSTIDs over the network footprint become tractable
once there are many baselines observed over long periods. That is a
network-only product: no single station can build it.

### 2.5 Footprint-resolved space-weather response

A flare's sudden ionospheric disturbance, a storm's TEC enhancement or
depletion, the terminator's passage — all have *spatial structure*. A single
column over one receiver sees a scalar time series; the network sees the
structure move across its footprint, turning each event into a small movie
rather than a single light curve.

## 3. Each node is multi-instrument: modal diversity

The "station" in this document is not only an `hf-timestd` receiver. A DASI2
node runs the full sigmond client suite off **one GPS-disciplined `radiod`**, and
several of those clients are ionospheric instruments in their own right — each
contributing a *different* observable from the *same* precise timebase. Where §2
adds **spatial** diversity (many copies of one instrument), this adds **modal**
diversity (several instrument *types* per site). The two multiply: a network of
multi-instrument nodes samples the ionosphere across space *and* across
observables at once.

The shared GPS governance is the enabler. Every client timestamps against the
same RTP↔UTC authority `hf-timestd` publishes (the §18 timing-authority
contract), so observables from different instruments — and different sites — are
directly co-registrable in time. Without a common, precise timebase, fusing a
TEC sample with an oblique ionogram with a magnetometer trace would be guesswork;
with it, they are the same clock's view of one ionosphere.

- **`hf-tec` — coded-beacon absolute TEC.** PRN-coded HF beacons at known
  locations and frequencies, received against GPS time, yield absolute
  group-delay slant TEC and differential carrier-phase TEC — much as GNSS does,
  but at HF on dedicated beacon paths. This is the purpose-built **absolute-TEC**
  complement to `hf-timestd`'s dTEC: where dTEC gives rate and fine temporal
  structure but an ambiguous DC level, coded-beacon TEC supplies the absolute
  slant TEC on its own paths. Distributed, these become directly-measured
  absolute-TEC samples feeding the tomographic input of §2.3 — and a second,
  independent absolute anchor alongside GNSS-VTEC.

- **`codar-sounder` — oblique ionograms.** Opportunistic reception of CODAR
  coastal-radar FMCW chirps, GPS-timed, produces swept-frequency **oblique
  soundings**: a *measured dispersion curve and MUF* along each path — the one
  thing a fixed-frequency dTEC instrument cannot produce. A network of
  CODAR-sounding nodes turns scattered coastal CODAR transmitters into a
  distributed oblique-ionosonde array: MUF maps, foF2/hmF2 constraints, and
  ground-truth mode identification that **calibrate the climatological foF2 model
  the reanalysis currently assumes** (the fixed-9.0-MHz `foF2_noon` in
  `hf-timestd`'s reanalysis).

- **`hfdl-recorder` — dense opportunistic illuminators.** HFDL ground stations
  are tens of fixed, known-location HF transmitters worldwide across many
  frequencies (~2.6–21 MHz). GPS-timestamped reception of their bursts adds
  path-integral, SNR, and Doppler observables on **far more azimuths and
  frequencies than the four time standards** — a global illuminator grid that is
  not at risk of defunding. This is the single biggest densifier of illuminator
  geometry: it multiplies the TX–RX great-circle paths each node samples,
  sharpening the tomographic and TID-imaging geometry of §2 *without adding a
  single transmitter to maintain*. (Decoded aircraft positions even offer moving
  reflectors — a possible bonus modality.)

- **`mag-recorder` — the driver, not the response.** A magnetometer sees the
  *cause* of much ionospheric variability: geomagnetic storms and substorms, the
  auroral and equatorial electrojets, the Sq current system, and the `dB/dt` that
  launches large-scale TIDs through auroral Joule heating. A single magnetometer
  yields a local K-index — the natural covariate for every other product here. A
  distributed magnetometer array measures the *spatial structure and timing of
  the external current systems*, so correlating ground `dB` against **GNSS-VTEC**
  and HF dTEC **separates driver from response**: the magnetometers register the
  auroral onset, the TEC network watches the resulting LSTID propagate
  equatorward, and the cross-correlation yields source attribution and
  propagation speed neither can establish alone. Co-locating a magnetometer with
  a GNSS-VTEC anchor makes that correlation a *per-site* product, not only a
  network-scale one.

- **`wspr-recorder` / `psk-recorder` — the existing dense layer.** WSPR/FST4W and
  FT4/FT8 already have *enormous* distributed amateur coverage (wsprnet,
  wsprdaemon, PSKReporter), so the network effect for those modes is not
  hypothetical — it exists today. Their spots are a ready-made, globally dense
  propagation dataset (path, SNR, frequency, often Doppler/drift) the
  GPS-governed instruments above can be cross-referenced against. The sigmond
  node's contribution is to add the *precisely-timed, physics-grade* instruments
  (TEC, sounding, magnetometer) at the **same site** as a WSPR/PSK reporter —
  upgrading a propagation-spot location into a calibrated ionospheric
  observatory.

Put together, a node is a small multi-instrument observatory and the network is a
large one — co-registered in time by shared GPS governance, spanning **driver**
(magnetometer) → **absolute TEC** (`hf-tec`, GNSS-VTEC) → **dispersion / MUF**
(`codar-sounder`) → **dense path-integral sampling** (`hf-timestd`,
`hfdl-recorder`, `wspr`/`psk`). Each modality covers a blind spot of the others,
and the network replicates that coverage across space.

## 4. Why the network does not depend on CHU — or any one transmitter

The world's HF time-standard transmitters are a slowly shrinking resource. CHU
(Ottawa) is the nearest-term risk: it has faced recurrent funding threats and
its disappearance would, for a **single central-US station**, be painful — CHU
is that station's only northeast azimuth, its only non-US-longitude path, the
only source of FSK-decoded verified UTC / DUT1 / leap-second data, and it fills
frequency gaps (3.330 / 7.850 / 14.670 MHz) between WWV's allocations. A lone
receiver dependent on exactly four transmitters degrades sharply when one goes
dark.

**A network does not share that fragility, by construction:**

- **Geometric diversity comes from receiver geometry, not transmitter count.**
  Azimuthal and pierce-point coverage that a single station could only get from
  a fourth transmitter is instead supplied by spreading receivers across the
  map. The mesh recovers the missing quadrant from *where the receivers are*.
- **Graceful local degradation.** Each node leans on whichever transmitters it
  can actually hear. European or Asian nodes use RWM, BPM, or other regional
  services; North American nodes use WWV/WWVH. No single station's outage, and
  no single transmitter's silence, takes down the network product — it only
  thins coverage locally, and the common-illuminator and tomographic methods of
  §2 keep working on the transmitters that remain.
- **Redundant absolute anchoring.** Every node carries its own GNSS VTEC
  anchor, so the absolute scale never depended on CHU's verified-UTC stream in
  the first place. CHU's time-code was a nice independent cross-check at one
  site; the network's absolute scale is distributed and GNSS-backed.

In short: the same step that turns the instrument from a perturbation sensor
into an imager (§2) *also* makes it robust to the attrition of the transmitter
infrastructure it listens to. Resilience and capability come from the same
design choice — more receivers.

> **Operational note for CHU specifically:** while CHU remains at risk, stations
> should keep its routines *reversibly disabled* rather than deleted when it
> goes off-air — it has returned from outages before, and its decoder and path
> geometry should remain instantly re-enablable.

## 5. Integration with the wider ecosystem

The network's products are designed to complement, not duplicate, existing
datasets:

- **GNSS-TEC networks (Madrigal / CEDAR, IGS IONEX):** the HF mesh is the
  *high-pass* complement to IONEX's slow, coarse absolute maps — minute-cadence
  path perturbations against a 2-hour, 2.5°×5° background.
- **HamSCI / PSWS / Grape:** the natural home. The dTEC product is a calibrated,
  anchored, multi-frequency sibling of the Grape Doppler-shift observable on the
  same transmitters; co-located comparison is a direct cross-validation.
- **Ionosondes (GIRO / DIDBase):** nearby digisonde foF2/hmF2 calibrates the
  network's climatological foF2 model and anchors its MUF estimates to
  measurement.
- **Space-weather indices (Kp, Dst, F10.7, GOES X-ray):** superposed-epoch
  analysis of network dTEC and D-layer absorption against flare and storm
  catalogs demonstrates the products are tracking real ionospheric physics.

## 6. The bottom line for an operator

Adding a node does three things at once. It contributes another vantage point to
a distributed-aperture instrument that can *image* ionospheric structure no
single station can localize (§2); it adds several **co-located, GPS-co-registered
modalities** — absolute TEC, oblique sounding, dense opportunistic paths, and the
geomagnetic driver — that each cover a blind spot of the others (§3); and it
makes the whole network more robust to the steady loss of the HF time standards
it depends on, CHU included (§4). One station with one instrument is a sensor;
many nodes each carrying many instruments is an observatory — and one that does
not break when a transmitter, or a single mode, goes dark.

---

## Related documentation

- [`hf-timestd/docs/PHYSICS.md`](../../hf-timestd/docs/PHYSICS.md) — single-station
  ionospheric measurement physics (dTEC, VTEC anchoring, TID detection, mode ID).
- [`CLIENT-CONTRACT.md`](CLIENT-CONTRACT.md) — how clients share the station's
  timing authority and shared sink.
- [`SCINTILLATION-MONITORING.md`](SCINTILLATION-MONITORING.md) — related
  station-wide ionospheric monitoring capability.
