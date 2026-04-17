# TUI Configurator — Design & Status

**Authors:** AC0G (Michael Hauan), AI6VN (Rob Robinett), with input from Claude
**Date:** 2026-04-09 (original); 2026-04-17 (revised)
**Status:** Initial skeleton implemented; per-client screens and CPU affinity
TUI planned.

## Motivation

The v4 rewrite decomposes wsprdaemon into independent, service-oriented
components: radiod (via ka9q-radio), wspr-recorder, hf-timestd, psk-recorder,
wsprdaemon-client, and future HamSCI clients.  Each component owns its own
configuration file and can run standalone.  But when several components share
a single receiver chain, their configurations must agree on frequencies,
radiod addresses, CPU affinity, timing sources, and disk budgets.

A TUI (terminal user interface) can guide users through this, providing
contextual help, live system probes, and cross-component validation —
without replacing any component's native config format.

---

## Design principles

1. **Each component's config file is authoritative.**  The TUI reads and
   writes native formats (TOML, INI) — it never invents a meta-format.
   A user who prefers `$EDITOR` can ignore the TUI entirely.

2. **Standalone-safe.**  If only hf-timestd is installed, the TUI works
   with just that component.  Cross-component rules that reference missing
   components are skipped.

3. **Show, don't just ask.**  The right panel displays live state — radiod
   channel count, clock offset, disk usage — so users see the system
   working as they configure it.

4. **No wdlib dependency contamination.**  The TUI is a separate package
   (`lib/sigmond/tui/`) with Textual as a lazy runtime dependency.
   Core `smd` remains stdlib-only.

---

## Architecture

### Entry point

```
smd config edit
```

Textual is lazy-imported — if not installed, smd prints a clear error
and exits.  All other smd commands work without Textual.

### Three-panel TUI layout

```
┌───────────────────────────────────────────────────────────────┐
│  Dr. SigMonD — Configurator                    [H]elp  [Q]   │
├───────────────┬──────────────────────┬────────────────────────┤
│ Components    │ Settings             │ Context / Guidance     │
│               │                      │                        │
│ ☰ Topology    │ radiod host:         │ If radiod runs on a    │
│ ✔ radiod      │  ○ This machine      │ separate machine, CPU  │
│ ✔ hf-timestd  │  ● Remote host       │ affinity is not managed│
│ ✔ psk-recorder│                      │ here — the remote      │
│ ✘ wspr-rec    │ Hostname:            │ radiod admin controls  │
│ ✔ Validate    │  [k3lr-rx888.local]  │ its own core isolation.│
│               │                      │                        │
│               │ Status DNS:          │ The status DNS name is │
│               │  [k3lr-rx888-status. │ how ka9q-python        │
│               │   local          ]   │ discovers radiod's     │
│               │                      │ control channel.       │
│               │ Managed by suite:    │                        │
│               │  [No]                │ ──────────────────     │
│               │                      │ Current: radiod is     │
│               │                      │ reachable  ✓           │
│               │                      │ 4 channels active      │
│               │                      │ Load: 12% on rx888    │
└───────────────┴──────────────────────┴────────────────────────┘
```

**Left panel** — component tree, always visible.  Health indicators
(green ✔ / red ✘) next to each enabled component, based on
`systemctl is-active`.

**Center panel** — active screen (topology editor, radiod status,
validate results, etc.).

**Right panel** — contextual help and live system state for the
current selection.

**Keybindings:** `t` = topology, `r` = radiod, `v` = validate, `q` = quit.

---

## Screens

### Implemented

#### 1. Topology

Enable/disable components for this host.  Shows all components from the
catalog (merged with topology.toml) as a table with enabled/managed
toggles.  Selecting a row toggles enabled.  Save button writes to
`/etc/sigmond/topology.toml`.

**Data source:** `topology.load_topology()`, `catalog.load_catalog()`.

#### 2. Radiod Status (live, via ka9q-python)

Coordinator-level view of radiod — shows what sigmond cares about, not
the full deep-dive:

- **Active channels:** SSRC, frequency (MHz), preset, sample rate, SNR
- **Frontend health:** GPSDO lock, calibration ppm, reference Hz,
  AD overrange, LNA/IF gain

Data comes from ka9q-python's `discover_channels()` and
`RadiodControl.poll_status()`.  Connection uses `status_dns` from
`coordination.toml`.

**Deep dive:** A button suspends sigmond's TUI and launches
ka9q-python's full 8-panel TUI (`ka9q tui <status_dns>`) for detailed
radiod control.  Sigmond resumes when ka9q tui exits.

