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
EDITION="full"

usage() {
    cat <<'EOF'
Usage: ./scripts/build_installer.sh [--clean] [--skip-package] [--edition full|client]

  --clean                 Remove build/, dist/, and installer/Output first.
  --skip-package          Build and verify dist/OpenWhisper without creating packages.
  --edition full|client   Edition to build (default: full).

Set OPENWHISPER_PYTHON to override venv/bin/python.
EOF
}

while (($#)); do
    case "$1" in
        --clean) CLEAN=1 ;;
        --skip-package) SKIP_PACKAGE=1 ;;
        --edition)
            shift
            (($#)) || { echo "Missing value for --edition" >&2; usage >&2; exit 2; }
            EDITION="$1"
            ;;
        --edition=*)
            EDITION="${1#*=}"
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

case "$EDITION" in
    full|client) ;;
    *) echo "Unsupported --edition value: $EDITION (expected full or client)" >&2; exit 2 ;;
esac
export OPENWHISPER_EDITION="$EDITION"

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

step "Building OpenWhisper $VERSION ($EDITION) for Linux amd64"
echo "    Python: $($PYTHON --version 2>&1)"
echo "    glibc : $(getconf GNU_LIBC_VERSION 2>/dev/null || ldd --version | head -1)"

if (( CLEAN )); then
    step "Cleaning previous build output"
    rm -rf -- build dist installer/Output
fi

step "Generating application icons"
"$PYTHON" scripts/generate_icon.py

step "Freezing with PyInstaller ($EDITION)"
xvfb-run -a "$PYTHON" -m PyInstaller \
    --noconfirm --clean --log-level WARN OpenWhisper.spec

DIST_DIR="$REPO_ROOT/dist/OpenWhisper"
INTERNAL_DIR="$DIST_DIR/_internal"
EXE_PATH="$DIST_DIR/OpenWhisper"
[[ -x "$EXE_PATH" ]] || fail "expected executable not found: $EXE_PATH"

if [[ "$EDITION" == "full" ]]; then
    step "Staging bundled Parakeet Unified runtime (transcribe.cpp)"
    "$PYTHON" scripts/stage_bundled_runtime.py --dest "$INTERNAL_DIR"
fi

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

step "Verifying the frozen bundle ($EDITION)"

for path in \
    "$INTERNAL_DIR/edition.json" \
    "$INTERNAL_DIR/ui_qt/styles/theme.qss" \
    "$INTERNAL_DIR/ui_qt/assets/openwhisper.ico" \
    "$INTERNAL_DIR/ui_qt/assets/openwhisper.png" \
    "$INTERNAL_DIR/webui/dist/index.html" \
    "$INTERNAL_DIR/docs/linux-system-audio.md" \
    "$INTERNAL_DIR/THIRD_PARTY_NOTICES.md" \
    "$INTERNAL_DIR/third_party_licenses/PyQt6/LICENSE" \
    "$INTERNAL_DIR/third_party_licenses/Qt/LICENSE" \
    "$INTERNAL_DIR/PyQt6/Qt6/plugins/platforms/libqxcb.so" \
    "$INTERNAL_DIR/PyQt6/Qt6/plugins/imageformats/libqsvg.so"; do
    [[ -f "$path" ]] || fail "required bundle asset missing: ${path#"$DIST_DIR/"}"
done

actual_edition="$("$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("edition", ""))' "$INTERNAL_DIR/edition.json")"
[[ "$actual_edition" == "$EDITION" ]] || \
    fail "edition.json reported '$actual_edition', expected '$EDITION'"

if [[ "$EDITION" == "full" ]]; then
    for path in \
        "$INTERNAL_DIR/bundled_components/asr-transcribe/.installed" \
        "$INTERNAL_DIR/bundled_components/asr-transcribe/manifest.json" \
        "$INTERNAL_DIR/bundled_components/asr-transcribe/transcribe-native/libtranscribe.so" \
        "$INTERNAL_DIR/bundled_components/asr-transcribe/transcribe-native/libggml-vulkan.so"; do
        [[ -f "$path" ]] || fail "required bundle asset missing: ${path#"$DIST_DIR/"}"
    done
