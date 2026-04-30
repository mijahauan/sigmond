# Proxmox VM — CPU Isolation, Hyperthread Pairs, Per-vCPU Pinning, and Clock Tuning Runbook

**Author:** Rob Robinett (AI6VN / W0DAS)
**Date:** April 2026
**Companion to:** `wsprdaemon-proxmox-vm-setup.md` (PCIe USB passthrough — prerequisite)
**Companion to:** `wsprdaemon-proxmox-bios-checklist.md` (offline reference for BIOS visit)
**Goal:** Once PCIe USB passthrough is working, partition CPUs between
host and VM, expose real hyperthread pairs to the guest, deterministically
pin every guest vCPU to a specific host CPU, and reserve one in-guest
HT pair for radiod alone.

---

## Why this matters

For sustained 500 MB/s SDR data with no dropped samples and
sub-millisecond timestamping, four things matter:

1. **No CPU contention on radiod's pair.** radiod's USB3/FFT thread
   group is latency-sensitive. Anything else scheduled on those CPUs
   adds jitter the tight loops cannot tolerate.

2. **Real hyperthread topology in the guest.** radiod pins its
   high-rate threads to a hyperthread sibling pair so they share L1/L2
   cache. By default a Proxmox VM presents flat topology (no HT
   siblings visible to the guest); sigmond falls back to assuming
   consecutive pairs, but pinning still has to be honored at the host
   level for the L1/L2 sharing to be real.

3. **Deterministic per-vCPU placement.** Process-wide affinity (the
   `affinity:` field) keeps QEMU on a set of host CPUs, but inside that
   set vCPU threads are free to float. That floats radiod's "pair" off
   real silicon HT siblings into different physical cores. Per-vCPU
   pinning fixes vCPU N to a specific host CPU.

4. **Stable, accurate clock.** SDR timestamping for HamSCI GRAPE, WSPR,
   and FT8/FT4 demands sub-millisecond accuracy. Virtualized clocks
   need chrony plus a stable host TSC (or a clean kvm-clock fallback).

---

## Hardware reference (this host)

CPU: **AMD Ryzen 5 5560U** (Lucienne, 6 cores / 12 threads).

HT pairing on this CPU is **sequential** — verify on the host before
committing to the numbers below. Earlier versions of this document
claimed AMD-style "split" pairing (sibling of CPU 0 is CPU 8); that is
wrong for this hardware. Confirm by running on the host:

```bash
cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list | sort -u
# Expected output (sequential):
#   0,1
#   2,3
#   4,5
#   6,7
#   8,9
#   10,11
```

Host CPU/core map (sequential):

| Physical core | Logical CPUs |
|---|---|
| core 0 | 0, 1 |
| core 1 | 2, 3 |
| core 2 | 4, 5 |
| core 3 | 6, 7 |
| core 4 | 8, 9 ← reserved for host |
| core 5 | 10, 11 ← reserved for host |

---

## Allocation plan (one VM only)

- **Host (Proxmox OS):** cores 4–5 → CPUs 8–11 (4 logical CPUs).
- **VM 101 (AI6VN-1):** cores 0–3 → CPUs 0–7 (4 physical, 8 vCPUs).
- **Inside the VM:** vCPU 0,1 (one guest HT pair) reserved for radiod
  alone via guest-side `isolcpus=0,1`. vCPU 2–7 for guest kernel and
  every other service (sigmond, ka9q-web, hf-timestd, etc.).

Two layers of `isolcpus`:

| Layer | Boundary | Purpose |
|---|---|---|
| Host | `isolcpus=0-7` | Keep Proxmox kernel work off CPUs 0–7 so QEMU has them uncontested. |
| Guest | `isolcpus=0,1` | Keep guest kernel work off vCPU 0,1 so radiod has its HT pair uncontested. |

Per-vCPU pinning (Phase 2) makes vCPU N → host CPU N a fixed mapping,
so the guest's vCPU 0,1 always lands on host CPUs 0,1 (a real HT pair).

---

## Phases overview and reboot map

