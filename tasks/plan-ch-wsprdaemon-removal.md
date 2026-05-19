# Plan — Remove ClickHouse + wsprdaemon-client, rename sink module, §17 rewrite

**Status:** awaiting approval — no edits made yet (except the earlier
`SUPPORTED_CONTRACT_VERSION` 0.5→0.6 bump, already committed-pending).

## Decisions locked (user)

1. §17 `kind` enum → engine-agnostic (`clickhouse` → `service`).
2. ClickHouse: **fully removed** from sigmond and all dependent repos.
3. wsprdaemon-client: **removed** from sigmond catalog/topology/code.
4. `smd verifier report --target psk`: **reworked to SQLite-only**.
5. Repo scope: **all dependent repos**, one coordinated sweep.
6. `lib/sigmond/hamsci_ch/` → **renamed engine-neutral**.
7. Contract: **in-place v0.6 amendment** (no v0.7 bump).

## Naming choices (confirm or override)

- Module `lib/sigmond/hamsci_ch/` → **`lib/sigmond/hamsci_sink/`**.
- Public class: drop CH `Writer`; the SQLite writer becomes the sole
  exported `Writer` with `Writer.from_env()` (SQLite-only). External
  callers: `from sigmond.hamsci_sink import Writer`.
- §17 `kind`: `file` | **`service`**.

## Repos in scope (9)

| Repo | Role | Disposition |
|------|------|-------------|
| sigmond | source of truth | full rework (Phase 0) |
| psk-recorder | live client | CH removal + import rename + contract 0.6 |
| hfdl-recorder | live client | CH removal + import rename |
| codar-sounder | live client | CH removal + import rename |
| wspr-recorder | live client | import rename + contract 0.6 |
| mag-recorder | live client | deploy.toml `[clickhouse]` block removal |
| hs-uploader | reader/shipper | drop `sources/clickhouse.py` + import rename |
| sigmond-clickhouse | CH component repo | **retired** — only sigmond's catalog ref removed; repo archival is owner's call (out of edit scope) |
| wsprdaemon-client | deprecated client | **assumption: leave code untouched** — it is being retired; its `hamsci_ch` import will break but the repo is dead. Confirm. |

## Phase 0 — sigmond repo

### 0A. Sink module rename + CH backend removal
- `git mv lib/sigmond/hamsci_ch lib/sigmond/hamsci_sink`.
- Delete CH `Writer` class from `writer.py`; keep shared `BufferFull`.
- `SqliteWriter` → exported as `Writer`; `from_env()` drops the
  `SIGMOND_CLICKHOUSE_URL` branch (SQLite-only: `SIGMOND_SQLITE_PATH`
  override → default `/var/lib/sigmond/sink.db` → no-op).
- Drop `ConnectionConfig`, `resolve_db_alias()` (CH-only).
- Rewrite `__init__.py` exports + docstring.
- Update sigmond's own importer: `storage_migrate.py`.

### 0B. Delete ClickHouse machinery
- Delete `lib/sigmond/commands/ch_apply.py`.
- `bin/smd`: remove `ch_apply` import + CH migration block in `cmd_apply()`.
- `coordination.py`: delete `ClickHouseStorage`, `Storage`,
  `_CLICKHOUSE_DEFAULT_MODES`; strip CH env emission from `render_env()`.
- `pyproject.toml`: delete `[project.optional-dependencies].clickhouse`.
- `etc/coordination.example.toml`: delete `[storage.clickhouse]` block.
- `storage_migrate.py` / `smd storage migrate-to-sqlite`: **keep** —
  it is the operator cleanup tool for legacy CH installs.

### 0C. Catalog + topology
- `etc/catalog.toml`: delete `[client.sigmond-clickhouse]` and
  `[client.wsprdaemon-client]`; bump psk-recorder / hf-timestd /
  wspr-recorder `contract` 0.5 → 0.6 (mag-recorder already 0.6).
- `catalog.py`: drop the `wspr` alias (keep `grape`).
- `etc/topology.example.toml`: drop wsprdaemon-client component/key.
- `etc/coordination.example.toml`: drop `[[clients.wspr]]`.

### 0D. wsprdaemon-client code removal
- Delete `clients/wspr.py`, `wd_client_config.py`,
  `tui/screens/wd_client.py`.
- `clients/__init__.py`: drop WsprAdapter registration (keep grape).
- `tui/app.py`, `tui/widgets/component_tree.py`: drop wd_client screen.
- `topology.py`, `discover.py`, `commands/config.py`, `paths.py`,
  `commands/ka9q_watch.py` (drop one venv glob), `lifecycle.py`,
  `cpu.py`, `scripts/sigmond-decode-health-collect.py`: remove refs.
- `harmonize.py`: keep the generic §16.3.1 meta-client rule; drop
  wsprdaemon-client as its example.

### 0E. Contract doc — §17 in-place v0.6 amendment
- §17: `clickhouse` kind → `service`; example JSON + field table;
  §17.5 heading + obligations → engine-agnostic wording; **delete**
  the wspr/sigmond-clickhouse schema-vendoring paragraph (1926-1934).
- §17.4 `disk_writes` auto-promotion: unchanged (already agnostic).
- Version header: drop wsprdaemon-client from the conformant-client
  list; add a "v0.6 §17 revised" changelog note.

### 0F. psk verifier rework
- `verifier_report_psk.py`: drop the upstream-CH HTTP diff; rework to
  a local-sink audit reading the SQLite `psk.spots` queue
  (queued vs delivered/acked). **Design micro-decision flagged below.**

### 0G. Docs
- `CLAUDE.md` (Sink backend selection, core-commands list, topology,
  architecture layers, companion-project section),
  `README.md`, `tui-configurator.md`, `docs/HOST-CAPACITY-PLANNING.md`,
  `docs/SCINTILLATION-MONITORING.md`, `docs/installation-guide.md`.