fi

broken_bundle_links="$(find -L "$DIST_DIR" -type l -print)"
[[ -z "$broken_bundle_links" ]] || \
    fail "frozen bundle contains broken symlinks: $broken_bundle_links"

require_native_tree() {
    local label="$1" pattern="$2"
    if ! find "$INTERNAL_DIR" -type f -path "$pattern" -name '*.so*' -print -quit | grep -q .; then
        fail "required native package is missing shared objects: $label"
    fi
}
if [[ "$EDITION" == "full" ]]; then
    require_native_tree ctranslate2 '*ctranslate2*'
    require_native_tree onnxruntime '*onnxruntime*'
    require_native_tree PyAV '*av*'
else
    for client_forbidden in \
        ctranslate2 faster_whisper onnxruntime tokenizers huggingface_hub hf_xet \
        av av.libs bundled_components/asr-transcribe services/whisper_models.json; do
        [[ ! -e "$INTERNAL_DIR/$client_forbidden" ]] || \
            fail "client-excluded package/asset leaked into the bundle: $client_forbidden"
    done
    leaked_client_libs="$(find "$INTERNAL_DIR" -type f \
        \( -name '*ctranslate2*.so*' -o -name '*onnxruntime*.so*' -o -name 'libavcodec*.so*' -o -name 'libavformat*.so*' -o -name 'libavutil*.so*' -o -name 'libswresample*.so*' -o -name 'libswscale*.so*' \) \
        -print)"
    [[ -z "$leaked_client_libs" ]] || \
        fail "client-excluded native ML/FFmpeg libraries leaked into the bundle: $leaked_client_libs"
fi

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

if [[ "$EDITION" == "client" ]]; then
    HEALTH_OUTPUT="build/release-health-client.json"
else
    HEALTH_OUTPUT="build/release-health.json"
fi
xvfb-run -a "$EXE_PATH" --self-test
xvfb-run -a "$PYTHON" scripts/check_release_health.py --executable "$EXE_PATH" --output "$HEALTH_OUTPUT" --timeout 45

dist_bytes="$(du -sb "$DIST_DIR" | cut -f1)"
echo "    bundle size: $(format_size "$dist_bytes")"
echo "    all required assets and native libraries are verified"

if (( SKIP_PACKAGE )); then
    step "Done (native packages skipped)"
    echo "    Frozen app: $DIST_DIR"
    exit 0
fi

step "Staging the shared Linux filesystem tree ($EDITION)"
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

OUTPUT_DIR="$REPO_ROOT/installer/Output"
if [[ "$EDITION" == "client" ]]; then
    PKG_NAME="openwhisper-client"
    CONFLICT_PKG="openwhisper"
    EXTRA_CONTROL_FIELDS=$'Provides: openwhisper\n'
    EXTRA_PKGINFO_FIELDS=$'provides = openwhisper\n'
    DESC_SUMMARY="Lightweight speech-to-text client for OpenWhisper Host and cloud APIs"
    DESC_BODY=$' OpenWhisper Client records microphone audio and connects to a paired\n OpenWhisper Host or configured cloud APIs without bundling local ML\n runtimes. It includes global hotkeys, auto-paste, transcription history,\n and a PyQt6 desktop interface.'
    PKGDESC="Lightweight speech-to-text client for OpenWhisper Host and cloud APIs"
    STAGE_ROOT="$REPO_ROOT/build/linux-stage-client"
    DOC_SUBDIR="usr/share/doc/openwhisper-client"
    LICENSE_SUBDIR="usr/share/licenses/openwhisper-client"
    DEB_ARTIFACT="$OUTPUT_DIR/OpenWhisper-Client-$VERSION-linux-amd64.deb"
    ARCH_ARTIFACT="$OUTPUT_DIR/OpenWhisper-Client-$VERSION-linux-x86_64.pkg.tar.zst"
