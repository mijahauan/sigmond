# CLAUDE.md — Sigmond Development Briefing

## What this project is

**Sigmond** ("Dr. SigMonD" — a play on Sigmund Freud) is the unified
installer, configurator, and lifecycle manager for the HamSCI SDR suite:
wsprdaemon, hf-timestd, ka9q-radio, ka9q-web, and future HamSCI clients.

The CLI entry point is **`smd`**.

## Authors

- Rob Robinett (AI6VN, GitHub: rrobinett) — wsprdaemon architect
- Michael Hauan (AC0G, GitHub: mijahauan) — hf-timestd / ka9q-python / wspr-recorder /ka9q-update / psk-recorder author
- Repo: https://github.com/mijahauan/sigmond

## Architecture reference

See `tui-configurator.md` for the full design — three-panel Textual TUI,
topology registry, harmonization rules, screen flow, and open questions.

Other design discussions in `docs/`:

- `HOST-CAPACITY-PLANNING.md` — workload tiers (realtime / timing /
  decoder / background), cache-island vs CPU-pinning as separate
  levers, and open questions for matching client cost to host
  topology. Discussion seed, not final policy.
- `PACKET-LOSS-DIAGNOSTICS.md` — six-layer model for tracking RTP
  gaps from kernel UDP buffer through USB starvation.
- `networking.md` — IGMP-snooping silent-failure mode and the
  igmp-querier mitigation.

## CPU pinning & the Proxmox host

Sigmond stations typically run as a **KVM guest on a Proxmox host**, and
this shapes the CPU strategy in ways not visible from inside the guest:

- **CPU frequency/governor control lives on the HOST, not the guest.**
  The guest has no `cpufreq` at all (`/sys/.../cpufreq` is absent). Any
  frequency work must be done on the Proxmox host.
