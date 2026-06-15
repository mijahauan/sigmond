# Greenfield install runbook — bare host to recording station

The end-to-end **operational** walkthrough: from a freshly-`install.sh`'d host
to a tuned, validated SDR node recording and uploading. This picks up where the
install docs stop (they end at "install a component") and documents the *order*
and the *host tuning* that make radiod run effectively.

- New to sigmond? Run `install.sh` first — see
  [`install-quickstart.md`](install-quickstart.md) (the bootstrap) and
  [`installation-guide.md`](installation-guide.md) (Proxmox / networking /
  capacity).
- Multi-client / multi-instance details: [`MULTI-INSTANCE-ARCHITECTURE.md`](MULTI-INSTANCE-ARCHITECTURE.md).
- Native build deps: [`native-binaries.md`](native-binaries.md).
- **Automating this runbook** (TUI-driven, profile-based bring-up) is specced in
  [`install-orchestration-design.md`](install-orchestration-design.md).

Throughout, the **worked example** is the first station this runbook was written
from: host `sigma`, callsign **AC0G**, grid **EM38ww**, one RX888mk2, running
radiod + wspr-recorder + hf-timestd. Substitute your own values.

---

## The canonical order

Do these in order. Each phase assumes the previous one succeeded.

| # | Phase | Command(s) | Why this order |
|---|-------|-----------|----------------|
| 0 | Bootstrap | `./install.sh` | Gets `smd`, the venv, the two repos. |
| 1 | Identity | `smd config identity` | Callsign/grid feed every client's reporter id. |
| 2 | Topology | `smd enable <component>` | Declares what this host runs. |
| 3 | Install | `smd install <component>` | Clones, builds native deps, runs `apply`. |
| 4 | radiod config | `smd config init radiod` | The SDR daemon needs a `[global]`+`[rx888]` conf. |
| 5 | FFT wisdom | `smd admin wisdom plan` | **Long, manual, one-time.** radiod won't start cleanly without it. |
| 6 | Host tuning | (mostly automatic via `apply`) + `smd admin validate` | rmem_max, CPU affinity, governor — radiod RT correctness. |
| 7 | Client config | `smd config edit <client>` | Reporter ids, PSWS station/instrument. |
| 8 | Start + verify | `smd start` / `smd admin validate` / `smd status` | Bring it up, confirm the board is clean. |
| 9 | PSWS key (if uploading) | `setup-psws-keys.sh` | External — register with PSWS. |

> **What `smd install` / `smd apply` now do for you automatically** (so you don't
> hand-run them): link every unit in each component's `systemd/` dir, write the
> radiod CPUAffinity drop-ins, install the `smd-cpu-governor.service` boot
> oneshot, write the `99-sigmond-multicast.conf` rcvbuf drop-in, and reconcile
> radiod channel fragments + FX3 firmware. The only host-tuning step that is
> *not* automatic is FFT wisdom (phase 5, it's slow) and `isolcpus` (phase 6,
> needs a reboot).

---

## Phase 1 — Identity

```bash
smd config identity            # prompts for callsign + Maidenhead grid
# worked example: callsign AC0G, grid EM38ww
```

Grid lands in `/etc/sigmond/coordination.toml`; callsign threads into each
client's reporter id (phase 7).

## Phase 2 — Topology

Enable only what this host runs. Everything ships disabled.

```bash
smd enable ka9q-radio          # the SDR daemon — always first (start_priority 0)
smd enable wspr-recorder
smd enable hf-timestd
# leave the rest disabled:  smd disable psk-recorder   (etc.)
smd list                       # confirm enabled/disabled
```

`enable`/`disable` edit `/etc/sigmond/topology.toml`. Note the canonical name is
`ka9q-radio` (alias `radiod`); `smd enable radiod` resolves to it.

## Phase 3 — Install

```bash
smd install ka9q-radio         # native build: apt deps + make install
smd install wspr-recorder      # clones callhash/hs-uploader siblings as needed
smd install hf-timestd
```

Each install clones to `/opt/git/sigmond/<name>`, builds any native binary per
[`native-binaries.md`](native-binaries.md) (pinned to a commit, manifest
emitted), installs the client's venv, and runs `smd apply`.

