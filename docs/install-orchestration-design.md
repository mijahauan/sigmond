# Install orchestration design — TUI-driven, contract-ordered bring-up

**Status:** Design (proposed). Captures the agreed model for turning a bare host
into a running DASI2 station with `./install.sh` as the only CLI and the sigmond
TUI driving everything after. Phases A–C implemented (2026-06-06); see roadmap.

Companion to the [greenfield runbook](greenfield-runbook.md) (the manual
procedure this automates) and the [client contract](CLIENT-CONTRACT.md)
(the per-client config machinery this orchestrates).

---

## 1. Goal / end-state

- `./install.sh` is the **only** CLI the operator types. It self-installs
  sigmond plus the pure-python substrate, then hands off to the TUI.
- The **sigmond TUI** drives the rest — identity, install, per-client config,
  start, validate — organized around the install process, with Installation
  leading the nav and a greenfield-aware landing.
- The **DASI2 core set** (hf-timestd, wspr-recorder, psk-recorder, mag-recorder
  + their infra) installs in one selection.
- **Every client remains standalone.** Sigmond orchestrates; it never writes
  inside a client's config (contract §1). Each client's own configurator
  produces a config that runs without sigmond oversight.

## 2. Design principles

1. **Reuse, don't reimplement.** The contract machinery already exists — drive
   it, don't rebuild it (§4).
2. **Clients own their config UX** (§14). Sigmond passes cross-client commons as
   *advisory* defaults via the env-var bag and invokes each client's
   `--non-interactive` configurator.
3. **Dependency-ordered + conditional.** The pipeline branches on local-vs-remote
   radiod and treats independent (§16) clients as a parallel track.
4. **Checkpoints punctuate the path** so failures surface early, not at the end.
5. **Overlap the slow step.** FFT wisdom runs in the background while later
   configs proceed; only the *start* of local-radiod-bound services waits on it.

## 3. Dependency model (authoritative — from catalog + contract)

| Component | Hard `requires` | radiod-bound? | Role |
|---|---|---|---|
| ka9q-radio (radiod) | — | — | Foundation; `start_priority=0` |
| hf-timestd | ka9q-python, ka9q-radio | yes | Client **and** §18 timing authority |
| wspr-recorder | ka9q-python, ka9q-radio | yes | + callhash (sibling); soft-binds hf-timestd timing |
| psk-recorder | ka9q-python, ka9q-radio | yes | + callhash (sibling); soft-binds hf-timestd timing |
| mag-recorder | hs-uploader | **NO** | §16 independent — runs with no radiod |
| ka9q-web | ka9q-radio | yes | Optional UI (`prio=200`) |
| callhash, hs-uploader | — | — | Pure-python libraries (auto-pulled as siblings) |
| igmp-querier, gpsdo-monitor | — | — | `kind=infra` — tied to a *local* radiod segment |
| ka9q-update | — | — | `kind=infra` — updater **tool**, not a runtime component |

Key distinctions that shape the pipeline:

- **mag-recorder is radiod-independent.** It must not wait on radiod or wisdom.
- **hf-timestd is a timing authority** for wspr/psk — a *soft* dependency:
  consumers fall back to RTP-default mode (§18) if it is absent, so the
  orchestrator must not hard-block on it.
- **igmp-querier / gpsdo-monitor are local-radiod infra**, not universal base:
  igmp-querier keeps multicast alive on the radiod segment; gpsdo-monitor
  watches the GPSDO disciplining radiod's sample clock. A remote-radiod host
  needs neither (igmp-querier only if its own segment requires it).
- **The libraries are already auto-pulled** by their dependent clients
  (callhash by wspr/psk, hs-uploader by mag) as sibling editable deps.
  Front-loading them in `install.sh` is robustness (avoids the documented
  mid-install callhash clone failure), not a new hard requirement.

## 4. What already exists (reused, not built)

| Building block | Where |
|---|---|
| Per-client config interview with `--non-interactive` | psk/mag `config-wizard.sh`, hf-timestd `config-review.sh` |
| `smd config init/edit <client> [<instance>]` invocation surface | contract §14.2 |
| Cross-client commons env bag (`STATION_CALL`, `SIGMOND_RADIOD_STATUS`, `SIGMOND_TIME_SOURCE`, …) | contract §14.3 |
| Local-vs-remote radiod model | `coordination.toml [radiod.<id>] host/status_dns` |
| Background wisdom | `sigmond-wisdom.service` (oneshot, `Condition=!/etc/fftw/wisdomf`) |
| Host tuning auto-applied | `cmd_apply`: affinity drop-ins, governor service, rmem drop-in |
| Catalog dependency graph + lifecycle | `catalog.toml` `requires`/`uses`, `start_priority` |

