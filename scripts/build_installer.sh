#!/usr/bin/env bash
# Build the release-grade Debian/Ubuntu x86-64 installer.
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
  --skip-package  Build and verify dist/OpenWhisper without creating a .deb.

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

for command in file find ldd numfmt readelf sha256sum; do
    command -v "$command" >/dev/null || fail "required build command not found: $command"
done
if (( ! SKIP_PACKAGE )); then
    for command in desktop-file-validate dpkg-deb gzip lintian xvfb-run; do
        command -v "$command" >/dev/null || \
            fail "$command is required to build the release package"
    done
fi

VERSION="$($PYTHON -c 'import _version; print(_version.__version__)')"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid _version.py value: $VERSION"
"$PYTHON" -c 'import PyInstaller' >/dev/null 2>&1 || \
    fail "PyInstaller is missing; install the release requirements and constraints"

# Give icon generation, PyInstaller, and dpkg the same stable timestamp input.
# Repeated builds are reproducible when the locked inputs and toolchain match.
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
"$PYTHON" -m PyInstaller \
    --noconfirm --clean --log-level WARN OpenWhisper.spec

DIST_DIR="$REPO_ROOT/dist/OpenWhisper"
INTERNAL_DIR="$DIST_DIR/_internal"
EXE_PATH="$DIST_DIR/OpenWhisper"
[[ -x "$EXE_PATH" ]] || fail "expected executable not found: $EXE_PATH"

step "Verifying the frozen bundle"
for path in \
    "$INTERNAL_DIR/ui_qt/styles/theme.qss" \
    "$INTERNAL_DIR/ui_qt/assets/openwhisper.ico" \
    "$INTERNAL_DIR/ui_qt/assets/openwhisper.png" \
    "$INTERNAL_DIR/webui/dist/index.html" \
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
    step "Done (Debian package skipped)"
    echo "    Frozen app: $DIST_DIR"
    exit 0
fi

step "Building the Debian package"
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

PACKAGE_ROOT="$REPO_ROOT/build/linux-package"
OUTPUT_DIR="$REPO_ROOT/installer/Output"
ARTIFACT="$OUTPUT_DIR/OpenWhisper-$VERSION-linux-amd64.deb"
rm -rf -- "$PACKAGE_ROOT"
mkdir -p \
    "$PACKAGE_ROOT/DEBIAN" \
    "$PACKAGE_ROOT/usr/bin" \
    "$PACKAGE_ROOT/usr/lib/openwhisper" \
    "$PACKAGE_ROOT/usr/share/applications" \
    "$PACKAGE_ROOT/usr/share/doc/openwhisper" \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/256x256/apps" \
    "$PACKAGE_ROOT/usr/share/lintian/overrides" \
    "$OUTPUT_DIR"

cp -a "$DIST_DIR/." "$PACKAGE_ROOT/usr/lib/openwhisper/"
# Bytecode caches are build-host debris, not runtime inputs. Shared objects do
# not need execute permission; normalize wheel/PyInstaller modes before root
# installs the package.
find "$PACKAGE_ROOT/usr/lib/openwhisper" -type d -name __pycache__ \
    -prune -exec rm -rf -- {} +
find "$PACKAGE_ROOT/usr/lib/openwhisper" -type f \
    \( -name '*.so' -o -name '*.so.*' \) -exec chmod 0644 -- {} +
install -m 0755 installer/linux/openwhisper "$PACKAGE_ROOT/usr/bin/openwhisper"
ln -s openwhisper "$PACKAGE_ROOT/usr/bin/ow"
install -m 0644 installer/linux/openwhisper.desktop \
    "$PACKAGE_ROOT/usr/share/applications/openwhisper.desktop"
install -m 0644 ui_qt/assets/openwhisper.png \
    "$PACKAGE_ROOT/usr/share/icons/hicolor/256x256/apps/openwhisper.png"
install -m 0644 LICENSE "$PACKAGE_ROOT/usr/share/doc/openwhisper/copyright"
install -m 0644 README.md "$PACKAGE_ROOT/usr/share/doc/openwhisper/README.md"
gzip -n -9 -c CHANGELOG.md >"$PACKAGE_ROOT/usr/share/doc/openwhisper/changelog.gz"
chmod 0644 "$PACKAGE_ROOT/usr/share/doc/openwhisper/changelog.gz"
install -m 0644 installer/linux/lintian-overrides \
    "$PACKAGE_ROOT/usr/share/lintian/overrides/openwhisper"

