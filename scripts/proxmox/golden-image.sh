#!/usr/bin/env bash
# golden-image.sh — capture / instantiate the DASI2 golden VM on a
# Proxmox host.  Runs ON the Proxmox host as root (unlike bootstrap.sh,
# which runs inside the guest).
#
#   capture <vmid> [--name NAME] [--template-id ID]
#       Clone the (STOPPED, capture-prep'd) reference VM to a new VM
#       and convert that clone to a Proxmox template.  The source VM is
#       left untouched.  Refuses a running source; refuses a source
#       whose disk was not prepared (best-effort marker check is the
#       guest's job — run `smd admin capture-prep --yes` + shutdown
#       inside the VM first).
#
#   clone <template-id> <site-name> [--vmid ID] [--full]
#       Instantiate a new site VM from the template.  Strips the
#       host-specific bits the template may carry (hookscript,
#       hostpci passthrough entries) — those are re-created per host
#       by sigmond's proxmox bootstrap after first boot.
#
# After clone: boot the VM, then inside it run
#     sudo smd admin personalize --reset-identity --yes
#     sudo $EDITOR /etc/sigmond/site-profile.toml && sudo smd config render
#     sudo bash scripts/proxmox/bootstrap.sh        # passthrough + CPU pinning
#
# See docs/PROVISIONING-INPUTS.md §9 and scripts/proxmox/README.md.
set -euo pipefail

err()  { echo -e "\033[31m[ERR ]\033[0m $*" >&2; }
info() { echo -e "\033[32m[INFO]\033[0m $*"; }
warn() { echo -e "\033[33m[WARN]\033[0m $*"; }

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

need_qm() {
    command -v qm >/dev/null || { err "qm not found — run on the Proxmox host"; exit 1; }
    [[ $EUID -eq 0 ]] || { err "run as root on the Proxmox host"; exit 1; }
}

next_id() {
    if command -v pvesh >/dev/null; then
        pvesh get /cluster/nextid 2>/dev/null && return
    fi
    # fallback: max existing + 1
    local max
    max=$(qm list | awk 'NR>1 {print $1}' | sort -n | tail -1)
    echo $(( ${max:-99} + 1 ))
}

vm_status() { qm status "$1" 2>/dev/null | awk '{print $2}'; }

cmd_capture() {
    local vmid="" name="dasi2-golden" tid=""
    vmid="${1:-}"; shift || true
    [[ -n "$vmid" ]] || usage
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --name)        name="$2"; shift 2 ;;
            --template-id) tid="$2"; shift 2 ;;
            *) err "unknown arg: $1"; usage ;;
        esac
    done
    need_qm

    local status
    status="$(vm_status "$vmid")"
    [[ -n "$status" ]] || { err "VM $vmid not found"; exit 1; }
    if [[ "$status" != "stopped" ]]; then
        err "VM $vmid is '$status' — run \`smd admin capture-prep --yes\` inside it,"
        err "shut it down, then re-run capture."
        exit 1
    fi

    tid="${tid:-$(next_id)}"
    # Stamp the capture date into the template name for image versioning.
    local stamp
    stamp="$(date +%Y%m%d)"
    local full_name="${name}-${stamp}"

    info "cloning VM $vmid -> $tid (full clone: '$full_name')"
    qm clone "$vmid" "$tid" --name "$full_name" --full
    info "converting $tid to a template"
    qm template "$tid"
    info "template ready: $tid ($full_name)"
    info "instantiate a site with:  $0 clone $tid <site-name>"
}

cmd_clone() {
    local tid="" site="" newid="" full=0
    tid="${1:-}"; site="${2:-}"; shift 2 || true
    [[ -n "$tid" && -n "$site" ]] || usage
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --vmid) newid="$2"; shift 2 ;;
            --full) full=1; shift ;;
            *) err "unknown arg: $1"; usage ;;
        esac
    done
    need_qm

    [[ -n "$(vm_status "$tid")" ]] || { err "template $tid not found"; exit 1; }
    newid="${newid:-$(next_id)}"

    local args=(qm clone "$tid" "$newid" --name "$site")
    # Linked clones are fast + thin but tie the VM to the template's
    # storage; --full makes an independent copy (recommended for
    # deployment to a different host after backup/restore).
    [[ $full -eq 1 ]] && args+=(--full)
    info "cloning template $tid -> VM $newid ('$site')"
    "${args[@]}"

    # Strip host-specific config the template may carry — the sigmond
    # proxmox bootstrap re-creates these for THIS host's topology.
    if qm config "$newid" | grep -q '^hookscript:'; then
        info "removing inherited hookscript (bootstrap re-renders per host)"
        qm set "$newid" --delete hookscript
    fi
    local pci
    for pci in $(qm config "$newid" | grep -oE '^hostpci[0-9]+' || true); do
        info "removing inherited $pci passthrough (bootstrap re-adds per host)"
        qm set "$newid" --delete "$pci"
    done

    info "VM $newid ready.  Next:"
    info "  1. qm start $newid   (console: qm terminal $newid)"
    info "  2. inside the VM:  sudo smd admin personalize --reset-identity --yes"
    info "  3. fill /etc/sigmond/site-profile.toml + sudo smd config render"
    info "  4. sudo bash scripts/proxmox/bootstrap.sh   (USB passthrough + CPU pinning)"
    info "  5. sudo smd admin readiness    (site gate must be READY when configured)"
}

case "${1:-}" in
    capture) shift; cmd_capture "$@" ;;
    clone)   shift; cmd_clone "$@" ;;
    *) usage ;;
esac
