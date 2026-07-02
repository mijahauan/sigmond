# Proxmox VM Bootstrap

Scripts that turn a fresh Debian 13 VM running under Proxmox VE into a
fully-configured Sigmond/wsprdaemon station with PCIe USB-controller
passthrough, CPU isolation, vfio binding, and the cpu-pin hookscript —
in one operator-driven flow.

## Entry point

These scripts are invoked automatically by the top-level `install.sh`
when it detects a KVM guest and the operator answers "yes" to the
"Proxmox passthrough setup?" prompt. To invoke directly:

```bash
sudo bash scripts/proxmox/bootstrap.sh
```

Re-running on a partially-configured system is safe: bootstrap reads
`/etc/sigmond/install-state.env` and resumes from the last completed
state.

## What runs where

| Script                        | Where it runs            | Purpose                                                |
|-------------------------------|--------------------------|--------------------------------------------------------|
| `bootstrap.sh`                | Inside the VM (root)     | Orchestrator — drives the whole flow.                  |
| `lib.sh`                      | Sourced by bootstrap     | State, SSH, prompt, logging helpers.                   |
| `host-discover.sh`            | scp'd to Proxmox host    | Detects VMID, USB controllers, IOMMU groups, CPU.      |
| `host-apply.sh`               | scp'd to Proxmox host    | Writes vfio/grub/qm config, hookscript, initramfs.     |
| `host-verify.sh`              | scp'd to Proxmox host    | Post-reboot check that vfio-pci took ownership.        |
| `cpu-pin-VMID.sh.template`    | Rendered into /var/lib/vz/snippets/ | Per-vCPU pin + per-pCPU freq cap hookscript.|
| `sigmond-install-resume.service` | Installed in /etc/systemd/system/ | Oneshot that resumes bootstrap after reboot.   |

## State file

`/etc/sigmond/install-state.env` (mode 0600). One state value plus all
discovered/operator-supplied parameters. Survives reboots.

## Golden image (capture / clone)

`golden-image.sh` — run ON the Proxmox host as root — captures a prepared
reference VM as a Proxmox template and instantiates per-site VMs from it:

```bash
# inside the reference VM first (strips per-site state, then shut down):
sudo smd admin capture-prep --yes && sudo shutdown -h now

# on the Proxmox host:
./golden-image.sh capture <vmid> --name dasi2-golden   # full clone -> qm template
./golden-image.sh clone <template-id> <site-name>      # per new site
```

`clone` strips the inherited hookscript + hostpci passthrough entries —
those are host-specific and re-created by `bootstrap.sh` from inside the
new VM. First boot of a clone: `smd admin personalize --reset-identity
--yes`, fill `/etc/sigmond/site-profile.toml`, `smd config render`, then
run `bootstrap.sh`. Gate everything with `smd admin readiness`.

## Prerequisites

- BIOS configured per `docs/proxmox/wsprdaemon-proxmox-bios-checklist.md`
  (operator does this once, before any of these scripts run).
- VM created in Proxmox with the desired memory and disk.
- Linux user account inside the VM with sudo access.

## Reference

- `docs/proxmox/wsprdaemon-proxmox-vm-setup.md` — manual procedure these
  scripts automate.
- `docs/proxmox/wsprdaemon-proxmox-cpu-clock-tuning.md` — CPU isolation
  and clock tuning details.
- `docs/proxmox/cpu-pin-VMID.sh.example` — original hand-written example
  that `cpu-pin-VMID.sh.template` is parameterized from.
- `tasks/plan-proxmox-vm-bootstrap.md` — design notes for this flow.
