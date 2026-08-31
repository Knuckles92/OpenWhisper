#!/usr/bin/env bash
# Build the Apple Silicon macOS 14+ DMG for OpenWhisper.
#
# Produces an ad-hoc-signed (unnotarized) OpenWhisper.app packed as
# OpenWhisper-<version>-macos-arm64.dmg. PyInstaller does not cross-compile;
# this script must run on Darwin arm64.
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CLEAN=0
SKIP_DMG=0
MOUNT_POINT=""
STAGE_DIR=""
WORKDIR=""

usage() {
    cat <<'EOF'
Usage: ./scripts/build_installer_macos.sh [--clean] [--skip-dmg]

  --clean     Remove build/, dist/, and installer/Output first.
  --skip-dmg  Freeze and verify OpenWhisper.app without packing a DMG.

Environment:
  OPENWHISPER_PYTHON                 Override the venv interpreter.
  OPENWHISPER_MACOS_CODESIGN_IDENTITY
      Optional Developer ID Application identity. When unset, PyInstaller
      ad-hoc-signs the bundle (the default for the unnotarized preview).
EOF
}

while (($#)); do
    case "$1" in
        --clean) CLEAN=1 ;;
        --skip-dmg) SKIP_DMG=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

step() {
    printf '\n\033[1;36m==> %s\033[0m\n' "$*"
}

fail() {
    echo "error: $*" >&2
    exit 1
}

format_size() {
    local bytes="$1"
    if command -v numfmt >/dev/null 2>&1; then
        numfmt --to=iec-i --suffix=B "$bytes" 2>/dev/null && return 0
    fi
    printf '%s bytes' "$bytes"
}

cleanup() {
    local status=$?
    if [[ -n "$MOUNT_POINT" ]]; then
        hdiutil detach "$MOUNT_POINT" -force >/dev/null 2>&1 || true
        rm -rf -- "$MOUNT_POINT"
    fi
    if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
        rm -rf -- "$STAGE_DIR"
    fi
    if [[ -n "$WORKDIR" && -d "$WORKDIR" ]]; then
        rm -rf -- "$WORKDIR"
    fi
    return "$status"
}
trap cleanup EXIT

[[ "$(uname -s)" == "Darwin" ]] || fail "the macOS installer must be built on macOS"
[[ "$(uname -m)" == "arm64" ]] || fail "only Apple Silicon (arm64) is supported for the macOS DMG"

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

for command in codesign file hdiutil iconutil lipo otool plutil shasum; do
    command -v "$command" >/dev/null || fail "required build command not found: $command"
done

VERSION="$("$PYTHON" -c 'import _version; print(_version.__version__)')"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid _version.py value: $VERSION"
"$PYTHON" -c 'import PyInstaller' >/dev/null 2>&1 || \
    fail "PyInstaller is missing; install the release requirements and constraints"

BUNDLE_ID="tech.fiorilabs.openwhisper"
MIN_OS="14.0"
APP_NAME="OpenWhisper"
DIST_APP="$REPO_ROOT/dist/${APP_NAME}.app"
APP_BIN="$DIST_APP/Contents/MacOS/${APP_NAME}"
INFO_PLIST="$DIST_APP/Contents/Info.plist"
OUTPUT_DIR="$REPO_ROOT/installer/Output"
DMG_ARTIFACT="$OUTPUT_DIR/${APP_NAME}-${VERSION}-macos-arm64.dmg"
ICNS_PATH="$REPO_ROOT/build/macos/openwhisper.icns"

SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || fail "SOURCE_DATE_EPOCH must be an integer"
export SOURCE_DATE_EPOCH

step "Building OpenWhisper $VERSION for macOS arm64"
echo "    Python : $($PYTHON --version 2>&1)"
echo "    Host   : $(uname -s) $(uname -m)"
if [[ -n "${OPENWHISPER_MACOS_CODESIGN_IDENTITY:-}" ]]; then
    echo "    Sign   : $OPENWHISPER_MACOS_CODESIGN_IDENTITY"
else
    echo "    Sign   : ad-hoc (unnotarized preview)"
fi

if (( CLEAN )); then
    step "Cleaning previous build output"
    rm -rf -- build dist installer/Output
fi

step "Generating application icons"
"$PYTHON" scripts/generate_icon.py
[[ -f "$ICNS_PATH" ]] || fail "expected ICNS not found: $ICNS_PATH"

step "Freezing with PyInstaller"
"$PYTHON" -m PyInstaller --noconfirm --clean --log-level WARN OpenWhisper.spec

[[ -d "$DIST_APP" ]] || fail "expected app bundle not found: $DIST_APP"
[[ -x "$APP_BIN" ]] || fail "expected executable not found: $APP_BIN"
[[ -f "$INFO_PLIST" ]] || fail "expected Info.plist not found: $INFO_PLIST"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/openwhisper-macos-build.XXXXXX")"

step "Verifying Info.plist identity and privacy keys"
plutil -lint "$INFO_PLIST" >/dev/null

