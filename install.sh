#!/bin/bash
# One-time installer (macOS / Linux): registers `ow` and `openwhisper` as global
# commands by adding the scripts/ folder to your PATH.
#
# Idempotent: re-running removes any previous OpenWhisper PATH block and writes
# a fresh one, so moving the repo self-corrects. Only edits shell profile files
# -- your venv, code, and the scripts/ folder are left untouched.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$REPO/scripts"
VENV_PYTHON="$REPO/venv/bin/python"
MARK_BEGIN="# >>> OpenWhisper >>>"
MARK_END="# <<< OpenWhisper <<<"
FISH_MARK_BEGIN="# >>> OpenWhisper >>>"
FISH_MARK_END="# <<< OpenWhisper <<<"

remove_posix_block() {
    local file="$1"
    [ -f "$file" ] || return 0
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
        $0==b {skip=1; next}
        $0==e {skip=0; next}
        skip!=1 {print}
    ' "$file" > "$file.owtmp" && mv "$file.owtmp" "$file"
}

remove_fish_block() {
    local file="$1"
    [ -f "$file" ] || return 0
    awk -v b="$FISH_MARK_BEGIN" -v e="$FISH_MARK_END" '
        $0==b {skip=1; next}
        $0==e {skip=0; next}
        skip!=1 {print}
    ' "$file" > "$file.owtmp" && mv "$file.owtmp" "$file"
}

append_posix_block() {
    local file="$1"
    mkdir -p "$(dirname "$file")"
    remove_posix_block "$file"
    {
        echo "$MARK_BEGIN"
        echo "export PATH=\"$SCRIPTS_DIR:\$PATH\""
        echo "$MARK_END"
    } >> "$file"
}

append_fish_block() {
    local file="$1"
    mkdir -p "$(dirname "$file")"
    remove_fish_block "$file"
    {
        echo "$FISH_MARK_BEGIN"
        echo "fish_add_path \"$SCRIPTS_DIR\""
        echo "$FISH_MARK_END"
    } >> "$file"
}

collect_profile_files() {
    # Cover common defaults: Debian/Ubuntu bash reads ~/.bashrc; macOS zsh reads
    # ~/.zprofile; macOS bash login shells read ~/.bash_profile.
    local -a files=()
    files+=("$HOME/.bashrc" "$HOME/.zprofile")
    if [ "$(uname -s)" = "Darwin" ]; then
        files+=("$HOME/.bash_profile")
    fi
    if [ "$(basename "${SHELL:-}")" = "fish" ]; then
        files+=("$HOME/.config/fish/config.fish")
    fi

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

if [ ! -x "$VENV_PYTHON" ]; then
    echo "[error] Virtual environment not found."
    echo "        Expected: $VENV_PYTHON"
    echo "        Create it first:"
    echo "          cd \"$REPO\" && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

chmod +x "$SCRIPTS_DIR/openwhisper" "$SCRIPTS_DIR/ow"

updated_files=()
while IFS= read -r profile; do
    case "$profile" in
        *.fish)
            append_fish_block "$profile"
            ;;
        *)
            append_posix_block "$profile"
            ;;
    esac
    updated_files+=("$profile")
done < <(collect_profile_files)

echo "[ok] Added $SCRIPTS_DIR to your PATH in:"
for profile in "${updated_files[@]}"; do
    echo "     $profile"
done
echo

case "$(basename "${SHELL:-bash}")" in
    zsh)
        echo "Run this in your current terminal, then try \`ow\`:"
        echo "  source \"$HOME/.zprofile\""
        ;;
    fish)
        echo "Run this in your current terminal, then try \`ow\`:"
        echo "  source \"$HOME/.config/fish/config.fish\""
        ;;
    *)
        echo "Run this in your current terminal, then try \`ow\`:"
        echo "  source \"$HOME/.bashrc\""
        ;;
esac
echo
echo "Or open a new terminal and run: ow"
