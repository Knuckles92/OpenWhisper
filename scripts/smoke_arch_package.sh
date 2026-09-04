#!/usr/bin/env bash
# Install and smoke-test the Arch package inside archlinux:latest.
# EXPECTED_VERSION must be set; the package is mounted at
# /tmp/openwhisper.pkg.tar.zst.
set -euo pipefail

strip_noextract() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    local tmp
    tmp="$(mktemp)"
    # Official images omit usr/share/doc via NoExtract. Real Arch desktops do
    # not, and this smoke asserts the packaged Linux audio guide is installed.
    awk '/^NoExtract/{next} {print}' "$file" >"$tmp"
    cat "$tmp" >"$file"
    rm -f "$tmp"
}

pacman-key --init
pacman-key --populate archlinux
pacman -Sy --noconfirm archlinux-keyring
pacman -Syu --noconfirm
pacman -S --noconfirm --needed file namcap xorg-server-xvfb

strip_noextract /etc/pacman.conf
shopt -s nullglob
for file in /etc/pacman.conf.d/*.conf /etc/pacman.d/*.conf; do
    strip_noextract "$file"
done
rm -f /etc/pacman.conf.d/noextract.conf

pacman -U --noconfirm /tmp/openwhisper.pkg.tar.zst
set -x
test "$(openwhisper --version)" = "OpenWhisper $EXPECTED_VERSION"
test "$(ow --version)" = "OpenWhisper $EXPECTED_VERSION"
test "$(pacman -Q openwhisper)" = "openwhisper $EXPECTED_VERSION-1"
package_info="$(pacman -Qi openwhisper)"
grep -Fq "MIT" <<<"$package_info"
grep -Fq "GPL-3.0-only" <<<"$package_info"
grep -Fq "LGPL-3.0-only" <<<"$package_info"
test -f /usr/share/applications/openwhisper.desktop
test -f /usr/share/icons/hicolor/256x256/apps/openwhisper.png
test -f /usr/share/doc/openwhisper/linux-system-audio.md
test -f /usr/share/licenses/openwhisper/LICENSE
test -f /usr/share/licenses/openwhisper/PyQt6-GPL-3.0.txt
test -f /usr/share/licenses/openwhisper/Qt-LGPL-3.0.txt
pacman -Qkk openwhisper
namcap /tmp/openwhisper.pkg.tar.zst || \
    echo "namcap reported findings (advisory; not a gate)"
set +x

failures="$(mktemp)"
while IFS= read -r -d "" candidate; do
    file -b "$candidate" | grep -q ELF || continue
    if ! output="$(LD_LIBRARY_PATH=/usr/lib/openwhisper/_internal ldd "$candidate" 2>&1)"; then
        printf "%s\n%s\n" "$candidate" "$output" >>"$failures"
    elif grep -q "not found" <<<"$output"; then
        printf "%s\n%s\n" "$candidate" "$output" >>"$failures"
    fi
done < <(find /usr/lib/openwhisper -type f -print0)
test ! -s "$failures" || { cat "$failures" >&2; exit 1; }

Xvfb :99 -screen 0 1280x1024x24 >/tmp/xvfb.log 2>&1 &
xvfb_pid=$!
export DISPLAY=:99
sleep 2
kill -0 "$xvfb_pid" || { cat /tmp/xvfb.log >&2; exit 1; }
openwhisper --self-test
kill "$xvfb_pid"

pacman -R --noconfirm openwhisper
test ! -e /usr/lib/openwhisper
test ! -e /usr/bin/openwhisper
test ! -e /usr/bin/ow
