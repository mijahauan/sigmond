#!/usr/bin/env bash
# install.sh — Bootstrap Sigmond (Dr. SigMonD) on any Linux host.
#
# Recommended install:
#
#   sudo mkdir -p /opt/git/sigmond
#   sudo chown $USER /opt/git/sigmond
#   git clone https://github.com/mijahauan/sigmond /opt/git/sigmond/sigmond
#   cd /opt/git/sigmond/sigmond
#   ./install.sh
#
# Sigmond installs at /opt/git/sigmond/sigmond/, peer to its managed
# components (hf-timestd, ka9q-python, wsprdaemon-client, etc., all of
# which live at /opt/git/sigmond/<name>/).  This script will refuse to
# run from any other location.
#
# What this script does:
#   1. Validates the canonical install path
#   2. Creates the `sigmond` system user + group (owns /opt/git/sigmond)
#   3. Verifies sudo access
#   4. Installs git and Python 3.11+ if missing
#   5. Creates FHS directories (/etc/sigmond, /var/lib/sigmond, etc.)
#   6. Sets ownership of /opt/git/sigmond to sigmond:sigmond + setgid on
#      directories so future writes inherit the group
#   7. Adds the invoking user to the sigmond group (so they can edit
#      /opt/git/sigmond/* as themselves)
#   8. Writes a default /etc/sigmond/topology.toml (all components off)
#   9. Copies /etc/sigmond/catalog.toml from the repo
#  10. Builds /opt/sigmond/venv with sigmond[tui] (Textual + Rich)
#  11. Symlinks bin/smd into /usr/local/sbin/smd
#
# After this script completes, run:
#   sudo smd install               — CLI: install all catalog components
#   sudo smd install wspr-recorder — CLI: install one component
#   sudo smd tui                   — TUI: browse and install components
#
# Note: the sigmond group membership applies to sessions started AFTER
# install.sh.  Open a new shell (or `newgrp sigmond`) before editing
# files in /opt/git/sigmond as yourself.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CANONICAL_REPO="/opt/git/sigmond/sigmond"
SMD_BIN="$REPO_DIR/bin/smd"
VENV_DIR="/opt/sigmond/venv"
INSTALL_SMD="/usr/local/sbin/smd"

# ─── terminal helpers ────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info() { echo -e "${CYAN}[sigmond]${NC} $*"; }
ok()   { echo -e "${GREEN}[  ok  ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ warn ]${NC} $*"; }
die()  { echo -e "${RED}[error ]${NC} $*" >&2; exit 1; }

# ─── Proxmox VM detection (auto-skip when not applicable) ────────────────────
# Running in a KVM guest with no prior install state? Offer to run the
# Proxmox host passthrough bootstrap first. Bare-metal hosts (virt=none)
# and resume runs (state file present, or env var set, or no TTY) skip
# this entirely — existing flow is untouched.
_virt="$(systemd-detect-virt 2>/dev/null || echo none)"
_state_file="/etc/sigmond/install-state.env"
if [[ "$_virt" == "kvm" \
      && -z "${SIGMOND_SKIP_PROXMOX_PROMPT:-}" \
      && ! -f "$_state_file" \
      && -e /dev/tty \
      && -x "$REPO_DIR/scripts/proxmox/bootstrap.sh" ]]; then
    info "Detected KVM guest. Sigmond can configure the Proxmox host's PCIe USB"
    info "passthrough, CPU isolation, and vfio binding — required for full"
    info "bare-metal SDR performance with RX-888 or similar."
    printf '%b[?]%b Run Proxmox passthrough setup first? [y/N]: ' "$YELLOW" "$NC" >/dev/tty
    read -r _resp </dev/tty || _resp=""
    if [[ "$_resp" =~ ^[Yy] ]]; then
        info "handing off to scripts/proxmox/bootstrap.sh…"
        exec bash "$REPO_DIR/scripts/proxmox/bootstrap.sh"
    fi
    info "skipping Proxmox setup — proceeding with bare-metal install."
fi
unset _virt _state_file _resp

echo -e "${BOLD}"
echo "  ┌─────────────────────────────────────────────┐"
echo "  │  Dr. SigMonD — HamSCI SDR suite manager     │"
echo "  │  'Zo... ven did your signals first propagate?│"
echo "  └─────────────────────────────────────────────┘"
echo -e "${NC}"

