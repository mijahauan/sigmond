# Host capacity planning — design discussion

**Status:** open design discussion, no policy decisions yet.
**Audience:** Rob, Michael — and future contributors arriving cold.
**Why this exists:** writing it down so we can think about it
together rather than re-derive it under pressure each time a host
falls over.

---

## The shape of the problem

A sigmond host runs a stack with very different resource personalities:

* radiod ingesting USB at the SDR's full sample rate and emitting one
  forward FFT per channel as multicast IQ.
* hf-timestd consuming several timestation channels and feeding chrony
  via SHM at sub-microsecond accuracy.
* Decoder clients (psk-recorder/jt9, wspr-recorder, hfdl-recorder,
  codar-sounder) consuming channels and emitting spots.
* Background services: web APIs, watchdogs,
  cleanup timers, sigmond infra (igmp-querier, wd-rac).

These workloads compete for three distinct resources:

1. **CPU bandwidth** — wall-time core-seconds.
2. **L2/L3 cache footprint** — radiod's FFT working set is sensitive
   to L3 pollution by big-buffer decoders. This is *separate* from
   CPU bandwidth: cores in the same CCX/CCD share L3, so a workload
   on a "different core" can still trash radiod's cache.
3. **Latency budget / preemption** — timestd's chrony SHM update
   path has a real-time deadline; if a decoder happens to be running
   when timestd needs the CPU, the kernel must preempt promptly.

Different machines have very different topologies for these
resources. Today's policy was tuned by hand for one host. We need a
generalization.

---

## The hierarchy of application importance

In rough priority order — highest first. **Higher tiers preempt
lower tiers absolutely; lower tiers fill the leftover capacity.**

1. **Realtime ingest + forward FFT.** radiod reading the SDR over
   USB and producing the channelized multicast streams. *Without
   this, nothing downstream works.* This tier is non-negotiable —
   if we run out of headroom, every other tier yields.

2. **Timing-critical clients.** hf-timestd's `core-recorder`,
   `fusion`, `l2-calibration`, and `metrology@` services. They
   consume the FFT outputs to drive chrony's timing. They are
   sample-rate processing on the channels they care about (the 9
   timestation channels at 24 kHz IQ each), so their working set
   is small relative to radiod's.

3. **Decoder clients.** jt9 (psk-recorder), wspr-recorder,
   dumphfdl (hfdl-recorder), codar-sounder. CPU-heavy and
   buffer-heavy. Slot-driven: tens of independent invocations per
   minute.

4. **Background.** Post-hoc analysis (`hf-timestd physics`,
   `iono-reanalysis`), observation surfaces (`web-api`,
   `radiod-monitor`), housekeeping (`pipeline-watchdog`, `prune`,
   `wd-spool-clean`), and sigmond infrastructure (igmp-querier,
   wd-rac). Bursty or low-rate; tolerant of arbitrary scheduling.

The current `AFFINITY_UNITS` mapping has only two buckets
(`'radiod'` and `'other'`). Tiers 2, 3, and 4 all collapse into
`'other'` and end up sharing one CPU pool. Today on bee1 that means
hf-timestd's timing math is sharing 8 cores with 30+ jt9
invocations, and `physics` (post-processing) sits on the same pool
as `core-recorder` (real-time). The mapping is too coarse.

---

## Cache isolation vs. CPU pinning — they're not the same lever

Pulling these apart explicitly because the conflation has cost us
debugging time.

**Cache isolation** is binary at the cache-island granularity. On a
2-CCX Ryzen 5700U, a workload on cores 0-7 shares L3 with everything
on cores 0-7 *no matter which specific core within that range it
runs on*. CCX0 vs CCX1 is the only knob that affects L3 pollution.

**CPU pinning** decides how many cores a tier may run on within a
cache island, and how it shares them with other tiers in the same
island.

Concrete consequence: if radiod needs cache isolation but only ~3
cores of CPU, you do not need to give it an entire 8-core CCX
exclusively. You can put light, low-cache-footprint timing services
on the same CCX without degrading radiod's L3 — and reclaim those
cores for actual work.

This insight matters for small-CCX hosts (the Ryzen 5700U we
deployed on, the Pi 5 if anyone tries one) where wasting half the
machine on cache isolation is unaffordable.

---

## What's currently in sigmond

`lib/sigmond/cpu.py`:

* `gather_capabilities()` reads `/sys/devices/system/cpu/.../cache/`
  and produces a list of `CacheIsland` objects per L2 and L3 — so we
  already have the topology we need to map tiers onto cores.
* `compute_affinity_plan()` accepts a `[cpu_affinity]` override
  (`radiod_cpus = "..."`, `other_cpus = "..."`) but otherwise has
  only one heuristic: pair radiod 1:1 with physical cores and dump
  everything else into `other_cpus`. It does not consult
  `CacheIsland` data.
* `AFFINITY_UNITS = {unit: 'radiod' | 'other'}` — two-bucket.
* `smd diag cpu-affinity --apply` writes
  `smd-cpu-affinity.conf` drop-ins per unit.

