# sigmond multi-instance client architecture

Locks the per-reporter-instance shape that all recorder clients
(wspr-recorder, psk-recorder, hfdl-recorder, codar-sounder,
mag-recorder, future VLF) and the sigmond substrate must converge on.
Companion to [CLI-V2-SPEC.md](CLI-V2-SPEC.md) (CLI verb surface) and
[CLIENT-CONTRACT.md](CLIENT-CONTRACT.md) (client contract).

**Supersedes** [`tasks/plan-multi-rx888-sources.md`](../tasks/plan-multi-rx888-sources.md)
Phase 3 onward (the "one wspr-recorder serves N sources" model).
Phase 2 of that plan (the `smd admin sources` CLI namespace) is still
load-bearing and gets adapted to the per-instance model below; Phase 1
(radiod control-plane discovery) is still relevant as planned. The
remaining phases are absorbed into the implementation phases of *this*
doc.

Source state: this repo at commit `5f145d7` (2026-05-25), plus
psk-recorder and wspr-recorder as of the same date.

---

## 1. Why a multi-instance architecture

Today's recorder clients are implicitly single-instance per host. The
substrate has half the pieces in place — systemd templates with `@%i`,
per-instance env files at `/etc/<client>/env/<instance>.env`,
`lifecycle.py:UnitRef` understands instance/template — but the rest
(config files, sources files, data dirs, log dirs, the spot-row
identity, the operator-facing CLI/TUI) treats "client" as the unit of
deployment. Three frictions surface:

1. **Reporter identity is implicit.** WSPRnet sees spots as
   `(callsign + suffix)` per receiver-channel; the spot row tags
   `radiod_id` and `host_id` but never names the *reporter*. An
   operator wanting "all spots from AC0G/B1 in the last hour" has to
   reconstruct it from the radiod-id mapping in their head.
2. **One process per host doesn't scale per-reporter.** A site running
   two receivers under one callsign-suffix scheme (AC0G/B1 + AC0G/B2)
   either uses two unrelated systemd units with hand-rolled naming, or
   crams both into one process and loses per-reporter isolation
   (memory, restart, observability, resource control). See §2.
3. **The "instance" axis is split-brain across the codebase.** psk-recorder
   spots have a column literally named `instance` set to `radiod_id`
   (`psk-recorder/src/psk_recorder/core/ch_tailer.py:425`). sigmond's
   lifecycle layer treats the systemd `@<instance>` name as the
   instance. Neither matches the operator's mental model, which is "a
   reporter is a deployment context."

The fix: make the **reporter ID** the first-class instance discriminator
across systemd, the file system, sigmond's CLI/TUI, the per-spot
schema, and the in-process configuration of each client.

---

## 2. The deployment model — one process per reporter

After investigating the workload (FFT releases GIL; decoders run as
subprocesses; psk-recorder already hit memory-fragmentation issues
needing `MALLOC_ARENA_MAX=2`), per-process-per-reporter wins on
operational grounds:

| Concern | Single-process-N-sources | One-process-per-reporter |
|---|---|---|
| asyncio event-loop serialization | one loop shared by all sources | each process has its own |
| Memory fragmentation | additive in one RSS | bounded per process; per-instance `malloc_trim()` |
| Failure-domain coupling | one bad source kills all | per-instance restart |
| systemd resource control (`MemoryHigh`, `CPUWeight`, `Slice=`) | one set, shared by all sources | per-instance |
| Observability (journal grep) | one stream, all sources mixed | per-instance unit, per-instance journal |
| Restart scope | edit for one source = all restart | edit for one instance = only that instance |
| Baseline memory cost | low (one interpreter) | ~50-100 MB × N instances |
| SQLite write contention | one writer | N writers (WAL mode; needs validation under load) |
| Cross-instance shared state | trivial (in-memory) | needs IPC (rare; reporters are independent) |

The right-side column wins because the operational wins (isolation,
resource control, observability, failure-domain) are exactly what
Python solved with "more processes" for these workloads decades ago.
The single-process model would only be preferable if reporters needed
shared in-memory state — they don't.

**SQLite contention** is the one engineering risk to validate before
committing the full migration; quick WAL benchmark with N=4 writers at
cycle boundaries is sufficient (estimated 30 min). Listed as an
implementation-phase task in §10.

---