- **Each local radiod runs on a hyperthread sibling PAIR.** A radiod
  instance's FFT and block threads share L1/L2 when placed on the two
  logical CPUs of one physical core — a large efficiency win, and the
  difference between clean capture and USB packet drops. So each *local*
  radiod is pinned to its own sibling pair (first local radiod → the
  first pair, a second → the next physical core's pair, etc.), and
  everything else is kept off those cores.
- **Decoder clients run on the remaining cores, never radiod's.**
  wspr-recorder / psk-recorder / hfdl-recorder decode threads pollute
  radiod's L3 and steal its cores if left unconfined — the standard
  symptom is USB packet drops. They are pinned to the worker cores via
  per-template `smd-cpu-affinity.conf` drop-ins, driven by
  `AFFINITY_UNITS` in `lib/sigmond/cpu.py`. **If you add a new decoder
  client, add it to `AFFINITY_UNITS`** or it silently runs on radiod's
  cores (this exact regression hit wspr-recorder, which was missing from
  the map).

### How it's wired

- **Host side** — `scripts/proxmox/bootstrap.sh` discovers the host's
  hyperthread pairing and computes the layout (radiod pair, worker
  cores, one HT pair reserved for the host), then `host-apply.sh`
  renders `cpu-pin-VMID.sh.template` into a Proxmox **hookscript**
  (`/var/lib/vz/snippets/cpu-pin-<VMID>.sh`, registered via
  `qm config <VMID>` → `hookscript:`). On VM `post-start` the hookscript
  (a) sets per-pCPU `scaling_max_freq` caps — radiod cores fast, worker
  cores capped so the package can sustain radiod's clock under full
  decode load — and (b) does **strict 1:1 vCPU→pCPU pinning**
  (`taskset` per `CPU N/KVM` QEMU thread). The hookscript is the single
  source of truth for host-side freq caps and pinning; **do not add a
  separate systemd freq service** — it just duplicates the hookscript.
- **Any uniform 2-way-SMT pairing is auto-configured** — sequential
  (`{0,1},{2,3},…`) *and* split (`{0,8},{1,9},…`, common on AMD/Intel).
  `host-discover.sh` emits the real sibling pairs (`HT_PAIRS`);
  `sigmond.cpu.compute_host_cpu_layout` (unit-tested) maps each local
  radiod onto a real host sibling pair and emits the vCPU→pCPU map
  (identity for sequential, interleaved for split). Set
  `LOCAL_RADIOD_COUNT=N` for a host with multiple local RX888 triplets;
  each gets its own sibling pair. Non-SMT / asymmetric hosts still fall
  back to manual config (`docs/proxmox/wsprdaemon-proxmox-cpu-clock-tuning.md`).

Verify on a host: `cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list`
(the real pairing), `qm config <VMID> | grep hookscript`, and the live
per-vCPU pin via `taskset -pc <tid>` on the QEMU `CPU N/KVM` threads.

## Cross-process upload wake (notification vs. state)

In a multi-receiver merge fleet each receiver
(`wspr-recorder@B4-100`, `@bee1`, `@bee2`) is a **separate process**
writing spots/noise into one shared `/var/lib/sigmond/sink.db`, but only
the merge instance runs the uploader. Decoders signal "cycle committed"
to the uploader through a **Unix datagram socket** —
`/var/lib/sigmond/upload-wake.sock` (the `s`-type file, group-writable by
`sigmond` so any recorder can send). See `wspr_recorder/upload_wake.py`
(`notify()` = the ping, `WakeListener` = the answering thread;
`WSPR_WAKE_DEBUG=1` traces send/receive), wired in `spot_sink.py`
(`_on_per_rx_committed` calls `notify()` on every commit) and
`hs_uploader_shim.py` (uploader binds the listener → sets the pump's wake
Event).

**The principle to preserve when touching this:** the socket is a
**stateless edge trigger** — the datagram is a content-free `b"w"` from an
*unbound* sender, so it carries **no identity and no count**; it only
means "something changed, go look." The **shared `sink.db` is the source
of truth.** On any wake the uploader *re-derives* completeness from the
sink — it never tallies pings. "All N receivers done with cycle C" is a
query (`wspr_completion.cycle_complete`): every `WD_MERGE_REPORTERS` name
must have a noise row for C (noise is written last, so its presence ==
that receiver is done). Because the trigger is decoupled from the truth,
a **lost** ping (uploader restarting → ENOENT), a **duplicate** ping
(fragmented commit), or **reordered** pings can't desync it; a 15 s
polling backstop (`WSPR_PUMP_INTERVAL_SEC`) covers missed pings and
`WD_MERGE_BACKSTOP_SEC` force-ships if a receiver dies. **Never** try to
make the socket carry per-receiver state and count to N in memory — one
lost/dup datagram would desync permanently and you'd lose the count
across restarts. Treat notifications as hints; re-derive correctness from
durable shared state.

## Developer commands

`smd` itself is stdlib-only at runtime, but the test suite and TUI need
extras. The dev venv lives at `.venv/` (separate from the installed
`/opt/git/sigmond/sigmond/venv`); `scripts/dev-setup.sh` builds it:

```bash
./scripts/dev-setup.sh        # creates .venv with [tui,dev] extras +
                              # editable ka9q-python from a sibling checkout.
                              # Safe to re-run; recreates the venv.
                              # Uses uv when available, falls back to pip+venv.
```

### Tests

`pyproject.toml` configures pytest with `testpaths = ["tests"]`. The dev
venv's pytest is the canonical runner:

```bash
.venv/bin/pytest                                  # full suite (42 test files)
.venv/bin/pytest tests/test_lifecycle.py          # one file
.venv/bin/pytest tests/test_lifecycle.py::test_X  # one test
.venv/bin/pytest -k harmonize                     # by keyword
.venv/bin/pytest -x -vv tests/test_catalog.py     # stop on first failure, verbose
```

Fixtures and shared scaffolding live in `tests/fixtures/` and `tests/conftest.py`.

### Linting & typing

No ruff / black / mypy / pre-commit configured. There is no formal
formatter or type-checker gate; match the surrounding style and the
existing stdlib-first convention. Don't introduce one without a
conversation about scope (the core-must-be-stdlib-only constraint
already forecloses most options).

### Running `smd` from the dev tree without reinstalling

`bin/smd` is plain Python and works from a checkout:

```bash
PYTHONPATH=lib ./bin/smd list           # uses your editable source tree
PYTHONPATH=lib ./bin/smd tui            # TUI also (needs .venv with [tui])
```

Or use the installed symlink (`/usr/local/bin/smd`); on a sigmond-installed
host that symlink already points at the repo, so an editable workflow is
automatic — no `pip install -e` step needed for the core. The dev venv is
only required for tests and the TUI extras.

## Core commands (implemented)

```
smd component install [<client>]    Install a client from catalog, or full-suite
                         (bare `smd install` kept as alias; `smd software install`
                         deprecated in CLI v2 — see docs/CLI-V2-SPEC.md §5)
smd apply                Reconcile running services with current config
                         (`smd software apply` deprecated in v2)
smd start                Start all managed services
smd stop                 Stop all managed services
smd restart              Restart managed services (with reset-failed)
smd reload               Reload via signal or restart (auto-routing)
smd component list       Per-component status: lifecycle + git ref + upstream
                         divergence + version policy + verdict.
                         (bare `smd list` kept as alias)
smd component update [<name>]       Pull and reconcile per topology version policy
                         (was `smd list --update`/`--apply`). Requires root.
smd component list --catalog        Show catalog of known clients (was `--available`).
smd admin log <client>         Follow journal, tail file logs
smd admin log set-level [<client>] <lvl>  Set per-client (+ SIGHUP) or global default log
                         level (was `smd admin log --level`).
smd status               Service health + client inventory enrichment
smd config show|migrate  Inspect or migrate coordination config
smd config init <c>      Invoke a client's first-run wizard (CONTRACT-v0.5 §14)
smd config edit <c>      Invoke a client's edit flow, or $EDITOR fallback
smd config init radiod   Sigmond-owned wizard: probe USB, render radiod@<id>.conf
                         per SDR, register in coordination.toml (CONTRACT-v0.5 §14.4)
smd admin validate             Cross-client harmonization rules (read-only)
smd watch ka9q           Compare pinned ka9q-radio commit vs upstream and
                         flag changes that would break RTP delivery
                         (was `smd ka9q-watch`)
smd admin diag                 Network + deps + client validation diagnostics
smd tui                  Launch interactive TUI configurator
smd admin environment list|probe|describe   Situational awareness of network peers
smd admin storage migrate-to-sqlite   Remove a leftover legacy ClickHouse
                         install once SQLite (the sole local sink) is in
                         use. Dry-run by default; --yes to execute.
                         Requires root.
smd admin storage trim         TTL-based janitor for the local SQLite sink.
                         `--all` applies per-target policies from env
                         (PSK_RETENTION_MIN=60 min); 30-min floor
                         enforced. One-shot mode: `--target-db psk
                         --max-age 2h`. Systemd timer
                         `sigmond-storage-trim-all.timer` (15 min).
smd admin verifier report      Windowed audit of upload delivery. Default
                         `--target wspr` reads wsprnet_audit (per-spot
                         delivered/lost/in_flight/rejected/silent_drop
                         cohorts + cadence). `--target psk` audits the
                         local SQLite sink for FT8/FT4 spot delivery,
                         with cadence on the 15s / 7.5s FT cycles.
```

## Sink backend selection

`sigmond.hamsci_sink.Writer.from_env()` picks the producer-side sink at
construction time:

- `SIGMOND_SQLITE_PATH` set → `Writer` at that path (override).
- Unset                    → `Writer` at `/var/lib/sigmond/sink.db`
  if writable, else no-op (preserves standalone-safety).

SQLite is the sole local sink. On a host carrying a leftover legacy
ClickHouse install, use `smd admin storage migrate-to-sqlite` to clean it up.

## Architecture layers

1. **Catalog** (`etc/catalog.toml`, `lib/sigmond/catalog.py`) — registry
   of known clients.  Answers "what could be installed?"  Three layers
   merged via sparse per-field overlay; deprecation list excludes
   retired clients from discovery.  See "Catalog layering" below.

2. **Installer** (`lib/sigmond/installer.py`) — catalog-driven install:
   clone repo to `/opt/git/sigmond/<name>`, run the client's canonical `install.sh`.
   Each client's installer is authoritative; sigmond delegates, not duplicates.

3. **Lifecycle** (`lib/sigmond/lifecycle.py`, contract v0.5 §5) — resolves
   systemd units from each client's `deploy.toml`.  Expands templated units,
   discovers instances, marks orphans.  Powers start/stop/restart/reload/list.

4. **Logging** (`lib/sigmond/log_cmd.py`, contract v0.3 §10/§11) — journal
   tailing, file-log tailing via `log_paths` from inventory, runtime log-level
   control via `coordination.env` + SIGHUP.

5. **Status/diag enrichment** — `smd status` and `smd admin diag` query each
   installed client's `inventory --json` and `validate --json` to surface
   version, channels, frequencies, modes, and validation issues.

6. **Contract adapter** (`lib/sigmond/clients/contract.py`) — generic adapter
   that shells out to `<client> inventory|validate --json` and translates to
   sigmond's internal `ClientView`.

7. **Harmonization** (`lib/sigmond/harmonize.py`) — cross-client rules:
   CPU isolation, frequency coverage, radiod resolution, timing chain.

8. **Lifecycle lock** (`lib/sigmond/lifecycle.py`, contract v0.5 §5.5) —
   flock-based mutual exclusion on `/var/lib/sigmond/lifecycle.lock`.
   Mutating verbs (install, apply, start, stop, restart, reload, list --apply)
   acquire the lock; read-only verbs (list, status, log, diag) are lock-free.

9. **Start ordering** (`lib/sigmond/lifecycle.py`, contract v0.5 §5.4) —
   `order_units()` ensures radiod starts first (if enabled), then clients
   in coordination.toml declaration order. Stop is reversed.

10. **Catalog walk install** — `smd install` (no args) iterates the catalog
    + topology. Clients with `install_script` go through the catalog path;
    C projects (radiod, ka9q-web) delegate to ka9q-update's `install-ka9q.sh`.

11. **TUI configurator** (`lib/sigmond/tui/`, Textual) — three-panel layout
    accessed via `smd tui`. Left: component tree with health indicators.
    Center: various screens (topology, install, logs, validate, cpu_affinity,
    cpu_freq, environment, gpsdo, lifecycle, apply, components, backup, restore).
    Right: contextual help. Textual is a lazy import; core smd stays stdlib-only.

12. **Environment discovery** (`lib/sigmond/commands/environment.py`,
    `lib/sigmond/discovery/`) — situational awareness of network peers:
    mDNS discovery of KIWISDRs and GPSDOs, IGMP multicast probing, NTP
    client probing, HTTP discovery. Powers `smd admin environment` and TUI screens.

13. **ka9q-radio drift watcher** (`lib/sigmond/commands/ka9q_watch.py`) —
    thin wrapper around `ka9q-python/scripts/check_upstream_drift.py`.
    Compares the pinned ka9q-radio commit against `origin/main` and
    classifies the delta as pass / warn / fail (red = stream-critical
    field shifted, RTP delivery to clients would break). Read-only, no
    sudo. Surfaced as `smd watch ka9q` (legacy `smd ka9q-watch` still
    works during the v2 deprecation window) and as the TUI Observe →
    ka9q-watch screen. Operator-triggered; no scheduler installed —
    rerun manually before deploying a new ka9q-radio build.

## Implemented TUI screens

- **Overview** — system health dashboard with component status
- **Install** — browse and install components from the catalog
- **Topology** — enable/disable components with live validation
- **Logs** — view and filter service logs (journal and file)
- **CPU affinity** — visual core map with conflict detection
- **CPU frequency** — monitor and control CPU frequencies
- **Environment** — discover and probe network peers (KIWISDRs, GPSDOs, NTP)
- **GPSDO** — monitor Leo Bodnar GPSDO health via mDNS
- **Validate** — cross-client harmonization checks
- **Lifecycle** — start/stop/restart services
- **Apply** — reconcile services with current config
- **List (Software versions)** — per-component status (git ref, upstream
  divergence, version policy) with Update All / per-component update
  buttons; replaces the old separate Update screen.
- **Backup/Restore** — backup and restore configuration
- **RAC** — Remote Access Channel (frpc tunnel) configuration
- **Config show** — dump effective coordination config
- **Diag net** — network diagnostics for multicast readiness
- **Radiod** — radiod status and channel monitoring

## Still to build

- **TUI per-client config screens** — wspr-recorder, hf-timestd, psk-recorder
  settings editors with live probing.
- **Start ordering validation** — warn if clients declare cross-client
  After=/Requires= systemd dependencies.

## Topology registry

```toml
# /etc/sigmond/topology.toml — controls what's enabled on this host
[component.radiod]
enabled = true
managed = true

[component.hf-timestd]
enabled = true

[component.psk-recorder]
enabled = true

[component.wspr-recorder]
enabled = false
```

Old topology names (`grape`, `wspr`) are accepted as aliases with deprecation
warnings.  The canonical names match `etc/catalog.toml`.

## Catalog layering

`load_catalog()` merges three layers, lowest precedence first, via
*sparse per-field overlay* — only the keys present in a higher layer
override the same keys from earlier layers; missing keys fall through:

1. **Discovery** — synthesized from each `/opt/git/sigmond/<name>/deploy.toml`.
   This is Wave 2's "drop-in client" path: clone a contract-conformant
   repo, no sigmond-side edits required.
2. **Repo default** — `etc/catalog.toml` shipped with sigmond.  Adds
   entries that can't be discovered (`ka9q-radio` has no
   `/opt/git/sigmond/` checkout), source-only dep declarations
   (`callhash`, `hs-uploader`), and `[deprecated.<name>]` blocks.
