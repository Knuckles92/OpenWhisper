#!/usr/bin/env bash
# Build the release-grade Debian/Ubuntu and Arch Linux x86-64 installers.
#
# PyInstaller is not a cross-compiler. Run this on x86-64 Linux, preferably
# Ubuntu 22.04 (glibc 2.35), which is also the release-workflow baseline.
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLEAN=0
SKIP_PACKAGE=0

usage() {
    cat <<'EOF'
Usage: ./scripts/build_installer.sh [--clean] [--skip-package]

  --clean         Remove build/, dist/, and installer/Output first.
  --skip-package  Build and verify dist/OpenWhisper without creating packages.

Set OPENWHISPER_PYTHON to override venv/bin/python.
EOF
}

while (($#)); do
    case "$1" in
        --clean) CLEAN=1 ;;
        --skip-package) SKIP_PACKAGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

step() {
    printf '\n\033[1;36m==> %s\033[0m\n' "$*"
}

format_size() {
    numfmt --to=iec-i --suffix=B "$1" 2>/dev/null || printf '%s bytes' "$1"
}

fail() {
    echo "error: $*" >&2
    exit 1
}

render_template() {
    local template="$1"
    local output="$2"
    shift 2
    "$PYTHON" - "$template" "$output" "$@" <<'PY'
from pathlib import Path
import re
import sys

template, output, *pairs = sys.argv[1:]
if len(pairs) % 2:
    raise SystemExit("replacements must be key/value pairs")
text = Path(template).read_text(encoding="utf-8")
for marker, value in zip(pairs[0::2], pairs[1::2]):
    text = text.replace(marker, value)
if re.search(r"@[A-Z_]+@", text):
    raise SystemExit(f"Unresolved placeholder in {template}")
Path(output).write_text(text, encoding="utf-8")
PY
}

clone_stage() {
    local dest="$1"
    rm -rf -- "$dest"
    mkdir -p "$dest"
    if ! cp -al -- "$STAGE_ROOT/usr" "$dest/usr" 2>/dev/null; then
        cp -a -- "$STAGE_ROOT/usr" "$dest/usr"
    fi
}

[[ "$(uname -s)" == "Linux" ]] || fail "the Linux installer must be built on Linux"
[[ "$(uname -m)" == "x86_64" ]] || fail "only x86_64 Linux is currently supported"

PYTHON="${OPENWHISPER_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    if [[ -x "$REPO_ROOT/venv/bin/python" ]]; then
        PYTHON="$REPO_ROOT/venv/bin/python"
    elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
        PYTHON="$REPO_ROOT/.venv/bin/python"
    else
        fail "virtual environment not found; create one with: python3 -m venv venv"
    fi
fi
[[ -x "$PYTHON" ]] || fail "Python is not executable: $PYTHON"

for command in file find ldd numfmt patchelf readelf sha256sum xvfb-run; do
    command -v "$command" >/dev/null || fail "required build command not found: $command"
done
if (( ! SKIP_PACKAGE )); then
    for command in bsdtar desktop-file-validate dpkg-deb gzip lintian zstd; do
        command -v "$command" >/dev/null || \
            fail "$command is required to build the release package"
    done
fi

VERSION="$($PYTHON -c 'import _version; print(_version.__version__)')"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid _version.py value: $VERSION"
"$PYTHON" -c 'import PyInstaller' >/dev/null 2>&1 || \
    fail "PyInstaller is missing; install the release requirements and constraints"

# Give icon generation, PyInstaller, and the packers the same stable timestamp
# input. Repeated builds are reproducible when the locked inputs and toolchain
# match.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || fail "SOURCE_DATE_EPOCH must be an integer"
export SOURCE_DATE_EPOCH

step "Building OpenWhisper $VERSION for Linux amd64"
echo "    Python: $($PYTHON --version 2>&1)"
echo "    glibc : $(getconf GNU_LIBC_VERSION 2>/dev/null || ldd --version | head -1)"

if (( CLEAN )); then
    step "Cleaning previous build output"
    rm -rf -- build dist installer/Output
fi

step "Generating application icons"
"$PYTHON" scripts/generate_icon.py

step "Freezing with PyInstaller"
xvfb-run -a "$PYTHON" -m PyInstaller \
    --noconfirm --clean --log-level WARN OpenWhisper.spec