> **ka9q-python is installed *editable*** into every venv (clients **and**
> sigmond's own), so `smd admin validate`'s protocol-compat check always reflects the
> live source pin. If you advance the ka9q-radio build, re-stamp the pin:
> `python /opt/git/sigmond/ka9q-python/scripts/sync_types.py --apply --ka9q-radio /opt/git/sigmond/ka9q-radio`
> (header-only re-stamp when `--check` already says "in sync").

> **hf-timestd raytracing (PHaRLAP/pyLAP) is optional.** `smd install hf-timestd`
> builds pyLAP automatically *if* PHaRLAP is present, and otherwise installs fine
> with the geometric fallback. PHaRLAP is licence-restricted (DST, Australia) and
> is never bundled in the repo:
> - **DASI2 golden-image** clones already carry PHaRLAP + a built pyLAP — nothing
>   to do (a venv rebuild self-heals via `hf-timestd/scripts/ensure-pylap.sh`,
>   run by its `deploy.toml` build steps). The image is licence-controlled: don't
>   share it outside the grant.
> - **General operators** supply their own PHaRLAP archive once:
>   `sudo bash /opt/git/sigmond/hf-timestd/scripts/install.sh --pharlap-zip /path/pharlap_4.7.4.zip`
>   See `hf-timestd/docs/EXTERNAL_PREREQUISITES.md` §3.

## Phase 4 — radiod config (RX888)

```bash
smd config init radiod         # writes /etc/radio/<instance>.conf  ([global]+[rx888])
# re-run with --reconfig to regenerate
```

RX888-specific gotchas this runbook hit:

- **USB 3.0 SuperSpeed is mandatory.** The RX888 streams ~129.6 Msps; on a USB
  2.0 (480 Mbps) port `rx888_usb_init` fails. Verify: `smd admin diag` flags the link
  speed, or `lsusb -t` should show the device at `5000M`. Re-seat into a blue
  USB3 port if not.
- **FX3 firmware / DFU.** A freshly-plugged RX888 enumerates in DFU mode
  (`04b4:00f3`) and needs the boot firmware loaded to become `04b4:00f1`. The
  udev rule fires `rx888_boot.service` on `ACTION=="add"`, but a device already
  attached at install time won't have triggered it — `smd install` now reloads
  and re-triggers udev (`udevadm trigger --action=add`). If stuck in DFU, replug
  or `sudo udevadm trigger --action=add --subsystem-match=usb`.
- radiod's conf is serial-locked to *this* RX888 so a second SDR can't grab the
  wrong instance.

## Phase 5 — FFT wisdom (the slow manual step)

radiod plans a large forward FFT at startup; without precomputed FFTW wisdom it
either fails or runs poorly.

```bash
smd admin wisdom plan                # generates /etc/fftw/wisdomf — can take ~30+ min
smd admin wisdom status
```

- This is **the** step that isn't automated — it's CPU-bound and long. Kick it
  off and let it run.
- **Run exactly one planner.** Two concurrent `fftwf-wisdom` processes on the
  same core thrash and neither finishes. If you see two, kill both and restart a
  single run.
- radiod's log will report `fftwf_import_system_wisdom() succeeded` once it's in
  place.

## Phase 6 — Host tuning

Most of this is applied automatically by `smd apply` during install. Verify, then
do the two manual extras.

```bash
smd admin validate                   # the board should be all checks passing
```

What `apply` already put in place (confirm via validate / the files):

- **Multicast rcvbuf** — `/etc/sysctl.d/99-sigmond-multicast.conf` sets
  `net.core.rmem_max = 64 MiB`. ka9q-python requests 8 MiB SO_RCVBUF per socket
  (kernel doubles to 16 MiB); stock Debian's ~208 KiB clamps it and *every*
  multicast subscriber drops packets. Validate rule: `kernel_rcvbuf_adequate`.
- **CPU affinity** — radiod pinned to its cores via systemd `CPUAffinity`
  drop-ins; all other services kept off them. Validate rule:
  `cpu_isolation_runtime`. (Worked example: radiod = cores 0,1; everything else
  = 2-15 on a 16-core host.)
- **Governor persistence** — `smd-cpu-governor.service` (boot oneshot) re-pins
  `performance` on the radiod cores every boot. intel_pstate boots `powersave`,
  which throttles radiod's FFT cores. Without this the governor reverted on every
  reboot. Confirm: `systemctl is-enabled smd-cpu-governor.service`.

Manual extras:

- **CPU freq caps (optional)** — `smd admin diag cpu-freq --apply` writes per-core
  `scaling_max_freq` from `[cpu_freq]` in topology.toml. Usually unnecessary:
  radiod cores default to hardware max at boot, and capping the other cores only
  throttles your decode clients. (Note: freq caps are **not** boot-persistent —
  there's no oneshot for them.)
- **`isolcpus` (optional, needs a reboot)** — kernel-level isolation of the
  radiod cores. Incremental over the affinity isolation (which already passes
  validate): it stops kernel threads/IRQs/stray tasks from *ever* touching those
  cores. Stage it in GRUB:
  ```bash
  # add to GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub, then:
  sudo update-grub
  # worked example value:  isolcpus=0,1 rcu_nocbs=0,1
  ```
  Then reboot at your convenience; confirm with `cat /proc/cmdline`. Omit
  `nohz_full` — radiod is multi-threaded (tens of threads on a couple of cores),
  so it can't benefit and may add overhead.

> **Why phase 6 matters for radiod specifically:** the governor and affinity
> determine whether radiod's real-time FFT pipeline gets uncontested, full-clock
> cores. Get this wrong and you see dropped packets and decode gaps even though
> every service is "running."

## Phase 7 — Client config (reporter ids, PSWS)

Each client owns its own config; edit it after install.

```bash
smd config edit wspr-recorder
smd config edit hf-timestd
```

Worked-example values:

- **wspr-recorder** reporter id `AC0G/SIGMA`. Reporter ids with a `/` are stored
  with `/`->`=` encoding internally (systemd-safe); use the slash form on the
  command line and in config — sigmond handles the encoding
  (`parse_user_reporter_id`). Don't reinvent escaping.
- **hf-timestd** PSWS `station_id = "S000418"`, `instrument_id = 367`, plus
  callsign/grid and the radiod status mDNS name
  (`ac0g-sigma-rx888-status.local`).

For multiple instances of one client (e.g. several reporter ids), use the
per-instance flow in [`MULTI-INSTANCE-ARCHITECTURE.md`](MULTI-INSTANCE-ARCHITECTURE.md);
new instances seed their config from the shared `/etc/<client>/config.toml`.

For which radiod channels/feeds a client subscribes to: `smd admin sources list|add|apply`.

## Phase 8 — Start + verify

```bash
smd start                      # honors start_priority — radiod first
smd status
smd admin validate                   # final board: aim for all checks passing
```

Spot-check data is actually flowing:

```bash
sudo find /dev/shm/wspr-recorder -name '*.wav' | wc -l     # WSPR WAVs in flight
sudo find /var/lib/timestd/raw_buffer -maxdepth 2 -type d  # hf-timestd Digital RF
systemctl list-timers 'grape-*' 'timestd-*'                # hf-timestd timers active
```

## Phase 9 — PSWS upload key (only if uploading to PSWS)

External, one-time, yours to do — the timers are already active and will upload
once the key is registered:

```bash
sudo bash /opt/git/sigmond/hf-timestd/scripts/setup-psws-keys.sh
# then register the printed public key with PSWS for your station_id
```

---

## `smd admin validate` — the checks that bite on a fresh host

A clean greenfield ends with every check passing (0 warnings, 0 failures).

| Check | Fails when | Fix |
|-------|-----------|-----|
| `kernel_rcvbuf_adequate` | `rmem_max` < 16 MiB | auto via `apply`; or write `99-sigmond-multicast.conf` (64 MiB) + `sysctl --load` |
| `ka9q_python_compat` | radiod build != ka9q-python pin | `sync_types.py --apply`; ensure ka9q-python is editable in the venv |
| `cpu_isolation_runtime` | something else runs on radiod cores | re-run `smd admin diag cpu-affinity --apply` |
| `channel_count` | client channel demand vs radiod | check client `sources` / radiod fragments |
| `disk_budget` | `/var` over threshold | storage trim / retention |

---

## Troubleshooting — symptoms seen bringing up the first host

Most of these are now auto-handled by `install`/`apply`; listed so you recognize
them if they recur.

| Symptom | Cause | Resolution |
|---------|-------|-----------|
| radiod `status=226/NAMESPACE` | a `ReadWritePaths` dir (e.g. `/etc/fftw`) didn't pre-exist | sigmond pre-creates it; if custom, `mkdir` it before start |
| `smd enable radiod` -> "not in catalog" | alias vs canonical name | use `ka9q-radio` (now resolved automatically) |
| RX888 stuck `04b4:00f3` (DFU) | udev didn't fire for already-attached device | `udevadm trigger --action=add --subsystem-match=usb` |
| `rx888_usb_init failed` | RX888 on USB 2.0 | move to a USB 3.0 SuperSpeed port |
| `smd start` -> "Unit not found" (a `.timer`) | timer or its target `.service` not linked | fixed — `apply` links every unit in `systemd/`; re-run `smd apply` |
| client `226/NAMESPACE` on first start | a `StateDirectory`/`ReadWritePaths` path missing (e.g. `/var/lib/hs-uploader`) | the client's `deploy.toml` mkdir step creates it; re-run install |
| `uv` editable reinstall -> EACCES on `__pycache__` | venv built root-owned, removing as non-root | reinstall as root, then `chmod -R a+rX` the venv |
| governor back to `powersave` after reboot | `smd-cpu-governor.service` not installed | `smd apply` (or `smd admin diag cpu-affinity --apply`) installs + enables it |

---

## Copy-paste skeleton (substitute your values)

```bash
# 0. bootstrap (see install-quickstart.md)
git clone https://github.com/mijahauan/sigmond ~/sigmond && cd ~/sigmond && ./install.sh

# 1-3. identity, topology, install
smd config identity                       # AC0G / EM38ww
smd enable ka9q-radio wspr-recorder hf-timestd
smd install ka9q-radio
smd install wspr-recorder
smd install hf-timestd

# 4-5. radiod config + wisdom (wisdom is slow — let it finish)
smd config init radiod
smd admin wisdom plan

# 6. verify host tuning that apply already did; stage isolcpus (reboot later)
smd admin validate
#   edit /etc/default/grub: GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=0,1 rcu_nocbs=0,1"
sudo update-grub

# 7-8. client config, start, verify
smd config edit wspr-recorder             # reporter AC0G/SIGMA
smd config edit hf-timestd                # PSWS S000418 / instrument 367
smd start
smd admin validate && smd status

# 9. PSWS key (only if uploading)
sudo bash /opt/git/sigmond/hf-timestd/scripts/setup-psws-keys.sh
```
