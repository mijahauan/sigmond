# Timing-chain architecture — chrony / gpsd / hf-timestd

**Status:** Design (proposed 2026-06-06). Step 1 (quiet the storm) applied to the
AC0G/sigma host the same day; steps 2–4 not yet built.

The GPSDO → gpsd → chrony → hf-timestd timing stack on a HamSCI station kept
turning a single failure into a 2–3 component failure. This documents *why* and
the architecture that makes recovery idempotent and non-cascading.

## The failure we hit

On sigma, the local GPS reference (Leo Bodnar LG290P) kept dropping out and the
system fell back to internet NTP. Root cause was not any one component but a
destructive feedback loop:

1. gpsd writes GPS time to SysV SHM `NTP0` (+ PPS); chrony reads it (`refclock
   SHM 0`). Whichever of gpsd/chrony creates the segment first owns it — so a
   chrony restart re-created `NTP0` as root-0600 and **locked gpsd out** → GPS
   reach 0.
2. hf-timestd's `timestd-fusion.service` ran, on every start,
   `ExecStartPre=systemctl stop chrony`, `ipcrm -M NTP0/NTP1` (deleting gpsd's
   SHM outright), and `ExecStartPost=systemctl restart chrony`.
3. hf-timestd's watchdogs (`pipeline-watchdog`, `chrony-monitor` running
   `check-chrony-reach.sh`, `hpps/hfps-watchdog`) restarted fusion and/or chrony
   whenever the `FUSE`/`HPPS` chrony sources were missing — and those sources are
   missing because chrony has no `refclock SHM` wired to read hf-timestd's
   solution. So the checks could never pass → endless restarts (13 chrony
   restarts in 30 min), each nuking the GPS reference.

## The anti-pattern (three coupled mistakes)

1. **Recovery by restarting a *shared* dependency.** hf-timestd "recovered" by
   restarting chrony — which gpsd also depends on. A component power-cycled
   infrastructure it doesn't own, breaking the other producer.
2. **Non-deterministic resource ownership.** SHM-segment ownership depended on
   start order, so every restart was *destructive* instead of idempotent.
3. **Health checks wired to blind restarts.** "FUSE/HPPS reach 0" → restart
   chrony, when the real problem was hf-timestd's own feed not being wired in.
   Dueling watchdogs amplified it into a storm.

## The four rules

1. **Stable interfaces, not lifecycle coupling.** Producers (gpsd, hf-timestd)
   write to chrony's SHM; chrony consumes. With a fixed SHM contract (stable unit
   numbers + perms, pre-created) every process can restart independently and
   non-destructively. The coupling that hurts is one process restarting another.
2. **Own-only recovery.** A component may restart only itself or resources it
   *exclusively owns* — never a shared dependency. hf-timestd must never
   `systemctl restart chrony`; if its feed is missing it restarts its own writer
   or asks chrony to `reload`.
3. **One reconciler owns the chain.** Replace the competing watchdogs with a
   single idempotent reconciler that knows the whole graph and fixes the
   *specific* broken link. It is the only actor allowed to act on the chain.
4. **Deterministic ordering + per-unit self-heal.** Express the dependency in
   systemd (`gpsd → chrony → hf-timestd`), give each unit `Restart=on-failure`,
   and pre-create the SHM with fixed perms before any of them.

## Target architecture

```
GPSDO (LG290P) ──► gpsd ──► SHM[0]=GPS, SHM[1]=PPS ──┐
   (gpsdo-monitor observes lock/sats)                 ├─► chrony ─► system clock
hf-timestd fusion ──► SHM[2]=FUSE, SHM[3]=HPPS ───────┘   (restarted by NOBODY
                                                            except the reconciler)
```