3. **Operator override** — `/etc/sigmond/catalog.toml` (per-host).
   Should contain *only* the fields a host genuinely overrides —
   e.g. `repo = "git@my-fork:foo"`.

### Why sparse overlay (and not first-file-wins)

The pre-`7d172b4` design read the first existing file and replaced
whole `CatalogEntry` objects.  An operator file that predated a new
repo-side entry silently shadowed the whole catalog, so new clients
(and source-only deps like `callhash` / `hs-uploader`) stayed
invisible until each host manually re-synced
`/etc/sigmond/catalog.toml`.  Sparse overlay makes new repo entries
propagate on `git pull` with zero per-host sync work.

### `[deprecated.<name>]` blocks

Names listed here are *excluded* from the catalog returned by
`load_catalog()`, so a stale `/opt/git/sigmond/<name>/deploy.toml`
cannot revive a removed client through discovery.  `smd list` shows
deprecated entries in a separate section when their checkout still
lingers on disk.  `smd remove <name>` is a full purge for these
names — stop+disable units, remove the deploy.toml link symlinks,
`rm -rf` source / venv / `/etc/<name>/`, plus any paths in the
block's `extra_paths = [...]` list (legacy dirs that don't follow
the `<name>`-suffix convention — e.g. wsprdaemon-client's config
lived at `/etc/wsprdaemon/`, not `/etc/wsprdaemon-client/`).

