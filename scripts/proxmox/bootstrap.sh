#!/usr/bin/env bash
# scripts/proxmox/bootstrap.sh — Proxmox VM passthrough orchestrator.
#
# Invoked by install.sh when systemd-detect-virt=kvm and the operator
# opts in. Or directly:
#
#     sudo bash sigmond/scripts/proxmox/bootstrap.sh
#
# Re-entrant via state in /etc/sigmond/install-state.env. After the
# host reboot, a systemd oneshot (sigmond-install-resume.service)
# re-invokes this script with --resume.
#
# Phases:
#   PRE_HOST          → ssh-copy-id, host discovery + apply, reboot host
#   HOST_CONFIGURED   → after reboot, verify vfio binding
#   HOST_REBOOTED     → run sigmond install.sh inside the VM
#   SIGMOND_INSTALLED → cleanup, disable resume unit
#   DONE              → no-op

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIGMOND_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"

# ─── CLI ──────────────────────────────────────────────────────────────────────
RESUME=false
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --resume)  RESUME=true ;;
        --dry-run) DRY_RUN=true ;;
        --help|-h) sed -n '2,18p' "$0"; exit 0 ;;
        *)         die "unknown arg: $arg" ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    info "Re-executing under sudo…"
    exec sudo -E "$0" "$@"
fi

state_load

# ─── invoking-user resolution ─────────────────────────────────────────────────
# Even when re-execed under sudo, SUDO_USER tells us who the operator is.
# When systemd resumes us, neither SUDO_USER nor LOGNAME is set — fall back
# to whatever was captured in state during phase_pre_host.
if [[ -z "${INVOKING_USER:-}" ]]; then
    INVOKING_USER="${SUDO_USER:-${LOGNAME:-root}}"
fi
if [[ -z "${INVOKING_HOME:-}" ]]; then
    INVOKING_HOME="$(getent passwd "$INVOKING_USER" | cut -d: -f6)"
    [[ -n "$INVOKING_HOME" ]] || die "could not resolve home dir for $INVOKING_USER"
fi

# ─── helpers used across phases ───────────────────────────────────────────────
require_cmd() {
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || die "$c not found in PATH"
    done
}

run_as_user() {
    # Run a command as the invoking (non-root) user, even when we're root.
    local user="$1"; shift
    sudo -u "$user" -H -- "$@"
}

apt_install_quiet() {
    DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" >/dev/null
}

