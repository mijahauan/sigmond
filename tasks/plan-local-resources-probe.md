# Plan: Local-Resources Probe + NIC/IGMP Coverage

**Status (2026-04-29):** Phases 1, 2, 4, 6 shipped (302/0/35 → 372/1/35; +63 tests, 0 regressions).
Phases 3 and 5 deferred — Phase 3 needs a managed switch to talk to,
Phase 5 was absorbed into 2b/4 except for SNMP test extensions which
are part of Phase 3.  See per-phase Tasks lists below for what landed.
The 1 pre-existing harmonize failure (ka9q-python ↔ ka9q-radio commit
pin drift) is unrelated.
**Motivation:** packet-loss diagnostics in the RX888 → radiod → RTP → consumer pipeline. See conversation context for the suspect list. This plan covers only the sigmond observability gap; radiod's own input/output drop counters and per-RTP-stream gap counters at consumers are out of scope.

## Goal

Make a sigmond snapshot sufficient to localize multicast / FFT / USB packet loss to one of: kernel UDP buffer, host NIC, switch port, IGMP querier, host CPU contention, or USB controller — without re-running `ethtool` / `mpstat` / `dmesg` by hand.

## Counters to capture (success criteria)

| Suspect layer | Counter | Where it lands |
|---|---|---|
| Kernel UDP | `RcvbufErrors`, `InErrors`, `InCsumErrors` | `local_system` obs |
| Host NIC | `rx_missed_errors`, `rx_no_buffer_count`, `rx_fifo_errors`, `rx_dropped`, `multicast` | `local_system` obs |
| CPU contention | per-core `%usr`, `%sys`, `%soft`, `%idle`; `freq_mhz`; C-state residency | `local_system` obs |
| IRQ pinning drift | per-IRQ per-core counts, classified by handler name | `local_system` obs |
| USB | URB error counts, overruns, reset counts (RX888 specifically) | `local_system` obs |
| Switch port | `ifInDiscards`, `ifOutDiscards`, `ifInErrors`, `ifOutErrors` (per port) | `network_device` obs |
| IGMP | querier presence, version, last-seen — per VLAN where snooping is on | `igmp_querier` / `igmp_snooper` obs |

## Architectural calls (decide before coding)

1. **NIC counters live under `local_system`, not `network_device`.** They are host-syscall observations; `network_device` stays SNMP-only. Shared `NicCounters` field schema lets a future TUI overlay them.
2. **One new source: `local_resources`.** Single localhost target, like the existing [discovery/usb_sdr.py](lib/sigmond/discovery/usb_sdr.py) pattern. Emits one `Observation(kind="local_system", source="local_resources", …)` whose `fields` carry the structured payload. Avoids per-NIC explosion in `targets_for_source`.
3. **Reuse existing `snmp` source for switch port + IGMP fields.** No new source.
4. **No persistent agent.** Probe runs on `smd environment probe` like every other source; cadence enforced by `RateLimiter`. Future change to background agent is a separate plan.

---

## Phase 1 — Declarative shape: `DeclaredLocalSystem`

