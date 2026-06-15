# meteor-scatter — design / next-session plan

**Status:** PHASE 1 SHIPPED (2026-06-12) — repo scaffolded at
`/opt/git/sigmond/meteor-scatter` from the psk-recorder skeleton, the
FT8/FT4 dual-mode model collapsed to a single 15 s `msk144` mode, and the
contract surface (`validate`/`inventory`/`version`, placeholder fail-fast)
verified green. jt9 binaries bundled in `bin/decoders/`. Not yet in git, no
venv built, not deployed. Phases 2-4 (jt9 decode wiring, wsprdaemon upload,
install/tests/deploy) PENDING. Upload target chosen: **wsprdaemon.org** with
shared reporter_id `AC0G=S` (operator, 2026-06-12). See the
`sigmond-meteor-scatter-plan` memory for the detailed handoff.
**Author intent (2026-06-12):** a new sigmond client that monitors meteor-scatter
activity via WSJT-X's **MSK144** protocol on the **10 m and 6 m** bands, decodes
heard "pings" with the **`jt9`** binary already used by the suite, and uploads
heard spots to **wsprdaemon.org** under the **same reporter ID as wspr/psk**.
Must follow the sigmond client-contract and the hardening patterns from the
2026-06-12 resilience sweep (`SESSION_2026-06-12_CLIENT_RESILIENCE_SWEEP.md`).