# ─── sudo ─────────────────────────────────────────────────────────────────────
if [[ $EUID -eq 0 ]]; then
    SUDO=""
else
    info "Checking sudo access…"
    if ! sudo -n true 2>/dev/null; then
        info "Sigmond needs sudo to write system files (/etc, /opt, /usr/local/sbin)."
        sudo -v || die "sudo access required — ask your sysadmin or run as root."
    fi
    SUDO="sudo"
fi

# ─── canonical-path enforcement ──────────────────────────────────────────────
# Sigmond's source-of-truth lives at /opt/git/sigmond/sigmond/, peer to
# the components it manages.  Refuse to install from any other location;
# the install would otherwise leave Pattern A symlinks pointing at a
# non-canonical clone (that's the bug we keep fixing manually).
if [[ "$REPO_DIR" != "$CANONICAL_REPO" ]]; then
    die "install.sh must be run from $CANONICAL_REPO (got: $REPO_DIR)
       To fix:
         sudo mkdir -p /opt/git/sigmond
         sudo chown \$USER /opt/git/sigmond
         git clone https://github.com/mijahauan/sigmond $CANONICAL_REPO
         cd $CANONICAL_REPO
         ./install.sh"
fi

# ─── sigmond user + group ────────────────────────────────────────────────────
# A single non-human user `sigmond` owns /opt/git/sigmond/*.  Humans (Rob,
# Michael, anyone collaborating) become members of the `sigmond` group and
# edit as themselves, with setgid keeping group ownership consistent.
if ! getent passwd sigmond >/dev/null 2>&1; then
    info "Creating system user/group: sigmond"
    $SUDO useradd --system --user-group --home-dir /opt/git/sigmond \
                  --shell /usr/sbin/nologin sigmond
fi
ok "sigmond user/group ready: $(getent passwd sigmond | cut -d: -f1,3,4,7)"

# Add the invoking user to the sigmond group so they can edit
# /opt/git/sigmond/* as themselves.  $SUDO_USER is set when running via
# sudo; falls back to $USER for direct-as-root invocations.
INVOKER="${SUDO_USER:-${USER:-}}"
if [[ -n "$INVOKER" && "$INVOKER" != "root" ]]; then
    if ! id -nG "$INVOKER" 2>/dev/null | tr ' ' '\n' | grep -qx sigmond; then
        info "Adding $INVOKER to sigmond group"
        $SUDO usermod -aG sigmond "$INVOKER"
        warn "$INVOKER must log out and back in (or 'newgrp sigmond') for group membership to take effect"
    else
        ok "$INVOKER is already in the sigmond group"
    fi
fi

# ─── /opt/git/sigmond/ ownership + setgid ───────────────────────────────────
# Make /opt/git/sigmond/* a group-shared tree:  files are sigmond:sigmond,
# group has read+write, directories have setgid so newly-created files
# inherit the sigmond group automatically.
info "Setting /opt/git/sigmond ownership: sigmond:sigmond + setgid"
$SUDO chown -R sigmond:sigmond /opt/git/sigmond
$SUDO chmod -R g+rwX /opt/git/sigmond
$SUDO find /opt/git/sigmond -type d -exec chmod g+s {} +
ok "/opt/git/sigmond ownership and permissions set"

