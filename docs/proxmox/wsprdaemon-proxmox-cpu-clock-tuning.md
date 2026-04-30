# Configuring CPU Isolation, Hyperthread Pairs, and Clock Accuracy for WSPRDAEMON / Sigmond in a Proxmox VM

**Author:** Rob Robinett (AI6VN / W0DAS)
**Date:** April 2026
**Companion to:** `wsprdaemon-proxmox-vm-setup.md`
**Goal:** Once PCIe USB passthrough is working, fully isolate the VM's assigned CPU cores from host workloads, expose host hyperthread pairs to the guest so that radiod can pin to real sibling threads, and stabilize the system clock for accurate SDR timestamping.

---

## Why this matters

For sustained 500 MB/s SDR data with no dropped samples, three things matter beyond raw USB bandwidth:

1. **No CPU contention.** If the host scheduler runs anything else (kernel threads, cron jobs, the Proxmox web UI, ZFS) on the cores assigned to the VM, those tasks introduce jitter that radiod's tight loops cannot tolerate.

2. **Correct hyperthread topology.** radiod pins its high-rate threads to a hyperthread sibling pair so the two cooperating threads share L1/L2 cache. By default a Proxmox VM presents a flat topology (no hyperthreads visible to the guest), so the guest's `thread_siblings_list` is wrong and radiod's pair-detection logic fails.

3. **Stable, accurate clock.** SDR timestamping for HamSCI GRAPE, WSPR, and FT8/FT4 demands sub-millisecond accuracy. Virtualized clocks are notoriously poor — especially on AMD APU mini PCs where the BIOS often reports an unstable TSC.

---

## Prerequisites

- PCIe USB passthrough working per `wsprdaemon-proxmox-vm-setup.md`
- VM 101 running with `affinity: 0-9` (or similar) assigned
- Root access on both host and guest

---

## Part 1 — Identify host hyperthread pairs

Before anything else, you need to know which logical CPUs on the host are siblings of which physical core. This determines everything downstream.

Run on the **host**:

```bash
for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
    n=$(basename $cpu | sed 's/cpu//')
    siblings=$(cat $cpu/topology/thread_siblings_list 2>/dev/null)
    core=$(cat $cpu/topology/core_id 2>/dev/null)
    printf "CPU %3s  core_id=%s  siblings=%s\n" "$n" "$core" "$siblings"
done | sort -t= -k2 -n
```

You'll see one of two patterns. **Sequential pairing** (most common — used by Intel CPUs and by AMD Ryzen U-series mobile/embedded chips like the 5560U, 5700U, 5825U, 7530U found in most mini PCs):

```
CPU   0  core_id=0  siblings=0-1
CPU   1  core_id=0  siblings=0-1
CPU   2  core_id=1  siblings=2-3
CPU   3  core_id=1  siblings=2-3
CPU   4  core_id=2  siblings=4-5
CPU   5  core_id=2  siblings=4-5
...
```

Each physical core's two threads are consecutively numbered. Physical core 0 = CPUs 0+1, core 1 = CPUs 2+3, etc.

**Split pairing** (less common — seen on some AMD desktop/server Ryzen and EPYC parts):

```
CPU   0  core_id=0  siblings=0,8
CPU   1  core_id=1  siblings=1,9
CPU   2  core_id=2  siblings=2,10
...
CPU   8  core_id=0  siblings=0,8
CPU   9  core_id=1  siblings=1,9
...
```

The first N CPUs are the "primary" thread of each core; the second N are the siblings. Physical core 0 = CPUs 0+N, core 1 = CPUs 1+(N+1), etc.

Most KAMRUI / Beelink / Minisforum / GMKtec mini PCs based on Ryzen 5 5560U, 5700U, 5825U or Ryzen 7 7530U use **sequential pairing**, so this is the assumption used in the worked examples below.

Or use a one-liner that just prints the unique pairs:

```bash
cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list | sort -u
```

**Write down the pairings** — you need them for steps 2, 3, and 4.

Quick summary command:

```bash
lscpu --extended=CPU,CORE,SOCKET | head -20
```

The CORE column tells you which physical core each logical CPU belongs to. CPUs sharing a CORE value are hyperthread siblings.

---

## Part 2 — Choose VM CPU assignment based on hyperthread pairs

Your goal: pick **N physical cores** worth of host CPUs, including both siblings of each, for the VM. The exact CPUs depend on your host's pairing scheme.

### Worked example: Ryzen 5 5560U (6 cores / 12 threads, sequential pairing)

This is the reference hardware for these documents. Pairings are:

| Physical core | Logical CPUs (HT pair) |
|---|---|
| 0 | 0, 1 |
| 1 | 2, 3 |
| 2 | 4, 5 |
| 3 | 6, 7 |
| 4 | 8, 9 |
| 5 | 10, 11 |

To assign 5 physical cores (10 vCPUs) to the VM and leave 1 physical core (2 logical CPUs) for the host:

```bash
qm set 101 --affinity 0-9
```

This gives the VM physical cores 0-4 (both threads of each), reserving core 5 (CPUs 10-11) for the host. Sequential pairing means a contiguous range like `0-9` correctly preserves all five HT pairs.

The Ryzen 5 5825U and Ryzen 7 5825U/7530U found in similar mini PCs use the same scheme. For an 8-core part you'd use `affinity: 0-13` to give the VM 7 physical cores and reserve core 7.

### Alternative: split-pairing CPUs (AMD desktop/server parts)

If your `thread_siblings_list` showed split pairing (e.g., siblings of CPU 0 are CPU 0 and CPU 8), then sequential ranges break HT pair preservation. To assign 5 physical cores you would instead use:

```bash
qm set 101 --affinity 0-4,8-12
```

This gives both threads of each of physical cores 0-4. The pattern is "first N cores plus their siblings."

### Verify your choice

```bash
# After setting affinity, list the host CPU assignments and which physical cores they correspond to
for c in $(seq 0 9); do   # adjust to match your affinity list
    core=$(cat /sys/devices/system/cpu/cpu$c/topology/core_id)
    siblings=$(cat /sys/devices/system/cpu/cpu$c/topology/thread_siblings_list)
    echo "Host CPU $c -> physical core $core (siblings: $siblings)"
done
```

You should see each physical `core_id` listed exactly twice (once per sibling). If any core appears only once, you've forgotten to include its sibling.

---

## Part 3 — Expose hyperthread topology to the guest

Proxmox's `qm` CLI does not expose the `threads` parameter — its `cores` field is really a vCPU count, not a physical-core count. To get the guest to see proper hyperthread pairs, you need to inject raw QEMU `-smp` arguments via the `args:` config option.

For a 10-vCPU VM (5 physical cores × 2 threads):

Edit `/etc/pve/qemu-server/101.conf` directly:

```
cores: 5
sockets: 1
args: -smp 10,sockets=1,cores=5,threads=2,maxcpus=10
```

