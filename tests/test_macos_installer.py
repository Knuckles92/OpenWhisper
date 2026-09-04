"""Static contract tests for the Apple Silicon macOS DMG packaging path.

These run on every platform. They do not freeze a real ``.app``; that requires
Darwin arm64 and is exercised by ``scripts/build_installer_macos.sh`` in CI.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_installer_macos.sh"
SPEC = ROOT / "OpenWhisper.spec"
WORKFLOW = ROOT / ".github" / "workflows" / "build-installers.yml"
GENERATE_ICON = ROOT / "scripts" / "generate_icon.py"
CONSTRAINTS = ROOT / "requirements-release-constraints.txt"
APP_QT = ROOT / "app_qt.py"


def test_macos_build_script_is_executable_and_valid_bash():
    assert BUILD_SCRIPT.is_file()
    assert os.access(BUILD_SCRIPT, os.X_OK)
    completed = subprocess.run(
        ["bash", "-n"],
        input=BUILD_SCRIPT.read_bytes().replace(b"\r\n", b"\n"),
        capture_output=True,
        check=False,
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
    assert "entitlements_file=None" in spec
    assert "App Sandbox" in spec or "no permissive hardened-runtime" in spec.lower() \
        or "No App Sandbox" in spec


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
    source = APP_QT.read_text(encoding="utf-8")
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
    # Comment may mention spctl; the builder must not invoke it as a gate.
    assert not re.search(r"(^|\n)\s*spctl\b", script)
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
    # No Apple secrets or notarization in the first path.
    assert "notarytool" not in workflow
    assert "APPLE_ID" not in workflow
    assert "ASC_KEY" not in workflow
    assert "OPENWHISPER_MACOS_CODESIGN_IDENTITY" not in workflow.split(
        "macos:"
    )[1].split("bundle:")[0]


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