# ─── phase: PRE_HOST ──────────────────────────────────────────────────────────
phase_pre_host() {
    info "Phase 1: configuring Proxmox host (this will require one host-root password entry)…"

    # Sanity: required tools.
    if ! command -v ssh >/dev/null || ! command -v scp >/dev/null || ! command -v ssh-keygen >/dev/null; then
        info "installing openssh-client…"
        apt-get update -qq
        apt_install_quiet openssh-client
    fi

    # KVM check (defensive — install.sh already gated this, but if invoked directly…).
    local virt
    virt="$(systemd-detect-virt 2>/dev/null || echo none)"
    [[ "$virt" == "kvm" ]] || die "this script targets KVM/Proxmox guests; detected virt='$virt'"

    # ─── auto-clone ka9q-python if missing ────────────────────────────────────
    local kp_dir="$INVOKING_HOME/ka9q-python"
    if [[ ! -d "$kp_dir" ]]; then
        info "ka9q-python not found at $kp_dir — cloning…"
        require_cmd git
        run_as_user "$INVOKING_USER" git clone https://github.com/mijahauan/ka9q-python "$kp_dir"
        ok "ka9q-python cloned"
    else
        ok "ka9q-python present at $kp_dir"
    fi

    # ─── ensure root has an SSH keypair ───────────────────────────────────────
    local key="/root/.ssh/id_ed25519"
    if [[ ! -f "$key" ]]; then
        info "generating root SSH keypair (ed25519)…"
        mkdir -p /root/.ssh
        chmod 700 /root/.ssh
        ssh-keygen -t ed25519 -f "$key" -N "" -C "sigmond-bootstrap@$(hostname)" >/dev/null
        ok "wrote $key"
    fi

    # ─── prompt for Proxmox host ──────────────────────────────────────────────
    local host
    host="$(prompt "Proxmox host name or IP" "${PROXMOX_HOST:-}")"
    [[ -n "$host" ]] || die "Proxmox host required"

    # ─── ssh-copy-id (one-time interactive) ───────────────────────────────────
    # Test specifically with the generated root key — not whatever agent or
    # other key happens to be available — because that's what systemd-resume
    # will use after the host reboot (no agent in that context).
    if ssh -o BatchMode=yes -o ConnectTimeout=5 \
        -o StrictHostKeyChecking=accept-new \
        -i "$key" -o IdentitiesOnly=yes \
        "root@${host}" true 2>/dev/null; then
        ok "root@${host} already trusts our generated key"
    else
        info "Installing this VM's public key on root@${host}."
        info "You will be prompted ONCE for the host's root password."
        ssh-copy-id -i "${key}.pub" -o StrictHostKeyChecking=accept-new "root@${host}" </dev/tty
        ok "key installed on host"
        # Verify post-install with the specific key (no agent fallback).
        ssh -o BatchMode=yes -o ConnectTimeout=5 \
            -i "$key" -o IdentitiesOnly=yes \
            "root@${host}" true \
            || die "key install reported success but specific-key SSH still fails — check host's /root/.ssh/authorized_keys"
    fi

    PROXMOX_HOST="$host"
    PROXMOX_USER="root"
    state_set PROXMOX_HOST "$host" PROXMOX_USER "root" \
              SIGMOND_REPO "$SIGMOND_REPO" \
              INVOKING_USER "$INVOKING_USER" \
              INVOKING_HOME "$INVOKING_HOME"

    # ─── discover host config ─────────────────────────────────────────────────
    local vm_serial=""
    if [[ -r /sys/class/dmi/id/product_serial ]]; then
        vm_serial="$(tr -d ' \n' </sys/class/dmi/id/product_serial)"
    fi

    info "running discovery on host…"
    local discover_out
    discover_out="$(host_run_script "$SCRIPT_DIR/host-discover.sh" "$vm_serial")"

    # Parse k=v output into env.
    local result vmid usb_vid_did usb_addrs iommu_ok host_cpu_count ht_pattern cpu_vendor
    declare -A KV=()
    while IFS='=' read -r k v; do
        [[ -z "$k" || "$k" =~ ^# ]] && continue
        # Strip outer quotes from %q-style output.
        v="${v#\'}"; v="${v%\'}"
        v="${v#\"}"; v="${v%\"}"
        KV[$k]="$v"
    done <<<"$discover_out"

    result="${KV[DISCOVERY_RESULT]:-}"
    if [[ "$result" == "ambiguous" ]]; then
        info "Multiple VMs found on host. Pick one:"
        grep '^VM_CANDIDATE=' <<<"$discover_out" | sed 's/^VM_CANDIDATE=/  /'
        local vmid_pick
        vmid_pick="$(prompt "VMID")"
        [[ "$vmid_pick" =~ ^[0-9]+$ ]] || die "VMID must be numeric"
        # Re-run discovery scoped to this VMID — actually, we don't need to;
        # USB/CPU info is host-global. Just record VMID and re-run with it.
        discover_out="$(ssh_host "VMID=${vmid_pick} bash /tmp/host-discover.sh ''")"
        # Re-parse.
        KV=()
        while IFS='=' read -r k v; do
            [[ -z "$k" || "$k" =~ ^# ]] && continue
            v="${v#\'}"; v="${v%\'}"; v="${v#\"}"; v="${v%\"}"
            KV[$k]="$v"
        done <<<"$discover_out"
        result="${KV[DISCOVERY_RESULT]:-}"
    fi

    [[ "$result" == "ok" ]] || die "host discovery failed (result=$result). Re-run with --resume after fixing."

    vmid="${KV[VMID]}"
    usb_vid_did="${KV[USB_VID_DID]}"
    usb_addrs="${KV[USB_ADDRS_FOR_ID]}"
    iommu_ok="${KV[IOMMU_OK]}"
    host_cpu_count="${KV[HOST_CPU_COUNT]}"
    ht_pattern="${KV[HT_PATTERN]}"
    local ht_pairs="${KV[HT_PAIRS]}"
    cpu_vendor="${KV[CPU_VENDOR]}"

    [[ "$iommu_ok" == "1" ]] || die "USB controllers share IOMMU group with non-USB devices. Manual config required — see docs/proxmox/wsprdaemon-proxmox-vm-setup.md Step 2."

    # Number of LOCAL radiod instances — one host HT sibling pair each.
    # Defaults to 1; set LOCAL_RADIOD_COUNT=N for a host with multiple local
    # RX888 triplets. Remote receivers' radiods run on other hosts and are not
    # counted here.
    local radiod_count="${LOCAL_RADIOD_COUNT:-1}"

    ok "VMID=${vmid} USB=${usb_vid_did} (addrs:${usb_addrs}) host_cpus=${host_cpu_count} HT=${ht_pattern} pairs='${ht_pairs}' local_radiods=${radiod_count} CPU=${cpu_vendor}"

    # ─── compute CPU layout ───────────────────────────────────────────────────
    # Topology-aware: place each local radiod on a real host HT sibling pair
    # (so its FFT/block threads share L1/L2), pin decode workers to the
    # remaining pairs, reserve the last physical core for the Proxmox host.
    # Handles sequential ({0,1},{2,3}…) AND split ({0,8},{1,9}…) hosts. The
    # computation lives in sigmond.cpu (unit-tested); bootstrap evals the
    # shell-var block it emits (RADIOD_CPUS WORKER_CPUS VCPU_TO_PCPU
    # ISOLCPUS_RANGE VM_CORES VM_THREADS VM_VCPU_COUNT).
    [[ -n "$ht_pairs" ]] || die "host-discover did not report HT_PAIRS (stale host-discover.sh on host?)"
    local RADIOD_CPUS WORKER_CPUS VCPU_TO_PCPU ISOLCPUS_RANGE VM_CORES VM_THREADS VM_VCPU_COUNT
    local layout_vars
    layout_vars="$(PYTHONPATH="$SIGMOND_REPO/lib" python3 -c '
import sys
from sigmond.cpu import parse_ht_pairs, compute_host_cpu_layout, layout_shell_vars
pairs = parse_ht_pairs(sys.argv[1])
lay = compute_host_cpu_layout(pairs, local_radiod_count=int(sys.argv[2]))
print(layout_shell_vars(lay))
' "$ht_pairs" "$radiod_count")" \
        || die "CPU layout computation failed (HT_PAIRS='$ht_pairs' local_radiods=$radiod_count). Non-SMT/asymmetric host or too few cores — configure manually per docs/proxmox/wsprdaemon-proxmox-cpu-clock-tuning.md."
    eval "$layout_vars"

    state_set VMID "$vmid" \
              USB_VID_DID "$usb_vid_did" \
              USB_ADDRS_FOR_ID "$usb_addrs" \
              CPU_VENDOR "$cpu_vendor" \
              HOST_CPU_COUNT "$host_cpu_count" \
              HT_PATTERN "$ht_pattern" \
              LOCAL_RADIOD_COUNT "$radiod_count" \
              VM_VCPU_COUNT "$VM_VCPU_COUNT" \
              VM_CORES "$VM_CORES" \
              VM_THREADS "$VM_THREADS" \
              ISOLCPUS_RANGE "$ISOLCPUS_RANGE" \
              RADIOD_CPUS "$RADIOD_CPUS" \
              WORKER_CPUS "$WORKER_CPUS" \
              VCPU_TO_PCPU "$VCPU_TO_PCPU" \
              RADIOD_FREQ_KHZ "3200000" \
              WORKER_FREQ_KHZ "1400000"

    # ─── apply host config ────────────────────────────────────────────────────
    info "pushing host-apply.sh + cpu-pin template to host…"
    scp_to_host "$SCRIPT_DIR/host-apply.sh" "$SCRIPT_DIR/cpu-pin-VMID.sh.template"

    info "running host-apply on host (this can take ~30s for initramfs)…"
    ssh_host "VMID='$vmid' USB_VID_DID='$usb_vid_did' CPU_VENDOR='$cpu_vendor' \
              HOST_CPU_COUNT='$host_cpu_count' VM_VCPU_COUNT='$VM_VCPU_COUNT' \
              VM_CORES='$VM_CORES' VM_THREADS='$VM_THREADS' \
              ISOLCPUS_RANGE='$ISOLCPUS_RANGE' \
              RADIOD_CPUS='$RADIOD_CPUS' WORKER_CPUS='$WORKER_CPUS' \
              VCPU_TO_PCPU='$VCPU_TO_PCPU' \
              RADIOD_FREQ_KHZ='3200000' WORKER_FREQ_KHZ='1400000' \
              bash /tmp/host-apply.sh"

    ok "host configured"

    # ─── install resume unit ──────────────────────────────────────────────────
    info "installing systemd resume unit…"
    cp "$SCRIPT_DIR/sigmond-install-resume.service" "$RESUME_UNIT_PATH"
    systemctl daemon-reload
    systemctl enable "$RESUME_UNIT" >/dev/null 2>&1
    ok "resume unit enabled (will fire on next boot)"

    state_advance HOST_CONFIGURED

    # ─── reboot host ──────────────────────────────────────────────────────────
    cat <<EOF

${BOLD}Host reboot required${NC} — the Proxmox host will reboot now.
This VM will be killed when the host shuts down, and will come back up
automatically (qm onboot=1). Resume happens via systemd; you do not
need to do anything else.

Approximate timeline:
  - host reboot:       1-3 minutes
  - VM autostart:      ~30 seconds
  - resume + verify:   ~30 seconds
  - sigmond install:   1-3 minutes (longer if first time installing uv/venv)

You can watch progress in /var/log/syslog or by re-attaching to a fresh
SSH session and running:
  sudo journalctl -u sigmond-install-resume -f

EOF
    if confirm "Reboot the Proxmox host now?" "Y"; then
        info "triggering host reboot…"
        ssh_host "systemctl reboot" || true
        ok "reboot signal sent — this VM will go down momentarily"
        sleep 5
        exit 0
    else
        warn "host not rebooted. Re-run this script with --resume after rebooting manually."
        warn "  ssh root@${host} 'systemctl reboot'"
        exit 0
    fi
}

# ─── phase: HOST_CONFIGURED → verify after reboot ─────────────────────────────
phase_host_rebooted_verify() {
    info "Phase 2: verifying host vfio binding after reboot…"

    # Wait for SSH to host to come back.
    local tries=0 max_tries=60
    until ssh_host "true" 2>/dev/null; do
        tries=$((tries + 1))
        [[ $tries -lt $max_tries ]] || die "host SSH did not come back after $((max_tries*5))s"
        sleep 5
    done
    ok "host SSH reachable"

    info "running host-verify.sh on host…"
    scp_to_host "$SCRIPT_DIR/host-verify.sh"
    if ssh_host "USB_VID_DID='$USB_VID_DID' bash /tmp/host-verify.sh"; then
        ok "vfio-pci binding confirmed"
    else
        warn "host verification FAILED — vfio-pci is not the active driver for the USB controllers."
        warn "  See: docs/proxmox/wsprdaemon-proxmox-vm-setup.md → Troubleshooting → 'Host reboots when starting the VM'"
        warn "  After fixing, re-run: sudo bash $SIGMOND_REPO/scripts/proxmox/bootstrap.sh --resume"
        die "verification failed; state preserved at HOST_CONFIGURED"
    fi

    # Inside-VM verification: lsusb should show RX-888 (or whatever USB devices are plugged in).
    if command -v lsusb >/dev/null 2>&1; then
        info "USB devices visible inside VM:"
        lsusb | sed 's/^/    /' || true
    fi

    state_advance HOST_REBOOTED
    phase_sigmond_install
}

# ─── phase: HOST_REBOOTED → install sigmond inside the VM ─────────────────────
phase_sigmond_install() {
    info "Phase 3: installing Sigmond inside the VM…"

    # chrony with explicit NIST stratum-1 servers (per docs/proxmox/wsprdaemon-proxmox-cpu-clock-tuning.md).
    if ! dpkg -s chrony >/dev/null 2>&1; then
        info "installing chrony…"
        apt-get update -qq
        apt_install_quiet chrony
    fi

    local chrony_drop=/etc/chrony/conf.d/sigmond-nist.conf
    if [[ ! -f "$chrony_drop" ]]; then
        mkdir -p "$(dirname "$chrony_drop")"
        cat > "$chrony_drop" <<'EOF'
# /etc/chrony/conf.d/sigmond-nist.conf — managed by sigmond.
# Explicit NIST stratum-1 servers for SDR timestamping accuracy.
server time-a-wwv.nist.gov iburst
server time-b-wwv.nist.gov iburst
server time-c-wwv.nist.gov iburst
server time-a-g.nist.gov iburst
EOF
        systemctl restart chrony 2>/dev/null || systemctl restart chronyd 2>/dev/null || true
        ok "chrony configured with NIST stratum-1 servers"
    fi

    # Run the existing top-level install.sh — but tell it to skip the Proxmox
    # detection prompt so we don't recurse.
    info "running $SIGMOND_REPO/install.sh…"
    SIGMOND_SKIP_PROXMOX_PROMPT=1 bash "$SIGMOND_REPO/install.sh"

    state_advance SIGMOND_INSTALLED
    phase_finalize
}

# ─── phase: SIGMOND_INSTALLED → cleanup ───────────────────────────────────────
phase_finalize() {
    info "Phase 4: finalizing…"

    if [[ -f "$RESUME_UNIT_PATH" ]]; then
        systemctl disable "$RESUME_UNIT" >/dev/null 2>&1 || true
        rm -f "$RESUME_UNIT_PATH"
        systemctl daemon-reload
        ok "resume unit removed"
    fi

    state_advance DONE

    cat <<EOF

${BOLD}${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}
${BOLD}${GREEN}║  Sigmond Proxmox VM bootstrap COMPLETE                       ║${NC}
${BOLD}${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}

  Next steps:
    smd list --available           # see catalog
    smd install radiod             # install ka9q-radio
    smd install hf-timestd         # then any clients you want
    smd status                     # monitor

  State file: $SIGMOND_STATE_FILE
  Host backups: ssh root@${PROXMOX_HOST:-<host>} ls /root/proxmox-passthrough-backup/

EOF
}

# ─── dispatch ─────────────────────────────────────────────────────────────────
if ! $RESUME; then
    cat <<EOF
${BOLD}
  ╔══════════════════════════════════════════════════════════════╗
  ║  Sigmond — Proxmox VM passthrough setup                      ║
  ║  Multi-stage installer with one host reboot.                 ║
  ╚══════════════════════════════════════════════════════════════╝
${NC}
EOF
fi

case "${INSTALL_STATE:-}" in
    ""|PRE_HOST)        phase_pre_host ;;
    HOST_CONFIGURED)    phase_host_rebooted_verify ;;
    HOST_REBOOTED)      phase_sigmond_install ;;
    SIGMOND_INSTALLED)  phase_finalize ;;
    DONE)               ok "Already done (state=DONE). Nothing to do." ;;
    *)                  die "unknown INSTALL_STATE='${INSTALL_STATE}'" ;;
esac
