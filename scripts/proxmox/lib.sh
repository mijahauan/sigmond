#!/usr/bin/env bash
# scripts/proxmox/lib.sh — shared helpers for Proxmox VM bootstrap.
# Sourced by bootstrap.sh; not run directly.

# ─── state file ───────────────────────────────────────────────────────────────
SIGMOND_STATE_FILE="${SIGMOND_STATE_FILE:-/etc/sigmond/install-state.env}"
SIGMOND_STATE_DIR="$(dirname "$SIGMOND_STATE_FILE")"
RESUME_UNIT="sigmond-install-resume.service"
RESUME_UNIT_PATH="/etc/systemd/system/${RESUME_UNIT}"

# ─── terminal helpers ─────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    : "${RED:=$(tput setaf 1 2>/dev/null || true)}"
    : "${GREEN:=$(tput setaf 2 2>/dev/null || true)}"
    : "${YELLOW:=$(tput setaf 3 2>/dev/null || true)}"
    : "${CYAN:=$(tput setaf 6 2>/dev/null || true)}"
    : "${BOLD:=$(tput bold 2>/dev/null || true)}"
    : "${NC:=$(tput sgr0 2>/dev/null || true)}"
else
    RED=; GREEN=; YELLOW=; CYAN=; BOLD=; NC=
fi

info() { printf '%s[proxmox]%s %s\n' "$CYAN" "$NC" "$*"; }
ok()   { printf '%s[  ok  ]%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '%s[ warn ]%s %s\n' "$YELLOW" "$NC" "$*"; }
die()  { printf '%s[error ]%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

# ─── state file accessors ─────────────────────────────────────────────────────
state_load() {
    [[ -f "$SIGMOND_STATE_FILE" ]] || return 0
    set -a
    # shellcheck disable=SC1090
    . "$SIGMOND_STATE_FILE"
    set +a
}

state_set() {
    [[ $# -ge 2 && $(($# % 2)) -eq 0 ]] || die "state_set: needs KEY VALUE pairs"
    mkdir -p "$SIGMOND_STATE_DIR"
    chmod 755 "$SIGMOND_STATE_DIR"
    touch "$SIGMOND_STATE_FILE"
    chmod 600 "$SIGMOND_STATE_FILE"
    while [[ $# -ge 2 ]]; do
        local key="$1" val="$2"; shift 2
        sed -i "/^${key}=/d" "$SIGMOND_STATE_FILE"
        # Single-quote, with internal "'" rewritten as "'\''".
        # Compatible with both bash `.` sourcing and systemd EnvironmentFile.
        local esc="${val//\'/\'\\\'\'}"
        printf "%s='%s'\n" "$key" "$esc" >> "$SIGMOND_STATE_FILE"
    done
}

state_advance() {
    local new_state="$1"
    state_set INSTALL_STATE "$new_state" LAST_UPDATED "$(date -Iseconds)"
    info "state → ${new_state}"
}

# ─── SSH to Proxmox host ──────────────────────────────────────────────────────
# Always pin to /root/.ssh/id_ed25519 with IdentitiesOnly=yes so behavior is
# identical between interactive sudo runs (where SSH_AUTH_SOCK may leak in
# via sudo -E) and systemd-resume runs (where it won't).
SIGMOND_SSH_KEY="${SIGMOND_SSH_KEY:-/root/.ssh/id_ed25519}"

ssh_host() {
    : "${PROXMOX_HOST:?PROXMOX_HOST not set}"
    ssh -o BatchMode=yes -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new \
        -i "$SIGMOND_SSH_KEY" -o IdentitiesOnly=yes \
        "${PROXMOX_USER:-root}@${PROXMOX_HOST}" "$@"
}

scp_to_host() {
    : "${PROXMOX_HOST:?PROXMOX_HOST not set}"
    scp -q -o BatchMode=yes -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new \
        -i "$SIGMOND_SSH_KEY" -o IdentitiesOnly=yes \
        "$@" "${PROXMOX_USER:-root}@${PROXMOX_HOST}:/tmp/"
}

# Push a script to /tmp on the host and run it with args.
# Captures stdout; stderr is passed through.
host_run_script() {
    local local_path="$1"; shift
    local fname; fname="$(basename "$local_path")"
    scp_to_host "$local_path"
    ssh_host "chmod +x /tmp/$fname && /tmp/$fname $*"
}

# ─── prompts ──────────────────────────────────────────────────────────────────
# Read one line from /dev/tty (works even when stdin is the script itself).
prompt() {
    local q="$1" default="${2:-}" reply
    if [[ -n "$default" ]]; then
        printf '%s[?]%s %s [%s]: ' "$YELLOW" "$NC" "$q" "$default" >/dev/tty
    else
        printf '%s[?]%s %s: ' "$YELLOW" "$NC" "$q" >/dev/tty
    fi
    read -r reply </dev/tty || reply=""
    [[ -z "$reply" ]] && reply="$default"
    echo "$reply"
}

confirm() {
    local q="$1" default="${2:-N}" reply
    reply="$(prompt "$q [y/N]" "$default")"
    [[ "$reply" =~ ^[Yy] ]]
}
