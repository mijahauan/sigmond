# TUI Configurator — Proposal for Review

**Authors:** AC0G (Michael Hauan), with input from Claude  
**Date:** 2026-04-09  
**Status:** Draft — awaiting review by Rob (AI6VN)

## Motivation

The v4 rewrite decomposes wsprdaemon into independent, service-oriented
components: radiod (via ka9q-radio), wspr-recorder, hf-timestd, and future
HamSCI clients.  Each component owns its own configuration file and can run
standalone.  But when several components share a single receiver chain,
their configurations must agree on frequencies, radiod addresses, CPU
affinity, timing sources, and disk budgets.

A TUI (terminal user interface) can guide users through this, providing
contextual help, live system probes, and cross-component validation —
without replacing any component's native config format.

### Name discussion

The suite now encompasses wsprdaemon alongside other current and future
HamSCI interests (time-standard recording, ionospheric physics, etc.).
A rename is under consideration — something that references **HamSCI**
rather than wsprdaemon alone.  Candidate names TBD; the TUI and its
surrounding tooling should adopt whatever name is chosen.  Throughout this
document, "the suite" refers to the collection of managed components
regardless of final branding.

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

4. **No wdlib dependency contamination.**  The TUI is a separate binary
   with its own dependencies (e.g., Textual).  The core `wdlib` library
   remains stdlib-only.

---

## Architecture

### Topology registry

A lightweight manifest tells the TUI what is installed and where each
component's config lives:

```toml
# ~/.config/wd-suite/topology.toml  (path TBD after rename)

[radiod]
host = "k3lr-rx888.local"            # "localhost" when co-located
managed = false                       # TUI won't start/stop it
status_dns = "k3lr-rx888-status.local"
cores = [0, 1, 2, 3]                 # cores reserved for radiod (if local)

[wspr-recorder]
config = "/etc/wsprdaemon/wspr-recorder.toml"
managed = true

[hf-timestd]
config = "/etc/hf-timestd/config.toml"
managed = true

[wsprdaemon]
config = "/etc/wsprdaemon/wsprdaemon.conf"
managed = true

# Future clients register the same way:
# [fst4w-recorder]
# config = "/etc/fst4w-recorder/config.toml"
# managed = true
```

The TUI reads each component's native config through a per-format
reader/writer module.  Components never see the topology file.

### Three-panel TUI layout

```
┌───────────────────────────────────────────────────────────────┐
│  suite configurator                            [H]elp  [Q]   │
├───────────────┬──────────────────────┬────────────────────────┤
│ Components    │ Settings             │ Context / Guidance     │
│               │                      │                        │
│ ▸ Topology    │ radiod host:         │ If radiod runs on a    │
│   radiod      │  ○ This machine      │ separate machine, CPU  │
│   wspr-rec    │  ● Remote host       │ affinity is not managed│
│   hf-timestd  │                      │ here — the remote      │
│   CPU Pins    │ Hostname:            │ radiod admin controls  │
│   Validate    │  [k3lr-rx888.local]  │ its own core isolation.│
│   Deploy      │                      │                        │
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
(green/yellow/red) next to each entry.

**Center panel** — settings for the selected component.  Widget types:
radio buttons, text fields, frequency pickers, core-map selectors.

**Right panel** — contextual intelligence:
- Plain-language explanation of the current setting
- Live system state (radiod reachable? clock offset? disk free?)
- Conflict warnings when settings in one component affect another

### Screen flow

Users navigate these screens roughly in order, though any screen is
accessible at any time:

1. **Topology** — where is radiod?  Local, remote, or multiple instances?
   This single decision gates CPU affinity management and network health
   checks.

2. **radiod** (if local and managed) — hardware, sample rate, front-end
   gain, CPU core assignment shown as a visual core map.

3. **wspr-recorder** — band selection via a visual HF spectrum with WSPR
   sub-bands highlighted; output directory with disk-space indicator;
   per-channel gain.

4. **hf-timestd** — timing authority (rtp / fusion / auto), compression,
   physics pipeline toggle, archive path with quota visualization.  Shows
   current clock offset if the service is running.

5. **CPU Affinity** — the cross-cutting orchestration screen:

```
CPU Core Map (8 cores)
┌────┬────┬────┬────┬────┬────┬────┬────┐
│ C0 │ C1 │ C2 │ C3 │ C4 │ C5 │ C6 │ C7 │
│████│████│    │    │░░░░│░░░░│░░░░│    │
│ RD │ RD │    │    │ WR │ HF │ DEC│    │
└────┴────┴────┴────┴────┴────┴────┴────┘
████ = radiod (isolated, SCHED_FIFO)
░░░░ = other suite components
     = available

