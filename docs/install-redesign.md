# Install redesign — station patterns, hardware-aware install, 3-step IA

_Status (2026-06-23): **Stages 0–3 shipped.** Stages 0+1 (canonical dasi2,
hostname-derived radiod name, hardware-aware install) landed earlier (`3406557`,
`9c4fc26`). The multi-site deployment-readiness pass committed the remaining
direction: **install now implies enable** (the vocabulary fix — see below), the
**core/discretionary split is encoded** (`[profile.dasi2].optional`), the
**Guided bring-up screen carries the optional-clients picker** (`--with-optional`),
and the **Installation nav was collapsed to the ①②③ arc + Advanced group**
(Stage 3). Extends `install-orchestration-design.md`, `RADIOD-IDENTIFICATION.md`,
and the nav proposal in `TUI-FUNCTION-INVENTORY.md` §4._

> ✅ **Direction confirmed & shipped.** The vocabulary question — *why are
> `install` and `enable` separate?* — resolved to: they are not, for the common
> path. `smd install <name>` sets `enabled = true` in topology by default
> (`--no-enable` opts out); `enable`/`disable` remain the reversible runtime
> toggle for an already-installed component (disable-not-delete). The mental
> model is now **download → install → configure → start**. The `base`/`client`
> guided assistants remain TUI flows composing the existing Install / Configure /
> Lifecycle screens (the dasi2 Guided bring-up screen is the reference); no CLI
> entry point — the CLI equivalent is the documented install → config → start
> sequence.

## 0. Why

The Install surface drifted from the project's actual mental model:

- **"daisy" is not a project term.** It was invented as the name for "the
  basic station," with `dasi2` framed as "daisy + magnetometer." That's
  backwards: **dasi2 is the canonical deployment**, and the magnetometer-less
  case is a *degraded* state, not a separate product.
- The Installation nav grew to 9 leaves, mixing the real build sequence with
  "under the hood" tuning (CPU affinity/freq, FFT wisdom) and admin info
  (Software versions) that a station builder never needs to see.
- radiod is treated as needing operator configuration when its *only*
  functional input — the mDNS status name — can be derived from the hostname.

This redesign realigns the surface with the build sequence and removes input
the system can supply itself.

## 1. Decisions (locked)

1. **Delete "daisy" everywhere.** `dasi2` is the official canonical station
   and the default for a bare `smd bringup`.
2. **Three install patterns, not N static profiles:**
   - **dasi2** — turnkey canonical bundle (fixed target set).
   - **base** — *hardware-detected* local station (install what's plugged in,
     then add clients).
   - **client** — *remote-radiod* decode-client picker (no local SDR).
3. **Hardware-aware install with dependency-aware warnings** (rx888 / GPSDO /
   magnetometer), using the existing `hardware_detect.py` probes.
4. **Installation nav collapses to 3 sequential steps**: ① download & install ·
   ② configure · ③ enable/disable & start/stop. "Topology" stops being an
   action; it is the *derived* list of what's prepared to run here. Tuning /
   admin leaves move out of Installation.
5. **radiod status name is auto-composed from the hostname**:
   `<short-hostname>-status.local` — zero operator input. radiod therefore
   needs no Configure step.

### Assumption made explicit
In the sigmond context radiod is **always** driven by an RX888. ka9q-radio
supports other SDRs, but sigmond does not use them with radiod today, and
non-radiod receivers (KiwiSDR, OpenHPSDR) don't go through radiod at all.
Therefore **one radiod per host**, and the status name needs no SDR-model
disambiguator (drop the `-rx888mk2` suffix seen on `sigma`/`B4-100`). Multi-
radiod-per-host becomes a manual/advanced exception, not something the guided
flow produces.

## 2. The three install patterns

