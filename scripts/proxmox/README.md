# Proxmox host + VM setup

The full DASI2 install is **two phases**:

1. **Host (standalone, before any VM)** — `host-setup.sh`, run as root ON
   a fresh Proxmox host from a sigmond checkout.  Installs the host RAC
   (frpc — the site is remotely supportable from this point on, even with
   no VM), applies the CPU/passthrough base tuning (grub IOMMU +
   isolcpus/nohz_full flags, vfio modules, initramfs), saves the computed
   CPU layout to `/etc/sigmond/host-layout.env`, and reboots once.

   ```bash
   git clone https://github.com/HamSCI/sigmond
   sudo bash sigmond/scripts/proxmox/host-setup.sh [--radiod-count N]
   ```

2. **VM (instantiate + tune)** — clone the golden VM
   (`golden-image.sh clone`), boot it, personalize + site-profile, then
   `bootstrap.sh` inside it binds THIS host's passthrough + CPU pinning
   to the VM (hookscript, `qm set`).  When phase 1 already rebooted the
   host with the base config active, bootstrap detects it
   (`REBOOT_REQUIRED=0`) and skips the disruptive host reboot — only a
   one-time VM power-cycle is needed to bind the passthrough.

`bootstrap.sh` also still works standalone on a legacy single-phase
install (fresh Debian VM, host never prepared): it applies the host base
itself and reboots the host mid-flow, resuming automatically.

## Entry points

Phase 1: `sudo bash scripts/proxmox/host-setup.sh` (on the host).
Phase 2: invoked automatically by the top-level `install.sh` when it
detects a KVM guest and the operator answers "yes" to the "Proxmox
passthrough setup?" prompt — or directly:

```bash
sudo bash scripts/proxmox/bootstrap.sh
```

Re-running on a partially-configured system is safe: bootstrap reads
`/etc/sigmond/install-state.env` and resumes from the last completed
state.

## What runs where

| Script                        | Where it runs            | Purpose                                                |
|-------------------------------|--------------------------|--------------------------------------------------------|
| `host-setup.sh`               | ON the Proxmox host (root) | Phase 1 orchestrator: host RAC + base tuning + reboot. |
| `golden-image.sh`             | ON the Proxmox host (root) | Capture the golden template / clone per-site VMs.     |
| `bootstrap.sh`                | Inside the VM (root)     | Phase 2 orchestrator — binds passthrough/pinning to the VM. |
| `lib.sh`                      | Sourced by bootstrap     | State, SSH, prompt, logging helpers.                   |
| `host-discover.sh`            | scp'd to host (or run locally by host-setup, `--no-vm`) | Detects VMID, USB controllers, IOMMU groups, CPU. |
| `host-apply.sh`               | scp'd to host (or run locally by host-setup) | Base: vfio/grub/initramfs. With VMID: hookscript + qm config. |
| `host-verify.sh`              | scp'd to Proxmox host    | Post-reboot check that vfio-pci took ownership.        |
| `cpu-pin-VMID.sh.template`    | Rendered into /var/lib/vz/snippets/ | Per-vCPU pin + per-pCPU freq pin (min==max) hookscript.|
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