## 3. Reporter ID — the canonical instance discriminator

A reporter ID is the operator-meaningful identifier for one deployment
context (one receiver-channel scheme as WSPRnet would see it).

**Format (locked):** matches the regex `[A-Z0-9][A-Z0-9-]*[A-Z0-9]`
— uppercase alphanumerics and ASCII hyphens, no leading/trailing
hyphen. Examples: `AC0G-B1`, `AC0G-B2`, `KP4MD-RPI4`, `W4UK-WEST`.

**Why path-safe by construction (not sanitization-with-display):**
- Single source of truth for the identifier; no risk of two forms
  drifting apart
- Reporter IDs travel through many surfaces (systemd unit names, file
  paths, env-var values, log-line prefixes, TUI labels); a single form
  for all of them is simpler than a path-safe form + a display form
- WSPRnet's slash convention (`AC0G/B1`) is rendered only at the
  upload boundary: the uploader translates `AC0G-B1` → `AC0G/B1` when
  posting to WSPRnet, leaving the sigmond-internal world consistent

The translation rule at the WSPRnet boundary is mechanical:
`reporter_id.replace('-', '/', 1)` — first hyphen becomes the slash,
remaining hyphens are part of the suffix.

---

## 4. Canonical file layout (locked)

Every instance owns the following paths, all keyed on
`<client>` + `<reporter-id>`:

| Concern | Path |
|---|---|
| systemd unit | `/etc/systemd/system/<client>@<reporter-id>.service` (template `<client>@.service` shipped by client repo) |
| systemd drop-ins | `/etc/systemd/system/<client>@<reporter-id>.service.d/` |
| Per-instance env | `/etc/<client>/env/<reporter-id>.env` |
| Per-instance config | `/etc/<client>/<reporter-id>.toml` |
| Per-instance sources | `/etc/sigmond/clients/<client>@<reporter-id>.sources.toml` |
| Per-instance state | `/var/lib/<client>/<reporter-id>/` |
| Per-instance logs (file) | `/var/log/<client>/<reporter-id>/` |
| Per-instance runtime | `/run/<client>/<reporter-id>/` (e.g. PIDs, control sockets) |
| Per-instance ka9q-radio conf (if applicable) | `/etc/radio/radiod@<source-id>.conf` (radiod side, unchanged — sigmond doesn't manage radiod) |

The per-instance config file replaces today's per-client config (e.g.
`/etc/wspr-recorder/wspr-recorder-config.toml`). Per-client global
config is *not* layered underneath — each instance is self-contained.
If two instances on the same host want to share knobs, they each
declare them; no inheritance.

---

## 5. The per-instance config file — source of truth for instance state

Owned and read by the client (each client repo's `configurator.py`
schema). Sigmond writes the initial file via `smd admin instance add` (§6),
then defers to the client's config-edit flow (whiptail wizard,
in-TUI Textual wizard, or `$EDITOR`).

Minimum schema all clients must support (proposed for v0.8 of the
client contract):

```toml
# /etc/<client>/<reporter-id>.toml

[instance]
reporter_id = "AC0G-B1"            # MUST match filename; sanity check at load

sources = [                         # list of source-keys, see §3 Wiring of CLI-V2-SPEC
    "radiod:my-rx888",
    # "kiwi:grape-corner-1",        # optional, multi-source per reporter
]

[instance.metadata]                 # informational; analysis use, not load-bearing
antenna  = "loop"                   # operator description
sdr      = "rx888-mk2"              # SDR model / serial / friendly name
# Free-form additional keys allowed; clients ignore unknowns.

# ... client-specific sections below ([processing], [timing], etc.) ...
```

The `[instance]` block is the contract sigmond depends on; the rest is
the client's. The `sources` list is what `smd admin sources apply` writes
into the per-instance sources file (§4); the per-instance config holds
the operator-curated copy as the source of truth, and `smd admin sources
apply` renders it into the runtime config the client actually reads.

---

## 6. The `smd admin instance` CLI namespace

New namespace in `bin/smd` (per CLI-V2-SPEC.md style — namespaced,
not bare verbs).

