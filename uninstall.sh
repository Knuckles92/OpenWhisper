#!/bin/bash
# Removes `ow` and `openwhisper` from your PATH by deleting the OpenWhisper
# block from shell profile files. Does not delete the venv, source code, or
# scripts/ folder -- re-running install.sh later restores the commands.
set -euo pipefail

MARK_BEGIN="# >>> OpenWhisper >>>"
MARK_END="# <<< OpenWhisper <<<"
FISH_MARK_BEGIN="# >>> OpenWhisper >>>"
FISH_MARK_END="# <<< OpenWhisper <<<"

remove_posix_block() {
    local file="$1"
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
        $0==b {skip=1; next}
        $0==e {skip=0; next}
        skip!=1 {print}
    ' "$file" > "$file.owtmp" && mv "$file.owtmp" "$file"
}

remove_fish_block() {
    local file="$1"
    awk -v b="$FISH_MARK_BEGIN" -v e="$FISH_MARK_END" '
        $0==b {skip=1; next}
        $0==e {skip=0; next}
        skip!=1 {print}
    ' "$file" > "$file.owtmp" && mv "$file.owtmp" "$file"
}

collect_profile_files() {
    local -a files=("$HOME/.bashrc" "$HOME/.zprofile")
    if [ "$(uname -s)" = "Darwin" ]; then
        files+=("$HOME/.bash_profile")
    fi
    files+=("$HOME/.config/fish/config.fish")

    local file seen=()
    for file in "${files[@]}"; do
        local duplicate=0
        for existing in "${seen[@]:-}"; do
            if [ "$existing" = "$file" ]; then
                duplicate=1
                break
            fi
        done
        if [ "$duplicate" -eq 0 ]; then
            seen+=("$file")
            printf '%s\n' "$file"
        fi
    done
}

removed=0
while IFS= read -r profile; do
    case "$profile" in
        *.fish)
            if [ -f "$profile" ] && grep -qF "$FISH_MARK_BEGIN" "$profile"; then
                remove_fish_block "$profile"
                echo "[ok] Removed OpenWhisper from PATH in $profile"
                removed=1
            fi
            ;;
        *)
            if [ -f "$profile" ] && grep -qF "$MARK_BEGIN" "$profile"; then
                remove_posix_block "$profile"
                echo "[ok] Removed OpenWhisper from PATH in $profile"
                removed=1
            fi
            ;;
    esac
done < <(collect_profile_files)

if [ "$removed" -eq 0 ]; then
    echo "[ok] OpenWhisper PATH entry not found (nothing to remove)."
else
    echo "Open a new terminal for the change to take effect."
fi