| Phase | What changes | Where | Reboot? |
|---|---|---|---|
| 0 | Verify + snapshot | Host & VM | none |
| 1 | BIOS settings (TSC, C-states) | Host BIOS | **HOST reboot** (into BIOS, save, out) |
| 2 | VM conf (cores, args, affinity) + per-vCPU pinning hookscript | Host | **VM stop/start** only |
| 3 | Host kernel cmdline (isolcpus, nohz_full, rcu_nocbs) | Host | **HOST reboot** (VM restarts too) |
| 4 | Guest kernel cmdline (isolcpus=0,1) | VM | **VM reboot** |
| 5 | chrony — already complete on AI6VN-1 | (skip) | none |
| 6 | Validation | both | none |
| 7 | Snapshot configs | both | none |

Three reboots in total: two host, one guest. After each reboot the doc
shows where to resume.

---

## Phase 0 — Verify and snapshot (no changes)

### 0.1 Verify HT pairing on the host

```bash
# On host
lscpu --extended=CPU,CORE,SOCKET | head -20
cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list | sort -u
```

If output is not sequential as shown in "Hardware reference" above,
**stop and reconsider** — the affinity numbers below assume sequential
pairing.

### 0.2 Snapshot host configs

```bash
# On host
sudo mkdir -p /root/proxmox-passthrough-backup
sudo cp /etc/pve/qemu-server/101.conf  /root/proxmox-passthrough-backup/101.conf.pre-tuning
sudo cp /etc/modprobe.d/vfio.conf      /root/proxmox-passthrough-backup/
sudo cp /etc/default/grub              /root/proxmox-passthrough-backup/grub.host.pre-tuning
sudo cp /etc/modules                   /root/proxmox-passthrough-backup/
date | sudo tee /root/proxmox-passthrough-backup/saved-on-pre-tuning.txt
```

### 0.3 Snapshot guest configs

```bash
# Inside VM
sudo mkdir -p /root/vm-config-backup
sudo cp /etc/default/grub        /root/vm-config-backup/grub.guest.pre-tuning
sudo cp /etc/chrony/chrony.conf  /root/vm-config-backup/
sudo systemctl is-active chrony chronyd ntp ntpsec systemd-timesyncd > /root/vm-config-backup/timesync-state.txt 2>&1
date | sudo tee /root/vm-config-backup/saved-on-pre-tuning.txt
```

### 0.4 Verify chrony state (so we know we can skip Phase 5)

```bash
# Inside VM
systemctl is-active chrony   # expect: active
chronyc tracking | head -10  # expect: stratum 2-3, RMS offset < 1 ms
cat /sys/devices/system/clocksource/clocksource0/current_clocksource  # expect: kvm-clock
```

If chrony is **not** active, do the chrony migration described in
"Appendix A — chrony migration" before continuing. As of 2026-04-29 on
AI6VN-1, chrony is already active with RMS offset around 80 µs.

### 0.5 Capture sigmond's current view of the affinity plan

```bash
# Inside VM
sudo /opt/sigmond/venv/bin/python /home/sigmond/sigmond/bin/smd diag cpu-affinity --json | tee /root/vm-config-backup/smd-cpu-affinity.pre-tuning.json
```

This is the baseline — useful for diffing once the changes are in.

---

## Phase 1 — BIOS visit on the host

> ⚠ **REBOOT REQUIRED**. Open `wsprdaemon-proxmox-bios-checklist.md` on
> a separate device (phone, laptop, printed) — Claude Code is not
> available during the BIOS visit. Follow the checklist, save changes,
> and let the host reboot back into Proxmox.

After Phase 1, return here for Phase 2.

If chrony is currently sub-millisecond and you would rather defer the
BIOS visit, you can skip Phase 1 and come back to it later. The
remaining phases do not depend on it. The cost of skipping is that
unstable-TSC warnings will keep appearing in `dmesg` and clock noise
will be slightly higher.

---

## Phase 2 — VM topology, affinity, and per-vCPU pinning