### dasi2 — official canonical station (turnkey)
Fixed target set (unchanged from today's `[profile.dasi2]`):

| layer | components |
|-------|-----------|
| foundation | ka9q-radio |
| local-radiod infra | igmp-querier, gpsdo-monitor, ka9q-web |
| clients | hf-timestd (grape), wspr-recorder, psk-recorder, **mag-recorder** |

Detection drives **warnings, not the target set** — the bundle is fixed; if a
piece of hardware is absent we alert + warn of lost functionality (§3) but the
canonical station is still the goal.

### base — hardware-detected local station
Not a static client list. The flow:

1. **Probe the USB bus** (`hardware_detect.py`).
2. **Install the foundation that matches what's present:**
   - rx888 on the bus → ka9q-radio + ka9q-web (+ igmp-querier — required for
     multicast).
   - Leo Bodnar GPSDO present → gpsdo-monitor.
   - magnetometer present → mag-recorder.
3. **Let the operator pick the additional clients** they want (wspr-recorder,
   psk-recorder, hf-timestd, hfdl-recorder, codar-sounder, hf-tec).
4. Configure → enable → start the chosen set.

### client — remote-radiod decode clients
No local SDR. The flow:

1. Ask for the **remote radiod status DNS** (e.g. `bee3-status.local`).
2. **Let the operator choose** which decode clients to install.
3. Configure each against the remote radiod → enable → start.

> base and client are **guided assistants**, closer to the greenfield wizard
> than to a one-shot `smd install --profile`. They *compose* existing
> machinery (install → config → enable → start), they don't add new engines.

## 3. Hardware-aware install + dependency warnings

Single "kit check" at install time, driven by `hardware_detect.py`
(`_probe_sdr`, `_probe_magnetometer`, `_probe_gpsdo` → `Presence`). Replaces
today's scattered abort/skip/nudge logic in `bin/smd` bringup.

Dependency model to encode in the warnings:

| missing hardware | functional consequence | install action |
|------------------|------------------------|----------------|
| **rx888** | no radiod → **nothing decodes** (wspr/psk/grape all dark) | software installs, station can't run until SDR attached; loud alert |
| **GPSDO** | rx888 is **undisciplined** → wspr/psk/grape frequency accuracy degraded (NOT just "gpsdo-monitor missing") | install proceeds; warn |
| **magnetometer** | **magnetometer uploads won't work** | install proceeds; warn |

Key correction vs today: GPSDO absence currently only "skips gpsdo-monitor."
It must instead warn that **the recorders lose frequency discipline**, because
grape/wspr/psk depend on a gpsdo-disciplined rx888.

**DECIDED:** on a **dasi2** install, absent-hardware components install
**enabled-but-dormant** — they light up when the hardware is later plugged in
(uses the existing `harmonize.dormant_reason` gating). **base** is
**detection-gated**: absent hardware → don't install that component. _This host
has no magnetometer, so it's a live test of the dasi2 mag-dormant path._

**IMPLEMENTED (Stage 1):**
- `smd start` is now **dormancy-aware** — it skips an enabled hardware-gated
  component (only mag-recorder / gpsdo-monitor exist) whose device is
  *confidently* absent (`dormant_reason`, fires on `False` only; unknown still
  starts). Same source of truth as `smd validate` / status surfaces, now
  extended to the start path. radiod is NOT gated, so never affected.
- bringup no longer **skips** mag/gpsdo when absent — it keeps them in the plan
  (install + configure + enable) so they install dormant, and emits the
  dependency-aware warnings. Their "configured" checkpoint is **soft**
  (`build_plan(dormant=…)`) so a config step that can't finish without the
  device doesn't abort the bring-up.
- **rx888 during bringup:** kept as a hard **abort** (a turnkey bringup
  install+starts a station; with no SDR radiod can't run and the start phase
  would hang). The "install software now, attach SDR later" semantics belong to
  the **base** install-only assistant (Stage 2), not the dasi2 bringup.

## 4. radiod auto-naming

**Rule:** `radiod status DNS = "<short-hostname>-status.local"`, where
`short-hostname = socket.gethostname().split('.')[0]`. Composed automatically;
never prompted.

- Consistent with `RADIOD-IDENTIFICATION.md` (the mDNS control/status name is
  the only functional ID; `bee1-status.local` is already exactly this form).
- Centralize in the existing canonical-status helper (`bin/smd:8351`).
  `smd config register-radiod` and the bringup path call it instead of
  prompting or seeding the `<configure-via-config-init>` placeholder (ties off
  the placeholder class of bug fixed in `dd6ec9b`).
- Consequence: **radiod drops out of the Configure step.** With antenna
  defaults already applied by the greenfield work, radiod needs nothing from
  the operator. Configure shrinks to **station identity (reporter id + grid,
  collected once)** plus the clients that need per-instance input.

## 5. Installation information architecture

The nav mirrors the build sequence: download → build/install → configure
(+ instances) → enable → start, consolidated to **three steps**.

```
  BEFORE (Installation, 9 leaves)        AFTER (Installation, 3 steps)
  ──────────────────────────────         ─────────────────────────────
  ✨ Guided bring-up  ┐
  ➕ Install          ┴────────────────▶ ① Download & install
                                            (pattern: dasi2 / base / client,
                                             or individual components)
  ⚙ Configuration  ───────────────────▶ ② Configure
                                            (station identity + client
                                             instances; radiod = zero-touch, §4)
  ☰ Topology          ┐
  (Lifecycle, today    ┴───────────────▶ ③ Enable/disable & start/stop
   under Maintenance)                       (topology = this view's
                                             "prepared to run" list)

  ⚑ Software versions ┐  move OUT of Installation → "Advanced / Under-the-hood"
  ⊞ SDR inventory     │  (Software versions = admin info; SDR inventory + CPU +
  ⚙ CPU affinity      ├─ FFT = things the operator normally never touches.
  ⇵ CPU frequency     │   SDR inventory is Advanced, NOT folded into Configure.)
  ⨉ FFT Wisdom        ┘
```

- **Topology** is no longer a leaf; it is the derived state surfaced by step ③.
- **Lifecycle** (currently under *Maintenance*) becomes step ③.
- Reconcile with `TUI-FUNCTION-INVENTORY.md` §4's 4-way reorg — this refines
  the Installation column of that proposal.

**DECIDED:** one **"Advanced / Under-the-hood"** group holds the displaced
leaves — Software versions, **SDR inventory**, CPU affinity, CPU frequency,
FFT wisdom. SDR inventory is Advanced, NOT folded into Configure.

## 6. Phased roadmap

**Stage 0 — vocabulary + radiod auto-naming — DONE, shipped `3406557`**
- Remove `[profile.daisy]`; default bare `smd bringup` → `dasi2`.
- Purge "daisy" from `bin/smd` (default resolution ~2672, help 12852/12856,
  bash completion 6408, the dasi2-nudge 2723-2725), `greenfield.py`
  (61/63/150/190), `app.py` (684-685).
- Implement `<short-hostname>-status.local` auto-derivation in the canonical
  helper; wire `register-radiod` + bringup to it; drop the placeholder/prompt.
- Update `RADIOD-IDENTIFICATION.md` (auto-derivation) + catalog comment.
- Tests: profile default, status-name derivation, no-daisy guard.

**Stage 1 — hardware-aware install + dependency warnings — DONE, shipped `9c4fc26`**
- Dependency-aware messages (§3); dormancy-aware `smd start`; bringup
  install-dormant with soft checkpoints. Tests: `test_bringup` +dormant,
  `test_radiod_config` unchanged. Verified live via `smd bringup dasi2
  --dry-run` on this host (mag + GPSDO both absent → both warnings, both
  install dormant; rx888 present → proceeds).

**Stage 2 — base & client guided assistants — SHIPPED (core)**
- The Guided bring-up screen (`tui/screens/greenfield.py`) is the reference
  assistant: pick a profile (dasi2/base/client), enter identity once, preview
  the staged plan, Begin. It composes install → configure → enable → start via
  the `bringup` plan engine (`lib/sigmond/bringup.py`).
- **Optional-clients picker shipped**: profiles that declare `optional` show an
  "Optional clients" section; checking it passes `--with-optional` so the
  discretionary set (codar-sounder, hf-tec, hfdl-recorder, meteor-scatter for
  dasi2) installs alongside the core. A single discretionary client is added
  later with `smd install <name>` (installs + enables).
- base/client stay assistant-driven (no CLI entry point); the static
  `[profile.dasi2]` remains the one canonical bundle.

**Stage 3 — Installation IA reorg (nav) — SHIPPED**
- Installation collapsed to **✨ Guided bring-up · ① Download & install ·
  ② Configure · ③ Enable / start / stop** (Lifecycle moved up from Maintenance).
- **Topology leaf removed** — topology is derived state, surfaced by step ③.
- The under-the-hood five — Software versions, **SDR inventory**, CPU affinity,
  CPU frequency, FFT wisdom — moved into one **Advanced** group (SDR inventory
  stays Advanced, NOT folded into Configure — see §5).
- Implemented in `tui/widgets/component_tree.py`; pinned by
  `tests/test_tui_navigation.py::test_installation_is_the_three_step_arc`.
- TODO: reconcile the prose in `TUI-FUNCTION-INVENTORY.md` §4 with the shipped tree.

## 7. Decisions resolved + remaining
- ✓ dasi2 absent-hardware → **install-dormant**; base → detection-gated (§3).
- ✓ Displaced leaves → one **Advanced** group, incl. SDR inventory (§5).
- ✓ base/client are **TUI-only assistants** — no CLI entry point. The CLI
  equivalent is the documented install → config → enable → start sequence.
- OPEN (resolve at Stage 2): base/client "client picker" UI — new widget vs
  reuse Topology-enable + Install selection chrome.