This reframes the work as **orchestration + TUI-front + small additions**, not a
redesign.

## 5. The bring-up pipeline

```
Stage 0  SELF-INSTALL (install.sh)   sigmond + libraries: ka9q-python, callhash, hs-uploader
                                     -- checkpoint: substrate importable --
Stage 1  LOCAL radiod? -- remote --> clients bind remote status_dns; NO wisdom, NO gpsdo;
         |                            igmp-querier only if this segment needs it
         +- local:
            infra:   igmp-querier, gpsdo-monitor          (network + GPSDO discipline)
            runtime: ka9q-radio (+ ka9q-web optional; ka9q-update = updater tool only)
            tuning:  affinity + governor + rmem           (auto via apply)
            config:  ka9q-radio (RX888)
            > background: sigmond-wisdom.service ----------+
                                     -- checkpoint: radiod configured --   | overlaps Stage 2-3a
Stage 2  hf-timestd  (radiod-bound; TIMING AUTHORITY provider)            |
                                     -- checkpoint: GRAPE recording --      |
Stage 3a wspr-recorder, psk-recorder (radiod-bound; soft-bind hf-timestd) |
                                     -- checkpoint: each validates --       |
Stage 3b mag-recorder  <-- INDEPENDENT track: no radiod, no wisdom, runs anytime after Stage 0
                                     -- checkpoint: mag validates --
Stage 4  START   radiod-bound services gated on wisdom(local)/reachability(remote);
                 order: igmp-querier -> radiod -> hf-timestd -> wspr/psk;  mag-recorder independent
                                     -- final: smd validate all green --
```

### Conditional branches
- **Local vs remote radiod:** detected (topology `ka9q-radio` enabled -> local;
  else a remote `[radiod.<id>]` / mDNS discovery) and **confirmed** with the
  operator. Remote skips the entire radiod stack, gpsdo-monitor, and wisdom.
- **Independent clients (§16):** mag-recorder runs on the 3b track regardless of
  radiod state — never gated on radiod or wisdom.
- **Wisdom gating:** kicked async at Stage 1; gates only the *start* of
  local-radiod-bound services (Stage 4), never config. Remote-radiod hosts never
  wait.

## 6. Checkpoints

| After | Verifies | Policy |
|---|---|---|
| Stage 0 | substrate importable (`ka9q_python`, callhash, hs-uploader) | **hard-stop** |
| Stage 1 | radiod configured; tuning applied; wisdom launched | **hard-stop** |
| Stage 2 | hf-timestd recording GRAPE; timing endpoint advertised | advisory |
| Stage 3a/b | each client passes its own `validate` / self-describe | advisory |
| Stage 4 | `smd validate` board all green | advisory (report) |

## 7. New components to build

1. **`install.sh` substrate extension** — install the pure-python libraries
   (ka9q-python already; add callhash, hs-uploader) so the substrate exists
   before any client install.
2. **Catalog `[profile.<name>]` schema** — e.g.
   ```toml
   [profile.dasi2]
   description        = "DASI2 grant core station"
   clients            = ["hf-timestd", "wspr-recorder", "psk-recorder", "mag-recorder"]
   local_radiod_infra = ["igmp-querier", "gpsdo-monitor"]
   optional           = ["ka9q-web"]
   ```
   Libraries are implicit (auto-pulled). Other grants/sites add their own
   profiles.
3. **Bring-up orchestration engine** — a headless stage runner
   (`smd bringup [--profile <name>] [--remote-radiod <status_dns>]`) implementing
   Stage 0-4 with the conditional branches, background wisdom, and checkpoints.
   Pure logic so it is testable and gives a CLI fallback.
4. **TUI guided-flow front** — leads the nav, greenfield-aware landing, drives
   the engine and the per-client `--non-interactive` configurators; surfaces
   checkpoint results inline.
5. **Start-ordering fixes** — `start_priority` currently only set on radiod (0)
   and the §200 group, so nothing orders infra-before-subscribers or
   authority-before-consumers. Proposed:
   | Component | `start_priority` |
   |---|---|
   | igmp-querier, gpsdo-monitor, ka9q-radio | 0 |
   | hf-timestd | 50 (after radiod, before consumers) |
   | wspr-recorder, psk-recorder | 100 (default) |
   | ka9q-web, mag-recorder, codar-sounder | 200 |