This phase changes how the VM is launched. No host reboot. The VM is
stopped and started once.

### 2.1 Edit the VM config

Edit `/etc/pve/qemu-server/101.conf` on the **host**. Change these
fields (others — `hostpci0`, `hostpci1`, `machine: q35` — must remain
untouched):

```
affinity: 0-7
cores: 4
sockets: 1
args: -smp 8,sockets=1,cores=4,threads=2,maxcpus=8
hookscript: local:snippets/vm-101-affinity.sh
```

Why each field:

- `affinity: 0-7` — process-wide mask: QEMU may run only on host CPUs
  0–7. (Phase 3 will also harden this with `isolcpus`.)
- `cores: 4, sockets: 1` — the "Proxmox view" of the topology:
  4 physical cores. `cores × threads` must equal total vCPUs.
- `args: -smp 8,sockets=1,cores=4,threads=2,maxcpus=8` — the QEMU view:
  8 vCPUs as 4 cores × 2 threads. Proxmox's `qm` CLI does not expose
  `threads`, so the only way to give the guest real HT topology is
  this raw `-smp` injection.
- `hookscript: local:snippets/vm-101-affinity.sh` — runs the per-vCPU
  pinning script after VM start (see 2.2).

### 2.2 Install the per-vCPU pinning hookscript

Per-vCPU pinning is the difference between "vCPU 0 is somewhere in the
host CPU 0–7 range" and "vCPU 0 is exactly on host CPU 0". radiod
**requires** the latter — without it, the guest HT pair (vCPU 0,1)
can drift onto two different physical cores and lose L1/L2 sharing.

Create the hookscript on the **host**:

```bash
sudo install -d /var/lib/vz/snippets

sudo tee /var/lib/vz/snippets/vm-101-affinity.sh <<'EOF'
#!/bin/bash
# Per-vCPU pinning for VM 101 on AMD Ryzen 5 5560U.
# Pin vCPU N to host CPU N so guest HT pair (vCPU 0,1) lands on
# real host HT siblings (host CPU 0,1) — required for radiod.
set -e
vmid=$1
phase=$2

# Only act on the post-start phase, and only for VM 101.
[ "$phase" = "post-start" ] || exit 0
[ "$vmid" = "101" ]          || exit 0

pid_file=/run/qemu-server/${vmid}.pid
[ -r "$pid_file" ] || { logger -t vm-${vmid}-affinity "no pid file"; exit 0; }
pid=$(cat "$pid_file")

# 1:1 vCPU → host CPU.  VM has 8 vCPUs; host CPUs 0-7 are the VM pool;
# host CPUs 8-11 are reserved for the Proxmox host OS.
declare -A pin=(
    [0]=0  [1]=1  [2]=2  [3]=3
    [4]=4  [5]=5  [6]=6  [7]=7
)

# Give QEMU a moment to spawn all vCPU threads.  post-start fires after
# QEMU is up but vCPU naming can lag fractionally.
for attempt in 1 2 3 4 5; do
    found=0
    for tdir in /proc/${pid}/task/*/; do
        tid=$(basename "$tdir")
        name=$(cat "${tdir}comm" 2>/dev/null) || continue
        # vCPU thread names look like:  CPU 0/KVM
        if [[ $name =~ ^CPU\ ([0-9]+)/KVM$ ]]; then
            vcpu=${BASH_REMATCH[1]}
            host_cpu=${pin[$vcpu]}
            if [ -n "$host_cpu" ]; then
                if taskset -pc "$host_cpu" "$tid" >/dev/null; then
                    logger -t vm-${vmid}-affinity \
                        "pinned vCPU $vcpu (tid $tid) to host CPU $host_cpu"
                    found=$((found+1))
                fi
            fi
        fi
    done
    [ "$found" -ge 8 ] && exit 0
    sleep 0.5
done

logger -t vm-${vmid}-affinity "warning: pinned only $found/8 vCPUs"
exit 0
EOF

sudo chmod +x /var/lib/vz/snippets/vm-101-affinity.sh
```

