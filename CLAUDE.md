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

## Core commands (implemented)

```
smd software install [<client>]  Install a client from catalog, or full-suite
smd software apply               Reconcile running services with current config
                                 (`smd install` / `smd apply` remain as aliases)
smd start                Start all managed services
smd stop                 Stop all managed services
smd restart              Restart managed services (with reset-failed)
smd reload               Reload via signal or restart (auto-routing)
smd list                 Per-component status: lifecycle + git ref + upstream
                         divergence + version policy + verdict.
smd list --update         Pull and reconcile per topology version policy
                         (was the separate `smd update`). Requires root.
smd list --catalog       Show catalog of known clients (was --available).
smd log <client>         Follow journal, tail file logs, or set log level
smd status               Service health + client inventory enrichment
smd config show|migrate  Inspect or migrate coordination config
smd config init <c>      Invoke a client's first-run wizard (CONTRACT-v0.5 §14)
smd config edit <c>      Invoke a client's edit flow, or $EDITOR fallback
smd config init radiod   Sigmond-owned wizard: probe USB, render radiod@<id>.conf
                         per SDR, register in coordination.toml (CONTRACT-v0.5 §14.4)
smd validate             Cross-client harmonization rules (read-only)
smd ka9q-watch           Compare pinned ka9q-radio commit vs upstream and
                         flag changes that would break RTP delivery
smd diag                 Network + deps + client validation diagnostics
smd tui                  Launch interactive TUI configurator
smd environment list|probe|describe   Situational awareness of network peers
smd storage migrate-to-sqlite   Remove a leftover legacy ClickHouse
                         install once SQLite (the sole local sink) is in
                         use. Dry-run by default; --yes to execute.
                         Requires root.
smd storage trim         TTL-based janitor for the local SQLite sink.
                         `--all` applies per-target policies from env
                         (PSK_RETENTION_MIN=60 min); 30-min floor
                         enforced. One-shot mode: `--target-db psk
                         --max-age 2h`. Systemd timer
                         `sigmond-storage-trim-all.timer` (15 min).
smd verifier report      Windowed audit of upload delivery. Default
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
ClickHouse install, use `smd storage migrate-to-sqlite` to clean it up.

## Architecture layers

1. **Catalog** (`etc/catalog.toml`, `lib/sigmond/catalog.py`) — static
   registry of known clients.  Answers "what could be installed?"
   Includes topology-alias bridge (grape → hf-timestd).

2. **Installer** (`lib/sigmond/installer.py`) — catalog-driven install:
   clone repo to `/opt/git/sigmond/<name>`, run the client's canonical `install.sh`.
   Each client's installer is authoritative; sigmond delegates, not duplicates.

3. **Lifecycle** (`lib/sigmond/lifecycle.py`, contract v0.5 §5) — resolves
   systemd units from each client's `deploy.toml`.  Expands templated units,
   discovers instances, marks orphans.  Powers start/stop/restart/reload/list.

4. **Logging** (`lib/sigmond/log_cmd.py`, contract v0.3 §10/§11) — journal
   tailing, file-log tailing via `log_paths` from inventory, runtime log-level
   control via `coordination.env` + SIGHUP.

5. **Status/diag enrichment** — `smd status` and `smd diag` query each
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
    client probing, HTTP discovery. Powers `smd environment` and TUI screens.

13. **ka9q-radio drift watcher** (`lib/sigmond/commands/ka9q_watch.py`) —
    thin wrapper around `ka9q-python/scripts/check_upstream_drift.py`.
    Compares the pinned ka9q-radio commit against `origin/main` and
    classifies the delta as pass / warn / fail (red = stream-critical
    field shifted, RTP delivery to clients would break). Read-only, no
    sudo. Surfaced as `smd ka9q-watch` and as the TUI Observe →
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
re-run of `install.sh` is needed.  `smd list --update` exploits this
to drive fleet upgrades.

### The two layers to consider

1. **Source on disk.**  `git pull` (or `smd list --update`).  Editable
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
sudo smd list --update          # pulls all repos per topology version policy
sudo smd restart                # restarts every enabled component

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
