# sigmond function inventory + TUI alignment audit

Snapshot of every capability sigmond exposes, mapped to the CLI verbs
that drive it and the TUI screens that surface it. Produced as
preparation for re-shaping the TUI left-tree navigation around four
operator mental-model categories (Installation / Maintenance /
Debugging / Routine monitoring) instead of the three current ones
(Configure / Observe / Operate).

Source state: commit `01d6bb7` (2026-05-24).

---

## 1. CLI verb surface (`bin/smd`)

Top-level surface after the `admin`-umbrella rework (CLI-V2-SPEC §9):
a small daily core plus one `admin` umbrella holding the occasional /
diagnostic / maintenance verbs. Sub-groups indented.

### Core (top-level)

| Verb | Subverbs | Handler | Mutating? |
|---|---|---|---|
| `apply` | | `cmd_apply` | yes |
| `bringup` | `--profile <name>` | `cmd_bringup` | yes |
| `component` | `list / install / update / add / remove / enable / disable` | `cmd_component_*` | mixed |
| `config` | `show / identity / refresh / migrate / backup / restore / init / edit` | `cmd_config_*` | mixed (identity/refresh/init/edit/restore mutate) |
| `disable` | | `cmd_disable` | yes |
| `enable` | | `cmd_enable` | yes |
| `install` | (alias for `component install`) | `cmd_install` | yes |
| `list` | (alias for `component list`) | `cmd_list` | optional |
| `reload` | (`--via=auto\|systemd\|socket`) | `cmd_reload` | yes |
| `restart` | | `cmd_restart` | yes |
| `start` | | `cmd_start` | yes |
| `status` | | `cmd_status` | no |
| `stop` | | `cmd_stop` | yes |
| `tui` | | `cmd_tui` | n/a |
| `watch` | `wspr / psk / hfdl / codar / hf-gps-tec / mag / ka9q / radiod / uploads / verifier` | dispatches into the matching `cmd_*_watch` | no |

### `admin` umbrella (`smd admin <subverb>`)

| Subverb | Sub-subverbs | Handler | Mutating? |
|---|---|---|---|
| `diag` | `cpu-affinity / cpu-freq / net` | `cmd_diag*` | no (`--apply` opt-in) |
| `validate` | | `cmd_validate` | no |
| `verifier` | `report [--target wspr\|psk] / rehabilitate` | `cmd_verifier_*` | rehabilitate mutates |
| `wisdom` | `plan / status` | `cmd_wisdom_*` | plan mutates |
| `storage` | `migrate-to-sqlite / trim / tune-timestd` | `cmd_storage_*` | yes |
| `environment` | `list / probe / describe` | `cmd_environment_*` | no |
| `sources` | `list / add / remove / apply` | `cmd_sources_*` | yes |
| `public-ip` | | `cmd_public_ip` | no |
| `log` | `set-level` | `cmd_log` | optional (`set-level` writes) |
| `rac` | `status / start / stop / restart / install` | (rac dispatch) | yes |
| `timing` | `status / reconcile` | `cmd_timing` | reconcile mutates |
| `radiod` | `migrate` | (radiod dispatch) | yes |
| `instance` | `list / show / add / remove / edit / enable / disable / migrate` | (instance dispatch) | mixed |
| `uninstall` | | `cmd_uninstall` | yes |
| `completion` | `bash` | `cmd_completion` | no |

Notes / leftovers:

- The `_MUTATING` set in `main()` includes `'update'` but there is
  no `update` subparser. Dead entry — safe to remove.
- **Removed in the `admin` rework (hard, no shim):** `cpu`
  (→ `admin diag cpu-freq`), `add`/`remove` (→ `component add/remove`),
  `software` (→ `install`/`apply`), `timestd-tune-storage`
  (→ `admin storage tune-timestd`). See CLI-V2-SPEC §9.
- The legacy single-target watch verbs (`psk-watch`, `wspr-watch`,
  `hfdl-watch`, `codar-watch`, `mag-watch`, `ka9q-watch`) were removed
  earlier; all watchers are reached via `watch <target>`.

---

## 2. TUI screen surface (`lib/sigmond/tui/screens/`)

31 screen modules. Each maps to exactly one `action_show_*` in
`lib/sigmond/tui/app.py`. `action_show_update` is a kept-for-back-compat
alias that re-dispatches to `action_show_components`, so it doesn't
warrant a separate row.