The numbers must agree:
- Proxmox `cores: 5` × `sockets: 1` = 5 (this is what Proxmox thinks it's allocating)
- QEMU `-smp 10,sockets=1,cores=5,threads=2` says 10 total vCPUs as 5 physical cores × 2 threads
- These agree because `cores × threads = total vCPUs`

After editing:

```bash
qm stop 101
qm start 101
```

**Verify inside the guest:**

```bash
# Inside VM 101
lscpu | grep -E 'Thread|Core|Socket|CPU\(s\)'
```

Should report:
```
CPU(s):                  10
Thread(s) per core:      2
Core(s) per socket:      5
Socket(s):               1
```

Then check that sibling lists are populated:

```bash
cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
cat /sys/devices/system/cpu/cpu1/topology/thread_siblings_list
```

You should see something like `0,5` and `1,6` (or whatever the QEMU topology produces). The key thing: each sibling list has **two** CPUs in it, not just one. That's what radiod needs to detect hyperthread pairs.

If `thread_siblings_list` still contains only the CPU itself, the `args:` line didn't take. Check `qm config 101` to confirm it's there, and `ps -ef | grep qemu-system | grep -- -smp` on the host to see what arguments QEMU actually got.

---

## Part 4 — Isolate VM's CPUs from the host scheduler

Now we keep the host kernel from scheduling its own work on the cores assigned to the VM. This is done at the host kernel command line.

Edit `/etc/default/grub` on the **host**:

```
GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=on iommu=pt isolcpus=0-9 nohz_full=0-9 rcu_nocbs=0-9"
```

Replace `0-9` with the same CPU list you used for `affinity:`. Each parameter does:

| Parameter | Effect |
|---|---|
| `isolcpus=` | Removes those CPUs from the kernel scheduler's general pool. Nothing runs there unless explicitly pinned. |
| `nohz_full=` | Disables the periodic 1000 Hz timer tick on those CPUs when only one task is running. Eliminates ~1000 interrupts/sec of jitter. |
| `rcu_nocbs=` | Moves RCU (Read-Copy-Update) callback processing off these CPUs to other CPUs. |

For split-pairing CPUs, the list would instead be `isolcpus=0-4,8-12` etc. — match whatever you set for `affinity:`.

Then:

```bash
update-grub
reboot
```

**Verify after reboot:**

```bash
# Should show only QEMU/kvm processes on the isolated CPUs (plus per-CPU kernel threads which are unavoidable)
ps -eo pid,psr,comm,class | awk 'NR==1 || ($2 < 10 && $1 > 1000)' | sort -k2 -n | head -30
```

You'll still see per-CPU pinned kernel threads (kworker, ksoftirqd, migration, rcu*, etc.) on the isolated CPUs. These are infrastructure threads tied to specific CPUs that cannot be moved — they consume essentially zero CPU at idle. What `isolcpus=` actually removes is general scheduling of userspace processes. Compare to non-isolated CPUs:

```bash
ps -eo pid,psr,comm | awk '$2 == 10 || $2 == 11' | head
```

Those non-isolated CPUs will be running everything else: pveproxy, sshd, systemd, ZFS workers, etc.

**Caution:** if your host has 16 logical CPUs and you isolate 10 of them, the host has only 6 logical CPUs (3 physical cores) for everything else — Proxmox web UI, networking, ZFS, monitoring. For a dedicated SDR appliance this is fine. Make sure you're leaving enough host CPU for the rest of the system to function.

---

## Part 5 — Migrate VM from ntpd/systemd-timesyncd to chrony

chrony handles virtualized clocks much better than ntpd. It tolerates stepwise jumps, slews faster after suspend/resume, and copes with the inevitable hiccups of running on top of an unstable host TSC.

### Step 5a — Install chrony and remove competing time daemons

Inside the VM:

```bash
# Remove competing time daemons
sudo systemctl stop ntp ntpsec systemd-timesyncd 2>/dev/null
sudo apt purge ntp ntpsec 2>/dev/null
sudo systemctl disable systemd-timesyncd 2>/dev/null

# Install chrony
sudo apt update
sudo apt install -y chrony
```

### Step 5b — Configure chrony with low-latency stratum-1 servers

The default Debian/Ubuntu chrony config uses regional pool servers, which often select stratum-2 or stratum-3 backends with mediocre RTT. For best accuracy, add explicit stratum-1 references at the top of the config so chrony prefers them.

Edit `/etc/chrony/chrony.conf`:

```bash
sudo vi /etc/chrony/chrony.conf
```

Find the existing `pool` line:

```
/pool
```

Press Enter, then `O` (capital O) to open a new line **above** the pool line and enter insert mode. Type these lines:

```
# Low-latency stratum-1 references (good from Bay Area / West Coast)
server time-b-wwv.nist.gov iburst
server time.cloudflare.com iburst
server time.google.com iburst
server time.apple.com iburst

```

(Include a blank line at the end before the existing pool line.)

Press `Esc` to leave insert mode, then `:wq` and Enter to save.

Notes on server choices:

- `time-b-wwv.nist.gov` — NIST stratum-1 in Fort Collins CO (also `time-a-wwv` and `time-c-wwv` are siblings)
- `time.cloudflare.com` — anycast, usually routes to a nearby PoP
- `time.google.com` — anycast, also nearby
- `time.apple.com` — Apple's NTP, very stable; from California typically resolves to the Santa Clara region

For sites outside North America, replace these with regionally appropriate stratum-1 sources.

### Step 5c — Enable and start chrony

```bash
sudo systemctl enable --now chrony
sudo systemctl status chrony
```

### Step 5d — (Optional) From-scratch alternative

If you'd rather replace the entire config rather than edit it, this is a known-good complete configuration:

```bash
sudo tee /etc/chrony/chrony.conf > /dev/null <<'EOF'
# Low-latency stratum-1 references (good from Bay Area / West Coast)
server time-b-wwv.nist.gov iburst
server time.cloudflare.com iburst
server time.google.com iburst
server time.apple.com iburst

# Pool fallback (in case explicit servers become unreachable)
pool 0.us.pool.ntp.org iburst maxsources 2

# Allow chrony to step the clock if it's wildly off at startup
makestep 1.0 3

# Record the rate at which the system clock gains/loses time
driftfile /var/lib/chrony/chrony.drift

# Enable kernel synchronization of the real-time clock
rtcsync

# Logging
logdir /var/log/chrony
log measurements statistics tracking
EOF

sudo systemctl restart chrony
```

**Verify:**

```bash
chronyc tracking      # shows current sync state, last offset, RMS error
chronyc sources -v    # shows server selection and per-source quality
chronyc sourcestats   # shows long-term quality of each source
```

A healthy `chronyc tracking` on this configuration looks like:

```
Reference ID    : ... (time-b-wwv.nist.gov)
Stratum         : 2
Ref time (UTC)  : Thu Apr 30 19:09:35 2026
System time     : 0.000063000 seconds fast of NTP time
Last offset     : +0.000067348 seconds
RMS offset      : 0.000080842 seconds
Frequency       : 23.134 ppm slow
Residual freq   : +0.003 ppm
Skew            : 0.755 ppm
Root delay      : 0.012345678 seconds
Root dispersion : 0.001815836 seconds
Update interval : 257.4 seconds
Leap status     : Normal
```

**Targets for RMS offset:**

- Under 100 µs (0.000100 seconds) — excellent, achievable with the configuration in this document
- Under 1 ms (0.001 seconds) — good, fine for WSPR/FT8/FT4
- Over 10 ms — something is wrong; check that systemd-timesyncd is fully disabled, the explicit servers are reachable, and the host clock is healthy (Part 6)

The `chronyc sources -v` output shows each source with its current state. The `^*` marker indicates the currently-selected best source; `^+` are combinable backups; `^-` are non-combined alternates; `^?` is still being evaluated. After 5-10 minutes of running, you should see your explicit stratum-1 servers (NIST, Google, Apple) marked `^*` or `^+`, with the pool fallback at `^-`.

---

## Part 6 — Address the unstable TSC warning

If the host kernel logs:

```
TSC found unstable after boot, most likely due to broken BIOS. Use 'tsc=unstable'.
kvm: SMP vm created on host with unstable TSC; guest TSC will not be reliable
```

The TSC (Time Stamp Counter) is a hardware register that increments at a fixed rate and is the kernel's preferred clocksource because reads are cheap. When the BIOS or firmware doesn't behave correctly across CPU C-states, the kernel marks TSC unstable and falls back to HPET, which is much slower to read and noisier. KVM then warns that the guest TSC won't be reliable either.

### Step 6a — Check if your BIOS exposes the relevant settings

Most KAMRUI / NiPoGi / ACEMAGIC / Beelink / GMKtec / Minisforum mini PCs ship with **locked-down AMI BIOSes** that hide the AMD CBS (Common BIOS Settings) menu. If that's your situation, skip directly to Step 6b — the kernel approach is the standard workaround for these vendor-locked BIOSes.

If you have an unlocked BIOS (rarer on consumer mini PCs, common on enterprise hardware), look for and **disable**:

- Global C-State Control / CPU C-States (typically under CPU Configuration or AMD CBS)
- C-State 6 (or any C-State above C2)
- Cool'n'Quiet / SpeedStep dynamic frequency scaling
- Power Supply Idle Control → set to "Typical Current Idle"

Look for and **enable**:

- HPET (High Precision Event Timer)
- "Invariant TSC" or "TSC sync" if present

Reboot and check:

```bash
dmesg | grep -i tsc
```

If you no longer see "TSC found unstable", you're done with the TSC issue. If the BIOS doesn't expose any of these controls, proceed to Step 6b.

### Step 6b — Force tsc=reliable and disable deep C-states (works on BIOS-locked mini PCs)

This is the standard fix on consumer AMD mini PCs where the BIOS hides advanced settings. The two kernel parameters together:

- `tsc=reliable` — tells the kernel to trust TSC despite the BIOS not advertising it as invariant
- `processor.max_cstate=1` — keeps the CPU in shallow idle (C0/C1 only) so it can't enter deep sleep states that desync TSC

Edit `/etc/default/grub` on the host:

```
GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=on iommu=pt isolcpus=0-9 nohz_full=0-9 rcu_nocbs=0-9 tsc=reliable processor.max_cstate=1"
```

Then `update-grub && reboot`.

(Adjust the CPU range to match whatever you set for `affinity:` — `0-9` for the Ryzen 5 5560U / 5825U with sequential pairing, or `0-4,8-12` for split-pairing CPUs.)

Trade-off: `processor.max_cstate=1` increases idle power consumption by roughly 3-5W. For a 24/7 SDR appliance running radiod continuously, the CPU rarely enters deep idle anyway, so the practical cost is near zero.

After reboot, verify:

```bash
cat /sys/devices/system/clocksource/clocksource0/current_clocksource
```

Should report `tsc`. If it still says `hpet`, the kernel didn't accept the `tsc=reliable` override (rare).

### Step 6c — Tell the guest to use kvm-clock

The guest should automatically use `kvm-clock` (a paravirtualized clocksource that reads the host's clock cheaply). Verify inside the VM:

```bash
cat /sys/devices/system/clocksource/clocksource0/current_clocksource
cat /sys/devices/system/clocksource/clocksource0/available_clocksource
```

Should show `kvm-clock` as current. If it shows something else (like `hpet`), set it explicitly:

```bash
echo kvm-clock > /sys/devices/system/clocksource/clocksource0/current_clocksource
```

To make this persistent, add to the guest's `/etc/default/grub`:

```
GRUB_CMDLINE_LINUX_DEFAULT="quiet clocksource=kvm-clock"
```

Then `update-grub && reboot`.

### Step 6d — Verify clock stability over time

After 10 minutes of running, check:

```bash
chronyc tracking
```

The `RMS offset` should be **under 1 millisecond**, ideally under 100 microseconds. If it's larger or growing, the underlying clocksource is still poor — go back and revisit BIOS settings, or accept that the guest clock will need more aggressive chrony discipline.

---

## Part 7 — Snapshot the working configuration

Save copies of all the host and guest config files:

```bash
# On the host
mkdir -p /root/proxmox-passthrough-backup
cp /etc/pve/qemu-server/101.conf /root/proxmox-passthrough-backup/
cp /etc/modprobe.d/vfio.conf /root/proxmox-passthrough-backup/
cp /etc/default/grub /root/proxmox-passthrough-backup/grub.host
cp /etc/modules /root/proxmox-passthrough-backup/

# Inside the VM
mkdir -p /root/vm-config-backup
cp /etc/chrony/chrony.conf /root/vm-config-backup/
cp /etc/default/grub /root/vm-config-backup/grub.guest

date > /root/proxmox-passthrough-backup/saved-on.txt
```

---

## Part 8 — Validation tests

Once everything is configured, validate end-to-end:

### Test 1: Host CPU isolation

```bash
# On host
ps -eo pid,psr,comm,class | awk 'NR==1 || ($2 < 10 && $1 > 1000)' | sort -k2 -n
```

Should show only QEMU/kvm threads in the userspace process list. Per-CPU kernel infrastructure threads (kworker, ksoftirqd, migration, rcu*) will still appear and that's expected — they're pinned to their CPU and consume essentially zero CPU at idle.

(Adjust the `$2 < 10` to match whatever upper bound applies for your CPU range.)

### Test 2: Guest topology

```bash
# Inside VM
lscpu | grep -E 'Thread|Core|Socket'
cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
```

Should show 2 threads per core, and `thread_siblings_list` should have 2 CPUs in it.

### Test 3: Guest clocksource and chrony

```bash
# Inside VM
cat /sys/devices/system/clocksource/clocksource0/current_clocksource   # expect: kvm-clock
chronyc tracking | grep -E 'Reference|Last offset|RMS offset'
```

RMS offset should be < 1 ms after a few minutes.

### Test 4: radiod hyperthread pair detection

If sigmond / radiod has logic to detect HT pairs and pin to them, run it and confirm it finds pairs successfully. The relevant output usually appears in radiod startup logs — look for messages about CPU affinity or thread placement.

### Test 5: SDR throughput under load

Run a wsprdaemon recording session and watch for USB transfer errors:

```bash
# Inside VM
dmesg -w | grep -iE 'xhci|usb' | grep -iE 'error|fail|reset|halt'
```

A clean run should produce no output here. If you see errors, check:

- CPU isolation (Part 4) — host might still be stealing CPU time
- Cable / port quality
- Power delivery (some mini PCs throttle USB power under sustained load)

---

## Part 9 — Per-vCPU pinning via hookscript (CRITICAL)

### Why this is necessary

The `affinity:` field in the VM config is **process-level**, not per-vCPU. It restricts the QEMU process (and all its threads) to a set of host CPUs, but does **not** pin individual vCPU threads to specific host CPUs. The Linux scheduler then makes its own load-balancing decisions, and on isolated CPUs (`isolcpus=`) it tends to be conservative about migrating threads.

The observed failure mode without per-vCPU pinning: **all 10 vCPU threads bunch on a single host CPU**, time-sharing it. Inside the guest, all 10 vCPUs report 100% utilization. On the host, only one CPU shows activity and the others are idle. radiod's pinning to specific guest vCPUs doesn't translate to specific host CPUs, so HT-pair-aware scheduling inside the guest provides no real benefit.

This bug is silent and easy to miss — both the guest and the host appear "busy" in different ways, but actual throughput is roughly 1/N of what it should be.

### Solution: hookscript that pins each vCPU to a specific host CPU after VM start

Proxmox supports per-VM hookscripts that run at lifecycle events (pre-start, post-start, pre-stop, post-stop). We use the `post-start` phase to taskset each vCPU thread to a single host CPU.

### Step 9a — Create the snippets directory

```bash
# ON HOST
mkdir -p /var/lib/vz/snippets
```

### Step 9b — Create the hookscript

Create `/var/lib/vz/snippets/cpu-pin-101.sh` with this content (use whatever editor you prefer):

```bash
#!/bin/bash
# Per-vCPU pinning for VM 101
# Maps guest vCPU N -> host CPU N (1:1 for affinity 0-9)
# Preserves HT pair topology: guest vCPU 0+1 -> host CPUs 0+1 (real siblings), etc.

VMID=$1
PHASE=$2

if [ "$PHASE" = "post-start" ] && [ "$VMID" = "101" ]; then
    # Wait for QEMU to fully start all vCPU threads
    sleep 2

    # Find the QEMU process for this VM
    QEMU_PID=$(cat /var/run/qemu-server/${VMID}.pid 2>/dev/null)
    if [ -z "$QEMU_PID" ]; then
        echo "cpu-pin: could not find QEMU pid for VM $VMID" >&2
        exit 0
    fi

    # Pin each vCPU thread to its corresponding host CPU
    for tid in $(ps -T -p $QEMU_PID -o tid= 2>/dev/null); do
        comm=$(cat /proc/$tid/comm 2>/dev/null)
        if [[ "$comm" =~ ^CPU\ ([0-9]+)/KVM$ ]]; then
            vcpu=${BASH_REMATCH[1]}
            host_cpu=$vcpu
            taskset -pc $host_cpu $tid > /dev/null 2>&1
            echo "cpu-pin: VM $VMID vCPU $vcpu (tid $tid) -> host CPU $host_cpu"
        fi
    done | logger -t cpu-pin-${VMID}
fi

exit 0
```

This script assumes a 1:1 vCPU-to-host-CPU mapping where guest vCPU N runs on host CPU N. For sequential pairing (Ryzen 5 5560U, 5825U, 7530U), this preserves HT pair topology naturally — guest vCPU 0+1 land on host CPUs 0+1, which are real siblings.

For other VMs (different VMID, different CPU mapping), copy the script to `cpu-pin-VMID.sh` and adjust the `VMID` check and the host_cpu mapping inside the loop.

### Step 9c — Make it executable

```bash
# ON HOST
chmod +x /var/lib/vz/snippets/cpu-pin-101.sh
```

### Step 9d — Attach the hookscript to the VM

```bash
# ON HOST
qm set 101 --hookscript local:snippets/cpu-pin-101.sh
```

### Step 9e — Add topoext CPU flag (required for Ryzen guests)

Without the `topoext` CPU feature flag, AMD CPU models don't advertise SMT support to the guest, even though Linux will accept the `threads=2` topology. QEMU prints this warning at startup:

```
kvm: warning: This family of AMD CPU doesn't support hyperthreading(2). Please configure -smp options properly or try enabling topoext feature.
```

Proxmox restricts which CPU flags can be set via the `cpu:` config field for security reasons, and `topoext` is not in the allowed set. The workaround is to use the `args:` escape hatch, same as for `-smp threads=2`.

Edit `/etc/pve/qemu-server/101.conf` and modify the existing `args:` line to include `-cpu host,topoext=on`:

Before:
```
args: -smp 10,sockets=1,cores=5,threads=2,maxcpus=10
```

After:
```
args: -smp 10,sockets=1,cores=5,threads=2,maxcpus=10 -cpu host,topoext=on
```

The `-cpu host,topoext=on` in args will override the `cpu: host` line. Leave `cpu: host` in place (Proxmox uses it for its own bookkeeping).

### Step 9f — Restart and verify

```bash
# ON HOST
qm stop 101
qm start 101
sleep 5

# Check the hookscript ran
journalctl -t cpu-pin-101 -n 20

# Confirm each vCPU is on its own host CPU
ps -eLo psr,pcpu,comm --no-headers | grep KVM | sort -k1 -n
```

The journal should show 10 lines like `cpu-pin: VM 101 vCPU N (tid XXXX) -> host CPU N`. The `ps` output should show each vCPU thread on a distinct host CPU 0-9.

The `kvm: warning: This family of AMD CPU doesn't support hyperthreading` message should NOT appear in the start logs anymore. Verify:

```bash
# ON HOST
journalctl -b | grep -i 'hyperthreading\|topoext'
```

Inside the guest, confirm topoext is now advertised:

```bash
# IN VM
cat /proc/cpuinfo | grep flags | head -1 | tr ' ' '\n' | grep topoext
```

Should output `topoext`.

### Step 9g — Validate under real load

Run a stress test inside the VM that exercises specific vCPUs, then verify on the host that the load lands on the corresponding host CPUs:

```bash
# IN VM — load only vCPUs 2 through 9, leaving 0 and 1 free for radiod
taskset -c 2-9 stress -c 8
```

```bash
# ON HOST — should show vCPUs 2-9 at ~100%, vCPUs 0 and 1 idle, host CPUs 10-11 idle
ps -eLo psr,pcpu,comm --no-headers | grep KVM | sort -k1 -n
```

Expected output (rough):
```
0   <5%   CPU 0/KVM       (idle, available for radiod)
1   <5%   CPU 1/KVM       (idle, available for radiod)
2   ~100% CPU 2/KVM
3   ~100% CPU 3/KVM
...
9   ~100% CPU 9/KVM
```

This confirms that pinning is end-to-end correct: a workload running on a specific guest vCPU consumes exactly the corresponding host CPU. Without per-vCPU pinning, the load would distribute unpredictably across whichever host CPUs the scheduler felt like using.

---

## Part 10 — Operational observations and validation under load

Once the configuration is complete, the following observations are expected when running radiod alongside other workloads. None of these indicate a problem; they're documented here so future debugging doesn't chase phantoms.

### Expected: elevated radiod CPU under cache contention

Symptom: when something CPU-intensive runs on guest vCPUs 2-9, the radiod processes pinned to vCPUs 0-1 show modestly higher CPU utilization than when the rest of the system is idle.

Cause: the Ryzen 5 5560U has a single 16 MB unified L3 cache shared across all 6 physical cores. When 8 stress workers run on cores 2-9, they evict radiod's working set from L3 more aggressively, increasing radiod's per-sample cycle cost. This is a hardware-level effect; it would happen on bare metal too. The vCPU pinning correctly maintains thread placement — the cache contention is unavoidable architecture.

Validation that it's *only* cache contention and not a real problem:

```bash
# IN VM — should show no errors during sustained load
journalctl -u radiod -n 100 --no-pager | grep -iE 'error|drop|fail|warn'
dmesg -T | grep -iE 'xhci|usb' | grep -iE 'error|fail|reset|halt'
```

Both should return nothing. If they do, the issue is not just cache contention.

### Expected: host CPUs 10-11 show steady low-level activity

Symptom: even when the VM is doing nothing, host CPUs 10-11 show 1-5% utilization from kworker, ksoftirqd, pvestatd, pve-firewall, and similar processes.

Cause: this is the host doing its normal management work — Proxmox cluster heartbeat, ZFS scrub scheduling, network stack housekeeping. It's confined to CPUs 10-11 by `isolcpus=0-9` and consumes essentially zero capacity. This is exactly what we want.

### Expected: per-CPU kernel threads on isolated CPUs

Symptom: `ps -eLo psr,comm` on the host shows kernel threads like `kworker/N:0H`, `ksoftirqd/N`, `migration/N`, `rcuog/N` on isolated CPUs 0-9.

Cause: these are per-CPU pinned kernel infrastructure threads that exist on every CPU. They cannot be moved off because they're tied to that specific CPU's hardware. They sit idle and consume essentially zero CPU time. `isolcpus=` removes general scheduling, not these infrastructure threads.

### Recommended hygiene: IRQ affinity isolation

By default, hardware interrupts can be handled on any CPU. To eliminate one more source of jitter on the radiod cores, rebind all movable interrupts to host CPUs 10-11:

```bash
# ON HOST
for irq in /proc/irq/*/smp_affinity_list; do
    echo "10-11" > "$irq" 2>/dev/null
done
```

(Some IRQs are pinned to specific CPUs by their drivers and can't be moved — those will silently fail. That's expected.)

To make this persistent across reboots, add it to `/etc/rc.local` or create a systemd unit. A simple approach with rc.local:

```bash
# ON HOST
sudo tee /etc/rc.local > /dev/null <<'EOF'
#!/bin/bash
# Move all movable IRQs to host CPUs 10-11 to keep them off VM cores
for irq in /proc/irq/*/smp_affinity_list; do
    echo "10-11" > "$irq" 2>/dev/null
done
exit 0
EOF
sudo chmod +x /etc/rc.local
```

Verify after a reboot:

```bash
# ON HOST
for irq in /proc/irq/*/smp_affinity_list; do
    n=$(basename $(dirname $irq))
    aff=$(cat $irq 2>/dev/null)
    desc=$(grep "^ *$n:" /proc/interrupts | awk '{print $NF}' 2>/dev/null)
    [ -n "$aff" ] && printf "IRQ %4s  cpus=%-15s  %s\n" "$n" "$aff" "$desc"
done | head -30
```

Most IRQs should now show `cpus=10-11`. The few that show specific CPUs (like `cpus=5` for some platform-specific interrupts) are pinned by the driver and cannot be moved.

This is hygiene, not a fix for any specific problem. radiod was working fine before this change. But it removes one more potential source of latency spikes on the SDR processing cores.

### Comparison with bare-metal performance

The expected steady-state performance of this VM configuration is essentially identical to bare-metal performance for the radiod workload. Specifically:

- Same sustained USB throughput (PCIe passthrough = direct DMA, no virtualization overhead in the data path)
- Same clock accuracy (kvm-clock paravirtualization adds <1µs overhead on TSC reads)
- Same L3 cache behavior (vCPUs execute directly on physical cores)
- Slightly higher TLB miss cost due to nested page tables (typically <2% overhead, often unmeasurable)

The configuration is therefore suitable for production SDR deployments where bare-metal performance is required but the management benefits of virtualization (snapshots, easy migration, consolidation with other VMs) are desirable.

---

## Troubleshooting

### Guest still shows `Thread(s) per core: 1`

The `args:` line in `/etc/pve/qemu-server/101.conf` isn't taking effect. Check:

```bash
ps -ef | grep qemu-system | grep -- '-smp'
```

If the `-smp` argument doesn't include `threads=2`, the args line is wrong or the VM was started before the edit. Stop and restart:

```bash
qm stop 101 && qm start 101
```

### chrony shows large offsets that don't settle

Check that the VM has working network access to NTP pools:

```bash
chronyc activity
chronyc sources
```

If sources are unreachable, check firewall rules for UDP port 123 outbound. Also check that `systemd-timesyncd` is fully disabled — it can fight chrony for control of the clock.

### All vCPUs land on a single host CPU

Symptom: inside the VM, all N vCPUs show high utilization. On the host, `ps -eLo psr,pcpu,comm --no-headers | grep KVM | sort -k1 -n` shows all `CPU N/KVM` threads with the same `psr` value (e.g., all on host CPU 5).

Cause: the per-vCPU pinning hookscript (Part 9) is not running, or the VM was started before it was attached.

Fix:
1. Verify the hookscript is attached: `qm config 101 | grep hookscript`
2. Verify the script is executable: `ls -la /var/lib/vz/snippets/cpu-pin-101.sh`
3. Restart the VM: `qm stop 101 && qm start 101`
4. Check the journal: `journalctl -t cpu-pin-101 -n 20`
5. Re-verify per-vCPU placement: `ps -eLo psr,pcpu,comm --no-headers | grep KVM | sort -k1 -n`

### "kvm: warning: This family of AMD CPU doesn't support hyperthreading"

The `topoext` CPU flag is not enabled. See Part 9, Step 9e.

### Host instability after adding `isolcpus`

If the host becomes unresponsive or services time out, you may have isolated too many CPUs. On a Ryzen 5 5560U (12 logical CPUs), `isolcpus=0-9` leaves only CPUs 10-11 (1 physical core) for the host. That's tight but workable for a dedicated SDR appliance. If it's too tight, back off to `isolcpus=0-7` (giving the host 4 logical CPUs / 2 physical cores) and reduce the VM accordingly.

### radiod still can't find hyperthread pairs

Even with proper guest topology, radiod's pair-detection logic may parse `/sys` differently than expected. Check what files it actually reads — usually `thread_siblings_list` or `core_cpus_list`. Inside the VM:

```bash
ls -la /sys/devices/system/cpu/cpu0/topology/
cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list | sort -u
```

If sibling lists look correct but radiod still complains, the issue may be in radiod's parsing rather than the VM topology.

---

## Companion documents

- `wsprdaemon-proxmox-vm-setup.md` — PCIe USB passthrough setup (do this first)
- This document — CPU isolation, hyperthread pairs, clock accuracy

Future topics worth documenting separately:

- NUMA configuration for multi-socket hosts
- Migration of the VM between hosts
- Backup and snapshot strategy for the SDR workload
- Upgrading Proxmox / kernel without losing the passthrough config