else
    PKG_NAME="openwhisper"
    CONFLICT_PKG="openwhisper-client"
    EXTRA_CONTROL_FIELDS=""
    EXTRA_PKGINFO_FIELDS=""
    DESC_SUMMARY="Private, local-first speech-to-text desktop application"
    DESC_BODY=$' OpenWhisper records microphone audio and transcribes it with local Whisper\n models or configured cloud APIs. It includes global hotkeys, auto-paste,\n transcription history, and a PyQt6 desktop interface.'
    PKGDESC="Private, local-first speech-to-text desktop application"
    STAGE_ROOT="$REPO_ROOT/build/linux-stage"
    DOC_SUBDIR="usr/share/doc/openwhisper"
    LICENSE_SUBDIR="usr/share/licenses/openwhisper"
    DEB_ARTIFACT="$OUTPUT_DIR/OpenWhisper-$VERSION-linux-amd64.deb"
    ARCH_ARTIFACT="$OUTPUT_DIR/OpenWhisper-$VERSION-linux-x86_64.pkg.tar.zst"
fi

rm -rf -- "$STAGE_ROOT"
mkdir -p \
    "$STAGE_ROOT/usr/bin" \
    "$STAGE_ROOT/usr/lib/openwhisper" \
    "$STAGE_ROOT/usr/share/applications" \
    "$STAGE_ROOT/$DOC_SUBDIR/third-party" \
    "$STAGE_ROOT/usr/share/icons/hicolor/256x256/apps" \
    "$STAGE_ROOT/$LICENSE_SUBDIR" \
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
install -m 0644 LICENSE "$STAGE_ROOT/$DOC_SUBDIR/copyright"
install -m 0644 README.md "$STAGE_ROOT/$DOC_SUBDIR/README.md"
install -m 0644 docs/linux-system-audio.md \
    "$STAGE_ROOT/$DOC_SUBDIR/linux-system-audio.md"
install -m 0644 THIRD_PARTY_NOTICES.md \
    "$STAGE_ROOT/$DOC_SUBDIR/THIRD_PARTY_NOTICES.md"
install -m 0644 "$INTERNAL_DIR/third_party_licenses/PyQt6/LICENSE" \
    "$STAGE_ROOT/$DOC_SUBDIR/third-party/PyQt6-GPL-3.0.txt"
install -m 0644 "$INTERNAL_DIR/third_party_licenses/Qt/LICENSE" \
    "$STAGE_ROOT/$DOC_SUBDIR/third-party/Qt-LGPL-3.0.txt"
install -m 0644 LICENSE \
    "$STAGE_ROOT/$LICENSE_SUBDIR/LICENSE"
install -m 0644 "$INTERNAL_DIR/third_party_licenses/PyQt6/LICENSE" \
    "$STAGE_ROOT/$LICENSE_SUBDIR/PyQt6-GPL-3.0.txt"
install -m 0644 "$INTERNAL_DIR/third_party_licenses/Qt/LICENSE" \
    "$STAGE_ROOT/$LICENSE_SUBDIR/Qt-LGPL-3.0.txt"
gzip -n -9 -c CHANGELOG.md >"$STAGE_ROOT/$DOC_SUBDIR/changelog.gz"
chmod 0644 "$STAGE_ROOT/$DOC_SUBDIR/changelog.gz"

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