**CONFIRMED 2026-06-12 — `jt9` supports MSK144.** `jt9-x86-v27 --help`
(bundled in-repo at `wspr-recorder/bin/decoders/jt9-{x86,arm32,arm64}-v27`,
arch-resolved exactly like wspr) advertises a `--msk144  MSK144 mode` flag,
`-p/--tr-period SECONDS` (set `15` for the standard meteor-scatter T/R period),
`--sub-mode` (MSK144 Sh/short variants), and `-f/--rx-frequency HERTZ` (audio
passband offset, default 1500). Likely decode call (validate against real
decodes in the build session):
`jt9 --msk144 -p 15 -f 1500 -a <workdir> <wav>` → parse decodes from stdout /
the WSJT-X decodes file (mirror wspr's `DecoderRunner` line-diff parsing). The
binary is x86-64 and runs on `sigma`; it reads 12 kHz `*.wav` files, the same
WAV format wspr/psk already produce.

---

## 1. What MSK144 / meteor scatter is (and why it shapes the design)

Meteor trails ionize for **a few milliseconds to ~2 seconds** ("pings"),
opening ultra-short propagation windows. MSK144 (which replaced the older,
un-FEC'd FSK441) is engineered for that regime: short **72 ms** frames, the same
message transmitted **repeatedly** through a T/R period, and aggressive LDPC FEC
so a single ping can carry a full decode. Default T/R sequence length is **15 s**
(also 5 s/10 s short variants; "Sh" short-hashed messages exist for established
contacts). Conventional WSJT-X dial frequencies (USB):

| Band | MSK144 dial freq |
|------|------------------|
| 10 m | **28.130 MHz** |
| 6 m  | **50.260 MHz** |

**Design implication:** this is a *monitoring/reporting* use, not a real-time
QSO. The pragmatic architecture is **slotted, 15 s, T/R-aligned** WAVs decoded
once per period — `jt9` in MSK144 mode finds *all* pings in the window and we
report each as a spot. That reuses the existing slotted recorder architecture
almost directly; we do NOT need WSJT-X's continuous within-period re-decode loop
for a monitor.

## 2. Closest templates to clone

- **psk-recorder** — the closest structural match: per-channel `SlotWorker`,
  process-local `Ring`, 15 s cadence, fork-a-decoder-per-slot, in-process
  uploader, full contract surface, and **all the 2026-06-12 hardening already
  applied** (placeholder fail-fast, decode-timeout, thread supervision,
  progress-tied watchdog). Start here.
- **wspr-recorder** — borrow its `jt9` invocation + `_resolve_decoder_binaries`
  arch-resolution, its `ka9q-python MultiStream` ingest, and its
  **hs-uploader → wsprdaemon.org** transport (`WsprUploaderHs` /
  `WsprdaemonTarSftp`), which is the upload target the operator specified.

Net: **psk's skeleton + wspr's jt9 decode and wsprdaemon upload.**

## 3. Architecture sketch

```
radiod (10 m @ 28.130 MHz, 6 m @ 50.260 MHz channels, USB, 12 kHz)
   │  ka9q-python MultiStream (shared socket per mcast group)   [reuse wspr/psk]
   ▼
per-channel Ring (float32) ──► SlotWorker @ 15 s T/R cadence    [psk pattern]
   │   at slot boundary: write 15 s WAV (atomic .tmp→rename)
   ▼
fork jt9 (MSK144 mode) on the WAV, bounded by a decode timeout  [psk decode-timeout]
   │   parse ping decodes → MSK144 spots (call, grid, freq, dt, snr, ts)
   ▼
in-process hs-uploader pump → wsprdaemon.org (SFTP)             [wspr WsprUploaderHs]
   │   reporter_id == the wspr/psk reporter identity (shared)
   ▼
(optional) sigmond SQLite sink rows via hamsci_sink, single writer thread
```

Apply, verbatim, the sweep checklist: placeholder fail-fast (`exit 78` +
`RestartPreventExitStatus=78`), `Type=notify` + `_ProgressGate` watchdog
(heartbeat = RTP samples received, the signal-independent counter — meteor pings
are rare, so do NOT tie liveness to decode output), `_supervise` on every
background thread, jt9 subprocess timeout + pipe draining + reaping, graceful
SIGTERM shutdown, `OnFailure=` on the unit.

## 4. Contract-surface checklist (sigmond CLIENT-CONTRACT.md)

Mirror psk/wspr (both at contract v0.7): native TOML config (§1), radiod-id
binding (§2), self-describe `inventory`/`validate`/`version --json` (§3),
templated `meteor-scatter@<id>.service` `Type=notify` (§4), `deploy.toml`
manifest (§5), ka9q-python with destination read from `ChannelInfo` (§6/§7),
`RADIOD_<id>_CHAIN_DELAY_NS` (§8), `log_paths` + log-level env (§10/§11),
validate hardening incl. SSRC uniqueness + the placeholder check (§12), control
surface (§13), `config init`/`edit` via `sigmond.wizard_dispatch` (§14), output
sinks in inventory (§17), optional §18 timing-authority subscriber.

## 5. OPEN QUESTIONS — resolve before/early in the build session

1. **6 m hardware path.** ✅ RESOLVED (2026-06-12) — sigma's radiod already
   advertises 6 m: psk-recorder records FT8 @ 50.313 MHz and FT4 @ 50.318 MHz
   on this station today, proving the RX888 path covers the 6 m band. MSK144's
   50.260 MHz sits in that same band, so no extra hardware/tuner work is
   needed. (10 m @ 28.130 MHz is the RX888 HF path.) radiod status name =
   `sigma-rx888mk2-status.local`.
2. **`jt9` MSK144 invocation + output.** ✅ RESOLVED (2026-06-12) — ran
   `jt9 --msk144 -p 15 -f 1500 -a <workdir> <wav>` (cwd=workdir, touch
   `plotspec`+`decdata`): exits 0, writes decode lines to **`decoded.txt`** in
   the workdir, and prints a **`<DecodeFinished> <ndecodes> …`** sentinel to
   stdout. So Phase 2's decode loop mirrors `wspr_recorder/decoder.py`'s
   `decode_fst4w` but reads `decoded.txt` deltas (NOT stdout, NOT
   `fst4_decodes.dat`). jt9 reports the AUDIO offset frequency; add the
   channel dial freq for absolute RF. **Remaining sub-task:** validate the
   exact `decoded.txt` column layout against a *real* MSK144 decode (expected
   `HHMMSS snr dt freq & message`, `&` = MSK144 sync char) — no real ping WAV
   is available pre-deploy, so confirm it live.
3. **wsprdaemon.org MSK144 ingest.** wsprdaemon is WSPR/FST4W-centric; confirm
   with **Rob (wsprdaemon.org)** whether it accepts MSK144 meteor-scatter spots,
   and in what format (does `hs-uploader` need a new MSK144 transport, or does an
   existing spots subtree fit?). Fallback target for WSJT-X-mode spots is
   **PSKReporter**, which natively accepts MSK144 — decide which the operator
   actually wants.
4. **Decode cadence / WAV length.** Confirm 15 s T/R-aligned slots match the
   monitored stations' sequence length (operators may run 15 s). Decide whether
   to also handle 5 s/10 s short periods.
5. **Spot shape + dedup.** Define the MSK144 spot row (call, grid, freq_hz, dt,
   snr/“ping strength”, ts, reporter_id, band) and cross-period dedup (the same
   station pings repeatedly).

## 6. Proposed repo

`/opt/git/sigmond/meteor-scatter` (suggested name — protocol-specific, matching
`wspr-recorder`/`psk-recorder`). Service user `meteorscat` (or per install.sh
convention). Templated unit `meteor-scatter@<reporter-id>.service`. Reuse the
sibling-editable `[tool.uv.sources]` pattern for `ka9q-python`, `callhash`,
`hs-uploader`.

## 7. Suggested phased plan for the build session

- **Phase 0** — resolve Open Questions 1–3 (hardware, jt9 CLI, upload target);
  these gate the design.
- **Phase 1** — scaffold the repo from psk-recorder; rename, strip FT4/FT8,
  wire the two MSK144 bands; get `inventory`/`validate`/`version` green and the
  placeholder fail-fast + watchdog in place from day one.
- **Phase 2** — record → 15 s WAV → `jt9` MSK144 decode → parse pings (mirror
  wspr's `DecoderRunner`, bounded by the decode-timeout pattern).
- **Phase 3** — upload heard spots to the confirmed target with the shared
  reporter_id (wspr `hs-uploader` transport).
- **Phase 4** — hardening pass against the sweep checklist + tests
  (`test_watchdog_gate`, decode-timeout, supervisor), then live deploy + monitor.