| Screen module | Action | One-line role |
|---|---|---|
| `activity` | `action_show_activity` | Live tail of `smd watch <target>` for wspr / psk / hfdl / codar / ka9q / uploads / verifier; one subprocess per screen, Stop/Clear, Start re-targets |
| `annotation_quality` | `action_show_annotation_quality` | Per-consumer science verdict: each running recorder + the global σ/tier attached + green/yellow/red threshold + substrate explanation |
| `apply` | `action_show_apply` | Reconcile services with topology/coordination (`smd apply`) |
| `authority` | `action_show_authority` | Substrate view: live `authority.json` (active tier, σ, witnesses) (also composed into `timing_authority`) |
| `backup` | `action_show_backup` | Snapshot all config to `sigmond-config-*.tar.gz` |
| `client_config` | `action_show_client_config` | Run a client's first-run wizard / edit its config |
| `components` | `action_show_components` | Catalog: install status, git ref, version policy per component |
| `config_show` | `action_show_config` | Read-only coordination + client-config snapshot (`smd config show`) |
| `cpu_affinity` | `action_show_cpu_affinity` | Hardware topology + affinity plan + observed state + Apply-plan button |
| `cpu_freq` | `action_show_cpu_freq` | Per-CPU `scaling_max_freq` view against `[cpu_freq]` policy + Apply-policy button |
| `diag_net` | `action_show_diag_net` | IGMP classification for multicast safety |
| `environment` | `action_show_environment` | Declared vs observed peers (mDNS / ka9q / NTP / KiwiSDR / GPSDO) |
| `fft_wisdom` | `action_show_fft_wisdom` | FFTW wisdom planning (one-time per host, hours on first run) |
| `gpsdo` | `action_show_gpsdo` | Live GPSDO status from `/run/gpsdo/` |
| `install` | `action_show_install` | Catalog install picker (single / all-missing) |
| `instance` | `action_show_instance` | Per-reporter client instance lifecycle — list / add / remove + dry-run scan of legacy radiod-keyed deployments (full migration is CLI-only via `smd admin instance migrate --yes`) |
| `ka9q_watch` | `action_show_ka9q_watch` | Compare pinned ka9q-radio commit vs `origin/main` |
| `kiwisdr` | `action_show_kiwisdr` | Live KiwiSDR status + GPS |
| `lifecycle` | `action_show_lifecycle` | Start / stop / restart / reload managed units |
| `logs` | `action_show_logs` | Follow journal or tail log_paths per component |
| `overview` | `action_show_overview` | Service health + clients + CPU affinity summary (landing) |
| `placeholder` | (helper) | Generic placeholder screen for stubs |
| `rac` | `action_show_rac` | Configure frpc reverse tunnel to vpn.wsprdaemon.org |
| `radiod` | `action_show_radiod` | Live ka9q-python status (channels, frontend, SNR) |
| `restore` | `action_show_restore` | Browse + extract a backup tar over the live system |
| `sdr_inventory` | `action_show_sdr_inventory` | SDR labelling (USB enumeration + assignment) |
| `sources` | `action_show_sources` | Per-client sensor-feed selection (radiod / KiwiSDR; future mag / vlf) — list / apply; add/remove still CLI-only |
| `timing` | `action_show_timing` | Chrony-facade view: source comparison vs HPPS, root dispersion (also composed into `timing_authority`) |
| `timing_authority` | `action_show_timing_authority` | Combined nav entry: AuthorityScreen on top + TimingScreen below (authority + chrony together for "is timing healthy?" monitoring) |
| `topology` | `action_show_topology` | Enable / disable catalog components for this host |
| `validate` | `action_show_validate` | Cross-client harmonization rules (radiod / freq / CPU / disk) |
| `verifier` | `action_show_verifier` | Wsprnet upload audit (`verifier report`) + per-callsign suppression clear (`verifier rehabilitate`); one screen, two sections |

---

## 3. Capability → CLI → TUI mapping (gap audit)

