# Session 2026-06-12 — Client resilience hardening sweep

**Goal:** take the high-priority installed sigmond clients toward production
readiness — testing and fixing resilience, edge cases, fall-back, recovery, and
anything else that threatens reliable long-running operation.

**Clients in scope:** `hf-timestd`, `wspr-recorder`, `psk-recorder`,
`mag-recorder`.

**Method:** systematic per-client audit on the live station `sigma`. Fixes were
edited in each repo (via `sudo`, since worktrees are service-user-owned),
deployed live (`daemon-reload` + targeted restarts), and verified — each fix was
reproduced/failing first, then confirmed fixed, with no false-positives on the
live config. Changes committed per-repo (authored *Michael Hauan*, Claude
co-author).

> **Repo ownership gotcha:** each client's worktree files are chowned to its
> service user (`timestd`/`wsprrec`/`pskrec`) but `.git` is owned by `sigmond`.
> Edit files with `sudo` (in-place truncate preserves owner); **stage/commit as
> the `.git` owner** (`sudo -u <owner> git -C <repo> …`). Committing as the
> worktree owner fails on `.git/index.lock`.

---

## What shipped, per client

### hf-timestd — failed-unit hygiene + alerting (`7cef200`, `d393798`)
- **IONEX download** now skips cleanly (exit 0) when NASA Earthdata credentials
  are unconfigured — IONEX is *optional* enrichment, not a fault. Was leaving a
  permanently-`failed` unit + repeating OnFailure alert on every timer tick. A
  real download failure *with* creds present still exits 1 (retry + alert).
- **chrony-monitor** `SuccessExitStatus=1` so a degraded HF refclock feed
  (FUSE/HPPS reach below threshold — the normal poor-antenna / GPS-fallback
  state) is a probe *finding*, not a unit failure. Exit 2 (chrony down /
  refclocks absent) still fails; the WARNING is still journaled each run.
- Added `OnFailure=timestd-alert@%n.service` to five always-restart services
  (`l2-calibration`, `metrology@`, `radiod-monitor`, `vtec`, `web-api`) that
  were crash-restarting silently with no operator signal.
- Removed the obsolete `timestd-sqlite-parity` units (HDF5↔SQLite dual-write
  verification for a migration that completed at Phase 4; the `parity_check_all.sh`
  ExecStart was never committed). Marked `docs/HDF5-TO-SQLITE-MIGRATION.md`
  COMPLETE.

*Verified-good (no change needed):* fusion correctly **stops** feeding chrony
when it has no measurements (→ chrony marks the refclock unreachable → clean GPS
fallback); core-recorder self-restarts on a 5-min data stall; disk-full ENOSPC
is caught; chronyc/gpsd subprocess calls all have timeouts.

### wspr-recorder — placeholder fail-fast + watchdog (`71f64fd`, `8ae2548`)
- **Unconfigured-radiod placeholder fail-fast.** A freshly-seeded config carries
  sigmond's `<configure-via-config-init>` radiod status placeholder, which can
  never resolve. The `Type=notify` daemon aborted in `connect()`,
  `Restart=always` respawned it, and it hammered ~10 restarts to StartLimit
  lockout. Now: detect the placeholder at startup, log a clear "run config init"
  message, **exit `EX_CONFIG` (78)** with `RestartPreventExitStatus=78` so
  systemd stops cleanly — no crash-loop. Transient radiod-down is NOT caught
  (keeps retrying, correct for boot ordering). `validate` also flags it.
- Fixed a latent bug: `cli._handle_daemon` swallowed the daemon's exit code, so
  the existing fatal `return 1` path silently exited 0.
- **Progress-tied watchdog** (see cross-cutting patterns below).

### psk-recorder — fully hardened (`463b155`, `ad039fe`, `e89af70`, `966d5c8`)
- **Placeholder fail-fast** (same pattern as wspr).
- **Decode-timeout** (`slot.py`): a hung `decode_ft8` (e.g. on a corrupt WAV)
  sat in `_pending_procs` forever, leaking two stdio FDs + the spool WAV each;
  across ~19 channels that grows until the `MemoryMax=2G` cgroup OOM-kills the
  daemon and `Restart=always` re-enters the same state. Now the reap loop
  kills+reaps any decode past `DECODE_TIMEOUT_SEC=60`, counts it failed, and
  cleans up. `_kill_proc` closes the pipes immediately.
- **Thread supervision**: the cycle-batcher writer, lifetime-keepalive, and stats
  loops were bare daemon threads — an unexpected exception killed them silently
  (batcher worst: spots stop, `_batches` grows unbounded). A shared `_supervise`
  wrapper logs loudly + auto-restarts the loop with capped backoff.
