# BIOS Checklist for the WSPR / Sigma Proxmox Host

**Author:** Rob Robinett (AI6VN / W0DAS)
**Date:** April 2026
**Companion to:** `wsprdaemon-proxmox-cpu-clock-tuning.md`
**Purpose:** Standalone reference you can read **outside Claude Code** (on
your phone, printed, or via the Claude app on a laptop) while you are
sitting in the BIOS of the Proxmox host. Claude Code is not running
during this visit — this document is what you take with you.

---

## Why this BIOS visit matters

The host BIOS controls behaviors that the OS cannot fully override:

1. **TSC stability.** AMD Ryzen mini PCs commonly default to power-saving
   states (deep C-states, dynamic frequency scaling) that make the Time
   Stamp Counter drift relative to wall-clock. The Linux kernel detects
   this and falls back to a slower, noisier clocksource (HPET), and KVM
   then warns "guest TSC will not be reliable." That hurts SDR
   timestamping accuracy for HamSCI GRAPE / WSPR / FT8.

2. **HPET availability.** When the kernel can't trust TSC it needs HPET
   as a backup. Some BIOSes ship with HPET disabled by default.

3. **IOMMU / virtualization extensions.** Already verified working
   (otherwise PCIe USB passthrough wouldn't run), but worth confirming
   while you're in there.

The goal of the visit: make the OS see TSC as stable so KVM stops
warning, and so chrony in the guest can hold sub-millisecond accuracy
without fighting the firmware.

---

## Before you reboot

- [ ] Confirm the host can reach the BIOS via the keyboard plugged into
      it (host has no USB once vfio-pci binds, but BIOS is pre-OS, so
      USB still works at the firmware level — the keyboard works in
      BIOS even though it disappears once Linux boots).
- [ ] Have this document open on your phone or another machine.
- [ ] Note the BIOS entry key for this box (typical: **Del**, **F2**,
      **Esc**, or **F11** — for most mini PCs it's **Del** or **F2**).
- [ ] Note that some changes will require multiple reboots into BIOS.

---

## Disable these (stability over power saving)

Names vary by vendor. Look under **Advanced**, **AMD CBS** (Custom BIOS
Settings), **Power Management**, or **CPU Configuration**. AMD CBS is
the most likely location for the C-state controls. If AMD CBS is not
visible, look for an "Advanced Mode" toggle (often a key like F7 or a
password prompt) or a menu labeled "Chipset" → "AMD CBS".

| Setting | Common names | Set to |
|---|---|---|
| Global C-State Control | "Global C-state Control", "C-State Control", "CPU C-States" | **Disabled** |
| Package C6 | "Pkg C6", "Package C6 State", "C6 State" | **Disabled** |
| Core C6 | "Core C6", "CC6", "CC6 State" | **Disabled** |
| Cool'n'Quiet | "Cool'n'Quiet", "AMD Cool'n'Quiet", "CnQ" | **Disabled** |
| Power Supply Idle | "Power Supply Idle Control" | **Typical Current Idle** (not Low) |
| SVM hibernate quirks | "S0i3", "Modern Standby", "Connected Standby" | **Disabled** |

**Why each one:** the deep C-states (anything past C2) and Cool'n'Quiet
both stop the TSC or change its rate when the core sleeps. "Power Supply
Idle = Low" can cause the chip to drop into a deeper idle than the OS
expects. S0i3 / Modern Standby is for laptops and adds another sleep
state Linux doesn't always handle cleanly.

---

## Enable these

| Setting | Common names | Set to |
|---|---|---|
| HPET | "HPET", "High Precision Event Timer" | **Enabled** |
| Invariant TSC | "Invariant TSC", "TSC Sync", "Constant TSC" | **Enabled** (if present) |
| IOMMU / AMD-Vi | "IOMMU", "AMD-Vi", "AMD Virtualization" | **Enabled** (verify — should already be on) |
| SVM Mode | "SVM", "SVM Mode", "AMD-V" | **Enabled** (verify — should already be on) |
| Above 4G Decoding | "Above 4G Decoding", "Above 4G MMIO" | **Enabled** (verify — should already be on) |

---

## Leave these alone

- **SMT (Simultaneous Multithreading) — keep ENABLED.** We need
  hyperthread pairs. Disabling SMT breaks the entire allocation plan.
- **Resizable BAR / ReBAR.** Unrelated to this work; don't change.
- **Memory frequency / XMP.** Don't change unless you know the system is
  unstable for memory reasons.
- **Secure Boot.** Don't change; if it was working before, leave it.
- **Boot order.** Don't change unless you specifically need to.

---

## What to do if a setting is not visible

1. **Look for an "Advanced Mode" toggle.** Many AMI-based BIOSes hide
   AMD CBS until you press F7 or enter a service password. Some Beelink
   / Minisforum / ASRock 4x4 boards ship with "Advanced" hidden.

2. **Check the parent menu.** "Cool'n'Quiet" is sometimes under "Power",
   sometimes under "AMD CBS → CPU Common Options", sometimes under
   "OC / Tweaker" — same option, different paths per vendor.

3. **If a setting genuinely doesn't exist on this BIOS, skip it.** The
   fallback path (kernel `tsc=reliable`, see below) catches what BIOS
   can't.

4. **Take a photo** of any menu where you're unsure rather than guessing.
   Out of BIOS you can ask Claude (app, web, or Code) and we'll figure
   it out together.

---

## While you're in there: notes to capture

Write these down for later (so we don't have to revisit BIOS to find them):

- BIOS version / build date
- CPU info as reported by BIOS (model, microcode revision if shown)
- Total memory and frequency
- Whether AMD CBS submenu was visible without unlocking
- Whether HPET was already enabled or you turned it on
- Whether there was an "Invariant TSC" option at all (varies by AGESA version)

---

## Save and reboot

Save changes (typically **F10** → confirm). The host will reboot.

---

## After reboot — verification commands

Run these on the **host** (SSH back in once it's up):

```bash
# 1. Confirm TSC is no longer flagged unstable
dmesg | grep -i tsc

# Healthy: no "TSC found unstable" line.
# If still flagged unstable, BIOS settings didn't fully fix it; proceed
# to the kernel fallback below.

# 2. Confirm HPET is exposed
ls /dev/hpet 2>/dev/null && echo "HPET present" || echo "HPET not exposed"
dmesg | grep -i hpet | head -5

# 3. Confirm chosen clocksource
cat /sys/devices/system/clocksource/clocksource0/current_clocksource
cat /sys/devices/system/clocksource/clocksource0/available_clocksource

# Best outcome: current_clocksource is "tsc". Acceptable: "hpet".
# Worst: only "jiffies" — something is very wrong, escalate.

# 4. Confirm IOMMU and vfio-pci still bound after BIOS changes
dmesg | grep -E 'AMD-Vi|DMAR' | head
lspci -nnk -s 05:00.3 | grep "Kernel driver"
lspci -nnk -s 05:00.4 | grep "Kernel driver"

# Both must show "Kernel driver in use: vfio-pci". If they show
# xhci_pci, the vfio early bind broke during the BIOS visit (rare but
# possible if BIOS reset clobbered IOMMU groups). Re-run
# update-initramfs -u -k all and reboot.
```

---

## Fallback — if BIOS settings did not fix unstable TSC

If `dmesg | grep -i tsc` still shows "TSC found unstable" after the BIOS
visit, force the kernel to trust TSC anyway. This is safe **only**
after testing for clock drift; chrony will catch it if TSC actually
drifts (RMS offset will grow over time).

On the **host**:

```bash
sudo cp /etc/default/grub /root/proxmox-passthrough-backup/grub.host.pre-tsc-reliable

sudoedit /etc/default/grub
# Add  tsc=reliable  to the GRUB_CMDLINE_LINUX_DEFAULT="..."  line.
# It joins the existing flags (amd_iommu=on iommu=pt isolcpus=... etc).

sudo update-grub
sudo reboot
```

After reboot:

```bash
dmesg | grep -i tsc
cat /sys/devices/system/clocksource/clocksource0/current_clocksource   # expect: tsc
```

---

## When to give up on TSC

If both BIOS settings and `tsc=reliable` fail to produce a stable TSC,
fall back to `kvm-clock` only and accept slightly higher offset
variance. chrony in the guest can usually hold sub-millisecond accuracy
on kvm-clock alone, but jitter goes up. Document the fallback choice
in `wsprdaemon-proxmox-cpu-clock-tuning.md`'s Phase 7 snapshot so
future you knows this hardware has a quirky TSC.

---

## What this document does NOT cover

- Per-vCPU pinning, isolcpus, hookscripts — those are OS-level changes,
  see `wsprdaemon-proxmox-cpu-clock-tuning.md`.
- vfio-pci binding — already done, see `wsprdaemon-proxmox-vm-setup.md`.
- chrony installation — already complete on AI6VN-1.

This doc is **only** for the BIOS visit. Once you're back in Linux,
return to the tuning runbook.
