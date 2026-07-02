# Per-installation provisioning inputs

**Scope:** every piece of unique-per-installation information, credential, and
manual action required to stand up a sigmond + dasi2-client host — in **both**
deployment models (golden image and sigmond clone + install). Use this to plan
how each input is collected, applied, and (for images) reset per clone.

**Status (2026-06-13):** the *inventory* (§1–§6) reflects what the code/templates
require today. The **site profile (§8) is IMPLEMENTED** (`smd config render`);
the *first-boot checklist* (§9) is still a proposal. Today's distribution channel
is `/etc/sigmond/coordination.toml` → `coordination.env` (consumed by every
client), now also fed from `site-profile.toml`.

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
| Maidenhead grid **or** precise lat/lon (+ elevation) | same | Required | **auto-detected** from a GPS fix via gpsd (the GPSDO/Bodnar feeds gpsd) — offered as the wizard default; grid derived from lat/lon. Falls back to manual entry. |
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
| **radiod status (mDNS) address** to consume | every consuming client (`SIGMOND_RADIOD_STATUS`) | Required — **auto-discovered** via mDNS (`_ka9q-ctl._udp`, matched on this host's `source=<hostname>` record; names carry a hardware suffix e.g. `sigma-rx888mk2-status.local`). Offered as the wizard default. |
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

**Auto-detection (2026-06-13).** The wizard now fills two of the most-prompted
fields automatically, offering them as confirmable defaults (and applying them
directly in `--non-interactive` / headless installs):
- **Location** — a GPS fix from gpsd (the GPSDO/Bodnar feeds gpsd) seeds
  latitude/longitude; the grid square is derived from them.
- **radiod status** — mDNS discovery of this host's own radiod
  (`_ka9q-ctl._udp`, matched on `source=<hostname>`).

Both degrade silently to manual entry when gpsd has no fix / no radiod is
advertised, so the contract is unchanged. Separately, `gpsdo-monitor` now parses
position from the Bodnar's NMEA and exposes a Maidenhead locator
(`NmeaState.maidenhead()`), so a host that reads the Bodnar directly (no gpsd)
can also surface its grid.

---

## 8. Single per-site profile (IMPLEMENTED)

One operator-edited, non-secret file is the source of truth for per-site
identity; `smd config render` translates it into `coordination.toml` /
`coordination.env`, which every client already consumes.

```
sudo smd config render --init      # scaffold /etc/sigmond/site-profile.toml
sudo $EDITOR /etc/sigmond/site-profile.toml
smd config render --dry-run        # preview what would be published
sudo smd config render             # write coordination.toml [host]+[station] + .env
```

**What render does:** `[station]` → coordination `[host]` (call/grid/lat/lon,
via the canonical identity writer) and an additive `[station]` block (PSWS ids,
reporter calls); then re-renders `coordination.env`, which now also emits
`STATION_PSWS_STATION_ID`, `STATION_PSWS_INSTRUMENT_ID`, `STATION_WSPRNET_CALL`,
`STATION_PSKREPORTER_CALL` alongside the existing `STATION_CALL`/`GRID`/`LAT`/`LON`.
It never touches secrets (§10) and leaves `smd config identity`/`refresh`
backward-compatible (they only patch `[host]`). Hardware hints in the profile are
captured for reference; radiod config remains authoritative for the live status
address.

> **Client adoption:** clients consume these from `coordination.env` as wizard
> defaults the same way they already read `STATION_CALL`/`GRID`:
> - **hf-timestd** `setup-station.sh` auto-fills the PSWS Station/Instrument ID
>   prompts from `STATION_PSWS_STATION_ID` / `STATION_PSWS_INSTRUMENT_ID` (and
>   defaults the PSWS toggle on when an id is published).
> - **mag-recorder** already reads `STATION_PSWS_STATION_ID` — the key-name
>   alignment makes it adopt automatically.
> - `STATION_WSPRNET_CALL` / `STATION_PSKREPORTER_CALL` are an override hook for
>   a reporter call that differs from `STATION_CALL`; wspr/psk report under the
>   station callsign today, so nothing consumes them yet.

**Phase 2 (one-file identity, 2026-07-02):** render additionally **pushes
PSWS ids through** into each installed PSWS recorder's own config file
(hf-timestd `[station].id`/`instrument_id`, mag-recorder
`[station].psws_station_id`/`instrument_id`) — the uploader manifest resolves
`{station_id}`/`{instrument_id}` from those files, so coordination alone was
not enough. Empty profile values never clobber a hand-configured id. The
profile gained `[psws.instruments]` (per-recorder ids; the legacy single
`instrument_id` remains hf-timestd/GRAPE's) and `[reporters].reporter_id`
(the WSPR/PSK instance id, e.g. `AC0G/S`). When `[psws].enabled`, render also
ensures the station SSH key (`/etc/hs-uploader/keys/id_ed25519_host`) exists
and prints the pubkey to register at the PSWS portal. `--if-present` makes
render a quiet no-op when no profile exists (used by the unconditional
bring-up step). **`smd bringup` now consumes the profile**: identity flags
default from it (a filled profile = prompt-free bring-up), the full render
runs before Stage 3 (so wizards see the PSWS env defaults), and a Stage 4
re-render pushes ids into the client configs created by Stage 3 before the
uploader manifest resolves them.

Location: `/etc/sigmond/site-profile.toml` (sigmond-owned, world-readable —
**non-secret only**; secrets stay in their §4 paths). Schema:

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

## 9. First-boot personalization (image model)

A golden image carries **shareable** state only (software, PHaRLAP/pyLAP, units).
The reference host's **identity** and any baked **secrets** must be wiped from the
image before capture, and re-applied per clone on first boot.

**`smd admin personalize`** (IMPLEMENTED) orchestrates the per-clone first-boot
steps — plan-first, `--yes` to execute (self-elevates), idempotent:

```bash
sudo smd admin personalize --hostname dasi2-em38 --reset-identity --yes
```

It: sets the **hostname** (from `--hostname` or site-profile `[host].hostname`;
radiod mDNS names follow); with `--reset-identity`, regenerates `/etc/machine-id`
+ **SSH host keys** (destructive, clone-only, opt-in); runs **`smd config render`**
(coordination from `site-profile.toml`, scaffolding it if absent); then reports
**`smd admin secrets status`** + **`smd admin validate`**; writes a
`/etc/sigmond/.personalized` sentinel; and prints the remaining manual steps. Run
without `--yes` to preview the plan.

**Before capturing the image (on the reference host):** this checklist is
now IMPLEMENTED as **`smd admin capture-prep`** (plan-first; `--yes`
executes; `--keep-data` for debug captures). It stops all managed services,
then: removes §4 secrets + every station SSH key; resets `site-profile.toml`
to the scaffold; clears coordination `[host]`/`[station]`; resets PSWS ids in
the recorder configs to placeholders; removes per-instance recorder
configs/env files (from the instance registry); clears data roots, upload
cursors, learned callsign hashes, per-CPU FFT wisdom, logs + journal; and
LAST wipes OS identity (truncate `/etc/machine-id`, remove SSH host keys,
remove `.personalized`). It finishes by running
`smd admin readiness --gate capture` as the verdict, then instructs:
**shut down (do not reboot)** and capture on the Proxmox host with
`scripts/proxmox/golden-image.sh capture <vmid>` (full-clone → `qm template`,
date-stamped name). Per site: `golden-image.sh clone <template-id> <site>`
(strips inherited hookscript/hostpci — the proxmox bootstrap re-creates them
per host), boot, `smd admin personalize --reset-identity --yes`.

- [ ] **Keep PHaRLAP baked in** — `/opt/pharlap_4.7.4` (with its DST licence
      files + `.provenance`) and the built pyLAP in the venv stay in the image
      (decided 2026-06-14; capture-prep never touches it). Verify:
      `hf-timestd data sources` → `Raytrace: available`.

**On first boot of each clone** — `smd admin personalize --reset-identity --yes`
does the automatable steps; the operator still:
- [ ] supplies the site's **`site-profile.toml`** (or fills the scaffold) before/at render;
- [ ] installs per-site **secrets** — `smd admin secrets install secrets.age` (§10)
      (personalize reports which are missing);
- [ ] regenerates **FFT wisdom** (per-CPU; slow — see `smd admin wisdom`);
- [ ] applies host tuning (`isolcpus`, grub) and **reboots** if changed (also to
      activate new SSH host keys).

**Still requires a human, out of band (cannot be automated — §3):** PSWS account
+ public-key registration, Earthdata account, PHaRLAP request/download (only if
not already in the image), RAC token issuance.

**Secrets delivery:** see §10 — the per-site secrets install step.

---

## 10. Secrets delivery (recommended design)

**Status (2026-06-13):** **IMPLEMENTED** as `smd admin secrets`
(status / template / install / bundle). Default channel = an `age`-encrypted
per-site bundle. (`age` is required only for the bundle channel: `apt install age`.)

### The surface is small — most "secrets" generate themselves

The crucial realisation: the SSH-key secrets are **generated on the host**, so
they are never delivered and are already image-clone-safe:

- **PSWS SSH key** — `setup-psws-keys.sh` runs `ssh-keygen` on the host; the
  private key never leaves. Per-host. Only the *public* key is registered via
  the PSWS portal (the un-automatable human step, §3).
- **hs-uploader SSH key** — auto-generated (`ed25519`) on first use at
  `/etc/hs-uploader/keys/`. Per-host.

So the genuinely **delivered** secrets reduce to **two, both optional**:

| Secret | Scope | Issued by | Bake into image? |
|---|---|---|---|
| Earthdata netrc (login/password) | **fleet-shared** (one NASA account serves all hosts) | operator's NASA account | No — real password |
| RAC `user`/`token`/`remotePort` (frpc.toml) | **per-station** | Rob / wd-admin | No |
| `frps-ca.crt` | fleet-shared | wd-admin | Yes — it's a CA cert, not secret |

Because the surface is two optional files, no heavy infra (Vault/sops
pipelines) is warranted — a thin installer plus a simple encrypted bundle.

### `smd admin secrets` — standardise placement, perms, validation (channel-agnostic)

- `smd admin secrets template` — write placeholder files at the canonical paths
  (root; never clobbers an existing file).
- `smd admin secrets install <dir | bundle.age>` — copy operator-supplied
  secrets to their canonical paths (`/etc/hf-timestd/earthdata-netrc`,
  `/etc/sigmond/frpc.toml`), `chown` the owning user + `chmod 0600`, and
  **validate format** (netrc has a `machine urs.earthdata.nasa.gov` line + real
  login/password; frpc token is non-placeholder). Root; never echoes contents.
  A `.age` source is decrypted (`-i <identity>`, or prompt for a passphrase) and
  extracted to a 0700 temp dir, then placed.
- `smd admin secrets bundle <dir> -o secrets.age {-R recipients | -p}` —
  operator-side helper: tar the known secret files found in `<dir>` and
  age-encrypt them into the per-site bundle.
- `smd admin secrets status` — presence / perms / validity, **never contents**.
  Also surfaced by **`smd admin validate`** (a `secrets` harmonization rule):
  Earthdata is flagged only when present-but-broken (absence is a valid choice,
  it's optional enrichment); RAC's `frpc.toml` is required when the `rac`
  component is enabled, and a placeholder token is flagged whenever the file
  exists. Run validate as root for full content validation (the files are 0600).

### Default channel: `age`-encrypted per-site bundle

Per-site `secrets.age` = `{earthdata-netrc, frpc.toml}` encrypted with
[`age`](https://github.com/FiloSottile/age) (single static binary, no key
server — well-matched to a small fleet):

- **Safe to ship anywhere** (USB, scp, or even *beside the image*) because it is
  encrypted. First boot runs `smd admin secrets install secrets.age`; the operator
  supplies the `age` identity once — a passphrase, or a key on removable media.
- Plain `smd admin secrets install <usbdir>` remains available for the trivial case
  (trusted local transport, no crypto).

### Rules

1. Generated-on-host secrets (SSH keys) are **never delivered** — let them
   self-generate per clone.
2. True secrets **never ride an *exported* image**; deliver via the bundle. Even
   fleet-shared Earthdata (which is optional) is delivered, not baked.
3. Fleet-shared **non-secret** material (`frps-ca.crt`) *may* be baked into the
   controlled image.

### Optional hardening (later)

`systemd-creds encrypt` + `LoadCredentialEncrypted=` so the placed secrets are
host-bound-encrypted at rest and a clone cannot reuse them (a feature). Adds
complexity; defer until the basic installer is in use.

---

## 11. Open items to decide

1. ✅ DONE — `site-profile.toml` + `smd config render` (§8); clients adopt the
   published keys (hf-timestd PSWS prompts; mag-recorder auto via name
   alignment). Reporter-call override keys remain available for future use.
2. ✅ DONE — `smd admin secrets` (status/template/install/bundle) + age-bundle
   flow implemented and verified (§10), and surfaced in `smd admin validate`
   via a `secrets` harmonization rule (gated on enabled components; no
   site-profile dependency).
3. ✅ DONE — `smd admin personalize` first-boot oneshot implemented (§9).
4. ✅ DECIDED 2026-06-14 — **PHaRLAP is baked into the controlled DASI2 image**
   (single-licensee internal deployment). The image carries `/opt/pharlap_4.7.4`
   (with its DST licence files + `.provenance`) and the built pyLAP in the venv;
   the pre-capture wipe (§9) keeps them. The image is licence-controlled — never
   published or shared outside the grant. Model B (clone + install) operators
   still stage PHaRLAP themselves. See EXTERNAL_PREREQUISITES.md §3.

See also: `hf-timestd/docs/EXTERNAL_PREREQUISITES.md` (the per-component detail
for PHaRLAP, Earthdata, PSWS), and `greenfield-runbook.md` (the clone-model
bring-up phases).