What's missing for the general case:

1. A tier-aware `AFFINITY_UNITS` mapping (`'realtime' | 'timing' |
   'decoder' | 'background'`).
2. A planner that maps tiers → CPU sets given a host's
   `CacheIsland` layout, not just the operator's hand-edited
   override.
3. Per-unit resource cost descriptors, so sigmond can warn before
   deploy when a host is over-budget rather than discover overload
   by watching jt9 OOM.

---

## What sigmond would need to know about each unit

A coarse cost descriptor — the goal is "good enough to refuse
obviously-bad deployments," not exact prediction. Rough fields:

* `tier`: `'realtime' | 'timing' | 'decoder' | 'background'`
* `cpu_sustained`: average core-seconds-per-second under normal load
* `cpu_burst`: peak core-seconds-per-second over a multi-second window
* `rss_max`: working memory ceiling (cgroup `MemoryMax`-shaped)
* `cache_footprint`: `'large' | 'medium' | 'small'` — qualitative L3
  pressure, not bytes
* `instances_per_cycle`: for slot-driven workloads, how many parallel
  child processes per cadence (e.g. jt9: N channels × 1 process per
  slot)

Open question: does this live in the contract (each client declares
its own cost) or in sigmond's catalog (sigmond annotates
externally)? The contract is more honest — costs change when
clients change — but slower to bootstrap. The catalog can be filled
in for known clients today and migrate to the contract later.

A `smd diag capacity` view would tally declared costs per host,
compare against `gather_capabilities()`, and warn if the host is
over-budget on any tier or topology assumption.

---

## Open design questions

1. **Tier-island mapping policy.** When a host has fewer cache
   islands than tiers, how do we collapse them? Today's
   ad-hoc-correct answer for a 2-CCX host is "realtime+timing on
   island A, decoder+background on island B." A 1-island host has
   to fall back to Nice-only separation. A many-island host
   (Threadripper, EPYC) opens placement choices we haven't thought
   through.

2. **What "preempt" actually means.** `Nice=-10` only affects
   SCHED_OTHER. Some of radiod's threads run SCHED_FIFO with their
   own priority logic; sigmond doesn't currently coordinate with
   that. When timing math contends with a SCHED_FIFO radiod thread
   on the same core, what's the actual outcome? Hasn't bitten us
   yet but worth modeling before we put timestd on radiod's CCX.

3. **Decoder concurrency caps.** Even with perfect tiering, jt9
   spawns enough parallel processes per slot to overrun an 8-core
   pool if decode latency exceeds slot length. We need either a
   per-client concurrency cap (`psk-recorder` self-throttles based
   on declared CPU budget) or a host-level resource controller.
   Self-throttling is simpler; host-level is fairer across clients.

4. **Heterogeneous hosts.** What about a Pi 5 ingest node feeding a
   bigger box for decoding? Sigmond currently sees each host as
   independent; capacity planning across hosts is a separate
   problem.

5. **Cost descriptor ownership.** Contract vs catalog (above).

6. **What we can measure vs what we have to declare.** Some costs
   are observable (cgroup CPU usage, RSS history). Could sigmond
   *learn* costs from observed runtime instead of taking them at
   declared face value? Probably yes for sustained CPU and RSS, no
   for cache footprint.

---

## Today's tactical state on bee1 (2026-05-09)

Concrete data point that motivates this discussion. Snapshot for
context, not a recommended pattern:

* Radiod owns CCX0 (cores 0-7) via the `[cpu_affinity]
  radiod_cpus` override.
* Everything else (decoders, all of hf-timestd including `physics`,
  sigmond infra) is pinned to CCX1 (cores
  8-15) via `AFFINITY_UNITS` role `'other'`.
* hf-timestd's timing-critical services (`core-recorder`, `fusion`,
  `l2-calibration`, `metrology@`) bumped to `Nice=-10` so they
  preempt decoders on the shared CCX1.
* psk-recorder is currently stopped — its jt9 was OOM-looping under
  the CCX1 confinement (30 spawns / 15 s window, 8 cores can't
  drain them in time, processes accumulate, 2 GB cgroup limit
  trips). See `project_psk_recorder_oom_loop.md`.

This works for radiod's USB drops but is structurally fragile: it
oversubscribes CCX1 and gives hf-timestd's `physics`
(post-processing, not timing) the same CPU pool as the timing math.
Both symptoms are symptoms of the missing tier abstraction.

---

## Suggested next steps

Not a commitment — a discussion seed.

1. **Agree on tier names and definitions** so both human-side
   conversation and code use the same vocabulary.
2. **Catalog declared costs for the existing clients** in some
   format, even if hand-written and approximate. This forces us to
   notice when we don't actually know.
3. **Sketch the tier-→-island mapping for the hosts we run today**
   (bee1, any Pi or VM hosts) and see whether the policy collapses
   sanely on each.
4. **Decide where tactical fixes stop and architectural change
   starts.** The bee1 jt9 OOM has both: a per-client concurrency
   cap is tactical and could ship now; the tier abstraction is
   architectural and deserves its own session.