DIST_DIR="$REPO_ROOT/dist/OpenWhisper"
INTERNAL_DIR="$DIST_DIR/_internal"
EXE_PATH="$DIST_DIR/OpenWhisper"
[[ -x "$EXE_PATH" ]] || fail "expected executable not found: $EXE_PATH"

step "Removing build-host library search paths"
PYTHON_BASE_PREFIX="$("$PYTHON" -c 'import sys; print(sys.base_prefix)')"
while IFS= read -r -d '' candidate; do
    if ! file -b "$candidate" | grep -q 'ELF'; then
        continue
    fi
    rpath="$(patchelf --print-rpath "$candidate" 2>/dev/null || true)"
    if [[ "$rpath" == "$PYTHON_BASE_PREFIX/lib" ]]; then
        patchelf --remove-rpath "$candidate"
    fi
done < <(find "$DIST_DIR" -type f -print0)

step "Verifying the frozen bundle"

for path in \
    "$INTERNAL_DIR/ui_qt/styles/theme.qss" \
    "$INTERNAL_DIR/ui_qt/assets/openwhisper.ico" \
    "$INTERNAL_DIR/ui_qt/assets/openwhisper.png" \
    "$INTERNAL_DIR/webui/dist/index.html" \
    "$INTERNAL_DIR/THIRD_PARTY_NOTICES.md" \
    "$INTERNAL_DIR/third_party_licenses/PyQt6/LICENSE" \
    "$INTERNAL_DIR/third_party_licenses/Qt/LICENSE" \
    "$INTERNAL_DIR/PyQt6/Qt6/plugins/platforms/libqxcb.so" \
    "$INTERNAL_DIR/PyQt6/Qt6/plugins/imageformats/libqsvg.so"; do
    [[ -f "$path" ]] || fail "required bundle asset missing: ${path#"$DIST_DIR/"}"
done

broken_bundle_links="$(find -L "$DIST_DIR" -type l -print)"
[[ -z "$broken_bundle_links" ]] || \
    fail "frozen bundle contains broken symlinks: $broken_bundle_links"

require_native_tree() {
    local label="$1" pattern="$2"
    if ! find "$INTERNAL_DIR" -type f -path "$pattern" -name '*.so*' -print -quit | grep -q .; then
        fail "required native package is missing shared objects: $label"
    fi
}
require_native_tree ctranslate2 '*ctranslate2*'
require_native_tree onnxruntime '*onnxruntime*'
require_native_tree PyAV '*av*'

for forbidden in torch nvidia scipy sympy networkx; do
    [[ ! -e "$INTERNAL_DIR/$forbidden" ]] || \
        fail "excluded package leaked into the bundle: $forbidden"
done

# A bundled build-host libstdc++ / libgcc_s shadows Arch's newer gcc-libs and
# then system mesa / libglvnd fail to load. The spec strips these; refuse to
# ship if they leak back in.
bundled_cxx="$(find "$DIST_DIR" \( -name 'libstdc++.so*' -o -name 'libgcc_s.so*' \) -print)"
[[ -z "$bundled_cxx" ]] || \
    fail "bundled C++ runtime would shadow the system copy on Arch: $bundled_cxx"

echo "    checking ELF dependencies"
LDD_FAILURES="$(mktemp)"
trap 'rm -f -- "$LDD_FAILURES"' EXIT
while IFS= read -r -d '' candidate; do
    if ! file -b "$candidate" | grep -q 'ELF'; then
        continue
    fi
    if ! output="$(LD_LIBRARY_PATH="$INTERNAL_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
        ldd "$candidate" 2>&1)"; then
        printf '%s\nldd analysis failed:\n%s\n\n' \
            "${candidate#"$DIST_DIR/"}" "$output" >>"$LDD_FAILURES"
        continue
    fi
    if grep -q 'not found' <<<"$output"; then
        printf '%s\n%s\n\n' "${candidate#"$DIST_DIR/"}" "$output" >>"$LDD_FAILURES"
    fi
done < <(find "$DIST_DIR" -type f -print0)
if [[ -s "$LDD_FAILURES" ]]; then
    cat "$LDD_FAILURES" >&2
    fail "one or more bundled ELF files have unresolved shared libraries"
fi

actual_version="$("$EXE_PATH" --version)"
[[ "$actual_version" == "OpenWhisper $VERSION" ]] || \
    fail "frozen executable reported '$actual_version', expected 'OpenWhisper $VERSION'"