Register the hookscript with the VM (already in the conf if you edited
it manually in 2.1, but this enforces it):

```bash
sudo qm set 101 --hookscript local:snippets/vm-101-affinity.sh
```

### 2.3 Restart the VM

```bash
# On host
sudo qm stop 101
sudo qm start 101
```

### 2.4 Verify Phase 2

On the **host**:

```bash
# QEMU was launched with the right -smp string
ps -ef | grep qemu-system-x86_64 | grep -- '-smp'
# Expect: -smp 8,sockets=1,cores=4,threads=2,maxcpus=8

# Per-vCPU pinning succeeded — check syslog
sudo journalctl -t vm-101-affinity --since '5 minutes ago' --no-pager
# Expect 8 lines: "pinned vCPU N (tid ...) to host CPU N" for N=0..7

# Live confirmation: each vCPU thread's affinity mask
pid=$(cat /run/qemu-server/101.pid)
for tdir in /proc/${pid}/task/*/; do
    tid=$(basename "$tdir")
    name=$(cat "${tdir}comm" 2>/dev/null)
    [[ $name == "CPU "*"/KVM" ]] || continue
    mask=$(grep Cpus_allowed_list /proc/${pid}/task/${tid}/status | awk '{print $2}')
    printf '%-12s tid=%s allowed=%s\n' "$name" "$tid" "$mask"
done | sort
# Expect each "CPU N/KVM" allowed=N (single CPU, no range).
```

Inside the **VM**:

```bash
# Guest now sees 4×2 topology, not flat
lscpu | grep -E 'Thread|Core|Socket|CPU\(s\)'
# Expect:
#   CPU(s): 8
#   Thread(s) per core: 2
#   Core(s) per socket: 4
#   Socket(s): 1

# Sibling list shows pairs, not singletons
cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list   # 0,1
cat /sys/devices/system/cpu/cpu2/topology/thread_siblings_list   # 2,3
```

### 2.5 Gate

- RX-888 still enumerates at SuperSpeed (`lsusb -t` shows `5000M`).
- No xhci errors (`dmesg | grep -iE 'xhci|usb' | grep -iE 'error|fail|reset|halt'` is empty).
- radiod can still start. If radiod was running, restart it: `sudo systemctl restart radiod@*`.
- `sudo /opt/sigmond/venv/bin/python /home/sigmond/sigmond/bin/smd diag cpu-affinity` reports radiod assigned to vCPUs 0,1 with no warnings.

If anything fails, restore `/etc/pve/qemu-server/101.conf` from
`/root/proxmox-passthrough-backup/101.conf.pre-tuning` and stop here to
investigate before continuing.

---

## Phase 3 — Host kernel cmdline: isolate CPUs 0–7

This phase keeps the Proxmox kernel from scheduling its own work on the
VM's CPU pool.

### 3.1 Edit /etc/default/grub on the host

```bash
sudo cp /etc/default/grub /root/proxmox-passthrough-backup/grub.host.pre-isolcpus

sudoedit /etc/default/grub
```

Modify the `GRUB_CMDLINE_LINUX_DEFAULT` line to **append**
(do not remove the existing flags):

```
GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=on iommu=pt isolcpus=0-7 nohz_full=0-7 rcu_nocbs=0-7"
```

If you already added `tsc=reliable` from the BIOS-checklist fallback,
keep it.

What each parameter does:

| Parameter | Effect |
|---|---|
| `isolcpus=0-7` | Removes those CPUs from the kernel's general scheduler pool. Nothing runs there unless explicitly pinned. |
| `nohz_full=0-7` | Disables the periodic 1000 Hz timer tick on those CPUs when only one task is running. Eliminates ~1000 interrupts/sec of jitter. |
| `rcu_nocbs=0-7` | Moves RCU callback processing off these CPUs onto the non-isolated ones (8–11). |

### 3.2 Apply and reboot

```bash
sudo update-grub
sudo reboot
```

> ⚠ **REBOOT REQUIRED — HOST**. The VM will go down with the host and
> come back up automatically (assuming `onboot 1`). After the host is
> back, SSH back in and resume at 3.3.