Extend [environment.py:96-103](lib/sigmond/environment.py#L96-L103):

```python
@dataclass
class DeclaredLocalSystem:
    id: str = "localhost"
    cpu_affinity: list = field(default_factory=list)
    cpu_governor: str = ""
    sdrs: list = field(default_factory=list)
    expect: dict = field(default_factory=dict)
    # NEW — observation targets
    nics: list = field(default_factory=list)              # ["eth0", "enp1s0"]
    usb_devices: list = field(default_factory=list)       # ["1d50:6150"] vendor:product
    irq_pins: dict = field(default_factory=dict)          # {"xhci_hcd": [2,3], "eth0": [4,5]}
```

Update parser at [environment_kinds.py:283-293](lib/sigmond/environment_kinds.py#L283-L293) to read `nics`, `usb_devices`, `irq_pins` from the TOML row.

Update `_local_system_is_declared` at [environment_kinds.py:296-300](lib/sigmond/environment_kinds.py#L296-L300) to include the new fields in the truthiness check.

**TOML shape (operator-facing):**
```toml
[local_system]
cpu_affinity = [...]
cpu_governor = "performance"
sdrs = ["rx888-0"]
nics = ["eth0"]
usb_devices = ["1d50:6150"]   # RX888 vendor:product
irq_pins = { xhci_hcd = [2, 3], eth0 = [4, 5] }

[local_system.expect]
udp_rcvbuf_errors_rate_max = 0
nic_rx_missed_errors_rate_max = 0
softirq_percent_max = 30
irq_pin_drift_allowed = false
```

**Tasks:**
- [x] Add fields to `DeclaredLocalSystem` dataclass.
- [x] Extend `_parse_local_system`.
- [x] Update `_local_system_is_declared`.
- [x] Add fixture + test in [tests/test_environment_loader.py](tests/test_environment_loader.py) for the new TOML keys.

---

## Phase 2 — Probe: `discovery/local_resources.py` (new file)

New module, ~200 LOC, mirrors [discovery/usb_sdr.py](lib/sigmond/discovery/usb_sdr.py) for shape (single `localhost` target, returns `list[Observation]`).

**Public signature** (per [discovery/__init__.py:5-7](lib/sigmond/discovery/__init__.py#L5-L7)):
```python
def probe(env, *, timeout, limiter,
          read_proc=_read_proc_default,
          run_ethtool=_run_ethtool_default,
          read_sysfs=_read_sysfs_default,
          clock=time.time) -> list[Observation]:
```

Three injected transports so tests don't touch `/proc` or shell out.

**Internal collectors** (each returns a flat dict, all merged into `Observation.fields`):
- `_collect_cpu(read_proc)` — diff two `/proc/stat` snapshots over `interval_s`. Pure stdlib parse.
- `_collect_udp(read_proc)` — parse `/proc/net/snmp` `Udp:` line. Delta vs. cached previous reading (cache in `RateLimiter._last`-adjacent dict, or persist in env cache).
- `_collect_nics(declared_nics, run_ethtool)` — for each NIC in `declared.nics`, run `ethtool -S <nic>`, grep the five field names listed above. Subprocess pattern matches [clients/contract.py](lib/sigmond/clients/contract.py) `_run`.
- `_collect_irqs(declared_irq_pins, read_proc)` — parse `/proc/interrupts`, sum per-core counts for handlers matching declared keys, compare to `irq_pins` map; emit drift if observed cores differ.
- `_collect_usb(declared_usb_devices, read_sysfs)` — for each `vendor:product`, walk `/sys/bus/usb/devices/*` to find matching device, read `urbnum` and any error counters in sysfs. (RX888-specific firmware counters: punt to a follow-up if the sysfs path is non-trivial.)

**Output shape** — one `Observation` per probe call:
```python
Observation(
    source="local_resources",
    kind="local_system",
    id="localhost",
    endpoint="localhost",
    fields={
        "cpu_per_core": [{"core": 0, "usr": 12.3, "soft": 4.1, ...}, ...],
        "udp": {"rcvbuf_errors": 17, "in_errors": 0, "interval_s": 60.0},
        "nics": {"eth0": {"rx_missed_errors": 0, "rx_no_buffer_count": 0, ...}},
        "irqs": {"xhci_hcd": {"observed_cores": [2,3], "drift": false}, ...},
        "usb": {"1d50:6150": {"urbnum": 1024, "errors": 0}},
    },
    ok=True,
)
```

**Cadence:** add `"local_resources": 60.0` to `DEFAULT_CADENCE` at [discovery/\_\_init\_\_.py:29-39](lib/sigmond/discovery/__init__.py#L29-L39).

**Dispatch:** add to `ALL_SOURCES` and `ACTIVE_SOURCES` tuples at [discovery/\_\_init\_\_.py:46-49](lib/sigmond/discovery/__init__.py#L46-L49); add the `local_resources` branch to `module_for_source` at [discovery/\_\_init\_\_.py:57-86](lib/sigmond/discovery/__init__.py#L57-L86) and to `targets_for_source` at [discovery/\_\_init\_\_.py:89-111](lib/sigmond/discovery/__init__.py#L89-L111) returning `["localhost"]`.

**Tasks:**
- [x] Create `lib/sigmond/discovery/local_resources.py`.
- [x] Implement `_collect_cpu`, `_collect_udp`, `_collect_nics`, `_collect_irqs`, `_collect_usb`.
- [x] Wire delta-vs-previous caching for `udp` (need a previous-snapshot store; reuse the env cache file or add a sidecar).
- [x] Register source in `discovery/__init__.py` (cadence, ALL_SOURCES, ACTIVE_SOURCES, dispatch, targets).
- [x] Document new TOML keys in `docs/` (operator-facing).

---

## Phase 3 — Extend `discovery/snmp.py` for switch ports + IGMP

[discovery/snmp.py](lib/sigmond/discovery/snmp.py) is 119 lines today and queries `sysDescr`/`sysUpTime`/`sysName`/interface count. Extend to also return:

**For `network_device` observations:**
- `IF-MIB::ifInDiscards.<port>`, `ifOutDiscards.<port>`, `ifInErrors.<port>`, `ifOutErrors.<port>` per interface index.
- Land in `Observation.fields["ports"] = [{"index": 1, "in_discards": 0, ...}, ...]`.

**For `igmp_querier` / `igmp_snooper` observations** (currently no probe — they declare-only):
- `IGMP-STD-MIB` querier address, version, last-change time.
- `IGMP-SNOOPING-MIB` (vendor-specific; varies by switch — implement the Cisco/HP/Mikrotik OIDs first; gracefully degrade on unknown).
- Emit as `Observation(kind="igmp_querier", source="snmp", …)` and same for snooper.

**Targets for IGMP probes:** `targets_for_source` at [discovery/\_\_init\_\_.py:89-111](lib/sigmond/discovery/__init__.py#L89-L111) currently has no branch for `igmp_querier`/`igmp_snooper`. Either:
- Add new source names `snmp_igmp_querier` / `snmp_igmp_snooper` (verbose), OR
- Have the existing `snmp` source emit observations for all three kinds when the same host appears in multiple declared lists.

Recommendation: stick with the existing `snmp` source. The probe receives `env`, so it can iterate `env.network_devices`, `env.igmp_queriers`, `env.igmp_snoopers` and emit accordingly. No new source needed.

**Tasks (deferred — Phase 3 is on hold per 2026-04-29 decision):**
- [ ] Add ifTable port-counter walk to `discovery/snmp.py`.
- [ ] Add IGMP-STD-MIB querier query.
- [ ] Have `snmp.probe` iterate IGMP querier/snooper declarations and emit per-kind observations.
- [ ] ~~Skeleton vendor-OID dispatch for snooping; ship Cisco-only first~~ — operator decision: wait.

---

## Phase 4 — Expect-classifiers (reconciler integration)

[discovery/reconciler.py](lib/sigmond/discovery/reconciler.py) drives the per-kind `expect_classifier` from [environment_kinds.py](lib/sigmond/environment_kinds.py). Today neither `local_system` nor `network_device` has a classifier registered ([environment_kinds.py:359-364](lib/sigmond/environment_kinds.py#L359-L364), [:375-381](lib/sigmond/environment_kinds.py#L375-L381)).

Add two classifiers in [environment_kinds.py](lib/sigmond/environment_kinds.py) alongside `_radiod_classifier`:

```python
def _local_system_classifier(declared, good_obs: list) -> tuple:
    expect = declared.expect or {}
    merged = {}
    for o in good_obs:
        merged.update(o.fields)
    udp_rate = (merged.get("udp", {}).get("rcvbuf_errors", 0)
                / max(merged.get("udp", {}).get("interval_s", 1), 1))
    if udp_rate > expect.get("udp_rcvbuf_errors_rate_max", float("inf")):
        return "degraded", f"UDP RcvbufErrors rate {udp_rate:.2f}/s exceeds max"
    # ... same shape for nic_rx_missed_errors, softirq_percent, irq_pin_drift
    return "healthy", ""

def _network_device_classifier(declared, good_obs: list) -> tuple:
    # similar — checks ifInDiscards/ifOutDiscards rate against expect
```

Wire into the `KindSpec` entries at [environment_kinds.py:359-364](lib/sigmond/environment_kinds.py#L359-L364) and [:375-381](lib/sigmond/environment_kinds.py#L375-L381).

**Tasks:**
- [x] Implement `_local_system_classifier`.
- [ ] Implement `_network_device_classifier` — deferred with Phase 3.
- [x] Wire `_local_system_classifier` into `REGISTRY`. Network-device wiring deferred.
- [ ] Add an `igmp_querier` classifier — deferred with Phase 3.

---

## Phase 5 — Tests

Match the existing layout in [tests/](tests/):

- [x] `tests/test_discovery_local_resources.py` — 35 tests across parse correctness, delta math, IRQ drift, and full probe integration.
- [ ] Extend `tests/test_discovery_probes.py` with the new SNMP fields — deferred with Phase 3.
- [x] Extend `tests/test_environment_loader.py` for the new `nics` / `usb_devices` / `irq_pins` / `expect.*` TOML keys (round-trip + filter behavior).
- [x] `tests/test_local_system_classifier.py` — 15 tests covering classifier rules + reconciler integration. Network-device path deferred with Phase 3.
- [x] `tests/test_discovery_cache.py` — 9 tests for the cache-schema extension (Phase 2a).

**Baseline:** memory says cumulative was **302/0/35 (Wave 3 complete)**. Target: net additions, zero regressions.

---

## Phase 6 — Documentation

- [x] Add a `docs/PACKET-LOSS-DIAGNOSTICS.md` cookbook.
- [x] Add `etc/environment.example.toml` template with the new declarable keys (commented examples).
- [ ] ~~Add a paragraph to `tui-configurator.md`~~ — skipped: that doc currently has no Environment-screen section at all, so the paragraph would be a larger scope expansion than this plan warrants.

---

## Out of scope (explicitly)

- Persistent time-series storage (Prometheus / TSDB). The probe writes to the existing on-disk cache as a single snapshot; trend analysis is a future plan.
- Background agent that polls without operator command. Today every probe runs on `smd environment probe`. A daemon mode is a separate design.
- Radiod-internal counters (input/output drops). These belong to the radiod adapter, not environment discovery.
- RTP sequence-gap counters at consumers (hf-timestd, ka9q-web). These belong inside the consumer projects and are reported via the contract adapter's `inventory --json`.
- Any code change that requires a non-stdlib import in core sigmond (per CLAUDE.md "stdlib only for the core").

## Resolved decisions (2026-04-29)

1. **UDP delta caching** — extend the existing `ENVIRONMENT_CACHE` schema. Add a top-level `previous_local_resources` key holding the last `/proc/net/snmp` and `/proc/interrupts` snapshots. Touched in [discovery/\_\_init\_\_.py](lib/sigmond/discovery/__init__.py) `save_cache` / `load_cache`.
2. **USB error counters** — first cut parses `dmesg --since 1min` only. RX888 firmware control-endpoint counters deferred to a follow-up.
3. **IGMP snooping vendor OIDs** — defer entirely. Phase 3 ships only the standard `IGMP-STD-MIB` querier query and the ifTable port counters. Snooper observations remain declare-only until a non-Cisco environment forces the conversation.
4. **Source naming** — `local_resources`. The existing `"local"` enum entry at [environment.py:176](lib/sigmond/environment.py#L176) is from an earlier draft and is unused as a probe source today.

## Estimated scope

- Phase 1: ~30 LOC + 3 tests
- Phase 2: ~250 LOC + ~18 tests (new probe module)
- Phase 3: ~120 LOC delta in `snmp.py` + ~6 tests
- Phase 4: ~80 LOC across 2 classifiers + 4 tests
- Phase 5: covered above
- Phase 6: docs only

**Total:** roughly 480 LOC, 31 new tests. Comparable to Wave 3's ~210 LOC + 23 tests.