| Capability | CLI verb(s) | TUI screen | Gap |
|---|---|---|---|
| First-time install (catalog walk) | `install`, `software install` | `install` | — |
| Software update / git pull per policy | `list --update`, `list --apply` | `components` | — |
| Topology enable/disable | `enable`, `disable`, `add`, `remove` | `topology` | `add` (clone repo) is CLI-only |
| Apply reconciliation | `apply`, `software apply` | `apply` | — |
| Lifecycle (start/stop/restart/reload) | `start / stop / restart / reload` | `lifecycle` | — |
| Service health snapshot | `status` | `overview` | — |
| Config view | `config show` | `config_show` | — |
| Config edit (per client) | `config edit <client>` | `client_config` | — |
| Config wizard (first-run, per client) | `config init <client>` | `client_config` | — |
| Coordination identity bootstrap | `config identity` | — | **Gap** — CLI-only |
| Coordination schema migration | `config migrate` | (button on `config_show`) | minor |
| Coordination refresh | `config refresh` | — | **Gap** — CLI-only |
| Backup / restore | `config backup / restore` | `backup`, `restore` | — |
| CPU affinity plan | `diag cpu-affinity [--apply]` | `cpu_affinity` | apply button on screen (confirm-modal-gated, auto-refresh) |
| CPU frequency plan | `diag cpu-freq [--apply]`, `cpu` | `cpu_freq` | apply button on screen (confirm-modal-gated, auto-refresh) |
| Network / IGMP diagnostics | `diag net [--listen]` | `diag_net` | — |
| FFTW wisdom (plan / status) | `wisdom plan / status` | `fft_wisdom` | one screen serves both verbs |
| Per-client SDR source selection (radiod / KiwiSDR feeds) | `sources list/add/remove/apply` | `sources` | list + apply paths surfaced; add/remove still CLI-only |
| ka9q-radio pin / compat watch | `ka9q-watch`, `watch ka9q` | `ka9q_watch` | — |
| Activity watch (wspr/psk/hfdl/codar) | `watch <target>` | `activity` | live-tail screen with target selector covers all four |
| Uploads activity watch | `watch uploads` | `activity` | reachable via the same screen's target selector |
| Verifier watch | `watch verifier` | `activity` | reachable via the same screen's target selector |
| Verifier report / rehabilitate | `verifier report / rehabilitate` | `verifier` | both surfaced on one screen (report top, rehabilitate bottom) |
| Storage migration (CH → SQLite) | `storage migrate-to-sqlite` | — | one-shot; CLI-fine |
| Storage trim (daily janitor) | `storage trim`, `timestd-tune-storage` | — | runs via systemd timers per `project_ch_to_sqlite_migration`; CLI-fine |
| Live radiod (ka9q-python) | (none — TUI-only) | `radiod` | — |
| Live GPSDO | (none — TUI-only) | `gpsdo` | — |
| Live KiwiSDR | (none — TUI-only) | `kiwisdr` | — |
| Authority substrate live view | (none — TUI-only) | `authority` | — |
| Timing (chrony facade) live view | `chronyc sources` (external) | `timing` | — |
| Environment (peers) | `environment list/probe/describe` | `environment` | — |
| SDR labelling | (handled inside `config init radiod`) | `sdr_inventory` | — |
| Logs | `log` | `logs` | log-level change still CLI-only |
| Validation | `validate` | `validate` | — |
| RAC tunnel | (manual frpc today) | `rac` | sudo path was fixed in `01d6bb7` |
| Public IP probe | `public-ip` | — | trivial; CLI-fine |
| TUI launch | `tui` | n/a | — |
| Bash completion | `completion bash` | n/a | — |

### Gap summary

One real surface gap remains. Closed so far (4 of 5):
**sources**, **activity watches**, **verifier** (report +
rehabilitate combined for workflow cohesion), and the
**cpu-affinity / cpu-freq apply** mutations (Apply buttons on the
existing read screens, gated by confirm modals and auto-refreshing
on success).

1. **Coordination identity / refresh** — `config identity`,
   `config refresh`. Installation-adjacent (identity is first-run)
   and maintenance-adjacent (refresh after coordination changes).

Maps to the four-category proposal as:

- Gap 1 → **Installation** (identity) + **Maintenance** (refresh)

Closing the gaps is *not* in scope for the reorganization commit
itself — the reorganization places empty slots where they belong, and
the follow-up backlog fills them.

---

## 4. Proposed 4-way reorganization

The current three groups (Configure / Observe / Operate) blur two
distinct operator workflows: setting up a host the first time vs.
keeping it healthy day-to-day. The four-way scheme separates them.

