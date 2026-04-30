# Packet-Loss Diagnostics

When RTP streams from radiod show sequence gaps, when channels stutter,
or when downstream consumers (hf-timestd, ka9q-web, recorders) report
missing samples — this is the diagnostic loop.

The symptom (an RTP gap at a consumer) can be produced by any of six
layers: kernel UDP buffer, host NIC, switch port, IGMP, host CPU
contention, or USB controller starvation.  Sigmond collects host-side
counters from every layer it can reach and surfaces threshold breaches
as `degraded` deltas.

## Quick start

Add a `[local_system]` section to `/etc/sigmond/environment.toml` (see
`etc/environment.example.toml` for a full template).  Minimum config to
get a useful first pass on a typical RX888-on-a-Beelink host:

```toml
[local_system]
nics = ["eth0"]                                # NIC the multicast lands on
usb_devices = ["1d50:6150"]                    # RX888 vendor:product
irq_pins = { xhci_hcd = [2, 3], eth0 = [4, 5] }

[local_system.expect]
udp_rcvbuf_errors_rate_max = 0                 # any UDP loss = degraded
softirq_percent_max = 30                       # any core above 30% = degraded
irq_pin_drift_allowed = false                  # IRQs must land on the cores above
```

Then:

```bash
smd environment probe --source=local_resources
smd environment           # see healthy/degraded for local_system
```

Run it twice — the first probe records a baseline snapshot, the second
computes rates over the interval between them.

## What gets captured

Every `local_resources` probe emits one observation with five field
groups.  Each maps to a packet-loss suspect.

| Field | Source | Suspect it diagnoses |
|---|---|---|
| `udp.rcvbuf_errors_rate` | `/proc/net/snmp` | Kernel UDP buffer overrun — most common multicast loss cause |
| `udp.in_errors_rate` | `/proc/net/snmp` | Other UDP-layer kernel errors |
| `cpu_per_core[i].soft` | `/proc/stat` | Softirq contention on the NIC's handler core |
| `cpu_per_core[i].usr/sys/idle` | `/proc/stat` | Per-core load — find which thread is monopolising a core |
| `nics.<iface>.rx_missed_errors` | `ethtool -S` | NIC ring overrun — driver or DMA pressure |
| `nics.<iface>.rx_no_buffer_count` | `ethtool -S` | NIC buffer exhaustion |
| `irqs.<handler>` | `/proc/interrupts` | IRQ pinning drift — interrupts landing on wrong cores |
| `usb.urb_errors` | `dmesg` | USB controller errors — RX888 capture stalls |

## How to read the output

`smd environment --json | jq '.observations[] | select(.source=="local_resources")'`
gives the raw probe output.  In a healthy state the rates are zero and
`observed_cores` matches `expected_cores` for every declared handler.

A degraded delta means one of the four classifier rules fired:

```
udp.rcvbuf_errors_rate 0.5/s exceeds max 0
```
The kernel is dropping UDP packets.  Either the receiver process isn't
draining the socket fast enough (back-pressure from disk, GIL, downstream
work) or the socket buffer (`SO_RCVBUF`) is too small.  Check
`net.core.rmem_max` and that consumers actually request a large
`SO_RCVBUF`.

```
core softirq% 47 exceeds max 30
```
A single core is spending too much time servicing the NIC.  Check IRQ
affinity — a NIC interrupt pinned to the same core that runs radiod's
DSP thread will starve both.

```
irq xhci_hcd firing on unexpected cores [10] (expected [2, 3])
```
IRQ balancing has drifted.  Reapply `/proc/irq/<n>/smp_affinity_list`
or pin via `irqbalance`'s ban list.

## What this probe does NOT cover (yet)

- **Switch-side counters.**  `ifInDiscards` / `ifOutDiscards` and IGMP
  querier health require an SNMP-extended `network_device` probe.
  Tracked in `tasks/plan-local-resources-probe.md` Phase 3.
- **NIC error rates.**  `ethtool -S` counters are reported as absolute
  since-boot values, not rates.  A nonzero `rx_missed_errors` means
  loss happened *at some point*, not necessarily recently.  Per-counter
  delta math is a follow-up.
- **Per-USB-device error attribution.**  `dmesg` lines don't reliably
  carry `vendor:product` info, so the USB error tally is host-wide.
  RX888 firmware control-endpoint counters would fix this; deferred.
- **radiod-internal drops.**  These come from the radiod adapter's
  `inventory --json` output, not this probe.
- **RTP sequence gaps at consumers.**  Live in the consumer projects
  (hf-timestd, ka9q-web) and surface via the contract adapter.

## Suspect-to-counter quick reference

You suspect…                       | Look at…
------------------------------------|----------
Receiver socket buffer overflow     | `udp.rcvbuf_errors_rate`
Socket buffer too small             | `udp.rcvbuf_errors_rate` AND `net.core.rmem_max`
NIC ring saturation                 | `nics.<iface>.rx_missed_errors`, `rx_no_buffer_count`
Wrong NIC counter? Check link speed | `nics.<iface>.multicast` should be increasing
Softirq starving DSP thread         | `cpu_per_core[i].soft` for the NIC's IRQ core
DSP thread itself overloaded        | `cpu_per_core[i].usr` for radiod's pinned cores
USB controller stalling             | `usb.urb_errors`, `usb.overruns`, `usb.resets`
IRQ landing on wrong core           | `irqs.<handler>.observed_cores` vs `expected_cores`
Switch port congestion              | (Phase 3 — not yet implemented)
IGMP querier flapping               | (Phase 3 — not yet implemented)

## Related

- `tasks/plan-local-resources-probe.md` — full design and remaining
  phases
- `etc/environment.example.toml` — operator-facing config template
- `lib/sigmond/discovery/local_resources.py` — probe implementation