- **SHM contract:** a oneshot pre-creates units 0–3 (mode 0666) ordered before
  gpsd/chrony/hf-timestd. gpsd writes 0/1, hf-timestd writes 2/3, chrony reads
  all four as refclocks. No ownership race. (Interim alternative: run gpsd as
  root so chrony restarts can't lock it out.)
- **chrony:** config carries all four refclocks (adds the missing
  `refclock SHM 2 refid FUSE` / `SHM 3 refid HPPS`). Config changes apply via
  `chronyc reload` / `systemctl reload` — **never restart**.
- **hf-timestd:** *publishes* to its SHM units and never touches chrony's
  lifecycle. Remove the chrony stop/restart + `ipcrm` from `timestd-fusion`;
  remove the `restart chrony` from `check-chrony-reach.sh`; watchdogs may restart
  fusion but may not touch chrony or gpsd's SHM.
- **Reconciler (`smd admin timing`):** a timer + on-demand verb (the natural home given
  `smd apply` is already sigmond's idempotent reconciler) that walks the chain
  top-down and fixes exactly one link — GPSDO unlocked → alert; gpsd not writing
  SHM → restart gpsd; chrony missing a refclock / not selecting GPS → rewrite
  config + `chronyc reload`, chrony dead → restart chrony once; FUSE/HPPS absent
  → restart fusion only.
- **`smd admin validate`:** reports the whole chain's health (observability), separate
  from remediation.

Result: any single failure → the reconciler restores the desired state of the
whole chain idempotently, and because the interfaces are stable, no restart
cascades into its neighbours.

## Implementation plan

- **Step 1 — quiet the storm (DONE 2026-06-06, host-level, reversible):** stop +
  disable the chrony-bouncing timers (`timestd-pipeline-watchdog`,
  `timestd-chrony-monitor`, `timestd-hpps-watchdog`, `timestd-hfps-watchdog`);
  drop-in on `timestd-fusion.service` neutralising its chrony stop/restart +
  `ipcrm` (keeping only the legitimate mkdir/chown ExecStartPre); re-establish
  the GPS reference. Stabilises the system so the GPS fix holds while steps 2–4
  are built.
- **Step 2 — stable contract (DONE on host 2026-06-06; sigmond `51f0367`):**
  `sigmond-shm-precreate.service` (ordered Before gpsd/chrony/fusion) creates
  NTP0-3 at root:0666 — **GPS now survives chrony restarts** (LG29/PPS reach
  climb back to 377, PPS Stratum 1; before, they stuck at reach 0). Removed the
  conflicting `chrony After=gpsd` drop-in (it formed a cycle with gpsd's stock
  `After=chronyd`; the pre-create makes ordering moot for SHM ownership). Added
  the `FUSE`(SHM1)/`HPPS`(SHM2) refclocks to `/etc/chrony/conf.d/` — chrony reads
  them, no SHM-0 collision with gpsd (the current code writes unit=1/2; only the
  stale install.sh/docs claimed unit 0). `Restart=on-failure` self-heal added to
  gpsd/chrony/fusion/recorder.
  - **Caveat:** FUSE/HPPS are wired but still at reach 0 — the hf-timestd
    fusion/recorder aren't *feeding* their SHM units after restart (sysv_ipc is
    present; it's a pipeline-readiness matter, not a chrony/contract problem).
    Not blocking — GPS is the reference.
  - **Repo durability DONE (hf-timestd `10a4df0`):** `timestd-fusion.service`
    no longer stops/restarts chrony or `ipcrm`s the SHM (own-only recovery);
    `check-chrony-reach.sh` is report-only (never restarts chrony);
    `install.sh` installs `config/chrony-timestd-refclocks.conf` (FUSE=SHM1,
    HPPS=SHM2) to `/etc/chrony/conf.d/` instead of the `TSL1`-on-SHM-0 append,
    and no longer installs the backwards `chronyd-timestd-shm.conf` ordering
    drop-in. A fresh hf-timestd install no longer reintroduces the cascade.
- **Step 3 — the reconciler (DONE 2026-06-06, sigmond `2ae05d2`):**
  `smd admin timing [status|reconcile] [--dry-run]` — `lib/sigmond/commands/timing.py`
  probes the chain (shm/gpsd/gps-feed/chrony/fuse/metrology) and `reconcile()`
  applies OWN-ONLY remediation (gps-feed broken→restart gpsd; chrony dead→restart
  chrony, the ONLY allowed chrony-restarter; FUSE down via metrology→start the
  metrology writers, never chrony). `sigmond-timing-reconcile.timer` runs it every
  3 min (ConditionFileIsExecutable=/usr/sbin/gpsd). 7 hermetic tests. Replaces the
  hf-timestd watchdogs (which stay disabled). Verified on sigma: status 6/6
  healthy, reconcile a clean no-op.
- **Step 4 — observability (DONE 2026-06-06, sigmond `HEAD`):** `rule_timing_reference`
  in `harmonize.py` (ALL_RUNTIME_RULES) folds the chain into `smd admin validate` —
  read-only, pointing failures at `smd admin timing reconcile`; skips without a local
  gpsd. `smd admin validate` now shows `timing_reference: selected PPS (stratum 1)`.

**Roadmap COMPLETE.** The anti-pattern is designed out: stable SHM contract +
own-only recovery + a single reconciler + deterministic ordering, observable via
`smd admin validate` and remediated via `smd admin timing reconcile`.

This spans sigmond + hf-timestd but uses the same reconcile philosophy sigmond
already applies (`apply` / `validate`), extended to own the timing chain.