plist_string() {
    local key="$1"
    /usr/libexec/PlistBuddy -c "Print :$key" "$INFO_PLIST" 2>/dev/null \
        || fail "Info.plist is missing required key: $key"
}

actual_id="$(plist_string CFBundleIdentifier)"
actual_short="$(plist_string CFBundleShortVersionString)"
actual_version="$(plist_string CFBundleVersion)"
actual_min="$(plist_string LSMinimumSystemVersion)"
mic_usage="$(plist_string NSMicrophoneUsageDescription)"
audio_usage="$(plist_string NSAudioCaptureUsageDescription)"

[[ "$actual_id" == "$BUNDLE_ID" ]] || \
    fail "CFBundleIdentifier is '$actual_id', expected '$BUNDLE_ID'"
[[ "$actual_short" == "$VERSION" ]] || \
    fail "CFBundleShortVersionString is '$actual_short', expected '$VERSION'"
[[ "$actual_version" == "$VERSION" ]] || \
    fail "CFBundleVersion is '$actual_version', expected '$VERSION'"
[[ "$actual_min" == "$MIN_OS" ]] || \
    fail "LSMinimumSystemVersion is '$actual_min', expected '$MIN_OS'"
[[ -n "$mic_usage" ]] || fail "NSMicrophoneUsageDescription is empty"
[[ -n "$audio_usage" ]] || fail "NSAudioCaptureUsageDescription is empty"
if /usr/libexec/PlistBuddy -c "Print :NSScreenCaptureUsageDescription" "$INFO_PLIST" >/dev/null 2>&1; then
    fail "NSScreenCaptureUsageDescription must not be invented for this release"
fi
echo "    bundle id $actual_id, version $actual_short, min macOS $actual_min"

step "Verifying required bundle assets"
for relative in \
    "Contents/Resources/ui_qt/styles/theme.qss" \
    "Contents/Resources/ui_qt/assets/openwhisper.ico" \
    "Contents/Resources/ui_qt/assets/openwhisper.png" \
    "Contents/Resources/webui/dist/index.html" \
    "Contents/Resources/THIRD_PARTY_NOTICES.md" \
    "Contents/Resources/third_party_licenses/PyQt6/LICENSE" \
    "Contents/Resources/third_party_licenses/Qt/LICENSE"; do
    # PyInstaller may place data under Resources or Frameworks depending on
    # type; accept either sealed location.
    if [[ -f "$DIST_APP/$relative" ]]; then
        continue
    fi
    alt="${relative/Contents\/Resources/Contents/Frameworks}"
    if [[ -f "$DIST_APP/$alt" ]]; then
        continue
    fi
    # Some assets stay beside the executable via Frameworks cross-links.
    base="$(basename "$relative")"
    if find "$DIST_APP/Contents" -type f -name "$base" -print -quit | grep -q .; then
        continue
    fi
    fail "required bundle asset missing: $relative"
done

for forbidden in torch nvidia scipy sympy networkx; do
    if find "$DIST_APP" -type d -name "$forbidden" -print -quit | grep -q .; then
        fail "excluded package leaked into the bundle: $forbidden"
    fi
done

step "Running frozen --version and --self-test"
actual_version_cli="$("$APP_BIN" --version)"
[[ "$actual_version_cli" == "OpenWhisper $VERSION" ]] || \
    fail "frozen executable reported '$actual_version_cli', expected 'OpenWhisper $VERSION'"
"$APP_BIN" --self-test

step "Verifying code signature"
# Ad-hoc signatures are expected for the unnotarized preview. Do not gate on
# Gatekeeper/spctl success: Apple rejects unnotarized downloads by design.
codesign --verify --deep --strict --verbose=2 "$DIST_APP"
CODESIGN_DV="$WORKDIR/codesign-dv.txt"
# codesign -dv writes details to stderr.
codesign -dv --verbose=2 "$DIST_APP" >"$CODESIGN_DV" 2>&1 || true
if [[ -n "${OPENWHISPER_MACOS_CODESIGN_IDENTITY:-}" ]]; then
    grep -q "Authority=" "$CODESIGN_DV" \
        || fail "expected a real signing authority when OPENWHISPER_MACOS_CODESIGN_IDENTITY is set"
else
    if grep -Eqi 'Signature=adhoc|flags=0x[0-9a-f]*adhoc|Authority=\(ad hoc\)|signed by|adhoc' \
        "$CODESIGN_DV"; then
        echo "    ad-hoc signature confirmed"
    else
        # codesign -dv wording varies; accept Identifier presence after verify.
        grep -q "Identifier=$BUNDLE_ID" "$CODESIGN_DV" \
            || fail "codesign details missing expected bundle identifier"
        echo "    signature verified (ad-hoc default; no Developer ID identity configured)"
    fi
fi

step "Auditing Mach-O architecture and linkage"
ARCH_FAILURES="$WORKDIR/arch-failures.txt"
LINK_FAILURES="$WORKDIR/link-failures.txt"
: >"$ARCH_FAILURES"
: >"$LINK_FAILURES"