broken_links="$(find -L "$PACKAGE_ROOT" -type l -print)"
[[ -z "$broken_links" ]] || fail "package contains broken symlinks: $broken_links"
unsafe_modes="$(find "$PACKAGE_ROOT" -xdev -type f \
    \( -perm /0022 -o -perm /6000 \) -print)"
[[ -z "$unsafe_modes" ]] || fail "package contains unsafe file modes: $unsafe_modes"

INSTALLED_SIZE="$(du -sk "$PACKAGE_ROOT/usr" | cut -f1)"
"$PYTHON" - \
    installer/linux/control.in "$PACKAGE_ROOT/DEBIAN/control" \
    "$VERSION" "$ARCHITECTURE" "$GLIBC_MIN" "$INSTALLED_SIZE" <<'PY'
from pathlib import Path
import sys

template, output, version, architecture, glibc_min, installed_size = sys.argv[1:]
text = Path(template).read_text(encoding="utf-8")
replacements = {
    "@VERSION@": version,
    "@ARCHITECTURE@": architecture,
    "@GLIBC_MIN@": glibc_min,
    "@INSTALLED_SIZE@": installed_size,
}
for marker, value in replacements.items():
    text = text.replace(marker, value)
import re
if re.search(r"@[A-Z_]+@", text):
    raise SystemExit("Unresolved placeholder in Debian control template")
Path(output).write_text(text, encoding="utf-8")
PY
chmod 0755 "$PACKAGE_ROOT/DEBIAN"
chmod 0644 "$PACKAGE_ROOT/DEBIAN/control"

# Normalize the staged tree too; copied bundle files otherwise retain build
# timestamps even though dpkg itself honors SOURCE_DATE_EPOCH.
find "$PACKAGE_ROOT" -print0 | xargs -0 touch -h -d "@$SOURCE_DATE_EPOCH"

rm -f -- "$ARTIFACT"
dpkg-deb --root-owner-group -Zxz -z9 --build "$PACKAGE_ROOT" "$ARTIFACT"

dpkg-deb --info "$ARTIFACT" >/dev/null
package_contents="$(dpkg-deb --contents "$ARTIFACT")"
grep -q './usr/lib/openwhisper/OpenWhisper$' <<<"$package_contents" || \
    fail "package does not contain the application executable"
grep -q './usr/bin/openwhisper$' <<<"$package_contents" || \
    fail "package does not contain the command launcher"
grep -q './usr/bin/ow -> openwhisper$' <<<"$package_contents" || \
    fail "package does not contain the ow command alias"
grep -q './usr/share/applications/openwhisper.desktop$' <<<"$package_contents" || \
    fail "package does not contain the desktop entry"
grep -q './usr/share/icons/hicolor/256x256/apps/openwhisper.png$' \
    <<<"$package_contents" || fail "package does not contain the desktop icon"
[[ "$(dpkg-deb --field "$ARTIFACT" Package)" == "openwhisper" ]] || fail "wrong package name"
[[ "$(dpkg-deb --field "$ARTIFACT" Version)" == "$VERSION" ]] || fail "wrong package version"
[[ "$(dpkg-deb --field "$ARTIFACT" Architecture)" == "amd64" ]] || fail "wrong package architecture"

desktop-file-validate installer/linux/openwhisper.desktop
# Fail malformed/policy-error packages while still reporting non-fatal
# warnings. Intentional PyInstaller embedded-library exceptions are documented
# narrowly in installer/linux/lintian-overrides.
lintian --fail-on error "$ARTIFACT"

artifact_bytes="$(stat -c %s "$ARTIFACT")"
digest="$(sha256sum "$ARTIFACT" | cut -d' ' -f1)"
step "Build complete"
echo "    Installer : $ARTIFACT"
echo "    Version   : $VERSION"
echo "    Requires  : glibc >= $GLIBC_MIN"
echo "    Package   : $(format_size "$artifact_bytes") (installed $(format_size "$dist_bytes"))"
echo "    SHA-256   : $digest"
echo
echo "    Upload this .deb beside the Windows setup exe and win64 update archive."