### 0H. Tests
- Delete `test_ch_apply.py`, `test_hamsci_ch.py`.
- `test_sqlite_writer.py` → rename, drop CH-dispatch cases.
- Update `test_coordination.py`, `test_catalog.py`, `test_harmonize.py`,
  `test_lifecycle.py`, `test_log_cmd.py`, `test_verifier_report_psk.py`,
  `test_contract_adapter.py` (already done), `test_storage_migrate.py`.

## Phase 1 — client repos

Per repo: delete `clickhouse/` schema dir, delete `ch_tailer.py` /
`ch_writer.py`, drop `[clickhouse]` from `deploy.toml`, repoint
`hamsci_ch` imports → `hamsci_sink`, drop `data_sinks` `clickhouse`
entries / make engine-agnostic, delete CH tests, bump `contract_version`
to 0.6 where the client is live.

- **psk-recorder** — `core/ch_tailer.py`, `clickhouse/`, imports in
  `uploader.py` `recorder.py` `hs_uploader_shim.py`, `deploy.toml`,
  `tests/test_ch_tailer.py` + import-using tests.
- **hfdl-recorder** — `core/ch_tailer.py`, `clickhouse/`, `deploy.toml`,
  `tests/test_ch_tailer.py`.
- **codar-sounder** — `core/daemon.py`, `core/output.py`, `clickhouse/`,
  `deploy.toml`.
- **wspr-recorder** — `spot_sink.py`, `__main__.py`, `tests/`,
  `deploy.toml`.
- **mag-recorder** — `deploy.toml` `[clickhouse]` removal.
- **hs-uploader** — delete `src/hs_uploader/sources/clickhouse.py`,
  repoint imports, check `transports/wsprdaemon.py`.

## Sequencing

Per-repo branch + commit (not one mega-commit — keeps each reviewable).
Land sigmond Phase 0 first (it defines `hamsci_sink`), then the client
repos together. "No interim broken state" = land the set close
together; clients pin/pull the new sigmond lib as part of their commit.

## Open items needing your call before I start

1. **wsprdaemon-client repo** — leave its code untouched (it's
   retired)? Or do you want its CH/import code cleaned up too?
2. **psk verifier rework semantics** — with no upstream CH diff, what
   should `--target psk` actually report? Proposed: audit the local
   SQLite sink — count psk rows queued vs delivered/acked, with FT
   cycle cadence, no round-trip claim. Confirm or specify.
3. **Module name** `hamsci_sink` and **§17 kind** `service` — OK, or
   prefer other names (`sink` / `external`)?
4. Confirm per-repo commits (vs one sweep) is acceptable.

## Review section

### Phase 0 — sigmond — DONE (branch `feat/remove-clickhouse-wsprdaemon`, commit `bc454bd`)

- `hamsci_ch` → `hamsci_sink`; CH `Writer` deleted; SQLite writer is the
  sole exported `Writer`, `from_env()` SQLite-only.
- `ch_apply.py` + the `smd apply` CH step deleted; `[storage.clickhouse]`
  / `ClickHouseStorage` / `Storage` removed from coordination; `clickhouse`
  pyproject extra removed; `[storage.clickhouse]` example removed.
- wsprdaemon-client removed: catalog + topology entries, `wspr` alias,
  `WsprAdapter`, `wd_client_config.py`, TUI `wd_client` screen, and the
  topology/paths/config/cpu/ka9q_watch/lifecycle touchpoints.
- CONTRACT §17 rewritten engine-agnostic (`clickhouse` kind → `service`),
  in-place v0.6; §16 meta-client example de-wsprdaemon-client'd.
- `verifier report --target psk` reworked to a SQLite-only local-queue
  audit (in_flight vs stale).
- catalog contract bumped to 0.6 for psk-recorder/hf-timestd/wspr-recorder.
- Tests: 540 pass, 12 fail — all 12 pre-existing on untouched `main`
  (9 = textual not installed, 3 = catalog discovery pollution).
  Zero regressions.
- `smd storage migrate-to-sqlite` kept (legacy-CH cleanup tool).
- `docs/SCINTILLATION-MONITORING.md` left untracked (operator WIP).

### Phase 1 — client repos — DONE

All on branch `feat/remove-clickhouse`:

| Repo | Commit(s) | Notes |
|------|-----------|-------|
| psk-recorder | `c37ce3c` | `uploader.py` was pure CH — deleted; SQLite uploader path is unconditional |
| hfdl-recorder | `733c5b3` | runtime-built `data_sinks` CH entry removed; 87 tests pass |
| codar-sounder | `1a124e6`, `6d92253` | follow-up reconciled an invented `kind="sqlite"` → drop the entry |
| wspr-recorder | `8429f61`, `393a6df` | follow-up bumped runtime `CONTRACT_VERSION` 0.4→0.6 to match deploy.toml |
| mag-recorder | `3cb9a67` | one-line legacy-alias comment |
| hs-uploader | `52758ec` | doc-only (reads queue by raw SQL) |

`data_sinks` reconciled across psk/hfdl/codar: the `clickhouse` entry
is dropped, file sinks kept, no engine-named `kind`.

### Known remaining items (not in scope of this change)

- `wspr-recorder/wsprdaemon_verifier.py` still queries the *upstream*
  wsprdaemon.org ClickHouse (wd10/20/30) directly — a separate
  redesign, untouched.
- `ch_tailer.py` in psk/hfdl-recorder is now a misnomer (writes to the
  SQLite sink) — cosmetic rename deferred.
- Seven `feat/remove-clickhouse*` branches are unmerged and unpushed.