build_deb_package() {
    local artifact="$1"
    local deb_root="$2"
    local xz_level="$3"
    local expected_bundled_model="${4:-}"

    clone_stage "$deb_root"
    mkdir -p \
        "$deb_root/DEBIAN" \
        "$deb_root/usr/share/lintian/overrides"
    sed "s/^openwhisper binary:/$PKG_NAME binary:/" installer/linux/lintian-overrides \
        >"$deb_root/usr/share/lintian/overrides/$PKG_NAME"
    chmod 0644 "$deb_root/usr/share/lintian/overrides/$PKG_NAME"
    render_template \
        installer/linux/control.in "$deb_root/DEBIAN/control" \
        @PACKAGE@ "$PKG_NAME" \
        @VERSION@ "$VERSION" \
        @ARCHITECTURE@ "$ARCHITECTURE" \
        @GLIBC_MIN@ "$GLIBC_MIN" \
        @CONFLICTS@ "$CONFLICT_PKG" \
        @REPLACES@ "$CONFLICT_PKG" \
        @EXTRA_CONTROL_FIELDS@ "$EXTRA_CONTROL_FIELDS" \
        @INSTALLED_SIZE@ "$INSTALLED_SIZE" \
        @DESCRIPTION_SUMMARY@ "$DESC_SUMMARY" \
        @DESCRIPTION_BODY@ "$DESC_BODY"
    chmod 0755 "$deb_root/DEBIAN"
    chmod 0644 "$deb_root/DEBIAN/control"
    find "$deb_root/DEBIAN" "$deb_root/usr/share/lintian" -print0 | \
        xargs -0 touch -h -d "@$SOURCE_DATE_EPOCH"

    rm -f -- "$artifact"
    dpkg-deb --root-owner-group -Zxz "-z$xz_level" --build "$deb_root" "$artifact"

    dpkg-deb --info "$artifact" >/dev/null
    local package_contents
    package_contents="$(dpkg-deb --contents "$artifact")"
    grep -q './usr/lib/openwhisper/OpenWhisper$' <<<"$package_contents" || \
        fail "Debian package does not contain the application executable"
    grep -q './usr/lib/openwhisper/_internal/edition.json$' <<<"$package_contents" || \
        fail "Debian package does not contain edition.json"
    grep -q './usr/bin/openwhisper$' <<<"$package_contents" || \
        fail "Debian package does not contain the command launcher"
    grep -q './usr/bin/ow -> openwhisper$' <<<"$package_contents" || \
        fail "Debian package does not contain the ow command alias"
    grep -q './usr/share/applications/openwhisper.desktop$' <<<"$package_contents" || \
        fail "Debian package does not contain the desktop entry"
    grep -q './usr/share/icons/hicolor/256x256/apps/openwhisper.png$' \
        <<<"$package_contents" || fail "Debian package does not contain the desktop icon"
    grep -q "./$DOC_SUBDIR/linux-system-audio.md\$" \
        <<<"$package_contents" || fail "Debian package does not contain the Linux audio guide"
    if [[ "$EDITION" == "full" ]]; then
        grep -q './usr/share/doc/openwhisper/linux-system-audio.md$' \
            <<<"$package_contents" || fail "Debian package does not contain usr/share/doc/openwhisper/linux-system-audio.md"
    else
        ! grep -q 'bundled_components/asr-transcribe' <<<"$package_contents" || \
            fail "Client Debian package unexpectedly contains asr-transcribe"
    fi
    if [[ -n "$expected_bundled_model" ]]; then
        grep -q "./usr/lib/openwhisper/_internal/bundled_models/$expected_bundled_model/installed.json$" \
            <<<"$package_contents" || fail "All-in-one Debian package is missing bundled model: $expected_bundled_model"
    fi
    [[ "$(dpkg-deb --field "$artifact" Package)" == "$PKG_NAME" ]] || fail "wrong package name"
    [[ "$(dpkg-deb --field "$artifact" Version)" == "$VERSION" ]] || fail "wrong package version"
    [[ "$(dpkg-deb --field "$artifact" Architecture)" == "amd64" ]] || fail "wrong package architecture"
    [[ "$(dpkg-deb --field "$artifact" Conflicts)" == "$CONFLICT_PKG" ]] || fail "wrong Conflicts field"
    [[ "$(dpkg-deb --field "$artifact" Replaces)" == "$CONFLICT_PKG" ]] || fail "wrong Replaces field"

    lintian --fail-on error "$artifact"
}

