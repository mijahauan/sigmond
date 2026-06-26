# SigMonD Suite — Requirements Index

The single front door to the suite's requirements baseline. Each component's
requirements live **in its own repo** (`<repo>/docs/REQUIREMENTS.md`); this index
is the centralized map — same pattern as the catalog and
[PSWS-MAPPING.md](PSWS-MAPPING.md): distributed source of truth, central index.

- **Method & template:** [REQUIREMENTS-TEMPLATE.md](REQUIREMENTS-TEMPLATE.md)
- **Interface (seam) requirements:** [CLIENT-CONTRACT.md](CLIENT-CONTRACT.md) (the
  sigmond↔component contract) and [PSWS-INTERFACE-BOUNDARY.md](PSWS-INTERFACE-BOUNDARY.md)
  (the sigmond↔PSWS seam). Component docs **reference** these, never restate them.
- **Live delivery tracking:** [Project #18](https://github.com/orgs/HamSCI/projects/18).
  Requirements docs are the durable *what & why*; #18 is the *what's being done*.
  Requirement IDs (`SIG-F-001`, `HFT-Q-003`, …) are the spine linking spec → issue → test.

## Reading the status

Each doc tags every requirement with **provenance** — `[DOC]` documented · `[CODE]`
implicit-in-code (reverse-engineered) · `[NEW]` surfaced by the review — and
**status** — ✅ implemented · 🟡 partial · ⬜ planned. A component's *tag mix* is its
maturity fingerprint: mostly `[DOC]/[CODE]✅` = mature-but-was-undocumented; lots of
`[NEW]⬜` = scope never captured.

## Components

| Component | Prefix | Kind | Maturity | Requirements doc |
|---|---|---|---|---|
| **sigmond** | `SIG` | overseer | Mature | [sigmond/docs/REQUIREMENTS.md](REQUIREMENTS.md) ✅ |
| **hf-timestd** | `HFT` | client (timing authority) | Mature | [hf-timestd/docs/REQUIREMENTS.md](https://github.com/HamSCI/hf-timestd/blob/main/docs/REQUIREMENTS.md) ✅ |
| **superdarn-sounder** | `SDS` | client | Early | [superdarn-sounder/docs/REQUIREMENTS.md](https://github.com/HamSCI/superdarn-sounder/blob/main/docs/REQUIREMENTS.md) ✅ |
| **wspr-recorder** | `WSP` | client | Mature | [wspr-recorder/docs/REQUIREMENTS.md](https://github.com/HamSCI/wspr-recorder/blob/main/docs/REQUIREMENTS.md) ✅ |
| **psk-recorder** | `PSK` | client | Mature | [psk-recorder/docs/REQUIREMENTS.md](https://github.com/HamSCI/psk-recorder/blob/main/docs/REQUIREMENTS.md) ✅ |
| **mag-recorder** | `MAG` | client (non-radiod) | Active | [mag-recorder/docs/REQUIREMENTS.md](https://github.com/HamSCI/mag-recorder/blob/main/docs/REQUIREMENTS.md) ✅ |
| **codar-sounder** | `CDR` | client | Active | [codar-sounder/docs/REQUIREMENTS.md](https://github.com/HamSCI/codar-sounder/blob/main/docs/REQUIREMENTS.md) ✅ |
| **hfdl-recorder** | `HFD` | client | Active | [hfdl-recorder/docs/REQUIREMENTS.md](https://github.com/HamSCI/hfdl-recorder/blob/main/docs/REQUIREMENTS.md) ✅ |
| **hf-tec** | `TEC` | client | Early | [hf-tec/docs/REQUIREMENTS.md](https://github.com/HamSCI/hf-tec/blob/main/docs/REQUIREMENTS.md) ✅ |
| **meteor-scatter** | `MTS` | client | Active | [meteor-scatter/docs/REQUIREMENTS.md](https://github.com/HamSCI/meteor-scatter/blob/main/docs/REQUIREMENTS.md) ✅ |
| **ka9q-python** | `KQP` | library | Mature | [ka9q-python/docs/REQUIREMENTS.md](https://github.com/HamSCI/ka9q-python/blob/main/docs/REQUIREMENTS.md) ✅ |
| **hamsci-dsp** | `DSP` | library | Active | [hamsci-dsp/docs/REQUIREMENTS.md](https://github.com/HamSCI/hamsci-dsp/blob/main/docs/REQUIREMENTS.md) ✅ |
| **hs-uploader** | `HSU` | library | Active | [hs-uploader/docs/REQUIREMENTS.md](https://github.com/HamSCI/hs-uploader/blob/main/docs/REQUIREMENTS.md) ✅ |
| **callhash** | `CLH` | library | Mature | [callhash/docs/REQUIREMENTS.md](https://github.com/HamSCI/callhash/blob/main/docs/REQUIREMENTS.md) ✅ |
| **gpsdo-monitor** | `GDM` | infra | Active | [gpsdo-monitor/docs/REQUIREMENTS.md](https://github.com/HamSCI/gpsdo-monitor/blob/main/docs/REQUIREMENTS.md) ✅ |
| **igmp-querier** | `IGQ` | infra | Stub/Infra | [igmp-querier/docs/REQUIREMENTS.md](https://github.com/HamSCI/igmp-querier/blob/main/docs/REQUIREMENTS.md) ✅ |
| **sigmond-rac** | `RAC` | infra | Stub/Infra | [sigmond-rac/docs/REQUIREMENTS.md](https://github.com/HamSCI/sigmond-rac/blob/main/docs/REQUIREMENTS.md) ✅ |

> Upstream/vendored C projects (`ka9q-radio`, `ka9q-web`, `onion`, `ft8_lib`) are
> out of scope for suite requirements docs — their requirements live with their
> upstreams. Sigmond's requirements (`SIG-*`) cover how the suite *consumes* them.

## Conventions for new docs

- One `docs/REQUIREMENTS.md` per repo, filled from [the template](REQUIREMENTS-TEMPLATE.md).
- Right-size depth to maturity: a Stub/Infra component's doc is short and mostly
  `[CODE]`; an Early one is mostly `[NEW]/⬜`.
- Derive the I/O sections (§8) from `deploy.toml` + `inventory --json` so they
  can't drift from code.
- Promote every `[NEW]` gap to a Project #18 issue and back-link it in §13.
