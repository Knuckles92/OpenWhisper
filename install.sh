#!/bin/bash
# One-time installer (macOS): registers `ow` and `openwhisper` as global
# commands by adding the scripts/ folder to your PATH via ~/.zprofile.
#
# Idempotent: re-running removes any previous OpenWhisper PATH block and writes
# a fresh one, so moving the repo self-corrects. Only edits ~/.zprofile -- your
# venv, code, and the scripts/ folder are left untouched.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$REPO/scripts"
VENV_PYTHON="$REPO/venv/bin/python"
PROFILE="$HOME/.zprofile"
MARK_BEGIN="# >>> OpenWhisper >>>"
MARK_END="# <<< OpenWhisper <<<"

remove_block() {
    local file="$1"
    [ -f "$file" ] || return 0
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
        $0==b {skip=1; next}
        $0==e {skip=0; next}
        skip!=1 {print}
    ' "$file" > "$file.owtmp" && mv "$file.owtmp" "$file"
}

if [ ! -x "$VENV_PYTHON" ]; then
    echo "[error] Virtual environment not found."
    echo "        Expected: $VENV_PYTHON"
    echo "        Create it first:"
    echo "          cd \"$REPO\" && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

chmod +x "$SCRIPTS_DIR/openwhisper" "$SCRIPTS_DIR/ow"

remove_block "$PROFILE"
{
    echo "$MARK_BEGIN"
    echo "export PATH=\"$SCRIPTS_DIR:\$PATH\""
    echo "$MARK_END"
} >> "$PROFILE"

echo "[ok] Added $SCRIPTS_DIR to your PATH in $PROFILE"
echo "Open a new terminal (or run: source \"$PROFILE\") so 'ow' and 'openwhisper' work everywhere."
