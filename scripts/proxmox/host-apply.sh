#!/usr/bin/env bash
# scripts/proxmox/host-apply.sh
#
# Runs *on the Proxmox host* (scp'd to /tmp by bootstrap.sh). Applies all
# host-side configuration needed for PCIe USB-controller passthrough and
# CPU isolation, idempotently.
#
# Required env vars (passed via ssh):
#   USB_VID_DID, CPU_VENDOR, ISOLCPUS_RANGE   — always
#   VMID, VM_VCPU_COUNT, VM_CORES, VM_THREADS, RADIOD_CPUS, WORKER_CPUS,
#   VCPU_TO_PCPU                              — only for VM binding
#
# Two-phase install model: with VMID EMPTY/unset, applies only the
# VM-independent host base (grub IOMMU/isolcpus flags, vfio modules,
# initramfs) — Phase 1, run by host-setup.sh BEFORE any VM exists.
# With VMID set, additionally renders the cpu-pin hookscript and binds
# the VM (qm set) — Phase 2, run by the guest bootstrap.
#
# Reads the cpu-pin template from /tmp/cpu-pin-VMID.sh.template (VM mode).
#
# Idempotent: re-runs cleanly. Backup of mutated upstream files written
# to /root/proxmox-passthrough-backup/.

set -euo pipefail

# ─── inputs ───────────────────────────────────────────────────────────────────
VMID="${VMID:-}"
: "${USB_VID_DID:?USB_VID_DID required}"
: "${CPU_VENDOR:?CPU_VENDOR required}"
: "${ISOLCPUS_RANGE:?ISOLCPUS_RANGE required}"
: "${RADIOD_FREQ_KHZ:=3200000}"
: "${WORKER_FREQ_KHZ:=1400000}"
if [[ -n "$VMID" ]]; then
    : "${VM_VCPU_COUNT:?VM_VCPU_COUNT required (VM mode)}"
    : "${VM_CORES:?VM_CORES required (VM mode)}"
    : "${VM_THREADS:?VM_THREADS required (VM mode)}"
    : "${RADIOD_CPUS:?RADIOD_CPUS required (VM mode)}"
    : "${WORKER_CPUS:?WORKER_CPUS required (VM mode)}"
    : "${VCPU_TO_PCPU:?VCPU_TO_PCPU required (VM mode)}"
    CONF="/etc/pve/qemu-server/${VMID}.conf"
    [[ -f "$CONF" ]] || { echo "ERROR: VM config $CONF does not exist" >&2; exit 1; }
fi

BACKUP_DIR="/root/proxmox-passthrough-backup"
TEMPLATE="/tmp/cpu-pin-VMID.sh.template"
SNIPPET="/var/lib/vz/snippets/cpu-pin-${VMID}.sh"

log() { printf '[host-apply] %s\n' "$*"; }
backup_once() {
    local src="$1"
    local dst="${BACKUP_DIR}/$(basename "$src").original"
    [[ -e "$dst" ]] && return 0
    [[ -e "$src" ]] || return 0
    mkdir -p "$BACKUP_DIR"
    cp -p "$src" "$dst"
    log "backup: $src → $dst"
}

# ─── /etc/default/grub.d/sigmond.cfg ──────────────────────────────────────────
backup_once /etc/default/grub
mkdir -p /etc/default/grub.d

if [[ "$CPU_VENDOR" == "AuthenticAMD" ]]; then
    IOMMU_FLAGS="amd_iommu=on iommu=pt"
elif [[ "$CPU_VENDOR" == "GenuineIntel" ]]; then
    IOMMU_FLAGS="intel_iommu=on iommu=pt"
else
    IOMMU_FLAGS="iommu=pt"
fi

cat > /etc/default/grub.d/sigmond.cfg <<EOF
# /etc/default/grub.d/sigmond.cfg — managed by sigmond. Do not edit by hand.
# Re-run \`bash sigmond/install.sh\` (or the Proxmox bootstrap) to update.
GRUB_CMDLINE_LINUX_DEFAULT="\${GRUB_CMDLINE_LINUX_DEFAULT} ${IOMMU_FLAGS} isolcpus=${ISOLCPUS_RANGE} nohz_full=${ISOLCPUS_RANGE} rcu_nocbs=${ISOLCPUS_RANGE} tsc=reliable processor.max_cstate=1"
EOF
log "wrote /etc/default/grub.d/sigmond.cfg"

# ─── /etc/modules ─────────────────────────────────────────────────────────────
backup_once /etc/modules
for mod in vfio vfio_iommu_type1 vfio_pci; do
    if ! grep -qE "^${mod}\$" /etc/modules; then
        echo "$mod" >> /etc/modules
        log "added $mod to /etc/modules"
    fi
done

# ─── /etc/modprobe.d/vfio.conf ────────────────────────────────────────────────
backup_once /etc/modprobe.d/vfio.conf
cat > /etc/modprobe.d/vfio.conf <<EOF
# /etc/modprobe.d/vfio.conf — managed by sigmond. Do not edit by hand.
# Bind USB controllers ($USB_VID_DID) to vfio-pci at boot.
# softdep ensures vfio-pci binds *before* xhci_pci tries to claim them —
# critical on AMD APUs where live detach causes a host reboot.
options vfio-pci ids=${USB_VID_DID} disable_vga=1
softdep xhci_pci pre: vfio-pci
EOF
log "wrote /etc/modprobe.d/vfio.conf"