### 3.3 Verify Phase 3 (after host reboot)

On the **host**:

```bash
# Confirm cmdline took effect
cat /proc/cmdline
# Expect to see all three: isolcpus=0-7 nohz_full=0-7 rcu_nocbs=0-7

# Confirm CPUs 0-7 are nearly empty of host work
ps -eo pid,psr,comm | awk '$2 <= 7 && NR > 1 {print}' | sort -k2 -n | head -30
# Expect: only QEMU vCPU threads (CPU N/KVM) and a few unmovable per-CPU
# kernel threads (migration/N, idle_inject/N, cpuhp/N, ksoftirqd/N).
# NO systemd, kworker (except per-cpu), pveproxy, etc.

# Compare to non-isolated CPUs 8-11 — those should be busy
ps -eo pid,psr,comm | awk '$2 >= 8 && $2 <= 11 && NR > 1 {print}' | sort -k2 -n | head
```

Inside the **VM**:

```bash
# RX-888 still good
lsusb -t | grep -i rx888 || lsusb | grep -i 'cypress\|04b4'
dmesg | grep -iE 'xhci|usb' | grep -iE 'error|fail|reset|halt' | tail
# Expect: zero error lines

# radiod healthy
sudo systemctl status 'radiod@*' --no-pager | head -20
```

### 3.4 Gate

- Host stays responsive on SSH (CPUs 8–11 are enough for Proxmox UI,
  networking, and ZFS).
- VM came back up cleanly.
- RX-888 streams without errors.

> 🛑 If host became unresponsive or services time out, the most common
> cause is too few host CPUs. To roll back, edit `/etc/default/grub` to
> remove the three new parameters and `update-grub && reboot`. Do this
> via the host's console (display + keyboard, IPMI, or directly at the
> machine) since SSH may be unreliable.

---

## Phase 4 — Guest kernel cmdline: isolate vCPU 0,1 for radiod

The second layer. Same mechanism, applied inside the VM.

### 4.1 Edit /etc/default/grub inside the VM

```bash
# Inside VM
sudo cp /etc/default/grub /root/vm-config-backup/grub.guest.pre-isolcpus

sudoedit /etc/default/grub
```

Modify `GRUB_CMDLINE_LINUX_DEFAULT` to **append**:

```
GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=0,1 nohz_full=0,1 rcu_nocbs=0,1"
```

(Keep any existing flags. If you already had `clocksource=kvm-clock`
or similar, leave it.)

### 4.2 Apply and reboot

```bash
sudo update-grub
sudo reboot
```

> ⚠ **REBOOT REQUIRED — VM**. The host stays up; only VM 101 reboots.

### 4.3 Verify Phase 4 (after VM reboot)

Inside the **VM**:

```bash
# Cmdline took effect
cat /proc/cmdline
# Expect: isolcpus=0,1 nohz_full=0,1 rcu_nocbs=0,1

# Kernel reports those CPUs isolated
cat /sys/devices/system/cpu/isolated   # expect: 0-1

# vCPUs 0 and 1 are nearly empty of guest work
ps -eo pid,psr,comm | awk '$2 == 0 || $2 == 1' | sort -k2 -n | head -30
# Expect: only radiod threads (once it's running) and unmovable per-CPU
# kernel threads (migration/0, ksoftirqd/0, etc.).  No systemd,
# no chronyd, no random kworkers.
```

### 4.4 Re-apply sigmond's affinity drop-ins

Once the kernel is reporting `isolated=0-1`, ask sigmond to recompute
and apply its plan so radiod's drop-in matches:

```bash
sudo /opt/sigmond/venv/bin/python /home/sigmond/sigmond/bin/smd diag cpu-affinity --apply
sudo /opt/sigmond/venv/bin/python /home/sigmond/sigmond/bin/smd diag cpu-affinity
```

Sigmond should report:

