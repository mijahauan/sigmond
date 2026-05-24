#!/usr/bin/env bash
# scripts/install/ensure_uv.sh — shared sigmond-suite installer helper.
#
# Defines `_ensure_uv`, the function each client install.sh calls to
# guarantee uv (https://astral.sh/uv) is on $PATH before running
# `uv venv` / `uv sync` / `uv pip install`.
#
# Sourced from each client's install.sh via:
#
#     _ENSURE_UV_SH="/opt/git/sigmond/sigmond/scripts/install/ensure_uv.sh"
#     if [[ -r "$_ENSURE_UV_SH" ]]; then
#         # shellcheck source=/dev/null
#         source "$_ENSURE_UV_SH"
#     else
#         # Inline fallback for the bootstrap case where sigmond hasn't
#         # been cloned yet -- keep in sync with this file.  (Standalone
#         # clients like hs-uploader and gpsdo-monitor don't otherwise
#         # require sigmond, so we don't force-clone it.)
#         _ensure_uv() { ...inline copy... }
#     fi
#     _ensure_uv
#
# Idempotent.  Returns 0 if uv is already (or now) on PATH; returns
# non-zero on failure (does NOT exit, so callers can react via
# `_ensure_uv || exit 1` if they want a hard stop).  Output uses plain
# `[INFO]` / `[ERROR]` prefixes; callers with colored logging will see
# uncolored prefixes for these few lines, which is acceptable.
#
# See sigmond/CLAUDE.md "Fleet upgrade pattern" for the surrounding
# convention (uv editable installs, uv.lock-pinned syncs, etc.).

_ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        printf '[INFO]  uv %s at %s\n' \
            "$(uv --version 2>/dev/null | awk '{print $2}')" \
            "$(command -v uv)"
        return 0
    fi
    printf '[INFO]  uv not found -- installing system-wide to /usr/local/bin\n'
    if ! command -v curl >/dev/null; then
        printf '[ERROR] curl not found (apt install curl)\n' >&2
        return 1
    fi
    # Astral installer honors XDG_BIN_HOME for target dir; UV_NO_MODIFY_PATH=1
    # because /usr/local/bin is already on every shell's PATH (the older
    # --no-modify-path flag is deprecated as of uv 0.11.x).
    if ! curl -LsSf https://astral.sh/uv/install.sh \
        | env XDG_BIN_HOME=/usr/local/bin UV_NO_MODIFY_PATH=1 sh; then
        printf '[ERROR] uv installer failed\n' >&2
        return 1
    fi
    if ! command -v uv >/dev/null; then
        printf '[ERROR] uv installer ran but uv is still not on PATH\n' >&2
        return 1
    fi
    printf '[INFO]  uv %s installed\n' \
        "$(uv --version 2>/dev/null | awk '{print $2}')"
}