# ─── rebuild grub + initramfs ─────────────────────────────────────────────────
log "running update-grub…"
update-grub >/dev/null
log "running update-initramfs -u -k all (this can take a minute)…"
update-initramfs -u -k all >/dev/null

# ─── VM-independent base done — report whether a reboot is still needed ──────
# The guest bootstrap / host-setup use this to skip a redundant reboot when
# the base config (isolcpus cmdline + vfio-pci binding) is already active.
REBOOT_REQUIRED=1
if grep -qw "isolcpus=${ISOLCPUS_RANGE}" /proc/cmdline; then
    first_addr="$(lspci -nn | grep "\[${USB_VID_DID}\]" | awk '{print $1; exit}')"
    if [[ -n "$first_addr" ]] && \
       [[ "$(basename "$(readlink -f /sys/bus/pci/devices/0000:${first_addr}/driver 2>/dev/null || true)")" == "vfio-pci" ]]; then
        REBOOT_REQUIRED=0
    fi
fi
echo "REBOOT_REQUIRED=${REBOOT_REQUIRED}"

if [[ -z "$VMID" ]]; then
    echo "host-apply: base complete (no VM bound — phase 1)"
    exit 0
fi

# ─── cpu-pin hookscript ───────────────────────────────────────────────────────
[[ -f "$TEMPLATE" ]] || { echo "ERROR: $TEMPLATE not found (scp it before running this)" >&2; exit 1; }

mkdir -p /var/lib/vz/snippets

# Render the hookscript by parameter substitution. The template uses sentinel
# placeholders: @@VMID@@, @@RADIOD_CPUS@@, @@WORKER_CPUS@@, @@VCPU_TO_PCPU@@,
# @@RADIOD_FREQ_KHZ@@, @@WORKER_FREQ_KHZ@@.
sed \
    -e "s|@@VMID@@|${VMID}|g" \
    -e "s|@@RADIOD_CPUS@@|${RADIOD_CPUS}|g" \
    -e "s|@@WORKER_CPUS@@|${WORKER_CPUS}|g" \
    -e "s|@@VCPU_TO_PCPU@@|${VCPU_TO_PCPU}|g" \
    -e "s|@@RADIOD_FREQ_KHZ@@|${RADIOD_FREQ_KHZ}|g" \
    -e "s|@@WORKER_FREQ_KHZ@@|${WORKER_FREQ_KHZ}|g" \
    "$TEMPLATE" > "$SNIPPET"
chmod +x "$SNIPPET"
log "wrote $SNIPPET"

# ─── qm config ────────────────────────────────────────────────────────────────
backup_once "$CONF"

# Discover existing USB device passthrough lines to remove.
mapfile -t USB_LINES < <(grep -E '^usb[0-9]+:' "$CONF" | sed -E 's/:.*//' || true)
if [[ ${#USB_LINES[@]} -gt 0 ]]; then
    log "removing existing USB device passthrough lines: ${USB_LINES[*]}"
    qm set "$VMID" --delete "$(IFS=,; echo "${USB_LINES[*]}")"
fi

# Reset existing hostpci lines so we can re-apply cleanly.
mapfile -t PCI_LINES < <(grep -E '^hostpci[0-9]+:' "$CONF" | sed -E 's/:.*//' || true)
if [[ ${#PCI_LINES[@]} -gt 0 ]]; then
    log "clearing existing hostpci lines: ${PCI_LINES[*]}"
    qm set "$VMID" --delete "$(IFS=,; echo "${PCI_LINES[*]}")"
fi

# Find USB controller PCI addresses (host-side, sysfs).
mapfile -t USB_ADDRS < <(lspci -nn | grep "\[${USB_VID_DID}\]" | awk '{print $1}')
[[ ${#USB_ADDRS[@]} -gt 0 ]] || { echo "ERROR: no PCI devices match $USB_VID_DID" >&2; exit 1; }

i=0
for addr in "${USB_ADDRS[@]}"; do
    qm set "$VMID" "-hostpci${i}" "0000:${addr},pcie=1"
    log "set hostpci${i} = 0000:${addr}"
    i=$((i+1))
done

qm set "$VMID" --machine q35
qm set "$VMID" --cpu host
qm set "$VMID" --boot order=scsi0
qm set "$VMID" --onboot 1
qm set "$VMID" --hookscript "local:snippets/cpu-pin-${VMID}.sh"
qm set "$VMID" --affinity "$ISOLCPUS_RANGE"
qm set "$VMID" --cores "$VM_CORES" --sockets 1
qm set "$VMID" --args "-smp ${VM_VCPU_COUNT},sockets=1,cores=${VM_CORES},threads=${VM_THREADS},maxcpus=${VM_VCPU_COUNT} -cpu host,topoext=on"
log "qm set complete"

# Snapshot the working configuration alongside the originals.
cp -p "$CONF" "${BACKUP_DIR}/${VMID}.conf.applied"
date -Iseconds > "${BACKUP_DIR}/applied-on.txt"

echo "host-apply: complete"