build_arch_package() {
    local artifact="$1"
    local pacman_root="$2"
    local zstd_level="$3"
    local expected_bundled_model="${4:-}"

    clone_stage "$pacman_root"
    render_template \
        installer/linux/PKGINFO.in "$pacman_root/.PKGINFO" \
        @PKGNAME@ "$PKG_NAME" \
        @VERSION@ "$VERSION" \
        @PKGDESC@ "$PKGDESC" \
        @GLIBC_MIN@ "$GLIBC_MIN" \
        @BUILDDATE@ "$SOURCE_DATE_EPOCH" \
        @INSTALLED_SIZE_BYTES@ "$INSTALLED_SIZE_BYTES" \
        @CONFLICT@ "$CONFLICT_PKG" \
        @EXTRA_PKGINFO_FIELDS@ "$EXTRA_PKGINFO_FIELDS"
    chmod 0644 "$pacman_root/.PKGINFO"
    touch -h -d "@$SOURCE_DATE_EPOCH" "$pacman_root/.PKGINFO"

    # gzip -n keeps the header timestamp out of the reproducibility story, matching
    # changelog.gz. Force root ownership so pacman -Qkk matches a root install.
    (
        cd "$pacman_root"
        bsdtar --uid 0 --gid 0 --uname root --gname root \
            --format=mtree \
            --options='!all,use-set,type,uid,gid,mode,time,size,md5,sha256,link' \
            -cf - .PKGINFO usr
    ) | gzip -n -9 >"$pacman_root/.MTREE"
    chmod 0644 "$pacman_root/.MTREE"
    touch -h -d "@$SOURCE_DATE_EPOCH" "$pacman_root/.MTREE"

    # Pipe an uncompressed ustar stream through the zstd CLI rather than bsdtar's
    # --zstd filter so Ubuntu 22.04's libarchive build does not have to write zstd.
    # Skip --long; the default window is what pacman's libarchive expects.
    # .PKGINFO is the first archive member, .MTREE the second.
    rm -f -- "$artifact"
    (
        cd "$pacman_root"
        bsdtar --uid 0 --gid 0 --uname root --gname root --format=ustar \
            -cf - .PKGINFO .MTREE usr
    ) | zstd -T0 "-$zstd_level" -q -o "$artifact"

    local arch_contents first_member ow_listing
    arch_contents="$(zstd -d -c -- "$artifact" | bsdtar -tf -)"
    first_member="$(head -n1 <<<"$arch_contents")"
    [[ "$first_member" == ".PKGINFO" ]] || \
        fail "Arch package first member must be .PKGINFO, got '$first_member'"
    grep -q '^\.MTREE$' <<<"$arch_contents" || \
        fail "Arch package does not contain .MTREE"
    grep -q '^usr/lib/openwhisper/OpenWhisper$' <<<"$arch_contents" || \
        fail "Arch package does not contain the application executable"
    grep -q '^usr/lib/openwhisper/_internal/edition.json$' <<<"$arch_contents" || \
        fail "Arch package does not contain edition.json"
    grep -q '^usr/bin/openwhisper$' <<<"$arch_contents" || \
        fail "Arch package does not contain the command launcher"
    grep -q '^usr/bin/ow$' <<<"$arch_contents" || \
        fail "Arch package does not contain the ow command alias"
    ow_listing="$(zstd -d -c -- "$artifact" | bsdtar -tvf - usr/bin/ow)"
    grep -Eq 'usr/bin/ow -> openwhisper$' <<<"$ow_listing" || \
        fail "Arch package ow alias is not a symlink to openwhisper"
    grep -q '^usr/share/applications/openwhisper.desktop$' <<<"$arch_contents" || \
        fail "Arch package does not contain the desktop entry"
    grep -q '^usr/share/icons/hicolor/256x256/apps/openwhisper.png$' \
        <<<"$arch_contents" || fail "Arch package does not contain the desktop icon"
    for license_file in LICENSE PyQt6-GPL-3.0.txt Qt-LGPL-3.0.txt; do
        grep -q "^$LICENSE_SUBDIR/$license_file\$" \
            <<<"$arch_contents" || fail "Arch package is missing license file: $license_file"
    done
    if [[ "$EDITION" == "full" ]]; then
        grep -q '^usr/share/licenses/openwhisper/LICENSE$' <<<"$arch_contents" || fail "missing LICENSE"
        grep -q '^usr/share/licenses/openwhisper/PyQt6-GPL-3.0.txt$' <<<"$arch_contents" || fail "missing PyQt6 license"
        grep -q '^usr/share/licenses/openwhisper/Qt-LGPL-3.0.txt$' <<<"$arch_contents" || fail "missing Qt license"
    else
        ! grep -q 'bundled_components/asr-transcribe' <<<"$arch_contents" || \
            fail "Client Arch package unexpectedly contains asr-transcribe"
    fi
    grep -q "^$DOC_SUBDIR/linux-system-audio.md\$" \
        <<<"$arch_contents" || fail "Arch package does not contain the Linux audio guide"
    if [[ -n "$expected_bundled_model" ]]; then
        grep -q "^usr/lib/openwhisper/_internal/bundled_models/$expected_bundled_model/installed.json$" \
            <<<"$arch_contents" || fail "All-in-one Arch package is missing bundled model: $expected_bundled_model"
    fi
    grep -q '^depend = glibc>=' "$pacman_root/.PKGINFO" || \
        fail "Arch package metadata is missing the glibc dependency"
    grep -q "^conflict = $CONFLICT_PKG\$" "$pacman_root/.PKGINFO" || \
        fail "Arch package metadata is missing conflict = $CONFLICT_PKG"
}