⚠  wspr-recorder decode threads overlap with radiod
   on core 2.  This may cause sample drops at high
   sample rates.  Move decode to cores 4-7?
   [Yes, move]  [No, keep]
```

When radiod is remote, this screen simplifies to local components only
and notes that radiod affinity is managed elsewhere.

6. **Validate** — runs all cross-component checks (see harmonization
   rules below) and presents a pass/warn/fail summary.

7. **Deploy** — shows a diff of what will change in each config file,
   then offers: `[Write configs]` → `[Restart changed services]` →
   `[Verify health]`.

### Harmonization rules

The TUI's core value is codified knowledge about how components interact.
These rules live in Python code, not in config:

| Rule | Components | Check |
|------|-----------|-------|
| Status DNS consistency | all | Every component's radiod reference resolves to the same instance |
| Frequency coverage | wspr-rec, hf-timestd | Requested frequencies fall within radiod's ADC bandwidth |
| CPU isolation | radiod, all others | radiod cores do not overlap with any other component's affinity mask |
| Timing chain | hf-timestd, wspr-rec | If hf-timestd is enabled, wspr-recorder uses its calibration output |
| Disk budget | wspr-rec, hf-timestd | Combined write rates fit within storage capacity over the retention window |
| Channel count | all ka9q-python clients | Total dynamic channels do not exceed radiod's configured limit |

### Live probing

The right-panel context data comes from:

- **ka9q-python** `RadiodControl` — radiod status, active channels, load
- **hf-timestd** — `/run/wsprdaemon/{instance}/hftime.json` for clock offset
- **systemd** — `systemctl is-active` for service state
- **OS** — `os.sched_getaffinity()`, `/proc/cpuinfo` for actual CPU topology and pinning
- **Filesystem** — `shutil.disk_usage()` for storage headroom

### Implementation notes

- **Framework:** [Textual](https://textual.textualize.io/) — Python,
  mouse+keyboard, runs in any terminal, CSS-like styling.  Runtime
  dependency of the TUI tool only.
- **Config I/O:** Per-component reader/writer modules that understand
  each native format (TOML, INI).  No meta-format.
- **Entry point:** A standalone script (e.g., `bin/wd-configure`) in the
  suite repo, installed into `/opt/wsprdaemon/venv/bin/`.

---

## What this demonstrates

The TUI doubles as a showcase.  A new user configuring the system for the
first time sees radiod channels appearing in real time via ka9q-python,
clock offsets updating from hf-timestd, and recording health from
wspr-recorder.  The system is visibly *working* as they configure it —
far more convincing than documentation alone.

---

## Open questions

- [ ] Suite rename: candidates referencing HamSCI (e.g., `hamsci-suite`,
      `hamsci-sdr`, `hamsci-tools`)?  Name affects paths, package name,
      systemd unit prefixes, and this TUI's branding.
- [ ] Should the topology registry be auto-discovered from installed
      systemd units, or manually authored?
- [ ] Should the TUI support headless / scripted mode (e.g.,
      `wd-configure --validate --json`) for CI or remote management?
- [ ] Where does the TUI live — in this repo, or in a new top-level
      suite repo that depends on all components?