```
Overview                       [landing — Monitoring landing pane]

Installation                   [first-time setup, infrequent]
    Topology                   (enable components for this host)
    Software versions          (catalog: install + policy per component)
    Install                    (catalog walk / per-entry installer)
    SDR inventory              (USB enumeration + labelling)
    FFT Wisdom                 (one-time per host; hours on first run)
    Identity                   [planned — config identity bootstrap]

Maintenance / Updating         [routine operator actions]
    Lifecycle                  (start / stop / restart / reload)
    Apply                      (reconcile services with config)
    Client config              (edit / re-run wizard)
    Config view                (read-only coordination + client-config
                                snapshot; grouped with the other
                                config screens here — operators reach
                                for "show me the config" alongside
                                "edit the config", not from the
                                live-monitoring surfaces.)
    Sources                    (per-client SDR feed selection
                                (radiod / KiwiSDR); list + apply
                                in TUI, add/remove via CLI)
    CPU affinity               (apply plan)
    CPU frequency              (apply plan)
    Backup                     (snapshot config)
    Restore                    (browse + extract backup)

Debugging                      [diagnose + watch when something looks wrong]
    Logs                       (journal / log_paths follow)
    Verifier                   (wsprnet upload audit + rehabilitate)
    Validate                   (cross-client harmonization rules)
    Diag: net                  (IGMP + multicast)
    ka9q-watch                 (pin vs upstream compat)

Routine monitoring             [day-to-day "is it working" surfaces]
    Overview                   (landing)
    Environment                (declared vs observed peers — the "what's
                                actually here?" snapshot the operator
                                wants AT THE TOP of Monitoring, before
                                drilling into any one peer's live view)
    Timing & Authority         (combined: authority substrate view on
                                top + chrony facade view below — the
                                natural reading order for "is timing
                                healthy?")
    Annotation Quality         (per-consumer science verdict)
    Activity                   (live tail of `smd watch <target>` —
                                wspr / psk / hfdl / codar / ka9q /
                                uploads / verifier; one screen, target
                                selector. Moved here from Debugging:
                                operators watch activity continuously,
                                not only when something looks wrong.)
    GPSDO live                 (per-device PLL / GPS / antenna)
    ka9q-radio live            (per-radiod channels + SNR)
    KiwiSDR live               (per-KiwiSDR status)
    RAC tunnel                 (vendor reverse-tunnel state — operators
                                want to see "is the support session
                                up?" at the same beat as the other
                                live-state surfaces, not only when
                                something looks broken)
```

### Screens that span categories — resolved

| Screen | Resolution | Reason |
|---|---|---|
| `sdr_inventory` | **Installation** | The label-write workflow is what makes it load-bearing; routine browsing of "which SDRs are here" is already in `overview` / `radiod`. |
| `rac` | **Monitoring** | RAC's screen is "is the vendor reverse-tunnel up right now?" — a live-state read the operator wants alongside the other Monitoring surfaces.  Mutations (start/stop the tunnel) are rare and gate the same screen. |
| `fft_wisdom` | **Installation** | One-time per host (hours-long planning on RX-888). The "status" view is rare enough that surfacing it in Installation is fine. |
| `lifecycle` | **Maintenance** | Start/stop/restart is the most frequently invoked maintenance action; Overview already covers monitoring of running/not-running. |
| `client_config` | **Maintenance** | View is already in `config_show`; this screen is the edit path. |
| `components` (Software versions) | **Installation** | Confusable with Maintenance (updates), but the primary mental model is "what catalog do I have, at what pins" — close-coupled to fresh install. The `--update / --apply` mutating path is a maintenance use-case; the *list* view is installation. Keep here; follow-up can split if it becomes annoying. |
| `topology` | **Installation** | Enable/disable is mostly first-time and rarely revisited after a host stabilises. |
| `config_show` | **Maintenance** | Operators reach for "show me the config" alongside the config-editing screens (Client config, Sources, CPU affinity), not from the live-state monitoring surfaces.  The "Migrate" button on it reinforces that grouping. |

### Renames

- "Configure" group → split into **Installation** + bits of
  **Maintenance** + **Debugging** as above.
- "Observe" group → **Monitoring** (5 screens) + **Debugging** (5
  screens). Live SDR/timing screens are Monitoring; environment /
  validate / diag-net / ka9q-watch / logs are Debugging.
- "Operate" group → folded into **Installation** (FFT Wisdom) +
  **Maintenance** (Lifecycle, Apply).

### Landing

Overview stays the default landing screen, under Monitoring. A
"summarise each category's health" dashboard could come later but
isn't worth the complexity until the four-way structure proves itself.

### Keybindings

All existing key bindings keep working — the reorganization changes
the *visual grouping only*, not the action names. No `action_show_*`
gets renamed, so `BINDINGS`, `component_tree.on_tree_node_selected`,
and the navigation test all keep functioning with one-line label /
group swaps.

---

## 5. Out of scope for this reorganization

- Filling the five gaps above with new screens. The reorganization
  creates space for them by category; new screens are follow-ups.
- The `'update'` entry in `_MUTATING`. Dead-code cleanup, not IA.
- Splitting `components` into install-time browse vs maintenance-time
  update. Possible follow-up if the combined screen feels wrong.
- §13 socket reload route in clients. Status per
  `project_sigmond_tui_gaps` — ready, no client implements yet.
- `authority.json` schema v2 (§18.4 anchor-pair + rate). See
  `project_authority_json_v18_gap`.
