# Setting Up WSPRDAEMON / Sigma in a Virtual Machine Running Under Proxmox VE

**Author:** Rob Robinett (AI6VN / W0DAS)
**Date:** April 2026
**Hardware tested:** AMD Ryzen-class mini PC (Renoir/Cezanne APU) running Proxmox VE
**Goal:** Run wsprdaemon / sigma + ka9q-radio inside a Proxmox VM with the RX-888 mk2 SDR achieving full bare-metal performance — sustained ~500 MB/s USB 3.0 transfer with no dropped samples.

---

## Why this is hard

The RX-888 mk2 streams roughly 500 MB/s of IQ data over USB 3.0 SuperSpeed. QEMU's emulated USB stack — even with `usb3=1` forcing XHCI emulation — cannot keep up. CPU emulation overhead causes USB transfer errors and dropped samples, breaking radiod.

The only way to achieve bare-metal performance inside a VM is **full PCIe passthrough of the USB host controller** to the VM, so the VM's kernel talks DMA-direct to the xHCI hardware. There is no QEMU layer in the data path.

On AMD Renoir/Cezanne APUs, this is complicated by the fact that the USB 3.1 controllers (`05:00.3` and `05:00.4`) are sibling functions of the integrated GPU (`05:00.0`). Doing a live driver detach when starting the VM can destabilize the iGPU and cause a host reboot. The workaround is to bind `vfio-pci` to the USB controllers **at boot**, before the host kernel ever touches them.

---

## Prerequisites

- Proxmox VE 8.x or 9.x host installed
- AMD or Intel CPU with IOMMU support enabled in BIOS (`AMD-Vi` / `VT-d`)
- A guest VM already created (this guide assumes VM ID 101)
- SSH access to the host (because the host will lose all USB during this process, including keyboard/mouse on AMD APUs where there is no separate chipset USB controller)
- Knowledge of which PCI devices are your USB controllers — get this with:

```bash
lspci -nn | grep -i usb
```

---

## Step 1 — Verify IOMMU is enabled

```bash
dmesg | grep -e DMAR -e IOMMU
```

You should see lines like `AMD-Vi: ... initialized` or `DMAR: IOMMU enabled`. If not, enable IOMMU in BIOS, then add to `/etc/default/grub`:

```
GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=on iommu=pt"
```

(For Intel: `intel_iommu=on iommu=pt`.) Then `update-grub && reboot`.

---

## Step 2 — Verify IOMMU groups isolate the USB controllers

```bash
for d in /sys/kernel/iommu_groups/*/devices/*; do
    n=${d#*/iommu_groups/*}; n=${n%%/*}
    printf 'IOMMU Group %s: ' "$n"
    lspci -nns "${d##*/}"
done | sort -t: -k2 -n
```

Find the lines for your USB controllers. On AMD Renoir/Cezanne with this PC, they look like:

```
IOMMU Group 17: 05:00.3 USB controller [0c03]: AMD Renoir/Cezanne USB 3.1 [1022:1639]
IOMMU Group 18: 05:00.4 USB controller [0c03]: AMD Renoir/Cezanne USB 3.1 [1022:1639]
```

**Each USB controller must be in its own IOMMU group**, isolated from the boot drive controller, the GPU, and other critical devices. If they share a group with something you can't pass through, you'll need the `pcie_acs_override` patch — but with the systems that we have, IOMMU groups are clean out of the box.

---

## Step 3 — Identify the vendor:device ID and verify uniqueness

```bash
lspci -nn | grep 1022:1639
```

(Replace `1022:1639` with whatever your USB controllers report.) This must return **only** the USB controllers — nothing else. If other devices share the ID, you'll need a different binding method (per-PCI-address rather than by ID).

---

## Step 4 — Configure vfio-pci to bind at boot

Ensure vfio modules load early. Edit `/etc/modules`:

```
vfio
vfio_iommu_type1
vfio_pci
```

(Note: `vfio_virqfd` was merged into core `vfio` in newer kernels; if you see a "Failed to find module 'vfio_virqfd'" warning at boot, just remove that line.)

Create `/etc/modprobe.d/vfio.conf`:

```bash
cat > /etc/modprobe.d/vfio.conf <<'EOF'
# Bind both AMD Renoir/Cezanne USB 3.1 controllers to vfio-pci at boot
# 1022:1639 = 05:00.3 and 05:00.4 (USB controllers only; verified unique)
options vfio-pci ids=1022:1639 disable_vga=1

# Make sure vfio-pci binds before xhci_pci tries to claim them
softdep xhci_pci pre: vfio-pci
EOF
```

The `softdep` line is critical. Without it, `xhci_pci` may load first and claim the controllers, then we'd have to do a live detach when the VM starts — which is exactly what causes AMD APUs to reboot.

---

## Step 5 — Rebuild initramfs and reboot

```bash
update-initramfs -u -k all
reboot
```

---

## Step 6 — Verify vfio-pci owns the controllers after reboot

```bash
lspci -nnk -s 05:00.3
lspci -nnk -s 05:00.4
```

For each, you must see:

```
Kernel driver in use: vfio-pci
Kernel modules: xhci_pci
```

If you see `xhci_pci` (not `vfio-pci`) as the driver in use, the early bind failed — go back and check `softdep` in `/etc/modprobe.d/vfio.conf` and rerun `update-initramfs -u -k all`.

