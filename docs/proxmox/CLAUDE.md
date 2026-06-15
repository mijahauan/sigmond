# Proxmox VM Setup for Sigmond — Context for Claude Code

This directory documents the Proxmox VE host configuration required to run sigmond (and wsprdaemon / ka9q-radio / radiod) inside a virtual machine with full bare-metal SDR performance.

## When to read these documents

Read both companion documents before doing any work that touches:

- Proxmox VM configuration (`/etc/pve/qemu-server/*.conf`)
- VFIO / PCIe passthrough configuration on the host
- Host kernel command line (`/etc/default/grub`)
- VM CPU pinning, affinity, or topology
- Time synchronization (chrony, ntp, systemd-timesyncd)
- BIOS settings related to virtualization, IOMMU, C-states, or TSC

These documents capture an afternoon of debugging that produced a working configuration. **Do not regenerate or rewrite this configuration from scratch** — read the docs first and follow the established procedure.

## Documents in this directory

### `wsprdaemon-proxmox-vm-setup.md`
PCIe USB controller passthrough setup. Covers:
- Why USB device passthrough (`usb: host=...`) cannot achieve 500 MB/s sustained from the RX-888 — only full PCIe controller passthrough can
- The AMD Renoir/Cezanne reset bug and why vfio-pci must bind at boot (not at VM start)
- IOMMU group verification
- The q35 machine type requirement
- Step-by-step working procedure
- Troubleshooting common failure modes

This is the **first** document to read. The setup it describes is the prerequisite for everything else.