## 8. Proposed design decisions

| # | Decision | Proposed resolution |
|---|---|---|
| 1 | Orchestration home | Headless `smd bringup` engine; TUI drives it (testable, CLI fallback) |
| 2 | Local-vs-remote radiod | Auto-detect + confirm with operator |
| 3 | install.sh infra scope | Libraries unconditional; igmp-querier/gpsdo-monitor in local-radiod stage (default-on/opt-out) |
| 4 | Profile representation | `[profile.<name>]` blocks in `catalog.toml` |
| 5 | Checkpoint policy | Stage 0 + Stage 1 hard-stop; rest advisory |

## 9. Phased implementation roadmap

- **Phase A (DONE, commit `c1d47e1`):** `install.sh` substrate (callhash,
  hs-uploader); `[profile.<name>]` schema + `[profile.dasi2]` + `load_profiles()`;
  `smd install --profile`; nav reorder so Installation leads + greenfield-aware
  landing + profile-driven one-shot install buttons; start-ordering fixes
  (igmp/gpsdo=0, hf-timestd=50).
- **Phase B (DONE, commit `f31fa85`):** `lib/sigmond/bringup.py` pure plan
  builder + `smd bringup --profile [--remote-radiod] [--with-optional] [--dry-run]`
  executor (local/remote branch, background wisdom + wisdom-gated start,
  checkpoint probes); TUI greenfield landing offers per-profile guided bring-up
  buttons (suspend → `smd bringup`); `tests/test_bringup.py` (6 tests).
- **Phase C (DONE, sigmond `33d53ba` + hf-timestd `6619100`):** `smd config
  init/edit` forwards `--non-interactive` to the client entry point; `smd bringup
  --non-interactive` threads it through every config step; checkpoint probes use
  the client's `inventory --json` self-describe; hf-timestd's setup-station.sh
  gained `--non-interactive`.  §14 commons env bag was already wired.  All four
  DASI2 clients now drive non-interactively.

## 10. Validation status

Exercised on the AC0G / `sigma` host (local radiod enabled) on 2026-06-06.

**Plan builder — unit tests** (`tests/test_bringup.py`, 7 tests, all pass):
local vs remote branches, stage assignment (hf-timestd→Stage 2, mag→3b),
the single hard checkpoint, the `--with-optional` toggle, the final validate,
and `--non-interactive` appending the flag to config steps.

**`smd bringup` dry-run — all flag paths exercised on the host:**

| Invocation | Observed plan |
|---|---|
| `--profile dasi2 --dry-run` | LOCAL detected; full Stage 1 radiod stack (igmp/gpsdo/ka9q-radio → tuning → configure radiod → background wisdom → HARD `radiod configured`); Stage 2 hf-timestd; 3a wspr/psk; 3b mag; Stage 4 ⏳ wait-wisdom → start → validate |
| `--remote-radiod bee3-status.local --dry-run` | REMOTE; Stage 1 collapses to a single note (no radiod stack, gpsdo, or wisdom); no hard gate; Stage 4 has **no** wisdom wait; client stages unchanged |
| `--non-interactive --dry-run` | every `configure` step tagged `(non-interactive)`; install/tune/start/checkpoint steps unchanged |
| `--with-optional --dry-run` | `install ka9q-web` added to Stage 1 (no configure step — it has no `[contract.config]`) |

**Other:** `smd validate` remains 11/0/0 after all Phase A–C changes.
hf-timestd's `setup-station.sh --non-interactive` was run for real against a
temp `--config` path and produced a complete config with callsign / grid /
location / status populated from the §14 env bag.

**Not yet validated:** a real (non-dry) end-to-end `smd bringup` — the actual
installs, config interviews, the ~30-min FFT-wisdom wait, and checkpoint probes
have only been dry-run / unit-tested. Best run on a fresh or test host, not the
live station. The TUI suspend→`smd bringup` path also needs a TTY to exercise
live.

## 11. Open questions / future

- Remote-radiod discovery: mDNS browse for `*-status.local` vs operator-entered
  `status_dns`.
- Multi-radiod hosts (`SIGMOND_RADIOD_COUNT > 1`): per-instance reporter suffixes
  already covered by §19 — the engine just iterates instances.
- Non-DASI2 profiles (other grants) drop in as additional `[profile.*]` blocks
  with no engine changes.