if command -v xvfb-run >/dev/null; then
    xvfb-run -a "$EXE_PATH" --self-test
elif [[ -n "${DISPLAY:-}" ]]; then
    "$EXE_PATH" --self-test
else
    echo "    warning: bundle import self-test skipped (install xvfb to run it headlessly)" >&2
fi

dist_bytes="$(du -sb "$DIST_DIR" | cut -f1)"
echo "    bundle size: $(format_size "$dist_bytes")"
echo "    all required assets and native libraries are present"

if (( SKIP_PACKAGE )); then
    step "Done (native packages skipped)"
    echo "    Frozen app: $DIST_DIR"
    exit 0
fi

step "Staging the shared Linux filesystem tree"
ARCHITECTURE="$(dpkg --print-architecture)"
[[ "$ARCHITECTURE" == "amd64" ]] || fail "dpkg architecture must be amd64, got $ARCHITECTURE"

GLIBC_MIN="$({
    while IFS= read -r -d '' candidate; do
        if file -b "$candidate" | grep -q 'ELF'; then
            readelf --version-info "$candidate" 2>/dev/null
        fi
    done < <(find "$DIST_DIR" -type f -print0)
} | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | cut -d_ -f2 | sort -Vu | tail -1)"
GLIBC_MIN="${GLIBC_MIN:-2.17}"

STAGE_ROOT="$REPO_ROOT/build/linux-stage"
OUTPUT_DIR="$REPO_ROOT/installer/Output"
DEB_ARTIFACT="$OUTPUT_DIR/OpenWhisper-$VERSION-linux-amd64.deb"
ARCH_ARTIFACT="$OUTPUT_DIR/OpenWhisper-$VERSION-linux-x86_64.pkg.tar.zst"
rm -rf -- "$STAGE_ROOT"
mkdir -p \
    "$STAGE_ROOT/usr/bin" \
    "$STAGE_ROOT/usr/lib/openwhisper" \
    "$STAGE_ROOT/usr/share/applications" \
    "$STAGE_ROOT/usr/share/doc/openwhisper/third-party" \
    "$STAGE_ROOT/usr/share/icons/hicolor/256x256/apps" \
    "$OUTPUT_DIR"

cp -a "$DIST_DIR/." "$STAGE_ROOT/usr/lib/openwhisper/"
# Bytecode caches are build-host debris, not runtime inputs. Shared objects do
# not need execute permission; normalize wheel/PyInstaller modes before root
# installs the package.
find "$STAGE_ROOT/usr/lib/openwhisper" -type d -name __pycache__ \
    -prune -exec rm -rf -- {} +
find "$STAGE_ROOT/usr/lib/openwhisper" -type f \
    \( -name '*.so' -o -name '*.so.*' \) -exec chmod 0644 -- {} +
install -m 0755 installer/linux/openwhisper "$STAGE_ROOT/usr/bin/openwhisper"
ln -s openwhisper "$STAGE_ROOT/usr/bin/ow"
install -m 0644 installer/linux/openwhisper.desktop \
    "$STAGE_ROOT/usr/share/applications/openwhisper.desktop"
install -m 0644 ui_qt/assets/openwhisper.png \
    "$STAGE_ROOT/usr/share/icons/hicolor/256x256/apps/openwhisper.png"
install -m 0644 LICENSE "$STAGE_ROOT/usr/share/doc/openwhisper/copyright"
install -m 0644 README.md "$STAGE_ROOT/usr/share/doc/openwhisper/README.md"
install -m 0644 THIRD_PARTY_NOTICES.md \
    "$STAGE_ROOT/usr/share/doc/openwhisper/THIRD_PARTY_NOTICES.md"
install -m 0644 "$INTERNAL_DIR/third_party_licenses/PyQt6/LICENSE" \
    "$STAGE_ROOT/usr/share/doc/openwhisper/third-party/PyQt6-GPL-3.0.txt"
install -m 0644 "$INTERNAL_DIR/third_party_licenses/Qt/LICENSE" \
    "$STAGE_ROOT/usr/share/doc/openwhisper/third-party/Qt-LGPL-3.0.txt"
gzip -n -9 -c CHANGELOG.md >"$STAGE_ROOT/usr/share/doc/openwhisper/changelog.gz"
chmod 0644 "$STAGE_ROOT/usr/share/doc/openwhisper/changelog.gz"

