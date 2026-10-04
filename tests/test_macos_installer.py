"""Static contract tests for the Apple Silicon macOS DMG packaging path.

These run on every platform. They do not freeze a real ``.app``; that requires
Darwin arm64 and is exercised by ``scripts/build_installer_macos.sh`` in CI.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_installer_macos.sh"
SPEC = ROOT / "OpenWhisper.spec"
WORKFLOW = ROOT / ".github" / "workflows" / "build-installers.yml"
GENERATE_ICON = ROOT / "scripts" / "generate_icon.py"
CONSTRAINTS = ROOT / "requirements-release-constraints.txt"
ENTRYPOINT = ROOT / "main.py"


def test_macos_build_script_is_executable_and_valid_bash():
    assert BUILD_SCRIPT.is_file()
    assert os.access(BUILD_SCRIPT, os.X_OK)
    bash = shutil.which("bash")
    if os.name == "nt":
        # Prefer Git Bash over an inaccessible WindowsApps/WSL launcher alias.
        git = shutil.which("git")
        candidates = ([Path(git).resolve().parent.parent / "bin" / "bash.exe"] if git else [])
        candidates += [Path(os.environ[key]) / "Git" / "bin" / "bash.exe"
                       for key in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(key)]
        bash = next((str(path) for path in candidates if path.is_file()), bash)
    if not bash:
        pytest.skip("Bash is unavailable; native macOS CI validates shell syntax")
    completed = subprocess.run(
        [bash, "-n"],
        input=BUILD_SCRIPT.read_bytes().replace(b"\r\n", b"\n"),
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_spec_builds_macos_app_bundle_with_stable_identity():
    spec = SPEC.read_text(encoding="utf-8")
    assert 'MACOS_BUNDLE_IDENTIFIER = "tech.fiorilabs.openwhisper"' in spec
    assert 'MACOS_MINIMUM_SYSTEM_VERSION = "14.0"' in spec
    assert "app = BUNDLE(" in spec
    assert 'name=f"{APP_NAME}.app"' in spec
    assert 'bundle_identifier=MACOS_BUNDLE_IDENTIFIER' in spec
    assert '"LSMinimumSystemVersion": MACOS_MINIMUM_SYSTEM_VERSION' in spec
    assert '"NSMicrophoneUsageDescription"' in spec
    assert '"NSAudioCaptureUsageDescription"' in spec
    assert "NSScreenCaptureUsageDescription" not in spec
    assert 'target_arch = "arm64" if sys.platform == "darwin" else None' in spec
    assert "OPENWHISPER_MACOS_CODESIGN_IDENTITY" in spec
    # Entitlements only for a Developer ID (hardened runtime) build.
    assert "entitlements_file=str(MACOS_ENTITLEMENTS) if _codesign_identity else None" in spec
    assert "No App Sandbox" in spec


def test_hardened_runtime_entitlements_are_minimal_and_valid():
    import plistlib

    with open(ROOT / "installer" / "macos" / "OpenWhisper.entitlements", "rb") as handle:
        entitlements = plistlib.load(handle)
    assert entitlements == {
        "com.apple.security.device.audio-input": True,
        "com.apple.security.cs.disable-library-validation": True,
        "com.apple.security.cs.allow-jit": True,
        "com.apple.security.cs.allow-unsigned-executable-memory": True,
    }
    # No sandbox, Apple Events or other capabilities beyond the above.
    assert not any(key.startswith("com.apple.security.app-sandbox") for key in entitlements)


def test_spec_retains_lazy_macos_framework_imports():
    spec = SPEC.read_text(encoding="utf-8")
    for name in (
        "objc",
        "Foundation",
        "HIServices",
        "ScreenCaptureKit",
        "CoreMedia",
        "Quartz",
    ):
        assert f'"{name}"' in spec


def test_package_self_test_imports_macos_capture_stack():
    source = ENTRYPOINT.read_text(encoding="utf-8")
    # Keep the Darwin branch self-contained so a freeze cannot pass without SCK.
    darwin_block = source.split('elif sys.platform == "darwin":', 1)[1].split(
        "else:", 1
    )[0]
    for name in (
        "objc",
        "Foundation",
        "HIServices",
        "ScreenCaptureKit",
        "CoreMedia",
        "Quartz",
    ):
        assert f'"{name}"' in darwin_block


def test_release_constraints_pin_darwin_pyobjc():
    text = CONSTRAINTS.read_text(encoding="utf-8")
    required = (
        "pyobjc-core==12.2.2",
        "pyobjc-framework-ApplicationServices==12.2.2",
        "pyobjc-framework-Cocoa==12.2.2",
        "pyobjc-framework-CoreMedia==12.2.2",
        "pyobjc-framework-CoreText==12.2.2",
        "pyobjc-framework-Quartz==12.2.2",
        "pyobjc-framework-ScreenCaptureKit==12.2.2",
    )
    for pin in required:
        assert pin in text
        assert f'{pin} ; sys_platform == "darwin"' in text


def test_generate_icon_builds_icns_under_build_on_darwin():
    source = GENERATE_ICON.read_text(encoding="utf-8")
    assert 'ICNS_OUTPUT_PATH = REPO_ROOT / "build" / "macos" / "openwhisper.icns"' in source
    assert "def build_icns" in source
    assert "ICNS_BASE_SIZES = (16, 32, 128, 256, 512)" in source
    assert 'iconutil' in source
    assert 'if sys.platform == "darwin":' in source
    assert "build_icns()" in source


def test_macos_build_script_enforces_host_and_artifact_contract():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert '[[ "$(uname -s)" == "Darwin" ]]' in script
    assert '[[ "$(uname -m)" == "arm64" ]]' in script
    assert 'DMG_ARTIFACT="$OUTPUT_DIR/${APP_NAME}-${VERSION}-macos-arm64.dmg"' in script
    assert "tech.fiorilabs.openwhisper" in script
    assert "LSMinimumSystemVersion" in script
    assert "NSMicrophoneUsageDescription" in script
    assert "NSAudioCaptureUsageDescription" in script
    assert "NSScreenCaptureUsageDescription" in script  # explicitly rejected
    assert "must not be invented" in script
    assert "codesign --verify --deep --strict" in script
    assert 'codesign --verify --deep --strict --verbose=2 "$MOUNTED_APP"' in script
    assert 'rm -rf -- "$MOUNT_POINT"' in script
    # Gatekeeper gates only the notarized build; the ad-hoc preview cannot pass.
    notarized = [block.split("\nfi\n", 1)[0] for block in script.split("if (( NOTARIZE )); then")[1:]]
    outside = script
    for block in notarized:
        outside = outside.replace(block, "")
    assert not re.search(r"(^|\n)\s*spctl\b", outside)
    assert sum("spctl --assess" in block for block in notarized) == 2
    assert "lipo -archs" in script
    assert "otool -L" in script
    assert "hdiutil create" in script
    assert "-format UDZO" in script
    assert 'ln -s /Applications' in script
    assert '"$APP_BIN" --version' in script
    assert '"$APP_BIN" --self-test' in script
    assert "OPENWHISPER_MACOS_CODESIGN_IDENTITY" in script
    assert "Do not disable Gatekeeper" in script


def test_release_workflow_includes_macos_arm64_dmg():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "runs-on: macos-14" in workflow
    assert 'test "$(uname -m)" = "arm64"' in workflow
    assert "./scripts/build_installer_macos.sh --clean" in workflow
    assert "OpenWhisper-*-macos-arm64.dmg" in workflow
    assert "needs: [windows, linux, macos]" in workflow
    assert "expected_artifacts=5" in workflow
    assert 'gh release upload "$RELEASE_TAG" release/* --clobber' in workflow
    macos = workflow.split("\n  macos:")[1].split("\n  bundle:")[0]
    # Signing is opt-in: the credentials step runs only when both secrets
    # exist, and the keychain and key are removed whatever happens.
    assert "if: env.MACOS_CERTIFICATE_P12 != '' && env.MACOS_NOTARY_KEY_P8 != ''" in macos
    assert "${{ secrets.MACOS_CERTIFICATE_P12 }}" in macos
    assert 'echo "OPENWHISPER_MACOS_CODESIGN_IDENTITY=$identity"' in macos
    assert "security delete-keychain" in macos and "if: always()" in macos
    assert "APPLE_ID" not in workflow


def test_macos_build_notarizes_only_with_credentials_and_identity():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "NOTARIZE=0" in script
    assert "OPENWHISPER_MACOS_NOTARY_PROFILE" in script
    assert "OPENWHISPER_MACOS_NOTARY_KEY" in script
    assert "notarization needs OPENWHISPER_MACOS_CODESIGN_IDENTITY" in script
    assert 'xcrun notarytool submit "$artifact"' in script
    assert 'xcrun stapler staple "$DIST_APP"' in script
    assert 'xcrun stapler staple "$DMG_ARTIFACT"' in script
    assert 'codesign --sign "$OPENWHISPER_MACOS_CODESIGN_IDENTITY" --timestamp "$DMG_ARTIFACT"' in script
    assert "missing the hardened runtime" in script
    # The DMG is staged from the stapled app, and hashed after its own staple.
    assert script.index('xcrun stapler staple "$DIST_APP"') < script.index('ditto -- "$DIST_APP"')
    assert script.index('xcrun stapler staple "$DMG_ARTIFACT"') < script.index('dmg_hash="$(shasum')


def test_frozen_macos_install_channel_is_notify_only():
    from services.app_update import (
        ApplyMode,
        InstallChannel,
        ReleaseAsset,
        ReleaseInfo,
        can_apply,
        channel_label,
        resolve_release_apply_mode,
    )

    assert channel_label(InstallChannel.INSTALLER, "darwin") == "macOS application"

    windows_asset = ReleaseAsset(
        url="https://example.invalid/OpenWhisper-Setup.exe",
        name="OpenWhisper-Setup.exe",
        size_bytes=123,
        sha256="a" * 64,
    )
    release = ReleaseInfo(
        version="9.9.9",
        tag_name="v9.9.9",
        html_url="https://example.invalid/release",
        notes="",
        setup_asset=windows_asset,
        native_asset=windows_asset,
    )
    assert resolve_release_apply_mode(
        InstallChannel.INSTALLER,
        release,
        platform_name="darwin",
    ) == ApplyMode.NOTIFY_ONLY
    assert not can_apply(
        InstallChannel.INSTALLER,
        release,
        platform_name="darwin",
    )


def test_release_workflow_supports_documented_setup_only_recovery():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "setup_only_windows:" in workflow
    assert "rm -f release/OpenWhisper-*-win64.tar.xz" in workflow
    assert "expected_artifacts=4" in workflow
    assert "expected_artifacts=5" in workflow
    assert 'gh release delete-asset "$RELEASE_TAG" "$archive_name" --yes' in workflow
    assert 'release/* --clobber' in workflow
