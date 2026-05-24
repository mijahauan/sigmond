#!/bin/bash
# sigmond.wizard_dispatch — shell-side helpers for per-client whiptail wizards.
#
# DRAFT.  See README.md in this directory for status and the observed
# contract this captures.
#
# A client's scripts/config-wizard.sh sources this for the universal
# preflight + colored stderr loggers.  Suggested usage:
#
#   #!/bin/bash
#   set -euo pipefail
#   SIGMOND_WIZARD_LIB_SH="${SIGMOND_WIZARD_LIB_SH:-/opt/git/sigmond/sigmond/lib/sigmond/wizard_dispatch/wizard_dispatch.sh}"
#   # shellcheck disable=SC1090
#   . "$SIGMOND_WIZARD_LIB_SH"
#   preflight_or_exit_2
#   ...

# Recommended whiptail box sizing.  Clients can override before sourcing
# (set explicitly first, the test below preserves) or after sourcing.
: "${WIDTH:=78}"
: "${HEIGHT:=20}"
: "${LIST_HEIGHT:=10}"
: "${BACKTITLE:=sigmond client configuration}"

# Colored stderr loggers.  Identical across all three current wizards.
_info() { printf '  %s\n'                "$*" >&2; }
_warn() { printf '  \033[33m⚠\033[0m %s\n' "$*" >&2; }
_err()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }

# Preflight check the wizard scripts repeat: refuse to run if whiptail
# isn't installed or stdout isn't a TTY.  This is belt-and-braces —
# the calling Python (`is_wizard_available` in wizard_dispatch.py)
# already gated for both conditions, but the wizard script may be
# invoked directly during development.  Exit code 2 distinguishes
# "shouldn't have been called" from "operator cancelled" (exit 0)
# and "real error" (exit 1).
preflight_or_exit_2() {
    if ! command -v whiptail >/dev/null 2>&1; then
        _err "whiptail not found on PATH; this script should not have been invoked."
        exit 2
    fi
    if [[ ! -t 1 ]]; then
        _err "stdout is not a TTY; this script should not have been invoked."
        exit 2
    fi
}