desktop-file-validate installer/linux/openwhisper.desktop

if [[ "$EDITION" == "client" ]]; then
    step "Building the Client-Only Debian package"
    build_deb_package "$DEB_ARTIFACT" "$REPO_ROOT/build/linux-deb-client" 9

    step "Building the Client-Only Arch package"
    build_arch_package "$ARCH_ARTIFACT" "$REPO_ROOT/build/linux-pacman-client" 19

    deb_bytes="$(stat -c %s "$DEB_ARTIFACT")"
    arch_bytes="$(stat -c %s "$ARCH_ARTIFACT")"
    deb_digest="$(sha256sum "$DEB_ARTIFACT" | cut -d' ' -f1)"
    arch_digest="$(sha256sum "$ARCH_ARTIFACT" | cut -d' ' -f1)"
    step "Build complete (Client-Only)"
    echo "    Debian Client : $DEB_ARTIFACT"
    echo "    Arch Client   : $ARCH_ARTIFACT"
    echo "    Version       : $VERSION"
    echo "    Requires      : glibc >= $GLIBC_MIN"
    echo "    Debian Client : $(format_size "$deb_bytes") (installed $(format_size "$dist_bytes"))"
    echo "    Arch Client   : $(format_size "$arch_bytes")"
    echo "    SHA-256       : $deb_digest  $(basename "$DEB_ARTIFACT")"
    echo "    SHA-256       : $arch_digest  $(basename "$ARCH_ARTIFACT")"
    exit 0
fi

step "Building the Debian package"
build_deb_package "$DEB_ARTIFACT" "$REPO_ROOT/build/linux-deb" 9

# There is no Ubuntu-installable equivalent of lintian for pacman packages
# (namcap is Arch-only), so package-level validation of the .pkg.tar.zst
# happens in the archlinux:latest workflow smoke.
step "Building the Arch package"
build_arch_package "$ARCH_ARTIFACT" "$REPO_ROOT/build/linux-pacman" 19