### `wsprdaemon-proxmox-cpu-clock-tuning.md`
CPU isolation, hyperthread pair exposure, and clock accuracy. Covers:
- Identifying host hyperthread pairs (sequential vs split pairing)
- Choosing VM CPU affinity that preserves hyperthread pair topology (worked example for Ryzen 5 5560U)
- Exposing hyperthread topology to the guest via QEMU `args:` (Proxmox's `qm` CLI does not expose `threads` directly)
- Isolating VM CPUs from the host scheduler with `isolcpus`, `nohz_full`, `rcu_nocbs` (Part 4 — host side)
- **Isolating guest kernel work off radiod's HT pairs with in-VM `isolcpus=0-(2N-1)` for N radiod instances (Part 4b — second layer)**
- Migrating from ntpd / systemd-timesyncd to chrony inside the VM with low-latency stratum-1 servers
- Addressing the AMD APU "TSC found unstable" warning via `tsc=reliable processor.max_cstate=1` (works around BIOS-locked mini PCs)
- **Per-vCPU pinning via Proxmox hookscript (Part 9) — required for SMT to actually work end-to-end on AMD Ryzen guests**
- **Per-pCPU frequency caps for multi-radiod stations (Part 9b) — limits memory-bandwidth contention when workers wake at WSPR minute boundaries**
- Validation tests for each layer
- Operational observations under load and IRQ affinity hygiene (Part 10)
- Known operational gotchas surfaced by guest reboots (Part 11): chrony auto-start blocked by hf-timestd ordering bug, psk-recorder `Type=notify` timeout
- **VM-specific gotchas that explain why FFT pegs at 100% (Part 12)**: QEMU emulates a fake cache topology unless `host-cache-info=on` is set; `args: -cpu` silently drops Proxmox's KVM paravirt flags; amd-pstate-epp + nohz_full parks CPUs at scaling_min_freq under sustained 100% load; nested-virt overhead from exposed `svm` flag; 8 MB L3 only supports one radiod per VM on Cezanne U-series.

This is the **second** document, addressing tuning work that follows the basic passthrough setup.

## Critical constraints that must not be violated

These are non-negotiable based on the working configuration. If a future change conflicts with any of these, stop and reconsider.

### Host configuration
- **vfio-pci must bind to the USB controllers at boot**, before xhci_pci touches them. Live driver detach at VM start time causes host reboot on AMD APUs (the AMD reset bug). The `softdep xhci_pci pre: vfio-pci` line in `/etc/modprobe.d/vfio.conf` is essential.
- **The host has no USB at all** after vfio binding on AMD APUs (no separate chipset USB controller). This is normal. Keyboard/mouse plugged into the host appear inside the VM. Host management is SSH-only.
- **IOMMU groups must isolate the USB controllers** from critical host devices (boot drive, network). On AMD Renoir/Cezanne this is clean by default. If a future hardware change breaks this, the `pcie_acs_override` kernel patch becomes necessary.
- **Kernel command line requires** `amd_iommu=on iommu=pt` (or `intel_iommu=on iommu=pt` on Intel hosts).

### VM configuration
- **`machine: q35` is mandatory** for PCIe passthrough. The default i440fx fails immediately with "q35 machine model is not enabled at PCI.pm line 514."
- **No `usb*` device passthrough lines** — the host no longer has those USB buses, so they fail at VM start. The RX-888 enumerates inside the VM via the passed-through xHCI controllers.
- **CPU affinity must include both hyperthread siblings of each physical core**, not just sequential CPU numbers. The Ryzen 5 5560U / 5825U / 7530U U-series mobile parts use **sequential pairing** (cores 0+1 are HT siblings, 2+3, 4+5, etc.), so `affinity: 0-9` correctly preserves five HT pairs. Some AMD desktop/server parts use **split pairing** (cores 0+N are siblings) where `affinity: 0-4,8-12` is the equivalent. Verify with `/sys/devices/system/cpu/cpuN/topology/thread_siblings_list` before assuming.
- **Guest topology requires `args: -smp ...,threads=2`** in the conf file because Proxmox's `qm` CLI does not expose the threads parameter. Without this, the guest sees a flat topology and radiod's hyperthread pair detection fails.
- **`-cpu host,topoext=on` is required in `args:`** for AMD Ryzen guests. Without `topoext`, QEMU emits a hyperthreading warning at start and the guest CPU doesn't properly advertise SMT to the OS, even though Linux accepts the topology. Proxmox's `cpu:` field cannot set `topoext` (security restriction), so it must go in `args:`.
- **Per-vCPU pinning hookscript is REQUIRED** (`/var/lib/vz/snippets/cpu-pin-VMID.sh` attached via `--hookscript`). Without it, all vCPU threads bunch on a single host CPU under the Linux scheduler with `isolcpus=` in effect — a silent ~10x performance failure. The `affinity:` field is process-level, not per-vCPU, and does not prevent this. See Part 9 of the tuning doc.
- **Guest kernel needs its own `isolcpus=` covering radiod's HT pairs.** Inside the VM, `GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=0-(2N-1) nohz_full=0-(2N-1) rcu_nocbs=0-(2N-1)"` where N is the number of radiod instances. On AI6VN-1 with 3 radiods (KFS-NW, KFS-OMNI, KFS-SW), this is `isolcpus=0-5`. The host-side isolcpus alone doesn't keep the guest kernel off radiod's HT pair — that's what the second layer is for. See Part 4b.
- **`args: -cpu` must include `host-cache-info=on`.** Without it, QEMU advertises a fictional cache topology to the guest (typically claiming a 16 MB L3 on a host with an 8 MB L3). FFTW's wisdom planner reads that and picks codelet block sizes for the fake cache, causing constant DRAM evictions on every FFT pass. Symptom: radiod FFT thread sustains ~100% CPU when it should be ~50%. See Part 12a.
- **`args: -cpu` must include `+kvm_pv_eoi,+kvm_pv_unhalt`.** Proxmox normally adds these from `cpu: host`, but when `args:` also has a `-cpu` line QEMU uses the LAST one — silently dropping the paravirt flags. They reduce VM exits on interrupt EOI and spinlock waits. See Part 12b.
- **`args: -cpu` should include `-svm`** to disable nested-virt overhead for SDR guests that never run their own VMs. See Part 12d.
- **Hookscript must pin `scaling_min_freq == scaling_max_freq`** for VM-assigned pCPUs, not just set `scaling_max_freq`. `amd-pstate-epp` + `nohz_full` interaction parks the CPU at scaling_min_freq under sustained load (no scheduler tick → driver never sees load → never boosts), even with `governor=performance` and `EPP=performance`. See Part 12c and `cpu-pin-VMID.sh.example`.
- **One radiod per VM on Cezanne U-series mini PCs.** 8 MB L3 is enough for one radiod's FFT pipeline plus the wsprdaemon decoder/uploader workload but cannot support 2 or 3 radiods concurrently (FFT threads peg at 99% from L3 contention). Bare-metal Debian 13 on the same 5560U fits 3 radiods because KVM's address-translation tables don't compete for L1/L2 cache. See Part 12e.

### Complete recommended `args:` line (5560U / one radiod / sequential HT pairs)
```
args: -smp 10,sockets=1,cores=5,threads=2,maxcpus=10 -cpu host,host-cache-info=on,topoext=on,+kvm_pv_eoi,+kvm_pv_unhalt,-svm
```

### Time synchronization
- **Use chrony, not ntpd or systemd-timesyncd** inside the VM. chrony tolerates virtualized clocks much better.
- **The guest should use kvm-clock** as its clocksource. This is usually automatic but worth verifying with `cat /sys/devices/system/clocksource/clocksource0/current_clocksource`.
- **Host kernel must have `tsc=reliable processor.max_cstate=1`** in `GRUB_CMDLINE_LINUX_DEFAULT` to prevent the AMD APU TSC instability that otherwise destabilizes guest timing.
- **Use explicit stratum-1 servers in chrony.conf**, not just regional pools — pools often select stratum-2/3 backends with worse latency. The validated reference list (good from California) is `time-b-wwv.nist.gov`, `time.cloudflare.com`, `time.google.com`, `time.apple.com`.

## Hardware context

The reference system is a **KAMRUI mini PC with AMD Ryzen 5 5560U** (Renoir/Cezanne APU, 6 cores / 12 threads). Key specifics:

- USB controllers `05:00.3` and `05:00.4`, both with PCI ID `1022:1639`
- iGPU at `05:00.0` (sibling function on same physical device — relevant for the reset bug)
- Boot NVMe at `04:00.0` (separate IOMMU group, safe to leave with host)
- Two Intel I225-V Ethernet controllers (separate IOMMU groups)
- HT pairing is **sequential** (core 0 = CPUs 0+1, core 1 = CPUs 2+3, etc.)
- BIOS is **locked-down AMI** — does not expose AMD CBS / C-state controls. TSC stability is achieved via kernel parameters (`tsc=reliable processor.max_cstate=1`) rather than BIOS.

Validated working configuration achieves:
- 500 MB/s sustained RX-888 USB throughput with no transfer errors
- ~63 µs RMS clock offset to NIST stratum-1 reference via chrony
- 5 physical cores (10 vCPUs as 5 HT pairs) dedicated to VM, isolated from host scheduler

Other AMD Ryzen U-series parts (5700U, 5825U, 7530U) found in similar mini PC boxes (KAMRUI, Beelink, Minisforum, GMKtec, NiPoGi, ACEMAGIC) use the same Renoir/Cezanne or related architectures and follow the same procedure. **Re-verify IOMMU groups and PCI IDs on each new machine** — they're usually consistent across this generation but it's not guaranteed.

If working on completely different hardware (Intel, older AMD, server-class), re-verify everything from Part 1 of the setup doc.

## Task-specific guidance

### When asked to debug VM performance issues
1. First check whether the basic setup in `wsprdaemon-proxmox-vm-setup.md` is intact: `lspci -nnk -s 05:00.3` should show `vfio-pci` as kernel driver in use
2. Then check CPU isolation per `wsprdaemon-proxmox-cpu-clock-tuning.md` Part 8 (validation tests for each layer)
3. Verify per-vCPU pinning fired on the host: `journalctl -t cpu-pin-101 --since '1 hour ago'` should show 10 `cpu-pin: VM 101 vCPU N -> host pCPU N` entries plus 10 `cpu-cap: pCPU N max=... MHz` entries on every VM start
4. Inside the VM, run `smd admin diag cpu-affinity` and `smd admin validate` — `cpu_isolation_runtime` should report `radiod cores [...] uncontested`
5. After any guest reboot, also check Part 11: chrony may need a manual `systemctl start chrony.service` until the hf-timestd ordering bug is fixed
6. Don't suggest USB device passthrough as an alternative — it cannot meet the bandwidth requirement

### When asked to add a new VM or migrate to new hardware
1. Re-verify IOMMU groups on the new hardware
2. Re-identify the USB controller PCI IDs (may not be 1022:1639 on different chips)
3. Follow the procedure in the setup doc rather than copying configs blindly

### When asked about clock accuracy issues
1. Verify chrony is running and `chronyc tracking` shows reasonable offset
2. Check guest clocksource is kvm-clock
3. Investigate host TSC stability per Part 6 of the tuning doc
4. SDR timestamping for HamSCI GRAPE / WSPR / FT8 needs sub-millisecond accuracy — this is non-negotiable for the application

### When asked to update or upgrade Proxmox / kernel
1. Back up `/etc/pve/qemu-server/*.conf`, `/etc/modprobe.d/vfio.conf`, `/etc/default/grub`, `/etc/modules` first
2. After upgrade, verify `lspci -nnk -s <usb-controller-addr>` still shows `vfio-pci` — kernel updates can break the early bind
3. Run `update-initramfs -u -k all` after any modprobe.d changes