- `radiod@*.service` → CPUAffinity=`0 1`
- `other` services → CPUAffinity=`2 3 4 5 6 7`
- No "outside isolated pool" warnings.
- No foreign drop-ins or sched_setaffinity overrides.

### 4.5 Gate

- radiod runs on vCPU 0,1.
- All other services (systemd-resolved, chronyd, ka9q-web, hf-timestd
  components, etc.) run on vCPU 2–7.
- chrony's RMS offset is still sub-millisecond.

---

## Phase 5 — chrony migration

**Status on AI6VN-1: already complete.**

Verified 2026-04-29: chrony is active, RMS offset ~80 µs, kvm-clock is
the guest clocksource, ntpsec / systemd-timesyncd are inactive. **Skip
this phase** unless a future install starts from scratch — in which
case follow Appendix A.

---

## Phase 6 — Validation

End-to-end checks after all phases are applied.

### 6.1 Host CPU isolation

```bash
# Host
ps -eo pid,psr,comm | awk '$2 <= 7' | sort -k2 -n
```

Should show only QEMU vCPU threads and unmovable per-CPU kernel threads.

### 6.2 Guest topology

```bash
# Inside VM
lscpu | grep -E 'Thread|Core|Socket'
cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list | sort -u
```

Should show 4 cores × 2 threads, sibling lists `0,1 / 2,3 / 4,5 / 6,7`.

### 6.3 Guest CPU isolation

```bash
# Inside VM
cat /sys/devices/system/cpu/isolated   # 0-1
ps -eo pid,psr,comm | awk '$2 <= 1' | sort -k2 -n
```

Only radiod threads on vCPU 0,1.

### 6.4 Guest clock

```bash
# Inside VM
cat /sys/devices/system/clocksource/clocksource0/current_clocksource
chronyc tracking | grep -E 'Reference|Last offset|RMS offset'
```

Clocksource = kvm-clock. RMS offset < 1 ms (ideally < 100 µs).

### 6.5 Sigmond cross-check

```bash
# Inside VM
sudo /opt/sigmond/venv/bin/python /home/sigmond/sigmond/bin/smd validate
sudo /opt/sigmond/venv/bin/python /home/sigmond/sigmond/bin/smd diag cpu-affinity
```

`smd validate` should pass `cpu_isolation` and `cpu_isolation_runtime`.
`smd diag cpu-affinity` should show no warnings.

### 6.6 SDR throughput under load

```bash
# Inside VM, with radiod streaming
dmesg -w | grep -iE 'xhci|usb' | grep -iE 'error|fail|reset|halt'
```

Watch for several minutes during a recording session. Expect zero
output. If errors appear, see Troubleshooting.

---

## Phase 7 — Snapshot working configuration

```bash
# Host
sudo mkdir -p /root/proxmox-passthrough-backup
sudo cp /etc/pve/qemu-server/101.conf            /root/proxmox-passthrough-backup/101.conf.tuned
sudo cp /etc/modprobe.d/vfio.conf                /root/proxmox-passthrough-backup/
sudo cp /etc/default/grub                        /root/proxmox-passthrough-backup/grub.host.tuned
sudo cp /etc/modules                             /root/proxmox-passthrough-backup/
sudo cp /var/lib/vz/snippets/vm-101-affinity.sh  /root/proxmox-passthrough-backup/
date | sudo tee /root/proxmox-passthrough-backup/saved-on-tuned.txt

# Inside VM
sudo mkdir -p /root/vm-config-backup
sudo cp /etc/default/grub        /root/vm-config-backup/grub.guest.tuned
sudo cp /etc/chrony/chrony.conf  /root/vm-config-backup/
sudo /opt/sigmond/venv/bin/python /home/sigmond/sigmond/bin/smd diag cpu-affinity --json \
    | sudo tee /root/vm-config-backup/smd-cpu-affinity.tuned.json > /dev/null
date | sudo tee /root/vm-config-backup/saved-on-tuned.txt
```

---

## Troubleshooting

### Guest still shows `Thread(s) per core: 1`

The `args:` line in `/etc/pve/qemu-server/101.conf` isn't taking effect.

