#!/bin/bash
# Removes `ow` and `openwhisper` from your PATH by deleting the OpenWhisper
# block from ~/.zprofile. Does not delete the venv, source code, or scripts/
# folder -- re-running install.sh later restores the commands.
set -euo pipefail

PROFILE="$HOME/.zprofile"
MARK_BEGIN="# >>> OpenWhisper >>>"
MARK_END="# <<< OpenWhisper <<<"

if [ ! -f "$PROFILE" ] || ! grep -qF "$MARK_BEGIN" "$PROFILE"; then
    echo "[ok] OpenWhisper PATH entry not found in $PROFILE (nothing to remove)."
    exit 0
fi

awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0==b {skip=1; next}
    $0==e {skip=0; next}
    skip!=1 {print}
' "$PROFILE" > "$PROFILE.owtmp" && mv "$PROFILE.owtmp" "$PROFILE"

echo "[ok] Removed OpenWhisper from PATH in $PROFILE"
echo "Open a new terminal for the change to take effect."
