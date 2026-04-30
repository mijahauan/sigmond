# Proxmox VM Setup for Sigma — Context for Claude Code

This directory documents the Proxmox VE host configuration required to run sigma (and wsprdaemon / ka9q-radio / radiod) inside a virtual machine with full bare-metal SDR performance.

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
CPU isolation, hyperthread pair exposure, per-vCPU pinning, and clock accuracy. Phased runbook with explicit reboot points. Covers:
- Verifying host hyperthread pairing (5560U is sequential — `0,1 / 2,3 / …`)
- Two-layer CPU partition: host gets cores 4–5 (CPUs 8–11); VM gets cores 0–3 (CPUs 0–7 = 8 vCPUs)
- Exposing real HT pairs to the guest via QEMU `args: -smp ...,threads=2` (Proxmox's `qm` CLI does not expose `threads` directly)
- **Per-vCPU pinning** via Proxmox hookscript so vCPU N → host CPU N is fixed (mandatory — process-wide affinity alone lets vCPU threads float and breaks L1/L2 sharing for radiod's HT pair)
- Two-layer `isolcpus`: host `isolcpus=0-7` keeps Proxmox kernel off VM cores; guest `isolcpus=0,1` keeps guest kernel off radiod's pair
- Cross-checking against sigmond's existing affinity logic (`lib/sigmond/cpu.py`, `harmonize.py`)
- Validation tests at each phase

This is the **second** document, addressing tuning work that follows the basic passthrough setup.

### `wsprdaemon-proxmox-bios-checklist.md`
**Offline-friendly** reference for the BIOS visit (Phase 1 of the tuning runbook). Read this on a phone or Mac while sitting in BIOS — Claude Code is not available pre-OS. Covers C-state disabling, HPET, Invariant TSC, vendor-name variations, fallback to `tsc=reliable` if BIOS can't fix it.

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
- **CPU affinity must include both hyperthread siblings of each physical core.** On this 5560U with **sequential** pairing, `affinity: 0-7` gives 4 physical cores worth (cores 0–3) cleanly. Verify pairing with `cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list | sort -u` — the docs were originally written assuming AMD split pairing, which is **wrong for this 5560U**.
- **`cores: 4` and `args: -smp 8,sockets=1,cores=4,threads=2,maxcpus=8`** in `101.conf`. Proxmox's `qm` CLI does not expose `threads`, so the only way to give the guest real HT topology is the raw `-smp` injection. Without this the guest sees a flat topology.
- **Per-vCPU pinning is mandatory, not optional.** Process-wide `affinity:` alone lets vCPU threads float across host CPUs 0–7 and breaks L1/L2 sharing for radiod's HT pair. A Proxmox hookscript (`/var/lib/vz/snippets/vm-101-affinity.sh`, registered with `qm set 101 --hookscript local:snippets/vm-101-affinity.sh`) pins each vCPU to a specific host CPU on `post-start`.
- **Two layers of CPU isolation:**
  - Host kernel cmdline: `isolcpus=0-7 nohz_full=0-7 rcu_nocbs=0-7` — keeps Proxmox kernel work off VM cores.
  - Guest kernel cmdline: `isolcpus=0,1 nohz_full=0,1 rcu_nocbs=0,1` — keeps guest kernel work off radiod's HT pair (vCPU 0,1).
- **Host reservation:** cores 4–5 (CPUs 8–11) belong to Proxmox. The host runs only on those four logical CPUs once `isolcpus=0-7` is in effect — that is enough for SSH, Proxmox UI, networking, and ZFS, but tight. Don't run anything else on this host.

### Time synchronization
- **Use chrony, not ntpd or systemd-timesyncd** inside the VM. chrony tolerates virtualized clocks much better.
- **The guest should use kvm-clock** as its clocksource. This is usually automatic but worth verifying with `cat /sys/devices/system/clocksource/clocksource0/current_clocksource`.
- **AI6VN-1 status (2026-04-29):** chrony is already active, RMS offset ~80 µs, kvm-clock is the guest clocksource, ntpsec / systemd-timesyncd are inactive. Phase 5 of the tuning runbook can be skipped on this VM.

### Hardware (this host)
- **CPU:** AMD Ryzen 5 5560U (Lucienne, Zen 2 family, 6 cores / 12 threads).
- **HT pairing:** sequential — `thread_siblings_list` for cpu0 is "0,1", cpu2 is "2,3", etc. NOT the split scheme (cpu0 siblings = "0,8") the original doc claimed for AMD APUs. Verify before committing.
- VM: 101, hostname `AI6VN-1`.

## Hardware context

This system is an AMD Ryzen 5 **5560U** (Lucienne) mini PC. Key specifics:

- USB controllers `05:00.3` and `05:00.4`, both with PCI ID `1022:1639`
- iGPU at `05:00.0` (sibling function — relevant for the reset bug)
- Boot NVMe at `04:00.0` (separate IOMMU group, safe)
- Two Intel I225-V Ethernet controllers (separate IOMMU groups)
- BIOS reports unstable TSC (common quirk on these mini PCs)
- 6 cores / 12 threads, **sequential HT pairing** (verified)

If working on different hardware, re-verify IOMMU groups, PCI device IDs, and HT pairing scheme before applying any of the procedures.

## Task-specific guidance

### When asked to debug VM performance issues
1. First check whether the basic setup in `wsprdaemon-proxmox-vm-setup.md` is intact: `lspci -nnk -s 05:00.3` should show `vfio-pci` as kernel driver in use
2. Then check CPU isolation per `wsprdaemon-proxmox-cpu-clock-tuning.md` Phase 6 (validation)
3. Verify per-vCPU pinning fired: `journalctl -t vm-101-affinity --since '1 hour ago'` should show 8 "pinned vCPU N to host CPU N" entries on every VM start
4. Inside the VM, run `sudo smd diag cpu-affinity` — it should report no warnings, with radiod on vCPU 0,1 and other services on vCPU 2-7
5. Don't suggest USB device passthrough as an alternative — it cannot meet the bandwidth requirement

### When asked to add a new VM or migrate to new hardware
1. Re-verify IOMMU groups on the new hardware
2. Re-identify the USB controller PCI IDs (may not be 1022:1639 on different chips)
3. Follow the procedure in the setup doc rather than copying configs blindly

### When asked about clock accuracy issues
1. Verify chrony is running and `chronyc tracking` shows reasonable offset
2. Check guest clocksource is kvm-clock
3. Investigate host TSC stability per `wsprdaemon-proxmox-bios-checklist.md`; the tuning doc references the BIOS doc from Phase 1
4. SDR timestamping for HamSCI GRAPE / WSPR / FT8 needs sub-millisecond accuracy — this is non-negotiable for the application

### When asked to update or upgrade Proxmox / kernel
1. Back up `/etc/pve/qemu-server/*.conf`, `/etc/modprobe.d/vfio.conf`, `/etc/default/grub`, `/etc/modules` first
2. After upgrade, verify `lspci -nnk -s <usb-controller-addr>` still shows `vfio-pci` — kernel updates can break the early bind
3. Run `update-initramfs -u -k all` after any modprobe.d changes