**Data source:** `ka9q.RadiodControl`, `ka9q.discover_channels()`.

#### 3. Validate

Runs all six cross-client harmonization rules and displays color-coded
results (green pass, yellow warn, red fail).  Re-run button refreshes
after configuration changes.

**Data source:** `harmonize.run_all()`, `sysview.build_system_view()`.

### Planned

#### 4. CPU Affinity

Visual core map showing per-component CPU assignments, based on the
CPU affinity system Rob built (`smd diag cpu-affinity`):

```
CPU Core Map (16 logical CPUs, 8 physical cores)
┌────┬────┬────┬────┬────┬────┬────┬────┐
│ C0 │ C1 │ C2 │ C3 │ C4 │ C5 │ C6 │ C7 │  physical
│ +8 │ +9 │+10 │+11 │+12 │+13 │+14 │+15 │  HT siblings
│████│    │░░░░│░░░░│░░░░│░░░░│░░░░│░░░░│
│ RD │    │ WR │ HF │ PSK│ WD │    │    │
└────┴────┴────┴────┴────┴────┴────┴────┘
████ = radiod (per-instance: one physical core + HT sibling)
░░░░ = other suite components (remaining cores)
     = available

Radiod assignment:
  radiod@k3lr-rx888.service  →  CPUs 0, 8 (physical core 0)

Other services:  CPUs 2-7, 10-15
  timestd-core-recorder.service, psk-recorder@default.service,
  wd-ka9q-record@default.service, ...

⚠  radiod has 47 threads — 3 have non-process affinity
   (use smd diag cpu-affinity --threads for details)

[Apply]  [Refresh]
```

This screen will visualize and control what `smd diag cpu-affinity`
already does via CLI:

- **Read HT topology** from `/sys/devices/system/cpu/cpuN/topology/thread_siblings_list`
- **Assign radiod instances** to physical cores (core 0 for first instance,
  core 1 for second, etc. — both HT siblings per core)
- **Assign remaining cores** to all other managed services
- **Show per-thread affinity** within each service (radiod's internal
  threads may override systemd via `sched_setaffinity()`)
- **Apply** writes `smd-cpu-affinity.conf` drop-ins with both
  `CPUAffinity=` and `AllowedCPUs=` (cgroup-enforced, defeats radiod's
  `sched_setaffinity()`), removes foreign drop-ins from hf-timestd and
  wd-ctl, and disables the hf-timestd affinity watcher

**Data source:** `_compute_affinity_plan()`, `_get_physical_cores()`,
`_thread_affinity_groups()` from `bin/smd`.

#### 5. CPU Frequency

Show and apply per-CPU `scaling_max_freq` limits:

- **Radiod CPUs:** high-performance (default 3200 MHz)
- **Other CPUs:** power-efficient (default 1400 MHz)

Configurable via `[cpu_freq]` in `topology.toml` or CLI flags.

**Data source:** `cmd_diag_cpu_freq()` from `bin/smd`.

#### 6. Per-Client Config Screens

Per-component settings editors for wspr-recorder, hf-timestd,
psk-recorder, wsprdaemon-client.  Each reads/writes the client's native
config format (TOML) with contextual help and live state.

Stretch goal — depends on each client exposing enough structure via
`inventory --json` for the TUI to present meaningful controls.

#### 7. Deploy

Config diff preview showing what will change in each file, then:
`[Write configs]` → `[Restart changed services]` → `[Verify health]`.

---

## Live probing

The right-panel context data and radiod screen come from:

- **ka9q-python** `RadiodControl` / `discover_channels()` — radiod
  status, active channels, frontend health, SNR
- **Client contract** `<client> inventory --json` — version, channels,
  frequencies, modes, issues
- **systemd** — `systemctl is-active` for service state
- **OS** — `/sys/devices/system/cpu/*/topology/thread_siblings_list` for
  HT sibling grouping; `/proc/<pid>/status` for per-thread CPU affinity;
  `/sys/devices/system/cpu/*/cpufreq/` for frequency limits
- **Filesystem** — `shutil.disk_usage()` for storage headroom

---

## Harmonization rules

The TUI's core value is codified knowledge about how components interact.
These rules live in Python code (`lib/sigmond/harmonize.py`), not in config:

| Rule | Components | Check |
|------|-----------|-------|
| Status DNS consistency | all | Every component's radiod reference resolves to the same instance |
| Frequency coverage | wspr-rec, hf-timestd, psk-rec | Requested frequencies fall within radiod's ADC bandwidth |
| CPU isolation | radiod, all others | radiod cores do not overlap with any other component's affinity mask |
| Timing chain | hf-timestd, wspr-rec | If hf-timestd is enabled, wspr-recorder uses its calibration output |
| Disk budget | wspr-rec, hf-timestd | Combined write rates fit within storage capacity over the retention window |
| Channel count | all ka9q-python clients | Total dynamic channels do not exceed radiod's configured limit |

---

## CPU affinity architecture (smd-owned)

Sigmond owns all CPU affinity policy for the station.  This supersedes
per-client affinity management (hf-timestd's `setup-cpu-affinity.sh`,
wd-ctl's drop-ins).

### Design

- **Radiod gets dedicated physical cores.** Each radiod instance is
  assigned one physical core (both HT siblings).  First instance →
  core 0, second → core 1, etc.

- **Everything else shares remaining cores.** All other managed services
  (hf-timestd, psk-recorder, wspr-recorder, wsprdaemon-client, ka9q-web)
  get the pool of non-radiod cores.

- **Enforcement is cgroup-based.** Drop-ins set both `CPUAffinity=`
  (initial placement) and `AllowedCPUs=` (cgroup-enforced ceiling).
  `AllowedCPUs=` defeats radiod's internal `sched_setaffinity()` calls
  that would otherwise override systemd's `CPUAffinity=`.

- **Foreign policies are removed.** `smd diag cpu-affinity --apply`
  removes drop-ins written by hf-timestd (`cpu-affinity.conf`) and
  wd-ctl (`99-wdctl-cpu-affinity.conf`), and disables the
  `timestd-radiod-affinity.path` watcher that would recreate them.

### Configuration

```toml
# /etc/sigmond/topology.toml

[cpu_affinity]
radiod_cpus = ""      # auto-computed from HT topology if empty
other_cpus  = ""      # auto-computed as complement of radiod_cpus

[cpu_freq]
radiod_max_mhz = 3200   # high-performance for radiod cores
other_max_mhz  = 1400   # power-efficient for everything else
```

### CLI

```
smd diag cpu-affinity              # show current state
smd diag cpu-affinity --threads    # show per-thread detail
smd diag cpu-affinity --apply      # take ownership + write drop-ins
smd diag cpu-freq                  # show frequency policy
smd diag cpu-freq --apply          # write scaling_max_freq
```

---

## Implementation files

```
lib/sigmond/tui/
├── __init__.py                    # launch() entry point (lazy import)
├── app.py                         # SigmondApp — 3-panel layout, keybindings
├── screens/
│   ├── __init__.py
│   ├── topology.py                # ✔ enable/disable components + save
│   ├── radiod.py                  # ✔ live status via ka9q-python + deep dive
│   ├── validate.py                # ✔ harmonization rule runner
│   ├── cpu_affinity.py            # planned — visual core map + apply
│   └── cpu_freq.py                # planned — frequency limit viewer + apply
└── widgets/
    ├── __init__.py
    ├── component_tree.py          # ✔ left panel tree with health indicators
    └── context_panel.py           # ✔ right panel contextual help
```

### Dependencies

- **Core smd:** Python 3.11, stdlib only.
- **TUI:** Textual (lazy import, only for `smd config edit`).
- **Radiod screen:** ka9q-python (optional; graceful degradation if absent).
- **Dev venv:** `.venv/` with textual + pytest for development.

### Reused modules

| Module | TUI usage |
|--------|-----------|
| `sysview.build_system_view()` | Canonical data assembly for validate |
| `topology.load_topology()` | Component state for topology screen |
| `coordination.load_coordination()` | Radiod addresses for radiod screen |
| `catalog.load_catalog()` | Available clients for topology merge |
| `harmonize.run_all()` | Validation rules for validate screen |
| `ka9q.RadiodControl` | Live radiod queries for radiod screen |
| `ka9q.discover_channels()` | Active channel enumeration |

---

## Open questions

- [ ] Should the TUI support headless / scripted mode (e.g.,
      `smd config edit --validate --json`) for CI or remote management?
- [ ] Should the CPU affinity screen extract `_compute_affinity_plan()`
      and friends from `bin/smd` into `lib/sigmond/cpu.py` for reuse?
- [ ] Per-client config screens: how much can we derive from
      `inventory --json` vs. needing per-client TUI knowledge?