broken_links="$(find -L "$STAGE_ROOT" -type l -print)"
[[ -z "$broken_links" ]] || fail "package contains broken symlinks: $broken_links"
unsafe_modes="$(find "$STAGE_ROOT" -xdev -type f \
    \( -perm /0022 -o -perm /6000 \) -print)"
[[ -z "$unsafe_modes" ]] || fail "package contains unsafe file modes: $unsafe_modes"

# Normalize the staged tree before either packer sees it so the .deb and the
# pacman package wrap the same files at the same timestamps.
find "$STAGE_ROOT" -print0 | xargs -0 touch -h -d "@$SOURCE_DATE_EPOCH"

INSTALLED_SIZE="$(du -sk "$STAGE_ROOT/usr" | cut -f1)"
INSTALLED_SIZE_BYTES="$(du -sb "$STAGE_ROOT/usr" | cut -f1)"

step "Building the Debian package"
DEB_ROOT="$REPO_ROOT/build/linux-deb"
clone_stage "$DEB_ROOT"
mkdir -p \
    "$DEB_ROOT/DEBIAN" \
    "$DEB_ROOT/usr/share/lintian/overrides"
install -m 0644 installer/linux/lintian-overrides \
    "$DEB_ROOT/usr/share/lintian/overrides/openwhisper"
render_template \
    installer/linux/control.in "$DEB_ROOT/DEBIAN/control" \
    @VERSION@ "$VERSION" \
    @ARCHITECTURE@ "$ARCHITECTURE" \
    @GLIBC_MIN@ "$GLIBC_MIN" \
    @INSTALLED_SIZE@ "$INSTALLED_SIZE"
chmod 0755 "$DEB_ROOT/DEBIAN"
chmod 0644 "$DEB_ROOT/DEBIAN/control"
find "$DEB_ROOT/DEBIAN" "$DEB_ROOT/usr/share/lintian" -print0 | \
    xargs -0 touch -h -d "@$SOURCE_DATE_EPOCH"

rm -f -- "$DEB_ARTIFACT"
dpkg-deb --root-owner-group -Zxz -z9 --build "$DEB_ROOT" "$DEB_ARTIFACT"

dpkg-deb --info "$DEB_ARTIFACT" >/dev/null
package_contents="$(dpkg-deb --contents "$DEB_ARTIFACT")"
grep -q './usr/lib/openwhisper/OpenWhisper$' <<<"$package_contents" || \
    fail "Debian package does not contain the application executable"
grep -q './usr/bin/openwhisper$' <<<"$package_contents" || \
    fail "Debian package does not contain the command launcher"
grep -q './usr/bin/ow -> openwhisper$' <<<"$package_contents" || \
    fail "Debian package does not contain the ow command alias"
grep -q './usr/share/applications/openwhisper.desktop$' <<<"$package_contents" || \
    fail "Debian package does not contain the desktop entry"
grep -q './usr/share/icons/hicolor/256x256/apps/openwhisper.png$' \
    <<<"$package_contents" || fail "Debian package does not contain the desktop icon"
[[ "$(dpkg-deb --field "$DEB_ARTIFACT" Package)" == "openwhisper" ]] || fail "wrong package name"
[[ "$(dpkg-deb --field "$DEB_ARTIFACT" Version)" == "$VERSION" ]] || fail "wrong package version"
[[ "$(dpkg-deb --field "$DEB_ARTIFACT" Architecture)" == "amd64" ]] || fail "wrong package architecture"

desktop-file-validate installer/linux/openwhisper.desktop
# Fail malformed/policy-error packages while still reporting non-fatal
# warnings. Intentional PyInstaller embedded-library exceptions are documented
# narrowly in installer/linux/lintian-overrides.
lintian --fail-on error "$DEB_ARTIFACT"

# There is no Ubuntu-installable equivalent of lintian for pacman packages
# (namcap is Arch-only), so package-level validation of the .pkg.tar.zst
# happens in the archlinux:latest workflow smoke.
step "Building the Arch package"
PACMAN_ROOT="$REPO_ROOT/build/linux-pacman"
clone_stage "$PACMAN_ROOT"
render_template \
    installer/linux/PKGINFO.in "$PACMAN_ROOT/.PKGINFO" \
    @VERSION@ "$VERSION" \
    @GLIBC_MIN@ "$GLIBC_MIN" \
    @BUILDDATE@ "$SOURCE_DATE_EPOCH" \
    @INSTALLED_SIZE_BYTES@ "$INSTALLED_SIZE_BYTES"
