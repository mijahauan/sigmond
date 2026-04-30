# Plan: Consumer-side StreamQuality Surface (Phase 7)

**Status:** draft (2026-04-29) — review before any code lands.
**Motivation:** ka9q-python already captures rich RTP loss accounting in `StreamQuality` (sequence loss, late/duplicate packets, gap-event sources, completeness%) — see [ka9q/stream_quality.py:46-216](ka9q-python/ka9q/stream_quality.py#L46-L216). hf-timestd already accumulates it in [stream_recorder_v2.py:928-959](hf-timestd/src/hf_timestd/core/stream_recorder_v2.py#L928-L959). But the data never escapes the running daemon — no CLI, no web-api, no file. Sigmond cannot see it. This plan closes that gap so the packet-loss diagnostic loop is complete.

## Architectural choice (option A from the inventory)

**Daemon writes a snapshot file; CLI reads and emits.** Chosen because:
- Matches the existing `<binary> inventory --json` / `validate --json` contract pattern sigmond already speaks.
- CLI works on a stopped daemon (returns stale snapshot with `stale_seconds` flag).
- No new dependency direction (no HTTP from sigmond to the consumer).
- Symmetric across consumers: wspr-recorder, psk-recorder add the same subcommand.

Rejected:
- Web-api endpoint — port discovery problem, daemon-must-be-running.
- Folding into `inventory --json` — breaks the contract that inventory is config-derivable without a daemon.
- Folding into `status --json` — would change exit-code semantics that tools depend on.

## Resolved decisions to call out before code lands

1. **Snapshot path:** `/run/hf-timestd/quality.json`. Tmpfs, recreated on boot, writeable by `User=timestd` via systemd `RuntimeDirectory=hf-timestd`.
2. **Snapshot cadence:** every 5 seconds. Tradeoff: 5s aligns with sigmond's typical probe cadence; tighter wastes CPU on JSON serialisation; looser makes rate computations noisy.
3. **Atomicity:** write to `/run/hf-timestd/quality.json.tmp` then `os.replace()` — POSIX-atomic, the CLI never sees a partial file.
4. **Per-recorder vs aggregate:** the snapshot lists one entry per recorder (per channel), with an aggregate `summary` block. Consumers can already inspect per-channel via `recorder.get_quality()`.

## Resolved decisions (2026-04-29)

1. **Sigmond surfacing — option (b), via `smd diag` / `smd status`.** Consumer-flagged issues in `validate --json` already reach `smd diag`/`status` through the existing pipeline; sigmond additionally surfaces the raw structured `quality --json` payload alongside ClientView so the operator can browse it in those commands. **TUI organisation deferred** — let observation drive the layout rather than designing it up front.

2. **Threshold ownership — split.** The consumer owns the **significance** decision: hf-timestd decides what completeness% counts as broken for *its* pipeline, and emits its own `validate --json` issues at that bar. Sigmond owns **allocation among competitors** — when multiple clients report quality breaches, sigmond needs to be able to correlate ("both hf-timestd and wspr-recorder lost packets in the same window → upstream contention, not a per-consumer bug"). For now: collect, don't gate. No sigmond-side thresholds in this phase. Sigmond's job is to ingest, store, and surface so patterns become visible. Threshold enforcement / cross-client correlation is a follow-up plan once we have a real time-series of quality snapshots from a running site.

3. **Cumulative vs rate — consumer-side delta.** Snapshot writer emits both the cumulative total and the per-second rate since the previous snapshot. Sigmond stays stateless on this axis. The plan's snapshot JSON already shows both fields per recorder.

---

## Phase 7a — hf-timestd daemon snapshot writer

**Where:** [src/hf_timestd/core/core_recorder_v2.py](hf-timestd/src/hf_timestd/core/core_recorder_v2.py) — `CoreRecorderV2` (class at [:99](hf-timestd/src/hf_timestd/core/core_recorder_v2.py#L99), main loop around [:463](hf-timestd/src/hf_timestd/core/core_recorder_v2.py#L463)) owns `self.recorders: Dict[str, StreamRecorderV2]` at [:216](hf-timestd/src/hf_timestd/core/core_recorder_v2.py#L216).

**New module:** `src/hf_timestd/core/quality_snapshot.py` — ~80 LOC:
```python
class QualitySnapshotWriter:
    def __init__(self, recorders, path="/run/hf-timestd/quality.json",
                 interval_s=5.0, clock=time.time):
        ...
    def start(self) -> None:        # spawn writer thread
    def stop(self) -> None:
    def _write_once(self) -> None:  # snapshot each recorder, atomic replace
```

Snapshot JSON shape:
```json
{
  "schema_version": 1,
  "captured_at": 1714397223.456,
  "instance": "default",
  "client": "hf-timestd",
  "recorders": [
    {
      "description": "WWV_5000",
      "frequency_hz": 5000000,
      "ssrc": 1234,
      "completeness_pct": 99.97,
      "packets_received":   123456,
      "packets_lost_total": 7,
      "packets_lost_rate":  0.02,        // delta since previous snapshot, /s
      "packets_resequenced_total": 42,
      "total_gaps_filled": 2400,
      "uptime_seconds":   3600.0,
      "last_quality_age_s": 0.8,
      "batch_gap_sources": {"network_loss": 5, "resequence_timeout": 2}
    },
    ...
  ],
  "summary": {
    "min_completeness_pct": 99.97,
    "total_packets_lost":    7,
    "any_recorder_unhealthy": false
  }
}
```

**Wiring:** `CoreRecorderV2.run()` instantiates `QualitySnapshotWriter` after `self.recorders` is populated (line ~647 area), starts it, and stops it in shutdown.

**Tasks:**
- [x] New `core/quality_snapshot.py` — `QualitySnapshotWriter` is **non-threaded**, driven from the existing main loop (`tick()` called every 5s). Coupling to the loop is intentional: a hung loop produces a stale snapshot, which sigmond reads as a daemon-health signal. Atomic write via `os.replace()`. ~190 LOC.
- [x] Integrate into `CoreRecorderV2.run()` lifecycle — instantiation after recorders are populated, tick added alongside the existing `last_quota_check` 5-minute block.
- [x] `tests/test_quality_snapshot.py` — 12 tests across payload shape, delta-rate math (incl. stream-restart clamp and zero-interval handling), summary aggregation, atomic write, parent-dir creation, recorder-attribute-error tolerance.

---

## Phase 7b — hf-timestd CLI subcommand

**Where:** [src/hf_timestd/cli.py](hf-timestd/src/hf_timestd/cli.py) alongside `_handle_inventory` at [:61](hf-timestd/src/hf_timestd/cli.py#L61).

```python
def _handle_quality(args):
    """`hf-timestd quality --json` — sigmond-readable runtime stream quality.

    Reads the snapshot the running daemon writes to /run/hf-timestd/quality.json
    every ~5s.  Emits with stale_seconds so callers can detect a stopped daemon.
    """
    path = Path(getattr(args, 'snapshot_path',
                        '/run/hf-timestd/quality.json'))
    if not path.exists():
        print(json.dumps({
            "client": "hf-timestd",
            "error": "snapshot_missing",
            "snapshot_path": str(path),
        }))
        sys.exit(0)            # not a CLI failure — just no daemon
    payload = json.loads(path.read_text())
    payload["stale_seconds"] = round(time.time() - payload.get("captured_at", 0), 2)
    print(json.dumps(payload, indent=2))
```

Argparse wiring near the existing inventory/validate parsers around [:640](hf-timestd/src/hf_timestd/cli.py#L640).

**Tasks:**
- [x] Add `_handle_quality` and argparse subcommand at [cli.py](hf-timestd/src/hf_timestd/cli.py) alongside inventory/validate/status.
- [x] `--snapshot-path` override added (default `/run/hf-timestd/quality.json`).
- [x] `tests/test_cli_quality.py` — 4 tests: missing-file, malformed JSON, fresh snapshot with `stale_seconds`, zero-`captured_at` defensive case.
- [x] End-to-end smoke test: `python3 -m hf_timestd.cli quality --snapshot-path …` works against a real file.

---

## Phase 7c — hf-timestd systemd unit

**Where:** [systemd/timestd-core-recorder.service](hf-timestd/systemd/timestd-core-recorder.service).

Add a single line:
```ini
RuntimeDirectory=hf-timestd
```
This makes systemd create `/run/hf-timestd/` owned by `User=timestd` on each service start, and clean it up on stop. No ExecStartPre needed.

**Tasks:**
- [x] Added `RuntimeDirectory=hf-timestd` to the service unit alongside `WorkingDirectory`. Systemd creates `/run/hf-timestd/` owned by `User=timestd` on each start, cleaned up on stop. No ExecStartPre needed.
- [x] `User=timestd` unchanged (line 12).
- **Operator note:** the unit needs `systemctl daemon-reload` + `systemctl restart timestd-core-recorder` for the directive to take effect. Rolling restart on bee1 is the deploy step.

---

## Phase 7d — sigmond ContractAdapter extension

**Where:** [lib/sigmond/clients/contract.py](sigmond/lib/sigmond/clients/contract.py).

Add a `read_quality(client) -> Optional[dict]` method that shells out to `<binary> quality --json`. Mirrors existing `read_view()` shape — same subprocess pattern, 5s timeout, returns `None` on missing subcommand (graceful degradation for clients that don't implement it yet).

**Tasks:**
- [x] `read_quality()` added to `ContractAdapter` ([contract.py](sigmond/lib/sigmond/clients/contract.py)). Subprocess pattern matches `read_view()` / `validate_native()`; all failure modes (no binary, timeout, non-zero exit, malformed JSON, non-dict top-level) silently return None — a client without the subcommand is not a sigmond-level issue.
- [x] `ClientView.quality: Optional[dict]` added ([base.py:52](sigmond/lib/sigmond/clients/base.py#L52)). `ContractAdapter.read_view()` populates it via `read_quality()` after the existing parse.
- [x] 7 unit tests in `ReadQualityTests` covering all failure modes + happy path + error-payload pass-through.
- [x] Live-validated against running daemon on bee1: `read_quality()` returns fresh snapshot with `stale_seconds=1.22`, summary block populated.

---

## Phase 7e — sigmond surfacing

Per resolved decisions: sigmond does **not** apply its own quality thresholds in this phase. Two narrow integration points:

1. **Stale-snapshot guard** — the only sigmond-emitted issue. If `quality --json` reports `stale_seconds > 30`, that means the daemon's writer thread isn't running, which is a sigmond-level concern (process supervision) rather than a consumer-significance call.

2. **Surface raw quality in `smd status` / `smd diag`** — extend the status output to include the `quality` payload per client. Operator can read completeness%, packets_lost_rate, batch_gap_sources directly. No filtering, no thresholding — let the operator see everything until patterns emerge.

```python
# In ContractAdapter or a new helper:
def validate_native(self) -> list:
    issues = self._existing_validate_native()
    q = self.read_quality()
    if q and q.get("stale_seconds", 0) > 30:
        issues.append({
            "severity": "warn",
            "message": f"quality snapshot stale: {q['stale_seconds']}s "
                       f"(daemon snapshot writer may be stalled)",
        })
    return issues

# And: extend ClientView with a `quality: Optional[dict]` field that
# `smd status` displays under each client.
```

Consumer-flagged issues (hf-timestd's own significance calls) already flow through the existing `validate --json` channel — no sigmond change needed for that path.

**Tasks:**
- [x] Stale-snapshot guard added to `ContractAdapter.validate_native()`. Threshold `QUALITY_STALE_THRESHOLD_S = 30.0` (6× writer cadence). Emits a single `severity: warn` issue when `stale_seconds > 30`. Sigmond-level supervision check; per-recorder thresholds remain consumer-owned.
- [x] `ClientView.quality` populated from `read_quality()` (covered by Phase 7d).
- [ ] ~~Update `smd status` / `smd diag` output to render the `quality` block~~ — deferred per "TUI organisation to follow" decision (2026-04-29). The validate-issue path already flows the stale-snapshot warning through both commands; richer rendering waits until field experience says what to surface.
- [x] 6 integration tests in `QualityIntegrationTests` covering stale → warn, fresh → silent, missing-subcommand → silent, native-issue pass-through.
- [x] Live-validated: against the running daemon, `validate_native()` returns no issues (snapshot fresh, no native problems).

---

## Phase 7f — Documentation

**Tasks:**
- [ ] Add a "Consumer-side stream quality" section to [docs/PACKET-LOSS-DIAGNOSTICS.md](sigmond/docs/PACKET-LOSS-DIAGNOSTICS.md) — what `hf-timestd quality --json` shows, how to read the rate fields, what the validate-issue thresholds catch.
- [ ] Update [docs/CLIENT-CONTRACT.md](sigmond/docs/CLIENT-CONTRACT.md) (or [docs/CONTRACT-v0.5-DRAFT.md](sigmond/docs/CONTRACT-v0.5-DRAFT.md)) with a §16 describing the optional `quality --json` subcommand: shape, cadence, freshness contract.

---

## Out of scope (explicitly)

- **wspr-recorder / psk-recorder snapshot writers.** Once hf-timestd lands, those follow the same pattern; that's a separate plan per consumer.
- **A sigmond-side rate cache for consumer quality.** The snapshot writer computes its own delta-rate (Open Question 3 recommendation), so sigmond is stateless on this dimension.
- **Per-recorder TUI display.** Phase 7e surfaces breaches as validate issues; a richer TUI per-client quality panel is a follow-up.
- **Backfilling validate issues for runs where the snapshot file is rotated mid-run.** Quality is a now-state, not a history.

## Estimated scope

| Phase | LOC | Tests | Repo |
|---|---|---|---|
| 7a — daemon writer | ~80 | ~12 | hf-timestd |
| 7b — CLI subcommand | ~30 | ~4 | hf-timestd |
| 7c — systemd unit | ~1 | (ops verification) | hf-timestd |
| 7d — ContractAdapter | ~50 | ~6 | sigmond |
| 7e — quality surfacing | ~30 | ~5 | sigmond |
| 7f — docs | n/a | n/a | sigmond |

**Total:** ~190 LOC, ~27 tests, split roughly 60/40 between the two repos. Comparable to one Wave or one major Phase from the local-resources work.

## Why this plan, not just code

- Cross-repo work — touching hf-timestd's daemon, CLI, and systemd unit is non-trivial and the operator may have constraints (deploy windows, permission to add `RuntimeDirectory`).
- Three real architectural choices are still open (1, 2, 3) and the wrong call on any of them is rework.
- The first consumer (hf-timestd) sets the pattern the second and third will copy. Worth getting the shape right before locking it in.
