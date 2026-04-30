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
- Isolating VM CPUs from the host scheduler with `isolcpus`, `nohz_full`, `rcu_nocbs`
- Migrating from ntpd / systemd-timesyncd to chrony inside the VM with low-latency stratum-1 servers
- Addressing the AMD APU "TSC found unstable" warning via `tsc=reliable processor.max_cstate=1` (works around BIOS-locked mini PCs)
- **Per-vCPU pinning via Proxmox hookscript (Part 9) — required for SMT to actually work end-to-end on AMD Ryzen guests**
- Validation tests for each layer
- Operational observations under load and IRQ affinity hygiene (Part 10)

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
2. Then check CPU isolation per `wsprdaemon-proxmox-cpu-clock-tuning.md` Part 8
3. Don't suggest USB device passthrough as an alternative — it cannot meet the bandwidth requirement

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
