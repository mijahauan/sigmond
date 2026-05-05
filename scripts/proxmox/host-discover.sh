#!/usr/bin/env bash
# scripts/proxmox/host-discover.sh
#
# Runs *on the Proxmox host* (scp'd to /tmp by bootstrap.sh).
# Discovers VMID, USB controller PCI IDs, IOMMU group isolation, and
# CPU layout. Emits a key=value block on stdout for the VM-side
# orchestrator to consume.
#
# Args (optional):
#   $1 = VM_PRODUCT_SERIAL  — UUID from VM's /sys/class/dmi/id/product_serial
#                              used to disambiguate when multiple VMs exist.
#
# Exit non-zero on any unrecoverable detection failure (e.g. USB controllers
# share an IOMMU group with non-USB devices).

set -euo pipefail

VM_SERIAL="${1:-}"

emit() { printf '%s=%q\n' "$1" "$2"; }
log()  { printf '# %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ─── identify the VM ──────────────────────────────────────────────────────────
mapfile -t VM_CONFS < <(ls /etc/pve/qemu-server/*.conf 2>/dev/null || true)
[[ ${#VM_CONFS[@]} -gt 0 ]] || die "no VMs found in /etc/pve/qemu-server/"

VMID=""
if [[ -n "$VM_SERIAL" ]]; then
    # Match by SMBIOS serial — Proxmox stores it as smbios1: uuid=<UUID>,...
    for conf in "${VM_CONFS[@]}"; do
        if grep -q "uuid=${VM_SERIAL}" "$conf" 2>/dev/null; then
            VMID="${conf##*/}"; VMID="${VMID%.conf}"
            break
        fi
    done
fi

if [[ -z "$VMID" ]]; then
    if [[ ${#VM_CONFS[@]} -eq 1 ]]; then
        VMID="${VM_CONFS[0]##*/}"; VMID="${VMID%.conf}"
        log "single VM found, using VMID=${VMID}"
    else
        # List candidates so VM-side can prompt operator.
        log "multiple VMs found; ambiguous"
        emit DISCOVERY_RESULT "ambiguous"
        for conf in "${VM_CONFS[@]}"; do
            id="${conf##*/}"; id="${id%.conf}"
            name="$(awk -F: '/^name:/{print $2; exit}' "$conf" | tr -d ' ')"
            printf 'VM_CANDIDATE=%s:%s\n' "$id" "${name:-unnamed}"
        done
        exit 0
    fi
fi

[[ "$VMID" =~ ^[0-9]+$ ]] || die "computed VMID is not numeric: '$VMID'"

# ─── identify USB controllers ─────────────────────────────────────────────────
# Find USB controllers (class 0c03), grouped by vendor:device id.
mapfile -t USB_LINES < <(lspci -nn | grep -i 'USB controller' || true)
[[ ${#USB_LINES[@]} -gt 0 ]] || die "no USB controllers found via lspci"

declare -A USB_BY_ID
USB_ADDRS=()
for line in "${USB_LINES[@]}"; do
    addr="${line%% *}"
    vid_did="$(grep -oE '\[[0-9a-f]{4}:[0-9a-f]{4}\]' <<<"$line" | tail -1 | tr -d '[]')"
    USB_ADDRS+=("$addr")
    USB_BY_ID[$vid_did]="${USB_BY_ID[$vid_did]:-} $addr"
done

# Pick the vendor:device ID that appears at exactly the desired count (>=2,
# and matches no non-USB device). For AMD Renoir/Cezanne this is 1022:1639.
USB_VID_DID=""
for id in "${!USB_BY_ID[@]}"; do
    count_total="$(lspci -nn | grep -c "\\[${id}\\]" || true)"
    # Count matching *USB* lines: pipe one-per-line into grep -c.
    count_usb="$(printf '%s\n' "${USB_LINES[@]}" | grep -cE "\\[${id}\\]" || true)"
    if [[ "$count_total" == "$count_usb" && "$count_usb" -ge 1 ]]; then
        USB_VID_DID="$id"
        USB_ADDRS_FOR_ID="${USB_BY_ID[$id]## }"
        break
    fi
done

[[ -n "$USB_VID_DID" ]] || die "no USB controller vendor:device ID is unique to USB controllers — manual config needed"

# ─── verify IOMMU group isolation ─────────────────────────────────────────────
IOMMU_OK=1
for addr in $USB_ADDRS_FOR_ID; do
    full_addr="0000:${addr}"
    grp_path="$(readlink -f /sys/bus/pci/devices/${full_addr}/iommu_group 2>/dev/null || true)"
    [[ -n "$grp_path" ]] || { log "no iommu_group for $full_addr — IOMMU disabled?"; IOMMU_OK=0; continue; }
    grp_id="${grp_path##*/}"
    # Every device in this group must be a USB controller (class 0c03).
    bad_in_group=()
    for dev_link in /sys/kernel/iommu_groups/${grp_id}/devices/*; do
        [[ -e "$dev_link" ]] || continue
        dev_addr="${dev_link##*/}"
        class="$(cat "/sys/bus/pci/devices/${dev_addr}/class" 2>/dev/null || echo 0x000000)"
        # 0x0c0330=USB xHCI, 0x0c0320=USB EHCI, 0x0c0300=USB UHCI
        [[ "$class" =~ ^0x0c03 ]] || bad_in_group+=("$dev_addr (class $class)")
    done
    if [[ ${#bad_in_group[@]} -gt 0 ]]; then
        log "IOMMU group $grp_id (containing $full_addr) shares with non-USB: ${bad_in_group[*]}"
        IOMMU_OK=0
    fi
done

# ─── CPU layout ───────────────────────────────────────────────────────────────
HOST_CPU_COUNT="$(nproc)"
SIBLINGS_LIST="$(cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list 2>/dev/null || echo 0)"
# Sequential pairing: "0,1" or "0-1". Split: "0,N/2".
HT_PATTERN="unknown"
if [[ "$SIBLINGS_LIST" =~ ^0[,-]1$ ]]; then
    HT_PATTERN="sequential"
elif [[ "$SIBLINGS_LIST" =~ ^0[,-]([0-9]+)$ ]]; then
    sib="${BASH_REMATCH[1]}"
    [[ "$sib" -eq $((HOST_CPU_COUNT/2)) ]] && HT_PATTERN="split"
fi

# CPU vendor (for grub iommu flag).
CPU_VENDOR="$(awk -F: '/^vendor_id/{print $2; exit}' /proc/cpuinfo | tr -d ' ')"

# ─── emit ─────────────────────────────────────────────────────────────────────
emit DISCOVERY_RESULT "ok"
emit VMID "$VMID"
emit USB_VID_DID "$USB_VID_DID"
emit USB_ADDRS_FOR_ID "${USB_ADDRS_FOR_ID## }"
emit IOMMU_OK "$IOMMU_OK"
emit HOST_CPU_COUNT "$HOST_CPU_COUNT"
emit HT_PATTERN "$HT_PATTERN"
emit CPU_VENDOR "$CPU_VENDOR"