chmod 0644 "$PACMAN_ROOT/.PKGINFO"
touch -h -d "@$SOURCE_DATE_EPOCH" "$PACMAN_ROOT/.PKGINFO"

# gzip -n keeps the header timestamp out of the reproducibility story, matching
# changelog.gz. Force root ownership so pacman -Qkk matches a root install.
(
    cd "$PACMAN_ROOT"
    bsdtar --uid 0 --gid 0 --uname root --gname root \
        --format=mtree \
        --options='!all,use-set,type,uid,gid,mode,time,size,md5,sha256,link' \
        .PKGINFO usr
) | gzip -n -9 >"$PACMAN_ROOT/.MTREE"
chmod 0644 "$PACMAN_ROOT/.MTREE"
touch -h -d "@$SOURCE_DATE_EPOCH" "$PACMAN_ROOT/.MTREE"

# Pipe an uncompressed ustar stream through the zstd CLI rather than bsdtar's
# --zstd filter so Ubuntu 22.04's libarchive build does not have to write zstd.
# Skip --long; the default window is what pacman's libarchive expects.
# .PKGINFO is the first archive member, .MTREE the second.
rm -f -- "$ARCH_ARTIFACT"
(
    cd "$PACMAN_ROOT"
    bsdtar --uid 0 --gid 0 --uname root --gname root --format=ustar \
        -cf - .PKGINFO .MTREE usr
) | zstd -T0 -19 -q -o "$ARCH_ARTIFACT"

arch_contents="$(zstd -d -c -- "$ARCH_ARTIFACT" | bsdtar -tf -)"
first_member="$(head -n1 <<<"$arch_contents")"
[[ "$first_member" == ".PKGINFO" ]] || \
    fail "Arch package first member must be .PKGINFO, got '$first_member'"
grep -q '^\.MTREE$' <<<"$arch_contents" || \
    fail "Arch package does not contain .MTREE"
grep -q '^usr/lib/openwhisper/OpenWhisper$' <<<"$arch_contents" || \
    fail "Arch package does not contain the application executable"
grep -q '^usr/bin/openwhisper$' <<<"$arch_contents" || \
    fail "Arch package does not contain the command launcher"
grep -q '^usr/bin/ow$' <<<"$arch_contents" || \
    fail "Arch package does not contain the ow command alias"
ow_listing="$(zstd -d -c -- "$ARCH_ARTIFACT" | bsdtar -tvf - usr/bin/ow)"
grep -Eq 'usr/bin/ow -> openwhisper$' <<<"$ow_listing" || \
    fail "Arch package ow alias is not a symlink to openwhisper"
grep -q '^usr/share/applications/openwhisper.desktop$' <<<"$arch_contents" || \
    fail "Arch package does not contain the desktop entry"
grep -q '^usr/share/icons/hicolor/256x256/apps/openwhisper.png$' \
    <<<"$arch_contents" || fail "Arch package does not contain the desktop icon"
grep -q '^depend = glibc>=' "$PACMAN_ROOT/.PKGINFO" || \
    fail "Arch package metadata is missing the glibc dependency"

deb_bytes="$(stat -c %s "$DEB_ARTIFACT")"
arch_bytes="$(stat -c %s "$ARCH_ARTIFACT")"
deb_digest="$(sha256sum "$DEB_ARTIFACT" | cut -d' ' -f1)"
arch_digest="$(sha256sum "$ARCH_ARTIFACT" | cut -d' ' -f1)"
step "Build complete"
echo "    Debian    : $DEB_ARTIFACT"
echo "    Arch      : $ARCH_ARTIFACT"
echo "    Version   : $VERSION"
echo "    Requires  : glibc >= $GLIBC_MIN"
echo "    Debian    : $(format_size "$deb_bytes") (installed $(format_size "$dist_bytes"))"
echo "    Arch      : $(format_size "$arch_bytes")"
echo "    SHA-256   : $deb_digest  $(basename "$DEB_ARTIFACT")"
echo "    SHA-256   : $arch_digest  $(basename "$ARCH_ARTIFACT")"
echo
echo "    Upload both Linux packages beside the Windows setup exe and win64 update archive."