- **Progress-tied watchdog** (see below).
- **SQLite busy-timeout** — *verified already handled*: the shared
  `sigmond.hamsci_sink` has `journal_mode=WAL`, a 30s busy-timeout, and
  buffer-retention-on-failure; 0 "database is locked" events in 24h. No change
  warranted (would be redundant churn).

### mag-recorder — stderr-deadlock fix (`594d999`)
- `_mag_usb_source` spawned `mag-usb` with `stderr=subprocess.PIPE` that nothing
  ever drained; over a long run the ~64 KiB pipe buffer fills, `mag-usb` blocks
  on its next stderr write, stops emitting stdout → no samples → the supervisor's
  watchdog ping stops → systemd `WatchdogSec` kill. Now inherits stderr to the
  journal + logs `mag-usb`'s exit code. (Client is inactive/hardware-pending; no
  venv on host, so its pytest suite was not run — flagged.)

---

## Cross-cutting hardening patterns (the reusable checklist)

These are the patterns every sigmond client (including new ones) should adopt:

1. **Fail fast on permanent misconfiguration, not crash-loop.** Detect known
   "unconfigured" sentinels (`<configure-via-config-init>`) at startup, log a
   clear remediation message, and `sys.exit(78)` (`EX_CONFIG`). Put
   `RestartPreventExitStatus=78` on the unit so systemd stops cleanly.
   *Distinguish permanent (misconfig) from transient (radiod booting) — keep
   retrying the latter for correct boot ordering.* Also surface it in `validate`.

2. **Type=notify watchdog tied to real progress, not an unconditional timer.**
   A shared, pure, clock-injected `_ProgressGate` pets `WATCHDOG=1` only while a
   data-path progress signal advances; it withholds the ping once progress
   stalls past `stall_sec` (< `WatchdogSec`), so a *wedged* (not crashed) daemon
   gets restarted. Enforcement begins only after the first advance; a longer
   startup-grace covers dead-from-start; **any uncertainty (error → `None`)
   always pings** (fail-safe — never false-kill a healthy recorder). Heartbeat =
   the most signal-independent counter available (wspr/psk: RTP samples received
   or slots processed — advances regardless of band activity).

3. **Supervise background threads.** Wrap each long-lived daemon-thread loop in a
   `_supervise(name, alive, fn, *args)` that logs loudly + auto-restarts the loop
   with capped backoff on an unexpected exception. Convert silent thread death
   into a loud, self-healing restart.

4. **Bound every subprocess.** Decoder forks (`decode_ft8`, `jt9`, `wsprd`) and
   external binaries (`mag-usb`) must have a timeout / kill-deadline, must have
   their stdio pipes drained or inherited (never an unread `PIPE`), and must be
   reaped (no zombies / FD leaks).

5. **Graceful, prompt shutdown.** Honor SIGTERM, drain in-flight work, close
   writers on their owning thread (avoid SQLite thread-affinity errors), so a
   `systemctl stop`/restart doesn't need `SIGKILL`.

6. **SQLite sink discipline.** Use the shared `sigmond.hamsci_sink` (WAL + 30s
   busy-timeout + buffer-retention). Do all writes from a single dedicated writer
   thread (psk's `CycleBatcher` pattern) to respect SQLite thread-affinity.

7. **systemd hygiene.** `OnFailure=` alerting on every always-restart service;
   `SuccessExitStatus=` for probes whose "finding" exit is not a fault; oneshot
   timer scripts must exit 0 on *expected/optional* conditions (missing optional
   creds, no data yet) — never leave a permanently-`failed` unit for a
   non-fault.

8. **Verify rather than assume.** Several audited "problems" were already handled
   (fusion fallback, hamsci_sink busy-timeout). Check the live system before
   writing a fix; report "already mitigated" rather than adding redundant churn.

---

## Deployment & verification posture

All recorder fixes are editable-install, so they activate on the next restart.
This session restarted `psk-recorder@AC0G=S` (decode-timeout → supervision →
watchdog) and `wspr-recorder@AC0G=S` (placeholder + exit-code + watchdog),
verifying each: provisioning, `READY=1`, 0 restarts, spots/samples flowing, and
a 4-minute post-deploy monitor of the watchdog change confirming **0
false-withholding** across both danger windows. hf-timestd's two formerly-failed
units are green; 0 priority-client units failed.

## Deferred (tracked, all LOW / hardware-pending)
- **wspr:** `wspr.noise` flush hits a SQLite thread-affinity error on *shutdown*
  (Writer conn created in a worker thread, flushed from the shutdown thread → 1
  noise row lost per restart; once ever, 0 on the running instance). Plus
  `on_stream_restored` `os._exit(75)` WAV-orphan edge, transient-vs-permanent
  DNS classification, `.tmp` WAV cleanup on startup.
- **mag:** `READY=1` sent before first sample; startup watchdog race if
  `mag-usb` first-sample > `WatchdogSec(30s)`.