Also note that on AMD APUs, `lsusb` on the host will return nothing — there's no separate chipset USB controller. The host has no USB. This is normal and expected.

---

## Step 7 — Configure the VM

The VM must use the **q35** machine type (PCIe-capable). The default `i440fx` chipset will fail with:

```
q35 machine model is not enabled at /usr/share/perl5/PVE/QemuServer/PCI.pm line 514.
```

Apply the config:

```bash
qm set 101 --machine q35
qm set 101 -hostpci0 0000:05:00.3,pcie=1
qm set 101 -hostpci1 0000:05:00.4,pcie=1
```

Remove any old USB device passthrough lines (they will fail because the host no longer has those USB buses):

```bash
qm config 101 | grep ^usb
qm set 101 --delete usb0,usb1,usb2   # adjust to match what's actually there
```

Update the boot order to drop any USB references:

```bash
qm set 101 --boot order=scsi0
```

Verify the final config:

```bash
cat /etc/pve/qemu-server/101.conf
```

Should look like:

```
affinity: 0-9
boot: order=scsi0
cores: 10
cpu: host
hostpci0: 0000:05:00.3,pcie=1
hostpci1: 0000:05:00.4,pcie=1
machine: q35
memory: 12288
...
```

---

## Step 8 — Start the VM and verify

In one SSH session, tail the host kernel log:

```bash
journalctl -kf
```

In another, start the VM:

```bash
qm start 101
```

A successful startup looks like:

```
vfio-pci 0000:05:00.3: resetting
vfio-pci 0000:05:00.3: reset done
vfio-pci 0000:05:00.4: resetting
vfio-pci 0000:05:00.4: reset done
```

— two reset/done pairs per controller, no AMD GPU errors, no AER errors, no host wobble.

Inside the VM, verify the RX-888 enumerates as a SuperSpeed device:

```bash
lsusb
```

Look for the RX-888 on a `Linux Foundation 3.0 root hub` (USB 3.0):

```
Bus 004 Device 002: ID 04b4:00f1 Cypress Semiconductor Corp. RX888mk2
```

And confirm SuperSpeed link rate:

```bash
lsusb -t
```

The RX-888 line should end in `5000M` (USB 3.0 SuperSpeed), not `480M` (USB 2.0).

---

## Step 9 — Re-enable autostart

Once you've confirmed everything works:

```bash
qm set 101 --onboot 1
```

---

## Step 10 — Snapshot the working configuration

This setup is hard-won. Save copies of all the config files:

```bash
mkdir -p /root/proxmox-passthrough-backup
cp /etc/pve/qemu-server/101.conf /root/proxmox-passthrough-backup/
cp /etc/modprobe.d/vfio.conf /root/proxmox-passthrough-backup/
cp /etc/default/grub /root/proxmox-passthrough-backup/
cp /etc/modules /root/proxmox-passthrough-backup/
date > /root/proxmox-passthrough-backup/saved-on.txt
```

---

## Troubleshooting

### Host reboots when starting the VM

This is the AMD APU "reset bug." It happens when `vfio-pci` is doing a live detach of the USB controller at VM start, and the reset destabilizes the iGPU sibling function. Fix: ensure `softdep xhci_pci pre: vfio-pci` is in `/etc/modprobe.d/vfio.conf` and `update-initramfs -u -k all` was run. Verify `lspci -nnk -s <addr>` shows `vfio-pci` as the driver in use *before* you start the VM.

### "q35 machine model is not enabled" error

You forgot `qm set 101 --machine q35`. The default i440fx machine type doesn't expose a PCIe bus, so PCIe passthrough is impossible.

### RX-888 enumerates as USB 2.0 (480M) instead of USB 3.0 (5000M)

Check the cable. The RX-888 demands a high-quality USB 3.0 cable; cheap or long cables drop to 2.0. Also verify the device is plugged into a USB 3.0 port on the mini PC (usually blue).

### Host has no USB at all after reboot

On AMD APUs this is normal and expected — both `1022:1639` controllers are now owned by `vfio-pci` and there's no separate chipset USB controller. The host is SSH-only. Keyboard/mouse plugged into the box will appear inside the VM.

### USB transfer errors / dropped samples inside the VM

If you see `xhci_hcd` errors or RX-888 watchdog firmware kicking in, it usually points to:

- CPU contention — see "CPU isolation" notes below
- Power delivery — some mini PCs throttle USB power under load
- Cable / port quality

Check inside the VM:

```bash
dmesg | grep -i 'xhci\|usb' | grep -iE 'error|fail|reset|halt' | tail
```

A clean run should show essentially nothing here.

---

## Performance considerations not covered above

These are referenced as separate optimization steps once basic passthrough is working:

- **Clock accuracy / TSC stability** — the `kvm: SMP vm created on host with unstable TSC` warning. Important for SDR timestamping.
- **CPU isolation** — keep host workloads off the cores assigned to the VM (`isolcpus`, `nohz_full`, `rcu_nocbs`).
- **NUMA / vCPU pinning** — pin guest vCPUs to specific physical cores rather than just CPU affinity.
- **Hyperthread-pair preservation** — radiod expects to pin to a hyperthread pair; ensure the VM's guest topology exposes pairs that map to real host pairs.
- **Migration from NTP to chrony** — chrony handles clock discipline better in virtualized environments.

These are addressed in companion documents and in the radiod / wsprdaemon configuration.
