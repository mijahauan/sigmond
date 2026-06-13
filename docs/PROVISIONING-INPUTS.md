# Per-installation provisioning inputs

**Scope:** every piece of unique-per-installation information, credential, and
manual action required to stand up a sigmond + dasi2-client host — in **both**
deployment models (golden image and sigmond clone + install). Use this to plan
how each input is collected, applied, and (for images) reset per clone.

**Status (2026-06-13):** the *inventory* (§1–§6) reflects what the code/templates
require today. The *site-profile schema* (§8) and *first-boot checklist* (§9) are
**proposals** for discussion — they are not yet implemented. What exists today is
`/etc/sigmond/coordination.env` (callsign/grid + per-radiod facts) and the
per-client `setup-station.sh` / `smd config` wizards.

---

## 0. The three kinds of input (and why they differ)

Each input falls into one of three buckets, and each bucket needs a different
handling strategy — this is the core planning distinction:

1. **Shareable** — identical across the whole grant; safe to bake into a golden
   image. (Software, PHaRLAP binaries + built pyLAP as single-licensee internal
   use, systemd units, parametric defaults.)
2. **Per-site identity** — must be unique per host; in the clone model the wizard
   collects it, in the image model it **must be reset on every clone** (a golden
   image otherwise ships one site's callsign/hostname/keys to all clones).
3. **Per-site secret / external action** — must be injected per host and **never
   baked into a shared image**; several require a human to act out-of-band (create
   an account, register a key, request a licensed download) and **cannot be
   automated at all**.

The "kind" column in the tables below uses **S** (shareable), **I** (identity),
**X** (secret/external).

---

## 1. Station identity  — kind: I

| Input | Component(s) | Required? | Source |
|---|---|---|---|
| Callsign | sigmond `STATION_CALL` → hf-timestd, psk, mag, wspr | Required for any upload/report | wizard; published once via `coordination.env` |
| Maidenhead grid **or** precise lat/lon (+ elevation) | same | Required | wizard (grid *or* decimal degrees) |
| Station description / antenna text | hf-timestd `[station].description` | Optional | wizard |
| PSWS **station id** (`Sxxxxxx`) | hf-timestd, mag-recorder, psk-recorder | Required if uploading | assigned by PSWS at account creation (§3) |
| PSWS **instrument id** | hf-timestd, mag-recorder | Required if uploading | assigned by PSWS |

## 2. Reporter / network-account IDs  — kind: I

| Input | Component | Required? | Notes |
|---|---|---|---|
| WSPRnet reporter call (+ grid) | wspr-recorder / wsprdaemon-client | Opt | usually == station callsign/grid |
| PSKReporter call (+ grid) | psk-recorder | Opt | == station callsign/grid |
| PSWS station/instrument id | (see §1) | Opt | drives GRAPE / mag / IQ upload paths |

## 3. External accounts & one-time registrations  — kind: X (cannot be automated)

| Action | For | Required? | What it involves |
|---|---|---|---|
| Create **PSWS account** | GRAPE / mag / IQ uploads | Opt | sign up at <https://pswsnetwork.caps.ua.edu/>; receive station id |
| **Register PSWS SFTP public key** in the web portal | uploads | Opt | server is StrictModes, SFTP-only. `hf-timestd/scripts/setup-psws-keys.sh` generates the keypair and prints the public key — the operator **must paste it into the PSWS portal** (authorized_keys cannot be uploaded over SFTP) |
| Create **NASA Earthdata** account | hf-timestd IONEX / DCB | Opt | register at <https://urs.earthdata.nasa.gov> |
| Request + download **PHaRLAP 4.7.4** from DST | hf-timestd raytracing | Opt | license request to DST (Australia); multi-day latency; **not redistributable** (see EXTERNAL_PREREQUISITES.md §3) |
| Obtain **RAC tunnel user/token** | rac (frpc) | Opt | from `wd-admin` (Rob's gateway): `RAC_USER`, `RAC_TOKEN` |

## 4. Per-site secrets / credentials  — kind: X (per host; never in a shared image)

| Secret | Path | Component |
|---|---|---|
| PSWS SFTP private key | `~timestd/.ssh/id_ed25519_psws_<station_id>` | hf-timestd, mag |
| Earthdata netrc | `/etc/hf-timestd/earthdata-netrc` (mode 0600) | hf-timestd |
| RAC frpc token | `rac` `frpc.toml` (`<RAC_TOKEN_FROM_WD_ADMIN>`) | rac |
| Upload keys / credentials | `/etc/hs-uploader/keys`, `/etc/signal-recorder/credentials` | uploaders |

## 5. Hardware-specific configuration  — kind: I

| Input | Component | Required? |
|---|---|---|
| SDR device + **serial / identifier**, sample rate, gain | ka9q-radio / radiod | Required |
| Antenna / front-end description, calibration | radiod / station | Opt |
| **radiod status (mDNS) address** to consume | every consuming client (`SIGMOND_RADIOD_STATUS`) | Required |
| Timing-authority mode + **GPS/PPS** wiring | hf-timestd | Required-ish |
| BPSK **PPS injector frequency** | hf-timestd L6 | Opt |
| **GNSS VTEC receiver** host/IP (ZED-F9P / ser2net) | hf-timestd `[gnss_vtec].host` | Opt |
| GPSDO presence | gpsdo-monitor | Opt |

## 6. Host / machine identity & manual host actions  — kind: I

| Item | Why per-host | Image-clone concern |
|---|---|---|
| **hostname** | radiod multicast names are hostname-derived | **must change on clone** or clones collide on the LAN |
| `/etc/machine-id` | systemd/network identity | **regenerate per clone** |
| **SSH host keys** | host identity | **regenerate per clone** |
| IP / DHCP / DNS | LAN | per-host |
| **FFT wisdom** | per-CPU, slow manual step | regenerate per hardware |
| `isolcpus` / grub tuning + **reboot** | CPU pinning | per-host, needs reboot |
| timezone / locale | — | per-site |

---

## 7. What the wizard already collects (clone model)

`hf-timestd/scripts/setup-station.sh` (and the equivalent `smd config` flow)
prompts for, or inherits from `coordination.env`: callsign, grid **or** lat/lon,
PSWS enable + station/instrument id, ka9q-radio status address, source mode,
timing-authority mode, GPS+PPS accuracy, BPSK PPS injector, GNSS VTEC presence,
IQ archive + compression. It does **not** (and cannot) perform the §3 external
actions or install the §4 secrets — those remain operator responsibilities.

---

## 8. PROPOSED: a single per-site profile

Today, callsign/grid flow through `coordination.env`, but PSWS ids, reporter
ids, hardware selections, and secrets are entered per client. A single
non-secret **site profile** that every client reads (with sigmond rendering each
client's config from it) would collapse both models to one fill-in step.

Proposed location: `/etc/sigmond/site-profile.toml` (sigmond-owned, world-
readable — **non-secret only**; secrets stay in their §4 paths).

```toml
# /etc/sigmond/site-profile.toml — non-secret per-site identity.
# Single source of truth; sigmond renders each client's config from this and
# publishes the cross-cutting keys into coordination.env. NO secrets here.
schema_version = 1

[station]
callsign     = "AC0G"
grid_square  = "EM38ww"          # or set latitude/longitude below
latitude     = 38.93             # optional if grid_square given
longitude    = -92.33
elevation_m  = 200
description   = "RX888 MkII + resonant dipole"

[psws]                            # HamSCI PSWS / GRAPE
enabled       = true
station_id    = "S000082"
instrument_id = "172"

[reporters]
wsprnet_call    = "AC0G"          # default: [station].callsign
pskreporter_call = "AC0G"
# grid defaults to [station].grid_square unless overridden

[host]
hostname      = "dasi2-em38"      # radiod instance names derive from this

[hardware]
sdr           = "rx888"
sdr_serial    = ""                # fill from `lsusb` / radiod discovery
radiod_status = "dasi2-em38-status.local"
timing        = "gps_pps"         # gps_pps | rtp | …
gnss_vtec_host = ""               # optional ZED-F9P / ser2net host

# Secrets are referenced, never stored here — they live at:
#   PSWS key      ~timestd/.ssh/id_ed25519_psws_<station_id>
#   Earthdata     /etc/hf-timestd/earthdata-netrc
#   RAC token     rac frpc.toml
[secrets]
# Declared so `smd admin validate` can check presence (not contents):
require = ["psws_key", "earthdata_netrc"]   # add "rac_token" if RAC enabled
```

**Rendering model (proposed):** `smd config render` reads `site-profile.toml`,
writes/refreshes each client's `*-config.toml` from its template, and publishes
`STATION_CALL` / `STATION_GRID` / `SIGMOND_RADIOD_*` into `coordination.env`.
Clients keep working stand-alone: if `site-profile.toml` is absent, the existing
per-client wizard remains the fallback (contract unchanged).

---

## 9. PROPOSED: first-boot personalization checklist (image model)

A golden image carries **shareable** state only (software, PHaRLAP/pyLAP, units).
The reference host's **identity** and any baked **secrets** must be wiped from the
image before capture, and re-applied per clone on first boot. A `smd personalize`
oneshot (or a documented runbook) should perform, in order:

**Before capturing the image (on the reference host):**
- [ ] Remove all §4 secrets (PSWS key, earthdata-netrc, RAC token).
- [ ] Clear identity: truncate `/etc/machine-id`; remove `/etc/ssh/ssh_host_*`.
- [ ] Reset `site-profile.toml` to placeholders (or remove it).
- [ ] Clear logs / data roots / FFT wisdom that are host-specific.
- [ ] Leave PHaRLAP + built pyLAP in place (shareable, controlled image).

**On first boot of each clone:**
- [ ] Set **hostname** (drives radiod instance/mDNS names).
- [ ] Generate `/etc/machine-id` (`systemd-machine-id-setup`).
- [ ] Regenerate **SSH host keys** (`dpkg-reconfigure openssh-server` / `ssh-keygen -A`).
- [ ] Confirm network / IP / DNS.
- [ ] Drop in the site's **`site-profile.toml`** (identity + reporter ids).
- [ ] Install per-site **secrets** from the secure channel (§4) — see "secrets
      delivery" below.
- [ ] `smd config render` (or `setup-station.sh --from-profile`) to re-render all
      client configs and refresh `coordination.env`.
- [ ] Regenerate **FFT wisdom** (per-CPU; slow).
- [ ] Apply host tuning (`isolcpus`, grub) and **reboot** if changed.
- [ ] `smd admin validate` + `hf-timestd data sources` to confirm identity,
      secrets presence, radiod consumption, and raytracing/data feeds.

**Still requires a human, out of band (cannot be automated — §3):** PSWS account
+ public-key registration, Earthdata account, PHaRLAP request/download (only if
not already in the image), RAC token issuance.

**Secrets delivery (to decide):** options for getting §4 secrets onto a clone
without baking them into the shared image — e.g. an encrypted per-site bundle
(age/sops) unlocked at first boot, an operator USB drop, or `smd` pulling from a
secrets store. This is the main open design decision for the image model.

---

## 10. Open items to decide

1. **Secrets delivery channel** for the image model (§9).
2. Whether to build `site-profile.toml` + `smd config render` (§8), or keep
   per-client wizards as the only path.
3. A `smd personalize` first-boot oneshot vs. a manual runbook (§9).
4. Whether PHaRLAP rides in the DASI2 image (single-licensee, controlled) or is
   staged per host even for image clones — see EXTERNAL_PREREQUISITES.md §3.

See also: `hf-timestd/docs/EXTERNAL_PREREQUISITES.md` (the per-component detail
for PHaRLAP, Earthdata, PSWS), and `greenfield-runbook.md` (the clone-model
bring-up phases).
