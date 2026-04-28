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

## Core commands (implemented)

```
smd install [<client>]   Install a client from catalog, or full-suite (legacy)
smd apply                Reconcile running services with current config
smd start                Start all managed services
smd stop                 Stop all managed services
smd restart              Restart managed services (with reset-failed)
smd reload               Reload via signal or restart (auto-routing)
smd list [--available]   List configured units, or catalog of known clients
smd log <client>         Follow journal, tail file logs, or set log level
smd status               Service health + client inventory enrichment
smd config show|migrate  Inspect or migrate coordination config
smd config init <c>      Invoke a client's first-run wizard (CONTRACT-v0.5 §14)
smd config edit <c>      Invoke a client's edit flow, or $EDITOR fallback
smd config init radiod   Sigmond-owned wizard: probe USB, render radiod@<id>.conf
                         per SDR, register in coordination.toml (CONTRACT-v0.5 §14.4)
smd validate             Cross-client harmonization rules (read-only)
smd update               Pull latest code and re-apply
smd diag                 Network + deps + client validation diagnostics
smd tui                  Launch interactive TUI configurator
smd environment list|probe|describe   Situational awareness of network peers
```

## Architecture layers

1. **Catalog** (`etc/catalog.toml`, `lib/sigmond/catalog.py`) — static
   registry of known clients.  Answers "what could be installed?"
   Includes topology-alias bridge (grape → hf-timestd, wspr → wsprdaemon-client).

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
   Mutating verbs (install, apply, start, stop, restart, reload, update)
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
    cpu_freq, environment, gpsdo, lifecycle, apply, update, backup, restore).
    Right: contextual help. Textual is a lazy import; core smd stays stdlib-only.

12. **Environment discovery** (`lib/sigmond/commands/environment.py`,
    `lib/sigmond/discovery/`) — situational awareness of network peers:
    mDNS discovery of KIWISDRs and GPSDOs, IGMP multicast probing, NTP
    client probing, HTTP discovery. Powers `smd environment` and TUI screens.

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
- **Update** — pull latest code and re-apply
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

[component.wsprdaemon-client]
enabled = true
```

Old topology names (`grape`, `wspr`) are accepted as aliases with deprecation
warnings.  The canonical names match `etc/catalog.toml`.

## Key constraints

- **Primary language:** Python 3.11, stdlib only for the core (`smd`).
  Textual is a runtime dep for the TUI subcommand only.
- **No wdlib dependency.** Sigmond is a separate tool; it may read
  wsprdaemon config but does not import wdlib.
- **Headless-first.** Every command must work without a terminal (for
  remote SSH, CI, and scripted installs). TUI is additive.
- **FHS-compliant paths:**
  - Config: `/etc/sigmond/`
  - Binaries: `/usr/local/sbin/smd`
  - Logs: `/var/log/sigmond/`
  - State: `/var/lib/sigmond/`

## Companion project

`wsprdaemon-client` lives at `/home/wsprdaemon/wsprdaemon-client` and is
the repo Sigmond will install and manage. Its `deps.conf` is the
authoritative source for dependency commit pins.

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
