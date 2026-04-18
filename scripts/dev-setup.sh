#!/bin/bash
# dev-setup.sh — create sigmond's dev venv (.venv) from pyproject.toml.
#
# Run from anywhere; resolves its own location.  Safe to re-run (recreates venv).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[tui,dev]'

# ka9q-python is installed editable from a sibling checkout if available.
# It is strongly recommended for the TUI's radiod screen but not required.
for path in ../ka9q-python /home/mjh/git/ka9q-python /opt/git/ka9q-python; do
    if [ -f "$path/pyproject.toml" ]; then
        .venv/bin/pip install -e "$path"
        break
    fi
done

echo
echo "Dev venv ready at $REPO_DIR/.venv"
echo "Activate with:  source .venv/bin/activate"