```
smd admin instance list                                    # all instances across all clients
smd admin instance list <client>                           # instances of one client
smd admin instance show <client> <reporter-id>             # config + units + sources for one
smd admin instance add <client> <reporter-id>              # create — see below for what it does
smd admin instance remove <client> <reporter-id>           # remove — stop unit, optionally --purge files
smd admin instance edit <client> <reporter-id>             # invoke the client's config-edit flow
smd admin instance enable <client> <reporter-id>           # systemctl enable + start
smd admin instance disable <client> <reporter-id>          # systemctl disable + stop
smd admin instance migrate                                 # one-shot migration from radiod-keyed names
```

**`smd admin instance add <client> <reporter-id>` does:**
1. Validate the reporter ID against the §3 regex
2. Refuse if the instance already exists for that client
3. Initialize `/etc/<client>/<reporter-id>.toml` (template from the
   client's `<client> config show --defaults --json`)
4. Initialize `/etc/<client>/env/<reporter-id>.env` (empty + sigmond
   header)
5. Initialize `/etc/sigmond/clients/<client>@<reporter-id>.sources.toml`
   (empty selection)
6. Create `/var/lib/<client>/<reporter-id>/`,
   `/var/log/<client>/<reporter-id>/`, `/run/<client>/<reporter-id>/`
   with correct ownership
7. **Does NOT** enable or start the unit — operator runs `smd admin instance
   enable` after picking sources and editing config

**`smd admin instance migrate` does** (one-shot, idempotent, dry-run by default):
1. Walk existing `<client>@<old>.service` units that are NOT
   already reporter-ID-named (heuristic: old name doesn't match §3
   regex constraints clearly, or is on a known migration list)
2. For each, prompt the operator for the new reporter ID
3. Move env file, config file, sources file (if any), data dir, log
   dir to the new reporter-ID-keyed paths
4. `systemctl disable` the old unit, `systemctl enable` the new one
5. `systemctl daemon-reload`, restart under the new name

Existing top-level `smd config init <client>` / `smd config edit
<client>` and `smd component enable <client>` (CLI-V2-SPEC.md aliases)
stay, but the canonical operator workflow becomes:

```
smd admin instance add wspr-recorder AC0G-B1
smd admin sources add wspr-recorder@AC0G-B1 radiod:my-rx888
smd admin instance edit wspr-recorder AC0G-B1     # set antenna, sdr, processing knobs
smd admin instance enable wspr-recorder AC0G-B1
```

---

## 7. Spot-row schema impact

The spot rows each client writes to the hamsci_sink JSON payload
gain `reporter_id` as a first-class field. Existing fields
(`radiod_id`, `host_id`, `rx_source`, `rx_call`, `rx_grid`,
`instance`) remain for the migration window but `reporter_id` becomes
the load-bearing identifier downstream.

```jsonc
{
  // existing fields unchanged
  "radiod_id":   "my-rx888",
  "host_id":     "bee1.local",
  "rx_source":   "radiod:my-rx888",
  "rx_call":     "AC0G",
  "rx_grid":     "EM38",
  // new in v0.8
  "reporter_id": "AC0G-B1",
  "antenna":     "loop",                  // from instance.metadata, may be null
}
```

The existing psk-recorder `instance` column (set to `radiod_id` today,
ch_tailer.py:425) is deprecated in favor of `reporter_id` — same
spot-row migration window as the rest of the contract bump.

The `wsprnet_reject_cache` and `wsprnet_audit` tables that the
verifier reads get a `reporter_id` column added; existing rows
backfill to `<rx_call>` (no suffix) as the legacy form.

---

## 8. TUI impact

Existing screens that talk about "the wspr-recorder" pick up an
instance dimension. The pattern: each per-recorder screen gets a
two-stage selector (client → instance) at the top. When a client has
only one instance, auto-select it and hide the second dropdown.

Affected screens (revisits of work shipped in this session's commits
3ab36a8 .. a133e54):

| Screen | What changes |
|---|---|
| `activity` | Activity target stays (`smd watch <target>`), but selecting `psk`/`wspr`/`hfdl`/`codar` exposes a second-stage instance dropdown; passes `--instance <reporter-id>` to the watcher |
| `verifier` | Verifier report's `--rx-call` becomes per-instance auto-filled; rehabilitate takes `<reporter-id>` instead of (or alongside) `<rx_call>` |
| `logs` | Per-instance log target — `smd admin log <client>@<reporter-id>` follows that one unit's journal |
| `lifecycle` | Per-instance start/stop/restart/reload buttons |
| `sources` | Two-stage selector; per-instance source list (now plural) |
| `client_config` | Two-stage selector — config init/edit operates on one instance |

New screens needed:
- **Instance** (under Installation): browse + add + remove instances
  across all clients. Equivalent of `smd admin instance list/add/remove`.

---

## 9. Backward compatibility — NOT preserved

Existing single-instance deployments (the `wspr-recorder@my-rx888`
shape on bee1 and similar) get migrated to reporter-keyed names via
`smd admin instance migrate`. No permanent dual-form support; the
radiod-keyed instance names are deprecated as soon as the migration
ships and removed one release after.

Rationale: dual-form support would mean every consumer
(systemd template lookups, log parsers, the TUI, the spot schema)
carries a "which form is this in?" branch forever. Painful. Migration
is one-shot, scriptable, and the operator community is small enough
to coordinate.

The migration helper is dry-run by default and asks for explicit
confirmation per host. Operators who *don't* migrate immediately keep
running on the old names — they just lose access to the new TUI
features that key off reporter ID. The deprecation window is one
release of the sigmond suite (matching CLI-V2-SPEC §5 cadence).

---

## 10. Implementation phases

One PR per phase, smallest viable slice. None of these are in scope
for the spec commit itself.

**Phase 1 — SQLite contention sanity check. DONE.** Benchmark in
`scripts/bench_sqlite_contention.py` spawns N writer processes, each
owning a real `hamsci_sink.Writer` to a shared temp `sink.db`, all
synchronizing burst start via `multiprocessing.Barrier`.  Detects
silent-retry events (the Writer catches lock failures, logs, and
retains the buffer) by checking `writer.buffered` post-flush.

Results on the host this spec was written on (2026-05-25):

| Scenario | N | Spots/burst | p50 | p95 | p99 | Errors | Verdict |
|---|---|---|---|---|---|---|---|
| WSPR burst | 4 | 170 | 6 ms | 22 ms | 22 ms | 0 | **GREEN** (threshold 50 ms) |
| FT8 burst  | 4 |  80 | 4 ms | 11 ms | 21 ms | 0 | **GREEN** (threshold 25 ms) |
| WSPR burst | 8 | 170 | 10 ms | 59 ms | 60 ms | 0 | RED (over threshold; informational stretch test) |
| FT8 burst  | 8 |  80 |  6 ms | 56 ms | 57 ms | 0 | RED (over threshold; informational stretch test) |

**Verdict: GREEN for the typical case (N ≤ 4 per host).** Phase 2+
unblocked.  N=8 shows linear contention from SQLite's writer-
serialization — tolerable for WSPR (120 s cycles) but pushes FT8's
15 s SLA; treat 6-8 reporters per host as a soft ceiling that warrants
re-benchmarking before committing.

**Phase 2 — `smd admin instance` CLI namespace.** Implements the seven
verbs from §6 with the per-instance file-layout actions. Does NOT
yet require client refactors — the sigmond side can create
instances even when the client still loads from the
shared-per-client config. Test with a "noop client" first.

**Phase 3 — psk-recorder per-instance refactor. DONE
(psk-recorder commit `162f967`, 2026-05-25).** Soft cutover, not the
hard switch the spec originally implied: legacy shared-config
deployments keep working with a one-line `DeprecationWarning` so
bee1's running psk-recorder isn't broken until the operator-driven
migration (Phase 8). Specifically:

- New `--instance <reporter-id>` flag on the daemon subparser
  (alongside the still-honored `--radiod-id`).
- `config.resolve_config_path()` precedence: `--config` (explicit)
  > `$PSK_RECORDER_CONFIG` > `/etc/psk-recorder/<instance>.toml`
  (preferred, when --instance given and file exists) > legacy
  `/etc/psk-recorder/psk-recorder-config.toml` (deprecation
  warning when --instance was given, silent otherwise).
- `config.extract_reporter_id()` reads the per-instance
  `[instance]` block; daemon falls back to `--instance` value when
  the resolved config has no `[instance]` block.
- Spot rows now carry **both** `instance` (= radiod_id, legacy;
  removed in Phase 9) and `reporter_id` (= per-instance value or
  radiod_id-derived fallback).
- systemd template `psk-recorder@.service` passes `--instance %i`
  alongside `--radiod-id %i`; `--config` intentionally omitted so
  the per-instance path can take effect once the operator
  migrates.
- 235/235 tests passing (12 new, 0 regressions).

Pilot deployment, then bee1 migration, happens in Phase 8.

The original spec language ("load `/etc/psk-recorder/<reporter-id>.toml`
instead of `psk-recorder-config.toml`") is technically softened: the
new path is preferred *when present*, not enforced. The strict
cutover happens in Phase 9 (remove the deprecated radiod-keyed
unit names + the legacy shared-config fall-through).

**Phase 4 — wspr-recorder per-instance refactor. DONE
(wspr-recorder commit `27035e0`, 2026-05-25).** Same shape as Phase
3 — soft cutover.  Drops the planned-but-unbuilt "single wspr-
recorder serves N sources" approach from plan-multi-rx888-sources.md
Phase 3 in favor of per-process-per-reporter. Specifically:

- New `--instance <reporter-id>` flag on the daemon
  (cli.py + legacy `__main__.py`).
- `config.resolve_config_path()` (mirrors psk-recorder's): five-rung
  precedence ladder, preferring `/etc/wspr-recorder/<instance>.toml`
  when present, falling back to `/etc/wspr-recorder/config.toml`
  with a `DeprecationWarning` when `--instance` was given but the
  per-instance file is missing.
- `config.extract_reporter_id()` reads the `[instance]` block from
  either a parsed dict or a Path.
- `SpotSink` accepts a `reporter_id`; both `spot_to_row` and
  `noise_to_row` add a `reporter_id` field, falling back to
  `radiod_id` when not provided.
- systemd template `wspr-recorder@.service` now passes
  `--instance %i`; `--config` intentionally dropped so the per-
  instance path can take effect once the operator migrates.
- 380/380 tests passing (19 new, 0 regressions).

**Phase 5 — hfdl-recorder / codar-sounder / mag-recorder per-instance refactor. DONE (2026-05-25).**
Three commits across three repos:

- **codar-sounder commit `20f0c8e`** — full Phase 3/4 pattern: new
  resolve_config_path + extract_reporter_id, --instance flag,
  reporter_id plumbed through SounderDaemon → _TransmitterPipeline,
  systemd template passes --instance %i; 233 tests pass.
- **hfdl-recorder commit `72296cd`** — same pattern: --instance,
  resolvers, reporter_id through HfdlRecorder → ChTailer, systemd
  template passes --instance %i; 88 tests pass.
- **mag-recorder commit `8fb48ff`** — code-level plumbing only.
  mag-recorder is singleton (`mag-recorder.service`, not
  templated), so --instance is currently dormant.  Per-instance
  config resolution falls through to the legacy shared path; the
  supervisor stamps each spooled sample with reporter_id when set
  (None today, so the field is omitted on legacy deployments).
  Template conversion is deferred to the Phase 8 migration tool;
  the code is ready for that conversion when it happens.

Cutover-fallback fix applied separately to psk-recorder (commit
`4d2ebf8`) and wspr-recorder (commit `7b31a3b`) during this same
phase: removed the daemon-side `reporter_id = args.instance`
fallback that wrongly used radiod-id values as reporter_ids during
the deprecation window.  Both daemons now leave reporter_id None
when no [instance] block exists; row-construction layers fall back
to radiod_id (matching the legacy `instance` field semantic).
Same fix baked into the Phase 5 commits from the start.

**Phase 6 — TUI screen revisits. PARTIAL (sigmond pending commit, 2026-05-25).**
Three of the seven §8 deliverables shipped this round:

- **Instance screen (new, under Installation)** — `lib/sigmond/tui/
  screens/instance.py`.  DataTable listing of per-reporter
  instances; add (with --dry-run option) + remove + dry-run scan of
  legacy radiod-keyed deployments.  Full interactive migration
  stays CLI-only (`smd admin instance migrate --yes`) — the TUI
  doesn't have a sensible interactive multi-prompt flow for that.
- **Activity screen** — added a second-stage instance dropdown.
  Selecting a per-recorder target (wspr/psk/hfdl/codar) populates
  the dropdown with configured per-instance reporter IDs PLUS any
  legacy radiod-keyed instances detected via systemctl.  Meta
  targets (ka9q / uploads / verifier) show "(no instance
  dimension)".  Passes `--instance <reporter-id>` to `smd watch
  <target>` when a specific instance is selected.
- **Lifecycle screen** — added a per-instance section below the
  existing component-level view.  Lists every templated recorder
  unit known to systemctl (active or inactive).  Per-unit
  start / stop / restart / reload-or-restart buttons that shell
  directly to `sudo systemctl <verb> <unit>` (bypassing sigmond's
  component-level lifecycle lock, which doesn't apply to single-
  unit actions).

**Phase 6b — remaining four screen revisits. DONE (sigmond pending
commit, 2026-05-25):**

- **Verifier screen** — second-stage instance dropdown between
  Target and Window.  Selecting an instance auto-fills the RX call
  Input with the WSPRnet slash form derived from reporter_id
  (e.g. `AC0G-B1` → `AC0G/B1` via `instance.to_wsprnet_form()`).
  Operator can still hand-type to override.
- **Logs screen** — second-stage instance dropdown populated when
  a templated recorder component is picked.  When an instance is
  selected, journalctl narrows to that single
  `<client>@<instance>.service` unit instead of following all of
  the component's units in aggregate.
- **Client config screen** — per-instance dropdown + "Edit per-
  instance" button.  Selecting a row populates the dropdown with
  the client's known instances (config + legacy radiod-keyed).
  The button runs `smd admin instance edit <client> <reporter-id>` (the
  CLI command suspends the TUI exactly like the other Edit
  buttons).  Today's `smd admin instance edit` is a Phase-2 stub that
  points at `$EDITOR` — wiring is ready for when the per-client
  refactor lets it drive the client's config flow with the per-
  instance path injected.
- **Sources screen** — added a hint section documenting the
  per-instance CLI syntax (`smd admin sources add <client>@<reporter-id>
  <kind>:<id>`) that the sources CLI will accept once **Phase 7**
  grows it.  Full per-instance filter UI is deferred to that
  phase; until then, sources are per-client.

Inventory updated: `instance` row added in §2, screen count 30 → 31.

**Phase 7 — Spot-schema bump to v0.8 + uploader awareness.
PARTIAL (sigmond pending commit, 2026-05-25):**

- **Phase 7a (DONE)** — CLIENT-CONTRACT.md bumped to v0.8 with a
  new §19 formalising the per-reporter-instance shape and the
  `reporter_id` row tag.  §19.1 locks the regex; §19.2 the
  per-instance config preference + soft cutover; §19.3 the
  row-tag MUST + the daemon-level "do NOT fall back to
  args.instance" rule; §19.4 the WSPRnet slash-form rendering
  rule + `to_wsprnet_form()` helper; §19.5 the sources file
  shape; §19.6 the migration path pointer.  Sources CLI silently
  accepts the per-instance form `<client>@<reporter-id>` (new
  helper `_is_valid_per_instance_client` in bin/smd validates
  the prefix against KNOWN_CLIENTS and the suffix against the
  reporter-id regex).
- **Phase 7b (DEFERRED)** — hs-uploader per-reporter pipelines.
  The WSPRnet transport's `_post()` uses one `identity.call` per
  pipeline; per-reporter operation needs one pipeline per
  reporter (each with `identity.call = to_wsprnet_form(reporter_id)`).
  This is architectural — the pipeline configuration shape
  changes, the dispatcher may need to fan out by reporter_id,
  and the per-cycle dedup logic in `dedup_records_for_wsprnet`
  may need to scope by reporter.  Multi-week.
- **Phase 7c (DEFERRED)** — `wsprnet_audit` /
  `wsprnet_reject_cache` schema bumps (wspr-recorder side).
  Each table gains a `reporter_id` column; existing rows
  backfill to `<rx_call>` (no suffix) as the legacy form.
  Operating-host DB migration plus verifier-side query updates.

**Phase 8 — Migration tool. DONE (sigmond pending commit, 2026-05-25).**
`smd admin instance migrate` per §6 — replaces the Phase-2 detect-only
stub with a one-shot interactive migration.

Detection (`detect_migration_candidates()` in `lib/sigmond/instance.py`)
unions two signals:
- per-instance env files at `/etc/<client>/env/<name>.env` whose
  stem isn't a valid reporter_id
- systemctl-loaded `<client>@<name>.service` units (templated
  recorders only — `_TEMPLATED_RECORDER_CLIENTS` set) whose
  instance name isn't a valid reporter_id

mag-recorder is intentionally excluded from the templated-recorder
set (singleton service, not a template); its conversion is
out-of-band.

Per-candidate migration (`migrate_one_instance()`) runs eight steps
in order: stop + disable old unit, create per-instance config (copy
legacy shared + prepend `[instance]` block), mv env / data / log /
drop-in directories, daemon-reload, enable + start new unit.

Dry-run is the default; `--yes` opts into the live destructive
path (requires root).  Per-candidate prompt for the new reporter
ID; blank input or invalid format skips that candidate.

**Smoke-tested on bee1:** dry-run detected the expected 5
candidates (psk/wspr/hfdl @ my-rx888, codar @ ac0g-bee1-rx888, an
inactive anomaly psk @ sigmond-decode-health-collect).  Live
migration NOT exercised yet on bee1 — that requires an explicit
operator-driven run with the four real reporter IDs settled.

**v1 per-instance config strategy:** the migration COPIES the
legacy shared config into the new per-instance config file and
prepends an `[instance]` block.  The new file contains the FULL
legacy content; operators may trim irrelevant `[[radiod]]` /
`[[band]]` blocks afterwards.  This trades a one-time cleanup
chore for migration-tool simplicity (no TOML splitting logic
required).  Documented in the migration-config header.

**installation-guide.md** companion section is deferred to the
operator-rollout follow-up; the tool itself is shipped.

**Phase 9 — Remove the deprecated radiod-keyed unit names.** One
release after Phase 8 lands. CLIENT-CONTRACT.md gains a §19
deprecating the radiod-keyed pattern formally.

---

## 11. Open questions deferred to implementation

Decisions the implementation phases can make without re-opening §1-9:

1. **Operator-input UX for reporter ID at `smd admin instance add` time.**
   Pure CLI prompt? A short modal in TUI's Instance screen? Both?
   Both, probably; default to CLI prompt.
2. **Default reporter-ID suggestion.** Sigmond knows the host
   callsign from coordination.toml. Suggest `<callsign>-<short-host>`
   (e.g. `AC0G-BEE1`) as a single-instance default? Operator can
   accept or override.
3. **`smd component enable <client>` semantics under multi-instance.**
   Today it sets `enabled=true` in topology. Under multi-instance,
   is "client enabled" still meaningful, or does enablement live
   per-instance only? Lean: keep `component enable/disable` as the
   catalog gate (is this client available to *create instances of*?),
   and use `instance enable/disable` for runtime.
4. **`smd start <client>` under multi-instance.** Today starts all
   units of a client. Should it become "start all enabled instances
   of this client"? Lean: yes, that's the natural read.
5. **Per-instance log level via `smd admin log set-level`.** Today the
   `set-level` form takes `<client>` (or omits it for global). Add a
   `<client>@<reporter-id>` form for per-instance. Backward
   compatible — the `<client>` form means "all instances of this
   client."
6. **`smd watch <target>` instance selection.** Already has
   `--instance` (visible in bash completion). Verify the underlying
   handler uses the reporter-ID form post-Phase-4 rather than the
   radiod-id form.

---

## 12. Out of scope for this spec

- The radiod-side configuration of `radiod@<source-id>.conf`.
  Radiod instances are managed outside sigmond; sigmond only consumes
  what radiod advertises as a source.
- IPC between reporter instances (e.g., a shared callsign cache).
  Each instance is independent by construction. If a future need
  surfaces, address it with a separate spec.
- Reporter-ID schema for non-recorder clients (gpsdo-monitor,
  hf-timestd, ka9q-web). Those clients are host-singleton today and
  don't have a reporter-identity dimension. The multi-instance
  pattern is opt-in per client; clients without per-reporter
  meaningfulness stay singleton.
- Cross-host reporter coordination (e.g., one operator running
  reporters under the same callsign on two hosts). Each host's
  instances stand alone; cross-host aggregation lives at the
  wsprdaemon.org / wsprnet tier as today.
- Changes to the WSPRnet upload protocol. The reporter ID's
  `AC0G/B1` rendering at upload time is purely a mechanical
  hyphen→slash mapping at the uploader boundary.