while IFS= read -r -d '' candidate; do
    file_desc="$(file -b "$candidate" 2>/dev/null || true)"
    case "$file_desc" in
        *Mach-O*) ;;
        *) continue ;;
    esac

    if ! lipo -archs "$candidate" 2>/dev/null | tr ' ' '\n' | grep -qx 'arm64'; then
        printf '%s\n  arches: %s\n' \
            "${candidate#"$DIST_APP/"}" \
            "$(lipo -archs "$candidate" 2>/dev/null || echo unknown)" \
            >>"$ARCH_FAILURES"
    fi

    # Reject absolute build-host library paths that would not exist on users'
    # machines. System frameworks and @rpath/@loader_path/@executable_path are fine.
    while IFS= read -r line; do
        [[ "$line" == *"is not an object file"* ]] && continue
        path="$(sed -E 's/^[[:space:]]+//; s/ \(compatibility.*//; s/ \(offset.*//' <<<"$line")"
        [[ -z "$path" ]] && continue
        case "$path" in
            "$candidate":) continue ;;
            @rpath/*|@loader_path/*|@executable_path/*|/System/*|/usr/lib/*) continue ;;
            /*)
                printf '%s\n  %s\n' "${candidate#"$DIST_APP/"}" "$path" >>"$LINK_FAILURES"
                ;;
        esac
    done < <(otool -L "$candidate" 2>/dev/null || true)
done < <(find "$DIST_APP" -type f -print0)

if [[ -s "$ARCH_FAILURES" ]]; then
    cat "$ARCH_FAILURES" >&2
    fail "one or more bundled Mach-O files lack an arm64 slice"
fi
if [[ -s "$LINK_FAILURES" ]]; then
    cat "$LINK_FAILURES" >&2
    fail "one or more bundled Mach-O files link absolute build-host paths"
fi
echo "    all Mach-O files include arm64; no absolute host library paths"

app_bytes="$(du -sk "$DIST_APP" | awk '{print $1 * 1024}')"
echo "    app size: $(format_size "$app_bytes")"

if (( SKIP_DMG )); then
    step "Done (DMG packing skipped)"
    echo "    App bundle: $DIST_APP"
    exit 0
fi

step "Staging drag-to-Applications DMG contents"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/openwhisper-macos-stage.XXXXXX")"
ditto -- "$DIST_APP" "$STAGE_DIR/${APP_NAME}.app"
ln -s /Applications "$STAGE_DIR/Applications"
[[ -d "$STAGE_DIR/${APP_NAME}.app" ]] || fail "failed to stage OpenWhisper.app"
[[ -L "$STAGE_DIR/Applications" ]] || fail "failed to create Applications symlink"

mkdir -p -- "$OUTPUT_DIR"
rm -f -- "$DMG_ARTIFACT"

step "Creating compressed DMG"
# UDZO is the standard downloadable disk image format. No internet-enable /
# Gatekeeper bypass tricks: users approve via Privacy & Security → Open Anyway.
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$STAGE_DIR" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG_ARTIFACT" >/dev/null

[[ -f "$DMG_ARTIFACT" ]] || fail "DMG was not created: $DMG_ARTIFACT"

step "Verifying DMG mount and nested app"
MOUNT_POINT="$(mktemp -d "${TMPDIR:-/tmp}/openwhisper-macos-mount.XXXXXX")"
hdiutil attach "$DMG_ARTIFACT" -readonly -nobrowse -mountpoint "$MOUNT_POINT" >/dev/null
[[ -d "$MOUNT_POINT/${APP_NAME}.app" ]] || fail "mounted DMG is missing OpenWhisper.app"
[[ -L "$MOUNT_POINT/Applications" ]] || fail "mounted DMG is missing Applications symlink"
MOUNTED_APP="$MOUNT_POINT/${APP_NAME}.app"
codesign --verify --deep --strict --verbose=2 "$MOUNTED_APP"
mounted_version="$("$MOUNTED_APP/Contents/MacOS/${APP_NAME}" --version)"
[[ "$mounted_version" == "OpenWhisper $VERSION" ]] || \
    fail "mounted app reported '$mounted_version', expected 'OpenWhisper $VERSION'"
hdiutil detach "$MOUNT_POINT" >/dev/null
rm -rf -- "$MOUNT_POINT"
MOUNT_POINT=""

dmg_bytes="$(stat -f%z "$DMG_ARTIFACT" 2>/dev/null || wc -c <"$DMG_ARTIFACT")"
dmg_hash="$(shasum -a 256 "$DMG_ARTIFACT" | awk '{print $1}')"

step "Build complete"
echo ""
echo "  DMG     : $DMG_ARTIFACT"
echo "  App     : $DIST_APP"
echo "  Version : $VERSION"
echo "  Arch    : arm64"
echo "  Size    : $(format_size "$dmg_bytes")  (app $(format_size "$app_bytes"))"
echo "  SHA-256 : $dmg_hash"
echo ""
echo "  The app inside this DMG is ad-hoc signed; the DMG is unnotarized."
echo "  Gatekeeper will warn on first open; users approve via System Settings →"
echo "  Privacy & Security → Open Anyway."
echo "  Do not disable Gatekeeper or strip quarantine attributes as install advice."
echo ""