### `smd config catalog-prune`

Trims `/etc/sigmond/catalog.toml` to only the fields that diverge
from the repo file.  If the operator file ends up empty, it's
removed (sparse overlay reads the repo file directly when no
operator file exists).  A `.bak` snapshot is written before any
destructive change.

Runs automatically at the end of `install.sh` (after the smd
symlink is in place) so each upgrade trims any drift.  Safe to
re-run manually: `smd config catalog-prune` (add `--dry-run`
to preview).

### Where to edit which file

* **New entry that all hosts should see** — `etc/catalog.toml` in
  the repo.  Commit + push; other hosts pick it up on `git pull`
  thanks to sparse overlay (re-running `install.sh` not required,
  though it's also the trigger for the next prune).
* **Deprecate a retired client** — `[deprecated.<name>]` block in
  the repo's `etc/catalog.toml`.  Include `extra_paths` if the
  legacy install put files under a non-conventional name.
* **Host-specific override** — `/etc/sigmond/catalog.toml`.  Write
  *only* the diverging fields; everything else falls through to
  discovery + the repo file.  The next prune leaves your edits
  alone (it only drops keys that re-duplicate the repo value).

## Fleet upgrade pattern

Sigmond consumers (`mag-recorder`, `psk-recorder`, `wspr-recorder`,
`hf-timestd`, `codar-sounder`, `hfdl-recorder`, plus the
`hs-uploader` + `gpsdo-monitor` non-Python clients-with-CLIs) all use
**uv** ([astral.sh/uv](https://astral.sh/uv)) as both their development
and production installer.  Each per-consumer `install.sh`:

1. Sources `scripts/install/ensure_uv.sh` from this repo (with an
   inline fallback when sigmond isn't yet cloned), which guarantees
   `uv` is on `$PATH` (installing it system-wide to `/usr/local/bin`
   via the Astral installer if needed).
2. Runs `uv venv $INSTALL_DIR/venv --python 3.11 --seed --quiet` once
   (creates the per-consumer venv at `/opt/<consumer>/venv`).
3. Runs
   `UV_PROJECT_ENVIRONMENT=$INSTALL_DIR/venv uv sync --project $REPO_ROOT --frozen --no-dev --quiet`
   which reads the project's `pyproject.toml` + committed `uv.lock`,
   resolves `[tool.uv.sources]` entries to local sibling paths
   (declared as `path = "../<repo>", editable = true`), and produces
   the venv in one shot.  Editable installs of every sibling
   (`ka9q-python`, `callhash`, `hs-uploader`, the consumer itself)
   land automatically; the `editable = true` flag is what keeps the
   "single source tree, many consumers" convention from degrading to
   "many wheel snapshots, independent of upstream changes."
4. Optionally adds `sigmond` (the orchestrator, not declared in each
   consumer's pyproject because it's lazy-imported with a fallback)
   via `uv pip install --quiet --python $INSTALL_DIR/venv/bin/python3 -e /opt/git/sigmond/sigmond`.
   **Note:** `uv pip install` honors `--python`, NOT
   `UV_PROJECT_ENVIRONMENT` — the latter applies only to project-level
   commands like `uv sync` / `uv lock`.  Confusing them silently
   installs into the wrong place.

The consequence — and the deliberate design payoff — is that **a
`git pull` of any sigmond-suite library propagates to every consumer's
venv with zero further action**.  Every venv sees the new source on
disk; every venv's `importlib.metadata.version()` reflects the bumped
`pyproject.toml` immediately; no per-venv `pip install --upgrade` or
re-run of `install.sh` is needed.  `smd component update` exploits this
to drive fleet upgrades.

### The two layers to consider

1. **Source on disk.**  `git pull` (or `smd component update`).  Editable
   installs auto-track.  If a consumer ever ends up with a wheel-style
   install of a sibling (e.g. someone hand-ran `uv pip install ka9q-python`
   from PyPI rather than `uv sync`), it stops auto-tracking until the
   next `uv sync` cycle re-resolves through `[tool.uv.sources]`.
2. **Code loaded in memory.**  Python imports modules once at process
   start.  A long-running service still holds its start-time bytecode
   until restarted.  Identify stale services by `systemctl show
   -p ActiveEnterTimestamp <unit>` and compare against the library's
   change timestamps.  Restart only the stale ones to minimize
   disruption.

### Per-host commands

```bash
# Canonical, restarts everything enabled:
smd component update       # pulls all repos per topology version policy
smd restart                # restarts every enabled component

# Surgical (only what's stale; less disruption to already-fresh services):
sudo -u sigmond git -C /opt/git/sigmond/<lib> pull --ff-only
sudo systemctl restart <unit1> <unit2> ...

# After pyproject / uv.lock changes for a consumer (rare; e.g. dep added):
sudo /opt/git/sigmond/<consumer>/install.sh   # idempotent; re-runs uv sync
sudo systemctl restart <consumer>             # to load new in-memory code
```

### The shared install helper

The seven consumer `install.sh` files (`hs-uploader`, `gpsdo-monitor`,
`mag-recorder`, `psk-recorder`, `wspr-recorder`, `codar-sounder`,
`hfdl-recorder`) each source `scripts/install/ensure_uv.sh` from this
repo via:

```bash
_ENSURE_UV_SH="/opt/git/sigmond/sigmond/scripts/install/ensure_uv.sh"
if [[ -r "$_ENSURE_UV_SH" ]]; then
    source "$_ENSURE_UV_SH"
else
    _ensure_uv() { ...inline fallback... }   # keep in sync with the canonical file
fi
_ensure_uv || { ...error...; exit 1; }
```

When sigmond is present (almost always, on any host running consumers),
the helper is the single source of truth.  The inline fallback covers
the bootstrap case where an operator clones one client standalone
without sigmond — relevant for `gpsdo-monitor` and `hs-uploader` which
don't otherwise require sigmond.  If you change `ensure_uv.sh`, also
update the inline fallbacks (the changes are normally small enough
that drift isn't a practical concern).

### Identifying stale services

Editable installs make the "what version is on disk" trivial (always
HEAD of the local repo).  The harder question is "which running
processes have stale code in memory?"  The rule of thumb: a service
whose `ActiveEnterTimestamp` predates the library commit you care
about needs restart.  Concretely:

```bash
# Library commit time (UTC):
git -C /opt/git/sigmond/<lib> log -1 --format=%ci <commit>

# Each candidate service's start time:
for u in <units...>; do
  printf "  %-45s %s\n" "$u" "$(systemctl show -p ActiveEnterTimestamp --value "$u")"
done
```

### Verifying the loaded code

Python source files aren't memory-mapped (`lsof` and `/proc/PID/maps`
won't show them), so the conclusive proof is `__pycache__` mtime:

```bash
ls -la /opt/git/sigmond/<lib>/<pkg>/__pycache__/<module>.cpython-*.pyc
```

The `.pyc` is regenerated on first import after the `.py` changes.
If its mtime is at or after the library commit time, any process
started after that mtime imported the new bytecode.

### Restart-order considerations

For interdependent service groups (notably `hf-timestd`: `core-recorder`
→ `metrology@*` → `fusion` → `physics`), restart independent observers
first (`radiod-monitor`, `vtec`, `web-api`, `l2-calibration`), then
`physics`, then `fusion` last among that group.  Systemd handles
dependencies on each individual `systemctl restart`, but staggering
order reduces the size of the transient inconsistent state.

### Cross-site / cross-operator note

Each operator runs their own sigmond install on their own hardware
(AC0G's bee1, Rob Robinett's B4-100, etc.).  Topology files at
`/etc/sigmond/environment.toml` are per-site and don't reference other
operators' hosts — coordination happens via PSWS / HamSCI upstream
endpoints, not direct fleet orchestration.  When a commit message
mentions a remote host (e.g. `3a6cf26` cited B4-100), that's
contextual; cross-operator upgrades are out of scope for `smd`.

## Key constraints

- **Primary language:** Python 3.11, stdlib only for the core (`smd`).
  Textual is a runtime dep for the TUI subcommand only.
- **No wdlib dependency.** Sigmond is a separate tool; it may read
  wsprdaemon config but does not import wdlib.
- **Headless-first.** Every command must work without a terminal (for
  remote SSH, CI, and scripted installs). TUI is additive.
- **FHS-compliant paths:**
  - Config: `/etc/sigmond/`
  - Binaries: `/usr/local/bin/smd` (symlink to the repo's `bin/smd`)
  - Logs: `/var/log/sigmond/`
  - State: `/var/lib/sigmond/`

## Generic Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

1. Don’t assume. Don’t hide confusion. Surface tradeoffs.

2. Minimum code that solves the problem. Nothing speculative.

3. Touch only what you must. Clean up only your own mess.

4. Define success criteria. Loop until verified.
