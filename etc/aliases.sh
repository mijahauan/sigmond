# Sigmond shell aliases & functions — the repo-owned source of truth.
#
# An operator's ~/.bash_aliases sources THIS file (install.sh wires that up),
# so edits here propagate to every sigmond host on `git pull` — no per-host
# copy to re-sync.  Only what lives in this file gets installed into a shell;
# keep it to the curated set sigmond actually wants everywhere.

# Repo root, derived from this file's location (…/<repo>/etc/aliases.sh).
_SIGMOND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"

alias ll='ls -l'
alias lrt='ls -lrt'
alias cds="cd ${_SIGMOND_DIR:-/opt/git/sigmond/sigmond}"

# tm: attach to a tmux session by name, creating it if it doesn't exist.
#   tm          -> the "main" session
#   tm <name>   -> the named session
tm() {
    local session="${1:-main}"
    if tmux has-session -t "$session" 2>/dev/null; then
        tmux attach -t "$session"
    else
        tmux new-session -s "$session"
    fi
}

# --- smd bash tab-completion --------------------------------------------------
# Auto-load smd's tab-completion in every shell, cached.  The cache is
# regenerated whenever the smd executable is newer than it (e.g. after a
# `git pull` or an edit to bin/smd), so new shells always get current completion
# at near-zero startup cost (generating it fresh costs ~0.2s; sourcing the cache
# is instant).  Sourcing the completion also defines the `smdrefresh`,
# `smdt`, and `smdtui` helpers that ship inside it.
#
# To refresh the CURRENT shell after editing smd without opening a new terminal,
# run:  smdrefresh   (defined by the sourced completion script).
if command -v smd >/dev/null 2>&1; then
    _smd_completion_cache="${XDG_CACHE_HOME:-$HOME/.cache}/smd/completion.bash"
    _smd_bin="$(readlink -f "$(command -v smd)" 2>/dev/null || command -v smd)"
    if [[ ! -s "$_smd_completion_cache" || "$_smd_bin" -nt "$_smd_completion_cache" ]]; then
        mkdir -p "$(dirname "$_smd_completion_cache")" 2>/dev/null \
            && smd admin completion bash > "$_smd_completion_cache" 2>/dev/null
    fi
    [[ -s "$_smd_completion_cache" ]] && source "$_smd_completion_cache"
    unset _smd_completion_cache _smd_bin
fi