```bash
# On host
ps -ef | grep qemu-system | grep -- '-smp'
```

If `threads=2` is missing, the args line is wrong or the VM is running
on the old config. Stop and restart:

```bash
sudo qm stop 101 && sudo qm start 101
```

### Per-vCPU pinning didn't fire

Check the journal on the host for `vm-101-affinity` entries:

```bash
sudo journalctl -t vm-101-affinity --since '10 minutes ago' --no-pager
```

If empty: the hookscript may not be registered, executable, or in the
right path. Verify:

```bash
qm config 101 | grep -i hookscript    # should reference local:snippets/vm-101-affinity.sh
ls -l /var/lib/vz/snippets/vm-101-affinity.sh   # must be executable
```

### Host became unresponsive after Phase 3 reboot

You may have isolated more CPUs than the host can spare. Reduce the
range (e.g. `isolcpus=0-5` to free up cores 3–5 for the host) and
reboot.

To recover from a wedged host: console access (display + keyboard, or
IPMI) and edit `/etc/default/grub` directly.

### chrony offset growing over time

Likely the host TSC is genuinely unreliable and BIOS settings didn't
fix it. Apply the `tsc=reliable` fallback from the BIOS checklist. If
that doesn't help, accept kvm-clock and increase chrony's polling:

```
# /etc/chrony/chrony.conf inside the VM
maxupdateskew 100.0
makestep 0.001 -1
```

### radiod can't find HT pairs even with proper guest topology

Sigmond reads `thread_siblings_list` and falls back to consecutive
pairs if every entry is a singleton. With the `args: -smp ...threads=2`
line in place, sibling lists should each have two CPUs in them. If
they still look like singletons:

```bash
# Inside VM
cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
# Should be "0,1" not "0"
```

If singleton, the `args:` line did not take effect — re-check Phase 2.

### foreign drop-ins reported by sigmond

Older `wd-ctl` / hf-timestd setup scripts wrote their own affinity
drop-ins. `sudo smd apply` clears them out and replaces with sigmond's
managed drop-in. The warning is harmless but easy to fix.

---

## Companion documents

- `wsprdaemon-proxmox-vm-setup.md` — PCIe USB passthrough (do this first).
- `wsprdaemon-proxmox-bios-checklist.md` — offline reference for BIOS visit (Phase 1).
- This document — runbook for everything CPU and clock related.

---

## Appendix A — chrony migration (only if starting fresh)

This is the procedure used when chrony is **not** already installed on
the VM. AI6VN-1 already has chrony; skip this appendix.

```bash
# Inside VM
sudo systemctl stop ntp ntpsec systemd-timesyncd 2>/dev/null
sudo apt purge ntp ntpsec 2>/dev/null
sudo systemctl disable systemd-timesyncd 2>/dev/null

sudo apt update
sudo apt install -y chrony

sudo tee /etc/chrony/chrony.conf <<'EOF'
# NTP pools
pool 0.us.pool.ntp.org iburst maxsources 4
pool 1.us.pool.ntp.org iburst maxsources 2
pool 2.us.pool.ntp.org iburst maxsources 2

# Stratum-1 reference servers (Bay Area / US)
server time.nist.gov iburst
server time.apple.com iburst

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

sudo systemctl enable --now chrony
sudo systemctl status chrony
chronyc tracking
```

Healthy `chronyc tracking`:

```
Reference ID    : ...
Stratum         : 2 or 3
System time     : 0.000... seconds slow/fast of NTP time
Last offset     : ±0.000... seconds
RMS offset      : 0.000... seconds   ← target < 0.001 (1 ms)
```

---

## Appendix B — future tuning topics not covered here

- IRQ affinity for passed-through devices (xhci IRQs land on a host
  CPU outside the VM pool).
- NUMA — N/A on this single-socket box.
- Multi-VM allocation — N/A; this box runs one VM.
- ZFS ARC tuning to keep host memory pressure off the VM.
- Backup / snapshot strategy that is aware of the running radiod state.