# ─── git safe.directory for /opt/git/sigmond/* ──────────────────────────────
# When sigmond's UID doesn't match the human's UID (the common case — sigmond
# is a system user, humans are uid 1000+), git refuses to operate with a
# "dubious ownership" error.  System-wide safe.directory entries scoped to
# /opt/git/sigmond/* let any user in the sigmond group use git there without
# per-user config.  We enumerate (rather than use `*`) so the trust scope is
# bounded.
info "Adding system-wide git safe.directory entries for /opt/git/sigmond/*"
for _repo_dir in /opt/git/sigmond/*/; do
    _repo_dir="${_repo_dir%/}"  # strip trailing slash
    if ! $SUDO git config --system --get-all safe.directory 2>/dev/null \
            | grep -Fxq "$_repo_dir"; then
        $SUDO git config --system --add safe.directory "$_repo_dir"
    fi
done
ok "git safe.directory entries set"

# ─── package manager detection ────────────────────────────────────────────────
_PKG_MGR=""
if   command -v apt-get &>/dev/null; then _PKG_MGR="apt"
elif command -v dnf     &>/dev/null; then _PKG_MGR="dnf"
elif command -v yum     &>/dev/null; then _PKG_MGR="yum"
elif command -v pacman  &>/dev/null; then _PKG_MGR="pacman"
fi

_pkg_install() {
    case "$_PKG_MGR" in
        apt)    $SUDO apt-get install -y "$@" ;;
        dnf)    $SUDO dnf install -y "$@" ;;
        yum)    $SUDO yum install -y "$@" ;;
        pacman) $SUDO pacman -S --noconfirm "$@" ;;
        *)      die "Cannot install $* — no known package manager found.  Install manually and re-run." ;;
    esac
}

# ─── git ──────────────────────────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    info "Installing git…"
    _pkg_install git
fi
ok "git: $(git --version)"

# ─── Python 3.11+ ─────────────────────────────────────────────────────────────
PYTHON3=""
for _py in python3.13 python3.12 python3.11 python3; do
    if command -v "$_py" &>/dev/null; then
        if "$_py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PYTHON3="$_py"
            break
        fi
    fi
done

if [[ -z "$PYTHON3" ]]; then
    info "Python 3.11+ not found — installing…"
    case "$_PKG_MGR" in
        apt)
            $SUDO apt-get update -qq
            _pkg_install python3.11 python3.11-venv
            PYTHON3="python3.11"
            ;;
        dnf)
            _pkg_install python3.11
            PYTHON3="python3.11"
            ;;
        *)
            die "Python 3.11+ is required.  Install it and re-run this script."
            ;;
    esac
fi

# Ensure the venv module is present (Debian/Ubuntu split it into a sub-package).
if ! "$PYTHON3" -m venv --help &>/dev/null 2>&1; then
    info "Installing python3-venv…"
    _pyver=$("$PYTHON3" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    case "$_PKG_MGR" in
        apt) _pkg_install "python${_pyver}-venv" ;;
        dnf) _pkg_install python3-venv ;;
        *)   die "python venv module missing — install it for your Python version." ;;
    esac
fi
ok "Python: $($PYTHON3 --version)"

# ─── FHS system directories ───────────────────────────────────────────────────
info "Creating system directories…"
$SUDO mkdir -p \
    /etc/sigmond \
    /var/lib/sigmond \
    /var/log/sigmond \
    /opt/sigmond
$SUDO chmod 755 /etc/sigmond /opt/sigmond
$SUDO chmod 750 /var/lib/sigmond /var/log/sigmond
ok "System directories ready"

# ─── catalog.toml ─────────────────────────────────────────────────────────────
# Always refresh from repo — catalog is Sigmond-managed and not user-edited.
info "Installing catalog → /etc/sigmond/catalog.toml"
$SUDO cp "$REPO_DIR/etc/catalog.toml" /etc/sigmond/catalog.toml
ok "catalog.toml installed"

# ─── default topology.toml ────────────────────────────────────────────────────
if [[ ! -f /etc/sigmond/topology.toml ]]; then
    info "Writing default topology → /etc/sigmond/topology.toml"
    $SUDO tee /etc/sigmond/topology.toml >/dev/null <<'TOML'
# /etc/sigmond/topology.toml — which components are enabled on this host.
#
# All components start disabled.  Use  sudo smd tui  (Install screen)
# or  sudo smd install <name>  to enable and install them.

[component.radiod]
enabled = false
managed = true

[component.hf-timestd]
enabled = false

[component.psk-recorder]
enabled = false

[component.wspr-recorder]
enabled = false

[component.wsprdaemon-client]
enabled = false
TOML
    ok "topology.toml installed (all components off by default)"
else
    ok "topology.toml already present — not overwritten"
fi

# ─── uv (fast package manager) ───────────────────────────────────────────────
UV=""
if command -v uv &>/dev/null; then
    UV="$(command -v uv)"
    ok "uv $(uv --version) found"
else
    info "Installing uv…"
    # Official uv installer: single static binary, no pip required.
    # UV_INSTALL_DIR=/usr/local/bin puts it system-wide; --no-modify-path
    # skips shell-profile edits since we know the directory is already in PATH.
    _uv_installer=$(mktemp /tmp/uv-install-XXXXXX.sh)
    _downloaded=false
    if command -v curl &>/dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh -o "$_uv_installer" 2>/dev/null \
            && _downloaded=true
    elif command -v wget &>/dev/null; then
        wget -qO "$_uv_installer" https://astral.sh/uv/install.sh 2>/dev/null \
            && _downloaded=true
    fi

    if $_downloaded; then
        # sudo sh -c "VAR=val sh script" avoids sudoers env_reset stripping our vars.
        # UV_NO_MODIFY_PATH=1 skips writing to ~/.bashrc; /usr/local/bin is already in PATH.
        $SUDO sh -c "UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh '$_uv_installer'"
    fi
    rm -f "$_uv_installer"

    if command -v uv &>/dev/null; then
        UV="$(command -v uv)"
        ok "uv installed: $(uv --version)"
    else
        warn "uv install failed — falling back to pip (slower first install)"
    fi
fi

# Helpers that use uv when available, plain pip/venv otherwise.
_venv_create() {
    local target="$1"
    if [[ -n "$UV" ]]; then
        $SUDO "$UV" venv --python "$PYTHON3" --clear "$target"
    else
        $SUDO "$PYTHON3" -m venv --clear "$target"
        $SUDO "$target/bin/pip" install --quiet --upgrade pip
    fi
}
_pip_install() {
    local target="$1"; shift
    if [[ -n "$UV" ]]; then
        $SUDO "$UV" pip install --quiet --python "$target/bin/python" "$@"
    else
        $SUDO "$target/bin/pip" install --quiet "$@"
    fi
}

# ─── sigmond TUI venv ─────────────────────────────────────────────────────────
info "Building sigmond venv at $VENV_DIR…"
_venv_create "$VENV_DIR"

info "Installing sigmond[tui] (textual, rich)…"
_pip_install "$VENV_DIR" -e "$REPO_DIR[tui]"

# Make venv world-readable/executable so any user can re-exec into it.
$SUDO chmod -R a+rX "$VENV_DIR"
ok "Venv ready at $VENV_DIR"

# ─── optional: ka9q-python editable install ───────────────────────────────────
for _ka9q in "$REPO_DIR/../ka9q-python" /opt/git/ka9q-python /home/mjh/git/ka9q-python; do
    if [[ -f "$_ka9q/pyproject.toml" ]]; then
        info "Installing ka9q-python from $_ka9q…"
        _pip_install "$VENV_DIR" -e "$_ka9q"
        ok "ka9q-python installed (editable)"
        break
    fi
done

# ─── smd symlink ──────────────────────────────────────────────────────────────
info "Installing smd → $INSTALL_SMD"
chmod a+x "$SMD_BIN"
$SUDO ln -sf "$SMD_BIN" "$INSTALL_SMD"
ok "smd installed at $INSTALL_SMD"

# ─── PATH reminder ────────────────────────────────────────────────────────────
if ! command -v smd &>/dev/null; then
    warn "/usr/local/sbin is not in your PATH."
    warn "Add this to ~/.bashrc or ~/.profile:"
    warn "  export PATH=\"\$PATH:/usr/local/sbin\""
    warn "Or use the full path:  $INSTALL_SMD"
fi

# ─── done ─────────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}${GREEN}╔═══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║   Sigmond is installed!  Next steps:                  ║${NC}"
echo -e "${BOLD}${GREEN}╚═══════════════════════════════════════════════════════╝${NC}"
echo
echo -e "  ${BOLD}Option 1 — interactive TUI (recommended):${NC}"
echo -e "    sudo smd tui"
echo -e "    Then use the Install screen to browse and install components."
echo
echo -e "  ${BOLD}Option 2 — install a specific component:${NC}"
echo -e "    sudo smd install radiod"
echo -e "    sudo smd install wspr-recorder"
echo -e "    sudo smd install psk-recorder"
echo -e "    sudo smd install hf-timestd"
echo -e "    sudo smd install wsprdaemon-client"
echo
echo -e "  ${BOLD}Option 3 — install all catalog components:${NC}"
echo -e "    sudo smd install"
echo
echo -e "  Available components:"
echo -e "    radiod             — ka9q-radio SDR daemon (server)"
echo -e "    wspr-recorder      — WSPR/FST4W audio recorder"
echo -e "    psk-recorder       — FT4/FT8 spot recorder"
echo -e "    hf-timestd         — HF time-standard analyzer (WWV/WWVH/CHU)"
echo -e "    wsprdaemon-client  — WSPR decoder + poster + uploader"
echo