step "Staging bundled speech model for all-in-one Linux packages"
ALL_IN_ONE_MODEL="$($PYTHON -c 'from services.local_asr.catalog import ALL_IN_ONE_MODEL; print(ALL_IN_ONE_MODEL)')"
"$PYTHON" scripts/stage_bundled_model.py \
    --dest "$STAGE_ROOT/usr/lib/openwhisper/_internal/bundled_models" \
    --model "$ALL_IN_ONE_MODEL"
find "$STAGE_ROOT/usr/lib/openwhisper/_internal/bundled_models" -print0 | \
    xargs -0 touch -h -d "@$SOURCE_DATE_EPOCH"
INSTALLED_SIZE="$(du -sk "$STAGE_ROOT/usr" | cut -f1)"
INSTALLED_SIZE_BYTES="$(du -sb "$STAGE_ROOT/usr" | cut -f1)"

DEB_ALLINONE_ARTIFACT="$OUTPUT_DIR/OpenWhisper-AllInOne-$VERSION-linux-amd64.deb"
ARCH_ALLINONE_ARTIFACT="$OUTPUT_DIR/OpenWhisper-AllInOne-$VERSION-linux-x86_64.pkg.tar.zst"

# Moderate compression levels for the all-in-one packages: quantized GGUF
# weights are nearly incompressible, so -z6 / -6 saves minutes with ~0% penalty.
step "Building the all-in-one Debian package"
build_deb_package "$DEB_ALLINONE_ARTIFACT" "$REPO_ROOT/build/linux-deb-allinone" 6 "$ALL_IN_ONE_MODEL"

step "Building the all-in-one Arch package"
build_arch_package "$ARCH_ALLINONE_ARTIFACT" "$REPO_ROOT/build/linux-pacman-allinone" 6 "$ALL_IN_ONE_MODEL"

deb_bytes="$(stat -c %s "$DEB_ARTIFACT")"
arch_bytes="$(stat -c %s "$ARCH_ARTIFACT")"
deb_aio_bytes="$(stat -c %s "$DEB_ALLINONE_ARTIFACT")"
arch_aio_bytes="$(stat -c %s "$ARCH_ALLINONE_ARTIFACT")"
deb_digest="$(sha256sum "$DEB_ARTIFACT" | cut -d' ' -f1)"
arch_digest="$(sha256sum "$ARCH_ARTIFACT" | cut -d' ' -f1)"
deb_aio_digest="$(sha256sum "$DEB_ALLINONE_ARTIFACT" | cut -d' ' -f1)"
arch_aio_digest="$(sha256sum "$ARCH_ALLINONE_ARTIFACT" | cut -d' ' -f1)"
step "Build complete"
echo "    Debian            : $DEB_ARTIFACT"
echo "    Debian All-in-One : $DEB_ALLINONE_ARTIFACT"
echo "    Arch              : $ARCH_ARTIFACT"
echo "    Arch All-in-One   : $ARCH_ALLINONE_ARTIFACT"
echo "    Version           : $VERSION"
echo "    Requires          : glibc >= $GLIBC_MIN"
echo "    Debian            : $(format_size "$deb_bytes") (installed $(format_size "$dist_bytes"))"
echo "    Debian All-in-One : $(format_size "$deb_aio_bytes")"
echo "    Arch              : $(format_size "$arch_bytes")"
echo "    Arch All-in-One   : $(format_size "$arch_aio_bytes")"
echo "    SHA-256           : $deb_digest  $(basename "$DEB_ARTIFACT")"
echo "    SHA-256           : $deb_aio_digest  $(basename "$DEB_ALLINONE_ARTIFACT")"
echo "    SHA-256           : $arch_digest  $(basename "$ARCH_ARTIFACT")"
echo "    SHA-256           : $arch_aio_digest  $(basename "$ARCH_ALLINONE_ARTIFACT")"
echo
echo "    Upload all Linux packages beside the Windows setup exes and win64 update archives."
